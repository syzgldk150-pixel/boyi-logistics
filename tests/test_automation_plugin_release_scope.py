from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.automation_plugins.catalog import CompositeToolRegistry
from agent.automation_plugins.first_party import (
    deferred_first_party_plugin_ids,
    release_first_party_automation_ids,
    release_first_party_broker_action_keys,
    release_first_party_instance_seeds,
    release_first_party_plugin_ids,
    resolve_release_first_party_manifests,
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
from shared.orchestration_schema import (
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


def test_only_release_scoped_plugins_can_be_packaged_bootstrapped_or_brokered() -> None:
    catalog = ToolRegistry()
    manifests = resolve_release_first_party_manifests(catalog)
    seeds = release_first_party_instance_seeds()

    assert set(manifests) == release_first_party_plugin_ids()
    assert {seed.plugin_id for seed in seeds} <= release_first_party_plugin_ids()
    assert {seed.automation_id for seed in seeds} == release_first_party_automation_ids()
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
    for source, actor_type in (
        ("legacy_api", ActorType.LEGACY_API),
        ("scheduler", ActorType.SCHEDULER),
    ):
        command = Command(
            command_type="tool.execute",
            source=source,
            actor=Actor(actor_type, f"{source}-caller"),
            parameters={"tool_name": "r7_arrival_checkin", "arguments": {}},
            idempotency_key=f"deferred-r7:{source}",
        )
        with pytest.raises(OrchestrationError) as raised:
            planner.plan(command, ContextSnapshot(values={}))
        assert raised.value.code == "UNKNOWN_TOOL"
