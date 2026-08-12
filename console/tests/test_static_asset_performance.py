import gzip
import io
import unittest
from pathlib import Path

from console.services.documents import DocumentServiceMixin


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class _Handler:
    def __init__(self, path, headers=None):
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.sent_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        return None

    def header_value(self, name):
        name = name.lower()
        return next(value for key, value in self.sent_headers if key.lower() == name)


class StaticAssetPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(DocumentServiceMixin)

    def test_versioned_static_asset_is_gzipped_and_immutable(self):
        handler = _Handler(
            "/static/style.css?v=cal-console-20260812-perf1",
            {"Accept-Encoding": "gzip, deflate"},
        )

        self.service._serve_static_file(handler, "style.css")

        self.assertEqual(200, handler.status)
        self.assertEqual("gzip", handler.header_value("Content-Encoding"))
        self.assertIn("Accept-Encoding", handler.header_value("Vary"))
        self.assertEqual(
            "public, max-age=31536000, immutable",
            handler.header_value("Cache-Control"),
        )
        self.assertEqual(
            (CONSOLE_DIR / "static" / "style.css").read_bytes(),
            gzip.decompress(handler.wfile.getvalue()),
        )
        self.assertLess(
            len(handler.wfile.getvalue()),
            (CONSOLE_DIR / "static" / "style.css").stat().st_size,
        )

    def test_static_asset_etag_returns_not_modified(self):
        first = _Handler("/static/vendor/feather-4.29.2.min.js")
        self.service._serve_static_file(first, "vendor/feather-4.29.2.min.js")
        etag = first.header_value("ETag")

        second = _Handler(
            "/static/vendor/feather-4.29.2.min.js",
            {"If-None-Match": etag},
        )
        self.service._serve_static_file(second, "vendor/feather-4.29.2.min.js")

        self.assertEqual(304, second.status)
        self.assertEqual(b"", second.wfile.getvalue())
        self.assertEqual(etag, second.header_value("ETag"))

    def test_gzip_quality_zero_is_respected(self):
        handler = _Handler(
            "/static/style.css?v=cal-console-20260812-perf1",
            {"Accept-Encoding": "br, gzip;q=0, *;q=1"},
        )

        self.service._serve_static_file(handler, "style.css")

        self.assertEqual(200, handler.status)
        self.assertFalse(any(name.lower() == "content-encoding" for name, _ in handler.sent_headers))
        self.assertEqual(
            (CONSOLE_DIR / "static" / "style.css").read_bytes(),
            handler.wfile.getvalue(),
        )

    def test_frontend_assets_stay_within_payload_budgets(self):
        static = CONSOLE_DIR / "static"
        budgets = {
            "assets/boyi-logistics-logo-7e1f2994.webp": 50_000,
            "assets/fonts/InterVariable-Latin.woff2": 150_000,
            "assets/fonts/SourceHanSansCN-UI.woff2": 500_000,
            "assets/fonts/SourceHanSansCN-Common.woff2": 2_500_000,
            "vendor/feather-4.29.2.min.js": 100_000,
        }

        for relative_path, maximum_size in budgets.items():
            with self.subTest(relative_path=relative_path):
                path = static / relative_path
                self.assertTrue(path.is_file())
                self.assertLess(path.stat().st_size, maximum_size)


if __name__ == "__main__":
    unittest.main()
