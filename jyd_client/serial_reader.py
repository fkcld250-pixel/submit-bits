from __future__ import annotations

import socket
import time
from collections.abc import Callable
from typing import Any

IAC = 0xFF
WILL = 0xFB
WONT = 0xFC
DO = 0xFD
DONT = 0xFE
SB = 0xFA
SE = 0xF0

OPT_BINARY = 0
OPT_SUPPRESS_GO_AHEAD = 3
OPT_COM_PORT = 44

SET_BAUDRATE = 1
SET_DATASIZE = 2
SET_PARITY = 3
SET_STOPSIZE = 4
SET_CONTROL = 5
PURGE_DATA = 12


def read_tcp_serial(
    host: str,
    port: int,
    total_timeout: int,
    idle_timeout: int,
    baud_rate: int = 9600,
    poll_byte: bytes = b"\x80",
    poll_interval: float = 0.701,
    max_payload_bytes: int = 0,
    stable_seconds: float = 0,
    stable_snapshot: Callable[[bytes], Any] | None = None,
    stable_sample_logger: Callable[[Any, float], None] | None = None,
) -> bytes:
    deadline = time.time() + total_timeout
    payload_chunks: list[bytes] = []
    last_data = time.time()
    next_poll = time.time()
    last_snapshot: Any = None
    last_stable_key: Any = None
    stable_since: float | None = None
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.settimeout(0.2)
        _send_rfc2217_line_settings(sock, baud_rate)
        while time.time() < deadline:
            now = time.time()
            if poll_byte and now >= next_poll:
                _send_serial_payload(sock, poll_byte)
                next_poll = now + poll_interval
            try:
                data = sock.recv(4096)
            except socket.timeout:
                now = time.time()
                if stable_seconds and stable_since is not None and now - stable_since >= stable_seconds:
                    break
                if payload_chunks and now - last_data >= idle_timeout and not stable_seconds:
                    break
                continue
            if not data:
                break
            payload = handle_telnet_and_extract_payload(sock, data)
            if payload:
                payload_chunks.append(payload)
                now = time.time()
                last_data = now
                if stable_snapshot is not None:
                    snapshot = stable_snapshot(b"".join(payload_chunks))
                    stable_key = _stable_key(snapshot)
                    if stable_key != last_stable_key:
                        last_snapshot = snapshot
                        last_stable_key = stable_key
                        stable_since = now if snapshot else None
                    elif snapshot and stable_since is None:
                        stable_since = now
                    if snapshot and stable_sample_logger is not None and _should_log_sample(snapshot):
                        stable_sample_logger(snapshot, 0 if stable_since is None else now - stable_since)
                    if stable_seconds and stable_since is not None and now - stable_since >= stable_seconds:
                        break
                if max_payload_bytes and sum(len(chunk) for chunk in payload_chunks) >= max_payload_bytes:
                    break
    return b"".join(payload_chunks)


def _stable_key(snapshot: Any) -> Any:
    if isinstance(snapshot, dict) and "_stable_key" in snapshot:
        return snapshot["_stable_key"]
    return snapshot


def _should_log_sample(snapshot: Any) -> bool:
    if isinstance(snapshot, dict):
        return bool(snapshot.get("_log_sample", True))
    return True


def decode_serial(data: bytes) -> str:
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.hex(" ")


def handle_telnet_and_extract_payload(sock: socket.socket, data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        byte = data[i]
        if byte != IAC:
            out.append(byte)
            i += 1
            continue

        if i + 1 >= len(data):
            break
        command = data[i + 1]
        if command == IAC:
            out.append(IAC)
            i += 2
            continue

        if command == SB:
            end = data.find(bytes([IAC, SE]), i + 2)
            i = len(data) if end == -1 else end + 2
            continue

        if command in (WILL, WONT, DO, DONT) and i + 2 < len(data):
            option = data[i + 2]
            response = _telnet_response(command, option)
            if response:
                try:
                    sock.sendall(response)
                except OSError:
                    pass
            i += 3
            continue

        i += 2
    return bytes(out)


def _telnet_response(command: int, option: int) -> bytes:
    if option in (OPT_BINARY, OPT_SUPPRESS_GO_AHEAD, OPT_COM_PORT):
        if command == DO:
            return bytes([IAC, WILL, option])
        if command == WILL:
            return bytes([IAC, DO, option])
    if command in (DO, DONT):
        return bytes([IAC, WONT, option])
    if command in (WILL, WONT):
        return bytes([IAC, DONT, option])
    return b""


def _send_rfc2217_line_settings(sock: socket.socket, baud_rate: int) -> None:
    # hub4com's command line sets --br=remote, so the TCP client provides the
    # serial line settings that the GUI used.
    messages = [
        bytes([IAC, DO, OPT_COM_PORT]),
        bytes([IAC, WILL, OPT_COM_PORT]),
        bytes([IAC, DO, OPT_BINARY]),
        bytes([IAC, WILL, OPT_BINARY]),
        bytes([IAC, WILL, OPT_SUPPRESS_GO_AHEAD]),
        _rfc2217_subnegotiation(SET_BAUDRATE, int(baud_rate).to_bytes(4, "big")),
        _rfc2217_subnegotiation(SET_DATASIZE, b"\x08"),
        _rfc2217_subnegotiation(SET_PARITY, b"\x01"),
        _rfc2217_subnegotiation(SET_STOPSIZE, b"\x01"),
        _rfc2217_subnegotiation(SET_CONTROL, b"\x01"),
        _rfc2217_subnegotiation(SET_CONTROL, b"\x08"),
        _rfc2217_subnegotiation(SET_CONTROL, b"\x0b"),
        _rfc2217_subnegotiation(PURGE_DATA, b"\x03"),
    ]
    try:
        sock.sendall(b"".join(messages))
    except OSError:
        pass


def _rfc2217_subnegotiation(command: int, payload: bytes) -> bytes:
    return bytes([IAC, SB, OPT_COM_PORT, command]) + _escape_iac(payload) + bytes([IAC, SE])


def _escape_iac(payload: bytes) -> bytes:
    return payload.replace(bytes([IAC]), bytes([IAC, IAC]))


def _send_serial_payload(sock: socket.socket, payload: bytes) -> None:
    try:
        sock.sendall(_escape_iac(payload))
    except OSError:
        pass


def strip_telnet_iac(data: bytes) -> bytes:
    # hub4com's RFC2217 mode can include Telnet negotiation bytes. The display
    # payload is still readable after removing IAC command triplets.
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] != IAC:
            out.append(data[i])
            i += 1
            continue
        if i + 1 >= len(data):
            break
        command = data[i + 1]
        if command == IAC:
            out.append(IAC)
            i += 2
        elif command == SB:
            end = data.find(bytes([IAC, SE]), i + 2)
            i = len(data) if end == -1 else end + 2
        else:
            i += 3 if i + 2 < len(data) else 2
    return bytes(out)
