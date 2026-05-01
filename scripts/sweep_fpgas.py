#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    from jyd_client.cli import main as cli_main

    return cli_main(["sweep-fpgas", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
