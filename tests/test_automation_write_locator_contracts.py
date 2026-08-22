from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from agent.automation_plugins.broker import (
    LocalBrokerCapabilityIssuer,
    _extract_write_target_ref,
    recoverable_write_action_contracts,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party import recoverable_write_broker_actions


def _arguments(action: str) -> dict[str, object]:
    if action in {
        "waybill.snapshot.replace", "arrival.forecast_snapshot.replace",
        "scan.snapshot.replace", "arrival.snapshot.replace",
        "split_pending.snapshot.refresh", "feishu.sheet.replace",
        "feishu.sheet.add", "feishu.bitable.write_records",
    }:
        return {"records": [{"tracking_number": "TRACKING-RAW-001"}], "target_date": "2026-08-22"}
    if action == "scan.snapshot.cleanup":
        return {"retention_days": 30}
    if action == "waybill.delivery_status.update":
        return {"bill_codes": ["TRACKING-RAW-001"], "status": "signed"}
    if action == "daily_sign.authoritative_sync":
        return {"target_date": "2026-08-22", "request_note": "private raw row"}
    if action == "finance.batch.acquire":
        return {"schema_version": 1, "contract": {"raw": "private"}, "contract_sha256": "a" * 64}
    if action == "finance.source_snapshot.write":
        return {
            "batch_id": 7, "target_date": "2026-08-22", "run_ref": "private-run-ref",
            "transactions": [{"tracking_number": "TRACKING-RAW-001"}],
        }
    if action == "finance.projection.commit":
        return {"batch_id": 7, "outcomes": [{"run_ref": "private-run-ref"}]}
    raise AssertionError(f"missing fixture for {action}")


class WriteLocatorContractTests(unittest.TestCase):
    def _issuer(self, tmp_path: Path, automation_id: str, operation: str, action: str):
        receipts: list[dict[str, object]] = []
        issuer = LocalBrokerCapabilityIssuer(
            tmp_path / f"{automation_id}-{action}.sock",
            write_attempt_recorder=receipts.append,
        )
        capability = issuer.issue(
            automation_id=automation_id,
            plugin_version="1.0.4",
            tool_name=f"automation.{automation_id}.run",
            ttl_seconds=60,
            runtime_permissions={
                "browser": operation.startswith("browser."),
                "network": operation.startswith("network."),
                "office": False,
                "max_broker_calls": 1,
                "broker_operations": [{
                    "operation": operation, "action": action,
                    "roles": ["managed_role"], "effect": "write",
                }],
            },
            account_roles=({"role": "managed_role"},),
            resource_roles=(),
            account_bindings={"managed_role": "opaque-internal-binding"},
            resource_bindings={},
            write_attempt_context={
                "automation_id": automation_id,
                "plugin_id": {
                    "arrive_list": "sync_arrive_list",
                    "arrival_stats": "sync_arrival_stats",
                    "daily_sign": "sync_daily_should_sign",
                    "delivery_status": "sync_delivery_status",
                    "finance_startup_catchup": "sync_finance_bills",
                }[automation_id],
                "generation": 2,
                "lease_id": str(uuid.uuid4()),
                "orchestration_run_id": str(uuid.uuid4()),
                "step_id": str(uuid.uuid4()),
            },
        )
        return issuer, capability, receipts

    def test_signed_five_project_write_set_equals_broker_locator_set(self):
        expected = {
            ("arrive_list", "projection.invoke", "waybill.snapshot.replace"),
            ("arrive_list", "projection.invoke", "arrival.forecast_snapshot.replace"),
            ("arrive_list", "network.request", "feishu.sheet.replace"),
            ("arrival_stats", "projection.invoke", "scan.snapshot.replace"),
            ("arrival_stats", "projection.invoke", "scan.snapshot.cleanup"),
            ("arrival_stats", "projection.invoke", "waybill.snapshot.replace"),
            ("arrival_stats", "projection.invoke", "arrival.snapshot.replace"),
            ("arrival_stats", "projection.invoke", "split_pending.snapshot.refresh"),
            ("arrival_stats", "network.request", "feishu.sheet.replace"),
            ("arrival_stats", "network.request", "feishu.sheet.add"),
            ("daily_sign", "ledger.invoke", "daily_sign.authoritative_sync"),
            ("delivery_status", "network.request", "feishu.bitable.write_records"),
            ("delivery_status", "projection.invoke", "waybill.delivery_status.update"),
            ("finance_startup_catchup", "ledger.invoke", "finance.batch.acquire"),
            ("finance_startup_catchup", "ledger.invoke", "finance.source_snapshot.write"),
            ("finance_startup_catchup", "ledger.invoke", "finance.projection.commit"),
        }
        self.assertEqual(expected, recoverable_write_broker_actions())
        self.assertEqual(expected, recoverable_write_action_contracts())
        self.assertEqual(16, len(expected))

    def test_every_signed_write_emits_payload_free_stable_locator(self):
        root = Path(self._testMethodName)
        for automation_id, operation, action in sorted(recoverable_write_broker_actions()):
            with self.subTest(automation_id=automation_id, action=action):
                issuer, capability, receipts = self._issuer(root, automation_id, operation, action)
                arguments = _arguments(action)
                request_id = str(uuid.uuid4())
                issuer.consume(
                    capability,
                    request_id=request_id,
                    operation=operation,
                    action=action,
                    role="managed_role",
                    arguments=arguments,
                )
                marker = issuer.mark_write_started_hook(capability, request_id=request_id)
                self.assertIsNotNone(marker)
                marker()
                self.assertEqual(1, len(receipts))
                receipt = receipts[0]
                locator = receipt["target_ref_json"]
                self.assertEqual(1, locator["schema"])
                self.assertEqual(automation_id, locator["automation_id"])
                raw = str(locator)
                for forbidden in ("TRACKING-RAW-001", "private", "token", "cookie", "password", "secret"):
                    self.assertNotIn(forbidden.lower(), raw.lower())
                self.assertIsInstance(locator["record_count"], int)
                self.assertEqual(64, len(receipt["target_ref_sha256"]))

    def test_invalid_locator_fails_before_a_write_is_started(self):
        issuer, capability, receipts = self._issuer(
            Path(self._testMethodName), "arrive_list", "network.request", "feishu.sheet.replace",
        )
        request_id = str(uuid.uuid4())
        issuer.consume(
                capability,
                request_id=request_id,
                operation="network.request",
                action="feishu.sheet.replace",
                role="managed_role",
                arguments={"target_date": "2026-08-22"},
        )
        with self.assertRaisesRegex(PluginExecutionError, "locator"):
            issuer.mark_write_started_hook(capability, request_id=request_id)()
        self.assertEqual([], receipts)
        self.assertEqual(0, issuer.started_mutating_call_count(capability))

    def test_issuer_write_never_uses_the_direct_grant_context_default(self):
        receipts: list[dict[str, object]] = []
        issuer = LocalBrokerCapabilityIssuer(
            Path(self._testMethodName) / "missing-context.sock",
            write_attempt_recorder=receipts.append,
        )
        capability = issuer.issue(
            automation_id="other-instance",
            plugin_version="1.0.0",
            tool_name="automation.other-instance.run",
            ttl_seconds=60,
            runtime_permissions={
                "browser": True, "network": False, "office": False,
                "max_broker_calls": 1,
                "broker_operations": [{
                    "operation": "browser.invoke", "action": "ronghui.clock.submit",
                    "roles": ["managed_role"], "effect": "write",
                }],
            },
            account_roles=({"role": "managed_role"},),
            resource_roles=(),
            account_bindings={"managed_role": "opaque-binding"},
            resource_bindings={},
        )

        request_id = str(uuid.uuid4())
        issuer.consume(
                capability, request_id=request_id, operation="browser.invoke",
                action="ronghui.clock.submit", role="managed_role", arguments={},
        )
        with self.assertRaisesRegex(PluginExecutionError, "evidence"):
            issuer.mark_write_started_hook(capability, request_id=request_id)()
        self.assertEqual([], receipts)
        self.assertEqual(0, issuer.started_mutating_call_count(capability))

    def test_normalized_locator_is_stable_and_target_or_action_changes_it(self):
        common = {
            "automation_id": "arrive_list", "operation": "projection.invoke",
            "action": "waybill.snapshot.replace", "role": "managed_role",
            "binding": "opaque-internal-binding", "request_id": "request-1",
            "plugin_id": "sync_arrive_list",
        }
        first, first_digest = _extract_write_target_ref(
            **common,
            arguments={"records": [{"tracking_number": "raw-a"}], "target_date": "2026-08-22"},
        )
        same, same_digest = _extract_write_target_ref(
            **common,
            arguments={"records": [{"tracking_number": "raw-a"}], "target_date": "2026-08-22"},
        )
        changed, changed_digest = _extract_write_target_ref(
            **common,
            arguments={"records": [{"tracking_number": "raw-a"}], "target_date": "2026-08-23"},
        )
        other_action, other_digest = _extract_write_target_ref(
            automation_id="arrival_stats", operation="projection.invoke",
            action="scan.snapshot.cleanup", role="managed_role",
            binding="opaque-internal-binding", request_id="request-1",
            plugin_id="sync_arrival_stats",
            arguments={"retention_days": 30},
        )
        self.assertEqual(first, same)
        self.assertEqual(first_digest, same_digest)
        self.assertNotEqual(first_digest, changed_digest)
        self.assertNotEqual(first_digest, other_digest)
        self.assertNotEqual(first["content_sha256"], changed["content_sha256"])
        self.assertEqual(0, other_action["record_count"])

    def test_receipt_migration_and_repository_keep_locator_digest_contract(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / "agent" / "migrations" / "025_automation_write_attempt_receipts.sql").read_text(
            encoding="utf-8"
        )
        repository = "\n".join(
            (root / "shared" / path).read_text(encoding="utf-8")
            for path in (
                "automation_plugin_generation_repository.py",
                "automation_write_attempt_repository.py",
            )
        )
        for column in (
            "receipt_id", "automation_id", "generation", "lease_id",
            "orchestration_run_id", "step_id", "request_id", "operation",
            "action", "argument_sha256", "target_ref_sha256", "target_ref_json",
            "outcome", "evidence_sha256",
        ):
            self.assertIn(column, migration)
        for field in (
            "binding_sha256", "batch_sha256", "run_sha256",
            "idempotency_key_sha256", "record_count",
        ):
            self.assertIn(f'"{field}"', repository)


if __name__ == "__main__":
    unittest.main()
