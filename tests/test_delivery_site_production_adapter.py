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
from tools.phase7_mysql_store import CONSOLE_WAYBILL_FIELDS


_SECRET = b"delivery-site-fresh-readback-test-secret"
_RESOURCE_ID = "phase7.delivery_status_bitable"
_SITE_BITABLE_ID = "phase7.site_send_bitable"
_SITE_SHEET_ID = "phase7.site_send_sheet"


class _Manager:
    def require_authenticated_binding(self, account_id: str) -> Mapping[str, Any]:
        return {
            "account_id": account_id,
            "system": "ronghui",
            "session_profile": "profile-test",
        }


def _resource_context() -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="delivery-test",
        plugin_version="1.0.0",
        tool_name="sync_delivery_status",
        operation="network.request",
        action="feishu.bitable.write_records",
        role="delivery_status_bitable",
        resource_id=_RESOURCE_ID,
        resource_bindings={"delivery_status_bitable": _RESOURCE_ID},
    )


def _projection_context() -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="delivery-test",
        plugin_version="1.0.0",
        tool_name="sync_delivery_status",
        operation="projection.invoke",
        action="waybill.delivery_status.update",
        role="account_id",
        account_ids=("ronghui-test",),
        account_bindings={"account_id": ("ronghui-test",)},
    )


def _site_context(*, action: str, role: str, resource_id: str) -> CoreBrokerInvocationContext:
    return CoreBrokerInvocationContext(
        automation_id="site-send-test",
        plugin_version="1.0.0",
        tool_name="sync_site_send_list",
        operation="network.request",
        action=action,
        role=role,
        resource_id=resource_id,
        resource_bindings={role: resource_id},
    )


def _bitable_row(record_id: str, waybill_no: str, status: str) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "fields": {"运单编号": waybill_no, "签收状态": status},
    }


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
    reads: list[tuple[str, str, int]] = []

    def feishu(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
        assert params["base_token"] == "base-test"
        assert params["table_id"] == "table-test"
        if action == "list_records":
            reads.append((action, params["table_id"], params["offset"]))
            return {"items": deepcopy(bitable_rows), "has_more": False}
        assert action == "write_records"
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

    def write_projection(codes: list[str], status: str) -> Mapping[str, Any]:
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
        _resource_context(),
        {
            "records": [
                {"record_id": "record-1", "status": "已签收"},
                {"record_id": "record-2", "status": "已签收"},
            ]
        },
    )
    projection = handlers[("projection.invoke", "waybill.delivery_status.update")](
        _projection_context(),
        {"bill_codes": ["WB-1", "WB-2"], "status": "signed"},
    )

    assert bitable["committed"] is True
    assert bitable["written"] == 2
    assert projection["committed"] is True
    assert projection["updated"] == 2
    assert len(reads) == 2
    assert all(table_id == "table-test" and offset == 0 for _, table_id, offset in reads)
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

    def write(codes: list[str], status: str) -> Mapping[str, Any]:
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
        projection_write=lambda codes, status: {"ok": True, "updated": 1},
    )

    with pytest.raises(PluginExecutionError) as exc:
        _delivery_handlers(ports)[("projection.invoke", "waybill.delivery_status.update")](
            _projection_context(),
            {"bill_codes": ["WB-1"], "status": "signed"},
        )
    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_delivery_write_response_loss_is_unknown_even_when_readback_matches() -> None:
    rows = {"WB-1": _projection_row("WB-1", "in_transit")}

    def write(codes: list[str], status: str) -> Mapping[str, Any]:
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
        if action == "list_records":
            return {"items": deepcopy(rows), "has_more": False}
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
            _resource_context(),
            {"records": [{"record_id": "record-1", "status": "已签收"}]},
        )
    assert exc.value.code == "BROKER_RESOURCE_MISMATCH"


def test_site_writes_bind_same_target_date_and_verify_both_exact_resources() -> None:
    bitable_rows = [_external_site_row("old", "OLD")]
    sheet_rows: list[list[Any]] = [["OLD", "旧站", "袋", 1, 2, "旧目的"]]
    sync_calls: list[tuple[str, str, str]] = []

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
    ) -> Mapping[str, Any]:
        nonlocal bitable_rows
        assert resource_id == _SITE_BITABLE_ID
        assert params["base_token"] == "site-base"
        assert params["table_id"] == "site-table"
        sync_calls.append(("bitable", resource_id, params["target_date"]))
        bitable_rows = [
            {"record_id": f"new-{index}", "fields": deepcopy(row["fields"])} for index, row in enumerate(records)
        ]
        return {"ok": True, "written": len(records)}

    def sync_sheet(
        resource_id: str,
        rows: list[list[Any]],
        params: dict[str, Any],
    ) -> Mapping[str, Any]:
        nonlocal sheet_rows
        assert resource_id == _SITE_SHEET_ID
        assert params["spreadsheet_token"] == "site-sheet-token"
        assert params["range"] == params["clear_range"] == "Data!A2:F100"
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
        ),
        {"records": records, "target_date": "2026-08-15"},
    )
    sheet = handlers[("network.request", "feishu.sheet.replace")](
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

    assert bitable["committed"] is True
    assert sheet["committed"] is True
    assert sheet_rows[0][3:5] == [0, 0]
    assert sync_calls == [
        ("bitable", _SITE_BITABLE_ID, "2026-08-15"),
        ("sheet", _SITE_SHEET_ID, "2026-08-15"),
    ]


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
        site_bitable_sync=lambda resource_id, records, params: {
            "ok": True,
            "written": 1,
        },
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
        site_sheet_sync=lambda resource_id, rows, params: {
            "ok": True,
            "rows": 1,
        },
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
            site_bitable_sync=sync,
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
            ),
            {
                "values": [["WB-1", "发货站", "纸箱", 0, 0, "目的站"]],
                "target_date": "2026-08-15",
            },
        )

    assert exc.value.code == "BROKER_RESOURCE_INVALID"
    assert writes == []


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

    def sync_bitable(resource_id, records, params):
        nonlocal bitable_rows
        bitable_rows = [
            {"record_id": f"new-{index}", "fields": deepcopy(row["fields"])} for index, row in enumerate(records)
        ]
        return {"ok": True, "written": response_count}

    def sync_sheet(resource_id, rows, params):
        nonlocal sheet_rows
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
