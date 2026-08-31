from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from service_v2_plugins._shared.build_zip import build_plugin_zip


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agent" / "service_v2_plugins" / "self_pickup_problem_upload_v2"
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "service_v2"
    / "self_pickup_problem_upload_v2"
    / "self_pickup_case.json"
)
V1_ACTION = (
    ROOT
    / "agent"
    / "first_party_automation_plugins"
    / "self_pickup_problem_upload"
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


def _load_action(name: str, path: Path):
    result_module = _load_module("self_pickup_result_test", RESULT_SOURCE)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    try:
        return _load_module(name, path)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous


def _build(source: Path, output: Path) -> bytes:
    built = build_plugin_zip(source, output)
    assert built == output.resolve()
    return built.read_bytes()


def test_self_pickup_v2_zip_is_deterministic_and_embeds_v1_action_and_result_bytes(
    tmp_path: Path,
) -> None:
    first = _build(SOURCE, tmp_path / "self-pickup-first.zip")
    second = _build(SOURCE, tmp_path / "self-pickup-second.zip")
    assert first == second
    with zipfile.ZipFile(tmp_path / "self-pickup-first.zip") as archive:
        expected_members = {
            "manifest.json",
            "payload/action.py",
            "payload/boyi_plugin_result.py",
            "payload/boyi_plugin_sdk.py",
            "payload/main.py",
            "payload/plugin.py",
        }
        assert set(archive.namelist()) == expected_members
        assert archive.read("payload/action.py") == V1_ACTION.read_bytes()
        assert archive.read("payload/boyi_plugin_result.py") == RESULT_SOURCE.read_bytes()
        for member in sorted(expected_members):
            source = archive.read(member)
            if member.endswith(".py"):
                compile(source, f"self_pickup_problem_upload_v2.zip/{member}", "exec")

    manifest = AutomationPluginManifestV2.from_mapping(
        json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    )
    assert manifest.plugin_id == "self_pickup_problem_upload_v2"
    assert manifest.provided_services == (
        "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1",
    )
    assert manifest.provides == (
        {
            "service": "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1",
            "operations": (
                {"name": "preview", "effect": "read"},
                {"name": "execute", "effect": "external_write"},
            ),
        },
    )
    assert manifest.capabilities == (
        {
            "name": "service.invoke",
            "operations": ("read_rows", "query", "create", "verify"),
            "account_role": None,
            "resource_role": None,
            "action_call_limits": {
                "read_rows": 1,
                "query": 250,
                "create": 250,
                "verify": 250,
            },
        },
    )
    assert manifest.contributes["scheduler"] == ()
    assert manifest.contributes["webhook"] == ()
    assert manifest.contributes["events"] == ()
    assert manifest.contributes["console"] == (
        {
            "id": "execute_console",
            "title": "预览自提问题件候选",
            "service": "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1",
            "operation": "execute",
            "selection_preview_operation": "preview",
            "default_enabled": False,
        },
    )
    assert manifest.contributes["feishu"] == (
        {
            "id": "execute_feishu",
            "service": "plugin.self_pickup_problem_upload_v2.self_pickup_problem_upload@1",
            "operation": "execute",
            "commands": ("自提到货问题件",),
            "selection_preview_operation": "preview",
            "default_enabled": False,
        },
    )
    assert set(manifest.config_schema["properties"]) == {
        "include_daxiang_s_self_pickup",
        "limit",
    }
    projection = ServiceV2ProjectContract.from_manifest(manifest)
    assert projection.allowed_entrypoints == ("execute_console", "execute_feishu")
    assert projection.default_entrypoints == ()
    assert projection.scheduling == {
        "supported": False,
        "allowed_kinds": [],
        "max_daily_times": 0,
    }
    assert projection.runtime_permissions["max_broker_calls"] == 751


def test_self_pickup_v2_payload_has_no_legacy_import_or_path_mutation() -> None:
    for path in (
        SOURCE / "payload" / "plugin.py",
        ROOT / "agent" / "service_v2_plugins" / "_shared" / "self_pickup_service_main.py",
    ):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "first_party_automation_plugins" not in source
        assert "sys.path" not in source
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


def test_connector_adapter_uses_semantic_operations_and_never_forwards_v1_roles() -> None:
    plugin = _load_module("self_pickup_plugin_test", SOURCE / "payload" / "plugin.py")
    calls: list[dict[str, object]] = []

    def broker(operation: str, *, action: str, role: str, arguments: dict[str, object]) -> object:
        calls.append(
            {
                "operation": operation,
                "action": action,
                "role": role,
                "arguments": copy.deepcopy(arguments),
            }
        )
        return {"evidence_ref": f"host-evidence:{len(calls)}"}

    plugin.service_invoke_adapter(
        broker,
        "network.request",
        action="feishu.sheet.read_rows",
        role="self_pickup_source_sheet",
        arguments={"end_column": "S", "max_rows": 2_000},
        preflight_services=plugin.PREVIEW_PREFLIGHT_SERVICES,
    )
    plugin.service_invoke_adapter(
        broker,
        "browser.invoke",
        action="ronghui.problem.query",
        role="daxiang_s_account_id",
        arguments={"bill_code": "R_DX_PICK"},
    )
    plugin.service_invoke_adapter(
        broker,
        "browser.invoke",
        action="ronghui.problem.create",
        role="account_id",
        arguments={"bill_code": "R_SELF"},
    )
    plugin.service_invoke_adapter(
        broker,
        "browser.invoke",
        action="ronghui.problem.verify",
        role="account_id",
        arguments={"bill_code": "R_SELF"},
    )

    assert [(item["action"], item["arguments"]["service"], item["arguments"]["operation"]) for item in calls] == [
        ("read_rows", plugin.SOURCE_CONNECTOR, "read_rows"),
        ("query", plugin.DAXIANG_CONNECTOR, "query"),
        ("create", plugin.PRIMARY_CONNECTOR, "create"),
        ("verify", plugin.PRIMARY_CONNECTOR, "verify"),
    ]
    assert calls[0]["role"] == plugin.SYSTEM_ROLE
    assert calls[0]["arguments"]["preflight_services"] == list(plugin.PREVIEW_PREFLIGHT_SERVICES)
    assert all(item["role"] == plugin.SYSTEM_ROLE for item in calls)
    nested_arguments = json.dumps(
        [item["arguments"]["arguments"] for item in calls], ensure_ascii=False
    )
    assert "account_id" not in nested_arguments
    assert "daxiang_s_account_id" not in nested_arguments
    assert "self_pickup_source_sheet" not in nested_arguments

    with pytest.raises(ValueError, match="primitive"):
        plugin.service_invoke_adapter(
            broker,
            "network.request",
            action="feishu.sheet.replace",
            role="self_pickup_source_sheet",
            arguments={},
        )
    with pytest.raises(ValueError, match="preflight"):
        plugin.service_invoke_adapter(
            broker,
            "network.request",
            action="feishu.sheet.read_rows",
            role="self_pickup_source_sheet",
            arguments={},
            preflight_services=(plugin.PRIMARY_CONNECTOR,),
        )


def test_console_selection_preview_accepts_signed_read_pair_and_rejects_drift() -> None:
    source = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    manifest = AutomationPluginManifestV2.from_mapping(source)
    contract = ServiceV2ProjectContract.from_manifest(manifest)

    console = manifest.contributes["console"][0]
    assert console["operation"] == "execute"
    assert console["selection_preview_operation"] == "preview"
    assert contract.invocation_contracts["execute_console"]["operation"] == "execute"
    assert contract.invocation_contracts["execute_console"]["effect"] == "external_write"

    wrong_operation = copy.deepcopy(source)
    wrong_operation["contributes"]["console"][0]["selection_preview_operation"] = "execute"
    with pytest.raises(PluginManifestError, match="selection preview"):
        AutomationPluginManifestV2.from_mapping(wrong_operation)

    wrong_governance = copy.deepcopy(source)
    wrong_governance["provides"][0]["operations"][0]["effect"] = "external_write"
    with pytest.raises(PluginManifestError, match="selection preview"):
        AutomationPluginManifestV2.from_mapping(wrong_governance)


def test_manifest_operation_effects_and_action_limits_are_distinct() -> None:
    manifest = AutomationPluginManifestV2.from_mapping(
        json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    )
    projection = ServiceV2ProjectContract.from_manifest(manifest)
    assert projection.invocation_contracts["execute_console"]["operation"] == "execute"
    assert projection.invocation_contracts["execute_console"]["effect"] == "external_write"
    assert projection.invocation_contracts["execute_feishu"]["operation"] == "execute"
    assert projection.runtime_permissions["broker_operations"] == [
        {
            "operation": "service.invoke",
            "action": action,
            "roles": ["__system__"],
            "effect": "external_write",
            "broker_effect": "write",
            "governance": {
                "effect": "external_write",
                "operation_type": "external_write",
                "risk_level": "high",
                "lock_class": "external_target",
                "evidence": {
                    "required": True,
                    "required_fields": ["service", "operation", "outcome"],
                },
                "postconditions": [{"name": "plugin_result_contract_valid"}],
                "retry": {"safe": False, "max_attempts": 1},
                "harness_allowed": False,
                "broker_effect": "write",
                "approval": {"mode": "project_policy"},
                "idempotency": {"mode": "parameters", "key_fields": []},
                "project_full_auto_allowed": True,
            },
            "dynamic_effect": True,
            "per_action_limit": limit,
        }
        for action, limit in (
            ("read_rows", 1),
            ("query", 250),
            ("create", 250),
            ("verify", 250),
        )
    ]
