"""Problem replies exactly as the original pages do them (用户 2026-11-27：处理逻辑与原页一致).

Pure rules, no I/O: the Agent runs them before writing and builds the write from them;
the reply forms in the customer-service page and the home message center are rendered
from :func:`ronghui_reply_form` / :func:`yunda_reply_form`, so options and limits have
one source. Sources (read-only capture 2026-11-27, see docs/original_page_processing_inventory.md):

* 融辉 “收到问题件查询” ``saveReplyData`` → ``saveReplDateAfterCheck``: only the notified
  site may reply, and not once a reply exists and the problem is 处理完毕; 退件 problems need
  a 退件处理意见; self-pickup problems (``IS_SELF_PICK=1``) need a 提货方式, and for
  安排司机提货/收件人自行提货 up to five drivers plus a pickup day (today or tomorrow) and hour
  slot; the drivers' pieces may not exceed the waybill's pieces; reply text (≤500) or drivers
  are required. The 处理结果 control is commented out on the page, so the save writes
  ``BL_CHECKOK=1`` (处理完毕) and leaves ``DEAL_RESULT`` unchanged; problems from
  协商再投接口/APP/K13 whose waybill is 已签收/已退件/已弃货 cannot be replied to.
* 韵达 ``query/reply.html``: ``prob_status`` 1 处理中 / 2 已处理 (no 已完结), default 已处理
  (处理中 while the problem is still 未处理); reply text ≤600 required; ``old_prob_status`` is
  always 2; 预约进仓 (problem type 572) needs 同意/不同意 when the account has review rights;
  replying is closed when ``reply_auth`` is 2 or the problem is 已完结/超时已完结.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping, Sequence

RONGHUI_PICK_TYPES = ("安排司机提货", "收件人自行提货", "其他")
RONGHUI_DRIVER_PICK_TYPES = ("安排司机提货", "收件人自行提货")
RONGHUI_RETURN_OPINIONS = ("同意退件", "帮我转件", "客户同意收件")
RONGHUI_PICKUP_HOURS = tuple(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(9, 21))
RONGHUI_REPLY_MAX = 500
RONGHUI_MAX_DRIVERS = 5
RONGHUI_CHECKED_SOURCES = ("协商再投接口", "APP", "K13")
RONGHUI_BLOCKED_BILL_STATUS = {"已签收": "该问题件运单已签收，只允许选择再派处理结果，原页无法再处理。",
                               "已退件": "运单已做了退件，不允许再处理！", "已弃货": "运单已做了弃货，不允许再处理！"}
YUNDA_STATUSES = (("1", "处理中"), ("2", "已处理"))
YUNDA_STATUS_BY_TEXT = {text: code for code, text in YUNDA_STATUSES}
YUNDA_REPLY_MAX = 600
YUNDA_REVIEW_TYPE_ID = "572"
YUNDA_CLOSED_STATUSES = {"2", "4"}  # 已完结、超时已完结（详情接口的数值状态）

_PHONE = re.compile(r"^1[3-9]\d{9}$")
_ID_CARD = re.compile(r"(^\d{15}$)|(^\d{18}$)|(^\d{17}(\d|X|x)$)")
_PLATE = re.compile(r"^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}$")


class ReplyRuleError(ValueError):
    """The reply the person filled in breaks an original-page rule; the message says which."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def pickup_days(today: dt.date) -> list[str]:
    return [today.isoformat(), (today + dt.timedelta(days=1)).isoformat()]


# -- 融辉 ------------------------------------------------------------------------------

def ronghui_reply_block(row: Mapping[str, Any], login_site: str) -> str:
    """Why the original page would not show 保存 for this problem, or ``""`` when it would."""
    if _text(row.get("SEND_SITE")) != _text(login_site):
        return "只有通知网点可以回复这条问题件。"
    if _text(row.get("REVERSION")) and _text(row.get("BL_CHECKOK")) == "1":
        return "这条问题件已回复并处理完毕，原页不能再回复。"
    return ""


def ronghui_reply_form(row: Mapping[str, Any], login_site: str, today: dt.date) -> dict[str, Any]:
    """What the reply form shows for this raw “收到问题件查询” row."""
    return {
        "platform": "ronghui", "blocked": ronghui_reply_block(row, login_site),
        "reply_max": RONGHUI_REPLY_MAX,
        "return_opinions": list(RONGHUI_RETURN_OPINIONS) if _text(row.get("TYPE")) == "退件" else [],
        "self_pick": _text(row.get("IS_SELF_PICK")) == "1",
        "pick_types": list(RONGHUI_PICK_TYPES), "driver_pick_types": list(RONGHUI_DRIVER_PICK_TYPES),
        "pickup_days": pickup_days(today), "pickup_hours": list(RONGHUI_PICKUP_HOURS),
        "max_drivers": RONGHUI_MAX_DRIVERS, "piece_number": _int(row.get("PIECE_NUMBER")),
    }


def _drivers(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ReplyRuleError("司机信息格式不正确。")
    drivers = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise ReplyRuleError("司机信息格式不正确。")
        name, phone, id_card = _text(raw.get("name")), _text(raw.get("phone")), _text(raw.get("idCard"))
        plate, count = _text(raw.get("plate")), _text(raw.get("count"))
        if not name:
            continue  # 原页只收集填写了司机姓名的行
        if not phone or not plate or not count:
            raise ReplyRuleError("司机信息填完整!")
        if not _PHONE.match(phone):
            raise ReplyRuleError(f"第{index}名司机手机号格式不正确")
        if id_card and not _ID_CARD.match(id_card):
            raise ReplyRuleError(f"第{index}名司机身份证号格式不正确")
        if not _PLATE.match(plate):
            raise ReplyRuleError(f"第{index}名司机车牌号格式不正确")
        pieces = _int(count)
        if pieces is None or pieces <= 0:
            raise ReplyRuleError(f"第{index}名司机件数必须是大于0的数字")
        drivers.append({"index": len(drivers) + 1, "name": name, "phone": phone, "idCard": id_card,
                        "plate": plate, "count": str(pieces)})
    if len(drivers) > RONGHUI_MAX_DRIVERS:
        raise ReplyRuleError("最多只能添加5名司机信息")
    return drivers


def validate_ronghui_reply(row: Mapping[str, Any], login_site: str, payload: Mapping[str, Any],
                           today: dt.date) -> dict[str, Any]:
    """The fields the save writes, after every check the original page runs before saving."""
    blocked = ronghui_reply_block(row, login_site)
    if blocked:
        raise ReplyRuleError(blocked)
    reply = _text(payload.get("reply_text"))
    if len(reply) > RONGHUI_REPLY_MAX:
        raise ReplyRuleError(f"回复最多{RONGHUI_REPLY_MAX}字。")
    fields: dict[str, Any] = {"REVERSION": reply, "PICK_TYPE": "", "PICKUP_TIME_DAY": "", "PICKUP_TIME_HOUR": "",
                              "RETURN_PROCESS_OPINION": "", "DRIVER_DATA": []}
    if _text(row.get("TYPE")) == "退件":
        opinion = _text(payload.get("return_opinion"))
        if opinion not in RONGHUI_RETURN_OPINIONS:
            raise ReplyRuleError("请选择退件处理意见。")
        fields["RETURN_PROCESS_OPINION"] = opinion
    if _text(row.get("IS_SELF_PICK")) == "1":
        pick = _text(payload.get("pick_type"))
        if pick not in RONGHUI_PICK_TYPES:
            raise ReplyRuleError("请选择提货方式。")
        fields["PICK_TYPE"] = pick
        if pick in RONGHUI_DRIVER_PICK_TYPES:
            drivers = _drivers(payload.get("drivers") or [])
            if not drivers:
                raise ReplyRuleError("司机信息填完整!")
            day, hour = _text(payload.get("pickup_day")), _text(payload.get("pickup_hour"))
            if day not in pickup_days(today):
                raise ReplyRuleError("请选择提货日期（今天或明天）。")
            if hour not in RONGHUI_PICKUP_HOURS:
                raise ReplyRuleError("请选择提货时间段。")
            pieces = _int(row.get("PIECE_NUMBER"))
            if pieces is not None and pieces < sum(int(driver["count"]) for driver in drivers):
                raise ReplyRuleError("总件数不能大于开单件数")
            fields.update(PICKUP_TIME_DAY=day, PICKUP_TIME_HOUR=hour, DRIVER_DATA=drivers)
    if not reply and not fields["DRIVER_DATA"]:
        raise ReplyRuleError("请填写回复内容!")
    return fields


def ronghui_bill_status_block(row: Mapping[str, Any], statuses: Sequence[Mapping[str, Any]]) -> str:
    """The original check against ``FIND_BILL_STATUS_ON_PROBLEM`` before saving."""
    if not statuses:
        return "问题件不存在！"
    if _text(row.get("DATA_FROM")) in RONGHUI_CHECKED_SOURCES:
        return RONGHUI_BLOCKED_BILL_STATUS.get(_text(statuses[0].get("BILL_STATUS")), "")
    return ""


def build_ronghui_reply_row(full_row: Mapping[str, Any], fields: Mapping[str, Any], user_info: Mapping[str, Any],
                            now: dt.datetime) -> dict[str, Any]:
    """The row ``TAB_PROBLEM_UPT`` saves: the problem read back whole, with the reply fields set."""
    row = dict(full_row)
    row["clearFields"] = []
    row.update({
        "REVERSION_MAN": _text(user_info.get("loginEmpName")), "BL_SEE": 1,
        "REVERSION_MAN_CODE": _text(user_info.get("loginEmpCode")),
        "REVERSION_DATE": now.strftime("%Y-%m-%d %H:%M:%S"), "REVERSION_SITE": _text(user_info.get("loginSiteName")),
        "REVERSION": fields["REVERSION"], "PICK_TYPE": fields["PICK_TYPE"],
        "PICKUP_TIME_DAY": fields["PICKUP_TIME_DAY"], "PICKUP_TIME_HOUR": fields["PICKUP_TIME_HOUR"],
        "RETURN_PROCESS_OPINION": fields["RETURN_PROCESS_OPINION"], "DRIVER_DATA": fields["DRIVER_DATA"],
        # 原页单司机字段取自已隐藏的旧表单控件（空文本框/下拉），保存时为空字符串。
        "DRIVER_NAME": "", "LICENSE_PLATE_NO": "", "MOBILE_PHONE": "", "VEHICLE_TYPE": "",
        "IDENTITY_CARD_NO": "", "BL_CHECKOK": 1,
    })
    return row


# -- 韵达 ------------------------------------------------------------------------------

def yunda_status_code(value: Any) -> str:
    """The reply page's numeric ``prob_status``; older callers send the text (已处理/处理中)."""
    text = _text(value)
    if text in dict(YUNDA_STATUSES):
        return text
    if text in YUNDA_STATUS_BY_TEXT:
        return YUNDA_STATUS_BY_TEXT[text]
    raise ReplyRuleError("处理状态只能选处理中或已处理。")


def yunda_reply_form(row: Mapping[str, Any]) -> dict[str, Any]:
    """What the reply form shows for a list row (the detail decides rights at submit)."""
    status = _text(row.get("prob_status"))
    return {
        "platform": "yunda", "blocked": "", "reply_max": YUNDA_REPLY_MAX,
        "statuses": [{"value": code, "label": text} for code, text in YUNDA_STATUSES],
        "default_status": "1" if status in {"0", "未处理"} else "2",
        "review_entry": _text(row.get("prob_type_id")) == YUNDA_REVIEW_TYPE_ID,
    }


def validate_yunda_reply(detail: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """The ``replyInfo.html`` form the reply page posts, after its checks."""
    if _text(detail.get("reply_auth")) == "2" or _text(detail.get("prob_status")) in YUNDA_CLOSED_STATUSES:
        raise ReplyRuleError("原系统不允许回复这条问题件（没有回复权限或已完结）。")
    reply = _text(payload.get("reply_text"))
    if not reply:
        raise ReplyRuleError("回复正文不能为空")
    if len(reply) > YUNDA_REPLY_MAX:
        raise ReplyRuleError(f"最多输入{YUNDA_REPLY_MAX}字")
    review = ""
    if _text(detail.get("prob_type_id")) == YUNDA_REVIEW_TYPE_ID and _text(detail.get("review_auth")) == "1":
        review = _text(payload.get("review_entry_status"))
        if review not in {"1", "2"}:
            raise ReplyRuleError("请确认是否同意预约进仓")
    return {"prob_status": yunda_status_code(payload.get("prob_status") or "2"), "review_entry_status": review,
            "reply_text": reply, "prob_main_id": _text(detail.get("prob_main_id")), "old_prob_status": "2"}


__all__ = [
    "RONGHUI_DRIVER_PICK_TYPES", "RONGHUI_PICKUP_HOURS", "RONGHUI_PICK_TYPES", "RONGHUI_RETURN_OPINIONS",
    "ReplyRuleError", "YUNDA_STATUSES", "build_ronghui_reply_row", "pickup_days", "ronghui_bill_status_block",
    "ronghui_reply_block", "ronghui_reply_form", "validate_ronghui_reply", "validate_yunda_reply",
    "yunda_reply_form", "yunda_status_code",
]
