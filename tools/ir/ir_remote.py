import re
import json
import time
import subprocess
import shlex
from typing import Dict, Any, Optional


class IRRemote:
    """Parse getevent-like mapping files and send ADB sendevent commands.

    Features:
    - Load mappings from a txt file (hot-reloadable)
    - Export mappings to JSON
    - Send short and long presses using `adb sendevent` commands
    """

    TYPE_NUM_MAP = {
        'EV_SYN': 0,
        'EV_KEY': 1,
        'EV_REL': 2,
        'EV_ABS': 3,
        'EV_MSC': 4,
    }

    CODE_NUM_MAP = {
        'SYN_REPORT': 0,
        'MSC_SCAN': 4,
        'KEY_ENTER': 28,
        'KEY_BACK': 158,
        'KEY_UP': 103,
        'KEY_DOWN': 108,
        'KEY_LEFT': 105,
        'KEY_RIGHT': 106,
        'KEY_HOME': 102,
        'KEY_MENU': 139,
        'KEY_OK': 352,
        'KEY_TV2': 378,
        'KEY_VCR': 379,
        'KEY_VCR2': 380,
        'KEY_CHANNELUP': 402,
        'KEY_CHANNELDOWN': 403,
        'KEY_PAGEUP': 104,
        'KEY_KP1': 79,
        'KEY_POWER': 116,
        'KEY_BOOKMARKS': 156,
        'KEY_ASSISTANT': 583,
        'KEY_CALENDAR': 397,
        'KEY_VOLUMEUP': 115,
        'KEY_VOLUMEDOWN': 114,
        'KEY_MUTE': 113,
        'KEY_AB': 406,
    }

    def __init__(self, mapping_txt: str, auto_reload: bool = True, use_su: bool = False):
        self.mapping_txt = mapping_txt
        self.auto_reload = auto_reload
        self.use_su = use_su
        self.mappings: Dict[str, Dict[str, Any]] = {}
        self.discovered_key_codes: Dict[str, Dict[str, int]] = {}
        self.default_device: Optional[str] = None
        self.reload()

    def reload(self):
        """(Re)load mappings from the txt file."""
        self.mappings = self._parse_txt(self.mapping_txt)
        self.default_device = self._find_default_device()

    def _find_default_device(self) -> Optional[str]:
        for mapping in self.mappings.values():
            device = mapping.get('device')
            if device:
                return device
        return None

    def _parse_txt(self, path: str) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        with open(path, 'r', encoding='utf-8') as f:
            lines = [l.rstrip('\n') for l in f]

        current_label: Optional[str] = None
        current_events: list[Dict[str, Any]] = []
        device_line_re = re.compile(
            r'(?P<device>/dev/input/event\d+):\s*(?P<type>EV_[A-Z]+)\s+(?P<code>[A-Z0-9_]+)\s+(?P<value>[0-9a-zA-Z_]+)'
        )

        def flush_block():
            nonlocal current_label, current_events
            if not current_events:
                return
            key = current_label or f"{current_events[0]['type_name']}_{current_events[0]['code_name']}_{current_events[0]['value']}"
            result[key.strip()] = {
                'device': current_events[0]['device'],
                'events': current_events,
            }
            current_label = None
            current_events = []

        for raw in lines:
            s = raw.strip()
            if not s:
                continue
            m = device_line_re.search(s)
            if m:
                if current_label is None and not current_events:
                    current_label = None
                device = m.group('device')
                type_name = m.group('type')
                code_name = m.group('code')
                value = m.group('value')
                current_events.append({
                    'device': device,
                    'type_name': type_name,
                    'code_name': code_name,
                    'value': value,
                })
            else:
                if current_events:
                    flush_block()
                current_label = s

        flush_block()
        return result

    def to_json(self, json_path: str):
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(self.mappings, f, indent=2, ensure_ascii=False)

    def _discover_device_key_codes(self, device: str) -> Dict[str, int]:
        try:
            completed = subprocess.run(
                ['adb', 'shell', f'getevent -pl {device}'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if completed.returncode != 0:
                return {}
            text = completed.stdout
            match = re.search(r'KEY\s+\(0001\):\s*(.*?)(?=\n\s+MSC\s+\(0004\):)', text, re.S)
            if not match:
                return {}
            tokens = re.findall(r'\S+', match.group(1))
            code = 1
            result: Dict[str, int] = {}
            for token in tokens:
                if token.startswith('KEY_') or token.startswith('BTN_'):
                    result[token] = code
                code += 1
            return result
        except Exception:
            return {}

    def _get_device_key_code(self, device: str, code_name: str) -> Optional[int]:
        self.discovered_key_codes.setdefault(device, self._discover_device_key_codes(device))
        return self.discovered_key_codes.get(device, {}).get(code_name)

    def _resolve_nums(self, type_name: str, code_name: str, device: Optional[str] = None):
        if type_name not in self.TYPE_NUM_MAP:
            raise ValueError(f"Unknown event type: {type_name}")
        type_num = self.TYPE_NUM_MAP[type_name]
        code_num = self.CODE_NUM_MAP.get(code_name)
        if code_num is None and type_name == 'EV_KEY':
            code_num = self._get_device_key_code(device or '', code_name)
        if code_num is None:
            try:
                code_num = int(code_name)
            except Exception:
                raise ValueError(f"Unknown code name: {code_name}")
        return type_num, code_num

    def _parse_key_code(self, code: Any) -> int:
        if isinstance(code, int):
            return code
        if isinstance(code, str):
            normalized = code.strip()
            if normalized.lower().startswith('0x'):
                return int(normalized, 16)
            if normalized.isdigit():
                return int(normalized)
            _, code_num = self._resolve_nums('EV_KEY', normalized, self.default_device)
            return code_num
        raise ValueError(f"Unsupported key code type: {code}")

    def send_key_code(self, code: Any, dry_run: bool = False, serial: Optional[str] = None, device: Optional[str] = None):
        device = device or self.default_device
        if not device:
            raise ValueError("No device available for sendevent")

        code_num = self._parse_key_code(code)
        commands = [
            ['shell', 'sendevent', device, str(self.TYPE_NUM_MAP['EV_KEY']), str(code_num), '1'],
            ['shell', 'sendevent', device, str(self.TYPE_NUM_MAP['EV_SYN']), str(self.CODE_NUM_MAP['SYN_REPORT']), '0'],
            ['shell', 'sendevent', device, str(self.TYPE_NUM_MAP['EV_KEY']), str(code_num), '0'],
            ['shell', 'sendevent', device, str(self.TYPE_NUM_MAP['EV_SYN']), str(self.CODE_NUM_MAP['SYN_REPORT']), '0'],
        ]
        self._adb_run_shell(commands, dry_run=dry_run, serial=serial)

    def _format_value(self, value: str) -> str:
        normalized = value.upper()
        if normalized in ('DOWN', 'PRESS'):
            return '1'
        if normalized in ('UP', 'RELEASE'):
            return '0'
        if normalized.lower().startswith('0x'):
            return normalized.lower()
        try:
            parsed = int(normalized, 16)
            return f'0x{parsed:x}'
        except Exception:
            return normalized

    def _run_with_su(self, commands, dry_run: bool = False, serial: Optional[str] = None):
        joined = ' ; '.join(' '.join(cmd[1:]) if cmd and cmd[0] == 'shell' else ' '.join(cmd) for cmd in commands)
        cmd = ['adb']
        if serial:
            cmd += ['-s', serial]
        # Try the most common su form first.
        cmd += ['shell', 'su', '-c', joined]
        if dry_run:
            print('DRY RUN (su):', ' '.join(cmd))
            return 0
        completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if completed.returncode == 0:
            return completed.returncode

        # Some devices do not support -c; fall back to piping into su.
        fallback_cmd = ['adb']
        if serial:
            fallback_cmd += ['-s', serial]
        fallback_cmd += ['shell', 'su']
        if dry_run:
            print('DRY RUN (su fallback):', ' '.join(fallback_cmd), 'with stdin:', joined)
            return 0
        completed = subprocess.run(
            fallback_cmd,
            input=joined + '\nexit\n',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode == 0:
            return completed.returncode
        raise RuntimeError(f"ADB failed: {completed.stderr.strip() or completed.stdout.strip()}")

    def _adb_run_shell(self, commands, dry_run: bool = False, serial: Optional[str] = None):
        """Run one or more shell commands via adb, with automatic su fallback on Permission denied."""
        if self.use_su:
            return self._run_with_su(commands, dry_run=dry_run, serial=serial)

        for command in commands:
            cmd = ['adb']
            if serial:
                cmd += ['-s', serial]
            cmd += command
            if dry_run:
                print('DRY RUN:', ' '.join(cmd))
                continue
            completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if completed.returncode == 0:
                continue
            stderr = (completed.stderr or completed.stdout or '').strip()
            if 'permission denied' in stderr.lower():
                try:
                    return self._run_with_su(commands, dry_run=dry_run, serial=serial)
                except Exception as exc:
                    raise RuntimeError(f"Device-side IR permission failure: {stderr}. Fallback su also failed: {exc}") from exc
            raise RuntimeError(f"ADB failed: {stderr}")
        return 0

    def _build_commands_for_events(self, events: list[Dict[str, Any]]) -> list[list[str]]:
        commands: list[list[str]] = []
        for event in events:
            type_num, code_num = self._resolve_nums(event['type_name'], event['code_name'], event.get('device'))
            value = self._format_value(event['value'])
            commands.append(['shell', 'sendevent', event['device'], str(type_num), str(code_num), value])

        if events and events[-1]['type_name'] != 'EV_SYN':
            commands.append([
                'shell',
                'sendevent',
                events[-1]['device'],
                str(self.TYPE_NUM_MAP['EV_SYN']),
                str(self.CODE_NUM_MAP['SYN_REPORT']),
                '0x0',
            ])
        return commands

    def _send_single(self, mapping: Dict[str, Any], dry_run: bool = False, serial: Optional[str] = None):
        commands = self._build_commands_for_events(mapping['events'])
        self._adb_run_shell(commands, dry_run=dry_run, serial=serial)

    def short_press(self, key: str, dry_run: bool = False, serial: Optional[str] = None):
        """Send a short press for `key`."""
        if self.auto_reload:
            self.reload()
        mapping = self.mappings.get(key)
        if mapping is None:
            raise KeyError(f"Key not found: {key}")

        self._send_single(mapping, dry_run=dry_run, serial=serial)

    def long_press(self, key: str, duration_ms: int = 1500, interval_ms: int = 200, dry_run: bool = False, serial: Optional[str] = None):
        """Send a long press by repeating the mapped event until duration elapses."""
        if self.auto_reload:
            self.reload()
        mapping = self.mappings.get(key)
        if mapping is None:
            raise KeyError(f"Key not found: {key}")

        end_time = time.time() + duration_ms / 1000.0
        while time.time() < end_time:
            self._send_single(mapping, dry_run=dry_run, serial=serial)
            time.sleep(interval_ms / 1000.0)


if __name__ == '__main__':
    import argparse
    from pathlib import Path

    default_mapping = Path(__file__).with_name('Keyevent.txt')

    p = argparse.ArgumentParser()
    p.add_argument('mapping', nargs='?', default=str(default_mapping), help='path to mapping txt file')
    p.add_argument('--to-json', help='export mappings to json path')
    p.add_argument('--dry', action='store_true', help='dry run (print commands)')
    args = p.parse_args()

    r = IRRemote(args.mapping)
    if args.to_json:
        r.to_json(args.to_json)
        print('Exported to', args.to_json)
    else:
        print('Loaded keys:')
        for k in r.mappings:
            print('-', k)
