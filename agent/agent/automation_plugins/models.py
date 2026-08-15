"""Domain records shared by plugin services and persistence adapters."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PluginTrustSource(str, Enum):
    ED25519_UPLOAD = "ed25519_upload"
    ED25519_FIRST_PARTY = "ed25519_first_party"
    BUILTIN_RELEASE = "builtin_release"


class PluginProjectState(str, Enum):
    INSTALLED = "INSTALLED"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    UPGRADING = "UPGRADING"
    UNINSTALLING = "UNINSTALLING"
    ERROR = "ERROR"


class PluginVersionState(str, Enum):
    INSTALLED = "INSTALLED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class PluginUninstallStatus(str, Enum):
    PENDING = "UNINSTALL_PENDING"
    COMPLETED = "UNINSTALLED"


class ExecutionBlockKind(str, Enum):
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    WRITE_OUTCOME_UNKNOWN = "WRITE_OUTCOME_UNKNOWN"


class RuntimeReconcileState(str, Enum):
    """Persistent state of one instance's target/committed runtime."""

    STABLE = "STABLE"
    PREPARING = "PREPARING"
    WAITING_COEFFECTS = "WAITING_COEFFECTS"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    DRAINING = "DRAINING"
    DISPOSING = "DISPOSING"
    BLOCKED_UNKNOWN_WRITE = "BLOCKED_UNKNOWN_WRITE"
    ERROR = "ERROR"


class RuntimeGenerationState(str, Enum):
    TARGET = "TARGET"
    PREPARING = "PREPARING"
    WAITING_COEFFECTS = "WAITING_COEFFECTS"
    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"
    DRAINING = "DRAINING"
    DISPOSING = "DISPOSING"
    DISPOSED = "DISPOSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class RuntimeCoeffectKind(str, Enum):
    ACCOUNT = "ACCOUNT"
    SESSION = "SESSION"
    RESOURCE = "RESOURCE"
    DEVICE = "DEVICE"
    CORE_ADAPTER = "CORE_ADAPTER"


class RuntimeEffectKind(str, Enum):
    PACKAGE_REFERENCE = "PACKAGE_REFERENCE"
    VENV_REFERENCE = "VENV_REFERENCE"
    INSTANCE_RUNTIME = "INSTANCE_RUNTIME"
    SCHEDULE_BINDING = "SCHEDULE_BINDING"
    WEBHOOK_BINDING = "WEBHOOK_BINDING"
    BROKER_SCOPE = "BROKER_SCOPE"
    WORKER_DEPLOYMENT = "WORKER_DEPLOYMENT"
    ENTRYPOINT_ROUTE = "ENTRYPOINT_ROUTE"


class RuntimeEffectState(str, Enum):
    PLANNED = "PLANNED"
    APPLIED = "APPLIED"
    DISPOSING = "DISPOSING"
    DISPOSED = "DISPOSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class RuntimeLeaseOutcome(str, Enum):
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_BEFORE_WRITE = "FAILED_BEFORE_WRITE"
    WRITE_VERIFIED = "WRITE_VERIFIED"
    WRITE_OUTCOME_UNKNOWN = "WRITE_OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class DeviceBinding:
    device_id: str
    device_name: str


@dataclass(frozen=True)
class PluginVersionRecord:
    plugin_id: str
    version: str
    package_sha256: str
    manifest_sha256: str
    manifest: Mapping[str, Any]
    trust_source: PluginTrustSource
    install_root: str | None
    state: PluginVersionState = PluginVersionState.INSTALLED
    installed_at: datetime = field(default_factory=utc_now)
    release_sha: str | None = None
    install_metadata: Mapping[str, Any] = field(default_factory=dict)

    def detached_manifest(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.manifest))


@dataclass(frozen=True)
class PluginInstanceRecord:
    automation_id: str
    display_name: str
    plugin_id: str
    state: PluginProjectState
    active_version: PluginVersionRecord
    # The lifecycle state is intentionally independent from whether the
    # currently committed generation may keep accepting work.  During an
    # UPGRADING prepare phase the project row retains its prior ``enabled``
    # bit while ``state`` advertises the transition to operators.
    enabled: bool | None = None
    record_version: int = 1
    target_generation: int = 1
    committed_generation: int | None = 1
    reconcile_state: RuntimeReconcileState = RuntimeReconcileState.STABLE
    committed_snapshot: RuntimeGenerationSnapshot | None = None


@dataclass(frozen=True)
class AutomationProjectConfigRecord:
    automation_id: str
    config: Mapping[str, Any]
    account_bindings: Mapping[str, Any]
    resource_bindings: Mapping[str, str]
    schedule: Mapping[str, Any]
    config_version: int
    configured: bool
    config_sha256: str
    account_bindings_sha256: str
    resource_bindings_sha256: str
    device_binding_sha256: str
    enabled_entrypoints: tuple[str, ...]
    device_binding: DeviceBinding | None = None


@dataclass(frozen=True)
class ExecutionBlock:
    kind: ExecutionBlockKind
    run_id: str
    message: str = ""


@dataclass(frozen=True)
class WorkerCleanupRequest:
    command_id: str
    automation_id: str
    version: str
    device_id: str
    requested_at: datetime
    package_sha256: str
    cleanup_scope: str = "INSTANCE"


@dataclass(frozen=True)
class FirstPartyInstanceSeed:
    automation_id: str
    plugin_id: str
    version: str
    display_name: str
    allowed_entrypoints: tuple[str, ...]


@dataclass(frozen=True)
class BootstrapResult:
    created: tuple[str, ...]
    existing: tuple[str, ...]
    rejected: Mapping[str, str]

    @property
    def ok(self) -> bool:
        return not self.rejected


@dataclass(frozen=True)
class PluginUninstallResult:
    automation_id: str
    purge_id: str
    status: PluginUninstallStatus
    pending_cleanup_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeGenerationSnapshot:
    """Immutable contract addressed by an instance generation.

    The complete non-secret execution metadata is persisted together with its
    hashes so a committed generation can keep running without consulting the
    mutable desired configuration. Account sessions, credentials and
    short-lived broker grants are deliberately excluded.
    """

    automation_id: str
    generation: int
    plugin_id: str
    plugin_version: str
    package_sha256: str
    manifest_sha256: str
    trust_source: PluginTrustSource
    project_config_sha256: str
    account_bindings_sha256: str
    resource_bindings_sha256: str
    device_binding_sha256: str
    schedule_sha256: str
    core_registry_sha256: str
    tool_contract_sha256: str
    invocation_contracts_sha256: str
    compiled_invocations_sha256: str
    runtime_descriptor_sha256: str
    governance_anchor_sha256: str
    policy_contract_sha256: str
    enabled_entrypoints: tuple[str, ...]
    execution_metadata: Mapping[str, Any]
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class RuntimeCoeffectSnapshot:
    kind: RuntimeCoeffectKind
    key: str
    revision: str
    ready: bool
    observed_at: datetime = field(default_factory=utc_now)
    reason_code: str | None = None


@dataclass(frozen=True)
class RuntimeEffectRecord:
    effect_id: str
    automation_id: str
    generation: int
    sequence: int
    kind: RuntimeEffectKind
    state: RuntimeEffectState
    reversible: bool
    effect_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeGenerationRecord:
    snapshot: RuntimeGenerationSnapshot
    state: RuntimeGenerationState
    coeffects: tuple[RuntimeCoeffectSnapshot, ...] = ()
    effects: tuple[RuntimeEffectRecord, ...] = ()


@dataclass(frozen=True)
class ProjectRuntimeRecord:
    automation_id: str
    target_generation: int
    committed_generation: int | None
    reconcile_state: RuntimeReconcileState
    record_version: int


@dataclass(frozen=True)
class RuntimeGenerationLease:
    lease_id: str
    automation_id: str
    generation: int
    snapshot: RuntimeGenerationSnapshot
    runtime_metadata: Mapping[str, Any]
    acquired_at: datetime
    expires_at: datetime
    outcome: RuntimeLeaseOutcome = RuntimeLeaseOutcome.RUNNING


@dataclass(frozen=True)
class GenerationVerificationContext:
    automation_id: str
    generation: int
    lease_id: str
    account_ids: tuple[str, ...]
    account_bindings_sha256: str
    requires_write_verification: bool


class GenerationBoundResult(dict[str, Any]):
    """In-process result envelope with a non-JSON generation side channel.

    Plugin JSON cannot forge this Python-only attribute, and the side channel
    never changes the signed output schema or business evidence payload.
    """

    def __init__(
        self,
        value: Mapping[str, Any],
        *,
        verification: GenerationVerificationContext,
    ) -> None:
        super().__init__(copy.deepcopy(dict(value)))
        self._generation_verification = verification

    @property
    def generation_verification(self) -> GenerationVerificationContext:
        return self._generation_verification
