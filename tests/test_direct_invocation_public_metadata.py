"""Public call metadata exposes phase and trigger, never raw execution inputs."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agent.automation_plugins.direct_invocation import public_invocation


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
