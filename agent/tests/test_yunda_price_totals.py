import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "agent" / "tms_runtime" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import yunda_price


class YundaPriceTotalTests(unittest.TestCase):
    def test_entry_total_adds_sms_to_entry_cost_total(self):
        total = yunda_price._entry_total_text(
            {
                "info": "1",
                "data": {"CostTotal": "87.64", "InsuredCost": "4.00"},
                "showCost": {"classifyCost": [{"name": "总金额", "parentCode": "TotalMoney", "oneTotal": "87.64"}]},
            },
            service_mode="派送",
            form={"DispatchSms": "1"},
        )

        self.assertEqual("87.69元", total)

    def test_entry_total_matches_page_js_truncation_for_self_pickup(self):
        total = yunda_price._entry_total_text(
            {"info": "1", "data": {"CostTotal": "59.44", "InsuredCost": "4.00"}},
            service_mode="自提",
            form={"DispatchSms": "1"},
        )

        self.assertEqual("59.48元", total)

    def test_entry_total_adds_sms_to_total_money_when_cost_total_missing(self):
        total = yunda_price._entry_total_text(
            {"info": "1", "showCost": {"classifyCost": [{"name": "总金额", "parentCode": "TotalMoney", "oneTotal": "87.64"}]}},
            service_mode="派送",
            form={"DispatchSms": "1"},
        )

        self.assertEqual("87.69元", total)

    def test_entry_total_does_not_add_platform_fee_outside_cost_total(self):
        total = yunda_price._entry_total_text(
            {"info": "1", "data": {"CostTotal": "87.64", "PlatformCost": "4.00"}},
            service_mode="派送",
            form={"DispatchSms": "1"},
        )

        self.assertEqual("87.69元", total)

    def test_entry_total_keeps_sms_fee_for_cost_total_only_response(self):
        total = yunda_price._entry_total_text(
            {"info": "1", "data": {"CostTotal": "563.70"}},
            service_mode="自提",
            form={"DispatchSms": "1"},
        )

        self.assertEqual("563.75元", total)

    def test_entry_apply_insured_amount_range_uses_minimum_like_page(self):
        form = {"InsuredAmount": "0.00"}

        changed = yunda_price._entry_apply_insured_amount_range(
            form,
            {"info": 1, "data": {"MIN": 4000, "MAX": 200000}},
        )

        self.assertTrue(changed)
        self.assertEqual("4000", form["InsuredAmount"])

    def test_entry_apply_insured_amount_range_preserves_valid_value(self):
        form = {"InsuredAmount": "5000"}

        changed = yunda_price._entry_apply_insured_amount_range(
            form,
            {"info": 1, "data": {"MIN": 4000, "MAX": 200000}},
        )

        self.assertFalse(changed)
        self.assertEqual("5000", form["InsuredAmount"])

    def test_sync_entry_insured_amount_uses_page_range_endpoint(self):
        class Response:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = "{}"

            def json(self):
                return {"info": 1, "data": {"MIN": 4000, "MAX": 200000}}

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data=None, headers=None, allow_redirects=None, timeout=None):
                self.calls.append({"url": url, "data": dict(data or {})})
                return Response()

        form = {"GoodsType": "184", "SettlementTotalNumber": "220", "InsuredAmount": "0.00"}
        session = Session()

        yunda_price._sync_entry_insured_amount(session, form=form, referer=yunda_price.ENTRY_INDEX_URL)

        self.assertEqual("4000", form["InsuredAmount"])
        self.assertEqual(yunda_price.ENTRY_INSURED_AMOUNT_URL, session.calls[0]["url"])
        self.assertEqual({"GoodsType": "184", "SettlementTotalNumber": "220"}, session.calls[0]["data"])


if __name__ == "__main__":
    unittest.main()
