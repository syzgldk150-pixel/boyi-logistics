"""Pure models shared by Direct invocation, verification and identity checks.

This module deliberately has no database, HTTP, tool, or environment imports.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping


class OrchestrationError(RuntimeError):
    """Base error carrying a stable API-safe error code."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})


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
