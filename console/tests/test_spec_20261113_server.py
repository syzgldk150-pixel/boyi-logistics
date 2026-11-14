"""Server side of the 2026-11-13 spec: waybill export and the quick-plugin preference."""

import json
from decimal import Decimal

import pytest

from console.navigation import (
    AutomationQuickPluginsValidationError,
    automation_quick_plugins_for_user,
    serialize_automation_quick_plugins,
)
from console.services.waybill_export import WAYBILL_EXPORT_HEADER, parse_waybill_export_keys, waybill_export_rows
from shared.identity_routes import console_permission


def test_export_keys_are_exact_distinct_and_bounded():
    assert parse_waybill_export_keys("yunda:41,manual:7,yunda:41") == [("yunda", 41), ("manual", 7)]
    for raw in ("", "best:1", "yunda:0", "yunda:41,", "yunda:x", "yunda:41;manual:7"):
        assert parse_waybill_export_keys(raw) is None
    assert parse_waybill_export_keys(",".join(f"ocr:{i}" for i in range(1, 502))) is None


def test_export_rows_follow_the_list_fee_rule():
    rows = waybill_export_rows([
        {"waybill_no": "900000001", "source": "yunda", "source_label": "韵达寄件", "opening_cost": "12.30", "status_label": "已签收"},
        {"waybill_no": "BY00001", "source": "manual", "source_label": "博益开单", "opening_cost": "0.00", "freight_fee": None},
    ])
    assert rows[0] == WAYBILL_EXPORT_HEADER
    fee = WAYBILL_EXPORT_HEADER.index("费用（元）")
    assert rows[1][fee] == Decimal("12.30") and rows[1][WAYBILL_EXPORT_HEADER.index("状态")] == "已签收"
    assert rows[2][fee] == "无数据"


def test_quick_plugins_keep_order_and_other_preferences():
    stored = json.dumps({"mobile_bottom_nav": ["/", "/waybills", "/tracking"]})
    saved = serialize_automation_quick_plugins(stored, ["b_demo", "a_demo"])
    assert json.loads(saved)["mobile_bottom_nav"] == ["/", "/waybills", "/tracking"]
    assert automation_quick_plugins_for_user({"ui_preferences_json": saved}) == ("b_demo", "a_demo")
    assert automation_quick_plugins_for_user({"ui_preferences_json": "{bad"}) == ()


@pytest.mark.parametrize("ids", [["a"] * 2, [f"p{i}" for i in range(9)], ["bad id"], "a_demo", [1]])
def test_quick_plugins_reject_invalid_lists(ids):
    with pytest.raises(AutomationQuickPluginsValidationError):
        serialize_automation_quick_plugins("{}", ids)


def test_new_routes_keep_their_permissions():
    assert console_permission("POST", "/settings/profile/automation-quick-plugins") == "plugins.execute:view"
    assert console_permission("GET", "/waybills/export.xlsx") == "business.query:view"
