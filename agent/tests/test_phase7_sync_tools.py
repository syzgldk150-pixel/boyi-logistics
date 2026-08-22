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
        self.send_order_sql_patch = patch(
            "tools.send_order_sync_tool.sync_console_waybills",
            return_value={"ok": True, "upserted": 0, "updates": 0, "creates": 0, "deleted_stale": 0},
        )
        self.yunda_send_sql_patch = patch(
            "tools.yunda_send_waybills_sync_tool.sync_console_waybills",
            return_value={"ok": True, "upserted": 0, "updates": 0, "creates": 0, "deleted_stale": 0},
        )
        self.delivery_status_sql_patch = patch(
            "tools.delivery_status_sync_tool.update_console_waybill_statuses",
            return_value={"ok": True, "updated": 0, "status": "signed"},
        )
        self.internal_token_patch.start()
        self.send_order_sql_mock = self.send_order_sql_patch.start()
        self.addCleanup(self.internal_token_patch.stop)
        self.yunda_send_sql_mock = self.yunda_send_sql_patch.start()
        self.delivery_status_sql_mock = self.delivery_status_sql_patch.start()
        self.addCleanup(self.send_order_sql_patch.stop)
        self.addCleanup(self.yunda_send_sql_patch.stop)
        self.addCleanup(self.delivery_status_sql_patch.stop)

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

    def test_delivery_status_sync_scans_bitable_and_updates_signed_records_only(self):
        self.delivery_status_sql_mock.reset_mock()
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_views":
                return {
                    "ok": True,
                    "items": [
                        {"view_id": "vewPending", "view_name": "未签收明细"},
                        {"view_id": "veweDmbdIS", "view_name": "寄件数据(总表)"},
                    ],
                }
            if action == "list_records":
                self.assertEqual("Fcm8b2H7wayK1UsYLjlcFmWhnMh", params["base_token"])
                self.assertEqual("tblX96gGAuBfJrtW", params["table_id"])
                self.assertEqual("vewPending", params["view_id"])
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-signed", "fields": {"运单编号": "R001", "签收状态": "未签收"}},
                        {"record_id": "rec-still", "fields": {"运单编号": "R002", "签收状态": "未签收"}},
                        {"record_id": "rec-done", "fields": {"运单编号": "R003", "签收状态": "已签收"}},
                        {"record_id": "rec-empty", "fields": {"运单编号": "", "签收状态": "未签收"}},
                    ],
                }
            if action == "write_records":
                records = params["records"]
                self.assertEqual(
                    [{"record_id": "rec-signed", "fields": {"签收状态": "已签收"}}],
                    records,
                )
                return {"ok": True, "written": len(records)}
            raise AssertionError(action)

        with (
            patch("tools.delivery_status_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={
                    "ok": True,
                    "data": [
                        {"运单编号": "R001", "签收状态": "签收"},
                        {"运单编号": "R002", "签收状态": "未签收"},
                    ],
                },
            ) as call_http,
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync(_ronghui_params())

        self.assertTrue(result["ok"])
        self.assertEqual(4, result["scanned"])
        self.assertEqual(2, result["pending"])
        self.assertEqual(2, result["queried"])
        self.assertEqual(1, result["updated"])
        self.assertEqual(1, result["unchanged"])
        self.assertEqual(0, result["unmatched"])
        self.assertEqual(1, result["skipped_empty_waybill"])
        self.assertEqual("vewPending", result["list_result"]["view_id"])
        self.assertEqual("未签收明细", result["list_result"]["view_name"])
        self.assertEqual("/delivery_status", call_http.call_args.args[0])
        self.assertEqual("R001,R002", call_http.call_args.args[1]["params"]["bill_codes"])
        self.assertIn("write_records", [action for action, _params in calls])
        self.delivery_status_sql_mock.assert_called_once_with(["R001"], "signed")

    def test_delivery_status_sync_reads_records_beyond_first_feishu_page(self):
        self.delivery_status_sql_mock.reset_mock()
        list_offsets: list[int] = []

        def _fake_feishu_operation(action, params):
            if action == "list_views":
                return {"ok": True, "items": [{"view_id": "vewPending", "view_name": "未签收明细"}]}
            if action == "list_records":
                list_offsets.append(params["offset"])
                if params["offset"] == 0:
                    return {
                        "ok": True,
                        "items": [
                            {
                                "record_id": f"rec-old-{index}",
                                "fields": {"运单编号": f"R{index:03d}", "签收状态": "已签收"},
                            }
                            for index in range(200)
                        ],
                    }
                if params["offset"] == 200:
                    return {
                        "ok": True,
                        "items": [
                            {"record_id": "rec-late", "fields": {"运单编号": "R201", "签收状态": "未签收"}},
                        ],
                    }
                return {"ok": True, "items": []}
            if action == "write_records":
                self.assertEqual(
                    [{"record_id": "rec-late", "fields": {"签收状态": "已签收"}}],
                    params["records"],
                )
                return {"ok": True, "written": len(params["records"])}
            raise AssertionError(action)

        with (
            patch("tools.delivery_status_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R201", "签收状态": "已签收"}]},
            ) as call_http,
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync(_ronghui_params())

        self.assertTrue(result["ok"])
        self.assertEqual([0, 200], list_offsets)
        self.assertEqual(201, result["scanned"])
        self.assertEqual(1, result["pending"])
        self.assertEqual(1, result["queried"])
        self.assertEqual(1, result["updated"])
        self.assertEqual("R201", call_http.call_args.args[1]["params"]["bill_codes"])
        self.delivery_status_sql_mock.assert_called_once_with(["R201"], "signed")

    def test_delivery_status_sync_dry_run_does_not_write_bitable(self):
        self.delivery_status_sql_mock.reset_mock()
        def _fake_feishu_operation(action, params):
            if action == "list_views":
                return {"ok": True, "items": [{"view_id": "vewPending", "view_name": "未签收明细"}]}
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-signed", "fields": {"运单编号": "R001", "签收状态": "未签收"}},
                    ],
                }
            if action == "write_records":
                raise AssertionError("dry_run should not write records")
            raise AssertionError(action)

        with (
            patch("tools.delivery_status_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R001", "签收状态": "已签收"}]},
            ),
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync(_ronghui_params(dry_run=True))

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, result["updated"])
        self.assertEqual(
            [{"record_id": "rec-signed", "fields": {"签收状态": "已签收"}}],
            result["planned_records"],
        )
        self.delivery_status_sql_mock.assert_not_called()

    def test_delivery_status_sync_keeps_explicit_webhook_mode_compatible(self):
        self.delivery_status_sql_mock.reset_mock()
        def _fake_feishu_operation(action, params):
            if action == "write_records":
                self.assertEqual(
                    [{"record_id": "rec-1", "fields": {"签收状态": "未签收"}}],
                    params["records"],
                )
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch(
                "tools.delivery_status_sync_tool.get_workflow_resource",
                return_value={"base_token": "base", "table_id": "table"},
            ),
            patch("tools.delivery_status_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch(
                "tools.delivery_status_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R001", "签收状态": "未签收"}]},
            ),
        ):
            result = delivery_status_sync_tool.run_delivery_status_sync(
                _ronghui_params(bill_codes=["R001"], record_ids=["rec-1"])
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["matched"])
        self.assertEqual(1, result["updated"])
        self.delivery_status_sql_mock.assert_not_called()

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
                    {"BILL_CODE": "R001", "INSERT_DATE": "2026-05-12 08:00:00", "PIECE_NUMBER": "1"},
                    {"BILL_CODE": "R002", "INSERT_DATE": "2026-05-12 09:00:00", "PIECE_NUMBER": "2"},
                    {"BILL_CODE": "R003", "INSERT_DATE": "2026-05-12 10:00:00", "PIECE_NUMBER": "3"},
                ]
                start = page_index * 2
                return Response({"total": 3, "data": rows[start:start + 2]})

        session = Session()
        with patch("Send_order.login_as_daxiang", return_value=session):
            rows = Send_order.run_once({"target_date": "2026-05-12", "page_size": 2})

        self.assertEqual(["R001", "R002", "R003"], [row["运单编号"] for row in rows])
        self.assertEqual(["0", "1"], [call["data"]["pageIndex"] for call in session.calls])
        self.assertIn("2026/05/12", session.calls[0]["data"]["REGISTER_DATE"])

    def test_send_order_runtime_keeps_explicit_single_page_behavior(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "total": 3,
                    "data": [{"BILL_CODE": "R003", "INSERT_DATE": "2026-05-12 10:00:00"}],
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

    def test_send_order_sync_replaces_target_date_with_new_bitable_snapshot(self):
        tms_payload = {
            "ok": True,
            "data": [
                {
                    "运单编号": "R001",
                    "发件日期": "2026-05-12 08:00:00",
                    "件数": "1",
                    "录单金额": "12.50",
                }
            ],
        }
        actions: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            actions.append((action, params))
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "rec-keep",
                            "fields": {
                                "运单编号": "R001",
                                "发件日期": send_order_sync_tool._date_to_timestamp_ms(date(2026, 5, 12)),
                            },
                        },
                        {
                            "record_id": "rec-stale",
                            "fields": {"运单编号": "R002", "发件日期": "2026-05-12 09:00:00"},
                        },
                        {
                            "record_id": "rec-other-day",
                            "fields": {"运单编号": "R003", "发件日期": "2026-05-11 09:00:00"},
                        },
                    ],
                }
            if action == "write_records":
                records = params["records"]
                self.assertEqual(1, len(records))
                self.assertNotIn("record_id", records[0])
                self.assertEqual("R001", records[0]["fields"]["运单编号"])
                self.assertEqual(12.5, records[0]["fields"]["录单金额"])
                return {"ok": True, "written": 1}
            if action == "delete_records":
                self.assertEqual(["rec-keep", "rec-stale"], params["record_ids"])
                return {"ok": True, "deleted": 2}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload) as call_http,
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            self.send_order_sql_mock.return_value = {"ok": True, "upserted": 1, "updates": 1, "creates": 0, "deleted_stale": 1}
            result = send_order_sync_tool.run_send_order_sync(_ronghui_params(target_date="2026-05-12"))

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(2, result["deleted"])
        self.assertEqual(1, result["sql_upserted"])
        self.assertEqual(1, result["sql_deleted_stale"])
        self.send_order_sql_mock.assert_called_once()
        sql_records = self.send_order_sql_mock.call_args.args[0]
        self.assertEqual("R001", sql_records[0]["waybill_no"])
        self.assertEqual("2026-05-12", sql_records[0]["open_date"])
        self.assertEqual("12.50", sql_records[0]["freight_fee"])
        self.assertEqual("ronghui", self.send_order_sql_mock.call_args.kwargs["source"])
        self.assertTrue(self.send_order_sql_mock.call_args.kwargs["replace_date"])
        self.assertEqual("2026-05-12", call_http.call_args.args[1]["params"]["target_date"])
        delete_actions = [params for action, params in actions if action == "delete_records"]
        self.assertTrue(delete_actions)
        self.assertNotIn("rec-other-day", delete_actions[0]["record_ids"])
        self.assertLess(
            [action for action, _params in actions].index("delete_records"),
            [action for action, _params in actions].index("write_records"),
        )

    def test_send_order_sync_filters_receipt_like_h_and_hr_rows(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R00015275708", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
                {"运单编号": "H001", "发件日期": "2026-05-12 09:00:00", "件数": "1"},
                {"运单编号": "HR002", "发件日期": "2026-05-12 10:00:00", "件数": "1"},
            ],
        }

        def _fake_feishu_operation(action, params):
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-h", "fields": {"运单编号": "H001", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "write_records":
                records = params["records"]
                self.assertEqual(1, len(records))
                self.assertEqual("R00015275708", records[0]["fields"]["运单编号"])
                return {"ok": True, "written": 1}
            if action == "delete_records":
                self.assertEqual(["rec-h"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(_ronghui_params(target_date="2026-05-12"))

        self.assertTrue(result["ok"])
        self.assertEqual(3, result["raw_fetched"])
        self.assertEqual(1, result["fetched"])
        self.assertEqual(2, result["skipped_receipt_like"])
        self.assertEqual(1, result["deleted"])

    def test_send_order_sync_reads_existing_records_after_feishu_200_row_page(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R250", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        list_offsets: list[int] = []

        def _fake_feishu_operation(action, params):
            if action == "list_records":
                list_offsets.append(params["offset"])
                self.assertEqual(200, params["limit"])
                if params["offset"] == 0:
                    return {
                        "ok": True,
                        "items": [
                            {
                                "record_id": f"rec-other-day-{index}",
                                "fields": {
                                    "运单编号": f"R{index:03d}",
                                    "发件日期": "2026-05-11",
                                },
                            }
                            for index in range(200)
                        ],
                    }
                if params["offset"] == 200:
                    return {
                        "ok": True,
                        "items": [
                            {
                                "record_id": "rec-keep",
                                "fields": {"运单编号": "R250", "发件日期": "2026-05-12"},
                            }
                        ],
                    }
                return {"ok": True, "items": []}
            if action == "write_records":
                self.assertNotIn("record_id", params["records"][0])
                return {"ok": True, "written": len(params["records"])}
            if action == "delete_records":
                self.assertEqual(["rec-keep"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(_ronghui_params(target_date="2026-05-12"))

        self.assertTrue(result["ok"])
        self.assertEqual([0, 200, 0, 200], list_offsets)
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["deleted"])
        self.assertEqual(200, result["list_result"]["list_limit"])

    def test_send_order_sync_deletes_duplicate_existing_waybill_records(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R001", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        list_call_count = 0

        def _fake_feishu_operation(action, params):
            nonlocal list_call_count
            if action == "list_records":
                list_call_count += 1
                if list_call_count > 1:
                    return {
                        "ok": True,
                        "items": [
                            {"record_id": "rec-new", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        ],
                    }
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-main", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-dup-1", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-dup-2", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "write_records":
                self.assertNotIn("record_id", params["records"][0])
                return {"ok": True, "written": len(params["records"])}
            if action == "delete_records":
                self.assertEqual(["rec-main", "rec-dup-1", "rec-dup-2"], params["record_ids"])
                return {"ok": True, "deleted": len(params["record_ids"])}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(_ronghui_params(target_date="2026-05-12"))

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(3, result["deleted"])

    def test_send_order_sync_cleans_duplicates_created_during_write_race(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R001", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        list_call_count = 0

        def _fake_feishu_operation(action, params):
            nonlocal list_call_count
            if action == "list_records":
                list_call_count += 1
                if list_call_count == 1:
                    return {"ok": True, "items": []}
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-new-1", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-new-2", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "write_records":
                self.assertNotIn("record_id", params["records"][0])
                return {"ok": True, "written": len(params["records"])}
            if action == "delete_records":
                self.assertEqual(["rec-new-2"], params["record_ids"])
                return {"ok": True, "deleted": len(params["record_ids"])}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(_ronghui_params(target_date="2026-05-12"))

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["dedup_deleted"])

    def test_send_order_sync_sql_only_skips_feishu(self):
        tms_payload = {
            "ok": True,
            "data": [
                {"运单编号": "R00015275708", "发件日期": "2026-05-12 08:00:00", "件数": "1"},
            ],
        }
        self.send_order_sql_mock.return_value = {
            "ok": True,
            "upserted": 1,
            "updates": 0,
            "creates": 1,
            "deleted_stale": 0,
        }

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.send_order_sync_tool.feishu_operation") as feishu_op,
        ):
            result = send_order_sync_tool.run_send_order_sync(
                _ronghui_params(target_date="2026-05-12", sql_only=True)
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["sql_only"])
        self.assertEqual(1, result["sql_upserted"])
        self.assertEqual(0, result["written"])
        feishu_op.assert_not_called()
        self.send_order_sql_mock.assert_called_once()

    def test_init_waybills_sql_from_feishu_reads_ronghui_and_yunda(self):
        captured: list[tuple[str, list[dict[str, Any]]]] = []

        def _fake_resource(key):
            if key == "phase7.send_order_bitable":
                return {"base_token": "base-rh", "table_id": "table-rh"}
            if key == "phase7.yunda_send_waybills_bitable":
                return {"base_token": "base-yd", "table_id": "table-yd"}
            return None

        def _fake_feishu_operation(action, params):
            self.assertEqual("list_records", action)
            self.assertEqual(200, params["limit"])
            table_id = params["table_id"]
            if table_id == "table-rh":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "rh-1",
                            "fields": {
                                "运单编号": "R00015275708",
                                "发件日期": "2026-05-12 08:00:00",
                                "目的网点": "长沙",
                                "收货人": "张三",
                            },
                        },
                        {
                            "record_id": "rh-2",
                            "fields": {
                                "运单编号": "HR0001",
                                "发件日期": "2026-05-12 09:00:00",
                            },
                        }
                    ],
                }
            if table_id == "table-yd":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "yd-1",
                            "fields": {
                                "运单编号": "978288946",
                                "日期": "2026-05-15",
                                "目的网点": "韵达站点",
                                "收货人": "李四",
                            },
                        }
                    ],
                }
            raise AssertionError(table_id)

        def _fake_sql(records, *, source, **kwargs):
            captured.append((source, records))
            return {"ok": True, "upserted": len(records), "updates": 0, "creates": len(records), "deleted_stale": 0}

        with (
            patch("tools.init_waybills_sql_from_feishu_tool.get_workflow_resource", side_effect=_fake_resource),
            patch("tools.init_waybills_sql_from_feishu_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.init_waybills_sql_from_feishu_tool.sync_console_waybills", side_effect=_fake_sql),
            patch("tools.init_waybills_sql_from_feishu_tool.delete_receipt_like_console_waybills", return_value={"ok": True, "deleted": 0}) as cleanup,
        ):
            result = init_waybills_sql_from_feishu_tool.run_init_waybills_sql_from_feishu({"list_limit": 500})

        self.assertTrue(result["ok"])
        self.assertEqual(3, result["feishu_records"])
        self.assertEqual(1, result["skipped_receipt_like"])
        self.assertEqual(2, result["sql_upserted"])
        self.assertEqual("ronghui", captured[0][0])
        self.assertEqual("R00015275708", captured[0][1][0]["waybill_no"])
        self.assertEqual("yunda", captured[1][0])
        self.assertEqual("978288946", captured[1][1][0]["waybill_no"])
        cleanup.assert_called_once_with(source="ronghui")

    def test_send_order_sync_zero_rows_clears_target_date(self):
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-1", "fields": {"运单编号": "R001", "发件日期": "2026-05-12"}},
                        {"record_id": "rec-2", "fields": {"运单编号": "R002", "发件日期": "2026-05-12"}},
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-1", "rec-2"], params["record_ids"])
                return {"ok": True, "deleted": 2}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", return_value={"ok": True, "data": []}),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(_ronghui_params(target_date="2026-05-12"))

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["fetched"])
        self.assertEqual(2, result["deleted"])
        self.assertNotIn("write_records", actions)

    def test_send_order_sync_supports_date_range(self):
        request_dates: list[str] = []

        def _fake_call_http_service(endpoint, request_body):
            self.assertEqual("/send_order", endpoint)
            target_date = request_body["params"]["target_date"]
            request_dates.append(target_date)
            return {
                "ok": True,
                "data": [
                    {
                        "运单编号": f"R{target_date[-2:]}",
                        "发件日期": f"{target_date} 08:00:00",
                        "件数": "1",
                    }
                ],
            }

        def _fake_feishu_operation(action, params):
            if action == "list_records":
                return {"ok": True, "items": []}
            if action == "write_records":
                return {"ok": True, "written": len(params["records"])}
            raise AssertionError(action)

        with (
            patch("tools.send_order_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(
                _ronghui_params(start_date="2026-05-06", end_date="2026-05-08")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["2026-05-06", "2026-05-07", "2026-05-08"], request_dates)
        self.assertEqual(3, result["days"])
        self.assertEqual(3, result["fetched"])
        self.assertEqual(3, result["creates"])
        self.assertEqual(3, result["written"])

    def test_send_order_sync_dry_run_does_not_write_or_delete(self):
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [{"record_id": "rec-stale", "fields": {"运单编号": "R002", "发件日期": "2026-05-12"}}],
                }
            raise AssertionError(action)

        with (
            patch(
                "tools.send_order_sync_tool.call_http_service",
                return_value={"ok": True, "data": [{"运单编号": "R001", "发件日期": "2026-05-12 08:00:00"}]},
            ),
            patch("tools.send_order_sync_tool.get_workflow_resource", return_value={"base_token": "base", "table_id": "table"}),
            patch("tools.send_order_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = send_order_sync_tool.run_send_order_sync(
                _ronghui_params(target_date="2026-05-12", dry_run=True)
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, result["planned"])
        self.assertEqual(1, result["planned_creates"])
        self.assertEqual(1, result["planned_deletes"])
        self.assertEqual(1, result["planned_sql_upserts"])
        self.assertEqual(["list_records"], actions)
        self.send_order_sql_mock.assert_not_called()

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

    def test_yunda_dispatch_forecast_sync_appends_by_default(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "主单号": "YD001",
                        "开单件数": "3",
                        "扫描件数": "2",
                        "重量/kg": "12.5",
                        "体积/m3": "0.3",
                        "包装类型": "纸箱",
                        "清场时间": "2026-05-10 18:00:00",
                        "规划时效": "24",
                        "开单目的地址": "湖南省邵阳市测试地址",
                        "预计到达时间": "2026-05-11 12:00:00",
                        "应派时间": "2026-05-11",
                    }
                ],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": "文本", "is_primary": True}]
                    + [
                        {"field_name": name}
                        for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES
                        if name != "应派时间"
                    ],
                }
            if action == "create_field":
                return {"ok": True, "field": params["field_name"]}
            if action == "write_records":
                record = params["records"][0]["fields"]
                self.assertEqual("YD001", record["文本"])
                self.assertEqual("YD001", record["主单号"])
                self.assertEqual(3, record["开单件数"])
                self.assertEqual(12.5, record["重量/kg"])
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                _yunda_params(target_date="2026-05-11")
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["append_only"])
        self.assertEqual(0, result["deleted"])
        self.assertEqual(1, result["written"])
        self.assertNotIn("list_records", [action for action, _params in calls])
        self.assertNotIn("delete_records", [action for action, _params in calls])
        create_calls = [params for action, params in calls if action == "create_field"]
        self.assertEqual(["应派时间"], [params["field_name"] for params in create_calls])

    def test_yunda_dispatch_forecast_sync_uses_primary_field_as_main_index(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "主单号": "YD001",
                        "开单件数": "3",
                        "扫描件数": "2",
                        "重量/kg": "12.5",
                        "体积/m3": "0.3",
                        "包装类型": "纸箱",
                        "清场时间": "2026-05-10 18:00:00",
                        "规划时效": "24",
                        "开单目的地址": "湖南省邵阳市测试地址",
                        "预计到达时间": "2026-05-11 12:00:00",
                        "应派时间": "2026-05-11",
                    }
                ],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": "文本", "is_primary": True}]
                    + [
                        {"field_name": name}
                        for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES
                        if name not in {"主单号", "应派时间"}
                    ],
                }
            if action == "create_field":
                return {"ok": True, "field": params["field_name"]}
            if action == "write_records":
                record = params["records"][0]["fields"]
                self.assertEqual("YD001", record["文本"])
                self.assertNotIn("主单号", record)
                self.assertEqual("2026-05-11", record["应派时间"])
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                _yunda_params(target_date="2026-05-11")
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["append_only"])
        self.assertEqual(0, result["deleted"])
        create_calls = [params for action, params in calls if action == "create_field"]
        self.assertEqual(["应派时间"], [params["field_name"] for params in create_calls])

    def test_yunda_dispatch_forecast_sync_surfaces_tms_runtime_error(self):
        tms_payload = {
            "ok": False,
            "error_type": "RuntimeError",
            "error": "韵达派件预测接口返回格式异常: list",
            "http_status": 500,
        }

        with patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                _yunda_params(target_date="2026-05-11")
            )

        self.assertIn("韵达派件预测接口返回格式异常: list", result["error"])
        self.assertNotEqual("yunda_dispatch_forecast 返回格式异常", result["error"])
        self.assertEqual("RuntimeError", result["error_type"])

    def test_yunda_dispatch_forecast_sync_clears_target_date_when_no_rows(self):
        tms_payload = {"ok": True, "data": {"ok": True, "total": 0, "records": []}}
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": name} for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES],
                }
            if action == "list_records":
                raise AssertionError("append mode should not list old records")
            if action == "delete_records":
                raise AssertionError("append mode should not delete old records")
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                _yunda_params(target_date="2026-05-11")
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["append_only"])
        self.assertEqual(0, result["deleted"])
        self.assertEqual(0, result["written"])
        self.assertNotIn("write_records", actions)

    def test_yunda_dispatch_forecast_sync_can_replace_target_date_when_requested(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [{"主单号": "YD001", "开单件数": "3", "应派时间": "2026-05-11"}],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {"ok": True, "items": [{"field_name": name} for name in yunda_dispatch_forecast_sync_tool.FIELD_NAMES]}
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-target", "fields": {"应派时间": "2026-05-11"}},
                        {"record_id": "rec-other", "fields": {"应派时间": "2026-05-12"}},
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-target"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                return {"ok": True, "written": 1}
            raise AssertionError(action)

        with (
            patch("tools.yunda_dispatch_forecast_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_dispatch_forecast_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_dispatch_forecast_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_dispatch_forecast_sync_tool.run_yunda_dispatch_forecast_sync(
                _yunda_params(target_date="2026-05-11", append_only=False)
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["append_only"])
        self.assertEqual(1, result["deleted"])

    def test_yunda_send_waybills_sql_only_updates_console_without_feishu(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "5.14编号": "978SQL001",
                        "日期": "2026-05-15",
                        "目的网点": "测试网点",
                        "收件地址": "测试地址",
                        "寄件人": "勇胜",
                        "收货人": "测试收件人",
                    }
                ],
            },
        }
        http_calls: list[tuple[str, dict[str, Any]]] = []
        sql_calls: list[dict[str, Any]] = []

        def _fake_call_http_service(endpoint, payload):
            http_calls.append((endpoint, payload))
            return tms_payload

        def _fake_sync_console_waybills(records, *, source, target_date, replace_date):
            sql_calls.append(
                {
                    "records": records,
                    "source": source,
                    "target_date": target_date,
                    "replace_date": replace_date,
                }
            )
            return {"ok": True, "upserted": len(records), "updates": 1, "creates": 0, "deleted_stale": 2}

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", side_effect=AssertionError("Feishu resource should not be read")),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=AssertionError("Feishu should not be called")),
            patch("tools.yunda_send_waybills_sync_tool.sync_console_waybills", side_effect=_fake_sync_console_waybills),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(target_date="2026-05-15", sql_only=True)
            )

        self.assertEqual("/yunda_send_waybills", http_calls[0][0])
        self.assertEqual("2026-05-15", http_calls[0][1]["params"]["target_date"])
        self.assertEqual(1, len(sql_calls))
        self.assertEqual("yunda", sql_calls[0]["source"])
        self.assertEqual(date(2026, 5, 15), sql_calls[0]["target_date"])
        self.assertTrue(sql_calls[0]["replace_date"])
        self.assertEqual("978SQL001", sql_calls[0]["records"][0]["waybill_no"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["sql_only"])
        self.assertEqual(1, result["sql_upserted"])
        self.assertEqual(2, result["sql_deleted_stale"])

    def test_yunda_send_waybills_sql_only_range_aggregates_sql_counts(self):
        def _fake_call_http_service(endpoint, payload):
            target_date = payload["params"]["target_date"]
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "total": 1,
                    "records": [{"5.14编号": f"978{target_date[-2:]}", "日期": target_date}],
                },
            }

        def _fake_sync_console_waybills(records, *, source, target_date, replace_date):
            return {"ok": True, "upserted": 1, "updates": 0, "creates": 1, "deleted_stale": int(target_date.day)}

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", side_effect=AssertionError("Feishu resource should not be read")),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=AssertionError("Feishu should not be called")),
            patch("tools.yunda_send_waybills_sync_tool.sync_console_waybills", side_effect=_fake_sync_console_waybills),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(
                    start_date="2026-05-15",
                    end_date="2026-05-16",
                    sql_only=True,
                )
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["sql_only"])
        self.assertEqual(2, result["days"])
        self.assertEqual(2, result["sql_upserted"])
        self.assertEqual(31, result["sql_deleted_stale"])

    def test_yunda_send_waybills_sync_replaces_target_date_with_new_bitable_snapshot(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 2,
                "records": [
                    {
                        "5.14编号": "978284775",
                        "目的网点": "湖南长沙岳麓区梅溪湖公司",
                        "收件区/县": "岳麓区",
                        "收件地址": "湖南省长沙市岳麓区梅溪湖街道金茂梅溪湖29栋3902",
                        "寄件人": "勇胜",
                        "寄件手机": "07315186128",
                        "收货人": "廖芬姣",
                        "收货电话": "18874714321",
                        "货物名称": "透析液",
                        "包装类型": "纸箱:16",
                        "派送方式": "不上楼",
                        "件数": "16",
                        "实际重量": "250.00",
                        "现付": "",
                        "月结": "",
                        "提付": "115.00",
                        "中转运费": "81.85",
                        "回单号": "",
                        "备注": "",
                        "结算重量": "250.00",
                        "体积": "1.0000",
                        "支付类型": "到付",
                        "体积重": "200",
                        "到付款": "115.00",
                    },
                    {
                        "5.14编号": "978281237",
                        "目的网点": "安徽铜陵公司三分部",
                        "收件区/县": "郊区",
                        "收件地址": "安徽省铜陵市郊区铜都大道中段铜南小区",
                        "寄件人": "勇胜",
                        "寄件手机": "07315186128",
                        "收货人": "洪师傅",
                        "收货电话": "15800009716",
                        "货物名称": "吨袋",
                        "包装类型": "编织袋:12",
                        "派送方式": "送货进仓",
                        "件数": "12",
                        "实际重量": "522.00",
                        "现付": "12.00",
                        "月结": "",
                        "提付": "",
                        "中转运费": "257.69",
                        "回单号": "HD001",
                        "备注": "测试备注",
                        "结算重量": "557.90",
                        "体积": "2.7895",
                        "支付类型": "现金",
                        "体积重": "557.90",
                        "到付款": "0.00",
                    },
                ],
            },
        }
        calls: list[tuple[str, dict[str, Any]]] = []
        sheet_calls: list[dict[str, Any]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": "5.14编号", "is_primary": True}]
                    + [{"field_name": name} for name in yunda_send_waybills_sync_tool.FIELD_NAMES if name != "5.14编号"],
                }
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "rec-old",
                            "fields": {"5.14编号": "978284775", "日期": "2026-05-15"},
                        },
                        {
                            "record_id": "rec-other-day",
                            "fields": {"5.14编号": "978284700", "日期": "2026-05-14"},
                        },
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-old"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                records = params["records"]
                self.assertNotIn("record_id", records[0])
                self.assertNotIn("record_id", records[1])
                first_fields = records[0]["fields"]
                self.assertEqual(16, first_fields["件数"])
                self.assertEqual(115, first_fields["提付"])
                self.assertIsNone(first_fields["现付"])
                self.assertIsNone(first_fields["月结"])
                self.assertEqual(81.85, first_fields["中转运费"])
                self.assertEqual(
                    yunda_send_waybills_sync_tool._to_date_timestamp_ms("2026-05-15"),
                    first_fields["日期"],
                )
                second_fields = records[1]["fields"]
                self.assertEqual(12, second_fields["现付"])
                self.assertEqual(257.69, second_fields["中转运费"])
                return {"ok": True, "written": 2}
            if action == "clear_sheet":
                sheet_calls.append(params)
                return {"ok": True}
            if action == "write_sheet":
                sheet_calls.append(params)
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            self.yunda_send_sql_mock.return_value = {"ok": True, "upserted": 2, "updates": 1, "creates": 1, "deleted_stale": 0}
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(target_date="2026-05-15")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(2, result["creates"])
        self.assertEqual(1, result["deleted"])
        self.assertEqual(2, result["written"])
        self.assertEqual(2, result["sql_upserted"])
        self.yunda_send_sql_mock.assert_called_once()
        sql_records = self.yunda_send_sql_mock.call_args.args[0]
        self.assertEqual("978284775", sql_records[0]["waybill_no"])
        self.assertEqual("2026-05-15", sql_records[0]["open_date"])
        self.assertEqual("115.00", sql_records[0]["freight_fee"])
        self.assertEqual("81.85", sql_records[0]["transfer_fee"])
        self.assertEqual("yunda", self.yunda_send_sql_mock.call_args.kwargs["source"])
        self.assertTrue(self.yunda_send_sql_mock.call_args.kwargs["replace_date"])
        self.assertLess(
            [action for action, _params in calls].index("delete_records"),
            [action for action, _params in calls].index("write_records"),
        )
        self.assertEqual(2, result["sheet_rows"])
        self.assertEqual(2, len(sheet_calls))
        self.assertEqual(yunda_send_waybills_sync_tool.DEFAULT_SPREADSHEET_TOKEN, sheet_calls[0]["spreadsheet_token"])
        self.assertEqual("Sheet1!A2:Y5000", sheet_calls[0]["range"])
        self.assertEqual("Sheet1!A2:Y3", sheet_calls[1]["range"])
        self.assertEqual("978284775", sheet_calls[1]["values"][0][0])
        self.assertEqual("2026-05-15", sheet_calls[1]["values"][0][-1])

    def test_yunda_send_waybills_sync_zero_rows_clears_target_date(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 0,
                "records": [],
            },
        }

        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_feishu_operation(action, params):
            calls.append((action, params))
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": name} for name in yunda_send_waybills_sync_tool.FIELD_NAMES],
                }
            if action == "list_records":
                return {
                    "ok": True,
                    "items": [
                        {"record_id": "rec-target", "fields": {"日期": "2026-05-21"}},
                        {"record_id": "rec-other", "fields": {"日期": "2026-05-20"}},
                    ],
                }
            if action == "delete_records":
                self.assertEqual(["rec-target"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            raise AssertionError(action)

        self.yunda_send_sql_mock.return_value = {
            "ok": True,
            "upserted": 0,
            "updates": 0,
            "creates": 0,
            "deleted_stale": 1,
        }

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(target_date="2026-05-21")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["fetched"])
        self.assertEqual(0, result["written"])
        self.assertEqual(0, result["sql_upserted"])
        self.assertEqual(1, result["deleted"])
        self.assertEqual(1, result["sql_deleted_stale"])
        self.assertEqual(0, result["sheet_rows"])
        self.assertTrue(result["sheet_result"]["skipped"])
        self.assertNotIn("write_records", [action for action, _params in calls])
        self.yunda_send_sql_mock.assert_called_once()

    def test_yunda_send_waybills_sync_supports_date_range(self):
        request_dates: list[str] = []
        write_batches: list[list[dict[str, Any]]] = []

        def _fake_call_http_service(endpoint, request_body):
            self.assertEqual("/yunda_send_waybills", endpoint)
            target_date = request_body["params"]["target_date"]
            request_dates.append(target_date)
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "target_date": target_date,
                    "total": 1,
                    "records": [
                        {
                            yunda_send_waybills_sync_tool.INDEX_FIELD_NAME: f"978{target_date[-2:]}",
                            yunda_send_waybills_sync_tool.DATE_FIELD_NAME: target_date,
                            "件数": "1",
                        }
                    ],
                },
            }

        def _fake_feishu_operation(action, params):
            if action == "list_fields":
                return {
                    "ok": True,
                    "items": [{"field_name": yunda_send_waybills_sync_tool.INDEX_FIELD_NAME, "is_primary": True}]
                    + [
                        {"field_name": name}
                        for name in yunda_send_waybills_sync_tool.FIELD_NAMES
                        if name != yunda_send_waybills_sync_tool.INDEX_FIELD_NAME
                    ],
                }
            if action == "list_records":
                return {"ok": True, "items": []}
            if action == "write_records":
                write_batches.append(params["records"])
                return {"ok": True, "written": len(params["records"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", side_effect=_fake_call_http_service),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(start_date="2026-05-06", end_date="2026-05-08")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["2026-05-06", "2026-05-07", "2026-05-08"], request_dates)
        self.assertEqual("2026-05-06", result["start_date"])
        self.assertEqual("2026-05-08", result["end_date"])
        self.assertEqual(3, result["days"])
        self.assertEqual(3, result["fetched"])
        self.assertEqual(3, result["creates"])
        self.assertEqual(3, result["written"])
        self.assertEqual(3, len(write_batches))

    def test_yunda_send_waybills_sync_uses_primary_field_when_index_field_missing(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [{"5.14编号": "978284775", "件数": "16", "支付类型": "到付", "提付": "115.00"}],
            },
        }

        def _fake_feishu_operation(action, params):
            if action == "list_fields":
                return {"ok": True, "items": [{"field_name": "编号", "is_primary": True}, {"field_name": "件数"}, {"field_name": "支付类型"}, {"field_name": "提付"}]}
            if action == "create_field":
                self.assertNotEqual("5.14编号", params["field_name"])
                return {"ok": True, "field": params["field_name"]}
            if action == "list_records":
                return {"ok": True, "items": [{"record_id": "rec-old", "fields": {"编号": "978284775", "日期": "2026-05-15"}}]}
            if action == "delete_records":
                self.assertEqual(["rec-old"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                record = params["records"][0]
                self.assertNotIn("record_id", record)
                self.assertEqual("978284775", record["fields"]["编号"])
                self.assertNotIn("5.14编号", record["fields"])
                return {"ok": True, "written": 1}
            if action == "clear_sheet":
                return {"ok": True}
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(target_date="2026-05-15")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["deleted"])

    def test_yunda_send_waybills_sync_maps_waybill_number_primary_when_ensure_fields_disabled(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [
                    {
                        "5.14编号": "978284775",
                        "件数": "16",
                        "支付类型": "到付",
                        "提付": "115.00",
                    }
                ],
            },
        }
        actions: list[str] = []

        def _fake_feishu_operation(action, params):
            actions.append(action)
            if action == "list_fields":
                return {"ok": True, "items": [{"field_name": "运单编号", "is_primary": True}, {"field_name": "件数"}]}
            if action == "list_records":
                return {"ok": True, "items": [{"record_id": "rec-old", "fields": {"运单编号": "978284775", "日期": "2026-05-15"}}]}
            if action == "delete_records":
                self.assertEqual(["rec-old"], params["record_ids"])
                return {"ok": True, "deleted": 1}
            if action == "write_records":
                record = params["records"][0]
                self.assertNotIn("record_id", record)
                self.assertEqual({"运单编号": "978284775", "件数": 16}, record["fields"])
                return {"ok": True, "written": 1}
            if action == "clear_sheet":
                return {"ok": True}
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.get_workflow_resource", return_value=None),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation", side_effect=_fake_feishu_operation),
            patch("tools.phase7_sync_common.feishu_operation", side_effect=_fake_feishu_operation),
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(target_date="2026-05-15", ensure_fields=False)
            )

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["updates"])
        self.assertEqual(1, result["creates"])
        self.assertEqual(1, result["deleted"])
        self.assertNotIn("create_field", actions)

    def test_yunda_send_waybills_sync_dry_run_does_not_call_feishu(self):
        tms_payload = {
            "ok": True,
            "data": {
                "ok": True,
                "total": 1,
                "records": [{"5.14编号": "978284775", "件数": "16", "支付类型": "到付", "提付": "115.00"}],
            },
        }

        with (
            patch("tools.yunda_send_waybills_sync_tool.call_http_service", return_value=tms_payload),
            patch("tools.yunda_send_waybills_sync_tool.feishu_operation") as feishu_operation_mock,
        ):
            result = yunda_send_waybills_sync_tool.run_yunda_send_waybills_sync(
                _yunda_params(target_date="2026-05-15", dry_run=True)
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, result["planned"])
        self.assertEqual(1, result["planned_sql_upserts"])
        self.assertEqual(1, result["planned_sheet_rows"])
        feishu_operation_mock.assert_not_called()
        self.yunda_send_sql_mock.assert_not_called()

    def test_arrival_stats_sync_propagates_auth_required_error_code(self):
        auth_payload = {
            "ok": False,
            "error_code": "AUTH_REQUIRED",
            "error": "当前未登录或登录态已过期。",
        }
        with patch("tools.arrival_stats_sync_tool.call_http_service", return_value=auth_payload):
            result = arrival_stats_sync_tool.run_arrival_stats_sync(
                {"account_id": "ronghui-test"}
            )
        self.assertEqual("AUTH_REQUIRED", result.get("error_code"))

    def test_arrive_list_sync_uses_dispatch_forecast_rows_without_detail_query(self):
        captured_request = {}

        def fake_call_http_service(endpoint, request_body):
            if endpoint == "/fetch_dispatch":
                captured_request.update(request_body)
                return {
                    "data": [
                        {"BILL_CODE": "H2003441275"},
                        {
                            "BILL_CODE": "R00014652502",
                            "GOODS_NAME": "测试货物",
                            "R_BILLCODE": "H2003441275",
                            "PIECE_NUMBER": 2,
                        }
                    ]
                }
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        with (
            patch("tools.arrive_list_sync_tool.call_http_service", side_effect=fake_call_http_service),
            patch("tools.arrive_list_sync_tool.replace_waybill_records", return_value={"ok": True, "replaced": 1}) as replace_records,
            patch("tools.arrive_list_sync_tool._write_sheet_resource", return_value={"ok": True, "rows": 1}) as write_sheet,
            patch(
                "tools.arrive_list_sync_tool.save_forecast_snapshot",
                return_value={"ok": True, "run_id": "forecast-run", "rows": 1},
            ),
        ):
            result = arrive_list_sync_tool.run_arrive_list_sync(
                {"account_id": "ronghui-test"}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["skipped_receipt_like"])
        self.assertEqual(1, result["bill_codes"])
        self.assertEqual(1, result["detail_records"])
        self.assertEqual("fetch_dispatch", result["source"])
        self.assertEqual("ronghui-test", captured_request["params"]["account_id"])
        records = replace_records.call_args.args[0]
        self.assertEqual(["R00014652502"], [record["tracking_number"] for record in records])
        self.assertEqual("测试货物", records[0]["goods_name"])
        self.assertEqual(2, records[0]["quantity"])
        sheet_rows = write_sheet.call_args_list[0].args[1]
        self.assertEqual("R00014652502", sheet_rows[0][0])
        self.assertEqual("H2003441275", sheet_rows[0][5])

    def test_arrive_list_sync_passes_target_date_to_dispatch(self):
        captured_request = {}

        def fake_call_http_service(endpoint, request_body):
            self.assertEqual("/fetch_dispatch", endpoint)
            captured_request.update(request_body)
            return {"data": []}

        with (
            patch("tools.arrive_list_sync_tool.call_http_service", side_effect=fake_call_http_service),
            patch("tools.arrive_list_sync_tool._write_sheet_resource", return_value={"ok": True, "rows": 0}),
        ):
            result = arrive_list_sync_tool.run_arrive_list_sync(
                {
                    "account_id": "ronghui-test",
                    "target_date": "2026-05-04",
                    "dry_run": True,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual("2026-05-04", captured_request["params"]["target_date"])
        self.assertEqual("ronghui-test", captured_request["params"]["account_id"])
        self.assertEqual("05.04运单编号", arrive_list_sync_tool._build_title({"target_date": "2026-05-04"})[0])

    def test_arrive_list_sync_requires_control_plane_account(self):
        with patch("tools.arrive_list_sync_tool.call_http_service") as source_mock:
            result = arrive_list_sync_tool.run_arrive_list_sync({})

        self.assertEqual("forecast_validation_failed", result["stage"])
        self.assertIn("account_id", result["error"])
        source_mock.assert_not_called()

    def test_arrival_tools_reject_nested_account_mismatch(self):
        with self.assertRaisesRegex(ValueError, "账号与控制平面批准"):
            arrive_list_sync_tool._build_dispatch_request(
                {
                    "account_id": "approved-account",
                    "request_body": {"params": {"accountId": "different-account"}},
                }
            )
        with self.assertRaisesRegex(ValueError, "账号与控制平面批准"):
            arrival_stats_sync_tool._bind_account_id(
                {"account_id": "different-account"},
                "approved-account",
                label="扫描请求",
            )

    def test_scan_sync_handles_malformed_fetch_response(self):
        with patch("tools.scan_sync_tool.call_http_service", return_value={"unexpected": True}):
            result = scan_sync_tool.run_scan_sync({"account_id": "ronghui_default"})
        self.assertIn("get_scan 返回格式异常", result["error"])

    def test_scan_sync_passes_target_date_and_dry_run_does_not_write_or_scan(self):
        rows = [{"source": "get_scan"}]
        normalized_rows = [
            {"raw_code": "R00010001", "destination": "测试站", "code_type": "child"}
        ]
        child_items = [{"bill_code": "R00010001", "station_name": "测试站"}]
        with (
            patch("tools.scan_sync_tool.call_http_service", return_value={"data": rows}) as call_http,
            patch("tools.scan_sync_tool.normalize_scan_rows", return_value=normalized_rows),
            patch("tools.scan_sync_tool.child_items_from_scan_rows", return_value=child_items),
            patch("tools.scan_sync_tool.replace_scan_codes") as replace_scan_codes,
        ):
            result = scan_sync_tool.run_scan_sync(
                {
                    "target_date": "2026-08-12",
                    "dry_run": True,
                    "account_id": "ronghui_default",
                }
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(1, call_http.call_count)
        self.assertEqual("/get_scan", call_http.call_args.args[0])
        self.assertEqual("2026/08/12", call_http.call_args.args[1]["params"]["date"])
        self.assertEqual(
            "ronghui_default",
            call_http.call_args.args[1]["params"]["account_id"],
        )
        replace_scan_codes.assert_not_called()

    def test_scan_sync_rejects_conflicting_target_date_params(self):
        with self.assertRaisesRegex(ValueError, "target_date 不能与"):
            scan_sync_tool._resolve_get_scan_request_params(
                {"target_date": "2026-08-12", "account_id": "ronghui_default"},
                {"params": {"date": "2026/08/13"}},
            )

    def test_scan_sync_stops_after_first_failed_batch_and_preserves_nested_error(self):
        rows = [{"source": "get_scan"}]
        normalized_rows = [
            {"raw_code": "R00010001", "destination": "测试站", "code_type": "child"}
        ]
        child_items = [
            {"bill_code": "R00010001", "station_name": "测试站"},
            {"bill_code": "R00010002", "station_name": "测试站"},
        ]
        scan_failure = {
            "ok": False,
            "data": {
                "ok": False,
                "stage": "upload",
                "message": 'value too large for column "SCAN_MAN_CODE"',
            },
        }
        with (
            patch(
                "tools.scan_sync_tool.call_http_service",
                side_effect=[{"data": rows}, scan_failure],
            ) as call_http,
            patch("tools.scan_sync_tool.normalize_scan_rows", return_value=normalized_rows),
            patch("tools.scan_sync_tool.child_items_from_scan_rows", return_value=child_items),
            patch(
                "tools.scan_sync_tool.replace_scan_codes",
                return_value={"ok": True, "replaced": 1},
            ),
            patch("tools.scan_sync_tool._trigger_scan_flow") as trigger_flow,
        ):
            result = scan_sync_tool.run_scan_sync({"batch_size": 1, "trigger_flow": True, "account_id": "ronghui_default"})

        self.assertFalse(result["ok"])
        self.assertEqual("SCAN_NEXT_BATCH_FAILED", result["error_code"])
        self.assertEqual(1, result["failed_batch"])
        self.assertIn("SCAN_MAN_CODE", result["error"])
        self.assertEqual(2, call_http.call_count)
        self.assertEqual(1, len(result["batch_results"]))
        self.assertEqual("batch_failed", result["flow_result"]["reason"])
        trigger_flow.assert_not_called()

    def test_scan_sync_reports_explicit_batch_limits(self):
        rows = [{"source": "get_scan"}]
        normalized_rows = [
            {"raw_code": "R00010001", "destination": "测试站", "code_type": "child"}
        ]
        child_items = [
            {"bill_code": f"R0001000{index}", "station_name": "测试站"}
            for index in range(1, 4)
        ]
        with (
            patch(
                "tools.scan_sync_tool.call_http_service",
                side_effect=[{"data": rows}, {"ok": True, "detail": []}],
            ),
            patch("tools.scan_sync_tool.normalize_scan_rows", return_value=normalized_rows),
            patch("tools.scan_sync_tool.child_items_from_scan_rows", return_value=child_items),
            patch(
                "tools.scan_sync_tool.replace_scan_codes",
                return_value={"ok": True, "replaced": 1},
            ),
        ):
            result = scan_sync_tool.run_scan_sync({"batch_size": 1, "max_batches": 1, "account_id": "ronghui_default"})

        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(3, result["candidate_items"])
        self.assertEqual(1, result["scheduled_items"])
        self.assertEqual(2, result["omitted_items"])

    def test_scan_sync_rejects_non_positive_limits(self):
        with self.assertRaisesRegex(ValueError, "batch_size 必须大于 0"):
            scan_sync_tool._chunk([], 0)

    def test_scan_sync_passes_optional_target_date_to_get_scan(self):
        captured_request = {}

        def fake_call_http_service(endpoint, request_body):
            self.assertEqual("/get_scan", endpoint)
            captured_request.update(request_body)
            return {"data": []}

        with patch(
            "tools.scan_sync_tool.call_http_service",
            side_effect=fake_call_http_service,
        ):
            result = scan_sync_tool.run_scan_sync(
                {"target_date": "2026-05-04", "dry_run": True, "account_id": "ronghui_default"}
            )

        self.assertTrue(result["ok"])
        self.assertEqual("2026/05/04", captured_request["params"]["date"])

    def test_scan_sync_omits_empty_target_date_for_today_default(self):
        captured_request = {}

        def fake_call_http_service(endpoint, request_body):
            self.assertEqual("/get_scan", endpoint)
            captured_request.update(request_body)
            return {"data": []}

        with patch(
            "tools.scan_sync_tool.call_http_service",
            side_effect=fake_call_http_service,
        ):
            result = scan_sync_tool.run_scan_sync({"target_date": "", "dry_run": True, "account_id": "ronghui_default"})

        self.assertTrue(result["ok"])
        self.assertNotIn("date", captured_request["params"])

    def test_scan_sync_rejects_conflicting_date_sources(self):
        with self.assertRaisesRegex(ValueError, "不能与 request_body.params"):
            scan_sync_tool.run_scan_sync(
                {
                    "target_date": "2026-05-04",
                    "request_body": {"params": {"date": "2026/05/03"}},
                    "dry_run": True,
                    "account_id": "ronghui_default",
                }
            )

    def test_arrival_stats_sync_handles_malformed_fetch_response(self):
        with patch("tools.arrival_stats_sync_tool.call_http_service", return_value={"unexpected": True}):
            with self.assertRaises(ValueError):
                arrival_stats_sync_tool.run_arrival_stats_sync({})

    def test_daily_sign_request_does_not_load_legacy_r13_resource(self):
        with patch("tools.daily_sign_sync_tool.get_workflow_resource") as resource_mock:
            request_body = daily_sign_sync_tool.build_daily_sign_request_body(
                {"request_body": {"days": 1}}
            )

        self.assertEqual({"days": 1}, request_body)
        resource_mock.assert_not_called()

    def test_daily_sign_sync_prefers_r13_account_manager_credentials(self):
        class FakeAccountManager:
            def resolve_role_account_params(self, params, **kwargs):
                self.kwargs = kwargs
                result = dict(params)
                result["username"] = "r13-account-user"
                result["password"] = "r13-account-pass"
                return result

        fake_manager = FakeAccountManager()
        with (
            patch("tools.daily_sign_sync_tool.get_account_manager", return_value=fake_manager),
            patch("tools.daily_sign_sync_tool.get_workflow_resource") as resource_mock,
        ):
            request_body = daily_sign_sync_tool.build_daily_sign_request_body(
                {
                    "r13_account_id": "r13_default",
                    "request_body": {"days": 1},
                }
            )

        self.assertEqual("r13_account_id", fake_manager.kwargs["account_field"])
        self.assertEqual("", fake_manager.kwargs["output_account_field"])
        self.assertEqual("", fake_manager.kwargs["output_session_profile_field"])
        self.assertEqual("r13-account-user", request_body["username"])
        self.assertEqual("r13-account-pass", request_body["password"])
        resource_mock.assert_not_called()

    def test_daily_sign_request_rejects_inline_credentials_and_account_selectors(self):
        for forbidden in ("username", "password", "account_id", "r13_account_id"):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                daily_sign_sync_tool.build_daily_sign_request_body(
                    {"request_body": {"days": 1, forbidden: "caller-controlled"}}
                )

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
                "tools.daily_sign_sync_tool.start_sync_run",
                return_value=("source-run", datetime(2026, 8, 13, 9, 0, 0)),
            ),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch("tools.daily_sign_pipeline.finish_sync_run") as finish_mock,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=_daily_completed_run_readback_proof,
            ) as verify_completed,
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
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
            patch("tools.daily_sign_sync_tool.call_http_service") as source_mock,
            patch("tools.daily_sign_sync_tool.start_sync_run") as run_mock,
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
            }
        ]
        with (
            patch(
                "tools.daily_sign_sync_tool.start_sync_run",
                return_value=("source-run", observed_at),
            ),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value={"data": r13_rows},
            ),
            patch(
                "tools.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_problem_events",
                return_value=([], {"rows": 0, "declared_total": 0, "complete": True}),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_sign_events",
                return_value=([], {"source_rows": 0, "complete": True}),
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_r13_sign_conflicts",
                return_value=([], {"complete": True, "queried": 0}),
            ) as exact_mock,
            patch(
                "tools.daily_sign_sync_tool._sync_historical_sign_verifications",
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
                "tools.daily_sign_sync_tool._enrich_missing_addresses",
                side_effect=lambda rows, _params: (rows, {"ok": True, "updated": 0}),
            ),
            patch(
                "tools.daily_sign_sync_tool.persist_daily_sign_snapshot",
                side_effect=_daily_persisted_snapshot_proof,
            ) as persist_mock,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_persistence",
                side_effect=_daily_persistence_readback_proof,
            ) as verify_persistence,
            patch(
                "tools.daily_sign_sync_tool._sync_bitable",
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
                "tools.daily_sign_sync_tool._sync_sheet",
                side_effect=lambda rows, _params: {
                    "ok": True,
                    "rows": len(rows),
                    "readback": _daily_projection_readback_proof(
                        rows,
                        digest_char="s",
                    ),
                },
            ),
            patch("tools.daily_sign_sync_tool.finish_sync_run") as finish_mock,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_completed_run",
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
        exact_mock.assert_called_once_with(
            ANY,
            {"R1": r13_rows[0]},
            ANY,
            persist=False,
        )
        historical_mock.assert_called_once_with(
            ANY,
            {"R1": r13_rows[0]},
            ANY,
            observed_at=observed_at,
            persist=False,
        )
        self.assertFalse(finish_mock.call_args.args[1]["degraded"])

    def test_daily_sign_sync_rejects_incomplete_exact_history_without_projection(self):
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
        with (
            patch(
                "tools.daily_sign_sync_tool.start_sync_run",
                return_value=("source-run", observed_at),
            ),
            patch("tools.daily_sign_sync_tool.load_daily_sign_state", return_value=state),
            patch(
                "tools.daily_sign_pipeline._resolve_r13_request",
                return_value={"days": 1, "fetch_all": True, "page": 1},
            ),
            patch(
                "tools.daily_sign_sync_tool.call_http_service",
                return_value={"data": []},
            ),
            patch(
                "tools.daily_sign_pipeline._source_query_window",
                return_value=(observed_at, observed_at),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_problem_events",
                return_value=([], {"rows": 0, "declared_total": 0, "complete": True}),
            ),
            patch(
                "tools.daily_sign_pipeline._collect_sign_events",
                return_value=([], {"source_rows": 0, "complete": True}),
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_r13_sign_conflicts",
                return_value=([], {"complete": True, "queried": 0}),
            ),
            patch(
                "tools.daily_sign_sync_tool._sync_historical_sign_verifications",
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
                "tools.daily_sign_pipeline._finish_failed_run",
                side_effect=_daily_failed_run_values,
            ) as failed_run_mock,
            patch(
                "tools.daily_sign_sync_tool.verify_daily_sign_completed_run",
                side_effect=_daily_completed_run_readback_proof,
            ) as verify_completed,
            patch("tools.daily_sign_sync_tool.persist_daily_sign_snapshot") as persist_mock,
            patch("tools.daily_sign_sync_tool._sync_bitable") as bitable_mock,
            patch("tools.daily_sign_sync_tool._sync_sheet") as sheet_mock,
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
        self.assertTrue(result["error"]["retryable"])
        failed_run_mock.assert_called_once()
        verify_completed.assert_called_once()
        persist_mock.assert_not_called()
        bitable_mock.assert_not_called()
        sheet_mock.assert_not_called()

    def test_site_send_list_sync_zero_rows_clears_targets(self):
        bitable_result = {"ok": True, "deleted": 3, "written": 0}
        sheet_result = {
            "ok": True,
            "rows": 0,
            "clear_result": {"ok": True},
            "write_result": {"ok": True, "skipped": True, "rows": 0},
        }
        with (
            patch("tools.site_send_list_sync_tool.call_http_service", return_value={"ok": True, "data": []}),
            patch("tools.site_send_list_sync_tool.sync_bitable_snapshot", return_value=bitable_result) as bitable_mock,
            patch("tools.site_send_list_sync_tool.sync_sheet_snapshot", return_value=sheet_result) as sheet_mock,
        ):
            result = site_send_list_sync_tool.run_site_send_list_sync(_ronghui_params())

        self.assertTrue(result["ok"])
        self.assertEqual(0, result["fetched"])
        self.assertNotIn("skip_reason", result)
        self.assertEqual(bitable_result, result["bitable_result"])
        self.assertEqual(sheet_result, result["sheet_result"])
        bitable_mock.assert_called_once_with(
            "phase7.site_send_bitable",
            [],
            {"account_id": "configured-ronghui-test"},
        )
        sheet_mock.assert_called_once_with(
            "phase7.site_send_sheet",
            [],
            {"account_id": "configured-ronghui-test"},
        )

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
                "tools.daily_sign_sync_tool.get_waybill_tracking_cache",
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
                "tools.daily_sign_sync_tool.call_http_service",
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

    def test_fresh_sheet_metadata_bypasses_warm_cache_and_rejects_invalid_refresh(self):
        token = "sheet-fresh-metadata-token"
        feishu_cli_tool._SHEET_REF_CACHE[token] = {"Data": "stale-sheet"}
        feishu_cli_tool._SHEET_INFO_CACHE[token] = {
            "Data": {"sheet_id": "stale-sheet", "title": "Data", "row_count": 99}
        }
        feishu_cli_tool._SHEET_TITLE_COUNTS_CACHE[token] = {"Data": 1}
        calls: list[str] = []

        def unavailable(method, path, payload=None, timeout=30):
            del payload, timeout
            calls.append(method)
            self.assertTrue(path.endswith("/sheets/query"))
            return {"error": "metadata unavailable"}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=unavailable):
            with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
                feishu_cli_tool._spreadsheet_sheet_ref_map(
                    token,
                    require_fresh_metadata=True,
                )

        self.assertEqual(["GET"], calls)
        self.assertEqual("stale-sheet", feishu_cli_tool._SHEET_REF_CACHE[token]["Data"])

        calls.clear()

        def invalid(method, path, payload=None, timeout=30):
            del payload, timeout
            calls.append(method)
            self.assertTrue(path.endswith("/sheets/query"))
            return {"code": 0, "data": {"sheets": []}}

        with patch("tools.feishu_cli_tool._call_open_api", side_effect=invalid):
            with self.assertRaisesRegex(RuntimeError, "metadata response is empty"):
                feishu_cli_tool._spreadsheet_sheet_ref_map(
                    token,
                    require_fresh_metadata=True,
                )

        self.assertEqual(["GET"], calls)
        self.assertEqual("stale-sheet", feishu_cli_tool._SHEET_REF_CACHE[token]["Data"])

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

    def test_feishu_clear_sheet_caps_end_row_to_sheet_row_count(self):
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
                                "sheet_id": "abc123",
                                "title": "Sheet1",
                                "grid_properties": {"row_count": 200},
                            },
                        ],
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
        self.assertEqual("abc123!A2:Y200", result["range"])
        self.assertEqual("DELETE", calls[1][0])
        self.assertEqual(2, calls[1][2]["dimension"]["startIndex"])
        self.assertEqual(200, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_feishu_clear_sheet_caps_end_row_from_camel_case_grid_properties(self):
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
                                "sheet_id": "Sheet1",
                                "title": "Sheet1",
                                "gridProperties": {"rowCount": 10},
                            },
                        ],
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
        self.assertEqual("Sheet1!A2:Y10", result["range"])
        self.assertEqual(10, calls[1][2]["dimension"]["endIndex"])
        self.assertEqual(2, len(calls))

    def test_sheet_snapshot_deletes_then_adds_rows_before_writing(self):
        feishu_cli_tool._SHEET_REF_CACHE.clear()
        feishu_cli_tool._SHEET_INFO_CACHE.clear()
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:Y10",
            "clear_range": "Sheet1!A2:Y5000",
        }
        calls = []
        query_count = 0

        def fake_call_open_api(method, path, payload=None, timeout=30):
            nonlocal query_count
            calls.append((method, path, payload))
            if path.endswith("/sheets/query"):
                query_count += 1
                return {
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "4103ec",
                                "title": "Sheet1",
                                "gridProperties": {"rowCount": 5 if query_count == 1 else 1},
                            }
                        ]
                    },
                }
            return {"code": 0, "data": {}}

        values = [[f"r{row}c{col}" for col in range(25)] for row in range(9)]
        with (
            patch("tools.phase7_sync_common.get_workflow_resource", return_value=resource),
            patch("tools.feishu_cli_tool._call_open_api", side_effect=fake_call_open_api),
        ):
            result = phase7_sync_common.sync_sheet_snapshot("phase7.yunda_send_waybills_sheet", values, {})

        self.assertTrue(result["ok"])
        self.assertEqual(("DELETE", "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range"), calls[1][:2])
        self.assertEqual(("POST", "/open-apis/sheets/v2/spreadsheets/sheet-token/dimension_range"), calls[3][:2])
        self.assertEqual({"sheetId": "4103ec", "majorDimension": "ROWS", "length": 9}, calls[3][2]["dimension"])
        self.assertEqual(("PUT", "/open-apis/sheets/v2/spreadsheets/sheet-token/values"), calls[4][:2])

    def test_feishu_clear_sheet_skips_when_range_starts_after_row_count(self):
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
                                "sheet_id": "abc123",
                                "title": "Sheet1",
                                "grid_properties": {"row_count": 1},
                            },
                        ],
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
        self.assertTrue(result["skipped"])
        self.assertEqual("abc123!A2:Y5000", result["range"])
        self.assertEqual(1, len(calls))
