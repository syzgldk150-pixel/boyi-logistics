"""Activation compares actual V2 compiler output with the locked signed row."""
from __future__ import annotations

import copy
import json

import pytest

from agent.automation_plugins.developer_v2 import init_service_v2_source
from agent.automation_plugins.manifest_v2 import AutomationPluginManifestV2
from agent.automation_plugins.runtime_repository import _service_v2_signed_activation_contract
from agent.automation_plugins.service_v2_contract import HOST_API_VERSION, ServiceV2ProjectContract
from shared.automation_plugin_repository import AutomationPluginRepository
from shared.automation_plugin_generation_transition_repository import _validate_installed_target_version
from shared.orchestration_repository_support import ConcurrentUpdateError, OrchestrationPersistenceError, _json_hash


@pytest.fixture
def material(tmp_path):
    source = tmp_path / "source"
    init_service_v2_source(source, plugin_id="activation_probe", name="Activation probe", version="1.0.0")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    parsed = AutomationPluginManifestV2.from_mapping(manifest)
    contract = ServiceV2ProjectContract.from_manifest(parsed)
    projected = _service_v2_signed_activation_contract(manifest)
    descriptor = {key: copy.deepcopy(projected[key]) for key in ("runtime", "runtime_permissions", "account_roles", "resource_roles")}
    descriptor["install_metadata"] = {"install_root": "/isolated/activation", "python_relative": ".venv/bin/python"}
    snapshot = {
        "plugin_id": parsed.plugin_id, "plugin_version": parsed.version,
        "runtime_model": "SERVICE_V2", "plugin_api": HOST_API_VERSION,
        "package_sha256": "a" * 64, "manifest_sha256": _json_hash(manifest),
        "trust_source": "SUPER_ADMIN_UPLOAD", "tool_contract_sha256": _json_hash(projected["tool_contract"]),
        "invocation_contracts_sha256": _json_hash(contract.invocation_contracts),
        "governance_anchor_sha256": _json_hash(projected["governance_anchor"]),
        "runtime_descriptor_sha256": _json_hash(descriptor),
        "execution_metadata": {"action_contract": projected["tool_contract"], "governance_anchor": projected["governance_anchor"], "runtime_descriptor": descriptor},
    }
    version = {key: value for key, value in snapshot.items() if key not in {"execution_metadata", "plugin_version"}}
    version.update({"version": parsed.version, "state": "INSTALLED", "manifest_json": manifest, "install_root_metadata_json": descriptor["install_metadata"]})
    repository = AutomationPluginRepository

    class Cursor:
        def execute(self, sql, params):
            assert "FOR UPDATE" in sql
            assert params == (parsed.plugin_id, parsed.version)

        def fetchone(self):
            return copy.deepcopy(version)

    return repository, Cursor(), snapshot, version


def test_actual_v2_manifest_compiles_and_validates_without_v1_fields(material):
    repository, cursor, snapshot, version = material
    assert "tool_contract" not in version["manifest_json"]
    result = _validate_installed_target_version(repository, cursor, snapshot=snapshot, service_v2_contract_projector=_service_v2_signed_activation_contract)
    assert result["version"] == version["version"]


@pytest.mark.parametrize("field", ("action_contract", "governance_anchor", "runtime_descriptor"))
def test_self_consistent_forged_snapshot_still_cannot_change_signed_contract(material, field):
    repository, cursor, snapshot, _ = material
    snapshot["execution_metadata"][field]["forged_permission"] = True
    hash_field = {"action_contract": "tool_contract_sha256", "governance_anchor": "governance_anchor_sha256", "runtime_descriptor": "runtime_descriptor_sha256"}[field]
    snapshot[hash_field] = _json_hash(snapshot["execution_metadata"][field])
    with pytest.raises(ConcurrentUpdateError):
        _validate_installed_target_version(repository, cursor, snapshot=snapshot, service_v2_contract_projector=_service_v2_signed_activation_contract)


def test_v2_cannot_fall_back_to_raw_v1_or_trust_mutated_manifest(material):
    repository, cursor, snapshot, version = material
    with pytest.raises(OrchestrationPersistenceError, match="projector is required"):
        _validate_installed_target_version(repository, cursor, snapshot=snapshot)
    version["manifest_json"]["name"] = "Tampered"
    with pytest.raises(ConcurrentUpdateError, match="manifest digest changed"):
        _validate_installed_target_version(repository, cursor, snapshot=snapshot, service_v2_contract_projector=_service_v2_signed_activation_contract)
