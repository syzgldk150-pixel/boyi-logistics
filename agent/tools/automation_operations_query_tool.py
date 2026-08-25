"""Fail-closed placeholder for the composition-root automation operations runner."""

from __future__ import annotations

import json
import sys


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    print(
        json.dumps(
            {
                "status": "FAILED",
                "data": {},
                "meta": {},
                "warnings": [],
                "error": {
                    "code": "AUTOMATION_OPERATIONS_COMPOSITION_REQUIRED",
                    "message": "Automation operations queries must use the Agent composition-root runner.",
                    "retryable": False,
                },
            },
            ensure_ascii=False,
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
