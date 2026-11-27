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
