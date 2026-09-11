"""
app_launch_stress.py - PPTP: app cold/hot launch performance stress test.

Measures how long the app(s) under test take to reach their first frame, in one
of two modes (frontend-selectable):

  cold : `am force-stop` the app first (guarantees no background process), then
         launch -> measures true cold-start time.
  hot  : app process already exists in the background (we HOME it first), then
         launch again -> measures bring-to-foreground (hot-start) time.

"要跑就三个一起跑" (per user): ALL presets in the hard-coded APP_PRESETS list
below are tested in sequence, each for `iterations` rounds. Each app gets its
own report + PASS/FAIL verdict; the overall run passes only if every app passes.

Per round two timings are captured and cross-checked:
  - `am start -W` -> TotalTime (primary metric, ms)
  - logcat `Displayed <activity>: +xxxms` (system tag; often ABSENT on pure hot
    starts because the window is not re-created)
Android 12+ also prints `LaunchState: COLD/WARM/HOT` in `am start -W` output,
which we validate against the mode so a "cold" round that accidentally reused a
live process (or a "hot" round with a dead process) is FAILED (not just noted).

Activity note: the `pkg/.Activity` shorthand only works when the activity lives
inside the package namespace. Prime Video's IgnitionActivity and YouTube TV's
launcher (ShellActivity) do NOT, so APP_PRESETS always stores the FULL activity
class name and the script builds `pkg/full.Class` itself.

Contract (matches other PPTP scripts):
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional; see PARAMS below)

Run standalone:
    python scripts/app_launch_stress.py --device <serial>
    python scripts/app_launch_stress.py --device <serial> \
        --params '{"mode": "hot", "iterations": 50}'

The apps under test are HARD-CODED in APP_PRESETS below (per user decision,
NOT a frontend param).
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

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a summary line.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# ---------------------------------------------------------------------------
# App(s) under test — HARD-CODED (per user, NOT a frontend param).
# "要跑就三个一起跑": every entry below is tested in sequence.
#   label   : short name used in report filenames
#   package : app package name
#   activity: FULL activity class name (NO leading dot - `pkg/.Activity` shorthand
#             breaks for activities outside the package namespace). Leave "" to
#             auto-resolve via `cmd package resolve-activity --brief`.
# ---------------------------------------------------------------------------
APP_PRESETS = [
    {"label": "netflix",
     "package": "com.netflix.ninja",
     "activity": "com.netflix.ninja.MainActivity"},
    {"label": "primevideo",
     "package": "com.amazon.amazonvideo.livingroom",
     # IgnitionActivity resolves to the app's real MainActivity on launch
     "activity": "com.amazon.ignition.IgnitionActivity"},
    {"label": "youtube",
     "package": "com.google.android.youtube.tv",
     # MainActivity is NOT exported; ShellActivity is the launchable entry
     "activity": "com.google.android.apps.youtube.tv.activity.ShellActivity"},
]

# Frontend-configurable params (declared for the PPTP platform).
# The platform renders a config modal from this list and passes the chosen
# values back via `--params`. Field keys: name / label / type / default /
# min / max / choices. Run with `--dump-params` to print this schema as JSON.
PARAMS = [
    {"name": "mode", "label": "启动方式", "type": "select",
     "choices": ["cold", "hot"], "default": "cold"},
    {"name": "iterations", "label": "每 APP 循环次数", "type": "int",
     "default": 100, "min": 1, "max": 100000},
    {"name": "settle_sec", "label": "启动后停留(秒)", "type": "int",
     "default": 3, "min": 0, "max": 3600},
    {"name": "gap_sec", "label": "启动前等待(秒)", "type": "int",
     "default": 1, "min": 0, "max": 3600},
    {"name": "launch_timeout", "label": "启动超时(秒)", "type": "int",
     "default": 20, "min": 5, "max": 600},
    {"name": "p95_threshold_ms", "label": "p95 阈值(ms, 0=不判定)", "type": "int",
     "default": 2000, "min": 0, "max": 600000},
]

# Cross-check tolerance between am start TotalTime and logcat Displayed (ms).
DISPLAYED_TOLERANCE_MS = 300


def adb_shell(serial: str, *args: str, timeout: int = 10) -> tuple[int, str, str]:
    """Run `adb -s <serial> shell <args...>`.

    Never raises. Returns (returncode, stdout, stderr).
    Negative return codes are reserved for infrastructure errors:
      -1 = subprocess timeout, -2 = adb executable not found.
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


def process_pids(serial: str, pkg: str) -> list[str]:
    """PIDs of the app process (empty list = not running)."""
    rc, out, _ = adb_shell(serial, "pidof", pkg, timeout=5)
    if rc != 0 or not out:
        return []
    return [p.strip() for p in out.split() if p.strip().isdigit()]


def force_stop(serial: str, pkg: str) -> None:
    adb_shell(serial, "am", "force-stop", pkg, timeout=10)


def press_home(serial: str) -> None:
    adb_shell(serial, "input", "keyevent", "KEYCODE_HOME", timeout=5)


def wake_screen(serial: str) -> None:
    adb_shell(serial, "input", "keyevent", "KEYCODE_WAKEUP", timeout=5)
    adb_shell(serial, "svc", "power", "stayon", "true", timeout=5)


def clear_logcat(serial: str) -> None:
    adb_shell(serial, "logcat", "-c", timeout=5)


def resolve_activity(serial: str, pkg: str) -> str | None:
    """Resolve the launchable activity component, e.g. "pkg/.MainActivity"."""
    for args in (("cmd", "package", "resolve-activity", "--brief", pkg),
                 ("pm", "resolve-activity", "--brief", pkg)):
        rc, out, _ = adb_shell(serial, *args, timeout=10)
        if rc == 0 and out:
            line = out.strip().splitlines()[0].strip()
            if line and "/" in line:
                return line
    return None


# ---------------- parsing ----------------
def _parse_ms_int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return -1


def parse_am_start(out: str) -> dict:
    """Parse `am start -W` summary lines into a dict."""
    res = {"status": "", "launch_state": "", "this_time": -1,
           "total_time": -1, "wait_time": -1, "complete": False}
    if not out:
        return res
    for line in out.splitlines():
        low = line.lower()
        if low.startswith("status:"):
            res["status"] = line.split(":", 1)[1].strip()
        elif low.startswith("launchstate:"):
            # e.g. "COLD [STOPPED]" -> take the first token before any bracket
            val = line.split(":", 1)[1].strip()
            res["launch_state"] = val.split()[0].upper() if val else ""
        elif low.startswith("thistime:"):
            res["this_time"] = _parse_ms_int(line.split(":", 1)[1])
        elif low.startswith("totaltime:"):
            res["total_time"] = _parse_ms_int(line.split(":", 1)[1])
        elif low.startswith("waittime:"):
            res["wait_time"] = _parse_ms_int(line.split(":", 1)[1])
        elif low.startswith("complete"):
            res["complete"] = True
    return res


DISPLAYED_RE = re.compile(r"Displayed .*?: \+(\d+)(?:s(\d+))?ms", re.I)


def parse_displayed_ms(out: str) -> int | None:
    """Parse the LAST `Displayed <act>: +1s234ms` line into ms, or None.

    Handles both forms: "+592ms" -> 592 and "+1s291ms" -> 1291.
    """
    if not out:
        return None
    best = None
    for line in out.splitlines():
        m = DISPLAYED_RE.search(line)
        if m:
            if m.group(2) is not None:          # +1s291ms
                best = int(m.group(1)) * 1000 + int(m.group(2))
            else:                               # +592ms
                best = int(m.group(1))
    return best


def get_displayed_ms(serial: str) -> int | None:
    """Best-effort read of the `Displayed` tag from logcat since last clear."""
    rc, out, _ = adb_shell(serial, "logcat", "-d", "-s", "ActivityTaskManager",
                           timeout=10)
    if rc != 0 or not out:
        rc, out, _ = adb_shell(serial, "logcat", "-d", "-s", "ActivityManager",
                               timeout=10)
    return parse_displayed_ms(out)


def launch_app(serial: str, component: str, timeout: int) -> dict:
    """Run `am start -W -n <component>`; return the parsed summary dict."""
    rc, out, err = adb_shell(serial, "am", "start", "-W", "-n", component,
                             timeout=timeout)
    if rc == 0 and out:
        return parse_am_start(out)
    return parse_am_start(out + ("\n" if out else "") + err)


# ---------------- sample / stats / report ----------------
def _failed_sample(attempt: int, note: str) -> dict:
    return {"attempt": attempt, "ok": False, "total_ms": -1, "wait_ms": -1,
            "launch_state": "", "displayed_ms": None, "note": note}


def finalize_sample(attempt: int, r: dict, displayed: int | None,
                    mode: str) -> tuple[dict, bool]:
    """Turn an `am start -W` result into (sample, ok).

    Hard-validates (fails the round, not just a note):
      - Status must be "ok" and TotalTime > 0 (a 0 TotalTime means the intent
        was just delivered to an already top-most instance - not a real launch)
      - LaunchState must match the mode when the system actually reports a
        real value (COLD / WARM / HOT). "UNKNOWN"/empty means the system gave
        no signal -> the process-level checks (force-stop / pidof) carry it.
    """
    ok = r["status"] == "ok" and r["total_time"] > 0
    note = ""
    if r["status"] and r["status"] != "ok":
        ok = False
        note = f"status={r['status']}"
    elif r["total_time"] <= 0:
        ok = False
        note = "no total_time"
    elif r["launch_state"] in ("COLD", "WARM", "HOT"):
        if mode == "cold" and r["launch_state"] != "COLD":
            ok = False
            note = (f"launch_state={r['launch_state']} (expected COLD) "
                    f"- not a real cold start")
        elif mode == "hot" and r["launch_state"] not in ("HOT", "WARM"):
            ok = False
            note = (f"launch_state={r['launch_state']} (expected HOT/WARM) "
                    f"- not a real hot start")
    elif r["launch_state"]:
        note = f"launch_state={r['launch_state']} (no semantic check)"
    if not ok and not note:
        note = "no total_time"
    return {
        "attempt": attempt, "ok": ok, "total_ms": r["total_time"],
        "wait_ms": r["wait_time"], "launch_state": r["launch_state"],
        "displayed_ms": displayed, "note": note,
    }, ok


def percentiles(values: list[int], ps: tuple[int, ...] = (50, 90, 95, 99)) -> dict:
    """Nearest-rank percentiles over a list of ints."""
    if not values:
        return {}
    vs = sorted(values)
    n = len(vs)
    res = {}
    for p in ps:
        idx = max(1, math.ceil(p / 100.0 * n)) - 1
        res[p] = vs[idx]
    return res


def save_report(device: str, mode: str, label: str, pkg: str, component: str,
                planned: int, completed: int, stopped_early: bool,
                failed_launches: int, samples: list[dict],
                stats: dict, p95_threshold_ms: int, passed: bool) -> str:
    """Write the JSON report under reports/stress-test/app-launch/. Returns path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test",
                              "app-launch")
    os.makedirs(report_dir, exist_ok=True)

    data = {
        "test_name": f"APP {'冷' if mode == 'cold' else '热'}启动压测 - {label}",
        "device_id": device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_label": label,
        "app_package": pkg,
        "app_activity": component,
        "mode": mode,
        "iterations_planned": planned,
        "iterations_completed": completed,
        "stopped_early": stopped_early,
        "failed_launches": failed_launches,
        "p95_threshold_ms": p95_threshold_ms,
        "stats_ms": stats,
        "passed": bool(passed),
        "displayed_tolerance_ms": DISPLAYED_TOLERANCE_MS,
        "samples": samples,
    }
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir,
                             f"app_launch_{mode}_{label}_{dev_short}_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return json_path


def run_one_app(serial: str, preset: dict, mode: str, iterations: int,
                settle_sec: int, gap_sec: int, launch_timeout: int,
                component: str) -> tuple[list[dict], int, bool]:
    """Cold/hot loop for ONE app. Returns (samples, failed_launches, stopped)."""
    pkg = preset["package"]
    label = preset["label"]
    samples: list[dict] = []
    failed_launches = 0
    stopped = False

    # Hot mode pre-flight: the app must have a live background process before
    # we can measure a hot start. Launch it once if it is not running.
    if mode == "hot" and not process_pids(serial, pkg):
        print("[setup] app process not running - launching once to establish "
              "a background process (not sampled)")
        launch_app(serial, component, launch_timeout)
        time.sleep(settle_sec)

    try:
        for i in range(1, iterations + 1):
            print(f"  --- {label} iteration {i}/{iterations} ---")

            if mode == "cold":
                # 1. force-stop + verify the process is really gone
                force_stop(serial, pkg)
                if process_pids(serial, pkg):
                    time.sleep(0.5)
                    force_stop(serial, pkg)
                if process_pids(serial, pkg):
                    print(f"  [{label}#{i}] WARN: process still alive "
                          f"after force-stop")
                    failed_launches += 1
                    samples.append(_failed_sample(
                        i, "force-stop left process alive"))
                    if gap_sec > 0:
                        time.sleep(gap_sec)
                    continue
                if gap_sec > 0:
                    time.sleep(gap_sec)
                clear_logcat(serial)
                r = launch_app(serial, component, launch_timeout)
                displayed = get_displayed_ms(serial)
                sample, ok = finalize_sample(i, r, displayed, mode)
                samples.append(sample)
                if not ok:
                    failed_launches += 1
                print(f"  [{label}#{i}] cold start: total={r['total_time']}ms "
                      f"wait={r['wait_time']}ms "
                      f"launch_state={r['launch_state'] or 'n/a'} "
                      f"displayed={displayed if displayed is not None else 'n/a'} "
                      f"-> {'OK' if ok else 'NG'}"
                      + (f" ({sample['note']})" if sample["note"] else ""))
                if settle_sec > 0:
                    time.sleep(settle_sec)

            else:  # hot
                # 1. the app must have a live process; if it died, cold-launch
                #    it once to re-establish (that launch is not a sample).
                if not process_pids(serial, pkg):
                    print(f"  [{label}#{i}] WARN: process gone - cold-launching "
                          f"to re-establish (not sampled)")
                    launch_app(serial, component, launch_timeout)
                    time.sleep(settle_sec)
                # 2. HOME it to the background, then relaunch
                press_home(serial)
                if gap_sec > 0:
                    time.sleep(gap_sec)
                clear_logcat(serial)
                r = launch_app(serial, component, launch_timeout)
                displayed = get_displayed_ms(serial)
                sample, ok = finalize_sample(i, r, displayed, mode)
                samples.append(sample)
                if not ok:
                    failed_launches += 1
                print(f"  [{label}#{i}] hot start: total={r['total_time']}ms "
                      f"wait={r['wait_time']}ms "
                      f"launch_state={r['launch_state'] or 'n/a'} "
                      f"displayed={displayed if displayed is not None else 'n/a'} "
                      f"-> {'OK' if ok else 'NG'}"
                      + (f" ({sample['note']})" if sample["note"] else ""))
                if settle_sec > 0:
                    time.sleep(settle_sec)
    except KeyboardInterrupt:
        stopped = True
        print(f"  [interrupt] app {label} stopped "
              f"after {len(samples)} completed iterations")

    return samples, failed_launches, stopped


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
        description="App cold/hot launch performance stress test")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"mode"?, "iterations"?, "settle_sec"?, '
                        '"gap_sec"?, "launch_timeout"?, "p95_threshold_ms"?}')
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    mode = str(params.get("mode", defaults["mode"]))
    if mode not in ("cold", "hot"):
        print(f"[warn] unknown mode '{mode}' - falling back to 'cold'")
        mode = "cold"
    iterations = int(params.get("iterations", defaults["iterations"]))
    settle_sec = int(params.get("settle_sec", defaults["settle_sec"]))
    gap_sec = int(params.get("gap_sec", defaults["gap_sec"]))
    launch_timeout = int(params.get("launch_timeout", defaults["launch_timeout"]))
    p95_threshold_ms = int(params.get("p95_threshold_ms",
                                      defaults["p95_threshold_ms"]))

    print(f"[config] device         = {args.device}")
    print(f"[config] mode           = {mode}")
    print(f"[config] apps           = "
          + ", ".join(f"{x['label']} ({x['package']})" for x in APP_PRESETS))
    print(f"[config] iterations     = {iterations} per app")
    print(f"[config] settle         = {settle_sec}s (after each launch)")
    print(f"[config] gap            = {gap_sec}s (before each launch)")
    print(f"[config] launch_timeout = {launch_timeout}s")
    print(f"[config] p95_threshold  = {p95_threshold_ms}ms "
          f"{'(disabled)' if p95_threshold_ms <= 0 else ''}")

    if not check_adb_connected(args.device):
        print("[error] device not reachable via adb")
        return 1

    # Screen must be on for a meaningful launch measurement.
    wake_screen(args.device)

    # Resolve launch components upfront (fail-fast per app, continue others).
    # component = "<pkg>/<full.Class>" - always fully qualified, never the
    # `pkg/.Activity` shorthand (breaks for activities outside the namespace).
    resolved: list[tuple[dict, str | None]] = []
    for preset in APP_PRESETS:
        if preset.get("activity"):
            comp = f"{preset['package']}/{preset['activity']}"
        else:
            comp = resolve_activity(args.device, preset["package"])
        if not comp:
            print(f"[error] cannot resolve launch activity for "
                  f"{preset['package']} - skipping this app")
        resolved.append((preset, comp))

    results = []
    any_stopped = False
    try:
        for preset, comp in resolved:
            if comp is None:
                results.append({"label": preset["label"],
                                "package": preset["package"],
                                "samples": None, "failed": 0, "stopped": False,
                                "stats": {}, "p95": None, "passed": False})
                continue

            print(f"\n========== app: {preset['label']} ({preset['package']}) "
                  f"-> {comp} ==========")
            samples, failed, stopped = run_one_app(
                args.device, preset, mode, iterations, settle_sec, gap_sec,
                launch_timeout, comp)
            if stopped:
                any_stopped = True

            valid = [s["total_ms"] for s in samples
                     if s["ok"] and s["total_ms"] >= 0]
            stats = percentiles(valid)
            p95 = stats.get(95)
            passed = (len(valid) > 0
                      and (p95_threshold_ms <= 0 or (p95 is not None
                                                     and p95 <= p95_threshold_ms)))

            both = [s for s in samples if s["ok"]
                    and s["displayed_ms"] is not None and s["total_ms"] >= 0]
            close = [s for s in both
                     if abs(s["total_ms"] - s["displayed_ms"])
                     <= DISPLAYED_TOLERANCE_MS]

            print(f"  === results ({mode}) ===")
            if stopped:
                print(f"  stopped early: planned {iterations}, "
                      f"completed {len(samples)}")
            print(f"  valid samples   : {len(valid)}")
            print(f"  failed launches : {failed}")
            if stats:
                print(f"  min / avg / max : {min(valid)} / "
                      f"{sum(valid) / len(valid):.1f} / {max(valid)} ms")
            print(f"  p50 / p90 / p95 : {stats.get(50)} / {stats.get(90)} / "
                  f"{stats.get(95)} ms  (p99 {stats.get(99)})")
            if both:
                print(f"  displayed x-check: {len(close)}/{len(both)} within "
                      f"{DISPLAYED_TOLERANCE_MS}ms of TotalTime")
            elif mode == "hot":
                print("  displayed x-check: n/a (Displayed usually absent on "
                      "hot start; semantic check = launch_state)")
            if p95_threshold_ms > 0:
                print(f"  OVERALL         : {'PASS' if passed else 'FAIL'} "
                      f"(p95 {p95}ms <= {p95_threshold_ms}ms)")
            for s in samples:
                if s["note"]:
                    print(f"  note #{s['attempt']}: {s['note']}")

            try:
                report_path = save_report(
                    args.device, mode, preset["label"], preset["package"], comp,
                    iterations, len(samples), stopped, failed, samples, stats,
                    p95_threshold_ms, passed)
                print(f"  report          : {report_path}")
            except Exception as e:
                print(f"[warn] failed to save report: {e}")

            results.append({"label": preset["label"],
                            "package": preset["package"],
                            "samples": samples, "failed": failed,
                            "stopped": stopped, "stats": stats,
                            "p95": p95, "passed": passed})
            if stopped:
                break  # user interrupted -> don't start the remaining apps
    except KeyboardInterrupt:
        # interrupt landed in the tiny window between two apps (run_one_app
        # already handles interrupts inside an app) - treat as a stop signal.
        any_stopped = True
        print("\n[interrupt] stopped between apps - "
              "summarizing completed apps")

    # Overall verdict across all apps.
    print(f"\n=== overall ({mode}) ===")
    for r in results:
        if r["samples"] is None:
            print(f"  {r['label']:<11} SKIP (no launch activity)")
        else:
            status = "PASS" if r["passed"] else "FAIL"
            print(f"  {r['label']:<11} {status:<5} p95={r['p95']}ms "
                  f"valid={len([1 for s in r['samples'] if s['ok'] and s['total_ms'] >= 0])}")
    if any_stopped:
        print("  stopped early (interrupt) - no overall verdict")
        return 0

    valid_counts = [len([s for s in r["samples"] if s and s["ok"]
                         and s["total_ms"] >= 0]) for r in results
                    if r["samples"] is not None]
    if not valid_counts or all(v == 0 for v in valid_counts):
        print("  overall         : FAIL (no valid samples)")
        return 1
    # Any skipped (unresolved) app fails the run too - user asked for all three.
    overall_pass = all(r["passed"] for r in results)
    print(f"  overall         : {'PASS' if overall_pass else 'FAIL'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
