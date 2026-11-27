"""Original-page processing actions run with a business account (2026-11-27).

Called from ``customer_service_problem`` for ``action=process`` (writes) and ``action=lookup``
(reads). Every write re-reads the record from the platform (the browser's copy only locates it),
runs the original page's checks from ``shared/original_page_actions.py``, saves exactly what the
page saves and reads it back; the result carries ``verified: True`` only when the read-back
matches, otherwise ``WRITE_OUTCOME_UNKNOWN``.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable

from agent.tms_runtime.scripts import customer_service_problem as base

O = base.RONGHUI_ORIGIN
WORK_MENU = "工单接收方查询-工单裁定申诉"
PROBLEM_MENU = "收到问题件查询"
_SUBPAGE_URL = re.compile(r"/widget/home\?authenticationKey=([0-9a-f]+)&pageId=([A-Za-z0-9]{20,})")


class ProcessingError(base.CustomerServiceProblemError):
    pass


def _text(value: Any) -> str:
    return base._clean_text(value)


def _rule(exc: Exception) -> ProcessingError:
    return ProcessingError("REPLY_RULE_REJECTED", str(exc))


def _user_info(session: Any) -> dict[str, Any]:
    info = base._read_user_info_cookie(session)
    if not all(_text(info.get(key)) for key in ("loginSiteName", "loginSiteCode", "loginEmpName", "loginEmpCode")):
        raise ProcessingError("LOGIN_INFO_MISSING", "融辉账号登录信息不完整，请到业务账号重新登录。")
    return info


def _subpage_context(session: Any, parent: dict[str, str], title: str) -> dict[str, str]:
    """The dialog page the list page opens (its own authenticationKey/pageId), found by its title."""
    html = getattr(session.get(parent["url"], timeout=20), "text", "") or ""
    found = []
    for key, page_id in sorted(set(_SUBPAGE_URL.findall(html))):
        url = f"{O}/widget/home?authenticationKey={key}&pageId={page_id}"
        page = getattr(session.get(url, timeout=20), "text", "") or ""
        match = re.search(r"<title>([^<]*)</title>", page)
        if match and match.group(1).strip() == title:
            found.append({"authentication_key": key, "page_id": page_id, "url": url, "menu_text": title})
    if len(found) != 1:
        raise ProcessingError("CAPTURE_REQUIRED", f"原系统找不到“{title}”页面，请到原系统核对。")
    return found[0]


def _find(session: Any, context: dict[str, str], call_id: str, data: dict[str, Any], *, paged: bool = False) -> list[dict[str, Any]]:
    response = session.post(f"{O}/dataQuery/{'findPageByCallId' if paged else 'findAllByCallId'}", params={"id": call_id},
                            data=data, headers=base._ronghui_headers(context, content_type="application/x-www-form-urlencoded; charset=UTF-8"),
                            timeout=base.RONGHUI_QUERY_TIMEOUT_SECONDS)
    payload = base._response_json(response, label="融辉查询")
    base._raise_if_source_failed(payload, label="融辉查询")
    return payload if isinstance(payload, list) else base._extract_rows(payload)


def _save(session: Any, context: dict[str, str], tables: list[tuple[str, list[dict[str, Any]]]], label: str) -> dict[str, Any]:
    envelope = [{"beforeAction": None, "operationKey": key, "afterAction": None, "idFields": [], "data": rows}
                for key, rows in tables]
    result = base._save_ronghui_tables(session, context, envelope)
    base._raise_if_source_failed(result, label=label, code="SOURCE_WRITE_FAILED")
    if not isinstance(result, dict) or result.get("success") is not True:
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", f"{label}已提交，但原系统没有明确返回成功，请到原系统核对，不要重复操作。")
    return result


def _server_now(session: Any, context: dict[str, str]) -> str:
    rows = _find(session, context, "FIND_NOW", {})
    value = _text(rows[0].get("SYSDATE")) if rows else ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        raise ProcessingError("INVALID_RESPONSE", "原系统没有返回服务器时间。")
    return value


# ---------------------------------------------------------------------------------------------
# 融辉工单
# ---------------------------------------------------------------------------------------------

def _work_order(session: Any, parent: dict[str, str], item: dict[str, Any], code: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The list's own re-check (``FIND_TAB_WORK_ORDER`` by bill/type/name), matched by work order code."""
    rows = _find(session, parent, "FIND_TAB_WORK_ORDER", {"BILL_CODE": _text(item.get("BILL_CODE") or item.get("waybill_no")),
                                                          "WORK_TYPE": _text(item.get("WORK_TYPE")),
                                                          "WORK_NAME": _text(item.get("WORK_NAME"))})
    matches = [row for row in rows if _text(row.get("WORK_ORDER_CODE")) == code]
    if len(matches) != 1:
        raise ProcessingError("PROBLEM_NOT_FOUND", "原系统找不到这张工单，请刷新后重试。")
    return matches[0], rows


def _work_order_process(session: Any, operation: str, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import (
        ActionRuleError, DATE_FORMAT, WORK_SUBPAGE_APPEAL, WORK_SUBPAGE_FOLLOW, validate_work_order,
        work_order_detail_guid, work_order_saved, work_order_tables,
    )

    code = _text(item.get("external_id") or item.get("WORK_ORDER_CODE"))
    if not code:
        raise ProcessingError("MISSING_REQUIRED_FIELDS", "缺少工单编号。")
    user_info = _user_info(session)
    parent = base._resolve_ronghui_page_context(session, WORK_MENU)
    row, same_bill = _work_order(session, parent, item, code)
    response_count, type_rows, now_value = 0, [], None
    if operation == "respond":
        response_count = len(_find(session, parent, "FIND_TAB_WORK_ORDER_DETAILS", {"WORK_ORDER_CODE": code, "HANDLE_STATE": 2}))
    if operation == "appeal":
        type_rows = _find(session, parent, "FIND_TAB_WORK_ORDER_TYPE", {"WORK_TYPE": _text(row.get("WORK_TYPE")),
                                                                       "WORK_NAME": _text(row.get("WORK_NAME"))})
        now_value = dt.datetime.strptime(_server_now(session, parent), DATE_FORMAT)
        if type_rows:
            row = {**row, "IS_ATTACHMENT_REQ": type_rows[0].get("IS_ATTACHMENT_REQ")}
    try:
        fields = validate_work_order(operation, row, payload, response_count=response_count,
                                     same_bill_rows=same_bill, type_rows=type_rows, server_now=now_value)
    except ActionRuleError as exc:
        raise _rule(exc) from None
    dialog = _subpage_context(session, parent, WORK_SUBPAGE_APPEAL if operation == "appeal" else WORK_SUBPAGE_FOLLOW)
    server_now = _server_now(session, dialog)
    now = dt.datetime.now(dt.timezone.utc)
    guid = work_order_detail_guid(now, _text(user_info.get("loginSiteCode")))
    tables = work_order_tables(operation, row, fields, user_info, server_now, guid, now.astimezone(base.SHANGHAI_TZ))
    result = _save(session, dialog, tables, "融辉工单")
    details = _find(session, parent, "FIND_TAB_WORK_ORDER_DETAILS", {"WORK_ORDER_CODE": code})
    after, _ = _work_order(session, parent, item, code)
    if not any(_text(entry.get("GUID")) == guid for entry in details) or not work_order_saved(operation, row, after):
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "融辉工单已提交，但读回的处理记录或工单状态不一致，请到原系统核对，不要重复操作。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": code}


# ---------------------------------------------------------------------------------------------
# 融辉问题件：标已查看、转发通知网点、联系人
# ---------------------------------------------------------------------------------------------

def _problem_row(session: Any, item: dict[str, Any]) -> dict[str, Any]:
    code = _text(item.get("external_id"))
    waybill = _text(item.get("waybill_no") or item.get("BILL_CODE"))
    if not code or not waybill:
        raise ProcessingError("MISSING_REQUIRED_FIELDS", "缺少问题件编号或单号。")
    return base._ronghui_problem_row(session, item, code, waybill)


def _problem_mark_seen(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import problem_needs_seen, problem_seen_row

    row = _problem_row(session, item)
    if not problem_needs_seen(row):
        return {"ok": True, "verified": True, "result": {"skipped": True}, "external_id": _text(item.get("external_id"))}
    context = base._resolve_ronghui_page_context(session, PROBLEM_MENU)
    result = _save(session, context, [("TAB_PROBLEM_UPT", [problem_seen_row(row)])], "融辉问题件标已查看")
    after = _problem_row(session, item)
    if _text(after.get("BL_SEE_STR")) != "已查看":
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "已提交标为已查看，但读回仍是未查看。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": _text(item.get("external_id"))}


def _sites(session: Any, context: dict[str, str], key: str) -> list[dict[str, Any]]:
    rows = _find(session, context, "FIND_SITE_ALL_COMBOBOX", {"key": key, "pageIndex": 0, "pageSize": 20}, paged=True)
    return [{"SITE_CODE": _text(row.get("SITE_CODE")), "SITE_NAME": _text(row.get("SITE_NAME"))} for row in rows[:20]]


def _problem_forward(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import ActionRuleError, problem_forward_row, validate_problem_forward

    user_info = _user_info(session)
    row = _problem_row(session, item)
    context = base._resolve_ronghui_page_context(session, PROBLEM_MENU)
    code = _text(payload.get("site_code"))
    try:
        site = validate_problem_forward(row, _text(user_info.get("loginSiteName")), payload,
                                        _sites(session, context, code) if code else [])
    except ActionRuleError as exc:
        raise _rule(exc) from None
    result = _save(session, context, [("TAB_PROBLEM_UPT", [problem_forward_row(row, site)])], "融辉问题件转发")
    after = base._ronghui_full_problem_row(session, context, row, _text(item.get("external_id")))
    if _text(after.get("SEND_SITE_CODE")) != site["SEND_SITE_CODE"]:
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "转发已提交，但读回的通知网点不一致，请到原系统核对。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": _text(item.get("external_id"))}


def _problem_contact(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import contact_text

    row = _problem_row(session, item)
    context = base._resolve_ronghui_page_context(session, PROBLEM_MENU)
    sites = _find(session, context, "FIND_SITE_INFO_BY_PROBLEM_SITE", {"SITE_CODE": _text(row.get("REGISTER_SITE_CODE"))})
    return {"ok": True, "contact": contact_text(sites[0] if sites else None) if sites else ""}


def _site_search(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import ActionRuleError, site_search_key

    try:
        key = site_search_key(payload.get("key"))
    except ActionRuleError as exc:
        raise _rule(exc) from None
    return {"ok": True, "sites": _sites(session, base._resolve_ronghui_page_context(session, PROBLEM_MENU), key)}


# ---------------------------------------------------------------------------------------------
# 融辉投诉、申诉、代取件
# ---------------------------------------------------------------------------------------------

AGAINST_MENU, FILED_MENU, APPEAL_MENU, PICKUP_MENU = "被投诉方查询", "投诉方登记", "申诉登记", "网点代取件业务"
WINDOW_DAYS, PAGE_SIZE, MAX_PAGES = 31, 200, 10


def _all_rows(session: Any, context: dict[str, str], call_id: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        batch = _find(session, context, call_id, {**data, "pageIndex": page, "pageSize": PAGE_SIZE}, paged=True)
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
    raise ProcessingError("TOO_MANY_ROWS", "原系统记录过多，无法确认这条记录，请到原系统处理。")


def _window(days: int = WINDOW_DAYS) -> str:
    from agent.tms_runtime.monitoring_ronghui import _window_range

    return _window_range(days)


def _site_scope(user_info: dict[str, Any]) -> dict[str, Any]:
    return {} if _text(user_info.get("loginSiteType")) == "总部" else {"LOGIN_SITE_CODE": _text(user_info.get("loginSiteCode"))}


def _complaint_row(session: Any, context: dict[str, str], call_id: str, user_info: dict[str, Any], exception_id: str) -> dict[str, Any]:
    window = _window()
    data = {"searchDateType": "EXCEPTION_DATE", "EXCEPTION_DATE": window, "SEARCH_DATE_RANGE": window,
            "CURRENT_LOGIN_SITE": _text(user_info.get("loginSiteCode")), **_site_scope(user_info)}
    matches = [row for row in _all_rows(session, context, call_id, data) if _text(row.get("EXCEPTION_ID")) == exception_id]
    if len(matches) != 1:
        raise ProcessingError("PROBLEM_NOT_FOUND", "原系统找不到这条投诉，请刷新后重试。")
    return matches[0]


def _paths(session: Any, context: dict[str, str], exception_id: str, upload_type: str) -> list[dict[str, Any]]:
    return _find(session, context, "FIND_EXCEPTION_PATH", {"EXCEPTION_ID": exception_id, "FILE_UPLOAD_TYPE": upload_type})


def _complaint_against_reply(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import ActionRuleError, complaint_reply_guid, complaint_reply_tables, validate_complaint_reply

    exception_id = _text(item.get("external_id"))
    user_info = _user_info(session)
    context = base._resolve_ronghui_page_context(session, AGAINST_MENU)
    row = _complaint_row(session, context, "FIND_TAB_EXCEPTION_SIDE", user_info, exception_id)
    try:
        fields = validate_complaint_reply(payload)
    except ActionRuleError as exc:
        raise _rule(exc) from None
    dialog = _subpage_context(session, context, "投诉回复-新增")
    guids = _find(session, dialog, "FIND_GUID", {})
    guid = complaint_reply_guid(_text(guids[0].get("GUID")) if guids else "")
    if not guid:
        raise ProcessingError("INVALID_RESPONSE", "原系统没有返回记录编号。")
    result = _save(session, dialog, complaint_reply_tables(row, fields, user_info, guid, dt.datetime.now(base.SHANGHAI_TZ)), "融辉投诉回复")
    records = _find(session, context, "FIND_TAB_PROCESS_RECORD", {"EXCEPTION_ID": exception_id, "pageIndex": 0, "pageSize": 50}, paged=True)
    if not any(_text(record.get("GUID")) == guid for record in records):
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "投诉回复已提交，但处理记录里读不到这条回复，请到原系统核对，不要重复回复。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": exception_id}


def _fresh_complaint(session: Any, context: dict[str, str], exception_id: str) -> dict[str, Any] | None:
    rows = _find(session, context, "FIND_TAB_EXCEPTION_REGISTER", {"EXCEPTION_ID": exception_id})
    return rows[0] if rows else None


def _complaint_filed_cancel(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import ActionRuleError, complaint_cancel_block, complaint_cancel_check

    exception_id = _text(item.get("external_id"))
    user_info = _user_info(session)
    context = base._resolve_ronghui_page_context(session, FILED_MENU)
    row = _complaint_row(session, context, "FIND_TAB_EXCEPTION_REGISTER_CS", user_info, exception_id)
    try:
        blocked = complaint_cancel_block(row, user_info)
        if blocked:
            raise ActionRuleError(blocked)
        complaint_cancel_check(_fresh_complaint(session, context, exception_id))
    except ActionRuleError as exc:
        raise _rule(exc) from None
    result = _save(session, context, [("TAB_EXCEPTION_DEL", [row])], "融辉取消投诉")
    if _fresh_complaint(session, context, exception_id) is not None:
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "取消投诉已提交，但原系统仍能读到这条投诉，请到原系统核对。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": exception_id}


def _save_paths(session: Any, context: dict[str, str], rows: list[dict[str, Any]]) -> None:
    for row in rows:  # 原页逐个附件单独保存
        _save(session, context, [("TAB_EXCEPTION_PATH_ADD", [row])], "融辉附件")


def _complaint_filed_appeal(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import ActionRuleError, complaint_appeal_update, exception_path_row, validate_complaint_appeal

    exception_id = _text(item.get("external_id"))
    user_info = _user_info(session)
    context = base._resolve_ronghui_page_context(session, FILED_MENU)
    row = _complaint_row(session, context, "FIND_TAB_EXCEPTION_REGISTER_CS", user_info, exception_id)
    submitted = bool(_paths(session, context, exception_id, "被投诉方附件"))
    try:
        fields = validate_complaint_appeal(row, payload, dt.datetime.now(base.SHANGHAI_TZ), submitted)
    except ActionRuleError as exc:
        raise _rule(exc) from None
    server_now = _server_now(session, context)
    result = _save(session, context, [("TAB_EXCEPTION_UPT", [complaint_appeal_update(row, fields, user_info, server_now)])], "融辉提交申诉")
    _save_paths(session, context, [exception_path_row(exception_id, file["file_name"], file["file_path"], "被投诉方附件", user_info, server_now)
                                   for _label, file in fields["files"]])
    fresh = _fresh_complaint(session, context, exception_id) or {}
    if _text(fresh.get("APPEAL_CONTENT")) != fields["content"]:
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "申诉已提交，但读回的申诉说明不一致，请到原系统核对。")
    if len(_paths(session, context, exception_id, "被投诉方附件")) < len(fields["files"]):
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "申诉已保存，但附件没有全部读回，请到原系统核对附件。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": exception_id}


def _complaint_filed_claim(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import CLAIM_SLOTS, ActionRuleError, complaint_claim_update, exception_path_row, validate_complaint_claim

    exception_id = _text(item.get("external_id"))
    user_info = _user_info(session)
    context = base._resolve_ronghui_page_context(session, FILED_MENU)
    row = _complaint_row(session, context, "FIND_TAB_EXCEPTION_REGISTER_CS", user_info, exception_id)
    submitted = any(_paths(session, context, exception_id, label) for _key, label in CLAIM_SLOTS)
    try:
        fields = validate_complaint_claim(row, payload, submitted)
    except ActionRuleError as exc:
        raise _rule(exc) from None
    server_now = _server_now(session, context)
    result = _save(session, context, [("TAB_EXCEPTION_UPT", [complaint_claim_update(row, user_info, server_now)])], "融辉提交材料")
    _save_paths(session, context, [exception_path_row(exception_id, file["file_name"], file["file_path"], label, user_info, server_now)
                                   for label, file in fields["files"]])
    missing = [label for label, _file in fields["files"] if not _paths(session, context, exception_id, label)]
    if missing:
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", f"材料已提交，但{('、'.join(missing))}没有读回，请到原系统核对。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": exception_id}


def _appeal_rows(session: Any, context: dict[str, str], user_info: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
    rows = _all_rows(session, context, "FIND_TAB_EXCEPTION_APPEAL", _site_scope(user_info))
    return [row for row in rows if f"{_text(row.get('EXCEPTION_ID'))}:{_text(row.get('CREATE_DATE'))}" == identifier]


def _appeal_delete(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import appeal_delete_block

    identifier = _text(item.get("external_id"))
    user_info = _user_info(session)
    context = base._resolve_ronghui_page_context(session, APPEAL_MENU)
    rows = _appeal_rows(session, context, user_info, identifier)
    if len(rows) != 1:
        raise ProcessingError("PROBLEM_NOT_FOUND", "原系统找不到这条申诉，请刷新后重试。")
    blocked = appeal_delete_block(rows[0])
    if blocked:
        raise ProcessingError("REPLY_RULE_REJECTED", blocked)
    result = _save(session, context, [("TAB_EXCEPTION_APPEAL_DEL", [rows[0]])], "融辉删除申诉")
    if _appeal_rows(session, context, user_info, identifier):
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "删除已提交，但原系统仍能读到这条申诉，请到原系统核对。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": identifier}


def _pickup_row(session: Any, context: dict[str, str], user_info: dict[str, Any], order_id: str) -> dict[str, Any]:
    window = _window()
    data = {"searchDateType": "CREATE_DATE", "CREATE_DATE": window, "SEARCH_DATE_RANGE": window, **_site_scope(user_info)}
    matches = [row for row in _all_rows(session, context, "FIND_TAB_UNITE_ORDERS_DQJ", data)
               if _text(row.get("TX_LOGISTIC_ID")) == order_id]
    if len(matches) != 1:
        raise ProcessingError("PROBLEM_NOT_FOUND", "原系统找不到这张代取件订单，请刷新后重试。")
    return matches[0]


def _pickup_revoke(session: Any, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from shared.original_page_actions import pickup_revoke_block, pickup_revoke_row, pickup_status

    order_id = _text(item.get("external_id"))
    user_info = _user_info(session)
    context = base._resolve_ronghui_page_context(session, PICKUP_MENU)
    row = _pickup_row(session, context, user_info, order_id)
    blocked = pickup_revoke_block(row)
    if blocked:
        raise ProcessingError("REPLY_RULE_REJECTED", blocked)
    result = _save(session, context, [("TAB_UNITE_ORDERS_UPT", [pickup_revoke_row(row, user_info, _server_now(session, context))])], "融辉撤销代取件")
    if pickup_status(_pickup_row(session, context, user_info, order_id)) != "3":
        raise ProcessingError("WRITE_OUTCOME_UNKNOWN", "撤销已提交，但读回的订单状态不是已取消，请到原系统核对。")
    return {"ok": True, "verified": True, "result": base._safe_json(result), "external_id": order_id}


# (platform, kind, operation) -> handler(session, item, payload)
Handler = Callable[[Any, dict[str, Any], dict[str, Any]], dict[str, Any]]
PROCESS: dict[tuple[str, str, str], Handler] = {
    ("ronghui", "problem", "mark_seen"): _problem_mark_seen,
    ("ronghui", "problem", "forward"): _problem_forward,
    **{("ronghui", "work_order", operation): (lambda op: lambda s, i, p: _work_order_process(s, op, i, p))(operation)
       for operation in ("respond", "finish", "urge", "appeal")},
    ("ronghui", "complaint_against", "reply"): _complaint_against_reply,
    ("ronghui", "complaint_filed", "cancel"): _complaint_filed_cancel,
    ("ronghui", "complaint_filed", "appeal"): _complaint_filed_appeal,
    ("ronghui", "complaint_filed", "claim"): _complaint_filed_claim,
    ("ronghui", "appeal", "delete"): _appeal_delete,
    ("ronghui", "pickup", "revoke"): _pickup_revoke,
}
LOOKUP: dict[tuple[str, str, str], Handler] = {
    ("ronghui", "problem", "contact"): _problem_contact,
    ("ronghui", "site", "search"): _site_search,
}


def run(platform: str, action: str, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    key = (platform, _text(params.get("kind")), _text(params.get("operation")))
    handler = (PROCESS if action == "process" else LOOKUP).get(key)
    if handler is None:
        raise ProcessingError("UNSUPPORTED_ACTION", f"不支持的处理动作：{'/'.join(key)}")
    result = handler(session, item, payload)
    json.dumps(result, ensure_ascii=False)  # results must stay plain JSON
    return result


__all__ = ["LOOKUP", "PROCESS", "ProcessingError", "run"]
