from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import getpass
import json
from pathlib import Path
import sys

from .config import load_config
from .db import Database
from .errors import JydClientError
from .runner import Runner


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    try:
        if args.command == "login":
            password = args.password or getpass.getpass("Password: ")
            user = Database(cfg.mysql).authenticate(args.user, password)
            print(json.dumps({"ok": True, "user_id": user.user_id, "username": user.username}, ensure_ascii=False))
            return 0
        if args.command == "list-boards":
            boards = Database(cfg.mysql).list_boards()
            for board in boards:
                print(json.dumps(board.__dict__, ensure_ascii=False))
            return 0
        if args.command == "release":
            Database(cfg.mysql).release_board(args.fpga)
            print(json.dumps({"ok": True, "released": args.fpga}, ensure_ascii=False))
            return 0
        if args.command == "reset-boards":
            affected = Database(cfg.mysql).reset_all_boards_available()
            print(json.dumps({"ok": True, "status": "available", "affected": affected}, ensure_ascii=False))
            return 0
        if args.command == "run":
            _authenticate_if_requested(cfg, args)
            _apply_serial_overrides(cfg, args)
            result = _run_bitfile_with_retries(Runner(cfg), args.bitfile, args)
            print(json.dumps(result.to_dict(), ensure_ascii=False))
            return 0 if result.success and not result.error else 1
        if args.command == "batch":
            _authenticate_if_requested(cfg, args)
            _apply_serial_overrides(cfg, args)
            jobs = _batch_job_count(args)
            bitfiles = _collect_bitfiles(args.path, args.pattern)
            output = Path(args.output or cfg.local["results_jsonl"]).expanduser()
            failures = 0
            with output.open("a", encoding="utf-8") as f:
                with ThreadPoolExecutor(max_workers=jobs) as executor:
                    futures = {
                        executor.submit(_run_batch_bitfile, cfg, bitfile, args): bitfile
                        for bitfile in bitfiles
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        line = json.dumps(result.to_dict(), ensure_ascii=False)
                        f.write(line + "\n")
                        f.flush()
                        print(line)
                        if not result.success or result.error:
                            failures += 1
            return 1 if failures else 0
    except JydClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jyd-client")
    parser.add_argument("--config", help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Validate contest account against server database")
    login.add_argument("--user", required=True)
    login.add_argument("--password")

    sub.add_parser("list-boards", help="List FPGA boards from database")

    release = sub.add_parser("release", help="Mark a board available")
    release.add_argument("--fpga", required=True)

    sub.add_parser("reset-boards", help="Force all FPGA boards to available")

    run = sub.add_parser("run", help="Program one bitstream and read result")
    add_auth_args(run)
    run.add_argument("bitfile")
    run.add_argument("--timeout", type=int)
    run.add_argument("--baud-rate", type=int)
    run.add_argument("--stable-seconds", type=float)
    add_retry_args(run)
    run.add_argument("--no-save-result", action="store_true")

    batch = sub.add_parser("batch", help="Program all bitstreams in a directory or list file")
    add_auth_args(batch)
    batch.add_argument("path")
    batch.add_argument("--pattern", default="*.bit")
    batch.add_argument("--output")
    batch.add_argument("--timeout", type=int)
    batch.add_argument("--baud-rate", type=int)
    batch.add_argument("--stable-seconds", type=float)
    batch.add_argument("--jobs", type=int, default=3, help="Parallel batch jobs, 1-3 (default: 3)")
    add_retry_args(batch)
    batch.add_argument("--no-save-result", action="store_true")
    return parser


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", default="13599187486")
    parser.add_argument("--password")
    parser.add_argument("--skip-login", action="store_true", help="Skip contest account validation")


def add_retry_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--retry", action="store_true", help="Retry when a run fails due to timeout")
    parser.add_argument(
        "--max-retries",
        type=int,
        help="Maximum timeout retries after the first attempt; implies --retry when greater than 0",
    )


def _authenticate_if_requested(cfg, args) -> None:
    if args.skip_login:
        return
    password = args.password or getpass.getpass("Password: ")
    Database(cfg.mysql).authenticate(args.user, password)


def _apply_serial_overrides(cfg, args) -> None:
    if getattr(args, "baud_rate", None):
        cfg.data["remote"]["serial_baud_rate"] = args.baud_rate
    if getattr(args, "stable_seconds", None) is not None:
        cfg.data["remote"]["serial_stable_seconds"] = args.stable_seconds


def _run_bitfile_with_retries(runner: Runner, bitfile: str | Path, args) -> object:
    max_retries = _timeout_retry_count(args)
    attempt = 0
    while True:
        if attempt:
            print(
                f"retrying after timeout: attempt {attempt + 1}/{max_retries + 1} bitfile={bitfile}",
                file=sys.stderr,
            )
        result = runner.run_bitfile(bitfile, save_result=not args.no_save_result, timeout=args.timeout)
        if result.success or not _is_timeout_failure(result) or attempt >= max_retries:
            return result
        attempt += 1


def _run_batch_bitfile(cfg, bitfile: str | Path, args) -> object:
    return _run_bitfile_with_retries(Runner(cfg), bitfile, args)


def _batch_job_count(args) -> int:
    jobs = int(args.jobs)
    if jobs < 1 or jobs > 3:
        raise JydClientError("--jobs must be between 1 and 3")
    return jobs


def _timeout_retry_count(args) -> int:
    if args.max_retries is not None and args.max_retries < 0:
        raise JydClientError("--max-retries must be >= 0")
    if args.max_retries is not None:
        return args.max_retries
    return 1 if args.retry else 0


def _is_timeout_failure(result: object) -> bool:
    error = getattr(result, "error", None)
    return bool(error and "timeout" in str(error).lower())


def _collect_bitfiles(path_arg: str, pattern: str) -> list[Path]:
    path = Path(path_arg).expanduser()
    if path.is_dir():
        return sorted(path.glob(pattern))
    if path.is_file() and path.suffix.lower() not in {".bit"}:
        return [Path(line.strip()).expanduser() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [path]


if __name__ == "__main__":
    raise SystemExit(main())
