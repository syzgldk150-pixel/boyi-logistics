"""Pure shared repositories for runtime configuration tables.

Connection creation is deliberately injected by the caller.  This module does
not load environment variables, create directories, or import database drivers.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from shared.automation_project_manifest import automation_id_for_reviewed_task


ConnectionFactory = Callable[[], Any]

WAYBILL_FIELDS = (
    "waybill_no",
    "destination_site",
    "open_date",
    "receiver_address",
    "receiver_name",
    "receiver_phone",
    "sender_name",
    "sender_phone",
    "goods_name_lines",
    "package_type_lines",
    "quantity_lines",
    "weight_volume",
    "delivery_method",
    "freight_fee",
    "pickup_fee",
    "delivery_fee",
    "transfer_fee",
    "payment_method",
    "insurance_amount",
    "cod_amount",
    "remark",
    "scan_status",
    "status",
)
WAYBILL_STATUSES = frozenset({"pending", "in_transit", "signed", "cancelled"})


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
            SELECT id, automation_id, automation_generation, name, tool_name, tool_params,
                   cron_expression, enabled,
                   last_run, last_status, last_duration_ms, last_message,
                   configuration_version, updated_at, created_at
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
            for field in ("last_run", "updated_at", "created_at"):
                row[field] = _format_datetime(row.get(field))
        return rows

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    SELECT id, automation_id, automation_generation, name, tool_name,
                           tool_params, cron_expression, enabled,
                           last_run, last_status, last_duration_ms, last_message,
                           configuration_version, updated_at, created_at
                    FROM scheduled_tasks WHERE id=%s
                    """,
                    (task_id,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        row["tool_params"] = _decode_json(row.get("tool_params"), {})
        for field in ("last_run", "updated_at", "created_at"):
            row[field] = _format_datetime(row.get(field))
        return row

    def upsert_task(self, task: dict[str, Any]) -> None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                self._upsert_cursor(cursor, task)

    @staticmethod
    def _upsert_cursor(cursor: Any, task: dict[str, Any]) -> None:
        # MySQL evaluates single-table assignments from left to right.  Keep
        # the version predicate before overwriting configuration columns so a
        # material edit invalidates any exact-schedule policy atomically.
        changed = """(
            NOT (tool_name <=> VALUES(tool_name))
            OR NOT (tool_params <=> VALUES(tool_params))
            OR NOT (cron_expression <=> VALUES(cron_expression))
            OR NOT (enabled <=> VALUES(enabled))
            OR (
                VALUES(automation_id) IS NOT NULL
                AND NOT (automation_id <=> VALUES(automation_id))
            )
        )"""
        cursor.execute(
            f"""
            INSERT INTO scheduled_tasks
                (id, automation_id, name, tool_name, tool_params, cron_expression, enabled)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                configuration_version = configuration_version + IF({changed}, 1, 0),
                updated_at = IF({changed}, CURRENT_TIMESTAMP(6), updated_at),
                automation_id = COALESCE(VALUES(automation_id), automation_id),
                name = VALUES(name),
                tool_name = VALUES(tool_name),
                tool_params = VALUES(tool_params),
                cron_expression = VALUES(cron_expression),
                enabled = VALUES(enabled)
            """,
            (
                task["id"],
                task.get("automation_id")
                or automation_id_for_reviewed_task(str(task.get("id") or "")),
                task["name"],
                task["tool_name"],
                json.dumps(task.get("tool_params") or {}, ensure_ascii=False),
                task["cron_expression"],
                bool(task.get("enabled", False)),
            ),
        )

    def replace_tasks(self, tasks: list[dict[str, Any]], *, stale_task_ids: set[str]) -> None:
        """Atomically upsert a task group and delete its stale members."""
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                for task in tasks:
                    self._upsert_cursor(cursor, task)
                for task_id in sorted(stale_task_ids):
                    cursor.execute("DELETE FROM scheduled_tasks WHERE id=%s", (task_id,))

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
                    SET last_run=NOW(), last_status=%s, last_duration_ms=%s,
                        last_message=%s, updated_at=updated_at
                    WHERE id=%s
                    """,
                    (last_status, last_duration_ms, last_message, task_id),
                )

    def update_runtime_at(
        self,
        task_ids: list[str],
        *,
        last_run: str | None,
        last_status: str | None,
        last_duration_ms: int | None,
        last_message: str | None,
    ) -> None:
        if not task_ids:
            return
        placeholders = ", ".join("%s" for _ in task_ids)
        sql = (
            "UPDATE scheduled_tasks "
            "SET last_run=%s, last_status=%s, last_duration_ms=%s, last_message=%s, "
            "updated_at=updated_at "
            f"WHERE id IN ({placeholders})"
        )
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    sql,
                    [last_run, last_status, last_duration_ms, last_message, *task_ids],
                )


class WorkflowResourceRepository:
    """The single persistence implementation for ``workflow_resources``."""

    def __init__(self, connection_factory: ConnectionFactory, *, cursor_factory: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory

    def list_records(self, *, include_config: bool = True) -> list[dict[str, Any]]:
        columns = (
            "resource_key, config_json, source, configuration_version, "
            "config_sha256, "
            "SHA2(CAST(config_json AS CHAR CHARACTER SET utf8mb4), 256) "
            "AS computed_config_sha256, updated_at, created_at"
            if include_config
            else (
                "resource_key, source, configuration_version, config_sha256, "
                "updated_at, created_at"
            )
        )
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(f"SELECT {columns} FROM workflow_resources ORDER BY resource_key ASC")
                rows = list(cursor.fetchall() or [])
        for row in rows:
            if include_config:
                row["config"] = _decode_json(row.get("config_json"), {})
                self._validate_integrity(row)
            for field in ("updated_at", "created_at"):
                row[field] = _format_datetime(row.get(field))
        return rows

    def get_record(self, resource_key: str) -> dict[str, Any] | None:
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    SELECT resource_key, config_json, source,
                           configuration_version, config_sha256,
                           SHA2(CAST(config_json AS CHAR CHARACTER SET utf8mb4), 256)
                               AS computed_config_sha256,
                           updated_at, created_at
                    FROM workflow_resources WHERE resource_key=%s
                    """,
                    (resource_key,),
                )
                row = cursor.fetchone()
        if not row:
            return None
        row["config"] = _decode_json(row.get("config_json"), {})
        self._validate_integrity(row)
        for field in ("updated_at", "created_at"):
            row[field] = _format_datetime(row.get(field))
        return row

    def upsert(self, resource_key: str, config: dict[str, Any], *, source: str) -> None:
        encoded = json.dumps(
            config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    INSERT INTO workflow_resources (
                        resource_key, config_json, config_sha256, source,
                        configuration_version
                    )
                    VALUES (
                        %s,
                        CAST(%s AS JSON),
                        SHA2(
                            CAST(CAST(%s AS JSON) AS CHAR CHARACTER SET utf8mb4),
                            256
                        ),
                        %s,
                        1
                    )
                    ON DUPLICATE KEY UPDATE
                        configuration_version = configuration_version + IF(
                            NOT (config_json <=> VALUES(config_json))
                            OR NOT (source <=> VALUES(source)),
                            1,
                            0
                        ),
                        config_sha256 = SHA2(
                            CAST(VALUES(config_json) AS CHAR CHARACTER SET utf8mb4),
                            256
                        ),
                        config_json = VALUES(config_json),
                        source = VALUES(source)
                    """,
                    (resource_key, encoded, encoded, source),
                )

    @staticmethod
    def _validate_integrity(row: dict[str, Any]) -> None:
        version = row.get("configuration_version")
        persisted = str(row.get("config_sha256") or "").lower()
        computed = str(row.pop("computed_config_sha256", "") or "").lower()
        if (
            type(version) is not int
            or version <= 0
            or len(persisted) != 64
            or persisted != computed
        ):
            raise ValueError("workflow resource revision is invalid")


class WaybillRepository:
    """The single persistence implementation for deployment-managed ``waybills``.

    Callers normalize source-system payloads before invoking this repository.
    The repository deliberately contains no schema mutation: missing columns
    are a release error and must be fixed by a versioned SQL migration.
    """

    def __init__(self, connection_factory: ConnectionFactory, *, cursor_factory: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self._cursor_factory = cursor_factory

    @staticmethod
    def normalize_status(value: Any) -> str:
        text = str(value or "").strip().lower()
        aliases = {
            "待发货": "pending",
            "运输中": "in_transit",
            "运输途中": "in_transit",
            "未签收": "in_transit",
            "签收": "signed",
            "已签收": "signed",
            "已取消": "cancelled",
            "已作废": "cancelled",
            "作废": "cancelled",
            "取消": "cancelled",
        }
        return text if text in WAYBILL_STATUSES else aliases.get(str(value or "").strip(), "in_transit")

    def ensure_schema(self) -> None:
        required = {"id", "waybill_no", "insurance_amount", "cod_amount", "status", "scan_status"}
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    SELECT COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'waybills'
                    """
                )
                columns = {str(row.get("COLUMN_NAME") or "") for row in cursor.fetchall() or []}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(
                "waybills schema is not migrated; run deployment migrations first: " + ", ".join(missing)
            )

    @staticmethod
    def _normalized_record(record: dict[str, Any]) -> dict[str, str] | None:
        if not isinstance(record, dict):
            return None
        payload = {field: str(record.get(field, "") or "").strip() for field in WAYBILL_FIELDS}
        if not payload["waybill_no"]:
            return None
        payload["status"] = WaybillRepository.normalize_status(payload.get("status"))
        return payload

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any] | None:
        if not row:
            return None
        payload = dict(row)
        for field in ("created_at", "updated_at"):
            if hasattr(payload.get(field), "strftime"):
                payload[field] = payload[field].strftime("%Y-%m-%d %H:%M:%S")
        return payload

    def get_by_number(self, waybill_no: str, *, source: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM waybills WHERE waybill_no=%s"
        params: list[Any] = [str(waybill_no or "").strip()]
        if source:
            sql += " AND source=%s"
            params.append(str(source).strip())
        sql += " ORDER BY id DESC LIMIT 1"
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(sql, params)
                return self._row_to_dict(cursor.fetchone())

    def list_by_numbers(self, waybill_numbers: list[str]) -> list[dict[str, Any]]:
        """Read every row matching one bounded, binary-exact identity set."""

        if not isinstance(waybill_numbers, list):
            raise ValueError("waybill_numbers must be a list")
        identities = [str(value or "").strip() for value in waybill_numbers]
        if (
            not identities
            or len(identities) > 20_000
            or any(not identity for identity in identities)
            or len(identities) != len(set(identities))
        ):
            raise ValueError("waybill_numbers must be unique, non-empty, and bounded")
        placeholders = ", ".join("%s" for _ in identities)
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    f"""
                    SELECT * FROM waybills
                    WHERE BINARY waybill_no IN ({placeholders})
                    ORDER BY BINARY waybill_no, id
                    """,
                    identities,
                )
                rows = [
                    converted
                    for row in cursor.fetchall() or []
                    if (converted := self._row_to_dict(row)) is not None
                ]
        requested = set(identities)
        if any(str(row.get("waybill_no") or "").strip() not in requested for row in rows):
            raise RuntimeError("waybill exact-set query returned an extra identity")
        return rows

    def list_by_source_date(self, *, source: str, target_date: str) -> list[dict[str, Any]]:
        """Freshly read one exact source/date projection for postcondition checks."""

        source_text = str(source or "").strip()
        date_text = str(target_date or "").strip()
        if not source_text or not date_text:
            raise ValueError("source and target_date are required")
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute(
                    """
                    SELECT * FROM waybills
                    WHERE source=%s AND open_date=%s
                    ORDER BY waybill_no, id
                    """,
                    (source_text, date_text),
                )
                return [
                    converted
                    for row in cursor.fetchall() or []
                    if (converted := self._row_to_dict(row)) is not None
                ]

    def sync_records(
        self,
        records: list[dict[str, Any]],
        *,
        source: str,
        target_date: str = "",
        replace_date: bool = False,
        writer_id: str = "",
        validate_schema: bool = True,
    ) -> dict[str, Any]:
        source_text = str(source or "").strip()[:32] or "sync"
        date_text = str(target_date or "").strip()
        normalized_by_waybill: dict[str, dict[str, str]] = {}
        for record in records:
            normalized = self._normalized_record(record)
            if normalized:
                normalized_by_waybill.setdefault(normalized["waybill_no"], normalized)

        if validate_schema:
            self.ensure_schema()
        updates = creates = deleted_stale = 0
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                for row in normalized_by_waybill.values():
                    cursor.execute(
                        """
                        SELECT id FROM waybills WHERE waybill_no=%s
                        ORDER BY CASE WHEN source=%s THEN 0 ELSE 1 END, id ASC LIMIT 1
                        """,
                        (row["waybill_no"], source_text),
                    )
                    existing = cursor.fetchone()
                    if existing and existing.get("id"):
                        updatable = [field for field in WAYBILL_FIELDS if field != "status"]
                        assignments = ", ".join(f"{field} = %s" for field in updatable)
                        cursor.execute(
                            f"""
                            UPDATE waybills SET {assignments},
                                status = CASE WHEN status = 'cancelled' THEN status ELSE %s END,
                                writer_id = %s, source = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            [
                                *[row[field] for field in updatable],
                                row["status"],
                                str(writer_id or ""),
                                source_text,
                                existing["id"],
                            ],
                        )
                        updates += 1
                    else:
                        columns = ["document_id", *WAYBILL_FIELDS, "writer_id", "source", "created_at", "updated_at"]
                        value_columns = columns[:-2]
                        placeholders = ", ".join("%s" for _ in value_columns)
                        cursor.execute(
                            f"INSERT INTO waybills ({', '.join(columns)}) VALUES ({placeholders}, NOW(), NOW())",
                            [None, *[row[field] for field in WAYBILL_FIELDS], str(writer_id or ""), source_text],
                        )
                        creates += 1

                if replace_date and date_text:
                    keep = list(normalized_by_waybill)
                    if keep:
                        placeholders = ", ".join("%s" for _ in keep)
                        cursor.execute(
                            f"""
                            DELETE FROM waybills
                            WHERE source = %s AND open_date = %s AND status <> 'cancelled'
                              AND waybill_no NOT IN ({placeholders})
                            """,
                            [source_text, date_text, *keep],
                        )
                    else:
                        cursor.execute(
                            "DELETE FROM waybills WHERE source = %s AND open_date = %s AND status <> 'cancelled'",
                            (source_text, date_text),
                        )
                    deleted_stale = int(cursor.rowcount or 0)
        return {
            "ok": True,
            "source": source_text,
            "upserted": updates + creates,
            "updates": updates,
            "creates": creates,
            "deleted_stale": deleted_stale,
            "target_date": date_text,
        }

    def update_statuses(
        self,
        waybill_numbers: list[str],
        status: str,
        *,
        validate_schema: bool = True,
        mark_write_started: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        normalized_status = self.normalize_status(status)
        clean_numbers = list(dict.fromkeys(str(value or "").strip() for value in waybill_numbers if str(value or "").strip()))
        if not clean_numbers:
            return {"ok": True, "updated": 0, "status": normalized_status}
        if validate_schema:
            self.ensure_schema()
        placeholders = ", ".join("%s" for _ in clean_numbers)
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                if mark_write_started is not None:
                    mark_write_started()
                cursor.execute(
                    f"UPDATE waybills SET status = %s, updated_at = NOW() WHERE BINARY waybill_no IN ({placeholders}) AND status <> 'cancelled'",
                    [normalized_status, *clean_numbers],
                )
                updated = int(cursor.rowcount or 0)
        return {"ok": True, "updated": updated, "status": normalized_status}

    def delete_receipt_like(self, *, source: str = "ronghui", validate_schema: bool = True) -> dict[str, Any]:
        source_text = str(source or "").strip()[:32] or "ronghui"
        if validate_schema:
            self.ensure_schema()
        with _connection(self._connection_factory) as connection:
            with _cursor(connection, self._cursor_factory) as cursor:
                cursor.execute("DELETE FROM waybills WHERE source = %s AND UPPER(waybill_no) LIKE 'H%%'", (source_text,))
                deleted = int(cursor.rowcount or 0)
        return {"ok": True, "source": source_text, "deleted": deleted}
