from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from agent.automation_plugins.catalog import (
    PluginCatalog,
    _entry_from_project,
    project_capability_from_snapshot,
    project_contract_fragment,
)
from agent.automation_plugins.generation import AutomationRuntimeReconciler
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    canonical_json_bytes,
    governance_anchor_from_tool_contract,
)
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    PluginVersionRecord,
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
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter,
    MySQLAutomationPluginRuntimeAdapter,
    snapshot_from_row,
    snapshot_to_row,
)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _persisted_sha(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _synthetic_manifest(version: str) -> AutomationPluginManifest:
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"marker": {"type": "string"}},
        "required": ["marker"],
    }
    tool_contract = {
        "name": "synthetic_upgrade_action",
        "version": version,
        "description": f"synthetic action {version}",
        "operation_type": "read",
        "risk_level": "low",
        "approval": {"mode": "none"},
        "permissions": [],
        "idempotency": {"required": True},
        "retry": {"mode": "never"},
        "evidence": [],
        "postconditions": [],
        "project_full_auto_allowed": False,
        "executor": "payload/main.py",
        "input_schema": copy.deepcopy(config_schema),
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
    }
    return AutomationPluginManifest.from_mapping(
        {
            "schema_version": 1,
            "plugin_id": "synthetic_upgrade_action",
            "name": "Synthetic upgrade action",
            "version": version,
            "description": "Synthetic action used only by generation tests",
            "execution_platform": "server",
            "runtime": {
                "kind": "python_subprocess",
                "entrypoint": "payload/main.py",
            },
            "config_schema": config_schema,
            "account_roles": [
                {
                    "role": "source",
                    "allowed_systems": ["ronghui"],
                    "required": True,
                    "argument_field": None,
                    "collection": False,
                }
            ],
            "resource_roles": [],
            "scheduling": {
                "supported": False,
                "allowed_kinds": [],
                "max_daily_times": 0,
            },
            "allowed_entrypoints": ["console"],
            "invocation_contracts": {
                "console": {
                    "input_schema": copy.deepcopy(config_schema),
                    "argument_template": {
                        "marker": {"source": "project_config", "key": "marker"}
                    },
                    "dynamic_resolvers": {},
                }
            },
            "governance_anchor": governance_anchor_from_tool_contract(tool_contract),
            "tool_contract": tool_contract,
            "worker_requirement": {
                "required": False,
                "interactive_session": False,
                "supported_os": ["linux"],
                "queue_deadline_seconds": 60,
            },
            "project_full_auto_allowed": False,
            "runtime_permissions": {
                "network": False,
                "browser": False,
                "office": False,
                "file_roles": [],
                "broker_operations": [],
                "max_broker_calls": 0,
            },
        }
    )


def _snapshot(
    *,
    automation_id: str,
    generation: int,
    manifest: AutomationPluginManifest,
    package_sha256: str,
    project_config: Mapping[str, Any],
    account_id: str,
    schedule_time: str,
    install_root: str,
) -> RuntimeGenerationSnapshot:
    account_role = str(manifest.account_roles[0]["role"])
    account_bindings = {account_role: account_id}
    resource_bindings: dict[str, str] = {}
    schedule = {
        "kind": "daily_times",
        "times": [schedule_time],
        "enabled": True,
    }
    action_contract = copy.deepcopy(dict(manifest.tool_contract))
    governance_anchor = copy.deepcopy(dict(manifest.governance_anchor))
    execution_metadata = {
        "project_config_version": generation,
        "project_config": dict(project_config),
        "account_bindings": account_bindings,
        "resource_bindings": resource_bindings,
        "device_binding": None,
        "schedule": schedule,
        "compiled_invocations": {
            "console": {"arguments": dict(project_config), "dynamic_resolvers": {}}
        },
        "runtime_descriptor": {
            "install_metadata": {
                "install_root": install_root,
                "python_relative": "venv/bin/python",
            },
            "runtime": copy.deepcopy(dict(manifest.runtime)),
            "runtime_permissions": copy.deepcopy(dict(manifest.runtime_permissions)),
            "account_roles": [copy.deepcopy(dict(item)) for item in manifest.account_roles],
            "resource_roles": [copy.deepcopy(dict(item)) for item in manifest.resource_roles],
        },
        "action_contract": action_contract,
        "governance_anchor": governance_anchor,
    }
    return RuntimeGenerationSnapshot(
        automation_id=automation_id,
        generation=generation,
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.version,
        package_sha256=package_sha256,
        manifest_sha256=manifest.manifest_sha256,
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        project_config_sha256=_sha(project_config),
        account_bindings_sha256=_sha(account_bindings),
        resource_bindings_sha256=_sha(resource_bindings),
        device_binding_sha256=_sha(None),
        schedule_sha256=_sha(schedule),
        core_registry_sha256=_sha({"registry": "unchanged"}),
        tool_contract_sha256=_sha(action_contract),
        invocation_contracts_sha256=_sha(
            manifest.to_mapping()["invocation_contracts"]
        ),
        compiled_invocations_sha256=_sha(
            execution_metadata["compiled_invocations"]
        ),
        runtime_descriptor_sha256=_sha(execution_metadata["runtime_descriptor"]),
        governance_anchor_sha256=_sha(governance_anchor),
        policy_contract_sha256=_sha({"mode": "REQUIRE_EACH_RUN"}),
        enabled_entrypoints=("console",),
        execution_metadata=execution_metadata,
    )


def _version(
    manifest: AutomationPluginManifest,
    snapshot: RuntimeGenerationSnapshot,
) -> PluginVersionRecord:
    return PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256=snapshot.package_sha256,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        install_root=str(
            snapshot.execution_metadata["runtime_descriptor"]["install_metadata"][
                "install_root"
            ]
        ),
        install_metadata={
            "install_root": snapshot.execution_metadata["runtime_descriptor"][
                "install_metadata"
            ]["install_root"],
            "python_relative": "venv/bin/python",
        },
    )


def _config_record(
    snapshot: RuntimeGenerationSnapshot,
    *,
    marker: str,
    account_id: str,
    schedule_time: str,
) -> AutomationProjectConfigRecord:
    return AutomationProjectConfigRecord(
        automation_id=snapshot.automation_id,
        config={"marker": marker},
        account_bindings={"source": account_id},
        resource_bindings={},
        schedule={
            "kind": "daily_times",
            "times": [schedule_time],
            "enabled": True,
        },
        config_version=snapshot.generation,
        configured=True,
        config_sha256=snapshot.project_config_sha256,
        account_bindings_sha256=snapshot.account_bindings_sha256,
        resource_bindings_sha256=snapshot.resource_bindings_sha256,
        device_binding_sha256=snapshot.device_binding_sha256,
        enabled_entrypoints=("console",),
    )


class _CatalogRepository:
    def __init__(self, project: PluginInstanceRecord) -> None:
        self.project = project

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None:
        return self.project if self.project.automation_id == automation_id else None

    def list_instances(self) -> list[PluginInstanceRecord]:
        return [self.project]


class _ConfigurationRepository:
    def __init__(self, record: AutomationProjectConfigRecord) -> None:
        self.record = record

    def get_project_config(self, automation_id: str) -> AutomationProjectConfigRecord | None:
        return self.record if self.record.automation_id == automation_id else None


class _LowLevelRuntimeRepository:
    def __init__(
        self,
        snapshots: Mapping[int, RuntimeGenerationSnapshot],
        *,
        enabled: bool = True,
    ) -> None:
        self.snapshots = dict(snapshots)
        self.committed_generation = 1
        self.generations = {
            1: RuntimeGenerationRecord(
                snapshot=self.snapshots[1],
                state=RuntimeGenerationState.COMMITTED,
            )
        }
        self.project_runtime = ProjectRuntimeRecord(
            automation_id=self.snapshots[1].automation_id,
            target_generation=1,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.STABLE,
            record_version=1,
        )
        self.enabled = enabled
        self.project_state = (
            PluginProjectState.ENABLED if enabled else PluginProjectState.DISABLED
        )
        self.leases: dict[str, dict[str, Any]] = {}
        self.released: list[tuple[str, str]] = []
        self.finalized: list[tuple[str, str]] = []

    def acquire_committed_generation_lease_row(
        self,
        automation_id: str,
        *,
        expected_generation: int,
        expected_manifest_sha256: str,
        lease_id: str,
        orchestration_run_id: str,
        expires_at: datetime,
        lease_owner: str,
    ) -> dict[str, Any]:
        assert lease_owner == "agent-runtime"
        assert expected_generation == self.committed_generation
        snapshot = self.snapshots[expected_generation]
        assert automation_id == snapshot.automation_id
        assert expected_manifest_sha256 == snapshot.manifest_sha256
        row = {
            "lease_id": lease_id,
            "automation_id": automation_id,
            "generation": expected_generation,
            "orchestration_run_id": orchestration_run_id,
            "outcome": "RUNNING",
            "acquired_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
        }
        self.leases[lease_id] = row
        return row

    def get_generation_row(
        self,
        automation_id: str,
        generation: int,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        record = self.generations.get(generation)
        if record is None or record.snapshot.automation_id != automation_id:
            return None
        snapshot = record.snapshot
        raw_snapshot = snapshot_to_row(snapshot)
        return {
            **raw_snapshot,
            "state": record.state.value,
            "snapshot_json": raw_snapshot,
            "snapshot_sha256": _persisted_sha(raw_snapshot),
            "enabled_entrypoints_sha256": _persisted_sha(
                list(snapshot.enabled_entrypoints)
            ),
            "coeffects": [],
            "effects": [],
        }

    def release_generation_lease_row(self, lease_id: str, *, outcome: str) -> None:
        self.released.append((lease_id, outcome))
        if outcome != RuntimeLeaseOutcome.VERIFYING.value:
            self.leases.pop(lease_id, None)

    def finalize_generation_write_row(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        outcome: str,
        evidence_sha256: str,
    ) -> None:
        assert automation_id == "customer-instance"
        assert generation in self.snapshots
        assert len(evidence_sha256) == 64
        self.finalized.append((lease_id, outcome))
        self.leases.pop(lease_id, None)

    def get_project_runtime(self, automation_id: str) -> ProjectRuntimeRecord | None:
        if automation_id != self.project_runtime.automation_id:
            return None
        return self.project_runtime

    def list_project_runtimes(self) -> Sequence[ProjectRuntimeRecord]:
        return (self.project_runtime,)

    def get_generation(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord | None:
        record = self.generations.get(generation)
        if record is None or record.snapshot.automation_id != automation_id:
            return None
        return record

    def list_project_generations(
        self,
        automation_id: str,
    ) -> Sequence[RuntimeGenerationRecord]:
        return tuple(
            record
            for record in self.generations.values()
            if record.snapshot.automation_id == automation_id
        )

    def allocate_target_generation(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        expected_committed_generation: int | None,
        request_id: str,
    ) -> RuntimeGenerationRecord:
        assert request_id
        existing = self.generations.get(snapshot.generation)
        if existing is not None:
            assert existing.snapshot == snapshot
            return existing
        assert self.project_runtime.committed_generation == expected_committed_generation
        record = RuntimeGenerationRecord(
            snapshot=snapshot,
            state=RuntimeGenerationState.TARGET,
        )
        self.snapshots[snapshot.generation] = snapshot
        self.generations[snapshot.generation] = record
        self.project_runtime = replace(
            self.project_runtime,
            target_generation=snapshot.generation,
            reconcile_state=RuntimeReconcileState.PREPARING,
            record_version=self.project_runtime.record_version + 1,
        )
        return record

    def _set_generation_state(
        self,
        generation: int,
        state: RuntimeGenerationState,
    ) -> None:
        self.generations[generation] = replace(
            self.generations[generation],
            state=state,
        )

    def mark_generation_preparing(self, automation_id: str, generation: int) -> None:
        assert automation_id == self.project_runtime.automation_id
        self._set_generation_state(generation, RuntimeGenerationState.PREPARING)

    def replace_generation_coeffects(
        self,
        automation_id: str,
        generation: int,
        coeffects: Sequence[RuntimeCoeffectSnapshot],
    ) -> None:
        assert automation_id == self.project_runtime.automation_id
        self.generations[generation] = replace(
            self.generations[generation],
            coeffects=tuple(coeffects),
        )

    def mark_generation_waiting_coeffects(
        self,
        automation_id: str,
        generation: int,
        *,
        reason_codes: Sequence[str],
    ) -> None:
        assert automation_id == self.project_runtime.automation_id
        assert reason_codes
        self._set_generation_state(
            generation,
            RuntimeGenerationState.WAITING_COEFFECTS,
        )
        self.project_runtime = replace(
            self.project_runtime,
            reconcile_state=RuntimeReconcileState.WAITING_COEFFECTS,
        )

    def reserve_generation_effect(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        plan: RuntimeEffectPlan,
        sequence: int,
    ) -> RuntimeEffectRecord:
        record = self.generations[snapshot.generation]
        existing = next(
            (effect for effect in record.effects if effect.sequence == sequence),
            None,
        )
        if existing is not None:
            return existing
        effect = RuntimeEffectRecord(
            effect_id=f"{snapshot.generation}:{sequence}",
            automation_id=snapshot.automation_id,
            generation=snapshot.generation,
            sequence=sequence,
            kind=plan.kind,
            state=RuntimeEffectState.PLANNED,
            reversible=plan.reversible,
            effect_key=plan.effect_key,
            payload=dict(plan.payload),
        )
        self.generations[snapshot.generation] = replace(
            record,
            effects=(*record.effects, effect),
        )
        return effect

    def _replace_effect(self, replacement: RuntimeEffectRecord) -> None:
        record = self.generations[replacement.generation]
        self.generations[replacement.generation] = replace(
            record,
            effects=tuple(
                replacement if effect.effect_id == replacement.effect_id else effect
                for effect in record.effects
            ),
        )

    def mark_generation_effect_applied(
        self,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        self._replace_effect(effect)
        return effect

    def mark_generation_prepared(self, automation_id: str, generation: int) -> None:
        assert automation_id == self.project_runtime.automation_id
        self._set_generation_state(generation, RuntimeGenerationState.PREPARED)
        self.project_runtime = replace(
            self.project_runtime,
            reconcile_state=RuntimeReconcileState.READY_TO_COMMIT,
        )

    def commit_generation_cas(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_committed_generation: int | None,
    ) -> ProjectRuntimeRecord:
        assert automation_id == self.project_runtime.automation_id
        assert self.project_runtime.committed_generation == expected_committed_generation
        assert self.generations[generation].state == RuntimeGenerationState.PREPARED
        self._set_generation_state(generation, RuntimeGenerationState.COMMITTED)
        self.committed_generation = generation
        self.project_state = (
            PluginProjectState.ENABLED if self.enabled else PluginProjectState.DISABLED
        )
        self.project_runtime = replace(
            self.project_runtime,
            committed_generation=generation,
            reconcile_state=RuntimeReconcileState.DRAINING,
            record_version=self.project_runtime.record_version + 1,
        )
        return self.project_runtime

    def mark_generation_draining(self, automation_id: str, generation: int) -> None:
        assert automation_id == self.project_runtime.automation_id
        self._set_generation_state(generation, RuntimeGenerationState.DRAINING)
        self.project_runtime = replace(
            self.project_runtime,
            reconcile_state=RuntimeReconcileState.DRAINING,
        )

    def list_active_generation_leases(
        self,
        automation_id: str,
        generation: int,
    ) -> Sequence[RuntimeGenerationLease]:
        snapshot = self.snapshots[generation]
        return tuple(
            RuntimeGenerationLease(
                lease_id=str(row["lease_id"]),
                automation_id=automation_id,
                generation=generation,
                snapshot=snapshot,
                runtime_metadata={},
                acquired_at=row["acquired_at"],
                expires_at=row["expires_at"],
                outcome=RuntimeLeaseOutcome(str(row["outcome"])),
            )
            for row in self.leases.values()
            if row["automation_id"] == automation_id
            and row["generation"] == generation
        )

    def has_unknown_generation_write(
        self,
        automation_id: str,
        generation: int,
    ) -> bool:
        assert automation_id == self.project_runtime.automation_id
        assert generation in self.generations
        return False

    def reserve_generation_dispose(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord:
        assert not self.list_active_generation_leases(automation_id, generation)
        self._set_generation_state(generation, RuntimeGenerationState.DISPOSING)
        return self.generations[generation]

    def _replace_effect_state(
        self,
        effect_id: str,
        state: RuntimeEffectState,
    ) -> None:
        for record in self.generations.values():
            for effect in record.effects:
                if effect.effect_id == effect_id:
                    self._replace_effect(replace(effect, state=state))
                    return
        raise AssertionError(f"unknown effect: {effect_id}")

    def mark_generation_effect_disposing(self, effect_id: str) -> None:
        self._replace_effect_state(effect_id, RuntimeEffectState.DISPOSING)

    def mark_generation_effect_disposed(self, effect_id: str) -> None:
        self._replace_effect_state(effect_id, RuntimeEffectState.DISPOSED)

    def complete_generation_dispose(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        assert automation_id == self.project_runtime.automation_id
        self._set_generation_state(generation, RuntimeGenerationState.DISPOSED)
        self.project_runtime = replace(
            self.project_runtime,
            reconcile_state=RuntimeReconcileState.STABLE,
        )

    def fail_generation(
        self,
        automation_id: str,
        generation: int,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        assert automation_id == self.project_runtime.automation_id
        assert error_code and error_summary
        self._set_generation_state(generation, RuntimeGenerationState.FAILED)

    def block_generation_unknown_write(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        assert automation_id == self.project_runtime.automation_id
        self._set_generation_state(generation, RuntimeGenerationState.BLOCKED)
        self.project_runtime = replace(
            self.project_runtime,
            reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
        )


class _ReadyCoeffects:
    def observe(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> Sequence[RuntimeCoeffectSnapshot]:
        return (
            RuntimeCoeffectSnapshot(
                kind=RuntimeCoeffectKind.CORE_ADAPTER,
                key="synthetic-adapter",
                revision=f"ready-for-{snapshot.plugin_version}",
                ready=True,
            ),
        )


class _RuntimeEffectPlanner:
    def plan(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> Sequence[RuntimeEffectPlan]:
        return (
            RuntimeEffectPlan(
                kind=RuntimeEffectKind.PACKAGE_REFERENCE,
                effect_key=f"package:{snapshot.plugin_version}",
                payload={"version": snapshot.plugin_version},
            ),
        )


class _RuntimeEffectDriver:
    def ensure_applied(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        plan: RuntimeEffectPlan,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        assert snapshot.generation == effect.generation
        assert plan.effect_key == effect.effect_key
        return replace(effect, state=RuntimeEffectState.APPLIED)

    def dispose(self, effect: RuntimeEffectRecord) -> None:
        assert effect.reversible is True


class _UnitOfWork:
    def __init__(self, low_level: _LowLevelRuntimeRepository) -> None:
        self.automation_plugins = low_level
        self.committed = False

    def __enter__(self) -> _UnitOfWork:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def commit(self) -> None:
        self.committed = True


class _OrchestrationRepository:
    def __init__(self, low_level: _LowLevelRuntimeRepository) -> None:
        self.low_level = low_level

    def unit_of_work(self) -> _UnitOfWork:
        return _UnitOfWork(self.low_level)


def test_catalog_accepts_legacy_signed_runtime_permissions_during_upgrade() -> None:
    source = _synthetic_manifest("1.0.0").to_mapping()
    source["runtime_permissions"] = {
        "network": True,
        "browser": False,
        "office": False,
        "file_roles": [],
        "broker_operations": [
            {
                "operation": "network.request",
                "action": "synthetic.read",
                "roles": ["source"],
            }
        ],
        "max_broker_calls": 1,
    }
    manifest = AutomationPluginManifest.from_mapping(source)
    signed_manifest = manifest.to_signed_mapping()
    assert manifest.runtime_permissions["broker_operations"][0]["effect"] == "write"
    assert "effect" not in signed_manifest["runtime_permissions"]["broker_operations"][0]

    snapshot = _snapshot(
        automation_id="legacy-upgrade-instance",
        generation=1,
        manifest=manifest,
        package_sha256="4" * 64,
        project_config={"marker": "A"},
        account_id="account-A",
        schedule_time="09:00",
        install_root="/plugins/action/legacy-v1",
    )
    execution_metadata = copy.deepcopy(dict(snapshot.execution_metadata))
    execution_metadata["runtime_descriptor"]["runtime_permissions"] = copy.deepcopy(
        signed_manifest["runtime_permissions"]
    )
    snapshot = replace(
        snapshot,
        runtime_descriptor_sha256=_sha(execution_metadata["runtime_descriptor"]),
        execution_metadata=execution_metadata,
    )
    generation_row = _LowLevelRuntimeRepository({1: snapshot}).get_generation_row(
        snapshot.automation_id,
        snapshot.generation,
    )
    assert generation_row is not None
    committed_version = replace(_version(manifest, snapshot), manifest=signed_manifest)
    committed_version_row = {
        "plugin_id": committed_version.plugin_id,
        "version": committed_version.version,
        "package_sha256": committed_version.package_sha256,
        "manifest_sha256": committed_version.manifest_sha256,
        "manifest_json": committed_version.manifest,
        "trust_source": committed_version.trust_source.value,
        "install_root_metadata_json": {
            **committed_version.install_metadata,
            "install_root": committed_version.install_root,
        },
        "state": committed_version.state.value,
        "installed_at": committed_version.installed_at,
    }
    desired_manifest = _synthetic_manifest("2.0.0")
    desired_snapshot = _snapshot(
        automation_id=snapshot.automation_id,
        generation=2,
        manifest=desired_manifest,
        package_sha256="6" * 64,
        project_config={"marker": "A"},
        account_id="account-A",
        schedule_time="09:00",
        install_root="/plugins/action/v2",
    )
    desired_version = _version(desired_manifest, desired_snapshot)
    desired_version_row = {
        "plugin_id": desired_version.plugin_id,
        "version": desired_version.version,
        "package_sha256": desired_version.package_sha256,
        "manifest_sha256": desired_version.manifest_sha256,
        "manifest_json": desired_version.manifest,
        "trust_source": desired_version.trust_source.value,
        "install_root_metadata_json": {
            **desired_version.install_metadata,
            "install_root": desired_version.install_root,
        },
        "state": desired_version.state.value,
        "installed_at": desired_version.installed_at,
    }

    class _CatalogLowLevel:
        def get_generation_row(
            self,
            automation_id: str,
            generation: int,
        ) -> Mapping[str, Any] | None:
            if (automation_id, generation) == (
                snapshot.automation_id,
                snapshot.generation,
            ):
                return generation_row
            return None

        def get_version(
            self,
            plugin_id: str,
            plugin_version: str,
        ) -> Mapping[str, Any] | None:
            if (plugin_id, plugin_version) == (manifest.plugin_id, manifest.version):
                return committed_version_row
            if (plugin_id, plugin_version) == (
                desired_manifest.plugin_id,
                desired_manifest.version,
            ):
                return desired_version_row
            return None

    row = {
        "automation_id": snapshot.automation_id,
        "display_name": "legacy upgrade instance",
        "plugin_id": manifest.plugin_id,
        "plugin_version": desired_manifest.version,
        "state": PluginProjectState.UPGRADING.value,
        "enabled": 1,
        "record_version": 2,
        "target_generation": 2,
        "committed_generation": 1,
        "reconcile_state": RuntimeReconcileState.PREPARING.value,
    }

    project = MySQLAutomationPluginCatalogRepositoryAdapter._project_from_row(
        _CatalogLowLevel(),
        row,
    )

    assert project.committed_snapshot == snapshot
    assert project.active_version.version == desired_manifest.version
    committed_contract = project_contract_fragment(_entry_from_project(project))
    assert committed_contract["committed_generation"] == snapshot.generation
    capability = project_capability_from_snapshot(snapshot)
    assert capability["_plugin_runtime"]["runtime_permissions"][
        "broker_operations"
    ][0]["effect"] == "write"
    assert "effect" not in snapshot.execution_metadata["runtime_descriptor"][
        "runtime_permissions"
    ]["broker_operations"][0]


def test_catalog_accepts_blocked_committed_generation_for_reconciliation() -> None:
    manifest = _synthetic_manifest("1.0.0")
    snapshot = _snapshot(
        automation_id="blocked-instance",
        generation=1,
        manifest=manifest,
        package_sha256="5" * 64,
        project_config={"marker": "A"},
        account_id="account-A",
        schedule_time="09:00",
        install_root="/plugins/action/blocked",
    )
    generation_row = _LowLevelRuntimeRepository({1: snapshot}).get_generation_row(
        "blocked-instance",
        1,
    )
    assert generation_row is not None
    generation_row["state"] = RuntimeGenerationState.BLOCKED.value
    version = _version(manifest, snapshot)
    version_row = {
        "plugin_id": version.plugin_id,
        "version": version.version,
        "package_sha256": version.package_sha256,
        "manifest_sha256": version.manifest_sha256,
        "manifest_json": version.manifest,
        "trust_source": version.trust_source.value,
        "install_root_metadata_json": {
            **version.install_metadata,
            "install_root": version.install_root,
        },
        "state": version.state.value,
        "installed_at": version.installed_at,
    }

    class _CatalogLowLevel:
        def get_generation_row(
            self,
            automation_id: str,
            generation: int,
        ) -> Mapping[str, Any] | None:
            if automation_id == "blocked-instance" and generation == 1:
                return generation_row
            return None

        def get_version(
            self,
            plugin_id: str,
            plugin_version: str,
        ) -> Mapping[str, Any] | None:
            if (plugin_id, plugin_version) == (manifest.plugin_id, manifest.version):
                return version_row
            return None

    row = {
        "automation_id": "blocked-instance",
        "display_name": "blocked instance",
        "plugin_id": manifest.plugin_id,
        "plugin_version": manifest.version,
        "state": PluginProjectState.ENABLED.value,
        "enabled": 1,
        "record_version": 1,
        "target_generation": 1,
        "committed_generation": 1,
        "reconcile_state": RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE.value,
    }
    project = MySQLAutomationPluginCatalogRepositoryAdapter._project_from_row(
        _CatalogLowLevel(),
        row,
    )

    assert project.committed_generation == 1
    assert project.committed_snapshot == snapshot

    generation_row["state"] = RuntimeGenerationState.DISPOSED.value
    try:
        MySQLAutomationPluginCatalogRepositoryAdapter._project_from_row(
            _CatalogLowLevel(),
            row,
        )
    except ValueError as exc:
        assert "missing or not committed" in str(exc)
    else:
        raise AssertionError("disposed committed generation was accepted")


def test_catalog_and_leases_remain_pinned_across_atomic_generation_switch() -> None:
    manifest_v1 = _synthetic_manifest("1.0.0")
    manifest_v2 = _synthetic_manifest("2.0.0")
    snapshot_v1 = _snapshot(
        automation_id="customer-instance",
        generation=1,
        manifest=manifest_v1,
        package_sha256="1" * 64,
        project_config={"marker": "A"},
        account_id="account-A",
        schedule_time="09:00",
        install_root="/plugins/action/v1",
    )
    snapshot_v2 = _snapshot(
        automation_id="customer-instance",
        generation=2,
        manifest=manifest_v2,
        package_sha256="2" * 64,
        project_config={"marker": "B"},
        account_id="account-B",
        schedule_time="10:00",
        install_root="/plugins/action/v2",
    )
    project = PluginInstanceRecord(
        automation_id="customer-instance",
        display_name="customer A/B",
        plugin_id=manifest_v1.plugin_id,
        state=PluginProjectState.ENABLED,
        active_version=_version(manifest_v1, snapshot_v1),
        enabled=True,
        target_generation=1,
        committed_generation=1,
        reconcile_state=RuntimeReconcileState.STABLE,
        committed_snapshot=snapshot_v1,
    )
    project_repo = _CatalogRepository(project)
    config_repo = _ConfigurationRepository(
        _config_record(
            snapshot_v1,
            marker="A",
            account_id="account-A",
            schedule_time="09:00",
        )
    )
    catalog = PluginCatalog(project_repo, config_repo)
    committed_a = catalog.get_project_capability("customer-instance")
    committed_a_meta = committed_a["_plugin_runtime"]
    assert catalog.require("customer-instance").enabled is True
    assert catalog.require("customer-instance").state == "ENABLED"
    assert committed_a_meta["generation"] == 1
    assert committed_a_meta["version"] == "1.0.0"

    low_level = _LowLevelRuntimeRepository({1: snapshot_v1, 2: snapshot_v2})
    runtime = MySQLAutomationPluginRuntimeAdapter(_OrchestrationRepository(low_level))
    old_lease = runtime.acquire_committed_generation(
        "customer-instance",
        expected_generation=1,
        expected_manifest_sha256=snapshot_v1.manifest_sha256,
        lease_id="lease-v1",
        orchestration_run_id="run-v1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert old_lease.orchestration_run_id == "run-v1"

    project_repo.project = PluginInstanceRecord(
        automation_id="customer-instance",
        display_name="customer A/B",
        plugin_id=manifest_v2.plugin_id,
        state=PluginProjectState.UPGRADING,
        active_version=_version(manifest_v2, snapshot_v2),
        enabled=True,
        record_version=2,
        target_generation=2,
        committed_generation=1,
        reconcile_state=RuntimeReconcileState.PREPARING,
        committed_snapshot=snapshot_v1,
    )
    config_repo.record = _config_record(
        snapshot_v2,
        marker="B",
        account_id="account-B",
        schedule_time="10:00",
    )
    desired_b = catalog.require("customer-instance")
    assert desired_b.installed_version == "2.0.0"
    assert desired_b.state == "UPGRADING"
    assert desired_b.enabled is True
    assert desired_b.target_generation == 2
    assert desired_b.committed_generation == 1

    precommit = catalog.get_project_capability("customer-instance")
    pre_meta = precommit["_plugin_runtime"]
    assert pre_meta["generation"] == 1
    assert pre_meta["version"] == "1.0.0"
    assert pre_meta["account_bindings"] == {
        str(manifest_v1.account_roles[0]["role"]): "account-A"
    }
    assert pre_meta["install_root"] == "/plugins/action/v1"
    assert catalog.safe_projection()["instances"][0]["config"] == {"marker": "B"}
    fragment = project_contract_fragment(catalog.require("customer-instance"))
    assert fragment["project_config_sha256"] == snapshot_v1.project_config_sha256

    reconcile_result = AutomationRuntimeReconciler(
        repository=low_level,
        coeffects=_ReadyCoeffects(),
        planner=_RuntimeEffectPlanner(),
        driver=_RuntimeEffectDriver(),
    ).reconcile(
        snapshot_v2,
        expected_committed_generation=1,
        request_id="upgrade-request",
    )
    assert reconcile_result.committed_generation == 2
    assert reconcile_result.draining_generations == (1,)
    assert reconcile_result.disposed_generations == ()
    assert low_level.generations[1].state == RuntimeGenerationState.DRAINING
    assert low_level.generations[2].state == RuntimeGenerationState.COMMITTED
    assert low_level.project_state == PluginProjectState.ENABLED
    project_repo.project = PluginInstanceRecord(
        automation_id="customer-instance",
        display_name="customer A/B",
        plugin_id=manifest_v2.plugin_id,
        state=PluginProjectState.ENABLED,
        active_version=_version(manifest_v2, snapshot_v2),
        enabled=True,
        record_version=3,
        target_generation=2,
        committed_generation=2,
        reconcile_state=RuntimeReconcileState.DRAINING,
        committed_snapshot=snapshot_v2,
    )
    postcommit = catalog.get_project_capability("customer-instance")
    post_meta = postcommit["_plugin_runtime"]
    assert post_meta["generation"] == 2
    assert post_meta["version"] == "2.0.0"
    assert post_meta["account_bindings"] == {
        str(manifest_v2.account_roles[0]["role"]): "account-B"
    }
    assert post_meta["install_root"] == "/plugins/action/v2"
    assert old_lease.runtime_metadata["_plugin_runtime"]["version"] == "1.0.0"
    assert old_lease.runtime_metadata["_plugin_runtime"]["account_bindings"] == {
        str(manifest_v1.account_roles[0]["role"]): "account-A"
    }
    new_lease = runtime.acquire_committed_generation(
        "customer-instance",
        expected_generation=2,
        expected_manifest_sha256=snapshot_v2.manifest_sha256,
        lease_id="lease-v2",
        orchestration_run_id="run-v2",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert new_lease.orchestration_run_id == "run-v2"
    assert new_lease.runtime_metadata["_plugin_runtime"]["version"] == "2.0.0"

    runtime.release_generation(old_lease, outcome=RuntimeLeaseOutcome.VERIFYING)
    runtime.finalize_generation_write(
        automation_id="customer-instance",
        generation=1,
        lease_id=old_lease.lease_id,
        outcome=RuntimeLeaseOutcome.WRITE_VERIFIED,
        evidence_sha256="a" * 64,
    )
    assert low_level.released == [("lease-v1", "VERIFYING")]
    assert low_level.finalized == [("lease-v1", "WRITE_VERIFIED")]


def test_generation_commit_restores_the_persisted_enabled_flag() -> None:
    manifest_v1 = _synthetic_manifest("1.0.0")
    manifest_v2 = _synthetic_manifest("2.0.0")
    snapshot_v1 = _snapshot(
        automation_id="disabled-instance",
        generation=1,
        manifest=manifest_v1,
        package_sha256="1" * 64,
        project_config={"marker": "A"},
        account_id="account-A",
        schedule_time="09:00",
        install_root="/plugins/action/v1",
    )
    snapshot_v2 = _snapshot(
        automation_id="disabled-instance",
        generation=2,
        manifest=manifest_v2,
        package_sha256="2" * 64,
        project_config={"marker": "B"},
        account_id="account-B",
        schedule_time="10:00",
        install_root="/plugins/action/v2",
    )
    low_level = _LowLevelRuntimeRepository(
        {1: snapshot_v1, 2: snapshot_v2},
        enabled=False,
    )

    result = AutomationRuntimeReconciler(
        repository=low_level,
        coeffects=_ReadyCoeffects(),
        planner=_RuntimeEffectPlanner(),
        driver=_RuntimeEffectDriver(),
    ).reconcile(
        snapshot_v2,
        expected_committed_generation=1,
        request_id="disabled-upgrade-request",
    )

    assert result.committed_generation == 2
    assert low_level.enabled is False
    assert low_level.project_state == PluginProjectState.DISABLED


def test_failed_uncommitted_target_keeps_only_committed_capability_available() -> None:
    manifest_v1 = _synthetic_manifest("1.0.0")
    manifest_v2 = _synthetic_manifest("2.0.0")
    snapshot_v1 = _snapshot(
        automation_id="failed-upgrade-instance",
        generation=1,
        manifest=manifest_v1,
        package_sha256="1" * 64,
        project_config={"marker": "A"},
        account_id="account-A",
        schedule_time="09:00",
        install_root="/plugins/action/v1",
    )
    snapshot_v2 = _snapshot(
        automation_id="failed-upgrade-instance",
        generation=2,
        manifest=manifest_v2,
        package_sha256="2" * 64,
        project_config={"marker": "B"},
        account_id="account-B",
        schedule_time="10:00",
        install_root="/plugins/action/v2",
    )
    project_repo = _CatalogRepository(
        PluginInstanceRecord(
            automation_id="failed-upgrade-instance",
            display_name="failed A/B upgrade",
            plugin_id=manifest_v2.plugin_id,
            state=PluginProjectState.UPGRADING,
            active_version=_version(manifest_v2, snapshot_v2),
            enabled=True,
            record_version=3,
            target_generation=2,
            committed_generation=1,
            reconcile_state=RuntimeReconcileState.ERROR,
            committed_snapshot=snapshot_v1,
        )
    )
    catalog = PluginCatalog(
        project_repo,
        _ConfigurationRepository(
            _config_record(
                snapshot_v2,
                marker="B",
                account_id="account-B",
                schedule_time="10:00",
            )
        ),
    )

    desired = catalog.require("failed-upgrade-instance")
    capability = catalog.get_project_capability("failed-upgrade-instance")
    routed = capability["_plugin_runtime"]

    assert desired.installed_version == "2.0.0"
    assert desired.reconcile_state == RuntimeReconcileState.ERROR
    assert routed["generation"] == 1
    assert routed["version"] == "1.0.0"
    assert routed["package_sha256"] == snapshot_v1.package_sha256
    assert routed["install_root"] == "/plugins/action/v1"

    low_level = _LowLevelRuntimeRepository({1: snapshot_v1, 2: snapshot_v2})
    runtime = MySQLAutomationPluginRuntimeAdapter(
        _OrchestrationRepository(low_level)
    )
    old_lease = runtime.acquire_committed_generation(
        "failed-upgrade-instance",
        expected_generation=1,
        expected_manifest_sha256=snapshot_v1.manifest_sha256,
        lease_id="failed-upgrade-old-lease",
        orchestration_run_id="run-old",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert old_lease.runtime_metadata["_plugin_runtime"]["version"] == "1.0.0"
    runtime.release_generation(old_lease, outcome=RuntimeLeaseOutcome.SUCCEEDED)
    assert low_level.released == [("failed-upgrade-old-lease", "SUCCEEDED")]


def test_strict_generation_decoder_rejects_snapshot_and_child_hash_drift() -> None:
    manifest = _synthetic_manifest("1.0.0")
    snapshot = _snapshot(
        automation_id="strict-instance",
        generation=1,
        manifest=manifest,
        package_sha256="3" * 64,
        project_config={"marker": "A"},
        account_id="account-A",
        schedule_time="09:00",
        install_root="/plugins/action/strict",
    )
    raw = snapshot_to_row(snapshot)
    row = {
        **raw,
        "snapshot_json": copy.deepcopy(raw),
        "snapshot_sha256": _persisted_sha(raw),
        "enabled_entrypoints_sha256": _persisted_sha(list(snapshot.enabled_entrypoints)),
    }
    assert snapshot_from_row(row) == snapshot

    tampered_snapshot = copy.deepcopy(row)
    tampered_snapshot["snapshot_json"]["execution_metadata"]["project_config"] = {
        "marker": "tampered"
    }
    try:
        snapshot_from_row(tampered_snapshot)
    except ValueError as exc:
        assert "snapshot digest" in str(exc)
    else:
        raise AssertionError("tampered generation snapshot was accepted")

    tampered_column = copy.deepcopy(row)
    tampered_column["package_sha256"] = "4" * 64
    try:
        snapshot_from_row(tampered_column)
    except ValueError as exc:
        assert "column drifted" in str(exc)
    else:
        raise AssertionError("generation column drift was accepted")

    tampered_version = copy.deepcopy(row)
    tampered_version["plugin_version"] = "9.9.9"
    try:
        snapshot_from_row(tampered_version)
    except ValueError as exc:
        assert "column drifted: plugin_version" in str(exc)
    else:
        raise AssertionError("generation version drift was accepted")

    tampered_child = copy.deepcopy(row)
    tampered_child["snapshot_json"]["execution_metadata"]["compiled_invocations"] = {
        "console": {"arguments": {"marker": "tampered"}, "dynamic_resolvers": {}}
    }
    tampered_child["snapshot_sha256"] = _persisted_sha(
        tampered_child["snapshot_json"]
    )
    try:
        snapshot_from_row(tampered_child)
    except Exception as exc:
        assert "execution metadata hash" in str(exc)
    else:
        raise AssertionError("generation child hash drift was accepted")
