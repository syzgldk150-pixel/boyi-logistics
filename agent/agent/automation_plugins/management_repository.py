"""MySQL adapters used only by the authenticated plugin management surface."""

from __future__ import annotations

import copy
import uuid
from typing import Any, Mapping, Sequence

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.models import AutomationProjectConfigRecord, DeviceBinding
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationProjectConfigurationReadAdapter,
)


class MySQLAutomationPluginManagementRepository:
    """Persist one atomic project configuration and project safe Worker rows.

    The low-level repository owns the transaction that updates project config,
    explicit bindings, all derived schedule rows and project authorization.
    This adapter never executes DDL and never returns Worker identity material
    to an HTTP projection.
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
        worker_requirement: Mapping[str, object],
        request_id: str,
    ) -> AutomationProjectConfigRecord:
        del automation_id, config_schema, worker_requirement, request_id
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
        device_binding: DeviceBinding | None,
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
                device_binding=device_binding,
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

    def get_worker_device(self, device_id: str) -> dict[str, Any] | None:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_worker_device(device_id)
        return copy.deepcopy(dict(row)) if isinstance(row, Mapping) else None

    def list_worker_devices(self) -> tuple[dict[str, Any], ...]:
        with self._orchestration.unit_of_work() as uow:
            rows = uow.automation_plugins.list_worker_devices()
        return tuple(
            copy.deepcopy(dict(row))
            for row in rows
            if isinstance(row, Mapping)
        )

    def pair_worker_device(
        self,
        row: Mapping[str, Any],
        *,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Use only the atomic, request-audited pairing repository contract.

        The low-level primitive persists the immutable device identity and its
        request audit record.  This adapter adds the domain event and outbox in
        the same unit of work.  Environments missing that primitive remain
        fail-closed and never fall back to the unaudited ``pair_device`` path.
        """

        with self._orchestration.unit_of_work() as uow:
            pairer = getattr(
                uow.automation_plugins,
                "pair_device_with_audit",
                None,
            )
            if not callable(pairer):
                raise PluginConflictError(
                    "Worker pairing audit persistence is not installed",
                    code="PLUGIN_WORKER_PAIRING_AUDIT_UNAVAILABLE",
                )
            persisted = pairer(
                row,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            device_id = str(persisted.get("device_id") or "")
            audit_payload = {
                "device_id": device_id,
                "identity_sha256": str(persisted.get("identity_sha256") or ""),
                "paired_public_key_fingerprint": str(
                    persisted.get("paired_public_key_fingerprint") or ""
                ),
                "capabilities_sha256": str(
                    persisted.get("capabilities_sha256") or ""
                ),
                "actor_id": actor_id,
            }
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"boyi:automation-worker-paired:{request_id}",
                )
            )
            uow.events.append_with_outbox(
                {
                    "event_id": event_id,
                    "event_type": "automation_worker.paired",
                    "schema_version": 1,
                    "source_system": "agent",
                    "source_event_id": f"worker-pair:{request_id}",
                    "entity_type": "automation_worker_device",
                    "entity_id": device_id,
                    "correlation_id": request_id,
                    "payload": audit_payload,
                },
                (
                    {
                        "consumer_name": "orchestration.audit",
                        "topic": "automation_worker.paired",
                        "partition_key": device_id,
                        "max_attempts": 10,
                    },
                ),
            )
            uow.commit()
        if not isinstance(persisted, Mapping):
            raise PluginConflictError(
                "paired Worker record did not persist",
                code="PLUGIN_WORKER_PAIRING_FAILED",
            )
        return copy.deepcopy(dict(persisted))


__all__ = ["MySQLAutomationPluginManagementRepository"]
