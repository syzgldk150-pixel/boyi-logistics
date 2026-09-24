"""Independent arithmetic and coverage checks for the typed shipment contract."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from agent.shipment_queries import ShipmentQueryService
from shared.shipment_metrics import (
    BUSINESS_ZONE, ShipmentDay, ShipmentQueryError, ShipmentRecord, aggregate,
    format_shipment_result, resolve_query, weight_kg,
)


NOW = datetime(2026, 9, 24, 16, tzinfo=BUSINESS_ZONE)


def arguments(**changes):
    return {"station": "测试站", "period": "today", "start_date": None, "end_date": None,
            "group_by": "total", "comparison": "none", **changes}


def snapshot(day=date(2026, 9, 24), **changes):
    rows = tuple(ShipmentRecord(f"{day.isoformat()}-{index}", "site-a", datetime.combine(day, NOW.time(), BUSINESS_ZONE) - timedelta(hours=1),
                 Decimal(weight), destination, cancelled, receipt, child)
                 for index, (weight, destination, cancelled, receipt, child) in enumerate([
                     ("1200.125", "甲地", False, False, False), ("800.875", "乙地", False, False, False),
                     ("90", "甲地", True, False, False), ("20", "乙地", False, True, False),
                     ("11", "甲地", False, False, True)]))
    return ShipmentDay("synthetic-ronghui", "site-a", "测试站", day, NOW, True, len(rows), rows, "synthetic-v1", **changes)


def test_program_totals_groups_exclusions_and_reverse():
    service = ShipmentQueryService(read_day=lambda station, day: snapshot(day), clock=lambda: NOW)
    result = service(arguments(group_by="destination"))
    current = result["current"]
    assert current["tonnes"] == "2.001"
    assert current["tickets"] == 2
    assert current["excluded"] == {"cancelled": 1, "receipt": 1, "child": 1, "outside_interval": 0}
    assert sum(Decimal(row["tonnes"]) for row in current["groups"]) == Decimal("2.001")
    assert Decimal(current["tonnes"]) * 1000 == Decimal("1200.125") + Decimal("800.875")
    assert current["validation"]["min_kg"] == "800.875"
    assert "2.001 吨" in format_shipment_result(result)


@pytest.mark.parametrize("value", [None, "", True, "NaN", "Infinity", "-1", "x"])
def test_invalid_weights_never_become_zero(value):
    with pytest.raises(ShipmentQueryError):
        weight_kg(value, "kg")


def test_unknown_unit_is_not_assumed_kg():
    with pytest.raises(ShipmentQueryError, match="单位"):
        weight_kg("1000", "unknown")


@pytest.mark.parametrize("change,code", [
    ({"complete": False}, "SHIPMENT_COVERAGE_INCOMPLETE"),
    ({"source_total": 100}, "SHIPMENT_COVERAGE_INCOMPLETE"),
    ({"source_revision": ""}, "SHIPMENT_COVERAGE_INCOMPLETE"),
])
def test_incomplete_pages_fail_before_total(change, code):
    day = replace(snapshot(), **change)
    with pytest.raises(ShipmentQueryError) as error:
        aggregate([day], start=NOW.replace(hour=0), end=NOW, group_by="total")
    assert error.value.code == code


def test_duplicates_and_wrong_station_fail():
    day = snapshot()
    for rows in (day.records + (day.records[0],), (replace(day.records[0], station_id="site-b"), *day.records[1:])):
        bad = replace(day, records=rows, source_total=len(rows))
        with pytest.raises(ShipmentQueryError):
            aggregate([bad], start=NOW.replace(hour=0), end=NOW, group_by="total")


def test_half_open_interval_excludes_cutoff_and_zero_is_known_complete():
    day = snapshot()
    rows = tuple(replace(row, opened_at=NOW) for row in day.records)
    current = aggregate([replace(day, records=rows)], start=NOW.replace(hour=0), end=NOW, group_by="date")
    assert current["status"] == "zero"
    assert current["tonnes"] == "0"
    assert current["groups"] == [{"label": "2026-09-24", "tickets": 0, "tonnes": "0"}]


def test_followups_only_use_structured_context():
    first = resolve_query(arguments(), now=NOW)
    next_query = resolve_query(arguments(station=None, period="yesterday", group_by="destination"), now=NOW, previous=first)
    assert next_query["station"] == "测试站"
    assert next_query["start_date"] == "2026-09-23"
    with pytest.raises(ShipmentQueryError):
        resolve_query(arguments(station=None, period="inherit"), now=NOW)


def test_current_business_day_uses_shanghai_not_utc():
    query = resolve_query(arguments(), now=NOW.replace(hour=0, minute=1).astimezone(__import__("datetime").timezone.utc))
    assert query["start_date"] == "2026-09-24"


def test_comparisons_full_day_same_time_and_zero_denominator():
    def loader(station, day):
        result = snapshot(day)
        if day < NOW.date():
            rows = (replace(result.records[0], chargeable_kg=Decimal("0")),)
            return replace(result, records=rows, source_total=1)
        return result
    service = ShipmentQueryService(read_day=loader, clock=lambda: NOW)
    for mode in ("previous_day_full", "previous_day_same_time"):
        result = service(arguments(comparison=mode))
        assert result["comparison"]["change_percent"] is None
        assert result["comparison"]["difference_tonnes"] == result["current"]["tonnes"]
        assert result["previous"]["end_exclusive"] != result["current"]["end_exclusive"]


def test_stale_and_missing_source_do_not_report_zero():
    service = ShipmentQueryService(read_day=lambda station, day: replace(snapshot(day), captured_at=NOW-timedelta(minutes=6)), clock=lambda: NOW)
    result = service(arguments())
    assert result["ok"] is False
    assert "current" not in result
    assert result["code"] == "SHIPMENT_SOURCE_STALE"


@pytest.mark.parametrize("changes", [{"account_id": "other"}, {"station": None}, {"period": "range", "start_date": "2026-01-01", "end_date": "2026-09-24"}])
def test_closed_arguments_and_missing_context(changes):
    with pytest.raises(ShipmentQueryError):
        resolve_query(arguments(**changes), now=NOW)
