"""
bt_reboot_stress.py - PPTP demo: reboot + Bluetooth speaker reconnect stress test.

Per iteration (default 100):
  1. Send `adb reboot` to device
  2. Wait the configured number of seconds
  3. Wait for device to come back online
  4. Poll until a Bluetooth A2DP (audio) device reconnects
  5. PASS if a Bluetooth speaker is connected, FAIL if not

At the end: print total / passed / success rate.

Contract (matches other PPTP scripts):
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional: iterations, wait_sec, bt_reconnect_timeout, back_online_timeout)

Run standalone:
    python scripts/bt_reboot_stress.py --device <serial>
    python scripts/bt_reboot_stress.py --device <serial> \
        --params '{"iterations": 3, "wait_sec": 90}'

How the "speaker reconnected" check works:
  - Reads `dumpsys bluetooth_manager`.
  - Requires the adapter to be ON (`enabled: true`) AND at least one A2DP
    state machine to be in `mConnectionState: CONNECTED`. A plain
    "bluetooth is on" check is not enough: it only proves the adapter
    state, not that the paired speaker actually reconnected.
  - Verified on EcoPro (MT9676 / Android TV): after boot the adapter
    auto-enables (SYSTEM_BOOT) and the A2DP audio device reconnects within
    a few seconds, reported as `A2dpStateMachine ... (Active)` with
    `mConnectionState: CONNECTED`.

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

# Frontend-configurable params (declared for the PPTP platform).
# The platform renders a config modal from this list and passes the chosen
# values back via `--params`. Field keys: name / label / type / default /
# min / max. Run with `--dump-params` to print this schema as JSON.
# NOTE: labels stay English (ASCII) to keep the code file free of CJK,
# matching battery_inout_stress.py (v2.5.0 language decision).
PARAMS = [
    {"name": "iterations", "label": "Iterations", "type": "int",
     "default": 100, "min": 1, "max": 100000},
    {"name": "wait_sec", "label": "Wait after reboot (s)", "type": "int",
     "default": 60, "min": 1, "max": 3600},
    {"name": "bt_reconnect_timeout", "label": "BT reconnect timeout (s)", "type": "int",
     "default": 90, "min": 5, "max": 3600},
    {"name": "back_online_timeout", "label": "Device online timeout (s)", "type": "int",
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


def check_bt_connected(serial: str) -> bool:
    """Return True if the device's Bluetooth adapter is ON and an A2DP
    (audio) device is connected.
    """
    rc, out, _ = adb_shell(serial, "dumpsys", "bluetooth_manager", timeout=15)
    if rc != 0 or not out:
        return False
    low = out.lower()
    # 1) Adapter must actually be ON (bluetooth enabled)
    if "enabled: true" not in low:
        return False
    # 2) Some A2DP audio device must be in CONNECTED state (speaker reconnected)
    return "mconnectionstate: connected" in low


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


def wait_for_bt_reconnect(serial: str, timeout_sec: int) -> bool:
    """Poll Bluetooth A2DP connection until the speaker reconnects.

    Reconnect time after boot varies (adapter enable -> bond restore ->
    A2DP profile connect), so poll instead of a single fixed settle.
    """
    print(f"[bt] waiting for bluetooth speaker to reconnect "
          f"(timeout {timeout_sec}s)...")
    deadline = time.time() + timeout_sec
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if check_bt_connected(serial):
            print(f"[bt] speaker reconnected after "
                  f"{int(time.time() - (deadline - timeout_sec))}s "
                  f"({attempt} polls)")
            return True
        time.sleep(5)
    print(f"[bt] TIMEOUT after {timeout_sec}s - no A2DP device connected")
    return False


def main() -> int:
    # --dump-params is consumed by the PPTP platform to render the params
    # config modal. Must short-circuit before argparse (and before any device
    # interaction). ensure_ascii keeps stdout pure-ASCII for safe piping.
    if "--dump-params" in sys.argv:
        print(json.dumps({"fields": PARAMS}))
        return 0

    defaults = {f["name"]: f["default"] for f in PARAMS}

    p = argparse.ArgumentParser(description="Reboot + Bluetooth speaker reconnect stress test")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"iterations"?, "wait_sec"?, "bt_reconnect_timeout"?, "back_online_timeout"?}')
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    iterations = int(params.get("iterations", defaults["iterations"]))
    wait_sec = int(params.get("wait_sec", defaults["wait_sec"]))
    bt_reconnect_timeout = int(params.get("bt_reconnect_timeout", defaults["bt_reconnect_timeout"]))
    back_online_timeout = int(params.get("back_online_timeout", defaults["back_online_timeout"]))

    print(f"[config] device             = {args.device}")
    print(f"[config] iterations         = {iterations}")
    print(f"[config] wait_sec           = {wait_sec} (after reboot, before checking back)")
    print(f"[config] bt_reconnect       = {bt_reconnect_timeout}s (polling speaker reconnect)")
    print(f"[config] online_timeout     = {back_online_timeout}s (waiting for device back)")

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

            # 4. Poll for the bluetooth speaker to reconnect
            if wait_for_bt_reconnect(args.device, bt_reconnect_timeout):
                print(f"[{i}] PASS: bluetooth speaker reconnected")
                results.append(True)
            else:
                print(f"[{i}] FAIL: bluetooth speaker did not reconnect")
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
