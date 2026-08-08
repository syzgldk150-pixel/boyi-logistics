from __future__ import annotations

import datetime as dt
import unittest
from decimal import Decimal, ROUND_HALF_UP

from shared.finance import (
    AccountBindingAmbiguousError,
    AccountBindingInvalidError,
    AccountBindingNotFoundError,
    Direction,
    FeeItemKey,
    FeeLevel,
    FeeMappingSeed,
    FinanceQuery,
    InvalidAmountError,
    MissingAmountError,
    Platform,
    TransactionRecord,
    format_money,
    quantize_storage,
    resolve_account_binding,
    sanitize_source_payload,
    to_decimal,
)


class FinanceDomainTests(unittest.TestCase):
    def test_decimal_conversion_and_display_rounding_are_financially_explicit(self) -> None:
        self.assertEqual(to_decimal("1,234.56789"), Decimal("1234.56789"))
        self.assertEqual(quantize_storage("1.23456"), Decimal("1.2346"))
        self.assertEqual(format_money("1.005"), "1.01")
        self.assertEqual(
            Decimal(format_money("1.005")),
            Decimal("1.005").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )

    def test_missing_non_finite_and_boolean_amounts_fail(self) -> None:
        with self.assertRaises(MissingAmountError):
            to_decimal("")
        with self.assertRaises(InvalidAmountError):
            to_decimal("NaN")
        with self.assertRaises(InvalidAmountError):
            to_decimal(True)
        self.assertEqual(to_decimal(None, missing_as_zero=True), Decimal("0.0000"))

    def test_account_resolution_requires_exact_unique_active_system_and_login(self) -> None:
        rows = [
            {
                "account_id": "fictional_a",
                "system": "ronghui",
                "login_account": "test-login-001",
                "session_profile": "fictional_profile_a",
                "name": "测试账号 A",
                "is_active": True,
                "is_default": False,
            },
            {
                "account_id": "fictional_disabled",
                "system": "ronghui",
                "login_account": "test-login-002",
                "session_profile": "fictional_profile_b",
                "is_active": False,
                "is_default": True,
            },
        ]
        binding = resolve_account_binding(
            rows, system="ronghui", login_account="test-login-001"
        )
        self.assertEqual(binding.account_id, "fictional_a")
        self.assertEqual(binding.session_profile, "fictional_profile_a")
        with self.assertRaises(AccountBindingNotFoundError):
            resolve_account_binding(rows, system="ronghui", login_account="test-login-002")
        with self.assertRaises(AccountBindingNotFoundError):
            resolve_account_binding(rows, system="yunda", login_account="test-login-001")

    def test_account_resolution_rejects_ambiguity_and_missing_active_state(self) -> None:
        duplicate = {
            "system": "yunda",
            "login_account": "fictional-login",
            "session_profile": "fictional-profile",
            "is_active": True,
        }
        with self.assertRaises(AccountBindingAmbiguousError):
            resolve_account_binding(
                [
                    {**duplicate, "account_id": "fictional_1"},
                    {**duplicate, "account_id": "fictional_2"},
                ],
                system="yunda",
                login_account="fictional-login",
            )
        with self.assertRaises(AccountBindingInvalidError):
            resolve_account_binding(
                [
                    {
                        "account_id": "fictional_3",
                        "system": "yunda",
                        "login_account": "fictional-login",
                        "session_profile": "fictional-profile",
                    }
                ],
                system="yunda",
                login_account="fictional-login",
            )
        with self.assertRaises(AccountBindingAmbiguousError):
            resolve_account_binding(
                [
                    {**duplicate, "account_id": "fictional_active"},
                    {**duplicate, "account_id": "fictional_inactive", "is_active": False},
                ],
                system="yunda",
                login_account="fictional-login",
            )
        with self.assertRaises(AccountBindingNotFoundError):
            resolve_account_binding(
                [{**duplicate, "account_id": "fictional_zero", "is_active": "0"}],
                system="yunda",
                login_account="fictional-login",
            )
        with self.assertRaises(AccountBindingInvalidError):
            resolve_account_binding(
                [{**duplicate, "account_id": "fictional_invalid", "is_active": "enabled"}],
                system="yunda",
                login_account="fictional-login",
            )

    def test_transaction_requires_stable_key_and_preserves_missing_balance(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_record_key"):
            TransactionRecord(
                platform=Platform.RONGHUI,
                account_id="fictional-account",
                login_account="fictional-login",
                source_record_key="",
                business_date="2026-01-01",
                primary_fee_name="测试费用",
                direction=Direction.EXPENSE,
                income="0",
                expense="1",
            )
        record = TransactionRecord(
            platform=Platform.RONGHUI,
            account_id="fictional-account",
            login_account="fictional-login",
            source_record_key="fictional-guid",
            business_date="2026-01-01",
            primary_fee_name="测试费用",
            direction=Direction.EXPENSE,
            income="0",
            expense="1.23456",
        )
        self.assertEqual(record.expense, Decimal("1.2346"))
        self.assertIsNone(record.before_balance)
        self.assertIsNone(record.after_balance)
        self.assertEqual(
            record.fee_key,
            FeeItemKey(Platform.RONGHUI, "测试费用", "", Direction.EXPENSE),
        )

    def test_transaction_direction_cannot_contradict_normalized_amounts(self) -> None:
        with self.assertRaisesRegex(ValueError, "income transaction"):
            TransactionRecord(
                platform=Platform.YUNDA,
                account_id="fictional-account",
                login_account="fictional-login",
                source_record_key="fictional-id",
                business_date="2026-01-01",
                primary_fee_name="测试费用",
                direction=Direction.INCOME,
                income="1",
                expense="2",
            )

    def test_source_payload_whitelist_excludes_unknown_fields_and_rejects_secrets(self) -> None:
        self.assertEqual(
            sanitize_source_payload(
                {"BILL_CODE": "fictional-bill", "unknown_business_field": "ignored"}
            ),
            {"BILL_CODE": "fictional-bill"},
        )
        with self.assertRaisesRegex(ValueError, "sensitive"):
            sanitize_source_payload({"access_token": "never-store"})

    def test_finance_query_and_month_inputs_validate(self) -> None:
        query = FinanceQuery(
            start_date=dt.date(2026, 1, 1),
            end_date="2026-01-31",
            platform="ronghui",
            direction="expense",
            fee_level="waybill",
        )
        self.assertIs(query.platform, Platform.RONGHUI)
        self.assertIs(query.direction, Direction.EXPENSE)
        self.assertIs(query.fee_level, FeeLevel.WAYBILL)
        with self.assertRaisesRegex(ValueError, "start_date"):
            FinanceQuery("2026-02-01", "2026-01-31")

    def test_operating_seed_cannot_bypass_empty_booking_target_rule(self) -> None:
        with self.assertRaisesRegex(ValueError, "operating"):
            FeeMappingSeed(
                platform=Platform.YUNDA,
                primary_fee_name="测试运营费",
                direction=Direction.EXPENSE,
                fee_level=FeeLevel.OPERATING,
                booking_fee_name="操作费",
                include_in_cost=True,
            )


if __name__ == "__main__":
    unittest.main()
