from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pytest

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.generation import (
    AutomationRuntimeReconciler,
    runtime_generation_health,
)
from agent.automation_plugins.models import (
    PluginTrustSource,
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
from agent.automation_plugins.production import ProductionRuntimeCoeffectProvider


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot(generation: int, version: str) -> RuntimeGenerationSnapshot:
    return RuntimeGenerationSnapshot(
        automation_id="project-a",
        generation=generation,
        plugin_id="action-a",
        plugin_version=version,
        package_sha256=_digest(f"package:{version}"),
        manifest_sha256=_digest(f"manifest:{version}"),
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        project_config_sha256=_digest("config"),
        account_bindings_sha256=_digest("accounts"),
        resource_bindings_sha256=_digest("resources"),
        device_binding_sha256=_digest("device"),
        schedule_sha256=_digest("schedule"),
        core_registry_sha256=_digest("registry"),
        tool_contract_sha256=_digest("tool"),
        invocation_contracts_sha256=_digest("invocations"),
        compiled_invocations_sha256=_digest("compiled-invocations"),
        runtime_descriptor_sha256=_digest("runtime-descriptor"),
        governance_anchor_sha256=_digest("governance-anchor"),
        policy_contract_sha256=_digest("policy"),
        enabled_entrypoints=("scheduler",),
        execution_metadata={
            "project_config_version": generation,
            "project_config": {"generation": generation},
            "account_bindings": {"source": f"account-{generation}"},
            "resource_bindings": {},
            "device_binding": None,
            "schedule": {"kind": "daily_times", "times": ["09:00"], "enabled": True},
            "compiled_invocations": {"scheduler": {"arguments": {}, "dynamic_resolvers": {}}},
            "runtime_descriptor": {
                "runtime": {"kind": "python_subprocess", "entrypoint": "payload/main.py"},
                "runtime_permissions": {},
                "account_roles": [],
                "resource_roles": [],
                "install_metadata": {
                    "install_root": f"/plugins/action-a/{version}",
                    "python_relative": "venv/bin/python",
                },
            },
            "action_contract": {"name": "action-a", "version": version},
            "governance_anchor": {"name": "action-a", "version": version},
        },
    )


class _MemoryGenerationRepository:
    def __init__(self) -> None:
        self.runtime: ProjectRuntimeRecord | None = None
        self.generations: dict[int, RuntimeGenerationRecord] = {}
        self.leases: dict[int, list[RuntimeGenerationLease]] = {}
        self.unknown: set[int] = set()
        self.events: list[str] = []
        self.fail_effect_applied_once = False

    def get_project_runtime(self, automation_id: str) -> ProjectRuntimeRecord | None:
        return self.runtime

    def list_project_runtimes(self) -> Sequence[ProjectRuntimeRecord]:
        return (self.runtime,) if self.runtime is not None else ()

    def get_generation(self, automation_id: str, generation: int) -> RuntimeGenerationRecord | None:
        return self.generations.get(generation)

    def list_project_generations(self, automation_id: str) -> Sequence[RuntimeGenerationRecord]:
        return tuple(self.generations.values())

    def allocate_target_generation(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        expected_committed_generation: int | None,
        request_id: str,
    ) -> RuntimeGenerationRecord:
        uuid.UUID(request_id)
        existing = self.generations.get(snapshot.generation)
        if existing:
            assert existing.snapshot == snapshot
            return existing
        assert (self.runtime.committed_generation if self.runtime else None) == expected_committed_generation
        record = RuntimeGenerationRecord(snapshot, RuntimeGenerationState.TARGET)
        self.generations[snapshot.generation] = record
        self.runtime = ProjectRuntimeRecord(
            snapshot.automation_id,
            snapshot.generation,
            expected_committed_generation,
            RuntimeReconcileState.PREPARING,
            (self.runtime.record_version + 1) if self.runtime else 1,
        )
        return record

    def _state(self, generation: int, state: RuntimeGenerationState) -> None:
        self.generations[generation] = replace(self.generations[generation], state=state)

    def mark_generation_preparing(self, automation_id: str, generation: int) -> None:
        self._state(generation, RuntimeGenerationState.PREPARING)

    def replace_generation_coeffects(
        self,
        automation_id: str,
        generation: int,
        coeffects: Sequence[RuntimeCoeffectSnapshot],
    ) -> None:
        self.generations[generation] = replace(
            self.generations[generation], coeffects=tuple(coeffects)
        )

    def mark_generation_waiting_coeffects(
        self,
        automation_id: str,
        generation: int,
        *,
        reason_codes: Sequence[str],
    ) -> None:
        assert reason_codes
        self._state(generation, RuntimeGenerationState.WAITING_COEFFECTS)
        assert self.runtime
        self.runtime = replace(self.runtime, reconcile_state=RuntimeReconcileState.WAITING_COEFFECTS)

    def reserve_generation_effect(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        plan: RuntimeEffectPlan,
        sequence: int,
    ) -> RuntimeEffectRecord:
        existing = [
            effect
            for effect in self.generations[snapshot.generation].effects
            if effect.sequence == sequence
        ]
        if existing:
            return existing[0]
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
        record = self.generations[snapshot.generation]
        self.generations[snapshot.generation] = replace(record, effects=(*record.effects, effect))
        return effect

    def mark_generation_effect_applied(self, effect: RuntimeEffectRecord) -> RuntimeEffectRecord:
        if self.fail_effect_applied_once:
            self.fail_effect_applied_once = False
            raise RuntimeError("simulated crash before applied ACK")
        self._replace_effect_state(effect.effect_id, RuntimeEffectState.APPLIED)
        return effect

    def mark_generation_prepared(self, automation_id: str, generation: int) -> None:
        self._state(generation, RuntimeGenerationState.PREPARED)

    def commit_generation_cas(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_committed_generation: int | None,
    ) -> ProjectRuntimeRecord:
        assert self.runtime and self.runtime.committed_generation == expected_committed_generation
        archival_unknown_predecessor = False
        if expected_committed_generation is not None:
            predecessor = self.generations[expected_committed_generation]
            if predecessor.state is RuntimeGenerationState.BLOCKED:
                assert expected_committed_generation in self.unknown
                archival_unknown_predecessor = True
            else:
                assert predecessor.state is RuntimeGenerationState.COMMITTED
                self._state(
                    expected_committed_generation,
                    RuntimeGenerationState.DRAINING,
                )
        self._state(generation, RuntimeGenerationState.COMMITTED)
        self.runtime = replace(
            self.runtime,
            committed_generation=generation,
            reconcile_state=(
                RuntimeReconcileState.STABLE
                if archival_unknown_predecessor or expected_committed_generation is None
                else RuntimeReconcileState.DRAINING
            ),
            record_version=self.runtime.record_version + 1,
        )
        self.events.append(f"commit:{generation}")
        return self.runtime

    def mark_generation_draining(self, automation_id: str, generation: int) -> None:
        self._state(generation, RuntimeGenerationState.DRAINING)
        self.events.append(f"drain:{generation}")

    def list_active_generation_leases(
        self, automation_id: str, generation: int
    ) -> Sequence[RuntimeGenerationLease]:
        return tuple(self.leases.get(generation, ()))

    def has_unknown_generation_write(self, automation_id: str, generation: int) -> bool:
        return generation in self.unknown

    def reserve_generation_dispose(
        self, automation_id: str, generation: int
    ) -> RuntimeGenerationRecord:
        assert not self.leases.get(generation) and generation not in self.unknown
        self._state(generation, RuntimeGenerationState.DISPOSING)
        return self.generations[generation]

    def mark_generation_effect_disposing(self, effect_id: str) -> None:
        self.events.append(f"effect-disposing:{effect_id}")
        self._replace_effect_state(effect_id, RuntimeEffectState.DISPOSING)

    def mark_generation_effect_disposed(self, effect_id: str) -> None:
        self.events.append(f"effect-disposed:{effect_id}")
        self._replace_effect_state(effect_id, RuntimeEffectState.DISPOSED)

    def _replace_effect_state(self, effect_id: str, state: RuntimeEffectState) -> None:
        for generation, record in self.generations.items():
            updated = tuple(
                replace(effect, state=state) if effect.effect_id == effect_id else effect
                for effect in record.effects
            )
            if updated != record.effects:
                self.generations[generation] = replace(record, effects=updated)
                return

    def complete_generation_dispose(self, automation_id: str, generation: int) -> None:
        self._state(generation, RuntimeGenerationState.DISPOSED)
        self.events.append(f"disposed:{generation}")
        if (
            self.runtime is not None
            and self.runtime.target_generation == generation
            and self.runtime.committed_generation is not None
            and self.runtime.committed_generation != generation
        ):
            self.runtime = replace(
                self.runtime,
                target_generation=self.runtime.committed_generation,
                reconcile_state=RuntimeReconcileState.STABLE,
                record_version=self.runtime.record_version + 1,
            )

    def fail_generation(
        self,
        automation_id: str,
        generation: int,
        *,
        error_code: str,
        error_summary: str,
    ) -> None:
        self._state(generation, RuntimeGenerationState.FAILED)
        if self.runtime is not None:
            old_generation_failed_after_commit = (
                self.runtime.committed_generation is not None
                and generation != self.runtime.committed_generation
                and generation != self.runtime.target_generation
            )
            self.runtime = replace(
                self.runtime,
                reconcile_state=(
                    RuntimeReconcileState.DRAINING
                    if old_generation_failed_after_commit
                    else RuntimeReconcileState.ERROR
                ),
            )

    def block_generation_unknown_write(self, automation_id: str, generation: int) -> None:
        self._state(generation, RuntimeGenerationState.BLOCKED)
        if self.runtime:
            self.runtime = replace(
                self.runtime,
                reconcile_state=RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
            )
            self.stabilize_project_after_archival_unknown(automation_id, generation)

    def stabilize_project_after_archival_unknown(
        self,
        automation_id: str,
        generation: int,
    ) -> None:
        if (
            self.runtime is not None
            and self.runtime.target_generation == self.runtime.committed_generation
            and self.runtime.committed_generation != generation
            and all(
                number == self.runtime.committed_generation
                or record.state is RuntimeGenerationState.DISPOSED
                or (
                    record.state is RuntimeGenerationState.BLOCKED
                    and number in self.unknown
                    and not self.leases.get(number)
                )
                for number, record in self.generations.items()
            )
        ):
            self.runtime = replace(
                self.runtime,
                reconcile_state=RuntimeReconcileState.STABLE,
            )


class _Coeffects:
    def __init__(self, ready: bool = True, revisions: Sequence[str] = ("revision-1",)) -> None:
        self.ready = ready
        self.revisions = list(revisions)
        self.calls = 0

    def observe(self, snapshot: RuntimeGenerationSnapshot) -> Sequence[RuntimeCoeffectSnapshot]:
        revision = self.revisions[min(self.calls, len(self.revisions) - 1)]
        self.calls += 1
        return (
            RuntimeCoeffectSnapshot(
                RuntimeCoeffectKind.CORE_ADAPTER,
                "browser-v1",
                revision,
                self.ready,
                reason_code=None if self.ready else "CORE_ADAPTER_NOT_READY",
            ),
        )


class _Planner:
    def __init__(self, *, reversible: bool = True) -> None:
        self.reversible = reversible

    def plan(self, snapshot: RuntimeGenerationSnapshot) -> Sequence[RuntimeEffectPlan]:
        return (
            RuntimeEffectPlan(
                RuntimeEffectKind.PACKAGE_REFERENCE,
                f"package:{snapshot.plugin_version}",
                {"version": snapshot.plugin_version},
                self.reversible,
            ),
            RuntimeEffectPlan(
                RuntimeEffectKind.BROKER_SCOPE,
                f"broker:{snapshot.generation}",
                {},
                self.reversible,
            ),
        )


class _Driver:
    def __init__(self) -> None:
        self.applied: list[str] = []
        self.disposed: list[str] = []

    def ensure_applied(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        plan: RuntimeEffectPlan,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        self.applied.append(plan.effect_key)
        return replace(effect, state=RuntimeEffectState.APPLIED)

    def dispose(self, effect: RuntimeEffectRecord) -> None:
        self.disposed.append(effect.effect_key)


class _IdempotentDriver(_Driver):
    def __init__(self) -> None:
        super().__init__()
        self.ensure_calls: list[str] = []
        self.materialized: set[str] = set()

    def ensure_applied(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        plan: RuntimeEffectPlan,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        del snapshot
        self.ensure_calls.append(plan.effect_key)
        if plan.effect_key not in self.materialized:
            self.materialized.add(plan.effect_key)
            self.applied.append(plan.effect_key)
        return replace(effect, state=RuntimeEffectState.APPLIED)


class _FailingDisposeDriver(_Driver):
    def dispose(self, effect: RuntimeEffectRecord) -> None:
        raise RuntimeError(f"cannot dispose {effect.effect_key}")


def _reconciler(
    repository: _MemoryGenerationRepository,
    *,
    coeffects: _Coeffects | None = None,
    planner: _Planner | None = None,
    driver: _Driver | None = None,
) -> tuple[AutomationRuntimeReconciler, _Driver]:
    driver = driver or _Driver()
    return (
        AutomationRuntimeReconciler(
            repository=repository,
            coeffects=coeffects or _Coeffects(),
            planner=planner or _Planner(),
            driver=driver,
        ),
        driver,
    )


def test_upgrade_commits_new_generation_then_drains_old_lease() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, driver = _reconciler(repository)
    first = reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    assert first.committed_generation == 1
    lease = RuntimeGenerationLease(
        str(uuid.uuid4()),
        "project-a",
        1,
        _snapshot(1, "1.0.0"),
        {},
        datetime.now(timezone.utc),
        datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    repository.leases[1] = [lease]

    upgraded = reconciler.reconcile(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )

    assert upgraded.committed_generation == 2
    assert upgraded.draining_generations == (1,)
    assert upgraded.disposed_generations == ()
    assert repository.generations[1].state == RuntimeGenerationState.DRAINING
    assert repository.events.index("commit:2") < repository.events.index("drain:1")

    repository.leases[1] = []
    assert reconciler.dispose_generation(repository.generations[1]) is True
    assert driver.disposed[-2:] == ["broker:1", "package:1.0.0"]


def test_postcommit_old_dispose_failure_keeps_committed_b_routable() -> None:
    repository = _MemoryGenerationRepository()
    driver = _FailingDisposeDriver()
    reconciler, _ = _reconciler(repository, driver=driver)
    reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )

    with pytest.raises(RuntimeError, match="cannot dispose"):
        reconciler.reconcile(
            _snapshot(2, "2.0.0"),
            expected_committed_generation=1,
            request_id=str(uuid.uuid4()),
        )

    assert repository.runtime and repository.runtime.target_generation == 2
    assert repository.runtime.committed_generation == 2
    assert repository.runtime.reconcile_state == RuntimeReconcileState.DRAINING
    assert repository.generations[2].state == RuntimeGenerationState.COMMITTED
    assert repository.generations[1].state == RuntimeGenerationState.FAILED


def test_unready_coeffect_never_applies_or_commits() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, driver = _reconciler(repository, coeffects=_Coeffects(False))
    result = reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    assert result.waiting_coeffects == ("CORE_ADAPTER_NOT_READY",)
    assert result.committed_generation is None
    assert driver.applied == []


def test_unauthenticated_session_does_not_block_structural_generation_commit() -> None:
    snapshot = _snapshot(1, "1.0.0")
    anchor = {"name": "action-a", "version": "1.0.0"}
    metadata = dict(snapshot.execution_metadata)
    metadata.update(
        {
            "account_bindings": {"source": "acct-1"},
            "resource_bindings": {},
            "governance_anchor": anchor,
            "runtime_descriptor": {
                **dict(metadata["runtime_descriptor"]),
                "runtime_permissions": {
                    "broker_operations": [
                        {
                            "operation": "browser.invoke",
                            "action": "fetch",
                            "roles": ["source"],
                            "effect": "read",
                        }
                    ]
                },
                "account_roles": [
                    {
                        "role": "source",
                        "allowed_systems": ["ronghui"],
                        "required": True,
                    }
                ],
                "resource_roles": [],
            },
        }
    )
    snapshot = replace(snapshot, execution_metadata=metadata)

    class _CoreCatalog:
        @staticmethod
        def get_capability(name: str) -> Mapping[str, str] | None:
            return anchor if name == "action-a" else None

    class _UnauthenticatedAccountManager:
        def __init__(self) -> None:
            self.session_checks = 0

        @staticmethod
        def list_accounts(**_: object) -> list[dict[str, object]]:
            return [
                {
                    "account_id": "acct-1",
                    "system": "ronghui",
                    "is_active": True,
                }
            ]

        def require_authenticated_binding(self, _account_id: str) -> Mapping[str, str]:
            self.session_checks += 1
            raise RuntimeError("not authenticated")

    account_manager = _UnauthenticatedAccountManager()
    repository = _MemoryGenerationRepository()
    reconciler, driver = _reconciler(
        repository,
        coeffects=ProductionRuntimeCoeffectProvider(
            core_catalog=_CoreCatalog(),
            broker_handler_keys=(("browser.invoke", "fetch"),),
            account_manager=account_manager,
        ),
    )

    result = reconciler.reconcile(
        snapshot,
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    health = runtime_generation_health(
        repository,
        expected_automation_ids={snapshot.automation_id},
    )

    assert result.waiting_coeffects == ()
    assert result.committed_generation == 1
    assert repository.runtime
    assert repository.runtime.reconcile_state == RuntimeReconcileState.STABLE
    assert repository.generations[1].state == RuntimeGenerationState.COMMITTED
    assert driver.applied == ["package:1.0.0", "broker:1"]
    assert account_manager.session_checks == 0
    assert health.healthy is True


def test_upgrade_waiting_dependency_keeps_committed_a_and_never_routes_b() -> None:
    repository = _MemoryGenerationRepository()
    healthy_reconciler, _ = _reconciler(repository)
    healthy_reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    waiting_reconciler, driver = _reconciler(
        repository,
        coeffects=_Coeffects(False),
    )

    result = waiting_reconciler.reconcile(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )

    assert result.committed_generation == 1
    assert result.waiting_coeffects == ("CORE_ADAPTER_NOT_READY",)
    assert repository.runtime and repository.runtime.target_generation == 2
    assert repository.runtime.committed_generation == 1
    assert repository.runtime.reconcile_state == RuntimeReconcileState.WAITING_COEFFECTS
    assert repository.generations[1].state == RuntimeGenerationState.COMMITTED
    assert repository.generations[2].state == RuntimeGenerationState.WAITING_COEFFECTS
    assert driver.applied == []


def test_archival_unknown_write_does_not_block_prepared_successor() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, _ = _reconciler(repository)
    reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    repository.unknown.add(1)
    repository.block_generation_unknown_write("project-a", 1)
    result = reconciler.reconcile(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    assert result.committed_generation == 2
    assert repository.generations[1].state == RuntimeGenerationState.BLOCKED
    assert repository.generations[2].state == RuntimeGenerationState.COMMITTED
    assert repository.runtime and repository.runtime.reconcile_state == RuntimeReconcileState.STABLE
    assert result.draining_generations == ()
    assert result.disposed_generations == ()


def test_non_reversible_effect_is_rejected_before_apply() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, driver = _reconciler(repository, planner=_Planner(reversible=False))
    with pytest.raises(PluginConflictError, match="non-reversible"):
        reconciler.reconcile(
            _snapshot(1, "1.0.0"),
            expected_committed_generation=None,
            request_id=str(uuid.uuid4()),
        )
    assert driver.applied == []
    assert repository.generations[1].state == RuntimeGenerationState.FAILED


def test_precommit_terminal_failure_without_effects_aborts_to_committed() -> None:
    repository = _MemoryGenerationRepository()
    healthy_reconciler, _ = _reconciler(repository)
    healthy_reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    old_lease = RuntimeGenerationLease(
        str(uuid.uuid4()),
        "project-a",
        1,
        _snapshot(1, "1.0.0"),
        {},
        datetime.now(timezone.utc),
        datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    repository.leases[1] = [old_lease]
    failing_reconciler, driver = _reconciler(
        repository,
        planner=_Planner(reversible=False),
    )

    with pytest.raises(PluginConflictError, match="non-reversible"):
        failing_reconciler.reconcile(
            _snapshot(2, "2.0.0"),
            expected_committed_generation=1,
            request_id=str(uuid.uuid4()),
        )

    assert repository.generations[1].state == RuntimeGenerationState.COMMITTED
    assert repository.generations[2].state == RuntimeGenerationState.DISPOSED
    assert repository.runtime == ProjectRuntimeRecord(
        automation_id="project-a",
        target_generation=1,
        committed_generation=1,
        reconcile_state=RuntimeReconcileState.STABLE,
        record_version=4,
    )
    assert repository.leases[1] == [old_lease]
    assert driver.applied == []


def test_precommit_failure_with_applied_effect_does_not_auto_compensate() -> None:
    repository = _MemoryGenerationRepository()
    healthy_reconciler, _ = _reconciler(repository)
    healthy_reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    snapshot_v2 = _snapshot(2, "2.0.0")
    repository.allocate_target_generation(
        snapshot_v2,
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    repository.mark_generation_preparing("project-a", 2)
    repository.replace_generation_coeffects(
        "project-a",
        2,
        _Coeffects().observe(snapshot_v2),
    )
    plan = _Planner().plan(snapshot_v2)[0]
    planned = repository.reserve_generation_effect(snapshot_v2, plan=plan, sequence=1)
    repository.mark_generation_effect_applied(
        replace(planned, state=RuntimeEffectState.APPLIED)
    )
    failing_reconciler, driver = _reconciler(
        repository,
        planner=_Planner(reversible=False),
    )

    with pytest.raises(PluginConflictError, match="non-reversible"):
        failing_reconciler.reconcile(
            snapshot_v2,
            expected_committed_generation=1,
            request_id=str(uuid.uuid4()),
        )

    assert repository.generations[1].state == RuntimeGenerationState.COMMITTED
    assert repository.generations[2].state == RuntimeGenerationState.FAILED
    assert repository.generations[2].effects[0].state == RuntimeEffectState.APPLIED
    assert repository.runtime and repository.runtime.target_generation == 2
    assert repository.runtime.committed_generation == 1
    assert repository.runtime.reconcile_state == RuntimeReconcileState.ERROR
    assert driver.disposed == []


def test_precommit_failure_with_unknown_write_does_not_auto_dispose() -> None:
    repository = _MemoryGenerationRepository()
    healthy_reconciler, _ = _reconciler(repository)
    healthy_reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    snapshot_v2 = _snapshot(2, "2.0.0")
    repository.unknown.add(2)
    failing_reconciler, _ = _reconciler(
        repository,
        planner=_Planner(reversible=False),
    )

    with pytest.raises(PluginConflictError, match="non-reversible"):
        failing_reconciler.reconcile(
            snapshot_v2,
            expected_committed_generation=1,
            request_id=str(uuid.uuid4()),
        )

    assert repository.generations[1].state == RuntimeGenerationState.COMMITTED
    assert repository.generations[2].state == RuntimeGenerationState.FAILED
    assert repository.runtime and repository.runtime.target_generation == 2
    assert repository.runtime.committed_generation == 1
    assert repository.runtime.reconcile_state == RuntimeReconcileState.ERROR


def test_generation_snapshot_never_contains_runtime_credentials() -> None:
    snapshot = _snapshot(1, "1.0.0")
    serialized: Mapping[str, Any] = snapshot.__dict__
    assert not any(
        token in key.lower()
        for key in serialized
        for token in ("password", "cookie", "credential", "session", "token")
    )
    assert RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN.value == "WRITE_OUTCOME_UNKNOWN"


def test_startup_reconcile_recovers_commit_before_old_drain() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, driver = _reconciler(repository)
    reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    target = repository.allocate_target_generation(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    assert reconciler.prepare_target(target) == ()
    repository.commit_generation_cas("project-a", 2, expected_committed_generation=1)

    recovered = reconciler.reconcile_incomplete()

    assert len(recovered) == 1
    assert recovered[0].disposed_generations == (1,)
    assert repository.generations[1].state == RuntimeGenerationState.DISPOSED
    assert driver.disposed[-2:] == ["broker:1", "package:1.0.0"]


def test_startup_reconcile_resumes_partially_applied_prepare_without_duplication() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, driver = _reconciler(repository)
    snapshot = _snapshot(1, "1.0.0")
    repository.allocate_target_generation(
        snapshot,
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    first_plan = _Planner().plan(snapshot)[0]
    planned = repository.reserve_generation_effect(snapshot, plan=first_plan, sequence=1)
    first = driver.ensure_applied(snapshot=snapshot, plan=first_plan, effect=planned)
    repository.mark_generation_effect_applied(first)
    repository.mark_generation_preparing("project-a", 1)

    recovered = reconciler.reconcile_incomplete()

    assert recovered[0].committed_generation == 1
    assert driver.applied.count("package:1.0.0") == 1
    assert driver.applied.count("broker:1") == 1


def test_startup_reconcile_retries_effect_dispose_after_crash() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, driver = _reconciler(repository)
    reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    target = repository.allocate_target_generation(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    reconciler.prepare_target(target)
    repository.commit_generation_cas("project-a", 2, expected_committed_generation=1)
    repository.mark_generation_draining("project-a", 1)
    repository.reserve_generation_dispose("project-a", 1)
    # Recreate a crash journal where the first reverse effect entered
    # DISPOSING but its durable dispose ACK was not saved.
    old = repository.generations[1]
    last_effect = max(old.effects, key=lambda item: item.sequence)
    repository._replace_effect_state(last_effect.effect_id, RuntimeEffectState.DISPOSING)

    recovered = reconciler.reconcile_incomplete()

    assert recovered[0].disposed_generations == (1,)
    assert driver.disposed[-2:] == ["broker:1", "package:1.0.0"]


def test_startup_resume_preserves_archival_unknown_generation() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, _ = _reconciler(repository)
    reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    repository.unknown.add(1)
    repository.block_generation_unknown_write("project-a", 1)
    target = repository.allocate_target_generation(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    assert reconciler.prepare_target(target) == ()

    recovered = reconciler.reconcile_incomplete()

    assert len(recovered) == 1
    assert recovered[0].committed_generation == 2
    assert repository.runtime and repository.runtime.reconcile_state == RuntimeReconcileState.STABLE
    assert repository.generations[1].state is RuntimeGenerationState.BLOCKED
    assert repository.generations[2].state is RuntimeGenerationState.COMMITTED
    assert reconciler.reconcile_incomplete() == ()


def test_startup_resume_closes_legacy_archival_drain_state() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, _ = _reconciler(repository)
    reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    repository.unknown.add(1)
    target = repository.allocate_target_generation(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    assert reconciler.prepare_target(target) == ()
    repository.commit_generation_cas("project-a", 2, expected_committed_generation=1)
    repository._state(1, RuntimeGenerationState.BLOCKED)
    assert repository.runtime
    repository.runtime = replace(
        repository.runtime,
        reconcile_state=RuntimeReconcileState.DRAINING,
    )

    recovered = reconciler.reconcile_incomplete()

    assert len(recovered) == 1
    assert repository.runtime.reconcile_state is RuntimeReconcileState.STABLE
    assert reconciler.reconcile_incomplete() == ()


def test_release_health_allows_archival_unknown_but_rejects_current_unknown() -> None:
    repository = _MemoryGenerationRepository()
    reconciler, _ = _reconciler(repository)
    reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )
    healthy = runtime_generation_health(repository, expected_automation_ids={"project-a"})
    assert healthy.healthy is True
    assert healthy.project_count == 1
    assert healthy.committed_count == 1
    followup_release = runtime_generation_health(repository, expected_automation_ids=set())
    assert followup_release.healthy is True
    assert followup_release.project_count == 1

    repository.unknown.add(1)
    repository.block_generation_unknown_write("project-a", 1)
    blocked = runtime_generation_health(repository, expected_automation_ids={"project-a"})
    assert blocked.healthy is False
    assert "WRITE_OUTCOME_UNKNOWN" in blocked.blocked_projects["project-a"]
    with pytest.raises(PluginConflictError, match="not release-ready"):
        blocked.assert_release_ready()

    deferred = runtime_generation_health(
        repository,
        expected_automation_ids=set(),
        ignored_automation_ids={"project-a"},
    )
    assert deferred.healthy is True
    assert deferred.project_count == 0
    assert deferred.committed_count == 0

    target = repository.allocate_target_generation(
        _snapshot(2, "2.0.0"),
        expected_committed_generation=1,
        request_id=str(uuid.uuid4()),
    )
    assert reconciler.prepare_target(target) == ()
    reconciler.resume_project("project-a")
    archival = runtime_generation_health(
        repository,
        expected_automation_ids={"project-a"},
    )
    assert archival.healthy is True
    assert archival.archival_unknown_generation_count == 1


def test_coeffect_revision_change_between_prepare_and_commit_never_switches() -> None:
    repository = _MemoryGenerationRepository()
    coeffects = _Coeffects(revisions=("account-rev-1", "account-rev-2"))
    reconciler, driver = _reconciler(repository, coeffects=coeffects)

    result = reconciler.reconcile(
        _snapshot(1, "1.0.0"),
        expected_committed_generation=None,
        request_id=str(uuid.uuid4()),
    )

    assert result.committed_generation is None
    assert result.waiting_coeffects == ("COEFFECT_REVISION_CHANGED",)
    assert repository.runtime and repository.runtime.committed_generation is None
    assert repository.generations[1].state == RuntimeGenerationState.WAITING_COEFFECTS
    assert driver.applied == ["package:1.0.0", "broker:1"]


def test_crash_after_effect_apply_before_ack_reuses_durable_planned_owner() -> None:
    repository = _MemoryGenerationRepository()
    repository.fail_effect_applied_once = True
    driver = _IdempotentDriver()
    reconciler, _ = _reconciler(repository, driver=driver)
    snapshot = _snapshot(1, "1.0.0")

    with pytest.raises(RuntimeError, match="simulated crash"):
        reconciler.reconcile(
            snapshot,
            expected_committed_generation=None,
            request_id=str(uuid.uuid4()),
        )
    journal = repository.generations[1].effects
    assert len(journal) == 1
    assert journal[0].state == RuntimeEffectState.PLANNED
    assert driver.materialized == {"package:1.0.0"}

    recovered = reconciler.reconcile_incomplete()

    assert recovered[0].committed_generation == 1
    assert driver.ensure_calls.count("package:1.0.0") == 2
    assert driver.applied.count("package:1.0.0") == 1
    assert repository.generations[1].state == RuntimeGenerationState.COMMITTED
    assert all(
        effect.state == RuntimeEffectState.APPLIED
        for effect in repository.generations[1].effects
    )
    assert repository.runtime and repository.runtime.reconcile_state == RuntimeReconcileState.STABLE
