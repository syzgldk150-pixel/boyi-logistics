"""Processing actions copied from the original pages (read-only capture 2026-11-27).

Pure rules, no I/O, the same way ``shared/problem_reply_rules.py`` handles problem replies: what
buttons a record shows and why one is refused (:func:`action_specs`), the checks the original page
runs before saving (``validate_*``) and the rows it saves (``*_tables``). The Agent re-reads the
record, runs these, saves and reads back; the Console renders the specs as the original dialogs.

Spec shape (rendered by ``console/static/original_page_actions.js``)::

    {"kind", "operation", "label", "title", "blocked": "" | reason,
     "fields": [{"name", "label", "type": readonly|text|textarea|money|select|site, "required",
                 "max", "value", "options"}],
     "attachments": None | {"purpose", "required"}, "confirm": "" | question}

融辉工单（“工单接收方查询-工单裁定申诉”）:
* 响应 ``answer`` → 子页“工单管理-跟进-完结”页型 ``_FOLLOW``；完结 ``clickend`` → ``_END``；
  催工单 ``urgeWork`` → ``_URGE``。保存 ``TAB_WORK_ORDER_DETAILS_ADD``（HANDLE_STATE 2/3/4）+
  ``TAB_WORK_ORDER_UPT``（WORK_STATE 3 + RESPONSE_DATE / WORK_STATE 4 + OVER_TIME / URGE_COUNT+1）+
  ``TAB_WORK_ORDER_DETAILS_PIC_ADD``；时间取 ``FIND_NOW``；明细 GUID 为北京时间 yyyyMMddHHmmss +
  随机数 + 登录网点编码（``getWorkOrderCodeTwo``）。
* 申诉 ``appeal`` → 子页“工单管理-申诉”：HANDLE_STATE 7 + APPEAL_STATUS 1 等。
融辉问题件（“收到问题件查询”）: 打开即标已查看（未处理且未查看时 BL_SEE=1）、转发通知网点
（``turnSendData``：SEND_SITE_CODE/SEND_SITE）。
"""
from __future__ import annotations

import datetime as dt
import random
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from shared.original_page_attachments import AttachmentError, attachments, ronghui_attachment_rows
from shared.problem_reply_rules import ronghui_reply_block

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
WORK_FINISHED = "已完结"
WORK_WAIT_HANDLE = "待责任方处理"
WORK_CONTENT_MAX = 500
WORK_MAX_RESPONSES = 2
WORK_SUBPAGE_FOLLOW = "工单管理-跟进-完结"
WORK_SUBPAGE_APPEAL = "工单管理-申诉"
# operation -> (button, dialog title, HANDLE_STATE, page type)
WORK_OPERATIONS = {
    "respond": ("响应", "新增数据", 2, "_FOLLOW"),
    "finish": ("完结", "新增数据", 3, "_END"),
    "urge": ("催工单", "新增数据", 4, "_URGE"),
    "appeal": ("申诉", "申诉", 7, ""),
}


class ActionRuleError(ValueError):
    """The original page would refuse this; the message is the page's own wording."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _field(name: str, label: str, kind: str = "readonly", *, value: Any = "", required: bool = False,
           max_length: int = 0, options: Sequence[Any] = ()) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": name, "label": label, "type": kind, "value": _text(value)}
    if required:
        spec["required"] = True
    if max_length:
        spec["max"] = max_length
    if options:
        spec["options"] = list(options)
    return spec


def _spec(kind: str, operation: str, label: str, title: str, fields: list[dict[str, Any]], *, blocked: str = "",
          attachments_purpose: str = "", attachments_required: bool = False, confirm: str = "") -> dict[str, Any]:
    return {"kind": kind, "operation": operation, "label": label, "title": title, "blocked": blocked,
            "fields": fields, "confirm": confirm,
            "attachments": {"purpose": attachments_purpose, "required": attachments_required} if attachments_purpose else None}


# ---------------------------------------------------------------------------------------------
# 融辉工单
# ---------------------------------------------------------------------------------------------

def work_order_block(operation: str, row: Mapping[str, Any]) -> str:
    """The list page's own checks on the selected row (before opening the dialog)."""
    state = _text(row.get("WORK_STATE"))
    if operation == "respond" and state == WORK_FINISHED:
        return "订单已完结！"
    if operation == "finish" and state != WORK_WAIT_HANDLE:
        return "工单状态暂不支持完结"
    if operation == "urge" and state == WORK_FINISHED:
        return "已完结工单无需催促！"
    if operation == "appeal":
        status = row.get("APPEAL_STATUS")
        if _text(status) not in ("", "0"):
            return "该工单已申诉，请勿重复申诉！"
        if _text(status) != "0":
            return "只有待申诉状态才能操作申诉！"
        if not _text(row.get("VERDICT_DATE")):
            return "裁定审核时间不可为空！"
    return ""


def work_order_specs(row: Mapping[str, Any], user_info: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """响应 / 完结 / 催工单 / 申诉 as the list page's toolbar offers them, with the dialog fields."""
    user_info = user_info or {}
    man, site = _text(user_info.get("loginEmpName")), _text(user_info.get("loginSiteName"))
    specs = []
    for operation in ("respond", "finish", "urge"):
        label, title, _, _ = WORK_OPERATIONS[operation]
        specs.append(_spec("work_order", operation, label, title, [
            _field("HANDLE_MAN", "处理人", value=man), _field("HANDLE_SITE", "处理站点", value=site),
            _field("WORK_TYPE", "工单类型", value=row.get("WORK_TYPE")), _field("WORK_NAME", "工单类别", value=row.get("WORK_NAME")),
            _field("WORK_CONTENT", "工单内容", "readonly_long", value=row.get("WORK_CONTENT")),
            _field("HANDLE_CONTENT", "处理内容", "textarea", required=True, max_length=WORK_CONTENT_MAX),
        ], blocked=work_order_block(operation, row), attachments_purpose="work_order"))
    specs.append(_spec("work_order", "appeal", "申诉", "申诉", [
        _field("BILL_CODE", "运单号", value=row.get("BILL_CODE")), _field("APPEAL_MAN", "申诉人", value=man),
        _field("APPEAL_SITE", "申诉网点", value=site),
        _field("APPEAL_CLAIM_AMOUNT", "索赔金额", "money", value=row.get("VERDICT_DEDUCT_AMOUNT1")),
        _field("HANDLE_CONTENT1", "申诉原因", "textarea", required=True, max_length=WORK_CONTENT_MAX),
    ], blocked=work_order_block("appeal", row), attachments_purpose="work_order"))
    return specs


def _money(value: Any) -> str:
    """索赔金额 ``vtype double:2;range:0,100000``: empty allowed, else 0–100000 with ≤2 decimals."""
    text = _text(value)
    if not text:
        return ""
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ActionRuleError("索赔金额必须是数字") from None
    if amount != amount.quantize(Decimal("0.01")) or not Decimal("0") <= amount <= Decimal("100000"):
        raise ActionRuleError("索赔金额须在 0 到 100000 之间，最多两位小数")
    return text


def validate_work_order(operation: str, row: Mapping[str, Any], payload: Mapping[str, Any], *,
                        response_count: int = 0, same_bill_rows: Sequence[Mapping[str, Any]] = (),
                        type_rows: Sequence[Mapping[str, Any]] = (), server_now: dt.datetime | None = None) -> dict[str, Any]:
    """Everything the list page and the dialog check, in the page's order; returns the cleaned input."""
    if operation not in WORK_OPERATIONS:
        raise ActionRuleError("不支持的工单操作。")
    blocked = work_order_block(operation, row)
    if blocked:
        raise ActionRuleError(blocked)
    if operation == "respond" and response_count >= WORK_MAX_RESPONSES:
        raise ActionRuleError("单个工单最多可响应2次!")
    if operation in ("respond", "finish") and len(same_bill_rows) == 1 and _text(same_bill_rows[0].get("WORK_STATE")) == WORK_FINISHED:
        raise ActionRuleError("工单已完结，无需跟进")
    try:
        files = attachments(payload.get("attachments"), "ronghui", "work_order")
    except AttachmentError as exc:
        raise ActionRuleError(str(exc)) from None
    if operation == "appeal":
        if type_rows:
            kind = type_rows[0]
            if not _text(kind.get("IS_APPEAL")) or _text(kind.get("IS_APPEAL")) == "0":
                raise ActionRuleError("该工单类别未开启申诉！")
            limit = _text(kind.get("APPEAL_TIME_LIMIT"))
            if limit and server_now is not None:
                verdict = dt.datetime.strptime(_text(row.get("VERDICT_DATE"))[:19], DATE_FORMAT)
                if (server_now - verdict).total_seconds() / 3600 >= float(limit):
                    raise ActionRuleError("已经超过申诉时效，不可再申诉！")
        reason = _text(payload.get("HANDLE_CONTENT1"))
        if not reason:
            raise ActionRuleError("申诉原因不能为空")
        if len(reason) > WORK_CONTENT_MAX:
            raise ActionRuleError("申诉原因不能超过500个字")
        attachment_required = bool(type_rows) and _text(type_rows[0].get("IS_ATTACHMENT_REQ")) == "1"
        if attachment_required and not files:
            raise ActionRuleError("必须上传申诉附件！")
        return {"reason": reason, "claim": _money(payload.get("APPEAL_CLAIM_AMOUNT")), "attachments": files}
    content = _text(payload.get("HANDLE_CONTENT"))
    if not content:
        raise ActionRuleError("处理内容不能为空")
    if len(content) > WORK_CONTENT_MAX:
        raise ActionRuleError("处理内容不能超过500个字")
    return {"content": content, "attachments": files}


def work_order_detail_guid(now_utc: dt.datetime, site_code: str, rand: int | None = None) -> str:
    """``getWorkOrderCodeTwo``: Beijing yyyyMMddHHmmss + random (one leading 0 when < 1000) + site code."""
    stamp = (now_utc.astimezone(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y%m%d%H%M%S")
    number = random.randrange(10000) if rand is None else rand
    return f"{stamp}{'0' + str(number) if number < 1000 else number}{site_code}"


def work_order_tables(operation: str, row: Mapping[str, Any], fields: Mapping[str, Any], user_info: Mapping[str, Any],
                      server_now: str, guid: str, now: dt.datetime) -> list[tuple[str, list[dict[str, Any]]]]:
    """The three ``saveTables`` parameters the dialog posts, in the page's order."""
    site, site_code = _text(user_info.get("loginSiteName")), _text(user_info.get("loginSiteCode"))
    man, man_code = _text(user_info.get("loginEmpName")), _text(user_info.get("loginEmpCode"))
    code = _text(row.get("WORK_ORDER_CODE"))
    handler = {"HANDLE_DATE": server_now, "GUID": guid, "HANDLE_SITE": site, "HANDLE_SITE_CODE": site_code,
               "HANDLE_MAN": man, "HANDLE_MAN_CODE": man_code}
    if operation == "appeal":
        detail = {"WORK_ORDER_CODE": code, "BILL_CODE": _text(row.get("BILL_CODE")), "APPEAL_MAN": man, "APPEAL_SITE": site,
                  "APPEAL_CLAIM_AMOUNT": fields["claim"], "HANDLE_CONTENT1": fields["reason"],
                  "IS_ATTACHMENT_REQ": _text(row.get("IS_ATTACHMENT_REQ")), **handler,
                  "HANDLE_STATE": 7, "HANDLE_CONTENT": fields["reason"]}
        update = {"APPEAL_STATUS": 1, "APPEAL_DATE": server_now, "APPEAL_MAN": man, "APPEAL_MAN_CODE": man_code,
                  "APPEAL_SITE": site, "APPEAL_SITE_CODE": site_code, "APPEAL_CLAIM_AMOUNT": fields["claim"],
                  "WORK_ORDER_CODE": code}
    else:
        state = WORK_OPERATIONS[operation][2]
        detail = {"WORK_ORDER_CODE": code, "URGE_COUNT": _text(row.get("URGE_COUNT")), "HANDLE_MAN": man, "HANDLE_SITE": site,
                  "WORK_TYPE": _text(row.get("WORK_TYPE")), "WORK_NAME": _text(row.get("WORK_NAME")),
                  "WORK_CONTENT": _text(row.get("WORK_CONTENT")), "HANDLE_CONTENT": fields["content"],
                  "HANDLE_STATE": state, **handler}
        if operation == "respond":
            update = {"WORK_STATE": 3, "RESPONSE_DATE": server_now}
        elif operation == "finish":
            update = {"WORK_STATE": 4, "OVER_TIME": server_now}
        else:
            try:
                count = int(_text(row.get("URGE_COUNT")) or 0)
            except ValueError:
                count = 0
            update = {"URGE_COUNT": count + 1}
        update["WORK_ORDER_CODE"] = code
    pictures = ronghui_attachment_rows(fields["attachments"], user_info, now, "", ORDER_GUID=guid)
    for picture in pictures:
        picture.pop("FILE_UPLOAD_TYPE", None)  # 工单附件行没有这一列（addInfoToPicDataGrid 不设）
    return [("TAB_WORK_ORDER_DETAILS_ADD", [detail]), ("TAB_WORK_ORDER_UPT", [update]),
            ("TAB_WORK_ORDER_DETAILS_PIC_ADD", pictures)]


def work_order_saved(operation: str, before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Read-back of the work order itself after the detail row is found."""
    if operation == "respond":
        return _text(after.get("WORK_STATE")) == WORK_WAIT_HANDLE
    if operation == "finish":
        return _text(after.get("WORK_STATE")) == WORK_FINISHED
    if operation == "urge":
        try:
            return int(_text(after.get("URGE_COUNT")) or 0) == int(_text(before.get("URGE_COUNT")) or 0) + 1
        except ValueError:
            return False
    return _text(after.get("APPEAL_STATUS")) not in ("", "0")


# ---------------------------------------------------------------------------------------------
# 融辉问题件：标已查看、转发通知网点
# ---------------------------------------------------------------------------------------------

def problem_needs_seen(row: Mapping[str, Any]) -> bool:
    return _text(row.get("BL_CHECKOK_STR")) == "未处理" and _text(row.get("BL_SEE_STR")) == "未查看"


def problem_seen_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"REGISTER_DATE": _text(row.get("REGISTER_DATE")), "BILL_CODE": _text(row.get("BILL_CODE")), "BL_SEE": 1}


def problem_forward_spec(row: Mapping[str, Any], login_site: str) -> dict[str, Any]:
    """“转发通知网点” + “确认转发”: shown with 保存 (same visibility as the reply buttons)."""
    return _spec("problem", "forward", "确认转发", "转发通知网点", [
        _field("NOTICE_SITE", "转发通知网点", "site", value=row.get("SEND_SITE"), required=True),
    ], blocked=ronghui_reply_block(row, login_site), confirm="")


def validate_problem_forward(row: Mapping[str, Any], login_site: str, payload: Mapping[str, Any],
                             sites: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    blocked = ronghui_reply_block(row, login_site)
    if blocked:
        raise ActionRuleError(blocked)
    code = _text(payload.get("site_code"))
    if not code:
        raise ActionRuleError("通知网点不能为空")
    matches = [site for site in sites if _text(site.get("SITE_CODE")) == code]
    if len(matches) != 1:
        raise ActionRuleError("找不到这个网点，请重新选择通知网点。")
    return {"SEND_SITE_CODE": code, "SEND_SITE": _text(matches[0].get("SITE_NAME"))}


def problem_forward_row(row: Mapping[str, Any], site: Mapping[str, str]) -> dict[str, Any]:
    return {"BILL_CODE": _text(row.get("BILL_CODE")), "SEND_SITE_CODE": site["SEND_SITE_CODE"], "SEND_SITE": site["SEND_SITE"],
            "REGISTER_DATE": _text(row.get("REGISTER_DATE"))}


def contact_text(site: Mapping[str, Any] | None) -> str:
    """“登记网点联系人和电话” as ``getSiteInfo`` writes it."""
    site = site or {}
    return f"登记网点：{_text(site.get('SITE_NAME'))}\r\n负责人：{_text(site.get('PRINCIPAL'))}\r\n联系电话：{_text(site.get('PHONE'))}"


SITE_KEY = re.compile(r"^[\w一-龥()（）\-]{1,30}$")


def site_search_key(value: Any) -> str:
    key = _text(value)
    if not SITE_KEY.match(key):
        raise ActionRuleError("请输入网点名称或编号。")
    return key


__all__ = [
    "ActionRuleError", "DATE_FORMAT", "WORK_OPERATIONS", "WORK_SUBPAGE_APPEAL", "WORK_SUBPAGE_FOLLOW", "contact_text",
    "problem_forward_row", "problem_forward_spec", "problem_needs_seen", "problem_seen_row", "site_search_key",
    "validate_problem_forward", "validate_work_order", "work_order_block", "work_order_detail_guid", "work_order_saved",
    "work_order_specs", "work_order_tables",
]
