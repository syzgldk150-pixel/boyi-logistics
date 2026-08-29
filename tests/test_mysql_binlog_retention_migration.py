from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "migrations"
    / "032_mysql_binlog_retention.sql"
)


def test_binlog_retention_migration_verifies_policy_without_admin_privilege():
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())

    assert "CHECK (configured_seconds = 2592000)" in sql
    assert "SELECT @@GLOBAL.binlog_expire_logs_seconds" in sql
    assert "SET PERSIST" not in sql
    assert "PURGE BINARY LOGS" not in sql
    assert "DELETE" not in sql
    assert "rm " not in sql
    assert "SYSTEM_VARIABLES_ADMIN" in sql
