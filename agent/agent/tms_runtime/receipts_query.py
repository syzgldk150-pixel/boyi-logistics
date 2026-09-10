"""Query receipt data for a page without starting an archive/synchronization job."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from agent.tms_runtime.account_manager import resolve_account_params
from agent.tms_runtime.direct_business import DirectBusinessError
from agent.tms_runtime.direct_execution import call_blocking
from agent.tms_runtime.scripts import receipts_sync


def query_receipts(params: dict[str, Any], *, read_source_boundary=None) -> dict[str, Any]:
    allowed = {"platform", "direction", "date_from", "date_to", "q", "receipt_status",
               "date_type", "code_type", "page", "page_size", "audit_status", "photo_status"}
    if set(params) - allowed:
        raise DirectBusinessError("INVALID_RECEIPT_QUERY", "回单查询包含未开放字段。")
    platform, direction = params.get("platform", "all"), params.get("direction", "send")
    if platform not in {"all", "ronghui", "yunda"} or direction not in {"send", "receive"}:
        raise DirectBusinessError("INVALID_RECEIPT_QUERY", "回单平台或方向无效。")
    try:
        start, end = date.fromisoformat(params["date_from"]), date.fromisoformat(params["date_to"])
        page, page_size = int(params.get("page", 1)), int(params.get("page_size", 50))
        if start > end or page < 1 or not 1 <= page_size <= 100:
            raise ValueError
    except (KeyError, ValueError, TypeError) as exc:
        raise DirectBusinessError("INVALID_RECEIPT_QUERY", "回单日期或分页参数无效。") from exc
    providers = ["ronghui", "yunda"] if platform == "all" else [platform]
    if direction == "receive" and "yunda" in providers:
        raise DirectBusinessError("RECEIPT_SOURCE_UNAVAILABLE", "韵达派方回单尚未接入。")

    def read_source(provider: str):
        source_params = resolve_account_params({}, default_system=provider,
            default_purpose="price" if provider == "ronghui" else "")
        source_params.update(params)
        source_params.update({"platform": provider, "page_size": receipts_sync.DEFAULT_PAGE_SIZE,
                              "max_pages": receipts_sync.DEFAULT_MAX_PAGES})
        if read_source_boundary is not None:
            fetched = read_source_boundary(source_params, provider, direction)
        else:
            fetched = receipts_sync._fetch_source(source_params, platform=provider, direction=direction)
        _, _, records, stats, warnings = fetched
        total = stats.get("total")
        if (warnings or type(total) is not int or total < 0 or stats.get("truncated")
                or stats.get("fetched") != total or stats.get("attachment_errors", 0)):
            raise DirectBusinessError("INCOMPLETE_RECEIPT_QUERY", "原平台回单数据未完整返回，本次查询失败。")
        if any(not row.get("waybill_no") and not row.get("receipt_no") for row in records):
            raise DirectBusinessError("INVALID_RECEIPT_ROW", "回单来源缺少单据身份。")
        keys = [receipts_sync._record_key(record) for record in records]
        if len(keys) != len(set(keys)):
            raise DirectBusinessError("AMBIGUOUS_RECEIPT_ROWS", "原平台返回重复回单身份，未合并或丢弃。")
        return records, {"platform": provider, "direction": direction, **stats}

    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        results = list(pool.map(read_source, providers))
    records = [record for result, _ in results for record in result]
    audit_status = str(params.get("audit_status") or "all")
    if audit_status != "all":
        records = [row for row in records if str(row.get("audit_status") or "") == audit_status
            or (audit_status == "待审核" and "待" in str(row.get("audit_status") or "")
                and "审核" in str(row.get("audit_status") or ""))]
    photo_status = str(params.get("photo_status") or "all")
    if photo_status in {"has_photo", "missing_photo"}:
        records = [row for row in records if (int(row.get("photo_count") or 0) > 0) == (photo_status == "has_photo")]
    records.sort(key=lambda row: (str(row.get("remote_updated_at") or ""), receipts_sync._record_key(row)), reverse=True)
    total = len(records)
    start_index = (page - 1) * page_size
    return {"rows": records[start_index:start_index + page_size], "pagination": {
        "page": page, "page_size": page_size, "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size), "offset": start_index,
        "has_prev": page > 1, "has_next": start_index + page_size < total,
        "start": start_index + 1 if total else 0, "end": min(start_index + page_size, total),
    }, "sources": [stats for _, stats in results], "complete": True}


async def query_receipts_async(params: dict[str, Any], timeout_sec: int, *, read_lifecycle=None) -> dict[str, Any]:
    loop = asyncio.get_running_loop()

    def source_boundary(source_params, provider, direction):
        account_id = source_params.get("account_id")
        if not account_id:
            raise DirectBusinessError("AUTH_REQUIRED", "回单查询未解析到有效业务账号。")
        async def read():
            return await read_lifecycle.call_read(operation="receipts-query." + provider,
                account_ids=(account_id,), handler=lambda: call_blocking(
                    lambda: receipts_sync._fetch_source(source_params, platform=provider, direction=direction),
                    timeout_sec=timeout_sec))
        return asyncio.run_coroutine_threadsafe(read(), loop).result()

    return await call_blocking(lambda: query_receipts(params,
        read_source_boundary=source_boundary if read_lifecycle is not None else None), timeout_sec=timeout_sec)
