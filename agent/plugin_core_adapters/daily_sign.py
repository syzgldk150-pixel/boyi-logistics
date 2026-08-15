"""Exact managed-resource adapter for the authoritative daily-sign workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.automation_plugins.errors import PluginExecutionError


_BITABLE_ROLE = "daily_sign_bitable"
_SHEET_ROLE = "daily_sign_sheet"
_REQUIRED_PROOF_FIELDS = (
    "persistence_sha256",
    "bitable_snapshot_sha256",
    "sheet_snapshot_sha256",
)


def _exact_resource(
    resource_id: str,
    *,
    kind: str,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    from agent.workflow_resource_store import get_workflow_resource

    try:
        raw = get_workflow_resource(resource_id)
    except Exception as exc:
        raise PluginExecutionError(
            "the exact daily-sign resource is unavailable",
            code="BROKER_RESOURCE_UNAVAILABLE",
        ) from exc
    if not isinstance(raw, Mapping):
        raise PluginExecutionError(
            "the exact daily-sign resource no longer exists",
            code="BROKER_RESOURCE_UNAVAILABLE",
        )
    resource = dict(raw)
    metadata = resource.get("_meta")
    if (
        resource.get("resource_kind") != kind
        or not isinstance(metadata, Mapping)
        or str(metadata.get("resource_key") or "").strip() != resource_id
    ):
        raise PluginExecutionError(
            "the exact daily-sign resource changed kind or identity",
            code="BROKER_RESOURCE_MISMATCH",
        )
    if any(not str(resource.get(field) or "").strip() for field in fields):
        raise PluginExecutionError(
            "the exact daily-sign resource configuration is incomplete",
            code="BROKER_RESOURCE_INVALID",
        )
    return resource


def _verified_success(result: Mapping[str, Any]) -> None:
    if result.get("status") != "SUCCESS":
        return
    meta = result.get("meta")
    if not isinstance(meta, Mapping):
        raise PluginExecutionError(
            "daily-sign success has no terminal readback proof",
            code="WRITE_OUTCOME_UNKNOWN",
        )
    evidence = meta.get("postcondition_evidence")
    proof = evidence.get("0") if isinstance(evidence, Mapping) else None
    details = proof.get("details") if isinstance(proof, Mapping) else None
    if (
        not isinstance(proof, Mapping)
        or proof.get("verified") is not True
        or proof.get("condition")
        != "authoritative_snapshot_and_projections_verified"
        or not isinstance(details, Mapping)
        or any(
            not isinstance(details.get(field), str)
            or len(str(details[field])) != 64
            for field in _REQUIRED_PROOF_FIELDS
        )
    ):
        raise PluginExecutionError(
            "daily-sign success has no terminal readback proof",
            code="WRITE_OUTCOME_UNKNOWN",
        )


def run_daily_sign_with_bound_resources(
    arguments: Mapping[str, Any],
    resource_bindings: Mapping[str, str],
) -> Mapping[str, Any]:
    """Inject exact resources only inside core, then require closed evidence."""

    if set(resource_bindings) != {_BITABLE_ROLE, _SHEET_ROLE}:
        raise PluginExecutionError(
            "daily-sign resource bindings are incomplete",
            code="BROKER_RESOURCE_UNAVAILABLE",
        )
    bitable_id = str(resource_bindings[_BITABLE_ROLE] or "").strip()
    sheet_id = str(resource_bindings[_SHEET_ROLE] or "").strip()
    if not bitable_id or not sheet_id:
        raise PluginExecutionError(
            "daily-sign resource bindings are incomplete",
            code="BROKER_RESOURCE_UNAVAILABLE",
        )
    bitable = _exact_resource(
        bitable_id,
        kind="feishu_bitable",
        fields=("base_token", "table_id"),
    )
    sheet = _exact_resource(
        sheet_id,
        kind="feishu_sheet",
        fields=("spreadsheet_token", "range"),
    )
    from tools.daily_sign_sync_tool import run_daily_sign_sync

    result = run_daily_sign_sync(
        {
            **dict(arguments),
            "base_token": str(bitable["base_token"]),
            "table_id": str(bitable["table_id"]),
            "spreadsheet_token": str(sheet["spreadsheet_token"]),
            "range": str(sheet["range"]),
        }
    )
    if not isinstance(result, Mapping):
        raise PluginExecutionError(
            "daily-sign authoritative workflow returned an invalid result",
            code="BROKER_SOURCE_INVALID",
        )
    _verified_success(result)
    return result


__all__ = ["run_daily_sign_with_bound_resources"]
