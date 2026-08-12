import unittest

from agent.direct_tool_router import (
    format_arrival_stats_reply,
    format_scan_sync_reply,
    format_send_order_sync_reply,
    format_split_pending_problem_upload_reply,
    format_yunda_dispatch_forecast_reply,
    format_yunda_send_waybills_reply,
)


class ConciseFeishuToolReplyTests(unittest.TestCase):
    def test_arrival_stats_reply_reports_partial_completion_without_internal_tables(self):
        text = format_arrival_stats_reply(
            {
                "success": True,
                "data": {
                    "main_trackings": 958,
                    "records": 29,
                    "count_result": {"arrived_nonzero": 29},
                    "primary_result": {"ok": True},
                    "secondary_result": {"ok": True},
                    "pending_result": {"skipped": True, "reason": "missing_resource"},
                    "split_pending_result": {"ok": True},
                    "archive_result": {"ok": True},
                },
            }
        )

        self.assertEqual(
            "统计到货数据部分完成\n完成：29 单\n已到货：29 单\n未完成：未齐货物表",
            text,
        )
        self.assertNotIn("扫描索引", text)
        self.assertNotIn("主统计表：", text)
        self.assertNotIn("归档快照：", text)

    def test_sync_replies_only_keep_completed_count(self):
        cases = (
            (
                format_send_order_sync_reply,
                {"success": True, "data": {"fetched": 12, "written": 10, "sql_upserted": 10}},
                "融辉寄件数据同步已完成\n完成：10 单",
            ),
            (
                format_yunda_dispatch_forecast_reply,
                {"success": True, "data": {"total": 13, "fetched": 12, "written": 12}},
                "韵达派件预测同步已完成\n完成：12 单",
            ),
            (
                format_yunda_send_waybills_reply,
                {"success": True, "data": {"total": 14, "fetched": 13, "written": 11}},
                "韵达寄件运单同步已完成\n完成：11 单",
            ),
        )
        for formatter, result, expected in cases:
            with self.subTest(formatter=formatter.__name__):
                self.assertEqual(expected, formatter(result))

    def test_problem_upload_reply_marks_partial_completion(self):
        text = format_split_pending_problem_upload_reply(
            {
                "stage": "done",
                "saved_bills": 2,
                "failed_bills": 1,
                "failed_bill_codes": ["R_FAIL"],
                "results": [
                    {
                        "bill_code": "R_FAIL",
                        "complete": False,
                        "problem_item": {"error": "提交超时"},
                    }
                ],
            }
        )

        self.assertIn("分批差错及问题件部分完成", text)
        self.assertIn("完成：2 单", text)
        self.assertIn("失败：1 单", text)
        self.assertNotIn("数据库快照", text)
        self.assertNotIn("目标 Sheet", text)

    def test_scan_reply_marks_all_failed_batches_as_not_completed(self):
        text = format_scan_sync_reply(
            {
                "success": True,
                "data": {
                    "child_items": 3,
                    "batch_results": [
                        {"batch": 1, "items": 3, "ok": False, "raw": {"error": "提交失败"}}
                    ],
                },
            }
        )

        self.assertEqual("扫描任务未完成\n完成：0 单\n失败：1 批（提交失败）", text)


if __name__ == "__main__":
    unittest.main()
