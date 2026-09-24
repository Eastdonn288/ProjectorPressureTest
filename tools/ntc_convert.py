#!/usr/bin/env python3
"""NTC raw ADC counts -> Celsius, off a perf_monitor samples.csv.

WHY THIS IS NOT IN perf_monitor
    perf_monitor records the two NTC nodes as RAW ADC COUNTS and says so in the
    file (`# ntc_unit=raw_adc`). The conversion deliberately does not live in the
    collector, because the constants that drive it are per-PROJECT: another board
    may use a different divider resistor, a different B, a different ADC full
    scale. Baking any one board's numbers into the collector would silently
    mismeasure every other board.

    So the split is:
      collector -> raw counts, unit declared, one column per node
      this tool -> counts -> Celsius, using the profile for THIS project

    Which also keeps the archived samples.csv comparable across projects and
    across revisions of the formula.

USAGE
    python tools/ntc_convert.py <samples.csv> [--profile NAME] [--out PATH]
    python tools/ntc_convert.py --list-profiles
    python tools/ntc_convert.py --selftest

    Default output is `<input>.temps.csv` next to the input. The input is never
    modified and never moved: the archive belongs to the run that produced it.
"""

import argparse
import contextlib
import csv
import io
import math
import re
import sys
import tempfile
from pathlib import Path

TOOL_VERSION = "1.3.1"
# The profile this tool ships with, named after the project it was written for
# (9660 / P53 / 2G, renamed from `default` on 2026-09-21). It is NOT a fallback:
# which profile a run uses is decided by resolve_profile_name() below, which
# only guesses when there is exactly one to guess from.
SHIPPED_PROFILE_NAME = "9660_P53_2G"
PROFILE_DIR = Path(__file__).resolve().parent / "ntc_profiles"

# The two nodes perf_monitor collects, in the order samples.csv carries them.
CHANNELS = ("lcd", "led")

KELVIN = 273.15
T0_C = 25.0
T0_K = KELVIN + T0_C

# The vendor Java rejects any reading at or above this count. The NTC config file
# has no equivalent key, so it is a tool default a profile may override.
ADC_INVALID_MIN_DEFAULT = 1000
PROFILE_KEY_ADC_INVALID_MIN = "PPTP ADC INVALID MIN"

# The vendor Java's SECOND B value, the one it switches to on the cold side of
# the curve, and the resistance it switches at. Optional as a PAIR: a profile
# carrying both converts in two segments, a profile carrying neither converts on
# one B (how every profile behaved before 2026-09-21). Carrying exactly one is
# neither, and is refused.
COLD_B_KEYS = ("NTC B VALUE COLD", "NTC B VALUE COLD2")
PROFILE_KEY_B_SWITCH_R = "NTC B SWITCH R"

# Column vocabulary of the file this tool writes.
OUT_RAW = {"lcd": "ntc_lcd_raw", "led": "ntc_led_raw"}
OUT_C = {"lcd": "ntc_lcd_c", "led": "ntc_led_c"}
OUT_ST = {"lcd": "ntc_lcd_st", "led": "ntc_led_st"}
SRC_C = {"lcd": "ntc_lcd", "led": "ntc_led"}
SRC_ST = "ntc_st"

ST_OK, ST_OOR, ST_BAD, ST_NONE = "ok", "oor", "bad", "-"

# The chart's labels are Chinese: this file's output goes to a PNG a person reads,
# never to stdout, which is the same exception ROW_NOTES / GATE_ZH carry in
# scripts/perf_monitor.py. Every print() in this tool stays ASCII.
CH_LABEL = {"lcd": "LCD 节点", "led": "LED 节点"}

# A chart is a claim about RESOLUTION, so these are the two decisions the picture
# has to encode honestly:
#
# 1. The staircase. These channels are read on the SLOW tier - one reading every
#    30 s - and the collector holds that reading until the next one. Zero-order
#    hold IS piecewise constant, so a step is its own shape. A slanted line between
#    two points 30 s apart asserts a temperature at every instant in between, and
#    nothing measured those instants. The fresh readings get a dot each, so the
#    real sample rate is visible instead of inferred from the slope - for as long
#    as the dots can be told apart at all, which is what DOT_MIN_GAP_PX decides.
#    Past that they are not drawn, and the subtitle says so.
#
# 2. The palette. Not a taste decision - both pairs below were run through the
#    categorical validator (lightness band, chroma floor, CVD separation,
#    normal-vision separation, contrast against the surface) and pass every gate:
#      light  #2a78d6 / #eb6834 on #fcfcfb - CVD dE 24.7, normal dE 33.6, all >= 3:1
#      dark   #3987e5 / #d95926 on #1a1a19 - CVD dE 26.8, normal dE 31.8, all >= 3:1
#    Slot order is part of that: lcd is slot 1, led is slot 2, and swapping them
#    would repaint a series the reader already learned. The selftest re-checks
#    these hexes, so a nicer-looking blue cannot land without re-validating.
CHART_THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e",
              "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
              "lcd": "#2a78d6", "led": "#eb6834"},
    "dark": {"surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7",
             "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
             "lcd": "#3987e5", "led": "#d95926"},
}
CHART_FIGSIZE = (11.0, 4.6)
CHART_DPI = 150
# The layout lives here rather than inline in the subplots_adjust / margins calls
# because the dot rule below is stated in DEVICE PIXELS - the units the reader's
# eye works in - and this makes that arithmetic reproducible without a renderer
# (and therefore assertable in the selftest).
CHART_ADJUST = {"left": 0.075, "right": 0.965, "top": 0.82, "bottom": 0.30}
CHART_MARGIN_X = 0.02
CHART_MARGIN_Y = 0.10
MARKER_PT = 8.0
MARKER_EDGE_PT = 2.0
# A dot claims "a reading happened here". It can only make that claim while the
# eye can tell it from its neighbour, which needs the mark's own diameter PLUS
# the ring that separates it. The ring is painted in the surface colour, so the
# moment the dots collide it stops separating them and starts erasing them AND
# whatever they are drawn on top of - the staircase.
#
# This is not a hypothetical: a 14.5 h run at one reading per 30 s is 1737 dots
# on a ~1468 px plot area, i.e. 0.81 px apart, ~26x too dense. Measured on the
# PNG (series-coloured pixels inside the axes): 11930 with the dots off, 1974
# with them on. The curve was the thing that disappeared.
#
# So past this gap the dots are not drawn - not drawn smaller, not thinned out.
# A shrunken dot is not the mark a shorter run taught the reader, and at this
# density it is sub-pixel anyway. Nothing is lost: the per-tick readings are all
# in the CSV, and the reading period is still in the subtitle.
DOT_MIN_GAP_PX = (MARKER_PT + MARKER_EDGE_PT) * CHART_DPI / 72.0


class ProfileError(Exception):
    """A profile or an input that cannot drive a conversion. Never swallowed."""


class ChartUnavailable(Exception):
    """The PNG could not be drawn. Never fatal: the CSV is the product."""


# ---------------------------------------------------------------------------
# The profile: the vendor's own config format, parsed tolerantly
# ---------------------------------------------------------------------------
# These files are written by hand and by other tools, in a format that is only
# almost INI: `KEY = value;` with a trailing semicolon, sometimes `KEY:` instead
# of `KEY =`, `#` comments, quoted paths. The parser accepts all of it -
# including the vendor file verbatim, which is the point: adding a project should
# be a file copy, not a translation.
# The trailing `[;:]?` matters more than it looks: the vendor files transcribe
# their own terminator loosely, so `NTC R2 = 4.7:` shows up beside `NTC R = 4.7;`.
# A colon in the MIDDLE of a value (a sysfs path, say) is untouched, because only
# a trailing separator can be consumed.
_VENDOR_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _]*?)\s*[:=]\s*(.*?)\s*[;:]?\s*$")


def parse_profile_text(text):
    """Vendor config text -> {NORMALISED KEY: raw string value}.

    Keys are upper-cased with runs of whitespace collapsed, so `NTC  B Value` and
    `ntc b value` are one key. Values keep their own case. Unknown keys are kept,
    not dropped: the profile doubles as the record of which board it came from,
    and discarding what we do not consume would hide a typo'd key.
    """
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _VENDOR_LINE.match(line)
        if not m:
            continue
        key = " ".join(m.group(1).upper().split())
        out[key] = m.group(2).strip().strip('"').strip()
    return out


class Profile:
    """The constants one project's NTC conversion needs, per channel.

    Per-channel values follow the vendor's own naming: the bare key is channel 0
    and the `2` suffix is channel 1 (`NTC B VALUE` / `NTC B VALUE2`, `NTC R` /
    `NTC R2`). Temperature compensation is the exception - it is named by channel
    up front (`NTC1 TEMP COMP` / `NTC2 TEMP COMP`) and falls back to the bare
    `NTC TEMP COMP` and then to zero, so a profile that does not compensate still
    loads instead of failing.
    """

    def __init__(self, name, path, raw):
        self.name = name
        self.path = path
        self.raw = raw

        def text(key):
            if key not in raw:
                raise ProfileError(
                    "%s: missing key %r (see tools/ntc_profiles/README.md)"
                    % (path, key))
            return raw[key]

        def num(key):
            try:
                return float(text(key))
            except ValueError:
                raise ProfileError("%s: %s is not a number: %r"
                                   % (path, key, raw[key]))

        def optional_num(key, default):
            return default if key not in raw else num(key)

        def pair(base, suffix2):
            """`NTC B VALUE` / `NTC B VALUE2` -> one value per channel."""
            return [num(base), num("%s%s" % (base, suffix2))]

        self.r_pull = [v * 1000.0 for v in pair("NTC R", "2")]        # ohms
        self.r0 = [v * 1000.0 for v in pair("NTC RESISTANCE", "2")]   # ohms
        self.b = pair("NTC B VALUE", "2")

        # The vendor's second segment. Absent is the normal case for a profile
        # written before 2026-09-21, and is not an error - it just means the
        # board's own implementation has no switch to mirror.
        self.b_cold = None
        self.b_switch_r = None
        present = [k in raw for k in COLD_B_KEYS]
        if any(present) and not all(present):
            raise ProfileError(
                "%s: %s must be given for BOTH channels or for neither"
                % (path, " / ".join(COLD_B_KEYS)))
        if all(present):
            if PROFILE_KEY_B_SWITCH_R not in raw:
                raise ProfileError(
                    "%s: %s is given without %s - a cold-side B that never "
                    "switches is a constant, not a second segment"
                    % (path, COLD_B_KEYS[0], PROFILE_KEY_B_SWITCH_R))
            self.b_cold = [num(COLD_B_KEYS[0]), num(COLD_B_KEYS[1])]
            self.b_switch_r = num(PROFILE_KEY_B_SWITCH_R)

        comp_default = optional_num("NTC TEMP COMP", 0.0)
        self.comp = [optional_num("NTC1 TEMP COMP", comp_default),
                     optional_num("NTC2 TEMP COMP", comp_default)]

        self.paths = [text("NTC1 PATH"), text("NTC2 PATH")]
        self.sar_full_scale = num("NTC SAR VALUE MAX")
        self.value_min = num("NTC VALUE MIN")
        self.value_max = num("NTC VALUE MAX")
        self.way = num("NTC OBTAIN TEMP WAY")
        self.adc_invalid_min = optional_num(PROFILE_KEY_ADC_INVALID_MIN,
                                           ADC_INVALID_MIN_DEFAULT)

        if self.way != 1:
            raise ProfileError(
                "%s: NTC OBTAIN TEMP WAY = %g. This tool implements the FORMULA "
                "path (1) only; the table path (0) needs the vendor's "
                "resistance/temperature table, which no config here carries."
                % (path, self.way))
        if self.sar_full_scale <= 1:
            raise ProfileError("%s: NTC SAR VALUE MAX = %g"
                               % (path, self.sar_full_scale))
        try:
            self.ntc_count = int(float(raw.get("NTC COUNT", len(CHANNELS))))
        except ValueError:
            raise ProfileError("%s: NTC COUNT is not a number: %r"
                               % (path, raw.get("NTC COUNT")))

    def describe(self):
        # `b_cold=none` is printed rather than omitted on purpose. A reader of an
        # archived temps.csv has to be able to tell WHICH B rule produced the
        # numbers in it, and an absent field reads as "not recorded", not as
        # "single segment".
        if self.b_cold is None:
            segment = "b_cold=none b_switch_r=none"
        else:
            segment = ("b_cold=%s b_switch_r=%g"
                       % (",".join("%g" % b for b in self.b_cold),
                          self.b_switch_r))
        return ("r_pull_k=%s r0_k=%s b=%s %s comp=%s sar_full_scale=%g "
                "adc_invalid_min=%g range=%g..%g"
                % (",".join("%g" % (r / 1000.0) for r in self.r_pull),
                   ",".join("%g" % (r / 1000.0) for r in self.r0),
                   ",".join("%g" % b for b in self.b),
                   segment,
                   ",".join("%g" % c for c in self.comp),
                   self.sar_full_scale, self.adc_invalid_min,
                   self.value_min, self.value_max))


def load_profile(name, profile_dir=None):
    """Read one profile by name from the profile directory."""
    d = Path(profile_dir) if profile_dir else PROFILE_DIR
    path = d / ("%s.ini" % name)
    if not path.exists():
        raise ProfileError(
            "no profile %r in %s (available: %s)"
            % (name, d, ", ".join(list_profiles(profile_dir)) or "none"))
    return Profile(name, path,
                   parse_profile_text(io.open(path, encoding="utf-8",
                                              errors="replace").read()))


def list_profiles(profile_dir=None):
    d = Path(profile_dir) if profile_dir else PROFILE_DIR
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.ini"))


def resolve_profile_name(requested, profile_dir=None):
    """Which profile to use when --profile was not given.

    Exactly one profile on disk is the only case where guessing is SAFE, so it is
    the only case where this tool guesses. With two or more, silence would mean
    converting one project's board with another project's constants and printing
    a plausible Celsius number that nothing downstream flags - the risk written
    down in TODO.md 6.19 ("换项目时忘了换 profile,就会悄悄出一份用错参数的摄氏度").
    The tool's own header line records which constants were used, but that is a
    post-hoc audit, not a guard. Refusing to guess is the guard.

    Note this is NOT `SHIPPED_PROFILE_NAME`: that constant names the file this
    repo ships, but it must never become an implicit fallback, or the guard above
    is defeated the moment a second project's profile is added.
    """
    if requested:
        return requested
    d = Path(profile_dir) if profile_dir else PROFILE_DIR
    available = list_profiles(profile_dir)
    if len(available) == 1:
        return available[0]
    if not available:
        raise ProfileError(
            "no profile in %s - expected at least %s.ini"
            % (d, SHIPPED_PROFILE_NAME))
    raise ProfileError(
        "%d profiles in %s (%s) - pass --profile to say which one. Guessing "
        "would convert this board with another project's constants."
        % (len(available), d, ", ".join(available)))


# ---------------------------------------------------------------------------
# The conversion
# ---------------------------------------------------------------------------
def adc_to_celsius(adc, profile, channel):
    """THE single conversion point. Every Celsius number this tool emits comes
    from here; nothing downstream re-derives or re-scales one.

    Mirrors the vendor's Java `transformNTCToTemp` with two deliberate
    departures, both decided rather than accidental:

      * TWO B SEGMENTS when the profile carries them, which is how the Java
        itself works: B = 4010 until the computed divider resistance rises past
        `NTC B SWITCH R`, then B = 3950. The test is on the RESISTANCE, not on
        the temperature, and it is a strict `>` - that is the Java's own order
        and operator, and it matters because the resistance is all it has at
        that point in the calculation. A profile without the pair converts on a
        single B. That is the pre-2026-09-21 behaviour, kept because a board
        whose implementation has no switch needs no second segment.

      * `sar_full_scale - 1` reproduces the Java's hard-coded `1023 - adcVal`
        while honouring a config that declares the full-scale COUNT (1024). A
        12-bit board declaring 4096 gets 4095 - adc, which is the same
        relationship rather than the same literal.

    Returns None - never a sentinel, never a raise - when the reading cannot
    produce a temperature: a count at or above the invalid threshold is a shorted
    or open node per the vendor's own error check, and a count of zero would put
    a log() of zero through the formula. A None means "no temperature", and the
    caller records that as `bad` rather than as a very cold day.
    """
    if adc is None:
        return None
    if adc <= 0 or adc >= profile.adc_invalid_min:
        return None
    denom = profile.sar_full_scale - 1.0 - adc
    if denom <= 0:
        return None

    resistance = profile.r_pull[channel] * adc / denom
    b = profile.b[channel]
    if profile.b_cold is not None and resistance > profile.b_switch_r:
        b = profile.b_cold[channel]
    temp = resistance / profile.r0[channel]
    temp = math.log(temp)
    temp = temp / b
    temp = temp + 1.0 / T0_K
    temp = 1.0 / temp
    return temp - KELVIN + profile.comp[channel]


def classify_c(temp, profile):
    """`ok` / `oor` / `bad` for a converted value.

    Out-of-range is REPORTED, not clamped. The config declares the range the
    vendor considers real, and a reading outside it is either a genuine fault or
    a wrong constant in the profile - both of which the reader has to see.
    Clamping would turn the second into a plausible number.
    """
    if temp is None:
        return ST_BAD
    if temp < profile.value_min or temp > profile.value_max:
        return ST_OOR
    return ST_OK


# ---------------------------------------------------------------------------
# Reading a perf_monitor samples.csv
# ---------------------------------------------------------------------------
class Samples:
    def __init__(self, path, preamble, header, rows):
        self.path = path
        self.preamble = preamble      # list[str], the `#` lines
        self.header = header          # list[str]
        self.rows = rows              # list[list[str]]

    def preamble_value(self, key):
        """Read one `key=value` out of the `#` preamble.

        The preamble packs SEVERAL keys onto one line - the real file reads
        `# ntc_unit=raw_adc ntc_paths=lcd:/sys/...in_voltage3_raw,led:...` - so
        this scans whitespace-separated tokens, not whole lines. Values never
        contain a space.
        """
        prefix = key + "="
        for line in self.preamble:
            for token in line.lstrip("#").strip().split():
                if token.startswith(prefix):
                    return token[len(prefix):]
        return None


def read_samples(path):
    """Parse a samples.csv, keeping its `#` preamble.

    The preamble is not decoration: it is the only place a samples.csv declares
    what unit its NTC columns are in and which sysfs node each column came from,
    and this tool cross-checks the profile against both.
    """
    raw_rows = list(csv.reader(io.open(path, encoding="utf-8", errors="replace")))
    preamble, body = [], []
    for r in raw_rows:
        if not r:
            continue
        if r[0].startswith("#"):
            preamble.append(r[0])
        else:
            body.append(r)
    if not body:
        raise ProfileError("%s: no data - only a preamble" % path)
    return Samples(path, preamble, body[0], body[1:])


def find_column(header, name):
    try:
        return header.index(name)
    except ValueError:
        return None


def check_unit(samples):
    """Refuse a file that does not declare its NTC columns are raw ADC counts.

    Without this, a converted or hand-edited file could be converted a second
    time - 28 C goes in as a count of 28 and comes out as a very hot node, with
    no error anywhere. The `ntc_unit=` declaration is what separates those two
    files, and every perf_monitor that emits the ntc columns emits it too, so a
    file that lacks it is not a file this tool produced.
    """
    unit = samples.preamble_value("ntc_unit")
    if unit != "raw_adc":
        raise ProfileError(
            "%s: the preamble declares ntc_unit=%s, not raw_adc. Converting "
            "anything but raw counts would produce confident nonsense, so this "
            "is refused rather than guessed at."
            % (samples.path, unit if unit else "(nothing)"))


# ---------------------------------------------------------------------------
# The conversion pass
# ---------------------------------------------------------------------------
def convert_samples(samples, profile):
    """-> (out_rows, summary).

    `out_rows` is ready to write. `summary` reports per channel how many rows
    carried a value, how many were FRESH readings, and the average/extremes over
    the fresh ones only.
    """
    header = samples.header
    i_t = find_column(header, "t_sec")
    if i_t is None:
        raise ProfileError("%s: no t_sec column" % samples.path)
    i_v = {ch: find_column(header, SRC_C[ch]) for ch in CHANNELS}
    missing = [SRC_C[ch] for ch in CHANNELS if i_v[ch] is None]
    if missing:
        raise ProfileError(
            "%s: no %s column(s). This file predates the NTC channel (v2.13.0) "
            "or the run deselected it - either way there is nothing to convert."
            % (samples.path, ", ".join(missing)))
    i_st = find_column(header, SRC_ST)

    out = []
    # Fresh readings only, mirroring the collector. A held tick ('h') repeats the
    # previous temperature, which is a fair thing to DRAW and a dishonest thing to
    # AVERAGE - counting it weights a value by how long the machine sat still.
    # The perf verdict's own n= is over fresh readings, so averaging the other way
    # would print two different means for one run.
    series = {ch: [] for ch in CHANNELS}
    seen = {ch: 0 for ch in CHANNELS}
    fresh = {ch: 0 for ch in CHANNELS}
    bad = {ch: 0 for ch in CHANNELS}
    oor = {ch: 0 for ch in CHANNELS}

    for row in samples.rows:
        state = row[i_st] if (i_st is not None and i_st < len(row)) else ""
        is_fresh = state == "f"
        raws, cs, sts = {}, {}, {}
        for ch in CHANNELS:
            j = i_v[ch]
            cell = ((row[j] if j < len(row) else "") or "").strip()
            adc = None
            if cell:
                try:
                    adc = float(cell)
                except ValueError:
                    adc = None
            c = adc_to_celsius(adc, profile, CHANNELS.index(ch))
            raws[ch] = cell
            cs[ch] = "" if c is None else "%.2f" % c
            sts[ch] = ST_NONE if not cell else classify_c(c, profile)
            if cell:
                seen[ch] += 1
                if is_fresh:
                    fresh[ch] += 1
                if c is None:
                    bad[ch] += 1
                elif sts[ch] == ST_OOR:
                    oor[ch] += 1
                elif is_fresh:
                    series[ch].append(c)
        out.append([row[i_t], raws["lcd"], raws["led"], cs["lcd"], cs["led"],
                    state, sts["lcd"], sts["led"]])

    summary = {}
    for ch in CHANNELS:
        vals = series[ch]
        summary[ch] = {
            "seen": seen[ch], "fresh": fresh[ch], "n": len(vals),
            "avg": (sum(vals) / len(vals)) if vals else None,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
            "bad": bad[ch], "oor": oor[ch],
        }
    return out, summary


OUT_HEADER = ["t_sec", OUT_RAW["lcd"], OUT_RAW["led"],
              OUT_C["lcd"], OUT_C["led"], SRC_ST,
              OUT_ST["lcd"], OUT_ST["led"]]


def out_preamble(samples, profile):
    return [
        "# PPTP ntc conversion v1 | tool=ntc_convert.py %s" % TOOL_VERSION,
        "# source=%s" % Path(samples.path).name,
        "# profile=%s (%s)" % (profile.name, profile.path),
        "# unit=celsius | the raw ADC columns are kept so the conversion can be "
        "re-checked",
        "# formula=1/T = 1/T0 + ln((R_pull*adc/(SAR-1-adc))/R0)/B with T0=25C",
        "# params=%s" % profile.describe(),
        "# channels=%s" % ",".join(CHANNELS),
        "# st=ok | oor=outside the profile's range | bad=no temperature could be "
        "computed | -=no reading in the source",
        "# summary=avg/min/max are over FRESH readings only, the same rule the "
        "perf verdict uses",
    ]


def write_out(path, samples, profile, rows):
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        for line in out_preamble(samples, profile):
            fh.write(line + "\r\n")
        w = csv.writer(fh)
        w.writerow(OUT_HEADER)
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _fmt(v):
    return "n/a" if v is None else "%.2f" % v


def print_summary(samples, profile, summary, out_path):
    print("profile : %s (%s)" % (profile.name, profile.path))
    print("          %s" % profile.describe())
    print("source  : %s" % samples.path)
    print("          %d data row(s), %d column(s)"
          % (len(samples.rows), len(samples.header)))

    if profile.ntc_count > len(CHANNELS):
        print("[warn] the profile declares %d NTC node(s); perf_monitor collects "
              "%d (%s). The rest are not in this file."
              % (profile.ntc_count, len(CHANNELS), ",".join(CHANNELS)))

    declared = samples.preamble_value("ntc_paths")
    if declared:
        # The samples.csv carries the sysfs path each column came from, so the
        # profile can be checked against the run instead of trusted. A mismatch
        # is a warning, not a failure: the constants are still the ones the
        # operator chose, and only they know whether the board changed.
        for part in declared.split(","):
            if ":" not in part:
                continue
            ch, path = part.split(":", 1)
            if ch in CHANNELS and profile.paths[CHANNELS.index(ch)] != path:
                print("[warn] profile channel %s says %s, this run read %s"
                      % (ch, profile.paths[CHANNELS.index(ch)], path))

    print("")
    print("%-4s %6s %6s %9s %9s %9s %5s %5s"
          % ("ch", "seen", "fresh", "avg C", "min C", "max C", "bad", "oor"))
    print("-" * 64)
    for ch in CHANNELS:
        s = summary[ch]
        print("%-4s %6d %6d %9s %9s %9s %5d %5d"
              % (ch, s["seen"], s["fresh"], _fmt(s["avg"]), _fmt(s["min"]),
                 _fmt(s["max"]), s["bad"], s["oor"]))
    print("-" * 64)
    print("wrote   : %s" % out_path)


# ---------------------------------------------------------------------------
# The chart
# ---------------------------------------------------------------------------
def chart_series(rows):
    """-> ({channel: (xs, ys)}, {channel: (fresh_xs, fresh_ys)}).

    Pure, so the selftest can assert the shape without a plotting library.

    `ys` carries NaN where the source had no reading; a NaN breaks the staircase
    rather than bridging it, which is what a missing reading should look like. A
    HELD tick ('h') is drawn - that is the hold, and drawing it is what makes the
    staircase the true shape - but it is not a measurement, so it gets no dot.
    A point is a true reading when the collector marked the tick fresh AND the
    conversion produced a temperature; a fresh tick whose count was out of range
    is a real sample of a broken reading, so it stays out of both.
    """
    C = {n: i for i, n in enumerate(OUT_HEADER)}
    lines, dots = {}, {}
    for ch in CHANNELS:
        xs, ys, fx, fy = [], [], [], []
        for r in rows:
            try:
                t = float(r[C["t_sec"]])
            except (TypeError, ValueError):
                continue
            cell = (r[C[OUT_C[ch]]] or "").strip()
            xs.append(t)
            ys.append(float(cell) if cell else float("nan"))
            if cell and r[C[SRC_ST]] == "f" and r[C[OUT_ST[ch]]] == ST_OK:
                fx.append(t)
                fy.append(float(cell))
        lines[ch] = (xs, ys)
        dots[ch] = (fx, fy)
    return lines, dots


def last_drawn(ys):
    """Index of the last point a channel's line reaches; None if it draws nothing.

    Not the same as the last dot: the final ticks of a run are usually held, so
    the line runs past the last measurement to the end of the run. `v == v` is the
    NaN test that needs no import - NaN is the one value unequal to itself.
    """
    out = None
    for i, v in enumerate(ys):
        if v == v:
            out = i
    return out


def _gaps(xs):
    """Gaps between consecutive readings of one channel, ascending; positive ones.

    A repeated timestamp (or a step backwards, from a clock that moved) is not a
    period and is dropped rather than allowed to drag the median down.
    """
    return sorted(b - a for a, b in zip(xs, xs[1:]) if b > a)


def _median(asc):
    """Median of an ASCENDING list; None when there is nothing to take it of."""
    n = len(asc)
    if not n:
        return None
    return asc[n // 2] if n % 2 else (asc[n // 2 - 1] + asc[n // 2]) / 2.0


def fresh_resolution(dots):
    """Median gap between consecutive true readings, in seconds; None if unknown.

    Median, not mean or minimum: a fresh tick that failed to read leaves a double
    gap, and that is the very thing a mean would spread over every other gap. The
    median reports the instrument's real period and ignores the hiccups.

    Pooled over both channels, because that is the figure the subtitle reports:
    the two nodes are read in a single command, so they share one period - and
    pooling means the number survives one channel dropping out mid-run.
    """
    gaps = []
    for ch in CHANNELS:
        gaps.extend(_gaps(dots[ch][0]))
    gaps.sort()
    return _median(gaps)


def axes_width_px():
    """The plot area's width in device pixels. Fixed layout, no renderer needed."""
    return (CHART_FIGSIZE[0] * CHART_DPI
            * (CHART_ADJUST["right"] - CHART_ADJUST["left"]))


def px_per_second(lines, drawn):
    """How much horizontal screen one second of the run gets, in device pixels.

    From the DATA and the layout constants, NOT from `ax.get_xlim()`: the axes has
    no data limits until something has been plotted, and this has to be answered
    before the dots are drawn. The final x range is the data's span widened by the
    x margins, which is exactly what ax.margins(x=...) will produce later.
    """
    xs = lines[drawn[0]][0] if drawn else []
    if not xs:
        return 0.0
    span = max(xs) - min(xs)
    if span <= 0:
        return 0.0
    return axes_width_px() / (span * (1.0 + 2.0 * CHART_MARGIN_X))


def dots_legible(dots, ch, px_per_s):
    """Can this channel's dots be told apart on screen?

    Pure, so the selftest can pin the rule without a plotting library. One reading
    (or none) has nothing to collide with, so it keeps its dot.
    """
    gap = _median(_gaps(dots[ch][0]))
    return gap is None or gap * px_per_s >= DOT_MIN_GAP_PX


def _mmss(sec):
    """Seconds -> `m:ss`. A 10-minute run is `10:00`, not a 3-digit second count."""
    sec = float(sec)
    m = int(sec // 60)
    s = int(round(sec - m * 60))
    if s == 60:
        m, s = m + 1, 0
    return "%d:%02d" % (m, s)


def _describe_run(samples, profile, dots, summary, dotless=()):
    """The subtitle: what the picture is OF, and how coarse it is.

    `dotless` names the channels whose dots were too dense to draw. Without it the
    picture would be silent about the marks a shorter run shows and this one does
    not, and the reader would be left inferring a sampling period from their
    absence.
    """
    bits = []
    res = fresh_resolution(dots)
    for ch in CHANNELS:
        if summary[ch]["n"]:
            bits.append("%s %d 个读数" % (CH_LABEL[ch], summary[ch]["n"]))
    bad = sum(summary[ch]["bad"] + summary[ch]["oor"] for ch in CHANNELS)
    body = " · ".join(bits) if bits else "无有效读数"
    if res:
        body += " · 每 %.1f 秒一个真实读数" % res
    else:
        body += " · 只有一个读数,看不出趋势"
    if bad:
        body += " · %d 个读数被判为无效" % bad
    if dotless:
        who = ("" if len(dotless) == len(CHANNELS)
               else "、".join(CH_LABEL[ch] for ch in dotless) + " 的")
        body += " · %s采样点密过屏幕像素,未画点" % who
    return body


def render_chart(path, samples, profile, rows, summary, theme="light"):
    """Write the PNG. Raises ChartUnavailable if it cannot be drawn at all."""
    try:
        import matplotlib
        matplotlib.use("Agg")           # no display, no GUI backend, no .matplotlibrc
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except ImportError as exc:
        raise ChartUnavailable("matplotlib is not importable (%s)" % exc)

    pal = CHART_THEMES[theme]
    lines, dots = chart_series(rows)
    drawn = [ch for ch in CHANNELS if summary[ch]["n"]]
    if not drawn:
        raise ChartUnavailable("no valid reading in this run - a chart of it "
                               "would be an empty box")

    # matplotlib's own DejaVu has no CJK glyphs: without a face that does, every
    # Chinese label renders as a row of boxes. The chain keeps this working on a
    # machine that has none of the Windows faces (it degrades to boxes, not to a
    # crash). rcParams is process-global - acceptable here, this is a CLI.
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                       "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=CHART_FIGSIZE, dpi=CHART_DPI)
    fig.patch.set_facecolor(pal["surface"])
    ax.set_facecolor(pal["surface"])

    for ch in drawn:
        xs, ys = lines[ch]
        ax.step(xs, ys, where="post", color=pal[ch], linewidth=2,
                solid_joinstyle="round", solid_capstyle="round", zorder=3,
                label=CH_LABEL[ch])
    # Decided before a single dot is drawn, and the line is already down: forcing
    # the dots on for a run this dense is what erased it.
    pps = px_per_second(lines, drawn)
    dotless = [ch for ch in drawn if not dots_legible(dots, ch, pps)]
    for ch in drawn:
        if ch in dotless:
            continue
        fx, fy = dots[ch]
        # r >= 4 with a ring in the surface colour, so a dot stays legible where
        # it lands on the line or on the other series. dots_legible() has already
        # established that there is room for the ring to do that job - where there
        # is not, the ring is a brush.
        ax.plot(fx, fy, "o", markersize=MARKER_PT, linestyle="none", zorder=4,
                markerfacecolor=pal[ch], markeredgecolor=pal["surface"],
                markeredgewidth=MARKER_EDGE_PT)

    # Direct-label the endpoint only - one number per series. A value on every
    # point is chaos, and the axis plus the CSV already carry the rest.
    #
    # The endpoint is the end of the LINE, not the last dot. Those differ whenever
    # the final ticks were held, and labelling the dot puts the number wherever the
    # last MEASUREMENT happened to land - which, in a run short enough to have one
    # reading, is the far left, on top of the y-axis tick labels. The line always
    # ends at the right edge, and the value it ends on is the last reading anyway.
    for ch in drawn:
        xs, ys = lines[ch]
        last = last_drawn(ys)
        if last is not None:
            ax.annotate("%.1f°C" % ys[last], (xs[last], ys[last]),
                        textcoords="offset points", xytext=(0, 11), ha="center",
                        color=pal["ink2"], fontsize=9, zorder=5)

    # Matplotlib's default 5% margin is measured against the data RANGE, so a run
    # whose two nodes sit far apart leaves the hotter one's endpoint dots almost
    # touching the title. This is visual only - no value is clipped either way.
    ax.margins(y=CHART_MARGIN_Y, x=CHART_MARGIN_X)
    ax.grid(axis="y", color=pal["grid"], linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(pal["axis"])
        ax.spines[side].set_linewidth(1)
    ax.tick_params(colors=pal["muted"], labelsize=9, length=3, width=1)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: "%.0f°C" % v))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: _mmss(v)))

    device = samples.preamble_value("device") or Path(samples.path).stem
    ax.set_title("NTC 节点温度 · %s" % device, loc="left", pad=24,
                 color=pal["ink"], fontsize=13)
    ax.text(0, 1.02, _describe_run(samples, profile, dots, summary, dotless),
            transform=ax.transAxes, ha="left", va="bottom",
            color=pal["ink2"], fontsize=9)
    ax.set_xlabel("运行时长", color=pal["ink2"], fontsize=10, labelpad=8)
    ax.set_ylabel("温度 (°C)", color=pal["ink2"], fontsize=10, labelpad=6)
    # Two series means a legend is mandatory; identity must never rest on colour
    # alone. Below the plot rather than inside it, so it cannot land on a line -
    # and low enough that it does not crowd the x-axis title above it.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncols=2,
              frameon=False, fontsize=9, labelcolor=pal["ink2"],
              handlelength=2.0, columnspacing=2.5)

    fig.subplots_adjust(**CHART_ADJUST)
    fig.savefig(path, facecolor=pal["surface"])
    plt.close(fig)


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
VENDOR_FIXTURE = '''
[NTC]
#NTC count
NTC COUNT = 2;
#NTC error-detect flag - kept in the profile, not consumed by this tool
NTC ERR DETECT = 1;
#divider resistor, k, and the ADC's full-scale count
NTC R = 4.7;
NTC R2 = 4.7:
NTC SAR VALUE MAX = 1024;
#B value
NTC B VALUE = 4010;
NTC B VALUE2 = 4010;
#R at 25C, k
NTC RESISTANCE = 10;
NTC RESISTANCE2 = 10:
NTC TEMP COMP = 20;
NTC1 PATH = "/sys/bus/iio/devices/iio:device0/in_voltage3_raw";
NTC2 PATH = "/sys/bus/iio/devices/iio:device0/in_voltage2_raw";
NTC CPU PATH = "/sys/class/thermal/thermal_zone0/temp";
NTC1 TEMP COMP = 0;
NTC2 TEMP COMP = 0;
NTC CPU TEMP COMP = 0;
NTC VALUE MAX = 100;
NTC VALUE MIN = -20;
NTC OBTAIN TEMP WAY = 1;
'''

# The real preamble, verbatim from an archived run - including the fact that
# ntc_unit and ntc_paths share one line, which is what broke the first version.
REAL_PREAMBLE = [
    "# PPTP perf samples v2 | script=perf_monitor.py | judge_version=v1",
    "# device=EE88885D8DEF08004CD1",
    "# config=interval_ms=2000,duration_sec=120,watch_pkg=(unset)",
    "# config_monitors=mem,gpu,fg,wifi,ntc",
    "# ntc_unit=raw_adc ntc_paths=lcd:/sys/bus/iio/devices/iio:device0/"
    "in_voltage3_raw,led:/sys/bus/iio/devices/iio:device0/in_voltage2_raw",
]
REAL_HEADER = ["t_sec", "cpu", "ntc_lcd", "ntc_led", "ntc_st"]

# Regression anchors: real readings off this board, each valued under BOTH rules.
#
# The last column is the vendor's - the two-segment rule its Java actually runs -
# and it is the one asserted. The middle column is the single-B value that stood
# here before 2026-09-21, kept so the size of the correction stays visible in
# `--selftest` output instead of buried in a git log. The two differ by ~0.05 C on
# the LCD node, not at all on the LED node (whose resistance never reaches the
# switch point), and by 0.16 C at the cold end.
#
# If a refactor moves one of these, the formula changed - which is the one thing
# that must never happen by accident, because the archived samples.csv files are
# permanent.
REFERENCES = (
    # adc, single B, two segments (what the vendor does)
    (664, 28.137578, 28.185747),
    (665, 28.040400, 28.087061),
    (666, 27.943159, 27.988313),
    (661, 28.428747, 28.481438),
    (371, 57.415137, 57.415137),
    (383, 56.047158, 56.047158),
    (800, 13.852098, 13.689187),
)


def _raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def _exc(fn):
    """The exception a call raises, or None. Lets an assertion read the message:
    a refusal that does not say what the options were is only half a refusal."""
    try:
        fn()
    except Exception as exc:                                  # noqa: BLE001
        return exc
    return None


def selftest():
    fails = []

    def check(name, ok, detail=""):
        if ok:
            print("[selftest] PASS  %s" % name)
        else:
            print("[selftest] FAIL  %s  (%s)" % (name, detail))
            fails.append(name)

    # The profile this tool actually SHIPS for this board. Everything below this
    # point could pass while the shipped file still converted the old way, which
    # is the failure that would matter.
    try:
        shipped, shipped_err = load_profile(SHIPPED_PROFILE_NAME), None
    except Exception as exc:                                  # noqa: BLE001
        shipped, shipped_err = None, exc
    check("profile: the shipped profile carries the vendor's second B "
          "and the resistance it switches at",
          shipped is not None and shipped.b_cold == [3950.0, 3950.0]
          and shipped.b_switch_r == 3588.0,
          str(shipped_err) if shipped is None else shipped.describe())
    check("profile: the shipped profile converts the LCD anchor the "
          "vendor's way",
          shipped is not None
          and abs(adc_to_celsius(664, shipped, 0) - 28.185747) < 0.0005,
          "n/a" if shipped is None
          else "%r" % adc_to_celsius(664, shipped, 0))

    # -- which profile, when --profile is absent -----------------------------
    # The guard against converting one project's board with another project's
    # constants (TODO.md 6.19). Its whole value is in the >1 case, so that is the
    # case these assertions are built around.
    with tempfile.TemporaryDirectory() as rtd:
        rdir = Path(rtd)
        check("profile: no profiles at all is an error, not a fallback to the "
              "shipped name",
              _raises(lambda: resolve_profile_name(None, str(rdir)), ProfileError))
        (rdir / "alpha.ini").write_text("[NTC]\n", encoding="utf-8")
        check("profile: exactly one profile is the one case where a guess is "
              "safe, and it is the one that gets used",
              resolve_profile_name(None, str(rdir)) == "alpha")
        (rdir / "beta.ini").write_text("[NTC]\n", encoding="utf-8")
        check("profile: two profiles REFUSES to guess - this is the guard, and "
              "it must not quietly pick the shipped name",
              _raises(lambda: resolve_profile_name(None, str(rdir)), ProfileError))
        check("profile: the refusal names the options, so the reader does not "
              "have to go and list them",
              "alpha" in str(_exc(lambda: resolve_profile_name(None, str(rdir))))
              and "beta" in str(_exc(lambda: resolve_profile_name(None, str(rdir)))))
        check("profile: an explicit --profile is never second-guessed",
              resolve_profile_name("beta", str(rdir)) == "beta")

    raw = parse_profile_text(VENDOR_FIXTURE)
    check("profile: the vendor's own file parses, semicolons and quotes and all",
          raw.get("NTC B VALUE") == "4010"
          and raw.get("NTC SAR VALUE MAX") == "1024"
          and raw.get("NTC1 PATH", "").startswith("/sys/bus/iio")
          and raw.get("NTC COUNT") == "2", str(sorted(raw))[:140])
    check("profile: a ':' separator parses like '=' (the vendor file mixes them)",
          parse_profile_text("NTC R2 : 4.7;").get("NTC R2") == "4.7")
    check("profile: key case and inner whitespace do not matter",
          parse_profile_text("ntc  b value = 3950").get("NTC B VALUE") == "3950")

    with tempfile.TemporaryDirectory() as td:
        pdir = Path(td)
        (pdir / "t.ini").write_text(VENDOR_FIXTURE, encoding="utf-8")
        prof = load_profile("t", pdir)
        check("profile: builds per channel from the bare/`2` key pair",
              prof.r_pull == [4700.0, 4700.0] and prof.r0 == [10000.0, 10000.0]
              and prof.b == [4010.0, 4010.0], str((prof.r_pull, prof.b)))
        check("profile: per-channel comp is read, not the bare NTC TEMP COMP",
              prof.comp == [0.0, 0.0], str(prof.comp))
        check("profile: a comp-less profile still loads, compensating by zero",
              Profile("x", "-", {k: v for k, v in raw.items()
                                 if "TEMP COMP" not in k}).comp == [0.0, 0.0])
        check("profile: PPTP-only keys default when the vendor file lacks them",
              prof.adc_invalid_min == 1000.0, str(prof.adc_invalid_min))
        check("profile: a missing required key is a loud error, not a zero",
              _raises(lambda: Profile("x", "-", {k: v for k, v in raw.items()
                                                 if k != "NTC B VALUE"}),
                      ProfileError))
        check("profile: the table path (OBTAIN TEMP WAY=0) is refused, not "
              "guessed at",
              _raises(lambda: Profile("x", "-",
                                      {**raw, "NTC OBTAIN TEMP WAY": "0"}),
                      ProfileError))
        check("profile: a non-numeric value is a loud error, not a zero",
              _raises(lambda: Profile("x", "-",
                                      {**raw, "NTC SAR VALUE MAX": "auto"}),
                      ProfileError))

        # -- the formula ------------------------------------------------------
        # The strongest check available is not a magic number, it is the point
        # where the NTC's resistance equals R0 by construction: there the
        # B-equation must return exactly the reference temperature, 25 C.
        # Solving R_pull*adc/(SAR-1-adc) = R0 gives adc = R0*(SAR-1)/(R0+R_pull).
        adc_25 = (prof.r0[0] * (prof.sar_full_scale - 1)
                  / (prof.r0[0] + prof.r_pull[0]))
        got25 = adc_to_celsius(adc_25, prof, 0)
        check("formula: the reading whose resistance IS R0 reads exactly 25.00 C",
              got25 is not None and abs(got25 - 25.0) < 1e-9,
              "adc=%r -> %r" % (adc_25, got25))

        # The vendor's file carries no cold-side B - because the vendor's Java
        # has no config to carry it in. The pair is added HERE rather than to
        # VENDOR_FIXTURE so that fixture keeps meaning "what the vendor actually
        # sent", and so a profile without the pair stays covered by every other
        # assertion in this function.
        seg_raw = {**raw, COLD_B_KEYS[0]: "3950", COLD_B_KEYS[1]: "3950",
                   PROFILE_KEY_B_SWITCH_R: "3588"}
        prof_seg = Profile("seg", "-", seg_raw)

        for adc, one_b, two_b in REFERENCES:
            got = adc_to_celsius(adc, prof_seg, 0)
            check("formula: adc=%d -> %.4f C (two segments, as the vendor does)"
                  % (adc, two_b),
                  got is not None and abs(got - two_b) < 0.0005, "got %r" % got)
            got1 = adc_to_celsius(adc, prof, 0)
            check("formula: adc=%d -> %.4f C with no segment pair in the profile"
                  % (adc, one_b),
                  got1 is not None and abs(got1 - one_b) < 0.0005,
                  "got %r" % got1)

        # -- the two-segment rule ---------------------------------------------
        # A switch that does nothing when both segments agree is a switch that
        # only ever chooses a B. That is the strongest statement available
        # without re-deriving the vendor's numbers: set the cold B equal to the
        # hot one and the segmented profile must reproduce the single-B profile
        # at EVERY count, on both sides of the switch point. Exact equality, not
        # a tolerance - the branch must select a value, not perturb one.
        agree = Profile("agree", "-", {**seg_raw, COLD_B_KEYS[0]: "4010",
                                       COLD_B_KEYS[1]: "4010"})
        check("segment: with both B equal, the segmented profile IS the "
              "single-B profile - the branch picks a B and does nothing else",
              all(adc_to_celsius(a, agree, 0) == adc_to_celsius(a, prof, 0)
                  for a in (1, 100, 442, 443, 600, 663, 800, 999)),
              str([(a, adc_to_celsius(a, agree, 0), adc_to_celsius(a, prof, 0))
                   for a in (442, 443)]))

        # Where it flips. An absurd cold B makes the branch unmistakable, so the
        # boundary is pinned to a COUNT rather than to "somewhere near 50 C":
        # R(442) = 3575.6 ohm stays on the hot B, R(443) = 3589.8 ohm switches.
        # That is 3588 ohm crossed, which is the whole rule.
        loud = Profile("loud", "-", {**seg_raw, COLD_B_KEYS[0]: "1000",
                                     COLD_B_KEYS[1]: "1000"})
        check("segment: the switch fires on the resistance crossing 3588 ohm, "
              "between counts 442 and 443",
              adc_to_celsius(442, loud, 0) == adc_to_celsius(442, prof, 0)
              and abs(adc_to_celsius(443, loud, 0)
                      - adc_to_celsius(443, prof, 0)) > 0.5,
              "%r vs %r" % (adc_to_celsius(442, loud, 0),
                            adc_to_celsius(443, loud, 0)))

        # Strictly greater, not greater-or-equal. Putting the switch point at
        # exactly the resistance adc=500 computes makes the test `r > r`, which
        # is False - so 500 keeps the hot B and 501, one count higher and
        # therefore more resistive, does not.
        r500 = prof.r_pull[0] * 500 / (prof.sar_full_scale - 1.0 - 500)
        at500 = Profile("b", "-", {**seg_raw, COLD_B_KEYS[0]: "1000",
                                   COLD_B_KEYS[1]: "1000",
                                   PROFILE_KEY_B_SWITCH_R: "%.17g" % r500})
        check("segment: the comparison is strict - a resistance equal to the "
              "switch point stays on the hot-side B",
              adc_to_celsius(500, at500, 0) == adc_to_celsius(500, prof, 0)
              and adc_to_celsius(501, at500, 0) != adc_to_celsius(501, prof, 0),
              "r500=%r" % r500)

        # Per channel, like every other constant in the file: overriding the LED
        # entry must leave the LCD conversion untouched.
        ch = Profile("ch", "-", {**seg_raw, COLD_B_KEYS[1]: "1000"})
        check("segment: the cold B is per channel - overriding the LED entry "
              "leaves the LCD conversion alone and changes the LED one",
              adc_to_celsius(664, ch, 0) == adc_to_celsius(664, prof_seg, 0)
              and adc_to_celsius(664, ch, 1) != adc_to_celsius(664, prof_seg, 1))

        # Half a pair is a mistake, not a default.
        check("segment: a cold B with no resistance to switch at is refused",
              _raises(lambda: Profile(
                  "x", "-", {k: v for k, v in seg_raw.items()
                             if k != PROFILE_KEY_B_SWITCH_R}), ProfileError))
        check("segment: a cold B for one channel only is refused",
              _raises(lambda: Profile(
                  "x", "-", {k: v for k, v in seg_raw.items()
                             if k != COLD_B_KEYS[1]}), ProfileError))
        check("segment: the params line says which rule ran, including when "
              "there is only one segment",
              "b_cold=none" in prof.describe()
              and "b_cold=3950,3950" in prof_seg.describe()
              and "b_switch_r=3588" in prof_seg.describe(),
              prof_seg.describe())

        # The sign of this is worth stating outright, because getting it backwards
        # is silent: a larger count is a COLDER node. The NTC sits on the low side
        # of the divider, so a rising count means rising NTC resistance, which for
        # an NTC means falling temperature. It is also what the two channels on
        # this board show - the LED node reads ~371 and is the hot one at ~57 C,
        # the LCD node reads ~664 and sits at ~28 C.
        check("formula: a larger count is a COLDER node",
              adc_to_celsius(600, prof, 0) > adc_to_celsius(700, prof, 0),
              "%r vs %r" % (adc_to_celsius(600, prof, 0),
                            adc_to_celsius(700, prof, 0)))
        check("formula: the comp is added, not folded into the ratio",
              abs(adc_to_celsius(664, Profile("x", "-",
                                              {**raw, "NTC1 TEMP COMP": "10"}),
                                 0)
                  - adc_to_celsius(664, prof, 0) - 10.0) < 1e-9)

        # -- the readings that must NOT become numbers -------------------------
        check("formula: the vendor error threshold and above -> no temperature",
              all(adc_to_celsius(a, prof, 0) is None
                  for a in (1000, 1001, 1023, 4095)))
        check("formula: a zero or negative count -> no temperature",
              all(adc_to_celsius(a, prof, 0) is None for a in (0, -1, -100)))
        check("formula: no reading at all -> no temperature",
              adc_to_celsius(None, prof, 0) is None)
        check("formula: a count past the declared range is flagged, not clamped",
              classify_c(adc_to_celsius(999, prof, 0), prof) == ST_OOR
              and classify_c(adc_to_celsius(664, prof, 0), prof) == ST_OK,
              "%r" % adc_to_celsius(999, prof, 0))

        # -- reading the real preamble shape ----------------------------------
        src = pdir / "perf_X.samples.csv"
        src.write_text("\r\n".join(REAL_PREAMBLE + [",".join(REAL_HEADER),
                                                    "0.0,10,664,371,f",
                                                    "2.0,11,664,371,h",
                                                    "4.0,12,665,372,f",
                                                    "6.0,13,,,n",
                                                    "8.0,14,1000,373,f",
                                                    "10.0,15,666,374,f"])
                       + "\r\n", encoding="utf-8")
        samp = read_samples(src)
        check("samples: the '#' preamble is kept, the header is found",
              len(samp.preamble) == 5 and samp.header == REAL_HEADER
              and len(samp.rows) == 6, str(samp.header))
        check("samples: a key packed onto a shared preamble line is still read",
              samp.preamble_value("ntc_unit") == "raw_adc",
              str(samp.preamble_value("ntc_unit")))
        check("samples: the per-column source path comes off the same line",
              (samp.preamble_value("ntc_paths") or "").startswith(
                  "lcd:/sys/bus/iio"), str(samp.preamble_value("ntc_paths")))
        check("samples: the unit check passes a raw file",
              not _raises(lambda: check_unit(samp), ProfileError))

        already = pdir / "already.csv"
        already.write_text("# ntc_unit=celsius\r\n"
                           "t_sec,ntc_lcd,ntc_led,ntc_st\r\n0,28.1,56.0,f\r\n",
                           encoding="utf-8")
        check("samples: an already-converted file is refused, not converted again",
              _raises(lambda: check_unit(read_samples(already)), ProfileError))
        silent = pdir / "silent.csv"
        silent.write_text("t_sec,ntc_lcd,ntc_led,ntc_st\r\n0,664,371,f\r\n",
                          encoding="utf-8")
        check("samples: a file that does not declare its unit is refused too - "
              "counts and degrees look identical",
              _raises(lambda: check_unit(read_samples(silent)), ProfileError))

        rows, summary = convert_samples(samp, prof)
        C = {n: i for i, n in enumerate(OUT_HEADER)}
        check("convert: one output row per input row, columns as declared",
              len(rows) == 6 and all(len(r) == len(OUT_HEADER) for r in rows),
              str([len(r) for r in rows]))
        check("convert: the raw count is kept next to the converted one",
              rows[0][C["ntc_lcd_raw"]] == "664"
              and rows[0][C["ntc_lcd_c"]] == "28.14", str(rows[0]))
        check("convert: the fresh reading converts and the held one repeats it",
              rows[0][C["ntc_lcd_c"]] == "28.14"
              and rows[1][C["ntc_lcd_c"]] == "28.14"
              and rows[0][C["ntc_st"]] == "f" and rows[1][C["ntc_st"]] == "h",
              "%s | %s" % (rows[0], rows[1]))
        check("convert: the two channels are converted against their own line",
              rows[0][C["ntc_lcd_c"]] == "28.14"
              and rows[0][C["ntc_led_c"]] == "57.42", str(rows[0]))
        check("convert: a row with no reading stays blank and says so",
              rows[3][C["ntc_lcd_raw"]] == "" and rows[3][C["ntc_lcd_c"]] == ""
              and rows[3][C["ntc_lcd_st"]] == ST_NONE, str(rows[3]))
        check("convert: an invalid count is flagged `bad` on THAT channel only",
              rows[4][C["ntc_lcd_st"]] == ST_BAD
              and rows[4][C["ntc_led_st"]] == ST_OK,
              "%s / %s" % (rows[4][C["ntc_lcd_st"]], rows[4][C["ntc_led_st"]]))
        check("convert: avg/min/max are over fresh readings, not over rows",
              summary["lcd"]["n"] == 3 and summary["lcd"]["seen"] == 5
              and summary["led"]["n"] == 4,
              "lcd %s | led %s" % (summary["lcd"], summary["led"]))
        check("convert: an invalid reading is excluded from the average, not "
              "folded in as a number",
              summary["lcd"]["bad"] == 1 and summary["lcd"]["n"] == 3,
              str(summary["lcd"]))
        # Anchored to the regression table, not to a number copied out of this
        # tool's own output - the three fresh lcd counts are 664, 665 and 666.
        check("convert: the lcd average is what the retained counts imply",
              abs(summary["lcd"]["avg"]
                  - (28.137578 + 28.040400 + 27.943159) / 3.0) < 1e-4,
              str(summary["lcd"]["avg"]))

        dst = pdir / "out.csv"
        write_out(dst, samp, prof, rows)
        back = list(csv.reader(io.open(dst, encoding="utf-8", errors="replace")))
        body = [r for r in back if r and not r[0].startswith("#")]
        check("write: preamble, header, and one line per row - all re-readable",
              body[0] == OUT_HEADER and len(body) == 7, str(len(body)))
        check("write: the preamble names the source, the profile, the unit and "
              "the formula",
              all(any(l and l[0].startswith("# " + k) for l in back)
                  for k in ("source=", "profile=t", "unit=celsius", "formula=",
                            "params=")))
        check("write: the output does NOT claim to be raw ADC, so nobody feeds "
              "28 C back in as a count",
              read_samples(dst).preamble_value("ntc_unit") is None)

        # A file with no NTC columns must fail loudly, not silently emit zeros -
        # and it must name the RIGHT reason. The archived runs that predate the
        # NTC channel (28/30 columns, metrics=cpu) carry neither the columns nor
        # the unit line, so checking the unit first would blame a missing
        # `ntc_unit=` for a file whose actual problem is that it has no NTC
        # channel at all. This assertion goes through main() because that is
        # where the ordering lives.
        old = pdir / "old.csv"
        old.write_text("t_sec,cpu\r\n0,1\r\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(old), "--profile", "t", "--profile-dir", str(pdir)])
        msg = buf.getvalue()
        check("samples: a pre-NTC file is refused, and the reason named is the "
              "MISSING COLUMN, not the missing unit declaration",
              rc == 2 and "ntc_lcd" in msg and "ntc_unit" not in msg,
              "rc=%r %s" % (rc, msg.strip()[:100]))

        # -- the chart -------------------------------------------------------
        # The shape assertions need no plotting library: chart_series() is pure,
        # and it is where the honesty rules actually live.
        lines, dots = chart_series(rows)
        check("chart: one x per data row, on both channels",
              len(lines["lcd"][0]) == 6 and len(lines["led"][0]) == 6,
              str([len(lines[ch][0]) for ch in CHANNELS]))
        check("chart: a row with no reading is a BREAK in the line, not a bridge "
              "between its neighbours",
              lines["lcd"][1][3] != lines["lcd"][1][3],
              repr(lines["lcd"][1]))
        check("chart: a held tick is drawn but is not a measurement - only the "
              "fresh, valid readings get dots",
              len(dots["lcd"][0]) == 3 and len(dots["led"][0]) == 4,
              "lcd %d led %d" % (len(dots["lcd"][0]), len(dots["led"][0])))
        check("chart: a fresh tick whose count was invalid gets no dot either",
              [round(x, 1) for x in dots["lcd"][0]] == [0.0, 4.0, 10.0],
              str(dots["lcd"][0]))
        check("chart: the resolution is the MEDIAN gap between true readings",
              fresh_resolution(dots) == 4.0, repr(fresh_resolution(dots)))
        check("chart: one reading means no resolution to report, not a guess",
              fresh_resolution({"lcd": ([0.0], [28.1]),
                                "led": ([], [])}) is None)
        check("chart: the endpoint label tracks the end of the LINE, which is "
              "past the last dot whenever the final ticks were held",
              last_drawn([28.1, 28.1, float("nan"), 28.1, 28.1]) == 4
              and last_drawn([float("nan"), 28.1]) == 1
              and last_drawn([float("nan")]) is None,
              str(last_drawn([float("nan")])))
        check("chart: the time axis reads as m:ss",
              (_mmss(0) == "0:00" and _mmss(30) == "0:30" and _mmss(600) == "10:00"
               and _mmss(3599) == "59:59"), _mmss(3599))
        check("chart: both themes carry the validated palette, both modes",
              CHART_THEMES["light"]["lcd"] == "#2a78d6"
              and CHART_THEMES["light"]["led"] == "#eb6834"
              and CHART_THEMES["dark"]["lcd"] == "#3987e5"
              and CHART_THEMES["dark"]["led"] == "#d95926",
              "changing these means re-running the palette validator")

        # -- the dot rule, which is a claim about the SCREEN -----------------
        # A dot says "a reading happened here", and it can only say that while the
        # eye can tell it from its neighbour. Both shapes below are real: the long
        # one is the archived run the rule was written for (52094 s, 1737
        # readings, i.e. 0.81 px per dot - the chart came back with the curve
        # gone), the short one is the 6-row fixture above.
        def long_rows(n=1737, period=30.0):
            """A reading every `period` seconds for `n` readings."""
            out = []
            for i in range(n):
                out.append(["%.1f" % (i * period), "699", "419",
                            "%.2f" % (24.5 + (i % 7) * 0.4),
                            "%.2f" % (52.0 + (i % 5) * 0.6),
                            "f", ST_OK, ST_OK])
            return out

        def long_summary(rs):
            """What convert_samples() would report for `rs`: every tick a reading."""
            n = len(rs)
            return {ch: {"seen": n, "fresh": n, "n": n, "avg": 25.0, "min": 24.0,
                         "max": 26.0, "bad": 0, "oor": 0} for ch in CHANNELS}

        long_lines, long_dots = chart_series(long_rows())
        long_pps = px_per_second(long_lines, ["lcd"])
        check("chart: a 14.5 h run gives each dot well under a pixel, ~26x under "
              "what a dot plus its ring needs - so they are not drawn",
              dots_legible(long_dots, "lcd", long_pps) is False,
              "%.2f px vs %.1f px min" % (30.0 * long_pps, DOT_MIN_GAP_PX))
        check("chart: the fixture's 2-minute run gives the same 30 s period "
              "hundreds of pixels, so its dots are drawn",
              dots_legible(dots, "lcd", px_per_second(lines, ["lcd"])) is True,
              "%.0f px" % (5.0 * px_per_second(lines, ["lcd"])))
        check("chart: the rule flips exactly at the threshold - one mark plus its "
              "ring is drawn, and a hundredth of a pixel less is not",
              dots_legible({"lcd": ([0.0, DOT_MIN_GAP_PX], [1.0, 2.0]),
                            "led": ([], [])}, "lcd", 1.0) is True
              and dots_legible({"lcd": ([0.0, DOT_MIN_GAP_PX - 0.01], [1.0, 2.0]),
                                "led": ([], [])}, "lcd", 1.0) is False,
              "%.2f px" % DOT_MIN_GAP_PX)
        check("chart: a single reading has nothing to collide with, so it keeps "
              "its dot however long the run around it was",
              dots_legible({"lcd": ([52094.0], [25.0]), "led": ([], [])},
                           "lcd", long_pps) is True)
        check("chart: a repeated timestamp is not a period - it is dropped rather "
              "than allowed to drag the median down",
              _gaps([0.0, 30.0, 30.0, 60.0]) == [30.0, 30.0]
              and _median([]) is None and _median([1.0, 3.0]) == 2.0,
              str(_gaps([0.0, 30.0, 30.0, 60.0])))
        check("chart: the subtitle names the dropped dots, so their absence is "
              "never something the reader has to infer",
              "未画点" in _describe_run(samp, prof, long_dots,
                                       long_summary(long_rows()), ["lcd", "led"])
              and "未画点" not in _describe_run(samp, prof, dots, summary),
              _describe_run(samp, prof, long_dots, long_summary(long_rows()),
                            ["lcd", "led"]))

        # matplotlib is optional by design - the CSV is the product, the PNG is
        # the second half of it - so a missing matplotlib is SKIPPED, never
        # reported as a pass. A skipped chart assertion must not read as "the
        # chart was verified".
        png = pdir / "chart.png"
        try:
            render_chart(png, samp, prof, rows, summary, theme="light")
        except ChartUnavailable as exc:
            print("[selftest] SKIP  chart: a PNG is written (%s)" % exc)
        else:
            blob = png.read_bytes()
            check("chart: a real PNG lands on disk, not an empty canvas",
                  blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) > 4096,
                  "%d bytes" % len(blob))
            png2 = pdir / "chart_dark.png"
            render_chart(png2, samp, prof, rows, summary, theme="dark")
            check("chart: the dark theme renders too (it is its own palette, not "
                  "an inverted light one)",
                  png2.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
                  and png2.read_bytes() != blob)

            # -- the same rule, read back off the PNG ------------------------
            # Optional dep on top of an optional dep: no image reader, no
            # assertion - SKIP, never a pass.
            try:
                from matplotlib import image as mpl_image
            except ImportError as exc:
                print("[selftest] SKIP  chart: the dot rule on the PNG (%s)" % exc)
            else:
                def series_px(p, ch, theme="light"):
                    """Series-coloured pixels INSIDE the axes.

                    The legend swatches carry the same colour, so the region is
                    the point: anything outside the plot area is the legend, the
                    title or the labels, and none of those are the curve.
                    """
                    arr = mpl_image.imread(p)
                    h, w = arr.shape[0], arr.shape[1]
                    sub = arr[int((1.0 - CHART_ADJUST["top"]) * h)
                              :int((1.0 - CHART_ADJUST["bottom"]) * h),
                              int(CHART_ADJUST["left"] * w)
                              :int(CHART_ADJUST["right"] * w), :3]
                    rgb = CHART_THEMES[theme][ch]
                    tgt = [int(rgb[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
                    return int(((abs(sub[:, :, 0] - tgt[0]) < 0.12)
                                & (abs(sub[:, :, 1] - tgt[1]) < 0.12)
                                & (abs(sub[:, :, 2] - tgt[2]) < 0.12)).sum())

                longs = long_rows()
                png3 = pdir / "chart_long.png"
                render_chart(png3, samp, prof, longs, long_summary(longs),
                             theme="light")
                kept = series_px(png3, "led")
                check("chart: a 14.5 h run keeps its CURVE - the staircase survives "
                      "the dot rule", kept > 4000, "%d led px inside the axes" % kept)

                # The bug, reproduced on purpose: forcing those dots back on is
                # exactly what the old code did, and it is what erased the line.
                # Without this, "never draw a dot anywhere" would pass every
                # assertion above it.
                real_legible = globals()["dots_legible"]
                globals()["dots_legible"] = lambda *_a, **_k: True
                try:
                    png4 = pdir / "chart_long_forced.png"
                    render_chart(png4, samp, prof, longs, long_summary(longs),
                                 theme="light")
                finally:
                    globals()["dots_legible"] = real_legible
                erased = series_px(png4, "led")
                check("chart: forcing the dots back on erases that curve - the exact "
                      "damage the rule prevents, at 1737 dots over 0.81 px each",
                      erased < kept / 2.0,
                      "%d px with the dots forced on, %d without" % (erased, kept))

                # ...and the other direction, so the rule cannot degenerate into
                # "never draw dots" unnoticed: a short run must keep them.
                globals()["dots_legible"] = lambda *_a, **_k: False
                try:
                    png5 = pdir / "chart_short_dotless.png"
                    render_chart(png5, samp, prof, rows, summary, theme="light")
                finally:
                    globals()["dots_legible"] = real_legible
                check("chart: a short run really is drawn WITH its dots - dropping "
                      "them changes the picture",
                      series_px(png, "lcd") > series_px(png5, "lcd"),
                      "%d px vs %d px without them"
                      % (series_px(png, "lcd"), series_px(png5, "lcd")))

                # The threshold is derived from MARKER_PT, so MARKER_PT has to BE
                # the size the marker is drawn at - otherwise the rule is a number
                # rather than a statement about the picture. One reading: the line
                # is then a single horizontal run, so the series colour's vertical
                # extent inside the axes IS the mark.
                def marker_px(p, ch, theme="light"):
                    arr = mpl_image.imread(p)
                    h, w = arr.shape[0], arr.shape[1]
                    sub = arr[int((1.0 - CHART_ADJUST["top"]) * h)
                              :int((1.0 - CHART_ADJUST["bottom"]) * h),
                              int(CHART_ADJUST["left"] * w)
                              :int(CHART_ADJUST["right"] * w), :3]
                    rgb = CHART_THEMES[theme][ch]
                    tgt = [int(rgb[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
                    mask = ((abs(sub[:, :, 0] - tgt[0]) < 0.12)
                            & (abs(sub[:, :, 1] - tgt[1]) < 0.12)
                            & (abs(sub[:, :, 2] - tgt[2]) < 0.12))
                    rows_any = mask.any(axis=1)
                    idx = [i for i, v in enumerate(rows_any) if v]
                    return (idx[-1] - idx[0] + 1) if idx else 0

                one = [["0.0", "699", "419", "25.00", "52.00", "f", ST_OK, ST_OK]]
                png6 = pdir / "chart_one.png"
                render_chart(png6, samp, prof, one,
                             {ch: {"seen": 1, "fresh": 1, "n": 1, "avg": 25.0,
                                   "min": 25.0, "max": 25.0, "bad": 0, "oor": 0}
                              for ch in CHANNELS}, theme="light")
                drawn = marker_px(png6, "lcd")
                # The stroke straddles the marker boundary, so half a ringwidth is
                # painted INWARD (the visible face is MARKER_PT - MARKER_EDGE_PT)
                # and half OUTWARD (which is what DOT_MIN_GAP_PX is: the space the
                # mark occupies, and therefore what a neighbour may not overlap).
                # Both come off the same two constants, so measuring the face pins
                # them both. Antialiasing puts a pixel either side of this, hence
                # the slack.
                want = (MARKER_PT - MARKER_EDGE_PT) * CHART_DPI / 72.0
                check("chart: the mark the threshold is derived from is the mark that "
                      "gets drawn", abs(drawn - want) < 3.0,
                      "drawn %.0f px, (MARKER_PT-MARKER_EDGE_PT) = %.1f pt = %.1f px"
                      % (drawn, MARKER_PT - MARKER_EDGE_PT, want))

    print("")
    if fails:
        print("[selftest] %d failure(s)" % len(fails))
        return 1
    print("[selftest] 0 failure(s)")
    return 0


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert the NTC raw ADC columns of a perf_monitor "
                    "samples.csv into Celsius.")
    ap.add_argument("samples", nargs="?",
                    help="the .samples.csv to convert (never modified)")
    ap.add_argument("--profile", default=None,
                    help="profile name under tools/ntc_profiles/ (default: the "
                         "one profile there, if there is EXACTLY one - with "
                         "more than one this is required, because guessing "
                         "would silently use another project's constants)")
    ap.add_argument("--no-chart", action="store_true",
                    help="write the CSV only; do not render the PNG")
    ap.add_argument("--theme", choices=sorted(CHART_THEMES), default="light",
                    help="chart palette (default: light - it is the one that "
                         "survives being pasted into a document)")
    ap.add_argument("--profile-dir", default=None,
                    help="override the profile directory")
    ap.add_argument("--out", default=None,
                    help="output path (default: <input>.temps.csv)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite the output if it already exists")
    ap.add_argument("--list-profiles", action="store_true",
                    help="list the profiles available and exit")
    ap.add_argument("--selftest", action="store_true",
                    help="run the built-in checks and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.list_profiles:
        names = list_profiles(args.profile_dir)
        print("profiles in %s:" % (args.profile_dir or PROFILE_DIR))
        for n in names:
            print("  %s" % n)
        return 0 if names else 1
    if not args.samples:
        ap.error("a samples.csv is required (or --selftest / --list-profiles)")

    src = Path(args.samples)
    if not src.exists():
        print("[error] no such file: %s" % src)
        return 2
    if args.out:
        dst = Path(args.out)
    else:
        name = src.name
        dst = src.with_name(name[:-4] + ".temps.csv"
                            if name.lower().endswith(".csv")
                            else name + ".temps.csv")
    if dst.exists() and not args.force:
        print("[error] %s exists - pass --force to overwrite" % dst)
        return 2

    try:
        # `args.profile` is None unless the user named one; resolve_profile_name
        # is the ONE place that decides what that means, and it refuses when the
        # answer cannot be decided safely.
        pname = resolve_profile_name(args.profile, args.profile_dir)
        profile = load_profile(pname, args.profile_dir)
        samples = read_samples(src)
        # The COLUMN check runs before the unit check, deliberately: a run that
        # predates the NTC channel has no ntc_lcd column AND no ntc_unit line,
        # and "this file has no ntc_lcd column" is the true diagnosis for it.
        # Checking the unit first makes that file report a missing unit
        # declaration, which sends the reader looking for the wrong thing. Both
        # checks are pure, so the order only decides which reason is printed.
        rows, summary = convert_samples(samples, profile)
        check_unit(samples)
    except ProfileError as exc:
        print("[error] %s" % exc)
        return 2

    write_out(dst, samples, profile, rows)
    print_summary(samples, profile, summary, dst)
    if not args.no_chart:
        # The PNG rides with the CSV, so the same --force that lets the CSV be
        # overwritten lets the chart be redrawn: they are one product, and a stale
        # PNG beside a fresh CSV is worse than no PNG.
        png = dst.with_suffix(".png")
        try:
            render_chart(png, samples, profile, rows, summary, theme=args.theme)
            print("chart   : %s (%s)" % (png, args.theme))
        except ChartUnavailable as exc:
            # Not fatal, and deliberately not silent: "the CSV is fine, the picture
            # is missing" is exactly the state that has someone hunting for a file
            # that was never going to exist.
            print("[warn] no chart written: %s" % exc)
            print("       the CSV above is complete. For the PNG: "
                  "%s -m pip install matplotlib" % sys.executable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
