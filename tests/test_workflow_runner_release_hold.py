from __future__ import annotations

import asyncio
import unittest

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


class WorkflowRunnerReleaseHoldTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
