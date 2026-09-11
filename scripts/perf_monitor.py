"""
perf_monitor.py - PPTP: real-time device performance monitor (CPU / GPU / mem + foreground app).

Streams periodic samples to the PPTP frontend for a live ECharts graph. Each
sample is emitted as one structured stdout line (PERF wire format below); the
platform's WebSocket relays stdout unchanged, so the server needs zero changes.
The chart / export logic lives entirely in the frontend.

PERF wire format (one line per record, prefix "PERF|", JSON ascii-safe):
  PERF|{"type":"meta","sources":{"cpu":"/proc/stat","mem":"/proc/meminfo",
        "gpu":"/sys/kernel/debug/mali0/dvfs_utilization"|null,"fg":"top -n 1 -b"},
        "device":"MT9676"}
  PERF|{"type":"sample","clock":1756107600000,"t":12.3,"cpu":23.1,
        "gpu":0.8,"mem":48.2,"fg_cpu":5.1,"fg_pkg":"com.netflix.ninja",
        "gpu_clk":552}
  clock = wall-clock epoch ms at sample time (real time x-axis);
  t = seconds since monitor start (spacing). Missing metrics are null;
  the frontend auto-hides that series.

Data sources (probed on the actual MT9676 device, 2026-08-25):
  cpu     : /proc/stat aggregate `cpu` line (cumulative counters, no root)
            -> per-interval busy% via delta (first sample is null, no baseline)
  mem     : /proc/meminfo MemAvailable / MemTotal (no root) -> (1-avail/total)*100
  gpu     : /sys/kernel/debug/mali0/dvfs_utilization
            `busy_time: X idle_time: Y` (cumulative, REQUIRES root; `su 0 cat`)
            -> busy/(busy+idle) via delta
  gpu_clk : /sys/kernel/debug/mali0/gpu_clock (MHz, root) - bonus field
  fg      : `dumpsys window | grep mFocusedApp` (front package) + `top -n 1 -b`
            per-process %CPU of that package (instantaneous). NOT `dumpsys
            cpuinfo`, which on this device is a ~5 min rolling average.

NOTE: the "standard" MTK/Mali sysfs GPU nodes (/sys/module/ged/parameters/*,
/sys/kernel/ged/hal/*, /sys/devices/platform/*mali*/utilization) DO NOT exist
on this board; the Mali counters live under debugfs `mali0` and need root. If
the GPU node is unreadable the script keeps running with gpu=null (and the
meta line reports gpu=null so the chart hides that series). Use `--probe` to
check node readability.

Contract (matches other PPTP scripts):
  --device <serial>  (required, injected by PPTP platform)
  --params <json>    (optional; see PARAMS below)
  --probe            (standalone: test node readability, then exit)

Run standalone:
    python scripts/perf_monitor.py --device <serial> \
        --params '{"interval_sec": 2, "duration_sec": 30, "track_foreground": true}'
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

# Frontend-configurable params (declared for the PPTP platform).
# The platform renders a config modal from this list and passes the chosen
# values back via `--params`. Field keys: name / label / type / default /
# min / max / choices. Run with `--dump-params` to print this schema as JSON.
PARAMS = [
    {"name": "interval_sec", "label": "采样间隔(秒)", "type": "float",
     "default": 2.0, "min": 0.5, "max": 60},
    {"name": "duration_sec", "label": "总时长(秒, 0=直到手动停止)", "type": "int",
     "default": 0, "min": 0, "max": 86400},
    {"name": "track_foreground", "label": "采集前台应用 CPU/包名", "type": "bool",
     "default": True},
]

# GPU counter reads. On this MT9676 board the Mali DVFS counters live under
# debugfs `mali0` and require root (`su 0 cat` verified working; `su -c` is
# rejected on this device). `timeout 5` guards against an su that waits.
GPU_DVFS_READ = "timeout 5 su 0 cat /sys/kernel/debug/mali0/dvfs_utilization"
GPU_CLK_READ = "timeout 5 su 0 cat /sys/kernel/debug/mali0/gpu_clock"

# Error markers reused for read-failure detection (mirrors sensor script).
ERROR_MARKERS = ("permission denied", "not found", "no such file",
                 "operation not permitted", "denied", "error",
                 "invalid option", "unknown option", "usage",
                 "not a terminal", "not an interactive")


# ---------------------------------------------------------------------------
# ADB layer
# ---------------------------------------------------------------------------
def adb_capture(args: list[str], timeout: int | None = None) -> str:
    """Run a subprocess, return stdout ("" on any failure). Never raises."""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def adb_shell(serial: str, command: str, timeout: int | None = None) -> str:
    """Run `adb -s <serial> shell <command>` (single-arg command). Returns stdout."""
    # list-arg (NOT shell=True): the compound command contains `;` `|` `>` which
    # must reach the DEVICE shell, not be interpreted by the PC cmd.exe.
    return adb_capture(["adb", "-s", serial, "shell", command], timeout=timeout)


def check_adb_connected(serial: str) -> bool:
    """True if `adb shell echo 1` round-trips correctly."""
    return adb_shell(serial, "echo 1", timeout=5).strip() == "1"


def getprop(serial: str, key: str) -> str:
    return adb_shell(serial, f"getprop {key}", timeout=5).strip()


def build_sample_command(track_fg: bool, last_pkg: str | None = None) -> str:
    """One `adb shell` compound command; all reads happen near-simultaneously."""
    parts = [
        "echo @@STAT",
        "cat /proc/stat",
        "echo @@MEM",
        "cat /proc/meminfo",
        "echo @@GPU",
        GPU_DVFS_READ,
        "echo @@CLK",
        GPU_CLK_READ,
    ]
    if track_fg:
        parts += [
            "echo @@FOCUS",
            "dumpsys window 2>/dev/null | grep mFocusedApp",
            "echo @@TOP",
            # Grep for the known package so we get its row regardless of how the
            # ~400-process list is CPU-sorted (a low-CPU foreground app can drop
            # below a fixed `head -N` cut). First sample has no package yet ->
            # grab a wider head and rely on parse_top_fg_cpu matching.
            (f"top -n 1 -b 2>/dev/null | grep {last_pkg} | head -3"
             if last_pkg else "top -n 1 -b 2>/dev/null | head -40"),
        ]
    return "; ".join(parts)


def split_sections(out: str) -> dict[str, str]:
    """Split compound output on `@@MARKER` lines into {MARKER: text}."""
    sections: dict[str, str] = {}
    cur: str | None = None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            sections[cur] = ""
        elif cur is not None:
            sections[cur] += line + "\n"
    return sections


# ---------------------------------------------------------------------------
# Parsers (per section)
# ---------------------------------------------------------------------------
def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """Parse aggregate `cpu` line -> (total, idle), or None."""
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        if len(parts) < 8:
            return None
        try:
            vals = [int(p) for p in parts[1:8]]
        except ValueError:
            return None
        idle = vals[3] + vals[4]          # idle + iowait
        return sum(vals), idle
    return None


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """Parse MemTotal / MemAvailable (kB) -> (total, available), or None."""
    total = avail = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            m = re.search(r":\s*(\d+)", line)
            total = int(m.group(1)) if m else None
        elif line.startswith("MemAvailable:"):
            m = re.search(r":\s*(\d+)", line)
            avail = int(m.group(1)) if m else None
    if total and avail:
        return total, avail
    return None


def parse_gpu_dvfs(text: str) -> tuple[int, int] | None:
    """Parse `busy_time: X idle_time: Y` -> (busy, idle), or None."""
    m = re.search(r"busy_time:\s*(\d+)\s+idle_time:\s*(\d+)", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_gpu_clk(text: str) -> int | None:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def parse_focus_pkg(text: str) -> str | None:
    """Parse front package from `mFocusedApp=ActivityRecord{... u0 pkg/Act tN}`."""
    m = re.search(r"mFocusedApp=.*?\bu0\s+([^\s/]+)/", text)
    return m.group(1) if m else None


def parse_top_fg_cpu(text: str, pkg: str | None) -> float | None:
    """%CPU of the foreground package's row in `top -n 1 -b` (instantaneous).

    Column layout (toybox): PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS
    """
    if not pkg:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 11:
            continue
        if parts[-1] == pkg or parts[-1].startswith(pkg + ":"):
            try:
                return float(parts[8])
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
def build_sample(secs: dict, cpu_baseline: tuple | None, gpu_baseline: tuple | None,
                 t_elapsed: float, track_fg: bool) -> dict:
    """Turn one sample's sections into a sample dict (+ updated baselines)."""
    cpu = None
    new_cpu = cpu_baseline
    st = parse_proc_stat(secs.get("STAT", ""))
    if st:
        new_cpu = st
        if cpu_baseline and st[1] > cpu_baseline[1]:     # idle went forward
            d_total = st[0] - cpu_baseline[0]
            d_idle = st[1] - cpu_baseline[1]
            if d_total > 0:
                cpu = round((1.0 - d_idle / d_total) * 100, 1)

    mem = None
    mt = parse_meminfo(secs.get("MEM", ""))
    if mt and mt[0] > 0:
        mem = round((1.0 - mt[1] / mt[0]) * 100, 1)

    gpu = None
    new_gpu = gpu_baseline
    gd = parse_gpu_dvfs(secs.get("GPU", ""))
    if gd:
        new_gpu = gd
        if gpu_baseline and gd[1] > gpu_baseline[1]:
            d_busy = gd[0] - gpu_baseline[0]
            d_idle = gd[1] - gpu_baseline[1]
            denom = d_busy + d_idle
            if denom > 0:
                gpu = round(d_busy / denom * 100, 1)

    gpu_clk = parse_gpu_clk(secs.get("CLK", ""))
    fg_pkg = parse_focus_pkg(secs.get("FOCUS", "")) if track_fg else None
    fg_cpu = (parse_top_fg_cpu(secs.get("TOP", ""), fg_pkg)
              if track_fg and fg_pkg else None)

    return {"t": round(t_elapsed, 1), "cpu": cpu, "gpu": gpu, "mem": mem,
            "fg_cpu": fg_cpu, "fg_pkg": fg_pkg, "gpu_clk": gpu_clk,
            "_cpu_prev": new_cpu, "_gpu_prev": new_gpu}


def _gpu_readable(serial: str) -> bool:
    """True if the GPU dvfs counter is readable (root su) on this device."""
    return parse_gpu_dvfs(adb_shell(serial, GPU_DVFS_READ, timeout=10)) is not None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _summary(values: list[float]) -> dict:
    if not values:
        return {}
    vals = sorted(values)
    n = len(vals)
    def pct(p: float) -> float:
        return vals[max(1, math.ceil(p / 100.0 * n)) - 1]
    return {"min": round(min(values), 1),
            "avg": round(sum(values) / len(values), 1),
            "max": round(max(values), 1),
            "p50": round(pct(50), 1), "p90": round(pct(90), 1),
            "p95": round(pct(95), 1)}


def save_report(device: str, cfg: dict, sources: dict, stopped_early: bool,
                samples: list[dict]) -> str:
    """Write the JSON report under reports/stress-test/perf/. Returns path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test", "perf")
    os.makedirs(report_dir, exist_ok=True)
    data = {
        "test_name": "性能监控 (CPU/GPU/内存/前台APP)",
        "device_id": device,
        "test_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg,
        "sources": sources,
        "stopped_early": bool(stopped_early),
        "sample_count": len(samples),
        "summary": {
            "cpu": _summary([s["cpu"] for s in samples if s.get("cpu") is not None]),
            "gpu": _summary([s["gpu"] for s in samples if s.get("gpu") is not None]),
            "mem": _summary([s["mem"] for s in samples if s.get("mem") is not None]),
            "fg_cpu": _summary([s["fg_cpu"] for s in samples
                                if s.get("fg_cpu") is not None]),
        },
        "samples": samples,
    }
    dev_short = device.replace(":", "_").replace(".", "_")
    json_path = os.path.join(report_dir, f"perf_{dev_short}_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return json_path


def _fmt(v: float | int | None) -> str:
    return "n/a" if v is None else f"{v}"


# ---------------------------------------------------------------------------
# Probe mode
# ---------------------------------------------------------------------------
def _probe_raw(serial: str, name: str, cmd: str) -> str:
    """Run one read for the probe; print first line; return full output."""
    out = adb_shell(serial, cmd, timeout=10).replace("\r", "")
    first = out.strip().splitlines()[0].strip()[:70] if out.strip() else "(empty)"
    print(f"[probe] {name:<10} : {first}")
    return out


def run_probe(serial: str) -> int:
    if not check_adb_connected(serial):
        print("[probe] device not reachable via adb")
        return 1
    print(f"[probe] device serial    = {serial}")
    for key in ("ro.soc.manufacturer", "ro.soc.model", "ro.hardware",
                "ro.hardware.egl"):
        print(f"[probe] {key:<19} = {getprop(serial, key) or '(empty)'}")
    print("--- node readability ---")
    cpu_out = _probe_raw(serial, "cpu", "cat /proc/stat")
    print(f"[probe] cpu parse    : {'OK' if parse_proc_stat(cpu_out) else 'FAIL'}")
    mem_out = _probe_raw(serial, "mem", "cat /proc/meminfo")
    mt = parse_meminfo(mem_out)
    if mt:
        print(f"[probe] mem parse    : OK (MemTotal={mt[0]}kB avail={mt[1]}kB)")
    else:
        print("[probe] mem parse    : FAIL")
    gpu_out = _probe_raw(serial, "gpu_dvfs", GPU_DVFS_READ)
    gd = parse_gpu_dvfs(gpu_out)
    if gd:
        print(f"[probe] gpu parse    : OK (busy={gd[0]} idle={gd[1]})")
    else:
        print("[probe] gpu parse    : FAIL")
    _probe_raw(serial, "gpu_clk", GPU_CLK_READ)
    focus = adb_shell(serial, "dumpsys window 2>/dev/null | grep mFocusedApp",
                      timeout=10).replace("\r", "")
    pkg = parse_focus_pkg(focus)
    print(f"[probe] fg_pkg       : {pkg or '(not found)'}")
    if pkg:
        top = adb_shell(serial, f"top -n 1 -b 2>/dev/null | grep {pkg} | head -3",
                        timeout=15)
        fg_cpu = parse_top_fg_cpu(top, pkg)
        print(f"[probe] fg_cpu       : {_fmt(fg_cpu)}%")
    if gd:
        print("[probe] result: GPU counter readable via root su - GPU series enabled")
    else:
        print("[probe] result: GPU counter NOT readable - GPU series hidden")
    return 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
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
        description="Real-time device performance monitor (CPU/GPU/mem + front app)")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"interval_sec"?, "duration_sec"?, '
                        '"track_foreground"?}')
    p.add_argument("--probe", action="store_true",
                   help="test node readability then exit")
    args = p.parse_args()

    if args.probe:
        return run_probe(args.device)

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    interval_sec = float(params.get("interval_sec", defaults["interval_sec"]))
    duration_sec = int(params.get("duration_sec", defaults["duration_sec"]))
    track_fg = bool(params.get("track_foreground", defaults["track_foreground"]))
    if interval_sec < 0.5:
        interval_sec = 0.5

    print(f"[config] device          = {args.device}")
    print(f"[config] interval_sec    = {interval_sec}")
    print(f"[config] duration_sec    = {duration_sec} "
          f"{'(until manually stopped)' if duration_sec <= 0 else ''}")
    print(f"[config] track_foreground= {track_fg}")
    print(f"[config] gpu_read        = {GPU_DVFS_READ}")

    if not check_adb_connected(args.device):
        print("[error] device not reachable via adb")
        return 1

    # Probe GPU readability once so the meta line can declare the series.
    gpu_ok = _gpu_readable(args.device)
    sources = {"cpu": "/proc/stat", "mem": "/proc/meminfo",
               "gpu": GPU_DVFS_READ if gpu_ok else None,
               "fg": "top -n 1 -b" if track_fg else None}
    soc_model = getprop(args.device, "ro.soc.model") or "unknown"
    print(f"[perf] sources: cpu={sources['cpu']} mem={sources['mem']} "
          f"gpu={sources['gpu'] or 'n/a'} fg={sources['fg'] or 'off'}")
    print("PERF|" + json.dumps({"type": "meta", "sources": sources,
                                "device": soc_model}, ensure_ascii=True))

    start = time.monotonic()
    cpu_baseline = None
    gpu_baseline = None
    last_pkg: str | None = None
    samples: list[dict] = []
    stopped = False
    sample_timeout = max(30, int(interval_sec) + 15)

    try:
        while True:
            if duration_sec > 0 and time.monotonic() - start >= duration_sec:
                break
            t_elapsed = time.monotonic() - start
            cmd = build_sample_command(track_fg, last_pkg)
            out = adb_shell(args.device, cmd, timeout=sample_timeout)
            secs = split_sections(out)
            sample = build_sample(secs, cpu_baseline, gpu_baseline, t_elapsed,
                                  track_fg)
            cpu_baseline = sample["_cpu_prev"]
            gpu_baseline = sample["_gpu_prev"]
            if sample.get("fg_pkg"):
                last_pkg = sample["fg_pkg"]
            # NOTE: `type` MUST be "sample" - the frontend dispatches PERF lines
            # on obj.type === "sample". `clock` is wall-clock epoch ms (device
            # sample time) so the chart can use a real time x-axis; missing type
            # silently drops every sample on the frontend (v2.4.1 fix).
            emit = {"type": "sample",
                    "clock": int(time.time() * 1000),
                    **{k: sample[k] for k in
                       ("t", "cpu", "gpu", "mem", "fg_cpu", "fg_pkg", "gpu_clk")}}
            print("PERF|" + json.dumps(emit, ensure_ascii=True))
            samples.append(sample)

            elapsed = time.monotonic() - start
            remaining = interval_sec - (elapsed - t_elapsed)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        stopped = True
        print("\n[perf] interrupted - saving report")

    cfg = {"interval_sec": interval_sec, "duration_sec": duration_sec,
           "track_foreground": track_fg}
    print("\n=== perf summary ===")
    if stopped:
        print(f"  stopped early after {len(samples)} samples")
    else:
        print(f"  completed {len(samples)} samples "
              f"({time.monotonic() - start:.1f}s)")
    for key, label in (("cpu", "CPU"), ("gpu", "GPU"), ("mem", "MEM"),
                       ("fg_cpu", "FG")):
        s = _summary([x[key] for x in samples if x.get(key) is not None])
        if s:
            print(f"  {label:<6} min={s['min']} avg={s['avg']} max={s['max']} "
                  f"p95={s['p95']} (%)")
        else:
            print(f"  {label:<6} no valid samples")
    try:
        report_path = save_report(args.device, cfg, sources, stopped, samples)
        print(f"  report          : {report_path}")
    except Exception as e:
        print(f"[warn] failed to save report: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
