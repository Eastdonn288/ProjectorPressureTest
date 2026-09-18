"""
perf_monitor.py - PPTP: device performance monitor (CPU / GPU / mem / foreground app)
                  with tiered sampling, incremental evidence and a closing verdict.

v2 (2026-09-14). Two things changed shape versus v1:

  1. SAMPLING IS TIERED. Not every metric is worth reading every tick. CPU is read
     every tick; memory / GPU / uptime every ~5 s; the foreground-app chain every
     ~30 s. Tiers that fall due on the same tick are merged into ONE adb call, so
     the per-tick cost drops from ~876 ms to 134-409 ms depending on the mix.

  2. THERE IS A VERDICT. v1 only printed numbers. v2 accumulates what CANNOT be
     recovered after the fact (why a reading is null, the raw counters behind every
     delta, when the device rebooted) and runs a pure function over that evidence
     at the end. The live stream stays a stream; the conclusion is printed once.

Wire format (one line per record, prefix "PERF|", JSON ascii-safe):
  PERF|{"type":"meta","sources":{...},"device":"MT9676","judge_version":"..."}
  PERF|{"type":"sample","clock":1756107600000,"t":12.3,"st":"ok:fffhhhhhh",
        "cpu":23.1,"gpu":0.8,"mem":48.2,"fg_cpu":5.1,"gpu_clk":552,
        "fg_pkg":"com.netflix.ninja","gap_ms":0}
  PERF|{"type":"event","clock":...,"t":4120.0,"kind":"reboot","reason":"...","detail":"..."}
  clock = wall-clock epoch ms at sample time (real time x-axis);
  t = seconds since monitor start; missing metrics are null.
  st = "<tick result>:<9 per-metric state chars>", see METRIC_ORDER.

Data sources (probed on the real MT9676 device, 2026-08-25 / 2026-09-14):
  cpu     : /proc/stat aggregate `cpu` line (cumulative, no root) -> delta busy%
  procs   : `set -- /proc/[0-9]*; echo $#` (shell glob builtin, no fork)
  up      : /proc/uptime col 1 (seconds since boot). This is how a device reboot is
            detected - and a reboot silently zeroes /proc/stat and the GPU counters,
            so the delta guard MUST see the same tick (hence uptime is a FAST layer
            reading, not a MED one).
  mem     : /proc/meminfo MemAvailable / MemTotal (no root) -> used%
  gpu     : /sys/kernel/debug/mali0/dvfs_utilization + gpu_clock, ONE `su 0 cat` of
            both files (two separate su calls cost an extra ~122 ms/tick).
            REQUIRES root; degrades to disabled if unreadable.
  fg      : `dumpsys window | grep -m1 mFocusedApp` -> package, then `pidof <pkg>`
            -> pid(s), then /proc/<pid>/stat utime+stime -> CPU%. NOT `top` (581 ms
            per read on this board, and its instantaneous %CPU is not repeatable:
            three back-to-back reads of one package gave 85.0 / 26.4 / 19.6).
            NOT `dumpsys cpuinfo` (5 min rolling average on this device).
            NOT /proc/<pid>/comm (kernel truncates at 15 chars: `(i.video.speaker)`).

NOTE: the "standard" MTK/Mali sysfs GPU nodes (/sys/module/ged/parameters/*,
/sys/kernel/ged/hal/*, /sys/devices/platform/*mali*/utilization) DO NOT exist
on this board; the Mali counters live under debugfs `mali0` and need root.

Contract (matches other PPTP scripts):
  --device <serial>  (required, injected by PPTP platform)
  --params <json>    (optional; see PARAMS below)
  --probe            (standalone: test node readability, then exit)
  --probe-cost       (standalone: re-measure the tiered command costs, then exit)
  --selftest         (standalone: assert judge/evidence invariants, then exit)

Run standalone:
    python scripts/perf_monitor.py --device <serial> \
        --params '{"interval_sec": 2, "duration_sec": 30}'
"""
import argparse
import csv
import html
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime

# Shared report engine, for the parameters table this report renders - it is
# built from PARAMS, so a parameter added there shows up here without anyone
# editing this file. A sibling module, resolved via sys.path[0]: the
# platform launches scripts by absolute path and a plain
# `python scripts/perf_monitor.py` puts scripts/ on sys.path too.
# See docs/REPORT_FORMAT.md.
import _pptp_report

# The optional key injector drives ir_runner's step executor rather than
# re-implementing `adb shell input keyevent` here: the .ini grammar, the
# KEYCODE_* vs KEY_* dispatch and the long-press handling already live there and
# are proven on this device. Importing it has no side effects (its main() is
# guarded by __name__), and a sibling import resolves exactly like the one above.
import ir_runner

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a verdict.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

SCRIPT_VERSION = "1.1.1"

# ---------------------------------------------------------------------------
# Frozen schema (order is load-bearing; do not sort or reorder)
# ---------------------------------------------------------------------------
# Metric state characters, one per entry of METRIC_ORDER. The distinction that
# matters most: `held` is a stale value carried forward by zero-order hold, NOT a
# reading. Without it a flat line produced by ZOH is indistinguishable from a
# device genuinely sitting at a constant value.
ST_FRESH = "f"        # due this tick, parsed, real delta/value
ST_HELD = "h"         # not due this tick, last known value carried forward
ST_NOT_DUE = "n"      # not due this tick and no history yet
ST_BASELINE = "b"     # due and parsed, but first read -> no delta exists yet
ST_FAILED = "x"       # due, parsed and failed
ST_ABSENT = "a"       # due, device reports the node does not exist / not readable
ST_DISABLED = "d"     # switched off by config or by the degradation policy

METRIC_ORDER = ["cpu", "procs", "up", "mem", "gpu", "gpu_clk",
                "fg_pkg", "fg_pid", "fg_cpu"]

# Metrics whose value is a DELTA between two reads. First successful read of each
# can only establish a baseline, never a value (`b`).
DELTA_METRICS = frozenset({"cpu", "gpu", "fg_cpu"})

TIER_FAST, TIER_MED, TIER_SLOW = "FAST", "MED", "SLOW"

# (section marker, tier, metrics it feeds). Command order == this order.
SECTIONS = [
    ("STAT", TIER_FAST, ("cpu",)),
    ("PROCS", TIER_FAST, ("procs",)),
    ("UP", TIER_FAST, ("up",)),
    ("MEM", TIER_MED, ("mem",)),
    ("FOCUS", TIER_SLOW, ("fg_pkg",)),
    ("FGPKG", TIER_SLOW, ("fg_pid",)),
    ("PIDSTAT", TIER_SLOW, ("fg_cpu",)),
    ("GPU", TIER_MED, ("gpu", "gpu_clk")),
]
SECTION_TIER = {name: tier for name, tier, _m in SECTIONS}

# /proc/<pid>/stat utime+stime are in USER_HZ (always 100 on Android); there is no
# portable way to query it from a shell one-liner, and getconf is absent on toybox.
USER_HZ = 100

# Sampling cadence. interval below 1.0 s is physically unreachable: the FULL
# composite command alone measured 408.7 ms and each tick also pays 110-140 ms of
# adb.exe process + transport overhead.
INTERVAL_MIN_SEC = 1.0
INTERVAL_MAX_SEC = 60.0
DURATION_MAX_SEC = 86400

# ---------------------------------------------------------------------------
# Detection thresholds -- ALL PLACEHOLDERS until a healthy long baseline exists.
# CALIBRATED stays False until a real 8 h healthy run has been measured; the flag
# is printed in every verdict so an uncalibrated report can never look like a
# confident one. See TODO.md 6.2 / docs/PERF_MONITOR_V2.md 12.7 #8.
# ---------------------------------------------------------------------------
CALIBRATED = False
JUDGE_VERSION = "v1" + ("" if CALIBRATED else "-uncalibrated")

T_COVERAGE_OK = 0.80           # >= this and the DATA gate passes outright
T_COVERAGE_WARN = 0.50         # below this the run is INCONCLUSIVE, not WARN
T_CADENCE_RATIO = 1.50         # p50(dt) / requested period
T_TIMEOUT_FRAC = 0.05          # timeouts / expected ticks -> warn
T_TIMEOUT_FRAC_FAIL = 0.20
T_LEAK_SLOPE_PCT_PER_H = 0.50  # block-median slope above this ...
T_LEAK_DELTA_PCT = 2.00        # ... AND total growth above this -> leak suspected
T_OOM_AVAIL_MB = 200.0         # MemAvailable floor -> OOM precursor
# A slope fitted over a window shorter than this is noise extrapolated to %/h:
# a 20 s run with 0.3% of drift reports "54%/h". Below this span the slope is
# still recorded as a diagnostic but may not raise an alarm.
T_MEM_MIN_SPAN_S = 1800.0
T_CPU_P95_BUSY = 90.0          # reporting only: sustained-high-CPU is not a fault
MEM_BLOCKS = 7                 # block count for the trend estimate
# Consecutive SLOW observations of "the watched app is not in the foreground"
# that count as one loss. Two, because a single observation can catch a
# transition (the app-switch animation, an OSD stealing focus) rather than a
# settled state.
WATCH_LOSS_STREAK = 2

# Timeout geometry. B (PC budget) must strictly exceed G (device guard) plus the
# device-side kill fan-out plus a round trip, or the PC gives up while the device
# is still running the old command and the next tick stacks another one on top.
DEVICE_GUARD_DEFAULT_S = 3.0
DEVICE_GUARD_MIN_S = 3.0
DEVICE_GUARD_MAX_S = 15.0
DEVICE_KILL_FANOUT_S = 2.0     # `timeout -k 2`
PC_BUDGET_MARGIN_S = 0.40
TRANSIT_FALLBACK_MS = 300.0    # only used if the startup self-measurement fails

BUDGET_WINDOW = 30             # ticks used to estimate the typical cost
BUDGET_GROW_STREAK = 3         # consecutive requests needed before G may grow

EVENT_RATE_WINDOW_S = 60.0

# A fixed wall clock for the synthetic event that --selftest renders, so the
# expected "18:39:12"-style string is predictable in any timezone.
SELFTEST_EVENT_CLOCK_MS = 1789614015576
SNAPSHOT_INTERVAL_S = 120.0    # partial verdict cadence (hard-kill insurance)
GPU_REPROBE_INTERVAL_S = 300.0
GPU_DEGRADE_AFTER = 3          # consecutive failed due ticks before disabling

# Frontend-configurable params (declared for the PPTP platform).
# Thresholds are deliberately NOT here: they are calibration values, not settings.
#
# Foreground tracking is NOT a param. It is always on: the fg_* metrics are the
# only signal for "the app under test died or fell to the background", which is
# the single most common thing that goes wrong in a long unattended run. The
# saving from switching it off is one `dumpsys window` + `pidof` +
# /proc/<pid>/stat per SLOW period, which `--probe-cost` already accounts for.
#
# watch_pkg is a fixed list rather than free text (user 2026-09-14): the point of
# the parameter is to name one of the apps actually being stressed, and a typo in
# a free-text package name silently disables the one gate it feeds.
#
# The four entries below are the apps the user actually stresses (confirmed
# 2026-09-14). They were read off the test device with:
#   adb shell cmd package query-activities -a android.intent.action.MAIN -c
#       android.intent.category.LEANBACK_LAUNCHER
# NOTE: `pm list packages -3` is NOT good enough - YouTube TV and Netflix are
# system apps on this device, so the third-party-only listing omits both.
# Before adding one, verify it exists: a watch_pkg that never appears on screen
# makes gate 9 report a LOSS of the watched app, i.e. a false FAIL.
WATCH_PKG_CHOICES = [
    {"value": "", "label": "(不关注 / 只看前台是哪个应用)"},
    {"value": "com.google.android.youtube.tv", "label": "YouTube TV"},
    {"value": "com.netflix.ninja", "label": "Netflix"},
    {"value": "com.amazon.amazonvideo.livingroom", "label": "Prime Video"},
    {"value": "com.mediatek.wwtv.mediaplayer", "label": "本地媒体播放器"},
]

def _key_ini_choices() -> list:
    """The .ini files the key injector may run, as {value,label} select choices.

    A fixed list rather than free text, for the same reason as watch_pkg: the
    point is to pick one of the sequences that exist, and a typo in a path would
    silently start nothing at all. Transient _seq_*.ini files are skipped -
    ir_runner writes those only while running with a `sequence_content` param,
    so they are never a real choice. Empty first entry = do not inject keys.
    """
    choices = [{"value": "", "label": "(不发送按键)"}]
    d = os.path.join(PROJECT_ROOT, "ir_sequences")
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.endswith(".ini") and not name.startswith("_"):
                choices.append({"value": f"ir_sequences/{name}",
                                "label": name})
    return choices


KEY_INI_CHOICES = _key_ini_choices()

PARAMS = [
    {"name": "interval_sec", "label": "采样间隔(秒)", "type": "float",
     "default": 2.0, "min": INTERVAL_MIN_SEC, "max": INTERVAL_MAX_SEC},
    {"name": "duration_sec", "label": "总时长(秒, 0=直到手动停止)", "type": "int",
     "default": 0, "min": 0, "max": DURATION_MAX_SEC},
    {"name": "watch_pkg", "label": "关注的应用(它掉到后台/挂掉才算异常)",
     "type": "select", "choices": WATCH_PKG_CHOICES, "default": ""},
    {"name": "key_ini", "label": "定时发按键的 ini(长跑时防空闲提示)",
     "type": "select", "choices": KEY_INI_CHOICES, "default": ""},
]

OFFLINE_RE = re.compile(r"device offline|no devices|device not found|not found",
                        re.I)
ABSENT_MARKERS = ("no such file", "not found", "permission denied",
                  "operation not permitted", "no such device")

CSV_COLUMNS = [
    "t_sec", "clock_ms", "res", "k", "cost_ms", "flush_ms", "late_ms", "gap_ms",
    "cpu", "cpu_st", "procs", "procs_st", "up", "up_st", "mem", "mem_st",
    "gpu", "gpu_st", "gpu_clk", "gpu_clk_st", "fg_pkg", "fg_pkg_st",
    "fg_pid", "fg_pid_st", "fg_cpu", "fg_cpu_st",
    "causes", "events",
]

MARKER_RE = re.compile(r"^@@([A-Z]+):([0-9a-f]{6})$")
ST_LINE_RE = re.compile(r"^(ok|partial|timeout|offline|error):[fhnbxad]{9}$")


# ---------------------------------------------------------------------------
# ADB layer - bounded, never raises, always says WHY it failed
# ---------------------------------------------------------------------------
def adb_run(args: list[str], budget_s: float) -> tuple[int | None, str, str]:
    """Run a subprocess with a HARD wall-clock bound. Returns (rc, out, err).

    rc is None when the PC budget expired. Never raises.

    `subprocess.run(timeout=)` is not enough: after TimeoutExpired it kills and
    waits, and CPython's wait is unbounded once the process is in an
    uninterruptible state. That hangs the sampling thread forever and the task
    silently stops producing data while still reporting `running`. So: kill, then
    ONE bounded second `communicate`, then abandon the pipes.
    """
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
    except Exception as e:
        return 1, "", f"spawn failed: {e}"
    try:
        out, err = proc.communicate(timeout=budget_s)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=1.0)
            return None, out or "", (err or "") + "pc budget expired"
        except subprocess.TimeoutExpired:
            # Still alive after SIGKILL (D-state on the PC side is impossible, but
            # a wedged adb.exe can ignore it). Close the pipes and walk away - the
            # thread must survive even if this child never reaps.
            for fh in (proc.stdout, proc.stderr):
                try:
                    fh.close()
                except Exception:
                    pass
            return None, "", "pc budget expired (unreaped)"
    except Exception as e:
        return 1, "", f"communicate failed: {e}"


def adb_shell(serial: str, command: str,
              budget_s: float) -> tuple[int | None, str, str]:
    """Run `adb -s <serial> shell <command>` as a list-arg (never shell=True).

    The compound command contains `;` `|` `$()` which MUST reach the DEVICE shell.
    """
    return adb_run(["adb", "-s", serial, "shell", command], budget_s)


def measure_transit_ms(serial: str, reps: int = 3) -> float:
    """Self-measure the adb round-trip baseline on THIS link.

    Decision 12.7 #6: the transit constant must not be hardcoded. USB measured
    110-140 ms; adb over WiFi is unmeasured and could be several times that, and
    an under-sized PC budget on a slow link manufactures exactly the overlap the
    budget exists to prevent. Measuring here absorbs the difference.
    """
    vals = []
    for _ in range(reps):
        t0 = time.perf_counter()
        rc, out, _err = adb_shell(serial, "true", 5.0)
        if rc == 0:
            vals.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(0.05)
    if not vals:
        return TRANSIT_FALLBACK_MS
    return statistics.median(vals)


def getprop(serial: str, key: str) -> str:
    rc, out, _err = adb_shell(serial, f"getprop {key}", 5.0)
    return out.strip() if rc == 0 else ""


# ---------------------------------------------------------------------------
# Scheduler (integer milliseconds only -- float seconds produce pathological
# ceil() values for T like 1.1 / 2.2 and break tier nesting)
# ---------------------------------------------------------------------------
def tier_ticks_per(t_ms: int) -> dict[str, int]:
    """Ticks between due points per tier, nested so SLOW is always a multiple of MED.

    `SLOW = T*ceil(30/T)` does NOT nest when T is fractional: it can produce a tick
    where SLOW is due and MED is not, which is a malformed sample. Nesting off MED
    cannot.
    """
    med = max(1, math.ceil(5000 / t_ms))
    slow = med * max(1, math.ceil(30000 / (med * t_ms)))
    return {TIER_FAST: 1, TIER_MED: med, TIER_SLOW: slow}


def is_due(tier: str, ticks_per: dict[str, int], k: int) -> bool:
    return (k % ticks_per[tier]) == 0


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
def section_due(name: str, secs: dict, tiers: set, gpu_enabled: bool) -> bool:
    """Is section `name` both PRESENT in this tick's output and DUE this tick?

    Module level rather than a closure inside Monitor.tick so it can be asserted
    on directly. That matters: this predicate has already been the site of one
    silent-channel-loss bug (see the history note in tick()), and it is
    unobservable from the outside - a section that is never 'due' simply never
    appears in the state string, which is indistinguishable from a short run.
    """
    return (name in secs and SECTION_TIER[name] in tiers
            and (name != "GPU" or gpu_enabled))


def build_tick_command(nonce: str, tiers_present: set[str], guard_s: int,
                       gpu_enabled: bool) -> str:
    """Assemble the compound device command for one tick.

    Every section is preceded by `@@<SEC>:<nonce>` and the command always ends with
    `@@DONE:<nonce>`. The nonce matters because device-side chatter can emit a line
    starting with `@@`; without it, one stray line shears every section boundary
    after it. The tail marker matters because `@@GPU` is echoed BEFORE the su read
    runs - without a terminator, "the su section was truncated" and "this device has
    no GPU node" are indistinguishable.

    The foreground chain is resolved DEVICE-side from one dumpsys call (shell
    parameter expansion, no fork). Substituting a remembered package client-side
    would lag one SLOW period behind an app switch and produce nothing at all on
    the first SLOW tick.
    """
    fg = TIER_SLOW in tiers_present
    parts: list[str] = []
    for name, tier, _metrics in SECTIONS:
        if tier not in tiers_present:
            continue
        if name == "GPU" and not gpu_enabled:
            continue
        if name in ("FOCUS", "FGPKG", "PIDSTAT") and not fg:
            continue
        mark = f"echo @@{name}:{nonce}"
        if name == "STAT":
            parts += [mark, "cat /proc/stat"]
        elif name == "PROCS":
            # Shell glob builtin, no fork: `ls /proc | grep -c` costs ~20x more.
            parts += [mark, "set -- /proc/[0-9]*", "echo $#"]
        elif name == "UP":
            parts += [mark, "cat /proc/uptime"]
        elif name == "MEM":
            parts += [mark, "cat /proc/meminfo"]
        elif name == "FOCUS":
            # $F is reused by FGPKG/PIDSTAT below; all three run in ONE shell.
            parts += [mark,
                      "F=$(dumpsys window 2>/dev/null | grep -m1 mFocusedApp)",
                      'echo "$F"',
                      "P=${F##*u0 }", "P=${P%%/*}"]
        elif name == "FGPKG":
            parts += [mark, 'if [ -n "$P" ]; then pidof "$P"; fi']
        elif name == "PIDSTAT":
            parts += [mark,
                      'if [ -n "$P" ]; then for x in $(pidof "$P"); do '
                      "cat /proc/$x/stat; done; fi"]
        elif name == "GPU":
            parts += [mark,
                      f"timeout -k 2 {guard_s} su 0 cat "
                      "/sys/kernel/debug/mali0/dvfs_utilization "
                      "/sys/kernel/debug/mali0/gpu_clock"]
    parts.append(f"echo @@DONE:{nonce}")
    return "; ".join(parts)


def split_sections(out: str, nonce: str) -> tuple[dict[str, str], int]:
    """Split device output into {SECTION: text}. Returns (sections, misparse_count).

    Only markers carrying THIS tick's nonce open a section. A `@@`-prefixed line
    that fails the test (device chatter, or a replayed echo) is counted and also
    CLOSES the current section, so its text cannot leak into the previous one.
    """
    secs: dict[str, list[str]] = {}
    cur: str | None = None
    misparse = 0
    for raw in out.replace("\r", "").splitlines():
        line = raw.rstrip("\n")
        if line.startswith("@@"):
            m = MARKER_RE.match(line.strip())
            if m and m.group(2) == nonce:
                cur = m.group(1)
                secs[cur] = []
            else:
                misparse += 1
                cur = None
        elif cur is not None:
            secs[cur].append(line)
    return {k: "\n".join(v) for k, v in secs.items()}, misparse


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------
def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """Aggregate `cpu` line -> (total, idle+iowait)."""
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
        return sum(vals), vals[3] + vals[4]
    return None


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """-> (MemTotal_kB, MemAvailable_kB)."""
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


def parse_uptime(text: str) -> float | None:
    """First column of /proc/uptime (seconds since boot)."""
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def parse_procs(text: str):
    for tok in text.split():
        try:
            return int(tok)
        except ValueError:
            continue
    return None


def parse_gpu_both(text: str) -> tuple[tuple[int, int] | None, int | None]:
    """Parse the ONE su read of dvfs_utilization + gpu_clock -> ((busy,idle), mhz).

    dvfs_utilization carries `busy_time: X idle_time: Y`; gpu_clock is a bare
    integer on its own line. The clock MUST be matched as a standalone integer
    line - a `re.search(r"(\\d+)")` over the combined text returns a busy_time digit
    instead, and the reported clock silently becomes a counter.
    """
    dvfs = re.search(r"busy_time:\s*(\d+)\s+idle_time:\s*(\d+)", text)
    busy_idle = (int(dvfs.group(1)), int(dvfs.group(2))) if dvfs else None
    clk = None
    for line in text.splitlines():
        s = line.strip()
        if s.isdigit():
            clk = int(s)
            break
    return busy_idle, clk


def parse_focus_pkg(text: str) -> str | None:
    """`mFocusedApp=ActivityRecord{... u0 pkg/Act tN}` -> pkg."""
    m = re.search(r"mFocusedApp=.*?\bu0\s+([^\s/]+)/", text)
    return m.group(1) if m else None


def parse_pidof(text: str) -> list[int]:
    pids = []
    for tok in text.split():
        try:
            pids.append(int(tok))
        except ValueError:
            continue
    return pids


def parse_pid_stats(text: str) -> dict[int, int]:
    """`/proc/<pid>/stat` lines -> {pid: utime + stime}.

    Field lookup is anchored on the LAST ')' because the comm field (field 2) may
    itself contain spaces and parentheses: `12066 (i.video.speaker) S ...`. After
    the ')' the fields are 3..N, so utime (field 14) is index 11 and stime (15) is
    index 12.
    """
    out: dict[int, int] = {}
    for line in text.splitlines():
        close = line.rfind(")")
        open_ = line.find("(")
        if open_ < 0 or close < 0:
            continue
        try:
            pid = int(line[:open_].strip())
        except ValueError:
            continue
        parts = line[close + 1:].split()
        if len(parts) < 13:
            continue
        try:
            out[pid] = int(parts[11]) + int(parts[12])
        except ValueError:
            continue
    return out


def section_error_kind(text: str) -> str:
    """Classify a failed section -> ST_ABSENT or ST_FAILED."""
    low = text.lower()
    return ST_ABSENT if any(k in low for k in ABSENT_MARKERS) else ST_FAILED


def pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    return s[max(1, math.ceil(p / 100.0 * len(s))) - 1]


def stats_of(vals: list[float], digits: int = 1) -> dict:
    if not vals:
        return {}
    r = lambda x: round(x, digits)  # noqa: E731
    return {"n": len(vals), "min": r(min(vals)), "max": r(max(vals)),
            "avg": r(sum(vals) / len(vals)), "p50": r(pct(vals, 50)),
            "p90": r(pct(vals, 90)), "p95": r(pct(vals, 95))}


# ---------------------------------------------------------------------------
# Evidence accumulator
# ---------------------------------------------------------------------------
class Evidence:
    """Everything that cannot be recovered after the run: null causes, the raw
    counters behind each delta, event times, reboot detection.

    The verdict is computed from this object and nothing else, so a report always
    carries both halves and any conclusion can be recomputed later.
    """

    def __init__(self, serial: str, t_ms: int, ticks_per: dict[str, int],
                 duration_sec: int, watch_pkg: str,
                 sources: dict):
        self.serial = serial
        self.dev_short = serial.replace(":", "_").replace(".", "_")
        self.t_ms = t_ms
        self.ticks_per = ticks_per
        self.duration_sec = duration_sec
        self.watch_pkg = watch_pkg
        self.sources = sources

        self.k = 0
        self.last_k = -1
        self.res_counts = {"ok": 0, "partial": 0, "timeout": 0,
                           "offline": 0, "error": 0}
        self.n_due = {m: 0 for m in METRIC_ORDER}
        self.n_due_ok = {m: 0 for m in METRIC_ORDER}
        self.n_fresh = {m: 0 for m in METRIC_ORDER}
        self.n_held = {m: 0 for m in METRIC_ORDER}

        self.dt_ms: list[float] = []
        self.cost_ms: list[float] = []
        self.late_ms: list[float] = []
        self.wall_ms = 0.0
        self.values: dict[str, list[float]] = {m: [] for m in METRIC_ORDER}
        self.mem_t: list[float] = []
        self.mem_avail_mb: list[float] = []

        self.cpu_t0: tuple[int, int] | None = None
        self.cpu_tN: tuple[int, int] | None = None
        self.gpu_t0: tuple[int, int] | None = None
        self.gpu_tN: tuple[int, int] | None = None

        self.up_first: float | None = None
        self.up_last: float | None = None
        self.reboots: list[dict] = []
        self.rollovers = 0
        self.fg_switches = 0
        self.fg_pkgs: list[str] = []
        self.fg_pid_lost = 0
        self.app_gone = 0
        self.pkg_mismatch = 0
        self._last_fg_pkg: str | None = None
        self._miss_streak = 0
        # Watched-app bookkeeping.
        #
        # `watch_seen` is the difference between "it was here and then it went
        # away" (a fault) and "it never showed up at all" (a fact about the
        # setup: wrong package, or a run that started on the launcher). Only
        # the first is a loss. Without this flag the two are indistinguishable,
        # and the code that shipped before v2.9.0 reported both as a loss.
        #
        # `watch_pid_dead` counts observations where the watched app is STILL
        # the focused window but its process is gone - it died in place. That
        # is the exact failure watch_pkg exists to catch, and comparing package
        # names alone can never see it.
        self.watch_seen = False
        self.watch_pid_dead = 0
        self._watch_lost = False

        self.events: list[dict] = []
        self.ev_budget: dict[str, float] = {}
        # Mirrored from the Monitor every tick: the budget geometry is part of the
        # evidence, because it explains every timeout in the run.
        self.guard_s = DEVICE_GUARD_DEFAULT_S
        self.transit_ms = TRANSIT_FALLBACK_MS
        self.start_iso = datetime.now().isoformat(timespec="seconds")
        self.end_iso: str | None = None
        self.csv_status = "ok"
        self.partial = False

    # -- events -------------------------------------------------------------
    def add_event(self, t_sec: float, clock_ms: int, kind: str, reason: str,
                  detail: str = "", immediate: bool = True) -> dict | None:
        """Record an event, subject to a fixed-window per-kind rate limit.

        The rate limiter registers a key ONLY when the event is actually kept. The
        reference implementation registered first and then truncated, so a real
        event could be swallowed by a limit that the swallowed event itself had
        already tripped.
        """
        if immediate:
            last = self.ev_budget.get(kind)
            if last is not None and (t_sec - last) < EVENT_RATE_WINDOW_S:
                return None
            self.ev_budget[kind] = t_sec
        ev = {"t_sec": round(t_sec, 1), "clock_ms": clock_ms, "type": kind,
              "reason": reason, "detail": detail}
        self.events.append(ev)
        return ev

    # -- per-tick ingestion -------------------------------------------------
    def ingest(self, row: dict, states: dict[str, str], res: str,
               mem_info: tuple[int, int] | None, cpu_raw, gpu_raw,
               up_val: float | None, fg_pkg: str | None, pid_set: list[int]):
        self.k += 1
        self.last_k = max(self.last_k, int(row["k"]))
        self.res_counts[res] = self.res_counts.get(res, 0) + 1
        # A failed tick marks every metric failed, so it inflates n_due for all of
        # them. That is right for the COVERAGE denominator - the slot really was
        # requested and really came back empty - but it is wrong for the "the
        # channel is wired up at all" gates: a device that is simply offline never
        # got the chance to answer. n_due_ok counts the opportunities the device
        # actually had.
        tick_ok = res in ("ok", "partial")
        for m in METRIC_ORDER:
            s = states.get(m, ST_DISABLED)
            if s in (ST_FRESH, ST_BASELINE, ST_FAILED, ST_ABSENT):
                self.n_due[m] += 1
                if tick_ok:
                    self.n_due_ok[m] += 1
            if s == ST_FRESH:
                self.n_fresh[m] += 1
            elif s == ST_HELD:
                self.n_held[m] += 1

        self.cost_ms.append(row["cost_ms"])
        self.late_ms.append(row["late_ms"])
        self.wall_ms = max(self.wall_ms, row["t_sec"] * 1000.0)
        v = row.get("_dt_ms")
        if v is not None:
            self.dt_ms.append(v)

        for m in ("cpu", "gpu", "mem", "gpu_clk", "procs", "fg_cpu"):
            if states.get(m) == ST_FRESH:
                val = row.get(m)
                if val is not None:
                    self.values[m].append(float(val))
        if states.get("up") in (ST_FRESH, ST_BASELINE) and up_val is not None:
            if self.up_first is None:
                self.up_first = up_val
            self.up_last = up_val
        if states.get("mem") == ST_FRESH and mem_info:
            self.mem_t.append(row["t_sec"])
            self.mem_avail_mb.append(round(mem_info[1] / 1024.0, 1))
        if cpu_raw is not None:
            if self.cpu_t0 is None:
                self.cpu_t0 = cpu_raw
            self.cpu_tN = cpu_raw
        if gpu_raw is not None:
            if self.gpu_t0 is None:
                self.gpu_t0 = gpu_raw
            self.gpu_tN = gpu_raw

        if fg_pkg and fg_pkg != self._last_fg_pkg:
            if self._last_fg_pkg is not None:
                self.fg_switches += 1
            self._last_fg_pkg = fg_pkg
            if fg_pkg not in self.fg_pkgs:
                self.fg_pkgs.append(fg_pkg)
        # ---- watched-app health -------------------------------------------
        # An observation is HEALTHY when the watched app is the focused window
        # AND its process exists. Everything else is one of two faults:
        #
        #   gone       it was healthy at least once, then left the foreground
        #              for WATCH_LOSS_STREAK consecutive observations
        #   pid_dead   it is still the focused window but its process is gone
        #
        # Both are gated on `watch_seen`, so "never showed up" can no longer be
        # reported as a loss (it is gate 9's third outcome, inconclusive).
        if self.watch_pkg and fg_pkg is not None:
            if fg_pkg == self.watch_pkg and pid_set:
                self.watch_seen = True
                self._watch_lost = False
                self._miss_streak = 0
            else:
                if fg_pkg == self.watch_pkg:
                    self.watch_pid_dead += 1
                else:
                    self._miss_streak += 1
                lost = (fg_pkg == self.watch_pkg
                        or self._miss_streak >= WATCH_LOSS_STREAK)
                # The latch keeps one count per episode: without it a watched
                # app that stays away would add a loss on every observation.
                if lost and self.watch_seen and not self._watch_lost:
                    self._watch_lost = True
                    self.app_gone += 1
                    self.pkg_mismatch += 1
        if fg_pkg and not pid_set:
            self.fg_pid_lost += 1

    def freeze(self, res: str, csv_name: str, partial: bool) -> dict:
        """Build the evidence dict: the ONLY input the verdict is computed from."""
        self.end_iso = self.end_iso or datetime.now().isoformat(timespec="seconds")
        # Denominator is the number of SCHEDULED slots, taken from the tick index.
        # Deriving it from elapsed time is off by one (ticks run at k=0..N-1 while
        # the elapsed span is (N-1)*T, so coverage reads >100%) and, worse, hides a
        # slow device: a stalled run inflates its own denominator and still scores
        # near 1.0. The tick index counts skipped slots correctly instead.
        expected = max(1, self.last_k + 1)
        good = self.res_counts["ok"] + self.res_counts["partial"]
        span_ms = self.wall_ms
        cad = statistics.median(self.dt_ms) if self.dt_ms else float(self.t_ms)

        metrics = {}
        for m in METRIC_ORDER:
            due = self.n_due[m]
            metrics[m] = {
                "n_fresh": self.n_fresh[m],
                "n_due": due,
                "n_due_ok": self.n_due_ok[m],
                "n_held": self.n_held[m],
                "coverage": round(self.n_fresh[m] / due, 3) if due else None,
            }
        for m, s in (("cpu", stats_of(self.values["cpu"])),
                     ("gpu", stats_of(self.values["gpu"])),
                     ("mem", stats_of(self.values["mem"])),
                     ("gpu_clk", stats_of(self.values["gpu_clk"], 0)),
                     ("procs", stats_of(self.values["procs"], 0)),
                     ("fg_cpu", stats_of(self.values["fg_cpu"]))):
            metrics[m].update(s)

        mem_ev = self._memory_evidence()
        cpu_vals = self.values["cpu"]
        ge90 = 0.0
        if cpu_vals and self.dt_ms:
            per = statistics.median(self.dt_ms) / 1000.0
            ge90 = round(per * sum(1 for v in cpu_vals if v >= T_CPU_P95_BUSY), 1)

        return {
            "schema": "pptp-perf-evidence/2",
            "judge_version": JUDGE_VERSION,
            "device": {"serial": self.serial, "dev_short": self.dev_short,
                       "uptime_first_s": self.up_first,
                       "uptime_last_s": self.up_last,
                       "reboots": self.reboots,
                       "rollovers": self.rollovers},
            "run": {
                "ticks": self.k,
                "expected": expected,
                "ok": self.res_counts["ok"],
                "partial_ticks": self.res_counts["partial"],
                "timeout": self.res_counts["timeout"],
                "offline": self.res_counts["offline"],
                "error": self.res_counts["error"],
                "coverage": round(good / expected, 3),
                "cadence_ms": round(cad, 1),
                "cadence_ratio": round(cad / self.t_ms, 3) if self.t_ms else None,
                "span_ms": round(span_ms, 1),
                "t_ms": self.t_ms,
                "duration_sec": self.duration_sec,
                "start_iso": self.start_iso,
                "end_iso": self.end_iso,
                "status": res,
                "partial": bool(partial),
                "csv_status": self.csv_status,
                "samples_csv": csv_name,
                "cost_ms": stats_of(self.cost_ms, 1),
                "late_ms": stats_of(self.late_ms, 1),
                "guard_s": self.guard_s,
                "transit_ms": self.transit_ms,
                "duty_pct": round(sum(self.cost_ms) / span_ms * 100.0, 2)
                if span_ms > 0 else None,
            },
            "metrics": metrics,
            "counters": {"cpu_first": list(self.cpu_t0) if self.cpu_t0 else None,
                         "cpu_last": list(self.cpu_tN) if self.cpu_tN else None,
                         "gpu_first": list(self.gpu_t0) if self.gpu_t0 else None,
                         "gpu_last": list(self.gpu_tN) if self.gpu_tN else None},
            "memory": mem_ev,
            "cpu": {"avg": metrics["cpu"].get("avg"), "p95": metrics["cpu"].get("p95"),
                    "max": metrics["cpu"].get("max"),
                    "ge90_s": ge90, "n": metrics["cpu"].get("n", 0)},
            "fg": {"switches": self.fg_switches, "pkgs": self.fg_pkgs,
                   "gone": self.app_gone, "pid_lost": self.fg_pid_lost,
                   "mismatch": self.pkg_mismatch, "watch_pkg": self.watch_pkg,
                   "seen": self.watch_seen,
                   "pid_dead": self.watch_pid_dead},
            "events": {"total": len(self.events),
                       "by_type": self._event_counts(),
                       "items": self.events},
            "sources": self.sources,
        }

    def _event_counts(self) -> dict:
        out: dict[str, int] = {}
        for e in self.events:
            out[e["type"]] = out.get(e["type"], 0) + 1
        return out

    def _memory_evidence(self) -> dict:
        """Block-median trend, because the raw series is far too autocorrelated for
        a naive least-squares slope (and Theil-Sen on raw points is O(n^2) and gets
        dragged by spikes)."""
        n = len(self.values["mem"])
        out = {"n": n, "blocks_valid": 0, "blocks_total": MEM_BLOCKS,
               "first_pct": None, "last_pct": None, "delta_pct": None,
               "slope_pct_per_h": None, "avail_min_mb": None, "avail_last_mb": None}
        if not n:
            return out
        out["first_pct"] = self.values["mem"][0]
        out["last_pct"] = self.values["mem"][-1]
        out["delta_pct"] = round(out["last_pct"] - out["first_pct"], 2)
        if self.mem_avail_mb:
            out["avail_min_mb"] = min(self.mem_avail_mb)
            out["avail_last_mb"] = self.mem_avail_mb[-1]
        if n < 4 or not self.mem_t:
            return out
        t0, t1 = self.mem_t[0], self.mem_t[-1]
        span = t1 - t0
        if span <= 0:
            return out
        buckets: list[list[float]] = [[] for _ in range(MEM_BLOCKS)]
        for t, v in zip(self.mem_t, self.values["mem"]):
            idx = min(MEM_BLOCKS - 1, int((t - t0) / span * MEM_BLOCKS))
            buckets[idx].append(v)
        pts = [(t0 + span * (i + 0.5) / MEM_BLOCKS, statistics.median(b))
               for i, b in enumerate(buckets) if b]
        out["blocks_valid"] = len(pts)
        if len(pts) < 2:
            return out
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        den = sum((p[0] - mx) ** 2 for p in pts)
        if den <= 0:
            return out
        slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / den
        out["slope_pct_per_h"] = round(slope * 3600.0, 3)
        return out


# ---------------------------------------------------------------------------
# Verdict -- a PURE function of evidence. Nothing else may be consulted.
# ---------------------------------------------------------------------------
def judge(ev: dict) -> dict:
    """evidence -> verdict. Deterministic and side-effect free, so that
    judge(report["evidence"]) == report["verdict"] holds for every report."""
    run = ev.get("run", {})
    met = ev.get("metrics", {})
    mem = ev.get("memory", {})
    dev = ev.get("device", {})
    fg = ev.get("fg", {})
    gates: list[dict] = []

    def gate(gid, name, status, detail):
        gates.append({"id": gid, "name": name, "status": status, "detail": detail})

    # 1 DATA
    if run.get("ticks_ok", run.get("ok", 0)) <= 0:
        gate(1, "DATA", "inconclusive", "no successful tick in this run")
    elif run.get("csv_status") != "ok":
        gate(1, "DATA", "warn", f"samples.csv degraded ({run.get('csv_status')})")
    else:
        gate(1, "DATA", "pass", f"{run.get('ticks', 0)} ticks, csv ok")

    # 2 COVERAGE -- denominator is the REQUESTED period, never the measured one.
    # Using the measured period makes coverage self-fulfilling: a run that falls
    # 12x behind inflates its own denominator and still scores ~1.0.
    cov = run.get("coverage")
    if cov is None or cov < T_COVERAGE_WARN:
        gate(2, "COVERAGE", "inconclusive", f"coverage={cov} below "
             f"{T_COVERAGE_WARN}")
    elif cov < T_COVERAGE_OK:
        gate(2, "COVERAGE", "warn", f"coverage={cov} below {T_COVERAGE_OK}")
    else:
        gate(2, "COVERAGE", "pass", f"coverage={cov}")

    # 3 CADENCE
    cad = run.get("cadence_ratio")
    if cad is None:
        gate(3, "CADENCE", "inconclusive", "no interval measured")
    elif cad > T_CADENCE_RATIO:
        gate(3, "CADENCE", "warn", f"cadence={cad}x requested period")
    else:
        gate(3, "CADENCE", "pass", f"cadence={cad}x")

    # 4 TIMEOUT
    exp = max(1, run.get("expected", 1))
    frac = run.get("timeout", 0) / exp
    if frac > T_TIMEOUT_FRAC_FAIL:
        gate(4, "TIMEOUT", "fail", f"{frac:.1%} of ticks exceeded the PC budget")
    elif frac > T_TIMEOUT_FRAC:
        gate(4, "TIMEOUT", "warn", f"{frac:.1%} of ticks exceeded the PC budget")
    else:
        gate(4, "TIMEOUT", "pass", f"{frac:.1%} timeouts")

    # 5 ENVELOPE
    t_ms = run.get("t_ms") or 0
    dur = run.get("duration_sec") or 0
    if not (INTERVAL_MIN_SEC * 1000 <= t_ms <= INTERVAL_MAX_SEC * 1000):
        gate(5, "ENVELOPE", "warn", f"interval {t_ms}ms outside supported range")
    elif dur > DURATION_MAX_SEC:
        gate(5, "ENVELOPE", "warn", f"duration {dur}s above {DURATION_MAX_SEC}s")
    else:
        gate(5, "ENVELOPE", "pass", f"interval={t_ms}ms duration={dur}s")

    # 6 REBOOT -- a reboot is a fact, not a fault, but the run MUST NOT be
    # aggregated across it: /proc/stat and the GPU counters restart from zero.
    reboots = dev.get("reboots") or []
    rolls = dev.get("rollovers") or 0
    if reboots:
        gate(6, "REBOOT", "warn",
             f"{len(reboots)} reboot(s); deltas were re-baselined at each one")
    elif rolls:
        gate(6, "REBOOT", "warn", f"{rolls} counter rollover(s) with no reboot")
    else:
        gate(6, "REBOOT", "pass", "no reboot detected")

    # 7 MEMORY
    slope = mem.get("slope_pct_per_h")
    delta = mem.get("delta_pct")
    avail_min = mem.get("avail_min_mb")
    span_s = (run.get("span_ms") or 0) / 1000.0
    trend_usable = span_s >= T_MEM_MIN_SPAN_S
    if not trend_usable:
        slope = None          # recorded in the report, just not actionable
    if avail_min is not None and avail_min < T_OOM_AVAIL_MB:
        gate(7, "MEMORY", "fail",
             f"MemAvailable fell to {avail_min}MB (OOM precursor)")
    elif (slope is not None and delta is not None
          and slope > T_LEAK_SLOPE_PCT_PER_H and delta > T_LEAK_DELTA_PCT):
        gate(7, "MEMORY", "fail",
             f"sustained rise slope={slope}%/h delta={delta}% over "
             f"{mem.get('blocks_valid')}/{MEM_BLOCKS} blocks")
    elif slope is not None and slope > T_LEAK_SLOPE_PCT_PER_H:
        gate(7, "MEMORY", "warn",
             f"rising but total delta only {delta}% (below {T_LEAK_DELTA_PCT}%)")
    elif not mem.get("n"):
        gate(7, "MEMORY", "inconclusive", "no memory readings")
    elif not trend_usable:
        gate(7, "MEMORY", "pass",
             f"delta={delta}% n={mem.get('n')} - trend window not reached "
             f"(needs {T_MEM_MIN_SPAN_S:.0f}s, have {span_s:.0f}s), "
             f"slope not judged")
    else:
        gate(7, "MEMORY", "pass",
             f"delta={delta}% slope={slope}%/h over "
             f"{mem.get('blocks_valid')}/{MEM_BLOCKS} blocks")

    # 8 CPU -- only meaningful once a delta pair was actually possible. The first
    # foreground read can only ever establish a baseline, so a run that reached
    # SLOW exactly once has nothing to measure and must not be warned about.
    # Counted on n_due_ok, not n_due: an offline stretch is not evidence that the
    # foreground chain is broken, and must not be reported as one.
    fg_due = met.get("fg_cpu", {}).get("n_due_ok", 0)
    fg_n = met.get("fg_cpu", {}).get("n_fresh", 0)
    if fg_n == 0 and fg_due < 2:
        gate(8, "CPU", "pass", f"only {fg_due} foreground read(s) - none expected")
    elif fg_n == 0:
        gate(8, "CPU", "warn",
             f"foreground CPU due {fg_due}x but never produced a value")
    else:
        gate(8, "CPU", "pass", f"fg_cpu n={fg_n}")

    # 9 APP -- only meaningful when a package is being watched. Without watch_pkg
    # the script has no basis to call a focus change abnormal (navigation, screens
    # savers and OSD popups all steal focus legitimately).
    #
    # Three outcomes. The third is the one the pre-v2.9.0 code got wrong in BOTH
    # directions: it called "never showed up" a loss (a false FAIL on a run that
    # started on the launcher, or on a package that is not installed on this
    # device) while missing "died in place" (a false PASS on the one failure
    # watch_pkg exists to catch).
    #
    #   gone / pid_dead -> the watched app really did break  -> fail
    #   never seen      -> nothing to judge                  -> inconclusive
    #   otherwise       -> it held up                        -> pass
    wset = fg.get("watch_pkg")
    wgone = fg.get("gone") or 0
    wdead = fg.get("pid_dead") or 0
    if wgone or wdead:
        # watch_pkg must be set for either counter to move, so reaching here is
        # always a hard failure - there is no "no watch_pkg" case to soften it.
        bits = []
        if wgone:
            bits.append(f"{wgone} loss(es) of watched package {wset}")
        if wdead:
            bits.append(f"{wdead} observation(s) with {wset} still focused "
                        f"but its process gone")
        gate(9, "APP", "fail", "; ".join(bits))
    elif wset and not fg.get("seen"):
        gate(9, "APP", "inconclusive",
             f"watched package {wset} never reached the foreground during "
             f"this run - not installed on this device, or never launched")
    else:
        gate(9, "APP", "pass", f"switches={fg.get('switches', 0)} "
             f"pkgs={len(fg.get('pkgs') or [])}")

    # Insufficient-observability floor. A short or empty run has no opinion; it
    # must never be reported as OK, and it is not a failure either.
    ticks = run.get("ticks", 0)
    span_s = (run.get("span_ms") or 0) / 1000.0
    if ticks < 5 or span_s < 30.0:
        gates.append({"id": 0, "name": "DURATION", "status": "inconclusive",
                      "detail": f"ticks={ticks} span={span_s:.1f}s too short"})

    order = {"pass": 0, "warn": 1, "fail": 2, "inconclusive": 3}
    caps = [g for g in gates if g["status"] != "pass"]
    worst = max((order[g["status"]] for g in gates), default=0)
    result = ["OK", "WARN", "FAIL", "INCONCLUSIVE"][worst]
    if run.get("partial") and result == "OK":
        result = "WARN"
        caps.append({"id": -1, "name": "PARTIAL", "status": "warn",
                     "detail": "interrupted snapshot - no final verdict was reached"})
    if not CALIBRATED:
        caps.append({"id": -2, "name": "UNCALIBRATED", "status": "warn",
                     "detail": "thresholds are placeholders, not measured values"})

    return {
        "result": result,
        "judge_version": JUDGE_VERSION,
        "calibrated": CALIBRATED,
        "gates": gates,
        "capped_by": [f"{g['id']}={g['status']}({g['name']})" for g in caps],
        "display": _display(ev, result, gates),
    }


def _display(ev: dict, result: str, gates: list[dict]) -> dict:
    """Pre-render the verdict block from evidence. Kept inside judge() so that
    formatting can never drift from the numbers it describes."""
    run = ev.get("run", {})
    met = ev.get("metrics", {})
    mem = ev.get("memory", {})
    dev = ev.get("device", {})
    fg = ev.get("fg", {})
    evs = ev.get("events", {})
    by = evs.get("by_type") or {}

    def q(v, unit="", digits=None):
        """Format a value with its unit, or a bare n/a when there is no reading."""
        if v is None:
            return "n/a"
        if digits is not None and isinstance(v, float):
            v = round(v, digits)
        return f"{v}{unit}"

    if fg.get("gone") or fg.get("pid_dead"):
        app_verdict = "GONE"
    elif fg.get("watch_pkg") and not fg.get("seen"):
        app_verdict = "NEVER SEEN"
    elif fg.get("switches"):
        app_verdict = "CHANGED"
    else:
        app_verdict = "STABLE"

    leak = result == "FAIL" and (mem.get("slope_pct_per_h") or 0) > 0
    other_events = sum(v for k, v in by.items()
                       if k not in ("timeout", "offline_start", "reboot"))

    return {
        "run": f"ticks={run.get('ticks', 0)} ok={run.get('ok', 0)} "
               f"coverage={(run.get('coverage') or 0) * 100:.0f}% "
               f"cadence={q(run.get('cadence_ratio'))}x",
        "data": f"samples.csv={run.get('samples_csv') or 'n/a'} "
                f"events={evs.get('total', 0)} judge_version={JUDGE_VERSION}",
        "device": f"{dev.get('serial')} ({dev.get('dev_short')})",
        "memory": f"{'LEAK SUSPECT' if leak else 'NO LEAK'}  "
                  f"delta={q(mem.get('delta_pct'), '%')}  "
                  f"slope={q(mem.get('slope_pct_per_h'), '%/h')}  "
                  f"blocks={mem.get('blocks_valid', 0)}/{MEM_BLOCKS}  "
                  f"n={mem.get('n', 0)}  "
                  f"avail_min={q(mem.get('avail_min_mb'), 'MB')}",
        "cpu": f"avg={q(met.get('cpu', {}).get('avg'), '%')}  "
               f"p95={q(met.get('cpu', {}).get('p95'), '%')}  "
               f"max={q(met.get('cpu', {}).get('max'), '%')}  "
               f">={T_CPU_P95_BUSY:.0f}% for "
               f"{q(ev.get('cpu', {}).get('ge90_s'), 's')} (load, not a fault)",
        "app": f"{app_verdict}  switches={fg.get('switches', 0)}  "
               f"pkgs={len(fg.get('pkgs') or [])}  gone={fg.get('gone', 0)}  "
               f"pid_lost={fg.get('pid_lost', 0)}  "
               f"pid_dead={fg.get('pid_dead', 0)}  "
               f"watch={fg.get('watch_pkg') or '(unset)'}",
        "events": f"timeout={run.get('timeout', 0)} "
                  f"offline={run.get('offline', 0)} "
                  f"reboot={len(dev.get('reboots') or [])} "
                  f"other={other_events}",
        # Non-passing gates carry their reason inline. Without it the block
        # says "9=inconclusive(APP)" and the answer is only in the report -
        # which is exactly the abstraction the notes block was added to kill.
        "gates": " ".join(f"{g['id']}={g['status']}({g['name']}): "
                          f"{g.get('detail') or '-'}"
                          for g in gates if g["status"] != "pass")
                 or "all pass",
        "duty": f"cost_median={q(run.get('cost_ms', {}).get('p50'), 'ms')} "
                f"duty={q(run.get('duty_pct'), '%')}",
    }


# --- human-facing copy -----------------------------------------------------
# Everything above here is machine formatting. This table is what BOTH the
# closing console block and the HTML report are rendered from, so the two
# surfaces cannot drift apart - the same reason _display() lives inside judge().
#
# The Chinese strings in this section are DATA for a human-facing surface, the
# same exception PARAMS labels already are. They are never printed: stdout stays
# pure ASCII, the Chinese only ever lands inside the HTML file.
ROW_SPEC = [
    # key into verdict["display"], ASCII label, Chinese label, deciding gate ids
    ("result", "RESULT", "综合判定", None),
    ("run",    "RUN",    "采样概况", (1, 2, 3)),
    ("data",   "DATA",   "数据落盘", (1,)),
    ("device", "DEVICE", "设备", ()),
    ("memory", "MEMORY", "内存 / 泄漏", (7,)),
    ("cpu",    "CPU",    "CPU 占用", (8,)),
    ("app",    "APP",    "前台应用", (9,)),
    ("events", "EVENTS", "链路事件", (4, 5, 6)),
    ("gates",  "GATES",  "未通过的门", None),
    ("duty",   "COST",   "监控开销", ()),
]

ROW_NOTES = {
    "result": (
        "worst status across all gates. OK=all passed, WARN=suspicious but not "
        "a fault, FAIL=a gate failed, INCONCLUSIVE=too little data to have an "
        "opinion",
        "所有门取最差结论：OK=全部通过；WARN=有疑点但不算故障；FAIL=有门未通过；"
        "INCONCLUSIVE=数据太少，不足以判断"),
    "run": (
        "coverage = ticks that returned data / ticks attempted (>=80% is "
        "healthy). cadence = median interval / the interval you asked for; "
        "1.0x is on time",
        "coverage=采到数据的周期占尝试周期的比例（≥80% 才算健康）；"
        "cadence=实测中位间隔÷设定间隔，1.0x 表示准时"),
    "data": (
        "the per-tick data file this run wrote, plus the threshold-set version "
        "it was judged with",
        "本次写出的逐拍数据文件，以及判定所用的阈值版本 judge_version"),
    "device": (
        "adb serial; the short form is what the archived folder is named after",
        "设备序列号；括号内为归档目录命名所用的短名"),
    "memory": (
        "delta = first block -> last block. slope = %/h fitted over the whole "
        "run. A leak needs BOTH over threshold, so the two are supposed to "
        "disagree sometimes",
        "delta=首尾内存区块的增长；slope=全程拟合的每小时增长率。两者必须同时超阈"
        "才判为泄漏，所以 slope 高而 delta 低时不会报警——这是设计如此，不是矛盾"),
    "cpu": (
        "share of all cores. Sustained high CPU is the point of a stress test, "
        "never a fault",
        "占全部核心的百分比。持续高 CPU 是压测的目的，不构成异常"),
    "app": (
        "STABLE = one foreground app throughout. CHANGED = it switched "
        "(navigation and screen savers do that). GONE = the watched app really "
        "broke - it left the foreground, or its window stayed up with no "
        "process behind it. NEVER SEEN = it never came to the foreground at "
        "all, so this run has no opinion on it (wrong package? not installed? "
        "never launched?)",
        "STABLE=全程同一个前台应用；CHANGED=发生过切换（导航、屏保都会切，属正常）；"
        "GONE=关注的应用真的坏了——掉出前台，或者窗口还在但进程已经没了；"
        "NEVER SEEN=关注的应用整场没上过前台，本次跑无法对它下结论"
        "（包名不对？没装？没打开？）"),
    "events": (
        "timeout/offline/reboot are faults in the PC<->device link, not faults "
        "in the device",
        "timeout/offline/reboot 是 PC 与设备之间的链路故障，不等于设备故障"),
    "gates": (
        "every gate that did not pass; 'all pass' means nothing was flagged",
        "所有未通过的门及原因；全部通过时显示 all pass"),
    "duty": (
        "cost_median = what one sample cost the PC. duty = that as a share of "
        "the interval, i.e. the load this monitor adds",
        "cost_median=单次采样在 PC 上的耗时；duty=它占采样间隔的比例，"
        "也就是本监控自身带来的负载"),
}

GATE_LEVEL = {"pass": 0, "warn": 1, "fail": 2, "inconclusive": 3}
LEVEL_NAME = ["ok", "warn", "fail", "inconclusive"]
LEVEL_ZH = {"ok": "通过", "warn": "注意", "fail": "异常",
            "inconclusive": "样本不足"}

GATE_ZH = {1: "数据", 2: "覆盖率", 3: "采样节拍", 4: "PC 预算超时", 5: "参数范围",
           6: "重启", 7: "内存", 8: "前台 CPU", 9: "前台应用",
           -1: "中断快照", -2: "阈值未标定"}

METRIC_ZH = {"cpu": "CPU 总占用", "procs": "进程数", "up": "开机时长",
             "mem": "内存占用", "gpu": "GPU 占用", "gpu_clk": "GPU 频率",
             "fg_pkg": "前台包名", "fg_pid": "前台进程号",
             "fg_cpu": "前台应用 CPU"}
METRIC_UNIT = {"cpu": "%", "procs": "个", "up": "s", "mem": "%", "gpu": "%",
               "gpu_clk": "MHz", "fg_pkg": "", "fg_pid": "", "fg_cpu": "%"}

STAT_KEYS = ("n", "min", "avg", "p50", "p90", "p95", "max")

HTML_CSS = """:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 32px 48px; background: #f4f6f8;
  color: #1d2733; font: 14px/1.6 "Microsoft YaHei", "PingFang SC",
  "Noto Sans CJK SC", "Segoe UI", Arial, sans-serif; }
h1 { margin: 0 0 4px; font-size: 20px; }
h2 { margin: 30px 0 10px; font-size: 15px; color: #33475b; }
h2 span { font-weight: 400; color: #7a8b9c; font-size: 13px; }
.sub { color: #67788a; font-size: 13px; margin-bottom: 18px; }
.banner { display: flex; align-items: baseline; gap: 14px; padding: 16px 20px;
  border-radius: 8px; border-left: 6px solid #8a9aa8; background: #fff;
  box-shadow: 0 1px 3px rgba(20,40,60,.09); }
.banner b { font-size: 26px; letter-spacing: .5px; }
.banner .zh { color: #4a5a6a; }
.banner.ok { border-color: #2f9e5f; } .banner.ok b { color: #227a49; }
.banner.warn { border-color: #d98c12; } .banner.warn b { color: #a86a06; }
.banner.fail { border-color: #cf3f3f; } .banner.fail b { color: #a92c2c; }
.banner.inconclusive { border-color: #8a9aa8; } .banner.inconclusive b { color: #55636f; }
table { width: 100%; border-collapse: collapse; background: #fff;
  box-shadow: 0 1px 3px rgba(20,40,60,.09); border-radius: 6px;
  overflow: hidden; }
th, td { padding: 9px 12px; text-align: left; vertical-align: top;
  border-bottom: 1px solid #e6ebf0; }
th { background: #eef2f6; font-weight: 600; color: #33475b; white-space: nowrap;
  font-size: 13px; }
tr:last-child td { border-bottom: 0; }
td.k { font-weight: 600; color: #22303d; white-space: nowrap; }
td.v { font-family: Consolas, "Courier New", monospace; font-size: 13px;
  color: #1d2733; }
td.note { color: #5b6b7c; font-size: 13px; }
td.k small, td.note small { display: block; font-weight: 400;
  color: #8a9aa8; font-size: 11.5px; }
td.num { text-align: right; font-family: Consolas, "Courier New", monospace;
  font-size: 13px; }
.chip { display: inline-block; min-width: 52px; text-align: center;
  padding: 1px 8px; border-radius: 10px; font-size: 12px; color: #fff;
  background: #8a9aa8; }
.chip.ok { background: #2f9e5f; } .chip.warn { background: #d98c12; }
.chip.fail { background: #cf3f3f; } .chip.inconclusive { background: #8a9aa8; }
.foot { margin-top: 26px; color: #7a8b9c; font-size: 12.5px; }
.foot code { color: #4a5a6a; }
.warnbar { margin-top: 18px; padding: 10px 14px; border-radius: 6px;
  background: #fff6e5; border-left: 4px solid #d98c12; color: #7a5405;
  font-size: 13px; }
"""


def verdict_rows(verdict: dict) -> list:
    """One row per indicator, with the level its own gates decided.

    Pure: reads only the verdict, so it is safe to call from the report writer,
    the console formatter and the selftest alike.
    """
    d = verdict.get("display") or {}
    by_id = {g.get("id"): g for g in (verdict.get("gates") or [])}
    out = []
    for key, label, label_zh, gids in ROW_SPEC:
        # "result" is the one row that does not live in display: it is the verdict
        # itself, and _display never copies it.
        if key == "result":
            value = str(verdict.get("result") or "?")
        else:
            value = d.get(key, "")
        # gids is None for the two overview rows (RESULT, GATES): they are coloured
        # by the overall result rather than by a subset of gates.
        if gids is None:
            level = str(verdict.get("result") or "").lower()
            if level not in LEVEL_NAME:
                level = "inconclusive"
        else:
            worst = 0
            for gid in gids:
                g = by_id.get(gid)
                if g:
                    worst = max(worst, GATE_LEVEL.get(g.get("status"), 0))
            level = LEVEL_NAME[worst]
        note_en, note_zh = ROW_NOTES.get(key, ("", ""))
        out.append({"key": key, "label": label, "label_zh": label_zh,
                    "value": value, "level": level,
                    "note_en": note_en, "note_zh": note_zh})
    return out


def metric_stats(ev_dict: dict) -> dict:
    """Per-metric distribution rows. Shared by the console block and the report,
    so the numbers in the HTML are the same ones printed to stdout."""
    out = {}
    metrics = ev_dict.get("metrics") or {}
    for m in METRIC_ORDER:
        src = metrics.get(m) or {}
        row = {k: src[k] for k in STAT_KEYS if k in src}
        if row:
            out[m] = row
    return out


def _note_lines(label: str, text: str, width: int, wrap_at: int = 78) -> list:
    """Label in the left column, the wrapped note in the right one."""
    pad = " " * (width + 3)
    wrapped = textwrap.wrap(text, width=max(20, wrap_at - len(pad))) or [""]
    return [f"{label:<{width}}   {wrapped[0]}"] + [pad + w for w in wrapped[1:]]


def format_result_lines(verdict: dict, report_path: str,
                        stats: dict | None = None,
                        html_path: str | None = None) -> list:
    """Render the closing block. Pure formatting of an already-decided verdict.

    CONSTRAINT: only the LAST line may be pickable as a report path. The server's
    _sniff_report_path takes a line containing " : " whose right-hand side ends in
    ".json" and keeps the FIRST such line. The html line is therefore written with
    "=" rather than ":" - the report is written into the repo's reports/ folder,
    so its path contains the substring "report", and relying on the extension
    check alone to reject it would be one careless edit away from breaking.
    """
    rows = verdict_rows(verdict)
    width = max(len(r["label"]) for r in rows)
    lines = [f"=== perf verdict (judge {verdict.get('judge_version')}) ==="]
    for r in rows:
        lines.append(f"{r['label']:<{width}} : {r['value']}")
    for m, s in (stats or {}).items():
        if s:
            lines.append(f"STAT {m:<8}: min={s['min']} avg={s['avg']} "
                         f"p50={s['p50']} p90={s['p90']} p95={s['p95']} "
                         f"max={s['max']} n={s['n']}")
    lines.append("")
    lines.append("--- what these mean " + "-" * 56)
    for r in rows:
        if r["note_en"]:
            lines.extend(_note_lines(r["label"], r["note_en"], width))
    if html_path:
        lines.append("")
        lines.append(f"  html   = {html_path}")
    lines.append(f"  report : {report_path}")
    return lines


def _esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _pct(v) -> str:
    """A 0..1 ratio from the evidence, as a percentage, or n/a."""
    if not isinstance(v, (int, float)):
        return "n/a"
    return f"{v * 100:.0f}%"


def _hms(clock_ms, full: bool = False) -> str:
    """Epoch milliseconds as a local wall clock ("18:39:12"), or "".

    Every event carries both a run-relative tick (`t_sec`) and the wall clock it
    happened at (`clock_ms`). The tick says WHERE in the run it was; the clock
    says WHEN it was - and that is the part an operator can line up against
    another device's log, a bug ticket, or their own memory of the afternoon.
    Local time on purpose: the report gets read next to the person who was
    watching the device, not next to a UTC reference.

    A missing or nonsensical clock degrades to "" instead of raising - the same
    HTML is also written from a partial snapshot on the way out of a run.
    """
    fmt = "%Y-%m-%d %H:%M:%S" if full else "%H:%M:%S"
    try:
        return time.strftime(fmt, time.localtime(int(clock_ms) / 1000.0))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def format_result_html(payload: dict) -> str:
    """Standalone Chinese HTML report, written next to report.json.

    Deliberately self-contained: no external stylesheet, no script, no chart
    library. The file has to survive being copied out of the archive folder and
    opened on a machine that has never run PPTP, possibly years from now - which
    is also why the numbers are rendered rather than plotted.

    Every figure comes from the same verdict/evidence the console block uses, so
    the two cannot disagree. English text in the source; Chinese in the output.
    """
    verdict = payload.get("verdict") or {}
    cfg = payload.get("config") or {}
    ev = payload.get("evidence") or {}
    stats = payload.get("stats") or {}
    rows = verdict_rows(verdict)

    result = str(verdict.get("result") or "?")
    level = result.lower()
    if level not in LEVEL_NAME:
        level = "inconclusive"
    gates = verdict.get("gates") or []
    run = ev.get("run") or {}
    met = ev.get("metrics") or {}

    interval_ms = cfg.get("interval_ms")
    parts = []

    parts.append(
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>性能监控报告 · {_esc(payload.get('device_id'))}</title>"
        f"<style>{HTML_CSS}</style></head><body>")
    parts.append("<h1>性能监控报告</h1>")
    parts.append(
        '<div class="sub">设备 <b>' + _esc(payload.get("device_id")) + "</b>"
        " · 采集时间 " + _esc(payload.get("test_time"))
        # The run parameters moved into the table below, which comes from
        # PARAMS and therefore cannot drift from what the platform offered.
        + "</div>")

    parts.append(
        f'<div class="banner {level}"><b>{_esc(result)}</b>'
        f'<span class="zh">判定结果 · {_esc(LEVEL_ZH.get(level, ""))}</span>'
        f'<span class="zh">共 {len(gates)} 项检查，'
        f'{sum(1 for g in gates if g.get("status") != "pass")} 项未通过</span>'
        "</div>")

    if not verdict.get("calibrated"):
        parts.append(
            '<div class="warnbar">本报告的判定阈值 '
            f'(<code>{_esc(verdict.get("judge_version"))}</code>) '
            "尚未用一次健康的长跑基线标定，属于设计假设值而非实测值。"
            "阈值偏松会漏报，偏紧会误报 —— 请把结论当作线索，而不是定论。</div>")

    # -- 1. indicator verdict ------------------------------------------------
    parts.append('<h2>一、指标判定 <span>每行结论由它自己那一组检查门决定</span></h2>')
    parts.append("<table><tr><th>指标</th><th>结论</th><th>数值</th>"
                 "<th>说明</th></tr>")
    for r in rows:
        parts.append(
            "<tr>"
            f'<td class="k">{_esc(r["label_zh"])}<br>'
            f'<span style="font-weight:400;color:#8a9aa8">{_esc(r["label"])}</span></td>'
            f'<td><span class="chip {r["level"]}">'
            f'{_esc(LEVEL_ZH.get(r["level"], ""))}</span></td>'
            f'<td class="v">{_esc(r["value"])}</td>'
            f'<td class="note">{_esc(r["note_zh"])}</td>'
            "</tr>")
    parts.append("</table>")

    # -- 2. the parameters this run actually used ----------------------------
    # Generated from PARAMS, so this report and the platform's config modal
    # cannot disagree about what was offered, and a parameter added to
    # PARAMS appears here with no edit to this file.
    parts.append(_pptp_report.render_params_table(
        {"interval_sec": cfg.get("interval_sec"),
         "duration_sec": cfg.get("duration_sec"),
         "watch_pkg": cfg.get("watch_pkg") or "",
         "key_ini": cfg.get("key_ini") or ""},
        PARAMS, num=2))
    # -- 3. distribution -----------------------------------------------------
    keys = ("min", "avg", "p50", "p90", "p95", "max", "n")
    heads = ("最小", "平均", "P50", "P90", "P95", "最大", "样本数")
    parts.append('<h2>三、通道分布 <span>只统计真正采到值的样本，'
                 "零阶保持（沿用上次读数）不计入</span></h2>")
    parts.append("<table><tr><th>指标</th><th>单位</th>"
                 + "".join(f"<th>{h}</th>" for h in heads)
                 + "<th>备注</th></tr>")
    for m in METRIC_ORDER:
        s = stats.get(m)
        if not s:
            continue
        mm = met.get(m) or {}
        note = f"采到 {mm.get('n_fresh', 0)} 拍 / 应采 {mm.get('n_due_ok', 0)} 拍"
        if mm.get("n_held"):
            note += f"，另有 {mm['n_held']} 拍沿用上次读数"
        if not mm.get("n_due_ok"):
            note = "本次未采到值（设备不提供该节点，或运行太短还没轮到它）"
        parts.append(
            "<tr>"
            f'<td class="k">{_esc(METRIC_ZH.get(m, m))}<br>'
            f'<span style="font-weight:400;color:#8a9aa8">{_esc(m)}</span></td>'
            f'<td>{_esc(METRIC_UNIT.get(m, ""))}</td>'
            + "".join(f'<td class="num">{_esc(s.get(k))}</td>' for k in keys)
            + f'<td class="note">{_esc(note)}</td>'
            "</tr>")
    parts.append("</table>")

    # -- 4. every gate -------------------------------------------------------
    parts.append('<h2>四、检查门明细 <span>全部列出，未通过的原因直接写在行内'
                 "</span></h2>")
    parts.append("<table><tr><th>#</th><th>检查</th><th>结论</th>"
                 "<th>原因</th></tr>")
    for g in gates:
        st = g.get("status")
        lv = "ok" if st == "pass" else (st if st in LEVEL_NAME else "warn")
        parts.append(
            "<tr>"
            f'<td class="num">{_esc(g.get("id"))}</td>'
            f'<td class="k">{_esc(GATE_ZH.get(g.get("id"), ""))} '
            f'<span style="font-weight:400;color:#8a9aa8">'
            f'{_esc(g.get("name"))}</span></td>'
            f'<td><span class="chip {lv}">'
            f'{_esc(LEVEL_ZH.get(lv, st))}</span></td>'
            f'<td class="v">{_esc(g.get("detail"))}</td>'
            "</tr>")
    parts.append("</table>")

    # -- 5. link events ------------------------------------------------------
    items = (ev.get("events") or {}).get("items") or []
    parts.append('<h2>五、链路事件 <span>'
                 f'共 {len(items)} 条，按发生时间排列</span></h2>')
    if items:
        parts.append("<table><tr><th>#</th><th>发生时刻</th>"
                     "<th>相对时刻</th><th>类型</th>"
                     "<th>原因</th><th>详情</th></tr>")
        for n, e in enumerate(items, 1):
            # The wall clock leads: "it broke at 18:39:12" is the sentence a
            # reader arrives with. The run-relative tick stays beside it as the
            # secondary reading, and the full date rides along in the tooltip -
            # a run that crosses midnight is otherwise ambiguous.
            parts.append(
                "<tr>"
                f'<td class="num">{n}</td>'
                f'<td class="num" title="{_esc(_hms(e.get("clock_ms"), full=True))}">'
                f'{_esc(_hms(e.get("clock_ms")) or "-")}</td>'
                f'<td class="num">{_esc(e.get("t_sec"))} s</td>'
                f'<td class="k">{_esc(e.get("type"))}</td>'
                f'<td class="note">{_esc(e.get("reason"))}</td>'
                f'<td class="note">{_esc(e.get("detail"))}</td>'
                "</tr>")
        parts.append("</table>")
    else:
        parts.append("<table><tr><td>本次运行没有记录到任何链路事件"
                     "（无超时、无掉线、无重启）。</td></tr></table>")

    parts.append(
        '<div class="foot">'
        f'判定版本 <code>{_esc(verdict.get("judge_version"))}</code>'
        f' · 采样 {_esc(run.get("ticks", 0))} 拍，其中成功 '
        f'{_esc(run.get("ticks_ok", run.get("ok", 0)))} 拍'
        f' · 覆盖率 {_esc(_pct(run.get("coverage")))}'
        f' · 原始数据 <code>{_esc(run.get("samples_csv"))}</code>'
        "<br>数据文件与本次报告同目录同名前缀，可直接用表格软件打开。"
        "</div>")

    parts.append("</body></html>")
    return "".join(parts)




# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------
class CsvSink:
    """Append-only samples.csv. The handle is held open for the whole run and
    flushed per tick, so a hard kill still leaves every completed row on disk.

    If the file is locked (Excel holding it open is the normal cause) the sink
    degrades instead of failing: raw per-tick capture is lost, but the verdict is
    computed from in-memory evidence and still stands. The degradation is recorded
    and forces the result down to WARN - a silently downgraded evidence chain is
    the one outcome that must not happen.
    """

    def __init__(self, path: str, preamble: list):
        self.path = path
        self.status = "ok"
        self.handle = None
        self.writer = None
        self.rows = 0
        self._open(preamble)

    def _open(self, preamble):
        """preamble items are either a raw comment string or a list of cells."""
        try:
            self.handle = open(self.path, "a", newline="", encoding="utf-8")
            self.writer = csv.writer(self.handle, lineterminator="\n")
            for item in preamble:
                if isinstance(item, str):
                    self.handle.write(item + "\n")
                else:
                    self.writer.writerow(item)
            self.handle.flush()
        except Exception as e:
            self.status = f"csv_degraded ({type(e).__name__})"
            self.handle = None
            self.writer = None

    def write(self, row: list) -> float:
        """Returns milliseconds spent writing (its own contribution to overhead)."""
        if self.writer is None:
            return 0.0
        t0 = time.perf_counter()
        try:
            self.writer.writerow(row)
            self.handle.flush()
            self.rows += 1
        except Exception as e:
            self.status = f"csv_degraded ({type(e).__name__})"
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None
            self.writer = None
        return (time.perf_counter() - t0) * 1000.0

    def close(self, footer: str):
        if self.handle is None:
            return
        try:
            if footer:
                self.handle.write(footer)
                self.handle.flush()
            self.handle.close()
        except Exception:
            pass
        self.handle = None
        self.writer = None


class KeyInjector:
    """Press keys on the device on a schedule, from an ir_sequences/*.ini.

    A long playback run dies on the app's own idle prompt ("are you still
    watching?"). One press per interval keeps it away. The interval is the .ini's
    `delay_ms`, deliberately not a parameter: two sources for one schedule would
    drift apart, and the place to retune it is the file named in the report.

    WHY A THREAD AND NOT A SUBPROCESS. The platform stops a task with
    CTRL_BREAK_EVENT, which a child process would indeed receive - but its
    force-stop path is proc.kill() (TerminateProcess), which sends no console
    control event at all. A child process would survive that and keep pressing
    keys on the device until the server next restarted and reaped it. A daemon
    thread dies with this process, so that failure mode cannot happen.

    WHY run_step AND NOT run_loop. run_loop loops forever with no exit, and its
    only escape is a KeyboardInterrupt raised in its own thread - which never
    arrives, because Windows delivers a console control event to the main thread
    only. Driving run_step from here instead buys an interruptible wait, an exact
    press count for the report, and no presses during teardown.

    Nothing here can change the verdict. An unreachable device, a malformed .ini
    or an unknown key name land in `status` (and thus in the report's config) and
    the measurement run carries on untouched.

    A press that fails once does NOT kill the keepalive - see _run.
    """

    def __init__(self, device: str, ini_rel: str, root: str):
        self.device = device
        self.ini_rel = ini_rel
        self.root = root
        self.sent = 0
        self.failed = 0
        self.n_steps = 0
        self.status = "off"
        self._stop = threading.Event()
        self._thread = None
        self._steps: list = []
        self._ir = None

    def start(self) -> None:
        """Load the .ini and begin pressing. Never raises."""
        try:
            path = self.ini_rel
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(self.root, path))
            self._steps = list(ir_runner.SequenceConfig(path).steps)
            if not self._steps:
                self.status = "failed: sequence is empty"
                print(f"[key] not started: {self.status}")
                return
            self.n_steps = len(self._steps)
            self._ir = ir_runner.IRRemote(
                event_path=ir_runner.resolve_event_path(None))
        except Exception as e:
            # A wrong path or a malformed ini is a configuration mistake, not a
            # reason to abandon a run that is measuring the device correctly.
            self.status = f"failed: {type(e).__name__}: {e}"
            print(f"[key] not started: {self.status}")
            return

        self.status = "running"
        # Announced here rather than by the caller, and before the thread starts:
        # the moment that thread runs it prints its own `  -> <KEY>` line for the
        # first press, and two unsynchronised prints would land on one line.
        print(f"[key] keepalive on: {self.ini_rel} ({self.n_steps} step(s); "
              f"pressing now, then every step's delay_ms)")
        print()
        self._thread = threading.Thread(target=self._run, name="key-inject",
                                       daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """One press, then delay_ms; repeat until stopped.

        run_step prints its own `  -> <KEY>` line per press - that is the
        heartbeat in the console, and the only per-press output on purpose.

        Repeats are driven from here instead of being handed to run_step as a
        single `count` because run_step's own repeat loop cannot be interrupted:
        a stop would let a burst press on to its end, and the whole step's presses
        would then count either all or none. One press per call makes the count
        exact and the stop immediate. delay_ms stays the gap between presses,
        exactly as run_step and run_loop pace it - so with the single-step
        keepalive ini it is simply the press interval.
        """
        pending = None       # a press error not yet attributed to either cause
        while not self._stop.is_set():
            for step in self._steps:
                # A count=1 copy: which adb channel and which press length to use
                # stays entirely run_step's business. count=0 presses nothing, the
                # same as run_step.
                one = ir_runner.SequenceStep(step.index, step.code, step.action,
                                             step.delay_ms, 1,
                                             step.long_duration_ms)
                for _ in range(step.count):
                    if self._stop.is_set():
                        return
                    try:
                        ir_runner.run_step(self._ir, one, self.device)
                        self.sent += 1
                    except Exception as e:
                        # Two very different things look identical right here. A
                        # press can be cut short by the very CTRL_BREAK that stops
                        # the task (that event reaches the whole process group,
                        # adb.exe included, so it comes back as a failure with
                        # empty stderr), and a press can also fail on its own: the
                        # device is busy or briefly offline, or adb is restarted
                        # under us. The stop flag does not separate them - adb.exe
                        # dies before our main thread notices, so it is still clear
                        # at this instant. The wait after this press does: a real
                        # fault is followed by the normal interval, a stop
                        # truncates it. So say it now (an 8-hour run should not
                        # learn of a press problem 30 minutes late) but attribute
                        # it only once that wait has run its course.
                        pending = e
                        print(f"[key] press error, will retry next interval: "
                              f"{type(e).__name__}: {e}")
                    if self._stop.wait(step.delay_ms / 1000.0):
                        return   # the stop: `pending` was that stop, drop it
                    if pending is not None:
                        # The interval ran out with the task still going, so the
                        # keepalive survived it. Giving up here would leave the
                        # rest of the run with no keepalive at all - the wrong
                        # trade for one missed press. Count it, try again.
                        self.failed += 1
                        pending = None

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the thread to finish, without waiting forever for it.

        A press in flight is an adb client with no timeout of its own, so the
        join is bounded and giving up is the right outcome: the thread is a
        daemon and the process is about to exit regardless. Same reasoning as
        adb_run's give-up branch.
        """
        if self.status == "running":
            self.status = "stopped"
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def atomic_write_json(path: str, payload: dict) -> None:
    """Write via a temp file + os.replace.

    A direct write that is interrupted (force-stop mid-serialisation) leaves a
    truncated file that parses as garbage. Replace is atomic on Windows for files
    on the same volume, so a reader sees either the old file or the new one.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, separators=(",", ":"))
    os.replace(tmp, path)


def atomic_write_text(path: str, text: str) -> None:
    """The text sibling of atomic_write_json - same temp file + os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def build_report(ev_dict: dict, verdict: dict, cfg: dict, sources: dict,
                 device: str) -> dict:
    return {
        "schema": "pptp-perf-report/2",
        "test_name": "性能监控 (CPU/GPU/内存/前台APP) + 判定",
        "device_id": device,
        "test_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": cfg,
        "sources": sources,
        "judge_version": JUDGE_VERSION,
        "stats": metric_stats(ev_dict),
        "verdict": verdict,
        "evidence": ev_dict,
    }


# ---------------------------------------------------------------------------
# Probe modes
# ---------------------------------------------------------------------------
def _probe_raw(serial: str, name: str, cmd: str) -> str:
    _rc, out, _err = adb_shell(serial, cmd, 15.0)
    out = out.replace("\r", "")
    first = out.strip().splitlines()[0].strip()[:70] if out.strip() else "(empty)"
    print(f"[probe] {name:<10} : {first}")
    return out


def run_probe(serial: str) -> int:
    rc, _o, _e = adb_shell(serial, "true", 5.0)
    if rc != 0:
        print("[probe] device not reachable via adb")
        return 1
    print(f"[probe] device serial    = {serial}")
    for key in ("ro.soc.manufacturer", "ro.soc.model", "ro.hardware",
                "ro.hardware.egl"):
        print(f"[probe] {key:<19} = {getprop(serial, key) or '(empty)'}")
    print("--- node readability ---")
    st = parse_proc_stat(_probe_raw(serial, "cpu", "cat /proc/stat"))
    print(f"[probe] cpu parse    : {'OK' if st else 'FAIL'}")
    mt = parse_meminfo(_probe_raw(serial, "mem", "cat /proc/meminfo"))
    print(f"[probe] mem parse    : "
          f"{'OK (total=%dkB avail=%dkB)' % mt if mt else 'FAIL'}")
    up = parse_uptime(_probe_raw(serial, "uptime", "cat /proc/uptime"))
    print(f"[probe] uptime parse : {'OK (%.1fs)' % up if up else 'FAIL'}")
    procs = parse_procs(_probe_raw(serial, "procs", "set -- /proc/[0-9]*; echo $#"))
    print(f"[probe] procs parse  : {'OK (%s)' % procs if procs is not None else 'FAIL'}")
    gtxt = _probe_raw(serial, "gpu", f"timeout -k 2 5 su 0 cat "
                       "/sys/kernel/debug/mali0/dvfs_utilization "
                       "/sys/kernel/debug/mali0/gpu_clock")
    gd, clk = parse_gpu_both(gtxt)
    print(f"[probe] gpu parse    : "
          f"{'OK (busy=%d idle=%d clk=%s)' % (gd[0], gd[1], clk) if gd else 'FAIL'}")
    fg_cmd = ("F=$(dumpsys window 2>/dev/null | grep -m1 mFocusedApp); echo \"$F\"; "
              "P=${F##*u0 }; P=${P%%/*}; pidof \"$P\"; "
              "for x in $(pidof \"$P\"); do cat /proc/$x/stat; done")
    ftxt = _probe_raw(serial, "fg chain", fg_cmd)
    pkg = parse_focus_pkg(ftxt)
    lines = ftxt.splitlines()
    pids = parse_pidof("\n".join(lines[1:2])) if len(lines) > 1 else []
    pstats = parse_pid_stats(ftxt)
    print(f"[probe] fg_pkg       : {pkg or '(not found)'}")
    print(f"[probe] fg_pid       : {pids or '(none)'}")
    print(f"[probe] fg_cpu stats : "
          f"{ {p: v for p, v in pstats.items()} or '(none)'}")
    transit = measure_transit_ms(serial)
    print(f"[probe] adb transit  : {transit:.1f} ms (self-measured, {3} reps)")
    print(f"[probe] PC budget B  : "
          f"{DEVICE_GUARD_DEFAULT_S + DEVICE_KILL_FANOUT_S + transit / 1000.0 + PC_BUDGET_MARGIN_S:.2f} s "
          f"for device guard G={DEVICE_GUARD_DEFAULT_S:.0f} s")
    if gd:
        print("[probe] result: GPU counter readable via root su - GPU tier enabled")
    else:
        print("[probe] result: GPU counter NOT readable - GPU tier disabled "
              "(re-probed every %ds)" % GPU_REPROBE_INTERVAL_S)
    return 0


def run_probe_cost(serial: str, reps: int = 5) -> int:
    """Re-measure the tiered command costs on THIS device and link, and print the
    duty-cycle table the design's numbers were locked against.

    The published figures (134 / 243 / 409 ms) are USB measurements under load. adb
    over WiFi has never been measured, so this exists to re-anchor them in place
    rather than to restate a promise.
    """
    nonce = "a17f3c"
    variants = [
        ("FAST (v2)", {TIER_FAST}, False),
        ("FAST+MED (v2)", {TIER_FAST, TIER_MED}, True),
        ("FULL (v2)", {TIER_FAST, TIER_MED, TIER_SLOW}, True),
    ]
    print(f"device={serial} reps={reps} guard G={DEVICE_GUARD_DEFAULT_S:.0f}")
    print("(one rep = one adb.exe process + one compound device shell)\n")
    print("%-16s %10s %10s %10s %8s" % ("variant", "median ms", "min ms",
                                        "max ms", "bytes"))
    print("-" * 60)
    med: dict[str, float] = {}
    for label, tiers, gpu in variants:
        ts, nbytes = [], 0
        for _ in range(reps):
            cmd = build_tick_command(nonce, tiers, int(DEVICE_GUARD_DEFAULT_S),
                                     gpu, True)
            t0 = time.perf_counter()
            rc, out, _err = adb_shell(serial, cmd, 30.0)
            ts.append((time.perf_counter() - t0) * 1000.0)
            nbytes = len(out.encode("utf-8", "replace"))
            if rc != 0:
                print(f"[warn] {label}: rc={rc}")
            time.sleep(0.3)
        med[label] = statistics.median(ts)
        print("%-16s %10.1f %10.1f %10.1f %8d"
              % (label, med[label], min(ts), max(ts), nbytes))

    f, m, full = med["FAST (v2)"], med["FAST+MED (v2)"], med["FULL (v2)"]
    print("\nduty cycle (tiers derived from T, integer ms):")
    print("%-8s %-8s %-8s %-30s %10s" % ("T", "MED", "SLOW", "tick mix", "duty"))
    print("-" * 70)
    for t_ms in (1000, 2000, 6000, 30000):
        tp = tier_ticks_per(t_ms)
        total = tp[TIER_SLOW]
        n_med = total // tp[TIER_MED]
        n_fast = total - n_med
        window = n_fast * f + (n_med - 1) * m + full
        mix = f"{n_fast} FAST + {n_med - 1} MED + 1 FULL"
        print("%-8.1f %-8.1f %-8.1f %-30s %9.2f%%"
              % (t_ms / 1000.0, tp[TIER_MED] * t_ms / 1000.0,
                 tp[TIER_SLOW] * t_ms / 1000.0, mix,
                 window / (tp[TIER_SLOW] * t_ms) * 100))
    return 0


# ---------------------------------------------------------------------------
# Selftest -- asserts the invariants the design promises, with no device needed
# ---------------------------------------------------------------------------
def _synthetic_evidence(result_wanted: str) -> dict:
    """Build an evidence dict shaped like a real one. Used only by --selftest."""
    # The leak series has to outrun T_MEM_MIN_SPAN_S, otherwise gate 7 is
    # deliberately inert (a short run cannot separate a leak from noise) and the
    # case tests nothing. 1000 x 2s = 2000s > 1800s.
    n = 1000 if result_wanted == "leak" else 300
    tp = tier_ticks_per(2000)
    ev = Evidence("TESTDEV", 2000, tp, 0, "", {})
    for i in range(n):
        t = i * 2.0
        if result_wanted == "leak":
            mem = 40.0 + i * 0.05
            avail = 2000.0 - i * 5.0
        elif result_wanted == "oom":
            mem = 60.0 + i * 0.30
            avail = 900.0 - i * 3.0
        else:
            mem = 45.0 + math.sin(i / 10.0)
            avail = 1800.0
        row = {"k": i, "t_sec": t, "cost_ms": 250.0, "late_ms": 3.0,
               "gap_ms": 0.0,
               "_dt_ms": 2000.0, "mem": round(mem, 2), "cpu": 40.0,
               "gpu": 10.0, "gpu_clk": 552, "procs": 400, "fg_cpu": 25.0}
        states = {m: ST_FRESH for m in METRIC_ORDER}
        ev.ingest(row, states, "ok", (4000000, int(avail * 1024)),
                  (100, 100), (10, 90), 8000.0 + t, "com.test.app", [1234])
    if result_wanted == "short":
        ev2 = Evidence("TESTDEV", 2000, tp, 0, "", {})
        ev2.ingest({"k": 0, "t_sec": 0.0, "cost_ms": 250.0, "late_ms": 0.0,
                    "gap_ms": 0.0,
                    "_dt_ms": 2000.0, "mem": 45.0, "cpu": 40.0, "gpu": 10.0,
                    "gpu_clk": 552, "procs": 400, "fg_cpu": 25.0},
                   {m: ST_FRESH for m in METRIC_ORDER}, "ok",
                   (4000000, 1800 * 1024), (100, 100), (10, 90), 8000.0,
                   "com.test.app", [1234])
        return ev2.freeze("ok", "x.samples.csv", False)
    if result_wanted == "degraded":
        ev.csv_status = "csv_degraded (PermissionError)"
    if result_wanted == "reboot":
        ev.reboots.append({"t_sec": 100.0, "before_s": 8000.0, "after_s": 40.0})
        # Also an event row: the report has to render an event's wall clock, and
        # this is the one synthetic case that raises one.
        ev.add_event(100.0, SELFTEST_EVENT_CLOCK_MS, "reboot",
                     "uptime went backwards", "8000.0s -> 40.0s")
    return ev.freeze("ok", "perf_TESTDEV_samples.csv", False)


def _synthetic_watch_evidence(focus: list, watch_pkg: str) -> dict:
    """Evidence for a run whose foreground observations are a given list.

    `focus` is one (pkg, pid_set) pair per SLOW observation - the foreground
    chain lives on the SLOW tier and nowhere else, so ticks between SLOW
    boundaries are fed fg_pkg=None, exactly as Monitor.tick feeds them. Used
    only by --selftest.
    """
    t_ms = 2000
    ticks_per = tier_ticks_per(t_ms)
    slow = ticks_per[TIER_SLOW]
    # Keep the run past the 5-tick / 30-s insufficient-observability floor, or
    # the DURATION gate answers for the whole report and the status of gate 9
    # stops being readable on its own.
    ticks = max(slow * (len(focus) + 1), 31)
    ev = Evidence("FAKEDEV", t_ms, ticks_per, 0, watch_pkg,
                  {"cpu": "stat", "mem": "meminfo", "gpu": None, "fg": "proc"})
    for i in range(ticks):
        idx = i // slow
        fg, pids = (focus[idx] if (i % slow == 0 and idx < len(focus))
                    else (None, []))
        states = {m: ST_DISABLED for m in METRIC_ORDER}
        ev.ingest({"k": i, "t_sec": float(i * 2), "cost_ms": 100.0, "late_ms": 0.0},
                  states, "ok", None, None, None, None, fg, pids)
    return ev.freeze("completed", "selftest.csv", False)


def run_selftest() -> int:
    fails: list[str] = []

    def check(name, cond, detail=""):
        print(f"[selftest] {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  ({detail})" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    # --- invariants that must hold for EVERY report -------------------------
    for kind in ("healthy", "leak", "oom", "reboot", "degraded", "short"):
        ev = _synthetic_evidence(kind)
        v1 = judge(ev)
        v2 = judge(json.loads(json.dumps(ev)))
        check(f"{kind}: judge is deterministic", v1 == v2)
        rep = build_report(ev, v1, {}, {}, "TESTDEV")
        check(f"{kind}: judge(report['evidence']) == report['verdict']",
              judge(rep["evidence"]) == rep["verdict"])
        ids = sorted(g["id"] for g in v1["gates"] if g["id"] > 0)
        check(f"{kind}: gates 1..9 all present", ids == list(range(1, 10)),
              str(ids))

        # The html path is a real one from the repo, because the report line's
        # own vulnerability comes from `reports/` containing the substring
        # "report" - a sanitised path here would test nothing.
        lines = format_result_lines(
            v1, "E:\\ProjectorPressureTest\\reports\\perf_x.json",
            {"cpu": {"n": 1, "min": 1, "max": 1, "avg": 1, "p50": 1,
                     "p90": 1, "p95": 1}},
            "E:\\ProjectorPressureTest\\reports\\perf_x.html")

        def sniff(line):
            """Mirror of server._sniff_report_path - the one rule that matters."""
            if " : " not in line or "report" not in line:
                return None
            cand = line.rsplit(" : ", 1)[1].strip()
            return cand if cand.lower().endswith(".json") else None

        hits = [n for n, l in enumerate(lines) if sniff(l)]
        check(f"{kind}: exactly one sniffable report path, on the last line",
              hits == [len(lines) - 1], f"hits={hits} of {len(lines)}")
        last = lines[-1]
        check(f"{kind}: last line is the report path",
              last.rsplit(" : ", 1)[1].strip().endswith(".json"))
        check(f"{kind}: html line is not sniffable",
              " : " not in [l for l in lines if ".html" in l][0])

    # --- the foreground chain is reachable -----------------------------------
    # The command has to ASK for the sections, and the predicate has to ACCEPT
    # them. Either one alone leaves the channel dead, and a dead channel looks
    # exactly like a short run from the outside.
    all_tiers = {TIER_FAST, TIER_MED, TIER_SLOW}
    slow_cmd = build_tick_command("abc123", all_tiers, 8, True)
    check("build_tick_command asks for the foreground chain",
          all(f"@@{n}:abc123" in slow_cmd for n in ("FOCUS", "FGPKG", "PIDSTAT")),
          slow_cmd[:200])
    check("build_tick_command drops the chain without SLOW",
          "@@FOCUS:abc123" not in build_tick_command(
              "abc123", {TIER_FAST, TIER_MED}, 8, True))
    fake_secs = {"FOCUS": "mFocusedApp=x", "FGPKG": "42", "PIDSTAT": "1 (x) S 1"}
    check("section_due accepts the foreground chain on a SLOW tick",
          all(section_due(n, fake_secs, all_tiers, True)
              for n in ("FOCUS", "FGPKG", "PIDSTAT")))
    check("section_due rejects the foreground chain without SLOW",
          not section_due("FOCUS", fake_secs, {TIER_FAST, TIER_MED}, True))
    check("section_due rejects an absent section",
          not section_due("FOCUS", {}, all_tiers, True))
    check("section_due gates GPU on gpu_enabled",
          section_due("GPU", {"GPU": "x"}, all_tiers, True)
          and not section_due("GPU", {"GPU": "x"}, all_tiers, False))

    # Every row must carry a value. RESULT read from the wrong dict was invisible
    # in the HTML (the banner still showed it) and only showed up as a blank line
    # in the console block, so it gets its own assertion.
    vr = verdict_rows(judge(_synthetic_evidence("healthy")))
    check("verdict rows all carry a value",
          all(r["value"] for r in vr),
          str([r["label"] for r in vr if not r["value"]]))
    check("verdict rows cover every display key",
          {r["key"] for r in vr if r["key"] != "result"}
          == {k for k in judge(_synthetic_evidence("healthy"))["display"]},
          str({r["key"] for r in vr if r["key"] != "result"}
              ^ set(judge(_synthetic_evidence("healthy"))["display"])))

    # --- the html report ----------------------------------------------------
    for kind in ("healthy", "leak", "short"):
        ev = _synthetic_evidence(kind)
        v = judge(ev)
        page = format_result_html({
            "device_id": "TESTDEV", "test_time": "2026-09-14 00:00:00",
            "config": {"interval_ms": 2000, "duration_sec": 0,
                       "watch_pkg": "com.netflix.ninja"},
            "verdict": v, "stats": metric_stats(ev), "evidence": ev})
        check(f"html({kind}): is a complete document",
              page.startswith("<!DOCTYPE html>") and page.endswith("</html>"))
        check(f"html({kind}): carries the result",
              v["result"] in page)
        check(f"html({kind}): no unrendered format placeholder",
              "{_" not in page and "%s" not in page and "None" not in page)
        check(f"html({kind}): every declared indicator has a row",
              all(r["label_zh"] in page for r in verdict_rows(v)))
        check(f"html({kind}): every gate is listed",
              all(str(g["name"]) in page for g in v["gates"]))
        check(f"html({kind}): utf-8 declared",
              'charset="utf-8"' in page)

    # An event row must carry the wall clock, not only the run-relative tick.
    # Built from the "reboot" case, the one synthetic evidence that raises an
    # event; the expected string is derived through the same timezone the
    # renderer uses, so this passes anywhere.
    ev_rb = _synthetic_evidence("reboot")
    v_rb = judge(ev_rb)
    page_rb = format_result_html({
        "device_id": "TESTDEV", "test_time": "2026-09-14 00:00:00",
        "config": {"interval_ms": 2000, "duration_sec": 0,
                   "watch_pkg": "com.netflix.ninja"},
        "verdict": v_rb, "stats": metric_stats(ev_rb), "evidence": ev_rb})
    want_hms = _hms(SELFTEST_EVENT_CLOCK_MS)
    want_date = _hms(SELFTEST_EVENT_CLOCK_MS, full=True)
    check("hms: formats an epoch clock as HH:MM:SS",
          len(want_hms) == 8 and want_hms.count(":") == 2, want_hms)
    check("hms: full form carries the date",
          want_date.startswith(time.strftime(
              "%Y-%m-%d", time.localtime(SELFTEST_EVENT_CLOCK_MS / 1000.0))),
          want_date)
    check("hms: a broken clock degrades to empty, never raises",
          _hms(None) == "" and _hms("nonsense") == "")
    check("html: event row shows the wall clock", want_hms in page_rb, want_hms)
    check("html: event row tooltip shows the full date", want_date in page_rb,
          want_date)
    check("html: event row keeps the run-relative tick", "100.0 s" in page_rb)
    check("html: the wall-clock column is announced", "发生时刻" in page_rb)
    # The same HTML is written from a partial snapshot on the way out of a run,
    # so a clock-less event must degrade rather than raise.
    ev_nc = _synthetic_evidence("reboot")
    for e in ev_nc["events"]["items"]:
        e.pop("clock_ms", None)
    page_nc = format_result_html({
        "device_id": "TESTDEV", "test_time": "2026-09-14 00:00:00",
        "config": {"interval_ms": 2000, "duration_sec": 0,
                   "watch_pkg": "com.netflix.ninja"},
        "verdict": judge(ev_nc), "stats": metric_stats(ev_nc),
        "evidence": ev_nc})
    check("html: a clock-less event degrades to a dash, not a crash",
          'title="">-</td>' in page_nc)

    # --- expected outcomes --------------------------------------------------
    check("healthy -> OK",
          judge(_synthetic_evidence("healthy"))["result"] == "OK",
          judge(_synthetic_evidence("healthy"))["result"])
    check("leak -> FAIL",
          judge(_synthetic_evidence("leak"))["result"] == "FAIL",
          judge(_synthetic_evidence("leak"))["result"])
    check("oom -> FAIL",
          judge(_synthetic_evidence("oom"))["result"] == "FAIL",
          judge(_synthetic_evidence("oom"))["result"])
    check("short run -> INCONCLUSIVE",
          judge(_synthetic_evidence("short"))["result"] == "INCONCLUSIVE",
          judge(_synthetic_evidence("short"))["result"])
    check("csv degraded caps at WARN",
          judge(_synthetic_evidence("degraded"))["result"] == "WARN",
          judge(_synthetic_evidence("degraded"))["result"])
    check("reboot -> WARN (fact, not fault)",
          judge(_synthetic_evidence("reboot"))["result"] == "WARN",
          judge(_synthetic_evidence("reboot"))["result"])

    # a partially-finished run must never be reported OK
    ev = _synthetic_evidence("healthy")
    ev["run"]["partial"] = True
    check("partial snapshot never OK", judge(ev)["result"] != "OK")

    # --- wire protocol ------------------------------------------------------
    for s in ("ok:fffhhhhhh", "ok:fffffffff", "offline:xxxhhhhhh",
              "timeout:nnnnhhnnn", "partial:fffxxhhhh", "ok:bbbnnnnnn"):
        check(f"st line well formed: {s}", bool(ST_LINE_RE.match(s)))
    check("st rejects 8 chars", not ST_LINE_RE.match("ok:ffffffff"))
    check("st rejects bad state char", not ST_LINE_RE.match("ok:fffffffz"))

    # --- scheduler ----------------------------------------------------------
    for t_ms in range(1000, 60001, 100):
        tp = tier_ticks_per(t_ms)
        ok = (tp[TIER_SLOW] % tp[TIER_MED] == 0 and tp[TIER_MED] >= 1)
        if not ok:
            check(f"tier nesting at T={t_ms}ms", False, str(tp))
            break
    else:
        check("tier nesting holds for T in [1.0, 60.0] step 0.1", True)
    tp = tier_ticks_per(2000)
    check("T=2000 -> MED every 3 ticks, SLOW every 15",
          (tp[TIER_MED], tp[TIER_SLOW]) == (3, 15), str(tp))

    # --- parsers against real captured device output ------------------------
    real_stat = ("12066 (i.video.speaker) S 5585 5585 0 0 -1 4194624 281882 2276 "
                 "591 0 14692 6881 2 7 10 -10 249 0 2790620")
    check("pid stat parses utils+stime (utime=14692 stime=6881)",
          parse_pid_stats(real_stat) == {12066: 21573},
          str(parse_pid_stats(real_stat)))
    check("focus pkg parses despite spaces-free comm truncation",
          parse_focus_pkg("mFocusedApp=ActivityRecord{59ae4c6 u0 "
                          "com.netflix.ninja/.MainActivity t204}")
          == "com.netflix.ninja")
    check("gpu_clk reads the standalone integer, not a busy_time digit",
          parse_gpu_both("busy_time: 1423 idle_time: 88771\n552\n") ==
          ((1423, 88771), 552),
          str(parse_gpu_both("busy_time: 1423 idle_time: 88771\n552\n")))

    # --- fg_cpu must leave `baseline` on the SECOND read ---------------------
    # This is a regression guard: fg_cpu is SLOW-tiered, so it gets two readings a
    # full SLOW period apart, and the first can only establish a baseline. The
    # original code advanced the stored baseline only when the comparison had
    # already succeeded, which is never true on the first call - so the metric sat
    # at `baseline` for entire runs (found on a real device, not by these tests).
    probe = Monitor.__new__(Monitor)
    probe.fg_prev, probe.fg_prev_pids, probe.fg_prev_ms = None, set(), 0
    v1, s1 = probe._fg_delta({12066: 21573}, 1_000_000)
    check("fg_cpu first read is a baseline", (v1, s1) == (None, ST_BASELINE),
          f"{v1} {s1}")
    v2, s2 = probe._fg_delta({12066: 21773}, 1_030_000)
    # 200 ticks over 30s == 200/30 % of one core
    check("fg_cpu second read yields a value",
          s2 == ST_FRESH and v2 is not None and abs(v2 - 200 / 30.0) < 0.05,
          f"{v2} {s2}")

    # --- section framing ----------------------------------------------------
    nonce = "a17f3c"
    out = (f"@@STAT:{nonce}\ncpu 1 2 3 4 5 6 7\n@@MEM:{nonce}\nMemTotal: 1 kB\n"
           f"@@CHATTER_NOT_MINE\n@@DONE:{nonce}\n")
    secs, mis = split_sections(out, nonce)
    check("nonce framing keeps foreign @@ lines out of sections",
          set(secs) == {"STAT", "MEM", "DONE"} and mis == 1,
          f"{set(secs)} mis={mis}")
    check("foreign @@ line does not leak into the previous section",
          "CHATTER" not in secs.get("MEM", ""))

    # --- gate 9: the watched app -------------------------------------------
    # Regression guard for a defect that made gate 9 wrong in BOTH directions.
    # It called "the app never showed up" a LOSS - a false FAIL on a run that
    # started on the launcher, or on a package that is not installed on this
    # device - while being blind to the app dying in place (focused window, no
    # process behind it), a false PASS on the one failure watch_pkg exists to
    # catch. Neither was visible to any other check here, because every other
    # check asked about SHAPE: the CSV is rectangular, the report round-trips,
    # there are nine gates. None asked whether a channel ever produced anything.
    YT = "com.google.android.youtube.tv"
    LAUNCHER = "com.google.android.apps.tv.launcherx"

    def watch_case(name, focus, watch, want):
        out = _synthetic_watch_evidence(focus, watch)
        g9 = next(g for g in judge(out)["gates"] if g["id"] == 9)
        f = out["fg"]
        check(f"gate 9 {name}",
              g9["status"] == want,
              f"want {want}, got {g9['status']}: {g9['detail']} "
              f"(seen={f['seen']} gone={f['gone']} pid_dead={f['pid_dead']})")

    watch_case("passes when the watched app holds the foreground",
               [(YT, [123])] * 4, YT, "pass")
    watch_case("fails when the watched app leaves the foreground",
               [(YT, [123]), (LAUNCHER, [9]), (LAUNCHER, [9])], YT, "fail")
    watch_case("is inconclusive when the watched app never appears",
               [(LAUNCHER, [9])] * 4, YT, "inconclusive")
    watch_case("is inconclusive when the package is not on the device",
               [(LAUNCHER, [9])] * 4, "com.example.not.installed", "inconclusive")
    watch_case("fails when the app is focused but its process is gone",
               [(YT, [123]), (YT, []), (YT, [])], YT, "fail")

    # The two counters that make the distinction possible must be in the
    # evidence, not just in memory: the gate reads the frozen dict, and a
    # counter that never reaches it is a counter that cannot influence anything.
    _w = _synthetic_watch_evidence([(YT, [123]), (LAUNCHER, [9])], YT)["fg"]
    check("fg evidence carries seen/pid_dead",
          _w.get("seen") is True and _w.get("pid_dead") == 0, str(_w))

    # An unset watch_pkg must never produce a loss: without it the script has no
    # basis to call any focus change abnormal.
    _u = _synthetic_watch_evidence(
        [(YT, [123]), (LAUNCHER, [9]), (LAUNCHER, [9])], "")["fg"]
    check("no watch_pkg -> no losses",
          _u["gone"] == 0 and _u["seen"] is False, str(_u))

    # --- the key injector's parameter ---------------------------------------
    # The injector itself needs a device, but the one part of it that can be
    # wrong without anyone noticing is the choice list the platform renders: a
    # list that has drifted from the folder would offer a path that starts
    # nothing, which looks exactly like a keepalive that is working.
    _ki = next(f for f in PARAMS if f["name"] == "key_ini")
    check("key_ini is off by default",
          _ki["default"] == "" and _ki["choices"][0].get("value") == "",
          str(_ki["choices"][0]))
    _offered = {c["value"] for c in _ki["choices"]}
    _on_disk = {""} | {
        f"ir_sequences/{n}" for n in
        os.listdir(os.path.join(PROJECT_ROOT, "ir_sequences"))
        if n.endswith(".ini") and not n.startswith("_")}
    check("key_ini offers every real ir_sequences/*.ini and no temp ones",
          _offered == _on_disk, str(sorted(_offered ^ _on_disk)))

    print(f"\n[selftest] {len(fails)} failure(s)")
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, serial: str, interval_sec: float, duration_sec: int,
                 watch_pkg: str, report_dir: str):
        self.serial = serial
        self.dev_short = serial.replace(":", "_").replace(".", "_")
        self.watch_pkg = watch_pkg
        # Set by main() only once the device is confirmed reachable, and left
        # None when no key_ini was configured. Read by _write_reports.
        self.keys = None
        self.duration_sec = duration_sec
        self.t_ms = max(int(INTERVAL_MIN_SEC * 1000),
                        min(int(INTERVAL_MAX_SEC * 1000),
                            int(round(interval_sec * 1000))))
        self.ticks_per = tier_ticks_per(self.t_ms)
        self.report_dir = report_dir
        self.ts = time.strftime("%Y%m%d_%H%M%S")
        stem = f"perf_{self.dev_short}_{self.ts}"
        self.csv_name = f"{stem}.samples.csv"
        self.events_name = f"{stem}.events.csv"
        self.report_name = f"{stem}.json"
        self.report_path = os.path.join(report_dir, self.report_name)
        self.html_name = f"{stem}.html"
        self.html_path = os.path.join(report_dir, self.html_name)
        self.csv_path = os.path.join(report_dir, self.csv_name)
        self.events_path = os.path.join(report_dir, self.events_name)

        self.origin_ms: int | None = None
        self.prev_start_ms: int | None = None
        self.prev_dt_ms: int | None = None
        self.guard_s = DEVICE_GUARD_DEFAULT_S
        self._grow_streak = 0
        self.transit_ms = TRANSIT_FALLBACK_MS

        self.cpu_prev: tuple[int, int] | None = None
        self.gpu_prev: tuple[int, int] | None = None
        self.fg_prev: dict[int, int] | None = None
        self.fg_prev_pids: set[int] = set()
        self.fg_prev_ms = 0
        self.up_prev: float | None = None
        self.hold: dict[str, object] = {}

        self.gpu_enabled = False
        self.gpu_fail_streak = 0
        self.gpu_next_probe_s = 0.0

        self.ev: Evidence | None = None
        self.csv: CsvSink | None = None
        self.events_csv = None
        self.events_writer = None
        self.last_snapshot_s = -1e9
        self.interrupted = False
        self.start_mono = 0.0
        self.t_start_ms = 0

    # -- helpers ------------------------------------------------------------
    def _now_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def _elapsed_s(self) -> float:
        return time.monotonic() - self.start_mono

    def _pc_budget_s(self) -> float:
        return (self.guard_s + DEVICE_KILL_FANOUT_S
                + self.transit_ms / 1000.0 + PC_BUDGET_MARGIN_S)

    def _adapt_guard(self) -> None:
        """Device-side guard from the TYPICAL tick cost, never from the p95.

        Sizing the guard on the p95 of a window that includes the very outliers it
        is meant to catch makes it grow toward the anomaly: a timed-out tick enters
        the window, the budget rises, the next hang is tolerated for longer. p90 of
        successful ticks only, and growth is damped because shrinking is safe and
        growing is not.
        """
        window = [c for c in self.ev.cost_ms if c > 0][-BUDGET_WINDOW:]
        if not window:
            return
        base = statistics.median(window) if len(window) < 5 else pct(window, 90)
        need = math.ceil(max(DEVICE_GUARD_MIN_S,
                             min(DEVICE_GUARD_MAX_S, 5.0 * base / 1000.0)))
        if need <= self.guard_s:
            self.guard_s = need
            self._grow_streak = 0
        else:
            self._grow_streak += 1
            if self._grow_streak >= BUDGET_GROW_STREAK:
                self.guard_s = min(self.guard_s + 1, need)
                self._grow_streak = 0

    def _probe_gpu(self) -> bool:
        cmd = (f"timeout -k 2 {int(self.guard_s)} su 0 cat "
               "/sys/kernel/debug/mali0/dvfs_utilization "
               "/sys/kernel/debug/mali0/gpu_clock")
        _rc, out, _err = adb_shell(self.serial, cmd, self._pc_budget_s())
        gd, _clk = parse_gpu_both(out)
        return gd is not None

    # -- lifecycle ----------------------------------------------------------
    def setup(self) -> bool:
        rc, _o, _e = adb_shell(self.serial, "true", 5.0)
        if rc != 0:
            return False
        self.transit_ms = measure_transit_ms(self.serial)
        gpu_ok = self._probe_gpu()
        self.gpu_enabled = gpu_ok
        sources = {
            "cpu": "/proc/stat",
            "mem": "/proc/meminfo",
            "gpu": "/sys/kernel/debug/mali0" if gpu_ok else None,
            "fg": "dumpsys window + pidof",
        }
        self.ev = Evidence(self.serial, self.t_ms, self.ticks_per,
                           self.duration_sec, self.watch_pkg,
                           sources)
        try:
            os.makedirs(self.report_dir, exist_ok=True)
        except Exception as e:
            print(f"[warn] cannot create {self.report_dir}: {e}")
        self.csv = CsvSink(self.csv_path, self._csv_preamble())
        try:
            self.events_csv = open(self.events_path, "a", newline="",
                                   encoding="utf-8")
            self.events_writer = csv.writer(self.events_csv,
                                            lineterminator="\n")
            self.events_writer.writerow(["t_sec", "clock_ms", "type", "reason",
                                         "detail"])
            self.events_csv.flush()
        except Exception as e:
            print(f"[warn] events.csv unavailable: {e}")
            self.events_csv = None
            self.events_writer = None

        soc = getprop(self.serial, "ro.soc.model") or "unknown"
        print(f"[perf] sources: cpu={sources['cpu']} mem={sources['mem']} "
              f"gpu={sources['gpu'] or 'n/a'} fg={sources['fg'] or 'off'}")
        print(f"[perf] tiers: FAST=1 MED={self.ticks_per[TIER_MED]} "
              f"SLOW={self.ticks_per[TIER_SLOW]} ticks "
              f"(T={self.t_ms}ms)  adb transit={self.transit_ms:.0f}ms")
        print("PERF|" + json.dumps(
            {"type": "meta", "sources": sources, "device": soc,
             "judge_version": JUDGE_VERSION}, ensure_ascii=True))
        return True

    def _csv_preamble(self) -> list[str]:
        """Comment block + header, written as the first rows of samples.csv.

        The comments carry the config and the raw counter origin, so the file can
        be re-read years later without the report beside it.
        """
        ev = self.ev
        tp = self.ticks_per
        return [
            f"# PPTP perf samples v2 | script=perf_monitor.py "
            f"| judge_version={JUDGE_VERSION}",
            f"# device={self.serial}",
            f"# config=interval_ms={self.t_ms},duration_sec={self.duration_sec},"
            f"watch_pkg={self.watch_pkg or '(unset)'}",
            f"# tiers=FAST:1,MED:{tp[TIER_MED]},SLOW:{tp[TIER_SLOW]}",
            f"# metrics={','.join(METRIC_ORDER)}",
            "# sources=cpu=stat,mem=meminfo,gpu=mali/dvfs,fg=window+pidof",
            f"# counters_t0={ev.start_iso}",
            CSV_COLUMNS,
        ]

    def _emit(self, kind: str, reason: str, detail: str = "",
              immediate: bool = True) -> None:
        t = self._elapsed_s()
        clock = int(time.time() * 1000)
        e = self.ev.add_event(t, clock, kind, reason, detail, immediate)
        if e is None:
            return
        if self.events_writer is not None:
            try:
                self.events_writer.writerow([e["t_sec"], e["clock_ms"], kind,
                                             reason, detail])
                self.events_csv.flush()
            except Exception:
                pass
        print("PERF|" + json.dumps({"type": "event", "clock": clock,
                                    "t": e["t_sec"], "kind": kind,
                                    "reason": reason, "detail": detail},
                                   ensure_ascii=True))

    # -- snapshot / finalisation -------------------------------------------
    def _write_reports(self, ev_dict: dict, verdict: dict, tag: str) -> None:
        """Write the JSON report and its HTML twin.

        The HTML is a render of the JSON, not a second opinion: it is built from
        the same payload, so a failure to write it can never change the verdict.
        Both are best-effort - a read-only reports/ folder must not kill a run
        that is otherwise fine.
        """
        payload = build_report(ev_dict, verdict, {
            "interval_sec": self.t_ms / 1000.0,
            "interval_ms": self.t_ms,
            "duration_sec": self.duration_sec,
            "watch_pkg": self.watch_pkg or None,
            # Recorded, not judged: keys are a test-harness aid, so they must not
            # move the verdict. The count is what tells a long unattended run
            # whether the keepalive was alive (it ticks up in every snapshot).
            "key_ini": self.keys.ini_rel if self.keys else None,
            "key_sent": self.keys.sent if self.keys else 0,
            "key_failed": self.keys.failed if self.keys else 0,
            "key_status": self.keys.status if self.keys else "off",
        }, self.ev.sources, self.serial)
        try:
            atomic_write_json(self.report_path, payload)
        except Exception as e:
            print(f"[warn] {tag} failed: {e}")
        try:
            atomic_write_text(self.html_path, format_result_html(payload))
        except Exception as e:
            print(f"[warn] {tag} html failed: {e}")

    def _snapshot(self, partial: bool, status: str) -> None:
        ev_dict = self.ev.freeze(status, self.csv_name, partial)
        verdict = judge(ev_dict)
        self._write_reports(ev_dict, verdict, "snapshot")

    def snapshot_due(self) -> None:
        t = self._elapsed_s()
        if t - self.last_snapshot_s >= SNAPSHOT_INTERVAL_S:
            self.last_snapshot_s = t
            self._snapshot(True, "running")

    def finish(self, status: str) -> dict:
        self.ev.end_iso = datetime.now().isoformat(timespec="seconds")
        if self.csv is not None:
            self.ev.csv_status = self.csv.status
        footer = (f"# end_of_run={self.ev.end_iso} ticks={self.ev.k} "
                  f"ticks_ok={self.ev.res_counts['ok']} "
                  f"run_wall_s={self._elapsed_s():.1f} status={status}\n")
        if self.csv is not None:
            self.csv.close(footer)
        if self.events_csv is not None:
            try:
                self.events_csv.close()
            except Exception:
                pass
            self.events_csv = None
            self.events_writer = None
        ev_dict = self.ev.freeze(status, self.csv_name, False)
        verdict = judge(ev_dict)
        self._write_reports(ev_dict, verdict, "final save")
        return {"evidence": ev_dict, "verdict": verdict}

    # -- the tick -----------------------------------------------------------
    def tick(self, k: int) -> dict:
        """One sample. Returns the CSV row dict (also used to build the wire line)."""
        tgt_ms = self.origin_ms + k * self.t_ms
        now = self._now_ms()
        if now < tgt_ms:
            time.sleep((tgt_ms - now) / 1000.0)
        start_ms = self._now_ms()
        late_ms = start_ms - tgt_ms
        gap_ms = 0.0
        if self.prev_start_ms is not None:
            gap_ms = max(0.0, start_ms - self.prev_start_ms - self.t_ms)
        self.prev_start_ms = start_ms
        t_sec = round((start_ms - self.origin_ms) / 1000.0, 1)

        tiers = {t for t, n in self.ticks_per.items() if (k % n) == 0}
        ev_mark = len(self.ev.events)     # events raised by THIS tick
        nonce = os.urandom(3).hex()
        cmd = build_tick_command(nonce, tiers, int(self.guard_s),
                                 self.gpu_enabled)
        rc, out, err = adb_shell(self.serial, cmd, self._pc_budget_s())
        cost_ms = self._now_ms() - start_ms

        secs, misparse = split_sections(out, nonce)
        if misparse:
            self._emit("misparse", f"{misparse} foreign @@ line(s) this tick")

        # --- classify the tick --------------------------------------------
        if rc is None:
            res = "timeout"
        elif rc != 0:
            res = "offline" if OFFLINE_RE.search(err or "") else "error"
        elif not secs and tiers:
            res = "offline" if not (out or "").strip() else "error"
        else:
            res = None      # decided below from the section outcomes

        # --- parse every due section (order: parse -> up -> guards -> deltas) ---
        vals: dict[str, object] = {}
        states: dict[str, str] = {}
        sec_fail = 0
        sec_ok = 0
        # `disabled` means config or the degradation policy switched the metric
        # off. It must NOT be used for "not due this tick" - that is `held` /
        # `not_due`. Conflating them makes every non-due tick look like a broken
        # channel and hides the real ones.
        for m in ("fg_pkg", "fg_pid", "fg_cpu"):
            states[m] = ST_NOT_DUE if m not in self.hold else ST_HELD

        # No per-section opt-out here any more. The foreground chain used to be
        # gated on a track_foreground flag; collapsing that flag away once left
        # `name not in (FOCUS, FGPKG, PIDSTAT)` behind - an INVERTED predicate that
        # silently switched the whole channel off for a whole run while every gate
        # still reported pass. Tier membership is the only condition it needs.
        def due_sec(name: str) -> bool:
            return section_due(name, secs, tiers, self.gpu_enabled)

        # up first: the reboot guard must run before any delta is taken
        up_val = None
        if due_sec("UP"):
            up_val = parse_uptime(secs["UP"])
            if up_val is None:
                states["up"] = section_error_kind(secs["UP"])
                sec_fail += 1
            else:
                sec_ok += 1
                states["up"] = ST_FRESH
                self._reboot_guard(up_val, t_sec)
        else:
            states["up"] = ST_NOT_DUE if self.up_prev is None else ST_HELD
        if up_val is not None:
            self.up_prev = up_val

        cpu_raw = None
        if due_sec("STAT"):
            cpu_raw = parse_proc_stat(secs["STAT"])
            if cpu_raw is None:
                states["cpu"] = section_error_kind(secs["STAT"])
                sec_fail += 1
            else:
                sec_ok += 1
                vals["cpu"], states["cpu"] = self._delta(
                    "cpu", cpu_raw, self.cpu_prev)
                self.cpu_prev = cpu_raw
        else:
            states["cpu"] = ST_NOT_DUE if self.cpu_prev is None else ST_HELD

        if due_sec("PROCS"):
            p = parse_procs(secs["PROCS"])
            if p is None:
                states["procs"] = section_error_kind(secs["PROCS"])
                sec_fail += 1
            else:
                sec_ok += 1
                vals["procs"], states["procs"] = p, ST_FRESH
        else:
            states["procs"] = ST_NOT_DUE if "procs" not in self.hold else ST_HELD

        mem_info = None
        if due_sec("MEM"):
            mem_info = parse_meminfo(secs["MEM"])
            if mem_info is None:
                states["mem"] = section_error_kind(secs["MEM"])
                sec_fail += 1
            else:
                sec_ok += 1
                vals["mem"] = round((1.0 - mem_info[1] / mem_info[0]) * 100, 1)
                states["mem"] = ST_FRESH
        else:
            states["mem"] = ST_NOT_DUE if "mem" not in self.hold else ST_HELD

        if due_sec("GPU"):
            gd, clk = parse_gpu_both(secs["GPU"])
            if gd is None:
                states["gpu"] = section_error_kind(secs["GPU"])
                states["gpu_clk"] = states["gpu"]
                sec_fail += 1
                self._gpu_degraded(t_sec)
            else:
                sec_ok += 1
                self.gpu_fail_streak = 0
                if clk is not None:
                    vals["gpu_clk"], states["gpu_clk"] = clk, ST_FRESH
                else:
                    states["gpu_clk"] = ST_FAILED if "gpu_clk" not in self.hold \
                        else ST_HELD
                vals["gpu"], states["gpu"] = self._delta(
                    "gpu", gd, self.gpu_prev)
                self.gpu_prev = gd
        elif self.gpu_enabled:
            states["gpu"] = ST_NOT_DUE if self.gpu_prev is None else ST_HELD
            states["gpu_clk"] = ST_NOT_DUE if "gpu_clk" not in self.hold else ST_HELD
        else:
            states["gpu"] = ST_DISABLED
            states["gpu_clk"] = ST_DISABLED

        fg_pkg = None
        if due_sec("FOCUS"):
            fg_pkg = parse_focus_pkg(secs["FOCUS"])
            if fg_pkg is None:
                states["fg_pkg"] = section_error_kind(secs["FOCUS"])
                sec_fail += 1
            else:
                sec_ok += 1
                vals["fg_pkg"], states["fg_pkg"] = fg_pkg, ST_FRESH
        if due_sec("FGPKG"):
            pids = parse_pidof(secs["FGPKG"])
            if not pids:
                states["fg_pid"] = (ST_NOT_DUE if "fg_pid" not in self.hold
                                    else ST_HELD) if fg_pkg is None else ST_FAILED
                if fg_pkg is not None:
                    sec_fail += 1
            else:
                sec_ok += 1
                vals["fg_pid"], states["fg_pid"] = min(pids), ST_FRESH
        if due_sec("PIDSTAT"):
            pstats = parse_pid_stats(secs["PIDSTAT"])
            if not pstats:
                states["fg_cpu"] = ST_FAILED if fg_pkg is not None else ST_NOT_DUE
                if fg_pkg is not None:
                    sec_fail += 1
            else:
                sec_ok += 1
                pid_set = set(pstats)
                if pid_set != self.fg_prev_pids:
                    if self.fg_prev_pids:
                        self._emit("pid_change", "foreground pid set changed",
                                   f"{sorted(self.fg_prev_pids)} -> {sorted(pid_set)}")
                    self.fg_prev = None
                    self.fg_prev_pids = pid_set
                vals["fg_cpu"], states["fg_cpu"] = self._fg_delta(pstats, start_ms)

        # --- tick result ---------------------------------------------------
        if res is None:
            if sec_fail == 0 and sec_ok > 0:
                res = "ok"
            elif sec_ok > 0:
                res = "partial"
            else:
                res = "error"
        if res == "timeout":
            self._emit("timeout", "PC budget expired",
                       f"budget={self._pc_budget_s():.2f}s cost={cost_ms}ms")
        elif res == "offline":
            self._emit("offline_start", "device unreachable",
                       (err or "").strip()[:80])
        elif res in ("ok", "partial") and self._was_offline:
            self._emit("offline_end", "device reachable again")

        # --- zero-order hold: a stalled tick is a HOLE, a not-due tick is not ---
        #
        # A failed tick carries NO values, and its state chars say so. Leaving the
        # frozen values in place would draw a flat continuation through an outage,
        # and a flat line reads as "the device settled down" - the exact misreading
        # the `-` cells and gap_ms exist to prevent. Nothing is lost: the last real
        # reading is still the previous CSV row.
        broken = res in ("timeout", "offline", "error")
        out_vals: dict[str, object] = {}
        for m in METRIC_ORDER:
            s = states.get(m, ST_DISABLED)
            if broken and s != ST_DISABLED:
                states[m] = ST_FAILED
                s = ST_FAILED
            if s == ST_FRESH and vals.get(m) is not None:
                self.hold[m] = vals[m]
                out_vals[m] = vals[m]
            elif s == ST_HELD and m in self.hold:
                out_vals[m] = self.hold[m]
            else:
                out_vals[m] = None
        self._was_offline = res == "offline"

        st = res + ":" + "".join(states.get(m, ST_DISABLED) for m in METRIC_ORDER)
        if not ST_LINE_RE.match(st):
            # Cannot happen by construction; if it ever does, degrade to a valid
            # frame rather than killing an 8 h run over a formatting bug.
            st = "error:" + ST_DISABLED * len(METRIC_ORDER)
            self._emit("frame_error", "generated an invalid st frame",
                       "see script bug")

        tick_events = [e["type"] for e in self.ev.events[ev_mark:]]
        row = {"t_sec": t_sec, "clock_ms": int(time.time() * 1000), "res": res,
               "k": k, "cost_ms": round(cost_ms, 1), "flush_ms": 0.0,
               "late_ms": round(late_ms, 1), "gap_ms": round(gap_ms, 1),
               "st": st,
               "_dt_ms": float(start_ms - self.prev_dt_ms) if self.prev_dt_ms
               else None,
               "causes": self._causes(states, res),
               "events": "|".join(tick_events)}
        self.prev_dt_ms = start_ms
        for m in METRIC_ORDER:
            row[m] = out_vals[m]
            row[m + "_st"] = states.get(m, ST_DISABLED)

        # Write the CSV row BEFORE ingesting: flush_ms is itself a cost the report
        # must account for, and it is only known once the row has been written.
        if self.csv is not None:
            row["flush_ms"] = round(self.csv.write(
                [row.get(c, "") for c in CSV_COLUMNS]), 2)
            self.ev.csv_status = self.csv.status

        self.ev.guard_s = self.guard_s
        self.ev.transit_ms = self.transit_ms
        self.ev.ingest(row, states, res, mem_info, cpu_raw,
                       self.gpu_prev if self.gpu_enabled else None,
                       up_val, fg_pkg if fg_pkg is not None else None,
                       sorted(self.fg_prev_pids) if self.fg_prev_pids else [])
        return row

    _was_offline = False

    def _causes(self, states: dict[str, str], res: str) -> str:
        bad = [f"{m}={states[m]}" for m in METRIC_ORDER
               if states.get(m) in (ST_FAILED, ST_ABSENT)]
        return ("res=" + res + ";" + ",".join(bad)) if bad else ("res=" + res)

    def _reboot_guard(self, up_val: float, t_sec: float) -> None:
        """A falling uptime means the device rebooted: every cumulative counter has
        restarted from zero and every delta must be re-baselined, or the next
        subtraction produces a nonsense value (the old code guarded this silently
        and just dropped the sample)."""
        if self.up_prev is not None and up_val < self.up_prev - 60.0:
            self.ev.reboots.append({"t_sec": t_sec,
                                    "before_s": round(self.up_prev, 1),
                                    "after_s": round(up_val, 1)})
            self.ev.rollovers += 1
            self.cpu_prev = None
            self.gpu_prev = None
            self.fg_prev = None
            self._emit("reboot", "device uptime went backwards",
                       f"{self.up_prev:.0f}s -> {up_val:.0f}s")
            self._emit("rollover", "counters re-baselined after reboot",
                       "cpu/gpu/fg", immediate=False)

    def _gpu_degraded(self, t_sec: float) -> None:
        self.gpu_fail_streak += 1
        if self.gpu_enabled and self.gpu_fail_streak >= GPU_DEGRADE_AFTER:
            self.gpu_enabled = False
            self.gpu_next_probe_s = t_sec + GPU_REPROBE_INTERVAL_S
            self._emit("src_degraded", "GPU counter unreadable",
                       f"{self.gpu_fail_streak} consecutive failures")

    def maybe_reprobe_gpu(self, t_sec: float) -> None:
        if self.gpu_enabled or t_sec < self.gpu_next_probe_s:
            return
        if self._probe_gpu():
            self.gpu_enabled = True
            self.gpu_fail_streak = 0
            self.gpu_next_probe_s = 0.0
            # meta.sources may only GROW: shrinking it would make the frontend
            # rebuild the series and discard the good data before the outage.
            src = dict(self.ev.sources)
            src["gpu"] = "/sys/kernel/debug/mali0"
            self.ev.sources = src
            print("PERF|" + json.dumps({"type": "meta", "sources": src,
                                        "judge_version": JUDGE_VERSION},
                                       ensure_ascii=True))
            self._emit("src_recovered", "GPU counter readable again")

    def _delta(self, metric: str, new: tuple[int, int], prev):
        if prev is None:
            return None, ST_BASELINE
        if new[0] < prev[0] or new[1] < prev[1]:
            self.ev.rollovers += 1
            self._emit("rollover", f"{metric} counter went backwards",
                       f"{prev} -> {new}", immediate=False)
            return None, ST_BASELINE
        if metric == "cpu":
            d_total = new[0] - prev[0]
            d_idle = new[1] - prev[1]
            if d_total <= 0:
                return None, ST_FAILED
            return round((1.0 - d_idle / d_total) * 100, 1), ST_FRESH
        d_busy = new[0] - prev[0]
        d_idle = new[1] - prev[1]
        denom = d_busy + d_idle
        if denom <= 0:
            return None, ST_FAILED
        return round(d_busy / denom * 100, 1), ST_FRESH

    def _fg_delta(self, pstats: dict[int, int], start_ms: int):
        """CPU% of the foreground app, summed over every pid pidof returns.

        The divisor is the wall time between the two /proc/<pid>/stat READS, not
        this tick's cost: fg lives on the SLOW tier, so its two readings are a full
        SLOW period apart, and dividing the accumulated ticks by one tick's cost
        would report roughly SLOW-period times too much CPU.
        """
        prev, prev_ms = self.fg_prev, self.fg_prev_ms
        # This reading becomes the baseline for the next one no matter how it
        # compares to the old one. Advancing it only on success deadlocks the
        # metric at `baseline` forever - the first call can never succeed because
        # there is nothing to compare against yet, so the baseline would never be
        # established and fg_cpu would stay null for the whole run.
        self.fg_prev = pstats
        self.fg_prev_ms = start_ms
        if prev is None:
            return None, ST_BASELINE
        common = set(pstats) & set(prev)
        if not common:
            return None, ST_BASELINE
        d_ticks = sum(pstats[p] - prev[p] for p in common)
        if d_ticks < 0:
            return None, ST_BASELINE
        dt_s = (start_ms - prev_ms) / 1000.0
        if dt_s <= 0.001:
            return None, ST_FAILED
        # ticks / USER_HZ = seconds of CPU; as a percentage of one core that is
        # (d_ticks / USER_HZ) / dt_s * 100 == d_ticks / dt_s. Values above 100 mean
        # the app used more than one core, which is correct and meaningful.
        return round(d_ticks / dt_s, 1), ST_FRESH


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    # --dump-params is consumed by the PPTP platform to render the params config
    # modal. Must short-circuit before argparse and before any device interaction.
    if "--dump-params" in sys.argv:
        print(json.dumps({"fields": PARAMS}))
        return 0
    if "--selftest" in sys.argv:
        return run_selftest()

    defaults = {f["name"]: f["default"] for f in PARAMS}
    p = argparse.ArgumentParser(
        description="Device performance monitor with tiered sampling + verdict")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}",
                   help='JSON: {"interval_sec"?, "duration_sec"?, '
                        '"watch_pkg"?, "key_ini"?}')
    p.add_argument("--probe", action="store_true",
                   help="test node readability then exit")
    p.add_argument("--probe-cost", action="store_true",
                   help="re-measure tiered command costs on this device then exit")
    p.add_argument("--probe-reps", type=int, default=5)
    args = p.parse_args()

    if args.probe:
        return run_probe(args.device)
    if args.probe_cost:
        return run_probe_cost(args.device, args.probe_reps)

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    try:
        interval_sec = float(params.get("interval_sec", defaults["interval_sec"]))
    except (TypeError, ValueError):
        interval_sec = float(defaults["interval_sec"])
    try:
        duration_sec = int(params.get("duration_sec", defaults["duration_sec"]))
    except (TypeError, ValueError):
        duration_sec = int(defaults["duration_sec"])
    watch_pkg = str(params.get("watch_pkg", defaults["watch_pkg"]) or "").strip()
    key_ini = str(params.get("key_ini", defaults["key_ini"]) or "").strip()

    # Clamp twice: the platform caches the schema but does not enforce min/max, so
    # a hand-written --params can still arrive out of range.
    interval_sec = max(INTERVAL_MIN_SEC, min(INTERVAL_MAX_SEC, interval_sec))
    duration_sec = max(0, min(DURATION_MAX_SEC, duration_sec))

    print(f"[config] device          = {args.device}")
    print(f"[config] interval_sec    = {interval_sec}")
    print(f"[config] duration_sec    = {duration_sec} "
          f"{'(until manually stopped)' if duration_sec <= 0 else ''}")
    print(f"[config] watch_pkg       = {watch_pkg or '(unset)'}")
    print(f"[config] key_ini         = {key_ini or '(none)'}")

    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test", "perf")
    mon = Monitor(args.device, interval_sec, duration_sec, watch_pkg,
                  report_dir)
    if not mon.setup():
        print("[error] device not reachable via adb")
        return 1

    # Keys start only after the device is confirmed reachable: a device that just
    # failed setup() will not accept them either, and a failed start would only
    # add noise to the log of a run that is about to exit.
    if key_ini:
        mon.keys = KeyInjector(args.device, key_ini, PROJECT_ROOT)
        mon.keys.start()        # announces itself; see KeyInjector.start

    mon.start_mono = time.monotonic()
    mon.origin_ms = mon._now_ms()
    mon.t_start_ms = mon.origin_ms
    k = 0
    try:
        while True:
            if duration_sec > 0 and mon._elapsed_s() >= duration_sec:
                break
            mon.maybe_reprobe_gpu(mon._elapsed_s())
            row = mon.tick(k)
            print("PERF|" + json.dumps({
                "type": "sample", "clock": row["clock_ms"], "t": row["t_sec"],
                "st": row["st"], "gap_ms": row["gap_ms"],
                "cpu": row.get("cpu"), "gpu": row.get("gpu"),
                "mem": row.get("mem"), "fg_cpu": row.get("fg_cpu"),
                "gpu_clk": row.get("gpu_clk"), "fg_pkg": row.get("fg_pkg"),
            }, ensure_ascii=True))
            mon._adapt_guard()
            mon.snapshot_due()
            k += 1
            # Never back-fill missed slots: after a stall, resume at the next
            # nominal tick that is still in the future, not at the next tick in
            # sequence. Otherwise a slow device fires a burst of catch-up ticks
            # and the load it is being measured under spikes.
            now = mon._now_ms()
            if now >= mon.origin_ms + k * mon.t_ms:
                k = (now - mon.origin_ms) // mon.t_ms + 1
    except KeyboardInterrupt:
        mon.interrupted = True
        print("\n[perf] interrupted - computing verdict")

    # Stop the keys before the verdict block, so the report path stays the last
    # line of stdout (format_result_lines) whatever happened above.
    if mon.keys is not None:
        mon.keys.stop()
        print(f"[key] keepalive: {mon.keys.sent} key(s) injected, "
              f"{mon.keys.failed} press error(s), {mon.keys.status}")

    status = "interrupted" if mon.interrupted else "completed"
    out = mon.finish(status)
    if mon.csv is not None and mon.csv.status != "ok":
        status = "csv_degraded"
    print()
    mstats = metric_stats(out["evidence"])
    for line in format_result_lines(
            out["verdict"], mon.report_path,
            {k: v for k, v in mstats.items()
             if k in ("cpu", "gpu", "mem", "fg_cpu")},
            mon.html_path):
        print(line)
    if mon.csv is not None and mon.csv.status != "ok":
        print(f"[warn] samples.csv degraded: {mon.csv.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
