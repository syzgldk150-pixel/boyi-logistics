"""自提到货问题件工具封装。"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from tools.phase7_sync_common import tms_auth_error_result
from tools.tms_tool import call_http_service


UPLOAD_TIMEOUT_SEC = 7200


def _bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def run_self_pickup_problem_upload(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    required_accounts = ["account_id"]
    if _bool_param(params, "include_daxiang_s_self_pickup", True):
        required_accounts.append("daxiang_s_account_id")
    missing = [field for field in required_accounts if not str(params.get(field) or "").strip()]
    if missing:
        message = f"项目设置必须显式绑定账号：{', '.join(missing)}"
        return {
            "ok": False,
            "stage": "blocked_config",
            "error": message,
            "message": message,
        }
    result = call_http_service(
        "/self_pickup_problem_upload",
        {
            "params": params,
            "timeout_sec": int(params.get("timeout_sec") or UPLOAD_TIMEOUT_SEC),
            "client_timeout_sec": int(params.get("timeout_sec") or UPLOAD_TIMEOUT_SEC) + 30,
        },
    )
    if auth_error := tms_auth_error_result(result):
        return auth_error
    if not isinstance(result, dict):
        return {"error": f"self_pickup_problem_upload 返回异常: {result}"}
    if result.get("error"):
        return {
            "ok": False,
            "stage": "failed",
            "error": result.get("error"),
            "message": str(result.get("error")),
            "raw": result,
        }
    payload = result.get("data") if isinstance(result.get("data"), dict) else result
    if not isinstance(payload, dict):
        return {"error": f"self_pickup_problem_upload 返回格式异常: {result}"}
    return payload


def main() -> None:
    raw = sys.stdin.read()
    params = json.loads(raw) if raw.strip() else {}
    result = run_self_pickup_problem_upload(params)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
