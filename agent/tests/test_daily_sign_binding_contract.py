"""Focused account-binding and empty-projection contracts for daily sign."""

from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from tools import daily_sign_pipeline, daily_sign_sync_tool


def _empty_state() -> dict:
    return {
        "ledger": {},
        "arrivals": {},
        "target_station_codes": set(),
        "problems": {},
        "signs": {},
        "sign_verifications": {},
        "source_refs": ["arrival_stat:arrival-run:arrival-hash"],
        "arrival_source_proof": {
            "complete": True,
            "active_stat_runs": 1,
            "latest_forecast_runs": 0,
            "run_ids": ["arrival-run"],
        },
    }


def test_daily_sign_request_requires_explicit_r13_account_binding() -> None:
    with patch(
        "tools.daily_sign_sync_tool.get_workflow_resource"
    ) as resource_mock, pytest.raises(ValueError, match="r13_account_id"):
        daily_sign_sync_tool.build_daily_sign_request_body(
            {"request_body": {"days": 1}}
        )

    resource_mock.assert_not_called()


def test_daily_sign_request_uses_account_contract_credentials_and_site() -> None:
    class FakeAccountManager:
        def require_active_binding_descriptor(self, account_id):
            self.descriptor_account_id = account_id
            return {
                "account_id": account_id,
                "system": "r13",
                "site_code": "7390017",
            }

        def resolve_role_account_params(self, params, **kwargs):
            self.kwargs = kwargs
            return {**params, "username": "r13-user", "password": "r13-pass"}

    manager = FakeAccountManager()
    with patch(
        "tools.daily_sign_sync_tool.get_account_manager", return_value=manager
    ):
        request = daily_sign_sync_tool.build_daily_sign_request_body(
            {"r13_account_id": "r13_default", "request_body": {"days": 1}}
        )

    assert manager.descriptor_account_id == "r13_default"
    assert manager.kwargs["account_field"] == "r13_account_id"
    assert manager.kwargs["output_account_field"] == ""
    assert manager.kwargs["output_session_profile_field"] == ""
    assert request["disp_site_code"] == "7390017"
    assert request["username"] == "r13-user"
    assert request["password"] == "r13-pass"


@pytest.mark.parametrize("site_code", [None, "", "7390004", "r13-other-site"])
def test_daily_sign_request_rejects_account_outside_required_site(site_code) -> None:
    manager = Mock()
    manager.require_active_binding_descriptor.return_value = {
        "account_id": "r13-other",
        "system": "r13",
        "site_code": site_code,
    }
    with (
        patch("tools.daily_sign_sync_tool.get_account_manager", return_value=manager),
        pytest.raises(ValueError, match="站点合同"),
    ):
        daily_sign_sync_tool.build_daily_sign_request_body(
            {"r13_account_id": "r13-other", "days": 1}
        )

    manager.resolve_role_account_params.assert_not_called()


def test_daily_sign_request_rejects_nested_broker_owned_material() -> None:
    forbidden_fields = (
        "username",
        "password",
        "account_id",
        "r13_account_id",
        "disp_site_code",
        "dispSiteCode",
        "r13_site_code",
    )
    for forbidden in forbidden_fields:
        with pytest.raises(ValueError):
            daily_sign_sync_tool.build_daily_sign_request_body(
                {"request_body": {"days": 1, forbidden: "caller-controlled"}}
            )


def test_daily_sign_request_rejects_top_level_site_override() -> None:
    for forbidden in ("disp_site_code", "dispSiteCode"):
        with pytest.raises(ValueError, match="broker-owned"):
            daily_sign_sync_tool.build_daily_sign_request_body(
                {
                    "r13_account_id": "r13_default",
                    "days": 1,
                    forbidden: "caller-controlled",
                }
            )


def test_empty_r13_existing_projection_fails_before_persistence() -> None:
    observed_at = datetime(2026, 8, 13, 9, 0, 0)
    preflight = Mock(
        return_value={
            "error": "R13 零行但飞书仍有上一版应签数据",
            "error_code": "EMPTY_R13_SOURCE",
        }
    )
    persist = Mock()
    bitable = Mock()
    sheet = Mock()
    sync_replacements = {
        "start_sync_run": Mock(return_value=("source-run", observed_at)),
        "load_daily_sign_state": Mock(return_value=_empty_state()),
        "call_http_service": Mock(return_value={"data": []}),
        "_sync_r13_sign_conflicts": Mock(
            return_value=([], {"complete": True, "queried": 0})
        ),
        "_sync_historical_sign_verifications": Mock(
            return_value=(
                [],
                {"complete": True, "queried": 0, "verification_rows": []},
            )
        ),
        "_preflight_empty_r13_projection": preflight,
        "verify_daily_sign_completed_run": Mock(return_value={"verified": True}),
        "persist_daily_sign_snapshot": persist,
        "_sync_bitable": bitable,
        "_sync_sheet": sheet,
    }
    pipeline_replacements = {
        "_resolve_r13_request": Mock(
            return_value={"days": 1, "fetch_all": True, "page": 1}
        ),
        "_source_query_window": Mock(return_value=(observed_at, observed_at)),
        "_collect_problem_events": Mock(
            return_value=(
                [],
                {"rows": 0, "declared_total": 0, "complete": True},
            )
        ),
        "_collect_sign_events": Mock(
            return_value=([], {"source_rows": 0, "complete": True})
        ),
        "_finish_failed_run": Mock(return_value={"status": "failed"}),
    }
    with (
        patch.multiple(daily_sign_sync_tool, **sync_replacements),
        patch.multiple(daily_sign_pipeline, **pipeline_replacements),
    ):
        result = daily_sign_sync_tool.run_daily_sign_sync(
            {
                "r13_account_id": "r13_default",
                "account_id": "ronghui_daxiang_s",
                "days": 1,
            }
        )

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "EMPTY_R13_SOURCE"
    assert result["error"]["retryable"] is True
    preflight.assert_called_once()
    persist.assert_not_called()
    bitable.assert_not_called()
    sheet.assert_not_called()
