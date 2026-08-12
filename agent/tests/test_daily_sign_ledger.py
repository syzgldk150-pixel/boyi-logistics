from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import patch

from tools.daily_sign_rules import build_ledger_row, calculate_system_sign_due
from tools import daily_sign_backfill_tool, daily_sign_sync_tool
from agent.tms_runtime.scripts import get_qianshou, get_scan, get_sign_records


def arrival(day: str, expected: int, arrived: int, destination: str = "邵阳大祥S站") -> dict:
    return {
        "business_date": day,
        "expected_quantity": expected,
        "arrived_quantity": arrived,
        "destination_station": destination,
    }


def problem(problem_type: str, registered_at: str, *, complete: bool = True) -> dict:
    return {
        "external_id": f"{problem_type}-{registered_at}",
        "problem_type": problem_type,
        "registered_at": registered_at,
        "upload_complete": complete,
    }


class DailySignLedgerRulesTest(unittest.TestCase):
 def test_normal_complete_is_due_next_day_end_of_day(self):
    due, state = calculate_system_sign_due([arrival("2026-08-12", 10, 10)], [])
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)
    self.assertEqual("complete_on_first_arrival", state["trace"]["reason"])


 def test_partial_without_valid_problem_is_due_next_day(self):
    due, _ = calculate_system_sign_due([arrival("2026-08-12", 10, 5)], [])
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_partial_with_successful_split_problem_before_cutoff_has_blank_due(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5)],
        [problem("少货/分批", "2026-08-12 16:59:59")],
    )
    self.assertIsNone(due)


 def test_split_problem_at_cutoff_or_failed_does_not_postpone(self):
    due_at_cutoff, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5)],
        [problem("少货/分批", "2026-08-12 17:00:00")],
    )
    due_failed, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5)],
        [problem("少货/分批", "2026-08-12 16:00:00", complete=False)],
    )
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due_at_cutoff)
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due_failed)


 def test_partial_completion_is_due_on_completion_day(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5), arrival("2026-08-13", 10, 5)],
        [problem("少货/分批", "2026-08-12 16:00:00")],
    )
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_daily_arrival_rows_are_accumulated_until_expected_quantity_is_reached(self):
    due, state = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5), arrival("2026-08-13", 10, 5)],
        [],
    )
    self.assertEqual(10, state["arrived_quantity"])
    self.assertEqual("completed", state["arrival_status"])
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_exact_manual_problem_before_cutoff_only_moves_due_later(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 5), arrival("2026-08-13", 10, 5)],
        [
            problem("少货/分批", "2026-08-12 16:00:00"),
            problem("客户要求延迟派送", "2026-08-13 16:59:59"),
            problem("联系不上收件人", "2026-08-14 16:00:00"),
        ],
    )
    self.assertEqual(datetime(2026, 8, 15, 23, 59, 59), due)


 def test_inexact_manual_type_and_after_cutoff_are_invalid(self):
    due, _ = calculate_system_sign_due(
        [arrival("2026-08-12", 10, 10)],
        [
            problem("客户要求延迟派送（其他）", "2026-08-13 16:00:00"),
            problem("联系不上收件人", "2026-08-13 17:00:00"),
        ],
    )
    self.assertEqual(datetime(2026, 8, 13, 23, 59, 59), due)


 def test_r13_only_candidate_keeps_r13_due_and_blank_system_due(self):
    row = build_ledger_row(
        "R1",
        r13_row={"billNumberMain": "R1", "planSignTime": "2026-08-13 23:59:59"},
        previous_row=None,
        arrival_history=[],
        problem_events=[],
        sign_event=None,
        observed_at=datetime(2026, 8, 12, 12, 0, 0),
    )
    self.assertEqual("2026-08-13 23:59:59", row["r13_plan_sign_at"])
    self.assertIsNone(row["system_sign_due_at"])
    self.assertIn("r13_without_arrival_history", row["data_quality_flags"])


 def test_r13_signed_without_tms_scan_stays_open_but_tms_main_sign_closes(self):
    open_row = build_ledger_row(
        "R1",
        r13_row={"billNumberMain": "R1", "signTime": "2026-08-13 10:00:00"},
        previous_row=None,
        arrival_history=[arrival("2026-08-12", 1, 1)],
        problem_events=[],
        sign_event=None,
        observed_at=datetime(2026, 8, 13, 12, 0, 0),
    )
    closed_row = build_ledger_row(
        "R1",
        r13_row={"billNumberMain": "R1", "isSigns": "未签"},
        previous_row=None,
        arrival_history=[arrival("2026-08-12", 1, 1)],
        problem_events=[],
        sign_event={"scanned_at": "2026-08-13 11:00:00", "scan_type": "签收"},
        observed_at=datetime(2026, 8, 13, 12, 0, 0),
    )
    self.assertFalse(open_row["tms_signed"])
    self.assertIn("r13_signed_without_tms_scan", open_row["data_quality_flags"])
    self.assertTrue(closed_row["tms_signed"])


class DailySignSyncPipelineTest(unittest.TestCase):
    def _state(self, *, signs=None):
        return {
            "ledger": {
                "OLD": {
                    "tracking_number": "OLD",
                    "r13_plan_sign_at": "2026-08-11 23:59:59",
                    "tms_signed": False,
                    "goods_name": "历史货",
                }
            },
            "arrivals": {
                "R1": [arrival("2026-08-12", 2, 2)],
                "R2": [arrival("2026-08-12", 1, 1, destination="旧网点")],
            },
            "target_station_codes": {"R1"},
            "problems": {},
            "signs": signs or {},
        }

    def test_candidate_union_keeps_r13_only_reroute_and_historical_until_tms_sign(self):
        before = self._state()
        after = self._state(
            signs={"OLD": {"tracking_number": "OLD", "scanned_at": "2026-08-12 10:00:00"}}
        )
        captured = []
        with (
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value=[
                    {"billNumberMain": "R2", "planSignTime": "2026-08-13 23:59:59", "signTime": "2026-08-12 09:00:00"},
                    {"billNumberMain": "R3", "planSignTime": "2026-08-13 23:59:59"},
                ],
            ),
            patch("tools.daily_sign_sync_tool.start_sync_run", return_value=("run", datetime(2026, 8, 12, 12, 0, 0))),
            patch("tools.daily_sign_sync_tool.finish_sync_run"),
            patch("tools.daily_sign_sync_tool._sync_manual_problem_events", return_value=([], {"ok": True, "complete": True})),
            patch("tools.daily_sign_sync_tool._sync_sign_events", return_value=([], {"ok": True, "complete": True})),
            patch("tools.daily_sign_sync_tool._sync_r13_sign_conflicts", return_value=([], {"ok": True, "complete": True})),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", side_effect=[before, after]),
            patch("tools.daily_sign_sync_tool.upsert_ledger_rows", return_value={"ok": True}),
            patch("tools.daily_sign_sync_tool._sync_bitable", return_value={"ok": True}),
            patch("tools.daily_sign_sync_tool._sync_sheet", side_effect=lambda rows, _params: captured.extend(rows) or {"ok": True}),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync({"enrich_addresses": False})

        self.assertTrue(result["ok"])
        self.assertEqual(["R1", "R2", "R3"], sorted(row["tracking_number"] for row in captured))
        r2 = next(row for row in captured if row["tracking_number"] == "R2")
        r3 = next(row for row in captured if row["tracking_number"] == "R3")
        self.assertFalse(r2["tms_signed"])
        self.assertIn("r13_signed_without_tms_scan", r2["data_quality_flags"])
        self.assertIsNone(r3["system_sign_due_at"])
        self.assertIsNone(r3["arrived_quantity"])

    def test_sign_query_failure_is_degraded_and_never_closes_old_rows(self):
        state = self._state()
        captured = []
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=[]),
            patch("tools.daily_sign_sync_tool.start_sync_run", return_value=("run", datetime(2026, 8, 12, 12, 0, 0))),
            patch("tools.daily_sign_sync_tool.finish_sync_run"),
            patch("tools.daily_sign_sync_tool._sync_manual_problem_events", return_value=([], {"ok": True, "complete": True})),
            patch("tools.daily_sign_sync_tool._sync_sign_events", return_value=(None, {"error": "scan unavailable", "complete": False})),
            patch("tools.daily_sign_sync_tool._sync_r13_sign_conflicts", return_value=([], {"ok": True, "complete": True})),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch("tools.daily_sign_sync_tool.upsert_ledger_rows", return_value={"ok": True}),
            patch("tools.daily_sign_sync_tool._sync_bitable", return_value={"ok": True}),
            patch("tools.daily_sign_sync_tool._sync_sheet", side_effect=lambda rows, _params: captured.extend(rows) or {"ok": True}),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync({"enrich_addresses": False})

        self.assertTrue(result["ok"])
        self.assertTrue(result["degraded"])
        self.assertIn("OLD", {row["tracking_number"] for row in captured})

    def test_problem_query_incomplete_stops_before_publish(self):
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=[]),
            patch("tools.daily_sign_sync_tool.start_sync_run", return_value=("run", datetime(2026, 8, 12, 12, 0, 0))),
            patch("tools.daily_sign_sync_tool.finish_sync_run"),
            patch("tools.daily_sign_sync_tool._sync_manual_problem_events", return_value=(None, {"error": "page interrupted", "complete": False})),
            patch("tools.daily_sign_sync_tool._sync_bitable") as bitable,
            patch("tools.daily_sign_sync_tool._sync_sheet") as sheet,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync({})

        self.assertIn("问题件同步不完整", result["error"])
        bitable.assert_not_called()
        sheet.assert_not_called()

    def test_problem_query_retries_transient_source_failure_without_guessing(self):
        problem_row = {
            "external_id": "P1",
            "registered_at": "2026-08-12 16:00:00",
            "problem_type": "联系不上收件人",
            "waybill_no": "R1",
            "registered_site": "邵阳大祥S站",
        }
        with (
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                side_effect=[
                    {
                        "ok": False,
                        "data": {
                            "ok": False,
                            "error_code": "SOURCE_QUERY_FAILED",
                            "message": "temporary source failure",
                        },
                    },
                    {
                        "ok": True,
                        "data": {
                            "ok": True,
                            "rows": [problem_row],
                            "stats": {"total": 1},
                        },
                    },
                ],
            ) as query,
            patch("tools.daily_sign_sync_tool.time.sleep") as sleep,
            patch(
                "tools.daily_sign_sync_tool.upsert_problem_events",
                return_value={"ok": True, "upserted": 1},
            ),
        ):
            events, result = daily_sign_sync_tool._sync_manual_problem_events(
                {
                    "problem_start_date": "2026-08-12",
                    "problem_end_date": "2026-08-12",
                    "problem_page_retries": 2,
                }
            )

        self.assertTrue(result["complete"])
        self.assertEqual("R1", events[0]["tracking_number"])
        self.assertEqual(2, query.call_count)
        sleep.assert_called_once_with(1.0)

    def test_problem_query_reports_persistent_source_error_without_raw_rows(self):
        failure = {
            "ok": False,
            "data": {
                "ok": False,
                "error_code": "SOURCE_QUERY_FAILED",
                "message": "source unavailable",
            },
        }
        with (
            patch("tools.daily_sign_sync_tool.call_http_service", return_value=failure),
            patch("tools.daily_sign_sync_tool.time.sleep"),
        ):
            events, result = daily_sign_sync_tool._sync_manual_problem_events(
                {
                    "problem_start_date": "2026-08-12",
                    "problem_end_date": "2026-08-12",
                    "problem_page_retries": 2,
                }
            )

        self.assertIsNone(events)
        self.assertFalse(result["complete"])
        self.assertEqual("SOURCE_QUERY_FAILED", result["error_code"])
        self.assertEqual("source unavailable", result["error"])
        self.assertNotIn("raw", result)

    def test_r13_signed_conflict_only_closes_on_exact_main_sign_route(self):
        state = {"signs": {}}
        responses = {
            "R1": {
                "data": {
                    "ok": True,
                    "type": "ronghui_tms",
                    "route_rows": [
                        {"scan_type": "签收", "scan_time": "2026-08-12 10:00:00", "scan_station": "邵阳大祥S站"}
                    ],
                }
            },
            "R2": {
                "data": {
                    "ok": True,
                    "type": "ronghui_tms",
                    "route_rows": [
                        {"scan_type": "到件", "scan_time": "2026-08-12 09:00:00", "scan_station": "邵阳大祥S站"}
                    ],
                }
            },
        }

        def fake_call(_endpoint, request):
            return responses[request["params"]["tracking_number"]]

        with (
            patch("tools.daily_sign_sync_tool.call_http_service", side_effect=fake_call),
            patch("tools.daily_sign_sync_tool.upsert_sign_events", return_value={"ok": True, "upserted": 1}) as store,
        ):
            events, result = daily_sign_sync_tool._sync_r13_sign_conflicts(
                {"exact_sign_workers": 2},
                {"R1": {"isSigns": "已签"}, "R2": {"signTime": "2026-08-12 11:00:00"}},
                state,
            )

        self.assertTrue(result["complete"])
        self.assertEqual(["R1"], [event["tracking_number"] for event in events])
        self.assertEqual("tms_tracking_exact", events[0]["source"])
        store.assert_called_once()

    def test_sheet_validates_nine_headers_and_writes_before_clearing_tail(self):
        actions = []

        def fake_operation(action, params):
            actions.append(action)
            if action == "read_sheet":
                return {"ok": True, "data": {"valueRange": {"values": [daily_sign_sync_tool.SHEET_HEADERS]}}}
            return {"ok": True}

        with (
            patch("tools.daily_sign_sync_tool.resolve_sheet_target", return_value=("token", "Sheet1!A2:I200")),
            patch("tools.daily_sign_sync_tool.feishu_operation", side_effect=fake_operation),
        ):
            result = daily_sign_sync_tool._sync_sheet(
                [{"tracking_number": "R1", "r13_plan_sign_at": "2026-08-13 23:59:59"}], {}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["read_sheet", "write_sheet", "clear_sheet"], actions)

    def test_sheet_header_mismatch_fails_without_write(self):
        with (
            patch("tools.daily_sign_sync_tool.resolve_sheet_target", return_value=("token", "Sheet1!A2:I200")),
            patch(
                "tools.daily_sign_sync_tool.feishu_operation",
                return_value={"ok": True, "data": {"valueRange": {"values": [["运单编号", "旧应签收时间"]]}}},
            ) as operation,
        ):
            result = daily_sign_sync_tool._sync_sheet([], {})

        self.assertIn("表头不一致", result["error"])
        self.assertEqual(1, operation.call_count)

    def test_bitable_uses_delta_write_then_delete(self):
        actions = []

        def fake_operation(action, params):
            actions.append(action)
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-a", "fields": {"运单编号": "A"}},
                        {"record_id": "rec-old", "fields": {"运单编号": "OLD"}},
                    ],
                }
            return {"ok": True, "written": len(params.get("records", [])), "deleted": len(params.get("record_ids", []))}

        with (
            patch("tools.daily_sign_sync_tool.resolve_bitable_target", return_value=("base", "table")),
            patch("tools.daily_sign_sync_tool._ensure_bitable_schema", return_value={"ok": True, "fields": {"R13应签收时间": 1}}),
            patch("tools.daily_sign_sync_tool.feishu_operation", side_effect=fake_operation),
        ):
            result = daily_sign_sync_tool._sync_bitable(
                [{"tracking_number": "A"}, {"tracking_number": "B"}], {}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["list_records", "write_records", "delete_records"], actions)


class DailySignSourceCompletenessTest(unittest.TestCase):
    def test_get_scan_uses_resolved_session_profile_and_login_site_cookie(self):
        session = object()
        profiles = []

        class Auth:
            def __init__(self, *, profile):
                profiles.append(profile)

            def login_and_get_session(self):
                return session

        with (
            patch("agent.tms_runtime.scripts.get_scan.TMSAuth", Auth),
            patch("agent.tms_runtime.scripts.get_scan._read_user_info_cookie", return_value={"SITE_CODE": "7390004"}),
            patch("agent.tms_runtime.scripts.get_scan._resolve_login_site_code_from_user_info", return_value="7390004"),
            patch("agent.tms_runtime.scripts.get_scan.collect_scan_rows", return_value=[]) as collect,
        ):
            result = get_scan.run_once(
                {
                    "date": "2026/08/12",
                    "output_format": "json",
                    "session_profile": "account-ronghui-daxiang-s",
                    "use_login_site_code": True,
                }
            )

        self.assertEqual([], result)
        self.assertEqual(["account-ronghui-daxiang-s"], profiles)
        self.assertEqual("7390004", collect.call_args.kwargs["site_code"])

    def test_r13_keeps_signed_fields_instead_of_filtering_them(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "records": [
                            {
                                "billNumberMain": "R1",
                                "planSignTime": "2026-08-13 23:59:59",
                                "isSigns": "已签",
                                "signSiteName": "邵阳大祥S站",
                                "signTime": "2026-08-12 10:00:00",
                                "dispTime": "2026-08-12 09:00:00",
                            }
                        ],
                        "total": 1,
                    }
                }

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

        class Auth:
            last_token = "token"

            def __init__(self, **_kwargs):
                pass

            def login_and_get_session(self, **_kwargs):
                return Session()

        with patch("agent.tms_runtime.scripts.get_qianshou.R13SSOAuth", Auth):
            rows = get_qianshou.fetch_qianshou(
                config_path=None,
                username=None,
                password=None,
                disp_site_code="7390004",
                start="2026-08-12 00:00:00",
                end="2026-08-12 23:59:59",
                days=1,
                page_size=100,
                page=1,
                account_id="r13_default",
            )

        self.assertEqual(1, len(rows))
        self.assertEqual("已签", rows[0]["isSigns"])
        self.assertEqual("2026-08-12 10:00:00", rows[0]["signTime"])
        self.assertEqual("2026-08-12 09:00:00", rows[0]["dispTime"])

    def test_r13_page_limit_is_incomplete_failure(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": {"records": [{"billNumberMain": "R1"}], "total": 2}}

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

        class Auth:
            last_token = "token"

            def __init__(self, **_kwargs):
                pass

            def login_and_get_session(self, **_kwargs):
                return Session()

        with patch("agent.tms_runtime.scripts.get_qianshou.R13SSOAuth", Auth):
            with self.assertRaisesRegex(RuntimeError, "max_pages"):
                get_qianshou.fetch_qianshou(
                    config_path=None,
                    username=None,
                    password=None,
                    disp_site_code="7390004",
                    start="2026-08-12 00:00:00",
                    end="2026-08-12 23:59:59",
                    days=1,
                    page_size=1,
                    page=1,
                    max_pages=1,
                    account_id="r13_default",
                )

    def test_tms_scan_keeps_type_time_and_site_and_fails_at_page_limit(self):
        raw_page = {
            "data": [
                {
                    "BILL_CODE": "R1",
                    "DESTINATION": "邵阳大祥S站",
                    "SCAN_TYPE": "签收",
                    "SCAN_DATE": "2026-08-12 10:00:00",
                    "SCAN_SITE": "邵阳大祥S站",
                }
            ]
        }
        with patch("agent.tms_runtime.scripts.get_scan.fetch_page", side_effect=[raw_page, {"data": []}]):
            rows = get_scan.collect_scan_rows(
                object(),
                {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                "7390004",
                "签收",
                100,
                20,
                max_pages=2,
            )
        self.assertEqual("签收", rows[0]["scan_type"])
        self.assertEqual("2026-08-12 10:00:00", rows[0]["scan_time"])
        self.assertEqual("邵阳大祥S站", rows[0]["scan_site"])

        with patch("agent.tms_runtime.scripts.get_scan.fetch_page", return_value=raw_page):
            with self.assertRaisesRegex(RuntimeError, "max_pages"):
                get_scan.collect_scan_rows(
                    object(),
                    {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                    "7390004",
                    "签收",
                    100,
                    20,
                    max_pages=1,
                )

    def test_tms_scan_rejects_missing_bill_and_conflicting_duplicates(self):
        with patch(
            "agent.tms_runtime.scripts.get_scan.fetch_page",
            return_value={"data": [{"SCAN_TYPE": "签收"}]},
        ):
            with self.assertRaisesRegex(RuntimeError, "without BILL_CODE"):
                get_scan.collect_scan_rows(
                    object(),
                    {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                    "7390004",
                    "签收",
                    100,
                    20,
                )

    def test_tms_sign_query_uses_real_page_contract_and_paginates_completely(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "FIND_SIGNED_TOTAL" in url:
                    return Response(
                        {
                            "data": [
                                {
                                    "AREA_NAME": "虚拟湖南省区",
                                    "SIGN_SITE_CODE": "7390004",
                                    "TOTAL_NUM": 2,
                                }
                            ],
                            "total": 1,
                        }
                    )
                page = int(kwargs["data"]["pageIndex"])
                if page == 0:
                    rows = [
                        {
                            "BILL_CODE": "R1",
                            "SIGN_DATE": "2026-08-12 10:00:00",
                            "SIGN_SITE": "邵阳大祥S站",
                            "RECORD_DATE": "2026-08-12 10:00:01",
                            "RECORD_SITE": "邵阳大祥S站",
                        }
                    ]
                else:
                    rows = [
                        {
                            "BILL_CODE": "R2",
                            "SIGN_DATE": "2026-08-12 11:00:00",
                            "SIGN_SITE": "邵阳大祥S站",
                        }
                    ]
                return Response({"data": rows, "total": 2})

        session = Session()
        with (
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._ronghui_headers", return_value={}),
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._raise_if_source_failed"),
        ):
            rows = get_sign_records.collect_sign_rows(
                session,
                {},
                start=datetime(2026, 8, 12, 0, 0, 0),
                end=datetime(2026, 8, 12, 23, 59, 59),
                login_site_code="7390004",
                page_size=1,
                max_pages=2,
            )

        self.assertEqual(["R1", "R2"], [row["扫描单号"] for row in rows])
        self.assertTrue(all(row["扫描类型"] == "签收" for row in rows))
        self.assertIn("FIND_SIGNED_TOTAL", session.calls[0][0])
        self.assertIn("FIND_SIGNED_DETAIL_ALL_EXCEL", session.calls[1][0])
        first_payload = session.calls[0][1]["data"]
        self.assertEqual("SIGN_DATE", first_payload["searchDateType"])
        self.assertEqual("7390004", first_payload["LOGIN_SITE_CODE"])
        self.assertEqual(0, first_payload["pageIndex"])
        detail_payload = session.calls[1][1]["data"]
        self.assertEqual("7390004", detail_payload["SIGN_SITE_CODE"])
        self.assertEqual("虚拟湖南省区", detail_payload["AREA_NAME"])
        self.assertEqual(1, session.calls[2][1]["data"]["pageIndex"])

    def test_tms_sign_query_rejects_missing_real_fields_and_incomplete_paging(self):
        with self.assertRaisesRegex(RuntimeError, "BILL_CODE"):
            get_sign_records.normalize_sign_row({"SIGN_DATE": "2026-08-12 10:00:00"})

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def post(self, url, **_kwargs):
                if "FIND_SIGNED_TOTAL" in url:
                    return Response(
                        {
                            "data": [
                                {
                                    "AREA_NAME": "虚拟湖南省区",
                                    "SIGN_SITE_CODE": "7390004",
                                    "TOTAL_NUM": 2,
                                }
                            ],
                            "total": 1,
                        }
                    )
                return Response(
                    {
                        "data": [
                            {
                                "BILL_CODE": "R1",
                                "SIGN_DATE": "2026-08-12 10:00:00",
                                "SIGN_SITE": "邵阳大祥S站",
                            }
                        ],
                        "total": 2,
                    }
                )

        with (
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._ronghui_headers", return_value={}),
            patch("agent.tms_runtime.scripts.get_sign_records.page_support._raise_if_source_failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "提前结束"):
                get_sign_records.collect_sign_rows(
                    Session(),
                    {},
                    start=datetime(2026, 8, 12, 0, 0, 0),
                    end=datetime(2026, 8, 12, 23, 59, 59),
                    login_site_code="7390004",
                    page_size=2,
                    max_pages=2,
                )

        conflicting = {
            "data": [
                {
                    "BILL_CODE": "R1",
                    "SCAN_TYPE": "签收",
                    "SCAN_DATE": "2026-08-12 10:00:00",
                    "SCAN_SITE": "站点一",
                },
                {
                    "BILL_CODE": "R1",
                    "SCAN_TYPE": "签收",
                    "SCAN_DATE": "2026-08-12 10:00:00",
                    "SCAN_SITE": "站点二",
                },
            ]
        }
        with patch("agent.tms_runtime.scripts.get_scan.fetch_page", return_value=conflicting):
            with self.assertRaisesRegex(RuntimeError, "conflicting data"):
                get_scan.collect_scan_rows(
                    object(),
                    {"start": "2026/08/12 00:00:00", "end": "2026/08/12 23:59:59"},
                    "7390004",
                    "签收",
                    100,
                    20,
                )

    def test_r13_backfill_merge_preserves_sheet_fields_and_refreshes_r13_fields(self):
        seed = daily_sign_backfill_tool._seed_row_from_sheet(
            [
                "R1",
                "2026-08-12 23:59:59",
                "2026-08-13 23:59:59",
                "旧品名",
                "纸箱",
                2,
                "旧地址",
                "送货",
                2,
            ]
        )
        merged = daily_sign_backfill_tool._merge_r13_seed(
            seed,
            {
                "billNumberMain": "R1",
                "planSignTime": "2026-08-14 23:59:59",
                "isSigns": "已签",
            },
            observed_at=datetime(2026, 8, 12, 12, 0, 0),
        )

        self.assertEqual("2026-08-14 23:59:59", merged["r13_plan_sign_at"])
        self.assertEqual("2026-08-13 23:59:59", merged["system_sign_due_at"])
        self.assertEqual("旧地址", merged["recipient_address"])
        self.assertFalse(merged["tms_signed"])


class DailySignBackfillTest(unittest.TestCase):
    def test_shadow_backfill_rebuilds_union_and_only_removes_tms_signed_rows(self):
        archive_values = [
            [f"列{index}" for index in range(19)],
            ["R1", "货物", "纸箱", "送货", 1, "", "", "", "", "邵阳大祥S站", "", "", "地址", "", "", "", "", "", 1],
        ]
        current_values = [
            daily_sign_sync_tool.SHEET_HEADERS,
            ["OLD", "2026-08-11 23:59:59", "", "旧货", "纸箱", 1, "旧地址", "送货", 1],
        ]

        def fake_feishu(_action, params):
            values = current_values if params["spreadsheet_token"] == "current-token" else archive_values
            return {"ok": True, "data": {"valueRange": {"values": values}}}

        with (
            patch("tools.daily_sign_backfill_tool.get_required_resource", return_value={"spreadsheet_token": "archive-token"}),
            patch("tools.daily_sign_backfill_tool._spreadsheet_sheet_ref_map", return_value={"2026-08-12": "archive-id"}),
            patch("tools.daily_sign_backfill_tool._spreadsheet_sheet_info", return_value={"row_count": 2}),
            patch("tools.daily_sign_backfill_tool.get_workflow_resource", return_value={"spreadsheet_token": "current-token", "read_range": "sheet!A1:I200"}),
            patch("tools.daily_sign_backfill_tool.feishu_operation", side_effect=fake_feishu),
            patch("tools.daily_sign_backfill_tool.save_arrival_stat_snapshot", return_value={"ok": True, "skipped": True}),
            patch(
                "tools.daily_sign_backfill_tool._read_r13_history",
                return_value=(
                    [
                        {"billNumberMain": "R1", "planSignTime": "2026-08-13 23:59:59"},
                        {"billNumberMain": "R2", "planSignTime": "2026-08-13 23:59:59"},
                    ],
                    {"ok": True, "rows": 2},
                ),
            ),
            patch("tools.daily_sign_backfill_tool._sync_manual_problem_events", return_value=([], {"ok": True, "complete": True})),
            patch(
                "tools.daily_sign_backfill_tool._sync_sign_events",
                return_value=(
                    [
                        {
                            "tracking_number": "OLD",
                            "scanned_at": "2026-08-12 10:00:00",
                            "scan_type": "签收",
                        }
                    ],
                    {"ok": True, "complete": True},
                ),
            ),
            patch("tools.daily_sign_backfill_tool._sync_r13_sign_conflicts", return_value=([], {"ok": True, "complete": True})),
            patch("tools.daily_sign_backfill_tool.upsert_ledger_rows") as upsert,
        ):
            result = daily_sign_backfill_tool.run_daily_sign_backfill({"apply": False})

        self.assertTrue(result["ok"])
        self.assertEqual("shadow", result["mode"])
        self.assertEqual(2, result["rebuilt_open_rows"])
        self.assertEqual(1, result["removed_open_rows_with_tms_sign"])
        self.assertFalse(result["published"])
        upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
