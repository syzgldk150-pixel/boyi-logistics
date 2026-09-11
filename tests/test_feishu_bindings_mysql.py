"""Binding lifecycle against an isolated database, including non-UTC sessions."""

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agent.orchestration.feishu_approval_service import FeishuApprovalService
from shared.orchestration_repository import OrchestrationRepository
from tests.test_manual_unknown_write_mysql import database as database


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8",
)


@pytest.fixture(params=("+08:00", "+00:00"))
def binding_repository(database, request):
    def connect():
        connection = database.pymysql.connect(
            host=database.host, port=database.port, user=database.user,
            password=database.password, database=database.database, charset="utf8mb4",
            cursorclass=database.pymysql.cursors.DictCursor, autocommit=False,
        )
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION time_zone=%s", (request.param,))
        return connection

    return OrchestrationRepository(connect, database.pymysql.cursors.DictCursor)


def seed_admin(repository):
    with repository.unit_of_work() as uow, uow.feishu_approvals.cursor() as cursor:
        cursor.execute(
            "INSERT INTO admin_users (username, password_hash, display_name, is_active, role, control_plane_role, created_at, updated_at) "
            "VALUES (%s, 'synthetic-not-a-login', 'Binding test', 1, 'super_admin', 'super_admin', NOW(6), NOW(6))",
            (f"binding-{uuid4().hex}",),
        )
        identity = cursor.lastrowid
        uow.commit()
    return identity


def test_fresh_binding_is_accepted_with_either_database_timezone(binding_repository):
    repository = binding_repository
    admin_id = seed_admin(repository)
    service = FeishuApprovalService(repository, None, send_text=lambda *_: True)
    challenge = service.create_binding_challenge(admin_id)
    sender = f"ou-synthetic-{uuid4().hex}"
    assert "绑定成功" in service.handle_binding_text(sender, "test-chat", challenge["command"])
    assert service.resolve_actor(sender).roles == ("admin", "super_admin")
    assert "已使用" in service.handle_binding_text(sender, "test-chat", challenge["command"])


def test_expired_binding_is_not_reported_as_another_administrator(binding_repository):
    repository = binding_repository
    admin_id = seed_admin(repository)
    service = FeishuApprovalService(repository, None, send_text=lambda *_: True)
    challenge = service.create_binding_challenge(admin_id)
    with repository.unit_of_work() as uow, uow.feishu_approvals.cursor() as cursor:
        cursor.execute("UPDATE feishu_admin_binding_challenges SET expires_at=%s WHERE challenge_id=%s",
                       (datetime(2000, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None), challenge["challenge_id"]))
        uow.commit()
    assert "已过期" in service.handle_binding_text("ou-expired", "test-chat", challenge["command"])
