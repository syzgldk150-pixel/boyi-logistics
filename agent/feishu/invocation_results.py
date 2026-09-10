"""Follow one accepted invocation; stored execution facts are never resubmitted."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from agent.orchestration.models import OrchestrationError
from shared.plugin_invocation_repository import ACTIVE_INVOCATION_STATUSES, TERMINAL_INVOCATION_STATUSES

logger = logging.getLogger("feishu")


class InvocationResultFollower:
    """Wait for one accepted call, with bounded retries for unavailable reads."""

    def __init__(self, *, retry_delay: float = 1.0) -> None:
        self._retry_delay = retry_delay

    async def invoke(self, *, service, event_id: str, sender_id: str, chat_id: str,
                     submit, on_accepted, on_waiting) -> dict[str, Any] | None:
        receipt = None
        accepted = False

        async def remember(value):
            nonlocal receipt, accepted
            accepted = True
            receipt = dict(value) if isinstance(value, dict) else None
            await on_accepted(value)

        try:
            result = await submit(remember)
        except Exception:
            if receipt is None:
                raise
            result = None
        if result is None and not accepted:
            return None  # The dynamic command did not match a plugin.
        if receipt is not None:
            identity = receipt.get("invocation_id")
            try:
                valid_id = str(UUID(identity)) == identity
            except (ValueError, TypeError, AttributeError):
                valid_id = False
            if not valid_id or not receipt.get("automation_id"):
                raise OrchestrationError("INVOCATION_IDENTITY_INVALID", "本次执行记录标识无效")
        if result is not None:
            if not isinstance(result, dict):
                raise OrchestrationError("INVOCATION_RESULT_INVALID", "本次执行返回了无效结果")
            self._validate_identity(result, receipt)
            if result.get("status") not in ACTIVE_INVOCATION_STATUSES:
                return result  # Keep existing terminal/rejection reply rendering.
        if receipt is None:
            raise OrchestrationError("INVOCATION_IDENTITY_INVALID", "本次执行缺少可查询的记录标识")
        try:
            await on_waiting()
        except Exception as exc:
            # A notification failure cannot abandon an accepted execution.
            logger.warning("Invocation waiting reply unavailable | error_type=%s", type(exc).__name__)
        failures = 0
        while True:
            try:
                result = await service.wait_feishu_invocation(
                    invocation_id=receipt["invocation_id"],
                    automation_id=receipt["automation_id"],
                    event_id=event_id, sender_id=sender_id, chat_id=chat_id,
                    timeout_seconds=30.0,
                )
                if not isinstance(result, dict) or result.get("status") not in ACTIVE_INVOCATION_STATUSES | TERMINAL_INVOCATION_STATUSES:
                    raise OrchestrationError("INVOCATION_RESULT_INVALID", "本次执行返回了无效状态")
                self._validate_identity(result, receipt)
            except OrchestrationError as exc:
                if exc.code in {"ACTOR_NOT_AUTHORIZED", "TRUSTED_ENTRYPOINT_REQUIRED", "INVOCATION_NOT_FOUND", "INVOCATION_IDENTITY_INVALID", "INVOCATION_RESULT_INVALID"}:
                    raise
                failures += 1
            except Exception:
                failures += 1
            else:
                failures = 0
                if result["status"] in TERMINAL_INVOCATION_STATUSES:
                    return result
            if failures >= 3:
                raise OrchestrationError("INVOCATION_RESULT_UNAVAILABLE", "本次执行结果连续读取失败，请在自动化页面查看")
            await asyncio.sleep(self._retry_delay * max(1, failures))

    @staticmethod
    def _validate_identity(result, receipt) -> None:
        if receipt is not None and any(result.get(field) != receipt.get(field) for field in ("invocation_id", "automation_id")):
            raise OrchestrationError("INVOCATION_IDENTITY_INVALID", "执行结果与本次调用不匹配")
