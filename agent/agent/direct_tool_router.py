"""确定性文本指令路由：价格、打卡、统计等。"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from agent.tms_runtime.account_contracts import PRICE_ACCOUNT_ID
from agent.tracking_number_validation import validate_tracking_number

PRICE_PREFIX_RE = re.compile(r"^\s*(?:报价|价格)(?:查询)?\s*[:：]?\s*(.+?)\s*$", re.IGNORECASE)
PRICE_SPLIT_RE = re.compile(r"[，,；;]")
TRACKING_QUERY_RE = re.compile(
    r"^\s*(?P<prefix>查(?:单号|运单|物流|快递)|单号查询|运单查询|物流查询|韵达|融辉)\s*[:：]?\s*(?P<code>[A-Za-z0-9]+)\s*$",
    re.IGNORECASE,
)
BARE_TRACKING_RE = re.compile(
    r"^\s*(?P<code>RC[A-Za-z0-9]+|R\d[A-Za-z0-9]*|200\d+|000\d+|\d{9,})\s*$",
    re.IGNORECASE,
)

DEPRECATED_SPLIT_COMMAND_RE = re.compile(
    r"^\s*(?:分批问题件|(?:上报|投诉)?分批差错(?:上报)?|上传分批/未到问题件)\s*$",
    re.IGNORECASE,
)

SELF_PICKUP_PROBLEM_UPLOAD_RE = re.compile(
    r"^(?=.*(?:自提(?:部)?|大祥S站|大祥S站自提))(?=.*问题件)(?=.*(?:到货|上传)).*$|^(?=.*开单为自提件)(?=.*问题件).*$",
    re.IGNORECASE,
)
SELF_PICKUP_PROBLEM_LABEL = "自提到货问题件"

SPLIT_PENDING_PROBLEM_UPLOAD_RE = re.compile(
    r"^\s*分批\s*$",
    re.IGNORECASE,
)
SPLIT_PENDING_PROBLEM_LABEL = "分批差错及问题件"

# These aliases identify one explicit migration instance route. They are not
# plugin or tool selectors: the Feishu adapter resolves the alias through the
# committed ``feishu_route`` resource and rejects missing or duplicate owners.
# A repeated installation receives a different administrator-configured alias.
FIRST_PARTY_FEISHU_ROUTE_KEYS = {
    "r7_arrival_checkin": "builtin.r7_arrival_checkin",
    "r7_departure_checkin": "builtin.r7_departure_checkin",
    "sync_scan_codes": "builtin.scan_codes",
    "sync_arrive_list": "builtin.arrive_list",
    "sync_daily_send_orders": "builtin.send_order",
    "sync_yunda_send_waybills": "builtin.yunda_send_waybills",
    "sync_yunda_dispatch_forecast": "builtin.yunda_dispatch_forecast",
    "sync_arrival_stats": "builtin.arrival_stats",
    "preview_self_pickup_problems": "builtin.self_pickup_problem_upload",
    "self_pickup_problem_upload": "builtin.self_pickup_problem_upload",
    "preview_split_pending_problems": "builtin.split_pending_problem_upload",
    "split_pending_problem_upload": "builtin.split_pending_problem_upload",
}


def _automation_project_request(
    tool_name: str,
    *,
    mode: str = "automation_project",
    dynamic_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one code-owned route request without action/account arguments."""

    return {
        "tool_name": tool_name,
        "params": {},
        "mode": mode,
        "automation_route_key": FIRST_PARTY_FEISHU_ROUTE_KEYS[tool_name],
        "dynamic_inputs": dict(dynamic_inputs or {}),
    }


def is_deprecated_split_command(text: str) -> bool:
    normalized = str(text or "").strip()
    return bool(DEPRECATED_SPLIT_COMMAND_RE.match(normalized))

ARRIVAL_STATS_RE = re.compile(
    r"^(?=.*统计)[\s统计到货数据刷新更新]+$"
)

SCAN_SYNC_RE = re.compile(
    r"^\s*(?:扫描(?:数据)?|执行\s*扫描(?:数据)?|开始\s*扫描(?:数据)?|(?:执行\s*)?获取并\s*扫描(?:数据)?|同步\s*扫描(?:数据)?|扫描\s*同步)\s*$",
    re.IGNORECASE,
)

ARRIVE_LIST_SYNC_RE = re.compile(
    r"^\s*(?:请|麻烦|帮我)?\s*(?:执行|运行|跑|同步|拉取|获取|更新|刷新)?\s*(?:一次|一下)?\s*(?:arrive[-_\s]*list|arrivelist|arrival[-_\s]*list|到货清单|预到达清单|预到达)\s*(?:脚本|同步|数据|任务|清单)?\s*$",
    re.IGNORECASE,
)

SEND_ORDER_SYNC_RE = re.compile(
    r"(?=.*(?:获取当日寄件数据|当日寄件数据|融辉寄件数据|TMS寄件数据|sync[-_\s]*daily[-_\s]*send[-_\s]*orders?)).*",
    re.IGNORECASE,
)

DISPATCH_FORECAST_SYNC_RE = re.compile(
    r"^\s*(?:请|麻烦|帮我)?\s*(?:执行|运行|跑|同步|拉取|获取|更新|刷新)?\s*(?:一次|一下)?\s*(?:(?:韵达|融辉)?\s*(?:网点)?派件量?预测|网点派件量预测主单表|应派预测)\s*(?:主单表|脚本|同步|数据|任务|清单)?\s*$",
    re.IGNORECASE,
)

YUNDA_PROFILE_HINT_RE = re.compile(r"(?:韵达|yunda|网点派件量预测主单表)", re.IGNORECASE)
YUNDA_SEND_WAYBILL_SYNC_RE = re.compile(
    r"(?=.*(?:韵达|yunda))(?=.*(?:寄件运单|寄件运单管理|send[-_\s]*waybills?)).*",
    re.IGNORECASE,
)
DATE_TEXT_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:年|-|/|\.)\s*(?P<month>\d{1,2})\s*(?:月|-|/|\.)\s*(?P<day>\d{1,2})\s*(?:日|号)?"
)
RONGHUI_PROFILE_HINT_RE = re.compile(r"(?:融辉|ronghui)", re.IGNORECASE)

R7_ARRIVAL_CHECKIN_RE = re.compile(
    r"^\s*(?:R7\s*)?到达\s*打卡\s*$",
    re.IGNORECASE,
)

R7_DEPARTURE_CHECKIN_RE = re.compile(
    r"^\s*(?:R7\s*)?(?:发车|发车\s*打卡)\s*$",
    re.IGNORECASE,
)

CONFIRM_RE = re.compile(
    r"^\s*(?:确认|确定|是的|是|对|好的|好|执行|继续|同意|ok|yes|y)\s*[!！。.~]*\s*$",
    re.IGNORECASE,
)
CANCEL_RE = re.compile(
    r"^\s*(?:取消|否|不|不要|算了|放弃|拒绝|no|cancel|n)\s*[!！。.~]*\s*$",
    re.IGNORECASE,
)

LOGIN_SEND_CODE_RE = re.compile(
    r"^\s*(?:请|麻烦|帮我)?\s*(?:"
    r"(?:韵达|yunda|大祥|操作场|报价|价格|price|TMS|后台保存账号|后台保存|后台|系统|账号|账户|登录态)?\s*(?:重新)?(?:登录|登陆)(?:\s*(?:TMS|后台|系统|账号|账户|登录态))?"
    r"|(?:韵达|yunda|大祥|操作场|报价|价格|price|TMS|后台保存账号|后台保存|后台|系统|账号|账户|登录态)?\s*(?:重新)?(?:登录|登陆|验证|恢复|刷新)"
    r"|(?:韵达|yunda|大祥|操作场|报价|价格|price|TMS|后台保存账号|后台保存|后台|系统|账号|账户|登录态)?\s*(?:发送|发|获取|申请|重发|再发)\s*(?:短信)?验证码"
    r"|(?:韵达|yunda|大祥|操作场|报价|价格|price)?\s*(?:短信)?验证码(?:\s*(?:登录|登陆|验证))?"
    r")\s*$",
    re.IGNORECASE,
)
LOGIN_YUNDA_RE = re.compile(r"(?:韵达|yunda)", re.IGNORECASE)
LOGIN_DAXIANG_RE = re.compile(r"(?:大祥|报价|价格|price)", re.IGNORECASE)
LOGIN_OPERATOR_RE = re.compile(r"(?:操作场|后台|保存的账号|后台保存)", re.IGNORECASE)
LOGIN_CHOICE_DAXIANG_RE = re.compile(r"^\s*(?:1|大祥|大祥账号|报价账号|价格账号|price)\s*$", re.IGNORECASE)
LOGIN_CHOICE_OPERATOR_RE = re.compile(r"^\s*(?:2|操作场|操作场账号|后台账号|后台保存账号|默认账号)\s*$", re.IGNORECASE)
LOGIN_CHOICE_YUNDA_RE = re.compile(r"^\s*(?:3|韵达|韵达账号|yunda)\s*$", re.IGNORECASE)

AUTOMATION_PROFILE_STATUS_RE = re.compile(
    r"^\s*(?:当前)?(?:后台)?自动化(?:profile|Profile|状态)?\s*$|^\s*当前自动化状态\s*$",
    re.IGNORECASE,
)
AUTOMATION_PROFILE_SWITCH_RE = re.compile(
    r"^\s*(?:切换到|切换为|使用|启用)\s*(?P<profile>融辉|ronghui|韵达|yunda)\s*(?:后台)?自动化\s*$",
    re.IGNORECASE,
)

EXECUTION_REQUEST_RE = re.compile(
    r"(?:执行|运行|跑|同步|拉取|获取|更新|刷新|打卡|上报|统计|扫描|报价|查询|查|登录|登陆|验证码|arrive[-_\s]*list|arrivelist|到货清单|预到达清单)",
    re.IGNORECASE,
)
UNVERIFIED_EXECUTION_REPLY_RE = re.compile(
    r"(?:已完成|执行完成|同步完成|运行完成|执行结果|写入(?:MySQL|飞书|数据库|表格)|成功获取|成功拉取)",
    re.IGNORECASE,
)
NEGATED_EXECUTION_REPLY_RE = re.compile(
    r"(?:未执行|没有执行|无法执行|不能执行|没有权限|未调用|还没有调用|需要.*工具)",
    re.IGNORECASE,
)


VERIFY_CODE_RE = re.compile(r"^\s*([A-Za-z0-9]{4,8})\s*$")
WRAPPING_QUOTES = (
    ("“", "”"),
    ("‘", "’"),
    ('"', '"'),
    ("'", "'"),
    ("「", "」"),
    ("『", "』"),
)


def _normalize_command_text(text: str) -> str:
    normalized = str(text or "").replace("\u200b", "").replace("\ufeff", "").strip()
    changed = True
    while changed and normalized:
        changed = False
        for left, right in WRAPPING_QUOTES:
            if normalized.startswith(left) and normalized.endswith(right):
                normalized = normalized[len(left): len(normalized) - len(right)].strip()
                changed = True
                break
    return normalized


def _extract_date_params(text: str) -> dict[str, str]:
    dates: list[str] = []
    for match in DATE_TEXT_RE.finditer(text):
        try:
            parsed = dt.date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            continue
        dates.append(parsed.isoformat())
    if len(dates) >= 2:
        return {"start_date": dates[0], "end_date": dates[1]}
    if len(dates) == 1:
        return {"target_date": dates[0]}
    return {}


def is_confirm_text(text: str) -> bool:
    return bool(CONFIRM_RE.match(str(text or "")))


def is_cancel_text(text: str) -> bool:
    return bool(CANCEL_RE.match(str(text or "")))


def parse_verify_code(text: str) -> str | None:
    match = VERIFY_CODE_RE.match(str(text or ""))
    return match.group(1) if match else None


def parse_login_send_code_session(text: str) -> str | None:
    normalized = _normalize_command_text(text)
    if not normalized or not LOGIN_SEND_CODE_RE.match(normalized):
        return None
    if LOGIN_YUNDA_RE.search(normalized):
        return "yunda"
    if LOGIN_DAXIANG_RE.search(normalized):
        return "price"
    if LOGIN_OPERATOR_RE.search(normalized):
        return "default"
    return "choice"


def parse_login_account_choice(text: str) -> str | None:
    normalized = _normalize_command_text(text)
    if LOGIN_CHOICE_DAXIANG_RE.match(normalized):
        return "price"
    if LOGIN_CHOICE_OPERATOR_RE.match(normalized):
        return "default"
    if LOGIN_CHOICE_YUNDA_RE.match(normalized):
        return "yunda"
    return None


def _automation_profile_from_text(text: str) -> str:
    if YUNDA_PROFILE_HINT_RE.search(text):
        return "yunda"
    if RONGHUI_PROFILE_HINT_RE.search(text):
        return "ronghui"
    try:
        from agent.automation_profile import get_current_profile

        return get_current_profile()
    except Exception:
        return "ronghui"


def is_execution_request(text: str) -> bool:
    return bool(EXECUTION_REQUEST_RE.search(_normalize_command_text(text)))


def looks_like_unverified_execution_reply(text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    if NEGATED_EXECUTION_REPLY_RE.search(content):
        return False
    return bool(UNVERIFIED_EXECUTION_REPLY_RE.search(content))


def parse_number_token(raw_value: str, *, number_kind: str) -> float | None:
    value = str(raw_value or "").strip().lower()
    if not value:
        return None

    if number_kind == "weight":
        suffixes = ("kg", "公斤", "千克")
    elif number_kind == "volume":
        suffixes = ("m³", "m3", "m^3", "立方", "方")
    else:
        suffixes = ("元", "rmb", "cny")

    for suffix in suffixes:
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
            break

    value = value.replace("，", "").replace(",", "")
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def parse_volume_expression_token(raw_value: str) -> float | None:
    expression = re.sub(r"\s+", "", str(raw_value or "").strip())
    if not expression or ("*" not in expression and "+" not in expression):
        return None

    total = Decimal("0")
    try:
        for group in expression.split("+"):
            factors = [part.strip() for part in group.split("*") if part.strip()]
            if len(factors) != 4:
                return None
            length, width, height, quantity = (Decimal(factor) for factor in factors)
            if (
                not all(factor.is_finite() for factor in (length, width, height, quantity))
                or length <= 0
                or width <= 0
                or height <= 0
                or quantity <= 0
            ):
                return None
            total += length * width * height * quantity
    except (InvalidOperation, ValueError):
        return None

    volume = (total / Decimal("1000000")).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return float(volume) if volume > 0 else None


def parse_volume_token(raw_value: str) -> float | None:
    parsed = parse_number_token(raw_value, number_kind="volume")
    if parsed is not None:
        return parsed
    return parse_volume_expression_token(raw_value)


def parse_price_request(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None

    matched = PRICE_PREFIX_RE.match(raw)
    candidate = matched.group(1).strip() if matched else raw
    if not PRICE_SPLIT_RE.search(candidate):
        return None

    parts = [part.strip() for part in PRICE_SPLIT_RE.split(candidate) if part.strip()]
    if len(parts) < 2:
        return None

    if (
        len(parts) >= 4
        and parse_number_token(parts[-3], number_kind="weight") is not None
        and parse_volume_token(parts[-2]) is not None
        and parse_number_token(parts[-1], number_kind="amount") is not None
    ):
        return None

    volume = None
    weight = parse_number_token(parts[-1], number_kind="weight")
    if len(parts) >= 3:
        address = ""
        if not address:
            possible_volume = parse_volume_token(parts[-1])
            possible_weight = parse_number_token(parts[-2], number_kind="weight")
        if not address and possible_volume is not None and possible_weight is not None:
            volume = possible_volume
            weight = possible_weight
            address = ",".join(parts[:-2]).strip()
        elif not address:
            address = ",".join(parts[:-1]).strip()
    else:
        address = parts[0].strip()

    if not address or weight is None:
        return None

    params: dict[str, Any] = {"address": address, "weight": weight}
    if volume is not None:
        params["volume"] = volume
    return params


def _tracking_provider(code: str, prefix: str = "") -> str | None:
    return validate_tracking_number(code, provider_hint=prefix).provider or None


def parse_tracking_request(text: str) -> dict[str, Any] | None:
    normalized = _normalize_command_text(text)
    if not normalized:
        return None
    match = TRACKING_QUERY_RE.match(normalized)
    prefix = ""
    if match:
        prefix = match.group("prefix") or ""
        code = match.group("code") or ""
    else:
        bare = BARE_TRACKING_RE.match(normalized)
        if not bare:
            return None
        code = bare.group("code") or ""
    validation = validate_tracking_number(code, provider_hint=prefix)
    if not validation.provider and not validation.error:
        return None
    params = validation.params()
    if validation.error:
        params["_local_result"] = {
            "success": False,
            **validation.error_result(),
        }
    return params


def direct_tool_request_from_text(text: str) -> dict[str, Any] | None:
    normalized = _normalize_command_text(text)
    if not normalized:
        return None

    switch_match = AUTOMATION_PROFILE_SWITCH_RE.match(normalized)
    if switch_match:
        profile = _automation_profile_from_text(str(switch_match.group("profile") or ""))
        return {
            "tool_name": "automation_profile",
            "params": {"action": "set", "profile": profile},
            "mode": "reply",
        }

    if AUTOMATION_PROFILE_STATUS_RE.match(normalized):
        return {
            "tool_name": "automation_profile",
            "params": {"action": "get"},
            "mode": "reply",
        }

    tracking_params = parse_tracking_request(normalized)
    if tracking_params:
        local_result = tracking_params.pop("_local_result", None)
        request = {
            "tool_name": "track_waybill",
            "params": tracking_params,
            "mode": "reply",
        }
        if local_result:
            request["local_result"] = local_result
        return request

    price_params = parse_price_request(normalized)
    if price_params:
        return {
            "tool_name": "get_price",
            "params": {**price_params, "account_id": PRICE_ACCOUNT_ID},
            "mode": "reply",
        }

    if SPLIT_PENDING_PROBLEM_UPLOAD_RE.match(normalized):
        return {
            **_automation_project_request(
                "preview_split_pending_problems",
                mode="automation_preview",
            ),
            "selection_intent": {
                "description": SPLIT_PENDING_PROBLEM_LABEL,
            },
        }

    if SELF_PICKUP_PROBLEM_UPLOAD_RE.match(normalized):
        return {
            **_automation_project_request(
                "preview_self_pickup_problems",
                mode="automation_preview",
            ),
            "confirm_intent": {
                "dynamic_inputs": {"dry_run": False},
                "description": SELF_PICKUP_PROBLEM_LABEL,
            },
        }

    if R7_ARRIVAL_CHECKIN_RE.match(normalized):
        return _automation_project_request("r7_arrival_checkin")

    if R7_DEPARTURE_CHECKIN_RE.match(normalized):
        return _automation_project_request(
            "r7_departure_checkin",
            mode="r7_departure_choice",
        )

    if SCAN_SYNC_RE.match(normalized):
        return _automation_project_request("sync_scan_codes")

    if ARRIVE_LIST_SYNC_RE.match(normalized):
        return _automation_project_request("sync_arrive_list")

    if SEND_ORDER_SYNC_RE.search(normalized):
        return _automation_project_request(
            "sync_daily_send_orders",
            dynamic_inputs=_extract_date_params(normalized),
        )

    if YUNDA_SEND_WAYBILL_SYNC_RE.search(normalized):
        return _automation_project_request(
            "sync_yunda_send_waybills",
            dynamic_inputs=_extract_date_params(normalized),
        )

    if DISPATCH_FORECAST_SYNC_RE.match(normalized):
        profile = _automation_profile_from_text(normalized)
        if profile == "yunda":
            return _automation_project_request(
                "sync_yunda_dispatch_forecast",
                dynamic_inputs=_extract_date_params(normalized),
            )
        return _automation_project_request("sync_arrive_list")

    if ARRIVAL_STATS_RE.match(normalized):
        return _automation_project_request("sync_arrival_stats")
    return None


def format_tool_reply(tool_name: str, result: dict[str, Any]) -> str:
    if tool_name == "track_waybill":
        return format_track_waybill_reply(result)
    if tool_name == "get_price":
        return format_price_reply(result)
    if tool_name in {"preview_split_pending_problems", "split_pending_problem_upload"}:
        return format_split_pending_problem_upload_reply(result)
    if tool_name in {"preview_self_pickup_problems", "self_pickup_problem_upload"}:
        return format_self_pickup_problem_upload_reply(result)
    if tool_name == "sync_scan_codes":
        return format_scan_sync_reply(result)
    if tool_name == "sync_arrive_list":
        return format_arrive_list_sync_reply(result)
    if tool_name == "sync_daily_send_orders":
        return format_send_order_sync_reply(result)
    if tool_name == "sync_arrival_stats":
        return format_arrival_stats_reply(result)
    if tool_name == "sync_yunda_dispatch_forecast":
        return format_yunda_dispatch_forecast_reply(result)
    if tool_name == "sync_yunda_send_waybills":
        return format_yunda_send_waybills_reply(result)
    if tool_name == "automation_profile":
        return format_automation_profile_reply(result)
    if tool_name == "r7_arrival_checkin":
        return format_r7_arrival_checkin_reply(result)
    if tool_name == "r7_departure_checkin":
        return format_r7_departure_checkin_reply(result)
    return _format_generic_reply(result)


def format_tool_reply_messages(tool_name: str, result: dict[str, Any]) -> list[str]:
    if tool_name == "get_price":
        return format_price_reply_messages(result)
    return [format_tool_reply(tool_name, result)]


def _route_rows_newest_first(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    normalized = [row for row in rows if isinstance(row, dict)]
    return sorted(normalized, key=lambda row: str(row.get("scan_time") or row.get("time") or ""), reverse=True)


YUNDA_TRANSPORT_VOUCHER_RE = re.compile(r"\s*[，,;；]?\s*凭证号[:：].*$")
YUNDA_BRACKETED_SITE_RE = re.compile(r"【([^】]+)】")
YUNDA_NETWORK_HANDOFF_KEYWORDS = ("正发往", "发往", "上一站是")
YUNDA_PROBLEM_ROUTE_KEYWORDS = ("【问题】", "问题扫描", "运单调整审核", "问题件")
OPENING_ROUTE_KEYWORDS = ("开单", "揽收", "收件", "发件扫描", "发件")
DAXIANG_OPENING_STATIONS = {"邵阳大祥站", "邵阳大祥S站"}


def _strip_yunda_transport_voucher(description: str) -> str:
    stripped = YUNDA_TRANSPORT_VOUCHER_RE.sub("", description).strip()
    return stripped or description


def _clean_reply_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _route_station(row: dict[str, Any]) -> str:
    for key in ("scan_station", "station_name", "node_name", "current_station", "site_name"):
        value = _clean_reply_text(row.get(key))
        if value:
            return value
    return ""


def _route_description(row: dict[str, Any], payload_type: str) -> str:
    description = _clean_reply_text(row.get("description"))
    if payload_type == "yunda" and description:
        description = _strip_yunda_transport_voucher(description)
    return description or _clean_reply_text(row.get("status") or row.get("type")) or "无数据"


def _format_route_summary(title: str, row: dict[str, Any], payload_type: str) -> str:
    station = _route_station(row)
    scan_time = _clean_reply_text(row.get("scan_time") or row.get("time"))
    description = _route_description(row, payload_type)
    contact = _clean_reply_text(row.get("contact"))

    lines = [f"{title}："]
    if station:
        lines.append(f"网点信息：{station}")
    lines.append(f"扫描时间：{scan_time or '无数据'}")
    lines.append(f"路由信息：{description}")
    lines.append(f"货物跟踪查询电话：{contact or '无数据'}")
    return "\n".join(lines)


def _yunda_route_match_text(row: dict[str, Any]) -> str:
    values = [
        _clean_reply_text(row.get(key))
        for key in ("status", "type", "description", "scan_station")
        if _clean_reply_text(row.get(key))
    ]
    return " ".join(values)


def _is_yunda_problem_route(row: dict[str, Any]) -> bool:
    route_text = _yunda_route_match_text(row)
    return any(keyword in route_text for keyword in YUNDA_PROBLEM_ROUTE_KEYWORDS)


def _is_yunda_network_handoff_route(row: dict[str, Any]) -> bool:
    if _is_yunda_problem_route(row):
        return False
    description = _route_description(row, "yunda")
    bracketed_sites = [
        site
        for site in YUNDA_BRACKETED_SITE_RE.findall(description)
        if site and site not in {"问题"}
    ]
    return len(bracketed_sites) >= 2 and any(
        keyword in description for keyword in YUNDA_NETWORK_HANDOFF_KEYWORDS
    )


def _latest_route_rows_for_reply(rows_newest_first: list[dict[str, Any]], payload_type: str) -> list[dict[str, Any]]:
    if not rows_newest_first:
        return []
    if payload_type != "yunda" or not _is_yunda_problem_route(rows_newest_first[0]):
        return rows_newest_first[:1]

    selected: list[dict[str, Any]] = []
    for row in rows_newest_first:
        selected.append(row)
        if len(selected) > 1 and _is_yunda_network_handoff_route(row):
            break
    return selected


def _latest_route_title(index: int) -> str:
    return "最新路由" if index == 0 else f"前序路由{index}"


def _oldest_opening_route(rows_newest_first: list[dict[str, Any]]) -> dict[str, Any]:
    rows_oldest_first = list(reversed(rows_newest_first))
    for row in rows_oldest_first:
        route_text = " ".join(
            _clean_reply_text(row.get(key))
            for key in ("status", "type", "description")
            if _clean_reply_text(row.get(key))
        )
        if any(keyword in route_text for keyword in OPENING_ROUTE_KEYWORDS):
            return row
    return rows_oldest_first[0] if rows_oldest_first else {}


def _waybill_stub(payload: dict[str, Any]) -> dict[str, Any]:
    stub = payload.get("waybill_stub")
    return stub if isinstance(stub, dict) else {}


def _waybill_info_value(payload: dict[str, Any], *labels: str) -> str:
    wanted = {label.strip() for label in labels if label.strip()}
    sections = payload.get("waybill_info")
    if not isinstance(sections, list):
        return ""
    for section in sections:
        if not isinstance(section, dict):
            continue
        items = section.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if _clean_reply_text(item.get("label")) in wanted:
                value = _clean_reply_text(item.get("value"))
                if value:
                    return value
    return ""


def _pieces_text(payload: dict[str, Any]) -> str:
    stub = _waybill_stub(payload)
    arrival_progress = payload.get("arrival_progress")
    expected_quantity = ""
    if isinstance(arrival_progress, dict):
        expected_quantity = _piece_count_text(arrival_progress.get("expected_quantity"))
    return (
        _clean_reply_text(stub.get("pieces"))
        or _clean_reply_text(stub.get("quantity"))
        or expected_quantity
        or _waybill_info_value(payload, "件数", "开单件数", "货物件数")
    )


def _recipient_text(payload: dict[str, Any]) -> str:
    stub = _waybill_stub(payload)
    name = _clean_reply_text(stub.get("recipient_name")) or _waybill_info_value(payload, "收货人", "收件人")
    phone = _clean_reply_text(stub.get("recipient_phone")) or _waybill_info_value(payload, "收货电话", "收件电话")
    extension = (
        _clean_reply_text(stub.get("recipient_phone_extension"))
        or _clean_reply_text(stub.get("recipient_extension"))
        or _clean_reply_text(stub.get("recipient_ext_no"))
        or _waybill_info_value(payload, "收件人分机号", "收货人分机号", "收件分机号", "收货分机号", "分机号")
    )
    extension_text = ""
    if extension and extension not in phone and not any(marker in phone for marker in ("分机", "转")):
        extension_text = f"分机号：{extension}"
    return " ".join(part for part in (name, phone, extension_text) if part)


def _recipient_address_text(payload: dict[str, Any]) -> str:
    stub = _waybill_stub(payload)
    return _clean_reply_text(stub.get("recipient_address")) or _waybill_info_value(payload, "收货地址", "收件地址")


def _goods_name_text(payload: dict[str, Any]) -> str:
    stub = _waybill_stub(payload)
    return _clean_reply_text(stub.get("goods_name")) or _waybill_info_value(
        payload,
        "货物名称",
        "货物名",
        "品名",
        "物品名称",
    )


def _destination_site_text(payload: dict[str, Any]) -> str:
    stub = _waybill_stub(payload)
    return (
        _clean_reply_text(stub.get("disp_site"))
        or _clean_reply_text(stub.get("destination_station"))
        or _clean_reply_text(stub.get("destination_site"))
        or _waybill_info_value(payload, "目的站点", "目的网点", "目的地")
    )


def _delivery_method_text(payload: dict[str, Any]) -> str:
    stub = _waybill_stub(payload)
    return _clean_reply_text(stub.get("delivery_method")) or _waybill_info_value(
        payload,
        "派送方式",
        "送货方式",
        "运输方式",
    )


def _piece_count_text(value: Any) -> str:
    text = _clean_reply_text(value)
    if not text:
        return ""
    return text if text.endswith("件") else f"{text} 件"


def _ronghui_arrived_pieces(payload: dict[str, Any]) -> str:
    arrival_progress = payload.get("arrival_progress")
    if isinstance(arrival_progress, dict) and arrival_progress.get("arrived_quantity") not in (None, ""):
        return _piece_count_text(arrival_progress.get("arrived_quantity"))
    return ""


def _is_daxiang_opening_route(opening_route: dict[str, Any]) -> bool:
    station = _route_station(opening_route)
    return station in DAXIANG_OPENING_STATIONS


def _format_waybill_summary(payload: dict[str, Any], opening_route: dict[str, Any] | None = None) -> str:
    payload_type = _clean_reply_text(payload.get("type"))
    pieces = _pieces_text(payload)
    recipient = _recipient_text(payload)

    lines = [
        "货物信息：",
        f"货物名称：{_goods_name_text(payload) or '无数据'}",
        f"货物件数：{pieces or '无数据'}",
        f"目的站点：{_destination_site_text(payload) or '无数据'}",
        f"派送方式：{_delivery_method_text(payload) or '无数据'}",
        f"收货人：{recipient or '无数据'}",
        f"收货地址：{_recipient_address_text(payload) or '无数据'}",
    ]
    if payload_type in {"ronghui_tms", "ronghui"}:
        if not _is_daxiang_opening_route(opening_route or {}):
            arrived = _ronghui_arrived_pieces(payload)
            lines.append(f"开单/到达：{pieces or '无数据'} / {arrived or '无数据'}")
    return "\n".join(lines)


def _fit_feishu_text(lines: list[str], total_rows: int, *, max_bytes: int = 4000) -> str:
    output: list[str] = []
    for line in lines:
        candidate = "\n\n".join([*output, line]) if output else line
        if len(candidate.encode("utf-8")) <= max_bytes:
            output.append(line)
            continue
        break
    if len(output) == len(lines):
        return "\n\n".join(output)
    shown_rows = max(0, len(output) - 1)
    suffix = f"...（内容过长，已截断，显示最新 {shown_rows}/{total_rows} 条）"
    while output and len("\n\n".join([*output, suffix]).encode("utf-8")) > max_bytes:
        output.pop()
    return "\n\n".join([*output, suffix]) if output else suffix


def format_track_waybill_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        return f"单号查询失败：{str(result.get('error') or '执行失败').strip()}"
    payload = result.get("data")
    if not isinstance(payload, dict):
        return f"单号查询：{payload}"
    if payload.get("error"):
        return f"单号查询失败：{payload['error']}"

    tracking_number = str(payload.get("tracking_number") or payload.get("requested_tracking_number") or "").strip()
    lines = [f"查询单号：{tracking_number}" if tracking_number else "查询单号："]
    rows = _route_rows_newest_first(
        payload.get("route_rows")
        or payload.get("tracks")
    )
    if not rows:
        contact = payload.get("contact")
        if isinstance(contact, dict) and contact.get("note"):
            lines.append(str(contact.get("note")).strip())
            return "\n\n".join(lines)
        lines.append("暂无路由信息")
        return "\n\n".join(lines)

    payload_type = _clean_reply_text(payload.get("type"))
    opening_route = _oldest_opening_route(rows)
    for index, row in enumerate(_latest_route_rows_for_reply(rows, payload_type)):
        lines.append(_format_route_summary(_latest_route_title(index), row, payload_type))
    lines.append(_format_route_summary("最初开单路由", opening_route, payload_type))
    lines.append(_format_waybill_summary(payload, opening_route))
    return _fit_feishu_text(lines, len(rows))


def format_automation_profile_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        return f"自动化 Profile 操作失败：{str(result.get('error') or '执行失败').strip()}"
    payload = result.get("data") if isinstance(result.get("data"), dict) else {}
    label = str(payload.get("label") or payload.get("profile") or "").strip()
    profile = str(payload.get("profile") or "").strip()
    if payload.get("action") == "set":
        return f"已切换到：{label or profile}"
    return f"当前自动化：{label or profile or '融辉自动化'}"


def format_send_order_sync_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        error_text = str(result.get("error") or "融辉寄件数据同步失败").strip()
        return f"融辉寄件数据同步失败：{error_text}"
    payload = result.get("data")
    if not isinstance(payload, dict):
        return f"融辉寄件数据同步：{payload}"
    if payload.get("error"):
        return f"融辉寄件数据同步失败：{payload['error']}"
    lines = ["融辉寄件数据同步已完成"]
    if payload.get("start_date") and payload.get("end_date"):
        lines.append(f"发件日期范围：{payload['start_date']} 至 {payload['end_date']}")
        if payload.get("days") not in (None, ""):
            lines.append(f"同步天数：{payload['days']}")
    for label, key in (
        ("发件日期", "target_date"),
        ("接口原始记录", "raw_fetched"),
        ("拉取记录", "fetched"),
        ("跳过回单号", "skipped_receipt_like"),
        ("更新记录", "updates"),
        ("新增记录", "creates"),
        ("删除旧记录", "deleted"),
        ("写入记录", "written"),
        ("SQL写入", "sql_upserted"),
        ("SQL删除旧记录", "sql_deleted_stale"),
        ("电子表格写入", "sheet_rows"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


def format_yunda_dispatch_forecast_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        error_text = str(result.get("error") or "韵达派件预测同步失败").strip()
        return f"韵达派件预测同步失败：{error_text}"
    payload = result.get("data")
    if not isinstance(payload, dict):
        return f"韵达派件预测同步：{payload}"
    if payload.get("error"):
        return f"韵达派件预测同步失败：{payload['error']}"
    lines = ["韵达派件预测同步已完成"]
    for label, key in (
        ("应派时间", "target_date"),
        ("接口总数", "total"),
        ("拉取记录", "fetched"),
        ("删除旧记录", "deleted"),
        ("写入记录", "written"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


def format_yunda_send_waybills_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        error_text = str(result.get("error") or "韵达寄件运单同步失败").strip()
        return f"韵达寄件运单同步失败：{error_text}"
    payload = result.get("data")
    if not isinstance(payload, dict):
        return f"韵达寄件运单同步：{payload}"
    if payload.get("error"):
        return f"韵达寄件运单同步失败：{payload['error']}"
    lines = ["韵达寄件运单同步已完成"]
    if payload.get("start_date") and payload.get("end_date"):
        lines.append(f"寄件日期范围：{payload['start_date']} 至 {payload['end_date']}")
        if payload.get("days") not in (None, ""):
            lines.append(f"同步天数：{payload['days']}")
    for label, key in (
        ("寄件日期", "target_date"),
        ("接口总数", "total"),
        ("拉取记录", "fetched"),
        ("更新记录", "updates"),
        ("新增记录", "creates"),
        ("写入记录", "written"),
        ("SQL写入", "sql_upserted"),
        ("SQL删除旧记录", "sql_deleted_stale"),
    ):
        value = payload.get(key)
        if value not in (None, ""):
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


def format_r7_departure_checkin_reply(result: dict[str, Any]) -> str:
    payload = result.get("data")
    if not isinstance(payload, dict):
        payload = {}

    if not result.get("success", False) or payload.get("ok") is False:
        error_text = str(
            payload.get("message")
            or payload.get("error")
            or result.get("error")
            or "执行失败"
        ).strip()
        lines = [f"R7 发车打卡失败：{error_text}"]
        stage = str(payload.get("stage") or "").strip()
        if stage:
            lines.append(f"阶段：{stage}")
        detail = payload.get("detail")
        if isinstance(detail, dict):
            departure_time = str(detail.get("departure_time") or "").strip()
            class_name = str(detail.get("class_name") or "").strip()
            plate_numbers = detail.get("plate_numbers")
            if class_name:
                lines.append(f"班次：{class_name}")
            if departure_time:
                lines.append(f"计划发车：{departure_time}")
            if plate_numbers:
                if isinstance(plate_numbers, list):
                    lines.append(f"车牌：{', '.join(str(item) for item in plate_numbers)}")
                else:
                    lines.append(f"车牌：{plate_numbers}")
            errors = detail.get("errors")
            if isinstance(errors, list) and errors:
                for item in errors[:3]:
                    if not isinstance(item, dict):
                        continue
                    plate = str(item.get("plate_number") or "").strip()
                    match_count = item.get("match_count")
                    if plate and match_count not in (None, ""):
                        lines.append(f"命中数：{plate}={match_count}")
        return "\n".join(lines)

    if payload.get("skipped"):
        lines = [f"R7 发车打卡已跳过：{payload.get('message') or '当天成功次数已达上限'}"]
        detail = payload.get("detail")
        if isinstance(detail, dict):
            business_date = str(detail.get("business_date") or "").strip()
            success_count = str(detail.get("success_count_today") or "").strip()
            daily_limit = str(detail.get("daily_success_limit") or "").strip()
            if business_date:
                lines.append(f"日期：{business_date}")
            if success_count and daily_limit:
                lines.append(f"今日成功：{success_count}/{daily_limit}")
        return "\n".join(lines)

    lines = ["R7 发车打卡已完成"]
    detail = payload.get("detail")
    if isinstance(detail, dict):
        for label, key in (
            ("班次", "class_name"),
            ("计划发车", "departure_time"),
            ("目标状态", "status_text"),
            ("验证状态", "verify_status_text"),
            ("今日成功", "success_count_today"),
            ("一天次数", "daily_success_limit"),
        ):
            value = str(detail.get(key) or "").strip()
            if value:
                lines.append(f"{label}：{value}")
        plate_numbers = detail.get("plate_numbers")
        if isinstance(plate_numbers, list) and plate_numbers:
            lines.append(f"车牌：{', '.join(str(item) for item in plate_numbers)}")
    cost_sec = payload.get("cost_sec")
    if cost_sec not in (None, ""):
        lines.append(f"耗时：{cost_sec} 秒")
    return "\n".join(lines)


def format_r7_arrival_checkin_reply(result: dict[str, Any]) -> str:
    payload = result.get("data")
    if not isinstance(payload, dict):
        payload = {}

    if not result.get("success", False) or payload.get("ok") is False:
        error_text = str(
            payload.get("message")
            or payload.get("error")
            or result.get("error")
            or "执行失败"
        ).strip()
        lines = [f"R7 到达打卡失败：{error_text}"]
        stage = str(payload.get("stage") or "").strip()
        if stage:
            lines.append(f"阶段：{stage}")
        detail = payload.get("detail")
        if isinstance(detail, dict):
            task_no = str(detail.get("task_no") or detail.get("task_number") or "").strip()
            status_text = str(detail.get("status_text") or "").strip()
            if task_no:
                lines.append(f"任务号：{task_no}")
            if status_text:
                lines.append(f"目标状态：{status_text}")
        return "\n".join(lines)

    if payload.get("skipped"):
        lines = [f"R7 到达打卡已跳过：{payload.get('message') or '当天成功次数已达上限'}"]
        detail = payload.get("detail")
        if isinstance(detail, dict):
            business_date = str(detail.get("business_date") or "").strip()
            success_count = str(detail.get("success_count_today") or "").strip()
            daily_limit = str(detail.get("daily_success_limit") or "").strip()
            if business_date:
                lines.append(f"日期：{business_date}")
            if success_count and daily_limit:
                lines.append(f"今日成功：{success_count}/{daily_limit}")
        return "\n".join(lines)

    lines = ["R7 到达打卡已完成"]
    stage = str(payload.get("stage") or "").strip()
    if stage:
        lines.append(f"阶段：{stage}")
    detail = payload.get("detail")
    if isinstance(detail, dict):
        for label, key in (
            ("任务号", "task_no"),
            ("目标状态", "status_text"),
            ("验证状态", "verify_status_text"),
            ("当前状态", "verify_status"),
            ("计划发车", "departure_time"),
            ("今日成功", "success_count_today"),
            ("一天次数", "daily_success_limit"),
        ):
            value = str(detail.get(key) or "").strip()
            if value:
                lines.append(f"{label}：{value}")
    cost_sec = payload.get("cost_sec")
    if cost_sec not in (None, ""):
        lines.append(f"耗时：{cost_sec} 秒")
    return "\n".join(lines)


def _scan_sync_batch_error(raw: Any) -> str:
    if not isinstance(raw, dict):
        return str(raw or "未知错误").strip()
    for key in ("error", "message"):
        value = raw.get(key)
        if value:
            return str(value).strip()
    for nested_key in ("data", "detail", "raw"):
        nested = raw.get(nested_key)
        if isinstance(nested, dict):
            nested_text = _scan_sync_batch_error(nested)
            if nested_text and nested_text != "未知错误":
                return nested_text
    return "未知错误"


def format_scan_sync_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        error_text = str(result.get("error") or "扫描失败").strip()
        return f"扫描任务失败：{error_text}"

    payload = result.get("data")
    if not isinstance(payload, dict):
        return f"扫描任务：{payload}"
    batch_results = payload.get("batch_results")
    failed_batches = []
    if isinstance(batch_results, list):
        failed_batches = [
            row for row in batch_results
            if isinstance(row, dict) and not row.get("ok")
        ]
    if payload.get("error"):
        lines = [f"扫描任务失败：{payload['error']}"]
        failed_batch = payload.get("failed_batch")
        if failed_batch not in (None, ""):
            lines.append(f"停止批次：第 {failed_batch} 批")
        for row in failed_batches[:5]:
            lines.append(
                f"- 第 {row.get('batch', '?')} 批："
                f"{_scan_sync_batch_error(row.get('raw'))[:120]}"
            )
        return "\n".join(lines)
    if failed_batches:
        lines = [f"扫描任务失败：检测到 {len(failed_batches)} 个失败批次"]
        for row in failed_batches[:5]:
            lines.append(
                f"- 第 {row.get('batch', '?')} 批："
                f"{_scan_sync_batch_error(row.get('raw'))[:120]}"
            )
        return "\n".join(lines)

    lines = ["扫描任务按计划完成" if payload.get("truncated") else "扫描任务已完成"]
    for label, key in (
        ("拉取扫描记录", "fetched"),
        ("刷新扫描索引", "normalized"),
        ("待扫描子单", "child_items"),
        ("scan_next 批次", "batches"),
    ):
        value = payload.get(key)
        if value is not None:
            lines.append(f"{label}：{value}")

    scan_index_result = payload.get("scan_index_result")
    if isinstance(scan_index_result, dict) and scan_index_result.get("replaced") is not None:
        lines.append(f"索引写入：{scan_index_result.get('replaced')}")

    if isinstance(batch_results, list):
        lines.append("scan_next 结果：全部成功")

    omitted_items = payload.get("omitted_items")
    if omitted_items not in (None, "", 0):
        lines.append(f"未排入本次扫描：{omitted_items}")

    skipped_signed_count = payload.get("skipped_signed_count")
    if skipped_signed_count not in (None, "", 0):
        lines.append(f"已签收跳过：{skipped_signed_count}")

    flow_result = payload.get("flow_result")
    if isinstance(flow_result, dict):
        if flow_result.get("skipped"):
            lines.append("后续流程：已跳过")
        elif flow_result.get("ok"):
            lines.append("后续流程：已触发")
        elif flow_result.get("error"):
            lines.append(f"后续流程：失败：{str(flow_result.get('error'))[:80]}")
    return "\n".join(lines)


def format_arrive_list_sync_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        error_text = str(result.get("error") or "到货清单同步失败").strip()
        return f"到货清单同步失败：{error_text}"

    payload = result.get("data")
    if not isinstance(payload, dict):
        return f"到货清单同步：{payload}"
    if payload.get("error"):
        return f"到货清单同步失败：{payload['error']}"

    lines = ["到货清单同步已完成"]
    for label, key in (
        ("派件预报", "fetched"),
        ("主单数", "bill_codes"),
        ("跳过回单号", "skipped_receipt_like"),
        ("基础清单", "detail_records"),
    ):
        value = payload.get(key)
        if value is not None:
            lines.append(f"{label}：{value}")

    mysql_result = payload.get("mysql_result")
    if isinstance(mysql_result, dict):
        replaced = mysql_result.get("replaced")
        if mysql_result.get("skipped") and replaced is not None:
            lines.append(f"MySQL：演练未写入，预计覆盖 {replaced}")
        elif replaced is not None:
            lines.append(f"MySQL：覆盖 {replaced}")
        elif mysql_result.get("error"):
            lines.append(f"MySQL：失败：{str(mysql_result.get('error'))[:80]}")

    def _append_sheet_summary(label: str, section: Any) -> None:
        if not isinstance(section, dict):
            return
        if section.get("error"):
            lines.append(f"{label}：失败：{str(section.get('error'))[:80]}")
            return
        rows = section.get("rows")
        if section.get("skipped"):
            if rows is not None:
                lines.append(f"{label}：演练未写入，预计 {rows} 行")
            else:
                lines.append(f"{label}：演练未写入")
            return
        if rows is not None:
            lines.append(f"{label}：写入 {rows} 行")
        elif section.get("ok") is not None:
            lines.append(f"{label}：{'成功' if section.get('ok') else '失败'}")

    _append_sheet_summary("主飞书表", payload.get("primary_result"))
    _append_sheet_summary("副飞书表", payload.get("secondary_result"))
    return "\n".join(lines)


def format_arrival_stats_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        error_text = str(result.get("error") or "统计失败").strip()
        return f"统计到货数据失败：{error_text}"

    payload = result.get("data")
    if not isinstance(payload, dict):
        return f"统计到货数据：{payload}"
    if payload.get("error"):
        return f"统计到货数据失败：{payload['error']}"

    def _summary(section: dict[str, Any] | Any) -> str:
        if not isinstance(section, dict):
            return "未知"
        if section.get("skipped"):
            reason = section.get("reason") or "已跳过"
            return f"跳过（{reason}）"
        if section.get("error"):
            return f"失败：{str(section['error'])[:60]}"
        if section.get("ok") is False:
            return "失败"
        return "成功"

    records = payload.get("records")
    main_trackings = payload.get("main_trackings")
    count_result = payload.get("count_result") or {}
    arrived_nonzero = count_result.get("arrived_nonzero") if isinstance(count_result, dict) else None

    lines = ["统计到货数据已完成"]
    if main_trackings is not None:
        lines.append(f"扫描索引主单：{main_trackings}")
    if records is not None:
        lines.append(f"统计主单记录：{records}")
    if arrived_nonzero is not None:
        lines.append(f"已到货主单：{arrived_nonzero}")
    lines.append(f"主统计表：{_summary(payload.get('primary_result'))}")
    lines.append(f"副统计表：{_summary(payload.get('secondary_result'))}")
    lines.append(f"未齐货物表：{_summary(payload.get('pending_result'))}")
    lines.append(f"分批及有发未到表：{_summary(payload.get('split_pending_result'))}")
    lines.append(f"归档快照：{_summary(payload.get('archive_result'))}")
    return "\n".join(lines)


def format_split_pending_problem_upload_reply(result: dict[str, Any]) -> str:
    if result.get("success") is False:
        error_text = str(result.get("error") or "上传失败").strip()
        return f"{SPLIT_PENDING_PROBLEM_LABEL}失败：{error_text}"

    payload = result.get("data") if "success" in result else result
    if not isinstance(payload, dict):
        return f"{SPLIT_PENDING_PROBLEM_LABEL}：{payload}"
    if payload.get("error"):
        return f"{SPLIT_PENDING_PROBLEM_LABEL}失败：{payload['error']}"

    stage = str(payload.get("stage") or "").strip()
    candidate_count = int(payload.get("candidate_count") or 0)
    type_counts = payload.get("type_counts") if isinstance(payload.get("type_counts"), dict) else {}
    split_count = int(type_counts.get("少货/分批") or 0)
    pending_count = int(type_counts.get("有发未到") or 0)

    if stage == "dry_run":
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        hidden_completed = int(payload.get("hidden_completed_count") or 0)
        if not candidates:
            return (
                f"{SPLIT_PENDING_PROBLEM_LABEL}：当前没有可执行运单。"
                f"已隐藏完整成功 {hidden_completed} 单。"
            )
        lines = [
            f"待执行{SPLIT_PENDING_PROBLEM_LABEL}候选 {candidate_count} 单",
            f"少货/分批：{split_count}",
            f"有发未到：{pending_count}",
            f"已隐藏完整成功：{hidden_completed}",
            "本次仅预览，未写目标表、数据库或融辉。",
        ]
        for item in candidates[:20]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('bill_code')} [{item.get('status')}] {item.get('problem_type')}，"
                f"已到{item.get('arrived_quantity')}/应到{item.get('expected_quantity')}件"
            )
        if candidate_count > 20:
            lines.append(f"... 其余 {candidate_count - 20} 单已省略")
        lines.append("")
        lines.append('回复“确认”直接执行全部；如需部分上传，请输入序号，例如“2”“1,3,5”或“2-4”。10 分钟内有效。')
        return "\n".join(lines)

    if stage in {"selection_required", "preview_expired"}:
        return str(payload.get("message") or payload.get("error") or "请重新发送“分批”")

    saved = int(payload.get("saved_bills") or 0)
    failed = int(payload.get("failed_bills") or 0)
    database_rows = int(payload.get("database_rows") or 0)
    target_rows = int(payload.get("target_sheet_rows") or 0)
    failed_codes = payload.get("failed_bill_codes") if isinstance(payload.get("failed_bill_codes"), list) else []
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    lines = [
        f"{SPLIT_PENDING_PROBLEM_LABEL}执行完成",
        f"当前未齐：{candidate_count}",
        f"少货/分批：{split_count}",
        f"有发未到：{pending_count}",
        f"目标 Sheet：{target_rows} 行",
        f"数据库快照：{database_rows} 行",
        f"融辉成功：{saved}",
        f"融辉失败：{failed}",
    ]
    if failed_codes:
        head = ", ".join(str(code) for code in failed_codes[:10])
        if len(failed_codes) > 10:
            head += f" 等 {len(failed_codes)} 单"
        lines.append(f"失败单号：{head}")
    failed_results = [
        item for item in results
        if isinstance(item, dict) and not item.get("complete")
    ]
    if failed_results:
        lines.append("失败原因：")
        for item in failed_results[:5]:
            bill_code = str(item.get("bill_code") or "?")
            complaint = item.get("complaint") if isinstance(item.get("complaint"), dict) else {}
            problem_item = item.get("problem_item") if isinstance(item.get("problem_item"), dict) else {}
            error_text = str(
                complaint.get("error")
                or complaint.get("message")
                or problem_item.get("error")
                or problem_item.get("message")
                or "步骤未完整成功"
            ).replace("\n", " ")
            if len(error_text) > 160:
                error_text = error_text[:160] + "..."
            lines.append(f"- {bill_code}: {error_text}")
    return "\n".join(lines)


def format_self_pickup_problem_upload_reply(result: dict[str, Any]) -> str:
    if result.get("success") is False:
        error_text = str(result.get("error") or "上传失败").strip()
        return f"{SELF_PICKUP_PROBLEM_LABEL}失败：{error_text}"

    payload = result.get("data") if "success" in result else result
    if not isinstance(payload, dict):
        return f"{SELF_PICKUP_PROBLEM_LABEL}：{payload}"
    if payload.get("error"):
        return f"{SELF_PICKUP_PROBLEM_LABEL}失败：{payload['error']}"

    stage = str(payload.get("stage") or "").strip()
    candidate_count = int(payload.get("candidate_count") or 0)
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    sheet_title = str(source.get("sheet_title") or source.get("sheet_id") or "").strip()
    destination_site = str(source.get("destination_site") or "邵阳自提部").strip()
    source_summaries = payload.get("source_summaries") if isinstance(payload.get("source_summaries"), list) else []

    def append_source_lines(lines: list[str], *, include_results: bool) -> None:
        if not source_summaries:
            return
        lines.append("来源明细：")
        for summary in source_summaries:
            if not isinstance(summary, dict):
                continue
            name = str(summary.get("source_name") or summary.get("destination_site") or "未知来源").strip()
            count = int(summary.get("candidate_count") or 0)
            if include_results:
                saved = int(summary.get("saved_bills") or 0)
                skipped = int(summary.get("skipped_bills") or 0)
                failed = int(summary.get("failed_bills") or 0)
                uploaded = int(summary.get("uploaded_files_total") or 0)
                lines.append(f"- {name}：候选 {count}，成功 {saved}，跳过 {skipped}，失败 {failed}，截图 {uploaded}")
            else:
                lines.append(f"- {name}：{count} 单")
                candidates = summary.get("candidates") if isinstance(summary.get("candidates"), list) else []
                for item in candidates[:10]:
                    if isinstance(item, dict):
                        lines.append(f"  - {item.get('bill_code')}")
                if count > 10:
                    lines.append(f"  ... 其余 {count - 10} 单已省略")

    if stage == "dry_run":
        candidates = payload.get("candidates") or []
        lines = [f"待上传{SELF_PICKUP_PROBLEM_LABEL}候选 {candidate_count} 单"]
        if sheet_title:
            lines.append(f"来源表：{sheet_title}")
        if not source_summaries:
            lines.append(f"目的站点：{destination_site}")
        if payload.get("screenshot_enabled"):
            lines.append("截图：已启用，可选附加到问题件")
        else:
            lines.append("截图：不上传")
        append_source_lines(lines, include_results=False)
        if not source_summaries:
            preview_count = min(len(candidates), 20)
            for item in candidates[:preview_count]:
                if isinstance(item, dict):
                    lines.append(f"- {item.get('bill_code')}")
            if candidate_count > preview_count:
                lines.append(f"... 其余 {candidate_count - preview_count} 单已省略")
        lines.append("")
        lines.append('确认上传请回复"确认"，放弃请回复"取消"。10 分钟内有效。')
        return "\n".join(lines)

    if stage == "no_candidates" or candidate_count == 0:
        return f"{SELF_PICKUP_PROBLEM_LABEL}：飞书未找到符合条件的单号（{destination_site}）"

    saved = int(payload.get("saved_bills") or 0)
    skipped = int(payload.get("skipped_bills") or 0)
    failed = int(payload.get("failed_bills") or 0)
    uploaded_total = int(payload.get("uploaded_files_total") or 0)
    failed_codes = payload.get("failed_bill_codes") or []
    results = payload.get("results") or []

    lines = [
        f"{SELF_PICKUP_PROBLEM_LABEL}完成",
        f"候选单号：{candidate_count}",
        f"成功上传：{saved}",
        f"跳过：{skipped}",
        f"失败：{failed}",
        f"附加截图文件：{uploaded_total}",
    ]
    if sheet_title:
        lines.append(f"来源表：{sheet_title}")
    append_source_lines(lines, include_results=True)
    skipped_results = [
        row for row in results
        if isinstance(row, dict) and row.get("skipped")
    ]
    if skipped_results:
        head = ", ".join(str(row.get("bill_code") or "?") for row in skipped_results[:10])
        if len(skipped_results) > 10:
            head += f" 等 {len(skipped_results)} 单"
        lines.append(f"跳过单号：{head}")
    if failed_codes:
        head = ", ".join(str(code) for code in failed_codes[:10])
        if len(failed_codes) > 10:
            head += f" 等 {len(failed_codes)} 单"
        lines.append(f"失败单号：{head}")

    failed_results = [
        row for row in results
        if isinstance(row, dict) and not row.get("saved") and not row.get("skipped")
    ]
    if failed_results:
        lines.append("失败原因：")
        for row in failed_results[:5]:
            bill = row.get("bill_code") or "?"
            msg = str(row.get("error") or row.get("message") or "未知错误").strip().replace("\n", " ")
            if len(msg) > 160:
                msg = msg[:160] + "..."
            lines.append(f"- {bill}: {msg}")
        if len(failed_results) > 5:
            lines.append(f"... 其余 {len(failed_results) - 5} 单原因略")
    return "\n".join(lines)


def format_price_reply(result: dict[str, Any]) -> str:
    messages = format_price_reply_messages(result)
    return "\n\n".join(message for message in messages if message)


def format_price_reply_messages(result: dict[str, Any]) -> list[str]:
    if not result.get("success", False):
        error_text = str(result.get("error") or "报价失败").strip()
        return [f"报价失败：{error_text}"]

    payload = result.get("data")
    if not isinstance(payload, dict):
        return [f"报价结果：{payload}"]
    if payload.get("error"):
        return [f"报价失败：{payload['error']}"]
    if payload.get("网点不可达"):
        return ["报价失败：网点不可达"]

    if isinstance(payload.get("ronghui"), dict) or isinstance(payload.get("yunda"), dict):
        messages: list[str] = []
        ronghui_payload = payload.get("ronghui")
        if isinstance(ronghui_payload, dict):
            messages.append(_format_ronghui_price_payload(ronghui_payload, title="融辉价格"))
        else:
            messages.append(_format_ronghui_price_payload(payload))

        yunda_payload = payload.get("yunda")
        if isinstance(yunda_payload, dict):
            messages.append(_format_yunda_price_payload(yunda_payload, title="韵达价格"))
        return [message for message in messages if message]

    return [_format_ronghui_price_payload(payload)]


def _format_ronghui_price_payload(payload: dict[str, Any], *, title: str = "") -> str:
    if _is_provider_unavailable(payload):
        return _format_provider_unavailable("融辉", title=title)
    if _is_provider_failure(payload):
        return _format_provider_failure("融辉", payload, title=title)

    lines: list[str] = []
    if title:
        lines.append(title)
    if payload.get("目的网点"):
        lines.append(f"目的网点：{payload['目的网点']}")

    for key in _pickup_price_order():
        if payload.get(key) not in (None, "", "/"):
            lines.append(f"{key}：{payload[key]}")

    for key in _dispatch_price_order():
        if payload.get(key) not in (None, "", "/"):
            lines.append(f"{key}：{payload[key]}")

    for key in _price_meta_order():
        if payload.get(key) not in (None, "", "/"):
            lines.append(f"{key}：{payload[key]}")

    return "\n".join(lines)


def _format_yunda_price_payload(payload: dict[str, Any], *, title: str = "") -> str:
    if _is_provider_unavailable(payload):
        return _format_provider_unavailable("韵达", title=title)
    if _is_provider_failure(payload):
        return _format_provider_failure("韵达", payload, title=title)

    lines: list[str] = []
    if title:
        lines.append(title)
    for key in (
        "目的网点",
        "韵达自提",
        "韵达派送",
        "是否派送",
        "首发分拨",
        "结算重量",
        "查询电话",
        "客服电话",
        "业务电话",
        "经理电话",
        "门店地址",
        "派送范围",
        "特殊区域",
        "特殊区域加收",
        "特殊区域提醒",
        "派送说明",
    ):
        if payload.get(key) not in (None, "", "/"):
            lines.append(f"{key}：{payload[key]}")
    if lines:
        return "\n".join(lines)
    return f"韵达报价结果：{payload}"


def _is_provider_unavailable(payload: dict[str, Any]) -> bool:
    return bool(payload.get("unavailable") or payload.get("不可到达") or payload.get("网点不可达"))


def _is_provider_failure(payload: dict[str, Any]) -> bool:
    return bool(payload.get("error"))


def _format_provider_unavailable(provider: str, *, title: str = "") -> str:
    lines: list[str] = []
    if title:
        lines.append(title)
    lines.append(f"{provider}不可到达")
    return "\n".join(lines)


def _format_provider_failure(provider: str, payload: dict[str, Any], *, title: str = "") -> str:
    lines: list[str] = []
    if title:
        lines.append(title)
    error_text = str(payload.get("error") or "报价失败").strip()
    lines.append(f"{provider}报价失败：{error_text}")
    return "\n".join(lines)


def _format_generic_reply(result: dict[str, Any]) -> str:
    if not result.get("success", False):
        return str(result.get("error") or "执行失败").strip()
    return "执行完成"


def _pickup_price_order() -> tuple[str, ...]:
    return (
        "精准零担",
        "融速达",
        "融安达",
        "融惠达",
    )


def _dispatch_price_order() -> tuple[str, ...]:
    return (
        "精准零担(派送)",
        "融速达(派送)",
        "融安达(派送)",
        "融惠达(派送)",
    )


def _price_meta_order() -> tuple[str, ...]:
    return (
        "查询电话",
        "经理电话",
        "门店地址",
        "派送范围",
        "特殊区域",
        "到件时效",
        "不派送范围",
        "超远乡镇",
        "超远乡镇详情",
    )
