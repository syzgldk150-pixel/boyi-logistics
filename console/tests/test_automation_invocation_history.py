"""History reads recover exact Invocation identity without executing anything."""

from http import HTTPStatus
from types import SimpleNamespace
from uuid import uuid4

import pytest

from console.routes.automation import handle_get
from console.tests.test_automation_control_plane_cutover import _App


def invocation(**changes):
    return {"invocation_id": str(uuid4()), "automation_id": "daily_sign", "source": "feishu",
            "status": "RUNNING", "invocation_phase": "run", "started_at": "2026-09-10T01:00:00+00:00",
            "finished_at": None, **changes}


def test_recent_executions_use_signed_project_route_and_only_public_metadata():
    row = invocation(arguments_json={"internal": "private"}, result={"data": {"rows": [1]}})
    app = _App({"ok": True, "data": {"items": [row]}})
    assert handle_get(app, object(), "/automations/projects/daily_sign/invocations", "", {})
    assert app.sent[0] == HTTPStatus.OK
    assert app.calls[0][0:2] == ("GET", "/internal/v1/automation-projects/daily_sign/invocations")
    assert app.calls[0][4]["actor_id"] == "17"
    assert app.sent[1]["items"][0]["invocation_id"] == row["invocation_id"]
    assert app.sent[1]["items"][0]["source"] == "feishu"
    assert set(app.sent[1]["items"][0]) == {"invocation_id", "automation_id", "source", "status", "invocation_phase", "started_at", "finished_at"}
    assert app.repository.runtime_updates == []


@pytest.mark.parametrize("changes", [
    {"automation_id": "scan_codes"}, {"invocation_id": "not-a-uuid"},
    {"invocation_phase": "guessed"}, {"status": "QUEUED"}, {"source": "unknown"},
])
def test_history_rejects_mismatched_or_incomplete_rows(changes):
    app = _App({"ok": True, "data": {"items": [invocation(**changes)]}})
    app._handle_automation_invocations_get(object(), "daily_sign")
    assert app.sent[0] == HTTPStatus.BAD_GATEWAY
    assert "items" not in app.sent[1]


def test_history_empty_and_missing_principal_never_start_work():
    app = _App({"ok": True, "data": {"items": []}})
    app._handle_automation_invocations_get(object(), "daily_sign")
    assert app.sent == (HTTPStatus.OK, {"automation_id": "daily_sign", "items": []})
    app._control_plane_read_context = lambda _: None
    app._handle_automation_invocations_get(object(), "daily_sign")
    assert len(app.calls) == 1
    assert all(call[0] == "GET" for call in app.calls)


def test_completed_snapshot_replaces_output_even_when_old_offset_is_larger():
    row = invocation(status="COMPLETED", result={"status": "SUCCESS", "data": {"processed": 19}})
    app = _App({"ok": True, "data": row})
    app._handle_automation_task_output(object(), {"task_id": ["daily_sign"], "invocation_id": [row["invocation_id"]], "offset": ["999"]})
    payload = app.sent[1]
    assert payload["output_mode"] == "snapshot"
    assert payload["lines"][0] == "状态：已完成"
    assert '"processed": 19' in payload["lines"][-1]
    assert app.repository.runtime_updates == []
    assert app.automation_virtual_task_state == {}


def test_recovered_active_invocation_cancels_only_exact_project_identity():
    row = invocation(status="CANCELLING")
    app = _App([{"ok": True, "data": row}, {"ok": True, "data": row}])
    app._control_plane_write_context = lambda _: {"_console_principal": {"actor_id": "17"}}
    app._parse_urlencoded_form = lambda _: {"task_id": "daily_sign", "invocation_id": row["invocation_id"]}
    app._handle_automation_task_cancel(SimpleNamespace(headers={"X-Browser-Request-UUID": str(uuid4())}))
    assert app.sent[0] == HTTPStatus.ACCEPTED
    assert app.calls[1][1] == f"/internal/v1/automation-invocations/{row['invocation_id']}/cancel"
    assert len(app.calls) == 2


def test_failed_snapshot_preserves_actual_safe_error_code_without_reexecution():
    row = invocation(status="FAILED", error_code="CONNECTOR_RESPONSE_INVALID",
                     error_summary="The operation did not produce a complete result")
    app = _App({"ok": True, "data": row})
    app._handle_automation_task_output(object(), {"task_id": [row["automation_id"]],
                                                "invocation_id": [row["invocation_id"]]})
    assert "错误代码：CONNECTOR_RESPONSE_INVALID" in app.sent[1]["lines"]
    assert not app.sent[1]["runtime"]["ok"]
    assert [call[0] for call in app.calls] == ["GET"]


@pytest.mark.parametrize("code", [None, "", "<script>unsafe</script>", "x" * 1024])
def test_output_does_not_render_invalid_error_code(code):
    from console.services.automation_invocation_output import invocation_output_lines

    assert invocation_output_lines(invocation(status="FAILED", error_code=code),
                                   state_label="执行失败") == ["状态：执行失败"]


def test_preview_phase_comes_from_persisted_invocation_not_browser_query():
    row = invocation(automation_id="scan_codes", status="COMPLETED", invocation_phase="formal")
    app = _App({"ok": True, "data": row})
    app._handle_automation_task_output(object(), {"task_id": ["scan_codes"], "invocation_id": [row["invocation_id"]], "scan_phase": ["preview"]})
    assert app.sent[1]["invocation_phase"] == "formal"
    assert "scan_preview" not in app.sent[1]
    assert len(app.calls) == 1


def test_failed_output_exposes_actual_write_verification_counts_without_claiming_success():
    from console.services.automation_invocation_output import invocation_output_lines
    row = invocation(status="WRITE_OUTCOME_UNKNOWN", write_receipts=[
        {"receipt_id":"verified-record","outcome":"WRITE_VERIFIED"},
        {"receipt_id":"unknown-record","outcome":"WRITE_OUTCOME_UNKNOWN"},
        {"receipt_id":"not-applied-record","outcome":"NOT_APPLIED"},
    ])
    lines = invocation_output_lines(row, state_label="写入结果未确认")
    assert lines == ["状态：写入结果未确认", "写入核验：已确认 1 项，未确认 1 项。",
                     "第 2 项未确认，核验记录：unknown-record"]
