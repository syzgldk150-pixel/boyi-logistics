from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from plugin_core_adapters.first_party import (
    _scan_recovery_windows,
    recover_scan_codes_unknown_write,
)


class _Catalog:
    def __init__(self, entry: object) -> None:
        self._entry = entry

    def require(self, automation_id: str) -> object:
        assert automation_id == "scan_codes"
        return self._entry


class _Target:
    def __init__(self) -> None:
        self.recovery_calls: list[dict[str, object]] = []

    def inspect_current_unknown_write(self, **_kwargs: object) -> dict[str, object]:
        return {
            "state": "RECEIPTS_IDENTIFIED",
            "lease_id": "lease-1",
            "receipt_identity_sha256": "a" * 64,
            "receipts": [
                {
                    "action": "ronghui.scan_next.submit",
                    "outcome": "WRITE_OUTCOME_UNKNOWN",
                }
            ],
        }

    def inspect_scan_unknown_write_context(self, **_kwargs: object) -> dict[str, object]:
        return {
            "state": "SCAN_RECOVERY_CONTEXT_IDENTIFIED",
            "items": [{"bill_code": "R1", "station_name": "下一站"}],
            "attempt_started_at": datetime(2026, 9, 1, 11, 29),
            "attempt_finished_at": datetime(2026, 9, 1, 11, 30),
        }

    def recover_unknown_write(self, **kwargs: object) -> dict[str, object]:
        self.recovery_calls.append(dict(kwargs))
        return {"recovery_status": "NOT_APPLIED", "transitioned": True}


class ScanUnknownWriteRecoveryTests(unittest.TestCase):
    def _runtime(self) -> tuple[object, _Target]:
        target = _Target()
        entry = SimpleNamespace(
            plugin_id="sync_scan_codes",
            committed_generation=3,
            target_generation=3,
            account_bindings={"account_id": "account-1"},
        )
        return SimpleNamespace(catalog=_Catalog(entry), target_service=target), target

    def test_naive_attempt_covers_utc_and_shanghai_interpretations(self) -> None:
        windows = _scan_recovery_windows(
            datetime(2026, 9, 1, 11, 29),
            datetime(2026, 9, 1, 11, 30),
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0][0].hour, 19)
        self.assertEqual(windows[1][0].hour, 11)

    @patch("plugin_core_adapters.first_party.get_account_manager")
    @patch("plugin_core_adapters.first_party._scan_next_readback_state")
    def test_exact_empty_readback_releases_old_mutex(
        self,
        readback: object,
        account_manager: object,
    ) -> None:
        runtime, target = self._runtime()
        readback.return_value = {
            "state": "NOT_APPLIED",
            "record_count": 0,
        }
        account_manager.return_value.require_active_binding_descriptor.return_value = {
            "session_profile": "profile-1"
        }

        result = recover_scan_codes_unknown_write(
            runtime,
            "scan_codes",
            "request-1",
        )

        self.assertEqual(result["recovery_status"], "NOT_APPLIED")
        self.assertEqual(len(target.recovery_calls), 1)
        proof = target.recovery_calls[0]["authoritative_not_applied_proof"]
        self.assertEqual(proof["receipt_identity_sha256"], "a" * 64)
        self.assertEqual(len(proof["evidence_sha256"]), 64)

    @patch("plugin_core_adapters.first_party.get_account_manager")
    @patch("plugin_core_adapters.first_party._scan_next_readback_state")
    def test_partial_readback_keeps_mutex(
        self,
        readback: object,
        account_manager: object,
    ) -> None:
        runtime, target = self._runtime()
        readback.return_value = {"state": "UNKNOWN", "record_count": 1}
        account_manager.return_value.require_active_binding_descriptor.return_value = {
            "session_profile": "profile-1"
        }

        result = recover_scan_codes_unknown_write(
            runtime,
            "scan_codes",
            "request-1",
        )

        self.assertIsNone(result)
        self.assertEqual(target.recovery_calls, [])


if __name__ == "__main__":
    unittest.main()
