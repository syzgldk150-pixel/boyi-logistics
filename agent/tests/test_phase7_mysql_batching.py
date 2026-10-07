"""Remote-database round trips: batched snapshot writes and one schema check."""

import unittest
from unittest.mock import patch

from pymysql.cursors import RE_INSERT_VALUES

from shared.scan_snapshot_recovery import SNAPSHOT_INITIAL_SEEN_COUNT, SNAPSHOT_UPSERT_SQL
from tools import phase7_mysql_store as store


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def fetchall(self):
        return self.rows.pop(0)


class FakeConnection:
    def __init__(self, rows):
        self.cursor_value = FakeCursor(rows)

    def cursor(self):
        return self.cursor_value

    def close(self):
        pass


def _migrated_rows():
    return [
        [{"TABLE_NAME": name} for name in ("waybill_data", "split_pending_problem_items", "scan_codes")],
        [
            {"TABLE_NAME": "split_pending_problem_items", "COLUMN_NAME": "complaint_status"},
            {"TABLE_NAME": "split_pending_problem_items", "COLUMN_NAME": "complaint_error_summary"},
            {"TABLE_NAME": "split_pending_problem_items", "COLUMN_NAME": "complaint_processed_at"},
            {"TABLE_NAME": "scan_codes", "COLUMN_NAME": "main_tracking"},
            {"TABLE_NAME": "scan_codes", "COLUMN_NAME": "snapshot_date"},
        ],
    ]


class BatchedInsertSqlTests(unittest.TestCase):
    def test_snapshot_and_split_pending_upserts_are_sent_as_one_multi_row_insert(self):
        # A literal inside VALUES makes PyMySQL execute one statement per row.
        for sql, parameter_count in (
            (SNAPSHOT_UPSERT_SQL, 7),
            (store._SPLIT_PENDING_UPSERT_SQL, 16),
        ):
            with self.subTest(sql=sql.split("(")[0].strip()):
                match = RE_INSERT_VALUES.match(sql)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(2).count("%s"), parameter_count)
        self.assertEqual(SNAPSHOT_INITIAL_SEEN_COUNT, 1)

    def test_split_pending_refresh_passes_every_column_as_a_parameter(self):
        cursor_calls = []

        class Cursor:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def executemany(self, sql, params):
                cursor_calls.append((sql, list(params)))

            def execute(self, sql, params=None):
                self.rowcount = 0

        class Connection:
            def begin(self):
                pass

            def cursor(self):
                return Cursor()

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        record = {
            "tracking_number": "R12345678901",
            "expected_quantity": 3,
            "arrived_quantity": 1,
            "pending_quantity": 2,
            "problem_type": "少货/分批",
            "problem_owner_type": "交接异常",
            "problem_cause": "应到3件 实际到1件",
        }
        with patch.object(store, "ensure_phase7_tables"), patch.object(store, "_connect", return_value=Connection()):
            store.replace_split_pending_problem_items([record])
        sql, params = cursor_calls[0]
        self.assertIs(sql, store._SPLIT_PENDING_UPSERT_SQL)
        self.assertEqual(len(params[0]), 16)
        self.assertEqual(params[0][9:15], ("pending", None, None, "not_applicable", None, None))


class SchemaValidationCacheTests(unittest.TestCase):
    def setUp(self):
        store._PHASE7_SCHEMA_VALIDATED.clear()
        self.addCleanup(store._PHASE7_SCHEMA_VALIDATED.clear)

    def test_successful_validation_runs_once_per_connector_and_target(self):
        opened = []

        def connect():
            opened.append(1)
            return FakeConnection(_migrated_rows())

        with patch.object(store, "_connect", connect):
            store.ensure_phase7_tables()
            store.ensure_phase7_tables()
        self.assertEqual(len(opened), 1)

        def other_connect():
            opened.append(2)
            return FakeConnection(_migrated_rows())

        with patch.object(store, "_connect", other_connect):
            store.ensure_phase7_tables()
        self.assertEqual(opened, [1, 2])

    def test_failed_validation_is_never_remembered(self):
        opened = []

        def connect():
            opened.append(1)
            return FakeConnection([[{"TABLE_NAME": "waybill_data"}]])

        with patch.object(store, "_connect", connect):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "not migrated"):
                    store.ensure_phase7_tables()
        self.assertEqual(len(opened), 2)


if __name__ == "__main__":
    unittest.main()
