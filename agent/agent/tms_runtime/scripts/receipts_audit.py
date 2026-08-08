"""Receipt audit target."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

try:
    from agent.tms_runtime.errors import TMSAuthStateError
except Exception:  # pragma: no cover - allows isolated script tests without FastAPI deps.
    class TMSAuthStateError(RuntimeError):
        def __init__(self, code: str, message: str):
            super().__init__(message)
            self.code = str(code or "AUTH_REQUIRED").strip() or "AUTH_REQUIRED"


SUPPORTED_PLATFORMS = {"ronghui", "yunda"}
AUDIT_STATUS_BY_RESULT = {
    "passed": "审核通过",
    "failed": "审核不通过",
}
RONGHUI_ORIGIN = "https://tms.ronghuiwl.com"
RONGHUI_SAVE_TABLES_URL = f"{RONGHUI_ORIGIN}/dataOperation/saveTables"
RONGHUI_SESSION_PROFILE = "price"
RONGHUI_AUDIT_MENU_BY_DIRECTION = {
    "send": ("2910", "寄方回单跟踪"),
    "receive": ("2911", "派方回单处理"),
}


def get_session_broker(profile_name: str):
    from agent.tms_runtime.session_broker import get_session_broker as build_broker

    return build_broker(profile_name)


def fetch_ronghui_process_rows(
    session: Any,
    row: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_sec: int,
) -> list[dict[str, Any]]:
    from agent.tms_runtime.scripts.receipts_sync import _fetch_ronghui_process_rows

    return _fetch_ronghui_process_rows(session, row, headers=headers, timeout_sec=timeout_sec)


def resolve_ronghui_entry_url(session: Any, *, entry_menu_id: str, entry_menu_text: str) -> str:
    from agent.tms_runtime.scripts.ronghui_waybill_proxy import _resolve_entry_url

    return _resolve_entry_url(session, entry_menu_id=entry_menu_id, entry_menu_text=entry_menu_text)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_platform(value: Any) -> str:
    text = _clean_text(value).lower()
    aliases = {
        "融辉": "ronghui",
        "rh": "ronghui",
        "ronghui": "ronghui",
        "韵达": "yunda",
        "yd": "yunda",
        "yunda": "yunda",
    }
    return aliases.get(text, text)


def _safe_business_params(params: dict[str, Any]) -> dict[str, str]:
    return {
        "receipt_id": _clean_text(params.get("receipt_id") or params.get("id")),
        "platform": _normalize_platform(params.get("platform")),
        "direction": _clean_text(params.get("direction")),
        "result": _clean_text(params.get("result")).lower(),
        "reason": _clean_text(params.get("reason")),
        "waybill_no": _clean_text(params.get("waybill_no")),
        "receipt_no": _clean_text(params.get("receipt_no")),
        "return_waybill_no": _clean_text(params.get("return_waybill_no")),
    }


def _has_identifier(params: dict[str, str]) -> bool:
    return bool(params["waybill_no"] or params["receipt_no"] or params["return_waybill_no"])


def _js_unescape(value: str) -> str:
    if not value:
        return ""

    def replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"%u([0-9A-Fa-f]{4})", replace_unicode, value)
    return unquote(text)


def _read_user_info_cookie(session: Any) -> dict[str, Any]:
    raw = ""
    cookies = getattr(session, "cookies", None)
    if cookies is not None:
        try:
            raw = cookies.get("userInfo") or cookies.get("USER_INFO") or ""
        except Exception:
            raw = ""
    if not raw:
        return {}
    try:
        decoded = _js_unescape(str(raw))
        payload = json.loads(decoded)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_text(source: dict[str, Any], *keys: str) -> str:
    if not isinstance(source, dict):
        return ""
    lowered = {str(key).lower(): value for key, value in source.items()}
    for key in keys:
        value = _clean_text(source.get(key))
        if value:
            return value
        value = _clean_text(lowered.get(str(key).lower()))
        if value:
            return value
    return ""


def _utc_iso_millis() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ronghui_audit_auth_headers(session: Any, direction: str) -> tuple[dict[str, str] | None, str]:
    menu_id, menu_text = RONGHUI_AUDIT_MENU_BY_DIRECTION.get(
        _clean_text(direction) or "send",
        RONGHUI_AUDIT_MENU_BY_DIRECTION["send"],
    )
    try:
        entry_url = resolve_ronghui_entry_url(session, entry_menu_id=menu_id, entry_menu_text=menu_text)
    except Exception as exc:
        return None, f"融辉审核页面鉴权参数获取失败：{exc}"

    query = parse_qs(urlparse(entry_url).query, keep_blank_values=True)
    authentication_key = _clean_text((query.get("authenticationKey") or [""])[0])
    page_id = _clean_text((query.get("pageId") or [""])[0])
    if not authentication_key or not page_id:
        return None, "融辉审核页面缺少 authenticationKey/pageId，无法提交保存请求。"
    return {"authenticationKey": authentication_key, "pageId": page_id}, ""


def _ronghui_process_query_row(params: dict[str, Any]) -> dict[str, Any]:
    raw_payload = params.get("raw_payload")
    raw = raw_payload if isinstance(raw_payload, dict) else {}
    return {
        "BILL_CODE": _first_text(raw, "BILL_CODE", "BILLCODE", "BILL_NO", "bill_code")
        or _clean_text(params.get("waybill_no")),
        "R_BILLCODE": _first_text(raw, "R_BILLCODE", "RETURN_BILL_CODE", "RECEIPT_NO", "receipt_no")
        or _clean_text(params.get("receipt_no")),
    }


def _has_ronghui_process_guid(params: dict[str, Any]) -> bool:
    raw_payload = params.get("raw_payload")
    raw = raw_payload if isinstance(raw_payload, dict) else {}
    return bool(_first_text(raw, "GUID", "guid", "PROCESS_RECORD_ID", "process_record_id"))


def _select_ronghui_process_row(
    rows: list[dict[str, Any]],
    *,
    bill_code: str,
    receipt_no: str,
) -> tuple[dict[str, Any] | None, str]:
    candidates = [
        row
        for row in rows
        if _first_text(row, "BILL_CODE", "BILLCODE", "BILL_NO") == bill_code
        and _first_text(row, "GUID", "PROCESS_RECORD_ID")
    ]
    if receipt_no:
        receipt_matches = [
            row
            for row in candidates
            if _first_text(row, "R_BILLCODE", "RETURN_BILL_CODE", "RECEIPT_NO") in {"", receipt_no}
        ]
        if receipt_matches:
            candidates = receipt_matches

    if len(candidates) == 1:
        return candidates[0], ""
    if not candidates:
        return None, "未查到可用于保存审核结果的融辉处理记录 GUID。"

    pending = [
        row
        for row in candidates
        if _first_text(row, "AUDIT_STATUS", "AUDIT_STATUS_TEXT") in {"1", "待审核", "待寄方审核", "待派方审核"}
    ]
    if len(pending) == 1:
        return pending[0], ""
    return None, f"查到 {len(candidates)} 条融辉处理记录，无法唯一确定要保存审核结果的记录。"


def _resolve_ronghui_process_payload(
    params: dict[str, Any],
    session: Any,
    *,
    headers: dict[str, str],
    timeout_sec: int,
) -> tuple[dict[str, Any] | None, str]:
    if _has_ronghui_process_guid(params):
        return dict(params), ""

    query_row = _ronghui_process_query_row(params)
    bill_code = _clean_text(query_row.get("BILL_CODE"))
    if not bill_code:
        return None, "缺少融辉运单编号，无法查询处理记录 GUID。"

    try:
        rows = fetch_ronghui_process_rows(session, query_row, headers=headers, timeout_sec=timeout_sec)
    except Exception as exc:
        return None, f"融辉处理记录查询失败：{exc}"

    process_row, error = _select_ronghui_process_row(
        rows,
        bill_code=bill_code,
        receipt_no=_clean_text(query_row.get("R_BILLCODE")),
    )
    if process_row is None:
        return None, error

    raw_payload = params.get("raw_payload")
    merged_raw = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    for key in ("GUID", "BILL_CODE", "R_BILLCODE", "REPLY_CONTENT"):
        value = _first_text(process_row, key)
        if value:
            merged_raw[key] = value
    resolved = dict(params)
    resolved["raw_payload"] = merged_raw
    return resolved, ""


def _ronghui_audit_row(params: dict[str, Any], user_info: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    raw_payload = params.get("raw_payload")
    raw = raw_payload if isinstance(raw_payload, dict) else {}
    result = _clean_text(params.get("result")).lower()
    guid = _first_text(raw, "GUID", "guid", "PROCESS_RECORD_ID", "process_record_id")
    bill_code = _first_text(raw, "BILL_CODE", "bill_code", "运单编号(原)") or _clean_text(params.get("waybill_no"))
    reply_content = _first_text(raw, "REPLY_CONTENT", "reply_content", "备注")
    if not guid:
        return None, "缺少融辉处理记录 GUID，无法直连保存审核结果。"
    if not bill_code:
        return None, "缺少融辉运单编号，无法直连保存审核结果。"
    audit_status = "3" if result == "failed" else "2"
    row = {
        "GUID": guid,
        "BILL_CODE": bill_code,
        "REPLY_CONTENT": reply_content,
        "AUDIT_CONTENT": _clean_text(params.get("reason")),
        "AUDIT_STATUS": audit_status,
        "AUDIT_SITE_CODE": _clean_text(user_info.get("loginSiteCode")),
        "AUDIT_SITE": _clean_text(user_info.get("loginSiteName")),
        "AUDIT_MAN_CODE": _clean_text(user_info.get("loginEmpCode")),
        "AUDIT_MAN": _clean_text(user_info.get("loginEmpName") or user_info.get("loginUserName")),
        "AUDIT_TIME": _utc_iso_millis(),
    }
    missing_user_fields = [
        key
        for key in ("AUDIT_SITE_CODE", "AUDIT_SITE", "AUDIT_MAN_CODE", "AUDIT_MAN")
        if not _clean_text(row.get(key))
    ]
    if missing_user_fields:
        return None, f"缺少融辉登录人字段：{', '.join(missing_user_fields)}。"
    return row, ""


def _safe_response_json(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        text = _clean_text(getattr(response, "text", ""))
        try:
            payload = json.loads(text) if text else {}
        except Exception:
            return {"success": False, "message": text or "融辉保存接口返回非 JSON。"}
    return payload if isinstance(payload, dict) else {"success": False, "message": "融辉保存接口返回格式异常。"}


def _audit_ronghui(params: dict[str, Any]) -> dict[str, Any]:
    result = _clean_text(params.get("result")).lower()
    session_profile = _clean_text(params.get("session_profile")) or RONGHUI_SESSION_PROFILE
    timeout_sec = int(params.get("timeout_sec") or 30)
    try:
        session = get_session_broker(session_profile).build_requests_session(validate=True)
    except TMSAuthStateError as exc:
        return _failure(
            platform="ronghui",
            result=result,
            error_code=getattr(exc, "code", "AUTH_REQUIRED") or "AUTH_REQUIRED",
            message=str(exc) or "融辉登录态不可用。",
        )
    except Exception as exc:
        return _failure(
            platform="ronghui",
            result=result,
            error_code="SESSION_UNAVAILABLE",
            message=f"融辉登录态初始化失败：{exc}",
        )

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": RONGHUI_ORIGIN,
        "Referer": f"{RONGHUI_ORIGIN}/widget/home",
        "X-Requested-With": "XMLHttpRequest",
    }
    auth_headers, error_message = _ronghui_audit_auth_headers(session, _clean_text(params.get("direction")) or "send")
    if auth_headers is None:
        return _failure(
            platform="ronghui",
            result=result,
            error_code="RONGHUI_AUDIT_AUTH_HEADER_UNAVAILABLE",
            message=error_message,
        )
    headers.update(auth_headers)

    resolved_params, error_message = _resolve_ronghui_process_payload(
        params,
        session,
        headers=headers,
        timeout_sec=timeout_sec,
    )
    if resolved_params is None:
        return _failure(
            platform="ronghui",
            result=result,
            error_code="MISSING_RONGHUI_AUDIT_FIELDS",
            message=error_message,
        )

    audit_row, error_message = _ronghui_audit_row(resolved_params, _read_user_info_cookie(session))
    if audit_row is None:
        return _failure(
            platform="ronghui",
            result=result,
            error_code="MISSING_RONGHUI_AUDIT_FIELDS",
            message=error_message,
        )

    operation = {
        "beforeAction": None,
        "operationKey": "TAB_PROCESS_RECORD_UPT",
        "afterAction": None,
        "idFields": [],
        "data": [audit_row],
    }
    params_json = json.dumps([operation], ensure_ascii=False, separators=(",", ":"))
    try:
        response = session.post(
            RONGHUI_SAVE_TABLES_URL,
            files={"params": (None, params_json)},
            headers=headers,
            timeout=timeout_sec,
        )
        response.raise_for_status()
    except TMSAuthStateError as exc:
        return _failure(
            platform="ronghui",
            result=result,
            error_code=getattr(exc, "code", "AUTH_REQUIRED") or "AUTH_REQUIRED",
            message=str(exc) or "融辉登录态不可用。",
        )
    except Exception as exc:
        return _failure(
            platform="ronghui",
            result=result,
            error_code="RONGHUI_AUDIT_REQUEST_FAILED",
            message=f"融辉审核保存接口调用失败：{exc}",
        )

    payload = _safe_response_json(response)
    if payload.get("success") is not True:
        return _failure(
            platform="ronghui",
            result=result,
            error_code="RONGHUI_AUDIT_SAVE_FAILED",
            message=_clean_text(payload.get("message")) or "融辉审核保存失败。",
        )

    audit_status = AUDIT_STATUS_BY_RESULT[result]
    return {
        "ok": True,
        "platform": "ronghui",
        "result_status": "direct_api_executed",
        "audit_status": audit_status,
        "message": f"融辉{audit_status}已通过接口提交。",
    }


def _failure(
    *,
    platform: str,
    result: str,
    error_code: str,
    message: str,
    result_status: str = "failed",
) -> dict[str, Any]:
    return {
        "ok": False,
        "platform": platform,
        "result_status": result_status,
        "audit_status": AUDIT_STATUS_BY_RESULT.get(result, ""),
        "error_code": error_code,
        "message": message,
    }


def run_once(params: dict[str, Any] | None = None) -> dict[str, Any]:
    safe_params = _safe_business_params(dict(params or {}))
    platform = safe_params["platform"]
    result = safe_params["result"]

    if platform not in SUPPORTED_PLATFORMS:
        return _failure(
            platform=platform,
            result=result,
            error_code="UNSUPPORTED_PLATFORM",
            message="回单审核仅支持融辉和韵达。",
        )
    if result not in AUDIT_STATUS_BY_RESULT:
        return _failure(
            platform=platform,
            result=result,
            error_code="INVALID_AUDIT_RESULT",
            message='审核结果必须是 "passed" 或 "failed"。',
        )
    if not _has_identifier(safe_params):
        return _failure(
            platform=platform,
            result=result,
            error_code="MISSING_RECEIPT_IDENTIFIER",
            message="缺少运单号、回单号或返回单号，无法定位回单。",
        )

    if platform == "ronghui":
        return _audit_ronghui(dict(params or {}))

    return _failure(
        platform=platform,
        result=result,
        error_code="AUDIT_CAPTURE_REQUIRED",
        message="回单审核真实接口尚未抓取，未执行第三方审核请求。",
        result_status="capture_required",
    )
