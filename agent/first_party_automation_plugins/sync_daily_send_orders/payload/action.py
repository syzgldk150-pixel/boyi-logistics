"""Package-owned Ronghui send-order snapshot synchronization."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from boyi_plugin_result import (
    broker_evidence_ref,
    executor_success_evidence,
    success_result,
)


ACTION_ID = "sync_daily_send_orders"
_ACCOUNT_ROLE = "account_id"
_BITABLE_ROLE = "send_order_bitable"
_TIMEZONE = ZoneInfo("Asia/Shanghai")
_SOURCE_PAGE_SIZE = 100
_SOURCE_MAX_PAGES = 50
_BITABLE_PAGE_SIZE = 200
_BITABLE_MAX_PAGES = 50
_MAX_RANGE_DAYS = 366
_MAX_BROKER_CALLS = 1_000
_MAX_RECORDS = 10_000
_WAYBILL_FIELD = "运单编号"
_DATE_FIELD = "发件日期"
_DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})")
_ALLOWED_ARGUMENTS = frozenset(
    {
        "target_date",
        "start_date",
        "end_date",
        "page_size",
        "max_pages",
        "dry_run",
        "sql_only",
        "sync_sql",
    }
)
_FIELD_MAP = (
    ("BILL_CODE", _WAYBILL_FIELD),
    ("INSERT_DATE", _DATE_FIELD),
    ("BL_SIGNS_MARKING_TEXT", "签收状态"),
    ("DESTINATION", "目的网点"),
    ("ACCEPT_COUNTY", "收件区/县"),
    ("ACCEPT_MAN_ADDRESS", "收件地址"),
    ("SEND_MAN", "寄件人"),
    ("SEND_MAN_PHONE", "寄件手机"),
    ("ACCEPT_MAN", "收货人"),
    ("ACCEPT_MAN_PHONE", "收货电话"),
    ("GOODS_NAME", "货物名称"),
    ("PACK_TYPE", "包装类型"),
    ("DISPATCH_MODE", "派送方式"),
    ("PIECE_NUMBER", "件数"),
    ("FEE_WEIGHT", "实际重量"),
    ("GUEST_FREIGHT", "录单金额"),
    ("R_BILLCODE", "回单号"),
    ("REMARK", "备注"),
    ("PAYMENT_TYPE", "支付类型"),
    ("VOLUME_WEIGHT", "体积重量"),
    ("VOLUME", "体积"),
    ("SETTLEMENT_WEIGHT", "结算重量"),
    ("TOPAYMENT", "到付款"),
)
_BITABLE_FIELDS = tuple(destination for _source, destination in _FIELD_MAP)
_NUMERIC_FIELDS = frozenset(
    {
        "件数",
        "实际重量",
        "录单金额",
        "体积重量",
        "体积",
        "结算重量",
        "到付款",
    }
)
_SCAN_STATUS_FIELDS = (
    "当前扫描状态",
    "最新扫描状态",
    "扫描状态",
    "最新扫描类型",
    "scan_status",
    "current_scan_status",
    "scan_type",
    "SCAN_TYPE",
)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (Mapping, list, tuple, set)):
        raise ValueError("send-order text field is invalid")
    return str(value).strip()


def _field_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("send-order field text is invalid")
    if isinstance(value, (str, int, float, Decimal)):
        return str(value).strip()
    if isinstance(value, (list, tuple)):
        return "".join(_field_text(item) for item in value).strip()
    if isinstance(value, Mapping):
        row = dict(value)
        for key in ("text", "value", "name", "link"):
            text_value = _field_text(row.get(key))
            if text_value:
                return text_value
        return "".join(_field_text(item) for item in row.values()).strip()
    raise ValueError("send-order field text is invalid")


def _waybill(value: object) -> str:
    result = _field_text(value)
    if result.startswith("="):
        result = result[1:].strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {"'", '"'}:
        result = result[1:-1].strip()
    result = "".join(result.split())
    if len(result) > 128:
        raise ValueError("send-order waybill identity is too long")
    return result


def _business_date(value: object, label: str) -> date:
    text_value = str(value or "").strip()
    try:
        return date.fromisoformat(text_value)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD") from exc


def _dates(arguments: Mapping[str, object]) -> list[str]:
    raw_start = str(arguments.get("start_date") or "").strip()
    raw_end = str(arguments.get("end_date") or "").strip()
    raw_target = str(arguments.get("target_date") or "").strip()
    if raw_target and (raw_start or raw_end):
        raise ValueError("target_date cannot be combined with a date range")
    if not raw_start and not raw_end:
        target = _business_date(raw_target, "target_date") if raw_target else datetime.now(_TIMEZONE).date()
        return [target.isoformat()]
    start = _business_date(raw_start or raw_end, "start_date")
    end = _business_date(raw_end or raw_start, "end_date")
    if start > end:
        raise ValueError("start_date cannot be later than end_date")
    count = (end - start).days + 1
    if count > _MAX_RANGE_DAYS:
        raise ValueError("send-order date range exceeds its signed limit")
    return [(start + timedelta(days=offset)).isoformat() for offset in range(count)]


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
        result = int(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if str(result) != str(value).strip() or not minimum <= result <= maximum:
        raise ValueError(f"{label} is outside its signed limit")
    return result


def _flag(arguments: Mapping[str, object], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if result < 0 or str(result) != str(value).strip():
        raise ValueError(f"{label} is invalid")
    return result


def _number(value: object) -> int | str | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        raise ValueError("send-order numeric field is invalid")
    text_value = str(value).replace(",", "").strip()
    try:
        number = Decimal(text_value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("send-order numeric field is invalid") from exc
    if not number.is_finite():
        raise ValueError("send-order numeric field is invalid")
    if number == number.to_integral_value():
        return int(number)
    return format(number, "f")


def _number_text(value: object) -> str:
    number = _number(value)
    return "" if number is None else str(number)


def _timestamp_ms(value: object, *, target_date: date) -> int:
    value_datetime: datetime | None = None
    if isinstance(value, datetime):
        value_datetime = value
    elif isinstance(value, date):
        value_datetime = datetime.combine(value, time.min)
    elif value not in (None, ""):
        text_value = str(value).strip()
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d",
        ):
            try:
                value_datetime = datetime.strptime(
                    text_value[:19] if " " in fmt else text_value[:10],
                    fmt,
                )
                break
            except ValueError:
                continue
    if value_datetime is None:
        value_datetime = datetime.combine(target_date, time.min)
    if value_datetime.tzinfo is None:
        value_datetime = value_datetime.replace(tzinfo=_TIMEZONE)
    return int(value_datetime.timestamp() * 1000)


def _date_text(value: object) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, _TIMEZONE).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    if isinstance(value, datetime):
        value_datetime = value if value.tzinfo else value.replace(tzinfo=_TIMEZONE)
        return value_datetime.astimezone(_TIMEZONE).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "link"):
            result = _date_text(value.get(key))
            if result:
                return result
        return ""
    if isinstance(value, (list, tuple)):
        for item in value:
            result = _date_text(item)
            if result:
                return result
        return ""
    text_value = str(value).strip()
    if text_value.isdigit():
        return _date_text(int(text_value))
    match = _DATE_RE.search(text_value)
    if match is None:
        return ""
    year, month, day = match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def _bitable_timestamp(value: object, *, target_date: str) -> int:
    if isinstance(value, bool):
        raise ValueError("send-order Bitable date field is invalid")
    if isinstance(value, (int, float, Decimal)):
        try:
            timestamp = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("send-order Bitable date field is invalid") from exc
        if not timestamp.is_finite():
            raise ValueError("send-order Bitable date field is invalid")
        if timestamp < Decimal("10000000000"):
            timestamp *= 1000
        integral = timestamp.to_integral_value()
        if timestamp != integral:
            raise ValueError("send-order Bitable date field is invalid")
        return int(integral)
    return _timestamp_ms(value, target_date=_business_date(target_date, "target_date"))


def _canonical_bitable_fields(
    value: object,
    *,
    target_date: str,
) -> dict[str, object]:
    fields = _object(value, "send-order Bitable fields")
    if set(fields) != set(_BITABLE_FIELDS):
        raise ValueError("send-order Bitable field set is incomplete")
    canonical: dict[str, object] = {}
    for field in _BITABLE_FIELDS:
        raw = fields.get(field)
        if field == _WAYBILL_FIELD:
            canonical[field] = _waybill(raw)
        elif field == _DATE_FIELD:
            canonical[field] = _bitable_timestamp(raw, target_date=target_date)
        elif field in _NUMERIC_FIELDS:
            canonical[field] = _number(raw)
        else:
            canonical[field] = _field_text(raw)
    if not canonical[_WAYBILL_FIELD]:
        raise ValueError("send-order Bitable record has no waybill identity")
    return canonical


def _status(value: object) -> str:
    return "signed" if "".join(_text(value).split()) in {"签收", "已签收"} else "in_transit"


def _scan_status(row: Mapping[str, object]) -> str:
    for key in _SCAN_STATUS_FIELDS:
        value = _field_text(row.get(key))
        if value:
            return value
    return ""


def _weight_volume(row: Mapping[str, object]) -> str:
    parts: list[str] = []
    for label, source in (
        ("实际重量", "FEE_WEIGHT"),
        ("体积", "VOLUME"),
        ("结算重量", "SETTLEMENT_WEIGHT"),
        ("体积重", "VOLUME_WEIGHT"),
    ):
        value = _number_text(row.get(source))
        if value:
            parts.append(f"{label} {value}")
    return " / ".join(parts)


def _normalize_source_row(
    row: Mapping[str, object],
    *,
    target_date: str,
) -> tuple[dict[str, object], dict[str, object]]:
    target = _business_date(target_date, "target_date")
    raw_source_date = row.get("INSERT_DATE")
    source_date = _date_text(raw_source_date)
    if raw_source_date not in (None, "") and not source_date:
        raise ValueError("Ronghui send-order row has an invalid send date")
    if source_date and source_date != target_date:
        raise ValueError("Ronghui send-order row is outside the requested date")
    fields: dict[str, object] = {}
    for source, destination in _FIELD_MAP:
        raw = row.get(source)
        if destination == _DATE_FIELD:
            fields[destination] = _timestamp_ms(raw, target_date=target)
        elif destination in _NUMERIC_FIELDS:
            fields[destination] = _number(raw)
        else:
            fields[destination] = _text(raw)
    waybill_no = _waybill(row.get("BILL_CODE"))
    fields[_WAYBILL_FIELD] = waybill_no
    projection = {
        "waybill_no": waybill_no,
        "destination_site": _text(row.get("DESTINATION")),
        "open_date": source_date or target_date,
        "receiver_address": _text(row.get("ACCEPT_MAN_ADDRESS")),
        "receiver_name": _text(row.get("ACCEPT_MAN")),
        "receiver_phone": _text(row.get("ACCEPT_MAN_PHONE")),
        "sender_name": _text(row.get("SEND_MAN")),
        "sender_phone": _text(row.get("SEND_MAN_PHONE")),
        "goods_name_lines": _text(row.get("GOODS_NAME")),
        "package_type_lines": _text(row.get("PACK_TYPE")),
        "quantity_lines": _number_text(row.get("PIECE_NUMBER")),
        "weight_volume": _weight_volume(row),
        "delivery_method": _text(row.get("DISPATCH_MODE")),
        "freight_fee": _number_text(row.get("GUEST_FREIGHT")),
        "pickup_fee": "",
        "delivery_fee": "",
        "transfer_fee": "",
        "payment_method": _text(row.get("PAYMENT_TYPE")),
        "insurance_amount": "",
        "cod_amount": _number_text(row.get("TOPAYMENT")),
        "remark": _text(row.get("REMARK")),
        "scan_status": _scan_status(row),
        "status": _status(row.get("BL_SIGNS_MARKING_TEXT")),
    }
    return fields, projection


def _collect_source(
    *,
    target_date: str,
    page_size: int,
    max_pages: int,
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], list[str], int]:
    rows: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    expected_total: int | None = None
    for page_index in range(max_pages):
        page = _object(
            broker(
                "browser.invoke",
                action="ronghui.send_order.read_page",
                role=_ACCOUNT_ROLE,
                arguments={
                    "target_date": target_date,
                    "page_index": page_index,
                    "page_size": page_size,
                },
            ),
            "Ronghui send-order page",
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("Ronghui send-order page items are invalid")
        if len(items) > page_size:
            raise ValueError("Ronghui send-order page exceeds page_size")
        total = _nonnegative_int(page.get("total"), "Ronghui send-order total")
        if total > _MAX_RECORDS:
            raise ValueError("Ronghui send-order result exceeds its signed limit")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise ValueError("Ronghui send-order total changed during pagination")
        rows.extend(_object(item, "Ronghui send-order row") for item in items)
        evidence_refs.append(broker_evidence_ref(page, "Ronghui send-order page"))
        if len(rows) > total:
            raise ValueError("Ronghui send-order page count exceeds total")
        if len(rows) == total:
            return rows, evidence_refs, page_index + 1
        if not items:
            raise ValueError("Ronghui send-order pagination ended before total")
    raise ValueError("Ronghui send-order pagination exceeded max_pages")


def _prepare_records(
    rows: list[dict[str, object]],
    *,
    target_date: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], int, int, int]:
    bitable_records: list[dict[str, object]] = []
    projection_records: list[dict[str, object]] = []
    seen_waybills: set[str] = set()
    skipped_receipt_like = 0
    skipped_missing_waybill = 0
    source_duplicates = 0
    for row in rows:
        waybill_no = _waybill(row.get("BILL_CODE"))
        if not waybill_no:
            skipped_missing_waybill += 1
            continue
        if waybill_no.upper().startswith("H"):
            skipped_receipt_like += 1
            continue
        if waybill_no in seen_waybills:
            source_duplicates += 1
            continue
        seen_waybills.add(waybill_no)
        fields, projection = _normalize_source_row(row, target_date=target_date)
        bitable_records.append({"fields": fields})
        projection_records.append(projection)
    return (
        bitable_records,
        projection_records,
        skipped_receipt_like,
        skipped_missing_waybill,
        source_duplicates,
    )


def _list_bitable_records(
    *,
    page_size: int,
    max_pages: int,
    fields: tuple[str, ...],
    broker: Callable[..., object],
) -> tuple[list[dict[str, object]], list[str], int]:
    if not fields or len(set(fields)) != len(fields) or not set(fields) <= set(_BITABLE_FIELDS):
        raise ValueError("send-order Bitable requested fields are invalid")
    records: list[dict[str, object]] = []
    evidence_refs: list[str] = []
    seen_refs: set[str] = set()
    offset = 0
    for page_index in range(max_pages):
        page = _object(
            broker(
                "network.request",
                action="feishu.bitable.list_records",
                role=_BITABLE_ROLE,
                arguments={
                    "offset": offset,
                    "page_size": page_size,
                    "fields": list(fields),
                },
            ),
            "send-order Bitable page",
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("send-order Bitable page items are invalid")
        if len(items) > page_size:
            raise ValueError("send-order Bitable page exceeds page_size")
        evidence_refs.append(broker_evidence_ref(page, "send-order Bitable page"))
        for raw in items:
            item = _object(raw, "send-order Bitable record")
            record_ref = _text(item.get("record_ref"))
            fields = item.get("fields")
            if not record_ref or len(record_ref) > 512 or not isinstance(fields, Mapping):
                raise ValueError("send-order Bitable record is incomplete")
            if record_ref in seen_refs:
                raise ValueError("send-order Bitable pagination repeated a record")
            seen_refs.add(record_ref)
            records.append({"record_ref": record_ref, "fields": dict(fields)})
        if len(records) > _MAX_RECORDS:
            raise ValueError("send-order Bitable scan exceeds its signed limit")
        if len(items) < page_size:
            return records, evidence_refs, page_index + 1
        offset += page_size
    raise ValueError("send-order Bitable pagination exceeded max_pages")


def _verify_bitable_snapshot(
    records: list[dict[str, object]],
    expected: list[dict[str, object]],
    *,
    target_date: str,
) -> None:
    actual_by_waybill: dict[str, dict[str, object]] = {}
    for record in records:
        fields = _canonical_bitable_fields(
            record.get("fields"),
            target_date=target_date,
        )
        if _date_text(fields[_DATE_FIELD]) != target_date:
            continue
        waybill_no = str(fields[_WAYBILL_FIELD])
        if waybill_no in actual_by_waybill:
            raise ValueError("send-order Bitable snapshot contains duplicate identities")
        actual_by_waybill[waybill_no] = fields

    expected_by_waybill: dict[str, dict[str, object]] = {}
    for record in expected:
        fields = _canonical_bitable_fields(
            record.get("fields"),
            target_date=target_date,
        )
        waybill_no = str(fields[_WAYBILL_FIELD])
        if waybill_no in expected_by_waybill:
            raise ValueError("send-order desired snapshot contains duplicate identities")
        expected_by_waybill[waybill_no] = fields
    if actual_by_waybill != expected_by_waybill:
        raise ValueError("send-order Bitable snapshot failed fresh readback verification")


def _target_record_refs(
    records: list[dict[str, object]],
    *,
    target_date: str,
) -> tuple[list[str], list[str]]:
    all_refs: list[str] = []
    duplicate_refs: list[str] = []
    seen_waybills: set[str] = set()
    for record in records:
        fields = _object(record.get("fields"), "send-order Bitable fields")
        if _date_text(fields.get(_DATE_FIELD)) != target_date:
            continue
        record_ref = _text(record.get("record_ref"))
        all_refs.append(record_ref)
        waybill_no = _waybill(fields.get(_WAYBILL_FIELD))
        if not waybill_no:
            continue
        if waybill_no in seen_waybills:
            duplicate_refs.append(record_ref)
        else:
            seen_waybills.add(waybill_no)
    return all_refs, duplicate_refs


def _committed(value: object, label: str) -> dict[str, object]:
    result = _object(value, label)
    if result.get("committed") is not True:
        raise ValueError(f"{label} was not committed")
    return result


def _delete_records(
    record_refs: list[str],
    *,
    broker: Callable[..., object],
    label: str,
) -> tuple[int, list[str]]:
    if not record_refs:
        return 0, []
    result = _committed(
        broker(
            "network.request",
            action="feishu.bitable.delete_records",
            role=_BITABLE_ROLE,
            arguments={"record_refs": record_refs},
        ),
        label,
    )
    deleted = _nonnegative_int(result.get("deleted"), f"{label} deleted count")
    if deleted != len(record_refs):
        raise ValueError(f"{label} deleted count changed")
    return deleted, [broker_evidence_ref(result, label)]


def _write_records(
    records: list[dict[str, object]],
    *,
    broker: Callable[..., object],
) -> tuple[int, list[str]]:
    if not records:
        return 0, []
    result = _committed(
        broker(
            "network.request",
            action="feishu.bitable.write_records",
            role=_BITABLE_ROLE,
            arguments={"records": records},
        ),
        "send-order Bitable write",
    )
    written = _nonnegative_int(result.get("written"), "send-order Bitable written count")
    if written != len(records):
        raise ValueError("send-order Bitable written count changed")
    return written, [broker_evidence_ref(result, "send-order Bitable write")]


def _replace_projection(
    records: list[dict[str, object]],
    *,
    target_date: str,
    broker: Callable[..., object],
) -> tuple[int, int, int, list[str]]:
    result = _committed(
        broker(
            "projection.invoke",
            action="waybill.ronghui.replace_date",
            role=_ACCOUNT_ROLE,
            arguments={"records": records, "target_date": target_date},
        ),
        "Ronghui waybill projection",
    )
    upserted = _nonnegative_int(result.get("upserted", 0), "projection upserted count")
    updates = _nonnegative_int(result.get("updates", 0), "projection updates count")
    creates = _nonnegative_int(result.get("creates", 0), "projection creates count")
    deleted_stale = _nonnegative_int(
        result.get("deleted_stale", 0),
        "projection deleted-stale count",
    )
    if upserted != updates + creates:
        raise ValueError("Ronghui waybill projection counts are inconsistent")
    return (
        upserted,
        deleted_stale,
        updates,
        [broker_evidence_ref(result, "Ronghui waybill projection")],
    )


def _sync_date(
    *,
    target_date: str,
    page_size: int,
    max_pages: int,
    list_limit: int,
    list_max_pages: int,
    dry_run: bool,
    sql_only: bool,
    sync_sql: bool,
    broker: Callable[..., object],
) -> tuple[dict[str, object], list[str]]:
    raw_rows, evidence_refs, source_pages = _collect_source(
        target_date=target_date,
        page_size=page_size,
        max_pages=max_pages,
        broker=broker,
    )
    (
        bitable_records,
        projection_records,
        skipped_receipt_like,
        skipped_missing_waybill,
        source_duplicates,
    ) = _prepare_records(raw_rows, target_date=target_date)
    summary: dict[str, object] = {
        "target_date": target_date,
        "raw_fetched": len(raw_rows),
        "fetched": len(bitable_records),
        "skipped_receipt_like": skipped_receipt_like,
        "skipped_missing_waybill": skipped_missing_waybill,
        "source_duplicates": source_duplicates,
        "updates": 0,
        "creates": len(bitable_records),
        "written": 0,
        "deleted": 0,
        "dedup_deleted": 0,
        "sql_upserted": 0,
        "sql_updates": 0,
        "sql_creates": 0,
        "sql_deleted_stale": 0,
        "source_pages": source_pages,
        "bitable_pages": 0,
        "dry_run": dry_run,
        "sql_only": sql_only,
    }

    if sql_only:
        if dry_run:
            summary.update(
                {
                    "planned": 0,
                    "planned_updates": 0,
                    "planned_creates": 0,
                    "planned_deletes": 0,
                    "planned_sql_upserts": len(projection_records),
                    "planned_sql_deletes": len(projection_records),
                }
            )
            return summary, evidence_refs
        if sync_sql:
            upserted, deleted_stale, updates, refs = _replace_projection(
                projection_records,
                target_date=target_date,
                broker=broker,
            )
            evidence_refs.extend(refs)
            summary.update(
                {
                    "sql_upserted": upserted,
                    "sql_updates": updates,
                    "sql_creates": upserted - updates,
                    "sql_deleted_stale": deleted_stale,
                }
            )
        return summary, evidence_refs

    existing, refs, pages = _list_bitable_records(
        page_size=list_limit,
        max_pages=list_max_pages,
        fields=(_WAYBILL_FIELD, _DATE_FIELD),
        broker=broker,
    )
    evidence_refs.extend(refs)
    summary["bitable_pages"] = pages
    stale_refs, _existing_duplicates = _target_record_refs(
        existing,
        target_date=target_date,
    )
    if dry_run:
        summary.update(
            {
                "planned": len(bitable_records),
                "planned_updates": 0,
                "planned_creates": len(bitable_records),
                "planned_deletes": len(stale_refs),
                "planned_sql_upserts": len(projection_records),
                "planned_sql_deletes": len(stale_refs),
            }
        )
        return summary, evidence_refs

    deleted, refs = _delete_records(
        stale_refs,
        broker=broker,
        label="send-order target-date delete",
    )
    evidence_refs.extend(refs)
    summary["deleted"] = deleted
    written, refs = _write_records(bitable_records, broker=broker)
    evidence_refs.extend(refs)
    summary["written"] = written

    rescanned, refs, pages = _list_bitable_records(
        page_size=list_limit,
        max_pages=list_max_pages,
        fields=_BITABLE_FIELDS,
        broker=broker,
    )
    evidence_refs.extend(refs)
    summary["bitable_pages"] = int(summary["bitable_pages"]) + pages
    _current_refs, duplicate_refs = _target_record_refs(
        rescanned,
        target_date=target_date,
    )
    dedup_deleted, refs = _delete_records(
        duplicate_refs,
        broker=broker,
        label="send-order duplicate cleanup",
    )
    evidence_refs.extend(refs)
    summary["dedup_deleted"] = dedup_deleted

    verified_snapshot, refs, pages = _list_bitable_records(
        page_size=list_limit,
        max_pages=list_max_pages,
        fields=_BITABLE_FIELDS,
        broker=broker,
    )
    evidence_refs.extend(refs)
    summary["bitable_pages"] = int(summary["bitable_pages"]) + pages
    _verify_bitable_snapshot(
        verified_snapshot,
        bitable_records,
        target_date=target_date,
    )

    if sync_sql:
        upserted, deleted_stale, updates, refs = _replace_projection(
            projection_records,
            target_date=target_date,
            broker=broker,
        )
        evidence_refs.extend(refs)
        summary.update(
            {
                "sql_upserted": upserted,
                "sql_updates": updates,
                "sql_creates": upserted - updates,
                "sql_deleted_stale": deleted_stale,
            }
        )
    return summary, evidence_refs


def _release_lock(
    broker: Callable[..., object],
    lease_ref: str,
) -> tuple[dict[str, object], str]:
    result = _committed(
        broker(
            "ledger.invoke",
            action="sync_daily_send_orders.lock.release",
            role=_ACCOUNT_ROLE,
            arguments={"lease_ref": lease_ref},
        ),
        "send-order synchronization lock release",
    )
    if result.get("released") is not True:
        raise ValueError("send-order synchronization lock was not released")
    return result, broker_evidence_ref(result, "send-order synchronization lock release")


def run_action(
    arguments: dict[str, object],
    broker: Callable[..., object],
) -> dict[str, object]:
    values = _object(arguments, "arguments")
    if set(values) - _ALLOWED_ARGUMENTS:
        raise ValueError("send-order arguments contain undeclared fields")
    dry_run = _flag(values, "dry_run", False)
    sql_only = _flag(values, "sql_only", False)
    sync_sql = _flag(values, "sync_sql", True)
    page_size = _bounded_int(
        values.get("page_size"),
        default=_SOURCE_PAGE_SIZE,
        minimum=1,
        maximum=500,
        label="page_size",
    )
    max_pages = _bounded_int(
        values.get("max_pages"),
        default=_SOURCE_MAX_PAGES,
        minimum=1,
        maximum=_SOURCE_MAX_PAGES,
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
        # One call is always reserved for releasing the acquired synchronization
        # lease, including when pagination exhausts the signed budget.
        if broker_call_count >= _MAX_BROKER_CALLS - 1:
            raise ValueError("send-order broker call budget exhausted")
        broker_call_count += 1
        return broker(
            operation,
            action=action,
            role=role,
            arguments=arguments,
        )

    acquired = _object(
        bounded_broker(
            "ledger.invoke",
            action="sync_daily_send_orders.lock.acquire",
            role=_ACCOUNT_ROLE,
            arguments={},
        ),
        "send-order synchronization lock",
    )
    if acquired.get("acquired") is not True:
        raise ValueError("send-order synchronization lock was not acquired")
    lease_ref = _text(acquired.get("lease_ref"))
    if not lease_ref or len(lease_ref) > 512:
        raise ValueError("send-order synchronization lock has no lease reference")
    per_date: list[dict[str, object]] = []
    try:
        all_evidence = [broker_evidence_ref(acquired, "send-order synchronization lock")]
        for target_date in dates:
            summary, refs = _sync_date(
                target_date=target_date,
                page_size=page_size,
                max_pages=max_pages,
                list_limit=_BITABLE_PAGE_SIZE,
                list_max_pages=_BITABLE_MAX_PAGES,
                dry_run=dry_run,
                sql_only=sql_only,
                sync_sql=sync_sql,
                broker=bounded_broker,
            )
            per_date.append(summary)
            all_evidence.extend(refs)
    except BaseException:
        try:
            _release_lock(broker, lease_ref)
        except Exception as release_exc:
            raise RuntimeError("send-order synchronization failed and its lock could not be released") from release_exc
        raise
    _released, release_evidence = _release_lock(broker, lease_ref)
    broker_call_count += 1
    all_evidence.append(release_evidence)

    total_keys = (
        "raw_fetched",
        "fetched",
        "skipped_receipt_like",
        "skipped_missing_waybill",
        "source_duplicates",
        "updates",
        "creates",
        "written",
        "deleted",
        "dedup_deleted",
        "sql_upserted",
        "sql_updates",
        "sql_creates",
        "sql_deleted_stale",
        "source_pages",
        "bitable_pages",
    )
    data: dict[str, object] = {key: sum(int(summary.get(key) or 0) for summary in per_date) for key in total_keys}
    data.update(
        {
            "start_date": dates[0],
            "end_date": dates[-1],
            "days": len(dates),
            "dry_run": dry_run,
            "sql_only": sql_only,
            "sync_sql": sync_sql,
            "per_date": per_date,
        }
    )
    if len(dates) == 1:
        data["target_date"] = dates[0]
    if dry_run:
        for key in (
            "planned",
            "planned_updates",
            "planned_creates",
            "planned_deletes",
            "planned_sql_upserts",
            "planned_sql_deletes",
        ):
            data[key] = sum(int(summary.get(key) or 0) for summary in per_date)
    observed_at = datetime.now(_TIMEZONE).isoformat()
    if not dry_run and int(data["fetched"]) == 0 and (not sql_only or sync_sql):
        execution_result = "no_data_cleared"
    elif dry_run:
        execution_result = "dry_run_complete"
    elif sql_only and not sync_sql:
        execution_result = "source_read_complete"
    else:
        execution_result = "requested_sinks_committed"
    data["evidence"] = {
        "source": "signed_first_party_plugin",
        "observed_at": observed_at,
        "pagination_complete": True,
        "broker_call_count": broker_call_count,
        "execution_result": execution_result,
    }
    if len(set(all_evidence)) != len(all_evidence):
        raise ValueError("send-order broker evidence references are not unique")
    result_ref, result_proof = executor_success_evidence(
        action_id=ACTION_ID,
        data=data,
        observed_at=observed_at,
    )
    all_evidence.append(result_ref)
    if sql_only:
        source_system = "ronghui+internal_projection" if sync_sql and not dry_run else "ronghui"
    elif sync_sql and not dry_run:
        source_system = "ronghui+feishu+internal_projection"
    else:
        source_system = "ronghui+feishu"
    return success_result(
        data=data,
        source_system=source_system,
        record_count=int(data["fetched"]),
        pagination_complete=True,
        evidence_refs=all_evidence,
        observed_at=observed_at,
        postconditions={"0": True},
        postcondition_evidence={"0": result_proof},
    )
