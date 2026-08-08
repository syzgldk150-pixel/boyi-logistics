"""Pure shared repositories for runtime configuration tables.

Connection creation is deliberately injected by the caller.  This module does
not load environment variables, create directories, or import database drivers.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator


ConnectionFactory = Callable[[], Any]


@contextmanager
def _connection(factory: ConnectionFactory) -> Iterator[Any]:
    resource = factory()
    if hasattr(resource, "__enter__"):
        with resource as connection:
            yield connection
        return

    try:
        yield resource
    finally:
        resource.close()


@contextmanager
def _cursor(connection: Any, cursor_factory: Any | None) -> Iterator[Any]:
    cursor = connection.cursor(cursor_factory) if cursor_factory is not None else connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def _decode_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _format_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


class ScheduledTaskRepository:
    """The single persistence implementation for ``scheduled_tasks``."""

    def __init__(self, connection_factory: ConnectionFactory, *, cursor_factory: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory

    def list_tasks(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = """
            SELECT id, name, tool_name, tool_params, cron_expression, enabled,
                   last_run, last_status, last_duration_ms, last_message, created_at
            FROM scheduled_tasks
        """
        if enabled_only:
            sql += " WHERE enabled=TRUE"
        sql += " ORDER BY name ASC, id ASC"
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(sql)
                rows = list(cursor.fetchall() or [])
        for row in rows:
            row["tool_params"] = _decode_json(row.get("tool_params"), {})
            for field in ("last_run", "created_at"):
                row[field] = _format_datetime(row.get(field))
        return rows

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    SELECT id, name, tool_name, tool_params, cron_expression, enabled,
                           last_run, last_status, last_duration_ms, last_message, created_at
                    FROM scheduled_tasks WHERE id=%s
                    """,
                    (task_id,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        row["tool_params"] = _decode_json(row.get("tool_params"), {})
        for field in ("last_run", "created_at"):
            row[field] = _format_datetime(row.get(field))
        return row

    def upsert_task(self, task: dict[str, Any]) -> None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    INSERT INTO scheduled_tasks
                        (id, name, tool_name, tool_params, cron_expression, enabled)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        tool_name = VALUES(tool_name),
                        tool_params = VALUES(tool_params),
                        cron_expression = VALUES(cron_expression),
                        enabled = VALUES(enabled)
                    """,
                    (
                        task["id"],
                        task["name"],
                        task["tool_name"],
                        json.dumps(task.get("tool_params") or {}, ensure_ascii=False),
                        task["cron_expression"],
                        bool(task.get("enabled", False)),
                    ),
                )

    def delete_task(self, task_id: str) -> None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute("DELETE FROM scheduled_tasks WHERE id=%s", (task_id,))

    def update_runtime(
        self,
        task_id: str,
        *,
        last_status: str | None,
        last_duration_ms: int | None,
        last_message: str | None,
    ) -> None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    UPDATE scheduled_tasks
                    SET last_run=NOW(), last_status=%s, last_duration_ms=%s, last_message=%s
                    WHERE id=%s
                    """,
                    (last_status, last_duration_ms, last_message, task_id),
                )


class WorkflowResourceRepository:
    """The single persistence implementation for ``workflow_resources``."""

    def __init__(self, connection_factory: ConnectionFactory, *, cursor_factory: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory

    def list_records(self, *, include_config: bool = True) -> list[dict[str, Any]]:
        columns = "resource_key, config_json, source, updated_at, created_at" if include_config else (
            "resource_key, source, updated_at, created_at"
        )
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(f"SELECT {columns} FROM workflow_resources ORDER BY resource_key ASC")
                rows = list(cursor.fetchall() or [])
        for row in rows:
            if include_config:
                row["config"] = _decode_json(row.get("config_json"), {})
            for field in ("updated_at", "created_at"):
                row[field] = _format_datetime(row.get(field))
        return rows

    def get_record(self, resource_key: str) -> dict[str, Any] | None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    SELECT resource_key, config_json, source, updated_at, created_at
                    FROM workflow_resources WHERE resource_key=%s
                    """,
                    (resource_key,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        row["config"] = _decode_json(row.get("config_json"), {})
        for field in ("updated_at", "created_at"):
            row[field] = _format_datetime(row.get(field))
        return row

    def upsert(self, resource_key: str, config: dict[str, Any], *, source: str) -> None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    INSERT INTO workflow_resources (resource_key, config_json, source)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        config_json = VALUES(config_json),
                        source = VALUES(source)
                    """,
                    (resource_key, json.dumps(config, ensure_ascii=False), source),
                )
