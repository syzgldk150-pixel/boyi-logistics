"""Agent layer P0: every natural-conversation turn and action decision is recorded.

Recording never changes the reply; the user's words are stored redacted.
"""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.agent_actions.recorder import TurnRecorder
from agent.channel_chat import reply_to_feishu
from agent.core import AgentCore
from agent.orchestration.models import Actor, ActorType
from shared.agent_action_audit import (
    ActionDecision, AgentActionAuditRepository, ConversationTurn, channel_of, redact_utterance,
)
from shared.identity_permissions import chat_scope
from tests.chat_runtime_support import configure_chat

CONSOLE = Actor(ActorType.CONSOLE_ADMIN, "17", ("admin",), authenticated_by="mysql_admin_session")
FEISHU = Actor(ActorType.FEISHU_USER, "ou-isolated", ("admin",), authenticated_by="feishu_admin_binding")
WECHAT = Actor(ActorType.FEISHU_USER, "wechat:device-1", ("admin",), authenticated_by="feishu_admin_binding")


class CollectingRecorder:
    def __init__(self, fail=False):
        self.turns, self.fail = [], fail

    def submit(self, turn, *, actor=None):
        if self.fail:
            raise RuntimeError("audit down")
        self.turns.append((turn, actor))


def _core(model):
    core = AgentCore()
    core.llm = SimpleNamespace(chat=model)
    return core


def test_plain_reply_is_recorded_per_channel_and_scope():
    async def model(messages, **_):
        return {"content": "你好，请告诉我你的业务问题。"}
    recorder = CollectingRecorder()
    core = _core(model)
    service, _ = configure_chat(core, turn_recorder=recorder)
    with chat_scope("private"):
        asyncio.run(core.handle_message("你好", actor=CONSOLE, source="console"))
    with chat_scope("group"):
        reply_to_feishu(service, actor=FEISHU, chat_id="group", event_id="e1", message="你好")
    reply_to_feishu(service, actor=WECHAT, chat_id="wx", event_id="e2", message="你好")
    turns = [turn for turn, _ in recorder.turns]
    assert [t.channel for t in turns] == ["console", "feishu", "wechat"]
    assert [t.chat_scope for t in turns] == ["private", "group", "group"]  # unknown counts as group
    assert {t.outcome for t in turns} == {"REPLY"}
    assert all(not t.decisions and t.error_code is None for t in turns)
    assert len({t.turn_id for t in turns}) == 3


def test_read_tool_records_execute_decision_with_minimized_arguments():
    replies = iter([
        {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "query_waybill", "arguments": json.dumps({"waybill_number": "R00014513348"})}}]},
        {"content": "这票运单已签收。"},
    ])
    async def model(messages, **_):
        return next(replies)
    recorder = CollectingRecorder()
    core = _core(model)
    handler = Mock(return_value={"waybill_number": "R00014513348", "status": "已签收"})
    configure_chat(core, handlers={"waybill.lookup": handler}, include_fixed=True, turn_recorder=recorder)
    result = asyncio.run(core.handle_message("这票运单什么状态", actor=CONSOLE, source="console"))
    assert result["reply"] == "这票运单已签收。"
    (turn, actor), = recorder.turns
    assert actor.actor_id == "17"
    assert turn.outcome == "READ" and turn.tool_calls == 1
    decision, = turn.decisions
    assert (decision.action_id, decision.effect, decision.decision) == ("waybill.lookup", "read", "EXECUTE")
    assert decision.arguments == {"waybill_number": "R00014513348"}


def test_fixed_text_query_is_recorded_without_model():
    reader = Mock(return_value={"tracking_number": "R00014513348", "route_rows": []})
    core = AgentCore(direct_tool_runners={"track_waybill": reader})
    core.llm = SimpleNamespace(chat=Mock(side_effect=AssertionError("exact reads bypass LLM")))
    recorder = CollectingRecorder()
    configure_chat(core, turn_recorder=recorder)
    asyncio.run(core.handle_message("R00014513348", actor=CONSOLE, source="console"))
    (turn, _), = recorder.turns
    assert turn.outcome == "FIXED" and turn.model is None


def test_failed_turn_is_recorded_with_error_code():
    async def model(messages, **_):
        raise RuntimeError("provider down")
    recorder = CollectingRecorder()
    core = _core(model)
    configure_chat(core, turn_recorder=recorder)
    with pytest.raises(Exception):
        asyncio.run(core.handle_message("你好", actor=CONSOLE, source="console"))
    (turn, _), = recorder.turns
    assert turn.outcome == "ERROR" and turn.error_code == "HARNESS_MODEL_UNAVAILABLE"


def test_audit_failure_never_changes_the_reply():
    async def model(messages, **_):
        return {"content": "收到。"}
    core = _core(model)
    configure_chat(core, turn_recorder=CollectingRecorder(fail=True))
    assert asyncio.run(core.handle_message("你好", actor=CONSOLE, source="console"))["reply"] == "收到。"


def test_channel_and_redaction_rules():
    assert channel_of("console_admin", "17") == "console"
    assert channel_of("feishu_user", "ou-1") == "feishu"
    assert channel_of("feishu_user", "wechat:d") == "wechat"
    text = redact_utterance("帮我联系13800000000，邮箱 someone@example.com")
    assert "13800000000" not in text and "someone@example.com" not in text
    assert len(redact_utterance("字" * 5000)) == 2000
    with pytest.raises(ValueError):
        ConversationTurn(turn_id="t", actor_id="a", channel="sms", chat_scope="private", message_text="x", outcome="REPLY")
    with pytest.raises(ValueError):
        ActionDecision(action_id="a", effect="read", decision="MAYBE")


class FakeCursor:
    def __init__(self, log):
        self.log, self.rowcount = log, 1

    def execute(self, sql, params):
        self.log.append(("execute", sql, params))

    def executemany(self, sql, rows):
        self.log.append(("executemany", sql, rows))

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, log, fail=False):
        self.log, self.fail, self.committed, self.rolled_back = log, fail, False, False

    def cursor(self):
        if self.fail:
            raise RuntimeError("db down")
        return FakeCursor(self.log)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _turn(**extra):
    values = dict(turn_id="00000000-0000-0000-0000-000000000001", actor_id="17", channel="console",
                  chat_scope="private", message_text="电话13800000000 报价", outcome="READ",
                  decisions=(ActionDecision(action_id="waybill.lookup", effect="read", decision="EXECUTE",
                                            arguments={"waybill_number": "R1"}),))
    values.update(extra)
    return ConversationTurn(**values)


def test_repository_writes_turn_and_decisions_in_one_transaction():
    log = []
    connection = FakeConnection(log)
    AgentActionAuditRepository(lambda: connection).record_turn(_turn())
    (kind1, sql1, params), (kind2, sql2, rows) = log
    assert kind1 == "execute" and "agent_conversation_turns" in sql1
    assert "13800000000" not in params[5]
    assert kind2 == "executemany" and "agent_action_decisions" in sql2
    assert rows[0][2] == 0 and json.loads(rows[0][6]) == {"waybill_number": "R1"}
    assert connection.committed


def test_recorder_logs_background_failure_and_record_now_raises(caplog):
    log = []
    recorder = TurnRecorder(AgentActionAuditRepository(lambda: FakeConnection(log, fail=True)))
    with caplog.at_level(logging.WARNING):
        recorder.submit(_turn())
        recorder.close()
    assert "agent turn audit write failed" in caplog.text
    with pytest.raises(RuntimeError):
        TurnRecorder(AgentActionAuditRepository(lambda: FakeConnection(log, fail=True))).record_now(_turn())


def test_recorder_fills_person_from_live_identity():
    log = []
    identity = SimpleNamespace(for_actor=lambda actor: SimpleNamespace(person_id="p-9"))
    recorder = TurnRecorder(AgentActionAuditRepository(lambda: FakeConnection(log)), identity_access=identity)
    recorder.record_now(_turn(), actor=CONSOLE)
    assert log[0][2][1] == "p-9"


# --- Real migration 084 on the isolated loopback MySQL ---------------------------------------
import os  # noqa: E402

from tests.mysql_migrated_database import database  # noqa: E402,F401


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
def test_migration_084_stores_turns_and_decisions(database):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    import pymysql

    def connect():
        return pymysql.connect(host=database.host, port=database.port, user=database.user,
                               password=database.password, database=database.database, charset="utf8mb4",
                               autocommit=False)

    repository = AgentActionAuditRepository(connect)
    repository.record_turn(_turn())
    with pytest.raises(pymysql.err.IntegrityError):  # the same turn is never written twice
        repository.record_turn(_turn())
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT channel, outcome, message_text FROM agent_conversation_turns")
        (channel, outcome, text), = cursor.fetchall()
        cursor.execute("SELECT seq, action_id, decision, JSON_EXTRACT(arguments_json, '$.waybill_number')"
                       " FROM agent_action_decisions")
        decisions = cursor.fetchall()
    assert (channel, outcome) == ("console", "READ") and "13800000000" not in text
    assert decisions == ((0, "waybill.lookup", "EXECUTE", '"R1"'),)
    summary = repository.summary(since="2000-01-01 00:00:00", until="2100-01-01 00:00:00")
    assert summary["outcomes"]["READ"]["turns"] == 1
    assert summary["decisions"] == [{"decision": "EXECUTE", "effect": "read", "count": 1}]
