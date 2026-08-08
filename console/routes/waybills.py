"""Waybill, dispatch, tracking and line-haul workspace routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/dispatch":
        app._render_dispatch(handler, query)
        return True
    if path == "/tracking":
        app._render_tracking(handler, query)
        return True
    if path == "/waybills":
        app._render_waybills(handler, query)
        return True
    if path == "/line-haul-contacts":
        app._render_line_haul_contacts(handler, query)
        return True
    if path.startswith("/waybills/") and path.endswith("/print"):
        waybill_id = app._parse_document_id(path)
        if waybill_id is None:
            app._send_text(handler, HTTPStatus.NOT_FOUND, "Waybill not found.")
        else:
            app._render_waybill_print(handler, waybill_id, query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/tracking/query":
        app._handle_tracking_query(handler)
        return True
    if path == "/waybills/quote-options":
        app._handle_quote_options(handler)
        return True
    if path == "/waybills/manual":
        app._handle_manual_waybill(handler)
        return True
    if path.startswith("/waybills/") and path.endswith("/status"):
        waybill_id = app._parse_document_id(path)
        if waybill_id is None:
            app._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")
        else:
            app._handle_waybill_status_update(handler, waybill_id)
        return True
    if path == "/line-haul-contacts/create":
        app._handle_line_haul_contact_create(handler)
        return True
    if path == "/line-haul-contacts/import-paste":
        app._handle_line_haul_contact_import_paste(handler)
        return True
    if path.startswith("/line-haul-contacts/") and path.endswith("/update"):
        app._handle_line_haul_contact_update(handler, path)
        return True
    return False
