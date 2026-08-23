from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY_ROOT = ROOT / "agent" / "first_party_automation_plugins"


def _load_action(plugin_id: str):
    result_source = FIRST_PARTY_ROOT / "_runtime" / "result.py"
    result_spec = importlib.util.spec_from_file_location("boyi_plugin_result", result_source)
    assert result_spec is not None and result_spec.loader is not None
    result_module = importlib.util.module_from_spec(result_spec)
    previous = sys.modules.get("boyi_plugin_result")
    sys.modules["boyi_plugin_result"] = result_module
    result_spec.loader.exec_module(result_module)
    source = FIRST_PARTY_ROOT / plugin_id / "payload" / "action.py"
    spec = importlib.util.spec_from_file_location(f"{plugin_id}_plugin_action", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("boyi_plugin_result", None)
        else:
            sys.modules["boyi_plugin_result"] = previous
    return module


def _walk_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def test_yunda_dispatch_payload_owns_normalization_and_exact_resource_commit() -> None:
    action = _load_action("sync_yunda_dispatch_forecast")
    with pytest.raises(ValueError, match="dest_brch"):
        action._destination_branch({})
    calls: list[tuple[str, str, str, dict[str, Any]]] = []

    def broker(operation, *, action, role, arguments):
        call = (operation, action, role, copy.deepcopy(dict(arguments)))
        calls.append(call)
        if action == "yunda.dispatch_forecast.read_page":
            return {
                "items": [
                    {
                        "ship_id": "YD-MAIN-1",
                        "unit_cnt": "2",
                        "scan_cnt": 1,
                        "frgt_wgt": "12.50",
                        "frgt_vol": "0.125",
                        "pkg_lod_typ": "纸箱",
                        "fld_tm": "2026-08-16 01:00:00",
                        "plan_tlns": "24",
                        "rcv_cust_addr": "长沙市",
                        "est_arv_tm": "2026-08-16 08:00:00",
                        "due_delv_dt": "2026-08-16 12:00:00",
                    }
                ],
                "next_cursor": None,
                "pagination_complete": True,
                "evidence_ref": "broker-evidence:yunda-dispatch-source",
            }
        if action == "feishu.bitable.append_yunda_dispatch_forecast":
            assert role == "dispatch_forecast_bitable"
            assert arguments["target_date"] == "2026-08-16"
            assert arguments["records"] == [
                {
                    "主单号": "YD-MAIN-1",
                    "开单件数": 2,
                    "扫描件数": 1,
                    "重量/kg": "12.5",
                    "体积/m3": "0.125",
                    "包装类型": "纸箱",
                    "清场时间": "2026-08-16 01:00:00",
                    "规划时效": 24,
                    "开单目的地址": "长沙市",
                    "预计到达时间": "2026-08-16 08:00:00",
                    "应派时间": "2026-08-16 12:00:00",
                }
            ]
            return {
                "committed": True,
                "written": 1,
                "record_count": 1,
                "verified": True,
                "readback_count": 1,
                "readback_sha256": "a" * 64,
                "evidence_ref": "broker-evidence:yunda-dispatch-sink",
            }
        raise AssertionError(action)

    result = action.run_action(
        {
            "target_date": "2026-08-16",
            "dest_brch": "56739382",
            "ensure_fields": True,
        },
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["append_only"] is True
    assert result["data"]["written"] == 1
    assert [item[1] for item in calls] == [
        "yunda.dispatch_forecast.read_page",
        "feishu.bitable.append_yunda_dispatch_forecast",
    ]
    assert not any(
        key == "account_id" or key.endswith("_account_id")
        for _operation, _action, _role, arguments in calls
        for key in _walk_keys(arguments)
    )


def test_yunda_send_payload_owns_source_merge_enrichment_and_commit_order() -> None:
    action = _load_action("sync_yunda_send_waybills")
    with pytest.raises(ValueError, match="delivery method is missing"):
        action._delivery_method({}, {})
    calls: list[tuple[str, str, str, dict[str, Any]]] = []
    evidence_sequence = 0

    normal = {
        "Logistics_Id": "YD-1",
        "Buyer_Destination_Dot_Name": "长沙网点",
        "Buyer_Area_Name": "岳麓区",
        "Shipping_Methods": "231",
        "Item_Total_Number": 2,
        "Gross_Weight": "12.50",
        "Settlement_Total_Number": "13.00",
        "Volume": "0.125",
        "Special_Freight": "115.00",
        "Payment_Type": "到付",
        "Created_Dot_Code": "56739382",
    }
    special = {
        "Logistics_Id": "YD-2",
        "Buyer_Destination_Dot_Name": "昆明网点",
        "Buyer_Area": "官渡区",
        "Shipping_Methods": "不上楼",
        "Item_Total_Number": 3,
        "Gross_Weight": "20.00",
        "Settlement_Total_Number": "21.00",
        "Volume": "0.500",
        "Special_Freight": "349.00",
        "Payment_Type": "现金",
        "Total_Cost_Money": "1037.25",
    }

    def broker(operation, *, action, role, arguments):
        nonlocal evidence_sequence
        calls.append((operation, action, role, copy.deepcopy(dict(arguments))))
        evidence_sequence += 1
        evidence_ref = f"broker-evidence:yunda-send-{evidence_sequence}"
        if action == "yunda.send_waybill.list_page":
            return {
                "items": [normal],
                "next_cursor": None,
                "pagination_complete": True,
                "evidence_ref": evidence_ref,
            }
        if action == "yunda.special_line.list_page":
            return {
                "items": [special],
                "next_cursor": None,
                "pagination_complete": True,
                "evidence_ref": evidence_ref,
            }
        if action == "yunda.waybill.tracking_detail":
            bill = arguments["bill_code"]
            return {
                "record": {
                    "Logistics_Id": bill,
                    "Item_Name": "配件",
                    "Packing_Type": "纸箱",
                    "Extend_Field1": "200",
                    "COD": "115.00" if bill == "YD-1" else "0.00",
                },
                "evidence_ref": evidence_ref,
            }
        if action == "yunda.waybill.original_data":
            bill = arguments["bill_code"]
            return {
                "record": {
                    "Sender_Name": "发件人",
                    "Sender_Phone": "07310000000",
                    "Buyer_Name": f"收件人-{bill}",
                    "Buyer_Mobile": "13800000000",
                    "Buyer_Address": f"地址-{bill}",
                },
                "evidence_ref": evidence_ref,
            }
        if action == "yunda.send_waybill.renderer_detail":
            assert arguments == {
                "bill_code": "YD-1",
                "created_dot_code": "56739382",
            }
            return {
                "record": {"price": {"Total": "81.85"}},
                "evidence_ref": evidence_ref,
            }
        if action == "feishu.bitable.replace_yunda_send_waybills_date":
            records = arguments["records"]
            assert [record["5.14编号"] for record in records] == ["YD-1", "YD-2"]
            assert records[0]["提付"] == "115.00"
            assert records[0]["中转运费"] == "81.85"
            assert records[1]["现付"] == "349.00"
            assert records[1]["中转运费"] == "1037.25"
            return {
                "committed": True,
                "record_count": 2,
                "written": 2,
                "deleted": 1,
                "verified": True,
                "readback_count": 2,
                "readback_sha256": "b" * 64,
                "evidence_ref": evidence_ref,
            }
        if action == "waybill.yunda.replace_date":
            return {
                "committed": True,
                "record_count": 2,
                "upserted": 2,
                "deleted_stale": 0,
                "verified": True,
                "readback_count": 2,
                "readback_sha256": "d" * 64,
                "evidence_ref": evidence_ref,
            }
        if action == "feishu.sheet.replace_yunda_send_waybills":
            return {
                "committed": True,
                "record_count": 2,
                "written": 2,
                "verified": True,
                "readback_count": 2,
                "readback_sha256": "c" * 64,
                "evidence_ref": evidence_ref,
            }
        raise AssertionError(action)

    result = action.run_action(
        {
            "target_date": "2026-08-15",
            "page_size": 200,
            "max_pages": 50,
        },
        broker,
    )

    assert result["status"] == "SUCCESS"
    assert result["data"]["fetched"] == 2
    assert result["data"]["written"] == 2
    assert result["data"]["sql_upserted"] == 2
    assert result["data"]["sheet_rows"] == 2
    actions = [item[1] for item in calls]
    assert actions[-3:] == [
        "feishu.bitable.replace_yunda_send_waybills_date",
        "waybill.yunda.replace_date",
        "feishu.sheet.replace_yunda_send_waybills",
    ]
    assert actions.count("yunda.send_waybill.renderer_detail") == 1
    assert not any(
        key == "account_id" or key.endswith("_account_id")
        for _operation, _action, _role, arguments in calls
        for key in _walk_keys(arguments)
    )


def test_yunda_send_empty_source_clears_every_requested_snapshot() -> None:
    action_module = _load_action("sync_yunda_send_waybills")
    calls: list[str] = []

    def broker(operation, *, action, role, arguments):
        del operation, role
        calls.append(action)
        if action in {
            "yunda.send_waybill.list_page",
            "yunda.special_line.list_page",
        }:
            return {
                "items": [],
                "next_cursor": None,
                "pagination_complete": True,
                "evidence_ref": f"broker-evidence:{action}",
            }
        assert arguments["records"] == []
        assert arguments["target_date"] == "2026-08-24"
        if action == "waybill.yunda.replace_date":
            return {
                "committed": True,
                "upserted": 0,
                "deleted_stale": 1,
                "evidence_ref": "broker-evidence:yunda-empty-projection",
            }
        assert action in {
            "feishu.bitable.replace_yunda_send_waybills_date",
            "feishu.sheet.replace_yunda_send_waybills",
        }
        return {
            "committed": True,
            "record_count": 0,
            "written": 0,
            "deleted": 1,
            "verified": True,
            "readback_count": 0,
            "readback_sha256": "e" * 64,
            "evidence_ref": f"broker-evidence:{action}",
        }

    result = action_module.run_action({"target_date": "2026-08-24"}, broker)

    assert result["status"] == "SUCCESS"
    assert result["meta"]["record_count"] == 0
    assert result["data"]["evidence"]["execution_result"] == "no_data_cleared"
    assert calls == [
        "yunda.send_waybill.list_page",
        "yunda.special_line.list_page",
        "feishu.bitable.replace_yunda_send_waybills_date",
        "waybill.yunda.replace_date",
        "feishu.sheet.replace_yunda_send_waybills",
    ]


def test_yunda_send_payload_stops_before_crossing_broker_call_budget() -> None:
    action = _load_action("sync_yunda_send_waybills")
    action._MAX_BROKER_CALLS = 1
    calls: list[str] = []

    def broker(_operation, *, action, role, arguments):
        del role, arguments
        calls.append(action)
        return {
            "items": [],
            "next_cursor": None,
            "pagination_complete": True,
            "evidence_ref": "broker-evidence:first-page",
        }

    with pytest.raises(ValueError, match="broker call budget exhausted"):
        action.run_action(
            {"target_date": "2026-08-15", "dry_run": True},
            broker,
        )
    assert calls == ["yunda.send_waybill.list_page"]
