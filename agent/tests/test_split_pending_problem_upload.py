import unittest
from unittest.mock import patch

from agent.direct_tool_router import direct_tool_request_from_text, is_deprecated_split_command
from tools import split_pending_problem_upload_tool as tool


SOURCE_HEADERS = [
    "08.03运单编号",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "回单号",
    "实际重量",
    "体积",
    "备注",
    "目的站点",
    "收件人",
    "收件电话",
    "收件地址",
    "结算重量",
    "体积重",
    "运费",
    "支付类型",
    "到付款",
    "累计到货件数",
]


def source_row(bill_code: str, expected: int, arrived: int) -> list[object]:
    return [
        bill_code,
        "货物",
        "纸箱",
        "送货",
        expected,
        "",
        10,
        1,
        "",
        "邵阳操作场",
        "收件人",
        "13800000000",
        "地址",
        10,
        1,
        20,
        "现付",
        0,
        arrived,
    ]


def source_values(*rows: list[object]) -> list[list[object]]:
    return [SOURCE_HEADERS, *rows]


class SplitPendingProblemUploadTests(unittest.TestCase):
    def test_classifies_partial_and_zero_arrival(self):
        candidates, source_rows = tool.classify_sheet_values(
            source_values(source_row("R_PART", 10, 4), source_row("R_ZERO", 6, 0))
        )
        self.assertEqual(2, source_rows)
        self.assertEqual("少货/分批", candidates[0]["problem_type"])
        self.assertEqual("应到10件，已到4件", candidates[0]["problem_cause"])
        self.assertEqual("有发未到", candidates[1]["problem_type"])
        self.assertEqual("通知类（不顺延时效）", candidates[1]["problem_owner_type"])

    def test_rejects_invalid_quantity(self):
        with self.assertRaisesRegex(ValueError, "大于应到件数"):
            tool.classify_sheet_values(source_values(source_row("R_BAD", 3, 4)))

    def test_stateful_candidates_hide_complete_and_show_step_retry(self):
        raw, _ = tool.classify_sheet_values(
            source_values(source_row("R_PART", 10, 4), source_row("R_ZERO", 6, 0))
        )
        eligible, hidden = tool._stateful_candidates(
            raw,
            [
                {
                    "tracking_number": "R_PART",
                    "problem_type": "少货/分批",
                    "upload_status": "success",
                    "complaint_status": "pending",
                },
                {
                    "tracking_number": "R_ZERO",
                    "problem_type": "有发未到",
                    "upload_status": "success",
                    "complaint_status": "not_applicable",
                },
            ],
        )
        self.assertEqual(1, hidden)
        self.assertEqual(["R_PART"], [item["bill_code"] for item in eligible])
        self.assertEqual("待补差错", eligible[0]["candidate_status"])
        self.assertTrue(eligible[0]["run_complaint"])
        self.assertFalse(eligible[0]["run_problem_item"])

    def test_same_type_quantity_change_stays_hidden_but_type_change_resets(self):
        partial, _ = tool.classify_sheet_values(source_values(source_row("R1", 10, 7)))
        hidden_candidates, hidden = tool._stateful_candidates(
            partial,
            [{
                "tracking_number": "R1",
                "problem_type": "少货/分批",
                "upload_status": "success",
                "complaint_status": "duplicate",
            }],
        )
        self.assertEqual([], hidden_candidates)
        self.assertEqual(1, hidden)

        changed, _ = tool._stateful_candidates(
            partial,
            [{
                "tracking_number": "R1",
                "problem_type": "有发未到",
                "upload_status": "success",
                "complaint_status": "not_applicable",
            }],
        )
        self.assertEqual("未执行", changed[0]["candidate_status"])
        self.assertEqual("pending", changed[0]["complaint_status"])
        self.assertEqual("pending", changed[0]["problem_item_status"])

    def test_fingerprint_changes_when_source_quantity_changes(self):
        first, _ = tool.classify_sheet_values(source_values(source_row("R1", 10, 4)))
        second, _ = tool.classify_sheet_values(source_values(source_row("R1", 10, 5)))
        first_state, _ = tool._stateful_candidates(first, [])
        second_state, _ = tool._stateful_candidates(second, [])
        self.assertNotEqual(
            tool._preview_fingerprint(first_state),
            tool._preview_fingerprint(second_state),
        )

    def test_dry_run_reads_only_and_returns_fingerprint(self):
        values = source_values(source_row("R_PART", 10, 4), source_row("R_ZERO", 6, 0))
        with patch.object(tool, "_read_source_values", return_value=(values, {"sheet_id": "8fc516"})), patch.object(
            tool, "list_split_pending_problem_items", return_value=[]
        ), patch.object(tool, "replace_split_pending_problem_items") as replace_mock, patch.object(
            tool, "_sync_target_sheet"
        ) as sheet_mock, patch.object(tool, "_upload_to_tms") as upload_mock:
            result = tool.run_split_pending_problem_upload({"dry_run": True})
        self.assertTrue(result["ok"])
        self.assertEqual("dry_run", result["stage"])
        self.assertEqual(2, result["candidate_count"])
        self.assertEqual(64, len(result["preview_fingerprint"]))
        replace_mock.assert_not_called()
        sheet_mock.assert_not_called()
        upload_mock.assert_not_called()

    def test_formal_mode_requires_selection_and_fingerprint_before_source_read(self):
        with patch.object(tool, "_read_source_values") as read_mock:
            result = tool.run_split_pending_problem_upload({"dry_run": False})
        self.assertEqual("selection_required", result["stage"])
        read_mock.assert_not_called()

    def test_stale_preview_produces_zero_writes(self):
        values = source_values(source_row("R1", 10, 4))
        with patch.object(tool, "_read_source_values", return_value=(values, {})), patch.object(
            tool, "list_split_pending_problem_items", return_value=[]
        ), patch.object(tool, "replace_split_pending_problem_items") as replace_mock, patch.object(
            tool, "_sync_target_sheet"
        ) as sheet_mock, patch.object(tool, "_upload_to_tms") as upload_mock:
            result = tool.run_split_pending_problem_upload(
                {
                    "dry_run": False,
                    "selected_bill_codes": ["R1"],
                    "preview_fingerprint": "0" * 64,
                }
            )
        self.assertEqual("preview_expired", result["stage"])
        replace_mock.assert_not_called()
        sheet_mock.assert_not_called()
        upload_mock.assert_not_called()

    def test_formal_mode_refreshes_all_but_uploads_only_selected(self):
        values = source_values(source_row("R1", 10, 4), source_row("R2", 6, 0))
        read_result = (values, {"sheet_id": "8fc516"})
        with patch.object(tool, "_read_source_values", return_value=read_result), patch.object(
            tool, "list_split_pending_problem_items", return_value=[]
        ):
            preview = tool.run_split_pending_problem_upload({"dry_run": True})
        upload_payload = {
            "message": "完成 1/1 单",
            "results": [{
                "bill_code": "R2",
                "complaint": None,
                "problem_item": {"status": "success"},
                "complete": True,
            }],
        }
        with patch.object(tool, "_read_source_values", return_value=read_result), patch.object(
            tool, "list_split_pending_problem_items", return_value=[]
        ), patch.object(
            tool,
            "replace_split_pending_problem_items",
            return_value={"current": 2},
        ) as replace_mock, patch.object(
            tool, "_sync_target_sheet", return_value={"rows": 2}
        ) as sheet_mock, patch.object(
            tool, "_upload_to_tms", return_value=upload_payload
        ) as upload_mock, patch.object(
            tool, "update_split_pending_combined_results", return_value={"updated": 1}
        ):
            result = tool.run_split_pending_problem_upload(
                {
                    "dry_run": False,
                    "selected_bill_codes": ["R2"],
                    "preview_fingerprint": preview["preview_fingerprint"],
                }
            )
        self.assertTrue(result["ok"])
        self.assertEqual(["R2"], result["selected_bill_codes"])
        self.assertEqual(2, len(replace_mock.call_args.args[0]))
        self.assertEqual(2, len(sheet_mock.call_args.args[0]))
        self.assertEqual(["R2"], [item["bill_code"] for item in upload_mock.call_args.args[0]])

    def test_router_accepts_only_exact_new_trigger(self):
        request = direct_tool_request_from_text("分批")
        self.assertEqual("preview_split_pending_problems", request["tool_name"])
        self.assertEqual({"account_id": "ronghui_default"}, request["params"])
        self.assertIn("selection_intent", request)
        for old_text in ("分批问题件", "上报分批差错", "分批差错", "上传分批/未到问题件"):
            self.assertTrue(is_deprecated_split_command(old_text))
            self.assertIsNone(direct_tool_request_from_text(old_text))


if __name__ == "__main__":
    unittest.main()
