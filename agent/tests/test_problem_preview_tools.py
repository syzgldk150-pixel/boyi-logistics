from unittest.mock import patch

from tools.preview_self_pickup_problems_tool import preview_self_pickup_problems
from tools.preview_split_pending_problems_tool import preview_split_pending_problems
from tools.self_pickup_problem_upload_tool import run_self_pickup_problem_upload


def _preview_payload(account_id: str):
    return {
        "ok": True,
        "stage": "dry_run",
        "account_id": account_id,
        "candidate_count": 1,
        "candidates": [{"bill_code": "R_TEST"}],
        "preview_fingerprint": "a" * 64,
        "source": {"resource_key": "test"},
    }


def test_self_pickup_legacy_wrapper_requires_project_account_bindings():
    with patch("tools.self_pickup_problem_upload_tool.call_http_service") as call_mock:
        result = run_self_pickup_problem_upload({"dry_run": True})
    assert result["stage"] == "blocked_config"
    assert "account_id" in result["error"]
    assert "daxiang_s_account_id" in result["error"]
    call_mock.assert_not_called()


def test_split_preview_forces_dry_run_and_emits_evidence():
    calls = []

    def runner(params):
        calls.append(params)
        return _preview_payload(params["account_id"])

    result = preview_split_pending_problems({"account_id": "ronghui_default"}, runner=runner)

    assert calls == [{"account_id": "ronghui_default", "dry_run": True}]
    assert result["status"] == "SUCCESS"
    assert result["meta"]["pagination_complete"] is True
    assert result["meta"]["record_count"] == 1
    assert result["meta"]["evidence_refs"]


def test_self_pickup_preview_forces_dry_run_and_emits_evidence():
    calls = []

    def runner(params):
        calls.append(params)
        return _preview_payload(params["account_id"])

    result = preview_self_pickup_problems(
        {
            "account_id": "ronghui_self_pickup_problem",
            "daxiang_s_account_id": "ronghui_daxiang_s",
        },
        runner=runner,
    )

    assert calls == [
        {
            "account_id": "ronghui_self_pickup_problem",
            "daxiang_s_account_id": "ronghui_daxiang_s",
            "dry_run": True,
        }
    ]
    assert result["status"] == "SUCCESS"
    assert result["data"]["stage"] == "dry_run"


def test_preview_rejects_extra_write_arguments_without_calling_runner():
    def runner(_params):
        raise AssertionError("runner must not be called")

    result = preview_split_pending_problems(
        {"account_id": "ronghui_default", "dry_run": False},
        runner=runner,
    )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "INVALID_ARGUMENTS"


def test_preview_rejects_non_dry_run_underlying_contract():
    result = preview_self_pickup_problems(
        {
            "account_id": "ronghui_self_pickup_problem",
            "daxiang_s_account_id": "ronghui_daxiang_s",
        },
        runner=lambda _params: {
            **_preview_payload("ronghui_self_pickup_problem"),
            "stage": "done",
        },
    )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "PREVIEW_FAILED"


def test_preview_rejects_candidate_count_mismatch():
    result = preview_split_pending_problems(
        {"account_id": "ronghui_default"},
        runner=lambda _params: {
            **_preview_payload("ronghui_default"),
            "candidate_count": 2,
        },
    )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "INVALID_PREVIEW_CONTRACT"


def test_self_pickup_preview_rejects_missing_fingerprint():
    result = preview_self_pickup_problems(
        {
            "account_id": "ronghui_self_pickup_problem",
            "daxiang_s_account_id": "ronghui_daxiang_s",
        },
        runner=lambda _params: {
            key: value
            for key, value in _preview_payload("ronghui_self_pickup_problem").items()
            if key != "preview_fingerprint"
        },
    )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "INVALID_PREVIEW_CONTRACT"
