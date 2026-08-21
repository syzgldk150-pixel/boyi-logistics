from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "agent" / "migrations" / "020_automation_full_auto_feishu_approvals.sql"


def test_migration_019_has_binding_queue_and_durable_full_auto_conversion():
    sql = MIGRATION.read_text(encoding="utf-8")
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
