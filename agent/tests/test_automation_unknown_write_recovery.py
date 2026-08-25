from __future__ import annotations

import unittest
from types import SimpleNamespace

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.management import AutomationPluginManagementService
from agent.automation_plugins.models import RuntimeReconcileState
from agent.automation_plugins.first_party import RECOVERABLE_WRITE_PROJECT_PLUGINS
from agent.orchestration.models import Actor, ActorType


class _Catalog:
    def require(self, automation_id):
        return SimpleNamespace(
            automation_id=automation_id,
            plugin_id=RECOVERABLE_WRITE_PROJECT_PLUGINS.get(automation_id, "test-plugin"),
            display_name="test",
            installed_version="1.0.0",
            enabled=True,
            state="ENABLED",
            record_version=1,
            target_generation=2,
            committed_generation=2,
            reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
        )


class _Target:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def recover_unknown_write(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def recover_current_unknown_write(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _actor():
    return Actor(
        ActorType.CONSOLE_ADMIN,
        "admin",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )


class UnknownWriteRecoveryTests(unittest.TestCase):
    def _service(self, result):
        target = _Target(result)
        return (
            AutomationPluginManagementService(
                catalog=_Catalog(), lifecycle=object(), configuration=object(),
                worker_repository=object(), target_service=target,
                package_repository=object(), storage=object(),
            ),
            target,
        )

    def test_every_supported_project_uses_server_owned_identity_only(self):
        for automation_id in (
            "arrive_list", "arrival_stats", "daily_sign", "delivery_status",
            "finance_startup_catchup",
        ):
            with self.subTest(automation_id=automation_id):
                service, target = self._service({
                    "recovery_status": "UNKNOWN",
                    "reason": "RECOVERY_TARGET_READBACK_UNAVAILABLE",
                    "run_id": "run-1",
                    "step_id": "step-1",
                    "transitioned": False,
                    "idempotent": False,
                    "evidence": {"receipt_count": 1, "receipt_digest": "a" * 64},
                })
                result = service.recover_unknown_write(
                    automation_id, generation=2, lease_id="lease-1",
                    request_id="actor-claim-is-ignored", actor=_actor(),
                )
                self.assertEqual("UNKNOWN", result["recovery_status"])
                self.assertEqual("lease-1", target.calls[0]["lease_id"])
                self.assertEqual("actor-claim-is-ignored", target.calls[0]["request_id"])
                self.assertEqual("admin", target.calls[0]["actor_id"])
                self.assertEqual("super_admin", target.calls[0]["actor_role"])
                self.assertEqual("run-1", result["run_id"])

    def test_current_recovery_resolves_generation_and_lease_on_server(self):
        service, target = self._service({
            "recovery_status": "APPLIED",
            "reason": "ALL_RECEIPTS_WRITE_VERIFIED",
            "run_id": "run-1",
            "step_id": "step-1",
            "transitioned": True,
            "idempotent": False,
            "evidence": {"receipt_count": 1, "receipt_digest": "a" * 64},
        })

        result = service.recover_current_unknown_write(
            "arrive_list",
            request_id="server-current-recovery",
            actor=_actor(),
        )

        self.assertEqual("APPLIED", result["recovery_status"])
        self.assertEqual(2, target.calls[0]["generation"])
        self.assertNotIn("lease_id", target.calls[0])
        self.assertEqual("admin", target.calls[0]["actor_id"])

    def test_old_arrival_stats_claim_is_rejected(self):
        service, _target = self._service({})
        with self.assertRaisesRegex(PluginConflictError, "actor-supplied"):
            service.recover_arrival_stats_not_applied(
                "arrival_stats", generation=2, lease_id="lease-1",
                evidence_sha256="a" * 64,
                readback={"arrival_stat_runs": 0}, request_id="request", actor=_actor(),
            )

    def test_non_target_project_is_rejected(self):
        service, _target = self._service({})
        with self.assertRaises(PluginConflictError):
            service.recover_unknown_write(
                "not_a_target", generation=2, lease_id="lease-1",
                request_id="request", actor=_actor(),
            )

    def test_duplicate_instance_is_scoped_by_plugin_identity(self):
        class DuplicateCatalog(_Catalog):
            def require(self, automation_id):
                entry = super().require(automation_id)
                return SimpleNamespace(**{**entry.__dict__, "plugin_id": "sync_arrive_list"})

        target = _Target({"recovery_status": "UNKNOWN", "evidence": {}})
        service = AutomationPluginManagementService(
            catalog=DuplicateCatalog(), lifecycle=object(), configuration=object(),
            worker_repository=object(), target_service=target,
            package_repository=object(), storage=object(),
        )
        service.recover_unknown_write(
            "second-arrive-list", generation=2, lease_id="lease-2",
            request_id="request", actor=_actor(),
        )
        self.assertEqual("second-arrive-list", target.calls[0]["automation_id"])


if __name__ == "__main__":
    unittest.main()
