from __future__ import annotations

import pymysql
import pytest

from agent.memory import Memory


class _Cursor:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows
        self.executed = False

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _sql: str) -> None:
        self.executed = True

    def fetchall(self) -> list[object]:
        return list(self._rows)


class _Connection:
    def __init__(self, table_names: set[str]) -> None:
        self.table_names = table_names
        self.cursor_factories: list[object | None] = []
        self.closed = False

    def cursor(self, cursor_factory: object | None = None) -> _Cursor:
        self.cursor_factories.append(cursor_factory)
        if cursor_factory is pymysql.cursors.DictCursor:
            rows: list[object] = [
                {"TABLE_NAME": table_name} for table_name in sorted(self.table_names)
            ]
        else:
            rows = [(table_name,) for table_name in sorted(self.table_names)]
        return _Cursor(rows)

    def close(self) -> None:
        self.closed = True


def _memory_with_connection(connection: _Connection) -> Memory:
    memory = Memory()
    memory._conn = lambda: connection  # type: ignore[method-assign]
    return memory


def test_memory_schema_validation_requests_dict_cursor_and_closes_connection():
    connection = _Connection({"conversations", "messages", "tool_logs", "knowledge"})
    memory = _memory_with_connection(connection)

    memory._validate_migrated_tables()

    assert connection.cursor_factories == [pymysql.cursors.DictCursor]
    assert connection.closed is True


def test_memory_schema_validation_reports_missing_table_and_closes_connection():
    connection = _Connection({"conversations", "messages", "tool_logs"})
    memory = _memory_with_connection(connection)

    with pytest.raises(RuntimeError, match="knowledge"):
        memory._validate_migrated_tables()

    assert connection.cursor_factories == [pymysql.cursors.DictCursor]
    assert connection.closed is True
