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


@pytest.mark.parametrize(
    "source_responses",
    (
        ({"ok": True}, {"success": True}),
        ({"success": True}, {"ok": True}),
    ),
)
def test_dual_clock_in_failure_is_not_retried(source_responses):
    auth, page, user, submit, sleep = _runtime_patches(source_responses)
    with (
        auth as auth_class,
        page,
        user,
        submit as submit_mock,
        sleep,
        pytest.raises(RuntimeError, match="dual clock-in failed"),
    ):
        clock_in_dual.submit_dual_clockin(
            delay_seconds=0,
            session_profile="default",
        )

    auth_class.assert_called_once_with(profile="default")
    assert submit_mock.call_count == 2


def test_dual_clock_in_preserves_both_explicit_source_confirmations():
    responses = [{"success": True, "message": "saved"}, {"success": True, "message": "saved"}]
    auth, page, user, submit, sleep = _runtime_patches(responses)
    with auth as auth_class, page, user, submit as submit_mock, sleep:
        result = clock_in_dual.submit_dual_clockin(
            delay_seconds=0,
            session_profile="daxiang_s",
        )

    auth_class.assert_called_once_with(profile="daxiang_s")
    assert submit_mock.call_count == 2
    assert result["first_success"] is True
    assert result["second_success"] is True
    assert result["first_response"] == responses[0]
    assert result["second_response"] == responses[1]


def test_run_api_requires_an_explicit_session_profile_without_fallback():
    with pytest.raises(
        ValueError,
        match="account_id must resolve to one explicit session_profile",
    ):
        clock_in_dual.run_api({})


@pytest.mark.parametrize("session_profile", ("default", "daxiang_s"))
def test_run_api_forwards_only_the_explicit_resolved_session_profile(session_profile):
    with patch.object(
        clock_in_dual,
        "submit_dual_clockin",
        return_value={"first_success": True, "second_success": True},
    ) as submit:
        result = clock_in_dual.run_api({"session_profile": session_profile})

    assert result == {"first_success": True, "second_success": True}
    assert submit.call_args.kwargs["session_profile"] == session_profile
