"""Closed, credential-free entrypoint ownership contract for v1 -> v2 migration."""

from __future__ import annotations

import copy
from typing import Any, Mapping


MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA = (
    "plugin-migration-entrypoint-ownership/1"
)
MIGRATION_ENTRYPOINT_KINDS = ("console", "scheduler", "feishu")
MIGRATION_OWNERSHIP_STATES = (
    "PREPARING",
    "TESTING",
    "READY",
    "CUTOVER",
    "ROLLED_BACK",
)
MIGRATION_PERSISTED_PAIR_STATES = (
    "PREPARING",
    "TESTING",
    "READY",
    "CUTTING_OVER",
    "CUTOVER",
    "ROLLING_BACK",
    "ROLLED_BACK",
    "COMPLETED",
    "ERROR",
)


def _identifier(value: object, field: str) -> str:
    text = str(value or "")
    if not text or text != text.strip() or len(text) > 191 or "\x00" in text:
        raise ValueError(f"{field} is invalid")
    return text


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field)


def _route_contract(
    value: object,
    *,
    kind: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"migration {kind} ownership is invalid")
    expected = {"source_enabled", "target_contribution_id"}
    if kind == "scheduler":
        expected.add("schedule_mode")
    if kind == "feishu":
        expected.update(
            {
                "source_tool_name",
                "source_route_key",
                "source_resource_id",
                "commands",
            }
        )
    if set(value) != expected or not isinstance(value.get("source_enabled"), bool):
        raise ValueError(f"migration {kind} ownership is invalid")
    source_enabled = value["source_enabled"]
    target_id = _optional_identifier(
        value.get("target_contribution_id"),
        f"migration {kind} target contribution",
    )
    result: dict[str, Any] = {
        "source_enabled": source_enabled,
        "target_contribution_id": target_id,
    }
    if kind == "console":
        if target_id is None:
            raise ValueError("migration Console target contribution is missing")
        return result
    if kind == "scheduler":
        schedule_mode = str(value.get("schedule_mode") or "")
        expected_mode = "COPY_SOURCE" if source_enabled else "NONE"
        if schedule_mode != expected_mode or (source_enabled != (target_id is not None)):
            raise ValueError("migration scheduler ownership is invalid")
        result["schedule_mode"] = schedule_mode
        return result

    source_tool = _optional_identifier(
        value.get("source_tool_name"),
        "migration Feishu source tool",
    )
    source_route = _optional_identifier(
        value.get("source_route_key"),
        "migration Feishu source route",
    )
    source_resource_id = _optional_identifier(
        value.get("source_resource_id"),
        "migration Feishu source resource",
    )
    commands = value.get("commands")
    if not isinstance(commands, (list, tuple)):
        raise ValueError("migration Feishu commands are invalid")
    normalized_commands = tuple(
        _identifier(command, "migration Feishu command") for command in commands
    )
    if (
        any(len(command) > 128 for command in normalized_commands)
        or len(normalized_commands) != len(set(normalized_commands))
        or source_enabled
        != bool(
            source_tool
            and source_route
            and source_resource_id
            and target_id
            and normalized_commands
        )
    ):
        raise ValueError("migration Feishu ownership is invalid")
    result.update(
        {
            "source_tool_name": source_tool,
            "source_route_key": source_route,
            "source_resource_id": source_resource_id,
            "commands": list(normalized_commands),
        }
    )
    return result


def _expected_owner(*, source_enabled: bool, state: str) -> str:
    if not source_enabled:
        return "NONE"
    return "SERVICE_V2" if state == "CUTOVER" else "ACTION_V1"


def normalize_migration_entrypoint_ownership(
    value: object,
) -> dict[str, Any]:
    """Return a closed ownership contract or reject the entire snapshot."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "console",
        "scheduler",
        "feishu",
        "owners",
    }:
        raise ValueError("migration entrypoint ownership is invalid")
    if value.get("schema") != MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA:
        raise ValueError("migration entrypoint ownership schema is invalid")
    routes = {
        kind: _route_contract(value.get(kind), kind=kind)
        for kind in MIGRATION_ENTRYPOINT_KINDS
    }
    owners = value.get("owners")
    if not isinstance(owners, Mapping) or set(owners) != set(
        MIGRATION_OWNERSHIP_STATES
    ):
        raise ValueError("migration state ownership is invalid")
    normalized_owners: dict[str, dict[str, str]] = {}
    for state in MIGRATION_OWNERSHIP_STATES:
        state_owners = owners.get(state)
        if not isinstance(state_owners, Mapping) or set(state_owners) != set(
            MIGRATION_ENTRYPOINT_KINDS
        ):
            raise ValueError("migration state ownership is invalid")
        expected = {
            kind: _expected_owner(
                source_enabled=bool(routes[kind]["source_enabled"]),
                state=state,
            )
            for kind in MIGRATION_ENTRYPOINT_KINDS
        }
        if dict(state_owners) != expected:
            raise ValueError("migration state ownership is invalid")
        normalized_owners[state] = expected
    return {
        "schema": MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA,
        **{kind: copy.deepcopy(routes[kind]) for kind in MIGRATION_ENTRYPOINT_KINDS},
        "owners": normalized_owners,
    }


def migration_entrypoint_owner(
    ownership: object,
    *,
    state: str,
    kind: str,
) -> str:
    normalized = normalize_migration_entrypoint_ownership(ownership)
    if state not in MIGRATION_OWNERSHIP_STATES or kind not in MIGRATION_ENTRYPOINT_KINDS:
        raise ValueError("migration ownership lookup is invalid")
    return str(normalized["owners"][state][kind])


def migration_effective_ownership_state(
    state: object,
    *,
    rolled_back_at: object | None = None,
) -> str | None:
    """Map durable pair states to a stable owner, or require fail-closed.

    ``CUTTING_OVER``/``ROLLING_BACK`` and ``ERROR`` intentionally have no
    inferred owner.  The database transaction normally skips those transient
    states; if one is present, a router must not guess which side is live.
    """

    normalized = str(state or "")
    if normalized in MIGRATION_OWNERSHIP_STATES:
        return normalized
    if normalized == "COMPLETED":
        return "ROLLED_BACK" if rolled_back_at is not None else "CUTOVER"
    if normalized in {"CUTTING_OVER", "ROLLING_BACK", "ERROR"}:
        return None
    raise ValueError("migration pair state is invalid")


__all__ = [
    "MIGRATION_ENTRYPOINT_OWNERSHIP_SCHEMA",
    "MIGRATION_ENTRYPOINT_KINDS",
    "MIGRATION_OWNERSHIP_STATES",
    "MIGRATION_PERSISTED_PAIR_STATES",
    "migration_entrypoint_owner",
    "migration_effective_ownership_state",
    "normalize_migration_entrypoint_ownership",
]
