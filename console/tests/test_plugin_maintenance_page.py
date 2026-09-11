"""Real Console auth/projection/template checks for the maintenance entry."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.app import LocalDocFlowApp
from console.routes.automation import handle_get


def app_and_result():
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    result = {}
    app.settings = SimpleNamespace(app_title="Test Console")
    app.template_env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    app._send_html = lambda _handler, body: result.update(html=body)
    app._send_json = lambda _handler, status, payload: result.update(status=status, payload=payload)
    return app, result


@pytest.mark.parametrize("role,legacy", [("admin", False), ("super_admin", True)])
def test_maintenance_denies_non_superadmin_and_basic_auth(role, legacy):
    app, result = app_and_result()
    app._load_automation_plugin_catalog_uncached = lambda *_a, **_k: pytest.fail("must not read catalog")
    handler = SimpleNamespace(current_admin_user={
        "id": 17, "role": role, "control_plane_role": role,
        "username": "operator", "display_name": "Operator",
        "is_legacy_basic_auth": legacy,
    })
    assert handle_get(app, handler, "/automations/maintenance", "", {})
    assert result["status"] == 403


def test_maintenance_uses_fresh_catalog_and_preserves_module_links_and_cas():
    app, result = app_and_result()
    observed = []
    migration = {
        "migration_pair_id": "4e19b908-1334-42cc-96e6-85fa164f52af",
        "role": "TARGET", "status_label": "验证中", "record_version": 7,
        "paired_automation_id": "old_finance", "entrypoint_owner_automation_id": "old_finance",
        "can_mark_ready": True, "can_cutover": False,
    }
    instances = [{
        "automation_id": "new_finance", "runtime_model": "SERVICE_V2",
        "instance_name": "财务<script>bad()</script>", "plugin_id": "finance_v2",
        "management": {"module": "finance"}, "migration": migration,
    }, {
        "automation_id": "old_other", "runtime_model": "ACTION_V1",
        "instance_name": "原插件", "management": {"module": "automation"}, "migration": {},
    }]
    def catalog(handler, **kwargs):
        observed.append(kwargs)
        return [], instances, [], [], frozenset(), "", True
    app._load_automation_plugin_catalog_uncached = catalog
    handler = SimpleNamespace(current_admin_user={
        "id": 17, "role": "super_admin", "control_plane_role": "super_admin",
        "username": "operator", "display_name": "Operator",
        "is_legacy_basic_auth": False,
    })
    app._render_plugin_maintenance(handler)
    assert observed == [{"summary": True}]
    assert 'data-record-version="7"' in result["html"]
    assert 'data-maintenance-action="ready"' in result["html"]
    assert 'data-maintenance-action="cutover"' not in result["html"]
    assert '/modules/finance/data-sources?open_task=new_finance' in result["html"]
    assert '财务&lt;script&gt;' in result["html"]
    assert '<script>bad()' not in result["html"]


def test_catalog_error_does_not_offer_migration_actions():
    app, result = app_and_result()
    app._control_plane_read_context = lambda _handler: {"actor_roles": ["super_admin"]}
    app._load_automation_plugin_catalog_uncached = lambda *_a, **_k: (
        [], [], [], [], frozenset(), "插件目录不可用", True,
    )
    app._render_plugin_maintenance(SimpleNamespace())
    assert "插件目录不可用" in result["html"]
    assert "data-maintenance-create" not in result["html"]
