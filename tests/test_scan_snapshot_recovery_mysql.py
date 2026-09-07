"""Real MySQL CAS evidence: old scan recovery cannot overwrite newer facts."""
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from shared.scan_snapshot_recovery import (
    SNAPSHOT_UPSERT_SQL, lock_snapshot_head, record_snapshot_head,
    restore_owned_snapshot, scan_snapshot_write_identity,
)
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


@pytest.mark.parametrize('round_number', range(20))
def test_original_scan_projection_restore_is_idempotent_and_rejects_newer_owner(repository, round_number):
    target_date = (date(2026, 1, 1) + timedelta(days=round_number)).isoformat()
    original = {'raw_code': 'R123456789010001', 'destination': '原始站点', 'code_type': 'child', 'main_tracking': 'R12345678901'}
    newer = {**original, 'destination': '已核验的新站点'}
    identity = {'orchestration_run_id': str(uuid4()), 'lease_id': str(uuid4())}
    payload = {'target_date': target_date, 'records': [original]}
    with repository.unit_of_work() as uow, uow.connection.cursor() as cursor:
        lock_snapshot_head(cursor, target_date)
        with scan_snapshot_write_identity(identity):
            record_snapshot_head(cursor, target_date, [original])
        first = restore_owned_snapshot(cursor, payload, run_id=identity['orchestration_run_id'], lease_id=identity['lease_id'])
        replay = restore_owned_snapshot(cursor, payload, run_id=identity['orchestration_run_id'], lease_id=identity['lease_id'])
        assert first['restored'] is True and replay['restored'] is False
        cursor.execute('DELETE FROM scan_codes WHERE snapshot_date=%s', (target_date,))
        cursor.execute(SNAPSHOT_UPSERT_SQL, tuple(newer.values()) + (target_date, target_date))
        with scan_snapshot_write_identity({'orchestration_run_id': str(uuid4()), 'lease_id': str(uuid4())}):
            record_snapshot_head(cursor, target_date, [newer])
        with pytest.raises(ValueError, match='SCAN_PROJECTION_SUPERSEDED'):
            restore_owned_snapshot(cursor, payload, run_id=identity['orchestration_run_id'], lease_id=identity['lease_id'])
        cursor.execute('SELECT destination FROM scan_codes WHERE snapshot_date=%s', (target_date,))
        assert cursor.fetchone()['destination'] == newer['destination']
        # If a newer publisher already stored exactly these original facts,
        # recovery proves the existing projection without modifying its owner.
        cursor.execute('UPDATE scan_codes SET destination=%s WHERE snapshot_date=%s', (original['destination'], target_date))
        current = restore_owned_snapshot(cursor, payload, run_id=identity['orchestration_run_id'], lease_id=identity['lease_id'])
        assert current['restored'] is False and current['snapshot_sha256'] == first['snapshot_sha256']
        uow.commit()
