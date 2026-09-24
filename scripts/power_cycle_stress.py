"""
power_cycle_stress.py - PPTP: hard power-cycle stress against an EXTERNAL relay.

The device is powered through a relay that has its own timer. This script NEVER
controls that power and NEVER assumes a cycle rhythm: it only OBSERVES the
device leaving adb (the power cut) and coming back (power restored). The
observed on/off timing is therefore REPORTED as a measurement rather than
compared against an expectation - the relay's rhythm is not fixed, and a script
that asserted one would start reporting false failures the day it changed.

Per cycle:
  1. Wait for the device to leave `adb devices`          (the power cut)
  2. Wait for it to come back                            (the power restore)
  3. Wait for `sys.boot_completed` == 1                  (timeout here = the verdict)
  4. Wait `post_boot_wait_sec` (init says "booted" before the UI flow is done)
  5. Read the Wi-Fi state                                (RECORDED, never judged)
  6. Run the post-boot IR sequence once, if one is selected

PRECONDITION: the device's power-on mode must be set to Direct (auto power-on).
Without it nothing boots after a power cut and every cycle fails. The platform
shows that as a hint pinned at the top of this script's params modal (D-70).

Contract (matches other PPTP scripts):
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional; see PARAMS below)
  --selftest            (standalone: assert the pure functions, then exit)

Run standalone:
    python scripts/power_cycle_stress.py --device <serial>
    python scripts/power_cycle_stress.py --device <serial> \
        --params '{"iterations": 2, "post_boot_wait_sec": 25}'   # short smoke run

IR notes (the in-process pattern approved in D-43):
  - ir_runner is imported IN-PROCESS, never spawned as a child. The platform's
    force-stop path is proc.kill() (TerminateProcess), which sends no console
    control event at all, so a child would survive the stop and keep pressing
    keys on the device until the server next restarted and reaped it.
  - run_loop is not used: it loops forever, and its only exit is a
    KeyboardInterrupt raised in its own thread - which Windows never delivers,
    because it sends console control events to the main thread only. Steps are
    driven one press at a time (`count=1` copies) so a stop is immediate;
    run_step's own repeat loop cannot be interrupted.
  - The .ini's `delay_ms` is the gap AFTER each press and is paced by this file,
    not by run_step (a count=1 copy never sleeps). That is what lets a
    multi-key navigation sequence express "press, wait, press" - the same
    reading perf_monitor's KeyInjector gives the field.
  - ONE PASS per cycle, never a loop. `count` is still honoured (a step with
    count=5 presses five times), but the steps are walked exactly once and the
    sequence ends. There is no repeat-forever mode here, unlike perf_monitor's
    keepalive. What that means in practice: `delay_ms` becomes a wait INSIDE the
    cycle, so reusing an idle-keepalive sequence (whose delay_ms is the repeat
    interval - 20 minutes, say) would make this script sit for 20 minutes in the
    middle of a cycle. Write a dedicated post-boot sequence with short delays.
  - Dispatching (Short/Long, sendevent vs `input keyevent`) stays entirely
    run_step's business: `KEY_*` -> sendevent (needs userdebug/root),
    `KEYCODE_*` -> input keyevent (works on a user build).
  - On this device (Android 14 / SDK 34) `KEYCODE_*` with a LongXXXX press is
    SILENTLY non-functional - only `KEY_*` long press actually injects. A
    sequence relying on it reports "sent" while nothing happens on screen, and
    this script cannot tell.

What this script CANNOT tell you:
  - whether the IR sequence did what you meant (did YouTube actually open and
    start playing). It reports how many presses were handED to adb, never what
    appeared on screen.
  - the relay's own off window. The "off duration" is measured as "left adb ->
    back in adb", which includes adbd coming back up, so it is an UPPER bound on
    that window rather than the window itself.
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
# platform and for a plain `python scripts/power_cycle_stress.py`. See
# docs/REPORT_FORMAT.md.
import _pptp_report

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a report.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

SCRIPT_VERSION = "1.0.0"

# reports/stress-test/<module>/ - must match ARCHIVE_MODULES in server.py, which
# groups the archive tree the same way.
REPORT_MODULE = "power"

# --- constants that are not user decisions ---------------------------------
# Consecutive sightings of "not in adb" before a power cut is believed. One
# failed/empty `adb devices` read must not be mistaken for a power cut: that
# would fake a cut AND a return, and the cycle would then fire the post-boot IR
# sequence at a device that never went away - a wrong action on the device, not
# just a wrong number in a report.
OFF_CONFIRM_POLLS = 2
PROBE_TIMEOUT_SEC = 5
# A power cut is the relay's business and has no upper bound, so the wait for it
# is unbounded; this only prints proof of life every so often (stdout events are
# allowed - this is not a banner/toast/status bar, see D-09/D-36).
HEARTBEAT_SEC = 300
# The device-side read, wrapped, so a `cmd wifi status` stuck in D state cannot
# hang the probe. Same guard and same reason as perf_monitor's WIFI_CMD.
WIFI_GUARD_S = 2
WIFI_CMD = f"timeout -k 1 {WIFI_GUARD_S} cmd wifi status"
ABSENT_MARKERS = ("no such file", "not found", "permission denied",
                  "operation not permitted", "no such device")
# Every state that means "the link is not usable". `unknown` counts as DOWN on
# purpose: a read that succeeded but cannot be classified must never be reported
# as a healthy link.
WIFI_STATES_DOWN = ("off", "noassoc", "noip", "unknown")
# State words -> Chinese, same vocabulary as perf_monitor's WIFI_ZH. Data for a
# human-facing surface, never printed to stdout.
WIFI_ZH = {"ok": "已连接", "noip": "已关联但无 IP", "noassoc": "未关联",
           "off": "WiFi 已关闭", "unknown": "无法判定"}


def _ir_ini_choices(root: str | None = None) -> list:
    """The .ini files the post-boot action may run, as {value,label} choices.

    A fixed list rather than free text, for the same reason as perf_monitor's
    _key_ini_choices: a mistyped path would silently send nothing at all, and the
    run would look like it worked. Transient _seq_*.ini files are skipped -
    ir_runner writes those only while running with a `sequence_content` param, so
    they are never a real choice. Non-recursive on purpose, and the empty first
    entry means "do not press anything": the default must never be a sequence
    that pokes the device.

    `root` is injectable so --selftest can drive this against a synthetic
    directory instead of the repo's own ir_sequences/ (which the user edits).
    """
    choices = [{"value": "", "label": "(不发送按键)"}]
    d = os.path.join(root or PROJECT_ROOT, "ir_sequences")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.endswith(".ini") and not name.startswith("_"):
                choices.append({"value": f"ir_sequences/{name}",
                                "label": name})
    return choices


IR_SEQUENCE_CHOICES = _ir_ini_choices()

# The power-on mode the whole flow depends on. Named once here: it is both the
# modal hint and the recorded test condition in the report, and two hand-kept
# copies of that string would drift. The ID is the ASCII half, printed to stdout
# (D-07: stdout is ASCII/English); the Chinese half is report and UI text.
REQUIRED_POWER_MODE = "Direct(上电自动开机)"
REQUIRED_POWER_MODE_ID = "Direct"
PARAMS_HINT = (f"需将设备上电方式改为 {REQUIRED_POWER_MODE},否则断电后不会自动开机,"
               f"本流程跑不起来。本脚本只观察外部继电器造成的断电/上电,不控制电源。")

# Frontend-configurable params (declared for the PPTP platform).
# Field keys: name / label / type / default / min / max / choices.
# Run with `--dump-params` to print this schema (plus `hint`) as JSON.
PARAMS = [
    {"name": "iterations", "label": "循环次数(一轮 = 断电→上电→开机后动作)",
     "type": "int", "default": 1000, "min": 1, "max": 100000},
    {"name": "post_boot_wait_sec", "label": "识别到开机完成后的等待(秒)",
     "type": "int", "default": 25, "min": 0, "max": 3600},
    {"name": "ir_sequence", "label": "开机后执行的红外序列(空 = 不发送按键)",
     "type": "select", "choices": IR_SEQUENCE_CHOICES, "default": ""},
    {"name": "boot_timeout_sec", "label": "开机完成超时(秒)",
     "type": "int", "default": 120, "min": 10, "max": 3600},
    {"name": "off_watchdog_sec", "label": "断电看门狗(秒,非节奏断言)",
     "type": "int", "default": 1800, "min": 60, "max": 86400},
    {"name": "poll_interval_sec", "label": "嗅探间隔(秒,也是时长测量分辨率)",
     "type": "int", "default": 2, "min": 1, "max": 60},
]


def poll_note(poll_sec: float) -> str:
    """The single statement of this script's resolution limit.

    The number is DERIVED from the poll interval, never restated by hand, so
    retuning the param retunes the caveat with it (the discipline D-54 sets for
    the WiFi sampling period).
    """
    return (f"Times are observed by polling every {poll_sec:g}s: an outage "
            f"shorter than that can fall between two polls and never be seen, "
            f"and each measured endpoint carries up to +/-{poll_sec:g}s of "
            f"uncertainty. Seeing an outage means it happened; NOT seeing one "
            f"does not mean it did not.")


def poll_note_zh(poll_sec: float) -> str:
    return (f"所有时长都是每 {poll_sec:g} 秒嗅探一次得来的:短于一个嗅探间隔的断电"
            f"可能整段落在两次嗅探之间而完全看不见,且每个端点最多有 ±{poll_sec:g} 秒的"
            f"不确定度。看到一次断电 = 真的断过;没看到 = 不能反推没断过。")


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


def parse_adb_devices(text: str) -> dict[str, str] | None:
    """`adb devices` output -> {serial: state}. None means the READ FAILED.

    None is NOT "no devices": a failed read must never be turned into a power
    cut, the same rule parse_wifi follows by returning None for a read failure.
    Callers keep their current state on None.

    The states that matter here are `device` (usable), `offline` (a stale row
    adb kept after the transport dropped - D-08's reconnect verb is what clears
    it) and `unauthorized` (the device re-enumerated and is asking again).
    """
    if "List of devices attached" not in text:
        return None
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def adb_devices(timeout: int = PROBE_TIMEOUT_SEC) -> dict[str, str] | None:
    """One `adb devices` (no -s: a single call covers every transport)."""
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None
    except (FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return parse_adb_devices(r.stdout or "")


def adb_reconnect_offline() -> int:
    """`adb reconnect offline`, best effort, never raises (D-08's verb).

    A power cut leaves adb holding a stale `offline` row for the serial, and
    while that row is there the device can come back powered up and still never
    read as `device`. Clearing it costs one call and is a no-op when there is
    nothing stale.
    """
    try:
        r = subprocess.run(["adb", "reconnect", "offline"],
                           capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
        return r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return -1


def wait_boot_completed(serial: str, timeout_sec: int,
                        poll_sec: int) -> tuple[bool, float]:
    """Poll `sys.boot_completed` until 1. Returns (ok, elapsed_seconds).

    This is the ONE timeout this script owns: the device is already back in adb,
    so its power is provably on and a boot that never finishes is a real fault.
    The wait for the device to return is a different story - see the watchdog.

    A power cut landing DURING this wait shows up as a boot timeout, because
    boot_completed then never arrives. That is honest (the device did not finish
    booting) but it is not the same fault as a dead device.
    """
    t0 = time.time()
    next_hb = t0 + HEARTBEAT_SEC
    while True:
        rc, out, _ = adb_shell(serial, "getprop", "sys.boot_completed",
                               timeout=PROBE_TIMEOUT_SEC)
        if rc == 0 and out.strip() == "1":
            return True, time.time() - t0
        elapsed = time.time() - t0
        if elapsed >= timeout_sec:
            return False, elapsed
        now = time.time()
        if now >= next_hb:
            print(f"[boot] still waiting for sys.boot_completed "
                  f"({int(elapsed)}s / {timeout_sec}s)")
            next_hb = now + HEARTBEAT_SEC
        time.sleep(poll_sec)


def _wifi_clean_ssid(v: str | None) -> str | None:
    """An SSID we can NAME, or None. `<unknown ssid>` is Android's placeholder
    for "the SSID is not visible to this caller" - it is not a connection."""
    if not v:
        return None
    s = v.strip().strip('"').strip()
    if not s or s.startswith("<") or s.lower() in ("unknown", "0x", "null"):
        return None
    return s


def _wifi_info_fields(line: str) -> dict[str, str]:
    """`WifiInfo: A: 1, B: 2, ...` -> {A: 1, B: 2}.

    Keyed rather than fished with a regex because the payload contains colons
    (MAC/BSSID) and because `Link speed` has `Tx Link speed` / `Rx Link speed`
    siblings that a substring search would happily match instead.
    """
    body = line.split(":", 1)[1] if ":" in line else ""
    out: dict[str, str] = {}
    for part in body.split(", "):
        if ": " in part:
            k, v = part.split(": ", 1)
            out.setdefault(k.strip(), v.strip())
    return out


def parse_wifi(text: str) -> dict | None:
    """`cmd wifi status` -> one state token plus the fields the report quotes.

    VERBATIM COPY of perf_monitor.parse_wifi. Do not "improve" it here: two
    copies of this parser that drift apart would make the platform's WiFi column
    and this script's WiFi column start describing the same device differently.
    perf_monitor is a script, not a library, so it cannot be imported (the same
    reason _ir_ini_choices above is a copy). See docs/PITFALLS.md #49.

    Returns None when the READ failed (empty output, or a shell error marker). A
    link that is DOWN is not a failed read and must never be reported as one.

    ANCHORING IS THE WHOLE POINT. `cmd wifi status` prints SSID and IP TWICE:
    once on the `WifiInfo:` line and again inside the `NetworkCapabilities:` ->
    `TransportInfo:` blob. A bare `re.search(r"IP: (\\S+)")` over the whole text
    therefore reads a DIFFERENT field than intended, and one that can disagree:
    on the reference device `WifiInfo` reported `IP: null` while `TransportInfo`
    reported `IP: /172.16.0.189`. That is the exact output pair that motivated
    this parser.
    """
    if not text or not text.strip():
        return None
    low = text.lower()
    if any(k in low for k in ABSENT_MARKERS):
        return None

    # "Wifi is enabled" / "Wifi is disabled"; both spellings are accepted so a
    # wording change on another Android version degrades to `unknown`, never to
    # a fabricated outage.
    enabled = "wifi is enabled" in low
    disabled = "wifi is disabled" in low or "wifi is not enabled" in low

    fields: dict[str, str] = {}
    seen_info = False
    assoc_ssid: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("WifiInfo:"):
            if not seen_info:              # FIRST WifiInfo line wins, always
                fields = _wifi_info_fields(line)
                seen_info = True
            continue
        if assoc_ssid is None:
            m = re.match(r'Wifi is connected to "([^"]*)"', line)
            if m:
                assoc_ssid = _wifi_clean_ssid(m.group(1))

    ssid = _wifi_clean_ssid(fields.get("SSID")) or assoc_ssid

    ip: str | None = None
    ip_raw = (fields.get("IP") or "").strip().lstrip("/").strip()
    if ip_raw and ip_raw.lower() not in ("null", "0.0.0.0", "unknown", "none"):
        ip = ip_raw

    rssi = None
    try:
        rssi = int((fields.get("RSSI") or "").strip())
    except ValueError:
        pass
    link_mbps = None
    try:
        link_mbps = float((fields.get("Link speed") or "").strip()
                          .replace("Mbps", "").strip())
    except ValueError:
        pass

    if disabled and not enabled:
        state = "off"
    elif seen_info:
        # Associated-but-no-IP is a real, OBSERVED state on the reference device
        # and it is the state in which a video client reports a connection error.
        state = "noip" if ip is None else "ok"
    elif assoc_ssid:
        # Associated, but this build printed no WifiInfo block. The IP is
        # unknown - and a missing record is NOT evidence of an outage.
        state = "ok"
    elif enabled:
        state = "noassoc"
    else:
        state = "unknown"

    return {"state": state, "ssid": ssid, "ip": ip, "rssi": rssi,
            "link_mbps": link_mbps,
            "supplicant": (fields.get("Supplicant field") or None)}


def read_wifi(serial: str) -> dict | None:
    """One Wi-Fi reading. None = the read failed (NOT the same as an outage)."""
    rc, out, _ = adb_shell(serial, WIFI_CMD, timeout=10)
    if rc != 0 or not out:
        return None
    return parse_wifi(out)


def observed_off_sec(t_off: float, t_return: float,
                     unknown: bool) -> float | None:
    """The observed outage in seconds, or None when it cannot be measured.

    `unknown` is set when the device was ALREADY offline as the cycle began: the
    power cut happened before this script looked, so there is no start to measure
    from. That reports None - never 0 - because a 0 reads as "the device never
    lost power", which is the opposite of what was observed. An unmeasured value
    stays unmeasured (D-71 decision 2).

    Pure, so --selftest drives THIS rather than a copy of the rule.
    """
    if unknown:
        return None
    return round(max(0.0, t_return - t_off), 1)


def verdict(cycles: list, interrupted: bool) -> dict:
    """Aggregate a run into its verdict. Pure, so --selftest drives THIS.

    THE RULE (D-71 decision 3): the only two things that can fail a run are the
    two timeouts - `sys.boot_completed` never arriving, and the device never
    coming back to adb. The Wi-Fi reading is a fact about the conditions the run
    happened under, never a verdict: a link that is down may be the device under
    test or the access point, and letting it fail a cycle would stake the verdict
    on the bench's Wi-Fi. (perf_monitor reaches the same conclusion from the
    other side - its wifi cap can only ever downgrade OK to WARN, never fail a
    run that was otherwise healthy.)

    A manual stop is not a failure: the completed cycles are judged on their own
    merits, and the report says how many of the planned cycles they cover.
    """
    total = len(cycles)
    passed = sum(1 for c in cycles if c.get("status") == "PASS")
    judged = total > 0
    all_passed = judged and passed == total
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "boot_failed": sum(1 for c in cycles
                           if c.get("reason") == "boot_timeout"),
        "return_failed": sum(1 for c in cycles
                             if c.get("reason") == "return_timeout"),
        "rate": (passed / total * 100) if total else 0.0,
        "judged": judged,
        "all_passed": all_passed,
        # A manual stop always exits 0: the platform's stop button is a normal
        # end to a run, not a failure (the same rule the sibling scripts use).
        "exit_code": 0 if interrupted else (0 if all_passed else 1),
    }


def run_selftest() -> int:
    """Assert what can be asserted without a device or a relay, then exit.

    Only pure functions and the params contract are covered. EVERYTHING that
    makes this script useful - the sniff loop, adb re-enumeration, the real
    boot_completed timeline, run_step's on-device dispatch - needs the hardware
    and is NOT covered here. A green run here means "the logic is self-consistent
    and the contract is intact", never "the test works".
    """
    fails: list[str] = []

    def check(name, cond, detail=""):
        print(f"[selftest] {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  ({detail})" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    # --- the adb devices parser --------------------------------------------
    devs = parse_adb_devices(
        "List of devices attached\n"
        "435E54A1BB0D08004C10\tdevice\n"
        "emulator-5554\tdevice\n"
        "* daemon started successfully\n"
        "192.168.1.9:5555\toffline\n"
        "ABC123\tunauthorized\n")
    check("adb devices: every serial parsed with its state",
          devs == {"435E54A1BB0D08004C10": "device", "emulator-5554": "device",
                   "192.168.1.9:5555": "offline", "ABC123": "unauthorized"},
          str(devs))
    check("adb devices: a stale offline row is NOT device",
          (devs or {}).get("192.168.1.9:5555") != "device")
    # The distinction the whole loop rests on: no devices is an EMPTY DICT (a
    # power cut), a failed read is None (we do not know), and only the latter
    # may never move the state machine.
    check("adb devices: header with nothing attached -> {} (a real outage)",
          parse_adb_devices("List of devices attached\n\n") == {})
    check("adb devices: unreadable output -> None (NOT an outage)",
          parse_adb_devices("") is None
          and parse_adb_devices("adb: device unauthorized.") is None
          and parse_adb_devices("error: no such file or directory") is None)

    # --- the ini choices ----------------------------------------------------
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "ir_sequences")
        os.makedirs(os.path.join(fake, "nested"))
        for name in ("b.ini", "a.ini", "_seq_tmp.ini", "readme.txt"):
            open(os.path.join(fake, name), "w").close()
        open(os.path.join(fake, "nested", "deep.ini"), "w").close()
        vals = [c["value"] for c in _ir_ini_choices(tmp)]
        check("ini choices: sorted, no _-prefixed, not recursive, empty-first",
              vals == ["", "ir_sequences/a.ini", "ir_sequences/b.ini"],
              str(vals))
    check("ini choices: a missing directory degrades to the empty entry only",
          [c["value"] for c in _ir_ini_choices(os.path.join(os.sep, "nope"))]
          == [""])
    # The repo's own directory, i.e. what the dropdown will actually show.
    real = _ir_ini_choices()
    check("ini choices: every non-empty choice exists on disk",
          real[0]["value"] == ""
          and all(os.path.isfile(os.path.join(PROJECT_ROOT, c["value"]))
                  for c in real[1:]), str(real[1:]))

    # --- the observed-outage rule ------------------------------------------
    check("off duration: measured when the cut was seen",
          observed_off_sec(100.0, 130.4, False) == 30.4)
    check("off duration: unmeasurable -> None, never 0",
          observed_off_sec(100.0, 130.4, True) is None)
    check("off duration: a backwards clock cannot produce a negative",
          observed_off_sec(130.0, 100.0, False) == 0.0)

    # --- the verdict --------------------------------------------------------
    ok_cycle = {"status": "PASS", "reason": "booted"}
    check("verdict: every cycle booted -> pass, exit 0",
          (lambda v: v["all_passed"] and v["exit_code"] == 0
           and v["judged"])(verdict([dict(ok_cycle)] * 3, False)))
    check("verdict: one boot timeout -> fail, exit 1",
          (lambda v: not v["all_passed"] and v["exit_code"] == 1
           and v["boot_failed"] == 1 and v["passed"] == 2)(
              verdict([dict(ok_cycle), dict(ok_cycle),
                       {"status": "FAIL", "reason": "boot_timeout"}], False)))
    check("verdict: a watchdog stop -> fail, exit 1",
          (lambda v: not v["all_passed"] and v["exit_code"] == 1
           and v["return_failed"] == 1)(
              verdict([{"status": "FAIL", "reason": "return_timeout"}], False)))
    check("verdict: a manual stop is not a failure (exit 0)",
          (lambda v: v["all_passed"] and v["exit_code"] == 0)(
              verdict([dict(ok_cycle), dict(ok_cycle)], True)))
    check("verdict: no cycle -> nothing to judge (exit 1, not a silent pass)",
          (lambda v: not v["judged"] and v["exit_code"] == 1)(
              verdict([], False)))

    # THE rule of this script: Wi-Fi is recorded, never judged, so a link that
    # was down every single cycle must still leave an all-booted run PASS.
    down = dict(ok_cycle)
    down["wifi"] = {"state": "noassoc", "ssid": None, "ip": None,
                    "rssi": None, "link_mbps": None, "supplicant": None}
    v = verdict([dict(down)] * 4, False)
    check("verdict: Wi-Fi down on every cycle -> still PASS",
          v["all_passed"] and v["exit_code"] == 0 and v["failed"] == 0, str(v))
    v = verdict([dict(down)] * 3 + [{"status": "FAIL",
                                     "reason": "boot_timeout"}], False)
    check("verdict: Wi-Fi down does not change a real FAIL",
          not v["all_passed"] and v["exit_code"] == 1)

    # --- the WiFi parser (a verbatim copy - this proves it landed intact) ---
    check("wifi: disabled -> off",
          (parse_wifi("Wifi is disabled\n") or {}).get("state") == "off")
    check("wifi: enabled with no association -> noassoc",
          (parse_wifi("Wifi is enabled\n") or {}).get("state") == "noassoc")
    # PITFALLS #49: `cmd wifi status` prints IP twice, and on the reference
    # device the two DISAGREE. The first WifiInfo line must win.
    two_ips = ("Wifi is enabled\n"
               'WifiInfo: SSID: "MyNet", IP: /10.0.0.5, RSSI: -50, '
               "Link speed: 100Mbps\n"
               "NetworkCapabilities: TransportInfo: IP: /192.168.1.9\n")
    got = parse_wifi(two_ips) or {}
    check("wifi: WifiInfo's IP wins over TransportInfo's (PITFALLS #49)",
          got.get("ip") == "10.0.0.5" and got.get("state") == "ok", str(got))
    no_ip = parse_wifi('WifiInfo: SSID: "MyNet", IP: null, RSSI: -55\n') or {}
    check("wifi: associated but IP null -> noip (a real, seen state)",
          no_ip.get("state") == "noip" and no_ip.get("ip") is None, str(no_ip))
    check("wifi: a read failure -> None, and it is NOT an outage",
          parse_wifi("") is None
          and parse_wifi("cmd: not found") is None)

    # --- the params contract ------------------------------------------------
    line = json.dumps({"fields": PARAMS, "hint": PARAMS_HINT})
    check("params: the --dump-params line is a single ASCII line",
          "\n" not in line and line.isascii())
    check("params: hint is non-empty, names the precondition, stays ASCII-safe",
          isinstance(PARAMS_HINT, str) and "Direct" in PARAMS_HINT
          and json.loads(line)["hint"] == PARAMS_HINT)
    ir = [f for f in PARAMS if f["name"] == "ir_sequence"][0]
    check("params: the IR sequence defaults to doing nothing",
          ir["default"] == "" and ir["choices"][0]["value"] == "")
    check("params: every field name is unique and every value round-trips",
          len({f["name"] for f in PARAMS}) == len(PARAMS)
          and json.loads(json.dumps(
              {f["name"]: f["default"] for f in PARAMS}))
          == {f["name"]: f["default"] for f in PARAMS})
    # A source guard: the platform sniffs one literal substring in a script's
    # source to decide the script owns the serial port and to stand its own
    # capture down (D-06). This script has no such parameter, and a mention
    # anywhere - even in a comment - would silently take the port away from the
    # platform. The needle is assembled at runtime so that this guard's own
    # source cannot trip it.
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    check("source: never mentions the platform's serial-port ownership substring",
          "serial" + "_port" not in src)

    print(f"\n[selftest] {len(fails)} failure(s)")
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


def send_sequence_once(serial: str, ini_rel: str) -> tuple[str, int, int]:
    """Send ONE pass of an ir_sequences/*.ini. Returns (status, sent, failed).

    ONE pass, deliberately: this is a post-boot action, not a keepalive, so the
    steps are walked exactly once. `count` is honoured, the step list is not
    repeated, and `run_loop` is never involved.

    Never raises: a wrong path, a malformed file, an unknown key name and an
    unreachable device all land in `status`, exactly like perf_monitor's
    KeyInjector. The IR sequence is a MEASUREMENT CONDITION, not the object under
    test - it can never move the verdict (D-43).

    Each press is a `count=1` copy of its step, paced from here, because
    run_step's own repeat loop cannot be interrupted: a stop would let a burst
    press on to its end. Pacing after every press except the last is what makes
    the .ini's delay_ms the gap between presses - the same reading KeyInjector
    gives the field, and the only way a multi-key navigation sequence can say
    "press, wait, press".
    """
    if not ini_rel:
        return "skipped (no sequence selected)", 0, 0
    try:
        # Imported here, not at module level: a missing or broken ir_runner must
        # cost the IR step and nothing else - the sniffing loop still measures.
        import ir_runner
    except Exception as e:
        return f"failed: cannot import ir_runner: {type(e).__name__}: {e}", 0, 0

    try:
        path = ini_rel
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(PROJECT_ROOT, path))
        steps = list(ir_runner.SequenceConfig(path).steps)
        if not steps:
            return "failed: sequence is empty", 0, 0
        ir = ir_runner.IRRemote(event_path=ir_runner.resolve_event_path(None))
    except Exception as e:
        return f"failed: {type(e).__name__}: {e}", 0, 0

    total = sum(s.count for s in steps)
    sent = failed = 0
    done = 0
    for step in steps:
        one = ir_runner.SequenceStep(step.index, step.code, step.action,
                                     step.delay_ms, 1, step.long_duration_ms)
        for _ in range(step.count):
            done += 1
            try:
                ir_runner.run_step(ir, one, serial)
                sent += 1
            except Exception as e:
                failed += 1
                print(f"[key] press error: {type(e).__name__}: {e}")
            # A count=1 copy never sleeps, so the gap is paced here. Skipped
            # after the final press: nothing follows it to space out.
            if done < total:
                time.sleep(step.delay_ms / 1000.0)

    status = (f"sent ({sent} press(es))" if not failed
              else f"sent {sent}, failed {failed}")
    return status, sent, failed


def save_report(device: str, data: dict) -> str:
    """Write the JSON report under reports/stress-test/power/. Returns the path.

    The filename MUST contain the short device id (`dev_short`): server.py's
    fallback scan, used when the stdout sniffer missed the `report :` line,
    matches candidates on that substring. Without it a hard-killed run loses its
    report silently.
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test",
                              REPORT_MODULE)
    os.makedirs(report_dir, exist_ok=True)
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir, f"power_cycle_{dev_short}_{ts}.json")
    _pptp_report.atomic_write_json(json_path, data)
    return json_path


def main() -> int:
    # --dump-params is consumed by the PPTP platform to render the params config
    # modal. Must short-circuit before argparse (and before any device
    # interaction). `hint` is a top-level sibling of `fields`: script-declared
    # static precondition text the platform pins to the top of that modal (D-70).
    # json.dumps keeps stdout pure-ASCII for safe piping, Chinese included.
    if "--dump-params" in sys.argv:
        print(json.dumps({"fields": PARAMS, "hint": PARAMS_HINT}))
        return 0
    if "--selftest" in sys.argv:
        return run_selftest()

    defaults = {f["name"]: f["default"] for f in PARAMS}

    p = argparse.ArgumentParser(
        description="Hard power-cycle stress test (external relay, sniffed)")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"iterations"?, "post_boot_wait_sec"?, '
                        '"ir_sequence"?, "boot_timeout_sec"?, '
                        '"off_watchdog_sec"?, "poll_interval_sec"?}')
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    iterations = int(params.get("iterations", defaults["iterations"]))
    post_boot_wait_sec = int(params.get("post_boot_wait_sec",
                                       defaults["post_boot_wait_sec"]))
    boot_timeout_sec = int(params.get("boot_timeout_sec",
                                     defaults["boot_timeout_sec"]))
    off_watchdog_sec = int(params.get("off_watchdog_sec",
                                     defaults["off_watchdog_sec"]))
    poll_interval_sec = int(params.get("poll_interval_sec",
                                      defaults["poll_interval_sec"]))
    if poll_interval_sec < 1:
        poll_interval_sec = 1
    ir_sequence = str(params.get("ir_sequence",
                                defaults["ir_sequence"]) or "").strip()

    print(f"[config] device          = {args.device}")
    print(f"[config] iterations      = {iterations}")
    print(f"[config] post_boot_wait  = {post_boot_wait_sec}s")
    print(f"[config] ir_sequence     = {ir_sequence or '(none)'}")
    print(f"[config] boot_timeout    = {boot_timeout_sec}s "
          f"(device back in adb -> sys.boot_completed)")
    print(f"[config] off_watchdog    = {off_watchdog_sec}s "
          f"(device left adb -> back; a liveness bound, NOT a rhythm assertion)")
    print(f"[config] poll_interval   = {poll_interval_sec}s "
          f"(= the measurement resolution)")
    print(f"[config] power mode      = {REQUIRED_POWER_MODE_ID} (required: the "
          f"device must power on by itself, set in firmware)")
    print(f"[note] {poll_note(poll_interval_sec)}")
    if not ir_sequence:
        print("[key] disabled (no ir_sequence selected)")
    print("[note] the relay is autonomous: this script observes the power cut "
          "and restore, it does not control power")

    cycles: list[dict] = []
    interrupted = False
    stopped_early_reason = ""
    n_read_fail = 0

    # --- the sniff state machine -------------------------------------------
    # WAIT_OFF: the device is in adb; waiting for the power cut. Unbounded by
    #           design - the relay owns this and inventing a bound would invent
    #           an expectation.
    # WAIT_BACK: the cut was seen; waiting for the device to return to adb.
    #           Bounded by off_watchdog_sec, which is a liveness bound rather
    #           than a rhythm assertion (a relay off window may legitimately
    #           grow), so it must stay far larger than any plausible window.
    # WAIT_BOOT: the device is back in adb, so its power is on; waiting for
    #           sys.boot_completed within boot_timeout_sec. THIS is the verdict.
    # POST_BOOT: one-shot - wait, read Wi-Fi, run the sequence.
    phase = "WAIT_OFF"
    absent = 0
    saw_present = False
    t_phase = time.time()
    next_hb = t_phase + HEARTBEAT_SEC
    t_off = 0.0

    def record(cycle_no: int, status: str, reason: str, **kw) -> dict:
        row = {"cycle": cycle_no, "status": status, "reason": reason,
               "off_duration": None, "boot_duration": None,
               "wifi": None, "ir_sequence": ir_sequence,
               "ir_status": "", "ir_sent": 0, "ir_failed": 0}
        row.update(kw)
        cycles.append(row)
        return row

    init = adb_devices()
    if init is None:
        print("[sniff] initial state: unreadable (adb devices failed)")
    else:
        print(f"[sniff] initial state: "
              f"{'online' if init.get(args.device) == 'device' else 'OFFLINE'}"
              f" (transport={init.get(args.device) or 'absent'})")
    print("[sniff] the relay drives the cycle; waiting for the first power cut")

    try:
        while len(cycles) < iterations:
            n = len(cycles) + 1

            if phase == "WAIT_OFF":
                devs = adb_devices()
                if devs is None:
                    n_read_fail += 1
                elif devs.get(args.device) == "device":
                    saw_present = True
                    absent = 0
                    # t_phase / next_hb are deliberately NOT reset here. They
                    # clock how long this WAIT_OFF has lasted, which is how long
                    # the script has been waiting for the relay - resetting them
                    # on every healthy read would mean a device that simply sits
                    # online produces no output at all, and a user could not tell
                    # a working script from a hung one.
                else:
                    absent += 1
                    if absent >= OFF_CONFIRM_POLLS:
                        # The power cut.
                        off_start_unknown = not saw_present
                        t_off = time.time()
                        phase = "WAIT_BACK"
                        saw_present = False
                        t_phase = t_off
                        next_hb = t_phase + HEARTBEAT_SEC
                        print(f"\n========== cycle {n}/{iterations} ==========")
                        print(f"[{n}] power cut observed"
                              + (" (device was ALREADY offline when this cycle "
                                 "began - its off duration cannot be measured)"
                                 if off_start_unknown else ""))
                        print(f"[{n}] `adb reconnect offline` rc={adb_reconnect_offline()}")
                        print(f"[{n}] waiting for the device to return to adb "
                              f"(watchdog {off_watchdog_sec}s)...")

            elif phase == "WAIT_BACK":
                devs = adb_devices()
                if devs is None:
                    n_read_fail += 1
                elif devs.get(args.device) == "device":
                    t_return = time.time()
                    # Measured once here and reused by both exit paths below, so
                    # a failed boot and a good one report the same outage.
                    off_sec = observed_off_sec(t_off, t_return,
                                               off_start_unknown)
                    phase = "WAIT_BOOT"
                    t_phase = t_return
                    next_hb = t_return + HEARTBEAT_SEC
                    print(f"[{n}] device back in adb after "
                          f"{t_return - t_off:.1f}s (observed)")
                    print(f"[{n}] waiting for sys.boot_completed "
                          f"(timeout {boot_timeout_sec}s)...")
                    ok, boot_dur = wait_boot_completed(args.device,
                                                       boot_timeout_sec,
                                                       poll_interval_sec)
                    if not ok:
                        print(f"[{n}] FAIL: sys.boot_completed never arrived "
                              f"within {boot_timeout_sec}s")
                        record(n, "FAIL", "boot_timeout",
                               off_duration=off_sec,
                               boot_duration=round(boot_dur, 1))
                        phase = "WAIT_OFF"
                        t_phase = time.time()
                        next_hb = t_phase + HEARTBEAT_SEC
                    else:
                        print(f"[{n}] boot completed after {boot_dur:.1f}s")
                        phase = "POST_BOOT"
                elif time.time() - t_off >= off_watchdog_sec:
                    print(f"[{n}] FAIL: device did not return to adb within "
                          f"{off_watchdog_sec}s")
                    print("[sniff] stopping the run: every further cycle would "
                          "fail the same way")
                    record(n, "FAIL", "return_timeout")
                    stopped_early_reason = (
                        f"第 {n} 轮:设备离开 adb 后 {off_watchdog_sec} 秒内未返回,"
                        f"已停止运行(无法区分是没上电还是没起来)")
                    break

            elif phase == "POST_BOOT":
                print(f"[{n}] waiting {post_boot_wait_sec}s (post-boot)...")
                time.sleep(post_boot_wait_sec)
                wifi = read_wifi(args.device)
                if wifi is None:
                    print(f"[{n}] WiFi: read failed (this is NOT an outage)")
                else:
                    print(f"[{n}] WiFi: {wifi.get('state')}"
                          f" / {wifi.get('ssid') or '-'}"
                          f" (read after the post-boot wait)")
                print(f"[{n}] sending IR sequence: {ir_sequence or '(none)'}")
                ir_status, ir_sent, ir_failed = send_sequence_once(
                    args.device, ir_sequence)
                if ir_sequence:
                    print(f"[{n}] IR: {ir_status}")
                print(f"[{n}] PASS")
                record(n, "PASS", "booted",
                       off_duration=off_sec,
                       boot_duration=round(boot_dur, 1),
                       wifi=wifi, ir_status=ir_status,
                       ir_sent=ir_sent, ir_failed=ir_failed)
                phase = "WAIT_OFF"
                t_phase = time.time()
                next_hb = t_phase + HEARTBEAT_SEC

            # Proof of life while waiting for something this script does not
            # control. stdout events are allowed (D-36); this is not a UI banner.
            now = time.time()
            if phase in ("WAIT_OFF", "WAIT_BACK") and now >= next_hb:
                where = ("waiting for the power cut" if phase == "WAIT_OFF"
                         else "waiting for the device to return to adb")
                print(f"[sniff] {where} (elapsed {int(now - t_phase)}s)")
                next_hb = now + HEARTBEAT_SEC

            if phase in ("WAIT_OFF", "WAIT_BACK"):
                time.sleep(poll_interval_sec)
    except KeyboardInterrupt:
        interrupted = True
        print("\n[interrupted] stop received")

    # --- summary + report ---------------------------------------------------
    v = verdict(cycles, interrupted)
    total, passed = v["total"], v["passed"]
    boot_failed, return_failed = v["boot_failed"], v["return_failed"]
    rate, judged, all_passed = v["rate"], v["judged"], v["all_passed"]
    exit_code = v["exit_code"]
    # One verdict, two consumers (the archived JSON and the HTML doc).
    result_txt = ("PASS" if all_passed else "FAIL") if judged else None
    level_txt = ("ok" if all_passed else "fail") if judged else "inconclusive"

    boots = [c["boot_duration"] for c in cycles if c["boot_duration"] is not None]
    offs = [c["off_duration"] for c in cycles if c["off_duration"] is not None]
    wifi_down = sum(1 for c in cycles
                    if c["wifi"] is not None
                    and c["wifi"].get("state") in WIFI_STATES_DOWN)
    wifi_unread = sum(1 for c in cycles if c["wifi"] is None)
    wifi_read = sum(1 for c in cycles if c["wifi"] is not None)

    def span(vals: list, empty: str = "n/a") -> str:
        """Min~max, or `empty` when nothing was measurable.

        `empty` is a parameter because this string reaches two different
        surfaces: stdout, which must stay ASCII (D-07), and the Chinese report,
        where an em-dash reads better.
        """
        if not vals:
            return empty
        return f"{min(vals):.1f} ~ {max(vals):.1f}s"

    # Two renderings of one fact, because it reaches two audiences: the Chinese
    # report, and stdout, which is ASCII-only (D-07). Same condition, so they
    # cannot disagree - only the wording differs.
    wifi_all_ok = bool(wifi_read and not wifi_down and not wifi_unread)
    if judged:
        wifi_txt = (f"每次均 {WIFI_ZH['ok']}" if wifi_all_ok
                    else f"{wifi_down} 次未连接"
                         + (f"、{wifi_unread} 次读取失败" if wifi_unread else ""))
        wifi_out = (f"ok every cycle ({wifi_read} read(s))" if wifi_all_ok
                    else f"{wifi_down} down, {wifi_unread} unreadable"
                         f" of {wifi_read + wifi_unread} read(s)")
    else:
        wifi_txt = wifi_out = "n/a"

    print(f"\n========== summary ==========")
    print(f"  passed: {passed} / {total}"
          + (f"  (planned {iterations})" if total != iterations else ""))
    print(f"  success rate: {rate:.1f}%")
    print(f"  boot time (s): {span(boots)}")
    print(f"  off time  (s): {span(offs)}   "
          f"(observed; the relay owns the rhythm)")
    print(f"  wifi after boot: {wifi_out}  (recorded, never judged)")
    if cycles:
        marks = " ".join("P" if c["status"] == "PASS" else "F" for c in cycles)
        print(f"  per-cycle: {marks}")
    if n_read_fail:
        print(f"  adb reads that failed: {n_read_fail} (not counted as outages)")

    warn_bits = []
    if judged and not all_passed:
        warn_bits.append(f"{total - passed} 轮未能开机"
                         f"(开机超时 {boot_failed} 轮、未返回 adb "
                         f"{return_failed} 轮)")
    if interrupted:
        warn_bits.append(f"本次运行被手动中断,判定只覆盖已完成的 {total} 轮")
    if wifi_down or wifi_unread:
        warn_bits.append(f"开机后 WiFi 有 {wifi_down} 次未连接、"
                         f"{wifi_unread} 次读取失败(仅记录,不参与判定)")
    if n_read_fail:
        warn_bits.append(f"有 {n_read_fail} 次 adb devices 读取失败"
                         f"(未计入断电判定)")

    cycle_rows = []
    for c in cycles:
        off_cell = ({"text": "—", "sub": "脚本启动时设备已离线", "cls": "num"}
                    if c["off_duration"] is None
                    else {"text": f"{c['off_duration']:.1f}", "cls": "num"})
        boot_cell = ({"text": "—", "cls": "num"}
                     if c["boot_duration"] is None
                     else {"text": f"{c['boot_duration']:.1f}", "cls": "num"})
        if c["wifi"] is None:
            wifi_cell = {"text": "读取失败", "cls": "note"}
        else:
            wifi_cell = {"text": WIFI_ZH.get(c["wifi"].get("state"),
                                             c["wifi"].get("state")),
                         "sub": c["wifi"].get("ssid") or "", "cls": "v"}
        ir_cell = ({"text": c["ir_status"], "cls": "note"} if ir_sequence
                   else {"text": "未配置", "cls": "note"})
        cycle_rows.append([c["cycle"], off_cell, boot_cell, wifi_cell, ir_cell,
                           {"chip": "ok" if c["status"] == "PASS" else "fail"}])

    notes = [
        poll_note_zh(poll_interval_sec),
        "「断电时长」是「设备离开 adb → 回到 adb」,其中包含 adbd 重新上线的时间,"
        "因此它是继电器断电窗口的上界,不是那个窗口本身。",
        "脚本启动时设备已经离线的那一轮,断电时长不可测,报告写「—」而不写 0"
        "(0 秒读起来像「没断过电」,与事实相反)。",
        "红外序列只报告「发出去了多少次按键」,无法报告 YouTube 是否真的打开并播放 —— "
        "序列的语义结果对脚本不可知。",
        f"设备为 Android 14 / SDK 34 时,`KEYCODE_*` 配 `LongXXXX` 长按静默失效,"
        f"只有 `KEY_*` 长按真正注入;脚本会报告「已发送」而屏幕上什么也没发生。",
        "开机后的红外序列每轮只跑一遍(单次截断,不循环调用),`count` 仍然生效。"
        "因此 ini 的 `delay_ms` 变成了轮内的等待:若复用 keepalive 型序列"
        "(它的 `delay_ms` 是重复间隔,例如 20 分钟),该轮会在序列中途停住不动。"
        "开机后动作请单独写一条延迟很短的序列。",
        "开机后 WiFi 状态只作为事实记录(五态:ok/noip/noassoc/off/unknown),"
        "永远不参与通过/不通过判定。",
    ]

    data = {
        "test_name": "硬开关机压测(继电器自主循环)",
        "device_id": args.device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "iterations_planned": iterations,
        "iterations_run": total,
        "passed": passed,
        "failed": total - passed,
        "success_rate": round(rate, 2),
        # The verdict is repeated here on purpose. build_payload's copy lives in
        # the HTML only - the file on disk is THIS dict - so without it an
        # archived run would be a pile of numbers with no answer in it.
        "result": result_txt,
        "level": level_txt,
        "interrupted": interrupted,
        "interrupted_note": (f"手动中断:已完成 {total} 轮,判定只覆盖这些轮次"
                             if interrupted else ""),
        "stopped_early_reason": stopped_early_reason,
        "cycles": cycles,
        "summary": {"boot_sec": {"min": min(boots) if boots else None,
                                 "max": max(boots) if boots else None},
                    "off_sec": {"min": min(offs) if offs else None,
                                "max": max(offs) if offs else None},
                    "wifi_down": wifi_down, "wifi_unread": wifi_unread,
                    "adb_read_failures": n_read_fail},
        "config": {
            "iterations": iterations,
            "post_boot_wait_sec": post_boot_wait_sec,
            "ir_sequence": ir_sequence,
            "boot_timeout_sec": boot_timeout_sec,
            "off_watchdog_sec": off_watchdog_sec,
            "poll_interval_sec": poll_interval_sec,
            "off_confirm_polls": OFF_CONFIRM_POLLS,
            "required_power_mode": REQUIRED_POWER_MODE,
            "power_controlled_by": "external relay (autonomous timer; script only observes)",
            "resolution_note": poll_note(poll_interval_sec),
            "script_version": SCRIPT_VERSION,
        },
    }

    doc = _pptp_report.build_payload(
        script="power_cycle_stress.py",
        script_version=SCRIPT_VERSION,
        test_name="硬开关机压测(继电器自主循环)",
        device_id=args.device,
        result=result_txt,
        level=level_txt,
        count_zh=(f"{passed}/{total} 轮开机成功" if judged
                  else "未完整跑完任何一轮"),
        warn_zh=" ".join(warn_bits),
        rows=[
            _pptp_report.row("overall", "总体结果",
                             result_txt or "—", level_txt,
                             "全部轮次均完成开机" if all_passed else
                             ("存在未能开机的轮次" if judged
                              else "本次运行没有可判定的轮次")),
            _pptp_report.row("passed", "开机成功 / 总轮次",
                             f"{passed} / {total}",
                             ("ok" if all_passed else "fail") if judged
                             else "inconclusive",
                             f"计划 {iterations} 轮,成功率 {rate:.1f}%"),
            _pptp_report.row("boot_fail", "未能开机",
                             f"{total - passed} 轮",
                             "info" if (judged and all_passed) else
                             ("fail" if judged else "inconclusive"),
                             f"开机超时 {boot_failed} 轮、离开 adb 后未返回 "
                             f"{return_failed} 轮"),
            _pptp_report.row("boot_time", "开机耗时(观测量)",
                             span(boots, "—"),
                             "info",
                             f"从设备回到 adb 到 sys.boot_completed;"
                             f"超时阈值 {boot_timeout_sec}s,超过即该轮失败"),
            _pptp_report.row("off_time", "断电时长(观测量)",
                             span(offs, "—"),
                             "info",
                             f"继电器节奏的观测量,不是判据;含 adbd 重新上线时间,"
                             f"±{poll_interval_sec}s 分辨率"),
            _pptp_report.row("wifi_state", "开机后 WiFi(仅记录)",
                             wifi_txt,
                             "info",
                             "不参与通过/不通过判定"),
        ],
        params_schema=PARAMS,
        params_values={"iterations": iterations,
                       "post_boot_wait_sec": post_boot_wait_sec,
                       "ir_sequence": ir_sequence,
                       "boot_timeout_sec": boot_timeout_sec,
                       "off_watchdog_sec": off_watchdog_sec,
                       "poll_interval_sec": poll_interval_sec},
        sections=[
            _pptp_report.section_table(
                "cycles", "逐轮明细",
                ["轮次", "断电时长(s)", "开机耗时(s)", "开机后 WiFi", "红外序列",
                 "结果"],
                cycle_rows,
                sub_zh=("时长为观测值,分辨率 ±"
                        f"{poll_interval_sec}s"),
                empty_zh="本次运行没有完整跑完任何一轮"),
            _pptp_report.section_kv(
                "prereq", "本次运行的前提条件",
                [("设备上电方式", f"{REQUIRED_POWER_MODE}(必需,否则断电后不会自动开机)"),
                 ("断电/上电由谁驱动", "外部继电器,自带定时器;脚本只观察,不控制电源"),
                 ("断电如何被识别", f"轮询 `adb devices` 每 {poll_interval_sec}s 一次,"
                                 f"连续 {OFF_CONFIRM_POLLS} 次看不到 `device` 才算断电"),
                 ("开机完成如何被识别", "轮询 `getprop sys.boot_completed` == 1"),
                 ("两个超时的分工",
                  f"开机完成 {boot_timeout_sec}s(判 FAIL);"
                  f"断电后未返回 {off_watchdog_sec}s(看门狗,判 FAIL 并停止运行)"),
                 ("断电时长是什么", "「离开 adb → 回到 adb」,含 adbd 上线时间,"
                                "是继电器窗口的上界")],
                sub_zh="这是流程能跑起来的前提"),
            _pptp_report.section_list("notes", "判读说明", notes,
                                      sub_zh="本脚本不能告诉你什么,同样重要"),
        ],
        detail=data,
    )

    try:
        report_path = save_report(args.device, data)
        html_path = _pptp_report.write_html_report(report_path, doc)
        if html_path:
            print(f"  html             = {html_path}")
        print(f"  report           : {report_path}")
    except Exception as e:
        print(f"[warn] failed to save report: {e}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
