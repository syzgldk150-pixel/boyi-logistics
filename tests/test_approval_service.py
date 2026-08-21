from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.policy_engine import PolicyEngine
from shared.orchestration_repository import InvalidStateError


class _Policy:
    @staticmethod
    def can_decide(_actor, *, required_role, source):
        return required_role == "admin" and source == "console"


class _ApprovalRows:
    def __init__(self, message: str) -> None:
        self.message = message

    def record_decision(self, _row, *, expected_plan_hash):
        del expected_plan_hash
        raise InvalidStateError(self.message)


class _UnitOfWork:
    def __init__(self, message: str) -> None:
        self.approvals = _ApprovalRows(message)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False


class _Repository:
    def __init__(self, message: str) -> None:
        self.message = message

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
        return _UnitOfWork(self.message)


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
        service = ApprovalService(
            _Repository("approval request is no longer pending"),
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

        self.assertEqual("APPROVAL_NOT_PENDING", raised.exception.code)

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


if __name__ == "__main__":
    unittest.main()
