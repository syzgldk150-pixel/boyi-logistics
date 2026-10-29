"""The static reviewed Host primitive list equals what production actually binds."""
from __future__ import annotations

import importlib

from agent.automation_plugins import reviewed_connectors
from agent.automation_plugins.reviewed_primitives import REVIEWED_HOST_PRIMITIVES
from plugin_core_adapters.host_primitives import build_production_host_primitive_handler_map

_CONNECTOR_MODULES = (
    "arrival_connectors_v2", "problem_connectors_v2", "scan_connectors_v2", "list_connectors_v2",
    "yunda_connectors_v2", "finance_connectors_v2", "daily_send_connectors_v2", "delivery_connectors_v2",
    "customer_connectors_v2", "daily_sign_connectors_v2", "route_status_connectors_v2",
)
_CLOCK_PRIMITIVES = {("browser.invoke", f"ronghui.clock.{name}") for name in ("precheck", "submit", "verify")}


class _Manager:
    def require_active_binding_descriptor(self, account_id):
        return {"account_id": account_id, "system": "ronghui", "session_profile": f"profile-{account_id}"}


def test_static_list_equals_connector_bindings_plus_clock_actions(monkeypatch):
    bound = set()
    original = reviewed_connectors.reviewed_connector_handler

    def recording(reviewed, *, tool, operation, action, role, **kwargs):
        bound.add((operation, action))
        return original(reviewed, tool=tool, operation=operation, action=action, role=role, **kwargs)

    for name in _CONNECTOR_MODULES:
        module = importlib.import_module(f"agent.automation_plugins.{name}")
        monkeypatch.setattr(module, "reviewed_connector_handler", recording, raising=False)
    from agent.automation_plugins.production_connectors import build_production_connector_registry

    build_production_connector_registry({key: (lambda *_a, **_k: None) for key in REVIEWED_HOST_PRIMITIVES})
    assert REVIEWED_HOST_PRIMITIVES == bound | _CLOCK_PRIMITIVES


def test_production_map_filtered_by_the_list_builds_every_connector():
    """main.py filters the merged map before Connectors bind; none may be lost."""
    from agent.automation_plugins.production_connectors import build_production_connector_registry

    handlers = build_production_host_primitive_handler_map(
        account_manager=_Manager(), cursor_secret=b"s" * 32,
        capability_authorizer=lambda _descriptor, _capability: None,
        allowed_action_keys=REVIEWED_HOST_PRIMITIVES,
    )
    assert set(handlers) == REVIEWED_HOST_PRIMITIVES
    assert build_production_connector_registry(handlers)
