"""Generate simple KEY reference doc from tools/ir/keyevent.txt.

Output: ir_sequences/KEY_REFERENCE.md
Format: two-column table, sorted by KEY_NAME.

Usage:
    python tools/ir/generate_keyref.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from ir.ir_remote import IRRemote  # noqa: E402

KEYEVENT = ROOT / "tools" / "ir" / "keyevent.txt"
OUT = ROOT / "ir_sequences" / "KEY_REFERENCE.md"


def extract_pairs(remote: IRRemote) -> list[tuple[str, str]]:
    """Return [(chinese_name, KEY_NAME), ...] from IRRemote.mappings.

    Each entry in keyevent.txt contains both DOWN and UP events, so we
    dedupe to one KEY_NAME per label.
    """
    pairs: dict[str, str] = {}
    for label, mapping in remote.mappings.items():
        cn = label.replace("非工厂遥控器_", "")
        for event in mapping.get("events", []):
            if event.get("type_name") == "EV_KEY":
                kn = event.get("code_name")
                if kn and cn not in pairs:
                    pairs[cn] = kn
                break  # first EV_KEY event per label is enough
    return list(pairs.items())


def main() -> int:
    if not KEYEVENT.exists():
        print(f"[err] not found: {KEYEVENT}")
        return 1
    remote = IRRemote(mapping_txt=str(KEYEVENT))
    pairs = extract_pairs(remote)
    pairs.sort(key=lambda x: x[1])  # sort by KEY_NAME

    lines = [
        "# 按键对照表\n",
        "\n",
        f"> 自动生成自 `tools/ir/keyevent.txt`({len(pairs)} 个按键)  \n",
        "> 改 ini 时查这里:左边是按钮的中文描述,右边是 `ir_sequence.ini` 里要写的 KEY_NAME。\n",
        "\n",
        "| 中文名 | KEY_NAME |\n",
        "|--------|----------|\n",
    ]
    for cn, kn in pairs:
        lines.append(f"| {cn} | `{kn}` |\n")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(lines), encoding="utf-8")
    print(f"wrote {OUT} ({len(pairs)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())