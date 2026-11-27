"""Read-only Ronghui sources for the home message center (four Boyi accounts).

用户确认（2026-11-27）：融辉读取邵阳操作场、邵阳大祥站、邵阳大祥S站、邵阳自提部四个账号的
公告、收到工单、收到问题件、投诉（被投诉方、投诉方）、申诉与代取件。操作场账号（分拨中心）能看到
下属网点的数据，所以它读到的每一类都只保留责任方/发起方（被投诉方/投诉方/申诉方）是邵阳操作场的
记录；其他三个账号按原页本网点返回的结果。同一记录被多个账号读到只算一次。

页面与接口均为 2026-11-27 实测（只读）：
* 公告：“公告查询” ``FIND_TAB_NOTICE_NOT_SEE``；
* 收到工单：“工单接收方查询-工单裁定申诉” ``FIND_TAB_WORK_ORDER``（ZRF=1，WORK_STATE 2/3），处理记录
  ``FIND_TAB_WORK_ORDER_DETAILS``；发起方为 ``REGISTER_SITE``；
* 收到问题件：复用客服问题件查询（“收到问题件查询”），我方还没回复的才显示；
* 被投诉方：“被投诉方查询” ``FIND_TAB_EXCEPTION_SIDE``，必须带 ``CURRENT_LOGIN_SITE``（原页
  ``getSearchParams``）；投诉方：“投诉方登记” ``FIND_TAB_EXCEPTION_REGISTER_CS``；按投诉时间
  ``EXCEPTION_DATE`` 查最近 31 天（不带日期原页报错）；投诉状态 ``STATUS`` 为“待审核”或仲裁状态
  ``ARBITRATE_STATUS`` 为“待审核/待复审”算未完结；
* 申诉：“申诉登记” ``FIND_TAB_EXCEPTION_APPEAL``，申诉状态 ``COMPLAINT_STATUS`` 为“待审核/待处理”；
* 代取件：“网点代取件业务” ``FIND_TAB_UNITE_ORDERS_DQJ``（只有大祥、大祥S账号有此页），订单状态
  ``STATUS`` 0 待分配 / 4 待接单 / 1 待揽收 算未完结。
"""

from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Iterable

from agent.tms_runtime import monitoring_window
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.monitoring_window import RECENT_NOTE, is_recent, latest_activity
from agent.tms_runtime.scripts.customer_service_problem import (
    CustomerServiceProblemError,
    _has_problem_reply,
    _read_user_info_cookie,
    _resolve_ronghui_page_context,
    _ronghui_headers,
    _ronghui_query,
)
from agent.tms_runtime.session_validators import looks_like_ronghui_login

ORIGIN = "https://tms.ronghuiwl.com"
HOME = f"{ORIGIN}/module/index?mv=index"
WORK_MENU = "工单接收方查询-工单裁定申诉"
CENTER_SITE = "邵阳操作场"
# (session profile, 显示名)：四个博益融辉账号，顺序即合并时的优先顺序。
ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("default", "操作场账号"),
    ("price_default", "大祥站账号"),
    ("daxiang_s", "大祥S站账号"),
    ("self_pickup_problem_upload", "自提部账号"),
)


@dataclass(frozen=True)
class Source:
    title: str
    menu: str
    kind: str
    state: str = ""
    call_id: str = ""
    optional_menu: bool = False  # 只有部分账号有这个页面：没有的账号跳过，不算失败
    is_exception: bool = True


SOURCES: dict[str, Source] = {
    "ronghui:work_response": Source("收到工单 · 待响应", WORK_MENU, "work", state="2"),
    "ronghui:work_processing": Source("收到工单 · 待处理", WORK_MENU, "work", state="3"),
    "ronghui:problems": Source("问题件 · 未回复", "收到问题件查询", "problem"),
    "ronghui:complaints_against": Source("被投诉 · 未完结", "被投诉方查询", "complaint", call_id="FIND_TAB_EXCEPTION_SIDE"),
    "ronghui:complaints_filed": Source("投诉 · 未完结", "投诉方登记", "complaint", call_id="FIND_TAB_EXCEPTION_REGISTER_CS"),
    "ronghui:appeals": Source("申诉 · 未完结", "申诉登记", "appeal", call_id="FIND_TAB_EXCEPTION_APPEAL"),
    "ronghui:pickups": Source("代取件 · 未揽收", "网点代取件业务", "pickup", call_id="FIND_TAB_UNITE_ORDERS_DQJ",
                              optional_menu=True),
    "ronghui:announcements": Source("未读公告与消息", "公告查询", "notice", is_exception=False),
}
ITEM_CATEGORY_IDS = frozenset(SOURCES)
STATE_LABELS = {"2": "待责任方响应", "3": "待责任方处理"}
FINISHED_STATE = "已完结"
COMPLAINT_OPEN_STATUS = frozenset({"待审核"})
ARBITRATION_OPEN_STATUS = frozenset({"待审核", "待复审"})
APPEAL_OPEN_STATUS = frozenset({"待审核", "待处理"})
PICKUP_OPEN_STATUS = {"0": "待分配", "4": "待接单", "1": "待揽收"}
PICKUP_STATUS_TEXT = {**PICKUP_OPEN_STATUS, "2": "已揽收", "3": "已取消"}
COMPLAINT_LOOKBACK_DAYS = 31  # 原页按投诉时间查询，不带日期会报错
FETCH_PAGE_SIZE = 200
MAX_ROWS = 2000
WORK_FETCH_PAGE_SIZE = 50
MAX_WORK_ORDERS = 1000
CENTER_NOTE = "操作场账号只显示责任方或发起方是邵阳操作场的记录。"
COMPLAINT_DATES = ("EXCEPTION_DATE", "REGISTER_DATE", "MODIFY_DATE", "AUDIT_DATE", "FIRST_AUDIT_DATE",
                   "ARBITRATION_CHECK_DATE", "ARBITRATION_DISPOSE_DATE", "ARBITRATION_REPLY_DATE", "APPEAL_DATE",
                   "CLAIM_DATE", "CANCEL_DATE")
APPEAL_DATES = ("CREATE_DATE", "REGISTER_DATE", "MODIFY_DATE", "AUDIT_DATE", "DEAL_DATE", "FIRST_AUDIT_DATE",
                "REPEAT_AUDIT_DATE")
PICKUP_DATES = ("CREATE_DATE", "UPDATE_DATE")


class RonghuiMonitoringError(ValueError):
    """A source could not be read or its contract could not be established."""


class MenuMissing(RonghuiMonitoringError):
    """This account's menu has no such page."""


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


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass
class Account:
    profile: str
    label: str
    session: Any
    site: str = ""
    site_code: str = ""
    site_type: str = ""
    contexts: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> bool:
        return self.site == CENTER_SITE


def _read(session, context, call_id, data, *, paged=False, timeout=20):
    try:
        response = session.post(
            f"{ORIGIN}/dataQuery/{'findPageByCallId' if paged else 'findAllByCallId'}",
            params={"id": call_id}, data=data,
            headers=_ronghui_headers(context), timeout=timeout, allow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。")
        if response.status_code != 200:
            raise RonghuiMonitoringError("融辉接口请求失败。")
        try:
            payload = response.json()
        except ValueError:
            body = str(getattr(response, "text", "")).lower()
            if any(marker in body for marker in ("/login", "用户登录", "<title>登录", "session error")):
                raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。") from None
            raise RonghuiMonitoringError("融辉接口未返回有效数据。") from None
    except (TMSAuthStateError, RonghuiMonitoringError):
        raise
    except Exception:
        raise RonghuiMonitoringError("融辉接口未返回有效数据。") from None
    rows = payload.get("data") if paged and isinstance(payload, dict) else payload
    if isinstance(payload, dict) and (payload.get("success") is False or payload.get("error")):
        raise RonghuiMonitoringError("融辉接口业务响应失败。")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RonghuiMonitoringError("融辉接口响应结构变化，无法确认数量。")
    total = payload.get("total") if paged else len(rows)
    if isinstance(total, bool) or not isinstance(total, int) or total < len(rows):
        raise RonghuiMonitoringError("融辉列表总数缺失或与列表不符。")
    if paged and (len(rows) > data["pageSize"] or (not rows and data["pageIndex"] * data["pageSize"] < total)):
        raise RonghuiMonitoringError("融辉列表分页数据与总数不符。")
    return rows, total


def _read_all(session, context, call_id, data, *, limit=MAX_ROWS, label="融辉列表") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 0
    while True:
        page_rows, total = _read(session, context, call_id, {**data, "pageIndex": page, "pageSize": FETCH_PAGE_SIZE},
                                 paged=True, timeout=30)
        if total > limit:
            raise RonghuiMonitoringError(f"{label}数量过多，请打开原系统核对。")
        rows.extend(page_rows)
        page += 1
        if page * FETCH_PAGE_SIZE >= total:
            return rows


def _home_requires_login(session) -> bool:
    """Whether the Ronghui home page sends this session to the login page."""
    try:
        response = session.get(HOME, timeout=20, allow_redirects=False)
    except TMSAuthStateError:
        return True
    except Exception:
        return False
    return looks_like_ronghui_login(response)


def _context(session, menu):
    try:
        return _resolve_ronghui_page_context(session, menu)
    except TMSAuthStateError:
        raise
    except Exception as exc:
        # 实测 2026-11-20：登录失效时菜单接口返回 200 空内容，首页才跳登录页；以首页结果区分登录失效与菜单缺失。
        if isinstance(exc, CustomerServiceProblemError) and exc.code == "INVALID_RESPONSE" and _home_requires_login(session):
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请到业务账号重新登录。") from None
        if isinstance(exc, CustomerServiceProblemError) and exc.code == "PAGE_CONTEXT_NOT_FOUND":
            raise MenuMissing(f"账号菜单里没有融辉“{menu}”页面。") from None
        raise RonghuiMonitoringError(f"无法取得融辉“{menu}”页面，请检查账号登录及菜单权限。") from None


def _account_context(account: Account, menu: str):
    if menu not in account.contexts:
        account.contexts[menu] = _context(account.session, menu)
    return account.contexts[menu]


def _open_account(profile: str, label: str, open_session: Callable[[str], Any]) -> Account:
    session = open_session(profile)
    info = _read_user_info_cookie(session) or {}
    account = Account(profile, label, session, site=_text(info.get("loginSiteName")),
                      site_code=_text(info.get("loginSiteCode")), site_type=_text(info.get("loginSiteType")))
    if not account.site or not account.site_code:
        raise TMSAuthStateError("AUTH_REQUIRED", "融辉账号登录信息里没有网点，请到业务账号重新登录。")
    return account


def _window_range(days: int) -> str:
    end = monitoring_window.today()
    start = end - dt.timedelta(days=days - 1)
    return json.dumps({"start": start.strftime("%Y/%m/%d") + " 00:00:00", "end": end.strftime("%Y/%m/%d") + " 23:59:59"},
                      ensure_ascii=False, separators=(",", ":"))


def _center_keeps(account: Account, row: dict[str, Any], keys: Iterable[str]) -> bool:
    """操作场账号只保留这些网点字段里有邵阳操作场的记录；其他账号原样保留。"""
    return not account.center or any(_text(row.get(key)) == CENTER_SITE for key in keys)


def _source_label(account: Account, title: str) -> str:
    return f"融辉 · {title} · {account.site or account.label}"


# -- 收到工单 ------------------------------------------------------------------------------

def _query(session, context, state, page, page_size):
    if not state:
        return _read(session, context, "FIND_TAB_NOTICE_NOT_SEE", {})
    return _read(session, context, "FIND_TAB_WORK_ORDER", {
        "ZRF": 1, "IS_DEL1": 1, "WORK_STATE": state,
        "ORDER_BY_CREATE_DATE": "ORDER_BY_DATE",
        "pageIndex": page - 1, "pageSize": page_size,
    }, paged=True)


def _own_work_orders(account: Account, context, state) -> list[dict[str, Any]]:
    """Read every received order in one state; the center account keeps only its own."""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        page_rows, total = _query(account.session, context, state, page, WORK_FETCH_PAGE_SIZE)
        if total > MAX_WORK_ORDERS:
            raise RonghuiMonitoringError("融辉收到工单数量过多，请打开原系统核对。")
        rows.extend(page_rows)
        if page * WORK_FETCH_PAGE_SIZE >= total:
            break
        page += 1
    own: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        code = _text(row.get("WORK_ORDER_CODE"))
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
        if _center_keeps(account, row, ("RESPONSIBLE_PARTY", "REGISTER_SITE")):
            own.append(row)
    return own


def _read_history(session, context, code: str) -> tuple[list[dict[str, Any]], int]:
    history, total = _read(session, context, "FIND_TAB_WORK_ORDER_DETAILS", {
        "WORK_ORDER_CODE": code, "pageIndex": 0, "pageSize": 20,
        "sortField": "HANDLE_DATE", "sortOrder": "desc",
    }, paged=True, timeout=8)
    if any(_text(entry.get("WORK_ORDER_CODE")) != code
           or "HANDLE_CONTENT" not in entry or not entry.get("HANDLE_DATE") for entry in history):
        raise RonghuiMonitoringError("工单处理记录字段缺失或所属编号不符。")
    history.sort(key=lambda entry: str(entry["HANDLE_DATE"]), reverse=True)
    return history, total


def _work_order_item(account: Account, row: dict[str, Any], history: tuple[list, int] | None, state: str,
                     title: str) -> dict[str, Any]:
    content = plain_text(row.get("WORK_CONTENT"))
    deadline = row.get("FIRST_RESPONSE_END_DATE" if state == "2" else "OVER_END_TIME")
    if deadline:
        content += f"\n{'响应' if state == '2' else '完结'}截止：{deadline}"
    occurred_at = _text(row.get("REGISTER_DATE"))
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
        "id": _text(row["WORK_ORDER_CODE"]), "title": _text(row.get("WORK_NAME")) or title,
        "content": content or "原系统正文为空", "occurred_at": occurred_at,
        "status_label": STATE_LABELS[state], "waybill_no": _text(row.get("BILL_CODE")),
        "source_label": _source_label(account, title), "open_url": HOME, "partial": history is None,
    }


def _work_items(account: Account, source: Source) -> list[dict[str, Any]]:
    context = _account_context(account, source.menu)
    rows = _own_work_orders(account, context, source.state)
    if not rows:
        return []

    def load(row):
        try:
            return _read_history(account.session, context, _text(row["WORK_ORDER_CODE"]))
        except Exception:
            return None

    # Only bounded read requests run concurrently on this account's session.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ronghui-history") as executor:
        histories = list(executor.map(load, rows))
    items = []
    for row, history in zip(rows, histories):
        times = [row.get("REGISTER_DATE")] + ([entry["HANDLE_DATE"] for entry in history[0]] if history else [])
        # A failed history read keeps the order visible (its recency cannot be disproved).
        if history is None or is_recent(times):
            items.append(_work_order_item(account, row, history, source.state, source.title))
    return items


# -- 公告 ------------------------------------------------------------------------------------

def _notice_items(account: Account, source: Source) -> list[dict[str, Any]]:
    rows, _ = _query(account.session, _account_context(account, source.menu), "", 1, 1)
    items = []
    for row in rows:
        if not is_recent([row.get("NOTICE_DATETIME")]):
            continue
        identifier = _text(row.get("GUID"))
        if not identifier:
            raise RonghuiMonitoringError("融辉消息缺少编号字段，请检查原系统。")
        content = "\n".join(filter(None, [plain_text(row.get("CONTENT")), plain_text(row.get("CONTENT1")),
                                          plain_text(row.get("CONTENT2"))]))
        if not content:
            # 实测 2026-11-18：“问题件”类通知不返回 CONTENT 列，只有类型、发送网点、发送人和运单号。
            summary = (("类型", "NOTICE_NAME_TEXT"), ("发送网点", "SEND_SITE"), ("发送人", "PERSON"), ("运单号", "BILL_CODE"))
            content = "\n".join(f"{label}：{plain_text(row.get(key))}" for label, key in summary if plain_text(row.get(key)))
        items.append({
            "id": identifier, "title": _text(row.get("TITLE")) or source.title, "content": content or "原系统正文为空",
            "occurred_at": _text(row.get("NOTICE_DATETIME")), "status_label": "未读",
            "waybill_no": _text(row.get("BILL_CODE")), "source_label": _source_label(account, source.title),
            "open_url": HOME,
        })
    return items


# -- 收到问题件 ------------------------------------------------------------------------------

def _problem_items(account: Account, source: Source) -> list[dict[str, Any]]:
    end = monitoring_window.today()
    start = monitoring_window.cutoff_date(end)
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        result = _ronghui_query(account.session, {
            "filters": {"direction": "received", "date_from": start.isoformat(), "date_to": end.isoformat(),
                        "page": page, "page_size": FETCH_PAGE_SIZE, "LOGIN_SITE_CODE": account.site_code},
            "account_id": account.profile, "account_label": account.label,
        })
        stats = result.get("stats") or {}
        total = stats.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total > MAX_ROWS:
            raise RonghuiMonitoringError("融辉收到问题件数量缺失或过多，请打开原系统核对。")
        rows.extend(result.get("rows") or [])
        if page * FETCH_PAGE_SIZE >= total:
            break
        page += 1
    items = []
    for row in rows:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        if _has_problem_reply(raw, _text(row.get("reply_text"))):
            continue  # 我方已回复
        if not _center_keeps(account, raw, ("REGISTER_SITE", "SEND_SITE")):
            continue
        times = [row.get("registered_at"), row.get("registration_saved_at"), row.get("updated_at")]
        if not is_recent(times):
            continue
        content = plain_text(row.get("problem_text"))
        sites = "；".join(filter(None, [f"发布网点：{_text(raw.get('REGISTER_SITE'))}" if raw.get("REGISTER_SITE") else "",
                                        f"通知网点：{_text(raw.get('SEND_SITE'))}" if raw.get("SEND_SITE") else ""]))
        items.append({
            "id": _text(row.get("external_id")), "title": _text(row.get("problem_type")) or source.title,
            "content": "\n".join(filter(None, [content or "原系统正文为空", sites])),
            "occurred_at": latest_activity(times), "status_label": "未回复",
            "waybill_no": _text(row.get("waybill_no")), "source_label": _source_label(account, source.title),
            "open_url": HOME,
        })
    return items


# -- 投诉、申诉、代取件 ------------------------------------------------------------------------

def _complaint_items(account: Account, source: Source) -> list[dict[str, Any]]:
    context = _account_context(account, source.menu)
    window = _window_range(COMPLAINT_LOOKBACK_DAYS)
    data = {"searchDateType": "EXCEPTION_DATE", "EXCEPTION_DATE": window, "SEARCH_DATE_RANGE": window,
            "CURRENT_LOGIN_SITE": account.site_code}
    if account.site_type != "总部":
        data["LOGIN_SITE_CODE"] = account.site_code
    rows = _read_all(account.session, context, source.call_id, data, label="融辉投诉")
    items = []
    for row in rows:
        identifier = _text(row.get("EXCEPTION_ID"))
        if not identifier:
            raise RonghuiMonitoringError("融辉投诉缺少编号字段，请检查原系统。")
        status, arbitration = _text(row.get("STATUS")), _text(row.get("ARBITRATE_STATUS"))
        if status not in COMPLAINT_OPEN_STATUS and arbitration not in ARBITRATION_OPEN_STATUS:
            continue
        if not _center_keeps(account, row, ("EXCEPTIONSITE_SIDE", "EXCEPTION_SITE", "REGISTER_SITE")):
            continue
        times = [row.get(key) for key in COMPLAINT_DATES]
        if not is_recent(times):
            continue
        label = "；".join(filter(None, [f"投诉{status}" if status else "", f"仲裁{arbitration}" if arbitration else ""]))
        lines = [plain_text(row.get("EXCEPTION_CONTENT") or row.get("CONTENT") or row.get("REMARK")),
                 f"投诉类型：{_text(row.get('EXCEPTION_TYPE'))}" if row.get("EXCEPTION_TYPE") else "",
                 f"投诉方：{_text(row.get('EXCEPTION_SITE') or row.get('REGISTER_SITE'))}",
                 f"被投诉方：{_text(row.get('EXCEPTIONSITE_SIDE'))}",
                 f"投诉时间：{_text(row.get('EXCEPTION_DATE'))}"]
        items.append({
            "id": identifier, "title": _text(row.get("EXCEPTION_TYPE")) or source.title,
            "content": "\n".join(line for line in lines if line and not line.endswith("：")),
            "occurred_at": latest_activity(times), "status_label": label or "未完结",
            "waybill_no": _text(row.get("BILL_CODE")), "source_label": _source_label(account, source.title),
            "open_url": HOME,
        })
    return items


def _appeal_items(account: Account, source: Source) -> list[dict[str, Any]]:
    context = _account_context(account, source.menu)
    data = {} if account.site_type == "总部" else {"LOGIN_SITE_CODE": account.site_code}
    rows = _read_all(account.session, context, source.call_id, data, label="融辉申诉")
    items = []
    for row in rows:
        status = _text(row.get("COMPLAINT_STATUS"))
        if status not in APPEAL_OPEN_STATUS:
            continue
        if not _center_keeps(account, row, ("APPEAL_SITE", "CREATE_SITE")):
            continue
        times = [row.get(key) for key in APPEAL_DATES]
        if not is_recent(times):
            continue
        # 实测 2026-11-27：申诉没有自己的编号（ROW_ID 只是查询行号），以所属投诉编号 + 登记时间标识。
        exception_id, created = _text(row.get("EXCEPTION_ID")), _text(row.get("CREATE_DATE"))
        if not exception_id or not created:
            raise RonghuiMonitoringError("融辉申诉缺少投诉编号或登记时间，请检查原系统。")
        identifier = f"{exception_id}:{created}"
        lines = [plain_text(row.get("APPEAL_CONTENT")),
                 f"申诉类型：{_text(row.get('APPEAL_TYPE_LB') or row.get('APPEAL_TYPE'))}" if (row.get("APPEAL_TYPE_LB") or row.get("APPEAL_TYPE")) else "",
                 f"申诉网点：{_text(row.get('APPEAL_SITE'))}" if row.get("APPEAL_SITE") else "",
                 f"登记时间：{_text(row.get('CREATE_DATE'))}" if row.get("CREATE_DATE") else ""]
        items.append({
            "id": identifier, "title": _text(row.get("PROCESS_TYPE")) or source.title,
            "content": "\n".join(filter(None, lines)) or "原系统正文为空",
            "occurred_at": latest_activity(times), "status_label": status,
            "waybill_no": _text(row.get("BILL_CODE")), "source_label": _source_label(account, source.title),
            "open_url": HOME,
        })
    return items


def _pickup_items(account: Account, source: Source) -> list[dict[str, Any]]:
    context = _account_context(account, source.menu)
    window = _window_range(COMPLAINT_LOOKBACK_DAYS)
    data = {"searchDateType": "CREATE_DATE", "CREATE_DATE": window, "SEARCH_DATE_RANGE": window}
    if account.site_type != "总部":
        data["LOGIN_SITE_CODE"] = account.site_code
    rows = _read_all(account.session, context, source.call_id, data, label="融辉代取件")
    items = []
    for row in rows:
        status = _text(row.get("STATUS"))
        code = next((key for key, text in PICKUP_STATUS_TEXT.items() if status in {key, text}), None)
        if code is None:
            raise RonghuiMonitoringError("融辉代取件返回了未知订单状态，已停止展示该查询结果。")
        if code not in PICKUP_OPEN_STATUS:
            continue
        times = [row.get(key) for key in PICKUP_DATES]
        if not is_recent(times):
            continue
        identifier = _text(row.get("TX_LOGISTIC_ID") or row.get("BILL_CODE"))
        if not identifier:
            raise RonghuiMonitoringError("融辉代取件缺少订单号，请检查原系统。")
        lines = [f"寄件人：{_text(row.get('SEND_NAME'))}" if row.get("SEND_NAME") else "",
                 f"物品：{_text(row.get('ITEM_NAME'))}" if row.get("ITEM_NAME") else "",
                 f"下单网点：{_text(row.get('ORDER_SITE'))}" if row.get("ORDER_SITE") else "",
                 f"接单网点：{_text(row.get('PREPARE_SITE'))}" if row.get("PREPARE_SITE") else "",
                 f"备注：{plain_text(row.get('REMARK'))}" if row.get("REMARK") else ""]
        items.append({
            "id": identifier, "title": f"代取件 {identifier}",
            "content": "\n".join(filter(None, lines)) or "原系统正文为空",
            "occurred_at": latest_activity(times), "status_label": PICKUP_STATUS_TEXT[code],
            "waybill_no": _text(row.get("BILL_CODE")), "source_label": _source_label(account, source.title),
            "open_url": HOME,
        })
    return items


_READERS = {"work": _work_items, "notice": _notice_items, "problem": _problem_items, "complaint": _complaint_items,
            "appeal": _appeal_items, "pickup": _pickup_items}


# -- 四个账号合并 ----------------------------------------------------------------------------

def _account_items(account: Account, category_ids: Iterable[str]) -> dict[str, Any]:
    """One account's items per category, or the exception that stopped that category."""
    result: dict[str, Any] = {}
    for category_id in category_ids:
        source = SOURCES[category_id]
        try:
            result[category_id] = _READERS[source.kind](account, source)
        except MenuMissing as exc:
            result[category_id] = None if source.optional_menu else exc
        except Exception as exc:  # recorded per account; never stops the other accounts
            result[category_id] = exc
            if isinstance(exc, TMSAuthStateError):
                for rest in category_ids:  # an expired login fails every later read of this account
                    result.setdefault(rest, exc)
                break
    return result


def _gather(open_session: Callable[[str], Any], category_ids: list[str],
            accounts: tuple[tuple[str, str], ...]) -> tuple[dict[str, list], dict[str, list[tuple[str, Exception]]]]:
    def run(entry):
        profile, label = entry
        try:
            account = _open_account(profile, label, open_session)
        except Exception as exc:
            return label, {category_id: exc for category_id in category_ids}
        try:
            return account.site or label, _account_items(account, category_ids)
        finally:
            close = getattr(account.session, "close", None)
            if callable(close):
                close()

    with ThreadPoolExecutor(max_workers=max(1, len(accounts)), thread_name_prefix="ronghui-account") as executor:
        per_account = list(executor.map(run, accounts))
    items: dict[str, list] = {category_id: [] for category_id in category_ids}
    failures: dict[str, list[tuple[str, Exception]]] = {category_id: [] for category_id in category_ids}
    for name, results in per_account:
        for category_id in category_ids:
            value = results.get(category_id)
            if isinstance(value, Exception):
                failures[category_id].append((name, value))
            elif value:
                items[category_id].append(value)
    merged: dict[str, list] = {}
    for category_id, groups in items.items():
        seen: dict[str, dict] = {}
        for group in groups:
            ids = [item["id"] for item in group]
            if len(ids) != len(set(ids)):
                failures[category_id].append(("", RonghuiMonitoringError("融辉列表出现重复编号，无法确认分页数量。")))
                continue
            for item in group:
                seen.setdefault(item["id"], item)  # 同一记录被几个账号读到只算一次
        merged[category_id] = sorted(seen.values(), key=lambda item: item["occurred_at"], reverse=True)
    return merged, failures


def _failure_text(failures: list[tuple[str, Exception]]) -> str:
    parts = []
    for name, exc in failures:
        reason = str(exc) if isinstance(exc, (TMSAuthStateError, RonghuiMonitoringError)) else "读取失败"
        parts.append(f"{name}：{reason}" if name else reason)
    return "；".join(parts)


def _outcome(category_id: str, items: list, failures: list, accounts_total: int) -> dict[str, Any]:
    """ok / partial (some accounts failed: their part is not counted) / failed (nothing could be read)."""
    if failures and len({name for name, _ in failures}) >= accounts_total:
        auth = all(isinstance(exc, TMSAuthStateError) for _, exc in failures)
        return {"count": None, "status": "auth_required" if auth else "error",
                "status_label": "登录失效" if auth else "读取失败", "message": _failure_text(failures)}
    source = SOURCES[category_id]
    note = RECENT_NOTE + (CENTER_NOTE if source.kind != "notice" else "")
    if failures:
        return {"count": len(items), "status": "partial", "status_label": "部分账号读取失败",
                "message": f"以下账号未计入：{_failure_text(failures)}。" + note}
    open_label = {"notice": "未读", "problem": "未回复", "pickup": "未揽收"}.get(source.kind, "未完结")
    label = (STATE_LABELS.get(source.state) or open_label) if items else "暂无待办"
    return {"count": len(items), "status": "ok", "status_label": label, "message": note}


def collect_categories(open_session: Callable[[str], Any], *,
                       accounts: tuple[tuple[str, str], ...] = ACCOUNTS) -> list[dict[str, Any]]:
    category_ids = list(SOURCES)
    merged, failures = _gather(open_session, category_ids, accounts)
    categories = []
    for category_id in category_ids:
        source = SOURCES[category_id]
        categories.append({
            "system": "ronghui", "category_id": category_id, "title": source.title,
            "items_supported": True, "detail_supported": True, "open_url": HOME,
            "is_exception": source.is_exception, "tone": "warning" if source.is_exception else "info",
            "include_in_total": True, "count_scope": category_id.split(":", 1)[1],
            **_outcome(category_id, merged[category_id], failures[category_id], len(accounts)),
        })
    return categories


def collect_items(open_session: Callable[[str], Any], category_id: str, page: int, page_size: int = 20, *,
                  accounts: tuple[tuple[str, str], ...] = ACCOUNTS) -> dict[str, Any]:
    if category_id not in SOURCES:
        raise RonghuiMonitoringError("不支持的融辉消息分类。")
    if not 1 <= page <= 10000 or not 1 <= page_size <= 20:
        raise RonghuiMonitoringError("消息页码或每页数量无效。")
    source = SOURCES[category_id]
    merged, failures = _gather(open_session, [category_id], accounts)
    items, failed = merged[category_id], failures[category_id]
    outcome = _outcome(category_id, items, failed, len(accounts))
    if outcome["count"] is None:
        first = failed[0][1]
        if isinstance(first, TMSAuthStateError) and outcome["status"] == "auth_required":
            raise first
        raise RonghuiMonitoringError(outcome["message"] or "融辉来源读取失败。")
    partial = bool(failed) or any(item.get("partial") for item in items)
    total = len(items)
    page_items = [{key: value for key, value in item.items() if key != "partial"}
                  for item in items[(page - 1) * page_size:page * page_size]]
    message = outcome["message"] if failed else (
        "部分处理记录读取失败，已保留显示，请核对原系统。" if partial else outcome["message"]
        + ("按最新动态时间排序。原系统未提供回复未读标识。" if source.kind == "work" else ""))
    return {
        "title": source.title, "items": page_items, "total": total, "page": page, "page_size": page_size,
        "has_more": page * page_size < total, "open_url": HOME,
        "status": "partial" if partial else "ok", "status_label": outcome["status_label"] if failed else (
            "部分读取失败" if partial else "已同步"),
        "message": message,
    }
