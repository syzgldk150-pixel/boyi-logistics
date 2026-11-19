"""融辉/韵达预付款对账单：按平台原样即时读取（只读，不入库）。

2026-11-18 在 ECS 用真实登录态只读核实：

- 融辉“预付款管理 / 结算明细查询”（MiniUI 页面）。必须先打开菜单给出的 widget 页，
  再带 ``authenticationKey``/``pageId`` 请求头调用 ``/dataQuery/findPageByCallId``，否则平台回
  “非法的请求”。四个表格：余额 ``FIND_BALANCE_ACCOUNT_INFO``（accountDatagrid）、统计
  ``FIND_BALANCE_QRY_TJ_WST``（datagridTJ，汇总数据）、统计行双击 ``FIND_BALANCE_QRY_TJ_DETAIL``
  （datagridTJDetails，带该行 CENTER_NAME_CODE/SITE_NAME_CODE/BALANCE_TYPE）、明细查询
  ``FIND_BALANCE_QRY_WST_WITH_SITE``（datagrid）。日期口径 ``DATE_TYPE`` 为 BALANCE_DATE（结算日期）
  或 FINANCE_DATE（账单日期），区间放在 ``DATE`` 与同名口径字段。列标题与顺序取自当次页面。
- 韵达“快运预付款系统”（KY_YFK）的报表在 ky-appept 模板系统：GET ``export/logins`` 经 ky-sso
  OAuth 跳转建立会话；``financialSysAuthor/getAuth`` 给出 userId；列 ``selectDynamicFileds``；
  “交易明细及汇总查询”三个页签走 ``dynamicPcTabInterface/selectInterface``（三层 JSON 字符串，
  最长 31 天）；“分部预付款 / 账户余额(二级网点)”走 ``selectFiledsData``（下拉条件空值为一个空格）。
  下钻由列的 ``skipData`` 定义，取值来自所点行，与平台页面一致。

金额与其它字段原样返回（JSON 小数保留原文字符串），不换算、不汇总。统计查询另附 ``groups``：
每个统计行（中心、网点、结算类型）按平台“双击统计行”的明细接口读出的笔数（明细总条数 ``total``）与
最近结算日期（明细“结算日期”列的最大值，列取自当次页面“明细数据”表格的定义，与所选日期口径无关；
明细超过可读范围时为空并注明），金额仍以统计行原值为准。
"""

from __future__ import annotations

import base64
import datetime as dt
import html as html_lib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.direct_business import DirectBusinessError
from agent.tms_runtime.direct_execution import call_blocking
from agent.tms_runtime.platform_waybill_print import RONGHUI_MENU_URL, RONGHUI_ORIGIN, _read_user_info, _walk_menu
from agent.tms_runtime.session_broker import get_session_broker
from shared.finance.platform_bills import (
    DEFAULT_PAGE_SIZE,
    MAX_SPAN_DAYS,
    PAGE_SIZES,
    RONGHUI_ACTIONS,
    RONGHUI_DATE_TYPES,
    YUNDA_ACTIONS,
    YUNDA_FILTER_KEYS,
    bill_account_ids,
)

CHINA_TZ = ZoneInfo("Asia/Shanghai")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
CODE_PATTERN = re.compile(r"[0-9A-Za-z_-]{1,30}")
MAX_PAGES = 20
# 统计查询每个结算类型最多读这么多页明细（每页 500 行）来求最近结算日；笔数始终取平台 total。
GROUP_DETAIL_PAGES = 6

RONGHUI_MENU = ("预付款管理", "结算明细查询")
RONGHUI_QUERY_URL = f"{RONGHUI_ORIGIN}/dataQuery/findPageByCallId"
# action -> (页面表格 id, 调用 id)
RONGHUI_GRIDS = {
    "balance": ("accountDatagrid", "FIND_BALANCE_ACCOUNT_INFO"),
    "summary": ("datagridTJ", "FIND_BALANCE_QRY_TJ_WST"),
    "summary_detail": ("datagridTJDetails", "FIND_BALANCE_QRY_TJ_DETAIL"),
    "detail": ("datagrid", "FIND_BALANCE_QRY_WST_WITH_SITE"),
}

YUNDA_APPEPT = "https://ky-appept.yunda56.com/export/"
YUNDA_APP_VALUE = "KY_YFK"
YUNDA_TRADE_MENU = "ac_trade_sum_query"
YUNDA_BRANCH_MENU = "account_amount_fb"
YUNDA_TRADE_TABS = {"summary": "ac_trade_sum_query", "daily": "ac_trade_sum_day", "detail": "ac_trade_detail_es"}
YUNDA_MENU_PARAMS = {YUNDA_TRADE_MENU: {"initSearch": 1}, YUNDA_BRANCH_MENU: {"workType": "branch", "initSearch": 1}}
YUNDA_BRANCH_CONDITIONS = {"auth_type": " ", "branch_status": " ", "send_status": " ", "account_status": " ",
                           "account": "", "super_branch_code": "", "branch_code": ""}


class PlatformBillError(DirectBusinessError):
    pass


def _loads(text: str) -> Any:
    """Parse platform JSON keeping decimals as their original text."""
    return json.loads(text, parse_float=str)


def _now() -> str:
    return dt.datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- input


def _text(params: dict[str, Any], key: str, default: str = "") -> str:
    value = params.get(key, default)
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", f"对账单查询参数 {key} 无效。")
    return str(value).strip()


def _dates(params: dict[str, Any]) -> tuple[str, str]:
    start, end = _text(params, "start"), _text(params, "end")
    if not DATE_PATTERN.fullmatch(start) or not DATE_PATTERN.fullmatch(end):
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "请选择开始和结束日期。")
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError as exc:
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "日期无效。") from exc
    if last < first:
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "结束日期不能早于开始日期。")
    if (last - first).days + 1 > MAX_SPAN_DAYS:
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", f"平台一次最多查询 {MAX_SPAN_DAYS} 天，请缩短日期范围。")
    return start, end


def _paging(params: dict[str, Any]) -> tuple[int, int]:
    try:
        page = int(_text(params, "page", "1") or 1)
        size = int(_text(params, "page_size", str(DEFAULT_PAGE_SIZE)) or DEFAULT_PAGE_SIZE)
    except ValueError as exc:
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "页码无效。") from exc
    if not 1 <= page <= 10000 or size not in PAGE_SIZES:
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "页码或每页条数无效。")
    return page, size


_COMMON_KEYS = {"platform", "account_id", "action", "start", "end", "page", "page_size"}
_RONGHUI_KEYS = _COMMON_KEYS | {"date_type", "center_code", "site_code", "balance_type"}
_YUNDA_KEYS = _COMMON_KEYS | {"filters"}


def validate_query(params: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "对账单查询格式无效。")
    platform = _text(params, "platform")
    if platform not in ("ronghui", "yunda"):
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "平台无效。")
    if set(params) - (_RONGHUI_KEYS if platform == "ronghui" else _YUNDA_KEYS):
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "对账单查询包含不支持的参数。")
    account_id = _text(params, "account_id")
    if account_id not in bill_account_ids(platform):
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "该账号不在对账单取数账号范围内。")
    action = _text(params, "action")
    query: dict[str, Any] = {"platform": platform, "account_id": account_id, "action": action}
    if platform == "ronghui":
        if action not in RONGHUI_ACTIONS:
            raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "融辉对账单查询类型无效。")
        if action != "balance":
            query["start"], query["end"] = _dates(params)
            date_type = _text(params, "date_type", "BALANCE_DATE")
            if date_type not in RONGHUI_DATE_TYPES:
                raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "日期口径无效。")
            query["date_type"] = date_type
        if action in ("summary_detail", "detail"):
            query["page"], query["page_size"] = _paging(params)
        if action == "summary_detail":
            center, site, kind = _text(params, "center_code"), _text(params, "site_code"), _text(params, "balance_type")
            if not CODE_PATTERN.fullmatch(center) or not CODE_PATTERN.fullmatch(site) or not 0 < len(kind) <= 60:
                raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "统计行的网点或结算类型无效。")
            query.update(center_code=center, site_code=site, balance_type=kind)
        return query
    if action not in YUNDA_ACTIONS:
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "韵达对账单查询类型无效。")
    query["page"], query["page_size"] = _paging(params)
    if action == "branches":
        return query
    query["start"], query["end"] = _dates(params)
    filters = params.get("filters") or {}
    if not isinstance(filters, dict) or set(filters) - YUNDA_FILTER_KEYS:
        raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", "韵达查询条件无效。")
    clean: dict[str, str] = {}
    for key, value in filters.items():
        if value is None:
            continue
        if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 100:
            raise PlatformBillError("INVALID_PLATFORM_BILL_QUERY", f"韵达查询条件 {key} 无效。")
        clean[key] = str(value)
    query["filters"] = clean
    return query


# --------------------------------------------------------------------------- Ronghui


def _ronghui_page(session: Any, timeout_sec: int) -> dict[str, str]:
    response = session.get(RONGHUI_MENU_URL, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=timeout_sec)
    try:
        nodes = response.json().get("result", {}).get("data")
    except ValueError:
        nodes = None
    if not isinstance(nodes, list):
        raise PlatformBillError("AUTH_REQUIRED", "融辉账号登录已失效，请到“业务账号”重新登录。")
    urls = {str(node.get("url") or "") for node, path in _walk_menu(nodes)
            if path[-2:] == RONGHUI_MENU and "/widget/home" in str(node.get("url") or "")}
    if len(urls) != 1:
        raise PlatformBillError("PLATFORM_PAGE_UNAVAILABLE", "该融辉账号没有唯一的“预付款管理 / 结算明细查询”菜单。")
    url = urljoin(RONGHUI_ORIGIN, urls.pop())
    query = parse_qs(urlparse(url).query)
    page = {"url": url, "authentication_key": (query.get("authenticationKey") or [""])[0],
            "page_id": (query.get("pageId") or [""])[0]}
    page["html"] = session.get(url, timeout=timeout_sec).text
    if not page["authentication_key"] or not page["page_id"]:
        raise PlatformBillError("PLATFORM_PAGE_UNAVAILABLE", "融辉“结算明细查询”页面未正常打开，请检查账号登录。")
    return page


_TAG = re.compile(r"<div\b([^>]*)>")
_ATTR = re.compile(r'([A-Za-z_:-]+)="([^"]*)"')


def ronghui_columns(page_html: str, grid_id: str) -> list[dict[str, str]]:
    """Columns of one MiniUI datagrid exactly as the page declares them (visible ones, in order)."""
    start = page_html.find(f'id="{grid_id}"')
    if start < 0:
        raise PlatformBillError("PLATFORM_PAGE_CHANGED", "融辉“结算明细查询”页面已变化，需重新核对后才能读取。")
    stop = page_html.find('viewtype="', start)
    segment = page_html[start: stop if stop > start else len(page_html)]
    columns, seen = [], set()
    for tag in _TAG.finditer(segment):
        attrs = dict(_ATTR.findall(tag.group(1)))
        field, header = attrs.get("field", ""), html_lib.unescape(attrs.get("header", "")).strip()
        if not field or not header or attrs.get("visible") == "false" or field in seen:
            continue
        seen.add(field)
        columns.append({"field": field, "title": header})
    if not columns:
        raise PlatformBillError("PLATFORM_PAGE_CHANGED", "融辉“结算明细查询”页面已变化，需重新核对后才能读取。")
    return columns


def _ronghui_rows(session: Any, page: dict[str, str], call_id: str, form: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    headers = {"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
               "Origin": RONGHUI_ORIGIN, "Referer": page["url"],
               "authenticationKey": page["authentication_key"], "pageId": page["page_id"]}
    response = session.post(f"{RONGHUI_QUERY_URL}?id={call_id}", headers=headers, data=form, timeout=timeout_sec)
    response.raise_for_status()
    payload = _loads(response.text)
    if not isinstance(payload, dict):
        raise ValueError("ronghui response is not an object")
    if payload.get("success") is False:
        reason = str(payload.get("message") or "").strip() or "未说明原因"
        raise PlatformBillError("PLATFORM_REJECTED", f"融辉拒绝了查询：{reason}")
    rows, total = payload.get("data"), payload.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("ronghui response lacks data/total")
    info = payload.get("totalInfo") if isinstance(payload.get("totalInfo"), dict) else {}
    # totalInfo.COUNT 只是行数（已由 total 给出），其余键才是平台合计。
    return {"rows": rows, "total": total, "footer": {key: value for key, value in info.items() if key != "COUNT"}}


def _latest_date(rows: list[Any], field: str) -> str | None:
    dates = [str(row.get(field) or "")[:10] for row in rows if isinstance(row, dict)]
    dates = [item for item in dates if DATE_PATTERN.fullmatch(item)]
    return max(dates) if dates else None


SETTLEMENT_DATE_TITLE = "结算日期"


def ronghui_groups(session: Any, page: dict[str, str], rows: list[Any], form: dict[str, Any],
                   timeout_sec: int) -> list[dict[str, Any]]:
    """笔数（明细总条数）与最近结算日期 for each statistics row, read the way the page drills into it."""
    grid, call_id = RONGHUI_GRIDS["summary_detail"]
    fields = [column["field"] for column in ronghui_columns(page["html"], grid) if column["title"] == SETTLEMENT_DATE_TITLE]
    if len(fields) != 1:
        raise PlatformBillError("PLATFORM_PAGE_CHANGED", "融辉“明细数据”表格没有唯一的“结算日期”列，页面已变化，需重新核对。")
    date_field = fields[0]
    groups, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("CENTER_NAME_CODE") or ""), str(row.get("SITE_NAME_CODE") or ""), str(row.get("BALANCE_TYPE") or ""))
        if key in seen or not key[2]:
            continue
        seen.add(key)
        detail_form = {**form, "CENTER_NAME_CODE": key[0], "SITE_NAME_CODE": key[1], "TURN_NAME_CODE": "", "BALANCE_TYPE": key[2]}
        read: list[Any] = []
        total = 0
        for index in range(GROUP_DETAIL_PAGES):
            part = _ronghui_rows(session, page, call_id, {**detail_form, "pageIndex": index, "pageSize": 500}, timeout_sec)
            read.extend(part["rows"])
            total = part["total"]
            if len(read) >= total or not part["rows"]:
                break
        complete = len(read) >= total
        groups.append({"center_code": key[0], "site_code": key[1], "balance_type": key[2], "count": total,
                       "latest_date": _latest_date(read, date_field) if complete else None,
                       "latest_note": "" if complete else "明细超过可读范围，未求最近结算日期"})
    return groups


def ronghui_bills(session_profile: str, query: dict[str, Any], *, timeout_sec: int) -> dict[str, Any]:
    session = get_session_broker(session_profile).open_capability_session("ronghui_home", validate=False)
    page = _ronghui_page(session, timeout_sec)
    info = _read_user_info(session)
    site_code, site_name = str(info.get("loginSiteCode") or ""), str(info.get("loginSiteName") or "")
    if not site_code:
        raise PlatformBillError("AUTH_REQUIRED", "融辉账号登录信息缺少网点编号，请到“业务账号”重新登录。")
    action = query["action"]
    grid_id, call_id = RONGHUI_GRIDS[action]
    if call_id not in page["html"]:
        raise PlatformBillError("PLATFORM_PAGE_CHANGED", "融辉“结算明细查询”页面已变化，需重新核对后才能读取。")
    columns = ronghui_columns(page["html"], grid_id)
    form: dict[str, Any] = {}
    if action == "balance":
        form = {"SITE_NAME_CODE": site_code}
    else:
        span = json.dumps({"start": f"{query['start']} 00:00:00", "end": f"{query['end']} 23:59:59"})
        form = {"DATE_TYPE": query["date_type"], "DATE": span, query["date_type"]: span}
        if action == "summary":
            form.update(CENTER_NAME_CODE="", SITE_NAME_CODE="", TURN_NAME_CODE="", BALANCE_TYPE="")
        elif action == "summary_detail":
            form.update(CENTER_NAME_CODE=query["center_code"], SITE_NAME_CODE=query["site_code"],
                        TURN_NAME_CODE="", BALANCE_TYPE=query["balance_type"])
        else:
            form.update(CODE="", SOURCE_TYPE="", CONFIRM_STATUS="", CONFIRM_STATUSNEW="", SITE_NAME_CODE=site_code)
    if action in ("balance", "summary"):
        # 统计与余额行数很少，一次读全（平台单页最多 500 行）。
        rows: list[Any] = []
        total, footer = 0, {}
        for index in range(MAX_PAGES):
            part = _ronghui_rows(session, page, call_id, {**form, "pageIndex": index, "pageSize": 500}, timeout_sec)
            rows.extend(part["rows"])
            total, footer = part["total"], part["footer"] or footer
            if len(rows) >= total or not part["rows"]:
                break
        if len(rows) < total:
            raise PlatformBillError("PLATFORM_DATA_TOO_LARGE", "融辉统计行数超出可读取范围，请缩短日期范围。")
        page_no, size = 1, max(len(rows), 1)
        if action == "summary":
            groups = ronghui_groups(session, page, rows, form, timeout_sec)
    else:
        page_no, size = query["page"], query["page_size"]
        part = _ronghui_rows(session, page, call_id, {**form, "pageIndex": page_no - 1, "pageSize": size}, timeout_sec)
        rows, total, footer = part["rows"], part["total"], part["footer"]
    result = {"site_code": site_code, "site_name": site_name, "columns": columns, "rows": rows,
              "total": total, "page": page_no, "page_size": size, "footer": footer}
    if action == "summary":
        result["groups"] = groups
    return result


# --------------------------------------------------------------------------- Yunda


class _Appept:
    def __init__(self, session: Any, menu_code: str, timeout_sec: int):
        self.session, self.menu_code, self.timeout = session, menu_code, timeout_sec
        param = base64.b64encode(json.dumps(YUNDA_MENU_PARAMS[menu_code], separators=(",", ":")).encode()).decode()
        response = session.get(f"{YUNDA_APPEPT}logins?templateCode={menu_code}"
                               f"&applicationCode=PCTab@SYS:{YUNDA_APP_VALUE}@PARAM:{param}", timeout=timeout_sec)
        landed = urlparse(response.url)
        if landed.netloc != "ky-appept.yunda56.com" or not landed.path.startswith("/export/public/"):
            raise PlatformBillError("AUTH_REQUIRED", "韵达账号登录已失效，请到“业务账号”重新登录。")
        self.headers = {"X-Requested-With": "XMLHttpRequest", "Referer": response.url,
                        "Origin": "https://ky-appept.yunda56.com"}
        auth = self._payload(session.get(f"{YUNDA_APPEPT}author/financialSysAuthor/getAuth",
                                         headers=self.headers, timeout=timeout_sec))
        data = _loads(auth) if isinstance(auth, str) else auth
        self.user_id = str((data or {}).get("username") or "") if isinstance(data, dict) else ""
        if not self.user_id:
            raise PlatformBillError("AUTH_REQUIRED", "韵达预付款系统未返回登录身份，请到“业务账号”重新登录。")

    @staticmethod
    def _payload(response: Any) -> Any:
        response.raise_for_status()
        body = _loads(response.text)
        if not isinstance(body, dict):
            raise ValueError("yunda response is not an object")
        if body.get("success") not in (True, "true"):
            reason = body.get("dataMap") if isinstance(body.get("dataMap"), str) else body.get("msg")
            raise PlatformBillError("PLATFORM_REJECTED", f"韵达拒绝了查询：{str(reason or '未说明原因').strip()}")
        return body.get("dataMap")

    def post(self, path: str, message: dict[str, Any]) -> Any:
        response = self.session.post(f"{YUNDA_APPEPT}{path}", headers=self.headers, timeout=self.timeout,
                                     files={"message": (None, json.dumps(message, ensure_ascii=False))})
        data = self._payload(response)
        return _loads(data) if isinstance(data, str) else data

    def columns(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        data = self.post("author/dynamicPcTabBasic/selectDynamicFileds", message)
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise ValueError("yunda columns missing")
        return yunda_columns(data["rows"])


def _skip_keys(column: dict[str, Any]) -> list[str]:
    skip = column.get("skipData")
    if isinstance(skip, str) and skip.strip():
        skip = _loads(skip)
    return [str(key) for key in skip] if isinstance(skip, dict) else []


def yunda_columns(rows: list[Any]) -> list[dict[str, Any]]:
    """Visible template columns in order; drill links keep the platform's target tab and parameter keys."""
    columns: list[dict[str, Any]] = []

    def add(item: dict[str, Any], prefix: str = "") -> None:
        children = [child for child in item.get("childList") or [] if isinstance(child, dict)]
        title = str(item.get("title") or "").strip()
        if children:
            for child in children:
                add(child, f"{prefix}{title}/")
            return
        field = str(item.get("field") or "")
        if not field or str(item.get("fieldHide")) == "1":
            return
        column: dict[str, Any] = {"field": field, "title": prefix + (title or field)}
        targets = item.get("skipTab")
        if isinstance(targets, str) and targets.strip():
            targets = _loads(targets)
        if str(item.get("skipFieldStatus")) == "1" and isinstance(targets, list) and targets:
            target = targets[0] if isinstance(targets[0], dict) else {}
            if target.get("skip_tab"):
                column["drill"] = {"tab": str(target["skip_tab"]), "keys": _skip_keys(item)}
        columns.append(column)

    for item in rows:
        if isinstance(item, dict):
            add(item)
    if not columns:
        raise ValueError("yunda template has no columns")
    return columns


def _yunda_table(data: Any) -> dict[str, Any]:
    """``selectInterface``: {"rows": "<{success, dataMap: '<{total, footer, rows}>'}>"}."""
    inner = data.get("rows") if isinstance(data, dict) else None
    inner = _loads(inner) if isinstance(inner, str) else inner
    if not isinstance(inner, dict):
        raise ValueError("yunda interface response missing")
    if inner.get("success") not in (True, "true"):
        reason = inner.get("dataMap") if isinstance(inner.get("dataMap"), str) else inner.get("msg")
        raise PlatformBillError("PLATFORM_REJECTED", f"韵达拒绝了查询：{str(reason or '未说明原因').strip()}")
    table = inner.get("dataMap")
    table = _loads(table) if isinstance(table, str) else table
    return _checked_table(table)


def _checked_table(table: Any) -> dict[str, Any]:
    if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
        raise ValueError("yunda table missing rows")
    total = table.get("total")
    if isinstance(total, str) and total.isdigit():
        total = int(total)
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError("yunda table missing total")
    footer = table.get("footer")
    return {"rows": table["rows"], "total": total, "footer": footer if isinstance(footer, list) else []}


def yunda_bills(session_profile: str, query: dict[str, Any], *, timeout_sec: int) -> dict[str, Any]:
    session = get_session_broker(session_profile).open_capability_session("yunda_inms", validate=False)
    action, page, size = query["action"], query["page"], query["page_size"]
    if action == "branches":
        app = _Appept(session, YUNDA_BRANCH_MENU, timeout_sec)
        message = {"menu_code": YUNDA_BRANCH_MENU, "template_code": YUNDA_BRANCH_MENU, "userId": app.user_id,
                   "APPVALUE": YUNDA_APP_VALUE, "model_code_name": "", "interface_skip": 0, "page": page, "rows": size,
                   **YUNDA_BRANCH_CONDITIONS, **YUNDA_MENU_PARAMS[YUNDA_BRANCH_MENU]}
        columns = app.columns(message)
        table = _checked_table(app.post("author/dynamicPcTabBasic/selectFiledsData", message))
    else:
        app = _Appept(session, YUNDA_TRADE_MENU, timeout_sec)
        tab = YUNDA_TRADE_TABS[action]
        message = {"menu_code": YUNDA_TRADE_MENU, "template_code": tab, "userId": app.user_id,
                   "APPVALUE": YUNDA_APP_VALUE, "model_code_name": "", "interface_skip": 1, "page": page, "rows": size,
                   "start_time": query["start"], "end_time": query["end"], **query["filters"], "initSearch": 1}
        columns = app.columns(message)
        table = _yunda_table(app.post("author/dynamicPcTabInterface/selectInterface", {
            "menu_code": YUNDA_TRADE_MENU, "tab_code": tab, "APPVALUE": YUNDA_APP_VALUE, "userId": app.user_id,
            "interface_skip": 1, "message": json.dumps(message, ensure_ascii=False)}))
    return {"user_id": app.user_id, "columns": columns, "rows": table["rows"], "total": table["total"],
            "page": page, "page_size": size, "footer": table["footer"]}


# --------------------------------------------------------------------------- business entry


def _platform_call(platform: str, action):
    import requests

    from agent.tms_runtime.errors import TMSAuthStateError

    name = "融辉" if platform == "ronghui" else "韵达"
    try:
        return action()
    except PlatformBillError:
        raise
    except TMSAuthStateError as exc:
        raise PlatformBillError(exc.code, f"{name}账号登录状态不可用：{exc}") from exc
    except requests.RequestException as exc:
        raise PlatformBillError("PLATFORM_UNREACHABLE", f"{name}接口请求失败（{type(exc).__name__}），请稍后重试。") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise PlatformBillError("PLATFORM_DATA_INVALID", f"{name}返回的数据格式与核实时不一致，请稍后重试或联系维护。") from exc


def _session_profile(platform: str, account_id: str) -> str:
    descriptor = get_account_manager().require_active_binding_descriptor(account_id)
    if descriptor["system"] != platform or not descriptor["session_profile"]:
        raise PlatformBillError("ACCOUNT_PLATFORM_MISMATCH", "对账单账号与平台不一致。")
    return descriptor["session_profile"]


async def platform_prepay_bills_async(params: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    query = validate_query(params)
    platform = query["platform"]
    read_timeout = min(int(timeout_sec), 40)

    def read() -> dict[str, Any]:
        profile = _platform_call(platform, lambda: _session_profile(platform, query["account_id"]))
        reader = ronghui_bills if platform == "ronghui" else yunda_bills
        result = _platform_call(platform, lambda: reader(profile, query, timeout_sec=read_timeout))
        return {"platform": platform, "account_id": query["account_id"], "action": query["action"],
                "fetched_at": _now(), **result}

    return await call_blocking(read, timeout_sec=timeout_sec)
