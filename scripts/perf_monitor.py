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
  ntc     : the two iio ADC nodes (/sys/bus/iio/devices/iio:device0/
            in_voltage{3,2}_raw), ONE `su 0 cat` of both. REQUIRES root: the
            mode is 0644 root:root, but this board is SELinux Enforcing and
            the `shell` domain is denied them - so a bare `cat` writes
            `Permission denied` to STDERR and NOTHING to stdout. Degrades to
            disabled if unreadable.
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
import contextlib
import csv
import html
import io
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

SCRIPT_VERSION = "1.4.1"

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

# ---------------------------------------------------------------------------
# WiFi link channel (a CONDITION channel, not a metric)
# ---------------------------------------------------------------------------
# It feeds the EMPTY metrics tuple in SECTIONS, so it never enters METRIC_ORDER,
# the `st` frame, or the per-metric distribution table. Same reasoning as the
# keepalive decision (PERF_MONITOR_V2 12.9 row 4): the link is a condition the
# test ran under, not the object under test. Adding it as a 10th metric is
# forbidden - see docs/DECISIONS.md D-53.
#
# WIFI_TIER is a USER RULING (2026-09-20), not a knob to quietly retune. The
# requirement was only "the report must show WHICH window the network was
# down", and the user chose the cheapest tier knowing the cost. That cost is
# stated in WIFI_RESOLUTION_NOTE and printed in every report; if this line ever
# moves, that note and its number move with it.
SEC_WIFI = "WIFI"
WIFI_TIER = TIER_SLOW
# Printed in every report that carries a wifi timeline. Three %g slots: the
# sampling period, the +- half-width, and the same period again as the
# "shorter than this may be missed entirely" bound. The bound is +-P, NOT +-P/2,
# because a sample is the LAST KNOWN GOOD observation rather than a midpoint of
# the interval it represents: with the first non-ok sample at t1 and the first ok
# sample after it at t2, the true start is in (t1-P, t1] and the true end in
# (t2-P, t2]. Never narrow this to make the report look precise - the number is
# the honest one and it is the ONLY mitigation for the miss-a-short-outage case.
WIFI_RESOLUTION_NOTE = (
    "WiFi is sampled on the SLOW tier: one sample every %g s. An outage window "
    "is therefore bounded to +-%g s, and an outage shorter than %g s can fall "
    "between two consecutive samples and never be seen at all."
)
WIFI_GUARD_S = 2               # `timeout -k 1 2` around the device-side read
# The device-side read, wrapped. Built HERE and used by both the tick command and
# the setup probe so the two can never disagree about what was sampled.
# Unwrapped, a `cmd wifi status` stuck in D state (a wedged system_server) runs
# until the PC budget expires -> rc is None -> res=timeout -> EVERY metric in
# that tick is marked ST_FAILED and the whole tick's data is discarded to report
# one unreadable wifi line. Wrapped, the worst case is one partial tick.
WIFI_CMD = f"timeout -k 1 {WIFI_GUARD_S} cmd wifi status"
# Human-readable source label, in the same spirit as "/proc/stat".
WIFI_SOURCE = "cmd wifi status"
WIFI_DEGRADE_AFTER = 3         # consecutive failed due ticks before disabling
WIFI_REPROBE_INTERVAL_S = 300.0
# The evidence dict is re-serialized on every 120 s snapshot, so it must stay
# O(1)-bounded (12.2). The event LIST is uncapped by design (bounded below by
# the sampling period itself); the window list is not, so it is capped.
WIFI_MAX_WINDOWS = 200
# Every state that means "the link is not usable". `unknown` counts as DOWN on
# purpose: a read that succeeded but could not be classified must never be
# reported as a healthy link, and it must never be reported as a read failure
# either (that is ST_FAILED/ST_ABSENT, which stay out of this tuple).
WIFI_STATES_DOWN = ("off", "noassoc", "noip", "unknown")

# ---------------------------------------------------------------------------
# NTC node temperatures (a CONDITION channel, not a metric)
# ---------------------------------------------------------------------------
# Same shape as WIFI above and for the same reason: the two NTC nodes describe
# the conditions the run happened under, not an object under test, and a 10th
# metric is forbidden (D-53). See D-57.
#
# NTC_TIER is a USER RULING (2026-09-20): the requirement was "a period average
# and a maximum, nothing complicated", and the user picked the SLOW tier knowing
# the cost. There is no resolution note to print here (unlike WIFI) because a
# temperature series has no window bounds to be honest about - but the period
# still means every figure in the report row must carry its own `n`, since most
# ticks hold a zero-order-held value rather than a reading.
#
# The paths are fixed by the board's iio driver (1c020a00.saradc, MTK SAR ADC).
# in_voltage3 is the LCD node and in_voltage2 the LED one - measured, not
# assumed - and THE ORDER BELOW IS THE ORDER THEY ARE CAT-ED IN, so it is
# load-bearing: parse_ntc reads the result positionally.
SEC_NTC = "NTC"
NTC_TIER = TIER_SLOW
NTC_LCD_PATH = "/sys/bus/iio/devices/iio:device0/in_voltage3_raw"
NTC_LED_PATH = "/sys/bus/iio/devices/iio:device0/in_voltage2_raw"
NTC_CHANNELS = ("lcd", "led")
NTC_GUARD_S = 2                # `timeout -k 1 2` around the device-side read
# Read through `su 0`, exactly like the Mali counters below, and NOT because
# of the permission bits: both nodes are mode 0644 root:root. This board runs
# SELinux Enforcing and the `shell` domain is denied `sysfs`, so a bare `cat`
# writes `Permission denied` to STDERR and NOTHING to stdout - which is why
# the console used to show an empty row and a bare FAIL. `su` itself prints
# nothing here (checked), so the framed tick command stays clean.
# The guard stays at NTC_GUARD_S rather than following the device guard the
# way the Mali read does: `su 0` measured +60.6 ms over a bare `cat` (139.5 ->
# 200.1 ms median), which does not move the bound this guard exists to set -
# the timeout only ever bites on a wedged driver.
NTC_CMD = f"timeout -k 1 {NTC_GUARD_S} su 0 cat {NTC_LCD_PATH} {NTC_LED_PATH}"
NTC_SOURCE = "iio in_voltage3/2_raw"
# The ADC -> Celsius conversion is NOT implemented HERE, and that is a
# permanent split rather than a placeholder. The constants it needs - divider
# resistor, B, ADC full scale, per-channel compensation - are per-PROJECT, so
# welding one board's numbers into the collector would silently mismeasure every
# other board. The conversion lives in tools/ntc_convert.py, driven by
# tools/ntc_profiles/<project>.ini. Storing RAW ADC COUNTS is therefore this
# script's contract, not a stopgap, and this unit string is declared in the
# samples.csv header so that no archived file is ever ambiguous about which of
# the two it holds.
#
# (The constants could not have been read off the device anyway:
# /sys/class/ktc_projector/ktc_ntc3/temperature returns the RAW count (662),
# there is no hwmon node, no in_voltage_scale (EINVAL), and no LED-side vendor
# node at all.)
NTC_UNIT = "raw_adc"
NTC_DEGRADE_AFTER = 3          # consecutive failed due ticks before disabling
NTC_REPROBE_INTERVAL_S = 300.0

# ---------------------------------------------------------------------------
# NTC auto-conversion (the DERIVED artifacts)
# ---------------------------------------------------------------------------
# Raw counts are this script's contract (NTC_UNIT above); Celsius and its chart
# are produced AFTER the run by calling tools/ntc_convert.py over the
# samples.csv this run just wrote. No conversion constant is repeated here -
# D-63 forbids that, and every number stays in tools/ntc_profiles/<project>.ini
# where the tool reads it.
#
# What IS decided here is WHICH profile to hand it, because a wrong profile is
# not a crash: it is a plausible-looking temperature computed from another
# board's B value. That is the risk recorded as TODO 6.19 and answered by D-66.
NTC_PROFILE_DEFAULT = "9660_P53_2G"
NTC_PROFILE_DIR = os.path.join(PROJECT_ROOT, "tools", "ntc_profiles")
NTC_CONVERT_TOOL = os.path.join(PROJECT_ROOT, "tools", "ntc_convert.py")
# The tool imports matplotlib, so this covers its interpreter start plus the
# render. Deliberately generous: the budget exists to bound a WEDGED child, not
# to cut short a slow one, and its expiry costs only the two derived files.
NTC_CONVERT_BUDGET_S = 120.0

# ---------------------------------------------------------------------------
# Selectable monitors (per run)
# ---------------------------------------------------------------------------
# ONE source of truth for "which sections does each user-facing monitor own".
# The granularity is a SECTION GROUP, not a metric, and that is forced by the
# device command rather than chosen: GPU must read both of its files in ONE
# `su 0 cat` (two reads cost 251.6 ms vs 129.9 ms), and FOCUS/FGPKG/PIDSTAT share
# one device-side `$P` shell variable. Splitting either group is not a finer
# granularity, it is a different command - slower, or broken.
#
# The FAST trio is deliberately NOT selectable. `up` is the reboot guard AND the
# only reason the cpu%/gpu% deltas survive a reboot (see 12.1), while STAT and
# PROCS are the tick's backbone. FAST is 118 ms of a 460 ms FULL tick, so
# dropping it buys almost nothing and silently corrupts every delta involved -
# a bad trade at any price. Locking it also means gates 5 and 6 can never lose
# their evidence, which is why only gates 7/8/9 need the override in judge().
#
# The Chinese in this table is a PARAMS label, which is the same sanctioned
# exception the rest of them are: it is never printed to stdout, it only ever
# reaches the frontend and the HTML report.
MONITOR_GROUPS = [
    ("mem", "内存", ("MEM",)),
    ("gpu", "GPU 占用", ("GPU",)),
    ("fg", "前台应用", ("FOCUS", "FGPKG", "PIDSTAT")),
    ("wifi", "WiFi 链路", (SEC_WIFI,)),
    ("ntc", "NTC 节点温度", (SEC_NTC,)),
]
MONITOR_IDS = tuple(gid for gid, _zh, _secs in MONITOR_GROUPS)
MONITOR_DEFAULT = list(MONITOR_IDS)
MONITOR_CHOICES = [{"value": gid, "label": zh}
                   for gid, zh, _secs in MONITOR_GROUPS]
MONITOR_ZH = {gid: zh for gid, zh, _secs in MONITOR_GROUPS}


def monitor_tokens(raw) -> list[str]:
    """Normalise the SHAPE of a monitors value into a list of raw tokens.

    Two shapes must work: the browser posts a list, a human on the command line
    would rather write "mem,ntc". Anything else is treated as "not specified".
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",")]
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [str(p).strip() for p in raw]
    return []


def norm_monitors(raw) -> list[str]:
    """Tokens -> the canonical enabled-id list, in MONITOR_GROUPS order.

    Unknown ids are DROPPED rather than fatal: a hand-written --params must not
    be able to abort a run, and an unknown id can only be a typo or a schema
    version skew. The order is forced to MONITOR_GROUPS order so that the
    report's "is this value the declared default?" comparison cannot be defeated
    by a differently ordered list from the browser.

    An input that asked for something but matched NOTHING falls back to the
    DEFAULT, not to the empty set. That direction is deliberate: monitoring too
    much costs a slightly slower tick, whereas monitoring nothing produces a run
    whose every gate is INCONCLUSIVE for a reason the operator never intended.
    """
    tokens = monitor_tokens(raw)
    if not tokens and raw is None:
        return list(MONITOR_DEFAULT)
    matched = {t for t in tokens if t in MONITOR_IDS}
    if tokens and any(tokens) and not matched:
        return list(MONITOR_DEFAULT)
    return [gid for gid in MONITOR_IDS if gid in matched]


def monitors_off_sections(monitors) -> frozenset:
    """The sections to leave OUT of the command, for the enabled monitor ids."""
    keep = set(norm_monitors(monitors))
    return frozenset(s for gid, _zh, secs in MONITOR_GROUPS
                     if gid not in keep for s in secs)


# A gate that lost its evidence to a deselection must never report pass. The
# `fg_due < 2` branch in gate 8 cannot tell "not selected" from "run was too
# short to reach SLOW twice" - before this override existed, deselecting the
# foreground made gate 8 print "only 0 foreground read(s) - none expected" and
# PASS, i.e. the report certified a channel that was never sampled. Only these
# three gates read a selectable channel: 7 memory, 8 CPU, 9 APP.
MON_GATE_OF = {"mem": (7,), "fg": (8, 9)}

# (section marker, tier, metrics it feeds). Command order == this order.
# WIFI and NTC are appended LAST so the byte order of the documented command in
# PERF_MONITOR_V2 12.1 is unchanged for every pre-existing section.
SECTIONS = [
    ("STAT", TIER_FAST, ("cpu",)),
    ("PROCS", TIER_FAST, ("procs",)),
    ("UP", TIER_FAST, ("up",)),
    ("MEM", TIER_MED, ("mem",)),
    ("FOCUS", TIER_SLOW, ("fg_pkg",)),
    ("FGPKG", TIER_SLOW, ("fg_pid",)),
    ("PIDSTAT", TIER_SLOW, ("fg_cpu",)),
    ("GPU", TIER_MED, ("gpu", "gpu_clk")),
    (SEC_WIFI, WIFI_TIER, ()),
    (SEC_NTC, NTC_TIER, ()),
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

def _ntc_profile_choices() -> list:
    """The project profiles tools/ntc_convert.py may be pointed at.

    Same shape and same reason as _key_ini_choices above: a fixed list beats
    free text because a mistyped profile name would convert THIS board's raw
    counts with some other board's constants, and the result looks like a
    temperature rather than like an error.

    Bare names, not paths - --profile takes a name and the tool resolves it
    against its own PROFILE_DIR. Listing is NON-recursive on purpose: the
    vendor's transcribed template sits in templates/ and, having no cold-side B,
    would convert this board under the wrong rule. Hiding it here is the same
    judgement --list-profiles makes at the other end.

    The shipped default is always the first entry, even when the directory is
    missing or empty: a select with no options would post nothing and leave the
    run with no profile at all. Everything after it is sorted.
    """
    choices = [{"value": NTC_PROFILE_DEFAULT, "label": NTC_PROFILE_DEFAULT}]
    try:
        names = sorted(os.listdir(NTC_PROFILE_DIR))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".ini") or name.startswith("_"):
            continue
        stem = name[:-len(".ini")]
        if stem != NTC_PROFILE_DEFAULT:
            choices.append({"value": stem, "label": stem})
    return choices


NTC_PROFILE_CHOICES = _ntc_profile_choices()

PARAMS = [
    {"name": "interval_sec", "label": "采样间隔(秒)", "type": "float",
     "default": 2.0, "min": INTERVAL_MIN_SEC, "max": INTERVAL_MAX_SEC},
    {"name": "duration_sec", "label": "总时长(秒, 0=直到手动停止)", "type": "int",
     "default": 0, "min": 0, "max": DURATION_MAX_SEC},
    {"name": "watch_pkg", "label": "关注的应用(它掉到后台/挂掉才算异常)",
     "type": "select", "choices": WATCH_PKG_CHOICES, "default": ""},
    {"name": "key_ini", "label": "定时发按键的 ini(长跑时防空闲提示)",
     "type": "select", "choices": KEY_INI_CHOICES, "default": ""},
    # Recorded, not judged: the profile only decides how the DERIVED celsius
    # files are computed, never the verdict (D-65). A name the tool cannot
    # resolve is therefore not an error that can move a result - it costs the
    # two temps files and prints the reason.
    {"name": "ntc_profile", "label": "NTC 换算的项目档",
     "type": "select", "choices": NTC_PROFILE_CHOICES,
     "default": NTC_PROFILE_DEFAULT},
    # Multi-select, so the browser posts a LIST. server.py needs no change for
    # that: RunRequest.params is dict[str, Any] and is json.dumps'd straight
    # through to argv, so a list arrives here as a list.
    #
    # Leaving every box unticked is a legitimate value, not an error: it runs the
    # locked FAST trio and nothing else. Deselecting 内存 or 前台应用 costs you
    # the evidence for gates 7/8/9, which is why judge() forces those gates to
    # INCONCLUSIVE rather than letting them report a pass they cannot support.
    {"name": "monitors", "label": "本次监控项(取消勾选=不采集该项)",
     "type": "multiselect", "choices": MONITOR_CHOICES,
     "default": MONITOR_DEFAULT},
]

OFFLINE_RE = re.compile(r"device offline|no devices|device not found|not found",
                        re.I)
ABSENT_MARKERS = ("no such file", "not found", "permission denied",
                  "operation not permitted", "no such device")

# 33 columns. The last two groups are CONDITION channels, not metrics: `wifi`
# is a state word plus its per-tick status char, `ntc_lcd`/`ntc_led` are raw ADC
# counts (see NTC_UNIT) sharing one status char. All of them use the same
# alphabet as every metric, and the `_st` companion is not decoration - without
# it a bare `ok` cannot be told apart from an `ok` that the channel is dead and
# zero-order-holding.
#
# `ntc_st` is ONE column for both channels on purpose. The two counts come from a
# single `cat` in a single tick, so freshness is a property of the READ, not of a
# channel; two per-channel status chars would imply they can be independently
# fresh, which this sampler cannot produce.
#
# `b` (baseline) is never emitted for wifi or ntc, and that is correct rather than
# an oversight: `b` exists because a DELTA metric's first read has no delta to
# report, whereas a link state and an absolute ADC count are both meaningful from
# the first observation. The first fresh read is `f`.
CSV_COLUMNS = [
    "t_sec", "clock_ms", "res", "k", "cost_ms", "flush_ms", "late_ms", "gap_ms",
    "cpu", "cpu_st", "procs", "procs_st", "up", "up_st", "mem", "mem_st",
    "gpu", "gpu_st", "gpu_clk", "gpu_clk_st", "fg_pkg", "fg_pkg_st",
    "fg_pid", "fg_pid_st", "fg_cpu", "fg_cpu_st",
    "wifi", "wifi_st",
    "ntc_lcd", "ntc_led", "ntc_st",
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


def run_tool(argv: list[str], budget_s: float) -> tuple[int | None, str, str]:
    """Run a LOCAL helper (a python tool under tools/) under adb_run's discipline.

    It delegates rather than repeating adb_run's body on purpose. The part that
    matters is the teardown - kill, ONE bounded communicate, then abandon the
    pipes instead of waiting forever on a child that will not reap - and a
    second copy of that is a second place for it to be got wrong. adb_run never
    mentions adb; it is already command-agnostic, so this is a name that says
    what the runner is FOR, not a second mechanism.
    """
    return adb_run(argv, budget_s)

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


def wifi_resolution_s(t_ms: int, ticks_per: dict[str, int]) -> float:
    """Sampling period of the wifi channel in seconds (30.0 at the default T).

    Derived rather than hardcoded so that changing WIFI_TIER cannot leave the
    report quoting a resolution the sampler does not actually have.
    """
    return ticks_per[WIFI_TIER] * t_ms / 1000.0


def ntc_resolution_s(t_ms: int, ticks_per: dict[str, int]) -> float:
    """Sampling period of the NTC channel in seconds (30.0 at the default T).

    Derived from NTC_TIER for exactly the reason wifi_resolution_s is derived
    from WIFI_TIER: a hardcoded 30 would keep being printed after somebody moved
    the tier, and the resolution is the number that tells a reader how much to
    distrust the average beside it.
    """
    return ticks_per[NTC_TIER] * t_ms / 1000.0


def is_due(tier: str, ticks_per: dict[str, int], k: int) -> bool:
    return (k % ticks_per[tier]) == 0


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------
def section_due(name: str, secs: dict, tiers: set,
                off_sections: frozenset) -> bool:
    """Is section `name` both PRESENT in this tick's output and DUE this tick?

    `off_sections` is the ONE switch for "do not collect this section", and it is
    the same object the command builder is handed. There used to be one switch
    per channel, and they drifted: the tick call site passed `gpu_enabled` to the
    builder but `wifi_enabled` to this predicate, so a degraded wifi channel kept
    spending ~76 ms per due tick on a read whose result was then thrown away -
    while 12.1 claimed the marker was no longer sent. One set, computed once per
    tick, makes that drift unrepresentable instead of merely fixed.

    Module level rather than a closure inside Monitor.tick so it can be asserted
    on directly. That matters: this predicate has already been the site of one
    silent-channel-loss bug (see the history note in tick()), and it is
    unobservable from the outside - a section that is never 'due' simply never
    appears in the state string, which is indistinguishable from a short run.
    """
    return (name in secs and SECTION_TIER[name] in tiers
            and name not in off_sections)


def build_tick_command(nonce: str, tiers_present: set[str], guard_s: int,
                       off_sections: frozenset) -> str:
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

    `off_sections` names the sections this tick must NOT collect, and it is the
    SAME set section_due is handed. One switch for both call sites rather than
    one per channel: the per-channel form is exactly what drifted here already
    (see section_due's note), and a per-channel switch cannot be shared even in
    principle. There is deliberately NO default value - a forgotten argument has
    to be a TypeError, not a silent empty set. That default is what let the drift
    above ship: `wifi_enabled=True` meant a caller could omit it and still run.
    """
    fg = TIER_SLOW in tiers_present
    parts: list[str] = []
    for name, tier, _metrics in SECTIONS:
        if tier not in tiers_present:
            continue
        if name in off_sections:
            continue
        # Belt and braces: the $F/$P variables FGPKG and PIDSTAT lean on only
        # exist when FOCUS ran this tick, so the three are already carried as one
        # unit by MONITOR_GROUPS. This guard keeps that invariant true even if a
        # future caller builds off_sections by hand.
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
        elif name == SEC_WIFI:
            # Same wrapper as the standalone probe - one definition, WIFI_CMD.
            parts += [mark, WIFI_CMD]
        elif name == SEC_NTC:
            # Same wrapper as the standalone probe - one definition, NTC_CMD.
            # Both paths in ONE cat: the second read would pay another adb
            # round trip for a file on the same driver (2 channels in one call
            # measured 92.5 ms against a 61.1 ms `adb shell true` baseline).
            parts += [mark, NTC_CMD]
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

    Returns None when the READ failed (empty output, or a shell error marker), so
    the caller can classify it with section_error_kind(). A link that is DOWN is
    not a failed read and must never be reported as one - see wifi_section_failed.

    ANCHORING IS THE WHOLE POINT. `cmd wifi status` prints SSID and IP TWICE: once
    on the `WifiInfo:` line and again inside the `NetworkCapabilities:` ->
    `TransportInfo:` blob. A bare `re.search(r"IP: (\\S+)")` over the whole text
    therefore reads a DIFFERENT field than intended, and one that can disagree:
    on the reference device `WifiInfo` reported `IP: null` while `TransportInfo`
    reported `IP: /172.16.0.189`. That is not hypothetical, it is the exact output
    pair that motivated this parser. See docs/PITFALLS.md #49.
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
        # unknown - and a missing record is NOT evidence of an outage. Claiming
        # one here would make a device that never prints WifiInfo permanently
        # "down", capping every run to WARN for a formatting difference.
        state = "ok"
    elif enabled:
        state = "noassoc"
    else:
        state = "unknown"

    return {"state": state, "ssid": ssid, "ip": ip, "rssi": rssi,
            "link_mbps": link_mbps,
            "supplicant": (fields.get("Supplicant state") or None)}


def parse_ntc(text: str) -> dict | None:
    """The two NTC ADC counts, in NTC_CHANNELS order (lcd then led).

    NOT CONVERTED TO CELSIUS, and that is this function's CONTRACT rather than a
    pending TODO. The conversion constants are per-PROJECT (divider resistor, B,
    ADC full scale, per-channel compensation), so the conversion is an offline
    step: tools/ntc_convert.py, driven by tools/ntc_profiles/<project>.ini and
    invoked automatically at the end of a run that collected NTC. That
    keeps one board's numbers out of every other board's run, and keeps the
    archived counts comparable across projects and across revisions of the
    formula. So every value this function returns - and therefore every NTC
    number in samples.csv and in the report row - is a RAW ADC COUNT, declared as
    such by NTC_UNIT in the CSV header and in the frozen evidence block.

    This is still the single funnel every NTC COUNT passes through, which is what
    keeps that contract true: nothing downstream may invent a temperature from
    these numbers, and the report prints degrees only for a file that has been
    through the offline converter.

    Returns None when the READ failed as a whole (empty output, or a shell error
    marker - classified by section_error_kind). A single unreadable channel does
    NOT fail the read: it comes back as None in its own slot, because losing the
    LCD reading too, over a moved LED file, would hide data we do have.

    Never raises. Garbage in -> None out; an unparseable read is a read failure,
    and the caller turns that into ST_FAILED, not into a traceback that kills an
    8 h run.

    One thing this deliberately does NOT do: say WHICH file failed when only one
    channel is unreadable. There is no per-channel marker inside the section, and
    on the reference device both nodes are world-readable and both exist, so the
    case is theoretical. It would show up as one permanently-None column with
    n=0 for that channel, not as an `a` state.
    """
    if not text or not text.strip():
        return None
    stripped = text.strip()
    if any(k in stripped.lower() for k in ABSENT_MARKERS):
        return None
    # split() rather than splitlines(): `cat a b` writes the two counts back to
    # back, so if the first file ever lacked its trailing newline the two would
    # arrive as "662371" - one token, and unparseable either way. Splitting on
    # any whitespace also survives a `cat` that joins them with a space.
    tokens = stripped.split()
    nums: list[int | None] = []
    for tok in tokens[:len(NTC_CHANNELS)]:
        try:
            nums.append(int(tok))
        except ValueError:
            nums.append(None)
    vals = {ch: (nums[i] if i < len(nums) else None)
            for i, ch in enumerate(NTC_CHANNELS)}
    if all(v is None for v in vals.values()):
        return None
    return vals


def wifi_transition(prev: str | None, new: str) -> str | None:
    """Event kind for a state change, or None. Pure, so --selftest drives THIS.

    Three rules, each load-bearing:
      - The first observation of a DOWN state is dated. A run that starts with
        the radio off must say so; otherwise the verdict is capped to WARN with
        nothing in the timeline to explain it.
      - The first observation of `ok` is NOT an event: there is nothing to date.
      - A change INSIDE the down set (noassoc -> noip) is NOT an event. It would
        not open or close a window, and a second `wifi_lost` for one outage
        would read to a human as a second outage.
    """
    was_down = prev in WIFI_STATES_DOWN
    is_down = new in WIFI_STATES_DOWN
    if is_down and not was_down:
        return "wifi_lost"
    if was_down and not is_down:
        return "wifi_back"
    return None


def wifi_section_failed(state: str) -> bool:
    """True ONLY for a read failure. A wifi outage is a fact, not a read failure.

    This distinction is the one thing that must not be got wrong. Counting an
    outage as a section failure would (a) report the sampler's health as a
    function of the router's, and (b) drive `ok` toward 0, which gate 1 reads as
    "no data" and turns a healthy CPU run INCONCLUSIVE.
    """
    return state in (ST_FAILED, ST_ABSENT)


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
        # The monitor selection, mirrored from the Monitor the same way guard_s
        # and transit_ms are. It has to live in the EVIDENCE and not be read off
        # a Monitor inside judge(): judge() is a pure function of this dict, and
        # a verdict that consulted live run state could not be recomputed from an
        # archived report - which is the whole point of storing the evidence.
        self.monitors: list[str] = list(MONITOR_DEFAULT)
        self.monitors_off: list[str] = []

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

        # -- wifi link (a condition channel, not a metric) --------------------
        # `wifi_state` is zero-order-held like every metric value: a tick that did
        # not sample the link still reports the last thing that was known. A
        # FAILED read deliberately leaves it untouched and does NOT append an
        # event - a read that told us nothing must not move the state machine.
        self.wifi_state: str | None = None
        self.wifi_due = 0
        self.wifi_fresh = 0
        self.wifi_fail = 0
        self.wifi_counts: dict[str, int] = {}
        self.wifi_n_transitions = 0
        self.wifi_down_ticks = 0
        self.wifi_windows: list[dict] = []
        self.wifi_truncated = False
        self._wifi_open: int | None = None
        # Latest fresh read's fields, quoted in the report so a human can tell
        # "which AP" from "which window".
        self.wifi_ssid: str | None = None
        self.wifi_rssi: int | None = None
        self.wifi_link_mbps: float | None = None
        self.wifi_supplicant: str | None = None
        self.wifi_last_t: float | None = None
        self.wifi_last_clock_ms: int | None = None

        # -- ntc node temperatures (a condition channel, not a metric) --------
        # Simpler than the wifi block above by design: a temperature has no
        # transitions, so there is no state machine, no window list, no event
        # kind and nothing to cap. Just the reads, the last known value and the
        # series the average is taken over.
        #
        # `ntc_series` holds FRESH readings only. That is what makes `avg` an
        # average of observations rather than of zero-order-held repeats - the
        # same discipline the metric stats follow, and the reason every NTC
        # figure in the report is printed next to its own `n`.
        self.ntc_last: dict[str, int | None] = {ch: None for ch in NTC_CHANNELS}
        self.ntc_series: dict[str, list[int]] = {ch: [] for ch in NTC_CHANNELS}
        self.ntc_due = 0
        self.ntc_fresh = 0
        self.ntc_fail = 0
        self.ntc_last_t: float | None = None

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

    # -- wifi link ----------------------------------------------------------
    def note_wifi(self, t_sec: float, clock_ms: int, state: str,
                  detail: dict | None = None) -> str | None:
        """One DUE tick on which the wifi section was actually requested.

        Call this only for ticks that sent the section: `not due` is the absence
        of a call, not a state, so there is no third case to get wrong here.

        Returns the transition kind (`wifi_lost` / `wifi_back`) so the caller can
        emit it, or None. The state machine itself lives in the module-level
        `wifi_transition` so --selftest drives the REAL one (PITFALLS #42) -
        this method only feeds it and keeps the bookkeeping.

        Nothing here emits an event: `Monitor._emit` is the single writer of
        events.csv and of the `PERF|` lines, and two writers is how a timeline
        ends up disagreeing with itself.
        """
        prev = self.wifi_state
        self.wifi_due += 1
        if wifi_section_failed(state):
            # A failed read is "we do not know", NOT "the network is down".
            # Record the failure and return: no count, no window, no transition,
            # and above all do not touch `wifi_state` - the last known link state
            # is still the best information we have.
            self.wifi_fail += 1
            return None

        self.wifi_fresh += 1
        self.wifi_counts[state] = self.wifi_counts.get(state, 0) + 1
        if detail:
            if detail.get("ssid"):
                self.wifi_ssid = detail["ssid"]
            if detail.get("rssi") is not None:
                self.wifi_rssi = detail["rssi"]
            if detail.get("link_mbps") is not None:
                self.wifi_link_mbps = detail["link_mbps"]
            if detail.get("supplicant"):
                self.wifi_supplicant = detail["supplicant"]
        self.wifi_last_t = round(t_sec, 1)
        self.wifi_last_clock_ms = clock_ms
        if state in WIFI_STATES_DOWN:
            self.wifi_down_ticks += 1

        kind = wifi_transition(prev, state)
        if kind:
            self.wifi_n_transitions += 1
        self.wifi_state = state
        self._wifi_window(t_sec, clock_ms, state)
        return kind

    def _wifi_window(self, t_sec: float, clock_ms: int, state: str) -> None:
        """Open on the first down observation, close on the first observation
        that is not down. Only fresh reads reach here.

        The list is CAPPED because the evidence dict is re-serialized on every
        120 s snapshot and must stay O(1)-bounded (12.2). The cap is not a silent
        one: `wifi_truncated` reports it, and `down_ticks` / `down_s_est` are
        counted independently of the windows, so the SUMMARY stays correct even
        when the detail has been cut. Losing the tail of a 200-outage run is a
        display limit; losing the total would be a wrong number.
        """
        if state in WIFI_STATES_DOWN:
            if self._wifi_open is not None:
                self.wifi_windows[self._wifi_open]["ticks"] += 1
                return
            if len(self.wifi_windows) >= WIFI_MAX_WINDOWS:
                self.wifi_truncated = True
                return
            self._wifi_open = len(self.wifi_windows)
            self.wifi_windows.append({
                "start_t": round(t_sec, 1), "start_clock_ms": clock_ms,
                "end_t": None, "end_clock_ms": None, "duration_s": None,
                "ticks": 1, "from": state, "to": None, "closed": False})
            return
        if self._wifi_open is not None:
            w = self.wifi_windows[self._wifi_open]
            w["end_t"] = round(t_sec, 1)
            w["end_clock_ms"] = clock_ms
            w["duration_s"] = round(t_sec - w["start_t"], 1)
            w["to"] = state
            w["closed"] = True
            self._wifi_open = None

    def _wifi_evidence(self) -> dict:
        """The frozen wifi block. Wall-clock fields are the point of the whole
        feature - they are what the user lines up against the archived logcat -
        so they are emitted for every window, open or closed."""
        res = wifi_resolution_s(self.t_ms, self.ticks_per)
        # ONE span definition, shared with the ASCII row and the HTML table via
        # wifi_window_span(). An earlier draft also carried
        # `down_ticks * resolution_s` ("confirmed down samples") next to this and
        # the two disagreed by 5x on the same outage (60 s confirmed vs a 300 s
        # observed span) - which a reader would take for a bug or, worse, would
        # take for whichever number suited them. Two names for one thing is not
        # extra information.
        span = round(sum(wifi_window_span(w, self.wifi_last_t)
                         for w in self.wifi_windows), 1)
        return {
            "state": self.wifi_state,
            "ssid": self.wifi_ssid,
            "rssi": self.wifi_rssi,
            "link_mbps": self.wifi_link_mbps,
            "supplicant": self.wifi_supplicant,
            "last_t": self.wifi_last_t,
            "last_clock_ms": self.wifi_last_clock_ms,
            "n_due": self.wifi_due,
            "n_fresh": self.wifi_fresh,
            "n_fail": self.wifi_fail,
            "counts": dict(self.wifi_counts),
            "n_transitions": self.wifi_n_transitions,
            "down_ticks": self.wifi_down_ticks,
            # Observed span across all windows, NOT a measured duration: the true
            # outage is bounded by +-resolution_s around this, and a sub-
            # resolution outage is not in here at all. See WIFI_RESOLUTION_NOTE;
            # do not "tidy" this into a precise-looking number.
            "down_s_span": span,
            "resolution_s": res,
            "note": WIFI_RESOLUTION_NOTE % (res, res, res),
            "outages": self.wifi_windows,
            "truncated": self.wifi_truncated,
        }

    # -- ntc node temperatures ----------------------------------------------
    def note_ntc(self, t_sec: float, state: str, vals: dict | None) -> None:
        """One DUE tick on which the ntc section was actually requested.

        Call this only for ticks that sent the section: `not due` is the absence
        of a call, not a state, so there is no third case to get wrong here.

        `state` is the per-tick status char the tick computed, which is what lets
        a FAILED read be counted without moving the last known value. The split
        mirrors note_wifi exactly, and the reason is the same one: a read that
        told us nothing must not be drawn as a continuation of the last thing
        that did.
        """
        self.ntc_due += 1
        if state in (ST_FAILED, ST_ABSENT):
            self.ntc_fail += 1
            return
        self.ntc_fresh += 1
        self.ntc_last_t = round(t_sec, 1)
        for ch in NTC_CHANNELS:
            v = (vals or {}).get(ch)
            if v is None:
                continue
            self.ntc_last[ch] = v
            self.ntc_series[ch].append(v)

    def _ntc_evidence(self) -> dict:
        """The frozen NTC block. One entry per channel plus the read counters.

        `unit` is repeated here rather than living only in the CSV header: this
        block is what a future tool reads, and a raw ADC count that looks exactly
        like a temperature is precisely the thing that gets re-reported as
        celsius six months later by someone who never saw the header.
        """
        chans = {}
        for ch in NTC_CHANNELS:
            s = stats_of([float(v) for v in self.ntc_series[ch]], 1)
            chans[ch] = {
                "path": NTC_LCD_PATH if ch == "lcd" else NTC_LED_PATH,
                "n": s.get("n", 0),
                "avg": s.get("avg"),
                "min": s.get("min"),
                "max": s.get("max"),
                "last": self.ntc_last.get(ch),
            }
        return {
            "unit": NTC_UNIT,
            "resolution_s": ntc_resolution_s(self.t_ms, self.ticks_per),
            "n_due": self.ntc_due,
            "n_fresh": self.ntc_fresh,
            "n_fail": self.ntc_fail,
            "channels": chans,
        }

    def wifi_closed_window(self) -> dict | None:
        """The window that most recently closed, or None.

        Called immediately after note_wifi() reported `wifi_back`, where the
        window that just closed is by construction the last closed one. Exists
        so the caller never has to reach into a private index to say how long
        the outage lasted.
        """
        for w in reversed(self.wifi_windows):
            if w["closed"]:
                return w
        return None

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
                # INSIDE the run block, because that is where judge() and
                # _display() read run facts from and they read nowhere else. An
                # earlier revision of this change put these two one level up:
                # the report then still rendered, still had a SCOPE row, and
                # still passed every assertion that did not feed it - while the
                # deselection override below read an empty set and every
                # deselected gate went back to reporting pass. --selftest now
                # pins the level, not just the existence, of this key.
                "monitors": list(self.monitors),
                "monitors_off": list(self.monitors_off),
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
            # Top level, NOT under "metrics": every consumer of "metrics" walks
            # METRIC_ORDER, and wifi is deliberately not a member of it. Same
            # shelf as "memory" / "cpu" / "fg".
            "wifi": self._wifi_evidence(),
            # Same shelf as "wifi" and for the same reason: it is a condition
            # channel, so it must NOT go under "metrics" - every consumer of that
            # dict walks METRIC_ORDER, which ntc is deliberately not a member of.
            "ntc": self._ntc_evidence(),
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

    # --- a gate must never pass on evidence it was never given --------------
    # Gate 8's `fg_due < 2` branch cannot tell "the foreground chain was not
    # SELECTED this run" from "the run was too short to reach SLOW twice", and
    # gate 9 falls through to a plain pass with switches=0 pkgs=0 when the whole
    # chain was skipped. So before this override existed, deselecting 前台应用
    # produced a PASS on a channel that was never sampled: the report would
    # certify as healthy the one thing it had not looked at. PITFALLS #52.
    #
    # Applied by ID, AFTER all nine gates are built and BEFORE `worst`, so the
    # gate ids stay exactly 1..9 (--selftest pins that) and the gate-table
    # contract is untouched. Only gates that read a SELECTABLE channel are
    # listed: the locked FAST trio feeds gates 5 and 6, which therefore can never
    # lose their evidence.
    off_ids = set((ev.get("run") or {}).get("monitors_off") or [])
    for mid, gids in MON_GATE_OF.items():
        if mid not in off_ids:
            continue
        for gid in gids:
            for g in gates:
                if g.get("id") == gid:
                    g["status"] = "inconclusive"
                    g["detail"] = (
                        f"monitor '{mid}' was switched off for this run - no "
                        f"evidence was collected, so this gate cannot be judged")

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

    # WiFi link condition. Deliberately NOT a gate: the link is a condition the
    # run happened UNDER, not a pass/fail property of the device under test, so
    # it must not add a tenth row to the gate table (--selftest pins the gate
    # ids to exactly 1..9). It is a negative-id pseudo-cap on the same shelf as
    # -1 PARTIAL and -2 UNCALIBRATED, which are appended to `caps` and never to
    # `gates`.
    #
    # Two asymmetries against -1 PARTIAL, both on purpose:
    #   - `caps.append` is UNCONDITIONAL. -1 only appends when it also changed
    #     the result, so a run that was already WARN leaves no trace of why. The
    #     reason must never be lost, even when something else already capped.
    #   - it can only ever downgrade OK -> WARN. A FAIL earned by something real
    #     must not be laundered into "well, the network was flaky".
    wifi = ev.get("wifi") or {}
    down_ticks = int(wifi.get("down_ticks") or 0)
    if down_ticks > 0:
        outs = wifi.get("outages") or []
        first = outs[0] if outs else None
        when = (_hms(first.get("start_clock_ms")) or "?") if first else "?"
        caps.append({"id": -3, "name": "WIFI", "status": "warn",
                     "detail": f"link down on {down_ticks} sample(s), "
                               f"span {wifi.get('down_s_span')}s, first at "
                               f"{when}, resolution "
                               f"{wifi.get('resolution_s')}s"})
        if result == "OK":
            result = "WARN"

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
    # The monitors this run was CONFIGURED to collect. A row whose channel is in
    # here was never sampled, so its counters are all 0 for the same reason a
    # stopped clock is right twice a day: nothing was ever asked of it. A
    # zero-filled row reads as a clean bill of health, which is the one thing
    # this report must not claim about a channel it did not collect. The SCOPE
    # row says this at run level; these are the row-level versions a reader
    # lands on first.
    off = set(run.get("monitors_off") or [])
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

    if "fg" in off:
        app_verdict = "NOT MONITORED"
    elif fg.get("gone") or fg.get("pid_dead"):
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
        "memory": ("NOT MONITORED  (the 'mem' monitor was switched off for "
                   "this run - no memory observation was ever taken)"
                   if "mem" in off else
                   f"{'LEAK SUSPECT' if leak else 'NO LEAK'}  "
                   f"delta={q(mem.get('delta_pct'), '%')}  "
                   f"slope={q(mem.get('slope_pct_per_h'), '%/h')}  "
                   f"blocks={mem.get('blocks_valid', 0)}/{MEM_BLOCKS}  "
                   f"n={mem.get('n', 0)}  "
                   f"avail_min={q(mem.get('avail_min_mb'), 'MB')}"),
        "cpu": f"avg={q(met.get('cpu', {}).get('avg'), '%')}  "
               f"p95={q(met.get('cpu', {}).get('p95'), '%')}  "
               f"max={q(met.get('cpu', {}).get('max'), '%')}  "
               f">={T_CPU_P95_BUSY:.0f}% for "
               f"{q(ev.get('cpu', {}).get('ge90_s'), 's')} (load, not a fault)",
        # The counters are suppressed rather than zeroed on purpose: printing
        # switches=0 pkgs=0 is the misreading this row exists to prevent.
        "app": ("NOT MONITORED  (the 'fg' monitor was switched off for this run"
                " - no foreground observation was ever taken)"
                if "fg" in off else
                f"{app_verdict}  switches={fg.get('switches', 0)}  "
                f"pkgs={len(fg.get('pkgs') or [])}  gone={fg.get('gone', 0)}  "
                f"pid_lost={fg.get('pid_lost', 0)}  "
                f"pid_dead={fg.get('pid_dead', 0)}  "
                f"watch={fg.get('watch_pkg') or '(unset)'}"),
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
        "wifi": _wifi_line(ev.get("wifi") or {}, off),
        "ntc": _ntc_line(ev.get("ntc") or {}, off),
        "scope": _scope_line(run),
    }


def wifi_window_span(w: dict, last_t: float | None) -> float:
    """Seconds spanned by one outage window, from its first down sample to the
    first sample after it that was not down.

    A window that never closed runs to `last_t`, the run's last fresh
    observation - it did not necessarily end there, it was still down when the
    run stopped, which is why both renderers prefix an open window's duration
    with `>=`.

    Shared by the evidence total, the ASCII row and the HTML table on purpose:
    three surfaces quoting one outage must quote the same number.
    """
    start = w.get("start_t")
    if start is None:
        return 0.0
    end = w.get("end_t")
    if end is None:
        end = last_t if last_t is not None else start
    return max(0.0, float(end) - float(start))


def _wifi_line(w: dict, off=frozenset()) -> str:
    """The ASCII one-liner for the WIFI row. MUST stay a single line and MUST
    NOT contain the literal `report` - the closing block is sniffed for the
    report path, and a stray match there would hand the caller the wrong line.

    This row is not decoration: `judge`'s `capped_by` is computed and never
    rendered by any of the three formatters, so THIS string and the HTML
    timeline section are the only places a WARN-capped run says why. That is
    also why the resolution is printed even on a healthy run - a reader has to
    be able to see the bound before trusting a clean timeline. (Wiring
    `capped_by` into the renderers is separate work; see TODO 6.x.)
    """
    due = int(w.get("n_due") or 0)
    if not due:
        # Never empty: every ROW_SPEC row must render a value on EVERY run,
        # including one where the channel was off the whole time. "Switched off"
        # and "read nothing" are two different stories about the same blank
        # column and only the caller knows which one happened.
        if "wifi" in off:
            return ("LINK ? not monitored (the 'wifi' monitor was switched off "
                    "for this run)")
        return "LINK ? no wifi observation in this run"
    res = w.get("resolution_s")
    res_s = f"{res:g}s" if isinstance(res, (int, float)) else "?"
    seen = f"seen={w.get('n_fresh', 0)}/{due}"
    if w.get("n_fail"):
        seen += f" fail={w['n_fail']}"
    state = w.get("state")
    down_ticks = int(w.get("down_ticks") or 0)
    # "currently down" and "was down at some point" are different statements and
    # must not be collapsed: a run that recovered would otherwise render as
    # `LINK DOWN(ok)`, which reads as a contradiction.
    if down_ticks and state in WIFI_STATES_DOWN:
        head = f"LINK DOWN({state})"
    elif down_ticks:
        head = "LINK RECOVERED"
    else:
        head = "LINK UP"
    if down_ticks:
        n_out = len(w.get("outages") or [])
        if w.get("truncated"):
            n_out = f"{n_out}+"
        return (f"{head} last_sample={_hms(w.get('last_clock_ms')) or '-'} "
                f"n_outage={n_out} span={w.get('down_s_span')}s "
                f"{seen} res={res_s}")
    rssi = w.get("rssi")
    rssi_s = f"{rssi}dBm" if rssi is not None else "-"
    return (f"{head} ssid={w.get('ssid') or '-'} rssi={rssi_s} "
            f"span=0s {seen} res={res_s}")


def _ntc_line(n: dict, off=frozenset()) -> str:
    """The ASCII one-liner for the NTC row. Same three hard constraints as
    _wifi_line and for the same reasons: a single line, never the literal
    `report` (the closing block is sniffed for the report path), never empty.

    `unit=` is printed even on a healthy run, and it reads `raw_adc`: the stored
    numbers are ADC counts, not degrees. The user's conversion formula has not
    been supplied yet, so this row is the only place on the console that says what
    the numbers actually are - and it says it every time rather than only when
    something looks wrong, because "662" does not look wrong.

    `n=` accompanies every average, for the reason 12.2 states: on the SLOW tier
    most ticks carry a zero-order-held value, so an average over readings whose
    count is unknown cannot be re-checked by the person reading it. `res=` says
    the sampling period the same way, and for the same reason.
    """
    due = int(n.get("n_due") or 0)
    if not due:
        # Never empty: every ROW_SPEC row must render a value on EVERY run,
        # including one where the channel was deselected or unreadable the whole
        # way through.
        if "ntc" in off:
            return ("TEMP ? not monitored (the 'ntc' monitor was switched off "
                    "for this run)")
        return "TEMP ? no ntc observation in this run"
    res = n.get("resolution_s")
    res_s = f"{res:g}s" if isinstance(res, (int, float)) else "?"
    chans = n.get("channels") or {}
    bits = []
    for ch in NTC_CHANNELS:
        c = chans.get(ch) or {}
        cnt = int(c.get("n") or 0)
        # A per-channel n, because one node can be unreadable while the other is
        # fine - the two are separate files behind one read.
        bits.append(f"{ch}=avg{c.get('avg')}/max{c.get('max')} n={cnt}"
                    if cnt else f"{ch}=n/a n=0")
    seen = f"seen={n.get('n_fresh', 0)}/{due}"
    if n.get("n_fail"):
        seen += f" fail={n['n_fail']}"
    return (f"TEMP {' '.join(bits)} {seen} "
            f"unit={n.get('unit') or '?'} res={res_s}")


def _scope_line(run: dict) -> str:
    """The ASCII one-liner for the SCOPE row. Same three hard constraints again.

    This row exists because `d` is OVERLOADED. In samples.csv a `d` status char
    means "switched off", and that can be either a deselection the operator chose
    or the runtime degradation policy giving up on an unreadable channel - two
    very different stories about a column that is blank either way. Without this
    row nobody, years later, can tell which happened; with it, the CSV's
    `# config_monitors=` line and this row together make the file self-describing.

    Degradation is deliberately NOT folded in here: a channel dropped at runtime
    leaves a `src_degraded` event and a null `sources` entry, and mixing the two
    into one line would blur the exact distinction the row was added to draw.
    """
    off = list(run.get("monitors_off") or [])
    n_on = len(run.get("monitors") or [])
    if not off:
        return (f"SCOPE all {n_on} monitor(s) enabled "
                f"(nothing was deselected for this run)")
    return (f"SCOPE off={','.join(off)} ({len(off)} monitor(s) not collected - "
            f"their columns are empty by CONFIGURATION, not by failure)")


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
    # Placed high on purpose: the scope qualifies every row below it. A reader who
    # skips it will read an empty 前台应用 row as "the app was fine".
    ("scope",  "SCOPE",  "监控范围", ()),
    ("device", "DEVICE", "设备", ()),
    ("memory", "MEMORY", "内存 / 泄漏", (7,)),
    ("cpu",    "CPU",    "CPU 占用", (8,)),
    ("app",    "APP",    "前台应用", (9,)),
    ("events", "EVENTS", "链路事件", (4, 5, 6)),
    ("gates",  "GATES",  "未通过的门", None),
    ("duty",   "COST",   "监控开销", ()),
    # `gids=()` and not None: this row is a pure FACT like DEVICE/COST, so the
    # renderer's else-branch keeps it green on every run. Painting it red would
    # claim a network outage is a defect of the device under test, which is
    # precisely the claim the "condition channel, not a metric" design refuses
    # to make.
    ("wifi",   "WIFI",   "WiFi 链路", ()),
    # A pure FACT row like DEVICE/COST/WIFI, so gids=() keeps it green on every
    # run. Colouring it by the result would turn "the operator deselected a
    # channel" into a finding about the device under test.
    ("ntc",    "NTC",    "NTC 节点温度", ()),
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
        "disagree sometimes. NOT MONITORED = the 'mem' monitor was "
        "switched off for this run",
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
        "never launched?). NOT MONITORED = the 'fg' monitor was switched "
        "off for this run, so this row has no opinion at all",
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
    "wifi": (
        "the WiFi link as a CONDITION the run happened under, not a fault of the "
        "device. LINK UP / LINK DOWN(state) / LINK RECOVERED says what the link "
        "was doing; ssid+rssi are the last good read. span= is the OBSERVED gap "
        "between the first sample that saw the link down and the first one after "
        "it that saw it up, summed over all outages - an ESTIMATE: samples land "
        "every 30 s, so a real outage lasts anywhere within +-30 s of the number "
        "shown, and an outage SHORTER THAN 30 s can fall between two samples and "
        "never be seen at all. A WiFi outage caps the result at WARN; it can "
        "never turn a FAIL into anything better.",
        "WiFi 链路是本次跑所处的「条件」，不是设备故障。LINK UP=正常，"
        "LINK DOWN(状态)=当前仍断，LINK RECOVERED=中断过但已恢复；"
        "ssid/rssi 是最后一次成功读取的值。span= 是「观测到的跨度」——"
        "第一次采到断网到第一次采到恢复之间的时间，多段中断累加；它是「估计值」："
        "采样间隔 30 秒，真实中断时长与所示数字相差 ±30 秒以内；"
        "短于 30 秒的瞬断可能正好落在两次采样之间，报告里完全看不出来。"
        "发生 WiFi 中断会把结论封顶为 WARN，但绝不会把 FAIL 改好。"),
    "scope": (
        "which monitors this run was configured to collect. A monitor listed "
        "under off= was NOT SAMPLED AT ALL: its columns are empty because it was "
        "turned off, not because anything failed, and the CSV rows carry a `d` "
        "status char to say so. Deselecting MEM or FG (see the off= list) costs "
        "the evidence that gates 7/8/9 judge from, so those gates are forced "
        "to INCONCLUSIVE "
        "and the whole run is reported as INCONCLUSIVE - a run must never pass a "
        "gate whose evidence nobody collected. A channel dropped at RUNTIME (the "
        "degradation policy giving up on an unreadable node) is a different "
        "thing: it appears as a src_degraded event and a null source, and it is "
        "deliberately not listed here.",
        "本次运行实际配置采集的监控项。off= 里列出的项【完全没有被采集】："
        "它的列是空的，是因为关掉了，不是因为出了问题；对应 CSV 行的状态字符是 d。"
        "取消「内存」或「前台应用」会带走门 7/8/9 赖以判读的证据，"
        "因此这些门会被强制判为 inconclusive，整次运行结论随之变成 INCONCLUSIVE —— "
        "报告绝不能声明一个从未被采集的通道是健康的。"
        "运行期被降级丢弃的通道是另一回事（会出现 src_degraded 事件、source 为空），"
        "有意不列在这里。"),
    "ntc": (
        "LCD/LED node temperatures as a CONDITION the run happened under, not a "
        "fault of the device - no gate reads them and nothing is capped by them. "
        "UNIT IS raw_adc: the values are ADC counts straight out of "
        "/sys/bus/iio/devices/iio:device0/in_voltage{3,2}_raw, NOT degrees - so "
        "read the row as a relative curve, never as a temperature. Converting it "
        "is AUTOMATIC at the end of the run, and only when this run actually read "
        "the nodes: tools/ntc_convert.py is run over this run's samples.csv and "
        "writes a .temps.csv AND a .temps.png beside it, in degrees per node, "
        "using the profile picked as `ntc_profile`; both are archived with the "
        "rest. avg/max are over FRESH readings only "
        "(n says how many); a tick that did not sample holds the previous value "
        "and is not counted. Sampled on the SLOW tier: one sample every 30 s. "
        "Failed reads are counted in fail= and leave a blank cell rather than "
        "continuing the line. Draw the curve in Excel from the samples.csv "
        "columns ntc_lcd / ntc_led against t_sec.",
        "LCD/LED 节点温度是本次跑所处的「条件」，不是设备故障——没有任何门读它，"
        "也不做任何卡控。单位是 raw_adc：数值直接来自 "
        "/sys/bus/iio/devices/iio:device0/in_voltage{3,2}_raw 的 ADC 计数，"
        "不是摄氏度——请当相对曲线读，不要当温度读。换算在收尾时自动进行——"
        "前提是这次真的读到了节点：平台对本次的 samples.csv 跑 tools/ntc_convert.py，"
        "在旁边生成一份 .temps.csv 与一张 .temps.png，"
        "用 `ntc_profile` 选定的项目 profile 给出每个节点的摄氏度，两个都随报告一起归档。"
        "若日志里没有换算那几行，可手工补跑。"
        "avg/max 只对真实读数统计（n 给出读数条数），"
        "未采样的周期沿用上一拍的值且不计入。走 SLOW 档，每 30 秒一个样本。"
        "读取失败计入 fail=，该拍留空而不是把线连下去。"
        "要自己画曲线，用 Excel 拿 samples.csv 的 ntc_lcd / ntc_led 列对 t_sec 绘制。"),
}

GATE_LEVEL = {"pass": 0, "warn": 1, "fail": 2, "inconclusive": 3}
LEVEL_NAME = ["ok", "warn", "fail", "inconclusive"]
LEVEL_ZH = {"ok": "通过", "warn": "注意", "fail": "异常",
            "inconclusive": "样本不足"}

GATE_ZH = {1: "数据", 2: "覆盖率", 3: "采样节拍", 4: "PC 预算超时", 5: "参数范围",
           6: "重启", 7: "内存", 8: "前台 CPU", 9: "前台应用",
           -1: "中断快照", -2: "阈值未标定", -3: "WiFi 中断"}

# WiFi link state words -> Chinese. Same shelf as GATE_ZH / METRIC_ZH: data for
# a human-facing surface, never printed to stdout.
WIFI_ZH = {"ok": "已连接", "noip": "已关联但无 IP", "noassoc": "未关联",
           "off": "WiFi 已关闭", "unknown": "无法判定"}

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
    wifi = ev.get("wifi") or {}

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
    # `monitors` is the one parameter that must not be allowed to fall back to
    # its declared default here. Reading it from the frozen run block is what
    # keeps this table honest when a run deselects something: defaulted, the
    # table claims all five monitors were collected while the SCOPE row two
    # sections later says one was not collected at all. A payload written before
    # that block existed still falls back - the old behaviour, and the honest
    # answer for a report that never recorded a selection.
    shown_params = {"interval_sec": cfg.get("interval_sec"),
                    "duration_sec": cfg.get("duration_sec"),
                    "watch_pkg": cfg.get("watch_pkg") or "",
                    "key_ini": cfg.get("key_ini") or ""}
    if isinstance(run.get("monitors"), list):
        shown_params["monitors"] = run["monitors"]
    parts.append(_pptp_report.render_params_table(shown_params, PARAMS, num=2))
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

    # --- six: wifi link timeline ------------------------------------------
    # APPENDED as section six rather than inserted, so the numbering of sections
    # one to five does not move.
    #
    # This section is where the feature pays off: it is the only place a reader
    # can line a network outage up against the platform's logcat. The bound is
    # stated in the heading and again in the disclaimer because it is the one
    # thing about this table that must not be over-trusted.
    res_s = wifi.get("resolution_s")
    res_s = f"{res_s:g}" if isinstance(res_s, (int, float)) else "?"
    outs = wifi.get("outages") or []
    n_out = len(outs)
    if wifi.get("truncated"):
        n_out = f"{n_out}+"
    span_total = wifi.get("down_s_span")
    span_txt = "" if not wifi.get("down_ticks") else \
        f' · 累计中断跨度 {_esc(span_total)} 秒'
    parts.append(
        f'<h2>六、WiFi 链路时间线<span>采样分辨率 {_esc(res_s)} 秒，'
        f'边界 ±{_esc(res_s)} 秒 · 共 {_esc(n_out)} 段中断'
        f'{span_txt}</span></h2>')
    if not wifi.get("n_due"):
        parts.append("<table><tr><td>本次运行没有采到任何 WiFi 数据"
                     "（设备不支持该命令，或通道一直不可读）。</td></tr></table>")
    elif not outs:
        parts.append(
            "<table><tr><td>"
            f'本次运行全程联网（成功读取 {_esc(wifi.get("n_fresh", 0))} 次，'
            f'读取失败 {_esc(wifi.get("n_fail", 0))} 次）。'
            "</td></tr></table>")
    else:
        parts.append("<table><tr><th>#</th><th>开始时刻</th><th>结束时刻</th>"
                     "<th>时长(s)</th><th>持续拍数</th><th>起始状态</th>"
                     "<th>恢复状态</th></tr>")
        for n, w in enumerate(outs, 1):
            closed = bool(w.get("closed"))
            # An open window is NOT a missing value: rendering a bare `None`
            # would both read as a bug and break the report's own "no None
            # anywhere" guarantee. It means the link was still down when the run
            # ended, and it says exactly that.
            end_cell = (_esc(_hms(w.get("end_clock_ms")) or "-") if closed
                        else "（运行结束时仍未恢复）")
            end_title = (_esc(_hms(w.get("end_clock_ms"), full=True))
                         if closed else "")
            # ONLY open windows get the `>=` prefix. An open window really is a
            # lower bound - it was still down when we stopped looking. A CLOSED
            # window is NOT: its span is t2 - t1 (last good before, first good
            # after), and the truth is somewhere in (span - P, span + P), so
            # `>=` would be an unfounded claim in the other direction. A real
            # 10 s outage straddling one sample would print `>=30.0`.
            span = wifi_window_span(w, wifi.get("last_t"))
            dur_cell = (f'≥{_esc(round(span, 1))}' if not closed
                        else _esc(round(span, 1)))
            parts.append(
                "<tr>"
                f'<td class="num">{n}</td>'
                f'<td class="num" title="{_esc(_hms(w.get("start_clock_ms"), full=True))}">'
                f'{_esc(_hms(w.get("start_clock_ms")) or "-")}</td>'
                f'<td class="num" title="{end_title}">{end_cell}</td>'
                f'<td class="num">{dur_cell}</td>'
                f'<td class="num">{_esc(w.get("ticks", 0))}</td>'
                f'<td class="note">{_esc(WIFI_ZH.get(w.get("from"), w.get("from")))}</td>'
                f'<td class="note">{_esc(WIFI_ZH.get(w.get("to"), w.get("to")) or "-")}</td>'
                "</tr>")
        parts.append("</table>")
    if wifi.get("truncated"):
        parts.append('<p class="sub">中断段数过多，本表只保留了前 '
                     f'{_esc(WIFI_MAX_WINDOWS)} 段；上方统计的总时长不受影响。</p>')
    # The disclaimer. Not boilerplate: the whole reason a short outage can be
    # invisible AND fail to cap the verdict is the 30 s sampling period, and a
    # reader who does not know that will read an empty table as "the network was
    # fine" rather than "the network was not looked at often enough".
    parts.append(f'<p class="sub">{_esc(wifi.get("note") or "")}</p>')
    if wifi.get("down_ticks"):
        parts.append(
            '<p class="sub">本次运行因 WiFi 中断被封顶为 WARN'
            '（不会把 FAIL 改好）。若要确认视频平台报 connection error 的时刻，'
            '请用上表的墙钟时刻与同目录归档的 <code>.logcat.log</code> 人工比对。</p>')

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
    """Print this probe's first stdout line - and SAY WHY when there is none.

    The reason can only come from stderr. A denied sysfs read is SILENT on
    stdout: `cat` writes `Permission denied` to fd 2 and exits 1, so a probe
    that keeps only stdout prints `(empty)` and a bare FAIL, and the reader has
    nothing to act on. That is precisely how the NTC nodes stayed unreadable
    with a clean-looking console.

    stderr is consulted ONLY when stdout is empty, so a working channel keeps
    its one-line summary and this cannot become noise. Forced to ASCII because
    a device that localises its own messages would otherwise abort the print on
    a GBK console (D-07).
    """
    _rc, out, err = adb_shell(serial, cmd, 15.0)
    out = out.replace("\r", "")
    if out.strip():
        print(f"[probe] {name:<10} : {out.strip().splitlines()[0].strip()[:70]}")
    else:
        why = (err or "").replace("\r", "").strip().splitlines()
        reason = why[0].strip()[:80] if why else ""
        reason = reason.encode("ascii", "replace").decode("ascii")
        print(f"[probe] {name:<10} : (empty) - "
              + (reason or "no output and no error - rc=%s" % _rc))
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
    # The wifi block is printed RAW as well as parsed. `cmd wifi status` prints
    # SSID and IP twice (once on `WifiInfo:`, once inside `NetworkCapabilities:
    # -> TransportInfo:`) and the two can DISAGREE - the reference device was
    # caught reporting `IP: null` in one and `IP: /172.16.0.189` in the other.
    # Writing a parser against a remembered shape of this output is how the
    # in-repo `"ssid:" in text` anti-pattern got shipped, so the raw text is
    # dumped here and the parser is only ever trusted against real bytes.
    wtxt = _probe_raw(serial, "wifi", WIFI_CMD)
    wparsed = parse_wifi(wtxt)
    if wparsed:
        print(f"[probe] wifi parse   : OK (state={wparsed['state']} "
              f"ssid={wparsed['ssid']} ip={wparsed['ip']} "
              f"rssi={wparsed['rssi']} link={wparsed['link_mbps']})")
        print(f"[probe] wifi states  : {sorted(WIFI_STATES_DOWN)} count as DOWN, "
              f"resolution {wifi_resolution_s(2000, tier_ticks_per(2000)):g}s")
    else:
        print("[probe] wifi parse   : FAIL - wifi channel will be disabled "
              f"(re-probed every {WIFI_REPROBE_INTERVAL_S:.0f}s)")
    # Force ASCII: stdout goes to a GBK console and then through the WS pipe, and
    # a device that localizes its own output would otherwise abort the print.
    for ln in wtxt.splitlines():
        print("[probe] wifi raw     | "
              + ln.encode("ascii", "replace").decode("ascii"))
    # --- NTC nodes ----------------------------------------------------------
    # The RAW count AND the presence of `in_voltage_scale` are both printed,
    # because together they are the answer to "why is this not in Celsius yet".
    # It was established once, by hand, and must not have to be established
    # again: the driver exports no scale (reading it returns EINVAL), there is no
    # hwmon node, and /sys/class/ktc_projector/ktc_ntc3/temperature hands back the
    # raw count itself - so the ADC -> Celsius formula cannot be derived from the
    # device at all and has to come from the person who owns the hardware.
    # Printing the ABSENCE is the point: a reader who later finds this function
    # will otherwise assume the scale was simply never looked for.
    ntxt = _probe_raw(serial, "ntc", NTC_CMD)
    nparsed = parse_ntc(ntxt)
    if nparsed:
        shown = ", ".join("%s=%s" % (c, nparsed[c]) for c in NTC_CHANNELS)
        print(f"[probe] ntc parse    : OK ({shown}) unit={NTC_UNIT} - NOT celsius")
    else:
        print("[probe] ntc parse    : FAIL - ntc channel will be disabled "
              f"(re-probed every {NTC_REPROBE_INTERVAL_S:.0f}s)")
    for ch, path in zip(NTC_CHANNELS, (NTC_LCD_PATH, NTC_LED_PATH)):
        print(f"[probe] {'ntc ' + ch:<13}: {path}")
    scale = os.path.dirname(NTC_LCD_PATH) + "/in_voltage_scale"
    # Same identity as the value read above: on an Enforcing board a bare
    # `cat` is DENIED here, and the stderr echo would then say "Permission
    # denied" - which reads as "the file exists, you lack the rights" and
    # flatly contradicts the conclusion printed on the next line. As root
    # the answer is the true one: this attribute is absent.
    _probe_raw(serial, "ntc scale", f"su 0 cat {scale}")
    print("[probe] ntc          : the ADC -> Celsius factor is NOT on this "
          "device; the formula must come from the user")
    raw_ascii = (ntxt or "").strip().replace("\n", " ")
    print("[probe] ntc raw      | "
          + raw_ascii.encode("ascii", "replace").decode("ascii"))

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

    The rows are sampled ROUND-ROBIN - every variant once per round, `reps`
    rounds - not one variant at a time. This table's product is the DIFFERENCE
    between adjacent rows (the wifi delta, the ntc delta), and a difference is
    only a cost if both rows were measured at the same moment. Sampling a whole
    variant before starting the next puts a drift window between the two rows the
    delta is taken over, and that drift lands in the delta. Measured on the
    reference device, idle, three runs each: the ntc delta read +57.9 / +51.5 /
    +86.4 ms blocked but +67.2 / +74.0 / +73.5 ms interleaved - the mean barely
    moved, the SPREAD fell by a factor of five. Interleaving turns the drift into
    a common-mode term the subtraction cancels. Call count, sleep count and
    runtime are unchanged. Its limit is stated at the loop below.
    """
    nonce = "a17f3c"

    def _off(*drop: str) -> frozenset:
        """off_sections that drops ONLY the named monitor groups.

        Named rather than built from a keep-list so every PUBLISHED row stays
        reproducible: `FULL (v2)` must keep measuring the exact command the
        pre-wifi figure came from (wifi off), and `FULL+WIFI` the one v2.12.0
        published (ntc off - NTC did not exist yet).
        """
        return frozenset(s for gid, _zh, secs in MONITOR_GROUPS
                         if gid in drop for s in secs)

    # Each row differs from the one above it by exactly ONE channel, so the
    # printed difference between adjacent rows IS that channel's cost. Without
    # that adjacency these would be four unrelated totals.
    variants = [
        ("FAST (v2)", {TIER_FAST}, _off()),
        ("FAST+MED (v2)", {TIER_FAST, TIER_MED}, _off()),
        ("FULL (v2)", {TIER_FAST, TIER_MED, TIER_SLOW}, _off("wifi", "ntc")),
        ("FULL+WIFI", {TIER_FAST, TIER_MED, TIER_SLOW}, _off("ntc")),
        ("FULL+WIFI+NTC", {TIER_FAST, TIER_MED, TIER_SLOW}, _off()),
    ]
    print(f"device={serial} reps={reps} guard G={DEVICE_GUARD_DEFAULT_S:.0f}")
    print("(one rep = one adb.exe process + one compound device shell)")
    print("(round-robin: each round runs every row below in turn, so the deltas")
    print(" printed under this table carry no host or device drift)\n")
    print("%-16s %10s %10s %10s %8s" % ("variant", "median ms", "min ms",
                                        "max ms", "bytes"))
    print("-" * 60)
    # The loops are ordered rounds-then-variants, not variants-then-reps. The
    # delta between two adjacent rows is only a cost if both rows were measured
    # at the same moment; blocking one variant at a time puts a whole
    # host-and-device drift window between them and books that drift as the
    # channel's cost. Interleaving makes it common-mode, and the subtraction
    # drops it. This does NOT remove drift correlated with POSITION within a
    # round - only a randomised order would, and that trades a systematic error
    # for a noisy one, which is worse for a table a human reads once.
    times: dict[str, list] = {label: [] for label, _t, _o in variants}
    sizes: dict[str, int] = {label: 0 for label, _t, _o in variants}
    for _ in range(reps):
        for label, tiers, off in variants:
            cmd = build_tick_command(nonce, tiers, int(DEVICE_GUARD_DEFAULT_S),
                                     off)
            t0 = time.perf_counter()
            rc, out, _err = adb_shell(serial, cmd, 30.0)
            times[label].append((time.perf_counter() - t0) * 1000.0)
            sizes[label] = len(out.encode("utf-8", "replace"))
            if rc != 0:
                print(f"[warn] {label}: rc={rc}")
            time.sleep(0.3)

    # Reporting is its own pass so the table still prints in cost order (each row
    # one channel above the last) no matter which order the samples came in.
    med: dict[str, float] = {}
    for label, _t, _o in variants:
        ts = times[label]
        med[label] = statistics.median(ts)
        print("%-16s %10.1f %10.1f %10.1f %8d"
              % (label, med[label], min(ts), max(ts), sizes[label]))

    f, m, full = med["FAST (v2)"], med["FAST+MED (v2)"], med["FULL+WIFI"]
    full_all = med["FULL+WIFI+NTC"]
    print("\ncondition-channel cost (device-side, measured, one at a time):")
    print("  FULL          = %.1f ms" % med["FULL (v2)"])
    print("  FULL+WIFI     = %.1f ms   wifi delta = %+.1f ms"
          % (med["FULL+WIFI"], med["FULL+WIFI"] - med["FULL (v2)"]))
    print("  FULL+WIFI+NTC = %.1f ms   ntc  delta = %+.1f ms"
          % (full_all, full_all - med["FULL+WIFI"]))
    print("  Both ride the SLOW tier, so both are paid once per 30 s window - "
          "the")
    print("  deltas above are per-window costs, not per-tick ones.")
    # TWO duty columns on purpose. The `wifi%` column is the basis the v2.12.0
    # figures were published on (wifi on, ntc not yet existing), and keeping it
    # is what lets a reader reconcile a re-measurement with the number already in
    # the docs. The `all-on%` column is what a DEFAULT run actually costs now
    # that ntc defaults on - the number to quote for "what does it cost to run
    # perf_monitor with everything switched on".
    print("\nduty cycle (tiers derived from T, integer ms):")
    print("%-7s %-7s %-7s %-29s %9s %9s"
          % ("T", "MED", "SLOW", "tick mix", "wifi%", "all-on%"))
    print("-" * 72)
    for t_ms in (1000, 2000, 6000, 30000):
        tp = tier_ticks_per(t_ms)
        total = tp[TIER_SLOW]
        n_med = total // tp[TIER_MED]
        n_fast = total - n_med
        mix = f"{n_fast} FAST + {n_med - 1} MED + 1 FULL"
        span = float(tp[TIER_SLOW] * t_ms)
        base = n_fast * f + (n_med - 1) * m
        print("%-7.1f %-7.1f %-7.1f %-29s %8.2f%% %8.2f%%"
              % (t_ms / 1000.0, tp[TIER_MED] * t_ms / 1000.0,
                 tp[TIER_SLOW] * t_ms / 1000.0, mix,
                 (base + full) / span * 100.0,
                 (base + full_all) / span * 100.0))
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
    slow_cmd = build_tick_command("abc123", all_tiers, 8, frozenset())
    check("build_tick_command asks for the foreground chain",
          all(f"@@{n}:abc123" in slow_cmd for n in ("FOCUS", "FGPKG", "PIDSTAT")),
          slow_cmd[:200])
    check("build_tick_command drops the chain without SLOW",
          "@@FOCUS:abc123" not in build_tick_command(
              "abc123", {TIER_FAST, TIER_MED}, 8, frozenset()))
    fake_secs = {"FOCUS": "mFocusedApp=x", "FGPKG": "42", "PIDSTAT": "1 (x) S 1"}
    check("section_due accepts the foreground chain on a SLOW tick",
          all(section_due(n, fake_secs, all_tiers, frozenset())
              for n in ("FOCUS", "FGPKG", "PIDSTAT")))
    check("section_due rejects the foreground chain without SLOW",
          not section_due("FOCUS", fake_secs, {TIER_FAST, TIER_MED},
                          frozenset()))
    check("section_due rejects an absent section",
          not section_due("FOCUS", {}, all_tiers, frozenset()))
    check("section_due gates GPU on off_sections",
          section_due("GPU", {"GPU": "x"}, all_tiers, frozenset())
          and not section_due("GPU", {"GPU": "x"}, all_tiers,
                              frozenset({"GPU"})))

    # --- wifi link channel ---------------------------------------------------
    # Fixtures are REAL captured bytes from the reference device, not a
    # remembered shape of the output. A parser written against a remembered
    # shape is how the in-repo `"ssid:" in text` anti-pattern shipped.
    #
    # This first one is the very pair that motivated the parser: the `WifiInfo:`
    # line says `IP: null` while the `NetworkCapabilities:` blob further down
    # says `IP: /172.16.0.189`. A whole-text `re.search(r"IP: ")` therefore reads
    # a different field than intended - and reads the OPTIMISTIC one, hiding the
    # exact state ("associated, no IP") in which a video client reports a
    # connection error. See PITFALLS #49.
    WIFI_INFO_NULL_IP = (
        "Wifi is enabled\n"
        "Wifi scanning is always available\n"
        "==== ClientModeManager instance: ConcreteClientModeManager{id=1 "
        "iface=wlan0 role=ROLE_CLIENT_PRIMARY} ====\n"
        'Wifi is connected to "WCS-DQA2-CC_5G_1"\n'
        'WifiInfo: SSID: "WCS-DQA2-CC_5G_1", BSSID: 44:c7:fc:0d:47:7d, '
        "MAC: 46:dd:3f:c4:9e:d7, IP: null, Security type: 2, "
        "Supplicant state: COMPLETED, Wi-Fi standard: 5, RSSI: -64, "
        "Link speed: 6Mbps, Tx Link speed: 6Mbps, "
        "Max Supported Tx Link speed: 866Mbps, Rx Link speed: -1Mbps, "
        "Frequency: 5180MHz, Net ID: 1\n"
        "NetworkCapabilities: [ Transports: WIFI Capabilities: "
        "NOT_METERED&INTERNET&VALIDATED TransportInfo: <SSID: <unknown ssid>, "
        "BSSID: 02:00:00:00:00:00, MAC: 02:00:00:00:00:00, "
        "IP: /172.16.0.189, Security type: 2, Supplicant state: COMPLETED, "
        "RSSI: -65, Link speed: 6Mbps> SignalStrength: -65 AdminUids: [1000] "
        'SSID: "WCS-DQA2-CC_5G_1" UnderlyingNetworks: Null]\n')
    w = parse_wifi(WIFI_INFO_NULL_IP)
    check("wifi: WifiInfo IP beats the NetworkCapabilities IP",
          bool(w) and w["state"] == "noip", str(w))
    check("wifi: ssid/rssi/link speed come off the WifiInfo line",
          bool(w) and w["ssid"] == "WCS-DQA2-CC_5G_1" and w["rssi"] == -64
          and w["link_mbps"] == 6.0, str(w))

    wifi_ok_fixture = WIFI_INFO_NULL_IP.replace("IP: null",
                                                "IP: /172.16.0.189", 1)
    w = parse_wifi(wifi_ok_fixture)
    check("wifi: a real IPv4 on the WifiInfo line is ok",
          bool(w) and w["state"] == "ok" and w["ip"] == "172.16.0.189", str(w))
    # Associated but no WifiInfo block at all: the IP is unknown, and a missing
    # RECORD is not evidence of an outage. A device that simply never prints
    # WifiInfo must not be permanently "down" - that would cap every run to WARN
    # over a formatting difference.
    w = parse_wifi('Wifi is enabled\nWifi is connected to "AP-X"\n')
    check("wifi: no WifiInfo block is ok, not an invented outage",
          bool(w) and w["state"] == "ok" and w["ssid"] == "AP-X", str(w))
    w = parse_wifi("Wifi is disabled\n")
    check("wifi: radio off is off", bool(w) and w["state"] == "off", str(w))
    w = parse_wifi("Wifi is enabled\nWifi scanning is always available\n")
    check("wifi: enabled with no association is noassoc",
          bool(w) and w["state"] == "noassoc", str(w))
    # The anti-pattern guard: `<unknown ssid>` is Android's placeholder for "not
    # visible to this caller", so text-matching it would call an unassociated
    # radio connected. Nothing here may be reported as a usable link.
    w = parse_wifi("Wifi is enabled\nNetworkCapabilities: [ TransportInfo: "
                   "<SSID: <unknown ssid>, IP: /10.0.0.1> ]\n")
    check("wifi: <unknown ssid> alone is not a connection",
          bool(w) and w["state"] != "ok" and w["ssid"] is None, str(w))
    # Empty and shell-error output mean the READ failed -> None. Anything else
    # non-empty means the read succeeded and we simply could not classify it ->
    # `unknown`, which counts as DOWN. That is deliberate and it is the safe
    # direction: an unrecognized format must never be summarized as a healthy
    # link. The cost is a run capped to WARN with `无法判定` on such a device,
    # which is the signal that something about this build changed.
    for junk in ("", "   ", "no such file or directory"):
        try:
            got = parse_wifi(junk)
        except Exception as e:       # noqa: BLE001 - the point IS no exception
            got = f"raised {type(e).__name__}"
        check(f"wifi: a failed read parses to None ({junk[:12]!r})",
              got is None, str(got))
    for junk in ("\x00\x01 nonsense", "???", "SomeFutureAndroidFormat: 1"):
        try:
            got = parse_wifi(junk)
        except Exception as e:       # noqa: BLE001
            got = f"raised {type(e).__name__}"
        check(f"wifi: unrecognized output is unknown, never ok ({junk[:12]!r})",
              isinstance(got, dict) and got["state"] == "unknown", str(got))

    # Requested -> accepted -> parsed -> counted. The first three are shape
    # assertions and shape cannot see a channel that was never wired up, so the
    # fourth one exists: a REAL blob has to travel the whole path and land in the
    # counters (PITFALLS #42).
    nonce_w = "b28e4d"
    cmd = build_tick_command(nonce_w, all_tiers, 8, frozenset())
    check("build_tick_command asks for the wifi section",
          f"@@{SEC_WIFI}:{nonce_w}" in cmd and WIFI_CMD in cmd, cmd[-120:])
    check("build_tick_command drops wifi when the channel is off",
          f"@@{SEC_WIFI}:{nonce_w}" not in build_tick_command(
              nonce_w, all_tiers, 8, frozenset({SEC_WIFI})))
    check("section_due gates WIFI on off_sections",
          section_due(SEC_WIFI, {"WIFI": "x"}, all_tiers, frozenset())
          and not section_due(SEC_WIFI, {"WIFI": "x"}, all_tiers,
                              frozenset({SEC_WIFI})))
    secs_w, _mis_w = split_sections(
        f"@@{SEC_WIFI}:{nonce_w}\n{wifi_ok_fixture}@@DONE:{nonce_w}\n", nonce_w)
    ev_w = Evidence("TESTDEV", 2000, tier_ticks_per(2000), 0, "", {})
    parsed_w = (parse_wifi(secs_w[SEC_WIFI])
                if section_due(SEC_WIFI, secs_w, all_tiers, frozenset())
                else None)
    if parsed_w:
        ev_w.note_wifi(0.0, 1, parsed_w["state"], parsed_w)
    check("wifi: request -> accept -> parse -> count, end to end",
          bool(parsed_w) and ev_w.wifi_counts.get("ok", 0) > 0
          and ev_w.wifi_fresh > 0, str(ev_w.wifi_counts))
    # A channel that was switched off is `not_due`, never `baseline`: `b` means
    # "first read, no delta", and the dead-channel reading must stay `n`.
    ev_off = Evidence("TESTDEV", 2000, tier_ticks_per(2000), 0, "", {})
    check("wifi: a disabled channel records no due ticks",
          ev_off.wifi_due == 0 and ev_off.wifi_state is None)

    # The transition sentinel. add_event's 60 s limiter is keyed on `kind`, and a
    # wifi transition is a PAIR - swallowing one half does not degrade the
    # timeline, it corrupts it (the outage would run to the end of the run). So
    # `_wifi_step` passes immediate=False. This drives the REAL emitter at 5 s
    # spacing: four transitions inside one 60 s window, all four must survive.
    # REVERT `immediate=False` AND THIS ASSERTION FAILS - that is the point of
    # it, and it was verified by actually doing so.
    mon_w = Monitor.__new__(Monitor)
    mon_w.ev = Evidence("TESTDEV", 2000, tier_ticks_per(2000), 0, "", {})
    mon_w.events_writer = None
    mon_w.events_csv = None
    mon_w.start_mono = time.monotonic()
    mon_w.wifi_fail_streak = 0
    mon_w.wifi_enabled = True
    mon_w.wifi_next_probe_s = 0.0
    with contextlib.redirect_stdout(io.StringIO()):
        for n, st in enumerate(("ok", "off", "ok", "off", "ok")):
            t = n * 5.0
            prev_w = mon_w.ev.wifi_state
            kind = mon_w.ev.note_wifi(t, 1_700_000_000_000 + int(t * 1000),
                                      st, {"state": st})
            if kind:
                mon_w._wifi_step(kind, prev_w, {"state": st})
    kinds = [e["type"] for e in mon_w.ev.events]
    check("wifi: transitions are NOT rate-limited away",
          kinds == ["wifi_lost", "wifi_back", "wifi_lost", "wifi_back"],
          str(kinds))
    check("wifi: a failed read is not a transition",
          wifi_section_failed(ST_FAILED) and wifi_section_failed(ST_ABSENT)
          and not wifi_section_failed("noip")
          and not wifi_section_failed("off"))

    # --- wifi windows --------------------------------------------------------
    def wifi_evidence(states, kind="healthy", step=30.0):
        """Freeze a synthetic report with a scripted wifi timeline."""
        d = _synthetic_evidence(kind)
        e = Evidence("TESTDEV", 2000, tier_ticks_per(2000), 0, "", {})
        for n, st in enumerate(states):
            t = n * step
            e.note_wifi(t, 1_700_000_000_000 + int(t * 1000), st, {"state": st})
        d["wifi"] = e.freeze("ok", "x.csv", False)["wifi"]
        return d

    d = wifi_evidence(["ok", "off", "off", "ok"])
    w = d["wifi"]
    check("wifi: a closed window records start, end and span",
          len(w["outages"]) == 1 and w["outages"][0]["closed"]
          and w["outages"][0]["start_t"] == 30.0
          and w["outages"][0]["end_t"] == 90.0
          and w["outages"][0]["duration_s"] == 60.0
          and w["outages"][0]["ticks"] == 2, json.dumps(w["outages"]))
    check("wifi: the window carries wall-clock times, not just tick offsets",
          w["outages"][0]["start_clock_ms"] == 1_700_000_030_000
          and w["outages"][0]["end_clock_ms"] == 1_700_000_090_000)
    check("wifi: the frozen total span matches the windows",
          w["down_s_span"] == 60.0 and w["down_ticks"] == 2, str(w["down_s_span"]))
    check("wifi: 30 s sampling is stated as the resolution, not hidden",
          w["resolution_s"] == 30.0 and "30" in w["note"], str(w["resolution_s"]))
    check("wifi: a healthy timeline reports no transition",
          w["n_transitions"] == 2)      # ok->off and off->ok; ok->ok is not one

    d_open = wifi_evidence(["ok", "off", "off"])
    wo = d_open["wifi"]
    check("wifi: an unclosed window is still reported",
          len(wo["outages"]) == 1 and not wo["outages"][0]["closed"]
          and wo["outages"][0]["end_t"] is None)
    v_open = judge(d_open)
    page_open = format_result_html({"verdict": v_open, "evidence": d_open,
                                    "config": {}, "device_id": "DEV",
                                    "stats": {}})
    check("wifi: an unclosed window renders a span, never a bare None",
          "None" not in page_open and "≥30.0" in page_open
          and "运行结束时仍未恢复" in page_open)
    check("wifi: the html says the run was capped by the outage",
          "被封顶为 WARN" in page_open)

    # The `>=` prefix belongs to OPEN windows only. A closed window's span is
    # t2 - t1 and the truth is within +-P of it, so `>=` would be an unfounded
    # claim: a real 10 s outage straddling one sample would print `>=30.0`.
    # Without this assertion the open-window check above still passes while
    # every closed window carries a false lower bound - the failure mode is
    # invisible because the wrong output is well-formed.
    # Assert on the SPAN CELL, not on the page: the coverage note legitimately
    # contains a `>=` of its own, so "no >= anywhere" would be testing the
    # wrong thing and would fail for the wrong reason.
    page_closed = format_result_html({"verdict": judge(d), "evidence": d,
                                      "config": {}, "device_id": "DEV",
                                      "stats": {}})
    check("wifi: a CLOSED window gets a plain span, no '>=' prefix",
          "≥60.0" not in page_closed and ">60.0<" in page_closed,
          "ge60=%r cell=%r" % ("≥60.0" in page_closed, ">60.0<" in page_closed))

    # Cap the window list, and say so - a silent truncation reads as a complete
    # timeline. The SUMMARY must survive the cap, so it is counted independently
    # of the window list.
    e_tr = Evidence("TESTDEV", 2000, tier_ticks_per(2000), 0, "", {})
    for n in range(WIFI_MAX_WINDOWS + 5):
        t = n * 60.0
        e_tr.note_wifi(t, 1, "off", {"state": "off"})
        e_tr.note_wifi(t + 30.0, 1, "ok", {"state": "ok"})
    wtr = e_tr.freeze("ok", "x.csv", False)["wifi"]
    check("wifi: the window list is capped and admits it",
          len(wtr["outages"]) == WIFI_MAX_WINDOWS and wtr["truncated"] is True,
          f"{len(wtr['outages'])} truncated={wtr['truncated']}")
    check("wifi: the capped total still counts every down sample",
          wtr["down_ticks"] == WIFI_MAX_WINDOWS + 5, str(wtr["down_ticks"]))
    d_tr = dict(_synthetic_evidence("healthy"), wifi=wtr)
    page_tr = format_result_html({"verdict": judge(d_tr), "evidence": d_tr,
                                  "config": {}, "device_id": "DEV",
                                  "stats": {}})
    check("wifi: the html states the truncation", "只保留了前" in page_tr)

    # --- wifi verdict interaction -------------------------------------------
    # User ruling 3: an outage CAPS the verdict at WARN. It must never do more
    # than that - a run that earned a FAIL keeps it.
    v_cap = judge(wifi_evidence(["ok", "off", "off", "ok"]))
    check("wifi: an outage caps OK down to WARN",
          v_cap["result"] == "WARN"
          and "-3=warn(WIFI)" in v_cap["capped_by"], str(v_cap["capped_by"]))
    check("wifi: the cap does not add a tenth gate",
          sorted(g["id"] for g in v_cap["gates"] if g["id"] > 0)
          == list(range(1, 10)))
    v_fail = judge(wifi_evidence(["ok", "off", "off", "ok"], kind="leak"))
    check("wifi: an outage cannot launder a FAIL into WARN",
          v_fail["result"] == "FAIL"
          and "-3=warn(WIFI)" in v_fail["capped_by"], v_fail["result"])
    v_clean = judge(wifi_evidence(["ok", "ok", "ok", "ok"]))
    check("wifi: a clean link caps nothing",
          "WIFI" not in " ".join(v_clean["capped_by"]))

    # --- wifi row ------------------------------------------------------------
    # The row must be non-empty on EVERY run, including one that never sampled
    # the link, and it must be printable to a GBK console.
    for label, dv in (("down", judge(wifi_evidence(["ok", "off", "off", "ok"]))),
                      ("up", judge(wifi_evidence(["ok", "ok"]))),
                      ("never sampled", judge(_synthetic_evidence("healthy")))):
        rows_w = {r["key"]: r["value"] for r in verdict_rows(dv)}
        val = rows_w.get("wifi") or ""
        check(f"wifi row: {label} renders a non-empty value", bool(val))
        check(f"wifi row: {label} is single-line ASCII with no 'report'",
              val.isascii() and "\n" not in val and "report" not in val, val)
    check("wifi row: 'down' says how long, 'up' says the AP",
          "span=" in dict((r["key"], r["value"])
                          for r in verdict_rows(
                              judge(wifi_evidence(["ok", "off", "off", "ok"]))))["wifi"]
          and "ssid=" in dict((r["key"], r["value"])
                              for r in verdict_rows(
                                  judge(wifi_evidence(["ok", "ok"]))))["wifi"])

    # --- ntc node temperatures ----------------------------------------------
    # The collector's contract is raw ADC counts and None, nothing else - the
    # conversion is an offline step (tools/ntc_convert.py), so this parser must
    # never grow one. Both halves are asserted, including the one that must never
    # raise: an exception here would kill an 8 h run over one unreadable sysfs
    # file.
    for _nt, _want in (("662\n371\n", {"lcd": 662, "led": 371}),
                       ("662 371\n", {"lcd": 662, "led": 371}),
                       ("662\n", {"lcd": 662, "led": None}),
                       ("", None),
                       ("garbage\ngarbage", None),
                       ("cat: /x: No such file or directory", None)):
        check(f"ntc: parse_ntc({_nt!r}) -> {_want!r}",
              parse_ntc(_nt) == _want, str(parse_ntc(_nt)))
    check("ntc: parse_ntc(None) is None and does not raise",
          parse_ntc(None) is None)
    check("ntc: a third line is ignored, not misread as lcd",
          parse_ntc("1\n2\n3\n") == {"lcd": 1, "led": 2})

    # The section has to be REQUESTED by the command builder, ACCEPTED by the due
    # predicate, PARSED and COUNTED. A parser that works when fed a blob by hand
    # proves one of those four; this repo has shipped a channel that was parsed
    # correctly and never asked for (PITFALLS #42), so all four are covered by
    # driving a REAL tick against a scripted device rather than calling parsers.
    #
    # adb_shell is the single seam every device read goes through, so replacing
    # it is what makes a tick possible with no device attached. The fake answers
    # ONLY the sections the command actually asked for: a fake that volunteered
    # sections nobody requested could not tell "the builder dropped it" from
    # "the predicate ignored it", which is the exact pair of failures at stake.
    seen_cmds: list[str] = []

    def fake_shell(_serial, cmd, _budget, **_kw):
        seen_cmds.append(cmd)
        if cmd == NTC_CMD:                      # the setup-time probe, unframed
            return 0, "662\n371\n", ""
        m = re.search(r"@@[A-Z]+:([0-9a-f]{6})", cmd)
        if not m:
            return 0, "", ""
        nonce = m.group(1)
        out = ""
        if f"@@UP:{nonce}" in cmd:
            out += f"@@UP:{nonce}\n8000.00 100.00\n"
        if f"@@{SEC_NTC}:{nonce}" in cmd:
            out += f"@@{SEC_NTC}:{nonce}\n662\n371\n"
        return 0, out + f"@@DONE:{nonce}\n", ""

    def one_tick(monitors):
        """A Monitor with no device behind it, driven for exactly one tick.

        setup() is deliberately not called: it creates samples.csv and
        events.csv, and --selftest writes no files. The three channel flags it
        would set after a successful probe are set directly instead, and the
        probe itself is asserted on its own two assertions below.
        """
        mx = Monitor("FAKEDEV", 2.0, 0, "", ".", monitors)
        mx.gpu_enabled = True
        mx.wifi_enabled = True
        mx.ntc_enabled = True
        mx.ev = Evidence("FAKEDEV", 2000, tier_ticks_per(2000), 0, "", {})
        mx.ev.monitors = list(mx.monitors)
        mx.ev.monitors_off = [g for g in MONITOR_IDS if g not in set(mx.monitors)]
        mx.csv = None
        mx.start_mono = time.monotonic()
        mx.origin_ms = mx._now_ms()        # k=0 lands on the origin: no sleep
        with contextlib.redirect_stdout(io.StringIO()):
            return mx, mx.tick(0)

    _real_shell = globals()["adb_shell"]
    globals()["adb_shell"] = fake_shell
    try:
        _probe_mon = Monitor("FAKEDEV", 2.0, 0, "", ".", MONITOR_DEFAULT)
        check("ntc: the setup-time probe accepts the real device bytes",
              _probe_mon._probe_ntc() is True, str(seen_cmds[-1:]))
        mon_on, row_on = one_tick(MONITOR_DEFAULT)
        mon_off, row_off = one_tick(["mem", "gpu", "wifi"])   # ntc NOT selected
    finally:
        globals()["adb_shell"] = _real_shell

    check("ntc: request -> accept -> parse -> count, in one real tick",
          row_on.get("ntc_lcd") == 662 and row_on.get("ntc_led") == 371
          and row_on.get("ntc_st") == ST_FRESH and mon_on.ev.ntc_fresh == 1
          and mon_on.ev.ntc_series.get("lcd") == [662],
          f"lcd={row_on.get('ntc_lcd')} led={row_on.get('ntc_led')} "
          f"st={row_on.get('ntc_st')} fresh={mon_on.ev.ntc_fresh}")
    check("ntc: a deselected channel leaves the command, it is not ignored",
          f"@@{SEC_NTC}:" in seen_cmds[-2] and f"@@{SEC_NTC}:" not in seen_cmds[-1],
          str([c[:70] for c in seen_cmds[-2:]]))
    # `d`, never `b`: `b` means "first read, no delta exists yet", and a
    # temperature has no delta to lack. A `b` here would say the channel is live
    # but young, which is the opposite of "switched off for this run".
    check("ntc: a deselected channel writes `d`, never `b`, and a blank cell",
          row_off.get("ntc_st") == ST_DISABLED
          and row_off.get("ntc_lcd") is None, str(row_off.get("ntc_st")))
    check("ntc: a deselected channel records no due tick",
          mon_off.ev.ntc_due == 0 and mon_off.ev.ntc_fresh == 0,
          f"due={mon_off.ev.ntc_due} fresh={mon_off.ev.ntc_fresh}")
    _frozen_on = mon_on.ev.freeze("ok", "x.samples.csv", False)
    _frozen_off = mon_off.ev.freeze("ok", "x.samples.csv", False)
    check("ntc: the frozen block states the unit and the 30 s resolution",
          _frozen_on["ntc"]["unit"] == NTC_UNIT
          and _frozen_on["ntc"]["resolution_s"] == 30.0
          and _frozen_on["ntc"]["channels"]["lcd"]["max"] == 662,
          json.dumps(_frozen_on["ntc"])[:120])
    check("ntc: it is a TOP-LEVEL key, not a tenth metric",
          "ntc" not in _frozen_on["metrics"] and "ntc" in _frozen_on)

    # The CSV contract, counted in Python rather than eyeballed. The three new
    # columns are also the reason NTC is a condition channel: the 9-char `st`
    # frame and the 9-wide {9} in ST_LINE_RE are untouched by them.
    check("csv: 33 columns, ntc between wifi_st and causes",
          len(CSV_COLUMNS) == 33
          and CSV_COLUMNS[CSV_COLUMNS.index("wifi_st") + 1:
                          CSV_COLUMNS.index("wifi_st") + 4]
          == ["ntc_lcd", "ntc_led", "ntc_st"]
          and CSV_COLUMNS[-2:] == ["causes", "events"], str(len(CSV_COLUMNS)))
    check("csv: a real tick's row fills all 33 of them",
          len([row_on.get(c, "") for c in CSV_COLUMNS]) == len(CSV_COLUMNS))
    check("csv: the st frame is still exactly 9 chars and still valid",
          len(row_on["st"].split(":")[1]) == 9
          and bool(ST_LINE_RE.match(row_on["st"])), row_on["st"])
    check("csv: a `d` metric state keeps the frame valid",
          bool(ST_LINE_RE.match("ok:" + ST_DISABLED * len(METRIC_ORDER))))
    check("csv: a deselected metric is never counted as a due opportunity",
          all(_frozen_off["metrics"][m]["n_due"] == 0
              and _frozen_off["metrics"][m]["coverage"] is None
              for m in ("fg_pkg", "fg_pid", "fg_cpu")),
          json.dumps({m: _frozen_off["metrics"][m]
                      for m in ("fg_pkg", "fg_pid", "fg_cpu")}))

    # --- the ntc row ---------------------------------------------------------
    for _lbl, _nev in (("sampled", _frozen_on["ntc"]), ("never sampled", {})):
        _val = _ntc_line(_nev)
        check(f"ntc row: {_lbl} renders a non-empty single-line ASCII value",
              bool(_val) and _val.isascii() and "\n" not in _val
              and "report" not in _val, _val)
    check("ntc row: states the unit, the average, the max AND the n",
          all(k in _ntc_line(_frozen_on["ntc"])
              for k in ("unit=raw_adc", "avg", "max", "n=1", "res=30s")),
          _ntc_line(_frozen_on["ntc"]))

    # --- the monitor selection -----------------------------------------------
    check("monitors: list, comma string and None all normalise",
          norm_monitors(["gpu", "ntc"]) == ["gpu", "ntc"]
          and norm_monitors("gpu,ntc") == ["gpu", "ntc"]
          and norm_monitors(None) == MONITOR_DEFAULT
          and norm_monitors([]) == [])
    check("monitors: output order is canonical, not the order given",
          norm_monitors("ntc,mem") == ["mem", "ntc"])
    check("monitors: unknown ids are dropped, and an all-unknown list falls "
          "back to the default rather than to monitoring nothing",
          norm_monitors("gpu,bogus") == ["gpu"]
          and norm_monitors("bogus") == MONITOR_DEFAULT)
    check("monitors: `fg` and `ntc` deselect their whole section group",
          monitors_off_sections(["mem", "gpu", "wifi"])
          == frozenset({"FOCUS", "FGPKG", "PIDSTAT", SEC_NTC})
          and monitors_off_sections(MONITOR_DEFAULT) == frozenset(),
          str(sorted(monitors_off_sections(["mem", "gpu", "wifi"]))))

    # The single-switch invariant, and the reason the whole off_sections
    # refactor was worth doing: for ANY `off` value, what the command ASKS FOR
    # and what the predicate ACCEPTS must be the same set. The bug this
    # replaces was exactly a disagreement between those two (PITFALLS #51) and
    # it was invisible - the channel went quiet while every gate said pass.
    _all_tiers = {TIER_FAST, TIER_MED, TIER_SLOW}
    _probe_secs = {n: "x" for n in SECTION_TIER}
    for _off in (frozenset(), frozenset({"MEM"}), frozenset({SEC_WIFI}),
                 frozenset({"FOCUS", "FGPKG", "PIDSTAT"}), frozenset({"GPU"}),
                 frozenset({"GPU", SEC_NTC}), frozenset(SECTION_TIER)):
        _cmd = build_tick_command("c0ffee", _all_tiers, 8, _off)
        _asked = {n for n in SECTION_TIER if f"@@{n}:c0ffee" in _cmd}
        _due = {n for n in SECTION_TIER
                if section_due(n, _probe_secs, _all_tiers, _off)}
        check(f"one switch: command and predicate agree on every section "
              f"(off={','.join(sorted(_off)) or 'none'})", _asked == _due,
              f"asked-only={sorted(_asked - _due)} due-only={sorted(_due - _asked)}")

    # The POSITIVE half of the same property, and the one shape that has
    # actually shipped here: an inverted predicate switching a channel off for a
    # whole run while every gate still reported pass. With nothing deselected,
    # every section must be both requested and accepted.
    check("one switch: with nothing deselected, every section is requested",
          all(f"@@{n}:c0ffee" in build_tick_command("c0ffee", _all_tiers, 8,
                                                    frozenset())
              for n in SECTION_TIER), str(sorted(SECTION_TIER)))
    check("one switch: with nothing deselected, every section is due",
          all(section_due(n, _probe_secs, _all_tiers, frozenset())
              for n in SECTION_TIER))

    # --- a gate must never pass on evidence nobody collected -----------------
    # These cases run against the REAL freeze(), so they pin the LEVEL of the
    # monitors key as well: placed one level too high, every one of them reads
    # as "nothing was deselected" and the override never fires.
    def judge_off(off, kind="healthy"):
        e_o = _synthetic_evidence(kind)
        e_o["run"]["monitors"] = [g for g in MONITOR_IDS if g not in off]
        e_o["run"]["monitors_off"] = list(off)
        return judge(e_o)

    # The cases above hand-patch the dict, so they prove what judge() DOES with
    # the key but not that freeze() puts it where judge() looks - and the
    # distinction is not academic: an earlier revision of this change stored it
    # one level up, every one of those cases still passed, and in a real run the
    # override read an empty set and gates 7/8/9 went back to a silent pass.
    # These two read a report that went through freeze() like a real one.
    # .get() rather than [] on purpose: if the block ever moves back out of
    # `run` this has to FAIL BY NAME. With direct indexing it raised KeyError and
    # took the whole selftest down with it, which reports nothing about which
    # property broke - a crash is not a test result.
    _frozen_run = _frozen_off.get("run") or {}
    check("the deselection rides inside run, where judge and _display look",
          _frozen_run.get("monitors_off") == ["fg", "ntc"]
          and _frozen_run.get("monitors") == ["mem", "gpu", "wifi"],
          str({k: v for k, v in _frozen_off.items()
               if k in ("run", "monitors", "monitors_off")})[:160])
    _v_frozen_off = judge(_frozen_off)
    check("judge applies the override to a report it did not have patched",
          all("switched off for this run" in g["detail"]
              for g in _v_frozen_off["gates"] if g["id"] in (8, 9)),
          str([(g["id"], g["status"], g["detail"][:45])
               for g in _v_frozen_off["gates"] if g["id"] in (7, 8, 9)]))

    _v_all = judge_off([])
    _v_fg = judge_off(["fg"])
    _v_mem = judge_off(["mem"])
    check("deselecting fg makes gates 8 and 9 inconclusive, not pass",
          [g["status"] for g in _v_fg["gates"] if g["id"] in (8, 9)]
          == ["inconclusive", "inconclusive"],
          str([(g["id"], g["status"]) for g in _v_fg["gates"]]))
    check("deselecting fg turns the whole result INCONCLUSIVE",
          _v_fg["result"] == "INCONCLUSIVE", _v_fg["result"])
    check("deselecting mem makes gate 7 inconclusive",
          [g["status"] for g in _v_mem["gates"] if g["id"] == 7]
          == ["inconclusive"],
          str([(g["id"], g["status"]) for g in _v_mem["gates"]]))
    # The other direction, and it matters just as much: these three feed no
    # gate, so deselecting them must change nothing about the judgement.
    check("deselecting gpu/wifi/ntc leaves every gate and the result untouched",
          judge_off(["gpu", "wifi", "ntc"])["gates"] == _v_all["gates"]
          and judge_off(["gpu", "wifi", "ntc"])["result"] == _v_all["result"])
    # An archived report predates the key entirely and must read as fully
    # monitored, not crash and not silently drop gates.
    _e_old = _synthetic_evidence("healthy")
    _e_old["run"].pop("monitors_off", None)
    check("evidence without a monitors key behaves as fully monitored",
          judge(_e_old)["gates"] == _v_all["gates"])

    # --- the scope row -------------------------------------------------------
    _scope_on = _scope_line({"monitors": MONITOR_DEFAULT, "monitors_off": []})
    _scope_off = _scope_line(_synthetic_evidence("healthy")["run"])
    check("scope row: non-empty, single-line, ASCII, no literal report",
          all(bool(s) and s.isascii() and "\n" not in s and "report" not in s
              for s in (_scope_on, _scope_off)), _scope_on)
    check("scope row: says `all` when nothing was deselected, `off=` otherwise",
          "off=" not in _scope_on and "off=fg" in _scope_line(
              {"monitors": ["mem", "gpu", "wifi"], "monitors_off": ["fg"]}),
          _scope_on)
    check("scope row: reads the deselection off the real frozen run block",
          "all 5 monitor(s)" in _scope_off, _scope_off)

    # --- a deselected row must SAY it was not monitored -----------------------
    # The SCOPE row states this once, at run level. These four are the row-level
    # versions the reader actually lands on, and before this they were the
    # misleading shape: APP printed "STABLE switches=0 pkgs=0 gone=0" and
    # MEMORY printed "NO LEAK delta=n/a ..." - all zeroes, because nothing was
    # ever asked of the channel, which reads exactly like "looked, found nothing
    # wrong". The two condition rows printed "? no observation in this run",
    # which cannot tell "switched off" from "read nothing all run".
    _d_all = judge_off([])["display"]
    _d_fg = judge_off(["fg"])["display"]
    _d_mem = judge_off(["mem"])["display"]
    _d_ntc = judge_off(["ntc"])["display"]
    _d_wifi = judge_off(["wifi"])["display"]
    check("a deselected monitor's row says NOT MONITORED, not a clean bill",
          "NOT MONITORED" in _d_fg["app"]
          and "NOT MONITORED" in _d_mem["memory"],
          "%s | %s" % (_d_fg["app"][:60], _d_mem["memory"][:60]))
    check("...and it does not print the all-zero counters that read as healthy",
          "switches=0" not in _d_fg["app"] and "NO LEAK" not in _d_mem["memory"],
          _d_fg["app"][:90])
    check("the condition rows separate `switched off` from `read nothing`",
          "not monitored" in _d_ntc["ntc"] and "not monitored" in _d_wifi["wifi"],
          "%s | %s" % (_d_ntc["ntc"][:55], _d_wifi["wifi"][:55]))
    check("a fully monitored run is untouched by any of this",
          "NOT MONITORED" not in _d_all["app"]
          and "NOT MONITORED" not in _d_all["memory"]
          and "STABLE" in _d_all["app"], _d_all["app"][:90])
    # NOT MONITORED is a value. The row contract is "every row renders a value
    # on every run", so the fix must not have been implemented by emptying the
    # row - that would trade a misleading line for a missing one.
    check("NOT MONITORED is still a non-empty value on every row",
          all(v for v in _d_fg.values()),
          "empty: %s" % str([k for k, v in _d_fg.items() if not v]))

    # The console block goes to a GBK console and then through the WS pipe, so
    # it has to be pure ASCII - including the NOTES, not just the row values. A
    # Chinese string in a note is invisible until it reaches a real terminal,
    # which is exactly how one got in during this change.
    for kind in ("healthy", "leak", "short", "reboot"):
        blk = format_result_lines(judge(_synthetic_evidence(kind)),
                                  "reports/perf_x.json")
        check(f"{kind}: the whole console block is pure ASCII",
              all(l.isascii() for l in blk),
              str([l for l in blk if not l.isascii()][:1]))
        check(f"{kind}: exactly one sniffable report path, still the last line",
              [n for n, l in enumerate(blk)
               if " : " in l and "report" in l
               and l.rsplit(" : ", 1)[1].strip().lower().endswith(".json")]
              == [len(blk) - 1])

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

    # The params table must report the selection THIS RUN was configured with,
    # not the declared default. Defaulted, the table and the SCOPE row disagree
    # inside one report - and the table is the one a reader trusts first.
    def _monitors_row(ev: dict) -> str:
        page = format_result_html({
            "device_id": "TESTDEV", "test_time": "2026-09-14 00:00:00",
            "config": {"interval_ms": 2000, "duration_sec": 0},
            "verdict": judge(ev), "stats": metric_stats(ev), "evidence": ev})
        for tr in re.findall(r"<tr>.*?</tr>", page, re.S):
            if "<small>monitors</small>" in tr:
                return tr
        return ""

    def _monitors_cell(ev: dict) -> str:
        """The value cell alone. Its neighbour prints the DEFAULT for
        comparison whenever the two differ, so a whole-row assertion cannot
        tell "this run selected it" from "the default says it"."""
        m = re.search(r'<td class="v">(.*?)</td>', _monitors_row(ev))
        return m.group(1) if m else ""

    ev_mo = _synthetic_evidence("healthy")
    ev_mo["run"]["monitors"] = ["mem", "gpu", "wifi", "ntc"]
    row_off = _monitors_row(ev_mo)
    cell_off = _monitors_cell(ev_mo)
    check("html: params table shows the monitors this run selected",
          "WiFi" in cell_off and "NTC" in cell_off
          and "前台应用" not in cell_off, cell_off[:200])
    check("html: a deselected monitor is not reported as collected",
          "本次未传" not in row_off, row_off[:200])

    ev_one = _synthetic_evidence("healthy")
    ev_one["run"]["monitors"] = ["mem"]
    cell_one = _monitors_cell(ev_one)
    check("html: a one-item selection does not leak the others into the table",
          "内存" in cell_one and "GPU" not in cell_one, cell_one[:200])

    ev_nk = _synthetic_evidence("healthy")
    ev_nk["run"].pop("monitors", None)
    check("html: no recorded selection falls back to the declared default",
          "本次未传" in _monitors_row(ev_nk))

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

    # --- probe-cost sampling order -------------------------------------------
    # The cost table's product is the difference between adjacent rows, so its
    # rows have to be sampled at the same moment or drift is booked as cost. The
    # order is asserted on the REAL function through the same adb_shell seam the
    # ntc block above uses, with the pacing neutralised: the 0.3 s sleep is not
    # part of the invariant, and three seconds of real sleeping would prove
    # nothing the command sequence does not already show.
    #
    # A variant is identified by the sections its command asks for, since that is
    # the only thing that differs between them: MEM appears from FAST+MED up,
    # FOCUS from FULL up, then WIFI and NTC. Five variants, five distinct
    # fingerprints - so "the first five commands are five DIFFERENT variants" is
    # a statement the recorded sequence can actually be checked against.
    cost_cmds: list[str] = []

    def cost_shell(_serial, cmd, _budget, **_kw):
        cost_cmds.append(cmd)
        return 0, "", ""

    def _fp(cmd):
        return (f"@@MEM:" in cmd, f"@@FOCUS:" in cmd,
                f"@@{SEC_WIFI}:" in cmd, f"@@{SEC_NTC}:" in cmd)

    _real_shell = globals()["adb_shell"]
    _real_sleep = time.sleep
    globals()["adb_shell"] = cost_shell
    time.sleep = lambda _s: None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            run_probe_cost("FAKEDEV", reps=2)
    finally:
        time.sleep = _real_sleep
        globals()["adb_shell"] = _real_shell

    _fps = [_fp(c) for c in cost_cmds]
    check("probe-cost: 2 reps x 5 variants is still 10 adb calls",
          len(cost_cmds) == 10, str(len(cost_cmds)))
    # The invariant, stated without reference to any expected schedule: a round
    # has to cover every variant before it repeats one. Under block sampling the
    # first five commands are the SAME variant five times.
    check("probe-cost: one round covers every variant before repeating",
          len(set(_fps[:5])) == 5, str(_fps[:5]))
    # ...and the exact schedule, because the invariant alone would also pass for
    # an order that visits the variants in some other rotation.
    _exp = [(False, False, False, False),      # FAST (v2)
            (True, False, False, False),       # FAST+MED (v2)
            (True, True, False, False),        # FULL (v2)
            (True, True, True, False),         # FULL+WIFI
            (True, True, True, True)]          # FULL+WIFI+NTC
    check("probe-cost: rounds are round-major, and the rows still read in "
          "cost order", _fps == _exp * 2, str(_fps))

    # --- NTC auto-conversion: the end-of-run derived files (D-65) ------------
    # mon_on / mon_off above are REAL Monitors driven through REAL ticks - one
    # with the NTC channel selected, one without - so the gate is exercised on
    # the state a run actually reaches rather than on a hand-set counter. Both
    # routes to "no NTC" (unticked, and never successfully read) land on the
    # same counter, which is why one assertion covers them.
    _real_tool = globals()["run_tool"]
    _conv_calls: list[list[str]] = []

    def fake_tool(argv, _budget):
        _conv_calls.append(list(argv))
        return 0, (
            "profile : 9660_P53_2G (/somewhere/9660_P53_2G.ini)\n"
            "source  : x.samples.csv\n"
            "rows    : 30\n"
            # A path the tool chose (so the echo cannot have been rebuilt here)
            # AND one containing "report" (so the separator really is being
            # removed, not merely absent from a friendly fixture).
            "wrote   : E:\\ProjectorPressureTest\\reports\\stress-test\\perf"
            "\\ZZZ_from_the_tool.samples.temps.csv\n"
            "chart   : E:\\ProjectorPressureTest\\reports\\stress-test\\perf"
            "\\ZZZ_from_the_tool.samples.temps.png (light)\n"
            "[warn] a warning with : an odd separator\n"), ""

    def conv(mon):
        """Drive _convert_ntc() with its stdout captured; never lets it raise."""
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                mon._convert_ntc()
        except Exception as e:                                  # noqa: BLE001
            return None, repr(e)
        return buf.getvalue(), None

    _conv_calls.clear()
    globals()["run_tool"] = fake_tool
    try:
        out_off, err_off = conv(mon_off)
    finally:
        globals()["run_tool"] = _real_tool
    check("ntc-convert: a run that collected no NTC spawns nothing, prints "
          "nothing, and does not raise",
          err_off is None and _conv_calls == [] and out_off == "",
          f"err={err_off} calls={_conv_calls} out={out_off!r}")

    _conv_calls.clear()
    globals()["run_tool"] = fake_tool
    try:
        out_on, err_on = conv(mon_on)
    finally:
        globals()["run_tool"] = _real_tool
    check("ntc-convert: a run that read NTC calls the tool once, with this "
          "run's own samples.csv and the profile it was given",
          err_on is None and len(_conv_calls) == 1
          and _conv_calls[0][1:] == [NTC_CONVERT_TOOL, mon_on.csv_path,
                                     "--profile", NTC_PROFILE_DEFAULT],
          f"err={err_on} calls={_conv_calls}")

    _ntc_lines = [l for l in (out_on or "").splitlines() if l.startswith("[ntc]")]

    def _sniffable(line):
        """The rule server._sniff_report_path actually applies (see above)."""
        return (" : " in line and "report" in line
                and line.rsplit(" : ", 1)[1].strip().lower().endswith(".json"))

    check("ntc-convert: the paths echoed are the TOOL's own, not rebuilt here",
          any("ZZZ_from_the_tool.samples.temps.csv" in l for l in _ntc_lines)
          and any("ZZZ_from_the_tool.samples.temps.png" in l
                  for l in _ntc_lines), str(_ntc_lines))
    check("ntc-convert: only [ntc] lines reach stdout, one line each",
          bool(out_on)
          and all(l.startswith("[ntc] ") for l in out_on.splitlines()),
          repr(out_on))
    check("ntc-convert: no [ntc] line carries the ' : ' separator, and none "
          "is sniffable as the report path",
          all(" : " not in l for l in _ntc_lines)
          and not any(_sniffable(l) for l in _ntc_lines), str(_ntc_lines))
    check("ntc-convert: the [ntc] lines are pure ASCII (D-07)",
          all(ord(c) < 128 for c in (out_on or "")), repr(out_on))

    _real_tool_path = globals()["NTC_CONVERT_TOOL"]
    globals()["NTC_CONVERT_TOOL"] = os.path.join(".", "no_such_ntc_tool.py")
    try:
        _conv_calls.clear()
        out_miss, err_miss = conv(mon_on)
    finally:
        globals()["NTC_CONVERT_TOOL"] = _real_tool_path
    _miss = (out_miss or "").splitlines()
    check("ntc-convert: a missing tool is one honest line, not an exception",
          err_miss is None and _conv_calls == [] and len(_miss) == 1
          and _miss[0].startswith("[ntc] no conversion:"),
          f"err={err_miss} calls={_conv_calls} out={out_miss!r}")

    def dead_tool(_argv, _budget):
        return None, "", "pc budget expired"

    globals()["run_tool"] = dead_tool
    try:
        out_dead, err_dead = conv(mon_on)
    finally:
        globals()["run_tool"] = _real_tool
    _dead = (out_dead or "").splitlines()
    check("ntc-convert: a wedged tool costs one line, and still no exception",
          err_dead is None and len(_dead) == 1
          and _dead[0].startswith("[ntc] no conversion: rc=None"),
          f"err={err_dead} out={out_dead!r}")

    _pvals = [c["value"] for c in NTC_PROFILE_CHOICES]
    check("ntc-convert: the profile select leads with the shipped default, then "
          "sorts, with no duplicates",
          bool(_pvals) and _pvals[0] == NTC_PROFILE_DEFAULT
          and _pvals[1:] == sorted(_pvals[1:])
          and len(set(_pvals)) == len(_pvals), str(_pvals))
    check("ntc-convert: the choices are bare names - templates/ and _ files are "
          "never selectable",
          all(not v.startswith("_") and "/" not in v and "\\" not in v
              and not v.endswith(".ini") for v in _pvals), str(_pvals))
    check("ntc-convert: what the select offers by default is a profile that "
          "exists on disk",
          os.path.isfile(os.path.join(NTC_PROFILE_DIR,
                                      NTC_PROFILE_DEFAULT + ".ini")),
          os.path.join(NTC_PROFILE_DIR, NTC_PROFILE_DEFAULT + ".ini"))

    # --- the NTC read goes through su 0, and an empty probe now says why -----
    check("ntc-read: both nodes are read through `su 0` - SELinux denies the "
          "shell domain, so a bare `cat` gives EMPTY stdout and the failure is "
          "invisible",
          "su 0 cat" in NTC_CMD
          and NTC_CMD.index(NTC_LCD_PATH) < NTC_CMD.index(NTC_LED_PATH),
          NTC_CMD)

    def _probe_with(out, err):
        """Run the real _probe_raw against a stubbed adb_shell."""
        real = globals()["adb_shell"]
        globals()["adb_shell"] = lambda *_a, **_k: (0, out, err)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                got = _probe_raw("FAKE", "ntc", NTC_CMD)
        finally:
            globals()["adb_shell"] = real
        return got, buf.getvalue()

    _got_ok, _line_ok = _probe_with("662\n371\n", "")
    check("ntc-read: a readable channel still prints exactly ONE line - the "
          "first value",
          _got_ok.replace("\r", "") == "662\n371\n"
          and _line_ok.count("\n") == 1 and "662" in _line_ok
          and "(empty)" not in _line_ok,
          repr(_line_ok))

    _got_bad, _line_bad = _probe_with("", "cat: /x: Permission denied\n")
    check("ntc-read: an EMPTY stdout reports the reason from stderr instead of "
          "a bare (empty) - this is the line whose absence hid the bug",
          _line_bad.count("\n") == 1
          and "(empty)" in _line_bad
          and "Permission denied" in _line_bad,
          repr(_line_bad))

    _got_non, _line_non = _probe_with("", "\u00e9chec\n")
    check("ntc-read: a non-ASCII reason cannot abort the print on a GBK "
          "console (D-07)",
          all(ord(c) < 128 for c in _line_non) and "chec" in _line_non,
          repr(_line_non))

    _got_none, _line_none = _probe_with("", "")
    check("ntc-read: a silent failure with no stderr at all still prints one "
          "line rather than nothing",
          _line_none.count("\n") == 1 and "(empty)" in _line_none
          and "rc=0" in _line_none,
          repr(_line_none))

    print(f"\n[selftest] {len(fails)} failure(s)")
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, serial: str, interval_sec: float, duration_sec: int,
                 watch_pkg: str, report_dir: str, monitors=None,
                 ntc_profile: str = NTC_PROFILE_DEFAULT):
        self.serial = serial
        self.dev_short = serial.replace(":", "_").replace(".", "_")
        self.watch_pkg = watch_pkg
        # Read only by _convert_ntc, and only to pick which profile the
        # conversion tool is pointed at. Defaulted rather than required so that
        # every existing construction site - including --selftest's - keeps
        # working unchanged.
        self.ntc_profile = ntc_profile or NTC_PROFILE_DEFAULT
        # The monitors this run was ASKED for, normalised to canonical order.
        # `off_static` is the part of off_sections() that the user chose; the
        # rest is added at runtime by the degradation policy.
        self.monitors = norm_monitors(monitors)
        self.off_static = monitors_off_sections(self.monitors)
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

        # WiFi link channel. No `wifi_prev_state` here: the state machine lives
        # in Evidence.note_wifi, which owns the last observation, so there is
        # exactly one copy of it and no way for the two to drift apart.
        self.wifi_enabled = False
        self.wifi_fail_streak = 0
        self.wifi_next_probe_s = 0.0

        self.ntc_enabled = False
        self.ntc_fail_streak = 0
        self.ntc_next_probe_s = 0.0

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

    def off_sections(self) -> frozenset:
        """The ONE answer to "which sections must not be collected this tick".

        Computed once per tick and handed to BOTH section_due() and
        build_tick_command(), so the two cannot disagree - which is exactly how
        the bug this replaces was born (see PITFALLS #51). A degraded channel and
        a deselected one end up in the same set, because from the sampler's point
        of view they are the same thing: do not spend the round trip.

        The distinction that must survive is in the STATE CHAR, not here: a
        deselected section writes `d` for every metric it feeds, while a degraded
        one writes `d` too - and `SCOPE` in the report is what tells a reader
        which of the two happened.
        """
        off = set(self.off_static)
        if not self.gpu_enabled:
            off.add("GPU")
        if not self.wifi_enabled:
            off.add(SEC_WIFI)
        if not self.ntc_enabled:
            off.add(SEC_NTC)
        return frozenset(off)

    def _probe_wifi(self) -> bool:
        """One bounded read to decide whether the channel is usable at all.

        Wrapped in `timeout` for the same reason the tick command is: this is a
        binder call into system_server, and an unbounded one would spend the PC
        budget before the first tick. No `su` - measured as plain shell on the
        reference device, so this can never be the thing that fails on a board
        where root is unavailable.
        """
        _rc, out, _err = adb_shell(self.serial, WIFI_CMD, self._pc_budget_s())
        return parse_wifi(out) is not None

    def _probe_ntc(self) -> bool:
        """One bounded read of both NTC nodes to decide whether they are usable.

        Wrapped in `timeout` for the same reason the tick command is, and here it
        matters even more than for wifi: an unbounded read of a sysfs file behind
        a wedged driver would spend the whole PC budget, and an expired budget
        marks EVERY metric of that tick ST_FAILED - the run would lose all its
        data to report one unreadable temperature. Wrapped, the worst case is one
        partial tick.

        `su 0`, like the Mali counters. The old note here said "no `su`, measured
        as plain shell on the reference device, where both nodes are
        world-readable" - the mode is indeed 0644, but that measurement was
        taken on a unit whose adbd was root at the time, and generalising from
        it is what let this fail with nothing on screen. Under Enforcing the
        `shell` domain is denied `sysfs` and a bare `cat` yields empty stdout.
        A board without root now loses NTC rather than keeping it; that is the
        deliberate trade, the same one the GPU tier already makes.
        """
        _rc, out, _err = adb_shell(self.serial, NTC_CMD, self._pc_budget_s())
        return parse_ntc(out) is not None

    # -- lifecycle ----------------------------------------------------------
    def setup(self) -> bool:
        rc, _o, _e = adb_shell(self.serial, "true", 5.0)
        if rc != 0:
            return False
        self.transit_ms = measure_transit_ms(self.serial)
        # A deselected section is never probed. That is the point of deselecting
        # it: "not monitored this run" has to mean zero device traffic, not one
        # probe plus no samples. It also keeps `sources` honest - a channel that
        # was never sampled must not advertise a source in the report.
        gpu_ok = self._probe_gpu() if "GPU" not in self.off_static else False
        self.gpu_enabled = gpu_ok
        wifi_ok = (self._probe_wifi() if SEC_WIFI not in self.off_static
                   else False)
        self.wifi_enabled = wifi_ok
        ntc_ok = (self._probe_ntc() if SEC_NTC not in self.off_static else False)
        self.ntc_enabled = ntc_ok
        sources = {
            "cpu": "/proc/stat",
            "mem": "/proc/meminfo",
            "gpu": "/sys/kernel/debug/mali0" if gpu_ok else None,
            "fg": "dumpsys window + pidof",
            "wifi": WIFI_SOURCE if wifi_ok else None,
            "ntc": NTC_SOURCE if ntc_ok else None,
        }
        self.ev = Evidence(self.serial, self.t_ms, self.ticks_per,
                           self.duration_sec, self.watch_pkg,
                           sources)
        # Set before the first tick, like guard_s/transit_ms, so a partial
        # snapshot taken mid-run still states the scope it was run under.
        self.ev.monitors = list(self.monitors)
        self.ev.monitors_off = [g for g in MONITOR_IDS
                                if g not in set(self.monitors)]
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
        print(f"[perf] monitors: {'+'.join(self.monitors) or '(none)'}"
              f"{'  OFF=' + ','.join(sorted(self.off_static)) if self.off_static else ''}")
        print(f"[perf] sources: cpu={sources['cpu']} mem={sources['mem']} "
              f"gpu={sources['gpu'] or 'n/a'} fg={sources['fg'] or 'off'} "
              f"wifi={sources['wifi'] or 'n/a'} ntc={sources['ntc'] or 'n/a'}")
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
            # `monitors` is what makes the file self-describing. Without it, a
            # column that is empty for a whole run cannot be told apart from a
            # column that was never requested - both render as blank cells with
            # a `d` status char, and `d` legitimately means either.
            f"# config_monitors={','.join(self.monitors) or '(none)'}"
            f"{' off=' + ','.join(sorted(self.off_static)) if self.off_static else ''}",
            f"# tiers=FAST:1,MED:{tp[TIER_MED]},SLOW:{tp[TIER_SLOW]}",
            f"# metrics={','.join(METRIC_ORDER)}",
            "# sources=cpu=stat,mem=meminfo,gpu=mali/dvfs,fg=window+pidof,"
            "wifi=cmd_wifi_status,ntc=iio_in_voltage3_2_raw",
            # Declared because an ADC count and a celsius reading are the same
            # shape of number. Every value in the two ntc columns is in THIS unit,
            # and the conversion is a SEPARATE OFFLINE TOOL (tools/ntc_convert.py,
            # driven by a per-project profile) rather than something this script
            # does - so the column names stay bare (`ntc_lcd`, not `ntc_lcd_c`) and
            # will not be renamed later: a rename would make archived CSVs
            # impossible to compare across versions, and the header already
            # carries the unit.
            f"# ntc_unit={NTC_UNIT} ntc_paths=lcd:{NTC_LCD_PATH},led:{NTC_LED_PATH}",
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

    def _convert_ntc(self) -> None:
        """Turn this run's raw ADC counts into Celsius, if there were any.

        Called at the very END of finish(), after the reports are already on
        disk, so that nothing here can move the verdict. These are DERIVED
        files (D-65): if the tool is missing, wedged, or refuses the profile,
        the run still ends with exactly the result it would have had, and the
        only cost is the two files and the reason printed here.

        The gate is `ntc_fresh` and nothing else. note_ntc is never called for a
        deselected section, so that counter is 0 for BOTH "the sensor channel
        was unticked" and "the node never once read" - one condition, one
        source, which is the whole of PITFALLS #51.

        The output paths are NOT derived here. The tool states them (`wrote`,
        `chart`) and this echoes what it said, because a second derivation of
        "where did it go" is a second thing to keep in step with D-63's
        contract - and the archived copy is moved by server.py anyway.

        One line per fact, never the tool's statistics table: the console is a
        stream the operator reads while a device is under load.
        """
        ev = getattr(self, "ev", None)
        if ev is None or ev.ntc_fresh <= 0:
            return
        if not os.path.isfile(NTC_CONVERT_TOOL):
            print(f"[ntc] no conversion: {NTC_CONVERT_TOOL} not found")
            return
        rc, out, err = run_tool(
            [sys.executable, NTC_CONVERT_TOOL, self.csv_path,
             "--profile", self.ntc_profile], NTC_CONVERT_BUDGET_S)
        for line in (out or "").splitlines():
            s = line.strip()
            raw = s.split(" ", 1)[0] if s else ""
            head = raw.strip("[]")
            if head not in ("wrote", "chart", "profile", "source",
                            "warn", "error"):
                continue
            # Re-spelled, not re-derived. The tool's own separator is ` : ` and
            # the paths it prints contain "report" (reports/stress-test/perf/).
            # A line carrying BOTH is exactly what server._sniff_report_path
            # looks for, so the separator is removed here rather than trusted
            # not to matter - otherwise the next `wrote` line this tool grows
            # would be free to break the console contract.
            print(f"[ntc] {head}: {s[len(raw):].lstrip(' :')}".rstrip()
                  .replace(" : ", ": "))
        if rc != 0:
            tail = (err or "").strip().splitlines()
            print(f"[ntc] no conversion: rc={rc}"
                  + (f" {tail[-1]}" if tail else ""))

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
        # AFTER the reports, deliberately, and on every exit path from this
        # method: the verdict is already durable before the first byte of the
        # conversion is produced. See _convert_ntc.
        self._convert_ntc()
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
        # ONE set, computed once, handed to both the builder and the predicate
        # below. Two switches for one decision is the whole of PITFALLS #51.
        off = self.off_sections()
        cmd = build_tick_command(nonce, tiers, int(self.guard_s), off)
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
        # A section switched off for this run writes `d` for every metric it
        # feeds - NOT `n` or `h`. That difference earns its keep twice over: `d`
        # is what exempts the slot from the whole-tick failure rewrite further
        # down (a deselected metric must never be reported as a failure), and it
        # is the only thing that lets a later reader of samples.csv tell
        # "deliberately not sampled" apart from "sampled and the read failed".
        def skip_state(sec: str, held: bool) -> str:
            if sec in off:
                return ST_DISABLED
            return ST_HELD if held else ST_NOT_DUE

        for m in ("fg_pkg", "fg_pid", "fg_cpu"):
            states[m] = skip_state("FOCUS", m in self.hold)

        # There IS a per-section opt-out here again, and it was removed once for
        # a good reason: the foreground chain used to be gated on a
        # `track_foreground` flag, and collapsing that flag away left an INVERTED
        # predicate that switched the whole channel off for a whole run while
        # every gate still reported pass (PITFALLS #51 records the family).
        #
        # Three things make it safe this time. `off` is ONE object computed once
        # per tick from ONE source of truth (MONITOR_GROUPS) and handed to both
        # this predicate and the command builder, so the two cannot drift - the
        # previous shape had a flag per channel and the call sites had already
        # diverged. A switched-off section writes `d`, so it is visible in every
        # row rather than merely absent. And --selftest asserts that for several
        # `off` values the predicate and the command agree on EVERY section, plus
        # that nothing is off in the all-on case, which turns an inverted
        # predicate into a test failure instead of a month of wrong data.
        def due_sec(name: str) -> bool:
            return section_due(name, secs, tiers, off)

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
            states["mem"] = skip_state("MEM", "mem" in self.hold)

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
        else:
            states["gpu"] = skip_state("GPU", self.gpu_prev is not None)
            states["gpu_clk"] = skip_state("GPU", "gpu_clk" in self.hold)

        # --- wifi link (a condition channel: it feeds no metric) -------------
        # Two rules, and neither may be inverted:
        #   1. `sec_fail` is incremented ONLY by wifi_section_failed(), i.e. only
        #      when the READ failed. A link that is down is a fact about the run,
        #      not a sampling problem, so it must not make this tick partial.
        #      (If it did, an outage would also drive `ok` toward 0 and gate 1
        #      would call a perfectly healthy CPU run INCONCLUSIVE.)
        #   2. The row VALUE for a failed read is None, matching every metric.
        #      `note_wifi` deliberately leaves its own last-known state alone, so
        #      the state machine survives the gap - but the CSV row for a tick
        #      that read nothing must be a hole, or a flat line would be drawn
        #      through the outage.
        wifi_val: str | None = None
        if due_sec(SEC_WIFI):
            parsed = parse_wifi(secs[SEC_WIFI])
            if parsed is None:
                states_wifi = section_error_kind(secs[SEC_WIFI])
                sec_fail += 1
                self._wifi_degraded(t_sec)
            else:
                sec_ok += 1
                self.wifi_fail_streak = 0
                states_wifi = ST_FRESH
                wifi_val = parsed["state"]
                prev_wifi = self.ev.wifi_state
                kind = self.ev.note_wifi(t_sec, int(time.time() * 1000),
                                         parsed["state"], parsed)
                if kind:
                    self._wifi_step(kind, prev_wifi, parsed)
        else:
            states_wifi = skip_state(SEC_WIFI, self.ev.wifi_state is not None)

        # --- ntc node temperatures (a condition channel, like wifi) ----------
        # Same two rules as the wifi block above, and for the same reasons:
        # `sec_fail` moves ONLY on a failed READ (a hot node is a fact about the
        # run, not a sampling problem, and letting it count would drive `ok`
        # toward 0 and make gate 1 call a healthy run INCONCLUSIVE), and the row
        # VALUE for a failed read is None so no chart is drawn through the gap.
        #
        # No event is emitted here. A temperature has no transitions to announce,
        # so there is no state machine, no window list, and nothing to add to the
        # 60 s event limiter - the curve belongs to Excel, which is what the
        # requirement asked for.
        ntc_vals: dict | None = None
        if due_sec(SEC_NTC):
            parsed_n = parse_ntc(secs[SEC_NTC])
            if parsed_n is None:
                states_ntc = section_error_kind(secs[SEC_NTC])
                sec_fail += 1
                self._ntc_degraded(t_sec)
            else:
                sec_ok += 1
                self.ntc_fail_streak = 0
                states_ntc = ST_FRESH
                ntc_vals = parsed_n
            self.ev.note_ntc(t_sec, states_ntc, ntc_vals)
        else:
            states_ntc = skip_state(SEC_NTC, self.ev.ntc_last_t is not None)

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

        # wifi: zero-order hold exactly like a metric, so a tick that did not
        # sample the link still says what was last known (with `h` saying so).
        # Every other status char - `n` with no history, `x`/`a`, `d` - yields a
        # blank cell, i.e. a hole, which is the honest rendering.
        if states_wifi == ST_FRESH:
            row["wifi"] = wifi_val
        elif states_wifi == ST_HELD and self.ev.wifi_state is not None:
            row["wifi"] = self.ev.wifi_state
        else:
            row["wifi"] = None
        row["wifi_st"] = states_wifi

        # ntc: zero-order hold exactly like wifi, so a tick that did not sample
        # the nodes still says what was last known (with `h` saying so). Every
        # other status char - `n` with no history, `x`/`a`, `d` - yields a blank
        # cell, i.e. a hole, which is the honest rendering and also what stops a
        # chart from being drawn straight through an outage.
        for ch in NTC_CHANNELS:
            if states_ntc == ST_FRESH:
                row["ntc_" + ch] = (ntc_vals or {}).get(ch)
            elif states_ntc == ST_HELD:
                row["ntc_" + ch] = self.ev.ntc_last.get(ch)
            else:
                row["ntc_" + ch] = None
        row["ntc_st"] = states_ntc

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
        """Re-test a degraded GPU counter, at most once per interval.

        Two conditions before spending a round trip, and both were missing
        something. A section the user DESELECTED must never be re-probed at all
        (`off_static`): the whole point of deselecting is zero device traffic.
        And a FAILED re-probe has to push the next attempt forward too - it used
        to leave `gpu_next_probe_s` at 0.0, so a device whose mali node is
        permanently unreadable got re-probed on EVERY tick, which cost more than
        the channel it was trying to bring back. That is the same shape as the
        wifi drift PITFALLS #51 records: two paths over one piece of state, and
        one of them forgetting to advance it.
        """
        if self.gpu_enabled or "GPU" in self.off_static:
            return
        if t_sec < self.gpu_next_probe_s:
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
        else:
            self.gpu_next_probe_s = t_sec + GPU_REPROBE_INTERVAL_S

    def _wifi_step(self, kind: str, prev: str | None, parsed: dict) -> None:
        """Announce a link transition.

        `immediate=False` is REQUIRED here, not a style choice. add_event's 60 s
        limiter is keyed on `kind` alone, and a wifi transition is a PAIR
        (lost/back): swallowing one half does not degrade the timeline, it
        CORRUPTS it - the down window would run to the end of the run and the
        report would claim an outage that never happened. `rollover` already
        uses this same bypass.

        Skipping the limiter is safe here for a structural reason rather than a
        hopeful one: a transition can only be observed on a DUE tick (SLOW, and
        only when the state actually changed), so the worst case is 2 events per
        30 s. That bound is a property of the sampler, not of the limiter, and
        it is written down in PERF_MONITOR_V2 12.10 rather than being hidden
        behind a rate limit that would eat real edges.
        """
        ssid = parsed.get("ssid") or "-"
        ip = parsed.get("ip") or "-"
        if kind == "wifi_lost":
            self._emit("wifi_lost", parsed["state"],
                       f"from={prev or '-'} state={parsed['state']} "
                       f"ssid={ssid} ip={ip}", immediate=False)
            return
        w = self.ev.wifi_closed_window()
        down_s = w["duration_s"] if w and w["duration_s"] is not None else 0.0
        ticks = w["ticks"] if w else 0
        self._emit("wifi_back", parsed["state"],
                   f"down={down_s:g}s ({ticks} tick(s)) ssid={ssid} ip={ip}",
                   immediate=False)

    def _wifi_degraded(self, t_sec: float) -> None:
        self.wifi_fail_streak += 1
        if self.wifi_enabled and self.wifi_fail_streak >= WIFI_DEGRADE_AFTER:
            self.wifi_enabled = False
            self.wifi_next_probe_s = t_sec + WIFI_REPROBE_INTERVAL_S
            self._emit("src_degraded", "wifi channel unreadable",
                       f"{self.wifi_fail_streak} consecutive failures")

    def maybe_reprobe_wifi(self, t_sec: float) -> None:
        """See maybe_reprobe_gpu for why the deselected case and the failed
        re-probe are both guarded here."""
        if self.wifi_enabled or SEC_WIFI in self.off_static:
            return
        if t_sec < self.wifi_next_probe_s:
            return
        if self._probe_wifi():
            self.wifi_enabled = True
            self.wifi_fail_streak = 0
            self.wifi_next_probe_s = 0.0
            # meta.sources may only GROW: shrinking it would make the frontend
            # rebuild the series and discard the good data before the outage.
            src = dict(self.ev.sources)
            src["wifi"] = WIFI_SOURCE
            self.ev.sources = src
            print("PERF|" + json.dumps({"type": "meta", "sources": src,
                                        "judge_version": JUDGE_VERSION},
                                       ensure_ascii=True))
            self._emit("src_recovered", "wifi channel readable again")
        else:
            self.wifi_next_probe_s = t_sec + WIFI_REPROBE_INTERVAL_S

    def _ntc_degraded(self, t_sec: float) -> None:
        self.ntc_fail_streak += 1
        if self.ntc_enabled and self.ntc_fail_streak >= NTC_DEGRADE_AFTER:
            self.ntc_enabled = False
            self.ntc_next_probe_s = t_sec + NTC_REPROBE_INTERVAL_S
            self._emit("src_degraded", "ntc channel unreadable",
                       f"{self.ntc_fail_streak} consecutive failures")

    def maybe_reprobe_ntc(self, t_sec: float) -> None:
        """Reuses src_degraded / src_recovered rather than inventing a kind: both
        names are about a DATA SOURCE, and an unreadable NTC node is the same
        event as an unreadable GPU counter from any reader's point of view."""
        if self.ntc_enabled or SEC_NTC in self.off_static:
            return
        if t_sec < self.ntc_next_probe_s:
            return
        if self._probe_ntc():
            self.ntc_enabled = True
            self.ntc_fail_streak = 0
            self.ntc_next_probe_s = 0.0
            # meta.sources may only GROW: shrinking it would make the frontend
            # rebuild the series and discard the good data before the outage.
            src = dict(self.ev.sources)
            src["ntc"] = NTC_SOURCE
            self.ev.sources = src
            print("PERF|" + json.dumps({"type": "meta", "sources": src,
                                        "judge_version": JUDGE_VERSION},
                                       ensure_ascii=True))
            self._emit("src_recovered", "ntc channel readable again")
        else:
            self.ntc_next_probe_s = t_sec + NTC_REPROBE_INTERVAL_S

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
                        '"watch_pkg"?, "key_ini"?, "ntc_profile"?}')
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
    # An empty string is "not answered", not "no profile": there is no such
    # thing as converting with no profile, so the default is what it falls to.
    ntc_profile = (str(params.get("ntc_profile",
                                  defaults["ntc_profile"]) or "").strip()
                   or NTC_PROFILE_DEFAULT)
    _asked = [t for t in monitor_tokens(params.get("monitors",
                                                    defaults["monitors"])) if t]
    _unknown = [t for t in _asked if t not in MONITOR_IDS]
    # norm_monitors falls back to ALL when nothing matched, so the log has to say
    # so - the operator asked for something and is getting something else.
    _fell_back = bool(_asked) and not (set(_asked) & set(MONITOR_IDS))
    monitors = norm_monitors(params.get("monitors", defaults["monitors"]))

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
    print(f"[config] ntc_profile     = {ntc_profile}")
    print(f"[config] monitors        = {','.join(monitors) or '(none)'}")
    if _unknown:
        print(f"[config] monitors        = ignored unknown id(s) "
              f"{','.join(_unknown)}")
    if _fell_back:
        # Monitoring NOTHING is never the right answer to a typo: it would turn
        # every gate INCONCLUSIVE for a reason the operator never chose. The
        # other direction - monitoring more than asked - only costs time.
        print("[config] monitors        = nothing recognisable was requested, "
              "monitoring ALL items instead of none")

    report_dir = os.path.join(PROJECT_ROOT, "reports", "stress-test", "perf")
    mon = Monitor(args.device, interval_sec, duration_sec, watch_pkg,
                  report_dir, monitors, ntc_profile=ntc_profile)
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
            mon.maybe_reprobe_wifi(mon._elapsed_s())
            mon.maybe_reprobe_ntc(mon._elapsed_s())
            row = mon.tick(k)
            print("PERF|" + json.dumps({
                "type": "sample", "clock": row["clock_ms"], "t": row["t_sec"],
                "st": row["st"], "gap_ms": row["gap_ms"],
                "cpu": row.get("cpu"), "gpu": row.get("gpu"),
                "mem": row.get("mem"), "fg_cpu": row.get("fg_cpu"),
                "gpu_clk": row.get("gpu_clk"), "fg_pkg": row.get("fg_pkg"),
                "wifi": row.get("wifi"),
                # Deliberately carries no `series` entry anywhere: the frontend
                # builds its chart series from a fixed key list, so a new key here
                # is ignored rather than misplotted, and no chart is resized. The
                # NTC curve belongs to Excel (user ruling, 2026-09-20).
                "ntc_lcd": row.get("ntc_lcd"), "ntc_led": row.get("ntc_led"),
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
