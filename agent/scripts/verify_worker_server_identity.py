#!/usr/bin/env python3
"""Fail-closed preflight for the online Windows Worker command signer."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from agent.windows_worker.release_preflight import verify_worker_server_identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    verify_worker_server_identity(arguments.environment_file)
    print("status=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
