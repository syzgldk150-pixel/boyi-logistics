"""Package-owned arrive-list pagination, normalization and commit ordering."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_arrive_list"
_ROLE = "account_id"
_MAX_PAGES = 500
_MONEY = Decimal("0.01")
_VOLUME = Decimal("0.001")
_FIELDS = (
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
        return str(Decimal(str(value)).quantize(scale, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("arrive-list numeric value is invalid") from exc


def _integer(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("arrive-list quantity is invalid") from exc
    if number != number.to_integral_value():
        raise ValueError("arrive-list quantity must be an integer")
    return int(number)


def _normalize_row(raw: object) -> dict[str, object]:
    if isinstance(raw, Mapping):
        row = dict(raw)
    elif isinstance(raw, (list, tuple)):
        row = {field: raw[index] if index < len(raw) else None for index, field in enumerate(_FIELDS)}
    else:
        raise ValueError("arrive-list row is invalid")
    tracking = str(_value(row, "tracking_number") or "").strip()
    if not tracking:
        raise ValueError("arrive-list row has no main waybill number")
    return {
        "tracking_number": tracking,
        "goods_name": str(_value(row, "goods_name") or "").strip(),
        "package_type": str(_value(row, "package_type") or "").strip(),
        "delivery_method": str(_value(row, "delivery_method") or "").strip(),
        "quantity": _integer(_value(row, "quantity")),
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


def _target_date(arguments: Mapping[str, object]) -> str:
    raw = str(arguments.get("target_date") or "").strip()
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _collect(
    arguments: Mapping[str, object],
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], int, int, list[str]]:
    cursor: str | None = None
    seen: set[str] = set()
    records: dict[str, dict[str, object]] = {}
    skipped_receipts = 0
    pages = 0
    evidence_refs: list[str] = []
    while True:
        page = _object(
            broker(
                "browser.invoke",
                action="ronghui.arrive_list.read_page",
                role=_ROLE,
                arguments={"target_date": _target_date(arguments), "cursor": cursor, "page_size": 200},
            ),
            "arrive-list page",
        )
        items = page.get("items")
        evidence_refs.append(broker_evidence_ref(page, "arrive-list page"))
        if not isinstance(items, list):
            raise ValueError("arrive-list page items are invalid")
        for raw in items:
            record = _normalize_row(raw)
            tracking = str(record["tracking_number"])
            if tracking.upper().startswith("H"):
                skipped_receipts += 1
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
            return (
                [records[key] for key in sorted(records)],
                pages,
                skipped_receipts,
                evidence_refs,
            )
        cursor = str(page.get("next_cursor") or "").strip()
        if not cursor or len(cursor) > 1024 or cursor in seen:
            raise ValueError("arrive-list pagination cursor is invalid")
        seen.add(cursor)


def _sheet_rows(records: list[dict[str, object]]) -> list[list[object]]:
    ordered = sorted(
        records,
        key=lambda item: (str(item["destination_station"]), str(item["tracking_number"])),
    )
    return [[item.get(field) if item.get(field) is not None else "" for field in _FIELDS] for item in ordered]


def _committed(value: object, label: str) -> dict[str, object]:
    result = _object(value, label)
    if result.get("committed") is not True:
        raise ValueError(f"{label} was not committed")
    return result


def run_action(arguments: dict[str, object], broker: Callable[..., object]) -> dict[str, object]:
    records, page_count, skipped_receipts, evidence_refs = _collect(arguments, broker)
    target_date = _target_date(arguments)
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    if arguments.get("dry_run") is True:
        data = {
            "dry_run": True,
            "fetched": len(records) + skipped_receipts,
            "bill_codes": len(records),
            "skipped_receipt_like": skipped_receipts,
            "evidence": {
                "source": "signed_first_party_plugin",
                "observed_at": observed_at,
                "pagination_complete": True,
                "page_count": page_count,
                "execution_result": "dry_run_complete",
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
            source_system="ronghui",
            record_count=len(records),
            pagination_complete=True,
            evidence_refs=evidence_refs,
            observed_at=observed_at,
            postconditions={"0": True},
            postcondition_evidence={"0": result_proof},
        )
    projection = _committed(
        broker(
            "projection.invoke",
            action="waybill.snapshot.replace",
            role=_ROLE,
            arguments={"records": records, "target_date": target_date},
        ),
        "waybill snapshot",
    )
    evidence_refs.append(broker_evidence_ref(projection, "waybill snapshot"))
    values = _sheet_rows(records)
    for slot in ("arrive_primary_sheet", "arrive_secondary_sheet"):
        sheet_result = _committed(
            broker(
                "network.request",
                action="feishu.sheet.replace",
                role=slot,
                arguments={"resource_slot": slot, "values": values, "target_date": target_date},
            ),
            slot,
        )
        evidence_refs.append(broker_evidence_ref(sheet_result, slot))
    forecast = _committed(
        broker(
            "projection.invoke",
            action="arrival.forecast_snapshot.replace",
            role=_ROLE,
            arguments={"records": records, "target_date": target_date},
        ),
        "arrival forecast snapshot",
    )
    evidence_refs.append(broker_evidence_ref(forecast, "arrival forecast snapshot"))
    data = {
        "fetched": len(records) + skipped_receipts,
        "bill_codes": len(records),
        "skipped_receipt_like": skipped_receipts,
        "detail_records": len(records),
        "evidence": {
            "source": "signed_first_party_plugin",
            "observed_at": observed_at,
            "pagination_complete": True,
            "page_count": page_count,
            "execution_result": (
                "no_data_cleared" if not records else "all_snapshots_committed"
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
        source_system="ronghui+feishu+mysql",
        record_count=len(records),
        pagination_complete=True,
        evidence_refs=evidence_refs,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
    )
