"""
Template for Ronghui TMS send order query using requests.
Includes payload builder and data normalization skeleton.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, Iterable, List, Optional

import requests

from agent.tms_runtime.scripts.login_manager import TMSAuth


DATA_QUERY_URL = "https://tms.ronghuiwl.com/dataQuery/findPageByCallId"
CALL_ID = "FIND_BILL_SEND"
DEFAULT_REFERER = "https://tms.ronghuiwl.com/widget/home"
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 50

DEFAULT_HEADERS = {
    "Accept": "text/plain, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
}

TOTAL_COLUMNS = [
    {"type": "sum", "column": "PIECE_NUMBER"},
    {"type": "sum", "column": "BILL_WEIGHT"},
    {"type": "sum", "column": "OVER_WEIGHT_NUMBER"},
    {"type": "sum", "column": "BOOT_MANAGE_FEE"},
    {"type": "sum", "column": "INSURANCE_FEE"},
    {"type": "sum", "column": "TRANSPORT_FEE"},
    {"type": "sum", "column": "OPERATE_FEE"},
    {"type": "sum", "column": "REC_BIGGOODS_FEE"},
    {"type": "sum", "column": "REC_LONGGOODS_FEE"},
    {"type": "sum", "column": "REC_TOPAYMENT_CHARGE"},
    {"type": "sum", "column": "REC_DISPATCH_FEE"},
    {"type": "sum", "column": "REC_RETURNBILL_FEE"},
    {"type": "sum", "column": "SETTLEMENT_WEIGHT"},
    {"type": "sum", "column": "FREIGHT"},
    {"type": "sum", "column": "VOLUME"},
    {"type": "sum", "column": "OTHER_FEE1"},
    {"type": "sum", "column": "OTHER_FEE2"},
    {"type": "sum", "column": "OTHER_FEE3"},
    {"type": "sum", "column": "INSURANCE"},
    {"type": "sum", "column": "VOLUME_WEIGHT"},
    {"type": "sum", "column": "TOPAYMENT"},
    {"type": "sum", "column": "OTHER_FEE"},
    {"type": "sum", "column": "GOODS_PAYMENT"},
    {"type": "sum", "column": "REC_GOODS_CHARGE"},
    {"type": "sum", "column": "GUEST_FREIGHT"},
]

FIELD_MAP = {
    "BILL_CODE": "运单编号",
    "INSERT_DATE": "发件日期",
    "BL_SIGNS_MARKING_TEXT": "签收状态",
    "DESTINATION": "目的网点",
    "ACCEPT_COUNTY": "收件区/县",
    "ACCEPT_MAN_ADDRESS": "收件地址",
    "SEND_MAN": "寄件人",
    "SEND_MAN_PHONE": "寄件手机",
    "ACCEPT_MAN": "收货人",
    "ACCEPT_MAN_PHONE": "收货电话",
    "GOODS_NAME": "货物名称",
    "PACK_TYPE": "包装类型",
    "DISPATCH_MODE": "派送方式",
    "PIECE_NUMBER": "件数",
    "FEE_WEIGHT": "实际重量",
    "GUEST_FREIGHT": "录单金额",
    "R_BILLCODE": "回单号",
    "REMARK": "备注",
    "PAYMENT_TYPE": "支付类型",
    "VOLUME_WEIGHT": "体积重量",
    "VOLUME": "体积",
    "SETTLEMENT_WEIGHT": "结算重量",
    "TOPAYMENT": "到付款",
}

NUMERIC_FIELDS = {
    "件数",
    "实际重量",
    "录单金额",
    "体积",
    "体积重量",
    "结算重量",
    "到付款",
}


def _build_date_range(target_date: Optional[dt.date] = None) -> Dict[str, str]:
    if target_date is None:
        target_date = dt.date.today()
    date_str = target_date.strftime("%Y/%m/%d")
    return {
        "start": f"{date_str} 00:00:00",
        "end": f"{date_str} 23:59:59",
    }


def _ensure_daxiang_user(auth: TMSAuth) -> None:
    user_info = auth.config.get("test_user_data") or {}
    daxiang_uid = user_info.get("daxiang_uid")
    daxiang_password = user_info.get("daxiang_password")
    if daxiang_uid and daxiang_password:
        user_info["operator_uid"] = daxiang_uid
        user_info["operator_password"] = daxiang_password
        auth.config["test_user_data"] = user_info


def login_as_daxiang(config_path: Optional[str] = None, *, profile: str = "default") -> requests.Session:
    auth = TMSAuth(config_path, profile=profile)
    _ensure_daxiang_user(auth)
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("login failed: no session")
    return session


def build_payload(
    date_range: Dict[str, str],
    page_index: int = 0,
    page_size: int = 100,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    payload: Dict[str, str] = {
        "CODE_TYPE": "BILL_CODE",
        "searchOrderInput": "",
        "searchDateType": "REGISTER_DATE",
        "SEARCH_DATE_RANGE1": json.dumps(date_range, ensure_ascii=True),
        "SEARCH_DATE_RANGE2": json.dumps({}, ensure_ascii=True),
        "BL_SIGNS_MARKING": "",
        "SEND_SITE_CODE": "",
        "SEND_AREA_CODE": "",
        "CUSTOMER_NAME": "",
        "TAKE_PIECE_EMPLOYEE_CODE": "",
        "REGISTER_SITE_CODE": "",
        "SEND_PROVINCE": "",
        "ORDER_TYPE": "",
        "IS_XX": "",
        "IS_LOAN": "",
        "BL_VIP": "",
        "DISPATCH_SITE_CODE": "",
        "WAITNOTIFY_SEND": "",
        "ACCEPT_AREA_CODE": "",
        "WAITNOTIFY_SEND_STATUS": "",
        "DISPATCH_MODE": "",
        "SEND_MAN": "",
        "PAYMENT_TYPE": "",
        "ACCEPT_MAN": "",
        "DESTINATION_CENTER_CODE": "",
        "DISPATCH_PROVINCE": "",
        "PRODUCT_CODE": "",
        "DESTINATION_TYPE": "",
        "isSort": "",
        "SEND_BUSINESS_TYPE": "",
        "DISP_BUSINESS_TYPE": "",
        "SEARCH_DATE_RANGE": json.dumps(date_range, ensure_ascii=True),
        "REGISTER_DATE": json.dumps(date_range, ensure_ascii=True),
        "ORDER_BY_CREATE_DATE": "ORDER_BY_DATE",
        "IS_SORT": "1",
        "ORDER_TYPE_TEXT": "",
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "sortField": "",
        "sortOrder": "",
        "totalColumns": json.dumps(TOTAL_COLUMNS, ensure_ascii=True, separators=(",", ":")),
    }

    if extra_filters:
        for key, value in extra_filters.items():
            if value is None:
                continue
            payload[str(key)] = str(value)

    return payload


def fetch_send_orders(
    session: requests.Session,
    date_range: Dict[str, str],
    page_index: int = 0,
    page_size: int = 100,
    referer: Optional[str] = None,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = build_payload(
        date_range,
        page_index=page_index,
        page_size=page_size,
        extra_filters=extra_filters,
    )

    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = referer or DEFAULT_REFERER

    resp = session.post(
        DATA_QUERY_URL,
        params={"id": CALL_ID},
        data=payload,
        headers=headers,
        allow_redirects=False,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _coerce_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_record(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for source_key, target_key in FIELD_MAP.items():
        value = item.get(source_key)
        if target_key in NUMERIC_FIELDS:
            normalized[target_key] = _coerce_number(value)
        else:
            normalized[target_key] = value if value is not None else ""
    return normalized


def normalize_records(raw_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = raw_json.get("data") or []
    if not isinstance(data, list):
        return []
    return [normalize_record(item) for item in data if isinstance(item, dict)]


def iter_pages(
    session: requests.Session,
    date_range: Dict[str, str],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    referer: Optional[str] = None,
    extra_filters: Optional[Dict[str, Any]] = None,
) -> Iterable[Dict[str, Any]]:
    page_index = 0
    while page_index < max_pages:
        raw = fetch_send_orders(
            session,
            date_range,
            page_index=page_index,
            page_size=page_size,
            referer=referer,
            extra_filters=extra_filters,
        )
        yield raw
        total = raw.get("total")
        if total is None:
            break
        try:
            total = int(total)
        except (TypeError, ValueError):
            break
        if (page_index + 1) * page_size >= total:
            break
        page_index += 1


def _coerce_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def run_once(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    params = params or {}
    target_date = params.get("target_date")
    explicit_page_index = "page_index" in params or "pageIndex" in params
    page_index = int(params.get("page_index", params.get("pageIndex", 0)))
    page_size = _coerce_int(params.get("page_size", params.get("pageSize", DEFAULT_PAGE_SIZE)), DEFAULT_PAGE_SIZE)
    max_pages = _coerce_int(params.get("max_pages", DEFAULT_MAX_PAGES), DEFAULT_MAX_PAGES)
    referer = params.get("referer")
    extra_filters = params.get("extra_filters") if isinstance(params.get("extra_filters"), dict) else None
    session_profile = str(params.get("session_profile") or "default").strip() or "default"

    date_obj = None
    if target_date:
        date_obj = dt.date.fromisoformat(str(target_date))

    date_range = _build_date_range(date_obj)
    session = login_as_daxiang(profile=session_profile)
    if not explicit_page_index:
        records: List[Dict[str, Any]] = []
        for raw_page in iter_pages(
            session,
            date_range,
            page_size=page_size,
            max_pages=max_pages,
            referer=referer,
            extra_filters=extra_filters,
        ):
            records.extend(normalize_records(raw_page))
        return records

    raw = fetch_send_orders(
        session,
        date_range,
        page_index=page_index,
        page_size=page_size,
        referer=referer,
        extra_filters=extra_filters,
    )
    return normalize_records(raw)


if __name__ == "__main__":
    results = run_once()
    print(json.dumps(results, ensure_ascii=False))
