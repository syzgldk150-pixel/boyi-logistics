from __future__ import annotations

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

    def test_completed_finance_run_is_consumed_by_finance_brain(self):
        from main import _finance_brain_completed_handler

        process_after_sync = Mock(return_value="finance-post-sync-coroutine")
        submitted = Mock()
        submitted.result.return_value = {
            "notified": 1,
            "analysis": {"status": "complete", "processed": 2},
        }
        delivery = {
            "event_id": "00000000-0000-0000-0000-000000000003",
            "event_type": "agent.run.completed",
            "payload_json": {
                "run_id": "00000000-0000-0000-0000-000000000004",
                "status": "COMPLETED",
                "tool_names": ["sync_finance_bills"],
            },
        }
        runtime = SimpleNamespace(
            finance_brain=SimpleNamespace(process_after_sync=process_after_sync)
        )

        with patch(
            "main.asyncio.run_coroutine_threadsafe",
            return_value=submitted,
        ) as submit:
            result = _finance_brain_completed_handler(
                runtime,
                "main-loop",
                delivery,
                object(),
            )

        process_after_sync.assert_called_once_with()
        submit.assert_called_once_with("finance-post-sync-coroutine", "main-loop")
        submitted.result.assert_called_once_with(timeout=1800)
        self.assertTrue(result["processed"])
        self.assertEqual(1, result["result"]["notified"])

    def test_non_finance_completion_does_not_invoke_finance_brain(self):
        from main import _finance_brain_completed_handler

        result = _finance_brain_completed_handler(
            SimpleNamespace(finance_brain=None),
            object(),
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
