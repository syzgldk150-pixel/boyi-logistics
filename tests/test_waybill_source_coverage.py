from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from agent.send_waybills_business import DirectWaybillQueryService, WaybillQuerySource
from shared.waybill_pagination import collect_complete_pages
from shared.waybill_source_coverage import WaybillSourceScope, coverage_satisfies


def test_pagination_requires_complete_stable_identity_set():
    pages = [{"rows": [{"id": "a"}, {"id": "b"}], "total": 3},
             {"rows": [{"id": "c"}], "total": 3}]
    values, total = collect_complete_pages(lambda page: pages[page], rows_from=lambda p: p["rows"],
        total_from=lambda p: p.get("total"), identity="id", first_page=0, page_size=2, max_pages=2)
    assert total == 3 and [row["id"] for row in values] == ["a", "b", "c"]


@pytest.mark.parametrize("pages,reason", [
    ([{"rows": [{"id": "a"}], "total": 2}], "INCOMPLETE"),
    ([{"rows": [{"id": "a"}], "total": None}], "TOTAL_MISSING"),
    ([{"rows": [{"id": "a"}, {"id": "a"}], "total": 2}], "DUPLICATED"),
    ([{"rows": [{"id": "a"}, {"id": "b"}], "total": 3}, {"rows": [{"id": "c"}], "total": 4}], "CHANGED"),
    ([{"rows": [{"id": "a"}, {"id": "b"}], "total": 3}], "LIMIT_REACHED"),
])
def test_pagination_failure_is_not_a_partial_success(pages, reason):
    with pytest.raises(ValueError, match=reason):
        collect_complete_pages(lambda page: pages[page], rows_from=lambda p: p["rows"],
            total_from=lambda p: p.get("total"), identity="id", first_page=0, page_size=2, max_pages=len(pages))


def test_today_and_permission_or_account_changes_require_new_proof():
    scope = WaybillSourceScope("ronghui", "fixture-site/send", "fixture-site/all", "account-a")
    yesterday = date(2026, 9, 8)
    row = {**scope.__dict__, "business_date": yesterday, "final_day": True,
           "complete_through": datetime(2026, 9, 8, 15, 59, 59)}
    assert coverage_satisfies(row, scope, yesterday, requested_at=datetime(2026, 9, 9, tzinfo=timezone.utc))
    assert not coverage_satisfies(row, scope, yesterday, requested_at=datetime(2026, 9, 8, 15, 55, tzinfo=timezone.utc))
    rebound = WaybillSourceScope("ronghui", scope.source_scope, scope.permission_scope, "account-b")
    assert not coverage_satisfies(row, rebound, yesterday, requested_at=datetime(2026, 9, 9, tzinfo=timezone.utc))


def test_unknown_scope_is_a_visible_partial_result_and_performs_no_writes():
    class Repository:
        def read_cached(self, **_kwargs):
            return {"rows": [], "total": 0}
        def publish(self, *_args, **_kwargs):
            pytest.fail("unknown scope must never publish")
    def unavailable(_provider):
        raise ValueError("WAYBILL_SOURCE_SCOPE_UNVERIFIED")
    result = DirectWaybillQueryService(Repository(), unavailable).query({"source": "all", "date_from": "2026-09-08"})
    assert result["ok"] is False and result["data_status"] == "partial"
    assert result["error_code"] == "WAYBILL_SOURCE_SCOPE_UNVERIFIED"
