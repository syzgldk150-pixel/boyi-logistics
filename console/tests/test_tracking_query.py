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


class _Handler:
    def __init__(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)


class TrackingQueryTests(unittest.TestCase):
    def _build_app(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_base_url="http://agent.test", agent_timeout_seconds=30)

        def capture_json(self, handler, status, payload):
            self.sent_status = status
            self.sent_payload = payload

        app._send_json = types.MethodType(capture_json, app)
        return app

    def test_ronghui_number_calls_unified_agent_tms_endpoint(self):
        app = self._build_app()
        calls = []

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            calls.append(
                {
                    "method": method,
                    "endpoint": endpoint,
                    "payload": payload,
                    "timeout": timeout,
                }
            )
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "cost_sec": 0.01,
                    "data": {
                        "ok": True,
                        "type": "ronghui_tms",
                        "tracking_number": "R00014513348",
                        "summary": {"status": "未签收"},
                        "waybill_stub": {
                            "sender_name": "勇胜",
                            "recipient_name": "张三",
                            "goods_name": "纸袋",
                        },
                        "waybill_info": [
                            {
                                "title": "货物信息",
                                "items": [
                                    {"label": "件数", "value": "13件"},
                                    {"label": "结算重量", "value": "570.00"},
                                ],
                            }
                        ],
                        "route_rows": [
                            {
                                "scan_time": "2026-05-13 10:00:38",
                                "upload_time": "2026-05-13 10:00:38",
                                "scan_type": "分拨到达待卸",
                                "description": "分拨到达待卸，快件到达【邵阳操作场】",
                                "contact": "邵阳操作场 0739-5455259",
                                "source": "TMS",
                            }
                        ],
                        "child_detail_rows": [
                            {
                                "child_waybill_no": "R000145133480001",
                                "scan_station": "邵阳操作场",
                                "scan_type": "发件",
                                "scan_time": "2026-05-13 10:00:38",
                                "description": "子单发件",
                            }
                        ],
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)

        app._handle_tracking_query(_Handler({"tracking_number": "R00014513348"}))

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("ronghui_tms", app.sent_payload["type"])
        self.assertEqual(
            [
                {
                    "scan_time": "2026-05-13 10:00:38",
                    "upload_time": "2026-05-13 10:00:38",
                    "scan_type": "分拨到达待卸",
                    "description": "分拨到达待卸，快件到达【邵阳操作场】",
                    "contact": "邵阳操作场 0739-5455259",
                    "source": "TMS",
                }
            ],
            app.sent_payload["route_rows"],
        )
        self.assertEqual("R000145133480001", app.sent_payload["child_detail_rows"][0]["child_waybill_no"])
        self.assertEqual("勇胜", app.sent_payload["waybill_stub"]["sender_name"])
        self.assertEqual("货物信息", app.sent_payload["waybill_info"][0]["title"])
        self.assertEqual("/internal/v1/tms/tracking_query", calls[0]["endpoint"])
        self.assertEqual({"tracking_number": "R00014513348", "decrypt_masked": True}, calls[0]["payload"]["params"])
        self.assertEqual(180, calls[0]["payload"]["timeout_sec"])

    def test_yunda_number_calls_unified_agent_tms_endpoint(self):
        app = self._build_app()
        calls = []

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            calls.append(
                {
                    "method": method,
                    "endpoint": endpoint,
                    "payload": payload,
                    "timeout": timeout,
                }
            )
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "cost_sec": 0.01,
                    "data": {
                        "ok": True,
                        "type": "yunda",
                        "tracking_number": "977808459",
                        "route_rows": [
                            {
                                "scan_time": "2026-05-12 13:11:45",
                                "description": "快件已被客户【指定位置】签收",
                                "contact": "湖南邵阳集配站 0739-5455259",
                                "data_source": "02",
                                "device_no": "56571150217797",
                            }
                        ],
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)

        app._handle_tracking_query(_Handler({"tracking_number": "977808459"}))

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("yunda", app.sent_payload["type"])
        self.assertEqual("湖南邵阳集配站 0739-5455259", app.sent_payload["route_rows"][0]["contact"])
        self.assertEqual("/internal/v1/tms/tracking_query", calls[0]["endpoint"])
        self.assertEqual({"tracking_number": "977808459", "decrypt_masked": True}, calls[0]["payload"]["params"])

    def test_tracking_template_uses_scan_detail_and_child_tabs_without_r7(self):
        template = (CONSOLE_DIR / "templates" / "tracking.html").read_text(encoding="utf-8")

        self.assertIn('data-shipment-tabs', template)
        self.assertIn('data-shipment-tab="routes"', template)
        self.assertIn('data-shipment-tab="details"', template)
        self.assertIn('data-shipment-tab="children"', template)
        self.assertIn('data-shipment-panel="routes"', template)
        self.assertIn('data-shipment-panel="details"', template)
        self.assertIn('data-shipment-panel="children"', template)
        self.assertIn("扫描轨迹", template)
        self.assertIn("运单详情", template)
        self.assertIn("子单详情", template)
        self.assertIn("child_detail_rows", template)
        self.assertIn("renderShipmentResult(result.data", template)
        self.assertNotIn("renderR7Routes(result.data);", template)
        self.assertNotIn("ronghui_r7", template)
        self.assertNotIn("node_tracks", template)


if __name__ == "__main__":
    unittest.main()
