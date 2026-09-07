"""Real MySQL scheduler supersession and cancellation races."""
from __future__ import annotations

from datetime import datetime
import threading
from uuid import uuid4


def run_test_scheduler_supersession_selector_is_exact_and_terminal_retry_observes_cancellation(case):
        """Exercise the selector and its Run -> Command -> WorkItem lock order."""

        from shared.automation_project_authorization import (
            AutomationEntrypoint,
            AutomationProjectInvocation,
        )
        from shared.orchestration_repository import InvalidStateError

        repository = case._repository()
        task_id = f"scheduler-supersession-{uuid4()}"
        automation_id = f"project_{uuid4().hex}"

        def create_scheduler_run(
            label: str,
            *,
            task: str,
            project: str,
            run_status: str,
            work_item_status: str = "OPEN",
            scheduled_for: str = "2026-08-22T00:00:00+00:00",
        ) -> dict[str, str]:
            command, item, run, event, outbox = case._aggregate_rows(label)
            invocation = AutomationProjectInvocation(
                automation_id=project,
                automation_generation=1,
                entrypoint=AutomationEntrypoint.SCHEDULER,
                contract_id=f"integration-contract-{project}",
                contract_hash="a" * 64,
                policy_version=1,
                project_configuration_version=1,
                request_id=f"scheduler:{task}:{uuid4()}",
            )
            command.update(
                {
                    "command_type": "automation.project.invoke",
                    "source": "scheduler",
                    "actor_type": "scheduler",
                    "actor_id": task,
                    "parameters": {
                        "tool_name": f"automation.{project}.run",
                        "arguments": {},
                        "execution_context": {
                            "task_id": task,
                            "scheduled_for": scheduled_for,
                        },
                    },
                    "automation_id": project,
                    "automation_generation": 1,
                    "automation_invocation": invocation.to_dict(),
                }
            )
            item["source"] = "scheduler"
            with repository.unit_of_work() as uow:
                receipt = uow.command_gateway_create(command, item, run, event, outbox)
                uow.commit()
            with case._connection(autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_runs SET status=%s WHERE run_id=%s",
                    (run_status, receipt["run_id"]),
                )
                cursor.execute(
                    "UPDATE work_items SET status=%s WHERE work_item_id=%s",
                    (work_item_status, receipt["work_item_id"]),
                )
            return {
                "run_id": str(receipt["run_id"]),
                "work_item_id": str(receipt["work_item_id"]),
            }

        exact = create_scheduler_run(
            "scheduler-supersession-exact",
            task=task_id,
            project=automation_id,
            run_status="FAILED_TERMINAL",
        )
        create_scheduler_run(
            "scheduler-supersession-other-task",
            task=f"other-{task_id}",
            project=automation_id,
            run_status="FAILED_TERMINAL",
        )
        create_scheduler_run(
            "scheduler-supersession-other-project",
            task=task_id,
            project=f"other_{automation_id}",
            run_status="FAILED_TERMINAL",
        )
        blocked = create_scheduler_run(
            "scheduler-supersession-blocked",
            task=task_id,
            project=automation_id,
            run_status="BLOCKED_DATA",
            work_item_status="BLOCKED_DATA",
        )
        unknown = create_scheduler_run(
            "scheduler-supersession-unknown",
            task=task_id,
            project=automation_id,
            run_status="BLOCKED_DATA",
            work_item_status="BLOCKED_DATA",
        )
        with case._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE agent_runs SET error_code='WRITE_OUTCOME_UNKNOWN' WHERE run_id=%s",
                (unknown["run_id"],),
            )
        nonterminal = create_scheduler_run(
            "scheduler-supersession-nonterminal",
            task=task_id,
            project=automation_id,
            run_status="RUNNING",
            work_item_status="IN_PROGRESS",
        )
        superseded_by_retry = create_scheduler_run(
            "scheduler-supersession-latest",
            task=task_id,
            project=automation_id,
            run_status="FAILED_TERMINAL",
        )
        repository.create_linked_retry_run(
            superseded_by_retry["run_id"],
            new_run_id=str(uuid4()),
            new_command_id=str(uuid4()),
        )
        reverse_completion = create_scheduler_run(
            "scheduler-supersession-reverse-completion",
            task=task_id,
            project=automation_id,
            run_status="FAILED_TERMINAL",
            scheduled_for="2026-08-24T00:00:00+00:00",
        )

        with repository.unit_of_work() as uow:
            selected = uow.runs.list_open_failed_scheduler_run_ids_for_supersession(
                automation_id=automation_id,
                scheduler_task_id=task_id,
                successful_work_item_id=str(uuid4()),
                successful_occurrence=datetime(2026, 8, 23, 0, 0),
            )
        case.assertEqual([exact["run_id"]], selected)
        case.assertNotIn(blocked["run_id"], selected)
        case.assertNotIn(unknown["run_id"], selected)
        case.assertNotIn(nonterminal["run_id"], selected)
        case.assertNotIn(reverse_completion["run_id"], selected)

        reverse_task_id = f"scheduler-supersession-race-{uuid4()}"
        reverse_project_id = f"project_{uuid4().hex}"
        reverse_race = create_scheduler_run(
            "scheduler-supersession-reverse-race",
            task=reverse_task_id,
            project=reverse_project_id,
            run_status="FAILED_TERMINAL",
        )
        with repository.unit_of_work() as cleanup_uow:
            candidate_ids = cleanup_uow.runs.list_open_failed_scheduler_run_ids_for_supersession(
                automation_id=reverse_project_id,
                scheduler_task_id=reverse_task_id,
                successful_work_item_id=str(uuid4()),
                successful_occurrence=datetime(2026, 8, 23, 0, 0),
            )
            case.assertEqual([reverse_race["run_id"]], candidate_ids)
            child = repository.create_linked_retry_run(
                reverse_race["run_id"],
                new_run_id=str(uuid4()),
                new_command_id=str(uuid4()),
            )
            source = cleanup_uow.runs.get(reverse_race["run_id"], for_update=True)
            case.assertIsNotNone(source)
            latest = cleanup_uow.runs.get_latest_for_work_item(
                reverse_race["work_item_id"],
                for_update=True,
            )
            case.assertIsNotNone(latest)
            case.assertEqual(child["run_id"], latest["run_id"])
            case.assertNotEqual(source["run_id"], latest["run_id"])
        with case._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM work_items WHERE work_item_id=%s",
                (reverse_race["work_item_id"],),
            )
            case.assertEqual("OPEN", cursor.fetchone()["status"])
            cursor.execute(
                "SELECT status FROM agent_runs WHERE run_id=%s",
                (child["run_id"],),
            )
            case.assertEqual("RECEIVED", cursor.fetchone()["status"])

        source_locked = threading.Event()
        release_source = threading.Event()
        outcomes: list[str] = []
        unexpected: list[BaseException] = []

        def supersede() -> None:
            try:
                with repository.unit_of_work() as uow:
                    source = uow.runs.get(exact["run_id"], for_update=True)
                    case.assertIsNotNone(source)
                    source_locked.set()
                    case.assertTrue(release_source.wait(10))
                    command = uow.commands.get(str(source["command_id"]), for_update=True)
                    case.assertIsNotNone(command)
                    item = uow.work_items.get(exact["work_item_id"], for_update=True)
                    case.assertIsNotNone(item)
                    uow.work_items.transition(
                        exact["work_item_id"],
                        expected_version=int(item["version"]),
                        expected_statuses=("OPEN",),
                        status="CANCELLED",
                        reason_code="SUPERSEDED_BY_LATER_SUCCESS",
                        reason_summary="已由后续成功运行取代",
                        resolution={"successful_run_id": "successful-run"},
                        closed_at=datetime.now(),
                    )
                    uow.commit()
                outcomes.append("superseded")
            except BaseException as exc:  # pragma: no cover - surfaced below
                unexpected.append(exc)

        def retry() -> None:
            try:
                repository.create_linked_retry_run(
                    exact["run_id"],
                    new_run_id=str(uuid4()),
                    new_command_id=str(uuid4()),
                )
                outcomes.append("retry-created")
            except InvalidStateError:
                outcomes.append("retry-rejected")
            except BaseException as exc:  # pragma: no cover - surfaced below
                unexpected.append(exc)

        supersede_thread = threading.Thread(target=supersede, daemon=True)
        retry_thread = threading.Thread(target=retry, daemon=True)
        supersede_thread.start()
        case.assertTrue(source_locked.wait(10))
        retry_thread.start()
        release_source.set()
        supersede_thread.join(10)
        retry_thread.join(10)

        case.assertFalse(supersede_thread.is_alive() or retry_thread.is_alive())
        case.assertEqual([], unexpected)
        case.assertCountEqual(["superseded", "retry-rejected"], outcomes)
        with case._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM work_items WHERE work_item_id=%s",
                (exact["work_item_id"],),
            )
            case.assertEqual("CANCELLED", cursor.fetchone()["status"])
