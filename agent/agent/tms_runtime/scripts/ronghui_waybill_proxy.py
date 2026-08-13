"""Raw same-origin proxy support for the Ronghui waybill entry page."""

from __future__ import annotations

import base64
import copy
import json
import re
import threading
import time
from html import escape, unescape
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from agent.tms_runtime.account_contracts import PRICE_SESSION_PROFILE
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import BASE_ORIGIN as RONGHUI_ORIGIN
from agent.tms_runtime.session_broker import get_session_broker
from shared.manual_entry_contracts import canonical_manual_proxy_path


ORDER_ENTRY_MENU_ID = "1622"
RONGHUI_WAYBILL_SESSION_PROFILE = PRICE_SESSION_PROFILE
RONGHUI_ENTRY_PATH = "/widget/home"
RONGHUI_ENTRY_REFERER = f"{RONGHUI_ORIGIN}{RONGHUI_ENTRY_PATH}"
MENU_PATH = "/menuTreeExtend/loadMenu"
DEFAULT_TIMEOUT_SEC = 180
RONGHUI_USER_INFO_COOKIE = "userInfo"
RONGHUI_SAFE_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
RONGHUI_MAX_SAFE_REDIRECTS = 5
RONGHUI_DOWNLOAD_CDN_HOST = "img.ronghuiwl.com"
RONGHUI_DOWNLOAD_CDN_PATH_RE = re.compile(
    r"^/group\d+/M\d{2}/[0-9A-Fa-f]{2}/[0-9A-Fa-f]{2}/[^/?#]+\.(?:jpg|jpeg|png|gif|webp)$",
    flags=re.IGNORECASE,
)
RONGHUI_USER_INFO_FIELDS = (
    "loginEmpCode",
    "loginEmpName",
    "loginEmpType",
    "loginOwnerFinanceCode",
    "loginOwnerSiteCode",
    "loginSiteCode",
    "loginSiteName",
    "loginSiteProvinceCode",
    "loginSiteType",
    "loginUserAccount",
    "loginUserName",
)

ALLOWED_PATH_PREFIXES = (
    "/widget/",
    "/static/",
    "/dataQuery/",
    "/dataOperation/",
    "/minic/",
    "/address/",
    "/advancePayment/",
    "/commonOption/",
    "/fhdquote/",
    "/file/",
    "/map/",
    "/userView/",
    "/unauth/download/",
    "/menuTreeExtend/",
    "/module/",
)
RELATIVE_ALLOWED_PATH_PREFIXES = tuple(prefix.lstrip("/") for prefix in ALLOWED_PATH_PREFIXES if prefix.startswith("/"))
STATIC_SAME_ORIGIN_SUFFIXES = (".css", ".woff", ".woff2", ".ttf", ".eot", ".otf")
STATIC_FONT_PATH_MARKERS = ("/fonts/", "/font/", "/iconfont/")
RONGHUI_PROXY_LOOKUP_CACHE_TTL_SEC = 300
RONGHUI_PROXY_LOOKUP_CACHE_MAX_ITEMS = 256
CACHEABLE_DATAQUERY_CALL_IDS = {
    "FIND_ALL_EMPLOYEE_COMBOBOX",
    "FIND_CREATE_BILL_DESTINATION",
    "FIND_CREATE_BILL_SEND_CENTER",
    "FIND_CUSTOMER_DISP_LIST_BILL",
    "FIND_CUSTOMER_SEND_LIST_BILL",
    "FIND_PRODUCT_TYPE",
    "FIND_SITE_AND_CENTER",
    "FIND_SITE_INFO_BY_SITE_CODE",
    "FIND_TAB_CUSTOMER",
    "FIND_TAB_SITE_BUSINESS_TYPE",
    "FIND_TAB_SITE_BY_AGENT",
    "FIND_TAB_SITE_RECORD_",
}
CACHEABLE_ADDRESS_CALL_IDS = {"FIND_COUNTY_COMBOBOX_PAGE"}
CACHEABLE_MINIC_OPTION_CODES = {
    "CARD_TYPE",
    "PAYMENT_TYPE",
    "TAB_CLASS",
    "TAB_DISPATCH_MODE",
    "TAB_PACKING_TYPE",
    "VIP_Added_Services",
    "WEIGHT_RATIO",
}
_RONGHUI_PROXY_LOOKUP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RONGHUI_PROXY_LOOKUP_CACHE_LOCK = threading.Lock()
HOP_BY_HOP_REQUEST_HEADERS = {
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "cookie",
    "host",
    "if-modified-since",
    "if-none-match",
    "if-range",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
BLOCKED_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "content-security-policy",
    "set-cookie",
    "transfer-encoding",
    "x-frame-options",
}
URL_ATTRIBUTE_PATTERN = (
    "data-url|data-src|data-href|formaction|action|background|poster|href|src|url"
)
ATTR_URL_RE = re.compile(
    rf"(?P<prefix>(?<![\w-])(?:{URL_ATTRIBUTE_PATTERN})\b\s*=\s*)(?P<quote>['\"])(?P<url>[^'\"]*)(?P=quote)",
    flags=re.IGNORECASE,
)
SRCSET_ATTR_RE = re.compile(
    r"(?P<prefix>\bsrcset\s*=\s*)(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
    flags=re.IGNORECASE,
)
SRCDOC_ATTR_RE = re.compile(
    r"(?P<prefix>\bsrcdoc\s*=\s*)(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
    flags=re.IGNORECASE,
)
OBJECT_DATA_ATTR_RE = re.compile(
    r"(?P<prefix><object\b[^>]*\bdata\s*=\s*)(?P<quote>['\"])(?P<url>[^'\"]*)(?P=quote)",
    flags=re.IGNORECASE,
)
UNQUOTED_OBJECT_DATA_ATTR_RE = re.compile(
    r"(?P<prefix><object\b[^>]*\bdata\s*=\s*)(?P<url>(?:(?:(?:https?:)?//tms\.ronghuiwl\.com)?/(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)|(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module))/[^\s>'\"\)]*)",
    flags=re.IGNORECASE,
)
REFRESH_HEADER_URL_RE = re.compile(
    r"(?P<prefix>\burl\s*=\s*)(?P<quote>['\"]?)(?P<url>[^'\";\s]+)(?P=quote)",
    flags=re.IGNORECASE,
)
BASE_HREF_RE = re.compile(
    r"(?P<prefix><base\b[^>]*\bhref\s*=\s*)(?P<quote>['\"])(?P<url>[^'\"]*)(?P=quote)",
    flags=re.IGNORECASE,
)
META_TAG_RE = re.compile(r"<meta\b[^>]*>", flags=re.IGNORECASE)
META_CSP_HTTP_EQUIV_RE = re.compile(
    r"\bhttp-equiv\s*=\s*(['\"]?)content-security-policy\1(?=[\s>/])",
    flags=re.IGNORECASE,
)
UNQUOTED_BASE_HREF_RE = re.compile(
    r"(?P<prefix><base\b[^>]*\bhref\s*=\s*)(?P<url>[^'\">\s]+)",
    flags=re.IGNORECASE,
)
UNQUOTED_ATTR_URL_RE = re.compile(
    rf"(?P<prefix>(?<![\w-])(?:{URL_ATTRIBUTE_PATTERN})\b\s*=\s*)(?P<url>(?:(?:(?:https?:)?//tms\.ronghuiwl\.com)?/(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)|(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module))/[^\s>'\"\)]*)",
    flags=re.IGNORECASE,
)
QUOTED_RONGHUI_URL_RE = re.compile(
    r"(?P<quote>['\"`])(?P<url>(?:(?:https?:)?//tms\.ronghuiwl\.com)?/(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)/[^'\"`]*)(?P=quote)",
    flags=re.IGNORECASE,
)
CSS_URL_RE = re.compile(
    r"(?P<prefix>\burl\(\s*)(?P<quote>['\"]?)(?P<url>(?:(?:https?:)?//tms\.ronghuiwl\.com)?/(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)/[^'\"\)]*)(?P=quote)(?P<suffix>\s*\))",
    flags=re.IGNORECASE,
)
RELATIVE_QUOTED_RONGHUI_URL_RE = re.compile(
    r"(?P<quote>['\"`])(?P<url>(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)/[^'\"`]*)(?P=quote)",
    flags=re.IGNORECASE,
)
ESCAPED_QUOTED_RONGHUI_URL_RE = re.compile(
    r"(?P<quote>['\"`])(?P<url>(?:(?:https?:)?\\*/\\*/tms\.ronghuiwl\.com)?\\*/(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)\\*/[^'\"`]*)(?P=quote)",
    flags=re.IGNORECASE,
)
ESCAPED_RELATIVE_QUOTED_RONGHUI_URL_RE = re.compile(
    r"(?P<quote>['\"`])(?P<url>(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)\\*/[^'\"`]*)(?P=quote)",
    flags=re.IGNORECASE,
)
RELATIVE_CSS_URL_RE = re.compile(
    r"(?P<prefix>\burl\(\s*)(?P<quote>['\"]?)(?P<url>(?:widget|static|dataQuery|dataOperation|minic|address|advancePayment|commonOption|fhdquote|file|map|userView|unauth/download|menuTreeExtend|module)/[^'\"\)]*)(?P=quote)(?P<suffix>\s*\))",
    flags=re.IGNORECASE,
)
SLASH_ESCAPE_RE = re.compile(r"\\+/")
RONGHUI_MAP_IFRAME_SRC_RE = re.compile(
    r"(?P<prefix><iframe\b(?=[^>]*\bid\s*=\s*['\"]mapContainer['\"])[^>]*\bsrc\s*=\s*)(?P<quote>['\"])(?P<url>https?://sutong\.api\.htkj56\.com/[^'\"]+)(?P=quote)",
    flags=re.IGNORECASE,
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _js_escape_cookie_value(text: str) -> str:
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./"
    output = []
    for char in str(text or ""):
        if char in safe:
            output.append(char)
            continue
        code = ord(char)
        output.append(f"%{code:02X}" if code < 256 else f"%u{code:04X}")
    return "".join(output)


def _decode_js_cookie_value(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""

    def replace_unicode_escape(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)

    text = re.sub(r"%u([0-9A-Fa-f]{4})", replace_unicode_escape, text)
    return unquote(text)


def _session_cookie_value(session: Any, name: str) -> str:
    cookies = getattr(session, "cookies", None)
    if not cookies:
        return ""
    if isinstance(cookies, dict):
        return _clean_text(cookies.get(name))
    try:
        for cookie in cookies:
            if isinstance(cookie, dict):
                cookie_name = _clean_text(cookie.get("name"))
                cookie_value = cookie.get("value")
            else:
                cookie_name = _clean_text(getattr(cookie, "name", ""))
                cookie_value = getattr(cookie, "value", "")
            if cookie_name == name:
                return _clean_text(cookie_value)
    except Exception:
        pass
    try:
        return _clean_text(cookies.get(name))
    except Exception:
        return ""


def _parse_user_info_cookie(value: Any) -> dict[str, Any]:
    raw = _clean_text(value)
    if not raw:
        return {}
    candidates = [raw, _decode_js_cookie_value(raw)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _client_user_info_cookie_from_session(session: Any) -> str:
    payload = _parse_user_info_cookie(_session_cookie_value(session, RONGHUI_USER_INFO_COOKIE))
    if not payload:
        return ""
    sanitized = {
        field: _clean_text(payload.get(field))
        for field in RONGHUI_USER_INFO_FIELDS
        if payload.get(field) is not None
    }
    if not sanitized:
        return ""
    cookie_payload = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    return _js_escape_cookie_value(cookie_payload)


def _is_allowed_path(path: str) -> bool:
    canonical = canonical_manual_proxy_path(path)
    return bool(canonical) and any(canonical.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES)


def _is_relative_allowed_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in RELATIVE_ALLOWED_PATH_PREFIXES)


def _query_text(value: Any) -> str:
    if isinstance(value, dict):
        return urlencode([(str(key), str(item)) for key, item in value.items() if item is not None], doseq=True)
    return _clean_text(value).lstrip("?")


def _walk_menu(nodes: Any):
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        yield from _walk_menu(node.get("children") or [])


def _safe_json_response(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        try:
            data = json.loads(str(getattr(response, "text", "") or "{}"))
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def _menu_request_headers() -> dict[str, str]:
    return _filter_request_headers(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
    )


def _resolve_entry_url(session: Any, *, entry_menu_id: Any = "", entry_menu_text: Any = "") -> str:
    custom_menu_id = _clean_text(entry_menu_id)
    custom_menu_text = _clean_text(entry_menu_text)
    target_menu_id = custom_menu_id or ("" if custom_menu_text else ORDER_ENTRY_MENU_ID)
    target_menu_text = custom_menu_text or "运单录入"
    menu_url = f"{RONGHUI_ORIGIN}{MENU_PATH}"
    response = session.request("POST", menu_url, headers=_menu_request_headers(), timeout=30)
    _auth_if_login_response(response, _response_content(response).decode("utf-8", errors="replace"))
    payload = _safe_json_response(response)
    nodes = payload.get("result", {}).get("data") if isinstance(payload.get("result"), dict) else []
    for node in _walk_menu(nodes):
        node_id = _clean_text(node.get("id"))
        text = _clean_text(node.get("text") or node.get("name"))
        url = _clean_text(node.get("url"))
        if not url or "/widget/home" not in url:
            continue
        if (target_menu_id and node_id == target_menu_id) or (target_menu_text and text == target_menu_text):
            return url if url.startswith(("http://", "https://")) else urljoin(RONGHUI_ORIGIN + "/", url.lstrip("/"))
    raise RuntimeError(f"Ronghui entry menu was not found: {target_menu_text or target_menu_id}")


def _target_from_params(
    session: Any,
    path_value: Any,
    query_value: Any = "",
    *,
    entry_menu_id: Any = "",
    entry_menu_text: Any = "",
) -> tuple[str, str, str]:
    raw_path = _clean_text(path_value)
    raw_query = _query_text(query_value)
    if not raw_path or raw_path == "/":
        remote_url = _resolve_entry_url(
            session,
            entry_menu_id=entry_menu_id,
            entry_menu_text=entry_menu_text,
        )
        parsed_entry = urlparse(remote_url)
        return parsed_entry.path, parsed_entry.query, remote_url

    parsed = urlparse(raw_path)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != urlparse(RONGHUI_ORIGIN).netloc:
            raise ValueError("Only Ronghui TMS URLs can be proxied.")
        path = canonical_manual_proxy_path(parsed.path)
        query_parts = parse_qsl(parsed.query, keep_blank_values=True)
    else:
        path = canonical_manual_proxy_path(
            parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
        )
        query_parts = parse_qsl(parsed.query, keep_blank_values=True)

    if raw_query:
        query_parts.extend(parse_qsl(raw_query, keep_blank_values=True))
    if not _is_allowed_path(path):
        raise ValueError("Path is outside the Ronghui allow-list.")

    query = urlencode(query_parts, doseq=True)
    remote_url = urlunparse(("https", urlparse(RONGHUI_ORIGIN).netloc, path, "", query, ""))
    return path, query, remote_url


def _safe_ronghui_redirect_url(location: Any, *, current_url: str) -> str:
    location_text = _clean_text(location)
    if not location_text:
        return ""
    absolute = urljoin(current_url, SLASH_ESCAPE_RE.sub("/", location_text))
    parsed = urlparse(absolute)
    origin = urlparse(RONGHUI_ORIGIN)
    if parsed.scheme not in {"http", "https"}:
        return ""
    redirect_netloc = parsed.netloc.lower()
    if redirect_netloc == origin.netloc.lower():
        if not _is_allowed_path(parsed.path):
            return ""
        return urlunparse(("https", origin.netloc, parsed.path, "", parsed.query, ""))
    if redirect_netloc != RONGHUI_DOWNLOAD_CDN_HOST:
        return ""
    if not RONGHUI_DOWNLOAD_CDN_PATH_RE.match(parsed.path):
        return ""
    return urlunparse(("https", RONGHUI_DOWNLOAD_CDN_HOST, parsed.path, "", parsed.query, ""))


def _should_follow_safe_redirects(method: str, path: str) -> bool:
    return method == "GET" and str(path or "").startswith("/unauth/download/")


def _request_ronghui_proxy(
    session: Any,
    method: str,
    remote_url: str,
    *,
    path: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_sec: int,
) -> Any:
    response = session.request(
        method,
        remote_url,
        headers=headers,
        data=body if method != "GET" else None,
        allow_redirects=False,
        timeout=timeout_sec,
    )
    if not _should_follow_safe_redirects(method, path):
        return response

    redirects = 0
    while int(getattr(response, "status_code", 0) or 0) in RONGHUI_SAFE_REDIRECT_STATUS_CODES:
        response_headers = getattr(response, "headers", {}) or {}
        location = ""
        iterator = response_headers.items() if isinstance(response_headers, dict) else getattr(response_headers, "items", lambda: [])()
        for key, value in iterator:
            if _clean_text(key).lower() == "location":
                location = _clean_text(value)
                break
        next_url = _safe_ronghui_redirect_url(location, current_url=str(getattr(response, "url", "") or remote_url))
        if not next_url:
            return response
        redirects += 1
        if redirects > RONGHUI_MAX_SAFE_REDIRECTS:
            return response
        response = session.request(
            method,
            next_url,
            headers=headers,
            data=None,
            allow_redirects=False,
            timeout=timeout_sec,
        )
    return response


def _query_pairs_without_cache_buster(query: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(str(query or ""), keep_blank_values=True):
        if key == "_":
            continue
        pairs.append((key, value))
    return pairs


def _single_query_value(pairs: list[tuple[str, str]], key: str) -> str:
    for item_key, value in pairs:
        if item_key == key:
            return value
    return ""


def _cacheable_lookup_key(method: str, path: str, query: str) -> str:
    if method != "GET":
        return ""
    pairs = _query_pairs_without_cache_buster(query)
    if any(key == "key" and value for key, value in pairs):
        return ""

    call_id = _single_query_value(pairs, "id")
    if path in {"/dataQuery/findAllByCallId", "/dataQuery/findPageByCallId"}:
        if call_id not in CACHEABLE_DATAQUERY_CALL_IDS:
            return ""
    elif path == "/address/inputtips":
        if call_id not in CACHEABLE_ADDRESS_CALL_IDS:
            return ""
    elif path == "/minic/combobox":
        option_code = _single_query_value(pairs, "optionCode")
        if option_code not in CACHEABLE_MINIC_OPTION_CODES:
            return ""
    else:
        return ""

    normalized_query = urlencode(sorted(pairs), doseq=True)
    return f"{path}?{normalized_query}"


def _get_lookup_cache_entry(cache_key: str) -> dict[str, Any] | None:
    if not cache_key:
        return None
    now = time.monotonic()
    with _RONGHUI_PROXY_LOOKUP_CACHE_LOCK:
        entry = _RONGHUI_PROXY_LOOKUP_CACHE.get(cache_key)
        if not entry:
            return None
        expires_at, payload = entry
        if expires_at <= now:
            _RONGHUI_PROXY_LOOKUP_CACHE.pop(cache_key, None)
            return None
        return copy.deepcopy(payload)


def _store_lookup_cache_entry(cache_key: str, payload: dict[str, Any]) -> None:
    if not cache_key:
        return
    with _RONGHUI_PROXY_LOOKUP_CACHE_LOCK:
        if len(_RONGHUI_PROXY_LOOKUP_CACHE) >= RONGHUI_PROXY_LOOKUP_CACHE_MAX_ITEMS:
            oldest_key = min(_RONGHUI_PROXY_LOOKUP_CACHE, key=lambda key: _RONGHUI_PROXY_LOOKUP_CACHE[key][0])
            _RONGHUI_PROXY_LOOKUP_CACHE.pop(oldest_key, None)
        _RONGHUI_PROXY_LOOKUP_CACHE[cache_key] = (
            time.monotonic() + RONGHUI_PROXY_LOOKUP_CACHE_TTL_SEC,
            copy.deepcopy(payload),
        )


def _filter_request_headers(headers: Any, *, content_type: str = "") -> dict[str, str]:
    output: dict[str, str] = {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            key_text = _clean_text(key)
            if not key_text or key_text.lower() in HOP_BY_HOP_REQUEST_HEADERS:
                continue
            output[key_text] = str(value)
    if content_type and "content-type" not in {key.lower() for key in output}:
        output["Content-Type"] = content_type
    output["Origin"] = RONGHUI_ORIGIN
    output["Referer"] = RONGHUI_ENTRY_REFERER
    return output


def _filter_response_headers(headers: Any, *, current_url: str = "", proxy_prefix: str = "") -> dict[str, str]:
    output: dict[str, str] = {}
    iterator = headers.items() if isinstance(headers, dict) else getattr(headers, "items", lambda: [])()
    for key, value in iterator:
        key_text = _clean_text(key)
        if not key_text or key_text.lower() in BLOCKED_RESPONSE_HEADERS:
            continue
        value_text = str(value)
        lowered_key = key_text.lower()
        if current_url and proxy_prefix and lowered_key == "location":
            value_text = _proxy_url_for(value_text, current_url=current_url, proxy_prefix=proxy_prefix)
        elif current_url and proxy_prefix and lowered_key == "refresh":
            value_text = _rewrite_refresh_header(value_text, current_url=current_url, proxy_prefix=proxy_prefix)
        output[key_text] = value_text
    content_type = output.get("content-type") or output.get("Content-Type") or ""
    if current_url and _is_cacheable_static_response(current_url, content_type):
        output["Cache-Control"] = "public, max-age=86400"
        output.pop("Pragma", None)
        output.pop("pragma", None)
    else:
        output["Cache-Control"] = "no-store"
        output["Pragma"] = "no-cache"
    return output


def _decode_body(params: dict[str, Any]) -> bytes | None:
    raw_base64 = _clean_text(params.get("body_base64"))
    if raw_base64:
        return base64.b64decode(raw_base64)
    body_text = params.get("body")
    if body_text is None:
        return None
    return str(body_text).encode("utf-8")


def _charset_from_content_type(content_type: str) -> str:
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, flags=re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _should_keep_static_same_origin(path: str) -> bool:
    lowered = str(path or "").lower()
    if lowered.endswith(STATIC_SAME_ORIGIN_SUFFIXES):
        return True
    return lowered.endswith(".svg") and any(marker in lowered for marker in STATIC_FONT_PATH_MARKERS)


def _is_cacheable_static_response(current_url: str, content_type: str = "") -> bool:
    path = urlparse(current_url).path.lower()
    if not path.startswith("/static/") or not _should_keep_static_same_origin(path):
        return False
    if path.endswith((".js", ".mjs")) or _is_javascript_content_type(content_type):
        return False
    return True


def _direct_static_url(parsed: Any) -> str:
    direct = f"{RONGHUI_ORIGIN}{parsed.path}"
    if parsed.query:
        direct = f"{direct}?{parsed.query}"
    if parsed.fragment:
        direct = f"{direct}#{parsed.fragment}"
    return direct


def _proxy_url_for(remote_url: str, *, current_url: str, proxy_prefix: str) -> str:
    original = _clean_text(remote_url)
    value = SLASH_ESCAPE_RE.sub("/", original)
    if not value or value.startswith(("#", "$", "javascript:", "data:", "mailto:", "tel:")):
        return original
    if "://" not in value and not value.startswith(("/", "//")) and _is_relative_allowed_path(value):
        value = f"/{value}"
    absolute = urljoin(current_url, value)
    parsed = urlparse(absolute)
    if parsed.netloc and parsed.netloc.lower() != urlparse(RONGHUI_ORIGIN).netloc:
        return original
    if not _is_allowed_path(parsed.path):
        return original
    if parsed.path.startswith("/static/") and not _should_keep_static_same_origin(parsed.path):
        return _direct_static_url(parsed)
    proxied = f"{proxy_prefix.rstrip('/')}{parsed.path}"
    if parsed.query:
        proxied = f"{proxied}?{parsed.query}"
    if parsed.fragment:
        proxied = f"{proxied}#{parsed.fragment}"
    return proxied


def _rewrite_refresh_header(value: str, *, current_url: str, proxy_prefix: str) -> str:
    def replace(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{match.group('quote')}{rewritten}{match.group('quote')}"

    return REFRESH_HEADER_URL_RE.sub(replace, str(value or ""), count=1)


def _proxy_base_url_for(remote_url: str, *, current_url: str, proxy_prefix: str) -> str:
    original = _clean_text(remote_url)
    value = SLASH_ESCAPE_RE.sub("/", original)
    if not value or value.startswith(("#", "$", "javascript:", "data:", "mailto:", "tel:")):
        return original
    absolute = urljoin(current_url, value)
    parsed = urlparse(absolute)
    if parsed.netloc and parsed.netloc.lower() != urlparse(RONGHUI_ORIGIN).netloc:
        return original
    if parsed.path in {"", "/"}:
        proxied = f"{proxy_prefix.rstrip('/')}/"
        if parsed.query:
            proxied = f"{proxied}?{parsed.query}"
        if parsed.fragment:
            proxied = f"{proxied}#{parsed.fragment}"
        return proxied
    return _proxy_url_for(original, current_url=current_url, proxy_prefix=proxy_prefix)


def _rewrite_urls(text: str, *, current_url: str, proxy_prefix: str) -> str:
    def remove_meta_csp(match: re.Match[str]) -> str:
        tag = match.group(0)
        return "" if META_CSP_HTTP_EQUIV_RE.search(tag) else tag

    def replace_base_href(match: re.Match[str]) -> str:
        rewritten = _proxy_base_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{match.group('quote')}{escape(rewritten, quote=True)}{match.group('quote')}"

    def replace_unquoted_base_href(match: re.Match[str]) -> str:
        rewritten = _proxy_base_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{rewritten}"

    def replace_attr(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{match.group('quote')}{escape(rewritten, quote=True)}{match.group('quote')}"

    def replace_unquoted_attr(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{rewritten}"

    def replace_quoted(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('quote')}{rewritten}{match.group('quote')}"

    def replace_css_url(match: re.Match[str]) -> str:
        rewritten = _proxy_url_for(match.group("url"), current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{match.group('quote')}{rewritten}{match.group('quote')}{match.group('suffix')}"

    def defer_map_iframe_src(match: re.Match[str]) -> str:
        quote = match.group("quote")
        url = match.group("url")
        return f"{match.group('prefix')}{quote}about:blank{quote} data-codex-deferred-src={quote}{url}{quote}"

    def replace_srcset_attr(match: re.Match[str]) -> str:
        rewritten = _rewrite_srcset_value(
            match.group("value"),
            current_url=current_url,
            proxy_prefix=proxy_prefix,
        )
        return f"{match.group('prefix')}{match.group('quote')}{escape(rewritten, quote=True)}{match.group('quote')}"

    def replace_srcdoc_attr(match: re.Match[str]) -> str:
        decoded = unescape(match.group("value"))
        rewritten = _rewrite_urls(decoded, current_url=current_url, proxy_prefix=proxy_prefix)
        return f"{match.group('prefix')}{match.group('quote')}{escape(rewritten, quote=True)}{match.group('quote')}"

    rewritten = META_TAG_RE.sub(remove_meta_csp, text)
    rewritten = BASE_HREF_RE.sub(replace_base_href, rewritten)
    rewritten = UNQUOTED_BASE_HREF_RE.sub(replace_unquoted_base_href, rewritten)
    rewritten = SRCDOC_ATTR_RE.sub(replace_srcdoc_attr, rewritten)
    rewritten = OBJECT_DATA_ATTR_RE.sub(replace_attr, rewritten)
    rewritten = RONGHUI_MAP_IFRAME_SRC_RE.sub(defer_map_iframe_src, rewritten)
    rewritten = ATTR_URL_RE.sub(replace_attr, rewritten)
    rewritten = SRCSET_ATTR_RE.sub(replace_srcset_attr, rewritten)
    rewritten = UNQUOTED_OBJECT_DATA_ATTR_RE.sub(replace_unquoted_attr, rewritten)
    rewritten = UNQUOTED_ATTR_URL_RE.sub(replace_unquoted_attr, rewritten)
    rewritten = CSS_URL_RE.sub(replace_css_url, rewritten)
    rewritten = RELATIVE_CSS_URL_RE.sub(replace_css_url, rewritten)
    rewritten = ESCAPED_QUOTED_RONGHUI_URL_RE.sub(replace_quoted, rewritten)
    rewritten = ESCAPED_RELATIVE_QUOTED_RONGHUI_URL_RE.sub(replace_quoted, rewritten)
    rewritten = QUOTED_RONGHUI_URL_RE.sub(replace_quoted, rewritten)
    return RELATIVE_QUOTED_RONGHUI_URL_RE.sub(replace_quoted, rewritten)


def _rewrite_srcset_value(value: str, *, current_url: str, proxy_prefix: str) -> str:
    candidates: list[str] = []
    for candidate in str(value or "").split(","):
        item = candidate.strip()
        if not item:
            continue
        parts = item.split(None, 1)
        rewritten = _proxy_url_for(parts[0], current_url=current_url, proxy_prefix=proxy_prefix)
        if len(parts) == 2:
            candidates.append(f"{rewritten} {parts[1]}")
        else:
            candidates.append(rewritten)
    return ", ".join(candidates)


RONGHUI_PREFILL_HELPER = """
<script id="codex-ronghui-prefill-script">
(function () {
  if (window.codexManualPrefill && window.codexManualPrefill.ronghui) return;
  window.codexManualPrefill = window.codexManualPrefill || {};
  function clean(value) { return String(value == null ? "" : value).trim(); }
  function namesOf(spec) {
    var names = [];
    if (spec && spec.key) names.push(spec.key);
    if (spec && Array.isArray(spec.names)) names = names.concat(spec.names);
    return names.map(clean).filter(Boolean);
  }
  function setMiniControlValue(control, value) {
    try {
      if (!control) return false;
      if (typeof control.setValue === "function") control.setValue(value);
      if (typeof control.setText === "function") control.setText(value);
      if (typeof control.doValueChanged === "function") control.doValueChanged();
      if (typeof control.fire === "function") control.fire("valuechanged");
      return true;
    } catch (_) {
      return false;
    }
  }
  function setMiniValue(name, value) {
    try {
      if (!window.mini || typeof window.mini.get !== "function") return false;
      return setMiniControlValue(window.mini.get(name), value);
    } catch (_) {
      return false;
    }
  }
  function dispatchFieldEvents(element) {
    ["input", "change", "blur"].forEach(function (type) {
      try { element.dispatchEvent(new Event(type, { bubbles: true })); } catch (_) {}
    });
  }
  function setMiniElementValue(element, value) {
    if (!element || !window.mini || typeof window.mini.get !== "function") return false;
    var cursor = element;
    var depth = 0;
    while (cursor && depth < 6) {
      var id = cursor.getAttribute && cursor.getAttribute("id");
      if (id && setMiniControlValue(window.mini.get(id), value)) return true;
      cursor = cursor.parentElement;
      depth += 1;
    }
    return false;
  }
  function setElementValue(element, value) {
    if (!element) return false;
    try {
      if (setMiniElementValue(element, value)) {
        dispatchFieldEvents(element);
        return true;
      }
      if (element.tagName === "SELECT") {
        var wanted = clean(value);
        Array.prototype.some.call(element.options || [], function (option) {
          if (clean(option.value) === wanted || clean(option.textContent) === wanted) {
            element.value = option.value;
            return true;
          }
          return false;
        });
      } else {
        element.value = value;
      }
      dispatchFieldEvents(element);
      return true;
    } catch (_) {
      return false;
    }
  }
  function selectorSafe(name) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(name);
    return String(name).replace(/["\\\\]/g, "\\\\$&");
  }
  var SECTION_LABELS = ["发货信息", "寄件信息", "收件信息", "代收货款", "货物信息", "签回单", "融定制服务", "成本信息", "折扣信息", "其他费用", "收入信息"];
  function normalizeLabel(value) {
    return clean(value).replace(/[：:*＊\\s]/g, "");
  }
  function elementOwnText(element) {
    var text = "";
    Array.prototype.slice.call(element && element.childNodes || []).forEach(function (node) {
      if (node && node.nodeType === 3) text += " " + node.textContent;
    });
    if (clean(text)) return clean(text);
    if (element && element.children && element.children.length <= 1) return clean(element.textContent);
    return "";
  }
  function isSectionHeaderText(text) {
    var normalized = normalizeLabel(text);
    if (!normalized) return false;
    for (var i = 0; i < SECTION_LABELS.length; i += 1) {
      var wanted = normalizeLabel(SECTION_LABELS[i]);
      if (normalized === wanted || (normalized.indexOf(wanted) !== -1 && normalized.length <= wanted.length + 8)) return true;
    }
    return false;
  }
  function sectionIndex(section, elements) {
    var wanted = normalizeLabel(section);
    if (!wanted) return -1;
    for (var i = 0; i < elements.length; i += 1) {
      var text = normalizeLabel(elementOwnText(elements[i]));
      if (text === wanted || (text.indexOf(wanted) !== -1 && text.length <= wanted.length + 8)) return i;
    }
    return -1;
  }
  function sectionElements(section, selector) {
    var matches = Array.prototype.slice.call(document.querySelectorAll(selector));
    if (!section) return matches;
    var elements = Array.prototype.slice.call(document.querySelectorAll("body *"));
    var start = sectionIndex(section, elements);
    if (start < 0) return matches;
    var end = elements.length;
    var currentSection = normalizeLabel(section);
    for (var i = start + 1; i < elements.length; i += 1) {
      var text = normalizeLabel(elementOwnText(elements[i]));
      if (!text || text === currentSection || (text.indexOf(currentSection) !== -1 && text.length <= currentSection.length + 8)) continue;
      if (isSectionHeaderText(text)) {
        end = i;
        break;
      }
    }
    return matches.filter(function (element) {
      var index = elements.indexOf(element);
      return index >= start && index < end;
    });
  }
  function elementInSection(element, section) {
    if (!section) return true;
    return sectionElements(section, "input,textarea,select").indexOf(element) !== -1;
  }
  function findDomField(name, section) {
    var escaped = selectorSafe(name);
    var selectors = [
      "#" + escaped,
      '[name="' + escaped + '"]',
      '[data-field="' + escaped + '"]',
      '[data-ronghui-field="' + escaped + '"]',
      '[placeholder*="' + escaped + '"]'
    ];
    for (var i = 0; i < selectors.length; i += 1) {
      var found = document.querySelector(selectors[i]);
      if (found && /^(INPUT|TEXTAREA|SELECT)$/.test(found.tagName || "") && elementInSection(found, section)) return found;
    }
    return null;
  }
  function findFollowingField(label) {
    var field = null;
    var next = label.nextElementSibling;
    while (next) {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(next.tagName || "")) return next;
      field = next.querySelector && next.querySelector("input,textarea,select");
      if (field) return field;
      next = next.nextElementSibling;
    }
    next = label.parentElement && label.parentElement.nextElementSibling;
    while (next) {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(next.tagName || "")) return next;
      field = next.querySelector && next.querySelector("input,textarea,select");
      if (field) return field;
      next = next.nextElementSibling;
    }
    return null;
  }
  function findFieldNearLabel(name, section) {
    var labels = sectionElements(section, "label,td,th,span,div");
    for (var i = 0; i < labels.length; i += 1) {
      var label = labels[i];
      var text = normalizeLabel(elementOwnText(label));
      var wanted = normalizeLabel(name);
      if (!labelTextMatches(text, wanted)) continue;
      var field = findFollowingField(label);
      if (field) return field;
      var scope = label.closest("tr,.form-group,.mini-panel,.mini-tabs,.search_forms_row,.form_item") || label.parentElement;
      field = scope && scope.querySelector("input,textarea,select");
      if (field) return field;
    }
    return null;
  }
  function labelTextMatches(text, wanted) {
    if (!wanted || !text) return false;
    if (text === wanted) return true;
    if (text.indexOf("查询") !== -1 && wanted.indexOf("查询") === -1) return false;
    if (wanted.length < 3) return false;
    return text.indexOf(wanted) !== -1 && text.length <= wanted.length + 4;
  }
  function fillSpec(spec) {
    var value = clean(spec && spec.value);
    if (!value) return { ok: true, skipped: true, key: clean(spec && spec.key) };
    var names = namesOf(spec);
    for (var i = 0; i < names.length; i += 1) {
      var name = names[i];
      if (setMiniValue(name, value)) return { ok: true, key: clean(spec.key || name), matched: name };
      var field = findDomField(name, spec.section) || findFieldNearLabel(name, spec.section);
      if (setElementValue(field, value)) return { ok: true, key: clean(spec.key || name), matched: name };
    }
    return { ok: false, key: clean(spec && spec.key) || names[0] || "unknown" };
  }
  function normalizeFields(payload) {
    var fields = payload && payload.fields;
    if (Array.isArray(fields)) return fields;
    if (fields && typeof fields === "object") {
      return Object.keys(fields).map(function (key) { return { key: key, names: [key], value: fields[key] }; });
    }
    return [];
  }
  function isVisible(element) {
    if (!element || !element.ownerDocument) return false;
    try {
      var style = window.getComputedStyle(element);
      if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity || 1) === 0) return false;
      return Boolean(element.offsetWidth || element.offsetHeight || element.getClientRects().length);
    } catch (_) {
      return false;
    }
  }
  function isEditableField(element) {
    if (!element || !/^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName || "")) return false;
    if (element.disabled || element.readOnly) return false;
    if (element.type && /^(hidden|button|submit|reset|checkbox|radio)$/i.test(element.type)) return false;
    return isVisible(element);
  }
  function passiveStartupNoticeText(text) {
    var normalized = clean(text).replace(/\\s+/g, "");
    if (!normalized) return false;
    if (!/(提示|温馨提示|通知|公告|消息)/.test(normalized)) return false;
    if (/(保存|提交|删除|作废|审核|结算|运费|代收|生成单号|打印|是否|确认录单)/.test(normalized)) return false;
    return true;
  }
  function visibleNoticeScopes() {
    var selectors = [
      ".mini-messagebox",
      ".mini-window",
      ".mini-panel",
      ".mini-popup",
      ".mini-messagebox-content"
    ].join(",");
    return Array.prototype.filter.call(document.querySelectorAll(selectors), isVisible);
  }
  function dismissStartupNotice() {
    var scopes = visibleNoticeScopes();
    for (var i = 0; i < scopes.length; i += 1) {
      var scope = scopes[i];
      var text = clean(scope.textContent || "");
      if (!passiveStartupNoticeText(text)) continue;
      var controls = Array.prototype.slice.call(scope.querySelectorAll("button,a,.mini-button,.mini-tools-close,.mini-panel-close"));
      for (var j = 0; j < controls.length; j += 1) {
        var control = controls[j];
        var label = clean(control.textContent || control.title || control.getAttribute("aria-label") || "");
        var isCloseTool = /mini-tools-close|mini-panel-close/.test(String(control.className || ""));
        if (!isCloseTool && !/^(确定|关闭|知道了|我知道了)$/.test(label)) continue;
        try {
          control.click();
          return true;
        } catch (_) {}
      }
    }
    return false;
  }
  function hasBlockingStartupNotice() {
    var scopes = visibleNoticeScopes();
    for (var i = 0; i < scopes.length; i += 1) {
      if (passiveStartupNoticeText(scopes[i].textContent || "")) return true;
    }
    return false;
  }
  function hasLoadingMask() {
    var selectors = [
      ".mini-mask-loading",
      ".mini-mask-msg",
      ".mini-mask",
      ".loading",
      "[class*='loading']"
    ].join(",");
    return Array.prototype.some.call(document.querySelectorAll(selectors), function (element) {
      return isVisible(element) && !passiveStartupNoticeText(element.textContent || "");
    });
  }
  function hasWritableEntryFields() {
    var fields = []
      .concat(sectionElements("寄件信息", "input,textarea,select"))
      .concat(sectionElements("收件信息", "input,textarea,select"))
      .concat(sectionElements("货物信息", "input,textarea,select"));
    if (fields.some(isEditableField)) return true;
    return Array.prototype.some.call(document.querySelectorAll("input,textarea,select"), isEditableField);
  }
  function isRonghuiPrefillReady() {
    dismissStartupNotice();
    if (document.readyState !== "complete") return false;
    if (!document.body) return false;
    var bodyText = clean(document.body.innerText || document.body.textContent || "");
    if (!bodyText || /AUTH_REQUIRED|AUTH_PENDING_CODE|登录态已失效|登录态已过期|验证码/.test(bodyText)) return false;
    if (!/(运单编号|发货日期)/.test(bodyText)) return false;
    if (!/(寄件信息|寄件人|发货信息)/.test(bodyText)) return false;
    if (!/(收件信息|收件人)/.test(bodyText)) return false;
    if (hasBlockingStartupNotice() || hasLoadingMask()) return false;
    return hasWritableEntryFields();
  }
  var prefillRunSerial = 0;
  var activePrefillKey = "";
  var activePrefillRunning = false;
  var prefillReadyTimer = 0;
  var prefillReadyNotified = false;
  function postPrefillReady() {
    try {
      window.parent.postMessage({
        type: "SHIPNOW_PREFILL_READY",
        provider: "ronghui"
      }, window.location.origin);
    } catch (_) {}
  }
  function waitForRonghuiPrefillReady(attempt) {
    if (prefillReadyTimer) {
      window.clearTimeout(prefillReadyTimer);
      prefillReadyTimer = 0;
    }
    if (prefillReadyNotified) return;
    if (isRonghuiPrefillReady()) {
      prefillReadyNotified = true;
      postPrefillReady();
      return;
    }
    if (attempt < 80) {
      prefillReadyTimer = window.setTimeout(function () {
        waitForRonghuiPrefillReady(attempt + 1);
      }, 500);
    }
  }
  function notifyPrefillReadyWhenReady() {
    waitForRonghuiPrefillReady(0);
  }
  function runPrefill(message, attempt, serial) {
    var payload = message.payload || {};
    var specs = normalizeFields(payload);
    var filled = [];
    var missing = [];
    if (serial !== prefillRunSerial) return;
    if (!isRonghuiPrefillReady()) {
      if (attempt < 80) {
        window.setTimeout(function () { runPrefill(message, attempt + 1, serial); }, 500);
        return;
      }
      window.parent.postMessage({
        type: "SHIPNOW_PREFILL_RESULT",
        provider: "ronghui",
        ok: false,
        filled: filled,
        missing: specs.map(function (spec) { return clean(spec && spec.key) || "unknown"; }),
        error: "融辉原页尚未加载完成"
      }, window.location.origin);
      if (serial === prefillRunSerial) activePrefillRunning = false;
      return;
    }
    specs.forEach(function (spec) {
      var result = fillSpec(spec);
      if (result.skipped) return;
      if (result.ok) filled.push(result.key);
      else missing.push(result.key);
    });
    if (missing.length && attempt < 80) {
      window.setTimeout(function () { runPrefill(message, attempt + 1, serial); }, 500);
      return;
    }
    window.parent.postMessage({
      type: "SHIPNOW_PREFILL_RESULT",
      provider: "ronghui",
      ok: Boolean(filled.length),
      filled: filled,
      missing: missing
    }, window.location.origin);
    if (serial === prefillRunSerial) activePrefillRunning = false;
  }
  function startPrefill(message) {
    var payloadKey = clean(message && message.prefill_key);
    if (payloadKey && payloadKey === activePrefillKey && activePrefillRunning) {
      // Ignore repeated parent sends for the same payload; keep the current retry loop alive.
      return;
    }
    activePrefillKey = payloadKey || "";
    activePrefillRunning = true;
    prefillRunSerial += 1;
    runPrefill(message, 0, prefillRunSerial);
  }
  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (event.origin !== window.location.origin) return;
    if (data.type !== "SHIPNOW_PREFILL" || data.provider !== "ronghui") return;
    startPrefill(data);
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", notifyPrefillReadyWhenReady, { once: true });
  }
  window.addEventListener("load", notifyPrefillReadyWhenReady, { once: true });
  window.setTimeout(notifyPrefillReadyWhenReady, 300);
  window.setTimeout(notifyPrefillReadyWhenReady, 1200);
  notifyPrefillReadyWhenReady();
  window.codexManualPrefill.ronghui = { setMiniValue: setMiniValue, run: startPrefill };
})();
</script>
"""


def _runtime_proxy_helper(*, proxy_prefix: str, user_info_cookie: str = "") -> str:
    allowed_paths = "|".join(re.escape(prefix) for prefix in ALLOWED_PATH_PREFIXES)
    relative_allowed_paths = "|".join(re.escape(prefix) for prefix in RELATIVE_ALLOWED_PATH_PREFIXES)
    return f"""
<script id="codex-ronghui-proxy-script">
(function () {{
  var proxyPrefix = {json.dumps(proxy_prefix.rstrip("/"), ensure_ascii=False)};
  var remoteOrigin = {json.dumps(RONGHUI_ORIGIN, ensure_ascii=False)};
  var ronghuiUserInfoCookie = {json.dumps(user_info_cookie, ensure_ascii=False)};
  var allowedPath = new RegExp("^(?:" + {json.dumps(allowed_paths, ensure_ascii=False)} + ")");
  var relativeAllowedPath = new RegExp("^(?:" + {json.dumps(relative_allowed_paths, ensure_ascii=False)} + ")");
  var allowedReferencePattern = new RegExp("(^|[\\\\s\\\"'=(`>])\\\\/?(?:" + {json.dumps(relative_allowed_paths, ensure_ascii=False)} + ")", "i");
  var cacheableDataQueryCallIds = makeSet({json.dumps(sorted(CACHEABLE_DATAQUERY_CALL_IDS), ensure_ascii=False)});
  var cacheableAddressCallIds = makeSet({json.dumps(sorted(CACHEABLE_ADDRESS_CALL_IDS), ensure_ascii=False)});
  var cacheableMinicOptionCodes = makeSet({json.dumps(sorted(CACHEABLE_MINIC_OPTION_CODES), ensure_ascii=False)});
  var rewritingHtmlText = false;
  function makeSet(values) {{
    var out = Object.create(null);
    for (var i = 0; i < values.length; i += 1) out[values[i]] = true;
    return out;
  }}
  function ensureRonghuiUserInfoCookie() {{
    if (!ronghuiUserInfoCookie) return;
    try {{
      if (!/(?:^|;\\s*)userInfo=/.test(document.cookie || "")) {{
        document.cookie = "userInfo=" + ronghuiUserInfoCookie + "; path=/; SameSite=Lax";
      }}
    }} catch (_) {{}}
  }}
  function shouldKeepStaticSameOrigin(pathname) {{
    var path = String(pathname || "").toLowerCase();
    return /\\.(?:css|woff2?|ttf|eot|otf)$/.test(path) ||
      (/\\.svg$/.test(path) && /\\/(?:fonts?|iconfont)\\//.test(path));
  }}
  function mayContainRonghuiReference(value) {{
    if (typeof value !== "string") return false;
    return value.indexOf(remoteOrigin) !== -1 ||
      value.indexOf("//tms.ronghuiwl.com") !== -1 ||
      value.indexOf(proxyPrefix + "/") !== -1 ||
      allowedReferencePattern.test(value);
  }}
  function normalizeLookupCacheBuster(input) {{
    if (typeof input !== "string") return input;
    try {{
      var url = new URL(input, window.location.href);
      if (url.origin !== window.location.origin || url.pathname.indexOf(proxyPrefix + "/") !== 0) return input;
      var remotePath = url.pathname.slice(proxyPrefix.length);
      var id = url.searchParams.get("id") || "";
      var optionCode = url.searchParams.get("optionCode") || "";
      var key = url.searchParams.get("key") || "";
      var shouldNormalize = false;
      if (key) return input;
      if ((remotePath === "/dataQuery/findAllByCallId" || remotePath === "/dataQuery/findPageByCallId") && cacheableDataQueryCallIds[id]) {{
        shouldNormalize = true;
      }} else if (remotePath === "/address/inputtips" && cacheableAddressCallIds[id]) {{
        shouldNormalize = true;
      }} else if (remotePath === "/minic/combobox" && cacheableMinicOptionCodes[optionCode]) {{
        shouldNormalize = true;
      }}
      if (!shouldNormalize || !url.searchParams.has("_")) return input;
      url.searchParams.delete("_");
      return url.pathname + (url.searchParams.toString() ? "?" + url.searchParams.toString() : "") + url.hash;
    }} catch (_) {{
      return input;
    }}
  }}
  function rewriteRonghuiUrl(input) {{
    var original = input;
    var raw = "";
    if (typeof input === "string") {{
      raw = input.trim();
    }} else if (input && typeof input.href === "string") {{
      raw = input.href.trim();
    }} else {{
      return input;
    }}
    if (!raw || /^(?:#|javascript:|data:|mailto:|tel:)/i.test(raw)) return input;
    if (!/^(?:[a-z][a-z0-9+.-]*:|\\/)/i.test(raw) && relativeAllowedPath.test(raw)) {{
      raw = "/" + raw;
    }}
    try {{
      var url = new URL(raw, window.location.href);
      if (url.origin === window.location.origin && url.pathname.indexOf(proxyPrefix + "/") === 0) {{
        if (url.pathname.indexOf(proxyPrefix + "/static/") === 0) {{
          if (shouldKeepStaticSameOrigin(url.pathname.slice(proxyPrefix.length))) return input;
          return remoteOrigin + url.pathname.slice(proxyPrefix.length) + url.search + url.hash;
        }}
        return normalizeLookupCacheBuster(url.pathname + url.search + url.hash);
      }}
      if (url.origin === remoteOrigin && url.pathname.indexOf(proxyPrefix + "/") === 0) {{
        return normalizeLookupCacheBuster(url.pathname + url.search + url.hash);
      }}
      if ((url.origin === remoteOrigin || url.origin === window.location.origin) && allowedPath.test(url.pathname)) {{
        if (url.pathname.indexOf("/static/") === 0) {{
          if (shouldKeepStaticSameOrigin(url.pathname)) return proxyPrefix + url.pathname + url.search + url.hash;
          return remoteOrigin + url.pathname + url.search + url.hash;
        }}
        return normalizeLookupCacheBuster(proxyPrefix + url.pathname + url.search + url.hash);
      }}
    }} catch (_) {{}}
    return original;
  }}
  function rewriteSrcset(value) {{
    if (typeof value !== "string") return value;
    return value.split(",").map(function (candidate) {{
      var item = candidate.trim();
      if (!item) return "";
      var parts = item.split(/\\s+/, 2);
      var rewritten = rewriteRonghuiUrl(parts[0]);
      var descriptor = item.slice(parts[0].length).trim();
      return descriptor ? rewritten + " " + descriptor : rewritten;
    }}).filter(Boolean).join(", ");
  }}
  function rewriteTextUrls(value) {{
    if (typeof value !== "string") return value;
    return value.replace(/(\\burl\\(\\s*)(['"]?)([^'")\\s]+)(\\2)(\\s*\\))/gi, function (_, prefix, quote, url, suffixQuote, suffix) {{
      var rewritten = rewriteRonghuiUrl(url);
      return prefix + quote + rewritten + suffixQuote + suffix;
    }});
  }}
  function rewriteCssImportText(value) {{
    if (typeof value !== "string") return value;
    return value.replace(/(@import\\s+)(?!url\\()(['"])([^'"]+)(\\2)/gi, function (_, prefix, quote, url, suffixQuote) {{
      var rewritten = rewriteRonghuiUrl(url);
      return prefix + quote + rewritten + suffixQuote;
    }});
  }}
  function rewriteStyleText(value) {{
    return rewriteCssImportText(rewriteTextUrls(value));
  }}
  function rewriteRonghuiBaseUrl(input) {{
    var rewritten = rewriteRonghuiUrl(input);
    if (rewritten !== input) return rewritten;
    var raw = "";
    if (typeof input === "string") {{
      raw = input.trim();
    }} else if (input && typeof input.href === "string") {{
      raw = input.href.trim();
    }} else {{
      return input;
    }}
    if (!raw || /^(?:#|javascript:|data:|mailto:|tel:)/i.test(raw)) return input;
    try {{
      var url = new URL(raw, window.location.href);
      if (url.origin === window.location.origin && url.pathname.indexOf(proxyPrefix + "/") === 0) {{
        return input;
      }}
      if ((url.origin === remoteOrigin || url.origin === window.location.origin) && (url.pathname === "/" || url.pathname === "")) {{
        return proxyPrefix + "/" + url.search + url.hash;
      }}
    }} catch (_) {{}}
    return input;
  }}
  function rewriteMetaRefreshContent(value) {{
    if (typeof value !== "string" || !/\\burl\\s*=/i.test(value)) return value;
    return value.replace(/(\\burl\\s*=\\s*)(['"]?)([^'";\\s]+)(\\2)/i, function (_, prefix, quote, url, suffixQuote) {{
      var rewritten = rewriteRonghuiUrl(url);
      return prefix + quote + rewritten + suffixQuote;
    }});
  }}
  function isMetaRefresh(element) {{
    if (!element || !element.getAttribute) return false;
    var tagName = String(element.tagName || "").toLowerCase();
    var httpEquiv = String(element.getAttribute("http-equiv") || element.httpEquiv || "").toLowerCase();
    return tagName === "meta" && httpEquiv === "refresh";
  }}
  function isMetaContentSecurityPolicy(element) {{
    if (!element || !element.getAttribute) return false;
    var tagName = String(element.tagName || "").toLowerCase();
    var httpEquiv = String(element.getAttribute("http-equiv") || element.httpEquiv || "").toLowerCase();
    return tagName === "meta" && httpEquiv === "content-security-policy";
  }}
  function rewriteMetaRefreshElement(element) {{
    if (!isMetaRefresh(element) || !element.getAttribute || !element.setAttribute) return;
    var value = element.getAttribute("content") || "";
    var rewritten = rewriteMetaRefreshContent(value);
    if (rewritten && rewritten !== value) element.setAttribute("content", rewritten);
  }}
  function removeMetaContentSecurityPolicy(element) {{
    if (!isMetaContentSecurityPolicy(element) || !element.parentNode) return;
    try {{
      element.parentNode.removeChild(element);
    }} catch (_) {{}}
  }}
  function patchUrlAttributes() {{
    if (!window.Element || !window.Element.prototype || !window.Element.prototype.setAttribute) return;
    var originalSetAttribute = window.Element.prototype.setAttribute;
    window.Element.prototype.setAttribute = function (name, value) {{
      var key = String(name || "").toLowerCase();
      var tagName = String(this.tagName || "").toLowerCase();
      if (key === "href" && tagName === "base") {{
        value = rewriteRonghuiBaseUrl(value);
      }} else if (key === "data" && tagName === "object") {{
        value = rewriteRonghuiUrl(value);
      }} else if (
        key === "src" || key === "href" || key === "action" || key === "formaction" ||
        key === "url" || key === "data-url" || key === "data-src" || key === "data-href" ||
        key === "poster" || key === "background"
      ) {{
        value = rewriteRonghuiUrl(value);
      }} else if (key === "style") {{
        value = rewriteStyleText(value);
      }} else if (key === "srcdoc") {{
        value = rewriteHtmlText(value);
      }} else if (key === "srcset") {{
        value = rewriteSrcset(value);
      }} else if (key === "content" && isMetaRefresh(this)) {{
        value = rewriteMetaRefreshContent(value);
      }}
      return originalSetAttribute.call(this, name, value);
    }};
  }}
  function patchUrlProperty(proto, property, rewriter) {{
    if (!proto) return;
    var rewrite = rewriter || rewriteRonghuiUrl;
    var descriptor = Object.getOwnPropertyDescriptor(proto, property);
    if (!descriptor || !descriptor.configurable || !descriptor.set) return;
    Object.defineProperty(proto, property, {{
      configurable: true,
      enumerable: descriptor.enumerable,
      get: function () {{
        return descriptor.get ? descriptor.get.call(this) : this.getAttribute(property);
      }},
      set: function (value) {{
        return descriptor.set.call(this, rewrite.call(this, value));
      }}
    }});
  }}
  function rewriteElementUrlAttribute(element, attrName) {{
    if (!element || !element.getAttribute || !element.setAttribute) return;
    var value = element.getAttribute(attrName) || "";
    var rewritten = rewriteRonghuiUrl(value);
    if (rewritten && rewritten !== value) element.setAttribute(attrName, rewritten);
  }}
  function rewriteElementStyleAttribute(element) {{
    if (!element || !element.getAttribute || !element.setAttribute) return;
    var value = element.getAttribute("style") || "";
    var rewritten = rewriteStyleText(value);
    if (rewritten && rewritten !== value) element.setAttribute("style", rewritten);
  }}
  function rewriteElementSrcdocAttribute(element) {{
    if (!element || !element.getAttribute || !element.setAttribute) return;
    var value = element.getAttribute("srcdoc") || "";
    var rewritten = rewriteHtmlText(value);
    if (rewritten && rewritten !== value) element.setAttribute("srcdoc", rewritten);
  }}
  function rewriteBaseHrefElement(element) {{
    if (!element || !element.getAttribute || !element.setAttribute) return;
    var value = element.getAttribute("href") || "";
    var rewritten = rewriteRonghuiBaseUrl(value);
    if (rewritten && rewritten !== value) element.setAttribute("href", rewritten);
  }}
  function rewriteKnownUrlAttributes(root) {{
    if (!root || !root.querySelectorAll) return;
    var selector = "[src],[href],[action],[formaction],[url],[data-url],[data-src],[data-href],[poster],[background],[srcset],[style],[srcdoc]";
    var elements = [];
    try {{
      if (root.matches && root.matches(selector)) elements.push(root);
      elements = elements.concat(Array.prototype.slice.call(root.querySelectorAll(selector)));
    }} catch (_) {{
      return;
    }}
    elements.forEach(function (element) {{
      [
        "src", "href", "action", "formaction", "url", "data-url",
        "data-src", "data-href", "poster", "background"
      ].forEach(function (attrName) {{
        rewriteElementUrlAttribute(element, attrName);
      }});
      rewriteElementStyleAttribute(element);
      rewriteElementSrcdocAttribute(element);
      if (element.getAttribute && element.setAttribute) {{
        var srcset = element.getAttribute("srcset") || "";
        var rewrittenSrcset = rewriteSrcset(srcset);
        if (rewrittenSrcset && rewrittenSrcset !== srcset) element.setAttribute("srcset", rewrittenSrcset);
      }}
    }});
    try {{
      if (root.matches && root.matches("base[href]")) rewriteBaseHrefElement(root);
      Array.prototype.slice.call(root.querySelectorAll("base[href]")).forEach(rewriteBaseHrefElement);
    }} catch (_) {{}}
    if (root.querySelectorAll) {{
      Array.prototype.slice.call(root.querySelectorAll('meta[http-equiv="refresh"],meta[http-equiv="Refresh"],meta[http-equiv="REFRESH"]')).forEach(rewriteMetaRefreshElement);
    }}
    try {{
      if (root.matches && root.matches("meta[http-equiv]")) removeMetaContentSecurityPolicy(root);
      Array.prototype.slice.call(root.querySelectorAll("meta[http-equiv]")).forEach(removeMetaContentSecurityPolicy);
    }} catch (_) {{}}
    try {{
      if (root.matches && root.matches("object[data]")) rewriteElementUrlAttribute(root, "data");
      Array.prototype.slice.call(root.querySelectorAll("object[data]")).forEach(function (element) {{
        rewriteElementUrlAttribute(element, "data");
      }});
    }} catch (_) {{}}
  }}
  function rewriteHtmlText(value) {{
    if (typeof value !== "string") return value;
    if (rewritingHtmlText) return value;
    if (!mayContainRonghuiReference(value)) return value;
    value = rewriteTextUrls(value);
    if (value.indexOf("<") === -1) return value;
    try {{
      rewritingHtmlText = true;
      var template = document.createElement("template");
      template.innerHTML = value;
      rewriteKnownUrlAttributes(template.content || template);
      return template.innerHTML;
    }} catch (_) {{
      return value;
    }} finally {{
      rewritingHtmlText = false;
    }}
  }}
  function rewriteFormAction(form) {{
    if (!form || !form.getAttribute || !form.setAttribute) return;
    rewriteElementUrlAttribute(form, "action");
  }}
  function rewriteSubmitterAction(submitter) {{
    rewriteElementUrlAttribute(submitter, "formaction");
  }}
  function patchFormSubmit() {{
    if (!window.HTMLFormElement || !window.HTMLFormElement.prototype) return;
    var originalSubmit = window.HTMLFormElement.prototype.submit;
    if (originalSubmit) {{
      window.HTMLFormElement.prototype.submit = function () {{
        rewriteFormAction(this);
        return originalSubmit.apply(this, arguments);
      }};
    }}
    var originalRequestSubmit = window.HTMLFormElement.prototype.requestSubmit;
    if (originalRequestSubmit) {{
      window.HTMLFormElement.prototype.requestSubmit = function () {{
        rewriteFormAction(this);
        if (arguments.length > 0) rewriteSubmitterAction(arguments[0]);
        return originalRequestSubmit.apply(this, arguments);
      }};
    }}
  }}
  function patchHistoryMethod(name) {{
    if (!window.history || !window.history[name]) return;
    var originalMethod = window.history[name];
    window.history[name] = function () {{
      if (arguments.length > 2) arguments[2] = rewriteRonghuiUrl(arguments[2]);
      return originalMethod.apply(this, arguments);
    }};
  }}
  function patchHtmlFragmentSinks() {{
    patchUrlProperty(window.Element && window.Element.prototype, "innerHTML", rewriteHtmlText);
    patchUrlProperty(window.Element && window.Element.prototype, "outerHTML", rewriteHtmlText);
    if (window.Element && window.Element.prototype && window.Element.prototype.insertAdjacentHTML) {{
      var originalInsertAdjacentHTML = window.Element.prototype.insertAdjacentHTML;
      window.Element.prototype.insertAdjacentHTML = function (position, text) {{
        return originalInsertAdjacentHTML.call(this, position, rewriteHtmlText(text));
      }};
    }}
    if (document && document.write) {{
      var originalDocumentWrite = document.write;
      document.write = function () {{
        for (var i = 0; i < arguments.length; i += 1) {{
          arguments[i] = rewriteHtmlText(arguments[i]);
        }}
        return originalDocumentWrite.apply(this, arguments);
      }};
    }}
    if (document && document.writeln) {{
      var originalDocumentWriteln = document.writeln;
      document.writeln = function () {{
        for (var i = 0; i < arguments.length; i += 1) {{
          arguments[i] = rewriteHtmlText(arguments[i]);
        }}
        return originalDocumentWriteln.apply(this, arguments);
      }};
    }}
  }}
  function patchDynamicCss() {{
    if (window.CSSStyleDeclaration && window.CSSStyleDeclaration.prototype) {{
      patchUrlProperty(window.CSSStyleDeclaration.prototype, "cssText", rewriteStyleText);
      if (window.CSSStyleDeclaration.prototype.setProperty) {{
        var originalSetProperty = window.CSSStyleDeclaration.prototype.setProperty;
        window.CSSStyleDeclaration.prototype.setProperty = function (name, value, priority) {{
          return originalSetProperty.call(this, name, rewriteStyleText(value), priority);
        }};
      }}
    }}
    if (window.CSSStyleSheet && window.CSSStyleSheet.prototype && window.CSSStyleSheet.prototype.insertRule) {{
      var originalInsertRule = window.CSSStyleSheet.prototype.insertRule;
      window.CSSStyleSheet.prototype.insertRule = function (rule, index) {{
        if (arguments.length > 1) return originalInsertRule.call(this, rewriteStyleText(rule), index);
        return originalInsertRule.call(this, rewriteStyleText(rule));
      }};
    }}
  }}
  function observeAddedNodes() {{
    if (!window.MutationObserver || !document) return;
    var target = document.documentElement || document;
    if (!target) return;
    var observer = new MutationObserver(function (mutations) {{
      mutations.forEach(function (mutation) {{
        if (mutation.type === "attributes" && mutation.target) {{
          rewriteKnownUrlAttributes(mutation.target);
          return;
        }}
        Array.prototype.slice.call(mutation.addedNodes || []).forEach(function (node) {{
          if (!node) return;
          if (node.nodeType === 1 || node.nodeType === 9 || node.nodeType === 11) {{
            rewriteKnownUrlAttributes(node);
          }}
        }});
      }});
    }});
    observer.observe(target, {{
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: [
        "src", "href", "action", "formaction", "url", "data-url",
        "data-src", "data-href", "poster", "background", "srcset",
        "style", "srcdoc", "data", "http-equiv"
      ]
    }});
  }}
  function rewriteUrlOptions(options) {{
    if (typeof options === "string" || (options && typeof options.href === "string")) {{
      return rewriteRonghuiUrl(options);
    }}
    if (options && typeof options === "object") {{
      if (options.url) options.url = rewriteRonghuiUrl(options.url);
      if (options.href) options.href = rewriteRonghuiUrl(options.href);
      if (options.src) options.src = rewriteRonghuiUrl(options.src);
    }}
    return options;
  }}
  function rewriteAjaxOptions(options) {{
    return rewriteUrlOptions(options);
  }}
  function patchAjaxLibrary(library, label) {{
    if (!library || !library.ajax || library.ajax.__codexRonghuiPatched) return;
    var originalAjax = library.ajax;
    var patchedAjax = function () {{
      if (arguments.length > 0) arguments[0] = rewriteAjaxOptions(arguments[0]);
      return originalAjax.apply(this, arguments);
    }};
    patchedAjax.__codexRonghuiPatched = true;
    library.ajax = patchedAjax;
    if (label === "jQuery") window.__codexRonghuiJqueryAjaxPatched = true;
    if (label === "$") window.__codexRonghuiDollarAjaxPatched = true;
  }}
  function patchDeferredAjaxGlobal(name) {{
    try {{
      var descriptor = Object.getOwnPropertyDescriptor(window, name);
      if (descriptor && !descriptor.configurable) {{
        patchAjaxLibrary(window[name], name);
        return;
      }}
      var currentValue = window[name];
      Object.defineProperty(window, name, {{
        configurable: true,
        enumerable: descriptor ? descriptor.enumerable : true,
        get: function () {{
          return currentValue;
        }},
        set: function (value) {{
          currentValue = value;
          patchAjaxLibrary(currentValue, name);
        }}
      }});
      if (currentValue) patchAjaxLibrary(currentValue, name);
    }} catch (_) {{
      patchAjaxLibrary(window[name], name);
    }}
  }}
  function patchMiniLibrary(library) {{
    if (!library) return;
    if (library.open && !library.open.__codexRonghuiPatched) {{
      var originalOpen = library.open;
      var patchedOpen = function () {{
        if (arguments.length > 0) arguments[0] = rewriteUrlOptions(arguments[0]);
        return originalOpen.apply(this, arguments);
      }};
      patchedOpen.__codexRonghuiPatched = true;
      try {{
        library.open = patchedOpen;
        window.__codexRonghuiMiniOpenPatched = true;
      }} catch (_) {{}}
    }}
    if (library.ajax && !library.ajax.__codexRonghuiPatched) {{
      var originalAjax = library.ajax;
      var patchedAjax = function () {{
        if (arguments.length > 0) arguments[0] = rewriteAjaxOptions(arguments[0]);
        return originalAjax.apply(this, arguments);
      }};
      patchedAjax.__codexRonghuiPatched = true;
      try {{
        library.ajax = patchedAjax;
        window.__codexRonghuiMiniAjaxPatched = true;
      }} catch (_) {{}}
    }}
  }}
  function patchDeferredMiniGlobal(name) {{
    try {{
      var descriptor = Object.getOwnPropertyDescriptor(window, name);
      if (descriptor && !descriptor.configurable) {{
        patchMiniLibrary(window[name]);
        return;
      }}
      var currentValue = window[name];
      Object.defineProperty(window, name, {{
        configurable: true,
        enumerable: descriptor ? descriptor.enumerable : true,
        get: function () {{
          return currentValue;
        }},
        set: function (value) {{
          currentValue = value;
          patchMiniLibrary(currentValue);
        }}
      }});
      if (currentValue) patchMiniLibrary(currentValue);
    }} catch (_) {{
      patchMiniLibrary(window[name]);
    }}
  }}
  function patchUrlConstructor(name) {{
    var Original = window[name];
    if (!Original) return;
    var Patched = function (url, options) {{
      return new Original(rewriteRonghuiUrl(url), options);
    }};
    try {{
      Patched.prototype = Original.prototype;
      if (Object.setPrototypeOf) Object.setPrototypeOf(Patched, Original);
    }} catch (_) {{}}
    window[name] = Patched;
  }}
  function patchNavigatorBeacon() {{
    if (!window.navigator || !window.navigator.sendBeacon) return;
    var originalSendBeacon = window.navigator.sendBeacon;
    window.navigator.sendBeacon = function (url, data) {{
      return originalSendBeacon.call(this, rewriteRonghuiUrl(url), data);
    }};
  }}
  function loadDeferredRonghuiMapFrame() {{
    var frame = document.getElementById("mapContainer");
    if (!frame || !frame.getAttribute || !frame.setAttribute) return;
    var src = frame.getAttribute("data-codex-deferred-src") || "";
    if (!src || frame.__codexRonghuiMapLoaded) return;
    frame.__codexRonghuiMapLoaded = true;
    frame.setAttribute("src", src);
    try {{
      frame.removeAttribute("data-codex-deferred-src");
    }} catch (_) {{}}
  }}
  function patchDeferredRonghuiMapFrame() {{
    if (window.__codexRonghuiMapTogglePatched) return;
    var originalGetDispInfoByAddress = window.getDispInfoByAddress;
    if (typeof originalGetDispInfoByAddress !== "function") return;
    window.__codexRonghuiMapTogglePatched = true;
    window.getDispInfoByAddress = function () {{
      loadDeferredRonghuiMapFrame();
      return originalGetDispInfoByAddress.apply(this, arguments);
    }};
  }}
  window.loadCodexRonghuiMapFrame = loadDeferredRonghuiMapFrame;
  document.addEventListener("click", function (event) {{
    var target = event && event.target;
    if (!target || !target.closest) return;
    if (target.closest("#searchDesBtn,[data-codex-load-map]")) loadDeferredRonghuiMapFrame();
  }}, true);
  var mapPatchAttempts = 0;
  var mapPatchTimer = window.setInterval(function () {{
    patchDeferredRonghuiMapFrame();
    mapPatchAttempts += 1;
    if (window.__codexRonghuiMapTogglePatched || mapPatchAttempts > 50) {{
      window.clearInterval(mapPatchTimer);
    }}
  }}, 200);
  ensureRonghuiUserInfoCookie();
  patchUrlAttributes();
  patchHtmlFragmentSinks();
  patchDynamicCss();
  observeAddedNodes();
  patchNavigatorBeacon();
  patchUrlConstructor("EventSource");
  patchUrlConstructor("Worker");
  patchUrlConstructor("SharedWorker");
  if (window.jQuery && window.jQuery.ajax) patchAjaxLibrary(window.jQuery, "jQuery");
  if (window.$ && window.$.ajax) patchAjaxLibrary(window.$, "$");
  patchDeferredAjaxGlobal("jQuery");
  patchDeferredAjaxGlobal("$");
  if (window.mini && (window.mini.open || window.mini.ajax)) patchMiniLibrary(window.mini);
  patchDeferredMiniGlobal("mini");
  patchUrlProperty(window.HTMLMetaElement && window.HTMLMetaElement.prototype, "content", function (value) {{
    return isMetaRefresh(this) ? rewriteMetaRefreshContent(value) : value;
  }});
  patchUrlProperty(window.HTMLIFrameElement && window.HTMLIFrameElement.prototype, "src");
  patchUrlProperty(window.HTMLIFrameElement && window.HTMLIFrameElement.prototype, "srcdoc", rewriteHtmlText);
  patchUrlProperty(window.HTMLScriptElement && window.HTMLScriptElement.prototype, "src");
  patchUrlProperty(window.HTMLImageElement && window.HTMLImageElement.prototype, "src");
  patchUrlProperty(window.HTMLImageElement && window.HTMLImageElement.prototype, "srcset", rewriteSrcset);
  patchUrlProperty(window.HTMLVideoElement && window.HTMLVideoElement.prototype, "src");
  patchUrlProperty(window.HTMLVideoElement && window.HTMLVideoElement.prototype, "poster");
  patchUrlProperty(window.HTMLAudioElement && window.HTMLAudioElement.prototype, "src");
  patchUrlProperty(window.HTMLSourceElement && window.HTMLSourceElement.prototype, "src");
  patchUrlProperty(window.HTMLSourceElement && window.HTMLSourceElement.prototype, "srcset", rewriteSrcset);
  patchUrlProperty(window.HTMLTrackElement && window.HTMLTrackElement.prototype, "src");
  patchUrlProperty(window.HTMLEmbedElement && window.HTMLEmbedElement.prototype, "src");
  patchUrlProperty(window.HTMLObjectElement && window.HTMLObjectElement.prototype, "data");
  patchUrlProperty(window.HTMLBaseElement && window.HTMLBaseElement.prototype, "href", rewriteRonghuiBaseUrl);
  patchUrlProperty(window.HTMLLinkElement && window.HTMLLinkElement.prototype, "href");
  patchUrlProperty(window.HTMLAnchorElement && window.HTMLAnchorElement.prototype, "href");
  patchUrlProperty(window.HTMLAreaElement && window.HTMLAreaElement.prototype, "href");
  patchUrlProperty(window.HTMLFormElement && window.HTMLFormElement.prototype, "action");
  patchUrlProperty(window.HTMLButtonElement && window.HTMLButtonElement.prototype, "formAction");
  patchUrlProperty(window.HTMLInputElement && window.HTMLInputElement.prototype, "formAction");
  patchUrlProperty(window.HTMLInputElement && window.HTMLInputElement.prototype, "src");
  patchFormSubmit();
  patchHistoryMethod("pushState");
  patchHistoryMethod("replaceState");
  if (window.open) {{
    var originalWindowOpen = window.open;
    window.open = function () {{
      if (arguments.length > 0) arguments[0] = rewriteRonghuiUrl(arguments[0]);
      return originalWindowOpen.apply(this, arguments);
    }};
  }}
  if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {{
    var originalOpen = window.XMLHttpRequest.prototype.open;
    window.XMLHttpRequest.prototype.open = function () {{
      if (arguments.length > 1) arguments[1] = rewriteRonghuiUrl(arguments[1]);
      return originalOpen.apply(this, arguments);
    }};
  }}
  if (window.fetch) {{
    var originalFetch = window.fetch;
    window.fetch = function (input, init) {{
      if (typeof input === "string" || (input && typeof input.href === "string")) {{
        input = rewriteRonghuiUrl(input);
      }} else if (input && input.url && window.Request) {{
        var rewritten = rewriteRonghuiUrl(input.url);
        if (rewritten !== input.url) input = new Request(rewritten, input);
      }}
      return originalFetch.call(this, input, init);
    }};
  }}
  document.addEventListener("submit", function (event) {{
    var form = event.target;
    rewriteFormAction(form);
    rewriteSubmitterAction(event && event.submitter);
  }}, true);
}})();
</script>
"""


def _inject_runtime_proxy_helper(html: str, *, proxy_prefix: str, user_info_cookie: str = "") -> str:
    helpers: list[str] = []
    if "codex-ronghui-prefill-script" not in html:
        helpers.append(RONGHUI_PREFILL_HELPER)
    if "codex-ronghui-proxy-script" not in html:
        helpers.append(_runtime_proxy_helper(proxy_prefix=proxy_prefix, user_info_cookie=user_info_cookie))
    if not helpers:
        return html
    helper = "".join(helpers)
    head_match = re.search(r"<head(?:\s[^>]*)?>", html, flags=re.IGNORECASE)
    if head_match:
        insert_at = head_match.end()
        return f"{html[:insert_at]}{helper}{html[insert_at:]}"
    return f"{helper}{html}"


def _should_rewrite_text_response(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(
        marker in lowered
        for marker in (
            "text/html",
            "application/xhtml+xml",
            "text/css",
            "text/javascript",
            "text/plain",
            "application/javascript",
            "application/x-javascript",
            "application/json",
            "text/json",
            "application/xml",
            "text/xml",
            "image/svg+xml",
        )
    )


def _should_inject_runtime_helper(content_type: str) -> bool:
    lowered = content_type.lower()
    return "text/html" in lowered or "application/xhtml+xml" in lowered


def _is_javascript_content_type(content_type: str) -> bool:
    lowered = content_type.lower()
    return any(
        marker in lowered
        for marker in (
            "text/javascript",
            "application/javascript",
            "application/x-javascript",
        )
    )


def _response_content(response: Any) -> bytes:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    return str(getattr(response, "text", "") or "").encode("utf-8")


def _auth_if_login_response(response: Any, text: str) -> None:
    url = _clean_text(getattr(response, "url", ""))
    lowered = text.lower()
    header_text = ""
    headers = getattr(response, "headers", {})
    iterator = headers.items() if isinstance(headers, dict) else getattr(headers, "items", lambda: [])()
    for key, value in iterator:
        if _clean_text(key).lower() in {"location", "refresh"}:
            header_text = f"{header_text}\n{_clean_text(value)}"
    lowered_headers = header_text.lower()
    if (
        "/system/login" in url
        or "system/login" in lowered_headers
        or "id=\"loinform\"" in lowered
        or "validatecode" in lowered
        and "supplier" in lowered
    ):
        raise TMSAuthStateError("AUTH_REQUIRED", "Ronghui login is required.")


def run_once(params: dict[str, Any]) -> dict[str, Any]:
    params = params or {}
    method = _clean_text(params.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return {"ok": False, "error_code": "INVALID_PROXY_METHOD", "error": f"Unsupported method: {method}"}

    raw_path = _clean_text(params.get("path"))
    session = None
    if not raw_path or raw_path == "/":
        session = get_session_broker(RONGHUI_WAYBILL_SESSION_PROFILE).build_requests_session(validate=True)
    try:
        path, query, remote_url = _target_from_params(
            session,
            params.get("path"),
            params.get("query"),
            entry_menu_id=params.get("entry_menu_id"),
            entry_menu_text=params.get("entry_menu_text"),
        )
    except ValueError as exc:
        return {"ok": False, "error_code": "INVALID_PROXY_PATH", "error": str(exc)}
    if session is None:
        session = get_session_broker(RONGHUI_WAYBILL_SESSION_PROFILE).build_requests_session(validate=True)
    user_info_cookie = _client_user_info_cookie_from_session(session)
    cache_key = _cacheable_lookup_key(method, path, query)
    cached_payload = _get_lookup_cache_entry(cache_key)
    if cached_payload is not None:
        cached_payload["remote_url"] = remote_url
        cached_payload["remote_path"] = path
        cached_payload["remote_query"] = query
        cached_headers = cached_payload.get("headers")
        if isinstance(cached_headers, dict):
            cached_headers["X-Codex-Proxy-Cache"] = "hit"
        return cached_payload

    content_type = _clean_text(params.get("content_type"))
    headers = _filter_request_headers(params.get("headers"), content_type=content_type)
    body = _decode_body(params)
    response = _request_ronghui_proxy(
        session,
        method,
        remote_url,
        path=path,
        headers=headers,
        body=body,
        timeout_sec=int(params.get("timeout_sec") or DEFAULT_TIMEOUT_SEC),
    )
    raw_content = _response_content(response)
    _auth_if_login_response(response, raw_content.decode("utf-8", errors="replace"))

    proxy_prefix = _clean_text(params.get("proxy_prefix")) or "/ocr/ronghui/live"
    current_response_url = str(getattr(response, "url", "") or remote_url)
    response_headers = _filter_response_headers(
        getattr(response, "headers", {}),
        current_url=current_response_url,
        proxy_prefix=proxy_prefix,
    )
    if cache_key:
        response_headers["Cache-Control"] = f"private, max-age={RONGHUI_PROXY_LOOKUP_CACHE_TTL_SEC}"
        response_headers.pop("Pragma", None)
        response_headers.pop("pragma", None)
    response_content_type = response_headers.get("content-type") or response_headers.get("Content-Type") or ""
    if _should_rewrite_text_response(response_content_type):
        charset = _charset_from_content_type(response_content_type)
        text = raw_content.decode(charset, errors="replace")
        if _is_javascript_content_type(response_content_type):
            rewritten = text
        else:
            rewritten = _rewrite_urls(
                text,
                current_url=current_response_url,
                proxy_prefix=proxy_prefix,
            )
        if _should_inject_runtime_helper(response_content_type):
            rewritten = _inject_runtime_proxy_helper(
                rewritten,
                proxy_prefix=proxy_prefix,
                user_info_cookie=user_info_cookie,
            )
        raw_content = rewritten.encode(charset, errors="replace")

    result = {
        "ok": True,
        "status_code": int(getattr(response, "status_code", 200) or 200),
        "headers": response_headers,
        "body_base64": base64.b64encode(raw_content).decode("ascii"),
        "remote_url": remote_url,
        "remote_path": path,
        "remote_query": query,
    }
    if cache_key and result["status_code"] == 200:
        _store_lookup_cache_entry(cache_key, result)
    return result
