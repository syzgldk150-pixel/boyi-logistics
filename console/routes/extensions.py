"""Compatibility redirects for the retired standalone extension center."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import quote


def _extension_action(path: str) -> tuple[str, str] | None:
    prefix = "/extensions/"
    if not path.startswith(prefix):
        return None
    value = path[len(prefix) :]
    plugin_id, separator, action = value.partition("/")
    if not plugin_id or not separator or not action or "/" in action:
        return None
    return plugin_id, action


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/extensions":
        app._redirect(handler, "/automations")
        return True
    if path.startswith("/extensions/") and "/" not in path[len("/extensions/") :]:
        plugin_id = path[len("/extensions/") :]
        app._redirect(handler, f"/automations?plugin={quote(plugin_id, safe='')}")
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path not in {"/extensions/inspect", "/extensions/install"} and _extension_action(path) is None:
        return False
    app._control_plane_error(
        handler,
        HTTPStatus.GONE,
        "EXTENSION_CENTER_RETIRED",
        "扩展中心已合并到自动化，请刷新页面后从自动化管理扩展。",
    )
    return True
