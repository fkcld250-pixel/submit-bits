from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import time
import uuid
import zipfile

from .errors import RemoteCommandError, require_module
from .logging_utils import log
from .models import Board


class RemoteSession:
    def __init__(self, host: str, ssh_cfg: dict, remote_cfg: dict):
        paramiko = require_module("paramiko", "pip install -r requirements.txt")
        self.host = host
        self.ssh_cfg = ssh_cfg
        self.remote_cfg = remote_cfg
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._last_hw_server_log_path = win_join(
            self.remote_cfg.get("temp_dir", "C:/Temp"), "hw_server_exec_log.txt"
        )

    def __enter__(self) -> "RemoteSession":
        self.client.connect(
            hostname=self.host,
            port=int(self.ssh_cfg.get("port", 22)),
            username=self.ssh_cfg["user"],
            password=self.ssh_cfg["password"],
            timeout=int(self.ssh_cfg.get("timeout", 20)),
            banner_timeout=int(self.ssh_cfg.get("timeout", 20)),
            auth_timeout=int(self.ssh_cfg.get("timeout", 20)),
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.client.close()

    def exec(self, command: str, check: bool = True, timeout: int | None = None) -> tuple[int, str, str]:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")
        if check and exit_status != 0:
            raise RemoteCommandError(
                f"remote command failed ({exit_status}) on {self.host}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
            )
        return exit_status, out, err

    def tail_file(self, remote_path: str, lines: int = 80) -> str:
        command = f'powershell -NoProfile -Command "Get-Content {ps_quote(remote_path)} -Tail {int(lines)}"'
        _, out, err = self.exec(command, check=False)
        return (out + err).strip()

    def put_text(self, remote_path: str, text: str) -> None:
        with self.client.open_sftp() as sftp:
            with sftp.file(remote_path, "w") as f:
                f.write(text)

    def put_file(self, local: Path, remote_path: str) -> None:
        total_size = local.stat().st_size
        progress = _UploadProgress(total_size)
        with self.client.open_sftp() as sftp:
            sftp.put(str(local), remote_path, callback=progress)
        log(f"uploaded {local.name} to {self.host}:{remote_path}")

    def ensure_temp(self) -> str:
        temp_dir = self.remote_cfg.get("temp_dir", "C:/Temp")
        self.exec(f'powershell -NoProfile -Command "New-Item -ItemType Directory -Path {ps_quote(temp_dir)} -Force | Out-Null"')
        return temp_dir

    def stop_port_owner(self, port: int) -> None:
        command = (
            'powershell -NoProfile -Command '
            f'"$port={int(port)}; '
            '$procId=(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess '
            '| Where-Object { $_ -ne 0 } | Select-Object -First 1; '
            'if($procId){taskkill /F /PID $procId | Out-Null}"'
        )
        self.exec(command, check=False)

    def start_hw_server(self, port: int, jtag_filter: str) -> None:
        temp_dir = self.ensure_temp()
        log_path = win_join(temp_dir, "hw_server_exec_log.txt")
        self.stop_port_owner(port)
        if self.remote_cfg.get("use_generated_hw_server_script", False):
            ps_path = win_join(temp_dir, "start_hw_server_jyd.ps1")
            script = _start_hw_server_script(self.remote_cfg["vivado_path"])
            self.put_text(ps_path, script)
            self._start_powershell_script_async(
                ps_path,
                {
                    "Port": str(int(port)),
                    "Jtag": jtag_filter,
                    "LogPath": log_path,
                },
            )
            log(f"using generated hw_server script {ps_path}")
        else:
            ps_path = self.remote_cfg.get("hw_server_script_path", "C:/Temp/start_hw_server.ps1")
            self._last_hw_server_log_path = win_join(temp_dir, f"{jtag_filter}_hw_server_exec_log.txt")
            self._start_powershell_script_async(
                ps_path,
                {
                    "Port": str(int(port)),
                    "Jtag": jtag_filter,
                },
            )
            log(f"using remote hw_server script {ps_path}")
        log(f"requested async hw_server start on {self.host}:{port}")

    def hw_server_log_path(self) -> str:
        return self._last_hw_server_log_path

    def wait_local_port(self, port: int, timeout: int, label: str = "port", log_path: str | None = None) -> None:
        script = (
            f"$deadline=(Get-Date).AddSeconds({int(timeout)}); "
            f"while((Get-Date) -lt $deadline){{ "
            f"$c=Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue; "
            "if($c){ exit 0 }; Start-Sleep -Seconds 1 }; exit 1"
        )
        status, out, err = self.exec(f'powershell -NoProfile -Command "{script}"', check=False, timeout=timeout + 10)
        if status != 0:
            log_tail = self.tail_file(log_path or self.hw_server_log_path(), lines=20)
            detail = f"\nremote {label} log:\n{log_tail}" if log_tail else ""
            raise RemoteCommandError(
                f"timeout waiting for remote local {label} port {port} on {self.host}{detail}\n{out}{err}"
            )

    def start_com2tcp(self, com_name: str, tcp_port: int) -> None:
        temp_dir = self.ensure_temp()
        log_path = win_join(temp_dir, "com2tcp_exec_log.txt")
        script_path = win_join(temp_dir, "run_com2tcp.ps1")
        self.stop_port_owner(tcp_port)
        status, out, err = self._start_powershell_script_async(
            script_path,
            {
                "ComPort": com_name,
                "TcpPort": str(int(tcp_port)),
            },
        )
        if status != 0:
            tail = self.exec(f'powershell -NoProfile -Command "Get-Content {ps_quote(log_path)} -Tail 20"', check=False)[1]
            raise RemoteCommandError(
                "failed to start remote com2tcp. Expected C:/Temp/run_com2tcp.ps1 to exist on the remote host.\n"
                f"stdout:\n{out}\nstderr:\n{err}\nlog:\n{tail}"
            )
        log(f"requested async com2tcp start on {self.host}:{tcp_port} for {com_name}")

    def com2tcp_log_path(self) -> str:
        return win_join(self.remote_cfg.get("temp_dir", "C:/Temp"), "com2tcp_exec_log.txt")

    def _start_powershell_script_async(self, script_path: str, params: dict[str, str]) -> tuple[int, str, str]:
        args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win_path(script_path)]
        for key, value in params.items():
            args.extend([f"-{key}", value])
        command_line = "powershell.exe " + " ".join(cmd_quote(value) for value in args)
        command = (
            'powershell -NoProfile -Command '
            f'"(Invoke-CimMethod -ClassName Win32_Process -MethodName Create '
            f"-Arguments @{{CommandLine={ps_quote(command_line)}}}).ReturnValue\""
        )
        return self.exec(command, check=False)

    def program_bitstream(self, bitfile: Path, board: Board) -> None:
        temp_dir = self.ensure_temp()
        remote_bit = win_join(temp_dir, f"{board.fpga_name}_fpga.bit")
        remote_tcl = win_join(temp_dir, f"{board.fpga_name}_auto_program.tcl")
        local_tcl = generate_program_tcl(bitfile_remote=remote_bit, port=board.total_port)
        remote_zip = win_join(temp_dir, f"bits.z{uuid.uuid4().hex[:5]}")

        self.cleanup_bitstream_zip(remote_zip, reason="before upload")
        try:
            with tempfile.TemporaryDirectory(prefix="jyd-bitstream-zip-") as tmp_dir:
                local_zip = Path(tmp_dir) / Path(remote_zip).name
                with zipfile.ZipFile(local_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.write(bitfile, arcname=Path(remote_bit).name)
                log(
                    "compressed bitstream "
                    f"{bitfile.name} -> {local_zip.name} ({_format_bytes(local_zip.stat().st_size)})"
                )
                self.put_file(local_zip, remote_zip)

            self.extract_bitstream_zip(remote_zip, temp_dir, remote_bit)
            self.cleanup_bitstream_zip(remote_zip, reason="after extract")
            self.put_text(remote_tcl, local_tcl)
            vivado = self.remote_cfg["vivado_path"]
            command = f'cmd /c ""{vivado}" -mode batch -source "{remote_tcl}""'
            status, out, err = self.exec(command, check=False, timeout=600)
            if status != 0:
                raise RemoteCommandError(f"Vivado programming failed ({status})\nSTDOUT:\n{out}\nSTDERR:\n{err}")
            log(f"Vivado programming completed for {bitfile.name} on {self.host}")
        finally:
            self.cleanup_bitstream_zip(remote_zip, reason="final cleanup")

    def cleanup_bitstream_zip(self, remote_zip: str, reason: str = "cleanup") -> None:
        log(f"cleaning temporary bitstream zip ({reason}): {self.host}:{remote_zip}")
        script = (
            f"$zip={ps_quote(remote_zip)}; "
            "if(Test-Path -LiteralPath $zip){ "
            "Remove-Item -LiteralPath $zip -Force -ErrorAction Stop; "
            "}; "
            "exit 0"
        )
        command = (
            'powershell -NoProfile -Command '
            f'"{script}"'
        )
        status, out, err = self.exec(command, check=False)
        if status != 0:
            raise RemoteCommandError(f"failed to clean temporary bitstream zip ({status})\nSTDOUT:\n{out}\nSTDERR:\n{err}")
        log(f"temporary bitstream zip cleanup completed ({reason})")

    def extract_bitstream_zip(self, remote_zip: str, temp_dir: str, remote_bit: str) -> None:
        script = (
            f"$zip={ps_quote(remote_zip)}; "
            f"$dest={ps_quote(temp_dir)}; "
            f"$bit={ps_quote(remote_bit)}; "
            "if(Test-Path -LiteralPath $bit){ Remove-Item -LiteralPath $bit -Force }; "
            "Add-Type -AssemblyName System.IO.Compression.FileSystem; "
            "[System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $dest)"
        )
        command = f'powershell -NoProfile -Command "{script}"'
        status, out, err = self.exec(command, check=False, timeout=120)
        if status != 0:
            raise RemoteCommandError(f"failed to extract bitstream zip ({status})\nSTDOUT:\n{out}\nSTDERR:\n{err}")
        log(f"extracted bitstream zip on {self.host}:{remote_zip}")


def wait_tcp(host: str, port: int, timeout: int) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise RemoteCommandError(f"timeout waiting for {host}:{port}: {last_error}")


def generate_program_tcl(bitfile_remote: str, port: int) -> str:
    bitfile = bitfile_remote.replace("\\", "/")
    return f"""
open_hw_manager
connect_hw_server -url 127.0.0.1:{int(port)}
catch {{ close_hw_target }}
open_hw_target
after 500
set devices [get_hw_devices]
if {{[llength $devices] == 0}} {{
  puts "ERROR: No device detected"
  exit 1
}}
refresh_hw_device -update_hw_probes false [lindex $devices 0]
after 300
set devices [get_hw_devices]
if {{[llength $devices] == 0}} {{
  puts "ERROR: No device detected after refresh"
  exit 2
}}
set target_dev [lindex $devices 0]
current_hw_device $target_dev
catch {{ open_hw_device $target_dev }}
set bitfile "{bitfile}"
if {{![file exists $bitfile]}} {{
  puts "ERROR: Bitfile not found: $bitfile"
  exit 3
}}
set_property PROGRAM.FILE $bitfile $target_dev
program_hw_devices $target_dev
close_hw_manager
exit 0
""".lstrip()


def win_join(base: str, name: str) -> str:
    return base.rstrip("/\\") + "/" + name


def win_path(value: str) -> str:
    if len(value) >= 3 and value[1] == ":" and value[2] == "/":
        return value.replace("/", "\\")
    return value


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cmd_quote(value: str) -> str:
    return '"' + value.replace('"', r'\"') + '"'


class _UploadProgress:
    def __init__(self, total_size: int):
        self.total_size = max(total_size, 1)
        self._last_percent = -5

    def __call__(self, transferred: int, total: int) -> None:
        total_size = max(total or self.total_size, 1)
        percent = min(100, int(transferred * 100 / total_size))
        if percent < 100 and percent - self._last_percent < 5:
            return
        self._last_percent = percent
        log(
            "upload progress: "
            f"{percent}% ({_format_bytes(transferred)}/{_format_bytes(total_size)})"
        )


def _format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


def _start_hw_server_script(vivado_path: str) -> str:
    escaped = vivado_path.replace("'", "''")
    return f"""
param(
  [int]$Port,
  [string]$Jtag,
  [string]$LogPath
)
$VivadoPath = '{escaped}'
$HwServerPath = $VivadoPath -replace 'vivado\\.bat$', 'hw_server.bat'
if (!(Test-Path $HwServerPath)) {{
  $HwServerPath = Join-Path (Split-Path $VivadoPath -Parent) 'hw_server.bat'
}}
if (!(Test-Path $HwServerPath)) {{
  throw "Cannot locate hw_server.bat from $VivadoPath"
}}
$args = @('-s', "tcp::$Port")
if ($Jtag -and $Jtag.Trim().Length -gt 0) {{
  $args += @('-e', "set jtag-port-filter $Jtag")
}}
"Starting $HwServerPath $($args -join ' ')" | Out-File -FilePath $LogPath -Encoding utf8
$quotedArgs = ($args | ForEach-Object {{ if ($_ -match '\\s') {{ '"' + ($_ -replace '"','\\"') + '"' }} else {{ $_ }} }}) -join ' '
$cmdLine = '"' + $HwServerPath + '" ' + $quotedArgs + ' >> "' + $LogPath + '" 2>&1'
Start-Process -FilePath "cmd.exe" -ArgumentList @('/c', $cmdLine) -WindowStyle Hidden
Start-Sleep -Seconds 2
exit 0
""".lstrip()
