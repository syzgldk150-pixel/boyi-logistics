from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.models import Actor, ActorType, OrchestrationError, RiskLevel
from agent.orchestration.policy_engine import PolicyDecision, PolicyEngine
from shared.orchestration_repository import InvalidStateError


class _Policy:
    @staticmethod
    def can_decide(_actor, *, required_role, source):
        return required_role == "admin" and source == "console"


class _ApprovalRows:
    def __init__(self, message: str, trace: list[str]) -> None:
        self.message = message
        self.trace = trace

    def record_decision(self, _row, *, expected_plan_hash):
        del expected_plan_hash
        self.trace.append("approval_lock")
        raise InvalidStateError(self.message)


class _RunRows:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def get(self, run_id: str, *, for_update: bool):
        self.trace.append("run_lock" if for_update else "run_read")
        return {"run_id": run_id, "status": "WAITING_APPROVAL"}


class _UnitOfWork:
    def __init__(self, message: str, trace: list[str]) -> None:
        self.runs = _RunRows(trace)
        self.approvals = _ApprovalRows(message, trace)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False


class _Repository:
    def __init__(self, message: str) -> None:
        self.message = message
        self.trace: list[str] = []

    @staticmethod
    def get_approval(_approval_id: str):
        return {
            "approval_id": "approval-1",
            "run_id": "run-1",
            "required_role": "admin",
            "plan_hash": "plan-hash",
            "status": "PENDING",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        }

    def unit_of_work(self):
        return _UnitOfWork(self.message, self.trace)


class ApprovalServiceConcurrencyTests(unittest.TestCase):
    def test_bound_feishu_super_admin_can_decide_but_unbound_user_cannot(self):
        engine = PolicyEngine(object())
        bound = Actor(
            ActorType.FEISHU_USER,
            "ou-bound",
            ("admin", "super_admin"),
            authenticated_by="feishu_admin_binding",
        )
        unbound = Actor(
            ActorType.FEISHU_USER,
            "ou-unbound",
            (),
            authenticated_by="feishu_verified_event",
        )
        self.assertTrue(engine.can_decide(bound, required_role="super_admin", source="feishu"))
        self.assertFalse(engine.can_decide(unbound, required_role="super_admin", source="feishu"))

    def test_concurrent_second_decision_has_a_stable_conflict_code(self):
        repository = _Repository("approval request is no longer pending")
        service = ApprovalService(repository, _Policy())

        with self.assertRaises(OrchestrationError) as raised:
            service.decide(
                approval_id="approval-1",
                plan_hash="plan-hash",
                actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1", ("admin",)),
                source="console",
                decision="APPROVED",
            )

        self.assertEqual("APPROVAL_NOT_PENDING", raised.exception.code)
        self.assertEqual(["run_lock", "approval_lock"], repository.trace)

    def test_concurrent_plan_change_has_a_stable_stale_code(self):
        service = ApprovalService(
            _Repository("approval plan hash is stale"),
            _Policy(),
        )

        with self.assertRaises(OrchestrationError) as raised:
            service.decide(
                approval_id="approval-1",
                plan_hash="plan-hash",
                actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1", ("admin",)),
                source="console",
                decision="APPROVED",
            )

        self.assertEqual("PLAN_STALE", raised.exception.code)

    def test_request_locks_and_validates_run_before_touching_approval_rows(self):
        trace: list[str] = []

        class _RequestRuns:
            @staticmethod
            def get(_run_id, *, for_update):
                trace.append("run_lock" if for_update else "run_read")
                return {
                    "run_id": "run-1",
                    "work_item_id": "work-locked",
                    "status": "WAITING_APPROVAL",
                    "plan_hash": "plan-hash",
                    "correlation_id": "correlation-locked",
                    "causation_id": "causation-locked",
                    "cancel_requested_at": None,
                }

        class _RequestApprovals:
            @staticmethod
            def expire_stale(_run_id, _plan_hash):
                trace.append("approval_expire")

            @staticmethod
            def get_latest_for_run(_run_id, *, for_update):
                assert for_update is True
                trace.append("approval_lock")
                return None

            @staticmethod
            def create_or_get(row):
                trace.append("approval_create")
                assert row["work_item_id"] == "work-locked"
                return dict(row)

        class _RequestEvents:
            @staticmethod
            def append_with_outbox(event, _outbox):
                trace.append("event")
                assert event["work_item_id"] == "work-locked"
                assert event["correlation_id"] == "correlation-locked"
                assert event["causation_id"] == "causation-locked"

        class _RequestUow:
            runs = _RequestRuns()
            approvals = _RequestApprovals()
            events = _RequestEvents()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def commit():
                trace.append("commit")

        class _RequestRepository:
            @staticmethod
            def unit_of_work():
                return _RequestUow()

        service = ApprovalService(_RequestRepository(), _Policy())
        service.request(
            run={"run_id": "run-1", "work_item_id": "untrusted"},
            plan=SimpleNamespace(plan_hash="plan-hash", impact={}),
            policy_decision=PolicyDecision(
                allowed=True,
                requires_approval=True,
                required_role="admin",
                risk_level=RiskLevel.HIGH,
                code="APPROVAL_REQUIRED",
                reason="test",
            ),
            requested_by=Actor(
                ActorType.CONSOLE_ADMIN,
                "admin-1",
                ("admin",),
            ),
        )

        self.assertEqual(
            [
                "run_lock",
                "approval_expire",
                "approval_lock",
                "approval_create",
                "event",
                "commit",
            ],
            trace,
        )

    def test_request_rejects_stale_or_cancelled_run_before_approval_lock(self):
        decision = PolicyDecision(
            allowed=True,
            requires_approval=True,
            required_role="admin",
            risk_level=RiskLevel.HIGH,
            code="APPROVAL_REQUIRED",
            reason="test",
        )
        actor = Actor(ActorType.CONSOLE_ADMIN, "admin-1", ("admin",))

        for row_update, expected_message in (
            ({"plan_hash": "other-plan"}, "plan hash is stale"),
            ({"cancel_requested_at": datetime.now(timezone.utc)}, "cancellation"),
        ):
            with self.subTest(row_update=row_update):
                trace: list[str] = []

                class _Runs:
                    @staticmethod
                    def get(_run_id, *, for_update):
                        trace.append("run_lock" if for_update else "run_read")
                        return {
                            "run_id": "run-1",
                            "work_item_id": "work-1",
                            "status": "WAITING_APPROVAL",
                            "plan_hash": "plan-hash",
                            "correlation_id": "correlation-1",
                            "causation_id": None,
                            "cancel_requested_at": None,
                            **row_update,
                        }

                class _Approvals:
                    @staticmethod
                    def expire_stale(*_args):
                        trace.append("approval_touched")

                class _Uow:
                    runs = _Runs()
                    approvals = _Approvals()

                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False

                class _Repo:
                    @staticmethod
                    def unit_of_work():
                        return _Uow()

                service = ApprovalService(_Repo(), _Policy())
                with self.assertRaisesRegex(
                    InvalidStateError,
                    expected_message,
                ):
                    service.request(
                        run={"run_id": "run-1"},
                        plan=SimpleNamespace(plan_hash="plan-hash", impact={}),
                        policy_decision=decision,
                        requested_by=actor,
                    )
                self.assertEqual(["run_lock"], trace)

    def test_stale_plan_invalidation_locks_run_first_and_advances_feishu_queue(self):
        trace: list[str] = []
        outboxes: list[tuple[dict, tuple[dict, ...]]] = []

        class _Runs:
            @staticmethod
            def get(_run_id, *, for_update):
                trace.append("run_lock" if for_update else "run_read")
                return {
                    "run_id": "run-1",
                    "correlation_id": "correlation-1",
                    "causation_id": None,
                }

        class _Approvals:
            @staticmethod
            def get_latest_for_run(_run_id, *, for_update):
                trace.append("approval_lock" if for_update else "approval_read")
                return {
                    "approval_id": "approval-1",
                    "work_item_id": "work-1",
                    "run_id": "run-1",
                    "plan_hash": "plan-hash",
                    "status": "APPROVED",
                }

            @staticmethod
            def invalidate_pending(*, run_id):
                assert run_id == "run-1"
                trace.append("invalidate")
                return 1

        class _Events:
            @staticmethod
            def append_with_outbox(event, outbox):
                trace.append("event")
                outboxes.append((dict(event), tuple(outbox)))

        class _Uow:
            runs = _Runs()
            approvals = _Approvals()
            events = _Events()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def commit():
                trace.append("commit")

        class _Repo:
            @staticmethod
            def unit_of_work():
                return _Uow()

        ApprovalService(_Repo(), _Policy()).invalidate_for_stale_plan("run-1")

        self.assertEqual(
            ["run_lock", "approval_lock", "invalidate", "event", "commit"],
            trace,
        )
        event, outbox = outboxes[0]
        self.assertEqual("agent.approval.invalidated", event["event_type"])
        self.assertTrue(
            any(item["consumer_name"] == "feishu.approval" for item in outbox)
        )


if __name__ == "__main__":
    unittest.main()
