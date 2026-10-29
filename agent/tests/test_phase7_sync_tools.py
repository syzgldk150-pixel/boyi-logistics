"""Focused tests extracted from the former TMS runtime aggregate."""

from unittest.mock import ANY

from _tms_runtime_test_support import *  # noqa: F403


def _ronghui_params(**values):
    return {"account_id": "configured-ronghui-test", **values}


def _yunda_params(**values):
    return {"account_id": "configured-yunda-test", **values}


def _daily_persisted_snapshot_proof(**kwargs):
    return {
        "ok": True,
        "ledger_rows": len(kwargs["ledger_rows"]),
        "publication_rows": len(kwargs["publication_rows"]),
        "persistence_marker": kwargs["persistence_marker"],
    }


def _daily_persistence_readback_proof(**kwargs):
    marker = kwargs["persistence_marker"]

    def row_set(name):
        return {
            "verified": True,
            "record_count": marker[name]["count"],
            "sha256": marker[name]["sha256"],
        }

    return {
        "verified": True,
        "record_count": len(kwargs["ledger_rows"]),
        "problem_events": row_set("problem_events"),
        "sign_events": row_set("sign_events"),
        "sign_verification_states": row_set("sign_verification_states"),
        "ledger_rows": row_set("ledger_rows"),
        "publication_rows": row_set("publication_rows"),
        "ledger_sha256": marker["ledger_rows"]["sha256"],
        "publication_sha256": marker["publication_rows"]["sha256"],
        "persistence_sha256": marker["marker_sha256"],
    }


def _daily_projection_readback_proof(rows, *, digest_char):
    return {
        "verified": True,
        "record_count": len(rows),
        "snapshot_sha256": digest_char * 64,
    }


def _daily_completed_run_readback_proof(*, expected_values, **_kwargs):
    diagnostics = expected_values.get("diagnostics_json")
    marker = (
        diagnostics.get("persistence_commit")
        if isinstance(diagnostics, dict)
        else None
    )
    return {
        "verified": True,
        "record_count": expected_values.get("published_rows", 0),
        "publication_sha256": expected_values.get("fingerprint") or "",
        "persistence_sha256": (
            marker.get("marker_sha256") if isinstance(marker, dict) else ""
        ),
    }


def _daily_failed_run_values(_run_id, diagnostics, *, message):
    return {
        "status": "failed",
        "published_rows": 0,
        "fingerprint": diagnostics.get("fingerprint"),
        "diagnostics_json": diagnostics,
        "error_summary": message,
    }


class Phase7SyncToolTests(unittest.TestCase):
    def setUp(self):
        self.internal_token_patch = patch.dict(
            os.environ,
            {"AGENT_INTERNAL_API_TOKEN": "test-internal-token"},
            clear=False,
        )
        self.internal_token_patch.start()
        self.addCleanup(self.internal_token_patch.stop)

    def test_single_account_sync_builders_fail_closed_and_bind_approved_account(self):
        target_date = date(2026, 8, 13)
        builders = (
            (send_order_sync_tool._build_request_body, "ronghui-test"),
            (yunda_dispatch_forecast_sync_tool._build_request_body, "yunda-test"),
            (yunda_send_waybills_sync_tool._build_request_body, "yunda-test"),
        )

        for builder, account_id in builders:
            with self.subTest(builder=builder.__module__):
                with self.assertRaisesRegex(ValueError, "account_id"):
                    builder({}, target_date)
                request = builder({"account_id": account_id}, target_date)
                self.assertEqual(account_id, request["params"]["account_id"])
                self.assertNotIn("accountId", request["params"])
                self.assertNotIn("session_profile", request["params"])

        with self.assertRaisesRegex(ValueError, "不一致"):
            send_order_sync_tool._build_request_body(
                {
                    "account_id": "approved-account",
                    "request_body": {"params": {"accountId": "other-account"}},
                },
                target_date,
            )

    def test_get_waybill_tracking_cache_merges_console_waybill_and_scan_rows(self):
        calls: list[tuple[str, list[Any] | tuple[Any, ...] | None]] = []

        class Cursor:
            _next_row = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "FROM waybill_data wd" in sql:
                    self._next_row = None
                elif "FROM scan_codes" in sql:
                    self._next_row = {
                        "arrived_quantity": 1,
                        "first_arrival_at": "2026-06-03 10:00:00",
                        "last_arrival_at": "2026-06-03 10:00:00",
                    }
                elif "FROM waybills" in sql:
                    self._next_row = {
                        "waybill_no": "R0001",
                        "quantity_lines": "3件",
                        "receiver_name": "李四",
                        "receiver_phone": "13900000000",
                        "receiver_address": "湖南邵阳",
                        "destination_site": "邵阳自提部",
                    }

            def fetchone(self):
                return self._next_row

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def cursor(self):
                return self.cursor_obj

            def close(self):
                return None

        with (
            patch("tools.phase7_mysql_store.ensure_phase7_tables", return_value=None),
            patch("tools.phase7_mysql_store.ensure_console_waybill_table", return_value=None),
            patch("tools.phase7_mysql_store._connect", return_value=Connection()),
        ):
            cache = phase7_mysql_store.get_waybill_tracking_cache("R0001")

        self.assertEqual("R0001", cache["tracking_number"])
        self.assertEqual("李四", cache["recipient_name"])
        self.assertEqual("13900000000", cache["recipient_phone"])
        self.assertEqual("3件", cache["quantity"])
        self.assertEqual(1, cache["arrived_quantity"])
        self.assertTrue(any(params == ("R0001",) for _sql, params in calls))
        scan_sql = next(sql for sql, _params in calls if "FROM scan_codes" in sql)
        self.assertIn("COUNT(DISTINCT raw_code)", scan_sql)
        self.assertIn("MIN(last_seen_at)", scan_sql)
        self.assertIn("code_type = 'child'", scan_sql)
        self.assertNotIn("first_seen_at", scan_sql)

    def test_send_order_runtime_fetches_all_pages_by_default(self):
        class Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, params=None, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "params": params, "data": data})
                page_index = int(data["pageIndex"])
                rows = [
                    {"BILL_CODE": "R001", "REGISTER_DATE": "2026-05-12 08:00:00", "PIECE_NUMBER": "1", "GUEST_FREIGHT": "12.50"},
                    {"BILL_CODE": "R002", "REGISTER_DATE": "2026-05-12 09:00:00", "PIECE_NUMBER": "2", "GUEST_FREIGHT": 0},
                    {"BILL_CODE": "R003", "REGISTER_DATE": "2026-05-12 10:00:00", "PIECE_NUMBER": "3", "GUEST_FREIGHT": "15.50"},
                ]
                start = page_index * 2
                return Response({"total": 3, "data": rows[start:start + 2]})

        session = Session()
        with patch("Send_order.login_as_daxiang", return_value=session):
            rows = Send_order.run_once({"target_date": "2026-05-12", "page_size": 2})

        self.assertEqual(["R001", "R002", "R003"], [row["运单编号"] for row in rows])
        self.assertEqual(
            ["2026-05-12 08:00:00", "2026-05-12 09:00:00", "2026-05-12 10:00:00"],
            [row["发件日期"] for row in rows],
        )
        self.assertEqual(["0", "1"], [call["data"]["pageIndex"] for call in session.calls])
        self.assertIn("2026/05/12", session.calls[0]["data"]["REGISTER_DATE"])

    def test_send_order_runtime_keeps_explicit_single_page_behavior(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "total": 3,
                    "data": [{"BILL_CODE": "R003", "REGISTER_DATE": "2026-05-12 10:00:00", "GUEST_FREIGHT": "15.50"}],
                }

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, params=None, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"data": data})
                return Response()

        session = Session()
        with patch("Send_order.login_as_daxiang", return_value=session):
            rows = Send_order.run_once({"target_date": "2026-05-12", "page_index": 1, "page_size": 2})

        self.assertEqual(["R003"], [row["运单编号"] for row in rows])
        self.assertEqual("2026-05-12 10:00:00", rows[0]["发件日期"])
        self.assertEqual(1, len(session.calls))
        self.assertEqual("1", session.calls[0]["data"]["pageIndex"])

    def test_send_order_runtime_uses_bound_session_profile(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"total": 0, "data": []}

        class Session:
            def post(self, url, params=None, data=None, headers=None, allow_redirects=None, timeout=None):
                return Response()

        captured: dict[str, Any] = {}

        def fake_login(config_path=None, *, profile="default"):
            captured["profile"] = profile
            return Session()

        with patch("Send_order.login_as_daxiang", side_effect=fake_login):
            Send_order.run_once({"target_date": "2026-05-12", "session_profile": "ronghui_ops"})

        self.assertEqual("ronghui_ops", captured["profile"])

    def test_feishu_record_list_normalizes_lark_cli_record_id_list(self):
        payload = {
            "ok": True,
            "data": {
                "record_id_list": ["rec-1"],
                "data": [["YD001", "2026-05-11"]],
                "fields": ["主单号", "应派时间"],
            },
        }

        result = feishu_cli_tool._normalize_bitable_record_list(payload)

        self.assertEqual("rec-1", result["items"][0]["record_id"])
        self.assertEqual("YD001", result["items"][0]["fields"]["主单号"])
        self.assertEqual("2026-05-11", result["items"][0]["fields"]["应派时间"])

    def test_feishu_list_views_uses_open_api_and_normalizes_items(self):
        calls: list[tuple[str, str]] = []

        def _fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path))
            return {
                "ok": True,
                "data": {
                    "items": [{"view_id": "vewPending", "view_name": "未签收明细"}],
                    "has_more": False,
                },
            }

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=_fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "list_views",
                {"base_token": "base", "table_id": "table"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("vewPending", result["items"][0]["view_id"])
        self.assertEqual("GET", calls[0][0])
        self.assertIn("/open-apis/bitable/v1/apps/base/tables/table/views?", calls[0][1])

    def test_daily_sign_sync_surfaces_get_qianshou_error(self):
        from datetime import datetime

        state = {
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
        with (
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.start_sync_run",
                return_value=("source-run", datetime(2026, 8, 13, 9, 0, 0)),
            ),
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline.finish_sync_run") as finish_mock,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=_daily_completed_run_readback_proof,
            ) as verify_completed,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.call_http_service",
                return_value={
                    "ok": False,
                    "error_code": "AUTH_REQUIRED",
                    "error": "R13 SSO login failed",
                },
            ),
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(
                {
                    "r13_account_id": "r13_default",
                    "account_id": "ronghui_daxiang_s",
                    "days": 1,
                }
            )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("AUTH_REQUIRED", result["error"]["code"])
        finish_mock.assert_called_once()
        verify_completed.assert_called_once()

    def test_daily_sign_sync_rejects_implicit_accounts_before_source_calls(self):
        with (
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.call_http_service") as source_mock,
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.start_sync_run") as run_mock,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync({})

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("ACCOUNT_AMBIGUOUS", result["error"]["code"])
        source_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_daily_sign_sync_commits_c7_ledger_and_returns_unified_evidence(self):
        from datetime import datetime

        observed_at = datetime(2026, 8, 13, 9, 0, 0)
        state = {
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
        r13_rows = [
            {
                "billNumberMain": "R1",
                "planSignTime": "2026-08-13 23:59:59",
                "goodsName": "货物",
                "packTypeDesc": "纸箱",
                "pcs": 1,
                "dispatchMode": "送货",
                "dispAddress": "地址",
                "problemType": "",
                "problemRegisterDate": "",
                "problemCause": "",
                "problemRegisterSite": "",
            }
        ]
        with (
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.start_sync_run",
                return_value=("source-run", observed_at),
            ),
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 30, "fetch_all": True, "page": 1,
                              "include_history": True, "end": "2026-08-13 23:59:59"},
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.call_http_service",
                return_value={"data": r13_rows},
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._collect_problem_events",
                return_value=([], {"rows": 0, "declared_total": 0, "complete": True}),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._collect_sign_events",
                return_value=([], {"source_rows": 0, "complete": True}),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_r13_sign_conflicts",
                return_value=([], {"complete": True, "queried": 0}),
            ) as exact_mock,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_historical_sign_verifications",
                return_value=(
                    [],
                    {
                        "complete": True,
                        "queried": 0,
                        "verification_rows": [],
                    },
                ),
            ) as historical_mock,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._enrich_missing_addresses",
                side_effect=lambda rows, _params: (rows, {"ok": True, "updated": 0}),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.persist_daily_sign_snapshot",
                side_effect=_daily_persisted_snapshot_proof,
            ) as persist_mock,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.verify_daily_sign_persistence",
                side_effect=_daily_persistence_readback_proof,
            ) as verify_persistence,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_bitable",
                side_effect=lambda rows, _params: {
                    "ok": True,
                    "written": len(rows),
                    "readback": _daily_projection_readback_proof(
                        rows,
                        digest_char="b",
                    ),
                },
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_sheet",
                side_effect=lambda rows, _params: {
                    "ok": True,
                    "rows": len(rows),
                    "written": len(rows),
                    "unchanged": False,
                    "format_readback": {"verified": True},
                    "readback": _daily_projection_readback_proof(
                        rows,
                        digest_char="s",
                    ),
                },
            ),
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.finish_sync_run") as finish_mock,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=_daily_completed_run_readback_proof,
            ) as verify_completed,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(
                {
                    "r13_account_id": "r13_default",
                    "account_id": "ronghui_daxiang_s",
                    "days": 1,
                }
            )

        self.assertEqual("SUCCESS", result["status"])
        self.assertIsNone(result["error"])
        self.assertEqual("source-run", result["data"]["source_run_id"])
        self.assertEqual(["daily_sign:R1"], result["data"]["legacy_candidate_keys"])
        self.assertTrue(result["meta"]["pagination_complete"])
        persist_kwargs = persist_mock.call_args.kwargs
        marker = persist_kwargs["persistence_marker"]
        self.assertIn(
            f"mysql:daily_sign_ledger:{marker['ledger_rows']['sha256']}",
            result["meta"]["evidence_refs"],
        )
        self.assertEqual(1, len(persist_kwargs["ledger_rows"]))
        self.assertEqual([], persist_kwargs["sign_verification_states"])
        self.assertEqual(0, marker["problem_events"]["count"])
        self.assertEqual(0, marker["sign_events"]["count"])
        self.assertEqual(0, marker["sign_verification_states"]["count"])
        self.assertEqual(1, marker["ledger_rows"]["count"])
        self.assertEqual(1, marker["publication_rows"]["count"])
        self.assertEqual(64, len(marker["marker_sha256"]))
        verify_persistence.assert_called_once()
        verify_completed.assert_called_once()
        exact_mock.assert_not_called()
        historical_mock.assert_not_called()
        self.assertEqual("not_requested", result["data"]["diagnostics"]["secondary_sources"]["status"])
        self.assertFalse(finish_mock.call_args.args[1]["degraded"])

    def test_daily_sign_sync_rejects_incomplete_arrival_source_without_projection(self):
        from datetime import datetime

        observed_at = datetime(2026, 8, 13, 9, 0, 0)
        state = {
            "ledger": {},
            "arrivals": {},
            "target_station_codes": set(),
            "problems": {},
            "signs": {},
            "sign_verifications": {},
            "source_refs": ["arrival_stat:arrival-run:arrival-hash"],
            "arrival_source_proof": {
                "complete": False,
                "active_stat_runs": 0,
                "latest_forecast_runs": 0,
                "run_ids": ["arrival-run"],
            },
        }
        with (
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.start_sync_run",
                return_value=("source-run", observed_at),
            ),
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 30, "fetch_all": True, "page": 1,
                              "include_history": True, "end": "2026-08-13 23:59:59"},
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.call_http_service",
                return_value={"data": []},
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._collect_problem_events",
                return_value=([], {"rows": 0, "declared_total": 0, "complete": True}),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._collect_sign_events",
                return_value=([], {"source_rows": 0, "complete": True}),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_r13_sign_conflicts",
                return_value=([], {"complete": True, "queried": 0}),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_historical_sign_verifications",
                return_value=(
                    [],
                    {
                        "complete": False,
                        "errors": [{"tracking_number": "R1", "error": "source unavailable"}],
                        "verification_rows": [],
                    },
                ),
            ),
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._finish_failed_run",
                side_effect=_daily_failed_run_values,
            ) as failed_run_mock,
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=_daily_completed_run_readback_proof,
            ) as verify_completed,
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.persist_daily_sign_snapshot") as persist_mock,
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_bitable") as bitable_mock,
            patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_sheet") as sheet_mock,
        ):
            result = daily_sign_sync_tool.run_daily_sign_sync(
                {
                    "r13_account_id": "r13_default",
                    "account_id": "ronghui_daxiang_s",
                    "days": 1,
                }
            )

        self.assertEqual("FAILED", result["status"])
        self.assertEqual("INCOMPLETE_SOURCE_EVIDENCE", result["error"]["code"])
        self.assertFalse(result["error"]["retryable"])
        failed_run_mock.assert_called_once()
        verify_completed.assert_called_once()
        persist_mock.assert_not_called()
        bitable_mock.assert_not_called()
        sheet_mock.assert_not_called()

    def test_daily_sign_sync_omits_historical_row_outside_current_r13_source(self):
        from datetime import datetime

        observed_at = datetime(2026, 8, 13, 9, 0, 0)
        for plan_sign_at in (None, "not-a-time"):
            with self.subTest(plan_sign_at=plan_sign_at):
                state = {
                    "ledger": {
                        "R-HIST": {
                            "tracking_number": "R-HIST",
                            "r13_plan_sign_at": plan_sign_at,
                            "first_seen_r13_at": "2026-08-11 09:00:00",
                            "r13_current": False,
                            "tms_signed": False,
                            "goods_name": "货物",
                            "package_type": "纸箱",
                            "expected_quantity": 1,
                            "delivery_method": "送货",
                            "recipient_address": "地址",
                        }
                    },
                    "arrivals": {
                        "R-HIST": [
                            {
                                "business_date": "2026-08-12",
                                "expected_quantity": 1,
                                "arrived_quantity": 1,
                                "goods_name": "货物",
                                "package_type": "纸箱",
                                "delivery_method": "送货",
                                "recipient_address": "地址",
                            }
                        ]
                    },
                    "target_station_codes": {"R-HIST"},
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
                with (
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.start_sync_run",
                        return_value=("source-run", observed_at),
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.load_daily_sign_state",
                        return_value=state,
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._resolve_r13_request",
                        return_value={"days": 30, "fetch_all": True, "page": 1,
                              "include_history": True, "end": "2026-08-13 23:59:59"},
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.call_http_service",
                        return_value={"data": []},
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._source_query_window",
                        return_value=(observed_at, observed_at),
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._collect_problem_events",
                        return_value=(
                            [],
                            {"rows": 0, "declared_total": 0, "complete": True},
                        ),
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_pipeline._collect_sign_events",
                        return_value=([], {"source_rows": 0, "complete": True}),
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_r13_sign_conflicts",
                        return_value=([], {"complete": True, "queried": 0}),
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_historical_sign_verifications",
                        return_value=(
                            [],
                            {
                                "complete": True,
                                "queried": 1,
                                "verification_rows": [],
                            },
                        ),
                    ),
                    patch("service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.finish_sync_run"),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.verify_daily_sign_completed_run",
                        side_effect=_daily_completed_run_readback_proof,
                    ) as verify_completed,
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.persist_daily_sign_snapshot",
                        side_effect=_daily_persisted_snapshot_proof,
                    ) as persist_mock,
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.verify_daily_sign_persistence",
                        side_effect=_daily_persistence_readback_proof,
                    ),
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_bitable",
                        side_effect=lambda rows, _params: {
                            "ok": True,
                            "written": len(rows),
                            "readback": _daily_projection_readback_proof(
                                rows, digest_char="b"
                            ),
                        },
                    ) as bitable_mock,
                    patch(
                        "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool._sync_sheet",
                        side_effect=lambda rows, _params: {
                            "ok": True,
                            "rows": len(rows),
                            "written": len(rows),
                            "unchanged": False,
                            "format_readback": {"verified": True},
                            "readback": _daily_projection_readback_proof(
                                rows, digest_char="s"
                            ),
                        },
                    ) as sheet_mock,
                ):
                    result = daily_sign_sync_tool.run_daily_sign_sync(
                        {
                            "r13_account_id": "r13-project-selected",
                            "account_id": "ronghui-project-selected",
                            "days": 1,
                        }
                    )

                self.assertEqual("SUCCESS", result["status"])
                self.assertIsNone(result["error"])
                self.assertEqual(0, result["data"]["diagnostics"]["r13_rows"])
                self.assertEqual(0, result["data"]["diagnostics"]["candidate_rows"])
                self.assertEqual([], persist_mock.call_args.kwargs["ledger_rows"])
                verify_completed.assert_called_once()
                persist_mock.assert_called_once()
                self.assertEqual([], persist_mock.call_args.kwargs["publication_rows"])
                bitable_mock.assert_called_once_with([], ANY)
                sheet_mock.assert_called_once_with([], ANY)

    def test_daily_sign_sheet_values_match_header_columns(self):
        values = daily_sign_sync_tool._build_sheet_values(
            [
                {
                    "billNumberMain": "YS1",
                    "planSignTime": "2026-04-25 23:59:59",
                    "goodsName": "固化剂",
                    "packTypeDesc": "编织袋+桶",
                    "pcs": 30,
                    "dispAddress": "湖南省邵阳市大祥区雨溪镇",
                    "dispatchMode": "送货（不含上楼）",
                }
            ]
        )

        self.assertEqual(8, len(values[0]))
        self.assertEqual("湖南省邵阳市大祥区雨溪镇", values[0][5])
        self.assertEqual("送货（不含上楼）", values[0][6])
        self.assertEqual("", values[0][7])

    def test_daily_sign_sync_sorts_feishu_output_by_plan_sign_time(self):
        rows = daily_sign_sync_tool._sort_rows_by_plan_sign_time(
            [
                {"billNumberMain": "LATE", "planSignTime": "2026-06-21 23:59:59"},
                {"billNumberMain": "EARLY", "planSignTime": "2026-06-19 23:59:59"},
                {"billNumberMain": "MIDDLE", "planSignTime": "2026-06-20 23:59:59"},
            ]
        )
        written_values = daily_sign_sync_tool._build_sheet_values(rows)
        written_records = daily_sign_sync_tool._build_records(rows)
        self.assertEqual(["EARLY", "MIDDLE", "LATE"], [row[0] for row in written_values])
        self.assertEqual(
            ["EARLY", "MIDDLE", "LATE"],
            [record["fields"]["运单编号"] for record in written_records],
        )

    def test_daily_sign_sync_writes_arrived_quantity_to_sheet_column_h(self):
        rows = [
            {
                "billNumberMain": "R0001",
                "planSignTime": "2026-06-04 23:59:59",
                "goodsName": "配件",
                "packTypeDesc": "编织袋",
                "pcs": 6,
                "dispAddress": "湖南省邵阳市大祥区",
                "dispatchMode": "送货（不含上楼）",
            }
        ]
        with (
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.get_waybill_tracking_cache",
                create=True,
                return_value={"arrived_quantity": 4},
            ),
        ):
            rows, result = daily_sign_sync_tool._enrich_rows_with_arrival_quantities(rows, {})

        self.assertTrue(result["ok"])
        written_values = daily_sign_sync_tool._build_sheet_values(rows)
        captured_sheet_params = daily_sign_sync_tool._sheet_params_for_values(
            {
                "spreadsheet_token": "sheet-token",
                "range": "Sheet1!A2:G100",
                "clear_range": "Sheet1!A2:G100",
            },
            written_values,
        )
        self.assertEqual(8, len(written_values[0]))
        self.assertEqual(4, written_values[0][7])
        self.assertEqual("Sheet1!A2:H2", captured_sheet_params["range"])
        self.assertEqual("Sheet1!A2:H100", captured_sheet_params["clear_range"])

    def test_daily_sign_sync_enriches_masked_addresses_before_writing(self):
        rows = [
            {
                "billNumberMain": "R0001",
                "planSignTime": "2026-05-20 23:59:59",
                "goodsName": "瓦",
                "packTypeDesc": "托盘袋",
                "pcs": 2,
                "dispAddress": "湖南省******",
                "dispatchMode": "送货（不含上楼）",
            }
        ]
        with (
            patch(
                "service_v2_plugins.sync_daily_should_sign_v2.payload.business.daily_sign_sync_tool.call_http_service",
                return_value={
                    "ok": True,
                    "data": [
                        {
                            "tracking_number": "R0001",
                            "recipient_address": "湖南省邵阳市大祥区雨溪镇",
                        }
                    ],
                },
            ) as call_tms,
        ):
            rows, result = daily_sign_sync_tool._enrich_rows_with_detail_addresses(
                rows,
                {"account_id": "ronghui_daxiang_s"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["updated"])
        written_records = daily_sign_sync_tool._build_records(rows)
        written_values = daily_sign_sync_tool._build_sheet_values(rows)
        self.assertEqual("湖南省邵阳市大祥区雨溪镇", written_records[0]["fields"]["收件人地址"])
        self.assertEqual("湖南省邵阳市大祥区雨溪镇", written_values[0][5])
        self.assertEqual("/query_waybill_detail", call_tms.call_args.args[0])
        self.assertEqual(
            [{"bill_code": "R0001"}],
            call_tms.call_args.args[1]["params"]["items"],
        )

    def test_sheet_snapshot_can_clear_wider_range_than_write_range(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:G3",
            "clear_range": "Sheet1!A2:H3",
        }

        with (
            patch("tools.phase7_sync_common.get_workflow_resource", return_value=resource),
            patch("tools.phase7_sync_common.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = phase7_sync_common.sync_sheet_snapshot(
                "phase7.daily_sign_sheet",
                [["bill", "time", "goods", "pack", 1, "addr", "mode"]],
                {},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("clear_sheet", feishu_op.call_args_list[0].args[0])
        self.assertEqual("Sheet1!A2:H3", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual("write_sheet", feishu_op.call_args_list[1].args[0])
        self.assertEqual("Sheet1!A2:G3", feishu_op.call_args_list[1].args[1]["range"])

    def test_bitable_snapshot_marks_only_at_first_real_mutation(self):
        params = {"base_token": "base-token", "table_id": "table-id"}

        with patch(
            "tools.phase7_sync_common.feishu_operation",
            return_value={"error": "list failed"},
        ) as feishu_op:
            marks: list[str] = []
            result = phase7_sync_common.sync_bitable_snapshot(
                "phase7.site_send_bitable",
                [{"fields": {"运单编号": "WB-1"}}],
                params,
                mark_write_started=lambda: marks.append("started"),
            )

        self.assertIn("error", result)
        self.assertEqual([], marks)
        self.assertEqual(["list_records"], [call.args[0] for call in feishu_op.call_args_list])

        delete_actions: list[str] = []

        def delete_path(action, _params):
            delete_actions.append(action)
            if action == "list_records":
                return {"items": [{"record_id": "old-record"}]}
            self.assertEqual(["started"], delete_marks)
            if action == "delete_records":
                return {"ok": True, "deleted": 1}
            return {"ok": True, "written": 1}

        delete_marks: list[str] = []
        with patch("tools.phase7_sync_common.feishu_operation", side_effect=delete_path):
            deleted = phase7_sync_common.sync_bitable_snapshot(
                "phase7.site_send_bitable",
                [{"fields": {"运单编号": "WB-1"}}],
                params,
                mark_write_started=lambda: delete_marks.append("started"),
            )

        self.assertTrue(deleted["ok"])
        self.assertEqual(["list_records", "delete_records", "write_records"], delete_actions)
        self.assertEqual(["started"], delete_marks)

        write_actions: list[str] = []

        def write_path(action, _params):
            write_actions.append(action)
            if action == "list_records":
                return {"items": []}
            self.assertEqual(["started"], write_marks)
            return {"ok": True, "written": 1}

        write_marks: list[str] = []
        with patch("tools.phase7_sync_common.feishu_operation", side_effect=write_path):
            written = phase7_sync_common.sync_bitable_snapshot(
                "phase7.site_send_bitable",
                [{"fields": {"运单编号": "WB-1"}}],
                params,
                mark_write_started=lambda: write_marks.append("started"),
            )

        self.assertTrue(written["ok"])
        self.assertEqual(["list_records", "write_records"], write_actions)
        self.assertEqual(["started"], write_marks)

        no_op_actions: list[str] = []
        no_op_marks: list[str] = []

        def no_op_path(action, _params):
            no_op_actions.append(action)
            return {"items": []}

        with patch("tools.phase7_sync_common.feishu_operation", side_effect=no_op_path):
            no_op = phase7_sync_common.sync_bitable_snapshot(
                "phase7.site_send_bitable",
                [],
                params,
                mark_write_started=lambda: no_op_marks.append("started"),
            )

        self.assertTrue(no_op["ok"])
        self.assertEqual(["list_records"], no_op_actions)
        self.assertEqual([], no_op_marks)

    def test_sheet_snapshot_marks_after_cold_cache_resolution_and_before_mutation(self):
        params = {
            "spreadsheet_token": "sheet-token",
            "range": "Data!A2:F2",
            "clear_range": "Data!A2:F2",
        }
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        failed_calls: list[str] = []
        failed_marks: list[str] = []

        def metadata_failure(method, path, payload=None, timeout=30):
            failed_calls.append(method)
            self.assertTrue(path.endswith("/sheets/query"))
            return {"error": "sheet metadata unavailable"}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=metadata_failure):
            failed = phase7_sync_common.sync_sheet_snapshot(
                "phase7.site_send_sheet",
                [["WB-1", "发货站", "纸箱", 1, 2, "目的站"]],
                params,
                mark_write_started=lambda: failed_marks.append("started"),
            )

        self.assertIn("error", failed)
        self.assertEqual(["GET"], failed_calls)
        self.assertEqual([], failed_marks)

        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        mutation_calls: list[str] = []
        mutation_marks: list[str] = []

        def sheet_write_path(method, path, payload=None, timeout=30):
            if method in {"DELETE", "POST", "PUT"}:
                self.assertEqual(["started"], mutation_marks)
                mutation_calls.append(method)
                return {"code": 0, "data": {}}
            self.assertEqual("GET", method)
            self.assertTrue(path.endswith("/sheets/query"))
            return {
                "code": 0,
                "data": {
                    "sheets": [
                        {
                            "sheet_id": "sheet-data",
                            "title": "Data",
                            "gridProperties": {"rowCount": 100},
                        }
                    ]
                },
            }

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=sheet_write_path):
            written = phase7_sync_common.sync_sheet_snapshot(
                "phase7.site_send_sheet",
                [["WB-1", "发货站", "纸箱", 1, 2, "目的站"]],
                params,
                mark_write_started=lambda: mutation_marks.append("started"),
            )

        self.assertTrue(written["ok"])
        self.assertEqual(["DELETE", "PUT"], mutation_calls)
        self.assertEqual(["started"], mutation_marks)

        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        no_op_calls: list[str] = []
        no_op_marks: list[str] = []

        def empty_sheet_path(method, path, payload=None, timeout=30):
            no_op_calls.append(method)
            self.assertEqual("GET", method)
            self.assertTrue(path.endswith("/sheets/query"))
            return {
                "code": 0,
                "data": {
                    "sheets": [
                        {
                            "sheet_id": "sheet-data",
                            "title": "Data",
                            "gridProperties": {"rowCount": 1},
                        }
                    ]
                },
            }

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=empty_sheet_path):
            no_op = phase7_sync_common.sync_sheet_snapshot(
                "phase7.site_send_sheet",
                [],
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Data!A2:F100",
                    "clear_range": "Data!A2:F100",
                },
                mark_write_started=lambda: no_op_marks.append("started"),
            )

        self.assertTrue(no_op["ok"])
        self.assertEqual(["GET"], no_op_calls)
        self.assertEqual([], no_op_marks)

    def test_sheet_snapshot_includes_clear_error_detail(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:G3",
            "clear_range": "Sheet1!A2:H3",
        }

        with (
            patch("tools.phase7_sync_common.get_workflow_resource", return_value=resource),
            patch(
                "tools.phase7_sync_common.feishu_operation",
                return_value={"error": "range not found"},
            ),
        ):
            result = phase7_sync_common.sync_sheet_snapshot(
                "phase7.daily_sign_sheet",
                [["bill", "time", "goods", "pack", 1, "addr", "mode"]],
                {},
            )

        self.assertIn("飞书清空电子表格失败", result["error"])
        self.assertIn("range not found", result["error"])

    def test_feishu_clear_sheet_uses_dimension_apis(self):
        result = feishu_cli_tool.feishu_operation(
            "clear_sheet",
            {
                "spreadsheet_token": "sheet-token",
                "range": "Sheet1!A2:H3",
                "dry_run": True,
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual("DELETE", result["api"][0]["method"])
        self.assertEqual(
            "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range",
            result["api"][0]["url"],
        )
        self.assertEqual(
            {
                "dimension": {
                    "sheetId": "Sheet1",
                    "majorDimension": "ROWS",
                    "startIndex": 2,
                    "endIndex": 3,
                }
            },
            result["api"][0]["body"],
        )
        self.assertEqual(1, len(result["api"]))

    def test_feishu_clear_sheet_resolves_sheet_title_before_delete(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {"sheet_id": "abc123", "title": "Sheet1"},
                        ],
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:H3",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("abc123!A2:H3", result["range"])
        self.assertEqual("GET", calls[0][0])
        self.assertEqual(
            "/open-apis/sheets/v3/spreadsheets/sheet-token/sheets/query",
            calls[0][1],
        )
        self.assertEqual("DELETE", calls[1][0])
        self.assertEqual(
            "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range",
            calls[1][1],
        )
        self.assertEqual("abc123", calls[1][2]["dimension"]["sheetId"])
        self.assertEqual("ROWS", calls[1][2]["dimension"]["majorDimension"])
        self.assertEqual(2, calls[1][2]["dimension"]["startIndex"])
        self.assertEqual(3, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_feishu_clear_sheet_uses_only_sheet_when_title_changed(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        calls = []

        def fake_call_open_api(method, path, payload=None, timeout=30):
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "4103ec",
                                "title": "Yunda data",
                                "gridProperties": {"rowCount": 12},
                            }
                        ]
                    },
                }
            return {"code": 0, "data": {}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api):
            result = feishu_cli_tool.feishu_operation(
                "clear_sheet",
                {
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:Y5000",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual("4103ec!A2:Y12", result["range"])
        self.assertEqual("4103ec", calls[1][2]["dimension"]["sheetId"])
        self.assertEqual(12, calls[1][2]["dimension"]["endIndex"])
