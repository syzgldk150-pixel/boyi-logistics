"""Linking a Boyi waybill to a platform waybill and tracking through the link."""
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from console.database import DocumentRepository, ManualWaybillEditError, normalize_waybill_link, waybill_link_text
from console.routes import waybills
from console.tests.test_manual_waybill_edit import JsonHandler, build_app


def link_body(**overrides):
    body = {"request_id": str(uuid4()), "platform": "ronghui", "waybill_no": " r12345678901 ",
            "expected_platform": "", "expected_waybill_no": ""}
    body.update(overrides)
    return body


def test_link_normalizes_number_and_records_authenticated_actor():
    app = build_app()
    app.repository.link_manual_waybill.return_value = {"outcome": "SAVED", "changes": []}
    body = link_body()
    handler = JsonHandler(body)
    assert waybills.handle_post(app, handler, "/waybills/7/platform-link", "", {})
    assert handler.status == 200
    assert handler.result()["message"] == "已绑定融辉 R12345678901，轨迹将查询该平台运单。"
    args, kwargs = app.repository.link_manual_waybill.call_args
    assert args == (7, "ronghui", "R12345678901")
    assert kwargs["expected_platform"] == "" and kwargs["expected_waybill_no"] == ""
    assert kwargs["request_id"] == body["request_id"]
    assert kwargs["actor_id"] == "7" and kwargs["actor_name"] == "测试管理员"


def test_clearing_a_link_sends_empty_values():
    app = build_app()
    app.repository.link_manual_waybill.return_value = {"outcome": "SAVED", "changes": []}
    handler = JsonHandler(link_body(platform="", waybill_no="", expected_platform="best", expected_waybill_no="BEST0001"))
    app._handle_manual_waybill_link(handler, 7)
    assert handler.status == 200 and handler.result()["message"] == "已解除平台运单绑定。"
    args, kwargs = app.repository.link_manual_waybill.call_args
    assert args == (7, "", "")
    assert (kwargs["expected_platform"], kwargs["expected_waybill_no"]) == ("best", "BEST0001")


@pytest.mark.parametrize("changes,status", [
    ({"platform": "sf"}, 422), ({"waybill_no": "R12-34"}, 422), ({"waybill_no": ""}, 422),
    ({"extra": "x"}, 400), ({"waybill_no": 12345}, 400),
])
def test_invalid_link_requests_are_rejected_before_saving(changes, status):
    app = build_app()
    handler = JsonHandler(link_body(**changes))
    app._handle_manual_waybill_link(handler, 7)
    assert handler.status == status
    app.repository.link_manual_waybill.assert_not_called()


def test_link_requires_matching_browser_request_uuid():
    app = build_app()
    handler = JsonHandler(link_body())
    handler.headers["X-Browser-Request-UUID"] = str(uuid4())
    app._handle_manual_waybill_link(handler, 7)
    assert handler.status == 400
    app.repository.link_manual_waybill.assert_not_called()


@pytest.mark.parametrize("code,status", [("STALE", 409), ("CANCELLED", 409), ("NOT_FOUND", 404)])
def test_link_conflicts_are_reported(code, status):
    app = build_app()
    app.repository.link_manual_waybill.side_effect = ManualWaybillEditError(code, "测试冲突")
    handler = JsonHandler(link_body())
    app._handle_manual_waybill_link(handler, 7)
    assert handler.status == status and handler.result()["error_code"] == code


def test_normalize_and_display_link():
    assert normalize_waybill_link(" Yunda ", "4321 0987 65") == ("yunda", "4321098765")
    assert normalize_waybill_link("", " ") == ("", "")
    assert waybill_link_text("best", "BEST0001") == "百世 BEST0001"
    assert waybill_link_text("", "") == ""


def test_linked_row_tracks_the_platform_number():
    repository = DocumentRepository.__new__(DocumentRepository)
    row = repository._row_to_waybill({"id": 7, "source": "manual", "waybill_no": "BY00007", "status": "in_transit",
                                      "linked_platform": "yunda", "linked_waybill_no": "4321098765"})
    assert row["linked_text"] == "韵达 4321098765"
    assert parse_qs(urlparse(row["tracking_url"]).query) == {"tracking_number": ["4321098765"], "provider": ["yunda"]}
    plain = repository._row_to_waybill({"id": 8, "source": "manual", "waybill_no": "BY00008", "status": "in_transit"})
    assert plain["linked_text"] == "" and plain["tracking_url"] == "/tracking?tracking_number=BY00008"


def test_tracking_query_passes_linked_provider_and_stops_for_best():
    app = build_app()
    with patch("console.services.waybills_receipts.call_business",
               return_value={"ok": True, "data": {"ok": True}}) as call:
        handler = JsonHandler({"tracking_number": "4321098765", "provider": "yunda"})
        app._handle_tracking_query(handler)
        assert call.call_args.args[2] == {"tracking_number": "4321098765", "decrypt_masked": True, "provider": "yunda"}

        best = JsonHandler({"tracking_number": "BEST0001", "provider": "best"})
        app._handle_tracking_query(best)
        assert best.status == 422 and "百世" in best.result()["error"]
        assert call.call_count == 1

        plain = JsonHandler({"tracking_number": "R12345678901", "provider": "unknown"})
        app._handle_tracking_query(plain)
        assert "provider" not in call.call_args.args[2]


@pytest.mark.parametrize(("data", "note"), [
    ({"linked_platform": "ronghui", "linked_match_count": 1, "linked_platform_status": "signed"}, ""),
    ({"linked_platform": "ronghui", "linked_match_count": 0}, "融辉单未入库"),
    ({"linked_platform": "yunda", "linked_match_count": 2, "linked_platform_status": "signed"}, "韵达单号重复"),
    ({"linked_platform": "yunda", "linked_match_count": 1, "linked_platform_status": "cancelled"}, "韵达单已作废"),
    ({"linked_platform": "best"}, "百世状态未接入"),
    ({"linked_platform": "ronghui", "linked_match_count": 0, "status": "cancelled"}, ""),
    ({"linked_platform": "ronghui"}, ""),
    ({}, ""),
])
def test_linked_row_explains_a_missing_platform_route_status(data, note):
    repository = DocumentRepository.__new__(DocumentRepository)
    row = {"id": 9, "source": "manual", "waybill_no": "BY00009", "status": "in_transit", **data}
    if data.get("linked_platform"):
        row["linked_waybill_no"] = "R0001"
    assert repository._row_to_waybill(row)["linked_status_note"] == note


def test_union_query_takes_route_status_only_from_one_uncancelled_platform_row():
    sql = DocumentRepository._waybill_query_table("all")
    assert "FROM boyi_waybills b WHERE source='manual'" in sql
    assert "WHEN 'ronghui' THEN (SELECT COUNT(*) FROM ronghui_waybills p" in sql
    assert "WHEN 'yunda' THEN (SELECT MAX(p.scan_status) FROM yunda_waybills p" in sql
    assert "best" not in sql


def test_manual_source_query_keeps_the_linked_route_columns():
    sql = DocumentRepository._waybill_query_table("manual")
    assert sql.startswith("(SELECT ") and sql.endswith(") AS saved_waybills")
    assert "FROM waybills" not in sql and "FROM boyi_waybills b WHERE source='manual'" in sql
    assert "AS linked_match_count" in sql
    assert DocumentRepository._waybill_query_table("ocr") == "boyi_waybills"
