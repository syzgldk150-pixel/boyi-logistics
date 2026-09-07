from __future__ import annotations

from copy import deepcopy

import pytest

from agent import feishu_readback
from agent.automation_plugins.errors import PluginExecutionError
from plugin_core_adapters.daily_send import build_production_daily_send_ports
from tools import daily_sign_sync_tool


@pytest.fixture(autouse=True)
def _disable_readback_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_readback.time, "sleep", lambda _delay: None)


def test_shared_readback_retries_stale_observations_without_a_second_write() -> None:
    reads = 0
    sleeps: list[float] = []

    def reader() -> dict[str, str]:
        nonlocal reads
        reads += 1
        return {"state": "new" if reads == 3 else "old"}

    # The test models one already-issued write; the shared helper only sees
    # the reader and therefore cannot accidentally issue another mutation.
    original_sleep = feishu_readback.time.sleep
    feishu_readback.time.sleep = sleeps.append
    try:
        observed = feishu_readback.retry_readback(
            reader,
            lambda value: value == {"state": "new"},
            label="test mutation",
        )
    finally:
        feishu_readback.time.sleep = original_sleep

    assert observed == {"state": "new"}
    assert reads == 3
    assert sleeps == [0.5, 1.0]


def test_shared_readback_persistent_mismatch_is_unknown_after_four_reads() -> None:
    reads = 0

    def reader() -> dict[str, str]:
        nonlocal reads
        reads += 1
        return {"state": "old"}

    with pytest.raises(PluginExecutionError) as failure:
        feishu_readback.retry_readback(
            reader,
            lambda value: value == {"state": "new"},
            label="test mutation",
        )

    assert failure.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert reads == 4


_SEND_FIELDS = {
    "运单编号": "R001",
    "发件日期": 1_778_515_200_000,
    "签收状态": "已签收",
    "目的网点": "长沙",
    "收件区/县": "大祥区",
    "收件地址": "测试地址",
    "寄件人": "寄件人",
    "寄件手机": "13000000000",
    "收货人": "收货人",
    "收货电话": "13100000000",
    "货物名称": "配件",
    "包装类型": "纸箱",
    "派送方式": "送货",
    "件数": 2,
    "实际重量": "10.50",
    "录单金额": "12.50",
    "回单号": "",
    "备注": "测试",
    "支付类型": "现付",
    "体积重量": "11.25",
    "体积": "0.25",
    "结算重量": "11",
    "到付款": "3.40",
}


def _daily_send_ports(feishu):
    return build_production_daily_send_ports(
        resource_loader=lambda _resource_id: {
            "resource_kind": "feishu_bitable",
            "base_token": "base",
            "table_id": "table",
        },
        feishu_operation=feishu,
        source_page=lambda *_args: {"items": [], "total": 0},
        projection_sync=lambda *_args: {"ok": True},
        projection_read=lambda _date: [],
        projection_lookup=lambda _waybill: None,
    )


def test_daily_send_lost_write_ack_is_proved_by_delayed_bitable_readback() -> None:
    records: list[dict[str, object]] = []
    list_calls = 0

    def feishu(action: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal list_calls
        if action == "list_records":
            list_calls += 1
            visible = [] if list_calls == 2 else records
            return {"ok": True, "items": deepcopy(visible)}
        if action == "write_records":
            records[:] = [
                {"record_id": "record-1", "fields": deepcopy(params["records"][0]["fields"])}
            ]
            raise TimeoutError("response lost after commit")
        raise AssertionError(action)

    ports = _daily_send_ports(feishu)
    result = ports.bitable_write("resource", [{"fields": deepcopy(_SEND_FIELDS)}])

    assert result == {"ok": True, "verified": True, "written": 1}
    assert list_calls == 3


def test_daily_sign_schema_lost_ack_is_proved_without_repeating_field_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = {
        "运单编号": 1,
        "R13应签收时间": 1,
        "问题件后应签时间": 1,
        "货物品名": 1,
        "包装类型": 1,
        "货物件数": 2,
        "收件人地址": 1,
        "送货方式": 1,
        "到货件数": 2,
    }
    fields = [
        {"field_name": name, "type": value}
        for name, value in required.items()
        if name != "到货件数"
    ]
    complete_fields = fields + [{"field_name": "到货件数", "type": 2}]
    list_calls = 0
    create_calls = 0

    def feishu(action: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal list_calls, create_calls
        if action == "list_fields":
            list_calls += 1
            visible = fields if list_calls < 3 else complete_fields
            return {"ok": True, "items": deepcopy(visible)}
        if action == "create_field":
            create_calls += 1
            raise TimeoutError("response lost after schema commit")
        raise AssertionError(action)

    # The first list is missing the field, the first fresh read is still stale,
    # and the second fresh read exposes the field created by the lost ACK.
    monkeypatch.setattr(daily_sign_sync_tool, "feishu_operation", feishu)
    result = daily_sign_sync_tool._ensure_bitable_schema("base", "table", {})

    assert result["ok"] is True
    assert create_calls == 1
    assert list_calls == 3


def test_daily_sign_sheet_lost_write_ack_is_proved_by_delayed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = [["R1", "2026-08-15 23:59:59", "", "货物", "纸箱", 2, "地址", "派送", ""]]
    blank = [[], []]
    readback_calls = 0
    write_calls = 0

    monkeypatch.setattr(
        daily_sign_sync_tool,
        "resolve_sheet_target",
        lambda _params, _key: ("sheet-token", "每日应签!A2:I3"),
    )
    monkeypatch.setattr(
        daily_sign_sync_tool,
        "_build_ledger_sheet_values",
        lambda _rows: deepcopy(expected),
    )

    def feishu(action: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal readback_calls, write_calls
        if action == "read_sheet":
            value_range = str(params["range"])
            if value_range.endswith("A1:I1"):
                return {"ok": True, "values": [daily_sign_sync_tool.SHEET_HEADERS]}
            readback_calls += 1
            return {"ok": True, "values": blank if readback_calls == 1 else expected + [[]]}
        if action == "write_sheet":
            write_calls += 1
            raise TimeoutError("response lost after commit")
        if action == "clear_sheet":
            raise AssertionError("an uncertain data write must not trigger a second mutation")
        raise AssertionError(action)

    monkeypatch.setattr(daily_sign_sync_tool, "feishu_operation", feishu)
    result = daily_sign_sync_tool._sync_sheet([{}], {})

    assert result["ok"] is True
    assert result["readback"]["verified"] is True
    assert write_calls == 1
    assert readback_calls == 2


def test_daily_sign_bitable_lost_write_ack_is_proved_by_delayed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = {
        "运单编号": 1,
        "R13应签收时间": 1,
        "问题件后应签时间": 1,
        "货物品名": 1,
        "包装类型": 1,
        "货物件数": 2,
        "收件人地址": 1,
        "送货方式": 1,
        "到货件数": 2,
    }
    target = [{"fields": {"运单编号": "R1"}}]
    stored: list[dict[str, object]] = []
    record_reads = 0
    write_calls = 0

    monkeypatch.setattr(
        daily_sign_sync_tool,
        "resolve_bitable_target",
        lambda _params, _key: ("base", "table"),
    )
    monkeypatch.setattr(daily_sign_sync_tool, "_build_ledger_records", lambda _rows, date_field_type=1: deepcopy(target))

    def feishu(action: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal record_reads, write_calls
        if action == "list_fields":
            return {"ok": True, "items": [{"field_name": name, "type": value} for name, value in required.items()]}
        if action == "list_records":
            record_reads += 1
            visible = [] if record_reads == 2 else stored
            return {"ok": True, "items": deepcopy(visible)}
        if action == "write_records":
            write_calls += 1
            stored[:] = [{"record_id": "record-1", "fields": deepcopy(params["records"][0]["fields"])}]
            raise TimeoutError("response lost after commit")
        raise AssertionError(action)

    monkeypatch.setattr(daily_sign_sync_tool, "feishu_operation", feishu)
    result = daily_sign_sync_tool._sync_bitable([{}], {})

    assert result["ok"] is True
    assert result["readback"]["verified"] is True
    assert write_calls == 1
    assert record_reads == 3
