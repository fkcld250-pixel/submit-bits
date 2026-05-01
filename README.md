# JYD Linux FPGA Client

Linux command line client for the FPGA competition test flow reverse engineered from `main_gui.exe`.

## Setup

```bash
cd ~/jyd-unix
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Quick Use

```bash
python3 -m jyd_client.cli login --user 13599187486 --password 1234567
python3 -m jyd_client.cli list-boards
python3 -m jyd_client.cli run path/to/design.bit --user 13599187486 --password 1234567
python3 -m jyd_client.cli batch path/to/bits --user 13599187486 --password 1234567
```

If stale `in_use` board state blocks testing, force board selection:

```bash
python3 -m jyd_client.cli run path/to/design.bit --skip-login --force-use
python3 -m jyd_client.cli run path/to/design.bit --skip-login --fpga FPGA1
```

`--force-use` ignores the database `in_use` state and randomly selects from all
FPGA resources. `--fpga` forces a specific `fpga_name` and implies
`--force-use`. In batch mode, `--fpga` runs with one worker to avoid programming
the same board concurrently.

The default configuration targets `192.168.2.200`, MySQL database `port_manager`, and SSH user `remoteuser`.

## Configuration

On first run, the client creates `config.toml` next to this README. Edit it if the contest environment changes.

Important defaults:

- MySQL: `192.168.2.200:3306`, user `root`, password `123456`, database `port_manager`
- SSH: user `remoteuser`, password `jyd123`, port `22`
- Remote Vivado path: `D:/vivado/Vivado/2023.2/bin/vivado.bat`
- Remote temp directory: `C:/Temp`
- Serial read: polls with byte `0x80` every `0.701s` at `9600` baud, then waits until the parsed display and LED state have stayed unchanged for `10s`. Override per run with `--stable-seconds`.

## Output

`run` prints one JSON object. `batch` writes JSONL rows to `results.jsonl` by default. Each row includes board identity, burn status, parsed display result, LED state, and errors.

Progress logs are written to stderr so stdout remains machine-readable JSON.

## Notes

This client does not modify the Windows GUI program. It directly implements the backend flow observed in the packaged Python bytecode: login via MySQL, allocate a board, SSH to the remote Windows host, run Vivado/hw_server/com2tcp, read the forwarded serial stream, parse the seven-segment display, then release the board.

Bitstreams are compressed locally to a temporary zip named like `bits.z12345`,
uploaded by SFTP, extracted on the remote Windows host, and then removed. Each
programming attempt cleans remote `bits.z*` files before upload, immediately
after extraction, and again during final cleanup; these cleanup steps are logged
to stderr.
