"""Dependency inversion ports for the Agent control plane."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from agent.orchestration.models import Command, ContextSnapshot, PlanStep


@runtime_checkable
class ToolCatalogPort(Protocol):
    @property
    def catalog_hash(self) -> str: ...

    def get_capability(self, tool_name: str) -> Mapping[str, Any] | None: ...

    def validate_arguments(self, tool_name: str, arguments: Mapping[str, Any]) -> None: ...

    def list_llm_capabilities(self) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class ToolExecutionPort(Protocol):
    async def execute_step(
        self,
        step: PlanStep,
        *,
        run_id: str,
        step_id: str,
        execution_context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def cancel_step(self, *, run_id: str, step_id: str) -> Mapping[str, Any]: ...

    async def reconcile_step(
        self,
        step: PlanStep,
        *,
        run_id: str,
        step_id: str,
        persisted_step: Mapping[str, Any],
        execution_context: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class ContextProviderPort(Protocol):
    def build_context(self, command: Command) -> ContextSnapshot: ...


@runtime_checkable
class RepositoryPort(Protocol):
    def create_command_work_item_run(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def get_run(self, run_id: str) -> Mapping[str, Any] | None: ...

    def get_command(self, command_id: str) -> Mapping[str, Any] | None: ...

    def update_run_state(
        self,
        run_id: str,
        *,
        expected_version: int,
        expected_status: str,
        target_status: str,
        values: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class EventPublisherPort(Protocol):
    def publish(
        self,
        event_type: str,
        *,
        entity_type: str,
        entity_id: str,
        payload: Mapping[str, Any],
        correlation_id: str,
        causation_id: str | None = None,
        consumers: Sequence[str] = (),
    ) -> str: ...


@runtime_checkable
class OutboxRepositoryPort(Protocol):
    def claim_outbox(self, *, worker_id: str, limit: int, lease_seconds: int) -> list[Mapping[str, Any]]: ...

    def acknowledge_outbox(self, outbox_id: int, *, worker_id: str) -> None: ...

    def retry_outbox(
        self,
        outbox_id: int,
        *,
        worker_id: str,
        error_code: str,
        error_summary: str,
        delay_seconds: int,
    ) -> None: ...

    def dead_letter_outbox(
        self,
        outbox_id: int,
        *,
        worker_id: str,
        error_code: str,
        error_summary: str,
    ) -> None: ...
