"""Release gate accepts only target configuration tied to its real upgrade."""
from __future__ import annotations

import copy
import os
from uuid import NAMESPACE_URL, uuid5

import pytest

from tests.test_automation_project_plugin_policy_history import _package_identity
from tests.test_automation_project_release_manifest_preflight import (
    _bootstrap_policy_event, _configuration_event, _configuration_evidence,
    _durable_policy, _later_policy_contract, _migration_full_auto_event,
    _plugin_version_event, _plugin_version_evidence, preflight,  # noqa: F401
)
from tests.test_first_party_upgrade_configuration_mysql import upgrade  # noqa: F401
from tests.test_legacy_unknown_scope_migration_mysql import database  # noqa: F401


ACTOR = "system:release:first-party-upgrade"
PROJECT = "send_order"


def _release_pair(preflight):
    config = _configuration_event(event_id=3, mode="PROJECT_FULL_AUTO", generation=2,
        config_version=3, actor_id=ACTOR)
    config["automation_id"] = PROJECT
    config["request_id"] = config["correlation_id"] = str(uuid5(NAMESPACE_URL, "isolated:configuration"))
    plugin = {**_plugin_version_event(event_id=4), "automation_id": PROJECT,
        "project_configuration_version": 3, "actor_id": ACTOR}
    plugin["request_id"] = plugin["correlation_id"] = str(uuid5(NAMESPACE_URL, "isolated:upgrade"))
    configuration = {**_configuration_evidence(preflight, config), "automation_id": PROJECT}
    staged = {**_plugin_version_evidence(preflight, plugin, prepared_request_id=config["request_id"]),
        "automation_id": PROJECT}
    metadata = staged["configuration_metadata_json"]
    metadata["immutable_version_identity"] = {
        "from_package": _package_identity(package_sha256="1" * 64, manifest_sha256="2" * 64),
        "to_package": _package_identity(package_sha256=metadata["package_sha256"], manifest_sha256="3" * 64),
    }
    configuration["configuration_metadata_json"]["first_party_upgrade_target"] = {
        key: metadata[key] for key in ("from_version", "to_version", "package_sha256")
    }
    rows = [configuration, staged]
    _rehash(preflight, rows)
    return config, plugin, rows


def _rehash(preflight, rows):
    for row in rows:
        row["configuration_metadata_sha256"] = preflight._canonical_sha256(row["configuration_metadata_json"])


def test_full_policy_chain_accepts_exact_first_party_target_configuration(preflight):
    config, plugin, rows = _release_pair(preflight)
    preflight._validate_later_project_policy_chain(
        _later_policy_contract(), automation_id=PROJECT,
        item={"automation_id": PROJECT, "initial_mode": "REQUIRE_EACH_RUN", "policy_version": 1},
        project={"generation": 2, "config_version": 3},
        policy=_durable_policy("PROJECT_FULL_AUTO", 4, 2, 3, plugin),
        policy_events=[_bootstrap_policy_event(PROJECT, to_mode="REQUIRE_EACH_RUN"),
            _migration_full_auto_event(PROJECT), config, plugin],
        configuration_evidence=rows,
    )


@pytest.mark.parametrize("invalid", [
    "ordinary_actor", "from_version", "to_version", "package_digest", "target_extra",
    "metadata_extra", "missing_pair", "duplicate_pair", "wrong_project", "wrong_request",
    "wrong_generation", "wrong_config_version", "wrong_actor", "wrong_pair_reason",
    "unsigned_target", "missing_immutable_identity", "bad_target_schema", "bad_metadata_hash",
])
def test_target_metadata_cannot_bypass_normal_configuration_evidence(preflight, invalid):
    config, _plugin, rows = _release_pair(preflight)
    metadata = rows[0]["configuration_metadata_json"]
    target = metadata["first_party_upgrade_target"]
    staged = rows[1]
    if invalid == "ordinary_actor":
        config["actor_id"] = rows[0]["configuration_actor_id"] = rows[0]["policy_actor_id"] = "admin"
    elif invalid in {"from_version", "to_version"}:
        target[invalid] = "4.0.0"
    elif invalid == "package_digest":
        target["package_sha256"] = "e" * 64
    elif invalid == "target_extra":
        target["unexpected"] = True
    elif invalid == "metadata_extra":
        metadata["unexpected"] = True
    elif invalid == "missing_pair":
        rows.pop()
    elif invalid == "duplicate_pair":
        rows.append(copy.deepcopy(staged))
    elif invalid == "wrong_project":
        staged["automation_id"] = "other-project"
    elif invalid == "wrong_request":
        staged["configuration_metadata_json"]["prepared_configuration_request_id"] = str(uuid5(NAMESPACE_URL, "other"))
    elif invalid == "wrong_generation":
        staged["policy_project_generation"] = staged["configuration_metadata_json"]["target_generation"] = 3
    elif invalid == "wrong_config_version":
        staged["policy_configuration_version"] = 4
    elif invalid == "wrong_actor":
        staged["policy_actor_id"] = staged["configuration_actor_id"] = "other-admin"
    elif invalid == "wrong_pair_reason":
        staged["policy_reason"] = "OTHER"
    elif invalid == "unsigned_target":
        staged["configuration_metadata_json"]["immutable_version_identity"]["to_package"]["trust_source"] = "super_admin_upload"
    elif invalid == "missing_immutable_identity":
        staged["configuration_metadata_json"].pop("immutable_version_identity")
    elif invalid == "bad_target_schema":
        metadata["first_party_upgrade_target"] = []
    else:
        rows[0]["configuration_metadata_sha256"] = "f" * 64
    if invalid != "bad_metadata_hash":
        _rehash(preflight, rows)
    with pytest.raises(preflight.AutomationProjectReleaseManifestError,
        match="AUTOMATION_PROJECT_FOLLOWUP_CONFIGURATION_EVENT_INVALID"):
        preflight._validate_followup_configuration_event(
            _later_policy_contract(), automation_id=PROJECT, event=config, configuration_evidence=rows,
        )


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires explicit isolated MySQL 8")
def test_real_bootstrap_writer_events_pass_release_gate_readback(upgrade, preflight):
    database, adapter, _old, target, _manifest, seed = upgrade
    adapter.bootstrap_missing((target,), (seed,), release_sha="a" * 40)
    with database.helper._connection() as connection, connection.cursor() as cursor:
        _policies, events, evidence = preflight._read_project_policy_evidence(
            cursor, {"release_projects": {seed.automation_id}},
        )
    configuration_events = [event for event in events[seed.automation_id]
        if event["reason"] == "PROJECT_CONFIGURATION_CHANGED"]
    assert len(configuration_events) == 1
    assert preflight._validate_followup_configuration_event(
        _later_policy_contract(), automation_id=seed.automation_id, event=configuration_events[0],
        configuration_evidence=evidence[seed.automation_id],
    ) is False
