"""Sign-for and receipt thumbnails of 寄件运单查询 rows."""

import base64
import io
import json
import sys
import tempfile
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, select_autoescape

CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))
sys.modules.setdefault(
    "config",
    types.SimpleNamespace(MODULE_DIR=CONSOLE_DIR, PROJECT_ROOT=CONSOLE_DIR, Settings=object,
                          load_settings=lambda: SimpleNamespace()),
)
from app import LocalDocFlowApp  # noqa: E402
from services import waybill_photos  # noqa: E402

PRINCIPAL = {"actor_type": "console_admin", "actor_id": "7", "roles": ["super_admin"],
             "display_name": "tester", "authenticated_by": "mysql_admin_session"}


class _Handler:
    def __init__(self):
        self.headers = {}
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        return None

    def json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class _ImageResponse:
    status = 200
    headers = {"Content-Type": "image/jpeg"}

    def __init__(self, payload):
        self.stream = io.BytesIO(payload)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, size=-1):
        return self.stream.read(size)


def _build_app(*, principal=PRINCIPAL):
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.settings = SimpleNamespace(app_title="Test Console", agent_base_url="http://agent.test",
                                   agent_timeout_seconds=30, runtime_dir=Path(tempfile.mkdtemp()))
    app._mysql_console_principal = lambda: principal
    return app


class WaybillPhotoListTest(unittest.TestCase):
    def setUp(self):
        waybill_photos._photo_lists.clear()

    def test_photo_list_reads_agent_once_and_links_images_through_console(self):
        app = _build_app()
        calls = []

        def agent_request(method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append((endpoint, payload["params"], console_principal["actor_id"]))
            return {"ok": True, "data": {
                "platform": "yunda", "waybill_no": "900000001",
                "sign_photos": ["https://kyscan.yunda56.com/ws/sc/20261111/a.jpg"],
                "receipt_photos": ["https://kyinms.yunda56.com/upload/return/b.jpg"],
            }}

        app._agent_request = agent_request
        query = {"platform": ["yunda"], "waybill_no": ["900000001"]}
        first, second = _Handler(), _Handler()
        app._handle_waybill_photos(first, query)
        app._handle_waybill_photos(second, query)

        self.assertEqual(HTTPStatus.OK, first.status)
        self.assertEqual(first.json(), second.json())
        self.assertEqual([("/internal/v1/business/waybill-photos",
                           {"platform": "yunda", "waybill_no": "900000001"}, "7")], calls)
        data = first.json()["data"]
        self.assertEqual(
            ["/waybills/photo-image?platform=yunda&url=https%3A%2F%2Fkyscan.yunda56.com%2Fws%2Fsc%2F20261111%2Fa.jpg"],
            data["sign"])
        self.assertEqual(1, len(data["receipt"]))
        self.assertEqual("", data["receipt_error"])

    def test_receipt_failure_is_reported_and_not_cached(self):
        app = _build_app()
        calls = []

        def agent_request(method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append(endpoint)
            return {"ok": True, "data": {"platform": "ronghui", "waybill_no": "R900000001",
                                         "sign_photos": [], "receipt_photos": [],
                                         "receipt_error": "融辉回单读取失败（HTTPError）"}}

        app._agent_request = agent_request
        query = {"platform": ["ronghui"], "waybill_no": ["R900000001"]}
        handler = _Handler()
        app._handle_waybill_photos(handler, query)
        app._handle_waybill_photos(_Handler(), query)

        self.assertEqual("融辉回单读取失败（HTTPError）", handler.json()["data"]["receipt_error"])
        self.assertEqual(2, len(calls))

    def test_invalid_input_and_missing_admin_session_never_call_agent(self):
        app = _build_app(principal=None)
        app._agent_request = lambda *_args, **_kwargs: self.fail("agent must not be called")
        bad, no_session = _Handler(), _Handler()
        app._handle_waybill_photos(bad, {"platform": ["best"], "waybill_no": ["900000001"]})
        app._handle_waybill_photos(no_session, {"platform": ["yunda"], "waybill_no": ["900000001"]})

        self.assertEqual(HTTPStatus.BAD_REQUEST, bad.status)
        self.assertEqual(HTTPStatus.FORBIDDEN, no_session.status)


class WaybillPhotoImageTest(unittest.TestCase):
    def test_yunda_sign_photo_is_fetched_once_then_served_from_cache(self):
        app = _build_app()
        url = "https://kyscan.yunda56.com/ws/sc/20261111/a.jpg"
        query = {"platform": ["yunda"], "url": [url]}
        with patch("console.services.waybills_receipts.urlopen",
                   return_value=_ImageResponse(b"\xff\xd8sign-photo")) as mocked:
            first = _Handler()
            app._handle_waybill_photo_image(first, query)
            second = _Handler()
            app._handle_waybill_photo_image(second, query)

        self.assertEqual(1, mocked.call_count)
        self.assertEqual(url, mocked.call_args.args[0].full_url)
        self.assertEqual(b"\xff\xd8sign-photo", second.wfile.getvalue())
        self.assertIn(("Content-Type", "image/jpeg"), second.sent_headers)

    def test_addresses_outside_the_platform_image_hosts_are_not_fetched(self):
        app = _build_app()
        app._agent_request = lambda *_args, **_kwargs: self.fail("agent must not be called")
        with patch("console.services.waybills_receipts.urlopen") as mocked:
            for platform, url in (("yunda", "https://example.com/ws/a.jpg"),
                                  ("ronghui", "https://tms.ronghuiwl.com/login/a.jpg"),
                                  ("yunda", "http://kyscan.yunda56.com/ws/a.jpg")):
                handler = _Handler()
                app._handle_waybill_photo_image(handler, {"platform": [platform], "url": [url]})
                self.assertIn(handler.status, {HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND}, url)
        mocked.assert_not_called()

    def test_yunda_receipt_image_is_read_through_agent(self):
        app = _build_app()
        calls = []

        def agent_request(method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append((endpoint, payload["params"]))
            return {"ok": True, "data": {"mime_type": "image/jpeg", "size": 9,
                                         "content_base64": base64.b64encode(b"\xff\xd8receipt").decode("ascii")}}

        app._agent_request = agent_request
        url = "https://kyinms.yunda56.com/upload/return/b.jpg"
        handler = _Handler()
        app._handle_waybill_photo_image(handler, {"platform": ["yunda"], "url": [url]})

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual([("/internal/v1/business/receipt-attachment-image", {"source_url": url})], calls)


class WaybillPhotoTemplateTest(unittest.TestCase):
    def _render_rows(self, rows):
        env = Environment(loader=FileSystemLoader(CONSOLE_DIR / "templates"), autoescape=select_autoescape(["html"]))
        source = (CONSOLE_DIR / "templates" / "waybills.html").read_text(encoding="utf-8")
        start = source.index("{% macro photo_slots(row) -%}")
        end = source.index("{%- endmacro %}", start) + len("{%- endmacro %}")
        template = env.from_string(source[start:end] + "{% for row in rows %}<i>{{ photo_slots(row) }}</i>{% endfor %}")
        return template.render(rows=rows)

    def test_only_signed_platform_or_linked_rows_read_photos(self):
        html = self._render_rows([
            {"source": "yunda", "waybill_no": "900000001", "status": "signed"},
            {"source": "ronghui", "waybill_no": "R900000002", "status": "in_transit"},
            {"source": "manual", "waybill_no": "B1", "status": "signed",
             "linked_platform": "ronghui", "linked_waybill_no": "R900000003"},
            {"source": "manual", "waybill_no": "B2", "status": "signed"},
        ])

        self.assertIn('data-platform="yunda" data-waybill-no="900000001"', html)
        self.assertNotIn("R900000002", html)
        self.assertIn('data-platform="ronghui" data-waybill-no="R900000003"', html)
        self.assertEqual(2, html.count("data-waybill-photos"))
        self.assertEqual(4, html.count("<i>"))
        self.assertEqual(8, html.count('class="waybill-photo-slot"'))  # every row keeps two slots


if __name__ == "__main__":
    unittest.main()
