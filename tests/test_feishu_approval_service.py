from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

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
        self.leased_terminal: dict | None = None
        self.finished: list[tuple[str, str]] = []
        self.finished_bindings: list[tuple[str, str, str]] = []
        self.notified: list[str] = []
        self.reserved: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str, str]] = []
        self.queue_advances = 0

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
        if self.active and str(self.active.get("approval_id")) == str(approval_id):
            lease_token = self.active.get("notification_lease_token")
            lease_expiry = self.active.get("notification_lease_expires_at")
            if lease_token and (
                not isinstance(lease_expiry, datetime)
                or lease_expiry > datetime.now()
            ):
                self.leased_terminal = self.active
                self.active = None
            else:
                self.active = self.next_active
                self.next_active = None
                self.queue_advances += 1
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

    def notification_lease_for_binding(self, binding_id):
        if self.binding is None or str(self.binding["binding_id"]) != str(binding_id):
            return None
        for row in (self.active, self.leased_terminal):
            if not row:
                continue
            token = row.get("notification_lease_token")
            expiry = row.get("notification_lease_expires_at")
            if token and isinstance(expiry, datetime) and expiry > datetime.now():
                return {
                    "delivery_id": row.get("delivery_id"),
                    "notification_lease_token": token,
                    "notification_lease_expires_at": expiry,
                }
        return None

    def activate_next(self, binding_id):
        if self.binding is None or str(self.binding["binding_id"]) != str(binding_id):
            return None
        if self.leased_terminal is not None:
            expiry = self.leased_terminal.get("notification_lease_expires_at")
            if isinstance(expiry, datetime) and expiry > datetime.now():
                return None
            self.leased_terminal["notification_lease_token"] = None
            self.leased_terminal["notification_lease_expires_at"] = None
            self.leased_terminal = None
        if self.active is None and self.next_active is not None:
            self.active = self.next_active
            self.next_active = None
            self.queue_advances += 1
        return self.active_for_binding(binding_id)

    def reserve_notification(
        self,
        binding_id,
        delivery_id,
        lease_token,
        *,
        lease_seconds,
    ):
        if self.binding is None or str(self.binding["binding_id"]) != str(binding_id):
            return False
        if not self.active or str(self.active.get("delivery_id")) != str(delivery_id):
            return False
        if self.active.get("notified_at") is not None:
            return False
        now = datetime.now()
        current_token = self.active.get("notification_lease_token")
        current_expiry = self.active.get("notification_lease_expires_at")
        if current_token and (
            not isinstance(current_expiry, datetime) or current_expiry > now
        ):
            return False
        self.active["notification_lease_token"] = str(lease_token)
        self.active["notification_lease_expires_at"] = now + timedelta(
            seconds=int(lease_seconds)
        )
        self.reserved.append((str(delivery_id), str(lease_token)))
        return True

    def finalize_notification(self, binding_id, delivery_id, lease_token):
        row = next(
            (
                item
                for item in (self.active, self.leased_terminal)
                if item and str(item.get("delivery_id")) == str(delivery_id)
            ),
            None,
        )
        if row is None:
            return False
        if str(row.get("notification_lease_token") or "") != str(lease_token):
            return False
        if row.get("notified_at") is not None:
            return False
        row["notified_at"] = datetime.now()
        row["notification_lease_token"] = None
        row["notification_lease_expires_at"] = None
        self.notified.append(str(delivery_id))
        if row is self.leased_terminal:
            self.leased_terminal = None
        self.activate_next(binding_id)
        return True

    def release_notification(
        self,
        binding_id,
        delivery_id,
        lease_token,
        *,
        error_summary,
    ):
        row = next(
            (
                item
                for item in (self.active, self.leased_terminal)
                if item and str(item.get("delivery_id")) == str(delivery_id)
            ),
            None,
        )
        if row is None:
            return False
        if str(row.get("notification_lease_token") or "") != str(lease_token):
            return False
        row["notification_lease_token"] = None
        row["notification_lease_expires_at"] = None
        row["last_error_summary"] = str(error_summary)
        self.released.append((str(delivery_id), str(lease_token)))
        if row is self.leased_terminal:
            self.leased_terminal = None
        self.activate_next(binding_id)
        return True


class _Events:
    def __init__(self) -> None:
        self.appended: list[tuple[dict, tuple[dict, ...]]] = []

    def append_with_outbox(self, event, outbox):
        event_copy = dict(event)
        outbox_copy = tuple(dict(item) for item in outbox)
        self.appended.append((event_copy, outbox_copy))
        return {"event": event_copy, "outbox": list(outbox_copy)}


class _Uow:
    def __init__(self, repository):
        self._repository = repository
        self.feishu_approvals = repository.rows
        self.events = repository.events

    def __enter__(self):
        self._repository.uow_depth += 1
        return self

    def __exit__(self, *_args):
        self._repository.uow_depth -= 1
        return False

    def commit(self):
        return None


class _Repository:
    def __init__(self):
        self.rows = _Rows()
        self.events = _Events()
        self.uow_depth = 0
        self.send_results: list[bool] = []
        self.send_error: Exception | None = None

    def unit_of_work(self):
        return _Uow(self)


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
        assert repository.uow_depth == 0
        repository.rows.sent.append((str(receive_id), str(text), str(kind)))
        if repository.send_error is not None:
            raise repository.send_error
        return repository.send_results.pop(0) if repository.send_results else True

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
            elif "notification_lease_token IS NOT NULL" in sql and "SELECT" in sql:
                trace.append("live_lease")
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

    assert len(trace) >= 5
    assert trace[0] == "binding"
    assert trace[1] == "active_delivery"
    assert trace[2] == "live_lease"
    assert trace[3] == "queued_delivery"
    assert trace[4] == "activate"


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


def test_stale_reply_persists_retry_before_attempting_the_outside_send(monkeypatch):
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "delivery_id": "delivery-stale",
        "approval_id": "approval-stale",
        "plan_hash": "a" * 64,
        "approval_status": "APPROVED",
        "expires_at": datetime.now() + timedelta(minutes=5),
        "open_id": "ou-1",
        "notified_at": datetime.now(),
    }
    repository.rows.next_active = {
        "delivery_id": "delivery-recovered",
        "approval_id": "approval-recovered",
        "plan_hash": "b" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrival_stats",
        "source": "scheduler",
        "risk_level": "HIGH",
        "notified_at": None,
    }
    notify = service._notify_active_bindings

    def crash_before_send(*_args, **_kwargs):
        raise RuntimeError("simulated process exit after commit")

    monkeypatch.setattr(service, "_notify_active_bindings", crash_before_send)
    with pytest.raises(RuntimeError, match="simulated process exit"):
        service.handle_text("ou-1", "oc-1", "1")

    assert len(repository.events.appended) == 1
    retry_event, retry_outbox = repository.events.appended[0]
    assert retry_event["event_type"] == "agent.feishu.notification.retry"
    assert retry_event["payload"]["binding_ids"] == [
        str(repository.rows.binding["binding_id"])
    ]
    assert retry_outbox[0]["consumer_name"] == "feishu.approval"

    monkeypatch.setattr(service, "_notify_active_bindings", notify)
    result = service.handle_outbox(
        {
            "topic": retry_outbox[0]["topic"],
            "entity_id": retry_event["entity_id"],
            "payload_json": retry_event["payload"],
        },
        None,
    )

    assert result == {"approval_id": "approval-stale", "sent": 1}
    assert repository.rows.queue_advances == 1
    assert repository.rows.notified == ["delivery-recovered"]


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

    result = service.handle_outbox(delivery, None)

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
        None,
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


def test_failed_send_releases_lease_and_retry_finalizes_without_uow_network():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "delivery_id": "delivery-retry",
        "approval_id": "approval-retry",
        "plan_hash": "e" * 64,
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
        "entity_id": "approval-retry",
        "payload_json": {"plan_hash": "e" * 64},
    }
    repository.send_results = [False, True]

    with pytest.raises(RuntimeError, match="notification failed"):
        service.handle_outbox(delivery, None)

    assert len(repository.rows.reserved) == 1
    assert len(repository.rows.released) == 1
    assert repository.rows.active["notification_lease_token"] is None
    assert repository.rows.active["notified_at"] is None

    assert service.handle_outbox(delivery, None) == {
        "approval_id": "approval-retry",
        "sent": 1,
    }
    assert len(repository.rows.reserved) == 2
    assert repository.rows.reserved[0][1] != repository.rows.reserved[1][1]
    assert repository.rows.notified == ["delivery-retry"]
    assert repository.rows.active["notified_at"] is not None
    assert repository.uow_depth == 0


def test_live_notification_lease_is_retried_after_expiry_without_queue_advance():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "delivery_id": "delivery-leased",
        "approval_id": "approval-leased",
        "plan_hash": "f" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrive_list",
        "source": "scheduler",
        "risk_level": "HIGH",
        "notified_at": None,
        "notification_lease_token": "other-worker",
        "notification_lease_expires_at": datetime.now() + timedelta(minutes=5),
    }
    delivery = {
        "topic": "agent.approval.requested",
        "entity_id": "approval-leased",
        "payload_json": {"plan_hash": "f" * 64},
    }

    with pytest.raises(RuntimeError, match="already leased"):
        service.handle_outbox(delivery, None)

    assert repository.rows.sent == []
    assert repository.rows.queue_advances == 0
    repository.rows.active["notification_lease_expires_at"] = (
        datetime.now() - timedelta(seconds=1)
    )

    assert service.handle_outbox(delivery, None)["sent"] == 1
    assert repository.rows.notified == ["delivery-leased"]
    assert repository.rows.queue_advances == 0


def test_terminal_inflight_delivery_keeps_the_next_queue_item_inactive():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    binding_id = str(repository.rows.binding["binding_id"])
    repository.rows.active = {
        "delivery_id": "delivery-inflight",
        "approval_id": "approval-inflight",
        "plan_hash": "a" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrival_stats",
        "source": "scheduler",
        "risk_level": "HIGH",
        "notified_at": None,
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
    reservation = service._reserve_active_notification(binding_id)
    assert reservation["state"] == "RESERVED"

    invalidated = {
        "topic": "agent.approval.invalidated",
        "entity_id": "approval-inflight",
        "payload_json": {"plan_hash": "a" * 64},
    }
    with pytest.raises(RuntimeError, match="already leased"):
        service.handle_outbox(invalidated, None)

    assert repository.rows.active is None
    assert repository.rows.next_active["approval_id"] == "approval-next"
    assert repository.rows.queue_advances == 0
    assert repository.rows.sent == []

    assert service._finalize_notification(
        binding_id,
        "delivery-inflight",
        str(reservation["notification_lease_token"]),
    ) is True
    assert repository.rows.active["approval_id"] == "approval-next"
    assert repository.rows.queue_advances == 1

    assert service.handle_outbox(invalidated, None)["sent"] == 1
    assert repository.rows.queue_advances == 1
    assert repository.rows.notified == ["delivery-inflight", "delivery-next"]


def test_retry_after_invalidation_send_failure_does_not_advance_queue_twice():
    service, repository, _approvals = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_text("ou-1", "oc-1", challenge["command"])
    repository.rows.active = {
        "delivery_id": "delivery-old",
        "approval_id": "approval-old",
        "plan_hash": "1" * 64,
        "approval_status": "INVALIDATED",
        "expires_at": datetime.now() + timedelta(minutes=5),
        "open_id": "ou-1",
        "notified_at": datetime.now(),
    }
    repository.rows.next_active = {
        "delivery_id": "delivery-next",
        "approval_id": "approval-next",
        "plan_hash": "2" * 64,
        "approval_status": "PENDING",
        "expires_at": datetime.now() + timedelta(minutes=10),
        "open_id": "ou-1",
        "automation_id": "arrive_list",
        "source": "scheduler",
        "risk_level": "HIGH",
        "notified_at": None,
    }
    delivery = {
        "topic": "agent.approval.invalidated",
        "entity_id": "approval-old",
        "payload_json": {"plan_hash": "1" * 64},
    }
    repository.send_results = [False, True]

    with pytest.raises(RuntimeError, match="notification failed"):
        service.handle_outbox(delivery, None)
    assert repository.rows.queue_advances == 1
    assert repository.rows.active["approval_id"] == "approval-next"

    assert service.handle_outbox(delivery, None)["sent"] == 1
    assert repository.rows.queue_advances == 1
    assert repository.rows.active["approval_id"] == "approval-next"
    assert repository.rows.notified == ["delivery-next"]


def test_notification_repository_reserve_and_finalize_use_token_cas():
    calls: list[tuple[str, tuple | None]] = []

    class _Cursor:
        description = None
        rowcount = 1

        def execute(self, sql, params=None):
            calls.append((" ".join(str(sql).split()), params))
            self.rowcount = 1

        def fetchone(self):
            return None

        def close(self):
            return None

    class _Connection:
        def cursor(self, *_args):
            return _Cursor()

    repository = FeishuApprovalRepository(_Connection())
    repository._lock_binding = lambda binding_id: {"binding_id": binding_id}
    repository.activate_next = lambda _binding_id: None

    assert repository.reserve_notification(
        "binding-1",
        "delivery-1",
        "token-1",
        lease_seconds=120,
    ) is True
    assert repository.finalize_notification(
        "binding-1",
        "delivery-1",
        "token-1",
    ) is True
    assert repository.release_notification(
        "binding-2",
        "delivery-2",
        "token-2",
        error_summary="failed",
    ) is True

    reserve_sql, reserve_params = calls[0]
    assert "notification_lease_expires_at<=NOW(6)" in reserve_sql
    assert "status='ACTIVE'" in reserve_sql
    assert "notified_at IS NULL" in reserve_sql
    assert reserve_params == ("token-1", 120, "delivery-1", "binding-1")
    finalize_sql, finalize_params = calls[1]
    assert "notification_lease_token=%s" in finalize_sql
    assert "notified_at IS NULL" in finalize_sql
    assert finalize_params == ("delivery-1", "binding-1", "token-1")
    release_sql, release_params = calls[2]
    assert "notification_lease_token=NULL" in release_sql
    assert "notification_lease_token=%s" in release_sql
    assert release_params == ("failed", "delivery-2", "binding-2", "token-2")


def test_notification_lease_migration_is_resumable_and_guards_the_binding_lane():
    migration = (
        Path(__file__).resolve().parents[1]
        / "agent"
        / "migrations"
        / "030_feishu_notification_lease.sql"
    ).read_text(encoding="utf-8")
    normalized = " ".join(migration.split())

    assert "ADD COLUMN notification_lease_token CHAR(36) NULL" in normalized
    assert "ADD COLUMN notification_lease_expires_at DATETIME(6) NULL" in normalized
    assert "column_name='notification_lease_token'" in normalized
    assert "column_name='notification_lease_expires_at'" in normalized
    assert "notification_lane_binding_id CHAR(36) GENERATED ALWAYS" in normalized
    assert "ADD UNIQUE INDEX uq_feishu_notification_lane_binding" in normalized
    assert "ADD INDEX idx_feishu_notification_lease" in normalized
    assert "ADD CONSTRAINT chk_feishu_notification_lease_pair" in normalized
    assert "notification_lease_token IS NULL OR notified_at IS NULL" in normalized
    assert "CREATE TABLE" not in normalized
