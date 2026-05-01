from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class User:
    user_id: int
    username: str
    used_times: int | None = None
    limit_times: int | None = None

    @property
    def remaining_times(self) -> int | None:
        if self.limit_times is None or self.limit_times <= 0 or self.used_times is None:
            return None
        return max(0, self.limit_times - self.used_times)


@dataclass(frozen=True)
class Board:
    fpga_name: str
    total_port: int
    twin_port: int | None
    jtag_filter: str
    vcom_name: str
    com_name: str
    ip: str
    expected_result: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Board":
        return cls(
            fpga_name=str(row["fpga_name"]),
            total_port=int(row["total_port"]),
            twin_port=_optional_int(row.get("twin_port")),
            jtag_filter=str(row.get("jtag_filter") or ""),
            vcom_name=str(row.get("vcom_name") or ""),
            com_name=str(row.get("com_name") or ""),
            ip=str(row.get("IP") or row.get("ip") or ""),
            expected_result=None if row.get("result") is None else str(row.get("result")),
        )

    @property
    def serial_tcp_port(self) -> int:
        if self.vcom_name.isdigit():
            return int(self.vcom_name)
        return self.twin_port or self.total_port

    @property
    def serial_com_name(self) -> str:
        return self.com_name or self.vcom_name


@dataclass
class RunResult:
    bitfile: str
    fpga_name: str | None
    board_ip: str | None
    started_at: str
    ended_at: str | None = None
    success: bool = False
    burned: bool = False
    parsed_result: str = ""
    led: dict[str, Any] | None = None
    expected_result: str | None = None
    task_success: bool = False
    task_judgment: dict[str, Any] | None = None
    user_id: int | None = None
    usage_counted: bool = False
    used_times_before: int | None = None
    limit_times: int | None = None
    remaining_times_after: int | None = None
    error: str | None = None

    @classmethod
    def start(cls, bitfile: str) -> "RunResult":
        return cls(
            bitfile=bitfile,
            fpga_name=None,
            board_ip=None,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

    def finish(self) -> None:
        self.ended_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
