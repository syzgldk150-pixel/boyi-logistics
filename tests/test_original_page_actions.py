"""Processing actions copied from the original pages (shared/original_page_actions.py, 2026-11-27)."""
import datetime as dt

import pytest

from shared.original_page_actions import (
    ActionRuleError, contact_text, problem_forward_row, problem_needs_seen, validate_problem_forward,
    validate_work_order, work_order_block, work_order_detail_guid, work_order_specs, work_order_tables,
)

ROW = {"WORK_ORDER_CODE": "W1", "BILL_CODE": "B1", "WORK_TYPE": "咨询单", "WORK_NAME": "催派", "WORK_CONTENT": "请尽快派送",
       "WORK_STATE": "待责任方响应", "URGE_COUNT": 2, "APPEAL_STATUS": None}
USER = {"loginSiteName": "邵阳大祥站", "loginSiteCode": "S1", "loginEmpName": "合成员工", "loginEmpCode": "E1"}


def test_buttons_carry_the_list_pages_own_refusals():
    specs = {spec["operation"]: spec for spec in work_order_specs(ROW, USER)}
    assert [spec["label"] for spec in specs.values()] == ["响应", "完结", "催工单", "申诉"]
    assert specs["respond"]["blocked"] == "" and specs["finish"]["blocked"] == "工单状态暂不支持完结"
    assert specs["appeal"]["blocked"] == "只有待申诉状态才能操作申诉！"
    assert specs["respond"]["fields"][-1] == {"name": "HANDLE_CONTENT", "label": "处理内容", "type": "textarea", "value": "",
                                               "required": True, "max": 500}
    assert work_order_block("respond", {**ROW, "WORK_STATE": "已完结"}) == "订单已完结！"
    assert work_order_block("urge", {**ROW, "WORK_STATE": "已完结"}) == "已完结工单无需催促！"
    assert work_order_block("appeal", {**ROW, "APPEAL_STATUS": 1}) == "该工单已申诉，请勿重复申诉！"
    assert work_order_block("appeal", {**ROW, "APPEAL_STATUS": 0}) == "裁定审核时间不可为空！"


@pytest.mark.parametrize("operation,kwargs,payload,message", [
    ("respond", {"response_count": 2}, {"HANDLE_CONTENT": "x"}, "单个工单最多可响应2次!"),
    ("respond", {"same_bill_rows": [{"WORK_STATE": "已完结"}]}, {"HANDLE_CONTENT": "x"}, "工单已完结，无需跟进"),
    ("respond", {}, {"HANDLE_CONTENT": ""}, "处理内容不能为空"),
    ("respond", {}, {"HANDLE_CONTENT": "x" * 501}, "不能超过500"),
])
def test_dialog_checks(operation, kwargs, payload, message):
    with pytest.raises(ActionRuleError, match=message):
        validate_work_order(operation, ROW, payload, **kwargs)


def test_appeal_checks_type_switch_time_limit_attachment_and_amount():
    row = {**ROW, "APPEAL_STATUS": 0, "VERDICT_DATE": "2026-11-26 10:00:00"}
    now = dt.datetime(2026, 11, 27, 9, 0, 0)
    good = {"HANDLE_CONTENT1": "责任不在我方", "APPEAL_CLAIM_AMOUNT": "12.50"}
    with pytest.raises(ActionRuleError, match="未开启申诉"):
        validate_work_order("appeal", row, good, type_rows=[{"IS_APPEAL": 0}], server_now=now)
    with pytest.raises(ActionRuleError, match="超过申诉时效"):
        validate_work_order("appeal", row, good, type_rows=[{"IS_APPEAL": 1, "APPEAL_TIME_LIMIT": 12}], server_now=now)
    with pytest.raises(ActionRuleError, match="必须上传申诉附件"):
        validate_work_order("appeal", row, good, type_rows=[{"IS_APPEAL": 1, "IS_ATTACHMENT_REQ": 1}], server_now=now)
    with pytest.raises(ActionRuleError, match="索赔金额"):
        validate_work_order("appeal", row, {**good, "APPEAL_CLAIM_AMOUNT": "1.234"}, server_now=now)
    fields = validate_work_order("appeal", row, good, type_rows=[{"IS_APPEAL": 1, "APPEAL_TIME_LIMIT": 48}], server_now=now)
    assert fields["claim"] == "12.50" and fields["reason"] == "责任不在我方"
    assert validate_work_order("appeal", row, good)["claim"] == "12.50"  # 原页查不到类别设置时不拦


def test_saved_rows_match_the_dialog():
    now = dt.datetime(2026, 11, 27, 1, 2, 3, tzinfo=dt.timezone.utc)
    guid = work_order_detail_guid(now, "S1", rand=7)
    assert guid == "2026112709020307S1"  # 北京时间 + 随机数（不足 1000 只补一个 0）+ 网点编码
    attachment = {"platform": "ronghui", "file_name": "a.png", "file_ext": ".png", "file_size_kb": 3, "file_path": "/unauth/download/a"}
    tables = work_order_tables("respond", ROW, {"content": "已联系", "attachments": [attachment]}, USER, "2026-11-27 09:02:03", guid, now)
    assert [key for key, _ in tables] == ["TAB_WORK_ORDER_DETAILS_ADD", "TAB_WORK_ORDER_UPT", "TAB_WORK_ORDER_DETAILS_PIC_ADD"]
    detail = tables[0][1][0]
    assert detail["HANDLE_STATE"] == 2 and detail["GUID"] == guid and detail["HANDLE_SITE_CODE"] == "S1" and detail["HANDLE_CONTENT"] == "已联系"
    assert tables[1][1] == [{"WORK_STATE": 3, "RESPONSE_DATE": "2026-11-27 09:02:03", "WORK_ORDER_CODE": "W1"}]
    assert tables[2][1][0]["ORDER_GUID"] == guid and "FILE_UPLOAD_TYPE" not in tables[2][1][0]
    urge = work_order_tables("urge", ROW, {"content": "催", "attachments": []}, USER, "T", guid, now)
    assert urge[1][1] == [{"URGE_COUNT": 3, "WORK_ORDER_CODE": "W1"}] and urge[2][1] == []
    finish = work_order_tables("finish", ROW, {"content": "完", "attachments": []}, USER, "T", guid, now)
    assert finish[0][1][0]["HANDLE_STATE"] == 3 and finish[1][1][0]["WORK_STATE"] == 4 and finish[1][1][0]["OVER_TIME"] == "T"
    appeal = work_order_tables("appeal", ROW, {"reason": "理由", "claim": "5", "attachments": []}, USER, "T", guid, now)
    assert appeal[0][1][0]["HANDLE_STATE"] == 7 and appeal[1][1][0]["APPEAL_STATUS"] == 1 and appeal[1][1][0]["APPEAL_CLAIM_AMOUNT"] == "5"


def test_problem_forward_and_seen():
    row = {"SEND_SITE": "邵阳大祥站", "REVERSION": "", "BL_CHECKOK": "0", "BILL_CODE": "B1", "REGISTER_DATE": "2026-11-27 08:00:00",
           "BL_CHECKOK_STR": "未处理", "BL_SEE_STR": "未查看"}
    assert problem_needs_seen(row) and not problem_needs_seen({**row, "BL_SEE_STR": "已查看"})
    with pytest.raises(ActionRuleError, match="通知网点不能为空"):
        validate_problem_forward(row, "邵阳大祥站", {}, [])
    with pytest.raises(ActionRuleError, match="找不到这个网点"):
        validate_problem_forward(row, "邵阳大祥站", {"site_code": "X"}, [{"SITE_CODE": "Y", "SITE_NAME": "y"}])
    with pytest.raises(ActionRuleError):
        validate_problem_forward(row, "邵阳操作场", {"site_code": "Y"}, [{"SITE_CODE": "Y", "SITE_NAME": "y"}])
    site = validate_problem_forward(row, "邵阳大祥站", {"site_code": "Y"}, [{"SITE_CODE": "Y", "SITE_NAME": "合成网点"}])
    assert problem_forward_row(row, site) == {"BILL_CODE": "B1", "SEND_SITE_CODE": "Y", "SEND_SITE": "合成网点",
                                              "REGISTER_DATE": "2026-11-27 08:00:00"}
    assert contact_text({"SITE_NAME": "甲", "PRINCIPAL": "乙", "PHONE": "1"}) == "登记网点：甲\r\n负责人：乙\r\n联系电话：1"


def test_complaint_rules_follow_the_original_pages():
    from shared.original_page_actions import (
        complaint_appeal_block, complaint_appeal_update, complaint_cancel_block, complaint_cancel_check, complaint_claim_block,
        complaint_filed_specs, complaint_reply_guid, complaint_reply_tables, validate_complaint_appeal, validate_complaint_reply,
    )

    own = {"EXCEPTION_ID": "E1", "BILL_CODE": "B1", "EXCEPTION_MAN_CODE": "E1", "CATEGORY": "延误", "STATUS": "待审核",
           "ARBITRATE_STATUS": None, "APPEAL_TYPE": "差错处理"}
    assert complaint_cancel_block(own, USER) == ""
    assert complaint_cancel_block({**own, "EXCEPTION_MAN_CODE": "X"}, USER) == "仅支持投诉人取消投诉"
    assert complaint_cancel_block({**own, "EXCEPTION_MAN_CODE": "X", "EXCEPTION_TYPE_DATA_FROM": "APP", "EXCEPTION_SITE_CODE": "S1"}, USER) == ""
    assert complaint_cancel_block({**own, "CATEGORY": "违禁品"}, USER) == "投诉类型为【违禁品】,不可修改"
    with pytest.raises(ActionRuleError, match="投诉已审核"):
        complaint_cancel_check({"STATUS": "审核通过"})
    complaint_cancel_check({"STATUS": "待审核", "ARBITRATE_STATUS": ""})
    # 原页条件是“投诉状态和仲裁状态都有值且都不是审核驳回”才拦，仲裁状态为空时放行。
    assert complaint_appeal_block(own) == ""
    assert complaint_appeal_block({**own, "ARBITRATE_STATUS": "待审核"}) == "投诉审核驳回时投诉网点可进行操作申诉！"
    assert complaint_appeal_block({**own, "STATUS": "审核驳回"}) == ""
    assert complaint_appeal_block({**own, "STATUS": "审核驳回", "AUDIT_STATUS": 2}) == "该投诉已提交申诉。"
    assert complaint_claim_block(own) == "仲裁才有理赔的资料补充；普通投诉没有理赔！"
    assert [spec["operation"] for spec in complaint_filed_specs(own, USER)] == ["cancel", "appeal", "claim"]
    rejected = {**own, "STATUS": "审核驳回", "APPEAL_TYPE": "仲裁处理", "ARBITRATE_STATUS": "待理赔"}
    with pytest.raises(ActionRuleError, match="待理赔"):
        validate_complaint_appeal(rejected, {"APPEAL_CONTENT": "x"}, dt.datetime(2026, 11, 27))
    rejected["ARBITRATE_STATUS"] = "审核驳回"
    fields = validate_complaint_appeal(rejected, {"APPEAL_CONTENT": "责任不在我方", "appeal_file1": [
        {"platform": "ronghui", "file_name": "a.jpg", "file_path": "/unauth/download/a"}]}, dt.datetime(2026, 11, 27))
    update = complaint_appeal_update(rejected, fields, {**USER, "loginUserName": "合成用户"}, "T")
    assert update["ARBITRATE_STATUS"] == "待审核" and "STATUS" not in update and update["APPEAL_STATUS"] == "已申诉"
    assert update["APPEAL_MAN"] == "合成用户" and "APPEAL_MAN_CODE" not in update
    with pytest.raises(ActionRuleError, match="回复内容不能为空"):
        validate_complaint_reply({})
    guid = complaint_reply_guid("abc-123")
    assert guid == guid.upper() and not set(guid) & set("+-/=")
    tables = complaint_reply_tables(own, validate_complaint_reply({"REPLY_CONTENT": "已核实", "attachments": [
        {"platform": "ronghui", "file_name": "a.png", "file_ext": ".png", "file_path": "/unauth/download/a"}]}), USER, guid,
        dt.datetime(2026, 11, 27))
    assert tables[0][1][0]["PROCESS_TYPE"] == "投诉回复" and tables[0][1][0]["GUID"] == guid
    assert tables[1][1][0]["PROCESS_RECORD_ID"] == guid and tables[1][1][0]["FILE_UPLOAD_TYPE"] == "被投诉方回复附件"


def test_appeal_delete_and_pickup_revoke_rules():
    from shared.original_page_actions import appeal_delete_block, pickup_revoke_block, pickup_revoke_row

    assert appeal_delete_block({"COMPLAINT_STATUS": "待审核"}) == ""
    assert appeal_delete_block({"COMPLAINT_STATUS": "已审核"}) == "非待审核状态不可删除！"
    assert pickup_revoke_block({"STATUS": "已揽收"}).startswith("订单已揽收录单不支持撤销")
    assert pickup_revoke_block({"STATUS": 3}) == "已取消订单不支持重复撤销!"
    assert pickup_revoke_block({"STATUS": "5"}) == "撤销待审核订单不支持撤销!"
    assert pickup_revoke_block({"STATUS": "待揽收"}) == ""
    assert pickup_revoke_row({"TX_LOGISTIC_ID": "T1"}, USER, "T") == {"TX_LOGISTIC_ID": "T1", "STATUS": 3, "REVOKE_DATE": "T",
                                                                     "REVOKE_MAN": "合成员工", "REVOKE_MAN_CODE": "E1"}
