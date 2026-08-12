"""Shared, credential-free contracts for finance source capture.

Live adapters supply page-discovered field bindings and a page fetch callback.
This module only validates JSON payloads, paginates by a stable source key, and
normalizes financial values without guessing source fields.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class FinanceCaptureError(RuntimeError):
    """Traceable source failure which never contains auth material or row data."""

    def __init__(self, code: str, message: str, *, stage: str = "") -> None:
        super().__init__(message)
        self.code = str(code or "FINANCE_CAPTURE_FAILED")
        self.stage = str(stage or "")


@dataclass(frozen=True)
class PageBatch:
    rows: list[dict[str, Any]]
    total: int | None
    pages: int
    page_row_counts: tuple[int, ...]
    page_row_count: int
    duplicate_rows: int


@dataclass(frozen=True)
class CaptureResult:
    transactions: list[dict[str, Any]]
    summaries: list[dict[str, Any]]
    source_site_code: str
    source_site_name: str
    validation: dict[str, Any]


TRANSACTION_WHITELIST = frozenset(
    {
        "platform",
        "account_id",
        "source_id",
        "target_date",
        "trade_time",
        "bill_time",
        "waybill_no",
        "bill_code",
        "balance_order",
        "business_code",
        "source_reference",
        "fee_level_1",
        "fee_level_2",
        "fee_name",
        "income",
        "expend",
        "amount",
        "old_amount",
        "new_amount",
        "logistics_id",
        "remark",
        "source_site_code",
        "source_site_name",
        "source_payload",
    }
)

SUMMARY_WHITELIST = frozenset(
    {
        "platform",
        "account_id",
        "snapshot_date",
        "fee_level_1",
        "fee_level_2",
        "fee_name",
        "income",
        "expend",
        "net_amount",
        "source_site_code",
        "source_site_name",
    }
)


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def amount_storage_text(value: Any, *, field: str) -> str:
    try:
        from shared.finance.money import (
            InvalidAmountError,
            MissingAmountError,
            decimal_to_storage_text,
            to_decimal,
        )
    except ImportError as exc:
        raise FinanceCaptureError(
            "SHARED_FINANCE_UNAVAILABLE",
            "shared.finance 公共金额模块不可用",
            stage="shared_import",
        ) from exc
    try:
        return decimal_to_storage_text(to_decimal(value))
    except MissingAmountError as exc:
        raise FinanceCaptureError(
            "AMOUNT_MISSING",
            f"金额字段缺失：{field}",
            stage="normalize",
        ) from exc
    except InvalidAmountError as exc:
        raise FinanceCaptureError(
            "AMOUNT_INVALID",
            f"金额字段格式异常：{field}",
            stage="normalize",
        ) from exc
    except Exception as exc:
        code = clean_text(getattr(exc, "code", "")) or "AMOUNT_INVALID"
        raise FinanceCaptureError(code, f"金额字段格式异常：{field}", stage="normalize") from exc


def parse_source_datetime(value: Any, *, field: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime.combine(value, dt.time.min)
    elif isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            parsed = dt.datetime.fromtimestamp(timestamp, SHANGHAI_TZ)
        except (OSError, OverflowError, ValueError) as exc:
            raise FinanceCaptureError("DATETIME_INVALID", f"时间字段格式异常：{field}", stage="normalize") from exc
    else:
        text = clean_text(value)
        if not text:
            raise FinanceCaptureError("DATETIME_MISSING", f"时间字段缺失：{field}", stage="normalize")
        parsed = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ):
            try:
                parsed = dt.datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise FinanceCaptureError("DATETIME_INVALID", f"时间字段格式异常：{field}", stage="normalize")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def response_json(response: Any, *, platform: str, stage: str) -> Any:
    status = int(getattr(response, "status_code", 0) or 0)
    headers = getattr(response, "headers", {}) or {}
    content_type = clean_text(headers.get("content-type")).lower()
    location = clean_text(headers.get("location")).lower()
    body = clean_text(getattr(response, "text", ""))
    response_url = clean_text(getattr(response, "url", "")).lower()
    if status in {301, 302, 303, 307, 308, 401, 403} or any(
        marker in location or marker in response_url for marker in ("/login", "login.html", "ky-sso", "passport")
    ):
        raise FinanceCaptureError("AUTH_REQUIRED", f"{platform} 财务页面登录态失效", stage=stage)
    if not body:
        raise FinanceCaptureError("EMPTY_RESPONSE", f"{platform} 财务接口返回空响应", stage=stage)
    lower = body.lower()
    if "text/html" in content_type and any(
        marker in lower
        for marker in ("type=\"password\"", "type='password'", "login-form", "loginform", "ky-sso")
    ):
        raise FinanceCaptureError("AUTH_REQUIRED", f"{platform} 财务接口返回登录页", stage=stage)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads(body)
        except Exception as exc:
            raise FinanceCaptureError("NON_JSON_RESPONSE", f"{platform} 财务接口返回非 JSON", stage=stage) from exc
    if not isinstance(payload, (dict, list)):
        raise FinanceCaptureError("FIELD_DRIFT", f"{platform} 财务接口顶层结构异常", stage=stage)
    return payload


def extract_rows_total(payload: Any, *, stage: str) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(payload, list):
        if any(not isinstance(row, dict) for row in payload):
            raise FinanceCaptureError("FIELD_DRIFT", "财务列表包含非对象行", stage=stage)
        return list(payload), None
    if not isinstance(payload, dict):
        raise FinanceCaptureError("FIELD_DRIFT", "财务响应不是对象", stage=stage)

    candidates: list[tuple[str, Any]] = []
    for key in ("rows", "records", "list", "items"):
        if isinstance(payload.get(key), list):
            candidates.append((key, payload[key]))
    data = payload.get("data")
    if isinstance(data, list):
        candidates.append(("data", data))
    elif isinstance(data, dict):
        for key in ("rows", "records", "list", "items"):
            if isinstance(data.get(key), list):
                candidates.append((f"data.{key}", data[key]))
    if len(candidates) != 1:
        raise FinanceCaptureError(
            "FIELD_DRIFT",
            "财务响应未找到唯一列表字段",
            stage=stage,
        )
    rows = candidates[0][1]
    if any(not isinstance(row, dict) for row in rows):
        raise FinanceCaptureError("FIELD_DRIFT", "财务列表包含非对象行", stage=stage)

    total: int | None = None
    total_sources: list[Any] = [payload.get("total"), payload.get("count")]
    if isinstance(data, dict):
        total_sources.extend([data.get("total"), data.get("count")])
    for value in total_sources:
        if value in (None, ""):
            continue
        try:
            total = int(str(value))
        except ValueError as exc:
            raise FinanceCaptureError("FIELD_DRIFT", "财务响应 total 不是整数", stage=stage) from exc
        break
    return list(rows), total


def paginate_by_source_key(
    fetch_page: Callable[[int, int], Any],
    *,
    source_key: str,
    page_size: int,
    max_pages: int,
    stage: str,
) -> PageBatch:
    if page_size <= 0 or max_pages <= 0:
        raise ValueError("page_size/max_pages 必须大于 0")
    by_key: dict[str, dict[str, Any]] = {}
    total: int | None = None
    duplicate_rows = 0
    page_row_count = 0
    page_row_counts: list[int] = []
    pages = 0
    for page in range(1, max_pages + 1):
        payload = fetch_page(page, page_size)
        page_rows, page_total = extract_rows_total(payload, stage=stage)
        pages = page
        page_row_count += len(page_rows)
        page_row_counts.append(len(page_rows))
        if total is None:
            total = page_total
        elif page_total is not None and page_total != total:
            raise FinanceCaptureError("PAGINATION_TOTAL_DRIFT", "分页期间 total 发生变化", stage=stage)
        for row in page_rows:
            if source_key not in row or not clean_text(row.get(source_key)):
                raise FinanceCaptureError("STABLE_KEY_MISSING", f"财务行缺少稳定键 {source_key}", stage=stage)
            source_id = clean_text(row[source_key])
            previous = by_key.get(source_id)
            if previous is not None:
                duplicate_rows += 1
                if previous != row:
                    raise FinanceCaptureError("STABLE_KEY_CONFLICT", "分页重叠行内容不一致", stage=stage)
                continue
            by_key[source_id] = dict(row)
        if not page_rows:
            if page == 1 and total is None:
                raise FinanceCaptureError(
                    "UNVERIFIED_TOTAL",
                    "财务首屏为空但响应未提供 total=0",
                    stage=stage,
                )
            break
        if total is not None and len(by_key) >= total:
            break
        if len(page_rows) < page_size:
            break
    else:
        if total is None or len(by_key) < total:
            raise FinanceCaptureError("PAGINATION_TRUNCATED", "财务分页达到 max_pages 仍未完成", stage=stage)
    if total is None:
        raise FinanceCaptureError(
            "UNVERIFIED_TOTAL",
            "财务分页响应未提供可验证的 total",
            stage=stage,
        )
    if len(by_key) < total:
        raise FinanceCaptureError("PAGINATION_TRUNCATED", "财务分页结果少于 total", stage=stage)
    if len(by_key) > total:
        raise FinanceCaptureError("PAGINATION_TOTAL_MISMATCH", "财务分页唯一行数大于 total", stage=stage)
    return PageBatch(
        rows=list(by_key.values()),
        total=total,
        pages=pages,
        page_row_counts=tuple(page_row_counts),
        page_row_count=page_row_count,
        duplicate_rows=duplicate_rows,
    )


def require_row_keys(row: Mapping[str, Any], keys: Iterable[str], *, stage: str) -> None:
    missing = [key for key in keys if key not in row]
    if missing:
        raise FinanceCaptureError("FIELD_DRIFT", "财务行字段结构发生变化", stage=stage)


def whitelist_record(record: Mapping[str, Any], *, summary: bool = False) -> dict[str, Any]:
    allowed = SUMMARY_WHITELIST if summary else TRANSACTION_WHITELIST
    return {key: record[key] for key in allowed if key in record}


def validate_normalized_summaries(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    required = {"platform", "account_id", "snapshot_date", "income", "expend"}
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not required.issubset(row) or not clean_text(row.get("fee_level_1") or row.get("fee_name")):
            raise FinanceCaptureError(
                "SUMMARY_FIELD_DRIFT",
                "财务汇总行尚未按平台真实字段正规化",
                stage="summary_normalize",
            )
        income = amount_storage_text(row.get("income"), field="summary_income")
        expend = amount_storage_text(row.get("expend"), field="summary_expend")
        income_value = Decimal(income)
        expend_value = Decimal(expend)
        if income_value < 0 or expend_value < 0 or (income_value == 0) == (expend_value == 0):
            raise FinanceCaptureError(
                "AMOUNT_DIRECTION_INVALID",
                "财务汇总行收入/支出方向不唯一",
                stage="summary_normalize",
            )
        candidate = dict(row)
        candidate["income"] = income
        candidate["expend"] = expend
        normalized.append(whitelist_record(candidate, summary=True))
    return normalized


def validate_page_identity(
    *,
    platform: str,
    account_match: Any,
    login_site_code: Any,
    source_site_code: Any,
) -> None:
    """Require browser-verified account identity and an exact site match."""
    if account_match is not True:
        raise FinanceCaptureError(
            "ACCOUNT_PAGE_MISMATCH",
            f"{platform} 财务页面与账号管理登录账号不匹配",
            stage="page_discovery",
        )
    login_site = clean_text(login_site_code)
    source_site = clean_text(source_site_code)
    if not login_site or not source_site:
        raise FinanceCaptureError(
            "SOURCE_SITE_MISSING",
            f"{platform} 财务页面未解析到登录网点和查询网点",
            stage="page_discovery",
        )
    if login_site != source_site:
        raise FinanceCaptureError(
            "SOURCE_SITE_MISMATCH",
            f"{platform} 财务页面查询网点与登录网点不一致",
            stage="page_discovery",
        )


def filter_target_date(
    records: Sequence[dict[str, Any]],
    *,
    target_date: dt.date,
) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    excluded = 0
    for record in records:
        parsed = parse_source_datetime(record.get("trade_time"), field="trade_time")
        if parsed.date() != target_date:
            excluded += 1
            continue
        normalized = dict(record)
        normalized["trade_time"] = parsed.isoformat()
        normalized["target_date"] = target_date.isoformat()
        kept.append(normalized)
    if records and not kept:
        raise FinanceCaptureError("DATE_RANGE_MISMATCH", "财务接口返回行均不属于目标日期", stage="date_filter")
    return kept, excluded


def decimal_extrema(records: Sequence[Mapping[str, Any]], fields: Iterable[str]) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for field in fields:
        values: list[Decimal] = []
        for record in records:
            text = clean_text(record.get(field))
            if not text:
                continue
            try:
                values.append(Decimal(text))
            except InvalidOperation:
                continue
        if values:
            output[field] = {"min": format(min(values), "f"), "max": format(max(values), "f")}
    return output
