"""Read-only Ronghui notice and received work-order monitoring."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.scripts.customer_service_problem import (
    _resolve_ronghui_page_context,
    _ronghui_headers,
)

ORIGIN = "https://tms.ronghuiwl.com"
HOME = f"{ORIGIN}/module/index?mv=index"
WORK_MENU = "工单接收方查询-工单裁定申诉"
SOURCES = {
    "ronghui:announcements": ("未读公告与消息", "公告查询", ""),
    "ronghui:work_response": ("收到工单 · 待响应", WORK_MENU, "2"),
    "ronghui:work_processing": ("收到工单 · 待处理", WORK_MENU, "3"),
}
STATE_LABELS = {"2": "待责任方响应", "3": "待责任方处理"}


class RonghuiMonitoringError(ValueError):
    """A source could not be read or its contract could not be established."""


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: Any) -> str:
    parser = _PlainText()
    parser.feed(str(value or ""))
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def _read(session, context, call_id, data, *, paged=False, timeout=20):
    try:
        response = session.post(
            f"{ORIGIN}/dataQuery/{'findPageByCallId' if paged else 'findAllByCallId'}",
            params={"id": call_id}, data=data,
            headers=_ronghui_headers(context), timeout=timeout, allow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉消息或工单登录态已失效，请重新登录。")
        if response.status_code != 200:
            raise RonghuiMonitoringError("融辉消息或工单接口请求失败。")
        try:
            payload = response.json()
        except ValueError:
            body = str(getattr(response, "text", "")).lower()
            if any(marker in body for marker in ("/login", "用户登录", "<title>登录", "session error")):
                raise TMSAuthStateError("AUTH_REQUIRED", "融辉消息或工单登录态已失效，请重新登录。") from None
            raise RonghuiMonitoringError("融辉消息或工单接口未返回有效数据。") from None
    except (TMSAuthStateError, RonghuiMonitoringError):
        raise
    except Exception:
        raise RonghuiMonitoringError("融辉消息或工单接口未返回有效数据。") from None
    rows = payload.get("data") if paged and isinstance(payload, dict) else payload
    if isinstance(payload, dict) and (payload.get("success") is False or payload.get("error")):
        raise RonghuiMonitoringError("融辉消息或工单业务响应失败。")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RonghuiMonitoringError("融辉消息或工单响应结构变化，无法确认数量。")
    total = payload.get("total") if paged else len(rows)
    if isinstance(total, bool) or not isinstance(total, int) or total < len(rows):
        raise RonghuiMonitoringError("融辉消息或工单总数缺失或与列表不符。")
    if paged and (len(rows) > data["pageSize"] or (not rows and data["pageIndex"] * data["pageSize"] < total)):
        raise RonghuiMonitoringError("融辉消息或工单分页数据与总数不符。")
    return rows, total


def _context(session, menu):
    try:
        return _resolve_ronghui_page_context(session, menu)
    except TMSAuthStateError:
        raise
    except Exception:
        raise RonghuiMonitoringError(f"无法取得融辉“{menu}”页面，请检查账号登录及菜单权限。") from None


def _query(session, context, state, page, page_size):
    if not state:
        return _read(session, context, "FIND_TAB_NOTICE_NOT_SEE", {})
    return _read(session, context, "FIND_TAB_WORK_ORDER", {
        "ZRF": 1, "IS_DEL1": 1, "WORK_STATE": state,
        "ORDER_BY_CREATE_DATE": "ORDER_BY_DATE",
        "pageIndex": page - 1, "pageSize": page_size,
    }, paged=True)


def collect_categories(session) -> list[dict[str, Any]]:
    categories = []
    contexts = {}
    for category_id, (title, menu, state) in SOURCES.items():
        category = {
            "system": "ronghui", "category_id": category_id, "title": title,
            "items_supported": True, "detail_supported": True, "open_url": HOME,
            "is_exception": bool(state), "tone": "warning" if state else "info",
            "include_in_total": True, "count_scope": "received_work_orders" if state else "unread_announcements",
        }
        try:
            if menu not in contexts:
                contexts[menu] = _context(session, menu)
            rows, total = _query(session, contexts[menu], state, 1, 1)
            if state and any(row.get("WORK_STATE") != STATE_LABELS[state] for row in rows):
                raise RonghuiMonitoringError("融辉工单筛选状态不符，无法确认待办数量。")
            category.update(count=total, status="ok", status_label="待处理" if total else "暂无待办", message="")
        except Exception as exc:
            category.update(count=None, status="auth_required" if isinstance(exc, TMSAuthStateError) else "error",
                            status_label="登录失效" if isinstance(exc, TMSAuthStateError) else "读取失败",
                            message=str(exc) if isinstance(exc, (TMSAuthStateError, RonghuiMonitoringError)) else "融辉来源读取失败。")
        categories.append(category)
    return categories


def collect_items(session, category_id: str, page: int, page_size: int = 20) -> dict[str, Any]:
    if category_id not in SOURCES:
        raise RonghuiMonitoringError("不支持的融辉消息分类。")
    if not 1 <= page <= 10000 or not 1 <= page_size <= 20:
        raise RonghuiMonitoringError("消息页码或每页数量无效。")
    title, menu, state = SOURCES[category_id]
    context = _context(session, menu)
    rows, total = _query(session, context, state, page, page_size)
    if not state:
        rows = sorted(rows, key=lambda r: str(r.get("NOTICE_DATETIME") or ""), reverse=True)
        rows = rows[(page - 1) * page_size:page * page_size]
    items = []
    seen = set()
    for row in rows:
        identifier = str(row.get("WORK_ORDER_CODE" if state else "GUID") or "").strip()
        content_key = "WORK_CONTENT" if state else "CONTENT"
        if not identifier or content_key not in row:
            raise RonghuiMonitoringError("融辉消息或工单缺少编号/正文字段，请检查原系统。")
        if identifier in seen:
            raise RonghuiMonitoringError("融辉列表出现重复编号，无法确认分页数量。")
        seen.add(identifier)
        if state and row.get("WORK_STATE") != STATE_LABELS[state]:
            raise RonghuiMonitoringError("融辉返回了非待处理工单，已停止展示该查询结果。")
        content = plain_text(row.get(content_key))
        occurred_at = str(row.get("REGISTER_DATE" if state else "NOTICE_DATETIME") or "")
        if state:
            deadline = row.get("FIRST_RESPONSE_END_DATE" if state == "2" else "OVER_END_TIME")
            if deadline:
                content += f"\n{'响应' if state == '2' else '完结'}截止：{deadline}"
        else:
            content = "\n".join(filter(None, [content, plain_text(row.get("CONTENT1")), plain_text(row.get("CONTENT2"))]))
        items.append({
            "id": identifier, "title": str(row.get("WORK_NAME" if state else "TITLE") or title),
            "content": content or "原系统正文为空", "occurred_at": occurred_at,
            "status_label": STATE_LABELS[state] if state else "未读",
            "waybill_no": str(row.get("BILL_CODE") or ""),
            "source_label": f"融辉 · {title}", "open_url": HOME,
        })
    partial = False
    if state and items:
        # Only bounded read requests run concurrently; account/session settings
        # stay unchanged. Each item can retain its verified body on a failure.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ronghui-history") as executor:
            results = list(executor.map(lambda item: _append_history(session, context, item), items))
        partial = not all(results)
    items.sort(key=lambda item: item["occurred_at"], reverse=True)
    return {
        "title": title, "items": items, "total": total, "page": page, "page_size": page_size,
        "has_more": page * page_size < total, "open_url": HOME,
        "status": "partial" if partial else "ok", "status_label": "部分读取失败" if partial else "已同步",
        "message": "部分处理记录读取失败，请核对原系统。" if partial else (
            "按接收方查询所有日期的未完结工单；按原系统登记时间分页，页内按最新处理时间排序。原系统未提供回复未读标识。" if state else ""
        ),
    }


def _append_history(session, context, item: dict[str, Any]) -> bool:
    try:
        history, total = _read(session, context, "FIND_TAB_WORK_ORDER_DETAILS", {
            "WORK_ORDER_CODE": item["id"], "pageIndex": 0, "pageSize": 20,
            "sortField": "HANDLE_DATE", "sortOrder": "desc",
        }, paged=True, timeout=8)
        if any(str(entry.get("WORK_ORDER_CODE") or "").strip() != item["id"]
               or "HANDLE_CONTENT" not in entry or not entry.get("HANDLE_DATE") for entry in history):
            raise RonghuiMonitoringError("工单处理记录字段缺失或所属编号不符。")
        if history:
            history.sort(key=lambda entry: str(entry["HANDLE_DATE"]), reverse=True)
            item["content"] += "\n\n处理记录（原系统未提供未读标识）：\n" + "\n".join(
                f"{entry['HANDLE_DATE']} {entry.get('HANDLE_STATE', '')}：{plain_text(entry.get('HANDLE_CONTENT'))}"
                for entry in history
            )
            item["occurred_at"] = max(item["occurred_at"], str(history[0]["HANDLE_DATE"]))
        if total > len(history):
            item["content"] += "\n仅展示最新20条处理记录，完整历史请打开原系统。"
        return True
    except Exception:
        item["content"] += "\n处理记录暂时读取失败，请打开原系统核对最新回复。"
        return False
