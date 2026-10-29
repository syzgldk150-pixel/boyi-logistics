"""Shared isolated-database helpers for the collector acceptance drills.

Each drill owns an explicit ``v32_*_test`` database on the loopback MySQL; no
production account directory or network destination is accessed.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

import pymysql

from agent.automation_plugins.developer_v2 import build_service_v2_package, init_service_v2_source
from agent.orchestration.models import Actor, ActorType
from shared.plugin_invocation_repository import TERMINAL_INVOCATION_STATUSES

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "v32_customer_flow_test"
ACTOR = Actor(ActorType.CONSOLE_ADMIN, "v32-collector-admin", roles=("super_admin",), authenticated_by="mysql_admin_session")


def connect(*, database=DATABASE):
    if (not re.fullmatch(r"v32_[a-z0-9_]+_test", database)
            or os.environ.get("AGENT_DB_NAME") != database
            or os.environ.get("AGENT_DB_HOST") != "127.0.0.1"
            or os.environ.get("PYTHON_DOTENV_DISABLED") != "1"):
        raise RuntimeError("the drill requires its explicit isolated database and disabled dotenv")
    return pymysql.connect(host="127.0.0.1", port=int(os.environ["AGENT_DB_PORT"]),
        user=os.environ["AGENT_DB_USER"], password=os.environ["AGENT_DB_PASS"],
        database=database, charset="utf8mb4", autocommit=False, cursorclass=pymysql.cursors.DictCursor)


def prepare_database(*, database=DATABASE, reset=False):
    from tests.test_mysql_orchestration_integration import MySqlOrchestrationIntegrationTests, _load_migration_runner
    if (not re.fullmatch(r"v32_[a-z0-9_]+_test", database)
            or os.environ.get("AGENT_DB_NAME") != database or os.environ.get("AGENT_DB_HOST") != "127.0.0.1"):
        raise RuntimeError("the drill cannot initialize another database")
    fixture = MySqlOrchestrationIntegrationTests
    fixture.pymysql, fixture.host = pymysql, "127.0.0.1"
    fixture.port, fixture.user = int(os.environ["AGENT_DB_PORT"]), os.environ["AGENT_DB_USER"]
    fixture.password, fixture.runner = os.environ["AGENT_DB_PASS"], _load_migration_runner()
    with pymysql.connect(host=fixture.host, port=fixture.port, user=fixture.user,
            password=fixture.password, autocommit=True) as connection, connection.cursor() as cursor:
        if reset:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    fixture._run_migrations(database)
    fixture._run_migrations(database, check_only=True)
    with connect(database=database) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE scheduled_tasks SET enabled=0")
        connection.commit()


def wait_invocation(invocation_id, *, timeout=60, connection_factory=connect):
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        with connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT invocation_id,status,error_code,error_summary,result_json FROM automation_plugin_invocations WHERE invocation_id=%s", (invocation_id,))
            row = cursor.fetchone()
        if row and row["status"] in TERMINAL_INVOCATION_STATUSES:
            raw_result = row.pop("result_json")
            row["result"] = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            return row
        threading.Event().wait(0.1)
    raise AssertionError(f"real Invocation completion bound exceeded: {row}")


def setup_unrelated(management):
    source = management.task_env / "unrelated-source"
    archive = management.task_env / "unrelated.zip"
    init_service_v2_source(source, plugin_id="v32_unrelated", name="无关计算任务", version="1.0.0")
    build_service_v2_package(source, archive)
    package = archive.read_bytes()
    result = management.management.install_service_v2(package, request_id=str(uuid4()),
        transport_package_sha256=sha256(package).hexdigest(),
        raw_intent=json.dumps({"instance_name": "无关计算任务", "permissions_confirmed": True}), actor=ACTOR)
    automation_id = result["automation_id"]
    entry = management.catalog.require(automation_id)
    management.management.save_plugin_settings(automation_id, config={}, account_bindings={}, resource_bindings={},
        request_id=str(uuid4()), expected_project_configuration_version=entry.project_config_version, actor=ACTOR)
    entry = management.catalog.require(automation_id)
    management.management.set_enabled(automation_id, enabled=True, request_id=str(uuid4()),
        expected_record_version=entry.record_version, actor=ACTOR)
    management.targets.reconcile_project(automation_id)
    return automation_id


def require_complete(invocation_id, *, connection_factory=connect):
    result = wait_invocation(invocation_id, connection_factory=connection_factory)
    if result["status"] != "COMPLETED":
        raise AssertionError(f"actual Invocation failed: {result}")
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT lease.generation,lease.outcome,version.plugin_version,
            lease.runtime_metadata_sha256,lease.released_at FROM automation_project_generation_leases lease
            JOIN automation_project_generations version ON version.automation_id=lease.automation_id AND version.generation=lease.generation
            WHERE lease.invocation_id=%s ORDER BY lease.acquired_at""", (invocation_id,))
        leases = cursor.fetchall()
        cursor.execute("SELECT action,outcome,COUNT(*) AS n FROM automation_write_attempt_receipts WHERE invocation_id=%s GROUP BY action,outcome ORDER BY action,outcome", (invocation_id,))
        receipts = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) AS n FROM agent_runs")
        assert cursor.fetchone()["n"] == 0, "direct collection must not create or resume a Run"
        cursor.execute("SELECT COUNT(*) AS n FROM agent_commands")
        assert cursor.fetchone()["n"] == 0, "direct collection must not create a Command"
    if not leases or any(lease["outcome"] not in {"SUCCEEDED", "WRITE_VERIFIED"} or not lease["released_at"] for lease in leases):
        raise AssertionError(f"actual generation lease did not finish: {leases}")
    for lease in leases:
        lease["released_at"] = lease["released_at"].isoformat() if lease["released_at"] else None
    return {**result, "leases": leases, "receipts": receipts}
