"""Quote inputs must fail before any provider request."""

import unittest
from unittest.mock import patch

from agent.tms_runtime.scripts import get_price
from tools import price_tool


class PriceInputValidationTests(unittest.TestCase):
    def test_tool_rejects_missing_nonpositive_and_nonfinite_weight(self):
        for params in (
            {"address": "测试地址"},
            {"address": "测试地址", "weight": 0},
            {"address": "测试地址", "weight": -1},
            {"address": "测试地址", "weight": float("nan")},
        ):
            with self.subTest(params=params), patch.object(price_tool, "get_combined_price") as quote:
                result = price_tool.run_price_tool(params)
                self.assertEqual(result["error_code"], "INVALID_PARAMS")
                quote.assert_not_called()

    def test_tool_rejects_invalid_explicit_volume(self):
        with patch.object(price_tool, "get_combined_price") as quote:
            result = price_tool.run_price_tool(
                {"address": "测试地址", "weight": 1, "volume": 0}
            )
        self.assertEqual(result["error_code"], "INVALID_PARAMS")
        quote.assert_not_called()

    def test_runtime_script_defends_against_invalid_weight(self):
        with patch.object(get_price, "fetch_prices") as quote:
            with self.assertRaisesRegex(ValueError, "weight"):
                get_price.run_once({"address": "测试地址"})
            with self.assertRaisesRegex(ValueError, "positive"):
                get_price.run_once({"address": "测试地址", "weight": -1})
        quote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
