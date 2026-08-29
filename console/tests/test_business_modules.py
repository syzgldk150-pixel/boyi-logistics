from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment, FileSystemLoader

from console.services.business_modules import BusinessModulesServiceMixin
from shared.business_modules import BUSINESS_MODULE_CATALOG


class _Console(BusinessModulesServiceMixin):
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.sent: list[tuple[int, dict[str, Any]]] = []
        self.settings = SimpleNamespace(app_title="test")

    def _agent_request(self, method: str, endpoint: str, *, payload=None, **_kwargs):
        self.calls.append((method, endpoint, payload))
        return self.response

    def _mysql_console_principal(self, _user):
        return {"actor_id": "1"}

    def _send_json(self, _handler, status, payload):
        self.sent.append((int(status), payload))

    @staticmethod
    def _parse_json_body(handler):
        return handler.body


class _Handler:
    def __init__(
        self,
        body: dict[str, Any] | None = None,
        *,
        role: str = "super_admin",
        user_id: int = 1,
        legacy: bool = False,
        origin: str = "http://console",
    ) -> None:
        self.body = body or {}
        self.current_admin_user = {"id": user_id, "role": role, "username": "owner", "is_legacy_basic_auth": legacy}
        self.headers = {"Host": "console", "Origin": origin}


def _rows(*, receipts: str = "ENABLED", waybill_entry: str = "ENABLED", finance: str = "ENABLED") -> dict[str, Any]:
    return {"ok": True, "data": {"items": [
        {"module_code": "overview", "lifecycle_state": "ENABLED"},
        {"module_code": "automations", "lifecycle_state": "ENABLED"},
        {"module_code": "automation_accounts", "lifecycle_state": "ENABLED"},
        {"module_code": "llm_settings", "lifecycle_state": "ENABLED"},
        {"module_code": "work_items", "lifecycle_state": "ENABLED"},
        {"module_code": "system_settings", "lifecycle_state": "ENABLED"},
        {"module_code": "waybill_query", "lifecycle_state": "ENABLED"},
        {"module_code": "waybill_entry", "lifecycle_state": waybill_entry},
        {"module_code": "finance", "lifecycle_state": finance},
        {"module_code": "receipts", "lifecycle_state": receipts},
    ]}}


def test_navigation_uses_all_static_registrations_during_status_outage() -> None:
    app = _Console({"ok": False})
    routes = {item["route"] for item in app._business_module_navigation(None)}
    assert len(routes) == len(BUSINESS_MODULE_CATALOG)
    assert "/" in routes and "/automations" in routes and "/receipts" in routes
    assert app.calls == []


def test_module_status_failure_is_cached_briefly() -> None:
    app = _Console({"ok": False})

    assert app._business_module_rows() is None
    assert app._business_module_rows() is None
    assert len(app.calls) == 1

    app._invalidate_business_module_status_cache()
    assert app._business_module_rows() is None
    assert len(app.calls) == 2


def test_module_manager_navigation_is_super_admin_only_and_status_independent(
    monkeypatch,
) -> None:
    super_admin = {"id": 1, "role": "super_admin", "is_legacy_basic_auth": False}
    ordinary_admin = {"id": 2, "role": "admin", "is_legacy_basic_auth": False}
    legacy_super_admin = {"id": 3, "role": "super_admin", "is_legacy_basic_auth": True}
    app = _Console(_rows())

    monkeypatch.setattr(
        "console.services.business_modules.current_admin_user",
        lambda: super_admin,
    )
    assert "/settings/modules" in {
        item["route"] for item in app._business_module_navigation()
    }
    assert "/settings/modules" in {item["route"] for item in app._business_module_navigation(super_admin)}
    for user in (ordinary_admin, legacy_super_admin, None, {"id": 0, "role": "super_admin"}):
        assert "/settings/modules" not in {
            item["route"] for item in app._business_module_navigation(user)
        }

    outage_app = _Console({"ok": False})
    assert "/settings/modules" in {
        item["route"] for item in outage_app._business_module_navigation(super_admin)
    }
    assert "/settings/modules" not in {
        item["route"] for item in outage_app._business_module_navigation(ordinary_admin)
    }
    assert "/settings/modules" not in app._business_module_mobile_navigation_for_user(
        ordinary_admin
    )
    assert len(BUSINESS_MODULE_CATALOG) == 14


def test_super_admin_mobile_navigation_uses_the_common_navigation_projection() -> None:
    app = _Console(_rows())
    user = {
        "id": 1,
        "role": "super_admin",
        "is_legacy_basic_auth": False,
        "ui_preferences_json": json.dumps(
            {"mobile_bottom_nav": ["/settings/modules", "/tracking", "/automations"]}
        ),
    }

    assert "/settings/modules" in app._business_module_mobile_navigation_for_user(user)


def test_mobile_navigation_preserves_static_registered_routes() -> None:
    app = _Console(_rows())
    navigation = list(app._business_module_navigation())
    routes = app._business_module_mobile_nav({"ui_preferences_json": json.dumps({"mobile_bottom_nav": ["/receipts", "/tracking", "/automations"]})}, navigation)
    assert routes == ("/receipts", "/tracking", "/automations")
    repaired = app._business_module_mobile_nav({"ui_preferences_json": json.dumps({"mobile_bottom_nav": ["/tracking"]})}, navigation)
    assert len(repaired) == 3 and "/tracking" in repaired


def test_template_mobile_navigation_adapter_keeps_disabled_module_registered() -> None:
    app = _Console(_rows(receipts="DISABLED"))

    routes = app._business_module_mobile_navigation_for_user(
        {"ui_preferences_json": json.dumps({"mobile_bottom_nav": ["/receipts", "/automations"]})}
    )

    assert "/receipts" in routes
    assert "/automations" in routes


def test_status_outage_allows_get_and_rejects_optional_module_writes() -> None:
    app = _Console({"ok": False})
    app._reset_business_module_request_state()

    assert app._reject_unavailable_business_module_request(
        _Handler(), "/receipts", method="GET"
    ) is False
    assert app._business_module_status_unavailable_for_request() is True

    write_handler = _Handler()
    assert app._reject_unavailable_business_module_request(
        write_handler, "/receipts/sync", method="POST"
    ) is True
    assert app.sent[-1][0] == 503
    assert app.sent[-1][1]["error_code"] == "MODULE_STATUS_UNAVAILABLE"
    assert len(app.calls) == 1

    core_write_handler = _Handler()
    assert app._reject_unavailable_business_module_request(
        core_write_handler, "/automations/run", method="POST"
    ) is True
    assert app.sent[-1][1]["error_code"] == "MODULE_STATUS_UNAVAILABLE"

    app._reset_business_module_request_state()
    assert app._business_module_status_unavailable_for_request() is False


def test_direct_optional_page_and_api_are_rejected_when_disabled() -> None:
    app = _Console(_rows(receipts="DISABLED"))
    handler = _Handler()
    assert app._reject_unavailable_business_module_request(handler, "/receipts") is True
    assert app.sent[-1][0] == 404
    assert app._reject_unavailable_business_module_request(handler, "/") is False


def test_waybill_entry_owns_manual_quote_and_original_page_api_prefixes() -> None:
    app = _Console(_rows(waybill_entry="DISABLED"))
    for path in ("/waybills/manual", "/waybills/quote-options", "/original-pages/ronghui/launch"):
        handler = _Handler()
        assert app._module_code_for_request(path) == "waybill_entry"
        assert app._reject_unavailable_business_module_request(handler, path) is True
        assert app.sent[-1][0] == 404
    assert app._module_code_for_request("/waybills") == "waybill_query"


def test_runtime_subdirectories_are_isolated_between_waybill_entry_and_finance() -> None:
    finance_runtime = "/runtime/finance_knowledge/finance_rules_v1.md"
    waybill_runtime = "/runtime/artifacts/processed/document.jpg"
    waybill_disabled = _Console(_rows(waybill_entry="DISABLED", finance="ENABLED"))
    finance_disabled = _Console(_rows(waybill_entry="ENABLED", finance="DISABLED"))

    assert waybill_disabled._module_code_for_request(finance_runtime) == "finance"
    assert waybill_disabled._reject_unavailable_business_module_request(_Handler(), finance_runtime) is False
    blocked_waybill = _Handler()
    assert waybill_disabled._module_code_for_request(waybill_runtime) == "waybill_entry"
    assert waybill_disabled._reject_unavailable_business_module_request(blocked_waybill, waybill_runtime) is True
    assert waybill_disabled.sent[-1][0] == 404

    blocked_finance = _Handler()
    assert finance_disabled._module_code_for_request(finance_runtime) == "finance"
    assert finance_disabled._reject_unavailable_business_module_request(blocked_finance, finance_runtime) is True
    assert finance_disabled.sent[-1][0] == 404


def test_runtime_module_gate_normalizes_single_decoded_paths_before_matching() -> None:
    finance_disabled = _Console(_rows(waybill_entry="ENABLED", finance="DISABLED"))
    waybill_disabled = _Console(_rows(waybill_entry="DISABLED", finance="ENABLED"))
    finance_paths = (
        "/runtime/artifacts/../finance_knowledge/finance_rules_v1.md",
        "/runtime/artifacts/%2e%2e/finance_knowledge/finance_rules_v1.md",
    )
    waybill_paths = (
        "/runtime/finance_knowledge/../artifacts/processed/document.jpg",
        "/runtime/finance_knowledge/%2e%2e/artifacts/processed/document.jpg",
    )

    for path in finance_paths:
        handler = _Handler()
        assert finance_disabled._module_code_for_request(path) == "finance"
        assert finance_disabled._reject_unavailable_business_module_request(handler, path) is True
        assert finance_disabled.sent[-1][1]["error_code"] == "MODULE_UNAVAILABLE"
    for path in waybill_paths:
        handler = _Handler()
        assert waybill_disabled._module_code_for_request(path) == "waybill_entry"
        assert waybill_disabled._reject_unavailable_business_module_request(handler, path) is True
        assert waybill_disabled.sent[-1][1]["error_code"] == "MODULE_UNAVAILABLE"

    # The file handler decodes only once: double encoding names a literal path,
    # rather than traversing into finance.
    double_encoded = "/runtime/artifacts/%252e%252e/finance_knowledge/finance_rules_v1.md"
    assert finance_disabled._module_code_for_request(double_encoded) == "waybill_entry"
    assert finance_disabled._reject_unavailable_business_module_request(_Handler(), double_encoded) is False


def test_runtime_module_gate_blocks_paths_that_escape_the_runtime_root() -> None:
    app = _Console(_rows())
    for path in (
        "/runtime/../finance_knowledge/finance_rules_v1.md",
        "/runtime/%2e%2e/finance_knowledge/finance_rules_v1.md",
        "/runtime/artifacts/%2e%2e/%2e%2e/finance_knowledge/finance_rules_v1.md",
        "/runtime/%2ffinance_knowledge/finance_rules_v1.md",
    ):
        handler = _Handler()
        assert app._module_code_for_request(path) == ""
        assert app._reject_unavailable_business_module_request(handler, path) is True
        assert app.sent[-1][0] == 404
        assert app.sent[-1][1]["error_code"] == "INVALID_MODULE_RUNTIME_PATH"


def test_lifecycle_write_requires_super_admin_same_origin_and_closed_dto() -> None:
    app = _Console({"ok": True, "data": {"module": {"module_code": "receipts"}}})
    denied = _Handler(role="admin")
    app._handle_business_module_lifecycle(denied)
    assert denied.body == {} and app.sent[-1][0] == 403
    bad_origin = _Handler({"module_code": "receipts"}, origin="https://elsewhere")
    app._handle_business_module_lifecycle(bad_origin)
    assert app.sent[-1][0] == 403
    good = _Handler({"module_code": "receipts", "action": "disable", "reason": "maintenance", "request_id": str(uuid.uuid4()), "expected_record_version": 1, "actor": "forged"})
    app._handle_business_module_lifecycle(good)
    assert app.sent[-1][0] == 400
    assert not app.calls


def test_audit_trigger_and_result_container_have_distinct_dom_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "business_modules.html").read_text(encoding="utf-8")
    script = (root / "static" / "business_modules.js").read_text(encoding="utf-8")

    assert "data-module-audit-trigger" in template
    assert "data-module-audit-list" in template
    assert "data-module-audit>" not in template
    assert 'closest("[data-module-audit-trigger]")' in script
    assert 'querySelector("[data-module-audit-list]")' in script
    assert "已启用" in template
    assert 'disable: "停用后将拒绝该模块的新业务请求' in script


def test_base_template_displays_agent_outage_without_hiding_navigation() -> None:
    root = Path(__file__).resolve().parents[1]
    app = _Console({"ok": False})
    app._reset_business_module_request_state()
    assert app._reject_unavailable_business_module_request(
        _Handler(), "/receipts", method="GET"
    ) is False
    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=True)
    env.globals.update(
        current_admin_user=lambda: None,
        navigation_for_user=lambda: app._business_module_navigation(None),
        mobile_navigation_for_user=app._business_module_mobile_navigation_for_user,
        business_module_status_unavailable=app._business_module_status_unavailable_for_request,
    )

    rendered = env.get_template("base.html").render(app_title="test")

    assert 'data-agent-unavailable' in rendered
    assert "Agent 服务不可用" in rendered
    assert rendered.count('class="nav-link"') == len(BUSINESS_MODULE_CATALOG)
