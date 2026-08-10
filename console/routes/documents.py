"""Template, module catalogue and document-review routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import unquote


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/templates/new":
        app._render_template_editor(handler, None, query)
        return True
    if path.startswith("/templates/") and path.endswith("/edit"):
        template_name = unquote(path[len("/templates/") : -len("/edit")].strip("/"))
        if not template_name:
            app._send_text(handler, HTTPStatus.NOT_FOUND, "Template not found.")
        else:
            app._render_template_editor(handler, template_name, query)
        return True
    if path.startswith("/modules/"):
        slug = path[len("/modules/") :].strip("/")
        if not slug:
            app._send_text(handler, HTTPStatus.NOT_FOUND, "Module not found.")
        else:
            app._render_module(handler, slug, query)
        return True
    if path.startswith("/documents/") and path.endswith("/export.json"):
        document_id = app._parse_document_id(path)
        if document_id is None:
            app._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
        else:
            app._export_document_json(handler, document_id)
        return True
    if path.startswith("/documents/"):
        document_id = app._parse_document_id(path)
        if document_id is None:
            app._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
        else:
            app._render_document(handler, document_id, query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/templates/select":
        app._handle_template_select(handler)
        return True
    if path == "/templates/save":
        app._handle_template_save(handler)
        return True

    document_actions = {
        "/review": app._handle_review,
        "/reprocess": app._handle_reprocess,
        "/delete": app._handle_delete,
    }
    for suffix, action in document_actions.items():
        if path.startswith("/documents/") and path.endswith(suffix):
            document_id = app._parse_document_id(path)
            if document_id is None:
                app._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
            else:
                action(handler, document_id)
            return True
    return False
