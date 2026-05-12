from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.toml"


DEFAULT_CONFIG = {
    "mysql": {
        "host": "192.168.2.200",
        "port": 3306,
        "user": "root",
        "password": "mhw168",
        "database": "port_manager",
        "charset": "utf8mb4",
    },
    "ssh": {
        "user": "remoteuser",
        "password": "mhw168",
        "port": 22,
        "timeout": 20,
    },
    "remote": {
        "vivado_path": "D:/vivado/Vivado/2023.2/bin/vivado.bat",
        "temp_dir": "C:/Temp",
        "hw_server_wait_seconds": 30,
        "serial_read_seconds": 60,
        "serial_idle_seconds": 3,
        "serial_stable_seconds": 15,
        "serial_baud_rate": 9600,
        "serial_poll_byte": "80",
        "serial_poll_interval_seconds": 0.701,
        "serial_max_payload_bytes": 0,
        "hw_server_script_path": "C:/Temp/start_hw_server.ps1",
        "use_generated_hw_server_script": False,
        "heartbeat_enabled": True,
        "heartbeat_interval_seconds": 45,
        "stale_board_minutes": 3,
    },
    "local": {
        "results_jsonl": "results.jsonl",
    },
}


@dataclass(frozen=True)
class Config:
    data: dict
    path: Path

    @property
    def mysql(self) -> dict:
        return self.data["mysql"]

    @property
    def ssh(self) -> dict:
        return self.data["ssh"]

    @property
    def remote(self) -> dict:
        return self.data["remote"]

    @property
    def local(self) -> dict:
        return self.data["local"]


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    data = _deep_copy(DEFAULT_CONFIG)
    if not cfg_path.exists():
        save_config(data, cfg_path)
        return Config(data=data, path=cfg_path)

    text = cfg_path.read_text(encoding="utf-8")
    loaded = _parse_toml(text)
    _merge_dict(data, loaded)
    return Config(data=data, path=cfg_path)


def save_config(data: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_toml(data), encoding="utf-8")


def _deep_copy(value: dict) -> dict:
    return json.loads(json.dumps(value))


def _merge_dict(base: dict, update: dict) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value


def _parse_toml(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore

    return tomllib.loads(text)


def _dump_toml(data: dict) -> str:
    lines: list[str] = []
    for section, values in data.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, str):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
            elif isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            else:
                lines.append(f"{key} = {value}")
        lines.append("")
    return "\n".join(lines)
