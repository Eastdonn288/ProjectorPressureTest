"""
ir_runner.py - PPTP platform script for infrared remote simulation.

This is a thin wrapper that:
  1. Loads the IR sequence from an ini file (default: ir_sequences/default.ini)
  2. Uses IRRemote (from tools/ir/) to send ADB sendevent commands
  3. Streams each step's progress to stdout (visible in PPTP UI)
  4. Loops the whole sequence infinitely until stopped (Ctrl+C / platform stop)

Contract:
  --device <serial>     (required, injected by PPTP platform)
  --params <json>       (optional, can override sequence path)

Run standalone (for testing without PPTP):
    python scripts/ir_runner.py --device <serial>
    # Press Ctrl+C to stop.

Custom sequence:
    python scripts/ir_runner.py --device <serial> \
        --params '{"sequence": "ir_sequences/aging.ini"}'
"""
import argparse
import configparser
import json
import signal
import sys
import time
import uuid
from pathlib import Path

# Make CTRL_BREAK_EVENT (sent by PPTP platform's stop button on Windows)
# raise KeyboardInterrupt so the loop can exit cleanly with a log line.
# Without this, Python's default SIGBREAK handler kills the process abruptly
# and the user never sees "stopped after N loops".
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

# --- path bootstrap: make `tools/` importable ---
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from ir.ir_remote import IRRemote  # noqa: E402

# --- defaults (edit here if you want a different default sequence) ---
DEFAULT_SEQUENCE = ROOT / "ir_sequences" / "default.ini"
DEFAULT_KEYEVENT = ROOT / "tools" / "ir" / "keyevent.txt"


def build_keyname_index(remote: IRRemote) -> dict[str, dict]:
    """Build KEY_NAME -> mapping dict by inspecting IRRemote.mappings.

    IRRemote parses Keyevent.txt where each entry is keyed by a Chinese label
    (e.g. "非工厂遥控器_Home键"). But ir_sequence.ini references keys by their
    KEY_NAME (e.g. "KEY_HOME"). We build a secondary index here so the runner
    can look up events by KEY_NAME.
    """
    index: dict[str, dict] = {}
    for mapping in remote.mappings.values():
        for event in mapping.get("events", []):
            if event.get("type_name") == "EV_KEY":
                key_name = event.get("code_name")
                if key_name and key_name not in index:
                    index[key_name] = mapping
    return index


def load_sequence(path: Path) -> list[dict]:
    """Parse ir_sequence.ini into a list of step dicts.

    Format per line (5 fields, no name):
        <index>-<code>-<Short|LongXXXX>-<delay_ms>-<count>

    Example:
        1-KEY_HOME-Short-2000-1
        2-KEY_VCR-Long3000-500-1
    """
    cfg = configparser.ConfigParser()
    read_files = cfg.read(str(path), encoding="utf-8")
    if not read_files:
        raise FileNotFoundError(f"sequence file not found or unreadable: {path}")

    if not cfg.has_section("sequence") or not cfg.has_option("sequence", "steps"):
        raise ValueError(f"missing [sequence] / steps in {path}")

    steps = []
    for raw in cfg.get("sequence", "steps").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("-")
        if len(parts) < 5:
            print(f"[warn] skip malformed step (need 5 fields): {line}")
            continue
        idx, code, kind, delay_ms, count = parts[:5]
        kind_lower = kind.strip().lower()
        is_long = kind_lower.startswith("long")
        long_ms = 0
        if is_long:
            tail = kind_lower[4:]
            try:
                long_ms = int(tail)
            except ValueError:
                long_ms = 1500
        try:
            steps.append({
                "index": int(idx),
                "code": code.strip(),
                "long": is_long,
                "long_ms": long_ms,
                "delay_ms": int(delay_ms),
                "count": int(count),
            })
        except ValueError as e:
            print(f"[warn] skip bad step ({e}): {line}")
    return steps


def run_step(remote: IRRemote, keyname_index: dict, step: dict, serial: str) -> None:
    """Send one step N times (count). Each press has its own log line."""
    mapping = keyname_index.get(step["code"])
    if mapping is None:
        available = ", ".join(sorted(keyname_index.keys()))
        raise RuntimeError(
            f"key '{step['code']}' not in Keyevent.txt. Available: {available}"
        )
    for n in range(1, step["count"] + 1):
        if step["count"] > 1:
            print(f"  -> {step['code']} [{n}/{step['count']}]")
        else:
            print(f"  -> {step['code']}")
        try:
            if step["long"]:
                # Long press: loop inside IRRemote; simulate via repeated short_press with interval
                end_time = time.time() + step["long_ms"] / 1000.0
                while time.time() < end_time:
                    remote._send_single(mapping, serial=serial)
                    time.sleep(0.2)
            else:
                remote._send_single(mapping, serial=serial)
        except Exception as e:
            raise RuntimeError(f"step '{step['code']}' failed: {e}") from e
        if n < step["count"]:
            time.sleep(step["delay_ms"] / 1000.0)


def main() -> int:
    p = argparse.ArgumentParser(description="Run an IR sequence on a device")
    p.add_argument("--device", required=True, help="ADB device serial")
    p.add_argument("--params", default="{}", help="JSON params, may contain 'sequence' path")
    args = p.parse_args()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    # Resolve sequence path: temp content (from modal) > param path > default
    # Temp content is written to a file and deleted on exit (see finally).
    seq_content = params.get("sequence_content")
    _is_temp_seq = False
    if seq_content:
        IR_SEQUENCES_DIR.mkdir(parents=True, exist_ok=True)
        seq_path = IR_SEQUENCES_DIR / f"_seq_{uuid.uuid4().hex}.ini"
        seq_path.write_text(seq_content, encoding="utf-8")
        _is_temp_seq = True
    else:
        seq_arg = params.get("sequence")
        seq_path = Path(seq_arg) if seq_arg else DEFAULT_SEQUENCE
        if not seq_path.is_absolute():
            seq_path = ROOT / seq_path

    if not seq_path.exists():
        print(f"[err] sequence file not found: {seq_path}")
        return 2
    if not DEFAULT_KEYEVENT.exists():
        print(f"[err] keyevent file not found: {DEFAULT_KEYEVENT}")
        return 2

    print(f"[runner] device    = {args.device}")
    print(f"[runner] sequence  = {seq_path}{' (temp)' if _is_temp_seq else ''}")
    print(f"[runner] keyevent  = {DEFAULT_KEYEVENT}")

    try:
        steps = load_sequence(seq_path)
    except Exception as e:
        print(f"[err] failed to parse sequence: {e}")
        return 3
    print(f"[runner] loaded {len(steps)} steps")
    print()

    remote = IRRemote(mapping_txt=str(DEFAULT_KEYEVENT))
    keyname_index = build_keyname_index(remote)
    print(f"[runner] keyevent index: {len(keyname_index)} keys -> "
          f"{', '.join(sorted(keyname_index.keys()))}")
    print("[runner] mode: infinite loop (Ctrl+C or platform stop to end)")
    print()

    loop_count = 0
    try:
        while True:
            loop_count += 1
            print(f"\n========== loop {loop_count} ==========")
            for i, step in enumerate(steps, 1):
                print(f"=== step {i}/{len(steps)}: {step['code']} ===")
                run_step(remote, keyname_index, step, args.device)
                if i < len(steps):
                    time.sleep(step["delay_ms"] / 1000.0)
            # Small pause between loops - avoid hammering the device if
            # the last step's delay was short or zero.
            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"\n[interrupted] stopped after {loop_count} loop(s)")
        return_code = 130
    except RuntimeError as e:
        print(f"[err] {e}")
        return_code = 4
    finally:
        # Always remove temp sequence file (any exit path)
        if _is_temp_seq:
            try:
                seq_path.unlink()
            except Exception:
                pass

    # Unreachable: the while True loop only exits via exception
    return return_code


if __name__ == "__main__":
    sys.exit(main())