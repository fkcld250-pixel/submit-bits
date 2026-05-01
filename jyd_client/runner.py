from __future__ import annotations

from pathlib import Path
import time

from .db import Database
from .logging_utils import log
from .models import RunResult
from .parser import parse_signal_bytes
from .remote import RemoteSession
from .serial_reader import read_tcp_serial


TASK_DISPLAY_MIN_EXCLUSIVE = 0x37000000
TASK_DISPLAY_MAX_INCLUSIVE = 0x37999999
TASK_EXPECTED_LED_HEX = "0x01221c08"


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = Database(cfg.mysql)

    def run_bitfile(
        self,
        bitfile: str | Path,
        save_result: bool = True,
        timeout: int | None = None,
        force_use: bool = False,
        fpga_name: str | None = None,
        release_board: bool = True,
    ) -> RunResult:
        bit_path = Path(bitfile).expanduser().resolve()
        result = RunResult.start(str(bit_path))
        board = None
        if not bit_path.exists():
            result.error = f"bitfile not found: {bit_path}"
            result.finish()
            return result

        try:
            board = self.db.allocate_board(force=force_use, fpga_name=fpga_name)
            result.fpga_name = board.fpga_name
            result.board_ip = board.ip
            result.expected_result = board.expected_result
            if not release_board:
                self.db.mark_board_in_use(board.fpga_name)
                log(f"keeping board {board.fpga_name} marked in_use after run")
            allocation_mode = "forced" if force_use or fpga_name else "allocated"
            log(f"confirmed board {board.fpga_name} status=in_use ({allocation_mode})")
            log(
                f"{allocation_mode} board "
                f"{board.fpga_name} ip={board.ip} hw_port={board.total_port} "
                f"serial_port={board.serial_tcp_port} com={board.serial_com_name}"
            )

            with RemoteSession(board.ip, self.cfg.ssh, self.cfg.remote) as remote:
                log(f"starting hw_server for {board.fpga_name}")
                remote.start_hw_server(board.total_port, board.jtag_filter)
                tcp_port = board.serial_tcp_port
                log(f"starting serial bridge for {board.fpga_name}")
                remote.start_com2tcp(board.serial_com_name, tcp_port)
                remote.wait_local_port(tcp_port, 15, label="com2tcp", log_path=remote.com2tcp_log_path())
                log(f"remote serial bridge is listening on local port {tcp_port}")
                hw_timeout = int(self.cfg.remote.get("hw_server_wait_seconds", 30))
                log(f"waiting up to {hw_timeout}s for remote local hw_server port {board.total_port}")
                remote.wait_local_port(board.total_port, hw_timeout, label="hw_server")
                log(f"remote hw_server is listening on local port {board.total_port}")
                log(f"uploading and programming bitstream {bit_path}")
                remote.program_bitstream(bit_path, board)
                result.burned = True
                log(f"stopping hw_server for {board.fpga_name}")
                remote.stop_port_owner(board.total_port)

            serial_timeout = int(timeout or self.cfg.remote.get("serial_read_seconds", 60))
            idle_timeout = int(self.cfg.remote.get("serial_idle_seconds", 3))
            stable_seconds = float(self.cfg.remote.get("serial_stable_seconds", 15))
            baud_rate = int(self.cfg.remote.get("serial_baud_rate", 9600))
            poll_byte = str(self.cfg.remote.get("serial_poll_byte", "80"))
            poll_interval = float(self.cfg.remote.get("serial_poll_interval_seconds", 0.701))
            max_payload_bytes = int(self.cfg.remote.get("serial_max_payload_bytes", 0))
            log(
                f"reading serial result for up to {serial_timeout}s at {baud_rate} baud; "
                f"waiting for {stable_seconds:g}s stable display/LED"
            )
            signal_filter = _SignalSampleFilter()
            serial_started_at = time.monotonic()
            payload_bytes = read_tcp_serial(
                board.ip,
                board.serial_tcp_port,
                serial_timeout,
                idle_timeout,
                baud_rate,
                poll_byte=bytes.fromhex(poll_byte),
                poll_interval=poll_interval,
                max_payload_bytes=max_payload_bytes,
                stable_seconds=stable_seconds,
                stable_snapshot=signal_filter,
                stable_sample_logger=_log_stable_signal_sample,
            )
            serial_elapsed = time.monotonic() - serial_started_at
            if signal_filter.final_snapshot:
                result.parsed_result = str(signal_filter.final_snapshot["display"])
                result.led = signal_filter.final_snapshot["led"]
            elif payload_bytes:
                signals = parse_signal_bytes(payload_bytes)
                result.parsed_result = str(signals["display"])
                result.led = signals["led"]
            else:
                result.parsed_result = ""
                result.led = None
            result.success = _is_success(result.parsed_result, result.expected_result)
            result.task_judgment = judge_task_result(result.parsed_result, result.led)
            result.task_success = bool(result.task_judgment["success"])
            if not result.task_success and not result.error:
                result.error = _format_task_judgment_error(result.task_judgment)
            if (
                not result.success
                and not result.error
                and stable_seconds
                and signal_filter.final_snapshot is None
                and serial_elapsed >= max(0, serial_timeout - 0.5)
            ):
                result.error = "timeout waiting for stable serial display/LED"
            log(
                "serial read completed: "
                f"payload={len(payload_bytes)} bytes "
                f"parsed_result={result.parsed_result!r} led={result.led!r} "
                f"task_success={result.task_success!r}"
            )

            if save_result and result.task_success:
                self.db.save_result(board.fpga_name, result.parsed_result)
                log(f"saved result for {board.fpga_name}: {result.parsed_result}")
        except Exception as exc:
            result.error = str(exc)
            log(f"run failed: {exc}")
        finally:
            if board is not None:
                try:
                    with RemoteSession(board.ip, self.cfg.ssh, self.cfg.remote) as remote:
                        log(f"stopping remote ports for {board.fpga_name}")
                        remote.stop_port_owner(board.total_port)
                        remote.stop_port_owner(board.serial_tcp_port)
                except Exception as exc:
                    log(f"failed to stop remote ports for {board.fpga_name}: {exc}")
                if release_board:
                    try:
                        log(f"releasing board {board.fpga_name}")
                        self.db.release_board(board.fpga_name)
                        log(f"released board {board.fpga_name}")
                    except Exception as exc:
                        release_error = f"failed to release board {board.fpga_name}: {exc}"
                        result.error = f"{result.error}; {release_error}" if result.error else release_error
                        log(release_error)
                else:
                    log(f"skipping board release for {board.fpga_name}; status remains in_use")
            result.task_judgment = judge_task_result(result.parsed_result, result.led)
            result.task_success = bool(result.task_judgment["success"])
            result.finish()

        return result


def _is_success(parsed_result: str, expected_result: str | None) -> bool:
    if not parsed_result:
        return False
    if not expected_result or len(parsed_result) != 8 or len(expected_result) != 8:
        return True
    if parsed_result[:2] != expected_result[:2]:
        return False
    try:
        return int(parsed_result[2:]) < int(expected_result[2:])
    except ValueError:
        return False


def judge_task_result(parsed_result: str, led: dict[str, object] | None) -> dict[str, object]:
    display_value = _parse_hex_int(parsed_result)
    display_ok = (
        display_value is not None
        and TASK_DISPLAY_MIN_EXCLUSIVE < display_value <= TASK_DISPLAY_MAX_INCLUSIVE
    )
    led_hex = str(led.get("hex", "")) if isinstance(led, dict) else ""
    led_ok = led_hex.lower() == TASK_EXPECTED_LED_HEX
    return {
        "success": display_ok and led_ok,
        "display_ok": display_ok,
        "led_ok": led_ok,
        "display_value": parsed_result,
        "display_min_exclusive": f"0x{TASK_DISPLAY_MIN_EXCLUSIVE:08x}",
        "display_max_inclusive": f"0x{TASK_DISPLAY_MAX_INCLUSIVE:08x}",
        "led_hex": led_hex,
        "expected_led_hex": TASK_EXPECTED_LED_HEX,
    }


def _format_task_judgment_error(judgment: dict[str, object]) -> str:
    return (
        "task judgment failed: "
        f"display={judgment.get('display_value', '')!r} "
        f"display_ok={judgment.get('display_ok', False)!r} "
        f"led_hex={judgment.get('led_hex', '')!r} "
        f"led_ok={judgment.get('led_ok', False)!r}"
    )


def _parse_hex_int(value: str) -> int | None:
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


class _SignalSampleFilter:
    min_valid_value = 0x37000000

    def __init__(self) -> None:
        self.final_snapshot: dict[str, object] | None = None
        self._last_valid_snapshot: dict[str, object] | None = None

    def __call__(self, payload: bytes) -> dict[str, object] | None:
        signals = parse_signal_bytes(payload)
        display = str(signals["display"])
        led = signals.get("led")
        led_bits = str(led.get("bits", "")) if isinstance(led, dict) else ""
        if not display and not led_bits:
            return None

        snapshot = {"display": display, "led": led}
        stable_key = (display, led_bits)
        if _is_legal_display(display, self.min_valid_value):
            self.final_snapshot = snapshot
            self._last_valid_snapshot = snapshot
            return {
                **snapshot,
                "valid": True,
                "_stable_key": stable_key,
                "_log_sample": True,
            }

        if self._last_valid_snapshot is not None:
            valid_led = self._last_valid_snapshot.get("led")
            valid_led_bits = str(valid_led.get("bits", "")) if isinstance(valid_led, dict) else ""
            return {
                **self._last_valid_snapshot,
                "valid": True,
                "_stable_key": (self._last_valid_snapshot.get("display", ""), valid_led_bits),
                "_log_sample": False,
            }

        self.final_snapshot = snapshot
        return {
            **snapshot,
            "valid": False,
            "_stable_key": stable_key,
            "_log_sample": True,
        }


def _is_legal_display(display: str, min_value: int) -> bool:
    try:
        return int(display, 16) >= min_value
    except ValueError:
        return False


def _log_stable_signal_sample(snapshot: object, stable_elapsed: float) -> None:
    if not isinstance(snapshot, dict):
        return
    led = snapshot.get("led")
    if isinstance(led, dict):
        led_text = f"bits={led.get('bits', '')!r} hex={led.get('hex', '')!r}"
    else:
        led_text = repr(led)
    log(
        "serial sample: "
        f"display={snapshot.get('display', '')!r} valid={snapshot.get('valid', False)!r} led={led_text} "
        f"stable_for={stable_elapsed:.1f}s"
    )
