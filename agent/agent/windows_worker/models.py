"""Closed Worker device and job contracts."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from agent.automation_plugins.errors import WorkerProtocolError


class DeviceServiceState(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DRAINING = "DRAINING"
    DISABLED = "DISABLED"


class InteractiveSessionState(str, Enum):
    AVAILABLE = "AVAILABLE"
    LOCKED = "LOCKED"
    LOGGED_OUT = "LOGGED_OUT"


class WorkerJobType(str, Enum):
    INSTALL = "INSTALL"
    UPGRADE = "UPGRADE"
    UNINSTALL = "UNINSTALL"
    INVOKE = "INVOKE"
    CLEANUP = "CLEANUP"


class WorkerJobStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED_DATA = "BLOCKED_DATA"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class WorkerDeviceSnapshot:
    device_id: str
    display_name: str
    service_state: DeviceServiceState
    session_state: InteractiveSessionState
    last_seen_at: datetime
    agent_version: str


@dataclass(frozen=True)
class WorkerJob:
    job_id: str
    automation_id: str
    plugin_id: str
    plugin_version: str
    job_type: WorkerJobType
    status: WorkerJobStatus
    payload: Mapping[str, Any]
    target_device_id: str
    available_at: datetime
    deadline_at: datetime
    requires_interactive_session: bool
    operation_type: str
    automation_generation: int = 1
    attempt_count: int = 0
    max_attempts: int = 1
    cleanup_scope: str | None = None

    def __post_init__(self) -> None:
        if type(self.automation_generation) is not int or self.automation_generation <= 0:
            raise ValueError("Windows automation jobs require a positive generation")
        if self.max_attempts != 1:
            raise ValueError("Windows automation jobs must use max_attempts=1")
        if not self.target_device_id:
            raise ValueError("Windows automation jobs require one explicit named device")
        if self.deadline_at.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("Worker job timestamps must be timezone-aware")
        if self.deadline_at <= self.available_at:
            raise ValueError("Worker job deadline must follow available_at")

    @property
    def is_write(self) -> bool:
        return self.operation_type in {
            "internal_projection_write",
            "external_write",
            "financial_write",
            "destructive",
        }


@dataclass(frozen=True)
class WorkerHealth:
    held: bool
    active_jobs: int
    service_state: DeviceServiceState | None = None
    session_state: InteractiveSessionState | None = None


_JOB_FIELDS = frozenset(
    {
        "job_id",
        "automation_id",
        "automation_generation",
        "plugin_id",
        "plugin_version",
        "job_type",
        "status",
        "payload",
        "target_device_id",
        "available_at",
        "deadline_at",
        "requires_interactive_session",
        "operation_type",
        "attempt_count",
        "max_attempts",
        "cleanup_scope",
    }
)
_FORBIDDEN_PAYLOAD_KEY_PARTS = (
    "password",
    "cookie",
    "credential",
    "secret",
    "token",
    "authorization",
)


def assert_broker_only_worker_value(value: Any, *, depth: int = 0) -> None:
    if depth > 12:
        raise WorkerProtocolError("Worker job payload nesting is too deep")
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise WorkerProtocolError("Worker job payload keys must be strings")
            key = raw_key.casefold()
            if (
                key in {"account_id", "account_ids"}
                or key.endswith(("_account_id", "_account_ids"))
                or any(part in key for part in _FORBIDDEN_PAYLOAD_KEY_PARTS)
            ):
                raise WorkerProtocolError("Worker job payload cannot contain accounts or credentials")
            assert_broker_only_worker_value(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            assert_broker_only_worker_value(child, depth=depth + 1)
        return
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise WorkerProtocolError("Worker job payload contains a non-JSON value")


def _aware_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise WorkerProtocolError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerProtocolError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise WorkerProtocolError(f"{field} must be timezone-aware")
    return parsed


def worker_job_from_mapping(value: Mapping[str, Any]) -> WorkerJob:
    """Parse one closed, instance-bound command from a signed envelope."""

    raw = dict(value)
    if set(raw) != _JOB_FIELDS or not isinstance(raw.get("payload"), dict):
        raise WorkerProtocolError("Worker job schema is invalid")
    string_fields = (
        "job_id",
        "automation_id",
        "plugin_id",
        "plugin_version",
        "target_device_id",
        "operation_type",
    )
    if any(not isinstance(raw.get(field), str) or not raw[field] for field in string_fields):
        raise WorkerProtocolError("Worker job identity fields are invalid")
    if not isinstance(raw.get("requires_interactive_session"), bool):
        raise WorkerProtocolError("Worker job interactive flag is invalid")
    if any(
        isinstance(raw.get(field), bool) or not isinstance(raw.get(field), int)
        for field in ("automation_generation", "attempt_count", "max_attempts")
    ):
        raise WorkerProtocolError("Worker job attempt counters are invalid")
    if raw.get("cleanup_scope") is not None and not isinstance(raw.get("cleanup_scope"), str):
        raise WorkerProtocolError("Worker job cleanup scope is invalid")
    assert_broker_only_worker_value(raw["payload"])
    try:
        return WorkerJob(
            job_id=str(raw["job_id"]),
            automation_id=str(raw["automation_id"]),
            automation_generation=raw["automation_generation"],
            plugin_id=str(raw["plugin_id"]),
            plugin_version=str(raw["plugin_version"]),
            job_type=WorkerJobType(str(raw["job_type"])),
            status=WorkerJobStatus(str(raw["status"])),
            payload=dict(raw["payload"]),
            target_device_id=str(raw["target_device_id"]),
            available_at=_aware_time(raw["available_at"], "available_at"),
            deadline_at=_aware_time(raw["deadline_at"], "deadline_at"),
            requires_interactive_session=raw["requires_interactive_session"],
            operation_type=str(raw["operation_type"]),
            attempt_count=raw["attempt_count"],
            max_attempts=raw["max_attempts"],
            cleanup_scope=(
                str(raw["cleanup_scope"])
                if raw["cleanup_scope"] is not None
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("Worker job fields are invalid") from exc


def worker_job_to_mapping(job: WorkerJob) -> dict[str, Any]:
    """Serialize one job without adding account/session material."""

    assert_broker_only_worker_value(job.payload)
    return {
        "job_id": job.job_id,
        "automation_id": job.automation_id,
        "automation_generation": job.automation_generation,
        "plugin_id": job.plugin_id,
        "plugin_version": job.plugin_version,
        "job_type": job.job_type.value,
        "status": job.status.value,
        "payload": copy.deepcopy(dict(job.payload)),
        "target_device_id": job.target_device_id,
        "available_at": job.available_at.astimezone(timezone.utc).isoformat(),
        "deadline_at": job.deadline_at.astimezone(timezone.utc).isoformat(),
        "requires_interactive_session": job.requires_interactive_session,
        "operation_type": job.operation_type,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "cleanup_scope": job.cleanup_scope,
    }
