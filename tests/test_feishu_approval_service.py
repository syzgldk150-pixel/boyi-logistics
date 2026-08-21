from __future__ import annotations

from datetime import datetime, timedelta

from agent.orchestration.feishu_approval_service import FeishuApprovalService


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
        return dict(self.active) if self.active else None

    def finish_approval(self, _approval_id, **_kwargs):
        self.active = None
        return []


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

    def decide(self, **kwargs):
        self.decisions.append(dict(kwargs))
        return {"status": kwargs["decision"]}


def _service():
    repository = _Repository()
    approvals = _Approvals()
    return (
        FeishuApprovalService(
            repository,
            approvals,
            send_text=lambda _receive_id, _text, _kind: True,
        ),
        repository,
        approvals,
    )


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
