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
    FinanceSnapshotRejectedError,
    Platform,
    RONGHUI_BOOKING_FEE_ITEMS,
    RONGHUI_CONFIRMED_FEE_RULES,
    enabled_finance_platforms,
    enabled_finance_source_specs,
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
    @staticmethod
    def _enabled_source_params() -> tuple[str, ...]:
        return tuple(
            value
            for spec in enabled_finance_source_specs()
            for value in (spec.platform, spec.account_id)
        )

    def _assert_placeholder_counts(
        self, records: list[tuple[str, tuple[Any, ...]]]
    ) -> None:
        for sql, params in records:
            self.assertEqual(
                sql.count("%s"),
                len(params),
                msg=f"placeholder mismatch for SQL: {sql}",
            )

    def test_user_confirmed_ronghui_baseline_contains_exact_fourteen_subjects(self) -> None:
        expected = {
            "收直派服务费": ("direct_delivery_service", "waybill", True),
            "收包仓费": ("warehouse_contract_fee", "operating", False),
            "保险费": ("insurance_fee", "waybill", True),
            "寄到付款": ("cod_freight_income", "waybill", True),
            "电子标签服务费": ("electronic_label_service", "waybill", True),
            "收固定中转费": ("fixed_transfer_fee", "operating", False),
            "短信扣费": ("sms_fee", "waybill", True),
            "收中转费追加": ("transfer_fee_adjustment", "waybill", True),
            "收到付款手续费": ("cod_handling_fee", "waybill", True),
            "电子回单服务费": ("electronic_receipt_service", "waybill", True),
            "收场地费折让": ("site_fee_discount", "waybill", True),
            "收派送费折让": ("delivery_fee_discount", "waybill", True),
            "收末端请车费": ("terminal_vehicle_fee", "waybill", True),
            "收派送费": ("delivery_fee", "waybill", True),
        }

        actual = {
            item.primary_fee_name: (
                item.subject_code,
                item.fee_level.value,
                item.requires_waybill,
            )
            for item in RONGHUI_CONFIRMED_FEE_RULES
        }
        self.assertEqual(expected, actual)

    def test_finance_schema_contract_lists_all_migration_owned_tables(self) -> None:
        for table in {
            "finance_sync_batches",
            "finance_sync_runs",
            "finance_transactions",
            "finance_summary_snapshots",
            "finance_fee_items",
            "finance_fee_mappings",
            "finance_mapping_audit_logs",
            "finance_fee_subjects",
            "finance_review_cases",
            "finance_review_ai_runs",
            "finance_waybill_facts",
            "finance_anomalies",
            "finance_knowledge_exports",
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

    def test_initialize_schema_accepts_default_dbapi_tuple_rows(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if "information_schema.TABLES" in sql:
                return [(table,) for table in FINANCE_REQUIRED_TABLES]
            return []

        FinanceRepository(lambda: RouterConnection(records, router)).initialize_schema()
        self.assertEqual(1, len(records))

    def test_initialize_schema_rejects_unknown_row_shapes(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if "information_schema.TABLES" in sql:
                return [("finance_sync_batches", "unexpected")]
            return []

        with self.assertRaisesRegex(TypeError, "unsupported row shape"):
            FinanceRepository(lambda: RouterConnection(records, router)).initialize_schema()

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
            "list_review_cases",
            "list_waybill_facts",
            "get_evolution_summary",
            "get_knowledge_snapshot",
        ):
            self.assertTrue(callable(getattr(FinanceRepository, name)))

    def test_visible_entry_sql_escapes_mysql_date_format_percent(self) -> None:
        sql = FinanceRepository._VISIBLE_ENTRY_FROM

        self.assertEqual(
            2,
            sql.count("DATE_FORMAT(t.business_date, '%%Y-%%m-01')"),
        )
        self.assertNotIn("DATE_FORMAT(t.business_date, '%Y-%m-01')", sql)

    def test_entry_filters_bind_enabled_sources_before_explicit_disabled_scope(self) -> None:
        repository = FinanceRepository(lambda: RouterConnection([]))
        query = FinanceQuery(
            "2026-01-01",
            "2026-01-31",
            platform=Platform.YUNDA,
            account_id="yunda_default",
        )

        clauses, params = repository._entry_filters(query)

        source_clause = clauses[1]
        self.assertEqual(
            len(enabled_finance_source_specs()),
            source_clause.count("(t.platform = %s AND t.account_id = %s)"),
        )
        self.assertEqual(
            [
                dt.date(2026, 1, 1),
                dt.date(2026, 1, 31),
                *self._enabled_source_params(),
                Platform.YUNDA.value,
                "yunda_default",
            ],
            params,
        )
        self.assertNotIn("yunda_default", self._enabled_source_params())

    def test_failed_source_and_freshness_sql_bind_enabled_sources_in_order(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []
        repository = FinanceRepository(lambda: RouterConnection(records))
        query = FinanceQuery("2026-01-01", "2026-01-31")

        repository._failed_sources(query)
        repository._freshness(query)

        failed_sql, failed_params = next(
            (sql, params)
            for sql, params in records
            if sql.startswith("SELECT r.platform, r.account_id, r.target_date")
        )
        self.assertIn("r.status = %s", failed_sql)
        self.assertEqual(
            (
                dt.date(2026, 1, 1),
                dt.date(2026, 1, 31),
                "failed",
                *self._enabled_source_params(),
            ),
            failed_params,
        )

        freshness_sql, freshness_params = next(
            (sql, params)
            for sql, params in records
            if "AS warning_runs" in sql
        )
        self.assertIn("status IN (%s, %s)", freshness_sql)
        self.assertEqual(
            (
                "warning",
                "success",
                "no_data",
                dt.date(2026, 1, 1),
                dt.date(2026, 1, 31),
                *self._enabled_source_params(),
            ),
            freshness_params,
        )

    def test_waybill_fact_queries_bind_enabled_sources_before_explicit_scope(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []
        repository = FinanceRepository(lambda: RouterConnection(records))

        result = repository.list_waybill_facts(
            start_date=dt.date(2026, 1, 1),
            end_date=dt.date(2026, 1, 31),
            platform="yunda",
            account_id="yunda_default",
            waybill_no="fictional-waybill",
            limit=25,
            offset=5,
        )

        self.assertEqual(0, result["total"])
        count_sql, count_params = records[0]
        data_sql, data_params = records[1]
        self.assertEqual(
            len(enabled_finance_source_specs()),
            count_sql.count("(f.platform = %s AND f.account_id = %s)"),
        )
        expected_scope = (
            dt.date(2026, 1, 1),
            dt.date(2026, 1, 31),
            *self._enabled_source_params(),
            "yunda",
            "yunda_default",
            "fictional-waybill",
        )
        self.assertEqual(expected_scope, count_params)
        self.assertEqual((*expected_scope, 25, 5), data_params)
        self.assertIn("LIMIT %s OFFSET %s", data_sql)
        self._assert_placeholder_counts(records)

    def test_review_and_anomaly_queries_bind_enabled_scope_in_order(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []
        repository = FinanceRepository(lambda: RouterConnection(records))

        repository.list_review_cases(status="open", limit=7, offset=2)
        repository.list_unnotified_anomalies(limit=37)

        review_count_sql, review_count_params = records[0]
        review_data_sql, review_data_params = records[1]
        self.assertIn("INNER JOIN finance_fee_items fi", review_count_sql)
        self.assertIn("fi.platform IN", review_count_sql)
        self.assertEqual((*enabled_finance_platforms(), "open"), review_count_params)
        self.assertEqual(
            (*enabled_finance_platforms(), "open", 7, 2),
            review_data_params,
        )
        self.assertIn("fi.platform IN", review_data_sql)

        anomaly_sql, anomaly_params = records[2]
        self.assertEqual(
            len(enabled_finance_source_specs()),
            anomaly_sql.count("(a.platform = %s AND a.account_id = %s)"),
        )
        self.assertEqual((*self._enabled_source_params(), 37), anomaly_params)
        self._assert_placeholder_counts(records)

    def test_knowledge_snapshot_version_and_items_share_enabled_platform_scope(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT COALESCE(MAX(log.id), 0) AS version_no"):
                return {"version_no": 11}
            return []

        result = FinanceRepository(
            lambda: RouterConnection(records, router)
        ).get_knowledge_snapshot()

        self.assertEqual(11, result["version_no"])
        self.assertEqual([], result["items"])
        self.assertEqual(2, len(records))
        for sql, params in records:
            self.assertIn("fi.platform IN", sql)
            self.assertEqual(enabled_finance_platforms(), params)
        self._assert_placeholder_counts(records)

    def test_disabled_fee_item_rebuild_is_rejected_before_mutation(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT id, platform FROM finance_fee_items"):
                return {"id": 19, "platform": "yunda"}
            return []

        repository = FinanceRepository(lambda: RouterConnection(records, router))

        with self.assertRaisesRegex(ValueError, "finance source is not enabled"):
            repository.rebuild_waybill_facts_for_fee_item(
                fee_item_id=19,
                reviewed_by="fictional-reviewer",
                review_reason="fictional review",
            )

        self.assertEqual(1, len(records))
        self.assertEqual((19,), records[0][1])
        self._assert_placeholder_counts(records)

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
                        "account_id": "price_default",
                        "login_account": "fictional-login",
                        "total_income": Decimal("10.005"),
                        "total_expense": Decimal("3.004"),
                        "waybill_cost": Decimal("2.005"),
                        "operating_cost": Decimal("0.999"),
                        "waybill_net": Decimal("-2.005"),
                        "operating_net": Decimal("-0.999"),
                    }
                ]
            if "MAX(COALESCE(r.login_account, '')) AS login_account" in sql:
                return [
                    {
                        "platform": "ronghui",
                        "account_id": "price_default",
                        "login_account": "fictional-login",
                    },
                    {
                        "platform": "ronghui",
                        "account_id": "ronghui_daxiang_s",
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
            if row["account_id"] == "ronghui_daxiang_s"
        )
        self.assertEqual("0.00", no_data_account["total_expense"])
        self.assertEqual("0.00", no_data_account["waybill_cost"])
        combined_sql = "\n".join(sql for sql, _ in records)
        self.assertIn("MAX(id) AS latest_run_id", combined_sql)
        self.assertIn("status IN ('success', 'no_data')", combined_sql)
        presence_params = next(
            params
            for sql, params in records
            if "MAX(COALESCE(r.login_account, '')) AS login_account" in sql
        )
        self.assertEqual(
            (
                "success",
                "no_data",
                dt.date(2026, 1, 1),
                dt.date(2026, 1, 31),
                *self._enabled_source_params(),
            ),
            presence_params,
        )

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

    def test_fee_mapping_response_filters_disabled_platform_and_binds_params_in_order(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT COUNT(*) AS total FROM finance_fee_items"):
                return {"total": 1}
            if "SELECT fi.id AS fee_item_id" in sql:
                return [
                    {
                        "fee_item_id": 1,
                        "platform": "ronghui",
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
        self.assertNotIn(Platform.YUNDA.value, result["booking_fee_items"])
        self.assertEqual(
            set(enabled_finance_platforms()),
            set(result["booking_fee_items"]),
        )
        count_params = next(
            params
            for sql, params in records
            if sql.startswith("SELECT COUNT(*) AS total FROM finance_fee_items")
        )
        self.assertEqual(enabled_finance_platforms(), count_params)
        data_params = next(
            params for sql, params in records if "SELECT fi.id AS fee_item_id" in sql
        )
        self.assertEqual(
            (
                "pending",
                "bound",
                dt.date(2026, 2, 1),
                dt.date(2026, 2, 1),
                *enabled_finance_platforms(),
                500,
                0,
            ),
            data_params,
        )

    def test_disabled_platform_fee_mapping_write_is_rejected_before_mutation(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT * FROM finance_fee_items"):
                return {
                    "id": 9,
                    "platform": "yunda",
                    "direction": "expense",
                }
            return []

        repository = FinanceRepository(lambda: RouterConnection(records, router))

        with self.assertRaisesRegex(ValueError, "finance source is not enabled"):
            repository.save_fee_mapping(
                fee_item_id=9,
                fee_level=FeeLevel.OPERATING,
                canonical_subject_name="Fictional operating subject",
                booking_fee_name="",
                effective_start_month="2026-07",
                include_in_cost=True,
                changed_by="fictional-admin",
                reason="fictional rejected mapping",
            )

        self.assertEqual(1, len(records))
        self.assertTrue(records[0][0].startswith("SELECT * FROM finance_fee_items"))

    def test_same_month_mapping_correction_supersedes_overlapping_active_version(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT id, platform FROM finance_fee_items"):
                return {"id": 9, "platform": "ronghui"}
            if sql.startswith("SELECT * FROM finance_fee_items"):
                return {
                    "id": 9,
                    "platform": "ronghui",
                    "direction": "expense",
                }
            if sql.startswith("SELECT id FROM finance_fee_subjects"):
                return {"id": 13}
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
            canonical_subject_name="Fictional operating subject",
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

    def test_commit_snapshot_rejects_disabled_source_before_any_business_write(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT * FROM finance_sync_runs"):
                return {
                    "id": 17,
                    "platform": "yunda",
                    "account_id": "yunda_default",
                    "login_account": "fictional-login",
                    "target_date": dt.date(2026, 1, 2),
                    "status": "running",
                }
            return []

        repository = FinanceRepository(lambda: RouterConnection(records, router))
        validation = type("Validation", (), {"passed": True})()

        with self.assertRaisesRegex(FinanceSnapshotRejectedError, "source is not enabled"):
            repository.commit_run_snapshot(
                run_id=17,
                transactions=(object(),),
                summaries=(),
                validation=validation,
            )

        self.assertEqual(1, len(records))
        self.assertTrue(records[0][0].startswith("SELECT * FROM finance_sync_runs"))

    def test_seed_mapping_scan_is_limited_to_enabled_platforms(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        seeded = FinanceRepository(lambda: RouterConnection(records)).seed_fee_mappings(
            seeds=()
        )

        self.assertEqual(0, seeded)
        seed_sql, seed_params = records[0]
        self.assertTrue(seed_sql.startswith("SELECT * FROM finance_fee_items WHERE"))
        self.assertIn("platform IN", seed_sql)
        self.assertEqual(enabled_finance_platforms(), seed_params)

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
            if sql.startswith("SELECT platform, raw_primary_fee_name"):
                return {
                    "platform": "ronghui",
                    "raw_primary_fee_name": "测试严格项目",
                    "raw_secondary_fee_name": "",
                }
            if sql.startswith("SELECT id FROM finance_fee_subjects"):
                return {"id": 13}
            if sql.startswith("SELECT id, direction, fee_level"):
                return {
                    "id": 7,
                    "direction": "expense",
                    "fee_level": "waybill",
                    "canonical_subject_id": 13,
                    "booking_fee_name": "操作费",
                    "requires_waybill": 1,
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
        self.assertEqual(dt.date(2026, 1, 1), insert_params[6])
        self.assertEqual(dt.date(2026, 6, 1), insert_params[7])

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
                        "account_id": "price_default",
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
            account_id="price_default",
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
            account_id="price_default",
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

    def test_historical_sync_batch_rows_retain_disabled_failed_source_details(self) -> None:
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
                    # MySQL may expose aggregate SUM/COUNT values as DECIMAL.
                    "total_runs": Decimal("4.0000"),
                    "success_runs": Decimal("3.0000"),
                    "failed_runs": Decimal("1.0000"),
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
        self.assertEqual(4, result["items"][0]["total_runs"])
        self.assertEqual(3, result["items"][0]["success_runs"])
        self.assertEqual(1, result["items"][0]["failed_runs"])
        self.assertEqual(1, len(result["items"][0]["failed_sources"]))
        failure = result["items"][0]["failed_sources"][0]
        self.assertEqual("fictional-yunda-role", failure["account_id"])
        self.assertEqual("FIELD_DRIFT", failure["error_code"])
        self.assertEqual((), records[0][1])
        self.assertEqual(("success", "no_data", "failed", 100, 0), records[1][1])
        self.assertEqual((21, "failed"), records[2][1])
        self.assertNotIn("account_id = %s", records[1][0])
        self._assert_placeholder_counts(records)

    def test_sync_batch_rows_reject_fractional_aggregate_counts(self) -> None:
        records: list[tuple[str, tuple[Any, ...]]] = []

        def router(sql: str, _params: tuple[Any, ...]):
            if sql.startswith("SELECT COUNT(*) AS total FROM finance_sync_batches"):
                return {"total": Decimal("1.0000")}
            if "COUNT(r.id) AS total_runs" in sql:
                return {
                    "id": 22,
                    "total_runs": Decimal("1.5000"),
                    "success_runs": Decimal("1.0000"),
                    "failed_runs": Decimal("0.0000"),
                }
            return []

        repository = FinanceRepository(lambda: RouterConnection(records, router))
        with self.assertRaisesRegex(ValueError, "total_runs must be an integer count"):
            repository.list_sync_batches()


if __name__ == "__main__":
    unittest.main()
