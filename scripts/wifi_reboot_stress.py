"""
wifi_reboot_stress.py - PPTP demo: reboot + Wi-Fi reconnect stress test.

Per iteration (default 100):
  1. Send `adb reboot` to device
  2. Wait the configured number of seconds
  3. Wait for device to come back online
  4. Give Wi-Fi a moment to re-associate
  5. Check Wi-Fi connection status
  6. PASS if connected, FAIL if not

At the end: print total / passed / success rate.

Contract (matches other PPTP scripts):
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional: iterations, wait_sec, wifi_settle_sec, back_online_timeout)

Run standalone:
    python scripts/wifi_reboot_stress.py --device <serial>
    python scripts/wifi_reboot_stress.py --device <serial> \
        --params '{"iterations": 3, "wait_sec": 90}'

Notes on ADB outages during the script:
  - During reboot, ADB commands will fail (device is offline). The script
    handles this: each adb_shell call returns gracefully (returncode != 0).
  - After reboot, the script polls sys.boot_completed until device is back.
  - If the user clicks "中断" on the platform UI, Ctrl+C is delivered and
    the script prints whatever summary it has so far.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

# Shared HTML report engine. A sibling module, resolved via sys.path[0] -
# CPython puts the script's own directory there, so this works both under the
# platform and for a plain `python scripts/wifi_reboot_stress.py`. See
# docs/REPORT_FORMAT.md.
import _pptp_report

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a summary line.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

SCRIPT_VERSION = "1.0.0"

# reports/stress-test/<module>/ - must match ARCHIVE_MODULES in server.py, which
# groups the archive tree the same way. Shares the "wifi" module with the two
# other wifi scripts; the filename prefix is what tells them apart.
REPORT_MODULE = "wifi"

# Frontend-configurable params (declared for the PPTP platform).
# The platform renders a config modal from this list and passes the chosen
# values back via `--params`. Field keys: name / label / type / default /
# min / max. Run with `--dump-params` to print this schema as JSON.
PARAMS = [
    {"name": "iterations", "label": "循环次数", "type": "int", "default": 100,
     "min": 1, "max": 100000},
    {"name": "wait_sec", "label": "重启后等待(秒)", "type": "int",
     "default": 60, "min": 1, "max": 3600},
    {"name": "wifi_settle_sec", "label": "上线后WiFi稳定等待(秒)", "type": "int",
     "default": 5, "min": 0, "max": 3600},
    {"name": "back_online_timeout", "label": "设备上线超时(秒)", "type": "int",
     "default": 90, "min": 5, "max": 3600},
]


def adb_shell(serial: str, *args: str, timeout: int = 10) -> tuple[int, str, str]:
    """Run `adb -s <serial> shell <args...>`.

    Never raises. Returns (returncode, stdout, stderr).
    Negative return codes are reserved for infrastructure errors:
      -1 = subprocess timeout
      -2 = adb executable not found
    """
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -2, "", "adb not found"


def check_wifi_connected(serial: str) -> bool:
    """Return True if device reports Wi-Fi as connected to a network.

    Tries modern `cmd wifi status` first (Android 10+); falls back to
    `dumpsys wifi` for older versions.
    """
    # Modern Android (10+): `cmd wifi status`
    rc, out, _ = adb_shell(serial, "cmd", "wifi", "status", timeout=10)
    if rc == 0 and out:
        low = out.lower()
        if "wifi is enabled" in low:
            # SSID present = actually associated to a network
            if "ssid:" in low and "ssid: <unknown" not in low:
                return True
            # Some Androids omit SSID but still say "connected"
            if "connected" in low:
                return True
            return False  # wifi on but no network
    # Older Android: dumpsys wifi
    rc, out, _ = adb_shell(serial, "dumpsys", "wifi", timeout=15)
    if rc != 0 or not out:
        return False
    # "mNetworkInfo [type: WIFI...]: state: CONNECTED/" indicates active connection
    return "state: CONNECTED" in out


def wait_for_device_online(serial: str, timeout_sec: int) -> bool:
    """Poll `sys.boot_completed` until device is back. Return True/False."""
    print(f"[wait] waiting for device back online (timeout {timeout_sec}s)...")
    deadline = time.time() + timeout_sec
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        rc, out, _ = adb_shell(serial, "getprop", "sys.boot_completed", timeout=5)
        if rc == 0 and out.strip() == "1":
            print(f"[wait] device back online after {attempt} polls "
                  f"({int(time.time() - (deadline - timeout_sec))}s)")
            return True
        time.sleep(2)
    print(f"[wait] TIMEOUT after {timeout_sec}s")
    return False


def save_report(device: str, iterations: int, wait_sec: int,
                wifi_settle_sec: int, back_online_timeout: int,
                results: list[bool]) -> str:
    """Write the JSON report under reports/stress-test/wifi/. Returns the path.

    The filename MUST contain the short device id (`dev_short`): server.py's
    fallback scan, used when the stdout sniffer missed the `report :` line,
    matches candidates on that substring. Without it a hard-killed run loses
    its report silently.
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test",
                              REPORT_MODULE)
    os.makedirs(report_dir, exist_ok=True)

    total = len(results)
    passed = sum(results)
    rate = (passed / total * 100) if total > 0 else 0.0
    data = {
        "test_name": "重启+WiFi回连测试",
        "device_id": device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "iterations_planned": iterations,
        "iterations_run": total,
        "passed": passed,
        "failed": total - passed,
        "success_rate": round(rate, 2),
        "per_iteration": ["PASS" if r else "FAIL" for r in results],
        "config": {
            "wait_sec": wait_sec,
            "wifi_settle_sec": wifi_settle_sec,
            "back_online_timeout": back_online_timeout,
        },
    }
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir, f"wifi_reboot_{dev_short}_{ts}.json")
    _pptp_report.atomic_write_json(json_path, data)
    return json_path


def main() -> int:
    # --dump-params is consumed by the PPTP platform to render the params
    # config modal. Must short-circuit before argparse (and before any device
    # interaction). ensure_ascii keeps stdout pure-ASCII for safe piping.
    if "--dump-params" in sys.argv:
        print(json.dumps({"fields": PARAMS}))
        return 0

    defaults = {f["name"]: f["default"] for f in PARAMS}

    p = argparse.ArgumentParser(description="Reboot + Wi-Fi reconnect stress test")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"iterations"?, "wait_sec"?, "wifi_settle_sec"?, "back_online_timeout"?}')
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    iterations = int(params.get("iterations", defaults["iterations"]))
    wait_sec = int(params.get("wait_sec", defaults["wait_sec"]))
    wifi_settle_sec = int(params.get("wifi_settle_sec", defaults["wifi_settle_sec"]))
    back_online_timeout = int(params.get("back_online_timeout", defaults["back_online_timeout"]))

    print(f"[config] device         = {args.device}")
    print(f"[config] iterations     = {iterations}")
    print(f"[config] wait_sec       = {wait_sec} (after reboot, before checking back)")
    print(f"[config] wifi_settle    = {wifi_settle_sec}s (after device back, before wifi check)")
    print(f"[config] online_timeout = {back_online_timeout}s (waiting for device back)")

    results: list[bool] = []

    try:
        for i in range(1, iterations + 1):
            print(f"\n========== iteration {i}/{iterations} ==========")

            # 1. Reboot
            print(f"[{i}] sending `adb reboot`...")
            rc, _, _ = adb_shell(args.device, "reboot", timeout=10)
            # `reboot` itself may "fail" because the device disconnects
            # mid-command. That's normal; we don't fail the iteration.
            print(f"[{i}] reboot issued (rc={rc}; non-zero is normal)")

            # 2. Wait the configured time before checking back
            print(f"[{i}] waiting {wait_sec}s...")
            time.sleep(wait_sec)

            # 3. Wait for device back online
            if not wait_for_device_online(args.device, back_online_timeout):
                print(f"[{i}] FAIL: device did not come back in time")
                results.append(False)
                continue

            # 4. Small settle for Wi-Fi to re-associate
            print(f"[{i}] settling {wifi_settle_sec}s for Wi-Fi...")
            time.sleep(wifi_settle_sec)

            # 5. Check wifi
            print(f"[{i}] checking Wi-Fi...")
            if check_wifi_connected(args.device):
                print(f"[{i}] PASS: Wi-Fi connected")
                results.append(True)
            else:
                print(f"[{i}] FAIL: Wi-Fi not connected")
                results.append(False)
    except KeyboardInterrupt:
        print("\n[interrupted] Ctrl+C received")
    finally:
        # Always print summary, even on interrupt
        total = len(results)
        passed = sum(results)
        rate = (passed / total * 100) if total > 0 else 0.0
        print(f"\n========== summary ==========")
        print(f"  passed: {passed} / {total}")
        print(f"  success rate: {rate:.1f}%")
        if results:
            print(f"  per-iteration: {' '.join('P' if r else 'F' for r in results)}")

        # The Chinese HTML twin of the report below, written here in the
        # `finally` so an interrupted run still leaves a readable page behind.
        # A run that never completed an iteration has no verdict to report, so
        # it says so instead of borrowing the FAIL that main() returns.
        judged = total > 0
        all_passed = judged and passed == total
        doc = _pptp_report.build_payload(
            script="wifi_reboot_stress.py",
            script_version=SCRIPT_VERSION,
            test_name="重启 + WiFi 回连压测",
            device_id=args.device,
            result=("PASS" if all_passed else "FAIL") if judged else None,
            level=("ok" if all_passed else "fail") if judged else "inconclusive",
            count_zh=f"{passed}/{total} 轮 WiFi 成功回连" if judged
                     else "未完整跑完任何一轮",
            warn_zh=("" if (all_passed or not judged) else
                     f"{total - passed} 轮重启后 WiFi 未连上,"
                     f"成功率 {rate:.1f}%"),
            rows=[
                _pptp_report.row("overall", "总体结果",
                                 ("PASS" if all_passed else "FAIL") if judged
                                 else "—",
                                 ("ok" if all_passed else "fail") if judged
                                 else "inconclusive",
                                 "全部轮次 WiFi 正常回连" if all_passed else
                                 ("存在失败轮次" if judged
                                  else "本次运行没有可判定的轮次")),
                _pptp_report.row("passed", "通过 / 总轮次",
                                 f"{passed} / {total}",
                                 ("ok" if all_passed else "fail") if judged
                                 else "inconclusive",
                                 f"计划 {iterations} 轮"),
                _pptp_report.row("rate", "WiFi 回连成功率", f"{rate:.1f}%",
                                 ("ok" if all_passed else "fail") if judged
                                 else "inconclusive",
                                 "每轮上线并稳定后检查 WiFi 是否已关联"),
            ],
            params_schema=PARAMS,
            params_values={"iterations": iterations, "wait_sec": wait_sec,
                           "wifi_settle_sec": wifi_settle_sec,
                           "back_online_timeout": back_online_timeout},
            sections=[
                _pptp_report.section_list(
                    "per_iteration", "逐轮结果",
                    [f"第 {i} 轮:{'PASS' if r else 'FAIL'}"
                     for i, r in enumerate(results, 1)],
                    sub_zh="与 stdout 里的 P/F 串一一对应", empty_zh="无"),
            ],
            detail={"iterations_planned": iterations, "iterations_run": total,
                    "passed": passed, "failed": total - passed,
                    "success_rate": round(rate, 2),
                    "per_iteration": ["PASS" if r else "FAIL" for r in results]})

        try:
            report_path = save_report(args.device, iterations, wait_sec,
                                      wifi_settle_sec, back_online_timeout,
                                      results)
            html_path = _pptp_report.write_html_report(report_path, doc)
            if html_path:
                print(f"  html             = {html_path}")
            print(f"  report           : {report_path}")
        except Exception as e:
            print(f"[warn] failed to save report: {e}")

        # Return 0 if all passed, non-zero otherwise (CI-friendly)
        return 0 if (total > 0 and passed == total) else 1


if __name__ == "__main__":
    sys.exit(main())