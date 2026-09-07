"""The isolated E2E reset must not race live fixtures or follow other paths."""
import os
import shutil
import stat
import sys

import pytest

from agent.automation_plugins.errors import PluginPackageError
from tests.v32_acceptance import management_fixture, prepare_database


@pytest.fixture
def owned_environment(monkeypatch, tmp_path):
    root = tmp_path / 'environment'
    root.mkdir()
    monkeypatch.setattr(management_fixture, 'TASK_ENV', root)
    for key, value in {'AGENT_DB_HOST': '127.0.0.1', 'AGENT_DB_PORT': '33326',
            'AGENT_DB_NAME': 'v32_e2e_test', 'PYTHON_DOTENV_DISABLED': '1', 'MIGRATION_ENV_FILE': '/dev/null'}.items():
        monkeypatch.setenv(key, value)
    return root


def test_reset_refuses_live_participant_before_database_mutation(owned_environment, monkeypatch):
    entered = []
    monkeypatch.setattr(prepare_database, '_prepare_locked', lambda **kwargs: entered.append(kwargs))
    monkeypatch.setattr(sys, 'argv', ['prepare_database', '--reset-owned-fixture'])
    with management_fixture.e2e_fixture_lock():
        with pytest.raises(RuntimeError, match='in use'):
            prepare_database.main()
    assert entered == []


def test_participant_refuses_exclusive_reset_and_reopens_after_release(owned_environment):
    with management_fixture.e2e_fixture_lock(exclusive=True):
        with pytest.raises(RuntimeError, match='in use'):
            management_fixture.e2e_fixture_lock()
    with management_fixture.e2e_fixture_lock():
        pass


def test_runtime_reset_is_scoped_and_rejects_redirected_installation(owned_environment, tmp_path):
    paths = prepare_database.owned_runtime_paths()
    assert tuple(path.name for path in paths) == ('plugin-installed', 'synthetic-plugins', 'console-runtime')
    assert all(path.parent == owned_environment for path in paths)
    outside = tmp_path / 'other-project'
    outside.mkdir()
    (owned_environment / 'plugin-installed').symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match='exact owned directory'):
        prepare_database.owned_runtime_paths()
    assert outside.is_dir()


def test_runtime_reset_removes_real_readonly_installation_and_preserves_neighbors(owned_environment):
    installation = owned_environment / 'plugin-installed'
    package = installation / 'synthetic_plugin' / '1.0.0' / 'package' / 'payload'
    package.mkdir(parents=True)
    payload = package / 'main.py'
    payload.write_text('print("synthetic fixture")\n', encoding='utf-8')
    payload.chmod(0o444)
    for directory in (package, *package.parents):
        if directory == owned_environment:
            break
        directory.chmod(0o555)
    neighbor = owned_environment / 'preserved-evidence.json'
    neighbor.write_text('{}\n', encoding='utf-8')
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            shutil.rmtree(installation)
    assert payload.exists()
    assert stat.S_IMODE(package.stat().st_mode) == 0o555
    with management_fixture.e2e_fixture_lock(exclusive=True):
        prepare_database._reset_owned_runtime()
    assert not installation.exists()
    assert neighbor.read_text(encoding='utf-8') == '{}\n'


def test_runtime_reset_rejects_nested_link_before_chmod_or_deletion(owned_environment, tmp_path):
    installation = owned_environment / 'plugin-installed'
    installation.mkdir()
    payload = installation / 'main.py'
    payload.write_text('synthetic', encoding='utf-8')
    payload.chmod(0o444)
    outside = tmp_path / 'unrelated.txt'
    outside.write_text('preserve', encoding='utf-8')
    outside.chmod(0o444)
    sources = owned_environment / 'synthetic-plugins'
    sources.mkdir()
    (sources / 'external.py').symlink_to(outside)
    with management_fixture.e2e_fixture_lock(exclusive=True):
        with pytest.raises(PluginPackageError, match='symbolic links'):
            prepare_database._reset_owned_runtime()
    assert payload.read_text(encoding='utf-8') == 'synthetic'
    assert stat.S_IMODE(payload.stat().st_mode) == 0o444
    assert outside.read_text(encoding='utf-8') == 'preserve'
    assert stat.S_IMODE(outside.stat().st_mode) == 0o444
