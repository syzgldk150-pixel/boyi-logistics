"""Agent layer P3: back-office actions in conversation (problems, replies, receipts).

Accounts come from the Console customer-service settings, dates are closed
options resolved by code, results are rendered by code, and a reply may only
target a row of the person's own last query and quote the person's words —
always after "确认".
"""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.agent_actions.business_actions import BusinessActions, resolve_period
from agent.agent_actions.recorder import pre_write_audit
from agent.harness.catalog import HarnessToolCatalog
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage
from agent.harness_application import TrustedHarnessInvocationAdapter
from agent.harness_online import OnlineHarnessSidecar
from agent.llm_client import LLMClient
from agent.orchestration.models import Actor, ActorType
from shared.identity_permissions import IdentityAccess

ACTOR = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin", "super_admin"), authenticated_by="feishu_admin_binding")
ACCOUNTS = [{"platform": "yunda", "account_id": "yd-1", "name": "韵达客服"}]
ROWS = [{"external_id": f"P{index}", "waybill_no": f"39000000000{index}", "status": "待处理",
         "source_direction": "published_to_me", "content": f"客户催件 {index}"} for index in range(1, 4)]


class Platform:
    def __init__(self, rows=ROWS, fail=None):
        self.calls, self.rows, self.fail = [], rows, fail

    def __call__(self, operation, params, principal, request_id):
        self.calls.append((operation, dict(params), dict(principal), request_id))
        if self.fail:
            return {"ok": False, "error_code": "AUTH_REQUIRED", "error": self.fail}
        if operation == "customer-service-query":
            return {"ok": True, "data": {"rows": [dict(row) for row in self.rows], "total": len(self.rows)}}
        if operation == "customer-service-reply":
            return {"ok": True, "invocation_id": "00000000-0000-4000-8000-0000000000f1", "status": "COMPLETED", "data": {}}
        if operation == "receipts-query":
            return {"ok": True, "data": {"rows": [{"platform": "ronghui", "waybill_no": "R90000000001", "receipt_status": "已签回",
                                                   "audit_status": "待审核", "photo_count": 2}],
                                         "pagination": {"total": 1}}}
        raise AssertionError(operation)


def _actions(platform, accounts=ACCOUNTS, identity=None):
    return BusinessActions(invoke=platform, cs_accounts=lambda: accounts, identity_access=identity,
                           today=lambda: date(2026, 11, 15))


def test_periods_are_resolved_by_code():
    today = date(2026, 11, 15)
    assert resolve_period("today", today) == ("2026-11-15", "2026-11-15")
    assert resolve_period("yesterday", today) == ("2026-11-14", "2026-11-14")
    assert resolve_period("last_7_days", today) == ("2026-11-09", "2026-11-15")


def test_query_uses_configured_accounts_and_numbers_rows():
    platform = Platform()
    text, decision = _actions(platform).turn(ACTOR).execute(
        "biz_query_problems", {"period": "last_7_days"}, message="最近 7 天韵达的问题件")
    operation, params, principal, _ = platform.calls[0]
    assert operation == "customer-service-query"
    assert params == {"platform": "yunda", "account_id": "yd-1", "account_label": "韵达客服", "direction": "published_to_me",
                      "date_from": "2026-11-09", "date_to": "2026-11-15", "page": 1, "rows": 50}
    assert principal == {"actor_id": "ou-clerk", "roles": ["admin", "super_admin"]}
    assert "共 3 件" in text and "2. 韵达 390000000002 · 待处理" in text and decision.decision == "EXECUTE"


def test_query_without_configured_accounts_says_so():
    text, decision = _actions(Platform(), accounts=[]).turn(ACTOR).execute(
        "biz_query_problems", {"period": "today"}, message="今天的问题件")
    assert "还没有选" in text and decision.error_code == "CS_ACCOUNT_NOT_CONFIGURED"


def test_reply_needs_a_recent_query_the_persons_words_and_a_confirm():
    platform = Platform()
    actions = _actions(platform)
    text, decision = actions.turn(ACTOR).execute("biz_reply_problem", {"item": 2, "reply_text": "已安排明天派送"},
                                                message="回复第2条：已安排明天派送")
    assert "请先查询问题件" in text and decision.decision == "CLARIFY"
    actions.turn(ACTOR).execute("biz_query_problems", {"period": "today"}, message="今天的问题件")
    text, decision = actions.turn(ACTOR).execute("biz_reply_problem", {"item": 2, "reply_text": "已经派送完成"},
                                                message="回复第2条：已安排明天派送")
    assert decision.decision == "CLARIFY"  # invented text is never sent
    text, decision = actions.turn(ACTOR).execute("biz_reply_problem", {"item": 2, "reply_text": "已安排明天派送"},
                                                message="回复第2条：已安排明天派送")
    assert decision.decision == "CONFIRM" and "390000000002" in text and "回复“确认”" in text
    assert [call[0] for call in platform.calls] == ["customer-service-query"]  # nothing written yet
    other = Actor(ActorType.FEISHU_USER, "ou-other", ("admin",), authenticated_by="feishu_admin_binding")
    assert not actions.turn(other).has_pending()  # pending belongs to its own person
    reply, invocation = actions.turn(ACTOR, "req-2").confirm_pending()
    operation, params, _, request_id = platform.calls[-1]
    assert operation == "customer-service-reply" and invocation
    assert params == {"platform": "yunda", "account_id": "yd-1", "account_label": "韵达客服", "external_id": "P2",
                      "source_direction": "published_to_me", "waybill_no": "390000000002", "status": "待处理",
                      "reply_text": "已安排明天派送", "prob_status": "已处理", "old_prob_status": "待处理"}
    assert "已提交并核对" in reply
    assert actions.turn(ACTOR).confirm_pending()[0] == "没有等待确认的操作。"  # only once


def test_reply_cancel_and_unknown_outcome():
    platform = Platform(rows=ROWS[:1])
    actions = _actions(platform)
    actions.turn(ACTOR).execute("biz_query_problems", {"period": "today"}, message="今天的问题件")
    actions.turn(ACTOR).execute("biz_reply_problem", {"reply_text": "收到"}, message="回复：收到")  # one row: item optional
    assert "已取消" in actions.turn(ACTOR).cancel_pending()
    actions.turn(ACTOR).execute("biz_reply_problem", {"reply_text": "收到"}, message="回复：收到")
    platform_unknown = lambda *args: {"ok": False, "status": "WRITE_OUTCOME_UNKNOWN", "invocation_id": "i"}
    actions._invoke = platform_unknown
    assert "不要重复回复" in actions.turn(ACTOR).confirm_pending()[0]


def test_capability_is_checked_live():
    access = IdentityAccess(active=True, agent={"ai.chat": "direct", "business.query": "direct"}, channel="agent")
    identity = SimpleNamespace(for_actor=lambda actor: access)
    platform = Platform()
    text, decision = _actions(platform, identity=identity).turn(ACTOR).execute(
        "biz_query_problems", {"period": "today"}, message="今天的问题件")
    assert decision.decision == "REJECT" and not platform.calls
    text, decision = _actions(platform, identity=identity).turn(ACTOR).execute(
        "biz_query_receipts", {"period": "today"}, message="今天的回单")
    assert decision.decision == "EXECUTE" and "R90000000001" in text and "回单管理" in text


def test_receipt_query_failure_is_reported():
    text, decision = _actions(Platform(fail="登录失效")).turn(ACTOR).execute(
        "biz_query_receipts", {"period": "yesterday"}, message="昨天的回单")
    assert "回单查询失败：登录失效" in text and decision.decision == "REJECT"


# -- through the model loop ------------------------------------------------------------

def _sidecar(actions, reply, request_id=None):
    model = LLMClient()
    model.public_status = lambda: {"configured": True}
    async def chat(messages, tools=None, **_):
        assert any(tool["function"]["name"] == "biz_reply_problem" for tool in tools)
        return reply
    model.chat = chat
    adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(), actor=ACTOR, base_request_id=str(uuid4()), fixed_handlers={})
    return OnlineHarnessSidecar(catalog=HarnessToolCatalog(invocation_port=adapter), llm=model,
                                business_turn=actions.turn(ACTOR, request_id or str(uuid4())))


def _call(name, arguments):
    import json
    return {"tool_calls": [{"id": "c", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


def test_loop_renders_results_and_runs_the_reply_only_after_confirm():
    platform = Platform()
    actions = _actions(platform)
    first = _sidecar(actions, _call("biz_query_problems", {"period": "today"})).run(
        messages=(HarnessMessage("user", "今天的问题件", str(uuid4())),), timeout_seconds=5)
    assert "1. 韵达 390000000001" in first.content and first.decisions[0].decision == "EXECUTE"
    second = _sidecar(actions, _call("biz_reply_problem", {"item": 1, "reply_text": "马上处理"})).run(
        messages=(HarnessMessage("user", "回复第1条：马上处理", str(uuid4())),), timeout_seconds=5)
    assert "回复“确认”" in second.content and second.decisions[0].decision == "CONFIRM"
    written = []
    with pre_write_audit(lambda decisions: written.append(list(decisions))):
        third = _sidecar(actions, {"content": "unused"}).run(
            messages=(HarnessMessage("user", "确认", str(uuid4())),), timeout_seconds=5)
    assert written and written[0][-1].action_id == "cs.reply_problem"  # recorded before the write
    assert "已提交并核对" in third.content and platform.calls[-1][0] == "customer-service-reply"


def test_reply_is_not_sent_when_it_cannot_be_recorded():
    platform = Platform()
    actions = _actions(platform)
    actions.turn(ACTOR).execute("biz_query_problems", {"period": "today"}, message="今天的问题件")
    actions.turn(ACTOR).execute("biz_reply_problem", {"item": 1, "reply_text": "马上处理"}, message="回复第1条：马上处理")
    def broken(_decisions):
        raise RuntimeError("audit down")
    with pre_write_audit(broken), pytest.raises(HarnessError):
        _sidecar(actions, {"content": "unused"}).run(messages=(HarnessMessage("user", "确认", str(uuid4())),), timeout_seconds=5)
    assert platform.calls[-1][0] == "customer-service-query" and actions.turn(ACTOR).has_pending()
