import io
import json
import re
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, select_autoescape


CONSOLE_DIR = Path(__file__).resolve().parents[1]
BROWSER_REQUEST_UUID = "123e4567-e89b-42d3-a456-426614174000"

from console.app import LocalDocFlowApp
from console.finance_service import FinanceValidationError
from console.navigation import CONSOLE_NAVIGATION


class _Handler:
    def __init__(self, body=None):
        payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/json",
            "X-Browser-Request-UUID": BROWSER_REQUEST_UUID,
        }
        self.rfile = io.BytesIO(payload)
        self.wfile = io.BytesIO()


class _FakeFinanceService:
    def __init__(self):
        self.calls = []
        self.fail_next = False

    def _result(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.fail_next:
            self.fail_next = False
            raise FinanceValidationError("筛选条件无效。")
        return {"resource": name}

    def get_summary(self, query):
        return self._result("summary", query)

    def get_trend(self, query):
        return self._result("trend", query)

    def list_entries(self, query):
        return self._result("entries", query)

    def list_fee_mappings(self, query):
        return self._result("fee_mappings", query)

    def list_sync_batches(self, query):
        return self._result("sync_batches", query)

    def list_review_cases(self, query):
        return self._result("review_cases", query)

    def list_waybill_facts(self, query):
        return self._result("waybill_facts", query)

    def knowledge_status(self):
        return self._result("knowledge")

    def build_sync_arguments(self, body):
        return self._result("sync", body)

    def build_backfill_arguments(self, body):
        return self._result("backfill", body)

    def save_fee_mapping(self, fee_item_id, body, *, changed_by):
        return self._result("save_mapping", fee_item_id, body, changed_by=changed_by)

    def build_retry_arguments(self, batch_id):
        return self._result("retry_batch", batch_id)

    def analyze_review_cases(self, body):
        return self._result("analyze_reviews", body)

    def reject_review_case(self, review_case_id, body, *, changed_by):
        return self._result("reject_review", review_case_id, body, changed_by=changed_by)


class FinanceModuleWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        self.app.settings = SimpleNamespace(app_title="ShipNow")
        self.app.finance_service = _FakeFinanceService()
        self.app.template_env = Environment(
            loader=FileSystemLoader(CONSOLE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.app.template_env.globals["current_admin_user"] = lambda: None
        self.sent_status = None
        self.sent_payload = None
        self.sent_html = ""
        self.command_submissions = []

        def send_json(app, handler, status, payload):
            self.sent_status = status
            self.sent_payload = payload

        def send_html(app, handler, body, status=HTTPStatus.OK):
            self.sent_status = status
            self.sent_html = body

        self.app._send_json = types.MethodType(send_json, self.app)
        self.app._send_html = types.MethodType(send_html, self.app)
        self.app._control_plane_write_context = lambda _handler: {
            "actor": {"actor_id": "7"},
            "actor_roles": ["super_admin"],
            "source": "console",
            "_console_principal": {"actor_id": "7"},
        }

        def submit_command(app, **kwargs):
            self.command_submissions.append(kwargs)
            return {
                "ok": True,
                "status": HTTPStatus.ACCEPTED,
                "data": {
                    "command_id": "command-1",
                    "work_item_id": "work-item-1",
                    "run_id": "run-1",
                    "status": "RECEIVED",
                    "reused": False,
                    "next_poll_after_ms": 1000,
                },
            }

        def send_receipt(app, handler, result, *, message):
            self.sent_status = HTTPStatus.ACCEPTED
            self.sent_payload = {
                "ok": True,
                "pending": True,
                "message": message,
                **result["data"],
            }

        self.app._submit_console_tool_command = types.MethodType(submit_command, self.app)
        self.app._send_console_command_receipt = types.MethodType(send_receipt, self.app)

    def test_sidebar_links_to_dedicated_finance_workbench(self):
        item = next(item for item in CONSOLE_NAVIGATION if item["route"] == "/modules/finance")

        self.assertEqual("dollar-sign", item["icon"])
        self.assertEqual("财务模块", item["label"])

    def test_finance_route_renders_specialized_six_tab_workbench(self):
        self.app._render_finance(_Handler(), {})

        self.assertEqual(HTTPStatus.OK, self.sent_status)
        self.assertIn("data-finance-workbench", self.sent_html)
        self.assertIn('data-finance-tab="overview"', self.sent_html)
        self.assertIn('data-finance-tab="entries"', self.sent_html)
        self.assertIn('data-finance-tab="mappings"', self.sent_html)
        self.assertIn('data-finance-tab="sync"', self.sent_html)
        self.assertIn('data-finance-tab="reviews"', self.sent_html)
        self.assertIn('data-finance-tab="waybill-facts"', self.sent_html)
        self.assertNotIn("module-detail-sections", self.sent_html)

    def test_finance_assets_are_page_scoped_and_no_chart_library_is_global(self):
        base = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        template = (CONSOLE_DIR / "templates" / "finance.html").read_text(encoding="utf-8")

        self.assertIn('/static/finance.css', template)
        self.assertIn('/static/finance.js', template)
        self.assertNotIn('/static/finance.js', base)
        self.assertNotIn("chart.js", template.lower())
        self.assertNotIn("echarts", template.lower())

    def test_overview_has_svg_charts_and_equivalent_data_tables(self):
        template = (CONSOLE_DIR / "templates" / "finance.html").read_text(encoding="utf-8")

        self.assertIn('data-finance-trend-chart', template)
        self.assertIn('role="img"', template)
        self.assertIn('data-finance-trend-table', template)
        self.assertIn('data-finance-ranking-table', template)
        self.assertIn('data-finance-account-table', template)
        self.assertIn("金额单位：元", template)

    def test_rendered_platform_controls_only_expose_enabled_ronghui(self):
        self.app._render_finance(_Handler(), {})
        rendered = self.sent_html
        script = (CONSOLE_DIR / "static" / "finance.js").read_text(encoding="utf-8")

        platform_selects = re.findall(
            r'<select (?:data-finance-platform|name="platform")>(.*?)</select>',
            rendered,
            flags=re.DOTALL,
        )
        self.assertEqual(5, len(platform_selects))
        for options in platform_selects:
            self.assertEqual(["all", "ronghui"], re.findall(r'<option value="([^"]+)">', options))
        self.assertIn(
            'id="finance-booking-ronghui" data-finance-booking-fee-items="ronghui"',
            rendered,
        )
        self.assertNotIn('data-finance-booking-fee-items="yunda"', rendered)
        self.assertIn("payload.booking_fee_items", script)
        self.assertNotIn("集配站费用", rendered)
        self.assertNotIn("增值服务费", rendered)
        self.assertNotIn("平台费", rendered)

    def test_frontend_keeps_yunda_label_only_for_historical_sync_records(self):
        script = (CONSOLE_DIR / "static" / "finance.js").read_text(encoding="utf-8")

        self.assertIn('yunda: "韵达"', script)
        self.assertIn(
            "failedSources.map((source) => `${platformLabel(source.platform)}",
            script,
        )

    def test_accessibility_and_responsive_states_are_present(self):
        template = (CONSOLE_DIR / "templates" / "finance.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "finance.js").read_text(encoding="utf-8")
        css = (CONSOLE_DIR / "static" / "finance.css").read_text(encoding="utf-8")

        self.assertIn('role="tablist"', template)
        self.assertIn('role="tabpanel"', template)
        self.assertIn('aria-live="polite"', template)
        self.assertIn('role="alert"', template)
        self.assertIn('data-finance-partial-warning', template)
        self.assertIn("finance-skeleton-line", template)
        self.assertIn("当前筛选范围没有趋势数据", template)
        self.assertIn('event.key === "ArrowRight"', script)
        self.assertIn('event.key === "Home"', script)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertIn("{% block body_class %}finance-page{% endblock %}", template)
        self.assertIn("@media (max-width: 900px)", css)
        self.assertIn("body.finance-page .app-shell", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", css)
        self.assertIn("body.finance-page .sidebar .nav-menu", css)
        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn("finance-table--responsive", css)

    def test_frontend_uses_server_plot_values_not_money_arithmetic(self):
        script = (CONSOLE_DIR / "static" / "finance.js").read_text(encoding="utf-8")

        self.assertIn('row[`${key}_plot`]', script)
        self.assertIn("waybill_net_plot", script)
        self.assertIn("operating_net_plot", script)
        self.assertNotIn("parseFloat(row.income", script)
        self.assertNotIn("parseFloat(row.expense", script)
        self.assertNotIn("total_income +", script)

    def test_frontend_submits_stable_browser_uuid_and_uses_run_receipt(self):
        script = (CONSOLE_DIR / "static" / "finance.js").read_text(encoding="utf-8")

        self.assertIn('"X-Browser-Request-UUID": newBrowserRequestUuid()', script)
        self.assertIn("receipt?.run_id", script)
        self.assertIn("事项中心完成审批", script)
        self.assertNotIn("await loadBatches();\n        await loadOverview();", script)

    def test_finance_api_routes_include_review_fact_and_knowledge_workflows(self):
        source = (CONSOLE_DIR / "routes" / "finance.py").read_text(encoding="utf-8")
        routes = (
            "/finance/summary",
            "/finance/trend",
            "/finance/entries",
            "/finance/fee-mappings",
            "/finance/sync-batches",
            "/finance/sync",
            "/finance/backfill",
            "/finance/fee-mappings/\\d+",
            "/finance/sync-batches/\\d+/retry",
            "/finance/review-cases",
            "/finance/waybill-facts",
            "/finance/knowledge",
            "/finance/reviews/analyze",
            "/finance/review-cases/\\d+/reject",
        )

        for route in routes:
            self.assertIn(route, source)

    def test_finance_schema_is_initialized_during_app_startup(self):
        source = (CONSOLE_DIR / "app.py").read_text(encoding="utf-8")

        self.assertIn("self.finance_service.initialize_schema()", source)

    def test_get_handlers_return_consistent_success_envelope(self):
        for resource in ("summary", "trend", "entries", "fee_mappings", "sync_batches"):
            with self.subTest(resource=resource):
                self.app._handle_finance_get(_Handler(), resource, {"platform": ["ronghui"]})
                self.assertEqual(HTTPStatus.OK, self.sent_status)
                self.assertTrue(self.sent_payload["ok"])
                self.assertEqual(resource, self.sent_payload["data"]["resource"])

    def test_post_handlers_cover_sync_backfill_mapping_and_retry(self):
        self.app._handle_finance_post(_Handler({"rescan_days": 7}), "sync")
        self.assertEqual(HTTPStatus.ACCEPTED, self.sent_status)
        self.assertTrue(self.sent_payload["pending"])
        self.assertEqual("sync_finance_bills", self.command_submissions[-1]["tool_name"])
        self.assertEqual("sync", self.command_submissions[-1]["arguments"]["resource"])
        self.assertEqual(BROWSER_REQUEST_UUID, self.command_submissions[-1]["browser_request_uuid"])

        self.app._handle_finance_post(
            _Handler({"start_date": "2026-07-01", "end_date": "2026-07-11"}),
            "backfill",
        )
        self.assertEqual(HTTPStatus.ACCEPTED, self.sent_status)
        self.assertEqual("backfill", self.command_submissions[-1]["arguments"]["resource"])

        with patch(
            "console.services.monitoring_finance.current_admin_user",
            return_value={"username": "admin"},
        ):
            self.app._handle_finance_post(
                _Handler({"fee_level": "operating"}),
                "save_mapping",
                path="/finance/fee-mappings/12",
            )
        self.assertEqual("save_mapping", self.sent_payload["data"]["resource"])
        self.assertEqual(12, self.app.finance_service.calls[-1][1][0])

        self.app._handle_finance_post(
            _Handler(),
            "retry_batch",
            path="/finance/sync-batches/8/retry",
        )
        self.assertEqual(HTTPStatus.ACCEPTED, self.sent_status)
        self.assertEqual("retry_batch", self.command_submissions[-1]["arguments"]["resource"])

    def test_validation_errors_are_readable_json(self):
        self.app.finance_service.fail_next = True

        self.app._handle_finance_get(_Handler(), "summary", {})

        self.assertEqual(HTTPStatus.BAD_REQUEST, self.sent_status)
        self.assertFalse(self.sent_payload["ok"])
        self.assertEqual("FINANCE_VALIDATION_ERROR", self.sent_payload["error_code"])
        self.assertEqual("筛选条件无效。", self.sent_payload["message"])


if __name__ == "__main__":
    unittest.main()
