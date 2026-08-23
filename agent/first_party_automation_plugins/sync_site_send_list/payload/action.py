"""Package-owned site-send pagination, filtering and projection order."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
    utc_observed_at,
)


ACTION_ID = "sync_site_send_list"
_ACCOUNT_ROLE = "account_id"
_BITABLE_ROLE = "site_send_bitable"
_SHEET_ROLE = "site_send_sheet"
_PAGE_SIZE = 100
_MAX_PAGES = 200
_MAX_ROWS = 20_000
_EXCLUDED_SEND_SITES = frozenset(
    {
        "邵阳新邵站",
        "邵阳大祥站",
        "邵阳鹏达营业部",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "tracking_number",
        "send_site",
        "package_type",
        "destination",
        "pieces",
        "weight",
    }
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _optional_number(value: object, label: str) -> int | float | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be a finite number")
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _target_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("target_date is required")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("target_date must use YYYY-MM-DD") from exc


def _collect_rows(
    broker: Callable[..., object],
    *,
    requested_date: str,
) -> tuple[list[dict[str, object]], int, str, list[str]]:
    cursor: str | None = None
    bound_date = requested_date
    seen_cursors: set[str] = set()
    rows: list[dict[str, object]] = []
    pages = 0
    evidence_refs: list[str] = []
    while True:
        page_arguments: dict[str, object] = {
            "cursor": cursor,
            "page_size": _PAGE_SIZE,
            "target_date": bound_date,
        }
        page = _object(
            broker(
                "browser.invoke",
                action="ronghui.site_send.read_page",
                role=_ACCOUNT_ROLE,
                arguments=page_arguments,
            ),
            "site-send page",
        )
        page_date = _target_date(page.get("target_date"))
        if page_date != bound_date:
            raise ValueError("site-send source changed its business date")
        bound_date = page_date
        items = page.get("items")
        evidence_refs.append(broker_evidence_ref(page, "site-send page"))
        if not isinstance(items, list):
            raise ValueError("site-send page items are invalid")
        for item in items:
            row = _object(item, "site-send row")
            if set(row) != _SOURCE_FIELDS:
                raise ValueError("site-send row schema is invalid")
            rows.append(row)
        pages += 1
        if pages > _MAX_PAGES or len(rows) > _MAX_ROWS:
            raise ValueError("site-send source exceeded its signed row limit")
        if page.get("pagination_complete") is True:
            if page.get("next_cursor") not in (None, ""):
                raise ValueError("complete site-send page returned a cursor")
            return rows, pages, bound_date, evidence_refs
        cursor = str(page.get("next_cursor") or "").strip()
        if not cursor or len(cursor) > 1024 or cursor in seen_cursors:
            raise ValueError("site-send pagination cursor is invalid")
        seen_cursors.add(cursor)


def _normalize(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: dict[str, dict[str, object]] = {}
    for row in rows:
        tracking = str(row.get("tracking_number") or "").strip()
        send_site = str(row.get("send_site") or "").strip()
        destination = str(row.get("destination") or "").strip()
        pieces = _optional_number(row.get("pieces"), "pieces")
        weight = _optional_number(row.get("weight"), "weight")
        if not tracking:
            continue
        if tracking.upper().startswith("H") or send_site in _EXCLUDED_SEND_SITES:
            continue
        if not any((send_site, destination, pieces is not None, weight is not None)):
            continue
        value = {
            "tracking_number": tracking,
            "send_site": send_site,
            "package_type": str(row.get("package_type") or "").strip(),
            "destination": destination,
            "pieces": pieces,
            "weight": weight,
        }
        previous = normalized.get(tracking)
        if previous is not None and previous != value:
            raise ValueError("site-send duplicate waybill is inconsistent")
        normalized.setdefault(tracking, value)
    return list(normalized.values())


def _committed_count(result: Mapping[str, object], expected: int, label: str) -> None:
    if result.get("committed") is not True:
        raise ValueError(f"{label} snapshot was not committed")
    observed = result.get("record_count")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed != expected:
        raise ValueError(f"{label} snapshot count did not match")


def run_action(arguments: dict[str, object], broker: Callable[..., object]) -> dict[str, object]:
    if set(arguments) != {"target_date"}:
        raise ValueError("site-send arguments are invalid")
    requested_date = _target_date(arguments.get("target_date"))
    rows, page_count, business_date, evidence_refs = _collect_rows(
        broker,
        requested_date=requested_date,
    )
    normalized = _normalize(rows)
    records = [
        {
            "fields": {
                "tracking_number": item["tracking_number"],
                "send_site": item["send_site"],
                "package_type": item["package_type"],
                "destination": item["destination"],
                "pieces": item["pieces"],
                "weight": item["weight"],
            }
        }
        for item in normalized
    ]
    sheet_values = [
        [
            item["tracking_number"],
            item["send_site"],
            item["package_type"],
            item["pieces"] if item["pieces"] is not None else "",
            item["weight"] if item["weight"] is not None else "",
            item["destination"],
        ]
        for item in normalized
    ]
    bitable = _object(
        broker(
            "network.request",
            action="feishu.bitable.replace_snapshot",
            role=_BITABLE_ROLE,
            arguments={"records": records, "target_date": business_date},
        ),
        "site-send bitable result",
    )
    _committed_count(bitable, len(normalized), "site-send bitable")
    evidence_refs.append(broker_evidence_ref(bitable, "site-send bitable result"))
    sheet = _object(
        broker(
            "network.request",
            action="feishu.sheet.replace",
            role=_SHEET_ROLE,
            arguments={"values": sheet_values, "target_date": business_date},
        ),
        "site-send sheet result",
    )
    _committed_count(sheet, len(normalized), "site-send sheet")
    evidence_refs.append(broker_evidence_ref(sheet, "site-send sheet result"))
    observed_at = utc_observed_at()
    data = {
        "target_date": business_date,
        "fetched": len(rows),
        "normalized": len(normalized),
        "filtered": len(rows) - len(normalized),
        "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "pagination_complete": True,
            "page_count": page_count,
            "execution_result": (
                "no_data_cleared" if not normalized else "both_snapshots_committed"
            ),
        },
    }
    result_ref, result_proof = executor_success_evidence(
        action_id=ACTION_ID,
        data=data,
        observed_at=observed_at,
    )
    evidence_refs.append(result_ref)
    return success_result(
        data=data,
        source_system="ronghui+feishu",
        record_count=len(normalized),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
    )
