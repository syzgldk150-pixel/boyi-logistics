"""Shared primitives for transactional orchestration repositories.

This module is configuration-free and never opens database connections by itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from typing import Any, Iterator

from shared.redaction import redact_sensitive, redact_text


class OrchestrationPersistenceError(RuntimeError):
    """Base class for control-plane persistence failures."""


class IdempotencyConflict(OrchestrationPersistenceError):
    """An idempotency identity was reused for different immutable input."""


class ConcurrentUpdateError(OrchestrationPersistenceError):
    """An optimistic state transition or leased update lost its race."""


class InvalidStateError(OrchestrationPersistenceError):
    """A persisted status or state transition is invalid."""


COMMAND_STATUSES = frozenset({"RECEIVED", "ACCEPTED", "REJECTED"})
WORK_ITEM_STATUSES = frozenset(
    {
        "OPEN",
        "IN_PROGRESS",
        "NEEDS_CLARIFICATION",
        "WAITING_APPROVAL",
        "BLOCKED_LOGIN",
        "BLOCKED_DATA",
        "RESOLVED",
        "CANCELLED",
    }
)
RUN_STATUSES = frozenset(
    {
        "RECEIVED",
        "CONTEXT_READY",
        "PLANNED",
        "VALIDATED",
        "WAITING_APPROVAL",
        "RUNNING",
        "VERIFYING",
        "COMPLETED",
        "NEEDS_CLARIFICATION",
        "BLOCKED_LOGIN",
        "BLOCKED_DATA",
        "PARTIAL",
        "FAILED_RETRYABLE",
        "FAILED_TERMINAL",
        "CANCELLED",
    }
)
STEP_STATUSES = frozenset(
    {
        "PENDING",
        "WAITING_APPROVAL",
        "RUNNING",
        "VERIFYING",
        "BLOCKED_LOGIN",
        "BLOCKED_DATA",
        "COMPLETED",
        "SKIPPED",
        "FAILED_RETRYABLE",
        "FAILED_TERMINAL",
        "CANCELLED",
    }
)
APPROVAL_STATUSES = frozenset({"PENDING", "APPROVED", "REJECTED", "EXPIRED", "INVALIDATED"})
OUTBOX_STATUSES = frozenset({"PENDING", "PROCESSING", "PUBLISHED", "DEAD_LETTER"})
OUTBOX_CANDIDATE_SCAN_LIMIT = 500
SCHEDULER_SUPERSESSION_BATCH_LIMIT = 100
TERMINAL_RUN_STATUSES = frozenset(
    {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}
)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _status(value: Any, allowed: frozenset[str], field: str, *, default: str | None = None) -> str:
    raw = getattr(value, "value", value)
    normalized = str(raw or default or "").strip().upper()
    if normalized not in allowed:
        raise InvalidStateError(f"unsupported {field}: {normalized or '<empty>'}")
    return normalized


def _safe_error(value: Any) -> str | None:
    text = redact_text(value).strip()
    return text[:500] or None


def _safe_comment(value: Any) -> str | None:
    text = redact_text(value).strip()
    return text[:1000] or None


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise OrchestrationPersistenceError("persisted orchestration JSON is invalid") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(
        redact_sensitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_param(value: Any, default: Any) -> str:
    return _canonical_json(default if value is None else value)


def _row_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = getattr(cursor, "description", None) or ()
    return {str(column[0]): value for column, value in zip(description, row)}


def _rows(cursor: Any) -> list[dict[str, Any]]:
    return [item for row in (cursor.fetchall() or []) if (item := _row_dict(cursor, row))]


def _decode_row(row: dict[str, Any] | None, json_fields: Iterable[str]) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    for field in json_fields:
        if field in payload:
            payload[field] = _json_value(payload.get(field), None)
    return payload


def _created_flag(row: dict[str, Any], created: bool) -> dict[str, Any]:
    payload = dict(row)
    payload["_created"] = created
    return payload


class RepositoryBase:
    def __init__(self, connection: Any, cursor_factory: Any | None = None) -> None:
        self.connection = connection
        self.cursor_factory = cursor_factory

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        cursor = (
            self.connection.cursor(self.cursor_factory)
            if self.cursor_factory is not None
            else self.connection.cursor()
        )
        try:
            yield cursor
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()
