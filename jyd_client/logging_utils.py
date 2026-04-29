from __future__ import annotations

from datetime import datetime
import sys


def log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", file=sys.stderr, flush=True)
