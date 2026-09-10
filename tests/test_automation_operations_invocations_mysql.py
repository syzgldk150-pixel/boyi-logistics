"""Real MySQL aggregation of independent calls, without legacy task creation."""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.business_query import (
    AutomationOperationsQueryService,
    MySQLAutomationOperationsRepository,
)
from tests.test_module_data_sources_mysql import database  # noqa: F401


def _legacy_counts(fixture, name):
    with fixture._connection(name) as connection, connection.cursor() as cursor:
        result = {}
        for table in ("agent_commands", "work_items", "agent_runs", "agent_run_steps"):
            cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
            result[table] = cursor.fetchone()["count"]
        return result


@pytest.fixture
def operations(database):
    # The imported fixture owns a randomly named, fully migrated database. No
    # test below uses or removes the caller's AGENT_DB_NAME database.
    fixture, name = database
    before = _legacy_counts(fixture, name)
    assert all(count == 0 for count in before.values())
    created_ids = []

    def insert(status, started_at, updated_at=None):
        invocation_id = str(uuid4())
        started = datetime.fromisoformat(started_at)
        updated = datetime.fromisoformat(updated_at) if updated_at else started
        finished = updated if status in {
            "COMPLETED", "FAILED", "CANCELLED", "WRITE_OUTCOME_UNKNOWN"
        } else None
        with fixture._connection(name) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO automation_plugin_invocations
                    (invocation_id, request_key_sha256, request_sha256,
                     request_id, automation_id, operation, source, actor_id,
                     owner_id, status, invocation_json, arguments_json,
                     started_at, finished_at, updated_at)
                   VALUES (%s,%s,%s,%s,'synthetic-operations-plugin','execute',
                     'console','synthetic-admin',%s,%s,'{}','{}',%s,%s,%s)""",
                (invocation_id, uuid4().hex * 2, "a" * 64, invocation_id,
                 str(uuid4()), status, started, finished, updated),
            )
            connection.commit()
        created_ids.append(invocation_id)

    service = AutomationOperationsQueryService(
        MySQLAutomationOperationsRepository(lambda: fixture._connection(name))
    )
    try:
        yield SimpleNamespace(insert=insert, service=service)
        assert _legacy_counts(fixture, name) == before
    finally:
        # Remove only records created by this test, including after failures.
        if created_ids:
            with fixture._connection(name) as connection, connection.cursor() as cursor:
                placeholders = ",".join(["%s"] * len(created_ids))
                cursor.execute(
                    f"DELETE FROM automation_plugin_invocations WHERE invocation_id IN ({placeholders})",
                    created_ids,
                )
                connection.commit()


def _query(operations, start_date="2026-09-09", end_date="2026-09-09"):
    return operations.service.run({"start_date": start_date, "end_date": end_date})


def test_direct_statuses_terminal_denominator_and_started_date_freshness(operations):
    # Start times are UTC. Both boundaries of the Shanghai business day are
    # tested at microsecond precision; updated_at must not choose membership.
    operations.insert("COMPLETED", "2026-09-08 16:00:00")
    operations.insert("COMPLETED", "2026-09-09 00:00:00")
    operations.insert("FAILED", "2026-09-09 01:00:00", "2026-09-10 18:00:00")
    operations.insert("CANCELLED", "2026-09-09 02:00:00")
    operations.insert("WRITE_OUTCOME_UNKNOWN", "2026-09-09 03:00:00")
    operations.insert("STARTING", "2026-09-09 04:00:00")
    operations.insert("RUNNING", "2026-09-09 05:00:00")
    operations.insert("RUNNING", "2026-09-09 06:00:00")
    operations.insert("CANCELLING", "2026-09-09 15:59:59.999999")
    operations.insert("COMPLETED", "2026-09-08 15:59:59.999999", "2026-09-12 00:00:00")
    operations.insert("COMPLETED", "2026-09-09 16:00:00", "2026-09-12 01:00:00")

    assert _query(operations) == {
        "query_type": "automation_operations",
        "record_source": "automation_plugin_invocations",
        "availability": "DATA",
        "period": {"start_date": "2026-09-09", "end_date": "2026-09-09"},
        "invocations": {
            "status_counts": {
                "COMPLETED": 2, "FAILED": 1, "CANCELLED": 1,
                "WRITE_OUTCOME_UNKNOWN": 1, "STARTING": 1,
                "RUNNING": 2, "CANCELLING": 1,
            },
            "total": 9,
            "success_rate": {
                "completed_invocations": 2,
                "terminal_invocations": 5,
                "value": "0.4000",
            },
        },
        "freshness": {
            "latest_invocation_started_at": "2026-09-09T15:59:59Z",
            "latest_invocation_updated_at": "2026-09-10T18:00:00Z",
        },
    }


def test_shanghai_midnight_partitions_one_utc_date_and_closed_day_range(operations):
    operations.insert("COMPLETED", "2026-09-09 15:59:59.999999")
    operations.insert("FAILED", "2026-09-09 16:00:00")
    operations.insert("RUNNING", "2026-09-09 16:00:00.000001")

    previous = _query(operations)
    following = _query(operations, "2026-09-10", "2026-09-10")
    combined = _query(operations, "2026-09-09", "2026-09-10")

    assert previous["invocations"] == {
        "status_counts": {"COMPLETED": 1}, "total": 1,
        "success_rate": {
            "completed_invocations": 1, "terminal_invocations": 1, "value": "1.0000"
        },
    }
    assert following["invocations"] == {
        "status_counts": {"FAILED": 1, "RUNNING": 1}, "total": 2,
        "success_rate": {
            "completed_invocations": 0, "terminal_invocations": 1, "value": "0.0000"
        },
    }
    assert combined["invocations"] == {
        "status_counts": {"COMPLETED": 1, "FAILED": 1, "RUNNING": 1}, "total": 3,
        "success_rate": {
            "completed_invocations": 1, "terminal_invocations": 2, "value": "0.5000"
        },
    }
    assert combined["invocations"]["total"] == (
        previous["invocations"]["total"] + following["invocations"]["total"]
    )


def test_only_active_invocations_have_data_but_no_completed_rate(operations):
    for status in ("STARTING", "RUNNING", "CANCELLING"):
        operations.insert(status, "2026-09-09 00:00:00")

    result = _query(operations)

    assert result["availability"] == "DATA"
    assert result["invocations"] == {
        "status_counts": {"STARTING": 1, "RUNNING": 1, "CANCELLING": 1},
        "total": 3,
        "success_rate": None,
    }


def test_empty_day_does_not_use_outside_invocation_updates_or_legacy_totals(operations):
    operations.insert("FAILED", "2026-09-08 15:59:59.999999", "2026-09-09 12:00:00")
    operations.insert("COMPLETED", "2026-09-09 16:00:00")

    assert _query(operations) == {
        "query_type": "automation_operations",
        "record_source": "automation_plugin_invocations",
        "availability": "NO_DATA",
        "period": {"start_date": "2026-09-09", "end_date": "2026-09-09"},
        "invocations": {"status_counts": {}, "total": 0, "success_rate": None},
        "freshness": {
            "latest_invocation_started_at": None,
            "latest_invocation_updated_at": None,
        },
    }
