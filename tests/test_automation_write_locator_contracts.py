from __future__ import annotations

import unittest
from pathlib import Path

from agent.automation_plugins.broker import _extract_write_target_ref


class WriteLocatorContractTests(unittest.TestCase):
    def test_normalized_locator_is_stable_and_target_or_action_changes_it(self):
        common = {
            "automation_id": "arrive_list", "operation": "projection.invoke",
            "action": "waybill.snapshot.replace", "role": "managed_role",
            "binding": "opaque-internal-binding", "request_id": "request-1",
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
