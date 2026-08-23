"""
Query delivery sign status by bill codes using Ronghui TMS data query.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

import requests

from agent.tms_runtime.scripts.login_manager import TMSAuth


DATA_QUERY_URL = "https://tms.ronghuiwl.com/dataQuery/findAllByCallId"
CALL_ID = "FIND_BILL_SEND"
DEFAULT_REFERER = "https://tms.ronghuiwl.com/widget/home"

DEFAULT_HEADERS = {
    "Accept": "text/plain, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
}

FIELD_MAP = {
    "BILL_CODE": "运单编号",
    "BL_SIGNS_MARKING_TEXT": "签收状态",
}

_SPLIT_RE = re.compile(r"[,\s;]+")
BILL_KEYS = (
    "bill_code",
    "billCode",
    "BILL_CODE",
    "运单编号",
    "单号",
    "运单号",
    "main_bill",
    "mainBill",
    "master_bill",
    "masterBill",
)


def _ensure_daxiang_user(auth: TMSAuth) -> None:
    user_info = auth.config.get("test_user_data") or {}
    daxiang_uid = user_info.get("daxiang_uid")
    daxiang_password = user_info.get("daxiang_password")
    if daxiang_uid and daxiang_password:
        user_info["operator_uid"] = daxiang_uid
        user_info["operator_password"] = daxiang_password
        auth.config["test_user_data"] = user_info


def login_as_daxiang(config_path: Optional[str] = None) -> requests.Session:
    auth = TMSAuth(config_path)
    _ensure_daxiang_user(auth)
    session = auth.login_and_get_session()
    if session is None:
        raise RuntimeError("login failed: no session")
    return session


def _normalize_bill_code(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        return ""
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _split_bill_codes(text: str) -> List[str]:
    parts = _SPLIT_RE.split(text.strip())
    codes = [_normalize_bill_code(part) for part in parts if part]
    return [code for code in codes if code]


def _coerce_bill_codes(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if parsed is not None:
            return _coerce_bill_codes(parsed)
        return _split_bill_codes(text)
    if isinstance(raw, dict):
        for key in ("items", "data", "records", "rows", "bill_codes", "billCodes", "codes"):
            if key in raw:
                return _coerce_bill_codes(raw.get(key))
        for key in BILL_KEYS:
            if key in raw and raw[key]:
                return _split_bill_codes(str(raw[key]))
        if "json" in raw and isinstance(raw["json"], dict):
            return _coerce_bill_codes(raw["json"])
        return []
    if isinstance(raw, list):
        codes: List[str] = []
        for entry in raw:
            if isinstance(entry, str):
                codes.extend(_split_bill_codes(entry))
                continue
            if isinstance(entry, dict):
                for key in BILL_KEYS:
                    if key in entry and entry[key]:
                        codes.extend(_split_bill_codes(str(entry[key])))
                        break
                else:
                    if "json" in entry and isinstance(entry["json"], dict):
                        codes.extend(_coerce_bill_codes(entry["json"]))
        return [code for code in codes if code]
    return []


def _get_param(params: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    if not isinstance(params, dict):
        return default
    for key in keys:
        if key in params and params[key] is not None:
            return params[key]
    return default


def build_payload(
    bill_codes: List[str],
    page_index: int = 0,
    page_size: int = 200,
) -> Dict[str, str]:
    requested = [_normalize_bill_code(code) for code in bill_codes]
    if not requested or any(not code for code in requested):
        raise ValueError("Missing bill codes")
    return {
        "BILL_CODE": ",".join(requested),
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "sortField": "",
        "sortOrder": "",
        "totalColumns": "[]",
    }


def fetch_delivery_status(
    session: requests.Session,
    bill_codes: List[str],
    page_index: int = 0,
    page_size: int = 200,
    referer: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_payload(
        bill_codes,
        page_index=page_index,
        page_size=page_size,
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
    raw = resp.json()
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError("Ronghui delivery-status response is invalid")
    return {"data": raw, "total": len(raw)}


def iter_pages(
    session: requests.Session,
    bill_codes: List[str],
    page_size: int = 200,
    referer: Optional[str] = None,
) -> Iterable[Dict[str, Any]]:
    yield fetch_delivery_status(
        session,
        bill_codes,
        page_index=0,
        page_size=page_size,
        referer=referer,
    )


def normalize_records(raw_items: List[Dict[str, Any]], bill_codes: List[str]) -> List[Dict[str, Any]]:
    status_map: Dict[str, str] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        bill_code = _normalize_bill_code(item.get("BILL_CODE"))
        if not bill_code:
            continue
        status_text = item.get("BL_SIGNS_MARKING_TEXT")
        status_map[bill_code] = "" if status_text is None else str(status_text)

    records: List[Dict[str, Any]] = []
    for code in bill_codes:
        records.append(
            {
                FIELD_MAP["BILL_CODE"]: code,
                FIELD_MAP["BL_SIGNS_MARKING_TEXT"]: status_map.get(code, ""),
            }
        )
    return records


def run_once(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    params = params or {}
    raw_codes = _get_param(
        params,
        "items",
        "data",
        "records",
        "rows",
        "bill_codes",
        "billCodes",
        "codes",
        default=None,
    )
    if raw_codes is None:
        raw_codes = _get_param(params, "items_json", "itemsJson", "bill_codes_json", "billCodesJson", default=None)

    bill_codes = _coerce_bill_codes(raw_codes)
    if not bill_codes:
        single = _get_param(
            params,
            "bill_code",
            "billCode",
            "BILL_CODE",
            "运单编号",
            default="",
        )
        bill_codes = _coerce_bill_codes(single)
    if not bill_codes:
        raise ValueError("Missing bill codes")

    page_size = int(_get_param(params, "page_size", "pageSize", default=max(100, len(bill_codes))))
    page_size = max(1, page_size)
    referer = _get_param(params, "referer", default=None)
    config_path = _get_param(params, "config_path", "configPath", default=None)

    session = login_as_daxiang(config_path)
    raw_items: List[Dict[str, Any]] = []
    for raw in iter_pages(
        session,
        bill_codes,
        page_size=page_size,
        referer=referer,
    ):
        data = raw.get("data")
        if isinstance(data, list):
            raw_items.extend(data)

    return normalize_records(raw_items, bill_codes)


if __name__ == "__main__":
    raise SystemExit("Delivery_status.py is intended to be called via run_once().")
