"""
sensor_reboot_stress.py - PPTP: reboot + sensor re-connect stress test.

Reboots the projector and, once it is back online, verifies the selected
sensor (gsensor / ToF) reads valid data again. Per iteration:
  1. `adb reboot`
  2. wait for device back online (`sys.boot_completed`), timeout `reboot_timeout`
  3. poll the sensor up to `POLL_COUNT` times, `POLL_INTERVAL` apart
  4. PASS iff a valid reading shows up within the polls

At the end: print success rate, save a JSON report to
`reports/stress-test/sensor/`, exit 0 iff rate >= `PASS_THRESHOLD`.

Only `sensor` / `iterations` / `reboot_timeout` are frontend params; sensor read
tuning (`SENSOR_WINDOW` / `POLL_COUNT` / `POLL_INTERVAL`) and the pass criteria
(`PASS_THRESHOLD`) are hard-coded module constants (per user).

Contract (matches other PPTP scripts):
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional; see PARAMS below)

Run standalone:
    python scripts/sensor_reboot_stress.py --device <serial>
    python scripts/sensor_reboot_stress.py --device <serial> \
        --params '{"sensor": "tof", "iterations": 3, "reboot_timeout": 90}'

Sensor reads (root/su required; dumpsys sensorservice cannot see these):
  gsensor : cat /dev/gsensor
        each line: 7 comma-separated ints, e.g. "76,2,2040,0,0,0,9797";
        field 7 is the effective accel reading (~9800 mm/s^2 at rest), -1 when
        invalid. We collect for `SENSOR_WINDOW` s (device-side timeout) and
        count the samples with field 7 > 0.
  ToF     : cat /sys/class/nd_tof/nds01/ranging_data_fast
        output "depth:1382,1379,1390,1379" + "confi:100,100,100,100".
  This device's su is "bare" (rejects `su -c`), so reads try several su styles
  and fall back to an interactive stdin `su` session (mirrors manual adb flow).
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

# Shared HTML report engine. A sibling module, resolved via sys.path[0] -
# CPython puts the script's own directory there, so this works both under the
# platform and for a plain `python scripts/sensor_reboot_stress.py`.
import _pptp_report

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a summary line.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

SCRIPT_VERSION = "1.0.0"

# ---- sensor device nodes & read tuning (internal, not frontend params) ----
GSENSOR_NODE = "/dev/gsensor"
TOF_NODE = "/sys/class/nd_tof/nds01/ranging_data_fast"
GSENSOR_MIN_LINES = 3      # min valid samples within a window => data is flowing
GSENSOR_MAX_SAMPLES = 20   # cap kept samples (data stream is fast)
GSENSOR_RAW_LINES = 120    # cap raw lines pulled over adb (head truncation)

# Sensor read tuning & pass criteria — HARD-CODED (per user, NOT frontend params).
SENSOR_WINDOW = 5          # collection window per read (sec)
POLL_COUNT = 8             # post-boot detection attempts
POLL_INTERVAL = 3          # seconds between attempts
PASS_THRESHOLD = 98.0      # success-rate threshold (%)

# Frontend-configurable params (declared for the PPTP platform).
# The platform renders a config modal from this list and passes the chosen
# values back via `--params`. Field keys: name / label / type / default /
# min / max / choices (choices only used when type == "select"). Run with
# `--dump-params` to print this schema as JSON.
PARAMS = [
    {"name": "sensor", "label": "传感器序列", "type": "select",
     "choices": ["gsensor", "tof"], "default": "gsensor"},
    {"name": "iterations", "label": "循环次数", "type": "int", "default": 100,
     "min": 1, "max": 100000},
    {"name": "reboot_timeout", "label": "设备上线超时(秒)", "type": "int",
     "default": 80, "min": 10, "max": 3600},
]


class ADBHelper:
    """ADB helpers for reading projector sensor nodes (root required)."""

    ERROR_MARKERS = ('permission denied', 'not found', 'no such file',
                     'operation not permitted', 'denied', 'error',
                     'invalid option', 'unknown option', 'usage',
                     'not a terminal', 'not an interactive')

    def __init__(self, device_id: str):
        self.device_id = device_id

    def run_adb_command(self, command: str, ignore_errors: bool = False) -> str:
        """Run a raw adb command string; return stdout ("" on failure)."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", check=not ignore_errors,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return "" if ignore_errors else ""

    def _run_capture(self, command: str, timeout: int | None = None) -> str:
        """Run a command; keep stdout even on non-zero exit (e.g. timeout 124)."""
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True,
                text=True, encoding="utf-8", errors="ignore", timeout=timeout,
            )
            return result.stdout or ""
        except subprocess.TimeoutExpired as e:
            out = e.stdout
            if isinstance(out, bytes):
                out = out.decode("utf-8", errors="ignore")
            return out or ""
        except Exception:
            return ""

    def read_device_file(self, node: str, timeout_sec: int,
                         max_lines: int | None = None) -> str:
        """Read a device node, trying several su styles in turn.

        This device's su is "bare" (rejects `su -c`), so the non-interactive
        attempts fail fast and we fall back to an interactive stdin su session
        (mirrors the manual `adb shell` -> `su` -> `cat` flow).
        """
        t = timeout_sec
        src = f"timeout {t} cat {node}"
        if max_lines:
            src += f" | head -n {max_lines}"
        attempts = [
            ("shell", src, "direct(root)"),
            ("shell", f"su -c '{src}'", "su -c"),
            ("shell", f"su 0 -c '{src}'", "su 0 -c"),
            ("shell", f"su 0 '{src}'", "su 0"),
        ]
        for mode, inner, label in attempts:
            cmd = f"adb -s {self.device_id} {mode} {inner}"
            out = self._run_capture(cmd, timeout=t + 8)
            out = out.replace("\r", "")
            if out and not any(m in out.lower() for m in self.ERROR_MARKERS):
                self._report_read_method(node, label)
                return out
        # stdin-pipe interactive su (closest to the manual su flow)
        out = self._su_session(src, t)
        out = out.replace("\r", "")
        if out and not any(m in out.lower() for m in self.ERROR_MARKERS):
            self._report_read_method(node, "interactive-su(stdin)")
            return out
        # PTY fallback: su -c + adb -t (allocates a pseudo-terminal)
        cmd = f"adb -s {self.device_id} -t shell su -c '{src}'"
        out = self._run_capture(cmd, timeout=t + 8)
        out = out.replace("\r", "")
        if out and not any(m in out.lower() for m in self.ERROR_MARKERS):
            self._report_read_method(node, "su -c +PTY")
            return out
        return ""

    def _report_read_method(self, node: str, label: str) -> None:
        """Print which read method first succeeded, once per node."""
        if not hasattr(self, "_reported_methods"):
            self._reported_methods = set()
        if label not in self._reported_methods:
            self._reported_methods.add(label)
            print(f"[read] {os.path.basename(node)} via {label}")

    def _su_session(self, inner_cmd: str, timeout_sec: int) -> str:
        """Interactive stdin-pipe su: `adb shell su` then feed the command."""
        p = None
        try:
            p = subprocess.Popen(
                f"adb -s {self.device_id} shell su",
                shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="ignore")
            # communicate feeds the command then closes the pipe
            out, _ = p.communicate(input=f"{inner_cmd}\nexit\n",
                                   timeout=timeout_sec + 15)
            return out or ""
        except Exception:
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.communicate()
                except Exception:
                    pass
            return ""

    def check_connected(self) -> bool:
        """True if `adb shell echo 1` round-trips correctly."""
        out = self.run_adb_command(
            f"adb -s {self.device_id} shell echo 1", ignore_errors=True)
        return out.strip() == "1"

    def reboot_device(self) -> None:
        self.run_adb_command(
            f"adb -s {self.device_id} shell reboot", ignore_errors=True)

    def wait_for_device_online(self, timeout: int) -> bool:
        """Poll sys.boot_completed until device is back. True/False."""
        print(f"[wait] waiting for device back online (timeout {timeout}s)...")
        start = time.time()
        while time.time() - start < timeout:
            out = self.run_adb_command(
                f"adb -s {self.device_id} shell getprop sys.boot_completed",
                ignore_errors=True)
            if out.strip() == "1":
                print(f"[wait] device back online ({time.time() - start:.1f}s)")
                return True
            time.sleep(3)
        print(f"[wait] TIMEOUT after {timeout}s")
        return False

    # ---------------- parsing ----------------
    @staticmethod
    def _parse_gsensor(raw: str) -> list[dict]:
        """Parse gsensor output -> list of {accel, line} valid samples.

        Each line: 7 comma-separated ints; field 7 is the effective accel
        reading (mm/s^2, ~9800 at rest), -1 when invalid. Keep field 7 > 0.
        """
        samples: list[dict] = []
        if not raw:
            return samples
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                vals = [int(p) for p in parts[:7]]
            except ValueError:
                continue
            accel = vals[6]
            if accel > 0:
                samples.append({"accel": accel, "line": line})
        return samples[:GSENSOR_MAX_SAMPLES]

    @staticmethod
    def _parse_tof(raw: str) -> tuple[list[int], list[int]]:
        """Parse ToF output -> (depth, confi). Output like:
            depth:1382,1379,1390,1379
            confi:100,100,100,100
        """
        depth: list[int] = []
        confi: list[int] = []
        if not raw:
            return depth, confi
        for line in raw.splitlines():
            line = line.strip()
            m = re.match(r"^depth\s*[:=,]\s*(.+)", line, re.I)
            if m:
                depth = [int(v) for v in re.findall(r"\d+", m.group(1))]
                continue
            m = re.match(r"^confi\s*[:=,]\s*(.+)", line, re.I)
            if m:
                confi = [int(v) for v in re.findall(r"\d+", m.group(1))]
        # only depth:/confi: labeled lines count (PTY echo may add stray lines)
        return depth, confi

    # ---------------- collection ----------------
    def collect_sensor(self, sensor: str, window: int) -> tuple[bool, dict | None]:
        """Collect one reading of the selected sensor.

        Returns (ok, value_or_None):
          gsensor: ok = >= GSENSOR_MIN_LINES valid samples
                   value = {"count", "accel", "min", "max"}
          tof:     ok = parsed depth non-empty
                   value = {"depth", "confi"}
        """
        if sensor == "gsensor":
            raw = self.read_device_file(
                GSENSOR_NODE, window, max_lines=GSENSOR_RAW_LINES)
            samples = self._parse_gsensor(raw)
            if len(samples) >= GSENSOR_MIN_LINES:
                accels = [s["accel"] for s in samples]
                value = {"count": len(samples), "accel": samples[-1]["accel"],
                         "min": min(accels), "max": max(accels)}
                return True, value
            return False, None
        # tof
        raw = self.read_device_file(TOF_NODE, window)
        depth, confi = self._parse_tof(raw)
        if depth:
            return True, {"depth": depth, "confi": confi}
        return False, None


def format_result(sensor: str, value: dict | None) -> str:
    """One-line ASCII summary of a collected sensor value."""
    if not value:
        return "no data"
    if sensor == "gsensor":
        return (f"accel={value['accel']}mm/s2 (valid {value['count']}, "
                f"range {value['min']}~{value['max']})")
    confi = value.get("confi")
    confi_s = f", confi={confi}" if confi else ""
    return f"depth={value['depth']}mm{confi_s}"


def save_report(device: str, sensor: str, sensor_cn: str, iterations: int,
                total_valid: int, stopped_early: bool, success_count: int,
                failure_count: int, success_rate: float, reboot_timeout: int,
                test_results: list) -> str:
    """Write the JSON report under reports/stress-test/sensor/. Returns path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test", "sensor")
    os.makedirs(report_dir, exist_ok=True)

    data = {
        "test_name": f"重启投影仪{sensor_cn}回连测试",
        "device_id": device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_attempts": iterations,
        "actual_completed": total_valid,
        "stopped_early": stopped_early,
        "success_count": success_count,
        "failure_count": failure_count,
        "success_rate": round(success_rate, 2),
        "pass_threshold": PASS_THRESHOLD,
        "sensor": sensor,
        "sensor_config": {
            "gsensor_node": GSENSOR_NODE,
            "gsensor_min_samples": GSENSOR_MIN_LINES,
            "tof_node": TOF_NODE,
            "reboot_timeout_sec": reboot_timeout,
            "sensor_window_sec": SENSOR_WINDOW,
            "poll_count": POLL_COUNT,
            "poll_interval_sec": POLL_INTERVAL,
        },
        "test_results": test_results,
    }
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir, f"reboot_{sensor}_{dev_short}_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return json_path


def main() -> int:
    # --dump-params is consumed by the PPTP platform to render the params
    # config modal. Must short-circuit before argparse (and before any device
    # interaction). The server decodes stdout as UTF-8, so Chinese labels here
    # are safe.
    if "--dump-params" in sys.argv:
        print(json.dumps({"fields": PARAMS}))
        return 0

    defaults = {f["name"]: f["default"] for f in PARAMS}

    p = argparse.ArgumentParser(
        description="Reboot + sensor re-connect stress test (gsensor / tof)")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"sensor"?, "iterations"?, "reboot_timeout"?}')
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    sensor = str(params.get("sensor", defaults["sensor"]))
    if sensor not in ("gsensor", "tof"):
        print(f"[warn] unknown sensor '{sensor}' - falling back to 'gsensor'")
        sensor = "gsensor"
    iterations = int(params.get("iterations", defaults["iterations"]))
    reboot_timeout = int(params.get("reboot_timeout", defaults["reboot_timeout"]))

    sensor_cn = "重力传感器(gsensor)" if sensor == "gsensor" else "ToF距离传感器"

    print(f"[config] device         = {args.device}")
    print(f"[config] sensor         = {sensor}")
    print(f"[config] iterations     = {iterations}")
    print(f"[config] reboot_timeout = {reboot_timeout}s")
    print(f"[config] sensor_window  = {SENSOR_WINDOW}s (hardcoded)")
    print(f"[config] poll           = {POLL_COUNT} x {POLL_INTERVAL}s (hardcoded)")
    print(f"[config] pass_threshold = {PASS_THRESHOLD}% (hardcoded)")

    adb = ADBHelper(args.device)
    # best-effort `adb root`; read_device_file falls back to su chains anyway
    adb.run_adb_command(f"adb -s {args.device} root", ignore_errors=True)

    if not adb.check_connected():
        print("[error] device not reachable via adb")
        return 1

    # Baseline read before any reboot (warns early if the sensor is dead)
    ok0, val0 = adb.collect_sensor(sensor, SENSOR_WINDOW)
    print(f"[baseline] {sensor} read before loop: "
          f"{'OK' if ok0 else 'no data'}"
          + (f" ({format_result(sensor, val0)})" if val0 else ""))

    success_count = 0
    failure_count = 0
    boot_times: list[float] = []
    test_results: list[dict] = []
    stopped_early = False

    try:
        for i in range(1, iterations + 1):
            print(f"\n--- iteration {i}/{iterations} ---")

            # 1. reboot the projector
            print("[1] reboot...")
            cycle_start = time.time()
            adb.reboot_device()

            # 2. wait for device back online
            online = adb.wait_for_device_online(timeout=reboot_timeout)
            boot_elapsed = time.time() - cycle_start

            if not online:
                print("[1] FAIL: device did not come back in time")
                failure_count += 1
                test_results.append({
                    "attempt": i, "ok": False, "value": None,
                    "boot_time": -1, "detect_time": -1,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "error": "device reboot timeout",
                })
                break

            boot_times.append(boot_elapsed)
            print(f"[2] device online in {boot_elapsed:.1f}s")

            # 3. poll the sensor until it reads valid data
            sensor_ok = False
            sensor_val = None
            for poll in range(1, POLL_COUNT + 1):
                sensor_ok, sensor_val = adb.collect_sensor(sensor, SENSOR_WINDOW)
                if sensor_ok:
                    print(f"[3] poll {poll}: {sensor} OK | "
                          f"{format_result(sensor, sensor_val)}")
                    break
                if poll < POLL_COUNT:
                    print(f"[3] poll {poll}: {sensor} no data, "
                          f"wait {POLL_INTERVAL}s")
                    time.sleep(POLL_INTERVAL)
                else:
                    print(f"[3] poll {poll}: {sensor} NG "
                          f"(no data after {POLL_COUNT} polls)")

            detect_elapsed = time.time() - cycle_start
            test_results.append({
                "attempt": i, "ok": sensor_ok, "value": sensor_val,
                "boot_time": round(boot_elapsed, 2),
                "detect_time": round(detect_elapsed, 2),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            if sensor_ok:
                success_count += 1
                print(f"[result] OK  | {format_result(sensor, sensor_val)}")
            else:
                failure_count += 1
                print("[result] NG")

            print(f"[time] boot {boot_elapsed:.1f}s | detect {detect_elapsed:.1f}s")

            if i % 10 == 0:
                rate = (success_count / i) * 100
                print(f"[progress] ok {success_count} | ng {failure_count} "
                      f"| rate {rate:.2f}%")
    except KeyboardInterrupt:
        stopped_early = True
        print("\n[interrupt] Ctrl+C received - summarizing completed iterations")

    total_valid = success_count + failure_count
    success_rate = (success_count / total_valid * 100) if total_valid > 0 else 0.0
    avg_boot = (sum(boot_times) / len(boot_times)) if boot_times else 0.0
    detect_times = [r["detect_time"] for r in test_results
                    if r["detect_time"] >= 0]
    avg_cycle = (sum(detect_times) / len(detect_times)) if detect_times else 0.0

    print(f"\n=== results ===")
    if stopped_early:
        print(f"  stopped early: planned {iterations}, completed {total_valid}")
    print(f"  ok            : {success_count}")
    print(f"  ng            : {failure_count}")
    if total_valid > 0:
        print(f"  success rate  : {success_rate:.2f}%")
    print(f"  avg boot      : {avg_boot:.1f}s")
    print(f"  avg cycle     : {avg_cycle:.1f}s")
    if not stopped_early:
        passed = success_rate >= PASS_THRESHOLD
        print(f"  OVERALL       : {'PASS' if passed else 'FAIL'} "
              f"(>= {PASS_THRESHOLD:.1f}%)")

    if failure_count > 0:
        print("  failed attempts:")
        for r in test_results:
            if not r["ok"]:
                print(f"    #{r['attempt']}: {r.get('error', 'sensor no data')}")

    # The Chinese HTML twin of the report printed above, built from the same
    # local values as that block so the two cannot disagree. This script prints
    # NO overall verdict line when the run was interrupted, so there is nothing
    # to mirror: `passed` only exists on the branch that prints it, and an
    # early-stopped run stays result=None - the page then says the run is not
    # judged instead of inventing a PASS out of a partial run.
    verdict = None if stopped_early else ("PASS" if passed else "FAIL")
    verdict_level = "inconclusive" if verdict is None else (
        "ok" if passed else "fail")

    warn_zh = ""
    if stopped_early:
        warn_zh = (f"运行提前结束(手动停止或中断),计划 {iterations} 轮,"
                   f"实际完成 {total_valid} 轮 - 本次不做通过/不通过判定")
    elif not passed:
        warn_zh = (f"成功率 {success_rate:.2f}% 低于达标线 "
                   f"{PASS_THRESHOLD:.1f}%,共 {failure_count} 轮重启后"
                   f"未能读到 {sensor} 数据")

    doc = _pptp_report.build_payload(
        script="sensor_reboot_stress.py",
        script_version=SCRIPT_VERSION,
        test_name=f"重启投影仪{sensor_cn}回连测试",
        device_id=args.device,
        result=verdict,
        level=verdict_level,
        count_zh=(f"提前结束:完成 {total_valid}/{iterations} 轮"
                  if stopped_early
                  else f"{success_count}/{total_valid} 轮读到有效数据"),
        warn_zh=warn_zh,
        rows=[
            _pptp_report.row("overall", "总体判定",
                             verdict if verdict else "未判定", verdict_level,
                             (f"达标线:成功率 >= {PASS_THRESHOLD:.1f}%"
                              if verdict else "运行提前结束,未做判定")),
            _pptp_report.row("planned", "计划轮次", iterations, "info",
                             f"每轮:重启 -> 等待上线 -> 轮询 {sensor}"),
            _pptp_report.row("completed", "实际完成轮次", total_valid, "info",
                             "提前结束,未跑满计划" if stopped_early
                             else "按计划跑满"),
            _pptp_report.row("ok", "成功次数", success_count,
                             "ok" if total_valid and success_count == total_valid
                             else "info",
                             f"{POLL_COUNT} 次轮询内读到有效数据即算成功"),
            _pptp_report.row("ng", "失败次数", failure_count,
                             "fail" if failure_count else "info",
                             "见下方失败明细" if failure_count else "无失败"),
            _pptp_report.row("rate", "成功率",
                             f"{success_rate:.2f}%" if total_valid else None,
                             verdict_level, f"达标线 {PASS_THRESHOLD:.1f}%"),
            _pptp_report.row("boot", "平均开机耗时", f"{avg_boot:.1f}s", "info",
                             (f"{len(boot_times)} 次成功上线参与平均"
                              if boot_times else "本次无成功上线记录")),
            _pptp_report.row("cycle", "平均单轮耗时", f"{avg_cycle:.1f}s", "info",
                             "从发起重启到本轮读数结束"),
        ],
        params_schema=PARAMS,
        params_values={"sensor": sensor, "iterations": iterations,
                       "reboot_timeout": reboot_timeout},
        sections=[
            _pptp_report.section_table(
                "failures", "失败明细", ["轮次", "原因"],
                [[r["attempt"], r.get("error", "sensor no data")]
                 for r in test_results if not r["ok"]],
                sub_zh="与 stdout 的 failed attempts 列表同源",
                empty_zh="本次无失败轮次"),
        ],
        # Same keys as the JSON report written below, so the page and the file
        # describe one run identically. Nothing secret in here, so there is no
        # hide_keys.
        detail={
            "total_attempts": iterations,
            "actual_completed": total_valid,
            "stopped_early": stopped_early,
            "success_count": success_count,
            "failure_count": failure_count,
            "success_rate": round(success_rate, 2),
            "pass_threshold": PASS_THRESHOLD,
            "sensor": sensor,
            "sensor_config": {
                "gsensor_node": GSENSOR_NODE,
                "gsensor_min_samples": GSENSOR_MIN_LINES,
                "tof_node": TOF_NODE,
                "reboot_timeout_sec": reboot_timeout,
                "sensor_window_sec": SENSOR_WINDOW,
                "poll_count": POLL_COUNT,
                "poll_interval_sec": POLL_INTERVAL,
            },
            "test_results": test_results,
        })

    # Report is always saved (even on early stop), matching the original test.
    try:
        report_path = save_report(
            args.device, sensor, sensor_cn, iterations, total_valid,
            stopped_early, success_count, failure_count, success_rate,
            reboot_timeout, test_results)
        html_path = _pptp_report.write_html_report(report_path, doc)
        if html_path:
            print(f"  html          = {html_path}")
        print(f"  report        : {report_path}")
    except Exception as e:
        print(f"[warn] failed to save report: {e}")

    if total_valid == 0:
        return 1
    if stopped_early:
        return 0
    return 0 if success_rate >= PASS_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(main())
