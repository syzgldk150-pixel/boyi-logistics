"""Pure control-plane models and state-machine invariants.

This module deliberately has no database, HTTP, tool, or environment imports.
Persisted v1 and v2 plans retain their schema-specific hash semantics so plan
hashes remain stable across processes, upgrades, and service restarts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from shared.automation_project_authorization import AutomationProjectInvocation


PLAN_SCHEMA_VERSION = 2
SUPPORTED_PLAN_SCHEMA_VERSIONS = frozenset({1, PLAN_SCHEMA_VERSION})
RESERVED_AUTOMATION_CONTEXT_FIELDS = frozenset(
    {
        "automation_id",
        "automation_generation",
        "automation_invocation",
        "contract_id",
        "contract_hash",
        "policy_version",
        "project_configuration_version",
        "_automation_project_invocation",
    }
)


class OrchestrationError(RuntimeError):
    """Base error carrying a stable API-safe error code."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


class StateConflictError(OrchestrationError):
    pass


class ActorType(str, Enum):
    CONSOLE_ADMIN = "console_admin"
    FEISHU_USER = "feishu_user"
    SCHEDULER = "scheduler"
    WEBHOOK = "webhook"
    EVENT = "event"
    SYSTEM = "system"
    LEGACY_API = "legacy_api"


class OperationType(str, Enum):
    READ = "read"
    COMPUTE = "compute"
    INTERNAL_PROJECTION_WRITE = "internal_projection_write"
    EXTERNAL_WRITE = "external_write"
    FINANCIAL_WRITE = "financial_write"
    DESTRUCTIVE = "destructive"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"


class ApprovalMode(str, Enum):
    NONE = "none"
    REQUIRED = "required"
    SCHEDULE_ALLOWLIST = "schedule_allowlist"
    DISABLED = "disabled"


class RunStatus(str, Enum):
    RECEIVED = "RECEIVED"
    CONTEXT_READY = "CONTEXT_READY"
    PLANNED = "PLANNED"
    VALIDATED = "VALIDATED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    BLOCKED_LOGIN = "BLOCKED_LOGIN"
    BLOCKED_DATA = "BLOCKED_DATA"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    CANCELLED = "CANCELLED"


class WorkItemStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    BLOCKED_LOGIN = "BLOCKED_LOGIN"
    BLOCKED_DATA = "BLOCKED_DATA"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    BLOCKED_LOGIN = "BLOCKED_LOGIN"
    BLOCKED_DATA = "BLOCKED_DATA"
    CANCELLED = "CANCELLED"


RUN_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED_TERMINAL,
        RunStatus.CANCELLED,
    }
)


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RECEIVED: frozenset(
        {
            RunStatus.CONTEXT_READY,
            RunStatus.NEEDS_CLARIFICATION,
            RunStatus.BLOCKED_LOGIN,
            RunStatus.BLOCKED_DATA,
            RunStatus.FAILED_RETRYABLE,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.CONTEXT_READY: frozenset(
        {
            RunStatus.PLANNED,
            RunStatus.NEEDS_CLARIFICATION,
            RunStatus.BLOCKED_LOGIN,
            RunStatus.BLOCKED_DATA,
            RunStatus.FAILED_RETRYABLE,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PLANNED: frozenset(
        {
            RunStatus.VALIDATED,
            RunStatus.NEEDS_CLARIFICATION,
            RunStatus.BLOCKED_DATA,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.VALIDATED: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.RUNNING,
            RunStatus.BLOCKED_LOGIN,
            RunStatus.BLOCKED_DATA,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.NEEDS_CLARIFICATION,
            RunStatus.BLOCKED_LOGIN,
            RunStatus.BLOCKED_DATA,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.VERIFYING,
            RunStatus.NEEDS_CLARIFICATION,
            RunStatus.BLOCKED_LOGIN,
            RunStatus.BLOCKED_DATA,
            RunStatus.FAILED_RETRYABLE,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.VERIFYING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.BLOCKED_LOGIN,
            RunStatus.BLOCKED_DATA,
            RunStatus.FAILED_RETRYABLE,
            RunStatus.FAILED_TERMINAL,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.NEEDS_CLARIFICATION: frozenset({RunStatus.CONTEXT_READY, RunStatus.CANCELLED}),
    RunStatus.BLOCKED_LOGIN: frozenset({RunStatus.CONTEXT_READY, RunStatus.CANCELLED}),
    RunStatus.BLOCKED_DATA: frozenset({RunStatus.CONTEXT_READY, RunStatus.CANCELLED}),
    RunStatus.FAILED_RETRYABLE: frozenset({RunStatus.CONTEXT_READY, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.PARTIAL: frozenset(),
    RunStatus.FAILED_TERMINAL: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.OPEN: frozenset(
        {
            WorkItemStatus.IN_PROGRESS,
            WorkItemStatus.NEEDS_CLARIFICATION,
            WorkItemStatus.WAITING_APPROVAL,
            WorkItemStatus.BLOCKED_LOGIN,
            WorkItemStatus.BLOCKED_DATA,
            WorkItemStatus.RESOLVED,
            WorkItemStatus.CANCELLED,
        }
    ),
    WorkItemStatus.IN_PROGRESS: frozenset(
        {
            WorkItemStatus.OPEN,
            WorkItemStatus.NEEDS_CLARIFICATION,
            WorkItemStatus.WAITING_APPROVAL,
            WorkItemStatus.BLOCKED_LOGIN,
            WorkItemStatus.BLOCKED_DATA,
            WorkItemStatus.RESOLVED,
            WorkItemStatus.CANCELLED,
        }
    ),
    WorkItemStatus.NEEDS_CLARIFICATION: frozenset(
        {WorkItemStatus.OPEN, WorkItemStatus.IN_PROGRESS, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.WAITING_APPROVAL: frozenset(
        {
            WorkItemStatus.OPEN,
            WorkItemStatus.IN_PROGRESS,
            WorkItemStatus.NEEDS_CLARIFICATION,
            WorkItemStatus.CANCELLED,
        }
    ),
    WorkItemStatus.BLOCKED_LOGIN: frozenset(
        {WorkItemStatus.OPEN, WorkItemStatus.IN_PROGRESS, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.BLOCKED_DATA: frozenset(
        {WorkItemStatus.OPEN, WorkItemStatus.IN_PROGRESS, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.RESOLVED: frozenset(),
    WorkItemStatus.CANCELLED: frozenset(),
}


def assert_run_transition(current: RunStatus | str, target: RunStatus | str) -> None:
    try:
        current_status = RunStatus(current)
        target_status = RunStatus(target)
    except ValueError as exc:
        raise StateConflictError("UNKNOWN_RUN_STATUS", f"Unknown run status: {exc}") from exc
    if target_status not in RUN_TRANSITIONS[current_status]:
        raise StateConflictError(
            "ILLEGAL_RUN_TRANSITION",
            f"Run cannot transition from {current_status.value} to {target_status.value}",
        )


def assert_work_item_transition(current: WorkItemStatus | str, target: WorkItemStatus | str) -> None:
    try:
        current_status = WorkItemStatus(current)
        target_status = WorkItemStatus(target)
    except ValueError as exc:
        raise StateConflictError("UNKNOWN_WORK_ITEM_STATUS", f"Unknown work item status: {exc}") from exc
    if target_status not in WORK_ITEM_TRANSITIONS[current_status]:
        raise StateConflictError(
            "ILLEGAL_WORK_ITEM_TRANSITION",
            f"Work item cannot transition from {current_status.value} to {target_status.value}",
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Naive datetime is not allowed in canonical orchestration data")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json(item))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Actor:
    actor_type: ActorType
    actor_id: str
    roles: tuple[str, ...] = ()
    display_name: str = ""
    authenticated_by: str = ""

    def __post_init__(self) -> None:
        actor_id = str(self.actor_id or "").strip()
        if not actor_id:
            raise OrchestrationError("INVALID_ACTOR", "actor_id is required")
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "roles", tuple(sorted({str(role).strip() for role in self.roles if str(role).strip()})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_type": self.actor_type.value,
            "actor_id": self.actor_id,
            "roles": list(self.roles),
            "display_name": self.display_name,
            "authenticated_by": self.authenticated_by,
        }


@dataclass(frozen=True)
class EntityRef:
    entity_type: str
    entity_id: str
    source_system: str = ""
    relation_type: str = "subject"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.entity_type or "").strip() or not str(self.entity_id or "").strip():
            raise OrchestrationError("INVALID_ENTITY_REF", "entity_type and entity_id are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "source_system": self.source_system,
            "relation_type": self.relation_type,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Command:
    command_type: str
    source: str
    actor: Actor
    parameters: Mapping[str, Any]
    idempotency_key: str
    entity_refs: tuple[EntityRef, ...] = ()
    automation_invocation: AutomationProjectInvocation | None = None
    command_id: str = field(default_factory=new_id)
    correlation_id: str = field(default_factory=new_id)
    requested_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name, value in (
            ("command_type", self.command_type),
            ("source", self.source),
            ("idempotency_key", self.idempotency_key),
        ):
            if not str(value or "").strip():
                raise OrchestrationError("INVALID_COMMAND", f"{name} is required")
        if not isinstance(self.parameters, Mapping):
            raise OrchestrationError("INVALID_COMMAND_PARAMETERS", "parameters must be a JSON object")
        canonical_json(self.parameters)
        if _contains_reserved_automation_context(self.parameters):
            raise OrchestrationError(
                "RESERVED_AUTOMATION_CONTEXT",
                "Automation project context is server-owned",
            )
        invocation = self.automation_invocation
        tool_name = str(self.parameters.get("tool_name") or "")
        is_project_tool = (
            tool_name.startswith("automation.")
            and tool_name.endswith(".run")
            and len(tool_name) > len("automation..run")
        )
        if invocation is None and (
            self.command_type == "automation.project.invoke" or is_project_tool
        ):
            raise OrchestrationError(
                "RESERVED_AUTOMATION_CONTEXT",
                "Automation projects may only be invoked through a trusted project entrypoint",
            )
        if invocation is not None:
            if self.command_type != "automation.project.invoke":
                raise OrchestrationError(
                    "INVALID_AUTOMATION_COMMAND",
                    "Typed automation invocations require the project command type",
                )
            if self.source != invocation.entrypoint.value:
                raise OrchestrationError(
                    "INVALID_AUTOMATION_COMMAND",
                    "Automation invocation source does not match its trusted entrypoint",
                )
            expected_tool_name = f"automation.{invocation.automation_id}.run"
            if tool_name != expected_tool_name:
                raise OrchestrationError(
                    "INVALID_AUTOMATION_COMMAND",
                    "Automation invocation tool identity does not match its project",
                )
            if not isinstance(self.parameters.get("arguments"), Mapping):
                raise OrchestrationError(
                    "INVALID_AUTOMATION_COMMAND",
                    "Automation invocation arguments must be a JSON object",
                )
            context = self.parameters.get("execution_context", {})
            if not isinstance(context, Mapping):
                raise OrchestrationError(
                    "INVALID_AUTOMATION_COMMAND",
                    "Automation execution context must be a JSON object",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type,
            "source": self.source,
            "actor": self.actor.to_dict(),
            "parameters": dict(self.parameters),
            "idempotency_key": self.idempotency_key,
            "entity_refs": [ref.to_dict() for ref in self.entity_refs],
            "automation_invocation": (
                self.automation_invocation.to_dict()
                if self.automation_invocation is not None
                else None
            ),
            "correlation_id": self.correlation_id,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True)
class ContextSnapshot:
    values: Mapping[str, Any]
    account_ids: tuple[str, ...] = ()
    source_integrity: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return sha256_json(
            {
                "values": self.values,
                "account_ids": self.account_ids,
                "source_integrity": self.source_integrity,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": dict(self.values),
            "account_ids": list(self.account_ids),
            "source_integrity": dict(self.source_integrity),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class PlanStep:
    step_key: str
    tool_name: str
    tool_version: str
    operation_type: OperationType
    arguments: Mapping[str, Any]
    account_id: str | None
    depends_on: tuple[str, ...]
    idempotency_key: str
    expected_evidence: tuple[Mapping[str, Any], ...]
    postconditions: tuple[Mapping[str, Any], ...]
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("step_key", self.step_key),
            ("tool_name", self.tool_name),
            ("tool_version", self.tool_version),
            ("idempotency_key", self.idempotency_key),
        ):
            if not str(value or "").strip():
                raise OrchestrationError("INVALID_PLAN_STEP", f"{name} is required")
        if not isinstance(self.arguments, Mapping):
            raise OrchestrationError("INVALID_STEP_ARGUMENTS", "step arguments must be a JSON object")
        canonical_json(self.arguments)

    def hash_dict(self) -> dict[str, Any]:
        return {
            "step_key": self.step_key,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "operation_type": self.operation_type.value,
            "arguments": dict(self.arguments),
            "account_id": self.account_id,
            "depends_on": list(self.depends_on),
            "idempotency_key": self.idempotency_key,
            "expected_evidence": list(self.expected_evidence),
            "postconditions": list(self.postconditions),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.hash_dict(),
            "risk_level": self.risk_level.value,
            "requires_approval": self.requires_approval,
        }


@dataclass(frozen=True)
class Plan:
    command_type: str
    context_fingerprint: str
    tool_catalog_hash: str
    steps: tuple[PlanStep, ...]
    impact: Mapping[str, Any] = field(default_factory=dict)
    automation_id: str | None = None
    automation_generation: int | None = None
    automation_contract_hash: str | None = None
    schema_version: int = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version not in SUPPORTED_PLAN_SCHEMA_VERSIONS
        ):
            raise OrchestrationError("UNSUPPORTED_PLAN_SCHEMA", f"Unsupported plan schema: {self.schema_version}")
        keys = [step.step_key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise OrchestrationError("DUPLICATE_STEP_KEY", "Plan step keys must be unique")
        project_fields = (
            self.automation_id,
            self.automation_generation,
            self.automation_contract_hash,
        )
        if any(value is not None for value in project_fields):
            if not all(value is not None for value in project_fields):
                raise OrchestrationError(
                    "INVALID_AUTOMATION_PLAN",
                    "Automation plan identity must be complete",
                )
            if not str(self.automation_id or "").strip():
                raise OrchestrationError(
                    "INVALID_AUTOMATION_PLAN",
                    "Automation plan identity is empty",
                )
            if type(self.automation_generation) is not int or self.automation_generation <= 0:
                raise OrchestrationError(
                    "INVALID_AUTOMATION_PLAN",
                    "Automation plan generation must be a positive integer",
                )
            if len(str(self.automation_contract_hash or "")) != 64:
                raise OrchestrationError(
                    "INVALID_AUTOMATION_PLAN",
                    "Automation plan contract hash is invalid",
                )

    def hash_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "command_type": self.command_type,
            "context_fingerprint": self.context_fingerprint,
            "tool_catalog_hash": self.tool_catalog_hash,
            "steps": [step.hash_dict() for step in self.steps],
            "impact": dict(self.impact),
        }
        if self.automation_id is not None:
            payload["automation_id"] = self.automation_id
            payload["automation_generation"] = self.automation_generation
            payload["automation_contract_hash"] = self.automation_contract_hash
        return payload

    @property
    def plan_hash(self) -> str:
        return sha256_json(self.hash_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.hash_payload(),
            "steps": [step.to_dict() for step in self.steps],
            "plan_hash": self.plan_hash,
        }


@dataclass(frozen=True)
class ToolResult:
    status: str
    data: Mapping[str, Any]
    meta: Mapping[str, Any]
    warnings: tuple[str, ...] = ()
    error: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": dict(self.data),
            "meta": dict(self.meta),
            "warnings": list(self.warnings),
            "error": dict(self.error) if self.error is not None else None,
        }


@dataclass(frozen=True)
class CommandReceipt:
    command_id: str
    work_item_id: str
    run_id: str
    status: RunStatus
    reused: bool
    next_poll_after_ms: int = 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "work_item_id": self.work_item_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "reused": self.reused,
            "next_poll_after_ms": self.next_poll_after_ms,
        }


def _contains_reserved_automation_context(value: Any) -> bool:
    if isinstance(value, Mapping):
        if RESERVED_AUTOMATION_CONTEXT_FIELDS.intersection(
            str(key) for key in value
        ):
            return True
        return any(_contains_reserved_automation_context(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_reserved_automation_context(item) for item in value)
    return False


def topological_steps(steps: Iterable[PlanStep]) -> tuple[PlanStep, ...]:
    """Return deterministic dependency order or fail on unknown/cyclic dependencies."""

    by_key = {step.step_key: step for step in steps}
    pending = {key: set(step.depends_on) for key, step in by_key.items()}
    unknown = sorted({dependency for deps in pending.values() for dependency in deps if dependency not in by_key})
    if unknown:
        raise OrchestrationError("UNKNOWN_STEP_DEPENDENCY", f"Unknown step dependencies: {', '.join(unknown)}")

    ordered: list[PlanStep] = []
    while pending:
        ready = sorted(key for key, dependencies in pending.items() if not dependencies)
        if not ready:
            raise OrchestrationError("CYCLIC_STEP_DEPENDENCY", "Plan step dependency graph contains a cycle")
        for key in ready:
            ordered.append(by_key[key])
            pending.pop(key)
            for dependencies in pending.values():
                dependencies.discard(key)
    return tuple(ordered)
