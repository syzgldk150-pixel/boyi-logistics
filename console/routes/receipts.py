"""Receipt index, audit, and fail-closed legacy original-page boundaries."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, raw_path: str, query: dict[str, list[str]]) -> bool:
    if app._active_original_page_proxy_disabled(handler, raw_path):
        return True
    if path == "/receipts":
        app._render_receipts(handler, query)
        return True
    if path == "/receipts/data":
        app._handle_receipts_data(handler, query)
        return True
    if path == "/receipts/shares":
        app._render_receipt_shares(handler, query)
        return True
    if path == "/receipts/shares/data":
        app._handle_receipt_shares_data(handler, query)
        return True
    if path == "/receipts/shares/naming-example":
        app._handle_receipt_share_naming_example(handler, query)
        return True
    if path == "/receipts/shares/senders":
        app._handle_receipt_share_senders(handler, query)
        return True
    if path == "/receipts/download-images":
        app._handle_receipts_image_archive(handler, query)
        return True
    if path.startswith("/receipts/attachments/"):
        app._handle_receipt_attachment(handler, path, query)
        return True
    if path.startswith("/receipts/"):
        app._handle_receipt_detail(handler, path)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, raw_path: str, query: dict[str, list[str]]) -> bool:
    if app._active_original_page_proxy_disabled(handler, raw_path):
        return True
    if path == "/receipts/shares":
        app._handle_receipt_share_create(handler)
        return True
    if path == "/receipts/shares/customers":
        app._handle_receipt_share_customer(handler)
        return True
    if path == "/receipts/shares/preview":
        app._handle_receipt_share_preview(handler)
        return True
    share_action = app._receipt_share_action_route(path)
    if share_action is not None:
        app._handle_receipt_share_action(handler, *share_action)
        return True
    if path == "/receipts/sync":
        app._handle_receipts_sync(handler)
        return True
    if path.startswith("/receipts/") and path.endswith("/audit"):
        app._handle_receipt_audit(handler, path)
        return True
    return False
