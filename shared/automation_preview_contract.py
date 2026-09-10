"""Closed public scan preview contract shared by Console and Feishu."""
from __future__ import annotations
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

PREVIEW_CONTRACT_VERSION = 2
SCAN_PREVIEW_PUBLIC_FIELDS = frozenset(
    {
        "contract_version",
        "automation_id",
        "preview_invocation_id",
        "target_date",
        "observed_at",
        "expires_at",
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
        "can_confirm",
    }
)

def normalize_scan_preview_projection(
    raw: Any,
    *,
    expected_invocation_id: str,
    expected_automation_id: str = "scan_codes",
) -> dict[str, Any] | None:
    """Accept only the frozen public scan preview contract."""

    if not isinstance(raw, Mapping) or set(raw) != SCAN_PREVIEW_PUBLIC_FIELDS:
        return None
    preview_invocation_id = str(raw.get("preview_invocation_id") or "").strip()
    try:
        normalized_preview_invocation_id = str(uuid.UUID(preview_invocation_id))
    except (ValueError, AttributeError):
        return None
    if normalized_preview_invocation_id != preview_invocation_id or preview_invocation_id != expected_invocation_id:
        return None
    if (raw.get("contract_version") != PREVIEW_CONTRACT_VERSION or raw.get("automation_id") != expected_automation_id
            or not isinstance(raw.get("can_confirm"), bool)):
        return None
    target_date = str(raw.get("target_date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
        return None
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return None
    timestamps: dict[str, str] = {}
    for field in ("observed_at", "expires_at"):
        value = str(raw.get(field) or "").strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if not value or len(value) > 64 or parsed.tzinfo is None:
            return None
        timestamps[field] = value
    counts: dict[str, int] = {}
    for field in (
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
    ):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counts[field] = value
    return {
        "contract_version": PREVIEW_CONTRACT_VERSION,
        "automation_id": expected_automation_id,
        "preview_invocation_id": preview_invocation_id,
        "target_date": target_date,
        **timestamps,
        **counts,
        "can_confirm": raw["can_confirm"],
    }
