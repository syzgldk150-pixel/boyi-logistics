"""Canonical preview routes shared by fixed commands and model-selected plugins."""

PREVIEW_ROUTES = {
    "builtin.scan_codes": ("sync_scan_codes", "扫描"),
    "builtin.self_pickup_problem_upload": ("self_pickup_problem_upload", "自提到货问题件"),
    "builtin.split_pending_problem_upload": ("split_pending_problem_upload", "分批"),
}


def service_preview_route(plugin_id: str, commands: list[str]) -> tuple[str, str]:
    """Match a reviewed plugin and its declared command, never a name substring."""
    for route, (tool, command) in PREVIEW_ROUTES.items():
        if plugin_id == f"{tool}_v2" and command in commands:
            return route, tool
    return "", ""
