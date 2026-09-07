from __future__ import annotations

from pathlib import Path

import pytest

from scripts.plugin_maintenance import PLUGIN_TESTS, maintenance_scope, package_plugin, selected_tests


def test_local_test_map_references_real_tests_and_has_each_required_business() -> None:
    for plugin_id in PLUGIN_TESTS:
        assert selected_tests(plugin_id)
    assert {
        "sync_scan_codes", "sync_arrival_stats", "self_pickup_problem_upload",
        "split_pending_problem_upload", "sync_finance_bills", "sync_customer_service_problems",
    } <= PLUGIN_TESTS.keys()


def test_core_or_unrelated_plugin_changes_cannot_be_classified_local() -> None:
    selected = "sync_customer_service_problems"
    local = "agent/first_party_automation_plugins/" + selected + "/payload/customer_problem_fields.py"
    assert maintenance_scope(selected, [local])["scope"] == "PLUGIN_ONLY_CANDIDATE"
    for path in (
        "shared/data_sources.py", "agent/migrations/039_module_data_sources.sql",
        "agent/agent/orchestration/workflow_runner.py", "agent/requirements.lock",
    ):
        result = maintenance_scope(selected, [local, path])
        assert result["scope"] == "CORE_UPDATE_REQUIRED"
        assert result["core_paths"] == [path]
    other = 'agent/first_party_automation_plugins/sync_scan_codes/payload/action.py'
    result = maintenance_scope(selected, [local, other])
    assert result['scope'] == 'MULTI_PLUGIN_REVIEW_REQUIRED'
    assert result['other_plugin_paths'] == [other]
    assert result['core_paths'] == []
    support = ['docs/maintenance.md', 'tests/v32_acceptance/decision_payload_test.py']
    result = maintenance_scope(selected, [local, *support])
    assert result['scope'] == 'PLUGIN_ONLY_CANDIDATE'
    assert result['supporting_paths'] == support


def test_test_only_v1_package_uses_real_payload_and_verifies_signature(tmp_path: Path) -> None:
    from agent.automation_plugins.package import Ed25519TrustStore, verify_signed_plugin_zip

    output = tmp_path / "customer-test.zip"
    result = package_plugin("sync_customer_service_problems", output,
        version="0.0.901", test_signing=True, signing_key_env=None, key_id=None)
    assert result["trust"] == "TEST_ONLY"
    verified = verify_signed_plugin_zip(output,
        verifier=Ed25519TrustStore({result["key_id"]: bytes.fromhex(result["public_key_hex"])}))
    assert verified.manifest.plugin_id == "sync_customer_service_problems"
    assert {entry.path for entry in verified.files} >= {"payload/action.py", "payload/customer_problem_fields.py"}
    assert not list(tmp_path.glob("*.pem"))
    with pytest.raises(ValueError, match="new ZIP"):
        package_plugin("sync_customer_service_problems", output,
            version="0.0.902", test_signing=True, signing_key_env=None, key_id=None)


def test_v1_missing_injected_key_fails_without_creating_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ISOLATED_MISSING_SIGNER", raising=False)
    output = tmp_path / "customer.zip"
    with pytest.raises(ValueError, match="not injected"):
        package_plugin("sync_customer_service_problems", output,
            version="0.0.901", test_signing=False,
            signing_key_env="ISOLATED_MISSING_SIGNER", key_id="isolated")
    assert not output.exists()
