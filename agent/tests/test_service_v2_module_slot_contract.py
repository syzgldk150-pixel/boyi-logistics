from __future__ import annotations

import copy
from typing import Any

import pytest

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_DRAFT_FIELDS,
    WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID,
)


def _manifest_mapping(*, effect: str = "read") -> dict[str, Any]:
    service = "plugin.waybill_extension.validator@1"
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": "waybill_extension",
        "name": "Waybill extension",
        "version": "1.0.0",
        "description": "Closed waybill-entry extension fixture.",
        "host_api": {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": None,
            "wheelhouse": [],
        },
        "provides": [
            {
                "service": service,
                "operations": [
                    {"name": "validate", "effect": effect},
                    {"name": "assistant_preview", "effect": "read"},
                ],
            }
        ],
        "requires": [],
        "capabilities": [],
        "account_roles": [],
        "resource_roles": [],
        "contributes": {
            "console": [],
            "scheduler": [],
            "webhook": [],
            "feishu": [],
            "events": [],
            "module_slots": [
                {
                    "id": "validate_waybill",
                    "slot": "waybill_entry.validators",
                    "title": "校验运单",
                    "service": service,
                    "operation": "validate",
                    "default_enabled": True,
                }
            ],
            "harness": [
                {
                    "id": "assistant_preview",
                    "title": "查询运单校验状态",
                    "description": "只读查看运单校验插件状态。",
                    "scenarios": ["查询运单校验插件状态"],
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                        "required": [],
                    },
                    "service": service,
                    "operation": "assistant_preview",
                    "effect": "read",
                    "confirmation_policy": "none",
                    "preview_operation": None,
                }
            ],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"mode": {"type": "string"}},
            "required": ["mode"],
        },
        "settings_ui": {
            "entry": "settings/index.html",
            "bridge_api": "1.0.0",
        },
        "storage": {"kv": False, "collections": []},
    }


def test_optional_module_slot_round_trips_and_compiles_one_host_resolver() -> None:
    source = _manifest_mapping()
    manifest = AutomationPluginManifestV2.from_mapping(source)

    assert manifest.to_mapping() == source
    contract = ServiceV2ProjectContract.from_manifest(manifest)
    invocation = contract.invocation_contracts["validate_waybill"]
    assert contract.contribution_kinds == {
        "validate_waybill": "module_slots",
        "assistant_preview": "harness",
    }
    assert invocation["contribution_kind"] == "module_slots"
    assert invocation["argument_template"] == {
        "mode": {"source": "project_config", "key": "mode"}
    }
    assert invocation["dynamic_resolvers"] == {
        "waybill": WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID
    }
    assert set(invocation["input_schema"]["properties"]["waybill"]["properties"]) == set(
        WAYBILL_ENTRY_DRAFT_FIELDS
    )
    assert invocation["input_schema"]["properties"]["waybill"][
        "additionalProperties"
    ] is False
    assert invocation["input_schema"]["required"] == ["mode", "waybill"]
    assert set(invocation) == {
        "input_schema",
        "service",
        "operation",
        "contribution_kind",
        "argument_template",
        "dynamic_resolvers",
        "effect",
        "governance",
    }
    assert "waybill" in contract.tool_contract["input_schema"]["properties"]
    assert "waybill" not in contract.tool_contract["input_schema"]["required"]


def test_manifest_without_module_slots_is_not_rewritten_or_rehashed() -> None:
    source = _manifest_mapping()
    source["contributes"].pop("module_slots")

    manifest = AutomationPluginManifestV2.from_mapping(source)

    assert manifest.to_mapping() == source
    assert "module_slots" not in manifest.to_mapping()["contributes"]
    assert manifest.manifest_sha256 == AutomationPluginManifestV2.from_mapping(
        copy.deepcopy(source)
    ).manifest_sha256


@pytest.mark.parametrize(
    "mutate",
    [
        lambda source: source["contributes"]["module_slots"][0].update(
            slot="waybill_entry.sidebar"
        ),
        lambda source: source["contributes"]["module_slots"][0].update(
            html="<button>run</button>"
        ),
        lambda source: source["contributes"]["module_slots"][0].update(
            endpoint="/internal/run"
        ),
    ],
)
def test_module_slot_declaration_rejects_unknown_slot_and_ui_or_endpoint_fields(
    mutate,
) -> None:
    source = _manifest_mapping()
    mutate(source)

    with pytest.raises(PluginManifestError):
        AutomationPluginManifestV2.from_mapping(source)


def test_module_slot_rejects_write_provider_and_reserved_waybill_config() -> None:
    with pytest.raises(PluginManifestError, match="read or compute"):
        AutomationPluginManifestV2.from_mapping(
            _manifest_mapping(effect="external_write")
        )

    source = _manifest_mapping()
    source["config_schema"]["properties"] = {"waybill": {"type": "string"}}
    source["config_schema"]["required"] = ["waybill"]
    manifest = AutomationPluginManifestV2.from_mapping(source)
    with pytest.raises(PluginManifestError, match="collides"):
        ServiceV2ProjectContract.from_manifest(manifest)
