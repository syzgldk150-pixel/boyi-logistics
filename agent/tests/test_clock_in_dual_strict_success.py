from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.tms_runtime.scripts import clock_in_dual


def _runtime_patches(source_responses):
    return (
        patch.object(
            clock_in_dual,
            "TMSAuth",
            return_value=SimpleNamespace(login_and_get_session=lambda: object()),
        ),
        patch.object(
            clock_in_dual,
            "_resolve_clockin_page_context",
            return_value={"add_url": "https://example.invalid/add"},
        ),
        patch.object(clock_in_dual, "_load_user_info", return_value={}),
        patch.object(clock_in_dual, "submit_clockin", side_effect=source_responses),
        patch.object(clock_in_dual.time, "sleep", return_value=None),
    )


def test_dual_clock_in_requires_explicit_success_from_each_save_response():
    auth, page, user, submit, sleep = _runtime_patches([{"ok": True}, {"ok": True}])
    with auth, page, user, submit, sleep, pytest.raises(RuntimeError, match="dual clock-in failed"):
        clock_in_dual.submit_dual_clockin(delay_seconds=0)


def test_dual_clock_in_preserves_both_explicit_source_confirmations():
    responses = [{"success": True, "message": "saved"}, {"success": True, "message": "saved"}]
    auth, page, user, submit, sleep = _runtime_patches(responses)
    with auth, page, user, submit, sleep:
        result = clock_in_dual.submit_dual_clockin(delay_seconds=0)

    assert result["first_success"] is True
    assert result["second_success"] is True
    assert result["first_response"] == responses[0]
    assert result["second_response"] == responses[1]
