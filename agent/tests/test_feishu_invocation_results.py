from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.orchestration.models import OrchestrationError
from feishu.invocation_results import InvocationResultFollower


INVOCATION_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
RECEIPT = {"invocation_id": INVOCATION_ID, "automation_id": "automation.scan_codes", "status": "RUNNING"}


def _arguments(service, submit, **overrides):
    return {
        "service": service, "event_id": "event-one", "sender_id": "sender-one", "chat_id": "chat-one",
        "submit": submit, "on_accepted": AsyncMock(), "on_waiting": AsyncMock(), **overrides,
    }


@pytest.mark.parametrize("initial", ["active", "timeout", "read_error"])
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED", "WRITE_OUTCOME_UNKNOWN"])
def test_accepted_call_follows_real_terminal_result_without_resubmission(initial, status):
    async def exercise():
        terminal = {**RECEIPT, "status": status, "output": {"actual_result": "preserved"}}
        service = SimpleNamespace(wait_feishu_invocation=AsyncMock(side_effect=[RECEIPT, terminal]))

        async def accept(callback):
            await callback(dict(RECEIPT))
            if initial == "timeout":
                raise OrchestrationError("RUN_WAIT_TIMEOUT", "wait expired")
            if initial == "read_error":
                raise RuntimeError("temporary read failure")
            return dict(RECEIPT)

        submit = AsyncMock(side_effect=accept)
        args = _arguments(service, submit)
        result = await InvocationResultFollower(retry_delay=0).invoke(**args)
        assert result == terminal
        assert submit.await_count == args["on_accepted"].await_count == args["on_waiting"].await_count == 1
        assert service.wait_feishu_invocation.await_count == 2
        assert all(call.kwargs == {
            "invocation_id": INVOCATION_ID, "automation_id": RECEIPT["automation_id"],
            "event_id": "event-one", "sender_id": "sender-one", "chat_id": "chat-one", "timeout_seconds": 30.0,
        } for call in service.wait_feishu_invocation.await_args_list)

    asyncio.run(exercise())


def test_concurrent_calls_for_same_project_keep_their_exact_results():
    async def exercise():
        ready = {INVOCATION_ID: asyncio.Event(), OTHER_ID: asyncio.Event()}
        accepted = {key: asyncio.Event() for key in ready}
        completions = []

        async def wait(**kwargs):
            invocation_id = kwargs["invocation_id"]
            await ready[invocation_id].wait()
            return {**RECEIPT, "invocation_id": invocation_id, "status": "COMPLETED", "output": {"owner": invocation_id}}

        service = SimpleNamespace(wait_feishu_invocation=AsyncMock(side_effect=wait))
        submits = []

        async def run(invocation_id):
            async def accept(callback):
                receipt = {**RECEIPT, "invocation_id": invocation_id}
                await callback(receipt)
                accepted[invocation_id].set()
                return receipt
            submit = AsyncMock(side_effect=accept)
            submits.append(submit)
            result = await InvocationResultFollower(retry_delay=0).invoke(
                **_arguments(service, submit, event_id=invocation_id),
            )
            completions.append(result["invocation_id"])
            return result

        first = asyncio.create_task(run(INVOCATION_ID))
        second = asyncio.create_task(run(OTHER_ID))
        await asyncio.gather(*(value.wait() for value in accepted.values()))
        ready[OTHER_ID].set()
        second_result = await second
        assert not first.done()
        ready[INVOCATION_ID].set()
        first_result = await first
        assert completions == [OTHER_ID, INVOCATION_ID]
        assert first_result["output"]["owner"] == INVOCATION_ID
        assert second_result["output"]["owner"] == OTHER_ID
        assert all(submit.await_count == 1 for submit in submits)

    asyncio.run(exercise())


@pytest.mark.parametrize("failures", [2, 3])
def test_transient_reads_retry_but_unavailable_results_stop_with_explicit_failure(failures):
    async def exercise():
        async def accept(callback):
            await callback(RECEIPT)
            return RECEIPT

        submit = AsyncMock(side_effect=accept)
        terminal = {**RECEIPT, "status": "COMPLETED"}
        service = SimpleNamespace(wait_feishu_invocation=AsyncMock(side_effect=[RuntimeError("read unavailable")] * failures + [terminal]))
        args = _arguments(service, submit)
        if failures == 3:
            with pytest.raises(OrchestrationError) as caught:
                await InvocationResultFollower(retry_delay=0).invoke(**args)
            assert caught.value.code == "INVOCATION_RESULT_UNAVAILABLE"
            assert "连续读取失败" in str(caught.value)
        else:
            assert await InvocationResultFollower(retry_delay=0).invoke(**args) == terminal
        assert service.wait_feishu_invocation.await_count == 3
        assert submit.await_count == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("wrong", [{"invocation_id": OTHER_ID}, {"automation_id": "automation.other"}, {"status": "not-a-status"}])
def test_result_identity_or_state_mismatch_is_not_retried_or_reported_as_completed(wrong):
    async def accept(callback):
        await callback(RECEIPT)
        return RECEIPT

    submit = AsyncMock(side_effect=accept)
    service = SimpleNamespace(wait_feishu_invocation=AsyncMock(return_value={**RECEIPT, "status": "COMPLETED", **wrong}))
    with pytest.raises(OrchestrationError) as caught:
        asyncio.run(InvocationResultFollower(retry_delay=0).invoke(**_arguments(service, submit)))
    assert caught.value.code in {"INVOCATION_IDENTITY_INVALID", "INVOCATION_RESULT_INVALID"}
    assert service.wait_feishu_invocation.await_count == submit.await_count == 1


def test_waiter_shutdown_propagates_cancellation_without_resubmitting_business():
    async def exercise():
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def wait(**_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def accept(callback):
            await callback(RECEIPT)
            return RECEIPT

        service = SimpleNamespace(wait_feishu_invocation=AsyncMock(side_effect=wait))
        submit = AsyncMock(side_effect=accept)
        task = asyncio.create_task(InvocationResultFollower(retry_delay=0).invoke(**_arguments(service, submit)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        assert service.wait_feishu_invocation.await_count == submit.await_count == 1

    asyncio.run(exercise())


def test_waiting_notification_failure_does_not_lose_the_terminal_result():
    async def accept(callback):
        await callback(RECEIPT)
        return RECEIPT

    terminal = {**RECEIPT, "status": "COMPLETED"}
    service = SimpleNamespace(wait_feishu_invocation=AsyncMock(return_value=terminal))
    args = _arguments(service, AsyncMock(side_effect=accept), on_waiting=AsyncMock(side_effect=RuntimeError("reply unavailable")))
    assert asyncio.run(InvocationResultFollower(retry_delay=0).invoke(**args)) == terminal
