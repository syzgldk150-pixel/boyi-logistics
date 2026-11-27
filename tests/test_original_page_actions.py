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


def test_yunda_work_order_buttons_follow_the_detail_page():
    from shared.original_page_actions import cssp_specs, validate_cssp, validate_cui_reply

    detail = {"complaintId": "C1", "compStatus": 10, "compStatusName": "已接单，待响应", "comper": "合成投诉人", "comperPhone": "13800000000",
              "sender": "合成寄件人", "senderPhone": "13900000000"}
    specs = {spec["operation"]: spec for spec in cssp_specs(detail)}
    assert [spec["label"] for spec in specs.values()] == ["立即响应", "首次回复", "回复处理", "转至理赔", "结单", "延长时效", "转站点"]
    assert specs["prompt_resp"]["blocked"] == "" and specs["first_reply"]["blocked"].startswith("当前处理状态")
    assert specs["turn_site"]["blocked"] == "" and specs["statement"]["blocked"] != ""
    respond = specs["prompt_resp"]["fields"]
    assert respond[0]["type"] == "switch" and respond[1]["value"] == "合成投诉人" and respond[-1]["value"] == "已联系投诉人响应工单诉求。"
    reply = specs["reply_handling"]["fields"][0]
    assert reply["type"] == "radio" and reply["autofill"]["values"]["发件人"] == ["合成寄件人", "13900000000"]
    now = dt.datetime(2026, 11, 27, 10, 0)
    good = {"putboundIdentity": "投诉人", "customerName": "合成投诉人", "callNumber": "13800000000", "dealContent": "已联系"}
    assert validate_cssp("prompt_resp", detail, good, now)["params"]["callNumber"] == "13800000000"
    with pytest.raises(ActionRuleError, match="外呼电话"):
        validate_cssp("prompt_resp", detail, {**good, "callNumber": "138-0"}, now)
    with pytest.raises(ActionRuleError, match="8:00至下午18:00"):
        validate_cssp("turn_site", detail, {"dealContent": "转"}, dt.datetime(2026, 11, 27, 19, 0))
    first = {**detail, "compStatus": 2}
    with pytest.raises(ActionRuleError, match="预计解决时间"):
        validate_cssp("first_reply", first, good, now)
    assert validate_cssp("first_reply", first, {**good, "estimatedCompletionTime": "2026-11-28 10:00"}, now)["params"]["estimatedCompletionTime"]
    with pytest.raises(ActionRuleError, match="沟通记录"):
        validate_cui_reply({"replayCustomerName": "甲", "replayCustomerPhone": "1"})


def test_yunda_transfer_rules():
    from shared.original_page_actions import transfer_abnormal_block, transfer_accept_confirm, transfer_specs, validate_transfer_abnormal

    row = {"Replace_Id": "R1", "Replace_Status_Code": "0", "Order_Source": "690", "Ext_Field4": "镇上自提", "Abnormal_Type": ""}
    assert "可能有加收" in transfer_accept_confirm(row) and transfer_accept_confirm({**row, "Order_Source": "1"}) == "是否确认接单？"
    assert [spec["label"] for spec in transfer_specs(row)] == ["接单", "转单", "异常登记"]
    assert transfer_abnormal_block({**row, "Abnormal_Type": "超区"}) == "已做过异常登记无需再次登记"
    assert transfer_abnormal_block({**row, "Abnormal_Type": "客户货未备齐或无货", "Delay_Day": "2"}) == ""
    assert transfer_abnormal_block({**row, "Replace_Status_Code": "2"}) == "只有待接单、已接单支持异常登记"
    with pytest.raises(ActionRuleError, match="请选择异常类型"):
        validate_transfer_abnormal(row, {})
    with pytest.raises(ActionRuleError, match="最多50字"):
        validate_transfer_abnormal(row, {"Abnormal_Type": "2", "Abnormal_Remark": "x" * 51})
    staged = {"platform": "yunda", "file_name": "a.jpg", "old_name": "a.jpg", "new_name": "0" * 32, "file_path": "staged:" + "0" * 32}
    fields = validate_transfer_abnormal(row, {"Abnormal_Type": "2", "abnormal_image2": [staged]})
    assert fields["label"] == "客户联系不上" and fields["images"] == [(2, staged)]
    with pytest.raises(ActionRuleError, match="用途不符"):
        validate_transfer_abnormal(row, {"Abnormal_Type": "2", "abnormal_image1": [{**staged, "file_path": "/up/x.jpg"}]})


def test_yunda_arbitration_message_rules():
    from shared.original_page_actions import arbitration_specs, validate_arbitration_message

    assert [spec["label"] for spec in arbitration_specs({})] == ["留言"]
    with pytest.raises(ActionRuleError, match="1~1000"):
        validate_arbitration_message({"message": ""})
    with pytest.raises(ActionRuleError, match="1~1000"):
        validate_arbitration_message({"message": "x" * 1001})
    assert validate_arbitration_message({"message": " 请尽快审核 "}) == "请尽快审核"


def test_ronghui_new_appeal_form_checks_in_page_order():
    from shared.original_page_actions import (
        appeal_form_exception_id, appeal_form_spec, appeal_form_tables, appeal_side, complaint_against_specs, validate_appeal_form,
    )

    row = {"EXCEPTION_ID": "E1", "BILL_CODE": "B1", "APPEAL_TYPE": "差错处理", "EXCEPTION_VOLUME": "50"}
    assert [spec["label"] for spec in complaint_against_specs(row, USER)] == ["回复", "新增申诉"]
    spec = appeal_form_spec(row)
    assert [field["label"] for field in spec["fields"]] == ["运单号", "处理类型", "申诉类型", "联系方式", "索赔金额", "申诉内容", "实际重量"]
    assert spec["fields"][2]["lookup"]["depends_on"] == "PROCESS_TYPE" and len(spec["attachment_slots"]) == 10
    assert appeal_side("一级网点") == "网点" and appeal_side("分拨中心") == "中心" and appeal_side("总部") == ""
    base = {"bill": {"PRODUCT_TYPE": "普通"}, "appeal_type": {"APPEAL_TYPE": "延误", "BL_EVIDENCE": "0"}, "appeal_term_days": 7,
            "duplicate": False, "stakeholder": False, "trajectory_sites": [], "login_site_code": "S1"}
    payload = {"PROCESS_TYPE": "差错处理", "APPEAL_TYPE": "延误", "PHONE": "13800000000", "CLAIM_AMOUNT": "50"}
    with pytest.raises(ActionRuleError, match="未获取到相关运单信息"):
        validate_appeal_form(row, payload, **{**base, "bill": None})
    with pytest.raises(ActionRuleError, match="回单禁止申诉"):
        validate_appeal_form(row, payload, **{**base, "bill": {"IS_RETURN_BILL": 1}})
    with pytest.raises(ActionRuleError, match="请将数据填写完整"):
        validate_appeal_form(row, {**payload, "PHONE": ""}, **base)
    with pytest.raises(ActionRuleError, match="请将数据填写完整"):
        validate_appeal_form(row, payload, **{**base, "appeal_type": {"IS_COMPLAINT_CONTENT": "1"}})
    with pytest.raises(ActionRuleError, match="只能登记一次"):
        validate_appeal_form(row, payload, **{**base, "duplicate": True})
    assert validate_appeal_form(row, {**payload, "APPEAL_TYPE": "云呼未接申述"}, **{**base, "duplicate": True})
    with pytest.raises(ActionRuleError, match="请上传附件"):
        validate_appeal_form(row, payload, **{**base, "appeal_type": {"BL_EVIDENCE": "1"}})
    with pytest.raises(ActionRuleError, match="无关联关系"):
        validate_appeal_form(row, payload, **{**base, "stakeholder": True, "trajectory_sites": ["X"]})
    file = {"platform": "ronghui", "file_name": "a.jpg", "file_path": "/unauth/download/a"}
    fields = validate_appeal_form(row, {**payload, "appeal_file2": [file]}, **{**base, "appeal_type": {"BL_EVIDENCE": "1"}})
    new_id = appeal_form_exception_id(dt.datetime(2026, 11, 27, 9, 0, 0), rand=12345)
    assert new_id == "2026112709000012345"
    tables = appeal_form_tables(fields, {**USER, "loginUserName": "合成用户"}, "B1", new_id, "T")
    saved = tables[0][1][0]
    assert saved["EXCEPTION_ID"] == new_id and saved["COMPLAINT_STATUS"] == "待审核" and saved["FILE_PATH2"] == "/unauth/download/a"
    assert saved["APPEAL_SITE_CODE"] == "S1" and saved["CREATE_MAN"] == "合成用户" and saved["PRODUCT_TYPE"] == "普通"
    assert tables[1][0] == "TAB_EXCEPTION_PATH_ADD" and tables[1][1][0]["FILE_UPLOAD_TYPE"] == "申诉附件"
