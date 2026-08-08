from __future__ import annotations

import datetime as dt
import inspect
import unittest
from decimal import Decimal
from typing import Any

from shared.finance import (
    Direction,
    FeeLevel,
    FeeMappingSeed,
    FINANCE_REQUIRED_TABLES,
    FinanceQuery,
    FinanceRepository,
    Platform,
    RONGHUI_BOOKING_FEE_ITEMS,
    YUNDA_BOOKING_FEE_ITEMS,
    mysql_schema_statements,
)


class RouterCursor:
    def __init__(self, records: list[tuple[str, tuple[Any, ...]]], router=None) -> None:
        self.records = records
        self.router = router or (lambda _sql, _params: [])
        self.rows: list[dict[str, Any]] = []
        self.lastrowid = 1
        self.rowcount = 1
        self.description = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(str(sql).split())
        values = tuple(params or ())
        self.records.append((normalized, values))
        routed = self.router(normalized, values)
        if routed is None:
            self.rows = []
        elif isinstance(routed, dict):
            self.rows = [routed]
        else:
            self.rows = list(routed)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        return None


class RouterConnection:
    def __init__(self, records: list[tuple[str, tuple[Any, ...]]], router=None) -> None:
        self.records = records
        self.router = router
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return RouterCursor(self.records, self.router)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FinanceRepositoryTests(unittest.TestCase):
    def test_finance_schema_contract_lists_all_migration_owned_tables(self) -> None:
        for table in {
            "finance_sync_batches",
            "finance_sync_runs",
            "finance_transactions",
            "finance_summary_snapshots",
            "finance_fee_items",
            "finance_fee_mappings",
            "finance_mapping_audit_logs",
        }:
            self.assertIn(table, FINANCE_REQUIRED_TABLES)
        self.assertEqual((), mysql_schema_statements())

    def test_initialize_schema_uses_caller_factory_for_migration_validation(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []
        connections: list[RouterConnection] = []

        def factory():
            connection = RouterConnection(records)
            connections.append(connection)
            return connection

        def router(sql: str, _params: tuple[Any, ...]):
            if "information_schema.TABLES" in sql:
                return [{"TABLE_NAME": table} for table in FINANCE_REQUIRED_TABLES]
            return []

        FinanceRepository(lambda: RouterConnection(records, router)).initialize_schema()
        self.assertEqual(1, len(records))

    def test_public_signatures_keep_direction_read_only_and_expose_pipeline_methods(self) -> None:
        save_parameters = inspect.signature(FinanceRepository.save_fee_mapping).parameters
        self.assertNotIn("direction", save_parameters)
        for name in (
            "create_batch",
            "start_run",
            "start_failed_run",
            "commit_run_snapshot",
            "mark_run_no_data",
            "fail_run",
            "finalize_batch",
            "list_missing_dates",
            "list_retry_targets",
            "get_validation_context",
            "seed_fee_mappings",
            "get_summary",
            "get_trend",
            "get_expense_ranking",
            "list_entries",
            "list_fee_mappings",
            "list_sync_batches",
        ):
            self.assertTrue(callable(getattr(FinanceRepository, name)))

    def test_visible_entry_sql_escapes_mysql_date_format_percent(self) -> None:
        sql = FinanceRepository._VISIBLE_ENTRY_FROM

        self.assertEqual(
            2,
            sql.count("DATE_FORMAT(t.business_date, '%%Y-%%m-01')"),
        )
        self.assertNotIn("DATE_FORMAT(t.business_date, '%Y-%m-01')", sql)

    def test_summary_uses_latest_valid_run_and_no_data_freshness(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if "AS pending_fee_items" in sql:
                return {
                    "total_income": Decimal("10.005"),
                    "total_expense": Decimal("3.004"),
                    "net_change": Decimal("7.001"),
                    "waybill_cost": Decimal("2.005"),
                    "operating_cost": Decimal("0.999"),
                    "pending_fee_items": 1,
                    "warning_rows": 0,
                }
            if "GROUP BY t.platform, t.account_id" in sql:
                return [
                    {
                        "platform": "ronghui",
                        "account_id": "fictional-account",
                        "login_account": "fictional-login",
                        "total_income": Decimal("10.005"),
                        "total_expense": Decimal("3.004"),
                        "waybill_cost": Decimal("2.005"),
                        "operating_cost": Decimal("0.999"),
                    }
                ]
            if "MAX(COALESCE(r.login_account, '')) AS login_account" in sql:
                return [
                    {
                        "platform": "ronghui",
                        "account_id": "fictional-account",
                        "login_account": "fictional-login",
                    },
                    {
                        "platform": "yunda",
                        "account_id": "fictional-no-data-account",
                        "login_account": "fictional-no-data-login",
                    },
                ]
            if "AS warning_runs" in sql:
                return {
                    "latest_success_at": dt.datetime(2026, 1, 3, 0, 10),
                    "data_through_date": dt.date(2026, 1, 2),
                    "warning_runs": 0,
                }
            if "FROM finance_sync_runs r" in sql:
                return []
            return []

        repository = FinanceRepository(lambda: RouterConnection(records, router))
        result = repository.get_summary(FinanceQuery("2026-01-01", "2026-01-31"))
        self.assertEqual(result["total_income"], "10.01")
        self.assertEqual(result["total_expense"], "3.00")
        self.assertEqual(result["net_change"], "7.00")
        self.assertEqual(result["data_through_date"], "2026-01-02")
        self.assertEqual(result["validation_status"], "passed")
        self.assertEqual(2, len(result["accounts"]))
        no_data_account = next(
            row
            for row in result["accounts"]
            if row["account_id"] == "fictional-no-data-account"
        )
        self.assertEqual("0.00", no_data_account["total_expense"])
        self.assertEqual("0.00", no_data_account["waybill_cost"])
        combined_sql = "\n".join(sql for sql, _ in records)
        self.assertIn("MAX(id) AS latest_run_id", combined_sql)
        self.assertIn("status IN ('success', 'no_data')", combined_sql)

    def test_expense_ranking_contract_matches_accessible_table(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if "ORDER BY expense DESC" in sql:
                return [
                    {
                        "primary_fee_name": "一级项目",
                        "secondary_fee_name": "二级项目",
                        "booking_fee_name": "操作费",
                        "fee_level": "waybill",
                        "expense": Decimal("12.345"),
                    }
                ]
            return []

        result = FinanceRepository(lambda: RouterConnection(records, router)).get_expense_ranking(
            FinanceQuery("2026-01-01", "2026-01-31"), limit=10
        )
        self.assertEqual(result[0]["fee_name"], "二级项目")
        self.assertEqual(result[0]["direction"], Direction.EXPENSE.value)
        self.assertEqual(result[0]["expense"], "12.35")
        self.assertIn("MAX(id) AS latest_run_id", "\n".join(sql for sql, _ in records))

    def test_trend_includes_explicit_no_data_dates_as_zero_without_filling_gaps(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT t.business_date AS date"):
                return {
                    "date": dt.date(2026, 1, 2),
                    "income": Decimal("2.0000"),
                    "expense": Decimal("1.0000"),
                    "net_change": Decimal("1.0000"),
                }
            if sql.startswith("SELECT DISTINCT r.target_date AS date"):
                return [
                    {"date": dt.date(2026, 1, 2)},
                    {"date": dt.date(2026, 1, 3)},
                ]
            return []

        rows = FinanceRepository(
            lambda: RouterConnection(records, router)
        ).get_trend(FinanceQuery("2026-01-01", "2026-01-04"))

        self.assertEqual(["2026-01-02", "2026-01-03"], [row["date"] for row in rows])
        self.assertEqual("0.00", rows[1]["income"])
        self.assertEqual("0.00", rows[1]["expense"])

    def test_fee_mapping_response_uses_month_values_and_shared_leaf_options(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT COUNT(*) AS total FROM finance_fee_items"):
                return {"total": 1}
            if "SELECT fi.id AS fee_item_id" in sql:
                return [
                    {
                        "fee_item_id": 1,
                        "platform": "yunda",
                        "primary_fee_name": "测试项目",
                        "secondary_fee_name": "",
                        "direction": "expense",
                        "first_seen_month": dt.date(2026, 1, 1),
                        "last_seen_month": dt.date(2026, 2, 1),
                        "mapping_id": None,
                        "mapping_status": "pending",
                        "fee_level": None,
                        "booking_fee_name": None,
                        "effective_start_month": None,
                        "effective_end_month": None,
                        "include_in_cost": 0,
                        "version_no": None,
                    }
                ]
            return []

        result = FinanceRepository(lambda: RouterConnection(records, router)).list_fee_mappings(
            effective_month="2026-02"
        )
        self.assertEqual(result["items"][0]["first_seen_month"], "2026-01")
        self.assertEqual(result["items"][0]["last_seen_month"], "2026-02")
        self.assertEqual(
            result["booking_fee_items"][Platform.RONGHUI.value],
            sorted(RONGHUI_BOOKING_FEE_ITEMS),
        )
        self.assertEqual(
            result["booking_fee_items"][Platform.YUNDA.value],
            sorted(YUNDA_BOOKING_FEE_ITEMS),
        )

    def test_same_month_mapping_correction_supersedes_overlapping_active_version(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT * FROM finance_fee_items"):
                return {
                    "id": 9,
                    "platform": "yunda",
                    "direction": "expense",
                }
            if sql.startswith("SELECT * FROM finance_fee_mappings"):
                return {
                    "id": 7,
                    "fee_item_id": 9,
                    "direction": "expense",
                    "fee_level": "operating",
                    "booking_fee_name": None,
                    "effective_start_month": dt.date(2026, 7, 1),
                    "effective_end_month": None,
                    "include_in_cost": 1,
                    "mapping_status": "bound",
                    "version_no": 1,
                }
            if sql.startswith("SELECT COALESCE(MAX(version_no)"):
                return {"max_version": 1}
            return []

        repository = FinanceRepository(lambda: RouterConnection(records, router))
        mapping_id = repository.save_fee_mapping(
            fee_item_id=9,
            fee_level=FeeLevel.OPERATING,
            booking_fee_name="",
            effective_start_month="2026-07",
            include_in_cost=True,
            changed_by="fictional-admin",
            reason="fictional correction",
        )
        self.assertEqual(mapping_id, 1)
        sqls = [sql for sql, _ in records]
        supersede_index = next(
            index
            for index, sql in enumerate(sqls)
            if sql.startswith("UPDATE finance_fee_mappings SET superseded_at")
        )
        insert_index = next(
            index
            for index, sql in enumerate(sqls)
            if sql.startswith("INSERT INTO finance_fee_mappings")
        )
        self.assertLess(supersede_index, insert_index)

    def test_verified_seed_extends_to_actual_earlier_backfill_month(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []
        seed = FeeMappingSeed(
            platform=Platform.RONGHUI,
            primary_fee_name="测试严格项目",
            direction=Direction.EXPENSE,
            fee_level=FeeLevel.WAYBILL,
            booking_fee_name="操作费",
            include_in_cost=True,
        )

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT id, direction, fee_level"):
                return {
                    "id": 7,
                    "direction": "expense",
                    "fee_level": "waybill",
                    "booking_fee_name": "操作费",
                    "effective_start_month": dt.date(2026, 7, 1),
                    "include_in_cost": 1,
                    "created_by": "system:verified-baseline",
                }
            if sql.startswith("SELECT COALESCE(MAX(version_no)"):
                return {"max_version": 1}
            return []

        cursor = RouterCursor(records, router)
        repository = FinanceRepository(lambda: RouterConnection(records, router))
        created = repository._seed_mapping_if_missing(
            cursor,
            fee_item_id=9,
            first_seen_month=dt.date(2026, 1, 1),
            seed=seed,
        )

        self.assertTrue(created)
        insert_params = next(
            params
            for sql, params in records
            if sql.startswith("INSERT INTO finance_fee_mappings")
        )
        self.assertEqual(dt.date(2026, 1, 1), insert_params[4])
        self.assertEqual(dt.date(2026, 6, 1), insert_params[5])

    def test_validation_context_reads_platform_categories_and_latest_success_content(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT raw_primary_fee_name"):
                return [
                    {
                        "raw_primary_fee_name": "测试费用",
                        "raw_secondary_fee_name": "",
                        "direction": "expense",
                    }
                ]
            if sql.startswith("SELECT t.*"):
                return [
                    {
                        "platform": "ronghui",
                        "account_id": "fictional-account",
                        "login_account": "fictional-login",
                        "source_record_key": "fictional-guid",
                        "business_date": dt.date(2026, 1, 2),
                        "transaction_at": dt.datetime(2026, 1, 2, 10),
                        "raw_primary_fee_name": "测试费用",
                        "raw_secondary_fee_name": "",
                        "direction": "expense",
                        "income": Decimal("0.0000"),
                        "expense": Decimal("1.0000"),
                        "before_balance": Decimal("10.0000"),
                        "after_balance": Decimal("9.0000"),
                        "waybill_no": "fictional-waybill",
                        "source_reference": "0001",
                        "remark": "",
                    }
                ]
            return []

        context = FinanceRepository(lambda: RouterConnection(records, router)).get_validation_context(
            platform=Platform.RONGHUI,
            account_id="fictional-account",
            target_date="2026-01-02",
            source_record_keys=["fictional-guid"],
        )
        self.assertEqual(len(context["known_fee_items"]), 1)
        self.assertIn("fictional-guid", context["previous_record_payloads"])
        combined_sql = "\n".join(sql for sql, _ in records)
        self.assertIn("status = %s", combined_sql)
        self.assertIn("MAX(id) AS latest_run_id", combined_sql)

    def test_finalize_empty_batch_is_no_data_not_success(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT id FROM finance_sync_batches"):
                return {"id": 42}
            if sql.startswith("SELECT status, COUNT(*)"):
                return []
            return []

        status = FinanceRepository(lambda: RouterConnection(records, router)).finalize_batch(42)
        self.assertEqual(status.value, "no_data")
        update = next(
            params
            for sql, params in records
            if sql.startswith("UPDATE finance_sync_batches SET status")
        )
        self.assertEqual(update[0], "no_data")

    def test_binding_failure_run_uses_null_login_fields_and_is_immediately_failed(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT id, status FROM finance_sync_batches"):
                return {"id": 8, "status": "running"}
            if sql.startswith("SELECT COALESCE(MAX(attempt_no)"):
                return {"max_attempt": 0}
            return []

        run_id = FinanceRepository(lambda: RouterConnection(records, router)).start_failed_run(
            batch_id=8,
            platform=Platform.RONGHUI,
            account_id="fictional-role-account",
            target_date="2026-01-02",
            error_code="ACCOUNT_BINDING_FAILED",
            error_message="fictional binding failure",
        )
        self.assertEqual(run_id, 1)
        insert_sql, insert_params = next(
            (sql, params)
            for sql, params in records
            if sql.startswith("INSERT INTO finance_sync_runs")
        )
        self.assertIn("login_account, session_profile", insert_sql)
        self.assertIn("NULL, NULL", insert_sql)
        self.assertIn("failed", insert_params)
        self.assertNotIn("", insert_params)

    def test_sync_batch_rows_include_latest_failed_account_date_details(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT COUNT(*) AS total FROM finance_sync_batches"):
                return {"total": 1}
            if "COUNT(r.id) AS total_runs" in sql:
                return {
                    "id": 21,
                    "trigger_type": "sync",
                    "requested_start_date": dt.date(2026, 1, 2),
                    "requested_end_date": dt.date(2026, 1, 2),
                    "rescan_days": 1,
                    "status": "partial_failed",
                    "earliest_date_status": None,
                    "requested_by": None,
                    "started_at": dt.datetime(2026, 1, 3, 0, 10),
                    "finished_at": dt.datetime(2026, 1, 3, 0, 11),
                    "error_code": None,
                    "error_message": None,
                    "created_at": dt.datetime(2026, 1, 3, 0, 10),
                    "total_runs": 4,
                    "success_runs": 3,
                    "failed_runs": 1,
                }
            if sql.startswith("SELECT r.batch_id, r.platform"):
                return {
                    "batch_id": 21,
                    "platform": "yunda",
                    "account_id": "fictional-yunda-role",
                    "target_date": dt.date(2026, 1, 2),
                    "error_code": "FIELD_DRIFT",
                    "error_message": "fictional sanitized failure",
                }
            return []

        result = FinanceRepository(
            lambda: RouterConnection(records, router)
        ).list_sync_batches()

        self.assertEqual(1, result["total"])
        self.assertEqual(1, len(result["items"][0]["failed_sources"]))
        failure = result["items"][0]["failed_sources"][0]
        self.assertEqual("fictional-yunda-role", failure["account_id"])
        self.assertEqual("FIELD_DRIFT", failure["error_code"])


if __name__ == "__main__":
    unittest.main()
