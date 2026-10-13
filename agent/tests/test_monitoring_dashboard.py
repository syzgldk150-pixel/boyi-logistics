import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.tms_runtime import monitoring
from agent.tms_runtime.routes import router


class MonitoringParsingTests(unittest.TestCase):
    def setUp(self):
        monitoring._SNAPSHOT_CACHE.clear()
        monitoring._DAILY_SIGN_CACHE.clear()

    def test_yunda_detail_link_uses_only_server_category_metadata(self):
        safe_url = "https://ky-client.yunda56.com/#/ifarme/ifarme/4768/problem"
        snapshot = {"systems": [{"system": "yunda", "categories": [{
            "category_id": "yunda:001", "title": "问题件消息", "open_url": safe_url,
        }]}]}
        with patch("agent.tms_runtime.monitoring.build_monitoring_snapshot", return_value=snapshot) as collect:
            link = monitoring.build_monitoring_detail_link({
                "system": "yunda", "category_id": "yunda:001", "title": "未受信标题",
                "resource_id": "99999", "type_code": "999", "target_title": "未受信目标",
                "url": "https://example.invalid/", "open_url": "https://example.invalid/",
            })

        self.assertTrue(link["ok"])
        self.assertEqual("external", link["mode"])
        self.assertEqual("问题件消息", link["title"])
        self.assertEqual(safe_url, link["open_url"])
        self.assertEqual("", link["embed_url"])
        self.assertFalse(link["frame_allowed"])
        self.assertNotIn("example.invalid", str(link))
        self.assertNotIn("99999", str(link))
        collect.assert_called_once_with(systems=["yunda"])

    def test_daily_sign_snapshot_counts_target_date_rows_from_feishu_sheet(self):
        sheet_values = [
            ["运单编号", "R13应签收时间", "问题件后应签时间", "货物品名", "包装类型", "货物件数", "收件人地址", "送货方式", "到货件数"],
            ["R1", "2026-06-01 23:59:59", "2026-05-30 23:59:59", "食品", "纸箱", 1, "addr", "自提", 1],
            ["R2", "2026-05-31T12:00:00", "", "配件", "托盘", 1, "addr", "送货", ""],
            ["R3", "2026-06-01 00:00:00", "", "家具", "木架", 1, "addr", "送货", ""],
            ["", "2026-05-31 08:00:00", "", "空单号", "纸箱", 1, "addr", "送货", ""],
        ]

        with (
            patch(
                "agent.tms_runtime.monitoring.get_workflow_resource",
                return_value={
                    "spreadsheet_token": "sheet-token",
                    "range": "Sheet1!A2:G200",
                    "clear_range": "Sheet1!A2:H200",
                },
                create=True,
            ),
            patch(
                "agent.tms_runtime.monitoring.feishu_operation",
                return_value={"ok": True, "data": {"valueRange": {"values": sheet_values}}},
                create=True,
            ) as feishu_op,
            patch("agent.tms_runtime.monitoring.list_scheduled_tasks", return_value=[], create=True),
        ):
            result = monitoring.build_daily_sign_monitoring_snapshot(target_date="2026-05-31", force=True)

        self.assertTrue(result["ok"])
        self.assertEqual("ok", result["status"])
        self.assertEqual("2026-05-31", result["target_date"])
        self.assertEqual(2, result["counts"]["unsigned_today"])
        self.assertIn("飞书", result["message"])
        self.assertEqual("read_sheet", feishu_op.call_args.args[0])
        self.assertEqual("sheet-token", feishu_op.call_args.args[1]["spreadsheet_token"])
        self.assertEqual("Sheet1!A1:I200", feishu_op.call_args.args[1]["range"])
        self.assertNotIn("sheet-token", str(result))

    def test_daily_sign_snapshot_uses_daily_sign_schedule_for_refresh_metadata(self):
        current = time.mktime((2026, 5, 31, 14, 5, 0, 0, 0, -1))

        with (
            patch(
                "agent.tms_runtime.monitoring.get_workflow_resource",
                return_value={"spreadsheet_token": "sheet-token", "range": "Sheet1!A2:H200"},
                create=True,
            ),
            patch(
                "agent.tms_runtime.monitoring.feishu_operation",
                return_value={"ok": True, "data": {"valueRange": {"values": [["运单编号", "R13应签收时间", "问题件后应签时间"], ["R1", "2026-05-31 23:59:59", ""]]}}},
                create=True,
            ),
            patch(
                "agent.tms_runtime.monitoring.list_scheduled_tasks",
                return_value=[
                    {
                        "id": "daily_sign_0500",
                        "tool_name": "sync_daily_should_sign",
                        "cron_expression": "0 5 * * *",
                        "enabled": False,
                    },
                    {
                        "id": "daily_sign_1400",
                        "tool_name": "sync_daily_should_sign",
                        "cron_expression": "0 14 * * *",
                        "enabled": True,
                    },
                    {
                        "id": "daily_sign_1530",
                        "tool_name": "sync_daily_should_sign",
                        "cron_expression": "30 15 * * *",
                        "enabled": 1,
                    },
                    {
                        "id": "site_send_1400",
                        "tool_name": "sync_site_send_list",
                        "cron_expression": "0 14 * * *",
                        "enabled": True,
                    },
                ],
                create=True,
            ),
            patch("agent.tms_runtime.monitoring.time.time", return_value=current),
        ):
            result = monitoring.build_daily_sign_monitoring_snapshot(target_date="2026-05-31", force=True)

        self.assertEqual("2026-05-31 14:00:00", result["updated_at"])
        self.assertEqual(5100, result["poll_interval_sec"])
        self.assertEqual(
            {
                "time_values": ["14:00", "15:30"],
                "last_refresh_at": "2026-05-31 14:00:00",
                "next_refresh_at": "2026-05-31 15:30:00",
                "source": "scheduled_tasks",
            },
            result["refresh_schedule"],
        )

    def test_daily_sign_snapshot_returns_safe_error_without_exposing_sheet_token(self):
        with (
            patch(
                "agent.tms_runtime.monitoring.get_workflow_resource",
                return_value={"spreadsheet_token": "secret-sheet-token", "range": "Sheet1!A2:H200"},
            ),
            patch(
                "agent.tms_runtime.monitoring.feishu_operation",
                return_value={"error": "read failed for secret-sheet-token"},
            ),
            patch("agent.tms_runtime.monitoring.list_scheduled_tasks", return_value=[]),
        ):
            result = monitoring.build_daily_sign_monitoring_snapshot(target_date="2026-05-31", force=True)

        self.assertFalse(result["ok"])
        self.assertEqual("error", result["status"])
        self.assertEqual(0, result["counts"]["unsigned_today"])
        self.assertIn("read failed", result["message"])
        self.assertNotIn("secret-sheet-token", str(result))

    def test_snapshot_prefer_cached_returns_stale_payload_without_recollecting(self):
        yunda_payload = {
            "system": "yunda",
            "label": "韵达",
            "status": "ok",
            "status_label": "已连接",
            "message": "",
            "total_count": 2,
            "exception_count": 2,
            "updated_at": "2026-05-28 13:30:00",
            "categories": [],
        }
        ronghui_payload = {
            "system": "ronghui",
            "label": "融辉 TMS",
            "status": "ok",
            "status_label": "已连接",
            "message": "",
            "total_count": 1,
            "exception_count": 0,
            "updated_at": "2026-05-28 13:30:00",
            "categories": [],
        }
        with (
            patch("agent.tms_runtime.monitoring._collect_yunda_snapshot", return_value=yunda_payload) as collect_yunda,
            patch("agent.tms_runtime.monitoring._collect_ronghui_snapshot", return_value=ronghui_payload) as collect_ronghui,
            patch(
                "agent.tms_runtime.monitoring.get_session_broker",
                return_value=type("Broker", (), {"validate_health_matrix": lambda self: {"status": "ok"}})(),
            ),
            patch("agent.tms_runtime.monitoring._schedule_snapshot_refresh") as schedule_refresh,
        ):
            first = monitoring.build_monitoring_snapshot(systems=["yunda", "ronghui"], force=True)
            second = monitoring.build_monitoring_snapshot(
                systems=["yunda", "ronghui"],
                force=True,
                prefer_cached=True,
            )

        self.assertEqual(3, first["totals"]["total_pending"])
        self.assertEqual(first["totals"], second["totals"])
        self.assertTrue(second["cached"])
        self.assertTrue(second["stale"])
        self.assertTrue(second["refreshing"])
        self.assertGreaterEqual(second["cache_age_sec"], 0)
        self.assertEqual(1, collect_yunda.call_count)
        self.assertEqual(1, collect_ronghui.call_count)
        schedule_refresh.assert_called_once()

    def test_snapshot_collects_systems_in_parallel(self):
        def slow_yunda(*, force, updated_at):
            time.sleep(0.2)
            return {
                "system": "yunda",
                "label": "韵达",
                "status": "ok",
                "status_label": "已连接",
                "message": "",
                "total_count": 2,
                "exception_count": 2,
                "updated_at": updated_at,
                "categories": [],
            }

        def slow_ronghui(*, force, updated_at):
            time.sleep(0.2)
            return {
                "system": "ronghui",
                "label": "融辉 TMS",
                "status": "ok",
                "status_label": "已连接",
                "message": "",
                "total_count": 1,
                "exception_count": 0,
                "updated_at": updated_at,
                "categories": [],
            }

        with (
            patch("agent.tms_runtime.monitoring._collect_yunda_snapshot", side_effect=slow_yunda),
            patch("agent.tms_runtime.monitoring._collect_ronghui_snapshot", side_effect=slow_ronghui),
            patch(
                "agent.tms_runtime.monitoring.get_session_broker",
                return_value=type("Broker", (), {"validate_health_matrix": lambda self: {"status": "ok"}})(),
            ),
        ):
            started = time.perf_counter()
            result = monitoring.build_monitoring_snapshot(systems=["yunda", "ronghui"], force=True)
            elapsed = time.perf_counter() - started

        self.assertEqual(3, result["totals"]["total_pending"])
        self.assertLess(elapsed, 0.35)

    def test_forced_monitoring_does_not_validate_capability_health_matrix(self):
        target_payload = {
            "system": "yunda",
            "status": "ok",
            "total_count": 1,
            "exception_count": 0,
            "categories": [],
        }
        broker = Mock()

        with (
            patch("agent.tms_runtime.monitoring._collect_yunda_snapshot", return_value=target_payload),
            patch("agent.tms_runtime.monitoring.get_session_broker", return_value=broker),
        ):
            result = monitoring._collect_system_snapshot(
                "yunda",
                force=True,
                updated_at="2026-05-28 13:30:00",
            )

        self.assertEqual("ok", result["status"])
        self.assertNotIn("capability_health", result)
        broker.validate_health_matrix.assert_not_called()

    def test_yunda_snapshot_uses_target_calls_without_session_prevalidation(self):
        session = Mock()
        broker = Mock()
        broker.build_requests_session.return_value = session
        categories = [{
            "category_id": "yunda:notifications", "count": 2, "include_in_total": False,
            "status": "pending", "status_label": "待处理", "items_supported": True,
        }]
        with (
            patch("agent.tms_runtime.monitoring.get_session_broker", return_value=broker) as get_broker,
            patch("agent.tms_runtime.monitoring_yunda.collect_categories", return_value=categories) as collect,
        ):
            result = monitoring._collect_yunda_snapshot(
                force=True,
                updated_at="2026-05-28 13:30:00",
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual(categories, result["categories"])
        self.assertIsNone(result["total_count"])
        get_broker.assert_called_once_with("yunda")
        broker.build_requests_session.assert_called_once_with(validate=False)
        broker.validate_health_matrix.assert_not_called()
        collect.assert_called_once_with(session)
        session.close.assert_called_once_with()

    def test_daily_sign_prefer_cached_returns_cached_payload_without_requerying(self):
        with (
            patch(
                "agent.tms_runtime.monitoring.get_workflow_resource",
                return_value={"spreadsheet_token": "sheet-token", "range": "Sheet1!A2:H200"},
            ),
            patch(
                "agent.tms_runtime.monitoring.feishu_operation",
                return_value={"ok": True, "data": {"valueRange": {"values": [["运单编号", "R13应签收时间", "问题件后应签时间"], ["R1", "2026-05-31 23:59:59", ""]]}}},
            ) as feishu_op,
            patch("agent.tms_runtime.monitoring.list_scheduled_tasks", return_value=[]),
            patch("agent.tms_runtime.monitoring._schedule_daily_sign_refresh") as schedule_refresh,
        ):
            first = monitoring.build_daily_sign_monitoring_snapshot(target_date="2026-05-31", force=True)
            second = monitoring.build_daily_sign_monitoring_snapshot(
                target_date="2026-05-31",
                force=True,
                prefer_cached=True,
            )

        self.assertEqual(1, first["counts"]["unsigned_today"])
        self.assertEqual(first["counts"], second["counts"])
        self.assertTrue(second["cached"])
        self.assertTrue(second["refreshing"])
        self.assertEqual(1, feishu_op.call_count)
        schedule_refresh.assert_called_once()


class MonitoringRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_snapshot_route_passes_systems_and_force(self):
        expected = {
            "ok": True,
            "updated_at": "2026-05-28 13:30:00",
            "poll_interval_sec": 60,
            "totals": {"total_pending": 2, "yunda_pending": 2, "ronghui_pending": 0, "exception_pending": 2},
            "systems": [],
        }

        with patch("agent.tms_runtime.routes.build_monitoring_snapshot", return_value=expected) as mocked:
            response = self.client.get("/admin/monitoring/snapshot?systems=yunda,ronghui&force=1")

        self.assertEqual(200, response.status_code)
        self.assertEqual(expected, response.json())
        mocked.assert_called_once_with(systems=["yunda", "ronghui"], force=True, prefer_cached=False)

    def test_snapshot_route_passes_prefer_cached(self):
        expected = {
            "ok": True,
            "updated_at": "2026-05-28 13:30:00",
            "poll_interval_sec": 60,
            "cached": True,
            "stale": True,
            "refreshing": True,
            "cache_age_sec": 10,
            "totals": {"total_pending": 2, "yunda_pending": 2, "ronghui_pending": 0, "exception_pending": 2},
            "systems": [],
        }

        with patch("agent.tms_runtime.routes.build_monitoring_snapshot", return_value=expected) as mocked:
            response = self.client.get("/admin/monitoring/snapshot?systems=yunda,ronghui&force=1&prefer_cached=1")

        self.assertEqual(200, response.status_code)
        self.assertEqual(expected, response.json())
        mocked.assert_called_once_with(systems=["yunda", "ronghui"], force=True, prefer_cached=True)

    def test_daily_sign_route_passes_force_and_target_date(self):
        expected = {
            "ok": True,
            "status": "ok",
            "target_date": "2026-05-31",
            "updated_at": "2026-05-31 16:30:00",
            "poll_interval_sec": 60,
            "counts": {"unsigned_today": 3},
            "message": "飞书应签明细",
        }

        with patch("agent.tms_runtime.routes.build_daily_sign_monitoring_snapshot", return_value=expected) as mocked:
            response = self.client.get("/admin/monitoring/daily-sign?force=1&target_date=2026-05-31")

        self.assertEqual(200, response.status_code)
        self.assertEqual(expected, response.json())
        mocked.assert_called_once_with(force=True, target_date="2026-05-31", prefer_cached=False)

    def test_daily_sign_route_passes_prefer_cached(self):
        expected = {
            "ok": True,
            "status": "ok",
            "target_date": "2026-05-31",
            "updated_at": "2026-05-31 16:30:00",
            "poll_interval_sec": 60,
            "cached": True,
            "refreshing": True,
            "cache_age_sec": 10,
            "counts": {"unsigned_today": 3},
            "message": "飞书应签明细",
        }

        with patch("agent.tms_runtime.routes.build_daily_sign_monitoring_snapshot", return_value=expected) as mocked:
            response = self.client.get("/admin/monitoring/daily-sign?force=1&target_date=2026-05-31&prefer_cached=1")

        self.assertEqual(200, response.status_code)
        self.assertEqual(expected, response.json())
        mocked.assert_called_once_with(force=True, target_date="2026-05-31", prefer_cached=True)

    def test_detail_link_route_returns_safe_link(self):
        safe_url = "https://tms.ronghuiwl.com/module/index?mv=index"
        snapshot = {"systems": [{"system": "ronghui", "categories": [{
            "category_id": "ronghui:announcements", "title": "未读公告与消息", "open_url": safe_url,
        }]}]}
        with patch("agent.tms_runtime.monitoring.build_monitoring_snapshot", return_value=snapshot) as collect:
            response = self.client.post(
                "/admin/monitoring/detail-link",
                json={
                    "system": "ronghui", "category_id": "ronghui:announcements",
                    "title": "未受信标题", "resource_id": "99999", "url": "https://example.invalid/",
                },
            )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual("ronghui", payload["system"])
        self.assertEqual("external", payload["mode"])
        self.assertEqual(safe_url, payload["open_url"])
        self.assertEqual("未读公告与消息", payload["title"])
        collect.assert_called_once_with(systems=["ronghui"])


if __name__ == "__main__":
    unittest.main()
