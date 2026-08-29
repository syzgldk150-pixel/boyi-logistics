from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


class _RunRows:
    def __init__(self, run):
        self.run = dict(run)

    def get(self, run_id, *, for_update=False):
        del for_update
        return dict(self.run) if run_id == self.run.get("run_id") else None


class _EventCapture:
    def __init__(self):
        self.calls = []

    def append_with_outbox(self, event, deliveries):
        event = dict(event)
        deliveries = tuple(dict(item) for item in deliveries)
        self.calls.append((event, deliveries))
        return {"event": {**event, "event_id": event["event_id"]}, "outbox": deliveries}


def _projection_uow(*, startup_catchup=False, tool_name="sync_finance_bills"):
    run = {
        "run_id": "00000000-0000-0000-0000-000000000002",
        "work_item_id": "00000000-0000-0000-0000-000000000010",
        "correlation_id": "00000000-0000-0000-0000-000000000011",
        "status": "FAILED_TERMINAL",
        "plan_json": {
            "steps": [
                {
                    "tool_name": tool_name,
                    "arguments": {"_startup_catchup": startup_catchup},
                }
            ]
        },
    }
    events = _EventCapture()
    return SimpleNamespace(runs=_RunRows(run), events=events), events


class _ConsumedOutbox:
    def __init__(self):
        self.consumed = set()
        self.published = []

    def was_consumed(self, *, consumer_name, event_id):
        return (consumer_name, event_id) in self.consumed

    def record_consumption(self, *, consumer_name, event_id, result_summary):
        del result_summary
        self.consumed.add((consumer_name, event_id))

    def mark_published(self, outbox_id, *, worker_id):
        self.published.append((outbox_id, worker_id))


class _UnitOfWork:
    def __init__(self, outbox):
        self.outbox = outbox
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def commit(self):
        self.commits += 1


class _OutboxRepository:
    def __init__(self):
        self.outbox = _ConsumedOutbox()

    def unit_of_work(self):
        return _UnitOfWork(self.outbox)


class FinanceRunEventPolicyTests(unittest.TestCase):
    @staticmethod
    def _failure_delivery(*, startup_catchup=False):
        return {
            "outbox_id": 41,
            "consumer_name": "finance.failure_alert",
            "event_id": "00000000-0000-0000-0000-000000000020",
            "event_type": "finance.sync.failed",
            "run_id": "00000000-0000-0000-0000-000000000002",
            "payload_json": {
                "run_id": "00000000-0000-0000-0000-000000000002",
                "status": "FAILED_TERMINAL",
                "tool_name": "sync_finance_bills",
                "error_code": "FINANCE_SYNC_INTERNAL",
                "error_summary": "capture failed",
                "startup_catchup": startup_catchup,
            },
        }

    def test_failed_startup_catchup_projects_typed_event_but_suppresses_alert(self):
        from agent.orchestration.outbox_dispatcher import OutboxDispatcher
        from main import _finance_sync_failure_handler, _project_run_completed_event

        uow, events = _projection_uow(startup_catchup=True)

        result = _project_run_completed_event(
            {
                "event_id": "00000000-0000-0000-0000-000000000001",
                "event_type": "agent.run.status_changed",
                "run_id": "00000000-0000-0000-0000-000000000002",
                "correlation_id": "00000000-0000-0000-0000-000000000011",
                "payload_json": {
                    "from": "RUNNING",
                    "to": "FAILED_TERMINAL",
                    "error_code": "FINANCE_SYNC_INTERNAL",
                },
            },
            uow,
        )

        self.assertTrue(result["projected"])
        event, deliveries = events.calls[0]
        self.assertEqual("finance.sync.failed", event["event_type"])
        self.assertTrue(event["payload"]["startup_catchup"])
        self.assertEqual("finance.failure_alert", deliveries[0]["consumer_name"])

        repository = _OutboxRepository()
        dispatcher = OutboxDispatcher(
            repository,
            worker_id="test-worker",
            handlers={"finance.failure_alert": _finance_sync_failure_handler},
        )
        delivery = self._failure_delivery(startup_catchup=True)
        with patch("main.publish_finance_alert") as publish:
            dispatcher._deliver(delivery)
            dispatcher._deliver(delivery)
        publish.assert_not_called()
        self.assertEqual(
            {("finance.failure_alert", delivery["event_id"])},
            repository.outbox.consumed,
        )

    def test_regular_finance_failure_alert_is_redacted_and_consumed_once(self):
        from agent.orchestration.outbox_dispatcher import OutboxDispatcher
        from main import _finance_sync_failure_handler, _project_run_completed_event

        uow, events = _projection_uow()
        result = _project_run_completed_event(
            {
                "event_id": "00000000-0000-0000-0000-000000000001",
                "event_type": "agent.run.status_changed",
                "run_id": "00000000-0000-0000-0000-000000000002",
                "correlation_id": "00000000-0000-0000-0000-000000000011",
                "payload_json": {
                    "from": "RUNNING",
                    "to": "FAILED_TERMINAL",
                    "error_code": "FINANCE_SYNC_INTERNAL",
                    "error_summary": "password=dummy-finance-secret",
                },
            },
            uow,
        )

        self.assertTrue(result["projected"])
        event, _deliveries = events.calls[0]
        self.assertNotIn("dummy-finance-secret", str(event))
        self.assertIn("[REDACTED]", event["payload"]["error_summary"])

        repository = _OutboxRepository()
        dispatcher = OutboxDispatcher(
            repository,
            worker_id="test-worker",
            handlers={"finance.failure_alert": _finance_sync_failure_handler},
        )
        delivery = {
            **self._failure_delivery(),
            "event_id": event["event_id"],
            "payload_json": event["payload"],
        }
        with patch("main.publish_finance_alert", return_value=True) as publish:
            dispatcher._deliver(delivery)
            dispatcher._deliver(delivery)

        publish.assert_called_once()
        alert = publish.call_args.args[0]
        self.assertEqual("FINANCE_SYNC_INTERNAL", alert["anomaly_type"])
        self.assertNotIn("dummy-finance-secret", str(alert))
        self.assertEqual(
            {("finance.failure_alert", event["event_id"])},
            repository.outbox.consumed,
        )

    def test_non_finance_failure_does_not_project_finance_event(self):
        from main import _project_run_completed_event

        uow, events = _projection_uow(tool_name="sync_daily_should_sign")
        result = _project_run_completed_event(
            {
                "event_id": "00000000-0000-0000-0000-000000000001",
                "event_type": "agent.run.status_changed",
                "run_id": "00000000-0000-0000-0000-000000000002",
                "payload_json": {
                    "from": "RUNNING",
                    "to": "BLOCKED_DATA",
                    "error_code": "SOURCE_INCOMPLETE",
                },
            },
            uow,
        )

        self.assertFalse(result["projected"])
        self.assertEqual([], events.calls)

    def test_finance_blocked_status_projects_failure_alert_event(self):
        from main import _project_run_completed_event

        for status in ("BLOCKED_LOGIN", "BLOCKED_DATA"):
            with self.subTest(status=status):
                uow, events = _projection_uow()
                result = _project_run_completed_event(
                    {
                        "event_id": f"00000000-0000-0000-0000-{status.lower()}",
                        "event_type": "agent.run.status_changed",
                        "run_id": "00000000-0000-0000-0000-000000000002",
                        "payload_json": {
                            "from": "RUNNING",
                            "to": status,
                            "error_code": "AUTH_REQUIRED"
                            if status == "BLOCKED_LOGIN"
                            else "SOURCE_INCOMPLETE",
                        },
                    },
                    uow,
                )

                self.assertTrue(result["projected"])
                event, deliveries = events.calls[0]
                self.assertEqual(status, event["payload"]["status"])
                self.assertEqual("finance.failure_alert", deliveries[0]["consumer_name"])

    def test_completed_finance_run_submits_idempotent_analysis_without_waiting(self):
        from main import _finance_brain_completed_handler
        from agent.orchestration.models import CommandReceipt, RunStatus

        delivery = {
            "event_id": "00000000-0000-0000-0000-000000000003",
            "event_type": "agent.run.completed",
            "payload_json": {
                "run_id": "00000000-0000-0000-0000-000000000004",
                "status": "COMPLETED",
                "tool_names": ["sync_finance_bills"],
            },
        }
        commands = []

        def submit(command):
            commands.append(command)
            return CommandReceipt(
                command_id="00000000-0000-0000-0000-000000000010",
                work_item_id="00000000-0000-0000-0000-000000000011",
                run_id="00000000-0000-0000-0000-000000000012",
                status=RunStatus.RECEIVED,
                reused=len(commands) > 1,
            )

        first = _finance_brain_completed_handler(submit, delivery, object())
        second = _finance_brain_completed_handler(submit, delivery, object())

        self.assertTrue(first["processed"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(2, len(commands))
        self.assertEqual(commands[0].idempotency_key, commands[1].idempotency_key)
        self.assertEqual(
            "finance-analysis:v1:00000000-0000-0000-0000-000000000003",
            commands[0].idempotency_key,
        )
        self.assertEqual(
            {
                "tool_name": "analyze_finance_reviews",
                "arguments": {
                    "trigger_id": "00000000-0000-0000-0000-000000000003",
                    "source_run_id": "00000000-0000-0000-0000-000000000004",
                    "limit": 20,
                },
            },
            commands[0].parameters,
        )
        self.assertEqual("finance_review_queue", commands[0].entity_refs[0].entity_type)
        self.assertEqual("pending", commands[0].entity_refs[0].entity_id)
        self.assertEqual("system", commands[0].source)
        self.assertEqual("system", commands[0].actor.actor_type.value)
        self.assertIsNone(commands[0].automation_invocation)

    def test_manual_finance_analysis_returns_durable_run_receipt(self):
        from main import FinanceAnalyzeRequest, internal_analyze_finance_reviews
        from agent.orchestration.models import CommandReceipt, RunStatus

        receipt = CommandReceipt(
            command_id="00000000-0000-0000-0000-000000000020",
            work_item_id="00000000-0000-0000-0000-000000000021",
            run_id="00000000-0000-0000-0000-000000000022",
            status=RunStatus.RECEIVED,
            reused=False,
        )
        runtime = SimpleNamespace(submit_command=Mock(return_value=receipt))
        request = SimpleNamespace(
            state=SimpleNamespace(
                console_principal={
                    "actor_type": "console_admin",
                    "actor_id": "42",
                    "roles": ["admin"],
                    "authenticated_by": "console_identity",
                }
            )
        )

        with patch("main._runtime", return_value=runtime):
            response = asyncio.run(
                internal_analyze_finance_reviews(FinanceAnalyzeRequest(limit=7), request)
            )

        self.assertEqual(202, response.status_code)
        self.assertEqual(
            "/internal/v1/runs/00000000-0000-0000-0000-000000000022",
            response.headers["location"],
        )
        payload = json.loads(response.body)
        self.assertEqual(receipt.run_id, payload["data"]["run_id"])
        command = runtime.submit_command.call_args.args[0]
        self.assertEqual("console", command.source)
        self.assertEqual("analyze_finance_reviews", command.parameters["tool_name"])
        self.assertEqual(7, command.parameters["arguments"]["limit"])

    def test_non_finance_completion_does_not_invoke_finance_brain(self):
        from main import _finance_brain_completed_handler

        result = _finance_brain_completed_handler(
            Mock(side_effect=AssertionError("must not submit")),
            {
                "event_id": "00000000-0000-0000-0000-000000000005",
                "event_type": "agent.run.completed",
                "payload_json": {"tool_names": ["sync_daily_should_sign"]},
            },
            object(),
        )

        self.assertFalse(result["processed"])


if __name__ == "__main__":
    unittest.main()
