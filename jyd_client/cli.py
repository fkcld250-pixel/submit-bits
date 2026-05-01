from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import sys
from threading import Lock

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
            return 0 if result.task_success and not result.error else 1
        if args.command == "stability":
            _authenticate_if_requested(cfg, args)
            _apply_serial_overrides(cfg, args)
            return _run_stability_test(cfg, args)
        if args.command == "sweep-fpgas":
            _authenticate_if_requested(cfg, args)
            _apply_serial_overrides(cfg, args)
            return _run_fpga_sweep(cfg, args)
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
                        if not result.task_success or result.error:
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
    add_board_selection_args(run)
    add_retry_args(run)
    add_hold_args(run)
    run.add_argument("--no-save-result", action="store_true")

    stability = sub.add_parser("stability", help="Run repeated forced tests on one FPGA resource")
    add_auth_args(stability)
    stability.add_argument("bitfile")
    stability.add_argument("--fpga", required=True, help="FPGA resource fpga_name to force for every run")
    stability.add_argument("--count", type=int, default=10, help="Number of logical test runs (default: 10)")
    stability.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum retries per logical test run after the first attempt (default: 5)",
    )
    stability.add_argument("--output-dir", default="stability")
    stability.add_argument("--timeout", type=int)
    stability.add_argument("--baud-rate", type=int)
    stability.add_argument("--stable-seconds", type=float)
    stability.add_argument("--no-save-result", action="store_true")
    stability.set_defaults(force_use=True, keep_in_use=True)

    sweep = sub.add_parser("sweep-fpgas", help="Test bitstreams on every FPGA resource with up to 3 workers")
    add_auth_args(sweep)
    sweep.add_argument("path", help="Bitstream file, directory, or text file list")
    sweep.add_argument("--pattern", default="*.bit")
    sweep.add_argument("--output", default="stability/all_fpgas.jsonl")
    sweep.add_argument("--timeout", type=int)
    sweep.add_argument("--baud-rate", type=int)
    sweep.add_argument("--stable-seconds", type=float)
    add_retry_args(sweep)
    sweep.add_argument("--no-save-result", action="store_true")

    batch = sub.add_parser("batch", help="Program all bitstreams in a directory or list file")
    add_auth_args(batch)
    batch.add_argument("path")
    batch.add_argument("--pattern", default="*.bit")
    batch.add_argument("--output")
    batch.add_argument("--timeout", type=int)
    batch.add_argument("--baud-rate", type=int)
    batch.add_argument("--stable-seconds", type=float)
    batch.add_argument("--jobs", type=int, default=3, help="Parallel batch jobs, 1-3 (default: 3)")
    add_board_selection_args(batch)
    add_retry_args(batch)
    add_hold_args(batch)
    batch.add_argument("--no-save-result", action="store_true")
    return parser


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", default="13599187486")
    parser.add_argument("--password")
    parser.add_argument("--skip-login", action="store_true", help="Skip contest account validation")


def add_retry_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--retry", action="store_true", help="Retry when a run fails due to timeout or Vivado programming error")
    parser.add_argument(
        "--max-retries",
        type=int,
        help="Maximum retries after the first attempt; implies --retry when greater than 0",
    )


def add_board_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--force-use",
        action="store_true",
        help="Ignore board in_use status and allocate from all FPGA resources",
    )
    parser.add_argument(
        "--fpga",
        help="Force use a specific FPGA resource by fpga_name; implies --force-use",
    )


def add_hold_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--keep-in-use",
        action="store_true",
        help="Keep the FPGA board status as in_use after the command exits",
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
    result, _ = _run_bitfile_with_retry_info(runner, bitfile, args)
    return result


def _run_bitfile_with_retry_info(runner: Runner, bitfile: str | Path, args) -> tuple[object, int]:
    max_retries = _timeout_retry_count(args)
    attempt = 0
    retry_reason = ""
    while True:
        if attempt:
            print(
                f"retrying after {retry_reason}: attempt {attempt + 1}/{max_retries + 1} bitfile={bitfile}",
                file=sys.stderr,
            )
        result = runner.run_bitfile(
            bitfile,
            save_result=not args.no_save_result,
            timeout=args.timeout,
            force_use=getattr(args, "force_use", False) or bool(getattr(args, "fpga", None)),
            fpga_name=getattr(args, "fpga", None),
            release_board=not getattr(args, "keep_in_use", False),
        )
        retry_reason = _retry_failure_reason(result)
        if (result.task_success and not result.error) or not retry_reason or attempt >= max_retries:
            return result, attempt + 1
        _release_retry_hold_if_needed(runner, result, args)
        attempt += 1


def _run_batch_bitfile(cfg, bitfile: str | Path, args) -> object:
    return _run_bitfile_with_retries(Runner(cfg), bitfile, args)


def _release_retry_hold_if_needed(runner: Runner, result: object, args) -> None:
    if not getattr(args, "keep_in_use", False):
        return
    fpga_name = getattr(result, "fpga_name", None)
    if not fpga_name:
        return
    try:
        runner.db.release_board(fpga_name)
        print(f"released board {fpga_name} before retry", file=sys.stderr)
    except Exception as exc:
        print(f"failed to release board {fpga_name} before retry: {exc}", file=sys.stderr)


def _run_stability_test(cfg, args) -> int:
    if args.count <= 0:
        raise JydClientError("--count must be > 0")
    if args.max_retries < 0:
        raise JydClientError("--max-retries must be >= 0")

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{args.fpga}.jsonl"
    runner = Runner(cfg)
    success_count = 0
    completed = 0
    release_error = None

    try:
        with output_path.open("w", encoding="utf-8") as f:
            for index in range(1, args.count + 1):
                result, attempts = _run_one_stability_iteration(runner, args.bitfile, args, index)
                completed += 1
                if getattr(result, "task_success", False):
                    success_count += 1
                record = {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "fpga_name": args.fpga,
                    "iteration": index,
                    "attempts": attempts,
                    "max_retries": args.max_retries,
                    "result": result.to_dict(),
                }
                line = json.dumps(record, ensure_ascii=False)
                f.write(line + "\n")
                f.flush()
                print(line)
    finally:
        try:
            Database(cfg.mysql).release_board(args.fpga)
            print(f"released board {args.fpga} after stability test", file=sys.stderr)
        except Exception as exc:
            release_error = exc
            print(f"failed to release board {args.fpga} after stability test: {exc}", file=sys.stderr)

    rate = success_count / completed if completed else 0.0
    summary = {
        "fpga_name": args.fpga,
        "success": success_count,
        "total": completed,
        "success_rate": rate,
        "output": str(output_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if release_error is not None:
        return 1
    return 0 if success_count == completed else 1


def _run_fpga_sweep(cfg, args) -> int:
    bitfiles = _collect_bitfiles(args.path, args.pattern)
    boards = Database(cfg.mysql).list_boards()
    if not boards:
        raise JydClientError("no FPGA boards found")

    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    success_count = 0
    total = 0
    write_lock = Lock()
    worker_count = min(3, len(boards))

    with output_path.open("a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_run_fpga_sweep_board, cfg, args, board, bitfiles, f, write_lock): board
                for board in boards
            }
            for future in as_completed(futures):
                board_success, board_total = future.result()
                success_count += board_success
                total += board_total

    summary = {
        "success": success_count,
        "total": total,
        "success_rate": success_count / total if total else 0.0,
        "output": str(output_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if success_count == total else 1


def _run_fpga_sweep_board(cfg, args, board, bitfiles: list[Path], output_file, write_lock: Lock) -> tuple[int, int]:
    runner = Runner(cfg)
    run_args = argparse.Namespace(**vars(args))
    run_args.fpga = board.fpga_name
    run_args.force_use = True
    run_args.keep_in_use = False
    success_count = 0
    total = 0

    for bitfile in bitfiles:
        result, attempts = _run_bitfile_with_retry_info(runner, bitfile, run_args)
        total += 1
        if getattr(result, "task_success", False) and not getattr(result, "error", None):
            success_count += 1
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "fpga_name": board.fpga_name,
            "bitfile": str(bitfile),
            "attempts": attempts,
            "result": result.to_dict(),
        }
        line = json.dumps(record, ensure_ascii=False)
        with write_lock:
            output_file.write(line + "\n")
            output_file.flush()
            print(line)

    return success_count, total


def _run_one_stability_iteration(runner: Runner, bitfile: str | Path, args, index: int) -> tuple[object, int]:
    attempt = 0
    retry_reason = ""
    while True:
        if attempt:
            print(
                f"stability retry after {retry_reason}: iteration {index} "
                f"attempt {attempt + 1}/{args.max_retries + 1} bitfile={bitfile}",
                file=sys.stderr,
            )
        result = runner.run_bitfile(
            bitfile,
            save_result=not args.no_save_result,
            timeout=args.timeout,
            force_use=True,
            fpga_name=args.fpga,
            release_board=False,
        )
        if getattr(result, "task_success", False):
            return result, attempt + 1
        retry_reason = _retry_failure_reason(result)
        if not retry_reason or attempt >= args.max_retries:
            return result, attempt + 1
        attempt += 1


def _batch_job_count(args) -> int:
    jobs = int(args.jobs)
    if jobs < 1 or jobs > 3:
        raise JydClientError("--jobs must be between 1 and 3")
    if getattr(args, "fpga", None) and jobs != 1:
        print("forcing --jobs 1 because --fpga targets a single FPGA resource", file=sys.stderr)
        return 1
    return jobs


def _timeout_retry_count(args) -> int:
    if args.max_retries is not None and args.max_retries < 0:
        raise JydClientError("--max-retries must be >= 0")
    if args.max_retries is not None:
        return args.max_retries
    return 1 if args.retry else 0


def _retry_failure_reason(result: object) -> str:
    error = getattr(result, "error", None)
    if not error:
        return ""
    error_text = str(error).lower()
    if error_text.startswith("task judgment failed"):
        return ""
    if "timeout" in error_text:
        return "timeout"
    if "vivado programming failed" in error_text:
        return "Vivado programming failed"
    return "runtime error"


def _collect_bitfiles(path_arg: str, pattern: str) -> list[Path]:
    path = Path(path_arg).expanduser()
    if path.is_dir():
        return sorted(path.glob(pattern))
    if path.is_file() and path.suffix.lower() not in {".bit"}:
        return [Path(line.strip()).expanduser() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [path]


if __name__ == "__main__":
    raise SystemExit(main())
