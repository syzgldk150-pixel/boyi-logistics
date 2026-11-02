"""Both chat channels query current Invocation facts through one guarded reader."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.orchestration.models import OrchestrationError
from harness_composition import build_read_only_harness_gateway


@pytest.mark.parametrize("status,label", [("COMPLETED", "已完成"), ("WRITE_OUTCOME_UNKNOWN", "写入结果待核验")])
def test_current_invocation_is_projected_without_raw_payload(status, label):
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        lifecycle.get = Mock(return_value={"invocation_id":"invocation-1", "status":status,
            "plugin_id":"sync_arrive_list_v2", "plugin_version":"2.0.1",
            "result":{"meta":{"record_count":4}, "data":{"private_business_payload":"must-not-leak"}},
            "arguments":{"secret":"must-not-leak"}})
        runtime = SimpleNamespace(memory=SimpleNamespace(search_knowledge=Mock(), connection_factory=Mock()))
        gateway = build_read_only_harness_gateway(runtime, invocations=lifecycle)
        result = await asyncio.to_thread(gateway.handlers()["runs.get_summary"], {"run_id":"invocation-1"})
        assert result["找到"] is True and result["状态"] == label
        assert result["插件"] == "sync_arrive_list_v2"
        assert result["结果摘要"] == "处理记录：4 条"
        assert "must-not-leak" not in str(result)
        assert lifecycle.active_read_count() == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("failure,available", [
    (OrchestrationError("INVOCATION_NOT_FOUND", "missing"), True),
    (RuntimeError("database unavailable"), False),
])
def test_absent_invocation_is_not_found_and_read_failure_is_unavailable(failure, available):
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        lifecycle.get = Mock(side_effect=failure)
        runtime = SimpleNamespace(memory=SimpleNamespace(search_knowledge=Mock(), connection_factory=Mock()))
        gateway = build_read_only_harness_gateway(runtime, invocations=lifecycle)
        result = await asyncio.to_thread(gateway.handlers()["runs.get_summary"], {"run_id":"invocation-1"})
        assert result["可用"] is available
        if available:
            assert result["找到"] is False
    asyncio.run(exercise())
