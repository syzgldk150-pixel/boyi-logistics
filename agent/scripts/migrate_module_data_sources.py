"""Explicit deployment-time source import; does not load dotenv or change schedules."""

from __future__ import annotations

import argparse
import json
import os
import sys

from agent.workflow_resource_store import _connect
from shared.data_source_migration import import_legacy_finance_sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist verified legacy identities; otherwise only inspect.")
    args = parser.parse_args()
    for name in ("AGENT_DB_HOST", "AGENT_DB_PORT", "AGENT_DB_USER", "AGENT_DB_NAME"):
        if not os.getenv(name):
            parser.error(f"{name} must be supplied explicitly in the process environment")
    with _connect() as connection:
        connection.autocommit(False)
        result = import_legacy_finance_sources(connection, apply=args.apply)
        if args.apply:
            connection.commit()
        else:
            connection.rollback()
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 1 if result["unresolved"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
