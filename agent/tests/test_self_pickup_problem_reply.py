import unittest

from agent.direct_tool_router import format_self_pickup_problem_upload_reply


class SelfPickupProblemReplyTests(unittest.TestCase):
    def test_done_reply_labels_and_lists_skipped_bills_without_problem_content_reason(self):
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

        self.assertIn("跳过：1", text)
        self.assertIn("跳过 1", text)
        self.assertIn("跳过单号：R_SKIP", text)
        self.assertNotIn("货物未齐跳过", text)
        self.assertNotIn("已登记跳过", text)


if __name__ == "__main__":
    unittest.main()
