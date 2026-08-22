"""MySQL persistence for signed plugin packages and automation instances.

The repository deliberately keeps package identity (``plugin_id``) separate
from independently configured installations (``automation_id``).  It performs
no filesystem operations and never stores account credentials or sessions.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from shared.automation_project_policy_repository import PROJECT_POLICY_MODES
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    OrchestrationPersistenceError,
    RepositoryBase,
    _decode_row,
    _json_hash,
    _json_param,
    _json_value,
    _optional_text,
    _required_text,
    _row_dict,
    _rows,
    _safe_error,
)


class AutomationPluginReleaseHold(RuntimeError):
    """Raised when a worker mutation is attempted during release hold."""


class AutomationPluginPreparedTargetOccupied(ConcurrentUpdateError):
    """A config-only target was materialized before its plugin upgrade staged."""


class AutomationPluginPurgeBlocked(RuntimeError):
    """Raised when active or outcome-unknown execution blocks uninstall."""


_PROJECT_STATES = frozenset(
    {"INSTALLED", "ENABLED", "DISABLED", "UPGRADING", "UNINSTALLING", "ERROR"}
)
_DEVICE_SERVICE_STATES = frozenset({"ONLINE", "OFFLINE", "DRAINING", "DISABLED"})
_DEVICE_SESSION_STATES = frozenset({"AVAILABLE", "LOCKED", "LOGGED_OUT"})
_ACTIVE_JOB_STATES = frozenset({"CLAIMED", "RUNNING", "OUTCOME_UNKNOWN"})
_WORKER_OPERATION_TYPES = frozenset(
    {
        "read",
        "compute",
        "internal_projection_write",
        "external_write",
        "financial_write",
        "destructive",
    }
)
_WORKER_WRITE_OPERATION_TYPES = frozenset(
    {
        "internal_projection_write",
        "external_write",
        "financial_write",
        "destructive",
    }
)
_WORKER_TERMINAL_JOB_STATES = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED_DATA", "OUTCOME_UNKNOWN"}
)
_PLUGIN_TRUST_SOURCES = frozenset(
    {"ed25519_upload", "ed25519_first_party", "builtin_release"}
)
_RECONCILE_STATES = frozenset(
    {
        "STABLE",
        "PREPARING",
        "WAITING_COEFFECTS",
        "READY_TO_COMMIT",
        "DRAINING",
        "DISPOSING",
        "BLOCKED_UNKNOWN_WRITE",
        "ERROR",
    }
)
_GENERATION_STATES = frozenset(
    {
        "TARGET",
        "PREPARING",
        "WAITING_COEFFECTS",
        "PREPARED",
        "COMMITTED",
        "DRAINING",
        "DISPOSING",
        "DISPOSED",
        "FAILED",
        "BLOCKED",
    }
)
_COEFFECT_KINDS = frozenset(
    {"ACCOUNT", "SESSION", "RESOURCE", "DEVICE", "CORE_ADAPTER"}
)
_EFFECT_KINDS = frozenset(
    {
        "PACKAGE_REFERENCE",
        "VENV_REFERENCE",
        "INSTANCE_RUNTIME",
        "SCHEDULE_BINDING",
        "WEBHOOK_BINDING",
        "BROKER_SCOPE",
        "WORKER_DEPLOYMENT",
        "ENTRYPOINT_ROUTE",
    }
)
_GENERATION_HASH_FIELDS = (
    "package_sha256",
    "manifest_sha256",
    "project_config_sha256",
    "account_bindings_sha256",
    "resource_bindings_sha256",
    "device_binding_sha256",
    "schedule_sha256",
    "core_registry_sha256",
    "tool_contract_sha256",
    "invocation_contracts_sha256",
    "compiled_invocations_sha256",
    "runtime_descriptor_sha256",
    "governance_anchor_sha256",
    "policy_contract_sha256",
)


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


@dataclass(frozen=True)
class AutomationProjectGenerationStage:
    automation_id: str
    target_generation: int
    prior_target_generation: int
    committed_generation: int | None
    prior_reconcile_state: str
    project_state: str
    expected_record_version: int
    existing_generations: frozenset[int]


def _stageable_project_state(value: Any) -> str:
    state = str(value or "")
    if state not in _PROJECT_STATES:
        raise OrchestrationPersistenceError(
            "automation project state is invalid"
        )
    if state not in {"INSTALLED", "ENABLED", "DISABLED"}:
        raise ConcurrentUpdateError(
            "automation project cannot stage a generation in its current state"
        )
    return state


def _project_generation_stage(
    project: Mapping[str, Any],
    current_config: Mapping[str, Any],
    generation_rows: Sequence[Mapping[str, Any]],
    *,
    allow_initial: bool,
) -> AutomationProjectGenerationStage:
    """Plan one exact project generation without mutating persistence."""

    try:
        automation_id = _required_text(
            project.get("automation_id"),
            "automation_id",
        )
        target_generation = _positive_int(
            project.get("target_generation"),
            "target_generation",
        )
        committed_generation = _optional_positive_int(
            project.get("committed_generation"),
            "committed_generation",
        )
        record_version = _positive_int(
            project.get("record_version"),
            "record_version",
        )
    except ValueError as exc:
        raise OrchestrationPersistenceError(
            "automation project generation pointers are invalid"
        ) from exc
    project_state = _stageable_project_state(project.get("state"))
    reconcile_state = str(project.get("reconcile_state") or "")
    if reconcile_state not in _RECONCILE_STATES:
        raise OrchestrationPersistenceError(
            "automation project reconcile state is invalid"
        )
    configured = current_config.get("configured")
    if configured not in {True, False, 0, 1}:
        raise OrchestrationPersistenceError(
            "automation project configured state is invalid"
        )
    normalized_rows: dict[int, str] = {}
    try:
        for row in generation_rows:
            generation = _positive_int(row.get("generation"), "generation")
            state = str(row.get("state") or "")
            if state not in _GENERATION_STATES or generation in normalized_rows:
                raise ValueError("invalid generation history")
            normalized_rows[generation] = state
    except ValueError as exc:
        raise OrchestrationPersistenceError(
            "automation project generation history is invalid"
        ) from exc

    if committed_generation is None:
        if (
            not allow_initial
            or target_generation != 1
            or reconcile_state != "WAITING_COEFFECTS"
            or configured not in {False, 0}
            or normalized_rows
        ):
            raise ConcurrentUpdateError(
                "uncommitted automation project is not in its initial configuration state"
            )
        next_generation = 1
    else:
        if (
            reconcile_state != "STABLE"
            or target_generation != committed_generation
            or configured not in {True, 1}
        ):
            raise ConcurrentUpdateError(
                "automation project must be stable before a generation can advance"
            )
        if normalized_rows.get(committed_generation) != "COMMITTED":
            raise OrchestrationPersistenceError(
                "stable automation project has no committed generation record"
            )
        maximum = max(normalized_rows)
        if set(normalized_rows) != set(range(1, maximum + 1)) or any(
            state != "DISPOSED"
            for generation, state in normalized_rows.items()
            if generation != committed_generation
        ):
            raise ConcurrentUpdateError(
                "automation project generation history is not fully disposed"
            )
        next_generation = maximum + 1

    return AutomationProjectGenerationStage(
        automation_id=automation_id,
        target_generation=next_generation,
        prior_target_generation=target_generation,
        committed_generation=committed_generation,
        prior_reconcile_state=reconcile_state,
        project_state=project_state,
        expected_record_version=record_version,
        existing_generations=frozenset(normalized_rows),
    )


def _configuration_target_generation(
    project: Mapping[str, Any],
    current_config: Mapping[str, Any],
    generation_rows: Sequence[Mapping[str, Any]],
) -> int:
    """Compatibility projection for the shared generation staging contract."""

    return _project_generation_stage(
        project,
        current_config,
        generation_rows,
        allow_initial=True,
    ).target_generation


def _prepared_configuration_upgrade_stage(
    project: Mapping[str, Any],
    current_config: Mapping[str, Any],
    generation_rows: Sequence[Mapping[str, Any]],
) -> AutomationProjectGenerationStage:
    """Reuse only the exact empty target opened by a configuration save."""

    if str(project.get("reconcile_state") or "") != "PREPARING":
        raise ConcurrentUpdateError(
            "automation project has no prepared configuration generation"
        )
    try:
        target_generation = _positive_int(
            project.get("target_generation"),
            "target_generation",
        )
        committed_generation = _positive_int(
            project.get("committed_generation"),
            "committed_generation",
        )
    except ValueError as exc:
        raise OrchestrationPersistenceError(
            "automation project generation pointers are invalid"
        ) from exc

    # Validate the lineage by projecting the persisted project back to the
    # closed state from which save_project_config opened this exact target.
    closed_project = dict(project)
    closed_project["target_generation"] = committed_generation
    closed_project["reconcile_state"] = "STABLE"
    target_rows = tuple(
        row
        for row in generation_rows
        if row.get("generation") == target_generation
    )
    lineage_rows = tuple(
        row
        for row in generation_rows
        if row.get("generation") != target_generation
    )
    closed_stage = _project_generation_stage(
        closed_project,
        current_config,
        lineage_rows,
        allow_initial=False,
    )
    if closed_stage.target_generation != target_generation:
        raise ConcurrentUpdateError(
            "prepared configuration generation is not the next closed target"
        )
    if target_rows:
        if (
            len(target_rows) == 1
            and target_rows[0].get("state")
            in {"TARGET", "PREPARING", "WAITING_COEFFECTS", "PREPARED"}
        ):
            raise AutomationPluginPreparedTargetOccupied(
                "prepared configuration target is already materialized"
            )
        raise ConcurrentUpdateError(
            "prepared configuration target cannot be reused in its current state"
        )
    return AutomationProjectGenerationStage(
        automation_id=closed_stage.automation_id,
        target_generation=target_generation,
        prior_target_generation=target_generation,
        committed_generation=closed_stage.committed_generation,
        prior_reconcile_state="PREPARING",
        project_state=closed_stage.project_state,
        expected_record_version=closed_stage.expected_record_version,
        existing_generations=closed_stage.existing_generations,
    )


def _sha256(value: Any, field: str) -> str:
    text = _required_text(value, field).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _normalized_project_schedule(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"kind", "times", "enabled"}:
        raise ValueError("schedule must contain exactly kind, times and enabled")
    kind = value.get("kind")
    times = value.get("times")
    enabled = value.get("enabled")
    if kind not in {"none", "daily_times", "startup"} or type(enabled) is not bool:
        raise ValueError("schedule kind/enabled is invalid")
    if not isinstance(times, (list, tuple)) or any(type(item) is not str for item in times):
        raise ValueError("schedule times must be an array of HH:MM strings")
    normalized_times = sorted(str(item) for item in times)
    if kind == "none":
        if normalized_times or enabled:
            raise ValueError("none schedule must be disabled and empty")
        return {"kind": "none", "times": [], "enabled": False}
    if kind == "startup":
        if normalized_times:
            raise ValueError("startup schedule cannot contain times")
        return {"kind": "startup", "times": [], "enabled": enabled}
    if not normalized_times or len(normalized_times) != len(set(normalized_times)):
        raise ValueError("daily schedule times must be non-empty and unique")
    for item in normalized_times:
        if (
            len(item) != 5
            or item[2] != ":"
            or not item[:2].isdigit()
            or not item[3:].isdigit()
            or not 0 <= int(item[:2]) <= 23
            or not 0 <= int(item[3:]) <= 59
        ):
            raise ValueError("daily schedule time must be canonical HH:MM")
    return {"kind": "daily_times", "times": normalized_times, "enabled": enabled}


_QUARTER_HOUR_DAILY_TIMES = tuple(
    f"{hour:02d}:{minute:02d}"
    for hour in range(24)
    for minute in (0, 15, 30, 45)
)


def _schedule_from_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"kind": "none", "times": [], "enabled": False}
    enabled_values = {bool(row.get("enabled")) for row in rows}
    if len(enabled_values) != 1:
        raise OrchestrationPersistenceError("project schedule rows have mixed enabled states")
    enabled = enabled_values.pop()
    expressions = [str(row.get("cron_expression") or "").strip() for row in rows]
    if expressions == ["@startup"]:
        return {"kind": "startup", "times": [], "enabled": enabled}
    # ``customer_problems_shadow`` predates project-owned schedules and is
    # the sole reviewed interval cron in the migration inventory.  Expand
    # only that exact single-row expression to the equivalent closed DTO.
    if "*/15 * * * *" in expressions:
        if expressions != ["*/15 * * * *"]:
            raise OrchestrationPersistenceError(
                "reviewed interval cron cannot be mixed with other schedule rows"
            )
        return {
            "kind": "daily_times",
            "times": list(_QUARTER_HOUR_DAILY_TIMES),
            "enabled": enabled,
        }
    times: list[str] = []
    for expression in expressions:
        fields = expression.split()
        if (
            len(fields) != 5
            or fields[2:] != ["*", "*", "*"]
            or not fields[0].isdigit()
            or not fields[1].isdigit()
            or not 0 <= int(fields[0]) <= 59
            or not 0 <= int(fields[1]) <= 23
        ):
            raise OrchestrationPersistenceError(
                "project schedule contains a non-canonical system cron"
            )
        times.append(f"{int(fields[1]):02d}:{int(fields[0]):02d}")
    if len(times) != len(set(times)):
        raise OrchestrationPersistenceError("project schedule contains duplicate times")
    return {"kind": "daily_times", "times": sorted(times), "enabled": enabled}


def _schedule_expressions(schedule: Mapping[str, Any]) -> tuple[str, ...]:
    kind = schedule["kind"]
    if kind == "none":
        return ()
    if kind == "startup":
        return ("@startup",)
    if tuple(schedule["times"]) == _QUARTER_HOUR_DAILY_TIMES:
        # Preserve the exact reviewed legacy row identity and its approval
        # history when the migration generation commits.  This is the inverse
        # of the single accepted interval expansion in ``_schedule_from_rows``.
        return ("*/15 * * * *",)
    return tuple(
        f"{int(item[3:])} {int(item[:2])} * * *"
        for item in schedule["times"]
    )


def _stable_schedule_task_id(automation_id: str, cron_expression: str) -> str:
    digest = hashlib.sha256(
        f"automation-schedule-v1\0{automation_id}\0{cron_expression}".encode("utf-8")
    ).hexdigest()
    return f"ap_{digest[:40]}"


def _device_binding_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return _required_text(value.get("device_id"), "device_id")
    return _required_text(getattr(value, "device_id", None), "device_id")


def _mysql_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _generation_snapshot(
    automation_id: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("generation snapshot must be a mapping")
    snapshot = dict(value)
    if set(snapshot) != {
        "automation_id",
        "generation",
        "plugin_id",
        "plugin_version",
        "trust_source",
        "enabled_entrypoints",
        "execution_metadata",
        "created_at",
        *_GENERATION_HASH_FIELDS,
    }:
        raise ValueError("generation snapshot fields are not closed")
    if _required_text(snapshot.get("automation_id"), "automation_id") != automation_id:
        raise ValueError("generation snapshot automation_id does not match target")
    _positive_int(snapshot.get("generation"), "generation")
    _required_text(snapshot.get("plugin_id"), "plugin_id")
    _required_text(snapshot.get("plugin_version"), "plugin_version")
    trust_source = str(snapshot.get("trust_source") or "")
    if trust_source not in _PLUGIN_TRUST_SOURCES:
        raise ValueError("generation snapshot trust_source is invalid")
    for field in _GENERATION_HASH_FIELDS:
        snapshot[field] = _sha256(snapshot.get(field), field)
    entrypoints = snapshot.get("enabled_entrypoints")
    if (
        not isinstance(entrypoints, (list, tuple))
        or any(not isinstance(item, str) or not item.strip() for item in entrypoints)
        or len(entrypoints) != len(set(entrypoints))
    ):
        raise ValueError("generation enabled_entrypoints must be unique strings")
    snapshot["enabled_entrypoints"] = sorted(item.strip() for item in entrypoints)
    snapshot["execution_metadata"] = _generation_execution_metadata(
        snapshot.get("execution_metadata"),
        enabled_entrypoints=snapshot["enabled_entrypoints"],
    )
    created_at = snapshot.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be an ISO datetime") from exc
    snapshot["created_at"] = _mysql_datetime(created_at, "created_at")
    return snapshot


def _generation_execution_metadata(
    value: Any,
    *,
    enabled_entrypoints: Sequence[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "project_config_version",
        "project_config",
        "account_bindings",
        "resource_bindings",
        "device_binding",
        "schedule",
        "compiled_invocations",
        "runtime_descriptor",
        "action_contract",
        "governance_anchor",
    }:
        raise ValueError("generation execution metadata fields are not closed")
    normalized = dict(value)
    _positive_int(
        normalized.get("project_config_version"),
        "project_config_version",
    )
    for field in (
        "project_config",
        "account_bindings",
        "resource_bindings",
        "compiled_invocations",
        "runtime_descriptor",
        "action_contract",
        "governance_anchor",
    ):
        if not isinstance(normalized.get(field), Mapping):
            raise ValueError(f"generation {field} must be an object")
    normalized["schedule"] = _normalized_project_schedule(
        normalized.get("schedule")
    )
    device_binding = normalized.get("device_binding")
    if device_binding is not None and not isinstance(device_binding, Mapping):
        raise ValueError("generation device binding must be null or an object")
    if set(normalized["compiled_invocations"]) != set(enabled_entrypoints):
        raise ValueError(
            "generation compiled invocations must match enabled entrypoints"
        )
    _reject_sensitive_generation_metadata(normalized)
    # Round-trip through canonical JSON validation; no custom Python objects,
    # datetimes or plugin-controlled byte payloads may enter the snapshot.
    _json_hash(normalized)
    return normalized


def _validated_generation_row(value: Any) -> dict[str, Any]:
    """Validate the immutable snapshot against every duplicated SQL index."""

    if not isinstance(value, Mapping):
        raise OrchestrationPersistenceError("runtime generation row is invalid")
    row = dict(value)
    automation_id = _required_text(row.get("automation_id"), "automation_id")
    generation = _positive_int(row.get("generation"), "generation")
    raw_snapshot = row.get("snapshot_json")
    if not isinstance(raw_snapshot, Mapping):
        raise OrchestrationPersistenceError(
            "runtime generation snapshot is invalid"
        )
    snapshot = _generation_snapshot(automation_id, raw_snapshot)
    if (
        int(snapshot["generation"]) != generation
        or _json_hash(snapshot) != str(row.get("snapshot_sha256") or "")
        or str(snapshot["plugin_id"]) != str(row.get("plugin_id") or "")
        or str(snapshot["plugin_version"])
        != str(row.get("plugin_version") or "")
        or str(snapshot["trust_source"]) != str(row.get("trust_source") or "")
        or _json_hash(snapshot["enabled_entrypoints"])
        != str(row.get("enabled_entrypoints_sha256") or "")
        or any(
            str(snapshot[field]) != str(row.get(field) or "")
            for field in _GENERATION_HASH_FIELDS
        )
    ):
        raise OrchestrationPersistenceError(
            "runtime generation snapshot integrity failed"
        )
    row["snapshot_json"] = snapshot
    return row


def _reject_sensitive_generation_metadata(value: Any, path: str = "execution_metadata") -> None:
    sensitive = ("password", "secret", "cookie", "credential", "authorization", "api_key")
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).lower().replace("-", "_")
            if any(marker in key for marker in sensitive):
                raise ValueError(f"{path} contains credential material")
            _reject_sensitive_generation_metadata(nested, f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_generation_metadata(nested, f"{path}[{index}]")


def _selected_ids(
    cursor: Any,
    sql: str,
    params: Sequence[Any],
    field: str,
) -> list[str]:
    cursor.execute(sql, tuple(params))
    return [
        _required_text(row.get(field), field)
        for row in _rows(cursor)
    ]


def _sql_placeholders(values: Sequence[Any]) -> str:
    if not values:
        raise ValueError("SQL placeholder values must not be empty")
    return ", ".join(["%s"] * len(values))


def _worker_release_sha(value: Any) -> str:
    release_sha = _required_text(value, "release_sha")
    if not 7 <= len(release_sha) <= 64 or any(
        character not in "0123456789abcdef" for character in release_sha
    ):
        raise ValueError("release_sha must be a lowercase hexadecimal revision")
    return release_sha


def _normalized_worker_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("worker device identity must be an object")
    identity = dict(value)
    if set(identity) != {
        "device_key_id",
        "ed25519_public_key_base64",
        "tls_client_certificate_sha256",
    }:
        raise ValueError("worker device identity is not closed")
    key_id = _required_text(identity.get("device_key_id"), "device_key_id")
    public_key_text = _required_text(
        identity.get("ed25519_public_key_base64"),
        "ed25519_public_key_base64",
    )
    try:
        public_key = base64.b64decode(public_key_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("worker Ed25519 public key is not canonical base64") from exc
    if len(public_key) != 32:
        raise ValueError("worker Ed25519 public key must contain 32 bytes")
    certificate_sha256 = _sha256(
        identity.get("tls_client_certificate_sha256"),
        "tls_client_certificate_sha256",
    )
    return {
        "device_key_id": key_id,
        "ed25519_public_key_base64": public_key_text,
        "tls_client_certificate_sha256": certificate_sha256,
    }


def _canonical_uuid(value: Any, field: str) -> str:
    text = _required_text(value, field)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc
    if str(parsed) != text:
        raise ValueError(f"{field} must be a canonical UUID")
    return text


def _worker_time(value: Any, field: str) -> str:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat()


def _worker_job_body(
    row: Mapping[str, Any],
    *,
    claimed_attempt: bool,
) -> dict[str, Any]:
    payload = row.get("payload_json")
    if not isinstance(payload, Mapping):
        raise OrchestrationPersistenceError("worker job payload is invalid")
    attempt_count = _positive_int(
        int(row.get("attempt_count") or 0) + (1 if claimed_attempt else 0),
        "attempt_count",
    )
    max_attempts = _positive_int(row.get("max_attempts"), "max_attempts")
    if max_attempts != 1 or attempt_count != 1:
        raise OrchestrationPersistenceError(
            "durable Worker jobs must be dispatched exactly once"
        )
    operation_type = str(row.get("operation_type") or "")
    if operation_type not in _WORKER_OPERATION_TYPES:
        raise OrchestrationPersistenceError("worker job operation type is invalid")
    requires_interactive = row.get("requires_interactive_session")
    if requires_interactive not in {True, False, 0, 1}:
        raise OrchestrationPersistenceError(
            "worker job interactive requirement is invalid"
        )
    cleanup_scope = _optional_text(row.get("cleanup_scope"))
    return {
        "job_id": _canonical_uuid(row.get("job_id"), "job_id"),
        "automation_id": _required_text(row.get("automation_id"), "automation_id"),
        "automation_generation": _positive_int(
            row.get("automation_generation"),
            "automation_generation",
        ),
        "plugin_id": _required_text(row.get("plugin_id"), "plugin_id"),
        "plugin_version": _required_text(
            row.get("plugin_version"),
            "plugin_version",
        ),
        "job_type": _required_text(row.get("job_type"), "job_type").upper(),
        "status": "CLAIMED",
        "payload": dict(payload),
        "target_device_id": _required_text(
            row.get("target_device_id"),
            "target_device_id",
        ),
        "available_at": _worker_time(row.get("available_at"), "available_at"),
        "deadline_at": _worker_time(row.get("deadline_at"), "deadline_at"),
        "requires_interactive_session": bool(requires_interactive),
        "operation_type": operation_type,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "cleanup_scope": cleanup_scope,
    }


def _validate_dispatch_envelope(
    envelope: Mapping[str, Any],
    *,
    device_id: str,
    sequence: int,
    message_id: str,
    job_body: Mapping[str, Any],
    dispatch: Mapping[str, Any],
) -> None:
    expected_fields = {
        "schema_version",
        "message_id",
        "device_id",
        "sequence",
        "issued_at",
        "expires_at",
        "kind",
        "body",
        "key_id",
        "signature",
    }
    if set(envelope) != expected_fields:
        raise OrchestrationPersistenceError(
            "signed Worker dispatch envelope is not closed"
        )
    if (
        envelope.get("schema_version") != 1
        or envelope.get("message_id") != message_id
        or envelope.get("device_id") != device_id
        or type(envelope.get("sequence")) is not int
        or envelope.get("sequence") != sequence
        or envelope.get("kind") != "COMMAND"
        or not isinstance(envelope.get("key_id"), str)
        or not envelope.get("key_id")
        or not isinstance(envelope.get("signature"), str)
        or not envelope.get("signature")
    ):
        raise OrchestrationPersistenceError(
            "signed Worker dispatch envelope identity is invalid"
        )
    _canonical_uuid(message_id, "message_id")
    expected_body = {"job": dict(job_body), "dispatch": dict(dispatch)}
    if _json_hash(envelope.get("body")) != _json_hash(expected_body):
        raise OrchestrationPersistenceError(
            "signed Worker dispatch envelope body changed"
        )
    for field in ("issued_at", "expires_at"):
        if not isinstance(envelope.get(field), str) or not envelope.get(field):
            raise OrchestrationPersistenceError(
                "signed Worker dispatch envelope timestamp is invalid"
            )


def _validated_dispatch_row(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    envelope = row.get("dispatch_envelope_json")
    if not isinstance(envelope, Mapping):
        raise OrchestrationPersistenceError(
            "claimed Worker job has no durable dispatch envelope"
        )
    sequence = row.get("dispatch_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise OrchestrationPersistenceError(
            "claimed Worker job has an invalid dispatch sequence"
        )
    message_id = _canonical_uuid(
        row.get("dispatch_message_id"),
        "dispatch_message_id",
    )
    authorization_id = _canonical_uuid(
        row.get("dispatch_authorization_id"),
        "dispatch_authorization_id",
    )
    dispatch = {
        "release_hold": False,
        "authorization_id": authorization_id,
        "release_sha": _worker_release_sha(row.get("dispatch_release_sha")),
    }
    _validate_dispatch_envelope(
        envelope,
        device_id=_required_text(row.get("assigned_device_id"), "assigned_device_id"),
        sequence=sequence,
        message_id=message_id,
        job_body=_worker_job_body(row, claimed_attempt=False),
        dispatch=dispatch,
    )
    if str(row.get("dispatch_envelope_sha256") or "") != _json_hash(envelope):
        raise OrchestrationPersistenceError(
            "claimed Worker dispatch envelope digest changed"
        )
    return row


def _validated_worker_inbound_envelope(
    value: Mapping[str, Any],
    *,
    principal_device_id: str,
    expected_kind: str,
) -> dict[str, Any]:
    """Close a Worker-signed envelope after the transport verified Ed25519.

    The paired device identity comes from the authenticated transport, never
    from the request body.  Persistence still rechecks the signed envelope so
    a handler cannot accidentally pass a body extracted from another device.
    """

    envelope = dict(value)
    expected_fields = {
        "schema_version",
        "message_id",
        "device_id",
        "sequence",
        "issued_at",
        "expires_at",
        "kind",
        "body",
        "key_id",
        "signature",
    }
    if set(envelope) != expected_fields:
        raise OrchestrationPersistenceError(
            "signed Worker inbound envelope is not closed"
        )
    sequence = envelope.get("sequence")
    if (
        envelope.get("schema_version") != 1
        or envelope.get("device_id") != principal_device_id
        or type(sequence) is not int
        or sequence < 0
        or envelope.get("kind") != expected_kind
        or not isinstance(envelope.get("body"), Mapping)
        or not isinstance(envelope.get("key_id"), str)
        or not envelope.get("key_id")
        or not isinstance(envelope.get("signature"), str)
        or not envelope.get("signature")
    ):
        raise OrchestrationPersistenceError(
            "signed Worker inbound envelope identity is invalid"
        )
    _canonical_uuid(envelope.get("message_id"), "message_id")
    for field in ("issued_at", "expires_at"):
        if not isinstance(envelope.get(field), str) or not envelope.get(field):
            raise OrchestrationPersistenceError(
                "signed Worker inbound envelope timestamp is invalid"
            )
    return envelope


def _worker_status_body(value: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(value)
    expected_fields = {
        "job_id",
        "dispatch_message_id",
        "dispatch_authorization_id",
        "status",
        "process_confirmed",
        "result",
        "error_code",
    }
    if set(body) != expected_fields:
        raise OrchestrationPersistenceError("Worker JOB_STATUS body is not closed")
    status = str(body.get("status") or "").upper()
    if status not in _WORKER_TERMINAL_JOB_STATES:
        raise OrchestrationPersistenceError("Worker JOB_STATUS is not terminal")
    if type(body.get("process_confirmed")) is not bool:
        raise OrchestrationPersistenceError(
            "Worker JOB_STATUS process confirmation is invalid"
        )
    if not isinstance(body.get("result"), Mapping):
        raise OrchestrationPersistenceError("Worker JOB_STATUS result is invalid")
    error_code = body.get("error_code")
    if error_code is not None:
        if (
            type(error_code) is not str
            or not error_code
            or len(error_code) > 64
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                for character in error_code
            )
        ):
            raise OrchestrationPersistenceError(
                "Worker JOB_STATUS error_code is invalid"
            )
    if status == "SUCCEEDED":
        if body["process_confirmed"] is not True or error_code is not None:
            raise OrchestrationPersistenceError(
                "Worker success requires confirmed execution without an error"
            )
    elif status == "OUTCOME_UNKNOWN":
        if body["process_confirmed"] is not False or error_code is None:
            raise OrchestrationPersistenceError(
                "unknown Worker outcome must be unconfirmed and coded"
            )
    elif error_code is None:
        raise OrchestrationPersistenceError(
            "non-success Worker JOB_STATUS requires an error code"
        )
    body["job_id"] = _canonical_uuid(body.get("job_id"), "job_id")
    body["dispatch_message_id"] = _canonical_uuid(
        body.get("dispatch_message_id"),
        "dispatch_message_id",
    )
    body["dispatch_authorization_id"] = _canonical_uuid(
        body.get("dispatch_authorization_id"),
        "dispatch_authorization_id",
    )
    body["status"] = status
    body["result"] = dict(body["result"])
    return body


from shared.automation_plugin_generation_repository import (
    AutomationPluginGenerationRepositoryMixin,
)  # noqa: E402
from shared.automation_plugin_worker_repository import (
    AutomationPluginWorkerRepositoryMixin,
)  # noqa: E402


class AutomationPluginRepository(
    AutomationPluginGenerationRepositoryMixin,
    AutomationPluginWorkerRepositoryMixin,
    RepositoryBase,
):
    """Transactional repository used from an orchestration Unit of Work."""

    _VERSION_JSON_FIELDS = ("manifest_json", "install_root_metadata_json")
    _CONFIG_JSON_FIELDS = (
        "config_json",
        "account_bindings_json",
        "resource_bindings_json",
        "enabled_entrypoints_json",
        "desired_schedule_json",
        "compiled_invocations_json",
    )
    _JOB_JSON_FIELDS = (
        "payload_json",
        "worker_requirement_json",
        "dispatch_envelope_json",
        "result_json",
    )
    _WORKER_MESSAGE_JSON_FIELDS = ("envelope_json", "body_json")
    _PURGE_JSON_FIELDS = ("cleanup_devices_json", "instance_snapshot_json")
    _GENERATION_JSON_FIELDS = ("snapshot_json",)
    _COEFFECT_JSON_FIELDS = ("observation_json",)
    _EFFECT_JSON_FIELDS = ("evidence_json",)

    def get_package(self, plugin_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM automation_plugin_packages WHERE plugin_id=%s{suffix}",
                (_required_text(plugin_id, "plugin_id"),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def get_version(
        self,
        plugin_id: str,
        version: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM automation_plugin_versions
                WHERE plugin_id=%s AND version=%s{suffix}
                """,
                (
                    _required_text(plugin_id, "plugin_id"),
                    _required_text(version, "version"),
                ),
            )
            return _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._VERSION_JSON_FIELDS,
            )

    def register_package_version(
        self,
        *,
        package: Mapping[str, Any],
        version: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Insert an immutable signed version, idempotent by digest."""

        plugin_id = _required_text(package.get("plugin_id"), "plugin_id")
        version_name = _required_text(version.get("version"), "version")
        raw_trust_source = version.get("trust_source")
        trust_source = _required_text(
            getattr(raw_trust_source, "value", raw_trust_source),
            "trust_source",
        )
        if trust_source not in _PLUGIN_TRUST_SOURCES:
            raise ValueError("trust_source is not a supported signed-package authority")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_plugin_packages (
                    plugin_id, display_name, description, latest_version,
                    state, record_version
                ) VALUES (%s, %s, %s, %s, 'REGISTERED', 1)
                ON DUPLICATE KEY UPDATE plugin_id=plugin_id
                """,
                (
                    plugin_id,
                    _required_text(package.get("display_name"), "display_name"),
                    _required_text(package.get("description"), "description"),
                    version_name,
                ),
            )
            cursor.execute(
                """
                INSERT INTO automation_plugin_versions (
                    plugin_id, version, package_sha256, manifest_sha256,
                    manifest_json, tool_contract_sha256, config_schema_sha256,
                    allowed_entrypoints_sha256, invocation_contracts_sha256,
                    worker_requirement_sha256, runtime_sha256, scheduling_sha256,
                    project_full_auto_allowed, trust_source,
                    install_root_metadata_json, install_root_metadata_sha256,
                    installed_by_actor_id, state
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, 'INSTALLED'
                )
                ON DUPLICATE KEY UPDATE plugin_id=plugin_id
                """,
                (
                    plugin_id,
                    version_name,
                    _sha256(version.get("package_sha256"), "package_sha256"),
                    _sha256(version.get("manifest_sha256"), "manifest_sha256"),
                    _json_param(version.get("manifest_json"), {}),
                    _sha256(version.get("tool_contract_sha256"), "tool_contract_sha256"),
                    _sha256(version.get("config_schema_sha256"), "config_schema_sha256"),
                    _sha256(
                        version.get("allowed_entrypoints_sha256"),
                        "allowed_entrypoints_sha256",
                    ),
                    _sha256(
                        version.get("invocation_contracts_sha256"),
                        "invocation_contracts_sha256",
                    ),
                    _sha256(
                        version.get("worker_requirement_sha256"),
                        "worker_requirement_sha256",
                    ),
                    _sha256(version.get("runtime_sha256"), "runtime_sha256"),
                    _sha256(version.get("scheduling_sha256"), "scheduling_sha256"),
                    bool(version.get("project_full_auto_allowed")),
                    trust_source,
                    _json_param(version.get("install_root_metadata_json"), {}),
                    _sha256(
                        version.get("install_root_metadata_sha256"),
                        "install_root_metadata_sha256",
                    ),
                    _required_text(
                        version.get("installed_by_actor_id"),
                        "installed_by_actor_id",
                    ),
                ),
            )
        persisted = self.get_version(plugin_id, version_name, for_update=True)
        if persisted is None:
            raise OrchestrationPersistenceError("plugin version did not persist")
        immutable_fields = (
            "package_sha256",
            "manifest_sha256",
            "tool_contract_sha256",
            "config_schema_sha256",
            "allowed_entrypoints_sha256",
            "invocation_contracts_sha256",
            "worker_requirement_sha256",
            "runtime_sha256",
            "scheduling_sha256",
            "install_root_metadata_sha256",
            "trust_source",
        )
        if any(
            str(persisted.get(field_name) or "")
            != (
                trust_source
                if field_name == "trust_source"
                else str(version.get(field_name) or "")
            )
            for field_name in immutable_fields
        ) or bool(persisted.get("project_full_auto_allowed")) != bool(
            version.get("project_full_auto_allowed")
        ) or _json_hash(persisted.get("manifest_json")) != _json_hash(
            version.get("manifest_json")
        ) or _json_hash(persisted.get("install_root_metadata_json")) != _json_hash(
            version.get("install_root_metadata_json")
        ):
            raise IdempotencyConflict("immutable plugin version already has different bytes")
        return persisted

    def get_project(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM automation_projects WHERE automation_id=%s{suffix}",
                (_required_text(automation_id, "automation_id"),),
            )
            return _row_dict(cursor, cursor.fetchone())

    def list_projects(self, *, include_uninstalling: bool = True) -> list[dict[str, Any]]:
        where = "" if include_uninstalling else " WHERE state <> 'UNINSTALLING'"
        with self.cursor() as cursor:
            cursor.execute(f"SELECT * FROM automation_projects{where} ORDER BY automation_id")
            return _rows(cursor)

    def install_project_instance(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one server-generated instance id with request idempotency."""

        automation_id = _required_text(row.get("automation_id"), "automation_id")
        plugin_id = _required_text(row.get("plugin_id"), "plugin_id")
        request_id = _required_text(row.get("install_request_id"), "install_request_id")
        payload_sha = _sha256(row.get("install_payload_sha256"), "install_payload_sha256")
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_projects (
                    automation_id, plugin_id, plugin_version, display_name,
                    enabled, state, install_request_id, install_payload_sha256,
                    installed_by_actor_id, migration_authority, record_version
                ) VALUES (
                    %s, %s, %s, %s, FALSE, 'INSTALLED', %s, %s, %s, %s, 1
                )
                ON DUPLICATE KEY UPDATE automation_id=automation_id
                """,
                (
                    automation_id,
                    plugin_id,
                    _required_text(row.get("plugin_version"), "plugin_version"),
                    _required_text(row.get("display_name"), "display_name"),
                    request_id,
                    payload_sha,
                    _required_text(row.get("installed_by_actor_id"), "installed_by_actor_id"),
                    bool(row.get("migration_authority")),
                ),
            )
            cursor.execute(
                """
                SELECT * FROM automation_projects
                WHERE install_request_id=%s
                FOR UPDATE
                """,
                (request_id,),
            )
            persisted = _row_dict(cursor, cursor.fetchone())
        if persisted is None:
            raise OrchestrationPersistenceError("automation instance did not persist")
        if (
            str(persisted.get("plugin_id") or "") != plugin_id
            or str(persisted.get("plugin_version") or "")
            != str(row.get("plugin_version") or "")
            or str(persisted.get("install_payload_sha256") or "") != payload_sha
            or str(persisted.get("display_name") or "")
            != str(row.get("display_name") or "")
        ):
            raise IdempotencyConflict("install request was reused with different input")
        return persisted

    def stage_project_upgrade(
        self,
        automation_id: str,
        *,
        plugin_id: str,
        from_version: str,
        to_version: str,
        package_sha256: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
        expected_record_version: int,
        prepared_configuration_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Stage an immutable target version without changing live execution.

        ``automation_projects.plugin_version`` is the desired package pointer.
        Existing invocations continue to resolve exclusively from the immutable
        committed-generation snapshot until ``commit_generation_cas_row``
        switches the route.  The request event makes a lost HTTP response safe
        to retry without advancing a second generation.
        """

        project_id = _required_text(automation_id, "automation_id")
        safe_plugin_id = _required_text(plugin_id, "plugin_id")
        safe_from = _required_text(from_version, "from_version")
        safe_to = _required_text(to_version, "to_version")
        safe_package_sha = _sha256(package_sha256, "package_sha256")
        safe_request = _canonical_uuid(request_id, "request_id")
        safe_actor = _required_text(actor_id, "actor_id")
        safe_role = _required_text(actor_role, "actor_role")
        if safe_role != "super_admin":
            raise ValueError("plugin upgrade requires a super_admin actor")
        safe_expected_version = _positive_int(
            expected_record_version,
            "expected_record_version",
        )
        safe_prepared_configuration_request = (
            _canonical_uuid(
                prepared_configuration_request_id,
                "prepared_configuration_request_id",
            )
            if prepared_configuration_request_id is not None
            else None
        )
        request_payload = {
            "plugin_id": safe_plugin_id,
            "to_version": safe_to,
            "package_sha256": safe_package_sha,
            "expected_record_version": safe_expected_version,
        }
        if safe_prepared_configuration_request is not None:
            request_payload["prepared_configuration_request_id"] = (
                safe_prepared_configuration_request
            )
        request_payload_sha256 = _json_hash(request_payload)

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM automation_project_events
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (project_id, safe_request),
            )
            prior_event = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("metadata_json",),
            )
            if prior_event is not None:
                metadata = prior_event.get("metadata_json")
                if (
                    str(prior_event.get("event_type") or "")
                    != "PLUGIN_UPGRADE_STAGED"
                    or not isinstance(metadata, Mapping)
                    or str(metadata.get("request_payload_sha256") or "")
                    != request_payload_sha256
                    or str(prior_event.get("actor_id") or "") != safe_actor
                    or str(prior_event.get("actor_role") or "") != safe_role
                ):
                    raise IdempotencyConflict(
                        "plugin upgrade request was reused with different input"
                    )
                persisted = self.get_project(project_id, for_update=True)
                if persisted is None:
                    raise OrchestrationPersistenceError(
                        "automation project disappeared after staged upgrade"
                    )
                persisted["_upgrade_staged_created"] = False
                return persisted

            cursor.execute(
                "SELECT * FROM automation_projects WHERE automation_id=%s FOR UPDATE",
                (project_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project is not installed")
            if (
                str(project.get("plugin_id") or "") != safe_plugin_id
                or str(project.get("plugin_version") or "") != safe_from
            ):
                raise ConcurrentUpdateError(
                    "automation project plugin version changed before upgrade"
                )
            if int(project.get("record_version") or 0) != safe_expected_version:
                raise ConcurrentUpdateError(
                    "automation project instance version changed before upgrade"
                )
            from_state = str(project.get("state") or "")
            if from_state not in {"INSTALLED", "ENABLED", "DISABLED"}:
                raise ConcurrentUpdateError(
                    "automation project cannot start an upgrade in its current state"
                )
            if safe_from == safe_to:
                raise ConcurrentUpdateError("plugin upgrade target is already active")

            cursor.execute(
                """
                SELECT package_sha256 FROM automation_plugin_versions
                WHERE plugin_id=%s AND version=%s FOR UPDATE
                """,
                (safe_plugin_id, safe_to),
            )
            target = _row_dict(cursor, cursor.fetchone())
            if target is None or str(target.get("package_sha256") or "") != safe_package_sha:
                raise OrchestrationPersistenceError(
                    "immutable plugin upgrade target is not registered"
                )
            current_config = self.get_project_config(project_id, for_update=True)
            if current_config is None:
                raise OrchestrationPersistenceError(
                    "automation project configuration is not initialized"
                )
            if safe_prepared_configuration_request is None:
                generation_stage = self.lock_project_generation_stage(
                    project_id,
                    project=project,
                    current_config=current_config,
                    allow_initial=False,
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM automation_project_events
                    WHERE automation_id=%s AND request_id=%s FOR UPDATE
                    """,
                    (project_id, safe_prepared_configuration_request),
                )
                prepared_event = _decode_row(
                    _row_dict(cursor, cursor.fetchone()),
                    ("metadata_json",),
                )
                prepared_metadata = (
                    prepared_event.get("metadata_json")
                    if isinstance(prepared_event, Mapping)
                    else None
                )
                if (
                    not isinstance(prepared_event, Mapping)
                    or prepared_event.get("event_type") != "CONFIGURATION_UPDATED"
                    or prepared_event.get("actor_id") != safe_actor
                    or prepared_event.get("actor_role") != safe_role
                    or not isinstance(prepared_metadata, Mapping)
                    or prepared_metadata.get("to_project_configuration_version")
                    != current_config.get("config_version")
                ):
                    raise ConcurrentUpdateError(
                        "prepared configuration request does not match the project target"
                    )
                try:
                    _sha256(
                        prepared_metadata.get("request_payload_sha256"),
                        "prepared_configuration.request_payload_sha256",
                    )
                except ValueError as exc:
                    raise ConcurrentUpdateError(
                        "prepared configuration request audit hash is invalid"
                    ) from exc
                cursor.execute(
                    """
                    SELECT project_generation, project_configuration_version
                    FROM automation_project_policies
                    WHERE automation_id=%s FOR UPDATE
                    """,
                    (project_id,),
                )
                prepared_policy = _row_dict(cursor, cursor.fetchone())
                if (
                    prepared_policy is None
                    or prepared_policy.get("project_generation")
                    != project.get("target_generation")
                    or prepared_policy.get("project_configuration_version")
                    != current_config.get("config_version")
                ):
                    raise ConcurrentUpdateError(
                        "prepared configuration policy is not bound to the project target"
                    )
                with self.cursor() as generation_cursor:
                    generation_cursor.execute(
                        """
                        SELECT generation, state
                        FROM automation_project_generations
                        WHERE automation_id=%s
                        ORDER BY generation FOR UPDATE
                        """,
                        (project_id,),
                    )
                    generation_rows = _rows(generation_cursor)
                generation_stage = _prepared_configuration_upgrade_stage(
                    project,
                    current_config,
                    generation_rows,
                )
            next_generation = generation_stage.target_generation

            event_metadata = {
                "request_payload_sha256": request_payload_sha256,
                "from_version": safe_from,
                "to_version": safe_to,
                "package_sha256": safe_package_sha,
                "target_generation": next_generation,
                "previous_state": from_state,
                "prepared_configuration_request_id": (
                    safe_prepared_configuration_request
                ),
            }
            cursor.execute(
                """
                INSERT INTO automation_project_events (
                    automation_id, request_id, event_type, from_state, to_state,
                    metadata_json, metadata_sha256, actor_id, actor_role
                ) VALUES (
                    %s, %s, 'PLUGIN_UPGRADE_STAGED', %s, 'UPGRADING',
                    %s, %s, %s, %s
                )
                """,
                (
                    project_id,
                    safe_request,
                    from_state,
                    _json_param(event_metadata, {}),
                    _json_hash(event_metadata),
                    safe_actor,
                    safe_role,
                ),
            )
            cursor.execute(
                """
                UPDATE automation_projects
                SET plugin_version=%s, state='UPGRADING',
                    target_generation=%s, reconcile_state='PREPARING',
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND plugin_id=%s
                  AND plugin_version=%s AND record_version=%s
                  AND state=%s AND target_generation=%s
                  AND committed_generation <=> %s AND reconcile_state=%s
                """,
                (
                    safe_to,
                    next_generation,
                    project_id,
                    safe_plugin_id,
                    safe_from,
                    safe_expected_version,
                    from_state,
                    generation_stage.prior_target_generation,
                    generation_stage.committed_generation,
                    generation_stage.prior_reconcile_state,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "automation project changed while staging plugin upgrade"
                )

        persisted = self.get_project(project_id, for_update=True)
        if persisted is None:
            raise OrchestrationPersistenceError(
                "automation project disappeared after staged upgrade"
            )
        persisted["_upgrade_staged_created"] = True
        return persisted

    def get_project_config(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        with self.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM automation_project_configs WHERE automation_id=%s{suffix}",
                (_required_text(automation_id, "automation_id"),),
            )
            config = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._CONFIG_JSON_FIELDS,
            )
            if config is None:
                return None
            cursor.execute(
                f"""
                SELECT id, cron_expression, enabled
                FROM scheduled_tasks
                WHERE automation_id=%s
                ORDER BY id{suffix}
                """,
                (_required_text(automation_id, "automation_id"),),
            )
            schedule_rows = _rows(cursor)
        desired_schedule = config.get("desired_schedule_json")
        if not isinstance(desired_schedule, Mapping):
            raise OrchestrationPersistenceError(
                "automation project desired schedule is invalid"
            )
        config["schedule"] = dict(desired_schedule)
        config["committed_schedule"] = _schedule_from_rows(schedule_rows)
        config["scheduled_task_ids"] = tuple(str(row["id"]) for row in schedule_rows)
        return config

    def initialize_project_config(
        self,
        automation_id: str,
        *,
        enabled_entrypoints: Sequence[str],
    ) -> dict[str, Any]:
        empty: dict[str, Any] = {}
        entrypoints = sorted({_required_text(item, "entrypoint") for item in enabled_entrypoints})
        with self.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_project_configs (
                    automation_id, config_json, config_sha256,
                    account_bindings_json, account_bindings_sha256,
                    resource_bindings_json, resource_bindings_sha256,
                    enabled_entrypoints_json, enabled_entrypoints_sha256,
                    desired_schedule_json, desired_schedule_sha256,
                    compiled_invocations_json, compiled_invocations_sha256,
                    device_binding_sha256, configured, config_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, FALSE, 1
                )
                ON DUPLICATE KEY UPDATE automation_id=automation_id
                """,
                (
                    _required_text(automation_id, "automation_id"),
                    _json_param(empty, {}),
                    _json_hash(empty),
                    _json_param(empty, {}),
                    _json_hash(empty),
                    _json_param(empty, {}),
                    _json_hash(empty),
                    _json_param(entrypoints, []),
                    _json_hash(entrypoints),
                    _json_param({"kind": "none", "times": [], "enabled": False}, {}),
                    _json_hash({"kind": "none", "times": [], "enabled": False}),
                    _json_param(empty, {}),
                    _json_hash(empty),
                    _json_hash(None),
                ),
            )
        config = self.get_project_config(automation_id, for_update=True)
        if config is None:
            raise OrchestrationPersistenceError("automation project config did not persist")
        return config

    def lock_project_generation_stage(
        self,
        automation_id: str,
        *,
        project: Mapping[str, Any],
        current_config: Mapping[str, Any],
        allow_initial: bool = False,
    ) -> AutomationProjectGenerationStage:
        project_id = _required_text(automation_id, "automation_id")
        if str(project.get("automation_id") or "") != project_id:
            raise OrchestrationPersistenceError(
                "automation project generation identity changed"
            )
        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT generation, state
                FROM automation_project_generations
                WHERE automation_id=%s
                ORDER BY generation FOR UPDATE
                """,
                (project_id,),
            )
            generation_rows = _rows(cursor)
        return _project_generation_stage(
            project,
            current_config,
            generation_rows,
            allow_initial=allow_initial,
        )

    def apply_project_generation_stage(
        self,
        stage: AutomationProjectGenerationStage,
    ) -> None:
        if not isinstance(stage, AutomationProjectGenerationStage):
            raise TypeError("stage must be an AutomationProjectGenerationStage")
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_projects
                SET target_generation=%s, reconcile_state='PREPARING',
                    record_version=record_version+1, updated_at=NOW(6)
                WHERE automation_id=%s
                  AND record_version=%s
                  AND target_generation=%s
                  AND committed_generation <=> %s
                  AND reconcile_state=%s
                  AND state=%s
                """,
                (
                    stage.target_generation,
                    stage.automation_id,
                    stage.expected_record_version,
                    stage.prior_target_generation,
                    stage.committed_generation,
                    stage.prior_reconcile_state,
                    stage.project_state,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "automation project cannot stage a generation"
                )

    def save_project_config(
        self,
        automation_id: str,
        *,
        config: Mapping[str, Any],
        account_bindings: Mapping[str, Any],
        resource_bindings: Mapping[str, Any],
        enabled_entrypoints: Sequence[str],
        schedule: Mapping[str, Any],
        compiled_invocations: Mapping[str, Mapping[str, Any]],
        device_binding: Any | None,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_project_configuration_version: int,
    ) -> dict[str, Any]:
        """CAS project settings, schedules and authorization in one Unit of Work.

        ``scheduled_tasks`` is the sole schedule persistence authority.  Task
        identities and cron strings are derived server-side; callers can only
        supply the closed schedule DTO and already validated signed invocation
        materialization.
        """

        project_id = _required_text(automation_id, "automation_id")
        expected_version = _positive_int(
            expected_project_configuration_version,
            "expected_project_configuration_version",
        )
        normalized_actor = _required_text(actor_id, "actor_id")
        normalized_role = _required_text(actor_role, "actor_role")
        normalized_request = _required_text(request_id, "request_id")
        if not all(
            isinstance(value, Mapping)
            for value in (config, account_bindings, resource_bindings, compiled_invocations)
        ):
            raise ValueError("project configuration payloads must be objects")
        entrypoints = tuple(
            sorted({_required_text(item, "entrypoint") for item in enabled_entrypoints})
        )
        if len(entrypoints) != len(tuple(enabled_entrypoints)):
            raise ValueError("enabled_entrypoints must be a unique list")
        if set(compiled_invocations) != set(entrypoints):
            raise ValueError("compiled invocations must exactly match enabled entrypoints")
        normalized_compiled: dict[str, dict[str, Any]] = {}
        for entrypoint in entrypoints:
            compiled = compiled_invocations.get(entrypoint)
            if not isinstance(compiled, Mapping) or set(compiled) != {
                "arguments",
                "dynamic_resolvers",
            }:
                raise ValueError("compiled invocation payload is invalid")
            arguments = compiled.get("arguments")
            resolvers = compiled.get("dynamic_resolvers")
            if not isinstance(arguments, Mapping) or not isinstance(resolvers, Mapping):
                raise ValueError("compiled invocation arguments/resolvers must be objects")
            if any(
                not isinstance(field, str)
                or not isinstance(resolver, str)
                or not field
                or not resolver
                for field, resolver in resolvers.items()
            ):
                raise ValueError("compiled dynamic resolvers are invalid")
            normalized_compiled[entrypoint] = {
                "arguments": dict(arguments),
                "dynamic_resolvers": dict(resolvers),
            }
        normalized_schedule = _normalized_project_schedule(schedule)
        # Schedule times remain persisted while the scheduler entrypoint is off.
        # Generation commit materializes those rows disabled, so re-enabling the
        # entrypoint restores the same administrator-configured times.
        device_id = _device_binding_id(device_binding)
        request_payload = {
            "config": dict(config),
            "account_bindings": dict(account_bindings),
            "resource_bindings": dict(resource_bindings),
            "enabled_entrypoints": list(entrypoints),
            "schedule": normalized_schedule,
            "compiled_invocations": normalized_compiled,
            "device_id": device_id,
            "expected_project_configuration_version": expected_version,
        }
        request_payload_sha256 = _json_hash(request_payload)

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT project.*, version.manifest_json
                FROM automation_projects AS project
                INNER JOIN automation_plugin_versions AS version
                  ON version.plugin_id=project.plugin_id
                 AND version.version=project.plugin_version
                WHERE project.automation_id=%s
                FOR UPDATE
                """,
                (project_id,),
            )
            project = _row_dict(cursor, cursor.fetchone())
            if project is None:
                raise OrchestrationPersistenceError("automation project is not installed")
            _stageable_project_state(project.get("state"))
            manifest = _json_value(project.get("manifest_json"), {})
            if not isinstance(manifest, Mapping):
                raise OrchestrationPersistenceError("persisted plugin manifest is invalid")
            allowed_entrypoints = manifest.get("allowed_entrypoints")
            scheduling = manifest.get("scheduling")
            tool_contract = manifest.get("tool_contract")
            runtime = manifest.get("runtime")
            if (
                not isinstance(allowed_entrypoints, list)
                or not set(entrypoints) <= set(str(item) for item in allowed_entrypoints)
                or not isinstance(scheduling, Mapping)
                or not isinstance(tool_contract, Mapping)
                or not isinstance(runtime, Mapping)
            ):
                raise OrchestrationPersistenceError("persisted plugin contract is invalid")
            if (
                normalized_schedule["kind"] != "none"
                and scheduling.get("supported") is not True
            ):
                raise OrchestrationPersistenceError("plugin does not support schedules")
            tool_name = _required_text(tool_contract.get("name"), "tool_contract.name")
            if runtime.get("kind") == "core_tool_ref" and runtime.get("tool_name") != tool_name:
                raise OrchestrationPersistenceError("plugin runtime/tool identity mismatch")

            cursor.execute(
                """
                SELECT * FROM automation_project_configs
                WHERE automation_id=%s FOR UPDATE
                """,
                (project_id,),
            )
            current_config = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                self._CONFIG_JSON_FIELDS,
            )
            if current_config is None:
                raise OrchestrationPersistenceError("automation project config is not initialized")

            cursor.execute(
                """
                SELECT * FROM automation_project_events
                WHERE automation_id=%s AND request_id=%s FOR UPDATE
                """,
                (project_id, normalized_request),
            )
            prior_event = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("metadata_json",),
            )
            if prior_event is not None:
                metadata = prior_event.get("metadata_json")
                if (
                    prior_event.get("event_type") != "CONFIGURATION_UPDATED"
                    or not isinstance(metadata, Mapping)
                    or metadata.get("request_payload_sha256") != request_payload_sha256
                ):
                    raise IdempotencyConflict(
                        "configuration request was reused with different input"
                    )
                persisted = self.get_project_config(project_id, for_update=True)
                if persisted is None:
                    raise OrchestrationPersistenceError(
                        "automation project config disappeared after idempotent save"
                    )
                return persisted

            current_version = int(current_config.get("config_version") or 0)
            if current_version != expected_version:
                raise ConcurrentUpdateError("automation project config version changed")
            next_version = current_version + 1

            generation_stage = self.lock_project_generation_stage(
                project_id,
                project=project,
                current_config=current_config,
                allow_initial=True,
            )
            target_generation = generation_stage.target_generation
            committed_generation = generation_stage.committed_generation
            generation_numbers = generation_stage.existing_generations
            cursor.execute(
                """
                SELECT * FROM automation_project_policies
                WHERE automation_id=%s FOR UPDATE
                """,
                (project_id,),
            )
            policy = _decode_row(
                _row_dict(cursor, cursor.fetchone()),
                ("contract_snapshot_json",),
            )
            if policy is None and committed_generation is not None:
                raise OrchestrationPersistenceError(
                    "configured automation project policy is missing"
                )
            if policy is not None:
                mode = str(policy.get("mode") or "")
                try:
                    policy_version = _positive_int(
                        policy.get("version"),
                        "policy.version",
                    )
                    policy_generation = _positive_int(
                        policy.get("project_generation"),
                        "policy.project_generation",
                    )
                    policy_config_version = _positive_int(
                        policy.get("project_configuration_version"),
                        "policy.project_configuration_version",
                    )
                except ValueError as exc:
                    raise OrchestrationPersistenceError(
                        "automation project policy binding is invalid"
                    ) from exc
                if mode not in PROJECT_POLICY_MODES:
                    raise OrchestrationPersistenceError(
                        "automation project policy mode is invalid"
                    )
                if committed_generation is None:
                    if (
                        mode != "PROJECT_FULL_AUTO"
                        or policy_generation != target_generation
                        or policy_config_version != current_version
                    ):
                        raise ConcurrentUpdateError(
                            "initial automation project policy binding changed"
                        )
                elif (
                    policy_generation > committed_generation
                    or policy_config_version > current_version
                    or policy_generation not in generation_numbers
                ):
                    raise ConcurrentUpdateError(
                        "automation project policy is not bound to the stable lineage"
                    )
            else:
                policy_version = None

            device_binding_sha256 = _json_hash(None)
            if device_id is not None:
                cursor.execute(
                    """
                    SELECT device_id, paired_public_key_fingerprint,
                           capabilities_sha256, record_version
                    FROM automation_worker_devices
                    WHERE device_id=%s FOR UPDATE
                    """,
                    (device_id,),
                )
                device = _row_dict(cursor, cursor.fetchone())
                if device is None:
                    raise OrchestrationPersistenceError("named worker device is not paired")
                device_binding_sha256 = _json_hash(
                    {
                        "device_id": device_id,
                        "paired_public_key_fingerprint": _required_text(
                            device.get("paired_public_key_fingerprint"),
                            "paired_public_key_fingerprint",
                        ),
                        "capabilities_sha256": _sha256(
                            device.get("capabilities_sha256"),
                            "capabilities_sha256",
                        ),
                        "record_version": _positive_int(
                            int(device.get("record_version") or 0),
                            "device.record_version",
                        ),
                    }
                )

            cursor.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE automation_id=%s
                ORDER BY id FOR UPDATE
                """,
                (project_id,),
            )
            existing_tasks = [
                _decode_row(row, ("tool_params",)) or {}
                for row in _rows(cursor)
            ]
            by_cron: dict[str, dict[str, Any]] = {}
            for task in existing_tasks:
                cron = str(task.get("cron_expression") or "")
                if cron in by_cron:
                    raise OrchestrationPersistenceError(
                        "project schedule contains duplicate cron rows"
                    )
                by_cron[cron] = task

            expressions = _schedule_expressions(normalized_schedule)
            scheduler_arguments = normalized_compiled.get("scheduler", {}).get(
                "arguments", {}
            )
            if expressions and not isinstance(scheduler_arguments, Mapping):
                raise ValueError("scheduler invocation arguments are invalid")
            target_tasks: list[dict[str, Any]] = []
            for expression in expressions:
                existing = by_cron.get(expression)
                task_id = (
                    str(existing["id"])
                    if existing is not None
                    else _stable_schedule_task_id(project_id, expression)
                )
                suffix = (
                    "启动时"
                    if expression == "@startup"
                    else _schedule_from_rows(
                        [{"cron_expression": expression, "enabled": True}]
                    )["times"][0]
                )
                target_tasks.append(
                    {
                        "id": task_id,
                        "name": (
                            str(existing.get("name") or "")
                            if existing is not None
                            else f"{project.get('display_name') or project_id} {suffix}"
                        )[:128],
                        "cron_expression": expression,
                        "tool_params": dict(scheduler_arguments),
                    }
                )

            if target_tasks:
                placeholders = ", ".join(["%s"] * len(target_tasks))
                cursor.execute(
                    f"""
                    SELECT id, automation_id FROM scheduled_tasks
                    WHERE id IN ({placeholders}) FOR UPDATE
                    """,
                    tuple(task["id"] for task in target_tasks),
                )
                collisions = [
                    row
                    for row in _rows(cursor)
                    if str(row.get("automation_id") or "") != project_id
                ]
                if collisions:
                    raise OrchestrationPersistenceError(
                        "server-derived schedule identity collided with another project"
                    )

            cursor.execute(
                """
                SELECT task.id, policy.*
                FROM scheduled_tasks AS task
                LEFT JOIN scheduled_task_approval_policies AS policy
                  ON policy.task_id=task.id
                WHERE task.automation_id=%s
                ORDER BY task.id FOR UPDATE
                """,
                (project_id,),
            )
            legacy_policies = _rows(cursor)
            for legacy in legacy_policies:
                if legacy.get("mode") != "EXACT_SCHEDULE_EXEMPT":
                    continue
                cursor.execute(
                    """
                    INSERT INTO scheduled_task_approval_policy_events (
                        task_id, from_mode, to_mode, contract_hash,
                        contract_snapshot_json, tool_contract_hash,
                        actor_id, actor_role, reason, comment,
                        correlation_id, request_id
                    ) VALUES (
                        %s, 'EXACT_SCHEDULE_EXEMPT', 'REQUIRE_EACH_RUN',
                        NULL, NULL, NULL, %s, %s,
                        'PROJECT_CONFIGURATION_CHANGED', NULL, %s, %s
                    )
                    """,
                    (
                        legacy["id"],
                        normalized_actor,
                        normalized_role,
                        normalized_request,
                        normalized_request,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE scheduled_task_approval_policies
                    SET mode='REQUIRE_EACH_RUN', contract_hash=NULL,
                        contract_snapshot_json=NULL, tool_contract_hash=NULL,
                        approved_by_actor_id=NULL, approved_by_actor_role=NULL,
                        approved_by_actor_display_name=NULL, approved_at=NULL,
                        comment=NULL, version=version+1, updated_at=NOW(6)
                    WHERE task_id=%s
                    """,
                    (legacy["id"],),
                )

            cursor.execute(
                """
                UPDATE automation_project_configs
                SET config_json=%s, config_sha256=%s,
                    account_bindings_json=%s, account_bindings_sha256=%s,
                    resource_bindings_json=%s, resource_bindings_sha256=%s,
                    enabled_entrypoints_json=%s, enabled_entrypoints_sha256=%s,
                    desired_schedule_json=%s, desired_schedule_sha256=%s,
                    compiled_invocations_json=%s, compiled_invocations_sha256=%s,
                    device_id=%s, device_binding_sha256=%s,
                    configured=TRUE, config_version=%s,
                    updated_by_actor_id=%s, updated_at=NOW(6)
                WHERE automation_id=%s AND config_version=%s
                """,
                (
                    _json_param(config, {}),
                    _json_hash(config),
                    _json_param(account_bindings, {}),
                    _json_hash(account_bindings),
                    _json_param(resource_bindings, {}),
                    _json_hash(resource_bindings),
                    _json_param(list(entrypoints), []),
                    _json_hash(list(entrypoints)),
                    _json_param(normalized_schedule, {}),
                    _json_hash(normalized_schedule),
                    _json_param(normalized_compiled, {}),
                    _json_hash(normalized_compiled),
                    device_id,
                    device_binding_sha256,
                    next_version,
                    normalized_actor,
                    project_id,
                    expected_version,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("automation project config version changed")

            self.apply_project_generation_stage(generation_stage)

            if policy is None:
                cursor.execute(
                    """
                    INSERT INTO automation_project_policies (
                        automation_id, project_generation, mode,
                        project_configuration_version, version
                    ) VALUES (%s, %s, 'PROJECT_FULL_AUTO', %s, 1)
                    """,
                    (project_id, target_generation, next_version),
                )
                cursor.execute(
                    """
                    SELECT * FROM automation_project_policies
                    WHERE automation_id=%s FOR UPDATE
                    """,
                    (project_id,),
                )
                policy = _decode_row(
                    _row_dict(cursor, cursor.fetchone()),
                    ("contract_snapshot_json",),
                )
                if policy is None:
                    raise OrchestrationPersistenceError(
                        "project policy did not persist"
                    )
                policy_version = _positive_int(
                    policy.get("version"),
                    "policy.version",
                )
            cursor.execute(
                """
                INSERT INTO automation_project_policy_events (
                    automation_id, request_id, from_mode, to_mode,
                    contract_hash, contract_snapshot_json, tool_contract_hash,
                    plugin_contract_hash, project_generation,
                    project_configuration_version,
                    actor_id, actor_role, reason, correlation_id
                ) VALUES (
                    %s, %s, %s, %s, NULL, NULL, NULL, NULL,
                    %s, %s, %s, %s, 'PROJECT_CONFIGURATION_CHANGED', %s
                )
                """,
                (
                    project_id,
                    normalized_request,
                    policy.get("mode"),
                    policy.get("mode"),
                    target_generation,
                    next_version,
                    normalized_actor,
                    normalized_role,
                    normalized_request,
                ),
            )
            cursor.execute(
                """
                UPDATE automation_project_policies
                SET contract_hash=NULL, contract_snapshot_json=NULL,
                    tool_contract_hash=NULL,
                    plugin_contract_hash=NULL,
                    project_generation=%s,
                    project_configuration_version=%s,
                    version=version+1, updated_at=NOW(6)
                WHERE automation_id=%s AND version=%s
                """,
                (
                    target_generation,
                    next_version,
                    project_id,
                    policy_version,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError(
                    "automation project policy changed during configuration save"
                )
            event_metadata = {
                "request_payload_sha256": request_payload_sha256,
                "from_project_configuration_version": current_version,
                "to_project_configuration_version": next_version,
                "schedule_sha256": _json_hash(normalized_schedule),
                "scheduled_task_count": len(target_tasks),
            }
            cursor.execute(
                """
                INSERT INTO automation_project_events (
                    automation_id, request_id, event_type, from_state, to_state,
                    metadata_json, metadata_sha256, actor_id, actor_role
                ) VALUES (
                    %s, %s, 'CONFIGURATION_UPDATED', %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    project_id,
                    normalized_request,
                    project.get("state"),
                    project.get("state"),
                    _json_param(event_metadata, {}),
                    _json_hash(event_metadata),
                    normalized_actor,
                    normalized_role,
                ),
            )

        persisted = self.get_project_config(project_id, for_update=True)
        if persisted is None:
            raise OrchestrationPersistenceError("automation project config disappeared")
        return persisted

    def set_project_enabled(
        self,
        automation_id: str,
        *,
        enabled: bool,
        expected_record_version: int,
    ) -> dict[str, Any]:
        _positive_int(expected_record_version, "expected_record_version")
        state = "ENABLED" if enabled else "DISABLED"
        with self.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_projects
                SET enabled=%s, state=%s, record_version=record_version+1,
                    updated_at=NOW(6)
                WHERE automation_id=%s AND record_version=%s
                  AND state NOT IN ('UNINSTALLING', 'UPGRADING')
                """,
                (
                    bool(enabled),
                    state,
                    _required_text(automation_id, "automation_id"),
                    expected_record_version,
                ),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                raise ConcurrentUpdateError("automation project instance version changed")
        project = self.get_project(automation_id, for_update=True)
        if project is None:
            raise OrchestrationPersistenceError("automation project disappeared")
        return project

    def list_invalid_scheduled_project_links(self) -> list[dict[str, Any]]:
        """Return only safe identifiers; never include cron parameters."""

        with self.cursor() as cursor:
            cursor.execute(
                """
                SELECT task.id AS task_id, task.tool_name, task.automation_id,
                       CASE
                           WHEN task.automation_id IS NULL OR TRIM(task.automation_id)=''
                               THEN 'AUTOMATION_ID_REQUIRED'
                           WHEN project.automation_id IS NULL THEN 'PLUGIN_NOT_INSTALLED'
                           ELSE 'PLUGIN_DISABLED'
                       END AS error_code
                FROM scheduled_tasks AS task
                LEFT JOIN automation_projects AS project
                  ON project.automation_id=task.automation_id
                WHERE task.automation_id IS NULL OR TRIM(task.automation_id)=''
                   OR project.automation_id IS NULL
                   OR (
                       task.enabled=TRUE
                       AND (project.enabled=FALSE OR project.state <> 'ENABLED')
                   )
                ORDER BY task.id
                """
            )
            return _rows(cursor)
