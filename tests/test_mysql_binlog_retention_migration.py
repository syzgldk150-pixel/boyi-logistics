from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "migrations"
    / "032_mysql_binlog_retention.sql"
)


def test_binlog_retention_uses_persistent_mysql_policy_and_managed_purge():
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())

    assert "SET PERSIST binlog_expire_logs_seconds = 2592000" in sql
    assert "PURGE BINARY LOGS BEFORE DATE_SUB(NOW(6), INTERVAL 30 DAY)" in sql
    assert "DELETE" not in sql
    assert "rm " not in sql
