"""Persistence for the business handbook (migration 085) over a caller-supplied connection factory.

Never reads the environment or runs DDL. Every state change is one short
transaction that locks the entry row first, so two administrators cannot
activate or edit the same entry concurrently.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from shared.agent_handbook import HandbookError

ConnectionFactory = Callable[[], Any]
_JSON_COLUMNS = ("compiled_json", "validation_json", "trial_json", "acknowledged_json")


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


class AgentHandbookRepository:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        resource = self._connection_factory()
        managed = hasattr(resource, "__enter__") and hasattr(resource, "__exit__")
        connection = resource.__enter__() if managed else resource
        begin = getattr(connection, "begin", None)
        if callable(begin):
            begin()
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()
            if managed:
                resource.__exit__(None, None, None)
            else:
                resource.close()

    # -- reads ------------------------------------------------------------------------

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        names = [column[0] for column in cursor.description or ()]
        return [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]

    @staticmethod
    def _entry(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"entry_id": row["entry_id"], "title": row["title"], "status": row["status"],
                "latest_version": int(row["latest_version"]),
                "active_version": None if row["active_version"] is None else int(row["active_version"]),
                "created_by": row["created_by"], "created_at": _iso(row["created_at"]),
                "updated_at": _iso(row["updated_at"]), "row_version": int(row["row_version"])}

    @staticmethod
    def _version(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"version": int(row["version"]), "body": row["body_text"], "status": row["status"],
                "compiled": _load(row["compiled_json"]), "validation": _load(row["validation_json"]),
                "trial": _load(row["trial_json"]), "acknowledged": _load(row["acknowledged_json"]) or [],
                "ai_run_id": row["ai_run_id"], "edited_by": row["edited_by"], "compiled_by": row["compiled_by"],
                "approved_by": row["approved_by"], "approved_at": _iso(row["approved_at"]),
                "updated_at": _iso(row["updated_at"])}

    def list_entries(self) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_handbook_entries WHERE status <> 'RETIRED' ORDER BY updated_at DESC LIMIT 500")
            return [self._entry(row) for row in self._rows(cursor)]

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_handbook_entries WHERE entry_id=%s", (entry_id,))
            rows = self._rows(cursor)
            if not rows:
                raise HandbookError("HANDBOOK_ENTRY_NOT_FOUND", "没有找到这条业务说明")
            entry = self._entry(rows[0])
            cursor.execute("SELECT * FROM agent_handbook_versions WHERE entry_id=%s ORDER BY version DESC LIMIT 20",
                           (entry_id,))
            entry["versions"] = [self._version(row) for row in self._rows(cursor)]
        return entry

    def active_compiled(self) -> list[tuple[str, int, dict[str, Any]]]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT e.entry_id, v.version, v.compiled_json FROM agent_handbook_entries e"
                " JOIN agent_handbook_versions v ON v.entry_id=e.entry_id AND v.version=e.active_version"
                " WHERE e.status='ACTIVE' AND v.status='ACTIVE' ORDER BY e.created_at, e.entry_id")
            return [(row["entry_id"], int(row["version"]), _load(row["compiled_json"]) or {})
                    for row in self._rows(cursor)]

    # -- writes -----------------------------------------------------------------------

    def _lock(self, cursor: Any, entry_id: str, expected_row_version: int | None = None) -> dict[str, Any]:
        cursor.execute("SELECT * FROM agent_handbook_entries WHERE entry_id=%s FOR UPDATE", (entry_id,))
        rows = self._rows(cursor)
        if not rows:
            raise HandbookError("HANDBOOK_ENTRY_NOT_FOUND", "没有找到这条业务说明")
        entry = self._entry(rows[0])
        if entry["status"] == "RETIRED":
            raise HandbookError("HANDBOOK_ENTRY_RETIRED", "这条业务说明已作废")
        if expected_row_version is not None and entry["row_version"] != expected_row_version:
            raise HandbookError("HANDBOOK_CONFLICT", "这条说明刚被别人修改过，请刷新后再操作")
        return entry

    def _latest(self, cursor: Any, entry_id: str, version: int) -> dict[str, Any]:
        cursor.execute("SELECT * FROM agent_handbook_versions WHERE entry_id=%s AND version=%s FOR UPDATE",
                       (entry_id, version))
        rows = self._rows(cursor)
        if not rows:
            raise HandbookError("HANDBOOK_VERSION_NOT_FOUND", "没有找到这个版本")
        return self._version(rows[0])

    def create_entry(self, *, title: str, body: str, actor_id: str) -> str:
        entry_id = str(uuid.uuid4())
        with self._cursor() as cursor:
            cursor.execute("INSERT INTO agent_handbook_entries (entry_id, title, status, latest_version, created_by)"
                           " VALUES (%s, %s, 'DRAFT', 1, %s)", (entry_id, title, actor_id))
            cursor.execute("INSERT INTO agent_handbook_versions (entry_id, version, body_text, status, edited_by)"
                           " VALUES (%s, 1, %s, 'DRAFT', %s)", (entry_id, body, actor_id))
        return entry_id

    def save_draft(self, entry_id: str, *, title: str, body: str, actor_id: str, expected_row_version: int) -> int:
        """Edit the note. An unapproved latest version is replaced; otherwise a new version starts.

        Editing an ACTIVE entry pauses it until the new version is approved.
        """
        with self._cursor() as cursor:
            entry = self._lock(cursor, entry_id, expected_row_version)
            latest = self._latest(cursor, entry_id, entry["latest_version"])
            if latest["status"] in {"DRAFT", "COMPILED"}:
                version = latest["version"]
                cursor.execute(
                    "UPDATE agent_handbook_versions SET body_text=%s, status='DRAFT', compiled_json=NULL,"
                    " validation_json=NULL, trial_json=NULL, acknowledged_json=NULL, ai_run_id=NULL,"
                    " edited_by=%s, compiled_by=NULL WHERE entry_id=%s AND version=%s",
                    (body, actor_id, entry_id, version))
            else:
                version = latest["version"] + 1
                cursor.execute("INSERT INTO agent_handbook_versions (entry_id, version, body_text, status, edited_by)"
                               " VALUES (%s, %s, %s, 'DRAFT', %s)", (entry_id, version, body, actor_id))
            status = "PAUSED" if entry["status"] == "ACTIVE" else entry["status"]
            cursor.execute("UPDATE agent_handbook_entries SET title=%s, latest_version=%s, status=%s,"
                           " row_version=row_version+1 WHERE entry_id=%s", (title, version, status, entry_id))
        return version

    def record_compile(self, entry_id: str, version: int, *, compiled: Mapping[str, Any] | None,
                       validation: Mapping[str, Any], run_id: str | None, actor_id: str) -> None:
        with self._cursor() as cursor:
            entry = self._lock(cursor, entry_id)
            latest = self._latest(cursor, entry_id, version)
            if version != entry["latest_version"] or latest["status"] not in {"DRAFT", "COMPILED"}:
                raise HandbookError("HANDBOOK_CONFLICT", "说明已经变化，请刷新后重新编译")
            cursor.execute(
                "UPDATE agent_handbook_versions SET status=%s, compiled_json=%s, validation_json=%s, trial_json=NULL,"
                " acknowledged_json=NULL, ai_run_id=%s, compiled_by=%s WHERE entry_id=%s AND version=%s",
                ("COMPILED" if validation.get("ok") else "DRAFT", _dump(compiled), _dump(validation), run_id,
                 actor_id, entry_id, version))
            cursor.execute("UPDATE agent_handbook_entries SET row_version=row_version+1 WHERE entry_id=%s", (entry_id,))

    def record_trial(self, entry_id: str, version: int, trial: Mapping[str, Any]) -> None:
        with self._cursor() as cursor:
            self._lock(cursor, entry_id)
            if self._latest(cursor, entry_id, version)["status"] != "COMPILED":
                raise HandbookError("HANDBOOK_CONFLICT", "请先编译这条说明")
            cursor.execute("UPDATE agent_handbook_versions SET trial_json=%s WHERE entry_id=%s AND version=%s",
                           (_dump(trial), entry_id, version))

    def acknowledge(self, entry_id: str, version: int, indices: list[int]) -> None:
        with self._cursor() as cursor:
            self._lock(cursor, entry_id)
            if self._latest(cursor, entry_id, version)["status"] != "COMPILED":
                raise HandbookError("HANDBOOK_CONFLICT", "请先编译这条说明")
            cursor.execute("UPDATE agent_handbook_versions SET acknowledged_json=%s WHERE entry_id=%s AND version=%s",
                           (_dump(indices), entry_id, version))
            cursor.execute("UPDATE agent_handbook_entries SET row_version=row_version+1 WHERE entry_id=%s", (entry_id,))

    def approve(self, entry_id: str, version: int, *, actor_id: str,
                blockers: Callable[[Mapping[str, Any], int], list[str]]) -> None:
        """Activate ``version``; ``blockers`` re-checks it inside the locked transaction."""
        with self._cursor() as cursor:
            entry = self._lock(cursor, entry_id)
            current = self._latest(cursor, entry_id, version)
            problems = blockers(current, entry["latest_version"])
            if problems:
                raise HandbookError("HANDBOOK_NOT_APPROVABLE", "；".join(problems))
            if entry["active_version"] is not None and entry["active_version"] != version:
                cursor.execute("UPDATE agent_handbook_versions SET status='SUPERSEDED' WHERE entry_id=%s AND version=%s",
                               (entry_id, entry["active_version"]))
            cursor.execute("UPDATE agent_handbook_versions SET status='ACTIVE', approved_by=%s,"
                           " approved_at=CURRENT_TIMESTAMP(6) WHERE entry_id=%s AND version=%s",
                           (actor_id, entry_id, version))
            cursor.execute("UPDATE agent_handbook_entries SET status='ACTIVE', active_version=%s,"
                           " row_version=row_version+1 WHERE entry_id=%s", (version, entry_id))

    def resume(self, entry_id: str, *, expected_row_version: int) -> None:
        """Re-enable a paused entry whose approved version is still its latest."""
        with self._cursor() as cursor:
            entry = self._lock(cursor, entry_id, expected_row_version)
            if (entry["status"] != "PAUSED" or entry["active_version"] is None
                    or entry["active_version"] != entry["latest_version"]):
                raise HandbookError("HANDBOOK_NOT_RESUMABLE", "说明改动过，需要重新编译并审核后才能生效")
            cursor.execute("UPDATE agent_handbook_entries SET status='ACTIVE', row_version=row_version+1 WHERE entry_id=%s",
                           (entry_id,))

    def set_status(self, entry_id: str, status: str, *, expected_row_version: int) -> None:
        if status not in {"PAUSED", "RETIRED"}:
            raise HandbookError("HANDBOOK_STATUS_INVALID", "只能暂停或作废")
        with self._cursor() as cursor:
            self._lock(cursor, entry_id, expected_row_version)
            cursor.execute("UPDATE agent_handbook_entries SET status=%s, row_version=row_version+1 WHERE entry_id=%s",
                           (status, entry_id))


__all__ = ["AgentHandbookRepository"]
