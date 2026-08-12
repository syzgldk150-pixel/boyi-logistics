"""Shared Ronghui problem-item upload primitives.

The field names and type/owner mappings consumed by callers are based on the
real MiniUI "问题件录入" page.  This module deliberately performs no
existing-problem lookup: callers decide which rows to submit and every item is
attempted once per invocation.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse


BASE_URL = "https://tms.ronghuiwl.com"
MENU_URL = f"{BASE_URL}/menuTreeExtend/loadMenu"
INDEX_URL = f"{BASE_URL}/module/index?mv=index"
DATA_QUERY_URL = f"{BASE_URL}/dataQuery/findAllByCallId"
SAVE_TABLES_URL = f"{BASE_URL}/dataOperation/saveTables"

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


def resolve_problem_page_context(session: Any) -> dict[str, str]:
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
        if text == "问题件录入" or path.endswith("/问题件录入"):
            candidates.insert(0, url)
        elif "问题件录入" in path:
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
        if "TAB_PROBLEM_ADD" in html or "问题件录入" in html:
            resolved.append(
                {
                    "url": full_url,
                    "authentication_key": auth_key,
                    "page_id": page_id,
                }
            )
    if not resolved:
        raise RuntimeError("无法定位 TMS 问题件录入页面")
    unique = {
        (item["url"], item["authentication_key"], item["page_id"]): item
        for item in resolved
    }
    if len(unique) != 1:
        raise RuntimeError(f"TMS 问题件录入页面存在 {len(unique)} 个候选，拒绝隐式选择")
    return next(iter(unique.values()))


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

    bill_code = _clean_text(record.get("bill_code"))
    problem_type = _clean_text(record.get("problem_type"))
    problem_owner_type = _clean_text(record.get("problem_owner_type"))
    problem_cause = _clean_text(record.get("problem_cause"))
    if not bill_code or not problem_type or not problem_owner_type or not problem_cause:
        raise RuntimeError("问题件缺少运单号、问题类型、问题归属或问题原因")

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
    save_result = save(session, [make_operation("TAB_PROBLEM_ADD", problem_row)], page_context)
    if save_result.get("success") is False:
        raise RuntimeError(_clean_text(save_result.get("message")) or f"TMS 保存失败: {save_result}")

    postpone_updated = bool(postpone(session, bill_code, page_context)) if update_postpone else False
    return {
        "bill_code": bill_code,
        "saved": True,
        "external_id": guid,
        "problem_type": problem_type,
        "registered_at": problem_row.get("REGISTER_DATE"),
        "registration_saved_at": problem_row.get("REGISTER_SAVE_DATE"),
        "registered_site": problem_row.get("REGISTER_SITE"),
        "notice_site": notice_site,
        "notice_site_code": notice_site_code,
        "destination_site": _clean_text(bill_info.get("DESTINATION")),
        "guid": guid,
        "postpone_updated": postpone_updated,
        "message": _clean_text(save_result.get("message")) or "success",
    }
