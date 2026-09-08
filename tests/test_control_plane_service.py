from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from agent.orchestration.control_plane_service import ControlPlaneService
from agent.orchestration.models import Actor, ActorType, OrchestrationError


def _run(
    run_id: str,
    status: str,
    *,
    work_item_id: str = "work-1",
    command_id: str = "command-1",
    run_no: int = 1,
    version: int = 1,
) -> dict:
    return {
        "run_id": run_id,
        "work_item_id": work_item_id,
        "command_id": command_id,
        "run_no": run_no,
        "status": status,
        "mode": "COMMAND",
        "planner_kind": "DETERMINISTIC",
        "correlation_id": "correlation-1",
        "version": version,
        "plan_json": {
            "plan_hash": "plan-hash",
            "private_plan_field": "must-not-return",
            "steps": [
                {
                    "step_key": "step-1",
                    "tool_name": "query_waybill",
                    "tool_version": "1.0.0",
                    "operation_type": "read",
                    "arguments": {"password": "do-not-return", "bill_code": "123"},
                    "risk_level": "low",
                    "requires_approval": False,
                }
            ],
        },
        "steps": [
            {
                "step_id": "persisted-step-1",
                "run_id": run_id,
                "step_key": "step-1",
                "tool_name": "query_waybill",
                "operation_type": "read",
                "status": "PENDING",
                "input_summary_json": {"password": "do-not-return"},
            }
        ],
        "private_column": "must-not-return",
    }


def _work_item() -> dict:
    return {
        "work_item_id": "work-1",
        "command_id": "command-1",
        "type": "tool:query_waybill",
        "title": "query",
        "status": "OPEN",
        "priority": "NORMAL",
        "source": "console",
        "owner_type": None,
        "owner_id": None,
        "version": 1,
        "dedupe_key": "private-dedupe",
        "entities": [
            {
                "relation_type": "subject",
                "entity_type": "waybill",
                "entity_id": "123",
                "source_system": "ronghui",
                "metadata_json": {"password": "do-not-return"},
            }
        ],
    }


class _FakeRuns:
    def __init__(self, repository):
        self.repository = repository
        self.linked_retry_calls = 0

    def get(self, run_id: str, *, for_update: bool = False):
        del for_update
        row = self.repository.runs.get(run_id)
        return copy.deepcopy(row) if row else None

    def request_cancel(self, run_id: str, **kwargs):
        self.repository.cancel_requests.append((run_id, copy.deepcopy(kwargs)))
        current = self.repository.runs[run_id]
        current["cancel_requested_at"] = datetime(2026, 8, 13, 1, 2, 3)
        current["cancel_requested_by_type"] = kwargs["requested_by_type"]
        current["cancel_requested_by_id"] = kwargs["requested_by_id"]
        current["cancel_reason"] = kwargs["reason"]
        current["version"] += 1
        return copy.deepcopy(current)

    def get_automation_supersession_facts(self, run_id: str):
        return copy.deepcopy(self.repository.run_facts.get(run_id, {}))

    def cancel_suspended(
        self,
        run_id: str,
        *,
        expected_version: int,
        expected_statuses,
        error_code: str,
        error_summary: str,
        finished_at,
    ):
        current = self.repository.runs[run_id]
        if current["version"] != expected_version or current["status"] not in expected_statuses:
            raise RuntimeError("CAS conflict")
        current.update(
            {
                "status": "CANCELLED",
                "error_code": error_code,
                "error_summary": error_summary,
                "finished_at": finished_at,
                "worker_id": None,
                "lease_expires_at": None,
                "version": current["version"] + 1,
            }
        )
        return copy.deepcopy(current)

    def transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        expected_statuses,
        status: str,
        **values,
    ):
        current = self.repository.runs[run_id]
        if current["version"] != expected_version or current["status"] not in expected_statuses:
            raise RuntimeError("CAS conflict")
        current.update(values)
        current["status"] = status
        current["version"] += 1
        return copy.deepcopy(current)

    def create_linked_retry(
        self,
        source_run_id: str,
        *,
        new_run_id: str,
        new_command_id: str,
        expected_statuses,
        now=None,
    ):
        del now
        self.linked_retry_calls += 1
        source = self.repository.runs[source_run_id]
        if source["status"] not in expected_statuses:
            raise RuntimeError("source status changed")
        run_no = max(
            item["run_no"]
            for item in self.repository.runs.values()
            if item["work_item_id"] == source["work_item_id"]
        ) + 1
        created = _run(
            new_run_id,
            "RECEIVED",
            work_item_id=source["work_item_id"],
            command_id=new_command_id,
            run_no=run_no,
        )
        created["retry_of_run_id"] = source_run_id
        self.repository.runs[new_run_id] = created
        return copy.deepcopy(created)

    def list_for_work_item(self, work_item_id: str, *, limit: int, offset: int):
        rows = sorted(
            (row for row in self.repository.runs.values() if row["work_item_id"] == work_item_id),
            key=lambda row: row["run_no"],
        )
        return copy.deepcopy(rows[offset : offset + limit])

    def get_first_for_work_item(self, work_item_id: str):
        rows = self.list_for_work_item(work_item_id, limit=1, offset=0)
        return rows[0] if rows else None


class _FakeWorkItems:
    def __init__(self, repository):
        self.repository = repository

    def get_by_command(self, command_id: str):
        for item in self.repository.work_items.values():
            if item["command_id"] == command_id:
                return copy.deepcopy(item)
        return None

    def get(self, work_item_id: str, *, for_update: bool = False):
        del for_update
        item = self.repository.work_items.get(work_item_id)
        return copy.deepcopy(item) if item else None

    def transition(
        self,
        work_item_id: str,
        *,
        expected_version: int,
        expected_statuses,
        status: str,
        reason_code=None,
        reason_summary=None,
        resolution=None,
        closed_at=None,
    ):
        current = self.repository.work_items[work_item_id]
        if current["version"] != expected_version or current["status"] not in expected_statuses:
            raise RuntimeError("CAS conflict")
        current.update(
            {
                "status": status,
                "current_reason_code": reason_code,
                "current_reason_summary": reason_summary,
                "resolution_json": copy.deepcopy(resolution),
                "closed_at": closed_at,
                "version": current["version"] + 1,
            }
        )
        return copy.deepcopy(current)

    @staticmethod
    def list_by_type(_item_type: str):
        return []


class _FakeCommands:
    def __init__(self, repository):
        self.repository = repository

    def get(self, command_id: str, *, for_update: bool = False):
        del for_update
        row = self.repository.commands.get(command_id)
        return copy.deepcopy(row) if row else None


class _FakeApprovals:
    def __init__(self, repository):
        self.repository = repository

    def get_latest_for_run(self, run_id: str, *, for_update: bool = False):
        del for_update
        row = self.repository.approvals.get(run_id)
        return copy.deepcopy(row) if row else None

    def invalidate_pending(self, run_id: str, **kwargs):
        del kwargs
        current = self.repository.approvals.get(run_id)
        if current is None or current.get("status") not in {"PENDING", "APPROVED"}:
            return 0
        current["status"] = "INVALIDATED"
        current["decided_at"] = datetime(2026, 8, 13, 1, 2, 3)
        return 1


class _FakeEvents:
    def __init__(self, repository):
        self.repository = repository

    def append_with_outbox(self, event_row, outbox_rows):
        row = copy.deepcopy(dict(event_row))
        row["payload_json"] = row.pop("payload", {})
        row["created_at"] = row["occurred_at"]
        self.repository.events.append(row)
        self.repository.outbox.extend(copy.deepcopy(list(outbox_rows)))
        return {"event": copy.deepcopy(row), "outbox": copy.deepcopy(list(outbox_rows))}

    def list_for_work_item(self, work_item_id: str, *, limit: int):
        rows = [row for row in self.repository.events if row.get("work_item_id") == work_item_id]
        return copy.deepcopy(rows[:limit])

    def list_for_work_item_by_type(
        self,
        work_item_id: str,
        event_type: str,
        *,
        limit: int,
        offset: int,
    ):
        rows = [
            row
            for row in self.repository.events
            if row.get("work_item_id") == work_item_id
            and row.get("event_type") == event_type
        ]
        return copy.deepcopy(rows[offset : offset + limit])

    def list_for_work_item_by_type(
        self,
        work_item_id: str,
        event_type: str,
        *,
        limit: int,
        offset: int,
    ):
        rows = [
            row
            for row in self.repository.events
            if row.get("work_item_id") == work_item_id and row.get("event_type") == event_type
        ]
        return copy.deepcopy(rows[offset : offset + limit])


class _FakeUow:
    def __init__(self, repository):
        self.repository = repository
        self.runs = repository.run_store
        self.work_items = _FakeWorkItems(repository)
        self.commands = _FakeCommands(repository)
        self.approvals = _FakeApprovals(repository)
        self.events = _FakeEvents(repository)
        self.committed = False
        self._snapshot = None

    def __enter__(self):
        self._snapshot = copy.deepcopy(
            {
                "runs": self.repository.runs,
                "work_items": self.repository.work_items,
                "approvals": self.repository.approvals,
                "events": self.repository.events,
                "outbox": self.repository.outbox,
                "cancel_requests": self.repository.cancel_requests,
            }
        )
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc, traceback
        if exc_type is not None and self._snapshot is not None:
            for name, value in self._snapshot.items():
                setattr(self.repository, name, value)
        return False

    def commit(self):
        self.committed = True


class _FakeRepository:
    def __init__(self, runs=None):
        self.runs = {row["run_id"]: copy.deepcopy(row) for row in (runs or [])}
        self.run_store = _FakeRuns(self)
        self.work_items = {"work-1": _work_item()}
        self.commands = {
            str(row["command_id"]): {
                "command_id": str(row["command_id"]),
                "automation_id": "daily_sign",
            }
            for row in (runs or [])
        }
        self.run_facts: dict[str, dict] = {}
        self.events: list[dict] = []
        self.outbox: list[dict] = []
        self.evidence = [
            {
                "evidence_id": "evidence-1",
                "work_item_id": "work-1",
                "run_id": "run-1",
                "source_system": "ronghui",
                "source_record_type": "waybill",
                "source_record_id": "123",
                "entity_type": "waybill",
                "entity_id": "123",
                "summary_json": {"password": "secret", "record_count": 1},
                "observed_at": datetime(2026, 8, 13, 1, 2, 3),
                "storage_ref": "https://example.invalid/evidence?token=secret",
                "content_sha256": "private-hash",
            }
        ]
        self.approvals = {}
        self.cancel_requests = []
        self.assignment_calls = []
        self.blocked_pages = []

    def unit_of_work(self):
        return _FakeUow(self)

    def get_run(self, run_id: str):
        row = self.runs.get(run_id)
        return copy.deepcopy(row) if row else None

    def list_work_items(self, **kwargs):
        del kwargs
        return copy.deepcopy(list(self.work_items.values()))

    def get_work_item(self, work_item_id: str):
        item = self.work_items.get(work_item_id)
        return copy.deepcopy(item) if item else None

    def list_work_item_unknown_writes(self, work_item_id: str):
        return copy.deepcopy(getattr(self, "unknown_writes", []))

    def get_timeline(self, work_item_id: str, *, limit: int):
        return copy.deepcopy([row for row in self.events if row["work_item_id"] == work_item_id][:limit])

    def list_evidence(self, work_item_id: str, *, run_id=None, limit: int):
        return copy.deepcopy(
            [
                row
                for row in self.evidence
                if row["work_item_id"] == work_item_id and (not run_id or row["run_id"] == run_id)
            ][:limit]
        )

    def get_current_approval(self, run_id: str):
        return copy.deepcopy(self.approvals.get(run_id))

    def request_run_cancel(self, run_id: str, **kwargs):
        self.cancel_requests.append((run_id, copy.deepcopy(kwargs)))
        current = self.runs[run_id]
        current["cancel_requested_at"] = datetime(2026, 8, 13, 1, 2, 3)
        current["cancel_requested_by_type"] = kwargs["requested_by_type"]
        current["cancel_requested_by_id"] = kwargs["requested_by_id"]
        current["cancel_reason"] = kwargs["reason"]
        current["version"] += 1
        return copy.deepcopy(current)

    def assign_work_item(self, work_item_id: str, **kwargs):
        current = self.work_items[work_item_id]
        if current["version"] != kwargs["expected_version"]:
            raise RuntimeError("CAS conflict")
        current["owner_type"] = kwargs["owner_type"]
        current["owner_id"] = kwargs["owner_id"]
        current["version"] += 1
        self.assignment_calls.append((work_item_id, copy.deepcopy(kwargs)))
        return copy.deepcopy(current)

    def page_blocked_login_runs_for_account(self, account_id: str, *, limit: int, offset: int):
        del account_id, limit
        if self.blocked_pages:
            return copy.deepcopy(self.blocked_pages.pop(0))
        return {"items": [], "total": 0, "limit": 500, "offset": offset, "next_offset": None, "is_complete": True}


class _FakeApprovalService:
    def __init__(self, repository):
        self.repository = repository
        self.calls = []

    def decide(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        approval = {
            "approval_id": kwargs["approval_id"],
            "run_id": "run-1",
            "work_item_id": "work-1",
            "plan_hash": kwargs["plan_hash"],
            "required_role": "admin",
            "status": kwargs["decision"],
        }
        self.repository.approvals["run-1"] = approval
        return approval


class _Command:
    command_id = "command-1"
    parameters = {"tool_name": "query_waybill"}


ACTOR = Actor(ActorType.CONSOLE_ADMIN, "admin-1", ("admin",))
SUPER_ADMIN = Actor(
    ActorType.CONSOLE_ADMIN,
    "super-admin-1",
    ("super_admin",),
    authenticated_by="mysql_admin_session",
)


class ControlPlaneServiceTests(unittest.TestCase):
    def _service(self, repository, **kwargs):
        approval = _FakeApprovalService(repository)
        return ControlPlaneService(repository, approval, **kwargs), approval

    def test_work_item_includes_multiple_old_unknown_writes_without_current_state_gate(self):
        repository = _FakeRepository([_run("run-1", "CANCELLED")])
        repository.unknown_writes = [
            {"lease_id": "old-a", "generation": 1, "run_id": "run-1", "plugin_id": "sync_arrival_stats"},
            {"lease_id": "old-b", "generation": 2, "run_id": "run-1", "plugin_id": "daily_send_orders"},
        ]
        service, _ = self._service(repository)
        result = service.get_work_item("work-1")
        records = result["unknown_write_recoveries"]
        self.assertEqual(["old-a", "old-b"], [record["lease_id"] for record in records])
        self.assertTrue(records[0]["recovery_supported"])
        self.assertEqual("", records[0]["recovery_unavailable_reason"])
        self.assertFalse(records[1]["recovery_supported"])
        self.assertIn("尚无已审核", records[1]["recovery_unavailable_reason"])

    def test_read_dtos_are_whitelisted_and_recursively_redacted(self):
        repository = _FakeRepository([_run("run-1", "WAITING_APPROVAL")])
        repository.approvals["run-1"] = {
            "approval_id": "approval-1",
            "run_id": "run-1",
            "work_item_id": "work-1",
            "plan_hash": "plan-hash",
            "status": "PENDING",
            "required_role": "admin",
            "impact_json": {"password": "secret"},
        }
        repository.events.append(
            {
                "event_id": "event-1",
                "event_type": "agent.run.blocked",
                "work_item_id": "work-1",
                "run_id": "run-1",
                "entity_type": "agent_run",
                "entity_id": "run-1",
                "source_system": "agent",
                "correlation_id": "correlation-1",
                "payload_json": {"message": "token=secret"},
            }
        )
        service, _approval = self._service(repository)

        run_data = service.get_run("run-1")
        self.assertNotIn("private_column", run_data["run"])
        self.assertNotIn("input_summary_json", run_data["run"]["steps"][0])
        self.assertNotIn("arguments", run_data["run"]["plan"]["steps"][0])
        self.assertNotIn("private_plan_field", run_data["run"]["plan"])
        detail = service.get_work_item("work-1")
        self.assertNotIn("dedupe_key", detail["work_item"])
        self.assertNotIn("metadata_json", detail["work_item"]["entities"][0])
        self.assertIn("approve", detail["allowed_actions"])
        timeline = service.get_timeline("work-1")
        self.assertEqual("token=[REDACTED]", timeline["items"][0]["message"])
        evidence = service.list_evidence("work-1", run_id="run-1")
        self.assertEqual("[REDACTED]", evidence["items"][0]["summary"]["password"])
        self.assertNotIn("content_sha256", evidence["items"][0])
        self.assertIn("token=[REDACTED]", evidence["items"][0]["storage_ref"])

    def test_run_projection_distinguishes_queue_from_actual_source_read(self):
        queued = _run("run-1", "RUNNING")
        repository = _FakeRepository([queued])
        service, _approval = self._service(repository)

        queued_dto = service.get_run("run-1")["run"]
        self.assertEqual("queued", queued_dto["execution_phase"])
        self.assertEqual("WAITING_EXECUTION_SLOT", queued_dto["stage_code"])
        self.assertEqual("任务已受理，等待开始执行", queued_dto["stage_description"])

        repository.runs["run-1"]["steps"][0].update(
            {
                "status": "RUNNING",
                "started_at": datetime(2026, 9, 3, 1, 2, 3),
            }
        )
        active_dto = service.get_run("run-1")["run"]
        self.assertEqual("source_read", active_dto["execution_phase"])
        self.assertEqual("READING_SOURCE", active_dto["stage_code"])
        self.assertEqual("正在读取数据", active_dto["stage_description"])

    def test_run_projection_only_shows_run_activity_for_a_valid_lease(self):
        verifying = _run("run-1", "VERIFYING")
        repository = _FakeRepository([verifying])
        service, _approval = self._service(repository)

        orphan = service.get_run("run-1")["run"]
        self.assertEqual("queued", orphan["execution_phase"])
        self.assertEqual("WAITING_EXECUTION_SLOT", orphan["stage_code"])

        repository.runs["run-1"].update(
            {
                "worker_id": "worker-1",
                "lease_expires_at": datetime.now(timezone.utc)
                + timedelta(minutes=1),
                "started_at": datetime(2026, 9, 3, 1, 2, 3),
            }
        )
        leased = service.get_run("run-1")["run"]
        self.assertEqual("verifying", leased["execution_phase"])
        self.assertEqual("VERIFYING_RESULT", leased["stage_code"])

        repository.runs["run-1"]["status"] = "RUNNING"
        between_steps = service.get_run("run-1")["run"]
        self.assertEqual("processing", between_steps["execution_phase"])
        self.assertEqual("PROCESSING_DATA", between_steps["stage_code"])

    def test_unclaimed_run_phases_do_not_claim_channel_contention(self):
        for status in ("RECEIVED", "CONTEXT_READY", "PLANNED", "VALIDATED", "RUNNING", "VERIFYING"):
            with self.subTest(status=status):
                service, _approval = self._service(_FakeRepository([_run("run-1", status)]))
                dto = service.get_run("run-1")["run"]
                self.assertEqual("queued", dto["execution_phase"])
                self.assertEqual("WAITING_EXECUTION_SLOT", dto["stage_code"])
                self.assertEqual("任务已受理，等待开始执行", dto["stage_description"])

    def test_run_projection_reports_actual_resource_wait_reason(self):
        reasons = {
            "execution resource is busy": "正在等待相同账号或数据位置空闲",
            "browser capacity is busy": "正在等待浏览器执行名额",
            "execution resource has an unresolved external write": "相同账号或数据位置存在待核验写入，正在等待核验",
            "account credentials are being changed": "账号信息正在更新，等待更新完成",
        }
        for reason, description in reasons.items():
            with self.subTest(reason=reason):
                run = _run("run-1", "RUNNING")
                run.update(error_code="RESOURCE_WAIT", error_summary=reason)
                service, _approval = self._service(_FakeRepository([run]))
                dto = service.get_run("run-1")["run"]
                self.assertEqual("queued", dto["execution_phase"])
                self.assertEqual("RESOURCE_WAIT", dto["stage_code"])
                self.assertEqual(description, dto["stage_description"])

    def test_run_projection_maps_internal_source_error_to_stable_public_code(self):
        failed = _run("run-1", "BLOCKED_DATA")
        failed["error_code"] = "BROKER_SOURCE_INVALID"
        repository = _FakeRepository([failed])
        service, _approval = self._service(repository)

        run = service.get_run("run-1")["run"]
        self.assertEqual("finished", run["execution_phase"])
        self.assertEqual("SOURCE_SCHEMA_CHANGED", run["public_problem_code"])

    def test_self_pickup_action_validation_maps_to_source_structure_problem(self):
        failed = _run("run-1", "BLOCKED_DATA")
        failed["steps"][0].update(
            {
                "tool_name": "automation.self_pickup_problem_upload.run",
                "error_code": "ACTION_VALUE_ERROR",
                "status": "BLOCKED_DATA",
            }
        )
        repository = _FakeRepository([failed])
        service, _approval = self._service(repository)

        run = service.get_run("run-1")["run"]

        self.assertEqual("SOURCE_SCHEMA_CHANGED", run["public_problem_code"])

    def test_failed_retryable_continues_same_run_and_persists_event(self):
        repository = _FakeRepository([_run("run-1", "FAILED_RETRYABLE")])
        wakes = []
        outbox_wakes = []
        service, _approval = self._service(
            repository,
            wake_runner=wakes.append,
            wake_outbox=lambda: outbox_wakes.append(True),
        )

        result = service.retry_run("run-1", actor=ACTOR, reason="retry now")

        self.assertEqual("run-1", result["run"]["run_id"])
        self.assertEqual("CONTEXT_READY", result["run"]["status"])
        self.assertEqual(0, repository.run_store.linked_retry_calls)
        self.assertEqual("agent.run.retry_requested", repository.events[0]["event_type"])
        self.assertEqual(["run-1"], wakes)
        self.assertEqual([True], outbox_wakes)

    def test_terminal_retry_creates_linked_run_without_reusing_source(self):
        repository = _FakeRepository([_run("run-1", "FAILED_TERMINAL")])
        service, _approval = self._service(repository)

        result = service.retry_run("run-1", actor=ACTOR)

        new_run = result["run"]
        self.assertNotEqual("run-1", new_run["run_id"])
        self.assertEqual("RECEIVED", new_run["status"])
        self.assertEqual("run-1", new_run["retry_of_run_id"])
        self.assertEqual("FAILED_TERMINAL", repository.runs["run-1"]["status"])
        self.assertEqual(1, repository.run_store.linked_retry_calls)

    def test_terminal_write_retry_is_rejected_before_scheduler_identity_can_be_reused(self):
        source = _run("run-1", "FAILED_TERMINAL")
        source["plan_json"]["steps"][0]["operation_type"] = "external_write"
        repository = _FakeRepository([source])
        service, _approval = self._service(repository)

        with self.assertRaisesRegex(
            OrchestrationError,
            "Write runs cannot be replayed",
        ) as raised:
            service.retry_run("run-1", actor=ACTOR)

        self.assertEqual(
            "UNSAFE_WRITE_RETRY_REQUIRES_NEW_COMMAND",
            raised.exception.code,
        )
        self.assertEqual(0, repository.run_store.linked_retry_calls)

    def test_completed_run_cannot_be_retried(self):
        repository = _FakeRepository([_run("run-1", "COMPLETED")])
        service, _approval = self._service(repository)

        with self.assertRaisesRegex(OrchestrationError, "cannot be retried"):
            service.retry_run("run-1", actor=ACTOR)

    def test_cancelled_run_cannot_reopen_a_cancelled_work_item(self):
        repository = _FakeRepository([_run("run-1", "CANCELLED")])
        service, _approval = self._service(repository)

        with self.assertRaisesRegex(OrchestrationError, "cannot be retried"):
            service.retry_run("run-1", actor=ACTOR)

        self.assertNotIn("retry", service.get_run("run-1")["allowed_actions"])

    def test_clarify_persists_event_resumes_same_run_and_is_context_readable(self):
        repository = _FakeRepository([_run("run-1", "NEEDS_CLARIFICATION")])
        service, _approval = self._service(repository)

        result = service.clarify_run(
            "run-1",
            actor=ACTOR,
            clarification={"account_id": "account-1", "note": "use this account"},
        )

        self.assertEqual("run-1", result["run"]["run_id"])
        self.assertEqual("CONTEXT_READY", result["run"]["status"])
        self.assertEqual("agent.run.clarified", repository.events[0]["event_type"])
        context = service.resolve_command_context(_Command())
        self.assertEqual("account-1", context["clarifications"][0]["clarification"]["account_id"])
        self.assertEqual("account-1", context["clarification_override"]["account_id"])

    def test_plain_text_clarification_is_note_only(self):
        repository = _FakeRepository([_run("run-1", "NEEDS_CLARIFICATION")])
        service, _approval = self._service(repository)

        service.clarify_run("run-1", actor=ACTOR, clarification="account-1")

        payload = repository.events[0]["payload_json"]
        self.assertEqual(
            {"schema_version": 1, "note": "account-1"},
            payload["clarification"],
        )
        context = service.resolve_command_context(_Command())
        self.assertNotIn("clarification_override", context)

    def test_customer_project_alias_receives_authoritative_recheck_context(self):
        repository = _FakeRepository([_run("run-1", "CONTEXT_READY")])
        service, _approval = self._service(repository)

        class _CustomerProjectCommand(_Command):
            parameters = {
                "tool_name": "automation.customer_problems_shadow.run",
            }
            automation_invocation = SimpleNamespace(
                automation_id="customer_problems_shadow",
            )

        context = service.resolve_command_context(_CustomerProjectCommand())

        self.assertIn("customer_problem_open_refs", context)

    def test_clarification_is_scoped_to_exact_command_and_latest_explicit_patch(self):
        repository = _FakeRepository([_run("run-1", "NEEDS_CLARIFICATION")])
        service, _approval = self._service(repository)
        service.clarify_run(
            "run-1",
            actor=ACTOR,
            clarification={
                "account_id": "account-1",
                "argument_updates": {"direction": "published_to_me"},
            },
        )
        repository.runs["run-1"]["status"] = "NEEDS_CLARIFICATION"
        service.clarify_run(
            "run-1",
            actor=ACTOR,
            clarification={"argument_updates": {"direction": "my_published"}},
        )
        old_event = copy.deepcopy(repository.events[0])
        old_event["event_id"] = "old-command-event"
        old_event["payload_json"]["command_id"] = "command-old"
        old_event["payload_json"]["clarification"]["account_id"] = "wrong-account"
        repository.events.insert(0, old_event)

        context = service.resolve_command_context(_Command())

        self.assertEqual(2, len(context["clarifications"]))
        self.assertEqual("account-1", context["clarification_override"]["account_id"])
        self.assertEqual(
            {"direction": "my_published"},
            context["clarification_override"]["argument_updates"],
        )

    def test_clarification_contract_rejects_unknown_and_sensitive_update_fields(self):
        repository = _FakeRepository([_run("run-1", "NEEDS_CLARIFICATION")])
        service, _approval = self._service(repository)

        with self.assertRaisesRegex(OrchestrationError, "Unsupported clarification fields"):
            service.clarify_run(
                "run-1",
                actor=ACTOR,
                clarification={"guessed_value": "account-1"},
            )
        with self.assertRaisesRegex(OrchestrationError, "credential fields"):
            service.clarify_run(
                "run-1",
                actor=ACTOR,
                clarification={"argument_updates": {"password": "forbidden"}},
            )

    def test_clarify_rejects_unrelated_status(self):
        repository = _FakeRepository([_run("run-1", "BLOCKED_LOGIN")])
        service, _approval = self._service(repository)

        with self.assertRaisesRegex(OrchestrationError, "Only NEEDS_CLARIFICATION or BLOCKED_DATA"):
            service.clarify_run("run-1", actor=ACTOR, clarification="account-1")

    def test_assign_requires_caller_cas_version(self):
        repository = _FakeRepository([])
        service, _approval = self._service(repository)

        result = service.assign_work_item(
            "work-1",
            expected_version=1,
            owner_type="console_admin",
            owner_id="admin-2",
        )
        self.assertEqual(2, result["work_item"]["version"])
        with self.assertRaisesRegex(RuntimeError, "CAS conflict"):
            service.assign_work_item(
                "work-1",
                expected_version=1,
                owner_type="console_admin",
                owner_id="admin-3",
            )

    def test_approve_and_reject_delegate_to_approval_service(self):
        repository = _FakeRepository([_run("run-1", "WAITING_APPROVAL")])
        service, approval = self._service(repository)

        approved = service.approve(
            "approval-1",
            plan_hash="plan-hash",
            actor=ACTOR,
            source="console",
        )
        rejected = service.reject(
            "approval-2",
            plan_hash="plan-hash",
            actor=ACTOR,
            source="console",
        )

        self.assertEqual("APPROVED", approved["approval"]["status"])
        self.assertEqual("REJECTED", rejected["approval"]["status"])
        self.assertEqual(["APPROVED", "REJECTED"], [call["decision"] for call in approval.calls])


class ControlPlaneServiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_suspended_run_is_cancelled_immediately(self):
        repository = _FakeRepository([_run("run-1", "BLOCKED_DATA")])
        repository.work_items["work-1"]["status"] = "BLOCKED_DATA"
        active = []
        wakes = []

        async def cancel_active(run_id):
            active.append(run_id)

        service = ControlPlaneService(
            repository,
            _FakeApprovalService(repository),
            cancel_active=cancel_active,
            wake_runner=wakes.append,
        )

        result = await service.cancel_run("run-1", actor=ACTOR, comment="stop")

        self.assertEqual("CANCELLED", result["run"]["status"])
        self.assertEqual("CANCELLED", repository.work_items["work-1"]["status"])
        self.assertEqual([], repository.cancel_requests)
        self.assertEqual([], active)
        self.assertEqual([], wakes)
        self.assertEqual("agent.run.cancelled", repository.events[-1]["event_type"])

    async def test_unknown_write_suspended_run_cannot_be_cancelled(self):
        repository = _FakeRepository([_run("run-1", "BLOCKED_DATA")])
        repository.work_items["work-1"]["status"] = "BLOCKED_DATA"
        repository.run_facts["run-1"] = {"has_unknown_write_receipt": True}
        service = ControlPlaneService(
            repository,
            _FakeApprovalService(repository),
        )

        with self.assertRaises(OrchestrationError) as raised:
            await service.cancel_run("run-1", actor=ACTOR, comment="stop")

        self.assertEqual("RUN_WRITE_OUTCOME_UNKNOWN", raised.exception.code)
        self.assertEqual("BLOCKED_DATA", repository.runs["run-1"]["status"])
        self.assertEqual([], repository.cancel_requests)

    async def test_super_admin_can_explicitly_cancel_unknown_write_suspended_run(self):
        repository = _FakeRepository([_run("run-1", "BLOCKED_DATA")])
        repository.work_items["work-1"]["status"] = "BLOCKED_DATA"
        repository.run_facts["run-1"] = {"has_unknown_write_receipt": True}
        service = ControlPlaneService(
            repository,
            _FakeApprovalService(repository),
        )

        result = await service.cancel_run(
            "run-1",
            actor=SUPER_ADMIN,
            comment="旧扫描已结束，明确取消残留事项",
        )

        self.assertEqual("CANCELLED", result["run"]["status"])
        self.assertEqual(
            "CANCELLED_UNKNOWN_WRITE_BY_SUPER_ADMIN",
            result["run"]["error_code"],
        )
        self.assertEqual("CANCELLED", repository.work_items["work-1"]["status"])
        self.assertEqual([], repository.cancel_requests)
        self.assertTrue(
            repository.events[-1]["payload_json"][
                "write_outcome_unknown_acknowledged"
            ]
        )

    async def test_super_admin_unknown_write_cancellation_requires_reason(self):
        repository = _FakeRepository([_run("run-1", "BLOCKED_DATA")])
        repository.work_items["work-1"]["status"] = "BLOCKED_DATA"
        repository.run_facts["run-1"] = {"has_unknown_write_receipt": True}
        service = ControlPlaneService(
            repository,
            _FakeApprovalService(repository),
        )

        with self.assertRaises(OrchestrationError) as raised:
            await service.cancel_run("run-1", actor=SUPER_ADMIN)

        self.assertEqual("RUN_CANCELLATION_REASON_REQUIRED", raised.exception.code)
        self.assertEqual("BLOCKED_DATA", repository.runs["run-1"]["status"])

    async def test_cancel_requests_persistence_and_cancels_active_execution(self):
        repository = _FakeRepository([_run("run-1", "RUNNING")])
        repository.runs["run-1"].update(worker_id="active-worker", lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        repository.run_facts["run-1"] = {"has_inflight_step": True}
        active = []
        wakes = []

        async def cancel_active(run_id):
            active.append(run_id)

        service = ControlPlaneService(
            repository,
            _FakeApprovalService(repository),
            cancel_active=cancel_active,
            wake_runner=wakes.append,
        )
        result = await service.cancel_run("run-1", actor=ACTOR, comment="stop")

        self.assertEqual("run-1", result["run"]["run_id"])
        self.assertEqual(["run-1"], active)
        self.assertEqual(["run-1"], wakes)
        self.assertEqual("stop", repository.cancel_requests[0][1]["reason"])

    async def test_unclaimed_and_orphan_runs_cancel_immediately_without_rescheduling(self):
        statuses = (
            "RECEIVED",
            "CONTEXT_READY",
            "PLANNED",
            "VALIDATED",
            "RUNNING",
            "VERIFYING",
        )
        scheduled = datetime(2026, 9, 6, 1, 2, 3)
        for status in statuses:
            with self.subTest(status=status):
                run = _run("run-1", status)
                run["next_attempt_at"] = scheduled
                repository = _FakeRepository([run])
                active = []
                wakes = []
                service = ControlPlaneService(
                    repository,
                    _FakeApprovalService(repository),
                    cancel_active=active.append,
                    wake_runner=wakes.append,
                )

                result = await service.cancel_run(
                    "run-1",
                    actor=ACTOR,
                    comment="stop",
                )

                self.assertEqual("CANCELLED", result["run"]["status"])
                self.assertEqual(scheduled, repository.runs["run-1"]["next_attempt_at"])
                self.assertEqual("CANCELLED", repository.work_items["work-1"]["status"])
                self.assertEqual([], repository.cancel_requests)
                self.assertEqual([], active)
                self.assertEqual([], wakes)

    async def test_valid_run_lease_between_steps_uses_cooperative_cancellation(self):
        run = _run("run-1", "RUNNING")
        run.update(
            {
                "worker_id": "worker-1",
                "lease_expires_at": datetime.now(timezone.utc)
                + timedelta(minutes=1),
            }
        )
        repository = _FakeRepository([run])
        active = []
        service = ControlPlaneService(
            repository,
            _FakeApprovalService(repository),
            cancel_active=active.append,
        )

        result = await service.cancel_run("run-1", actor=ACTOR, comment="stop")

        self.assertEqual("RUNNING", result["run"]["status"])
        self.assertEqual(["run-1"], active)
        self.assertEqual(1, len(repository.cancel_requests))

    async def test_waiting_approval_cancellation_invalidates_delivery_atomically(self):
        repository = _FakeRepository([_run("run-1", "WAITING_APPROVAL")])
        repository.work_items["work-1"]["status"] = "WAITING_APPROVAL"
        repository.approvals["run-1"] = {
            "approval_id": "approval-1",
            "run_id": "run-1",
            "work_item_id": "work-1",
            "plan_hash": "plan-hash",
            "status": "PENDING",
        }
        service = ControlPlaneService(repository, _FakeApprovalService(repository))

        result = await service.cancel_run("run-1", actor=ACTOR, comment="stop")

        self.assertEqual("CANCELLED", result["run"]["status"])
        self.assertEqual("INVALIDATED", repository.approvals["run-1"]["status"])
        self.assertEqual(
            ["agent.approval.invalidated", "agent.run.cancelled"],
            [row["event_type"] for row in repository.events],
        )
        self.assertEqual(
            {"orchestration.audit", "feishu.approval"},
            {
                row["consumer_name"]
                for row in repository.outbox
                if row["topic"] == "agent.approval.invalidated"
            },
        )

    async def test_waiting_approval_cancellation_rolls_back_invalidation_on_failure(self):
        repository = _FakeRepository([_run("run-1", "WAITING_APPROVAL")])
        repository.work_items["work-1"]["status"] = "WAITING_APPROVAL"
        repository.approvals["run-1"] = {
            "approval_id": "approval-1",
            "run_id": "run-1",
            "work_item_id": "work-1",
            "plan_hash": "plan-hash",
            "status": "PENDING",
        }

        def fail_cancel(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("forced cancellation failure")

        repository.run_store.cancel_suspended = fail_cancel
        service = ControlPlaneService(repository, _FakeApprovalService(repository))

        with self.assertRaisesRegex(RuntimeError, "forced cancellation failure"):
            await service.cancel_run("run-1", actor=ACTOR, comment="stop")

        self.assertEqual("WAITING_APPROVAL", repository.runs["run-1"]["status"])
        self.assertEqual("WAITING_APPROVAL", repository.work_items["work-1"]["status"])
        self.assertEqual("PENDING", repository.approvals["run-1"]["status"])
        self.assertEqual([], repository.events)
        self.assertEqual([], repository.outbox)

    async def test_session_restore_pages_completely_and_resumes_exact_matches(self):
        first = _run("run-1", "BLOCKED_LOGIN", version=1)
        second = _run("run-2", "BLOCKED_LOGIN", run_no=2, version=3)
        repository = _FakeRepository([first, second])
        repository.commands["command-1"]["automation_id"] = None
        repository.blocked_pages = [
            {
                "items": [copy.deepcopy(first)],
                "total": 2,
                "limit": 1,
                "offset": 0,
                "next_offset": 1,
                "is_complete": False,
            },
            {
                "items": [copy.deepcopy(second)],
                "total": 2,
                "limit": 1,
                "offset": 1,
                "next_offset": None,
                "is_complete": True,
            },
        ]
        wakes = []
        service = ControlPlaneService(
            repository,
            _FakeApprovalService(repository),
            wake_runner=wakes.append,
        )

        result = await service.publish_session_restored("account-1")

        self.assertEqual(2, result["resumed_count"])
        self.assertEqual({"CONTEXT_READY"}, {repository.runs[run_id]["status"] for run_id in ("run-1", "run-2")})
        self.assertEqual(["run-1", "run-2"], wakes)
        self.assertEqual(
            ["account.session_restored", "account.session_restored"],
            [event["event_type"] for event in repository.events],
        )

    async def test_session_restore_leaves_old_automation_runs_untouched(self):
        automation = _run("automation", "BLOCKED_LOGIN", command_id="automation-command")
        ordinary = _run("ordinary", "BLOCKED_LOGIN", command_id="ordinary-command")
        orphan = _run("orphan", "BLOCKED_LOGIN", command_id="missing-command")
        repository = _FakeRepository([automation, ordinary, orphan])
        repository.commands["ordinary-command"]["automation_id"] = None
        del repository.commands["missing-command"]
        repository.blocked_pages = [{"items": [automation, ordinary, orphan], "is_complete": True}]
        before_automation = copy.deepcopy(repository.runs["automation"])
        before_orphan = copy.deepcopy(repository.runs["orphan"])
        wakes = []
        service = ControlPlaneService(repository, _FakeApprovalService(repository), wake_runner=wakes.append)

        result = await service.publish_session_restored("account-1")

        self.assertEqual(1, result["resumed_count"])
        self.assertEqual(["ordinary"], wakes)
        self.assertEqual("CONTEXT_READY", repository.runs["ordinary"]["status"])
        self.assertEqual(before_automation, repository.runs["automation"])
        self.assertEqual(before_orphan, repository.runs["orphan"])
        self.assertEqual(["ordinary"], [event["run_id"] for event in repository.events])


if __name__ == "__main__":
    unittest.main()
