"""Verify and project a raw platform statement capture.

A capture is the statement query (统计查询) of one source over a period plus
every detail row (明细数据) of that period, as the platform returned them.
The collector plugin declares where rows, totals and fields live; this module
knows no platform page. It proves the pages are complete and consistent, that
the details close the statement item by item, and that the balance chain is
unbroken, then projects canonical fields while keeping each raw row as is.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from shared.finance.money import ZERO, quantize_storage


DETAIL_FIELDS = (
    "source_key", "settled_at", "billed_at", "item_name", "amount", "before_balance",
    "after_balance", "waybill_no", "order_no", "site_code", "site_name", "source_type", "remark",
    "customer_name", "goods_name",
)
STATEMENT_FIELDS = ("item_name", "amount", "site_code", "site_name")
PROFILE_FIELDS = ("rows_pointer", "total_pointer", "page_fields", "site_field", "settlement_period_field",
                  "billing_period_field", "waybill_number_shapes")
MAX_PERIOD_DAYS = 31
# A declared waybill number pattern: ``R+11`` (letters then 11 digits), ``10``
# (10 digits) or ``200*`` (digits starting with 200, any length).
_WAYBILL_PATTERN = re.compile(r"(?:[A-Z]{1,4}\+)?[1-9][0-9]?|[0-9]{1,8}\*")
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


class PlatformCaptureError(ValueError):
    """The capture is incomplete or inconsistent; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _pointer(value: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 256:
        raise PlatformCaptureError("PROFILE_INVALID", "pointer must be a JSON pointer")
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PlatformCaptureError("PAGE_SHAPE_DRIFT", f"response has no {pointer}") from exc
    return value


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def number_shape(value: Any) -> str:
    """Letters prefix plus digit count, e.g. ``R+11`` or ``10``; ``""`` when empty."""

    text = _text(value)
    if not text:
        return ""
    match = re.fullmatch(r"([A-Za-z]{0,4})([0-9]{1,99})", text)
    if match is None:
        return "other"
    prefix, digits = match.groups()
    return f"{prefix.upper()}+{len(digits)}" if prefix else str(len(digits))


def waybill_pattern_match(value: Any, patterns: Sequence[str]) -> str | None:
    """The first declared waybill pattern the number matches, if any."""

    text = _text(value)
    for pattern in patterns:
        if pattern.endswith("*"):
            if re.fullmatch(r"[0-9]+", text) and text.startswith(pattern[:-1]):
                return pattern
        elif text and number_shape(text) == pattern:
            return pattern
    return None


def shape_label(shape: str) -> str:
    if not shape:
        return "无单号"
    if shape == "other":
        return "其他格式"
    if shape.endswith("*"):
        return f"{shape[:-1]}开头"
    prefix, _, digits = shape.rpartition("+")
    return f"{prefix}+{digits}位数字" if prefix else f"{digits}位数字"


def _amount(value: Any, label: str) -> Decimal:
    if value is None or isinstance(value, bool) or _text(value) == "":
        raise PlatformCaptureError("AMOUNT_MISSING", f"{label} is missing")
    try:
        amount = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise PlatformCaptureError("AMOUNT_INVALID", f"{label} is not a decimal") from exc
    if not amount.is_finite():
        raise PlatformCaptureError("AMOUNT_INVALID", f"{label} is not finite")
    return quantize_storage(amount)


def _optional_amount(value: Any, label: str) -> Decimal | None:
    if value is None or _text(value) == "":
        return None
    return _amount(value, label)


def _datetime(value: Any, label: str, *, required: bool) -> dt.datetime | None:
    text = _text(value)
    if not text:
        if required:
            raise PlatformCaptureError("DATE_MISSING", f"{label} is missing")
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise PlatformCaptureError("DATE_INVALID", f"{label} is not a date and time")


def _mapping(value: Any, fields: Sequence[str], label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise PlatformCaptureError("MAPPING_INVALID", f"{label} must map exactly {', '.join(fields)}")
    result = {}
    for key in fields:
        source = value[key]
        if not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", source):
            raise PlatformCaptureError("MAPPING_INVALID", f"{label}.{key} must name one source field")
        result[key] = source
    return result


def validate_field_mapping(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping) or set(value) != {"detail", "statement"}:
        raise PlatformCaptureError("MAPPING_INVALID", "field mapping needs detail and statement")
    return {
        "detail": _mapping(value["detail"], DETAIL_FIELDS, "detail"),
        "statement": _mapping(value["statement"], STATEMENT_FIELDS, "statement"),
    }


def validate_request_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(PROFILE_FIELDS):
        raise PlatformCaptureError("PROFILE_INVALID", "request profile fields are invalid")
    page_fields = value["page_fields"]
    if (not isinstance(page_fields, list) or not 1 <= len(page_fields) <= 4
            or any(not isinstance(item, str) or not item for item in page_fields)):
        raise PlatformCaptureError("PROFILE_INVALID", "page fields are invalid")
    for key in ("rows_pointer", "total_pointer"):
        if not isinstance(value[key], str) or not value[key].startswith("/"):
            raise PlatformCaptureError("PROFILE_INVALID", f"{key} must be a JSON pointer")
    for key in ("site_field", "settlement_period_field", "billing_period_field"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value[key]):
            raise PlatformCaptureError("PROFILE_INVALID", f"{key} must name one request field")
    # Which number shapes the platform uses for waybills. Only a hint for the
    # person classifying fee items; it never classifies anything by itself.
    shapes = value["waybill_number_shapes"]
    if (not isinstance(shapes, list) or not 1 <= len(shapes) <= 8 or len(set(shapes)) != len(shapes)
            or any(not isinstance(item, str) or not _WAYBILL_PATTERN.fullmatch(item) for item in shapes)):
        raise PlatformCaptureError("PROFILE_INVALID", "waybill number shapes are invalid")
    return {**{key: value[key] for key in PROFILE_FIELDS}, "page_fields": list(page_fields),
            "waybill_number_shapes": list(shapes)}


def _period_mentions(text: str, day: dt.date) -> bool:
    return day.strftime("%Y/%m/%d") in text or day.isoformat() in text


def _page_set(
    pages: Sequence[Mapping[str, Any]],
    *,
    profile: Mapping[str, Any],
    period_field: str,
    period_start: dt.date,
    period_end: dt.date,
    site_code: str,
    label: str,
) -> list[dict[str, Any]]:
    """All pages of one query: same request apart from paging, complete rows."""

    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)) or not pages:
        raise PlatformCaptureError("PAGES_MISSING", f"{label} has no pages")
    first_body = None
    total = None
    rows: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, Mapping) or not isinstance(page.get("body"), Mapping):
            raise PlatformCaptureError("PAGE_SHAPE_DRIFT", f"{label} page is invalid")
        body = dict(page["body"])
        fixed = {key: value for key, value in body.items() if key not in profile["page_fields"]}
        if first_body is None:
            first_body = fixed
            if _text(body.get(profile["site_field"])) != site_code:
                raise PlatformCaptureError("SITE_MISMATCH", f"{label} queried another site")
            period_text = _text(body.get(period_field))
            if not (_period_mentions(period_text, period_start) and _period_mentions(period_text, period_end)):
                raise PlatformCaptureError("PERIOD_MISMATCH", f"{label} did not query the declared period")
        elif fixed != first_body:
            raise PlatformCaptureError("PAGINATION_DRIFT", f"{label} pages changed their query")
        page_rows = _pointer(page.get("response"), profile["rows_pointer"])
        page_total = _pointer(page.get("response"), profile["total_pointer"])
        if not isinstance(page_rows, list) or any(not isinstance(row, Mapping) for row in page_rows):
            raise PlatformCaptureError("PAGE_SHAPE_DRIFT", f"{label} rows are invalid")
        try:
            page_total = int(str(page_total))
        except ValueError as exc:
            raise PlatformCaptureError("PAGE_SHAPE_DRIFT", f"{label} total is not an integer") from exc
        if total is None:
            total = page_total
        elif page_total != total:
            raise PlatformCaptureError("PAGINATION_TOTAL_DRIFT", f"{label} total changed between pages")
        rows.extend(dict(row) for row in page_rows)
    if total != len(rows):
        raise PlatformCaptureError("PAGINATION_INCOMPLETE", f"{label} returned {len(rows)} of {total} rows")
    return rows


def _statement(rows: list[dict[str, Any]], mapping: Mapping[str, str], site_code: str,
               basis: str) -> list[dict[str, Any]]:
    result = []
    for number, raw in enumerate(rows, start=1):
        item_name = _text(raw.get(mapping["item_name"]))
        if not item_name:
            raise PlatformCaptureError("FIELD_MISSING", "statement row has no item name")
        if _text(raw.get(mapping["site_code"])) != site_code:
            raise PlatformCaptureError("SITE_MISMATCH", "statement row belongs to another site")
        result.append({
            "basis": basis, "row_no": number, "item_name": item_name, "site_code": site_code,
            "site_name": _text(raw.get(mapping["site_name"])),
            "amount": _amount(raw.get(mapping["amount"]), "statement amount"), "raw": raw,
        })
    return result


def _order_key(value: str) -> tuple[int, int, str]:
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


@dataclass(frozen=True)
class PlatformCapture:
    site_code: str
    site_name: str
    period_start: dt.date
    period_end: dt.date
    statement_rows: tuple[dict[str, Any], ...]
    billing_statement_rows: tuple[dict[str, Any], ...]
    detail_rows: tuple[dict[str, Any], ...]
    metrics: Mapping[str, Any]
    content_sha256: str


def build_platform_capture(
    *,
    site_code: str,
    period_start: dt.date,
    period_end: dt.date,
    field_mapping: Any,
    request_profile: Any,
    statement_pages: Sequence[Mapping[str, Any]],
    billing_statement_pages: Sequence[Mapping[str, Any]],
    detail_pages: Sequence[Mapping[str, Any]],
) -> PlatformCapture:
    site_code = _text(site_code)
    if not site_code:
        raise PlatformCaptureError("SITE_MISSING", "the verified site code is missing")
    if period_end < period_start or (period_end - period_start).days >= MAX_PERIOD_DAYS:
        raise PlatformCaptureError("PERIOD_INVALID", f"a capture covers 1 to {MAX_PERIOD_DAYS} days")
    mapping = validate_field_mapping(field_mapping)
    profile = validate_request_profile(request_profile)
    common = {"profile": profile, "period_start": period_start, "period_end": period_end, "site_code": site_code}
    statement_raw = _page_set(statement_pages, period_field=profile["settlement_period_field"],
                              label="settlement statement", **common)
    billing_raw = _page_set(billing_statement_pages, period_field=profile["billing_period_field"],
                            label="billing statement", **common) if billing_statement_pages else []
    detail_raw = _page_set(detail_pages, period_field=profile["settlement_period_field"],
                           label="detail", **common)
    statement = _statement(statement_raw, mapping["statement"], site_code, "settlement")
    billing = _statement(billing_raw, mapping["statement"], site_code, "billing")

    fields = mapping["detail"]
    details: list[dict[str, Any]] = []
    for number, raw in enumerate(detail_raw, start=1):
        source_key = _text(raw.get(fields["source_key"]))
        item_name = _text(raw.get(fields["item_name"]))
        if not source_key or not item_name:
            raise PlatformCaptureError("FIELD_MISSING", "detail row has no source key or item name")
        if _text(raw.get(fields["site_code"])) != site_code:
            raise PlatformCaptureError("SITE_MISMATCH", "detail row belongs to another site")
        settled_at = _datetime(raw.get(fields["settled_at"]), "settled_at", required=True)
        if not period_start <= settled_at.date() <= period_end:
            raise PlatformCaptureError("PERIOD_MISMATCH", "detail row is outside the declared period")
        amount = _amount(raw.get(fields["amount"]), "detail amount")
        before = _optional_amount(raw.get(fields["before_balance"]), "before balance")
        after = _optional_amount(raw.get(fields["after_balance"]), "after balance")
        if (before is None) != (after is None):
            raise PlatformCaptureError("BALANCE_FIELD_INCOMPLETE", "before and after balance must both exist")
        if before is not None and quantize_storage(before + amount) != after:
            raise PlatformCaptureError("BALANCE_EQUATION_MISMATCH", "before balance plus amount is not after balance")
        details.append({
            "row_no": number, "source_key": source_key, "item_name": item_name,
            "order_no": _text(raw.get(fields["order_no"])) or None,
            "waybill_no": _text(raw.get(fields["waybill_no"])) or None,
            "settled_at": settled_at,
            "billed_at": _datetime(raw.get(fields["billed_at"]), "billed_at", required=False),
            "amount": amount, "before_balance": before, "after_balance": after,
            "site_name": _text(raw.get(fields["site_name"])),
            "source_type": _text(raw.get(fields["source_type"]))[:64] or None,
            "remark": _text(raw.get(fields["remark"]))[:1000] or None,
            "customer_name": _text(raw.get(fields["customer_name"]))[:255] or None,
            "goods_name": _text(raw.get(fields["goods_name"]))[:255] or None, "raw": raw,
        })
    keys = Counter(row["source_key"] for row in details)
    if any(count > 1 for count in keys.values()):
        raise PlatformCaptureError("DUPLICATE_SOURCE_KEY", "detail source keys are duplicated")

    chain = sorted((row for row in details if row["before_balance"] is not None),
                   key=lambda row: (row["settled_at"], _order_key(row["order_no"] or "")))
    breaks = sum(1 for previous, current in zip(chain, chain[1:])
                 if previous["after_balance"] != current["before_balance"])
    if breaks:
        raise PlatformCaptureError("BALANCE_CHAIN_BREAK", f"balance chain breaks {breaks} times")

    detail_by_item: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for row in details:
        detail_by_item[row["item_name"]] += row["amount"]
    statement_by_item: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for row in statement:
        statement_by_item[row["item_name"]] += row["amount"]
    mismatched = sorted(name for name in set(detail_by_item) | set(statement_by_item)
                        if detail_by_item.get(name, ZERO) != statement_by_item.get(name, ZERO))
    if mismatched:
        raise PlatformCaptureError("STATEMENT_MISMATCH", "details differ from the statement for: "
                                   + "、".join(mismatched[:10]))
    site_names = {row["site_name"] for row in statement + details if row["site_name"]}
    if len(site_names) > 1:
        raise PlatformCaptureError("SITE_MISMATCH", "rows carry more than one site name")

    income = sum((row["amount"] for row in details if row["amount"] > 0), ZERO)
    expense = -sum((row["amount"] for row in details if row["amount"] < 0), ZERO)
    metrics = {
        "statement_item_count": len(statement),
        "statement_net": sum((row["amount"] for row in statement), ZERO),
        "billing_statement_item_count": len(billing) if billing_statement_pages else None,
        "billing_statement_net": sum((row["amount"] for row in billing), ZERO) if billing_statement_pages else None,
        "detail_row_count": len(details),
        "detail_income": income,
        "detail_expense": expense,
        "detail_net": income - expense,
    }
    content = _sha256({
        "site_code": site_code, "period": [period_start.isoformat(), period_end.isoformat()],
        "statement": statement_raw, "billing_statement": billing_raw, "details": detail_raw,
    })
    return PlatformCapture(
        site_code=site_code, site_name=next(iter(site_names), ""), period_start=period_start,
        period_end=period_end, statement_rows=tuple(statement), billing_statement_rows=tuple(billing),
        detail_rows=tuple(details), metrics=metrics, content_sha256=content,
    )
