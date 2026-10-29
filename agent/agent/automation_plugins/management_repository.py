"""MySQL adapters used only by the authenticated plugin management surface."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.models import AutomationProjectConfigRecord
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationProjectConfigurationReadAdapter,
)


class MySQLAutomationPluginManagementRepository:
    """Persist one atomic project configuration.

    The low-level repository owns the transaction that updates project config,
    explicit bindings, all derived schedule rows and project authorization.
    This adapter never executes DDL.
    """

    def __init__(self, orchestration_repository: Any) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work()")
        self._orchestration = orchestration_repository
        self._reader = MySQLAutomationProjectConfigurationReadAdapter(
            orchestration_repository
        )

    def get_project_config(
        self,
        automation_id: str,
    ) -> AutomationProjectConfigRecord | None:
        return self._reader.get_project_config(automation_id)

    def initialize_unconfigured_project(
        self,
        automation_id: str,
        *,
        config_schema: Mapping[str, object],
        request_id: str,
    ) -> AutomationProjectConfigRecord:
        del automation_id, config_schema, request_id
        raise PluginConflictError(
            "project configuration initialization belongs to signed package installation",
            code="PLUGIN_CONFIGURATION_LIFECYCLE_REQUIRED",
        )

    def mark_plugin_contract_stale(
        self,
        automation_id: str,
        *,
        from_manifest_sha256: str | None,
        to_manifest_sha256: str,
        request_id: str,
    ) -> None:
        del automation_id, from_manifest_sha256, to_manifest_sha256, request_id
        raise PluginConflictError(
            "plugin contract changes require the generation upgrade coordinator",
            code="PLUGIN_GENERATION_UPGRADE_REQUIRED",
        )

    def save_project_config(
        self,
        automation_id: str,
        *,
        config: Mapping[str, object],
        account_bindings: Mapping[str, object],
        resource_bindings: Mapping[str, str],
        enabled_entrypoints: Sequence[str],
        schedule: Mapping[str, object],
        compiled_invocations: Mapping[str, Mapping[str, object]],
        contract_witness: Mapping[str, object],
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_project_configuration_version: int,
    ) -> AutomationProjectConfigRecord:
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.save_project_config(
                automation_id,
                config=config,
                account_bindings=account_bindings,
                resource_bindings=resource_bindings,
                enabled_entrypoints=enabled_entrypoints,
                schedule=schedule,
                compiled_invocations=compiled_invocations,
                contract_witness=contract_witness,
                actor_id=actor_id,
                actor_role=actor_role,
                request_id=request_id,
                expected_project_configuration_version=(
                    expected_project_configuration_version
                ),
            )
            uow.automation_projects.invalidate_pending_approvals_and_wake_runs(
                automation_id,
                event_repository=uow.events,
            )
            uow.commit()
        persisted = self._reader.get_project_config(automation_id)
        if persisted is None:
            raise PluginConflictError(
                "automation project configuration disappeared after save",
                code="PLUGIN_CONFIGURATION_NOT_FOUND",
            )
        return persisted

    def save_project_schedule_times(
        self,
        automation_id: str,
        *,
        schedule: Mapping[str, object],
        expected_schedule_sha256: str,
        scheduling: Mapping[str, object],
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_project_configuration_version: int,
    ) -> dict[str, Any]:
        """One Unit of Work; an ``INELIGIBLE`` answer commits nothing."""

        with self._orchestration.unit_of_work() as uow:
            result = uow.automation_plugins.save_project_schedule_times(
                automation_id,
                schedule=schedule,
                expected_schedule_sha256=expected_schedule_sha256,
                scheduling=scheduling,
                actor_id=actor_id,
                actor_role=actor_role,
                request_id=request_id,
                expected_project_configuration_version=(
                    expected_project_configuration_version
                ),
            )
            if result.get("outcome") == "SAVED":
                uow.commit()
        return dict(result)


__all__ = ["MySQLAutomationPluginManagementRepository"]
