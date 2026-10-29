"""Canonical preview routes shared by fixed commands and model-selected plugins."""

from agent.automation_plugins.fixed_entrypoints import FIXED_FEISHU_ROUTE_OWNERS

# Fixed route -> the one command its owning plugin declares for the preview.
PREVIEW_ROUTES = {
    "builtin.scan_codes": "扫描",
    "builtin.self_pickup_problem_upload": "自提到货问题件",
    "builtin.split_pending_problem_upload": "分批",
}


def service_preview_route(plugin_id: str, commands: list[str]) -> tuple[str, str]:
    """Match the route's owning plugin and its declared command, never a name substring."""
    for route, command in PREVIEW_ROUTES.items():
        if plugin_id == FIXED_FEISHU_ROUTE_OWNERS[route] and command in commands:
            return route, plugin_id
    return "", ""
