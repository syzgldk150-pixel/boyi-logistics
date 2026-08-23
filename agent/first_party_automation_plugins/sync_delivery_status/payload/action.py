"""Package-owned delivery-status scan, classification and commit ordering."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_delivery_status"
_ACCOUNT_ROLE = "account_id"
_BITABLE_ROLE = "delivery_status_bitable"
_PENDING_STATUS = "未签收"
_SIGNED_STATUSES = frozenset({"签收", "已签收"})
_SIGNED_WRITE_STATUS = "已签收"
_PENDING_VIEW_NAME = "未签收明细"
_MAX_PAGES = 50
_MAX_PAGE_SIZE = 200
_MAX_BATCH_SIZE = 200
_MAX_RECORDS = _MAX_PAGES * _MAX_PAGE_SIZE
_ALLOWED_ARGUMENTS = frozenset(
    {
        "bill_codes",
        "record_ids",
        "BILL_CODE",
        "RECORD_ID",
        "list_limit",
        "query_batch_size",
        "dry_run",
    }
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("delivery-status text value cannot be boolean")
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, (list, tuple)):
        return "".join(_text(item) for item in value).strip()
    if isinstance(value, Mapping):
        row = dict(value)
        for key in ("text", "value", "name", "link"):
            if key in row:
                text = _text(row.get(key))
                if text:
                    return text
        return "".join(_text(item) for item in row.values()).strip()
    raise ValueError("delivery-status text value is invalid")


def _waybill(value: object) -> str:
    text = _text(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    result = "".join(text.split())
    if len(result) > 128:
        raise ValueError("delivery-status waybill identity is too long")
    return result


def _status(value: object) -> str:
    result = "".join(_text(value).split())
    if len(result) > 128:
        raise ValueError("delivery status is too long")
    return result


def _positive_limit(value: object, *, default: int, maximum: int, label: str) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if str(result) != str(value).strip() or result <= 0 or result > maximum:
        raise ValueError(f"{label} is outside its signed limit")
    return result


def _split_values(value: object, *, label: str, waybills: bool) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    elif isinstance(value, str):
        raw_items = value.replace(";", ",").split(",")
    else:
        raise ValueError(f"{label} must be a list or comma-separated string")
    items = [(_waybill(item) if waybills else _text(item)) for item in raw_items]
    if any(not item for item in items):
        raise ValueError(f"{label} contains an empty identity")
    if len(items) > _MAX_RECORDS:
        raise ValueError(f"{label} exceeds its signed limit")
    return items


def _explicit_selection(arguments: Mapping[str, object]) -> tuple[list[str], list[str]]:
    lower_codes = _split_values(
        arguments.get("bill_codes"),
        label="bill_codes",
        waybills=True,
    )
    upper_codes = _split_values(
        arguments.get("BILL_CODE"),
        label="BILL_CODE",
        waybills=True,
    )
    lower_records = _split_values(
        arguments.get("record_ids"),
        label="record_ids",
        waybills=False,
    )
    upper_records = _split_values(
        arguments.get("RECORD_ID"),
        label="RECORD_ID",
        waybills=False,
    )
    if lower_codes and upper_codes and lower_codes != upper_codes:
        raise ValueError("delivery-status waybill inputs conflict")
    if lower_records and upper_records and lower_records != upper_records:
        raise ValueError("delivery-status record inputs conflict")
    bill_codes = lower_codes or upper_codes
    record_ids = lower_records or upper_records
    if bool(bill_codes) != bool(record_ids):
        raise ValueError("delivery-status explicit mode requires waybills and record IDs")
    if bill_codes and len(bill_codes) != len(record_ids):
        raise ValueError("delivery-status waybill and record counts differ")
    if len(record_ids) != len(set(record_ids)):
        raise ValueError("delivery-status record IDs must be unique")
    return bill_codes, record_ids


def _pending_view(broker: Callable[..., object]) -> tuple[str, list[str]]:
    result = _object(
        broker(
            "network.request",
            action="feishu.bitable.list_views",
            role=_BITABLE_ROLE,
            arguments={},
        ),
        "delivery-status view list",
    )
    items = result.get("items")
    if not isinstance(items, list):
        raise ValueError("delivery-status view list is invalid")
    matches: list[str] = []
    for raw in items:
        row = _object(raw, "delivery-status view")
        if _text(row.get("view_name")) != _PENDING_VIEW_NAME:
            continue
        view_id = _text(row.get("view_id"))
        if not view_id:
            raise ValueError("delivery-status pending view has no identity")
        matches.append(view_id)
    if len(matches) != 1:
        raise ValueError("delivery-status pending view is missing or ambiguous")
    return matches[0], [broker_evidence_ref(result, "delivery-status view list")]


def _record_fields(row: Mapping[str, object]) -> tuple[str, str]:
    fields = row.get("fields")
    if isinstance(fields, Mapping):
        values = dict(fields)
        return _waybill(values.get("运单编号")), _status(values.get("签收状态"))
    return _waybill(row.get("waybill_no")), _status(row.get("status"))


def _scan_pending_records(
    *,
    page_size: int,
    broker: Callable[..., object],
) -> tuple[list[dict[str, str]], int, int, list[str]]:
    view_id, evidence_refs = _pending_view(broker)
    cursor: str | None = None
    seen_cursors: set[str] = set()
    records: dict[str, dict[str, str]] = {}
    skipped_empty = 0
    scanned = 0
    pages = 0
    while True:
        page = _object(
            broker(
                "network.request",
                action="feishu.bitable.list_records",
                role=_BITABLE_ROLE,
                arguments={
                    "view_id": view_id,
                    "cursor": cursor,
                    "page_size": page_size,
                },
            ),
            "delivery-status record page",
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("delivery-status record page is invalid")
        evidence_refs.append(broker_evidence_ref(page, "delivery-status record page"))
        for raw in items:
            row = _object(raw, "delivery-status record")
            record_id = _text(row.get("record_id"))
            if not record_id:
                raise ValueError("delivery-status record has no identity")
            waybill_no, status = _record_fields(row)
            scanned += 1
            if not waybill_no:
                skipped_empty += 1
                continue
            candidate = {
                "record_id": record_id,
                "bill_code": waybill_no,
                "status": status,
            }
            previous = records.get(record_id)
            if previous is not None and previous != candidate:
                raise ValueError("delivery-status duplicate record is inconsistent")
            records[record_id] = candidate
        if scanned > _MAX_RECORDS:
            raise ValueError("delivery-status scan exceeds its signed limit")
        pages += 1
        if pages > _MAX_PAGES:
            raise ValueError("delivery-status pagination exceeds its signed limit")
        if page.get("pagination_complete") is True:
            if page.get("next_cursor") not in (None, ""):
                raise ValueError("complete delivery-status page returned a cursor")
            pending = [
                record
                for record in records.values()
                if record["status"] == _PENDING_STATUS
            ]
            return pending, scanned, skipped_empty, evidence_refs
        cursor = _text(page.get("next_cursor"))
        if not cursor or len(cursor) > 2048 or cursor in seen_cursors:
            raise ValueError("delivery-status pagination cursor is invalid")
        seen_cursors.add(cursor)


def _query_statuses(
    bill_codes: list[str],
    *,
    batch_size: int,
    broker: Callable[..., object],
) -> tuple[dict[str, str], list[str]]:
    status_by_code: dict[str, str] = {}
    evidence_refs: list[str] = []
    for start in range(0, len(bill_codes), batch_size):
        requested = bill_codes[start : start + batch_size]
        result = _object(
            broker(
                "browser.invoke",
                action="ronghui.delivery_status.read",
                role=_ACCOUNT_ROLE,
                arguments={"bill_codes": requested},
            ),
            "delivery-status source result",
        )
        items = result.get("items")
        if not isinstance(items, list):
            raise ValueError("delivery-status source items are invalid")
        evidence_refs.append(broker_evidence_ref(result, "delivery-status source result"))
        requested_set = set(requested)
        for raw in items:
            row = _object(raw, "delivery-status source row")
            code = _waybill(row.get("bill_code"))
            status = _status(row.get("status"))
            if not code or not status or code not in requested_set:
                raise ValueError("delivery-status source returned an invalid identity")
            previous = status_by_code.get(code)
            if previous is not None and previous != status:
                raise ValueError("delivery-status source returned conflicting statuses")
            status_by_code[code] = status
    return status_by_code, evidence_refs


def _committed(value: object, label: str) -> dict[str, object]:
    result = _object(value, label)
    if result.get("committed") is not True:
        raise ValueError(f"{label} was not committed")
    return result


def run_action(
    arguments: dict[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "arguments")
    unknown = set(values) - _ALLOWED_ARGUMENTS
    if unknown:
        raise ValueError("delivery-status arguments contain undeclared fields")
    dry_run = values.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    page_size = _positive_limit(
        values.get("list_limit"),
        default=_MAX_PAGE_SIZE,
        maximum=_MAX_PAGE_SIZE,
        label="list_limit",
    )
    batch_size = _positive_limit(
        values.get("query_batch_size"),
        default=100,
        maximum=_MAX_BATCH_SIZE,
        label="query_batch_size",
    )
    explicit_codes, explicit_record_ids = _explicit_selection(values)
    evidence_refs: list[str] = []
    if explicit_codes:
        pending_records = [
            {"bill_code": code, "record_id": record_id, "status": _PENDING_STATUS}
            for code, record_id in zip(explicit_codes, explicit_record_ids, strict=True)
        ]
        scanned = len(pending_records)
        skipped_empty = 0
        mode = "explicit"
    else:
        pending_records, scanned, skipped_empty, source_refs = _scan_pending_records(
            page_size=page_size,
            broker=broker,
        )
        evidence_refs.extend(source_refs)
        mode = "pending_view"

    unique_codes = list(dict.fromkeys(record["bill_code"] for record in pending_records))
    status_by_code, query_refs = _query_statuses(
        unique_codes,
        batch_size=batch_size,
        broker=broker,
    )
    evidence_refs.extend(query_refs)
    unmatched = [code for code in unique_codes if code not in status_by_code]
    write_records: list[dict[str, str]] = []
    signed_codes: list[str] = []
    unchanged = 0
    for record in pending_records:
        status = status_by_code.get(record["bill_code"])
        if status is None:
            continue
        if mode == "pending_view" and status not in _SIGNED_STATUSES:
            unchanged += 1
            continue
        write_records.append(
            {
                "record_id": record["record_id"],
                "status": _SIGNED_WRITE_STATUS if mode == "pending_view" else status,
            }
        )
        if status in _SIGNED_STATUSES:
            signed_codes.append(record["bill_code"])

    written = 0
    execution_result = "dry_run_complete"
    if not dry_run:
        if write_records:
            write_result = _committed(
                broker(
                    "network.request",
                    action="feishu.bitable.write_records",
                    role=_BITABLE_ROLE,
                    arguments={"records": write_records},
                ),
                "delivery-status Bitable write",
            )
            evidence_refs.append(
                broker_evidence_ref(write_result, "delivery-status Bitable write")
            )
            written = int(write_result.get("written") or 0)
            if written != len(write_records):
                raise ValueError("delivery-status Bitable write count changed")
        if signed_codes:
            projection = _committed(
                broker(
                    "projection.invoke",
                    action="waybill.delivery_status.update",
                    role=_ACCOUNT_ROLE,
                    arguments={
                        "bill_codes": list(dict.fromkeys(signed_codes)),
                        "status": "signed",
                    },
                ),
                "delivery-status projection update",
            )
            evidence_refs.append(
                broker_evidence_ref(projection, "delivery-status projection update")
            )
        execution_result = "no_data" if not pending_records else "writes_committed"

    observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    data = {
        "mode": mode,
        "dry_run": dry_run,
        "scanned": scanned,
        "pending": len(pending_records),
        "queried": len(unique_codes),
        "updated": len(write_records) if dry_run else written,
        "unchanged": unchanged,
        "unmatched": len(unmatched),
        "unmatched_bill_codes": unmatched,
        "skipped_empty_waybill": skipped_empty,
        "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "pagination_complete": True,
            "execution_result": execution_result,
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
        source_system=("ronghui" if dry_run else "ronghui+feishu+internal_projection"),
        record_count=len(pending_records),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
    )
