"""Pure validators for retired plugin policy and restoration evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


ORIGINAL = "ORIGINAL"
PREPARED_AWARE = "PREPARED_AWARE"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DURABLE_POLICY_MODES = frozenset(
    ("PROJECT_FULL_AUTO", "REQUIRE_EACH_RUN", "LEGACY_SCHEDULE_ONLY")
)
_POLICY_EVENT_CONTRACT_FIELDS = (
    "contract_hash",
    "contract_snapshot_json",
    "tool_contract_hash",
    "plugin_contract_hash",
)
_ORIGINAL_METADATA_FIELDS = frozenset(
    (
        "request_payload_sha256",
        "from_version",
        "to_version",
        "package_sha256",
        "target_generation",
        "previous_state",
    )
)
_PREPARED_METADATA_FIELDS = _ORIGINAL_METADATA_FIELDS | frozenset(
    ("prepared_configuration_request_id",)
)
_JOINED_POLICY_FIELDS = {
    "event_id": "policy_event_id",
    "from_mode": "from_mode",
    "to_mode": "to_mode",
    "contract_hash": "policy_contract_hash",
    "contract_snapshot_json": "policy_contract_snapshot_json",
    "tool_contract_hash": "policy_tool_contract_hash",
    "plugin_contract_hash": "policy_plugin_contract_hash",
    "project_configuration_version": "policy_configuration_version",
    "project_generation": "policy_project_generation",
    "actor_id": "policy_actor_id",
    "actor_role": "policy_actor_role",
    "actor_display_name": "policy_actor_display_name",
    "reason": "policy_reason",
    "comment": "policy_comment",
    "correlation_id": "policy_correlation_id",
}


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _empty_policy_event_contract(event: Mapping[str, Any]) -> bool:
    return all(event.get(field) is None for field in _POLICY_EVENT_CONTRACT_FIELDS)


def _joined_policy_event_matches(
    joined: Mapping[str, Any], event: Mapping[str, Any]
) -> bool:
    return all(
        event.get(field) == joined.get(alias)
        for field, alias in _JOINED_POLICY_FIELDS.items()
    )


def validate_plugin_version_evidence(
    event: Mapping[str, Any],
    configuration_evidence: Sequence[Mapping[str, Any]],
) -> tuple[str | None, bool, str]:
    """Validate exact retired plugin-writer evidence or raise ``ValueError``.

    The result is ``(prepared_request_id, legacy_downgrade, metadata_variant)``.
    Both metadata forms are closed: the original writer has exactly six keys;
    the prepared-aware writer has exactly those keys plus the prepared request.
    """

    matches = [
        row
        for row in configuration_evidence
        if row.get("request_id") == event.get("request_id")
    ]
    joined = matches[0] if len(matches) == 1 else {}
    metadata = joined.get("configuration_metadata_json")
    metadata_fields = frozenset(metadata) if isinstance(metadata, Mapping) else frozenset()
    if metadata_fields == _ORIGINAL_METADATA_FIELDS:
        metadata_variant = ORIGINAL
    elif metadata_fields == _PREPARED_METADATA_FIELDS:
        metadata_variant = PREPARED_AWARE
    else:
        raise ValueError("plugin metadata field set is invalid")

    prepared_request = metadata.get("prepared_configuration_request_id")
    event_generation = event.get("project_generation")
    target_generation = metadata.get("target_generation")
    legacy_downgrade = (
        event.get("from_mode") in _DURABLE_POLICY_MODES
        and event.get("from_mode") != event.get("to_mode")
        and event.get("to_mode") == "REQUIRE_EACH_RUN"
    )
    try:
        metadata_sha256 = _canonical_sha256(metadata)
    except (TypeError, ValueError):
        raise ValueError("plugin metadata canonical encoding is invalid") from None
    if (
        len(matches) != 1
        or (event.get("from_mode") != event.get("to_mode") and not legacy_downgrade)
        or event.get("to_mode") not in _DURABLE_POLICY_MODES
        or not _empty_policy_event_contract(event)
        or event.get("actor_role") != "super_admin"
        or type(event.get("actor_id")) is not str
        or not event.get("actor_id")
        or event.get("actor_display_name") is not None
        or event.get("comment") is not None
        or event.get("correlation_id") != event.get("request_id")
        or not _joined_policy_event_matches(joined, event)
        or type(joined.get("configuration_event_id")) is not int
        or joined.get("configuration_event_id", 0) <= 0
        or joined.get("configuration_event_type") != "PLUGIN_UPGRADE_STAGED"
        or joined.get("configuration_from_state")
        not in {"INSTALLED", "ENABLED", "DISABLED"}
        or joined.get("configuration_to_state") != "UPGRADING"
        or joined.get("configuration_actor_id") != event.get("actor_id")
        or joined.get("configuration_actor_role") != event.get("actor_role")
        or not _valid_sha256(metadata.get("request_payload_sha256"))
        or not _valid_sha256(metadata.get("package_sha256"))
        or type(metadata.get("from_version")) is not str
        or not metadata.get("from_version")
        or type(metadata.get("to_version")) is not str
        or not metadata.get("to_version")
        or metadata.get("from_version") == metadata.get("to_version")
        or type(event_generation) is not int
        or event_generation <= 0
        or type(target_generation) is not int
        or target_generation <= 0
        or target_generation != event_generation
        or metadata.get("previous_state") != joined.get("configuration_from_state")
        or (
            prepared_request is not None
            and (type(prepared_request) is not str or not prepared_request)
        )
        or metadata_sha256 != joined.get("configuration_metadata_sha256")
    ):
        raise ValueError("plugin policy evidence is invalid")
    return prepared_request, legacy_downgrade, metadata_variant


__all__ = [
    "ORIGINAL",
    "PREPARED_AWARE",
    "validate_plugin_version_evidence",
]
