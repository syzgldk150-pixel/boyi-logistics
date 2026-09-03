from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agent.automation_plugins import mysql_repository as mysql_repository_module
from agent.automation_plugins.catalog import PluginCatalog
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import (
    AutomationPluginManifest,
    canonical_json_bytes,
    governance_anchor_from_tool_contract,
)
from agent.automation_plugins.models import (
    FirstPartyInstanceSeed,
    PluginTrustSource,
    PluginVersionRecord,
    RuntimeGenerationSnapshot,
)
from agent.automation_plugins.mysql_repository import (
    MySQLAutomationPluginRepositoryAdapter,
)
from agent.automation_plugins.runtime_repository import snapshot_to_row
from agent.automation_plugins.storage import VERIFIED_ARCHIVE_RELATIVE
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    OrchestrationPersistenceError,
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


def _synthetic_manifest(
    version: str,
    *,
    require_new_field: bool = False,
) -> AutomationPluginManifest:
    properties: dict[str, Any] = {"marker": {"type": "string"}}
    required = ["marker"]
    if require_new_field:
        properties["required_new"] = {"type": "string"}
        required.append("required_new")
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
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
    argument_template = {
        field: {"source": "project_config", "key": field}
        for field in properties
    }
    return AutomationPluginManifest.from_mapping(
        {
            "schema_version": 1,
            "plugin_id": "synthetic_upgrade_action",
            "name": "Synthetic upgrade action",
            "version": version,
            "description": "Synthetic action used only by upgrade tests",
            "execution_platform": "server",
            "runtime": {
                "kind": "python_subprocess",
                "entrypoint": "payload/main.py",
            },
            "config_schema": config_schema,
            "account_roles": [],
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
                    "argument_template": argument_template,
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


def _version(manifest: AutomationPluginManifest, marker: str) -> PluginVersionRecord:
    package_sha256 = marker * 64
    install_root = f"/plugins/synthetic/{manifest.version}"
    return PluginVersionRecord(
        plugin_id=manifest.plugin_id,
        version=manifest.version,
        package_sha256=package_sha256,
        manifest_sha256=manifest.manifest_sha256,
        manifest=manifest.to_mapping(),
        trust_source=PluginTrustSource.ED25519_UPLOAD,
        install_root=install_root,
        install_metadata={
            "install_root": install_root,
            "python_relative": "venv/bin/python",
            "archive_relative": VERIFIED_ARCHIVE_RELATIVE,
            "archive_sha256": package_sha256,
        },
    )


def _snapshot(
    manifest: AutomationPluginManifest,
    version: PluginVersionRecord,
) -> RuntimeGenerationSnapshot:
    project_config = {"marker": "A"}
    account_bindings: dict[str, Any] = {}
    resource_bindings: dict[str, str] = {}
    schedule = {"kind": "none", "times": [], "enabled": False}
    compiled_invocations = {
        "console": {
            "arguments": copy.deepcopy(project_config),
            "dynamic_resolvers": {},
        }
    }
    runtime_descriptor = {
        "install_metadata": copy.deepcopy(dict(version.install_metadata)),
        "runtime": copy.deepcopy(dict(manifest.runtime)),
        "runtime_permissions": copy.deepcopy(dict(manifest.runtime_permissions)),
        "account_roles": [],
        "resource_roles": [],
    }
    action_contract = copy.deepcopy(dict(manifest.tool_contract))
    governance_anchor = copy.deepcopy(dict(manifest.governance_anchor))
    execution_metadata = {
        "project_config_version": 1,
        "project_config": project_config,
        "account_bindings": account_bindings,
        "resource_bindings": resource_bindings,
        "device_binding": None,
        "schedule": schedule,
        "compiled_invocations": compiled_invocations,
        "runtime_descriptor": runtime_descriptor,
        "action_contract": action_contract,
        "governance_anchor": governance_anchor,
    }
    return RuntimeGenerationSnapshot(
        automation_id="upgrade-instance",
        generation=1,
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.version,
        package_sha256=version.package_sha256,
        manifest_sha256=manifest.manifest_sha256,
        trust_source=PluginTrustSource.ED25519_UPLOAD,
        project_config_sha256=_sha(project_config),
        account_bindings_sha256=_sha(account_bindings),
        resource_bindings_sha256=_sha(resource_bindings),
        device_binding_sha256=_sha(None),
        schedule_sha256=_sha(schedule),
        core_registry_sha256=_sha({"registry": "synthetic"}),
        tool_contract_sha256=_sha(action_contract),
        invocation_contracts_sha256=_sha(
            manifest.to_mapping()["invocation_contracts"]
        ),
        compiled_invocations_sha256=_sha(compiled_invocations),
        runtime_descriptor_sha256=_sha(runtime_descriptor),
        governance_anchor_sha256=_sha(governance_anchor),
        policy_contract_sha256=_sha({"mode": "REQUIRE_EACH_RUN"}),
        enabled_entrypoints=("console",),
        execution_metadata=execution_metadata,
    )


def _version_row(record: PluginVersionRecord) -> dict[str, Any]:
    return {
        "plugin_id": record.plugin_id,
        "version": record.version,
        "package_sha256": record.package_sha256,
        "manifest_sha256": record.manifest_sha256,
        "manifest_json": copy.deepcopy(dict(record.manifest)),
        "trust_source": record.trust_source.value,
        "install_root_metadata_json": copy.deepcopy(dict(record.install_metadata)),
        "state": "INSTALLED",
        "installed_at": record.installed_at,
    }


def _generation_row(snapshot: RuntimeGenerationSnapshot) -> dict[str, Any]:
    raw = snapshot_to_row(snapshot)
    return {
        **raw,
        "state": "COMMITTED",
        "snapshot_json": raw,
        "snapshot_sha256": _persisted_sha(raw),
        "enabled_entrypoints_sha256": _persisted_sha(
            list(snapshot.enabled_entrypoints)
        ),
        "coeffects": [],
        "effects": [],
    }


class _LowLevelPluginRepository:
    def __init__(
        self,
        *,
        version: PluginVersionRecord,
        snapshot: RuntimeGenerationSnapshot,
        enabled: bool,
    ) -> None:
        self.projects = {
            snapshot.automation_id: {
                "automation_id": snapshot.automation_id,
                "plugin_id": version.plugin_id,
                "plugin_version": version.version,
                "display_name": "Synthetic upgrade instance",
                "enabled": enabled,
                "state": "ENABLED" if enabled else "DISABLED",
                "record_version": 1,
                "target_generation": 1,
                "committed_generation": 1,
                "reconcile_state": "STABLE",
            }
        }
        self.configs = {
            snapshot.automation_id: {
                "automation_id": snapshot.automation_id,
                "config_json": {"marker": "A"},
                "account_bindings_json": {},
                "resource_bindings_json": {},
                "enabled_entrypoints_json": ["console"],
                "desired_schedule_json": {
                    "kind": "none",
                    "times": [],
                    "enabled": False,
                },
                "compiled_invocations_json": {
                    "console": {
                        "arguments": {"marker": "A"},
                        "dynamic_resolvers": {},
                    }
                },
                "device_id": None,
                "config_version": 1,
            }
        }
        self.versions = {(version.plugin_id, version.version): _version_row(version)}
        self.generations = {
            (snapshot.automation_id, snapshot.generation): _generation_row(snapshot)
        }
        self.upgrade_requests: dict[str, dict[str, Any]] = {}
        self.unknown_writes: set[tuple[str, int]] = set()

    def get_project(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        del for_update
        row = self.projects.get(automation_id)
        return copy.deepcopy(row) if row is not None else None

    def get_project_config(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        del for_update
        row = self.configs.get(automation_id)
        return copy.deepcopy(row) if row is not None else None

    def get_version(self, plugin_id: str, version: str) -> dict[str, Any] | None:
        row = self.versions.get((plugin_id, version))
        return copy.deepcopy(row) if row is not None else None

    def get_generation_row(
        self,
        automation_id: str,
        generation: int,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        del for_update
        row = self.generations.get((automation_id, generation))
        return copy.deepcopy(row) if row is not None else None

    def has_unknown_generation_write_row(
        self,
        automation_id: str,
        generation: int,
    ) -> bool:
        return (automation_id, generation) in self.unknown_writes

    def lock_project_generation_history(
        self,
        automation_id: str,
        *,
        committed_generation: int,
    ) -> tuple[dict[str, Any], ...]:
        rows = []
        for (project_id, generation), persisted in sorted(self.generations.items()):
            if project_id != automation_id:
                continue
            row = copy.deepcopy(persisted)
            if (
                generation <= committed_generation
                and row.get("state") == "BLOCKED"
                and row.get("error_code") == "WRITE_OUTCOME_UNKNOWN"
                and (automation_id, generation) in self.unknown_writes
            ):
                row["_archival_unknown_write"] = True
            rows.append(row)
        return tuple(rows)

    def register_package_version(
        self,
        *,
        package: Mapping[str, Any],
        version: Mapping[str, Any],
    ) -> None:
        plugin_id = str(package["plugin_id"])
        version_name = str(version["version"])
        row = {
            "plugin_id": plugin_id,
            **copy.deepcopy(dict(version)),
            "state": "INSTALLED",
            "installed_at": datetime.now(timezone.utc),
        }
        key = (plugin_id, version_name)
        existing = self.versions.get(key)
        if existing is not None:
            if (
                existing["package_sha256"] != row["package_sha256"]
                or existing["manifest_sha256"] != row["manifest_sha256"]
            ):
                raise IdempotencyConflict("immutable plugin version drifted")
            return
        self.versions[key] = row

    def stage_project_upgrade(
        self,
        automation_id: str,
        *,
        plugin_id: str,
        from_version: str,
        to_version: str,
        package_sha256: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
        expected_record_version: int,
        allow_blocked_unknown_write_archive: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "automation_id": automation_id,
            "plugin_id": plugin_id,
            "from_version": from_version,
            "to_version": to_version,
            "package_sha256": package_sha256,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "expected_record_version": expected_record_version,
            "allow_blocked_unknown_write_archive": (
                allow_blocked_unknown_write_archive
            ),
        }
        prior = self.upgrade_requests.get(request_id)
        if prior is not None:
            if prior != payload:
                raise IdempotencyConflict(
                    "plugin upgrade request was reused with different input"
                )
            replay = self.get_project(automation_id)
            if replay is None:
                raise OrchestrationPersistenceError("upgraded project disappeared")
            replay["_upgrade_staged_created"] = False
            return replay

        project = self.projects.get(automation_id)
        if project is None:
            raise OrchestrationPersistenceError("automation project is not installed")
        if (
            project["plugin_id"] != plugin_id
            or project["plugin_version"] != from_version
            or project["record_version"] != expected_record_version
            or project["state"] not in {"INSTALLED", "ENABLED", "DISABLED"}
        ):
            raise ConcurrentUpdateError("automation project changed before upgrade")
        if (
            project.get("reconcile_state") == "BLOCKED_UNKNOWN_WRITE"
            and not allow_blocked_unknown_write_archive
        ):
            raise ConcurrentUpdateError("blocked unknown write requires release authority")
        target = self.versions.get((plugin_id, to_version))
        if target is None or target["package_sha256"] != package_sha256:
            raise OrchestrationPersistenceError("upgrade target is not registered")
        maximum = max(
            generation
            for (project_id, generation) in self.generations
            if project_id == automation_id
        )
        project.update(
            {
                "plugin_version": to_version,
                "state": "UPGRADING",
                "target_generation": maximum + 1,
                "reconcile_state": "PREPARING",
                "record_version": project["record_version"] + 1,
            }
        )
        self.upgrade_requests[request_id] = payload
        staged = copy.deepcopy(project)
        staged["_upgrade_staged_created"] = True
        return staged


class _AbortRevertCursor:
    def __init__(
        self,
        *,
        project: Mapping[str, Any],
        committed_generation: Mapping[str, Any],
    ) -> None:
        self.project = dict(project)
        self.committed_generation = dict(committed_generation)
        self.result: Mapping[str, Any] | list[Mapping[str, Any]] | None = None
        self.rowcount = 0
        self.executions: list[tuple[str, object]] = []
        self.project_updates: list[tuple[str, object]] = []
        self.policy_updates: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        normalized = " ".join(str(sql).split())
        self.executions.append((normalized, params))
        self.rowcount = 0
        if normalized.startswith("SELECT") and "FROM automation_projects" in normalized:
            self.result = self.project
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_generation_effects" in normalized
        ):
            self.result = []
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_generation_leases" in normalized
        ):
            self.result = []
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_policies" in normalized
        ):
            self.result = {"mode": "REQUIRE_EACH_RUN"}
            return
        if (
            normalized.startswith("SELECT")
            and "FROM automation_project_generations" in normalized
            and "COUNT(*)" not in normalized
        ):
            self.result = self.committed_generation
            return
        if normalized.startswith("SELECT COUNT(*) AS draining_count"):
            self.result = {"draining_count": 0}
            return
        if normalized.startswith("UPDATE automation_project_generations"):
            self.result = None
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE automation_projects"):
            self.result = None
            self.rowcount = 1
            self.project_updates.append((normalized, params))
            return
        if normalized.startswith("UPDATE automation_project_policies"):
            self.result = None
            self.rowcount = 1
            self.policy_updates.append((normalized, params))
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> Mapping[str, Any] | None:
        return self.result if isinstance(self.result, Mapping) else None

    def fetchall(self) -> list[Mapping[str, Any]]:
        return self.result if isinstance(self.result, list) else []

    def close(self) -> None:
        return None


class _AbortRevertConnection:
    def __init__(self, cursor: _AbortRevertCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _AbortRevertCursor:
        return self._cursor


class _PolicyRepository:
    def __init__(self, automation_id: str) -> None:
        self.policies = {
            automation_id: {
                "automation_id": automation_id,
                "mode": "PROJECT_FULL_AUTO",
                "version": 1,
            }
        }
        self.events: list[dict[str, Any]] = []
        self.expired: list[str] = []

    def get_policy(
        self,
        automation_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        del for_update
        policy = self.policies.get(automation_id)
        return copy.deepcopy(policy) if policy is not None else None

    def update_policy(
        self,
        automation_id: str,
        *,
        expected_version: int,
        **changes: Any,
    ) -> dict[str, Any]:
        policy = self.policies[automation_id]
        if policy["version"] != expected_version:
            raise ConcurrentUpdateError("policy changed")
        policy.update(copy.deepcopy(changes))
        policy["version"] += 1
        return copy.deepcopy(policy)

    def append_event(self, row: Mapping[str, Any]) -> None:
        self.events.append(copy.deepcopy(dict(row)))

    def expire_pending_approvals(self, automation_id: str) -> None:
        self.expired.append(automation_id)

    def invalidate_pending_approvals_and_wake_runs(
        self,
        automation_id: str,
        *,
        event_repository=None,
    ) -> tuple[str, ...]:
        del event_repository
        self.expired.append(automation_id)
        return ()


class _DomainEvents:
    def __init__(self) -> None:
        self.items: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]] = []

    def append_with_outbox(
        self,
        event: Mapping[str, Any],
        consumers: tuple[Mapping[str, Any], ...],
    ) -> None:
        self.items.append(
            (
                copy.deepcopy(dict(event)),
                tuple(copy.deepcopy(dict(item)) for item in consumers),
            )
        )


class _UnitOfWork:
    def __init__(self, repository: _OrchestrationRepository) -> None:
        self._repository = repository
        self.automation_plugins = repository.low_level
        self.automation_projects = repository.policies
        self.events = repository.events
        self._snapshot: dict[str, Any] | None = None
        self._committed = False

    def __enter__(self) -> _UnitOfWork:
        self._snapshot = self._repository.export_state()
        return self

    def __exit__(self, *_args: object) -> bool:
        if not self._committed:
            assert self._snapshot is not None
            self._repository.restore_state(self._snapshot)
        return False

    def commit(self) -> None:
        self._committed = True
        self._repository.commit_count += 1


class _OrchestrationRepository:
    def __init__(
        self,
        low_level: _LowLevelPluginRepository,
        policies: _PolicyRepository,
        events: _DomainEvents,
    ) -> None:
        self.low_level = low_level
        self.policies = policies
        self.events = events
        self.commit_count = 0

    def unit_of_work(self) -> _UnitOfWork:
        return _UnitOfWork(self)

    def export_state(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "projects": self.low_level.projects,
                "configs": self.low_level.configs,
                "versions": self.low_level.versions,
                "generations": self.low_level.generations,
                "upgrade_requests": self.low_level.upgrade_requests,
                "unknown_writes": self.low_level.unknown_writes,
                "policies": self.policies.policies,
                "policy_events": self.policies.events,
                "expired": self.policies.expired,
                "domain_events": self.events.items,
            }
        )

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self.low_level.projects = copy.deepcopy(state["projects"])
        self.low_level.configs = copy.deepcopy(state["configs"])
        self.low_level.versions = copy.deepcopy(state["versions"])
        self.low_level.generations = copy.deepcopy(state["generations"])
        self.low_level.upgrade_requests = copy.deepcopy(state["upgrade_requests"])
        self.low_level.unknown_writes = copy.deepcopy(state["unknown_writes"])
        self.policies.policies = copy.deepcopy(state["policies"])
        self.policies.events = copy.deepcopy(state["policy_events"])
        self.policies.expired = copy.deepcopy(state["expired"])
        self.events.items = copy.deepcopy(state["domain_events"])


def _harness(
    *,
    enabled: bool = True,
) -> tuple[
    MySQLAutomationPluginRepositoryAdapter,
    _OrchestrationRepository,
    PluginVersionRecord,
]:
    manifest_v1 = _synthetic_manifest("1.0.0")
    version_v1 = _version(manifest_v1, "1")
    snapshot_v1 = _snapshot(manifest_v1, version_v1)
    low_level = _LowLevelPluginRepository(
        version=version_v1,
        snapshot=snapshot_v1,
        enabled=enabled,
    )
    policies = _PolicyRepository(snapshot_v1.automation_id)
    orchestration = _OrchestrationRepository(low_level, policies, _DomainEvents())
    return (
        MySQLAutomationPluginRepositoryAdapter(
            orchestration,
            release_hold_provider=lambda: False,
        ),
        orchestration,
        version_v1,
    )


def _upgrade(
    repository: MySQLAutomationPluginRepositoryAdapter,
    version: PluginVersionRecord,
    *,
    request_id: str,
    expected_current_version: str = "1.0.0",
    expected_record_version: int = 1,
) -> Any:
    return repository.upgrade_instance(
        "upgrade-instance",
        version,
        actor_id="admin-1",
        actor_role="super_admin",
        request_id=request_id,
        expected_current_version=expected_current_version,
        expected_record_version=expected_record_version,
    )


def test_upgrade_stages_desired_version_once_and_keeps_committed_v1() -> None:
    repository, orchestration, version_v1 = _harness(enabled=True)
    version_v2 = _version(_synthetic_manifest("2.0.0"), "2")
    request_id = str(uuid.uuid4())

    staged = _upgrade(repository, version_v2, request_id=request_id)

    assert staged.state.value == "UPGRADING"
    assert staged.enabled is True
    assert staged.active_version.version == "2.0.0"
    assert staged.target_generation == 2
    assert staged.committed_generation == 1
    assert staged.committed_snapshot is not None
    assert staged.committed_snapshot.plugin_version == "1.0.0"
    assert staged.committed_snapshot.package_sha256 == version_v1.package_sha256
    catalog = PluginCatalog(repository)
    desired = catalog.require("upgrade-instance")
    assert desired.installed_version == "2.0.0"
    assert desired.state == "UPGRADING"
    committed_capability = catalog.get_project_capability("upgrade-instance")
    committed_runtime = committed_capability["_plugin_runtime"]
    assert committed_runtime["generation"] == 1
    assert committed_runtime["version"] == "1.0.0"
    assert committed_runtime["package_sha256"] == version_v1.package_sha256
    project = orchestration.low_level.projects["upgrade-instance"]
    assert project["plugin_version"] == "2.0.0"
    assert project["state"] == "UPGRADING"
    assert project["enabled"] is True
    assert project["target_generation"] == 2
    assert project["committed_generation"] == 1
    assert orchestration.policies.policies["upgrade-instance"]["mode"] == (
        "PROJECT_FULL_AUTO"
    )
    assert orchestration.policies.policies["upgrade-instance"]["project_generation"] == 2
    assert len(orchestration.policies.events) == 1
    assert len(orchestration.events.items) == 1
    assert orchestration.commit_count == 1

    replayed = _upgrade(repository, version_v2, request_id=request_id)

    assert replayed.record_version == staged.record_version
    assert replayed.target_generation == 2
    assert replayed.committed_generation == 1
    assert len(orchestration.low_level.upgrade_requests) == 1
    assert len(orchestration.policies.events) == 1
    assert len(orchestration.events.items) == 1
    assert orchestration.commit_count == 2

    state_before_drift = orchestration.export_state()
    version_v3 = _version(_synthetic_manifest("3.0.0"), "3")
    with pytest.raises(IdempotencyConflict, match="different input"):
        _upgrade(repository, version_v3, request_id=request_id)
    assert orchestration.export_state() == state_before_drift
    assert orchestration.commit_count == 2


def test_upgrade_accepts_an_intentionally_empty_entrypoint_set() -> None:
    repository, orchestration, _version_v1 = _harness(enabled=True)
    config = orchestration.low_level.configs["upgrade-instance"]
    config["enabled_entrypoints_json"] = []
    config["compiled_invocations_json"] = {}
    version_v2 = _version(_synthetic_manifest("2.0.0"), "2")

    staged = _upgrade(
        repository,
        version_v2,
        request_id=str(uuid.uuid4()),
    )

    assert staged.active_version.version == "2.0.0"
    assert staged.state.value == "UPGRADING"
    assert orchestration.low_level.configs["upgrade-instance"][
        "enabled_entrypoints_json"
    ] == []


def test_first_party_upgrade_preparation_recompiles_preserved_configuration() -> None:
    repository, orchestration, _version_v1 = _harness(enabled=True)
    low_level = orchestration.low_level
    low_level.configs["upgrade-instance"]["compiled_invocations_json"] = {}

    def save_project_config(automation_id: str, **payload: Any) -> dict[str, Any]:
        config = low_level.configs[automation_id]
        config.update(
            {
                "config_json": copy.deepcopy(payload["config"]),
                "account_bindings_json": copy.deepcopy(
                    payload["account_bindings"]
                ),
                "resource_bindings_json": copy.deepcopy(
                    payload["resource_bindings"]
                ),
                "enabled_entrypoints_json": list(payload["enabled_entrypoints"]),
                "desired_schedule_json": copy.deepcopy(payload["schedule"]),
                "compiled_invocations_json": copy.deepcopy(
                    payload["compiled_invocations"]
                ),
                "config_version": int(config["config_version"]) + 1,
            }
        )
        low_level.projects[automation_id]["record_version"] += 1
        return copy.deepcopy(config)

    low_level.save_project_config = save_project_config  # type: ignore[attr-defined]
    version_v2 = _version(_synthetic_manifest("2.0.0"), "2")
    seed = FirstPartyInstanceSeed(
        automation_id="upgrade-instance",
        plugin_id=version_v2.plugin_id,
        version=version_v2.version,
        display_name="Synthetic upgrade instance",
        allowed_entrypoints=("console",),
    )

    current_version, record_version, prepared_configuration_request_id = (
        repository._prepare_first_party_upgrade_configuration(
            seed=seed,
            version=version_v2,
            release_sha="a" * 40,
            expected_current_version="1.0.0",
            allow_blocked_unknown_write_archive=False,
        )
    )

    assert current_version == "1.0.0"
    assert record_version == 2
    assert prepared_configuration_request_id is None
    config = low_level.configs["upgrade-instance"]
    assert config["config_version"] == 2
    assert config["compiled_invocations_json"] == {
        "console": {
            "arguments": {"marker": "A"},
            "dynamic_resolvers": {},
        }
    }
    assert orchestration.policies.expired == ["upgrade-instance"]


def test_first_party_bootstrap_stages_an_exact_same_version_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, orchestration, version = _harness(enabled=True)
    seed = FirstPartyInstanceSeed(
        automation_id="upgrade-instance",
        plugin_id=version.plugin_id,
        version=version.version,
        display_name="Synthetic upgrade instance",
        allowed_entrypoints=("console",),
    )
    repair_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mysql_repository_module,
        "first_party_code_owned_config_repair_applies",
        lambda **_payload: True,
    )
    monkeypatch.setattr(
        mysql_repository_module,
        "first_party_code_owned_resource_binding_repair_applies",
        lambda **_payload: False,
    )
    monkeypatch.setattr(
        mysql_repository_module,
        "normalize_first_party_code_owned_versioned_config",
        lambda **payload: {**dict(payload["config"]), "repaired": "yes"},
    )

    def prepare_resource_repair(**payload: Any) -> tuple[str, int, str | None]:
        repair_calls.append(payload)
        return version.version, 1, None

    monkeypatch.setattr(
        repository,
        "_prepare_first_party_upgrade_configuration",
        prepare_resource_repair,
    )

    result = repository.bootstrap_missing(
        (version,),
        (seed,),
        release_sha="a" * 40,
    )

    assert result.created == ()
    assert result.existing == ("upgrade-instance",)
    assert len(repair_calls) == 1
    assert repair_calls[0]["expected_current_version"] == version.version
    assert repair_calls[0]["allow_same_version_repair"] is True
    assert repair_calls[0]["allow_blocked_unknown_write_archive"] is False
    assert orchestration.commit_count == 1


def test_first_party_bootstrap_advances_blocked_unknown_write_and_stable_lineages() -> None:
    target_version = _version(_synthetic_manifest("2.0.0"), "2")
    seed = FirstPartyInstanceSeed(
        automation_id="upgrade-instance",
        plugin_id=target_version.plugin_id,
        version=target_version.version,
        display_name="Synthetic upgrade instance",
        allowed_entrypoints=("console",),
    )

    blocked_repository, blocked_orchestration, _blocked_v1 = _harness(enabled=True)
    blocked_orchestration.low_level.projects["upgrade-instance"][
        "reconcile_state"
    ] = "BLOCKED_UNKNOWN_WRITE"
    blocked_orchestration.low_level.generations[("upgrade-instance", 1)][
        "state"
    ] = "BLOCKED"
    blocked_orchestration.low_level.generations[("upgrade-instance", 1)][
        "error_code"
    ] = "WRITE_OUTCOME_UNKNOWN"
    blocked_orchestration.low_level.unknown_writes.add(("upgrade-instance", 1))
    blocked_result = blocked_repository.bootstrap_missing(
        (target_version,),
        (seed,),
        release_sha="a" * 40,
    )

    assert blocked_result.created == ()
    assert blocked_result.existing == ("upgrade-instance",)
    assert (
        target_version.plugin_id,
        target_version.version,
    ) in blocked_orchestration.low_level.versions
    assert len(blocked_orchestration.low_level.upgrade_requests) == 1
    assert next(
        iter(blocked_orchestration.low_level.upgrade_requests.values())
    )["allow_blocked_unknown_write_archive"] is True
    blocked_project = blocked_orchestration.low_level.projects["upgrade-instance"]
    assert blocked_project["plugin_version"] == "2.0.0"
    assert blocked_project["state"] == "UPGRADING"
    assert blocked_project["target_generation"] == 2
    assert blocked_project["committed_generation"] == 1
    assert blocked_project["reconcile_state"] == "PREPARING"
    assert blocked_orchestration.low_level.generations[("upgrade-instance", 1)][
        "state"
    ] == "BLOCKED"
    assert ("upgrade-instance", 1) in blocked_orchestration.low_level.unknown_writes
    blocked_policy = blocked_orchestration.policies.policies["upgrade-instance"]
    assert blocked_policy["mode"] == "PROJECT_FULL_AUTO"
    assert blocked_policy["project_generation"] == 2
    assert blocked_orchestration.policies.events[-1]["reason"] == (
        "PLUGIN_VERSION_CHANGED"
    )

    stable_repository, stable_orchestration, _stable_v1 = _harness(enabled=True)

    stable_result = stable_repository.bootstrap_missing(
        (target_version,),
        (seed,),
        release_sha="b" * 40,
    )

    assert stable_result.created == ()
    assert stable_result.existing == ("upgrade-instance",)
    assert len(stable_orchestration.low_level.upgrade_requests) == 1
    assert next(
        iter(stable_orchestration.low_level.upgrade_requests.values())
    )["allow_blocked_unknown_write_archive"] is False
    assert stable_orchestration.low_level.projects["upgrade-instance"][
        "plugin_version"
    ] == "2.0.0"
    assert stable_orchestration.low_level.projects["upgrade-instance"][
        "state"
    ] == "UPGRADING"
    assert stable_orchestration.low_level.projects["upgrade-instance"][
        "target_generation"
    ] == 2


def test_incompatible_upgrade_rolls_back_registered_version_and_all_project_state() -> None:
    repository, orchestration, _version_v1 = _harness(enabled=True)
    incompatible_v2 = _version(
        _synthetic_manifest("2.0.0", require_new_field=True),
        "2",
    )
    before = orchestration.export_state()

    with pytest.raises(PluginConflictError) as raised:
        _upgrade(repository, incompatible_v2, request_id=str(uuid.uuid4()))

    assert raised.value.code == "PLUGIN_UPGRADE_CONFIGURATION_INCOMPATIBLE"
    assert orchestration.export_state() == before
    assert orchestration.commit_count == 0


def test_catalog_fails_closed_for_committed_and_desired_version_tampering() -> None:
    committed_repository, committed_orchestration, version_v1 = _harness()
    committed_key = (version_v1.plugin_id, version_v1.version)
    committed_orchestration.low_level.versions[committed_key]["package_sha256"] = (
        "f" * 64
    )

    with pytest.raises(ValueError, match="immutable package"):
        committed_repository.get_instance("upgrade-instance")

    desired_repository, desired_orchestration, _version_v1 = _harness()
    version_v2 = _version(_synthetic_manifest("2.0.0"), "2")
    _upgrade(
        desired_repository,
        version_v2,
        request_id=str(uuid.uuid4()),
    )
    desired_key = (version_v2.plugin_id, version_v2.version)
    desired_orchestration.low_level.versions[desired_key]["manifest_sha256"] = (
        "e" * 64
    )

    with pytest.raises(PluginConflictError, match="manifest digest"):
        PluginCatalog(desired_repository).require("upgrade-instance")


def test_failed_uncommitted_target_dispose_atomically_restores_committed_desired() -> None:
    cursor = _AbortRevertCursor(
        project={
            "automation_id": "upgrade-instance",
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "UPGRADING",
            "enabled": True,
            "target_generation": 2,
            "committed_generation": 1,
            "record_version": 4,
        },
        committed_generation={
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "1.0.0",
            "state": "COMMITTED",
        },
    )
    repository = AutomationPluginRepository(_AbortRevertConnection(cursor))

    repository.complete_generation_dispose_row("upgrade-instance", 2)

    assert len(cursor.project_updates) == 1
    update_sql, params = cursor.project_updates[0]
    assert "plugin_version=%s" in update_sql
    assert "target_generation=%s" in update_sql
    assert "CASE WHEN enabled THEN 'ENABLED' ELSE 'DISABLED' END" in update_sql
    assert "reconcile_state=%s" in update_sql
    assert params is not None
    assert "1.0.0" in params
    assert 1 in params
    assert "STABLE" in params
    assert len(cursor.policy_updates) == 1
    policy_sql, policy_params = cursor.policy_updates[0]
    assert "project_generation=%s" in policy_sql
    assert "mode=" not in policy_sql
    assert policy_params is not None
    assert 1 in policy_params


def test_postcommit_old_generation_dispose_never_reverts_committed_b() -> None:
    cursor = _AbortRevertCursor(
        project={
            "automation_id": "upgrade-instance",
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "ENABLED",
            "enabled": True,
            "target_generation": 2,
            "committed_generation": 2,
            "record_version": 5,
        },
        committed_generation={
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "COMMITTED",
        },
    )
    repository = AutomationPluginRepository(_AbortRevertConnection(cursor))

    repository.complete_generation_dispose_row("upgrade-instance", 1)

    assert len(cursor.project_updates) == 1
    update_sql, params = cursor.project_updates[0]
    assert "plugin_version=%s" not in update_sql
    assert "target_generation=%s" not in update_sql
    assert params == ("STABLE", "upgrade-instance")
    assert cursor.policy_updates == []


def test_postcommit_old_generation_failure_keeps_project_draining_not_error() -> None:
    cursor = _AbortRevertCursor(
        project={
            "automation_id": "upgrade-instance",
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "ENABLED",
            "enabled": True,
            "target_generation": 2,
            "committed_generation": 2,
            "record_version": 5,
        },
        committed_generation={
            "plugin_id": "synthetic_upgrade_action",
            "plugin_version": "2.0.0",
            "state": "COMMITTED",
        },
    )
    repository = AutomationPluginRepository(_AbortRevertConnection(cursor))

    repository.fail_generation_row(
        "upgrade-instance",
        1,
        error_code="DISPOSE_FAILED",
        error_summary="synthetic cleanup failure",
    )

    assert len(cursor.project_updates) == 1
    update_sql, params = cursor.project_updates[0]
    assert "CASE" in update_sql
    assert "committed_generation<>%s" in update_sql
    assert "target_generation<>%s" in update_sql
    assert "'DRAINING'" in update_sql
    assert "'ERROR'" in update_sql
    assert params == (1, 1, "upgrade-instance")


def test_aborted_upgrade_old_request_is_idempotent_and_new_request_advances() -> None:
    repository, orchestration, version_v1 = _harness(enabled=True)
    version_v2 = _version(_synthetic_manifest("2.0.0"), "2")
    old_request = str(uuid.uuid4())
    staged = _upgrade(repository, version_v2, request_id=old_request)
    assert staged.target_generation == 2

    failed_row = copy.deepcopy(
        orchestration.low_level.generations[("upgrade-instance", 1)]
    )
    failed_row.update({"generation": 2, "state": "DISPOSED"})
    orchestration.low_level.generations[("upgrade-instance", 2)] = failed_row
    project = orchestration.low_level.projects["upgrade-instance"]
    project.update(
        {
            "plugin_version": version_v1.version,
            "state": "ENABLED",
            "target_generation": 1,
            "reconcile_state": "STABLE",
            "record_version": project["record_version"] + 1,
        }
    )
    orchestration.policies.policies["upgrade-instance"]["project_generation"] = 1
    state_after_abort = orchestration.export_state()

    replayed = _upgrade(repository, version_v2, request_id=old_request)

    assert replayed.active_version.version == "1.0.0"
    assert replayed.target_generation == 1
    assert replayed.committed_generation == 1
    assert orchestration.low_level.projects["upgrade-instance"] == project
    assert len(orchestration.low_level.upgrade_requests) == 1
    assert len(orchestration.policies.events) == 1
    assert len(orchestration.events.items) == 1
    assert orchestration.policies.policies["upgrade-instance"]["mode"] == (
        "PROJECT_FULL_AUTO"
    )
    assert orchestration.policies.policies["upgrade-instance"]["project_generation"] == 1
    assert orchestration.export_state() == state_after_abort

    new_request = str(uuid.uuid4())
    restaged = _upgrade(
        repository,
        version_v2,
        request_id=new_request,
        expected_record_version=int(project["record_version"]),
    )

    assert restaged.active_version.version == "2.0.0"
    assert restaged.target_generation == 3
    assert restaged.committed_generation == 1
    assert len(orchestration.low_level.upgrade_requests) == 2
    assert orchestration.policies.policies["upgrade-instance"]["mode"] == (
        "PROJECT_FULL_AUTO"
    )
    assert orchestration.policies.policies["upgrade-instance"]["project_generation"] == 3
