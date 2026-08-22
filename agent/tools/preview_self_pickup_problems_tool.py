"""Read-only self-pickup problem-item preview."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

from tools.problem_preview_contract import PreviewRunner, run_read_only_preview
from tools.self_pickup_problem_upload_tool import run_self_pickup_problem_upload


def preview_self_pickup_problems(
    arguments: Mapping[str, Any],
    *,
    runner: PreviewRunner = run_self_pickup_problem_upload,
) -> dict[str, Any]:
    return run_read_only_preview(
        arguments,
        tool_name="preview_self_pickup_problems",
        runner=runner,
        account_fields=("account_id", "daxiang_s_account_id"),
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        payload = {}
    result = preview_self_pickup_problems(payload if isinstance(payload, dict) else {})
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
