import io
import json
import shutil
import subprocess
import sys
import unittest
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import (  # noqa: E402
    LocalDocFlowApp,
    QuoteOptionsValidationError,
    build_manual_quote_options,
    parse_quote_options_request,
)


REQUEST_ID = "11111111-1111-4111-8111-111111111111"
INVOCATION_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"


def _provider(*rows, **extra):
    return {"status": "success", "quotes": [
        {"label": label, "amount": amount, "delivery_method": method}
        for label, amount, method in rows
    ], **extra}


def _request(**changes):
    return {"receiver_address": "测试省测试市示例路1号", "weight_kg": "12.5",
        "volume_m3": "0.045", "delivery_method": "送货", **changes}


def _data():
    return {
        "ronghui": _provider(("融辉产品甲", "92.00", "派送")),
        "yunda": _provider(("韵达派送", "88.00", "派送")),
        "best": _provider(("百世产品甲", "78.00", "派送")),
    }


def _instance(**changes):
    return {"automation_id": PROJECT_ID, "plugin_id": "quote_prices_v2",
        "runtime_model": "SERVICE_V2", "contribution_projection_state": "ACTIVE",
        "enabled": True, "configured": True, "blocked": False,
        "enabled_console_entrypoints": ["quote_console"], **changes}


class _JsonHandler:
    def __init__(self, payload, request_id=REQUEST_ID):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json",
            "X-Browser-Request-UUID": request_id}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class ManualQuoteOptionsTests(unittest.TestCase):
    def test_selects_lowest_product_in_exact_delivery_mode(self):
        data = _data()
        data["ronghui"] = _provider(("融辉产品甲", "10.00", "自提"),
            ("融辉产品乙", "70.00", "派送"), ("融辉产品丙", "72.50", "派送"))
        payload = build_manual_quote_options(plugin_result=data, delivery_method="送货")
        ronghui = next(item for item in payload["quotes"] if item["provider"] == "ronghui")
        self.assertEqual("70.00", ronghui["price"])
        self.assertEqual("融辉产品乙", ronghui["product_name"])
        self.assertEqual("ronghui", payload["best_provider"])
        self.assertEqual(3, payload["available_count"])

    def test_self_pickup_never_uses_dispatch_or_legacy_result(self):
        data = _data()
        data["best"] = _provider(("百世派送", "1.00", "派送"), ("百世自提", "50.00", "自提"))
        data["yunda"] = {"韵达自提": "0.10"}
        payload = build_manual_quote_options(plugin_result=data, delivery_method="自提")
        self.assertEqual(1, payload["available_count"])
        self.assertEqual("best", payload["best_provider"])
        self.assertEqual("50.00", next(item for item in payload["quotes"] if item["provider"] == "best")["price"])

    def test_provider_failure_keeps_other_results_and_yongsheng_pending(self):
        data = _data()
        data["ronghui"] = {"status": "failed", "error": "融辉登录态失效", "error_code": "AUTH_REQUIRED"}
        payload = build_manual_quote_options(plugin_result=data, delivery_method="送货")
        quotes = {item["provider"]: item for item in payload["quotes"]}
        self.assertEqual("auth_required", quotes["ronghui"]["status"])
        self.assertEqual("pending", quotes["yongsheng"]["status"])
        self.assertEqual(2, payload["available_count"])
        self.assertEqual("best", payload["best_provider"])

    def test_invalid_platform_amount_does_not_silently_select_another_product(self):
        data = {"best": _provider(("产品甲", "bad", "派送"), ("产品乙", "20.00", "派送"))}
        payload = build_manual_quote_options(plugin_result=data, delivery_method="派送")
        best = next(item for item in payload["quotes"] if item["provider"] == "best")
        self.assertEqual("invalid_price", best["status"])
        self.assertFalse(payload["ok"])

    def test_missing_delivery_mode_is_invalid(self):
        payload = build_manual_quote_options(plugin_result={"best": {"status": "success",
            "quotes": [{"label": "产品甲", "amount": "20.00"}]}}, delivery_method="派送")
        self.assertFalse(payload["ok"])
        self.assertIn("送货方式", next(item for item in payload["quotes"] if item["provider"] == "best")["error"])

    def test_no_data_and_explicit_zero_are_distinct(self):
        missing = build_manual_quote_options(plugin_result={}, delivery_method="自提")
        self.assertEqual(0, missing["available_count"])
        self.assertEqual("", missing["best_provider"])
        zero = build_manual_quote_options(plugin_result={"best": _provider(("产品甲", "0.00", "自提"))}, delivery_method="自提")
        self.assertEqual(1, zero["available_count"])
        self.assertEqual("best", zero["best_provider"])

    def test_weight_volume_keep_input_precision(self):
        result = parse_quote_options_request(_request(weight_kg="1.235", volume_m3="0.001"))
        self.assertEqual(Decimal("1.235"), result["weight"])
        self.assertEqual(Decimal("0.001"), result["volume"])

    def test_missing_or_blank_volume_is_omitted_for_plugin_default(self):
        missing_volume = _request()
        missing_volume.pop("volume_m3")
        for payload in (missing_volume, _request(volume_m3=""), _request(volume_m3=" \t ")):
            with self.subTest(payload=payload):
                self.assertNotIn("volume", parse_quote_options_request(payload))

    def test_missing_weight_or_explicit_invalid_volume_is_rejected(self):
        for payload in (_request(weight_kg=""), _request(weight_kg="1" * 400),
                *(_request(volume_m3=value) for value in ("bad", "0", 0, "-1", None, True, "1" * 400))):
            with self.subTest(payload=payload), self.assertRaises(QuoteOptionsValidationError):
                parse_quote_options_request(payload)

    def test_volume_alias_is_used_only_when_primary_key_is_missing(self):
        payload = _request(field_volume_m3="0.02")
        payload.pop("volume_m3")
        self.assertEqual(Decimal("0.02"), parse_quote_options_request(payload)["volume"])
        payload["volume_m3"] = ""
        self.assertNotIn("volume", parse_quote_options_request(payload))
        payload["volume_m3"] = None
        with self.assertRaises(QuoteOptionsValidationError):
            parse_quote_options_request(payload)

    def test_summary_reports_default_only_from_plugin_request(self):
        for request, expected in (({"volume": "0.1", "volume_defaulted": True}, True),
                ({"volume": "0.1", "volume_defaulted": False}, False), ({"volume": "0.1"}, False)):
            payload = build_manual_quote_options(plugin_result={**_data(), "request": request},
                delivery_method="送货")
            self.assertEqual(expected, "体积未填写，本次按 0.1 m³ 计算。" in payload["message"])

    def test_browser_quote_input_omits_blank_volume_and_rejects_invalid_input(self):
        node = shutil.which("node") or shutil.which("node.exe")
        if not node:
            self.skipTest("Node.js is required for the quote input regression")
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        script = r"""
const assert = require("node:assert/strict");
const source = require("node:fs").readFileSync(0, "utf8");
const fields = {receiver_address:"测试地址",weight_kg:"1000",volume_m3:"",delivery_method:"送货"};
const manualFieldValue = name => fields[name].trim();
const volumeField = {validity:{badInput:false}};
const document = {getElementById:() => volumeField};
const start = source.indexOf("const readQuoteRequest =");
eval(source.slice(start, source.indexOf("const calculateQuoteOptions =", start)) + `
  let request = readQuoteRequest();
  assert.equal("volume_m3" in request, false);
  assert.equal(validateQuoteRequest(request), "");
  fields.volume_m3 = " 0.45 ";
  request = readQuoteRequest();
  assert.equal(request.volume_m3, "0.45");
  assert.equal(validateQuoteRequest(request), "");
  for (const value of ["0", "-1", "bad", "Infinity"]) {
    fields.volume_m3 = value;
    assert.notEqual(validateQuoteRequest(readQuoteRequest()), "");
  }
  fields.volume_m3 = "";
  volumeField.validity.badInput = true;
  assert.notEqual(validateQuoteRequest(readQuoteRequest()), "");
  volumeField.validity.badInput = false;
  fields.weight_kg = "";
  assert.notEqual(validateQuoteRequest(readQuoteRequest()), "");
`);
"""
        result = subprocess.run([node, "--input-type=commonjs", "-e", script],
            input=template, text=True, capture_output=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ManualQuoteOptionsEndpointTests(unittest.TestCase):
    def _app(self, *, instances=None, states=None):
        app = object.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_timeout_seconds=5)
        principal = {"actor_id": "admin-1", "roles": ["admin"]}
        app._control_plane_write_context = lambda handler: {"_console_principal": principal}
        app._load_automation_plugin_catalog_uncached = lambda handler: (
            [], [_instance()] if instances is None else instances, [], [], frozenset(), "", False)
        current_states = iter(states or [{"status": "COMPLETED", "result": {"data": _data()}}])
        calls = []

        def fake_request(method, endpoint, *, payload=None, timeout=None, console_principal=None):
            self.assertIs(principal, console_principal)
            calls.append((method, endpoint, payload))
            if method == "POST":
                self.assertEqual(f"/internal/v1/automation-projects/{PROJECT_ID}/invoke", endpoint)
                return {"ok": True, "data": {"automation_id": PROJECT_ID,
                    "invocation_id": INVOCATION_ID, "status": "STARTING"}}
            self.assertEqual(f"/internal/v1/automation-invocations/{INVOCATION_ID}", endpoint)
            return {"ok": True, "data": {"automation_id": PROJECT_ID,
                "invocation_id": INVOCATION_ID, **next(current_states)}}

        app._agent_request = fake_request
        return app, calls

    def test_quote_endpoint_invokes_one_plugin_with_same_request_and_polls_exact_result(self):
        app, calls = self._app(states=[{"status": "RUNNING"},
            {"status": "COMPLETED", "result": {"data": _data()}}])
        handler = _JsonHandler(_request())
        with patch("console.services.documents.time.sleep"):
            app._handle_quote_options(handler)
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("best", handler.json_body()["best_provider"])
        posts = [call for call in calls if call[0] == "POST"]
        self.assertEqual(1, len(posts))
        self.assertEqual({"request_id": REQUEST_ID, "contribution_id": "quote_console", "runtime_inputs": {
            "address": "测试省测试市示例路1号", "weight": 12.5, "volume": 0.045}}, posts[0][2])
        self.assertEqual(3, len(calls))

    def test_missing_duplicate_or_stale_instance_never_invokes(self):
        for instances in ([], [_instance(), _instance()], [_instance(contribution_projection_state="STALE")],
                [_instance(enabled_console_entrypoints=[])], [_instance(enabled=False)]):
            with self.subTest(instances=instances):
                app, calls = self._app(instances=instances)
                handler = _JsonHandler(_request())
                app._handle_quote_options(handler)
                self.assertFalse(handler.json_body()["ok"])
                self.assertEqual([], calls)

    def test_omitted_volume_stays_omitted_in_invocation_and_shows_plugin_default(self):
        data = {**_data(), "request": {"volume": "0.1", "volume_defaulted": True}}
        app, calls = self._app(states=[{"status": "COMPLETED", "result": {"data": data}}])
        handler = _JsonHandler(_request(volume_m3=""))
        app._handle_quote_options(handler)
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual({"address": "测试省测试市示例路1号", "weight": 12.5}, calls[0][2]["runtime_inputs"])
        self.assertIn("本次按 0.1 m³ 计算", handler.json_body()["message"])

    def test_mismatched_or_failed_invocation_never_returns_prices(self):
        for state in ({"status": "COMPLETED", "automation_id": "other", "result": {"data": _data()}},
                {"status": "COMPLETED", "invocation_id": REQUEST_ID, "result": {"data": _data()}},
                {"status": "FAILED", "error_summary": "本次报价失败", "result": {"data": _data()}},
                {"status": "COMPLETED", "result": {}}):
            with self.subTest(state=state):
                app, calls = self._app(states=[state])
                handler = _JsonHandler(_request())
                app._handle_quote_options(handler)
                self.assertFalse(handler.json_body()["ok"])
                self.assertEqual(0, handler.json_body()["available_count"])
                self.assertEqual(1, sum(call[0] == "POST" for call in calls))

    def test_invalid_number_or_request_id_is_rejected_before_catalog(self):
        for payload, request_id in ((_request(weight_kg="bad"), REQUEST_ID), (_request(), ""),
                (_request(volume_m3=None), REQUEST_ID), (_request(volume_m3=0), REQUEST_ID)):
            app, calls = self._app()
            app._load_automation_plugin_catalog_uncached = lambda _: self.fail("catalog should not be called")
            handler = _JsonHandler(payload, request_id=request_id)
            app._handle_quote_options(handler)
            self.assertEqual(HTTPStatus.BAD_REQUEST, handler.status)
            self.assertEqual([], calls)

    def test_failed_invocation_keeps_current_platform_errors_without_prices(self):
        data = {provider: {"status": "failed", "error": f"{provider} 本次无数据",
            "quotes": [{"label": "不能使用的报价", "amount": "1.00", "delivery_method": "派送"}]}
            for provider in ("ronghui", "yunda", "best")}
        app, _ = self._app(states=[{"status": "FAILED", "result": {"data": data}}])
        handler = _JsonHandler(_request())
        app._handle_quote_options(handler)
        payload = handler.json_body()
        self.assertFalse(payload["ok"])
        self.assertEqual(0, payload["available_count"])
        for row in payload["quotes"][:3]:
            self.assertIn("本次无数据", row["error"])
            self.assertEqual("", row["price"])


if __name__ == "__main__":
    unittest.main()
