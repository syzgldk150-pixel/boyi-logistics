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


# ---------------------------------------------------------------------------------------------
# 融辉投诉：被投诉方“回复”（子页“投诉回复-新增”）；投诉方“取消投诉”“提交申诉”“提交材料”
# ---------------------------------------------------------------------------------------------

COMPLAINT_REPLY_MAX = 500
CLAIM_SLOTS = (("claim_waybill", "面单附件图片"), ("claim_goods", "货物附件图片"), ("claim_value", "价值正面图片"))
APPEAL_SLOTS = (("appeal_file1", "附件1"), ("appeal_file2", "附件2"), ("appeal_file3", "附件3"))
NO_APPEAL_ARBITRATION = ("待理赔", "已理赔")


def _slots(slots: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"key": key, "label": label, "purpose": "complaint_image"} for key, label in slots]


def complaint_against_specs(row: Mapping[str, Any], user_info: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """被投诉方查询的“回复”（“新增投诉回复”：回复网点/回复人为当前登录，回复内容必填 ≤500，附件 ≤10 个 20MB）与“新增申诉”。"""
    user_info = user_info or {}
    reply = _spec("complaint_against", "reply", "回复", "新增投诉回复", [
        _field("BILL_CODE", "运单号", value=row.get("BILL_CODE")),
        _field("REPLY_SITE", "回复网点", value=user_info.get("loginSiteName")),
        _field("REPLY_MAN", "回复人", value=user_info.get("loginEmpName")),
        _field("REPLY_CONTENT", "回复内容", "textarea", required=True, max_length=COMPLAINT_REPLY_MAX),
    ], attachments_purpose="complaint_reply")
    return [reply, appeal_form_spec(row)]  # 原页工具栏顺序：回复、新增申诉


def complaint_reply_guid(server_guid: str) -> str:
    """``getGuid``: btoa(FIND_GUID) without + - / =, first 32 characters, upper case."""
    import base64

    encoded = base64.b64encode(_text(server_guid).encode("latin-1")).decode("ascii")
    return re.sub(r"[+\-/=]", "", encoded)[:32].upper()


def validate_complaint_reply(payload: Mapping[str, Any]) -> dict[str, Any]:
    content = _text(payload.get("REPLY_CONTENT"))
    if not content:
        raise ActionRuleError("回复内容不能为空")
    if len(content) > COMPLAINT_REPLY_MAX:
        raise ActionRuleError("回复内容不能超过500个字")
    try:
        files = attachments(payload.get("attachments"), "ronghui", "complaint_reply")
    except AttachmentError as exc:
        raise ActionRuleError(str(exc)) from None
    return {"content": content, "attachments": files}


def complaint_reply_tables(row: Mapping[str, Any], fields: Mapping[str, Any], user_info: Mapping[str, Any], guid: str,
                           now: dt.datetime) -> list[tuple[str, list[dict[str, Any]]]]:
    record = {"PROCESS_TYPE": "投诉回复", "BILL_CODE": _text(row.get("BILL_CODE")), "EXCEPTION_ID": _text(row.get("EXCEPTION_ID")),
              "REPLY_SITE": _text(user_info.get("loginSiteName")), "REPLY_SITE_CODE": _text(user_info.get("loginSiteCode")),
              "REPLY_MAN": _text(user_info.get("loginEmpName")), "REPLY_MAN_CODE": _text(user_info.get("loginEmpCode")),
              "REPLY_CONTENT": fields["content"], "GUID": guid}
    tables = [("TAB_PROCESS_RECORD_ADD", [record])]
    if fields["attachments"]:
        tables.append(("TAB_PROCESS_RECORD_PATH_ADD", ronghui_attachment_rows(
            fields["attachments"], user_info, now, "被投诉方回复附件", PROCESS_RECORD_ID=guid)))
    return tables


def complaint_cancel_block(row: Mapping[str, Any], user_info: Mapping[str, Any]) -> str:
    """``deleteMethod`` on the list row: only the complainant (or the APP complaint's site), never 违禁品."""
    app_own_site = _text(row.get("EXCEPTION_TYPE_DATA_FROM")) == "APP" and _text(user_info.get("loginSiteCode")) == _text(row.get("EXCEPTION_SITE_CODE"))
    if not app_own_site and _text(row.get("EXCEPTION_MAN_CODE")) != _text(user_info.get("loginEmpCode")):
        return "仅支持投诉人取消投诉"
    if _text(row.get("CATEGORY")) == "违禁品":
        return "投诉类型为【违禁品】,不可修改"
    return ""


def complaint_cancel_check(current: Mapping[str, Any] | None) -> None:
    """The fresh ``FIND_TAB_EXCEPTION_REGISTER`` row: refused once either status left 待审核."""
    if current and ((_text(current.get("STATUS")) and _text(current.get("STATUS")) != "待审核")
                    or (_text(current.get("ARBITRATE_STATUS")) and _text(current.get("ARBITRATE_STATUS")) != "待审核")):
        raise ActionRuleError("投诉已审核，不支持取消")


def complaint_appeal_block(row: Mapping[str, Any], submitted: bool = False) -> str:
    """“申诉详情”页签：只在投诉或仲裁审核驳回后开放；复审驳回或已传过申诉附件时按钮置灰。"""
    status, arbitration = row.get("STATUS"), row.get("ARBITRATE_STATUS")
    if status is not None and _text(status) != "审核驳回" and arbitration is not None and _text(arbitration) != "审核驳回":
        return "投诉审核驳回时投诉网点可进行操作申诉！"
    if _text(row.get("AUDIT_STATUS")) == "2" or submitted:
        return "该投诉已提交申诉。"
    return ""


def complaint_claim_block(row: Mapping[str, Any], submitted: bool = False) -> str:
    """“理赔资料补充”页签：处理类型不为空且不是 2 时不开放（原页如此判断）；已传过材料时按钮置灰。"""
    appeal_type = row.get("APPEAL_TYPE")
    if appeal_type is not None and _text(appeal_type) != "2":
        return "仲裁才有理赔的资料补充；普通投诉没有理赔！"
    if submitted:
        return "该投诉已提交材料。"
    return ""


def complaint_filed_specs(row: Mapping[str, Any], user_info: Mapping[str, Any]) -> list[dict[str, Any]]:
    appeal = _spec("complaint_filed", "appeal", "提交申诉", "申诉详情", [
        _field("BILL_CODE", "运单号", value=row.get("BILL_CODE")),
        _field("APPEAL_CONTENT", "申诉说明", "textarea", required=True),
    ], blocked=complaint_appeal_block(row))
    appeal["attachment_slots"] = _slots(APPEAL_SLOTS)
    claim = _spec("complaint_filed", "claim", "提交材料", "理赔资料补充",
                  [_field("BILL_CODE", "运单号", value=row.get("BILL_CODE"))], blocked=complaint_claim_block(row))
    claim["attachment_slots"] = _slots(CLAIM_SLOTS)
    return [
        complaint_form_spec(row, user_info),
        _spec("complaint_filed", "cancel", "取消投诉", "取消投诉", [], blocked=complaint_cancel_block(row, user_info),
              confirm="确定取消投诉？"),
        appeal, claim,
    ]


def _slot_files(payload: Mapping[str, Any], slots: Sequence[tuple[str, str]]) -> list[tuple[str, dict[str, Any]]]:
    result = []
    for key, label in slots:
        try:
            files = attachments(payload.get(key), "ronghui", "complaint_image")
        except AttachmentError as exc:
            raise ActionRuleError(f"{label}：{exc}") from None
        if files:
            result.append((label, files[0]))
    return result


def _within_appeal_period(exception_date: Any, days: int, now: dt.datetime) -> bool:
    """``commentTimeHandle``: only a millisecond timestamp can fail it (a date string gives NaN and passes)."""
    try:
        stamp = float(exception_date)
    except (TypeError, ValueError):
        return True
    return int((now.timestamp() - stamp / 1000) // 86400) <= days


def validate_complaint_appeal(row: Mapping[str, Any], payload: Mapping[str, Any], now: dt.datetime,
                              submitted: bool = False) -> dict[str, Any]:
    blocked = complaint_appeal_block(row, submitted)
    if blocked:
        raise ActionRuleError(blocked)
    content = _text(payload.get("APPEAL_CONTENT"))
    if not content:
        raise ActionRuleError("申诉说明不能为空")
    arbitration = _text(row.get("ARBITRATE_STATUS"))
    if arbitration in NO_APPEAL_ARBITRATION:
        raise ActionRuleError(f"“{arbitration}”仲裁状态下不能提交申诉！")
    appeal_type = _text(row.get("APPEAL_TYPE"))
    days = {"普通投诉处理": 7, "仲裁处理": 3}.get(appeal_type)
    if days is not None and not _within_appeal_period(row.get("EXCEPTION_DATE"), days, now):
        raise ActionRuleError("超过投诉申诉期不允许进行申诉登记！")
    return {"content": content, "files": _slot_files(payload, APPEAL_SLOTS)}


def complaint_appeal_update(row: Mapping[str, Any], fields: Mapping[str, Any], user_info: Mapping[str, Any], server_now: str) -> dict[str, Any]:
    data = {"EXCEPTION_ID": _text(row.get("EXCEPTION_ID")), "APPEAL_CONTENT": fields["content"], "APPEAL_DATE": server_now,
            "APPEAL_SITE": _text(user_info.get("loginSiteName")), "APPEAL_SITE_CODE": _text(user_info.get("loginSiteCode")),
            "APPEAL_TYPE": _text(row.get("APPEAL_TYPE")), "APPEAL_STATUS": "已申诉"}
    for key, source in (("APPEAL_MAN", "loginUserName"), ("APPEAL_MAN_CODE", "loginUserId")):
        if _text(user_info.get(source)):
            data[key] = _text(user_info.get(source))
    data["ARBITRATE_STATUS" if data["APPEAL_TYPE"] == "仲裁处理" else "STATUS"] = "待审核"
    return data


def validate_complaint_claim(row: Mapping[str, Any], payload: Mapping[str, Any], submitted: bool = False) -> dict[str, Any]:
    blocked = complaint_claim_block(row, submitted)
    if blocked:
        raise ActionRuleError(blocked)
    return {"files": _slot_files(payload, CLAIM_SLOTS)}


def complaint_claim_update(row: Mapping[str, Any], user_info: Mapping[str, Any], server_now: str) -> dict[str, Any]:
    data = {"EXCEPTION_ID": _text(row.get("EXCEPTION_ID")), "CLAIM_DATE": server_now,
            "CLAIM_SITE": _text(user_info.get("loginSiteName")), "CLAIM_SITE_CODE": _text(user_info.get("loginSiteCode"))}
    for key, source in (("CLAIM_MAN", "loginUserName"), ("CLAIM_MAN_CODE", "loginUserId")):
        if _text(user_info.get(source)):
            data[key] = _text(user_info.get(source))
    return data


def exception_path_row(exception_id: str, file_name: str, file_path: str, upload_type: str, user_info: Mapping[str, Any],
                       server_now: str) -> dict[str, Any]:
    """``saveExceptionPath``: one ``TAB_EXCEPTION_PATH_ADD`` row per file, saved after the main update."""
    return {"EXCEPTION_ID": exception_id, "FILE_NAME": file_name, "UPLOAD_SITE": _text(user_info.get("loginSiteName")),
            "UPLOAD_SITE_CODE": _text(user_info.get("loginSiteCode")), "UPLOAD_MAN": _text(user_info.get("loginEmpName")),
            "UPLOAD_MAN_CODE": _text(user_info.get("loginEmpCode")), "UPLOAD_DATE": server_now, "FILE_PATH": file_path,
            "FILE_UPLOAD_TYPE": upload_type}


# ---------------------------------------------------------------------------------------------
# 融辉申诉登记“删除”、网点代取件“撤销”
# ---------------------------------------------------------------------------------------------

def appeal_delete_block(row: Mapping[str, Any]) -> str:
    return "" if _text(row.get("COMPLAINT_STATUS")) == "待审核" else "非待审核状态不可删除！"


def appeal_specs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_spec("appeal", "delete", "删除", "删除", [], blocked=appeal_delete_block(row), confirm="确定删除记录？")]


PICKUP_STATUS_CODES = {"待分配": "0", "待接单": "4", "待揽收": "1", "已揽收": "2", "已取消": "3", "撤销待审核": "5"}


def pickup_status(row: Mapping[str, Any]) -> str:
    status = _text(row.get("STATUS"))
    return PICKUP_STATUS_CODES.get(status, status)


def pickup_revoke_block(row: Mapping[str, Any]) -> str:
    return {"2": "订单已揽收录单不支持撤销，请及时联系接单网点处理!", "3": "已取消订单不支持重复撤销!",
            "5": "撤销待审核订单不支持撤销!"}.get(pickup_status(row), "")


def pickup_specs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_spec("pickup", "revoke", "撤销", "撤销", [], blocked=pickup_revoke_block(row),
                  confirm="撤销后订单将取消，确认要撤销吗？")]


def pickup_revoke_row(row: Mapping[str, Any], user_info: Mapping[str, Any], server_now: str) -> dict[str, Any]:
    return {"TX_LOGISTIC_ID": _text(row.get("TX_LOGISTIC_ID")), "STATUS": 3, "REVOKE_DATE": server_now,
            "REVOKE_MAN": _text(user_info.get("loginEmpName")), "REVOKE_MAN_CODE": _text(user_info.get("loginEmpCode"))}


# ---------------------------------------------------------------------------------------------
# 韵达快运工单（CSSP“工单处理”详情页按钮、“催单待回复”回复）
# ---------------------------------------------------------------------------------------------

# 处理状态 compStatus（CSSP e5f8）：0 已发起 1 已分配 2 已响应 3 已首回 4 已结单 5 差评 6 延时 7 转站点 8 转理赔 10 已接单
CSSP_TIMED = ("claim", "extend", "turn_site")  # 原页只允许 8:00–18:59 提交
CSSP_IDENTITIES = ("投诉人", "发件人", "收件人", "第三方", "其他站点")
CSSP_RESPONSE_DEFAULT = "已联系投诉人响应工单诉求。"
CSSP_TEXT_MAX = 200
CSSP_NAME_MAX = 20
# operation -> (button, endpoint)
CSSP_OPERATIONS = {
    "prompt_resp": ("立即响应", "/cssp/comp/sysCompDeal/promptResp.do"),
    "first_reply": ("首次回复", "/cssp/comp/sysCompDeal/firstReply.do"),
    "reply_handling": ("回复处理", "/cssp/comp/sysCompDeal/replyHandling.do"),
    "claim": ("转至理赔", "/cssp/comp/sysCompDeal/claimApply.do"),
    "statement": ("结单", "/cssp/comp/sysCompDeal/statement.do"),
    "extend": ("延长时效", "/cssp/comp/sysCompDeal/extendLaging.do"),
    "turn_site": ("转站点", "/cssp/comp/sysCompDeal/turnOrg.do"),
}
CSSP_CUI_REPLY = "/cssp/collaboration/sysCollaborationManage/cuiReplay"
# 原页按钮的 v-control 权限（账号的权限来自 /cssp/sys/menu/newTree 中 type 为 2/3 的节点 perms）
CSSP_PERMISSIONS = {
    "prompt_resp": "comp:sysCompDeal:promptResp", "first_reply": "comp:sysCompDeal:firstReply",
    "reply_handling": "comp:sysCompDeal:replyHandling", "claim": "comp:sysCompDeal:claimApply",
    "statement": "comp:sysCompDeal:statement", "extend": "comp:sysCompDeal:extendLaging", "turn_site": "comp:sysCompDeal:turnOrg",
    "appoint": "comp:sysCompDeal:receiveAppointUser", "receive": "comp:sysCompDeal:receive",
    "urgent_receive": "comp:sysCompDeal:urgentReceive", "cui_reply": "collaboration:sysCollaborationManage:cuiReplay",
}
CSSP_APPOINT_STATUSES = (1, 10, 2, 3)  # 列表复选框只允许已分配、已接单、已响应、已首回的工单
CSSP_RECEIVE = {"receive": ("接单", "/cssp/comp/sysCompDeal/receive.do", "/cssp/comp/sysCompDeal/receiveNumQuery"),
                "urgent_receive": ("加急工单接单", "/cssp/comp/sysCompDeal/urgentReceive.do",
                                   "/cssp/comp/sysCompDeal/urgentReceiveNumQuery")}


def cssp_permissions(tree: Any) -> frozenset[str]:
    """``D()``: perms of every node whose attributes.type is "3" or 2."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, Mapping):
            attributes = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
            if attributes.get("type") in ("3", 2) and _text(attributes.get("perms")):
                found.add(_text(attributes.get("perms")))
            walk(node.get("children") or [])

    walk(tree.get("data") if isinstance(tree, Mapping) and "data" in tree else tree)
    return frozenset(found)


def _allowed(operation: str, permissions: Any) -> bool:
    return permissions is None or CSSP_PERMISSIONS.get(operation, "") in permissions


def _status(value: Any) -> int | None:
    try:
        return int(_text(value))
    except ValueError:
        return None


def cssp_block(operation: str, detail: Mapping[str, Any]) -> str:
    """The detail page's ``disabled`` rule for each button (the original greys it out)."""
    status = _status(detail.get("compStatus"))
    allowed = {
        "prompt_resp": status == 10, "first_reply": status == 2, "reply_handling": status == 3,
        "claim": status not in (0, 4, 5, 6, 7, 8), "statement": status in (3, 4),
        "extend": status in (1, 2, 3), "turn_site": status in (1, 10, 2, 3),
    }.get(operation, False)
    return "" if allowed else f"当前处理状态（{_text(detail.get('compStatusName')) or '未知'}）不能{CSSP_OPERATIONS[operation][0]}"


def _autofill(detail: Mapping[str, Any]) -> dict[str, list[str]]:
    return {"投诉人": [_text(detail.get("comper")), _text(detail.get("comperPhone"))],
            "发件人": [_text(detail.get("sender")), _text(detail.get("senderPhone"))],
            "收件人": [_text(detail.get("receiver")), _text(detail.get("receiverPhone"))]}


def _contact_fields(detail: Mapping[str, Any], *, radio: bool) -> list[dict[str, Any]]:
    if radio:  # 回复处理、结单：外呼人身份单选，选投诉人/发件人/收件人时带出姓名电话
        identity = _field("putboundIdentity", "外呼人身份", "radio", value="投诉人", options=CSSP_IDENTITIES)
        identity["autofill"] = {"targets": ["customerName", "callNumber"], "values": _autofill(detail)}
    else:  # 立即响应、首次回复：外呼人身份是“投诉人”开关
        identity = _field("putboundIdentity", "外呼人身份", "switch", value="投诉人")
        identity.update({"on": "投诉人", "off": " ", "note": "投诉受理中请先与投诉人联系"})
    return [identity,
            _field("customerName", "姓名", "text", value=detail.get("comper"), required=True, max_length=CSSP_NAME_MAX),
            _field("callNumber", "外呼电话", "digits", value=detail.get("comperPhone"), required=True)]


def cssp_appoint_spec(detail: Mapping[str, Any]) -> dict[str, Any]:
    """列表“转人员”：转入人员从本机构人员中查（getDeptEmp，按姓名或工号），必选。"""
    person = _field("userId", "转入人员", "site", required=True)
    person.update({"lookup_kind": "cssp", "lookup_operation": "people", "payload_key": "user_code", "placeholder": "输入姓名或工号"})
    blocked = "" if _status(detail.get("compStatus")) in CSSP_APPOINT_STATUSES else "该工单当前状态不能转人员"
    return _spec("cssp", "appoint", "转人员", "转人员", [person], blocked=blocked)


def cssp_specs(detail: Mapping[str, Any], permissions: Any = None) -> list[dict[str, Any]]:
    content = lambda label, value="": _field("dealContent", label, "textarea", value=value, required=True, max_length=CSSP_TEXT_MAX)  # noqa: E731
    first = _contact_fields(detail, radio=False) + [
        _field("estimatedCompletionTime", "预计解决时间", "datetime", required=True), content("解决进度")]
    specs = [
        _spec("cssp", "prompt_resp", "立即响应", "立即响应", _contact_fields(detail, radio=False) + [content("响应内容", CSSP_RESPONSE_DEFAULT)]),
        _spec("cssp", "first_reply", "首次回复", "首次回复", first),
        _spec("cssp", "reply_handling", "回复处理", "回复处理", _contact_fields(detail, radio=True) + [content("沟通记录")],
              attachments_purpose="cssp_reply"),
        _spec("cssp", "claim", "转至理赔", "转至理赔", [content("情况说明")]),
        _spec("cssp", "statement", "结单", "结单", _contact_fields(detail, radio=True) + [content("处理结果")]),
        _spec("cssp", "extend", "延长时效", "延长时效", [content("延长原因")]),
        _spec("cssp", "turn_site", "转站点", "转站点", [content("申请说明")]),
    ]
    for spec in specs:
        spec["blocked"] = cssp_block(spec["operation"], detail)
    specs.append(cssp_appoint_spec(detail))
    return [spec for spec in specs if _allowed(spec["operation"], permissions)]  # 没有权限的按钮原页不显示


def validate_cssp(operation: str, detail: Mapping[str, Any], payload: Mapping[str, Any], now: dt.datetime) -> dict[str, Any]:
    if operation not in CSSP_OPERATIONS:
        raise ActionRuleError("不支持的工单操作。")
    blocked = cssp_block(operation, detail)
    if blocked:
        raise ActionRuleError(blocked)
    if operation in CSSP_TIMED and not 8 <= now.hour <= 18:
        raise ActionRuleError("请于上午8:00至下午18:00提交该申请")
    params: dict[str, Any] = {}
    if operation in ("prompt_resp", "first_reply", "reply_handling", "statement"):
        identity = _text(payload.get("putboundIdentity")) if payload.get("putboundIdentity") != " " else " "
        allowed = CSSP_IDENTITIES if operation in ("reply_handling", "statement") else ("投诉人", " ")
        if identity not in allowed:
            raise ActionRuleError("请选择外呼人身份")
        name, phone = _text(payload.get("customerName")), _text(payload.get("callNumber"))
        if not name:
            raise ActionRuleError("请输入姓名")
        if len(name) > CSSP_NAME_MAX:
            raise ActionRuleError("姓名不能超过20个字")
        if not phone or not re.fullmatch(r"\d+", phone):
            raise ActionRuleError("请输入正确的外呼电话")
        params.update({"putboundIdentity": identity, "customerName": name, "callNumber": phone})
    if operation == "first_reply":
        when = _text(payload.get("estimatedCompletionTime"))
        try:
            moment = dt.datetime.strptime(when, "%Y-%m-%d %H:%M")
        except ValueError:
            raise ActionRuleError("请选择预计解决时间") from None
        if moment.date() < now.date():
            raise ActionRuleError("预计解决时间不能早于今天")
        params["estimatedCompletionTime"] = when
    text = _text(payload.get("dealContent"))
    if not text:
        raise ActionRuleError("请填写内容")
    if len(text) > CSSP_TEXT_MAX:
        raise ActionRuleError("内容不能超过200个字")
    params["dealContent"] = text
    files: list[dict[str, Any]] = []
    if operation == "reply_handling":
        try:
            files = attachments(payload.get("attachments"), "yunda", "cssp_reply")
        except AttachmentError as exc:
            raise ActionRuleError(str(exc)) from None
    return {"params": params, "files": files}


def cui_reply_specs(row: Mapping[str, Any], permissions: Any = None) -> list[dict[str, Any]]:
    return [cui_reply_spec(row)] if _allowed("cui_reply", permissions) else []


def cui_reply_spec(row: Mapping[str, Any]) -> dict[str, Any]:
    return _spec("cssp_cui", "reply", "回复", "催单回复", [
        _field("remark", "催单内容", "readonly_long", value=row.get("remark")),
        _field("replayCustomerName", "姓名", "text", required=True, max_length=CSSP_NAME_MAX),
        _field("replayCustomerPhone", "外呼电话", "digits", required=True),
        _field("replayContent", "沟通记录", "textarea", required=True, max_length=CSSP_TEXT_MAX),
    ])


def cssp_receive_specs(permissions: Any = None) -> list[dict[str, Any]]:
    """工单处理列表工具栏的“加急工单接单”“接单”（打开时读本站待接单量，本次接单量为不小于 1 的整数）。"""
    specs = []
    for operation in ("urgent_receive", "receive"):
        if not _allowed(operation, permissions):
            continue
        waiting = _field("wait_num", "本站待接单量")
        waiting["lookup"] = {"kind": "cssp", "operation": f"{operation}_count"}
        specs.append(_spec("cssp", operation, CSSP_RECEIVE[operation][0], CSSP_RECEIVE[operation][0], [
            waiting, _field("receiveNum", "本次接单量", "digits", required=True)]))
    return specs


def validate_receive_num(payload: Mapping[str, Any]) -> int:
    text = _text(payload.get("receiveNum"))
    if not text:
        raise ActionRuleError("此项必填")
    if not re.fullmatch(r"-?\d+", text):
        raise ActionRuleError("数值必须为整数")
    if int(text) < 1:
        raise ActionRuleError("数值不能小于1")
    return int(text)


def validate_cui_reply(payload: Mapping[str, Any]) -> dict[str, str]:
    name, phone, content = (_text(payload.get(key)) for key in ("replayCustomerName", "replayCustomerPhone", "replayContent"))
    if not name:
        raise ActionRuleError("请输入姓名")
    if len(name) > CSSP_NAME_MAX:
        raise ActionRuleError("姓名不能超过20个字")
    if not phone or not re.fullmatch(r"\d+", phone):
        raise ActionRuleError("请输入正确的外呼电话")
    if not content:
        raise ActionRuleError("请填写沟通记录")
    if len(content) > CSSP_TEXT_MAX:
        raise ActionRuleError("沟通记录不能超过200个字")
    return {"replayCustomerName": name, "replayCustomerPhone": phone, "replayContent": content}


# ---------------------------------------------------------------------------------------------
# 韵达调拨件确认：接单、转单、异常登记
# ---------------------------------------------------------------------------------------------

TRANSFER_ABNORMAL_TYPES = (
    ("1", "体积重量不符"), ("9", "发布网点余额不足"), ("11", "客户虚假电话"), ("12", "取件地址错误"), ("13", "客户取消发货"),
    ("14", "末端网点关停"), ("15", "不可抗力(极端天气等自然灾害）"), ("16", "客户不在家"), ("17", "交货方式更改为自送"),
    ("18", "客户上门地址变更"), ("19", "交通事故"), ("20", "货未贴箱唛或运单破损"), ("2", "客户联系不上"), ("3", "货物需二次包装"),
    ("4", "违禁品"), ("5", "超标件"), ("6", "客户货未备齐或无货"), ("7", "预约上门取件"), ("8", "超区"), ("10", "其他异常"),
)
TRANSFER_REMARK_MAX = 50
TRANSFER_IMAGE_SLOTS = (("abnormal_image1", "图片1"), ("abnormal_image2", "图片2"), ("abnormal_image3", "图片3"))


def transfer_accept_confirm(row: Mapping[str, Any]) -> str:
    if _text(row.get("Order_Source")) == "690" and ("镇上自提" in _text(row.get("Ext_Field4")) or _text(row.get("Special_Area_Code"))):
        return "此单目的地址为“特殊区域/镇上自提/仓库”可能有加收，请核对后联系客户确认。是否确认接单？"
    return "是否确认接单？"


def transfer_abnormal_block(row: Mapping[str, Any]) -> str:
    abnormal = _text(row.get("Abnormal_Type"))
    if abnormal and not (abnormal == "客户货未备齐或无货" and _text(row.get("Delay_Day")) != "-"):
        return "已做过异常登记无需再次登记"
    if _text(row.get("Replace_Status_Code")) not in ("0", "1"):
        return "只有待接单、已接单支持异常登记"
    return ""


TRANSFER_DELAY_REASON_MAX = 50
TRANSFER_REPLY_MAX = 200
TRANSFER_DELAY_IMAGE_SLOTS = (("delay_image1", "图片1"), ("delay_image2", "图片2"), ("delay_image3", "图片3"))


def transfer_delay_days(today: dt.date) -> list[list[str]]:
    """``settimeselect``: 1–29 天，显示为“月-日”（月补零、日不补零）。"""
    return [[str(days), f"{(today + dt.timedelta(days=days)).month:02d}-{(today + dt.timedelta(days=days)).day}"] for days in range(1, 30)]


def transfer_delay_block(row: Mapping[str, Any]) -> str:
    if _text(row.get("Is_Waybill_Overtime")) == "是" or _text(row.get("Is_Collect_Overtime")) == "是":
        return "揽发超时、开单超时的数据不支持延迟发货!"
    if _text(row.get("Replace_Status")) == "3":
        return "该单已撤销,不允许延迟发货!"
    try:
        audited = float(_text(row.get("Delay_Audit_Status")) or 0)
    except ValueError:
        audited = 0
    if audited > 1:
        return "请注意每条调拨件只能延迟发货一次"
    return ""


def transfer_delay_confirm(row: Mapping[str, Any]) -> str:
    if _text(row.get("Order_Source")) == "676":
        return "拼多多跨境订单延迟发货审核后，将会\"撤销订单\"，请上传真实有效的证明图片，虚假图片一经核实，将处以200元/票的处罚，是否确定提交?"
    return ""


def transfer_reply_block(row: Mapping[str, Any]) -> str:
    return "" if _text(row.get("Replace_Status_Code")) in ("0", "1") else "只有待接单、已接单的调拨件可以回复"


def _abnormal_code(value: Any) -> str:
    text = _text(value)
    return next((code for code, label in TRANSFER_ABNORMAL_TYPES if text in (code, label)), "")


def transfer_specs(row: Mapping[str, Any], today: dt.date | None = None) -> list[dict[str, Any]]:
    abnormal = _spec("transfer", "abnormal", "异常登记", "异常登记", [
        _field("Abnormal_Type", "异常类型", "select", required=True, options=[list(item) for item in TRANSFER_ABNORMAL_TYPES]),
        _field("Abnormal_Remark", "详细描述", "textarea", max_length=TRANSFER_REMARK_MAX),
    ], blocked=transfer_abnormal_block(row))
    abnormal["attachment_slots"] = [{"key": key, "label": label, "purpose": "transfer"} for key, label in TRANSFER_IMAGE_SLOTS]
    transfer = _spec("transfer", "transfer", "转单", "调拨件转单", [
        _field("Replace_Site", "转单网点", "site", required=True)])
    transfer["fields"][0]["lookup"] = "yunda_site"
    delay = _spec("transfer", "delay", "延迟发货", "延迟发货", [
        _field("notice", "提示", value="请注意 每条调拨件只能延迟发货一次。"),
        _field("Delay_Day", "延迟时间", "select", required=True, options=transfer_delay_days(today or dt.date.today())),
        _field("Delay_Reason", "延迟原因", "textarea", required=True, max_length=TRANSFER_DELAY_REASON_MAX),
    ], blocked=transfer_delay_block(row), confirm=transfer_delay_confirm(row))
    delay["attachment_slots"] = [{"key": key, "label": label, "purpose": "transfer"} for key, label in TRANSFER_DELAY_IMAGE_SLOTS]
    salesman = _field("Job_Code", "新业务员", "site", required=True)
    salesman.update({"lookup_kind": "transfer", "lookup_operation": "salesman", "payload_key": "job_code", "placeholder": "输入业务员姓名或工号"})
    job = _spec("transfer", "job", "转业务员", "转业务员", [_field("Job_Name", "原业务员", value=_text(row.get("Job_Name")) or "无"), salesman])
    reply = _spec("transfer", "reply", "回复", "回复", [
        _field("Abnormal_Type", "异常类型", "select", value=_abnormal_code(row.get("Abnormal_Type")),
               options=[list(item) for item in TRANSFER_ABNORMAL_TYPES]),
        _field("Abnormal_Remark", "详细描述", "readonly_long", value=row.get("Abnormal_Remark")),
        _field("Reply_Content", "异常说明", "textarea", max_length=TRANSFER_REPLY_MAX),
    ], blocked=transfer_reply_block(row))
    return [_spec("transfer", "accept", "接单", "提示", [], confirm=transfer_accept_confirm(row)), transfer, abnormal, delay, job, reply]


def validate_transfer_delay(row: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    blocked = transfer_delay_block(row)
    if blocked:
        raise ActionRuleError(blocked)
    day = _text(payload.get("Delay_Day"))
    if day not in {str(days) for days in range(1, 30)}:
        raise ActionRuleError("请选延迟时间")
    reason = _text(payload.get("Delay_Reason"))
    if not reason:
        raise ActionRuleError("延迟原因不能为空")
    if len(reason) > TRANSFER_DELAY_REASON_MAX:
        raise ActionRuleError("延迟原因不能大于50个字符")
    images = []
    for index, (key, label) in enumerate(TRANSFER_DELAY_IMAGE_SLOTS, start=1):
        try:
            files = attachments(payload.get(key), "yunda", "transfer")
        except AttachmentError as exc:
            raise ActionRuleError(f"{label}：{exc}") from None
        if files:
            images.append((index, files[0]))
    return {"Delay_Day": day, "Delay_Reason": reason, "images": images}


def validate_transfer_reply(row: Mapping[str, Any], detail: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, str]:
    """详情页“回复”：异常类型可改选，详细描述取详情原值，异常说明去首尾空白后最多 200 字（原页不要求必填）。"""
    blocked = transfer_reply_block({**row, "Replace_Status_Code": _text(detail.get("Replace_Status") or row.get("Replace_Status_Code"))})
    if blocked:
        raise ActionRuleError(blocked)
    content = _text(payload.get("Reply_Content"))
    if len(content) > TRANSFER_REPLY_MAX:
        raise ActionRuleError("最多不可以超过200字符！")
    kind = _text(payload.get("Abnormal_Type"))
    if kind and kind not in dict(TRANSFER_ABNORMAL_TYPES):
        raise ActionRuleError("请选择异常类型")
    return {"Abnormal_Type": kind, "Abnormal_Remark": _text(detail.get("Abnormal_Remark")), "Reply_Content": content}


def validate_transfer_abnormal(row: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    blocked = transfer_abnormal_block(row)
    if blocked:
        raise ActionRuleError(blocked)
    kind = _text(payload.get("Abnormal_Type"))
    if kind not in dict(TRANSFER_ABNORMAL_TYPES):
        raise ActionRuleError("请选择异常类型")
    remark = _text(payload.get("Abnormal_Remark"))
    if len(remark) > TRANSFER_REMARK_MAX:
        raise ActionRuleError("详细描述最多50字")
    images = []
    for index, (key, label) in enumerate(TRANSFER_IMAGE_SLOTS, start=1):
        try:
            files = attachments(payload.get(key), "yunda", "transfer")
        except AttachmentError as exc:
            raise ActionRuleError(f"{label}：{exc}") from None
        if files:
            images.append((index, files[0]))
    return {"Abnormal_Type": kind, "Abnormal_Remark": remark, "label": dict(TRANSFER_ABNORMAL_TYPES)[kind], "images": images}


# 韵达快运仲裁查询“留言”（arbitramentSearch/saveMessage，messageType=message，1–1000 字）
ARBITRATION_MESSAGE_MAX = 1000


def arbitration_specs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_spec("arbitration", "message", "留言", "留言", [
        _field("message", "留言", "textarea", required=True, max_length=ARBITRATION_MESSAGE_MAX)])]


def validate_arbitration_message(payload: Mapping[str, Any]) -> str:
    text = _text(payload.get("message"))
    if not 0 < len(text) <= ARBITRATION_MESSAGE_MAX:
        raise ActionRuleError("填写留言内容限制1~1000字以内！")
    return text


# ---------------------------------------------------------------------------------------------
# 融辉“新增申诉”（被投诉方查询 → 子页“申诉登记_测试_工单”）
# ---------------------------------------------------------------------------------------------

APPEAL_FORM_SUBPAGE = "申诉登记_测试_工单"
APPEAL_FORM_FILES = 10
APPEAL_FREE_DUPLICATE = "云呼未接申述"
APPEAL_WEIGHT_TYPE = "偷重偷方"


def appeal_side(login_site_type: str) -> str:
    """``xxxChangeMethod``: 申诉方按登录网点类型取“中心”或“网点”。"""
    site_type = _text(login_site_type)
    return "中心" if "中心" in site_type else "网点" if "网点" in site_type else ""


def appeal_form_spec(row: Mapping[str, Any]) -> dict[str, Any]:
    process = _field("PROCESS_TYPE", "处理类型", "select", value=row.get("APPEAL_TYPE"), required=True)
    process["lookup"] = {"kind": "appeal_form", "operation": "process_types"}
    appeal = _field("APPEAL_TYPE", "申诉类型", "select", required=True)
    appeal["lookup"] = {"kind": "appeal_form", "operation": "appeal_types", "depends_on": "PROCESS_TYPE"}
    appeal["effects"] = {"placeholder": {"target": "APPEAL_CONTENT", "meta": "TIP"},
                         "show_when": {"target": "PRACTICAL_WEIGHT", "equals": APPEAL_WEIGHT_TYPE}}
    weight = _field("PRACTICAL_WEIGHT", "实际重量", "money")
    weight["hidden"] = True
    spec = _spec("complaint_against", "appeal", "新增申诉", "新增数据", [
        _field("BILL_CODE", "运单号", value=row.get("BILL_CODE")), process, appeal,
        _field("PHONE", "联系方式", "text", required=True),
        _field("CLAIM_AMOUNT", "索赔金额", "money", value=row.get("EXCEPTION_VOLUME"), required=True),
        _field("APPEAL_CONTENT", "申诉内容", "textarea"), weight,
    ])
    spec["attachment_slots"] = [{"key": f"appeal_file{i}", "label": f"附件{i}", "purpose": "appeal"} for i in range(1, APPEAL_FORM_FILES + 1)]
    return spec


def appeal_create_spec(accounts: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """申诉登记页“新增”（addMethod33，与“新增申诉”同一子页，但运单号、处理类型、索赔金额由用户填写）。"""
    spec = appeal_form_spec({})
    spec.update({"kind": "appeal", "operation": "create", "label": "新增", "title": "新增数据"})
    spec["fields"][0] = _field("BILL_CODE", "运单号", "text", required=True, max_length=30)
    spec["account_options"] = [list(item) for item in accounts]
    return spec


def validate_appeal_form(row: Mapping[str, Any], payload: Mapping[str, Any], *, bill: Mapping[str, Any] | None,
                         appeal_type: Mapping[str, Any] | None, appeal_term_days: int | None, duplicate: bool,
                         stakeholder: bool, trajectory_sites: Sequence[str], login_site_code: str) -> dict[str, Any]:
    """``getException`` + ``saveMethod`` 的检查，按原页顺序。"""
    bill_code = _text(row.get("BILL_CODE"))
    if bill is None:
        raise ActionRuleError("未获取到相关运单信息!")
    if _text(bill.get("IS_RETURN_BILL")) == "1":
        raise ActionRuleError("回单禁止申诉!")
    process, kind = _text(payload.get("PROCESS_TYPE")), _text(payload.get("APPEAL_TYPE"))
    phone, claim = _text(payload.get("PHONE")), _text(payload.get("CLAIM_AMOUNT"))
    if not (process and kind and phone and claim) or (appeal_type is None):
        raise ActionRuleError("请将数据填写完整")
    if _text(appeal_type.get("IS_COMPLAINT_CONTENT")) == "1" and not _text(payload.get("APPEAL_CONTENT")):
        raise ActionRuleError("请将数据填写完整")
    weight = _text(payload.get("PRACTICAL_WEIGHT")) if kind == APPEAL_WEIGHT_TYPE else ""
    if weight:
        try:
            value = Decimal(weight)
        except InvalidOperation:
            raise ActionRuleError("实际重量必须是数字") from None
        if value != value.quantize(Decimal("0.01")) or not Decimal("0") <= value <= Decimal("100000"):
            raise ActionRuleError("实际重量须在 0 到 100000 之间，最多两位小数")
    if appeal_term_days is not None and appeal_term_days < 0:
        raise ActionRuleError("超过投诉申诉期不允许进行申诉登记！")  # 投诉日期为当天，只有负的申诉期会拦下
    if duplicate and kind != APPEAL_FREE_DUPLICATE:
        raise ActionRuleError("同一个运单号，同一个部门，同一个类型，只能登记一次！")
    files = []
    for index in range(1, APPEAL_FORM_FILES + 1):
        try:
            picked = attachments(payload.get(f"appeal_file{index}"), "ronghui", "appeal")
        except AttachmentError as exc:
            raise ActionRuleError(f"附件{index}：{exc}") from None
        if picked:
            files.append((index, picked[0]))
    if _text(appeal_type.get("BL_EVIDENCE")) == "1" and not any(index <= 3 for index, _file in files):
        raise ActionRuleError("请上传附件")
    if stakeholder and login_site_code not in set(trajectory_sites):
        raise ActionRuleError(f"申诉网点与【{bill_code}】无关联关系，无法提交数据")
    return {"PROCESS_TYPE": process, "APPEAL_TYPE": kind, "APPEAL_TYPE_LB": _text(appeal_type.get("APPEAL_TYPE_LB")),
            "PHONE": phone, "CLAIM_AMOUNT": claim, "APPEAL_CONTENT": _text(payload.get("APPEAL_CONTENT")),
            "PRACTICAL_WEIGHT": weight, "PRODUCT_TYPE": _text(bill.get("PRODUCT_TYPE")), "files": files}


def appeal_form_exception_id(now: dt.datetime, rand: int | None = None) -> str:
    """原页初始化：yyyyMMddHHmmss + 10000–99998 的随机数作为“流水号”。"""
    return now.strftime("%Y%m%d%H%M%S") + str(random.randrange(10000, 99999) if rand is None else rand)


def appeal_form_tables(fields: Mapping[str, Any], user_info: Mapping[str, Any], bill_code: str, exception_id: str,
                       server_now: str) -> list[tuple[str, list[dict[str, Any]]]]:
    data = {key: fields[key] for key in ("PROCESS_TYPE", "APPEAL_TYPE", "APPEAL_TYPE_LB", "PHONE", "CLAIM_AMOUNT", "APPEAL_CONTENT",
                                         "PRACTICAL_WEIGHT", "PRODUCT_TYPE")}
    data.update({"EXCEPTION_DATE": server_now, "BILL_CODE": bill_code, "EXCEPTION_ID": exception_id,
                 "APPEAL_SITE_CODE": _text(user_info.get("loginSiteCode")), "APPEAL_SITE": _text(user_info.get("loginSiteName")),
                 "REGISTER_DATE": server_now, "CREATE_DATE": server_now, "CREATE_SITE": _text(user_info.get("loginSiteName")),
                 "COMPLAINT_STATUS": "待审核"})
    for index in range(1, APPEAL_FORM_FILES + 1):
        data[f"FILE_PATH{index}"] = ""
    for index, file in fields["files"]:
        data[f"FILE_PATH{index}"] = _text(file.get("file_path"))
    for key in ("CREATE_MAN", "APPEAL_MAN"):
        if _text(user_info.get("loginUserName")):
            data[key] = _text(user_info.get("loginUserName"))
    tables = [("TAB_EXCEPTION_APPEAL_ADD", [data])]
    if fields["files"]:
        tables.append(("TAB_EXCEPTION_PATH_ADD", [exception_path_row(exception_id, _text(file.get("file_name")), _text(file.get("file_path")),
                                                                     "申诉附件", user_info, server_now) for _index, file in fields["files"]]))
    return tables


# ---------------------------------------------------------------------------------------------
# 融辉投诉方登记“新增”“修改”（子页“投诉登记-异形件”，saveExceptionData）
# ---------------------------------------------------------------------------------------------

COMPLAINT_FORM_SUBPAGE = "投诉登记-异形件"
COMPLAINT_ATTACHMENT_TYPE = "投诉方附件"
COMPLAINT_UNFIXED_REASONS = ("受污染", "液体泄露", "浸湿", "破损程度无法修复", "缺少修复工具", "货物变形", "缺少操作时间",
                             "无修复意识", "修复费用问题", "其它")
COMPLAINT_DAMAGE_CATEGORIES = ("破损类", "破损类修复")
COMPLAINT_COLLAR_TYPE = "围板箱录单不规范"
COMPLAINT_CAR_TYPE = "延误请车"
COMPLAINT_CAR_MESSAGE = ("经核查，该单号未按流程先上报工单“客诉加急请车或加急请车”。根据规定，此类情况不予受理仲裁延误请车申请。"
                         "所有请车后次日及之后补登记的工单，均做驳回处理，请知悉。")
# 表单 getData() 带出的全部字段（含隐藏字段），按原页顺序
COMPLAINT_FORM_FIELDS = (
    "EXCEPTION_ID", "SEND_SITE", "SEND_SITE_CODE", "DISPATCH_SITE", "DISPATCH_SITE_CODE", "SEND_COMPANY", "RECEIVER_COMPANY",
    "DISPATCH_FINANCE_CENTER", "DISPATCH_FINANCE_CENTER_CODE", "EXCEPTION_FINANCE_CENTER", "EXCEPTION_FINANCE_CENTER_CODE",
    "EXCEPTION_SIDE_F_CENTER", "EXCEPTION_SIDE_F_CENTER_CODE", "EXCEPTION_MAN", "EXCEPTION_MAN_CODE", "SEND_DATE", "GOODS_NAME",
    "PIECE_NUMBER", "GOODS_VALUES", "SEND_ADDRESS", "RECEIVER_ADDRESS", "REGISTER_DATE", "SIGN_DATE", "BL_NOTITY_DEBITEDPPARTY",
    "BL_BANGDING_SITE", "EXCEPTION_MAN_TYPE", "BILL_CODE", "OBJECT_TYPE", "APPEAL_TYPE", "CATEGORY", "EXCEPTION_TYPE",
    "COLLAR_NUMBER_LIST", "EXCEPTIONSITE_SIDE_CODE", "EXCEPTIONSITE_SIDE", "EXCEPTION_DEPT_SIDE_CODE", "EXCEPTION_DEPT_SIDE",
    "EXCEPTION_MAN_SIDE_CODE", "EXCEPTION_MAN_SIDE", "BL_AGENT", "EXCEPTION_MAN_PHONE", "EXCEPTION_SITE_CODE", "EXCEPTION_SITE",
    "DEPT_NAME", "EXCEPTION_VOLUME", "LOSS_NUMBER", "LOSS_GOODS_NAME", "COMPLAINANT_NAME", "GOODS_PRICE", "IS_FIXED",
    "UNFIXED_REASON", "REMARK", "EXCEPTION_TYPE_DATA_FROM", "OTHER_REASON",
)
# 用户在对话框里填写的字段（其余由运单、类型、网点查询带出）
COMPLAINT_USER_FIELDS = (
    "EXCEPTION_MAN_TYPE", "BILL_CODE", "OBJECT_TYPE", "APPEAL_TYPE", "CATEGORY", "EXCEPTION_TYPE", "COLLAR_NUMBER_LIST",
    "EXCEPTIONSITE_SIDE_CODE", "EXCEPTION_DEPT_SIDE_CODE", "EXCEPTION_MAN_SIDE_CODE", "BL_AGENT", "EXCEPTION_MAN_PHONE",
    "EXCEPTION_SITE_CODE", "EXCEPTION_VOLUME", "LOSS_NUMBER", "LOSS_GOODS_NAME", "COMPLAINANT_NAME", "GOODS_PRICE", "IS_FIXED",
    "UNFIXED_REASON", "REMARK", "OTHER_REASON", "BL_BANGDING_SITE",
)


def complaint_edit_block(row: Mapping[str, Any]) -> str:
    """``editMethodCs``：投诉状态或仲裁状态为待审核才可修改；违禁品不可修改。"""
    if _text(row.get("STATUS")) == "待审核" or _text(row.get("ARBITRATE_STATUS")) == "待审核":
        return "投诉类型为【违禁品】,不可修改" if _text(row.get("CATEGORY")) == "违禁品" else ""
    if _text(row.get("APPEAL_TYPE")) == "仲裁处理":
        return f"仲裁状态为【{_text(row.get('ARBITRATE_STATUS'))}】,不可修改"
    return f"投诉状态为【{_text(row.get('STATUS'))}】,不可修改"


def complaint_form_spec(row: Mapping[str, Any] | None, user_info: Mapping[str, Any],
                        accounts: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
    """“新增”（row 为空，挂在分类上方并先选业务账号）与“修改”（挂在本人投诉上）的同一张表单。"""
    editing = row is not None
    values = {key: _text((row or {}).get(key)) for key in COMPLAINT_USER_FIELDS + ("EXCEPTION_ID", "PRODUCT_TYPE", "SEND_DATE",
                                                                                  "REGISTER_DATE", "SIGN_DATE", "DEPT_NAME",
                                                                                  "EXCEPTIONSITE_SIDE", "EXCEPTION_DEPT_SIDE",
                                                                                  "EXCEPTION_MAN_SIDE", "EXCEPTION_SITE",
                                                                                  "EXCEPTION_TYPE_DATA_FROM")}
    if not editing:
        values.update({"OBJECT_TYPE": "1", "BL_AGENT": "0", "EXCEPTION_SITE_CODE": _text(user_info.get("loginSiteCode")),
                       "EXCEPTION_SITE": _text(user_info.get("loginSiteName"))})
    spec = _spec("complaint_filed", "update" if editing else "create", "修改" if editing else "新增",
                 "修改数据" if editing else "新增数据", [], blocked=complaint_edit_block(row) if editing else "")
    spec.update({"form": "complaint", "values": values, "unfixed_reasons": list(COMPLAINT_UNFIXED_REASONS),
                 "damage_categories": list(COMPLAINT_DAMAGE_CATEGORIES), "collar_type": COMPLAINT_COLLAR_TYPE})
    if accounts:
        spec["account_options"] = [list(item) for item in accounts]
    return spec


def accused_site_types(accused: Any) -> str:
    """``changeEXCEPTION_TYPE``：被投诉方可选的网点类型（网点→一级、二级网点；分拨→分拨中心；总部）。"""
    text, parts = _text(accused), []
    if "网点" in text:
        parts.append("一级网点,二级网点")
    if "分拨" in text:
        parts.append("分拨中心")
    if "总部" in text:
        parts.append("总部")
    return ",".join(parts)


def _decimal_in(value: str, low: str, high: str, integer: bool = False) -> bool:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return False
    if integer and amount != amount.to_integral_value():
        return False
    return Decimal(low) <= amount <= Decimal(high)


def complaint_required_missing(values: Mapping[str, Any], type_meta: Mapping[str, Any] | None) -> bool:
    """表单校验（form.validate）：固定必填 + 按处理类型、投诉类型、对象、代登记、破损修复切换的必填与数值范围。"""
    meta = type_meta or {}
    get = lambda key: _text(values.get(key))  # noqa: E731
    required = ["BILL_CODE", "EXCEPTION_MAN_PHONE", "APPEAL_TYPE", "CATEGORY", "EXCEPTION_TYPE"]
    if get("APPEAL_TYPE") == "仲裁处理":
        required += ["EXCEPTION_VOLUME", "LOSS_NUMBER"]
    if _text(meta.get("BL_ARBITRATION")) not in ("", "0"):
        required += ["LOSS_GOODS_NAME", "COMPLAINANT_NAME", "GOODS_PRICE"]
    if _text(meta.get("IS_COMPLAINT_CONTENT")) == "1":
        required.append("REMARK")
    if get("EXCEPTION_TYPE") == COMPLAINT_COLLAR_TYPE:
        required.append("COLLAR_NUMBER_LIST")
    if accused_site_types(meta.get("ACCUSED_SITE_TYPE")) and get("OBJECT_TYPE") in ("", "1"):
        required.append("EXCEPTIONSITE_SIDE_CODE")
    required += {"2": ["EXCEPTION_DEPT_SIDE_CODE"], "3": ["EXCEPTION_MAN_SIDE_CODE"]}.get(get("OBJECT_TYPE"), [])
    if get("BL_AGENT") == "1":
        required.append("EXCEPTION_SITE_CODE")
    if get("CATEGORY") in COMPLAINT_DAMAGE_CATEGORIES:
        required.append("IS_FIXED")
        if get("IS_FIXED") not in ("", "1"):
            required.append("UNFIXED_REASON")
            if get("UNFIXED_REASON") == "其它":
                required.append("OTHER_REASON")
    if any(not get(key) for key in required):
        return True
    ranges = (("EXCEPTION_VOLUME", "0.01", "99999999", False), ("LOSS_NUMBER", "1", "2000", True),
              ("GOODS_PRICE", "0.01", "2000000", False))
    return any(get(key) and not _decimal_in(get(key), low, high, integer) for key, low, high, integer in ranges)


def validate_complaint_form(values: Mapping[str, Any], *, type_meta: Mapping[str, Any] | None, type_row: Mapping[str, Any] | None,
                            signed_type_row: Mapping[str, Any] | None, bill: Mapping[str, Any] | None, from_order: bool,
                            loss_bill: Mapping[str, Any] | None, subjects: Sequence[Mapping[str, Any]],
                            files: Mapping[str, Sequence[Any]], duplicate: bool, side_in_trajectory: bool,
                            self_in_trajectory: bool, login_site_code: str, now: dt.datetime,
                            car_work_orders: bool | None, collar_bill: Mapping[str, Any] | None) -> None:
    """``saveExceptionData`` 的检查，按原页顺序；任一不过即抛出原页提示。"""
    if complaint_required_missing(values, type_meta):
        raise ActionRuleError("请把信息填写完整")
    get = lambda key: _text(values.get(key))  # noqa: E731
    if loss_bill is None:
        raise ActionRuleError("未获取到相关运单信息!")
    if get("LOSS_NUMBER") and int(Decimal(get("LOSS_NUMBER"))) > int(Decimal(_text(loss_bill.get("PIECE_NUMBER")) or "0")):
        raise ActionRuleError("损失数量必须小于等于运单子单数!")
    if any(_text(subject.get("IS_REQUIRED_TEXT")) == "是" and not files.get(_text(subject.get("FILE_CODE"))) for subject in subjects):
        raise ActionRuleError("请完善附件后进行保存！")
    row = type_row or {}
    if duplicate:
        raise ActionRuleError("同一单号同一类型同一网点仅支持上报一次" if _text(row.get("REPEAT_SITE_COMPLAINT")) == "1"
                              else "同一单号同一类型仅支持上报一次")
    if _text(row.get("BL_RELATED_SITE")) == "1" and not side_in_trajectory:
        raise ActionRuleError(f"被投诉投诉网点与【{get('BILL_CODE')}】无关联关系，无法提交数据")
    if _text(row.get("BL_RELATED_SITE1")) == "1" and not self_in_trajectory and login_site_code != "88888":
        raise ActionRuleError(f"投诉网点与【{get('BILL_CODE')}】无关联关系，无法提交数据")
    if _text(row.get("BL_AGREEMENT_TOUSU")) in ("", "0") and get("EXCEPTION_SITE_CODE") and get("EXCEPTIONSITE_SIDE_CODE") \
            and get("EXCEPTION_SITE_CODE") == get("EXCEPTIONSITE_SIDE_CODE"):
        raise ActionRuleError("投诉方和被投诉方不应该是同一个网点")
    sign_date = _text((bill or {}).get("SIGN_DATE"))
    if sign_date and _text(row.get("AGING")):
        signed = dt.datetime.strptime(sign_date[:19], DATE_FORMAT) if len(sign_date) >= 19 else dt.datetime.strptime(sign_date[:10], "%Y-%m-%d")
        if abs((now - signed).total_seconds()) / 3600 > float(_text(row.get("AGING"))):
            raise ActionRuleError("超出投诉时效，该类型无法投诉")
    signed_ok = signed_type_row is not None and (_text(signed_type_row.get("BL_SIGN")) == "1"
                                                 or (not from_order and _text((bill or {}).get("BL_SIGNS_MARKING")) == "1"))
    if not signed_ok:
        raise ActionRuleError("该运单未签收，此类型不允许未签收投诉")
    if not _text((bill or {}).get("SEND_SITE_CODE") or (bill or {}).get("PREPARE_SITE_CODE")):
        raise ActionRuleError(f"运单号【{get('BILL_CODE')}】不存在!")
    if get("OBJECT_TYPE") == "1" and not get("EXCEPTIONSITE_SIDE_CODE"):
        raise ActionRuleError("被投诉方编号不可为空！")
    if get("EXCEPTION_TYPE") == COMPLAINT_CAR_TYPE and car_work_orders is False:
        raise ActionRuleError(COMPLAINT_CAR_MESSAGE)
    if get("EXCEPTION_TYPE") == COMPLAINT_COLLAR_TYPE:
        if collar_bill is None:
            raise ActionRuleError("未获取到相关运单信息!")
        if _text(collar_bill.get("REGISTER_SITE_CODE")) != get("EXCEPTIONSITE_SIDE_CODE"):
            raise ActionRuleError("被投诉网点并不是运单录入网点!")
        if _text(collar_bill.get("BL_SIGNS_MARKING")) != "1":
            raise ActionRuleError("该类型只能投诉已签收运单!")


def complaint_bill_fields(bill: Mapping[str, Any] | None, from_order: bool) -> dict[str, str]:
    """``getBillInfo``：按运单（或代取件订单）带出的隐藏字段。"""
    bill = bill or {}
    if from_order:
        return {"EXCEPTION_FINANCE_CENTER": "", "EXCEPTION_FINANCE_CENTER_CODE": "", "SEND_SITE": _text(bill.get("PREPARE_SITE")),
                "SEND_SITE_CODE": _text(bill.get("PREPARE_SITE_CODE")), "SEND_DATE": "", "DISPATCH_SITE": _text(bill.get("DESTINATION")),
                "DISPATCH_SITE_CODE": _text(bill.get("DESTINATION_CODE")), "SEND_COMPANY": "", "RECEIVER_COMPANY": "",
                "DISPATCH_FINANCE_CENTER": "", "DISPATCH_FINANCE_CENTER_CODE": "", "SEND_ADDRESS": "", "RECEIVER_ADDRESS": "",
                "GOODS_NAME": _text(bill.get("ITEM_NAME")), "GOODS_VALUES": _text(bill.get("INSURANCE_VALUE")), "REGISTER_DATE": "",
                "SIGN_DATE": ""}
    return {"EXCEPTION_FINANCE_CENTER": _text(bill.get("SEND_FINANCE_CENTER")),
            "EXCEPTION_FINANCE_CENTER_CODE": _text(bill.get("SEND_FINANCE_CENTER_CODE")), "SEND_SITE": _text(bill.get("SEND_SITE")),
            "SEND_SITE_CODE": _text(bill.get("SEND_SITE_CODE")), "SEND_DATE": _text(bill.get("SEND_DATE")),
            "DISPATCH_SITE": _text(bill.get("DISPATCH_SITE")), "DISPATCH_SITE_CODE": _text(bill.get("DISPATCH_SITE_CODE")),
            "SEND_COMPANY": _text(bill.get("SEND_MAN_COMPANY")), "RECEIVER_COMPANY": _text(bill.get("ACCEPT_MAN_COMPANY")),
            "DISPATCH_FINANCE_CENTER": _text(bill.get("DISPATCH_FINANCE_CENTER")),
            "DISPATCH_FINANCE_CENTER_CODE": _text(bill.get("DISPATCH_FINANCE_CENTER_CODE")),
            "SEND_ADDRESS": _text(bill.get("SEND_MAN_ADDRESS")), "RECEIVER_ADDRESS": _text(bill.get("ACCEPT_MAN_ADDRESS")),
            "GOODS_NAME": _text(bill.get("GOODS_NAME")), "GOODS_VALUES": _text(bill.get("REAL_VALUE")),
            "REGISTER_DATE": _text(bill.get("REGISTER_DATE")), "SIGN_DATE": _text(bill.get("SIGN_DATE"))}


def complaint_save_row(operation: str, values: Mapping[str, Any], base_row: Mapping[str, Any], user_info: Mapping[str, Any],
                       server_now: str) -> dict[str, Any]:
    """表单数据 + 原页 ``saveExceptionData`` 补的字段（新增：登记信息、状态、来源 K13；修改：修改信息）。"""
    data = {key: _text(base_row.get(key)) for key in COMPLAINT_FORM_FIELDS}
    data.update({key: _text(values.get(key)) for key in COMPLAINT_FORM_FIELDS if key in values})
    if not data["EXCEPTION_SITE_CODE"]:
        data.update({"EXCEPTION_SITE": _text(user_info.get("loginSiteName")), "EXCEPTION_SITE_CODE": _text(user_info.get("loginSiteCode")),
                     "EXCEPTION_FINANCE_CENTER": _text(user_info.get("loginOwnerFinanceName")),
                     "EXCEPTION_FINANCE_CENTER_CODE": _text(user_info.get("loginOwnerFinanceCode"))})
    name, user_id = _text(user_info.get("loginUserName")), _text(user_info.get("loginUserId"))
    if operation == "create":
        data.update({"REGISTER_DATE": server_now, "REGISTER_SITE": _text(user_info.get("loginSiteName")),
                     "REGISTER_SITE_CODE": _text(user_info.get("loginSiteCode")), "REGISTER_MAN": name, "REGISTER_MAN_CODE": user_id,
                     "SUPERIOR_FINANCE_CENTER": _text(user_info.get("loginOwnerFinanceName")),
                     "SUPERIOR_FINANCE_CENTER_CODE": _text(user_info.get("loginOwnerFinanceCode")),
                     "APPEAL_STATUS": "未申诉", "EXCEPTION_TYPE_DATA_FROM": "K13"})
        data["ARBITRATE_STATUS" if data["APPEAL_TYPE"] == "仲裁处理" else "STATUS"] = "待审核"
        if data["BL_AGENT"] == "1":
            data.update({"AGENT_REGISTER_MAN": name, "AGENT_REGISTER_MAN_CODE": user_id})
    else:
        data.update({"MODIFY_DATE": server_now, "MODIFY_SITE": _text(user_info.get("loginSiteName")),
                     "MODIFY_SITE_CODE": _text(user_info.get("loginSiteCode")), "MODIFIER": name, "MODIFIER_CODE": user_id})
    return {key: value for key, value in data.items() if value is not None}


def complaint_attachment_rows(files: Mapping[str, Sequence[Mapping[str, Any]]], exception_id: str, user_info: Mapping[str, Any],
                              now: dt.datetime) -> list[dict[str, Any]]:
    rows = []
    for file_code, items in files.items():
        for row in ronghui_attachment_rows(items, user_info, now, COMPLAINT_ATTACHMENT_TYPE, FILE_CODE=file_code, EXCEPTION_ID=exception_id):
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------
# 韵达快运仲裁“继续申报”（仲裁查询详情页，updateAndSave('/save')）
# ---------------------------------------------------------------------------------------------

ARBITRATION_REDECLARE_STATE = "退单,可继续申报"
ARBITRATION_GIS_CATEGORY = "2019042911092400492"  # 转退件费：换到这个类别时先做地址解析检查
ARBITRATION_FEE_APPLY = "202203111359159596452"     # 费用申请仲裁
ARBITRATION_AUTO_CATEGORIES = ("2024011210033821415925", "2024031214334922121462", "2024042416251022970162",
                               "2024042416264022970249")  # 自动仲裁类别：不是当前类别时不可选
# (分组, 名称, 类别配置字段, 本次最多新增, 提交字段)
ARBITRATION_PICTURE_GROUPS = (("marina", "主面单", "isBillsUpload", 1, "marinaMult"),
                              ("prove", "价值证明", "isClaimLetterPicture", 6, "proveMult"),
                              ("state", "索赔函", "isStatement", 4, "stateMult"),
                              ("proof", "举证图片", "isProofPicture", 8, "proofMult"))
ARBITRATION_EDIT_FIELDS = ("declType", "declCategory", "claimAmount", "dutyDept", "dutyDeptCode", "abnormalNum", "problemDescribe")
ARBITRATION_PROBLEM_MAX = 1000
_CLAIM_AMOUNT = re.compile(r"^(([1-9][0-9]*)|(([0]\.\d{1,2}|[1-9][0-9]*\.\d{1,2})))$")


def arbitration_redeclare_block(row: Mapping[str, Any], state: str) -> str:
    """``goOnDeclare``：只有“退单,可继续申报”可继续申报；费用申请仲裁不可。"""
    if state == "已申诉,待处理":
        return "申诉有效，不予处理！如有异议，请联系总部客服！热线：22136666!"
    if state != ARBITRATION_REDECLARE_STATE:
        return "该单不是‘退单,可继续申报’状态，不允许继续申报！"
    if _text(row.get("declCategoryName") or row.get("declCategory")) == "费用申请仲裁":
        return "该单是 费用申请仲裁，此类别不允许继续申报"
    return ""


def arbitration_redeclare_spec(row: Mapping[str, Any], state: str) -> dict[str, Any]:
    spec = _spec("arbitration", "redeclare", "继续申报", "继续申报", [], blocked=arbitration_redeclare_block(row, state))
    spec.update({"form": "arbitration", "gis_category": ARBITRATION_GIS_CATEGORY, "fee_apply": ARBITRATION_FEE_APPLY,
                 "auto_categories": list(ARBITRATION_AUTO_CATEGORIES),
                 "picture_groups": [{"key": key, "label": label, "limit": limit} for key, label, _flag, limit, _field in ARBITRATION_PICTURE_GROUPS]})
    return spec


def arbitration_visible_groups(pictures: Mapping[str, Sequence[Any]], modules: Mapping[str, Any] | None) -> dict[str, bool]:
    """详情里有图片的分组显示；换了仲裁类别时按类别配置 Y 显示、N 隐藏，其他值不变（showModule）。"""
    visible = {key: bool(pictures.get(key)) for key, *_rest in ARBITRATION_PICTURE_GROUPS}
    for key, _label, flag, _limit, _field in ARBITRATION_PICTURE_GROUPS:
        value = _text((modules or {}).get(flag))
        if value == "Y":
            visible[key] = True
        elif value == "N":
            visible[key] = False
    return visible


def validate_arbitration_redeclare(detail: Mapping[str, Any], values: Mapping[str, Any], pictures: Mapping[str, Sequence[Mapping[str, Any]]],
                                   removed: set[str], new_files: Mapping[str, Sequence[Any]], visible: Mapping[str, bool],
                                   videos: Sequence[Mapping[str, Any]], new_videos: Sequence[Any], audios: Sequence[Mapping[str, Any]],
                                   new_audios: Sequence[Any]) -> None:
    """``validatePic`` + ``validateGoOnDeclareParam`` + 必传分组与视频、录音条数，按原页顺序。"""
    get = lambda key: _text(values.get(key))  # noqa: E731
    if len(get("problemDescribe")) > ARBITRATION_PROBLEM_MAX:
        raise ActionRuleError("问题描述限制长度1000字！")
    kept = lambda items: [item for item in items if f"old{_text(item.get('fileId'))}" not in removed]  # noqa: E731
    for key, label, _flag, limit, _field in ARBITRATION_PICTURE_GROUPS:
        items = kept(pictures.get(key) or [])
        centre = sum(1 for item in items if _text(item.get("isCentreUpload")) == "Y")
        if len(items) + len(new_files.get(key) or []) - centre > limit:
            raise ActionRuleError(f"{label}只能上传{limit}张图片！")
    if not get("declType"):
        raise ActionRuleError("请填写仲裁类型!")
    if not get("declCategory"):
        raise ActionRuleError("请填写仲裁类别!")
    if not get("claimAmount"):
        raise ActionRuleError("请填写索赔金额!")
    if not get("dutyDept") or not get("dutyDeptCode"):
        raise ActionRuleError("请填写正确责任网点！")
    if not _CLAIM_AMOUNT.match(get("claimAmount")) or Decimal(get("claimAmount")) > Decimal("9999999999.99"):
        raise ActionRuleError("索赔金额只能输入数字且必须大于等于0")
    try:
        if get("abnormalNum") and int(Decimal(get("abnormalNum"))) > int(Decimal(_text(detail.get("number")) or "0")):
            raise ActionRuleError("异常件数必须小于等于子单数！")
    except InvalidOperation:
        raise ActionRuleError("异常件数必须是数字") from None
    if not get("problemDescribe"):
        raise ActionRuleError("问题描述不能为空！")
    names = {"marina": "主面单附件信息必填！", "prove": "价值证明附件信息必填！", "state": "索赔函附件信息必填！", "proof": "举证图片附件信息必填！"}
    for key, *_rest in ARBITRATION_PICTURE_GROUPS:
        if visible.get(key) and not (kept(pictures.get(key) or []) or new_files.get(key)):
            raise ActionRuleError(names[key])
    if videos:  # 详情里有视频时视频区显示，必须有 1–3 段
        total = len(kept(videos)) + len(new_videos)
        if total < 1:
            raise ActionRuleError("上传视频信息必填！")
        if total > 3:
            raise ActionRuleError("最多上传三段视频！")
    if len(kept(audios)) + len(new_audios) > 2:
        raise ActionRuleError("最多上传2段音频！")


def arbitration_hidden_ids(pictures: Mapping[str, Sequence[Mapping[str, Any]]], visible: Mapping[str, bool]) -> list[str]:
    """``getHiddenPic``：被隐藏分组里的原有图片都记入 marinaUpdate（old + fileId）。"""
    return [f"old{_text(item.get('fileId'))}" for key, *_rest in ARBITRATION_PICTURE_GROUPS if not visible.get(key)
            for item in pictures.get(key) or []]


SITE_KEY = re.compile(r"^[\w一-龥()（）\-]{1,30}$")


def site_search_key(value: Any) -> str:
    key = _text(value)
    if not SITE_KEY.match(key):
        raise ActionRuleError("请输入网点名称或编号。")
    return key


__all__ = [
    "ARBITRATION_EDIT_FIELDS", "ARBITRATION_GIS_CATEGORY", "ARBITRATION_PICTURE_GROUPS", "arbitration_hidden_ids",
    "arbitration_redeclare_block", "arbitration_redeclare_spec", "arbitration_visible_groups", "validate_arbitration_redeclare",
    "COMPLAINT_FORM_FIELDS", "COMPLAINT_FORM_SUBPAGE", "COMPLAINT_USER_FIELDS", "accused_site_types", "complaint_attachment_rows",
    "complaint_bill_fields", "complaint_edit_block", "complaint_form_spec", "complaint_required_missing", "complaint_save_row",
    "validate_complaint_form",
    "appeal_create_spec",
    "transfer_delay_block", "transfer_delay_days", "transfer_reply_block", "validate_transfer_delay", "validate_transfer_reply",
    "CSSP_PERMISSIONS", "CSSP_RECEIVE", "cssp_appoint_spec", "cssp_permissions", "cssp_receive_specs", "cui_reply_specs",
    "validate_receive_num",
    "APPEAL_FORM_SUBPAGE", "appeal_form_exception_id", "appeal_form_spec", "appeal_form_tables", "appeal_side", "validate_appeal_form",
    "arbitration_specs", "validate_arbitration_message",
    "CSSP_CUI_REPLY", "CSSP_OPERATIONS", "TRANSFER_ABNORMAL_TYPES", "cssp_block", "cssp_specs", "cui_reply_spec",
    "transfer_abnormal_block", "transfer_accept_confirm", "transfer_specs", "validate_cssp", "validate_cui_reply",
    "validate_transfer_abnormal",
    "APPEAL_SLOTS", "CLAIM_SLOTS", "appeal_delete_block", "appeal_specs", "complaint_against_specs", "complaint_appeal_block",
    "complaint_appeal_update", "complaint_cancel_block", "complaint_cancel_check", "complaint_claim_block", "complaint_claim_update",
    "complaint_filed_specs", "complaint_reply_guid", "complaint_reply_tables", "exception_path_row", "pickup_revoke_block",
    "pickup_revoke_row", "pickup_specs", "pickup_status", "validate_complaint_appeal", "validate_complaint_claim",
    "validate_complaint_reply",
    "ActionRuleError", "DATE_FORMAT", "WORK_OPERATIONS", "WORK_SUBPAGE_APPEAL", "WORK_SUBPAGE_FOLLOW", "contact_text",
    "problem_forward_row", "problem_forward_spec", "problem_needs_seen", "problem_seen_row", "site_search_key",
    "validate_problem_forward", "validate_work_order", "work_order_block", "work_order_detail_guid", "work_order_saved",
    "work_order_specs", "work_order_tables",
]
