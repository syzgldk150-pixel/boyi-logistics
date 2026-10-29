"""Module-scoped fresh MySQL database with every migration applied (isolated test server only)."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

from tests import test_mysql_orchestration_integration as mysql_helpers
from tests.mysql_database_cleanup import drop_test_database


@pytest.fixture(scope="module")
def database():
    import pymysql

    # Reuse the established real migration fixture, but only create this test
    # module's fresh UUID database rather than the whole suite matrix.
    case = type("MigratedDatabase", (mysql_helpers.MySqlOrchestrationIntegrationTests,), {})
    case.pymysql = pymysql
    case.host = os.environ["AGENT_DB_HOST"]
    case.port = int(os.environ["AGENT_DB_PORT"])
    case.user = os.environ["AGENT_DB_USER"]
    case.password = os.environ["AGENT_DB_PASS"]
    case.database = f"test_migrated_{uuid4().hex}"
    case.runner = mysql_helpers._load_migration_runner()
    with case._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{case.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        case._run_migrations(case.database)
        yield case
    finally:
        with case._server_connection() as connection, connection.cursor() as cursor:
            drop_test_database(cursor, case.database)
