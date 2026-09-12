"""Both chat channels query current Invocation facts through one guarded reader."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.orchestration.models import OrchestrationError
from harness_composition import build_read_only_harness_gateway


@pytest.mark.parametrize("status,label", [("COMPLETED", "已完成"), ("WRITE_OUTCOME_UNKNOWN", "写入结果待核验")])
def test_current_invocation_is_projected_without_raw_payload_or_legacy_lookup(status, label):
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        lifecycle.get = Mock(return_value={"invocation_id":"invocation-1", "status":status,
            "plugin_id":"sync_arrive_list_v2", "plugin_version":"2.0.1",
            "result":{"meta":{"record_count":4}, "data":{"private_business_payload":"must-not-leak"}},
            "arguments":{"secret":"must-not-leak"}})
        repository = SimpleNamespace(list_work_items=Mock(), get_run=Mock(), get_evidence=Mock())
        runtime = SimpleNamespace(memory=SimpleNamespace(search_knowledge=Mock(), connection_factory=Mock()))
        gateway = build_read_only_harness_gateway(runtime, repository, invocations=lifecycle)
        result = await asyncio.to_thread(gateway.handlers()["runs.get_summary"], {"run_id":"invocation-1"})
        assert result["找到"] is True and result["状态"] == label
        assert result["插件"] == "sync_arrive_list_v2"
        assert result["结果摘要"] == "处理记录：4 条"
        assert "must-not-leak" not in str(result)
        repository.get_run.assert_not_called()
        assert lifecycle.active_read_count() == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("failure,legacy_called", [
    (OrchestrationError("INVOCATION_NOT_FOUND", "missing"), True),
    (RuntimeError("database unavailable"), False),
])
def test_history_is_queried_only_after_confirmed_current_absence(failure, legacy_called):
    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        lifecycle.get = Mock(side_effect=failure)
        repository = SimpleNamespace(list_work_items=Mock(), get_run=Mock(return_value=None), get_evidence=Mock())
        runtime = SimpleNamespace(memory=SimpleNamespace(search_knowledge=Mock(), connection_factory=Mock()))
        gateway = build_read_only_harness_gateway(runtime, repository, invocations=lifecycle)
        result = await asyncio.to_thread(gateway.handlers()["runs.get_summary"], {"run_id":"invocation-1"})
        assert bool(repository.get_run.call_count) is legacy_called
        assert result["可用"] is legacy_called
    asyncio.run(exercise())
