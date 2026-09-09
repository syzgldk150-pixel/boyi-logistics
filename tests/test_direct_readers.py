from __future__ import annotations

import asyncio
from unittest.mock import Mock
from types import SimpleNamespace
import threading

from agent.direct_readers import invoke_registered_reader
from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.execution_boundary import current_execution_capability
from agent.orchestration.models import Actor, ActorType
from agent.tool_registry import ToolRegistry
from shared.invocation_summary import invocation_count_summary
from harness_composition import build_read_only_harness_gateway


def invoke(handler, **overrides):
    values = dict(catalog=ToolRegistry(), name="track_waybill",
                  arguments={"tracking_number": "R00014513348"}, handler=handler,
                  actor=Actor(ActorType.CONSOLE_ADMIN, "test", roles=("admin",),
                              authenticated_by="mysql_admin_session"), source="console")
    values.update(overrides)
    return asyncio.run(invoke_registered_reader(**values))


def test_real_catalog_and_transient_capability_without_command():
    def query(arguments):
        assert current_execution_capability()
        return {"tracking_number": arguments["tracking_number"], "route_rows": []}

    result = invoke(query)
    assert result["success"] is True
    assert result["data"]["tracking_number"] == "R00014513348"
    assert not current_execution_capability()
    assert "run_id" not in result


def test_unbound_actor_and_invalid_fields_never_reach_reader():
    query = Mock(side_effect=AssertionError("must not execute"))
    assert invoke(query, actor=None)["error_code"] == "PERMISSION_DENIED"
    assert invoke(query, arguments={"tracking_number": "R00014513348", "account_override": "another"})["error_code"] == "INVALID_TOOL_ARGUMENTS"
    query.assert_not_called()


def test_llm_cannot_call_nonexposed_finance_reader():
    query = Mock(side_effect=AssertionError("must not execute"))
    assert invoke(query, name="query_business_finance", llm_selected=True)["error_code"] == "LLM_TOOL_NOT_ALLOWED"
    query.assert_not_called()


def test_empty_or_failed_read_cannot_be_success():
    assert invoke(lambda _: {})["error_code"] == "INVALID_QUERY_RESULT"
    assert invoke(lambda _: {"ok": False, "error": "source unavailable"})["success"] is False
    assert invoke(lambda _: {"status": "FAILED", "data": {}, "meta": {}, "warnings": [],
                             "error": {"code": "AUTH_REQUIRED", "message": "login required"}})["error_code"] == "AUTH_REQUIRED"


def test_summary_uses_only_explicit_current_integer_counts():
    assert invocation_count_summary({}) == ""
    assert invocation_count_summary({"result": {"meta": {"record_count": "19"}}}) == ""
    assert invocation_count_summary({"result": {"meta": {"record_count": True}}}) == ""
    assert invocation_count_summary({"result": {"meta": {"record_count": 19},
        "data": {"count_result": {"quantity_gaps": 3}}}}) == "处理记录：19 条；件数未齐单数：3"


def test_registered_agent_reader_obeys_release_hold_before_provider():
    lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: True)
    query = Mock(side_effect=AssertionError("must not execute"))
    assert invoke(query, invocations=lifecycle)["error_code"] == "PLUGIN_RELEASE_HELD"
    query.assert_not_called()
    assert lifecycle.active_read_count() == 0


def test_harness_fixed_reads_are_admitted_and_drained_without_stored_tasks():
    async def exercise():
        held = [True]
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: held[0])
        entered, finish = threading.Event(), threading.Event()
        def knowledge(query, limit):
            entered.set()
            assert finish.wait(timeout=3)
            return [{"category": "fixture", "content": query}]
        runtime = SimpleNamespace(memory=SimpleNamespace(search_knowledge=knowledge, connection_factory=lambda: None))
        repository = SimpleNamespace(list_work_items=Mock(), get_run=Mock(), get_evidence=Mock())
        gateway = build_read_only_harness_gateway(runtime, repository, invocations=lifecycle)
        reader = gateway.handlers()["knowledge.search"]
        try:
            await asyncio.to_thread(reader, {"query": "fixture", "limit": 1})
            raise AssertionError("release hold allowed a read")
        except Exception as exc:
            assert getattr(exc, "code", None) == "PLUGIN_RELEASE_HELD"
        assert not entered.is_set()
        held[0] = False
        task = asyncio.create_task(asyncio.to_thread(reader, {"query": "fixture", "limit": 1}))
        assert await asyncio.to_thread(entered.wait, 1)
        assert lifecycle.active_read_count() == 1
        assert lifecycle.active_invocations() == []
        finish.set()
        result = await task
        assert result["可用"] is True and result["结果"][0]["内容"] == "fixture"
        assert lifecycle.active_read_count() == 0
    asyncio.run(exercise())
