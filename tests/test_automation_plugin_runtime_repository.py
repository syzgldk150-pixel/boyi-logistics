from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginInstanceRecord,
    PluginProjectState,
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
from agent.automation_plugins.mysql_repository import (
    MySQLAutomationPluginRepositoryAdapter,
)
from agent.automation_plugins.ports import RuntimeEffectPlan
from agent.automation_plugins.runtime_repository import (
    MySQLAutomationPluginCatalogRepositoryAdapter,
    MySQLAutomationPluginRuntimeAdapter,
    MySQLAutomationProjectConfigurationReadAdapter,
    snapshot_to_row,
)
from shared.automation_plugin_generation_repository import (
    AutomationPluginGenerationRepositoryMixin,
    _exact_json_hash,
    _lock_scheduled_task_before_image,
    _restore_transition_task_before_image,
)
from shared.automation_plugin_generation_transition_repository import (
    _assert_transition_target_has_no_generation_leases,
)
from shared.orchestration_repository_support import ConcurrentUpdateError


def test_raw_identity_readers_do_not_parse_corrupt_rows() -> None:
    class _LowLevel:
        @staticmethod
        def list_projects() -> list[dict[str, Any]]:
            return [{"automation_id": "bad-project", "state": "not-a-state"}]

        @staticmethod
        def list_project_runtime_rows() -> list[dict[str, Any]]:
            return [
                {
                    "automation_id": "bad-runtime",
                    "reconcile_state": "not-a-state",
                }
            ]

    orchestration = _OrchestrationRepository(_LowLevel())

    assert MySQLAutomationPluginCatalogRepositoryAdapter(
        orchestration
    ).list_instance_ids() == ("bad-project",)
    assert MySQLAutomationPluginRuntimeAdapter(
        orchestration
    ).list_project_runtime_ids() == ("bad-runtime",)

    lifecycle_repository = object.__new__(MySQLAutomationPluginRepositoryAdapter)
    lifecycle_repository._catalog = MySQLAutomationPluginCatalogRepositoryAdapter(
        orchestration
    )
    assert lifecycle_repository.list_instance_ids() == ("bad-project",)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("config_json", [1]),
        ("account_bindings_json", [1]),
        ("resource_bindings_json", [1]),
        ("enabled_entrypoints_json", {"console": True}),
    ),
)
def test_project_config_reader_rejects_valid_json_with_wrong_container_type(
    field: str,
    invalid_value: object,
) -> None:
    row: dict[str, Any] = {
        "automation_id": "bad-project",
        "config_json": {},
        "account_bindings_json": {},
        "resource_bindings_json": {},
        "enabled_entrypoints_json": [],
        "schedule": {"kind": "none", "times": [], "enabled": False},
        "config_version": 1,
        "configured": 1,
        "config_sha256": "",
        "account_bindings_sha256": "",
        "resource_bindings_sha256": "",
        "device_binding_sha256": "",
    }
    row[field] = invalid_value

    class _LowLevel:
        @staticmethod
        def get_project_config(_automation_id: str) -> dict[str, Any]:
            return row

    reader = MySQLAutomationProjectConfigurationReadAdapter(
        _OrchestrationRepository(_LowLevel())
    )

    with pytest.raises(ValueError, match=field):
        reader.get_project_config("bad-project")


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
            base_committed_generation=expected_committed_generation,
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
        transition_token = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"test:{automation_id}:{generation}",
            )
        )
        self.generations[generation] = replace(
            self.generations[generation],
            state=RuntimeGenerationState.COMMITTED,
            activation_transition_token=transition_token,
            activation_phase=RuntimeActivationPhase.PENDING_PROJECTION,
        )
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

    def complete_generation_activation(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_transition_token: str,
    ) -> None:
        assert automation_id == self.project_runtime.automation_id
        record = self.generations[generation]
        assert record.activation_transition_token == expected_transition_token
        assert record.activation_phase in {
            RuntimeActivationPhase.PENDING_PROJECTION,
            RuntimeActivationPhase.ACTIVE,
        }
        self.generations[generation] = replace(
            record,
            activation_phase=RuntimeActivationPhase.ACTIVE,
        )

    def block_generation_activation(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_transition_token: str,
    ) -> None:
        assert automation_id == self.project_runtime.automation_id
        record = self.generations[generation]
        assert record.activation_transition_token == expected_transition_token
        self.generations[generation] = replace(
            record,
            activation_phase=RuntimeActivationPhase.BLOCKED,
        )

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


def test_runtime_adapter_delegates_token_guarded_activation_transitions() -> None:
    from agent.automation_plugins.runtime_repository import _service_v2_signed_activation_contract
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class _LowLevel:
        @staticmethod
        def rollback_generation_cas_row(
            automation_id: str,
            generation: int,
            **kwargs: object,
        ) -> dict[str, object]:
            calls.append(("rollback", (automation_id, generation), kwargs))
            return {
                "automation_id": automation_id,
                "target_generation": generation,
                "committed_generation": 1,
                "reconcile_state": RuntimeReconcileState.READY_TO_COMMIT.value,
                "record_version": 4,
            }

        @staticmethod
        def complete_generation_activation_row(
            automation_id: str,
            generation: int,
            **kwargs: object,
        ) -> None:
            calls.append(("complete", (automation_id, generation), kwargs))

        @staticmethod
        def block_generation_activation_row(
            automation_id: str,
            generation: int,
            **kwargs: object,
        ) -> None:
            calls.append(("block", (automation_id, generation), kwargs))

    adapter = MySQLAutomationPluginRuntimeAdapter(
        _OrchestrationRepository(_LowLevel())  # type: ignore[arg-type]
    )
    transition_token = "00000000-0000-0000-0000-000000000003"

    restored = adapter.rollback_generation_cas(
        "adapter-transition-instance",
        2,
        expected_base_committed_generation=1,
        expected_transition_token=transition_token,
    )
    adapter.complete_generation_activation(
        "adapter-transition-instance",
        2,
        expected_transition_token=transition_token,
    )
    adapter.block_generation_activation(
        "adapter-transition-instance",
        2,
        expected_transition_token=transition_token,
    )

    assert restored.committed_generation == 1
    assert restored.reconcile_state is RuntimeReconcileState.READY_TO_COMMIT
    assert calls == [
        (
            "rollback",
            ("adapter-transition-instance", 2),
            {
                "expected_base_committed_generation": 1,
                "expected_transition_token": transition_token,
            },
        ),
        (
            "complete",
            ("adapter-transition-instance", 2),
            {"expected_transition_token": transition_token,
             "service_v2_contract_projector": _service_v2_signed_activation_contract},
        ),
        (
            "block",
            ("adapter-transition-instance", 2),
            {"expected_transition_token": transition_token},
        ),
    ]


class _ActivationPhaseCursor:
    def __init__(self, *, transition_token: str, phase: str) -> None:
        self.transition_token = transition_token
        self.phase = phase
        self.rowcount = 0
        self._result: dict[str, object] | None = None

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT transition_token, phase"):
            self._result = {
                "transition_token": self.transition_token,
                "phase": self.phase,
            }
            self.rowcount = 1
            return
        if normalized.startswith(
            "UPDATE automation_project_generation_transitions"
        ):
            expected_token = str(params[2])
            if (
                expected_token == self.transition_token
                and self.phase == "PENDING_PROJECTION"
            ):
                self.phase = "BLOCKED"
                self.rowcount = 1
            else:
                self.rowcount = 0
            self._result = None
            return
        raise AssertionError(f"unexpected activation phase SQL: {normalized}")

    def fetchone(self) -> dict[str, object] | None:
        return self._result


class _CursorManager:
    def __init__(self, cursor: object) -> None:
        self.cursor = cursor

    def __enter__(self) -> object:
        return self.cursor

    def __exit__(self, *_args: object) -> bool:
        return False


class _LeaseActivationGateCursor:
    def __init__(
        self,
        *,
        generation_row: Mapping[str, Any],
        transition_phase: str | None,
    ) -> None:
        self.generation_row = dict(generation_row)
        self.transition_phase = transition_phase
        self.lease: dict[str, Any] | None = None
        self._result: dict[str, Any] | None = None
        self.rowcount = 0
        self.calls: list[str] = []

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        normalized = " ".join(sql.split())
        self.calls.append(normalized)
        if normalized.startswith(
            "SELECT committed_generation, enabled, state, reconcile_state"
        ):
            self._result = {
                "committed_generation": 1,
                "enabled": True,
                "state": "ENABLED",
                "reconcile_state": "STABLE",
            }
            self.rowcount = 1
            return
        if normalized.startswith(
            "SELECT * FROM automation_project_generations"
        ):
            self._result = copy.deepcopy(self.generation_row)
            self.rowcount = 1
            return
        if normalized.startswith(
            "SELECT phase FROM automation_project_generation_transitions"
        ):
            self._result = (
                None
                if self.transition_phase is None
                else {"phase": self.transition_phase}
            )
            self.rowcount = int(self._result is not None)
            return
        if normalized.startswith(
            "INSERT INTO automation_project_generation_leases"
        ):
            values = tuple(params)
            self.lease = {
                "lease_id": values[0],
                "automation_id": values[1],
                "generation": values[2],
                "orchestration_run_id": values[3],
                "invocation_id": values[4],
                "lease_owner": values[5],
                "runtime_metadata_json": values[6],
                "runtime_metadata_sha256": values[7],
                "outcome": "RUNNING",
                "expires_at": values[8],
            }
            self._result = None
            self.rowcount = 1
            return
        if normalized.startswith(
            "SELECT * FROM automation_project_generation_leases"
        ):
            self._result = copy.deepcopy(self.lease)
            self.rowcount = int(self._result is not None)
            return
        raise AssertionError(f"unexpected lease activation SQL: {normalized}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._result


class _LeaseActivationGateRepository(AutomationPluginGenerationRepositoryMixin):
    _GENERATION_JSON_FIELDS = ("snapshot_json",)

    def __init__(self, cursor: _LeaseActivationGateCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _CursorManager:
        return _CursorManager(self._cursor)


class _LeaseHistoryCursor:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = [dict(item) for item in rows]
        self.sql = ""

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        self.sql = " ".join(sql.split())
        assert params == ("reverse-lease-instance", 2)

    def fetchall(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.rows)


def test_reverse_cas_rejects_completed_target_generation_lease_history() -> None:
    cursor = _LeaseHistoryCursor(
        [
            {
                "lease_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb8",
                "outcome": "SUCCEEDED",
            }
        ]
    )

    with pytest.raises(ConcurrentUpdateError, match="lease history"):
        _assert_transition_target_has_no_generation_leases(
            cursor,
            automation_id="reverse-lease-instance",
            generation=2,
        )

    assert "outcome IN" not in cursor.sql
    assert "ORDER BY lease_id FOR UPDATE" in cursor.sql


class _ActivationPhaseRepository(AutomationPluginGenerationRepositoryMixin):
    def __init__(self, cursor: _ActivationPhaseCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _CursorManager:
        return _CursorManager(self._cursor)


def test_activation_block_is_token_guarded_and_idempotent() -> None:
    transition_token = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa6"
    cursor = _ActivationPhaseCursor(
        transition_token=transition_token,
        phase="PENDING_PROJECTION",
    )
    repository = _ActivationPhaseRepository(cursor)

    repository.block_generation_activation_row(
        "blocked-transition-instance",
        2,
        expected_transition_token=transition_token,
    )
    assert cursor.phase == "BLOCKED"

    repository.block_generation_activation_row(
        "blocked-transition-instance",
        2,
        expected_transition_token=transition_token,
    )
    assert cursor.phase == "BLOCKED"

    with pytest.raises(ConcurrentUpdateError, match="token changed"):
        repository.block_generation_activation_row(
            "blocked-transition-instance",
            2,
            expected_transition_token="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb6",
        )


class _TransitionTaskCursor:
    def __init__(
        self,
        *,
        current_tasks: Sequence[Mapping[str, Any]],
        journal_tasks: Sequence[Mapping[str, Any]],
    ) -> None:
        self.current_tasks = [dict(item) for item in current_tasks]
        self.journal_tasks = [dict(item) for item in journal_tasks]
        self._result: list[dict[str, Any]] = []
        self.rowcount = 0

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT task.id"):
            self._result = copy.deepcopy(self.current_tasks)
            self.rowcount = len(self._result)
            return
        if normalized.startswith("DELETE FROM scheduled_tasks"):
            self.rowcount = len(self.current_tasks)
            self.current_tasks = []
            self._result = []
            return
        if normalized.startswith(
            "SELECT * FROM automation_project_generation_transition_tasks"
        ):
            self._result = copy.deepcopy(self.journal_tasks)
            self.rowcount = len(self._result)
            return
        if normalized.startswith("INSERT INTO scheduled_tasks"):
            values = tuple(params)
            self.current_tasks.append(
                {
                    "id": values[0],
                    "automation_id": values[1],
                    "automation_generation": values[2],
                    "name": values[3],
                    "tool_name": values[4],
                    "tool_params": json.loads(str(values[5])) if values[5] else None,
                    "cron_expression": values[6],
                    "enabled": values[7],
                    "last_run": values[8],
                    "last_status": values[9],
                    "last_duration_ms": values[10],
                    "last_message": values[11],
                    "configuration_version": values[12],
                    "created_at": values[13],
                    "updated_at": values[14],
                }
            )
            self.rowcount = 1
            return
        if normalized.startswith(
            "INSERT INTO scheduled_task_approval_policies"
        ):
            values = tuple(params)
            task = next(item for item in self.current_tasks if item["id"] == values[0])
            task.update(
                {
                    "policy_task_id": values[0],
                    "policy_mode": values[1],
                    "policy_contract_hash": values[2],
                    "policy_contract_snapshot_json": (
                        json.loads(str(values[3])) if values[3] else None
                    ),
                    "policy_tool_contract_hash": values[4],
                    "policy_approved_by_actor_id": values[5],
                    "policy_approved_by_actor_role": values[6],
                    "policy_approved_by_actor_display_name": values[7],
                    "policy_approved_at": values[8],
                    "policy_comment": values[9],
                    "policy_version": values[10],
                    "policy_updated_at": values[11],
                }
            )
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL in transition task test: {normalized}")

    def fetchall(self) -> list[dict[str, Any]]:
        return self._result


def test_transition_task_before_image_restores_non_default_policy_exactly() -> None:
    observed_at = datetime(2026, 8, 31, 8, 30, tzinfo=timezone.utc)
    before_task = {
        "id": "old-task",
        "automation_id": "task-roundtrip-instance",
        "automation_generation": 1,
        "name": "Administrator supplied name",
        "tool_name": "automation.task-roundtrip-instance.run",
        "tool_params": {"marker": "old"},
        "cron_expression": "0 9 * * *",
        "enabled": 1,
        "last_run": observed_at,
        "last_status": "success",
        "last_duration_ms": 17,
        "last_message": "ok",
        "configuration_version": 3,
        "created_at": observed_at,
        "updated_at": observed_at,
        "policy_task_id": "old-task",
        "policy_mode": "EXACT_SCHEDULE_EXEMPT",
        "policy_contract_hash": "a" * 64,
        "policy_contract_snapshot_json": {"scope": "exact"},
        "policy_tool_contract_hash": "b" * 64,
        "policy_approved_by_actor_id": "admin-1",
        "policy_approved_by_actor_role": "super_admin",
        "policy_approved_by_actor_display_name": "Admin",
        "policy_approved_at": observed_at,
        "policy_comment": "approved",
        "policy_version": 7,
        "policy_updated_at": observed_at,
    }
    journal_task = {
        **before_task,
        "task_id": before_task["id"],
        "task_created_at": before_task["created_at"],
        "task_updated_at": before_task["updated_at"],
    }
    cursor = _TransitionTaskCursor(
        current_tasks=[
            {
                **before_task,
                "id": "new-task",
                "automation_generation": 2,
                "name": "new projection",
                "policy_task_id": "new-task",
                "policy_mode": "REQUIRE_EACH_RUN",
                "policy_contract_hash": None,
                "policy_contract_snapshot_json": None,
                "policy_tool_contract_hash": None,
                "policy_approved_by_actor_id": None,
                "policy_approved_by_actor_role": None,
                "policy_approved_by_actor_display_name": None,
                "policy_approved_at": None,
                "policy_comment": None,
                "policy_version": 1,
            }
        ],
        journal_tasks=[journal_task],
    )

    _restore_transition_task_before_image(
        cursor,
        automation_id="task-roundtrip-instance",
        transition_token="00000000-0000-0000-0000-000000000004",
    )
    restored = _lock_scheduled_task_before_image(
        cursor,
        automation_id="task-roundtrip-instance",
    )

    assert _exact_json_hash(restored) == _exact_json_hash([before_task])


def test_transition_task_empty_before_image_clears_first_install_projection() -> None:
    observed_at = datetime(2026, 8, 31, 8, 30, tzinfo=timezone.utc)
    cursor = _TransitionTaskCursor(
        current_tasks=[
            {
                "id": "initial-task",
                "automation_id": "first-install-instance",
                "automation_generation": 1,
                "name": "initial projection",
                "tool_name": "automation.first-install-instance.run",
                "tool_params": {},
                "cron_expression": "0 9 * * *",
                "enabled": 1,
                "last_run": None,
                "last_status": None,
                "last_duration_ms": None,
                "last_message": None,
                "configuration_version": 1,
                "created_at": observed_at,
                "updated_at": observed_at,
                "policy_task_id": "initial-task",
                "policy_mode": "REQUIRE_EACH_RUN",
                "policy_contract_hash": None,
                "policy_contract_snapshot_json": None,
                "policy_tool_contract_hash": None,
                "policy_approved_by_actor_id": None,
                "policy_approved_by_actor_role": None,
                "policy_approved_by_actor_display_name": None,
                "policy_approved_at": None,
                "policy_comment": None,
                "policy_version": 1,
                "policy_updated_at": observed_at,
            }
        ],
        journal_tasks=[],
    )

    _restore_transition_task_before_image(
        cursor,
        automation_id="first-install-instance",
        transition_token="00000000-0000-0000-0000-000000000005",
    )

    restored = _lock_scheduled_task_before_image(
        cursor,
        automation_id="first-install-instance",
    )
    assert restored == []
    assert _exact_json_hash(restored) == _exact_json_hash([])


def test_activation_transition_migration_closes_phase_and_before_image() -> None:
    sql = Path(
        "agent/migrations/034_runtime_generation_activation_journal.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS automation_project_generation_transitions" in sql
    assert (
        "CREATE TABLE IF NOT EXISTS "
        "automation_project_generation_transition_tasks" in sql
    )
    for phase in ("PENDING_PROJECTION", "ACTIVE", "ROLLED_BACK", "BLOCKED"):
        assert f"'{phase}'" in sql
    for field in (
        "transition_token",
        "base_committed_generation",
        "before_project_record_version",
        "pending_project_record_version",
        "before_tasks_sha256",
        "pending_tasks_sha256",
        "policy_contract_snapshot_json",
        "policy_version",
        "policy_updated_at",
    ):
        assert field in sql


def test_expired_invalid_generation_lease_migration_is_pre_write_only() -> None:
    sql = Path(
        "agent/migrations/035_finalize_expired_invalid_generation_leases.sql"
    ).read_text(encoding="utf-8")

    assert "lease.outcome IN ('RUNNING', 'VERIFYING')" in sql
    assert "lease.expires_at <= UTC_TIMESTAMP(6)" in sql
    assert "run.status = 'FAILED_TERMINAL'" in sql
    assert "run.error_code = 'GENERATION_LEASE_INVALID'" in sql
    assert "NOT EXISTS" in sql
    assert "automation_write_attempt_receipts" in sql
    assert "lease.outcome = 'FAILED_BEFORE_WRITE'" in sql
    assert "WRITE_OUTCOME_UNKNOWN" not in sql


def test_self_pickup_formula_source_locator_migration_is_exact_and_idempotent() -> None:
    sql = Path(
        "agent/migrations/036_self_pickup_formula_source_locator.sql"
    ).read_text(encoding="utf-8")

    assert "phase7.self_pickup_source_sheet" in sql
    assert "F0NVsI5dlhaWugtw14YcmdrQnvh" in sql
    assert "UeBd3I!A1:S5000" in sql
    assert "$.formula_source_sheet_id" in sql
    assert "8fc516!A1:S197" in sql
    assert "@cp036_resource_count = 1" in sql
    assert "@cp036_invalid_resource_count = 0" in sql
    assert "configuration_version = configuration_version + IF(" in sql
    assert "config_sha256 = SHA2(" in sql
