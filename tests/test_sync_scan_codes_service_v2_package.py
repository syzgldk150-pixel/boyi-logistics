from __future__ import annotations

import ast
import copy
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from service_v2_plugins._shared.build_zip import build_plugin_zip


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent" / "service_v2_plugins" / "sync_scan_codes_v2"
SHARED_MAIN = ROOT / "agent" / "service_v2_plugins" / "_shared" / "scan_service_main.py"
V1_ACTION = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "sync_scan_codes"
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


def test_scan_v2_zip_is_deterministic_and_embeds_reviewed_v1_bytes(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path / "scan-first.zip")
    second = _build(tmp_path / "scan-second.zip")
    assert first == second

    with zipfile.ZipFile(tmp_path / "scan-first.zip") as archive:
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
                compile(source, f"sync_scan_codes_v2.zip/{member}", "exec")


def test_scan_manifest_closes_two_operations_connectors_and_correlated_budget() -> None:
    source = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    manifest = AutomationPluginManifestV2.from_mapping(source)
    contract = ServiceV2ProjectContract.from_manifest(manifest)

    assert manifest.plugin_id == "sync_scan_codes_v2"
    assert manifest.provided_services == ("plugin.sync_scan_codes_v2.scan_codes@1",)
    assert manifest.provides == (
        {
            "service": "plugin.sync_scan_codes_v2.scan_codes@1",
            "operations": (
                {"name": "preview", "effect": "read"},
                {"name": "execute", "effect": "external_write"},
            ),
        },
    )
    assert tuple(dict(item) for item in manifest.connector_requirements) == (
        {
            "service": "connector.boyi.scan_ronghui@1",
            "account_role": "account_id",
        },
        {
            "service": "connector.boyi.scan_projection@1",
            "binding_kind": "host_internal",
        },
    )
    expected_limits = {
        "read_page": 500,
        "snapshot_replace": 1,
        "submit": 499,
        "verify": 499,
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
    assert sum(expected_limits.values()) == 1499
    assert contract.runtime_permissions["max_broker_calls"] == 1000
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
            "title": "预览并执行扫描同步",
            "service": manifest.provided_services[0],
            "operation": "execute",
            "default_enabled": False,
        },
    )
    assert manifest.contributes["feishu"] == (
        {
            "id": "execute_feishu",
            "service": manifest.provided_services[0],
            "operation": "execute",
            "commands": ("扫描",),
            "default_enabled": False,
        },
    )
    assert manifest.contributes["scheduler"] == ()
    assert manifest.contributes["webhook"] == ()
    assert manifest.contributes["events"] == ()
    assert "selection_preview_operation" not in manifest.contributes["console"][0]
    assert "selection_preview_operation" not in manifest.contributes["feishu"][0]
    assert contract.invocation_contracts["execute_console"]["effect"] == "external_write"
    assert contract.invocation_contracts["execute_feishu"]["effect"] == "external_write"

    config_properties = source["config_schema"]["properties"]
    assert "dry_run" not in config_properties
    assert "_scan_preview_binding" not in config_properties

    missing_limit = copy.deepcopy(source)
    del missing_limit["capabilities"][0]["action_call_limits"]["verify"]
    with pytest.raises(PluginManifestError, match="action_call_limits"):
        AutomationPluginManifestV2.from_mapping(missing_limit)

    oversized_individual = copy.deepcopy(source)
    oversized_individual["capabilities"][0]["action_call_limits"]["verify"] = 1001
    with pytest.raises(PluginManifestError, match="action_call_limits"):
        AutomationPluginManifestV2.from_mapping(oversized_individual)


def test_scan_v2_payload_has_no_legacy_import_path_or_whole_tool_fallback() -> None:
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
            "scan_sync_tool",
            "call_http_service",
            "run_once",
        ):
            assert forbidden not in source


def test_scan_connector_adapter_maps_exact_primitives_and_preflight_sets() -> None:
    plugin = _load_module("sync_scan_codes_plugin_test", SOURCE / "payload" / "plugin.py")
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
            "browser.invoke",
            "ronghui.scan.read_page",
            plugin.SCAN_CONNECTOR,
            "read_page",
        ),
        (
            "projection.invoke",
            "scan.snapshot.replace",
            plugin.PROJECTION_CONNECTOR,
            "snapshot_replace",
        ),
        (
            "browser.invoke",
            "ronghui.scan_next.submit",
            plugin.SCAN_CONNECTOR,
            "submit",
        ),
        (
            "browser.invoke",
            "ronghui.scan_next.verify",
            plugin.SCAN_CONNECTOR,
            "verify",
        ),
    )
    for index, (operation, action, service, connector_operation) in enumerate(cases):
        plugin.service_invoke_adapter(
            broker,
            operation,
            action=action,
            role=plugin.ACCOUNT_ROLE,
            arguments={"target_date": "2026-08-31"},
            preflight_services=(plugin.EXECUTE_PREFLIGHT_SERVICES if index == 0 else ()),
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

    with pytest.raises(ValueError, match="primitive"):
        plugin.service_invoke_adapter(
            broker,
            "browser.invoke",
            action="ronghui.scan.unknown",
            role=plugin.ACCOUNT_ROLE,
            arguments={},
        )
    with pytest.raises(ValueError, match="preflight"):
        plugin.service_invoke_adapter(
            broker,
            "browser.invoke",
            action="ronghui.scan.read_page",
            role=plugin.ACCOUNT_ROLE,
            arguments={},
            preflight_services=(plugin.PROJECTION_CONNECTOR,),
        )


def test_scan_mutation_map_starts_at_projection_or_external_submit_only() -> None:
    plugin = _load_module(
        "sync_scan_codes_plugin_mutation_test",
        SOURCE / "payload" / "plugin.py",
    )
    assert plugin.MUTATING_CONNECTOR_OPERATIONS == frozenset(
        {"snapshot_replace", "submit"}
    )
    assert {"read_page", "verify"}.isdisjoint(plugin.MUTATING_CONNECTOR_OPERATIONS)
