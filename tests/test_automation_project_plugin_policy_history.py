from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from shared.automation_plugin_repository import AutomationPluginRepository


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_HISTORY_PATH = (
    ROOT / "agent" / "scripts" / "automation_project_plugin_policy_history.py"
)
MIGRATION_RUNNER_PATH = ROOT / "agent" / "scripts" / "run_migrations.py"
POLICY_HISTORY_MODULE_NAMES = (
    "_automation_project_policy_history",
    "_automation_project_plugin_policy_history",
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plugin_history():
    return _load_module(PLUGIN_HISTORY_PATH, "test_plugin_policy_history")


def _canonical_sha256(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _plugin_event() -> dict:
    request_id = "55555555-5555-4555-a555-555555555555"
    return {
        "event_id": 3,
        "request_id": request_id,
        "from_mode": "PROJECT_FULL_AUTO",
        "to_mode": "REQUIRE_EACH_RUN",
        "contract_hash": None,
        "contract_snapshot_json": None,
        "tool_contract_hash": None,
        "plugin_contract_hash": None,
        "project_generation": 2,
        "project_configuration_version": 2,
        "actor_id": "admin-1",
        "actor_role": "super_admin",
        "actor_display_name": None,
        "reason": "PLUGIN_VERSION_CHANGED",
        "comment": None,
        "correlation_id": request_id,
    }


def _package_identity(*, package_sha256: str, manifest_sha256: str) -> dict:
    return AutomationPluginRepository._package_audit_identity(
        {
            "package_sha256": package_sha256,
            "manifest_sha256": manifest_sha256,
            "runtime_model": "ACTION_V1",
            "plugin_api": "1.0.0",
            "trust_source": "ed25519_first_party",
            "runtime_sha256": "c" * 64,
            "tool_contract_sha256": "d" * 64,
            "invocation_contracts_sha256": "e" * 64,
            "scheduling_sha256": "f" * 64,
            "manifest_json": {
                "capabilities": [],
                "provides": [],
                "requires": [],
                "contributes": {},
                "config_schema": {},
                "storage": {},
            },
        }
    )


def _plugin_evidence(
    event: dict,
    *,
    include_prepared_request: bool,
    include_immutable_version_identity: bool = False,
    target_generation=2,
) -> dict:
    metadata = {
        "request_payload_sha256": "a" * 64,
        "from_version": "1.0.0",
        "to_version": "2.0.0",
        "package_sha256": "b" * 64,
        "target_generation": target_generation,
        "previous_state": "ENABLED",
    }
    if include_prepared_request:
        metadata["prepared_configuration_request_id"] = None
    if include_immutable_version_identity:
        metadata["immutable_version_identity"] = {
            "from_package": _package_identity(
                package_sha256="1" * 64,
                manifest_sha256="2" * 64,
            ),
            "to_package": _package_identity(
                package_sha256=metadata["package_sha256"],
                manifest_sha256="3" * 64,
            ),
        }
    return {
        "request_id": event["request_id"],
        "policy_event_id": event["event_id"],
        "from_mode": event["from_mode"],
        "to_mode": event["to_mode"],
        "policy_contract_hash": event["contract_hash"],
        "policy_contract_snapshot_json": event["contract_snapshot_json"],
        "policy_tool_contract_hash": event["tool_contract_hash"],
        "policy_plugin_contract_hash": event["plugin_contract_hash"],
        "policy_configuration_version": event["project_configuration_version"],
        "policy_project_generation": event["project_generation"],
        "policy_actor_id": event["actor_id"],
        "policy_actor_role": event["actor_role"],
        "policy_actor_display_name": event["actor_display_name"],
        "policy_reason": event["reason"],
        "policy_comment": event["comment"],
        "policy_correlation_id": event["correlation_id"],
        "configuration_event_id": 30,
        "configuration_event_type": "PLUGIN_UPGRADE_STAGED",
        "configuration_from_state": "ENABLED",
        "configuration_to_state": "UPGRADING",
        "configuration_actor_id": event["actor_id"],
        "configuration_actor_role": event["actor_role"],
        "configuration_metadata_json": metadata,
        "configuration_metadata_sha256": _canonical_sha256(metadata),
    }


@pytest.mark.parametrize(
    ("include_prepared_request", "expected_variant"),
    ((False, "ORIGINAL"), (True, "PREPARED_AWARE")),
)
def test_plugin_policy_history_accepts_only_the_matching_closed_shape(
    plugin_history,
    include_prepared_request,
    expected_variant,
):
    event = _plugin_event()
    evidence = _plugin_evidence(
        event,
        include_prepared_request=include_prepared_request,
    )

    prepared_request, downgrade, variant = (
        plugin_history.validate_plugin_version_evidence(event, [evidence])
    )

    assert prepared_request is None
    assert downgrade is True
    assert variant == expected_variant


@pytest.mark.parametrize("include_prepared_request", (False, True))
@pytest.mark.parametrize("invalid_target_generation", (True, 1.0, "2", 0, -1))
def test_plugin_policy_history_rejects_non_positive_integer_target_generation(
    plugin_history,
    include_prepared_request,
    invalid_target_generation,
):
    event = _plugin_event()
    evidence = _plugin_evidence(
        event,
        include_prepared_request=include_prepared_request,
        target_generation=invalid_target_generation,
    )

    with pytest.raises(ValueError, match="plugin policy evidence is invalid"):
        plugin_history.validate_plugin_version_evidence(event, [evidence])


def test_plugin_policy_history_accepts_current_immutable_identity_shape(
    plugin_history,
):
    event = {**_plugin_event(), "to_mode": "PROJECT_FULL_AUTO"}
    evidence = _plugin_evidence(
        event,
        include_prepared_request=True,
        include_immutable_version_identity=True,
    )

    prepared_request, downgrade, variant = (
        plugin_history.validate_plugin_version_evidence(event, [evidence])
    )

    assert prepared_request is None
    assert downgrade is False
    assert variant == "PREPARED_AWARE"


@pytest.mark.parametrize(
    "mutation",
    (
        "extra_identity_field",
        "extra_package_field",
        "wrong_target_package",
        "wrong_host_contract",
        "missing_manifest_component",
        "invalid_plugin_api",
        "unhashable_runtime_model",
    ),
)
def test_plugin_policy_history_rejects_tampered_immutable_identity(
    plugin_history,
    mutation,
):
    event = {**_plugin_event(), "to_mode": "PROJECT_FULL_AUTO"}
    evidence = _plugin_evidence(
        event,
        include_prepared_request=True,
        include_immutable_version_identity=True,
    )
    metadata = evidence["configuration_metadata_json"]
    identity = metadata["immutable_version_identity"]
    target = identity["to_package"]
    if mutation == "extra_identity_field":
        identity["unexpected"] = True
    elif mutation == "extra_package_field":
        target["unexpected"] = True
    elif mutation == "wrong_target_package":
        target["package_sha256"] = "9" * 64
    elif mutation == "wrong_host_contract":
        target["technical_check"]["host_contract_sha256"] = "9" * 64
    elif mutation == "missing_manifest_component":
        target["manifest_component_sha256"].pop("storage")
    elif mutation == "invalid_plugin_api":
        target["plugin_api"] = "01.0.0"
    elif mutation == "unhashable_runtime_model":
        target["runtime_model"] = {}
    evidence["configuration_metadata_sha256"] = _canonical_sha256(metadata)

    with pytest.raises(ValueError, match="plugin policy evidence is invalid"):
        plugin_history.validate_plugin_version_evidence(event, [evidence])


def _load_migration_runner():
    return _load_module(
        MIGRATION_RUNNER_PATH,
        "test_migration_runner_policy_history_cleanup",
    )


def _restore_modules(previous: dict[str, object]) -> None:
    for name in POLICY_HISTORY_MODULE_NAMES:
        sys.modules.pop(name, None)
        if name in previous:
            sys.modules[name] = previous[name]


def test_migration_runner_does_not_leave_policy_history_helpers_registered():
    previous = {
        name: sys.modules[name]
        for name in POLICY_HISTORY_MODULE_NAMES
        if name in sys.modules
    }
    for name in POLICY_HISTORY_MODULE_NAMES:
        sys.modules.pop(name, None)
    try:
        _load_migration_runner()
        assert all(name not in sys.modules for name in POLICY_HISTORY_MODULE_NAMES)
    finally:
        _restore_modules(previous)


def test_migration_runner_preserves_existing_policy_history_modules():
    previous = {
        name: sys.modules[name]
        for name in POLICY_HISTORY_MODULE_NAMES
        if name in sys.modules
    }
    sentinels = {
        name: ModuleType(f"sentinel_{name}") for name in POLICY_HISTORY_MODULE_NAMES
    }
    sys.modules.update(sentinels)
    try:
        _load_migration_runner()
        for name, sentinel in sentinels.items():
            assert sys.modules.get(name) is sentinel
    finally:
        _restore_modules(previous)
