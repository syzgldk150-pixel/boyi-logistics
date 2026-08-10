"""Receipt index, audit and same-origin receipt proxy routes."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/receipts":
        app._render_receipts(handler, query)
        return True
    if path == "/receipts/data":
        app._handle_receipts_data(handler, query)
        return True
    if path == "/receipts/download-images":
        app._handle_receipts_image_archive(handler, query)
        return True
    if path.startswith("/receipts/attachments/"):
        app._handle_receipt_attachment(handler, path, query)
        return True
    if raw_path.startswith("/receipts/ronghui/live"):
        app._handle_ronghui_receipt_live_proxy(handler, raw_path, method="GET", query=query)
        return True
    if raw_path.startswith("/receipts/yunda/live"):
        app._handle_yunda_receipt_live_proxy(handler, raw_path, method="GET", query=query)
        return True
    if path.startswith("/receipts/"):
        app._handle_receipt_detail(handler, path)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/receipts/sync":
        app._handle_receipts_sync(handler)
        return True
    if path.startswith("/receipts/") and path.endswith("/audit"):
        app._handle_receipt_audit(handler, path)
        return True
    if raw_path.startswith("/receipts/ronghui/live"):
        app._handle_ronghui_receipt_live_proxy(handler, raw_path, method="POST", query=query)
        return True
    if raw_path.startswith("/receipts/yunda/live"):
        app._handle_yunda_receipt_live_proxy(handler, raw_path, method="POST", query=query)
        return True
    return False


def handle_write(
    app: Any,
    handler: Any,
    _path: str,
    raw_path: str,
    query: dict[str, list[str]],
    method: str,
) -> bool:
    if raw_path.startswith("/receipts/ronghui/live"):
        app._handle_ronghui_receipt_live_proxy(handler, raw_path, method=method, query=query)
        return True
    if raw_path.startswith("/receipts/yunda/live"):
        app._handle_yunda_receipt_live_proxy(handler, raw_path, method=method, query=query)
        return True
    return False
