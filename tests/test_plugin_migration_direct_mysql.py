"""Migration validation reads direct invocation facts from actual MySQL."""
from datetime import datetime, timedelta
import json
from uuid import uuid4

import pytest

from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_repository_support import ConcurrentUpdateError
from tests.test_customer_collection_business_mysql import _seed_generation
from tests.test_module_data_sources_mysql import database  # noqa: F401


def test_readiness_requires_fresh_success_and_exact_generation_not_an_old_run(database):
    fixture, name = database
    target, call_id, lease_id = "validation-" + uuid4().hex, str(uuid4()), str(uuid4())
    started = datetime(2026, 9, 11, 8, 0)
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        _seed_generation(connection, target)
        cursor.execute("""INSERT INTO automation_plugin_invocations
            (invocation_id,request_key_sha256,request_sha256,request_id,automation_id,generation,
             operation,source,actor_id,owner_id,status,invocation_json,arguments_json,result_json,started_at,finished_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,1,'validation','console','isolated-admin',%s,'COMPLETED',%s,'{}',%s,%s,%s,%s)""",
            (call_id, uuid4().hex * 2, "a" * 64, str(uuid4()), target, str(uuid4()),
             json.dumps({"contract_id": "manual_run"}), json.dumps({"status": "SUCCESS"}), started, started, started))
        cursor.execute("""INSERT INTO automation_project_generation_leases
            (lease_id,automation_id,generation,invocation_id,lease_owner,runtime_metadata_json,runtime_metadata_sha256,outcome,expires_at)
            VALUES(%s,%s,1,%s,'isolated','{}',%s,'SUCCEEDED',%s)""", (lease_id, target, call_id, "a" * 64, started + timedelta(minutes=1)))
        repository = AutomationPluginRepository(connection)
        def count(**changes):
            values = dict(pair_id=str(uuid4()), target_id=target, target_generation=1,
                testing_started_at=started - timedelta(seconds=1), console_contribution_ids={"manual_run"})
            return repository._lock_migration_manual_evidence_count(cursor, **{**values, **changes})
        assert count() == 1
        assert count(target_generation=2) == 0
        assert count(testing_started_at=started + timedelta(seconds=1)) == 0
        assert count(console_contribution_ids={"another-entry"}) == 0
        for status in ("RUNNING", "FAILED", "CANCELLED", "WRITE_OUTCOME_UNKNOWN"):
            cursor.execute("UPDATE automation_plugin_invocations SET status=%s WHERE invocation_id=%s", (status, call_id))
            assert count() == 0
        cursor.execute("UPDATE automation_plugin_invocations SET status='COMPLETED',source='scheduler' WHERE invocation_id=%s", (call_id,))
        assert count() == 0
        cursor.execute("UPDATE automation_plugin_invocations SET source='harness' WHERE invocation_id=%s", (call_id,))
        assert count() == 1
        cursor.execute("UPDATE automation_project_generation_leases SET outcome='WRITE_OUTCOME_UNKNOWN' WHERE lease_id=%s", (lease_id,))
        assert count() == 0
        cursor.execute("UPDATE automation_project_generation_leases SET outcome='WRITE_VERIFIED',verification_evidence_sha256=%s WHERE lease_id=%s", ("b" * 64, lease_id))
        assert count() == 0  # A result claim without a real receipt cannot prove a write.
        receipt_id = str(uuid4())
        cursor.execute("""INSERT INTO automation_write_attempt_receipts
            (receipt_id,automation_id,generation,lease_id,invocation_id,request_id,operation,action,
             argument_sha256,target_ref_sha256,target_ref_json,outcome,evidence_sha256,created_at,updated_at)
            VALUES(%s,%s,1,%s,%s,%s,'service.invoke','write',%s,%s,'{}','WRITE_VERIFIED',%s,%s,%s)""",
            (receipt_id, target, lease_id, call_id, str(uuid4()), "a" * 64, "b" * 64, "c" * 64, started, started))
        assert count() == 1
        cursor.execute("UPDATE automation_write_attempt_receipts SET outcome='WRITE_OUTCOME_UNKNOWN',evidence_sha256=NULL WHERE receipt_id=%s", (receipt_id,))
        assert count() == 0
        connection.rollback()


def test_disabled_source_stays_disabled_and_missing_enablement_is_not_guessed():
    preserve = AutomationPluginRepository._source_enabled_before_migration
    assert preserve({"source": {"enabled": False}}) is False
    assert preserve({"source": {"enabled": True}}) is True
    with pytest.raises(ConcurrentUpdateError, match="not captured"):
        preserve({"source": {}})


def test_cutover_counts_a_direct_call_before_its_generation_lease_exists(database):
    fixture, name = database
    target, call_id = "starting-" + uuid4().hex, str(uuid4())
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        _seed_generation(connection, target)
        cursor.execute("""INSERT INTO automation_plugin_invocations
            (invocation_id,request_key_sha256,request_sha256,request_id,automation_id,generation,
             operation,source,actor_id,owner_id,status,invocation_json,arguments_json,started_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,1,'validation','console','isolated-admin',%s,'STARTING','{}','{}',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (call_id, uuid4().hex * 2, "a" * 64, str(uuid4()), target, str(uuid4())))
        for status in ("STARTING", "RUNNING", "CANCELLING", "CANCELLED"):
            cursor.execute("UPDATE automation_plugin_invocations SET status=%s WHERE invocation_id=%s", (status, call_id))
            summary = AutomationPluginRepository._lock_migration_generation_leases(cursor, source_id="isolated-source", target_id=target,
                                                                                   testing_started_at=datetime(2026, 9, 11))
            assert summary["active"] == (0 if status == "CANCELLED" else 1)
        connection.rollback()


@pytest.mark.parametrize("side,when,outcome,active,unknown", [
    ("source", -1, "WRITE_OUTCOME_UNKNOWN", 0, 0),
    ("source", 0, "WRITE_OUTCOME_UNKNOWN", 0, 1),
    ("source", 1, "WRITE_OUTCOME_UNKNOWN", 0, 1),
    ("target", -1, "WRITE_OUTCOME_UNKNOWN", 0, 1),
    ("source", -1, "RUNNING", 1, 0),
    ("source", -1, "VERIFYING", 1, 0),
    ("target", 1, "RUNNING", 1, 0),
    ("target", 1, "FAILED_BEFORE_WRITE", 0, 0),
])
def test_migration_ignores_only_preexisting_terminal_source_history(database, side, when, outcome, active, unknown):
    fixture, name = database
    source, target = "source-" + uuid4().hex, "target-" + uuid4().hex
    started = datetime(2026, 9, 11, 8)
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        _seed_generation(connection, source)
        _seed_generation(connection, target)
        lease_id = str(uuid4())
        cursor.execute("""INSERT INTO automation_project_generation_leases
            (lease_id,automation_id,generation,lease_owner,runtime_metadata_json,runtime_metadata_sha256,
             outcome,acquired_at,expires_at) VALUES(%s,%s,1,'isolated','{}',%s,%s,%s,%s)""",
            (lease_id, source if side == "source" else target, "a" * 64, outcome,
             started + timedelta(seconds=when), started + timedelta(minutes=1)))
        summary = AutomationPluginRepository._lock_migration_generation_leases(cursor, source_id=source,
            target_id=target, testing_started_at=started)
        assert summary["active"] == active
        assert summary["unknown"] == unknown
        cursor.execute("SELECT outcome FROM automation_project_generation_leases WHERE lease_id=%s", (lease_id,))
        assert cursor.fetchone()["outcome"] == outcome
        connection.rollback()


@pytest.mark.parametrize("status,finished,matches,active,unknown", [
    ("WRITE_OUTCOME_UNKNOWN", True, True, 0, 0),
    ("FAILED", True, True, 0, 0),
    ("CANCELLED", True, True, 0, 0),
    ("WRITE_OUTCOME_UNKNOWN", False, True, 0, 1),
    ("WRITE_OUTCOME_UNKNOWN", True, False, 0, 1),
    ("RUNNING", True, True, 1, 1),
    ("CANCELLING", True, True, 1, 1),
    ("COMPLETED", True, True, 0, 1),
])
def test_stopped_direct_failure_is_history_without_fabricating_write_success(
        database, status, finished, matches, active, unknown):
    fixture, name = database
    target, call_id, lease_id = "stopped-" + uuid4().hex, str(uuid4()), str(uuid4())
    started = datetime(2026, 9, 12, 8)
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        _seed_generation(connection, target)
        cursor.execute("""INSERT INTO automation_plugin_invocations
            (invocation_id,request_key_sha256,request_sha256,request_id,automation_id,generation,
             operation,source,actor_id,owner_id,status,invocation_json,arguments_json,started_at,finished_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,'validation','console','isolated-admin',%s,%s,'{}','{}',%s,%s,%s)""",
            (call_id, uuid4().hex * 2, "a" * 64, str(uuid4()), target, 1 if matches else 2,
             str(uuid4()), status, started, started if finished else None, started))
        cursor.execute("""INSERT INTO automation_project_generation_leases
            (lease_id,automation_id,generation,invocation_id,lease_owner,runtime_metadata_json,
             runtime_metadata_sha256,outcome,acquired_at,expires_at)
            VALUES(%s,%s,1,%s,'isolated','{}',%s,'WRITE_OUTCOME_UNKNOWN',%s,%s)""",
            (lease_id, target, call_id, "a" * 64, started, started + timedelta(minutes=1)))
        summary = AutomationPluginRepository._lock_migration_generation_leases(cursor,
            source_id="isolated-source", target_id=target, testing_started_at=started)
        assert summary == {"active": active, "unknown": unknown, "target_verified": 0}
        assert AutomationPluginRepository._lock_migration_manual_evidence_count(cursor,
            pair_id=str(uuid4()), target_id=target, target_generation=1,
            testing_started_at=started, console_contribution_ids={"manual_run"}) == 0
        cursor.execute("SELECT outcome,verification_evidence_sha256 FROM automation_project_generation_leases WHERE lease_id=%s", (lease_id,))
        assert cursor.fetchone() == {"outcome": "WRITE_OUTCOME_UNKNOWN", "verification_evidence_sha256": None}
        connection.rollback()
