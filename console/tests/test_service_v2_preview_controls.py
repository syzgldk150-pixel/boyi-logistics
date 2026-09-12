"""Exercise V2 instance routes and actual preview controls used by the browser."""
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from console.services.automation_preview_support import normalize_selection_preview_projection
from console.tests import test_automation_control_plane_cutover as controls
from console.tests import test_automation_run_controls as templates

INSTANCE = "9d9f96b5-7b45-4741-837f-92ff9b8ab365"
PREVIEW = "11111111-1111-4111-8111-111111111111"
REQUEST = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("plugin_id,marker,label", [
    ("sync_scan_codes_v2", "scan", "生成预览"),
    ("self_pickup_problem_upload_v2", "selection", "读取候选"),
    ("split_pending_problem_upload_v2", "selection", "读取候选"),
])
def test_v2_instance_renders_preview_and_confirmation(plugin_id, marker, label):
    templates.AutomationRunControlsTemplateTests.setUpClass()
    view = templates.AutomationRunControlsTemplateTests()
    html = view._render({
        "task_id": INSTANCE, "name_value": "新版插件", "task_mode": "plugin",
        "tool_params_json": "{}", "is_schedulable": False,
        "plugin": {"plugin_id": plugin_id, "runtime_model": "SERVICE_V2",
                   "state": "ENABLED", "config_fields": [], "resource_roles": []},
    })
    assert f'data-{marker}-preview-workflow="true"' in html
    assert f'data-{marker}-preview-confirm' in html
    assert label in html


@pytest.mark.parametrize("selection", [False, True])
def test_confirmation_preserves_v2_instance_and_bounded_arguments(selection):
    app = controls._App({"ok": True, "status": 202, "data": {
        "status": "STARTING", "invocation_id": REQUEST}})
    app._control_plane_write_context = lambda _handler: {"_console_principal": {"actor_id": "17"}}
    values = {"task_id": INSTANCE, "preview_invocation_id": PREVIEW}
    if selection:
        values["selected_bill_codes_json"] = '["R0002"]'
    app._parse_urlencoded_form = lambda _handler: values
    handler = SimpleNamespace(headers={"X-Browser-Request-UUID": REQUEST})
    if selection:
        app._handle_selection_preview_confirmation(handler)
        suffix = f"selection-previews/{PREVIEW}/confirm"
        expected = {"request_id": REQUEST, "selected_bill_codes": ["R0002"]}
    else:
        app._handle_scan_preview_confirmation(handler)
        suffix = "invoke"
        expected = {"request_id": REQUEST, "preview_invocation_id": PREVIEW}
    assert app.sent[0] == HTTPStatus.ACCEPTED
    assert app.calls[0][:3] == ("POST", f"/internal/v1/automation-projects/{INSTANCE}/{suffix}", expected)


def test_v2_selection_projection_keeps_exact_instance_and_rejects_extra_fields():
    projection = controls.AutomationControlPlaneCutoverTests._selection_projection(PREVIEW)
    projection["automation_id"] = INSTANCE
    def normalize(value, instance=INSTANCE):
        return normalize_selection_preview_projection(value, expected_automation_id=instance,
                                                      expected_invocation_id=PREVIEW)
    assert normalize(projection) == projection
    assert normalize(projection, REQUEST) is None
    assert normalize({**projection, "candidates": [{**projection["candidates"][0], "token": "untrusted"}]}) is None


def test_completed_v2_scan_preview_loads_its_own_projection():
    projection = {"contract_version": 3, "automation_id": INSTANCE,
        "preview_invocation_id": PREVIEW, "target_date": "2026-09-12",
        "observed_at": "2026-09-12T03:58:00Z", "expires_at": "2026-09-12T04:13:00Z",
        "source_page_count": 1, "normalized_record_count": 3, "selection_count": 2,
        "batch_count": 1, "can_confirm": True, "preview_state": "AVAILABLE"}
    app = controls._App([{"ok": True, "status": 200, "data": {
        "invocation_id": PREVIEW, "automation_id": INSTANCE, "invocation_phase": "preview", "plugin_id": "sync_scan_codes_v2",
        "status": "COMPLETED", "finished_at": "2026-09-12 12:00:00", "next_poll_after_ms": 0}},
        {"ok": True, "status": 200, "data": projection}])
    app._handle_automation_task_output(object(), {"invocation_id": [PREVIEW],
        "task_id": [INSTANCE], "scan_phase": ["preview"], "offset": ["0"]})
    assert app.calls[1][1] == f"/internal/v1/automation-projects/{INSTANCE}/scan-previews/{PREVIEW}"
    assert app.sent[1]["scan_preview"] == projection
