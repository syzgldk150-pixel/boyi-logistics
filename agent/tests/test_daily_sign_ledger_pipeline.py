"""Focused tests for the authoritative daily-sign collection chain."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from agent.orchestration.models import OperationType, PlanStep
from agent.orchestration.result_verifier import ResultVerifier
from agent.tms_runtime.scripts import get_qianshou, get_sign_records
from agent.tool_registry import ToolRegistry
from tools import daily_sign_pipeline
from tools.daily_sign_rules import build_ledger_row


ACCOUNT_PARAMS = {
    "r13_account_id": "r13_default",
    "account_id": "ronghui_daxiang_s",
    "days": 1,
}


def _empty_state() -> dict:
    return {
        "ledger": {},
        "arrivals": {},
        "target_station_codes": set(),
        "problems": {},
        "signs": {},
        "source_refs": [],
        "arrival_source_proof": {
            "complete": True,
            "active_stat_runs": 1,
            "latest_forecast_runs": 0,
            "run_ids": ["arrival-run"],
        },
    }


def _r13_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: payload,
    )


def _r13_session(
    query_response: SimpleNamespace,
    *,
    site_code: str = "site-from-bound-account",
    site_type_code: int = 101,
    captured_query: dict | None = None,
    captured_requests: list[dict] | None = None,
) -> SimpleNamespace:
    account_response = _r13_response(
        {
            "code": 200,
            "data": {
                "siteCode": site_code,
                "siteName": "绑定账号所属站点",
                "siteTypeCode": site_type_code,
            },
        }
    )

    def post(url, *args, **kwargs):
        if captured_requests is not None:
            captured_requests.append(
                {"url": url, "args": args, "kwargs": dict(kwargs)}
            )
        if url == get_qianshou.AUTH_CONTEXT_URL:
            return account_response
        if captured_query is not None:
            captured_query.update(kwargs.get("json") or {})
        return query_response

    return SimpleNamespace(post=post)


class DailySignRuleTests(unittest.TestCase):
    def test_r13_sign_signal_does_not_close_without_main_sign_event(self):
        row = build_ledger_row(
            "R001",
            r13_row={
                "billNumberMain": "R001",
                "planSignTime": "2026-08-13 23:59:59",
                "signTime": "2026-08-13 08:00:00",
                "signSiteName": "展示状态",
            },
            previous_row=None,
            arrival_history=[],
            problem_events=[],
            sign_event=None,
            observed_at=datetime(2026, 8, 13, 9, 0, 0),
        )

        self.assertFalse(row["tms_signed"])
        self.assertIn("r13_signed_without_tms_scan", row["data_quality_flags"])

    def test_missing_arrival_and_r13_dates_never_guess_a_due_date(self):
        row = build_ledger_row(
            "R002",
            r13_row={"billNumberMain": "R002"},
            previous_row=None,
            arrival_history=[],
            problem_events=[],
            sign_event=None,
            observed_at=datetime(2026, 8, 13, 9, 0, 0),
        )

        self.assertIsNone(row["system_sign_due_at"])
        self.assertIsNone(row["r13_plan_sign_at"])
        self.assertFalse(row["tms_signed"])


class DailySignPipelineTests(unittest.TestCase):
    def test_r13_rows_require_a_valid_plan_sign_time(self):
        for value, expected_code in (
            ("", "SOURCE_FIELD_MISSING"),
            ("not-a-time", "SOURCE_FIELD_INVALID"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(daily_sign_pipeline.DailySignSyncError) as caught:
                    daily_sign_pipeline._r13_rows_by_code(
                        [{"billNumberMain": "R001", "planSignTime": value}]
                    )
                self.assertEqual(expected_code, caught.exception.code)

    def test_r13_request_rejects_inline_credentials(self):
        with self.assertRaises(daily_sign_pipeline.DailySignSyncError) as caught:
            daily_sign_pipeline._resolve_r13_request(
                {"request_body": {"days": 1, "username": "caller-controlled"}},
                "r13_default",
            )

        self.assertEqual("INVALID_ARGUMENT", caught.exception.code)

    def test_r13_account_auth_failure_keeps_auth_required_code(self):
        class AccountError(RuntimeError):
            code = "AUTH_REQUIRED"

        manager = SimpleNamespace(
            require_active_binding_descriptor=lambda _account_id: {
                "account_id": "r13_default",
                "system": "r13",
            },
            resolve_role_account_params=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AccountError("credential unavailable")
            )
        )
        with (
            patch("tools.daily_sign_sync_tool.get_account_manager", return_value=manager),
            self.assertRaises(daily_sign_pipeline.DailySignSyncError) as caught,
        ):
            daily_sign_pipeline._resolve_r13_request(
                {"request_body": {"days": 1}},
                "r13_default",
            )

        self.assertEqual("AUTH_REQUIRED", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

    def test_rejects_problem_pages_without_authoritative_total(self):
        response = {
            "data": {
                "ok": True,
                "rows": [],
                "stats": {"total": 0, "returned": 0},
            }
        }
        with patch("tools.daily_sign_pipeline.call_http_service", return_value=response):
            with self.assertRaises(daily_sign_pipeline.DailySignSyncError) as caught:
                daily_sign_pipeline._collect_problem_events(
                    {},
                    account_id="ronghui_daxiang_s",
                    start=datetime(2026, 8, 12, 0, 0, 0),
                    end=datetime(2026, 8, 13, 9, 0, 0),
                )

        self.assertEqual("PAGINATION_INCOMPLETE", caught.exception.code)

    def test_problem_pagination_deduplicates_identical_source_rows(self):
        row = {
            "external_id": "problem-1",
            "waybill_no": "R001",
            "problem_type": "少货",
            "registered_at": "2026-08-12 10:00:00",
            "registered_site": "测试网点",
            "source_direction": "registered",
        }
        responses = [
            {
                "data": {
                    "ok": True,
                    "rows": [row],
                    "stats": {
                        "total": 2,
                        "returned": 1,
                        "total_authoritative": True,
                    },
                }
            },
            {
                "data": {
                    "ok": True,
                    "rows": [dict(row)],
                    "stats": {
                        "total": 2,
                        "returned": 1,
                        "total_authoritative": True,
                    },
                }
            },
        ]

        with patch("tools.daily_sign_pipeline.call_http_service", side_effect=responses):
            events, proof = daily_sign_pipeline._collect_problem_events(
                {"problem_page_size": 1},
                account_id="ronghui_daxiang_s",
                start=datetime(2026, 8, 12, 0, 0, 0),
                end=datetime(2026, 8, 13, 9, 0, 0),
            )

        self.assertEqual(1, len(events))
        self.assertEqual(1, proof["rows"])
        self.assertEqual(2, proof["declared_total"])
        self.assertTrue(proof["complete"])

    def test_problem_pagination_rejects_conflicting_duplicate_source_rows(self):
        first = {
            "external_id": "problem-1",
            "waybill_no": "R001",
            "problem_type": "少货",
            "registered_at": "2026-08-12 10:00:00",
            "registered_site": "测试网点",
            "source_direction": "registered",
        }
        second = {**first, "problem_type": "破损"}
        responses = [
            {
                "data": {
                    "ok": True,
                    "rows": [first],
                    "stats": {
                        "total": 2,
                        "returned": 1,
                        "total_authoritative": True,
                    },
                }
            },
            {
                "data": {
                    "ok": True,
                    "rows": [second],
                    "stats": {
                        "total": 2,
                        "returned": 1,
                        "total_authoritative": True,
                    },
                }
            },
        ]

        with (
            patch("tools.daily_sign_pipeline.call_http_service", side_effect=responses),
            self.assertRaises(daily_sign_pipeline.DailySignSyncError) as caught,
        ):
            daily_sign_pipeline._collect_problem_events(
                {"problem_page_size": 1},
                account_id="ronghui_daxiang_s",
                start=datetime(2026, 8, 12, 0, 0, 0),
                end=datetime(2026, 8, 13, 9, 0, 0),
            )

        self.assertEqual("SOURCE_DUPLICATE_CONFLICT", caught.exception.code)

    def test_success_persists_complete_sources_and_returns_unified_result(self):
        observed_at = datetime(2026, 8, 13, 9, 0, 0)
        state = _empty_state()
        state.update(
            {
                "arrivals": {
                    "R001": [
                        {
                            "business_date": date(2026, 8, 12),
                            "destination_station": "邵阳大祥S站",
                            "expected_quantity": 1,
                            "arrived_quantity": 1,
                        }
                    ]
                },
                "target_station_codes": {"R001"},
                "source_refs": ["arrival_stat:arrival-run:fingerprint"],
            }
        )
        r13_rows = [
            {
                "billNumberMain": "R001",
                "planSignTime": "2026-08-13 23:59:59",
                "goodsName": "配件",
                "packTypeDesc": "纸箱",
                "pcs": 1,
                "dispAddress": "湖南省******",
                "dispatchMode": "送货",
                "signTime": "2026-08-13 08:00:00",
                "signSiteName": "R13 展示网点",
                "dispTime": None,
            }
        ]

        def source_call(endpoint, _payload):
            if endpoint == "/get_qianshou":
                return r13_rows
            if endpoint == "/customer_service_problem":
                return {
                    "data": {
                        "ok": True,
                        "rows": [],
                        "stats": {
                            "total": 0,
                            "returned": 0,
                            "total_authoritative": True,
                        },
                    }
                }
            if endpoint == "/get_sign_records":
                return []
            raise AssertionError(f"unexpected source endpoint: {endpoint}")

        def detail_call(endpoint, _payload):
            self.assertEqual("/query_waybill_detail", endpoint)
            return {
                "data": [
                    {
                        "tracking_number": "R001",
                        "recipient_address": "湖南省邵阳市大祥区",
                    }
                ]
            }

        with (
            patch(
                "tools.daily_sign_pipeline.start_sync_run",
                return_value=("source-run-1", observed_at),
            ),
            patch("tools.daily_sign_pipeline.load_daily_sign_state", return_value=state),
            patch(
                "tools.daily_sign_pipeline.earliest_relevant_source_date",
                return_value=date(2026, 8, 12),
            ),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch("tools.daily_sign_pipeline.call_http_service", side_effect=source_call),
            patch("tools.daily_sign_sync_tool.call_http_service", side_effect=detail_call),
            patch(
                "tools.daily_sign_sync_tool.get_waybill_tracking_cache",
                return_value={"arrived_quantity": 1},
            ),
            patch(
                "tools.daily_sign_pipeline.persist_daily_sign_snapshot",
                return_value={"ok": True, "ledger_rows": 1},
            ) as persist,
            patch(
                "tools.daily_sign_pipeline.sync_bitable_snapshot",
                return_value={"ok": True, "written": 1},
            ),
            patch(
                "tools.daily_sign_pipeline.sync_sheet_snapshot",
                return_value={"ok": True, "rows": 1},
            ),
            patch("tools.daily_sign_pipeline.finish_sync_run") as finish,
        ):
            result = daily_sign_pipeline.run_authoritative_daily_sign_sync(ACCOUNT_PARAMS)

        self.assertEqual("SUCCESS", result["status"])
        self.assertEqual("source-run-1", result["data"]["source_run_id"])
        self.assertTrue(result["meta"]["pagination_complete"])
        self.assertTrue(result["meta"]["postconditions"]["0"])
        self.assertEqual(["daily_sign:R001"], result["data"]["legacy_candidate_keys"])
        persisted_row = persist.call_args.kwargs["ledger_rows"][0]
        self.assertFalse(persisted_row["tms_signed"])
        self.assertIn("r13_signed_without_tms_scan", persisted_row["data_quality_flags"])
        finish_values = finish.call_args.args[1]
        self.assertEqual("success", finish_values["status"])
        self.assertTrue(finish_values["r13_complete"])
        self.assertTrue(finish_values["problems_complete"])
        self.assertTrue(finish_values["signs_complete"])
        capability = ToolRegistry().get_capability("sync_daily_should_sign")
        self.assertIsNotNone(capability)
        verification = ResultVerifier().verify(
            PlanStep(
                step_key="sync",
                tool_name="sync_daily_should_sign",
                tool_version="2.1.0",
                operation_type=OperationType.INTERNAL_PROJECTION_WRITE,
                arguments=ACCOUNT_PARAMS,
                account_id=None,
                depends_on=(),
                idempotency_key="daily-sign-test",
                expected_evidence=({"required": True},),
                postconditions=({"name": "authoritative_snapshot_committed"},),
            ),
            result,
            capability,
        )
        self.assertTrue(verification.accepted)

    def test_incomplete_problem_source_marks_run_failed_and_never_persists_ledger(self):
        observed_at = datetime(2026, 8, 13, 9, 0, 0)

        def source_call(endpoint, _payload):
            if endpoint == "/get_qianshou":
                return [{"billNumberMain": "R001", "planSignTime": "2026-08-13"}]
            if endpoint == "/customer_service_problem":
                return {"data": {"ok": True, "rows": [], "stats": {"total": 0}}}
            raise AssertionError(f"unexpected source endpoint: {endpoint}")

        with (
            patch(
                "tools.daily_sign_pipeline.start_sync_run",
                return_value=("source-run-2", observed_at),
            ),
            patch("tools.daily_sign_pipeline.load_daily_sign_state", return_value=_empty_state()),
            patch(
                "tools.daily_sign_pipeline.earliest_relevant_source_date",
                return_value=None,
            ),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch("tools.daily_sign_pipeline.call_http_service", side_effect=source_call),
            patch("tools.daily_sign_pipeline.persist_daily_sign_snapshot") as persist,
            patch("tools.daily_sign_pipeline.finish_sync_run") as finish,
        ):
            result = daily_sign_pipeline.run_authoritative_daily_sign_sync(ACCOUNT_PARAMS)

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("PAGINATION_INCOMPLETE", result["error"]["code"])
        persist.assert_not_called()
        self.assertEqual("failed", finish.call_args.args[1]["status"])


class DailySignSourceTests(unittest.TestCase):
    def test_get_qianshou_default_range_matches_the_live_r13_page_horizon(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 27, 12, 0, 0, tzinfo=tz)

        with patch.object(get_qianshou, "datetime", FixedDateTime):
            start, end = get_qianshou._default_range(1)

        self.assertEqual("2026-08-25 00:00:00", start)
        self.assertEqual("2026-08-30 23:59:59", end)

    def test_get_qianshou_uses_the_live_r13_plan_sign_query_contract(self):
        payload = get_qianshou._build_payload(
            start="2026-08-25 00:00:00",
            end="2026-08-30 23:59:59",
            disp_site_codes=["site-code"],
            page_size=100,
            page=1,
        )

        self.assertEqual(2, payload["queryType"])
        self.assertEqual("2026-08-25 00:00:00", payload["planSignTime_CondStart"])
        self.assertEqual("2026-08-30 23:59:59", payload["planSignTime_CondEnd"])
        self.assertNotIn("scanTime_CondStart", payload)
        self.assertNotIn("scanTime_CondEnd", payload)

    def test_get_qianshou_uses_the_exact_authenticated_r13_request_contract(self):
        self.assertEqual(
            "https://r13.ronghuiwl.com/gateway/public/aurora/auth",
            get_qianshou.AUTH_CONTEXT_URL,
        )
        captured_requests: list[dict] = []
        login_calls: list[dict] = []
        session = _r13_session(
            _r13_response({"data": {"records": [], "total": 0}}),
            captured_requests=captured_requests,
        )

        def login_and_get_session(**kwargs):
            login_calls.append(dict(kwargs))
            return session

        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=login_and_get_session,
        )
        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username=None,
                password=None,
                start="2026-08-13 00:00:00",
                end="2026-08-13 23:59:59",
                days=1,
                page_size=100,
                page=1,
                fetch_all=True,
                max_pages=10,
                account_id="r13-selected-in-project",
            )

        self.assertEqual([], rows)
        self.assertTrue(login_calls)
        self.assertTrue(all(call["attach_bearer"] is False for call in login_calls))
        context_request = captured_requests[0]
        self.assertEqual(get_qianshou.AUTH_CONTEXT_URL, context_request["url"])
        self.assertEqual({}, context_request["kwargs"]["json"])
        self.assertEqual(20, context_request["kwargs"]["timeout"])
        headers = context_request["kwargs"]["headers"]
        self.assertEqual("https://r13.ronghuiwl.com", headers["Origin"])
        self.assertEqual("site", headers["x-appId"])
        self.assertEqual(get_qianshou.REFERER_URL, headers["aurora-back"])
        self.assertEqual(get_qianshou.REFERER_URL, headers["Referer"])
        self.assertEqual("token-placeholder", headers["aurora-token"])
        self.assertNotIn("Authorization", headers)
        query_request = captured_requests[1]
        self.assertEqual(
            "https://r13.ronghuiwl.com/gateway/site/waybillSignWarn/pageGet",
            query_request["url"],
        )
        self.assertEqual(headers, query_request["kwargs"]["headers"])
        self.assertEqual(20, query_request["kwargs"]["timeout"])

    def test_get_qianshou_derives_site_filter_from_bound_account_context(self):
        captured_query: dict = {}
        query_response = _r13_response({"data": {"records": [], "total": 0}})
        session = _r13_session(
            query_response,
            site_code="site-selected-by-account",
            captured_query=captured_query,
        )
        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=lambda **_kwargs: session,
        )

        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username=None,
                password=None,
                start="2026-08-13 00:00:00",
                end="2026-08-13 23:59:59",
                days=1,
                page_size=100,
                page=1,
                fetch_all=True,
                max_pages=10,
                account_id="r13-selected-in-project",
            )

        self.assertEqual([], rows)
        self.assertEqual(
            ["site-selected-by-account"],
            captured_query["dispSiteCode_CondList"],
        )

    def test_get_qianshou_center_account_uses_its_unscoped_page_contract(self):
        captured_query: dict = {}
        query_response = _r13_response({"data": {"records": [], "total": 0}})
        session = _r13_session(
            query_response,
            site_code="88888",
            site_type_code=999,
            captured_query=captured_query,
        )
        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=lambda **_kwargs: session,
        )

        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username=None,
                password=None,
                start="2026-08-13 00:00:00",
                end="2026-08-13 23:59:59",
                days=1,
                page_size=100,
                page=1,
                fetch_all=True,
                max_pages=10,
                account_id="r13-center-selected-in-project",
            )

        self.assertEqual([], rows)
        self.assertEqual([], captured_query["dispSiteCode_CondList"])

    def test_get_qianshou_accepts_current_waybill_identity_field(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "data": {
                    "records": [
                        {
                            "waybillNo": "R001",
                            "planSignTime": "2026-08-13 23:59:59",
                            "goodsName": "货物",
                            "pcs": 1,
                            "dispatchMode": "送货",
                        }
                    ],
                    "total": 1,
                }
            },
        )
        session = _r13_session(response)
        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=lambda **_kwargs: session,
        )
        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username=None,
                password=None,
                start="2026-08-13 00:00:00",
                end="2026-08-13 23:59:59",
                days=1,
                page_size=100,
                page=1,
                fetch_all=True,
                max_pages=10,
                account_id="r13-test",
            )

        self.assertEqual("R001", rows[0]["billNumberMain"])
        self.assertEqual("2026-08-13 23:59:59", rows[0]["planSignTime"])

    def test_get_qianshou_rejects_row_without_plan_sign_time(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "data": {
                    "records": [{"waybillNo": "R001", "planSignTime": ""}],
                    "total": 1,
                }
            },
        )
        session = _r13_session(response)
        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=lambda **_kwargs: session,
        )
        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            with self.assertRaisesRegex(RuntimeError, "without planSignTime"):
                get_qianshou.fetch_qianshou(
                    config_path=None,
                    username=None,
                    password=None,
                    start="2026-08-13 00:00:00",
                    end="2026-08-13 23:59:59",
                    days=1,
                    page_size=100,
                    page=1,
                    fetch_all=True,
                    max_pages=10,
                    account_id="r13-test",
                )

    def test_get_qianshou_refreshes_one_invalid_cached_token(self):
        invalid = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"code": -2, "data": "", "message": "Token无效"},
        )
        valid_query = _r13_response({"data": {"records": [], "total": 0}})
        cached_session = SimpleNamespace(post=lambda *_args, **_kwargs: invalid)
        fresh_session = _r13_session(valid_query)

        class Auth:
            def __init__(self, **_kwargs):
                self.last_token = "cached-token"
                self.calls = []

            def login_and_get_session(self, **kwargs):
                self.calls.append(dict(kwargs))
                if kwargs.get("allow_cached") is False:
                    self.last_token = "fresh-token"
                    return fresh_session
                return cached_session

        auth = Auth()
        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username="managed-user",
                password="managed-password",
                start="2026-08-13 00:00:00",
                end="2026-08-13 23:59:59",
                days=1,
                page_size=100,
                page=1,
                fetch_all=True,
                max_pages=10,
                account_id="r13-test",
            )

        self.assertEqual([], rows)
        self.assertEqual(2, len(auth.calls))
        self.assertIs(False, auth.calls[1]["allow_cached"])
        self.assertTrue(all(call["attach_bearer"] is False for call in auth.calls))

    def test_get_qianshou_rejects_site_drift_after_token_refresh(self):
        invalid = _r13_response(
            {"code": -2, "data": "", "message": "Token无效"}
        )
        initial_session = _r13_session(invalid, site_code="site-before-refresh")
        fresh_session = _r13_session(
            _r13_response({"data": {"records": [], "total": 0}}),
            site_code="site-after-refresh",
        )

        class Auth:
            def __init__(self, **_kwargs):
                self.last_token = "cached-token"

            def login_and_get_session(self, **kwargs):
                if kwargs.get("allow_cached") is False:
                    self.last_token = "fresh-token"
                    return fresh_session
                return initial_session

        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            Auth,
        ):
            with self.assertRaisesRegex(RuntimeError, "site changed"):
                get_qianshou.fetch_qianshou(
                    config_path=None,
                    username="managed-user",
                    password="managed-password",
                    start="2026-08-13 00:00:00",
                    end="2026-08-13 23:59:59",
                    days=1,
                    page_size=100,
                    page=1,
                    fetch_all=True,
                    max_pages=10,
                    account_id="r13-test",
                )

    def test_get_qianshou_requires_complete_account_site_context(self):
        with self.assertRaisesRegex(RuntimeError, "missing siteTypeCode"):
            get_qianshou._read_account_site_filter(
                {"code": 200, "data": {"siteCode": "site-code"}}
            )

    def test_get_qianshou_keeps_rows_with_r13_dispatch_or_sign_signals(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "data": {
                    "records": [
                        {
                            "billNumberMain": "R001",
                            "planSignTime": "2026-08-13 23:59:59",
                            "dispTime": "2026-08-13 08:00:00",
                            "signTime": "2026-08-13 08:30:00",
                            "signSiteName": "R13 展示网点",
                        }
                    ],
                    "total": 1,
                }
            },
        )
        session = _r13_session(response)
        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=lambda **_kwargs: session,
        )
        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username=None,
                password=None,
                start="2026-08-13 00:00:00",
                end="2026-08-13 23:59:59",
                days=1,
                page_size=100,
                page=1,
                fetch_all=True,
                max_pages=10,
                account_id="r13-test",
            )

        self.assertEqual(["R001"], [row["billNumberMain"] for row in rows])
        self.assertEqual("2026-08-13 08:30:00", rows[0]["signTime"])

    def test_get_qianshou_rejects_early_terminal_page_before_declared_total(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"records": [], "total": 1}},
        )
        session = _r13_session(response)
        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=lambda **_kwargs: session,
        )
        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            with self.assertRaises(RuntimeError):
                get_qianshou.fetch_qianshou(
                    config_path=None,
                    username=None,
                    password=None,
                    start="2026-08-13 00:00:00",
                    end="2026-08-13 23:59:59",
                    days=1,
                    page_size=100,
                    page=1,
                    fetch_all=True,
                    max_pages=10,
                    account_id="r13-test",
                )

    def test_get_qianshou_rejects_pages_without_authoritative_total(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"records": []}},
        )
        session = _r13_session(response)
        auth = SimpleNamespace(
            last_token="token-placeholder",
            login_and_get_session=lambda **_kwargs: session,
        )
        with patch(
            "agent.tms_runtime.scripts.get_qianshou.R13SSOAuth",
            return_value=auth,
        ):
            with self.assertRaises(RuntimeError):
                get_qianshou.fetch_qianshou(
                    config_path=None,
                    username=None,
                    password=None,
                    start="2026-08-13 00:00:00",
                    end="2026-08-13 23:59:59",
                    days=1,
                    page_size=100,
                    page=1,
                    fetch_all=True,
                    max_pages=10,
                    account_id="r13-test",
                )

    def test_get_sign_records_requires_explicit_time_window(self):
        with self.assertRaises(ValueError):
            get_sign_records.run_once({"account_id": "ronghui_daxiang_s"})


if __name__ == "__main__":
    unittest.main()
