"""Ephemeral capabilities for nested calls made by governed tool executions."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from agent.connector_namespaces import DAILY_SIGN_TOOL


EXECUTION_CAPABILITY_HEADER = "X-Agent-Execution-Capability"
EXECUTION_CAPABILITY_ENV = "AGENT_EXECUTION_CAPABILITY"


@dataclass(frozen=True)
class _Capability:
    owner_tool: str
    expires_at: float


_LOCK = threading.RLock()
_CAPABILITIES: dict[str, _Capability] = {}
_CURRENT_CAPABILITY: ContextVar[str] = ContextVar("agent_execution_capability", default="")


_READ_ONLY_TMS_TARGETS = frozenset(
    {
        "delivery_status",
        "query_waybill_detail",
        "ronghui_tms_tracking",
        "tracking_query",
        "waybill_tracking",
        "yunda_waybill_tracking",
    }
)


TOOL_ALLOWED_TMS_TARGETS: dict[str, frozenset[str]] = {
    "query_waybill": frozenset({"delivery_status", "waybill_tracking"}),
    "track_waybill": frozenset({"query_waybill_detail", "tracking_query"}),
    "tms_query": _READ_ONLY_TMS_TARGETS,
    # The daily-sign Connector primitives read these TMS targets directly.
    DAILY_SIGN_TOOL: frozenset(
        {
            "customer_service_problem",
            "get_qianshou",
            "get_sign_records",
            "query_waybill_detail",
            "ronghui_tms_tracking",
        }
    ),
    "receipts_sync": frozenset({"receipts_sync"}),
    "receipts_audit": frozenset({"receipts_audit"}),
    "customer_service_problem_query": frozenset({"customer_service_problem"}),
    "customer_service_problem_detail": frozenset({"customer_service_problem"}),
    "customer_service_problem_fetch_attachment": frozenset({"customer_service_problem"}),
    "customer_service_problem_mark_read": frozenset({"customer_service_problem"}),
    "customer_service_problem_reply": frozenset({"customer_service_problem"}),
    "customer_service_problem_publish": frozenset({"customer_service_problem"}),
    "customer_service_problem_upload_attachment": frozenset({"customer_service_problem"}),
}


_ACTION_SCOPED_TMS_TARGETS = frozenset({"customer_service_problem"})


_TOOL_ALLOWED_TMS_ACTIONS: dict[tuple[str, str], frozenset[str]] = {
    (DAILY_SIGN_TOOL, "customer_service_problem"): frozenset({"query"}),
    ("customer_service_problem_query", "customer_service_problem"): frozenset({"query"}),
    ("customer_service_problem_detail", "customer_service_problem"): frozenset({"detail"}),
    ("customer_service_problem_fetch_attachment", "customer_service_problem"): frozenset(
        {"fetch_attachment"}
    ),
    ("customer_service_problem_mark_read", "customer_service_problem"): frozenset({"mark_read"}),
    ("customer_service_problem_reply", "customer_service_problem"): frozenset({"reply"}),
    ("customer_service_problem_publish", "customer_service_problem"): frozenset({"publish"}),
    ("customer_service_problem_upload_attachment", "customer_service_problem"): frozenset(
        {"upload_attachment"}
    ),
}


def issue_execution_capability(tool_name: str, *, ttl_seconds: float) -> str:
    owner_tool = str(tool_name or "").strip()
    if not owner_tool:
        raise ValueError("tool_name is required")
    token = secrets.token_urlsafe(32)
    capability = _Capability(
        owner_tool=owner_tool,
        expires_at=time.monotonic() + max(float(ttl_seconds), 1.0),
    )
    with _LOCK:
        _remove_expired_locked()
        _CAPABILITIES[token] = capability
    return token


def revoke_execution_capability(token: str) -> None:
    with _LOCK:
        _CAPABILITIES.pop(str(token or ""), None)


def current_execution_capability() -> str:
    return _CURRENT_CAPABILITY.get()


@contextmanager
def execution_capability_scope(tool_name: str, *, ttl_seconds: float) -> Iterator[str]:
    """Authorize nested HTTP calls made by an in-process tool adapter."""

    capability = issue_execution_capability(tool_name, ttl_seconds=ttl_seconds)
    context_token = _CURRENT_CAPABILITY.set(capability)
    try:
        yield capability
    finally:
        _CURRENT_CAPABILITY.reset(context_token)
        revoke_execution_capability(capability)


def authorize_tms_target(
    token: str,
    target_name: str,
    *,
    request_params: Mapping[str, object] | None = None,
) -> bool:
    safe_token = str(token or "").strip()
    target = str(target_name or "").strip().removeprefix("/tms/").removeprefix("/")
    if not safe_token or not target:
        return False
    with _LOCK:
        _remove_expired_locked()
        capability = _CAPABILITIES.get(safe_token)
    if capability is None:
        return False
    allowed = TOOL_ALLOWED_TMS_TARGETS.get(capability.owner_tool, frozenset())
    if target not in allowed:
        return False
    if target not in _ACTION_SCOPED_TMS_TARGETS:
        return True
    allowed_actions = _TOOL_ALLOWED_TMS_ACTIONS.get(
        (capability.owner_tool, target),
        frozenset(),
    )
    if not allowed_actions:
        return False
    if not isinstance(request_params, Mapping):
        return False
    action = str(request_params.get("action") or "").strip().lower()
    return action in allowed_actions


def _remove_expired_locked() -> None:
    now = time.monotonic()
    for token, capability in list(_CAPABILITIES.items()):
        if capability.expires_at <= now:
            _CAPABILITIES.pop(token, None)
