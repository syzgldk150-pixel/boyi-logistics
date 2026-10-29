"""Persisted preview binding for exact Console/Feishu problem selections."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent.orchestration.models import (
    OrchestrationError,
)


_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
@dataclass(frozen=True)
class SelectionPreviewExpectation:
    project_instance_id: str
    plugin_id: str
    generation: int
    contract_digest: str
    configuration_version: int
    runtime_model: str = "SERVICE_V2"
    entrypoint: str | None = None
    contribution_id: str | None = None
    title: str | None = None


def is_selection_preview_project(entry: Any) -> bool:
    if getattr(entry, "runtime_model", None) == "SERVICE_V2" and getattr(entry, "plugin_id", None) == "sync_scan_codes_v2":
        return False  # Scans confirm an exact batch plan, not a selectable row list.
    return any(
        selection_preview_contribution(entry, contribution_kind) is not None
        for contribution_kind in ("console", "feishu")
    )


def selection_preview_contribution(
    entry: Any,
    contribution_kind: str,
) -> Mapping[str, Any] | None:
    """Return one signed Service-v2 selection contribution without guessing."""

    if str(getattr(entry, "runtime_model", "") or "") != "SERVICE_V2":
        return None
    contributions = getattr(entry, "contributions", None)
    declarations = (
        contributions.get(contribution_kind)
        if isinstance(contributions, Mapping)
        else None
    )
    matches = [
        dict(item)
        for item in declarations or ()
        if isinstance(item, Mapping)
        and isinstance(item.get("selection_preview_operation"), str)
        and str(item.get("selection_preview_operation") or "").strip()
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_generic_candidates(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or not 1 <= len(row) <= 32
            or "bill_code" not in row
            or any(
                not isinstance(key, str)
                or not key
                or len(key) > 64
                or any(
                    token in key.lower()
                    for token in ("password", "cookie", "credential", "secret", "token")
                )
                for key in row
            )
            or any(
                isinstance(value, (Mapping, list, tuple, set, bytes))
                or (isinstance(value, str) and len(value) > 1000)
                for value in row.values()
            )
        ):
            raise _error("SELECTION_PREVIEW_INVALID", "候选运单字段与签名合同不一致。")
        bill_code = str(row.get("bill_code") or "").strip()
        if not bill_code or len(bill_code) > 64 or bill_code in seen:
            raise _error("SELECTION_PREVIEW_INVALID", "候选运单号缺失或重复。")
        seen.add(bill_code)
        result.append(dict(row))
    return result


def _selected_bill_codes(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)) or not values:
        raise _error("SELECTION_REQUIRED", "请至少选择一票运单。")
    selected = [str(item or "").strip() for item in values]
    if any(not item or len(item) > 64 for item in selected) or len(selected) != len(
        set(selected)
    ):
        raise _error("SELECTION_INVALID", "所选运单号为空、重复或格式无效。")
    return selected


def _service_v2_summary(data: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "duplicate_source_rows",
        "complete_count",
        "hidden_completed_count",
    )
    result: dict[str, Any] = {}
    for field in allowed:
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        result[field] = value
    counts = data.get("type_counts")
    if isinstance(counts, Mapping) and all(
        isinstance(key, str)
        and key
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        for key, value in counts.items()
    ):
        result["type_counts"] = dict(counts)
    return result


def _parse_timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("SELECTION_PREVIEW_INVALID", f"{field} 不是有效时间。") from exc
    return _aware_utc(parsed, field)


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _error("SELECTION_PREVIEW_INVALID", f"{field} 缺少时区。")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _error(code: str, message: str) -> OrchestrationError:
    return OrchestrationError(code, message, details={"status": "BLOCKED_DATA"})


__all__ = [
    "is_selection_preview_project",
    "selection_preview_contribution",
]
