"""The real customer auto-reply ZIP against the production Connector and a fake platform (isolated subprocess)."""
import asyncio

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.customer_auto_reply_handlers import CustomerAutoReplyPorts, build_customer_auto_reply_handler_map
from agent.automation_plugins.customer_connectors_v2 import build_customer_connectors
from tests.service_v2_production_protocol_support import PackagedConnectorHost

SID = "11111111-2222-4333-8444-555555555555"
REPLY = "您好，该件已到达大祥站，今天会安排派送。"


class Service:
    def __init__(self):
        self.finished = []

    async def auto_candidates(self, *, limit):
        return [{"source_id": "src", "external_id": f"p{i}", "source_direction": "received", "platform": "yunda",
                 "waybill_no": f"YD{i}"} for i in range(2)]

    async def auto_draft(self, source_id, external_id, source_direction):
        eligible = external_id == "p0"
        return {"suggestion_id": SID if eligible else "", "eligible": eligible, "reason": "" if eligible else "没有已授权模板"}

    async def auto_reply_plan(self, suggestion_id):
        return {"key": ("src", "p0", "received"), "suggestion_id": SID, "template_id": "t", "account_id": "isolated-yd",
                "platform": "yunda", "waybill_no": "YD0", "status": "待处理", "reply_text": REPLY}

    async def auto_reply_begin(self, plan, *, status="started"):
        pass

    async def auto_reply_finish(self, plan, status):
        self.finished.append(status)


def test_zip_replies_only_to_the_eligible_problem_and_reads_it_back(tmp_path):
    replied = []

    def platform(request):
        if request["action"] == "reply":
            replied.append(request["payload"]["reply_text"])
            return {"ok": True, "result": {"code": 0}}
        return {"ok": True, "details": [{"reply_text": replied[-1] if replied else ""}]}

    service = Service()
    reviewed = build_customer_auto_reply_handler_map(CustomerAutoReplyPorts(
        describe_account=lambda account: {"account_id": account, "system": "yunda", "session_profile": "isolated"},
        customer_action=platform, service=lambda: service), cursor_secret=b"k" * 32)
    registry = ConnectorRegistry(build_customer_connectors({**reviewed, **{key: (lambda *a, **k: None) for key in (
        ("browser.invoke", "customer_problem.list_page"), ("browser.invoke", "customer_problem.detail"))}}))
    context = CoreBrokerInvocationContext(automation_id="isolated-auto", plugin_version="1.0.0",
        tool_name="customer_problem_auto_reply_v2", operation="service.invoke", action="run", role="__system__",
        account_bindings={"customer_service_source": ("isolated-yd",)})
    host = PackagedConnectorHost(tmp_path, "customer_problem_auto_reply_v2", registry, context)
    result = host.execute({"max_per_run": 2}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert result["data"]["sent"] == 1 and result["data"]["not_eligible"] == 1
    assert replied == [REPLY] and service.finished == ["confirmed"]
    assert "isolated-yd" not in str(result)
