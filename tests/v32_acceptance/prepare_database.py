"""Prepare only the dedicated synthetic E2E MySQL database with real migrations."""
from __future__ import annotations
import argparse
import os
import sys

import pymysql
from tests.test_mysql_orchestration_integration import MySqlOrchestrationIntegrationTests, _load_migration_runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reset-owned-fixture',action='store_true')
    args = parser.parse_args()
    expected = {'AGENT_DB_HOST':'127.0.0.1','AGENT_DB_PORT':'33326','AGENT_DB_NAME':'v32_e2e_test',
        'PYTHON_DOTENV_DISABLED':'1','MIGRATION_ENV_FILE':'/dev/null'}
    if any(os.environ.get(key)!=value for key,value in expected.items()):
        raise RuntimeError('E2E migration requires exact isolated test environment')
    fixture = MySqlOrchestrationIntegrationTests
    fixture.pymysql,fixture.host,fixture.port = pymysql,'127.0.0.1',33326
    fixture.user,fixture.password = os.environ['AGENT_DB_USER'],os.environ['AGENT_DB_PASS']
    fixture.runner = _load_migration_runner()
    with pymysql.connect(host=fixture.host,port=fixture.port,user=fixture.user,password=fixture.password,autocommit=True) as connection,connection.cursor() as cursor:
        if args.reset_owned_fixture:
            cursor.execute('DROP DATABASE IF EXISTS v32_e2e_test')
        cursor.execute('CREATE DATABASE IF NOT EXISTS v32_e2e_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
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
