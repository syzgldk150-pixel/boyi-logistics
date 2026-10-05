"""Unchanged daily-sign rows must not be rewritten because of legacy columns."""
from copy import deepcopy

import pytest

from service_v2_plugins.sync_daily_should_sign_v2.payload.business import (
    daily_sign_readback as readback,
    daily_sign_sync_tool as sync,
)


@pytest.mark.parametrize("changed", [False, True])
def test_fifteen_rows_only_write_managed_field_changes(monkeypatch, changed):
    rows = [{"tracking_number": f"R{index:04}", "expected_quantity": 3,
             "arrived_quantity": 0} for index in range(15)]
    target = sync._build_ledger_records(rows)
    items = [{"record_id": f"rec-{index}", "fields": {
        **{key: value for key, value in row["fields"].items() if value not in (None, "")},
        "问题件后应签时间": "retained legacy value",
    }} for index, row in enumerate(target)]
    if changed:
        items[4]["fields"]["到货件数"] = 1
    calls, writes = [], []

    def operation(action, params):
        calls.append(action)
        if action == "list_fields":
            return {"items": [{"field_name": name,
                "type": 2 if name in {"货物件数", "到货件数"} else 1}
                for name in sync.SHEET_HEADERS]}
        if action == "list_records":
            return {"items": deepcopy(items), "has_more": False}
        if action == "write_records":
            writes.extend(deepcopy(params["records"]))
            for record in params["records"]:
                stored = next(item for item in items if item["record_id"] == record["record_id"])
                stored["fields"].update(record["fields"])
            return {"ok": True, "written": len(params["records"])}
        raise AssertionError(f"Unexpected operation: {action}")

    monkeypatch.setattr(sync, "resolve_bitable_target", lambda *_: ("base", "table"))
    monkeypatch.setattr(sync, "feishu_operation", operation)
    result = sync._sync_bitable(rows, {})
    assert result["ok"] and result["readback"]["verified"]
    assert result["readback"]["record_count"] == 15
    assert result["unchanged"] == (14 if changed else 15)
    assert result["written"] == len(writes) == (1 if changed else 0)
    assert calls == ["list_fields", "list_records"] + (
        ["write_records", "list_records"] if changed else []
    )
    if changed:
        assert writes[0]["record_id"] == "rec-4"
        assert writes[0]["fields"]["到货件数"] == 0
    assert all(item["fields"]["问题件后应签时间"] == "retained legacy value" for item in items)


@pytest.mark.parametrize("observed, matches", [
    ({"quantity": 0, "extra": "preserved"}, True),
    ({"quantity": 0, "note": None}, True),
    ({"quantity": 0, "note": ""}, True),
    ({"quantity": 0, "note": "changed"}, False),
    ({"quantity": None}, False),
    ({"quantity": "0"}, False),
    ({}, False),
])
def test_delta_and_readback_share_exact_managed_fields(observed, matches):
    assert readback.bitable_fields_match({"quantity": 0, "note": ""}, observed) is matches
