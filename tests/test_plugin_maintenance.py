from __future__ import annotations

from pathlib import Path

import pytest

from scripts.plugin_maintenance import PLUGIN_TESTS, maintenance_scope, selected_tests


def test_local_test_map_references_real_tests_and_has_each_required_business() -> None:
    packages = Path(__file__).resolve().parents[1] / "agent" / "service_v2_plugins"
    for plugin_id in PLUGIN_TESTS:
        assert selected_tests(plugin_id)
        assert (packages / plugin_id / "manifest.json").is_file()
    assert {
        "sync_scan_codes_v2", "sync_arrival_stats_v2", "self_pickup_problem_upload_v2",
        "split_pending_problem_upload_v2", "sync_customer_service_problems_v2",
    } <= PLUGIN_TESTS.keys()


def test_core_or_unrelated_plugin_changes_cannot_be_classified_local() -> None:
    selected = "sync_customer_service_problems_v2"
    local = "agent/service_v2_plugins/" + selected + "/payload/action.py"
    assert maintenance_scope(selected, [local])["scope"] == "PLUGIN_ONLY_CANDIDATE"
    for path in (
        "shared/data_sources.py", "agent/migrations/039_module_data_sources.sql",
        "agent/agent/orchestration/workflow_runner.py", "agent/requirements.lock",
    ):
        result = maintenance_scope(selected, [local, path])
        assert result["scope"] == "CORE_UPDATE_REQUIRED"
        assert result["core_paths"] == [path]
    other = 'agent/service_v2_plugins/sync_scan_codes_v2/payload/action.py'
    result = maintenance_scope(selected, [local, other])
    assert result['scope'] == 'MULTI_PLUGIN_REVIEW_REQUIRED'
    assert result['other_plugin_paths'] == [other]
    assert result['core_paths'] == []
    support = ['docs/maintenance.md', 'tests/v32_acceptance/decision_payload_test.py']
    result = maintenance_scope(selected, [local, *support])
    assert result['scope'] == 'PLUGIN_ONLY_CANDIDATE'
    assert result['supporting_paths'] == support


def test_v2_packaging_rejects_real_payload_change_after_test_snapshot(tmp_path: Path, monkeypatch) -> None:
    import shutil
    from scripts import plugin_maintenance as maintenance

    plugin_id = 'sync_scan_codes_v2'
    original_root = maintenance.PROJECT_ROOT
    source = tmp_path / 'agent/service_v2_plugins' / plugin_id
    source.parent.mkdir(parents=True)
    shutil.copytree(original_root / 'agent/service_v2_plugins' / plugin_id, source)
    for node in maintenance.selected_tests(plugin_id):
        relative = node.split('::', 1)[0]
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_root / relative, target)
    monkeypatch.setattr(maintenance, 'PROJECT_ROOT', tmp_path)
    tested = maintenance.v2_test_material(plugin_id)
    action = source / 'payload/action.py'
    action.write_text(action.read_text() + '\n# actual changed candidate after testing\n')
    output = tmp_path / 'candidate.zip'
    with pytest.raises(ValueError, match='changed after testing'):
        maintenance.package_plugin(plugin_id, output, expected_material=tested)
    assert not output.exists()
