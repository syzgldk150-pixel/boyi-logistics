from __future__ import annotations

import unittest

from shared.finance import (
    Direction,
    FeeItemKey,
    FeeLevel,
    Platform,
    RONGHUI_BOOKING_FEE_ITEMS,
    RONGHUI_STRICT_EXACT_ALIASES,
    RONGHUI_VERIFIED_OPERATING_FEES,
    YUNDA_BOOKING_FEE_GROUPS,
    YUNDA_BOOKING_FEE_ITEMS,
    YUNDA_STRICT_EXACT_ALIASES,
    YUNDA_VERIFIED_OPERATING_FEES,
    mapping_seed_for_fee_item,
    month_start,
    validate_booking_fee_name,
)


class FinanceMappingTests(unittest.TestCase):
    def test_real_waybill_leaf_and_verified_baseline_counts_are_fixed(self) -> None:
        self.assertEqual(len(RONGHUI_BOOKING_FEE_ITEMS), 33)
        self.assertEqual(len(YUNDA_BOOKING_FEE_ITEMS), 39)
        self.assertEqual(len(RONGHUI_STRICT_EXACT_ALIASES), 11)
        self.assertEqual(len(YUNDA_STRICT_EXACT_ALIASES), 12)
        self.assertEqual(len(RONGHUI_VERIFIED_OPERATING_FEES), 49)
        self.assertEqual(len(YUNDA_VERIFIED_OPERATING_FEES), 25)
        self.assertFalse(YUNDA_BOOKING_FEE_GROUPS & YUNDA_BOOKING_FEE_ITEMS)

    def test_strict_exact_alias_uses_observed_direction_without_suffix_rules(self) -> None:
        expense_seed = mapping_seed_for_fee_item(
            FeeItemKey(Platform.RONGHUI, "收操作费", "", Direction.EXPENSE)
        )
        self.assertIsNotNone(expense_seed)
        self.assertIs(expense_seed.fee_level, FeeLevel.WAYBILL)
        self.assertEqual(expense_seed.booking_fee_name, "操作费")
        self.assertTrue(expense_seed.include_in_cost)
        income_seed = mapping_seed_for_fee_item(
            FeeItemKey(Platform.RONGHUI, "收操作费", "", Direction.INCOME)
        )
        self.assertIsNotNone(income_seed)
        self.assertFalse(income_seed.include_in_cost)
        self.assertIsNone(
            mapping_seed_for_fee_item(
                FeeItemKey(Platform.YUNDA, "未核对费用s", "", Direction.EXPENSE)
            )
        )

    def test_confirmed_yunda_s_and_f_bindings_are_exact_and_directional(self) -> None:
        send_expense = mapping_seed_for_fee_item(
            FeeItemKey(Platform.YUNDA, "派送费s", "", Direction.EXPENSE)
        )
        self.assertIsNotNone(send_expense)
        self.assertIs(send_expense.fee_level, FeeLevel.WAYBILL)
        self.assertEqual(send_expense.booking_fee_name, "派送费")
        self.assertTrue(send_expense.include_in_cost)
        arrival_income = mapping_seed_for_fee_item(
            FeeItemKey(Platform.YUNDA, "派送费F(新)", "", Direction.INCOME)
        )
        self.assertIsNotNone(arrival_income)
        self.assertIs(arrival_income.fee_level, FeeLevel.OPERATING)
        self.assertEqual(arrival_income.booking_fee_name, "")
        self.assertFalse(arrival_income.include_in_cost)
        self.assertIsNone(
            mapping_seed_for_fee_item(
                FeeItemKey(Platform.YUNDA, "派送费F(新)", "", Direction.EXPENSE)
            )
        )

    def test_meeting_fee_is_one_exact_primary_secondary_pair(self) -> None:
        pair_seed = mapping_seed_for_fee_item(
            FeeItemKey(
                Platform.YUNDA,
                "收其他费用",
                "收其他费用-会议费",
                Direction.EXPENSE,
            )
        )
        self.assertIsNotNone(pair_seed)
        self.assertEqual(pair_seed.booking_fee_name, "会务费")
        self.assertIsNone(
            mapping_seed_for_fee_item(
                FeeItemKey(Platform.YUNDA, "收其他费用", "", Direction.EXPENSE)
            )
        )

    def test_verified_operating_fee_uses_actual_direction_and_cost_policy(self) -> None:
        ordinary_expense = mapping_seed_for_fee_item(
            FeeItemKey(Platform.RONGHUI, "财务服务费-WS", "", Direction.EXPENSE)
        )
        self.assertIsNotNone(ordinary_expense)
        self.assertIs(ordinary_expense.fee_level, FeeLevel.OPERATING)
        self.assertTrue(ordinary_expense.include_in_cost)
        funding_expense = mapping_seed_for_fee_item(
            FeeItemKey(Platform.RONGHUI, "充值", "", Direction.EXPENSE)
        )
        self.assertIsNotNone(funding_expense)
        self.assertFalse(funding_expense.include_in_cost)
        deposit_expense = mapping_seed_for_fee_item(
            FeeItemKey(Platform.RONGHUI, "收云呼设备押金", "", Direction.EXPENSE)
        )
        self.assertIsNotNone(deposit_expense)
        self.assertFalse(deposit_expense.include_in_cost)
        income = mapping_seed_for_fee_item(
            FeeItemKey(Platform.RONGHUI, "财务服务费-WS", "", Direction.INCOME)
        )
        self.assertIsNotNone(income)
        self.assertFalse(income.include_in_cost)

    def test_booking_target_must_be_verified_leaf_and_operating_target_is_empty(self) -> None:
        self.assertEqual(
            validate_booking_fee_name(
                platform=Platform.RONGHUI,
                fee_level=FeeLevel.WAYBILL,
                booking_fee_name="操作费",
            ),
            "操作费",
        )
        with self.assertRaisesRegex(ValueError, "verified"):
            validate_booking_fee_name(
                platform=Platform.YUNDA,
                fee_level=FeeLevel.WAYBILL,
                booking_fee_name="平台费",
            )
        with self.assertRaisesRegex(ValueError, "operating"):
            validate_booking_fee_name(
                platform=Platform.YUNDA,
                fee_level=FeeLevel.OPERATING,
                booking_fee_name="操作费",
            )
        self.assertEqual(
            validate_booking_fee_name(
                platform=Platform.YUNDA,
                fee_level=FeeLevel.OPERATING,
                booking_fee_name="",
            ),
            "",
        )

    def test_month_start_accepts_html_month_format(self) -> None:
        self.assertEqual(month_start("2026-07").isoformat(), "2026-07-01")
        self.assertEqual(month_start("2026-07-19").isoformat(), "2026-07-01")
        with self.assertRaises(ValueError):
            month_start("2026-13")


if __name__ == "__main__":
    unittest.main()
