"""Fail-closed impact previews for governed third-party and financial writes.

The preview is deliberately tool-specific.  A broad selector (for example
"all tasks in a status" or "a date range") is not treated as an exact impact
just because its input arguments are deterministic.  Such tools stay blocked
until they expose an authoritative, read-only preview/fingerprint contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Callable

from agent.orchestration.models import OperationType, OrchestrationError, sha256_json


ImpactBuilder = Callable[[str | None, Mapping[str, Any]], dict[str, Any]]


_BLOCKED_WRITE_TOOLS = {
    "self_pickup_problem_upload": "candidate set comes from a live spreadsheet and has no governed source fingerprint",
    "r7_arrival_checkin": "target tasks are selected by live status and have no exact task-id preview",
    "r7_departure_checkin": "target tasks are selected by live status/class/time and have no exact task-id preview",
    "customer_service_problem_upload_attachment": "the local file has no approved content hash and external target preview",
    "split_pending_problem_upload": "target problem and complaint records have no governed read-after-write verifier",
}


def build_write_impact(
    *,
    tool_name: str,
    operation_type: OperationType,
    account_id: str | None,
    arguments: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return an exact preview for writes, ``None`` for non-governed operations.

    Every external/financial write must either have a registered exact builder
    or fail closed.  The returned fingerprint is included in ``Plan.impact``;
    the normalized arguments and impact are both covered by ``plan_hash``.
    """

    if operation_type not in {OperationType.EXTERNAL_WRITE, OperationType.FINANCIAL_WRITE}:
        return None
    if tool_name in _BLOCKED_WRITE_TOOLS:
        raise _preview_required(tool_name, _BLOCKED_WRITE_TOOLS[tool_name])
    builder = _EXACT_BUILDERS.get(tool_name)
    if builder is None:
        raise _preview_required(tool_name, "no tool-specific exact impact preview is registered")
    preview = builder(account_id, arguments)
    payload = {
        "tool_name": tool_name,
        "operation_type": operation_type.value,
        "account_id": account_id,
        "entities": preview["entities"],
        "amounts": preview.get("amounts", {}),
        "source_version": preview["source_version"],
        "revalidation": preview["revalidation"],
    }
    payload["preview_fingerprint"] = sha256_json(payload)
    return payload


def validate_write_impact(*, operation_type: OperationType, impact: Mapping[str, Any]) -> None:
    """Reject persisted write plans that do not carry a complete preview."""

    if operation_type not in {OperationType.EXTERNAL_WRITE, OperationType.FINANCIAL_WRITE}:
        return
    fingerprint = str(impact.get("preview_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise OrchestrationError(
            "IMPACT_PREVIEW_REQUIRED",
            "Third-party and financial writes require an exact impact preview",
            details={"status": "BLOCKED_DATA"},
        )
    unhashed = dict(impact)
    unhashed.pop("preview_fingerprint", None)
    if sha256_json(unhashed) != fingerprint:
        raise OrchestrationError(
            "IMPACT_PREVIEW_STALE",
            "The persisted impact preview fingerprint does not match its content",
            details={"status": "BLOCKED_DATA"},
        )
    entities = impact.get("entities")
    if not isinstance(entities, list) or not entities:
        raise OrchestrationError(
            "IMPACT_PREVIEW_REQUIRED",
            "Write impact preview has no exact entity identifiers",
            details={"status": "BLOCKED_DATA"},
        )


def _preview_required(tool_name: str, reason: str) -> OrchestrationError:
    return OrchestrationError(
        "IMPACT_PREVIEW_REQUIRED",
        f"Tool {tool_name} is disabled until an exact read-only impact preview is available: {reason}",
        details={"status": "BLOCKED_DATA", "tool_name": tool_name},
    )


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = str(arguments.get(name) or "").strip()
    if not value:
        raise _preview_required("current write", f"missing exact selector {name}")
    return value


def _entity(
    *,
    entity_type: str,
    entity_id: str,
    source_system: str,
    action: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_system": source_system,
        "relation_type": "impact",
        "metadata": {"action": action, **dict(metadata or {})},
    }


def _selector_source_version(arguments: Mapping[str, Any]) -> dict[str, str]:
    """Fingerprint an exact selector without pretending it is remote row versioning."""

    return {
        "kind": "deterministic_selector",
        "value": sha256_json(arguments),
    }


def _single_customer_problem(
    account_id: str | None,
    arguments: Mapping[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    platform = _required_text(arguments, "platform")
    external_id = _required_text(arguments, "external_id")
    selected_account = _required_text(arguments, "account_id")
    if account_id and account_id != selected_account:
        raise _preview_required("customer service problem write", "step and argument account IDs differ")
    selector = {
        "platform": platform,
        "account_id": selected_account,
        "external_id": external_id,
        "action": action,
    }
    return {
        "entities": [
            _entity(
                entity_type="customer_service_problem",
                entity_id=external_id,
                source_system=platform,
                action=action,
                metadata={"account_id": selected_account},
            )
        ],
        "source_version": _selector_source_version(selector),
        "revalidation": "deterministic_selector_and_tool_read_after_write",
    }


def _mark_read(account_id: str | None, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return _single_customer_problem(account_id, arguments, action="mark_read")


def _reply(account_id: str | None, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return _single_customer_problem(account_id, arguments, action="reply")


def _publish(account_id: str | None, arguments: Mapping[str, Any]) -> dict[str, Any]:
    platform = _required_text(arguments, "platform")
    selected_account = _required_text(arguments, "account_id")
    if account_id and account_id != selected_account:
        raise _preview_required("customer service problem publish", "step and argument account IDs differ")
    payload = arguments.get("payload")
    if not isinstance(payload, Mapping):
        raise _preview_required("customer service problem publish", "payload is missing")
    waybill = str(payload.get("bill_code") or payload.get("ship_no") or "").strip()
    if not waybill:
        raise _preview_required("customer service problem publish", "payload has no exact waybill identifier")
    selector = {
        "platform": platform,
        "account_id": selected_account,
        "waybill": waybill,
        "payload_sha256": sha256_json(payload),
    }
    return {
        "entities": [
            _entity(
                entity_type="waybill",
                entity_id=waybill,
                source_system=platform,
                action="publish_problem",
                metadata={
                    "account_id": selected_account,
                    "payload_sha256": selector["payload_sha256"],
                },
            )
        ],
        "source_version": _selector_source_version(selector),
        "revalidation": "deterministic_create_payload_and_tool_read_after_write",
    }


def _receipt_audit(_account_id: str | None, arguments: Mapping[str, Any]) -> dict[str, Any]:
    platform = _required_text(arguments, "platform")
    waybill = _required_text(arguments, "waybill_no")
    selector = {
        "platform": platform,
        "direction": _required_text(arguments, "direction"),
        "waybill_no": waybill,
        "receipt_id": str(arguments.get("receipt_id") or "").strip(),
        "receipt_no": str(arguments.get("receipt_no") or "").strip(),
        "return_waybill_no": str(arguments.get("return_waybill_no") or "").strip(),
        "result": _required_text(arguments, "result"),
    }
    entity_id = selector["receipt_id"] or selector["receipt_no"] or waybill
    return {
        "entities": [
            _entity(
                entity_type="receipt",
                entity_id=entity_id,
                source_system=platform,
                action="audit",
                metadata={
                    "waybill_no": waybill,
                    "direction": selector["direction"],
                    "result": selector["result"],
                },
            )
        ],
        "source_version": _selector_source_version(selector),
        "revalidation": "unique_receipt_selector_and_tool_read_after_write",
    }


def _clock_in(account_id: str | None, arguments: Mapping[str, Any]) -> dict[str, Any]:
    resolved_account = _required_text(
        {"account_id": account_id or arguments.get("account_id")},
        "account_id",
    )
    site_code = _required_text(arguments, "sitecode")
    site_fb_code = _required_text(arguments, "sitefbcode")
    first_action = _required_text(arguments, "first_type")
    second_action = _required_text(arguments, "second_type")
    selector = {
        "account_id": resolved_account,
        "sitecode": site_code,
        "sitefbcode": site_fb_code,
        "first_type": first_action,
        "second_type": second_action,
    }
    return {
        "entities": [
            _entity(
                entity_type="site_clock_action",
                entity_id=f"{site_code}:{first_action}",
                source_system="ronghui",
                action=first_action,
                metadata={"sitecode": site_code, "account_id": resolved_account},
            ),
            _entity(
                entity_type="site_clock_action",
                entity_id=f"{site_fb_code}:{second_action}",
                source_system="ronghui",
                action=second_action,
                metadata={"sitecode": site_fb_code, "account_id": resolved_account},
            ),
        ],
        "source_version": _selector_source_version(selector),
        "revalidation": "deterministic_site_actions_and_tool_read_after_write",
    }


_EXACT_BUILDERS: dict[str, ImpactBuilder] = {
    "receipts_audit": _receipt_audit,
    "clock_in_dual": _clock_in,
    "customer_service_problem_mark_read": _mark_read,
    "customer_service_problem_reply": _reply,
    "customer_service_problem_publish": _publish,
}
