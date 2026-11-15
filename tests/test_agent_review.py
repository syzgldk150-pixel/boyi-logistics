"""Agent layer P4: “需他人复核” — a super administrator approves each write first, once."""
import asyncio
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.agent_actions.business_actions import BusinessActions
from agent.agent_actions.gateway import ActionGateway
from agent.agent_actions.review import ReviewService, requester_actor, requester_record
from agent.agent_actions.spec import ActionSpec
from agent.orchestration import agent_confirmation
from agent.orchestration.agent_confirmation import (
    AGENT_REVIEW_REQUIRED, person_confirmed, require_person_confirmation, reviewer_approved,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.agent_review_repository import ReviewError
from shared.identity_permissions import IdentityAccess, validate_agent_permissions

CLERK = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin",), authenticated_by="feishu_admin_binding")


def _identity(**agent):
    access = IdentityAccess(active=True, person_id="p", agent=agent, channel="agent")
    return SimpleNamespace(for_actor=lambda actor: access, access=access)


def test_review_is_a_write_level_only():
    assert validate_agent_permissions({"plugins.execute": "review"}) == {"plugins.execute": "review"}
    with pytest.raises(ValueError, match="无需确认或复核"):
        validate_agent_permissions({"business.query": "review"})


def test_write_gate_requires_review_even_after_the_persons_own_confirm():
    identity = _identity(**{"plugins.execute": "review"})
    with pytest.raises(OrchestrationError) as raised, person_confirmed():
        require_person_confirmation(identity_access=identity, actor=CLERK, capability="plugins.execute", title="到货清单")
    assert raised.value.code == AGENT_REVIEW_REQUIRED and raised.value.capability == "plugins.execute"
    with reviewer_approved():
        require_person_confirmation(identity_access=identity, actor=CLERK, capability="plugins.execute", title="到货清单")
        assert agent_confirmation._CONFIRMED.get()


def test_gateway_routes_review_level_writes_to_review():
    gateway = ActionGateway(identity_access=_identity(**{"plugins.execute": "review"}))
    write = ActionSpec(action_id="plugin:list", title="到货清单", effect="internal_projection_write", capability="plugins.execute")
    preview = ActionSpec(action_id="plugin:scan", title="扫描", effect="external_write", capability="plugins.execute", preview=True)
    assert gateway.decide(write, actor=CLERK, arguments={}, message="同步一下到货清单").decision == "REVIEW"
    assert gateway.decide(preview, actor=CLERK, arguments={}, message="执行扫描").decision == "EXECUTE"


class MemoryReviews:
    def __init__(self):
        self.rows, self.now = {}, datetime(2026, 11, 16, 9)

    def create(self, **values):
        request_id = str(uuid4())
        self.rows[request_id] = {"request_id": request_id, "short_code": "ABC234", "status": "PENDING",
                                 "expires_at": self.now + timedelta(seconds=values.pop("ttl_seconds")), **values}
        return dict(self.rows[request_id])

    def decide(self, request_id, *, approve, actor_label, note=""):
        row = self.rows[request_id]
        if row["status"] != "PENDING":
            raise ReviewError("REVIEW_ALREADY_DECIDED", "这条复核请求已经处理过")
        if row["expires_at"] <= self.now:
            row["status"] = "EXPIRED"
            raise ReviewError("REVIEW_EXPIRED", "这条复核请求已过期作废")
        row["status"] = "APPROVED" if approve else "REJECTED"
        return dict(row)

    def finish(self, request_id, *, executed, result_text, invocation_id=None):
        self.rows[request_id].update(status="EXECUTED" if executed else "FAILED", result_text=result_text)

    def list_recent(self):
        return list(self.rows.values())


def _service(identity, executor):
    repo, notes, told = MemoryReviews(), [], []
    service = ReviewService(repo, identity_access=identity, executors={"plugin.console": executor},
                            notify_reviewers=notes.append, notify_requester=lambda text, request: told.append(text))
    return service, repo, notes, told


def test_approval_rechecks_requester_and_runs_exactly_once():
    runs = []
    async def executor(request, actor):
        runs.append((actor.actor_id, agent_confirmation._REVIEWED.get()))
        return True, "已发起执行。", "inv-1"
    identity = _identity(**{"plugins.execute": "review"})
    service, repo, notes, told = _service(identity, executor)
    request = service.submit(kind="plugin.console", actor=CLERK, capability="plugins.execute", title="到货清单",
                             summary="执行“到货清单”", plan={"automation_id": "a"})
    assert "同意复核 ABC234" in notes[0] and "ABC234" in service.submitted_message(request)
    result = asyncio.run(service.approve(request["request_id"], reviewer_label="超管"))
    assert result["status"] == "EXECUTED" and runs == [("ou-clerk", True)] and "已由 超管 同意" in told[-1]
    with pytest.raises(ReviewError, match="已经处理过"):
        asyncio.run(service.approve(request["request_id"], reviewer_label="超管"))
    assert len(runs) == 1


def test_changed_permission_or_expiry_never_runs():
    async def executor(request, actor):
        raise AssertionError("must not run")
    identity = _identity(**{"plugins.execute": "review"})
    service, repo, _, told = _service(identity, executor)
    request = service.submit(kind="plugin.console", actor=CLERK, capability="plugins.execute", title="t", summary="s", plan={})
    identity.for_actor = lambda actor: IdentityAccess(active=True, agent={}, channel="agent")
    assert asyncio.run(service.approve(request["request_id"], reviewer_label="超管"))["status"] == "FAILED"
    assert "权限已变化" in told[-1]
    expired = service.submit(kind="plugin.console", actor=CLERK, capability="plugins.execute", title="t", summary="s", plan={})
    repo.now += timedelta(hours=3)
    with pytest.raises(ReviewError, match="过期"):
        asyncio.run(service.approve(expired["request_id"], reviewer_label="超管"))


def test_reject_tells_the_requester():
    async def executor(request, actor):
        raise AssertionError("must not run")
    service, _, _, told = _service(_identity(**{"plugins.execute": "review"}), executor)
    request = service.submit(kind="plugin.console", actor=CLERK, capability="plugins.execute", title="t", summary="执行“到货清单”", plan={})
    asyncio.run(service.reject(request["request_id"], reviewer_label="超管", note="今天不同步"))
    assert "被 超管 拒绝：今天不同步" in told[-1]


def test_requester_record_round_trips():
    actor = requester_actor(requester_record(CLERK))
    assert (actor.actor_type, actor.actor_id, actor.roles, actor.channel) == (ActorType.FEISHU_USER, "ou-clerk", ("admin",), "agent")


def test_business_reply_from_review_level_person_files_a_request():
    submitted = []
    reviews = SimpleNamespace(submit=lambda **values: submitted.append(values) or {"short_code": "XYZ789", "summary": values["summary"]},
                              submitted_message=lambda request: f"已提交复核请求 {request['short_code']}")
    platform_calls = []
    def platform(operation, params, principal, request_id):
        platform_calls.append(operation)
        return {"ok": True, "data": {"rows": [{"external_id": "P1", "waybill_no": "390000000001", "status": "待处理"}]}}
    actions = BusinessActions(invoke=platform, cs_accounts=lambda: [{"platform": "yunda", "account_id": "yd", "name": "客服"}],
                              identity_access=_identity(**{"customer_service": "review"}), reviews=reviews)
    actions.turn(CLERK).execute("biz_query_problems", {"period": "today"}, message="今天的问题件")
    text, decision = actions.turn(CLERK).execute("biz_reply_problem", {"reply_text": "马上处理"}, message="回复：马上处理")
    assert decision.decision == "REVIEW" and "XYZ789" in text and platform_calls == ["customer-service-query"]
    assert submitted[0]["kind"] == "cs.reply" and submitted[0]["plan"]["arguments"]["reply_text"] == "马上处理"


def test_console_assistant_files_plugin_review_instead_of_running():
    from agent.harness.catalog import HarnessToolCatalog
    from agent.harness.models import HarnessMessage
    from agent.harness_application import TrustedHarnessInvocationAdapter
    from agent.harness_online import OnlineHarnessSidecar
    from agent.llm_client import LLMClient
    from agent.plugin_conversations import ConversationPluginTarget, PluginConversationService

    console = Actor(ActorType.CONSOLE_ADMIN, "17", ("admin",), authenticated_by="mysql_admin_session", channel="agent")
    target = ConversationPluginTarget(handle="run_plugin_list", automation_id="list-1", generation=1, configuration_version=1,
                                      contribution_id="run", title="到货清单", description="同步", effect="internal_projection_write")

    class Service(PluginConversationService):
        def targets(self, **_):
            return (target,)
    policy = SimpleNamespace(invoke_trusted=lambda *a, **k: pytest.fail("must not run"),
                             identity_access=_identity(**{"plugins.execute": "review", "ai.chat": "direct"}))
    plugins = Service(policy)
    filed = []
    reviews = SimpleNamespace(submit=lambda **values: filed.append(values) or {"short_code": "QWE456", "summary": values["summary"]},
                              submitted_message=lambda request: f"已提交复核请求 {request['short_code']}")
    model = LLMClient()
    model.public_status = lambda: {"configured": True}
    async def chat(messages, **_):
        return {"tool_calls": [{"id": "c", "function": {"name": "run_plugin_list", "arguments": "{}"}}]}
    model.chat = chat
    adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(), actor=console, base_request_id=str(uuid4()), fixed_handlers={})
    sidecar = OnlineHarnessSidecar(catalog=HarnessToolCatalog(invocation_port=adapter), llm=model,
                                   plugin_turn=plugins.turn(actor=console, source="console", request_id=str(uuid4())), reviews=reviews)
    result = sidecar.run(messages=(HarnessMessage("user", "同步一下到货清单", str(uuid4())),), timeout_seconds=5)
    assert "QWE456" in result.content and filed[0]["kind"] == "plugin.console"
    assert filed[0]["plan"]["automation_id"] == "list-1" and result.decisions[0].decision == "REVIEW"


def test_feishu_review_reply_needs_a_super_admin(monkeypatch):
    from feishu import message_handler, review_requests

    replies = []
    async def reply(chat_id, text, **kwargs):
        replies.append(text)
    monkeypatch.setattr(message_handler, "_reply_text", reply)
    approved = []
    class Service:
        async def approve(self, code, *, reviewer_label):
            approved.append((code, reviewer_label))
            return {"result_text": "已同意并执行"}
    monkeypatch.setattr(review_requests, "_REVIEW_SERVICE", Service())
    clerk = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin",), authenticated_by="feishu_admin_binding")
    boss = Actor(ActorType.FEISHU_USER, "ou-boss", ("admin", "super_admin"), display_name="老板", authenticated_by="feishu_admin_binding")
    for actor, expected in ((clerk, "只有超级管理员"), (boss, "已同意并执行")):
        monkeypatch.setattr(message_handler, "_FEISHU_BINDING_RUNTIME", SimpleNamespace(resolve_actor=lambda _id, a=actor: a))
        token = message_handler._COMMAND_CONTEXT.set(message_handler.FeishuCommandContext("e", actor.actor_id, "chat"))
        try:
            assert asyncio.run(review_requests.handle_review_reply("同意复核 abc234", chat_id="chat", sender_id=actor.actor_id))
        finally:
            message_handler._COMMAND_CONTEXT.reset(token)
        assert expected in replies[-1]
    assert approved == [("ABC234", "老板")]
    assert not asyncio.run(review_requests.handle_review_reply("你好", chat_id="chat", sender_id="x"))


# -- real migration 086 ------------------------------------------------------------------
from tests.mysql_migrated_database import database  # noqa: E402,F401


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
def test_migration_086_review_lifecycle(database):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    import pymysql

    from shared.agent_review_repository import AgentReviewRepository

    def connect():
        return pymysql.connect(host=database.host, port=database.port, user=database.user, password=database.password,
                               database=database.database, charset="utf8mb4", autocommit=False)

    repo = AgentReviewRepository(connect)
    request = repo.create(kind="plugin.console", title="到货清单", summary="执行“到货清单”", capability="plugins.execute",
                          plan={"automation_id": "a"}, requester=requester_record(CLERK), requester_label="仓库",
                          reply_chat_id=None, ttl_seconds=3600)
    assert repo.get(request["short_code"].lower())["request_id"] == request["request_id"]
    assert repo.decide(request["short_code"], approve=True, actor_label="超管")["status"] == "APPROVED"
    with pytest.raises(ReviewError):
        repo.decide(request["request_id"], approve=False, actor_label="超管")
    repo.finish(request["request_id"], executed=True, result_text="已执行", invocation_id=None)
    assert repo.get(request["request_id"])["status"] == "EXECUTED"
    stale = repo.create(kind="plugin.console", title="t", summary="s", capability="plugins.execute", plan={},
                        requester=requester_record(CLERK), requester_label="仓库", reply_chat_id="chat", ttl_seconds=1)
    repo._clock = lambda: datetime.utcnow() + timedelta(seconds=5)
    assert repo.get(stale["request_id"])["status"] == "EXPIRED"
    with pytest.raises(ReviewError, match="过期"):
        repo.decide(stale["request_id"], approve=True, actor_label="超管")
    assert repo.super_admin_open_ids() == []
    assert [item["request_id"] for item in repo.list_recent()][0] in {request["request_id"], stale["request_id"]}
