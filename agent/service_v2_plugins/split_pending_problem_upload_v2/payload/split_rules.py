"""Single package-owned classification shared by arrival and problem-upload ZIPs."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

_TARGET_HEADERS = (
    "运单编号",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "回单号",
    "实际重量",
    "体积",
    "备注",
    "目的站点",
    "收件人",
    "收件电话",
    "收件地址",
    "结算重量",
    "体积重",
    "运费",
    "支付类型",
    "到付款",
    "累计到货件数",
)


_SOURCE_FIRST_HEADERS = frozenset({"运单编号", "单号"})


_SOURCE_LAST_HEADERS = frozenset({"累计到货件数", "已到货件数", "到货件数"})


def _text(value: object, label: str, *, maximum: int = 256) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (Mapping, list, tuple, set)):
        raise ValueError(f"{label} is invalid")
    result = str(value).strip()
    if result.startswith("="):
        result = result[1:].strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {"'", '"'}:
        result = result[1:-1].strip()
    if result.endswith(".0") and result[:-2].isdigit():
        result = result[:-2]
    if len(result) > maximum:
        raise ValueError(f"{label} is too long")
    return result


def _waybill(value: object) -> str:
    result = _text(value, "waybill", maximum=128)
    if any(character.isspace() for character in result):
        raise ValueError("waybill contains whitespace")
    return result


def _integer(value: object, label: str) -> int:
    raw = _text(value, label, maximum=64).replace(",", "")
    if not raw:
        raise ValueError(f"{label} is empty")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not an integer") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"{label} is not an integer")
    return int(number)


def _validate_headers(headers: list[object]) -> None:
    normalized = [_text(value, "source header", maximum=64) for value in headers]
    if normalized[0] not in _SOURCE_FIRST_HEADERS:
        raise ValueError("split source first column is not a waybill")
    for index, expected in enumerate(_TARGET_HEADERS[1:18], start=1):
        if normalized[index] != expected:
            raise ValueError(f"split source column {index + 1} must be {expected}")
    if normalized[18] not in _SOURCE_LAST_HEADERS:
        raise ValueError("split source last column is not an arrival count")


def _classify(rows: list[list[object]]) -> tuple[list[dict[str, object]], int]:
    if not rows:
        raise ValueError("split source has no header row")
    _validate_headers(rows[0])
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    source_rows = 0
    for row_number, row in enumerate(rows[1:], start=2):
        if not any(_text(value, "source cell", maximum=1024) for value in row):
            continue
        bill_code = _waybill(row[0])
        if not bill_code:
            raise ValueError(f"split source row {row_number} has no waybill")
        if bill_code in seen:
            raise ValueError(f"split source contains duplicate waybill {bill_code}")
        seen.add(bill_code)
        source_rows += 1
        expected = _integer(row[4], f"{bill_code} expected quantity")
        arrived = _integer(row[18], f"{bill_code} arrived quantity")
        if expected <= 0 or arrived < 0 or arrived > expected:
            raise ValueError(f"{bill_code} has an invalid arrival quantity")
        if arrived == expected:
            continue
        if arrived == 0:
            problem_type = "有发未到"
            owner = "通知类（不顺延时效）"
            cause = "有发未到"
        else:
            problem_type = "少货/分批"
            owner = "交接异常"
            cause = f"应到{expected}件 实际到{arrived}件"
        sheet_values = [_text(value, f"{bill_code} source cell", maximum=1024) for value in row[:18]] + [arrived]
        candidates.append(
            {
                "bill_code": bill_code,
                "source_row_no": row_number,
                "destination_station": _text(row[9], f"{bill_code} destination", maximum=256),
                "expected_quantity": expected,
                "arrived_quantity": arrived,
                "pending_quantity": expected - arrived,
                "problem_type": problem_type,
                "problem_owner_type": owner,
                "problem_cause": cause,
                "problem_cause_sha256": hashlib.sha256(cause.encode("utf-8")).hexdigest(),
                "sheet_values": sheet_values,
            }
        )
    return candidates, source_rows


_ROW_FIELDS = (
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
    "arrived_quantity",
)
_NUMERIC_COLUMNS = frozenset({4, 6, 7, 13, 14, 15, 17, 18})


def arrival_split_projection(records: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[list[object]]]:
    """Classify the current computed statistics; retain the original numeric cells."""
    values = [list(_TARGET_HEADERS)]
    for record in records:
        row = []
        for index, field in enumerate(_ROW_FIELDS):
            value = record.get(field)
            if index in _NUMERIC_COLUMNS and value not in (None, "", "null"):
                number = Decimal(str(value))
                if not number.is_finite():
                    raise ValueError(f"{field} is not finite")
                value = int(number) if number == number.to_integral_value() else float(number)
            row.append("" if value is None else value)
        values.append(row)
    candidates, _ = _classify(values)
    projection = [
        {
            "tracking_number": item["bill_code"],
            **{
                key: value
                for key, value in item.items()
                if key not in {"bill_code", "sheet_values", "problem_cause_sha256"}
            },
        }
        for item in candidates
    ]
    rows = [list(_TARGET_HEADERS), *[values[item["source_row_no"] - 1] for item in candidates]]
    return projection, rows
