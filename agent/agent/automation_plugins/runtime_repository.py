"""Production adapter for immutable plugin runtime generations.

The adapter is the only layer allowed to translate the shared MySQL row API
into plugin domain records.  In particular, invocation metadata is rebuilt
from the exact committed generation snapshot; it is never joined from the
mutable project/config/version rows after lease acquisition.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from agent.automation_plugins.catalog import project_capability_from_snapshot
from agent.automation_plugins.manifest import (
    canonical_json_bytes,
    runtime_descriptor_matches,
)
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginInstanceRecord,
    PluginProjectState,
    PluginRuntimeModel,
    PluginTrustSource,
    PluginVersionRecord,
    PluginVersionState,
    ProjectRuntimeRecord,
    RuntimeActivationPhase,
    RuntimeCoeffectKind,
    RuntimeCoeffectSnapshot,
    RuntimeEffectKind,
    RuntimeEffectRecord,
    RuntimeEffectState,
    RuntimeGenerationLease,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
    RuntimeLeaseOutcome,
    RuntimeReconcileState,
)
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from agent.automation_plugins.ports import RuntimeEffectPlan
from shared.automation_plugin_generation_unknown_write_repository import (
    stabilize_project_after_archival_unknown,
)
from shared.plugin_json import plugin_json_digest as _persisted_json_sha256
from agent.automation_plugins.catalog_read_scope import catalog_read_scope, catalog_read_transaction, catalog_version_cache, catalog_row_cache


_UNKNOWN_WRITE_RECOVERY_BATCH_LIMIT = 16


def _service_v2_signed_activation_contract(manifest_mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the activation identity from the exact locked installed manifest."""
    manifest = AutomationPluginManifestV2.from_mapping(manifest_mapping)
    contract = ServiceV2ProjectContract.from_manifest(manifest)
    return {
        "tool_contract": dict(contract.tool_contract),
        "governance_anchor": dict(contract.governance_anchor),
        "runtime": manifest.to_mapping()["runtime"],
        "runtime_permissions": dict(contract.runtime_permissions),
        "account_roles": [dict(role) for role in contract.account_roles],
        "resource_roles": [dict(role) for role in contract.resource_roles],
    }


def _utc_datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"persisted {field} is not an ISO datetime") from exc
    else:
        raise ValueError(f"persisted {field} is not a datetime")
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def snapshot_to_row(snapshot: RuntimeGenerationSnapshot) -> dict[str, Any]:
    """Return the closed JSON representation consumed by the shared UoW."""

    return {
        "automation_id": snapshot.automation_id,
        "generation": snapshot.generation,
        "plugin_id": snapshot.plugin_id,
        "plugin_version": snapshot.plugin_version,
        "package_sha256": snapshot.package_sha256,
        "manifest_sha256": snapshot.manifest_sha256,
        "trust_source": snapshot.trust_source.value,
        "runtime_model": snapshot.runtime_model.value,
        "plugin_api": snapshot.plugin_api,
        "project_config_sha256": snapshot.project_config_sha256,
        "account_bindings_sha256": snapshot.account_bindings_sha256,
        "resource_bindings_sha256": snapshot.resource_bindings_sha256,
        "device_binding_sha256": snapshot.device_binding_sha256,
        "schedule_sha256": snapshot.schedule_sha256,
        "core_registry_sha256": snapshot.core_registry_sha256,
        "tool_contract_sha256": snapshot.tool_contract_sha256,
        "invocation_contracts_sha256": snapshot.invocation_contracts_sha256,
        "compiled_invocations_sha256": snapshot.compiled_invocations_sha256,
        "runtime_descriptor_sha256": snapshot.runtime_descriptor_sha256,
        "governance_anchor_sha256": snapshot.governance_anchor_sha256,
        "policy_contract_sha256": snapshot.policy_contract_sha256,
        "enabled_entrypoints": list(snapshot.enabled_entrypoints),
        "execution_metadata": dict(snapshot.execution_metadata),
        "created_at": snapshot.created_at,
    }


def snapshot_from_row(row: Mapping[str, Any]) -> RuntimeGenerationSnapshot:
    raw = row.get("snapshot_json", row)
    if not isinstance(raw, Mapping):
        raise ValueError("persisted runtime snapshot is invalid")
    execution_metadata = raw.get("execution_metadata")
    entrypoints = raw.get("enabled_entrypoints")
    if not isinstance(execution_metadata, Mapping):
        raise ValueError("persisted runtime execution metadata is invalid")
    if not isinstance(entrypoints, (list, tuple)):
        raise ValueError("persisted runtime entrypoints are invalid")
    if "snapshot_json" in row:
        persisted_sha256 = str(row.get("snapshot_sha256") or "")
        if len(persisted_sha256) != 64 or _persisted_json_sha256(raw) != persisted_sha256:
            raise ValueError("persisted runtime snapshot digest is invalid")
        if str(row.get("automation_id") or "") != str(raw.get("automation_id") or ""):
            raise ValueError("persisted runtime snapshot project identity is invalid")
        if int(row.get("generation") or 0) != int(raw.get("generation") or 0):
            raise ValueError("persisted runtime snapshot generation identity is invalid")
    created_at = raw.get("created_at", row.get("created_at"))
    snapshot = RuntimeGenerationSnapshot(
        automation_id=str(raw.get("automation_id") or ""),
        generation=int(raw.get("generation") or 0),
        plugin_id=str(raw.get("plugin_id") or ""),
        plugin_version=str(raw.get("plugin_version") or ""),
        package_sha256=str(raw.get("package_sha256") or ""),
        manifest_sha256=str(raw.get("manifest_sha256") or ""),
        trust_source=PluginTrustSource(str(raw.get("trust_source") or "")),
        runtime_model=PluginRuntimeModel(str(raw.get("runtime_model") or "")),
        plugin_api=str(raw.get("plugin_api") or ""),
        project_config_sha256=str(raw.get("project_config_sha256") or ""),
        account_bindings_sha256=str(raw.get("account_bindings_sha256") or ""),
        resource_bindings_sha256=str(raw.get("resource_bindings_sha256") or ""),
        device_binding_sha256=str(raw.get("device_binding_sha256") or ""),
        schedule_sha256=str(raw.get("schedule_sha256") or ""),
        core_registry_sha256=str(raw.get("core_registry_sha256") or ""),
        tool_contract_sha256=str(raw.get("tool_contract_sha256") or ""),
        invocation_contracts_sha256=str(raw.get("invocation_contracts_sha256") or ""),
        compiled_invocations_sha256=str(raw.get("compiled_invocations_sha256") or ""),
        runtime_descriptor_sha256=str(raw.get("runtime_descriptor_sha256") or ""),
        governance_anchor_sha256=str(raw.get("governance_anchor_sha256") or ""),
        policy_contract_sha256=str(raw.get("policy_contract_sha256") or ""),
        enabled_entrypoints=tuple(str(item) for item in entrypoints),
        execution_metadata=dict(execution_metadata),
        created_at=_utc_datetime(created_at, "created_at"),
    )
    if "snapshot_json" in row:
        comparable_fields = (
            "automation_id",
            "generation",
            "plugin_id",
            "plugin_version",
            "package_sha256",
            "manifest_sha256",
            "project_config_sha256",
            "account_bindings_sha256",
            "resource_bindings_sha256",
            "device_binding_sha256",
            "schedule_sha256",
            "core_registry_sha256",
            "tool_contract_sha256",
            "invocation_contracts_sha256",
            "compiled_invocations_sha256",
            "runtime_descriptor_sha256",
            "governance_anchor_sha256",
            "policy_contract_sha256",
        )
        for field in comparable_fields:
            if field in row and str(row.get(field)) != str(getattr(snapshot, field)):
                raise ValueError(f"persisted runtime snapshot column drifted: {field}")
        if "trust_source" in row and str(row.get("trust_source") or "") != snapshot.trust_source.value:
            raise ValueError("persisted runtime snapshot column drifted: trust_source")
        if str(row.get("runtime_model") or "") != snapshot.runtime_model.value:
            raise ValueError("persisted runtime snapshot column drifted: runtime_model")
        if str(row.get("plugin_api") or "") != snapshot.plugin_api:
            raise ValueError("persisted runtime snapshot column drifted: plugin_api")
        expected_entrypoints_sha256 = _persisted_json_sha256(
            list(snapshot.enabled_entrypoints)
        )
        if (
            "enabled_entrypoints_sha256" in row
            and str(row.get("enabled_entrypoints_sha256") or "")
            != expected_entrypoints_sha256
        ):
            raise ValueError(
                "persisted runtime snapshot column drifted: enabled_entrypoints"
            )
    project_capability_from_snapshot(snapshot)
    return snapshot


def _coeffect_from_row(row: Mapping[str, Any]) -> RuntimeCoeffectSnapshot:
    raw = row.get("observation_json", row)
    if not isinstance(raw, Mapping):
        raise ValueError("persisted runtime coeffect is invalid")
    return RuntimeCoeffectSnapshot(
        kind=RuntimeCoeffectKind(
            str(raw.get("kind") or row.get("coeffect_kind") or "")
        ),
        key=str(raw.get("key") or row.get("coeffect_key") or ""),
        revision=str(raw.get("revision") or row.get("revision") or ""),
        ready=raw.get("ready") is True,
        observed_at=_utc_datetime(
            raw.get("observed_at", row.get("observed_at")),
            "observed_at",
        ),
        reason_code=(
            str(raw.get("reason_code")) if raw.get("reason_code") is not None else None
        ),
    )


def _effect_from_row(row: Mapping[str, Any]) -> RuntimeEffectRecord:
    payload = row.get("evidence_json")
    if not isinstance(payload, Mapping):
        payload = {}
    return RuntimeEffectRecord(
        effect_id=str(row.get("effect_id") or ""),
        automation_id=str(row.get("automation_id") or ""),
        generation=int(row.get("generation") or 0),
        sequence=int(row.get("effect_sequence") or row.get("sequence") or 0),
        kind=RuntimeEffectKind(str(row.get("effect_kind") or row.get("kind") or "")),
        state=RuntimeEffectState(str(row.get("state") or "")),
        reversible=row.get("reversible") is True or row.get("reversible") == 1,
        effect_key=str(row.get("effect_key") or ""),
        payload=dict(payload),
    )


def generation_from_row(row: Mapping[str, Any]) -> RuntimeGenerationRecord:
    coeffects = row.get("coeffects", ())
    effects = row.get("effects", ())
    if not isinstance(coeffects, (list, tuple)) or not isinstance(effects, (list, tuple)):
        raise ValueError("persisted generation children are invalid")
    base_committed_generation = row.get("base_committed_generation")
    if base_committed_generation is not None:
        base_committed_generation = int(base_committed_generation)
        if base_committed_generation <= 0:
            raise ValueError("persisted base committed generation is invalid")
    transition_token_value = row.get("activation_transition_token")
    activation_phase_value = row.get("activation_phase")
    if (transition_token_value is None) != (activation_phase_value is None):
        raise ValueError("persisted activation transition is incomplete")
    activation_transition_token: str | None = None
    activation_phase: RuntimeActivationPhase | None = None
    if transition_token_value is not None:
        raw_transition_token = str(transition_token_value)
        activation_transition_token = str(uuid.UUID(raw_transition_token))
        if activation_transition_token != raw_transition_token:
            raise ValueError(
                "persisted activation transition token is not canonical"
            )
        activation_phase = RuntimeActivationPhase(str(activation_phase_value))
    return RuntimeGenerationRecord(
        snapshot=snapshot_from_row(row),
        state=RuntimeGenerationState(str(row.get("state") or "")),
        coeffects=tuple(_coeffect_from_row(item) for item in coeffects),
        effects=tuple(_effect_from_row(item) for item in effects),
        base_committed_generation=base_committed_generation,
        activation_transition_token=activation_transition_token,
        activation_phase=activation_phase,
    )


def _runtime_from_row(row: Mapping[str, Any]) -> ProjectRuntimeRecord:
    committed = row.get("committed_generation")
    return ProjectRuntimeRecord(
        automation_id=str(row.get("automation_id") or ""),
        target_generation=int(row.get("target_generation") or 0),
        committed_generation=int(committed) if committed is not None else None,
        reconcile_state=RuntimeReconcileState(str(row.get("reconcile_state") or "")),
        record_version=int(row.get("record_version") or 0),
    )


def _version_from_row(row: Mapping[str, Any]) -> PluginVersionRecord:
    manifest = row.get("manifest_json")
    install_metadata = row.get("install_root_metadata_json")
    if not isinstance(manifest, Mapping) or not isinstance(install_metadata, Mapping):
        raise ValueError("persisted plugin version JSON is invalid")
    install_root = str(install_metadata.get("install_root") or "")
    if not install_root:
        raise ValueError("persisted plugin version has no immutable install root")
    return PluginVersionRecord(
        plugin_id=str(row.get("plugin_id") or ""),
        version=str(row.get("version") or ""),
        package_sha256=str(row.get("package_sha256") or ""),
        manifest_sha256=str(row.get("manifest_sha256") or ""),
        manifest=dict(manifest),
        trust_source=PluginTrustSource(str(row.get("trust_source") or "")),
        install_root=install_root,
        state=PluginVersionState(str(row.get("state") or "INSTALLED")),
        installed_at=_utc_datetime(row.get("installed_at"), "installed_at"),
        install_metadata=dict(install_metadata),
        runtime_model=PluginRuntimeModel(str(row.get("runtime_model") or "")),
        plugin_api=str(row.get("plugin_api") or ""),
    )


class MySQLAutomationPluginCatalogRepositoryAdapter:
    """Read desired instances while independently validating committed execution."""

    def __init__(self, orchestration_repository: Any) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work()")
        self._orchestration = orchestration_repository

    def catalog_read_scope(self):
        return catalog_read_scope(self._orchestration)

    def get_package_version(
        self,
        plugin_id: str,
        version: str,
    ) -> PluginVersionRecord | None:
        with catalog_read_transaction(self._orchestration) as uow:
            row = uow.automation_plugins.get_version(plugin_id, version)
            return _version_from_row(row) if row is not None else None

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        with catalog_read_transaction(self._orchestration) as uow:
            rows = (catalog_row_cache(self._orchestration) or {}).get(automation_id)
            row = rows.projects.get(automation_id) if rows is not None else uow.automation_plugins.get_project(automation_id)
            return self._project_from_row(uow.automation_plugins, row,
                version_cache=catalog_version_cache(self._orchestration), catalog_rows=rows) if row else None

    def list_instance_ids(self) -> Sequence[str]:
        """Read project identities without compiling persisted plugin objects."""

        with catalog_read_transaction(self._orchestration) as uow:
            return tuple(
                str(row.get("automation_id") or "").strip()
                if isinstance(row, Mapping)
                else ""
                for row in uow.automation_plugins.list_projects()
            )

    def list_instance_ids_for_module(self, module: str) -> Sequence[str]:
        with catalog_read_transaction(self._orchestration) as uow:
            return tuple(uow.automation_plugins.list_project_module_ids(module))

    def prefetch_catalog_instances(self, identities: Sequence[str]) -> None:
        with catalog_read_transaction(self._orchestration) as uow:
            cache = catalog_row_cache(self._orchestration)
            reader = getattr(uow.automation_plugins, "read_catalog_rows", None)
            if cache is not None and callable(reader) and any(item not in cache for item in identities):
                rows = reader(identities)
                cache.update({item: rows for item in identities})

    def list_instances(self) -> Sequence[PluginInstanceRecord]:
        with catalog_read_transaction(self._orchestration) as uow:
            return tuple(
                self._project_from_row(uow.automation_plugins, row)
                for row in uow.automation_plugins.list_projects()
            )

    @staticmethod
    def _project_from_row(low_level: Any, row: Mapping[str, Any], *, version_cache=None, catalog_rows=None) -> PluginInstanceRecord:
        automation_id = str(row.get("automation_id") or "")
        committed_generation = row.get("committed_generation")
        committed_snapshot: RuntimeGenerationSnapshot | None = None
        committed_version_row = None
        desired_version_name = str(row.get("plugin_version") or "")
        if committed_generation is not None:
            generation_row = catalog_rows.generation(low_level, automation_id, int(committed_generation)) if catalog_rows is not None else low_level.get_generation_row(
                automation_id,
                int(committed_generation),
                include_execution_details=False,
            )
            readable_states = {"COMMITTED", "BLOCKED"}
            if row.get("state") == "UNINSTALLING" and row.get("enabled") in (False, 0):
                # Revoked projects remain inspectable solely for their existing
                # uninstall reconciler. Active execution keeps the strict fence.
                readable_states.update({"DRAINING", "DISPOSING", "DISPOSED"})
            if generation_row is None or str(generation_row.get("state") or "") not in readable_states:
                raise ValueError("project committed generation is missing or not committed")
            committed_snapshot = generation_from_row(generation_row).snapshot
            package_key = (str(row.get("plugin_id") or ""), committed_snapshot.plugin_version)
            cached_package = version_cache.get(package_key) if version_cache is not None else None
            committed_version_row = cached_package[0] if cached_package is not None else (
                catalog_rows.version(low_level, *package_key) if catalog_rows is not None else low_level.get_version(*package_key))
            if committed_version_row is None:
                raise ValueError("project committed plugin version disappeared")
            committed_version = _version_from_row(committed_version_row)
            if (
                committed_version.package_sha256 != committed_snapshot.package_sha256
                or committed_version.manifest_sha256
                != committed_snapshot.manifest_sha256
                or committed_version.trust_source != committed_snapshot.trust_source
                or committed_version.runtime_model != committed_snapshot.runtime_model
                or committed_version.plugin_api != committed_snapshot.plugin_api
            ):
                raise ValueError(
                    "project committed generation differs from its immutable package"
                )
            committed_manifest = cached_package[1] if cached_package is not None else AutomationPluginManifestV2.from_mapping(
                committed_version.manifest
            )
            projected = ServiceV2ProjectContract.from_manifest(
                committed_manifest
            )
            committed_manifest_mapping = committed_manifest.to_mapping()
            expected_tool_contract = projected.tool_contract
            expected_runtime_permissions = projected.runtime_permissions
            expected_account_roles = projected.account_roles
            expected_resource_roles = projected.resource_roles
            expected_governance_anchor = projected.governance_anchor
            if (
                committed_manifest.plugin_id != committed_version.plugin_id
                or committed_manifest.version != committed_version.version
                or committed_manifest.manifest_sha256
                != committed_version.manifest_sha256
            ):
                raise ValueError("project committed package differs from its manifest")
            if version_cache is not None:
                version_cache[package_key] = (committed_version_row, committed_manifest)
            committed_capability = project_capability_from_snapshot(
                committed_snapshot
            )
            committed_runtime = committed_capability.get("_plugin_runtime")
            expected_runtime_descriptor = {
                "runtime": dict(committed_manifest_mapping["runtime"]),
                "runtime_permissions": dict(expected_runtime_permissions),
                "account_roles": [
                    dict(item) for item in expected_account_roles
                ],
                "resource_roles": [
                    dict(item) for item in expected_resource_roles
                ],
                "install_metadata": {
                    **dict(committed_version.install_metadata),
                    "install_root": committed_version.install_root,
                },
            }
            observed_runtime_descriptor = (
                committed_snapshot.execution_metadata.get("runtime_descriptor")
                if isinstance(committed_snapshot.execution_metadata, Mapping)
                else None
            )
            if (
                not isinstance(committed_runtime, Mapping)
                or canonical_json_bytes(committed_capability)
                != canonical_json_bytes(
                    {
                        **dict(expected_tool_contract),
                        "name": f"automation.{automation_id}.run",
                        "_plugin_runtime": dict(committed_runtime),
                    }
                )
                or not runtime_descriptor_matches(
                    observed_runtime_descriptor,
                    expected_runtime_descriptor,
                )
                or canonical_json_bytes(
                    committed_snapshot.execution_metadata.get("governance_anchor")
                )
                != canonical_json_bytes(expected_governance_anchor)
            ):
                raise ValueError(
                    "project committed generation differs from its immutable installation"
                )
        if committed_snapshot is not None and committed_snapshot.plugin_version == desired_version_name:
            version_row = committed_version_row
        elif catalog_rows is not None:
            version_row = catalog_rows.version(low_level, str(row.get("plugin_id") or ""), desired_version_name)
        else:
            version_row = low_level.get_version(str(row.get("plugin_id") or ""), desired_version_name)
        if version_row is None:
            raise ValueError("project desired plugin version disappeared")
        return PluginInstanceRecord(
            automation_id=automation_id,
            display_name=str(row.get("display_name") or ""),
            plugin_id=str(row.get("plugin_id") or ""),
            state=PluginProjectState(str(row.get("state") or "")),
            active_version=_version_from_row(version_row),
            enabled=row.get("enabled") is True or row.get("enabled") == 1,
            record_version=int(row.get("record_version") or 0),
            target_generation=int(row.get("target_generation") or 0),
            committed_generation=(
                int(committed_generation) if committed_generation is not None else None
            ),
            reconcile_state=RuntimeReconcileState(
                str(row.get("reconcile_state") or "")
            ),
            committed_snapshot=committed_snapshot,
        )


class MySQLAutomationProjectConfigurationReadAdapter:
    """Expose current desired configuration for Console, never for execution."""

    def __init__(self, orchestration_repository: Any) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work()")
        self._orchestration = orchestration_repository

    def get_project_config(
        self,
        automation_id: str,
    ) -> AutomationProjectConfigRecord | None:
        with catalog_read_transaction(self._orchestration) as uow:
            rows = (catalog_row_cache(self._orchestration) or {}).get(automation_id)
            row = rows.config(uow.automation_plugins, automation_id) if rows is not None else uow.automation_plugins.get_project_config(automation_id)
        if row is None:
            return None
        for field in (
            "config_json",
            "account_bindings_json",
            "resource_bindings_json",
        ):
            if not isinstance(row.get(field), Mapping):
                raise ValueError(f"persisted project config field is invalid: {field}")
        if not isinstance(row.get("enabled_entrypoints_json"), list):
            raise ValueError(
                "persisted project config field is invalid: enabled_entrypoints_json"
            )
        if not isinstance(row.get("schedule"), Mapping):
            raise ValueError("persisted project config field is invalid: schedule")
        if row.get("device_id") not in (None, ""):
            raise ValueError("persisted project config binds a retired Worker device")
        return AutomationProjectConfigRecord(
            automation_id=str(row.get("automation_id") or ""),
            config=dict(row["config_json"]),
            account_bindings=dict(row["account_bindings_json"]),
            resource_bindings={
                str(key): str(value)
                for key, value in dict(row["resource_bindings_json"]).items()
            },
            schedule=dict(row.get("schedule") or {}),
            config_version=int(row.get("config_version") or 0),
            configured=row.get("configured") is True or row.get("configured") == 1,
            config_sha256=str(row.get("config_sha256") or ""),
            account_bindings_sha256=str(row.get("account_bindings_sha256") or ""),
            resource_bindings_sha256=str(row.get("resource_bindings_sha256") or ""),
            device_binding_sha256=str(row.get("device_binding_sha256") or ""),
            enabled_entrypoints=tuple(
                str(item) for item in row["enabled_entrypoints_json"]
            ),
        )


class MySQLAutomationPluginRuntimeAdapter:
    """Implement generation repository and invocation-lease ports over one UoW."""

    def __init__(self, orchestration_repository: Any) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work()")
        self._orchestration = orchestration_repository

    def read_scope(self):
        """Share one snapshot for a bounded, read-only runtime inspection."""
        return catalog_read_scope(self._orchestration)

    def get_project_runtime(self, automation_id: str) -> ProjectRuntimeRecord | None:
        with catalog_read_transaction(self._orchestration) as uow:
            row = uow.automation_plugins.get_project_runtime_row(automation_id)
            return _runtime_from_row(row) if row is not None else None

    def list_project_runtime_ids(self) -> Sequence[str]:
        """Read runtime identities without parsing state or generation records."""

        with catalog_read_transaction(self._orchestration) as uow:
            return tuple(
                str(row.get("automation_id") or "").strip()
                if isinstance(row, Mapping)
                else ""
                for row in uow.automation_plugins.list_project_runtime_rows()
            )

    def list_project_runtimes(self) -> Sequence[ProjectRuntimeRecord]:
        with catalog_read_transaction(self._orchestration) as uow:
            return tuple(
                _runtime_from_row(row)
                for row in uow.automation_plugins.list_project_runtime_rows()
            )

    def get_generation(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord | None:
        with catalog_read_transaction(self._orchestration) as uow:
            row = uow.automation_plugins.get_generation_row(automation_id, generation)
            return generation_from_row(row) if row is not None else None

    def list_project_generations(
        self,
        automation_id: str,
    ) -> Sequence[RuntimeGenerationRecord]:
        with catalog_read_transaction(self._orchestration) as uow:
            return tuple(
                generation_from_row(row)
                for row in uow.automation_plugins.list_generation_rows(automation_id)
            )

    def allocate_target_generation(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        expected_committed_generation: int | None,
        request_id: str,
    ) -> RuntimeGenerationRecord:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.allocate_target_generation_row(
                snapshot_to_row(snapshot),
                expected_committed_generation=expected_committed_generation,
                request_id=request_id,
                service_v2_contract_projector=_service_v2_signed_activation_contract,
            )
            uow.commit()
        return generation_from_row(row)

    def mark_generation_preparing(self, automation_id: str, generation: int) -> None:
        self._write("mark_generation_preparing_row", automation_id, generation)

    def retry_disabled_route_preparation(self, snapshot, **identity) -> None:
        self._write("retry_disabled_route_preparation_row", snapshot_to_row(snapshot), **identity)

    def replace_generation_coeffects(
        self,
        automation_id: str,
        generation: int,
        coeffects: Sequence[RuntimeCoeffectSnapshot],
    ) -> None:
        payload = [
            {
                "kind": item.kind.value,
                "key": item.key,
                "revision": item.revision,
                "ready": item.ready,
                "observed_at": item.observed_at,
                "reason_code": item.reason_code,
            }
            for item in coeffects
        ]
        self._write(
            "replace_generation_coeffects_rows",
            automation_id,
            generation,
            payload,
        )

    def mark_generation_waiting_coeffects(
        self,
        automation_id: str,
        generation: int,
        *,
        reason_codes: Sequence[str],
    ) -> None:
        self._write(
            "mark_generation_waiting_coeffects_row",
            automation_id,
            generation,
            reason_codes=reason_codes,
        )

    def reserve_generation_effect(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        plan: RuntimeEffectPlan,
        sequence: int,
    ) -> RuntimeEffectRecord:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.reserve_generation_effect_row(
                snapshot_to_row(snapshot),
                plan={
                    "kind": plan.kind.value,
                    "effect_key": plan.effect_key,
                    "payload": dict(plan.payload),
                    "reversible": plan.reversible,
                },
                sequence=sequence,
            )
            uow.commit()
        return _effect_from_row(row)

    def mark_generation_effect_applied(
        self,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.mark_generation_effect_applied_row(
                {
                    "effect_id": effect.effect_id,
                    "automation_id": effect.automation_id,
                    "generation": effect.generation,
                    "sequence": effect.sequence,
                    "kind": effect.kind.value,
                    "reversible": effect.reversible,
                    "effect_key": effect.effect_key,
                    "payload": dict(effect.payload),
                }
            )
            uow.commit()
        return _effect_from_row(row)

    def mark_generation_prepared(self, automation_id: str, generation: int) -> None:
        self._write("mark_generation_prepared_row", automation_id, generation)

    def commit_generation_cas(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_committed_generation: int | None,
    ) -> ProjectRuntimeRecord:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.commit_generation_cas_row(
                automation_id,
                generation,
                expected_committed_generation=expected_committed_generation,
                service_v2_contract_projector=_service_v2_signed_activation_contract,
            )
            uow.commit()
        return _runtime_from_row(row)

    def rollback_generation_cas(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_base_committed_generation: int | None,
        expected_transition_token: str,
    ) -> ProjectRuntimeRecord:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.rollback_generation_cas_row(
                automation_id,
                generation,
                expected_base_committed_generation=(
                    expected_base_committed_generation
                ),
                expected_transition_token=expected_transition_token,
            )
            uow.commit()
        return _runtime_from_row(row)

    def complete_generation_activation(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_transition_token: str,
    ) -> None:
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.complete_generation_activation_row(
                automation_id,
                generation,
                expected_transition_token=expected_transition_token,
                service_v2_contract_projector=_service_v2_signed_activation_contract,
            )
            uow.commit()

    def block_generation_activation(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_transition_token: str,
    ) -> None:
        with self._orchestration.unit_of_work() as uow:
            uow.automation_plugins.block_generation_activation_row(
                automation_id,
                generation,
                expected_transition_token=expected_transition_token,
            )
            uow.commit()

    def set_project_dependency_scheduler_gate(
        self,
        automation_id: str,
        *,
        dependency_ready: bool,
    ) -> Mapping[str, Any]:
        result = self._write(
            "set_project_dependency_scheduler_gate",
            automation_id,
            dependency_ready=dependency_ready,
        )
        if not isinstance(result, Mapping):
            raise ValueError("dependency scheduler gate returned invalid evidence")
        return dict(result)

    def mark_generation_draining(self, automation_id: str, generation: int) -> None:
        self._write("mark_generation_draining_row", automation_id, generation)

    def list_active_generation_leases(
        self,
        automation_id: str,
        generation: int,
    ) -> Sequence[RuntimeGenerationLease]:
        with catalog_read_transaction(self._orchestration) as uow:
            rows = uow.automation_plugins.list_active_generation_lease_rows(
                automation_id,
                generation,
            )
            if not rows:
                return ()
            snapshot_row = uow.automation_plugins.get_generation_row(
                automation_id,
                generation,
            )
            if snapshot_row is None and rows:
                raise ValueError("active lease generation disappeared")
            snapshot = generation_from_row(snapshot_row).snapshot if snapshot_row else None
            return tuple(
                self._lease_from_row(row, snapshot=snapshot)
                for row in rows
                if snapshot is not None
            )

    def has_unknown_generation_write(self, automation_id: str, generation: int) -> bool:
        with catalog_read_transaction(self._orchestration) as uow:
            return bool(
                uow.automation_plugins.has_unknown_generation_write_row(
                    automation_id,
                    generation,
                )
            )

    def generation_lease_outcomes(self, automation_id: str) -> tuple[frozenset[int], frozenset[int]]:
        """Return (generations with active leases, generations with unknown writes)."""
        with catalog_read_transaction(self._orchestration) as uow:
            rows = uow.automation_plugins.generation_lease_outcome_rows(automation_id)
        active: set[int] = set()
        unknown: set[int] = set()
        for row in rows:
            generation = int(row["generation"])
            (unknown if row["outcome"] == "WRITE_OUTCOME_UNKNOWN" else active).add(generation)
        return frozenset(active), frozenset(unknown)

    def reserve_generation_dispose(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.reserve_generation_dispose_row(
                automation_id,
                generation,
            )
            uow.commit()
        return generation_from_row(row)

    def mark_generation_effect_disposing(self, effect_id: str) -> None:
        self._write("mark_generation_effect_disposing_row", effect_id)

    def mark_generation_effect_disposed(self, effect_id: str) -> None:
        self._write("mark_generation_effect_disposed_row", effect_id)

    def complete_generation_dispose(self, automation_id: str, generation: int) -> None:
        self._write("complete_generation_dispose_row", automation_id, generation)

    def fail_generation(
        self,
        automation_id: str,
        generation: int,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        self._write(
            "fail_generation_row",
            automation_id,
            generation,
            error_code=error_code,
            error_summary=error_summary,
        )

    def block_generation_unknown_write(self, automation_id: str, generation: int) -> None:
        self._write("block_generation_unknown_write_row", automation_id, generation)

    def stabilize_project_after_archival_unknown(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        with self._orchestration.unit_of_work() as uow:
            with uow.automation_plugins.cursor() as cursor:
                stabilize_project_after_archival_unknown(
                    cursor,
                    automation_id=automation_id,
                    generation=generation,
                )
            uow.commit()

    def acquire_committed_generation(
        self,
        automation_id: str,
        *,
        expected_generation: int,
        expected_manifest_sha256: str,
        lease_id: str,
        orchestration_run_id: str | None = None,
        invocation_id: str | None = None,
        provider_call: bool = False,
        expires_at: datetime,
    ) -> RuntimeGenerationLease:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.acquire_committed_generation_lease_row(
                automation_id,
                expected_generation=expected_generation,
                expected_manifest_sha256=expected_manifest_sha256,
                lease_id=lease_id,
                orchestration_run_id=orchestration_run_id,
                **({"invocation_id": invocation_id} if invocation_id is not None else {}),
                **({"provider_call": True} if provider_call else {}),
                expires_at=expires_at,
                lease_owner="agent-runtime",
            )
            generation_row = uow.automation_plugins.get_generation_row(
                automation_id,
                expected_generation,
                for_update=True,
            )
            if generation_row is None:
                raise ValueError("committed runtime generation disappeared during lease")
            snapshot = generation_from_row(generation_row).snapshot
            uow.commit()
        return self._lease_from_row(row, snapshot=snapshot)

    def release_generation(
        self,
        lease: RuntimeGenerationLease,
        *,
        outcome: RuntimeLeaseOutcome,
    ) -> None:
        self._write(
            "release_generation_lease_row",
            lease.lease_id,
            outcome=outcome.value,
        )

    def record_write_attempt(self, receipt: Mapping[str, object]) -> None:
        """Persist one broker-started write; the caller provides digests only."""

        self._write("record_generation_write_attempt_row", dict(receipt))

    def finalize_generation_write(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        outcome: RuntimeLeaseOutcome,
        evidence_sha256: str,
    ) -> None:
        self._write(
            "finalize_generation_write_row",
            automation_id=automation_id,
            generation=generation,
            lease_id=lease_id,
            outcome=outcome.value,
            evidence_sha256=evidence_sha256,
        )

    def _write(self, method_name: str, *args: object, **kwargs: object) -> Any:
        with self._orchestration.unit_of_work() as uow:
            method = getattr(uow.automation_plugins, method_name)
            result = method(*args, **kwargs)
            uow.commit()
            return result

    @staticmethod
    def _lease_from_row(
        row: Mapping[str, Any],
        *,
        snapshot: RuntimeGenerationSnapshot | None,
    ) -> RuntimeGenerationLease:
        if snapshot is None:
            raise ValueError("runtime lease has no immutable generation snapshot")
        return RuntimeGenerationLease(
            lease_id=str(row.get("lease_id") or ""),
            automation_id=str(row.get("automation_id") or ""),
            generation=int(row.get("generation") or 0),
            snapshot=snapshot,
            runtime_metadata=project_capability_from_snapshot(snapshot),
            acquired_at=_utc_datetime(row.get("acquired_at"), "acquired_at"),
            expires_at=_utc_datetime(row.get("expires_at"), "expires_at"),
            outcome=RuntimeLeaseOutcome(str(row.get("outcome") or "RUNNING")),
            invocation_id=row.get("invocation_id"),
            orchestration_run_id=(
                str(row["orchestration_run_id"])
                if row.get("orchestration_run_id") is not None
                else None
            ),
        )


__all__ = [
    "MySQLAutomationPluginCatalogRepositoryAdapter",
    "MySQLAutomationPluginRuntimeAdapter",
    "MySQLAutomationProjectConfigurationReadAdapter",
    "generation_from_row",
    "snapshot_from_row",
    "snapshot_to_row",
]
