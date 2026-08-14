"""Risk, role, approval, and exact scheduler-allowlist decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.orchestration.models import (
    Actor,
    ActorType,
    ApprovalMode,
    OperationType,
    OrchestrationError,
    Plan,
    RiskLevel,
    sha256_json,
)
from agent.orchestration.ports import ToolCatalogPort


logger = logging.getLogger("agent")


@dataclass(frozen=True)
class ScheduledAllowlistEntry:
    task_id: str
    tool_name: str
    tool_version: str
    arguments_hash: str
    dynamic_argument_rules: Mapping[str, str] = field(default_factory=dict)
    cron_expression: str = ""
    contract_hash: str = ""
    configuration_version: int | None = None

    @classmethod
    def from_arguments(
        cls,
        *,
        task_id: str,
        tool_name: str,
        tool_version: str,
        arguments: Mapping[str, Any],
        dynamic_argument_rules: Mapping[str, str] | None = None,
        cron_expression: str = "",
        contract_hash: str = "",
        configuration_version: int | None = None,
    ) -> "ScheduledAllowlistEntry":
        return cls(
            task_id=task_id,
            tool_name=tool_name,
            tool_version=tool_version,
            arguments_hash=sha256_json(arguments),
            dynamic_argument_rules=dict(dynamic_argument_rules or {}),
            cron_expression=str(cron_expression or ""),
            contract_hash=str(contract_hash or ""),
            configuration_version=(
                int(configuration_version)
                if configuration_version is not None
                else None
            ),
        )

    def matches(
        self,
        *,
        step: Any,
        execution_context: Mapping[str, Any],
    ) -> bool:
        arguments = dict(step.arguments)
        if self.configuration_version is not None:
            actual_configuration_version = execution_context.get("configuration_version")
            if (
                isinstance(actual_configuration_version, bool)
                or not isinstance(actual_configuration_version, int)
                or actual_configuration_version != self.configuration_version
            ):
                return False
        if self.cron_expression and str(execution_context.get("cron_expression") or "") != self.cron_expression:
            return False
        if self.cron_expression == "@startup":
            scheduled_for = _parse_scheduled_for(execution_context.get("scheduled_for"))
            if scheduled_for is None or not _matches_startup_occurrence(scheduled_for):
                return False
        elif self.cron_expression:
            scheduled_for = _parse_scheduled_for(execution_context.get("scheduled_for"))
            if scheduled_for is None or not _matches_daily_cron(scheduled_for, self.cron_expression):
                return False
        for field_name, rule in self.dynamic_argument_rules.items():
            actual = arguments.pop(field_name, None)
            if rule == "scheduled_previous_day":
                scheduled_for = _parse_scheduled_for(execution_context.get("scheduled_for"))
                if scheduled_for is None:
                    return False
                expected = (scheduled_for.date() - timedelta(days=1)).isoformat()
                if actual != expected:
                    return False
            else:
                return False
        return self.arguments_hash == sha256_json(arguments)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    required_role: str | None
    risk_level: RiskLevel
    code: str
    reason: str


class PolicyEngine:
    def __init__(
        self,
        catalog: ToolCatalogPort,
        *,
        scheduler_allowlist: Sequence[ScheduledAllowlistEntry] = (),
        scheduler_allowlist_provider: Callable[[], Sequence[ScheduledAllowlistEntry]] | None = None,
    ) -> None:
        self._catalog = catalog
        self._scheduler_allowlist = tuple(scheduler_allowlist)
        self._scheduler_allowlist_provider = scheduler_allowlist_provider

    def evaluate(
        self,
        plan: Plan,
        actor: Actor,
        *,
        source: str,
        execution_context: Mapping[str, Any] | None = None,
    ) -> PolicyDecision:
        execution_context = dict(execution_context or {})
        highest_risk = RiskLevel.LOW
        required_roles: set[str] = set()
        requires_approval = False

        for step in plan.steps:
            capability = self._catalog.get_capability(step.tool_name)
            if capability is None:
                raise OrchestrationError("UNKNOWN_TOOL", f"Unknown tool: {step.tool_name}")
            approval = capability.get("approval") or {}
            if not isinstance(approval, Mapping):
                raise OrchestrationError("INVALID_APPROVAL_CONTRACT", f"Invalid approval contract: {step.tool_name}")
            try:
                mode = ApprovalMode(str(approval.get("mode") or "none"))
            except ValueError as exc:
                raise OrchestrationError("INVALID_APPROVAL_MODE", f"Invalid approval mode: {step.tool_name}") from exc

            permissions = capability.get("permissions") or {}
            if not isinstance(permissions, Mapping):
                raise OrchestrationError(
                    "INVALID_PERMISSION_CONTRACT",
                    f"Invalid permission contract: {step.tool_name}",
                )
            raw_required_roles = permissions.get("required_roles")
            if not isinstance(raw_required_roles, list) or not raw_required_roles:
                raise OrchestrationError(
                    "INVALID_PERMISSION_CONTRACT",
                    f"Tool permissions require a non-empty role list: {step.tool_name}",
                )
            tool_roles = {str(role).strip() for role in raw_required_roles if str(role).strip()}
            if not tool_roles:
                raise OrchestrationError(
                    "INVALID_PERMISSION_CONTRACT",
                    f"Tool permissions require a non-empty role list: {step.tool_name}",
                )

            highest_risk = _max_risk(highest_risk, step.risk_level)
            if mode is ApprovalMode.DISABLED or step.operation_type is OperationType.DESTRUCTIVE or step.risk_level is RiskLevel.EXTREME:
                return PolicyDecision(
                    allowed=False,
                    requires_approval=False,
                    required_role=None,
                    risk_level=step.risk_level,
                    code="OPERATION_DISABLED",
                    reason=f"Operation is disabled by tool policy: {step.tool_name}",
                )
            if step.operation_type in {OperationType.READ, OperationType.COMPUTE} and mode is ApprovalMode.NONE:
                if actor.actor_type in {ActorType.CONSOLE_ADMIN, ActorType.LEGACY_API} and not _has_tool_role(
                    actor.roles,
                    tool_roles,
                ):
                    return PolicyDecision(
                        allowed=False,
                        requires_approval=False,
                        required_role=None,
                        risk_level=step.risk_level,
                        code="TOOL_PERMISSION_DENIED",
                        reason=f"Actor lacks a required tool role: {step.tool_name}",
                    )
                continue

            if step.operation_type is OperationType.EXTERNAL_WRITE:
                if (
                    mode is ApprovalMode.SCHEDULE_ALLOWLIST
                    and self._scheduled_allowlisted(step, actor, source, execution_context)
                ):
                    continue
                requires_approval = True
                required_roles.add("super_admin")
                continue
            if step.operation_type is OperationType.FINANCIAL_WRITE:
                if self._scheduled_allowlisted(step, actor, source, execution_context):
                    continue
                requires_approval = True
                required_roles.add("super_admin")
                continue
            if step.operation_type is OperationType.INTERNAL_PROJECTION_WRITE:
                if self._scheduled_allowlisted(step, actor, source, execution_context):
                    continue
                requires_approval = True
                required_roles.add(str(approval.get("required_role") or "admin"))
                continue
            if mode is ApprovalMode.REQUIRED:
                requires_approval = True
                required_roles.add(str(approval.get("required_role") or "admin"))
            elif mode is ApprovalMode.SCHEDULE_ALLOWLIST:
                if not self._scheduled_allowlisted(step, actor, source, execution_context):
                    requires_approval = True
                    required_roles.add(str(approval.get("required_role") or "admin"))

        required_role = "super_admin" if "super_admin" in required_roles else ("admin" if required_roles else None)
        return PolicyDecision(
            allowed=True,
            requires_approval=requires_approval,
            required_role=required_role,
            risk_level=highest_risk,
            code="APPROVAL_REQUIRED" if requires_approval else "ALLOWED",
            reason="A separate approval decision is required" if requires_approval else "Plan is allowed by policy",
        )

    def can_decide(self, actor: Actor, *, required_role: str, source: str) -> bool:
        if actor.actor_type is not ActorType.CONSOLE_ADMIN or source != "console":
            return False
        if required_role == "super_admin":
            return "super_admin" in actor.roles
        if required_role == "admin":
            return bool({"admin", "super_admin"}.intersection(actor.roles))
        return False

    def _scheduled_allowlisted(
        self,
        step,
        actor: Actor,
        source: str,
        execution_context: Mapping[str, Any],
    ) -> bool:
        if source != "scheduler" or actor.actor_type is not ActorType.SCHEDULER:
            return False
        task_id = str(execution_context.get("task_id") or "").strip()
        if not task_id or actor.actor_id != task_id:
            return False
        entries = self._scheduler_allowlist
        if self._scheduler_allowlist_provider is not None:
            try:
                provided = tuple(self._scheduler_allowlist_provider())
            except Exception:
                logger.warning("Persisted scheduler allowlist unavailable; evaluation failed closed")
                provided = ()
            entries += tuple(entry for entry in provided if isinstance(entry, ScheduledAllowlistEntry))
        return any(
            entry.task_id == task_id
            and entry.tool_name == step.tool_name
            and entry.tool_version == step.tool_version
            and entry.matches(step=step, execution_context=execution_context)
            for entry in entries
        )


def _max_risk(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.EXTREME: 3}
    return left if order[left] >= order[right] else right


def _has_tool_role(actor_roles: Sequence[str], required_roles: set[str]) -> bool:
    effective_roles = {str(role).strip() for role in actor_roles if str(role).strip()}
    if "super_admin" in effective_roles:
        effective_roles.add("admin")
    return bool(effective_roles.intersection(required_roles))


def _parse_scheduled_for(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _matches_daily_cron(scheduled_for: datetime, cron_expression: str) -> bool:
    parts = cron_expression.split()
    if len(parts) != 5 or parts[2:] != ["*", "*", "*"]:
        return False
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except ValueError:
        return False
    local = scheduled_for.astimezone(ZoneInfo("Asia/Shanghai"))
    return (
        local.hour == hour
        and local.minute == minute
        and local.second == 0
        and local.microsecond == 0
    )


def _matches_startup_occurrence(scheduled_for: datetime) -> bool:
    local = scheduled_for.astimezone(ZoneInfo("Asia/Shanghai"))
    return (
        local.hour == 0
        and local.minute == 10
        and local.second == 0
        and local.microsecond == 0
    )
