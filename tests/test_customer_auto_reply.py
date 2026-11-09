"""AI phase 4: authorized automatic replies — Host primitive, plugin action, service rules (no database, fake platform)."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.customer_auto_reply_handlers import (
    CANDIDATES_KEY, DRAFT_KEY, REPLY_KEY, CustomerAutoReplyPorts, build_customer_auto_reply_handler_map, existing_replies,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.customer_service_ai import CustomerServiceAIService
from shared.customer_service_ai import CustomerServiceAIError, CustomerServiceAIRepository

SID = "11111111-2222-4333-8444-555555555555"
KEY = ("src-1", "p-1", "received")
REPLY = "您好，该件 11-08 21:05 已到达大祥站，今天会安排派送，请保持电话畅通。"
PLAN = {"key": KEY, "suggestion_id": SID, "template_id": "t-1", "account_id": "acc-1", "platform": "yunda",
        "waybill_no": "YD1", "status": "待处理", "reply_text": REPLY, "scenario_id": "urge", "problem_type": "催件",
        "problem_text": "什么时候到", "evidence": []}


class FakeService:
    def __init__(self, plan=PLAN, plan_error=None):
        self.plan, self.plan_error = dict(plan), plan_error
        self.begun, self.finished = [], []

    async def auto_candidates(self, *, limit):
        return [{"source_id": "src-1", "external_id": "p-1", "source_direction": "received", "platform": "yunda",
                 "waybill_no": "YD1"}][:limit]

    async def auto_draft(self, *key):
        return {"suggestion_id": SID, "eligible": True, "reason": ""}

    async def auto_reply_plan(self, suggestion_id):
        if self.plan_error:
            raise self.plan_error
        assert suggestion_id == SID
        return dict(self.plan)

    async def auto_reply_begin(self, plan, *, status="started"):
        self.begun.append(status)

    async def auto_reply_finish(self, plan, status):
        self.finished.append(status)


class FakePlatform:
    def __init__(self, before="", after=REPLY, fail_reply=False):
        self.before, self.after, self.fail_reply = before, after, fail_reply
        self.calls = []

    def __call__(self, request):
        self.calls.append(request["action"])
        if request["action"] == "reply":
            if self.fail_reply:
                raise RuntimeError("connection reset")
            assert request["payload"]["reply_text"] == REPLY and request["item"]["external_id"] == "p-1"
            return {"ok": True, "result": {"code": 0}}
        reply = self.before if self.calls.count("reply") == 0 else self.after
        return {"ok": True, "details": [{"status": "待处理", "reply_text": reply}]}


def _handlers(service=None, platform=None):
    ports = CustomerAutoReplyPorts(
        describe_account=lambda account_id: {"account_id": account_id, "system": "yunda", "session_profile": "p"},
        customer_action=platform or FakePlatform(), service=lambda: service)
    return build_customer_auto_reply_handler_map(ports, cursor_secret=b"s" * 32)


def _context(accounts=("acc-1",), tool="customer_auto_reply", role="customer_service_source", marks=None):
    return CoreBrokerInvocationContext(automation_id="auto-1", plugin_version="1.0.0", tool_name=tool,
        operation=REPLY_KEY[0], action=REPLY_KEY[1], role=role, account_ids=accounts,
        mark_write_started=(lambda: marks.append(True)) if marks is not None else None)


def _call(handler, context, arguments):
    return asyncio.run(handler(context, arguments))


def test_reply_is_sent_once_and_confirmed_by_reading_the_platform_back():
    service, platform, marks = FakeService(), FakePlatform(), []
    result = _call(_handlers(service, platform)[REPLY_KEY], _context(marks=marks), {"suggestion_id": SID})
    assert result["confirmed"] is True and result["evidence_ref"].startswith("broker-evidence:customer-auto-reply:")
    assert platform.calls == ["detail", "reply", "detail"]
    assert service.begun == ["started"] and service.finished == ["confirmed"] and marks == [True]


def test_existing_platform_reply_is_recorded_and_nothing_is_written():
    service, platform = FakeService(), FakePlatform(before="网点已回复")
    result = _call(_handlers(service, platform)[REPLY_KEY], _context(), {"suggestion_id": SID})
    assert result["sent"] is False and result["skipped"] == "already_replied"
    assert platform.calls == ["detail"] and service.begun == ["skipped"]


@pytest.mark.parametrize("platform", [FakePlatform(after="暂无回复"), FakePlatform(fail_reply=True)])
def test_unconfirmed_reply_ends_as_unknown_and_is_never_retried(platform):
    service = FakeService()
    with pytest.raises(PluginExecutionError) as unknown:
        _call(_handlers(service, platform)[REPLY_KEY], _context(), {"suggestion_id": SID})
    assert unknown.value.code == "WRITE_OUTCOME_UNKNOWN" and service.finished == ["unknown"]
    assert platform.calls.count("reply") == 1


def test_reply_refuses_unauthorized_plans_foreign_accounts_and_wrong_context():
    with pytest.raises(PluginExecutionError) as refused:
        _call(_handlers(FakeService(plan_error=CustomerServiceAIError("AUTO_NOT_ALLOWED", "x")))[REPLY_KEY], _context(),
              {"suggestion_id": SID})
    assert refused.value.code == "AUTO_NOT_ALLOWED"
    platform = FakePlatform()
    with pytest.raises(PluginExecutionError) as foreign:
        _call(_handlers(FakeService(), platform)[REPLY_KEY], _context(accounts=("other",)), {"suggestion_id": SID})
    assert foreign.value.code == "BROKER_ACCOUNT_MISMATCH" and platform.calls == []
    with pytest.raises(PluginExecutionError):
        _handlers(FakeService())[REPLY_KEY](_context(tool="customer_problem"), {"suggestion_id": SID})
    with pytest.raises(PluginExecutionError):
        _handlers(FakeService())[REPLY_KEY](_context(), {"suggestion_id": SID, "reply_text": "plugin text"})
    with pytest.raises(PluginExecutionError) as unbound:
        _handlers(None)[REPLY_KEY](_context(), {"suggestion_id": SID})
    assert unbound.value.code == "BROKER_ACTION_UNAVAILABLE"


def test_candidates_and_draft_expose_only_identities_and_the_verdict():
    handlers = _handlers(FakeService())
    listed = _call(handlers[CANDIDATES_KEY], _context(), {"max_items": 3})
    assert set(listed["items"][0]) == {"source_id", "external_id", "source_direction", "platform", "waybill_no"}
    drafted = _call(handlers[DRAFT_KEY], _context(), {"source_id": "src-1", "external_id": "p-1", "source_direction": "received"})
    assert drafted["eligible"] is True and drafted["suggestion_id"] == SID
    with pytest.raises(PluginExecutionError):
        handlers[CANDIDATES_KEY](_context(), {"max_items": 99})


def test_existing_replies_ignores_placeholders():
    assert existing_replies({"details": [{"reply_text": "暂无回复"}, {"REVERSION": " "}]}) == []
    assert existing_replies({"rows": [{"REVERSION": "已安排"}]}) == ["已安排"]


# ---- the package's action, with a fake Host broker ----

def _load_action():
    root = Path(__file__).resolve().parents[1] / "agent/service_v2_plugins"
    if "boyi_plugin_result" not in sys.modules:
        result_spec = importlib.util.spec_from_file_location("boyi_plugin_result", root / "_shared/result.py")
        result_module = importlib.util.module_from_spec(result_spec)
        result_spec.loader.exec_module(result_module)
        sys.modules["boyi_plugin_result"] = result_module
    import shared.plugin_flow
    sys.modules.setdefault("plugin_flow", shared.plugin_flow)
    spec = importlib.util.spec_from_file_location("auto_reply_action", root / "customer_problem_auto_reply_v2/payload/action.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_action_replies_only_to_eligible_problems_and_stops_on_unknown():
    action = _load_action()
    calls = []
    verdicts = iter([{"eligible": True, "suggestion_id": SID}, {"eligible": False, "reason": "AI 把握不足"},
                     {"eligible": True, "suggestion_id": SID}])

    def broker(operation, *, action, role, arguments):
        calls.append(action)
        ref = {"evidence_ref": f"ref-{len(calls)}"}
        if action.endswith("candidates"):
            return {**ref, "items": [{"source_id": f"s{i}", "external_id": "e", "source_direction": "received"} for i in range(3)]}
        if action.endswith("draft"):
            return {**ref, **next(verdicts)}
        return {**ref, "confirmed": True, "skipped": "", "sent": True}

    result = action.run_action({"max_per_run": 3}, broker)
    assert result["status"] == "SUCCESS" and result["data"]["sent"] == 2 and result["data"]["not_eligible"] == 1
    assert calls.count("customer_auto_reply.reply") == 2

    def broker_unknown(operation, *, action, role, arguments):
        if action.endswith("candidates"):
            return {"evidence_ref": "r1", "items": [{"source_id": "s", "external_id": "e", "source_direction": "received"}]}
        if action.endswith("draft"):
            return {"evidence_ref": "r2", "eligible": True, "suggestion_id": SID}
        return {"evidence_ref": "r3", "confirmed": False, "skipped": ""}

    with pytest.raises(ValueError, match="WRITE_OUTCOME_UNKNOWN"):
        action.run_action({}, broker_unknown)
    with pytest.raises(ValueError):
        action.run_action({"max_per_run": 99}, broker)


# ---- service rules ----

class Repo:
    def __init__(self, stats=None, template=None, suggestion=None, problem=None):
        self.stats = stats or []
        self.template = template
        self.suggestion = suggestion
        self.problem = problem or {"platform": "yunda", "waybill_no": "YD1", "status": "待处理", "reply_text": "",
                                   "resolved": False, "problem_type": "催件", "problem_text": "t"}
        self.reviews = []

    def scenario_stats(self):
        return self.stats

    def get_template(self, template_id):
        return self.template

    def review_template(self, template_id, **kwargs):
        self.reviews.append(kwargs["action"])
        return {}

    def get_suggestion(self, suggestion_id):
        return self.suggestion

    def templates(self, statuses=()):
        return [self.template] if self.template else []

    def load_problem(self, key):
        return self.problem

    def source_account(self, source_id):
        return "acc-1"


STAT = {"scenario_id": "urge", "locked": False, "adopted": 19, "edited": 1, "adoption_rate": 0.95}
TEMPLATE = {"template_id": "t-1", "scenario_id": "urge", "status": "active", "auto_enabled": True,
            "reply_template": "您好，该件 {到站时间} 已到达 {网点}，今天会安排派送，请保持电话畅通。"}
SUGGESTION = {"suggestion_id": SID, "source_id": "src-1", "external_id": "p-1", "source_direction": "received",
              "scenario_id": "urge", "tier": "L3", "confidence": Decimal("0.90"), "reply_text": REPLY, "evidence_json": "[]"}


def _service(repo):
    return CustomerServiceAIService(repo, gateway=None)


@pytest.mark.parametrize(("stat", "blocked"), [
    (STAT, ""),
    ({**STAT, "adopted": 10, "edited": 1}, "至少使用 20 次"),
    ({**STAT, "adoption_rate": 0.9}, "95%"),
    ({**STAT, "locked": True}, "不能自动处理"),
])
def test_authorizing_needs_enough_unchanged_adoptions(stat, blocked):
    repo = Repo(stats=[stat], template=TEMPLATE)
    if blocked:
        with pytest.raises(CustomerServiceAIError) as refused:
            asyncio.run(_service(repo).review_template("t-1", action="authorize", actor_id="a"))
        assert blocked in refused.value.message and repo.reviews == []
    else:
        asyncio.run(_service(repo).review_template("t-1", action="authorize", actor_id="a"))
        assert repo.reviews == ["authorize"]


def test_plan_is_rebuilt_from_authorized_state_only():
    plan = asyncio.run(_service(Repo(template=TEMPLATE, suggestion=SUGGESTION)).auto_reply_plan(SID))
    assert plan["reply_text"] == REPLY and plan["account_id"] == "acc-1" and plan["key"] == KEY
    for repo in (
        Repo(template=TEMPLATE, suggestion={**SUGGESTION, "tier": "L2"}),
        Repo(template=TEMPLATE, suggestion={**SUGGESTION, "confidence": Decimal("0.80")}),
        Repo(template={**TEMPLATE, "auto_enabled": False}, suggestion=SUGGESTION),
        Repo(template=TEMPLATE, suggestion={**SUGGESTION, "reply_text": "您好，我们会尽快处理。"}),
        Repo(template=TEMPLATE, suggestion=SUGGESTION, problem={**Repo().problem, "reply_text": "已回复"}),
        Repo(template=TEMPLATE, suggestion=None),
    ):
        with pytest.raises(CustomerServiceAIError):
            asyncio.run(_service(repo).auto_reply_plan(SID))


# ---- repository ----

class _Cursor:
    def __init__(self, fail=None):
        self.executed, self.fail, self.description, self.rowcount = [], fail, [("template_id",)], 1

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if self.fail and sql.startswith("INSERT INTO cs_auto_replies"):
            raise self.fail

    def fetchall(self):
        return [("t-1",)]

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class DuplicateKey(Exception):
    def __init__(self):
        super().__init__(1062, "Duplicate entry")


def test_second_automatic_reply_for_a_problem_is_refused():
    repo = CustomerServiceAIRepository(lambda: _Connection(_Cursor(fail=DuplicateKey())))
    with pytest.raises(CustomerServiceAIError) as exists:
        repo.begin_auto_reply({"source_id": "s", "external_id": "e", "source_direction": "received", "suggestion_id": SID,
                               "template_id": "t", "platform": "yunda", "waybill_no": "Y", "reply_text": "r"})
    assert exists.value.code == "AUTO_REPLY_EXISTS"


def test_flagging_an_automatic_reply_revokes_its_template():
    cursor = _Cursor()
    result = CustomerServiceAIRepository(lambda: _Connection(cursor)).flag_auto_reply(KEY, actor_id="a", reason="说错了网点")
    assert result == {"template_id": "t-1", "auto_revoked": True}
    revoke_sql, params = cursor.executed[-1]
    assert "t.auto_enabled=0" in revoke_sql and "说错了网点" in params[1] and params[-1] == "t-1"


def test_primitive_results_satisfy_the_connector_contract():
    from agent.automation_plugins import connector_registry as registry_module
    from agent.automation_plugins.connector_registry import ConnectorHostInternalBindingRef, ConnectorRegistry
    from agent.automation_plugins.customer_connectors_v2 import build_customer_connectors

    handlers = _handlers(FakeService())
    registry = ConnectorRegistry(build_customer_connectors({**handlers, **{key: (lambda *a, **k: None) for key in (
        ("browser.invoke", "customer_problem.list_page"), ("browser.invoke", "customer_problem.detail"))}}))
    service = "connector.boyi.customer_auto_reply@1"
    binding = ConnectorHostInternalBindingRef(service=service)
    calls = {"candidates": (CANDIDATES_KEY, {"max_items": 2}),
             "draft": (DRAFT_KEY, {"source_id": "src-1", "external_id": "p-1", "source_direction": "received"}),
             "reply": (REPLY_KEY, {"suggestion_id": SID})}
    for operation, (key, arguments) in calls.items():
        resolved = registry.require_operation(service, operation)
        assert registry.prepare_invocation(resolved=resolved, binding=binding, arguments=arguments) == arguments
        output = {k: v for k, v in _call(handlers[key], _context(), arguments).items() if k != "evidence_ref"}
        registry_module._validate_schema_value(output, resolved.output_schema, subject="result")
    assert registry.require_operation(service, "reply").effect.value == "external_write"
