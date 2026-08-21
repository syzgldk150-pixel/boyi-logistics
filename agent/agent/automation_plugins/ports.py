"""Persistence and infrastructure ports for plugin lifecycle composition.

These contracts intentionally name domain operations, not migration numbers or
database tables. Implementations must make the documented uninstall steps
transactional and fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.models import (
    ExecutionBlock,
    AutomationProjectConfigRecord,
    DeviceBinding,
    FirstPartyInstanceSeed,
    PluginInstanceRecord,
    ProjectRuntimeRecord,
    RuntimeCoeffectSnapshot,
    RuntimeEffectKind,
    RuntimeEffectRecord,
    RuntimeGenerationLease,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeLeaseOutcome,
    PluginVersionRecord,
    WorkerCleanupRequest,
)


@dataclass(frozen=True)
class BootstrapPersistenceResult:
    created: tuple[str, ...]
    existing: tuple[str, ...]


@dataclass(frozen=True)
class HardUninstallPreparation:
    purge_id: str
    instance: PluginInstanceRecord
    cleanup_requests: tuple[WorkerCleanupRequest, ...]
    prepared_at: datetime
    delete_shared_package: bool


@runtime_checkable
class AutomationPluginRepositoryPort(Protocol):
    def get_package_version(self, plugin_id: str, version: str) -> PluginVersionRecord | None: ...

    def get_instance(self, automation_id: str) -> PluginInstanceRecord | None: ...

    def list_instances(self) -> Sequence[PluginInstanceRecord]: ...

    def install_instance(
        self,
        version: PluginVersionRecord,
        *,
        instance_name: str,
        actor_id: str,
        actor_role: str,
        request_id: str,
    ) -> PluginInstanceRecord:
        """Create a disabled, unconfigured instance and ref-count its package.

        The implementation generates the unique automation_id. Repeating the
        same (request_id, package digest, instance_name) returns that same ID;
        reuse with different inputs raises IDEMPOTENCY_CONFLICT. Package
        registration, empty account/resource bindings, unconfigured project
        config and REQUIRE_EACH_RUN policy are one transaction or a recoverable
        journal. No default account or schedule is created.
        """

    def upgrade_instance(
        self,
        automation_id: str,
        version: PluginVersionRecord,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
    ) -> PluginInstanceRecord:
        """Stage a desired version and mark project authorization stale.

        Existing config/account/resource/device bindings are preserved.  The
        committed generation continues serving until the new package is fully
        prepared and a later generation CAS switches all entrypoints.  A
        failure leaves the old committed generation executable and authority
        fail-closed.
        """

    def bootstrap_missing(
        self,
        versions: Sequence[PluginVersionRecord],
        instances: Sequence[FirstPartyInstanceSeed],
        *,
        release_sha: str,
    ) -> BootstrapPersistenceResult:
        """Atomically insert missing built-ins and preserve every existing row."""

    def set_enabled(
        self,
        automation_id: str,
        *,
        enabled: bool,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_record_version: int,
    ) -> PluginInstanceRecord: ...

    def prepare_hard_uninstall(
        self,
        automation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_current_version: str,
        expected_record_version: int,
    ) -> HardUninstallPreparation:
        """Atomically revoke execution/policy authority or raise on any block.

        The transaction must lock the project and re-check RUNNING, VERIFYING
        and WRITE_OUTCOME_UNKNOWN activity. If any exists, it must leave the
        project and policy untouched. On success it creates a short-lived purge
        journal and returns cleanup directives for explicitly bound devices.
        """

    def persist_cleanup_requests(
        self,
        preparation: HardUninstallPreparation,
    ) -> None:
        """Persist device cleanup independently of the plugin being deleted."""

    def get_hard_uninstall_preparation(
        self,
        *,
        automation_id: str,
        purge_id: str,
    ) -> HardUninstallPreparation | None: ...

    def all_cleanup_acknowledged(self, preparation: HardUninstallPreparation) -> bool:
        """True only when every exact device confirmed instance/package cleanup."""

    def reserve_hard_uninstall_finalize(
        self,
        preparation: HardUninstallPreparation,
    ) -> HardUninstallPreparation:
        """Atomically recheck ACKs, protected writes and last-version refcount."""

    def hard_delete_application_state(
        self,
        preparation: HardUninstallPreparation,
    ) -> None:
        """Delete application-owned plugin/policy/run/approval/evidence/log state.

        The purge journal, immutable version row and cleanup directives are
        excluded so filesystem cleanup can be retried after a crash. The
        instance has already been revoked and remains unreachable.
        """

    def complete_hard_uninstall(self, preparation: HardUninstallPreparation) -> None:
        """Commit the purge and remove its short-lived journal."""

    def mark_purge_failed(
        self,
        preparation: HardUninstallPreparation,
        *,
        error_code: str,
        error_summary: str,
    ) -> None: ...

    def list_execution_blocks(self, automation_id: str) -> Sequence[ExecutionBlock]: ...


@runtime_checkable
class PluginStoragePort(Protocol):
    def create_staging_root(self, plugin_id: str, version: str) -> Path: ...

    def commit_staging_root(
        self,
        staging_root: Path,
        *,
        plugin_id: str,
        version: str,
        manifest_sha256: str,
    ) -> Path: ...

    def persist_verified_archive(
        self,
        staging_root: Path,
        archive_bytes: bytes,
        *,
        expected_sha256: str,
    ) -> str:
        """Persist the original verified ZIP inside the immutable version tree.

        The returned value is a storage-relative filename, never an absolute
        filesystem path.  Worker delivery must re-read these exact bytes and
        re-check ``expected_sha256`` instead of rebuilding a ZIP from extracted
        files.
        """

    def read_verified_archive(
        self,
        install_root: Path,
        archive_relative: str,
        *,
        expected_sha256: str,
    ) -> bytes:
        """Return exact immutable archive bytes after path and digest checks."""

    def remove_version_root(self, install_root: Path) -> None: ...

    def remove_plugin_roots(self, plugin_id: str) -> None: ...

    def discard_staging_root(self, staging_root: Path) -> None: ...


@runtime_checkable
class PluginEnvironmentBuilderPort(Protocol):
    def build(self, version_root: Path, manifest: AutomationPluginManifest) -> Path: ...


@runtime_checkable
class ExecutionCapabilityIssuerPort(Protocol):
    @property
    def broker_endpoint(self) -> str: ...

    @property
    def broker_socket_path(self) -> Path | None: ...

    def issue(
        self,
        *,
        automation_id: str,
        plugin_version: str,
        tool_name: str,
        ttl_seconds: int,
        runtime_permissions: Mapping[str, object],
        account_roles: Sequence[Mapping[str, object]],
        resource_roles: Sequence[Mapping[str, object]],
        account_bindings: Mapping[str, object],
        resource_bindings: Mapping[str, str],
    ) -> str: ...

    def revoke(self, capability: str) -> None: ...

    def consumed_call_count(self, capability: str) -> int:
        """Return core-owned broker requests consumed by this exact capability."""


@runtime_checkable
class FirstPartyPackageProvider(Protocol):
    def load_versions(
        self,
        *,
        core_catalog: object,
        current_release_sha: str,
        expected_release_sha: str,
    ) -> Sequence[PluginVersionRecord]: ...


@runtime_checkable
class FirstPartyPackageMaterializerPort(Protocol):
    def materialize(self, version: PluginVersionRecord) -> PluginVersionRecord:
        """Create the immutable package root and isolated venv for one built-in."""

    def discard(self, version: PluginVersionRecord) -> None:
        """Remove an unreferenced root created by ``materialize``."""


@runtime_checkable
class PluginIntegrityVerifierPort(Protocol):
    def verify_install_root(self, runtime_metadata: Mapping[str, object]) -> None: ...


@runtime_checkable
class PluginSandboxLauncherPort(Protocol):
    async def launch(
        self,
        *,
        install_root: Path,
        python_relative: str,
        entrypoint_relative: str,
        environment: Mapping[str, str],
        broker_socket_path: Path | None,
    ) -> object:
        """Launch below a real OS sandbox or fail closed."""


@runtime_checkable
class AutomationProjectConfigurationPort(Protocol):
    """Core-owned project settings; plugins never persist config or cron rows."""

    def get_project_config(self, automation_id: str) -> AutomationProjectConfigRecord | None: ...

    def initialize_unconfigured_project(
        self,
        automation_id: str,
        *,
        config_schema: Mapping[str, object],
        worker_requirement: Mapping[str, object],
        request_id: str,
    ) -> AutomationProjectConfigRecord:
        """Create empty config, no schedule and REQUIRE_EACH_RUN authority."""

    def mark_plugin_contract_stale(
        self,
        automation_id: str,
        *,
        from_manifest_sha256: str | None,
        to_manifest_sha256: str,
        request_id: str,
    ) -> None:
        """Invalidate full-auto authority without overwriting config or cron."""

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
        device_binding: DeviceBinding | None,
        actor_id: str,
        actor_role: str,
        request_id: str,
        expected_project_configuration_version: int,
    ) -> AutomationProjectConfigRecord:
        """CAS all core-owned settings and atomically stale authorization."""


@runtime_checkable
class ProjectBindingResolverPort(Protocol):
    def validate_account_binding(
        self,
        *,
        automation_id: str,
        role: Mapping[str, object],
        account_id: str,
    ) -> None:
        """Reject missing, inactive or wrong-system accounts at save time.

        The execution capability issuer must re-resolve the exact account ID
        and require a currently authenticated SessionBroker session. Missing,
        disabled or expired sessions fail as BLOCKED_LOGIN; no replacement or
        default account is permitted.
        """

    def validate_resource_binding(
        self,
        *,
        automation_id: str,
        role: Mapping[str, object],
        resource_id: str,
    ) -> None: ...

    def resolve_device_binding(
        self,
        *,
        automation_id: str,
        device_id: str,
        worker_requirement: Mapping[str, object],
    ) -> DeviceBinding:
        """Return the exact named-device binding or raise; never reassign."""


@dataclass(frozen=True)
class RuntimeEffectPlan:
    """A reversible, core-owned side effect prepared for a generation."""

    kind: RuntimeEffectKind
    effect_key: str
    payload: Mapping[str, Any]
    reversible: bool = True


@runtime_checkable
class RuntimeGenerationRepositoryPort(Protocol):
    """Crash-recoverable target/committed generation persistence."""

    def get_project_runtime(self, automation_id: str) -> ProjectRuntimeRecord | None: ...

    def list_project_runtimes(self) -> Sequence[ProjectRuntimeRecord]: ...

    def get_generation(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord | None: ...

    def list_project_generations(
        self,
        automation_id: str,
    ) -> Sequence[RuntimeGenerationRecord]: ...

    def allocate_target_generation(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        expected_committed_generation: int | None,
        request_id: str,
    ) -> RuntimeGenerationRecord:
        """Persist a new target without changing the committed route."""

    def mark_generation_preparing(self, automation_id: str, generation: int) -> None: ...

    def replace_generation_coeffects(
        self,
        automation_id: str,
        generation: int,
        coeffects: Sequence[RuntimeCoeffectSnapshot],
    ) -> None: ...

    def mark_generation_waiting_coeffects(
        self,
        automation_id: str,
        generation: int,
        *,
        reason_codes: Sequence[str],
    ) -> None: ...

    def reserve_generation_effect(
        self,
        snapshot: RuntimeGenerationSnapshot,
        *,
        plan: RuntimeEffectPlan,
        sequence: int,
    ) -> RuntimeEffectRecord:
        """Durably reserve deterministic PLANNED ownership before apply."""

    def mark_generation_effect_applied(
        self,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord:
        """Transition the exact reserved effect from PLANNED to APPLIED."""

    def mark_generation_prepared(self, automation_id: str, generation: int) -> None: ...

    def commit_generation_cas(
        self,
        automation_id: str,
        generation: int,
        *,
        expected_committed_generation: int | None,
    ) -> ProjectRuntimeRecord:
        """Atomically switch every instance entrypoint to ``generation``."""

    def mark_generation_draining(self, automation_id: str, generation: int) -> None: ...

    def list_active_generation_leases(
        self,
        automation_id: str,
        generation: int,
    ) -> Sequence[RuntimeGenerationLease]: ...

    def has_unknown_generation_write(self, automation_id: str, generation: int) -> bool: ...

    def reserve_generation_dispose(
        self,
        automation_id: str,
        generation: int,
    ) -> RuntimeGenerationRecord:
        """Recheck no lease/unknown write and enter DISPOSING atomically.

        Repeating this call for an already-DISPOSING generation returns the
        same journal so a crash between dispose and ACK is recoverable.
        """

    def mark_generation_effect_disposing(self, effect_id: str) -> None: ...

    def mark_generation_effect_disposed(self, effect_id: str) -> None: ...

    def complete_generation_dispose(self, automation_id: str, generation: int) -> None: ...

    def fail_generation(
        self,
        automation_id: str,
        generation: int,
        *,
        error_code: str,
        error_summary: str,
    ) -> None: ...

    def block_generation_unknown_write(self, automation_id: str, generation: int) -> None: ...


@runtime_checkable
class RuntimeUnknownWriteReadPort(Protocol):
    """Read the complete unknown-write topology without exposing persistence SQL."""

    def list_project_generations(
        self,
        automation_id: str,
    ) -> Sequence[RuntimeGenerationRecord]: ...

    def list_active_generation_leases(
        self,
        automation_id: str,
        generation: int,
    ) -> Sequence[RuntimeGenerationLease]: ...

    def find_current_unknown_generation_write(
        self,
        automation_id: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class RuntimeCoeffectProviderPort(Protocol):
    def observe(
        self,
        snapshot: RuntimeGenerationSnapshot,
    ) -> Sequence[RuntimeCoeffectSnapshot]:
        """Read stable account/resource/device/adapter generation revisions.

        Transient authentication state is revalidated at invocation time and
        must not decide whether an immutable runtime generation can commit.
        """


@runtime_checkable
class RuntimeEffectPlannerPort(Protocol):
    def plan(self, snapshot: RuntimeGenerationSnapshot) -> Sequence[RuntimeEffectPlan]: ...


@runtime_checkable
class RuntimeEffectDriverPort(Protocol):
    def ensure_applied(
        self,
        *,
        snapshot: RuntimeGenerationSnapshot,
        plan: RuntimeEffectPlan,
        effect: RuntimeEffectRecord,
    ) -> RuntimeEffectRecord: ...

    def dispose(self, effect: RuntimeEffectRecord) -> None: ...


@runtime_checkable
class RuntimeGenerationLeasePort(Protocol):
    """Atomic invocation leases make committed-generation switching race free."""

    def acquire_committed_generation(
        self,
        automation_id: str,
        *,
        expected_generation: int,
        expected_manifest_sha256: str,
        lease_id: str,
        expires_at: datetime,
    ) -> RuntimeGenerationLease:
        """Lock the route and lease only the caller-approved exact generation."""

    def release_generation(
        self,
        lease: RuntimeGenerationLease,
        *,
        outcome: RuntimeLeaseOutcome,
    ) -> None:
        """Record process outcome.

        VERIFYING deliberately keeps a write lease non-terminal until the
        orchestration ResultVerifier calls ``finalize_generation_write``.
        """

    def finalize_generation_write(
        self,
        *,
        automation_id: str,
        generation: int,
        lease_id: str,
        outcome: RuntimeLeaseOutcome,
        evidence_sha256: str,
    ) -> None:
        """Accept only WRITE_VERIFIED or WRITE_OUTCOME_UNKNOWN, idempotently."""
