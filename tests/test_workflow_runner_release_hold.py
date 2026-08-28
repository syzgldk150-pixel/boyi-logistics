from __future__ import annotations

import asyncio
import threading
import unittest
from dataclasses import replace

from agent.orchestration.models import (
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
)
from agent.orchestration.workflow_runner import WorkflowRunner


class _ClaimRepository:
    def __init__(self) -> None:
        self.cancel_claims = 0
        self.run_claims = 0

    def claim_cancel_requested_runs(self, *_args, **_kwargs):
        self.cancel_claims += 1
        return []

    def claim_runs(self, *_args, **_kwargs):
        self.run_claims += 1
        return []


class _ConcurrentClaimRepository(_ClaimRepository):
    def __init__(self) -> None:
        super().__init__()
        self._pending = [{"run_id": "run-1"}, {"run_id": "run-2"}]
        self._lock = threading.Lock()

    def claim_runs(self, *_args, **_kwargs):
        with self._lock:
            self.run_claims += 1
            return [self._pending.pop(0)] if self._pending else []


def _runner(repository: _ClaimRepository) -> WorkflowRunner:
    return WorkflowRunner(
        repository=repository,
        catalog=None,
        execution_port=None,
        context_builder=None,
        planner=None,
        validator=None,
        policy=None,
        approval_service=None,
        verifier=None,
        worker_id="release-hold-test",
        poll_interval_seconds=0.1,
    )


def _step(
    *,
    operation: OperationType,
    account_id: str | None,
    tool_name: str = "test_tool",
) -> PlanStep:
    return PlanStep(
        step_key="step-1",
        tool_name=tool_name,
        tool_version="1.0.0",
        operation_type=operation,
        arguments={"account_id": account_id} if account_id else {},
        account_id=account_id,
        depends_on=(),
        idempotency_key="step-idempotency",
        expected_evidence=(),
        postconditions=(),
    )


def _plan(step: PlanStep, *, source_system: str = "ronghui") -> Plan:
    return Plan(
        command_type="run_tool",
        context_fingerprint="context",
        tool_catalog_hash="catalog",
        steps=(step,),
        impact={
            "entities": [
                {
                    "entity_type": "waybill",
                    "entity_id": "R001",
                    "source_system": source_system,
                    "metadata": {"action": "update"},
                }
            ]
        },
    )


class WorkflowRunnerReleaseHoldTests(unittest.TestCase):
    def test_default_pool_processes_two_claimed_runs_concurrently(self):
        async def exercise() -> None:
            repository = _ConcurrentClaimRepository()
            runner = _runner(repository)
            both_started = asyncio.Event()
            release = asyncio.Event()
            active: set[str] = set()

            async def process_claimed(run):
                active.add(str(run["run_id"]))
                if len(active) == 2:
                    both_started.set()
                await release.wait()
                active.discard(str(run["run_id"]))

            runner._process_claimed = process_claimed
            await runner.start()
            try:
                await asyncio.wait_for(both_started.wait(), timeout=1)
                self.assertEqual({"run-1", "run-2"}, active)
            finally:
                release.set()
                await runner.stop()

        asyncio.run(exercise())

    def test_held_start_does_not_claim_until_explicit_activation(self):
        async def exercise() -> None:
            repository = _ClaimRepository()
            runner = _runner(repository)
            await runner.start(held_for_release=True)
            try:
                await asyncio.sleep(0.15)
                self.assertEqual(0, repository.cancel_claims)
                self.assertEqual(0, repository.run_claims)
                self.assertEqual(
                    {
                        "state": "held",
                        "release_hold": True,
                        "active_runs": 0,
                    },
                    runner.runtime_status(),
                )

                status = runner.resume_after_release()
                self.assertEqual("running", status["state"])
                await asyncio.sleep(0.05)
                self.assertGreater(repository.cancel_claims, 0)
                self.assertGreater(repository.run_claims, 0)
            finally:
                await runner.stop()

        asyncio.run(exercise())

    def test_unstarted_runner_cannot_be_activated(self):
        runner = _runner(_ClaimRepository())
        with self.assertRaisesRegex(RuntimeError, "not available"):
            runner.resume_after_release()

    def test_worker_thread_wake_is_delivered_to_the_owner_event_loop(self):
        async def exercise() -> None:
            repository = _ClaimRepository()
            runner = _runner(repository)
            runner._poll_interval_seconds = 30
            await runner.start(held_for_release=True)
            try:
                await asyncio.sleep(0.05)
                await asyncio.to_thread(runner.resume_after_release)
                await asyncio.sleep(0.05)
                self.assertGreater(repository.run_claims, 0)
            finally:
                await runner.stop()

        asyncio.run(exercise())

    def test_same_account_write_is_serialized_without_blocking_another_account(self):
        async def exercise() -> None:
            runner = _runner(_ClaimRepository())
            first_step = _step(
                operation=OperationType.EXTERNAL_WRITE,
                account_id="account-a",
            )
            first_release = await runner._acquire_execution_slot(
                first_step,
                _plan(first_step),
                {},
            )
            same_account = asyncio.create_task(
                runner._acquire_execution_slot(
                    first_step,
                    _plan(first_step),
                    {},
                )
            )
            other_step = _step(
                operation=OperationType.EXTERNAL_WRITE,
                account_id="account-b",
            )
            other_release = await asyncio.wait_for(
                runner._acquire_execution_slot(
                    other_step,
                    _plan(other_step),
                    {},
                ),
                timeout=0.2,
            )
            await asyncio.sleep(0)
            self.assertFalse(same_account.done())

            other_release()
            first_release()
            second_release = await asyncio.wait_for(same_account, timeout=0.2)
            second_release()
            self.assertFalse(runner._execution_locks)

        asyncio.run(exercise())

    def test_plugin_write_uses_signed_account_and_resource_bindings(self):
        runner = _runner(_ClaimRepository())
        step = _step(
            operation=OperationType.EXTERNAL_WRITE,
            account_id=None,
            tool_name="automation.bound.run",
        )
        capability = {
            "_plugin_runtime": {
                "automation_id": "bound",
                "core_tool_name": "clock_in_dual",
                "account_bindings": {"primary": "account-bound"},
                "resource_bindings": {"site": "site-42"},
                "runtime_permissions": {"browser": False},
            }
        }

        keys = runner._execution_lock_keys(
            step,
            Plan(
                command_type="automation.project.invoke",
                context_fingerprint="context",
                tool_catalog_hash="catalog",
                steps=(step,),
                impact={"entities": []},
            ),
            capability,
        )

        self.assertIn(("account-write", "account-bound"), keys)
        self.assertIn(
            (
                "resource-write",
                "account-bound",
                "clock_in_dual",
                "site",
                "site-42",
                "external_write",
            ),
            keys,
        )

    def test_browser_steps_are_single_concurrency_even_when_reads(self):
        async def exercise() -> None:
            runner = _runner(_ClaimRepository())
            browser_step = _step(
                operation=OperationType.READ,
                account_id=None,
                tool_name="automation.browser.read",
            )
            plan = _plan(browser_step)
            capability = {
                "_plugin_runtime": {
                    "runtime_permissions": {"browser": True},
                }
            }
            first_release = await runner._acquire_execution_slot(
                browser_step,
                plan,
                capability,
            )
            second = asyncio.create_task(
                runner._acquire_execution_slot(browser_step, plan, capability)
            )
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            first_release()
            second_release = await asyncio.wait_for(second, timeout=0.2)
            second_release()

        asyncio.run(exercise())

    def test_core_browser_tools_share_the_default_single_browser_lane(self):
        async def exercise() -> None:
            runner = _runner(_ClaimRepository())
            price_step = _step(
                operation=OperationType.READ,
                account_id=None,
                tool_name="get_price",
            )
            clock_step = _step(
                operation=OperationType.EXTERNAL_WRITE,
                account_id="clock-account",
                tool_name="clock_in_dual",
            )
            price_release = await runner._acquire_execution_slot(
                price_step,
                _plan(price_step),
                {"heavy": True},
            )
            clock_slot = asyncio.create_task(
                runner._acquire_execution_slot(
                    clock_step,
                    _plan(clock_step),
                    {"heavy": False},
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(clock_slot.done())

            price_release()
            clock_release = await asyncio.wait_for(clock_slot, timeout=0.2)
            clock_release()

            ocr_step = _step(
                operation=OperationType.READ,
                account_id=None,
                tool_name="ocr_recognize",
            )
            self.assertFalse(
                runner._is_browser_step(ocr_step, {"heavy": True})
            )
            query_status = _step(
                operation=OperationType.READ,
                account_id=None,
                tool_name="query_waybill",
            )
            query_detail = replace(
                query_status,
                arguments={"query_type": "detail"},
            )
            self.assertFalse(runner._is_browser_step(query_status, {}))
            self.assertTrue(runner._is_browser_step(query_detail, {}))

        asyncio.run(exercise())

    def test_external_write_without_exact_lock_identity_fails_explicitly(self):
        runner = _runner(_ClaimRepository())
        step = _step(
            operation=OperationType.EXTERNAL_WRITE,
            account_id=None,
        )
        plan = Plan(
            command_type="run_tool",
            context_fingerprint="context",
            tool_catalog_hash="catalog",
            steps=(step,),
            impact={"entities": []},
        )
        with self.assertRaises(OrchestrationError) as captured:
            asyncio.run(runner._acquire_execution_slot(step, plan, {}))
        self.assertEqual("EXECUTION_LOCK_CONTEXT_REQUIRED", captured.exception.code)


if __name__ == "__main__":
    unittest.main()
