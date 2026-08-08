"""OCR workspace and provider-entry routes."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path in {"/ocr", "/workspaces/ocr"}:
        app._render_ocr_workspace(handler, query)
        return True
    if path == "/ocr/boyi/frame":
        frame_query = dict(query)
        frame_query["boyi_frame"] = ["1"]
        app._render_ocr_workspace(handler, frame_query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path.startswith("/ocr/yunda/"):
        app._handle_yunda_entry(handler, path)
        return True
    if path in {"/upload", "/ocr/upload"}:
        app._handle_upload(handler)
        return True
    return False
