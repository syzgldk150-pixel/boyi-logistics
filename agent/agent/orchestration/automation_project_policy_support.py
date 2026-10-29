"""Private validation and formatting helpers for project policy orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime
from typing import Any

from agent.automation_plugins.catalog import PluginCatalogEntry
from agent.automation_plugins.catalog_read_scope import catalog_read_transaction
from agent.orchestration.automation_project_service_v2 import (
    normalize_service_v2_module_slot_context,
)
from agent.orchestration.models import OrchestrationError
from agent.orchestration.policy_engine import ProjectPolicyEvaluation
from agent.orchestration.scan_preview_binding import (
    SCAN_PREVIEW_CONTEXT_KEY,
)
from agent.orchestration.selection_preview_binding import (
    selection_preview_contribution,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectPolicyMode,
    canonical_sha256,
)


def scoped_policy_projection(repository, catalog, selected, describe_entry):
    """Keep scoped display reads within one catalog read transaction."""
    # Inside an active catalog read scope this reuses its connection and
    # snapshot; otherwise it opens its own unit of work as before.
    with catalog_read_transaction(repository) as uow:
        rows = uow.automation_projects.list_policies(automation_ids=tuple(sorted(selected))) if selected is not None else uow.automation_projects.list_policies()
        policies = {str(row.get("automation_id") or ""): row for row in rows}
    scope = getattr(catalog, "read_scope", None)
    with scope() if callable(scope) else nullcontext():
        entries = catalog.list() if selected is None else (catalog.get(key) for key in sorted(selected))
        items = [describe_entry(entry, policies.get(entry.automation_id)) for entry in entries if entry is not None]
    result = {"items": sorted(items, key=lambda item: item["automation_id"])}
    if selected is not None:
        result["missing_automation_ids"] = sorted(selected - {item["automation_id"] for item in items})
    return result


_TRUSTED_CONTEXT_FIELDS = {
    AutomationEntrypoint.CONSOLE: frozenset({"dynamic_inputs"}),
    AutomationEntrypoint.HARNESS: frozenset(),
    AutomationEntrypoint.SCHEDULER: frozenset(
        {
            "task_id",
            "scheduled_for",
            "cron_expression",
            "configuration_version",
        }
    ),
    AutomationEntrypoint.FEISHU: frozenset(
        {
            "route_id",
            "route_revision",
            "event_id",
            "chat_id",
            "dynamic_inputs",
        }
    ),
    AutomationEntrypoint.WEBHOOK: frozenset(
        {
            "route_id",
            "route_revision",
            "source_event_id",
            "webhook_path",
            "webhook_method",
            "dynamic_inputs",
        }
    ),
    AutomationEntrypoint.EVENTS: frozenset({"event_name", "source_event_id"}),
    AutomationEntrypoint.MODULE_SLOTS: frozenset({"module_slot", "dynamic_inputs"}),
}
_SERVER_CONTEXT_FIELDS = frozenset(
    {
        "project_request_id",
        "entrypoint",
        "occurred_at",
        "automation_id",
        "automation_generation",
        "automation_invocation",
        "_automation_project_invocation",
        "contract_id",
        "contribution_id",
        "contract_hash",
        "policy_version",
        "project_configuration_version",
        "tool_name",
        "arguments",
        "source",
        "actor",
        "roles",
        SCAN_PREVIEW_CONTEXT_KEY,
    }
)


def _pending_set_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    identities = []
    for row in sorted(rows, key=lambda item: str(item.get("approval_id") or "")):
        identities.append(
            {
                "approval_id": str(row.get("approval_id") or ""),
                "run_id": str(row.get("run_id") or ""),
                "approval_round": int(row.get("approval_round") or 0),
                "plan_hash": str(row.get("plan_hash") or ""),
                "current_plan_hash": str(row.get("current_plan_hash") or ""),
                "risk_level": str(row.get("risk_level") or ""),
                "required_role": str(row.get("required_role") or ""),
                "expires_at": _datetime_text(row.get("expires_at")),
                "source": str(row.get("source") or ""),
                "invocation_sha256": canonical_sha256(
                    row.get("automation_invocation_json")
                ),
            }
        )
    return canonical_sha256(identities)


def _project_denied(code: str, reason: str) -> ProjectPolicyEvaluation:
    return ProjectPolicyEvaluation(
        allowed=False,
        requires_approval=False,
        code=code,
        reason=reason,
    )


def _policy_summary(
    *,
    configured: str,
    effective: str,
    status: str,
    reason: str | None,
) -> str:
    if effective == AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value:
        if status == "RECONCILING":
            return "完全自动，运行环境同步中；同步完成前不会运行旧配置。"
        if status in {"UNAVAILABLE", "UNSUPPORTED"}:
            safe_reason = str(reason or "PROJECT_RUNTIME_UNAVAILABLE")[:64]
            return f"完全自动意图已保留，但运行环境不可用，需修复后运行（{safe_reason}）。"
        return "当前项目为完全自动；每次运行仍校验签名、配置、入口和写后证据。"
    if effective == AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value:
        return "仍处于旧版计划权限；保存新的项目权限后由项目统一接管。"
    if status == "UNSUPPORTED":
        safe_reason = str(reason or "PROJECT_CONTRACT_UNAVAILABLE")[:64]
        return f"当前项目合同不可授予完全自动（{safe_reason}），仅允许有权限的管理员手动执行。"
    return "当前项目仅允许有权限的管理员在后台手动执行；自动触发会直接返回未授权。"


def _selection_contribution_id(
    entry: PluginCatalogEntry,
    entrypoint: AutomationEntrypoint,
) -> str | None:
    if str(getattr(entry, "runtime_model", None) or "") != "SERVICE_V2":
        return None
    declaration = selection_preview_contribution(entry, entrypoint.value)
    contribution_id = str((declaration or {}).get("id") or "").strip()
    if not contribution_id:
        raise OrchestrationError(
            "PROJECT_ENTRYPOINT_DISABLED",
            "Requested entrypoint has no signed selection contribution",
            details={"status": "BLOCKED_DATA"},
        )
    return contribution_id


def _automation_id(value: Any) -> str:
    automation_id = str(value or "").strip()
    if not automation_id or len(automation_id) > 128 or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:@-"
        for character in automation_id
    ):
        raise OrchestrationError(
            "AUTOMATION_PROJECT_NOT_FOUND",
            "Automation project identity is invalid",
        )
    return automation_id


def policy_list_automation_ids(values: Sequence[str]) -> frozenset[str]:
    """Validate an exact read-only projection scope, never an execution grant."""
    if isinstance(values, (str, bytes)) or not 1 <= len(values) <= 1000:
        raise OrchestrationError("PROJECT_LIST_FILTER_INVALID", "Expected 1 to 1000 exact automation identities")
    try:
        validated = [_automation_id(value) for value in values]
    except OrchestrationError as exc:
        raise OrchestrationError("PROJECT_LIST_FILTER_INVALID", "Invalid automation identity filter") from exc
    if list(values) != validated or len(set(validated)) != len(validated):
        raise OrchestrationError("PROJECT_LIST_FILTER_INVALID", "Automation identity filter contains whitespace or duplicates")
    return frozenset(validated)


def _request_id(value: Any) -> str:
    request_id = str(value or "").strip()
    if not request_id or len(request_id) > 191:
        raise OrchestrationError(
            "REQUEST_ID_REQUIRED",
            "A stable request id is required",
        )
    return request_id


def _entrypoint(value: AutomationEntrypoint | str) -> AutomationEntrypoint:
    try:
        return value if isinstance(value, AutomationEntrypoint) else AutomationEntrypoint(value)
    except (TypeError, ValueError) as exc:
        raise OrchestrationError(
            "PROJECT_ENTRYPOINT_INVALID",
            "Automation project entrypoint is invalid",
        ) from exc


def _trusted_context(
    entrypoint: AutomationEntrypoint,
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        context: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        context = dict(value)
    else:
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Trusted entrypoint context must be a JSON object",
        )
    allowed = _TRUSTED_CONTEXT_FIELDS[entrypoint]
    if set(context) - allowed or set(context) & _SERVER_CONTEXT_FIELDS:
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Trusted entrypoint context contains unsupported fields",
        )
    dynamic_inputs = context.get("dynamic_inputs")
    if dynamic_inputs is not None and not isinstance(dynamic_inputs, Mapping):
        raise OrchestrationError(
            "TRUSTED_CONTEXT_INVALID",
            "Trusted dynamic inputs must be a JSON object",
        )
    if entrypoint is AutomationEntrypoint.MODULE_SLOTS:
        return normalize_service_v2_module_slot_context(context)
    return context


def _positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise OrchestrationError(
            "PROJECT_VERSION_REQUIRED",
            f"{field_name} must be a positive integer",
        )
    return value


def _datetime_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
