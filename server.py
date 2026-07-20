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
import json
import os
import re
import subprocess
import sys
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
DATA_DIR = ROOT / "data"
TASKS_FILE = DATA_DIR / "tasks.json"
IR_SEQUENCES_DIR = ROOT / "ir_sequences"

for d in (SCRIPTS_DIR, STATIC_DIR, LOGS_DIR, DATA_DIR, IR_SEQUENCES_DIR):
    d.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# In-memory task state
# ---------------------------------------------------------------------------
# task_id -> {
#   task_id, device, script, params,
#   status: "running" | "interrupting" | "finished" | "failed" | "interrupted",
#   started_at, ended_at, exit_code,
#   log_file: Path,
#   _proc: Popen, _reader_task: asyncio.Task
# }
TASKS: dict[str, dict[str, Any]] = {}

# task_id -> set of WebSocket connections (for live log streaming)
WS_CLIENTS: dict[str, set[WebSocket]] = {}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="PPTP", version="2.0")

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
def _list_scripts() -> list[dict[str, str]]:
    """Return .py files under scripts/."""
    items: list[dict[str, str]] = []
    for p in sorted(SCRIPTS_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        items.append({"name": p.stem, "filename": p.name, "path": str(p)})
    return items


# ---------------------------------------------------------------------------
# Helpers - task log streaming
# ---------------------------------------------------------------------------
async def _stream_logs(task_id: str) -> None:
    """Read proc.stdout line-by-line, broadcast to WS clients, write to file."""
    task = TASKS.get(task_id)
    if not task:
        return
    proc = task["_proc"]
    log_file = task["log_file"]

    loop = asyncio.get_running_loop()
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            while True:
                # read_line is blocking; run in default executor
                line = await loop.run_in_executor(None, proc.stdout.readline)
                if not line:
                    break
                line = line.rstrip("\n")
                f.write(line + "\n")
                f.flush()
                await _broadcast(task_id, {"type": "log", "line": line})

        proc.wait()
        exit_code = proc.returncode
        task["exit_code"] = exit_code

        if task["status"] == "interrupting":
            task["status"] = "interrupted"
        elif exit_code == 0:
            task["status"] = "finished"
        else:
            task["status"] = "failed"

        task["ended_at"] = datetime.now().isoformat(timespec="seconds")
        await _broadcast(task_id, {
            "type": "end",
            "status": task["status"],
            "exit_code": exit_code,
            "ended_at": task["ended_at"],
        })
    except Exception as e:
        await _broadcast(task_id, {"type": "error", "message": str(e)})


async def _broadcast(task_id: str, message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False)
    dead: list[WebSocket] = []
    for ws in WS_CLIENTS.get(task_id, set()):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        WS_CLIENTS[task_id].discard(ws)


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


class SequenceRequest(BaseModel):
    content: str       # full ini file content to write


# ---------------------------------------------------------------------------
# Routes - health & meta
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": "2.0", "time": datetime.now().isoformat(timespec="seconds")}


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


# ---------------------------------------------------------------------------
# Routes - scripts
# ---------------------------------------------------------------------------
@app.get("/api/scripts")
async def api_scripts():
    return {"scripts": _list_scripts()}


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
    log_file = LOGS_DIR / f"{task_id}.log"
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

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(ROOT),
        creationflags=creationflags,
    )

    TASKS[task_id] = {
        "task_id": task_id,
        "device": device,
        "script": req.script,
        "params": req.params or {},
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "ended_at": None,
        "exit_code": None,
        "log_file": str(log_file),
        "_proc": proc,
    }
    WS_CLIENTS[task_id] = set()

    # Start log streaming
    asyncio.create_task(_stream_logs(task_id))

    return {"task_id": task_id, **_public_task(TASKS[task_id])}


@app.post("/api/stop/{task_id}")
async def api_stop(task_id: str):
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if t["status"] not in ("running",):
        return {"ok": True, "status": t["status"], "noop": True}
    t["status"] = "interrupting"
    proc = t["_proc"]
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
        # Mark as "interrupting" so _stream_logs will transition it to "interrupted"
        # when it sees the subprocess exit. Avoid race condition where _stream_logs
        # overwrites our "interrupted" with "failed".
        t["status"] = "interrupting"
        t["ended_at"] = datetime.now().isoformat(timespec="seconds")
        t["exit_code"] = -9
        await _broadcast(t["task_id"], {
            "type": "end",
            "status": "interrupted",
            "exit_code": -9,
            "ended_at": t["ended_at"],
            "reason": "force_stop",
        })
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
    # Close WS connections
    for tid in list(WS_CLIENTS.keys()):
        for ws in list(WS_CLIENTS[tid]):
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
        r = subprocess.run(["adb", "start-server"], capture_output=True, text=True, timeout=10)
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


@app.get("/api/tasks/{task_id}/log")
async def api_task_log(task_id: str):
    """Return full log file content."""
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    p = Path(t["log_file"])
    if not p.exists():
        return {"lines": []}
    text = p.read_text(encoding="utf-8", errors="replace")
    return {"lines": text.splitlines()}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: str):
    """Delete a finished/failed/interrupted task and its log file.

    Refuses to delete running or interrupting tasks (caller should stop first).
    """
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if t["status"] in ("running", "interrupting"):
        raise HTTPException(400, "cannot delete running task; stop it first")

    # Close any lingering WS connections
    for ws in list(WS_CLIENTS.get(task_id, set())):
        try:
            await ws.close()
        except Exception:
            pass
    WS_CLIENTS.pop(task_id, None)

    # Delete log file
    log_path = Path(t["log_file"])
    if log_path.exists():
        try:
            log_path.unlink()
        except Exception:
            pass

    del TASKS[task_id]
    return {"ok": True, "deleted": task_id}


@app.post("/api/tasks/cleanup")
async def api_cleanup_tasks():
    """Bulk-delete all finished/failed/interrupted tasks and their log files."""
    deleted = []
    for tid in list(TASKS.keys()):
        t = TASKS[tid]
        if t["status"] in ("finished", "failed", "interrupted"):
            for ws in list(WS_CLIENTS.get(tid, set())):
                try:
                    await ws.close()
                except Exception:
                    pass
            WS_CLIENTS.pop(tid, None)
            log_path = Path(t["log_file"])
            if log_path.exists():
                try:
                    log_path.unlink()
                except Exception:
                    pass
            del TASKS[tid]
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
async def ws_logs(ws: WebSocket, task_id: str):
    await ws.accept()
    t = TASKS.get(task_id)
    if not t:
        await ws.send_text(json.dumps({"type": "error", "message": "task not found"}))
        await ws.close()
        return

    WS_CLIENTS.setdefault(task_id, set()).add(ws)

    # Send current state immediately
    await ws.send_text(json.dumps({
        "type": "state",
        "status": t["status"],
        "started_at": t["started_at"],
        "ended_at": t["ended_at"],
        "exit_code": t["exit_code"],
    }, ensure_ascii=False))

    # Replay existing log file so reconnects see history.
    # Safety cap to prevent pathological cases (very large log files);
    # the client batches DOM updates so even tens of thousands of lines
    # render smoothly.
    MAX_REPLAY_LINES = 50000
    p = Path(t["log_file"])
    if p.exists():
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(0, total - MAX_REPLAY_LINES)
        if total > MAX_REPLAY_LINES:
            await ws.send_text(json.dumps({
                "type": "replay_meta",
                "total_lines": total,
                "shown_lines": total - start,
                "max_replay_lines": MAX_REPLAY_LINES,
            }, ensure_ascii=False))
        for line in lines[start:]:
            await ws.send_text(json.dumps({"type": "log", "line": line}, ensure_ascii=False))

    try:
        # Keep connection open; messages from client are no-op
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        WS_CLIENTS.get(task_id, set()).discard(ws)


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