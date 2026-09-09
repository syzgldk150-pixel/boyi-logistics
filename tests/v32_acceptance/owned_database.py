"""Real migrations for an explicitly named, loopback-only synthetic database."""
import os
import re

import pymysql

ISOLATED_MYSQL_PORTS = frozenset({33326, 33330})


def connect_owned(expected_name):
    if (not re.fullmatch(r'v32_[a-z0-9_]+_test', expected_name)
            or os.environ.get('AGENT_DB_NAME') != expected_name
            or os.environ.get('AGENT_DB_HOST') != '127.0.0.1'
            or int(os.environ.get('AGENT_DB_PORT', '0')) not in ISOLATED_MYSQL_PORTS
            or os.environ.get('PYTHON_DOTENV_DISABLED') != '1'
            or os.environ.get('MIGRATION_ENV_FILE') != '/dev/null'):
        raise RuntimeError('Exact owned loopback database and disabled dotenv are required')
    return pymysql.connect(host='127.0.0.1', port=int(os.environ['AGENT_DB_PORT']), user=os.environ['AGENT_DB_USER'],
        password=os.environ['AGENT_DB_PASS'], database=expected_name,
        charset='utf8mb4', autocommit=False, cursorclass=pymysql.cursors.DictCursor)


def prepare_owned(expected_name):
    from tests.test_mysql_orchestration_integration import MySqlOrchestrationIntegrationTests, _load_migration_runner
    # Validate the destination before using its name in any database mutation.
    try:
        connection = connect_owned(expected_name)
    except pymysql.err.OperationalError as error:
        if error.args[0] != 1049:
            raise
    else:
        connection.close()
    fixture = MySqlOrchestrationIntegrationTests
    fixture.pymysql, fixture.runner = pymysql, _load_migration_runner()
    fixture.host, fixture.port = '127.0.0.1', int(os.environ['AGENT_DB_PORT'])
    fixture.user, fixture.password = os.environ['AGENT_DB_USER'], os.environ['AGENT_DB_PASS']
    with fixture._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f'DROP DATABASE IF EXISTS `{expected_name}`')
        cursor.execute(f'CREATE DATABASE `{expected_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    fixture._run_migrations(expected_name)
    fixture._run_migrations(expected_name, check_only=True)
    with connect_owned(expected_name) as connection, connection.cursor() as cursor:
        cursor.execute('UPDATE scheduled_tasks SET enabled=0')
        connection.commit()
