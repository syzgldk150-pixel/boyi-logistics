"""Record natural-conversation turns off the reply path (agent layer P0).

The conversation service hands every finished turn to :class:`TurnRecorder`.
Read-only turns are written by one background worker so the cross-region RDS
round trip never delays a reply; a failed write is logged, never raised.
Writes that must be recorded *before* they run (P1 gateway) use
:meth:`TurnRecorder.record_now`, which raises on failure.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from shared.agent_action_audit import AgentActionAuditRepository, ConversationTurn

logger = logging.getLogger(__name__)


class TurnRecorder:
    def __init__(self, repository: AgentActionAuditRepository, *, identity_access: Any = None) -> None:
        if not isinstance(repository, AgentActionAuditRepository):
            raise TypeError("repository must be AgentActionAuditRepository")
        self._repository = repository
        self._identity_access = identity_access
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-turn-audit")

    def _with_person(self, turn: ConversationTurn, actor: Any) -> ConversationTurn:
        if turn.person_id or self._identity_access is None or actor is None:
            return turn
        try:
            person_id = self._identity_access.for_actor(actor).person_id
        except Exception:  # identity lookups fail closed elsewhere; the audit row keeps actor_id
            person_id = None
        return replace(turn, person_id=person_id) if person_id else turn

    def _write(self, turn: ConversationTurn, actor: Any) -> None:
        try:
            self._repository.record_turn(self._with_person(turn, actor))
        except Exception as exc:
            logger.warning("agent turn audit write failed | outcome=%s error_type=%s", turn.outcome, type(exc).__name__)

    def submit(self, turn: ConversationTurn, *, actor: Any = None) -> None:
        try:
            self._executor.submit(self._write, turn, actor)
        except RuntimeError:  # executor already shut down during stop
            logger.warning("agent turn audit skipped after shutdown")

    def record_now(self, turn: ConversationTurn, *, actor: Any = None) -> None:
        self._repository.record_turn(self._with_person(turn, actor))

    def close(self) -> None:
        self._executor.shutdown(wait=True)


__all__ = ["TurnRecorder"]
