"""Ronghui finance response adapter using page-discovered field bindings."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Mapping

from shared.finance.models import SummarySemantics

from agent.tms_runtime.scripts.finance_capture_common import (
    CaptureResult,
    FinanceCaptureError,
    amount_storage_text,
    clean_text,
    decimal_extrema,
    filter_target_date,
    paginate_by_source_key,
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

from first_party_automation_plugins.sync_finance_bills.payload.finance_fields import (
    RONGHUI_REQUIRED_BINDINGS as RONGHUI_REQUIRED_BINDINGS,
    RONGHUI_OPTIONAL_BINDINGS as RONGHUI_OPTIONAL_BINDINGS,
    RONGHUI_SOURCE_PAYLOAD_FIELDS as RONGHUI_SOURCE_PAYLOAD_FIELDS,
    FinanceFieldError, normalize_ronghui_row as _normalize_source_row,
)


def normalize_ronghui_row(row: Mapping[str, Any], *, field_bindings: Mapping[str, str],
                          account_id: str, source_site_code: str, source_site_name: str) -> dict[str, Any]:
    try:
        return whitelist_record(_normalize_source_row(row, field_bindings=field_bindings,
            account_id=account_id, source_site_code=source_site_code, source_site_name=source_site_name,
            amount_storage_text=amount_storage_text))
    except FinanceFieldError as exc:
        raise FinanceCaptureError(exc.code, str(exc), stage=exc.stage) from exc


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
        summary_semantics=SummarySemantics.SIGNED_NET_BY_FEE,
    )
