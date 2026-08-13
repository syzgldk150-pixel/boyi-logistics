import sys
import unittest
from datetime import date
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from finance_service import (  # noqa: E402
    FinanceService,
    FinanceUnprocessableError,
    FinanceUnavailableError,
    FinanceValidationError,
    parse_finance_filters,
)


class _FakeFinanceRepository:
    def __init__(self):
        self.calls = []
        self.initialize_error = None
        self.save_error = None

    def initialize_schema(self):
        self.calls.append(("initialize_schema",))
        if self.initialize_error:
            raise self.initialize_error

    def get_summary(self, query):
        self.calls.append(("get_summary", query))
        return {
            "total_income": "120.00",
            "total_expense": "80.00",
            "net_change": "40.00",
            "waybill_cost": "50.00",
            "operating_cost": "30.00",
            "pending_fee_items": 2,
            "latest_success_at": "2026-07-12 00:01:00",
            "data_through_date": "2026-07-11",
            "validation_status": "passed",
            "failed_sources": [],
            "accounts": [
                {
                    "platform": "ronghui",
                    "account_id": "a",
                    "login_account": "账号 A",
                    "total_income": "100.00",
                    "total_expense": "80.00",
                    "waybill_cost": "50.00",
                    "operating_cost": "30.00",
                },
                {
                    "platform": "yunda",
                    "account_id": "b",
                    "login_account": "账号 B",
                    "total_income": "20.00",
                    "total_expense": "20.00",
                    "waybill_cost": None,
                    "operating_cost": None,
                },
            ],
        }

    def get_expense_ranking(self, query, limit=10):
        self.calls.append(("get_expense_ranking", query, limit))
        return [
            {"fee_name": "项目甲", "direction": "expense", "expense": "60.00"},
            {"fee_name": "项目乙", "direction": "expense", "expense": "30.00"},
        ]

    def get_trend(self, query):
        self.calls.append(("get_trend", query))
        return [
            {"date": "2026-07-10", "income": "100.00", "expense": "50.00", "net_change": "50.00"},
            {"date": "2026-07-11", "income": "25.00", "expense": None, "net_change": "25.00"},
        ]

    def list_entries(self, query, *, limit, offset):
        self.calls.append(("list_entries", query, limit, offset))
        return {"items": [{"id": 1, "income": "10.00", "expense": None}], "total": 1, "limit": limit, "offset": offset}

    def list_fee_mappings(self, *, platform=None, effective_month=None):
        self.calls.append(("list_fee_mappings", platform, effective_month))
        return {
            "items": [
                {
                    "fee_item_id": 1,
                    "platform": "yunda",
                    "primary_fee_name": "项目甲",
                    "secondary_fee_name": "",
                    "mapping_status": "pending",
                    "booking_fee_name": "",
                },
                {
                    "fee_item_id": 2,
                    "platform": "yunda",
                    "primary_fee_name": "项目乙",
                    "secondary_fee_name": "",
                    "mapping_status": "bound",
                    "booking_fee_name": "录单项目乙",
                },
            ],
            "total": 2,
            "booking_fee_items": {
                "ronghui": ["派件费"],
                "yunda": ["派送费"],
            },
        }

    def save_fee_mapping(self, **kwargs):
        self.calls.append(("save_fee_mapping", kwargs))
        if self.save_error:
            raise self.save_error
        return 9

    def list_sync_batches(self, *, limit, offset, status=None):
        self.calls.append(("list_sync_batches", limit, offset, status))
        return {"items": [{"id": 3, "status": "success"}], "total": 1, "limit": limit, "offset": offset}


class FinanceServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = _FakeFinanceRepository()
        self.service = FinanceService(self.repository)

    def test_default_period_is_current_month_and_filters_are_explicit(self):
        filters = parse_finance_filters(
            {
                "platform": ["yunda"],
                "account_id": ["account-a"],
                "direction": ["expense"],
                "fee_level": ["waybill"],
                "fee_name": ["派送费"],
                "waybill_no": ["WB001"],
            },
            today=date(2026, 7, 12),
        )

        self.assertEqual(date(2026, 7, 1), filters.start_date)
        self.assertEqual(date(2026, 7, 12), filters.end_date)
        self.assertEqual("yunda", filters.platform)
        self.assertEqual("account-a", filters.account_id)
        self.assertEqual("expense", filters.direction)
        self.assertEqual("waybill", filters.fee_level)
        self.assertEqual("派送费", filters.fee_name)
        self.assertEqual("WB001", filters.waybill_no)

    def test_schema_initialization_uses_shared_repository_contract(self):
        self.service.initialize_schema()

        self.assertIn(("initialize_schema",), self.repository.calls)

    def test_schema_initialization_failure_is_explicitly_unavailable(self):
        self.repository.initialize_error = RuntimeError("database unavailable")

        with self.assertRaisesRegex(FinanceUnavailableError, "财务数据表初始化失败"):
            self.service.initialize_schema()

    def test_invalid_date_range_fails_without_swapping_dates(self):
        with self.assertRaisesRegex(FinanceValidationError, "结束日期不能早于开始日期"):
            parse_finance_filters(
                {"start_date": ["2026-07-12"], "end_date": ["2026-07-01"]},
                today=date(2026, 7, 12),
            )

    def test_repeated_finance_filter_is_rejected_instead_of_taking_first(self):
        with self.assertRaisesRegex(FinanceValidationError, "不能重复"):
            parse_finance_filters(
                {"platform": ["ronghui", "yunda"]},
                today=date(2026, 7, 12),
            )

    def test_summary_keeps_money_strings_and_adds_decimal_plot_ratios(self):
        payload = self.service.get_summary({}, today=date(2026, 7, 12))

        self.assertEqual("120.00", payload["total_income"])
        self.assertEqual("100.00", payload["accounts"][0]["waybill_cost_plot"])
        self.assertEqual("60.00", payload["accounts"][0]["operating_cost_plot"])
        self.assertIsNone(payload["accounts"][1]["waybill_cost_plot"])
        self.assertIsNone(payload["accounts"][1]["operating_cost_plot"])
        self.assertEqual("100.00", payload["expense_ranking"][0]["expense_plot"])
        self.assertEqual("50.00", payload["expense_ranking"][1]["expense_plot"])
        self.assertEqual({"start_date": "2026-07-01", "end_date": "2026-07-12"}, payload["period"])

    def test_trend_preserves_null_and_uses_server_plot_coordinates(self):
        payload = self.service.get_trend({}, today=date(2026, 7, 12))

        self.assertEqual("100.00", payload["items"][0]["income_plot"])
        self.assertEqual("50.00", payload["items"][0]["expense_plot"])
        self.assertEqual("25.00", payload["items"][1]["income_plot"])
        self.assertIsNone(payload["items"][1]["expense"])
        self.assertIsNone(payload["items"][1]["expense_plot"])

    def test_entries_apply_pagination_without_changing_amounts(self):
        payload = self.service.list_entries(
            {"page": ["3"], "page_size": ["20"]},
            today=date(2026, 7, 12),
        )

        self.assertEqual(3, payload["page"])
        self.assertEqual(20, payload["page_size"])
        self.assertEqual("10.00", payload["items"][0]["income"])
        self.assertIn(("list_entries", self.repository.calls[-1][1], 20, 40), self.repository.calls)

    def test_mapping_filters_are_server_side_and_do_not_use_account_id(self):
        payload = self.service.list_fee_mappings(
            {
                "platform": ["yunda"],
                "account_id": ["ignored-by-contract"],
                "effective_month": ["2026-07"],
                "status": ["bound"],
                "search": ["录单项目乙"],
            }
        )

        self.assertEqual(1, payload["total"])
        self.assertEqual(2, payload["items"][0]["fee_item_id"])
        self.assertEqual(
            {"ronghui": ["派件费"], "yunda": ["派送费"]},
            payload["booking_fee_items"],
        )
        self.assertIn(("list_fee_mappings", "yunda", "2026-07"), self.repository.calls)

    def test_save_mapping_ignores_untrusted_direction_and_passes_audit_fields(self):
        payload = self.service.save_fee_mapping(
            4,
            {
                "direction": "income",
                "fee_level": "waybill",
                "booking_fee_name": "派送费",
                "effective_start_month": "2026-07",
                "effective_end_month": "",
                "include_in_cost": True,
                "reason": "确认录单项目绑定",
            },
            changed_by="admin",
        )

        self.assertEqual({"mapping_id": 9, "fee_item_id": 4}, payload)
        call = self.repository.calls[-1][1]
        self.assertNotIn("direction", call)
        self.assertEqual("waybill", call["fee_level"])
        self.assertEqual("派送费", call["booking_fee_name"])
        self.assertTrue(call["include_in_cost"])
        self.assertEqual("admin", call["changed_by"])

    def test_save_mapping_does_not_require_direction_from_client(self):
        payload = self.service.save_fee_mapping(
            4,
            {
                "fee_level": "operating",
                "booking_fee_name": "",
                "effective_start_month": "2026-07",
                "include_in_cost": False,
                "reason": "方向由费用项目锁定",
            },
            changed_by="admin",
        )

        self.assertEqual({"mapping_id": 9, "fee_item_id": 4}, payload)
        self.assertNotIn("direction", self.repository.calls[-1][1])

    def test_locked_repository_direction_validation_is_preserved(self):
        self.repository.save_error = ValueError("only expense mappings may be included in cost")

        with self.assertRaisesRegex(
            FinanceUnprocessableError, "保存费用项目绑定未通过业务校验"
        ):
            self.service.save_fee_mapping(
                4,
                {
                    "fee_level": "operating",
                    "booking_fee_name": "",
                    "effective_start_month": "2026-07",
                    "include_in_cost": True,
                    "reason": "由仓储核验费用方向",
                },
                changed_by="admin",
            )

    def test_operating_mapping_allows_no_booking_fee_item(self):
        self.service.save_fee_mapping(
            5,
            {
                "direction": "expense",
                "fee_level": "operating",
                "booking_fee_name": "",
                "effective_start_month": "2026-07",
                "include_in_cost": True,
                "reason": "确认运营级费用",
            },
            changed_by="admin",
        )

        self.assertEqual("", self.repository.calls[-1][1]["booking_fee_name"])

    def test_waybill_mapping_requires_booking_fee_item(self):
        with self.assertRaisesRegex(FinanceValidationError, "请填写对应录单项目"):
            self.service.save_fee_mapping(
                5,
                {
                    "direction": "expense",
                    "fee_level": "waybill",
                    "booking_fee_name": "",
                    "effective_start_month": "2026-07",
                    "include_in_cost": True,
                    "reason": "尝试保存未绑定运单级费用",
                },
                changed_by="admin",
            )

    def test_sync_builds_governed_arguments_without_login_context(self):
        payload = self.service.build_sync_arguments(
            {"platform": "yunda", "account_id": "account-a", "rescan_days": 7}
        )

        self.assertEqual(
            {"mode": "sync", "rescan_days": 7, "platform": "yunda", "account_id": "account-a"},
            payload,
        )
        self.assertNotIn("login_account", payload)
        self.assertNotIn("session_profile", payload)

    def test_backfill_requires_real_date_range(self):
        with self.assertRaisesRegex(FinanceValidationError, "请填写回溯开始日期"):
            self.service.build_backfill_arguments({"end_date": "2026-07-11"})

    def test_sync_rejects_credential_fields(self):
        with self.assertRaisesRegex(FinanceValidationError, "不能包含登录账号"):
            self.service.build_sync_arguments({"login_account": "not-allowed"})

    def test_retry_sends_only_batch_id(self):
        self.assertEqual(
            {"mode": "retry", "batch_id": 8},
            self.service.build_retry_arguments(8),
        )

    def test_missing_shared_repository_is_explicitly_unavailable(self):
        service = FinanceService(object())

        with self.assertRaises(FinanceUnavailableError):
            service.get_summary({}, today=date(2026, 7, 12))


if __name__ == "__main__":
    unittest.main()
