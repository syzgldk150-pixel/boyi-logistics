"""Strict validators for retired automation-project policy writers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


CREDENTIAL_POLICY_DOWNGRADE_REASON = "ACCOUNT_CREDENTIAL_CHANGED"
CREDENTIAL_POLICY_RESTORE_REASON = "MIGRATION_022_CREDENTIAL_FULL_AUTO"
PLUGIN_POLICY_DOWNGRADE_REASON = "PLUGIN_VERSION_CHANGED"
PLUGIN_POLICY_RESTORE_REASON = "MIGRATION_022_PLUGIN_FULL_AUTO"
_CREDENTIAL_ACTOR_ID = "system:account-credential-change"
_CREDENTIAL_DISPLAY_NAME = "Account credential safety guard"
_CREDENTIAL_COMMENT = (
    "Project full-auto authorization revoked before bound credentials changed"
)
_RESTORE_ACTOR_ID = "system:migration:automation-credential-full-auto-v1"
_RESTORE_DISPLAY_NAME = "Migration 022"
_RESTORE_COMMENT = "Restored durable full-auto after legacy credential guard"
_PLUGIN_RESTORE_ACTOR_ID = "system:migration:automation-plugin-full-auto-v1"
_PLUGIN_RESTORE_DISPLAY_NAME = "Migration 022"
_PLUGIN_RESTORE_COMMENT = "Restored durable full-auto after legacy plugin downgrade"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CONTRACT_FIELDS = (
    "contract_hash",
    "contract_snapshot_json",
    "tool_contract_hash",
    "plugin_contract_hash",
)


def _valid_uuid(value: Any) -> bool:
    return type(value) is str and _UUID_RE.fullmatch(value) is not None


def _valid_request(value: Any, *, prefix: str, automation_id: str) -> bool:
    return type(value) is str and value == f"{prefix}:{automation_id}"


def _is_strict_plugin_downgrade(event: Mapping[str, Any]) -> bool:
    """Check the immutable fields written by the retired plugin upgrader.

    The project-event metadata is validated by the release manifest's joined
    evidence validator.  This local check deliberately covers the fields that
    a policy restore can bind to without querying another table.
    """

    request_id = event.get("request_id")
    return bool(
        all(event.get(field) is None for field in _CONTRACT_FIELDS)
        and event.get("from_mode") == "PROJECT_FULL_AUTO"
        and event.get("to_mode") == "REQUIRE_EACH_RUN"
        and event.get("reason") == PLUGIN_POLICY_DOWNGRADE_REASON
        and event.get("actor_role") == "super_admin"
        and type(event.get("actor_id")) is str
        and bool(event.get("actor_id"))
        and event.get("actor_display_name") is None
        and event.get("comment") is None
        and _valid_uuid(request_id)
        and event.get("correlation_id") == request_id
    )


def validate_credential_policy_history_event(
    *,
    automation_id: str,
    event: Mapping[str, Any],
    previous_event: Mapping[str, Any],
) -> bool | None:
    """Return the resulting full-auto authority for a retired writer event.

    ``None`` means the event belongs to another writer. ``False`` is the
    historical credential safety downgrade and ``True`` is its one-time 022
    repair. Both shapes remain part of the immutable audit chain even though
    current credential changes no longer alter durable project intent.
    """

    reason = event.get("reason")
    if reason not in {
        CREDENTIAL_POLICY_DOWNGRADE_REASON,
        CREDENTIAL_POLICY_RESTORE_REASON,
        PLUGIN_POLICY_RESTORE_REASON,
    }:
        return None
    common_valid = (
        all(event.get(field) is None for field in _CONTRACT_FIELDS)
        and event.get("actor_role") == "system"
        and _valid_uuid(event.get("correlation_id"))
    )
    if reason == CREDENTIAL_POLICY_DOWNGRADE_REASON:
        request_id = event.get("request_id")
        request_prefix = (
            request_id[: -(len(automation_id) + 1)]
            if type(request_id) is str and request_id.endswith(f":{automation_id}")
            else None
        )
        if not (
            common_valid
            and _valid_uuid(request_prefix)
            and event.get("from_mode") == "PROJECT_FULL_AUTO"
            and event.get("to_mode") == "REQUIRE_EACH_RUN"
            and event.get("actor_id") == _CREDENTIAL_ACTOR_ID
            and event.get("actor_display_name") == _CREDENTIAL_DISPLAY_NAME
            and event.get("comment") == _CREDENTIAL_COMMENT
        ):
            raise ValueError("invalid historical credential policy downgrade")
        return False
    is_credential_restore = reason == CREDENTIAL_POLICY_RESTORE_REASON
    restore_prefix = (
        "migration-022-credential-full-auto"
        if is_credential_restore
        else "migration-022-plugin-full-auto"
    )
    restore_actor_id = (
        _RESTORE_ACTOR_ID if is_credential_restore else _PLUGIN_RESTORE_ACTOR_ID
    )
    restore_display_name = (
        _RESTORE_DISPLAY_NAME
        if is_credential_restore
        else _PLUGIN_RESTORE_DISPLAY_NAME
    )
    restore_comment = (
        _RESTORE_COMMENT if is_credential_restore else _PLUGIN_RESTORE_COMMENT
    )
    predecessor_valid = (
        previous_event.get("reason") == CREDENTIAL_POLICY_DOWNGRADE_REASON
        and previous_event.get("from_mode") == "PROJECT_FULL_AUTO"
        and previous_event.get("to_mode") == event.get("from_mode")
    ) if is_credential_restore else _is_strict_plugin_downgrade(previous_event)
    if not (
        common_valid
        and _valid_request(
            event.get("request_id"),
            prefix=restore_prefix,
            automation_id=automation_id,
        )
        and event.get("from_mode") == "REQUIRE_EACH_RUN"
        and event.get("to_mode") == "PROJECT_FULL_AUTO"
        and event.get("actor_id") == restore_actor_id
        and event.get("actor_display_name") == restore_display_name
        and event.get("comment") == restore_comment
        and predecessor_valid
        and previous_event.get("project_generation")
        == event.get("project_generation")
        and previous_event.get("project_configuration_version")
        == event.get("project_configuration_version")
    ):
        raise ValueError("invalid retired policy restore")
    return True


__all__ = [
    "CREDENTIAL_POLICY_DOWNGRADE_REASON",
    "CREDENTIAL_POLICY_RESTORE_REASON",
    "PLUGIN_POLICY_DOWNGRADE_REASON",
    "PLUGIN_POLICY_RESTORE_REASON",
    "validate_credential_policy_history_event",
]
