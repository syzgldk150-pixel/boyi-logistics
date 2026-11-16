"""Persistence for 业务手册 → 知识来源 (migration 088) over a caller-supplied connection factory.

Never reads the environment or runs DDL. A scan replaces the document state and
adds its reminders in one transaction, so a failed scan leaves the last good state.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Sequence

from shared.knowledge_watch import Reminder

ConnectionFactory = Callable[[], Any]


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


class KnowledgeWatchRepository:
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

    @staticmethod
    def _rows(cursor: Any) -> list[dict[str, Any]]:
        names = [column[0] for column in cursor.description or ()]
        return [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]

    def known_documents(self) -> dict[str, dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT node_token, title, edit_time FROM agent_knowledge_documents WHERE present=1")
            return {row["node_token"]: row for row in self._rows(cursor)}

    def documents(self) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_knowledge_documents WHERE present=1 ORDER BY space_name, title LIMIT 500")
            return [{"node_token": row["node_token"], "space": row["space_name"], "title": row["title"],
                     "obj_type": row["obj_type"], "edit_time": row["edit_time"],
                     "first_seen_at": _iso(row["first_seen_at"]), "last_seen_at": _iso(row["last_seen_at"])}
                    for row in self._rows(cursor)]

    def record_scan(self, documents: Sequence[Mapping[str, Any]], reminders: Sequence[Reminder]) -> list[int]:
        """Store this scan's state and reminders together; documents no longer listed are marked absent."""
        with self._cursor() as cursor:
            cursor.execute("UPDATE agent_knowledge_documents SET present=0")
            for item in documents:
                cursor.execute(
                    "INSERT INTO agent_knowledge_documents (node_token, space_name, title, obj_type, edit_time)"
                    " VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE space_name=VALUES(space_name),"
                    " title=VALUES(title), obj_type=VALUES(obj_type), edit_time=VALUES(edit_time),"
                    " last_seen_at=CURRENT_TIMESTAMP(6), present=1",
                    (item["node_token"], item["space"], item["title"][:200], item["obj_type"][:32], item["edit_time"][:40]))
            ids = []
            for reminder in reminders:
                cursor.execute("INSERT INTO agent_knowledge_reminders (node_token, kind, title, detail) VALUES (%s, %s, %s, %s)",
                               (reminder.node_token, reminder.kind, reminder.title[:200], reminder.detail[:500]))
                ids.append(int(cursor.lastrowid))
            return ids

    def reminders(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_knowledge_reminders ORDER BY (status='OPEN') DESC, detected_at DESC LIMIT %s",
                           (max(1, min(int(limit), 500)),))
            return [{"reminder_id": int(row["reminder_id"]), "node_token": row["node_token"], "kind": row["kind"],
                     "title": row["title"], "detail": row["detail"], "status": row["status"],
                     "detected_at": _iso(row["detected_at"]), "checked_by": row["checked_by"],
                     "checked_at": _iso(row["checked_at"])} for row in self._rows(cursor)]

    def mark_checked(self, reminder_id: int, *, actor_id: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute("UPDATE agent_knowledge_reminders SET status='CHECKED', checked_by=%s, checked_at=UTC_TIMESTAMP(6)"
                           " WHERE reminder_id=%s AND status='OPEN'", (actor_id, int(reminder_id)))
            return cursor.rowcount == 1


__all__ = ["KnowledgeWatchRepository"]
