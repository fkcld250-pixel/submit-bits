from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def format_led_ascii(value: int | str | dict[str, Any] | None) -> str:
    led_value = _coerce_led_value(value)
    if led_value is None:
        return ""
    lines: list[str] = []
    for row in range(3, -1, -1):
        row_value = (led_value >> (row * 8)) & 0xFF
        cells = "".join("x" if row_value & (1 << bit) else "." for bit in range(7, -1, -1))
        lines.append(f"[ {cells} ]")
    return "\n".join(lines)


def _coerce_led_value(value: int | str | dict[str, Any] | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    if isinstance(value, dict):
        return _coerce_led_value(value.get("hex") or value.get("bits"))

    text = str(value).strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            return _coerce_led_value(decoded)
    if set(text) <= {"0", "1"} and len(text) == 32:
        return int(text[::-1], 2) & 0xFFFFFFFF
    return int(text, 0) & 0xFFFFFFFF


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a 32-bit LED value as a 4x8 ASCII board.")
    parser.add_argument("led", nargs="?", help="LED value, for example 0x01221c08.")
    args = parser.parse_args(argv)

    value = args.led
    if value is None and not sys.stdin.isatty():
        value = sys.stdin.read().strip()
    if value is None:
        parser.error("LED value is required")

    try:
        board = format_led_ascii(value)
    except ValueError as exc:
        parser.error(str(exc))
    if board:
        print(board)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
