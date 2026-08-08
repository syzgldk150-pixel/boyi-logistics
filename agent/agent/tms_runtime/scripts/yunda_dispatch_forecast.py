"""Fetch Yunda branch dispatch forecast master-bill rows."""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import get_session_broker
from agent.tms_runtime.yunda_report import (
    DEFAULT_DEST_BRANCH,
    DEFAULT_MAX_PAGES,
    DEFAULT_PAGE_SIZE,
    build_search_params,
    page_url,
    report_origin,
    search_url,
)


DEFAULT_TZ = ZoneInfo("Asia/Shanghai")

FIELD_MAP = (
    ("ship_id", "主单号"),
    ("unit_cnt", "开单件数"),
    ("scan_cnt", "扫描件数"),
    ("frgt_wgt", "重量/kg"),
    ("frgt_vol", "体积/m3"),
    ("pkg_lod_typ", "包装类型"),
    ("fld_tm", "清场时间"),
    ("plan_tlns", "规划时效"),
    ("rcv_cust_addr", "开单目的地址"),
    ("est_arv_tm", "预计到达时间"),
    ("due_delv_dt", "应派时间"),
)


def _target_date(params: dict[str, Any]) -> dt.date:
    raw = str(params.get("target_date") or params.get("due_delv_dt") or "").strip()
    if raw:
        return dt.date.fromisoformat(raw[:10])
    return dt.datetime.now(DEFAULT_TZ).date() + dt.timedelta(days=1)


def _report_origin(params: dict[str, Any]) -> str:
    return report_origin(params)


def _search_url(params: dict[str, Any]) -> str:
    return search_url(params)


def _page_url(params: dict[str, Any]) -> str:
    return page_url(params)


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (
        payload.get("rows"),
        data.get("rows"),
        data.get("list"),
        data.get("records"),
        data.get("data"),
        payload.get("data") if isinstance(payload.get("data"), list) else None,
        payload.get("records"),
    ):
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _extract_total(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for value in (
        payload.get("total"),
        payload.get("data", {}).get("total") if isinstance(payload.get("data"), dict) else None,
        payload.get("count"),
    ):
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def _normalize_value(value: Any) -> Any:
    if value is None:
        return ""
    return value


def normalize_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        target_name: _normalize_value(row.get(source_key))
        for source_key, target_name in FIELD_MAP
    }


def _build_query_params(
    params: dict[str, Any],
    *,
    target_date: dt.date,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return build_search_params(params, target_date=target_date, limit=limit, offset=offset)


def fetch_page(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    limit: int,
    offset: int,
) -> Any:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": _page_url(params),
        "X-Requested-With": "XMLHttpRequest",
    }
    response = session.get(
        _search_url(params),
        params=_build_query_params(params, target_date=target_date, limit=limit, offset=offset),
        headers=headers,
        allow_redirects=False,
        timeout=int(params.get("request_timeout_sec") or 30),
    )
    if response.status_code in {301, 302, 401, 403}:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达报表登录态已失效，请重新登录韵达账号。")
    content_type = str(response.headers.get("content-type") or "").lower()
    body = response.text or ""
    if "text/html" in content_type and ("登录" in body or "验证码" in body or "login" in body.lower()):
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达报表登录态已失效，请重新登录韵达账号。")
    response.raise_for_status()
    try:
        payload = response.json()
    except Exception as exc:
        if not body.strip():
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达报表接口返回空响应，请重新登录韵达账号。") from exc
        raise RuntimeError(f"韵达派件预测接口返回非 JSON: {body[:120]}") from exc
    if not isinstance(payload, (dict, list)):
        raise RuntimeError(f"韵达派件预测接口返回格式异常: {type(payload).__name__}")
    return payload


def collect_records(
    session: Any,
    params: dict[str, Any],
    *,
    target_date: dt.date,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> tuple[list[dict[str, Any]], int | None]:
    rows: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0
    for _page_index in range(max_pages):
        payload = fetch_page(session, params, target_date=target_date, limit=page_size, offset=offset)
        page_rows = _extract_rows(payload)
        if total is None:
            total = _extract_total(payload)
        if not page_rows:
            break
        rows.extend(page_rows)
        if total is not None and len(rows) >= total:
            break
        if len(page_rows) < page_size:
            break
        offset += page_size
    return rows, total


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    target_date = _target_date(params)
    page_size = int(params.get("page_size") or params.get("limit") or DEFAULT_PAGE_SIZE)
    max_pages = int(params.get("max_pages") or DEFAULT_MAX_PAGES)
    if page_size <= 0:
        raise ValueError("page_size must be > 0")
    if max_pages <= 0:
        raise ValueError("max_pages must be > 0")

    session_profile = str(params.get("session_profile") or "yunda").strip() or "yunda"
    broker = get_session_broker(session_profile)
    session = broker.build_requests_session(validate=not bool(params.get("skip_session_validate", False)))
    rows, total = collect_records(
        session,
        params,
        target_date=target_date,
        page_size=page_size,
        max_pages=max_pages,
    )
    records = [normalize_record(row) for row in rows]
    return {
        "ok": True,
        "source": "yunda_dispatch_forecast",
        "session_profile": session_profile,
        "target_date": f"{target_date:%Y-%m-%d}",
        "total": total if total is not None else len(rows),
        "fetched": len(rows),
        "records": records,
    }
