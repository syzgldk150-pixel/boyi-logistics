from __future__ import annotations

import ast
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.scripts import yunda_dispatch_forecast, yunda_send_waybills
from plugin_core_adapters import first_party as adapters
from tools import yunda_dispatch_forecast_sync_tool as dispatch_sink
from tools import yunda_send_waybills_sync_tool as send_sink
from tools import feishu_cli_tool


def _literal_assignment(path: Path, name: str):
    module = ast.parse(path.read_text(encoding="utf-8"))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"{name} is not a literal assignment")


def test_dispatch_payload_and_low_level_adapter_share_source_reviewed_field_contract(
    monkeypatch: pytest.MonkeyPatch,
):
    action_path = (
        ROOT
        / "agent"
        / "first_party_automation_plugins"
        / "sync_yunda_dispatch_forecast"
        / "payload"
        / "action.py"
    )
    field_map = _literal_assignment(action_path, "_FIELD_MAP")
    assert tuple(source for source, _target in field_map) == adapters._YUNDA_DISPATCH_SOURCE_FIELDS
    assert tuple(target for _source, target in field_map) == (
        "主单号",
        "开单件数",
        "扫描件数",
        "重量/kg",
        "体积/m3",
        "包装类型",
        "清场时间",
        "规划时效",
        "开单目的地址",
        "预计到达时间",
        "应派时间",
    )

    calls: list[tuple[object, object, object, object]] = []
    row = {
        field: f"source-{index}"
        for index, field in enumerate(adapters._YUNDA_DISPATCH_SOURCE_FIELDS)
    }
    row["unreviewed_server_field"] = "must-not-cross-broker"

    monkeypatch.setattr(adapters, "_yunda_session", lambda _descriptor: "session")

    def fetch_page(session, params, *, target_date, limit, offset):
        calls.append((session, params, target_date, (limit, offset)))
        return {"rows": [row], "total": 1}

    monkeypatch.setattr(yunda_dispatch_forecast, "fetch_page", fetch_page)
    result = adapters._yunda_dispatch_read_page(
        {"session_profile": "bound"},
        "2026-08-16",
        "branch-1",
        2,
        200,
    )

    assert calls == [
        (
            "session",
            {"dest_brch": "branch-1"},
            adapters.date(2026, 8, 16),
            (200, 400),
        )
    ]
    assert tuple(result["items"][0]) == adapters._YUNDA_DISPATCH_SOURCE_FIELDS
    assert "unreviewed_server_field" not in result["items"][0]
    assert result | {
        "returned": 1,
        "total": 1,
        "total_authoritative": True,
    } == result

    missing = dict(row)
    missing.pop("due_delv_dt")
    monkeypatch.setattr(
        yunda_dispatch_forecast,
        "fetch_page",
        lambda *_args, **_kwargs: {"rows": [missing], "total": 1},
    )
    with pytest.raises(PluginExecutionError) as exc:
        adapters._yunda_dispatch_read_page(
            {"session_profile": "bound"},
            "2026-08-16",
            "branch-1",
            0,
            200,
        )
    assert exc.value.code == "BROKER_SOURCE_INVALID"


def test_send_and_special_list_adapters_call_only_low_level_pages_and_project_allowlists(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(adapters, "_yunda_session", lambda _descriptor: "session")
    send_row = {
        field: f"send-{index}"
        for index, field in enumerate(adapters._YUNDA_SEND_SOURCE_FIELDS)
    }
    special_row = {
        field: f"special-{index}"
        for index, field in enumerate(adapters._YUNDA_SPECIAL_LINE_SOURCE_FIELDS)
    }
    send_row["unreviewed_server_field"] = "drop"
    special_row["unreviewed_server_field"] = "drop"
    calls: list[tuple[str, object, object, object, object]] = []

    def fetch_send_page(session, params, *, target_date, page, page_size):
        calls.append(("send", session, params, target_date, (page, page_size)))
        return {"rows": [send_row], "total": 1}

    def fetch_special_line_page(session, params, *, target_date, page, page_size):
        calls.append(("special", session, params, target_date, (page, page_size)))
        return {"rows": [special_row], "total": 1}

    monkeypatch.setattr(yunda_send_waybills, "fetch_send_page", fetch_send_page)
    monkeypatch.setattr(
        yunda_send_waybills,
        "fetch_special_line_page",
        fetch_special_line_page,
    )

    send = adapters._yunda_send_read_page(
        {"session_profile": "bound"}, "2026-08-14", 3, 100
    )
    special = adapters._yunda_special_line_read_page(
        {"session_profile": "bound"}, "2026-08-14", 4, 100
    )

    assert calls == [
        ("send", "session", {}, adapters.date(2026, 8, 14), (3, 100)),
        ("special", "session", {}, adapters.date(2026, 8, 14), (4, 100)),
    ]
    assert tuple(send["items"][0]) == adapters._YUNDA_SEND_SOURCE_FIELDS
    assert tuple(special["items"][0]) == adapters._YUNDA_SPECIAL_LINE_SOURCE_FIELDS
    assert "unreviewed_server_field" not in send["items"][0]
    assert "unreviewed_server_field" not in special["items"][0]


def test_yunda_detail_adapters_bind_echoed_identity_and_exact_contact_fields(
    monkeypatch: pytest.MonkeyPatch,
):
    bill_code = "TEST-WAYBILL-1"
    monkeypatch.setattr(adapters, "_yunda_session", lambda _descriptor: "session")
    tracking = {
        field: f"tracking-{index}"
        for index, field in enumerate(adapters._YUNDA_TRACKING_SOURCE_FIELDS)
    }
    tracking["Logistics_Id"] = bill_code
    tracking["unreviewed_server_field"] = "drop"
    original = {
        field: f"contact-{index}"
        for index, field in enumerate(adapters._YUNDA_ORIGINAL_SOURCE_FIELDS)
    }
    renderer = {
        "Logistics_Id": bill_code,
        "price": {"Total": "12.30", "Other": "drop"},
        "unreviewed_server_field": "drop",
    }

    monkeypatch.setattr(
        yunda_send_waybills,
        "fetch_waybill_detail",
        lambda session, requested, params: tracking,
    )
    monkeypatch.setattr(
        yunda_send_waybills,
        "fetch_original_data",
        lambda session, requested, params: original,
    )
    monkeypatch.setattr(
        yunda_send_waybills,
        "fetch_send_waybill_renderer",
        lambda session, requested, row, params: renderer,
    )

    projected_tracking = adapters._yunda_tracking_detail_read({}, bill_code)
    projected_original = adapters._yunda_original_data_read({}, bill_code)
    projected_renderer = adapters._yunda_renderer_detail_read({}, bill_code, "dot")

    assert tuple(projected_tracking) == adapters._YUNDA_TRACKING_SOURCE_FIELDS
    assert tuple(projected_original) == adapters._YUNDA_ORIGINAL_SOURCE_FIELDS
    assert projected_renderer == {
        "Logistics_Id": bill_code,
        "price": {"Total": "12.30"},
    }

    changed = dict(tracking, Logistics_Id="DIFFERENT-WAYBILL")
    monkeypatch.setattr(
        yunda_send_waybills,
        "fetch_waybill_detail",
        lambda session, requested, params: changed,
    )
    with pytest.raises(PluginExecutionError) as exc:
        adapters._yunda_tracking_detail_read({}, bill_code)
    assert exc.value.code == "BROKER_SOURCE_IDENTITY_MISMATCH"


def test_yunda_detail_missing_source_reviewed_shape_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(adapters, "_yunda_session", lambda _descriptor: "session")
    monkeypatch.setattr(
        yunda_send_waybills,
        "fetch_original_data",
        lambda session, requested, params: {"Buyer_Address": "only-one-field"},
    )
    with pytest.raises(PluginExecutionError) as original_exc:
        adapters._yunda_original_data_read({}, "TEST-WAYBILL-1")
    assert original_exc.value.code == "BROKER_SOURCE_INVALID"

    monkeypatch.setattr(
        yunda_send_waybills,
        "fetch_send_waybill_renderer",
        lambda session, requested, row, params: {
            "Logistics_Id": requested,
            "price": {},
        },
    )
    with pytest.raises(PluginExecutionError) as renderer_exc:
        adapters._yunda_renderer_detail_read({}, "TEST-WAYBILL-1", "dot")
    assert renderer_exc.value.code == "BROKER_SOURCE_INVALID"


def _dispatch_sink_record() -> dict[str, object]:
    record: dict[str, object] = {}
    for index, name in enumerate(dispatch_sink.FIELD_NAMES):
        record[name] = "1.25" if name in dispatch_sink.NUMBER_FIELDS else f"value-{index}"
    record[dispatch_sink.MAIN_FIELD_NAME] = "YD-DISPATCH-1"
    return record


def _send_sink_record() -> dict[str, object]:
    record: dict[str, object] = {}
    for index, name in enumerate(send_sink.FIELD_NAMES):
        record[name] = "1.25" if name in send_sink.NUMBER_FIELDS else f"value-{index}"
    record[send_sink.INDEX_FIELD_NAME] = "YD-SEND-1"
    record[send_sink.DATE_FIELD_NAME] = "2026-08-15"
    return record


def test_dispatch_bitable_write_is_followed_by_a_fresh_exact_resource_read(
    monkeypatch: pytest.MonkeyPatch,
):
    record = _dispatch_sink_record()
    expected_payload = dispatch_sink._build_records(
        [record],
        primary_field_name=dispatch_sink.MAIN_FIELD_NAME,
        has_explicit_main_field=True,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        adapters,
        "_exact_workflow_resource",
        lambda *_args, **_kwargs: {
            "base_token": "base",
            "table_id": "table",
        },
    )
    monkeypatch.setattr(
        dispatch_sink,
        "_ensure_fields",
        lambda *_args, **_kwargs: {
            "ok": True,
            "created": [],
            "primary_field_name": dispatch_sink.MAIN_FIELD_NAME,
            "has_explicit_main_field": True,
        },
    )

    def operation(action, params):
        del params
        calls.append(action)
        if action == "list_records" and calls.count("list_records") == 1:
            return {"data": {"items": [], "has_more": False}}
        if action == "write_records":
            return {"ok": True, "written": 1}
        if action == "list_records":
            return {
                "data": {
                    "items": [
                        {"record_id": "fresh-1", "fields": expected_payload[0]["fields"]}
                    ],
                    "has_more": False,
                }
            }
        raise AssertionError(action)

    monkeypatch.setattr(feishu_cli_tool, "feishu_operation", operation)
    result = adapters._append_yunda_dispatch_bitable(
        "resource",
        [record],
        "2026-08-16",
        True,
    )

    assert calls == ["list_records", "write_records", "list_records"]
    assert result | {
        "ok": True,
        "record_count": 1,
        "written": 1,
        "verified": True,
        "readback_count": 1,
    } == result
    assert len(result["readback_sha256"]) == 64


def test_empty_dispatch_append_is_a_verified_read_only_noop(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[str] = []

    monkeypatch.setattr(
        adapters,
        "_exact_workflow_resource",
        lambda *_args, **_kwargs: {"base_token": "base", "table_id": "table"},
    )
    monkeypatch.setattr(
        dispatch_sink,
        "_ensure_fields",
        lambda *_args, **_kwargs: pytest.fail(
            "an empty append must not mutate Bitable schema"
        ),
    )

    def operation(action: str, _params: object):
        calls.append(action)
        assert action == "list_records"
        return {"data": {"items": [], "has_more": False}}

    monkeypatch.setattr(feishu_cli_tool, "feishu_operation", operation)
    result = adapters._append_yunda_dispatch_bitable(
        "resource",
        [],
        "2026-08-16",
        True,
    )

    assert calls == ["list_records"]
    assert result | {
        "ok": True,
        "record_count": 0,
        "written": 0,
        "verified": True,
        "readback_count": 0,
        "no_op": True,
    } == result
    assert len(result["readback_sha256"]) == 64


@pytest.mark.parametrize("case", ["zero", "multiple", "incomplete"])
def test_dispatch_bitable_non_exact_readback_is_write_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
):
    record = _dispatch_sink_record()
    expected_payload = dispatch_sink._build_records(
        [record],
        primary_field_name=dispatch_sink.MAIN_FIELD_NAME,
        has_explicit_main_field=True,
    )
    complete = {"record_id": "fresh-1", "fields": expected_payload[0]["fields"]}
    if case == "zero":
        observed: list[dict[str, object]] = []
    elif case == "multiple":
        observed = [complete, {**complete, "record_id": "fresh-2"}]
    else:
        incomplete_fields = dict(expected_payload[0]["fields"])
        incomplete_fields.pop(dispatch_sink.FIELD_NAMES[-1])
        observed = [{"record_id": "fresh-1", "fields": incomplete_fields}]
    list_calls = 0

    monkeypatch.setattr(
        adapters,
        "_exact_workflow_resource",
        lambda *_args, **_kwargs: {"base_token": "base", "table_id": "table"},
    )
    monkeypatch.setattr(
        dispatch_sink,
        "_ensure_fields",
        lambda *_args, **_kwargs: {
            "ok": True,
            "created": [],
            "primary_field_name": dispatch_sink.MAIN_FIELD_NAME,
            "has_explicit_main_field": True,
        },
    )

    def operation(action, _params):
        nonlocal list_calls
        if action == "list_records":
            list_calls += 1
            items = [] if list_calls == 1 else observed
            return {"data": {"items": items, "has_more": False}}
        if action == "write_records":
            return {"ok": True, "written": 1}
        raise AssertionError(action)

    monkeypatch.setattr(feishu_cli_tool, "feishu_operation", operation)
    with pytest.raises(PluginExecutionError) as exc:
        adapters._append_yunda_dispatch_bitable(
            "resource",
            [record],
            "2026-08-16",
            True,
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_send_bitable_replace_reads_back_the_exact_target_date_snapshot(
    monkeypatch: pytest.MonkeyPatch,
):
    record = _send_sink_record()
    field_map = {name: name for name in send_sink.FIELD_NAMES}
    expected_payload, _updates, _creates = send_sink._build_records(
        [record],
        existing_by_waybill={},
        field_name_map=field_map,
        target_date=adapters.date(2026, 8, 15),
    )
    old = {
        "record_id": "old-1",
        "fields": {
            send_sink.INDEX_FIELD_NAME: "OLD",
            send_sink.DATE_FIELD_NAME: expected_payload[0]["fields"][send_sink.DATE_FIELD_NAME],
        },
    }
    calls: list[str] = []

    monkeypatch.setattr(
        adapters,
        "_exact_workflow_resource",
        lambda *_args, **_kwargs: {"base_token": "base", "table_id": "table"},
    )
    monkeypatch.setattr(
        send_sink,
        "_ensure_fields",
        lambda *_args, **_kwargs: {
            "ok": True,
            "created": [],
            "field_name_map": field_map,
        },
    )

    def operation(action, _params):
        calls.append(action)
        if action == "list_records" and calls.count("list_records") == 1:
            return {"data": {"items": [old], "has_more": False}}
        if action == "delete_records":
            return {"ok": True, "deleted": 1}
        if action == "write_records":
            return {"ok": True, "written": 1}
        if action == "list_records":
            return {
                "data": {
                    "items": [
                        {"record_id": "new-1", "fields": expected_payload[0]["fields"]}
                    ],
                    "has_more": False,
                }
            }
        raise AssertionError(action)

    monkeypatch.setattr(feishu_cli_tool, "feishu_operation", operation)
    result = adapters._replace_yunda_send_bitable(
        "resource",
        [record],
        "2026-08-15",
        True,
    )

    assert calls == ["list_records", "delete_records", "write_records", "list_records"]
    assert result | {
        "ok": True,
        "record_count": 1,
        "written": 1,
        "deleted": 1,
        "verified": True,
        "readback_count": 1,
    } == result


def test_send_sheet_replace_reads_back_every_identity_and_field(
    monkeypatch: pytest.MonkeyPatch,
):
    record = _send_sink_record()
    values = send_sink._build_sheet_values(
        [record],
        target_date=adapters.date(2026, 8, 15),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        adapters,
        "_exact_workflow_resource",
        lambda *_args, **_kwargs: {
            "spreadsheet_token": "sheet-token",
            "sheet_id": "Sheet1",
            "sheet_range": "A2:A2",
            "sheet_clear_range": "A2:Y5000",
        },
    )
    monkeypatch.setattr(
        "tools.phase7_sync_common.sync_sheet_snapshot",
        lambda resource_id, written, params: {
            "ok": True,
            "rows": len(written),
        },
    )

    def operation(action, _params):
        calls.append(action)
        assert action == "read_sheet"
        assert _params["range"] == "Sheet1!A2:Y5000"
        return {"ok": True, "data": {"valueRange": {"values": values}}}

    monkeypatch.setattr(feishu_cli_tool, "feishu_operation", operation)
    result = adapters._replace_yunda_send_sheet(
        "resource",
        [record],
        "2026-08-15",
        False,
    )

    assert calls == ["read_sheet"]
    assert result | {
        "ok": True,
        "record_count": 1,
        "written": 1,
        "verified": True,
        "readback_count": 1,
    } == result

    changed = [list(values[0])]
    changed[0][1] = "changed"
    monkeypatch.setattr(
        feishu_cli_tool,
        "feishu_operation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "data": {"valueRange": {"values": changed}},
        },
    )
    with pytest.raises(PluginExecutionError) as exc:
        adapters._replace_yunda_send_sheet(
            "resource",
            [record],
            "2026-08-15",
            False,
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_send_sheet_replace_rejects_a_stale_managed_tail_after_clear_ack(
    monkeypatch: pytest.MonkeyPatch,
):
    record = _send_sink_record()
    values = send_sink._build_sheet_values(
        [record],
        target_date=adapters.date(2026, 8, 15),
    )
    monkeypatch.setattr(
        adapters,
        "_exact_workflow_resource",
        lambda *_args, **_kwargs: {
            "spreadsheet_token": "sheet-token",
            "sheet_id": "Sheet1",
            "sheet_range": "A2:A2",
            "sheet_clear_range": "A2:Y5000",
        },
    )
    monkeypatch.setattr(
        "tools.phase7_sync_common.sync_sheet_snapshot",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        feishu_cli_tool,
        "feishu_operation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "data": {
                "valueRange": {
                    "values": [
                        *values,
                        ["" for _ in send_sink.FIELD_NAMES],
                        ["STALE", *["" for _ in send_sink.FIELD_NAMES[1:]]],
                    ]
                }
            },
        },
    )

    with pytest.raises(PluginExecutionError) as exc:
        adapters._replace_yunda_send_sheet(
            "resource",
            [record],
            "2026-08-15",
            False,
        )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_yunda_projection_uses_exact_fresh_source_date_readback(
    monkeypatch: pytest.MonkeyPatch,
):
    record = _send_sink_record()
    expected = send_sink._console_waybill_records(
        [record],
        target_date=adapters.date(2026, 8, 15),
    )[0]
    stale = {
        **expected,
        "waybill_no": "STALE",
        "source": "yunda",
    }
    state = [stale]

    def sync(rows, *, source, target_date, replace_date):
        assert source == "yunda"
        assert target_date.isoformat() == "2026-08-15"
        assert replace_date is True
        state[:] = [{**rows[0], "source": "yunda"}]
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(
        "tools.phase7_mysql_store.sync_console_waybills",
        sync,
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_console_waybills_by_source_date",
        lambda **_kwargs: [dict(row) for row in state],
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_console_waybills_by_numbers",
        lambda _identities: [],
    )

    result = adapters._replace_yunda_waybill_projection(
        [record],
        "2026-08-15",
    )

    assert result | {
        "ok": True,
        "verified": True,
        "record_count": 1,
        "readback_count": 1,
        "upserted": 1,
        "updates": 0,
        "creates": 1,
        "deleted_stale": 1,
    } == result
    assert len(result["readback_sha256"]) == 64


def test_yunda_projection_ack_with_mismatched_fresh_row_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
):
    record = _send_sink_record()
    expected = send_sink._console_waybill_records(
        [record],
        target_date=adapters.date(2026, 8, 15),
    )[0]
    state: list[dict[str, object]] = []

    def sync(*_args, **_kwargs):
        state[:] = [
            {
                **expected,
                "destination_site": "wrong-site",
                "source": "yunda",
            }
        ]
        return {"ok": True, "upserted": 1}

    monkeypatch.setattr(
        "tools.phase7_mysql_store.sync_console_waybills",
        sync,
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_console_waybills_by_source_date",
        lambda **_kwargs: [dict(row) for row in state],
    )
    monkeypatch.setattr(
        "tools.phase7_mysql_store.list_console_waybills_by_numbers",
        lambda _identities: [],
    )

    with pytest.raises(PluginExecutionError) as exc:
        adapters._replace_yunda_waybill_projection(
            [record],
            "2026-08-15",
        )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
