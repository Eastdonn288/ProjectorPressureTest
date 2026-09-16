"""
battery_inout_stress.py - PPTP: battery charge/discharge stress monitor.

Samples battery level / temperature / voltage / status from an Android
device (dumpsys battery) and streams them to the PPTP frontend as PERF
wire records for a live ECharts graph (battery level + temperature).

Battery data background (KTC projector, ro.config.batteryless=true):
  The battery is managed by an external BMS MCU. Android's health-service
  polls the MCU and pushes values into BatteryService, but polling is OFF by
  default. Enabling it via adb is blocked by SELinux (the health ShellCommand
  fails with "Failed transaction 2147483646"). The only path is the device
  serial console (a PC COM port): with a root shell on the console, send

      cmd android.hardware.health.IHealth/default set polling true

  and the adb dumpsys battery readings become live. This script re-arms the
  polling every SERIAL_RE_ENABLE_MIN minutes in case the device / health
  service restarts and resets it.

PERF wire format (one line per record, prefix "PERF|", JSON ascii-safe):
  PERF|{"type":"meta","sources":{"level":"dumpsys battery",...},
        "device":"MT9676"}
  PERF|{"type":"sample","clock":1756107600000,"t":12.3,"level":87,
        "temp":36.2,"voltage_mv":8400,"status":"charging",
        "jump":0,"jump_type":""}
  clock = wall-clock epoch ms at sample time; t = seconds since start.
  status keys: unknown / charging / discharging / not_charging / full.
  jump = level delta vs the previous sample (0 = no anomaly);
  jump_type = abnormal_rise / abnormal_drop / abnormal_jump / "" (none).

Stop conditions for a single run:
  - manual stop (SIGBREAK / KeyboardInterrupt)
  - charge mode: level >= 100 AND (status == full OR held at 100% for
    full_hold_sec) -> stop_reason "full"
  - any mode: 2 consecutive battery read failures (device powered off /
    adb unreachable) -> stop_reason "power_off"

Contract (matches other PPTP scripts):
  --device <serial>  (required, injected by PPTP platform)
  --params <json>    (optional; see PARAMS below)
  --probe            (standalone: test battery read + list serial ports)

Run standalone:
    python scripts/battery_inout_stress.py --device <serial> \
        --params '{"mode": "charge", "serial_port": "COM9", "interval_sec": 10}'
"""
import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import time

# Shared HTML report engine. A sibling module, resolved via sys.path[0] -
# CPython puts the script's own directory there, so this works both under the
# platform and for a plain `python scripts/battery_inout_stress.py`. See
# _pptp_report.py for the payload contract.
import _pptp_report

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a summary line.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

SCRIPT_VERSION = "1.0.0"

# Serial console / health polling config (probed on the actual device).
SERIAL_BAUD = 115200
SERIAL_RE_ENABLE_MIN = 5     # re-arm polling every N minutes (survives restarts)
POLLING_CMD_SET = "cmd android.hardware.health.IHealth/default set polling true"
POLLING_CMD_GET = "cmd android.hardware.health.IHealth/default get polling"

# Level jump detection (preserved from the original script). A change of
# >= JUMP_THRESHOLD against the expected direction - or ANY reversal - is
# flagged as abnormal. Direction follows the ACTUAL dumpsys status:
#   charging/full : level should rise (a drop = abnormal_drop, >= threshold
#                   rise = abnormal_rise)
#   discharging   : level should fall (a rise = abnormal_rise, >= threshold
#                   fall = abnormal_drop)
#   unknown/other : magnitude-only abnormal_jump
JUMP_THRESHOLD = 2

# dumpsys battery status int -> ASCII key.
STATUS_MAP = {1: "unknown", 2: "charging", 3: "discharging",
              4: "not_charging", 5: "full"}


def _com_port_choices():
    """Enumerate PC serial ports for the frontend dropdown (via pyserial)."""
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
        if ports:
            return [{"value": p.device,
                     "label": p.device + (f" | {p.description}" if p.description else "")}
                    for p in ports]
        return [{"value": "", "label": "(no COM port detected)"}]
    except Exception:
        return [{"value": "", "label": "(pyserial unavailable)"}]


def _params():
    """Frontend-configurable params (see perf_monitor.py contract)."""
    return [
        {"name": "mode", "label": "Mode (charge / discharge)", "type": "select",
         "default": "charge", "choices": ["charge", "discharge"]},
        {"name": "serial_port", "label": "Serial console port (COM)",
         "type": "select", "default": "", "choices": _com_port_choices()},
        {"name": "interval_sec", "label": "Sampling interval (s)", "type": "float",
         "default": 10.0, "min": 2.0, "max": 3600},
        {"name": "temp_warn_c", "label": "Temperature warning (C)", "type": "float",
         "default": 45.0, "min": 30.0, "max": 80.0},
        {"name": "full_hold_sec", "label": "Hold at 100% before stop (s)",
         "type": "int", "default": 60, "min": 0, "max": 3600},
    ]


PARAMS = _params()


# ---------------------------------------------------------------------------
# ADB layer
# ---------------------------------------------------------------------------
def adb_capture(args, timeout=None):
    """Run a subprocess, return stdout ("" on any failure). Never raises."""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def adb_shell(serial, command, timeout=None):
    """Run `adb -s <serial> shell <command>` (single-arg command). Returns stdout."""
    # list-arg (NOT shell=True): `dumpsys battery` must reach the DEVICE shell,
    # not be interpreted by the PC cmd.exe.
    return adb_capture(["adb", "-s", serial, "shell", command], timeout=timeout)


def check_adb_connected(serial):
    """True if `adb shell echo 1` round-trips correctly."""
    return adb_shell(serial, "echo 1", timeout=5).strip() == "1"


def getprop(serial, key):
    return adb_shell(serial, f"getprop {key}", timeout=5).strip()


# ---------------------------------------------------------------------------
# Battery read (dumpsys battery)
# ---------------------------------------------------------------------------
BATTERY_READ_CMD = "dumpsys battery"


def read_battery_raw(serial):
    """dumpsys battery output, or None after 3 failed attempts."""
    for attempt in range(3):
        out = adb_shell(serial, BATTERY_READ_CMD, timeout=8)
        if "level:" in out:
            return out
        if attempt < 2:
            time.sleep(1)
    return None


def parse_battery(text):
    """Parse dumpsys battery -> {level, status, temperature, voltage_mv}."""
    level = int(re.search(r"level:\s*(\d+)", text).group(1))
    status_raw = int(re.search(r"status:\s*(\d+)", text).group(1))
    temperature = int(re.search(r"temperature:\s*(\d+)", text).group(1)) / 10.0
    # Anchor at line start so we skip "Max charging voltage:" (uV, e.g. 12600000)
    # and grab the real `voltage:` field (mV).
    vm = re.search(r"(?m)^\s*voltage:\s*(\d+)", text)
    voltage_mv = int(vm.group(1)) if vm else 0
    return {"level": level,
            "status": STATUS_MAP.get(status_raw, "unknown"),
            "temperature": round(temperature, 1),
            "voltage_mv": voltage_mv}


def get_battery_info(serial):
    """Read + parse battery. Returns dict, or {"error": ...} on failure."""
    raw = read_battery_raw(serial)
    if raw is None:
        return {"error": "device not reachable / battery unreadable"}
    try:
        return parse_battery(raw)
    except Exception as e:
        return {"error": f"battery parse failed: {e}"}


# ---------------------------------------------------------------------------
# Serial console (enable health polling - the ONLY way adb readings go live)
# ---------------------------------------------------------------------------
def _serial_read_until(ser, sentinel, timeout):
    """Read from the serial console until sentinel seen + 150ms quiet (or timeout).

    The console echoes the typed command (mksh line editing), so the sentinel
    first appears in the echo and the real output comes last. Requiring
    "sentinel seen AND 150ms of silence" guarantees we captured both the echo
    and the full real output.
    """
    buf = b""
    deadline = time.time() + timeout
    marker_seen = False
    quiet_deadline = 0.0
    while time.time() < deadline:
        n = ser.in_waiting
        if n > 0:
            chunk = ser.read(n)
            buf += chunk
            if sentinel in buf:
                marker_seen = True
                quiet_deadline = time.time() + 0.15
        elif marker_seen and time.time() >= quiet_deadline:
            break
        else:
            time.sleep(0.02)
    return buf.decode("utf-8", errors="replace")


def _serial_cmd(ser, cmd, timeout=10, marker="__DONE__"):
    """Send one command to the serial console; return full output (incl. echo)."""
    ser.write(f"{cmd}; echo {marker}; echo $?\n".encode())
    ser.flush()
    return _serial_read_until(ser, marker.encode(), timeout)


def _serial_ensure_root(ser):
    """Ensure the serial console is at a root shell (su if needed)."""
    out = _serial_cmd(ser, "id", marker="__ID__", timeout=8)
    if "uid=0" in out:
        return True
    ser.write(b"su\n")
    ser.flush()
    time.sleep(0.6)
    out = _serial_cmd(ser, "id", marker="__ID__", timeout=8)
    return "uid=0" in out


def enable_battery_polling(port, baud=SERIAL_BAUD):
    """Enable health-service polling via the serial console.

    adb cannot run this command (SELinux blocks the health ShellCommand with
    "Failed transaction 2147483646"); only a root shell on the device serial
    console can. Returns True when `get polling` reports 1 (i.e. dumpsys
    battery readings are live). Prints status/warnings to stdout.
    """
    if not port:
        print("[batt] warning: no serial port configured - dumpsys battery may "
              "show cached values (flat level curve)")
        return False
    try:
        import serial
    except Exception as e:
        print(f"[batt] warning: pyserial unavailable ({e}) - "
              "cannot enable polling")
        return False
    try:
        with serial.Serial(port, baud, timeout=0.2) as ser:
            ser.reset_input_buffer()
            ser.write(b"\n")   # bring the line editor back to a fresh prompt
            ser.flush()
            time.sleep(0.3)
            if not _serial_ensure_root(ser):
                print(f"[batt] warning: {port} did not reach a root shell - "
                      "polling not enabled")
                return False
            _serial_cmd(ser, POLLING_CMD_SET, marker="__SET__", timeout=10)
            out = _serial_cmd(ser, POLLING_CMD_GET, marker="__GET__", timeout=10)
            m = re.search(r"Polling:\s*(\d)", out)
            polling = int(m.group(1)) if m else None
            if polling == 1:
                print(f"[batt] health polling enabled via {port} (Polling=1) - "
                      "dumpsys battery is live")
                return True
            print(f"[batt] warning: polling not enabled (Polling={polling}) "
                  f"raw={out.strip()[-120:]!r}")
            return False
    except Exception as e:
        print(f"[batt] warning: serial open failed: {e} (is {port} connected "
              f"and its terminal app closed?)")
        return False


# ---------------------------------------------------------------------------
# Jump / temperature detection
# ---------------------------------------------------------------------------
def detect_jump(status, last_level, level):
    """Direction-aware level jump detection. Returns (jump_delta, jump_type)."""
    diff = level - last_level
    jt = ""
    if status in ("charging", "full"):
        if diff < 0:
            jt = "abnormal_drop"
        elif diff >= JUMP_THRESHOLD:
            jt = "abnormal_rise"
    elif status == "discharging":
        if diff > 0:
            jt = "abnormal_rise"
        elif diff <= -JUMP_THRESHOLD:
            jt = "abnormal_drop"
    else:
        if abs(diff) >= JUMP_THRESHOLD:
            jt = "abnormal_jump"
    if jt:
        return diff, jt
    return 0, ""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _summary(values):
    if not values:
        return {}
    vals = sorted(values)
    n = len(vals)

    def pct(p):
        return vals[max(1, math.ceil(p / 100.0 * n)) - 1]
    return {"min": round(min(values), 1),
            "avg": round(sum(values) / len(values), 1),
            "max": round(max(values), 1),
            "p50": round(pct(50), 1), "p90": round(pct(90), 1),
            "p95": round(pct(95), 1)}


def _stat_text(stat, unit):
    """One _summary() result as a single line for the HTML report.

    The engine renders a row value as one escaped string, so min/avg/max/p95
    have to be folded here. An empty summary (no valid sample for that metric)
    says so instead of rendering as an empty cell. Only the returned string is
    Chinese - it is data for a human reader, same as the PARAMS labels.
    """
    if not stat:
        return "无有效样本"
    return (f"{stat['min']} / {stat['avg']} / {stat['max']} "
            f"(p95 {stat['p95']}) {unit}")


def save_report(device, cfg, sources, stopped_early, samples, stop_reason):
    """Write the JSON report under reports/stress-test/battery/. Returns path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test", "battery")
    os.makedirs(report_dir, exist_ok=True)
    jump_counts = {}
    for s in samples:
        jt = s.get("jump_type")
        if jt:
            jump_counts[jt] = jump_counts.get(jt, 0) + 1
    data = {
        "test_name": "Battery charge/discharge stress",
        "device_id": device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg,
        "sources": sources,
        "stopped_early": bool(stopped_early),
        "sample_count": len(samples),
        "summary": {
            "level": _summary([s["level"] for s in samples
                               if s.get("level") is not None]),
            "temp": _summary([s["temp"] for s in samples
                              if s.get("temp") is not None]),
            "voltage_mv": _summary([s["voltage_mv"] for s in samples
                                    if s.get("voltage_mv") is not None]),
            "jump_counts": jump_counts,
            "stop_reason": stop_reason,
        },
        "samples": samples,
    }
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir, f"battery_{dev_short}_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return json_path


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------
def run_probe(serial):
    """Standalone: device reachable + battery read + serial port list."""
    if not check_adb_connected(serial):
        print("[probe] device not reachable via adb")
        return 1
    print(f"[probe] device serial   = {serial}")
    for key in ("ro.soc.manufacturer", "ro.soc.model", "ro.hardware"):
        print(f"[probe] {key:<19} = {getprop(serial, key) or '(empty)'}")
    info = get_battery_info(serial)
    if "error" in info:
        print(f"[probe] battery read   : FAIL ({info['error']})")
    else:
        print(f"[probe] battery read   : OK level={info['level']}% "
              f"status={info['status']} temp={info['temperature']}C "
              f"voltage={info['voltage_mv']}mV")
    print("--- serial ports ---")
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            print("[probe] no COM ports detected")
        for p in ports:
            print(f"[probe] {p.device}  {p.description or ''}  ({p.hwid or ''})")
    except Exception as e:
        print(f"[probe] pyserial unavailable: {e}")
    return 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> int:
    # --dump-params is consumed by the PPTP platform to render the params
    # config modal. Must short-circuit before argparse (and before any device
    # interaction). The server decodes stdout as UTF-8, but this script keeps
    # every literal ASCII (project rule: no Chinese in code files).
    if "--dump-params" in sys.argv:
        print(json.dumps({"fields": PARAMS}))
        return 0

    defaults = {f["name"]: f["default"] for f in PARAMS}

    p = argparse.ArgumentParser(
        description="Battery charge/discharge stress monitor "
                    "(level/temp/voltage)")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"mode"?, "serial_port"?, "interval_sec"?, '
                        '"temp_warn_c"?, "full_hold_sec"?}')
    p.add_argument("--probe", action="store_true",
                   help="test battery read + list serial ports then exit")
    args = p.parse_args()

    if args.probe:
        return run_probe(args.device)

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    mode = params.get("mode", defaults["mode"])
    if mode not in ("charge", "discharge"):
        mode = defaults["mode"]
    serial_port = str(params.get("serial_port", defaults["serial_port"]))
    interval_sec = float(params.get("interval_sec", defaults["interval_sec"]))
    temp_warn_c = float(params.get("temp_warn_c", defaults["temp_warn_c"]))
    full_hold_sec = int(params.get("full_hold_sec", defaults["full_hold_sec"]))
    if interval_sec < 2.0:
        interval_sec = 2.0

    print(f"[config] device        = {args.device}")
    print(f"[config] mode          = {mode}")
    print(f"[config] serial_port   = {serial_port or '(none)'}")
    print(f"[config] interval_sec  = {interval_sec}")
    print(f"[config] temp_warn_c   = {temp_warn_c}")
    print(f"[config] full_hold_sec = {full_hold_sec}")

    if not check_adb_connected(args.device):
        print("[error] device not reachable via adb")
        return 1

    soc_model = getprop(args.device, "ro.soc.model") or "unknown"

    # Enable health polling so dumpsys battery reads are live. If this fails
    # (no port / port busy) the run continues with cached readings + warnings.
    print(f"[batt] enabling health polling via "
          f"{serial_port or '(no port configured)'} ...")
    enable_battery_polling(serial_port)
    last_poll_t = time.monotonic()

    sources = {"level": "dumpsys battery", "temp": "dumpsys battery",
               "voltage_mv": "dumpsys battery", "status": "dumpsys battery",
               "com_port": serial_port or "(none)", "mode": mode}
    print("PERF|" + json.dumps({"type": "meta", "sources": sources,
                                "device": soc_model}, ensure_ascii=True))

    start = time.monotonic()
    samples = []
    stopped = False
    stop_reason = "manual"
    last_level = None
    full_since = None
    temp_warned = False
    consec_errors = 0

    try:
        while True:
            t_elapsed = time.monotonic() - start

            # Re-arm polling periodically (device / health restarts reset it).
            if SERIAL_RE_ENABLE_MIN > 0 and \
               time.monotonic() - last_poll_t >= SERIAL_RE_ENABLE_MIN * 60:
                if enable_battery_polling(serial_port):
                    last_poll_t = time.monotonic()

            info = get_battery_info(args.device)
            if "error" in info:
                consec_errors += 1
                print(f"[batt] warning: {info['error']} "
                      f"(consecutive {consec_errors}/2)")
                if consec_errors >= 2:
                    stop_reason = "power_off"
                    print("[batt] device unreachable twice - assuming power off, "
                          "stopping")
                    break
                time.sleep(interval_sec)
                continue
            consec_errors = 0

            t_elapsed = time.monotonic() - start
            level = info["level"]
            status = info["status"]
            temp = info["temperature"]

            # Jump detection (direction follows the ACTUAL status).
            jump = 0
            jump_type = ""
            if last_level is not None:
                jump, jump_type = detect_jump(status, last_level, level)
                if jump_type:
                    print(f"[batt] warning: level jump [{jump_type}] "
                          f"{last_level}% -> {level}% (d{jump:+d}%)")
            last_level = level

            # Temperature warning (re-arms when the battery cools back down).
            if not temp_warned and temp >= temp_warn_c:
                temp_warned = True
                print(f"[batt] warning: temperature {temp}C >= "
                      f"{temp_warn_c}C (threshold)")
            elif temp_warned and temp < temp_warn_c - 1.0:
                temp_warned = False

            emit = {"type": "sample", "clock": int(time.time() * 1000),
                    "t": round(t_elapsed, 1), "level": level,
                    "temp": temp, "voltage_mv": info["voltage_mv"],
                    "status": status, "jump": jump, "jump_type": jump_type}
            print("PERF|" + json.dumps(emit, ensure_ascii=True))
            samples.append(emit)

            # Stop: 100% stable in charge mode.
            if mode == "charge" and level >= 100:
                if status == "full":
                    stop_reason = "full"
                    print("[batt] battery full (100%, status=full) - stopping")
                    break
                if full_hold_sec > 0:
                    if full_since is None:
                        full_since = time.monotonic()
                        print(f"[batt] level 100% reached - holding "
                              f"{full_hold_sec}s before stop ...")
                    elif time.monotonic() - full_since >= full_hold_sec:
                        stop_reason = "full"
                        print(f"[batt] held 100% for {full_hold_sec}s - stopping")
                        break
                else:
                    stop_reason = "full"
                    print("[batt] level 100% reached - stopping")
                    break
            else:
                full_since = None

            elapsed = time.monotonic() - start
            remaining = interval_sec - (elapsed - t_elapsed)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        stopped = True
        stop_reason = "manual"
        print("\n[batt] interrupted - saving report")

    cfg = {"mode": mode, "serial_port": serial_port,
           "interval_sec": interval_sec, "temp_warn_c": temp_warn_c,
           "full_hold_sec": full_hold_sec}
    print("\n=== battery summary ===")
    if stopped:
        print(f"  stopped early after {len(samples)} samples")
    else:
        print(f"  completed {len(samples)} samples "
              f"({time.monotonic() - start:.1f}s)")
    print(f"  stop reason : {stop_reason}")
    for key, label in (("level", "LEVEL"), ("temp", "TEMP"),
                       ("voltage_mv", "VOLT")):
        s = _summary([x[key] for x in samples if x.get(key) is not None])
        if s:
            unit = "%" if key == "level" else ("C" if key == "temp" else "mV")
            print(f"  {label:<6} min={s['min']} avg={s['avg']} max={s['max']} "
                  f"p95={s['p95']} ({unit})")
        else:
            print(f"  {label:<6} no valid samples")
    jump_counts = {}
    for x in samples:
        jt = x.get("jump_type")
        if jt:
            jump_counts[jt] = jump_counts.get(jt, 0) + 1
    if jump_counts:
        for jt, c in jump_counts.items():
            print(f"  jump [{jt}] x{c}")
    else:
        print("  jumps       : none")
    # The Chinese HTML twin of the report above, for reading afterwards. Built
    # from the same local values as the block just printed, so the two cannot
    # disagree. This script has NO pass/fail semantics - it samples until a stop
    # condition fires and always exits 0 - so `result` stays None and the page
    # shows the neutral "no verdict" banner rather than a verdict this run never
    # produced. Nothing in this report is sensitive (no credentials, unlike the
    # wifi scripts), so no hide_keys is needed.
    level_stat = _summary([x["level"] for x in samples
                           if x.get("level") is not None])
    temp_stat = _summary([x["temp"] for x in samples
                          if x.get("temp") is not None])
    volt_stat = _summary([x["voltage_mv"] for x in samples
                          if x.get("voltage_mv") is not None])
    stop_reason_zh = {"manual": "手动停止(平台停止按钮 / Ctrl+C)",
                      "full": "充电完成(电量 100% 且满足满电判定)",
                      "power_off": "设备连续两次读取失败(疑似断电 / 掉线)"}.get(
        stop_reason, stop_reason)
    temp_note = f"告警阈值 {temp_warn_c}C"
    if temp_stat and temp_stat["max"] >= temp_warn_c:
        temp_note += ",本次有样本达到阈值,脚本已告警"
    jump_text = ("无" if not jump_counts else
                 "、".join(f"{jt} x{c}"
                          for jt, c in sorted(jump_counts.items())))
    # Same shape as the dict save_report() writes, so the page's raw-data
    # section matches the JSON report file (test_time is shown in the header).
    report_detail = {
        "test_name": "Battery charge/discharge stress",
        "device_id": args.device,
        "config": cfg,
        "sources": sources,
        "stopped_early": bool(stopped),
        "sample_count": len(samples),
        "summary": {"level": level_stat, "temp": temp_stat,
                    "voltage_mv": volt_stat, "jump_counts": jump_counts,
                    "stop_reason": stop_reason},
        "samples": samples,
    }
    doc = _pptp_report.build_payload(
        script="battery_inout_stress.py",
        script_version=SCRIPT_VERSION,
        test_name="电池充放电压力测试",
        device_id=args.device,
        result=None,
        level="info",
        count_zh=f"本次采集 {len(samples)} 个样本",
        warn_zh=("设备连续两次读取电池失败(疑似断电 / ADB 掉线),"
                 "本次压测提前结束" if stop_reason == "power_off" else ""),
        rows=[
            _pptp_report.row("samples", "采样点数", len(samples), "info",
                             "手动停止,未跑满计划时长" if stopped
                             else "由停止条件结束"),
            _pptp_report.row("stop_reason", "停止原因", stop_reason, "info",
                             stop_reason_zh),
            _pptp_report.row("level", "电量 LEVEL", _stat_text(level_stat, "%"),
                             "info", "min / avg / max / p95,单位 %"),
            _pptp_report.row("temp", "温度 TEMP", _stat_text(temp_stat, "C"),
                             "info", temp_note),
            _pptp_report.row("voltage", "电压 VOLT",
                             _stat_text(volt_stat, "mV"), "info",
                             "min / avg / max / p95,单位 mV"),
            _pptp_report.row("jumps", "电量跳变", jump_text, "info",
                             "脚本按实际 status 判定方向,"
                             "异常跳变已逐条打印告警"),
        ],
        params_schema=_params(),
        params_values={"mode": mode, "serial_port": serial_port,
                       "interval_sec": interval_sec, "temp_warn_c": temp_warn_c,
                       "full_hold_sec": full_hold_sec},
        detail=report_detail,
    )

    try:
        report_path = save_report(args.device, cfg, sources, stopped, samples,
                                  stop_reason)
        html_path = _pptp_report.write_html_report(report_path, doc)
        if html_path:
            print(f"  html        = {html_path}")
        print(f"  report      : {report_path}")
    except Exception as e:
        print(f"[warn] failed to save report: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
