"""Read-only adapters for the verified Yunda message and work-order pages."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import requests

from agent.tms_runtime.errors import TMSAuthStateError


CLIENT_ORIGIN = "https://ky-client.yunda56.com"
EXPORT_ORIGIN = "https://ky-export.yunda56.com"
MESSAGE_PAGE = f"{CLIENT_ORIGIN}/#/ifarme/ifarme/21144/{quote('待办事项')}"
EXPORT_PAGE = f"{EXPORT_ORIGIN}/ydky_rabbit_web/ui/view/common/kyimport_message.html"
MESSAGE_API = f"{EXPORT_ORIGIN}/ydky_rabbit_web/actions/message"
CSSP_ORIGIN = "https://cssp.yunda56.com"
WORK_ORDER_PAGE = f"{CSSP_ORIGIN}/cssp/vue/index.html#/work-order/deal"
_SOURCES = {
    "yunda:notifications": ("通知待处理", "pending_notifications", MESSAGE_PAGE),
    "yunda:announcements": ("未读公告", "unread_announcements", MESSAGE_PAGE),
    "yunda:work_orders": ("收到的待处理工单", "work_orders", WORK_ORDER_PAGE),
    "yunda:work_order_replies": ("工单催单待回复", "work_order_replies", WORK_ORDER_PAGE),
}
ITEM_CATEGORY_IDS = frozenset(_SOURCES)


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
    """Never expose request exceptions, which may include an ssid query value."""
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


def _username(session: Any) -> str:
    payload = _request(session, "get", f"{CLIENT_ORIGIN}/client/user/info")
    if not isinstance(payload, dict) or payload.get("code") not in {200, "200"}:
        raise SourceError("韵达用户信息接口业务响应失败。")
    details = payload.get("details")
    if not isinstance(details, dict) or not _text(details.get("username")):
        raise SourceError("韵达用户信息缺少 details.username。")
    return _text(details["username"])


def _export_params(session: Any, action: str) -> dict[str, str]:
    # Match the original page's beforeSend cookie-to-query transport. Values
    # stay inside the request and must never be projected or logged.
    cookies = getattr(session, "cookies", ())
    values = {
        cookie.value
        for cookie in cookies
        if cookie.name == "cip_sessionid"
        and (cookie.domain.lstrip(".") == "yunda56.com" or cookie.domain.lstrip(".") == "ky-export.yunda56.com")
    }
    if len(values) != 1:
        raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息中心会话未就绪，请重新登录。")
    return {"actionId": action, "ssid": next(iter(values))}


def _export(session: Any, endpoint: str, action: str, data: dict[str, Any]) -> dict:
    payload = _request(
        session, "post", f"{MESSAGE_API}/{endpoint}.do",
        params=_export_params(session, action), data=data,
        headers={"Referer": EXPORT_PAGE, "Origin": EXPORT_ORIGIN, "X-Requested-With": "XMLHttpRequest"},
    )
    if not isinstance(payload, dict) or payload.get("errorCode") not in {0, "0"}:
        raise SourceError("韵达消息中心业务响应失败。")
    return payload


def _category(category_id: str, count: int | None = None) -> dict[str, Any]:
    title, count_scope, open_url = _SOURCES[category_id]
    return {
        "system": "yunda", "category_id": category_id, "title": title, "count": count,
        "status": "pending" if count else "empty", "status_label": "待处理" if count else "无待办",
        "items_supported": True, "detail_supported": True, "open_url": open_url,
        "is_exception": category_id in {"yunda:work_orders", "yunda:work_order_replies"},
        "tone": "warning" if count else "neutral", "count_scope": count_scope,
        "include_in_total": False,
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
        raise SourceError("韵达工单列表业务响应失败。")
    if any(not isinstance(row, dict) for row in payload["rows"]):
        raise SourceError("韵达列表记录结构异常。")
    total = _count(payload.get("total"), "total")
    if len(payload["rows"]) > total:
        raise SourceError("韵达列表记录数超过来源总数。")
    return payload["rows"], total


def _message_rows(session: Any, category_id: str, page: int, page_size: int, username: str) -> tuple[list[dict], int]:
    announcement = category_id == "yunda:announcements"
    condition = {"user": username, "readStatus" if announcement else "handleStatus": "0", "showModel": "0" if announcement else "1"}
    payload = _export(
        session,
        "getNoticeReadByStatus" if announcement else "getAdviseReadByStatus",
        "kyimport_message_notice_info_urlSearchData" if announcement else "kyimport_message_notice_info_urlSearchAdviseData",
        {"search_condition": json.dumps(condition, ensure_ascii=False), "page": page, "rows": page_size},
    )
    return _rows(payload)


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


def _type_categories(session: Any, username: str) -> list[dict[str, Any]]:
    payload = _export(session, "getTypes", "kyimport_message_notice_info_getTypes", {"userId": username, "sourceFlag": "down"})
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise SourceError("韵达待办分类结构变化：缺少 rows。")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in rows:
        if not isinstance(row, dict) or not _text(row.get("name")) or not _text(row.get("typeCode")):
            raise SourceError("韵达待办分类缺少名称或类型。")
        type_code = _text(row["typeCode"])
        category_id = "yunda:" + type_code
        if category_id in seen:
            raise SourceError("韵达待办分类类型重复。")
        seen.add(category_id)
        count = _count(row.get("sumValue"), "sumValue")
        title = _text(row["name"])
        resource_id = _text(row.get("newClientResourceId"))
        target_title = _text(row.get("noticeSystemName")) or title
        open_url = f"{CLIENT_ORIGIN}/#/ifarme/ifarme/{resource_id}/{quote(target_title, safe='')}" if resource_id.isdigit() else MESSAGE_PAGE
        result.append({
            "system": "yunda", "category_id": category_id, "title": title, "count": count,
            "status": "pending" if count else "empty", "status_label": "待处理" if count else "无待办",
            "items_supported": False, "detail_supported": True, "open_url": open_url,
            "is_exception": "问题" in title or "工单" in title,
            "tone": "warning" if count else "neutral", "type_code": type_code,
            "resource_id": resource_id if resource_id.isdigit() else "", "target_title": target_title,
            "count_scope": "message_center", "include_in_total": False,
        })
        # The page renders only top-level rows. chidList repeats the parent's
        # typeCode and uses another shape; projecting it double-counts a type.
    return result


def collect_categories(session: Any) -> list[dict[str, Any]]:
    """Collect each source independently; unknown amounts are never zeroed."""
    result: list[dict[str, Any]] = []
    username = ""
    identity_error: Exception | None = None
    cssp_user_id: str | None = None
    cssp_error: Exception | None = None
    try:
        username = _username(session)
        result.extend(_type_categories(session, username))
    except Exception as exc:
        # Username failures also block lists; category failures alone do not.
        if not username:
            identity_error = exc
        result.append({
            "system": "yunda", "category_id": "yunda:message_center", "title": "原系统待办分类",
            "count": None, "items_supported": False, "detail_supported": True,
            "open_url": MESSAGE_PAGE, "is_exception": False, "tone": "warning",
            "include_in_total": False, "count_scope": "message_center", **_failure(exc),
        })
    for category_id in _SOURCES:
        category = _category(category_id)
        try:
            if category_id in {"yunda:notifications", "yunda:announcements"}:
                if identity_error:
                    raise identity_error
                _, total = _message_rows(session, category_id, 1, 20, username)
            else:
                if cssp_error:
                    raise cssp_error
                if cssp_user_id is None:
                    try:
                        cssp_user_id = _cssp_user_id(session)
                    except Exception as exc:
                        cssp_error = exc
                        raise
                _, total = _work_order_rows(session, category_id, 1, 20, user_id=cssp_user_id)
            category = _category(category_id, total)
        except Exception as exc:
            category.update(_failure(exc), count=None, tone="warning")
        result.append(category)
    return result


def _message_item(row: dict, category_id: str) -> dict[str, Any]:
    if not _text(row.get("msgId")) or "msgContent" not in row:
        raise SourceError("韵达消息记录缺少 msgId 或 msgContent。")
    return {
        "id": _text(row["msgId"]), "title": _content(row.get("msgTitle")) or _SOURCES[category_id][0],
        "content": _content(row["msgContent"]), "occurred_at": _text(row.get("createdTime")),
        "status_label": (_text(row.get("readStatus")) or "状态未确认") if category_id == "yunda:announcements" else ("待处理" if row.get("handleStatus") == "0" else "状态未确认"),
        "waybill_no": _text(row.get("logisticsId")), "source_label": _text(row.get("typeName")) or "韵达消息中心",
        "open_url": MESSAGE_PAGE,
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
        "status": "ok", "status_label": "已连接", "message": "",
    }
    try:
        if category_id in {"yunda:notifications", "yunda:announcements"}:
            rows, total = _message_rows(session, category_id, page, page_size, _username(session))
            if len(rows) > page_size:
                raise SourceError("韵达消息接口未遵守分页限制。")
            items = [_message_item(row, category_id) for row in rows]
        else:
            rows, total = _work_order_rows(session, category_id, page, page_size)
            result["total"] = total
            if len(rows) > page_size:
                raise SourceError("韵达工单接口未遵守分页限制。")
            if category_id == "yunda:work_order_replies":
                items = [_reply_item(row) for row in rows]
            else:
                # Bounded read-only requests, without mutating session config.
                # At most 20 details / 4 workers, each with an 8-second timeout.
                with ThreadPoolExecutor(max_workers=4, thread_name_prefix="yunda-monitor-detail") as executor:
                    items = list(executor.map(lambda row: _work_order_item(session, row), rows))
        ids = [item["id"] for item in items]
        if len(ids) != len(set(ids)):
            raise SourceError("韵达本页存在重复记录，无法确认消息数量。")
        if not items and (page - 1) * page_size < total:
            raise SourceError("韵达来源总数非零但本页没有记录，请重新刷新。")
        items.sort(key=lambda item: item["occurred_at"], reverse=True)
        result.update(items=items, total=total, has_more=page * page_size < total)
    except Exception as exc:
        result.update(_failure(exc))
    return result
