"""Extension-center routing; lifecycle writes remain in the existing handlers."""

from __future__ import annotations

from typing import Any


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
        app._render_extensions(handler, query)
        return True
    if path.startswith("/extensions/") and "/" not in path[len("/extensions/") :]:
        app._render_extension_detail(handler, path[len("/extensions/") :], query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/extensions/install":
        app._handle_automation_plugin_package_upload(handler)
        return True
    route = _extension_action(path)
    if route is None:
        return False
    automation_id, action = route
    if action == "upgrade":
        app._handle_automation_plugin_package_upload(handler, automation_id=automation_id)
        return True
    if action in {"enable", "disable", "uninstall"}:
        app._handle_automation_plugin_instance_action(handler, automation_id, action)
        return True
    return False
