from __future__ import annotations

import unittest

from agent.orchestration.impact_preview import build_write_impact, validate_write_impact
from agent.orchestration.models import OperationType, OrchestrationError


class WriteImpactPreviewTests(unittest.TestCase):
    def test_clock_in_has_two_exact_site_action_entities(self):
        impact = build_write_impact(
            tool_name="clock_in_dual",
            operation_type=OperationType.EXTERNAL_WRITE,
            account_id="clock-account",
            arguments={
                "account_id": "clock-account",
                "sitecode": "SITE-A",
                "sitefbcode": "SITE-B",
                "sitename": "A",
                "sitefbname": "B",
                "first_type": "交件到港",
                "second_type": "接件离港",
            },
        )

        assert impact is not None
        self.assertEqual(
            ["SITE-A:交件到港", "SITE-B:接件离港"],
            [row["entity_id"] for row in impact["entities"]],
        )
        validate_write_impact(operation_type=OperationType.EXTERNAL_WRITE, impact=impact)

    def test_customer_problem_and_receipt_use_exact_identifiers(self):
        mark_read = build_write_impact(
            tool_name="customer_service_problem_mark_read",
            operation_type=OperationType.EXTERNAL_WRITE,
            account_id="account-1",
            arguments={"platform": "yunda", "account_id": "account-1", "external_id": "problem-7"},
        )
        receipt = build_write_impact(
            tool_name="receipts_audit",
            operation_type=OperationType.EXTERNAL_WRITE,
            account_id=None,
            arguments={
                "platform": "ronghui",
                "direction": "send",
                "result": "passed",
                "waybill_no": "R0001",
            },
        )

        assert mark_read is not None and receipt is not None
        self.assertEqual("problem-7", mark_read["entities"][0]["entity_id"])
        self.assertEqual("R0001", receipt["entities"][0]["entity_id"])

    def test_publish_binds_waybill_and_full_payload_hash(self):
        impact = build_write_impact(
            tool_name="customer_service_problem_publish",
            operation_type=OperationType.EXTERNAL_WRITE,
            account_id="account-1",
            arguments={
                "platform": "ronghui",
                "account_id": "account-1",
                "payload": {
                    "bill_code": "R0002",
                    "problem_type": "少货",
                    "owner_problem_type": "少货",
                    "notice_site_code": "S1",
                    "notice_site": "站点",
                    "problem_cause": "证据",
                },
            },
        )

        assert impact is not None
        self.assertEqual("R0002", impact["entities"][0]["entity_id"])
        self.assertRegex(impact["entities"][0]["metadata"]["payload_sha256"], r"^[0-9a-f]{64}$")

    def test_split_stays_blocked_until_read_after_write_verification_exists(self):
        with self.assertRaises(OrchestrationError) as raised:
            build_write_impact(
                tool_name="split_pending_problem_upload",
                operation_type=OperationType.EXTERNAL_WRITE,
                account_id="account-1",
                arguments={"dry_run": True},
            )
        self.assertEqual("IMPACT_PREVIEW_REQUIRED", raised.exception.code)

        with self.assertRaises(OrchestrationError) as raised:
            build_write_impact(
                tool_name="split_pending_problem_upload",
                operation_type=OperationType.EXTERNAL_WRITE,
                account_id="account-1",
                arguments={
                    "dry_run": False,
                    "selected_bill_codes": ["R2", "R1"],
                    "preview_fingerprint": "a" * 64,
                },
            )
        self.assertEqual("IMPACT_PREVIEW_REQUIRED", raised.exception.code)

    def test_broad_and_unregistered_writes_fail_closed(self):
        for tool_name in (
            "self_pickup_problem_upload",
            "r7_arrival_checkin",
            "r7_departure_checkin",
            "customer_service_problem_upload_attachment",
            "future_external_write",
        ):
            with self.subTest(tool_name=tool_name), self.assertRaises(OrchestrationError) as raised:
                build_write_impact(
                    tool_name=tool_name,
                    operation_type=OperationType.EXTERNAL_WRITE,
                    account_id="account-1",
                    arguments={},
                )
            self.assertEqual("IMPACT_PREVIEW_REQUIRED", raised.exception.code)
            self.assertEqual("BLOCKED_DATA", raised.exception.details["status"])

    def test_high_risk_finance_projection_does_not_require_external_impact_preview(self):
        self.assertIsNone(
            build_write_impact(
                tool_name="sync_finance_bills",
                operation_type=OperationType.INTERNAL_PROJECTION_WRITE,
                account_id="price_default",
                arguments={"mode": "sync", "target_date": "2026-08-12"},
            )
        )

    def test_preview_fingerprint_tampering_is_rejected(self):
        impact = build_write_impact(
            tool_name="customer_service_problem_reply",
            operation_type=OperationType.EXTERNAL_WRITE,
            account_id="account-1",
            arguments={
                "platform": "yunda",
                "account_id": "account-1",
                "external_id": "problem-1",
            },
        )
        assert impact is not None
        impact["entities"][0]["entity_id"] = "problem-2"
        with self.assertRaises(OrchestrationError) as raised:
            validate_write_impact(operation_type=OperationType.EXTERNAL_WRITE, impact=impact)
        self.assertEqual("IMPACT_PREVIEW_STALE", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
