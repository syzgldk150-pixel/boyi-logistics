"""Private validation and formatting helpers for project policy orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from agent.automation_plugins.catalog import PluginCatalogEntry
from agent.orchestration.automation_project_service_v2 import (
    normalize_service_v2_module_slot_context,
)
from agent.orchestration.models import OrchestrationError
from agent.orchestration.policy_engine import ProjectPolicyEvaluation
from agent.orchestration.scan_preview_binding import SCAN_PREVIEW_CONTEXT_KEY
from agent.orchestration.selection_preview_binding import (
    SelectionPreviewExpectation,
    selection_preview_contribution,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectPolicyMode,
    CompiledAutomationProjectContract,
    canonical_sha256,
)


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


def _bootstrap_automation_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise OrchestrationError(
            "PROJECT_POLICY_BOOTSTRAP_SCOPE_INVALID",
            "Automation project bootstrap scope must be an identity list",
        )
    normalized = tuple(sorted(_automation_id(value) for value in values))
    if not normalized or len(normalized) != len(set(normalized)):
        raise OrchestrationError(
            "PROJECT_POLICY_BOOTSTRAP_SCOPE_INVALID",
            "Automation project bootstrap scope is empty or duplicated",
        )
    return normalized


def _bootstrap_project_is_stable(project: Mapping[str, Any] | None) -> bool:
    if not isinstance(project, Mapping):
        return False
    target = project.get("target_generation")
    committed = project.get("committed_generation")
    return bool(
        project.get("migration_authority") in {True, 1}
        and project.get("enabled") in {True, 1}
        and str(project.get("state") or "") == "ENABLED"
        and type(target) is int
        and type(committed) is int
        and target > 0
        and target == committed
        and str(project.get("reconcile_state") or "") == "STABLE"
    )


def _validate_bootstrap_schedule_set(
    entry: PluginCatalogEntry,
    contract: CompiledAutomationProjectContract,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    snapshot = entry.committed_snapshot
    metadata = snapshot.execution_metadata if snapshot is not None else None
    schedule = metadata.get("schedule") if isinstance(metadata, Mapping) else None
    if not isinstance(schedule, Mapping) or set(schedule) != {
        "kind",
        "times",
        "enabled",
    }:
        raise OrchestrationError(
            "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
            "Committed project schedule is invalid",
        )
    kind = schedule.get("kind")
    times = schedule.get("times")
    enabled = schedule.get("enabled")
    if (
        kind not in {"none", "daily_times", "startup"}
        or type(enabled) is not bool
        or not isinstance(times, list)
        or any(type(item) is not str for item in times)
    ):
        raise OrchestrationError(
            "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
            "Committed project schedule is invalid",
        )
    if kind == "none":
        expected_expressions: tuple[str, ...] = ()
        if times or enabled:
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
                "Committed none schedule is invalid",
            )
    elif kind == "startup":
        expected_expressions = ("@startup",)
        if times:
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
                "Committed startup schedule is invalid",
            )
    else:
        canonical_times = tuple(sorted(times))
        if (
            not canonical_times
            or tuple(times) != canonical_times
            or len(canonical_times) != len(set(canonical_times))
            or any(
                len(item) != 5
                or item[2] != ":"
                or not item[:2].isdigit()
                or not item[3:].isdigit()
                or not 0 <= int(item[:2]) <= 23
                or not 0 <= int(item[3:]) <= 59
                for item in canonical_times
            )
        ):
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
                "Committed daily schedule is invalid",
            )
        quarter_hour_times = tuple(
            f"{hour:02d}:{minute:02d}"
            for hour in range(24)
            for minute in (0, 15, 30, 45)
        )
        expected_expressions = (
            ("*/15 * * * *",)
            if canonical_times == quarter_hour_times
            else tuple(
                f"{int(item[3:])} {int(item[:2])} * * *" for item in canonical_times
            )
        )
    actual_expressions = tuple(
        str(row.get("cron_expression") or "")
        for row in sorted(rows, key=lambda item: str(item.get("cron_expression") or ""))
    )
    if (
        actual_expressions != tuple(sorted(expected_expressions))
        or len(actual_expressions) != len(set(actual_expressions))
        or any(bool(row.get("enabled")) is not enabled for row in rows)
    ):
        raise OrchestrationError(
            "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
            "Committed project task set differs from its schedule",
        )
    task_ids = [str(row.get("id") or "").strip() for row in rows]
    scheduled_snapshot = contract.snapshot.get("scheduled_configurations")
    if (
        any(not task_id for task_id in task_ids)
        or len(task_ids) != len(set(task_ids))
        or any(
            str(row.get("automation_id") or "") != contract.automation_id
            or row.get("automation_generation") != contract.automation_generation
            or row.get("configuration_version")
            != contract.project_configuration_version
            or str(row.get("tool_name") or "")
            != f"automation.{contract.automation_id}.run"
            or not isinstance(row.get("tool_params"), Mapping)
            or f"scheduler:{str(row.get('id') or '')}"
            not in contract.invocation_contracts
            or dict(row.get("tool_params") or {})
            != dict(
                contract.invocation_contracts[
                    f"scheduler:{str(row.get('id') or '')}"
                ].expected_arguments
            )
            for row in rows
        )
        or not isinstance(scheduled_snapshot, list)
        or {str(item.get("task_id") or "") for item in scheduled_snapshot}
        != set(task_ids)
        or {key for key in contract.invocation_contracts if key.startswith("scheduler:")}
        != {f"scheduler:{task_id}" for task_id in task_ids}
    ):
        raise OrchestrationError(
            "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
            "Compiled project task identities are incomplete",
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
        return f"当前项目合同不可授予完全自动（{safe_reason}），每次运行均需审批。"
    return "当前项目所有入口每次运行都需要审批。"


def _selection_preview_expectation(
    entry: PluginCatalogEntry,
    contract: CompiledAutomationProjectContract,
    *,
    entrypoint: str | None = None,
    contribution_id: str | None = None,
) -> SelectionPreviewExpectation:
    runtime_model = str(getattr(entry, "runtime_model", "ACTION_V1") or "ACTION_V1")
    if runtime_model != "SERVICE_V2":
        return SelectionPreviewExpectation(
            project_instance_id=entry.automation_id,
            plugin_id=entry.plugin_id,
            generation=contract.automation_generation,
            contract_digest=contract.contract_hash,
            configuration_version=contract.project_configuration_version,
        )
    safe_entrypoint = str(entrypoint or AutomationEntrypoint.CONSOLE.value).strip()
    declaration = selection_preview_contribution(entry, safe_entrypoint)
    signed_contribution_id = str((declaration or {}).get("id") or "").strip()
    if (
        safe_entrypoint
        not in {
            AutomationEntrypoint.CONSOLE.value,
            AutomationEntrypoint.FEISHU.value,
        }
        or not signed_contribution_id
        or (
            contribution_id is not None
            and signed_contribution_id != str(contribution_id).strip()
        )
    ):
        raise OrchestrationError(
            "SELECTION_PREVIEW_PROJECT_INVALID",
            "Service v2 selection contributions are incomplete or ambiguous",
            details={"status": "BLOCKED_DATA"},
        )
    title = str(
        (declaration or {}).get("title") or getattr(entry, "display_name", "") or ""
    ).strip()
    return SelectionPreviewExpectation(
        project_instance_id=entry.automation_id,
        plugin_id=entry.plugin_id,
        generation=contract.automation_generation,
        contract_digest=contract.contract_hash,
        configuration_version=contract.project_configuration_version,
        runtime_model="SERVICE_V2",
        entrypoint=safe_entrypoint,
        contribution_id=signed_contribution_id,
        title=title,
    )


def _selection_contribution_id(
    entry: PluginCatalogEntry,
    entrypoint: AutomationEntrypoint,
) -> str | None:
    if str(getattr(entry, "runtime_model", "ACTION_V1") or "ACTION_V1") != "SERVICE_V2":
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


def _idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 191:
        raise OrchestrationError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "A bounded stable idempotency key is required",
        )
    return key


def _comment(value: Any) -> str:
    comment = str(value or "").strip()
    if len(comment) > 500:
        raise OrchestrationError("COMMENT_TOO_LONG", "Comment exceeds 500 characters")
    return comment


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
