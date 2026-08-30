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

from agent.automation_plugins.errors import AutomationPluginError, PluginConflictError
from agent.automation_plugins.models import (
    ProjectRuntimeRecord,
    RuntimeEffectKind,
    RuntimeEffectRecord,
    RuntimeEffectState,
    RuntimeCoeffectSnapshot,
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
    archival_unknown_generation_count: int = 0

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

    def _observe_coeffects(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> tuple[RuntimeCoeffectSnapshot, ...]:
        observed = tuple(self._coeffects.observe(snapshot))
        if not observed:
            raise PluginConflictError(
                "runtime generation has no observed coeffects",
                code="RUNTIME_COEFFECTS_MISSING",
            )
        identities = {(item.kind, item.key) for item in observed}
        if len(identities) != len(observed):
            raise PluginConflictError(
                "runtime coeffect snapshot contains duplicate identities"
            )
        return observed

    @staticmethod
    def _unavailable_coeffect_reasons(
        observed: Sequence[RuntimeCoeffectSnapshot],
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    item.reason_code or f"{item.kind.value}:{item.key}"
                    for item in observed
                    if not item.ready
                }
            )
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
        latest = self._observe_coeffects(snapshot)
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

    def _activate_committed_effects(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> None:
        """Refresh optional process-local indexes after the durable route CAS.

        Effect rows remain the source of truth.  Drivers that expose this hook
        may only project those exact APPLIED rows; transports must not become
        visible while a generation is merely PREPARED.
        """

        activate = getattr(self._driver, "activate_committed", None)
        if not callable(activate):
            return
        committed = self._repository.get_generation(
            snapshot.automation_id,
            snapshot.generation,
        )
        if committed is None or committed.state is not RuntimeGenerationState.COMMITTED:
            raise PluginConflictError(
                "committed runtime effects are unavailable after route switch",
                code="RUNTIME_COMMIT_INCONSISTENT",
            )
        activate(snapshot=snapshot, effects=committed.effects)

    def reconcile_committed_projection(
        self,
        generation: RuntimeGenerationRecord,
        *,
        project_enabled: bool,
        defer_scheduler_enable: bool = False,
    ) -> RuntimeReconcileResult:
        """Suspend or restore external routes for one immutable committed row."""

        if generation.state is not RuntimeGenerationState.COMMITTED:
            raise PluginConflictError(
                "runtime projection requires a committed generation",
                code="RUNTIME_COMMIT_INCONSISTENT",
            )
        snapshot = generation.snapshot
        observed = self._observe_coeffects(snapshot)
        reasons = set(self._unavailable_coeffect_reasons(observed))
        if project_enabled is not True:
            reasons.add("PROJECT_DISABLED")
        waiting = tuple(sorted(reasons))
        gate = getattr(
            self._repository,
            "set_project_dependency_scheduler_gate",
            None,
        )
        if not callable(gate):
            raise PluginConflictError(
                "dependency scheduler gate is unavailable",
                code="DEPENDENCY_SCHEDULER_GATE_UNAVAILABLE",
            )
        if waiting:
            # Close the durable physical trigger before withdrawing process
            # routes. Any later failure therefore remains fail-closed.
            gate(snapshot.automation_id, dependency_ready=False)
            deactivate = getattr(self._driver, "deactivate_committed", None)
            if not callable(deactivate):
                raise PluginConflictError(
                    "committed runtime projection cannot be withdrawn",
                    code="RUNTIME_PROJECTION_DEACTIVATION_UNAVAILABLE",
                )
            deactivate(snapshot=snapshot, effects=generation.effects)
        else:
            activate = getattr(self._driver, "activate_committed", None)
            if not callable(activate):
                raise PluginConflictError(
                    "committed runtime projection cannot be restored",
                    code="RUNTIME_PROJECTION_ACTIVATION_UNAVAILABLE",
                )
            # A Provider dependency-tree restore keeps every physical trigger
            # closed until all exact committed process projections are back.
            # Its caller then re-opens each project through the same strict
            # durable gate (including migration ownership checks).
            if defer_scheduler_enable:
                gate(snapshot.automation_id, dependency_ready=False)
            activate(snapshot=snapshot, effects=generation.effects)
            if not defer_scheduler_enable:
                gate(snapshot.automation_id, dependency_ready=True)
        return RuntimeReconcileResult(
            automation_id=snapshot.automation_id,
            target_generation=snapshot.generation,
            committed_generation=snapshot.generation,
            waiting_coeffects=waiting,
        )

    def projection_signature(self) -> object | None:
        signature = getattr(self._driver, "projection_signature", None)
        return signature() if callable(signature) else None

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
        observed = self._observe_coeffects(snapshot)
        self._repository.replace_generation_coeffects(
            snapshot.automation_id,
            snapshot.generation,
            observed,
        )
        unavailable = self._unavailable_coeffect_reasons(observed)
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

    def _is_archival_unknown_generation(
        self,
        generation: RuntimeGenerationRecord,
    ) -> bool:
        """Whether a non-routed generation is permanently retained for audit.

        This is deliberately narrower than "blocked": only the durable
        unknown-write state may survive a route switch.  Any other BLOCKED
        predecessor is a malformed journal and must remain fail-closed.
        """

        return (
            generation.state is RuntimeGenerationState.BLOCKED
            and self._repository.has_unknown_generation_write(
                generation.snapshot.automation_id,
                generation.snapshot.generation,
            )
        )

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
        self._activate_committed_effects(snapshot)
        draining: list[int] = []
        disposed: list[int] = []
        if (
            expected_committed_generation is not None
            and expected_committed_generation != snapshot.generation
        ):
            old = self._repository.get_generation(
                snapshot.automation_id,
                expected_committed_generation,
            )
            if old is None:
                raise PluginConflictError("previous committed generation disappeared")
            if not self._is_archival_unknown_generation(old):
                draining.append(expected_committed_generation)
                # The repository commit transaction has already atomically
                # moved normal predecessors to DRAINING.  Keep this call
                # idempotent for adapters that journal the transition after
                # commit, but never attempt it for the archival BLOCKED row.
                self._repository.mark_generation_draining(
                    snapshot.automation_id,
                    expected_committed_generation,
                )
                old = self._repository.get_generation(
                    snapshot.automation_id,
                    expected_committed_generation,
                ) or old
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
            self._activate_committed_effects(target.snapshot)
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

        if (
            target.state is RuntimeGenerationState.COMMITTED
            and project.committed_generation == target.snapshot.generation
        ):
            self._activate_committed_effects(target.snapshot)

        draining: list[int] = []
        disposed: list[int] = []
        archival_unknown: list[int] = []
        for generation in sorted(
            self._repository.list_project_generations(automation_id),
            key=lambda item: item.snapshot.generation,
        ):
            number = generation.snapshot.generation
            if number == project.committed_generation or generation.state == RuntimeGenerationState.DISPOSED:
                continue
            if generation.state is RuntimeGenerationState.BLOCKED:
                if self._is_archival_unknown_generation(generation):
                    archival_unknown.append(number)
                    continue
                raise PluginConflictError(
                    "non-current blocked runtime generation has no archival unknown write",
                    code="RUNTIME_ARCHIVAL_INCONSISTENT",
                )
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
        if archival_unknown and not draining:
            self._repository.stabilize_project_after_archival_unknown(
                automation_id,
                archival_unknown[0],
            )
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
                and not self._is_archival_unknown_generation(generation)
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

    raw_id_reader = getattr(repository, "list_project_runtime_ids", None)
    if callable(raw_id_reader):
        raw_ids = tuple(str(item or "").strip() for item in raw_id_reader())
        if any(not automation_id for automation_id in raw_ids) or len(set(raw_ids)) != len(
            raw_ids
        ):
            raise PluginConflictError(
                "runtime project identities are missing or duplicated",
                code="PLUGIN_IDENTITY_CONFLICT",
            )
        automation_ids = tuple(
            automation_id for automation_id in raw_ids if automation_id not in ignored
        )
        by_id: dict[str, ProjectRuntimeRecord] = {}
        blockers: dict[str, tuple[str, ...]] = {}
        for automation_id in automation_ids:
            try:
                project = repository.get_project_runtime(automation_id)
            except AutomationPluginError as exc:
                if exc.code == "PLUGIN_IDENTITY_CONFLICT":
                    raise
                blockers[automation_id] = (str(exc.code),)
                continue
            except ValueError:
                blockers[automation_id] = ("PROJECT_RUNTIME_DATA_INVALID",)
                continue
            if project is None:
                blockers[automation_id] = ("PROJECT_RUNTIME_MISSING",)
                continue
            if str(project.automation_id or "").strip() != automation_id:
                blockers[automation_id] = ("PROJECT_RUNTIME_DATA_INVALID",)
                continue
            by_id[automation_id] = project
    else:
        # Compatibility for in-memory adapters. Production persistence exposes
        # raw identities so parsing remains inside a single-project boundary.
        projects = tuple(repository.list_project_runtimes())
        raw_ids = tuple(str(project.automation_id or "").strip() for project in projects)
        if any(not automation_id for automation_id in raw_ids) or len(set(raw_ids)) != len(
            raw_ids
        ):
            raise PluginConflictError(
                "runtime project identities are missing or duplicated",
                code="PLUGIN_IDENTITY_CONFLICT",
            )
        by_id = {
            project.automation_id: project
            for project in projects
            if project.automation_id not in ignored
        }
        automation_ids = tuple(sorted(by_id))
        blockers = {}

    for missing in sorted(expected - set(automation_ids)):
        blockers[missing] = ("PROJECT_RUNTIME_MISSING",)
    committed_count = 0
    active_lease_count = 0
    archival_unknown_generation_count = 0
    for automation_id, project in sorted(by_id.items()):
        reasons: set[str] = set()
        project_committed = False
        project_active_leases = 0
        project_archival_unknown = 0
        try:
            if project.reconcile_state != RuntimeReconcileState.STABLE:
                reasons.add(f"RECONCILE_{project.reconcile_state.value}")
            if (
                project.committed_generation is None
                or project.target_generation != project.committed_generation
            ):
                reasons.add("TARGET_NOT_COMMITTED")
            else:
                project_committed = True
            generations = tuple(repository.list_project_generations(automation_id))
            committed_rows = [
                generation
                for generation in generations
                if generation.snapshot.generation == project.committed_generation
            ]
            if (
                len(committed_rows) != 1
                or committed_rows[0].state != RuntimeGenerationState.COMMITTED
            ):
                reasons.add("COMMITTED_GENERATION_INVALID")
            for generation in generations:
                number = generation.snapshot.generation
                leases = tuple(
                    repository.list_active_generation_leases(automation_id, number)
                )
                project_active_leases += len(leases)
                if leases:
                    reasons.add("ACTIVE_GENERATION_LEASE")
                unknown_write = repository.has_unknown_generation_write(
                    automation_id,
                    number,
                )
                archival_unknown = (
                    number != project.committed_generation
                    and generation.state is RuntimeGenerationState.BLOCKED
                    and unknown_write
                )
                if archival_unknown:
                    project_archival_unknown += 1
                elif unknown_write:
                    reasons.add("WRITE_OUTCOME_UNKNOWN")
                if (
                    number != project.committed_generation
                    and generation.state != RuntimeGenerationState.DISPOSED
                    and not archival_unknown
                ):
                    reasons.add(f"UNDISPOSED_{generation.state.value}")
        except AutomationPluginError as exc:
            if exc.code == "PLUGIN_IDENTITY_CONFLICT":
                raise
            blockers[automation_id] = (str(exc.code),)
            continue
        except ValueError:
            blockers[automation_id] = ("PROJECT_RUNTIME_DATA_INVALID",)
            continue
        if project_committed:
            committed_count += 1
        active_lease_count += project_active_leases
        archival_unknown_generation_count += project_archival_unknown
        if reasons:
            blockers[automation_id] = tuple(sorted(reasons))
    return RuntimeGenerationHealth(
        healthy=not blockers and expected <= set(automation_ids),
        project_count=len(automation_ids),
        committed_count=committed_count,
        active_lease_count=active_lease_count,
        blocked_projects=blockers,
        archival_unknown_generation_count=archival_unknown_generation_count,
    )
