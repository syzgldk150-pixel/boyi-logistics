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

    def _render_work_items(self, _handler, query):
        self.calls.append(("work_items", query))

    def _render_work_item_detail(self, _handler, work_item_id):
        self.calls.append(("work_item_detail", work_item_id))

    def _handle_control_plane_command_post(self, _handler):
        self.calls.append(("control_plane_command", None))

    def _handle_control_plane_work_items_get(self, _handler, query):
        self.calls.append(("control_plane_work_items", query))

    def _handle_control_plane_work_item_get(self, _handler, work_item_id):
        self.calls.append(("control_plane_work_item", work_item_id))

    def _handle_control_plane_timeline_get(self, _handler, work_item_id, query):
        self.calls.append(("control_plane_timeline", (work_item_id, query)))

    def _handle_control_plane_evidence_get(self, _handler, work_item_id, query):
        self.calls.append(("control_plane_evidence", (work_item_id, query)))

    def _handle_control_plane_run_get(self, _handler, run_id):
        self.calls.append(("control_plane_run", run_id))

    def _handle_control_plane_run_action_post(self, _handler, run_id, action):
        self.calls.append(("control_plane_run_action", (run_id, action)))

    def _handle_control_plane_approval_post(self, _handler, approval_id, decision):
        self.calls.append(("control_plane_approval", (approval_id, decision)))

    def _handle_control_plane_assign_post(self, _handler, work_item_id):
        self.calls.append(("control_plane_assign", work_item_id))

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

    def test_dispatches_control_plane_pages_through_the_single_router(self):
        handled = self.dispatcher.handle_get(
            self.app, self.handler, "/work-items", "/work-items", {"status": ["open"]}
        )
        detail_handled = self.dispatcher.handle_get(
            self.app,
            self.handler,
            "/work-items/work%3A123",
            "/work-items/work%3A123",
            {},
        )

        self.assertTrue(handled)
        self.assertTrue(detail_handled)
        self.assertEqual(
            [("work_items", {"status": ["open"]}), ("work_item_detail", "work:123")],
            self.app.calls,
        )

    def test_dispatches_control_plane_command_post(self):
        handled = self.dispatcher.handle_post(
            self.app,
            self.handler,
            "/control-plane/commands",
            "/control-plane/commands",
            {},
        )

        self.assertTrue(handled)
        self.assertEqual([("control_plane_command", None)], self.app.calls)

    def test_dispatches_each_control_plane_read_endpoint(self):
        query = {"cursor": ["next"]}
        cases = (
            (
                "/control-plane/work-items",
                ("control_plane_work_items", query),
            ),
            (
                "/control-plane/work-items/work-1",
                ("control_plane_work_item", "work-1"),
            ),
            (
                "/control-plane/work-items/work-1/timeline",
                ("control_plane_timeline", ("work-1", query)),
            ),
            (
                "/control-plane/work-items/work-1/evidence",
                ("control_plane_evidence", ("work-1", query)),
            ),
            (
                "/control-plane/runs/run-1",
                ("control_plane_run", "run-1"),
            ),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                self.app.calls.clear()
                handled = self.dispatcher.handle_get(
                    self.app,
                    self.handler,
                    path,
                    path,
                    query,
                )
                self.assertTrue(handled)
                self.assertEqual([expected], self.app.calls)

    def test_dispatches_each_control_plane_write_endpoint(self):
        cases = (
            (
                "/control-plane/runs/run-1/cancel",
                ("control_plane_run_action", ("run-1", "cancel")),
            ),
            (
                "/control-plane/runs/run-1/retry",
                ("control_plane_run_action", ("run-1", "retry")),
            ),
            (
                "/control-plane/runs/run-1/clarify",
                ("control_plane_run_action", ("run-1", "clarify")),
            ),
            (
                "/control-plane/approvals/approval-1/approve",
                ("control_plane_approval", ("approval-1", "approve")),
            ),
            (
                "/control-plane/approvals/approval-1/reject",
                ("control_plane_approval", ("approval-1", "reject")),
            ),
            (
                "/control-plane/work-items/work-1/assign",
                ("control_plane_assign", "work-1"),
            ),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                self.app.calls.clear()
                handled = self.dispatcher.handle_post(
                    self.app,
                    self.handler,
                    path,
                    path,
                    {},
                )
                self.assertTrue(handled)
                self.assertEqual([expected], self.app.calls)

    def test_dispatches_tracking_post_to_waybill_boundary(self):
        handled = self.dispatcher.handle_post(
            self.app, self.handler, "/tracking/query", "/tracking/query", {}
        )
        self.assertTrue(handled)
        self.assertEqual([("tracking_query", None)], self.app.calls)

    def test_unsafe_automation_generation_recovery_route_is_not_exposed(self):
        path = "/automations/plugins/arrival_stats/recover-not-applied"

        handled = self.dispatcher.handle_post(
            self.app,
            self.handler,
            path,
            path,
            {},
        )

        self.assertFalse(handled)
        self.assertEqual([], self.app.calls)

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
