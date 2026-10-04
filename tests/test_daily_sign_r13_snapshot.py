"""R13 original-page contract verified with DrissionPage on 2026-10-04."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.tms_runtime.scripts import get_qianshou
from service_v2_plugins.sync_daily_should_sign_v2.payload.business import daily_sign_pipeline as pipeline
from service_v2_plugins.sync_daily_should_sign_v2.payload.business import daily_sign_sync_tool as sync


def source_row(code="R00021000001"):
    return {
        "waybillNo": code, "displayPlanSignTime": "2026-10-04 23:59:59",
        "planSignTime": "2026-10-03 23:59:59", "isSigns": 0,
        "problemType": "测试类型", "problemRegisterDate": "2026-10-03 12:00:00",
        "problemCause": "测试内容", "problemRegisterSite": "测试网点",
        "goodsName": "测试货物", "packTypeDesc": "纸箱", "pcs": 0,
        "dispAddress": "测试地址", "dispatchMode": "自提",
    }


def fetch(pages):
    session = Mock()
    session.post.side_effect = [SimpleNamespace(raise_for_status=lambda: None, json=lambda d=d: d) for d in pages]
    auth = SimpleNamespace(last_token="test-only", login_and_get_session=lambda **_: session)
    with patch.object(get_qianshou, "R13SSOAuth", return_value=auth), patch.object(
        get_qianshou, "_request_account_context",
        return_value={"code": 200, "data": {"siteCode": "test-site", "siteTypeCode": 101}},
    ):
        rows = get_qianshou.fetch_qianshou(
            config_path=None, username=None, password=None, account_id="selected-pool-account",
            start="2026-09-05 00:00:00", end="2026-10-04 23:59:59", days=30,
            page_size=1, page=1,
        )
    return rows, session


def page(row, total=1):
    return {"code": 200, "data": {"data": [row], "total": total}}


def test_all_pages_keep_original_fields_and_exact_unsigned_filter():
    rows, session = fetch([page(source_row(), 2), page(source_row("R00021000002"), 2)])
    assert len(rows) == 2
    for number, call in enumerate(session.post.call_args_list, 1):
        body = call.kwargs["json"]
        assert body["isSigns"] == "0" and body["currentPage"] == number
        assert body["planSignTime_CondStart"] == "2026-09-05 00:00:00"
        assert body["planSignTime_CondEnd"] == "2026-10-04 23:59:59"
        assert body["dispSiteCode_CondList"] == ["test-site"]
    assert rows[0]["planSignTime"] == "2026-10-04 23:59:59"
    rendered = sync._apply_r13_publication_fields({"tracking_number": rows[0]["billNumberMain"],
        "goods_name": "stale", "arrived_quantity": 0, "calculation_trace": {}}, rows[0])
    expected = ["R00021000001", "2026-10-04 23:59:59", "测试类型", "2026-10-03 12:00:00",
        "测试内容", "测试网点", "测试货物", "纸箱", 0, "测试地址", "自提", 0]
    assert sync._build_ledger_sheet_values([rendered]) == [expected]
    fields = sync._build_ledger_records([rendered])[0]["fields"]
    assert list(fields) == sync.SHEET_HEADERS
    assert list(fields.values()) == expected


@pytest.mark.parametrize("change,reason", [({"isSigns": 1}, "unsigned scope"),
    ({"displayPlanSignTime": None}, "displayPlanSignTime")])
def test_rejects_out_of_scope_or_missing_display_time(change, reason):
    with pytest.raises(RuntimeError, match=reason):
        fetch([page({**source_row(), **change})])


def test_missing_source_field_is_not_replaced_by_history():
    row = source_row()
    del row["problemCause"]
    with pytest.raises(RuntimeError, match="problemCause"):
        fetch([page(row)])


@pytest.mark.parametrize("today,start,end", [
    (datetime(2026, 10, 4, 12), "2026-09-05 00:00:00", "2026-10-04 23:59:59"),
    (datetime(2026, 3, 1, 12), "2026-01-31 00:00:00", "2026-03-01 23:59:59"),
])
def test_fixed_thirty_business_days_override_old_saved_range(today, start, end):
    with (patch.object(sync, "build_daily_sign_request_body", return_value={"days": 365,
        "start": "2000-01-01", "end": "2030-01-01", "page": 4, "fetch_all": False}), patch.object(
        pipeline, "business_now", return_value=today),
    ):
        request = pipeline._resolve_r13_request({}, "selected-pool-account")
    assert request["start"] == start
    assert request["end"] == end
    assert request["days"] == 30 and request["page"] == 1 and request["fetch_all"] is True


def test_original_empty_problem_fields_do_not_reuse_prior_events():
    source = {**source_row(), "billNumberMain": "R1", "problemType": None, "problemRegisterDate": None,
        "problemCause": None, "problemRegisterSite": None}
    result = sync._apply_r13_publication_fields({"calculation_trace": {"r13_publication": {
        "problemType": "stale"}}}, source)
    assert list(sync._r13_problem_cells(result).values()) == ["", "", "", ""]


def test_snapshot_marker_preserves_tms_conflict_and_r13_problem_evidence():
    from tools.daily_sign_store import build_daily_sign_persistence_marker
    source = {**source_row(), "billNumberMain": "R1"}
    row = sync.build_ledger_row("R1", r13_row=source, previous_row=None,
        arrival_history=[], problem_events=[], sign_event=None,
        observed_at=datetime(2026, 10, 4, 12))
    row = sync._apply_r13_publication_fields(row, source)
    row["tms_signed"] = True
    values = dict(problem_events=[], sign_events=[], sign_verification_states=[],
        ledger_rows=[row], publication_rows=[row])
    before = build_daily_sign_persistence_marker(**values)
    row["calculation_trace"]["r13_publication"]["problemCause"] = "changed"
    after = build_daily_sign_persistence_marker(**values)
    assert before["publication_rows"]["count"] == 1
    assert before["publication_rows"]["sha256"] != after["publication_rows"]["sha256"]


def test_arrival_count_uses_latest_per_waybill_statistics_without_summing_days():
    history = [
        {"business_date": "2026-10-03", "arrived_quantity": 5, "run_id": "yesterday"},
        {"business_date": "2026-10-04", "arrived_quantity": 8, "run_id": "today"},
    ]
    observed_at = datetime(2026, 10, 4, 12)
    row = {"arrived_quantity": 99}
    result = sync._apply_latest_stat_quantity(row, history, observed_at)
    assert result["arrived_quantity"] == 8
    assert result["calculation_trace"]["arrival_quantity_source"]["used_prior_day"] is False
    result = sync._apply_latest_stat_quantity(row, history[:1], observed_at)
    assert result["arrived_quantity"] == 5
    assert result["calculation_trace"]["arrival_quantity_source"] == {
        "source": "latest_successful_statistics", "business_date": "2026-10-03",
        "run_id": "yesterday", "used_prior_day": True,
    }
    # A newer explicit correction wins even when it is zero or no data.
    history[-1]["arrived_quantity"] = 0
    assert sync._apply_latest_stat_quantity(row, history, observed_at)["arrived_quantity"] == 0
    history[-1]["arrived_quantity"] = None
    assert sync._apply_latest_stat_quantity(row, history, observed_at)["arrived_quantity"] is None
    assert sync._apply_latest_stat_quantity(row, [], observed_at)["arrived_quantity"] is None
    assert sync._build_ledger_sheet_values([{"arrived_quantity": None}])[0][-1] == "无数据"
    assert sync._build_ledger_records([{"arrived_quantity": None}])[0]["fields"]["到货件数"] is None


def test_arrival_count_rejects_ambiguous_or_invalid_statistics():
    row = {"business_date": "2026-10-04", "arrived_quantity": 2}
    with pytest.raises(ValueError, match="多条"):
        sync._apply_latest_stat_quantity({}, [row, row], datetime(2026, 10, 4))
    for value in (-1, "2.5", True):
        with pytest.raises(ValueError, match="非负整数"):
            sync._apply_latest_stat_quantity({}, [{**row, "arrived_quantity": value}], datetime(2026, 10, 4))
    with pytest.raises(ValueError, match="业务日期"):
        sync._apply_latest_stat_quantity({}, [{"arrived_quantity": 5}], datetime(2026, 10, 4))


def test_latest_statistics_selection_ignores_future_and_input_order():
    history = [
        {"business_date": "2026-10-05", "arrived_quantity": 10},
        {"business_date": "2026-10-04", "arrived_quantity": 8},
        {"business_date": "2026-10-03", "arrived_quantity": 5},
    ]
    assert sync._apply_latest_stat_quantity({}, history, datetime(2026, 10, 4))["arrived_quantity"] == 8
