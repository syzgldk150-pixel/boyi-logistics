"""Bounded schema contract for the rerunnable 027 business-module migration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


BUSINESS_MODULE_LIFECYCLE_VERSION = "027"
BUSINESS_MODULE_LIFECYCLE_TABLES = {
    "business_modules": {
        "columns": {
            "module_code": ("varchar", "varchar(64)", "NO"),
            "code_version": ("varchar", "varchar(32)", "NO"),
            "installed_version": ("varchar", "varchar(32)", "YES"),
            "lifecycle_state": (
                "enum",
                "enum('NOT_INSTALLED','DISABLED','ENABLED','BLOCKED')",
                "NO",
            ),
            "record_version": ("bigint", "bigint unsigned", "NO"),
            "created_at": ("datetime", "datetime(6)", "NO"),
            "updated_at": ("datetime", "datetime(6)", "NO"),
        },
        "indexes": {"PRIMARY": (0, ("module_code",))},
        "constraints": {
            "PRIMARY": "PRIMARY KEY",
            "chk_business_modules_version": "CHECK",
            "chk_business_modules_installed_version": "CHECK",
            "chk_business_modules_record_version": "CHECK",
        },
    },
    "business_module_events": {
        "columns": {
            "event_id": ("char", "char(36)", "NO"),
            "module_code": ("varchar", "varchar(64)", "NO"),
            "request_id": ("char", "char(36)", "NO"),
            "request_fingerprint": ("char", "char(64)", "NO"),
            "action": (
                "enum",
                "enum('install','enable','disable','upgrade','uninstall')",
                "NO",
            ),
            "actor_id": ("varchar", "varchar(191)", "NO"),
            "reason": ("varchar", "varchar(500)", "NO"),
            "before_json": ("json", "json", "NO"),
            "after_json": ("json", "json", "NO"),
            "record_version": ("bigint", "bigint unsigned", "NO"),
            "code_version": ("varchar", "varchar(32)", "NO"),
            "created_at": ("datetime", "datetime(6)", "NO"),
        },
        "indexes": {
            "PRIMARY": (0, ("event_id",)),
            "uq_business_module_events_request": (0, ("request_id",)),
            "idx_business_module_events_module_created": (
                1,
                ("module_code", "created_at", "event_id"),
            ),
        },
        "constraints": {
            "PRIMARY": "PRIMARY KEY",
            "uq_business_module_events_request": "UNIQUE",
            "chk_business_module_events_reason": "CHECK",
            "chk_business_module_events_version": "CHECK",
            "fk_business_module_events_module": "FOREIGN KEY",
        },
    },
}


def _schema_error(reason: str) -> None:
    raise RuntimeError(f"Business module lifecycle schema mismatch: {reason}")


def validate_business_module_lifecycle_schema(cursor: Any) -> None:
    """Fail before seed if interrupted 027 DDL left a different schema."""

    for table_name, contract in BUSINESS_MODULE_LIFECYCLE_TABLES.items():
        cursor.execute(
            """
            SELECT ENGINE, TABLE_COLLATION
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (table_name,),
        )
        table = cursor.fetchone()
        if not isinstance(table, Mapping):
            _schema_error(f"{table_name}: missing")
        if str(table.get("ENGINE") or "").lower() != "innodb" or str(
            table.get("TABLE_COLLATION") or ""
        ).lower() != "utf8mb4_unicode_ci":
            _schema_error(f"{table_name}: table definition")

        cursor.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (table_name,),
        )
        columns = {
            str(row.get("COLUMN_NAME")): (
                str(row.get("DATA_TYPE") or "").lower(),
                str(row.get("COLUMN_TYPE") or "").lower(),
                str(row.get("IS_NULLABLE") or "").upper(),
            )
            for row in (cursor.fetchall() or [])
            if isinstance(row, Mapping)
        }
        expected_columns = {
            name: (data_type.lower(), column_type.lower(), nullable)
            for name, (data_type, column_type, nullable) in contract["columns"].items()
        }
        if columns != expected_columns:
            _schema_error(f"{table_name}: columns")

        cursor.execute(
            """
            SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            ORDER BY INDEX_NAME, SEQ_IN_INDEX
            """,
            (table_name,),
        )
        indexes: dict[str, tuple[int, tuple[str, ...]]] = {}
        for row in cursor.fetchall() or []:
            if not isinstance(row, Mapping):
                _schema_error(f"{table_name}: index row")
            name = str(row.get("INDEX_NAME") or "")
            non_unique = int(row.get("NON_UNIQUE"))
            previous = indexes.get(name)
            if previous is None:
                indexes[name] = (non_unique, (str(row.get("COLUMN_NAME") or ""),))
            elif previous[0] != non_unique:
                _schema_error(f"{table_name}: index uniqueness")
            else:
                indexes[name] = (non_unique, (*previous[1], str(row.get("COLUMN_NAME") or "")))
        if indexes != contract["indexes"]:
            _schema_error(f"{table_name}: indexes")

        cursor.execute(
            """
            SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE
            FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (table_name,),
        )
        constraints = {
            str(row.get("CONSTRAINT_NAME")): str(row.get("CONSTRAINT_TYPE") or "").upper()
            for row in (cursor.fetchall() or [])
            if isinstance(row, Mapping)
        }
        if constraints != contract["constraints"]:
            _schema_error(f"{table_name}: constraints")

    cursor.execute(
        """
        SELECT CONSTRAINT_NAME, UPDATE_RULE, DELETE_RULE, REFERENCED_TABLE_NAME
        FROM information_schema.REFERENTIAL_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = DATABASE()
          AND TABLE_NAME = 'business_module_events'
        """
    )
    foreign_keys = [row for row in (cursor.fetchall() or []) if isinstance(row, Mapping)]
    if foreign_keys != [
        {
            "CONSTRAINT_NAME": "fk_business_module_events_module",
            "UPDATE_RULE": "RESTRICT",
            "DELETE_RULE": "RESTRICT",
            "REFERENCED_TABLE_NAME": "business_modules",
        }
    ]:
        _schema_error("business_module_events: foreign key")


def apply_business_module_lifecycle_migration(
    cursor: Any,
    path: Path,
    split_statements: Callable[[str], list[str]],
) -> None:
    """Apply 027 safely after an interrupted auto-committed DDL attempt."""

    statements = split_statements(path.read_text(encoding="utf-8"))
    expected_prefixes = (
        "CREATE TABLE IF NOT EXISTS business_modules",
        "CREATE TABLE IF NOT EXISTS business_module_events",
        "INSERT INTO business_modules",
    )
    if len(statements) != len(expected_prefixes) or any(
        not statement.startswith(prefix)
        for statement, prefix in zip(statements, expected_prefixes)
    ):
        raise RuntimeError("Business module lifecycle migration layout is invalid")
    for statement in statements[:2]:
        cursor.execute(statement)
    validate_business_module_lifecycle_schema(cursor)
    cursor.execute(statements[2])
