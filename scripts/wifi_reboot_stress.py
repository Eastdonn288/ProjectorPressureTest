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
import signal
import subprocess
import sys
import time

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a summary line.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)


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


def main() -> int:
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

    iterations = int(params.get("iterations", 100))
    wait_sec = int(params.get("wait_sec", 60))
    wifi_settle_sec = int(params.get("wifi_settle_sec", 5))
    back_online_timeout = int(params.get("back_online_timeout", 90))

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
        # Return 0 if all passed, non-zero otherwise (CI-friendly)
        return 0 if (total > 0 and passed == total) else 1


if __name__ == "__main__":
    sys.exit(main())