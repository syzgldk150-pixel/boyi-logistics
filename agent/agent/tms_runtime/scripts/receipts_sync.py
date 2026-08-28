"""Collect Yunda and Ronghui receipt rows for the Console receipt index."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

from agent.tms_runtime.account_contracts import PRICE_SESSION_PROFILE
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import BASE_ORIGIN as RONGHUI_ORIGIN
from agent.tms_runtime.session_broker import YUNDA_INMS_ORIGIN, get_session_broker


RONGHUI_DATA_QUERY_URL = f"{RONGHUI_ORIGIN}/dataQuery/findPageByCallId"
RONGHUI_FIND_ALL_URL = f"{RONGHUI_ORIGIN}/dataQuery/findAllByCallId"
RONGHUI_PROCESS_CALL_ID_BY_DIRECTION = {
    "send": "FIND_SEND_RETURN_PROCESS",
    "receive": "FIND_DISP_RETURN_PROCESS",
}
RONGHUI_RECORD_CALL_ID = "FIND_TAB_PROCESS_RECORD"
RONGHUI_RECORD_ATTACHMENT_CALL_ID = "FIND_TAB_PROCESS_RECORD_PATH"
RONGHUI_PIC_SCAN_ATTACHMENT_CALL_ID = "FIND_TAB_PIC_SCAN_ALL"
YUNDA_RECEIPT_PAGE_BY_DIRECTION = {
    "send": f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/mailing/index.html?page=tab&p=nil",
    "receive": f"{YUNDA_INMS_ORIGIN}/ky_inms/public/index.php/business/waybill/delivery/index.html?page=tab&p=nil",
}
YUNDA_RECEIPT_PATH_MARKER_BY_DIRECTION = {
    "send": "/business/waybill/mailing/",
    "receive": "/business/waybill/delivery/",
}
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 20
ATTACHMENT_FIELD_RE = re.compile(r"(file|photo|image|pic|attach|receiptattachment|reply)", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s'\"<>]+|/(?:file|unauth/download|dataQuery|static)/[^\s'\"<>]+")
RONGHUI_DIRECT_ATTACHMENT_HOSTS = {"rhk13.obs.cn-east-3.myhuaweicloud.com"}
RONGHUI_DIRECT_URL_RE = re.compile(
    r"(?:(?:https?:)?//)?rhk13\.obs\.cn-east-3\.myhuaweicloud\.com/[^\s'\"<>]+",
    re.IGNORECASE,
)
YUNDA_DATAGRID_URL_RE = re.compile(
    r"url\s*:\s*(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)",
    flags=re.IGNORECASE | re.DOTALL,
)
YUNDA_IGNORED_URL_MARKERS = ("/printer/", "download", "upload", "delete", "export", ".css", ".js")
YUNDA_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
YUNDA_QUERY_SPLIT_RE = re.compile(r"[\s,，;；]+")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _coerce_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _date_text(value: Any, default: dt.date) -> str:
    raw = _clean(value)
    if not raw:
        return default.isoformat()
    return raw[:10].replace("/", "-")


def _date_range(params: dict[str, Any]) -> tuple[str, str]:
    today = dt.date.today()
    start = _date_text(params.get("date_from") or params.get("start_date"), today)
    end = _date_text(params.get("date_to") or params.get("end_date"), dt.date.fromisoformat(start))
    return start, end


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
        raw = cookies.get("userInfo") or cookies.get("USER_INFO") or ""
    if not raw:
        return {}
    try:
        decoded = _js_unescape(str(raw))
        payload = json.loads(decoded)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_login_site_code_from_user_info(user_info: dict[str, Any]) -> str:
    queue: list[dict[str, Any]] = [user_info] if isinstance(user_info, dict) else []
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        for key in ("loginSiteCode", "siteCode", "login_site_code", "site_code", "loginOwnerSiteCode"):
            value = _clean(current.get(key))
            if value:
                return value
        for nested_key in ("result", "data", "userInfo", "loginInfo", "loginUserInfo", "user"):
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                queue.append(nested)
    return ""


def _resolve_ronghui_login_site_code(session: Any, params: dict[str, Any]) -> str:
    explicit = _clean(
        params.get("login_site_code")
        or params.get("loginSiteCode")
        or params.get("LOGIN_SITE_CODE")
        or params.get("site_code")
        or params.get("siteCode")
    )
    if explicit:
        return explicit
    return _resolve_login_site_code_from_user_info(_read_user_info_cookie(session))


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = (
        payload.get("rows"),
        payload.get("data"),
        payload.get("list"),
        payload.get("records"),
        data.get("rows"),
        data.get("list"),
        data.get("records"),
        data.get("items"),
    )
    for value in candidates:
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _extract_total(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for value in (payload.get("total"), payload.get("count"), data.get("total"), data.get("count")):
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def _first(row: dict[str, Any], *keys: str) -> str:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and _clean(row.get(key)):
            return _clean(row.get(key))
        value = lowered.get(key.lower())
        if _clean(value):
            return _clean(value)
    return ""


def _source_hash(value: str) -> str:
    return hashlib.sha256(_clean(value).encode("utf-8")).hexdigest()


def _normalize_ronghui_attachment_url(value: Any) -> str:
    raw = unescape(_clean(value)).replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("//"):
        candidate = f"https:{raw}"
    elif any(raw.lower().startswith(f"{host}/") for host in RONGHUI_DIRECT_ATTACHMENT_HOSTS):
        candidate = f"https://{raw}"
    else:
        candidate = urljoin(RONGHUI_ORIGIN, raw)
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host in RONGHUI_DIRECT_ATTACHMENT_HOSTS and parsed.scheme != "https":
        candidate = parsed._replace(scheme="https").geturl()
    return candidate


def _urls_from_value(value: Any, *, origin: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_urls_from_value(item, origin=origin))
        return output
    if isinstance(value, dict):
        output = []
        for item in value.values():
            output.extend(_urls_from_value(item, origin=origin))
        return output
    text = unescape(_clean(value))
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, (dict, list)):
        return _urls_from_value(parsed, origin=origin)
    output = []
    if origin == RONGHUI_ORIGIN:
        for match in RONGHUI_DIRECT_URL_RE.finditer(text):
            output.append(_normalize_ronghui_attachment_url(match.group(0)))
    for match in URL_RE.finditer(text):
        output.append(urljoin(origin, match.group(0)))
    if not output and ("/" in text or "." in text) and len(text) < 1024:
        parsed_url = urlparse(text)
        if parsed_url.scheme in {"http", "https"} or text.startswith("/"):
            output.append(urljoin(origin, text))
    return list(dict.fromkeys(output))


def _attachments_from_row(row: dict[str, Any], *, origin: str) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, value in row.items():
        if not ATTACHMENT_FIELD_RE.search(str(key)):
            continue
        for url in _urls_from_value(value, origin=origin):
            if url in seen:
                continue
            seen.add(url)
            attachments.append(
                {
                    "attachment_type": str(key),
                    "display_name": str(key),
                    "source_url": url,
                    "file_hash": _source_hash(url),
                    "mime_type": "",
                    "file_size": 0,
                    "uploaded_at": "",
                }
            )
    return attachments


def _dedupe_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attachment in attachments:
        source_url = _clean(attachment.get("source_url"))
        file_hash = _clean(attachment.get("file_hash"))
        key = source_url or file_hash
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(attachment)
    return output


def _attachment_name_from_path(source_url: str) -> str:
    path = urlparse(source_url).path or ""
    name = unquote(Path(path).name) if path else ""
    return name or "回单照片"


def _normalize_yunda_attachment_url(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    source_url = urljoin(YUNDA_INMS_ORIGIN, raw)
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path or "")
    suffix = Path(path).suffix.lower()
    if suffix and suffix not in YUNDA_IMAGE_SUFFIXES:
        return ""
    return parsed._replace(path=path).geturl()


def _yunda_address_attachments(row: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    groups = (
        ("Return_Adjunct", "电子回单"),
        ("Return_Sign_Adjunct", "已签电子回单"),
    )
    for prefix, display_name in groups:
        for index in range(1, 5):
            source_url = _normalize_yunda_attachment_url(_first(row, f"{prefix}_Addr{index}", f"{prefix}{index}"))
            if not source_url:
                continue
            attachments.append(
                {
                    "attachment_type": prefix,
                    "display_name": display_name,
                    "source_url": source_url,
                    "file_hash": _source_hash(source_url),
                    "mime_type": "",
                    "file_size": 0,
                    "uploaded_at": _first(row, "Signed_Elec_Update_Time", "Elec_Update_Time", "Return_Sign_Time", "Mail_Date"),
                }
            )
    return _dedupe_attachments(attachments)


def _ronghui_attachment_rows_to_attachments(rows: list[dict[str, Any]], *, attachment_type: str) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for row in rows:
        raw_path = _first(row, "FILE_PATH", "filePath", "file_url", "url")
        if not raw_path:
            continue
        source_url = _normalize_ronghui_attachment_url(raw_path)
        display_name = _first(row, "FILE_NAME", "fileName", "displayName") or _attachment_name_from_path(source_url)
        attachments.append(
            {
                "attachment_type": attachment_type,
                "display_name": display_name,
                "source_url": source_url,
                "file_hash": _source_hash(source_url),
                "mime_type": "",
                "file_size": _coerce_int(row.get("FILE_SIZE"), 0),
                "uploaded_at": _first(row, "UPLOAD_DATE", "CREATE_DATE", "uploaded_at"),
            }
        )
    return _dedupe_attachments(attachments)


def _parse_ronghui_datetime(value: Any) -> dt.datetime | None:
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _ronghui_process_pic_type(process_type: Any) -> str:
    text = _clean(process_type)
    if text == "寄方登记":
        return "0"
    if text == "派方登记":
        return "6"
    return ""


def _ronghui_system_pic_scan_payload(process_row: dict[str, Any]) -> dict[str, str]:
    bill_code = _first(process_row, "BILL_CODE", "BILLCODE", "BILL_NO")
    pic_type = _ronghui_process_pic_type(process_row.get("PROCESS_TYPE"))
    reply_time = _parse_ronghui_datetime(process_row.get("REPLY_TIME"))
    if not bill_code or not pic_type or reply_time is None:
        return {}
    start_time = (reply_time - dt.timedelta(minutes=1)).strftime("%Y/%m/%d %H:%M:%S")
    end_time = (reply_time + dt.timedelta(minutes=1)).strftime("%Y/%m/%d %H:%M:%S")
    return {
        "BILL_CODE": bill_code,
        "PIC_TYPE": pic_type,
        "CREATE_DATE": json.dumps({"start": start_time, "end": end_time}, ensure_ascii=False),
    }


def _fetch_ronghui_find_all(
    session: Any,
    call_id: str,
    *,
    data: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    headers: dict[str, str],
    timeout_sec: int,
) -> list[dict[str, Any]]:
    params = {"id": call_id}
    if query:
        params.update({str(key): value for key, value in query.items() if _clean(value)})
    if data is None:
        response = session.get(RONGHUI_FIND_ALL_URL, params=params, headers=headers, timeout=timeout_sec)
    else:
        response = session.post(RONGHUI_FIND_ALL_URL, params=params, data=data, headers=headers, timeout=timeout_sec)
    response.raise_for_status()
    return _extract_rows(response.json())


def _fetch_ronghui_process_rows(
    session: Any,
    row: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_sec: int,
) -> list[dict[str, Any]]:
    bill_code = _first(row, "BILL_CODE", "BILLCODE", "BILL_NO")
    if not bill_code:
        return []
    query = {"BILL_CODE": bill_code}
    receipt_no = _first(row, "R_BILLCODE", "RETURN_BILL_CODE", "RECEIPT_NO")
    if receipt_no:
        query["R_BILLCODE"] = receipt_no
    rows = _fetch_ronghui_find_all(
        session,
        RONGHUI_RECORD_CALL_ID,
        query=query,
        headers=headers,
        timeout_sec=timeout_sec,
    )
    return [item for item in rows if _first(item, "BILL_CODE", "BILLCODE", "BILL_NO") == bill_code]


def _fetch_ronghui_record_attachments(
    session: Any,
    row: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_sec: int,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for process_row in _fetch_ronghui_process_rows(session, row, headers=headers, timeout_sec=timeout_sec):
        if _first(process_row, "DATA_FROM") == "系统":
            payload = _ronghui_system_pic_scan_payload(process_row)
            if not payload:
                continue
            file_rows = _fetch_ronghui_find_all(
                session,
                RONGHUI_PIC_SCAN_ATTACHMENT_CALL_ID,
                data=payload,
                headers=headers,
                timeout_sec=timeout_sec,
            )
            attachments.extend(
                _ronghui_attachment_rows_to_attachments(
                    file_rows,
                    attachment_type=RONGHUI_PIC_SCAN_ATTACHMENT_CALL_ID,
                )
            )
            continue
        process_record_id = _first(process_row, "GUID", "PROCESS_RECORD_ID")
        if not process_record_id:
            continue
        file_rows = _fetch_ronghui_find_all(
            session,
            RONGHUI_RECORD_ATTACHMENT_CALL_ID,
            data={"PROCESS_RECORD_ID": process_record_id},
            headers=headers,
            timeout_sec=timeout_sec,
        )
        attachments.extend(
            _ronghui_attachment_rows_to_attachments(
                file_rows,
                attachment_type=RONGHUI_RECORD_ATTACHMENT_CALL_ID,
            )
        )
    return _dedupe_attachments(attachments)


def _ronghui_audit_status(row: dict[str, Any]) -> str:
    receipt_status = _first(row, "RETURNBILL_STATUS", "RETURN_BILL_STATUS", "回单状态")
    if receipt_status:
        return receipt_status
    return _first(row, "AUDIT_STATUS", "AUDIT_STATUS_TEXT", "审核状态")


def _normalize_ronghui_row(row: dict[str, Any], *, direction: str) -> dict[str, Any]:
    attachments = _attachments_from_row(row, origin=RONGHUI_ORIGIN)
    waybill_no = _first(row, "BILL_CODE", "BILLCODE", "BILL_NO", "运单编号(原)")
    receipt_no = _first(row, "R_BILLCODE", "RETURN_BILL_CODE", "RECEIPT_NO", "回单号")
    return_waybill_no = _first(row, "RH_BILL_CODE", "RETURN_BILLCODE", "运单编号(返回)")
    updated_at = _first(row, "AUDIT_TIME", "RH_SIGN_DATE", "RH_SEND_DATE", "SIGN_DATE", "SEND_DATE", "INSERT_DATE")
    return {
        "platform": "ronghui",
        "direction": direction,
        "waybill_no": waybill_no,
        "receipt_no": receipt_no,
        "return_waybill_no": return_waybill_no,
        "receipt_status": _first(row, "RETURNBILL_STATUS", "RETURN_BILL_STATUS", "回单状态"),
        "audit_status": _ronghui_audit_status(row),
        "photo_status": "已上传" if attachments else "未上传",
        "photo_count": len(attachments),
        "signed_confirmed": _first(row, "SIGN_STATUS", "SIGN_STATUS_TEXT", "签收状态"),
        "remote_updated_at": updated_at,
        "updated_at": updated_at,
        "raw_payload": row,
        "attachments": attachments,
    }


def _normalize_yunda_row(row: dict[str, Any], *, direction: str) -> dict[str, Any]:
    attachments = _dedupe_attachments([*_yunda_address_attachments(row), *_attachments_from_row(row, origin=YUNDA_INMS_ORIGIN)])
    waybill_no = _first(row, "Logistics_Id", "LogisticsId", "logisticsId", "waybill_no", "运单号")
    receipt_no = _first(row, "Return_Logistics_Id", "Return_Logistics", "ReturnLogisticsId", "ReceiptId", "ReceiptNo", "receipt_no", "回单号")
    return_waybill_no = _first(row, "Return_Express_Id", "ReturnExpressId", "ReturnWaybillNo", "回单快递单号")
    updated_at = _first(
        row,
        "Signed_Elec_Update_Time",
        "Elec_Update_Time",
        "Audit_Time",
        "AuditTime",
        "auditTime",
        "UpdateTime",
        "updated_at",
        "Return_Sign_Time",
        "Sign_Time",
        "Mail_Date",
        "审核时间",
    )
    return {
        "platform": "yunda",
        "direction": direction,
        "waybill_no": waybill_no,
        "receipt_no": receipt_no,
        "return_waybill_no": return_waybill_no,
        "receipt_status": _first(row, "Return_Status", "ReceiptStatus", "receiptStatus", "Sign_State", "Scan_State", "Cargo_Status", "回单状态"),
        "audit_status": _first(row, "Audit_Status_Name", "AuditStatusName", "AuditStatus", "auditStatus", "Is_Scan_Check", "审核状态"),
        "photo_status": "已上传" if attachments else "未上传",
        "photo_count": len(attachments),
        "signed_confirmed": _first(row, "Sign_Status_Name", "IsConfirmSign", "isConfirmSign", "Sign_State", "是否确认签收"),
        "remote_updated_at": updated_at,
        "updated_at": updated_at,
        "raw_payload": row,
        "attachments": attachments,
    }


def _ronghui_process_call_id(direction: str) -> str:
    return RONGHUI_PROCESS_CALL_ID_BY_DIRECTION.get(direction, RONGHUI_PROCESS_CALL_ID_BY_DIRECTION["send"])


def _ronghui_payload(
    params: dict[str, Any],
    *,
    direction: str,
    page_index: int,
    page_size: int,
    login_site_code: str = "",
) -> dict[str, Any]:
    start, end = _date_range(params)
    date_range = {"start": f"{start.replace('-', '/')} 00:00:00", "end": f"{end.replace('-', '/')} 23:59:59"}
    payload: dict[str, Any] = {
        "CODE_TYPE": _clean(params.get("code_type")) or "R_BILLCODE",
        "searchOrderInput": _clean(params.get("q") or params.get("waybill_no") or params.get("receipt_no")),
        "searchDateType": _clean(params.get("date_type")) or "SEND_DATE",
        "SEARCH_DATE_RANGE": json.dumps(date_range, ensure_ascii=False),
        "RETURNBILL_STATUS": _clean(params.get("receipt_status")),
        "SEND_DATE": json.dumps(date_range, ensure_ascii=False),
        "LOGIN_SITE_CODE": login_site_code,
        "pageIndex": str(page_index),
        "pageSize": str(page_size),
        "sortField": "",
        "sortOrder": "",
        "totalColumns": "[]",
    }
    if direction == "receive":
        payload["SEND_SITE_CODE"] = _clean(params.get("send_site_code") or params.get("SEND_SITE_CODE"))
    elif direction == "send":
        payload["DESTINATION_CODE"] = _clean(params.get("destination_code") or params.get("DESTINATION_CODE"))
    extra = params.get("extra_filters")
    if isinstance(extra, dict):
        payload.update({str(key): value for key, value in extra.items()})
    return payload


def _fetch_ronghui(params: dict[str, Any], *, direction: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_size = _coerce_int(params.get("page_size"), DEFAULT_PAGE_SIZE)
    max_pages = _coerce_int(params.get("max_pages"), DEFAULT_MAX_PAGES)
    timeout_sec = _coerce_int(params.get("timeout_sec"), 60)
    session_profile = _clean(params.get("session_profile")) or PRICE_SESSION_PROFILE
    session = get_session_broker(session_profile).build_requests_session(validate=False)
    login_site_code = _resolve_ronghui_login_site_code(session, params)
    call_id = _ronghui_process_call_id(direction)
    headers = {
        "Accept": "text/plain, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{RONGHUI_ORIGIN}/widget/home",
    }
    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page_index in range(max_pages):
        response = session.post(
            RONGHUI_DATA_QUERY_URL,
            params={"id": call_id},
            data=_ronghui_payload(
                params,
                direction=direction,
                page_index=page_index,
                page_size=page_size,
                login_site_code=login_site_code,
            ),
            headers=headers,
            timeout=timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        page_rows = _extract_rows(payload)
        rows.extend(page_rows)
        if total is None:
            total = _extract_total(payload)
        if not page_rows or (total is not None and len(rows) >= total):
            break
    normalized_records: list[dict[str, Any]] = []
    attachment_errors = 0
    for row in rows:
        record = _normalize_ronghui_row(row, direction=direction)
        try:
            process_attachments = _fetch_ronghui_record_attachments(
                session,
                row,
                headers=headers,
                timeout_sec=timeout_sec,
            )
        except Exception:
            process_attachments = []
            attachment_errors += 1
        attachments = _dedupe_attachments([*(record.get("attachments") or []), *process_attachments])
        if attachments:
            record["attachments"] = attachments
            record["photo_count"] = len(attachments)
            record["photo_status"] = "已上传"
        normalized_records.append(record)
    return normalized_records, {
        "fetched": len(rows),
        "total": total,
        "max_pages": max_pages,
        "truncated": total is not None and len(rows) < total,
        "attachment_errors": attachment_errors,
    }


def _select_yunda_datagrid_url(html: str, page_url: str, *, direction: str) -> str:
    if not html:
        return ""
    marker_positions = [
        idx
        for marker in ('$("#dg")', "$('#dg')", '"#dg"', "'#dg'", 'id="dg"', "id='dg'")
        for idx in [html.find(marker)]
        if idx >= 0
    ]
    regions: list[str] = []
    if marker_positions:
        start = min(marker_positions)
        regions.append(html[start : start + 80000])
    regions.append(html)
    required_path = YUNDA_RECEIPT_PATH_MARKER_BY_DIRECTION.get(direction, "")
    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for region_index, region in enumerate(regions):
        for match in YUNDA_DATAGRID_URL_RE.finditer(region):
            raw_url = _clean(match.group("url"))
            if not raw_url:
                continue
            absolute_url = urljoin(page_url, raw_url)
            lowered = absolute_url.lower()
            if required_path and required_path.lower() not in lowered:
                continue
            if any(marker in lowered for marker in YUNDA_IGNORED_URL_MARKERS):
                continue
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            candidates.append((_score_yunda_datagrid_url(absolute_url), region_index, absolute_url))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _discover_yunda_datagrid_url(session: Any, *, direction: str, timeout_sec: int) -> str:
    page_url = YUNDA_RECEIPT_PAGE_BY_DIRECTION[direction]
    response = session.get(page_url, timeout=timeout_sec)
    response.raise_for_status()
    html = response.text or ""
    return _select_yunda_datagrid_url(html, page_url, direction=direction)


def _score_yunda_datagrid_url(url: str) -> int:
    lowered = url.lower()
    path = urlparse(url).path.lower()
    score = 0
    if re.search(r"/(?:get)?list(?:\.|/)", path):
        score += 100
    if "/detail" in lowered:
        score -= 100
    return score


def _split_yunda_query_values(value: Any) -> list[str]:
    raw = _clean(value)
    if not raw:
        return []
    return [part for part in YUNDA_QUERY_SPLIT_RE.split(raw) if part]


def _resolve_yunda_single_query(params: dict[str, Any]) -> tuple[str, list[str]]:
    explicit_fields = (
        ("waybill_no", "LogisticsId"),
        ("receipt_no", "Return_Logistics_Id"),
        ("return_waybill_no", "Return_Express_Id"),
    )
    for param_key, yunda_field in explicit_fields:
        values = _split_yunda_query_values(params.get(param_key))
        if values:
            return yunda_field, values

    values = _split_yunda_query_values(params.get("q"))
    if not values:
        return "", []
    if all(re.fullmatch(r"\d{9}", value) for value in values):
        return "LogisticsId", values
    if all(re.fullmatch(r"\d{13}|\d{15}", value) for value in values):
        return "Return_Logistics_Id", values
    return "LogisticsId", values


def _yunda_payload(params: dict[str, Any], *, page: int, rows: int) -> dict[str, Any]:
    query_field, query_values = _resolve_yunda_single_query(params)
    if query_field and query_values:
        payload: dict[str, Any] = {"page": page, "rows": rows, query_field: query_values}
        extra = params.get("extra_filters")
        if isinstance(extra, dict):
            payload.update({str(key): value for key, value in extra.items()})
        return payload

    start, end = _date_range(params)
    payload: dict[str, Any] = {
        "page": page,
        "rows": rows,
        "timeType": _clean(params.get("timeType") or params.get("date_type")) or "0",
        "start_date": start,
        "start_time": _clean(params.get("start_time")) or "00:00:00",
        "end_date": end,
        "end_time": _clean(params.get("end_time")) or "23:59:59",
        "Return_Logistics_Status": _clean(params.get("receipt_status")) or "3",
        "Return_Adjunct_Addr": _clean(params.get("receipt_attachment_status")) or "all",
        "Return_Sign_Adjunct_Addr": _clean(params.get("signed_receipt_attachment_status")) or "all",
        "Is_Replace": _clean(params.get("replace_status")) or "all",
    }
    extra = params.get("extra_filters")
    if isinstance(extra, dict):
        payload.update({str(key): value for key, value in extra.items()})
    return payload


def _fetch_yunda(params: dict[str, Any], *, direction: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    warnings: list[str] = []
    page_size = _coerce_int(params.get("page_size"), DEFAULT_PAGE_SIZE)
    max_pages = _coerce_int(params.get("max_pages"), DEFAULT_MAX_PAGES)
    timeout_sec = _coerce_int(params.get("timeout_sec"), 60)
    session = get_session_broker("yunda").build_requests_session(validate=False)
    data_url = _clean(params.get("yunda_datagrid_url") or params.get("datagrid_url"))
    if not data_url:
        data_url = _discover_yunda_datagrid_url(session, direction=direction, timeout_sec=timeout_sec)
    if not data_url:
        warnings.append("Yunda datagrid URL was not discoverable from the receipt page; open the page and pass datagrid_url from $('#dg').datagrid('options').url.")
        return [], {"fetched": 0, "total": None}, warnings

    rows: list[dict[str, Any]] = []
    total: int | None = None
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": YUNDA_RECEIPT_PAGE_BY_DIRECTION[direction],
    }
    for page in range(1, max_pages + 1):
        response = session.post(
            data_url,
            data=_yunda_payload(params, page=page, rows=page_size),
            headers=headers,
            timeout=timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        page_rows = _extract_rows(payload)
        rows.extend(page_rows)
        if total is None:
            total = _extract_total(payload)
        if not page_rows or (total is not None and len(rows) >= total):
            break
    return [_normalize_yunda_row(row, direction=direction) for row in rows], {
        "fetched": len(rows),
        "total": total,
        "max_pages": max_pages,
        "truncated": total is not None and len(rows) < total,
    }, warnings


def _directions(params: dict[str, Any]) -> list[str]:
    direction = _clean(params.get("direction")).lower()
    if direction in {"send", "receive"}:
        return [direction]
    return ["send", "receive"]


def _platforms(params: dict[str, Any]) -> list[str]:
    platform = _clean(params.get("platform")).lower()
    if platform in {"yunda", "ronghui"}:
        return [platform]
    return ["yunda", "ronghui"]


def _record_key(record: dict[str, Any]) -> str:
    return "|".join(
        [
            _clean(record.get("platform")),
            _clean(record.get("direction")),
            _clean(record.get("waybill_no")),
            _clean(record.get("receipt_no")),
        ]
    )


def _fetch_source(params: dict[str, Any], *, platform: str, direction: str) -> tuple[str, str, list[dict[str, Any]], dict[str, Any], list[str]]:
    if platform == "ronghui":
        fetched_records, stats = _fetch_ronghui(params, direction=direction)
        return platform, direction, fetched_records, stats, []
    fetched_records, stats, yunda_warnings = _fetch_yunda(params, direction=direction)
    return platform, direction, fetched_records, stats, yunda_warnings


def _truncation_warning(platform: str, direction: str, stats: dict[str, Any]) -> str:
    if not stats.get("truncated"):
        return ""
    fetched = stats.get("fetched")
    total = stats.get("total")
    max_pages = stats.get("max_pages")
    return f"{platform}/{direction} reached max_pages={max_pages}; returned {fetched} of {total} rows."


def run_once(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(params or {})
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    source_stats: list[dict[str, Any]] = []
    sources = [
        (platform, direction)
        for platform in _platforms(params)
        for direction in _directions(params)
        if not (platform == "yunda" and direction == "receive")
    ]
    max_workers = min(len(sources) or 1, _coerce_int(params.get("source_workers"), 4))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_source = {
            executor.submit(_fetch_source, params, platform=platform, direction=direction): (platform, direction)
            for platform, direction in sources
        }
        for future in as_completed(future_to_source):
            platform, direction = future_to_source[future]
            try:
                platform, direction, fetched_records, stats, source_warnings = future.result()
                warnings.extend(source_warnings)
            except TMSAuthStateError:
                raise
            except Exception as exc:
                warnings.append(f"{platform}/{direction} sync failed: {type(exc).__name__}: {str(exc)[:200]}")
                fetched_records, stats = [], {"fetched": 0, "total": None}
            truncation = _truncation_warning(platform, direction, stats)
            if truncation:
                warnings.append(truncation)
            records.extend(fetched_records)
            source_stats.append({"platform": platform, "direction": direction, **stats})

    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        if not (_clean(record.get("waybill_no")) or _clean(record.get("receipt_no"))):
            continue
        key = _record_key(record)
        deduped[key] = record

    attachment_count = sum(len(record.get("attachments") or []) for record in deduped.values())
    return {
        "ok": True,
        "records": list(deduped.values()),
        "stats": {
            "fetched": len(records),
            "deduped": len(deduped),
            "attachments": attachment_count,
            "sources": source_stats,
        },
        "warnings": warnings,
    }
