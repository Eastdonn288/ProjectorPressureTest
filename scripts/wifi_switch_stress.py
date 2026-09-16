"""
wifi_switch_stress.py - PPTP: multi-network Wi-Fi cycle/switch stress test.

Cycles through a pre-configured list of Wi-Fi networks, connecting to each one
in turn. Each connection is verified against the expected SSID; success rate is
the fraction of connections that matched.

Per cycle:
  1. for each network in WIFI_NETWORKS:
       connect via `cmd wifi connect-network <ssid> <security> <password>`
       wait `connect_wait_sec` (retry once if nothing connected)
       PASS iff the connected SSID == expected SSID
  2. small `switch_gap_sec` pause between switches

At the end: print success rate, save a JSON report to
`reports/stress-test/wifi/`, exit 0 iff rate >= 98%.

Contract (matches other PPTP scripts):
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional; see PARAMS below)

Run standalone:
    python scripts/wifi_switch_stress.py --device <serial>
    python scripts/wifi_switch_stress.py --device <serial> \
        --params '{"cycles": 2, "connect_wait_sec": 10}'

NOTE on root:
  `cmd wifi connect-network` writes Wi-Fi config and throws
  `SecurityException: Uid 2000 does not have access to connect-network`
  when run without root on this device, so the connect command is invoked as
  `su 0 cmd wifi ...` (AOSP-style su, NOT `su -c`). Set `use_su: false` if a
  target device allows it without root.

NOTE on the network list:
  WIFI_NETWORKS is hard-coded/pre-configured in this file (per project
  decision it is intentionally NOT a frontend param). SSID/password must not
  contain shell-significant characters (spaces).
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
# platform and for a plain `python scripts/wifi_switch_stress.py`. See
# docs/REPORT_FORMAT.md.
import _pptp_report

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a summary line.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

SCRIPT_VERSION = "1.0.0"

# Pre-configured Wi-Fi networks to cycle through (NOT a frontend param).
WIFI_NETWORKS = [
    {"ssid": "WCS-DQA3-GXX_5G", "password": "88888888", "security": "wpa3"},
    {"ssid": "WCS-DQA2-CC_5G_1", "password": "wcsdqa002", "security": "wpa2"},
    {"ssid": "NW-DQA-CYJ-5G", "password": "test1111", "security": "wpa2"},
    {"ssid": "NW-DQA-FYH_5G1", "password": "test1111", "security": "wpa2"},
]

# Frontend-configurable params (declared for the PPTP platform).
# The platform renders a config modal from this list and passes the chosen
# values back via `--params`. Field keys: name / label / type / default /
# min / max. Run with `--dump-params` to print this schema as JSON.
PARAMS = [
    {"name": "cycles", "label": "循环次数", "type": "int", "default": 100,
     "min": 1, "max": 100000},
    {"name": "connect_wait_sec", "label": "连接等待(秒)", "type": "int",
     "default": 15, "min": 0, "max": 3600},
    {"name": "switch_gap_sec", "label": "切换间隔(秒)", "type": "int",
     "default": 2, "min": 0, "max": 3600},
    {"name": "use_su", "label": "cmd wifi 使用 su 权限", "type": "bool",
     "default": True},
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


def check_adb_connected(serial: str, timeout: int = 5) -> bool:
    """True if `adb shell echo 1` round-trips correctly."""
    rc, out, _ = adb_shell(serial, "echo", "1", timeout=timeout)
    return rc == 0 and out.strip() == "1"


def get_connected_ssid(serial: str) -> str | None:
    """SSID the device is currently connected to, or None."""
    rc, out, _ = adb_shell(serial, "cmd", "wifi", "status", timeout=10)
    if rc != 0:
        return None
    m = re.search(r'Wifi is connected to "([^"]+)"', out)
    return m.group(1) if m else None


def connect_to_wifi(serial: str, ssid: str, password: str, security: str,
                    wait_sec: int, use_su: bool) -> bool:
    """Connect to the given SSID and verify it actually connects.

    The connect command needs root on this device (`su 0 cmd wifi ...`).
    Returns True iff the connected SSID ends up matching `ssid`.
    """
    print(f"[connect] -> {ssid} ({security})...")
    if use_su:
        adb_shell(serial, "su", "0", "cmd", "wifi", "connect-network",
                  ssid, security, password, timeout=10)
    else:
        adb_shell(serial, "cmd", "wifi", "connect-network",
                  ssid, security, password, timeout=10)
    time.sleep(wait_sec)

    ssid_now = get_connected_ssid(serial)
    if ssid_now is None:
        print(f"  not connected after {wait_sec}s, waiting once more...")
        time.sleep(wait_sec)
        ssid_now = get_connected_ssid(serial)

    ok = ssid_now == ssid
    print(f"  expected: {ssid}")
    print(f"  actual  : {ssid_now}")
    print(f"  result  : {'PASS' if ok else 'FAIL'}")
    return ok


def save_report(device: str, cycles: int, networks: list[dict],
                planned: int, actual: int, success: int,
                success_rate: float, passed: bool) -> str:
    """Write the JSON report under reports/stress-test/wifi/. Returns path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test", "wifi")
    os.makedirs(report_dir, exist_ok=True)

    data = {
        "test_name": "WiFi循环连接测试",
        "device_id": device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_cycles": cycles,
        "wifi_networks": networks,
        "planned_attempts": planned,
        "actual_attempts": actual,
        "success_count": success,
        "success_rate": round(success_rate, 2),
        "passed": bool(passed),
    }
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir, f"wifi_cycle_{dev_short}_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return json_path


def main() -> int:
    # --dump-params is consumed by the PPTP platform to render the params
    # config modal. Must short-circuit before argparse (and before any device
    # interaction). ensure_ascii keeps stdout pure-ASCII for safe piping.
    if "--dump-params" in sys.argv:
        print(json.dumps({"fields": PARAMS}))
        return 0

    defaults = {f["name"]: f["default"] for f in PARAMS}

    p = argparse.ArgumentParser(description="Multi-network Wi-Fi cycle stress test")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"cycles"?, "connect_wait_sec"?, "switch_gap_sec"?, "use_su"?}')
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    cycles = int(params.get("cycles", defaults["cycles"]))
    connect_wait_sec = int(params.get("connect_wait_sec", defaults["connect_wait_sec"]))
    switch_gap_sec = int(params.get("switch_gap_sec", defaults["switch_gap_sec"]))
    use_su = bool(params.get("use_su", defaults["use_su"]))

    print(f"[config] device        = {args.device}")
    print(f"[config] cycles        = {cycles}")
    print(f"[config] connect_wait  = {connect_wait_sec}s (after each connect)")
    print(f"[config] switch_gap    = {switch_gap_sec}s (between switches)")
    print(f"[config] cmd wifi su   = {use_su} (su 0 cmd wifi ...)")

    if not check_adb_connected(args.device):
        print("[error] device not reachable via adb")
        return 1

    networks = WIFI_NETWORKS
    if not networks:
        print("[warn] WIFI_NETWORKS is empty - nothing to cycle")
        return 1
    print(f"[setup] networks       = {len(networks)}")
    for net in networks:
        print(f"          - {net['ssid']} ({net.get('security', 'wpa2')})")

    planned = len(networks) * cycles
    success = 0
    actual = 0

    try:
        for cycle in range(1, cycles + 1):
            if not check_adb_connected(args.device):
                print("\n[warn] ADB connection lost - stopping with partial results")
                break
            print(f"\n=== cycle {cycle}/{cycles} ===")
            for net in networks:
                actual += 1
                if connect_to_wifi(args.device, net["ssid"], net["password"],
                                   net.get("security", "wpa2"),
                                   connect_wait_sec, use_su):
                    success += 1
                if switch_gap_sec > 0:
                    time.sleep(switch_gap_sec)
    except KeyboardInterrupt:
        print("\n[interrupt] Ctrl+C received - summarizing completed connections")

    success_rate = (success / actual * 100) if actual > 0 else 0.0
    passed = success_rate >= 98.0

    print(f"\n=== results ===")
    print(f"  planned attempts : {planned}")
    print(f"  actual attempts  : {actual}")
    print(f"  success          : {success}/{actual} = {success_rate:.2f}%")
    print(f"  OVERALL          : {'PASS' if passed else 'FAIL'} (>= 98%)")

    # The Chinese HTML twin of the report above, for reading afterwards. Built
    # from the same local values as the block just printed, so the two cannot
    # disagree. NOTE: `wifi_networks` in the report holds plaintext passwords,
    # and this page is meant to be handed to someone else - hence hide_keys.
    doc = _pptp_report.build_payload(
        script="wifi_switch_stress.py",
        script_version=SCRIPT_VERSION,
        test_name="WiFi 循环连接测试",
        device_id=args.device,
        result="PASS" if passed else "FAIL",
        level="ok" if passed else "fail",
        count_zh=f"{success}/{actual} 次连接成功",
        warn_zh=("" if passed else
                 f"成功率 {success_rate:.2f}% 低于达标线 98%,"
                 f"共 {actual - success} 次连接未匹配到预期 SSID"),
        rows=[
            _pptp_report.row("overall", "总体结果",
                             "PASS" if passed else "FAIL",
                             "ok" if passed else "fail",
                             "达标线:成功率 >= 98%"),
            _pptp_report.row("planned", "计划连接次数", planned, "info",
                             f"{len(networks)} 个网络 × {cycles} 轮"),
            _pptp_report.row("actual", "实际连接次数", actual, "info",
                             "中途中断,未跑满计划" if actual < planned
                             else "按计划跑满"),
            _pptp_report.row("success", "连接成功次数", success,
                             "ok" if success == actual and actual else "info",
                             f"失败 {actual - success} 次"),
            _pptp_report.row("rate", "连接成功率", f"{success_rate:.2f}%",
                             "ok" if passed else "fail", "达标线 98.00%"),
        ],
        params_schema=PARAMS,
        params_values={"cycles": cycles,
                       "connect_wait_sec": connect_wait_sec,
                       "switch_gap_sec": switch_gap_sec,
                       "use_su": use_su},
        sections=[
            _pptp_report.section_table(
                "networks", "参与轮换的网络", ["SSID", "加密方式"],
                [[n["ssid"], n.get("security", "wpa2")] for n in networks],
                sub_zh="口令不在本页显示"),
        ],
        detail={"total_cycles": cycles, "wifi_networks": networks,
                "planned_attempts": planned, "actual_attempts": actual,
                "success_count": success,
                "success_rate": round(success_rate, 2), "passed": bool(passed)})
    doc["hide_keys"] = ["password"]

    try:
        report_path = save_report(args.device, cycles, networks, planned, actual,
                                  success, success_rate, passed)
        html_path = _pptp_report.write_html_report(report_path, doc)
        if html_path:
            print(f"  html             = {html_path}")
        print(f"  report           : {report_path}")
    except Exception as e:
        print(f"[warn] failed to save report: {e}")

    return 0 if (actual > 0 and passed) else 1


if __name__ == "__main__":
    sys.exit(main())
