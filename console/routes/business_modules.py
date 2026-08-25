from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/settings/modules":
        app._render_business_modules(handler, query)
        return True
    if path == "/settings/modules/data":
        app._handle_business_modules_data(handler)
        return True
    if path.startswith("/settings/modules/") and path.endswith("/data"):
        app._handle_business_modules_data(handler, path.split("/")[3])
        return True
    if path.startswith("/settings/modules/") and path.endswith("/audit"):
        app._handle_business_modules_data(handler, path.split("/")[3], audit=True)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/settings/modules/lifecycle":
        app._handle_business_module_lifecycle(handler)
        return True
    return False
