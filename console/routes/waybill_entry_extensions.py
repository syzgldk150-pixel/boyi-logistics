"""Closed same-origin routes for host-rendered waybill-entry module slots."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


_ROUTE_PREFIX = "/waybill-entry/extensions/"


def handle_get(
    _app: Any,
    _handler: Any,
    _path: str,
    _raw_path: str,
    _query: dict[str, list[str]],
) -> bool:
    return False


def handle_post(
    app: Any,
    handler: Any,
    path: str,
    _raw_path: str,
    _query: dict[str, list[str]],
) -> bool:
    if not path.startswith(_ROUTE_PREFIX):
        return False
    parts = path[len(_ROUTE_PREFIX) :].split("/")
    if len(parts) != 3 or parts[2] != "invoke" or not parts[0] or not parts[1]:
        app._control_plane_error(
            handler,
            HTTPStatus.NOT_FOUND,
            "WAYBILL_ENTRY_EXTENSION_ROUTE_NOT_FOUND",
            "录单扩展路径不存在。",
        )
        return True
    app._handle_waybill_entry_extension_invoke(
        handler,
        slot=parts[0],
        handle=parts[1],
    )
    return True
