"""Package-owned Yunda send-waybill collection, enrichment and commit order."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from yunda_send_fields import FIELD_NAMES as _FIELDS, delivery_method as _delivery_method, normalize_record as _normalize_record

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_yunda_send_waybills"
_ACCOUNT_ROLE = "account_id"
_BITABLE_ROLE = "send_waybills_bitable"
_SHEET_ROLE = "send_waybills_sheet"
_SOURCE_SEND = "send_waybill"
_SOURCE_SPECIAL = "special_line"
_MAX_RANGE_DAYS = 366
_MAX_DETAIL_RECORDS = 298
_MAX_BROKER_CALLS = 1_000
_ALLOWED_ARGUMENTS = frozenset(
    {
        "target_date",
        "start_date",
        "end_date",
        "sync_sheet",
        "ensure_fields",
        "page_size",
        "max_pages",
        "dry_run",
        "sql_only",
        "sync_sql",
    }
)

def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _business_date(value: object, label: str) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD") from exc


def _dates(arguments: Mapping[str, object]) -> list[str]:
    raw_start = str(arguments.get("start_date") or "").strip()
    raw_end = str(arguments.get("end_date") or "").strip()
    if not raw_start and not raw_end:
        raw_target = str(arguments.get("target_date") or "").strip()
        target = (
            _business_date(raw_target, "target_date")
            if raw_target
            else datetime.now(ZoneInfo("Asia/Shanghai")).date()
        )
        return [target.isoformat()]
    start = _business_date(raw_start or raw_end, "start_date")
    end = _business_date(raw_end or raw_start, "end_date")
    if start > end:
        raise ValueError("start_date cannot be later than end_date")
    days = (end - start).days + 1
    if days > _MAX_RANGE_DAYS:
        raise ValueError("Yunda send date range exceeds its signed limit")
    return [(start + timedelta(days=offset)).isoformat() for offset in range(days)]


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{label} is outside its signed limit")
    return result


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _first(*values: object) -> object:
    for value in values:
        if value not in (None, "") and _text(value):
            return value
    return ""



def _collect_source(
    *,
    action: str,
    target_date: str,
    page_size: int,
    max_pages: int,
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], list[str]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    rows: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    for _page in range(max_pages):
        result = _object(
            broker(
                "browser.invoke",
                action=action,
                role=_ACCOUNT_ROLE,
                arguments={
                    "target_date": target_date,
                    "cursor": cursor,
                    "page_size": page_size,
                },
            ),
            f"{action} result",
        )
        items = result.get("items")
        if not isinstance(items, list):
            raise ValueError(f"{action} items are invalid")
        rows.extend(_object(item, f"{action} row") for item in items)
        evidence_refs.append(broker_evidence_ref(result, action))
        if result.get("pagination_complete") is True:
            if result.get("next_cursor") not in (None, ""):
                raise ValueError(f"complete {action} page returned a cursor")
            return rows, evidence_refs
        cursor = str(result.get("next_cursor") or "").strip()
        if not cursor or len(cursor) > 2048 or cursor in seen_cursors:
            raise ValueError(f"{action} cursor is invalid")
        seen_cursors.add(cursor)
    raise ValueError(f"{action} pagination exceeded max_pages")


def _merge_sources(
    send_rows: list[dict[str, object]],
    special_rows: list[dict[str, object]],
) -> list[tuple[dict[str, object], str]]:
    merged: list[tuple[dict[str, object], str]] = []
    seen: dict[str, str] = {}
    for source, rows in ((_SOURCE_SEND, send_rows), (_SOURCE_SPECIAL, special_rows)):
        source_seen: set[str] = set()
        for row in rows:
            bill_code = _text(row.get("Logistics_Id"))
            if not bill_code:
                raise ValueError("Yunda send source row has no waybill identity")
            if bill_code in source_seen:
                raise ValueError("Yunda send source contains duplicate waybills")
            source_seen.add(bill_code)
            previous_source = seen.get(bill_code)
            if previous_source == _SOURCE_SEND and source == _SOURCE_SPECIAL:
                continue
            if previous_source is not None:
                raise ValueError("Yunda send source contains duplicate waybills")
            seen[bill_code] = source
            merged.append((row, source))
    if len(merged) > _MAX_DETAIL_RECORDS:
        raise ValueError("Yunda send detail workload exceeds its signed broker limit")
    return merged


def _detail(
    broker: Callable[..., object],
    *,
    action: str,
    bill_code: str,
    created_dot_code: str | None = None,
) -> tuple[dict[str, object], str]:
    arguments: dict[str, object] = {"bill_code": bill_code}
    if created_dot_code is not None:
        arguments["created_dot_code"] = created_dot_code
    result = _object(
        broker(
            "browser.invoke",
            action=action,
            role=_ACCOUNT_ROLE,
            arguments=arguments,
        ),
        action,
    )
    return (
        _object(result.get("record"), f"{action} record"),
        broker_evidence_ref(result, action),
    )



def _collect_date(
    *,
    target_date: str,
    page_size: int,
    max_pages: int,
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], dict[str, int], int, list[str]]:
    send_rows, evidence_refs = _collect_source(
        action="yunda.send_waybill.list_page",
        target_date=target_date,
        page_size=page_size,
        max_pages=max_pages,
        broker=broker,
    )
    special_rows, special_evidence = _collect_source(
        action="yunda.special_line.list_page",
        target_date=target_date,
        page_size=page_size,
        max_pages=max_pages,
        broker=broker,
    )
    evidence_refs.extend(special_evidence)
    merged = _merge_sources(send_rows, special_rows)
    records: list[dict[str, object]] = []
    for row, source in merged:
        bill_code = _text(row["Logistics_Id"])
        detail, detail_ref = _detail(
            broker,
            action="yunda.waybill.tracking_detail",
            bill_code=bill_code,
        )
        original, original_ref = _detail(
            broker,
            action="yunda.waybill.original_data",
            bill_code=bill_code,
        )
        evidence_refs.extend((detail_ref, original_ref))
        renderer: dict[str, object] = {}
        if source == _SOURCE_SEND:
            renderer, renderer_ref = _detail(
                broker,
                action="yunda.send_waybill.renderer_detail",
                bill_code=bill_code,
                created_dot_code=_text(
                    _first(row.get("Created_Dot_Code"), row.get("CreatedDotCode"))
                ),
            )
            evidence_refs.append(renderer_ref)
        records.append(
            _normalize_record(
                row,
                detail,
                original,
                renderer,
                target_date=target_date,
                source=source,
            )
        )
    return (
        records,
        {_SOURCE_SEND: len(send_rows), _SOURCE_SPECIAL: len(special_rows)},
        len(evidence_refs),
        evidence_refs,
    )


def _committed(value: object, label: str) -> dict[str, object]:
    result = _object(value, label)
    if result.get("committed") is not True:
        raise ValueError(f"{label} was not committed")
    return result


def _verified_resource(
    value: object,
    label: str,
    *,
    expected_records: int,
) -> dict[str, object]:
    result = _committed(value, label)
    readback_sha256 = str(result.get("readback_sha256") or "").strip()
    if (
        result.get("verified") is not True
        or result.get("readback_count") != expected_records
        or len(readback_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in readback_sha256
        )
    ):
        raise ValueError(f"{label} was not independently verified")
    return result


def run_action(
    arguments: dict[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "arguments")
    if set(values) - _ALLOWED_ARGUMENTS:
        raise ValueError("Yunda send arguments contain undeclared fields")
    flags: dict[str, bool] = {}
    for name, default in (
        ("sync_sheet", True),
        ("ensure_fields", True),
        ("dry_run", False),
        ("sql_only", False),
        ("sync_sql", True),
    ):
        flag = values.get(name, default)
        if not isinstance(flag, bool):
            raise ValueError(f"{name} is invalid")
        flags[name] = flag
    page_size = _bounded_int(
        values.get("page_size"),
        default=200,
        minimum=1,
        maximum=200,
        label="page_size",
    )
    max_pages = _bounded_int(
        values.get("max_pages"),
        default=50,
        minimum=1,
        maximum=50,
        label="max_pages",
    )
    dates = _dates(values)
    broker_call_count = 0

    def bounded_broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        nonlocal broker_call_count
        if broker_call_count >= _MAX_BROKER_CALLS:
            raise ValueError("Yunda send broker call budget exhausted")
        broker_call_count += 1
        return broker(
            operation,
            action=action,
            role=role,
            arguments=arguments,
        )

    all_evidence: list[str] = []
    per_date: list[dict[str, object]] = []
    totals = {
        "fetched": 0,
        "written": 0,
        "deleted": 0,
        "retained_unmatched": 0,
        "sql_upserted": 0,
        "sql_deleted_stale": 0,
        "sheet_rows": 0,
    }
    for target_date in dates:
        records, source_counts, page_evidence_count, evidence_refs = _collect_date(
            target_date=target_date,
            page_size=page_size,
            max_pages=max_pages,
            broker=bounded_broker,
        )
        all_evidence.extend(evidence_refs)
        summary: dict[str, object] = {
            "target_date": target_date,
            "fetched": len(records),
            "source_counts": source_counts,
            "source_evidence_count": page_evidence_count,
            "written": 0,
            "deleted": 0,
            "retained_unmatched": 0,
            "sql_upserted": 0,
            "sql_deleted_stale": 0,
            "sheet_rows": 0,
        }
        if not flags["dry_run"]:
            if not flags["sql_only"]:
                bitable = _verified_resource(
                    bounded_broker(
                        "network.request",
                        action="feishu.bitable.replace_yunda_send_waybills_date",
                        role=_BITABLE_ROLE,
                        arguments={
                            "records": records,
                            "target_date": target_date,
                            "ensure_fields": flags["ensure_fields"],
                        },
                    ),
                    "Yunda send Bitable replacement",
                    expected_records=len(records),
                )
                all_evidence.append(
                    broker_evidence_ref(bitable, "Yunda send Bitable replacement")
                )
                summary["written"] = int(bitable.get("written") or 0)
                summary["deleted"] = int(bitable.get("deleted") or 0)
                summary["retained_unmatched"] = int(bitable.get("retained_unmatched") or 0)
                if summary["written"] != len(records):
                    raise ValueError("Yunda send Bitable write count changed")
            if flags["sync_sql"]:
                projection = _committed(
                    bounded_broker(
                        "projection.invoke",
                        action="waybill.yunda.replace_date",
                        role=_ACCOUNT_ROLE,
                        arguments={
                            "records": records,
                            "target_date": target_date,
                            "ensure_fields": False,
                        },
                    ),
                    "Yunda waybill projection",
                )
                all_evidence.append(
                    broker_evidence_ref(projection, "Yunda waybill projection")
                )
                summary["sql_upserted"] = int(projection.get("upserted") or 0)
                summary["sql_deleted_stale"] = int(
                    projection.get("deleted_stale") or 0
                )
            if (
                not flags["sql_only"]
                and flags["sync_sheet"]
                and len(dates) == 1
            ):
                sheet = _verified_resource(
                    bounded_broker(
                        "network.request",
                        action="feishu.sheet.replace_yunda_send_waybills",
                        role=_SHEET_ROLE,
                        arguments={
                            "records": records,
                            "target_date": target_date,
                            "ensure_fields": False,
                        },
                    ),
                    "Yunda send sheet replacement",
                    expected_records=len(records),
                )
                all_evidence.append(
                    broker_evidence_ref(sheet, "Yunda send sheet replacement")
                )
                summary["sheet_rows"] = int(sheet.get("written") or 0)
                if summary["sheet_rows"] != len(records):
                    raise ValueError("Yunda send sheet write count changed")
        for key in totals:
            totals[key] += int(summary.get(key) or 0)
        per_date.append(summary)
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    if (
        not flags["dry_run"]
        and totals["fetched"] == 0
        and (not flags["sql_only"] or flags["sync_sql"])
    ):
        execution_result = "no_data_existing_rows_retained"
    elif flags["dry_run"]:
        execution_result = "dry_run_complete"
    else:
        execution_result = "requested_sinks_committed"
    data: dict[str, object] = {
        **totals,
        "total": totals["fetched"],
        "days": len(dates),
        "start_date": dates[0],
        "end_date": dates[-1],
        "dry_run": flags["dry_run"],
        "sql_only": flags["sql_only"],
        "per_date": per_date,
        "evidence": {
            "source": "service_v2_plugin",
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
    all_evidence = list(dict.fromkeys(all_evidence))
    all_evidence.append(result_ref)
    return success_result(
        data=data,
        source_system="yunda+feishu+internal_projection",
        record_count=totals["fetched"],
        pagination_complete=True,
        evidence_refs=all_evidence,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
    )
