"""Yunda finance response adapter using dynamically discovered field names."""

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
    whitelist_record,
)


YUNDA_DYNAMIC_ENDPOINT_NAMES = (
    "selectDynamicFileds",
    "selectFiledsData",
    "selectInterface",
)
YUNDA_SOURCE_KEY = "id"
YUNDA_REQUIRED_BINDINGS = (
    "trade_time",
    "fee_level_1",
    "fee_level_2",
    "income",
    "expend",
    "old_amount",
    "new_amount",
    "logistics_id",
    "source_reference",
)
YUNDA_OPTIONAL_BINDINGS = (
    "waybill_no",
    "business_code",
    "remark",
)

YUNDA_SOURCE_PAYLOAD_FIELDS = (
    "serial_no",
    "logistics_Id",
    "trade_time_ex",
    "buz_date",
    "trade_type",
    "trade_amount",
    "project_id",
    "project_name",
    "project_name_s",
    "sub_project_name",
    "parent_project_name",
    "account_no",
    "account_type",
    "charge_org_name",
    "branch_name",
    "trade_code",
    "business_code",
    "data_source",
)


def validate_yunda_dynamic_context(context: Mapping[str, Any]) -> None:
    endpoints = context.get("dynamic_endpoints")
    if not isinstance(endpoints, Mapping):
        raise FinanceCaptureError("FIELD_DRIFT", "韵达财务页面缺少动态接口信息", stage="page_discovery")
    missing = [name for name in YUNDA_DYNAMIC_ENDPOINT_NAMES if not clean_text(endpoints.get(name))]
    if missing:
        raise FinanceCaptureError("FIELD_DRIFT", "韵达财务页面动态接口发生变化", stage="page_discovery")
    if context.get("account_match") is not True:
        raise FinanceCaptureError("ACCOUNT_PAGE_MISMATCH", "韵达财务页面与账号管理登录账号不匹配", stage="page_discovery")
    source_site_code = clean_text(context.get("source_site_code"))
    source_site_name = clean_text(context.get("source_site_name"))
    if bool(source_site_code) != bool(source_site_name):
        raise FinanceCaptureError("SOURCE_SITE_MISSING", "韵达开户网点编码和名称必须同时存在", stage="page_discovery")
    if source_site_code and context.get("source_site_verified") is not True:
        raise FinanceCaptureError("SOURCE_SITE_MISMATCH", "韵达开户网点尚未由原页唯一验证", stage="page_discovery")


def normalize_yunda_row(
    row: Mapping[str, Any],
    *,
    field_bindings: Mapping[str, str],
    account_id: str,
    source_site_code: str,
    source_site_name: str,
) -> dict[str, Any]:
    resolved_bindings = {
        name: clean_text(field_bindings.get(name))
        for name in YUNDA_REQUIRED_BINDINGS
    }
    if any(not value for value in resolved_bindings.values()):
        raise FinanceCaptureError("FIELD_DRIFT", "韵达页面动态字段绑定不完整", stage="field_binding")
    required = [YUNDA_SOURCE_KEY, *resolved_bindings.values()]
    require_row_keys(row, required, stage="yunda_normalize")
    source_id = clean_text(row[YUNDA_SOURCE_KEY])
    if not source_id:
        raise FinanceCaptureError("STABLE_KEY_MISSING", "韵达财务行 id 为空", stage="yunda_normalize")
    income_source = row[resolved_bindings["income"]]
    expend_source = row[resolved_bindings["expend"]]
    income = amount_storage_text(
        Decimal("0.0000") if not clean_text(income_source) else income_source,
        field="income",
    )
    expend = amount_storage_text(
        Decimal("0.0000") if not clean_text(expend_source) else expend_source,
        field="expend",
    )
    income_value = Decimal(income)
    expend_value = Decimal(expend)
    if income_value < 0 or expend_value < 0 or (income_value == 0) == (expend_value == 0):
        raise FinanceCaptureError(
            "AMOUNT_DIRECTION_INVALID",
            "韵达财务行收入/支出方向不唯一",
            stage="yunda_normalize",
        )
    record = {
        "platform": "yunda",
        "account_id": account_id,
        "source_id": source_id,
        "trade_time": row[resolved_bindings["trade_time"]],
        "fee_level_1": clean_text(row[resolved_bindings["fee_level_1"]]),
        "fee_level_2": clean_text(row[resolved_bindings["fee_level_2"]]),
        "fee_name": clean_text(row[resolved_bindings["fee_level_2"]]) or clean_text(row[resolved_bindings["fee_level_1"]]),
        "income": income,
        "expend": expend,
        "old_amount": amount_storage_text(row[resolved_bindings["old_amount"]], field="old_amount"),
        "new_amount": amount_storage_text(row[resolved_bindings["new_amount"]], field="new_amount"),
        "logistics_id": clean_text(row[resolved_bindings["logistics_id"]]),
        "source_reference": clean_text(row[resolved_bindings["source_reference"]]),
        "source_site_code": source_site_code,
        "source_site_name": source_site_name,
        "source_payload": {
            key: row[key]
            for key in YUNDA_SOURCE_PAYLOAD_FIELDS
            if key in row
        },
    }
    if not record["source_reference"]:
        raise FinanceCaptureError("FIELD_DRIFT", "韵达财务行 serial_no 为空", stage="yunda_normalize")
    record["source_payload"].update(
        {
            "serial_no": row[resolved_bindings["source_reference"]],
            "logistics_Id": row[resolved_bindings["logistics_id"]],
        }
    )
    for target_name in YUNDA_OPTIONAL_BINDINGS:
        source_key = clean_text(field_bindings.get(target_name))
        if source_key and source_key in row:
            record[target_name] = clean_text(row[source_key])
            if target_name == "business_code":
                record["source_payload"]["business_code"] = row[source_key]
    return whitelist_record(record)


def capture_yunda_day(
    *,
    account_id: str,
    target_date: dt.date,
    context: Mapping[str, Any],
    field_bindings: Mapping[str, str],
    fetch_detail_page: Callable[[int, int], Any],
    summary_rows: list[dict[str, Any]] | None = None,
    page_size: int = 100,
    max_pages: int = 200,
) -> CaptureResult:
    validate_yunda_dynamic_context(context)
    source_site_code = clean_text(context["source_site_code"])
    source_site_name = clean_text(context["source_site_name"])
    batch = paginate_by_source_key(
        fetch_detail_page,
        source_key=YUNDA_SOURCE_KEY,
        page_size=page_size,
        max_pages=max_pages,
        stage="yunda_detail",
    )
    normalized = [
        normalize_yunda_row(
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
            "amount_extrema": decimal_extrema(transactions, ("income", "expend", "old_amount", "new_amount")),
        },
    )
