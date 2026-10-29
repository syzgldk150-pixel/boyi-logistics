"""Real MySQL CAS evidence: old scan recovery cannot overwrite newer facts."""
from pathlib import Path


from tests.test_workflow_runner_durable_admission import pytestmark, repository  # noqa: F401


def test_041_receipt_journals_and_scope_index_can_reapply(repository):
    from test_mysql_orchestration_integration import _load_migration_runner

    migration = Path(__file__).resolve().parents[1] / 'agent/migrations/041_scan_write_recovery_snapshot.sql'
    statements = _load_migration_runner().split_sql_statements(migration.read_text(encoding='utf-8'))
    with repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        for _ in range(2):
            for statement in statements:
                cursor.execute(statement)
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='automation_write_attempt_receipts' AND COLUMN_NAME IN ('scan_recovery_payload_json','execution_resource_keys_json')")
        assert {row['COLUMN_NAME'] for row in cursor.fetchall()} == {'scan_recovery_payload_json', 'execution_resource_keys_json'}
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.statistics WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='automation_write_attempt_receipts' AND INDEX_NAME='idx_write_attempt_unknown_scope' ORDER BY SEQ_IN_INDEX")
        assert [row['COLUMN_NAME'] for row in cursor.fetchall()] == ['outcome', 'receipt_id']
        uow.commit()

