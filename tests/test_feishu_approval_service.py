from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent.orchestration.feishu_approval_service import FeishuApprovalService
from agent.orchestration.models import OrchestrationError
from shared.feishu_approval_repository import FeishuApprovalRepository


class _Rows:
    def __init__(self) -> None:
        self.admin = {
            "id": 7,
            "username": "root-admin",
            "display_name": "Root Admin",
            "is_active": 1,
            "control_plane_role": "super_admin",
        }
        self.challenges: dict[str, dict] = {}
        self.binding: dict | None = None
        self.failure: dict | None = None
        self.active: dict | None = None
        self.next_active: dict | None = None
        self.finished: list[tuple[str, str]] = []
        self.finished_bindings: list[tuple[str, str, str]] = []
        self.notified: list[str] = []
        self.sent: list[tuple[str, str, str]] = []

    def get_admin_user(self, admin_user_id, **_kwargs):
        return dict(self.admin) if admin_user_id == 7 else None

    def create_challenge(self, row):
        challenge = {**row, "used_at": None, **self.admin}
        self.challenges[row["code_sha256"]] = challenge
        return dict(challenge)

    def get_challenge_by_digest(self, digest, **_kwargs):
        challenge = self.challenges.get(digest)
        return dict(challenge) if challenge else None

    def failure_state(self, _open_id, **_kwargs):
        return dict(self.failure) if self.failure else None

    def record_failure(self, open_id, **_kwargs):
        self.failure = {"open_id": open_id, "failed_attempts": 1}
        return dict(self.failure)

    def consume_challenge(self, *, challenge_id, admin_user_id, binding_id, open_id, chat_id):
        target = next(
            row for row in self.challenges.values() if row["challenge_id"] == challenge_id
        )
        target["used_at"] = datetime.now()
        self.binding = {
            "binding_id": binding_id,
            "admin_user_id": admin_user_id,
            "open_id": open_id,
            "last_chat_id": chat_id,
            "active": 1,
            **self.admin,
        }
        return dict(self.binding)

    def resolve_binding(self, open_id, **_kwargs):
        if not self.binding or self.binding["open_id"] != open_id:
            return None
        return {**self.binding, **self.admin}

    def get_binding_for_admin(self, admin_user_id):
        if not self.binding or self.binding["admin_user_id"] != admin_user_id:
            return None
        return {
            **self.binding,
            "notifications_enabled": 1,
            "bound_at": datetime.now(),
            "updated_at": datetime.now(),
        }

    def active_for_open_id(self, open_id, **_kwargs):
        if not self.resolve_binding(open_id):
            return None
        if not self.active:
            return None
        active = dict(self.active)
        active.setdefault("binding_id", str(self.binding["binding_id"]))
        return active

    def finish_approval(self, approval_id, *, status="DECIDED"):
        self.finished.append((str(approval_id), str(status)))
        self.active = self.next_active
        self.next_active = None
        if self.binding is None:
            return []
        return [str(self.binding["binding_id"])]

    def finish_active_for_binding(self, binding_id, approval_id, *, status="DECIDED"):
        self.finished_bindings.append(
            (str(binding_id), str(approval_id), str(status))
        )
        return self.finish_approval(approval_id, status=status)

    def expire_approval_if_due(self, approval_id):
        if self.active and str(self.active.get("approval_id")) == str(approval_id):
            self.active["approval_status"] = "EXPIRED"
            return True
        return False

    def enqueue_for_enabled_admins(self, _approval_id, _plan_hash):
        if self.binding is None:
            return []
        return [str(self.binding["binding_id"])]

    def active_for_binding(self, binding_id, **_kwargs):
        if self.binding is None or str(self.binding["binding_id"]) != str(binding_id):
            return None
        if not self.active:
            return None
        active = dict(self.active)
        active.setdefault("binding_id", str(binding_id))
        return active

    def mark_notified(self, delivery_id):
        self.notified.append(str(delivery_id))
        if self.active and str(self.active.get("delivery_id")) == str(delivery_id):
            self.active["notified_at"] = datetime.now()


class _Uow:
    def __init__(self, rows):
        self.feishu_approvals = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self):
        return None


class _Repository:
    def __init__(self):
        self.rows = _Rows()

    def unit_of_work(self):
        return _Uow(self.rows)


class _Approvals:
    def __init__(self):
        self.decisions: list[dict] = []
        self.error_code: str | None = None

    def decide(self, **kwargs):
        if self.error_code:
            raise OrchestrationError(self.error_code, "approval decision raced")
        self.decisions.append(dict(kwargs))
        return {"status": kwargs["decision"]}


def _service():
    repository = _Repository()
    approvals = _Approvals()

    def _send_text(receive_id, text, kind):
        repository.rows.sent.append((str(receive_id), str(text), str(kind)))
        return True

    return (
        FeishuApprovalService(
            repository,
            approvals,
            send_text=_send_text,
        ),
        repository,
        approvals,
    )


def test_approval_message_includes_only_the_sanitized_approval_projection():
    message = FeishuApprovalService._approval_message(
        {
            "automation_id": "arrival_stats",
            "tool_names": "arrival.scan, arrival.publish",
            "source": "scheduler",
            "risk_level": "HIGH",
            "plan_hash": "a" * 64,
            "expires_at": datetime(2026, 8, 22, 12, 30),
            "plan_json": {"secret": "must-not-appear"},
            "impact_json": {"customer": "must-not-appear"},
        }
    )

    assert "项目：arrival_stats" in message
    assert "工具：arrival.scan, arrival.publish" in message
    assert "来源：scheduler" in message
    assert "风险：HIGH" in message
    assert "影响摘要：执行已签名动作，并要求写后证据核验" in message
    assert "Plan：aaaaaaaaaaaa" in message
    assert "过期：2026-08-22 12:30" in message
    assert "must-not-appear" not in message


def test_one_time_binding_and_live_super_admin_role():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    assert service.handle_text("ou-1", "oc-1", challenge["command"]) == (
        "审批身份绑定成功；账号权限变更或解绑会立即生效。"
    )
    assert service.resolve_actor("ou-1").authenticated_by == "feishu_admin_binding"
    assert service.handle_text("ou-2", "oc-2", challenge["command"]).startswith("绑定码无效")
    repository.rows.admin["control_plane_role"] = "admin"
    assert service.resolve_actor("ou-1").roles == ()


def test_console_binding_status_does_not_expose_feishu_identifiers():
    service, _repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-private", "oc-private", challenge["command"])

    status = service.binding_status(7)

    assert status["bound"] is True
    assert "open_id" not in status["binding"]
    assert "last_chat_id" not in status["binding"]


def test_exact_one_decides_only_the_active_bound_approval():
    service, repository, approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "approval_id": "approval-1",
        "plan_hash": "a" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=5),
    }
    assert service.handle_text("ou-1", "oc-1", "hello") is None
    assert service.handle_text("ou-1", "oc-1", "1") == "已批准，原事项已恢复执行。"
    assert approvals.decisions[0]["decision"] == "APPROVED"
    assert approvals.decisions[0]["source"] == "feishu"


def test_downgraded_feishu_actor_is_forbidden_before_a_decision_is_recorded():
    service, repository, approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "approval_id": "approval-1",
        "plan_hash": "a" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=5),
    }
    # This represents the binding/admin recheck performed in ApprovalService's
    # Run -> Approval -> binding transaction after the message was accepted.
    approvals.error_code = "APPROVAL_FORBIDDEN"

    with pytest.raises(OrchestrationError) as raised:
        service.handle_text("ou-1", "oc-1", "1")

    assert raised.value.code == "APPROVAL_FORBIDDEN"
    assert approvals.decisions == []


def test_activate_next_locks_binding_before_any_delivery_row():
    trace: list[str] = []

    class _Cursor:
        description = None
        rowcount = 0

        def __init__(self):
            self.row = None

        def execute(self, sql, _params=None):
            if "FROM feishu_admin_bindings WHERE binding_id=" in sql:
                trace.append("binding")
                self.row = {"binding_id": "binding-1"}
            elif "WHERE binding_id=%s AND status='ACTIVE' FOR UPDATE" in sql:
                trace.append("active_delivery")
                self.row = None
            elif "WHERE binding_id=%s AND status='QUEUED'" in sql:
                trace.append("queued_delivery")
                self.row = {"delivery_id": "delivery-queued"}
            elif "SET status='ACTIVE'" in sql:
                trace.append("activate")
                self.row = None
            elif "WHERE delivery.binding_id=%s AND delivery.status='ACTIVE'" in sql:
                trace.append("read_active")
                self.row = None
            else:
                self.row = None

        def fetchone(self):
            return self.row

        def fetchall(self):
            return []

        def close(self):
            return None

    class _Connection:
        def __init__(self):
            self.cursor_value = _Cursor()

        def cursor(self, *_args):
            return self.cursor_value

    FeishuApprovalRepository(_Connection()).activate_next("binding-1")

    assert len(trace) >= 4
    assert trace[0] == "binding"
    assert trace[1] == "active_delivery"
    assert trace[2] == "queued_delivery"
    assert trace[3] == "activate"


def test_stale_active_reply_pushes_and_marks_the_next_serial_approval():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "approval_id": "approval-stale",
        "plan_hash": "a" * 64,
        "approval_status": "APPROVED",
        "expires_at": datetime.now() + timedelta(minutes=5),
    }
    repository.rows.next_active = {
        "delivery_id": "delivery-next",
        "approval_id": "approval-next",
        "plan_hash": "b" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrival_stats",
        "source": "scheduler",
        "risk_level": "HIGH",
        "notified_at": None,
    }

    assert service.handle_text("ou-1", "oc-1", "1") == (
        "当前审批已过期或已由其他管理员处理，已切换到下一条。"
    )
    assert repository.rows.finished == [("approval-stale", "SKIPPED")]
    assert repository.rows.finished_bindings == [
        (str(repository.rows.binding["binding_id"]), "approval-stale", "SKIPPED")
    ]
    assert repository.rows.notified == ["delivery-next"]
    assert repository.rows.sent[0][0] == "ou-1"
    assert "项目：arrival_stats" in repository.rows.sent[0][1]
    assert repository.rows.active["notified_at"] is not None


def test_new_request_outbox_skips_stale_active_and_pushes_next_without_reply():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "delivery_id": "delivery-stale",
        "approval_id": "approval-stale",
        "plan_hash": "a" * 64,
        "approval_status": "INVALIDATED",
        "expires_at": datetime.now() + timedelta(minutes=5),
        "open_id": "ou-1",
        "notified_at": datetime.now(),
    }
    repository.rows.next_active = {
        "delivery_id": "delivery-next",
        "approval_id": "approval-next",
        "plan_hash": "b" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrival_stats",
        "source": "scheduler",
        "risk_level": "HIGH",
        "notified_at": None,
    }
    delivery = {
        "topic": "agent.approval.requested",
        "entity_id": "approval-next",
        "payload_json": {"plan_hash": "b" * 64},
    }

    result = service.handle_outbox(delivery, _Uow(repository.rows))

    assert result == {"approval_id": "approval-next", "sent": 1}
    assert repository.rows.finished == [("approval-stale", "SKIPPED")]
    assert repository.rows.finished_bindings == [
        (str(repository.rows.binding["binding_id"]), "approval-stale", "SKIPPED")
    ]
    assert repository.rows.notified == ["delivery-next"]
    assert "项目：arrival_stats" in repository.rows.sent[0][1]


def test_invalidation_outbox_completes_active_and_pushes_next_without_reply():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "delivery_id": "delivery-invalidated",
        "approval_id": "approval-invalidated",
        "plan_hash": "a" * 64,
        "approval_status": "INVALIDATED",
        "expires_at": datetime.now() + timedelta(minutes=5),
        "open_id": "ou-1",
        "notified_at": datetime.now(),
    }
    repository.rows.next_active = {
        "delivery_id": "delivery-next",
        "approval_id": "approval-next",
        "plan_hash": "b" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrive_list",
        "source": "scheduler",
        "risk_level": "HIGH",
        "notified_at": None,
    }

    result = service.handle_outbox(
        {
            "topic": "agent.approval.invalidated",
            "entity_id": "approval-invalidated",
            "payload_json": {"plan_hash": "a" * 64},
        },
        _Uow(repository.rows),
    )

    assert result == {"approval_id": "approval-invalidated", "sent": 1}
    assert repository.rows.finished == [("approval-invalidated", "SKIPPED")]
    assert repository.rows.notified == ["delivery-next"]
    assert "项目：arrive_list" in repository.rows.sent[0][1]


@pytest.mark.parametrize(
    "error_code, expected_status",
    (
        ("APPROVAL_NOT_PENDING", "SKIPPED"),
        ("APPROVAL_EXPIRED", "EXPIRED"),
        ("PLAN_STALE", "SKIPPED"),
    ),
)
def test_decision_race_pushes_and_marks_the_next_serial_approval(
    error_code,
    expected_status,
):
    service, repository, approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "approval_id": "approval-raced",
        "plan_hash": "c" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=5),
    }
    repository.rows.next_active = {
        "delivery_id": "delivery-after-race",
        "approval_id": "approval-next",
        "plan_hash": "d" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrive_list",
        "source": "feishu",
        "risk_level": "EXTREME",
        "notified_at": None,
    }
    approvals.error_code = error_code

    assert service.handle_text("ou-1", "oc-1", "1") == (
        "当前审批已过期或已由其他管理员处理，已切换到下一条。"
    )
    assert repository.rows.finished == [("approval-raced", expected_status)]
    assert repository.rows.notified == ["delivery-after-race"]
    assert repository.rows.sent[0][0] == "ou-1"
    assert "项目：arrive_list" in repository.rows.sent[0][1]
    assert repository.rows.active["notified_at"] is not None
