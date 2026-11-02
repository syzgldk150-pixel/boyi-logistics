"""Fail-closed binding between a completed scan preview Invocation and one formal call.

The preview owns the exact bill list.  The formal Invocation carries only
compact, hash-bound evidence and consumes the preview in its acceptance
transaction.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.orchestration.models import OrchestrationError
from shared.automation_project_authorization import canonical_sha256


SCAN_PREVIEW_CONTEXT_KEY = "scan_preview"
SCAN_PREVIEW_CONTRACT_VERSION = 1
SCAN_PREVIEW_CONTEXT_VERSION = 2
# A confirmed scan must use a preview observed within this window.
SCAN_PREVIEW_TTL = timedelta(minutes=15)
_MAX_SOURCE_PAGES = 500
_MAX_SOURCE_EVIDENCE_REFS = 500
_MAX_ITEMS = 100_000
_MAX_BATCHES = 499
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def is_scan_preview_project(entry: Any) -> bool:
    """Match only an uploaded Service V2 scan plugin."""

    from agent.automation_plugins.models import PluginTrustSource

    return (
        getattr(entry, "runtime_model", None) == "SERVICE_V2"
        and getattr(entry, "plugin_id", None) == "sync_scan_codes_v2"
        and getattr(entry, "trust_source", None) in {
            PluginTrustSource.SUPER_ADMIN_UPLOAD.value,
            PluginTrustSource.BUILTIN_BUNDLE.value,
        }
    )


def require_scan_formal_governance(entry: Any) -> None:
    """A formal scan needs the plugin's declared preview/confirm entrypoint."""

    if is_scan_preview_project(entry):
        # The V2 catalog validates the signed service effects and generation.
        # Invocation policy then enforces this instance's live permissions.
        from agent.orchestration.selection_preview_binding import selection_preview_contribution
        if any(selection_preview_contribution(entry, kind) is not None for kind in ("console", "feishu")):
            return
    raise _error("SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED", "扫描插件未声明预览确认入口")


def validate_scan_preview_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context must be an object")
    context = dict(value)
    expected_fields = {
        "contract_version",
        "plugin_id",
        "preview_invocation_id",
        "preview_result_sha256",
        "project_instance_id",
        "generation",
        "contract_digest",
        "configuration_version",
        "target_date",
        "observed_at",
        "expires_at",
        "source_page_count",
        "normalized_record_count",
        "source_snapshot_sha256",
        "source_evidence_count",
        "source_evidence_refs_sha256",
        "selection_count",
        "selection_sha256",
        "batch_count",
        "batch_plan_sha256",
        "formal_arguments_sha256",
        "context_sha256",
    }
    if set(context) != expected_fields:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context schema is invalid")
    supplied_digest = _digest(context.get("context_sha256"), "context_sha256")
    unhashed = dict(context)
    unhashed.pop("context_sha256")
    if canonical_sha256(unhashed) != supplied_digest:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context digest is stale")
    if context.get("contract_version") != SCAN_PREVIEW_CONTEXT_VERSION:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview context version is unsupported")
    if context.get("plugin_id") != "sync_scan_codes_v2":
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview plugin identity is invalid")
    normalize_preview_invocation_id(context.get("preview_invocation_id"))
    for name in (
        "preview_result_sha256",
        "contract_digest",
        "source_snapshot_sha256",
        "source_evidence_refs_sha256",
        "selection_sha256",
        "batch_plan_sha256",
        "formal_arguments_sha256",
    ):
        _digest(context.get(name), name)
    _required_text(context.get("project_instance_id"), "project_instance_id", maximum=128)
    _positive_int(context.get("generation"), "generation")
    _positive_int(context.get("configuration_version"), "configuration_version")
    _date_text(context.get("target_date"))
    observed = _parse_timestamp(context.get("observed_at"), "observed_at")
    expires = _parse_timestamp(context.get("expires_at"), "expires_at")
    if expires != observed + SCAN_PREVIEW_TTL:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview expiry is invalid")
    _bounded_int(context.get("source_page_count"), "source_page_count", 1, _MAX_SOURCE_PAGES)
    normalized = _bounded_int(context.get("normalized_record_count"), "normalized_record_count", 0, _MAX_ITEMS)
    evidence_count = _bounded_int(
        context.get("source_evidence_count"), "source_evidence_count", 1, _MAX_SOURCE_EVIDENCE_REFS
    )
    selection = _bounded_int(context.get("selection_count"), "selection_count", 0, _MAX_ITEMS)
    batches = _bounded_int(context.get("batch_count"), "batch_count", 0, _MAX_BATCHES)
    if selection > normalized or (selection == 0) != (batches == 0) or evidence_count < 1:
        raise _error("SCAN_PREVIEW_CONTEXT_INVALID", "Scan preview counts are inconsistent")
    return context


def normalize_preview_invocation_id(value: Any) -> str:
    """Validate a real call record identity; never resolves an old Run."""
    try:
        normalized = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise _error("SCAN_PREVIEW_ID_INVALID", "候选调用标识无效") from exc
    if normalized != value:
        raise _error("SCAN_PREVIEW_ID_INVALID", "候选调用标识无效")
    return normalized


def _validate_preview_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    expected_fields = {
        "contract_version",
        "target_date",
        "observed_at",
        "pagination_complete",
        "source_page_count",
        "normalized_record_count",
        "source_snapshot_sha256",
        "source_evidence_refs",
        "selection_count",
        "selection_sha256",
        "batch_count",
        "batch_plan_sha256",
        "items",
    }
    value = dict(evidence)
    if set(value) != expected_fields or value.get("contract_version") != SCAN_PREVIEW_CONTRACT_VERSION:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview evidence schema is invalid")
    if value.get("pagination_complete") is not True:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview source pagination is incomplete")
    value["target_date"] = _date_text(value.get("target_date"))
    _parse_timestamp(value.get("observed_at"), "observed_at")
    value["source_page_count"] = _bounded_int(
        value.get("source_page_count"), "source_page_count", 1, _MAX_SOURCE_PAGES
    )
    value["normalized_record_count"] = _bounded_int(
        value.get("normalized_record_count"), "normalized_record_count", 0, _MAX_ITEMS
    )
    value["source_snapshot_sha256"] = _digest(
        value.get("source_snapshot_sha256"), "source_snapshot_sha256"
    )
    refs = value.get("source_evidence_refs")
    if (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= _MAX_SOURCE_EVIDENCE_REFS
        or len(refs) != len(set(refs))
    ):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview source evidence is invalid")
    value["source_evidence_refs"] = [
        _required_text(ref, "source_evidence_ref", maximum=2048) for ref in refs
    ]
    value["selection_count"] = _bounded_int(
        value.get("selection_count"), "selection_count", 0, _MAX_ITEMS
    )
    value["selection_sha256"] = _digest(value.get("selection_sha256"), "selection_sha256")
    value["batch_count"] = _bounded_int(
        value.get("batch_count"), "batch_count", 0, _MAX_BATCHES
    )
    value["batch_plan_sha256"] = _digest(
        value.get("batch_plan_sha256"), "batch_plan_sha256"
    )
    items = value.get("items")
    if not isinstance(items, list) or len(items) != value["selection_count"]:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection count is invalid")
    normalized_items: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"bill_code", "station_name"}:
            raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection schema is invalid")
        normalized_items.append(
            {
                "bill_code": _required_text(item.get("bill_code"), "bill_code", maximum=128),
                "station_name": _required_text(item.get("station_name"), "station_name", maximum=256),
            }
        )
    if len({item["bill_code"] for item in normalized_items}) != len(normalized_items):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview contains duplicate bill identities")
    if normalized_items != sorted(normalized_items, key=lambda item: (item["station_name"], item["bill_code"])):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection order is unstable")
    if canonical_sha256(normalized_items) != value["selection_sha256"]:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection digest is invalid")
    if value["selection_count"] > value["normalized_record_count"]:
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview selection exceeds its source snapshot")
    # The installed plugin owns the batch plan, including its defaults. Bind
    # its count/digest unchanged; the plugin revalidates that exact plan before
    # the first write, and Host receipts/readback still verify actual writes.
    # Repartitioning here would couple every plugin batching change to Host.
    if (bool(value["batch_count"]) != bool(value["selection_count"])
            or value["batch_count"] > value["selection_count"]):
        raise _error("SCAN_PREVIEW_INVALID", "The scan preview batch count is invalid")
    if not normalized_items and value["batch_plan_sha256"] != canonical_sha256([]):
        raise _error("SCAN_PREVIEW_INVALID", "The empty scan preview batch plan digest is invalid")
    value["items"] = normalized_items
    return value


def _bind_formal_arguments(
    preview_arguments: Mapping[str, Any],
    formal_arguments: Mapping[str, Any],
    *,
    target_date: str,
) -> dict[str, Any]:
    preview = dict(preview_arguments)
    formal = dict(formal_arguments)
    preview.pop("dry_run", None)
    formal.pop("dry_run", None)
    preview_target_date = str(preview.get("target_date") or target_date)
    formal_target_date = str(formal.get("target_date") or target_date)
    if (
        preview_target_date != target_date
        or formal_target_date != target_date
        or canonical_sha256(preview) != canonical_sha256(formal)
    ):
        raise _error("SCAN_PREVIEW_STALE", "The formal scan arguments differ from the preview")
    formal["dry_run"] = False
    return formal


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any, name: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid") from exc
    return _aware_utc(parsed, name)


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise _error("SCAN_PREVIEW_INVALID", "target_date must use YYYY-MM-DD") from exc
    if parsed != text:
        raise _error("SCAN_PREVIEW_INVALID", "target_date must be canonical")
    return text


def _required_text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid")
    return value.strip()


def _digest(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not _HEX_SHA256.fullmatch(text):
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid")
    return text


def _positive_int(value: Any, name: str) -> int:
    return _bounded_int(value, name, 1, 2_147_483_647)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _error("SCAN_PREVIEW_INVALID", f"{name} is invalid")
    return value


def _error(code: str, message: str) -> OrchestrationError:
    return OrchestrationError(code, message, details={"status": "BLOCKED_DATA"})
