"""Agent side of original-page processing: re-read, check, save, read back (2026-11-27)."""
import unittest
from unittest.mock import patch

from agent.tms_runtime.scripts import customer_service_problem as base
from agent.tms_runtime.scripts import original_page_processing as processing

USER = {"loginSiteName": "邵阳大祥站", "loginSiteCode": "S1", "loginEmpName": "合成员工", "loginEmpCode": "E1"}
ROW = {"WORK_ORDER_CODE": "W1", "BILL_CODE": "B1", "WORK_TYPE": "咨询单", "WORK_NAME": "催派", "WORK_CONTENT": "x",
       "WORK_STATE": "待责任方响应", "URGE_COUNT": 0}


class WorkOrderTests(unittest.TestCase):
    def run_respond(self, after_state="待责任方处理", found_guid=True, details_count=0):
        saved, state = [], {"saved": False}

        def find(session, context, call_id, data, paged=False):
            if call_id == "FIND_TAB_WORK_ORDER":
                return [{**ROW, "WORK_STATE": after_state}] if state["saved"] else [dict(ROW)]
            if call_id == "FIND_TAB_WORK_ORDER_DETAILS":
                if "HANDLE_STATE" in data:
                    return [{}] * details_count
                return [{"GUID": saved[0][0][1][0]["GUID"] if found_guid else "other"}]
            if call_id == "FIND_NOW":
                return [{"SYSDATE": "2026-11-27 09:00:00"}]
            raise AssertionError(call_id)

        def save(session, context, tables, label):
            self.assertEqual("SUB", context["page_id"])
            saved.append(tables)
            state["saved"] = True
            return {"success": True, "message": "数据保存成功"}

        with patch.object(base, "_read_user_info_cookie", return_value=USER), \
                patch.object(base, "_resolve_ronghui_page_context", return_value={"page_id": "P", "url": "u"}), \
                patch.object(processing, "_subpage_context", return_value={"page_id": "SUB", "url": "s"}), \
                patch.object(processing, "_find", side_effect=find), patch.object(processing, "_save", side_effect=save):
            result = processing.run("ronghui", "process", object(), {
                "kind": "work_order", "operation": "respond",
                "item": {"external_id": "W1", "BILL_CODE": "B1", "WORK_TYPE": "咨询单", "WORK_NAME": "催派"},
                "payload": {"HANDLE_CONTENT": "已联系客户"}})
        return result, saved

    def test_respond_saves_like_the_dialog_and_reads_back(self):
        result, saved = self.run_respond()
        self.assertTrue(result["verified"])
        self.assertEqual("W1", result["external_id"])
        self.assertEqual(2, saved[0][0][1][0]["HANDLE_STATE"])
        self.assertEqual({"WORK_STATE": 3, "RESPONSE_DATE": "2026-11-27 09:00:00", "WORK_ORDER_CODE": "W1"}, saved[0][1][1][0])

    def test_read_back_mismatch_is_unknown(self):
        for kwargs in ({"after_state": "待责任方响应"}, {"found_guid": False}):
            with self.assertRaises(base.CustomerServiceProblemError) as ctx:
                self.run_respond(**kwargs)
            self.assertEqual("WRITE_OUTCOME_UNKNOWN", ctx.exception.code)

    def test_original_refusal_stops_before_saving(self):
        with self.assertRaises(base.CustomerServiceProblemError) as ctx:
            self.run_respond(details_count=2)
        self.assertEqual("REPLY_RULE_REJECTED", ctx.exception.code)
        self.assertIn("最多可响应2次", str(ctx.exception))

    def test_unknown_action_is_refused(self):
        with self.assertRaises(base.CustomerServiceProblemError):
            processing.run("ronghui", "process", object(), {"kind": "work_order", "operation": "delete", "item": {}, "payload": {}})


class ProblemTests(unittest.TestCase):
    PROBLEM = {"GUID": "G1", "BILL_CODE": "B1", "TYPE": "其他", "REGISTER_DATE": "2026-11-27 08:00:00", "SEND_SITE": "邵阳大祥站",
               "REVERSION": "", "BL_CHECKOK": "0", "BL_CHECKOK_STR": "未处理", "BL_SEE_STR": "未查看", "REGISTER_SITE_CODE": "R1"}
    ITEM = {"external_id": "G1", "waybill_no": "B1", "registered_at": "2026-11-27 08:00:00"}

    def test_forward_checks_site_and_reads_back(self):
        saved = []
        with patch.object(base, "_read_user_info_cookie", return_value=USER), \
                patch.object(base, "_resolve_ronghui_page_context", return_value={"page_id": "P", "url": "u"}), \
                patch.object(base, "_ronghui_problem_row", return_value=dict(self.PROBLEM)), \
                patch.object(base, "_ronghui_full_problem_row", return_value={**self.PROBLEM, "SEND_SITE_CODE": "Y"}), \
                patch.object(processing, "_find", return_value=[{"SITE_CODE": "Y", "SITE_NAME": "合成网点"}]), \
                patch.object(processing, "_save", side_effect=lambda s, c, tables, label: saved.append(tables) or {"success": True}):
            result = processing.run("ronghui", "process", object(), {"kind": "problem", "operation": "forward",
                                                                      "item": self.ITEM, "payload": {"site_code": "Y"}})
        self.assertTrue(result["verified"])
        self.assertEqual([("TAB_PROBLEM_UPT", [{"BILL_CODE": "B1", "SEND_SITE_CODE": "Y", "SEND_SITE": "合成网点",
                                                "REGISTER_DATE": "2026-11-27 08:00:00"}])], saved[0])

    def test_mark_seen_only_when_unhandled_and_unseen(self):
        rows = [dict(self.PROBLEM), {**self.PROBLEM, "BL_SEE_STR": "已查看"}]
        saved = []
        with patch.object(base, "_resolve_ronghui_page_context", return_value={"page_id": "P", "url": "u"}), \
                patch.object(base, "_ronghui_problem_row", side_effect=rows), \
                patch.object(processing, "_save", side_effect=lambda s, c, tables, label: saved.append(tables) or {"success": True}):
            result = processing.run("ronghui", "process", object(), {"kind": "problem", "operation": "mark_seen", "item": self.ITEM, "payload": {}})
        self.assertTrue(result["verified"])
        self.assertEqual([("TAB_PROBLEM_UPT", [{"REGISTER_DATE": "2026-11-27 08:00:00", "BILL_CODE": "B1", "BL_SEE": 1}])], saved[0])
        with patch.object(base, "_ronghui_problem_row", return_value={**self.PROBLEM, "BL_SEE_STR": "已查看"}):
            skipped = processing.run("ronghui", "process", object(), {"kind": "problem", "operation": "mark_seen", "item": self.ITEM, "payload": {}})
        self.assertEqual({"skipped": True}, skipped["result"])


if __name__ == "__main__":
    unittest.main()


class ComplaintTests(unittest.TestCase):
    def test_cancel_refuses_when_audited_and_reads_back_removal(self):
        row = {"EXCEPTION_ID": "E1", "EXCEPTION_MAN_CODE": "E1", "CATEGORY": "延误"}
        fresh = [{"STATUS": "待审核"}, []]
        saved = []

        def find(session, context, call_id, data, paged=False):
            if call_id == "FIND_TAB_EXCEPTION_REGISTER":
                value = fresh.pop(0)
                return [value] if isinstance(value, dict) else value
            raise AssertionError(call_id)

        with patch.object(base, "_read_user_info_cookie", return_value=USER), \
                patch.object(base, "_resolve_ronghui_page_context", return_value={"page_id": "P", "url": "u"}), \
                patch.object(processing, "_complaint_row", return_value=row), patch.object(processing, "_find", side_effect=find), \
                patch.object(processing, "_save", side_effect=lambda s, c, tables, label: saved.append(tables) or {"success": True}):
            result = processing.run("ronghui", "process", object(), {"kind": "complaint_filed", "operation": "cancel",
                                                                      "item": {"external_id": "E1"}, "payload": {}})
        self.assertTrue(result["verified"])
        self.assertEqual([("TAB_EXCEPTION_DEL", [row])], saved[0])
        with patch.object(base, "_read_user_info_cookie", return_value=USER), \
                patch.object(base, "_resolve_ronghui_page_context", return_value={"page_id": "P", "url": "u"}), \
                patch.object(processing, "_complaint_row", return_value=row), \
                patch.object(processing, "_find", return_value=[{"STATUS": "审核通过"}]), patch.object(processing, "_save") as save:
            with self.assertRaises(base.CustomerServiceProblemError) as ctx:
                processing.run("ronghui", "process", object(), {"kind": "complaint_filed", "operation": "cancel",
                                                                 "item": {"external_id": "E1"}, "payload": {}})
        self.assertIn("投诉已审核", str(ctx.exception))
        save.assert_not_called()

    def test_pickup_revoke_reads_back_cancelled(self):
        rows = [{"TX_LOGISTIC_ID": "T1", "STATUS": "待揽收"}, {"TX_LOGISTIC_ID": "T1", "STATUS": "已取消"}]
        with patch.object(base, "_read_user_info_cookie", return_value=USER), \
                patch.object(base, "_resolve_ronghui_page_context", return_value={"page_id": "P", "url": "u"}), \
                patch.object(processing, "_pickup_row", side_effect=rows), patch.object(processing, "_server_now", return_value="T"), \
                patch.object(processing, "_save", return_value={"success": True}) as save:
            result = processing.run("ronghui", "process", object(), {"kind": "pickup", "operation": "revoke",
                                                                      "item": {"external_id": "T1"}, "payload": {}})
        self.assertTrue(result["verified"])
        self.assertEqual("TAB_UNITE_ORDERS_UPT", save.call_args[0][2][0][0])


class YundaTests(unittest.TestCase):
    def test_cssp_prompt_response_posts_like_the_page_and_reads_back(self):
        import datetime as dt

        details = [{"complaintId": "C1", "compStatus": 10}, {"complaintId": "C1", "compStatus": 2}]
        calls = []

        def cssp(session, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if "compInfo" in path:
                return details.pop(0)
            if "handlingDetail" in path:
                return []
            return {"code": 0, "msg": "操作成功"}

        with patch.object(processing, "_cssp", side_effect=cssp), \
                patch.object(processing.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = dt.datetime(2026, 11, 27, 10, 0, tzinfo=base.SHANGHAI_TZ)
            result = processing.run("yunda", "process", object(), {"kind": "cssp", "operation": "prompt_resp", "item": {"external_id": "C1"},
                                                                    "payload": {"putboundIdentity": "投诉人", "customerName": "甲", "callNumber": "13800000000",
                                                                                "dealContent": "已联系投诉人响应工单诉求。"}})
        self.assertTrue(result["verified"])
        post = [call for call in calls if call[0] == "post"][0]
        self.assertEqual("/cssp/comp/sysCompDeal/promptResp.do", post[1])
        self.assertEqual("C1", post[2]["params"]["complaintId"])

    def test_cssp_unchanged_after_submit_is_unknown(self):
        def cssp(session, method, path, **kwargs):
            if "compInfo" in path:
                return {"complaintId": "C1", "compStatus": 3}
            if "handlingDetail" in path:
                return {"rows": [{}]}
            return {"code": 0}

        with patch.object(processing, "_cssp", side_effect=cssp):
            with self.assertRaises(base.CustomerServiceProblemError) as ctx:
                processing.run("yunda", "process", object(), {"kind": "cssp", "operation": "statement", "item": {"external_id": "C1"},
                                                               "payload": {"putboundIdentity": "投诉人", "customerName": "甲", "callNumber": "1", "dealContent": "完"}})
        self.assertEqual("WRITE_OUTCOME_UNKNOWN", ctx.exception.code)

    def test_transfer_abnormal_uploads_staged_images_then_saves(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as folder, patch.object(processing, "STAGE_DIR", Path(folder) / "staged"):
            source = Path(folder) / "a.jpg"
            source.write_bytes(b"jpg")
            staged = processing.stage_file(source, "现场.jpg")
            rows = [{"Replace_Id": "R1", "Replace_Status_Code": "0", "Abnormal_Type": ""},
                    {"Replace_Id": "R1", "Replace_Status_Code": "0", "Abnormal_Type": "客户联系不上"}]
            posts = []

            def inms(session, url, data=None, files=None):
                posts.append((url.rsplit("/", 1)[-1], data, files))
                return {"status": 1, "msg": "成功"}

            with patch.object(processing, "_transfer_row", side_effect=lambda s, rid, status="0": rows.pop(0) if rows else None), \
                    patch.object(processing, "_inms", side_effect=inms):
                result = processing.run("yunda", "process", object(), {"kind": "transfer", "operation": "abnormal", "item": {"external_id": "R1"},
                                                                        "payload": {"Abnormal_Type": "2", "abnormal_image1": [staged]}})
            self.assertTrue(result["verified"])
            self.assertEqual(["uploadAbnormalImage.html", "save.html"], [post[0] for post in posts])
            self.assertEqual({"Replace_Id": "R1", "Image_Sort": 1}, posts[0][1])
            self.assertEqual(("现场.jpg", b"jpg"), posts[0][2]["file"])
            self.assertEqual({"Replace_Id[]": ["R1"], "Abnormal_Type": "2", "Abnormal_Remark": ""}, posts[1][1])
            self.assertFalse((Path(folder) / "staged" / staged["new_name"]).exists())


class NewAppealTests(unittest.TestCase):
    def test_new_appeal_saves_appeal_then_files_and_reads_back(self):
        row = {"EXCEPTION_ID": "E1", "BILL_CODE": "B1", "APPEAL_TYPE": "差错处理"}
        saved, new_ids = [], []

        def find(session, context, call_id, data, paged=False):
            return {
                "FIND_BILL_INFO_ON_EXCEPTION_REG": [{"PRODUCT_TYPE": "普通"}],
                "FIND_APPEAL_TYPE_COMBOBOX": [{"APPEAL_TYPE": "延误", "BL_EVIDENCE": "0"}],
                "GET_TAB_APPEAL_TYPE": [{"APPEAL_TERM": "7"}],
                "FIND_EXCEPTION_APPEAL_VALIDATE": [], "FIND_TAB_APPEAL_TYPE": [{"BL_STAKEHOLDER": 0}],
                "FIND_NOW": [{"SYSDATE": "2026-11-27 09:00:00"}],
                "FIND_TAB_EXCEPTION_APPEAL": [{"EXCEPTION_ID": new_ids[0] if new_ids else ""}],
                "FIND_EXCEPTION_PATH": [{"FILE_PATH": "/unauth/download/a"}],
            }[call_id]

        def save(session, context, tables, label):
            saved.append(tables)
            if tables[0][0] == "TAB_EXCEPTION_APPEAL_ADD":
                new_ids.append(tables[0][1][0]["EXCEPTION_ID"])
            return {"success": True}

        with patch.object(base, "_read_user_info_cookie", return_value={**USER, "loginSiteType": "一级网点"}), \
                patch.object(base, "_resolve_ronghui_page_context", return_value={"page_id": "P", "url": "u"}), \
                patch.object(processing, "_subpage_context", return_value={"page_id": "SUB", "url": "s"}), \
                patch.object(processing, "_complaint_row", return_value=row), patch.object(processing, "_find", side_effect=find), \
                patch.object(processing, "_save", side_effect=save):
            result = processing.run("ronghui", "process", object(), {"kind": "complaint_against", "operation": "appeal",
                                                                      "item": {"external_id": "E1"}, "payload": {
                                                                          "PROCESS_TYPE": "差错处理", "APPEAL_TYPE": "延误", "PHONE": "13800000000",
                                                                          "CLAIM_AMOUNT": "20", "appeal_file1": [
                                                                              {"platform": "ronghui", "file_name": "a.jpg", "file_path": "/unauth/download/a"}]}})
        self.assertTrue(result["verified"])
        self.assertEqual(["TAB_EXCEPTION_APPEAL_ADD", "TAB_EXCEPTION_PATH_ADD"], [tables[0][0] for tables in saved])


class YundaReceiveTests(unittest.TestCase):
    def test_receive_posts_count_and_reads_waiting_back(self):
        waiting = [5, 2]
        posts = []
        with patch.object(processing, "_cssp_waiting", side_effect=lambda s, op: waiting.pop(0)), \
                patch.object(processing, "_cssp", side_effect=lambda s, m, path, **kw: posts.append((path, kw)) or {"code": 0}):
            result = processing.run("yunda", "process", object(), {"kind": "cssp", "operation": "receive",
                                                                    "item": {"external_id": "receive"}, "payload": {"receiveNum": "3"}})
        self.assertTrue(result["verified"])
        self.assertEqual(("/cssp/comp/sysCompDeal/receive.do", {"params": {"receiveNum": 3}}), posts[0])

    def test_receive_without_change_is_unknown(self):
        with patch.object(processing, "_cssp_waiting", return_value=4), patch.object(processing, "_cssp", return_value={"code": 0}):
            with self.assertRaises(base.CustomerServiceProblemError) as ctx:
                processing.run("yunda", "process", object(), {"kind": "cssp", "operation": "receive",
                                                               "item": {"external_id": "receive"}, "payload": {"receiveNum": "1"}})
        self.assertEqual("WRITE_OUTCOME_UNKNOWN", ctx.exception.code)

    def test_appoint_checks_person_and_reads_owner_back(self):
        calls = []

        def cssp(session, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if "compInfo" in path:
                return {"complaintId": "C1", "compStatus": 2}
            if path.endswith("/getUser"):
                return {"code": 0, "User": {"userId": "U", "deptId": "D1"}}
            if path.endswith("/getDeptEmp"):
                return {"rows": [{"code": "1001", "name": "合成员工"}]}
            if path.endswith("/compDealPageQuery"):
                return {"rows": [{"complaintId": "C1", "belongUserName": "合成员工"}]}
            return {"code": 0}

        with patch.object(processing, "_cssp", side_effect=cssp):
            result = processing.run("yunda", "process", object(), {"kind": "cssp", "operation": "appoint",
                                                                    "item": {"external_id": "C1", "waybill_no": "W1"},
                                                                    "payload": {"user_code": "1001"}})
        self.assertTrue(result["verified"])
        post = [call for call in calls if call[0] == "post"][0]
        self.assertEqual({"userId": "1001", "complaintIds": "C1"}, post[2]["params"])
        self.assertEqual({"gs": "D1", "empid": "1001"}, [call for call in calls if call[1].endswith("getDeptEmp")][0][2]["params"])
