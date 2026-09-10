"""Console transport for direct, signed business operations."""

from __future__ import annotations

import uuid
from typing import Any


def call_business(app: Any, operation: str, params: dict[str, Any], *,
                  trusted_context: dict[str, Any], request_id: str = "", timeout_sec: int = 180,
                  write: bool = False) -> dict[str, Any]:
    principal = trusted_context.get("_console_principal")
    if not isinstance(principal, dict):
        return {"ok": False, "status": 403, "error": "需要真实管理员会话。"}
    normalized_id = app._normalize_browser_request_uuid(request_id)
    if write and not normalized_id:
        return {"ok": False, "status": 400, "error_code": "BROWSER_REQUEST_UUID_REQUIRED",
                "error": "缺少有效且稳定的请求标识，本次操作未执行。"}
    return app._agent_request("POST", f"/internal/v1/business/{operation}",
        payload={"params": params, "request_id": normalized_id or str(uuid.uuid4()), "timeout_sec": timeout_sec},
        timeout=max(timeout_sec + 15, app.settings.agent_timeout_seconds), console_principal=principal)
