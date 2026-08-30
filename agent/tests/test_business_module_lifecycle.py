from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.business_modules_api import create_business_module_router
from agent.core import AgentCore
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.models import Actor, ActorType, Command
from shared.business_module_repository import (
    BusinessModuleLifecycleError,
    BusinessModuleLifecycleService,
    BusinessModuleRepository,
)
from shared.business_modules import (
    BUSINESS_MODULE_BY_CODE,
    BUSINESS_MODULE_CATALOG,
    BUSINESS_MODULE_TOOL_OWNERS,
    CORE_MODULE_CODES,
    BusinessModuleCode,
)
import shared.business_module_repository as business_module_repository


def _business_module_migration_contract_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "business_module_migration_contract.py"
    spec = importlib.util.spec_from_file_location("_business_module_migration_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_split_sql_statements():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_migrations.py"
    spec = importlib.util.spec_from_file_location("_business_module_migration_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.split_sql_statements


class _LifecycleMigrationCursor:
    """Narrow information-schema fake for the 027 retry contract."""

    def __init__(self, contract: Any, *, existing_tables: set[str], rows: dict[str, dict[str, Any]] | None = None, invalid_table: str = "") -> None:
        self.contract = contract
        self.tables = set(existing_tables)
        self.rows = dict(rows or {})
        self.invalid_table = invalid_table
        self.created_tables: list[str] = []
        self.statements: list[str] = []
        self._one: dict[str, Any] | None = None
        self._many: list[dict[str, Any]] = []

    def execute(self, statement: str, params: tuple[Any, ...] = ()) -> None:
        sql = " ".join(statement.split())
        self.statements.append(sql)
        self._one = None
        self._many = []
        if sql.startswith("CREATE TABLE IF NOT EXISTS "):
            table = sql.split()[5]
            if table not in self.tables:
                self.tables.add(table)
                self.created_tables.append(table)
            return
        if sql.startswith("SELECT ENGINE, TABLE_COLLATION"):
            table = str(params[0])
            self._one = (
                {"ENGINE": "InnoDB", "TABLE_COLLATION": "utf8mb4_unicode_ci"}
                if table in self.tables
                else None
            )
            return
        if sql.startswith("SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE"):
            table = str(params[0])
            columns = dict(self.contract.BUSINESS_MODULE_LIFECYCLE_TABLES[table]["columns"])
            if table == self.invalid_table:
                columns.pop(next(iter(columns)))
            self._many = [
                {
                    "COLUMN_NAME": name,
                    "DATA_TYPE": data_type,
                    "COLUMN_TYPE": column_type,
                    "IS_NULLABLE": nullable,
                }
                for name, (data_type, column_type, nullable) in columns.items()
            ]
            return
        if sql.startswith("SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME"):
            table = str(params[0])
            self._many = [
                {
                    "INDEX_NAME": name,
                    "NON_UNIQUE": non_unique,
                    "SEQ_IN_INDEX": position,
                    "COLUMN_NAME": column,
                }
                for name, (non_unique, columns) in self.contract.BUSINESS_MODULE_LIFECYCLE_TABLES[table]["indexes"].items()
                for position, column in enumerate(columns, start=1)
            ]
            return
        if sql.startswith("SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE"):
            table = str(params[0])
            self._many = [
                {"CONSTRAINT_NAME": name, "CONSTRAINT_TYPE": constraint_type}
                for name, constraint_type in self.contract.BUSINESS_MODULE_LIFECYCLE_TABLES[table]["constraints"].items()
            ]
            return
        if sql.startswith("SELECT CONSTRAINT_NAME, UPDATE_RULE, DELETE_RULE, REFERENCED_TABLE_NAME"):
            self._many = [
                {
                    "CONSTRAINT_NAME": "fk_business_module_events_module",
                    "UPDATE_RULE": "RESTRICT",
                    "DELETE_RULE": "RESTRICT",
                    "REFERENCED_TABLE_NAME": "business_modules",
                }
            ]
            return
        if sql.startswith("INSERT INTO business_modules"):
            for item in BUSINESS_MODULE_CATALOG:
                self.rows.setdefault(
                    item.module_code,
                    {"lifecycle_state": "ENABLED", "installed_version": item.version},
                )
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._one

    def fetchall(self) -> list[dict[str, Any]]:
        return self._many


class _GateCursor:
    description = (("module_code",), ("code_version",), ("installed_version",), ("lifecycle_state",))

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.selected_rows = rows
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append(statement)
        if "WHERE module_code=%s" in statement:
            module_code = str((params or ("",))[0])
            self.selected_rows = [row for row in self.rows if row["module_code"] == module_code]

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.selected_rows]


class _GateCommands:
    def __init__(self, rows: list[dict[str, Any]], existing: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.rows = rows
        self.existing = existing if existing is not None else {}
        self.cursor_calls = 0
        self.cursor_instance: _GateCursor | None = None

    def cursor(self) -> _GateCursor:
        self.cursor_calls += 1
        self.cursor_instance = _GateCursor(self.rows)
        return self.cursor_instance

    def get_by_idempotency(self, source: str, key: str, *, for_update: bool) -> dict[str, Any] | None:
        assert for_update is True
        return self.existing.get((source, key))


class _GatewayUow:
    def __init__(self, rows: list[dict[str, Any]], existing: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.commands = _GateCommands(rows, existing)
        self.work_items = type("_WorkItems", (), {"add_entity": lambda *_args: None})()
        self.existing = existing
        self.created_count = 0
        self.committed = False

    def command_gateway_create(self, command_row: dict[str, Any], work_item_row: dict[str, Any], run_row: dict[str, Any], *_args: Any) -> dict[str, Any]:
        key = (str(command_row["source"]), str(command_row["idempotency_key"]))
        prior = self.existing.get(key)
        if prior is not None:
            return {**prior, "created": {"command": False}}
        self.created_count += 1
        receipt = {
            "command_id": command_row["command_id"],
            "work_item_id": work_item_row["work_item_id"],
            "run_id": run_row["run_id"],
            "created": {"command": True},
        }
        self.existing[key] = receipt
        return receipt

    def commit(self) -> None:
        self.committed = True


class _GatewayRepository:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.existing: dict[tuple[str, str], dict[str, Any]] = {}
        self.uows: list[_GatewayUow] = []

    def unit_of_work(self):
        repository = self

        class _Context:
            def __enter__(self) -> _GatewayUow:
                self.uow = _GatewayUow(repository.rows, repository.existing)
                repository.uows.append(self.uow)
                return self.uow

            def __exit__(self, *_args: Any) -> None:
                return None

        return _Context()

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "status": "RECEIVED"}


def _gate_rows() -> list[dict[str, Any]]:
    return [
        {"module_code": item.module_code, "code_version": item.version, "installed_version": item.version, "lifecycle_state": "ENABLED"}
        for item in BUSINESS_MODULE_CATALOG
    ]


def _gate_command(*, command_type: str = "tool.execute", tool_name: str = "query_waybill", key: str = "module-gate-key", source: str = "console", actor: Actor | None = None) -> Command:
    return Command(
        command_type=command_type,
        source=source,
        actor=actor or Actor(ActorType.CONSOLE_ADMIN, "admin-1", roles=("admin",)),
        parameters={"tool_name": tool_name, "arguments": {}},
        idempotency_key=key,
    )


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.description = None
        self._one: dict[str, Any] | None = None
        self._many: list[dict[str, Any]] = []
        self.rowcount = 0

    def execute(self, statement: str, params: tuple[Any, ...] = ()) -> None:
        sql = " ".join(statement.split())
        self.connection.statements.append((sql, params))
        self._one = None
        self._many = []
        if sql.startswith("SELECT module_code, action, request_fingerprint"):
            event = self.connection.events.get(str(params[0]))
            self._one = dict(event) if event else None
        elif sql.startswith("SELECT module_code, code_version") and "WHERE module_code=%s" in sql:
            row = self.connection.rows.get(str(params[0]))
            self._one = dict(row) if row else None
        elif sql.startswith("SELECT module_code FROM business_modules"):
            self._many = [{"module_code": code} for code in self.connection.rows]
        elif sql.startswith("UPDATE business_modules"):
            code_version, installed_version, state, record_version, module_code, expected_version = params
            row = self.connection.rows.get(str(module_code))
            self.rowcount = int(row is not None and row["record_version"] == expected_version)
            if self.rowcount:
                row.update(
                    {
                        "installed_version": installed_version,
                        "code_version": code_version,
                        "lifecycle_state": state,
                        "record_version": record_version,
                    }
                )
                self.connection.operations.append("update")
        elif sql.startswith("INSERT INTO business_module_events"):
            (
                module_code,
                request_id,
                fingerprint,
                action,
                actor_id,
                reason,
                before_json,
                after_json,
                record_version,
                code_version,
            ) = params
            self.connection.events[str(request_id)] = {
                "module_code": module_code,
                "request_id": request_id,
                "request_fingerprint": fingerprint,
                "action": action,
                "actor_id": actor_id,
                "reason": reason,
                "before_json": before_json,
                "after_json": after_json,
                "record_version": record_version,
                "code_version": code_version,
            }
            self.connection.operations.append("event")
            self.rowcount = 1
        elif sql.startswith("SELECT event_id, module_code"):
            self._many = []
        else:  # pragma: no cover - keeps this transaction fake intentionally closed
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> dict[str, Any] | None:
        return self._one

    def fetchall(self) -> list[dict[str, Any]]:
        return self._many

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.rows = {
            item.module_code: {
                "module_code": item.module_code,
                "code_version": item.version,
                "installed_version": item.version,
                "lifecycle_state": "ENABLED",
                "record_version": 1,
                "created_at": None,
                "updated_at": None,
            }
            for item in BUSINESS_MODULE_CATALOG
        }
        self.events: dict[str, dict[str, Any]] = {}
        self.operations: list[str] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def begin(self) -> None:
        self.operations.append("begin")

    def commit(self) -> None:
        self.operations.append("commit")

    def rollback(self) -> None:
        self.operations.append("rollback")

    def close(self) -> None:
        pass


def _repository(connection: _Connection) -> BusinessModuleRepository:
    return BusinessModuleRepository(lambda: connection)


def test_catalog_is_exact_immutable_14_menu_identity_set() -> None:
    codes = [item.module_code for item in BUSINESS_MODULE_CATALOG]

    assert len(codes) == 14
    assert len(set(codes)) == 14
    assert {item.module_code for item in BUSINESS_MODULE_CATALOG if not item.disable_allowed} == CORE_MODULE_CODES
    assert all(item.code_registered and item.version == "1.0.0" for item in BUSINESS_MODULE_CATALOG)


def test_catalog_contribution_contracts_are_exact_and_nonempty_for_manageable_modules() -> None:
    page_prefixes = [prefix for item in BUSINESS_MODULE_CATALOG for prefix in item.page_contributions]
    api_prefixes = [prefix for item in BUSINESS_MODULE_CATALOG for prefix in item.api_contributions]
    assert len(page_prefixes) == len(set(page_prefixes))
    assert len(api_prefixes) == len(set(api_prefixes))
    for item in BUSINESS_MODULE_CATALOG:
        if item.disable_allowed:
            assert item.api_contributions
    assert "/runtime" not in BUSINESS_MODULE_BY_CODE["waybill_entry"].api_contributions
    assert {"/waybills/manual", "/waybills/quote-options", "/original-pages", "/runtime/originals", "/runtime/artifacts", "/runtime/logs"}.issubset(
        set(BUSINESS_MODULE_BY_CODE["waybill_entry"].api_contributions)
    )
    assert "/runtime/finance_knowledge" in BUSINESS_MODULE_BY_CODE["finance"].api_contributions
    extensions = {item.module_code: set(item.internal_extensions) for item in BUSINESS_MODULE_CATALOG}
    assert "finance.ronghui.source.enabled" in extensions["finance"]
    assert "finance.yunda.source.not_launched" in extensions["finance"]
    assert "customer_service.ronghui.problem_source_adapter" in extensions["customer_service"]
    assert "tracking.line_haul.source_adapter" in extensions["tracking"]
    assert "automations.signed_action_package_platform" in extensions["automations"]
    assert "notification.feishu.background" in extensions["automations"]


def test_every_catalog_owned_tool_exists_exactly_once_in_registry() -> None:
    registry = (Path(__file__).resolve().parents[1] / "tools" / "registry.yaml").read_text(encoding="utf-8")
    registered = registry.splitlines()
    for item in BUSINESS_MODULE_CATALOG:
        for tool_name in item.tool_names:
            assert sum(line.strip() == f"- name: {tool_name}" for line in registered) == 1


def test_finance_analysis_command_is_owned_by_finance_module() -> None:
    assert BUSINESS_MODULE_TOOL_OWNERS["analyze_finance_reviews"] == "finance"
    assert "analyze_finance_reviews" in BUSINESS_MODULE_BY_CODE["finance"].tool_names


def test_migration_seeds_the_exact_enabled_baseline() -> None:
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "027_business_module_lifecycle.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS business_modules" in sql
    assert "CREATE TABLE IF NOT EXISTS business_module_events" in sql
    assert "CREATE TRIGGER" not in sql
    for item in BUSINESS_MODULE_CATALOG:
        assert f"('{item.module_code}', '1.0.0', '1.0.0', 'ENABLED', 1" in sql
    assert sql.count("'ENABLED', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)") == 14
    assert "idx_business_module_events_module_created (module_code, created_at, event_id)" in sql
    assert "ON DUPLICATE KEY UPDATE module_code = module_code" in sql


def test_027_retry_handles_fresh_and_partial_ddl_without_overwriting_state() -> None:
    contract = _business_module_migration_contract_module()
    path = Path(__file__).resolve().parents[1] / "migrations" / "027_business_module_lifecycle.sql"
    split_statements = _load_split_sql_statements()

    fresh = _LifecycleMigrationCursor(contract, existing_tables=set())
    contract.apply_business_module_lifecycle_migration(fresh, path, split_statements)
    assert fresh.created_tables == ["business_modules", "business_module_events"]
    assert set(fresh.rows) == {item.module_code for item in BUSINESS_MODULE_CATALOG}
    assert all(row["lifecycle_state"] == "ENABLED" for row in fresh.rows.values())

    partial = _LifecycleMigrationCursor(
        contract,
        existing_tables={"business_modules", "business_module_events"},
        rows={"finance": {"lifecycle_state": "DISABLED", "installed_version": "1.0.0"}},
    )
    contract.apply_business_module_lifecycle_migration(partial, path, split_statements)
    assert partial.created_tables == []
    assert partial.rows["finance"]["lifecycle_state"] == "DISABLED"
    assert set(partial.rows) == {item.module_code for item in BUSINESS_MODULE_CATALOG}


def test_027_retry_rejects_incompatible_partial_ddl_before_seed() -> None:
    contract = _business_module_migration_contract_module()
    path = Path(__file__).resolve().parents[1] / "migrations" / "027_business_module_lifecycle.sql"
    split_statements = _load_split_sql_statements()
    cursor = _LifecycleMigrationCursor(
        contract,
        existing_tables={"business_modules", "business_module_events"},
        invalid_table="business_module_events",
    )

    with pytest.raises(RuntimeError, match="Business module lifecycle schema mismatch"):
        contract.apply_business_module_lifecycle_migration(cursor, path, split_statements)
    assert not any(statement.startswith("INSERT INTO business_modules") for statement in cursor.statements)


def test_event_immutability_is_enforced_by_the_only_lifecycle_write_path() -> None:
    source = (Path(__file__).resolve().parents[2] / "shared" / "business_module_repository.py").read_text(encoding="utf-8")
    assert "INSERT INTO business_module_events" in source
    assert "UPDATE business_module_events" not in source
    assert "DELETE FROM business_module_events" not in source


def test_audit_uses_timestamp_then_stable_event_tiebreaker() -> None:
    source = (Path(__file__).resolve().parents[2] / "shared" / "business_module_repository.py").read_text(encoding="utf-8")
    assert "ORDER BY created_at DESC, event_id DESC" in source


def test_lifecycle_transition_locks_only_the_target_module_row() -> None:
    connection = _Connection()

    _repository(connection).change(
        module_code="receipts",
        action="disable",
        actor_id="admin-1",
        reason="test narrow lifecycle lock",
        request_id=str(uuid.uuid4()),
        expected_record_version=1,
    )

    locking_statements = [
        (sql, params) for sql, params in connection.statements if "FOR UPDATE" in sql
    ]
    assert len(locking_statements) == 1
    assert "WHERE module_code=%s FOR UPDATE" in locking_statements[0][0]
    assert locking_statements[0][1] == ("receipts",)
    assert any(
        sql == "SELECT module_code FROM business_modules"
        for sql, _params in connection.statements
    )


def test_transition_matrix_and_core_rejection() -> None:
    manageable = BusinessModuleCode("sample", "2.0.0", "Sample", ("sample",), ("/sample",), (), (), True)
    row = {"lifecycle_state": "NOT_INSTALLED", "installed_version": None}
    assert BusinessModuleRepository._transition(manageable, row, "install") == ("DISABLED", "2.0.0")
    row = {"lifecycle_state": "DISABLED", "installed_version": "1.0.0"}
    assert BusinessModuleRepository._transition(manageable, row, "enable") == ("ENABLED", "1.0.0")
    assert BusinessModuleRepository._transition(manageable, {"lifecycle_state": "ENABLED", "installed_version": "1.0.0"}, "disable") == ("DISABLED", "1.0.0")
    assert BusinessModuleRepository._transition(manageable, row, "uninstall") == ("NOT_INSTALLED", None)
    assert BusinessModuleRepository._transition(manageable, row, "upgrade") == ("DISABLED", "2.0.0")
    with pytest.raises(BusinessModuleLifecycleError, match="Core modules"):
        BusinessModuleRepository._transition(
            BUSINESS_MODULE_CATALOG[0],
            {"lifecycle_state": "ENABLED", "installed_version": "1.0.0"},
            "disable",
        )


def test_unknown_catalog_code_fails_closed_without_a_database_write() -> None:
    connection = _Connection()
    with pytest.raises(BusinessModuleLifecycleError) as exc_info:
        _repository(connection).change(
            module_code="not_registered",
            action="disable",
            actor_id="admin-1",
            reason="test",
            request_id=str(uuid.uuid4()),
            expected_record_version=1,
        )

    assert exc_info.value.code == "BLOCKED"
    assert connection.operations == []


def test_malformed_code_version_is_blocked_and_lifecycle_writes_fail_closed() -> None:
    connection = _Connection()
    connection.rows["receipts"]["code_version"] = "not-a-version"
    repository = _repository(connection)

    assert repository.get_module("receipts")["lifecycle_state"] == "BLOCKED"
    with pytest.raises(BusinessModuleLifecycleError) as exc_info:
        repository.change(
            module_code="receipts",
            action="disable",
            actor_id="admin-1",
            reason="test mismatch",
            request_id=str(uuid.uuid4()),
            expected_record_version=1,
        )
    assert exc_info.value.code == "BLOCKED"
    assert "update" not in connection.operations


def test_new_code_release_allows_upgrade_and_audits_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection()
    upgraded = BusinessModuleCode(
        "receipts", "2.0.0", "回单管理", ("receipts",), ("/receipts",), ("/receipts",),
        ("console.menu.receipts.view",), True,
    )
    catalog = tuple(upgraded if item.module_code == "receipts" else item for item in BUSINESS_MODULE_CATALOG)
    monkeypatch.setattr(business_module_repository, "BUSINESS_MODULE_BY_CODE", MappingProxyType({item.module_code: item for item in catalog}))
    connection.rows["receipts"]["code_version"] = "1.0.0"
    connection.rows["receipts"]["installed_version"] = "1.0.0"
    request_id = str(uuid.uuid4())
    before = _repository(connection).get_module("receipts")
    assert before["lifecycle_state"] == "BLOCKED"
    assert before["blocked_reason"] == "MODULE_UPGRADE_REQUIRED"
    assert before["upgrade_available"] is True

    result = _repository(connection).change(
        module_code="receipts", action="upgrade", actor_id="admin-1", reason="release 2.0.0",
        request_id=request_id, expected_record_version=1,
    )

    assert result["code_version"] == "2.0.0"
    assert result["installed_version"] == "2.0.0"
    event = connection.events[request_id]
    assert json.loads(event["before_json"])["code_version"] == "1.0.0"
    assert json.loads(event["after_json"])["code_version"] == "2.0.0"


def test_unknown_database_module_blocks_all_new_lifecycle_writes() -> None:
    connection = _Connection()
    connection.rows["unknown_legacy"] = {
        "module_code": "unknown_legacy",
        "code_version": "1.0.0",
        "installed_version": "1.0.0",
        "lifecycle_state": "ENABLED",
        "record_version": 1,
        "created_at": None,
        "updated_at": None,
    }

    with pytest.raises(BusinessModuleLifecycleError) as exc_info:
        _repository(connection).change(
            module_code="receipts",
            action="disable",
            actor_id="admin-1",
            reason="baseline must be closed",
            request_id=str(uuid.uuid4()),
            expected_record_version=1,
        )
    assert exc_info.value.code == "BLOCKED"
    assert "update" not in connection.operations


def test_lifecycle_event_and_change_commit_together_with_exact_idempotency_and_cas() -> None:
    connection = _Connection()
    repository = _repository(connection)
    request_id = str(uuid.uuid4())

    result = repository.change(
        module_code="receipts",
        action="disable",
        actor_id="admin-1",
        reason="maintenance window",
        request_id=request_id,
        expected_record_version=1,
    )
    assert result["lifecycle_state"] == "DISABLED"
    assert result["record_version"] == 2
    assert connection.operations == ["begin", "update", "event", "commit"]
    event = connection.events[request_id]
    assert json.loads(event["before_json"])["lifecycle_state"] == "ENABLED"
    assert json.loads(event["after_json"])["lifecycle_state"] == "DISABLED"

    replay = repository.change(
        module_code="receipts",
        action="disable",
        actor_id="admin-1",
        reason="maintenance window",
        request_id=request_id,
        expected_record_version=1,
    )
    assert replay["idempotent_replay"] is True
    assert connection.operations[-2:] == ["begin", "commit"]

    with pytest.raises(BusinessModuleLifecycleError) as reused:
        repository.change(
            module_code="receipts",
            action="enable",
            actor_id="admin-1",
            reason="different request",
            request_id=request_id,
            expected_record_version=2,
        )
    assert reused.value.code == "REQUEST_ID_REUSED"

    with pytest.raises(BusinessModuleLifecycleError) as stale:
        repository.change(
            module_code="receipts",
            action="enable",
            actor_id="admin-1",
            reason="restore",
            request_id=str(uuid.uuid4()),
            expected_record_version=1,
        )
    assert stale.value.code == "CAS_CONFLICT"


def test_command_gateway_accepts_new_fixed_module_commands_despite_legacy_state_drift() -> None:
    rows = _gate_rows()
    next(row for row in rows if row["module_code"] == "waybill_query")["lifecycle_state"] = "DISABLED"
    next(row for row in rows if row["module_code"] == "finance")["installed_version"] = "9.9.9"
    repository = _GatewayRepository(rows)
    gateway = CommandGateway(repository)

    first = gateway.submit(_gate_command(key="fixed-module-state-drift"))
    second = gateway.submit(_gate_command(tool_name="query_business_finance", key="fixed-module-version-drift"))

    assert first.reused is False and second.reused is False
    assert all(uow.commands.cursor_calls == 0 for uow in repository.uows)


def test_feishu_operating_summary_subqueries_create_distinct_gateway_commands() -> None:
    repository = _GatewayRepository(_gate_rows())
    gateway = CommandGateway(repository)
    actor = Actor(ActorType.FEISHU_USER, "bound-admin", roles=("admin",))
    finance_key = AgentCore._entry_idempotency_key(actor, "feishu", "business-summary-finance", "event-1")
    operations_key = AgentCore._entry_idempotency_key(actor, "feishu", "business-summary-operations", "event-1")

    first = gateway.submit(_gate_command(tool_name="query_business_finance", key=finance_key, source="feishu", actor=actor))
    second = gateway.submit(_gate_command(tool_name="query_automation_operations", key=operations_key, source="feishu", actor=actor))

    assert finance_key != operations_key
    assert first.reused is False and second.reused is False
    assert first.command_id != second.command_id
    assert sum(uow.created_count for uow in repository.uows) == 2


class _ApiService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_modules(self) -> dict[str, Any]:
        return {"release_sha": "development", "items": []}

    def catalog(self) -> dict[str, Any]:
        return {"release_sha": "development", "items": []}

    def get_module(self, module_code: str) -> dict[str, Any]:
        return {"release_sha": "development", "module": {"module_code": module_code}}

    def list_audit(self, module_code: str, *, limit: int = 200) -> dict[str, Any]:
        return {"release_sha": "development", "items": [{"module_code": module_code, "limit": limit}]}

    def change(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"release_sha": "development", "module": kwargs}


def test_api_keeps_legacy_lifecycle_reads_and_removes_lifecycle_write_route() -> None:
    service = _ApiService()
    admin_actor = Actor(ActorType.CONSOLE_ADMIN, "admin-1", roles=("admin",))
    calls: list[str] = []
    app = FastAPI()
    app.include_router(
        create_business_module_router(
            service_provider=lambda: service,  # type: ignore[arg-type]
            admin_actor_provider=lambda _request: calls.append("admin") or admin_actor,
        )
    )
    client = TestClient(app)

    assert client.get("/internal/v1/admin/modules").json()["ok"] is True
    assert client.get("/internal/v1/admin/modules/catalog").json()["ok"] is True
    assert client.get("/internal/v1/admin/modules/receipts/audit").json()["ok"] is True
    response = client.post(
        "/internal/v1/admin/modules/receipts/lifecycle",
        json={
            "action": "disable",
            "reason": "planned maintenance",
            "request_id": str(uuid.uuid4()),
            "expected_record_version": 1,
        },
    )
    assert response.status_code == 404
    assert calls == ["admin", "admin", "admin"]
    assert service.calls == []
