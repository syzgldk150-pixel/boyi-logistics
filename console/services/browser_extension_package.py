"""Package the reviewed browser-side original-page adapters from shipped source."""

from __future__ import annotations

import io
import json
from http import HTTPStatus
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile


EXTENSION_ROOT = Path(__file__).resolve().parents[1] / "static/browser_extensions/ronghui"
PACKAGE_FILES = (
    "manifest.json", "background.js", "content.js", "layout-host.js", "layout-shell.js", "README.md",
    "original-login.js", "yunda-login.css", "entry-events.js",
)


def build_ronghui_extension_package() -> tuple[bytes, str]:
    # Fixed source list: never archive a runtime directory or caller-provided path.
    sources = {name: (EXTENSION_ROOT / name).read_bytes() for name in PACKAGE_FILES}
    version = json.loads(sources["manifest.json"])["version"]
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sources.items():
            archive.writestr(f"extension/{name}", content)
    return output.getvalue(), f"boyi-ronghui-embed-{version}.zip"


def serve_ronghui_extension_package(app: Any, handler: Any) -> None:
    try:
        payload, filename = build_ronghui_extension_package()
    except (OSError, ValueError, KeyError):
        app._send_json(handler, HTTPStatus.SERVICE_UNAVAILABLE,
                       {"ok": False, "message": "浏览器扩展安装包暂不可用。"})
        return
    app._send_bytes(
        handler, HTTPStatus.OK, payload, "application/zip", cache_control="no-store",
        extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
