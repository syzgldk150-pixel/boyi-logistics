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


def test_daily_sign_request_uses_exact_selected_account_credentials() -> None:
    class FakeAccountManager:
        def require_active_binding_descriptor(self, account_id):
            self.descriptor_account_id = account_id
            return {
                "account_id": account_id,
                "system": "r13",
            }

        def resolve_role_account_params(self, params, **kwargs):
            self.kwargs = kwargs
            return {**params, "username": "r13-user", "password": "r13-pass"}

    manager = FakeAccountManager()
    with patch(
        "tools.daily_sign_sync_tool.get_account_manager", return_value=manager
    ):
        request = daily_sign_sync_tool.build_daily_sign_request_body(
            {"r13_account_id": "r13-project-selected", "request_body": {"days": 1}}
        )

    assert manager.descriptor_account_id == "r13-project-selected"
    assert manager.kwargs["account_field"] == "r13_account_id"
    assert manager.kwargs["output_account_field"] == ""
    assert manager.kwargs["output_session_profile_field"] == ""
    assert request["r13_account_id"] == "r13-project-selected"
    assert "disp_site_code" not in request
    assert request["username"] == "r13-user"
    assert request["password"] == "r13-pass"


def test_daily_sign_request_accepts_any_selected_r13_account_id() -> None:
    manager = Mock()
    manager.require_active_binding_descriptor.return_value = {
        "account_id": "r13-user-selected",
        "system": "r13",
    }
    manager.resolve_role_account_params.side_effect = lambda params, **_kwargs: params
    with patch(
        "tools.daily_sign_sync_tool.get_account_manager", return_value=manager
    ):
        request = daily_sign_sync_tool.build_daily_sign_request_body(
            {"r13_account_id": "r13-user-selected", "days": 1}
        )

    assert request["r13_account_id"] == "r13-user-selected"
    manager.resolve_role_account_params.assert_called_once()


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
    for forbidden in ("disp_site_code", "dispSiteCode", "r13_site_code"):
        with pytest.raises(ValueError, match="broker-owned"):
            daily_sign_sync_tool.build_daily_sign_request_body(
                {
                    "r13_account_id": "r13_default",
                    "days": 1,
                    forbidden: "caller-controlled",
                }
            )


def test_complete_empty_r13_result_commits_and_publishes_zero_rows() -> None:
    observed_at = datetime(2026, 8, 13, 9, 0, 0)

    def persist(**kwargs):
        return {"persistence_marker": kwargs["persistence_marker"]}

    def verify_persistence(**kwargs):
        marker = kwargs["persistence_marker"]
        return {
            "verified": True,
            "record_count": len(kwargs["ledger_rows"]),
            "publication_rows": {"record_count": len(kwargs["publication_rows"])},
            "persistence_sha256": marker["marker_sha256"],
            "publication_sha256": marker["publication_rows"]["sha256"],
            "ledger_sha256": "l" * 64,
        }

    def verify_completed(*, expected_values, **_kwargs):
        marker = expected_values["diagnostics_json"]["persistence_commit"]
        return {
            "verified": True,
            "record_count": expected_values["published_rows"],
            "publication_sha256": marker["publication_rows"]["sha256"],
            "persistence_sha256": marker["marker_sha256"],
        }

    bitable = Mock(
        return_value={
            "ok": True,
            "written": 0,
            "readback": {
                "verified": True,
                "record_count": 0,
                "snapshot_sha256": "b" * 64,
            },
        }
    )
    sheet = Mock(
        return_value={
            "ok": True,
            "rows": 0,
            "readback": {
                "verified": True,
                "record_count": 0,
                "snapshot_sha256": "s" * 64,
            },
        }
    )
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
        "finish_sync_run": Mock(),
        "verify_daily_sign_completed_run": Mock(side_effect=verify_completed),
        "persist_daily_sign_snapshot": persist,
        "verify_daily_sign_persistence": Mock(side_effect=verify_persistence),
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
                "r13_account_id": "r13-project-selected",
                "account_id": "ronghui-project-selected",
                "days": 1,
            }
        )

    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 0
    assert result["data"]["diagnostics"]["r13_rows"] == 0
    assert result["data"]["diagnostics"]["published_rows"] == 0
    assert bitable.call_args.args[0] == []
    assert sheet.call_args.args[0] == []
