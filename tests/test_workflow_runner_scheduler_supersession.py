from __future__ import annotations

import copy
from datetime import datetime, timezone

from agent.orchestration.models import Actor, ActorType, Command, RunStatus
from agent.orchestration.workflow_runner import WorkflowRunner
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
)


class _Runs:
    def __init__(self, repository):
        self.repository = repository

    def transition(self, run_id, *, expected_version, expected_statuses, status, **values):
        run = self.repository.run
        assert run["run_id"] == run_id
        assert run["version"] == expected_version
        assert run["status"] in expected_statuses
        run.update(values)
        run["status"] = status
        run["version"] += 1
        self.repository.trace.append(("run_transition", status))
        return copy.deepcopy(run)

    def list_open_failed_scheduler_run_ids_for_supersession(self, **kwargs):
        self.repository.selector_kwargs = dict(kwargs)
        candidate_ids = []
        for run in self.repository.prior_runs:
            occurrence = _occurrence(self.repository.prior_commands[str(run["command_id"])])
            latest = self._latest_for_work_item(str(run["work_item_id"]))
            if (
                run["status"] == RunStatus.FAILED_TERMINAL.value
                and self.repository.prior_items[str(run["work_item_id"])]["status"] == "OPEN"
                and latest["run_id"] == run["run_id"]
                and occurrence is not None
                and occurrence < kwargs["successful_occurrence"]
            ):
                candidate_ids.append(str(run["run_id"]))
        candidate_ids.sort()
        self.repository.selector_batches.append(candidate_ids[:100])
        if self.repository.fail_selector_on_call == len(self.repository.selector_batches):
            raise RuntimeError("simulated post-commit selector failure")
        if self.repository.invalidate_first_selector_batch and len(self.repository.selector_batches) == 1:
            self.repository.invalidated_run_ids.update(candidate_ids[:100])
        return candidate_ids[:100]

    def get(self, run_id, *, for_update=False):
        assert for_update
        rows = (self.repository.run, *self.repository.prior_runs)
        row = next((item for item in rows if item["run_id"] == run_id), None)
        if row is not None and run_id in self.repository.invalidated_run_ids:
            row["status"] = RunStatus.RECEIVED.value
        self.repository.trace.append(("run_lock", run_id))
        return copy.deepcopy(row) if row is not None else None

    def get_latest_for_work_item(self, work_item_id, *, for_update=False):
        assert for_update
        if work_item_id not in self.repository.prior_items:
            return None
        self.repository.trace.append(("latest_run_lock", work_item_id))
        return copy.deepcopy(self._latest_for_work_item(work_item_id))

    def _latest_for_work_item(self, work_item_id):
        if work_item_id in self.repository.latest_runs:
            return self.repository.latest_runs[work_item_id]
        if work_item_id == self.repository.prior_item["work_item_id"]:
            return self.repository.latest_run
        return next(run for run in self.repository.prior_runs if run["work_item_id"] == work_item_id)


class _WorkItems:
    def __init__(self, repository):
        self.repository = repository

    def get(self, work_item_id, *, for_update=False):
        assert for_update
        rows = (self.repository.current_item, *self.repository.prior_items.values())
        row = next((item for item in rows if item["work_item_id"] == work_item_id), None)
        return copy.deepcopy(row) if row is not None else None

    def transition(self, work_item_id, *, expected_version, expected_statuses, status, **values):
        rows = [self.repository.current_item, *self.repository.prior_items.values()]
        row = next(item for item in rows if item["work_item_id"] == work_item_id)
        assert row["version"] == expected_version
        assert row["status"] in expected_statuses
        row.update(values)
        if "reason_code" in values:
            row["current_reason_code"] = values["reason_code"]
        if "reason_summary" in values:
            row["current_reason_summary"] = values["reason_summary"]
        if "resolution" in values:
            row["resolution_json"] = values["resolution"]
        row["status"] = status
        row["version"] += 1
        self.repository.trace.append(("work_item_transition", work_item_id, status))
        return copy.deepcopy(row)


class _Events:
    def __init__(self, repository):
        self.repository = repository

    def append_with_outbox(self, event, outbox):
        self.repository.events.append((copy.deepcopy(event), copy.deepcopy(tuple(outbox))))
        self.repository.trace.append(("event", event["event_type"]))


class _Commands:
    def __init__(self, repository):
        self.repository = repository

    def get(self, command_id, *, for_update=False):
        assert for_update
        return copy.deepcopy(self.repository.prior_commands.get(command_id))


class _Uow:
    def __init__(self, repository):
        self.repository = repository
        self.runs = _Runs(repository)
        self.work_items = _WorkItems(repository)
        self.commands = _Commands(repository)
        self.events = _Events(repository)

    def __enter__(self):
        self.repository.trace.append(("enter",))
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        self.repository.trace.append(("exit",))
        return False

    def commit(self):
        self.repository.commits += 1
        self.repository.trace.append(("commit",))


class _Repository:
    def __init__(self):
        self.run = {
            "run_id": "successful-run",
            "work_item_id": "successful-item",
            "status": RunStatus.VERIFYING.value,
            "version": 4,
            "correlation_id": "successful-correlation",
            "causation_id": None,
        }
        self.current_item = {
            "work_item_id": "successful-item",
            "status": "IN_PROGRESS",
            "version": 3,
        }
        self.prior_run = {
            "run_id": "old-failed-run",
            "work_item_id": "old-item",
            "command_id": "old-command",
            "status": RunStatus.FAILED_TERMINAL.value,
            "version": 9,
        }
        self.latest_run = self.prior_run
        self.prior_runs = [self.prior_run]
        self.prior_item = {
            "work_item_id": "old-item",
            "status": "OPEN",
            "version": 9,
        }
        self.prior_command = {
            "command_id": "old-command",
            "source": "scheduler",
            "actor_type": "scheduler",
            "actor_id": "customer-problems-shadow",
            "automation_id": "customer_problems_shadow",
            "parameters_json": {
                "execution_context": {
                    "task_id": "customer-problems-shadow",
                    "scheduled_for": "2026-08-22T00:00:00+00:00",
                }
            },
            "requested_at": datetime(2026, 8, 22, 0, 0),
        }
        self.prior_items = {self.prior_item["work_item_id"]: self.prior_item}
        self.prior_commands = {self.prior_command["command_id"]: self.prior_command}
        self.latest_runs = {}
        self.events = []
        self.trace = []
        self.selector_kwargs = None
        self.selector_batches = []
        self.fail_selector_on_call = None
        self.invalidate_first_selector_batch = False
        self.invalidated_run_ids = set()
        self.commits = 0

    def add_prior_failures(self, count: int) -> None:
        for index in range(count):
            run_id = f"old-failed-run-{index}"
            work_item_id = f"old-item-{index}"
            command_id = f"old-command-{index}"
            run = {
                "run_id": run_id,
                "work_item_id": work_item_id,
                "command_id": command_id,
                "status": RunStatus.FAILED_TERMINAL.value,
                "version": 1,
            }
            item = {"work_item_id": work_item_id, "status": "OPEN", "version": 1}
            command = copy.deepcopy(self.prior_command)
            command.update({"command_id": command_id, "requested_at": datetime(2026, 8, 22)})
            self.prior_runs.append(run)
            self.prior_items[work_item_id] = item
            self.prior_commands[command_id] = command

    def unit_of_work(self):
        return _Uow(self)


def _scheduler_command(*, matching_context: bool = True) -> Command:
    task_id = "customer-problems-shadow"
    invocation = AutomationProjectInvocation(
        automation_id="customer_problems_shadow",
        automation_generation=3,
        entrypoint=AutomationEntrypoint.SCHEDULER,
        contract_id="contract-1",
        contract_hash="a" * 64,
        policy_version=2,
        project_configuration_version=4,
        request_id="scheduler:customer-problems-shadow:2026-08-23T00:00:00+08:00",
    )
    return Command(
        command_id="successful-command",
        command_type="automation.project.invoke",
        source="scheduler",
        actor=Actor(ActorType.SCHEDULER, task_id, ("system",)),
        parameters={
            "tool_name": "automation.customer_problems_shadow.run",
            "arguments": {},
            "execution_context": {
                "task_id": task_id if matching_context else "different-task",
                "scheduled_for": "2026-08-23T00:00:00+00:00",
            }
        },
        automation_invocation=invocation,
        idempotency_key="scheduler:customer-problems-shadow:2026-08-23T00:00:00+08:00",
        correlation_id="successful-correlation",
        requested_at=datetime(2026, 8, 23, 0, 0),
    )


def _occurrence(command: dict) -> datetime | None:
    context = command["parameters_json"]["execution_context"]
    scheduled_for = context.get("scheduled_for")
    if isinstance(scheduled_for, str):
        parsed = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return command.get("requested_at")


def test_scheduler_success_supersedes_only_locked_prior_terminal_failures_in_same_transaction():
    repository = _Repository()
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    completed = runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(),
    )

    assert completed["status"] == RunStatus.COMPLETED.value
    assert repository.commits == 1
    assert repository.current_item["status"] == "RESOLVED"
    assert repository.prior_item["status"] == "CANCELLED"
    assert repository.prior_item["current_reason_code"] == "SUPERSEDED_BY_LATER_SUCCESS"
    assert repository.prior_item["current_reason_summary"] == "已由后续成功运行取代"
    assert repository.prior_item["resolution_json"] == {"successful_run_id": "successful-run"}
    assert repository.selector_kwargs == {
        "automation_id": "customer_problems_shadow",
        "scheduler_task_id": "customer-problems-shadow",
        "successful_work_item_id": "successful-item",
        "successful_occurrence": datetime(2026, 8, 23, 0, 0),
    }
    assert [event[0]["event_type"] for event in repository.events] == [
        "agent.run.status_changed",
        "work_item.superseded_by_later_success",
    ]
    supersession_event, supersession_outbox = repository.events[1]
    assert supersession_event["work_item_id"] == "old-item"
    assert supersession_event["run_id"] == "old-failed-run"
    assert supersession_event["payload"] == {
        "successful_run_id": "successful-run",
        "failed_run_id": "old-failed-run",
        "scheduler_task_id": "customer-problems-shadow",
        "automation_id": "customer_problems_shadow",
    }
    assert supersession_outbox[0]["topic"] == "work_item.superseded_by_later_success"
    assert repository.trace.index(("run_transition", "COMPLETED")) < repository.trace.index(("commit",))
    assert repository.trace.index(("run_lock", "old-failed-run")) < repository.trace.index(
        ("latest_run_lock", "old-item")
    )
    assert repository.trace.index(("latest_run_lock", "old-item")) < repository.trace.index(
        ("work_item_transition", "old-item", "CANCELLED")
    )
    assert repository.trace.index(("work_item_transition", "old-item", "CANCELLED")) < repository.trace.index(("commit",))


def test_incomplete_scheduler_identity_does_not_select_or_supersede_prior_failures():
    repository = _Repository()
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(matching_context=False),
    )

    assert repository.selector_kwargs is None
    assert repository.prior_item["status"] == "OPEN"
    assert [event[0]["event_type"] for event in repository.events] == ["agent.run.status_changed"]


def test_later_retry_discovered_after_candidate_snapshot_keeps_old_item_open():
    repository = _Repository()
    repository.latest_run = {
        "run_id": "later-retry-run",
        "work_item_id": "old-item",
        "status": RunStatus.RECEIVED.value,
        "version": 1,
    }
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(),
    )

    assert repository.prior_item["status"] == "OPEN"
    assert [event[0]["event_type"] for event in repository.events] == ["agent.run.status_changed"]


def test_earlier_scheduler_success_does_not_supersede_a_later_failed_occurrence():
    repository = _Repository()
    repository.prior_command["parameters_json"]["execution_context"]["scheduled_for"] = (
        "2026-08-24T00:00:00+00:00"
    )
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(),
    )

    assert repository.prior_item["status"] == "OPEN"
    assert [event[0]["event_type"] for event in repository.events] == ["agent.run.status_changed"]


def test_scheduler_success_drains_older_failures_in_bounded_transactions():
    repository = _Repository()
    repository.add_prior_failures(249)
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(),
    )

    assert [len(batch) for batch in repository.selector_batches] == [100, 100, 50, 0]
    assert repository.commits == 3
    assert all(item["status"] == "CANCELLED" for item in repository.prior_items.values())


def test_requery_after_no_progress_drains_rows_beyond_an_invalidated_first_page():
    repository = _Repository()
    repository.add_prior_failures(249)
    repository.invalidate_first_selector_batch = True
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(),
    )

    assert [len(batch) for batch in repository.selector_batches] == [100, 100, 50, 0]
    assert repository.commits == 3
    invalidated_items = {
        run["work_item_id"]
        for run in repository.prior_runs
        if run["run_id"] in repository.invalidated_run_ids
    }
    assert all(repository.prior_items[item_id]["status"] == "OPEN" for item_id in invalidated_items)
    assert all(
        item["status"] == "CANCELLED"
        for item_id, item in repository.prior_items.items()
        if item_id not in invalidated_items
    )


def test_ambiguous_prior_occurrence_stays_open_and_stops_the_drain():
    repository = _Repository()
    context = repository.prior_command["parameters_json"]["execution_context"]
    context.pop("scheduled_for")
    repository.prior_command["requested_at"] = None
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(),
    )

    assert repository.prior_item["status"] == "OPEN"
    assert [len(batch) for batch in repository.selector_batches] == [0, 0]
    assert repository.commits == 1


def test_post_commit_drain_failure_leaves_the_completed_run_unchanged():
    repository = _Repository()
    repository.fail_selector_on_call = 2
    runner = WorkflowRunner.__new__(WorkflowRunner)
    runner._repository = repository

    completed = runner._complete_run_and_supersede_scheduler_failures(
        copy.deepcopy(repository.run),
        _scheduler_command(),
    )

    assert completed["status"] == RunStatus.COMPLETED.value
    assert repository.current_item["status"] == "RESOLVED"
    assert repository.commits == 1
