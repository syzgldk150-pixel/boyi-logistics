"""Authenticated application service for plugin lifecycle and project settings."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.automation_plugins.catalog import PluginCatalog, PluginCatalogEntry
from agent.automation_plugins.configuration import AutomationProjectConfigurationService
from agent.automation_plugins.errors import (
    PluginConflictError,
    PluginNotFoundError,
    PluginPackageError,
)
from agent.automation_plugins.first_party import RECOVERABLE_WRITE_PROJECT_PLUGINS
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.manifest_v2 import canonical_json_bytes
from agent.automation_plugins.migration import PluginMigrationControlPlane
from agent.automation_plugins.models import (
    AutomationProjectConfigRecord,
    PluginInstanceRecord,
    PluginProjectState,
    PluginRuntimeModel,
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
_SERVICE_V2_INSTALL_INTENT_FIELDS = frozenset(
    {
        "instance_name",
        "config",
        "account_bindings",
        "resource_bindings",
        "enabled_entrypoints",
        "schedule",
        "permissions_confirmed",
    }
)
_SERVICE_V2_INSTALL_INITIAL_CONFIG_VERSION = 1
_SERVICE_V2_INSTALL_STATE_WORKFLOW = "SERVICE_V2_INSTALL"
_SERVICE_V2_CONTRIBUTION_KINDS = frozenset(
    {"console", "scheduler", "webhook", "feishu", "events"}
)
_MANAGED_CONTRIBUTION_KINDS = frozenset({"console", "scheduler"})
_ACTIVE_CONTRIBUTION_FIELDS = (
    "contribution_id",
    "contribution_kind",
    "generation",
    "phase",
    "backend_status",
)


class MigrationPreparationPersistedError(PluginConflictError):
    """A create request failed after its non-runnable PREPARING hold committed."""

    code = "PLUGIN_MIGRATION_PREPARATION_PENDING"

    def __init__(self, *, migration_pair_id: str, phase: str) -> None:
        super().__init__(
            "migration preparation is durable but target copy is incomplete; "
            "retry the same request_id",
            code=self.code,
        )
        self.migration_pair_id = migration_pair_id
        self.phase = phase


def _iso_datetime(value: object) -> str:
    if not isinstance(value, datetime):
        return ""
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def _projection_field(record: object, field: str) -> object:
    if isinstance(record, Mapping):
        return record.get(field)
    return getattr(record, field, None)


def _contribution_projection(
    state: str,
    active: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "active_contributions": [dict(item) for item in active],
        "contribution_projection_state": state,
    }


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
        contribution_registry: Any | None = None,
    ) -> None:
        self._catalog = catalog
        self._lifecycle = lifecycle
        self._configuration = configuration
        self._workers = worker_repository
        self._targets = target_service
        self._packages = package_repository
        self._migrations = PluginMigrationControlPlane(package_repository)
        self._storage = storage
        self._release_hold_provider = release_hold_provider or (lambda: False)
        self._resource_catalog_provider = resource_catalog_provider
        self._contribution_registry = contribution_registry

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

    def _require_no_open_migration_pair(self, automation_id: str) -> None:
        """Keep ordinary project mutation from bypassing pair ownership.

        Pair transitions are the only path allowed to move future automatic
        entrypoints.  In particular, an ordinary config save/upgrade could
        otherwise prepare a fresh generation whose scheduler materialization
        races the durable CUTOVER or ROLLED_BACK owner.
        """

        finder = getattr(
            self._packages,
            "get_active_plugin_migration_pair_for_automation",
            None,
        )
        if not callable(finder):
            # The production repository always exposes this narrow query.  A
            # legacy in-memory management double has no persisted migration
            # table and therefore cannot own an entrypoint; keep that boundary
            # compatible instead of treating a test-only port as a false pair.
            return
        pair = finder(automation_id)
        if isinstance(pair, Mapping):
            raise PluginConflictError(
                "automation project is owned by an unfinished migration pair",
                code="PLUGIN_MIGRATION_PROJECT_MUTATION_BLOCKED",
            )

    @staticmethod
    def _instance_projection(instance: PluginInstanceRecord) -> dict[str, Any]:
        runtime_model = getattr(
            instance.active_version,
            "runtime_model",
            PluginRuntimeModel.ACTION_V1,
        )
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
            "runtime_model": getattr(runtime_model, "value", str(runtime_model)),
            "plugin_api": getattr(instance.active_version, "plugin_api", "1.0.0"),
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

    @staticmethod
    def _runtime_model_value(value: object) -> str:
        return str(getattr(value, "value", value) or "").strip().upper()

    @staticmethod
    def _safe_entrypoint_projection_is_closed(instance: Mapping[str, Any]) -> bool:
        entrypoints = instance.get("entrypoints")
        enabled = instance.get("enabled_entrypoints")
        kinds = instance.get("entrypoint_kinds")
        if (
            not isinstance(entrypoints, (list, tuple))
            or not isinstance(enabled, (list, tuple))
            or not isinstance(kinds, Mapping)
        ):
            return False
        if any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in (*entrypoints, *enabled)
        ):
            return False
        if (
            len(set(entrypoints)) != len(entrypoints)
            or len(set(enabled)) != len(enabled)
            or not set(enabled) <= set(entrypoints)
            or set(kinds) != set(entrypoints)
        ):
            return False
        return all(
            isinstance(kind, str)
            and kind == kind.strip().lower()
            and kind in _SERVICE_V2_CONTRIBUTION_KINDS
            for kind in kinds.values()
        )

    @staticmethod
    def _entry_managed_contribution_identities(
        entry: PluginCatalogEntry,
    ) -> frozenset[tuple[str, str]] | None:
        try:
            snapshot = entry.committed_snapshot
            raw_contributions = snapshot.execution_metadata["contributions"]
            if (
                not isinstance(raw_contributions, Mapping)
                or set(raw_contributions) != _SERVICE_V2_CONTRIBUTION_KINDS
            ):
                return None
            declared = [
                (kind, declaration["id"])
                for kind in _SERVICE_V2_CONTRIBUTION_KINDS
                for declaration in raw_contributions[kind]
            ]
            enabled = tuple(snapshot.enabled_entrypoints)
        except (AttributeError, KeyError, TypeError):
            return None
        identities = [contribution_id for _kind, contribution_id in declared]
        if any(
            not isinstance(contribution_id, str)
            or not contribution_id
            or contribution_id != contribution_id.strip()
            for contribution_id in identities
        ) or len(set(identities)) != len(identities):
            return None
        if (
            any(
                not isinstance(contribution_id, str)
                or not contribution_id
                or contribution_id != contribution_id.strip()
                for contribution_id in enabled
            )
            or len(set(enabled)) != len(enabled)
            or not set(enabled) <= set(identities)
        ):
            return None
        kinds_by_id = {contribution_id: kind for kind, contribution_id in declared}
        return frozenset(
            (kinds_by_id[contribution_id], contribution_id)
            for contribution_id in enabled
            if kinds_by_id[contribution_id] in _MANAGED_CONTRIBUTION_KINDS
        )

    def _active_contribution_generation(self, automation_id: str) -> int | None:
        provider = getattr(self._contribution_registry, "active_generation", None)
        if not callable(provider):
            raise ValueError("managed contribution registry is incomplete")
        generation = provider(automation_id)
        if generation is None:
            return None
        if type(generation) is not int or generation < 1:
            raise ValueError("managed contribution generation is invalid")
        return generation

    def _registry_active_contribution_projection(
        self,
        automation_id: str,
    ) -> tuple[int | None, list[dict[str, Any]]]:
        registry = self._contribution_registry
        snapshot = getattr(registry, "snapshot", None)
        if not callable(snapshot):
            raise ValueError("managed contribution registry is incomplete")
        generation = self._active_contribution_generation(automation_id)
        records = snapshot()
        if not isinstance(records, (list, tuple)):
            raise ValueError("managed contribution snapshot is invalid")

        active_records = (
            record
            for record in records
            if _projection_field(record, "automation_id") == automation_id
            and _projection_field(record, "generation") == generation
        )
        projected = [
            {
                field: _projection_field(record, field)
                for field in _ACTIVE_CONTRIBUTION_FIELDS
            }
            for record in active_records
        ]
        identities = [
            (item["contribution_kind"], item["contribution_id"])
            for item in projected
        ]
        if (
            any(
                type(item["generation"]) is not int
                or not isinstance(item["contribution_id"], str)
                or not item["contribution_id"]
                or item["contribution_id"] != item["contribution_id"].strip()
                or item["contribution_kind"] not in _MANAGED_CONTRIBUTION_KINDS
                or item["phase"] not in {"PREPARED", "COMMITTED", "DRAINING"}
                or item["backend_status"] not in {"READY", "DISABLED"}
                for item in projected
            )
            or len(set(identities)) != len(identities)
        ):
            raise ValueError("managed contribution projection is invalid")
        projected.sort(
            key=lambda item: (item["contribution_kind"], item["contribution_id"])
        )
        return generation, projected

    def _service_v2_contribution_projection(
        self,
        instance: Mapping[str, Any],
    ) -> dict[str, Any]:
        committed_generation = instance.get("committed_generation")
        if committed_generation is None:
            return _contribution_projection("INACTIVE")
        if (
            isinstance(committed_generation, bool)
            or not isinstance(committed_generation, int)
            or committed_generation < 1
        ):
            return _contribution_projection("STALE")

        automation_id = instance.get("automation_id")
        expected: frozenset[tuple[str, str]] | None = None
        enabled: object = None
        if (
            isinstance(automation_id, str)
            and automation_id
            and self._safe_entrypoint_projection_is_closed(instance)
        ):
            try:
                committed_entry = self._catalog.require(automation_id)
            except Exception:  # noqa: BLE001 - Catalog status must fail closed
                committed_entry = None
            if (
                committed_entry is not None
                and self._runtime_model_value(
                    getattr(committed_entry, "runtime_model", "")
                )
                == PluginRuntimeModel.SERVICE_V2.value
                and committed_entry.committed_generation == committed_generation
            ):
                expected = self._entry_managed_contribution_identities(
                    committed_entry
                )
                enabled = getattr(committed_entry, "enabled", None)
        if self._contribution_registry is None:
            state = (
                "ACTIVE"
                if enabled is True and expected == frozenset()
                else "INACTIVE"
                if enabled is False and expected == frozenset()
                else "STALE"
            )
            return _contribution_projection(state)
        if expected == frozenset():
            state = (
                "ACTIVE"
                if enabled is True
                else "INACTIVE"
                if enabled is False
                else "STALE"
            )
            return _contribution_projection(state)

        try:
            active_generation, active = self._registry_active_contribution_projection(
                str(automation_id or "")
            )
        except Exception:  # noqa: BLE001 - Catalog status must fail closed
            return _contribution_projection("STALE")

        actual = frozenset(
            (item["contribution_kind"], item["contribution_id"])
            for item in active
        )
        all_committed = all(item["phase"] == "COMMITTED" for item in active)
        if expected is None:
            state = "STALE"
        elif enabled is False:
            state = "INACTIVE" if active_generation is None and not active else "STALE"
        elif enabled is not True:
            state = "STALE"
        elif (
            active_generation == committed_generation
            and active
            and all_committed
            and actual == expected
        ):
            state = "ACTIVE"
        else:
            state = "STALE"
        return _contribution_projection(state, active)

    def catalog_projection(self, *, actor: Actor) -> dict[str, Any]:
        self._require_console_actor(actor, super_admin=False)
        projection = self._catalog.safe_projection()
        instances = projection.get("instances")
        if isinstance(instances, list):
            for instance in instances:
                if not isinstance(instance, dict) or self._runtime_model_value(
                    instance.get("runtime_model")
                ) != PluginRuntimeModel.SERVICE_V2.value:
                    continue
                contribution_projection = self._service_v2_contribution_projection(
                    instance
                )
                instance.update(contribution_projection)
                if (
                    contribution_projection["contribution_projection_state"]
                    == "STALE"
                    and instance.get("dependency_state") == "READY"
                ):
                    instance["dependency_state"] = "BLOCKED_DEPENDENCY"
                    reasons = instance.get("blocking_reasons")
                    if not isinstance(reasons, list):
                        reasons = []
                    instance["blocking_reasons"] = [
                        *reasons,
                        {
                            "code": "RUNTIME_PROJECTION_STALE",
                            "service": "",
                            "message": "运行时贡献投影与当前已提交代际不一致",
                        },
                    ]
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
        # The legacy endpoint remains ACTION_V1-compatible, but service-v2
        # packages must go through the wizard endpoint so every configuration
        # and binding decision participates in one durable request identity.
        try:
            self._lifecycle.inspect_service_v2_upload(
                package_bytes,
                transport_package_sha256=transport_package_sha256,
            )
        except PluginPackageError as exc:
            if getattr(exc, "code", "") != "PLUGIN_SERVICE_V2_REQUIRED":
                raise
            # A schema-v1 package is expected at this endpoint.  Its
            # established verifier remains the installation authority.
        else:
            raise PluginConflictError(
                "service-v2 packages require the closed installation wizard intent",
                code="PLUGIN_SERVICE_V2_INSTALL_INTENT_REQUIRED",
            )
        instance = self._lifecycle.install_upload(
            package_bytes,
            instance_name=instance_name,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            transport_package_sha256=transport_package_sha256,
        )
        if instance.active_version.runtime_model is PluginRuntimeModel.SERVICE_V2:
            raise PluginConflictError(
                "plugin runtime classification changed during installation",
                code="PLUGIN_RUNTIME_CLASSIFICATION_DRIFT",
            )
        return self._instance_projection(instance)

    @staticmethod
    def _service_v2_wizard_projection(verified: Any) -> dict[str, Any]:
        """Return only install-wizard material, never raw manifest authority."""

        manifest = verified.manifest
        contributions: list[dict[str, Any]] = []
        default_schedule: dict[str, Any] = {
            "kind": "none",
            "times": [],
            "enabled": False,
        }
        for kind in ("console", "scheduler", "webhook", "feishu", "events"):
            raw_items = manifest.contributes.get(kind, ())
            if not isinstance(raw_items, (list, tuple)):
                raise PluginConflictError(
                    "verified service-v2 contribution projection is invalid",
                    code="PLUGIN_CONTRACT_INVALID",
                )
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    raise PluginConflictError(
                        "verified service-v2 contribution projection is invalid",
                        code="PLUGIN_CONTRACT_INVALID",
                    )
                item = {
                    "id": str(raw.get("id") or ""),
                    "kind": kind,
                    "title": str(raw.get("title") or ""),
                    "default_enabled": raw.get("default_enabled") is True,
                }
                # A default scheduler is verified by the manifest boundary to
                # be representable by this Host's daily-time schedule model.
                if kind == "scheduler" and item["default_enabled"]:
                    raw_schedule = raw.get("schedule")
                    expression = (
                        raw_schedule.get("expression")
                        if isinstance(raw_schedule, Mapping)
                        else None
                    )
                    fields = expression.split() if isinstance(expression, str) else []
                    if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
                        default_schedule = {
                            "kind": "daily_times",
                            "times": [f"{int(fields[1]):02d}:{int(fields[0]):02d}"],
                            "enabled": True,
                        }
                contributions.append(item)
        permissions = []
        for capability in manifest.capabilities:
            if not isinstance(capability, Mapping):
                raise PluginConflictError(
                    "verified service-v2 permission projection is invalid",
                    code="PLUGIN_CONTRACT_INVALID",
                )
            permissions.append(
                {
                    "name": str(capability.get("name") or ""),
                    "operations": list(capability.get("operations") or ()),
                    "account_role": capability.get("account_role"),
                    "resource_role": capability.get("resource_role"),
                }
            )
        return {
            "plugin_id": manifest.plugin_id,
            "name": manifest.name,
            "version": manifest.version,
            "host_api": {
                "minimum": str(manifest.host_api["minimum"]),
                "maximum_exclusive": str(manifest.host_api["maximum_exclusive"]),
            },
            "permissions": permissions,
            "account_roles": [copy.deepcopy(dict(item)) for item in manifest.account_roles],
            "resource_roles": [copy.deepcopy(dict(item)) for item in manifest.resource_roles],
            "config_schema": copy.deepcopy(dict(manifest.config_schema)),
            "contributions": sorted(contributions, key=lambda item: (item["kind"], item["id"])),
            "scheduling": {
                "supported": any(item["kind"] == "scheduler" for item in contributions),
                "default_schedule": default_schedule,
            },
        }

    def inspect_service_v2_upload(
        self,
        package_bytes: bytes,
        *,
        request_id: str,
        transport_package_sha256: str,
        actor: Actor,
    ) -> dict[str, Any]:
        self._require_console_actor(actor, super_admin=True)
        try:
            uuid.UUID(str(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginConflictError(
                "plugin inspection request_id must be UUID",
                code="PLUGIN_INSTALL_INTENT_INVALID",
            ) from exc
        verified = self._lifecycle.inspect_service_v2_upload(
            package_bytes,
            transport_package_sha256=transport_package_sha256,
        )
        return self._service_v2_wizard_projection(verified)

    @staticmethod
    def _parse_service_v2_install_intent(raw_intent: str) -> dict[str, Any]:
        """Decode the closed, bounded wizard payload before any write occurs."""

        try:
            decoded = json.loads(raw_intent)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PluginConflictError(
                "service-v2 installation intent must be JSON",
                code="PLUGIN_INSTALL_INTENT_INVALID",
            ) from exc
        if not isinstance(decoded, Mapping) or set(decoded) != _SERVICE_V2_INSTALL_INTENT_FIELDS:
            forbidden = {"automation_id", "device_id", "manifest", "package_digest", "package_sha256"} & set(
                decoded if isinstance(decoded, Mapping) else ()
            )
            raise PluginConflictError(
                "service-v2 installation intent is not a closed server-owned contract"
                + (f": forbidden={sorted(forbidden)}" if forbidden else ""),
                code="PLUGIN_INSTALL_INTENT_INVALID",
            )
        forbidden_keys = {
            "automation_id",
            "device_id",
            "manifest",
            "manifest_sha256",
            "package_digest",
            "package_sha256",
        }

        def find_forbidden(value: Any) -> set[str]:
            if isinstance(value, Mapping):
                found: set[str] = set()
                for key, item in value.items():
                    key_text = str(key)
                    if key_text in forbidden_keys:
                        found.add(key_text)
                    found.update(find_forbidden(item))
                return found
            if isinstance(value, list):
                return set().union(*(find_forbidden(item) for item in value)) if value else set()
            return set()

        nested_forbidden = find_forbidden(decoded)
        if nested_forbidden:
            raise PluginConflictError(
                "service-v2 installation intent contains browser-owned authority: "
                + ", ".join(sorted(nested_forbidden)),
                code="PLUGIN_INSTALL_INTENT_INVALID",
            )
        instance_name = decoded.get("instance_name")
        if not isinstance(instance_name, str) or not instance_name.strip() or len(instance_name.strip()) > 120:
            raise PluginConflictError(
                "service-v2 instance_name is invalid",
                code="PLUGIN_INSTALL_INTENT_INVALID",
            )
        normalized = {
            "instance_name": instance_name.strip(),
            "config": decoded.get("config"),
            "account_bindings": decoded.get("account_bindings"),
            "resource_bindings": decoded.get("resource_bindings"),
            "enabled_entrypoints": decoded.get("enabled_entrypoints"),
            "schedule": decoded.get("schedule"),
            "permissions_confirmed": decoded.get("permissions_confirmed"),
        }
        if (
            not isinstance(normalized["config"], Mapping)
            or not isinstance(normalized["account_bindings"], Mapping)
            or not isinstance(normalized["resource_bindings"], Mapping)
            or not isinstance(normalized["enabled_entrypoints"], list)
            or not isinstance(normalized["schedule"], Mapping)
            or normalized["permissions_confirmed"] is not True
        ):
            raise PluginConflictError(
                "service-v2 installation intent values are invalid",
                code="PLUGIN_INSTALL_INTENT_INVALID",
            )
        if len(normalized["enabled_entrypoints"]) > 64 or any(
            not isinstance(value, str) or not value.strip() or len(value) > 128
            for value in normalized["enabled_entrypoints"]
        ):
            raise PluginConflictError(
                "service-v2 enabled entrypoints are invalid",
                code="PLUGIN_INSTALL_INTENT_INVALID",
            )
        return {
            "instance_name": normalized["instance_name"],
            "config": copy.deepcopy(dict(normalized["config"])),
            "account_bindings": copy.deepcopy(dict(normalized["account_bindings"])),
            "resource_bindings": copy.deepcopy(dict(normalized["resource_bindings"])),
            "enabled_entrypoints": tuple(
                str(value).strip() for value in normalized["enabled_entrypoints"]
            ),
            "schedule": copy.deepcopy(dict(normalized["schedule"])),
            "permissions_confirmed": True,
        }

    def install_service_v2(
        self,
        package_bytes: bytes,
        *,
        request_id: str,
        transport_package_sha256: str,
        raw_intent: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """Install a v2 project only through its complete, replayable intent."""

        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        intent = self._parse_service_v2_install_intent(raw_intent)
        verified = self._lifecycle.inspect_service_v2_upload(
            package_bytes,
            transport_package_sha256=transport_package_sha256,
        )
        intent_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "actor_id": actor.actor_id,
                    "package_sha256": verified.package_sha256,
                    "intent": intent,
                }
            )
        ).hexdigest()
        instance = self._lifecycle.install_upload(
            package_bytes,
            instance_name=str(intent["instance_name"]),
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            transport_package_sha256=transport_package_sha256,
            install_payload_sha256=intent_sha256,
        )
        entry = self._catalog.require(instance.automation_id)
        # Always ask the audited configuration transaction to prove the exact
        # deterministic child request.  Looking only at ``configured`` would
        # accept a later administrator edit as the original install result.
        configure_request_id = str(
            uuid.uuid5(uuid.UUID(request_id), "service-v2-initial-config")
        )
        self._configuration.save(
            instance.automation_id,
            config=intent["config"],
            account_bindings=intent["account_bindings"],
            resource_bindings=intent["resource_bindings"],
            enabled_entrypoints=intent["enabled_entrypoints"],
            schedule=intent["schedule"],
            device_id=None,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=configure_request_id,
            expected_project_configuration_version=(
                _SERVICE_V2_INSTALL_INITIAL_CONFIG_VERSION
            ),
        )
        reconcile_failed = False
        try:
            self._targets.reconcile_project(instance.automation_id)
        except Exception:  # noqa: BLE001 - durable desired state remains disabled
            reconcile_failed = True
        entry = self._catalog.require(instance.automation_id)
        if not reconcile_failed:
            self._retry_v2_consumers_after_provider_change(instance.automation_id)
            # Re-read after Provider consumers may have changed the local
            # projection.  A high-level enable performs its own final CAS and
            # committed-generation check; no lifecycle low-level enable here.
            entry = self._catalog.require(instance.automation_id)
            if entry.configured:
                self._require_committed_ready(entry)
                try:
                    enable_base_record_version = (
                        self._lifecycle.claim_service_v2_install_enable_base(
                            instance.automation_id,
                            root_request_id=request_id,
                            install_payload_sha256=intent_sha256,
                            configuration_request_id=configure_request_id,
                            actor_id=actor.actor_id,
                            actor_role=role,
                        )
                    )
                except (ConcurrentUpdateError, IdempotencyConflict) as exc:
                    raise PluginConflictError(
                        "service-v2 install enable baseline changed or is incomplete",
                        code="PLUGIN_INSTALL_PROGRESS_CONFLICT",
                    ) from exc
                enable_request_id, enable_expected_version, enable_context = (
                    self._service_v2_install_enable_claim(
                        entry,
                        base_record_version=enable_base_record_version,
                        root_request_id=request_id,
                        install_payload_sha256=intent_sha256,
                        actor_id=actor.actor_id,
                        actor_role=role,
                    )
                )
                self.set_enabled(
                    instance.automation_id,
                    enabled=True,
                    request_id=enable_request_id,
                    expected_record_version=enable_expected_version,
                    actor=actor,
                    _state_change_context=enable_context,
                )
                entry = self._catalog.require(instance.automation_id)
        projection = self._catalog_instance_projection(entry)
        projection.update(self._transition_projection(entry, reconcile_failed))
        return projection

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
        self._require_no_open_migration_pair(automation_id)
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
        if not reconcile_failed:
            self._retry_v2_consumers_after_provider_change(automation_id)
        projection = self._catalog_instance_projection(refreshed)
        projection.update(self._transition_projection(refreshed, reconcile_failed))
        return projection

    @staticmethod
    def _service_v2_install_enable_request_id(
        root_request_id: str,
        attempt: int,
    ) -> str:
        label = (
            "service-v2-auto-enable"
            if attempt == 1
            else f"service-v2-auto-enable:{attempt}"
        )
        return str(uuid.uuid5(uuid.UUID(root_request_id), label))

    @staticmethod
    def _service_v2_install_state_context(
        *,
        root_request_id: str,
        install_payload_sha256: str,
        attempt: int,
        phase: str,
    ) -> dict[str, object]:
        return {
            "workflow": _SERVICE_V2_INSTALL_STATE_WORKFLOW,
            "root_request_id": root_request_id,
            "install_payload_sha256": install_payload_sha256,
            "attempt": attempt,
            "phase": phase,
        }

    def _require_service_v2_install_state_witness(
        self,
        automation_id: str,
        *,
        request_id: str,
        enabled: bool,
        expected_record_version: int,
        actor_id: str,
        actor_role: str,
        state_change_context: Mapping[str, object],
    ) -> None:
        reader = getattr(self._lifecycle, "state_change_witness", None)
        if not callable(reader):
            raise PluginConflictError(
                "service-v2 install audit witness is unavailable",
                code="PLUGIN_INSTALL_PROGRESS_UNAVAILABLE",
            )
        witness = reader(automation_id, request_id=request_id)
        expected = {
            "enabled": enabled,
            "expected_record_version": expected_record_version,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "state_change_context": dict(state_change_context),
        }
        if not isinstance(witness, Mapping) or dict(witness) != expected:
            raise PluginConflictError(
                "service-v2 install state history changed or is incomplete",
                code="PLUGIN_INSTALL_PROGRESS_CONFLICT",
            )

    def _service_v2_install_enable_claim(
        self,
        entry: PluginCatalogEntry,
        *,
        base_record_version: int,
        root_request_id: str,
        install_payload_sha256: str,
        actor_id: str,
        actor_role: str,
    ) -> tuple[str, int, dict[str, object]]:
        """Prove the exact enable/compensation chain for one root request."""

        current_version = entry.record_version
        if (
            isinstance(base_record_version, bool)
            or not isinstance(base_record_version, int)
            or base_record_version < 1
            or isinstance(current_version, bool)
            or not isinstance(current_version, int)
            or current_version < base_record_version
        ):
            raise PluginConflictError(
                "service-v2 install record version is invalid",
                code="PLUGIN_INSTALL_PROGRESS_CONFLICT",
            )
        attempt = 1
        expected_version = base_record_version
        while current_version >= expected_version + 2:
            enable_request_id = self._service_v2_install_enable_request_id(
                root_request_id,
                attempt,
            )
            enable_context = self._service_v2_install_state_context(
                root_request_id=root_request_id,
                install_payload_sha256=install_payload_sha256,
                attempt=attempt,
                phase="enable",
            )
            rollback_request_id = str(
                uuid.uuid5(
                    uuid.UUID(enable_request_id),
                    "post-reconcile-rollback-disable",
                )
            )
            rollback_context = self._service_v2_install_state_context(
                root_request_id=root_request_id,
                install_payload_sha256=install_payload_sha256,
                attempt=attempt,
                phase="rollback",
            )
            self._require_service_v2_install_state_witness(
                entry.automation_id,
                request_id=enable_request_id,
                enabled=True,
                expected_record_version=expected_version,
                actor_id=actor_id,
                actor_role=actor_role,
                state_change_context=enable_context,
            )
            self._require_service_v2_install_state_witness(
                entry.automation_id,
                request_id=rollback_request_id,
                enabled=False,
                expected_record_version=expected_version + 1,
                actor_id=actor_id,
                actor_role=actor_role,
                state_change_context=rollback_context,
            )
            expected_version += 2
            attempt += 1

        if current_version not in {expected_version, expected_version + 1}:
            raise PluginConflictError(
                "service-v2 install state history is not contiguous",
                code="PLUGIN_INSTALL_PROGRESS_CONFLICT",
            )
        projected_state = getattr(entry.state, "value", entry.state)
        if current_version == expected_version:
            if bool(entry.enabled) or projected_state == PluginProjectState.ENABLED.value:
                raise PluginConflictError(
                    "service-v2 install state does not match its audit history",
                    code="PLUGIN_INSTALL_PROGRESS_CONFLICT",
                )
        elif not bool(entry.enabled) or projected_state != PluginProjectState.ENABLED.value:
            raise PluginConflictError(
                "service-v2 install enable result drifted after its audit event",
                code="PLUGIN_INSTALL_PROGRESS_CONFLICT",
            )
        request_id = self._service_v2_install_enable_request_id(
            root_request_id,
            attempt,
        )
        context = self._service_v2_install_state_context(
            root_request_id=root_request_id,
            install_payload_sha256=install_payload_sha256,
            attempt=attempt,
            phase="enable",
        )
        return request_id, expected_version, context

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
            "runtime_model": getattr(
                entry,
                "runtime_model",
                PluginRuntimeModel.ACTION_V1.value,
            ),
            "plugin_api": getattr(entry, "plugin_api", "1.0.0"),
            "active_runtime_model": getattr(
                entry,
                "active_runtime_model",
                PluginRuntimeModel.ACTION_V1.value,
            ),
            "active_version": getattr(
                entry,
                "active_version",
                entry.installed_version,
            ),
        }

    def _require_committed_ready(self, entry: PluginCatalogEntry) -> None:
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
        if (
            self._contribution_registry is None
            or self._runtime_model_value(getattr(entry, "runtime_model", ""))
            != PluginRuntimeModel.SERVICE_V2.value
        ):
            return
        managed = self._entry_managed_contribution_identities(entry)
        if managed is None:
            raise PluginConflictError(
                "the committed contribution projection is unavailable",
                code="PLUGIN_GENERATION_NOT_READY",
            )
        if not managed:
            return
        try:
            active_generation = self._active_contribution_generation(
                entry.automation_id
            )
        except Exception as exc:
            raise PluginConflictError(
                "the committed contribution projection is unavailable",
                code="PLUGIN_GENERATION_NOT_READY",
            ) from exc
        if active_generation != entry.committed_generation:
            raise PluginConflictError(
                "the committed contribution projection is not active",
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
        _state_change_context: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        self._require_no_open_migration_pair(automation_id)
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
            affected_consumers = (
                self._suspend_v2_provider_consumers(entry) if not enabled else ()
            )
            try:
                instance = self._lifecycle.set_enabled(
                    automation_id,
                    enabled=enabled,
                    actor_id=actor.actor_id,
                    actor_role=role,
                    request_id=request_id,
                    expected_record_version=expected_record_version,
                    state_change_context=_state_change_context,
                )
            except (ConcurrentUpdateError, IdempotencyConflict) as exc:
                raise PluginConflictError(
                    "automation instance version changed before state update",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                ) from exc
            return self._finish_enabled_state_change(
                entry,
                instance=instance,
                enabled=enabled,
                request_id=request_id,
                expected_record_version=expected_record_version,
                actor=actor,
                actor_role=role,
                affected_consumers=affected_consumers,
                state_change_context=_state_change_context,
            )
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
            if entry.record_version != expected_record_version:
                raise PluginConflictError(
                    "automation instance version changed while preparing enable",
                    code="PLUGIN_INSTANCE_VERSION_CONFLICT",
                )
            self._require_committed_ready(entry)
        affected_consumers = (
            self._suspend_v2_provider_consumers(entry) if not enabled else ()
        )
        instance = self._lifecycle.set_enabled(
            automation_id,
            enabled=enabled,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            expected_record_version=expected_record_version,
            state_change_context=_state_change_context,
        )
        return self._finish_enabled_state_change(
            entry,
            instance=instance,
            enabled=enabled,
            request_id=request_id,
            expected_record_version=expected_record_version,
            actor=actor,
            actor_role=role,
            affected_consumers=affected_consumers,
            state_change_context=_state_change_context,
        )

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
        self._require_no_open_migration_pair(automation_id)
        migration_uninstall_allowed = getattr(
            self._packages, "source_project_migration_uninstall_allowed", None
        )
        if callable(migration_uninstall_allowed) and not migration_uninstall_allowed(
            automation_id
        ):
            raise PluginConflictError(
                "complete every migration pair before uninstalling its v1 source",
                code="PLUGIN_MIGRATION_SOURCE_UNINSTALL_BLOCKED",
            )
        current = self._catalog.require(automation_id)
        if (
            current.record_version != expected_record_version
            or current.installed_version != current_version
        ):
            raise PluginConflictError(
                "automation instance changed before uninstall",
                code="PLUGIN_INSTANCE_VERSION_CONFLICT",
            )
        affected_consumers = self._suspend_v2_provider_consumers(current)
        result = self._lifecycle.hard_uninstall(
            automation_id,
            actor_id=actor.actor_id,
            actor_role=role,
            request_id=request_id,
            expected_current_version=current_version,
            expected_record_version=expected_record_version,
            before_finalize=lambda project_id: self._reconcile_before_uninstall(
                project_id,
                provided_services=tuple(getattr(current, "provided_services", ())),
                affected_consumers=affected_consumers,
            ),
        )
        return {
            "automation_id": result.automation_id,
            "status": result.status.value,
            "purge_id": result.purge_id,
            "pending_cleanup_count": len(result.pending_cleanup_commands),
        }

    def _reconcile_before_uninstall(
        self,
        automation_id: str,
        *,
        provided_services: Sequence[str] = (),
        affected_consumers: Sequence[str] = (),
    ) -> object:
        """Dispose every revoked generation before the purge can delete state.

        Uninstall preparation changes the project and all of its generations to
        a revoked/draining state atomically.  A normal project reconciliation
        then removes service registrations, routes and subprocess effects.  A
        missing target service is an explicit failure: finalizing the purge
        first could leave an orphaned service or process with no durable owner.
        """

        reconciler = getattr(self._targets, "reconcile_project", None)
        if not callable(reconciler):
            raise PluginConflictError(
                "plugin uninstall runtime reconciler is unavailable",
                code="PLUGIN_UNINSTALL_RECONCILE_UNAVAILABLE",
            )
        if provided_services:
            reconcile_tree = getattr(
                self._targets,
                "reconcile_provider_dependency_tree",
                None,
            )
            if not callable(reconcile_tree):
                raise PluginConflictError(
                    "plugin Provider dependency reconciler is unavailable",
                    code="PLUGIN_CONSUMER_RECONCILE_UNAVAILABLE",
                )
            return reconcile_tree(
                automation_id,
                provider_services=provided_services,
                enabled=False,
                consumer_automation_ids=affected_consumers,
            )
        return reconciler(automation_id)

    def permanently_clear_data(
        self,
        automation_id: str,
        *,
        request_id: str,
        reason: str,
        actor: Actor,
    ) -> dict[str, Any]:
        """Clear retained v2 data only after a separate super-admin action."""

        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        try:
            uuid.UUID(str(request_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise PluginConflictError(
                "plugin data purge request_id must be UUID",
                code="PLUGIN_DATA_PURGE_REQUEST_INVALID",
            ) from exc
        safe_reason = str(reason or "").strip()
        if not safe_reason or len(safe_reason) > 500:
            raise PluginConflictError(
                "plugin data purge requires a bounded reason",
                code="PLUGIN_DATA_PURGE_REASON_REQUIRED",
            )
        try:
            self._catalog.require(automation_id)
        except PluginNotFoundError:
            pass
        else:
            raise PluginConflictError(
                "uninstall the automation project before permanently clearing data",
                code="PLUGIN_DATA_PURGE_REQUIRES_UNINSTALL",
            )
        clearer = getattr(self._packages, "permanently_clear_plugin_documents", None)
        if not callable(clearer):
            raise PluginConflictError(
                "managed plugin data purge is unavailable",
                code="PLUGIN_DATA_PURGE_UNAVAILABLE",
            )
        result = clearer(
            automation_id,
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role=role,
            reason=safe_reason,
        )
        if not isinstance(result, Mapping):
            raise PluginConflictError(
                "managed plugin data purge returned invalid evidence",
                code="PLUGIN_DATA_PURGE_UNAVAILABLE",
            )
        return {
            "automation_id": str(result.get("automation_id") or automation_id),
            "cleared_count": int(result.get("cleared_count") or 0),
            "already_cleared": bool(result.get("already_cleared")),
        }

    def migration_pair(self, migration_pair_id: str, *, actor: Actor) -> dict[str, Any]:
        self._require_console_actor(actor, super_admin=True)
        return self._migrations.get_pair(migration_pair_id)

    @staticmethod
    def _migration_role_signature(role: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        """Compare role contracts without guessing a role name or argument key."""

        ignored = {"role", "argument_field"}
        normalized: list[tuple[str, str]] = []
        for key, value in role.items():
            if key in ignored:
                continue
            if isinstance(value, list):
                normalized_value = repr(tuple(sorted(repr(item) for item in value)))
            else:
                normalized_value = repr(value)
            normalized.append((str(key), normalized_value))
        return tuple(sorted(normalized))

    @classmethod
    def _map_migration_bindings(
        cls,
        *,
        source_bindings: Mapping[str, Any],
        source_roles: Sequence[Mapping[str, Any]],
        target_roles: Sequence[Mapping[str, Any]],
        kind: str,
    ) -> dict[str, Any]:
        """Map only one-to-one role-equivalent bindings across runtimes."""

        source_by_name = {
            str(role.get("role") or ""): role
            for role in source_roles
            if isinstance(role, Mapping) and str(role.get("role") or "")
        }
        if len(source_by_name) != len(source_roles) or not set(source_bindings) <= set(
            source_by_name
        ):
            raise PluginConflictError(
                f"migration source {kind} roles are not closed",
                code="PLUGIN_MIGRATION_BINDING_MAPPING_UNAVAILABLE",
            )
        result: dict[str, Any] = {}
        consumed: set[str] = set()
        for target_role in target_roles:
            if not isinstance(target_role, Mapping):
                raise PluginConflictError(
                    f"migration target {kind} role is invalid",
                    code="PLUGIN_MIGRATION_BINDING_MAPPING_UNAVAILABLE",
                )
            target_name = str(target_role.get("role") or "")
            candidates = [
                source_name
                for source_name, source_role in source_by_name.items()
                if cls._migration_role_signature(source_role)
                == cls._migration_role_signature(target_role)
            ]
            if len(candidates) != 1:
                raise PluginConflictError(
                    f"migration {kind} role cannot be uniquely mapped",
                    code="PLUGIN_MIGRATION_BINDING_MAPPING_UNAVAILABLE",
                )
            source_name = candidates[0]
            if source_name in source_bindings:
                result[target_name] = copy.deepcopy(source_bindings[source_name])
                consumed.add(source_name)
            elif target_role.get("required") is True:
                raise PluginConflictError(
                    f"migration required {kind} binding is missing",
                    code="PLUGIN_MIGRATION_BINDING_MAPPING_UNAVAILABLE",
                )
        if consumed != set(source_bindings):
            raise PluginConflictError(
                f"migration source {kind} binding has no target role",
                code="PLUGIN_MIGRATION_BINDING_MAPPING_UNAVAILABLE",
            )
        return result

    @staticmethod
    def _migration_target_entrypoints(
        entry: PluginCatalogEntry,
        schedule: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Choose the sole Console validation and Scheduler owner routes."""

        console = [
            str(item.get("id") or "")
            for item in entry.contributions.get("console", ())
            if isinstance(item, Mapping)
        ]
        if len(console) != 1 or not console[0]:
            raise PluginConflictError(
                "migration target must declare exactly one Console entrypoint",
                code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
            )
        entrypoints = list(console)
        if schedule.get("kind") != "none":
            schedulers = [
                str(item.get("id") or "")
                for item in entry.contributions.get("scheduler", ())
                if isinstance(item, Mapping)
            ]
            if len(schedulers) != 1 or not schedulers[0]:
                raise PluginConflictError(
                    "migration target must declare exactly one scheduler entrypoint",
                    code="PLUGIN_MIGRATION_ENTRYPOINT_MAPPING_UNAVAILABLE",
                )
            entrypoints.extend(schedulers)
        return tuple(entrypoints)

    def create_migration_pair(
        self,
        *,
        migration_pair_id: str,
        source_automation_id: str,
        target_automation_id: str,
        business_key_fields: tuple[str, ...],
        business_key_namespace: str | None,
        request_id: str,
        reason: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        source = self._catalog.require(source_automation_id)
        target = self._catalog.require(target_automation_id)
        if (
            getattr(source, "runtime_model", PluginRuntimeModel.ACTION_V1.value)
            != PluginRuntimeModel.ACTION_V1.value
            or getattr(target, "runtime_model", PluginRuntimeModel.ACTION_V1.value)
            != PluginRuntimeModel.SERVICE_V2.value
        ):
            raise PluginConflictError(
                "migration pair must bind ACTION_V1 to SERVICE_V2",
                code="PLUGIN_MIGRATION_RUNTIME_MODEL_INVALID",
            )
        source_record = self._configuration.read(source_automation_id)
        target_entrypoints = self._migration_target_entrypoints(
            target,
            source_record.schedule,
        )
        copied_accounts = self._map_migration_bindings(
            source_bindings=source_record.account_bindings,
            source_roles=source.account_roles,
            target_roles=target.account_roles,
            kind="account",
        )
        copied_resources = self._map_migration_bindings(
            source_bindings=source_record.resource_bindings,
            source_roles=source.resource_roles,
            target_roles=target.resource_roles,
            kind="resource",
        )
        # Persist the ownership gate *before* saving the target's copied
        # scheduler intent.  If the copy/finalize step crashes, PREPARING is
        # durable, the target's physical task stays disabled, and replaying
        # this exact request continues instead of creating a second pair.
        result = self._migrations.begin_pair_preparation(
            migration_pair_id=migration_pair_id,
            source_automation_id=source_automation_id,
            target_automation_id=target_automation_id,
            business_key_fields=business_key_fields,
            business_key_namespace=business_key_namespace,
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role=role,
            reason=reason,
        )
        if result.get("state") != "PREPARING":
            return self._migration_pair_copy_projection(
                result,
                source_automation_id=source_automation_id,
                target_automation_id=target_automation_id,
                source_record=source_record,
                target_entrypoints=target_entrypoints,
            )
        # Read the source once more only after the durable ownership gate is
        # in place.  Ordinary configuration mutation is now rejected for both
        # sides, so this is the exact source snapshot copied into the new v2
        # project rather than a pre-hold observation that could have raced a
        # last legacy configuration save.
        source_record = self._configuration.read(source_automation_id)
        target_entrypoints = self._migration_target_entrypoints(
            target,
            source_record.schedule,
        )
        copied_accounts = self._map_migration_bindings(
            source_bindings=source_record.account_bindings,
            source_roles=source.account_roles,
            target_roles=target.account_roles,
            kind="account",
        )
        copied_resources = self._map_migration_bindings(
            source_bindings=source_record.resource_bindings,
            source_roles=source.resource_roles,
            target_roles=target.resource_roles,
            kind="resource",
        )
        copy_request_id = str(uuid.uuid5(uuid.UUID(request_id), "migration-target-copy"))
        try:
            self._configuration.save(
                target_automation_id,
                config=copy.deepcopy(source_record.config),
                account_bindings=copied_accounts,
                resource_bindings=copied_resources,
                enabled_entrypoints=target_entrypoints,
                schedule=copy.deepcopy(source_record.schedule),
                device_id=None,
                actor_id=actor.actor_id,
                actor_role=role,
                request_id=copy_request_id,
                expected_project_configuration_version=target.project_config_version,
            )
        except Exception as exc:  # durable PREPARING now requires same-request replay
            raise MigrationPreparationPersistedError(
                migration_pair_id=migration_pair_id,
                phase="TARGET_COPY",
            ) from exc
        try:
            result = self._migrations.finalize_pair_preparation(
                migration_pair_id,
                request_id=str(uuid.uuid5(uuid.UUID(request_id), "migration-pair-finalize")),
                actor_id=actor.actor_id,
                actor_role=role,
                reason=reason,
            )
        except Exception as exc:  # target config is staged but not yet immutable TESTING
            raise MigrationPreparationPersistedError(
                migration_pair_id=migration_pair_id,
                phase="FINALIZE_TESTING",
            ) from exc
        # The pair is durable before target preparation begins, so the
        # generation-side scheduler gate can only materialize a disabled v2
        # task during manual verification.  A coeffect failure leaves TESTING
        # durable and explicitly non-runnable; the normal reconciler retries.
        try:
            self._targets.reconcile_project(target_automation_id)
        except Exception:  # noqa: BLE001 - copied desired state is durable and safe
            result = {**result, "target_preparation_state": "PREPARING"}
        else:
            refreshed_target = self._catalog.require(target_automation_id)
            prepared = (
                refreshed_target.committed_generation
                == refreshed_target.target_generation
                and refreshed_target.reconcile_state is RuntimeReconcileState.STABLE
            )
            result = {
                **result,
                "target_preparation_state": "PREPARED" if prepared else "PREPARING",
            }
        return self._migration_pair_copy_projection(
            result,
            source_automation_id=source_automation_id,
            target_automation_id=target_automation_id,
            source_record=source_record,
            target_entrypoints=target_entrypoints,
        )

    @staticmethod
    def _migration_pair_copy_projection(
        result: Mapping[str, Any],
        *,
        source_automation_id: str,
        target_automation_id: str,
        source_record: AutomationProjectConfigRecord,
        target_entrypoints: Sequence[str],
    ) -> dict[str, Any]:
        projection = dict(result)
        projection["copied_configuration"] = {
            "source_automation_id": source_automation_id,
            "target_automation_id": target_automation_id,
            "source_config_version": source_record.config_version,
            "target_entrypoints": list(target_entrypoints),
            "schedule": copy.deepcopy(source_record.schedule),
        }
        return projection

    def mark_migration_ready(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        reason: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        return self._migrations.mark_ready(
            migration_pair_id,
            expected_record_version=expected_record_version,
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role=role,
            reason=reason,
        )

    def cutover_migration_pair(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        reason: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        return self._migrations.cutover(
            migration_pair_id,
            expected_record_version=expected_record_version,
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role=role,
            reason=reason,
        )

    def rollback_migration_pair(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        reason: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        return self._migrations.rollback(
            migration_pair_id,
            expected_record_version=expected_record_version,
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role=role,
            reason=reason,
        )

    def complete_migration_pair(
        self,
        migration_pair_id: str,
        *,
        expected_record_version: int,
        request_id: str,
        reason: str,
        actor: Actor,
    ) -> dict[str, Any]:
        role = self._require_console_actor(actor, super_admin=True)
        self._require_mutation_allowed()
        return self._migrations.complete(
            migration_pair_id,
            expected_record_version=expected_record_version,
            request_id=request_id,
            actor_id=actor.actor_id,
            actor_role=role,
            reason=reason,
        )

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
        self._require_no_open_migration_pair(automation_id)
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
        if not reconcile_failed:
            self._retry_v2_consumers_after_provider_change(automation_id)
        entry = self._catalog.require(automation_id)
        if (
            not reconcile_failed
            and self._is_committed_ready(entry)
            and getattr(entry, "runtime_model", PluginRuntimeModel.ACTION_V1.value)
            == PluginRuntimeModel.SERVICE_V2.value
            and entry.configured
            and entry.current_enabled_entrypoints
            and not entry.enabled
        ):
            # The configuration write is durable before reconcile.  Only a
            # committed-ready v2 generation may cross the high-level enable
            # boundary; a failed reconcile must leave the project disabled.
            self.set_enabled(
                automation_id,
                enabled=True,
                request_id=str(
                    uuid.uuid5(uuid.UUID(request_id), "service-v2-auto-enable")
                ),
                expected_record_version=entry.record_version,
                actor=actor,
            )
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

    def _reconcile_v2_after_enabled_change(
        self,
        entry: PluginCatalogEntry,
        *,
        enabled: bool,
        affected_consumers: Sequence[str] = (),
    ) -> None:
        """Apply a v2 enablement change to effects before returning control."""

        if (
            getattr(entry, "runtime_model", PluginRuntimeModel.ACTION_V1.value)
            != PluginRuntimeModel.SERVICE_V2.value
        ):
            return
        provided_services = tuple(getattr(entry, "provided_services", ()))
        if provided_services:
            reconcile_tree = getattr(
                self._targets,
                "reconcile_provider_dependency_tree",
                None,
            )
            if not callable(reconcile_tree):
                raise PluginConflictError(
                    "plugin Provider dependency reconciler is unavailable",
                    code="PLUGIN_CONSUMER_RECONCILE_UNAVAILABLE",
                )
            reconcile_tree(
                entry.automation_id,
                provider_services=provided_services,
                enabled=enabled,
                consumer_automation_ids=affected_consumers or None,
            )
            return
        reconcile_project = getattr(self._targets, "reconcile_project", None)
        if not callable(reconcile_project):
            raise PluginConflictError(
                "plugin runtime reconciler is unavailable after state change",
                code="PLUGIN_RUNTIME_RECONCILE_UNAVAILABLE",
            )
        reconcile_project(entry.automation_id)

    def _finish_enabled_state_change(
        self,
        entry: PluginCatalogEntry,
        *,
        instance: PluginInstanceRecord,
        enabled: bool,
        request_id: str,
        expected_record_version: int,
        actor: Actor,
        actor_role: str,
        affected_consumers: Sequence[str],
        state_change_context: Mapping[str, object] | None,
    ) -> dict[str, Any]:
        """Reconcile v2 effects and compensate a failed enable to disabled."""

        try:
            self._reconcile_v2_after_enabled_change(
                entry,
                enabled=enabled,
                affected_consumers=affected_consumers,
            )
        except Exception as reconcile_error:
            if not enabled:
                if (
                    isinstance(reconcile_error, PluginConflictError)
                    and reconcile_error.code
                    == "RUNTIME_PROJECTION_REFRESH_FAILED"
                ):
                    projection = self._instance_projection(instance)
                    projection.update(
                        {
                            "generation_ready": False,
                            "transition_state": "BLOCKED_DEPENDENCY",
                            "contribution_projection_state": "STALE",
                            "runtime_projection_pending": True,
                        }
                    )
                    return projection
                raise
            persisted = self._catalog.require(entry.automation_id)
            persisted_state = getattr(persisted.state, "value", persisted.state)
            if (
                persisted.record_version != expected_record_version + 1
                or not bool(persisted.enabled)
                or persisted_state != PluginProjectState.ENABLED.value
            ):
                raise PluginConflictError(
                    "failed enable cannot be compensated from an exact committed state",
                    code="PLUGIN_ENABLE_ROLLBACK_CONFLICT",
                ) from reconcile_error
            rollback_request_id = str(
                uuid.uuid5(
                    uuid.UUID(request_id),
                    "post-reconcile-rollback-disable",
                )
            )
            rollback_context: dict[str, object]
            if (
                isinstance(state_change_context, Mapping)
                and state_change_context.get("workflow")
                == _SERVICE_V2_INSTALL_STATE_WORKFLOW
                and state_change_context.get("phase") == "enable"
            ):
                rollback_context = {
                    **dict(state_change_context),
                    "phase": "rollback",
                }
            else:
                rollback_context = {
                    "workflow": "PLUGIN_STATE_RECONCILE_ROLLBACK",
                    "root_request_id": request_id,
                    "phase": "rollback",
                }
            try:
                rollback_consumers = self._suspend_v2_provider_consumers(persisted)
                self._lifecycle.set_enabled(
                    entry.automation_id,
                    enabled=False,
                    actor_id=actor.actor_id,
                    actor_role=actor_role,
                    request_id=rollback_request_id,
                    expected_record_version=expected_record_version + 1,
                    state_change_context=rollback_context,
                )
            except Exception as rollback_error:
                raise PluginConflictError(
                    "service-v2 enable failed and its disabled rollback did not commit",
                    code="PLUGIN_ENABLE_ROLLBACK_FAILED",
                ) from rollback_error
            try:
                self._reconcile_v2_after_enabled_change(
                    persisted,
                    enabled=False,
                    affected_consumers=rollback_consumers,
                )
            except Exception as rollback_reconcile_error:
                raise PluginConflictError(
                    "service-v2 enable was rolled back but disabled effects still need recovery",
                    code="PLUGIN_ENABLE_ROLLBACK_RECONCILE_FAILED",
                ) from rollback_reconcile_error
            raise PluginConflictError(
                "service-v2 enable effects failed and the project was restored disabled",
                code="PLUGIN_ENABLE_RECONCILE_FAILED",
            ) from reconcile_error
        return self._instance_projection(instance)

    def _suspend_v2_provider_consumers(
        self,
        entry: PluginCatalogEntry,
    ) -> tuple[str, ...]:
        if (
            getattr(entry, "runtime_model", PluginRuntimeModel.ACTION_V1.value)
            != PluginRuntimeModel.SERVICE_V2.value
        ):
            return ()
        provided_services = tuple(getattr(entry, "provided_services", ()))
        if not provided_services:
            return ()
        suspend = getattr(self._targets, "suspend_provider_consumers", None)
        if not callable(suspend):
            raise PluginConflictError(
                "plugin consumer scheduler gate is unavailable",
                code="PLUGIN_CONSUMER_SCHEDULER_GATE_UNAVAILABLE",
            )
        consumers = suspend(
            entry.automation_id,
            provider_services=provided_services,
        )
        if not isinstance(consumers, Sequence) or isinstance(consumers, (str, bytes)):
            raise PluginConflictError(
                "plugin consumer scheduler gate returned invalid evidence",
                code="PLUGIN_CONSUMER_SCHEDULER_GATE_INVALID",
            )
        normalized = tuple(str(item).strip() for item in consumers)
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            raise PluginConflictError(
                "plugin consumer scheduler gate returned invalid evidence",
                code="PLUGIN_CONSUMER_SCHEDULER_GATE_INVALID",
            )
        return normalized

    def _retry_v2_consumers_after_provider_change(
        self,
        automation_id: str,
        *,
        strict: bool = False,
        provider_services: Sequence[str] | None = None,
    ) -> None:
        """Wake all waiting v2 consumers when this project may provide a service.

        Service registration is a coeffect shared by otherwise independent
        projects.  Re-running only the changed Provider leaves consumers in
        ``WAITING_COEFFECTS`` indefinitely, so a successful Provider/config
        reconcile schedules a bounded all-project pass.  The mutation that
        made the Provider durable is never rolled back if that best-effort
        pass encounters an unrelated project failure; the target service
        records such failures for the next health/reconcile cycle.
        """

        if provider_services is None:
            entry = self._catalog.require(automation_id)
            provider_services = tuple(getattr(entry, "provided_services", ()))
            runtime_model = getattr(
                entry, "runtime_model", PluginRuntimeModel.ACTION_V1.value
            )
        else:
            runtime_model = PluginRuntimeModel.SERVICE_V2.value
        if runtime_model != PluginRuntimeModel.SERVICE_V2.value or not provider_services:
            return
        retry_all = getattr(self._targets, "reconcile_all", None)
        if not callable(retry_all):
            if strict:
                raise PluginConflictError(
                    "plugin consumer reconciler is unavailable",
                    code="PLUGIN_CONSUMER_RECONCILE_UNAVAILABLE",
                )
            return
        if strict:
            retry_all()
            return
        try:
            retry_all()
        except Exception:  # noqa: BLE001 - desired Provider state is durable
            return

    def _is_committed_ready(self, entry: PluginCatalogEntry) -> bool:
        try:
            self._require_committed_ready(entry)
        except PluginConflictError:
            return False
        return True

    def _transition_projection(
        self,
        entry: PluginCatalogEntry,
        reconcile_failed: bool = False,
    ) -> dict[str, Any]:
        ready = self._is_committed_ready(entry)
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
