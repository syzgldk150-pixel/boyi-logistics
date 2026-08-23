from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from agent.automation_plugins.errors import PluginExecutionError
from plugin_core_adapters import arrival
from tools.daily_sign_store import snapshot_fingerprint


_FIELDS = arrival._ARRIVE_FIELDS


def _record(tracking_number: str = "A-100") -> dict[str, Any]:
    row: dict[str, Any] = {field: "" for field in _FIELDS}
    row.update(
        {
            "tracking_number": tracking_number,
            "quantity": 2,
            "actual_weight": "3.50",
            "volume": "0.125",
            "settlement_weight": "3.50",
            "volumetric_weight": "2.50",
            "shipping_fee": "12.34",
            "pay_on_arrival": "0.00",
        }
    )
    return row


def _stats_record(tracking_number: str = "A-100") -> dict[str, Any]:
    return {**_record(tracking_number), "arrived_quantity": 1}


def _arrival_record(tracking_number: str = "A-100") -> dict[str, Any]:
    stats = _stats_record(tracking_number)
    return {
        "tracking_number": tracking_number,
        "destination_station": stats["destination_station"],
        "expected_quantity": stats["quantity"],
        "arrived_quantity": stats["arrived_quantity"],
        "goods_name": stats["goods_name"],
        "package_type": stats["package_type"],
        "delivery_method": stats["delivery_method"],
        "recipient_address": stats["recipient_address"],
    }


def test_waybill_projection_accepts_lost_response_only_after_exact_fresh_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored: list[dict[str, Any]] = []

    def write(records: list[dict[str, Any]]) -> dict[str, Any]:
        stored[:] = deepcopy(records)
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(arrival, "_write_waybills", write)
    monkeypatch.setattr(arrival, "_read_waybills", lambda: deepcopy(stored))

    result = arrival._replace_waybill_snapshot([_record()], "2026-08-15")

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["record_count"] == 1
    assert len(result["readback_sha256"]) == 64


def test_waybill_projection_treats_mysql_decimals_as_the_same_signed_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired = _record()
    observed = deepcopy(desired)
    for field in (
        "actual_weight",
        "volume",
        "settlement_weight",
        "volumetric_weight",
        "shipping_fee",
        "pay_on_arrival",
    ):
        observed[field] = Decimal(str(observed[field]))

    monkeypatch.setattr(arrival, "_write_waybills", lambda _records: {"ok": True})
    monkeypatch.setattr(arrival, "_read_waybills", lambda: [observed])

    result = arrival._replace_waybill_snapshot([desired], "2026-08-15")

    assert result["verified"] is True
    assert result["record_count"] == 1


def test_waybill_projection_rejects_zero_or_incomplete_fresh_readback_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(arrival, "_write_waybills", lambda _records: {"ok": True})
    monkeypatch.setattr(arrival, "_read_waybills", lambda: [])

    with pytest.raises(PluginExecutionError) as exc:
        arrival._replace_waybill_snapshot([_record()], "2026-08-15")

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_forecast_projection_binds_one_new_run_identity_items_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_record()]
    runs: list[dict[str, Any]] = [
        {
            "run_id": "prior-run",
            "business_date": "2026-08-15",
            "status": "success",
            "row_count": 1,
            "fingerprint": snapshot_fingerprint([_record("A-OLD")]),
            "items": [_record("A-OLD")],
        }
    ]

    def write(business_date: date, records: list[dict[str, Any]]) -> dict[str, Any]:
        runs.append(
            {
                "run_id": "fresh-run",
                "business_date": business_date.isoformat(),
                "status": "success",
                "row_count": len(records),
                "fingerprint": snapshot_fingerprint(records),
                "items": deepcopy(records),
            }
        )
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(arrival, "_write_forecast", write)
    monkeypatch.setattr(arrival, "_read_forecast_runs", lambda _target: deepcopy(runs))

    result = arrival._replace_arrival_forecast_snapshot(rows, "2026-08-15")

    assert result["verified"] is True
    assert result["record_count"] == 1
    assert len(result["run_id_sha256"]) == 64


def test_forecast_projection_rejects_no_new_run_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(arrival, "_write_forecast", lambda _date, _rows: {"ok": True})
    monkeypatch.setattr(arrival, "_read_forecast_runs", lambda _target: deepcopy(runs))

    with pytest.raises(PluginExecutionError) as exc:
        arrival._replace_arrival_forecast_snapshot([_record()], "2026-08-15")

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_arrival_projection_accepts_only_one_new_active_run_with_exact_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_arrival_record()]
    runs = [
        {
            "run_id": "prior-active-run",
            "business_date": "2026-08-15",
            "status": "success",
            "row_count": 1,
            "fingerprint": snapshot_fingerprint([_arrival_record("A-OLD")]),
            "items": [_arrival_record("A-OLD")],
        }
    ]

    def write(business_date: date, desired: list[dict[str, Any]]) -> dict[str, Any]:
        fresh = {
            "run_id": "fresh-active-run",
            "business_date": business_date.isoformat(),
            "status": "success",
            "row_count": len(desired),
            "fingerprint": snapshot_fingerprint(desired),
            "items": deepcopy(desired),
        }
        runs[:] = [fresh]
        raise TimeoutError("response lost after active snapshot commit")

    monkeypatch.setattr(arrival, "_write_arrival", write)
    monkeypatch.setattr(arrival, "_read_arrival_runs", lambda _target: deepcopy(runs))

    result = arrival._replace_arrival_snapshot(records, "2026-08-15")

    assert result["verified"] is True
    assert result["record_count"] == 1


def test_split_pending_projection_reads_back_exact_classified_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_stats_record()]
    stored: list[dict[str, Any]] = []

    def write(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        stored[:] = deepcopy(candidates)
        raise TimeoutError("response lost after split projection commit")

    monkeypatch.setattr(arrival, "_write_split_projection", write)
    monkeypatch.setattr(arrival, "_read_split_projection", lambda: deepcopy(stored))

    result = arrival._refresh_split_pending_snapshot(records, "2026-08-15")

    assert result["verified"] is True
    assert result["record_count"] == 1
    assert result["candidate_count"] == 1


def test_scan_cleanup_requires_fresh_absence_of_stale_rows_and_preserves_fresh_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"raw_code": "STALE", "stale": True},
        {"raw_code": "FRESH", "stale": False},
    ]

    def cleanup(_retention_days: int) -> dict[str, Any]:
        rows[:] = [row for row in rows if not row["stale"]]
        raise TimeoutError("response lost after cleanup")

    monkeypatch.setattr(arrival, "_cleanup_scans", cleanup)
    monkeypatch.setattr(arrival, "_observe_cleanup", lambda _days: deepcopy(rows))

    result = arrival._cleanup_scan_snapshot(30)

    assert result == {
        "ok": True,
        "verified": True,
        "deleted": 1,
        "skipped": False,
    }


def test_arrive_sheet_uses_exact_resource_and_accepts_only_exact_dated_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-arrive-primary"
    resource = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "managed-token",
        "range": "Arrive!A2:R100",
        "clear_range": "Arrive!A2:R100",
        "title_range": "Arrive!A1:R1",
        "_meta": {"resource_key": resource_id},
    }
    rows = [[_record().get(field) for field in _FIELDS]]
    from tools.arrive_list_sync_tool import _build_title

    monkeypatch.setattr(arrival, "_load_resource", lambda exact: resource if exact == resource_id else None)
    monkeypatch.setattr(arrival, "_write_sheet_call", lambda _action, _params: False)

    def fresh(_resource, value_range, *, width):
        assert _resource == resource
        assert width == 18
        if value_range == resource["clear_range"]:
            return arrival._canonical_rows(rows, width=width)
        if value_range == resource["title_range"]:
            return arrival._canonical_rows(
                [_build_title({"target_date": "2026-08-15"})],
                width=width,
            )
        raise AssertionError(value_range)

    monkeypatch.setattr(arrival, "_fresh_sheet_rows", fresh)

    result = arrival._replace_arrive_sheet(resource_id, rows, "2026-08-15")

    assert result["verified"] is True
    assert result["target_date"] == "2026-08-15"


def test_empty_arrive_sheet_reconciles_clear_response_loss_and_updates_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-arrive-primary"
    resource = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "managed-token",
        "range": "Arrive!A2:R100",
        "clear_range": "Arrive!A2:R100",
        "title_range": "Arrive!A1:R1",
        "_meta": {"resource_key": resource_id},
    }
    writes: list[tuple[str, list[list[Any]]]] = []
    observed: dict[str, list[list[Any]]] = {
        resource["clear_range"]: [],
        resource["title_range"]: [["旧日期运单编号"]],
    }

    monkeypatch.setattr(
        arrival,
        "_load_resource",
        lambda exact: resource if exact == resource_id else None,
    )

    def write(_action: str, params: dict[str, Any]) -> bool:
        value_range = str(params["range"])
        values = deepcopy(params["values"])
        writes.append((value_range, values))
        if value_range == resource["clear_range"]:
            observed[value_range] = []
            return False
        observed[value_range] = values
        return True

    def fresh(_resource: dict[str, Any], value_range: str, *, width: int):
        assert _resource == resource
        return arrival._canonical_rows(observed[value_range], width=width)

    monkeypatch.setattr(arrival, "_write_sheet_call", write)
    monkeypatch.setattr(arrival, "_fresh_sheet_rows", fresh)

    result = arrival._replace_arrive_sheet(resource_id, [], "2026-08-24")

    assert result["verified"] is True
    assert result["record_count"] == 0
    assert result["target_date"] == "2026-08-24"
    assert [value_range for value_range, _values in writes] == [
        resource["clear_range"],
        resource["title_range"],
    ]
    assert writes[1][1][0][0] == "08.24运单编号"


def test_arrive_sheet_without_optional_title_range_verifies_only_managed_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-arrive-legacy"
    resource = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "managed-token",
        "range": "Arrive!A2:R100",
        "clear_range": "Arrive!A2:R100",
        "_meta": {"resource_key": resource_id},
    }
    rows = [[_record().get(field) for field in _FIELDS]]
    reads: list[str] = []
    monkeypatch.setattr(arrival, "_load_resource", lambda exact: resource if exact == resource_id else None)
    monkeypatch.setattr(arrival, "_write_sheet_call", lambda _action, _params: False)

    def fresh(_resource, value_range, *, width):
        reads.append(value_range)
        return arrival._canonical_rows(rows, width=width)

    monkeypatch.setattr(arrival, "_fresh_sheet_rows", fresh)

    result = arrival._replace_arrive_sheet(resource_id, rows, "2026-08-15")

    assert result["verified"] is True
    assert reads == [resource["clear_range"]]


def test_arrive_sheet_mismatch_after_write_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-arrive-primary"
    monkeypatch.setattr(
        arrival,
        "_load_resource",
        lambda _exact: {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "managed-token",
            "range": "Arrive!A2:R100",
            "clear_range": "Arrive!A2:R100",
            "title_range": "Arrive!A1:R1",
            "_meta": {"resource_key": resource_id},
        },
    )
    monkeypatch.setattr(arrival, "_write_sheet_call", lambda _action, _params: True)
    monkeypatch.setattr(arrival, "_fresh_sheet_rows", lambda *_args, **_kwargs: [])

    with pytest.raises(PluginExecutionError) as exc:
        arrival._replace_arrive_sheet(
            resource_id,
            [[_record().get(field) for field in _FIELDS]],
            "2026-08-15",
        )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_arrive_sheet_binding_failure_stays_prewrite_and_never_calls_feishu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[object] = []
    monkeypatch.setattr(arrival, "_load_resource", lambda _resource_id: None)
    monkeypatch.setattr(
        arrival,
        "_write_sheet_call",
        lambda action, params: writes.append((action, params)) or True,
    )

    with pytest.raises(PluginExecutionError) as exc:
        arrival._replace_arrive_sheet(
            "missing-resource",
            [[_record().get(field) for field in _FIELDS]],
            "2026-08-15",
        )

    assert exc.value.code == "BROKER_RESOURCE_UNAVAILABLE"
    assert writes == []


def test_arrival_stats_sheet_accepts_lost_response_only_after_exact_fresh_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-stats-primary"
    resource = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "managed-token",
        "snapshot_range": "Stats!A2:S100",
        "clear_range": "Stats!A2:S100",
        "title_range": "Stats!A1:S1",
        "_meta": {"resource_key": resource_id},
    }
    records = [_stats_record()]
    values = arrival._stats_values("stats", records, "2026-08-15")
    expected_data = arrival._canonical_rows(values[1:], width=19)
    expected_title = arrival._canonical_rows([values[0]], width=19)
    monkeypatch.setattr(arrival, "_load_resource", lambda _exact: resource)
    monkeypatch.setattr(arrival, "_write_sheet_call", lambda _action, _params: False)

    def fresh(_resource, value_range, *, width):
        assert _resource == resource
        assert width == 19
        if value_range == "Stats!A2:S100":
            return expected_data
        if value_range == "Stats!A1:S1":
            return expected_title
        raise AssertionError(value_range)

    monkeypatch.setattr(arrival, "_fresh_sheet_rows", fresh)

    result = arrival._replace_arrival_stats_sheet(
        resource_id,
        "stats",
        records,
        "2026-08-15",
    )

    assert result["verified"] is True
    assert result["record_count"] == 1


def test_split_pending_sheet_uses_exact_resource_and_rejects_mismatch_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-split-pending"
    monkeypatch.setattr(
        arrival,
        "_load_resource",
        lambda _exact: {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "managed-token",
            "sheet_id": "Split",
            "range": "Split!A1:S1",
            "clear_range": "Split!A2:S5000",
            "_meta": {"resource_key": resource_id},
        },
    )
    monkeypatch.setattr(arrival, "_write_sheet_call", lambda _action, _params: True)
    monkeypatch.setattr(arrival, "_fresh_sheet_rows", lambda *_args, **_kwargs: [])

    with pytest.raises(PluginExecutionError) as exc:
        arrival._replace_arrival_stats_sheet(
            resource_id,
            "split_pending",
            [_stats_record()],
            "2026-08-15",
        )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"


def test_arrival_archive_binds_target_date_sheet_and_exact_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-stats-archive"
    resource = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "managed-token",
        "default_write_range": "A1:S100",
        "_meta": {"resource_key": resource_id},
    }
    records = [_stats_record()]
    values = arrival._stats_values("stats", records, "2026-08-15")
    monkeypatch.setattr(arrival, "_load_resource", lambda _exact: resource)
    from tools import arrival_stats_sync_tool as archive_tool

    monkeypatch.setattr(
        archive_tool,
        "_find_archive_sheet",
        lambda _resource, title, **_kwargs: {
            "sheet_id": "Archive",
            "title": title,
            "row_count": len(values),
        },
    )
    monkeypatch.setattr(
        archive_tool,
        "_archive_clear_range",
        lambda *_args, **_kwargs: "Archive!A1:S100",
    )
    monkeypatch.setattr(
        archive_tool,
        "_resolve_archive_template_range",
        lambda *_args, **_kwargs: "Archive!A1:S100",
    )
    monkeypatch.setattr(arrival, "_write_sheet_call", lambda _action, _params: False)
    monkeypatch.setattr(
        arrival,
        "_fresh_sheet_rows",
        lambda _resource, value_range, *, width: (
            arrival._canonical_rows(values, width=width)
            if value_range == "Archive!A1:S100"
            else pytest.fail(value_range)
        ),
    )

    result = arrival._archive_arrival_stats_sheet(
        resource_id,
        records,
        "2026-08-15",
    )

    assert result["verified"] is True
    assert result["target_date"] == "2026-08-15"


def test_arrival_archive_rejects_add_ack_for_another_fresh_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = "resource-stats-archive"
    resource = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "managed-token",
        "default_write_range": "A1:S100",
        "_meta": {"resource_key": resource_id},
    }
    monkeypatch.setattr(arrival, "_load_resource", lambda _exact: resource)
    from tools import arrival_stats_sync_tool as archive_tool

    lookups = iter(
        [
            None,
            {
                "sheet_id": "fresh-target-sheet",
                "title": "2026-08-15",
                "row_count": 0,
            },
        ]
    )
    monkeypatch.setattr(
        archive_tool,
        "_find_archive_sheet",
        lambda *_args, **_kwargs: next(lookups),
    )
    monkeypatch.setattr(
        archive_tool,
        "_sheet_id_from_add_result",
        lambda _result: "acknowledged-other-sheet",
    )
    monkeypatch.setattr(arrival, "_invoke_feishu", lambda *_args, **_kwargs: {"ok": True})
    writes: list[object] = []
    monkeypatch.setattr(
        arrival,
        "_write_sheet_call",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )

    with pytest.raises(PluginExecutionError) as exc:
        arrival._archive_arrival_stats_sheet(
            resource_id,
            [_stats_record()],
            "2026-08-15",
        )

    assert exc.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert writes == []


def test_arrival_archive_lookup_rejects_duplicate_target_date_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import arrival_stats_sync_tool as archive_tool

    monkeypatch.setattr(
        archive_tool,
        "_spreadsheet_sheet_ref_map",
        lambda _token: {"2026-08-15": "first-id"},
    )
    monkeypatch.setattr(
        archive_tool,
        "_spreadsheet_sheet_title_count",
        lambda _token, _title: 2,
    )

    with pytest.raises(ValueError, match="ambiguous"):
        archive_tool._find_archive_sheet(
            {"spreadsheet_token": "managed-token"},
            "2026-08-15",
        )
