"""FinanceRepository: the ledger mixins over a caller-supplied connection factory.

The caller owns connection configuration; this module never reads the
environment, credentials or runs DDL. Every write is one transaction that
records an idempotent ``finance_events`` row keyed by the browser request id.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from shared.finance.db import fetchone, is_duplicate_key, json_text, request_sha256
from shared.finance.errors import FinanceError
from shared.finance.gl.accounts import AccountsMixin
from shared.finance.gl.balances import ReportsMixin
from shared.finance.gl.posting import PostingMixin
from shared.finance.gl.vouchers import VouchersMixin
from shared.finance.models import FinanceActor, parse_request_id
from shared.finance.settings import SettingsMixin

ConnectionFactory = Callable[[], Any]


class FinanceRepository(SettingsMixin, AccountsMixin, VouchersMixin, PostingMixin, ReportsMixin):
    """General-ledger persistence; the only code that writes ``finance_*`` tables."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        resource = self._connection_factory()
        if hasattr(resource, "__enter__") and hasattr(resource, "__exit__"):
            with resource as connection:
                yield from self._cursor_scope(connection)
        else:
            try:
                yield from self._cursor_scope(resource)
            finally:
                resource.close()

    @staticmethod
    def _cursor_scope(connection: Any) -> Iterator[Any]:
        cursor = connection.cursor()
        try:
            # Each statement sees the latest committed rows instead of a snapshot
            # taken at the transaction's first read; applies to this transaction only.
            cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            cursor.close()

    def _read(self, work: Callable[[Any], Any]) -> Any:
        with self._transaction() as cursor:
            return work(cursor)

    def _write(
        self,
        *,
        request_id: Any,
        action: str,
        target_type: str,
        target_id: Any,
        payload: Any,
        actor: FinanceActor,
        work: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run ``work`` once per request id; a repeated id replays the stored result."""

        if not isinstance(actor, FinanceActor):
            raise TypeError("actor must be a FinanceActor")
        request_id = parse_request_id(request_id)
        digest = request_sha256(action, f"{target_type}:{target_id}", payload)
        for attempt in (1, 2):
            try:
                with self._transaction() as cursor:
                    cursor.execute(
                        "SELECT action, request_sha256, result_json FROM finance_events WHERE request_id=%s",
                        (request_id,),
                    )
                    existing = fetchone(cursor)
                    if existing:
                        if existing["action"] != action or existing["request_sha256"] != digest:
                            raise FinanceError("REQUEST_ID_REUSED", "该请求标识已用于其他操作，请刷新页面后重试。")
                        stored = existing["result_json"]
                        return json.loads(stored) if isinstance(stored, (str, bytes)) else stored
                    result = work(cursor)
                    cursor.execute(
                        "INSERT INTO finance_events (request_id, request_sha256, action, target_type, target_id,"
                        " actor_id, actor_name, result_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (request_id, digest, action, target_type, str(result.get("id", target_id) or ""),
                         actor.actor_id[:128], actor.label, json_text(result)),
                    )
                    return result
            except FinanceError:
                raise
            except Exception as exc:
                # A concurrent twin committed first: the retry replays its result.
                if attempt == 1 and is_duplicate_key(exc):
                    continue
                raise
        raise AssertionError("unreachable")
