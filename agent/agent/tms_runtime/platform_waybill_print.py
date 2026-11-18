"""Original-site waybill labels of Ronghui and Yunda, read live from the platform.

Verified against the real pages on 2026-11-18 (read-only, ECS sessions):

- Ronghui “运单管理 / 主面单打印” (补打): ``FIDN_FACE_SINGLE_PRINT_ONE4`` with
  ``BILL_CODE`` returns the row the page prints; ``printBillNew`` prepares it
  and ``printKH`` draws the default “客户联” (page defaults: 不打印运费, 手机号
  不加密, 不显示 LOGO, 收件人不加密) on ``PRINT_INITA(0mm,0mm,80mm,100mm)`` /
  ``SET_PRINT_PAGESIZE(0,75mm,90mm)``. After a real print the page saves
  ``TAB_PRINT_COUNT_ADD`` through ``saveTables``; the row's ``PRINT_NUM``
  counts it. The page code is fingerprinted: when Ronghui changes it, printing
  stops with a clear message instead of drawing an outdated layout.
- Yunda INMS 主单打印 dialog (``printer/main/index.html``): ``printer/main/info``
  with the list row ``Id`` returns the label data, ``printer/getTemplate`` the
  C-Lodop template named by ``Print_Template``; the page replaces each data key
  once (``String.replace``) and runs the result. Printing logs through
  ``printer/insertPrintLog`` ``{method: main, print_data: [{Logistics_Id}]}``;
  INMS offers no read-back of that log.

The result is a closed list of C-Lodop drawing calls; the browser replays only
these calls, so no platform script runs in the Console origin.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.direct_business import DirectBusinessError
from agent.tms_runtime.direct_execution import call_blocking
from agent.tms_runtime.session_broker import get_session_broker

CHINA_TZ = ZoneInfo("Asia/Shanghai")
WAYBILL_NO_PATTERN = re.compile(r"[A-Za-z0-9-]{4,40}")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
PLATFORMS = {"ronghui", "yunda"}

# C-Lodop calls the two original pages use; the browser applies the same list.
LODOP_CALLS = frozenset({
    "PRINT_INITA", "SET_PRINT_PAGESIZE", "SET_PRINT_MODE", "SET_SHOW_MODE", "NEWPAGE",
    "ADD_PRINT_TEXT", "ADD_PRINT_HTM", "ADD_PRINT_LINE", "ADD_PRINT_IMAGE", "ADD_PRINT_BARCODE",
    "ADD_PRINT_RECT", "SET_PRINT_STYLEA", "SET_PREVIEW_WINDOW",
})

RONGHUI_ORIGIN = "https://tms.ronghuiwl.com"
RONGHUI_MENU_URL = f"{RONGHUI_ORIGIN}/menuTreeExtend/loadMenu"
RONGHUI_FIND_ALL_URL = f"{RONGHUI_ORIGIN}/dataQuery/findAllByCallId"
RONGHUI_SAVE_TABLES_URL = f"{RONGHUI_ORIGIN}/dataOperation/saveTables"
RONGHUI_PRINT_MENU = ("运单管理", "主面单打印")
RONGHUI_PRINT_CALL_ID = "FIDN_FACE_SINGLE_PRINT_ONE4"
RONGHUI_PRINT_COUNT_KEY = "TAB_PRINT_COUNT_ADD"
RONGHUI_LIAN_TYPE = "客户联"
# sha256 of the whitespace-free page functions (embedded images masked), 2026-11-18.
RONGHUI_CODE_FINGERPRINTS = {
    "printBillNew": "0079bf2fd542cacff42a68abf03d1d7c6a2df7bf51bc198e4a53da4ea989b5d0",
    "printKH": "d49d60cfa3cf1ca838ebd32029f3cc2394c367f934396d064cb4519c529f79fe",
}
RONGHUI_XCX_IMAGE = re.compile(
    r"ADD_PRINT_IMAGE\(242, 203, 63, 63, '(data:image/jpeg;base64,[A-Za-z0-9+/=]+)'\)")
_DATA_IMAGE = re.compile(r"'data:image/[a-z]+;base64,[A-Za-z0-9+/=]+'")

YUNDA_ORIGIN = "https://kyinms.yunda56.com"
YUNDA_PRINTER = f"{YUNDA_ORIGIN}/ky_inms/public/index.php/printer"
YUNDA_TEMPLATE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,40}")


class PlatformPrintError(DirectBusinessError):
    pass


# --------------------------------------------------------------------------- JS value semantics


def js_string(value: Any) -> str:
    """``String(value)`` as the original pages' JavaScript produces it."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def js_replace_first(text: str, pattern: str, replacement: str) -> str:
    """``text.replace(pattern, replacement)`` for a string pattern, including ``$`` codes."""
    index = text.find(pattern)
    if index < 0:
        return text
    before, after = text[:index], text[index + len(pattern):]
    out: list[str] = []
    i = 0
    while i < len(replacement):
        char = replacement[i]
        nxt = replacement[i + 1] if i + 1 < len(replacement) else ""
        if char == "$" and nxt in {"$", "&", "`", "'"}:
            out.append({"$": "$", "&": pattern, "`": before, "'": after}[nxt])
            i += 2
            continue
        out.append(char)
        i += 1
    return before + "".join(out) + after


def plus_xing(text: str, front: int, end: int) -> str:
    stars = "*" * max(0, len(text) - front - end)
    return text[:front] + stars + text[len(text) - end:]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _is_one(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value == 1


# --------------------------------------------------------------------------- call list


def _check_calls(calls: list[list[Any]]) -> list[list[Any]]:
    if not calls or calls[0][0] != "PRINT_INITA":
        raise PlatformPrintError("PLATFORM_LABEL_INVALID", "原站面单模板缺少打印初始化。")
    for name, args in calls:
        if name not in LODOP_CALLS:
            raise PlatformPrintError("PLATFORM_LABEL_INVALID", f"原站面单模板含未核实的打印指令 {name}。")
        if any(isinstance(arg, (list, dict)) or arg is None for arg in args):
            raise PlatformPrintError("PLATFORM_LABEL_INVALID", "原站面单模板参数格式无效。")
    return calls


_TOKEN = re.compile(r"""\s*(?:(-?\d+(?:\.\d+)?)|"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|(true|false))\s*(,|\))""", re.S)
_STATEMENT = re.compile(r"LODOP\.([A-Z_]+)\s*\(")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'", "/": "/"}


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda match: _ESCAPES.get(match.group(1), match.group(1)), text)


def parse_lodop_template(text: str) -> list[list[Any]]:
    """Parse a template made only of ``LODOP.NAME(literal, ...);`` statements."""
    calls: list[list[Any]] = []
    position = 0
    while True:
        rest = text[position:]
        if not rest.strip().strip(";").strip():
            return calls
        match = _STATEMENT.match(rest.lstrip().lstrip(";").lstrip())
        if not match:
            raise PlatformPrintError("PLATFORM_LABEL_INVALID", "原站面单模板含无法识别的语句。")
        position += len(rest) - len(rest.lstrip().lstrip(";").lstrip()) + match.end()
        args: list[Any] = []
        if text[position:].lstrip().startswith(")"):
            position += text[position:].index(")") + 1
        else:
            while True:
                token = _TOKEN.match(text, position)
                if not token:
                    raise PlatformPrintError("PLATFORM_LABEL_INVALID", "原站面单模板参数无法解析。")
                number, double, single, boolean, end = token.groups()
                if number is not None:
                    args.append(float(number) if "." in number else int(number))
                elif double is not None:
                    args.append(_unescape(double))
                elif single is not None:
                    args.append(_unescape(single))
                else:
                    args.append(boolean == "true")
                position = token.end()
                if end == ")":
                    break
        calls.append([match.group(1), args])


# --------------------------------------------------------------------------- Ronghui


def _walk_menu(nodes: Any, path: tuple[str, ...] = ()):
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        text = str(node.get("text") or node.get("name") or "").strip()
        current = (*path, text)
        yield node, current
        yield from _walk_menu(node.get("children"), current)


def _ronghui_headers(page: dict[str, str]) -> dict[str, str]:
    return {"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
            "Origin": RONGHUI_ORIGIN, "Referer": page["url"],
            "authenticationKey": page["authentication_key"], "pageId": page["page_id"]}


def _ronghui_print_page(session: Any, timeout_sec: int) -> dict[str, str]:
    response = session.get(RONGHUI_MENU_URL, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=timeout_sec)
    response.raise_for_status()
    payload = response.json()
    nodes = payload.get("result", {}).get("data") if isinstance(payload, dict) else None
    urls = {str(node.get("url") or "") for node, path in _walk_menu(nodes)
            if path[-2:] == RONGHUI_PRINT_MENU and "/widget/home" in str(node.get("url") or "")}
    if len(urls) != 1:
        raise PlatformPrintError("PLATFORM_PRINT_PAGE_UNAVAILABLE", "该融辉账号没有唯一的“主面单打印”菜单，无法读取原站面单。")
    url = urljoin(RONGHUI_ORIGIN, urls.pop())
    query = parse_qs(urlparse(url).query)
    html = session.get(url, timeout=timeout_sec).text
    page = {"url": url, "authentication_key": (query.get("authenticationKey") or [""])[0],
            "page_id": (query.get("pageId") or [""])[0], "html": html}
    if not page["authentication_key"] or not page["page_id"] or RONGHUI_PRINT_CALL_ID not in html:
        raise PlatformPrintError("PLATFORM_PRINT_PAGE_UNAVAILABLE", "融辉“主面单打印”页面未正常打开，请检查账号登录。")
    return page


def _segment(html: str, start: str, end: str) -> str:
    begin = html.find(start)
    stop = html.find(end, begin + 1) if begin >= 0 else -1
    return html[begin:stop] if begin >= 0 and stop > begin else ""


def ronghui_print_code(html: str) -> str:
    """Check the page's print functions are the reviewed ones; return the 小程序 image."""
    segments = {"printBillNew": _segment(html, "function printBillNew(", "var xcxImg"),
                "printKH": _segment(html, "function printKH(", "function searchMethod(")}
    for name, text in segments.items():
        digest = hashlib.sha256(re.sub(r"\s+", "", _DATA_IMAGE.sub("'<IMAGE>'", text)).encode("utf-8")).hexdigest()
        if not text or digest != RONGHUI_CODE_FINGERPRINTS[name]:
            raise PlatformPrintError("PLATFORM_PRINT_CODE_CHANGED",
                f"融辉原站面单打印代码（{name}）已变化，需重新核对后才能打印；请暂时在融辉原站打印。")
    image = RONGHUI_XCX_IMAGE.search(segments["printKH"])
    if not image:
        raise PlatformPrintError("PLATFORM_PRINT_CODE_CHANGED", "融辉原站面单缺少扫码下单图片，请暂时在融辉原站打印。")
    return image.group(1)


def _read_user_info(session: Any) -> dict[str, Any]:
    from agent.tms_runtime.ronghui_user_context import parse_ronghui_user_info_cookie

    for cookie in session.cookies:
        if cookie.name == "userInfo" and "ronghuiwl.com" in str(cookie.domain or ""):
            info = parse_ronghui_user_info_cookie(cookie.value)
            if info:
                return info
    return {}


def _ronghui_row(session: Any, page: dict[str, str], waybill_no: str, timeout_sec: int) -> dict[str, Any]:
    response = session.post(RONGHUI_FIND_ALL_URL, params={"id": RONGHUI_PRINT_CALL_ID}, data={"BILL_CODE": waybill_no},
                            headers=_ronghui_headers(page), timeout=timeout_sec)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise PlatformPrintError("PLATFORM_PRINT_DATA_INVALID", "融辉面单数据响应格式无效。")
    matched = [row for row in rows if isinstance(row, dict) and str(row.get("BILL_CODE") or "") == waybill_no]
    if len(matched) != 1:
        raise PlatformPrintError("PLATFORM_PRINT_DATA_NOT_FOUND",
            "该融辉账号在“主面单打印”中查不到这张运单。" if not matched else "融辉返回了多条同号运单，未打印。")
    return matched[0]


def _format_ronghui_date(value: Any) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?", str(value or "").strip())
    return f"{match[1]}/{match[2]}/{match[3]} {match[4]}:{match[5]}:{match[6]}" if match else ""


def ronghui_label_data(row: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """``printBillNew`` for one row with the page's default options."""

    def text(key: str, empty: str, prefix: str = "") -> str:
        value = row.get(key)
        return empty if value is None else prefix + js_string(value)

    data: dict[str, Any] = {"r3Arr": []}
    mode = row.get("DISPATCH_MODE")
    if mode is not None:
        data["DISPATCH_MODE"] = ("送货上楼" if _is_one(row.get("BL_UPSTAIRS")) else "送货（不含上楼）") if mode == "派送" else js_string(mode)
    else:
        data["DISPATCH_MODE"] = ""
    data["GOODS_TYPE"] = text("GOODS_TYPE", "")
    data["BILL_CODE"] = js_string(row.get("BILL_CODE"))
    data["BL_INSURESTATUS"] = row.get("BL_INSURESTATUS")
    data["BOROUGH"] = row.get("BOROUGH")
    data["NOW"] = "打印时间:" + now.strftime("%Y/%m/%d %H:%M:%S")
    data["REGISTER_DATE"] = "录单时间:" + _format_ronghui_date(row.get("REGISTER_DATE"))
    data["CONTACT_PHONE"] = "发货联系网点:" + js_string(row.get("CONTACT_PHONE"))
    sender = row.get("SEND_MAN")
    data["SEND_MAN"] = "委托人：" if sender is None else "委托人：" + js_string(sender)[:5]
    data["SEND_MAN_PHONE"] = text("SEND_MAN_PHONE", "联系方式：", "联系方式：")
    if row.get("SEND_MAN_PHONE_EXT") is not None:
        data["SEND_MAN_PHONE"] += " " + js_string(row["SEND_MAN_PHONE_EXT"])
    data["SEND_MAN_ADDRESS"] = text("SEND_MAN_ADDRESS", "")
    waiting = row.get("WAITNOTIFY_SEND_TEXT") == "是" and row.get("WAITNOTIFY_SEND_STATUS_TEXT") == "未放货"
    accept = row.get("ACCEPT_MAN")
    if accept is None:
        data["ACCEPT_MAN"] = "收件人："
    else:
        accept = js_string(accept)
        if waiting and len(accept) > 1:
            accept = plus_xing(accept, 1, 0)
        data["ACCEPT_MAN"] = "收件人：" + accept[:5]
    phone = row.get("ACCEPT_MAN_PHONE")
    if waiting and phone is not None and len(js_string(phone)) > 3:
        phone = plus_xing(js_string(phone), 3, 0)
    data["ACCEPT_MAN_PHONE"] = "联系方式：" if phone is None else "联系方式：" + js_string(phone)
    if row.get("ACCEPT_MAN_PHONE_EXT") is not None:
        data["ACCEPT_MAN_PHONE"] += " " + js_string(row["ACCEPT_MAN_PHONE_EXT"])
    address = row.get("ACCEPT_MAN_ADDRESS")
    if waiting and address is not None and len(js_string(address)) > 4:
        address = plus_xing(js_string(address), 4, 0)
    data["ACCEPT_MAN_ADDRESS"] = "" if address is None else js_string(address)
    data["TOPAYMENT"] = text("TOPAYMENT", "到付金额:0", "到付金额:")
    data["GOODS_PAYMENT"] = text("GOODS_PAYMENT", "代收金额:0", "代收金额:")
    data["INSURANCE"] = text("INSURANCE", "保价金额:0", "保价金额:")
    data["R_BILLCODE"] = text("R_BILLCODE", "回单号：", "回单号：")
    data["PIECE_NUMBER"] = text("PIECE_NUMBER", "件数：0", "件数：")
    data["FREIGHT"] = " "  # 页面默认不勾选“打印运费”
    data["SETTLEMENT_WEIGHT"] = text("SETTLEMENT_WEIGHT", "结算重量：0", "结算重量：")
    data["VOLUME"] = text("VOLUME", "体积：0", "体积：")
    data["REMARK"] = text("REMARK", "备注：", "备注：")
    data["FEE_WEIGHT"] = text("BILL_WEIGHT", "实际重量：0", "实际重量：")
    data["PACK_TYPE"] = text("PACK_TYPE", "打包类型：", "打包类型：")
    data["PRODUCT_TYPE"] = text("PRODUCT_TYPE", "")
    goods = row.get("GOODS_NAME")
    data["GOODS_NAME"] = "物品名称：" if goods is None else "物品名称：" + js_string(goods)[:4]
    marks = data["r3Arr"]
    if _is_one(row.get("WAITNOTIFY_SEND")):
        marks.append("等")
    if _is_one(row.get("BL_INTO_WAREHOUSE")):
        marks.append("仓")
    if row.get("GOODS_PAYMENT") is not None and (_number(row.get("GOODS_PAYMENT")) or 0) > 0:
        marks.append("代")
    if row.get("R_BILLCODE") is not None:
        marks.append("回")
    if row.get("TOPAYMENT") is not None and (_number(row.get("TOPAYMENT")) or 0) > 0:
        marks.append("到")
    if _is_one(row.get("BL_GOOD_PRINT")):
        marks.append("异")
    return data


def _black_box(size: int, label: str, *, padding: str = "") -> str:
    return ('<div id="printContent" style="background-color: black; color: white; display: flex; '
            'justify-content: center; alignItems: center; font-size: %dpx; font-weight: Bold;%s"><span>%s</span></div>'
            % (size, padding, label))


def _mark_box(label: str) -> str:
    return ('<div id="printContent" style="background-color: black; color: white; font-size: 14px; '
            'width: 15px; font-weight: Bold; padding: 2px;"><span>%s</span></div>' % label)


def ronghui_label_calls(data: dict[str, Any], *, xcx_image: str, site_type: str) -> list[list[Any]]:
    """``printBillNew`` page setup plus ``printKH(data, "客户联")``, call for call."""
    calls: list[list[Any]] = [
        ["PRINT_INITA", ["0mm", "0mm", "80mm", "100mm", "print"]],
        ["SET_PRINT_PAGESIZE", [0, "75mm", "90mm", "CreateCustomPage"]],
        ["NEWPAGE", []],
        ["SET_PRINT_STYLEA", [0, "Stretch", 1]],
    ]

    def text(top, left, width, height, value, size, bold=0, *, color=None, after=()):
        """ADD_PRINT_TEXT followed by the page's styles in the page's order."""
        calls.append(["ADD_PRINT_TEXT", [top, left, width, height, value]])
        calls.append(["SET_PRINT_STYLEA", [0, "FontName", "黑体"]])
        calls.append(["SET_PRINT_STYLEA", [0, "FontSize", size]])
        if color:
            calls.append(["SET_PRINT_STYLEA", [0, "FontColor", color]])
        calls.append(["SET_PRINT_STYLEA", [0, "Bold", bold]])
        calls.extend(["SET_PRINT_STYLEA", [0, key, value_]] for key, value_ in after)

    def line(top, left, bottom, right):
        calls.append(["ADD_PRINT_LINE", [top, left, bottom, right, 0, 1]])

    product = data.get("PRODUCT_TYPE")
    if product not in ("", None):
        calls.append(["ADD_PRINT_HTM", [1, 110, 80, 23, _black_box(14, js_string(product), padding=" padding: 3px 0px 3px 0px;")]])
    text(3, 220, 150, 20, RONGHUI_LIAN_TYPE, 12, 1)
    line(23, 0, 23, 300)
    text(25, 7.5, 150, 20, data["BILL_CODE"], 10, 1)
    text(5, 190, 150, 20, data["GOODS_TYPE"], 10, 1)
    status = data.get("BL_INSURESTATUS")
    if status is not None:
        status = _number(status)  # the page compares with ==
        if status == 1:
            calls.append(["ADD_PRINT_HTM", [26, 210, 25, 20, _black_box(10, "专车")]])
        elif status == 3:
            calls.append(["ADD_PRINT_HTM", [26, 210, 25, 20, _black_box(10, "自提")]])
        elif status == 2 and data.get("BOROUGH") is not None:
            borough = js_string(data["BOROUGH"])
            for code, left, width, label in (("1", 110, 25, "卸货"), ("2", 135, 25, "清点"), ("3", 160, 25, "拆托"),
                                             ("4", 185, 25, "进仓"), ("5", 210, 25, "码放"), ("6", 235, 50, "定时送货")):
                if code in borough:
                    calls.append(["ADD_PRINT_HTM", [26, left, width, 20, _black_box(10, label)]])
    line(41, 0, 40, 300)
    # The page draws index 4 twice (left 96 and 118) and never index 5.
    lefts = {0: [7.5], 1: [30], 2: [52], 3: [74], 4: [96, 118]}
    for index, mark in enumerate(data["r3Arr"]):
        for left in lefts.get(index, []):
            calls.append(["ADD_PRINT_HTM", [43, left, 20, 25, _mark_box(mark)]])
    text(46, 150, 150, 20, data["DISPATCH_MODE"], 10, 1, after=(("Alignment", 2),))
    line(64, 0, 64, 300)
    text(68, 7.5, 120, 10, data["SEND_MAN"], 9)
    text(68, 115, 180, 10, data["SEND_MAN_PHONE"], 9)
    text(84, 7.5, 280, 40, data["SEND_MAN_ADDRESS"], 9, after=(("LineSpacing", -3),))
    line(126, 0, 127, 300)
    text(130, 7.5, 120, 10, data["ACCEPT_MAN"], 9)
    text(130, 115, 180, 10, data["ACCEPT_MAN_PHONE"], 9)
    text(146, 7.5, 280, 40, data["ACCEPT_MAN_ADDRESS"], 9, after=(("LineSpacing", -3),))
    line(190, 0, 190, 300)
    code = data["BILL_CODE"]
    calls.append(["ADD_PRINT_BARCODE", [200, 20, 160, 25, "128Auto" if "HR" in code else "128A", code]])
    calls.append(["SET_PRINT_STYLEA", [0, "ShowBarText", 0]])
    text(226, 0, 170, 25, code, 7, after=(("Alignment", 2), ("LetterSpacing", 3)))
    text(195, 188, 200, 10, data["TOPAYMENT"], 7)
    text(210, 188, 200, 10, data["GOODS_PAYMENT"], 7)
    text(225, 188, 100, 10, data["INSURANCE"], 7)
    line(240, 0, 240, 300)
    text(244, 100, 100, 10, data["GOODS_NAME"], 7)
    text(255, 100, 100, 10, data["PACK_TYPE"], 7)
    text(267, 100, 100, 10, data["R_BILLCODE"], 7)
    text(279, 100, 100, 10, data["FREIGHT"], 7, color="red")
    text(244, 7.6, 100, 10, data["PIECE_NUMBER"], 7)
    text(255, 7.6, 100, 10, data["FEE_WEIGHT"], 7)
    text(267, 7.6, 100, 10, data["SETTLEMENT_WEIGHT"], 7)
    text(279, 7.6, 100, 10, data["VOLUME"], 7)
    line(291, 0, 291, 188)
    text(295, 7.6, 180, 20, data["REMARK"], 7)
    calls.append(["ADD_PRINT_IMAGE", [242, 203, 63, 63, xcx_image]])
    calls.append(["SET_PRINT_STYLEA", [0, "Stretch", 1]])
    text(305, 213, 180, 20, "扫码下单", 9)
    line(320, 0, 320, 300)
    text(322, 7.6, 228, 19, data["NOW"], 7)
    text(322, 140, 228, 19, data["REGISTER_DATE"], 7)
    if site_type in {"一级网点", "二级网点"}:
        text(332, 7.6, 228, 19, data["CONTACT_PHONE"], 7)
    calls.append(["SET_PREVIEW_WINDOW", [1, 2, 0, 550, 800, ""]])
    return calls


def _ronghui_session(session_profile: str) -> Any:
    return get_session_broker(session_profile).open_capability_session("ronghui_home", validate=False)


def ronghui_label(session_profile: str, waybill_no: str, *, timeout_sec: int, now: dt.datetime | None = None) -> dict[str, Any]:
    session = _ronghui_session(session_profile)
    page = _ronghui_print_page(session, timeout_sec)
    xcx_image = ronghui_print_code(page["html"])
    row = _ronghui_row(session, page, waybill_no, timeout_sec)
    info = _read_user_info(session)
    data = ronghui_label_data(row, now or dt.datetime.now(CHINA_TZ))
    calls = ronghui_label_calls(data, xcx_image=xcx_image, site_type=str(info.get("loginSiteType") or ""))
    return {"calls": _check_calls(calls), "print_count": row.get("PRINT_NUM")}


def ronghui_print_count_row(waybill_no: str, info: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    """The ``updateBillArr`` entry ``printBillNew`` saves after a real print."""
    missing = [key for key in ("loginEmpName", "loginEmpCode", "loginSiteName", "loginSiteCode")
               if not str(info.get(key) or "").strip()]
    if missing:
        raise PlatformPrintError("AUTH_REQUIRED", "融辉登录信息不完整，无法记录打印次数，请重新登录该账号。")
    created = now.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"MAIN_BILL_CODE": waybill_no, "BILL_CODE": waybill_no,
            "CREATE_MAN": info["loginEmpName"], "CREATE_MAN_CODE": info["loginEmpCode"],
            "CREATE_SITE": info["loginSiteName"], "CREATE_SITE_CODE": info["loginSiteCode"],
            "CREATE_DATE": created, "BILL_TYPE": "主单", "PRINT_TYPE": "寄件存根联",
            "DATA_FROM": "K13", "PAGE_FROM": "主面单打印"}


def ronghui_record_print(session_profile: str, waybill_no: str, *, timeout_sec: int) -> dict[str, Any]:
    session = _ronghui_session(session_profile)
    page = _ronghui_print_page(session, timeout_sec)
    ronghui_print_code(page["html"])
    before = _ronghui_row(session, page, waybill_no, timeout_sec).get("PRINT_NUM")
    if before is None:
        before = 0
    if isinstance(before, bool) or not isinstance(before, int):
        raise PlatformPrintError("PLATFORM_PRINT_DATA_INVALID", "融辉打印次数格式无效，未记录打印。")
    row = ronghui_print_count_row(waybill_no, _read_user_info(session), dt.datetime.now(CHINA_TZ))
    params = json.dumps([{"beforeAction": None, "operationKey": RONGHUI_PRINT_COUNT_KEY, "afterAction": None,
                          "idFields": [], "data": [row]}], ensure_ascii=False, separators=(",", ":"))
    headers = _ronghui_headers(page)
    response = session.post(RONGHUI_SAVE_TABLES_URL, files={"params": (None, params)}, headers=headers, timeout=timeout_sec)
    response.raise_for_status()
    try:
        ack = response.json() if response.content else {}
    except ValueError:
        ack = {}
    after = _ronghui_row(session, page, waybill_no, timeout_sec).get("PRINT_NUM")
    verified = isinstance(after, int) and not isinstance(after, bool) and after == before + 1
    return {"platform": "ronghui", "waybill_no": waybill_no, "submitted": bool(ack.get("success")) if isinstance(ack, dict) else False,
            "verified": verified, "print_count": after,
            "message": f"融辉打印次数已记录（第 {after} 次）。" if verified else
                       f"已向融辉提交打印记录，但回读打印次数为 {js_string(after)}（提交前 {before}），未能核实。"}


# --------------------------------------------------------------------------- Yunda


def _yunda_session(session_profile: str) -> Any:
    return get_session_broker(session_profile).open_capability_session("yunda_inms", validate=False)


def _yunda_headers(referer: str) -> dict[str, str]:
    return {"Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
            "Origin": YUNDA_ORIGIN, "Referer": referer,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}


def _yunda_row_id(session: Any, waybill_no: str, open_date: dt.date) -> str:
    from agent.tms_runtime.scripts import yunda_send_waybills as yunda

    rows, _total = yunda.collect_send_rows(session, {}, target_date=open_date, page_size=100, max_pages=50)
    matched = [row for row in rows if str(row.get("Logistics_Id") or "").strip() == waybill_no]
    if not matched:
        special, _ = yunda.collect_special_line_rows(session, {}, target_date=open_date, page_size=100, max_pages=50)
        if any(str(row.get("Logistics_Id") or "").strip() == waybill_no for row in special):
            raise PlatformPrintError("PLATFORM_PRINT_UNVERIFIED", "填仓寄件的韵达原站打印尚未核实，请在韵达原站打印。")
        raise PlatformPrintError("PLATFORM_PRINT_DATA_NOT_FOUND", "该韵达账号在开单当天的寄件列表里查不到这张运单。")
    ids = {str(row.get("Id") or "").strip() for row in matched}
    if len(ids) != 1 or not next(iter(ids)).isdigit():
        raise PlatformPrintError("PLATFORM_PRINT_DATA_INVALID", "韵达寄件列表中这张运单的记录不唯一，未打印。")
    return ids.pop()


def yunda_label_calls(data: dict[str, Any], template: str) -> list[list[Any]]:
    for key, value in data.items():
        template = js_replace_first(template, str(key), js_string(value))
    calls = parse_lodop_template(template)
    # The dialog's own preview hides its print button (prints go through the
    # dialog's 打印 button); here the preview is where the label is printed.
    return [call for call in calls if call != ["SET_SHOW_MODE", ["HIDE_PBUTTIN_PREVIEW", True]]]


def yunda_label(session_profile: str, waybill_no: str, open_date: dt.date, *, timeout_sec: int) -> dict[str, Any]:
    session = _yunda_session(session_profile)
    row_id = _yunda_row_id(session, waybill_no, open_date)
    referer = f"{YUNDA_PRINTER}/main/index.html?ids={row_id}"
    response = session.post(f"{YUNDA_PRINTER}/main/info.html", data={"ids": row_id, "getTemplateType": "new"},
                            headers=_yunda_headers(referer), allow_redirects=False, timeout=timeout_sec)
    response.raise_for_status()
    rows = response.json()
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or str(rows[0].get("Logistics_Id") or "") != waybill_no):
        raise PlatformPrintError("PLATFORM_PRINT_DATA_INVALID", "韵达返回的面单数据与运单不一致，未打印。")
    data = rows[0]
    name = str(data.get("Print_Template") or "")
    if not YUNDA_TEMPLATE_NAME.fullmatch(name):
        raise PlatformPrintError("PLATFORM_PRINT_DATA_INVALID", "韵达面单未指定有效的打印模板。")
    response = session.post(f"{YUNDA_PRINTER}/getTemplate/.html", data={"method": name},
                            headers=_yunda_headers(referer), allow_redirects=False, timeout=timeout_sec)
    response.raise_for_status()
    templates = response.json()
    template = templates.get(name) if isinstance(templates, dict) else None
    if not isinstance(template, str) or not template.strip():
        raise PlatformPrintError("PLATFORM_PRINT_DATA_INVALID", "未获取到韵达打印模板，请稍后重试。")
    return {"calls": _check_calls(yunda_label_calls(data, template)), "print_template": name}


def yunda_record_print(session_profile: str, waybill_no: str, open_date: dt.date, *, timeout_sec: int) -> dict[str, Any]:
    session = _yunda_session(session_profile)
    row_id = _yunda_row_id(session, waybill_no, open_date)
    response = session.post(f"{YUNDA_PRINTER}/insertPrintLog/.html",
                            data={"method": "main", "print_data[0][Logistics_Id]": waybill_no},
                            headers=_yunda_headers(f"{YUNDA_PRINTER}/main/index.html?ids={row_id}"),
                            allow_redirects=False, timeout=timeout_sec)
    accepted = response.status_code == 200
    try:
        ack = response.json()
    except ValueError:
        ack = None
    if isinstance(ack, dict) and "status" in ack and str(ack.get("status")) != "1":
        accepted = False
    return {"platform": "yunda", "waybill_no": waybill_no, "submitted": accepted, "verified": False,
            "message": "已向韵达提交打印记录；韵达原站不提供打印记录回读，无法核实。" if accepted else
                       f"韵达打印记录提交失败（HTTP {response.status_code}）。"}


# --------------------------------------------------------------------------- business entry points


def _validated(params: dict[str, Any]) -> tuple[str, str, str, dt.date | None]:
    if set(params) != {"platform", "waybill_no", "account_id", "open_date"} or not all(
            isinstance(params[key], str) for key in params):
        raise PlatformPrintError("INVALID_PLATFORM_PRINT_QUERY", "原站面单只接受平台、运单号、账号和开单日期。")
    platform, waybill_no = params["platform"].strip(), params["waybill_no"].strip()
    account_id, open_date = params["account_id"].strip(), params["open_date"].strip()
    if platform not in PLATFORMS or not WAYBILL_NO_PATTERN.fullmatch(waybill_no) or not account_id:
        raise PlatformPrintError("INVALID_PLATFORM_PRINT_QUERY", "原站面单的平台、运单号或账号无效。")
    day = None
    if platform == "yunda":
        if not DATE_PATTERN.fullmatch(open_date):
            raise PlatformPrintError("INVALID_PLATFORM_PRINT_QUERY", "韵达面单需要开单日期。")
        day = dt.date.fromisoformat(open_date)
    return platform, waybill_no, account_id, day


def _platform_call(platform: str, action):
    """Run one platform exchange; login and transport failures become clear messages."""
    import requests

    from agent.tms_runtime.errors import TMSAuthStateError

    name = "融辉" if platform == "ronghui" else "韵达"
    try:
        return action()
    except PlatformPrintError:
        raise
    except TMSAuthStateError as exc:
        raise PlatformPrintError(exc.code, f"{name}账号登录状态不可用：{exc}") from exc
    except requests.RequestException as exc:
        raise PlatformPrintError("PLATFORM_UNREACHABLE", f"{name}接口请求失败（{type(exc).__name__}），请稍后重试。") from exc
    except ValueError as exc:
        raise PlatformPrintError("PLATFORM_PRINT_DATA_INVALID", f"{name}返回的数据无法解析，请稍后重试。") from exc


def _session_profile(platform: str, account_id: str) -> str:
    descriptor = get_account_manager().require_active_binding_descriptor(account_id)
    if descriptor["system"] != platform or not descriptor["session_profile"]:
        raise PlatformPrintError("ACCOUNT_PLATFORM_MISMATCH", "运单记录的同步账号与平台不一致。")
    return descriptor["session_profile"]


async def platform_label_async(params: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    platform, waybill_no, account_id, day = _validated(params)
    read_timeout = min(int(timeout_sec), 30)

    def read() -> dict[str, Any]:
        profile = _platform_call(platform, lambda: _session_profile(platform, account_id))
        label = _platform_call(platform, lambda: (
            ronghui_label(profile, waybill_no, timeout_sec=read_timeout) if platform == "ronghui"
            else yunda_label(profile, waybill_no, day, timeout_sec=read_timeout)))
        return {"platform": platform, "waybill_no": waybill_no, **label}

    return await call_blocking(read, timeout_sec=timeout_sec)


def build_print_log_handler(invocations: Any):
    """Write the platform's print record once, journaled as a Console business write."""

    async def handler(params: dict[str, Any], principal: dict[str, Any], request_id: str, timeout_sec: int) -> dict[str, Any]:
        platform, waybill_no, account_id, day = _validated(params)
        write_timeout = min(int(timeout_sec), 30)

        async def execute() -> dict[str, Any]:
            def write() -> dict[str, Any]:
                profile = _platform_call(platform, lambda: _session_profile(platform, account_id))
                return _platform_call(platform, lambda: (
                    ronghui_record_print(profile, waybill_no, timeout_sec=write_timeout) if platform == "ronghui"
                    else yunda_record_print(profile, waybill_no, day, timeout_sec=write_timeout)))
            return await call_blocking(write, timeout_sec=timeout_sec)

        result = await invocations.call_business(
            operation="platform-waybill-print-log", request_id=request_id, actor_id=str(principal["actor_id"]),
            source="console", arguments=dict(params), handler=execute, write=True,
            verify=lambda outcome: outcome.get("verified") is True,
            account_ids=(account_id,), resource_keys=(("business", "platform-waybill-print-log", account_id, waybill_no),))
        data = result.get("result") if isinstance(result.get("result"), dict) else {}
        return {"ok": result.get("success") is True, "data": data, "invocation_id": result.get("invocation_id"),
                "status": result.get("status"), "error_code": result.get("error_code"),
                "error": result.get("error_summary") or data.get("message")}

    return handler
