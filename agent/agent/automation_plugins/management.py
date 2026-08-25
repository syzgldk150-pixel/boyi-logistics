"""Authenticated application service for plugin lifecycle and project settings."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.automation_plugins.catalog import PluginCatalog, PluginCatalogEntry
from agent.automation_plugins.configuration import AutomationProjectConfigurationService
from agent.automation_plugins.errors import PluginConflictError, PluginNotFoundError
from agent.automation_plugins.first_party import RECOVERABLE_WRITE_PROJECT_PLUGINS
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginInstanceRecord,
    PluginProjectState,
    PluginTrustSource,
    RuntimeReconcileState,
)
from agent.automation_plugins.ports import PluginStoragePort
from agent.orchestration.models import Actor, ActorType
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
)


_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DEVICE_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _iso_datetime(value: object) -> str:
    if not isinstance(value, datetime):
        return ""
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def classify_arrival_stats_recovery_readback(
    evidence: Mapping[str, object],
) -> str:
    """Classify only the closed, safe readback contract for one recovery."""

    expected = {
        "arrival_stat_runs",
        "arrival_stat_items",
        "feishu_rows_created",
    }
    if set(evidence) != expected:
        raise ValueError("arrival statistics recovery readback is incomplete")
    values: list[int] = []
    for field in sorted(expected):
        value = evidence.get(field)
        if type(value) is not int or value < 0:
            raise ValueError("arrival statistics recovery readback count is invalid")
        values.append(value)
    return "NOT_APPLIED" if not any(values) else "WRITE_OUTCOME_UNKNOWN"


class AutomationPluginManagementService:
    """Keep HTTP DTO handling outside lifecycle/configuration domain services."""

    def __init__(
        self,
        *,
        catalog: PluginCatalog,
        lifecycle: AutomationPluginService,
        configuration: AutomationProjectConfigurationService,
        worker_repository: Any,
        target_service: Any,
        package_repository: Any,
        storage: PluginStoragePort,
        release_hold_provider: Callable[[], bool] | None = None,
        resource_catalog_provider: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._catalog = catalog
        self._lifecycle = lifecycle
        self._configuration = configuration
        self._workers = worker_repository
        self._targets = target_service
        self._packages = package_repository
        self._storage = storage
        self._release_hold_provider = release_hold_provider or (lambda: False)
        self._resource_catalog_provider = resource_catalog_provider

    @staticmethod
    def _require_console_actor(actor: Actor, *, super_admin: bool) -> str:
        roles = frozenset(str(role).strip().lower() for role in actor.roles)
        if (
            actor.actor_type is not ActorType.CONSOLE_ADMIN
            or actor.authenticated_by != "mysql_admin_session"
            or not ({"admin", "super_admin"} & roles)
            or (super_admin and "super_admin" not in roles)
        ):
            raise PluginConflictError(
                "an authenticated Console super administrator is required"
                if super_admin
                else "an authenticated Console administrator is required",
                code="PLUGIN_MANAGEMENT_FORBIDDEN",
            )
        return "super_admin" if "super_admin" in roles else "admin"

    def _require_mutation_allowed(self) -> None:
        """Fail closed while deployment owns the plugin control plane."""

        try:
            release_held = self._release_hold_provider()
        except Exception as exc:
            raise PluginConflictError(
                "automation plugin release-hold state is unavailable",
                code="PLUGIN_RELEASE_HOLD_STATE_UNAVAILABLE",
            ) from exc
        if release_held is not False:
            raise PluginConflictError(
                "automation plugin mutations are disabled during release hold",
                code="PLUGIN_RELEASE_HOLD",
            )

    @staticmethod
    def _instance_projection(instance: PluginInstanceRecord) -> dict[str, Any]:
        return {
            "automation_id": instance.automation_id,
            "plugin_id": instance.plugin_id,
            "instance_name": instance.display_name,
            "version": instance.active_version.version,
            "enabled": (
                instance.enabled
                if instance.enabled is not None
                else instance.state.value == "ENABLED"
            ),
            "state": instance.state.value,
            "record_version": instance.record_version,
            "target_generation": instance.target_generation,
            "committed_generation": instance.committed_generation,
            "reconcile_state": instance.reconcile_state.value,
        }

    @staticmethod
    def _configuration_projection(
        record: AutomationProjectConfigRecord,
    ) -> dict[str, Any]:
        return {
            "automation_id": record.automation_id,
            "configured": record.configured,
            "project_configuration_version": record.config_version,
            "schedule": dict(record.schedule),
            "enabled_entrypoints": list(record.enabled_entrypoints),
        }

    def catalog_projection(self, *, actor: Actor) -> dict[str, Any]:
        self._require_console_actor(actor, super_admin=False)
        projection = self._catalog.safe_projection()
        resources: list[dict[str, str]] = []
        resource_pool_available = self._resource_catalog_provider is not None
        if self._resource_catalog_provider is not None:
            try:
                raw_resources = self._resource_catalog_provider()
                seen: set[str] = set()
                for raw in raw_resources:
                    if not isinstance(raw, Mapping) or set(raw) != {
                        "resource_id",
                        "name",
                        "kind",
                        "status",
                    }:
                        raise ValueError("managed resource projection is not closed")
                    resource_id = str(raw.get("resource_id") or "").strip()
                    name = str(raw.get("name") or "").strip()
                    kind = str(raw.get("kind") or "").strip().lower()
                    status = str(raw.get("status") or "").strip().lower()
                    if (
                        resource_id in seen
                        or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,160}", resource_id)
                        or not name
                        or len(name) > 160
                        or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", kind)
                        or status != "available"
                    ):
                        raise ValueError("managed resource projection is invalid")
                    seen.add(resource_id)
                    resources.append(
                        {
                            "resource_id": resource_id,
                            "name": name,
                            "kind": kind,
                            "status": status,
                        }
                    )
            except Exception:  # noqa: BLE001 - read projection must fail closed
                resources = []
                resource_pool_available = False
        projection["resources"] = sorted(resources, key=lambda item: item["resource_id"])
        projection["resource_pool_available"] = resource_pool_available
        return projection

    def recover_arrival_stats_not_applied(
        self,
        automation_id: str,
        *,
        generation: int,
        lease_id: str,
        evidence_sha256: str,
        readback: Mapping[str, object],
        request_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        del generation, lease_id, evidence_sha256, readback, request_id
        self._require_console_actor(actor, super_admin=True)
        raise PluginConflictError(
            "actor-supplied arrival statistics readback is not recovery authority",
            code="PLUGIN_RECOVERY_ACTOR_EVIDENCE_REJECTED",
        )

    def recover_unknown_write(
        self,
        automation_id: str,
        *,
        generation: int,
        lease_id: str,
        request_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """Resolve an unknown write using only server-owned durable evidence."""

        self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        entry = self._catalog.require(automation_id)
        # Scope is package identity, not the display/default instance id: a
        # reviewed first-party package may be installed under another exact
        # automation id and still has its own isolated receipt chain.
        if entry.plugin_id not in frozenset(RECOVERABLE_WRITE_PROJECT_PLUGINS.values()):
            raise PluginConflictError(
                "recovery is not available for this automation project",
                code="PLUGIN_RECOVERY_SCOPE_INVALID",
            )
        reader = getattr(self._targets, "recover_unknown_write", None)
        if not callable(reader):
            raise PluginConflictError(
                "server recovery reader is unavailable",
                code="PLUGIN_RECOVERY_UNAVAILABLE",
            )
        result = reader(
            automation_id=automation_id,
            generation=generation,
            lease_id=lease_id,
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role="super_admin",
        )
        if not isinstance(result, Mapping) or result.get("recovery_status") not in {
            "APPLIED", "NOT_APPLIED", "UNKNOWN",
        }:
            raise PluginConflictError(
                "server recovery reader returned invalid evidence",
                code="PLUGIN_RECOVERY_UNAVAILABLE",
            )
        return {
            **self._catalog_instance_projection(entry),
            "recovery_status": str(result["recovery_status"]),
            "reason": str(result.get("reason") or ""),
            "run_id": str(result.get("run_id") or ""),
            "step_id": str(result.get("step_id") or ""),
            "transitioned": bool(result.get("transitioned")),
            "idempotent": bool(result.get("idempotent")),
            "evidence": dict(result.get("evidence") or {}),
        }

    def recover_current_unknown_write(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """Recover the exact current generation without actor-supplied identity."""

        self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        entry = self._catalog.require(automation_id)
        if entry.plugin_id not in frozenset(RECOVERABLE_WRITE_PROJECT_PLUGINS.values()):
            raise PluginConflictError(
                "recovery is not available for this automation project",
                code="PLUGIN_RECOVERY_SCOPE_INVALID",
            )
        if (
            entry.reconcile_state is not RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE
            or entry.committed_generation is None
            or entry.target_generation != entry.committed_generation
        ):
            raise PluginConflictError(
                "automation project is not blocked on one current unknown write",
                code="PLUGIN_RECOVERY_STATE_INVALID",
            )
        reader = getattr(self._targets, "recover_current_unknown_write", None)
        if not callable(reader):
            raise PluginConflictError(
                "server recovery reader is unavailable",
                code="PLUGIN_RECOVERY_UNAVAILABLE",
            )
        result = reader(
            automation_id=automation_id,
            generation=int(entry.committed_generation),
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role="super_admin",
        )
        if not isinstance(result, Mapping) or result.get("recovery_status") not in {
            "APPLIED", "NOT_APPLIED", "UNKNOWN",
        }:
            raise PluginConflictError(
                "server recovery reader returned invalid evidence",
                code="PLUGIN_RECOVERY_UNAVAILABLE",
            )
        refreshed = self._catalog.require(automation_id)
        return {
            **self._catalog_instance_projection(refreshed),
            "recovery_status": str(result["recovery_status"]),
            "reason": str(result.get("reason") or ""),
            "run_id": str(result.get("run_id") or ""),
            "step_id": str(result.get("step_id") or ""),
            "transitioned": bool(result.get("transitioned")),
            "idempotent": bool(result.get("idempotent")),
            "evidence": dict(result.get("evidence") or {}),
        }

    def worker_projection(self, *, actor: Actor) -> dict[str, Any]:
        self._require_console_actor(actor, super_admin=False)
        workers: list[dict[str, Any]] = []
        for row in self._workers.list_worker_devices():
            device_id = str(row.get("device_id") or "").strip()
            platform = str(row.get("platform") or "").strip().lower()
            service_state = str(row.get("service_state") or "").strip().upper()
            if not device_id or platform != "windows":
                continue
            workers.append(self._worker_row_projection(row))
        return {"workers": sorted(workers, key=lambda item: item["device_id"])}

    @staticmethod
    def _worker_row_projection(row: Mapping[str, Any]) -> dict[str, Any]:
        device_id = str(row.get("device_id") or "").strip()
        service_state = str(row.get("service_state") or "").strip().upper()
        return {
            "device_id": device_id,
            "worker_id": device_id,
            "display_name": str(row.get("display_name") or device_id)[:120],
            "platform": "windows",
            "state": service_state,
            "status": service_state,
            "session_state": str(row.get("interactive_session_state") or "").upper(),
            "online": service_state == "ONLINE",
            "last_seen_at": _iso_datetime(row.get("last_seen_at")),
        }

    def pair_worker(
        self,
        *,
        device_id: str,
        display_name: str,
        platform: str,
        agent_version: str,
        identity: Mapping[str, Any],
        capabilities: Mapping[str, Any],
        request_id: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        try:
            uuid.UUID(str(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginConflictError(
                "Worker pairing request_id must be UUID",
                code="PLUGIN_WORKER_PAIRING_REQUEST_INVALID",
            ) from exc
        normalized_device_id = str(device_id or "").strip()
        normalized_name = str(display_name or "").strip()
        normalized_platform = str(platform or "").strip().lower()
        normalized_version = str(agent_version or "").strip()
        if (
            not _DEVICE_ID_RE.fullmatch(normalized_device_id)
            or not normalized_name
            or len(normalized_name) > 120
            or normalized_platform != "windows"
            or not re.fullmatch(r"\d+\.\d+\.\d+", normalized_version)
        ):
            raise PluginConflictError(
                "Worker pairing descriptor is invalid",
                code="PLUGIN_WORKER_PAIRING_REQUEST_INVALID",
            )
        closed_identity = dict(identity)
        if set(closed_identity) != {
            "device_key_id",
            "ed25519_public_key_base64",
            "tls_client_certificate_sha256",
        }:
            raise PluginConflictError(
                "Worker public identity fields are invalid",
                code="PLUGIN_WORKER_IDENTITY_INVALID",
            )
        key_id = str(closed_identity.get("device_key_id") or "")
        public_text = str(closed_identity.get("ed25519_public_key_base64") or "")
        certificate_sha256 = str(
            closed_identity.get("tls_client_certificate_sha256") or ""
        ).lower()
        try:
            public_key = base64.b64decode(public_text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PluginConflictError(
                "Worker Ed25519 public key is invalid",
                code="PLUGIN_WORKER_IDENTITY_INVALID",
            ) from exc
        if (
            not _DEVICE_KEY_ID_RE.fullmatch(key_id)
            or len(public_key) != 32
            or base64.b64encode(public_key).decode("ascii") != public_text
            or not _SHA256_RE.fullmatch(certificate_sha256)
        ):
            raise PluginConflictError(
                "Worker public identity is invalid",
                code="PLUGIN_WORKER_IDENTITY_INVALID",
            )
        closed_capabilities = dict(capabilities)
        if set(closed_capabilities) != {"interactive"} or not isinstance(
            closed_capabilities.get("interactive"),
            bool,
        ):
            raise PluginConflictError(
                "Worker capabilities are invalid",
                code="PLUGIN_WORKER_CAPABILITY_INVALID",
            )
        persisted = self._workers.pair_worker_device(
            {
                "device_id": normalized_device_id,
                "display_name": normalized_name,
                "platform": normalized_platform,
                "agent_version": normalized_version,
                "identity_json": {
                    "device_key_id": key_id,
                    "ed25519_public_key_base64": public_text,
                    "tls_client_certificate_sha256": certificate_sha256,
                },
                "paired_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
                "capabilities_json": closed_capabilities,
            },
            request_id=str(request_id),
            actor_id=actor.actor_id,
            actor_role=role,
        )
        return self._worker_row_projection(persisted)

    def install(
        self,
        package_bytes: bytes,
        *,
        instance_name: str,
        request_id: str,
        transport_package_sha256: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        instance = self._lifecycle.install_upload(
            package_bytes,
            instance_name=instance_name,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            transport_package_sha256=transport_package_sha256,
        )
        return self._instance_projection(instance)

    def upgrade(
        self,
        automation_id: str,
        package_bytes: bytes,
        *,
        request_id: str,
        expected_record_version: int,
        transport_package_sha256: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        current = self._catalog.require(automation_id)
        # The repository owns the atomic CAS and request UUID idempotency.  Do
        # not reject a response-loss retry merely because the first attempt
        # already advanced record_version before its HTTP response was lost.
        self._lifecycle.upgrade_upload(
            automation_id,
            package_bytes,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            expected_current_version=current.installed_version,
            expected_record_version=expected_record_version,
            transport_package_sha256=transport_package_sha256,
        )
        # The desired version is durable at this point.  A dependency may be
        # temporarily unavailable while the new generation is prepared, so a
        # post-write reconcile failure must not be reported as if the upgrade
        # itself had rolled back.  The transition projection remains explicitly
        # non-runnable until a matching committed generation becomes STABLE.
        reconcile_failed = False
        try:
            self._targets.reconcile_project(automation_id)
        except Exception:  # noqa: BLE001 - mutation already committed; project safe state
            reconcile_failed = True
        refreshed = self._catalog.require(automation_id)
        projection = self._catalog_instance_projection(refreshed)
        projection.update(self._transition_projection(refreshed, reconcile_failed))
        return projection

    @staticmethod
    def _catalog_instance_projection(entry: PluginCatalogEntry) -> dict[str, Any]:
        return {
            "automation_id": entry.automation_id,
            "plugin_id": entry.plugin_id,
            "instance_name": entry.display_name,
            "version": entry.installed_version,
            "enabled": entry.enabled,
            "state": entry.state,
            "record_version": entry.record_version,
            "target_generation": entry.target_generation,
            "committed_generation": entry.committed_generation,
            "reconcile_state": entry.reconcile_state.value,
        }

    @staticmethod
    def _require_committed_ready(entry: PluginCatalogEntry) -> None:
        snapshot = entry.committed_snapshot
        metadata = snapshot.execution_metadata if snapshot is not None else {}
        if (
            not entry.configured
            or snapshot is None
            or entry.committed_generation != snapshot.generation
            or entry.target_generation != entry.committed_generation
            or entry.reconcile_state is not RuntimeReconcileState.STABLE
            or snapshot.plugin_version != entry.installed_version
            or snapshot.project_config_sha256 != entry.project_config_sha256
            or snapshot.account_bindings_sha256 != entry.account_bindings_sha256
            or snapshot.resource_bindings_sha256 != entry.resource_bindings_sha256
            or snapshot.device_binding_sha256 != entry.device_binding_sha256
            or tuple(snapshot.enabled_entrypoints) != tuple(entry.current_enabled_entrypoints)
            or int(metadata.get("project_config_version") or 0)
            != entry.project_config_version
        ):
            raise PluginConflictError(
                "the desired plugin generation is not committed and release-ready",
                code="PLUGIN_GENERATION_NOT_READY",
            )

    def set_enabled(
        self,
        automation_id: str,
        *,
        enabled: bool,
        request_id: str,
        expected_record_version: int,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        entry = self._catalog.require(automation_id)
        if entry.record_version != expected_record_version:
            # A committed state change can outlive its HTTP response.  Let the
            # audited repository prove an exact retry before rejecting the
            # stale catalog version; never reconcile or prepare new work here.
            # The catalog must first have the only shape an exact lost-response
            # replay can produce.  This prevents the probe from creating a new
            # enable that bypasses readiness checks if versions race forward.
            target_state = (
                PluginProjectState.ENABLED.value
                if enabled
                else PluginProjectState.DISABLED.value
            )
            projected_state = getattr(entry.state, "value", entry.state)
            if (
                entry.record_version != expected_record_version + 1
                or bool(entry.enabled) is not bool(enabled)
                or projected_state != target_state
            ):
                raise PluginConflictError(
                    "automation instance version changed before state update",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                )
            try:
                instance = self._lifecycle.set_enabled(
                    automation_id,
                    enabled=enabled,
                    actor_id=actor.actor_id,
                    actor_role=role,
                    request_id=request_id,
                    expected_record_version=expected_record_version,
                )
            except (ConcurrentUpdateError, IdempotencyConflict) as exc:
                raise PluginConflictError(
                    "automation instance version changed before state update",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                ) from exc
            return self._instance_projection(instance)
        if enabled:
            expected_material = (
                entry.installed_version,
                entry.project_config_version,
                entry.project_config_sha256,
                entry.account_bindings_sha256,
                entry.resource_bindings_sha256,
                entry.device_binding_sha256,
            )
            self._targets.reconcile_project(automation_id)
            entry = self._catalog.require(automation_id)
            if expected_material != (
                entry.installed_version,
                entry.project_config_version,
                entry.project_config_sha256,
                entry.account_bindings_sha256,
                entry.resource_bindings_sha256,
                entry.device_binding_sha256,
            ):
                raise PluginConflictError(
                    "automation instance material changed while preparing enable",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                )
            self._require_committed_ready(entry)
            expected_record_version = entry.record_version
        instance = self._lifecycle.set_enabled(
            automation_id,
            enabled=enabled,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            expected_record_version=expected_record_version,
        )
        return self._instance_projection(instance)

    def uninstall(
        self,
        automation_id: str,
        *,
        request_id: str,
        expected_record_version: int,
        current_version: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        current = self._catalog.require(automation_id)
        if (
            current.record_version != expected_record_version
            or current.installed_version != current_version
        ):
            raise PluginConflictError(
                "automation instance changed before uninstall",
                code="PLUGIN_INSTANCE_VERSION_CONFLICT",
            )
        result = self._lifecycle.hard_uninstall(
            automation_id,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            expected_current_version=current_version,
            expected_record_version=expected_record_version,
        )
        return {
            "automation_id": result.automation_id,
            "status": result.status.value,
            "purge_id": result.purge_id,
            "pending_cleanup_count": len(result.pending_cleanup_commands),
        }

    def save_configuration(
        self,
        automation_id: str,
        *,
        config: Mapping[str, Any],
        account_bindings: Mapping[str, Any],
        resource_bindings: Mapping[str, Any],
        enabled_entrypoints: tuple[str, ...],
        schedule: Mapping[str, Any],
        device_id: str | None,
        request_id: str,
        expected_project_configuration_version: int,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        record = self._configuration.save(
            automation_id,
            config=config,
            account_bindings=account_bindings,
            resource_bindings=resource_bindings,
            enabled_entrypoints=enabled_entrypoints,
            schedule=schedule,
            device_id=device_id,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            expected_project_configuration_version=(
                expected_project_configuration_version
            ),
        )
        # Saving desired state revokes prior authority immediately.  Reconcile
        # is attempted synchronously, but an unavailable coeffect remains a
        # visible non-runnable target rather than rolling back or using an old
        # account/device.
        reconcile_failed = False
        try:
            self._targets.reconcile_project(automation_id)
        except Exception:  # noqa: BLE001 - desired config already committed fail-closed
            reconcile_failed = True
        projection = self._configuration_projection(record)
        entry = self._catalog.require(automation_id)
        projection.update(
            {
                "target_generation": entry.target_generation,
                "committed_generation": entry.committed_generation,
                "reconcile_state": entry.reconcile_state.value,
                **self._transition_projection(entry, reconcile_failed),
            }
        )
        return projection

    @classmethod
    def _is_committed_ready(cls, entry: PluginCatalogEntry) -> bool:
        try:
            cls._require_committed_ready(entry)
        except PluginConflictError:
            return False
        return True

    @classmethod
    def _transition_projection(
        cls,
        entry: PluginCatalogEntry,
        reconcile_failed: bool = False,
    ) -> dict[str, Any]:
        ready = cls._is_committed_ready(entry)
        blocked = reconcile_failed or entry.reconcile_state in {
            RuntimeReconcileState.WAITING_COEFFECTS,
            RuntimeReconcileState.BLOCKED_UNKNOWN_WRITE,
            RuntimeReconcileState.ERROR,
        }
        return {
            "generation_ready": ready,
            "transition_state": (
                "READY" if ready else "BLOCKED_DEPENDENCY" if blocked else "PREPARING"
            ),
        }

    def package_bytes(
        self,
        plugin_id: str,
        version: str,
        *,
        expected_sha256: str,
    ) -> bytes:
        """Return only the exact original signed package for Worker delivery."""

        record = self._packages.get_package_version(plugin_id, version)
        if record is None:
            raise PluginNotFoundError("automation plugin version is not installed")
        digest = str(expected_sha256 or "").strip().lower()
        if digest != record.package_sha256:
            raise PluginConflictError(
                "requested Worker package digest does not match the installed version",
                code="PLUGIN_PACKAGE_DIGEST_MISMATCH",
            )
        if record.trust_source not in {
            PluginTrustSource.ED25519_FIRST_PARTY,
            PluginTrustSource.ED25519_UPLOAD,
        }:
            raise PluginConflictError(
                "Worker delivery requires an Ed25519-signed package",
                code="PLUGIN_PACKAGE_TRUST_INVALID",
            )
        metadata = dict(record.install_metadata)
        if (
            not record.install_root
            or metadata.get("archive_sha256") != record.package_sha256
            or not isinstance(metadata.get("archive_relative"), str)
        ):
            raise PluginConflictError(
                "installed plugin version has no immutable signed archive",
                code="PLUGIN_PACKAGE_ARCHIVE_MISSING",
            )
        return self._storage.read_verified_archive(
            Path(record.install_root),
            str(metadata["archive_relative"]),
            expected_sha256=record.package_sha256,
        )


__all__ = ["AutomationPluginManagementService"]
