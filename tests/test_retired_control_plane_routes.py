"""The retired Command/Run/Work Item/Approval control plane is no longer mounted."""

from __future__ import annotations


def _paths(routes) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = str(getattr(route, "path", ""))
        if path:
            paths.add(path)
        included = getattr(route, "original_router", None)
        if included is not None:
            paths.update(_paths(list(included.routes)))
    return paths


def test_retired_control_plane_routes_are_not_mounted() -> None:
    import main

    paths = _paths(main.app.routes)
    for retired in ("/internal/v1/commands", "/internal/v1/runs", "/internal/v1/work-items", "/internal/v1/approvals"):
        assert not any(path == retired or path.startswith(retired + "/") for path in paths), retired
    assert not any("pending-approvals" in path for path in paths)
    assert "/internal/v1/admin/feishu-approval-binding" in paths
