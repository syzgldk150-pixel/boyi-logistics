import io
import json
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import (  # noqa: E402
    LocalDocFlowApp,
    QuoteOptionsValidationError,
    build_manual_quote_options,
    parse_quote_options_request,
)


class _JsonHandler:
    def __init__(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        return None

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class ManualQuoteOptionsTests(unittest.TestCase):
    def test_ronghui_dispatch_uses_lowest_dispatch_product(self):
        payload = build_manual_quote_options(
            ronghui_result={
                "目的网点": "杭州余杭",
                "融惠达": "80.00",
                "融惠达(派送)": "126.50",
                "精准零担(派送)": "118.20",
            },
            yunda_result={"韵达派送": "135.00", "目的网点": "杭州韵达"},
            delivery_method="送货",
        )

        ronghui = next(item for item in payload["quotes"] if item["provider"] == "ronghui")
        self.assertEqual("available", ronghui["status"])
        self.assertEqual("118.20", ronghui["price"])
        self.assertEqual("精准零担", ronghui["product_name"])
        self.assertEqual("ronghui", payload["best_provider"])

    def test_ronghui_self_pickup_ignores_dispatch_prices(self):
        payload = build_manual_quote_options(
            ronghui_result={
                "目的网点": "杭州余杭",
                "融惠达": "88.00",
                "精准零担": "90.00",
                "融惠达(派送)": "70.00",
            },
            yunda_result={"error": "韵达不可达"},
            delivery_method="自提",
        )

        ronghui = next(item for item in payload["quotes"] if item["provider"] == "ronghui")
        self.assertEqual("available", ronghui["status"])
        self.assertEqual("88.00", ronghui["price"])
        self.assertEqual("融惠达", ronghui["product_name"])

    def test_yunda_uses_selected_delivery_method_price(self):
        payload = build_manual_quote_options(
            ronghui_result={"网点不可达": "网点不可达"},
            yunda_result={"韵达自提": "52.00", "韵达派送": "60.00", "目的网点": "杭州韵达"},
            delivery_method="自提",
        )

        yunda = next(item for item in payload["quotes"] if item["provider"] == "yunda")
        self.assertEqual("available", yunda["status"])
        self.assertEqual("52.00", yunda["price"])
        self.assertEqual("韵达自提", yunda["product_name"])
        self.assertEqual("yunda", payload["best_provider"])

    def test_single_provider_failure_keeps_available_provider(self):
        payload = build_manual_quote_options(
            ronghui_result={"error": "融辉登录态失效", "error_code": "AUTH_REQUIRED"},
            yunda_result={"韵达派送": "73.40", "目的网点": "杭州韵达"},
            delivery_method="送货",
        )

        ronghui = next(item for item in payload["quotes"] if item["provider"] == "ronghui")
        self.assertEqual("auth_required", ronghui["status"])
        self.assertIn("登录态", ronghui["error"])
        self.assertEqual(1, payload["available_count"])
        self.assertEqual("yunda", payload["best_provider"])

    def test_both_provider_failures_report_no_available_quote(self):
        payload = build_manual_quote_options(
            ronghui_result={"网点不可达": "网点不可达"},
            yunda_result={"error": "韵达地址不可达"},
            delivery_method="送货",
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(0, payload["available_count"])
        self.assertEqual("", payload["best_provider"])
        self.assertIn("无可用报价", payload["message"])

    def test_missing_weight_or_volume_is_rejected_without_defaults(self):
        with self.assertRaises(QuoteOptionsValidationError):
            parse_quote_options_request(
                {
                    "receiver_address": "浙江省杭州市余杭区文一西路969号",
                    "weight_kg": "",
                    "volume_m3": "0.35",
                    "delivery_method": "送货",
                }
            )

        with self.assertRaises(QuoteOptionsValidationError):
            parse_quote_options_request(
                {
                    "receiver_address": "浙江省杭州市余杭区文一西路969号",
                    "weight_kg": "12",
                    "volume_m3": "abc",
                    "delivery_method": "送货",
                }
            )


class ManualQuoteOptionsEndpointTests(unittest.TestCase):
    def test_quote_options_endpoint_calls_both_agent_price_sources(self):
        app = object.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_timeout_seconds=5)
        calls = []

        def fake_agent_request(method, endpoint, *, payload=None, timeout=None):
            calls.append((method, endpoint, payload, timeout))
            if endpoint == "/tms/get_price":
                return {"ok": True, "status": 200, "data": {"融惠达(派送)": "92.00", "目的网点": "杭州余杭"}}
            if endpoint == "/tms/yunda_price":
                return {"ok": True, "status": 200, "data": {"韵达派送": "88.00", "目的网点": "杭州韵达"}}
            raise AssertionError(endpoint)

        app._agent_request = fake_agent_request
        handler = _JsonHandler(
            {
                "receiver_address": "浙江省杭州市余杭区文一西路969号",
                "weight_kg": "12.5",
                "volume_m3": "0.45",
                "delivery_method": "送货",
            }
        )

        app._handle_quote_options(handler)

        body = handler.json_body()
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("yunda", body["best_provider"])
        self.assertEqual({"/tms/get_price", "/tms/yunda_price"}, {call[1] for call in calls})
        self.assertTrue(all(call[2]["params"]["volume"] == "0.45" for call in calls))

    def test_quote_options_endpoint_rejects_invalid_numbers_before_agent_call(self):
        app = object.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_timeout_seconds=5)
        app._agent_request = lambda *args, **kwargs: self.fail("agent should not be called")
        handler = _JsonHandler(
            {
                "receiver_address": "浙江省杭州市余杭区文一西路969号",
                "weight_kg": "bad",
                "volume_m3": "0.45",
                "delivery_method": "送货",
            }
        )

        app._handle_quote_options(handler)

        self.assertEqual(HTTPStatus.BAD_REQUEST, handler.status)
        self.assertFalse(handler.json_body()["ok"])


if __name__ == "__main__":
    unittest.main()
