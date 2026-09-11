"""Authenticated direct reads shared by the chat facade's fixed tools."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from agent.execution_boundary import execution_capability_scope
from agent.orchestration.policy_engine import PolicyEngine
from agent.tms_runtime.direct_execution import call_blocking
from shared.redaction import redact_sensitive, redact_text
from shared.identity_permissions import tool_permission


async def invoke_registered_reader(*, catalog, name, arguments, handler, actor, source,
                                   llm_selected=False, timeout_seconds=1800, invocations=None, identity_access=None):
    if identity_access is not None and not identity_access.allows(actor, tool_permission(name)):
        return {"success": False, "error_code": "PERMISSION_DENIED", "error": "当前身份没有该查询权限，请在后台检查绑定的身份。"}
    capability = catalog.get_capability(name)
    if not capability or capability.get("operation_type") not in {"read", "compute"}:
        return {"success": False, "error_code": "DIRECT_READ_NOT_AVAILABLE", "error": "该查询未开放"}
    if (capability.get("approval") or {}).get("mode") == "disabled":
        return {"success": False, "error_code": "TOOL_DISABLED", "error": "该查询已停用"}
    if llm_selected and capability.get("llm_exposed") is not True:
        return {"success": False, "error_code": "LLM_TOOL_NOT_ALLOWED", "error": "该查询不向模型开放"}
    required = (capability.get("permissions") or {}).get("required_roles", ())
    if actor is None or any(not PolicyEngine(catalog).can_decide(actor, required_role=role, source=source)
                            for role in required):
        return {"success": False, "error_code": "PERMISSION_DENIED", "error": "当前账号没有查询权限"}
    try:
        validated = catalog.validate_input(name, arguments)
    except (ValueError, TypeError, KeyError) as exc:
        return {"success": False, "error_code": "INVALID_TOOL_ARGUMENTS", "error": redact_text(exc)}
    timeout = min(float(timeout_seconds), float(capability["timeout"]))
    async def read():
        with execution_capability_scope(name, ttl_seconds=max(1, int(timeout)) + 30):
            return await call_blocking(handler, validated, timeout_sec=timeout)
    try:
        raw = await invocations.call_read(operation=name, handler=read) if invocations else await read()
    except asyncio.TimeoutError:
        return {"success": False, "error_code": "QUERY_TIMEOUT", "error": "本次查询超时，请重新查询"}
    except Exception as exc:
        return {"success": False, "error_code": getattr(exc, "code", "QUERY_FAILED"), "error": redact_text(exc)[:300]}
    if not isinstance(raw, Mapping) or not raw:
        return {"success": False, "error_code": "INVALID_QUERY_RESULT", "error": "查询未返回有效结果"}
    raw = redact_sensitive(dict(raw))
    unified = {"status", "data", "meta", "warnings", "error"}.issubset(raw)
    failed = (raw.get("error") or raw.get("ok") is False or raw.get("success") is False
              or str(raw.get("status", "")).upper() in {"FAILED", "ERROR", "AUTH_REQUIRED"})
    error = raw.get("error")
    return {"success": not bool(failed), "data": raw["data"] if unified else raw,
            "error": error.get("message") if isinstance(error, Mapping) else error,
            "error_code": error.get("code") if isinstance(error, Mapping) else raw.get("error_code")}
