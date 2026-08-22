from __future__ import annotations

from copy import deepcopy

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from plugin_core_adapters.daily_send import build_production_daily_send_ports


_FIELDS = {
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
_PROJECTION = {
    "waybill_no": "R001",
    "destination_site": "长沙",
    "open_date": "2026-05-12",
    "receiver_address": "测试地址",
    "receiver_name": "收货人",
    "receiver_phone": "13100000000",
    "sender_name": "寄件人",
    "sender_phone": "13000000000",
    "goods_name_lines": "配件",
    "package_type_lines": "纸箱",
    "quantity_lines": "2",
    "weight_volume": "实际重量 10.50",
    "delivery_method": "送货",
    "freight_fee": "12.50",
    "pickup_fee": "",
    "delivery_fee": "",
    "transfer_fee": "",
    "payment_method": "现付",
    "insurance_amount": "",
    "cod_amount": "3.40",
    "remark": "测试",
    "scan_status": "签收扫描",
    "status": "signed",
}


class _AccountManager:
    def require_authenticated_binding(self, account_id):
        return {
            "account_id": account_id,
            "system": "ronghui",
            "session_profile": "profile",
        }


def _resource_loader(resource_id):
    assert resource_id == "exact-resource"
    return {
        "resource_kind": "feishu_bitable",
        "base_token": "core-only-base",
        "table_id": "core-only-table",
    }


def test_bitable_write_and_delete_are_decided_by_fresh_exact_readback():
    records: list[dict] = []

    def feishu(action, params):
        assert params["base_token"] == "core-only-base"
        assert params["table_id"] == "core-only-table"
        if action == "list_records":
            offset = int(params["offset"])
            limit = int(params["limit"])
            page = records[offset : offset + limit]
            return {"ok": True, "items": deepcopy(page)}
        if action == "write_records":
            records[:] = [
                {
                    "record_id": f"record-{index}",
                    "fields": deepcopy(record["fields"]),
                }
                for index, record in enumerate(params["records"], start=1)
            ]
            raise TimeoutError("response lost after commit")
        if action == "delete_records":
            deleted = set(params["record_ids"])
            records[:] = [record for record in records if record["record_id"] not in deleted]
            raise TimeoutError("response lost after commit")
        raise AssertionError(action)

    ports = build_production_daily_send_ports(
        account_manager=_AccountManager(),
        resource_loader=_resource_loader,
        feishu_operation=feishu,
        source_page=lambda *_args: {"items": [], "total": 0},
        projection_sync=lambda *_args: {"ok": True},
        projection_read=lambda _date: [],
        projection_lookup=lambda _waybill: None,
    )
    written = ports.bitable_write(
        "exact-resource",
        [{"fields": deepcopy(_FIELDS)}],
    )
    assert written == {"ok": True, "verified": True, "written": 1}
    deleted = ports.bitable_delete("exact-resource", ("record-1",))
    assert deleted == {"ok": True, "verified": True, "deleted": 1}


def test_bitable_write_fails_closed_when_fresh_fields_do_not_match():
    def feishu(action, params):
        if action == "write_records":
            return {"ok": True, "written": 1}
        if action == "list_records":
            wrong = deepcopy(_FIELDS)
            wrong["目的网点"] = "another-site"
            return {
                "ok": True,
                "items": [{"record_id": "record-1", "fields": wrong}],
            }
        raise AssertionError((action, params))

    ports = build_production_daily_send_ports(
        account_manager=_AccountManager(),
        resource_loader=_resource_loader,
        feishu_operation=feishu,
        source_page=lambda *_args: {"items": [], "total": 0},
        projection_sync=lambda *_args: {"ok": True},
        projection_read=lambda _date: [],
        projection_lookup=lambda _waybill: None,
    )
    assert ports.bitable_write(
        "exact-resource",
        [{"fields": deepcopy(_FIELDS)}],
    ) == {"ok": False, "verified": False, "written": 0}


@pytest.mark.parametrize("mutation", ["write", "delete"])
@pytest.mark.parametrize(
    "invalid_readback",
    [
        {"ok": True, "items": [None]},
        {"ok": True},
    ],
)
def test_bitable_postwrite_malformed_item_page_is_unknown(
    mutation: str,
    invalid_readback: dict,
):
    list_calls = 0

    def feishu(action, _params):
        nonlocal list_calls
        if action in {"write_records", "delete_records"}:
            return {"ok": True}
        if action == "list_records":
            list_calls += 1
            if list_calls == 1:
                return {
                    "ok": True,
                    "items": (
                        []
                        if mutation == "write"
                        else [
                            {
                                "record_id": "record-1",
                                "fields": deepcopy(_FIELDS),
                            }
                        ]
                    ),
                }
            return deepcopy(invalid_readback)
        raise AssertionError(action)

    ports = build_production_daily_send_ports(
        account_manager=_AccountManager(),
        resource_loader=_resource_loader,
        feishu_operation=feishu,
        source_page=lambda *_args: {"items": [], "total": 0},
        projection_sync=lambda *_args: {"ok": True},
        projection_read=lambda _date: [],
        projection_lookup=lambda _waybill: None,
    )

    with pytest.raises(PluginExecutionError) as exc:
        if mutation == "write":
            ports.bitable_write(
                "exact-resource",
                [{"fields": deepcopy(_FIELDS)}],
            )
        else:
            ports.bitable_delete("exact-resource", ("record-1",))

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("mutation", ["write", "delete"])
def test_bitable_mutation_must_preserve_every_prior_unmanaged_record(
    mutation: str,
):
    records = [
        {
            "record_id": "unrelated",
            "fields": {**deepcopy(_FIELDS), "运单编号": "OTHER"},
        }
    ]
    if mutation == "delete":
        records.append(
            {
                "record_id": "target",
                "fields": deepcopy(_FIELDS),
            }
        )

    def feishu(action, params):
        if action == "list_records":
            offset = int(params["offset"])
            limit = int(params["limit"])
            return {"ok": True, "items": deepcopy(records[offset : offset + limit])}
        if action == "write_records":
            records[:] = [
                {
                    "record_id": "fresh",
                    "fields": deepcopy(params["records"][0]["fields"]),
                }
            ]
            return {"ok": True, "written": 1}
        if action == "delete_records":
            records.clear()
            return {"ok": True, "deleted": 1}
        raise AssertionError(action)

    ports = build_production_daily_send_ports(
        account_manager=_AccountManager(),
        resource_loader=_resource_loader,
        feishu_operation=feishu,
        source_page=lambda *_args: {"items": [], "total": 0},
        projection_sync=lambda *_args: {"ok": True},
        projection_read=lambda _date: [],
        projection_lookup=lambda _waybill: None,
    )

    if mutation == "write":
        result = ports.bitable_write(
            "exact-resource",
            [{"fields": deepcopy(_FIELDS)}],
        )
        assert result == {"ok": False, "verified": False, "written": 0}
    else:
        result = ports.bitable_delete("exact-resource", ("target",))
        assert result == {"ok": False, "verified": False, "deleted": 0}


def test_projection_counts_and_success_come_from_prestate_and_fresh_rows():
    state = [
        {
            **_PROJECTION,
            "waybill_no": "STALE",
            "status": "in_transit",
        }
    ]

    def sync(records, target_date):
        assert target_date == "2026-05-12"
        state[:] = deepcopy(records)
        raise TimeoutError("response lost after commit")

    ports = build_production_daily_send_ports(
        account_manager=_AccountManager(),
        resource_loader=_resource_loader,
        feishu_operation=lambda *_args: {"items": []},
        source_page=lambda *_args: {"items": [], "total": 0},
        projection_sync=sync,
        projection_read=lambda _date: deepcopy(state),
        projection_lookup=lambda _waybill: None,
    )
    result = ports.projection_replace([deepcopy(_PROJECTION)], "2026-05-12")
    assert result == {
        "ok": True,
        "verified": True,
        "upserted": 1,
        "updates": 0,
        "creates": 1,
        "deleted_stale": 1,
    }


def test_exact_account_and_resource_are_never_defaulted():
    loaded: list[str] = []

    def load(resource_id):
        loaded.append(resource_id)
        return _resource_loader(resource_id)

    ports = build_production_daily_send_ports(
        account_manager=_AccountManager(),
        resource_loader=load,
        feishu_operation=lambda _action, _params: {"ok": True, "items": []},
        source_page=lambda descriptor, *_args: {
            "items": [{"BILL_CODE": descriptor["account_id"]}],
            "total": 1,
        },
        projection_sync=lambda *_args: {"ok": True},
        projection_read=lambda _date: [],
        projection_lookup=lambda _waybill: None,
    )
    assert ports.describe_account("chosen-account")["account_id"] == "chosen-account"
    assert ports.source_page(
        ports.describe_account("chosen-account"),
        "2026-05-12",
        0,
        100,
    )["items"] == [{"BILL_CODE": "chosen-account"}]
    ports.bitable_list("exact-resource", 0, 200, ("运单编号", "发件日期"))
    assert loaded == ["exact-resource"]
