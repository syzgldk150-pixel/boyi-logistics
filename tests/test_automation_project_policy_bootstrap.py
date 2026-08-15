from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from agent.automation_plugins.first_party import release_first_party_automation_ids
from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
)
from agent.orchestration.policy_engine import PolicyEngine
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    CompiledAutomationProjectContract,
    InvocationArgumentContract,
    canonical_sha256,
)
from shared.automation_project_manifest import (
    FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES,
)
from shared.automation_project_policy_repository import (
    AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID,
    AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ROLE,
    AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY,
    AUTOMATION_PROJECT_BOOTSTRAP_EVENT_COMMENT,
    AUTOMATION_PROJECT_BOOTSTRAP_POLICY_COMMENT,
    AUTOMATION_PROJECT_BOOTSTRAP_REASON,
    LEGACY_SCHEDULE_GRANT_ACTOR_ID,
    LEGACY_SCHEDULE_GRANT_ACTOR_ROLE,
    LEGACY_SCHEDULE_GRANT_REASON,
    PLUGIN_CONFIGURATION_ACTOR_ID,
    PLUGIN_CONFIGURATION_ACTOR_ROLE,
    PLUGIN_CONFIGURATION_REASON,
    AutomationProjectBootstrapContractError,
    automation_project_bootstrap_initial_mode,
    automation_project_bootstrap_project_set_sha256,
    automation_project_bootstrap_source_snapshot_sha256,
    automation_project_configuration_bootstrap_request_id,
    automation_project_policy_bootstrap_request_id,
    legacy_scheduled_policy_grant_request_id,
    validate_automation_project_bootstrap_policy_event,
    validate_automation_project_bootstrap_source_snapshot,
    validate_existing_automation_project_bootstrap,
    validate_legacy_scheduled_grant_event,
    validate_persisted_automation_project_configuration_evidence,
)
from shared.orchestration_repository_support import _json_hash


ROOT = Path(__file__).resolve().parents[1]
RELEASE_PREFLIGHT_PATH = (
    ROOT / "agent" / "scripts" / "automation_project_release_manifest_preflight.py"
)
RELEASE_SHA = "a" * 40
FUTURE_RELEASE_SHA = "b" * 40
CONFIGURATION_VERSION = 2
AUTOMATION_GENERATION = 1
UNAUTHORIZED_SCHEDULE_PROJECTS = frozenset(
    {"customer_problems_shadow", "yunda_dispatch_forecast"}
)


def _sha(value: object) -> str:
    return canonical_sha256(value)


def _load_release_preflight():
    spec = importlib.util.spec_from_file_location(
        "automation_project_policy_bootstrap_release_preflight_test",
        RELEASE_PREFLIGHT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _task_arguments(automation_id: str) -> dict[str, str]:
    return {"automation_id": automation_id}


def _cron_expression(task_id: str) -> str:
    if task_id == "customer_problems_shadow":
        return "*/15 * * * *"
    if task_id == "finance_startup_catchup":
        return "@startup"
    suffix = task_id.rsplit("_", 1)[-1]
    if len(suffix) != 4 or not suffix.isdigit():
        raise AssertionError(f"unrecognized release task identity: {task_id}")
    return f"{int(suffix[2:])} {int(suffix[:2])} * * *"


def _schedule_metadata(task_ids: tuple[str, ...], *, enabled: bool) -> dict:
    if not task_ids:
        return {"kind": "none", "times": [], "enabled": False}
    if task_ids == ("finance_startup_catchup",):
        return {"kind": "startup", "times": [], "enabled": enabled}
    if task_ids == ("customer_problems_shadow",):
        return {
            "kind": "daily_times",
            "times": [
                f"{hour:02d}:{minute:02d}"
                for hour in range(24)
                for minute in (0, 15, 30, 45)
            ],
            "enabled": enabled,
        }
    return {
        "kind": "daily_times",
        "times": sorted(
            f"{task_id[-4:-2]}:{task_id[-2:]}" for task_id in task_ids
        ),
        "enabled": enabled,
    }


def _build_contract(
    automation_id: str,
    rows: list[dict],
    *,
    allowed_entrypoints: frozenset[str],
) -> CompiledAutomationProjectContract:
    tool_name = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
        automation_id
    ].tool_name
    arguments = _task_arguments(automation_id)
    effective_entrypoints = set(allowed_entrypoints)
    if not rows:
        effective_entrypoints.discard("scheduler")
    invocation_contracts: dict[str, InvocationArgumentContract] = {}
    for row in rows:
        contract_id = f"scheduler:{row['id']}"
        invocation_contracts[contract_id] = InvocationArgumentContract(
            contract_id=contract_id,
            entrypoint="scheduler",
            expected_arguments=arguments,
            dynamic_argument_resolvers={},
        )
    for entrypoint in sorted(effective_entrypoints - {"scheduler"}):
        invocation_contracts[entrypoint] = InvocationArgumentContract(
            contract_id=entrypoint,
            entrypoint=entrypoint,
            expected_arguments=arguments,
            dynamic_argument_resolvers={},
        )
    invocation_contracts = dict(sorted(invocation_contracts.items()))
    tool_contract_hash = _sha({"tool": tool_name, "version": "1.0.0"})
    plugin_contract_hash = _sha(
        {"automation_id": automation_id, "generation": AUTOMATION_GENERATION}
    )
    scheduled_configurations = [
        {
            "task_id": row["id"],
            "contract_id": f"scheduler:{row['id']}",
            "configuration_version": CONFIGURATION_VERSION,
            "enabled": row["enabled"],
            "cron_expression_hash": _sha(row["cron_expression"]),
            "arguments_hash": _sha(row["tool_params"]),
            "dynamic_resolvers_hash": _sha({}),
        }
        for row in sorted(rows, key=lambda value: value["id"])
    ]
    snapshot = {
        "schema_version": 1,
        "automation_id": automation_id,
        "automation_generation": AUTOMATION_GENERATION,
        "manifest_sha256": _sha({"manifest": automation_id}),
        "tool_name": tool_name,
        "governance_anchor_name": tool_name,
        "tool_version": "1.0.0",
        "operation_type": OperationType.EXTERNAL_WRITE.value,
        "risk_level": RiskLevel.HIGH.value,
        "allowed_entrypoints": sorted(effective_entrypoints),
        "invocation_contracts": [
            {
                "contract_id": contract.contract_id,
                "entrypoint": contract.entrypoint,
                "arguments_hash": _sha(contract.expected_arguments),
                "dynamic_resolvers_hash": _sha(
                    contract.dynamic_argument_resolvers
                ),
            }
            for contract in invocation_contracts.values()
        ],
        "account_bindings_sha256": _sha({}),
        "resource_bindings_sha256": _sha({}),
        "device_binding_sha256": _sha(None),
        "project_config_sha256": _sha({"automation_id": automation_id}),
        "tool_contract_hash": tool_contract_hash,
        "plugin_contract_hash": plugin_contract_hash,
        "scheduled_configurations": scheduled_configurations,
    }
    contract_hash = _sha(snapshot)
    return CompiledAutomationProjectContract(
        automation_id=automation_id,
        automation_generation=AUTOMATION_GENERATION,
        manifest_sha256=snapshot["manifest_sha256"],
        tool_name=tool_name,
        tool_version="1.0.0",
        operation_type=OperationType.EXTERNAL_WRITE.value,
        risk_level=RiskLevel.HIGH.value,
        invocation_contracts=invocation_contracts,
        account_bindings={},
        allowed_entrypoints=frozenset(effective_entrypoints),
        contract_hash=contract_hash,
        tool_contract_hash=tool_contract_hash,
        plugin_contract_hash=plugin_contract_hash,
        project_configuration_version=CONFIGURATION_VERSION,
        snapshot=snapshot,
        can_full_auto=True,
    )


def _project_config(
    automation_id: str,
    *,
    schedule: dict,
    allowed_entrypoints: frozenset[str],
) -> dict:
    payloads = {
        "config_json": {"automation_id": automation_id},
        "account_bindings_json": {},
        "resource_bindings_json": {},
        "enabled_entrypoints_json": sorted(allowed_entrypoints),
        "desired_schedule_json": schedule,
        "compiled_invocations_json": {"automation_id": automation_id},
    }
    config = {
        "automation_id": automation_id,
        "configured": True,
        "config_version": CONFIGURATION_VERSION,
        "device_id": None,
        **payloads,
    }
    for field_name, value in payloads.items():
        config[field_name.removesuffix("_json") + "_sha256"] = _json_hash(
            value
        )
    return config


def _configuration_event_evidence(
    automation_id: str,
    *,
    config: dict,
    scheduled_task_count: int,
) -> tuple[list[dict], list[dict]]:
    request_id = automation_project_configuration_bootstrap_request_id(
        RELEASE_SHA,
        automation_id,
    )
    request_payload_sha256 = _json_hash(
        {
            "config": dict(config["config_json"]),
            "account_bindings": dict(config["account_bindings_json"]),
            "resource_bindings": dict(config["resource_bindings_json"]),
            "enabled_entrypoints": list(config["enabled_entrypoints_json"]),
            "schedule": dict(config["desired_schedule_json"]),
            "compiled_invocations": dict(config["compiled_invocations_json"]),
            "device_id": config["device_id"],
            "expected_project_configuration_version": (
                CONFIGURATION_VERSION - 1
            ),
        }
    )
    metadata = {
        "request_payload_sha256": request_payload_sha256,
        "from_project_configuration_version": CONFIGURATION_VERSION - 1,
        "to_project_configuration_version": CONFIGURATION_VERSION,
        "schedule_sha256": config["desired_schedule_sha256"],
        "scheduled_task_count": scheduled_task_count,
    }
    policy_event = {
        "event_id": 1,
        "automation_id": automation_id,
        "request_id": request_id,
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "REQUIRE_EACH_RUN",
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "project_configuration_version": CONFIGURATION_VERSION,
        "project_generation": AUTOMATION_GENERATION,
        "actor_id": PLUGIN_CONFIGURATION_ACTOR_ID,
        "actor_role": PLUGIN_CONFIGURATION_ACTOR_ROLE,
        "actor_display_name": None,
        "reason": PLUGIN_CONFIGURATION_REASON,
        "comment": None,
        "correlation_id": request_id,
    }
    evidence = {
        "policy_event_id": 1,
        "request_id": request_id,
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "REQUIRE_EACH_RUN",
        "policy_contract_hash": None,
        "policy_contract_snapshot_json": None,
        "policy_tool_contract_hash": None,
        "policy_plugin_contract_hash": None,
        "policy_configuration_version": CONFIGURATION_VERSION,
        "policy_project_generation": AUTOMATION_GENERATION,
        "policy_actor_id": PLUGIN_CONFIGURATION_ACTOR_ID,
        "policy_actor_role": PLUGIN_CONFIGURATION_ACTOR_ROLE,
        "policy_actor_display_name": None,
        "policy_reason": PLUGIN_CONFIGURATION_REASON,
        "policy_comment": None,
        "policy_correlation_id": request_id,
        "configuration_event_id": 1,
        "configuration_event_type": "CONFIGURATION_UPDATED",
        "configuration_from_state": "ENABLED",
        "configuration_to_state": "ENABLED",
        "configuration_metadata_json": metadata,
        "configuration_metadata_sha256": _json_hash(metadata),
        "configuration_actor_id": PLUGIN_CONFIGURATION_ACTOR_ID,
        "configuration_actor_role": PLUGIN_CONFIGURATION_ACTOR_ROLE,
    }
    return [policy_event], [evidence]


def _scheduled_grant_event(row: dict, *, event_id: int) -> dict:
    tool_contract_hash = _sha(
        {"scheduled_tool": row["tool_name"], "version": "1.0.0"}
    )
    snapshot = {
        "schema_version": 1,
        "task_id": row["id"],
        "tool_name": row["tool_name"],
        "tool_version": "1.0.0",
        "operation_type": OperationType.EXTERNAL_WRITE.value,
        "risk_level": RiskLevel.HIGH.value,
        "approval_mode": "schedule_allowlist",
        "cron_expression": row["cron_expression"],
        "enabled": True,
        "configuration_version": CONFIGURATION_VERSION - 1,
        "arguments_hash": _sha(row["tool_params"]),
        "dynamic_rules_hash": _sha({}),
        "postconditions_hash": _sha([]),
        "tool_contract_hash": tool_contract_hash,
    }
    request_id = legacy_scheduled_policy_grant_request_id(row["id"])
    return {
        "event_id": event_id,
        "task_id": row["id"],
        "request_id": request_id,
        "from_mode": "REQUIRE_EACH_RUN",
        "to_mode": "EXACT_SCHEDULE_EXEMPT",
        "contract_hash": _sha(snapshot),
        "contract_snapshot_json": snapshot,
        "tool_contract_hash": tool_contract_hash,
        "actor_id": LEGACY_SCHEDULE_GRANT_ACTOR_ID,
        "actor_role": LEGACY_SCHEDULE_GRANT_ACTOR_ROLE,
        "actor_display_name": "Control Plane v1 migration",
        "reason": LEGACY_SCHEDULE_GRANT_REASON,
        "comment": "preserve previously authorized production automation",
        "correlation_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    }


def _scheduled_retirement_event(
    row: dict,
    *,
    automation_id: str,
    event_id: int,
) -> dict:
    request_id = automation_project_configuration_bootstrap_request_id(
        RELEASE_SHA,
        automation_id,
    )
    return {
        "event_id": event_id,
        "task_id": row["id"],
        "request_id": request_id,
        "from_mode": "EXACT_SCHEDULE_EXEMPT",
        "to_mode": "REQUIRE_EACH_RUN",
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "actor_id": PLUGIN_CONFIGURATION_ACTOR_ID,
        "actor_role": PLUGIN_CONFIGURATION_ACTOR_ROLE,
        "actor_display_name": None,
        "reason": PLUGIN_CONFIGURATION_REASON,
        "comment": None,
        "correlation_id": request_id,
    }


class _BootstrapState:
    def __init__(self) -> None:
        self.release_ids = tuple(sorted(release_first_party_automation_ids()))
        self.projects: dict[str, dict] = {}
        self.configs: dict[str, dict] = {}
        self.contracts: dict[str, CompiledAutomationProjectContract] = {}
        self.entries: dict[str, SimpleNamespace] = {}
        self.rows: dict[str, list[dict]] = {}
        self.legacy_rows: dict[str, list[dict]] = {}
        self.policies: dict[str, dict] = {}
        self.policy_events: dict[str, list[dict]] = {}
        self.configuration_evidence: dict[str, list[dict]] = {}
        self.scheduled_events: dict[str, list[dict]] = {}
        self.items: dict[str, dict] = {}
        self.marker: dict | None = None
        self.domain_event_calls = 0
        self.commits = 0
        event_id = 100
        for automation_id in self.release_ids:
            template = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[automation_id]
            task_ids = tuple(sorted(template.scheduled_task_ids))
            schedule_enabled = bool(task_ids) and (
                automation_id not in UNAUTHORIZED_SCHEDULE_PROJECTS
            )
            schedule = _schedule_metadata(task_ids, enabled=schedule_enabled)
            arguments = _task_arguments(automation_id)
            rows = [
                {
                    "id": task_id,
                    "automation_id": automation_id,
                    "tool_name": f"automation.{automation_id}.run",
                    "tool_params": dict(arguments),
                    "cron_expression": _cron_expression(task_id),
                    "enabled": schedule_enabled,
                    "automation_generation": AUTOMATION_GENERATION,
                    "configuration_version": CONFIGURATION_VERSION,
                    "scheduled_policy_mode": "REQUIRE_EACH_RUN",
                    "scheduled_policy_version": (
                        3 if schedule_enabled else 1
                    ),
                    "scheduled_contract_hash": None,
                    "scheduled_contract_snapshot_json": None,
                    "scheduled_tool_contract_hash": None,
                }
                for task_id in task_ids
            ]
            legacy_rows = [
                {
                    "id": row["id"],
                    "name": row["id"],
                    "tool_name": template.tool_name,
                    "tool_params": {
                        "legacy_automation_id": automation_id,
                        "account_id": f"{automation_id}-account",
                    },
                    "cron_expression": row["cron_expression"],
                    "enabled": row["enabled"],
                    "configuration_version": CONFIGURATION_VERSION - 1,
                }
                for row in rows
            ]
            contract = _build_contract(
                automation_id,
                rows,
                allowed_entrypoints=template.allowed_entrypoints,
            )
            config = _project_config(
                automation_id,
                schedule=schedule,
                allowed_entrypoints=contract.allowed_entrypoints,
            )
            policy_events, evidence = _configuration_event_evidence(
                automation_id,
                config=config,
                scheduled_task_count=len(rows),
            )
            scheduled_events: list[dict] = []
            if schedule_enabled:
                for row, legacy_row in zip(rows, legacy_rows, strict=True):
                    event_id += 1
                    scheduled_events.append(
                        _scheduled_grant_event(legacy_row, event_id=event_id)
                    )
                    event_id += 1
                    scheduled_events.append(
                        _scheduled_retirement_event(
                            row,
                            automation_id=automation_id,
                            event_id=event_id,
                        )
                    )
            self.projects[automation_id] = {
                "automation_id": automation_id,
                "migration_authority": True,
                "enabled": True,
                "state": "ENABLED",
                "target_generation": AUTOMATION_GENERATION,
                "committed_generation": AUTOMATION_GENERATION,
                "reconcile_state": "STABLE",
            }
            self.configs[automation_id] = config
            self.contracts[automation_id] = contract
            self.entries[automation_id] = SimpleNamespace(
                automation_id=automation_id,
                committed_snapshot=SimpleNamespace(
                    execution_metadata={"schedule": schedule}
                ),
            )
            self.rows[automation_id] = rows
            self.legacy_rows[automation_id] = legacy_rows
            self.policies[automation_id] = {
                "automation_id": automation_id,
                "mode": "REQUIRE_EACH_RUN",
                "version": 1,
                "contract_hash": None,
                "contract_snapshot_json": None,
                "tool_contract_hash": None,
                "plugin_contract_hash": None,
                "project_generation": AUTOMATION_GENERATION,
                "project_configuration_version": CONFIGURATION_VERSION,
                "approved_by_actor_id": None,
                "approved_by_actor_role": None,
                "approved_by_actor_display_name": None,
                "approved_at": None,
                "comment": None,
            }
            self.policy_events[automation_id] = policy_events
            self.configuration_evidence[automation_id] = evidence
            self.scheduled_events[automation_id] = scheduled_events


class _BootstrapProjectsPort:
    def __init__(self, repository: "_BootstrapRepository") -> None:
        self.repository = repository

    @property
    def state(self) -> _BootstrapState:
        return self.repository.state

    def get_bootstrap_marker_018(self, **_kwargs):
        return copy.deepcopy(self.state.marker)

    def list_bootstrap_items_018(self, **_kwargs):
        return [
            copy.deepcopy(self.state.items[key])
            for key in sorted(self.state.items)
        ]

    def get_policy(self, automation_id, **_kwargs):
        policy = self.state.policies.get(automation_id)
        return copy.deepcopy(policy)

    def list_configuration_rows(self, automation_id, **_kwargs):
        return copy.deepcopy(self.state.rows[automation_id])

    def list_automation_identity_backup_rows_018(self, task_ids, **_kwargs):
        expected = set(task_ids)
        rows = [
            copy.deepcopy(row)
            for project_rows in self.state.legacy_rows.values()
            for row in project_rows
            if row["id"] in expected
        ]
        if {row["id"] for row in rows} != expected:
            raise AssertionError("bootstrap backup task set mismatch")
        return sorted(rows, key=lambda row: row["id"])

    def list_policy_events(self, automation_id, **_kwargs):
        return copy.deepcopy(self.state.policy_events[automation_id])

    def list_configuration_event_evidence(self, automation_id, **_kwargs):
        return copy.deepcopy(self.state.configuration_evidence[automation_id])

    def list_scheduled_policy_events(self, automation_id, **_kwargs):
        return copy.deepcopy(self.state.scheduled_events[automation_id])

    def get_event_by_request(self, automation_id, request_id, **_kwargs):
        for event in self.state.policy_events[automation_id]:
            if event["request_id"] == request_id:
                return copy.deepcopy(event)
        return None

    def update_policy(
        self,
        automation_id,
        *,
        expected_version,
        mode,
        contract_hash,
        contract_snapshot,
        tool_contract_hash,
        plugin_contract_hash,
        project_generation,
        project_configuration_version,
        actor_id,
        actor_role,
        actor_display_name,
        comment,
    ):
        policy = self.state.policies[automation_id]
        if policy["version"] != expected_version:
            raise AssertionError("policy CAS mismatch")
        policy.update(
            {
                "mode": mode,
                "contract_hash": contract_hash,
                "contract_snapshot_json": copy.deepcopy(contract_snapshot),
                "tool_contract_hash": tool_contract_hash,
                "plugin_contract_hash": plugin_contract_hash,
                "project_generation": project_generation,
                "project_configuration_version": (
                    project_configuration_version
                ),
                "approved_by_actor_id": actor_id,
                "approved_by_actor_role": actor_role,
                "approved_by_actor_display_name": actor_display_name,
                "approved_at": datetime.now(timezone.utc),
                "comment": comment,
                "version": expected_version + 1,
            }
        )
        return copy.deepcopy(policy)

    def append_event(self, row):
        events = self.state.policy_events[row["automation_id"]]
        event = copy.deepcopy(dict(row))
        event["event_id"] = max(
            (int(existing["event_id"]) for existing in events),
            default=0,
        ) + 1
        events.append(event)
        return copy.deepcopy(event)

    def create_bootstrap_item_018(
        self,
        *,
        automation_id,
        initial_mode,
        source_set_sha256,
        source_snapshot,
        policy_version,
    ):
        item = {
            "automation_id": automation_id,
            "initial_mode": initial_mode,
            "source_set_sha256": source_set_sha256,
            "source_snapshot_json": copy.deepcopy(source_snapshot),
            "policy_version": policy_version,
            "completed_at": datetime.now(timezone.utc),
        }
        self.state.items[automation_id] = item
        return copy.deepcopy(item)

    def create_bootstrap_marker_018(
        self,
        *,
        release_sha,
        project_set_sha256,
        completed_by,
    ):
        marker = {
            "marker_id": 1,
            "release_sha": release_sha,
            "project_set_sha256": project_set_sha256,
            "completed_by": completed_by,
            "completed_at": datetime.now(timezone.utc),
        }
        self.state.marker = marker
        return copy.deepcopy(marker)


class _BootstrapPluginsPort:
    def __init__(self, repository: "_BootstrapRepository") -> None:
        self.repository = repository

    def get_project(self, automation_id, **_kwargs):
        return copy.deepcopy(self.repository.state.projects[automation_id])


class _NoBootstrapDomainEventsPort:
    def __init__(self, repository: "_BootstrapRepository") -> None:
        self.repository = repository

    def append_with_outbox(self, _event, _outbox):
        self.repository.state.domain_event_calls += 1


class _BootstrapUow:
    def __init__(self, repository: "_BootstrapRepository") -> None:
        self.repository = repository
        self.automation_projects = _BootstrapProjectsPort(repository)
        self.automation_plugins = _BootstrapPluginsPort(repository)
        self.events = _NoBootstrapDomainEventsPort(repository)
        self._snapshot: _BootstrapState | None = None

    def __enter__(self):
        self.repository.uow_entries += 1
        self._snapshot = copy.deepcopy(self.repository.state)
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is not None and self._snapshot is not None:
            self.repository.state = self._snapshot
        return False

    def commit(self):
        self.repository.state.commits += 1


class _BootstrapRepository:
    def __init__(self) -> None:
        self.state = _BootstrapState()
        self.uow_entries = 0

    def unit_of_work(self):
        return _BootstrapUow(self)


class _BootstrapCatalog:
    def __init__(self, state: _BootstrapState) -> None:
        self.state = state

    def require(self, automation_id):
        return self.state.entries[automation_id]

    def get(self, automation_id):
        return self.state.entries.get(automation_id)

    def list(self):
        return tuple(self.state.entries[key] for key in sorted(self.state.entries))


def _bootstrap_service(
    repository: _BootstrapRepository,
) -> AutomationProjectPolicyService:
    service = AutomationProjectPolicyService(
        repository,
        core_catalog=SimpleNamespace(),
        plugin_catalog=_BootstrapCatalog(repository.state),
        release_hold_provider=lambda: True,
    )
    service._lock_and_compile_contract = (  # type: ignore[method-assign]
        lambda _uow, entry, **_kwargs: (
            repository.state.contracts[entry.automation_id],
            copy.deepcopy(repository.state.configs[entry.automation_id]),
        )
    )
    return service


class AutomationProjectPolicyBootstrapTests(TestCase):
    def setUp(self) -> None:
        self.repository = _BootstrapRepository()
        self.service = _bootstrap_service(self.repository)
        self.release_ids = tuple(sorted(release_first_party_automation_ids()))

    def _bootstrap(self, *, release_sha: str = RELEASE_SHA):
        return self.service.bootstrap_legacy_project_policies(
            expected_automation_ids=self.release_ids,
            release_sha=release_sha,
        )

    def test_exact_release_bootstrap_is_atomic_audited_and_future_idempotent(self):
        result = self._bootstrap()

        self.assertEqual(16, len(self.release_ids))
        self.assertEqual(
            57,
            sum(len(rows) for rows in self.repository.state.rows.values()),
        )
        self.assertEqual(
            {
                "status": "created",
                "project_count": 16,
                "legacy_schedule_only": 10,
                "require_each_run": 6,
                "retired_scheduled_exact": 55,
                "project_set_sha256": self.repository.state.marker[
                    "project_set_sha256"
                ],
            },
            result,
        )
        modes = [item["initial_mode"] for item in self.repository.state.items.values()]
        self.assertEqual(10, modes.count("LEGACY_SCHEDULE_ONLY"))
        self.assertEqual(6, modes.count("REQUIRE_EACH_RUN"))
        self.assertEqual(0, self.repository.state.domain_event_calls)
        self.assertEqual(
            AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY,
            self.repository.state.marker["completed_by"],
        )
        for automation_id, item in self.repository.state.items.items():
            snapshot = validate_automation_project_bootstrap_source_snapshot(
                item["source_snapshot_json"]
            )
            self.assertEqual(automation_id, snapshot["automation_id"])
            self.assertEqual(
                item["initial_mode"],
                automation_project_bootstrap_initial_mode(snapshot),
            )
            self.assertEqual(
                item["source_set_sha256"],
                automation_project_bootstrap_source_snapshot_sha256(snapshot),
            )
            for task in snapshot["scheduled_tasks"]:
                self.assertEqual(
                    f"automation.{automation_id}.run",
                    task["tool_name"],
                )
                legacy_row = next(
                    row
                    for row in self.repository.state.legacy_rows[automation_id]
                    if row["id"] == task["task_id"]
                )
                self.assertNotEqual(
                    legacy_row["tool_name"],
                    task["tool_name"],
                )
            bootstrap_request = automation_project_policy_bootstrap_request_id(
                automation_id
            )
            bootstrap_event = next(
                event
                for event in self.repository.state.policy_events[automation_id]
                if event["request_id"] == bootstrap_request
            )
            self.assertEqual(
                AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID,
                bootstrap_event["actor_id"],
            )
            self.assertEqual(
                AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ROLE,
                bootstrap_event["actor_role"],
            )
            self.assertEqual(
                AUTOMATION_PROJECT_BOOTSTRAP_REASON,
                bootstrap_event["reason"],
            )
            self.assertEqual(
                AUTOMATION_PROJECT_BOOTSTRAP_EVENT_COMMENT,
                bootstrap_event["comment"],
            )
            self.assertEqual(bootstrap_request, bootstrap_event["correlation_id"])
            validate_persisted_automation_project_configuration_evidence(
                source_snapshot=snapshot,
                release_sha=RELEASE_SHA,
                policy_events=self.repository.state.policy_events[automation_id],
                evidence_rows=self.repository.state.configuration_evidence[
                    automation_id
                ],
                generation_schedule_sha256=self.repository.state.configs[
                    automation_id
                ]["desired_schedule_sha256"],
            )

        state_before_future_release = copy.deepcopy(self.repository.state)
        replay = self._bootstrap(release_sha=FUTURE_RELEASE_SHA)
        self.assertEqual("already_present", replay["status"])
        self.assertEqual(RELEASE_SHA, replay["release_sha"])
        self.assertEqual(10, replay["legacy_schedule_only"])
        self.assertEqual(6, replay["require_each_run"])
        self.assertEqual(
            state_before_future_release.items,
            self.repository.state.items,
        )
        self.assertEqual(
            state_before_future_release.policy_events,
            self.repository.state.policy_events,
        )
        self.assertEqual(0, self.repository.state.domain_event_calls)

    def test_release_sha_is_full_git_sha_before_transaction(self):
        for invalid in ("a" * 39, "a" * 41, True):
            with self.subTest(invalid=invalid):
                repository = _BootstrapRepository()
                service = _bootstrap_service(repository)
                with self.assertRaises(OrchestrationError) as raised:
                    service.bootstrap_legacy_project_policies(
                        expected_automation_ids=repository.state.release_ids,
                        release_sha=invalid,  # type: ignore[arg-type]
                    )
                self.assertEqual(
                    "PROJECT_POLICY_BOOTSTRAP_RELEASE_INVALID",
                    raised.exception.code,
                )
                self.assertEqual(0, repository.uow_entries)

    def test_partial_item_or_late_evidence_failure_rolls_back_every_write(self):
        partial_repository = _BootstrapRepository()
        partial_repository.state.items["orphan"] = {
            "automation_id": "orphan"
        }
        partial_service = _bootstrap_service(partial_repository)
        with self.assertRaises(OrchestrationError) as raised:
            partial_service.bootstrap_legacy_project_policies(
                expected_automation_ids=partial_repository.state.release_ids,
                release_sha=RELEASE_SHA,
            )
        self.assertEqual("PROJECT_POLICY_BOOTSTRAP_PARTIAL", raised.exception.code)
        self.assertIsNone(partial_repository.state.marker)
        self.assertEqual({"orphan"}, set(partial_repository.state.items))

        late_automation_id = sorted(self.repository.state.release_ids)[-1]
        self.repository.state.configuration_evidence[late_automation_id][0][
            "configuration_metadata_json"
        ]["request_payload_sha256"] = "f" * 64
        before = copy.deepcopy(self.repository.state)
        with self.assertRaises(OrchestrationError) as raised:
            self._bootstrap()
        self.assertEqual(
            "PROJECT_POLICY_BOOTSTRAP_SOURCE_INVALID",
            raised.exception.code,
        )
        self.assertEqual(before.__dict__, self.repository.state.__dict__)
        self.assertIsNone(self.repository.state.marker)
        self.assertEqual({}, self.repository.state.items)
        self.assertEqual(0, self.repository.state.domain_event_calls)

    def test_marker_mode_snapshot_and_contract_event_tamper_fail_closed(self):
        self._bootstrap()
        marker = copy.deepcopy(self.repository.state.marker)
        items = [
            copy.deepcopy(self.repository.state.items[key])
            for key in sorted(self.repository.state.items)
        ]
        expected_ids = tuple(item["automation_id"] for item in items)

        swapped = copy.deepcopy(items)
        swapped[0]["source_snapshot_json"], swapped[1][
            "source_snapshot_json"
        ] = (
            swapped[1]["source_snapshot_json"],
            swapped[0]["source_snapshot_json"],
        )
        mode_tampered = copy.deepcopy(items)
        mode_tampered[0]["initial_mode"] = (
            "REQUIRE_EACH_RUN"
            if mode_tampered[0]["initial_mode"] == "LEGACY_SCHEDULE_ONLY"
            else "LEGACY_SCHEDULE_ONLY"
        )
        schema_tampered = copy.deepcopy(items)
        schema_tampered[0]["source_snapshot_json"]["schema_version"] = True
        for tampered in (swapped, mode_tampered, schema_tampered):
            with self.subTest(tamper=tampered[0]["automation_id"]):
                with self.assertRaises(
                    AutomationProjectBootstrapContractError
                ):
                    validate_existing_automation_project_bootstrap(
                        marker,
                        tampered,
                        expected_automation_ids=expected_ids,
                    )

        legacy_item = next(
            item for item in items if item["initial_mode"] == "LEGACY_SCHEDULE_ONLY"
        )
        event = next(
            copy.deepcopy(event)
            for event in self.repository.state.policy_events[
                legacy_item["automation_id"]
            ]
            if event["request_id"]
            == automation_project_policy_bootstrap_request_id(
                legacy_item["automation_id"]
            )
        )
        event["tool_contract_hash"] = "0" * 64
        with self.assertRaises(AutomationProjectBootstrapContractError):
            validate_automation_project_bootstrap_policy_event(
                event,
                item=legacy_item,
            )

    def test_main_bootstraps_only_after_reconcile_and_before_policy_engine(self):
        source = (ROOT / "agent" / "main.py").read_text(encoding="utf-8")
        reconcile = source.index("plugin_runtime.reconcile")
        bootstrap = source.index("bootstrap_legacy_project_policies")
        policy_engine = source.index("PolicyEngine(")
        self.assertLess(reconcile, bootstrap)
        self.assertLess(bootstrap, policy_engine)
        self.assertIn("scheduler_release_hold_requested()", source)
        self.assertIn("len(bootstrap_automation_ids) != 16", source)

    def test_post_018_initial_preflight_replays_real_typed_rows_and_legacy_backup(self):
        self._bootstrap()
        preflight = _load_release_preflight()
        contract = dict(preflight._load_release_contract())
        contract["validate_generation_row"] = lambda row: row

        schedules: dict[str, dict] = {}
        backups: dict[str, dict] = {}
        scheduled_policies: dict[str, dict] = {}
        scheduled_events: dict[str, list[dict]] = {}
        projects: dict[str, dict] = {}
        generations: dict[str, dict] = {}
        for automation_id in self.release_ids:
            rows = self.repository.state.rows[automation_id]
            for row in rows:
                task_id = row["id"]
                schedules[task_id] = copy.deepcopy(row)
                scheduled_policies[task_id] = {
                    "scheduled_policy_mode": row["scheduled_policy_mode"],
                    "scheduled_policy_version": row["scheduled_policy_version"],
                    "scheduled_contract_hash": row["scheduled_contract_hash"],
                    "scheduled_contract_snapshot_json": row[
                        "scheduled_contract_snapshot_json"
                    ],
                    "scheduled_tool_contract_hash": row[
                        "scheduled_tool_contract_hash"
                    ],
                }
                scheduled_events[task_id] = []
            for row in self.repository.state.legacy_rows[automation_id]:
                backups[row["id"]] = copy.deepcopy(row)
            for event in self.repository.state.scheduled_events[automation_id]:
                scheduled_events[event["task_id"]].append(copy.deepcopy(event))

            config = copy.deepcopy(self.repository.state.configs[automation_id])
            config.update(
                {
                    "generation": AUTOMATION_GENERATION,
                    "config_version": CONFIGURATION_VERSION,
                }
            )
            projects[automation_id] = config
            compiled = {}
            if rows:
                compiled["scheduler"] = {
                    "arguments": copy.deepcopy(rows[0]["tool_params"]),
                    "dynamic_resolvers": {},
                }
            generations[automation_id] = {
                "generation": AUTOMATION_GENERATION,
                "generation_state": "COMMITTED",
                "plugin_id": FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES[
                    automation_id
                ].tool_name,
                "committed_at": datetime.now(timezone.utc),
                "schedule_sha256": config["desired_schedule_sha256"],
                "snapshot_json": {
                    "execution_metadata": {
                        "project_config_version": CONFIGURATION_VERSION,
                        "schedule": copy.deepcopy(
                            config["desired_schedule_json"]
                        ),
                        "compiled_invocations": compiled,
                    }
                },
            }

        items_by_id = {
            automation_id: copy.deepcopy(item)
            for automation_id, item in self.repository.state.items.items()
        }
        policy_events = copy.deepcopy(self.repository.state.policy_events)
        configuration_evidence = copy.deepcopy(
            self.repository.state.configuration_evidence
        )
        policies = copy.deepcopy(self.repository.state.policies)
        with (
            patch.object(
                preflight,
                "_read_bootstrap_artifacts",
                return_value=(
                    copy.deepcopy(self.repository.state.marker),
                    items_by_id,
                ),
            ),
            patch.object(
                preflight,
                "_read_project_policy_evidence",
                return_value=(policies, policy_events, configuration_evidence),
            ),
            patch.object(
                preflight,
                "_read_scheduled_policy_evidence",
                return_value=(scheduled_policies, scheduled_events),
            ),
            patch.object(
                preflight,
                "_read_bootstrap_generations",
                return_value=generations,
            ),
        ):
            policy_count = preflight._validate_bootstrap_and_policy_state(
                object(),
                contract=contract,
                schedules=schedules,
                backups=backups,
                projects=projects,
                expect_initial_production_manifest=True,
            )

        self.assertEqual(16, policy_count)


class _PolicyCatalog:
    def __init__(self, capabilities: dict[str, dict]) -> None:
        self.capabilities = capabilities

    def get_capability(self, tool_name):
        return self.capabilities.get(tool_name)


def _capability(*, approval_mode: str = "required") -> dict:
    return {
        "approval": {
            "mode": approval_mode,
            "required_role": "super_admin",
        },
        "permissions": {"required_roles": ["admin"]},
    }


def _plan_for_contract(
    contract: CompiledAutomationProjectContract,
    arguments: dict,
    *,
    tool_name: str | None = None,
    operation_type: OperationType = OperationType.EXTERNAL_WRITE,
    risk_level: RiskLevel = RiskLevel.HIGH,
) -> Plan:
    return Plan(
        command_type="automation.project.invoke",
        context_fingerprint="bootstrap-policy-context",
        tool_catalog_hash="bootstrap-policy-catalog",
        steps=(
            PlanStep(
                step_key="execute",
                tool_name=tool_name or contract.tool_name,
                tool_version=contract.tool_version,
                operation_type=operation_type,
                arguments=arguments,
                account_id=None,
                depends_on=(),
                idempotency_key="bootstrap-policy-step",
                expected_evidence=(),
                postconditions=(),
                risk_level=risk_level,
                requires_approval=True,
            ),
        ),
        automation_id=contract.automation_id,
        automation_generation=contract.automation_generation,
        automation_contract_hash=contract.contract_hash,
    )


def _invocation_for(
    contract: CompiledAutomationProjectContract,
    policy: dict,
    *,
    entrypoint: AutomationEntrypoint,
    task_id: str | None = None,
) -> AutomationProjectInvocation:
    contract_id = (
        f"scheduler:{task_id}"
        if entrypoint is AutomationEntrypoint.SCHEDULER
        else entrypoint.value
    )
    return AutomationProjectInvocation(
        automation_id=contract.automation_id,
        automation_generation=contract.automation_generation,
        entrypoint=entrypoint,
        contract_id=contract_id,
        contract_hash=contract.contract_hash,
        policy_version=policy["version"],
        project_configuration_version=contract.project_configuration_version,
        request_id=f"policy-{contract.automation_id}-{entrypoint.value}",
    )


class AutomationProjectLegacyPolicyEngineTests(TestCase):
    def setUp(self) -> None:
        self.repository = _BootstrapRepository()
        self.service = _bootstrap_service(self.repository)
        self.service.bootstrap_legacy_project_policies(
            expected_automation_ids=self.repository.state.release_ids,
            release_sha=RELEASE_SHA,
        )
        capabilities = {
            contract.tool_name: _capability()
            for contract in self.repository.state.contracts.values()
        }
        capabilities.update(
            {
                "hard-disabled": _capability(approval_mode="disabled"),
                "hard-destructive": _capability(),
                "hard-extreme": _capability(),
            }
        )
        self.provider_calls = 0

        def provider(*args, **kwargs):
            self.provider_calls += 1
            return self.service.evaluate_invocation(*args, **kwargs)

        self.engine = PolicyEngine(
            _PolicyCatalog(capabilities),
            project_policy_provider=provider,
        )

    def _evaluate(
        self,
        automation_id: str,
        entrypoint: AutomationEntrypoint,
    ):
        contract = self.repository.state.contracts[automation_id]
        policy = self.repository.state.policies[automation_id]
        arguments = _task_arguments(automation_id)
        task_id = (
            self.repository.state.rows[automation_id][0]["id"]
            if entrypoint is AutomationEntrypoint.SCHEDULER
            else None
        )
        actor_type = {
            AutomationEntrypoint.SCHEDULER: ActorType.SCHEDULER,
            AutomationEntrypoint.CONSOLE: ActorType.CONSOLE_ADMIN,
            AutomationEntrypoint.FEISHU: ActorType.FEISHU_USER,
            AutomationEntrypoint.WEBHOOK: ActorType.WEBHOOK,
        }[entrypoint]
        actor = Actor(
            actor_type,
            task_id or f"{entrypoint.value}-actor",
            roles=("admin",),
        )
        context = {"task_id": task_id} if task_id else {}
        return self.engine.evaluate(
            _plan_for_contract(contract, arguments),
            actor,
            source=entrypoint.value,
            execution_context=context,
            automation_invocation=_invocation_for(
                contract,
                policy,
                entrypoint=entrypoint,
                task_id=task_id,
            ),
        )

    def test_legacy_scheduler_overrides_baseline_but_other_entrypoints_require(self):
        scheduler = self._evaluate("send_order", AutomationEntrypoint.SCHEDULER)
        console = self._evaluate("send_order", AutomationEntrypoint.CONSOLE)
        feishu = self._evaluate("send_order", AutomationEntrypoint.FEISHU)
        webhook = self._evaluate("delivery_status", AutomationEntrypoint.WEBHOOK)

        self.assertTrue(scheduler.allowed)
        self.assertFalse(scheduler.requires_approval)
        for decision in (console, feishu, webhook):
            self.assertTrue(decision.allowed)
            self.assertTrue(decision.requires_approval)

        self.repository.state.policies["send_order"]["contract_hash"] = "0" * 64
        stale = self._evaluate("send_order", AutomationEntrypoint.SCHEDULER)
        self.assertTrue(stale.allowed)
        self.assertTrue(stale.requires_approval)

    def test_disabled_destructive_and_extreme_reject_before_project_provider(self):
        contract = self.repository.state.contracts["send_order"]
        policy = self.repository.state.policies["send_order"]
        invocation = _invocation_for(
            contract,
            policy,
            entrypoint=AutomationEntrypoint.CONSOLE,
        )
        actor = Actor(
            ActorType.CONSOLE_ADMIN,
            "admin",
            roles=("admin", "super_admin"),
        )
        cases = (
            ("hard-disabled", OperationType.EXTERNAL_WRITE, RiskLevel.HIGH),
            ("hard-destructive", OperationType.DESTRUCTIVE, RiskLevel.HIGH),
            ("hard-extreme", OperationType.EXTERNAL_WRITE, RiskLevel.EXTREME),
        )
        for tool_name, operation_type, risk_level in cases:
            with self.subTest(tool_name=tool_name):
                before = self.provider_calls
                decision = self.engine.evaluate(
                    _plan_for_contract(
                        contract,
                        _task_arguments("send_order"),
                        tool_name=tool_name,
                        operation_type=operation_type,
                        risk_level=risk_level,
                    ),
                    actor,
                    source="console",
                    automation_invocation=invocation,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual("OPERATION_DISABLED", decision.code)
                self.assertEqual(before, self.provider_calls)


class AutomationProjectBootstrapPureContractTests(TestCase):
    def test_legacy_grant_accepts_independent_uuid_correlation_only(self):
        row = {
            "id": "legacy_task",
            "tool_name": "tms_query",
            "tool_params": {"query": "reviewed"},
            "cron_expression": "5 21 * * *",
            "enabled": 1,
            "configuration_version": CONFIGURATION_VERSION - 1,
        }
        event = _scheduled_grant_event(row, event_id=1)

        self.assertNotEqual(event["request_id"], event["correlation_id"])
        validate_legacy_scheduled_grant_event(event, row=row)

        event["correlation_id"] = "not-a-uuid"
        with self.assertRaises(AutomationProjectBootstrapContractError):
            validate_legacy_scheduled_grant_event(event, row=row)

    def test_project_set_hash_is_canonical_and_binds_first_release(self):
        repository = _BootstrapRepository()
        service = _bootstrap_service(repository)
        service.bootstrap_legacy_project_policies(
            expected_automation_ids=repository.state.release_ids,
            release_sha=RELEASE_SHA,
        )
        items = list(repository.state.items.values())
        digest = automation_project_bootstrap_project_set_sha256(
            RELEASE_SHA,
            items,
        )
        self.assertEqual(repository.state.marker["project_set_sha256"], digest)
        self.assertNotEqual(
            digest,
            automation_project_bootstrap_project_set_sha256(
                FUTURE_RELEASE_SHA,
                items,
            ),
        )

    def test_policy_comment_is_bound_to_authoritative_event_and_policy(self):
        repository = _BootstrapRepository()
        service = _bootstrap_service(repository)
        service.bootstrap_legacy_project_policies(
            expected_automation_ids=repository.state.release_ids,
            release_sha=RELEASE_SHA,
        )
        legacy_policy = next(
            policy
            for policy in repository.state.policies.values()
            if policy["mode"] == "LEGACY_SCHEDULE_ONLY"
        )
        self.assertEqual(
            AUTOMATION_PROJECT_BOOTSTRAP_POLICY_COMMENT,
            legacy_policy["comment"],
        )
