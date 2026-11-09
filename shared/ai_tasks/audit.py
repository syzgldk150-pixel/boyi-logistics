"""Persistence for ``ai_task_runs`` (migration 074) over a caller-supplied connection factory.

Never reads the environment or runs DDL. Each call is its own short
transaction; the caller decides whether a failed write blocks its result.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from shared.ai_tasks import TASK_OUTCOMES, TASK_STATUSES

ConnectionFactory = Callable[[], Any]
_OUTPUT_LIMIT = 20000


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


class AITaskAuditRepository:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        resource = self._connection_factory()
        managed = hasattr(resource, "__enter__") and hasattr(resource, "__exit__")
        connection = resource.__enter__() if managed else resource
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

    def record_run(self, run: Mapping[str, Any]) -> None:
        status = str(run["status"])
        if status not in TASK_STATUSES:
            raise ValueError("unknown AI task status")
        result = run.get("result")
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO ai_task_runs (run_id, task_id, prompt_version, subject_ref, status, provider, model,"
                " config_version_id, input_sha256, output_text, result_json, error_code, error_message, attempts,"
                " duration_ms, actor_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(run["run_id"]), str(run["task_id"]), str(run["prompt_version"]), _text(run["subject_ref"], 191),
                    status, _text(run.get("provider"), 32), _text(run.get("model"), 128), run.get("config_version_id"),
                    run.get("input_sha256"), _text(run.get("output_text"), _OUTPUT_LIMIT),
                    None if result is None else json.dumps(result, ensure_ascii=False, default=str),
                    _text(run.get("error_code"), 64), _text(run.get("error_message"), 500),
                    int(run.get("attempts") or 0), int(run.get("duration_ms") or 0), _text(run.get("actor_id"), 128),
                ),
            )

    def record_outcome(self, run_id: str, *, outcome: str, actor_id: str) -> bool:
        """Mark what a person did with the advice; returns False when the run is unknown or already decided."""
        if outcome not in TASK_OUTCOMES:
            raise ValueError("unknown AI task outcome")
        with self._cursor() as cursor:
            cursor.execute(
                "UPDATE ai_task_runs SET outcome=%s, outcome_actor_id=%s, outcome_at=CURRENT_TIMESTAMP(6)"
                " WHERE run_id=%s AND status='SUCCEEDED' AND outcome IS NULL",
                (outcome, str(actor_id)[:128], str(run_id)),
            )
            return bool(cursor.rowcount)


__all__ = ["AITaskAuditRepository"]
