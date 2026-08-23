"""Unified customer-service problem item adapter for Ronghui and Yunda."""

from __future__ import annotations

import base64
import datetime as dt
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from agent.tms_runtime.account_contracts import PRICE_SESSION_PROFILE

try:
    from agent.tms_runtime.errors import TMSAuthStateError
except Exception:  # pragma: no cover - keeps direct script tests lightweight.
    class TMSAuthStateError(RuntimeError):
        def __init__(self, code: str, message: str):
            super().__init__(message)
            self.code = str(code or "AUTH_REQUIRED").strip() or "AUTH_REQUIRED"


RONGHUI_ORIGIN = "https://tms.ronghuiwl.com"
RONGHUI_MENU_URL = f"{RONGHUI_ORIGIN}/menuTreeExtend/loadMenu"
RONGHUI_INDEX_URL = f"{RONGHUI_ORIGIN}/module/index?mv=index"
RONGHUI_FIND_ALL_URL = f"{RONGHUI_ORIGIN}/dataQuery/findAllByCallId"
RONGHUI_SAVE_TABLES_URL = f"{RONGHUI_ORIGIN}/dataOperation/saveTables"
RONGHUI_UPLOAD_URL = f"{RONGHUI_ORIGIN}/file/upload?sysFileUploadId=ALL"
RONGHUI_PROBLEM_PIC_SCAN_CALL_ID = "FIND_PIC_SCAN_BY_BILL_CODE"

YUNDA_ORIGIN = "https://kyproblem.yunda56.com"
YUNDA_PUBLIC_ROOT = f"{YUNDA_ORIGIN}/ky_problem/public/index.php"
YUNDA_QUERY_LIST_URL = f"{YUNDA_PUBLIC_ROOT}/query/list.html"
YUNDA_QUERY_DETAIL_URL = f"{YUNDA_PUBLIC_ROOT}/query/listDetail.html"
YUNDA_QUERY_READ_URL = f"{YUNDA_PUBLIC_ROOT}/query/Read.html"
YUNDA_QUERY_REPLY_URL = f"{YUNDA_PUBLIC_ROOT}/query/replyInfo.html"
YUNDA_ISSUE_LIST_URL = f"{YUNDA_PUBLIC_ROOT}/issue/list.html"
YUNDA_ISSUE_SAVE_URL = f"{YUNDA_PUBLIC_ROOT}/issue/save.html"
YUNDA_UPLOAD_URL = f"{YUNDA_PUBLIC_ROOT}/issue/uploadImg.html"
SHANGHAI_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
YUNDA_ISSUE_DEFAULT_LOOKBACK_DAYS = 2

SUPPORTED_PLATFORMS = {"ronghui", "yunda"}
SUPPORTED_ACTIONS = {"query", "detail", "mark_read", "reply", "publish", "upload_attachment", "fetch_attachment"}
RONGHUI_QUERY_MENU_BY_DIRECTION = {
    "registered": "登记问题件查询",
    "sent": "登记问题件查询",
    "receive": "收到问题件查询",
    "received": "收到问题件查询",
    "inbox": "收到问题件查询",
}
RONGHUI_PUBLISH_MENU_TEXT = "问题件录入"

RONGHUI_QUERY_DIRECTIONS = {
    "登记问题件查询": "registered",
    "收到问题件查询": "received",
}
YUNDA_PUBLISHED_DIRECTIONS = {"issue", "publish", "published", "sent"}

SENSITIVE_RESULT_KEYS = {
    "password",
    "cookie",
    "cookies",
    "token",
    "authorization",
    "authenticationkey",
    "authentication_key",
    "pageid",
    "page_id",
    "sso_uid",
    "kyflag",
    "q",
}


class CustomerServiceProblemError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: str = "failed"):
        super().__init__(message)
        self.code = str(code or "CUSTOMER_SERVICE_PROBLEM_ERROR").strip() or "CUSTOMER_SERVICE_PROBLEM_ERROR"
        self.status = str(status or "failed").strip() or "failed"


def get_session_broker(profile_name: str):
    from agent.tms_runtime.session_broker import get_session_broker as build_broker

    return build_broker(profile_name)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


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


_UNREPLIED_STATUS_RE = re.compile(r"(未回复|未回|待回复|暂无回复|无回复)")
_EMPTY_REPLY_TEXTS = {"0", "-", "无", "暂无", "暂无回复", "无回复"}


def _has_problem_reply(row: dict[str, Any], reply_text: str) -> bool:
    count_text = _first_text(row, "reply_count", "REPLY_COUNT")
    if count_text:
        try:
            if float(count_text) > 0:
                return True
        except ValueError:
            pass
    text = _clean_text(reply_text)
    if not text or text in _EMPTY_REPLY_TEXTS or _UNREPLIED_STATUS_RE.search(text):
        return False
    return True


def _display_problem_status(status: str, row: dict[str, Any], reply_text: str) -> str:
    text = _clean_text(status)
    if _has_problem_reply(row, reply_text) and (not text or _UNREPLIED_STATUS_RE.search(text)):
        return "已回复"
    return text


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _bool_param(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return bool(value)


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_RESULT_KEYS:
                continue
            result[str(key)] = _safe_json(item)
        return result
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _error_result(code: str, message: str, *, status: str = "failed", platform: str = "", action: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "error_code": _clean_text(code) or "CUSTOMER_SERVICE_PROBLEM_ERROR",
        "message": _clean_text(message) or "客服问题件处理失败。",
        "platform": platform,
        "action": action,
    }


def _extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for candidate in (
        payload.get("rows"),
        payload.get("data"),
        payload.get("list"),
        payload.get("records"),
        data.get("rows"),
        data.get("list"),
        data.get("records"),
        data.get("items"),
    ):
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def _extract_declared_total(payload: Any) -> int | None:
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for value in (payload.get("total"), data.get("total"), payload.get("count"), data.get("count")):
            if value in (None, ""):
                continue
            try:
                parsed = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            if parsed < 0:
                continue
            return parsed
    return None


def _extract_total(payload: Any, rows: list[dict[str, Any]]) -> int:
    declared = _extract_declared_total(payload)
    return declared if declared is not None else len(rows)


def _js_unescape(value: str) -> str:
    if not value:
        return ""

    def replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"%u([0-9A-Fa-f]{4})", replace_unicode, str(value))
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
        decoded = _js_unescape(raw)
        payload = json.loads(decoded)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_profile(platform: str, params: dict[str, Any]) -> str:
    profile = _clean_text(params.get("session_profile"))
    if profile:
        return profile
    return "yunda" if platform == "yunda" else PRICE_SESSION_PROFILE


def _build_session(platform: str, params: dict[str, Any]) -> Any:
    broker = get_session_broker(_session_profile(platform, params))
    return broker.build_requests_session(validate=not _bool_param(params.get("skip_session_validate"), default=False))


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


def _normalize_direction(value: Any, *, platform: str) -> str:
    text = _clean_text(value).lower()
    if not text:
        return "received" if platform == "ronghui" else "query"
    to_me_direction = "received" if platform == "ronghui" else "query"
    my_published_direction = "registered" if platform == "ronghui" else "published"
    aliases = {
        "published_to_me": to_me_direction,
        "published-to-me": to_me_direction,
        "to_me": to_me_direction,
        "to-me": to_me_direction,
        "发布给我的": to_me_direction,
        "my_published": my_published_direction,
        "my-published": my_published_direction,
        "published_by_me": my_published_direction,
        "published-by-me": my_published_direction,
        "我发布的": my_published_direction,
        "receive": "received",
        "received": "received",
        "inbox": "received",
        "收到": "received",
        "registered": "registered",
        "sent": "registered" if platform == "ronghui" else "sent",
        "登记": "registered",
        "query": "query",
        "published": "published",
        "publish": "published",
        "issue": "published",
    }
    return aliases.get(text, text)


def normalize_problem_rows(
    platform: str,
    rows: list[dict[str, Any]],
    *,
    account_id: str,
    account_label: str,
    source_direction: str,
) -> list[dict[str, Any]]:
    normalized_platform = _normalize_platform(platform)
    if normalized_platform not in SUPPORTED_PLATFORMS:
        raise CustomerServiceProblemError("UNSUPPORTED_PLATFORM", f"不支持的平台：{platform}")

    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if normalized_platform == "ronghui":
            external_id = _first_text(row, "GUID")
            waybill_no = _first_text(row, "BILL_CODE", "bill_code")
            status = _first_text(row, "REVERSION_STATUS", "BL_CHECKOK_STR", "BL_RETURN", "IS_REPLY")
            problem_type = _first_text(row, "TYPE")
            problem_text = _first_text(row, "PROBLEM_CAUSE")
            reply_text = _first_text(row, "REVERSION", "DEAL_RESULT")
            created_at = _first_text(row, "REGISTER_DATE", "REGISTER_SAVE_DATE")
            registered_at = _first_text(row, "REGISTER_DATE")
            registration_saved_at = _first_text(row, "REGISTER_SAVE_DATE")
            registered_site = _first_text(row, "REGISTER_SITE")
            updated_at = _first_text(row, "REVERSION_DATE")
            problem_type = _first_text(row, "TYPE")
            registered_at = _first_text(row, "REGISTER_DATE")
            registration_saved_at = _first_text(row, "REGISTER_SAVE_DATE")
            registered_site = _first_text(row, "REGISTER_SITE")
        else:
            external_id = _first_text(row, "prob_main_id")
            waybill_no = _first_text(row, "ship_no", "LogisticsId")
            status = _first_text(row, "prob_status", "check_status", "issue_check_status")
            problem_text = _first_text(row, "prob_text")
            problem_type = _first_text(row, "prob_type", "issue_type")
            reply_text = _first_text(row, "reply_text")
            created_at = _first_text(row, "created_time")
            registered_at = created_at
            registration_saved_at = ""
            registered_site = _first_text(row, "register_site", "site_name")
            updated_at = _first_text(row, "modified_time", "reply_time")
            problem_type = _first_text(row, "problem_type", "prob_type")
            registered_at = created_at
            registration_saved_at = created_at
            registered_site = _first_text(row, "register_site", "site_name")
        if not external_id:
            raise CustomerServiceProblemError(
                "MISSING_EXTERNAL_ID",
                f"{normalized_platform} 问题件第 {index + 1} 行缺少唯一键，已停止处理。",
            )
        output.append(
            {
                "platform": normalized_platform,
                "account_id": _clean_text(account_id),
                "account_label": _clean_text(account_label) or _clean_text(account_id),
                "source_direction": _clean_text(source_direction),
                "external_id": external_id,
                "waybill_no": waybill_no,
                "status": _display_problem_status(status, row, reply_text),
                "problem_type": problem_type,
                "problem_text": problem_text,
                "reply_text": reply_text,
                "created_at": created_at,
                "registered_at": registered_at,
                "registration_saved_at": registration_saved_at,
                "registered_site": registered_site,
                "updated_at": updated_at,
                "problem_type": problem_type,
                "registered_at": registered_at,
                "registration_saved_at": registration_saved_at,
                "registered_site": registered_site,
                "raw": _safe_json(row),
            }
        )
    return output


def _safe_business_item(params: dict[str, Any]) -> dict[str, Any]:
    item = params.get("item")
    return item if isinstance(item, dict) else {}


def _safe_payload(params: dict[str, Any]) -> dict[str, Any]:
    payload = params.get("payload")
    return payload if isinstance(payload, dict) else {}


def build_ronghui_save_tables_envelope(operation_key: str, row: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = row if isinstance(row, list) else [row]
    return [
        {
            "beforeAction": None,
            "operationKey": _clean_text(operation_key),
            "afterAction": None,
            "idFields": [],
            "data": rows,
        }
    ]


def _extract_between(text: str, start_token: str, end_token: str) -> str:
    start = text.find(start_token)
    if start == -1:
        return ""
    start += len(start_token)
    end = text.find(end_token, start)
    if end == -1:
        return ""
    return text[start:end]


def _walk_menu(nodes: list[dict[str, Any]], path: str = ""):
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        text = _clean_text(node.get("text") or node.get("name") or node.get("title"))
        new_path = f"{path}/{text}" if path and text else text or path
        yield node, new_path
        children = node.get("children") or node.get("items") or []
        if isinstance(children, list):
            yield from _walk_menu(children, new_path)


def _menu_nodes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            return _menu_nodes(json.loads(text))
        except Exception:
            return []
    if not isinstance(payload, dict):
        return []
    raw_result = payload.get("result")
    if isinstance(raw_result, (list, str)):
        return _menu_nodes(raw_result)
    result = raw_result if isinstance(raw_result, dict) else {}
    candidates = (
        result.get("data"),
        payload.get("data"),
        payload.get("rows"),
        result.get("children"),
        payload.get("children"),
        result.get("items"),
        payload.get("items"),
        result.get("menus"),
        payload.get("menus"),
    )
    for data in candidates:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, str):
            nested = _menu_nodes(data)
            if nested:
                return nested
        if isinstance(data, dict):
            nested = _menu_nodes(data)
            if nested:
                return nested
    if any(key in payload for key in ("text", "name", "title")):
        return [payload]
    return []


def _response_json(response: Any, *, label: str) -> Any:
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    try:
        return response.json()
    except Exception as exc:
        body = _clean_text(getattr(response, "text", ""))
        raise CustomerServiceProblemError("INVALID_RESPONSE", f"{label} 接口返回非 JSON：{body[:120]}") from exc


def _ronghui_menu_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": RONGHUI_ORIGIN,
        "Referer": RONGHUI_INDEX_URL,
        "X-Requested-With": "XMLHttpRequest",
    }


def _resolve_ronghui_page_context(session: Any, menu_text: str) -> dict[str, str]:
    if hasattr(session, "request"):
        response = session.request("POST", RONGHUI_MENU_URL, headers=_ronghui_menu_headers(), timeout=20)
    else:
        response = session.post(RONGHUI_MENU_URL, headers=_ronghui_menu_headers(), timeout=20)
    payload = _response_json(response, label="融辉菜单")
    candidates: list[tuple[str, str]] = []
    for node, path in _walk_menu(_menu_nodes(payload)):
        text = _clean_text(node.get("text") or node.get("name") or node.get("title"))
        url = _clean_text(node.get("url") or node.get("href"))
        if not url or "/widget/home" not in url:
            continue
        if text == menu_text or path.endswith(f"/{menu_text}"):
            candidates.append((url, path))
    unique_urls = []
    for url, path in candidates:
        full_url = urljoin(RONGHUI_ORIGIN, url)
        if full_url not in unique_urls:
            unique_urls.append(full_url)
    if not unique_urls:
        raise CustomerServiceProblemError("PAGE_CONTEXT_NOT_FOUND", f"融辉菜单未找到：{menu_text}")
    if len(unique_urls) > 1:
        raise CustomerServiceProblemError("AMBIGUOUS_PAGE_CONTEXT", f"融辉菜单匹配到多个 {menu_text} 页面，停止猜测。")

    page_url = unique_urls[0]
    page_response = session.get(page_url, timeout=20)
    if hasattr(page_response, "raise_for_status"):
        page_response.raise_for_status()
    html = str(getattr(page_response, "text", "") or "")
    query = parse_qs(urlparse(page_url).query, keep_blank_values=True)
    authentication_key = _clean_text((query.get("authenticationKey") or [""])[0])
    page_id = _clean_text((query.get("pageId") or [""])[0])
    authentication_key = authentication_key or _extract_between(html, 'authenticationKey:"', '"')
    page_id = page_id or _extract_between(html, 'pageId:"', '"')
    if not authentication_key or not page_id:
        raise CustomerServiceProblemError("PAGE_CONTEXT_INCOMPLETE", f"融辉 {menu_text} 页面缺少 authenticationKey/pageId。")
    return {
        "menu_text": menu_text,
        "url": page_url,
        "html": html,
        "authentication_key": authentication_key,
        "page_id": page_id,
    }


def _ronghui_headers(page_context: dict[str, str], *, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": RONGHUI_ORIGIN,
        "Referer": page_context.get("url") or f"{RONGHUI_ORIGIN}/widget/home",
        "X-Requested-With": "XMLHttpRequest",
        "authenticationKey": _clean_text(page_context.get("authentication_key")),
        "pageId": _clean_text(page_context.get("page_id")),
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


RONGHUI_PROBLEM_GRID_FIELDS = (
    "GUID",
    "BILL_CODE",
    "PROBLEM_CAUSE",
    "REVERSION",
    "REGISTER_DATE",
    "REGISTER_SAVE_DATE",
    "BL_CHECKOK",
)


def _ronghui_grid_candidate_score(html: str, start: int, end: int) -> int:
    tag_start = html.rfind("<", 0, start)
    tag_end = html.find(">", end)
    local = html[tag_start : tag_end + 1] if tag_start >= 0 and tag_end >= end else html[start:end]
    window = html[max(0, start - 900) : min(len(html), end + 900)]
    local_lower = local.lower()
    score = 0
    if re.search(r"\bid\s*=\s*(['\"])datagrid\1", local, re.I):
        score += 100
    if re.search(r"\bname\s*=\s*(['\"])datagrid\1", local, re.I):
        score += 80
    if re.search(r"mini\.get\(\s*(['\"])datagrid\1\s*\)", local):
        score += 80
    if "mini-datagrid" in local_lower or "type=\"datagrid\"" in local_lower or "type='datagrid'" in local_lower:
        score += 15
    score += sum(8 for field in RONGHUI_PROBLEM_GRID_FIELDS if field in window)
    return score


def _select_ronghui_grid_url(page_context: dict[str, str]) -> str:
    html = str(page_context.get("html") or "")
    candidates: list[tuple[str, int]] = []
    for match in re.finditer(r"(?P<quote>['\"])(?P<url>(?:https?://tms\.ronghuiwl\.com)?/dataQuery/findPageByCallId\?id=[^'\"]+)(?P=quote)", html):
        candidates.append((urljoin(RONGHUI_ORIGIN, match.group("url")), _ronghui_grid_candidate_score(html, match.start(), match.end())))
    for match in re.finditer(r"(?:url|data-url)\s*[:=]\s*(?P<quote>['\"])(?P<url>[^'\"]*findPageByCallId\?id=[^'\"]+)(?P=quote)", html):
        candidates.append((urljoin(RONGHUI_ORIGIN, match.group("url")), _ronghui_grid_candidate_score(html, match.start(), match.end())))
    by_url: dict[str, int] = {}
    for item, score in candidates:
        item = item.replace("\\/", "/")
        by_url[item] = max(by_url.get(item, 0), score)
    if not by_url:
        raise CustomerServiceProblemError("GRID_URL_NOT_FOUND", f"融辉 {page_context.get('menu_text')} 页面未解析到 grid 查询 URL。")
    if len(by_url) == 1:
        return next(iter(by_url))

    scored = sorted(by_url.items(), key=lambda item: item[1], reverse=True)
    top_url, top_score = scored[0]
    runner_up = scored[1][1]
    if top_score > 0 and top_score > runner_up:
        return top_url
    raise CustomerServiceProblemError(
        "AMBIGUOUS_GRID_URL",
        f"融辉 {page_context.get('menu_text')} 页面出现多个 grid URL，未找到唯一主问题件 datagrid。",
    )


def _date_range(filters: dict[str, Any]) -> tuple[str, str, str]:
    today = dt.datetime.now().date()
    start = _clean_text(filters.get("date_from") or filters.get("start_date") or today.strftime("%Y/%m/%d")).replace("-", "/")
    end = _clean_text(filters.get("date_to") or filters.get("end_date") or today.strftime("%Y/%m/%d")).replace("-", "/")
    start_time = _clean_text(filters.get("start_time") or "00:00:00")
    end_time = _clean_text(filters.get("end_time") or "23:59:59")
    start_text = f"{start} {start_time}"
    end_text = f"{end} {end_time}"
    range_value = json.dumps({"start": start_text, "end": end_text}, ensure_ascii=False, separators=(",", ":"))
    return start_text, end_text, range_value


def _resolve_ronghui_login_site_code(session: Any, filters: dict[str, Any]) -> str:
    explicit = _clean_text(
        filters.get("LOGIN_SITE_CODE")
        or filters.get("login_site_code")
        or filters.get("loginSiteCode")
        or filters.get("site_code")
    )
    if explicit:
        return explicit
    user_info = _read_user_info_cookie(session)
    return _first_text(user_info, "loginSiteCode", "siteCode", "loginOwnerSiteCode")


def build_ronghui_query_payload(filters: dict[str, Any], *, direction: str, login_site_code: str = "") -> dict[str, Any]:
    filters = filters if isinstance(filters, dict) else {}
    _start, _end, range_text = _date_range(filters)
    page_size = _coerce_int(filters.get("page_size") or filters.get("rows"), default=50, minimum=1, maximum=200)
    raw_page_index = filters.get("pageIndex")
    if raw_page_index not in (None, ""):
        page_index = _coerce_int(raw_page_index, default=0, minimum=0, maximum=9999)
    else:
        page_number = _coerce_int(filters.get("page"), default=1, minimum=1, maximum=10000)
        page_index = max(page_number - 1, 0)
    send_site_code = _clean_text(filters.get("SEND_SITE_CODE") or filters.get("send_site_code"))
    if not send_site_code and direction != "registered":
        send_site_code = _clean_text(login_site_code)
    common = {
        "searchBillType": _clean_text(filters.get("searchBillType") or "BILL_CODE"),
        "searchOrderInput": _clean_text(filters.get("q") or filters.get("waybill_no") or filters.get("BILL_CODE")),
        "searchDateType": _clean_text(filters.get("searchDateType") or "REGISTER_DATE"),
        "SEARCH_DATE_RANGE": range_text,
        "REGISTER_DATE": range_text,
        "BL_RETURN": _clean_text(filters.get("BL_RETURN")),
        "SEND_SITE_CODE": send_site_code,
        "SEND_SITE": _clean_text(filters.get("SEND_SITE") or filters.get("send_site")),
        "LOGIN_SITE_CODE": _clean_text(
            filters.get("LOGIN_SITE_CODE") or filters.get("login_site_code") or filters.get("loginSiteCode")
        ),
        "pageIndex": page_index,
        "pageSize": page_size,
        "sortField": _clean_text(filters.get("sortField")),
        "sortOrder": _clean_text(filters.get("sortOrder")),
        "totalColumns": _clean_text(filters.get("totalColumns") or "[]"),
    }
    if direction == "registered":
        common.update(
            {
                "BL_REPLY": _clean_text(filters.get("BL_REPLY") or filters.get("is_replied")),
                "REGISTER_SITE_CODE": _clean_text(filters.get("REGISTER_SITE_CODE") or filters.get("register_site_code")),
                "REGISTER_SITE": _clean_text(filters.get("REGISTER_SITE") or filters.get("register_site")),
                "REGISTER_MAN_CODE": _clean_text(filters.get("REGISTER_MAN_CODE") or filters.get("register_man_code")),
            }
        )
    else:
        common.update(
            {
                "IS_REPLY": _clean_text(filters.get("IS_REPLY") or filters.get("is_replay") or filters.get("is_reply")),
                "BL_CHECKOK": _clean_text(filters.get("BL_CHECKOK") or filters.get("check_status")),
                "REGISTER_SITE_CODE": _clean_text(filters.get("REGISTER_SITE_CODE") or filters.get("register_site_code")),
                "REGISTER_SITE": _clean_text(filters.get("REGISTER_SITE") or filters.get("register_site")),
            }
        )
    return common


def _raise_if_source_failed(payload: Any, *, label: str, code: str = "SOURCE_QUERY_FAILED") -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("success") is False or str(payload.get("success")).lower() == "false":
        message = _clean_text(payload.get("message") or payload.get("msg") or "原系统返回失败。")
        raise CustomerServiceProblemError(code, f"{label}失败：{message}")


def _ronghui_query(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    filters = params.get("filters") if isinstance(params.get("filters"), dict) else {}
    direction = _normalize_direction(filters.get("direction") or params.get("direction"), platform="ronghui")
    menu_text = RONGHUI_QUERY_MENU_BY_DIRECTION.get(direction)
    if not menu_text:
        raise CustomerServiceProblemError("INVALID_DIRECTION", f"融辉不支持的问题件方向：{direction}")
    page_context = _resolve_ronghui_page_context(session, menu_text)
    grid_url = _clean_text(filters.get("grid_url")) or _select_ronghui_grid_url(page_context)
    query_payload = build_ronghui_query_payload(
        filters,
        direction=RONGHUI_QUERY_DIRECTIONS.get(menu_text, direction),
        login_site_code=_resolve_ronghui_login_site_code(session, filters),
    )
    response = session.post(
        grid_url,
        data=query_payload,
        headers=_ronghui_headers(page_context, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
        timeout=30,
    )
    payload = _response_json(response, label="融辉问题件查询")
    _raise_if_source_failed(payload, label="融辉问题件查询")
    raw_rows = _extract_rows(payload)
    rows = normalize_problem_rows(
        "ronghui",
        raw_rows,
        account_id=_clean_text(params.get("account_id")),
        account_label=_clean_text(params.get("account_label")),
        source_direction=RONGHUI_QUERY_DIRECTIONS.get(menu_text, direction),
    )
    declared_total = _extract_declared_total(payload)
    return {
        "ok": True,
        "rows": rows,
        "stats": {
            "total": declared_total if declared_total is not None else len(raw_rows),
            "returned": len(rows),
            "total_authoritative": declared_total is not None,
        },
    }


def _save_ronghui_tables(session: Any, page_context: dict[str, str], envelope: list[dict[str, Any]]) -> dict[str, Any]:
    params_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    response = session.post(
        RONGHUI_SAVE_TABLES_URL,
        files={"params": (None, params_json)},
        headers=_ronghui_headers(page_context),
        timeout=60,
    )
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    if not getattr(response, "content", b""):
        return {"success": False, "message": "原系统保存接口返回空响应。"}
    try:
        return response.json()
    except Exception:
        return {"success": False, "message": _clean_text(getattr(response, "text", ""))[:500]}


def _require_external_id(item: dict[str, Any], *, platform: str) -> str:
    external_id = _first_text(item, "external_id", "GUID", "prob_main_id")
    if not external_id:
        raise CustomerServiceProblemError("MISSING_EXTERNAL_ID", f"{platform} 问题件缺少 external_id。")
    return external_id


def _ronghui_query_menu_text_for_item(item: dict[str, Any], params: dict[str, Any]) -> str:
    filters = params.get("filters") if isinstance(params.get("filters"), dict) else {}
    direction = _normalize_direction(
        item.get("source_direction") or params.get("direction") or filters.get("direction"),
        platform="ronghui",
    )
    return RONGHUI_QUERY_MENU_BY_DIRECTION.get(direction) or RONGHUI_QUERY_MENU_BY_DIRECTION["received"]


def _filename_from_source_path(source_path: str) -> str:
    parsed = urlparse(_clean_text(source_path))
    path = unquote(parsed.path or _clean_text(source_path))
    return Path(path).name or "problem-attachment"


def _fetch_ronghui_problem_attachments(
    session: Any,
    item: dict[str, Any],
    page_context: dict[str, str],
) -> list[dict[str, Any]]:
    external_id = _require_external_id(item, platform="ronghui")
    response = session.post(
        RONGHUI_FIND_ALL_URL,
        params={"id": RONGHUI_PROBLEM_PIC_SCAN_CALL_ID},
        data={"OUT_GUID": external_id, "PIC_TYPE": "3"},
        headers=_ronghui_headers(page_context, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
        timeout=30,
    )
    payload = _response_json(response, label="融辉问题件图片查询")
    _raise_if_source_failed(payload, label="融辉问题件图片查询", code="SOURCE_ATTACHMENT_QUERY_FAILED")
    attachments: list[dict[str, Any]] = []
    for row in _extract_rows(payload):
        save_pos = _first_text(row, "SAVE_POS")
        if not save_pos:
            continue
        filename = _first_text(row, "FILE_NAME") or _filename_from_source_path(save_pos)
        attachments.append(
            {
                "path": save_pos,
                "filename": filename,
                "is_image": True,
                "source": RONGHUI_PROBLEM_PIC_SCAN_CALL_ID,
                "raw": _safe_json(row),
            }
        )
    return attachments


def _ronghui_detail(session_or_params: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    session = None if params is None else session_or_params
    params = session_or_params if params is None else params
    item = _safe_business_item(params)
    _require_external_id(item, platform="ronghui")
    details: list[dict[str, Any]] = [_safe_json(item.get("raw") or {})]
    if session is not None:
        page_context = _resolve_ronghui_page_context(session, _ronghui_query_menu_text_for_item(item, params))
        attachments = _fetch_ronghui_problem_attachments(session, item, page_context)
        if attachments:
            details.append({"attachments": attachments})
    return {"ok": True, "item": _safe_json(item), "details": details}


def _ronghui_reply(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    item = _safe_business_item(params)
    payload = _safe_payload(params)
    external_id = _require_external_id(item, platform="ronghui")
    raw_item = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    waybill_no = _first_text(item, "waybill_no", "BILL_CODE") or _first_text(raw_item, "BILL_CODE", "bill_code")
    reply_text = _clean_text(payload.get("reply_text") or payload.get("REVERSION") or payload.get("reversion"))
    if not waybill_no or not reply_text:
        raise CustomerServiceProblemError("MISSING_REQUIRED_FIELDS", "融辉回复必须包含单号和回复内容。")
    page_context = _resolve_ronghui_page_context(session, RONGHUI_QUERY_MENU_BY_DIRECTION["received"])
    row = dict(raw_item)
    row.update(
        {
            "GUID": external_id,
            "BILL_CODE": waybill_no,
            "REVERSION": reply_text,
        }
    )
    deal_result = _clean_text(payload.get("deal_result") or payload.get("DEAL_RESULT"))
    if deal_result:
        row["DEAL_RESULT"] = deal_result
    transfer_code = _clean_text(payload.get("transfer_code") or item.get("TRANSFER_CODE") or raw_item.get("TRANSFER_CODE"))
    if transfer_code:
        row["TRANSFER_CODE"] = transfer_code
    status_text = _clean_text(payload.get("prob_status") or payload.get("REVERSION_STATUS"))
    if status_text:
        row["REVERSION_STATUS"] = status_text
    bl_checkok = _clean_text(payload.get("bl_checkok") or payload.get("BL_CHECKOK"))
    if bl_checkok:
        row["BL_CHECKOK"] = bl_checkok
    result = _save_ronghui_tables(session, page_context, build_ronghui_save_tables_envelope("TAB_PROBLEM_UPT", row))
    _raise_if_source_failed(result, label="融辉回复", code="SOURCE_REPLY_FAILED")
    return {"ok": True, "result": _safe_json(result), "external_id": external_id}


def _ronghui_mark_read(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    item = _safe_business_item(params)
    payload = _safe_payload(params)
    external_id = _require_external_id(item, platform="ronghui")
    update_fields = payload.get("update_fields")
    if not isinstance(update_fields, dict) or not update_fields:
        raise CustomerServiceProblemError(
            "CAPTURE_REQUIRED",
            "融辉标记已读需要原页真实 update_fields；未抓到字段前不猜测 BL_SEE/查看状态。",
            status="capture_required",
        )
    row = {"GUID": external_id, **update_fields}
    page_context = _resolve_ronghui_page_context(session, RONGHUI_QUERY_MENU_BY_DIRECTION["received"])
    result = _save_ronghui_tables(session, page_context, build_ronghui_save_tables_envelope("TAB_PROBLEM_UPT", row))
    return {"ok": bool(result.get("success", True)), "result": _safe_json(result), "external_id": external_id}


def _fetch_ronghui_guid(session: Any, page_context: dict[str, str]) -> str:
    response = session.post(
        f"{RONGHUI_FIND_ALL_URL}?id=FIND_GUID",
        data={},
        headers=_ronghui_headers(page_context, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
        timeout=30,
    )
    payload = _response_json(response, label="融辉 FIND_GUID")
    rows = _extract_rows(payload)
    guid = _first_text(rows[0], "GUID") if rows else ""
    if not guid:
        raise CustomerServiceProblemError("MISSING_GUID", "融辉 FIND_GUID 未返回 GUID。")
    return guid


def _ronghui_publish(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_payload(params)
    bill_code = _clean_text(payload.get("bill_code") or payload.get("BILL_CODE"))
    problem_cause = _clean_text(payload.get("problem_cause") or payload.get("PROBLEM_CAUSE"))
    problem_type = _clean_text(payload.get("problem_type") or payload.get("TYPE"))
    owner_problem_type = _clean_text(payload.get("owner_problem_type") or payload.get("OWNER_PROBELM_TYPE"))
    notice_site_code = _clean_text(payload.get("notice_site_code") or payload.get("SEND_SITE_CODE"))
    notice_site = _clean_text(payload.get("notice_site") or payload.get("SEND_SITE"))
    missing = [
        name
        for name, value in {
            "bill_code": bill_code,
            "problem_cause": problem_cause,
            "problem_type": problem_type,
            "owner_problem_type": owner_problem_type,
            "notice_site_code": notice_site_code,
            "notice_site": notice_site,
        }.items()
        if not value
    ]
    if missing:
        raise CustomerServiceProblemError("MISSING_REQUIRED_FIELDS", f"融辉发布问题件缺少字段：{', '.join(missing)}")
    page_context = _resolve_ronghui_page_context(session, RONGHUI_PUBLISH_MENU_TEXT)
    guid = _clean_text(payload.get("GUID")) or _fetch_ronghui_guid(session, page_context)
    user_info = _read_user_info_cookie(session)
    row = {
        "GUID": guid,
        "BILL_CODE": bill_code,
        "SEND_SITE_CODE": notice_site_code,
        "SEND_SITE": notice_site,
        "PROBLEM_CAUSE": problem_cause,
        "TYPE": problem_type,
        "OWNER_PROBELM_TYPE": owner_problem_type,
        "REGISTER_SITE_CODE": _first_text(user_info, "loginSiteCode"),
        "REGISTER_SITE": _first_text(user_info, "loginSiteName"),
        "REGISTER_MAN_CODE": _first_text(user_info, "loginEmpCode", "loginUserId"),
        "REGISTER_MAN": _first_text(user_info, "loginEmpName", "loginUserName"),
        "REGISTER_MAN_DEPT": _first_text(user_info, "loginEmpDeptName"),
        "REGISTER_DATE": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "DATA_FROM": "K13",
    }
    result = _save_ronghui_tables(session, page_context, build_ronghui_save_tables_envelope("TAB_PROBLEM_ADD", row))
    return {"ok": bool(result.get("success", True)), "result": _safe_json(result), "external_id": guid}


def _upload_ronghui_attachment(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_payload(params)
    file_path = _clean_text(payload.get("file_path") or params.get("file_path"))
    if not file_path:
        raise CustomerServiceProblemError("MISSING_FILE", "缺少上传文件路径。")
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise CustomerServiceProblemError("FILE_NOT_FOUND", f"上传文件不存在：{path}")
    page_context = _resolve_ronghui_page_context(session, RONGHUI_PUBLISH_MENU_TEXT)
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    try:
        with path.open("rb") as handle:
            response = session.post(
                RONGHUI_UPLOAD_URL,
                files={"file": (path.name, handle, mime_type)},
                headers=_ronghui_headers(page_context),
                timeout=60,
            )
        result = _response_json(response, label="融辉附件上传")
    finally:
        if _bool_param(payload.get("delete_after_upload"), default=True):
            try:
                path.unlink()
            except OSError:
                pass
    return {"ok": True, "result": _safe_json(result)}


def _yunda_origin_date_parts(filters: dict[str, Any]) -> tuple[str, str, str, str]:
    def split_date_time(value: Any, *, default_time: str) -> tuple[str, str]:
        text = _clean_text(value).replace("/", "-")
        if not text:
            return "", ""
        if " " in text:
            date_part, time_part = text.split(None, 1)
            return date_part.strip(), time_part.strip()
        return text, default_time

    start_date, inferred_start_time = split_date_time(
        filters.get("start_date") or filters.get("date_from"),
        default_time="00:00:00",
    )
    end_date, inferred_end_time = split_date_time(
        filters.get("end_date") or filters.get("date_to"),
        default_time="23:59:59",
    )
    return (
        start_date,
        _clean_text(filters.get("start_time")) or inferred_start_time,
        end_date,
        _clean_text(filters.get("end_time")) or inferred_end_time,
    )


def build_yunda_query_payload(filters: dict[str, Any]) -> dict[str, Any]:
    filters = filters if isinstance(filters, dict) else {}
    page = _coerce_int(filters.get("page"), default=1, minimum=1, maximum=9999)
    rows = _coerce_int(filters.get("rows") or filters.get("page_size"), default=50, minimum=1, maximum=200)
    start_date, start_time, end_date, end_time = _yunda_origin_date_parts(filters)
    return {
        "LogisticsId": _clean_text(filters.get("LogisticsId") or filters.get("q") or filters.get("waybill_no")),
        "bl_attachment_status": _clean_text(filters.get("bl_attachment_status")),
        "check_status": _clean_text(filters.get("check_status")),
        "damage_degree": _clean_text(filters.get("damage_degree")),
        "damage_link": _clean_text(filters.get("damage_link")),
        "damage_type": _clean_text(filters.get("damage_type")),
        "end_date": end_date,
        "end_time": end_time,
        "is_replay": _clean_text(filters.get("is_replay") or filters.get("is_reply")),
        "issuer_site": _clean_text(filters.get("issuer_site")),
        "page": page,
        "prob_status": _clean_text(filters.get("prob_status")),
        "problem_type": _clean_text(filters.get("problem_type")),
        "problem_type_classes": _clean_text(filters.get("problem_type_classes")),
        "reply_by": _clean_text(filters.get("reply_by")),
        "rows": rows,
        "scan_source": _clean_text(filters.get("scan_source")),
        "source": _clean_text(filters.get("source")),
        "start_date": start_date,
        "start_time": start_time,
        "sum_site": _clean_text(filters.get("sum_site")),
        "sum_type": _clean_text(filters.get("sum_type")),
        "time": _clean_text(filters.get("time")),
        "udf012": _clean_text(filters.get("udf012")),
        "udf015": _clean_text(filters.get("udf015")),
        "udf016": _clean_text(filters.get("udf016")),
    }


def _yunda_issue_today() -> dt.date:
    return dt.datetime.now(SHANGHAI_TZ).date()


def build_yunda_issue_list_payload(filters: dict[str, Any]) -> dict[str, Any]:
    filters = filters if isinstance(filters, dict) else {}
    page = _coerce_int(filters.get("page"), default=1, minimum=1, maximum=9999)
    rows = _coerce_int(filters.get("rows") or filters.get("page_size"), default=50, minimum=1, maximum=200)
    start_date, start_time, end_date, end_time = _yunda_origin_date_parts(filters)
    if not start_date and not end_date:
        today = _yunda_issue_today()
        start_date = (
            today - dt.timedelta(days=YUNDA_ISSUE_DEFAULT_LOOKBACK_DAYS)
        ).isoformat()
        start_time = "00:00:00"
        end_date = today.isoformat()
        end_time = "23:59:59"
    elif not start_date or not end_date:
        raise CustomerServiceProblemError(
            "INVALID_DATE_RANGE",
            "韵达发布问题件查询必须同时提供开始和结束日期。",
        )
    return {
        "bl_attachment": _clean_text(filters.get("bl_attachment")),
        "ship_no": _clean_text(filters.get("ship_no") or filters.get("q") or filters.get("waybill_no")),
        "end_date": end_date,
        "end_time": end_time,
        "page": page,
        "prob_status": _clean_text(filters.get("prob_status")),
        "problem_type": _clean_text(filters.get("problem_type")),
        "problem_type_classes": _clean_text(filters.get("problem_type_classes")),
        "rows": rows,
        "sex": _clean_text(filters.get("sex")),
        "start_date": start_date,
        "start_time": start_time,
        "time": _clean_text(filters.get("time")),
    }


def _yunda_headers(referer: str = "") -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": YUNDA_ORIGIN,
        "Referer": referer or f"{YUNDA_PUBLIC_ROOT}/query/index.html",
        "X-Requested-With": "XMLHttpRequest",
    }


def _compact_yunda_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None and not (isinstance(value, str) and value == "")}


def _raise_if_yunda_auth_required(response: Any, body: str) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {301, 302, 401, 403}:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type") or headers.get("Content-Type") or "").lower()
    response_url = str(getattr(response, "url", "") or "").lower()
    location = str(headers.get("location") or headers.get("Location") or "").lower()
    lower_body = str(body or "").lower()
    login_url = any(marker in response_url or marker in location for marker in ("ky-sso", "/login", "login.html"))
    password_form = any(marker in lower_body for marker in ('type="password"', "type='password'", 'name="password"', "name='password'"))
    sso_redirect = re.search(
        r"(?:window\.)?location(?:\.href)?\s*=\s*['\"][^'\"]*(?:ky-sso|sso\.yunda56\.com)[^'\"]*(?:login|passport)",
        lower_body,
    )
    explicit_login = "login-form" in lower_body or "loginform" in lower_body or "密码登录" in str(body or "")
    if "text/html" in content_type and (login_url or password_form or sso_redirect or explicit_login):
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录韵达账号。")
    if status_code == 200 and not str(body or "").strip():
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达接口返回空响应，请重新登录韵达账号。")


def _yunda_post_json(
    session: Any,
    url: str,
    data: dict[str, Any],
    *,
    referer: str,
    label: str,
    preserve_empty: bool = False,
) -> Any:
    request_data = dict(data) if preserve_empty else _compact_yunda_payload(data)
    response = session.post(
        url,
        data=request_data,
        headers=_yunda_headers(referer),
        timeout=30,
    )
    body = str(getattr(response, "text", "") or "")
    _raise_if_yunda_auth_required(response, body)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    try:
        return response.json()
    except Exception as exc:
        raise CustomerServiceProblemError("INVALID_RESPONSE", f"韵达{label}接口返回非 JSON：{body[:120]}") from exc


def _yunda_query(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    filters = params.get("filters") if isinstance(params.get("filters"), dict) else {}
    direction = _normalize_direction(filters.get("direction") or params.get("direction"), platform="yunda")
    if direction in YUNDA_PUBLISHED_DIRECTIONS:
        url = YUNDA_ISSUE_LIST_URL
        referer = f"{YUNDA_PUBLIC_ROOT}/issue/index.html"
        payload = build_yunda_issue_list_payload(filters)
        source_direction = "published"
        label = "发布列表"
        preserve_empty = True
    else:
        url = YUNDA_QUERY_LIST_URL
        referer = f"{YUNDA_PUBLIC_ROOT}/query/index.html"
        payload = build_yunda_query_payload(filters)
        source_direction = "query"
        label = "查询列表"
        preserve_empty = False
    data = _yunda_post_json(
        session,
        url,
        payload,
        referer=referer,
        label=label,
        preserve_empty=preserve_empty,
    )
    raw_rows = _extract_rows(data)
    rows = normalize_problem_rows(
        "yunda",
        raw_rows,
        account_id=_clean_text(params.get("account_id")),
        account_label=_clean_text(params.get("account_label")),
        source_direction=source_direction,
    )
    return {"ok": True, "rows": rows, "stats": {"total": _extract_total(data, raw_rows), "returned": len(rows)}}


def _yunda_detail(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    item = _safe_business_item(params)
    external_id = _require_external_id(item, platform="yunda")
    data = _yunda_post_json(
        session,
        YUNDA_QUERY_DETAIL_URL,
        {"prob_main_id": external_id},
        referer=f"{YUNDA_PUBLIC_ROOT}/query/index.html",
        label="详情",
    )
    return {"ok": True, "external_id": external_id, "details": _safe_json(_extract_rows(data) or data)}


def _yunda_mark_read(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    item = _safe_business_item(params)
    external_id = _require_external_id(item, platform="yunda")
    data = _yunda_post_json(
        session,
        YUNDA_QUERY_READ_URL,
        {"prob_main_id": external_id},
        referer=f"{YUNDA_PUBLIC_ROOT}/query/index.html",
        label="标记已读",
    )
    return {"ok": True, "external_id": external_id, "result": _safe_json(data)}


def _yunda_reply(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    item = _safe_business_item(params)
    payload = _safe_payload(params)
    external_id = _require_external_id(item, platform="yunda")
    prob_status = _clean_text(payload.get("prob_status"))
    reply_text = _clean_text(payload.get("reply_text"))
    if not prob_status or not reply_text:
        raise CustomerServiceProblemError("MISSING_REQUIRED_FIELDS", "韵达回复必须包含处理状态和回复内容。")
    data = {
        "prob_status": prob_status,
        "reply_text": reply_text,
        "prob_main_id": external_id,
        "old_prob_status": _clean_text(payload.get("old_prob_status") or item.get("status")),
        "file_arr": payload.get("file_arr") if isinstance(payload.get("file_arr"), list) else [],
    }
    result = _yunda_post_json(
        session,
        YUNDA_QUERY_REPLY_URL,
        data,
        referer=f"{YUNDA_PUBLIC_ROOT}/query/reply.html?prob_main_id={external_id}",
        label="回复",
    )
    _raise_if_source_failed(result, label="韵达回复", code="SOURCE_REPLY_FAILED")
    return {"ok": True, "external_id": external_id, "result": _safe_json(result)}


def _yunda_publish(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_payload(params)
    required = ("ship_no", "classes_type", "prob_text", "site_id")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise CustomerServiceProblemError("MISSING_REQUIRED_FIELDS", f"韵达发布问题件缺少字段：{', '.join(missing)}")
    data = dict(payload)
    data["file_arr"] = data.get("file_arr") if isinstance(data.get("file_arr"), list) else []
    result = _yunda_post_json(
        session,
        YUNDA_ISSUE_SAVE_URL,
        data,
        referer=f"{YUNDA_PUBLIC_ROOT}/issue/index.html",
        label="发布",
    )
    return {"ok": True, "result": _safe_json(result)}


def _upload_yunda_attachment(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_payload(params)
    file_path = _clean_text(payload.get("file_path") or params.get("file_path"))
    if not file_path:
        raise CustomerServiceProblemError("MISSING_FILE", "缺少上传文件路径。")
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise CustomerServiceProblemError("FILE_NOT_FOUND", f"上传文件不存在：{path}")
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    try:
        with path.open("rb") as handle:
            response = session.post(
                YUNDA_UPLOAD_URL,
                files={"file": (path.name, handle, mime_type)},
                headers=_yunda_headers(f"{YUNDA_PUBLIC_ROOT}/issue/index.html"),
                timeout=60,
            )
        body = str(getattr(response, "text", "") or "")
        _raise_if_yunda_auth_required(response, body)
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        try:
            result = response.json()
        except Exception:
            result = {"raw": body[:500]}
    finally:
        if _bool_param(payload.get("delete_after_upload"), default=True):
            try:
                path.unlink()
            except OSError:
                pass
    return {"ok": True, "result": _safe_json(result)}


def _sniff_image_content_type(payload: bytes) -> str:
    if not payload:
        return ""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _looks_like_image(payload: bytes) -> bool:
    return bool(_sniff_image_content_type(payload))


def _normalize_attachment_source_url(platform: str, source_url: Any) -> str:
    raw = _clean_text(source_url).replace("&amp;", "&").replace("\\/", "/").replace("\\", "/")
    if not raw:
        raise CustomerServiceProblemError("MISSING_ATTACHMENT_URL", "附件图片地址为空。")
    if re.search(r"[\s\"'<>]", raw):
        raise CustomerServiceProblemError("INVALID_ATTACHMENT_URL", "附件图片地址格式无效。")
    if raw.startswith("//"):
        raw = f"https:{raw}"
    origin = RONGHUI_ORIGIN if platform == "ronghui" else YUNDA_ORIGIN
    if platform == "yunda" and (raw.startswith("/base/") or re.match(r"^(base|query|issue)/", raw, flags=re.I)):
        raw = urljoin(f"{YUNDA_PUBLIC_ROOT}/", raw.lstrip("/"))
    elif raw.startswith("/"):
        raw = urljoin(origin, raw)
    elif not re.match(r"^https?://", raw, flags=re.I):
        raw = urljoin(f"{origin}/", raw)

    parsed = urlparse(raw)
    allowed_host = urlparse(origin).netloc
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != allowed_host.lower():
        raise CustomerServiceProblemError("INVALID_ATTACHMENT_URL", "附件图片地址不属于当前平台原站。")
    sensitive_keys = {key.lower() for key in parse_qs(parsed.query).keys()} & SENSITIVE_RESULT_KEYS
    if sensitive_keys:
        raise CustomerServiceProblemError("SENSITIVE_ATTACHMENT_URL", "附件图片地址包含登录态参数，已拒绝代理。")
    return raw


def _fetch_problem_attachment(session: Any, params: dict[str, Any]) -> dict[str, Any]:
    platform = _normalize_platform(params.get("platform"))
    payload = _safe_payload(params)
    source_url = _normalize_attachment_source_url(platform, payload.get("source_url") or params.get("source_url"))
    headers = {"Accept": "image/*,*/*", "User-Agent": "Mozilla/5.0"}
    headers["Referer"] = f"{YUNDA_PUBLIC_ROOT}/query/index.html" if platform == "yunda" else RONGHUI_INDEX_URL
    response = session.get(source_url, headers=headers, timeout=30)
    content_type = str((getattr(response, "headers", {}) or {}).get("Content-Type") or "").split(";", 1)[0].strip()
    body_text = ""
    try:
        body_text = str(getattr(response, "text", "") or "")
    except Exception:
        body_text = ""
    if platform == "yunda" and not content_type.startswith("image/"):
        _raise_if_yunda_auth_required(response, body_text)
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    payload_bytes = bytes(getattr(response, "content", b"") or b"")
    if len(payload_bytes) > 10 * 1024 * 1024:
        raise CustomerServiceProblemError("ATTACHMENT_TOO_LARGE", "附件图片超过 10MB，已停止预览。")
    if not _looks_like_image(payload_bytes):
        raise CustomerServiceProblemError("INVALID_ATTACHMENT_CONTENT", "原站附件返回的不是图片内容。")
    # Ignore upstream MIME: only a raster type derived from magic bytes is
    # safe to return to Console.  SVG/XML is deliberately not supported.
    content_type = _sniff_image_content_type(payload_bytes)
    filename = Path(urlparse(source_url).path).name or "problem-attachment"
    return {
        "ok": True,
        "source_url": source_url,
        "content_type": content_type,
        "filename": filename,
        "body_base64": base64.b64encode(payload_bytes).decode("ascii"),
    }


def _dispatch_action(platform: str, action: str, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    if platform == "ronghui":
        if action == "query":
            return _ronghui_query(session, params)
        if action == "detail":
            return _ronghui_detail(session, params)
        if action == "reply":
            return _ronghui_reply(session, params)
        if action == "mark_read":
            return _ronghui_mark_read(session, params)
        if action == "publish":
            return _ronghui_publish(session, params)
        if action == "upload_attachment":
            return _upload_ronghui_attachment(session, params)
        if action == "fetch_attachment":
            return _fetch_problem_attachment(session, params)
    if platform == "yunda":
        if action == "query":
            return _yunda_query(session, params)
        if action == "detail":
            return _yunda_detail(session, params)
        if action == "reply":
            return _yunda_reply(session, params)
        if action == "mark_read":
            return _yunda_mark_read(session, params)
        if action == "publish":
            return _yunda_publish(session, params)
        if action == "upload_attachment":
            return _upload_yunda_attachment(session, params)
        if action == "fetch_attachment":
            return _fetch_problem_attachment(session, params)
    raise CustomerServiceProblemError("UNSUPPORTED_ACTION", f"不支持的客服问题件动作：{platform}/{action}")


def run_once(params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    platform = _normalize_platform(params.get("platform"))
    action = _clean_text(params.get("action") or "query").lower()
    if platform not in SUPPORTED_PLATFORMS:
        return _error_result("UNSUPPORTED_PLATFORM", f"不支持的平台：{params.get('platform')}", platform=platform, action=action)
    if action not in SUPPORTED_ACTIONS:
        return _error_result("UNSUPPORTED_ACTION", f"不支持的动作：{action}", platform=platform, action=action)
    try:
        session = _build_session(platform, params)
        result = _dispatch_action(platform, action, session, params)
        result.update(
            {
                "platform": platform,
                "action": action,
                "account_id": _clean_text(params.get("account_id")),
                "account_label": _clean_text(params.get("account_label")),
            }
        )
        return _safe_json(result)
    except TMSAuthStateError as exc:
        return _error_result(
            getattr(exc, "code", "AUTH_REQUIRED") or "AUTH_REQUIRED",
            str(exc) or "登录态已失效。",
            status="auth_required",
            platform=platform,
            action=action,
        )
    except CustomerServiceProblemError as exc:
        return _error_result(exc.code, str(exc), status=exc.status, platform=platform, action=action)
    except Exception as exc:
        return _error_result(type(exc).__name__, str(exc), platform=platform, action=action)
