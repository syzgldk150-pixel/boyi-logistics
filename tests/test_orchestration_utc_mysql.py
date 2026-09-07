"""Real MySQL clocks: UTC workflow deadlines with an unchanged session zone."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agent.orchestration.approval_service import APPROVAL_TTL, ApprovalService
from agent.orchestration.models import Actor, ActorType, Plan, RiskLevel
from agent.orchestration.policy_engine import PolicyDecision, PolicyEngine
from shared.orchestration_repository import OrchestrationRepository
from tests import test_mysql_orchestration_integration as mysql_helpers
from tests.test_manual_unknown_write_mysql import database as database


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated real MySQL 8",
)


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(params=("+08:00", "+00:00"))
def repository(database, request):
    def connect():
        connection = database.pymysql.connect(
            host=database.host, port=database.port, user=database.user,
            password=database.password, database=database.database,
            charset="utf8mb4", autocommit=False,
            cursorclass=database.pymysql.cursors.DictCursor,
        )
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION time_zone=%s", (request.param,))
            cursor.execute("SELECT @@session.time_zone AS zone")
            assert cursor.fetchone()["zone"] == request.param
        return connection

    return OrchestrationRepository(connect, database.pymysql.cursors.DictCursor)


def new_run(repository, *, status="RECEIVED", **run_values):
    command, item, run, event, deliveries = mysql_helpers.MySqlOrchestrationIntegrationTests._aggregate_rows("utc-queue")
    # A normal CommandGateway omits this optional field. Exercise its actual
    # aggregate persistence path instead of inserting a corrected timestamp.
    run.pop("next_attempt_at")
    run.update(status=status, **run_values)
    with repository.unit_of_work() as uow:
        receipt = uow.command_gateway_create(command, item, run, event, deliveries)
        persisted = uow.runs.get(receipt["run_id"])
        uow.commit()
    return persisted


def claim_ids(repository, *, worker="utc-worker", status="RECEIVED"):
    return {row["run_id"] for row in repository.claim_runs(worker, (status,), limit=500, lease_seconds=60)}


def update_run(repository, run_id, sql, params=()):
    # Only test-owned UUID rows are mutated, never the database/system clock.
    with repository.unit_of_work() as uow, uow.runs.cursor() as cursor:
        cursor.execute(sql, (*params, run_id))
        assert cursor.rowcount == 1
        uow.commit()


def new_approval(repository):
    plan = Plan(command_type="integration_probe", context_fingerprint="c" * 64,
                tool_catalog_hash="d" * 64, steps=())
    run = new_run(repository, status="WAITING_APPROVAL", plan_hash=plan.plan_hash)
    service = ApprovalService(repository, PolicyEngine(catalog=None))
    before = utc_now()
    approval = service.request(
        run=run, plan=plan,
        policy_decision=PolicyDecision(True, True, "admin", RiskLevel.HIGH, "APPROVAL_REQUIRED", "integration approval"),
        requested_by=Actor(ActorType.CONSOLE_ADMIN, "integration-admin", roles=("admin",)),
    )
    after = utc_now()
    assert before + APPROVAL_TTL <= approval["expires_at"] <= after + APPROVAL_TTL
    return run, approval


def inspect_expiry(uow, run, approval, path):
    if path == "expire_stale":
        uow.approvals.expire_stale(run["run_id"], run["plan_hash"])
    elif path == "expire":
        uow.approvals.expire(approval["approval_id"])
    elif path == "prepare":
        uow.approvals.prepare_approved_execution(run["run_id"], expected_plan_hash=run["plan_hash"])
    else:
        raise AssertionError("unknown test path")
    return uow.approvals.get(approval["approval_id"])


def test_new_command_without_due_override_is_immediately_claimable(repository):
    before = utc_now()
    run = new_run(repository)
    after = utc_now()
    assert before <= run["next_attempt_at"] <= after
    assert run["run_id"] in claim_ids(repository)


def test_real_future_retry_is_not_shortened_or_claimed_early(repository):
    future = utc_now() + timedelta(minutes=5)
    run = new_run(repository, next_attempt_at=future)
    assert run["next_attempt_at"] == future
    assert run["run_id"] not in claim_ids(repository)
    running = new_run(repository)
    assert running["run_id"] in claim_ids(repository, worker="retry-owner")
    with repository.unit_of_work() as uow:
        retry = uow.runs.release_or_schedule(running["run_id"], worker_id="retry-owner",
                                             status="FAILED_RETRYABLE", next_attempt_at=future)
        assert retry["next_attempt_at"] == future
        uow.commit()
    assert running["run_id"] not in claim_ids(repository, status="FAILED_RETRYABLE")


def test_active_utc_lease_is_not_stolen_and_only_expired_lease_is_reclaimable(repository):
    run = new_run(repository)
    assert run["run_id"] in claim_ids(repository, worker="owner")
    assert run["run_id"] not in claim_ids(repository, worker="contender")
    with repository.unit_of_work() as uow:
        claimed = uow.runs.get(run["run_id"])
        assert claimed["worker_id"] == "owner" and claimed["lease_expires_at"] > utc_now()
        renewed = uow.runs.renew_lease(run["run_id"], worker_id="owner", lease_seconds=60)
        assert renewed["worker_id"] == "owner" and renewed["lease_expires_at"] > utc_now()
        uow.commit()
    update_run(repository, run["run_id"],
               "UPDATE agent_runs SET lease_expires_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) WHERE run_id=%s")
    assert run["run_id"] in claim_ids(repository, worker="contender")


@pytest.mark.parametrize("path", ("approval", "recovered"))
def test_explicit_wakeup_is_immediate(repository, path):
    status = "WAITING_APPROVAL" if path == "approval" else "BLOCKED_DATA"
    run = new_run(repository, status=status, next_attempt_at=utc_now() + timedelta(hours=1))
    before = utc_now()
    with repository.unit_of_work() as uow:
        if path == "approval":
            uow.runs.make_waiting_approval_runnable(run["run_id"])
        else:
            uow.runs.release_recovered(run["run_id"], expected_version=run["version"],
                                       expected_statuses=(status,), status="RECEIVED")
        woken = uow.runs.get(run["run_id"])
        uow.commit()
    assert before <= woken["next_attempt_at"] <= utc_now()
    assert run["run_id"] in claim_ids(repository, status=woken["status"])


@pytest.mark.parametrize("path", ("expire_stale", "expire", "prepare"))
def test_approval_ttl_is_neither_premature_nor_extended(repository, path):
    run, approval = new_approval(repository)
    with repository.unit_of_work() as uow:
        pending = inspect_expiry(uow, run, approval, path)
        assert pending["status"] == "PENDING"
        assert pending["expires_at"] == approval["expires_at"]
        uow.commit()
    with repository.unit_of_work() as uow, uow.approvals.cursor() as cursor:
        cursor.execute("UPDATE approval_requests SET expires_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) WHERE approval_id=%s",
                       (approval["approval_id"],))
        before = utc_now()
        expired = inspect_expiry(uow, run, approval, path)
        assert expired["status"] == "EXPIRED"
        assert before <= expired["decided_at"] <= utc_now()
        uow.commit()


@pytest.mark.parametrize("expired", (False, True))
def test_decision_uses_current_utc_deadline_and_retains_timely_approval(repository, expired):
    run, approval = new_approval(repository)
    decision_id = str(uuid4())
    with repository.unit_of_work() as uow, uow.approvals.cursor() as cursor:
        if expired:
            cursor.execute("UPDATE approval_requests SET expires_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) WHERE approval_id=%s",
                           (approval["approval_id"],))
        before = utc_now()
        decided = uow.approvals.record_decision(
            {"decision_id": decision_id, "approval_id": approval["approval_id"],
             "actor_type": "console_admin", "actor_id": "integration-admin", "actor_roles": ["admin"], "decision": "APPROVED"},
            expected_plan_hash=run["plan_hash"],
        )
        assert decided["status"] == ("EXPIRED" if expired else "APPROVED")
        assert before <= decided["decided_at"] <= utc_now()
        cursor.execute("SELECT decided_at FROM approval_decisions WHERE decision_id=%s", (decision_id,))
        decision = cursor.fetchone()
        if expired:
            assert decided["_decision_error"] == "APPROVAL_EXPIRED" and decision is None
        else:
            assert before <= decision["decided_at"] <= utc_now()
            cursor.execute("UPDATE approval_requests SET expires_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND) WHERE approval_id=%s",
                           (approval["approval_id"],))
            assert uow.approvals.prepare_approved_execution(run["run_id"], expected_plan_hash=run["plan_hash"])["outcome"] == "APPROVED"
        uow.commit()


def test_outbox_keeps_its_consistent_database_clock(repository):
    run = new_run(repository)
    with repository.unit_of_work() as uow:
        rows = uow.outbox.claim("utc-outbox", consumer_name="integration-consumer", limit=500)
        assert run["run_id"] in {row["run_id"] for row in rows}
        uow.commit()


def test_read_projection_uses_command_utc_submission_without_rewriting_metadata(repository):
    run = new_run(repository)
    with repository.unit_of_work() as uow:
        command = uow.commands.get(run["command_id"])
        locked = uow.runs.get(run["run_id"], for_update=True)
        assert "submitted_at" not in locked
    detail = repository.get_run(run["run_id"])
    listed = repository.list_runs_for_work_item(run["work_item_id"])
    assert detail["submitted_at"] == command["requested_at"]
    assert listed[0]["submitted_at"] == command["requested_at"]
    assert detail["created_at"] == listed[0]["created_at"] == run["created_at"]
