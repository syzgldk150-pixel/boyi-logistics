from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_020 = (
    ROOT / "agent" / "migrations" / "020_automation_full_auto_feishu_approvals.sql"
)
MIGRATION_021 = (
    ROOT / "agent" / "migrations" / "021_recover_full_auto_waiting_approvals.sql"
)


def test_migration_020_has_binding_queue_and_durable_full_auto_conversion():
    sql = MIGRATION_020.read_text(encoding="utf-8")
    for table in (
        "feishu_admin_binding_challenges",
        "feishu_admin_bindings",
        "feishu_binding_failures",
        "feishu_approval_deliveries",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "code_sha256" in sql
    assert "WHERE mode IN ('REQUIRE_EACH_RUN', 'LEGACY_SCHEDULE_ONLY')" in sql
    assert "SET mode='PROJECT_FULL_AUTO'" in sql
    assert "MIGRATION_019_FULL_AUTO" in sql
    assert "status='INVALIDATED'" in sql
    assert "SET run.next_attempt_at=NOW(6)" in sql


def test_migration_021_recovers_only_current_full_auto_typed_waiting_runs():
    sql = MIGRATION_021.read_text(encoding="utf-8")
    normalized = " ".join(sql.split())

    assert "UPDATE agent_runs AS run" in normalized
    assert "JOIN approval_requests AS approval" in normalized
    assert "JOIN agent_commands AS command" in normalized
    assert "JOIN automation_project_policies AS policy" in normalized
    assert "policy.automation_id = command.automation_id" in normalized
    assert "run.status='WAITING_APPROVAL'" in normalized
    assert "approval.status IN ('PENDING', 'APPROVED')" in normalized
    assert "command.command_type='automation.project.invoke'" in normalized
    assert "command.automation_invocation_json IS NOT NULL" in normalized
    assert "policy.mode='PROJECT_FULL_AUTO'" in normalized

    assert "approval.status='INVALIDATED'" in normalized
    assert "approval.decided_at=NOW(6)" in normalized
    assert "run.next_attempt_at=NOW(6)" in normalized
    assert "run.worker_id=NULL" in normalized
    assert "run.lease_expires_at=NULL" in normalized
    assert "run.version=run.version+1" in normalized


def test_migration_021_does_not_rewrite_project_policy():
    sql = MIGRATION_021.read_text(encoding="utf-8")

    assert "UPDATE automation_project_policies" not in sql
    assert "SET mode=" not in sql
