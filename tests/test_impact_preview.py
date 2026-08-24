from __future__ import annotations

import unittest

from agent.orchestration.impact_preview import build_write_impact, validate_write_impact
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OperationType,
    OrchestrationError,
)
from agent.orchestration.planner import DeterministicPlanner
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
)


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

    def test_split_formal_selection_has_exact_ordered_waybill_impact(self):
        with self.assertRaises(OrchestrationError) as raised:
            build_write_impact(
                tool_name="automation.split_pending_problem_upload.run",
                operation_type=OperationType.EXTERNAL_WRITE,
                account_id="account-1",
                arguments={"dry_run": True},
            )
        self.assertEqual("IMPACT_PREVIEW_REQUIRED", raised.exception.code)

        impact = build_write_impact(
            tool_name="automation.split_pending_problem_upload.run",
            operation_type=OperationType.EXTERNAL_WRITE,
            account_id="account-1",
            arguments={
                "dry_run": False,
                "selected_bill_codes": ["R2", "R1"],
                "preview_fingerprint": "a" * 64,
            },
        )

        assert impact is not None
        self.assertEqual(["R2", "R1"], [item["entity_id"] for item in impact["entities"]])
        self.assertEqual([0, 1], [item["metadata"]["selection_index"] for item in impact["entities"]])
        self.assertEqual(
            "signed_preview_fingerprint_all_target_preflight_and_independent_read_after_write",
            impact["revalidation"],
        )
        validate_write_impact(operation_type=OperationType.EXTERNAL_WRITE, impact=impact)

    def test_self_pickup_formal_selection_has_exact_ordered_waybill_impact(self):
        impact = build_write_impact(
            tool_name="automation.self_pickup_problem_upload.run",
            operation_type=OperationType.EXTERNAL_WRITE,
            account_id=None,
            arguments={
                "dry_run": False,
                "selected_bill_codes": ["R_SELF", "R_DX_PICK"],
                "preview_fingerprint": "b" * 64,
            },
        )

        assert impact is not None
        self.assertEqual(
            ["R_SELF", "R_DX_PICK"],
            [item["entity_id"] for item in impact["entities"]],
        )
        self.assertEqual(
            "register_arrived_self_pickup_problem",
            impact["entities"][0]["metadata"]["action"],
        )
        validate_write_impact(
            operation_type=OperationType.EXTERNAL_WRITE,
            impact=impact,
        )

        with self.assertRaises(OrchestrationError) as raised:
            build_write_impact(
                tool_name="automation.self_pickup_problem_upload.run",
                operation_type=OperationType.EXTERNAL_WRITE,
                account_id=None,
                arguments={
                    "dry_run": False,
                    "selected_bill_codes": [f"R{index}" for index in range(251)],
                    "preview_fingerprint": "b" * 64,
                },
            )
        self.assertEqual("IMPACT_PREVIEW_REQUIRED", raised.exception.code)

    def test_split_impact_rejects_unbound_or_ambiguous_selection(self):
        invalid_arguments = (
            {"dry_run": False, "selected_bill_codes": [], "preview_fingerprint": "a" * 64},
            {
                "dry_run": False,
                "selected_bill_codes": ["R1", "R1"],
                "preview_fingerprint": "a" * 64,
            },
            {
                "dry_run": False,
                "selected_bill_codes": ["R 1"],
                "preview_fingerprint": "a" * 64,
            },
            {
                "dry_run": False,
                "selected_bill_codes": ["=R1"],
                "preview_fingerprint": "a" * 64,
            },
            {
                "dry_run": False,
                "selected_bill_codes": ["R1"],
                "preview_fingerprint": "not-a-fingerprint",
            },
            {
                "dry_run": False,
                "selected_bill_codes": [f"R{index}" for index in range(91)],
                "preview_fingerprint": "a" * 64,
            },
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(OrchestrationError) as raised:
                build_write_impact(
                    tool_name="automation.split_pending_problem_upload.run",
                    operation_type=OperationType.EXTERNAL_WRITE,
                    account_id="account-1",
                    arguments=arguments,
                )
            self.assertEqual("IMPACT_PREVIEW_REQUIRED", raised.exception.code)

    def test_split_project_formal_plan_uses_exact_selected_waybill_impact(self):
        tool_name = "automation.split_pending_problem_upload.run"

        class _Catalog:
            catalog_hash = "catalog-digest"

            @staticmethod
            def get_capability(requested_tool_name):
                if requested_tool_name != tool_name:
                    return None
                return {
                    "version": "1.0.21",
                    "operation_type": "external_write",
                    "risk_level": "high",
                    "llm_exposed": False,
                    "evidence": [],
                    "postconditions": [
                        {"name": "third_party_split_problem_confirmed"}
                    ],
                }

        command = Command(
            command_type="automation.project.invoke",
            source="feishu",
            actor=Actor(ActorType.FEISHU_USER, "user-1", roles=("super_admin",)),
            parameters={
                "tool_name": tool_name,
                "account_id": "account-1",
                "arguments": {
                    "dry_run": False,
                    "selected_bill_codes": ["R2", "R1"],
                    "preview_fingerprint": "a" * 64,
                },
            },
            idempotency_key="split-formal-1",
            automation_invocation=AutomationProjectInvocation(
                automation_id="split_pending_problem_upload",
                automation_generation=1,
                entrypoint=AutomationEntrypoint.FEISHU,
                contract_id="split-contract-v1",
                contract_hash="b" * 64,
                policy_version=1,
                project_configuration_version=1,
                request_id="split-formal-1",
            ),
        )

        plan = DeterministicPlanner(_Catalog()).plan(
            command,
            ContextSnapshot(values={}),
        )

        self.assertEqual(
            ["R2", "R1"],
            [entity["entity_id"] for entity in plan.impact["entities"]],
        )
        self.assertEqual(OperationType.EXTERNAL_WRITE, plan.steps[0].operation_type)

    def test_self_pickup_project_formal_plan_uses_exact_selected_waybill_impact(self):
        tool_name = "automation.self_pickup_problem_upload.run"

        class _Catalog:
            catalog_hash = "catalog-digest"

            @staticmethod
            def get_capability(requested_tool_name):
                if requested_tool_name != tool_name:
                    return None
                return {
                    "version": "1.0.21",
                    "operation_type": "external_write",
                    "risk_level": "high",
                    "llm_exposed": False,
                    "evidence": [],
                    "postconditions": [
                        {"name": "third_party_self_pickup_problem_confirmed"}
                    ],
                }

        command = Command(
            command_type="automation.project.invoke",
            source="feishu",
            actor=Actor(ActorType.FEISHU_USER, "user-1", roles=("super_admin",)),
            parameters={
                "tool_name": tool_name,
                "account_id": None,
                "arguments": {
                    "dry_run": False,
                    "selected_bill_codes": ["R_SELF", "R_DX_PICK"],
                    "preview_fingerprint": "b" * 64,
                },
            },
            idempotency_key="self-pickup-formal-1",
            automation_invocation=AutomationProjectInvocation(
                automation_id="self_pickup_problem_upload",
                automation_generation=1,
                entrypoint=AutomationEntrypoint.FEISHU,
                contract_id="self-pickup-contract-v1",
                contract_hash="c" * 64,
                policy_version=1,
                project_configuration_version=1,
                request_id="self-pickup-formal-1",
            ),
        )

        plan = DeterministicPlanner(_Catalog()).plan(
            command,
            ContextSnapshot(values={}),
        )

        self.assertEqual(
            ["R_SELF", "R_DX_PICK"],
            [entity["entity_id"] for entity in plan.impact["entities"]],
        )
        self.assertEqual(OperationType.EXTERNAL_WRITE, plan.steps[0].operation_type)

    def test_broad_and_unregistered_writes_fail_closed(self):
        for tool_name in (
            "self_pickup_problem_upload",
            "r7_arrival_checkin",
            "r7_departure_checkin",
            "customer_service_problem_upload_attachment",
            "split_pending_problem_upload",
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
