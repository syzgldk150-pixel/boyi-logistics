"""Upgrade a task-owned pre-052 MySQL database without touching old business rows."""
import os
from uuid import uuid4

import pymysql
import pytest

from shared.problem_write_intents import ProblemWriteIntents, UnresolvedProblemWrite
from tests import test_mysql_orchestration_integration as support

pytestmark = pytest.mark.skipif(os.getenv('RUN_MYSQL_INTEGRATION') != '1', reason='isolated MySQL required')


def test_052_upgrade_preserves_all_old_rows_and_reentry_keeps_write_facts():
    helper = type('ProblemIntentUpgrade', (support.MySqlOrchestrationIntegrationTests,), {})
    helper.pymysql = pymysql
    helper.host, helper.port = os.environ['AGENT_DB_HOST'], int(os.environ['AGENT_DB_PORT'])
    helper.user, helper.password = os.environ['AGENT_DB_USER'], os.environ['AGENT_DB_PASS']
    helper.database = 'problem_upgrade_' + uuid4().hex + '_test'
    helper.runner = support._load_migration_runner()
    assert helper.host == '127.0.0.1'
    with helper._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE `{helper.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    try:
        helper._apply_through(helper.database, '017')
        helper._seed_required_project_resources(helper.database)
        helper._apply_through(helper.database, '051')
        with helper._connection(autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute('''INSERT INTO boyi_waybills
                (id,waybill_no,open_date,receiver_address,goods_name_lines,package_type_lines,
                 quantity_lines,freight_fee,remark,source,created_at,updated_at)
                VALUES(7252,'OWNED-UPGRADE','2026-09-15','隔离地址','隔离货物','纸箱',
                 '7','123.4500','保留人工备注','manual','2026-09-15 12:00:00','2026-09-15 12:00:00')''')
            cursor.execute('SHOW TABLES')
            tables = sorted(next(iter(row.values())) for row in cursor.fetchall())
        def snapshot():
            with helper._connection() as connection, connection.cursor() as cursor:
                result = {}
                for table in tables:
                    if table != 'schema_migrations':
                        cursor.execute(f'SELECT * FROM `{table}`')
                        result[table] = sorted(cursor.fetchall(), key=repr)
                return result
        before = snapshot()
        helper._run_migrations(helper.database)
        assert snapshot() == before
        store = ProblemWriteIntents(helper._connection)
        target = 'a' * 64
        attempt = store.reserve(target)
        helper._run_migrations(helper.database)
        assert snapshot() == before
        with pytest.raises(UnresolvedProblemWrite):
            store.reserve(target)
        store.settle(target, attempt, 'NOT_APPLIED')
        second = store.reserve(target)
        assert second != attempt
        store.settle(target, second, 'VERIFIED')
        with pytest.raises(UnresolvedProblemWrite):
            store.reserve(target)
        helper._run_migrations(helper.database, check_only=True)
    finally:
        with helper._server_connection() as connection, connection.cursor() as cursor:
            cursor.execute(f'DROP DATABASE `{helper.database}`')
