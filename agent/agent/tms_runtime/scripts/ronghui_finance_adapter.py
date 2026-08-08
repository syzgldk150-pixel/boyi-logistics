"""Ronghui finance response adapter using page-discovered field bindings."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Callable, Mapping

from agent.tms_runtime.scripts.finance_capture_common import (
    CaptureResult,
    FinanceCaptureError,
    amount_storage_text,
    clean_text,
    decimal_extrema,
    filter_target_date,
    paginate_by_source_key,
    require_row_keys,
    validate_normalized_summaries,
    validate_page_identity,
    whitelist_record,
)


RONGHUI_FINANCE_ENDPOINT = "/dataQuery/findPageByCallId"
RONGHUI_DETAIL_CALL_ID = "FIND_BALANCE_QRY_WST_WITH_SITE"
RONGHUI_SUMMARY_CALL_ID = "FIND_BALANCE_QRY_TJ_WST"
RONGHUI_DRILLDOWN_CALL_ID = "FIND_BALANCE_QRY_TJ_DETAIL"
RONGHUI_SOURCE_KEY = "GUID"
RONGHUI_SETTLEMENT_DATE_KEY = "BALANCE_DATE"

RONGHUI_REQUIRED_BINDINGS = (
    "trade_time",
    "fee_name",
    "amount",
    "old_amount",
    "new_amount",
    "balance_order",
    "bill_code",
)
RONGHUI_OPTIONAL_BINDINGS = (
    "bill_time",
    "waybill_no",
    "business_code",
    "source_site_code",
    "source_site_name",
    "remark",
)

RONGHUI_SOURCE_PAYLOAD_FIELDS = (
    "BALANCE_ORDER",
    "BILL_CODE",
    "FINANCE_DATE",
    "BALANCE_DATE",
    "BALANCE_TYPE",
    "CENTER_NAME",
    "SITE_NAME",
    "QUANTITY",
    "SETTLEMENT_WEIGHT",
    "SOURCE",
)


def _binding(field_bindings: Mapping[str, str], name: str, *, required: bool = False) -> str:
    value = clean_text(field_bindings.get(name))
    if required and not value:
        raise FinanceCaptureError("FIELD_DRIFT", f"融辉页面缺少字段绑定：{name}", stage="field_binding")
    return value


def normalize_ronghui_row(
    row: Mapping[str, Any],
    *,
    field_bindings: Mapping[str, str],
    account_id: str,
    source_site_code: str,
    source_site_name: str,
) -> dict[str, Any]:
    for name in RONGHUI_REQUIRED_BINDINGS:
        _binding(field_bindings, name, required=True)
    if _binding(field_bindings, "trade_time", required=True) != RONGHUI_SETTLEMENT_DATE_KEY:
        raise FinanceCaptureError(
            "FIELD_DRIFT",
            "融辉结算日期字段必须是 BALANCE_DATE",
            stage="field_binding",
        )
    required_source_keys = [RONGHUI_SOURCE_KEY] + [field_bindings[name] for name in RONGHUI_REQUIRED_BINDINGS]
    require_row_keys(row, required_source_keys, stage="ronghui_normalize")

    amount = amount_storage_text(row[field_bindings["amount"]], field="amount")
    amount_decimal = Decimal(amount)
    if amount_decimal == 0:
        raise FinanceCaptureError(
            "AMOUNT_DIRECTION_INVALID",
            "融辉财务行金额为零，无法确定收支方向",
            stage="ronghui_normalize",
        )
    zero = amount_storage_text(Decimal("0.0000"), field="direction_zero")
    record: dict[str, Any] = {
        "platform": "ronghui",
        "account_id": account_id,
        "source_id": clean_text(row[RONGHUI_SOURCE_KEY]),
        "trade_time": row[field_bindings["trade_time"]],
        "fee_name": clean_text(row[field_bindings["fee_name"]]),
        "amount": amount,
        "income": amount if amount_decimal > 0 else zero,
        "expend": amount_storage_text(-amount_decimal, field="expend") if amount_decimal < 0 else zero,
        "source_site_code": source_site_code,
        "source_site_name": source_site_name,
        "old_amount": amount_storage_text(row[field_bindings["old_amount"]], field="old_amount"),
        "new_amount": amount_storage_text(row[field_bindings["new_amount"]], field="new_amount"),
        "source_reference": clean_text(row[field_bindings["balance_order"]]),
        "balance_order": clean_text(row[field_bindings["balance_order"]]),
        "bill_code": clean_text(row[field_bindings["bill_code"]]),
    }
    record["source_payload"] = {
        key: row[key]
        for key in RONGHUI_SOURCE_PAYLOAD_FIELDS
        if key in row
    }
    record["source_payload"].update(
        {
            "BALANCE_ORDER": row[field_bindings["balance_order"]],
            "BILL_CODE": row[field_bindings["bill_code"]],
            "BALANCE_DATE": row[field_bindings["trade_time"]],
        }
    )
    for name in RONGHUI_OPTIONAL_BINDINGS:
        source_key = _binding(field_bindings, name)
        if source_key and source_key in row:
            target = name
            record[target] = clean_text(row[source_key])
    if not record["source_id"]:
        raise FinanceCaptureError("STABLE_KEY_MISSING", "融辉财务行 GUID 为空", stage="ronghui_normalize")
    if not record["fee_name"]:
        raise FinanceCaptureError("FIELD_DRIFT", "融辉财务行结算类型为空", stage="ronghui_normalize")
    if not record["source_reference"]:
        raise FinanceCaptureError("FIELD_DRIFT", "融辉财务行 BALANCE_ORDER 为空", stage="ronghui_normalize")
    return whitelist_record(record)


def capture_ronghui_day(
    *,
    account_id: str,
    target_date: dt.date,
    field_bindings: Mapping[str, str],
    source_site_code: str,
    source_site_name: str,
    login_site_code: str,
    account_match: bool,
    fetch_detail_page: Callable[[int, int], Any],
    summary_rows: list[dict[str, Any]] | None = None,
    page_size: int = 100,
    max_pages: int = 200,
) -> CaptureResult:
    if not clean_text(source_site_name):
        raise FinanceCaptureError("SOURCE_SITE_MISSING", "融辉财务页面未解析到当前网点", stage="page_discovery")
    validate_page_identity(
        platform="融辉",
        account_match=account_match,
        login_site_code=login_site_code,
        source_site_code=source_site_code,
    )
    batch = paginate_by_source_key(
        fetch_detail_page,
        source_key=RONGHUI_SOURCE_KEY,
        page_size=page_size,
        max_pages=max_pages,
        stage="ronghui_detail",
    )
    normalized = [
        normalize_ronghui_row(
            row,
            field_bindings=field_bindings,
            account_id=account_id,
            source_site_code=source_site_code,
            source_site_name=source_site_name,
        )
        for row in batch.rows
    ]
    transactions, excluded = filter_target_date(normalized, target_date=target_date)
    summaries = validate_normalized_summaries(summary_rows or [])
    return CaptureResult(
        transactions=transactions,
        summaries=summaries,
        source_site_code=source_site_code,
        source_site_name=source_site_name,
        validation={
            "source_total": batch.total,
            "page_row_counts": list(batch.page_row_counts),
            "page_row_count": batch.page_row_count,
            "unique_count": len(batch.rows),
            "accepted_rows": len(transactions),
            "excluded_other_dates": excluded,
            "duplicate_page_rows": batch.duplicate_rows,
            "pages": batch.pages,
            "amount_extrema": decimal_extrema(transactions, ("amount", "income", "expend")),
        },
    )
