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
GITHUB_API = "https://api.github.com"
RESULT_ARTIFACT_NAME = "fpga-test-result"
RESULT_JSON_NAME = "result.json"
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 60 * 60


class SubmitError(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def read_secret(script_dir: Path, name: str) -> str:
    path = script_dir / "secrets" / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise SubmitError(f"secret file not found: {path}") from exc
    if not value:
        raise SubmitError(f"secret file is empty: {path}")
    return value


def github_request(
    token: str,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    accept: str = "application/vnd.github+json",
) -> tuple[int, bytes, dict[str, str]]:
    data = None
    headers = {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jyd-call-submit",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SubmitError(f"GitHub API {method} {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SubmitError(f"GitHub API {method} {url} failed: {exc}") from exc


def update_self(script_path: Path, token: str) -> None:
    url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/contents/{REMOTE_SCRIPT_PATH}?ref={urllib.parse.quote(REF)}"
    status, body, _ = github_request(token, "GET", url)
    if status != 200:
        raise SubmitError(f"unexpected GitHub contents status: {status}")
    data = json.loads(body.decode("utf-8"))
    if data.get("encoding") != "base64" or "content" not in data:
        raise SubmitError("unexpected GitHub contents response for call_submit.py")

    content = base64.b64decode(data["content"])
    tmp_path = script_path.with_name(f".{script_path.name}.{os.getpid()}.tmp")
    tmp_path.write_bytes(content)
    tmp_path.chmod(script_path.stat().st_mode)
    os.replace(tmp_path, script_path)
    log(f"updated {script_path} from {OWNER}/{REPO}@{REF}:{REMOTE_SCRIPT_PATH}")


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


def dispatch_workflow(token: str, bitstream_zip_url: str) -> int:
    url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW_ID}/dispatches"
    payload = {
        "ref": REF,
        "inputs": {"bitstream_zip_url": bitstream_zip_url},
        "return_run_details": True,
    }
    status, body, _ = github_request(token, "POST", url, payload=payload)
    if status == 200 and body:
        data = json.loads(body.decode("utf-8"))
        workflow_run = data.get("workflow_run")
        if isinstance(workflow_run, dict) and workflow_run.get("id") is not None:
            return int(workflow_run["id"])
        if data.get("id") is not None:
            return int(data["id"])
        run_url = data.get("url") or data.get("run_url")
        if isinstance(run_url, str):
            run_id = int(run_url.rstrip("/").split("/")[-1])
            return run_id
    if status != 204:
        raise SubmitError(f"unexpected workflow dispatch status: {status}")
    return find_recent_workflow_run(token)


def find_recent_workflow_run(token: str) -> int:
    deadline = time.monotonic() + 120
    url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW_ID}/runs?branch={REF}&event=workflow_dispatch&per_page=10"
    while time.monotonic() < deadline:
        _, body, _ = github_request(token, "GET", url)
        runs = json.loads(body.decode("utf-8")).get("workflow_runs", [])
        if runs:
            return int(runs[0]["id"])
        time.sleep(5)
    raise SubmitError("could not find the dispatched workflow run")


def wait_for_run(token: str, run_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runs/{run_id}"
    while time.monotonic() < deadline:
        _, body, _ = github_request(token, "GET", url)
        run = json.loads(body.decode("utf-8"))
        status = run.get("status")
        conclusion = run.get("conclusion")
        log(f"workflow run {run_id}: status={status} conclusion={conclusion}")
        if status == "completed":
            return run
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SubmitError(f"timed out waiting for workflow run {run_id}")


def download_result_artifact(token: str, run_id: int, temp_dir: Path) -> dict[str, Any]:
    url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runs/{run_id}/artifacts?per_page=100"
    _, body, _ = github_request(token, "GET", url)
    artifacts = json.loads(body.decode("utf-8")).get("artifacts", [])
    artifact = next((item for item in artifacts if item.get("name") == RESULT_ARTIFACT_NAME), None)
    if artifact is None:
        names = ", ".join(str(item.get("name")) for item in artifacts)
        raise SubmitError(f"result artifact {RESULT_ARTIFACT_NAME!r} not found; artifacts: {names}")

    archive_url = artifact.get("archive_download_url")
    if not isinstance(archive_url, str):
        raise SubmitError("result artifact did not include archive_download_url")
    _, archive_bytes, _ = github_request(token, "GET", archive_url, accept="application/vnd.github+json")
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
    output = {
        "workflow_run_id": run.get("id"),
        "workflow_url": run.get("html_url"),
        "workflow_conclusion": run.get("conclusion"),
        "burned": raw.get("burned"),
        "seg": raw.get("parsed_result", ""),
        "led": normalize_led_hex(raw.get("led")),
        "error": raw.get("error") or result.get("error") or "",
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)


def submit_bitfile(script_dir: Path, bitfile: Path) -> int:
    token = read_secret(script_dir, "gh_token.txt")
    password = read_secret(script_dir, "zip_password.txt")

    with tempfile.TemporaryDirectory(prefix="jyd-call-submit-") as temp:
        temp_dir = Path(temp)
        zip_path = create_encrypted_zip(bitfile, password, temp_dir)
        log(f"created encrypted zip: {zip_path}")
        download_url = upload_tmpfile(zip_path)
        log(f"uploaded encrypted zip: {download_url}")
        run_id = dispatch_workflow(token, download_url)
        log(f"dispatched workflow run: https://github.com/{OWNER}/{REPO}/actions/runs/{run_id}")
        run = wait_for_run(token, run_id)
        result = download_result_artifact(token, run_id, temp_dir)
        print_public_result(run, result)
        return 0 if run.get("conclusion") == "success" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit one JYD bitstream to the remote FPGA test workflow.")
    parser.add_argument("arg", nargs="?", help="'update' or the path to the top.bit file to submit.")
    args = parser.parse_args(argv)

    script_path = Path(__file__).resolve()
    script_dir = script_path.parent

    try:
        if args.arg == "update":
            update_self(script_path, read_secret(script_dir, "gh_token.txt"))
            return 0
        if not args.arg:
            parser.error("bitfile is required unless using the update subcommand")
        return submit_bitfile(script_dir, Path(args.arg).expanduser().resolve())
    except SubmitError as exc:
        print(json.dumps({"burned": False, "seg": "", "led": "", "error": str(exc)}, ensure_ascii=False))
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
