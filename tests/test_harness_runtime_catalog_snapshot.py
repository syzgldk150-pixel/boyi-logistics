"""AI assistant tool listing reads one catalog snapshot over reused connections."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from agent.harness_runtime import HarnessRuntime
from shared import connection_reuse


class _Catalog:
    def __init__(self) -> None:
        self.active = False
        self.scopes = 0

    @contextmanager
    def read_scope(self):
        self.scopes += 1
        self.active = True
        try:
            yield
        finally:
            self.active = False


def _runtime(catalog: _Catalog, seen: list[tuple[bool, bool]]) -> HarnessRuntime:
    runtime = HarnessRuntime.__new__(HarnessRuntime)
    runtime._policy_service = SimpleNamespace(_plugin_catalog=catalog)
    runtime.status = lambda: SimpleNamespace(status="READY")

    def record(*_args):
        seen.append((catalog.active, connection_reuse._active_scope.get() is not None))
        return [{"tool_id": "t", "title": "查询", "description": "只读"}]

    runtime._public_tools = record
    return runtime


def test_public_tools_build_inside_one_catalog_snapshot_and_connection_scope() -> None:
    catalog, seen = _Catalog(), []
    runtime = _runtime(catalog, seen)

    tools = runtime.public_tools(object(), "00000000-0000-4000-8000-000000000000")

    assert tools == [{"tool_id": "t", "title": "查询", "description": "只读"}]
    assert seen == [(True, True)]
    assert catalog.scopes == 1
    assert not catalog.active
    assert connection_reuse._active_scope.get() is None


def test_public_tools_skip_catalog_when_runtime_not_ready() -> None:
    catalog, seen = _Catalog(), []
    runtime = _runtime(catalog, seen)
    runtime.status = lambda: SimpleNamespace(status="CAPABILITY_UNAVAILABLE")

    assert runtime.public_tools(object(), "00000000-0000-4000-8000-000000000000") == []
    assert catalog.scopes == 0
