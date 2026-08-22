"""Thin package boundary for the existing authoritative daily-sign workflow."""

from __future__ import annotations

from collections.abc import Mapping

from boyi_plugin_result import validate_result


ACTION_ID = "sync_daily_should_sign"
_ROLE = "account_id"
_SENSITIVE_KEY_MARKERS = (
    "password",
    "cookie",
    "credential",
    "secret",
    "token",
    "session",
    "authorization",
)


def _contains_broker_owned_material(value: object) -> bool:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            normalized = str(raw_key).strip().lower().replace("-", "_")
            if normalized in {"account_id", "account_ids", "account_label"} or normalized.endswith(
                ("_account_id", "_account_ids")
            ):
                return True
            if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
                return True
            if _contains_broker_owned_material(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_broker_owned_material(item) for item in value)
    return False


def run_action(arguments, broker):
    if not isinstance(arguments, Mapping):
        raise TypeError("daily-sign arguments must be an object")
    broker_arguments = dict(arguments)
    if _contains_broker_owned_material(broker_arguments):
        raise ValueError("daily-sign account and credential fields are broker-owned")

    response = broker(
        "ledger.invoke",
        action="daily_sign.authoritative_sync",
        role=_ROLE,
        arguments=broker_arguments,
    )
    if not isinstance(response, Mapping) or set(response) != {"result", "evidence_ref"}:
        raise ValueError("daily-sign authoritative primitive response is invalid")
    evidence_ref = str(response.get("evidence_ref") or "").strip()
    encoded = response.get("result")
    if not evidence_ref or len(evidence_ref) > 512 or not isinstance(encoded, Mapping):
        raise ValueError("daily-sign authoritative primitive evidence is invalid")
    if _contains_broker_owned_material(encoded):
        raise ValueError("daily-sign authoritative primitive exposed broker-owned material")

    result = dict(encoded)
    raw_meta = result.get("meta")
    if not isinstance(raw_meta, Mapping):
        raise ValueError("daily-sign authoritative result metadata is invalid")
    meta = dict(raw_meta)
    if meta.pop("account_scope", None) != "multi_account" or "account_id" in meta:
        raise ValueError("daily-sign authoritative account scope is invalid")
    meta["account_id"] = "multi_account"
    result["meta"] = meta
    return validate_result(result)
