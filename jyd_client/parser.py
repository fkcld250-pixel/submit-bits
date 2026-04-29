from __future__ import annotations

import re


SEGMENT_MAP = {
    "1111110": "0",
    "0110000": "1",
    "1101101": "2",
    "1111001": "3",
    "0110011": "4",
    "1011011": "5",
    "1011111": "6",
    "1110000": "7",
    "1111111": "8",
    "1111011": "9",
}

_left_digits: str | None = None
_right_digits: str | None = None


def parse_display(raw: str) -> str:
    bits = _extract_bits(raw)
    if not bits:
        return ""

    if len(bits) % 8 == 0:
        return _parse_groups([bits[i : i + 8] for i in range(0, len(bits), 8)])
    if len(bits) % 7 == 0:
        return "".join(SEGMENT_MAP.get(bits[i : i + 7], "?") for i in range(0, len(bits), 7))
    return ""


def parse_full_display(raw: str) -> str:
    parsed = parse_display(raw)
    if parsed:
        return parsed

    numbers = re.findall(r"\d+", raw)
    if numbers:
        return numbers[-1]
    return raw.strip()


def parse_full_display_bytes(data: bytes) -> str:
    return parse_signal_bytes(data)["display"]


def parse_signal_bytes(data: bytes) -> dict[str, object]:
    global _left_digits, _right_digits
    _left_digits = None
    _right_digits = None
    parsed = ""
    led_bits = ""
    for frame in _iter_display_frames(data):
        current = _parse_display_frame(frame)
        if current:
            parsed = current
        led_bits = signal_dict_from_frame(frame)["LightDisplays"]
    return {
        "display": parsed,
        "led": _parse_led_bits(led_bits),
    }


def signal_dict_from_frame(frame: bytes) -> dict[str, str]:
    frame = frame[:18]
    return {
        "Code": "1",
        "LightDisplays": _bytes_to_csv_bits(frame[14:18]),
        "DigitalDisplays": _bytes_to_csv_bits(frame[0:5]),
        "DigitalBtns": _bytes_to_csv_bits(frame[5:6]),
        "DigitalTaggles": _bytes_to_csv_bits(frame[6:14]),
    }


def _extract_bits(raw: str) -> str:
    candidates = re.findall(r"[01]{7,}", raw)
    if not candidates:
        hex_bytes = re.findall(r"\b(?:0x)?[0-9a-fA-F]{2}\b", raw)
        if hex_bytes:
            return "".join(f"{int(x, 16):08b}" for x in hex_bytes)
        return ""
    return max(candidates, key=len)


def _parse_groups(groups: list[str]) -> str:
    chars: list[str] = []
    for group in groups:
        digit = SEGMENT_MAP.get(group[:7], "?")
        dot = "." if group[7] == "1" else ""
        chars.append(digit + dot)
    return "".join(chars)


def _iter_display_frames(data: bytes):
    for i in range(0, len(data) - 17, 18):
        yield data[i : i + 18]


def _parse_display_frame(frame: bytes) -> str:
    global _left_digits, _right_digits

    signals = signal_dict_from_frame(frame)
    display_bits = signals["DigitalDisplays"]
    display_val = _parse_csv_display(display_bits)[::-1]
    bits = display_bits.split(",")
    if len(bits) < 10:
        return display_val

    cs_right = bits[8]
    cs_left = bits[9]
    if cs_left == "1":
        _left_digits = display_val
    elif cs_right == "1":
        _right_digits = display_val

    if _left_digits and _right_digits:
        combined = "".join(left + right for left, right in zip(_left_digits, _right_digits))
        _left_digits = None
        _right_digits = None
        return combined
    return display_val


def _bytes_to_csv_bits(data: bytes) -> str:
    return ",".join(bit for byte in data for bit in f"{byte:08b}"[::-1])


def _parse_csv_display(binary_str: str) -> str:
    bits = [x.strip() for x in binary_str.split(",") if x.strip() in {"0", "1"}]
    if len(bits) != 40:
        return ""
    result: list[str] = []
    for i in range(0, 40, 10):
        group = bits[i : i + 10]
        digit = SEGMENT_MAP.get("".join(group[:7]), "?")
        dot = "." if group[7] == "1" else ""
        result.append(digit + dot)
    return "".join(result)


def _parse_led_bits(binary_str: str) -> dict[str, object]:
    bits = [x.strip() for x in binary_str.split(",") if x.strip() in {"0", "1"}]
    if not bits:
        return {"bits": "", "hex": "", "active_indices": []}
    bit_string = "".join(bits)
    return {
        "bits": bit_string,
        "hex": _bits_to_hex(bit_string),
        "active_indices": [i for i, bit in enumerate(bits) if bit == "1"],
    }


def _bits_to_hex(bit_string: str) -> str:
    if not bit_string:
        return ""
    value = int(bit_string[::-1], 2)
    width = max(1, (len(bit_string) + 3) // 4)
    return f"0x{value:0{width}x}"
