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

    def _handle_automation_account_post(self, _handler, _path):
        return False

    def _automation_session_route(self, _path):
        return None


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

    def test_dispatches_tracking_post_to_waybill_boundary(self):
        handled = self.dispatcher.handle_post(
            self.app, self.handler, "/tracking/query", "/tracking/query", {}
        )
        self.assertTrue(handled)
        self.assertEqual([("tracking_query", None)], self.app.calls)

    def test_unknown_waybill_print_is_owned_by_waybill_boundary(self):
        handled = self.dispatcher.handle_get(
            self.app, self.handler, "/waybills/not-a-number/print", "/waybills/not-a-number/print", {}
        )
        self.assertTrue(handled)
        self.assertEqual(
            [("parse", "/waybills/not-a-number/print"), ("send_text", (HTTPStatus.NOT_FOUND, "Waybill not found."))],
            self.app.calls,
        )


if __name__ == "__main__":
    unittest.main()
