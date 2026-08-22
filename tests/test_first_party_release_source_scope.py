from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent.automation_plugins.first_party import release_first_party_plugin_ids
from scripts.first_party_release_scope import (
    ReleaseScopeError,
    deferred_source_files,
    release_plugin_ids,
    release_source_files,
    test_files as selected_test_files,
    verify_staged_tree,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY_ROOT = REPOSITORY_ROOT / "agent" / "first_party_automation_plugins"


def test_ast_scope_matches_runtime_allowlist_without_loading_deferred_payloads() -> None:
    selected = release_plugin_ids(REPOSITORY_ROOT)
    release_paths = set(release_source_files(REPOSITORY_ROOT))
    deferred_paths = set(deferred_source_files(REPOSITORY_ROOT))

    assert selected == release_first_party_plugin_ids()
    assert release_paths
    assert release_paths.isdisjoint(deferred_paths)
    for plugin_id in selected:
        assert FIRST_PARTY_ROOT / plugin_id / "payload" / "action.py" in release_paths
    assert all(
        not path.is_relative_to(FIRST_PARTY_ROOT / plugin_id)
        for path in release_paths
        for plugin_id in release_first_party_plugin_ids() ^ {
            path.name
            for path in FIRST_PARTY_ROOT.iterdir()
            if path.is_dir() and path.name != "_runtime"
        }
    )


def test_blocked_source_tests_are_audited_outside_the_release_gate() -> None:
    gate = set(selected_test_files(REPOSITORY_ROOT, scope="gate", suite="root"))
    deferred = set(
        selected_test_files(REPOSITORY_ROOT, scope="deferred", suite="root")
    )
    selected = release_plugin_ids(REPOSITORY_ROOT)

    assert gate.isdisjoint(deferred)
    assert REPOSITORY_ROOT / "tests" / "test_automation_plugin_platform.py" in gate
    assert REPOSITORY_ROOT / "tests" / "test_automation_plugin_release_scope.py" in gate
    assert REPOSITORY_ROOT / "tests" / "test_first_party_action_payloads.py" in gate
    assert REPOSITORY_ROOT / "tests" / "test_first_party_plugin_release_builder.py" in gate
    assert REPOSITORY_ROOT / "tests" / "test_automation_plugin_platform.py" not in deferred
    assert REPOSITORY_ROOT / "tests" / "test_first_party_action_payloads.py" not in deferred
    dedicated_tests = {
        "self_pickup_problem_upload": "test_self_pickup_problem_upload_plugin_action.py",
        "split_pending_problem_upload": "test_split_pending_problem_upload_plugin_action.py",
        "sync_daily_send_orders": "test_sync_daily_send_orders_plugin_action.py",
        "sync_daily_should_sign": "test_sync_daily_should_sign_action_payload.py",
        "sync_delivery_status": "test_delivery_status_action_payload.py",
        "sync_finance_bills": "test_sync_finance_bills_action_payload.py",
        "sync_scan_codes": "test_scan_codes_action_payload.py",
        "sync_site_send_list": "test_site_send_action_payload.py",
    }
    for plugin_id, name in dedicated_tests.items():
        path = REPOSITORY_ROOT / "tests" / name
        assert (path in gate) is (plugin_id in selected)
        assert (path in deferred) is (plugin_id not in selected)


def _copy_staged_scope(destination: Path) -> None:
    release_scope = (
        REPOSITORY_ROOT
        / "agent"
        / "agent"
        / "automation_plugins"
        / "release_scope.py"
    )
    staged_scope = (
        destination / "agent" / "agent" / "automation_plugins" / "release_scope.py"
    )
    staged_scope.parent.mkdir(parents=True)
    shutil.copyfile(release_scope, staged_scope)
    runtime = destination / "agent" / "first_party_automation_plugins" / "_runtime"
    runtime.mkdir(parents=True)
    for name in ("main.py", "result.py"):
        shutil.copyfile(FIRST_PARTY_ROOT / "_runtime" / name, runtime / name)
    for plugin_id in release_first_party_plugin_ids():
        action = (
            destination
            / "agent"
            / "first_party_automation_plugins"
            / plugin_id
            / "payload"
            / "action.py"
        )
        action.parent.mkdir(parents=True)
        shutil.copyfile(
            FIRST_PARTY_ROOT / plugin_id / "payload" / "action.py",
            action,
        )


def test_staged_scope_rejects_even_one_deferred_package_directory(tmp_path: Path) -> None:
    _copy_staged_scope(tmp_path)
    verify_staged_tree(tmp_path)

    deferred_id = next(
        path.name
        for path in FIRST_PARTY_ROOT.iterdir()
        if path.is_dir()
        and path.name != "_runtime"
        and path.name not in release_first_party_plugin_ids()
    )
    deferred_action = (
        tmp_path
        / "agent"
        / "first_party_automation_plugins"
        / deferred_id
        / "payload"
        / "action.py"
    )
    deferred_action.parent.mkdir(parents=True)
    deferred_action.write_text("this is deliberately not parsed\n", encoding="utf-8")

    with pytest.raises(ReleaseScopeError, match="unexpected"):
        verify_staged_tree(tmp_path)


def test_publish_and_remote_preflight_share_the_fail_closed_scope_helper() -> None:
    publisher = (
        REPOSITORY_ROOT / "agent" / "deploy" / "publish_to_ecs.ps1"
    ).read_text(encoding="utf-8")
    remote = (
        REPOSITORY_ROOT / "agent" / "deploy" / "remote_release.sh"
    ).read_text(encoding="utf-8")

    assert "function Get-ReleaseFirstPartyPluginIds" in publisher
    assert "function Test-ReleaseScopedFirstPartyPath" in publisher
    assert "function Invoke-ReleaseScopeHelper" in publisher
    assert "wsl.exe -d Ubuntu --exec /usr/bin/python3" in publisher
    assert "Test-ReleaseScopedFirstPartyPath $relative $releasePluginIds" in publisher
    assert 'Invoke-ReleaseScopeHelper $PayloadRoot @("verify-staged")' in publisher
    assert '"plugin_core_adapters"' in publisher
    assert "preflight_staged_first_party_source_scope" in remote
    assert '--repository-root "${STAGE_ROOT}" verify-staged' in remote
    assert remote.index("preflight_staged_first_party_source_scope") < remote.index(
        '"${runtime_python}" -m compileall'
    )
