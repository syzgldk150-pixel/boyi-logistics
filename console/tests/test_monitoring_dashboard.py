import io
import json
import sys
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import LocalDocFlowApp  # noqa: E402


class _JsonHandler:
    pass


class _StreamHandler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        self.headers.append(("__end__", ""))


class _PortalTemplateEnv:
    def __init__(self):
        self.context = None

    def get_template(self, name):
        if name != "portal.html":
            raise AssertionError(f"unexpected template: {name}")
        env = self

        class _Template:
            def render(self, **context):
                env.context = context
                return "portal"

        return _Template()


class MonitoringConsoleTests(unittest.TestCase):
    def _build_app(self, agent_payload=None):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_base_url="http://agent.test", agent_timeout_seconds=30)
        app._agent_calls = []
        app._agent_payload = agent_payload or {
            "ok": True,
            "updated_at": "2026-05-28 13:30:00",
            "poll_interval_sec": 60,
            "totals": {"total_pending": 7, "yunda_pending": 7, "ronghui_pending": 0, "exception_pending": 7},
            "systems": [],
        }

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self._agent_calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            return {"ok": True, "status": 200, "data": self._agent_payload}

        def capture_json(self, handler, status, payload):
            self.sent_status = status
            self.sent_payload = payload

        app._agent_request = types.MethodType(agent_request, app)
        app._send_json = types.MethodType(capture_json, app)
        return app

    def test_summary_proxies_agent_snapshot(self):
        app = self._build_app()

        app._handle_monitoring_summary(_JsonHandler(), {"systems": ["yunda,ronghui"], "force": ["1"]})

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(7, app.sent_payload["totals"]["total_pending"])
        self.assertEqual("/internal/v1/admin/monitoring/snapshot?systems=yunda%2Cronghui&force=1", app._agent_calls[0]["endpoint"])

    def test_summary_passes_prefer_cached_to_agent(self):
        app = self._build_app(
            {
                "ok": True,
                "updated_at": "2026-05-28 13:30:00",
                "poll_interval_sec": 60,
                "cached": True,
                "stale": True,
                "refreshing": True,
                "cache_age_sec": 12,
                "totals": {"total_pending": 7, "yunda_pending": 7, "ronghui_pending": 0, "exception_pending": 7},
                "systems": [],
            }
        )

        app._handle_monitoring_summary(
            _JsonHandler(),
            {"systems": ["yunda,ronghui"], "force": ["1"], "prefer_cached": ["1"]},
        )

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertTrue(app.sent_payload["refreshing"])
        self.assertEqual(
            "/internal/v1/admin/monitoring/snapshot?systems=yunda%2Cronghui&force=1&prefer_cached=1",
            app._agent_calls[0]["endpoint"],
        )

    def test_daily_sign_proxies_agent_snapshot(self):
        app = self._build_app(
            {
                "ok": True,
                "status": "ok",
                "target_date": "2026-05-31",
                "updated_at": "2026-05-31 16:30:00",
                "poll_interval_sec": 60,
                "counts": {"unsigned_today": 3},
                "message": "飞书应签明细",
            }
        )

        app._handle_monitoring_daily_sign(_JsonHandler(), {"force": ["1"], "target_date": ["2026-05-31"]})

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual(3, app.sent_payload["counts"]["unsigned_today"])
        self.assertEqual(
            "/internal/v1/admin/monitoring/daily-sign?force=1&target_date=2026-05-31",
            app._agent_calls[0]["endpoint"],
        )

    def test_daily_sign_passes_prefer_cached_to_agent(self):
        app = self._build_app(
            {
                "ok": True,
                "status": "ok",
                "target_date": "2026-05-31",
                "updated_at": "2026-05-31 16:30:00",
                "poll_interval_sec": 60,
                "cached": True,
                "refreshing": True,
                "cache_age_sec": 8,
                "counts": {"unsigned_today": 3},
                "message": "飞书应签明细",
            }
        )

        app._handle_monitoring_daily_sign(
            _JsonHandler(),
            {"force": ["1"], "target_date": ["2026-05-31"], "prefer_cached": ["1"]},
        )

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertTrue(app.sent_payload["refreshing"])
        self.assertEqual(
            "/internal/v1/admin/monitoring/daily-sign?force=1&target_date=2026-05-31&prefer_cached=1",
            app._agent_calls[0]["endpoint"],
        )

    def test_detail_link_get_proxies_agent_post(self):
        app = self._build_app(
            {
                "ok": True,
                "system": "yunda",
                "category_id": "yunda:001",
                "mode": "iframe",
                "embed_url": "https://ky-client.yunda56.com/#/ifarme/ifarme/4768/%E9%97%AE%E9%A2%98%E4%BB%B6",
                "open_url": "https://ky-client.yunda56.com/#/ifarme/ifarme/4768/%E9%97%AE%E9%A2%98%E4%BB%B6",
            }
        )

        app._handle_monitoring_detail_link(
            _JsonHandler(),
            {"system": ["yunda"], "category_id": ["yunda:001"], "resource_id": ["4768"], "title": ["问题件消息"]},
        )

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("iframe", app.sent_payload["mode"])
        self.assertEqual("POST", app._agent_calls[0]["method"])
        self.assertEqual("/internal/v1/admin/monitoring/detail-link", app._agent_calls[0]["endpoint"])
        self.assertEqual("yunda:001", app._agent_calls[0]["payload"]["category_id"])

    def test_stream_can_send_one_snapshot_event(self):
        app = self._build_app()
        handler = _StreamHandler()

        app._handle_monitoring_stream(handler, {"once": ["1"]})

        body = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertIn(("Content-Type", "text/event-stream; charset=utf-8"), handler.headers)
        self.assertIn("event: snapshot", body)
        self.assertIn('"total_pending": 7', body)

    def test_portal_render_skips_legacy_home_aggregations(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(app_title="Console")
        app.template_env = _PortalTemplateEnv()

        class _Repository:
            def list_documents(self, *args, **kwargs):
                raise AssertionError("list_documents should not be called for monitoring portal")

            def count_by_status(self):
                raise AssertionError("count_by_status should not be called for monitoring portal")

        class _TaskQueue:
            def snapshot(self):
                raise AssertionError("task queue snapshot should not be called for monitoring portal")

        app.repository = _Repository()
        app.task_queue = _TaskQueue()
        app._build_module_view_models = types.MethodType(
            lambda self, counts: (_ for _ in ()).throw(AssertionError("_build_module_view_models should not run")),
            app,
        )
        app._build_relationship_cards = types.MethodType(
            lambda self: (_ for _ in ()).throw(AssertionError("_build_relationship_cards should not run")),
            app,
        )

        def capture_html(self, handler, body):
            self.sent_body = body

        app._send_html = types.MethodType(capture_html, app)
        app._render_portal(_JsonHandler(), {})

        self.assertEqual("portal", app.sent_body)
        self.assertEqual("Console", app.template_env.context["app_title"])

    def test_portal_template_has_monitoring_realtime_hooks(self):
        template = (CONSOLE_DIR / "templates" / "portal.html").read_text(encoding="utf-8")

        self.assertIn("data-monitoring-dashboard", template)
        self.assertIn("new EventSource('/monitoring/stream')", template)
        self.assertIn("/monitoring/detail-link", template)
        self.assertIn("data-monitoring-daily-sign", template)
        self.assertIn("/monitoring/daily-sign", template)
        self.assertIn("今日应签未签", template)
        self.assertIn("飞书应签明细", template)
        self.assertIn("scheduleDailySignRefresh", template)
        self.assertIn("payload?.poll_interval_sec", template)
        self.assertNotIn("R13 实时未签明细", template)
        self.assertIn("AbortController", template)
        self.assertIn("detailLinkCache", template)
        self.assertIn("data-monitoring-current-src", template)
        self.assertIn("addEventListener('click', (event)", template)
        self.assertIn("prefer_cached=1", template)
        self.assertIn("data-monitoring-agent-status", template)
        self.assertIn("renderSummaryUnavailable", template)
        self.assertIn("Agent 服务不可用", template)
        self.assertIn("monitoringRequestTimeoutMs = 8000", template)
        self.assertIn("fetchMonitoringJson", template)
        self.assertIn("signal: controller.signal", template)
        self.assertNotIn(">--<", template)
        self.assertNotIn("catch(() => undefined)", template)

    def test_portal_template_does_not_render_live_status_pill(self):
        template = (CONSOLE_DIR / "templates" / "portal.html").read_text(encoding="utf-8")

        self.assertNotIn("monitor-status-row", template)
        self.assertNotIn("monitor-live", template)
        self.assertNotIn("data-monitoring-live", template)
        self.assertNotIn("setLive(", template)


if __name__ == "__main__":
    unittest.main()
