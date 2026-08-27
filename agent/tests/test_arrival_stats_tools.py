"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403
from tools import daily_sign_store


class ArrivalStatsToolTests(unittest.TestCase):
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
        self.completed_arrivals_patch = patch(
            "tools.arrival_stats_sync_tool.load_completed_arrival_trackings_before",
            return_value=(
                set(),
                {
                    "ok": True,
                    "source": "arrival_stat_active_snapshots",
                    "target_date": "2026-08-13",
                    "prior_successful_dates": 0,
                    "completed_tracking_numbers": 0,
                },
            ),
        )
        self.internal_token_patch.start()
        self.send_order_sql_mock = self.send_order_sql_patch.start()
        self.addCleanup(self.internal_token_patch.stop)
        self.yunda_send_sql_mock = self.yunda_send_sql_patch.start()
        self.delivery_status_sql_mock = self.delivery_status_sql_patch.start()
        self.completed_arrivals_mock = self.completed_arrivals_patch.start()
        self.addCleanup(self.send_order_sql_patch.stop)
        self.addCleanup(self.yunda_send_sql_patch.stop)
        self.addCleanup(self.delivery_status_sql_patch.stop)
        self.addCleanup(self.completed_arrivals_patch.stop)

    def test_pre_arrive_site_code_falls_back_to_default(self):
        session = _DummySession(_DummyResponse(status_code=200, text="<html></html>"))
        with patch.dict(fetch_pre_arrive_list.os.environ, {}, clear=True):
            site_code, source = fetch_pre_arrive_list._resolve_site_code_http(
                session,
                {},
                timeout_sec=1,
            )

        self.assertEqual("7390004", site_code)
        self.assertEqual("default", source)

    def test_arrive_list_sheet_result_summary_does_not_expose_tokens(self):
        result = arrive_list_sync_tool._summarize_feishu_result(
            {
                "ok": True,
                "identity": "bot",
                "data": {
                    "spreadsheetToken": "sensitive-token",
                    "updatedCells": 18,
                    "updatedRows": 1,
                    "updatedColumns": 18,
                    "updatedRange": "Sheet1!A1:R1",
                },
            }
        )

        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sensitive-token", dumped)
        self.assertNotIn("spreadsheetToken", dumped)
        self.assertEqual(18, result["updatedCells"])

    def test_arrival_stats_counts_from_scan_index_for_all_trackings(self):
        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            [
                {"raw_code": "R000143402890001", "destination": "demo", "code_type": "child"},
                {"raw_code": "R000143402890002", "destination": "demo", "code_type": "child"},
                {"raw_code": "2001513259", "destination": "demo", "code_type": "main"},
            ],
            [
                {"tracking_number": "R00014340289", "quantity": 2},
                {"tracking_number": "2001513259", "quantity": 20},
                {"tracking_number": "2003503200", "quantity": 5},
            ],
            ["R00014340289", "2001513259", "2003503200"],
        )

        self.assertEqual(2, count_map["R00014340289"])
        self.assertEqual(0, count_map["2001513259"])
        self.assertEqual(0, count_map["2003503200"])
        self.assertEqual("scan_index", result["source"])
        self.assertEqual(3, result["counted"])
        self.assertEqual(1, result["arrived_nonzero"])
        self.assertEqual(0, result["quantity_adjustments"])
        self.assertEqual(1, result["quantity_gaps"])

    def test_arrival_stats_refreshes_existing_masked_waybill_records(self):
        tracking_numbers, plan = arrival_stats_sync_tool._detail_tracking_numbers(
            [
                {
                    "tracking_number": "R0001",
                    "goods_name": "配件",
                    "recipient_name": "张三",
                    "recipient_phone": "13800000000",
                    "recipient_address": "湖南省邵阳市大祥区测试路1号",
                    "destination_station": "邵阳大祥S站",
                },
                {
                    "tracking_number": "R0002",
                    "goods_name": "大米",
                    "recipient_name": "李*",
                    "recipient_phone": "158****7398",
                    "recipient_address": "湖南省邵阳市大祥区测试路2号",
                    "destination_station": "邵阳大祥S站",
                },
            ],
            ["R0003", "R0002"],
            {},
        )

        self.assertEqual(["R0003", "R0002"], tracking_numbers)
        self.assertEqual({"missing": 2, "stale": 1, "total": 2}, plan)

    def test_arrival_stats_adds_current_scan_missing_main_trackings(self):
        existing_records = [
            {
                "tracking_number": "R00014600001",
                "goods_name": "货物1",
                "quantity": 1,
                "recipient_name": "张三",
                "recipient_phone": "13800000000",
                "recipient_address": "湖南省邵阳市大祥区测试路1号",
                "destination_station": "邵阳",
            },
            {
                "tracking_number": "R00014600002",
                "goods_name": "货物2",
                "quantity": 1,
                "recipient_name": "李四",
                "recipient_phone": "13900000000",
                "recipient_address": "湖南省邵阳市大祥区测试路2号",
                "destination_station": "邵阳",
            },
        ]
        current_scan_rows = [
            {"raw_code": "R00014600001", "destination": "邵阳", "code_type": "main"},
            {"raw_code": "R00014600002", "destination": "邵阳", "code_type": "main"},
            {"raw_code": "R00014600003", "destination": "邵阳", "code_type": "main"},
            {"raw_code": "R000146000040001", "destination": "邵阳", "code_type": "child"},
            {"raw_code": "R000146000050001", "destination": "邵阳", "code_type": "child"},
        ]
        fetched_records = [
            {"tracking_number": "R00014600003", "goods_name": "补抓3", "quantity": 1, "destination_station": "邵阳"},
            {"tracking_number": "R00014600004", "goods_name": "补抓4", "quantity": 1, "destination_station": "邵阳"},
            {"tracking_number": "R00014600005", "goods_name": "补抓5", "quantity": 1, "destination_station": "邵阳"},
        ]
        written_values = []

        def fake_write_stats(resource_key, values, params):
            written_values.append(values)
            return {"ok": True, "rows": len(values)}

        with (
            patch(
                "tools.arrival_stats_sync_tool.fetch_arrive_list_records",
                return_value=(existing_records, {"ok": True, "source": "fetch_dispatch", "bill_codes": 2}),
            ),
            patch("tools.arrival_stats_sync_tool._refresh_scan_index", return_value=(current_scan_rows, {"ok": True})),
            patch("tools.arrival_stats_sync_tool.list_scan_codes", return_value=current_scan_rows),
            patch(
                "tools.arrival_stats_sync_tool._fetch_waybill_details",
                return_value=(fetched_records, {"ok": True, "requested": 3, "fetched": 3}),
            ) as fetch_details,
            patch("tools.arrival_stats_sync_tool._write_stats_sheet", side_effect=fake_write_stats),
        ):
            result = arrival_stats_sync_tool.run_arrival_stats_sync(
                {
                    "dry_run": True,
                    "archive_snapshot": False,
                    "pending_sheet_disabled": True,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["R00014600003", "R00014600004", "R00014600005"], fetch_details.call_args.args[0])
        self.assertEqual(5, result["records"])
        self.assertEqual("dry_run", result["split_pending_result"]["reason"])
        self.assertEqual(5, result["split_pending_result"]["source_rows"])
        self.assertEqual(6, len(written_values[0]))
        self.assertEqual(
            ["R00014600001", "R00014600002", "R00014600003", "R00014600004", "R00014600005"],
            [row[0] for row in written_values[0][1:]],
        )

    def test_arrival_stats_uses_today_arrive_list_union_today_scans(self):
        arrive_records = [
            {
                "tracking_number": "R00020000001",
                "goods_name": "未扫描货物",
                "quantity": 3,
                "recipient_name": "收件人甲",
                "recipient_phone": "13800000001",
                "recipient_address": "湖南省邵阳市测试地址1号",
                "destination_station": "邵阳",
            },
            {
                "tracking_number": "R00020000002",
                "goods_name": "已扫描货物",
                "quantity": 2,
                "recipient_name": "收件人乙",
                "recipient_phone": "13800000002",
                "recipient_address": "湖南省邵阳市测试地址2号",
                "destination_station": "邵阳",
            },
        ]
        current_scan_rows = [
            {"raw_code": "R000200000020002", "destination": "邵阳", "code_type": "child"},
            {"raw_code": "R000200000030001", "destination": "邵阳", "code_type": "child"},
        ]
        accumulated_scan_rows = [
            {"raw_code": "R000199999990001", "destination": "邵阳", "code_type": "child"},
            {"raw_code": "R000200000020001", "destination": "邵阳", "code_type": "child"},
            *current_scan_rows,
        ]
        fetched_records = [
            {
                "tracking_number": "R00020000003",
                "goods_name": "扫描补入货物",
                "quantity": 1,
                "recipient_name": "收件人丙",
                "recipient_phone": "13800000003",
                "recipient_address": "湖南省邵阳市测试地址3号",
                "destination_station": "邵阳",
            }
        ]
        written_values = []

        def fake_write_stats(resource_key, values, params):
            written_values.append(values)
            return {"ok": True, "rows": len(values)}

        with (
            patch(
                "tools.arrival_stats_sync_tool.fetch_arrive_list_records",
                return_value=(arrive_records, {"ok": True, "source": "fetch_dispatch", "bill_codes": 2}),
            ),
            patch("tools.arrival_stats_sync_tool._refresh_scan_index", return_value=(current_scan_rows, {"ok": True})),
            patch("tools.arrival_stats_sync_tool.list_scan_codes", return_value=accumulated_scan_rows),
            patch(
                "tools.arrival_stats_sync_tool._fetch_waybill_details",
                return_value=(fetched_records, {"ok": True, "requested": 1, "fetched": 1}),
            ) as fetch_details,
            patch("tools.arrival_stats_sync_tool._write_stats_sheet", side_effect=fake_write_stats),
        ):
            result = arrival_stats_sync_tool.run_arrival_stats_sync(
                {"dry_run": True, "archive_snapshot": False, "pending_sheet_disabled": True}
            )

        self.assertTrue(result["ok"])
        self.assertEqual(["R00020000003"], fetch_details.call_args.args[0])
        rows_by_tracking = {row[0]: row for row in written_values[0][1:]}
        self.assertEqual(
            {"R00020000001", "R00020000002", "R00020000003"},
            set(rows_by_tracking),
        )
        self.assertNotIn("R00019999999", rows_by_tracking)
        self.assertEqual(0, rows_by_tracking["R00020000001"][-1])
        self.assertEqual(2, rows_by_tracking["R00020000002"][-1])
        self.assertEqual(1, rows_by_tracking["R00020000003"][-1])
        self.assertEqual(2, result["main_trackings"])
        self.assertEqual(3, result["accumulated_main_trackings"])

    def test_arrival_stats_filters_prior_complete_arrive_only_but_keeps_current_rescan(self):
        arrive_records = [
            {
                "tracking_number": "R00021000001",
                "goods_name": "历史已到齐且当天未扫",
                "quantity": 2,
                "destination_station": "邵阳",
            },
            {
                "tracking_number": "R00021000002",
                "goods_name": "历史已到齐但当天重扫",
                "quantity": 2,
                "destination_station": "邵阳",
            },
            {
                "tracking_number": "R00021000003",
                "goods_name": "历史未到齐",
                "quantity": 3,
                "destination_station": "邵阳",
            },
            {
                "tracking_number": "R00021000005",
                "goods_name": "历史到货为零且当天未扫",
                "quantity": 4,
                "destination_station": "邵阳",
            },
        ]
        current_scan_rows = [
            {"raw_code": "R000210000020002", "destination": "邵阳", "code_type": "child"},
            {"raw_code": "R000210000040001", "destination": "邵阳", "code_type": "child"},
        ]
        accumulated_scan_rows = [
            {"raw_code": "R000210000020001", "destination": "邵阳", "code_type": "child"},
            {"raw_code": "R000210000030001", "destination": "邵阳", "code_type": "child"},
            *current_scan_rows,
        ]
        fetched_records = [
            {
                "tracking_number": "R00021000004",
                "goods_name": "仅当天扫描",
                "quantity": 1,
                "destination_station": "邵阳",
            }
        ]
        written_values = []

        def fake_write_stats(resource_key, values, params):
            written_values.append(values)
            return {"ok": True, "rows": len(values)}

        history_result = {
            "ok": True,
            "source": "arrival_stat_active_snapshots",
            "target_date": "2026-08-13",
            "prior_successful_dates": 1,
            "completed_tracking_numbers": 2,
        }
        with (
            patch(
                "tools.arrival_stats_sync_tool.fetch_arrive_list_records",
                return_value=(arrive_records, {"ok": True, "source": "fetch_dispatch", "bill_codes": 4}),
            ),
            patch("tools.arrival_stats_sync_tool._refresh_scan_index", return_value=(current_scan_rows, {"ok": True})),
            patch("tools.arrival_stats_sync_tool.list_scan_codes", return_value=accumulated_scan_rows),
            patch(
                "tools.arrival_stats_sync_tool.load_completed_arrival_trackings_before",
                return_value=({"R00021000001", "R00021000002"}, history_result),
            ),
            patch(
                "tools.arrival_stats_sync_tool._fetch_waybill_details",
                return_value=(fetched_records, {"ok": True, "requested": 1, "fetched": 1}),
            ),
            patch("tools.arrival_stats_sync_tool._write_stats_sheet", side_effect=fake_write_stats),
        ):
            result = arrival_stats_sync_tool.run_arrival_stats_sync(
                {
                    "target_date": "2026-08-13",
                    "dry_run": True,
                    "archive_snapshot": False,
                    "pending_sheet_disabled": True,
                }
            )

        self.assertTrue(result["ok"])
        rows_by_tracking = {row[0]: row for row in written_values[0][1:]}
        self.assertEqual(
            {"R00021000002", "R00021000003", "R00021000004", "R00021000005"},
            set(rows_by_tracking),
        )
        self.assertNotIn("R00021000001", rows_by_tracking)
        self.assertEqual(2, rows_by_tracking["R00021000002"][-1])
        self.assertEqual(1, rows_by_tracking["R00021000003"][-1])
        self.assertEqual(1, rows_by_tracking["R00021000004"][-1])
        self.assertEqual(0, rows_by_tracking["R00021000005"][-1])
        self.assertEqual(
            {
                **history_result,
                "arrive_list_input": 4,
                "matched_historical_completed": 2,
                "filtered_arrive_only": 1,
                "preserved_current_scan": 1,
                "arrive_list_output": 3,
            },
            result["historical_filter_result"],
        )

    def test_historical_completed_query_uses_only_prior_active_success_snapshots(self):
        class FakeCursor:
            def __init__(self):
                self.queries = []
                self.fetchone_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def execute(self, query, params):
                self.queries.append((query, params))

            def fetchone(self):
                self.fetchone_calls += 1
                return {"prior_successful_dates": 2}

            def fetchall(self):
                return [
                    {"tracking_number": "R00021000001"},
                    {"tracking_number": " R00021000002 "},
                    {"tracking_number": ""},
                ]

        class FakeConnection:
            def __init__(self):
                self.fake_cursor = FakeCursor()
                self.closed = False

            def cursor(self):
                return self.fake_cursor

            def close(self):
                self.closed = True

        connection = FakeConnection()
        with (
            patch("tools.daily_sign_store.ensure_daily_sign_tables"),
            patch("tools.daily_sign_store._connect", return_value=connection),
        ):
            completed, summary = daily_sign_store.load_completed_arrival_trackings_before(date(2026, 8, 13))

        self.assertEqual({"R00021000001", "R00021000002"}, completed)
        self.assertEqual(2, summary["prior_successful_dates"])
        self.assertEqual(2, summary["completed_tracking_numbers"])
        self.assertTrue(connection.closed)
        self.assertEqual(2, len(connection.fake_cursor.queries))
        run_query, run_params = connection.fake_cursor.queries[0]
        self.assertIn("status = 'success'", run_query)
        self.assertIn("is_active = TRUE", run_query)
        self.assertIn("business_date < %s", run_query)
        self.assertEqual((date(2026, 8, 13),), run_params)
        completion_query, completion_params = connection.fake_cursor.queries[1]
        self.assertIn("status = 'success'", completion_query)
        self.assertIn("is_active = TRUE", completion_query)
        self.assertIn("business_date < %s", completion_query)
        self.assertIn("MAX(latest_r.business_date)", completion_query)
        self.assertIn("GROUP BY latest_i.tracking_number", completion_query)
        self.assertIn("i.expected_quantity > 0", completion_query)
        self.assertEqual((date(2026, 8, 13), date(2026, 8, 13)), completion_params)
        self.assertIn("arrived_quantity >= i.expected_quantity", completion_query)

    def test_historical_completed_lookup_failure_is_not_silently_ignored(self):
        with patch(
            "tools.arrival_stats_sync_tool.load_completed_arrival_trackings_before",
            side_effect=RuntimeError("historical snapshot unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "historical snapshot unavailable"):
                arrival_stats_sync_tool._filter_historical_completed_arrive_records(
                    [{"tracking_number": "R00021000001"}],
                    set(),
                    date(2026, 8, 13),
                )

    def test_arrival_stats_rejects_historical_scan_only_mode(self):
        with patch("tools.arrival_stats_sync_tool.fetch_arrive_list_records") as fetch_arrive_list:
            result = arrival_stats_sync_tool.run_arrival_stats_sync({"refresh_disabled": True})

        self.assertEqual("current_scan_required", result["stage"])
        fetch_arrive_list.assert_not_called()

    def test_optional_pending_sheet_missing_resource_is_skipped(self):
        with patch(
            "tools.arrival_stats_sync_tool._write_stats_sheet",
            side_effect=ValueError("未找到 phase7.pending_arrivals_sheet，请先导入到 MySQL"),
        ):
            result = arrival_stats_sync_tool._write_optional_pending_sheet([["运单编号"]], {})

        self.assertEqual(
            {
                "ok": False,
                "skipped": True,
                "reason": "missing_resource",
                "detail": "未找到 phase7.pending_arrivals_sheet，请先导入到 MySQL",
            },
            result,
        )

    def test_optional_pending_sheet_write_error_is_non_blocking(self):
        with patch(
            "tools.arrival_stats_sync_tool._write_stats_sheet",
            return_value={"error": "写入统计表失败"},
        ):
            result = arrival_stats_sync_tool._write_optional_pending_sheet([["运单编号"]], {})

        self.assertTrue(result["skipped"])
        self.assertEqual("write_failed", result["reason"])
        self.assertNotIn("error", result)

    def test_arrival_snapshot_is_activated_when_optional_pending_sheet_write_fails(self):
        record = {
            "tracking_number": "R00014600001",
            "goods_name": "货物",
            "package_type": "纸箱",
            "delivery_method": "送货",
            "quantity": 1,
            "recipient_name": "张三",
            "recipient_phone": "13800000000",
            "recipient_address": "湖南省邵阳市大祥区测试路1号",
            "destination_station": "邵阳大祥S站",
        }

        def fake_write(resource_key, values, params):
            if resource_key == "phase7.pending_arrivals_sheet":
                return {"error": "write failed"}
            return {"ok": True, "rows": len(values)}

        with (
            patch(
                "tools.arrival_stats_sync_tool.fetch_arrive_list_records",
                return_value=([record], {"ok": True, "source": "fetch_dispatch", "bill_codes": 1}),
            ),
            patch("tools.arrival_stats_sync_tool._refresh_scan_index", return_value=([], {"ok": True})),
            patch("tools.arrival_stats_sync_tool.list_scan_codes", return_value=[]),
            patch(
                "tools.arrival_stats_sync_tool._fetch_waybill_details",
                return_value=([], {"ok": True, "requested": 0, "fetched": 0}),
            ),
            patch("tools.arrival_stats_sync_tool.list_pending_waybills", return_value=[]),
            patch("tools.arrival_stats_sync_tool._write_stats_sheet", side_effect=fake_write),
            patch("tools.arrival_stats_sync_tool.save_arrival_stat_snapshot") as save_snapshot,
        ):
            result = arrival_stats_sync_tool.run_arrival_stats_sync({"dry_run": True, "archive_snapshot": False})

        self.assertTrue(result["ok"])
        self.assertEqual("write_failed", result["pending_result"]["reason"])
        save_snapshot.assert_called_once()

    def test_arrival_stats_does_not_use_total_quantity_for_partial_child_arrivals(self):
        scan_rows = [
            {
                "raw_code": f"R00014371325{index:04d}",
                "destination": "邵阳自提部",
                "code_type": "child",
            }
            for index in range(1, 30)
        ]

        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            scan_rows,
            [{"tracking_number": "R00014371325", "quantity": 31}],
            ["R00014371325"],
        )

        self.assertEqual(29, count_map["R00014371325"])
        self.assertEqual(0, result["quantity_adjustments"])
        self.assertEqual(1, result["quantity_gaps"])

    def test_arrival_stats_counts_accumulate_across_days(self):
        # 11-piece shipment: 5 children scanned yesterday + 6 today should sum to 11
        yesterday_rows = [
            {"raw_code": f"R0001437132500{idx:02d}", "destination": "邵阳", "code_type": "child"} for idx in range(1, 6)
        ]
        today_rows = [
            {"raw_code": f"R0001437132500{idx:02d}", "destination": "邵阳", "code_type": "child"}
            for idx in range(6, 12)
        ]
        cumulative_rows = yesterday_rows + today_rows

        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            cumulative_rows,
            [{"tracking_number": "R00014371325", "quantity": 11}],
            ["R00014371325"],
        )

        self.assertEqual(11, count_map["R00014371325"])
        self.assertEqual(1, result["arrived_nonzero"])
        self.assertEqual(0, result["quantity_gaps"])

    def test_arrival_stats_caps_accumulated_scan_count_at_opened_quantity(self):
        scan_rows = [
            {
                "raw_code": f"R00014371325{index:04d}",
                "destination": "邵阳",
                "code_type": "child",
            }
            for index in range(1, 5)
        ]

        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            scan_rows,
            [{"tracking_number": "R00014371325", "quantity": 2}],
            ["R00014371325"],
        )

        self.assertEqual(2, count_map["R00014371325"])
        self.assertEqual(1, result["quantity_adjustments"])

    def test_normalize_scan_rows_emits_main_tracking(self):
        normalized = phase7_mysql_store.normalize_scan_rows(
            [
                {"扫描单号": "2001513259", "目的地": "邵阳"},
                {"扫描单号": "R000143402890001", "目的地": "邵阳"},
                {"扫描单号": "20055750680002", "目的地": "邵阳武冈站"},
            ]
        )
        by_code = {row["raw_code"]: row for row in normalized}
        self.assertEqual("2001513259", by_code["2001513259"]["main_tracking"])
        self.assertEqual("main", by_code["2001513259"]["code_type"])
        self.assertEqual("R00014340289", by_code["R000143402890001"]["main_tracking"])
        self.assertEqual("child", by_code["R000143402890001"]["code_type"])
        self.assertEqual("2005575068", by_code["20055750680002"]["main_tracking"])
        self.assertEqual("child", by_code["20055750680002"]["code_type"])

    def test_arrival_stats_collapses_numeric_ronghui_child_trackings(self):
        scan_rows = [
            {"raw_code": "20055750680002", "destination": "邵阳武冈站", "code_type": "child"},
            {"raw_code": "20055750680004", "destination": "邵阳武冈站", "code_type": "child"},
            {"raw_code": "20055750680020", "destination": "邵阳武冈站", "code_type": "child"},
        ]

        missing = arrival_stats_sync_tool._missing_trackings_from_current_scan(scan_rows, [], {})
        count_map, result = arrival_stats_sync_tool._count_arrivals_from_scan_rows(
            scan_rows,
            [{"tracking_number": "2005575068", "quantity": 20}],
            ["2005575068"],
        )

        self.assertEqual(["2005575068"], missing)
        self.assertEqual(3, count_map["2005575068"])
        self.assertEqual(1, result["arrived_nonzero"])
        self.assertEqual(1, result["quantity_gaps"])
        self.assertFalse(phase7_mysql_store.should_include_waybill_tracking("20055750680002"))
        self.assertTrue(phase7_mysql_store.should_include_waybill_tracking("2005575068"))

    def test_phase7_mysql_wsl_gateway_uses_localhost_outside_wsl(self):
        with (
            patch.dict(os.environ, {"DOCFLOW_MYSQL_HOST": "wsl-gateway"}, clear=True),
            patch.object(phase7_mysql_store, "_running_in_wsl", return_value=False),
        ):
            self.assertEqual("127.0.0.1", phase7_mysql_store._resolve_mysql_host())

    def test_phase7_mysql_wsl_gateway_uses_gateway_inside_wsl(self):
        with (
            patch.dict(os.environ, {"DOCFLOW_MYSQL_HOST": "wsl-gateway"}, clear=True),
            patch.object(phase7_mysql_store, "_running_in_wsl", return_value=True),
            patch.object(phase7_mysql_store, "_wsl_gateway_ip", return_value="172.25.63.253"),
        ):
            self.assertEqual("172.25.63.253", phase7_mysql_store._resolve_mysql_host())

    def test_phase7_mysql_prefers_agent_db_host(self):
        with patch.dict(
            os.environ,
            {"AGENT_DB_HOST": "agent-db.internal", "DOCFLOW_MYSQL_HOST": "wsl-gateway"},
            clear=True,
        ):
            self.assertEqual("agent-db.internal", phase7_mysql_store._resolve_mysql_host())

    def test_apply_scan_window_default_does_not_inject_dates(self):
        params = arrival_stats_sync_tool._apply_scan_window({"output_format": "json"}, 1)
        self.assertNotIn("start", params)
        self.assertNotIn("end", params)

    def test_apply_scan_window_uses_target_date_for_single_day(self):
        params = arrival_stats_sync_tool._apply_scan_window(
            {"output_format": "json"},
            1,
            date(2026, 5, 4),
        )
        self.assertEqual("2026/05/04", params["date"])

    def test_apply_scan_window_widens_to_n_days(self):
        from datetime import datetime as _dt

        params = arrival_stats_sync_tool._apply_scan_window({"output_format": "json"}, 30)
        self.assertIn("start", params)
        self.assertIn("end", params)
        start_dt = _dt.strptime(params["start"], "%Y/%m/%d %H:%M:%S")
        end_dt = _dt.strptime(params["end"], "%Y/%m/%d %H:%M:%S")
        # Inclusive 30-day span -> 29 days between start and end
        self.assertEqual(29, (end_dt.date() - start_dt.date()).days)

    def test_render_stats_sheet_values_uses_stable_waybill_header(self):
        values = phase7_mysql_store.render_stats_sheet_values(
            [{"tracking_number": "R0001"}],
            {},
            target_date="2026-05-04",
        )
        self.assertEqual("运单编号", values[0][0])

    def test_apply_scan_window_respects_user_override(self):
        params = arrival_stats_sync_tool._apply_scan_window(
            {"output_format": "json", "start": "2026/04/20 00:00:00"},
            30,
        )
        self.assertEqual("2026/04/20 00:00:00", params["start"])
        self.assertNotIn("end", params)

    def test_render_pending_sheet_values_formats_status_and_counts(self):
        from datetime import datetime as _dt

        values = phase7_mysql_store.render_pending_sheet_values(
            [
                {
                    "tracking_number": "R0001",
                    "destination_station": "邵阳大祥S站",
                    "expected_quantity": 11,
                    "arrived_quantity": 5,
                    "pending_quantity": 6,
                    "arrival_status": "partial",
                    "first_arrival_at": _dt(2026, 4, 25, 9, 30, 0),
                    "last_arrival_at": _dt(2026, 4, 26, 14, 15, 30),
                },
                {
                    "tracking_number": "R0002",
                    "destination_station": "邵阳大祥S站",
                    "expected_quantity": 3,
                    "arrived_quantity": 0,
                    "pending_quantity": 3,
                    "arrival_status": "pending",
                    "first_arrival_at": None,
                    "last_arrival_at": None,
                },
            ]
        )

        self.assertEqual(phase7_mysql_store.PENDING_ARRIVAL_HEADERS, values[0])
        self.assertEqual(
            ["R0001", "邵阳大祥S站", 11, 5, 6, "部分到货", "2026-04-25 09:30:00", "2026-04-26 14:15:30"], values[1]
        )
        self.assertEqual(["R0002", "邵阳大祥S站", 3, 0, 3, "未到货", "", ""], values[2])

    def test_arrive_and_stats_sheet_numeric_cells_are_numbers(self):
        record = {
            "tracking_number": "R0001",
            "goods_name": "配件",
            "package_type": "纸箱",
            "delivery_method": "派送",
            "quantity": 2,
            "receipt_number": "",
            "actual_weight": Decimal("12.50"),
            "volume": Decimal("0.30"),
            "remarks": "",
            "destination_station": "邵阳",
            "recipient_name": "张三",
            "recipient_phone": "13800000000",
            "recipient_address": "湖南省邵阳市",
            "settlement_weight": Decimal("13.00"),
            "volumetric_weight": Decimal("10.50"),
            "shipping_fee": Decimal("21.30"),
            "payment_type": "现金",
            "pay_on_arrival": Decimal("0.00"),
        }

        arrive_row = phase7_mysql_store.render_arrive_sheet_rows([record])[0]
        stats_row = phase7_mysql_store.render_stats_sheet_values([record], {"R0001": 2})[1]

        for row in (arrive_row, stats_row):
            self.assertIsInstance(row[4], int)
            self.assertIsInstance(row[6], float)
            self.assertIsInstance(row[7], float)
            self.assertIsInstance(row[13], int)
            self.assertIsInstance(row[14], float)
            self.assertIsInstance(row[15], float)
            self.assertIsInstance(row[17], int)
            self.assertIsInstance(row[0], str)
            self.assertIsInstance(row[11], str)
        self.assertIsInstance(stats_row[18], int)

    def test_waybill_export_headers_use_累计_label(self):
        self.assertEqual("累计到货件数", phase7_mysql_store.WAYBILL_EXPORT_HEADERS[-1])

    def test_yunda_waybill_tracking_maps_route_rows(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def json(self):
                return {
                    "total": 1,
                    "rows": [
                        {
                            "977808459": {
                                "smi": {
                                    "1": {
                                        "Scan_Time": "2026-05-10 17:28:54",
                                        "description": (
                                            '快件在<span data-original-title="56512000&lt;br&gt;'
                                            "江苏无锡分拨中心&lt;br&gt;分拨经理【戴昭刚】"
                                            '&lt;br&gt;分拨客服电话【0512-87830060】">'
                                            "【湖南邵阳双清滨江公司】</span>已揽件开单"
                                        ),
                                        "SR": "网点系统",
                                        "DV": "81102202005961",
                                    },
                                    "2": {
                                        "Scan_Time": "2026-05-12 13:11:45",
                                        "description": "快件已被客户<span>【指定位置】</span>签收",
                                        "SR": "02",
                                        "DV": "56571150217797",
                                    },
                                    "3": {
                                        "Scan_Time": "2026-05-13 10:00:00",
                                        "description": (
                                            '快件到达<span class="siteName">【江苏无锡分拨中心】</span>'
                                            '上一站是<span class="siteName">【湖南长沙分拨中心】</span>'
                                        ),
                                        "SR": "01",
                                        "DV": "81102439001556",
                                    },
                                    "info": {"ignored": True},
                                }
                            }
                        },
                    ],
                    "site": {
                        "江苏无锡分拨中心": {
                            "site_code": 56512000,
                            "site_name": "江苏无锡分拨中心",
                            "type": 3,
                            "fzr": "赖照刚",
                            "problem_phone": "0512-87830060",
                        },
                        "湖南长沙分拨中心": {
                            "site_code": 56731000,
                            "site_name": "湖南长沙分拨中心",
                            "type": 3,
                            "fzr": "邓鑫",
                            "problem_phone": "0731-89512469",
                        },
                    },
                }

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"method": "POST", "url": url, "data": data})
                return Response()

        session = Session()
        result = yunda_waybill_tracking.query_yunda_tracking(
            session,
            "977808459",
            {},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("yunda", result["type"])
        self.assertEqual("977808459", result["tracking_number"])
        self.assertEqual(3, len(result["route_rows"]))
        self.assertEqual("2026-05-10 17:28:54", result["route_rows"][0]["scan_time"])
        self.assertEqual(
            "江苏无锡分拨中心：分拨经理【戴昭刚】；分拨客服电话【0512-87830060】",
            result["route_rows"][0]["contact"],
        )
        self.assertEqual("网点系统", result["route_rows"][0]["data_source"])
        self.assertEqual("签收", result["route_rows"][1]["status"])
        self.assertEqual("56571150217797", result["route_rows"][1]["device_no"])
        self.assertEqual(
            "江苏无锡分拨中心：分拨经理【赖照刚】；分拨客服电话【0512-87830060】\n"
            "湖南长沙分拨中心：分拨经理【邓鑫】；分拨客服电话【0731-89512469】",
            result["route_rows"][2]["contact"],
        )
        self.assertEqual("977808459", session.calls[0]["data"]["Ids[]"])

    def test_yunda_waybill_tracking_maps_waybill_details(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def json(self):
                return {
                    "rows": [
                        {
                            "980077246": {
                                "logistics": {
                                    "Logistics_Id": "980077246",
                                    "Sender_Name": "勇胜",
                                    "Sender_Phone": "073*****128",
                                    "Buyer_Name": "洪师傅",
                                    "Buyer_Mobile": "158****9716",
                                    "Buyer_Destination_Dot_Code": "安徽铜陵公司三分部",
                                    "Buyer_Address": "安徽省铜陵市郊区铜都大道中段",
                                    "Item_Name": "吨袋",
                                    "Packing_Type": "编织袋:12",
                                    "Item_Total_Number": 12,
                                    "Gross_Weight": "522.00",
                                    "Settlement_Total_Number": "557.90",
                                    "Volume": "2.7895",
                                    "Payment_Type": "现金",
                                    "Freight": "12.00",
                                    "Shipping_Methods": "180",
                                    "COD": "0.00",
                                    "Remarks": "测试备注",
                                },
                                "smi": {
                                    "1": {
                                        "Scan_Time": "2026-05-10 17:28:54",
                                        "description": "快件已揽件开单",
                                    }
                                },
                            }
                        }
                    ]
                }

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        result = yunda_waybill_tracking.query_yunda_tracking(
            Session(),
            "980077246",
            {},
        )

        self.assertEqual("980077246", result["waybill_stub"]["waybill_no"])
        self.assertEqual("勇胜", result["waybill_stub"]["sender_name"])
        self.assertEqual("洪师傅", result["waybill_stub"]["recipient_name"])
        self.assertEqual("安徽铜陵公司三分部", result["waybill_stub"]["disp_site"])
        self.assertEqual("吨袋", result["waybill_stub"]["goods_name"])
        self.assertEqual("派送", result["waybill_stub"]["delivery_method"])
        self.assertEqual("522.00 kg", result["waybill_stub"]["weight"])
        self.assertEqual("测试备注", result["waybill_stub"]["remark"])
        info_sections = {section["title"]: section["items"] for section in result["waybill_info"]}
        self.assertIn({"label": "寄件人", "value": "勇胜"}, info_sections["发货信息"])
        self.assertIn({"label": "收货人", "value": "洪师傅"}, info_sections["收货信息"])
        self.assertIn({"label": "目的网点", "value": "安徽铜陵公司三分部"}, info_sections["收货信息"])
        self.assertIn({"label": "货物名称", "value": "吨袋"}, info_sections["货物信息"])
        self.assertIn({"label": "派送方式", "value": "派送"}, info_sections["货物信息"])
        self.assertIn({"label": "运费", "value": "12.00"}, info_sections["费用信息"])

    def test_yunda_waybill_tracking_decrypts_masked_contact_details(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def __init__(self, payload):
                self._payload = payload
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "data": data})
                if url.endswith("/system/mail/getOriginalData.html"):
                    return Response(
                        {
                            "data": {
                                "Sender_Name": "勇胜",
                                "Sender_Phone": "07315186128",
                                "Buyer_Name": "振杰",
                                "Buyer_Mobile": "13700003310",
                            }
                        }
                    )
                return Response(
                    {
                        "rows": [
                            {
                                "980520249": {
                                    "logistics": {
                                        "Logistics_Id": "980520249",
                                        "Sender_Name": "勇*",
                                        "Sender_Phone": "073*****128",
                                        "Buyer_Name": "振*",
                                        "Buyer_Mobile": "137*****3310",
                                        "Buyer_Address": "四川省成都市新都区中集大道",
                                        "Shipping_Methods": "自提",
                                    },
                                    "smi": {
                                        "1": {
                                            "Scan_Time": "2026-05-31 10:00:00",
                                            "description": "快件已到达",
                                        }
                                    },
                                }
                            }
                        ]
                    }
                )

        session = Session()
        result = yunda_waybill_tracking.query_yunda_tracking(session, "980520249", {})

        self.assertEqual("勇胜", result["waybill_stub"]["sender_name"])
        self.assertEqual("振杰", result["waybill_stub"]["recipient_name"])
        info_sections = {section["title"]: section["items"] for section in result["waybill_info"]}
        self.assertIn({"label": "寄件人", "value": "勇胜"}, info_sections["发货信息"])
        self.assertIn({"label": "寄件电话", "value": "07315186128"}, info_sections["发货信息"])
        self.assertIn({"label": "收货人", "value": "振杰"}, info_sections["收货信息"])
        self.assertIn({"label": "收货电话", "value": "13700003310"}, info_sections["收货信息"])
        self.assertEqual(
            ["/system/mail/list.html", "/system/mail/getOriginalData.html"],
            [call["url"][call["url"].find("/system/mail/") :] for call in session.calls],
        )

    def test_tracking_query_detects_providers(self):
        self.assertEqual("yunda", tracking_query.detect_tracking_provider("977808459"))
        self.assertEqual("yunda", tracking_query.detect_tracking_provider("298861675"))
        self.assertEqual("yunda", tracking_query.detect_tracking_provider("708429045"))
        self.assertEqual("ronghui", tracking_query.detect_tracking_provider("200123456"))
        self.assertEqual("zhuanxian", tracking_query.detect_tracking_provider("000123456"))

    def test_yunda_waybill_tracking_accepts_empty_route_response(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = ""

            def json(self):
                return {"total": 0, "rows": []}

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        result = yunda_waybill_tracking.query_yunda_tracking(
            Session(),
            "977808459",
            {},
        )

        self.assertTrue(result["ok"])
        self.assertEqual("yunda", result["type"])
        self.assertEqual([], result["route_rows"])

    def test_track_waybill_tool_calls_unified_tms_endpoint(self):
        with patch(
            "tools.track_waybill_tool.call_http_service",
            return_value={
                "ok": True,
                "cost_sec": 0.01,
                "data": {
                    "ok": True,
                    "type": "yunda",
                    "tracking_number": "977808459",
                    "route_rows": [],
                },
            },
        ) as call_http:
            result = track_waybill_tool.run_track_waybill({"tracking_number": " 977808459 "})

        self.assertEqual("yunda", result["type"])
        self.assertEqual("977808459", result["tracking_number"])
        self.assertEqual("/tms/tracking_query", call_http.call_args.args[0])
        self.assertEqual("977808459", call_http.call_args.args[1]["params"]["tracking_number"])

    def test_track_waybill_tool_rejects_invalid_r_tracking_number_without_http_call(self):
        with patch("tools.track_waybill_tool.call_http_service") as call_http:
            result = track_waybill_tool.run_track_waybill({"tracking_number": "R000016211453"})

        self.assertEqual(
            {
                "error": "单号格式错误：R 开头融辉单号应为 R+11位主单或 R+15位子单，请检查是否多输/少输数字。",
                "error_code": "INVALID_TRACKING_NUMBER",
            },
            result,
        )
        call_http.assert_not_called()

    def test_tms_runtime_exposes_ronghui_tms_tracking_target(self):
        from agent.tms_runtime.dispatch import TARGETS, TARGET_ACCOUNT_SYSTEMS

        self.assertIn("ronghui_tms_tracking", TARGETS)
        self.assertIn("yunda_waybill_tracking", TARGETS)
        self.assertIn("yunda_waybill_entry", TARGETS)
        self.assertIn("yunda_price", TARGETS)
        self.assertIn("tracking_query", TARGETS)
        self.assertIn("yunda_dispatch_forecast", TARGETS)
        self.assertIn("yunda_send_waybills", TARGETS)
        self.assertEqual("ronghui", TARGET_ACCOUNT_SYSTEMS["ronghui_tms_tracking"])
        self.assertEqual("yunda", TARGET_ACCOUNT_SYSTEMS["yunda_price"])
        self.assertIn("/tms/ronghui_tms_tracking", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_waybill_tracking", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_waybill_entry", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_price", {route.path for route in router.routes})
        self.assertIn("/tms/tracking_query", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_dispatch_forecast", {route.path for route in router.routes})
        self.assertIn("/tms/yunda_send_waybills", {route.path for route in router.routes})

    def test_query_waybill_detail_requests_decrypted_view(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"result": {"data": [{"BILL_CODE": "R0001"}]}}

        class Session:
            def __init__(self):
                self.data = None

            def post(self, *args, **kwargs):
                self.data = kwargs["data"]
                return Response()

        session = Session()

        row = tms_query_waybill_detail._query_one(session, "R0001")

        self.assertEqual("R0001", row["tracking_number"])
        self.assertEqual({"billCode": "R0001", "isView": "true"}, session.data)

    def test_query_waybill_detail_includes_real_tracking_sign_scan(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"result": {"data": [{"BILL_CODE": "R0001"}]}}

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        with patch(
            "agent.tms_runtime.scripts.ronghui_tms_tracking.fetch_main_route_rows",
            return_value=[
                {"SCAN_TYPE": "派件", "SCAN_DATE": "2026-08-10 12:30:00", "SCAN_SITE": "邵阳大祥S站"},
                {"SCAN_TYPE": "签收", "SCAN_DATE": "2026-08-10 12:36:07", "SCAN_SITE": "邵阳大祥S站"},
            ],
        ) as fetch_routes:
            row = tms_query_waybill_detail._query_one(
                Session(),
                "R0001",
                include_sign_status=True,
                tracking_url="https://tms.ronghuiwl.com/widget/home?authenticationKey=real",
            )

        self.assertTrue(row["sign_status_checked"])
        self.assertTrue(row["is_signed"])
        self.assertEqual("2026-08-10 12:36:07", row["actual_sign_time"])
        self.assertEqual("邵阳大祥S站", row["sign_site_name"])
        self.assertEqual("R0001", fetch_routes.call_args.args[1])
        self.assertEqual(
            "https://tms.ronghuiwl.com/widget/home?authenticationKey=real",
            fetch_routes.call_args.kwargs["tracking_url"],
        )

    def test_query_waybill_detail_marks_nonempty_tracking_without_sign_scan_unsigned(self):
        status = tms_query_waybill_detail._sign_status_fields(
            [{"SCAN_TYPE": "派件", "SCAN_DATE": "2026-08-10 12:30:00"}]
        )

        self.assertTrue(status["sign_status_checked"])
        self.assertFalse(status["is_signed"])

    def test_query_waybill_detail_skips_browser_when_api_row_is_complete(self):
        api_row = {
            "requested_bill_code": "R0001",
            "tracking_number": "R0001",
            "goods_name": "配件",
            "recipient_name": "张三",
            "recipient_phone": "13800000000",
            "recipient_address": "湖南省邵阳市大祥区测试路1号",
            "destination_station": "邵阳大祥S站",
        }

        with (
            patch("query_waybill_detail._run_single_session", return_value=[api_row]),
            patch("query_waybill_detail._overlay_with_browser", return_value={}) as overlay,
        ):
            rows = tms_query_waybill_detail.query_waybill_details(
                bill_codes=["R0001"],
                decrypt_masked=True,
            )

        self.assertEqual("R0001", rows[0]["tracking_number"])
        overlay.assert_called_once_with(
            bill_codes=[],
            headless=True,
            timeout_ms=30_000,
            batch_size=1,
            max_workers=1,
        )

    def test_waybill_tracking_click_decrypt_uses_miniui_component(self):
        class Frame:
            def __init__(self):
                self.evaluated = False

            def evaluate(self, script):
                self.evaluated = True
                return True

        frame = Frame()

        waybill_tracking._click_decrypt(frame)

        self.assertTrue(frame.evaluated)

    def test_arrival_stats_sheet_write_skips_header_when_range_starts_at_row_two(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "Sheet1!A2:B3",
            "clear_range": "Sheet1!A2:B3",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch("tools.arrival_stats_sync_tool.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = arrival_stats_sync_tool._write_stats_sheet(
                "phase7.arrive_primary_sheet",
                values,
                {},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(1, result["rows"])
        self.assertEqual("Sheet1!A2:B3", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual("Sheet1!A1:B1", feishu_op.call_args_list[1].args[1]["range"])
        self.assertEqual([["header-a", "header-b"]], feishu_op.call_args_list[1].args[1]["values"])
        self.assertEqual("Sheet1!A2:B2", feishu_op.call_args_list[2].args[1]["range"])
        self.assertEqual([["row-a", "row-b"]], feishu_op.call_args_list[2].args[1]["values"])

    def test_arrival_stats_sheet_clear_preserves_header_and_extends_to_arrival_count(self):
        resource = {
            "spreadsheet_token": "sheet-token",
            "range": "8fc516!A2:S31",
            "clear_range": "8fc516!A1:R200",
            "title_range": "8fc516!A1:R1",
        }
        values = [
            [f"header-{index}" for index in range(19)],
            [f"row-{index}" for index in range(19)],
        ]

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch("tools.arrival_stats_sync_tool.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = arrival_stats_sync_tool._write_stats_sheet(
                "phase7.arrive_secondary_sheet",
                values,
                {},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("8fc516!A2:S200", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual("8fc516!A1:S1", feishu_op.call_args_list[1].args[1]["range"])
        self.assertEqual("8fc516!A2:S2", feishu_op.call_args_list[2].args[1]["range"])

    def test_arrival_stats_sheet_write_keeps_header_when_range_starts_at_row_one(self):
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        self.assertEqual(
            values,
            arrival_stats_sync_tool._values_for_stats_write("Sheet1!A1:B3", values),
        )

    def test_arrival_stats_archive_creates_missing_date_sheet(self):
        resource = {
            "spreadsheet_token": "archive-token",
            "default_write_range": "A1:B20",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        def _fake_feishu_operation(action, params):
            if action == "add_sheet":
                return {
                    "data": {
                        "replies": [
                            {
                                "addSheet": {
                                    "properties": {
                                        "sheetId": "sheet-new",
                                    }
                                }
                            }
                        ]
                    }
                }
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch("tools.arrival_stats_sync_tool._find_archive_sheet", return_value=None),
            patch("tools.arrival_stats_sync_tool.feishu_operation", side_effect=_fake_feishu_operation) as feishu_op,
        ):
            result = arrival_stats_sync_tool._archive_snapshot(
                values,
                {"archive_title": "2026-05-22"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual("sheet-new", result["sheet_id"])
        self.assertFalse(result["reused_existing_sheet"])
        self.assertEqual(["add_sheet", "write_sheet"], [call.args[0] for call in feishu_op.call_args_list])
        self.assertEqual("sheet-new!A1:B2", feishu_op.call_args_list[1].args[1]["range"])

    def test_arrival_stats_archive_reuses_existing_date_sheet_and_clears_old_rows(self):
        resource = {
            "spreadsheet_token": "archive-token",
            "default_write_range": "Sheet1!A1:B3",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch(
                "tools.arrival_stats_sync_tool._find_archive_sheet",
                return_value={"sheet_id": "sheet-existing", "title": "2026-05-22", "row_count": 6},
            ),
            patch("tools.arrival_stats_sync_tool.feishu_operation", return_value={"ok": True}) as feishu_op,
        ):
            result = arrival_stats_sync_tool._archive_snapshot(
                values,
                {"archive_title": "2026-05-22"},
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["reused_existing_sheet"])
        self.assertEqual("sheet-existing", result["sheet_ref"])
        self.assertEqual(["write_sheet", "write_sheet"], [call.args[0] for call in feishu_op.call_args_list])
        self.assertEqual("sheet-existing!A1:B6", feishu_op.call_args_list[0].args[1]["range"])
        self.assertEqual(6, len(feishu_op.call_args_list[0].args[1]["values"]))
        self.assertEqual("sheet-existing!A1:B2", feishu_op.call_args_list[1].args[1]["range"])

    def test_arrival_stats_archive_resolves_add_sheet_conflict_by_requerying(self):
        resource = {
            "spreadsheet_token": "archive-token",
            "default_write_range": "A1:B3",
        }
        values = [
            ["header-a", "header-b"],
            ["row-a", "row-b"],
        ]

        def _fake_feishu_operation(action, params):
            if action == "add_sheet":
                return {"error": "sheet title already exists"}
            if action == "write_sheet":
                return {"ok": True, "rows": len(params["values"])}
            raise AssertionError(action)

        with (
            patch("tools.arrival_stats_sync_tool.get_required_resource", return_value=resource),
            patch(
                "tools.arrival_stats_sync_tool._find_archive_sheet",
                side_effect=[
                    None,
                    {"sheet_id": "sheet-existing", "title": "2026-05-22", "row_count": 4},
                ],
            ) as find_sheet,
            patch("tools.arrival_stats_sync_tool.feishu_operation", side_effect=_fake_feishu_operation) as feishu_op,
        ):
            result = arrival_stats_sync_tool._archive_snapshot(
                values,
                {"archive_title": "2026-05-22"},
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["reused_existing_sheet"])
        self.assertEqual("sheet-existing", result["sheet_id"])
        self.assertEqual(2, find_sheet.call_count)
        self.assertEqual(
            ["add_sheet", "write_sheet", "write_sheet"], [call.args[0] for call in feishu_op.call_args_list]
        )
        self.assertEqual("sheet-existing!A1:B4", feishu_op.call_args_list[1].args[1]["range"])

    def test_arrival_stats_public_result_removes_tokens(self):
        result = arrival_stats_sync_tool._public_result(
            {
                "ok": True,
                "data": {
                    "spreadsheetToken": "sensitive-token",
                    "updatedCells": 10,
                },
                "nested": [{"webhook": "https://example.invalid/hook"}],
            }
        )

        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sensitive-token", dumped)
        self.assertNotIn("spreadsheetToken", dumped)
        self.assertNotIn("example.invalid", dumped)
        self.assertEqual(10, result["data"]["updatedCells"])
