"""Package-owned arrival-statistics collection and projection orchestration.

The core exposes bounded Ronghui pages and atomic projection/resource ports.
This replaceable package owns source union, de-duplication, detail refresh,
arrival counting, quantity caps, commit order and result evidence.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_arrival_stats"
_ROLE = "account_id"
_MAX_PAGES = 500
_MAX_RECORDS = 20_000
_MONEY = Decimal("0.01")
_VOLUME = Decimal("0.001")
_R_CHILD = re.compile(r"^(?:R\d{11}|RC\d{10})\d{4}$")
_NUMERIC_CHILD = re.compile(r"^200\d{11}$")
_WAYBILL_FIELDS = (
    "tracking_number",
    "goods_name",
    "package_type",
    "delivery_method",
    "quantity",
    "receipt_number",
    "actual_weight",
    "volume",
    "remarks",
    "destination_station",
    "recipient_name",
    "recipient_phone",
    "recipient_address",
    "settlement_weight",
    "volumetric_weight",
    "shipping_fee",
    "payment_type",
    "pay_on_arrival",
)
_ALIASES = {
    "tracking_number": ("tracking_number", "bill_code", "billCode", "BILL_CODE", "trackingNumber"),
    "goods_name": ("goods_name", "goodsName", "GOODS_NAME"),
    "package_type": ("package_type", "packageType", "PACK_TYPE"),
    "delivery_method": ("delivery_method", "deliveryMethod", "DISPATCH_MODE"),
    "quantity": ("quantity", "qty", "pcs", "PIECE_NUMBER"),
    "receipt_number": ("receipt_number", "receiptNumber", "R_BILLCODE"),
    "actual_weight": ("actual_weight", "actualWeight", "BILL_WEIGHT"),
    "volume": ("volume", "VOLUME"),
    "remarks": ("remarks", "remark", "REMARK"),
    "destination_station": ("destination_station", "destinationStation", "DESTINATION"),
    "recipient_name": ("recipient_name", "recipientName", "ACCEPT_MAN"),
    "recipient_phone": ("recipient_phone", "recipientPhone", "ACCEPT_MAN_PHONE"),
    "recipient_address": ("recipient_address", "recipientAddress", "ACCEPT_MAN_ADDRESS"),
    "settlement_weight": ("settlement_weight", "settlementWeight", "SETTLEMENT_WEIGHT"),
    "volumetric_weight": ("volumetric_weight", "volumetricWeight", "VOLUME_WEIGHT"),
    "shipping_fee": ("shipping_fee", "shippingFee", "FREIGHT"),
    "payment_type": ("payment_type", "paymentType", "PAYMENT_TYPE"),
    "pay_on_arrival": ("pay_on_arrival", "payOnArrival", "TOPAYMENT"),
}


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _value(row: Mapping[str, object], field: str) -> object:
    for alias in _ALIASES[field]:
        if alias in row:
            return row.get(alias)
    return None


def _decimal(value: object, scale: Decimal) -> str | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("waybill numeric value is invalid") from exc
    if not number.is_finite():
        raise ValueError("waybill numeric value is not finite")
    return str(number.quantize(scale, rounding=ROUND_HALF_UP))


def _integer(value: object, *, label: str, missing_as_zero: bool = False) -> int | None:
    if value in (None, ""):
        return 0 if missing_as_zero else None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"{label} must be an integer")
    result = int(number)
    if result < 0:
        raise ValueError(f"{label} cannot be negative")
    return result


def _bounded_limit(
    value: object,
    *,
    label: str,
    default: int | None,
    maximum: int = _MAX_RECORDS,
) -> int | None:
    if value in (None, ""):
        return default
    result = _integer(value, label=label)
    assert result is not None
    if result > maximum:
        raise ValueError(f"{label} exceeds its signed limit")
    return result


def _target_date(arguments: Mapping[str, object]) -> str:
    value = str(arguments.get("target_date") or "").strip()
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _validate_arguments(arguments: Mapping[str, object]) -> None:
    if arguments.get("refresh_disabled") not in (None, False):
        raise ValueError("arrival statistics requires a fresh target-day scan")
    if arguments.get("scan_window_days") not in (None, 1):
        raise ValueError("arrival statistics accepts one target-day scan only")
    request_override = arguments.get("arrive_list_request_body")
    if request_override not in (None, "", {}):
        raise ValueError("unsigned arrive-list request overrides are not supported")


def _normalize_waybill(raw: object) -> dict[str, object]:
    if isinstance(raw, Mapping):
        row = dict(raw)
    elif isinstance(raw, (list, tuple)):
        row = {
            field: raw[index] if index < len(raw) else None
            for index, field in enumerate(_WAYBILL_FIELDS)
        }
    else:
        raise ValueError("waybill row is invalid")
    tracking = str(_value(row, "tracking_number") or "").strip()
    if not tracking:
        raise ValueError("waybill row has no main waybill number")
    return {
        "tracking_number": tracking,
        "goods_name": str(_value(row, "goods_name") or "").strip(),
        "package_type": str(_value(row, "package_type") or "").strip(),
        "delivery_method": str(_value(row, "delivery_method") or "").strip(),
        "quantity": _integer(_value(row, "quantity"), label="waybill quantity"),
        "receipt_number": str(_value(row, "receipt_number") or "").strip(),
        "actual_weight": _decimal(_value(row, "actual_weight"), _MONEY),
        "volume": _decimal(_value(row, "volume"), _VOLUME),
        "remarks": str(_value(row, "remarks") or "").strip(),
        "destination_station": str(_value(row, "destination_station") or "").strip(),
        "recipient_name": str(_value(row, "recipient_name") or "").strip(),
        "recipient_phone": str(_value(row, "recipient_phone") or "").strip(),
        "recipient_address": str(_value(row, "recipient_address") or "").strip(),
        "settlement_weight": _decimal(_value(row, "settlement_weight"), _MONEY),
        "volumetric_weight": _decimal(_value(row, "volumetric_weight"), _MONEY),
        "shipping_fee": _decimal(_value(row, "shipping_fee"), _MONEY),
        "payment_type": str(_value(row, "payment_type") or "").strip(),
        "pay_on_arrival": _decimal(_value(row, "pay_on_arrival"), _MONEY),
    }


def _collect_waybills(
    target_date: str,
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], int, list[str]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    records: dict[str, dict[str, object]] = {}
    evidence_refs: list[str] = []
    pages = 0
    while True:
        page = _object(
            broker(
                "browser.invoke",
                action="ronghui.arrive_list.read_page",
                role=_ROLE,
                arguments={"target_date": target_date, "cursor": cursor, "page_size": 200},
            ),
            "arrive-list page",
        )
        evidence_refs.append(broker_evidence_ref(page, "arrive-list page"))
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("arrive-list page items are invalid")
        for raw in items:
            record = _normalize_waybill(raw)
            tracking = str(record["tracking_number"])
            if tracking.upper().startswith("H"):
                continue
            previous = records.get(tracking)
            if previous is not None and previous != record:
                raise ValueError("arrive-list duplicate waybill is inconsistent")
            records[tracking] = record
        pages += 1
        if pages > _MAX_PAGES:
            raise ValueError("arrive-list pagination exceeded its signed limit")
        if page.get("pagination_complete") is True:
            if page.get("next_cursor") not in (None, ""):
                raise ValueError("complete arrive-list page returned a cursor")
            break
        cursor = str(page.get("next_cursor") or "").strip()
        if not cursor or len(cursor) > 1024 or cursor in seen_cursors:
            raise ValueError("arrive-list pagination cursor is invalid")
        seen_cursors.add(cursor)
    return [records[key] for key in sorted(records)], pages, evidence_refs


def _scan_source_row(raw: object) -> dict[str, str]:
    row = _object(raw, "scan row")
    if set(row) != {"bill_code", "destination", "scan_type", "scan_time", "scan_site"}:
        raise ValueError("scan row schema is invalid")
    code = str(row.get("bill_code") or "").strip()
    if not code or len(code) > 128:
        raise ValueError("scan row bill code is invalid")
    return {
        "bill_code": code,
        "destination": str(row.get("destination") or "").strip(),
        "scan_type": str(row.get("scan_type") or "").strip(),
        "scan_time": str(row.get("scan_time") or "").strip(),
        "scan_site": str(row.get("scan_site") or "").strip(),
    }


def _collect_scans(
    target_date: str,
    broker: Callable[..., object],
) -> tuple[list[dict[str, str]], int, list[str]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    identities: dict[tuple[str, str, str], dict[str, str]] = {}
    evidence_refs: list[str] = []
    pages = 0
    while True:
        page = _object(
            broker(
                "browser.invoke",
                action="ronghui.scan.read_page",
                role=_ROLE,
                arguments={"target_date": target_date, "cursor": cursor, "page_size": 200},
            ),
            "scan page",
        )
        evidence_refs.append(broker_evidence_ref(page, "scan page"))
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("scan page items are invalid")
        for raw in items:
            row = _scan_source_row(raw)
            identity = (row["bill_code"], row["scan_type"], row["scan_time"])
            previous = identities.get(identity)
            if previous is not None and previous != row:
                raise ValueError("scan duplicate identity is inconsistent")
            identities[identity] = row
        pages += 1
        if pages > _MAX_PAGES:
            raise ValueError("scan pagination exceeded its signed limit")
        if page.get("pagination_complete") is True:
            if page.get("next_cursor") not in (None, ""):
                raise ValueError("complete scan page returned a cursor")
            break
        cursor = str(page.get("next_cursor") or "").strip()
        if not cursor or len(cursor) > 1024 or cursor in seen_cursors:
            raise ValueError("scan pagination cursor is invalid")
        seen_cursors.add(cursor)
    rows = [identities[key] for key in sorted(identities)]
    if len(rows) > _MAX_RECORDS:
        raise ValueError("scan source exceeds its signed record limit")
    return rows, pages, evidence_refs


def _main_tracking(code: str, known: set[str]) -> str | None:
    value = str(code or "").strip()
    if not value or value.upper().startswith("H"):
        return None
    if _R_CHILD.fullmatch(value) or _NUMERIC_CHILD.fullmatch(value):
        return value[:-4]
    if len(value) > 4 and value[:-4] in known:
        return value[:-4]
    return value


def _normalized_scan_snapshot(rows: list[Mapping[str, object]]) -> list[dict[str, str]]:
    codes = {str(row.get("bill_code") or row.get("raw_code") or "").strip() for row in rows}
    output: dict[str, dict[str, str]] = {}
    for raw in rows:
        code = str(raw.get("bill_code") or raw.get("raw_code") or "").strip()
        main = _main_tracking(code, codes)
        if main is None:
            continue
        normalized = {
            "raw_code": code,
            "destination": str(raw.get("destination") or "").strip(),
            "code_type": "child" if code != main else "main",
            "main_tracking": main,
        }
        previous = output.get(code)
        if previous is not None and previous != normalized:
            raise ValueError("scan code has inconsistent normalized data")
        output[code] = normalized
    return [output[key] for key in sorted(output)]


def _persisted_scan_rows(value: object) -> list[dict[str, str]]:
    payload = _object(value, "scan snapshot")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > _MAX_RECORDS:
        raise ValueError("scan snapshot rows are invalid")
    source_rows: list[dict[str, object]] = []
    for raw in items:
        row = _object(raw, "persisted scan row")
        if set(row) != {"raw_code", "destination", "code_type"}:
            raise ValueError("persisted scan row schema is invalid")
        source_rows.append(row)
    return _normalized_scan_snapshot(source_rows)


def _needs_detail(record: Mapping[str, object]) -> bool:
    if not any(record.get(field) not in (None, "", 0) for field in _WAYBILL_FIELDS[1:]):
        return True
    for field in ("recipient_name", "recipient_phone"):
        if "*" in str(record.get(field) or ""):
            return True
    address = str(record.get("recipient_address") or "").strip()
    return not address or len(address) < 8 or "|" in address or address.endswith(("省", "市", "区", "县"))


def _merge_records(
    existing: list[dict[str, object]],
    fetched: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged = {str(row["tracking_number"]): dict(row) for row in existing}
    for row in fetched:
        tracking = str(row["tracking_number"])
        payload = dict(merged.get(tracking, {}))
        for key, value in row.items():
            if value not in (None, ""):
                payload[key] = value
        merged[tracking] = payload
    return sorted(
        merged.values(),
        key=lambda row: (str(row.get("destination_station") or ""), str(row["tracking_number"])),
    )


def _arrival_counts(
    scan_rows: list[dict[str, str]],
    records: list[dict[str, object]],
    tracking_numbers: list[str],
) -> tuple[dict[str, int], dict[str, int | str | bool]]:
    child_counts: Counter[str] = Counter()
    direct_main: set[str] = set()
    for row in scan_rows:
        main = row["main_tracking"]
        if row["raw_code"] == main:
            direct_main.add(main)
        else:
            child_counts[main] += 1
    quantities = {
        str(row["tracking_number"]): _integer(
            row.get("quantity"),
            label="waybill quantity",
            missing_as_zero=True,
        )
        for row in records
    }
    counts: dict[str, int] = {}
    adjustments = 0
    quantity_gaps = 0
    for tracking in tracking_numbers:
        arrived = child_counts.get(tracking, 0)
        expected = quantities.get(tracking) or 0
        if expected > arrived and (arrived > 0 or tracking in direct_main):
            quantity_gaps += 1
        if expected > 0 and arrived > expected:
            arrived = expected
            adjustments += 1
        counts[tracking] = arrived
    return counts, {
        "ok": True,
        "source": "scan_index",
        "requested": len(tracking_numbers),
        "counted": len(counts),
        "arrived_nonzero": sum(1 for value in counts.values() if value > 0),
        "scan_rows": len(scan_rows),
        "child_scan_rows": sum(child_counts.values()),
        "direct_main_rows": len(direct_main),
        "quantity_adjustments": adjustments,
        "quantity_gaps": quantity_gaps,
    }


def _committed(value: object, label: str, *, allow_skipped: bool = False) -> dict[str, object]:
    result = _object(value, label)
    if result.get("committed") is not True:
        raise ValueError(f"{label} was not committed")
    if result.get("skipped") is True and not allow_skipped:
        raise ValueError(f"{label} was unexpectedly skipped")
    return result


def _projection_read(
    broker: Callable[..., object],
    *,
    action: str,
    arguments: Mapping[str, object],
    label: str,
) -> tuple[dict[str, object], str]:
    result = _object(
        broker(
            "projection.invoke",
            action=action,
            role=_ROLE,
            arguments=dict(arguments),
        ),
        label,
    )
    return result, broker_evidence_ref(result, label)


def run_action(
    arguments: dict[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    _validate_arguments(arguments)
    target_date = _target_date(arguments)
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    arrive_records, arrive_pages, evidence_refs = _collect_waybills(target_date, broker)
    current_scan_source, scan_pages, scan_evidence = _collect_scans(target_date, broker)
    evidence_refs.extend(scan_evidence)
    current_scan = _normalized_scan_snapshot(current_scan_source)
    current_main = {row["main_tracking"] for row in current_scan}

    completed_result, completed_ref = _projection_read(
        broker,
        action="arrival.snapshot.completed_before",
        arguments={"target_date": target_date},
        label="completed arrival history",
    )
    evidence_refs.append(completed_ref)
    completed_values = completed_result.get("tracking_numbers")
    if not isinstance(completed_values, list) or any(not isinstance(item, str) for item in completed_values):
        raise ValueError("completed arrival history is invalid")
    completed = set(completed_values)
    arrive_records = [
        row
        for row in arrive_records
        if str(row["tracking_number"]) not in completed or str(row["tracking_number"]) in current_main
    ]

    dry_run = arguments.get("dry_run") is True
    if dry_run:
        scan_write = None
    else:
        scan_write = _committed(
            broker(
                "projection.invoke",
                action="scan.snapshot.replace",
                role=_ROLE,
                arguments={"records": current_scan, "target_date": target_date},
            ),
            "scan snapshot",
        )
        evidence_refs.append(broker_evidence_ref(scan_write, "scan snapshot"))

    if not dry_run:
        retention = _bounded_limit(
            arguments.get("scan_codes_retention_days"),
            label="scan_codes_retention_days",
            default=30,
            maximum=3650,
        )
        assert retention is not None
        cleanup = _committed(
            broker(
                "projection.invoke",
                action="scan.snapshot.cleanup",
                role=_ROLE,
                arguments={"retention_days": retention},
            ),
            "scan snapshot cleanup",
            allow_skipped=True,
        )
        evidence_refs.append(broker_evidence_ref(cleanup, "scan snapshot cleanup"))

    accumulated_result, accumulated_ref = _projection_read(
        broker,
        action="scan.snapshot.read",
        arguments={"target_date": target_date},
        label="accumulated scan snapshot",
    )
    evidence_refs.append(accumulated_ref)
    accumulated_scan = _persisted_scan_rows(accumulated_result)

    existing_trackings = {str(row["tracking_number"]) for row in arrive_records}
    missing = sorted(current_main - existing_trackings)
    missing_limit = _bounded_limit(arguments.get("missing_limit"), label="missing_limit", default=None)
    if missing_limit is not None:
        missing = missing[:missing_limit]
    stale = [str(row["tracking_number"]) for row in arrive_records if _needs_detail(row)]
    detail_codes = list(dict.fromkeys([*missing, *stale]))
    fetched: list[dict[str, object]] = []
    for tracking in detail_codes:
        detail = _object(
            broker(
                "browser.invoke",
                action="ronghui.waybill_detail.read",
                role=_ROLE,
                arguments={"tracking_number": tracking},
            ),
            "waybill detail",
        )
        evidence_refs.append(broker_evidence_ref(detail, "waybill detail"))
        if detail.get("found") is True:
            record = _normalize_waybill(detail.get("record"))
            if record["tracking_number"] != tracking:
                raise ValueError("waybill detail changed the requested identity")
            fetched.append(record)
        elif detail.get("found") is not False:
            raise ValueError("waybill detail found flag is invalid")

    merged = _merge_records(arrive_records, fetched)
    merged_trackings = {str(row["tracking_number"]) for row in merged}
    missing_scanned = sorted(current_main - merged_trackings)
    if missing_scanned:
        raise ValueError("target-day scan waybills are missing exact detail records")

    export_limit = _bounded_limit(arguments.get("export_limit"), label="export_limit", default=None)
    export_records = merged if export_limit is None else merged[:export_limit]
    tracking_numbers = [str(row["tracking_number"]) for row in export_records]
    child_limit = _bounded_limit(
        arguments.get("child_count_limit"),
        label="child_count_limit",
        default=None,
    )
    if child_limit is not None:
        tracking_numbers = tracking_numbers[:child_limit]
    count_map, count_result = _arrival_counts(accumulated_scan, export_records, tracking_numbers)
    stats_records = [
        {**row, "arrived_quantity": count_map.get(str(row["tracking_number"]))}
        for row in export_records
    ]

    warnings: list[str] = []
    if dry_run:
        commit_state = "dry_run_complete"
    else:
        waybill = _committed(
            broker(
                "projection.invoke",
                action="waybill.snapshot.replace",
                role=_ROLE,
                arguments={"records": merged, "target_date": target_date},
            ),
            "waybill snapshot",
        )
        evidence_refs.append(broker_evidence_ref(waybill, "waybill snapshot"))
        for slot, role in (
            ("arrival_stats_primary", "arrival_stats_primary_sheet"),
            ("arrival_stats_secondary", "arrival_stats_secondary_sheet"),
        ):
            result = _committed(
                broker(
                    "network.request",
                    action="feishu.sheet.replace",
                    role=role,
                    arguments={"resource_slot": slot, "records": stats_records, "target_date": target_date},
                ),
                slot,
            )
            evidence_refs.append(broker_evidence_ref(result, slot))

        if arguments.get("pending_sheet_disabled") is not True:
            pending_result, pending_ref = _projection_read(
                broker,
                action="waybill.pending.read",
                arguments={"target_date": target_date},
                label="pending waybills",
            )
            evidence_refs.append(pending_ref)
            pending_records = pending_result.get("items")
            if not isinstance(pending_records, list):
                raise ValueError("pending waybill projection is invalid")
            pending_write = _committed(
                broker(
                    "network.request",
                    action="feishu.sheet.replace",
                    role="arrival_stats_pending_sheet",
                    arguments={
                        "resource_slot": "arrival_stats_pending",
                        "records": pending_records,
                        "target_date": target_date,
                    },
                ),
                "pending arrivals sheet",
                allow_skipped=True,
            )
            evidence_refs.append(broker_evidence_ref(pending_write, "pending arrivals sheet"))
            if pending_write.get("skipped") is True:
                warnings.append("optional pending-arrivals sheet was unavailable")

        if stats_records and arguments.get("archive_snapshot", True) is True:
            archive = _committed(
                broker(
                    "network.request",
                    action="feishu.sheet.add",
                    role="arrival_stats_archive_sheet",
                    arguments={"records": stats_records, "target_date": target_date},
                ),
                "arrival statistics archive",
            )
            evidence_refs.append(broker_evidence_ref(archive, "arrival statistics archive"))

        split_pending = _committed(
            broker(
                "projection.invoke",
                action="split_pending.snapshot.refresh",
                role=_ROLE,
                arguments={"records": stats_records, "target_date": target_date},
            ),
            "split-pending snapshot",
        )
        evidence_refs.append(broker_evidence_ref(split_pending, "split-pending snapshot"))
        split_pending_sheet = _committed(
            broker(
                "network.request",
                action="feishu.sheet.replace",
                role="arrival_stats_split_pending_sheet",
                arguments={
                    "resource_slot": "arrival_stats_split_pending",
                    "records": stats_records,
                    "target_date": target_date,
                },
            ),
            "split-pending sheet",
        )
        evidence_refs.append(
            broker_evidence_ref(split_pending_sheet, "split-pending sheet")
        )
        arrival_records = [
            {
                "tracking_number": row["tracking_number"],
                "destination_station": row.get("destination_station"),
                "expected_quantity": row.get("quantity"),
                "arrived_quantity": count_map.get(str(row["tracking_number"])),
                "goods_name": row.get("goods_name"),
                "package_type": row.get("package_type"),
                "delivery_method": row.get("delivery_method"),
                "recipient_address": row.get("recipient_address"),
            }
            for row in export_records
        ]
        arrival_snapshot = _committed(
            broker(
                "projection.invoke",
                action="arrival.snapshot.replace",
                role=_ROLE,
                arguments={"records": arrival_records, "target_date": target_date},
            ),
            "arrival statistics snapshot",
        )
        evidence_refs.append(broker_evidence_ref(arrival_snapshot, "arrival statistics snapshot"))
        commit_state = (
            "no_data_cleared"
            if not export_records
            else "all_required_outputs_committed"
        )

    data: dict[str, object] = {
        "dry_run": dry_run,
        "records": len(export_records),
        "arrive_pages": arrive_pages,
        "scan_pages": scan_pages,
        "current_main_trackings": len(current_main),
        "accumulated_main_trackings": len({row["main_tracking"] for row in accumulated_scan}),
        "detail_requested": len(detail_codes),
        "detail_fetched": len(fetched),
        "count_result": count_result,
        "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "pagination_complete": True,
            "execution_result": commit_state,
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
        source_system="ronghui+feishu+internal_projection",
        record_count=len(export_records),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
        warnings=warnings,
    )
