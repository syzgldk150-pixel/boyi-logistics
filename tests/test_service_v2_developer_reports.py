from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from io import BytesIO
import zipfile

import pytest

from agent.automation_plugins import developer_reports_v2, service_v2_contract
from agent.automation_plugins.developer_reports_v2 import (
    NOT_EVALUATED_OFFLINE,
    diff_verified_packages,
    project_permission_report,
)
from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.host_capability_registry import (
    HostCapabilityRegistry,
    default_host_capability_registry,
)
from agent.automation_plugins.package_v2 import verify_unsigned_plugin_zip_v2


def _manifest(
    *,
    plugin_id: str = "report_plugin",
    version: str = "1.0.0",
    submit_effect: str = "external_write",
) -> dict[str, object]:
    service = f"plugin.{plugin_id}.runner@1"
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": plugin_id,
        "name": "Report plugin",
        "version": version,
        "description": "Offline developer report fixture",
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
                    {"name": "delete_everything", "effect": "read"},
                    {"name": "submit", "effect": submit_effect},
                ],
            }
        ],
        "requires": [{"service": "plugin.dependency.catalog@1"}],
        "capabilities": [
            {
                "name": "storage.kv",
                "operations": ["put", "get"],
                "account_role": None,
                "resource_role": None,
            },
            {
                "name": "browser.session",
                "operations": [
                    "ronghui.clock.submit",
                    "ronghui.clock.precheck",
                ],
                "account_role": "operator",
                "resource_role": None,
            },
            {
                "name": "service.invoke",
                "operations": ["lookup"],
                "account_role": None,
                "resource_role": None,
            },
        ],
        "account_roles": [
            {
                "role": "operator",
                "allowed_systems": ["ronghui"],
                "required": True,
            }
        ],
        "resource_roles": [
            {
                "role": "source_table",
                "allowed_kinds": ["table"],
                "required": False,
            }
        ],
        "contributes": {
            "console": [
                {
                    "id": "manual_read",
                    "title": "Manual read",
                    "service": service,
                    "operation": "delete_everything",
                    "default_enabled": True,
                }
            ],
            "scheduler": [
                {
                    "id": "scheduled_submit",
                    "title": "Scheduled submit",
                    "service": service,
                    "operation": "submit",
                    "default_enabled": False,
                    "schedule": {
                        "kind": "cron",
                        "expression": "5 2 * * *",
                        "timezone": "Asia/Shanghai",
                    },
                }
            ],
            "webhook": [],
            "feishu": [],
            "events": [],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
        "storage": {"kv": True, "collections": []},
    }


def _verified(
    manifest: dict[str, object],
    *,
    payload: bytes = b"def run():\n    return {'ok': True}\n",
) -> object:
    stream = BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr("payload/main.py", payload)
    package_bytes = stream.getvalue()
    return verify_unsigned_plugin_zip_v2(
        package_bytes,
        transport_sha256=hashlib.sha256(package_bytes).hexdigest(),
    )


def _row(rows: list[dict[str, object]], **identity: str) -> dict[str, object]:
    return next(
        row
        for row in rows
        if all(row.get(key) == value for key, value in identity.items())
    )


def test_permission_report_is_stable_declarative_and_uses_explicit_effects() -> None:
    verified = _verified(_manifest())

    report = project_permission_report(verified)

    assert report["schema"] == "service-v2-project-permissions/1"
    assert report["authority"] == {
        "mode": "DECLARATION_ONLY",
        "grants_created": False,
        "project_bindings_evaluated": False,
    }
    assert report["plugin"]["plugin_id"] == "report_plugin"
    misleading = _row(
        report["provided_operations"],
        service="plugin.report_plugin.runner@1",
        operation="delete_everything",
    )
    assert misleading["effect"] == "read"
    assert misleading["governance"]["broker_effect"] == "read"

    read_action = _row(
        report["host_capabilities"],
        capability="storage.kv",
        action="get",
    )
    assert read_action["role"] == {"kind": "system", "name": "__system__"}
    assert read_action["effect"] == "read"
    assert read_action["actual_effect"] == "read"
    assert read_action["scheduler_allowed"] is True
    assert read_action["per_call_limit"] == 64
    assert read_action["grant"] is False

    service_invoke = _row(
        report["host_capabilities"],
        capability="service.invoke",
        action="lookup",
    )
    assert service_invoke["dynamic_effect"] is True
    assert service_invoke["admission_ceiling"] == "external_write"
    assert service_invoke["actual_effect"] == "RUNTIME_RESOLVED"
    assert service_invoke["effect_resolution"] == "PROVIDER_OPERATION_AT_RUNTIME"
    assert service_invoke["grant"] is False

    assert report["account_roles"][0]["role"] == "operator"
    assert report["resource_roles"][0]["role"] == "source_table"
    console = _row(report["contributions"], kind="console", id="manual_read")
    scheduler = _row(
        report["contributions"],
        kind="scheduler",
        id="scheduled_submit",
    )
    assert console["effect"] == "read"
    assert scheduler["effect"] == "external_write"
    assert report["runtime_summary"]["runtime"]["python"] == "3.10"
    assert report["runtime_summary"]["artifact"]["file_count"] == 2
    assert json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False)) == report


def test_permission_report_rejects_unknown_and_disabled_host_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown_manifest = _manifest()
    capabilities = unknown_manifest["capabilities"]
    assert isinstance(capabilities, list)
    storage_capability = capabilities[0]
    assert isinstance(storage_capability, dict)
    storage_capability["operations"] = ["unknown_action"]
    unknown = _verified(unknown_manifest)
    with pytest.raises(PluginManifestError, match="unavailable"):
        project_permission_report(unknown)

    descriptors = [
        replace(item, enabled=False)
        if item.capability == "storage.kv" and item.action == "get"
        else item
        for item in default_host_capability_registry().snapshot()
    ]

    def _disabled_registry() -> HostCapabilityRegistry:
        return HostCapabilityRegistry(descriptors)

    monkeypatch.setattr(
        developer_reports_v2,
        "default_host_capability_registry",
        _disabled_registry,
    )
    monkeypatch.setattr(
        service_v2_contract,
        "default_host_capability_registry",
        _disabled_registry,
    )
    with pytest.raises(PluginManifestError, match="unavailable"):
        project_permission_report(_verified(_manifest()))


@pytest.mark.parametrize(
    ("before_manifest", "after_manifest", "after_payload", "classification"),
    [
        (
            _manifest(plugin_id="report_plugin"),
            _manifest(plugin_id="other_plugin"),
            b"def run():\n    return 2\n",
            "INVALID_IDENTITY",
        ),
        (
            _manifest(version="1.0.0"),
            _manifest(version="1.0.0"),
            b"def run():\n    return 2\n",
            "IMMUTABLE_VERSION_CONFLICT",
        ),
        (
            _manifest(version="2.0.0"),
            _manifest(version="1.9.9"),
            b"def run():\n    return 2\n",
            "DOWNGRADE",
        ),
        (
            _manifest(version="1.0.0"),
            _manifest(version="1.1.0"),
            b"def run():\n    return 2\n",
            "REVIEW_REQUIRED",
        ),
    ],
)
def test_package_diff_classifies_identity_immutability_downgrade_and_review(
    before_manifest: dict[str, object],
    after_manifest: dict[str, object],
    after_payload: bytes,
    classification: str,
) -> None:
    result = diff_verified_packages(
        _verified(before_manifest),
        _verified(after_manifest, payload=after_payload),
    )

    assert result["classification"] == classification
    assert result["review_required"] is (classification == "REVIEW_REQUIRED")
    assert result["project_configuration"] == NOT_EVALUATED_OFFLINE
    assert result["configuration"]["project_configuration"] == NOT_EVALUATED_OFFLINE
    assert result["compatibility_claim"] == "NONE"


def test_package_diff_reports_no_change_for_the_same_verified_package() -> None:
    verified = _verified(_manifest())

    result = diff_verified_packages(verified, verified)

    assert result["classification"] == "NO_CHANGE"
    assert result["package"]["same_bytes"] is True
    assert result["manifest"]["changed_sections"] == []
    assert result["files"] == {"added": [], "removed": [], "changed": []}
    assert result["permissions"]["changed"] is False
    assert result["contributions"]["changed"] is False


def test_package_diff_marks_payload_only_upgrade_for_review() -> None:
    result = diff_verified_packages(
        _verified(_manifest(version="1.0.0")),
        _verified(
            _manifest(version="1.1.0"),
            payload=b"def run():\n    return {'ok': 'changed'}\n",
        ),
    )

    assert result["classification"] == "REVIEW_REQUIRED"
    assert result["package"]["payload_changed"] is True
    assert result["package"]["payload_only"] is True
    assert result["manifest"]["changed_sections"] == ["version"]
    assert result["permissions"]["changed"] is False
    assert result["contributions"]["changed"] is False
    assert result["configuration"]["changed"] is False
    assert result["storage"]["changed"] is False


def test_package_diff_detects_explicit_effect_escalation() -> None:
    result = diff_verified_packages(
        _verified(_manifest(version="1.0.0", submit_effect="read")),
        _verified(_manifest(version="1.1.0", submit_effect="external_write")),
    )

    assert result["classification"] == "REVIEW_REQUIRED"
    assert result["permissions"]["effect_escalation"] is True
    escalations = result["permissions"]["effect_escalations"]
    assert any(
        item["identity"]
        == ["provider", "plugin.report_plugin.runner@1", "submit"]
        and item["before_effect"] == "read"
        and item["after_effect"] == "external_write"
        for item in escalations
    )
    assert result["permissions"]["provided_operations"]["changed"]
    assert result["contributions"]["changed"] is True


def test_package_diff_ignores_declarative_array_order() -> None:
    before_manifest = _manifest(version="1.0.0")
    after_manifest = copy.deepcopy(_manifest(version="1.1.0"))
    provides = after_manifest["provides"]
    capabilities = after_manifest["capabilities"]
    assert isinstance(provides, list) and isinstance(provides[0], dict)
    assert isinstance(capabilities, list)
    provides[0]["operations"] = list(reversed(provides[0]["operations"]))
    capabilities.reverse()
    for capability in capabilities:
        assert isinstance(capability, dict)
        capability["operations"] = list(reversed(capability["operations"]))

    result = diff_verified_packages(
        _verified(before_manifest),
        _verified(after_manifest),
    )

    assert result["classification"] == "REVIEW_REQUIRED"
    assert result["manifest"]["changed_sections"] == ["version"]
    assert result["permissions"]["changed"] is False
    assert result["contributions"]["changed"] is False
    assert result["permissions"]["effect_escalation"] is False
