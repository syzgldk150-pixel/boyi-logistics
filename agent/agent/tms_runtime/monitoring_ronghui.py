"""Read-only Ronghui notice and received work-order monitoring."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.monitoring_window import RECENT_NOTE, is_recent, latest_activity
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
FINISHED_STATE = "已完结"
# 用户确认（2026-11-18）：只有这四个网点为责任方的工单属于博益。
OWN_RESPONSIBLE_PARTIES = frozenset({"邵阳大祥站", "邵阳大祥S站", "邵阳操作场", "邵阳自提部"})
WORK_FETCH_PAGE_SIZE = 50
MAX_WORK_ORDERS = 1000
WORK_SCOPE_NOTE = "只显示责任方为邵阳大祥站、邵阳大祥S站、邵阳操作场、邵阳自提部的未完结工单。"


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


def _own_work_orders(session, context, state) -> list[dict[str, Any]]:
    """Read every received order in one state, then keep only Boyi's sites."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        page_rows, total = _query(session, context, state, page, WORK_FETCH_PAGE_SIZE)
        if total > MAX_WORK_ORDERS:
            raise RonghuiMonitoringError("融辉收到工单数量过多，请打开原系统核对。")
        rows.extend(page_rows)
        if page * WORK_FETCH_PAGE_SIZE >= total:
            break
        page += 1
    own: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("WORK_ORDER_CODE") or "").strip()
        if not code or "WORK_CONTENT" not in row or "RESPONSIBLE_PARTY" not in row:
            raise RonghuiMonitoringError("融辉工单缺少编号、正文或责任方字段，请检查原系统。")
        if code in seen:
            raise RonghuiMonitoringError("融辉列表出现重复编号，无法确认分页数量。")
        seen.add(code)
        work_state = row.get("WORK_STATE")
        if work_state == FINISHED_STATE:
            continue
        if work_state != STATE_LABELS[state]:
            raise RonghuiMonitoringError("融辉返回了非待处理工单，已停止展示该查询结果。")
        if str(row.get("RESPONSIBLE_PARTY") or "").strip() in OWN_RESPONSIBLE_PARTIES:
            own.append(row)
    return own


def _read_history(session, context, code: str) -> tuple[list[dict[str, Any]], int]:
    history, total = _read(session, context, "FIND_TAB_WORK_ORDER_DETAILS", {
        "WORK_ORDER_CODE": code, "pageIndex": 0, "pageSize": 20,
        "sortField": "HANDLE_DATE", "sortOrder": "desc",
    }, paged=True, timeout=8)
    if any(str(entry.get("WORK_ORDER_CODE") or "").strip() != code
           or "HANDLE_CONTENT" not in entry or not entry.get("HANDLE_DATE") for entry in history):
        raise RonghuiMonitoringError("工单处理记录字段缺失或所属编号不符。")
    history.sort(key=lambda entry: str(entry["HANDLE_DATE"]), reverse=True)
    return history, total


def _recent_work_orders(session, context, state) -> list[tuple[dict[str, Any], tuple[list, int] | None]]:
    """Boyi's open orders whose registration or latest handling record is recent.

    A failed history read keeps the order visible (its recency cannot be disproved)."""
    rows = _own_work_orders(session, context, state)
    if not rows:
        return []

    def load(row):
        try:
            return _read_history(session, context, str(row["WORK_ORDER_CODE"]).strip())
        except Exception:
            return None

    # Only bounded read requests run concurrently; account/session settings stay unchanged.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ronghui-history") as executor:
        histories = list(executor.map(load, rows))
    result = []
    for row, history in zip(rows, histories):
        times = [row.get("REGISTER_DATE")] + ([entry["HANDLE_DATE"] for entry in history[0]] if history else [])
        if history is None or is_recent(times):
            result.append((row, history))
    return result


def _recent_announcements(session, context) -> list[dict[str, Any]]:
    rows, _ = _query(session, context, "", 1, 1)
    return [row for row in rows if is_recent([row.get("NOTICE_DATETIME")])]


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
            if state:
                total = len(_recent_work_orders(session, contexts[menu], state))
            else:
                total = len(_recent_announcements(session, contexts[menu]))
            category.update(count=total, status="ok", message=RECENT_NOTE + (WORK_SCOPE_NOTE if state else ""),
                            status_label=(STATE_LABELS[state] if state else "未读") if total else "暂无待办")
        except Exception as exc:
            category.update(count=None, status="auth_required" if isinstance(exc, TMSAuthStateError) else "error",
                            status_label="登录失效" if isinstance(exc, TMSAuthStateError) else "读取失败",
                            message=str(exc) if isinstance(exc, (TMSAuthStateError, RonghuiMonitoringError)) else "融辉来源读取失败。")
        categories.append(category)
    return categories


def _work_order_item(row: dict[str, Any], history: tuple[list, int] | None, state: str, title: str) -> dict[str, Any]:
    content = plain_text(row.get("WORK_CONTENT"))
    deadline = row.get("FIRST_RESPONSE_END_DATE" if state == "2" else "OVER_END_TIME")
    if deadline:
        content += f"\n{'响应' if state == '2' else '完结'}截止：{deadline}"
    occurred_at = str(row.get("REGISTER_DATE") or "")
    if history is None:
        content += "\n处理记录暂时读取失败，请打开原系统核对最新回复。"
    else:
        entries, total = history
        if entries:
            content += "\n\n处理记录（原系统未提供未读标识）：\n" + "\n".join(
                f"{entry['HANDLE_DATE']} {entry.get('HANDLE_STATE', '')}：{plain_text(entry.get('HANDLE_CONTENT'))}"
                for entry in entries
            )
            occurred_at = latest_activity([occurred_at, entries[0]["HANDLE_DATE"]]) or occurred_at
        if total > len(entries):
            content += "\n仅展示最新20条处理记录，完整历史请打开原系统。"
    return {
        "id": str(row["WORK_ORDER_CODE"]).strip(), "title": str(row.get("WORK_NAME") or title),
        "content": content or "原系统正文为空", "occurred_at": occurred_at,
        "status_label": STATE_LABELS[state], "waybill_no": str(row.get("BILL_CODE") or ""),
        "source_label": f"融辉 · {title}", "open_url": HOME,
    }


def _announcement_item(row: dict[str, Any], title: str) -> dict[str, Any]:
    identifier = str(row.get("GUID") or "").strip()
    if not identifier:
        raise RonghuiMonitoringError("融辉消息缺少编号字段，请检查原系统。")
    content = "\n".join(filter(None, [plain_text(row.get("CONTENT")), plain_text(row.get("CONTENT1")),
                                      plain_text(row.get("CONTENT2"))]))
    if not content:
        # 实测 2026-11-18：“问题件”类通知不返回 CONTENT 列，只有类型、发送网点、发送人和运单号。
        summary = (("类型", "NOTICE_NAME_TEXT"), ("发送网点", "SEND_SITE"), ("发送人", "PERSON"), ("运单号", "BILL_CODE"))
        content = "\n".join(f"{label}：{plain_text(row.get(key))}" for label, key in summary if plain_text(row.get(key)))
    return {
        "id": identifier, "title": str(row.get("TITLE") or title), "content": content or "原系统正文为空",
        "occurred_at": str(row.get("NOTICE_DATETIME") or ""), "status_label": "未读",
        "waybill_no": str(row.get("BILL_CODE") or ""), "source_label": f"融辉 · {title}", "open_url": HOME,
    }


def collect_items(session, category_id: str, page: int, page_size: int = 20) -> dict[str, Any]:
    if category_id not in SOURCES:
        raise RonghuiMonitoringError("不支持的融辉消息分类。")
    if not 1 <= page <= 10000 or not 1 <= page_size <= 20:
        raise RonghuiMonitoringError("消息页码或每页数量无效。")
    title, menu, state = SOURCES[category_id]
    context = _context(session, menu)
    partial = False
    if state:
        pairs = _recent_work_orders(session, context, state)
        partial = any(history is None for _, history in pairs)
        items = [_work_order_item(row, history, state, title) for row, history in pairs]
    else:
        items = [_announcement_item(row, title) for row in _recent_announcements(session, context)]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise RonghuiMonitoringError("融辉列表出现重复编号，无法确认分页数量。")
    items.sort(key=lambda item: item["occurred_at"], reverse=True)
    total = len(items)
    items = items[(page - 1) * page_size:page * page_size]
    return {
        "title": title, "items": items, "total": total, "page": page, "page_size": page_size,
        "has_more": page * page_size < total, "open_url": HOME,
        "status": "partial" if partial else "ok", "status_label": "部分读取失败" if partial else "已同步",
        "message": "部分处理记录读取失败，已保留显示，请核对原系统。" if partial else (
            RECENT_NOTE + (WORK_SCOPE_NOTE + "按最新动态时间排序。原系统未提供回复未读标识。" if state else "")
        ),
    }
