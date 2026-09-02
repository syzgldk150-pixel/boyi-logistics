from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from service_v2_plugins._shared.build_zip import build_plugin_zip


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent" / "service_v2_plugins" / "split_pending_problem_upload_v2"
SHARED_MAIN = (
    ROOT / "agent" / "service_v2_plugins" / "_shared" / "split_pending_service_main.py"
)
V1_ACTION = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "split_pending_problem_upload"
    / "payload"
    / "action.py"
)
RESULT_SOURCE = ROOT / "agent" / "first_party_automation_plugins" / "_runtime" / "result.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(output: Path) -> bytes:
    built = build_plugin_zip(SOURCE, output)
    assert built == output.resolve()
    return built.read_bytes()


def test_split_pending_v2_zip_is_deterministic_and_embeds_reviewed_v1_bytes(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path / "split-pending-first.zip")
    second = _build(tmp_path / "split-pending-second.zip")
    assert first == second

    with zipfile.ZipFile(tmp_path / "split-pending-first.zip") as archive:
        expected_members = {
            "manifest.json",
            "payload/action.py",
            "payload/boyi_plugin_result.py",
            "payload/boyi_plugin_sdk.py",
            "payload/main.py",
            "payload/plugin.py",
            "settings/index.html",
            "settings/settings.css",
            "settings/settings.js",
        }
        assert set(archive.namelist()) == expected_members
        assert archive.read("payload/action.py") == V1_ACTION.read_bytes()
        assert archive.read("payload/boyi_plugin_result.py") == RESULT_SOURCE.read_bytes()
        for member in sorted(expected_members):
            source = archive.read(member)
            if member.endswith(".py"):
                compile(source, f"split_pending_problem_upload_v2.zip/{member}", "exec")


def test_split_pending_manifest_closes_operations_selection_and_454_call_budget() -> None:
    source = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    manifest = AutomationPluginManifestV2.from_mapping(source)
    contract = ServiceV2ProjectContract.from_manifest(manifest)

    assert manifest.plugin_id == "split_pending_problem_upload_v2"
    assert manifest.provided_services == (
        "plugin.split_pending_problem_upload_v2.split_pending_problem_upload@1",
    )
    assert manifest.provides == (
        {
            "service": "plugin.split_pending_problem_upload_v2.split_pending_problem_upload@1",
            "operations": (
                {"name": "preview", "effect": "read"},
                {"name": "execute", "effect": "external_write"},
            ),
        },
    )
    assert tuple(dict(item) for item in manifest.connector_requirements) == (
        {
            "service": "connector.boyi.split_pending_source_sheet@1",
            "binding_kind": "resource",
            "resource_role": "split_pending_source_sheet",
        },
        {
            "service": "connector.boyi.split_pending_target_sheet@1",
            "binding_kind": "resource",
            "resource_role": "split_pending_target_sheet",
        },
        {
            "service": "connector.boyi.split_pending_projection@1",
            "binding_kind": "host_internal",
        },
        {
            "service": "connector.boyi.split_pending_ronghui@1",
            "account_role": "account_id",
        },
        {
            "service": "connector.boyi.split_pending_problem_ledger@1",
            "account_role": "account_id",
        },
    )
    expected_limits = {
        "read_rows": 1,
        "snapshot_read": 1,
        "problem_query": 90,
        "snapshot_replace": 1,
        "replace_rows": 1,
        "problem_create": 90,
        "problem_verify": 90,
        "event_upsert": 90,
        "result_upsert": 90,
    }
    assert manifest.capabilities == (
        {
            "name": "service.invoke",
            "operations": tuple(expected_limits),
            "account_role": None,
            "resource_role": None,
            "action_call_limits": expected_limits,
        },
    )
    assert sum(expected_limits.values()) == 454
    assert contract.runtime_permissions["max_broker_calls"] == 454
    assert {
        item["action"]: item["per_action_limit"]
        for item in contract.runtime_permissions["broker_operations"]
    } == expected_limits

    assert contract.allowed_entrypoints == (
        "execute_console",
        "execute_feishu",
        "assistant_preview",
    )
    assert contract.default_entrypoints == ("assistant_preview",)
    assert manifest.contributes["console"] == (
        {
            "id": "execute_console",
            "title": "预览分批问题件候选",
            "service": manifest.provided_services[0],
            "operation": "execute",
            "selection_preview_operation": "preview",
            "default_enabled": False,
        },
    )
    assert manifest.contributes["feishu"] == (
        {
            "id": "execute_feishu",
            "service": manifest.provided_services[0],
            "operation": "execute",
            "commands": ("分批",),
            "selection_preview_operation": "preview",
            "default_enabled": False,
        },
    )
    assert manifest.contributes["scheduler"] == ()
    assert manifest.contributes["webhook"] == ()
    assert manifest.contributes["events"] == ()
    assert contract.invocation_contracts["execute_console"]["effect"] == "external_write"
    assert contract.invocation_contracts["execute_feishu"]["effect"] == "external_write"

    wrong_preview = copy.deepcopy(source)
    wrong_preview["contributes"]["console"][0]["selection_preview_operation"] = "execute"
    with pytest.raises(PluginManifestError, match="selection preview"):
        AutomationPluginManifestV2.from_mapping(wrong_preview)

    wrong_effect = copy.deepcopy(source)
    wrong_effect["provides"][0]["operations"][0]["effect"] = "external_write"
    with pytest.raises(PluginManifestError, match="selection preview"):
        AutomationPluginManifestV2.from_mapping(wrong_effect)


def test_split_pending_v2_payload_has_no_legacy_import_path_or_whole_tool_fallback() -> None:
    for path in (SOURCE / "payload" / "plugin.py", SHARED_MAIN):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            str(node.module or "").split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert "agent" not in imported_roots
        assert "tools" not in imported_roots
        for forbidden in (
            "first_party_automation_plugins",
            "sys.path",
            "split_pending_problem_upload_tool",
            "call_http_service",
            "run_once",
        ):
            assert forbidden not in source


def test_connector_adapter_maps_all_primitives_without_forwarding_v1_roles() -> None:
    plugin = _load_module("split_pending_plugin_test", SOURCE / "payload" / "plugin.py")
    calls: list[dict[str, object]] = []

    def broker(
        operation: str,
        *,
        action: str,
        role: str,
        arguments: dict[str, object],
    ) -> object:
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": copy.deepcopy(arguments),
            }
        )
        return {"evidence_ref": f"host-evidence:{len(calls)}"}

    cases = (
        (
            "network.request",
            "feishu.sheet.read_rows",
            "split_pending_source_sheet",
            plugin.SOURCE_CONNECTOR,
            "read_rows",
        ),
        (
            "projection.invoke",
            "split_pending.snapshot.read",
            "split_pending_target_sheet",
            plugin.PROJECTION_CONNECTOR,
            "snapshot_read",
        ),
        (
            "browser.invoke",
            "ronghui.problem.query",
            "account_id",
            plugin.RONGHUI_CONNECTOR,
            "problem_query",
        ),
        (
            "projection.invoke",
            "split_pending.snapshot.replace",
            "split_pending_target_sheet",
            plugin.PROJECTION_CONNECTOR,
            "snapshot_replace",
        ),
        (
            "network.request",
            "feishu.sheet.replace_rows",
            "split_pending_target_sheet",
            plugin.TARGET_CONNECTOR,
            "replace_rows",
        ),
        (
            "browser.invoke",
            "ronghui.problem.create",
            "account_id",
            plugin.RONGHUI_CONNECTOR,
            "problem_create",
        ),
        (
            "browser.invoke",
            "ronghui.problem.verify",
            "account_id",
            plugin.RONGHUI_CONNECTOR,
            "problem_verify",
        ),
        (
            "ledger.invoke",
            "daily_sign.problem_event.upsert",
            "account_id",
            plugin.LEDGER_CONNECTOR,
            "event_upsert",
        ),
        (
            "projection.invoke",
            "split_pending.result.upsert",
            "split_pending_target_sheet",
            plugin.PROJECTION_CONNECTOR,
            "result_upsert",
        ),
    )
    for index, (operation, action, role, service, connector_operation) in enumerate(cases):
        plugin.service_invoke_adapter(
            broker,
            operation,
            action=action,
            role=role,
            arguments={"bill_code": "R_SYNTHETIC"},
            preflight_services=(
                plugin.EXECUTE_PREFLIGHT_SERVICES if index == 0 else ()
            ),
        )
        call = calls[-1]
        assert call["operation"] == "service.invoke"
        assert call["action"] == connector_operation
        assert call["role"] == plugin.SYSTEM_ROLE
        assert call["arguments"]["service"] == service
        assert call["arguments"]["operation"] == connector_operation

    assert calls[0]["arguments"]["preflight_services"] == list(
        plugin.EXECUTE_PREFLIGHT_SERVICES
    )
    assert all("preflight_services" not in call["arguments"] for call in calls[1:])
    nested = json.dumps(
        [call["arguments"]["arguments"] for call in calls],
        ensure_ascii=False,
    )
    assert "account_id" not in nested
    assert "split_pending_source_sheet" not in nested
    assert "split_pending_target_sheet" not in nested

    with pytest.raises(ValueError, match="primitive"):
        plugin.service_invoke_adapter(
            broker,
            "network.request",
            action="feishu.sheet.read_rows",
            role="split_pending_target_sheet",
            arguments={},
        )
    with pytest.raises(ValueError, match="preflight"):
        plugin.service_invoke_adapter(
            broker,
            "network.request",
            action="feishu.sheet.read_rows",
            role="split_pending_source_sheet",
            arguments={},
            preflight_services=(plugin.SOURCE_CONNECTOR,),
        )


def test_mutating_connector_set_starts_at_every_projection_or_external_write() -> None:
    plugin = _load_module(
        "split_pending_plugin_mutation_test",
        SOURCE / "payload" / "plugin.py",
    )
    assert plugin.MUTATING_CONNECTOR_OPERATIONS == frozenset(
        {
            "snapshot_replace",
            "replace_rows",
            "problem_create",
            "event_upsert",
            "result_upsert",
        }
    )
    assert {
        "read_rows",
        "snapshot_read",
        "problem_query",
        "problem_verify",
    }.isdisjoint(plugin.MUTATING_CONNECTOR_OPERATIONS)
