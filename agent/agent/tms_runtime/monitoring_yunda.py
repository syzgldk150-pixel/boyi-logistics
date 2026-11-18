"""Read-only adapters for the verified Yunda problem, arbitration, transfer and work-order pages.

用户确认（2026-11-18）：韵达客户端“待办事项”消息中心更新不及时，首页不再读取；
只读取问题件查询、仲裁查询、调拨件确认和快运工单系统四个原页的未完结记录。
"""

from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime import monitoring_window
from agent.tms_runtime.monitoring_window import RECENT_NOTE, is_recent, latest_activity


CSSP_ORIGIN = "https://cssp.yunda56.com"
WORK_ORDER_PAGE = f"{CSSP_ORIGIN}/cssp/vue/index.html#/work-order/deal"
PROBLEM_ROOT = "https://kyproblem.yunda56.com/ky_problem/public/index.php"
PROBLEM_PAGE = f"{PROBLEM_ROOT}/query/index.html?kyflag=new_client"
PROBLEM_LIST = f"{PROBLEM_ROOT}/query/list.html"
ARBITRATION_ORIGIN = "https://kyzc.yunda56.com:8090"
ARBITRATION_PAGE = f"{ARBITRATION_ORIGIN}/kyzc/arbitrament/arbitramentSearch"
ARBITRATION_LIST = f"{ARBITRATION_PAGE}/list"
TRANSFER_ROOT = "https://kyinms.yunda56.com/ky_inms/public/index.php/business/replaceLogistics/ReplaceLogisticsConfirm"
TRANSFER_PAGE = f"{TRANSFER_ROOT}/index.html?page=tab"
TRANSFER_LIST = f"{TRANSFER_ROOT}/list.html"

_SOURCES = {
    "yunda:problems": ("问题件 · 未完结", "received_problems", PROBLEM_PAGE),
    "yunda:arbitrations": ("仲裁 · 未结束", "arbitrations", ARBITRATION_PAGE),
    "yunda:transfers": ("调拨件 · 待接单", "transfers", TRANSFER_PAGE),
    "yunda:work_orders": ("收到的待处理工单", "work_orders", WORK_ORDER_PAGE),
    "yunda:work_order_replies": ("工单催单待回复", "work_order_replies", WORK_ORDER_PAGE),
}
ITEM_CATEGORY_IDS = frozenset(_SOURCES)
FETCH_PAGE_SIZE = 100
MAX_PENDING_ROWS = 500
# 问题件、仲裁、调拨件原页一次最多查询 31 天（实测 2026-11-18），最近 3 天范围在限制内。

# 问题件查询 base/getProbStatus：0 未处理、1 处理中、3 超时未完结（2 已完结、4 超时已完结、5 无需处理不显示）。
PROBLEM_OPEN_STATUSES = {"0": "未处理", "1": "处理中", "3": "超时未完结"}
# 仲裁查询“最新处理状态”：除三个终态外都显示（用户确认 2026-11-18）。
ARBITRATION_OPEN_CODES = "ysbwjd,yjdwcl,yclwss,yssdcl,ssycldsh,tdkjxsb,ycldsh,jxsbwcl"
ARBITRATION_FINAL_STATES = {"结算审核通过", "结算审核不通过", "中心不受理"}
# 调拨件确认 Replace_Status：0 待接单（用户确认只显示待接单）。
TRANSFER_PENDING_STATUS = "0"


class SourceError(RuntimeError):
    """An upstream shape or business error with a safe, local message."""


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(self.hidden - 1, 0)

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _content(value: Any) -> str:
    parser = _PlainText()
    parser.feed(unescape(_text(value)))
    return "\n".join(part.strip() for part in "".join(parser.parts).splitlines() if part.strip())


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", _text(value)):
        raise SourceError(f"韵达返回的 {field} 数量无效。")
    return int(value)


def _request(session: Any, method: str, url: str, *, timeout: int = 15, **kwargs: Any) -> Any:
    """Never expose request exceptions, which may include session query values."""
    try:
        response = getattr(session, method)(url, timeout=timeout, allow_redirects=False, **kwargs)
    except requests.Timeout as exc:
        raise SourceError("韵达来源请求超时，请稍后刷新。") from exc
    except requests.RequestException as exc:
        raise SourceError("韵达来源暂时无法连接，请稍后刷新。") from exc
    status = getattr(response, "status_code", 0)
    if status in {301, 302, 303, 307, 308, 401, 403}:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达该来源登录态已失效，请重新登录。")
    if status != 200:
        raise SourceError(f"韵达来源返回 HTTP {status}。")
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        body = _text(getattr(response, "text", "")).lower()
        if any(mark in body for mark in ("/login", "用户登录", "<title>登录", "session error")):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达该来源登录态已失效，请重新登录。") from exc
        raise SourceError("韵达来源未返回有效 JSON 数据。") from exc
    if isinstance(payload, dict):
        message = " ".join(_text(payload.get(key)) for key in ("msg", "message", "cause"))
        code = payload.get("code", payload.get("errorCode"))
        if code in {401, 403, 1001, "401", "403", "1001", "AUTH_REQUIRED"} or any(
            mark in message.lower() for mark in ("未登录", "请登录", "请先登录", "请重新登录", "登录失效", "登录过期", "session error")
        ):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达该来源登录态已失效，请重新登录。")
    return payload


def _category(category_id: str, count: int | None = None) -> dict[str, Any]:
    title, count_scope, open_url = _SOURCES[category_id]
    return {
        "system": "yunda", "category_id": category_id, "title": title, "count": count,
        "status": "pending" if count else "empty", "status_label": "待处理" if count else "无待办",
        "items_supported": True, "detail_supported": True, "open_url": open_url,
        "is_exception": True, "tone": "warning" if count else "neutral", "count_scope": count_scope,
        "include_in_total": False, "message": RECENT_NOTE,
    }


def _failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TMSAuthStateError):
        return {"status": "auth_required", "status_label": "登录失效", "message": str(exc)}
    return {
        "status": "error", "status_label": "读取失败",
        "message": str(exc) if isinstance(exc, SourceError) else "韵达来源读取失败，请稍后刷新。",
    }


def _rows(payload: Any) -> tuple[list[dict], int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise SourceError("韵达列表结构变化：缺少 rows。")
    if any(payload[key] not in {0, "0"} for key in ("code", "errorCode") if key in payload):
        raise SourceError("韵达列表业务响应失败。")
    if any(not isinstance(row, dict) for row in payload["rows"]):
        raise SourceError("韵达列表记录结构异常。")
    total = _count(payload.get("total"), "total")
    if len(payload["rows"]) > total:
        raise SourceError("韵达列表记录数超过来源总数。")
    return payload["rows"], total


def _recent_range() -> tuple[dt.date, dt.date]:
    end = monitoring_window.today()
    return monitoring_window.cutoff_date(end), end


def _read_all(fetch_page: Callable[[int], tuple[list[dict], int]]) -> list[dict]:
    """Read every page of one bounded query; a short page with a larger total is a failure."""
    rows: list[dict] = []
    page = 1
    while True:
        page_rows, total = fetch_page(page)
        if total > MAX_PENDING_ROWS:
            raise SourceError("韵达未完结记录过多，请打开原系统核对。")
        if len(page_rows) > FETCH_PAGE_SIZE:
            raise SourceError("韵达列表未遵守分页限制。")
        if not page_rows and (page - 1) * FETCH_PAGE_SIZE < total:
            raise SourceError("韵达来源总数非零但本页没有记录，请重新刷新。")
        rows.extend(page_rows)
        if page * FETCH_PAGE_SIZE >= total:
            return rows
        page += 1


def _merge(rows: list[dict], key: str, label: str) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in rows:
        identifier = _text(row.get(key))
        if not identifier:
            raise SourceError(f"韵达{label}记录缺少 {key}。")
        # 同一记录可能因分页期间数据变动重复出现；按原系统编号去重，不重复计数。
        merged.setdefault(identifier, row)
    return list(merged.values())


def _problem_rows(session: Any) -> list[dict]:
    headers = {"Referer": f"{PROBLEM_ROOT}/query/index.html", "Origin": "https://kyproblem.yunda56.com",
               "X-Requested-With": "XMLHttpRequest"}
    rows: list[dict] = []
    start, end = _recent_range()
    # time=1 按发布时间、time=2 按处理时间：两者合并即最近有新发布或新处理的问题件。
    for time_type in ("1", "2"):
        for status in PROBLEM_OPEN_STATUSES:
            def fetch(page: int, status: str = status, time_type: str = time_type):
                # issuer_site=1 是原页“发布给我的”。
                data = {"issuer_site": "1", "prob_status": status, "time": time_type,
                        "start_date": start.isoformat(), "start_time": "00:00:00",
                        "end_date": end.isoformat(), "end_time": "23:59:59",
                        "page": page, "rows": FETCH_PAGE_SIZE}
                return _rows(_request(session, "post", PROBLEM_LIST, data=data, headers=headers, timeout=20))
            rows.extend(_read_all(fetch))
    merged = _merge(rows, "prob_main_id", "问题件")
    open_labels = set(PROBLEM_OPEN_STATUSES.values())
    if any(_text(row.get("prob_status")) not in open_labels for row in merged):
        raise SourceError("韵达问题件返回了已完结记录，已停止展示该查询结果。")
    return merged


def _open_arbitration_session(session: Any) -> None:
    """kyzc 通过韵达统一登录换取本系统会话；只跟随已知登录跳转，不提交任何表单。"""
    try:
        response = session.get(ARBITRATION_PAGE, timeout=25, allow_redirects=True)
    except requests.Timeout as exc:
        raise SourceError("韵达仲裁系统请求超时，请稍后刷新。") from exc
    except requests.RequestException as exc:
        raise SourceError("韵达仲裁系统暂时无法连接，请稍后刷新。") from exc
    final = urlsplit(_text(getattr(response, "url", "")))
    body = _text(getattr(response, "text", ""))
    if final.hostname != "kyzc.yunda56.com" or not final.path.startswith("/kyzc/arbitrament/arbitramentSearch") \
            or "快运仲裁查询" not in body:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达仲裁系统登录态已失效，请重新登录韵达账号。")
    if getattr(response, "status_code", 0) != 200:
        raise SourceError(f"韵达仲裁系统返回 HTTP {getattr(response, 'status_code', 0)}。")


def _arbitration_state(row: dict) -> str:
    return _text(row.get("disposeState")).replace("，", ",")


def _arbitration_rows(session: Any) -> list[dict]:
    _open_arbitration_session(session)
    headers = {"Referer": ARBITRATION_PAGE, "X-Requested-With": "XMLHttpRequest"}
    rows: list[dict] = []
    start, end = _recent_range()
    # 申报时间或最新处理时间落在最近 3 天的仲裁都显示。
    for search_date in ("k.declare_date", "g.assignment_update_time"):
        def fetch(page: int, search_date: str = search_date):
            params = {"searchDate": search_date, "startDate": start.isoformat(), "endDate": end.isoformat(),
                      "belongCate": "arbitrament", "disposeState": ARBITRATION_OPEN_CODES,
                      "limit": FETCH_PAGE_SIZE, "offset": (page - 1) * FETCH_PAGE_SIZE}
            return _rows(_request(session, "get", ARBITRATION_LIST, params=params, headers=headers, timeout=20))
        rows.extend(_read_all(fetch))
    merged = _merge(rows, "declId", "仲裁")
    if any(_arbitration_state(row) in ARBITRATION_FINAL_STATES or not _arbitration_state(row) for row in merged):
        raise SourceError("韵达仲裁返回了已结束或无状态的记录，已停止展示该查询结果。")
    return merged


def _transfer_rows(session: Any) -> list[dict]:
    headers = {"Referer": TRANSFER_PAGE, "X-Requested-With": "XMLHttpRequest"}
    start, end = _recent_range()

    def fetch(page: int):
        data = {"start_date": start.isoformat(), "start_time": "00:00:00",
                "end_date": end.isoformat(), "end_time": "23:59:59",
                "Replace_Status": TRANSFER_PENDING_STATUS, "page": page, "rows": FETCH_PAGE_SIZE}
        return _rows(_request(session, "post", TRANSFER_LIST, data=data, headers=headers, timeout=20))

    merged = _merge(_read_all(fetch), "Replace_Id", "调拨件")
    if any(_text(row.get("Replace_Status_Code")) != TRANSFER_PENDING_STATUS for row in merged):
        raise SourceError("韵达调拨件返回了非待接单记录，已停止展示该查询结果。")
    return merged


def _cssp_user_id(session: Any) -> str:
    identity = _request(session, "get", f"{CSSP_ORIGIN}/cssp/sys/user/getUser", headers={"Referer": WORK_ORDER_PAGE})
    if not isinstance(identity, dict) or identity.get("code") not in {0, "0"}:
        raise SourceError("韵达工单用户信息接口业务响应失败。")
    user = identity.get("User")
    if not isinstance(user, dict) or not _text(user.get("userId")):
        raise SourceError("韵达工单用户信息缺少 User.userId，无法确认收到的工单范围。")
    return _text(user["userId"])


def _work_order_rows(session: Any, category_id: str, page: int, page_size: int, *, user_id: str | None = None) -> tuple[list[dict], int]:
    user_id = user_id if user_id is not None else _cssp_user_id(session)
    endpoint = "/cssp/comp/sysComplaintSearch/compDealPageQuery" if category_id == "yunda:work_orders" else "/cssp/collaboration/sysCollaborationManage/cuiPageQuery"
    params = {"offset": (page - 1) * page_size, "limit": page_size}
    if category_id == "yunda:work_order_replies":
        params["userId"] = user_id
    payload = _request(session, "get", CSSP_ORIGIN + endpoint, params=params, headers={"Referer": WORK_ORDER_PAGE})
    return _rows(payload)


def _recent_cssp_rows(session: Any, category_id: str, *, user_id: str | None = None) -> list[dict]:
    """快运工单系统按原系统分页读全量，再只保留最近 3 天登记或催单的记录。"""
    user_id = user_id if user_id is not None else _cssp_user_id(session)
    rows = _read_all(lambda page: _work_order_rows(session, category_id, page, FETCH_PAGE_SIZE, user_id=user_id))
    time_key = "compTime" if category_id == "yunda:work_orders" else "colTime"
    return [row for row in rows if is_recent([row.get(time_key)])]


_READERS = {
    "yunda:problems": _problem_rows,
    "yunda:arbitrations": _arbitration_rows,
    "yunda:transfers": _transfer_rows,
}


def collect_categories(session: Any) -> list[dict[str, Any]]:
    """Collect each source independently; unknown amounts are never zeroed."""
    result: list[dict[str, Any]] = []
    cssp_user_id: str | None = None
    cssp_error: Exception | None = None
    for category_id in _SOURCES:
        category = _category(category_id)
        try:
            if category_id in _READERS:
                total = len(_READERS[category_id](session))
            else:
                if cssp_error:
                    raise cssp_error
                if cssp_user_id is None:
                    try:
                        cssp_user_id = _cssp_user_id(session)
                    except Exception as exc:
                        cssp_error = exc
                        raise
                total = len(_recent_cssp_rows(session, category_id, user_id=cssp_user_id))
            category = _category(category_id, total)
        except Exception as exc:
            category.update(_failure(exc), count=None, tone="warning")
        result.append(category)
    return result


def _problem_item(row: dict) -> dict[str, Any]:
    lines = [_content(row.get("prob_text")) or "原系统正文为空"]
    if _text(row.get("site_id")):
        lines.append(f"发布网点：{_text(row['site_id'])}")
    reply = _content(row.get("reply_text"))
    if reply:
        lines.append(f"最新回复（{_text(row.get('reply_time')) or '时间未提供'}）：{reply}")
    else:
        lines.append("本网点尚未回复")
    title = _text(row.get("prob_type_nm")) or _text(row.get("prnt_prob_name")) or "问题件"
    return {
        "id": _text(row["prob_main_id"]), "title": title, "content": "\n".join(lines),
        "occurred_at": latest_activity([row.get("created_time"), row.get("modified_time"), row.get("reply_time")]),
        "status_label": _text(row.get("prob_status")),
        "waybill_no": _text(row.get("ship_no")), "source_label": "韵达问题件查询",
        "open_url": PROBLEM_PAGE, "deadline": _text(row.get("over_time")),
    }


def _arbitration_item(row: dict) -> dict[str, Any]:
    fields = (("申报网点", "declareDept"), ("费用大类", "costTypeName"), ("金额", "money"),
              ("申报时间", "createDate"), ("申诉时间", "appealDate"), ("最新处理时间", "assignmentUpdateTime"))
    lines = [f"{label}：{_text(row.get(key))}" for label, key in fields if _text(row.get(key))]
    title = " · ".join(filter(None, (_text(row.get("declType")), _text(row.get("declCategory"))))) or "仲裁"
    return {
        "id": _text(row["declId"]), "title": title, "content": "\n".join(lines) or "原系统未提供详情",
        "occurred_at": latest_activity([row.get("createDate"), row.get("appealDate"), row.get("assignmentUpdateTime")]),
        "status_label": _arbitration_state(row), "waybill_no": _text(row.get("waybillCode")),
        "source_label": "韵达仲裁查询", "open_url": ARBITRATION_PAGE,
    }


def _transfer_item(row: dict) -> dict[str, Any]:
    fields = (("发布网点", "Apply_Site_Name"), ("取件网点", "Replace_Site_Name"), ("取件地址", "Sender_Address"),
              ("预约取件时间", "Appointment_Take_Time"), ("订单号", "Order_Id"))
    lines = [f"{label}：{_text(row.get(key))}" for label, key in fields if _text(row.get(key))]
    return {
        "id": _text(row["Replace_Id"]), "title": "调拨件待接单", "content": "\n".join(lines) or "原系统未提供详情",
        "occurred_at": _text(row.get("Create_Time")), "status_label": _text(row.get("Replace_Status")) or "待接单",
        "waybill_no": _text(row.get("Logistics_Id")), "source_label": "韵达调拨件确认", "open_url": TRANSFER_PAGE,
    }


def _work_order_item(session: Any, row: dict) -> dict[str, Any]:
    complaint_id = _text(row.get("complaintId"))
    if not complaint_id:
        raise SourceError("韵达工单记录缺少 complaintId。")
    info = _request(session, "get", f"{CSSP_ORIGIN}/cssp/comp/sysCompMain/compInfo/{quote(complaint_id, safe='')}", timeout=8, headers={"Referer": WORK_ORDER_PAGE})
    if not isinstance(info, dict) or "compContent" not in info:
        raise SourceError("韵达工单详情缺少 compContent，无法展示正文。")
    if _text(info.get("complaintId")) != complaint_id:
        raise SourceError("韵达工单详情编号与列表不一致，已停止展示正文。")
    return {
        "id": complaint_id, "title": _text(row.get("compTypeName")) or "收到的工单",
        "content": _content(info["compContent"]), "occurred_at": _text(row.get("compTime")),
        "status_label": _text(row.get("compStatusName")) or "状态未确认",
        "waybill_no": _text(row.get("listId")), "source_label": "韵达快运工单系统",
        "open_url": WORK_ORDER_PAGE, "deadline": _text(row.get("dealOverTime")),
    }


def _reply_item(row: dict) -> dict[str, Any]:
    complaint_id = _text(row.get("complaintId"))
    occurred_at = _text(row.get("colTime"))
    if not complaint_id or not occurred_at or "remark" not in row:
        raise SourceError("韵达催单待回复记录缺少 complaintId、colTime 或 remark。")
    return {
        "id": f"{complaint_id}:{occurred_at}", "title": "收到的催单待回复",
        "content": _content(row["remark"]), "occurred_at": occurred_at,
        "status_label": "待回复", "waybill_no": _text(row.get("listId")),
        "source_label": "韵达快运工单系统", "open_url": WORK_ORDER_PAGE,
    }


_ITEMS = {
    "yunda:problems": _problem_item,
    "yunda:arbitrations": _arbitration_item,
    "yunda:transfers": _transfer_item,
}


def collect_items(session: Any, category_id: str, page: int, page_size: int = 20) -> dict[str, Any]:
    """Read one page, without acknowledgement, reply, acceptance or read writes."""
    if category_id not in _SOURCES:
        raise ValueError("该韵达来源仅支持打开原系统。")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("页码必须为正整数。")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 20:
        raise ValueError("每页条数必须为 1 至 20。")
    category = _category(category_id)
    result = {
        "title": category["title"], "items": [], "total": None, "page": page,
        "page_size": page_size, "has_more": False, "open_url": category["open_url"],
        "status": "ok", "status_label": "已连接", "message": category.get("message", ""),
    }
    try:
        if category_id in _READERS:
            items = [_ITEMS[category_id](row) for row in _READERS[category_id](session)]
            items.sort(key=lambda item: item["occurred_at"], reverse=True)
            total = len(items)
            items = items[(page - 1) * page_size:page * page_size]
        else:
            time_key = "compTime" if category_id == "yunda:work_orders" else "colTime"
            rows = sorted(_recent_cssp_rows(session, category_id), key=lambda row: _text(row.get(time_key)), reverse=True)
            total = len(rows)
            rows = rows[(page - 1) * page_size:page * page_size]
            if category_id == "yunda:work_order_replies":
                items = [_reply_item(row) for row in rows]
            else:
                # Bounded read-only requests, without mutating session config.
                # At most 20 details / 4 workers, each with an 8-second timeout.
                with ThreadPoolExecutor(max_workers=4, thread_name_prefix="yunda-monitor-detail") as executor:
                    items = list(executor.map(lambda row: _work_order_item(session, row), rows))
            items.sort(key=lambda item: item["occurred_at"], reverse=True)
        ids = [item["id"] for item in items]
        if len(ids) != len(set(ids)):
            raise SourceError("韵达本页存在重复记录，无法确认消息数量。")
        result.update(items=items, total=total, has_more=page * page_size < total)
    except Exception as exc:
        result.update(_failure(exc))
    return result
