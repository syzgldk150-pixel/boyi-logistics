import io
import json
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from console.app import LocalDocFlowApp
from console.finance_service import (
    FinanceNotFoundError,
    FinancePeriodRequestError,
    FinanceService,
    FinanceValidationError,
)
from console.routes import finance as finance_routes
from shared.finance import FinancePeriodError
from shared.identity_permissions import SUPER_ADMIN
from shared.identity_routes import console_permission


CONSOLE_DIR = Path(__file__).resolve().parents[1]
REQUEST_UUID = "123e4567-e89b-42d3-a456-426614174000"


class _Handler:
    def __init__(self, body=None, *, role="super_admin", header_uuid=REQUEST_UUID):
        payload = json.dumps(body if body is not None else {"request_id": REQUEST_UUID}, ensure_ascii=False).encode("utf-8")
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/json",
            "X-Browser-Request-UUID": header_uuid,
        }
        self.rfile = io.BytesIO(payload)
        self.wfile = io.BytesIO()
        self.current_admin_user = {"id": 7, "username": "finance-admin", "control_plane_role": role}


class _FakeService:
    def __init__(self):
        self.calls = []

    def get_period(self, month):
        self.calls.append(("get", month))
        return {"month": month, "status": "open"}

    def confirm_period_items(self, month, body, *, actor):
        self.calls.append(("confirm", month, body, actor))
        return {"confirmed": body["items"]}

    def close_period(self, month, body, *, actor):
        self.calls.append(("close", month, body, actor))
        return {"version": 1}

    def reopen_period(self, month, body, *, actor):
        self.calls.append(("reopen", month, body, actor))
        return {"status": "reopened"}

    def acknowledge_anomaly(self, anomaly_id, body, *, actor):
        self.calls.append(("acknowledge", anomaly_id, body, actor))
        return {"status": "acknowledged"}


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append((name, args))


class FinancePeriodConsoleTests(unittest.TestCase):
    def setUp(self):
        self.app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        self.app.settings = SimpleNamespace(app_title="ShipNow", agent_timeout_seconds=5)
        self.app.finance_service = _FakeService()
        self.sent = []

        def send_json(app, handler, status, payload):
            self.sent.append((status, payload))

        self.app._send_json = types.MethodType(send_json, self.app)
        self.app._control_plane_write_context = lambda _handler: {"actor": {"actor_id": "7"}}

    def test_routes_dispatch_period_paths_only(self):
        recorder = _Recorder()
        self.assertTrue(finance_routes.handle_get(recorder, None, "/finance/periods/2026-09", "", {}))
        for path, action, target in (
            ("/finance/periods/2026-09/confirmations", "confirm", "2026-09"),
            ("/finance/periods/2026-09/close", "close", "2026-09"),
            ("/finance/periods/2026-09/reopen", "reopen", "2026-09"),
            ("/finance/anomalies/42/acknowledge", "acknowledge", "42"),
        ):
            self.assertTrue(finance_routes.handle_post(recorder, None, path, "", {}))
            self.assertEqual(("_handle_finance_period_post", (None, action, target)), recorder.calls[-1])
        self.assertEqual(("_handle_finance_period_get", (None, "2026-09")), recorder.calls[0])
        query = {"capture": [REQUEST_UUID]}
        self.assertTrue(finance_routes.handle_get(recorder, None, "/finance/periods/2026-09/platform-details", "", query))
        self.assertEqual(("_handle_finance_period_details_get", (None, "2026-09", query)), recorder.calls[-1])
        self.assertFalse(finance_routes.handle_get(recorder, None, "/finance/periods/2026-9", "", {}))
        self.assertFalse(finance_routes.handle_post(recorder, None, "/finance/periods/2026-09/delete", "", {}))

    def test_close_and_reopen_require_super_admin_route_permission(self):
        self.assertEqual(SUPER_ADMIN, console_permission("POST", "/finance/periods/2026-09/close"))
        self.assertEqual(SUPER_ADMIN, console_permission("POST", "/finance/periods/2026-09/reopen"))
        self.assertEqual("finance.manage", console_permission("POST", "/finance/periods/2026-09/confirmations"))
        self.assertEqual("finance.manage", console_permission("POST", "/finance/anomalies/3/acknowledge"))
        self.assertEqual("finance.read", console_permission("GET", "/finance/periods/2026-09"))
        self.assertEqual("finance.read", console_permission("GET", "/finance/periods/2026-09/platform-details"))

    def test_view_reports_whether_the_viewer_may_close(self):
        self.app._handle_finance_period_get(_Handler(role="admin"), "2026-09")
        status, payload = self.sent[-1]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertFalse(payload["data"]["viewer"]["can_close"])
        self.app._handle_finance_period_get(_Handler(), "2026-09")
        self.assertTrue(self.sent[-1][1]["data"]["viewer"]["can_close"])

    def test_admin_cannot_close_or_reopen_but_may_confirm(self):
        for action in ("close", "reopen"):
            self.app._handle_finance_period_post(_Handler(role="admin"), action, "2026-09")
            status, payload = self.sent[-1]
            self.assertEqual(HTTPStatus.FORBIDDEN, status)
            self.assertEqual("SUPER_ADMIN_REQUIRED", payload["error_code"])
        self.assertEqual([], self.app.finance_service.calls)
        body = {"request_id": REQUEST_UUID, "items": [{"fee_item_id": 1, "mapping_id": 2}]}
        self.app._handle_finance_period_post(_Handler(body, role="admin"), "confirm", "2026-09")
        self.assertEqual(HTTPStatus.OK, self.sent[-1][0])
        name, month, sent_body, actor = self.app.finance_service.calls[-1]
        self.assertEqual(("confirm", "2026-09", "finance-admin"), (name, month, actor))
        self.assertEqual(REQUEST_UUID, sent_body["request_id"])

    def test_request_uuid_must_match_the_browser_header(self):
        self.app._handle_finance_period_post(
            _Handler(header_uuid="00000000-0000-4000-8000-000000000000"), "close", "2026-09")
        status, payload = self.sent[-1]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual("INVALID_REQUEST", payload["error_code"])
        self.assertEqual([], self.app.finance_service.calls)

    def test_super_admin_close_reopen_and_acknowledge_reach_the_service(self):
        self.app._handle_finance_period_post(_Handler(), "close", "2026-09")
        self.app._handle_finance_period_post(_Handler({"request_id": REQUEST_UUID, "reason": "补录"}), "reopen", "2026-09")
        self.app._handle_finance_period_post(_Handler({"request_id": REQUEST_UUID, "reason": "无单号"}), "acknowledge", "42")
        self.assertEqual(["close", "reopen", "acknowledge"], [call[0] for call in self.app.finance_service.calls])
        self.assertEqual(42, self.app.finance_service.calls[-1][1])
        self.assertTrue(all(status == HTTPStatus.OK for status, _payload in self.sent))

    def test_ledger_refusals_keep_their_code_status_and_details(self):
        class Repository:
            def get_summary(self, *_args, **_kwargs):
                return {}

            def close_period(self, *_args, **_kwargs):
                raise FinancePeriodError("PERIOD_CHECKS_FAILED", "封账前检查未全部通过。", details=[{"code": "coverage"}])

            def reopen_period(self, *_args, **_kwargs):
                raise FinancePeriodError("PERIOD_NOT_CLOSED", "该月尚未封账。")

        service = FinanceService(Repository())
        with self.assertRaises(FinancePeriodRequestError) as failed:
            service.close_period("2026-09", {"request_id": REQUEST_UUID}, actor="finance-admin")
        self.assertEqual((422, "PERIOD_CHECKS_FAILED"), (failed.exception.http_status, failed.exception.error_code))
        self.app._send_finance_error(_Handler(), failed.exception)
        self.assertEqual([{"code": "coverage"}], self.sent[-1][1]["details"])
        with self.assertRaises(FinancePeriodRequestError) as conflict:
            service.reopen_period("2026-09", {"request_id": REQUEST_UUID, "reason": "补录"}, actor="finance-admin")
        self.assertEqual(409, conflict.exception.http_status)

    def test_platform_details_validate_the_query_before_reading(self):
        calls = []

        class Repository:
            def get_summary(self, *_args, **_kwargs):
                return {}

            def platform_month_details(self, month, **kwargs):
                calls.append((month, kwargs))
                if kwargs["item_name"] == "不存在":
                    raise LookupError("capture does not belong to this month")
                return {"month": month, "rows": []}

        service = FinanceService(Repository())
        query = {"capture": [REQUEST_UUID], "item": ["派送费"], "basis": ["billing"], "offset": ["50"]}
        self.assertEqual({"month": "2026-09", "rows": []}, service.get_period_platform_details("2026-09", query))
        self.assertEqual(("2026-09", {"capture_key": REQUEST_UUID, "item_name": "派送费", "basis": "billing",
                                      "offset": 50, "limit": 50}), calls[-1])
        for bad in ({**query, "capture": ["not-a-uuid"]}, {**query, "capture": [REQUEST_UUID.upper()]},
                    {**query, "item": [""]}, {**query, "basis": ["other"]}, {**query, "offset": ["-1"]},
                    {**query, "item": ["a", "b"]}):
            with self.assertRaises(FinanceValidationError):
                service.get_period_platform_details("2026-09", bad)
        self.assertEqual(1, len(calls))
        with self.assertRaises(FinanceNotFoundError):
            service.get_period_platform_details("2026-09", {**query, "item": ["不存在"]})

    def test_page_has_the_period_tab_dialog_and_uuid_requests(self):
        template = (CONSOLE_DIR / "templates" / "finance.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "finance.js").read_text(encoding="utf-8")
        self.assertIn('data-finance-tab="periods"', template)
        self.assertIn('data-finance-panel="periods"', template)
        self.assertIn("<dialog", template)
        self.assertIn('periods: "/finance/periods"', script)
        self.assertIn('"X-Browser-Request-UUID": requestId', script)
        self.assertIn("showModal()", script)
        self.assertIn("data-finance-platform-dialog", template)
        self.assertIn("data-finance-platform-basis", template)
        self.assertIn("/platform-details?", script)


if __name__ == "__main__":
    unittest.main()
