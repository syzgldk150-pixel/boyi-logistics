from __future__ import annotations

import io
import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from console.routes import ocr
from console.services import browser_extension_package as package


class BrowserExtensionPackageTests(unittest.TestCase):
    def test_download_is_an_installable_extension_with_only_reviewed_sources(self):
        app = SimpleNamespace(_send_bytes=Mock(), _send_json=Mock())
        self.assertTrue(ocr.handle_get(app, object(), "/ocr/browser-extension.zip", "", {}))
        args, kwargs = app._send_bytes.call_args
        self.assertEqual(args[1], HTTPStatus.OK)
        self.assertEqual(args[3], "application/zip")
        self.assertEqual(kwargs["cache_control"], "no-store")
        with ZipFile(io.BytesIO(args[2])) as archive:
            manifest = json.loads(archive.read("extension/manifest.json"))
            self.assertEqual(manifest["manifest_version"], 3)
            scripts = {manifest["background"]["service_worker"]}
            for content in manifest["content_scripts"]:
                scripts.update(content.get("js", []))
                scripts.update(content.get("css", []))
            self.assertEqual(set(archive.namelist()),
                             {f"extension/{name}" for name in scripts | {"manifest.json", "README.md"}})
            for name in scripts:
                self.assertEqual(archive.read(f"extension/{name}"),
                                 (package.EXTENSION_ROOT / name).read_bytes())
            self.assertIn(manifest["version"], kwargs["extra_headers"]["Content-Disposition"])
        app._send_json.assert_not_called()

    def test_incomplete_deployment_returns_failure_instead_of_partial_zip(self):
        app = SimpleNamespace(_send_bytes=Mock(), _send_json=Mock())
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(package, "EXTENSION_ROOT", Path(directory)):
                package.serve_ronghui_extension_package(app, object())
        app._send_bytes.assert_not_called()
        self.assertEqual(app._send_json.call_args.args[1], HTTPStatus.SERVICE_UNAVAILABLE)

    def test_package_route_cannot_bypass_console_login(self):
        from console.app import LocalDocFlowApp

        app = object.__new__(LocalDocFlowApp)
        app._handle_isolated_original_page_request = Mock(return_value=False)
        app._active_original_page_proxy_disabled = Mock(return_value=False)
        app._ensure_authorized = Mock(return_value=False)
        app.routes = Mock()
        app.handle_get(SimpleNamespace(path="/ocr/browser-extension.zip"))
        app._ensure_authorized.assert_called_once()
        app.routes.handle_get.assert_not_called()
