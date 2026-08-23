from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.delivery_site_handlers import (
    DeliverySiteHandlerPorts,
    build_delivery_site_handler_map,
)
from agent.automation_plugins.errors import PluginExecutionError
from plugin_core_adapters.delivery_site import build_production_delivery_site_ports
from plugin_core_adapters.first_party import _site_send_read_page
from tools.phase7_mysql_store import CONSOLE_WAYBILL_FIELDS


_SECRET = b"delivery-site-fresh-readback-test-secret"
_RESOURCE_ID = "phase7.delivery_status_bitable"
_SITE_BITABLE_ID = "phase7.site_send_bitable"
_SITE_SHEET_ID = "phase7.site_send_sheet"


def _stub_site_send_read(
    monkeypatch,
    *,
    canonical_bill_code: str | None = None,
    list_bill_code: str | None = None,
):
    from agent.tms_runtime.scripts import get_infor as bill_info
    from agent.tms_runtime.scripts import get_wangdiansendlist as source
    from agent.tms_runtime.scripts.login_manager import TMSAuth

    monkeypatch.setattr(TMSAuth, "login_and_get_session", lambda _self: object())
    monkeypatch.setattr(source, "build_date_range", lambda *_args: {})
    monkeypatch.setattr(source, "build_payload", lambda *_args: {})
    monkeypatch.setattr(source, "build_headers", lambda: {})
    monkeypatch.setattr(source, "fetch_page", lambda *_args: {})
    monkeypatch.setattr(
        source,
        "extract_rows",
        lambda _raw: [
            {
                "BILL_CODE": "WB-1",
                "SCAN_SITE": "发货站",
                "DESTINATION": "目的站",
                "PIECE_NUMBER": 1,
                "SETTLEMENT_WEIGHT": 2,
            }
        ],
    )
    monkeypatch.setattr(source, "fetch_bill_info_html", lambda *_args, **_kwargs: "<html />")
    detail_fields = {source.LABEL_PACK_TYPE: "纸箱"}
    if canonical_bill_code is not None:
        detail_fields[bill_info.LABEL_BILL_CODE] = canonical_bill_code
    if list_bill_code is not None:
        detail_fields[source.LABEL_BILL_CODE] = list_bill_code
    monkeypatch.setattr(source, "parse_bill_info_html", lambda _html: detail_fields)
    return source


def test_site_send_detail_without_identity_echo_keeps_exact_requested_binding(
    monkeypatch,
) -> None:
    _stub_site_send_read(monkeypatch)

    result = _site_send_read_page(
        {"session_profile": "profile-test"},
        "2026-08-23",
        0,
        100,
    )

    assert result["items"] == [
        {
            "tracking_number": "WB-1",
            "send_site": "发货站",
            "package_type": "纸箱",
            "destination": "目的站",
            "pieces": 1,
            "weight": 2,
        }
    ]


@pytest.mark.parametrize(
    ("canonical_bill_code", "list_bill_code"),
    [
        ("WB-OTHER", None),
        (None, "WB-OTHER"),
        ("WB-1", "WB-OTHER"),
    ],
)
def test_site_send_detail_rejects_any_mismatched_bill_identity(
    monkeypatch,
    canonical_bill_code,
    list_bill_code,
) -> None:
    _stub_site_send_read(
        monkeypatch,
        canonical_bill_code=canonical_bill_code,
        list_bill_code=list_bill_code,
    )

    with pytest.raises(PluginExecutionError) as exc_info:
        _site_send_read_page(
            {"session_profile": "profile-test"},
            "2026-08-23",
            0,
            100,
        )

    assert exc_info.value.code == "BROKER_SOURCE_INVALID"


class _Manager:
    def require_authenticated_binding(self, account_id: str) -> Mapping[str, Any]:
        return {
            "account_id": account_id,
            "system": "ronghui",
            "session_profile": "profile-test",
        }


def _resource_context(*, mark_write_started=None) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="delivery-test",
        plugin_version="1.0.0",
        tool_name="sync_delivery_status",
        operation="network.request",
        action="feishu.bitable.write_records",
        role="delivery_status_bitable",
        resource_id=_RESOURCE_ID,
        resource_bindings={"delivery_status_bitable": _RESOURCE_ID},
        mark_write_started=mark_write_started,
    )


def _projection_context(*, mark_write_started=None) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="delivery-test",
        plugin_version="1.0.0",
        tool_name="sync_delivery_status",
        operation="projection.invoke",
        action="waybill.delivery_status.update",
        role="account_id",
        account_ids=("ronghui-test",),
        account_bindings={"account_id": ("ronghui-test",)},
        mark_write_started=mark_write_started,
    )


def _site_context(
    *,
    action: str,
    role: str,
    resource_id: str,
    mark_write_started=None,
) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="site-send-test",
        plugin_version="1.0.0",
        tool_name="sync_site_send_list",
        operation="network.request",
        action=action,
        role=role,
        resource_id=resource_id,
        resource_bindings={role: resource_id},
        mark_write_started=mark_write_started,
    )


def _bitable_row(record_id: str, waybill_no: str, status: str) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "fields": {"运单编号": waybill_no, "签收状态": status},
    }


def _exact_bitable_record(
    rows: list[dict[str, Any]],
    params: Mapping[str, Any],
) -> dict[str, Any]:
    record_id = str(params.get("record_id") or "")
    matches = [row for row in rows if row["record_id"] == record_id]
    if len(matches) != 1:
        return {"error": "record unavailable"}
    return {"data": {"record": deepcopy(matches[0])}}


def _projection_row(waybill_no: str, status: str) -> dict[str, str]:
    row = {field: "" for field in CONSOLE_WAYBILL_FIELDS}
    row["waybill_no"] = waybill_no
    row["status"] = status
    return row


def _site_record(tracking_number: str, destination: str = "目的站") -> dict[str, Any]:
    return {
        "fields": {
            "tracking_number": tracking_number,
            "send_site": "发货站",
            "package_type": "纸箱",
            "destination": destination,
            "pieces": 0,
            "weight": 0,
        }
    }


def _external_site_row(
    record_id: str,
    tracking_number: str,
    destination: str = "目的站",
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "fields": {
            "运单编号": tracking_number,
            "发货网点": "发货站",
            "包装类型": "纸箱",
            "目的网点": destination,
            "件数": 0,
            "重量": 0,
        },
    }


def _delivery_handlers(
    ports: DeliverySiteHandlerPorts,
) -> dict[tuple[str, str], Any]:
    return build_delivery_site_handler_map(ports, cursor_secret=_SECRET)


def test_delivery_writes_require_exact_fresh_bitable_and_projection_snapshots() -> None:
    bitable_rows = [
        _bitable_row("record-1", "WB-1", "未签收"),
        _bitable_row("record-2", "WB-2", "未签收"),
    ]
    projection_rows = {
        "WB-1": _projection_row("WB-1", "in_transit"),
        "WB-2": _projection_row("WB-2", "in_transit"),
    }
    reads: list[tuple[str, str, str]] = []
    bitable_marks: list[str] = []
    projection_marks: list[str] = []

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        assert params["base_token"] == "base-test"
        assert params["table_id"] == "table-test"
        if action == "get_record":
            reads.append((action, params["table_id"], params["record_id"]))
            return _exact_bitable_record(bitable_rows, params)
        assert action == "write_records"
        assert bitable_marks == ["started"]
        assert set(params) == {
            "base_token",
            "table_id",
            "records",
            "as",
            "dry_run",
        }
        for update in params["records"]:
            match = [row for row in bitable_rows if row["record_id"] == update["record_id"]]
            assert len(match) == 1
            match[0]["fields"].update(update["fields"])
        return {"ok": True, "written": len(params["records"])}

    def read_projection(codes: tuple[str, ...]) -> list[dict[str, str]]:
        return [deepcopy(projection_rows[code]) for code in codes]

    def write_projection(codes: list[str], status: str, write_started) -> Mapping[str, Any]:
        write_started()
        assert projection_marks == ["started"]
        for code in codes:
            projection_rows[code]["status"] = status
        return {"ok": True, "updated": len(codes)}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "base-test",
            "table_id": "table-test",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=feishu,
        projection_read=read_projection,
        projection_write=write_projection,
    )
    handlers = _delivery_handlers(ports)

    bitable = handlers[("network.request", "feishu.bitable.write_records")](
        _resource_context(mark_write_started=lambda: bitable_marks.append("started")),
        {
            "records": [
                {"record_id": "record-1", "status": "已签收"},
                {"record_id": "record-2", "status": "已签收"},
            ]
        },
    )
    projection = handlers[("projection.invoke", "waybill.delivery_status.update")](
        _projection_context(mark_write_started=lambda: projection_marks.append("started")),
        {"bill_codes": ["WB-1", "WB-2"], "status": "signed"},
    )

    assert bitable["committed"] is True
    assert bitable["written"] == 2
    assert projection["committed"] is True
    assert projection["updated"] == 2
    assert len(reads) == 4
    assert bitable_marks == ["started"]
    assert projection_marks == ["started"]
    assert all(table_id == "table-test" for _, table_id, _ in reads)
    assert [record_id for _, _, record_id in reads] == [
        "record-1",
        "record-2",
        "record-1",
        "record-2",
    ]
    assert "base-test" not in bitable["evidence_ref"]
    assert _RESOURCE_ID not in bitable["evidence_ref"]
    assert "ronghui-test" not in projection["evidence_ref"]


@pytest.mark.parametrize("response_count", [0, 1, 3])
def test_delivery_projection_rejects_zero_partial_or_extra_write_counts(
    response_count: int,
) -> None:
    rows = {
        "WB-1": _projection_row("WB-1", "in_transit"),
        "WB-2": _projection_row("WB-2", "in_transit"),
    }

    def write(codes: list[str], status: str, write_started) -> Mapping[str, Any]:
        if write_started is not None:
            write_started()
        for code in codes:
            rows[code]["status"] = status
        return {"ok": True, "updated": response_count}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        projection_read=lambda codes: [deepcopy(rows[code]) for code in codes],
        projection_write=write,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("projection.invoke", "waybill.delivery_status.update")](
            _projection_context(),
            {"bill_codes": ["WB-1", "WB-2"], "status": "signed"},
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


@pytest.mark.parametrize(
    "post_rows",
    [
        [],
        [
            _projection_row("WB-1", "signed"),
            _projection_row("WB-1", "signed"),
        ],
        [
            _projection_row("WB-1", "signed"),
            _projection_row("WB-EXTRA", "signed"),
        ],
        [{"waybill_no": "WB-1", "status": "signed"}],
        [_projection_row("WB-1", "in_transit")],
    ],
    ids=["zero", "multiple", "extra", "missing-field", "inconsistent"],
)
def test_delivery_projection_post_write_anomalies_are_unknown(
    post_rows: list[dict[str, str]],
) -> None:
    read_count = 0

    def read(codes: tuple[str, ...]) -> list[dict[str, str]]:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return [_projection_row("WB-1", "in_transit")]
        return deepcopy(post_rows)

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        projection_read=read,
        projection_write=lambda codes, status, write_started: (
            (write_started() if write_started is not None else None)
            or {"ok": True, "updated": 1}
        ),
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("projection.invoke", "waybill.delivery_status.update")](
            _projection_context(),
            {"bill_codes": ["WB-1"], "status": "signed"},
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_delivery_write_response_loss_is_unknown_even_when_readback_matches() -> None:
    rows = {"WB-1": _projection_row("WB-1", "in_transit")}

    def write(codes: list[str], status: str, write_started) -> Mapping[str, Any]:
        if write_started is not None:
            write_started()
        rows["WB-1"]["status"] = status
        raise TimeoutError("response lost")

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        projection_read=lambda codes: [deepcopy(rows[code]) for code in codes],
        projection_write=write,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("projection.invoke", "waybill.delivery_status.update")](
            _projection_context(),
            {"bill_codes": ["WB-1"], "status": "signed"},
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("response_count", [0, 1, 3])
def test_delivery_bitable_rejects_zero_partial_or_extra_write_counts(
    response_count: int,
) -> None:
    rows = [
        _bitable_row("record-1", "WB-1", "未签收"),
        _bitable_row("record-2", "WB-2", "未签收"),
    ]

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        if action == "get_record":
            return _exact_bitable_record(rows, params)
        assert action == "write_records"
        for update in params["records"]:
            next(row for row in rows if row["record_id"] == update["record_id"])["fields"].update(update["fields"])
        return {"ok": True, "written": response_count}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "base-test",
            "table_id": "table-test",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=feishu,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.bitable.write_records")](
            _resource_context(),
            {
                "records": [
                    {"record_id": "record-1", "status": "已签收"},
                    {"record_id": "record-2", "status": "已签收"},
                ]
            },
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_delivery_pre_write_resource_binding_error_keeps_original_code() -> None:
    marks: list[str] = []
    def load_resource(resource_id: str) -> Mapping[str, Any]:
        raise PluginExecutionError(
            "resource revision changed",
            code="BROKER_RESOURCE_MISMATCH",
        )

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=load_resource,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.bitable.write_records")](
            _resource_context(mark_write_started=lambda: marks.append("started")),
            {"records": [{"record_id": "record-1", "status": "已签收"}]},
        )
    assert exc.value.code == "BROKER_RESOURCE_MISMATCH"
    assert marks == []


def test_delivery_pre_write_snapshot_failure_does_not_mark_write_started() -> None:
    marks: list[str] = []
    write_calls: list[object] = []

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        if action == "get_record":
            raise RuntimeError("fresh snapshot unavailable")
        write_calls.append(params)
        return {"ok": True, "written": 1}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "base-test",
            "table_id": "table-test",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=feishu,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.bitable.write_records")](
            _resource_context(mark_write_started=lambda: marks.append("started")),
            {"records": [{"record_id": "record-1", "status": "已签收"}]},
        )

    assert exc.value.code == "BROKER_RESOURCE_UNAVAILABLE"
    assert marks == []
    assert write_calls == []


def test_delivery_pre_write_identity_failure_does_not_mark_write_started() -> None:
    marks: list[str] = []
    write_calls: list[object] = []

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        if action == "get_record":
            return {
                "data": {
                    "record": _bitable_row("other-record", "WB-OTHER", "未签收")
                }
            }
        write_calls.append(params)
        return {"ok": True, "written": 1}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "base-test",
            "table_id": "table-test",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=feishu,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.bitable.write_records")](
            _resource_context(mark_write_started=lambda: marks.append("started")),
            {"records": [{"record_id": "record-1", "status": "已签收"}]},
        )

    assert exc.value.code == "BROKER_SOURCE_INVALID"
    assert marks == []
    assert write_calls == []


def test_nested_bitable_list_failure_is_failed_before_write() -> None:
    marks: list[str] = []
    read_count = 0

    def nested_list_failure(action: str, _params: dict[str, Any]) -> Mapping[str, Any]:
        nonlocal read_count
        assert action == "list_records"
        read_count += 1
        if read_count == 1:
            return {"data": {"items": [], "has_more": False}}
        return {"error": "Bitable list unavailable"}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "base-test",
            "table_id": "table-test",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=nested_list_failure,
        site_bitable_sync=lambda *_args: nested_list_failure("list_records", {}),
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.bitable.replace_snapshot")](
            _site_context(
                action="feishu.bitable.replace_snapshot",
                role="site_send_bitable",
                resource_id=_SITE_BITABLE_ID,
                mark_write_started=lambda: marks.append("started"),
            ),
            {"records": [_site_record("WB-1")], "target_date": "2026-08-15"},
        )

    assert exc.value.code == "FAILED_BEFORE_WRITE"
    assert marks == []
    assert read_count == 2


def test_nested_sheet_metadata_failure_is_failed_before_write() -> None:
    marks: list[str] = []
    reads: list[str] = []

    def read_sheet(action: str, _params: dict[str, Any]) -> Mapping[str, Any]:
        assert action == "read_sheet"
        reads.append(action)
        return {"values": []}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "sheet-test",
            "range": "Data!A2:F20",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=read_sheet,
        site_sheet_sync=lambda *_args: {"error": "fresh metadata unavailable"},
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.sheet.replace")](
            _site_context(
                action="feishu.sheet.replace",
                role="site_send_sheet",
                resource_id=_SITE_SHEET_ID,
                mark_write_started=lambda: marks.append("started"),
            ),
            {
                "values": [["WB-1", "发货站", "纸箱", 0, 0, "目的站"]],
                "target_date": "2026-08-15",
            },
        )

    assert exc.value.code == "FAILED_BEFORE_WRITE"
    assert marks == []
    assert reads == ["read_sheet"]


def test_nested_projection_schema_failure_is_failed_before_write() -> None:
    marks: list[str] = []
    rows = {"WB-1": _projection_row("WB-1", "in_transit")}

    def invalid_projection(*_args: object) -> Mapping[str, Any]:
        raise PluginExecutionError("projection schema changed", code="BROKER_SOURCE_INVALID")

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        projection_read=lambda codes: [deepcopy(rows[code]) for code in codes],
        projection_write=invalid_projection,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("projection.invoke", "waybill.delivery_status.update")](
            _projection_context(mark_write_started=lambda: marks.append("started")),
            {"bill_codes": ["WB-1"], "status": "signed"},
        )

    assert exc.value.code == "FAILED_BEFORE_WRITE"
    assert marks == []


def test_nested_projection_failure_after_marker_is_unknown() -> None:
    marks: list[str] = []
    rows = {"WB-1": _projection_row("WB-1", "in_transit")}

    def lost_response(_codes: list[str], _status: str, marker) -> Mapping[str, Any]:
        marker()
        raise TimeoutError("response lost after write attempt")

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        projection_read=lambda codes: [deepcopy(rows[code]) for code in codes],
        projection_write=lost_response,
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("projection.invoke", "waybill.delivery_status.update")](
            _projection_context(mark_write_started=lambda: marks.append("started")),
            {"bill_codes": ["WB-1"], "status": "signed"},
        )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert marks == ["started"]


def test_site_writes_bind_same_target_date_and_verify_both_exact_resources() -> None:
    bitable_rows = [_external_site_row("old", "OLD")]
    sheet_rows: list[list[Any]] = [["OLD", "旧站", "袋", 1, 2, "旧目的"]]
    sync_calls: list[tuple[str, str, str]] = []
    bitable_marks: list[str] = []
    sheet_marks: list[str] = []

    def load_resource(resource_id: str) -> Mapping[str, Any]:
        if resource_id == _SITE_BITABLE_ID:
            return {
                "resource_kind": "feishu_bitable",
                "base_token": "site-base",
                "table_id": "site-table",
                "_meta": {"resource_key": resource_id},
            }
        assert resource_id == _SITE_SHEET_ID
        return {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "site-sheet-token",
            "range": "Data!A2:F100",
            "clear_range": "Data!A2:F100",
            "_meta": {"resource_key": resource_id},
        }

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        if action == "list_records":
            assert params["base_token"] == "site-base"
            assert params["table_id"] == "site-table"
            return {"items": deepcopy(bitable_rows), "has_more": False}
        assert action == "read_sheet"
        assert params["spreadsheet_token"] == "site-sheet-token"
        assert params["range"] == "Data!A2:F100"
        return {"values": deepcopy(sheet_rows)}

    def sync_bitable(
        resource_id: str,
        records: list[dict[str, Any]],
        params: dict[str, Any],
        write_started,
    ) -> Mapping[str, Any]:
        nonlocal bitable_rows
        assert resource_id == _SITE_BITABLE_ID
        assert params["base_token"] == "site-base"
        assert params["table_id"] == "site-table"
        write_started()
        assert bitable_marks == ["started"]
        sync_calls.append(("bitable", resource_id, params["target_date"]))
        bitable_rows = [
            {"record_id": f"new-{index}", "fields": deepcopy(row["fields"])} for index, row in enumerate(records)
        ]
        return {"ok": True, "written": len(records)}

    def sync_sheet(
        resource_id: str,
        rows: list[list[Any]],
        params: dict[str, Any],
        write_started,
    ) -> Mapping[str, Any]:
        nonlocal sheet_rows
        assert resource_id == _SITE_SHEET_ID
        assert params["spreadsheet_token"] == "site-sheet-token"
        assert params["range"] == params["clear_range"] == "Data!A2:F100"
        write_started()
        assert sheet_marks == ["started"]
        sync_calls.append(("sheet", resource_id, params["target_date"]))
        sheet_rows = deepcopy(rows)
        return {"ok": True, "rows": len(rows)}

    handlers = _delivery_handlers(
        build_production_delivery_site_ports(
            account_manager=_Manager(),
            resource_loader=load_resource,
            feishu_operation=feishu,
            site_bitable_sync=sync_bitable,
            site_sheet_sync=sync_sheet,
        )
    )
    records = [_site_record("WB-1")]
    bitable = handlers[("network.request", "feishu.bitable.replace_snapshot")](
        _site_context(
            action="feishu.bitable.replace_snapshot",
            role="site_send_bitable",
            resource_id=_SITE_BITABLE_ID,
            mark_write_started=lambda: bitable_marks.append("started"),
        ),
        {"records": records, "target_date": "2026-08-15"},
    )
    sheet = handlers[("network.request", "feishu.sheet.replace")](
        _site_context(
            action="feishu.sheet.replace",
            role="site_send_sheet",
            resource_id=_SITE_SHEET_ID,
            mark_write_started=lambda: sheet_marks.append("started"),
        ),
        {
            "values": [["WB-1", "发货站", "纸箱", 0, 0, "目的站"]],
            "target_date": "2026-08-15",
        },
    )

    assert bitable["committed"] is True
    assert sheet["committed"] is True
    assert sheet_rows[0][3:5] == [0, 0]
    assert sync_calls == [
        ("bitable", _SITE_BITABLE_ID, "2026-08-15"),
        ("sheet", _SITE_SHEET_ID, "2026-08-15"),
    ]
    assert bitable_marks == ["started"]
    assert sheet_marks == ["started"]


@pytest.mark.parametrize(
    "post_rows",
    [
        [],
        [
            _external_site_row("one", "WB-1"),
            _external_site_row("two", "WB-1"),
        ],
        [
            _external_site_row("one", "WB-1"),
            _external_site_row("two", "WB-EXTRA"),
        ],
        [{"record_id": "one", "fields": {"运单编号": "WB-1"}}],
        [_external_site_row("one", "WB-1", "另一目的站")],
    ],
    ids=["zero", "multiple", "extra", "missing-field", "inconsistent"],
)
def test_site_bitable_post_write_anomalies_are_unknown(
    post_rows: list[dict[str, Any]],
) -> None:
    read_count = 0

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        nonlocal read_count
        assert action == "list_records"
        read_count += 1
        rows = [_external_site_row("old", "OLD")] if read_count == 1 else post_rows
        return {"items": deepcopy(rows), "has_more": False}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "site-base",
            "table_id": "site-table",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=feishu,
        site_bitable_sync=lambda resource_id, records, params, write_started: (
            (write_started() if write_started is not None else None)
            or {"ok": True, "written": 1}
        ),
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.bitable.replace_snapshot")](
            _site_context(
                action="feishu.bitable.replace_snapshot",
                role="site_send_bitable",
                resource_id=_SITE_BITABLE_ID,
            ),
            {"records": [_site_record("WB-1")], "target_date": "2026-08-15"},
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


@pytest.mark.parametrize(
    "post_rows",
    [
        [],
        [
            ["WB-1", "发货站", "纸箱", 0, 0, "目的站"],
            ["WB-1", "发货站", "纸箱", 0, 0, "目的站"],
        ],
        [
            ["WB-1", "发货站", "纸箱", 0, 0, "目的站"],
            ["WB-EXTRA", "发货站", "纸箱", 0, 0, "目的站"],
        ],
        [["WB-1", "发货站", "纸箱", 0, 0, "目的站", "额外列"]],
        [["WB-1", "发货站", "纸箱", 0, 0, "另一目的站"]],
    ],
    ids=["zero", "multiple", "extra", "extra-field", "inconsistent"],
)
def test_site_sheet_post_write_anomalies_are_unknown(
    post_rows: list[list[Any]],
) -> None:
    read_count = 0

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        nonlocal read_count
        assert action == "read_sheet"
        read_count += 1
        rows = [["OLD", "旧站", "袋", 1, 2, "旧目的"]] if read_count == 1 else post_rows
        return {"values": deepcopy(rows)}

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "site-sheet-token",
            "range": "Data!A2:F100",
            "clear_range": "Data!A2:F100",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=feishu,
        site_sheet_sync=lambda resource_id, rows, params, write_started: (
            (write_started() if write_started is not None else None)
            or {"ok": True, "rows": 1}
        ),
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.sheet.replace")](
            _site_context(
                action="feishu.sheet.replace",
                role="site_send_sheet",
                resource_id=_SITE_SHEET_ID,
            ),
            {
                "values": [["WB-1", "发货站", "纸箱", 0, 0, "目的站"]],
                "target_date": "2026-08-15",
            },
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_site_write_response_loss_is_unknown_and_target_date_is_required() -> None:
    rows = [_external_site_row("old", "OLD")]

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        assert action == "list_records"
        return {"items": deepcopy(rows), "has_more": False}

    def sync(resource_id, records, params):
        nonlocal rows
        rows = [_external_site_row("new", "WB-1")]
        raise TimeoutError("response lost")

    handlers = _delivery_handlers(
        build_production_delivery_site_ports(
            account_manager=_Manager(),
            resource_loader=lambda resource_id: {
                "resource_kind": "feishu_bitable",
                "base_token": "site-base",
                "table_id": "site-table",
                "_meta": {"resource_key": resource_id},
            },
            feishu_operation=feishu,
            site_bitable_sync=lambda resource_id, records, params, write_started: (
                (write_started() if write_started is not None else None)
                or sync(resource_id, records, params)
            ),
        )
    )
    context = _site_context(
        action="feishu.bitable.replace_snapshot",
        role="site_send_bitable",
        resource_id=_SITE_BITABLE_ID,
    )

    with pytest.raises(PluginExecutionError) as lost:
        handlers[("network.request", "feishu.bitable.replace_snapshot")](
            context,
            {"records": [_site_record("WB-1")], "target_date": "2026-08-15"},
        )
    assert lost.value.code == "WRITE_OUTCOME_UNKNOWN"

    with pytest.raises(PluginExecutionError) as missing:
        handlers[("network.request", "feishu.bitable.replace_snapshot")](
            context,
            {"records": [_site_record("WB-1")]},
        )
    assert missing.value.code == "BROKER_ARGUMENT_INVALID"


def test_site_invalid_sheet_binding_fails_before_write_with_original_code() -> None:
    writes: list[object] = []
    marks: list[str] = []
    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "site-sheet-token",
            "range": "not-an-a1-range",
            "_meta": {"resource_key": resource_id},
        },
        site_sheet_sync=lambda *args: writes.append(args) or {"ok": True, "rows": 1},
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.sheet.replace")](
            _site_context(
                action="feishu.sheet.replace",
                role="site_send_sheet",
                resource_id=_SITE_SHEET_ID,
                mark_write_started=lambda: marks.append("started"),
            ),
            {
                "values": [["WB-1", "发货站", "纸箱", 0, 0, "目的站"]],
                "target_date": "2026-08-15",
            },
        )

    assert exc.value.code == "BROKER_RESOURCE_INVALID"
    assert writes == []
    assert marks == []


@pytest.mark.parametrize("sink", ["bitable", "sheet"])
def test_empty_site_snapshot_is_a_verified_noop_only_when_both_reads_are_empty(
    sink: str,
) -> None:
    marks: list[str] = []
    sync_calls: list[str] = []

    def resource(resource_id: str) -> Mapping[str, Any]:
        if sink == "bitable":
            assert resource_id == _SITE_BITABLE_ID
            return {
                "resource_kind": "feishu_bitable",
                "base_token": "site-base",
                "table_id": "site-table",
                "_meta": {"resource_key": resource_id},
            }
        assert resource_id == _SITE_SHEET_ID
        return {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "site-sheet-token",
            "range": "Data!A2:F100",
            "clear_range": "Data!A2:F100",
            "_meta": {"resource_key": resource_id},
        }

    def feishu(action: str, _params: dict[str, Any]) -> Mapping[str, Any]:
        if sink == "bitable":
            assert action == "list_records"
            return {"items": [], "has_more": False}
        assert action == "read_sheet"
        return {"values": []}

    def no_op_bitable(*_args: object) -> Mapping[str, Any]:
        sync_calls.append("bitable")
        return {"ok": True, "written": 0}

    def no_op_sheet(*_args: object) -> Mapping[str, Any]:
        sync_calls.append("sheet")
        return {"ok": True, "rows": 0}

    handlers = _delivery_handlers(
        build_production_delivery_site_ports(
            account_manager=_Manager(),
            resource_loader=resource,
            feishu_operation=feishu,
            site_bitable_sync=no_op_bitable,
            site_sheet_sync=no_op_sheet,
        )
    )
    if sink == "bitable":
        result = handlers[("network.request", "feishu.bitable.replace_snapshot")](
            _site_context(
                action="feishu.bitable.replace_snapshot",
                role="site_send_bitable",
                resource_id=_SITE_BITABLE_ID,
                mark_write_started=lambda: marks.append("started"),
            ),
            {"records": [], "target_date": "2026-08-15"},
        )
    else:
        result = handlers[("network.request", "feishu.sheet.replace")](
            _site_context(
                action="feishu.sheet.replace",
                role="site_send_sheet",
                resource_id=_SITE_SHEET_ID,
                mark_write_started=lambda: marks.append("started"),
            ),
            {"values": [], "target_date": "2026-08-15"},
        )

    assert sync_calls == (["bitable"] if sink == "bitable" else [])
    assert marks == []
    assert result["_broker_verified_write_noop_v1"] is True
    assert result["record_count"] == result["readback_count"] == result["written"] == 0


def test_default_sheet_empty_snapshot_bypasses_clear_and_keeps_marker_unstarted() -> None:
    reads: list[str] = []
    marks: list[str] = []

    def feishu(action: str, _params: dict[str, Any]) -> Mapping[str, Any]:
        assert action == "read_sheet"
        reads.append(action)
        return {"values": []}

    # Do not inject ``site_sheet_sync``: the production default clears before
    # writing, so this proves the adapter's early no-op branch bypasses it.
    handlers = _delivery_handlers(
        build_production_delivery_site_ports(
            account_manager=_Manager(),
            resource_loader=lambda resource_id: {
                "resource_kind": "feishu_sheet",
                "spreadsheet_token": "site-sheet-token",
                "range": "Data!A2:F100",
                "clear_range": "Data!A2:F100",
                "_meta": {"resource_key": resource_id},
            },
            feishu_operation=feishu,
        )
    )
    result = handlers[("network.request", "feishu.sheet.replace")](
        _site_context(
            action="feishu.sheet.replace",
            role="site_send_sheet",
            resource_id=_SITE_SHEET_ID,
            mark_write_started=lambda: marks.append("started"),
        ),
        {"values": [], "target_date": "2026-08-15"},
    )

    assert reads == ["read_sheet", "read_sheet"]
    assert marks == []
    assert result["_broker_verified_write_noop_v1"] is True


@pytest.mark.parametrize("nonempty_target", [False, True])
def test_sheet_nonempty_input_or_target_still_mutates_once(nonempty_target: bool) -> None:
    sheet_rows: list[list[Any]] = (
        [["OLD", "旧站", "袋", 1, 2, "旧目的"]] if nonempty_target else []
    )
    marks: list[str] = []
    sync_calls: list[list[list[Any]]] = []

    def feishu(action: str, _params: dict[str, Any]) -> Mapping[str, Any]:
        assert action == "read_sheet"
        return {"values": deepcopy(sheet_rows)}

    def sync(_resource_id, rows, _params, marker) -> Mapping[str, Any]:
        nonlocal sheet_rows
        marker()
        sync_calls.append(deepcopy(rows))
        sheet_rows = deepcopy(rows)
        return {"ok": True, "rows": len(rows)}

    handlers = _delivery_handlers(
        build_production_delivery_site_ports(
            account_manager=_Manager(),
            resource_loader=lambda resource_id: {
                "resource_kind": "feishu_sheet",
                "spreadsheet_token": "site-sheet-token",
                "range": "Data!A2:F100",
                "clear_range": "Data!A2:F100",
                "_meta": {"resource_key": resource_id},
            },
            feishu_operation=feishu,
            site_sheet_sync=sync,
        )
    )
    values = [] if nonempty_target else [["WB-1", "发货站", "纸箱", 0, 0, "目的站"]]
    handlers[("network.request", "feishu.sheet.replace")](
        _site_context(
            action="feishu.sheet.replace",
            role="site_send_sheet",
            resource_id=_SITE_SHEET_ID,
            mark_write_started=lambda: marks.append("started"),
        ),
        {"values": values, "target_date": "2026-08-15"},
    )

    assert marks == ["started"]
    assert sync_calls == [values]


def test_delivery_bitable_marker_callback_failure_is_failed_before_write() -> None:
    rows = [_bitable_row("record-1", "WB-1", "未签收")]
    write_calls: list[str] = []

    def feishu(action: str, _params: dict[str, Any]) -> Mapping[str, Any]:
        if action == "get_record":
            return _exact_bitable_record(rows, _params)
        write_calls.append(action)
        raise AssertionError("write must not run after marker receipt failure")

    ports = build_production_delivery_site_ports(
        account_manager=_Manager(),
        resource_loader=lambda resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "base-test",
            "table_id": "table-test",
            "_meta": {"resource_key": resource_id},
        },
        feishu_operation=feishu,
    )

    def marker_failure() -> None:
        raise RuntimeError("durable receipt unavailable")

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("network.request", "feishu.bitable.write_records")](
            _resource_context(mark_write_started=marker_failure),
            {"records": [{"record_id": "record-1", "status": "已签收"}]},
        )

    assert exc.value.code == "FAILED_BEFORE_WRITE"
    assert write_calls == []


@pytest.mark.parametrize("response_count", [0, 1, 3])
@pytest.mark.parametrize("sink", ["bitable", "sheet"])
def test_site_sinks_reject_zero_partial_or_extra_write_counts(
    sink: str,
    response_count: int,
) -> None:
    desired_records = [_site_record("WB-1"), _site_record("WB-2")]
    desired_rows = [
        ["WB-1", "发货站", "纸箱", 0, 0, "目的站"],
        ["WB-2", "发货站", "纸箱", 0, 0, "目的站"],
    ]
    bitable_rows = [_external_site_row("old", "OLD")]
    sheet_rows: list[list[Any]] = [["OLD", "旧站", "袋", 1, 2, "旧目的"]]

    def load_resource(resource_id: str) -> Mapping[str, Any]:
        if sink == "bitable":
            return {
                "resource_kind": "feishu_bitable",
                "base_token": "site-base",
                "table_id": "site-table",
                "_meta": {"resource_key": resource_id},
            }
        return {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "site-sheet-token",
            "range": "Data!A2:F100",
            "clear_range": "Data!A2:F100",
            "_meta": {"resource_key": resource_id},
        }

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        if action == "list_records":
            return {"items": deepcopy(bitable_rows), "has_more": False}
        assert action == "read_sheet"
        return {"values": deepcopy(sheet_rows)}

    def sync_bitable(resource_id, records, params, write_started):
        nonlocal bitable_rows
        if write_started is not None:
            write_started()
        bitable_rows = [
            {"record_id": f"new-{index}", "fields": deepcopy(row["fields"])} for index, row in enumerate(records)
        ]
        return {"ok": True, "written": response_count}

    def sync_sheet(resource_id, rows, params, write_started):
        nonlocal sheet_rows
        if write_started is not None:
            write_started()
        sheet_rows = deepcopy(rows)
        return {"ok": True, "rows": response_count}

    handlers = _delivery_handlers(
        build_production_delivery_site_ports(
            account_manager=_Manager(),
            resource_loader=load_resource,
            feishu_operation=feishu,
            site_bitable_sync=sync_bitable,
            site_sheet_sync=sync_sheet,
        )
    )

    with pytest.raises(PluginExecutionError) as exc:
        if sink == "bitable":
            handlers[("network.request", "feishu.bitable.replace_snapshot")](
                _site_context(
                    action="feishu.bitable.replace_snapshot",
                    role="site_send_bitable",
                    resource_id=_SITE_BITABLE_ID,
                ),
                {"records": desired_records, "target_date": "2026-08-15"},
            )
        else:
            handlers[("network.request", "feishu.sheet.replace")](
                _site_context(
                    action="feishu.sheet.replace",
                    role="site_send_sheet",
                    resource_id=_SITE_SHEET_ID,
                ),
                {"values": desired_rows, "target_date": "2026-08-15"},
            )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
