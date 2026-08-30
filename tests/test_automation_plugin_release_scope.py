from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.automation_plugins.catalog import CompositeToolRegistry
from agent.automation_plugins.first_party import (
    _broker_action,
    deferred_first_party_plugin_ids,
    release_first_party_automation_ids,
    release_first_party_broker_action_keys,
    release_first_party_instance_seeds,
    release_first_party_plugin_ids,
    resolve_first_party_manifests,
    resolve_release_first_party_manifests,
)
from agent.automation_plugins.errors import PluginPackageError
from agent.automation_plugins.daily_send_handlers import (
    MARKED_WRITE_ACTION_KEYS as DAILY_SEND_MARKED_WRITE_ACTION_KEYS,
)
from agent.automation_plugins.delivery_site_handlers import (
    MARKED_WRITE_ACTION_KEYS as DELIVERY_SITE_MARKED_WRITE_ACTION_KEYS,
)
from agent.automation_plugins.first_party_handlers import (
    MARKED_WRITE_ACTION_KEYS as CORE_MARKED_WRITE_ACTION_KEYS,
)
from agent.automation_plugins.problem_handlers import (
    MARKED_WRITE_ACTION_KEYS as PROBLEM_MARKED_WRITE_ACTION_KEYS,
)
from agent.automation_plugins.release_scope import (
    DEFERRED_R7_PLUGIN_IDS,
    WINDOWS_WORKER_RELEASE_ENABLED,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OrchestrationError,
)
from agent.orchestration.planner import DeterministicPlanner
from agent.tool_registry import ToolRegistry
from plugin_core_adapters.finance import (
    MARKED_WRITE_ACTION_KEYS as FINANCE_MARKED_WRITE_ACTION_KEYS,
)
from shared.orchestration_schema import (
    REQUIRED_COLUMNS,
    REQUIRED_TABLES,
    WINDOWS_WORKER_REQUIRED_COLUMNS,
    WINDOWS_WORKER_REQUIRED_TABLES,
    orchestration_schema_requirements,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_MATRIX = (
    REPOSITORY_ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "MIGRATION_MATRIX.md"
)


def _matrix_states() -> dict[str, str]:
    states: dict[str, str] = {}
    row_pattern = re.compile(
        r"^\| `(?P<plugin>[^`]+)` \|.*\| `(?P<state>RUNNABLE|BLOCKED)`;"
    )
    for line in MIGRATION_MATRIX.read_text(encoding="utf-8").splitlines():
        match = row_pattern.match(line)
        if match:
            states[match.group("plugin")] = match.group("state")
    return states


def test_release_scope_matches_reviewed_runnable_matrix_and_defers_r7() -> None:
    states = _matrix_states()
    runnable = {plugin_id for plugin_id, state in states.items() if state == "RUNNABLE"}
    blocked = {plugin_id for plugin_id, state in states.items() if state == "BLOCKED"}

    assert states
    assert release_first_party_plugin_ids() == runnable
    assert release_first_party_plugin_ids().isdisjoint(blocked)
    assert DEFERRED_R7_PLUGIN_IDS <= blocked
    assert release_first_party_plugin_ids().isdisjoint(DEFERRED_R7_PLUGIN_IDS)
    assert WINDOWS_WORKER_RELEASE_ENABLED is False


def test_server_release_schema_does_not_require_windows_worker_extension() -> None:
    server_tables, server_columns = orchestration_schema_requirements(
        include_windows_worker=WINDOWS_WORKER_RELEASE_ENABLED
    )

    assert server_tables.isdisjoint(WINDOWS_WORKER_REQUIRED_TABLES)
    assert server_columns.isdisjoint(WINDOWS_WORKER_REQUIRED_COLUMNS)


def test_startup_schema_requires_write_attempt_receipts_and_identity_columns() -> None:
    tables, columns = orchestration_schema_requirements(include_windows_worker=False)
    expected_columns = {
        "receipt_id", "automation_id", "generation", "lease_id",
        "orchestration_run_id", "step_id", "request_id", "operation",
        "action", "argument_sha256", "target_ref_sha256", "target_ref_json",
        "outcome", "evidence_sha256",
    }

    assert "automation_write_attempt_receipts" in REQUIRED_TABLES
    assert "automation_write_attempt_receipts" in tables
    assert {
        ("automation_write_attempt_receipts", column)
        for column in expected_columns
    } <= REQUIRED_COLUMNS
    assert {
        ("automation_write_attempt_receipts", column)
        for column in expected_columns
    } <= columns


def test_startup_schema_requires_runtime_generation_transition_journal() -> None:
    tables, columns = orchestration_schema_requirements(include_windows_worker=False)
    expected_tables = {
        "automation_project_generation_transitions",
        "automation_project_generation_transition_tasks",
    }
    expected_columns = {
        ("automation_project_generation_transitions", "transition_token"),
        ("automation_project_generation_transitions", "base_committed_generation"),
        ("automation_project_generation_transitions", "phase"),
        ("automation_project_generation_transitions", "before_project_record_version"),
        ("automation_project_generation_transitions", "pending_project_record_version"),
        ("automation_project_generation_transitions", "before_policy_version"),
        ("automation_project_generation_transitions", "pending_policy_version"),
        ("automation_project_generation_transitions", "before_tasks_sha256"),
        ("automation_project_generation_transitions", "pending_tasks_sha256"),
        ("automation_project_generation_transition_tasks", "transition_token"),
        ("automation_project_generation_transition_tasks", "task_id"),
        ("automation_project_generation_transition_tasks", "automation_generation"),
        ("automation_project_generation_transition_tasks", "tool_params"),
        ("automation_project_generation_transition_tasks", "configuration_version"),
        ("automation_project_generation_transition_tasks", "policy_mode"),
        (
            "automation_project_generation_transition_tasks",
            "policy_contract_snapshot_json",
        ),
        ("automation_project_generation_transition_tasks", "policy_version"),
    }

    assert expected_tables <= REQUIRED_TABLES
    assert expected_tables <= tables
    assert expected_columns <= REQUIRED_COLUMNS
    assert expected_columns <= columns


def test_first_party_broker_effects_are_closed_and_current_actions_explicit() -> None:
    manifests = resolve_first_party_manifests(ToolRegistry())

    for manifest in manifests.values():
        for action in manifest.runtime_permissions["broker_operations"]:
            classified = _broker_action(
                str(action["operation"]),
                str(action["action"]),
                *(str(role) for role in action["roles"]),
            )
            assert classified.effect == action["effect"]

    with pytest.raises(PluginPackageError, match="no explicit effect classification"):
        _broker_action("browser.invoke", "future.unlisted.write", "account_id")


def test_every_release_signed_write_has_a_declared_handler_start_boundary() -> None:
    manifests = resolve_release_first_party_manifests(ToolRegistry())
    signed_writes = {
        (str(action["operation"]), str(action["action"]))
        for manifest in manifests.values()
        for action in manifest.runtime_permissions["broker_operations"]
        if action["effect"] == "write"
    }
    marked_writes = (
        CORE_MARKED_WRITE_ACTION_KEYS
        | DAILY_SEND_MARKED_WRITE_ACTION_KEYS
        | PROBLEM_MARKED_WRITE_ACTION_KEYS
        | DELIVERY_SITE_MARKED_WRITE_ACTION_KEYS
        | FINANCE_MARKED_WRITE_ACTION_KEYS
    )
    signed_reads = {
        (str(action["operation"]), str(action["action"]))
        for manifest in manifests.values()
        for action in manifest.runtime_permissions["broker_operations"]
        if action["effect"] == "read"
    }

    assert marked_writes == signed_writes
    assert marked_writes.isdisjoint(signed_reads)


def test_only_release_scoped_plugins_can_be_packaged_bootstrapped_or_brokered() -> None:
    catalog = ToolRegistry()
    manifests = resolve_release_first_party_manifests(catalog)
    seeds = release_first_party_instance_seeds()

    assert set(manifests) == release_first_party_plugin_ids()
    assert {seed.plugin_id for seed in seeds} <= release_first_party_plugin_ids()
    assert {seed.automation_id for seed in seeds} == release_first_party_automation_ids()
    assert "r7_departure_checkin" not in manifests
    assert all(seed.automation_id != "r7_departure_checkin" for seed in seeds)
    assert all(manifest.execution_platform == "server" for manifest in manifests.values())
    assert all(
        manifest.worker_requirement.get("required") is False
        for manifest in manifests.values()
    )

    allowed_broker_actions = release_first_party_broker_action_keys(catalog)
    declared_broker_actions = {
        (str(item["operation"]), str(item["action"]))
        for manifest in manifests.values()
        for item in manifest.runtime_permissions["broker_operations"]
    }
    assert allowed_broker_actions == declared_broker_actions
    assert ("browser.invoke", "r7.arrival.submit") not in allowed_broker_actions
    assert ("browser.invoke", "r7.departure.submit") not in allowed_broker_actions


def test_release_manifest_resolution_never_inspects_blocked_tool_contracts() -> None:
    base_catalog = ToolRegistry()
    selected = release_first_party_plugin_ids()
    inspected: set[str] = set()

    class _ReleaseOnlyCatalog:
        def get_capability(self, plugin_id: str):
            if plugin_id not in selected:
                raise AssertionError(f"blocked action was inspected: {plugin_id}")
            inspected.add(plugin_id)
            return base_catalog.get_capability(plugin_id)

    manifests = resolve_release_first_party_manifests(_ReleaseOnlyCatalog())

    assert set(manifests) == selected
    assert inspected == selected


def test_deferred_first_party_tools_are_absent_from_the_production_core_catalog() -> None:
    core_catalog = ToolRegistry()

    class _EmptyPluginCatalog:
        catalog_hash = "0" * 64

        @staticmethod
        def get_capability(_tool_name: str):
            return None

        @staticmethod
        def list_llm_capabilities() -> list[dict[str, object]]:
            return []

        @staticmethod
        def list() -> list[object]:
            return []

    blocked = deferred_first_party_plugin_ids()
    production_catalog = CompositeToolRegistry(
        core_catalog,
        _EmptyPluginCatalog(),
        blocked_core_tool_names=blocked,
    )

    assert blocked
    assert DEFERRED_R7_PLUGIN_IDS <= blocked
    assert all(production_catalog.get_capability(name) is None for name in blocked)
    assert all(
        production_catalog.get_capability(name) is not None
        for name in release_first_party_plugin_ids()
    )
    assert not (
        blocked
        & {
            str(capability.get("name") or "")
            for capability in production_catalog.list_llm_capabilities()
        }
    )
    assert blocked.isdisjoint(production_catalog.list_tools())
    assert blocked.isdisjoint(
        {
            str(item["function"]["name"])
            for item in production_catalog.get_openai_tools()
        }
    )

    planner = DeterministicPlanner(production_catalog)
    for tool_name in sorted(DEFERRED_R7_PLUGIN_IDS):
        for source, actor_type in (
            ("legacy_api", ActorType.LEGACY_API),
            ("scheduler", ActorType.SCHEDULER),
        ):
            command = Command(
                command_type="tool.execute",
                source=source,
                actor=Actor(actor_type, f"{source}-caller"),
                parameters={"tool_name": tool_name, "arguments": {}},
                idempotency_key=f"deferred-r7:{tool_name}:{source}",
            )
            with pytest.raises(OrchestrationError) as raised:
                planner.plan(command, ContextSnapshot(values={}))
            assert raised.value.code == "UNKNOWN_TOOL"
