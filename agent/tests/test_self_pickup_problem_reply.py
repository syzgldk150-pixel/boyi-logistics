import unittest

from agent.direct_tool_router import format_self_pickup_problem_upload_reply


class SelfPickupProblemReplyTests(unittest.TestCase):
    def test_done_reply_only_keeps_outcome_counts(self):
        text = format_self_pickup_problem_upload_reply(
            {
                "stage": "done",
                "candidate_count": 2,
                "saved_bills": 1,
                "skipped_bills": 1,
                "failed_bills": 0,
                "uploaded_files_total": 0,
                "results": [
                    {"bill_code": "R_SKIP", "skipped": True, "message": "跳过"},
                    {"bill_code": "R_SAVE", "saved": True},
                ],
                "source_summaries": [
                    {
                        "source_name": "邵阳自提部",
                        "candidate_count": 2,
                        "saved_bills": 1,
                        "skipped_bills": 1,
                        "failed_bills": 0,
                        "uploaded_files_total": 0,
                    }
                ],
            }
        )

        self.assertEqual("自提到货问题件已完成\n完成：1 单\n跳过：1 单", text)

    def test_preview_reply_only_keeps_candidate_count_and_confirmation(self):
        text = format_self_pickup_problem_upload_reply(
            {
                "stage": "dry_run",
                "candidate_count": 1,
                "source": {"sheet_title": "每日到货表"},
                "screenshot_enabled": False,
                "source_summaries": [
                    {
                        "source_name": "邵阳大祥S站自提",
                        "candidate_count": 1,
                        "candidates": [{"bill_code": "R00019410354"}],
                    }
                ],
            }
        )

        self.assertEqual(
            "自提到货问题件待确认：1 单\n回复“确认”执行，回复“取消”放弃。10 分钟内有效。",
            text,
        )
        self.assertNotIn("来源", text)
        self.assertNotIn("截图", text)
        self.assertNotIn("R00019410354", text)


if __name__ == "__main__":
    unittest.main()
