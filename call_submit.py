#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any


OWNER = "fkcld250-pixel"
REPO = "submit-bits"
REF = "main"
WORKFLOW_ID = "test-bitstream.yml"
REMOTE_SCRIPT_PATH = "call_submit.py"
TMPFILE_UPLOAD_URL = "https://tmpfile.link/api/upload"
RESULT_ARTIFACT_NAME = "fpga-test-result"
RESULT_JSON_NAME = "result.json"
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 60 * 60


class SubmitError(RuntimeError):
    pass


def log(message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def read_secret(script_dir: Path, name: str) -> str:
    path = script_dir / "secrets" / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise SubmitError(f"secret file not found: {path}") from exc
    if not value:
        raise SubmitError(f"secret file is empty: {path}")
    return value


def run_gh_api(args: list[str], *, input_data: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["gh", "api", *args],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SubmitError("gh command not found; install GitHub CLI and run gh auth login on this machine") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        raise SubmitError(f"gh api failed: {' '.join(args)}\n{stderr}") from exc
    return completed.stdout


def gh_api_json(args: list[str], *, input_data: bytes | None = None) -> dict[str, Any]:
    body = run_gh_api(args, input_data=input_data)
    if not body:
        return {}
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise SubmitError(f"expected JSON object from gh api, got: {type(data).__name__}")
    return data


def update_self(script_path: Path) -> None:
    endpoint = f"/repos/{OWNER}/{REPO}/contents/{REMOTE_SCRIPT_PATH}?ref={urllib.parse.quote(REF)}"
    data = gh_api_json([endpoint])
    if data.get("encoding") != "base64" or "content" not in data:
        raise SubmitError("unexpected GitHub contents response for call_submit.py")

    content = base64.b64decode(data["content"])
    tmp_path = script_path.with_name(f".{script_path.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(content)
    tmp_path.chmod(script_path.stat().st_mode)
    os.replace(tmp_path, script_path)
    log("script updated")


def run_command(args: list[str]) -> None:
    try:
        subprocess.run(args, check=True)
    except FileNotFoundError as exc:
        raise SubmitError(f"command not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise SubmitError(f"command failed with exit code {exc.returncode}: {' '.join(args)}") from exc


def create_encrypted_zip(bitfile: Path, password: str, temp_dir: Path) -> Path:
    if not bitfile.is_file():
        raise SubmitError(f"bitfile not found: {bitfile}")
    if bitfile.suffix.lower() != ".bit":
        raise SubmitError(f"expected a .bit file, got: {bitfile}")

    zip_path = temp_dir / "bitstream.zip"
    if shutil.which("zip"):
        run_command(["zip", "-j", "-P", password, str(zip_path), str(bitfile)])
    elif shutil.which("7z"):
        run_command(["7z", "a", "-tzip", f"-p{password}", str(zip_path), str(bitfile)])
    else:
        raise SubmitError("neither zip nor 7z is available for encrypted zip creation")

    if not zip_path.is_file() or zip_path.stat().st_size == 0:
        raise SubmitError(f"encrypted zip was not created: {zip_path}")
    return zip_path


def upload_tmpfile(path: Path) -> str:
    boundary = f"----jyd-{uuid.uuid4().hex}"
    file_bytes = path.read_bytes()
    filename = path.name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    request = urllib.request.Request(
        TMPFILE_UPLOAD_URL,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "jyd-call-submit",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise SubmitError(f"tmpfile upload failed: HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise SubmitError(f"tmpfile upload failed: {exc}") from exc

    link = data.get("downloadLinkEncoded") or data.get("downloadLink")
    if not isinstance(link, str) or not link:
        raise SubmitError(f"tmpfile response did not contain a download link: {data}")
    return link


def list_recent_workflow_runs() -> list[dict[str, Any]]:
    endpoint = f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW_ID}/runs?branch={REF}&event=workflow_dispatch&per_page=20"
    runs = gh_api_json([endpoint]).get("workflow_runs", [])
    if not isinstance(runs, list):
        raise SubmitError("unexpected GitHub workflow runs response")
    return [run for run in runs if isinstance(run, dict)]


def dispatch_workflow(bitstream_zip_url: str) -> int:
    known_run_ids = {
        int(run["id"])
        for run in list_recent_workflow_runs()
        if run.get("id") is not None
    }
    endpoint = f"/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW_ID}/dispatches"
    payload = {
        "ref": REF,
        "inputs": {"bitstream_zip_url": bitstream_zip_url},
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        payload_path = f.name
    try:
        body = run_gh_api(["--method", "POST", endpoint, "--input", payload_path])
    finally:
        Path(payload_path).unlink(missing_ok=True)

    if body:
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict):
            workflow_run = data.get("workflow_run")
            if isinstance(workflow_run, dict) and workflow_run.get("id") is not None:
                return int(workflow_run["id"])
            if data.get("id") is not None:
                return int(data["id"])
            run_url = data.get("url") or data.get("run_url")
            if isinstance(run_url, str):
                return int(run_url.rstrip("/").split("/")[-1])
    return find_recent_workflow_run(known_run_ids)


def find_recent_workflow_run(known_run_ids: set[int]) -> int:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        for run in list_recent_workflow_runs():
            run_id = run.get("id")
            if run_id is None:
                continue
            run_id = int(run_id)
            if run_id not in known_run_ids:
                return run_id
        time.sleep(5)
    raise SubmitError("could not find the dispatched workflow run")


def wait_for_run(run_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    endpoint = f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}"
    while time.monotonic() < deadline:
        run = gh_api_json([endpoint])
        status = run.get("status")
        conclusion = run.get("conclusion")
        log(f"workflow : status={status} conclusion={conclusion}")
        if status == "completed":
            return run
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SubmitError("timed out waiting for workflow run")


def download_result_artifact(run_id: int, temp_dir: Path) -> dict[str, Any]:
    endpoint = f"/repos/{OWNER}/{REPO}/actions/runs/{run_id}/artifacts?per_page=100"
    artifacts = gh_api_json([endpoint]).get("artifacts", [])
    artifact = next((item for item in artifacts if item.get("name") == RESULT_ARTIFACT_NAME), None)
    if artifact is None:
        names = ", ".join(str(item.get("name")) for item in artifacts)
        raise SubmitError(f"result artifact {RESULT_ARTIFACT_NAME!r} not found; artifacts: {names}")

    artifact_id = artifact.get("id")
    if artifact_id is None:
        raise SubmitError("result artifact did not include id")
    archive_bytes = run_gh_api([f"/repos/{OWNER}/{REPO}/actions/artifacts/{artifact_id}/zip"])
    artifact_zip = temp_dir / "result-artifact.zip"
    artifact_zip.write_bytes(archive_bytes)

    with zipfile.ZipFile(artifact_zip) as zf:
        try:
            with zf.open(RESULT_JSON_NAME) as result_file:
                return json.loads(result_file.read().decode("utf-8"))
        except KeyError as exc:
            raise SubmitError(f"{RESULT_JSON_NAME} not found in result artifact") from exc


def normalize_led_hex(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("hex") or value.get("bits")
    if value is None or value == "":
        return ""
    text = str(value).strip()
    if set(text) <= {"0", "1"} and len(text) == 32:
        return f"0x{int(text[::-1], 2) & 0xFFFFFFFF:08x}"
    try:
        return f"0x{int(text, 0) & 0xFFFFFFFF:08x}"
    except ValueError:
        return text


def print_public_result(run: dict[str, Any], result: dict[str, Any]) -> None:
    raw = result.get("result")
    if not isinstance(raw, dict):
        raw = {}
    error_text = raw.get("error") or result.get("error") or ""
    output = {
        "workflow_conclusion": run.get("conclusion"),
        "burned": raw.get("burned"),
        "seg": raw.get("parsed_result", ""),
        "led": normalize_led_hex(raw.get("led")),
        "has_error": bool(error_text),
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)


def print_error_result(error_text: str = "", *, debug: bool = False) -> None:
    output = {
        "workflow_conclusion": None,
        "burned": False,
        "seg": "",
        "led": "",
        "has_error": True,
    }
    if debug and error_text:
        output["error"] = error_text
    print(json.dumps(output, ensure_ascii=False), flush=True)


def submit_bitfile(script_dir: Path, bitfile: Path) -> int:
    password = read_secret(script_dir, "zip_password.txt")

    with tempfile.TemporaryDirectory(prefix="jyd-call-submit-") as temp:
        temp_dir = Path(temp)
        zip_path = create_encrypted_zip(bitfile, password, temp_dir)
        log("encrypted zip created")
        download_url = upload_tmpfile(zip_path)
        log("encrypted zip uploaded")
        run_id = dispatch_workflow(download_url)
        log("workflow run dispatched")
        run = wait_for_run(run_id)
        result = download_result_artifact(run_id, temp_dir)
        print_public_result(run, result)
        return 0 if run.get("conclusion") == "success" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit one JYD bitstream to the remote FPGA test workflow.")
    parser.add_argument("arg", nargs="?", help="'update' or the path to the top.bit file to submit.")
    parser.add_argument("--debug", action="store_true", help="show detailed submit errors on failure")
    args = parser.parse_args(argv)

    script_path = Path(__file__).resolve()
    script_dir = script_path.parent

    try:
        if args.arg == "update":
            update_self(script_path)
            return 0
        if not args.arg:
            parser.error("bitfile is required unless using the update subcommand")
        return submit_bitfile(script_dir, Path(args.arg).expanduser().resolve())
    except SubmitError as exc:
        error_text = str(exc)
        print_error_result(error_text, debug=args.debug)
        if args.debug:
            log(f"error occurred: {error_text}")
        else:
            log("error occurred")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
