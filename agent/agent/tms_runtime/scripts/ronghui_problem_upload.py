"""Shared Ronghui problem-item upload and authoritative readback primitives.

The write contract comes from the real MiniUI ``问题件录入`` page.  Successful
completion additionally requires an exact, unique row from the independent
``登记问题件查询`` list.  A save acknowledgement is never treated as business
success on its own.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse


BASE_URL = "https://tms.ronghuiwl.com"
MENU_URL = f"{BASE_URL}/menuTreeExtend/loadMenu"
INDEX_URL = f"{BASE_URL}/module/index?mv=index"
DATA_QUERY_URL = f"{BASE_URL}/dataQuery/findAllByCallId"
PAGE_QUERY_URL = f"{BASE_URL}/dataQuery/findPageByCallId"
SAVE_TABLES_URL = f"{BASE_URL}/dataOperation/saveTables"

PROBLEM_ENTRY_MENU_TEXT = "问题件录入"
PROBLEM_REGISTER_QUERY_MENU_TEXT = "登记问题件查询"
PROBLEM_REGISTER_QUERY_CALL_ID = "FIND_PROBLEM_REGISTER_LIST"
AUTHORITATIVE_PAGE_SIZE = 100
AUTHORITATIVE_MAX_ROWS = 1000

_CALL_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PAGE_KEYS = ("pageIndex", "pageSize", "sortField", "sortOrder", "totalColumns")
_PROBLEM_REQUIRED_NONEMPTY_FIELDS = (
    "GUID",
    "BILL_CODE",
    "TYPE",
    "OWNER_PROBELM_TYPE",
    "PROBLEM_CAUSE",
    "SEND_SITE_CODE",
    "SEND_SITE",
    "REGISTER_SITE_CODE",
    "REGISTER_SITE",
    "REGISTER_MAN_CODE",
    "REGISTER_MAN",
    "REGISTER_SAVE_DATE",
)
_PROBLEM_EXPECTED_MATCH_FIELDS = (
    "GUID",
    "BILL_CODE",
    "TYPE",
    "OWNER_PROBELM_TYPE",
    "PROBLEM_CAUSE",
    "SEND_SITE_CODE",
    "SEND_SITE",
    "REGISTER_SITE_CODE",
    "REGISTER_SITE",
    "REGISTER_MAN_CODE",
    "REGISTER_MAN",
)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/widget/home",
    "X-Requested-With": "XMLHttpRequest",
}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _json_datetime(value: dt.datetime | None = None) -> str:
    value = value or dt.datetime.now(dt.timezone.utc).astimezone()
    return value.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _extract_input_value(html: str, input_id: str) -> str:
    import re

    pattern = rf"id=[\"']{re.escape(input_id)}[\"'][^>]*value=[\"']([^\"']*)[\"']"
    match = re.search(pattern, html)
    return match.group(1) if match else ""


def _js_unescape(value: str) -> str:
    import re

    def _replace_unicode(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"%u([0-9A-Fa-f]{4})", _replace_unicode, value or "")
    return unquote(text)


def _read_user_info_cookie(session: Any) -> dict[str, Any]:
    raw = session.cookies.get("userInfo")
    if not raw:
        return {}
    try:
        payload = json.loads(_js_unescape(raw))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def fetch_login_context(session: Any) -> dict[str, str]:
    response = session.get(INDEX_URL, timeout=20)
    response.raise_for_status()
    html = response.text
    user_info = _read_user_info_cookie(session)
    return {
        "site_code": _extract_input_value(html, "loginSiteCode") or _clean_text(user_info.get("loginSiteCode")),
        "site_name": _extract_input_value(html, "loginSiteName") or _clean_text(user_info.get("loginSiteName")),
        "emp_code": _clean_text(user_info.get("loginEmpCode")),
        "emp_name": _clean_text(user_info.get("loginEmpName")),
        "user_id": _clean_text(user_info.get("loginUserId") or user_info.get("loginEmpCode")),
        "user_name": _clean_text(user_info.get("loginUserName") or user_info.get("loginEmpName")),
        "dept_name": _clean_text(user_info.get("loginEmpDeptName")),
    }


def _walk_menu(nodes: list[dict[str, Any]], path: str = ""):
    for node in nodes or []:
        text = _clean_text(node.get("text") or node.get("name"))
        new_path = f"{path}/{text}" if path and text else text or path
        yield node, new_path
        children = node.get("children") or []
        if isinstance(children, list):
            yield from _walk_menu(children, new_path)


def _extract_between(text: str, start_token: str, end_token: str) -> str:
    start = text.find(start_token)
    if start == -1:
        return ""
    start += len(start_token)
    end = text.find(end_token, start)
    return "" if end == -1 else text[start:end]


def page_context_from_url(url: str) -> dict[str, str]:
    """Build the request-header context encoded by a real Ronghui page URL."""

    full_url = urljoin(BASE_URL, _clean_text(url))
    parsed = urlparse(full_url)
    query = parse_qs(parsed.query)
    return {
        "url": full_url,
        "authentication_key": _clean_text((query.get("authenticationKey") or [""])[0]),
        "page_id": _clean_text((query.get("pageId") or [""])[0]),
    }


def _resolve_menu_page_context(
    session: Any,
    *,
    menu_text: str,
    content_markers: tuple[str, ...],
) -> dict[str, str]:
    response = session.get(MENU_URL, timeout=20)
    response.raise_for_status()
    payload = response.json()
    nodes = payload.get("result", {}).get("data") if isinstance(payload, dict) else []
    candidates: list[str] = []
    for node, path in _walk_menu(nodes if isinstance(nodes, list) else []):
        text = _clean_text(node.get("text") or node.get("name"))
        url = _clean_text(node.get("url"))
        if not url or "/widget/home" not in url:
            continue
        if text == menu_text or path.endswith(f"/{menu_text}"):
            candidates.insert(0, url)
        elif menu_text in path:
            candidates.append(url)

    resolved: list[dict[str, str]] = []
    for url in dict.fromkeys(candidates):
        full_url = urljoin(BASE_URL, url)
        parsed = urlparse(full_url)
        query = parse_qs(parsed.query)
        auth_key = (query.get("authenticationKey") or [""])[0]
        page_id = (query.get("pageId") or [""])[0]
        page_response = session.get(full_url, timeout=20)
        page_response.raise_for_status()
        html = page_response.text
        auth_key = auth_key or _extract_between(html, 'authenticationKey:"', '"')
        page_id = page_id or _extract_between(html, 'pageId:"', '"')
        if any(marker in html for marker in content_markers):
            resolved.append(
                {
                    "url": full_url,
                    "authentication_key": auth_key,
                    "page_id": page_id,
                }
            )
    if not resolved:
        raise RuntimeError(f"无法定位 TMS {menu_text}页面")
    unique = {
        (item["url"], item["authentication_key"], item["page_id"]): item
        for item in resolved
    }
    if len(unique) != 1:
        raise RuntimeError(f"TMS {menu_text}页面存在 {len(unique)} 个候选，拒绝隐式选择")
    return next(iter(unique.values()))


def resolve_problem_page_context(session: Any) -> dict[str, str]:
    return _resolve_menu_page_context(
        session,
        menu_text=PROBLEM_ENTRY_MENU_TEXT,
        content_markers=("TAB_PROBLEM_ADD", PROBLEM_ENTRY_MENU_TEXT),
    )


def resolve_registered_problem_query_context(session: Any) -> dict[str, str]:
    """Resolve the independent registered-problem list used for verification."""

    return _resolve_menu_page_context(
        session,
        menu_text=PROBLEM_REGISTER_QUERY_MENU_TEXT,
        content_markers=(PROBLEM_REGISTER_QUERY_CALL_ID, PROBLEM_REGISTER_QUERY_MENU_TEXT),
    )


def _headers(page_context: dict[str, str], *, content_type: str | None = None) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers["Referer"] = page_context.get("url") or f"{BASE_URL}/widget/home"
    if page_context.get("authentication_key"):
        headers["authenticationKey"] = page_context["authentication_key"]
    if page_context.get("page_id"):
        headers["pageId"] = page_context["page_id"]
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def query_rows(
    session: Any,
    call_id: str,
    data: dict[str, Any] | None = None,
    *,
    page_context: dict[str, str],
) -> list[dict[str, Any]]:
    response = session.post(
        f"{DATA_QUERY_URL}?id={call_id}",
        data=data or {},
        headers=_headers(page_context, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    return []


def _page_rows_and_total(payload: Any, *, call_id: str) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"TMS {call_id} 分页响应不是对象")
    if payload.get("success") is False or str(payload.get("success")).strip().lower() == "false":
        message = _clean_text(payload.get("message") or payload.get("msg"))
        raise RuntimeError(f"TMS {call_id} 查询失败: {message or 'source rejected'}")

    containers = [payload]
    nested = payload.get("data")
    if isinstance(nested, dict):
        containers.append(nested)

    row_candidates: list[list[dict[str, Any]]] = []
    if isinstance(nested, list):
        row_candidates.append([row for row in nested if isinstance(row, dict)])
        if len(row_candidates[-1]) != len(nested):
            raise RuntimeError(f"TMS {call_id} 分页响应包含非对象行")
    for container in containers:
        for key in ("rows", "list", "records", "items"):
            candidate = container.get(key)
            if not isinstance(candidate, list):
                continue
            rows = [row for row in candidate if isinstance(row, dict)]
            if len(rows) != len(candidate):
                raise RuntimeError(f"TMS {call_id} 分页响应包含非对象行")
            if rows not in row_candidates:
                row_candidates.append(rows)
    if len(row_candidates) != 1:
        raise RuntimeError(f"TMS {call_id} 分页响应行集合缺失或存在歧义")

    totals: list[int] = []
    for container in containers:
        for key in ("total", "count"):
            raw = container.get(key)
            if raw in (None, ""):
                continue
            if isinstance(raw, bool) or not re.fullmatch(r"\d+", str(raw).strip()):
                raise RuntimeError(f"TMS {call_id} 分页总数无效")
            parsed = int(str(raw).strip())
            if parsed not in totals:
                totals.append(parsed)
    if len(totals) != 1:
        raise RuntimeError(f"TMS {call_id} 分页总数缺失或冲突")
    return row_candidates[0], totals[0]


def query_page_rows(
    session: Any,
    *,
    call_id: str,
    data: dict[str, Any],
    page_context: dict[str, str],
    page_size: int = AUTHORITATIVE_PAGE_SIZE,
    max_rows: int = AUTHORITATIVE_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Read every row from one exact Ronghui ``findPageByCallId`` query.

    The source must declare one consistent total.  Missing totals, malformed
    rows, early-empty pages and totals above the explicit safety bound fail
    closed instead of being interpreted as a complete result.
    """

    if not _CALL_ID_RE.fullmatch(_clean_text(call_id)):
        raise ValueError("call_id 不是受支持的融辉分页查询标识")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ValueError("page_size 必须是 1 到 100 的整数")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < page_size:
        raise ValueError("max_rows 必须是不小于 page_size 的整数")
    if not isinstance(data, dict):
        raise ValueError("分页查询参数必须是对象")
    reserved = sorted(set(data).intersection(_PAGE_KEYS))
    if reserved:
        raise ValueError(f"分页查询参数不能覆盖技术字段: {', '.join(reserved)}")

    rows: list[dict[str, Any]] = []
    declared_total: int | None = None
    page_index = 0
    while True:
        request_data = {
            **data,
            "pageIndex": page_index,
            "pageSize": page_size,
            "sortField": "",
            "sortOrder": "",
            "totalColumns": "[]",
        }
        response = session.post(
            f"{PAGE_QUERY_URL}?id={call_id}",
            data=request_data,
            headers=_headers(page_context, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(f"TMS {call_id} 分页响应不是 JSON") from exc
        page_rows, page_total = _page_rows_and_total(payload, call_id=call_id)
        if page_total > max_rows:
            raise RuntimeError(f"TMS {call_id} 分页总数超过安全上限 {max_rows}")
        if declared_total is None:
            declared_total = page_total
        elif declared_total != page_total:
            raise RuntimeError(f"TMS {call_id} 分页总数发生变化")
        if len(page_rows) > page_size:
            raise RuntimeError(f"TMS {call_id} 单页行数超过请求上限")
        rows.extend(page_rows)
        if len(rows) == declared_total:
            return rows
        if len(rows) > declared_total:
            raise RuntimeError(f"TMS {call_id} 分页行数超过声明总数")
        if not page_rows:
            raise RuntimeError(f"TMS {call_id} 在读取完整结果前返回空页")
        page_index += 1


def query_registered_problem_items(
    session: Any,
    *,
    bill_code: str,
    login_context: dict[str, str],
    page_context: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Query the real registered-problem list for one exact bill code."""

    bill_code = _clean_text(bill_code)
    login_site_code = _clean_text(login_context.get("site_code"))
    if not bill_code:
        raise ValueError("登记问题件查询缺少运单号")
    if not login_site_code:
        raise RuntimeError("登记问题件查询缺少登录网点编号")
    query_context = page_context or resolve_registered_problem_query_context(session)
    return query_page_rows(
        session,
        call_id=PROBLEM_REGISTER_QUERY_CALL_ID,
        data={"BILL_CODE": bill_code, "LOGIN_SITE_CODE": login_site_code},
        page_context=query_context,
    )


def match_unique_registered_problem_item(
    rows: list[dict[str, Any]],
    *,
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Require one exact authoritative problem-item row and return safe proof."""

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("问题件读后结果不是对象列表")
    expected_values = {field: _clean_text(expected.get(field)) for field in _PROBLEM_EXPECTED_MATCH_FIELDS}
    missing_expected = [field for field, value in expected_values.items() if not value]
    if missing_expected:
        raise RuntimeError(f"问题件写入预期缺少关键字段: {', '.join(missing_expected)}")

    bill_rows = [row for row in rows if _clean_text(row.get("BILL_CODE")) == expected_values["BILL_CODE"]]
    if not bill_rows:
        raise RuntimeError("问题件权威列表未找到目标运单")
    for row in bill_rows:
        missing = [field for field in _PROBLEM_REQUIRED_NONEMPTY_FIELDS if not _clean_text(row.get(field))]
        if missing:
            raise RuntimeError(f"问题件权威列表缺少关键字段: {', '.join(missing)}")

    matches = [
        row
        for row in bill_rows
        if all(_clean_text(row.get(field)) == value for field, value in expected_values.items())
    ]
    if not matches:
        raise RuntimeError("问题件权威列表未找到与写入计划完全一致的记录")
    if len(matches) != 1:
        raise RuntimeError(f"问题件权威列表存在 {len(matches)} 条完全一致记录，拒绝隐式选择")
    row = matches[0]
    return {
        "source": PROBLEM_REGISTER_QUERY_CALL_ID,
        "external_id": _clean_text(row.get("GUID")),
        "bill_code": expected_values["BILL_CODE"],
        "matched_fields": list(_PROBLEM_EXPECTED_MATCH_FIELDS),
        "registered_at": _clean_text(row.get("REGISTER_SAVE_DATE")),
    }


def match_unique_registered_problem_fingerprint(
    rows: list[dict[str, Any]],
    *,
    bill_code: str,
    external_id: str,
    problem_type: str,
    problem_owner_type: str,
    problem_cause_sha256: str,
) -> dict[str, Any]:
    """Require one complete row matching an external ID and signed field hash."""

    expected = {
        "bill_code": _clean_text(bill_code),
        "external_id": _clean_text(external_id),
        "problem_type": _clean_text(problem_type),
        "problem_owner_type": _clean_text(problem_owner_type),
        "problem_cause_sha256": _clean_text(problem_cause_sha256),
    }
    if (
        any(not value for value in expected.values())
        or not re.fullmatch(r"[0-9a-f]{64}", expected["problem_cause_sha256"])
    ):
        raise RuntimeError("问题件指纹核验参数不完整")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("问题件读后结果不是对象列表")
    bill_rows = [
        row
        for row in rows
        if _clean_text(row.get("BILL_CODE")) == expected["bill_code"]
    ]
    for row in bill_rows:
        missing = [
            field
            for field in _PROBLEM_REQUIRED_NONEMPTY_FIELDS
            if not _clean_text(row.get(field))
        ]
        if missing:
            raise RuntimeError(f"问题件权威列表缺少关键字段: {', '.join(missing)}")
    matches = [
        row
        for row in bill_rows
        if _clean_text(row.get("GUID")) == expected["external_id"]
        and _clean_text(row.get("TYPE")) == expected["problem_type"]
        and _clean_text(row.get("OWNER_PROBELM_TYPE"))
        == expected["problem_owner_type"]
        and hashlib.sha256(
            _clean_text(row.get("PROBLEM_CAUSE")).encode("utf-8")
        ).hexdigest()
        == expected["problem_cause_sha256"]
    ]
    if not matches:
        raise RuntimeError("问题件权威列表未找到与签名字段指纹一致的记录")
    if len(matches) != 1:
        raise RuntimeError(f"问题件权威列表存在 {len(matches)} 条指纹一致记录，拒绝隐式选择")
    row = matches[0]
    return {
        "source": PROBLEM_REGISTER_QUERY_CALL_ID,
        "external_id": expected["external_id"],
        "bill_code": expected["bill_code"],
        "problem_type": expected["problem_type"],
        "problem_owner_type": expected["problem_owner_type"],
        "problem_cause_sha256": expected["problem_cause_sha256"],
        "registered_at": _clean_text(row.get("REGISTER_SAVE_DATE")),
        "registered_site": _clean_text(row.get("REGISTER_SITE")),
    }


def find_unique_registered_problem_fingerprint(
    rows: list[dict[str, Any]],
    *,
    bill_code: str,
    problem_type: str,
    problem_owner_type: str,
    problem_cause_sha256: str,
) -> dict[str, Any] | None:
    """Return the only complete row matching the signed plan, or ``None``."""

    bill = _clean_text(bill_code)
    problem = _clean_text(problem_type)
    owner = _clean_text(problem_owner_type)
    cause_hash = _clean_text(problem_cause_sha256)
    if (
        not bill
        or not problem
        or not owner
        or not re.fullmatch(r"[0-9a-f]{64}", cause_hash)
    ):
        raise RuntimeError("问题件指纹查询参数不完整")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("问题件读后结果不是对象列表")
    bill_rows = [row for row in rows if _clean_text(row.get("BILL_CODE")) == bill]
    for row in bill_rows:
        missing = [
            field
            for field in _PROBLEM_REQUIRED_NONEMPTY_FIELDS
            if not _clean_text(row.get(field))
        ]
        if missing:
            raise RuntimeError(f"问题件权威列表缺少关键字段: {', '.join(missing)}")
    matches = [
        row
        for row in bill_rows
        if _clean_text(row.get("TYPE")) == problem
        and _clean_text(row.get("OWNER_PROBELM_TYPE")) == owner
        and hashlib.sha256(
            _clean_text(row.get("PROBLEM_CAUSE")).encode("utf-8")
        ).hexdigest()
        == cause_hash
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError(f"问题件权威列表存在 {len(matches)} 条指纹一致记录，拒绝隐式选择")
    row = matches[0]
    return match_unique_registered_problem_fingerprint(
        rows,
        bill_code=bill,
        external_id=_clean_text(row.get("GUID")),
        problem_type=problem,
        problem_owner_type=owner,
        problem_cause_sha256=cause_hash,
    )


def verify_registered_problem_item(
    session: Any,
    *,
    expected: dict[str, Any],
    login_context: dict[str, str],
    page_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    rows = query_registered_problem_items(
        session,
        bill_code=_clean_text(expected.get("BILL_CODE")),
        login_context=login_context,
        page_context=page_context,
    )
    return match_unique_registered_problem_item(rows, expected=expected)


def verify_registered_problem_fingerprint(
    session: Any,
    *,
    bill_code: str,
    external_id: str,
    problem_type: str,
    problem_owner_type: str,
    problem_cause_sha256: str,
    login_context: dict[str, str],
    page_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    rows = query_registered_problem_items(
        session,
        bill_code=bill_code,
        login_context=login_context,
        page_context=page_context,
    )
    return match_unique_registered_problem_fingerprint(
        rows,
        bill_code=bill_code,
        external_id=external_id,
        problem_type=problem_type,
        problem_owner_type=problem_owner_type,
        problem_cause_sha256=problem_cause_sha256,
    )


def fetch_guid(session: Any, page_context: dict[str, str]) -> str:
    rows = query_rows(session, "FIND_GUID", page_context=page_context)
    guid = _clean_text(rows[0].get("GUID")) if rows else ""
    if not guid:
        raise RuntimeError("TMS FIND_GUID 未返回 GUID")
    return guid


def fetch_bill_info(session: Any, bill_code: str, page_context: dict[str, str]) -> dict[str, Any]:
    call_id = "FIND_BILL_BY_BILL_CODE_RH" if bill_code.startswith("H") else "FIND_BILL_BY_BILL_CODE_NEW"
    rows = query_rows(session, call_id, {"BILL_CODE": bill_code}, page_context=page_context)
    if not rows:
        raise RuntimeError("TMS 未查到运单")
    return rows[0]


def resolve_notice_site(bill_info: dict[str, Any], login_context: dict[str, str]) -> tuple[str, str]:
    login_site_code = _clean_text(login_context.get("site_code"))
    send_site_code = _clean_text(bill_info.get("SEND_SITE_CODE"))
    register_site_code = _clean_text(bill_info.get("REGISTER_SITE_CODE") or bill_info.get("SEND_SITE_CODE"))
    register_site = _clean_text(bill_info.get("REGISTER_SITE") or bill_info.get("SEND_SITE"))
    if login_site_code in {send_site_code, register_site_code}:
        return _clean_text(bill_info.get("DISPATCH_SITE_CODE")), _clean_text(bill_info.get("DISPATCH_SITE"))
    return register_site_code, register_site


def operation(operation_key: str, data: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    rows = data if isinstance(data, list) else [data]
    return {
        "beforeAction": None,
        "operationKey": operation_key,
        "afterAction": None,
        "idFields": [],
        "data": rows,
    }


def save_tables(session: Any, operations: list[dict[str, Any]], page_context: dict[str, str]) -> dict[str, Any]:
    params_json = json.dumps(operations, ensure_ascii=False, separators=(",", ":"))
    headers = _headers(page_context)
    headers.pop("Content-Type", None)
    original_content_type = session.headers.pop("Content-Type", None)
    try:
        response = session.post(
            SAVE_TABLES_URL,
            files={"params": (None, params_json)},
            headers=headers,
            timeout=60,
        )
    finally:
        if original_content_type is not None:
            session.headers["Content-Type"] = original_content_type
    response.raise_for_status()
    if not response.content:
        return {"success": True, "message": ""}
    try:
        return response.json()
    except Exception:
        return {"success": False, "message": response.text[:500]}


def build_problem_row(
    *,
    bill_code: str,
    guid: str,
    notice_site_code: str,
    notice_site: str,
    login_context: dict[str, str],
    problem_cause: str,
    problem_type: str,
    problem_owner_type: str,
) -> dict[str, Any]:
    return {
        "localFilePath": "",
        "TRANSFER_CODE": "",
        "BILL_STATUS": "",
        "EMPLOYEE_CODE": "",
        "OPERATION_EMPLOYEE": "",
        "IS_SEND_SITE_REGISTER": 0,
        "FILE1": "",
        "FILE2": "",
        "FILE3": "",
        "FILE4": "",
        "GUID": guid,
        "BILL_CODE": bill_code,
        "TYPE": problem_type,
        "OWNER_PROBELM_TYPE": problem_owner_type,
        "SEND_SITE_CODE": notice_site_code,
        "SEND_SITE": notice_site,
        "PROBLEM_CAUSE": problem_cause,
        "FILE_PATH": None,
        "FILE_PATH2": "",
        "FILE_PATH3": "",
        "FILE_PATH4": "",
        "REGISTER_SITE": login_context.get("site_name", ""),
        "REGISTER_SITE_CODE": login_context.get("site_code", ""),
        "REGISTER_MAN_DEPT": login_context.get("dept_name", ""),
        "REGISTER_DATE": _json_datetime(),
        "REGISTER_SAVE_DATE": _json_datetime(),
        "REGISTER_MAN": login_context.get("emp_name", ""),
        "REGISTER_MAN_CODE": login_context.get("emp_code", ""),
        "DATA_FROM": "K13",
        "VERIFY_MSG": "",
    }


def update_postpone_days(session: Any, bill_code: str, page_context: dict[str, str]) -> bool:
    rows = query_rows(
        session,
        f"FIND_BILL_POSTPONE_DAYS&BILL_CODE={quote(bill_code, safe='')}",
        page_context=page_context,
    )
    if not rows:
        return False
    payload = {"BILL_CODE": bill_code, "POSTPONE_DAYS": rows[0].get("POSTPONE_DAYS")}
    result = save_tables(session, [operation("TAB_BILL_UPT", payload)], page_context)
    return bool(result.get("success", True))


def upload_problem_item(
    session: Any,
    *,
    record: dict[str, Any],
    page_context: dict[str, str],
    login_context: dict[str, str],
    query_page_context: dict[str, str] | None = None,
    update_postpone: bool = False,
    helpers: dict[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Upload one problem item using the real Ronghui page contract."""

    helpers = helpers or {}
    fetch_bill = helpers.get("fetch_bill_info", fetch_bill_info)
    resolve_site = helpers.get("resolve_notice_site", resolve_notice_site)
    fetch_new_guid = helpers.get("fetch_guid", fetch_guid)
    build_row = helpers.get("build_problem_row", build_problem_row)
    make_operation = helpers.get("operation", operation)
    save = helpers.get("save_tables", save_tables)
    postpone = helpers.get("update_postpone_days", update_postpone_days)
    verify = helpers.get("verify_registered_problem_item", verify_registered_problem_item)

    bill_code = _clean_text(record.get("bill_code"))
    problem_type = _clean_text(record.get("problem_type"))
    problem_owner_type = _clean_text(record.get("problem_owner_type"))
    problem_cause = _clean_text(record.get("problem_cause"))
    if not bill_code or not problem_type or not problem_owner_type or not problem_cause:
        raise RuntimeError("问题件缺少运单号、问题类型、问题归属或问题原因")
    missing_login = [
        field
        for field in ("site_code", "site_name", "emp_code", "emp_name")
        if not _clean_text(login_context.get(field))
    ]
    if missing_login:
        raise RuntimeError(f"问题件登记缺少登录身份字段: {', '.join(missing_login)}")

    bill_info = fetch_bill(session, bill_code, page_context)
    notice_site_code, notice_site = resolve_site(bill_info, login_context)
    if not notice_site_code or not notice_site:
        raise RuntimeError("通知网点自动匹配为空")
    if notice_site_code == _clean_text(login_context.get("site_code")):
        raise RuntimeError("通知网点和登记网点一致，页面规则禁止上传")

    guid = fetch_new_guid(session, page_context)
    problem_row = build_row(
        bill_code=bill_code,
        guid=guid,
        notice_site_code=notice_site_code,
        notice_site=notice_site,
        login_context=login_context,
        problem_cause=problem_cause,
        problem_type=problem_type,
        problem_owner_type=problem_owner_type,
    )
    save_result: dict[str, Any] = {}
    save_error: Exception | None = None
    try:
        raw_save_result = save(session, [make_operation("TAB_PROBLEM_ADD", problem_row)], page_context)
        if isinstance(raw_save_result, dict):
            save_result = raw_save_result
        else:
            save_error = RuntimeError("TMS 保存响应不是对象")
    except Exception as exc:
        save_error = exc

    try:
        verification = verify(
            session,
            expected=problem_row,
            login_context=login_context,
            page_context=query_page_context,
        )
    except Exception as exc:
        if save_error is not None:
            raise RuntimeError("WRITE_OUTCOME_UNKNOWN: TMS 保存响应不可用且权威列表未能确认写入") from exc
        if save_result.get("success") is False:
            message = _clean_text(save_result.get("message")) or "TMS 明确拒绝保存"
            raise RuntimeError(f"{message}；权威列表未找到完全一致记录") from exc
        raise RuntimeError("TMS 保存已返回，但权威列表未找到完全一致记录") from exc

    save_acknowledged = save_error is None and save_result.get("success") is not False

    postpone_updated = bool(postpone(session, bill_code, page_context)) if update_postpone else False
    return {
        "bill_code": bill_code,
        "saved": True,
        "verified": True,
        "save_acknowledged": save_acknowledged,
        "external_id": _clean_text(verification.get("external_id")) or guid,
        "problem_type": problem_type,
        "registered_at": problem_row.get("REGISTER_DATE"),
        "registration_saved_at": problem_row.get("REGISTER_SAVE_DATE"),
        "registered_site": problem_row.get("REGISTER_SITE"),
        "notice_site": notice_site,
        "notice_site_code": notice_site_code,
        "destination_site": _clean_text(bill_info.get("DESTINATION")),
        "guid": guid,
        "verification": verification,
        "postpone_updated": postpone_updated,
        "message": _clean_text(save_result.get("message")) or "verified",
    }
