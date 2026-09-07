"""Prepare only the dedicated synthetic E2E MySQL database with real migrations."""
from __future__ import annotations
import argparse
import os
import sys

import pymysql
from agent.automation_plugins.storage import FilesystemPluginStorage, validate_plugin_tree
from tests.test_mysql_orchestration_integration import MySqlOrchestrationIntegrationTests, _load_migration_runner


def owned_runtime_paths():
    """Validate all exact E2E runtime destinations before any reset mutation."""
    from tests.v32_acceptance.management_fixture import E2E_RUNTIME_SUBDIRECTORIES, TASK_ENV

    if TASK_ENV.resolve() != TASK_ENV:
        raise RuntimeError('E2E environment directory must not resolve through a symlink')
    paths = tuple(TASK_ENV / name for name in E2E_RUNTIME_SUBDIRECTORIES)
    for path in paths:
        if path.is_symlink() or path.resolve() != path or (path.exists() and not path.is_dir()):
            raise RuntimeError('E2E reset destination is not an exact owned directory: ' + str(path))
        if path.exists():
            validate_plugin_tree(path)
    return paths


def _reset_owned_runtime():
    """Caller holds the exclusive lock; reuse storage's link-safe read-only cleanup."""
    for path in owned_runtime_paths():
        if path.exists():
            FilesystemPluginStorage._remove_tree(path)
        print('owned_runtime_reset=' + str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset-owned-fixture',action='store_true')
    args = parser.parse_args()
    expected = {'AGENT_DB_HOST':'127.0.0.1','AGENT_DB_PORT':'33326','AGENT_DB_NAME':'v32_e2e_test',
        'PYTHON_DOTENV_DISABLED':'1','MIGRATION_ENV_FILE':'/dev/null'}
    if any(os.environ.get(key)!=value for key,value in expected.items()):
        raise RuntimeError('E2E migration requires exact isolated test environment')
    from tests.v32_acceptance.management_fixture import e2e_fixture_lock

    with e2e_fixture_lock(exclusive=True):
        _prepare_locked(reset_owned_fixture=args.reset_owned_fixture)


def _prepare_locked(*, reset_owned_fixture):
    # Caller holds the exclusive E2E lock for both filesystem and database work.
    if reset_owned_fixture:
        owned_runtime_paths()
    fixture = MySqlOrchestrationIntegrationTests
    fixture.pymysql,fixture.host,fixture.port = pymysql,'127.0.0.1',33326
    fixture.user,fixture.password = os.environ['AGENT_DB_USER'],os.environ['AGENT_DB_PASS']
    fixture.runner = _load_migration_runner()
    with pymysql.connect(host=fixture.host,port=fixture.port,user=fixture.user,password=fixture.password,autocommit=True) as connection,connection.cursor() as cursor:
        if reset_owned_fixture:
            cursor.execute('DROP DATABASE IF EXISTS v32_e2e_test')
        cursor.execute('CREATE DATABASE IF NOT EXISTS v32_e2e_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    if reset_owned_fixture:
        _reset_owned_runtime()
    fixture._run_migrations('v32_e2e_test')
    fixture._run_migrations('v32_e2e_test',check_only=True)
    with fixture._connection('v32_e2e_test',autocommit=True) as connection,connection.cursor() as cursor:
        cursor.execute('UPDATE scheduled_tasks SET enabled=0')
        cursor.execute('SELECT COUNT(*) AS pending FROM scheduled_tasks WHERE enabled<>0')
        if cursor.fetchone()['pending']:
            raise AssertionError('synthetic schedules remain enabled')
    print('v32_e2e_test migration_check=ok physical_schedules=disabled')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
