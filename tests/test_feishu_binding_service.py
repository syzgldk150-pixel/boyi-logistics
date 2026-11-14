from __future__ import annotations

from datetime import datetime

from agent.orchestration.feishu_binding_service import FeishuBindingService


class _Rows:
    def __init__(self) -> None:
        self.admin = {
            "id": 7,
            "username": "root-admin",
            "display_name": "Root Admin",
            "is_active": 1,
            "control_plane_role": "super_admin",
            "person_id": "11111111-2222-4333-8444-555555555555",
        }
        self.challenges: dict[str, dict] = {}
        self.binding: dict | None = None
        self.failure: dict | None = None

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
        target = next(row for row in self.challenges.values() if row["challenge_id"] == challenge_id)
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
        return {**self.binding, "notifications_enabled": 1, "bound_at": datetime.now(), "updated_at": datetime.now()}

    def revoke(self, admin_user_id):
        if self.binding and self.binding["admin_user_id"] == admin_user_id:
            self.binding["active"] = 0


class _Uow:
    def __init__(self, rows: _Rows) -> None:
        self.feishu_bindings = rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def commit(self) -> None:
        return None


class _Repository:
    def __init__(self) -> None:
        self.rows = _Rows()

    def unit_of_work(self):
        return _Uow(self.rows)


def _service() -> tuple[FeishuBindingService, _Repository]:
    repository = _Repository()
    return FeishuBindingService(repository), repository


def test_one_time_binding_and_live_super_admin_role():
    service, repository = _service()
    challenge = service.create_binding_challenge(7)

    assert service.handle_binding_text("ou-1", "oc-1", challenge["command"]) == (
        "飞书身份绑定成功；身份权限变更或解绑会立即生效。"
    )
    assert service.resolve_actor("ou-1").authenticated_by == "feishu_admin_binding"
    assert service.handle_binding_text("ou-2", "oc-2", challenge["command"]).startswith("绑定码无效")
    repository.rows.admin["control_plane_role"] = "admin"
    assert service.resolve_actor("ou-1").roles == ()


def test_binding_ingress_ignores_old_approval_replies():
    service, _repository = _service()
    challenge = service.create_binding_challenge(7)
    assert service.handle_binding_text("ou-1", "oc-1", challenge["command"])

    assert service.handle_binding_text("ou-1", "oc-1", "1") is None
    assert service.handle_binding_text("ou-1", "oc-1", "2") is None


def test_console_binding_status_does_not_expose_feishu_identifiers():
    service, _repository = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_binding_text("ou-private", "oc-private", challenge["command"])

    status = service.binding_status(7)

    assert status["bound"] is True
    assert "open_id" not in status["binding"]
    assert "last_chat_id" not in status["binding"]


def test_revoked_binding_resolves_as_an_unbound_sender():
    service, _repository = _service()
    challenge = service.create_binding_challenge(7)
    service.handle_binding_text("ou-1", "oc-1", challenge["command"])

    service.revoke_binding(7)

    actor = service.resolve_actor("ou-1")
    assert actor.roles == ()
    assert actor.authenticated_by == "feishu_verified_event"
