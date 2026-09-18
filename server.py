"""
PPTP backend - single file FastAPI server.

Endpoints:
  GET  /healthz                  -> {"ok": true}
  GET  /api/devices              -> list of adb devices
  GET  /api/scripts              -> list of *.py in scripts/
  POST /api/run                  -> {device, script, params} -> {task_id}
  POST /api/stop/{task_id}       -> terminate running task
  GET  /api/tasks                -> list of all tasks (no proc handle)
  GET  /api/tasks/{task_id}      -> single task detail
  WS   /ws/logs/{task_id}        -> live stdout/stderr stream

Run: uvicorn server:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pydantic import BaseModel, field_validator


class NoCacheMiddleware(BaseHTTPMiddleware):
    """Disable HTTP caching for static assets in dev mode.

    Prevents the browser from serving stale app.js / style.css after we
    edit them. Production builds should set proper long-lived cache headers
    via CDN, but for our local dev platform the safest default is no cache.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/") or path == "/":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
STATIC_DIR = ROOT / "static"
LOGS_DIR = ROOT / "logs"
ARCHIVE_DIR = ROOT / "archive"
REPORTS_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"
TASKS_FILE = DATA_DIR / "tasks.json"
IR_SEQUENCES_DIR = ROOT / "ir_sequences"

# REPORTS_DIR is created by the scripts, not by us (they makedirs it lazily),
# so it is deliberately NOT in the mkdir loop below - we only ever read it.
for d in (SCRIPTS_DIR, STATIC_DIR, LOGS_DIR, ARCHIVE_DIR, DATA_DIR, IR_SEQUENCES_DIR):
    d.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Per-task log capture (v2.6.0) + per-task archive (v2.7.0)
# ---------------------------------------------------------------------------
# Every task always streams its own stdout. In addition the server attaches two
# device-side capture channels:
#   logcat - always on, one `adb logcat` child per task
#   serial - opt-in per run, one COM port reader thread per task
# All three channels share one wire format (WS frames tagged with "source") and
# one on-disk naming scheme, so adding a 4th channel later touches neither the
# WS protocol nor the archive layout.
#
# The frontend renders ONLY stdout - permanently. logcat/serial are captured,
# archived and never displayed (see the WS_SUBS filter below, which keeps them
# off the wire entirely unless a client explicitly subscribes).
#
# logs/ is a transient work area: when a task reaches a terminal state its
# files are MOVED into archive/<name>/ and stay there forever.
LOG_SUFFIX = {"stdout": ".log", "logcat": ".logcat.log", "serial": ".serial.log"}
CAPTURE_SOURCES = ("stdout", "logcat", "serial")
CAPTURES_COUNTED = ("logcat", "serial")   # device-side channels (not stdout)

# Archive member names. Deliberately short and stable - these are what a human
# sees when they open the folder, and what /api/tasks/{id}/log?source= resolves
# to once a task is archived.
ARCHIVE_MEMBER = {"stdout": "stdout.log", "logcat": "logcat.log", "serial": "serial.log"}
ARCHIVE_REPORT_NAME = "report.json"
ARCHIVE_SUMMARY_NAME = "summary.json"

# Archives are grouped by module, mirroring reports/stress-test/<module>/ so the
# two trees are browsable the same way. This has to be an explicit table, not a
# guess from the filename: "wifi_onoff" / "wifi_reboot" / "wifi_switch" are one
# module, "bt_reboot" is not, and scripts may be added at any time.
# Unknown scripts land in ARCHIVE_MODULE_FALLBACK rather than at the archive root,
# so the root only ever contains module folders.
ARCHIVE_MODULES = {
    "ir_runner.py": "ir",
    "wifi_onoff_stress.py": "wifi",
    "wifi_reboot_stress.py": "wifi",
    "wifi_switch_stress.py": "wifi",
    "sensor_reboot_stress.py": "sensor",
    "app_launch_stress.py": "app-launch",
    "perf_monitor.py": "perf",
    "battery_inout_stress.py": "battery",
    "bt_reboot_stress.py": "bt",
}
ARCHIVE_MODULE_FALLBACK = "other"
# Artifacts the BROWSER renders and posts back (server has no chart renderer).
# Whitelisted because the name becomes a filename on disk.
ARCHIVE_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
ARCHIVE_UPLOADS = ("chart.png", "perf.csv")

# Scripts that write their own JSON report under reports/stress-test/. Two jobs,
# and the second one is easy to miss:
#   1. it words the "no report" note in summary.json ("this script writes no
#      report" vs "the report is missing");
#   2. it is the ADMISSION GATE for the mtime fallback scan in
#      _find_task_reports() - a hard kill can skip the script's own
#      `report :` line, and the fallback only looks for scripts listed here.
# So a report-writing script missing from this set loses its report, silently,
# in exactly the case the fallback exists for. Keep it in step with scripts/.
REPORT_WRITING_SCRIPTS = frozenset({
    "app_launch_stress.py",
    "battery_inout_stress.py",
    "bt_reboot_stress.py",
    "perf_monitor.py",
    "sensor_reboot_stress.py",
    "wifi_reboot_stress.py",
    "wifi_onoff_stress.py",
    "wifi_switch_stress.py",
})

# Report files already copied into an archive. One report belongs to exactly one
# run - without this, two tasks on the same device started close together can
# both match the same file by mtime and the later one gets a copy of the
# earlier one's data. Process-lifetime only; reports/ is tiny.
_CLAIMED_REPORTS: set[str] = set()

# logcat: `-T 1` prints the most recent line then keeps following (unlike
# `-t N`, which implies -d and exits immediately). Buffers exclude "events"
# (noisy, not useful here) and "kernel" (userdebug/eng builds only).
# LOGCAT_FILTER is the volume lever: *:V can be GB/24h on a chatty device,
# *:I cuts it roughly an order of magnitude.
LOGCAT_ENABLED = True                 # set False to run without device log capture
LOGCAT_BUFFERS = ("main", "system", "crash")
LOGCAT_FILTER = "*:I"
# CONSECUTIVE restart budget (reset as soon as logcat delivers a line again).
# 5 was far too small: while a device is rebooting, adb may exit instead of
# blocking, burning ~15 restarts in a 30s outage - so the old value killed
# logcat permanently at reboot #6 of a default 100-iteration run. This bounds
# "device gone for good" at ~2 minutes, which is the actual intent.
LOGCAT_MAX_RESTARTS = 60
LOGCAT_RESTART_DELAY_SEC = 2.0
# Lines re-read from the device ring buffer after a restart. A reboot wipes the
# buffer, so this recovers the boot log that `-T 1` would skip; the cost is
# duplicated lines when the restart was an adb hiccup rather than a reboot.
LOGCAT_RESTART_TAIL_LINES = 2000
LOGCAT_READ_BYTES = 65536
# Default raised 256MB -> 1GB in v2.7.0. Measured on a real wifi_reboot_stress
# run: one reboot (shutdown + boot storm) produces ~1.3MB of logcat, against
# only ~6KB for the quiet period before it. All three reboot scripts
# (wifi_reboot / bt_reboot / sensor_reboot) default to 100 iterations, so a
# single DEFAULT run is ~128MB - half the old cap - and the cap would have been
# hit at roughly iteration 197, silently truncating the rest.
LOGCAT_MAX_BYTES = int(os.environ.get("PPTP_LOGCAT_MAX_MB", "1024")) * 1024 * 1024
LOGCAT_CAP_CHECK_LINES = 256           # check the size cap every N lines, not per line

WS_SEND_TIMEOUT_SEC = 5.0
REPLAY_TAIL_BYTES = 4 * 1024 * 1024

# Device watchdog (v2.7.2): how often to look for devices that a running task
# needs but adb has lost, and the per-device floor between reconnect attempts.
DEVICE_WATCH_INTERVAL_SEC = 5.0
ADB_RECONNECT_MIN_INTERVAL_SEC = 20.0

SERIAL_BAUD = 115200
SERIAL_READ_TIMEOUT = 0.2
SERIAL_QUEUE_MAX = 2000
SERIAL_OPEN_TIMEOUT = 3.0
# CONSECUTIVE reopen budget, cleared as soon as the console delivers a byte -
# same rule as logcat (see LOGCAT_MAX_RESTARTS). A device reboot can take the
# USB-serial adapter away and bring it back, so ~2 minutes of patience.
SERIAL_MAX_RESTARTS = 60
SERIAL_RECONNECT_DELAY_SEC = 2.0
# Serial was the only channel with unbounded disk growth: a device stuck before
# the bootloader banner, or a console shell left spewing at 115200 baud, writes
# up to ~1.2 GB/day. Same treatment as logcat - stop with a visible marker
# rather than fill the disk. Raise with PPTP_SERIAL_MAX_MB.
SERIAL_MAX_BYTES = int(os.environ.get("PPTP_SERIAL_MAX_MB", "512")) * 1024 * 1024
# Port names arrive from the browser, so validate the shape before opening.
SERIAL_PORT_RE = re.compile(r"^(COM[1-9][0-9]{0,2}|/dev/tty[A-Za-z0-9._-]{1,32})$")


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _name_parts(device: str, script: str, started_at: str) -> tuple[str, str, str]:
    """Sanitized (timestamp, script, device) triple shared by both namings."""
    try:
        ts = datetime.fromisoformat(started_at).strftime("%Y%m%d-%H%M%S")
    except Exception:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    scr = _SAFE_NAME_RE.sub("_", Path(script).stem)[:32] or "script"
    dev = _SAFE_NAME_RE.sub("_", device)[:32] or "device"
    return ts, scr, dev


def make_log_stem(task_id: str, device: str, script: str, started_at: str) -> str:
    """Build a human-readable, still task-prefixed log filename stem.

    Shape: <task_id>_<yyyyMMdd-HHmmss>_<script>_<device>

    The task_id stays FIRST on purpose: while a task is RUNNING its files live
    in logs/ and the cleanup path globs `<task_id>*` with a hex-regex guard, so
    keeping it leading means those guards need no change. Once the task ends,
    _archive_task moves the files into archive/ under make_archive_name(), which
    drops the task_id - that is the name a human actually has to read.
    """
    ts, scr, dev = _name_parts(device, script, started_at)
    return f"{task_id}_{ts}_{scr}_{dev}"


def script_module(script: str) -> str:
    """Archive module folder for a script (wifi / sensor / perf / ...)."""
    return ARCHIVE_MODULES.get(script, ARCHIVE_MODULE_FALLBACK)


def make_archive_name(device: str, script: str, started_at: str) -> str:
    """Human-facing archive folder name: <yyyyMMdd-HHmmss>_<script>_<device>.

    No task_id: the whole point of the archive is that a person can find it.
    The task_id is preserved inside summary.json instead. The module folder is
    the parent - see _archive_task.
    """
    ts, scr, dev = _name_parts(device, script, started_at)
    return f"{ts}_{scr}_{dev}"


def _log_path(task: dict[str, Any], source: str) -> Path:
    """Resolve one capture channel's log file - the single path authority.

    Archived tasks read from archive/<module>/<dir>/<member>; everything else (a
    task still running, or one that predates v2.7.0) reads from logs/.
    api_task_log and the ws_logs replay both go through here, so neither needs
    to know that archiving happened.
    """
    arch = task.get("archive")
    if arch:
        return _archive_dir(arch) / ARCHIVE_MEMBER[source]
    stem = task.get("log_stem") or task["task_id"]
    return LOGS_DIR / f"{stem}{LOG_SUFFIX[source]}"


def _archive_dir(arch: dict[str, Any]) -> Path:
    """Absolute path of an archived task's folder: archive/<module>/<name>/.

    Falls back to the flat archive/<name>/ layout for a task that was archived
    before module folders existed. Those only live in the in-memory task list,
    so this matters exactly once - a server that is upgraded while still holding
    tasks from the previous build - but getting it wrong shows an empty log with
    no explanation, which is a bad way to learn about it.
    """
    module = arch.get("module")
    if module:
        return ARCHIVE_DIR / module / arch["dir"]
    flat = ARCHIVE_DIR / arch["dir"]
    return flat if flat.is_dir() else ARCHIVE_DIR / ARCHIVE_MODULE_FALLBACK / arch["dir"]


# ---------------------------------------------------------------------------
# In-memory task state
# ---------------------------------------------------------------------------
# task_id -> {
#   task_id, device, script, params,
#   status: "running" | "interrupting" | "finished" | "failed" | "interrupted",
#   started_at, ended_at, exit_code,
#   log_file: Path,
#   captures: {source: {active, status, lines, ...}},   # public summary (no _ keys)
#   _proc: Popen, _reader_task: asyncio.Task,
#   _cap_logcat / _cap_serial: private capture state,
#   _cap_<source>_task: asyncio.Task driving that capture
# }
TASKS: dict[str, dict[str, Any]] = {}

# task_id -> set of WebSocket connections (for live log streaming)
WS_CLIENTS: dict[str, set[WebSocket]] = {}

# id(websocket) -> set of sources that connection subscribed to. Kept separate
# from WS_CLIENTS (which is keyed by task) so _broadcast can skip a whole
# channel when nobody is watching it - a source with zero subscribers costs
# one set comprehension and nothing else.
WS_SUBS: dict[int, set[str]] = {}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="PPTP", version="2.11.2")

# Disable HTTP caching for static files (dev mode)
app.add_middleware(NoCacheMiddleware)

_SERVER_STARTED_AT = time.time()
_SERVER_PID = os.getpid()


@app.on_event("startup")
async def _reap_orphan_scripts():
    """Kill leftover python scripts from a previous crashed PPTP server.

    Heuristic: scan tasklist for python.exe processes whose parent PID is dead
    AND whose command line contains --device (our script signature).
    """
    if os.name != "nt":
        return  # only Windows for now
    try:
        # Write our PID so a future instance can detect if we died
        (DATA_DIR / "server.pid").write_text(str(_SERVER_PID), encoding="utf-8")

        # Find PIDs of running python.exe processes
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
             "ProcessId,CommandLine,ParentProcessId", "/format:list"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return

    # Parse wmic output (key=value per process, blank line between)
    current: dict[str, str] = {}
    processes: list[dict[str, str]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            if current.get("ProcessId"):
                processes.append(current)
                current = {}
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            current[k.strip()] = v.strip()
    if current.get("ProcessId"):
        processes.append(current)

    # Live PIDs (any process alive on the system)
    try:
        live_out = subprocess.run(
            ["wmic", "process", "get", "ProcessId", "/format:list"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        live_pids = set()
        for ln in live_out.stdout.splitlines():
            ln = ln.strip()
            if "=" in ln and ln.lower().startswith("processid="):
                live_pids.add(ln.split("=", 1)[1].strip())
    except Exception:
        live_pids = set()

    killed = 0
    for p in processes:
        try:
            pid = int(p.get("ProcessId", "0"))
            ppid = int(p.get("ParentProcessId", "0"))
            cmd = p.get("CommandLine", "")
        except ValueError:
            continue
        # Only target: looks like one of our script subprocesses
        if "--device" not in cmd:
            continue
        # Skip ourselves (uvicorn itself)
        if pid == _SERVER_PID:
            continue
        # If parent is alive and not us, leave it alone (might be a sibling)
        if ppid in live_pids and ppid != _SERVER_PID:
            continue
        # Parent dead → orphan. Kill it.
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5,
            )
            killed += 1
        except Exception:
            pass

    if killed:
        # Log to server.log for visibility
        print(f"[startup] reaped {killed} orphan script subprocess(es)")

    # Clean up any leftover temp sequence files (from previous server crash
    # where the script subprocess didn't get to run its finally block).
    cleaned = 0
    for f in IR_SEQUENCES_DIR.glob("_seq_*.ini"):
        try:
            f.unlink()
            cleaned += 1
        except Exception:
            pass
    if cleaned:
        print(f"[startup] cleaned {cleaned} orphan temp sequence file(s)")

    # Orphan logcat clients (v2.6.0). If this server was killed rather than shut
    # down, its `adb logcat` children survive and keep writing forever.
    _reap_orphan_logcat()


def _reap_orphan_logcat() -> None:
    """Kill leftover `adb logcat` children from a previous crashed PPTP server.

    Matches the exact argv this server builds (`-v threadtime` + `-T 1`) rather
    than any process merely containing "logcat", so a developer's own hand-typed
    `adb logcat -s Foo` in another terminal is never touched.
    """
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='adb.exe'", "get",
             "ProcessId,CommandLine", "/format:list"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except Exception:
        return

    current: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            if current.get("ProcessId"):
                rows.append(current)
                current = {}
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            current[k.strip()] = v.strip()
    if current.get("ProcessId"):
        rows.append(current)

    killed = 0
    for row in rows:
        cmd = row.get("CommandLine", "")
        if "logcat" not in cmd or "-v threadtime" not in cmd or "-T 1" not in cmd:
            continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(int(row["ProcessId"]))],
                capture_output=True, timeout=5,
            )
            killed += 1
        except Exception:
            pass
    if killed:
        print(f"[startup] reaped {killed} orphan adb logcat process(es)")


# ---------------------------------------------------------------------------
# Device watchdog (v2.7.2)
# ---------------------------------------------------------------------------
def _serial_is_offline(devices: list[dict[str, str]], serial: str) -> bool:
    """True if `serial` is absent from adb, or present but not usable."""
    for d in devices:
        if d.get("serial") == serial:
            return d.get("status") != "device"
    return True


def _adb_reconnect_offline() -> None:
    """`adb reconnect offline` - asks adb to re-probe offline/unauthorized devices.

    Deliberately NOT `adb kill-server`: that would tear down every adb client on
    the box, including the logcat capture of an unrelated, healthy device. The
    user's rule is "reconnecting must not disturb other running scripts", and
    `reconnect offline` only touches devices that are already unusable.
    Never raises.
    """
    try:
        subprocess.run(["adb", "reconnect", "offline"],
                       capture_output=True, text=True, timeout=10,
                       encoding="utf-8", errors="replace")
    except Exception:
        pass


@app.on_event("startup")
async def _start_device_watchdog():
    asyncio.create_task(_device_watchdog())


async def _device_watchdog() -> None:
    """Periodically nudge adb to recover devices that a running task needs.

    Monitor/reboot scripts take the device away on purpose, so "device is gone"
    is a normal condition rather than an error - but if USB re-enumerated, adb
    may not pick the device back up on its own. This loop notices and asks.

    Scope is deliberately narrow: only devices that some running/interrupting
    task is actually using, only `adb reconnect offline`, and at most one
    attempt per device per ADB_RECONNECT_MIN_INTERVAL_SEC. Nothing is written to
    the UI - the device card already shows "临时离线", which is the signal the
    user asked for.
    """
    last_attempt: dict[str, float] = {}
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.sleep(DEVICE_WATCH_INTERVAL_SEC)
            busy = {
                t.get("device")
                for t in list(TASKS.values())
                if t.get("status") in ("running", "interrupting") and t.get("device")
            }
            if not busy:
                last_attempt.clear()
                continue
            devices = await loop.run_in_executor(None, _list_adb_devices)
            now = time.time()
            for serial in busy:
                if not _serial_is_offline(devices, serial):
                    last_attempt.pop(serial, None)
                    continue
                if now - last_attempt.get(serial, 0.0) < ADB_RECONNECT_MIN_INTERVAL_SEC:
                    continue
                last_attempt[serial] = now
                print(f"[watchdog] {serial} offline - adb reconnect offline")
                await loop.run_in_executor(None, _adb_reconnect_offline)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Helpers - ADB
# ---------------------------------------------------------------------------
def _list_adb_devices() -> list[dict[str, str]]:
    """Run `adb devices -l` and parse to list of dicts.

    Returns empty list if adb is missing, hangs, or returns non-zero.
    Never raises — callers can safely render an empty list.
    """
    try:
        out = subprocess.run(
            ["adb", "devices", "-l"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return []

    if out.returncode != 0:
        return []

    devices: list[dict[str, str]] = []
    # example line: "abc123       device usb:1-1 product:foo model:Bar device:Baz"
    for line in out.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, status = parts[0], parts[1]
        if status not in ("device", "offline", "unauthorized"):
            continue
        kv = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append({
            "serial": serial,
            "status": status,
            "model": kv.get("model", kv.get("product", "")),
            "product": kv.get("product", ""),
            "transport": kv.get("transport", kv.get("usb", "")),
        })
    return devices


# ---------------------------------------------------------------------------
# Helpers - scripts
# ---------------------------------------------------------------------------
def _script_source(name: str) -> str:
    """Read a script's source, or "" if it cannot be read. Never raises."""
    try:
        return (SCRIPTS_DIR / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _script_owns_serial(name: str) -> bool:
    """True if the script drives the serial console itself.

    Source sniff rather than a --dump-params round trip: this runs on the
    /api/scripts and /api/run paths, where spawning a subprocess per script
    would be far too slow. A module-level PARAMS entry named "serial_port" is
    the only way a script takes over the port, and the literal is unambiguous.
    """
    return "serial_port" in _script_source(name)


def _list_scripts() -> list[dict[str, Any]]:
    """Return .py files under scripts/."""
    items: list[dict[str, Any]] = []
    for p in sorted(SCRIPTS_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        # Cheap source-sniff: a script that implements --dump-params declares
        # frontend-configurable params (see /api/scripts/{name}/params).
        src = _script_source(p.name)
        items.append({
            "name": p.stem,
            "filename": p.name,
            "path": str(p),
            "has_params": "--dump-params" in src,
            # Drives the serial-capture opt-in: when true the UI disables the
            # platform's own serial capture for this script (the script wins).
            "owns_serial": "serial_port" in src,
        })
    return items


# ---------------------------------------------------------------------------
# Helpers - log capture
# ---------------------------------------------------------------------------
def _public_captures(t: dict[str, Any]) -> dict[str, Any]:
    """Public per-source capture summary (no private keys, safe for the API).

    Carries only slow-moving state: the line counters are deliberately NOT
    here. This dict ships inside every /api/tasks payload and is part of
    tasksRenderKey, so a value that changes every couple of seconds would force
    a full task-list re-render on every poll. Per-channel line counts reach the
    user through the archive instead (archive/<dir>/summary.json + the console
    summary line), not through the live UI.
    """
    out: dict[str, Any] = {"stdout": {"active": True, "status": t.get("status", "running")}}
    for src in CAPTURES_COUNTED:
        cap = t.get(f"_cap_{src}")
        if not cap:
            continue
        info: dict[str, Any] = {
            "active": bool(cap.get("_active")),
            "status": cap.get("status", "off"),
        }
        for k in ("restarts", "detail", "port"):
            v = cap.get(k)
            if v not in (None, "", 0):
                info[k] = v
        out[src] = info
    return out


def _new_capture(active: bool, status: str, extra: dict | None = None) -> dict[str, Any]:
    cap: dict[str, Any] = {
        "_active": active,
        "status": status,
        "lines": 0,
        "bytes": 0,
        "restarts": 0,
        "dropped": 0,
        "detail": "",
        "_proc": None,
        "_stop_evt": None,
        "_stop": False,
        "_stopped": False,
        "_lines_since_check": 0,
    }
    if extra:
        cap.update(extra)
    return cap


def _ws_sources(ws: WebSocket) -> set[str]:
    return WS_SUBS.get(id(ws), {"stdout"})


async def _announce_captures(task: dict) -> None:
    """Refresh the public snapshot and tell clients a channel changed state.

    Both halves matter. The broadcast tells a connected client immediately; the
    snapshot refresh is what makes GET /api/tasks honest, because `captures` is
    a plain field on the task. v2.7.0 deleted the 2s counter ticker (it existed
    only for the removed pills) and with it the ONLY thing that refreshed that
    field -- so a channel could sit in `starting` in the API for an entire run
    while actually reconnecting, and the truth only appeared at task end. Every
    status transition goes through here now, so nothing else needs to remember.
    """
    task["captures"] = _public_captures(task)
    await _broadcast(task["task_id"], {
        "type": "capture_status", "captures": task["captures"],
    }, source=None)


async def _broadcast(task_id: str, message: dict, source: str | None = "stdout") -> None:
    """Send one frame to the WS clients subscribed to `source`.

    source=None is a control frame (state / end / capture_status / counts) and
    goes to everyone. For a real channel, clients that did not subscribe are
    skipped - a channel nobody watches costs one set comprehension and nothing
    else, which is what keeps an always-on logcat feed free for a stdout-only UI.

    The send timeout is new in v2.6.0 (previously unbounded): a wedged TCP
    connection must not be able to stall a capture reader forever.
    """
    if source is None:
        subs = list(WS_CLIENTS.get(task_id, set()))
        payload = json.dumps(message, ensure_ascii=False)
    else:
        subs = [ws for ws in WS_CLIENTS.get(task_id, set()) if source in _ws_sources(ws)]
        if not subs:
            return
        payload = json.dumps({**message, "source": source}, ensure_ascii=False)

    for ws in subs:
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=WS_SEND_TIMEOUT_SEC)
        except Exception:
            WS_CLIENTS.get(task_id, set()).discard(ws)
            WS_SUBS.pop(id(ws), None)
            # Actually close it. Dropping it from the sets only stops US sending;
            # the browser would sit on a socket that will never speak again, and
            # the client only reconnects from its own onclose handler - so a
            # wedged client would silently stop receiving logs forever, with the
            # UI still looking like it was attached to a live task.
            #
            # The close is bounded for the same reason the send is: this runs on
            # the capture reader's path, and a socket that just proved it can
            # stall would otherwise get a second, unbounded chance to stall it.
            try:
                await asyncio.wait_for(ws.close(), timeout=WS_SEND_TIMEOUT_SEC)
            except Exception:
                pass


async def _emit(task_id: str, source: str, line: str, fh=None, size: int | None = None) -> None:
    """Count one captured line, append it to the channel's file, stream it.

    Disk first, then WS: a slow client can delay the stream but never the file.
    """
    cap = TASKS.get(task_id, {}).get(f"_cap_{source}")
    if cap is not None:
        cap["lines"] = cap.get("lines", 0) + 1
        cap["bytes"] = cap.get("bytes", 0) + (size if size is not None else len(line) + 1)
    if fh is not None:
        try:
            fh.write(line + "\n")
            fh.flush()
        except Exception:
            pass
    await _broadcast(task_id, {"type": "log", "line": line}, source)


async def _stream_logs(task_id: str) -> None:
    """Read proc.stdout line-by-line, broadcast to WS clients, write to file."""
    task = TASKS.get(task_id)
    if not task:
        return
    proc = task["_proc"]
    log_file = task["log_file"]

    loop = asyncio.get_running_loop()
    reader_error: str | None = None
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            while True:
                # read_line is blocking; run in default executor
                try:
                    line = await loop.run_in_executor(None, proc.stdout.readline)
                except Exception as e:
                    # A failing reader must NOT escape to the handler at the
                    # bottom of this function: that skips the entire
                    # finalization block below, leaving the task "running"
                    # forever with no archive. Break out so the terminal state
                    # is still written; the reason is reported just after.
                    reader_error = f"stdout read failed: {e}"
                    break
                if not line:
                    break
                text = line.rstrip("\n")
                await _emit(task_id, "stdout", text, f)
                # Sniff the report path while lines flow. Every report-writing
                # script prints `  report <pad>: <abs path>` right after saving,
                # and that line is the only runtime signal the path ever exists.
                # Stop looking once found - this runs per line.
                if not task.get("_report_path"):
                    p = _sniff_report_path(text)
                    if p:
                        task["_report_path"] = p

        if reader_error:
            await _broadcast(task_id, {"type": "error", "message": reader_error},
                             source=None)
            # We are blind to this process now, so it must not be left running:
            # an unwatched stress run has no way to end. Same termination the
            # force-stop path uses. exit_code then decides the final status.
            try:
                proc.terminate()
            except Exception:
                pass

        # Off the event loop: after a reader failure the child may still be
        # alive, and a blocking wait() here would freeze the whole server.
        await loop.run_in_executor(None, proc.wait)
        exit_code = proc.returncode

        if task.get("_finalized"):
            # A force-stop already finalized this task: it had to, because a
            # SIGKILLed script never prints its report line and may never exit.
            # It set the terminal status, stopped the captures, archived and
            # broadcast the end frame + archive line already - mirroring
            # everything below. Finalizing a second time would overwrite its
            # "interrupted" with the "failed" that this exit code (-9) earns,
            # which is exactly the race that used to leave the task stuck
            # non-terminal and the device blocked by the /api/run guard.
            return

        task["exit_code"] = exit_code

        if task["status"] == "interrupting":
            task["status"] = "interrupted"
        elif exit_code == 0:
            task["status"] = "finished"
        else:
            task["status"] = "failed"

        task["ended_at"] = datetime.now().isoformat(timespec="seconds")

        # Close the device channels first (this is what releases the serial port
        # and the logcat file handles), THEN archive - the log files cannot be
        # moved on Windows while anyone still holds them open.
        await _stop_captures(task_id, "task_end")
        await asyncio.get_running_loop().run_in_executor(
            None, _archive_task, task_id, "task_end")

        await _broadcast(task_id, {
            "type": "end",
            "status": task["status"],
            "exit_code": exit_code,
            "ended_at": task["ended_at"],
            "archive": task.get("archive"),
        }, source=None)
        await _broadcast_archive_line(task_id)
    except Exception as e:
        await _broadcast(task_id, {"type": "error", "message": str(e)}, source=None)


async def _capture_logcat(task_id: str) -> None:
    """Stream `adb logcat` for one task, restarting across adb/device outages.

    The restart loop is the answer to both `POST /api/adb/reconnect` (which runs
    `adb kill-server` and kills the logcat client) and to reboot stress scripts
    that take the device away for minutes at a time. Each gap writes a visible
    marker line - that marker is the only way a reader can tell a gap happened.

    Note: we never run `logcat -c`. app_launch_stress.py reads the device-side
    ring buffer (`logcat -d -s ActivityTaskManager`) for its cold-start
    cross-check; clearing the buffer from here would silently break it.
    """
    task = TASKS.get(task_id)
    if not task:
        return
    cap = task["_cap_logcat"]
    serial = task["device"]

    # First start tails a single line so a fresh task does not inherit whatever
    # happened to be sitting in the ring buffer. A RESTART tails far more, on
    # purpose: the usual reason we are restarting is that the device rebooted,
    # and everything it logged during the boot is still in the (fresh) ring
    # buffer. `-T 1` there would throw that away and the file would resume only
    # once adbd is back - roughly 25s into boot. The cost is that a restart
    # after a mere adb hiccup (no reboot) re-dumps the last N lines, so the
    # marker line says so explicitly.
    def _logcat_args(tail_lines: int) -> list[str]:
        a = ["adb", "-s", serial, "logcat", "-v", "threadtime", "-T", str(tail_lines)]
        for buf in LOGCAT_BUFFERS:
            a += ["-b", buf]
        a.append(LOGCAT_FILTER)
        return a

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    fh = None
    try:
        fh = open(_log_path(task, "logcat"), "a", encoding="utf-8")
    except Exception as e:
        cap["detail"] = f"open log failed: {e}"

    restarts = 0
    try:
        while True:
            if cap.get("_stop"):
                break
            args = _logcat_args(1 if restarts == 0 else LOGCAT_RESTART_TAIL_LINES)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    creationflags=creationflags,
                    limit=LOGCAT_READ_BYTES,
                )
            except Exception as e:
                cap["status"] = "failed"
                cap["detail"] = f"logcat exec failed: {e}"
                break

            cap["_proc"] = proc
            cap["status"] = "running"
            cap["detail"] = ""
            await _announce_captures(task)

            got_output = False
            try:
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not got_output:
                        got_output = True
                        # Output proves the device is reachable again, so the
                        # restart budget is clear. It has to work this way: a
                        # reboot-heavy soak uses several restarts PER REBOOT
                        # (adb exits rather than blocking while the device is
                        # down), and a lifetime counter would kill logcat at
                        # iteration ~6 of a default 100-iteration run. What the
                        # budget actually bounds is CONSECUTIVE failures - a
                        # device that is gone for good.
                        if restarts:
                            restarts = 0
                            cap["restarts"] = 0
                    await _emit(task_id, "logcat", line, fh, len(raw))
                    cap["_lines_since_check"] += 1
                    if cap["_lines_since_check"] >= LOGCAT_CAP_CHECK_LINES:
                        cap["_lines_since_check"] = 0
                        if cap["bytes"] >= LOGCAT_MAX_BYTES:
                            cap_mb = LOGCAT_MAX_BYTES // (1024 * 1024)
                            cap["status"] = "capped"
                            cap["detail"] = f"size cap {cap_mb}MB reached"
                            await _emit(
                                task_id, "logcat",
                                f"--- logcat capture stopped: size cap reached "
                                f"({cap_mb}MB, raise PPTP_LOGCAT_MAX_MB) ---", fh)
                            try:
                                proc.kill()
                            except Exception:
                                pass
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                cap["status"] = "failed"
                cap["detail"] = str(e)

            # Reap the subprocess. `kill()` is synchronous; `wait()` must be
            # awaited or the transport is left unclosed on loop shutdown.
            if cap["_proc"] is not None:
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                cap["exit_code"] = proc.returncode
                cap["_proc"] = None

            if cap.get("_stop") or cap["status"] == "capped":
                break

            restarts += 1
            cap["restarts"] = (cap.get("restarts") or 0) + 1
            if restarts > LOGCAT_MAX_RESTARTS:
                cap["status"] = "failed"
                cap["detail"] = (
                    f"logcat exited {restarts} times in a row "
                    f"(device gone for ~{int(restarts * LOGCAT_RESTART_DELAY_SEC)}s)")
                break
            await _emit(
                task_id, "logcat",
                f"--- logcat capture restart #{restarts} (consecutive) "
                f"- re-reading last {LOGCAT_RESTART_TAIL_LINES} lines, "
                f"overlap with earlier output is possible ---", fh)
            await _announce_captures(task)
            await asyncio.sleep(LOGCAT_RESTART_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    finally:
        cap["_proc"] = None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass


async def _capture_serial(task_id: str, port: str) -> None:
    """Stream a device debug console over a COM port (opt-in per run).

    A serial port has no file descriptor, so each attempt needs a real thread.
    The reader thread hands lines to the event loop through an asyncio.Queue; it
    never awaits and never touches asyncio state directly.

    The channel is SUPERVISED: if the port goes away (device reboot causing USB
    re-enumeration, adapter glitch, cable knock) the attempt ends and we reopen
    it, writing a visible marker line into serial.log. Before v2.7.2 this was a
    single open attempt for the whole run, so one glitch silently ended console
    capture for the rest of a multi-hour soak.

    Failures are never fatal: a missing pyserial, a malformed port name, a port
    that is not enumerated, or a busy port all degrade to a status string. The
    task itself must keep running regardless.
    """
    task = TASKS.get(task_id)
    if not task:
        return
    cap = task["_cap_serial"]

    # These two are permanent conditions - retrying cannot fix a typo or a
    # missing pyserial, so they report and stop rather than entering the loop.
    if not port or not SERIAL_PORT_RE.match(port):
        cap["status"] = "failed"
        cap["detail"] = f"invalid port name: {port!r}"
        return

    try:
        import serial  # noqa: PLC0415 - lazy so the server boots without pyserial
        from serial.tools import list_ports
    except Exception as e:
        cap["status"] = "unavailable"
        cap["detail"] = f"pyserial unavailable: {e}"
        return

    loop = asyncio.get_running_loop()

    # One handle for the whole task, so markers and data land in the same file
    # in order and the gap is visible to anyone reading the archive later.
    fh = None
    try:
        fh = open(_log_path(task, "serial"), "a", encoding="utf-8")
    except Exception:
        fh = None

    fails = 0
    try:
        while not cap.get("_stop"):
            reason = ""
            try:
                enumerated = {p.device for p in list_ports.comports()}
            except Exception:
                enumerated = set()
            if enumerated and port not in enumerated:
                # The adapter is not on the bus right now. Common during a
                # device reboot, and exactly the case worth waiting out.
                reason = f"port not present: {port}"
                got_data = False
            else:
                reason, got_data = await _serial_attempt(
                    task_id, task, cap, serial, port, loop, fh)

            if got_data:
                # Real console output proves the link works, so the budget is
                # clear. Must work this way: the counter bounds CONSECUTIVE
                # failures (adapter gone for good), not the number of glitches
                # a long soak is allowed to survive. Same rule as logcat.
                fails = 0
                cap["restarts"] = 0

            if cap.get("_stop") or cap["status"] == "capped" or not reason:
                break

            fails += 1
            cap["restarts"] = fails
            if fails > SERIAL_MAX_RESTARTS:
                cap["status"] = "failed"
                cap["detail"] = (f"{reason} -- giving up after {fails} consecutive "
                                 f"attempts (~{int(fails * SERIAL_RECONNECT_DELAY_SEC)}s)")
                await _announce_captures(task)
                break
            was_reconnecting = cap["status"] == "reconnecting"
            cap["status"] = "reconnecting"
            cap["detail"] = reason
            # Announce only on ENTRY into reconnecting. `captures` lives on the
            # task object and is therefore part of tasksRenderKey, so announcing
            # on every 2s attempt would force a full task-card re-render for the
            # whole storm - the exact anti-pattern documented as pitfall #23.
            # Per-attempt detail goes to serial.log's marker lines instead, and
            # the final count lands in summary.json.
            if not was_reconnecting:
                await _announce_captures(task)
            await _emit(
                task_id, "serial",
                f"--- serial capture reconnecting #{fails} ({reason}) ---", fh)
            await asyncio.sleep(SERIAL_RECONNECT_DELAY_SEC)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # The supervisor itself must not die quietly.
        cap["status"] = "failed"
        cap["detail"] = f"serial supervisor error: {e}"
    finally:
        evt = cap.get("_stop_evt")
        if evt is not None:
            try:
                evt.set()
            except Exception:
                pass
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass


async def _serial_attempt(task_id: str, task: dict, cap: dict, serial_mod,
                          port: str, loop, fh) -> tuple[str, bool]:
    """One open-and-read cycle.

    Returns (failure_reason, got_data). An EMPTY reason means "stop cleanly" -
    the channel was asked to stop or hit the size cap - and the caller must not
    retry on it.

    `cap["_stop_evt"]` is repointed at THIS attempt's event, because
    _stop_captures stops the reader thread through it and the previous thread is
    already gone by the time we get here.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=SERIAL_QUEUE_MAX)
    stop_evt = threading.Event()
    opened = threading.Event()
    err: list[str] = []

    def _put(item: str) -> None:
        """Runs on the event loop via call_soon_threadsafe."""
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            # Drop the oldest line instead of growing without bound - a debug
            # console can outrun a slow WS client over a 24h soak.
            cap["dropped"] = cap.get("dropped", 0) + 1
            try:
                q.get_nowait()
                q.put_nowait(item)
            except Exception:
                pass

    def reader() -> None:
        buf = b""
        try:
            # Defaults for DTR/RTS are left untouched on purpose: asserting them
            # can reset a device console. battery_inout_stress.py opens the same
            # way on this hardware without perturbing it.
            with serial_mod.Serial(port, SERIAL_BAUD,
                                   timeout=SERIAL_READ_TIMEOUT) as ser:
                opened.set()
                while not stop_evt.is_set():
                    # in_waiting or 1 (not readline) so a console prompt with no
                    # trailing newline does not stall for a full timeout tick.
                    chunk = ser.read(ser.in_waiting or 1)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        loop.call_soon_threadsafe(
                            _put, raw.decode("utf-8", errors="replace").rstrip("\r"))
                if buf:
                    loop.call_soon_threadsafe(
                        _put, buf.decode("utf-8", errors="replace"))
        except Exception as e:
            err.append(str(e))
            opened.set()

    cap["_stop_evt"] = stop_evt
    thread = threading.Thread(
        target=reader, name=f"pptp-serial-{task_id[:8]}", daemon=True)
    thread.start()

    try:
        # Wait for the port to open (or fail) so a busy COM port becomes a
        # status instead of a hang. Polling is fine - it is a bounded window.
        deadline = time.time() + SERIAL_OPEN_TIMEOUT
        while not opened.is_set() and time.time() < deadline:
            if cap.get("_stop"):
                return "", False
            await asyncio.sleep(0.05)

        if err:
            return err[0], False
        if not opened.is_set():
            return "open timed out", False

        cap["status"] = "running"
        cap["detail"] = ""
        await _announce_captures(task)

        got_data = False
        while True:
            if cap.get("_stop"):
                return "", got_data
            # Wake at least once a second even on a silent console, so a reader
            # thread that died after the open window (USB adapter re-enumerated,
            # cable glitch, read error) is noticed. Without this the consumer
            # would block on an empty queue forever while the channel still
            # reported "running" - a dead console indistinguishable from a quiet
            # one, which for a 24h soak means silently losing the rest of it.
            try:
                line = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not thread.is_alive():
                    return (err[0] if err else "serial reader thread died"), got_data
                continue
            got_data = True
            await _emit(task_id, "serial", line, fh)
            cap["_lines_since_check"] += 1
            if cap["_lines_since_check"] >= LOGCAT_CAP_CHECK_LINES:
                cap["_lines_since_check"] = 0
                if cap["bytes"] >= SERIAL_MAX_BYTES:
                    cap_mb = SERIAL_MAX_BYTES // (1024 * 1024)
                    cap["status"] = "capped"
                    cap["detail"] = f"size cap {cap_mb}MB reached"
                    await _emit(
                        task_id, "serial",
                        f"--- serial capture stopped: size cap reached "
                        f"({cap_mb}MB, raise PPTP_SERIAL_MAX_MB) ---", fh)
                    return "", got_data
    except asyncio.CancelledError:
        raise
    finally:
        # Cancelling this coroutine does not stop the reader thread - only the
        # event does. The thread exits within one SERIAL_READ_TIMEOUT tick.
        stop_evt.set()


async def _stop_captures(task_id: str, reason: str) -> None:
    """Stop every capture channel for a task. Idempotent, safe from any state.

    Deliberately front-loads all the synchronous teardown (flags, the reader
    stop event, proc.kill()) before its first await, so being cancelled midway
    cannot leave a capture running - at worst the bookkeeping is skipped.

    Note that cancelling the asyncio task is NOT what stops the serial reader;
    only _stop_evt does. Both are done here.
    """
    t = TASKS.get(task_id)
    if not t:
        return

    pending: list[tuple[str, dict, Any, Any]] = []
    for src in CAPTURES_COUNTED:
        cap = t.get(f"_cap_{src}")
        if not cap or cap.get("_stopped"):
            continue
        ctask = t.get(f"_cap_{src}_task")
        proc = cap.get("_proc")
        # A channel that never came up (opt-out `off`, arbitration `skipped`) has
        # nothing to stop and nothing to report.
        if ctask is None and proc is None and cap.get("_stop_evt") is None:
            continue
        # A channel that stopped on its OWN terms keeps that status. `failed`
        # (pyserial could not open the port, logcat died), `unavailable` (no
        # pyserial) and `capped` (size limit hit) are the only explanation the
        # user ever gets; stamping "stopped / task_end" over them would report a
        # capture that never worked - or one that was truncated - as a clean
        # stop. Teardown still runs either way: a dead-but-running process must
        # still be reaped.
        keep_status = cap.get("status") in ("failed", "unavailable", "skipped", "capped")
        cap["_stop"] = True
        cap["_stopped"] = True
        evt = cap.get("_stop_evt")
        if evt is not None:
            try:
                evt.set()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        if ctask is not None and not ctask.done():
            ctask.cancel()
        if keep_status:
            pending.append((src, cap, ctask, proc))
            continue
        cap["status"] = "stopped"
        cap["detail"] = reason
        pending.append((src, cap, ctask, proc))

    if not pending:
        # Still refresh the public snapshot: a channel may have transitioned
        # (e.g. logcat failed) without there being anything left to tear down.
        t["captures"] = _public_captures(t)
        return

    t["captures"] = _public_captures(t)
    for _src, _cap, ctask, proc in pending:
        if ctask is not None:
            try:
                await ctask
            except BaseException:   # includes the CancelledError we just caused
                pass
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except BaseException:
                pass

    # No capture_status/capture_counts broadcast here any more (v2.7.0): the
    # frontend never displays the device channels, so it has nothing to render
    # them into. The outcome reaches the user through _archive_task's console
    # summary line and through archive/<dir>/summary.json.


def _archive_task(task_id: str, reason: str) -> None:
    """Move a finished task's artifacts into archive/<module>/<name>/ + manifest.

    Idempotent and synchronous on purpose: it is called from four different
    teardown paths (_stream_logs, force-stop, force-cleanup, server shutdown),
    one of which runs with 0.8s left before os._exit. It must never raise - a
    failure to archive is recorded in the manifest, not propagated, because the
    alternative is losing the task's teardown entirely.
    """
    task = TASKS.get(task_id)
    if not task or task.get("_archived"):
        return
    task["_archived"] = True

    notes: list[str] = []
    module = script_module(task.get("script", ""))
    name = make_archive_name(
        task.get("device", ""), task.get("script", ""), task.get("started_at", ""))
    parent = ARCHIVE_DIR / module
    dest = parent / name
    # Two runs in the same second on the same script+device would collide.
    n = 2
    while dest.exists():
        dest = parent / f"{name}-{n}"
        n += 1
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[archive] mkdir failed for {task_id}: {e}")
        return

    artifacts: dict[str, Any] = {}

    # 1) Move the three capture logs out of the transient logs/ area. They must
    #    already be closed: _stream_logs' `with open(...)` exits before it calls
    #    us, and _stop_captures closed the device channels. Windows refuses to
    #    move a file with an open handle, so a failure here is non-fatal - we
    #    keep the original in logs/ and say so.
    for src in CAPTURE_SOURCES:
        srcp = LOGS_DIR / f"{task.get('log_stem') or task_id}{LOG_SUFFIX[src]}"
        if not srcp.exists():
            continue
        try:
            size = srcp.stat().st_size
            lines = _count_lines(srcp)
            srcp.replace(dest / ARCHIVE_MEMBER[src])
            artifacts[ARCHIVE_MEMBER[src]] = {"bytes": size, "lines": lines}
        except Exception as e:
            notes.append(f"{src}: move failed ({type(e).__name__}) - still in logs/")

    # 2) Take the report the script wrote for itself.
    #
    #    Copy, then remove the source - i.e. a move, expressed as two steps so a
    #    failure to delete can never cost us the only copy. The effect is that
    #    reports/ is only a staging area that empties itself: once a run is
    #    archived, its report exists in exactly one place (archive/<run>/) and
    #    there is no second copy drifting out of date. Running a script by hand
    #    outside the platform still writes there, which is the only reason the
    #    folder exists at all.
    report_srcs, report_how = _find_task_reports(task)
    if report_srcs:
        for idx, src in enumerate(report_srcs):
            # First one keeps the canonical name; extras from the same run
            # (app_launch writes one per app) get report-2.json, report-3.json.
            name = (ARCHIVE_REPORT_NAME if idx == 0
                    else f"report-{idx + 1}.json")
            try:
                shutil.copy2(src, dest / name)
                artifacts[name] = {"bytes": src.stat().st_size}
                try:
                    src.unlink()
                except Exception:
                    notes.append(f"{name}: copied, but the original in reports/ "
                                 f"could not be removed")
            except Exception as e:
                notes.append(f"{name}: copy failed ({type(e).__name__})")
                if idx == 0:
                    report_how = None
        if len(report_srcs) > 1:
            notes.append(f"{len(report_srcs)} reports from this run "
                         f"(one per app) - all archived")

        # 2b) The data files written next to the report (perf_monitor v2 emits
        #     samples.csv + events.csv). They keep their own names: unlike the
        #     report they are not interchangeable, and the flat name already
        #     carries the device and timestamp that make them self-describing.
        #     Without this they would pile up in reports/ forever, since the
        #     report move above is the only thing that empties that staging area.
        seen_comp: set[str] = set()
        for src in report_srcs:
            for comp in _companion_files(src):
                key = str(comp.resolve())
                if key in seen_comp:
                    continue
                seen_comp.add(key)
                try:
                    shutil.copy2(comp, dest / comp.name)
                    artifacts[comp.name] = {"bytes": comp.stat().st_size}
                    try:
                        comp.unlink()
                    except Exception:
                        notes.append(f"{comp.name}: copied, but the original in "
                                     f"reports/ could not be removed")
                except Exception as e:
                    notes.append(f"{comp.name}: copy failed ({type(e).__name__})")
    elif task.get("script") not in REPORT_WRITING_SCRIPTS:
        notes.append("this script writes no report")
    else:
        notes.append("report not found (hard kill skips the script's save step?)")

    caps = _public_captures(task)
    for src in CAPTURE_SOURCES:
        ci = caps.get(src) or {}
        if ci.get("status") == "capped":
            notes.append(f"{src}: TRUNCATED at the size cap")

    summary = {
        "task_id": task_id,
        "device": task.get("device"),
        "script": task.get("script"),
        "params": task.get("params") or {},
        "status": task.get("status"),
        "exit_code": task.get("exit_code"),
        "started_at": task.get("started_at"),
        "ended_at": task.get("ended_at"),
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "archive_reason": reason,
        "captures": caps,
        "artifacts": artifacts,
        "report_source": report_how,
        "notes": notes,
    }
    try:
        (dest / ARCHIVE_SUMMARY_NAME).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[archive] summary write failed for {task_id}: {e}")

    total = sum(a.get("bytes", 0) for a in artifacts.values())
    task["archive"] = {
        "module": module,
        "dir": dest.name,
        "path": str(dest),
        "files": sorted(artifacts.keys()),
        "bytes": total,
    }
    # Private: drives the one-line console summary. Kept out of the public task
    # payload so /api/tasks does not carry a copy of the manifest every 2s.
    task["_archive_summary"] = summary
    print(f"[archive] {task_id} -> archive/{module}/{dest.name} "
          f"({total} bytes, {reason})")


def _sniff_report_path(line: str) -> str | None:
    """Pull `<abs path>` out of a script's `  report : <path>` summary line.

    The label is spelled `report` by every report-writing script; only the
    column padding differs, so split on the LAST " : " and keep it if it looks
    like a .json path. Validation against REPORTS_DIR happens later, in
    _find_task_reports - this is only a candidate.
    """
    if " : " not in line or "report" not in line:
        return None
    cand = line.rsplit(" : ", 1)[1].strip()
    return cand if cand.lower().endswith(".json") else None


async def _broadcast_archive_line(task_id: str) -> None:
    """Push ONE console line describing what was archived.

    v2.7.0 removed the live channel UI, which also removed the only place a
    serial-capture failure used to show up. Without this line a failed capture
    would be completely silent - exactly the kind of thing that gets reported as
    a bug much later. One line, existing channel, no new DOM, no polling.

    Broadcast only: it is NOT written into stdout.log (that file is the script's
    own output and is already archived by this point).
    """
    task = TASKS.get(task_id)
    summary = (task or {}).get("_archive_summary")
    if not task or not summary:
        return

    caps = summary.get("captures") or {}
    bits: list[str] = []
    for src in CAPTURE_SOURCES:
        info = caps.get(src) or {}
        st = info.get("status") or "off"
        entry = summary["artifacts"].get(ARCHIVE_MEMBER[src]) or {}
        if st == "running":                     # never reached a terminal state
            st = "interrupted"
        bits.append(f"{src} {entry.get('lines', 0)}L")
        if st not in ("stopped", "finished", "interrupted"):
            bits[-1] += f" ({st}{': ' + info['detail'] if info.get('detail') else ''})"
    for name in (ARCHIVE_REPORT_NAME, *ARCHIVE_UPLOADS):
        if name in summary["artifacts"]:
            bits.append(name)
    # Report sidecars. v2.10.0 gives every report-writing script a Chinese .html
    # twin of its JSON (perf_monitor had one since v2.9.0; see
    # docs/REPORT_FORMAT.md). It lands in the archive via _companion_files, but
    # its name is derived from the report stem, so it cannot be listed as a
    # constant above. Suffix match is safe: the only .html that ever reaches this
    # folder is a script's own report twin - nothing else writes one - and this
    # line is meant to be a faithful inventory of the folder it names.
    for name in sorted(summary["artifacts"]):
        if name.lower().endswith(".html"):
            bits.append(name)

    arch = task["archive"]
    line = (f"[archive] {arch.get('module', ARCHIVE_MODULE_FALLBACK)}/"
            f"{arch['dir']}/ | " + " | ".join(bits))
    for note in summary.get("notes") or []:
        line += f" | note: {note}"
    await _broadcast(task_id, {"type": "log", "line": line}, source=None)


def _count_lines(p: Path) -> int:
    """Count lines without holding the file open - the handle must be free."""
    try:
        with p.open("rb") as fh:
            return sum(1 for _ in fh)
    except Exception:
        return 0


def _find_task_reports(task: dict[str, Any]) -> tuple[list[Path], str | None]:
    """Locate every JSON report a script wrote for this run, best first.

    Preferred source is the `report : <path>` line every report-writing script
    prints (captured live by _stream_logs). Falls back to scanning REPORTS_DIR
    for files whose mtime lands inside the task window - a hard kill can skip
    the print. Either way the path is validated to live under REPORTS_DIR: the
    line comes from a script's stdout, so it is data, not a trusted path.

    Returns a LIST because one run can produce several reports - app_launch
    writes one per app. Archiving only the printed path would leave the rest
    behind in reports/ forever, which is exactly the clutter the user asked to
    be rid of.

    The scan is deliberately narrow, because attributing the WRONG report is
    worse than attributing none: a plausible-looking report.json that actually
    belongs to the previous run is a fabricated artifact. Three guards:
      1. Only scripts known to write reports are scanned at all - otherwise a
         run of ir_runner / wifi_reboot (which never write one) would pick up
         whatever the neighbouring run just produced.
      2. mtime must be >= task start. The report is written as the script's last
         act, so it cannot predate the run.
      3. A report file is claimed by at most one task, ever.
    """
    def _safe(p: Path) -> Path | None:
        try:
            rp = p.resolve()
        except Exception:
            return None
        return rp if REPORTS_DIR.resolve() in rp.parents else None

    def _siblings(started, ended, dev_short, exclude: Path) -> list[Path]:
        if started is None:
            return []
        lo, hi = started.timestamp(), ended.timestamp() + 5
        out: list[tuple[float, Path]] = []
        try:
            for f in REPORTS_DIR.rglob("*.json"):
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if not (lo <= m <= hi) or not dev_short or dev_short not in f.name:
                    continue
                rp = f.resolve()
                if rp == exclude or str(rp) in _CLAIMED_REPORTS:
                    continue
                out.append((m, f))
        except Exception:
            return []
        out.sort()
        return [f for _m, f in out]

    started = _parse_dt(task.get("started_at"))
    ended = _parse_dt(task.get("ended_at")) or datetime.now()
    dev_short = (task.get("device") or "").replace(":", "_").replace(".", "_")

    raw = task.get("_report_path")
    if raw:
        safe = _safe(Path(raw))
        if safe is not None and safe.exists():
            group = [safe] + _siblings(started, ended, dev_short, safe)
            for f in group:
                _CLAIMED_REPORTS.add(str(f.resolve()))
            return group, "stdout_line"

    if task.get("script") not in REPORT_WRITING_SCRIPTS:
        return [], None

    hits = _siblings(started, ended, dev_short, Path("\0"))
    if not hits:
        return [], None
    chosen = hits[-1]          # newest first
    ordered = [chosen] + [f for f in hits if f != chosen]
    for f in ordered:
        _CLAIMED_REPORTS.add(str(f.resolve()))
    return ordered, "mtime_scan"


def _companion_files(report: Path) -> list[Path]:
    """Data files a run wrote next to its report.

    Same directory, same stem, a different extension - perf_monitor v2 writes
    `perf_<dev>_<ts>.json` plus `.samples.csv`, `.events.csv` and `.html`. Pairing
    is by exact stem prefix rather than by mtime or name substring so that a run
    can never pick up a neighbour's data when two runs land in the same second.
    Returns [] on any error: this feeds archiving, which must never raise.
    """
    out: list[Path] = []
    try:
        prefix = report.stem + "."
        for f in report.parent.iterdir():
            if (f.is_file() and f.suffix.lower() in (".csv", ".html")
                    and f.name.startswith(prefix)):
                out.append(f)
    except Exception:
        return []
    return sorted(out)


def _parse_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _public_task(t: dict[str, Any]) -> dict[str, Any]:
    """Strip private keys (those starting with _) for API response."""
    return {k: v for k, v in t.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    device: str
    script: str       # script filename, e.g. "ir_remote.py"
    params: dict[str, Any] | None = None
    # Serial capture opt-in (v2.6.0). All defaulted so an older frontend that
    # does not send them still runs exactly as before.
    serial_capture: bool = False
    serial_port: str | None = None
    # Explicit override of the "script owns the port" arbitration: hold the
    # serial port for the whole run even if the script wants it too.
    serial_force: bool = False
    # Logcat opt-out (v2.7.5). Defaults True so an older frontend that does not
    # send it keeps exactly today's behaviour (every task captures logcat).
    logcat_capture: bool = True


class SequenceRequest(BaseModel):
    content: str       # full ini file content to write


# ---------------------------------------------------------------------------
# Routes - health & meta
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": "2.11.2", "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/server/status")
async def api_server_status():
    """Server-side info, useful for the UI to show PID/uptime/task counts."""
    running = [t for t in TASKS.values() if t["status"] == "running"]
    interrupting = [t for t in TASKS.values() if t["status"] == "interrupting"]
    return {
        "pid": _SERVER_PID,
        "started_at": datetime.fromtimestamp(_SERVER_STARTED_AT).isoformat(timespec="seconds"),
        "uptime_sec": int(time.time() - _SERVER_STARTED_AT),
        "running_tasks": len(running),
        "interrupting_tasks": len(interrupting),
        "total_tasks": len(TASKS),
        "scripts_count": len(_list_scripts()),
    }


@app.post("/api/server/shutdown")
async def api_server_shutdown():
    """Gracefully stop all running script processes, then shut down the server."""
    interrupted = 0
    stopping = []
    for t in list(TASKS.values()):
        if t["status"] == "running":
            t["status"] = "interrupting"
            try:
                proc = t["_proc"]
                if os.name == "nt":
                    proc.send_signal(getattr(subprocess, "CTRL_BREAK_EVENT", 1))
                else:
                    proc.terminate()
                interrupted += 1
            except Exception:
                pass
        if t.get("_cap_logcat") or t.get("_cap_serial"):
            stopping.append(_stop_captures(t["task_id"], "server_shutdown"))

    # Tear the capture channels down before the hard exit, otherwise the logcat
    # clients outlive the server and have to be reaped on next startup. Bounded
    # so a stuck port cannot delay the shutdown response indefinitely.
    if stopping:
        try:
            await asyncio.wait_for(asyncio.gather(*stopping, return_exceptions=True), 2.0)
        except Exception:
            pass

    # Archive before the hard exit: os._exit bypasses _stream_logs entirely, so
    # this is the last chance to move these tasks' files out of logs/. Pure file
    # I/O, and _archive_task swallows its own failures - it cannot block exit.
    for t in list(TASKS.values()):
        try:
            _archive_task(t["task_id"], "server_shutdown")
        except Exception:
            pass

    # Schedule a hard exit after the response is flushed.
    loop = asyncio.get_running_loop()
    loop.call_later(0.8, lambda: os._exit(0))

    return {
        "ok": True,
        "interrupting": interrupted,
        "message": f"已请求中断 {interrupted} 个任务,服务将在约 1 秒后关闭。",
    }


@app.get("/")
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        raise HTTPException(404, "index.html not found")
    return FileResponse(idx)


# ---------------------------------------------------------------------------
# Routes - devices
# ---------------------------------------------------------------------------
@app.get("/api/devices")
async def api_devices():
    return {"devices": _list_adb_devices()}


@app.get("/api/serial/ports")
async def api_serial_ports():
    """COM ports available for the per-run serial capture opt-in.

    Mirrors battery_inout_stress.py's `_com_port_choices`, deliberately
    reimplemented here rather than imported - importing a script would execute
    it. Degrades to an empty list (never raises) when pyserial is missing.
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return {"ports": [], "available": False}
    try:
        ports = [p.device for p in list_ports.comports()]
    except Exception:
        return {"ports": [], "available": False}
    return {"ports": ports, "available": True}


# ---------------------------------------------------------------------------
# Routes - scripts
# ---------------------------------------------------------------------------
@app.get("/api/scripts")
async def api_scripts():
    return {"scripts": _list_scripts()}


# Params schema cache: key = (script name, mtime), so editing a script
# invalidates its cached entry automatically.
_PARAMS_CACHE: dict[tuple[str, float], dict] = {}


@app.get("/api/scripts/{name}/params")
async def api_script_params(name: str):
    """Return the frontend-configurable param schema declared by a script.

    Scripts that declare params support a `--dump-params` flag which prints
    {"fields": [...]} (list of {name, label, type, default, ...}) and exits.
    Scripts without it return an empty fields list.
    """
    script_path = (SCRIPTS_DIR / name).resolve()
    if SCRIPTS_DIR.resolve() not in script_path.parents:
        raise HTTPException(400, "invalid script path")
    if not script_path.exists() or script_path.suffix != ".py":
        raise HTTPException(404, f"script not found: {name}")

    key = (name, script_path.stat().st_mtime)
    cached = _PARAMS_CACHE.get(key)
    if cached is not None:
        return cached

    fields: list = []
    try:
        r = subprocess.run(
            [sys.executable, "-u", str(script_path), "--dump-params"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            cwd=str(ROOT),
        )
        out = (r.stdout or "").strip()
        if out:
            data = json.loads(out)
            if isinstance(data, dict) and isinstance(data.get("fields"), list):
                fields = data["fields"]
    except Exception:
        fields = []

    result = {"script": name, "fields": fields}
    _PARAMS_CACHE[key] = result
    return result


@app.get("/api/sequences")
async def api_list_sequences():
    """List all .ini sequence files in ir_sequences/.

    Returns lightweight metadata only (no content) for the picker modal.
    """
    items: list[dict[str, Any]] = []
    if IR_SEQUENCES_DIR.exists():
        for f in sorted(IR_SEQUENCES_DIR.glob("*.ini")):
            stat = f.stat()
            items.append({
                "name": f.stem,
                "filename": f.name,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            })
    return {"sequences": items}


def _resolve_seq_path(name: str) -> Path:
    """Resolve a sequence name to its .ini path, with traversal protection."""
    # Only allow simple filenames like "default" or "aging_12h"
    if "/" in name or "\\" in name or ".." in name or not name:
        raise HTTPException(400, "invalid sequence name")
    path = (IR_SEQUENCES_DIR / f"{name}.ini").resolve()
    if IR_SEQUENCES_DIR.resolve() not in path.parents:
        raise HTTPException(400, "invalid sequence path")
    return path


@app.get("/api/sequences/{name}")
async def api_get_sequence(name: str):
    """Read an IR sequence .ini file. name is the file stem (e.g. 'default')."""
    path = _resolve_seq_path(name)
    if not path.exists():
        raise HTTPException(404, f"sequence not found: {name}.ini")
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}")
    return {"name": name, "content": content}


@app.put("/api/sequences/{name}")
async def api_save_sequence(name: str, body: SequenceRequest):
    """Write content to ir_sequences/{name}.ini. Overwrites existing file."""
    path = _resolve_seq_path(name)
    try:
        path.write_text(body.content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"write failed: {e}")
    return {"ok": True, "name": name, "path": str(path)}


@app.delete("/api/sequences/{name}")
async def api_delete_sequence(name: str):
    """Delete an IR sequence .ini file. Refuses to delete 'default'."""
    if name == "default":
        raise HTTPException(400, "不能删除默认序列 default")
    path = _resolve_seq_path(name)
    if not path.exists():
        raise HTTPException(404, f"sequence not found: {name}.ini")
    try:
        path.unlink()
    except Exception as e:
        raise HTTPException(500, f"delete failed: {e}")
    return {"ok": True, "deleted": name}


class SequenceRequest(BaseModel):
    content: str       # full ini file content to write


class SequenceCreateRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^[A-Za-z0-9_\-]{1,64}$", v):
            raise ValueError("name must be 1-64 chars, letters/digits/_/- only")
        if v.lower() in {"con", "prn", "aux", "nul", "com1", "lpt1"}:
            raise ValueError("reserved name")
        return v


@app.post("/api/sequences")
async def api_create_sequence(body: SequenceCreateRequest):
    """Create a new .ini sequence file with a minimal template.

    The user is expected to edit the file (via filesystem or a future
    in-app editor) to add actual steps. The platform only handles file
    management + selection, not content editing.
    """
    path = _resolve_seq_path(body.name)
    if path.exists():
        raise HTTPException(409, f"sequence already exists: {body.name}.ini")
    template = (
        "[sequence]\n"
        "# Unified format (5 fields, no name):\n"
        "# <index>-<code>-<kind>-<delay_ms>-<count>\n"
        "# <kind> = \"Short\" or \"LongXXXX\" (long-press duration in ms)\n"
        "\n"
        "steps =\n"
        "    1-KEY_HOME-Short-1000-1\n"
    )
    try:
        path.write_text(template, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"create failed: {e}")
    return {"ok": True, "name": body.name, "path": str(path)}


# ---------------------------------------------------------------------------
# Routes - tasks
# ---------------------------------------------------------------------------
@app.post("/api/run")
async def api_run(req: RunRequest):
    device = req.device.strip()
    if not device:
        raise HTTPException(400, "device is required")

    # Reject if this device already has a running or interrupting task.
    # Prevents two scripts from sending conflicting ADB commands concurrently.
    for existing in TASKS.values():
        if (existing["device"] == device
                and existing["status"] in ("running", "interrupting")):
            raise HTTPException(
                409,
                f"设备 {device} 上有正在运行的任务({existing['script']}),"
                f"请先停止或等待完成"
            )

    script_path = (SCRIPTS_DIR / req.script).resolve()
    # Prevent path traversal: script must be under SCRIPTS_DIR
    if SCRIPTS_DIR.resolve() not in script_path.parents and script_path != SCRIPTS_DIR:
        raise HTTPException(400, "invalid script path")
    if not script_path.exists() or script_path.suffix != ".py":
        raise HTTPException(404, f"script not found: {req.script}")

    task_id = uuid.uuid4().hex
    started_at = datetime.now().isoformat(timespec="seconds")
    log_stem = make_log_stem(task_id, device, req.script, started_at)
    log_file = LOGS_DIR / f"{log_stem}{LOG_SUFFIX['stdout']}"
    log_file.touch()

    # Build argv. --device always passed; --params as JSON if provided.
    # `-u` forces unbuffered stdout so logs stream in real-time instead of
    # being held in Python's pipe buffer until the script exits.
    argv = [
        sys.executable,
        "-u",
        str(script_path),
        "--device", device,
    ]
    if req.params:
        argv += ["--params", json.dumps(req.params, ensure_ascii=False)]

    creationflags = 0
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP so we can terminate cleanly
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    # Text mode MUST name its encoding. Bare `text=True` picks
    # locale.getpreferredencoding() - GBK on a Chinese Windows - while
    # server_window.ps1 hands the child PYTHONIOENCODING=utf-8. The two ends
    # then disagree, and a single non-ASCII byte in the script's stdout makes
    # readline() raise UnicodeDecodeError. Pin BOTH ends to UTF-8 here, and
    # `errors="replace"` so no inbound byte sequence can raise at all.
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=str(ROOT),
        creationflags=creationflags,
        env=child_env,
    )

    # Serial capture arbitration. The script wins by default: a script that
    # declares a `serial_port` param needs the port for its own polling, and
    # holding it here would make that polling fail silently while the run
    # continues - the primary data would go flat with no obvious cause. The
    # trade is explicit instead: the frontend disables the control and the
    # server reports `skipped` with a reason. serial_force overrides.
    serial_status = "off"
    serial_detail = ""
    serial_port = (req.serial_port or "").strip()
    if req.serial_capture:
        if _script_owns_serial(req.script) and not req.serial_force:
            serial_status = "skipped"
            serial_detail = "script_owns_port"
        else:
            serial_status = "starting"

    # Logcat opt-out (v2.7.5). The default (capture everything) is right for
    # stress scripts, where the device-side crash trail is the whole point. It is
    # wrong for a monitor that runs for hours: an 8-hour run writes ~1GB, nobody
    # reads a 1GB logcat, and the extra `adb logcat` stream competes with the very
    # measurements we are taking. Same per-run control shape as the serial opt-in.
    logcat_status = "starting" if req.logcat_capture else "off"

    TASKS[task_id] = {
        "task_id": task_id,
        "device": device,
        "script": req.script,
        "params": req.params or {},
        "status": "running",
        "started_at": started_at,
        "ended_at": None,
        "exit_code": None,
        "log_file": str(log_file),
        "log_stem": log_stem,
        # Exact per-channel filenames, so the UI never reconstructs a path.
        # Static for the life of the task, so it is safe inside a render key.
        "log_files": {s: f"{log_stem}{LOG_SUFFIX[s]}" for s in CAPTURE_SOURCES},
        "captures": {},
        "_proc": proc,
        "_cap_logcat": _new_capture(active=(logcat_status == "starting"),
                                    status=logcat_status),
        "_cap_serial": _new_capture(
            active=(serial_status == "starting"),
            status=serial_status,
            extra={"detail": serial_detail, "port": serial_port},
        ),
        "_reader_task": None,
        "_cap_logcat_task": None,
        "_cap_serial_task": None,
    }
    WS_CLIENTS[task_id] = set()

    task = TASKS[task_id]
    task["captures"] = _public_captures(task)
    task["_reader_task"] = asyncio.create_task(_stream_logs(task_id))

    if LOGCAT_ENABLED and logcat_status == "starting":
        task["_cap_logcat_task"] = asyncio.create_task(_capture_logcat(task_id))
    if serial_status == "starting":
        task["_cap_serial_task"] = asyncio.create_task(
            _capture_serial(task_id, serial_port))

    return {"task_id": task_id, **_public_task(task)}


@app.post("/api/stop/{task_id}")
async def api_stop(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if t["status"] not in ("running",):
        return {"ok": True, "status": t["status"], "noop": True}
    t["status"] = "interrupting"
    proc = t["_proc"]
    # Deliberately NOT stopping the capture channels here: the device-side
    # shutdown/teardown logs right after an interrupt are the most valuable part
    # of the run. _stream_logs stops them when the script actually exits; the
    # force-stop path covers a script that ignores CTRL_BREAK.
    try:
        if os.name == "nt":
            proc.send_signal(getattr(subprocess, "CTRL_BREAK_EVENT", 1))
        else:
            proc.terminate()
    except Exception:
        pass
    return {"ok": True, "status": "interrupting"}


@app.post("/api/tasks/force-stop-by-device/{device_serial}")
async def api_force_stop_by_device(device_serial: str):
    """Hard-kill (SIGKILL) all running/interrupting tasks for a device.

    Use when device is temporarily offline and the subprocess is not responding
    to CTRL_BREAK. Log files are preserved.
    """
    killed = 0
    for t in list(TASKS.values()):
        if t["device"] != device_serial:
            continue
        if t["status"] not in ("running", "interrupting"):
            continue
        try:
            t["_proc"].kill()
            killed += 1
        except Exception:
            pass

        # This endpoint is the task's FINISHER, and it must CLAIM that job here,
        # synchronously, before the first await below.
        #
        # Why: _stream_logs has been parked in proc.wait() and wakes up the
        # instant the child dies. If it wins the race it finalizes the task
        # itself - with the status this exit code earns (-9 => "failed") - and
        # stops the captures on its way out. This coroutine then resumed and
        # used to write "interrupting" on top, i.e. a NON-terminal status over
        # an already-decided one, and by then _stream_logs had already returned:
        # nothing was left to ever advance it. The task sat non-terminal
        # forever, and the /api/run guard that refuses a device with a
        # running-or-interrupting task blocked that device until the server was
        # restarted. (_stream_logs now stands down when it sees this flag.)
        #
        # A killed task is "interrupted", not "failed" - the operator asked for
        # this - so decide it here and let _stream_logs drop out.
        t["_finalized"] = "force_stop"
        t["status"] = "interrupted"
        t["ended_at"] = datetime.now().isoformat(timespec="seconds")
        t["exit_code"] = -9
        # The subprocess was SIGKILLed, so there is no EOF to wait for -
        # stop the capture channels here rather than in _stream_logs.
        await _stop_captures(t["task_id"], "force_stop")
        # Archive here too: a SIGKILLed script never prints its report line and
        # may be stuck, so waiting for _stream_logs is not guaranteed.
        await asyncio.get_running_loop().run_in_executor(
            None, _archive_task, t["task_id"], "force_stop")
        await _broadcast(t["task_id"], {
            "type": "end",
            "status": "interrupted",
            "exit_code": -9,
            "ended_at": t["ended_at"],
            "reason": "force_stop",
            "archive": t.get("archive"),
        }, source=None)
        await _broadcast_archive_line(t["task_id"])
    return {"ok": True, "killed": killed}


@app.post("/api/tasks/force-cleanup")
async def api_force_cleanup():
    """Hard-kill ALL subprocesses, clear TASKS. For hard recovery."""
    killed = 0
    for t in list(TASKS.values()):
        try:
            t["_proc"].kill()
            killed += 1
        except Exception:
            pass
        # Stop captures BEFORE TASKS.clear(): the capture tasks look their task
        # up by id, so clearing first would strand them with no way to stop.
        try:
            await _stop_captures(t["task_id"], "force_cleanup")
        except Exception:
            pass
        # Archive before TASKS.clear() wipes the dict we read the metadata from.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, _archive_task, t["task_id"], "force_cleanup")
        except Exception:
            pass
        rt = t.get("_reader_task")
        if rt is not None and not rt.done():
            rt.cancel()
    # Close WS connections
    for tid in list(WS_CLIENTS.keys()):
        for ws in list(WS_CLIENTS[tid]):
            WS_SUBS.pop(id(ws), None)
            try:
                await ws.close()
            except Exception:
                pass
        WS_CLIENTS.pop(tid, None)
    TASKS.clear()
    return {"ok": True, "killed": killed}


@app.post("/api/adb/reconnect")
async def api_adb_reconnect():
    """adb kill-server + adb start-server. Manual recovery only."""
    try:
        subprocess.run(["adb", "kill-server"], capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        r = subprocess.run(["adb", "start-server"], capture_output=True, text=True,
                           timeout=10, encoding="utf-8", errors="replace")
        ok = r.returncode == 0
        return {"ok": ok, "stderr": r.stderr.strip() if not ok else ""}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/tasks")
async def api_tasks():
    return {"tasks": [_public_task(t) for t in TASKS.values()]}


@app.get("/api/tasks/{task_id}")
async def api_task_detail(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return _public_task(t)


def _read_tail(path: Path, max_bytes: int) -> tuple[list[str], int, bool]:
    """Read at most the last `max_bytes` of a file, as a list of lines.

    Reading a capped 256MB logcat file whole and JSON-serializing it would spike
    memory hard, so the tail is read in bytes and the partial first line dropped.
    Returns (lines, total_bytes, truncated).
    """
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()          # drop the partial first line
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines(), size, size > max_bytes


@app.get("/api/tasks/{task_id}/log")
async def api_task_log(task_id: str, source: str = "stdout"):
    """Return a capture channel's log content. Defaults to stdout."""
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if source not in CAPTURE_SOURCES:
        raise HTTPException(400, f"unknown source: {source}")
    # Path is always derived server-side; never accept one from the client.
    p = _log_path(t, source)
    if not p.exists():
        return {"lines": [], "source": source, "bytes": 0, "truncated": False}
    lines, size, truncated = _read_tail(p, REPLAY_TAIL_BYTES)
    return {"lines": lines, "source": source, "bytes": size, "truncated": truncated}


class ArtifactUpload(BaseModel):
    name: str
    data: str          # data-URL ("data:image/png;base64,...") or bare base64


def _archive_dir_for(task_id: str) -> Path | None:
    """The archive folder of a task, or None if it has not been archived.

    task_id comes from a URL path, so it is shape-checked before it is ever used
    to build a path - same guard as everywhere else in this file.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        return None
    t = TASKS.get(task_id)
    arch = (t or {}).get("archive")
    if not arch:
        return None
    d = _archive_dir(arch).resolve()
    # Belt and braces: module + dir both came from our own tables, but re-check
    # containment so a malformed entry can never escape archive/.
    if ARCHIVE_DIR.resolve() not in d.parents:
        return None
    return d if d.is_dir() else None


@app.get("/api/tasks/{task_id}/report")
async def api_task_report(task_id: str):
    """Serve a run's archived HTML report inline, in a new browser tab.

    The frontend entry point is the 「报告」 button on a task card. archive/ is
    not statically mounted, so this route is the only way to open one.

    One run can hold more than one report - app_launch_stress writes one per
    app - so this serves the first in name order. The button's title says how
    many there are, and the rest are reachable by opening the archive folder.
    """
    dest = _archive_dir_for(task_id)
    if dest is None:
        raise HTTPException(404, "task not archived (or unknown task)")
    htmls = sorted(
        p for p in dest.iterdir()
        if p.is_file() and p.suffix.lower() == ".html")
    if not htmls:
        raise HTTPException(404, "this run archived no html report")
    return FileResponse(htmls[0], media_type="text/html")


@app.post("/api/tasks/{task_id}/archive/artifact")
async def api_archive_artifact(task_id: str, body: ArtifactUpload):
    """Receive a browser-rendered artifact (chart PNG / perf CSV) for the archive.

    The server has no chart renderer (ECharts lives only in the browser), so the
    client renders with the same code path it uses for manual export and posts
    the result here. Best-effort by design: if no browser is open when the task
    ends, the archive simply has no chart.png - summary.json says so.
    """
    dest = _archive_dir_for(task_id)
    if dest is None:
        raise HTTPException(404, "task not archived (or unknown task)")
    if body.name not in ARCHIVE_UPLOADS:
        raise HTTPException(400, f"unknown artifact: {body.name}")

    payload = body.data
    if payload.startswith("data:"):
        head, _, payload = payload.partition(",")
        if "base64" not in head:
            raise HTTPException(400, "only base64 data-URLs are accepted")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        raise HTTPException(400, "invalid base64 payload")
    if not raw:
        raise HTTPException(400, "empty payload")
    if len(raw) > ARCHIVE_UPLOAD_MAX_BYTES:
        raise HTTPException(400, f"payload too large (max {ARCHIVE_UPLOAD_MAX_BYTES} bytes)")

    try:
        (dest / body.name).write_bytes(raw)
    except Exception as e:
        raise HTTPException(500, f"write failed: {e}")

    # Update the manifest so the archive is self-describing without the UI.
    t = TASKS.get(task_id) or {}
    summary = t.get("_archive_summary")
    if summary is not None:
        summary.setdefault("artifacts", {})[body.name] = {"bytes": len(raw)}
        try:
            (dest / ARCHIVE_SUMMARY_NAME).write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    arch = t.get("archive")
    if arch and body.name not in arch.get("files", []):
        arch["files"] = sorted(set(arch.get("files", [])) | {body.name})
        arch["bytes"] = arch.get("bytes", 0) + len(raw)
    return {"ok": True, "name": body.name, "bytes": len(raw)}


@app.post("/api/tasks/{task_id}/archive/reveal")
async def api_archive_reveal(task_id: str):
    """Open the task's archive folder in the OS file manager.

    Safe here specifically because this platform is local-only, single-user and
    has no auth - the process and the person clicking are the same machine. There
    is no client-supplied path: the folder comes from our own task record.
    """
    dest = _archive_dir_for(task_id)
    if dest is None:
        raise HTTPException(404, "task not archived (or unknown task)")
    try:
        if os.name == "nt":
            os.startfile(str(dest))          # noqa: S606 - local single-user tool
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(dest)])
        else:
            subprocess.Popen(["xdg-open", str(dest)])
    except Exception as e:
        raise HTTPException(500, f"could not open folder: {e}")
    return {"ok": True, "path": str(dest)}


@app.post("/api/archive/reveal")
async def api_archive_reveal_root():
    """Open the archive root in the OS file manager. No client input at all."""
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(ARCHIVE_DIR))     # noqa: S606 - local single-user tool
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ARCHIVE_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(ARCHIVE_DIR)])
    except Exception as e:
        raise HTTPException(500, f"could not open folder: {e}")
    return {"ok": True, "path": str(ARCHIVE_DIR)}


@app.get("/api/archive/stats")
async def api_archive_stats():
    """Size and count of the archive tree, plus a per-module breakdown.

    Layout is archive/<module>/<run>/, so a run is a directory whose PARENT is a
    module folder - counting only top-level dirs would count 9 modules and call
    it a day.
    """
    def _weigh(d: Path) -> int:
        n = 0
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    n += f.stat().st_size
            except OSError:
                continue
        return n

    count = 0
    total = 0
    modules: dict[str, dict[str, int]] = {}
    try:
        for entry in ARCHIVE_DIR.iterdir():
            if not entry.is_dir():
                continue
            # A directory holding summary.json IS a run; otherwise it is a
            # module folder of runs. This keeps the totals honest for a run
            # archived by the pre-module build, which sits directly under
            # archive/ - reporting "1 run, 6 KB" while 1.7 MB is on disk would
            # be exactly the kind of quiet lie this feature exists to prevent.
            if (entry / ARCHIVE_SUMMARY_NAME).is_file():
                runs = [entry]
                label = "(root)"
            else:
                runs = [d for d in entry.iterdir() if d.is_dir()]
                label = entry.name
            if not runs:
                continue
            m_bytes = sum(_weigh(r) for r in runs)
            modules[label] = {"count": len(runs), "bytes": m_bytes}
            count += len(runs)
            total += m_bytes
    except Exception:
        pass
    return {"count": count, "bytes": total, "modules": modules,
            "dir": str(ARCHIVE_DIR)}


def _forget_task(task_id: str) -> None:
    """Drop a task's in-memory entry. Deliberately touches NOTHING on disk.

    v2.7.0 made archives permanent, so removing a task from the list must not
    destroy its archive - otherwise one slip of the mouse would silently delete
    the very thing "permanent retention" promises to keep. Anything that does
    need removing from logs/ (a task that ended but was never archived) is left
    for the user to clear by hand; the archive folder is the durable copy.

    Kept as a named function (rather than inlining `del TASKS[...]`) so the
    intent is greppable and so a future "also delete the archive" affordance has
    an obvious place to live.
    """
    TASKS.pop(task_id, None)


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str):
    """Remove a finished/failed/interrupted task from the list.

    The task's archive folder under archive/ is KEPT (v2.7.0). Refuses to delete
    running or interrupting tasks (caller should stop first).
    """
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if t["status"] in ("running", "interrupting"):
        raise HTTPException(400, "cannot delete running task; stop it first")

    # Close any lingering WS connections
    for ws in list(WS_CLIENTS.get(task_id, set())):
        WS_SUBS.pop(id(ws), None)
        try:
            await ws.close()
        except Exception:
            pass
    WS_CLIENTS.pop(task_id, None)

    _forget_task(task_id)
    return {"ok": True, "deleted": task_id, "archive": t.get("archive")}


@app.post("/api/tasks/cleanup")
async def api_cleanup_tasks():
    """Bulk-remove all finished/failed/interrupted tasks from the list.

    Archives under archive/ are KEPT (v2.7.0) - this only empties the UI list.
    """
    deleted = []
    for tid in list(TASKS.keys()):
        t = TASKS[tid]
        if t["status"] in ("finished", "failed", "interrupted"):
            for ws in list(WS_CLIENTS.get(tid, set())):
                WS_SUBS.pop(id(ws), None)
                try:
                    await ws.close()
                except Exception:
                    pass
            WS_CLIENTS.pop(tid, None)
            _forget_task(tid)
            deleted.append(tid)
    return {"ok": True, "deleted": len(deleted), "ids": deleted}


# ---------------------------------------------------------------------------
# Routes - IR sequences
# ---------------------------------------------------------------------------
def _resolve_seq_path(name: str) -> Path:
    """Resolve a sequence name to its .ini path, with traversal protection."""
    # Only allow simple filenames like "default" or "aging_12h"
    if "/" in name or "\\" in name or ".." in name or not name:
        raise HTTPException(400, "invalid sequence name")
    path = (IR_SEQUENCES_DIR / f"{name}.ini").resolve()
    if IR_SEQUENCES_DIR.resolve() not in path.parents:
        raise HTTPException(400, "invalid sequence path")
    return path


@app.get("/api/sequences/{name}")
async def api_get_sequence(name: str):
    """Read an IR sequence .ini file. name is the file stem (e.g. 'default')."""
    path = _resolve_seq_path(name)
    if not path.exists():
        raise HTTPException(404, f"sequence not found: {name}.ini")
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}")
    return {"name": name, "content": content}


@app.put("/api/sequences/{name}")
async def api_save_sequence(name: str, body: SequenceRequest):
    """Write content to ir_sequences/{name}.ini. Overwrites existing file."""
    path = _resolve_seq_path(name)
    try:
        path.write_text(body.content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"write failed: {e}")
    return {"ok": True, "name": name, "path": str(path)}


# ---------------------------------------------------------------------------
# Routes - WebSocket log stream
# ---------------------------------------------------------------------------
@app.websocket("/ws/logs/{task_id}")
async def ws_logs(ws: WebSocket, task_id: str, sources: str = "stdout"):
    await ws.accept()
    t = TASKS.get(task_id)
    if not t:
        await ws.send_text(json.dumps({"type": "error", "message": "task not found"}))
        await ws.close()
        return

    # Unknown tokens are ignored rather than rejected - a typo in the query
    # string should not cut the stream. Default is stdout, so the pre-v2.6.0
    # frontend keeps exactly its old behaviour (plus a "source" key per frame).
    requested = {s.strip() for s in sources.split(",") if s.strip()}
    requested = {s for s in requested if s in CAPTURE_SOURCES} or {"stdout"}

    WS_CLIENTS.setdefault(task_id, set()).add(ws)
    WS_SUBS[id(ws)] = requested

    # Send current state immediately (captures included, so a reconnect learns
    # which channels exist and what state they are in)
    await ws.send_text(json.dumps({
        "type": "state",
        "status": t["status"],
        "started_at": t["started_at"],
        "ended_at": t["ended_at"],
        "exit_code": t["exit_code"],
        "captures": t.get("captures") or _public_captures(t),
    }, ensure_ascii=False))

    # Replay each subscribed channel from its own file so reconnects see
    # history. Capped by lines for stdout (the client batches DOM updates, so
    # tens of thousands of lines render smoothly) and by bytes for everything
    # else - a logcat file can be hundreds of MB, and read_text() on it would
    # spike memory and block the loop. Fixed channel order keeps the console
    # deterministic across reconnects.
    MAX_REPLAY_LINES = 50000
    for src in CAPTURE_SOURCES:
        if src not in requested:
            continue
        p = _log_path(t, src)
        if not p.exists():
            continue
        try:
            lines, total_bytes, byte_truncated = _read_tail(p, REPLAY_TAIL_BYTES)
        except OSError:
            continue
        total = len(lines)
        start = max(0, total - MAX_REPLAY_LINES)
        if total > MAX_REPLAY_LINES or byte_truncated:
            await ws.send_text(json.dumps({
                "type": "replay_meta",
                "source": src,
                "total_lines": total,
                "shown_lines": total - start,
                "max_replay_lines": MAX_REPLAY_LINES,
                "truncated": byte_truncated,
            }, ensure_ascii=False))
        for line in lines[start:]:
            await ws.send_text(json.dumps(
                {"type": "log", "line": line, "source": src}, ensure_ascii=False))

    try:
        # Keep connection open. Client messages are currently a no-op; this
        # loop is the seam where a future tab bar sends {"type":"subscribe"} to
        # change channels without tearing the socket down.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        WS_CLIENTS.get(task_id, set()).discard(ws)
        WS_SUBS.pop(id(ws), None)


# ---------------------------------------------------------------------------
# Static files (mount last so it doesn't shadow API routes)
# ---------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, log_level="info")