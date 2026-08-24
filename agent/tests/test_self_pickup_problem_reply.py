import unittest

from agent.direct_tool_router import format_self_pickup_problem_upload_reply


class SelfPickupProblemReplyTests(unittest.TestCase):
    def test_dry_run_with_exact_evidence_offers_confirmation(self):
        text = format_self_pickup_problem_upload_reply(
            {
                "stage": "dry_run",
                "candidate_count": 2,
                "preview_fingerprint": "a" * 64,
                "candidates": [
                    {"bill_code": "R_SELF"},
                    {"bill_code": "R_DX_PICK"},
                ],
            }
        )

        self.assertIn('回复"确认"', text)
        self.assertNotIn("不生成确认操作", text)

    def test_empty_dry_run_does_not_offer_confirmation(self):
        text = format_self_pickup_problem_upload_reply(
            {
                "stage": "dry_run",
                "candidate_count": 0,
                "preview_fingerprint": "a" * 64,
                "candidates": [],
            }
        )

        self.assertIn("不生成确认操作", text)
        self.assertNotIn('回复"确认"', text)

    def test_over_limit_dry_run_does_not_offer_confirmation(self):
        text = format_self_pickup_problem_upload_reply(
            {
                "stage": "dry_run",
                "candidate_count": 251,
                "preview_fingerprint": "a" * 64,
                "candidates": [
                    {"bill_code": f"R{index}"}
                    for index in range(251)
                ],
            }
        )

        self.assertIn("超过单次签名上限", text)
        self.assertNotIn('回复"确认"', text)

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
