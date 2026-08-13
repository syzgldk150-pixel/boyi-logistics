"""
定时获取派件预测数据：基于 requests.Session 调用接口，无需 Selenium。
依赖 login_manager.TMSAuth 提供已登录的 Session（包含 Cookie）。
"""

import datetime
import json
from typing import Any, Dict, List, Optional

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.ronghui_user_context import (
    RONGHUI_USER_INFO_COOKIE,
    ronghui_user_context_signature,
)
from agent.tms_runtime.scripts.login_manager import TMSAuth


DISPATCH_URL = "https://tms.ronghuiwl.com/dataQuery/findPageByCallId"
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 100


def _session_user_context_signatures(session: Any) -> set[tuple[str, ...]]:
    cookies = getattr(session, "cookies", None)
    if cookies is None:
        raise TMSAuthStateError(
            "AUTH_REQUIRED",
            "Ronghui session is missing its authenticated user context.",
        )

    signatures: set[tuple[str, ...]] = set()
    incomplete = False
    matched = False
    try:
        cookie_rows = list(cookies)
    except TypeError:
        cookie_rows = []

    for cookie in cookie_rows:
        name = str(getattr(cookie, "name", "") or "")
        if name.lower() != RONGHUI_USER_INFO_COOKIE.lower():
            continue
        matched = True
        signature = ronghui_user_context_signature(getattr(cookie, "value", ""))
        if signature is None:
            incomplete = True
        else:
            signatures.add(signature)

    if not matched:
        for name in (RONGHUI_USER_INFO_COOKIE, "USER_INFO"):
            try:
                raw = cookies.get(name)
            except Exception:
                raw = None
            if not raw:
                continue
            matched = True
            signature = ronghui_user_context_signature(raw)
            if signature is None:
                incomplete = True
            else:
                signatures.add(signature)

    if not matched:
        raise TMSAuthStateError(
            "AUTH_REQUIRED",
            "Ronghui session is missing its authenticated user context.",
        )
    if incomplete or len(signatures) != 1:
        raise TMSAuthStateError(
            "AUTH_REQUIRED",
            "Ronghui session user context is incomplete or ambiguous.",
        )
    return signatures


def resolve_login_site_code(session: Any, *, explicit_site_code: str = "") -> str:
    signature = next(iter(_session_user_context_signatures(session)))
    observed_site_code = signature[3]
    requested_site_code = str(explicit_site_code or "").strip()
    if requested_site_code and requested_site_code != observed_site_code:
        raise TMSAuthStateError(
            "ACCOUNT_AMBIGUOUS",
            "Requested site code does not match the selected account session.",
        )
    return observed_site_code


def build_date_range(target_date: Optional[datetime.date] = None) -> Dict[str, str]:
    """构造当天的起止时间字符串。"""
    if target_date is None:
        target_date = datetime.date.today()
    start = f"{target_date.strftime('%Y/%m/%d')} 00:00:00"
    end = f"{target_date.strftime('%Y/%m/%d')} 23:59:59"
    return {"start": start, "end": end}


def fetch_dispatch_records(
    session,
    login_site_code: str,
    date_range: Optional[Dict[str, str]] = None,
    page_index: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Dict[str, Any]:
    """
    调用 FIND_DISPATCH_FORECAST_CENTER 接口，返回原始 JSON。
    可按需分页：page_index 从 0 开始。
    """
    if date_range is None:
        date_range = build_date_range()

    payload = {
        "PRINT_TYPE_VIEW": "62",
        "CODE_TYPE": "BILL_CODE",
        "searchOrderInput": "",
        # 手工页面默认“寄件日期”对应的实际提交值是 SEND_DATE。
        # arrive-list 需要与人工在“派件运单查询”页面的查询结果保持一致。
        "searchDateType": "SEND_DATE",
        "SEARCH_DATE_RANGE": json.dumps(date_range, ensure_ascii=False),
        "PRE_OR_NEXT_STATION_CODE": "",
        "SCAN_SITE_CODE": "",
        "PRINT_TYPE_VIEW2": "客户",
        "ORDER_TYPE": "",
        "FORECAST": "1",
        "WAITNOTIFY_SEND": "",
        "WAITNOTIFY_SEND_STATUS": "",
        "BL_VIP": "",
        "EXCLUDE_RETURNBILL": "2",
        "encryption": "0",
        "isViewLogo": "0",
        "freight_type": "",
        "SCAN_DATE": json.dumps(date_range, ensure_ascii=False),
        "LOGIN_SITE_CODE": login_site_code,
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "sortField": "",
        "sortOrder": "",
        "totalColumns": "[]",
    }

    headers = {
        "Accept": "text/plain, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://tms.ronghuiwl.com",
        "Referer": "https://tms.ronghuiwl.com/widget/home",
        "X-Requested-With": "XMLHttpRequest",
    }

    resp = session.post(
        DISPATCH_URL,
        params={"id": "FIND_DISPATCH_FORECAST_CENTER"},
        data=payload,
        headers=headers,
        allow_redirects=False,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_data_list(raw_json: Any) -> List[Any]:
    if isinstance(raw_json, list):
        return raw_json
    if not isinstance(raw_json, dict):
        return []
    data = raw_json.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "rows", "records", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


FIELDS_ORDER = [
    "BILL_CODE",
    "GOODS_NAME",
    "PACK_TYPE",
    "DISPATCH_MODE",
    "PIECE_NUMBER",
    "R_BILLCODE",
    "BILL_WEIGHT",
    "VOLUME",
    "REMARK",
    "DESTINATION",
    "ACCEPT_MAN",
    "ACCEPT_MAN_PHONE",
    "ACCEPT_MAN_ADDRESS",
    "SETTLEMENT_WEIGHT",
    "VOLUME_WEIGHT",
    "FREIGHT",
    "PAYMENT_TYPE",
    "TOPAYMENT",
]


def format_records(raw_json: Dict[str, Any]) -> List[List[Any]]:
    """将接口返回的数据按指定字段顺序提取为二维数组，仅返回值。"""
    data = _extract_data_list(raw_json)

    rows: List[List[Any]] = []
    for item in data:
        if isinstance(item, dict):
            row = [("" if item.get(field) is None else item.get(field)) for field in FIELDS_ORDER]
        elif isinstance(item, (list, tuple)):
            row = list(item[: len(FIELDS_ORDER)])
            row.extend([""] * (len(FIELDS_ORDER) - len(row)))
        else:
            continue
        rows.append(row)
    return rows


def collect_dispatch_records(
    session,
    login_site_code: str,
    date_range: Optional[Dict[str, str]] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> List[List[Any]]:
    if page_size <= 0:
        raise ValueError("page_size must be > 0")
    if max_pages <= 0:
        raise ValueError("max_pages must be > 0")

    rows: List[List[Any]] = []
    page_index = 0
    while page_index < max_pages:
        raw = fetch_dispatch_records(
            session,
            login_site_code=login_site_code,
            date_range=date_range,
            page_index=page_index,
            page_size=page_size,
        )
        page_items = _extract_data_list(raw)
        if not page_items:
            break
        rows.extend(format_records(raw))
        if len(page_items) < page_size:
            break
        page_index += 1
    return rows


def _run_once_impl(
    target_date: Optional[datetime.date] = None,
    *,
    session_profile: str,
    explicit_site_code: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> List[List[Any]]:
    """登录、拉取当天派件数据并返回整理后的二维数组。"""
    safe_session_profile = str(session_profile or "").strip()
    if not safe_session_profile:
        raise TMSAuthStateError(
            "AUTH_REQUIRED",
            "An explicit account session profile is required.",
        )
    auth = TMSAuth(profile=safe_session_profile)
    session = auth.login_and_get_session()
    if session is None:
        raise TMSAuthStateError("AUTH_REQUIRED", "Ronghui login did not return a session.")

    date_range = build_date_range(target_date)
    return collect_dispatch_records(
        session,
        login_site_code=resolve_login_site_code(
            session,
            explicit_site_code=explicit_site_code,
        ),
        date_range=date_range,
        page_size=page_size,
        max_pages=max_pages,
    )


if __name__ == "__main__":
    result = _run_once_impl(session_profile="")
    # N8N Execute Command 使用：只打印一条 JSON 结果
    print(json.dumps(result, ensure_ascii=False))


def run_once(params: Dict[str, Any]) -> Any:
    params = params or {}
    target_date_str = params.get("target_date")
    target_date: Optional[datetime.date] = None
    if target_date_str:
        target_date = datetime.date.fromisoformat(str(target_date_str))
    site_code_values = {
        str(params.get(key) or "").strip()
        for key in ("login_site_code", "loginSiteCode", "LOGIN_SITE_CODE")
        if str(params.get(key) or "").strip()
    }
    if len(site_code_values) > 1:
        raise ValueError("Conflicting login site-code aliases")
    explicit_site_code = next(iter(site_code_values), "")
    session_profile = str(params.get("session_profile") or "").strip()
    page_size = int(params.get("page_size") or params.get("pageSize") or DEFAULT_PAGE_SIZE)
    max_pages = int(params.get("max_pages") or params.get("maxPages") or DEFAULT_MAX_PAGES)
    return _run_once_impl(
        target_date=target_date,
        session_profile=session_profile,
        explicit_site_code=explicit_site_code,
        page_size=page_size,
        max_pages=max_pages,
    )
