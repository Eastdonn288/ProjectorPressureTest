#!/usr/bin/env python3
"""PPTP IR runner — 自包含的红外压测脚本(单文件)。

平台调用方式:
    python scripts/ir_runner.py --device <serial>
        (可选) --params '{"sequence": "ir_sequences/default.ini"}'

也可以独立 CLI 调用:
    python scripts/ir_runner.py --device 7B9B...                 # 默认序列 + 无限循环
    python scripts/ir_runner.py --device 7B9B... --loops 3       # 跑 3 圈
    python scripts/ir_runner.py --device 7B9B... --step 5        # 从第 5 步开始
    python scripts/ir_runner.py --list                           # 只列步骤
    python scripts/ir_runner.py --dry --device 7B9B...           # dry run(只打印不发)

设计要点:
- 自包含:不依赖 keyevent.txt / tools/ir/ 任何外部文件,所有按键码硬编码在 TYPE_NUM_MAP / CODE_NUM_MAP
- 短按:`send_key_code` = send_key_down + send_key_up
- 长按:`send_key_down` → 等待 duration_ms → `send_key_up`(正确实现,非"循环发短按")
- 序列格式(5 字段,无 name):`idx-code-kind-delay_ms-count`
  - `kind=Short`:短按
  - `kind=LongXXXX`:长按,XXXX 是按住时长(ms);仅 `Long` 缺省 1500ms
  - `count`:重复次数(每次之间 delay_ms)
- 默认按 IR 设备路径 `/dev/input/event1` 发送 sendevent,可通过 CLI 参数 / 环境变量 / 改顶部常量覆盖
- 两种按键命名(ini 里都可用,按名字自动分发):
  - `KEY_*`(如 KEY_HOME):走 sendevent(linux input code,直接写 /dev/input/eventN,需 userdebug/root)
  - `KEYCODE_*`(如 KEYCODE_IP / KEYCODE_2D3D):走上层 `adb shell input keyevent`
    (Android keycode 名称,user 版无需 userdebug;名称由设备端解析为数值)

依赖:仅 Python 3.10+ 标准库。
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============ 路径常量 ============
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DEFAULT_SEQUENCE = ROOT / "ir_sequences" / "default.ini"


# ============ 默认 IR event 设备路径 ============
# 当前设备的 IR 输入是 /dev/input/event1(从旧 keyevent.txt 推断);
# 覆盖方式(优先级从高到低):
#   1. CLI --device-event-path
#   2. 环境变量 IR_EVENT_PATH
#   3. 改这里
DEFAULT_EVENT_PATH = "/dev/input/event1"


def resolve_event_path(cli_arg: Optional[str]) -> str:
    if cli_arg:
        return cli_arg
    return os.environ.get("IR_EVENT_PATH", DEFAULT_EVENT_PATH)


# ====================================================================
# IRRemote — ADB sendevent 发送器(自包含,无外部数据文件)
# ====================================================================
class IRRemote:
    """ADB sendevent IR sender. Self-contained — no keyevent.txt.

    Implements:
    - send_key_down(code):  send EV_KEY value=1 + EV_SYN value=0
    - send_key_up(code):    send EV_KEY value=0 + EV_SYN value=0
    - short_press(code):    send_key_down + send_key_up (no sleep)
    - long_press(code, ms): send_key_down + sleep + send_key_up

    Long press is correctly implemented as a held key state, NOT a
    rapid burst of short presses (the old ir_runner bug).
    """

    TYPE_NUM_MAP = {
        "EV_SYN": 0,
        "EV_KEY": 1,
        "EV_REL": 2,
        "EV_ABS": 3,
        "EV_MSC": 4,
    }
    # 设备按键码(从旧 keyevent.txt 提取 — 25 个按键,含 2026-07-20 新增的 KEY_MEMO)
    CODE_NUM_MAP = {
        "SYN_REPORT": 0,
        "MSC_SCAN": 4,
        "KEY_ENTER": 28,
        "KEY_BACK": 158,
        "KEY_UP": 103,
        "KEY_DOWN": 108,
        "KEY_LEFT": 105,
        "KEY_RIGHT": 106,
        "KEY_HOME": 102,
        "KEY_MENU": 139,
        "KEY_OK": 352,
        "KEY_TV2": 378,
        "KEY_VCR": 379,
        "KEY_VCR2": 380,
        "KEY_CHANNELUP": 402,
        "KEY_CHANNELDOWN": 403,
        "KEY_PAGEUP": 104,
        "KEY_KP1": 79,
        "KEY_POWER": 116,
        "KEY_MEMO": 396,                # 0x18c — 新增(2026-07-20)
        "KEY_BOOKMARKS": 156,
        "KEY_ASSISTANT": 583,
        "KEY_CALENDAR": 397,
        "KEY_VOLUMEUP": 115,
        "KEY_VOLUMEDOWN": 114,
        "KEY_MUTE": 113,
        "KEY_AB": 406,
    }

    # 上层注入按键 A:厂商遥控按键(Android keycode 名)—— user 版无需 userdebug 可用。
    # key = ini 里写的名字(KEYCODE_* 形式,唯一标识);value = 注入目标 Android keycode 名称,
    # 由设备端自行解析为数值,故不需硬编码数字(数值在不同 Android 版本间稳定,但写名称最稳)。
    # 来源:厂商固件 keymap(2026-08-17 用户提供);中文名见 ir_sequences/KEY_REFERENCE.md 第二张表。
    ANDROID_KEYCODE_MAP = {
        "KEYCODE_2D3D": "KEYCODE_TV_INPUT_HDMI_1",
        "KEYCODE_BI": "KEYCODE_TV_INPUT_HDMI_3",
        "KEYCODE_PIC": "KEYCODE_TV_INPUT_HDMI_4",
        "KEYCODE_ADC": "KEYCODE_TV_NETWORK",
        "KEYCODE_COLOR": "KEYCODE_TV_ANTENNA_CABLE",
        "KEYCODE_REST1": "KEYCODE_BUTTON_2",
        "KEYCODE_REST2": "KEYCODE_TV_TERRESTRIAL_ANALOG",
        "KEYCODE_REST3": "KEYCODE_BUTTON_5",
        "KEYCODE_ATV": "KEYCODE_ZENKAKU_HANKAKU",
        "KEYCODE_DTV": "KEYCODE_BUTTON_15",
        "KEYCODE_FAC": "KEYCODE_HENKAN",
        "KEYCODE_WRITE_MAC": "KEYCODE_BUTTON_12",
        "KEYCODE_AV": "KEYCODE_BUTTON_9",
        "KEYCODE_YPBPR": "KEYCODE_TV_TIMER_PROGRAMMING",
        "KEYCODE_HDMI": "KEYCODE_NAVIGATE_PREVIOUS",
        "KEYCODE_VGA": "KEYCODE_NAVIGATE_NEXT",
        "KEYCODE_USB": "KEYCODE_NAVIGATE_IN",
        "KEYCODE_MAC": "KEYCODE_NAVIGATE_OUT",
        "KEYCODE_DDC": "KEYCODE_TV_RADIO_SERVICE",
        "KEYCODE_IP": "KEYCODE_BUTTON_14",
        "KEYCODE_MENU": "KEYCODE_BUTTON_13",
        "KEYCODE_HOME": "KEYCODE_F3",
        "KEYCODE_FOCUS_KEYSTONE": "KEYCODE_TV_SATELLITE_SERVICE",
    }

    # 上层注入按键 B:安卓原生标准按键 —— 名称即 keycode 名(恒等透传),
    # 注册在此仅为让 `supported keys` 打印与 KEY_REFERENCE.md 第三张表有据可查。
    # 数值见 KEY_REFERENCE.md 第三张表(仅参考;ini 里一律写名称)。
    NATIVE_ANDROID_KEYCODES = (
        # 系统/导航
        "KEYCODE_HOME", "KEYCODE_BACK", "KEYCODE_MENU", "KEYCODE_ENTER",
        "KEYCODE_ESCAPE", "KEYCODE_APP_SWITCH", "KEYCODE_SETTINGS",
        "KEYCODE_DPAD_UP", "KEYCODE_DPAD_DOWN", "KEYCODE_DPAD_LEFT",
        "KEYCODE_DPAD_RIGHT", "KEYCODE_DPAD_CENTER",
        # 电源/显示
        "KEYCODE_POWER", "KEYCODE_SLEEP", "KEYCODE_WAKEUP",
        "KEYCODE_BRIGHTNESS_UP", "KEYCODE_BRIGHTNESS_DOWN",
        # 音量
        "KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_DOWN", "KEYCODE_MUTE",
        # 媒体
        "KEYCODE_MEDIA_PLAY_PAUSE", "KEYCODE_MEDIA_PLAY", "KEYCODE_MEDIA_PAUSE",
        "KEYCODE_MEDIA_STOP", "KEYCODE_MEDIA_NEXT", "KEYCODE_MEDIA_PREVIOUS",
    )

    def __init__(self, event_path: str, use_su: bool = False):
        self.event_path = event_path
        self.use_su = use_su

    # ---------- low-level sendevent ----------
    def _build_down_commands(self, code: str) -> List[List[str]]:
        code_num = self.CODE_NUM_MAP.get(code)
        if code_num is None:
            raise KeyError(f"Unknown KEY_NAME: {code} (not in CODE_NUM_MAP)")
        return [
            ["shell", "sendevent", self.event_path,
             str(self.TYPE_NUM_MAP["EV_KEY"]), str(code_num), "1"],
            ["shell", "sendevent", self.event_path,
             str(self.TYPE_NUM_MAP["EV_SYN"]), str(self.CODE_NUM_MAP["SYN_REPORT"]), "0"],
        ]

    def _build_up_commands(self, code: str) -> List[List[str]]:
        code_num = self.CODE_NUM_MAP.get(code)
        if code_num is None:
            raise KeyError(f"Unknown KEY_NAME: {code} (not in CODE_NUM_MAP)")
        return [
            ["shell", "sendevent", self.event_path,
             str(self.TYPE_NUM_MAP["EV_KEY"]), str(code_num), "0"],
            ["shell", "sendevent", self.event_path,
             str(self.TYPE_NUM_MAP["EV_SYN"]), str(self.CODE_NUM_MAP["SYN_REPORT"]), "0"],
        ]

    def send_key_down(self, code: str, dry_run: bool = False, serial: Optional[str] = None) -> None:
        """Send ONLY the key-down event (value=1). Start of a long press."""
        commands = self._build_down_commands(code)
        self._adb_run_shell(commands, dry_run=dry_run, serial=serial)

    def send_key_up(self, code: str, dry_run: bool = False, serial: Optional[str] = None) -> None:
        """Send ONLY the key-up event (value=0). End of a long press."""
        commands = self._build_up_commands(code)
        self._adb_run_shell(commands, dry_run=dry_run, serial=serial)

    def short_press(self, code: str, dry_run: bool = False, serial: Optional[str] = None) -> None:
        """Send a short press: down + up (no sleep between)."""
        self._adb_run_shell(
            self._build_down_commands(code) + self._build_up_commands(code),
            dry_run=dry_run, serial=serial,
        )

    def long_press(self, code: str, duration_ms: int,
                   dry_run: bool = False, serial: Optional[str] = None) -> None:
        """Send a REAL long press: down → sleep(duration_ms) → up.

        CORRECT long-press: held key state (down then sleep then up).
        NOT a rapid burst of short presses (which was the old bug).
        """
        if code not in self.CODE_NUM_MAP:
            raise KeyError(f"Unknown KEY_NAME: {code} (not in CODE_NUM_MAP)")
        self.send_key_down(code, dry_run=dry_run, serial=serial)
        if dry_run:
            print(f"      [DRY] hold {duration_ms}ms")
        else:
            time.sleep(duration_ms / 1000.0)
        self.send_key_up(code, dry_run=dry_run, serial=serial)

    # ---------- adb shell exec with su fallback ----------
    def _run_with_su(self, commands, dry_run: bool, serial: Optional[str]) -> int:
        joined = " ; ".join(" ".join(c[1:]) if c and c[0] == "shell" else " ".join(c) for c in commands)
        cmd = ["adb"]
        if serial:
            cmd += ["-s", serial]
        cmd += ["shell", "su", "-c", joined]
        if dry_run:
            print("DRY RUN (su):", " ".join(cmd))
            return 0
        completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode == 0:
            return completed.returncode
        # Some devices don't support -c; fall back to piping into su.
        fallback_cmd = ["adb"]
        if serial:
            fallback_cmd += ["-s", serial]
        fallback_cmd += ["shell", "su"]
        if dry_run:
            print("DRY RUN (su fallback):", " ".join(fallback_cmd), "with stdin:", joined)
            return 0
        completed = subprocess.run(
            fallback_cmd, input=joined + "\nexit\n",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if completed.returncode == 0:
            return completed.returncode
        raise RuntimeError(f"ADB failed: {completed.stderr.strip() or completed.stdout.strip()}")

    def _adb_run_shell(self, commands, dry_run: bool = False, serial: Optional[str] = None) -> int:
        """Run one or more shell commands via adb, with automatic su fallback on Permission denied."""
        if self.use_su:
            return self._run_with_su(commands, dry_run=dry_run, serial=serial)
        for command in commands:
            cmd = ["adb"]
            if serial:
                cmd += ["-s", serial]
            cmd += command
            if dry_run:
                print("DRY RUN:", " ".join(cmd))
                continue
            completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if completed.returncode == 0:
                continue
            stderr = (completed.stderr or completed.stdout or "").strip()
            if "permission denied" in stderr.lower():
                try:
                    return self._run_with_su(commands, dry_run=dry_run, serial=serial)
                except Exception as exc:
                    raise RuntimeError(f"Device-side IR permission failure: {stderr}. Fallback su also failed: {exc}") from exc
            raise RuntimeError(f"ADB failed: {stderr}")
        return 0

    # ---------- 按键名 → 注入方式 分发 ----------
    @classmethod
    def resolve_key(cls, name: str):
        """名字 → (method, target)。

        - `KEYCODE_*` → ("input", Android keycode 名):走 `adb shell input keyevent`,
          user 版无需 userdebug;未在 ANDROID_KEYCODE_MAP 内的 KEYCODE_* 原样透传
          (即可用任意标准 Android keycode 名,如 KEYCODE_POWER)。
        - 其余 `KEY_*` → ("sendevent", linux input code):走 sendevent 直接写
          /dev/input/eventN(需 userdebug/root)。
        """
        if name.startswith("KEYCODE_"):
            return ("input", cls.ANDROID_KEYCODE_MAP.get(name, name))
        code = cls.CODE_NUM_MAP.get(name)
        if code is None:
            raise KeyError(f"Unknown key name: {name} (neither in CODE_NUM_MAP nor ANDROID_KEYCODE_MAP)")
        return ("sendevent", str(code))

    # ---------- 上层注入路径(adb shell input keyevent / keydown / keyup) ----------
    def input_keyevent(self, android_name: str, dry_run: bool = False, serial: Optional[str] = None) -> None:
        """Short press 上层注入:input keyevent 自带 down+up,一次完成。"""
        self._adb_run_shell([["shell", "input", "keyevent", android_name]], dry_run=dry_run, serial=serial)

    def input_long_press(self, android_name: str, duration_ms: int, dry_run: bool = False, serial: Optional[str] = None) -> None:
        """Long press 上层注入:input keydown → hold → input keyup,精确控制按住时长。
        注意按住时长需大于系统长按阈值(~500ms)才算真长按。"""
        self._adb_run_shell([["shell", "input", "keydown", android_name]], dry_run=dry_run, serial=serial)
        if dry_run:
            print(f"      [DRY] hold {duration_ms}ms (input keyevent)")
        else:
            time.sleep(duration_ms / 1000.0)
        self._adb_run_shell([["shell", "input", "keyup", android_name]], dry_run=dry_run, serial=serial)


# ====================================================================
# SequenceConfig — ir_sequences/*.ini 解析(5 字段格式)
# ====================================================================
@dataclass
class SequenceStep:
    index: int
    code: str
    action: str            # "Short" / "Long"
    delay_ms: int          # 每次重复之间的间隔
    count: int             # 重复次数
    long_duration_ms: int = 0   # 仅 Long 有效:按住时长(默认 1500)

    def __repr__(self) -> str:
        if self.action == "Long":
            return f"Step({self.index}: {self.code} Long({self.long_duration_ms}ms) delay={self.delay_ms}ms x{self.count})"
        return f"Step({self.index}: {self.code} Short delay={self.delay_ms}ms x{self.count})"


# 5 字段:<idx>-<code>-<Short|LongNNNN>-<delay_ms>-<count>
# LongNNNN 中的 NNNN 是按住时长(ms);仅 Long 缺省 1500
_STEP_RE = re.compile(r"^(\d+)-([A-Z0-9_]+)-(Short|Long(\d*))-(\d+)-(\d+)$")


class SequenceConfig:
    """Parse ir_sequence.ini into a list of SequenceStep.

    Format per line (5 fields, no name):
        <index>-<code>-<Short|LongXXXX>-<delay_ms>-<count>

    Examples:
        1-KEY_HOME-Short-2000-1
        2-KEY_VCR-Long3000-500-1     # Long 3000ms, count=1, delay between reps 500ms
        3-KEY_POWER-Long-1000-2       # Long default 1500ms, count=2
    """

    def __init__(self, path: Path):
        self.path = path
        self.steps: List[SequenceStep] = []
        self.reload()

    def reload(self) -> None:
        cfg = configparser.ConfigParser()
        read = cfg.read(str(self.path), encoding="utf-8")
        if not read:
            raise FileNotFoundError(f"sequence file not found or unreadable: {self.path}")
        if not cfg.has_section("sequence") or not cfg.has_option("sequence", "steps"):
            raise ValueError(f"missing [sequence] / steps in {self.path}")

        steps: List[SequenceStep] = []
        for raw in cfg.get("sequence", "steps").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = _STEP_RE.match(line)
            if not m:
                print(f"[warn] skip malformed step (need 5 fields: idx-code-Short|LongXXXX-delay_ms-count): {line}")
                continue
            idx = int(m.group(1))
            code = m.group(2)
            action = "Long" if m.group(3).lower().startswith("long") else "Short"
            tail = m.group(4) or ""
            delay_ms = int(m.group(5))
            count = int(m.group(6))
            long_dur = int(tail) if tail else 1500  # 仅 "Long" 时缺省 1500ms
            steps.append(SequenceStep(
                index=idx, code=code, action=action,
                delay_ms=delay_ms, count=count,
                long_duration_ms=long_dur,
            ))
        self.steps = steps

    def __iter__(self):
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def __getitem__(self, i: int) -> SequenceStep:
        return self.steps[i]


# ====================================================================
# run_step / run_loop — 实际执行
# ====================================================================
def run_step(ir: IRRemote, step: SequenceStep, serial: str, dry_run: bool = False) -> None:
    """Execute one step N times (count). Each press has its own log line."""
    # 按名字自动分发:KEYCODE_* → input keyevent(user 版可用);KEY_* → sendevent(userdebug)
    try:
        method, target = IRRemote.resolve_key(step.code)
    except KeyError:
        sendevent_keys = ", ".join(sorted(k for k in ir.CODE_NUM_MAP if k.startswith("KEY_")))
        input_keys = ", ".join(sorted(ir.ANDROID_KEYCODE_MAP))
        raise RuntimeError(
            f"key '{step.code}' unknown. sendevent keys: {sendevent_keys} | input keyevent keys: {input_keys}"
        ) from None

    for n in range(1, step.count + 1):
        if step.action == "Long":
            dur = step.long_duration_ms
            if step.count > 1:
                print(f"  -> {step.code} Long({dur}ms) [{n}/{step.count}]")
            else:
                print(f"  -> {step.code} Long({dur}ms)")
            try:
                if method == "input":
                    ir.input_long_press(target, dur, dry_run=dry_run, serial=serial)
                else:
                    ir.long_press(step.code, dur, dry_run=dry_run, serial=serial)
            except Exception as e:
                raise RuntimeError(f"step '{step.code}' failed: {e}") from e
        else:  # Short
            if step.count > 1:
                print(f"  -> {step.code} [{n}/{step.count}]")
            else:
                print(f"  -> {step.code}")
            try:
                if method == "input":
                    ir.input_keyevent(target, dry_run=dry_run, serial=serial)
                else:
                    ir.short_press(step.code, dry_run=dry_run, serial=serial)
            except Exception as e:
                raise RuntimeError(f"step '{step.code}' failed: {e}") from e
        if n < step.count:
            time.sleep(step.delay_ms / 1000.0)


def run_loop(
    ir: IRRemote,
    steps: List[SequenceStep],
    serial: str,
    *,
    loops: Optional[int] = None,
    start_index: int = 1,
    dry_run: bool = False,
) -> int:
    """Run all steps in a loop. loops=None means infinite (Ctrl+C / platform stop to end).
    Returns exit code (0 success, 130 interrupted, 4 runtime error).
    """
    loop_count = 0
    return_code = 0
    try:
        while True:
            loop_count += 1
            if loops is not None and loop_count > loops:
                print(f"\n[done] completed {loops} loop(s)")
                break
            print(f"\n========== loop {loop_count}{'' if loops is None else f'/{loops}'} ==========")
            # Only consider steps whose index >= start_index (CLI --step N skips
            # the earlier ones). Inter-loop delay uses the LAST EXECUTED step's
            # delay_ms — previously was hardcoded 1.0 which ignored ini config
            # (bugfix 2026-07-20).
            executed = [s for s in steps if s.index >= start_index]
            for i, step in enumerate(executed, 1):
                print(f"=== step {step.index}/{len(steps)}: {step.code} ({step.action}) ===")
                run_step(ir, step, serial, dry_run=dry_run)
                if i < len(executed):
                    time.sleep(step.delay_ms / 1000.0)
            # Inter-loop pause: respect the LAST executed step's delay_ms.
            # Fallback to 1.0s if sequence is empty (no steps ran).
            if loops is None or loop_count < loops:
                inter_loop_ms = executed[-1].delay_ms if executed else 1000
                if dry_run:
                    print(f"[DRY] inter-loop sleep {inter_loop_ms}ms")
                else:
                    time.sleep(inter_loop_ms / 1000.0)
    except KeyboardInterrupt:
        print(f"\n[interrupted] stopped after {loop_count} loop(s)")
        return_code = 130
    except RuntimeError as e:
        print(f"[err] {e}")
        return_code = 4
    return return_code


# ====================================================================
# Platform runner — PPTP 调这个(通过 subprocess)
# ====================================================================
def run_as_task(args) -> int:
    """Entry point when invoked by PPTP platform (--device + --params)."""
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError:
        print(f"[warn] invalid --params JSON, using defaults: {args.params}")
        params = {}

    seq_content = params.get("sequence_content")
    _is_temp_seq = False
    if seq_content:
        IR_SEQUENCES_DIR = ROOT / "ir_sequences"
        IR_SEQUENCES_DIR.mkdir(parents=True, exist_ok=True)
        seq_path = IR_SEQUENCES_DIR / f"_seq_{uuid.uuid4().hex}.ini"
        seq_path.write_text(seq_content, encoding="utf-8")
        _is_temp_seq = True
    else:
        seq_arg = params.get("sequence")
        seq_path = Path(seq_arg) if seq_arg else DEFAULT_SEQUENCE
        if not seq_path.is_absolute():
            seq_path = ROOT / seq_path

    return_code = 0
    try:
        if not seq_path.exists():
            print(f"[err] sequence file not found: {seq_path}")
            return 2

        steps = SequenceConfig(seq_path).steps
        event_path = resolve_event_path(getattr(args, "device_event_path", None))
        ir = IRRemote(event_path=event_path)

        print(f"[runner] device    = {args.device}")
        print(f"[runner] sequence  = {seq_path}")
        print(f"[runner] event_path= {event_path}")
        print(f"[runner] loaded {len(steps)} steps")
        print()
        sendevent_keys = sorted(k for k in ir.CODE_NUM_MAP if k.startswith("KEY_"))
        vendor_keys = sorted(ir.ANDROID_KEYCODE_MAP.keys())
        native_keys = sorted(ir.NATIVE_ANDROID_KEYCODES)
        print(f"[runner] supported keys: {len(sendevent_keys)} sendevent (KEY_*, userdebug) -> {', '.join(sendevent_keys)}")
        print(f"[runner] input keyevent (KEYCODE_*, works on user build): {len(vendor_keys)} vendor remote -> {', '.join(vendor_keys)}")
        print(f"[runner] input keyevent native: {len(native_keys)} -> {', '.join(native_keys)}")
        print("[runner] mode: infinite loop (Ctrl+C or platform stop to end)")
        print()

        return_code = run_loop(ir, steps, args.device)
    finally:
        if _is_temp_seq:
            try:
                seq_path.unlink()
            except Exception:
                pass
    return return_code


# ====================================================================
# CLI — 独立调用(平台外调试)
# ====================================================================
def run_as_cli(args) -> int:
    """Entry point when invoked directly from command line."""
    try:
        steps = SequenceConfig(args.ini).steps
    except Exception as e:
        print(f"Error loading sequence: {e}", file=sys.stderr)
        return 1

    if args.list:
        print(f"Sequence file: {args.ini}")
        print(f"Total steps   : {len(steps)}")
        print("-" * 60)
        for s in steps:
            print(f"  {s}")
        return 0

    if not steps:
        print("No steps defined.")
        return 0

    event_path = resolve_event_path(args.device_event_path)
    ir = IRRemote(event_path=event_path, use_su=args.use_su)

    print(f"Device    : {args.device or '(auto)'}")
    print(f"Event path: {event_path}")
    print(f"Sequence  : {args.ini}")
    print(f"Mode      : {'DRY RUN' if args.dry else 'LIVE'}{' + su' if args.use_su else ''}")
    print(f"Loops     : {args.loops if args.loops else 'infinite'}")
    print(f"Start step: {args.step}")
    print("-" * 60)

    if not args.device and not args.dry:
        print("[warn] no --device given, commands will fail at ADB layer")

    return run_loop(ir, steps, args.device or "", loops=args.loops, start_index=args.step, dry_run=args.dry)


def main() -> int:
    p = argparse.ArgumentParser(description="PPTP IR runner (single-file, correct long-press).")
    p.add_argument("--device", "-s", help="ADB device serial (required when running via PPTP)")
    p.add_argument("--params", default="{}", help="JSON params (PPTP convention; may contain 'sequence')")
    p.add_argument("--device-event-path", help=f"IR event device path (default: {DEFAULT_EVENT_PATH}; env: IR_EVENT_PATH)")
    # CLI-only flags
    p.add_argument("--ini", help="(CLI) sequence .ini path (overrides --params)")
    p.add_argument("--dry", action="store_true", help="(CLI) dry run — print commands only")
    p.add_argument("--loops", type=int, default=None, help="(CLI) loop count (default: infinite)")
    p.add_argument("--step", type=int, default=1, help="(CLI) 1-based step to start from")
    p.add_argument("--list", action="store_true", help="(CLI) list steps without running")
    p.add_argument("--use-su", action="store_true", help="(CLI) force su for sendevent")

    args = p.parse_args()

    # Platform mode: PPTP invokes with --device + (optional) --params
    # CLI mode: user invokes with --ini (no --params needed)
    if args.ini is not None or args.list:
        return run_as_cli(args)
    return run_as_task(args)


if __name__ == "__main__":
    sys.exit(main())
