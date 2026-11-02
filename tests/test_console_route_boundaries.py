from __future__ import annotations

import unittest
from http import HTTPStatus

from console.routes import ConsoleRouteDispatcher


class _Handler:
    pass


class _App:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def _render_waybills(self, _handler, query):
        self.calls.append(("waybills", query))

    def _handle_tracking_query(self, _handler):
        self.calls.append(("tracking_query", None))

    def _parse_document_id(self, path):
        self.calls.append(("parse", path))
        return None

    def _send_text(self, _handler, status, message):
        self.calls.append(("send_text", (status, message)))

    def _send_json(self, _handler, status, payload):
        self.calls.append(("send_json", (status, payload)))

    def _handle_waybill_print_data(self, _handler, waybill_id, query):
        self.calls.append(("waybill_print_data", (waybill_id, query)))

    def _handle_automation_account_post(self, _handler, _path):
        return False

    def _automation_session_route(self, _path):
        return None

    def _active_original_page_proxy_disabled(self, _handler, _path):
        return False


class ConsoleRouteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.dispatcher = ConsoleRouteDispatcher()
        self.app = _App()
        self.handler = _Handler()

    def test_dispatches_waybill_workspace_without_http_framework_change(self):
        handled = self.dispatcher.handle_get(
            self.app, self.handler, "/waybills", "/waybills", {"page": ["2"]}
        )
        self.assertTrue(handled)
        self.assertEqual([("waybills", {"page": ["2"]})], self.app.calls)

    def test_retired_work_item_and_control_plane_paths_are_not_routed(self):
        for path in ("/work-items", "/work-items/work-1", "/control-plane/work-items", "/control-plane/runs/run-1"):
            with self.subTest(path=path):
                self.assertFalse(self.dispatcher.handle_get(self.app, self.handler, path, path, {}))
        for path in ("/control-plane/commands", "/control-plane/runs/run-1/cancel", "/control-plane/approvals/a-1/approve"):
            with self.subTest(path=path):
                self.assertFalse(self.dispatcher.handle_post(self.app, self.handler, path, path, {}))
        self.assertEqual([], self.app.calls)

    def test_dispatches_tracking_post_to_waybill_boundary(self):
        handled = self.dispatcher.handle_post(
            self.app, self.handler, "/tracking/query", "/tracking/query", {}
        )
        self.assertTrue(handled)
        self.assertEqual([("tracking_query", None)], self.app.calls)

    def test_unknown_waybill_print_data_is_owned_by_waybill_boundary(self):
        handled = self.dispatcher.handle_get(
            self.app, self.handler, "/waybills/not-a-number/print-data", "/waybills/not-a-number/print-data", {}
        )
        self.assertTrue(handled)
        self.assertEqual(
            [("parse", "/waybills/not-a-number/print-data"),
             ("send_json", (HTTPStatus.NOT_FOUND, {"ok": False, "message": "运单不存在。"}))],
            self.app.calls,
        )

    def test_print_data_route_passes_id_and_source(self):
        self.app._parse_document_id = lambda _path: 41
        query = {"source": ["manual"]}
        handled = self.dispatcher.handle_get(
            self.app, self.handler, "/waybills/41/print-data", "/waybills/41/print-data", query,
        )
        self.assertTrue(handled)
        self.assertEqual([("waybill_print_data", (41, query))], self.app.calls)

    def test_removed_waybill_print_page_is_not_routed(self):
        self.assertFalse(self.dispatcher.handle_get(
            self.app, self.handler, "/waybills/41/print", "/waybills/41/print", {"source": ["manual"]},
        ))
        self.assertEqual([], self.app.calls)


if __name__ == "__main__":
    unittest.main()
