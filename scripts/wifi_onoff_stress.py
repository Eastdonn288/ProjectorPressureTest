"""
wifi_onoff_stress.py - PPTP: Wi-Fi on/off reconnect + scan-count stress test.

Per iteration:
  1. disable Wi-Fi (`cmd wifi set-wifi-enabled disabled`), wait `off_sec`
  2. enable Wi-Fi  (`cmd wifi set-wifi-enabled enabled`), wait `on_sec` for re-associate
  3. check Wi-Fi connected SSID (`cmd wifi status`)
  4. if connected, ping `ping_target` for network connectivity
  5. scan networks (`wpa_cli ... scan_results`) and compare count to the
     baseline taken before the loop
  6. accumulate pass/fail per check

At the end: print per-check success rates, save a JSON report to
`reports/stress-test/wifi/`, exit 0 iff all three checks pass.

Contract (matches other PPTP scripts):
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional; see param names below)

Run standalone:
    python scripts/wifi_onoff_stress.py --device <serial>
    python scripts/wifi_onoff_stress.py --device <serial> \
        --params '{"iterations": 3, "on_sec": 10}'

NOTE on wpa_cli + root:
  `wpa_cli -i wlan0 scan` only returns scan results when run as root on this
  device, so scan / scan_results are invoked as `su 0 wpa_cli ...`.
  This is the AOSP-style su (`su 0 <cmd>`), NOT `su -c 'cmd'` — `su -c`
  fails with "invalid uid/gid -c" on this device. Set `use_su: false` in
  params if a target device exposes wpa_cli without root.

Stop handling: PPTP platform sends CTRL_BREAK (SIGBREAK on Windows), which the
platform turns into KeyboardInterrupt here. The loop then falls through to a
summary of the completed iterations (report is only saved on full completion,
matching the original test's behavior).
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a summary line.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# Frontend-configurable params (declared for the PPTP platform).
# The platform renders a config modal from this list and passes the chosen
# values back via `--params`. Field keys: name / label / type / default /
# min / max. Run with `--dump-params` to print this schema as JSON.
PARAMS = [
    {"name": "iterations", "label": "循环次数", "type": "int", "default": 2,
     "min": 1, "max": 100000},
    {"name": "off_sec", "label": "关闭WiFi后等待(秒)", "type": "int",
     "default": 5, "min": 0, "max": 3600},
    {"name": "on_sec", "label": "开启WiFi后等待(秒)", "type": "int",
     "default": 15, "min": 0, "max": 3600},
    {"name": "scan_sec", "label": "触发扫描后等待(秒)", "type": "int",
     "default": 5, "min": 0, "max": 3600},
    {"name": "count_threshold", "label": "扫描数量比例阈值", "type": "float",
     "default": 0.5, "min": 0.1, "max": 1.0},
    {"name": "use_su", "label": "wpa_cli 使用 su 权限", "type": "bool",
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


def ping_test(serial: str, target: str, count: int = 2, deadline: int = 5) -> bool:
    """True if ping gets any reply (less than 100% packet loss)."""
    rc, out, _ = adb_shell(
        serial, "ping", "-c", str(count), "-W", str(deadline), target,
        timeout=15,
    )
    if rc != 0 or not out:
        return False
    low = out.lower()
    if "bytes from" in out or "ttl=" in low:
        m = re.search(r"(\d+)% packet loss", out)
        if m:
            return int(m.group(1)) < 100
        return True
    return False


def parse_scan_results(scan_output: str) -> list[dict]:
    """Parse `wpa_cli scan_results` tab-separated lines into network dicts."""
    networks: list[dict] = []
    if not scan_output:
        return networks

    lines = scan_output.strip().split("\n")

    # Skip header line(s) (bssid / Selected ...)
    start_index = 0
    for i, line in enumerate(lines):
        if line and not line.startswith("bssid") and not line.startswith("Selected"):
            start_index = i
            break

    for line in lines[start_index:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 5:
            networks.append({
                "bssid": parts[0],
                "frequency": parts[1],
                "signal_level": parts[2],
                "flags": parts[3],
                "ssid": parts[4] if parts[4] else "[hidden]",
            })
        elif len(parts) >= 1:
            networks.append({"bssid": parts[0], "ssid": "unknown"})
    return networks


def scan_wifi_networks(serial: str, iface: str = "wlan0", use_su: bool = True,
                       scan_sec: int = 5) -> list[dict]:
    """Trigger a scan, wait, fetch results, return parsed network list.

    wpa_cli needs root on this device, so scan + scan_results run as
    `su 0 wpa_cli ...` when use_su is True.
    """
    print("[scan] triggering wpa_cli scan...")
    if use_su:
        adb_shell(serial, "su", "0", "wpa_cli", "-i", iface, "scan", timeout=15)
    else:
        adb_shell(serial, "wpa_cli", "-i", iface, "scan", timeout=15)
    time.sleep(scan_sec)

    if use_su:
        rc, out, _ = adb_shell(serial, "su", "0", "wpa_cli", "-i", iface,
                               "scan_results", timeout=15)
    else:
        rc, out, _ = adb_shell(serial, "wpa_cli", "-i", iface,
                               "scan_results", timeout=15)
    networks = parse_scan_results(out if rc == 0 else "")
    print(f"[scan] found {len(networks)} network(s)")
    return networks


def save_report(device: str, planned: int, actual: int, expected_ssid: str,
                wifi_ok: int, network_ok: int, count_ok: int,
                count_abnormal: int, count_details: list[dict],
                wifi_rate: float, network_rate: float, count_rate: float) -> str:
    """Write the JSON report under reports/stress-test/wifi/. Returns path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test", "wifi")
    os.makedirs(report_dir, exist_ok=True)

    data = {
        "test_name": "WiFi开关回连测试",
        "device_id": device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "planned_attempts": planned,
        "actual_attempts": actual,
        "expected_ssid": expected_ssid,
        "wifi_success_count": wifi_ok,
        "wifi_success_rate": round(wifi_rate, 2),
        "network_success_count": network_ok,
        "network_success_rate": round(network_rate, 2),
        "wifi_count_success_count": count_ok,
        "wifi_count_success_rate": round(count_rate, 2),
        "wifi_count_abnormal_count": count_abnormal,
        "wifi_count_abnormal_details": count_details,
    }
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir, f"wifi_switch_{dev_short}_{ts}.json")
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

    p = argparse.ArgumentParser(description="Wi-Fi on/off reconnect stress test")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"iterations"?, "off_sec"?, "on_sec"?, "scan_sec"?, '
                        '"count_threshold"?, "use_su"?, "wifi_iface"?, "ping_target"?, '
                        '"ping_count"?, "ping_deadline"?, "wifi_rate_threshold"?, '
                        '"count_rate_threshold"?, "network_rate_threshold"?}')
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    iterations = int(params.get("iterations", defaults["iterations"]))
    off_sec = int(params.get("off_sec", defaults["off_sec"]))
    on_sec = int(params.get("on_sec", defaults["on_sec"]))
    scan_sec = int(params.get("scan_sec", defaults["scan_sec"]))
    count_threshold = float(params.get("count_threshold", defaults["count_threshold"]))
    use_su = bool(params.get("use_su", defaults["use_su"]))
    wifi_iface = str(params.get("wifi_iface", "wlan0"))
    ping_target = str(params.get("ping_target", "223.5.5.5"))
    ping_count = int(params.get("ping_count", 2))
    ping_deadline = int(params.get("ping_deadline", 5))
    wifi_rate_threshold = float(params.get("wifi_rate_threshold", 98.0))
    network_rate_threshold = float(params.get("network_rate_threshold", 98.0))
    count_rate_threshold = float(params.get("count_rate_threshold", 80.0))

    print(f"[config] device      = {args.device}")
    print(f"[config] iterations  = {iterations}")
    print(f"[config] off_sec     = {off_sec} (wait after wifi off)")
    print(f"[config] on_sec      = {on_sec} (wait after wifi on)")
    print(f"[config] scan_sec    = {scan_sec} (wait after scan trigger)")
    print(f"[config] wpa_cli su  = {use_su} (su 0 wpa_cli ...)")
    print(f"[config] iface       = {wifi_iface}")
    print(f"[config] ping        = {ping_target} (count={ping_count}, deadline={ping_deadline}s)")

    if not check_adb_connected(args.device):
        print("[error] device not reachable via adb")
        return 1

    current_ssid = get_connected_ssid(args.device)
    if not current_ssid:
        print("[warn] device is not connected to any Wi-Fi - aborting")
        return 1
    print(f"[setup] connected to: {current_ssid}")

    # Baseline scan: wifi-count check compares every iteration against this.
    print("\n=== baseline scan ===")
    initial_count = len(scan_wifi_networks(args.device, wifi_iface, use_su, scan_sec))
    print(f"[setup] baseline wifi count = {initial_count}")

    wifi_ok = network_ok = count_ok = 0
    count_abnormal = 0
    count_details: list[dict] = []
    actual = 0
    interrupted = False

    try:
        for _ in range(iterations):
            if not check_adb_connected(args.device):
                print("\n[warn] ADB connection lost - stopping with partial results")
                break
            actual += 1
            print(f"\n=== iteration {actual}/{iterations} ===")

            # 1. disable wifi
            print("[1/4] disabling Wi-Fi...")
            adb_shell(args.device, "cmd", "wifi", "set-wifi-enabled", "disabled", timeout=10)
            time.sleep(off_sec)

            # 2. enable wifi
            print("[2/4] enabling Wi-Fi...")
            adb_shell(args.device, "cmd", "wifi", "set-wifi-enabled", "enabled", timeout=10)
            time.sleep(on_sec)

            # 3. connection check
            print("[3/4] checking Wi-Fi connection...")
            ssid = get_connected_ssid(args.device)
            connected = ssid is not None
            print(f"      connected: {'PASS' if connected else 'FAIL'} (ssid={ssid})")
            if connected:
                wifi_ok += 1

            # 4. ping (only if connected)
            network_connected = False
            if connected:
                print(f"      ping {ping_target}...")
                network_connected = ping_test(args.device, ping_target, ping_count, ping_deadline)
                print(f"      network: {'PASS' if network_connected else 'FAIL'}")
                if network_connected:
                    network_ok += 1

            # 5. scan + count check
            print("[4/4] scanning networks + count check...")
            networks = scan_wifi_networks(args.device, wifi_iface, use_su, scan_sec)
            cur_count = len(networks)
            ratio = (cur_count / initial_count) if initial_count > 0 else 1.0
            count_normal = ratio >= count_threshold
            print(f"      count {cur_count} vs baseline {initial_count} "
                  f"(ratio {ratio:.2f}): {'PASS' if count_normal else 'FAIL'}")
            if count_normal:
                count_ok += 1
            else:
                count_abnormal += 1
                count_details.append({
                    "attempt": actual,
                    "current_count": cur_count,
                    "previous_count": initial_count,
                    "ratio": round(ratio, 2),
                })

            for net in networks[:5]:
                ssid_show = net["ssid"] if len(net["ssid"]) <= 24 else net["ssid"][:21] + "..."
                print(f"      {ssid_show:<26} {net['signal_level']}dBm")
    except KeyboardInterrupt:
        interrupted = True
        print("\n[interrupt] Ctrl+C received - summarizing completed iterations")

    # Per-check success rates (based on actual iterations run)
    wifi_rate = (wifi_ok / actual * 100) if actual > 0 else 0.0
    network_rate = (network_ok / actual * 100) if actual > 0 else 0.0
    count_rate = (count_ok / actual * 100) if actual > 0 else 0.0

    wifi_passed = wifi_rate >= wifi_rate_threshold
    network_passed = network_rate >= network_rate_threshold
    count_passed = count_rate >= count_rate_threshold
    overall_passed = wifi_passed and network_passed and count_passed

    print(f"\n=== results ===")
    print(f"  planned iterations: {iterations}")
    print(f"  actual iterations : {actual}")
    print(f"  wifi connect      : {wifi_ok}/{actual} = {wifi_rate:.2f}%  "
          f"{'PASS' if wifi_passed else 'FAIL'} (>= {wifi_rate_threshold:.0f}%)")
    print(f"  network ping      : {network_ok}/{actual} = {network_rate:.2f}%  "
          f"{'PASS' if network_passed else 'FAIL'} (>= {network_rate_threshold:.0f}%)")
    print(f"  scan count        : {count_ok}/{actual} = {count_rate:.2f}%  "
          f"{'PASS' if count_passed else 'FAIL'} (>= {count_rate_threshold:.0f}%)")
    print(f"  count abnormal    : {count_abnormal}")
    if count_details:
        print("  abnormal details  :")
        for d in count_details:
            print(f"    attempt {d['attempt']}: {d['current_count']} vs {d['previous_count']} "
                  f"(ratio {d['ratio']:.2f})")
    print(f"  OVERALL           : {'PASS' if overall_passed else 'FAIL'}")

    if not interrupted:
        try:
            report_path = save_report(
                args.device, iterations, actual, current_ssid,
                wifi_ok, network_ok, count_ok, count_abnormal, count_details,
                wifi_rate, network_rate, count_rate,
            )
            print(f"  report            : {report_path}")
        except Exception as e:
            print(f"[warn] failed to save report: {e}")

    return 0 if (actual > 0 and overall_passed) else 1


if __name__ == "__main__":
    sys.exit(main())
