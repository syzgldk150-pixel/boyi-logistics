"""Persistence for "需他人复核" review requests (migration 086).

Never reads the environment or runs DDL. Decisions are compare-and-set on the
row status, so a request is approved, rejected or expired exactly once and an
approved request is executed at most once.
"""
from __future__ import annotations

import json
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping

ConnectionFactory = Callable[[], Any]
STATUSES = ("PENDING", "APPROVED", "REJECTED", "EXPIRED", "EXECUTED", "FAILED")
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class ReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


class AgentReviewRepository:
    def __init__(self, connection_factory: ConnectionFactory, *, clock: Callable[[], datetime] = _utc_now) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory, self._clock = connection_factory, clock

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

    def _public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        expired = row["status"] == "PENDING" and row["expires_at"] <= self._clock()
        return {"request_id": row["request_id"], "short_code": row["short_code"], "kind": row["kind"],
                "status": "EXPIRED" if expired else row["status"], "title": row["title"], "summary": row["summary"],
                "capability": row["capability"], "plan": _load(row["plan_json"]), "requester": _load(row["requester_json"]),
                "requester_label": row["requester_label"], "reply_chat_id": row["reply_chat_id"],
                "expires_at": _iso(row["expires_at"]), "decided_by": row["decided_by"], "decided_at": _iso(row["decided_at"]),
                "decision_note": row["decision_note"], "result_text": row["result_text"],
                "invocation_id": row["invocation_id"], "created_at": _iso(row["created_at"])}

    def create(self, *, kind: str, title: str, summary: str, capability: str, plan: Mapping[str, Any],
               requester: Mapping[str, Any], requester_label: str, reply_chat_id: str | None,
               ttl_seconds: int) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        expires_at = self._clock() + timedelta(seconds=ttl_seconds)
        with self._cursor() as cursor:
            for _ in range(5):
                code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
                cursor.execute("SELECT 1 FROM agent_review_requests WHERE short_code=%s", (code,))
                if not cursor.fetchall():
                    break
            else:
                raise ReviewError("REVIEW_CODE_EXHAUSTED", "暂时无法生成复核编号，请稍后重试")
            cursor.execute(
                "INSERT INTO agent_review_requests (request_id, short_code, kind, status, title, summary, capability,"
                " plan_json, requester_json, requester_label, reply_chat_id, expires_at)"
                " VALUES (%s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, %s, %s, %s)",
                (request_id, code, kind, title[:191], summary[:1000], capability,
                 json.dumps(plan, ensure_ascii=False, default=str), json.dumps(requester, ensure_ascii=False),
                 requester_label[:191], reply_chat_id, expires_at))
        return self.get(request_id)

    def get(self, request_id: str) -> dict[str, Any]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_review_requests WHERE request_id=%s OR short_code=%s",
                           (request_id, str(request_id).upper()))
            rows = self._rows(cursor)
        if not rows:
            raise ReviewError("REVIEW_NOT_FOUND", "没有找到这条复核请求")
        return self._public(rows[0])

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_review_requests ORDER BY (status='PENDING') DESC, created_at DESC LIMIT %s",
                           (max(1, min(int(limit), 500)),))
            return [self._public(row) for row in self._rows(cursor)]

    def decide(self, request_id: str, *, approve: bool, actor_label: str, note: str = "") -> dict[str, Any]:
        """PENDING -> APPROVED/REJECTED once; an expired request becomes EXPIRED instead."""
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM agent_review_requests WHERE request_id=%s OR short_code=%s FOR UPDATE",
                           (request_id, str(request_id).upper()))
            rows = self._rows(cursor)
            if not rows:
                raise ReviewError("REVIEW_NOT_FOUND", "没有找到这条复核请求")
            row = rows[0]
            if row["status"] != "PENDING":
                raise ReviewError("REVIEW_ALREADY_DECIDED", "这条复核请求已经处理过")
            if row["expires_at"] <= self._clock():
                cursor.execute("UPDATE agent_review_requests SET status='EXPIRED' WHERE request_id=%s", (row["request_id"],))
                raise ReviewError("REVIEW_EXPIRED", "这条复核请求已过期作废，需要的话请发起人重新提出")
            cursor.execute("UPDATE agent_review_requests SET status=%s, decided_by=%s, decided_at=%s, decision_note=%s"
                           " WHERE request_id=%s AND status='PENDING'",
                           ("APPROVED" if approve else "REJECTED", actor_label[:191], self._clock(), note[:500] or None,
                            row["request_id"]))
        return self.get(row["request_id"])

    def finish(self, request_id: str, *, executed: bool, result_text: str, invocation_id: str | None = None) -> None:
        with self._cursor() as cursor:
            cursor.execute("UPDATE agent_review_requests SET status=%s, result_text=%s, invocation_id=%s"
                           " WHERE request_id=%s AND status='APPROVED'",
                           ("EXECUTED" if executed else "FAILED", result_text[:1000], invocation_id, request_id))

    def super_admin_open_ids(self) -> list[str]:
        """Feishu open_ids of active super administrators with notifications on."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT b.open_id FROM feishu_admin_bindings b JOIN persons p ON p.person_id=b.person_id"
                " JOIN admin_users u ON u.person_id=p.person_id WHERE b.active=TRUE AND b.notifications_enabled=TRUE"
                " AND p.is_active=TRUE AND u.is_active=TRUE AND u.control_plane_role='super_admin'")
            return [str(row["open_id"]) for row in self._rows(cursor)]


__all__ = ["AgentReviewRepository", "ReviewError", "STATUSES"]
