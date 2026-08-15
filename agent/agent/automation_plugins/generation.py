"""Target/committed runtime generations with reversible effect reconciliation.

The reconciler never imports plugin code into the Agent process.  It prepares
an immutable target generation, observes core-owned coeffects, applies only
reversible platform effects, atomically switches the instance route, drains
old leases, and disposes old effects in reverse order.  Business writes are
execution outcomes, not reversible effects; an unknown write blocks disposal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Mapping, Sequence

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.models import (
    RuntimeEffectKind,
    RuntimeEffectRecord,
    RuntimeEffectState,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
    RuntimeReconcileState,
)
from agent.automation_plugins.ports import (
    RuntimeCoeffectProviderPort,
    RuntimeEffectDriverPort,
    RuntimeEffectPlan,
    RuntimeEffectPlannerPort,
    RuntimeGenerationRepositoryPort,
)
from shared.redaction import redact_text


@dataclass(frozen=True)
class RuntimeReconcileResult:
    automation_id: str
    target_generation: int
    committed_generation: int | None
    waiting_coeffects: tuple[str, ...] = ()
    draining_generations: tuple[int, ...] = ()
    disposed_generations: tuple[int, ...] = ()


@dataclass(frozen=True)
class RuntimeGenerationHealth:
    healthy: bool
    project_count: int
    committed_count: int
    active_lease_count: int
    blocked_projects: Mapping[str, tuple[str, ...]]

    def assert_release_ready(self) -> None:
        if not self.healthy:
            raise PluginConflictError(
                "automation runtime generations are not release-ready",
                code="PLUGIN_RUNTIME_GENERATIONS_NOT_READY",
            )


class AutomationRuntimeReconciler:
    """Crash-recoverable hot replacement for instance-bound plugin actions."""

    def __init__(
        self,
        *,
        repository: RuntimeGenerationRepositoryPort,
        coeffects: RuntimeCoeffectProviderPort,
        planner: RuntimeEffectPlannerPort,
        driver: RuntimeEffectDriverPort,
    ) -> None:
        self._repository = repository
        self._coeffects = coeffects
        self._planner = planner
        self._driver = driver

    @staticmethod
    def _validate_effect_plans(plans: Sequence[RuntimeEffectPlan]) -> None:
        identities: set[tuple[RuntimeEffectKind, str]] = set()
        for plan in plans:
            if not isinstance(plan, RuntimeEffectPlan):
                raise PluginConflictError("runtime effect planner returned an invalid plan")
            if not plan.effect_key or len(plan.effect_key) > 240:
                raise PluginConflictError("runtime effect key is invalid")
            if plan.reversible is not True:
                raise PluginConflictError(
                    "non-reversible work cannot be prepared as a runtime effect",
                    code="RUNTIME_EFFECT_NOT_REVERSIBLE",
                )
            if plan.kind == RuntimeEffectKind.ENTRYPOINT_ROUTE:
                raise PluginConflictError(
                    "entrypoint route is committed only by the repository CAS",
                    code="RUNTIME_ROUTE_EFFECT_RESERVED",
                )
            identity = (plan.kind, plan.effect_key)
            if identity in identities:
                raise PluginConflictError("runtime effect planner returned a duplicate effect")
            identities.add(identity)

    @staticmethod
    def _validate_effect(
        effect: RuntimeEffectRecord,
        *,
        snapshot: RuntimeGenerationSnapshot,
        plan: RuntimeEffectPlan,
        sequence: int,
        expected_state: RuntimeEffectState,
    ) -> None:
        if (
            effect.automation_id != snapshot.automation_id
            or effect.generation != snapshot.generation
            or effect.sequence != sequence
            or effect.kind != plan.kind
            or effect.effect_key != plan.effect_key
            or effect.reversible is not True
            or effect.state != expected_state
        ):
            raise PluginConflictError(
                "runtime effect driver returned a mismatched effect",
                code="RUNTIME_EFFECT_MISMATCH",
            )

    def _compensate(self, effects: Sequence[RuntimeEffectRecord]) -> None:
        for effect in sorted(effects, key=lambda item: item.sequence, reverse=True):
            if (
                effect.state not in {RuntimeEffectState.APPLIED, RuntimeEffectState.DISPOSING}
                or effect.reversible is not True
            ):
                continue
            if effect.state == RuntimeEffectState.APPLIED:
                self._repository.mark_generation_effect_disposing(effect.effect_id)
            self._driver.dispose(effect)
            self._repository.mark_generation_effect_disposed(effect.effect_id)

    def _fail_terminal_prepare(
        self,
        target: RuntimeGenerationRecord,
        error: Exception,
    ) -> None:
        """Fail a terminal target and safely abort only an empty pre-commit journal."""

        snapshot = target.snapshot
        self._repository.fail_generation(
            snapshot.automation_id,
            snapshot.generation,
            error_code=getattr(error, "code", type(error).__name__.upper())[:64],
            error_summary=redact_text(error)[:500],
        )
        failed = self._repository.get_generation(
            snapshot.automation_id,
            snapshot.generation,
        )
        project = self._repository.get_project_runtime(snapshot.automation_id)
        if (
            failed is None
            or failed.state != RuntimeGenerationState.FAILED
            or failed.effects
            or project is None
            or project.target_generation != snapshot.generation
            or project.committed_generation is None
            or project.committed_generation == snapshot.generation
            or self._repository.has_unknown_generation_write(
                snapshot.automation_id,
                snapshot.generation,
            )
            or self._repository.list_active_generation_leases(
                snapshot.automation_id,
                snapshot.generation,
            )
        ):
            return
        try:
            self.dispose_generation(failed)
        except Exception as abort_error:  # noqa: BLE001 - preserve terminal cause
            error.add_note(
                "automatic empty-target abort failed: "
                f"{redact_text(abort_error)[:300]}"
            )

    def _revalidate_coeffects(
        self,
        target: RuntimeGenerationRecord,
    ) -> tuple[str, ...]:
        """Close the prepare/commit race over reactive core dependencies."""

        snapshot = target.snapshot
        latest = tuple(self._coeffects.observe(snapshot))
        previous = {
            (item.kind, item.key): (item.revision, item.ready)
            for item in target.coeffects
        }
        current = {
            (item.kind, item.key): (item.revision, item.ready)
            for item in latest
        }
        reasons = {
            item.reason_code or f"{item.kind.value}:{item.key}"
            for item in latest
            if not item.ready
        }
        if current != previous:
            reasons.add("COEFFECT_REVISION_CHANGED")
        if reasons:
            self._repository.replace_generation_coeffects(
                snapshot.automation_id,
                snapshot.generation,
                latest,
            )
            normalized = tuple(sorted(reasons))
            self._repository.mark_generation_waiting_coeffects(
                snapshot.automation_id,
                snapshot.generation,
                reason_codes=normalized,
            )
            return normalized
        return ()

    def prepare_target(self, target: RuntimeGenerationRecord) -> tuple[str, ...]:
        snapshot = target.snapshot
        if target.state == RuntimeGenerationState.PREPARED:
            return ()
        if target.state not in {
            RuntimeGenerationState.TARGET,
            RuntimeGenerationState.PREPARING,
            RuntimeGenerationState.WAITING_COEFFECTS,
        }:
            raise PluginConflictError("runtime target is not preparable")
        try:
            return self._prepare_target(target)
        except PluginConflictError as exc:
            self._fail_terminal_prepare(target, exc)
            raise

    def _prepare_target(self, target: RuntimeGenerationRecord) -> tuple[str, ...]:
        snapshot = target.snapshot
        self._repository.mark_generation_preparing(snapshot.automation_id, snapshot.generation)
        observed = tuple(self._coeffects.observe(snapshot))
        if not observed:
            raise PluginConflictError(
                "runtime generation has no observed coeffects",
                code="RUNTIME_COEFFECTS_MISSING",
            )
        identities = {(item.kind, item.key) for item in observed}
        if len(identities) != len(observed):
            raise PluginConflictError("runtime coeffect snapshot contains duplicate identities")
        self._repository.replace_generation_coeffects(
            snapshot.automation_id,
            snapshot.generation,
            observed,
        )
        unavailable = tuple(
            sorted({item.reason_code or f"{item.kind.value}:{item.key}" for item in observed if not item.ready})
        )
        if unavailable:
            self._repository.mark_generation_waiting_coeffects(
                snapshot.automation_id,
                snapshot.generation,
                reason_codes=unavailable,
            )
            return unavailable

        plans = tuple(self._planner.plan(snapshot))
        self._validate_effect_plans(plans)
        current = self._repository.get_generation(snapshot.automation_id, snapshot.generation)
        if current is None:
            raise PluginConflictError("runtime target disappeared during preparation")
        by_sequence: dict[int, RuntimeEffectRecord] = {}
        for effect in current.effects:
            if effect.sequence in by_sequence:
                raise PluginConflictError("runtime effect journal has duplicate sequence numbers")
            by_sequence[effect.sequence] = effect
        if set(by_sequence) - set(range(1, len(plans) + 1)):
            raise PluginConflictError("persisted runtime effects exceed the deterministic plan")
        for sequence, plan in enumerate(plans, start=1):
            effect = by_sequence.get(sequence)
            if effect is None:
                effect = self._repository.reserve_generation_effect(
                    snapshot,
                    plan=plan,
                    sequence=sequence,
                )
                self._validate_effect(
                    effect,
                    snapshot=snapshot,
                    plan=plan,
                    sequence=sequence,
                    expected_state=RuntimeEffectState.PLANNED,
                )
            elif effect.state not in {RuntimeEffectState.PLANNED, RuntimeEffectState.APPLIED}:
                raise PluginConflictError("runtime effect journal is not preparable")
            else:
                self._validate_effect(
                    effect,
                    snapshot=snapshot,
                    plan=plan,
                    sequence=sequence,
                    expected_state=effect.state,
                )
            if effect.state == RuntimeEffectState.PLANNED:
                applied = self._driver.ensure_applied(
                    snapshot=snapshot,
                    plan=plan,
                    effect=effect,
                )
                self._validate_effect(
                    applied,
                    snapshot=snapshot,
                    plan=plan,
                    sequence=sequence,
                    expected_state=RuntimeEffectState.APPLIED,
                )
                # If this ACK fails, the durable PLANNED owner remains. Startup
                # reconciliation calls ensure_applied with the same key and can
                # inspect/reuse the exact infrastructure effect.
                self._repository.mark_generation_effect_applied(applied)
        self._repository.mark_generation_prepared(snapshot.automation_id, snapshot.generation)
        return ()

    def dispose_generation(self, generation: RuntimeGenerationRecord) -> bool:
        snapshot = generation.snapshot
        if generation.state == RuntimeGenerationState.DISPOSED:
            return True
        leases = tuple(
            self._repository.list_active_generation_leases(
                snapshot.automation_id,
                snapshot.generation,
            )
        )
        if leases:
            self._repository.mark_generation_draining(snapshot.automation_id, snapshot.generation)
            return False
        if self._repository.has_unknown_generation_write(
            snapshot.automation_id,
            snapshot.generation,
        ):
            self._repository.block_generation_unknown_write(
                snapshot.automation_id,
                snapshot.generation,
            )
            return False
        reserved = self._repository.reserve_generation_dispose(
            snapshot.automation_id,
            snapshot.generation,
        )
        effects = sorted(reserved.effects, key=lambda item: item.sequence, reverse=True)
        if any(effect.reversible is not True for effect in effects):
            self._repository.block_generation_unknown_write(
                snapshot.automation_id,
                snapshot.generation,
            )
            raise PluginConflictError(
                "non-reversible work was persisted as a runtime effect",
                code="RUNTIME_EFFECT_NOT_REVERSIBLE",
            )
        try:
            self._compensate(effects)
            self._repository.complete_generation_dispose(
                snapshot.automation_id,
                snapshot.generation,
            )
            return True
        except Exception as exc:
            self._repository.fail_generation(
                snapshot.automation_id,
                snapshot.generation,
                error_code=getattr(exc, "code", type(exc).__name__.upper())[:64],
                error_summary=redact_text(exc)[:500],
            )
            raise

    def reconcile(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        expected_committed_generation: int | None,
        request_id: str,
    ) -> RuntimeReconcileResult:
        target = self._repository.allocate_target_generation(
            snapshot,
            expected_committed_generation=expected_committed_generation,
            request_id=request_id,
        )
        unavailable = self.prepare_target(target)
        if unavailable:
            return RuntimeReconcileResult(
                automation_id=snapshot.automation_id,
                target_generation=snapshot.generation,
                committed_generation=expected_committed_generation,
                waiting_coeffects=unavailable,
            )
        prepared = self._repository.get_generation(snapshot.automation_id, snapshot.generation)
        if prepared is None or prepared.state != RuntimeGenerationState.PREPARED:
            raise PluginConflictError("runtime target was not durably prepared")
        unavailable = self._revalidate_coeffects(prepared)
        if unavailable:
            return RuntimeReconcileResult(
                automation_id=snapshot.automation_id,
                target_generation=snapshot.generation,
                committed_generation=expected_committed_generation,
                waiting_coeffects=unavailable,
            )
        project = self._repository.commit_generation_cas(
            snapshot.automation_id,
            snapshot.generation,
            expected_committed_generation=expected_committed_generation,
        )
        draining: list[int] = []
        disposed: list[int] = []
        if (
            expected_committed_generation is not None
            and expected_committed_generation != snapshot.generation
        ):
            draining.append(expected_committed_generation)
            self._repository.mark_generation_draining(
                snapshot.automation_id,
                expected_committed_generation,
            )
            old = self._repository.get_generation(
                snapshot.automation_id,
                expected_committed_generation,
            )
            if old is None:
                raise PluginConflictError("previous committed generation disappeared")
            if self.dispose_generation(old):
                disposed.append(expected_committed_generation)
        return RuntimeReconcileResult(
            automation_id=snapshot.automation_id,
            target_generation=snapshot.generation,
            committed_generation=project.committed_generation,
            draining_generations=tuple(draining),
            disposed_generations=tuple(disposed),
        )

    def resume_project(self, automation_id: str) -> RuntimeReconcileResult:
        """Resume a journaled prepare/switch/drain after process interruption."""

        project = self._repository.get_project_runtime(automation_id)
        if project is None:
            raise PluginConflictError("project runtime does not exist")
        target = self._repository.get_generation(automation_id, project.target_generation)
        if target is None:
            raise PluginConflictError("project target generation does not exist")
        waiting: tuple[str, ...] = ()
        if target.state in {
            RuntimeGenerationState.TARGET,
            RuntimeGenerationState.PREPARING,
            RuntimeGenerationState.WAITING_COEFFECTS,
        }:
            waiting = self.prepare_target(target)
            target = self._repository.get_generation(automation_id, project.target_generation)
            if target is None:
                raise PluginConflictError("project target disappeared after preparation")
        if waiting:
            return RuntimeReconcileResult(
                automation_id=automation_id,
                target_generation=project.target_generation,
                committed_generation=project.committed_generation,
                waiting_coeffects=waiting,
            )
        if target.state == RuntimeGenerationState.PREPARED:
            unavailable = self._revalidate_coeffects(target)
            if unavailable:
                return RuntimeReconcileResult(
                    automation_id=automation_id,
                    target_generation=project.target_generation,
                    committed_generation=project.committed_generation,
                    waiting_coeffects=unavailable,
                )
            project = self._repository.commit_generation_cas(
                automation_id,
                target.snapshot.generation,
                expected_committed_generation=project.committed_generation,
            )
        elif (
            target.state == RuntimeGenerationState.COMMITTED
            and project.committed_generation != target.snapshot.generation
        ):
            raise PluginConflictError(
                "generation commit journal and project route disagree",
                code="RUNTIME_COMMIT_INCONSISTENT",
            )
        elif target.state not in {RuntimeGenerationState.COMMITTED, RuntimeGenerationState.DISPOSED}:
            raise PluginConflictError("project target cannot be resumed automatically")

        draining: list[int] = []
        disposed: list[int] = []
        for generation in sorted(
            self._repository.list_project_generations(automation_id),
            key=lambda item: item.snapshot.generation,
        ):
            number = generation.snapshot.generation
            if number == project.committed_generation or generation.state == RuntimeGenerationState.DISPOSED:
                continue
            if generation.state in {
                RuntimeGenerationState.COMMITTED,
                RuntimeGenerationState.DRAINING,
                RuntimeGenerationState.DISPOSING,
            }:
                draining.append(number)
                if generation.state == RuntimeGenerationState.COMMITTED:
                    self._repository.mark_generation_draining(automation_id, number)
                    generation = self._repository.get_generation(automation_id, number) or generation
                if self.dispose_generation(generation):
                    disposed.append(number)
        return RuntimeReconcileResult(
            automation_id=automation_id,
            target_generation=project.target_generation,
            committed_generation=project.committed_generation,
            draining_generations=tuple(draining),
            disposed_generations=tuple(disposed),
        )

    def reconcile_incomplete(self) -> tuple[RuntimeReconcileResult, ...]:
        """Startup scan for every journal that may need deterministic recovery."""

        results: list[RuntimeReconcileResult] = []
        for project in sorted(
            self._repository.list_project_runtimes(),
            key=lambda item: item.automation_id,
        ):
            generations = tuple(self._repository.list_project_generations(project.automation_id))
            has_undisposed_old = any(
                generation.snapshot.generation != project.committed_generation
                and generation.state != RuntimeGenerationState.DISPOSED
                for generation in generations
            )
            if project.reconcile_state != RuntimeReconcileState.STABLE or has_undisposed_old:
                results.append(self.resume_project(project.automation_id))
        return tuple(results)


def runtime_generation_health(
    repository: RuntimeGenerationRepositoryPort,
    *,
    expected_automation_ids: Collection[str],
    ignored_automation_ids: Collection[str] = (),
) -> RuntimeGenerationHealth:
    """Closed release gate for generation switching and unknown writes."""

    expected = {str(item) for item in expected_automation_ids}
    ignored = {str(item) for item in ignored_automation_ids}
    if expected & ignored:
        raise ValueError("expected and ignored automation identities overlap")
    projects = tuple(
        project
        for project in repository.list_project_runtimes()
        if project.automation_id not in ignored
    )
    by_id = {project.automation_id: project for project in projects}
    blockers: dict[str, tuple[str, ...]] = {}
    for missing in sorted(expected - set(by_id)):
        blockers[missing] = ("PROJECT_RUNTIME_MISSING",)
    committed_count = 0
    active_lease_count = 0
    for automation_id, project in sorted(by_id.items()):
        reasons: set[str] = set()
        if project.reconcile_state != RuntimeReconcileState.STABLE:
            reasons.add(f"RECONCILE_{project.reconcile_state.value}")
        if (
            project.committed_generation is None
            or project.target_generation != project.committed_generation
        ):
            reasons.add("TARGET_NOT_COMMITTED")
        else:
            committed_count += 1
        generations = tuple(repository.list_project_generations(automation_id))
        committed_rows = [
            generation
            for generation in generations
            if generation.snapshot.generation == project.committed_generation
        ]
        if len(committed_rows) != 1 or committed_rows[0].state != RuntimeGenerationState.COMMITTED:
            reasons.add("COMMITTED_GENERATION_INVALID")
        for generation in generations:
            number = generation.snapshot.generation
            leases = tuple(repository.list_active_generation_leases(automation_id, number))
            active_lease_count += len(leases)
            if leases:
                reasons.add("ACTIVE_GENERATION_LEASE")
            if repository.has_unknown_generation_write(automation_id, number):
                reasons.add("WRITE_OUTCOME_UNKNOWN")
            if number != project.committed_generation and generation.state != RuntimeGenerationState.DISPOSED:
                reasons.add(f"UNDISPOSED_{generation.state.value}")
        if reasons:
            blockers[automation_id] = tuple(sorted(reasons))
    return RuntimeGenerationHealth(
        healthy=not blockers and expected <= set(by_id),
        project_count=len(projects),
        committed_count=committed_count,
        active_lease_count=active_lease_count,
        blocked_projects=blockers,
    )
