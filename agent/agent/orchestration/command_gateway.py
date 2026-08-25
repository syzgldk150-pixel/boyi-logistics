"""Idempotent command acceptance and legacy submit-and-wait compatibility."""

from __future__ import annotations

import asyncio
from datetime import timezone
from typing import Any, Callable, Mapping

from agent.orchestration.models import (
    Command,
    CommandReceipt,
    OrchestrationError,
    RUN_TERMINAL_STATUSES,
    RunStatus,
    new_id,
    sha256_json,
)


WAITING_RUN_STATUSES = frozenset(
    {
        RunStatus.WAITING_APPROVAL,
        RunStatus.NEEDS_CLARIFICATION,
        RunStatus.BLOCKED_LOGIN,
        RunStatus.BLOCKED_DATA,
        RunStatus.FAILED_RETRYABLE,
    }
)


class CommandGateway:
    def __init__(self, repository: Any, *, wake_runner=None, business_module_gate=None) -> None:
        self._repository = repository
        self._wake_runner = wake_runner
        self._business_module_gate = business_module_gate

    def submit(
        self,
        command: Command,
        *,
        uow_guard: Callable[[Any], None] | None = None,
    ) -> CommandReceipt:
        work_item_type, title, dedupe_key = self._classify_work_item(command)
        work_item_id = new_id()
        run_id = new_id()
        event_id = new_id()
        now = command.requested_at.astimezone(timezone.utc).replace(tzinfo=None)

        command_row = {
            "command_id": command.command_id,
            "command_type": command.command_type,
            "source": command.source,
            "actor_type": command.actor.actor_type.value,
            "actor_id": command.actor.actor_id,
            "actor_roles": list(command.actor.roles),
            "entity_refs": [ref.to_dict() for ref in command.entity_refs],
            "parameters": dict(command.parameters),
            "automation_id": (
                command.automation_invocation.automation_id
                if command.automation_invocation is not None
                else None
            ),
            "automation_generation": (
                command.automation_invocation.automation_generation
                if command.automation_invocation is not None
                else None
            ),
            "automation_invocation": (
                command.automation_invocation.to_dict()
                if command.automation_invocation is not None
                else None
            ),
            "idempotency_key": command.idempotency_key,
            "correlation_id": command.correlation_id,
            "status": "RECEIVED",
            "requested_at": now,
        }
        work_item_row = {
            "work_item_id": work_item_id,
            "command_id": command.command_id,
            "type": work_item_type,
            "title": title,
            "status": "OPEN",
            "priority": self._priority_for(command),
            "source": command.source,
            "dedupe_key": dedupe_key,
        }
        run_row = {
            "run_id": run_id,
            "work_item_id": work_item_id,
            "command_id": command.command_id,
            "run_no": 1,
            "status": RunStatus.RECEIVED.value,
            "mode": "COMMAND",
            "planner_kind": "DETERMINISTIC",
            "correlation_id": command.correlation_id,
        }
        event_row = {
            "event_id": event_id,
            "event_type": "command.received",
            "schema_version": 1,
            "source_system": command.source,
            "source_event_id": command.command_id,
            "entity_type": "agent_command",
            "entity_id": command.command_id,
            "work_item_id": work_item_id,
            "run_id": run_id,
            "occurred_at": now,
            "observed_at": now,
            "correlation_id": command.correlation_id,
            "causation_id": None,
            "payload": {
                "command_type": command.command_type,
                "source": command.source,
                "actor_type": command.actor.actor_type.value,
                "actor_id": command.actor.actor_id,
                "automation_id": (
                    command.automation_invocation.automation_id
                    if command.automation_invocation is not None
                    else None
                ),
                "automation_generation": (
                    command.automation_invocation.automation_generation
                    if command.automation_invocation is not None
                    else None
                ),
            },
        }
        outbox_rows = (
            {
                "consumer_name": "orchestration.run_worker",
                "topic": "command.received",
                "partition_key": dedupe_key,
                "max_attempts": 10,
            },
        )
        try:
            with self._repository.unit_of_work() as uow:
                if uow_guard is not None:
                    uow_guard(uow)
                if self._business_module_gate is not None:
                    existing = uow.commands.get_by_idempotency(
                        command.source, command.idempotency_key, for_update=True
                    )
                    if existing is None:
                        self._business_module_gate.check_new_command(command, uow)
                receipt = uow.command_gateway_create(
                    command_row,
                    work_item_row,
                    run_row,
                    event_row,
                    outbox_rows,
                )
                for ref in command.entity_refs:
                    uow.work_items.add_entity(
                        {
                            "work_item_id": receipt["work_item_id"],
                            "relation_type": ref.relation_type,
                            "entity_type": ref.entity_type,
                            "entity_id": ref.entity_id,
                            "source_system": ref.source_system or command.source,
                            "metadata_json": dict(ref.metadata),
                        }
                    )
                uow.commit()
        except OrchestrationError:
            raise
        except Exception as exc:
            raise OrchestrationError("PERSISTENCE_UNAVAILABLE", "Command could not be persisted") from exc

        created = receipt.get("created") or {}
        reused = not bool(created.get("command"))
        persisted_run = self._repository.get_run(str(receipt["run_id"])) or {}
        try:
            status = RunStatus(str(persisted_run.get("status") or RunStatus.RECEIVED.value))
        except ValueError as exc:
            raise OrchestrationError("UNKNOWN_RUN_STATUS", "Persisted run has an unknown status") from exc
        result = CommandReceipt(
            command_id=str(receipt["command_id"]),
            work_item_id=str(receipt["work_item_id"]),
            run_id=str(receipt["run_id"]),
            status=status,
            reused=reused,
            next_poll_after_ms=_next_poll_ms(status),
        )
        if self._wake_runner is not None:
            self._wake_runner(result.run_id)
        return result

    async def submit_and_wait(
        self,
        command: Command,
        *,
        timeout_seconds: float = 1800.0,
        poll_interval_seconds: float = 0.2,
    ) -> Mapping[str, Any]:
        receipt = self.submit(command)
        return await self.wait_for_run(
            receipt.run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    async def wait_for_run(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 1800.0,
        poll_interval_seconds: float = 0.2,
    ) -> Mapping[str, Any]:
        """Wait for an already accepted Run without resubmitting its Command."""

        deadline = asyncio.get_running_loop().time() + max(0.1, float(timeout_seconds))
        while True:
            run = await asyncio.to_thread(self._repository.get_run, run_id)
            if not run:
                raise OrchestrationError("RUN_NOT_FOUND", "Persisted run could not be read")
            try:
                status = RunStatus(str(run.get("status") or ""))
            except ValueError as exc:
                raise OrchestrationError("UNKNOWN_RUN_STATUS", "Persisted run has an unknown status") from exc
            if status in RUN_TERMINAL_STATUSES or status in WAITING_RUN_STATUSES:
                return run
            if asyncio.get_running_loop().time() >= deadline:
                raise OrchestrationError("RUN_WAIT_TIMEOUT", "Timed out while waiting for the run")
            await asyncio.sleep(max(0.05, float(poll_interval_seconds)))

    @staticmethod
    def _classify_work_item(command: Command) -> tuple[str, str, str]:
        tool_name = str(command.parameters.get("tool_name") or "").strip()
        dedupe_key = "command:" + sha256_json(
            {
                "source": command.source,
                "idempotency_key": command.idempotency_key,
            }
        )
        if command.command_type == "tool.execute" and tool_name:
            return (
                f"tool:{tool_name}"[:64],
                f"执行工具：{tool_name}"[:255],
                dedupe_key,
            )
        return (
            command.command_type[:64],
            f"处理命令：{command.command_type}"[:255],
            dedupe_key,
        )

    @staticmethod
    def _priority_for(command: Command) -> str:
        value = str(command.parameters.get("priority") or "NORMAL").strip().upper()
        if value not in {"LOW", "NORMAL", "HIGH", "URGENT"}:
            raise OrchestrationError("INVALID_PRIORITY", f"Unknown work item priority: {value}")
        return value


def _next_poll_ms(status: RunStatus) -> int:
    if status in RUN_TERMINAL_STATUSES:
        return 0
    if status in WAITING_RUN_STATUSES:
        return 5000
    return 1000
