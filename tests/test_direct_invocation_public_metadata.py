"""Public call metadata exposes phase and trigger, never raw execution inputs."""

from datetime import datetime, timezone
from uuid import uuid4
from types import SimpleNamespace

import pytest

from agent.automation_plugins.direct_invocation import DirectPluginInvocationService, public_invocation


@pytest.mark.parametrize("arguments,preview_id,expected", [
    ({"dry_run": True}, None, "preview"), ({"dry_run": False}, str(uuid4()), "formal"), ({}, None, "run"),
])
def test_public_phase_comes_from_actual_persisted_call(arguments, preview_id, expected):
    row = {"invocation_id": str(uuid4()), "source": "scheduler", "status": "RUNNING",
           "arguments_json": {**arguments, "private_field": "private"}, "preview_invocation_id": preview_id,
           "started_at": datetime.now(timezone.utc)}
    projection = public_invocation(row)
    assert projection["invocation_phase"] == expected
    assert projection["source"] == "scheduler"
    assert "private_field" not in str(projection)
    assert "arguments_json" not in projection and "preview_invocation_id" not in projection


def test_history_omits_large_business_results_but_keeps_execution_identity():
    row = {"invocation_id": str(uuid4()), "automation_id": "daily-sign", "source": "console",
           "status": "COMPLETED", "started_at": datetime.now(timezone.utc),
           "result_json": {"data": {"rows": ["historical output"] * 10000}}}
    service = object.__new__(DirectPluginInvocationService)
    service.repository = SimpleNamespace(list_recent=lambda identity, limit: [row])
    service._public = public_invocation
    history = service.list_recent("daily-sign")
    assert history[0]["invocation_id"] == row["invocation_id"]
    assert history[0]["status"] == "COMPLETED" and history[0]["invocation_phase"] == "run"
    assert "result" not in history[0] and "output" not in history[0]
    assert public_invocation(row)["result"] == row["result_json"]
