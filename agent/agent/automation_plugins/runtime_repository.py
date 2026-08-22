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
    AutomationPluginManifest,
    canonical_json_bytes,
)
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    DeviceBinding,
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    PluginVersionRecord,
    PluginVersionState,
    ProjectRuntimeRecord,
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
from agent.automation_plugins.ports import RuntimeEffectPlan
from shared.redaction import redact_sensitive


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


def _persisted_json_sha256(value: object) -> str:
    serialized = json.dumps(
        redact_sensitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


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
    return RuntimeGenerationRecord(
        snapshot=snapshot_from_row(row),
        state=RuntimeGenerationState(str(row.get("state") or "")),
        coeffects=tuple(_coeffect_from_row(item) for item in coeffects),
        effects=tuple(_effect_from_row(item) for item in effects),
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
    )


class MySQLAutomationPluginCatalogRepositoryAdapter:
    """Read desired instances while independently validating committed execution."""

    def __init__(self, orchestration_repository: Any) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work()")
        self._orchestration = orchestration_repository

    def get_package_version(
        self,
        plugin_id: str,
        version: str,
    ) -> PluginVersionRecord | None:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_version(plugin_id, version)
            return _version_from_row(row) if row is not None else None

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_project(automation_id)
            return self._project_from_row(uow.automation_plugins, row) if row else None

    def list_instances(self) -> Sequence[PluginInstanceRecord]:
        with self._orchestration.unit_of_work() as uow:
            return tuple(
                self._project_from_row(uow.automation_plugins, row)
                for row in uow.automation_plugins.list_projects()
            )

    @staticmethod
    def _project_from_row(low_level: Any, row: Mapping[str, Any]) -> PluginInstanceRecord:
        automation_id = str(row.get("automation_id") or "")
        committed_generation = row.get("committed_generation")
        committed_snapshot: RuntimeGenerationSnapshot | None = None
        desired_version_name = str(row.get("plugin_version") or "")
        if committed_generation is not None:
            generation_row = low_level.get_generation_row(
                automation_id,
                int(committed_generation),
            )
            if generation_row is None or str(generation_row.get("state") or "") not in {
                "COMMITTED",
                "BLOCKED",
            }:
                raise ValueError("project committed generation is missing or not committed")
            committed_snapshot = generation_from_row(generation_row).snapshot
            committed_version_row = low_level.get_version(
                str(row.get("plugin_id") or ""),
                committed_snapshot.plugin_version,
            )
            if committed_version_row is None:
                raise ValueError("project committed plugin version disappeared")
            committed_version = _version_from_row(committed_version_row)
            if (
                committed_version.package_sha256 != committed_snapshot.package_sha256
                or committed_version.manifest_sha256
                != committed_snapshot.manifest_sha256
                or committed_version.trust_source != committed_snapshot.trust_source
            ):
                raise ValueError(
                    "project committed generation differs from its immutable package"
                )
            committed_manifest = AutomationPluginManifest.from_mapping(
                committed_version.manifest
            )
            if (
                committed_manifest.plugin_id != committed_version.plugin_id
                or committed_manifest.version != committed_version.version
                or committed_manifest.manifest_sha256
                != committed_version.manifest_sha256
            ):
                raise ValueError(
                    "project committed package differs from its signed manifest"
                )
            committed_capability = project_capability_from_snapshot(
                committed_snapshot
            )
            committed_runtime = committed_capability.get("_plugin_runtime")
            expected_runtime_descriptor = {
                "runtime": dict(committed_manifest.runtime),
                "runtime_permissions": dict(committed_manifest.runtime_permissions),
                "account_roles": [dict(item) for item in committed_manifest.account_roles],
                "resource_roles": [
                    dict(item) for item in committed_manifest.resource_roles
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
                        **dict(committed_manifest.tool_contract),
                        "name": f"automation.{automation_id}.run",
                        "_plugin_runtime": dict(committed_runtime),
                    }
                )
                or canonical_json_bytes(observed_runtime_descriptor)
                != canonical_json_bytes(expected_runtime_descriptor)
                or canonical_json_bytes(
                    committed_snapshot.execution_metadata.get("governance_anchor")
                )
                != canonical_json_bytes(
                    committed_manifest.to_mapping()["governance_anchor"]
                )
            ):
                raise ValueError(
                    "project committed generation differs from its signed installation"
                )
        version_row = low_level.get_version(
            str(row.get("plugin_id") or ""),
            desired_version_name,
        )
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
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_project_config(automation_id)
        if row is None:
            return None
        for field in (
            "config_json",
            "account_bindings_json",
            "resource_bindings_json",
            "enabled_entrypoints_json",
        ):
            if not isinstance(row.get(field), (Mapping, list)):
                raise ValueError(f"persisted project config field is invalid: {field}")
        device_id = str(row.get("device_id") or "")
        device_name = str(row.get("device_name") or "")
        if bool(device_id) != bool(device_name):
            raise ValueError("persisted project device binding is incomplete")
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
            device_binding=(
                DeviceBinding(device_id=device_id, device_name=device_name)
                if device_id
                else None
            ),
        )


class MySQLAutomationPluginRuntimeAdapter:
    """Implement generation repository and invocation-lease ports over one UoW."""

    def __init__(self, orchestration_repository: Any) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work()")
        self._orchestration = orchestration_repository

    def get_project_runtime(self, automation_id: str) -> ProjectRuntimeRecord | None:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_project_runtime_row(automation_id)
            return _runtime_from_row(row) if row is not None else None

    def list_project_runtimes(self) -> Sequence[ProjectRuntimeRecord]:
        with self._orchestration.unit_of_work() as uow:
            return tuple(
                _runtime_from_row(row)
                for row in uow.automation_plugins.list_project_runtime_rows()
            )

    def get_generation(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord | None:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.get_generation_row(automation_id, generation)
            return generation_from_row(row) if row is not None else None

    def list_project_generations(
        self,
        automation_id: str,
    ) -> Sequence[RuntimeGenerationRecord]:
        with self._orchestration.unit_of_work() as uow:
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
            )
            uow.commit()
        return generation_from_row(row)

    def mark_generation_preparing(self, automation_id: str, generation: int) -> None:
        self._write("mark_generation_preparing_row", automation_id, generation)

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
            )
            uow.commit()
        return _runtime_from_row(row)

    def mark_generation_draining(self, automation_id: str, generation: int) -> None:
        self._write("mark_generation_draining_row", automation_id, generation)

    def list_active_generation_leases(
        self,
        automation_id: str,
        generation: int,
    ) -> Sequence[RuntimeGenerationLease]:
        with self._orchestration.unit_of_work() as uow:
            rows = uow.automation_plugins.list_active_generation_lease_rows(
                automation_id,
                generation,
            )
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
        with self._orchestration.unit_of_work() as uow:
            return bool(
                uow.automation_plugins.has_unknown_generation_write_row(
                    automation_id,
                    generation,
                )
            )

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

    def acquire_committed_generation(
        self,
        automation_id: str,
        *,
        expected_generation: int,
        expected_manifest_sha256: str,
        lease_id: str,
        orchestration_run_id: str,
        expires_at: datetime,
    ) -> RuntimeGenerationLease:
        with self._orchestration.unit_of_work() as uow:
            row = uow.automation_plugins.acquire_committed_generation_lease_row(
                automation_id,
                expected_generation=expected_generation,
                expected_manifest_sha256=expected_manifest_sha256,
                lease_id=lease_id,
                orchestration_run_id=orchestration_run_id,
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

    def check_finance_startup_occurrence(
        self,
        *,
        automation_id: str,
        generation: int,
        configuration_version: int,
        occurrence: str,
        idempotency_key: str,
    ) -> dict[str, bool | str]:
        """Return Agent-owned, exact evidence for one startup occurrence."""

        with self._orchestration.unit_of_work() as uow:
            reader = getattr(
                uow.automation_plugins,
                "finance_startup_occurrence_gate_row",
                None,
            )
            if not callable(reader):
                raise ValueError("finance startup occurrence gate is unavailable")
            result = reader(
                automation_id=automation_id,
                generation=generation,
                configuration_version=configuration_version,
                occurrence=occurrence,
                idempotency_key=idempotency_key,
            )
        if not isinstance(result, Mapping):
            raise ValueError("finance startup occurrence gate returned invalid evidence")
        return dict(result)

    def inspect_unknown_write_recovery(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
    ) -> dict[str, Any]:
        """Read locked durable identity evidence; never resolves a lease."""

        with self._orchestration.unit_of_work() as uow:
            reader = getattr(
                uow.automation_plugins,
                "unknown_write_recovery_snapshot_row",
                None,
            )
            if not callable(reader):
                raise ValueError("unknown-write recovery evidence is unavailable")
            result = reader(
                automation_id=automation_id,
                generation=generation,
                lease_id=lease_id,
            )
        if not isinstance(result, Mapping):
            raise ValueError("unknown-write recovery evidence is invalid")
        return dict(result)

    def resolve_unknown_write_not_applied(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        evidence_sha256: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Resolve one externally read-back, pre-write failure atomically."""

        with self._orchestration.unit_of_work() as uow:
            resolver = getattr(
                uow.automation_plugins,
                "resolve_unknown_generation_write_not_applied_row",
                None,
            )
            if not callable(resolver):
                raise ValueError("runtime unknown-write recovery is unavailable")
            result = resolver(
                automation_id,
                generation,
                lease_id,
                evidence_sha256=evidence_sha256,
            )
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"boyi:automation-plugin-generation-recovery:{request_id}",
                )
            )
            uow.events.append_with_outbox(
                {
                    "event_id": event_id,
                    "event_type": "automation_plugin.generation_recovered",
                    "schema_version": 1,
                    "source_system": "agent",
                    "source_event_id": f"plugin-generation-recovery:{request_id}",
                    "entity_type": "automation_project",
                    "entity_id": automation_id,
                    "correlation_id": request_id,
                    "payload": {
                        "automation_id": automation_id,
                        "generation": generation,
                        "lease_id": lease_id,
                        "outcome": "NOT_APPLIED",
                        "evidence_sha256": evidence_sha256,
                        "actor_id": actor_id,
                        "actor_role": actor_role,
                    },
                },
                (
                    {
                        "consumer_name": "orchestration.audit",
                        "topic": "automation_plugin.generation_recovered",
                        "partition_key": automation_id,
                        "max_attempts": 10,
                    },
                ),
            )
            uow.commit()
        if not isinstance(result, Mapping):
            raise ValueError("runtime generation recovery did not persist")
        return dict(result)

    def resolve_unknown_write_recovery(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """Run the server-owned receipt recovery as one orchestration UoW."""

        with self._orchestration.unit_of_work() as uow:
            resolver = getattr(uow, "recover_unknown_automation_write", None)
            if not callable(resolver):
                raise ValueError("transactional unknown-write recovery is unavailable")
            result = resolver(
                automation_id=automation_id,
                generation=generation,
                lease_id=lease_id,
                request_id=request_id,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            uow.commit()
        if not isinstance(result, Mapping):
            raise ValueError("transactional unknown-write recovery returned invalid data")
        return dict(result)

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
