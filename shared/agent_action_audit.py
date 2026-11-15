"""Natural-conversation turns and action decisions (migration 084).

One row per Feishu / WeChat / Console AI-assistant turn and one row per action
the turn decided on. The user's words are stored only after shared redaction;
arguments are already minimized by the caller. Never reads the environment or
runs DDL; the caller supplies the connection factory and decides whether a
failed write blocks its work (reads: warn only; writes: P1 gateway refuses).
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

from shared.redaction import redact_text

ConnectionFactory = Callable[[], Any]

CHANNELS = frozenset({"console", "feishu", "wechat"})
CHAT_SCOPES = frozenset({"private", "group"})
# What the turn ended as. REPLY: model answered with no action; READ: one or
# more read actions ran; PLUGIN: a plugin was started or handed to a channel;
# CONFIRM: the person was asked to confirm; CLARIFY: the assistant asked back;
# FIXED: a deterministic text query answered without the model; REJECTED: an
# action was refused; ERROR: the turn failed.
OUTCOMES = frozenset({"REPLY", "READ", "PLUGIN", "CONFIRM", "CLARIFY", "FIXED", "REJECTED", "ERROR"})
DECISIONS = frozenset({"EXECUTE", "CONFIRM", "REVIEW", "CLARIFY", "REJECT"})

_PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_MESSAGE_LIMIT = 2000


def redact_utterance(text: Any) -> str:
    """Shared redaction plus phone and e-mail masking, bounded for storage."""
    value = redact_text(text)
    value = _PHONE.sub("[手机号已隐藏]", value)
    value = _EMAIL.sub("[邮箱已隐藏]", value)
    return value[:_MESSAGE_LIMIT]


def channel_of(actor_type: str, actor_id: str) -> str:
    """WeChat devices travel the Feishu chain as ``wechat:<device_id>``."""
    if actor_type == "console_admin":
        return "console"
    return "wechat" if str(actor_id).startswith("wechat:") else "feishu"


@dataclass(frozen=True)
class ActionDecision:
    action_id: str
    effect: str
    decision: str
    action_title: str = ""
    arguments: Mapping[str, Any] | None = None
    invocation_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError("unknown action decision")
        if not self.action_id or not self.effect:
            raise ValueError("action decision needs an action and effect")


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: str
    actor_id: str
    channel: str
    chat_scope: str
    message_text: str
    outcome: str
    person_id: str | None = None
    model: str | None = None
    tool_calls: int = 0
    duration_ms: int = 0
    error_code: str | None = None
    decisions: Sequence[ActionDecision] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS or self.chat_scope not in CHAT_SCOPES or self.outcome not in OUTCOMES:
            raise ValueError("conversation turn has an unknown channel, scope or outcome")


def _decision_id(turn_id: str, seq: int) -> str:
    from uuid import NAMESPACE_URL, uuid5
    return str(uuid5(NAMESPACE_URL, f"agent-decision:{turn_id}:{seq}"))


class AgentActionAuditRepository:
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
        if callable(begin):  # the shared factory may hand out autocommit connections
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

    def record_turn(self, turn: ConversationTurn) -> None:
        """Turn and its decisions in one short transaction; writing the same turn again updates it."""
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO agent_conversation_turns (turn_id, person_id, actor_id, channel, chat_scope,"
                " message_text, model, outcome, tool_calls, duration_ms, error_code)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                # A write is recorded before it runs and the finished turn again after (P1).
                " ON DUPLICATE KEY UPDATE person_id=COALESCE(VALUES(person_id), person_id), model=VALUES(model),"
                " outcome=VALUES(outcome), tool_calls=VALUES(tool_calls), duration_ms=VALUES(duration_ms),"
                " error_code=VALUES(error_code)",
                (
                    turn.turn_id, (turn.person_id or None) and str(turn.person_id)[:64], str(turn.actor_id)[:191],
                    turn.channel, turn.chat_scope, redact_utterance(turn.message_text),
                    (turn.model or None) and str(turn.model)[:128], turn.outcome,
                    max(0, min(int(turn.tool_calls), 65535)), max(0, int(turn.duration_ms)),
                    (turn.error_code or None) and str(turn.error_code)[:64],
                ),
            )
            if turn.decisions:
                cursor.executemany(
                    "INSERT INTO agent_action_decisions (decision_id, turn_id, seq, action_id, action_title,"
                    " effect, arguments_json, decision, invocation_id, error_code)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE action_id=VALUES(action_id), action_title=VALUES(action_title),"
                    " effect=VALUES(effect), arguments_json=VALUES(arguments_json), decision=VALUES(decision),"
                    " invocation_id=VALUES(invocation_id), error_code=VALUES(error_code)",
                    [
                        (
                            _decision_id(turn.turn_id, seq), turn.turn_id, seq, str(item.action_id)[:191],
                            redact_text(item.action_title)[:191] or None, str(item.effect)[:32],
                            None if item.arguments is None else json.dumps(
                                item.arguments, ensure_ascii=False, default=str, sort_keys=True),
                            item.decision, item.invocation_id, (item.error_code or None) and str(item.error_code)[:64],
                        )
                        for seq, item in enumerate(turn.decisions)
                    ],
                )

    def summary(self, *, since: str, until: str) -> dict[str, Any]:
        """Counts for the replay/evaluation report; ``since``/``until`` are UTC datetimes."""
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT outcome, COUNT(*), COALESCE(AVG(duration_ms), 0) FROM agent_conversation_turns"
                " WHERE created_at >= %s AND created_at < %s GROUP BY outcome",
                (since, until),
            )
            outcomes = {str(row[0]): {"turns": int(row[1]), "avg_ms": int(row[2])} for row in cursor.fetchall()}
            cursor.execute(
                "SELECT d.decision, d.effect, COUNT(*) FROM agent_action_decisions d"
                " WHERE d.created_at >= %s AND d.created_at < %s GROUP BY d.decision, d.effect",
                (since, until),
            )
            decisions = [{"decision": str(r[0]), "effect": str(r[1]), "count": int(r[2])} for r in cursor.fetchall()]
        return {"outcomes": outcomes, "decisions": decisions}


__all__ = [
    "CHANNELS", "CHAT_SCOPES", "DECISIONS", "OUTCOMES",
    "ActionDecision", "AgentActionAuditRepository", "ConversationTurn",
    "channel_of", "redact_utterance",
]
