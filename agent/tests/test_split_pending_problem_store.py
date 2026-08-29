import unittest
from unittest.mock import patch

from tools import phase7_mysql_store as store


class FakeCursor:
    def __init__(self, rows=None):
        self.calls = []
        self.executemany_calls = []
        self.rowcount = 1
        self.rows = list(rows or [])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.rowcount = 1

    def executemany(self, sql, params):
        values = list(params)
        self.executemany_calls.append((sql, values))
        self.rowcount = len(values)

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def begin(self):
        self.begin_count += 1

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def record(bill_code="R1", problem_type="少货/分批"):
    arrived = 4 if problem_type == "少货/分批" else 0
    return {
        "bill_code": bill_code,
        "source_row_no": 2,
        "destination_station": "邵阳操作场",
        "expected_quantity": 10,
        "arrived_quantity": arrived,
        "pending_quantity": 10 - arrived,
        "problem_type": problem_type,
        "problem_owner_type": "交接异常" if arrived else "通知类（不顺延时效）",
        "problem_cause": f"应到10件 实际到{arrived}件" if arrived else "有发未到",
    }


class SplitPendingProblemStoreTests(unittest.TestCase):
    def test_snapshot_upsert_preserves_problem_step_and_disables_complaint_step(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        with patch.object(store, "ensure_phase7_tables"), patch.object(
            store, "_connect", return_value=connection
        ):
            result = store.replace_split_pending_problem_items(
                [record("R_PART"), record("R_ZERO", "有发未到")]
            )
        self.assertEqual(2, result["upserted"])
        sql, values = cursor.executemany_calls[0]
        self.assertIn("WHEN problem_type = VALUES(problem_type) THEN upload_status", sql)
        self.assertIn("complaint_status = 'not_applicable'", sql)
        self.assertNotIn("WHEN VALUES(problem_type) = '少货/分批' THEN 'pending'", sql)
        self.assertEqual("not_applicable", values[0][-2])
        self.assertEqual("not_applicable", values[1][-2])
        self.assertEqual(1, connection.commit_count)

    def test_combined_result_updates_only_problem_steps(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        with patch.object(store, "ensure_phase7_tables"), patch.object(
            store, "_connect", return_value=connection
        ):
            result = store.update_split_pending_combined_results(
                [
                    {
                        "bill_code": "R1",
                        "complaint": None,
                        "problem_item": {"status": "failed", "error": "保存失败"},
                    },
                    {
                        "bill_code": "R2",
                        "complaint": None,
                        "problem_item": {"status": "success"},
                    },
                ]
            )
        self.assertEqual(2, result["updated"])
        self.assertEqual(2, len(cursor.calls))
        self.assertEqual("failed", cursor.calls[0][1][0])
        self.assertEqual("success", cursor.calls[1][1][0])
        self.assertIn("complaint_status = 'not_applicable'", cursor.calls[0][0])

    def test_complaint_result_is_rejected_and_rolls_back(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        with patch.object(store, "ensure_phase7_tables"), patch.object(
            store, "_connect", return_value=connection
        ), self.assertRaisesRegex(ValueError, "不再支持 complaint 结果"):
            store.update_split_pending_combined_results(
                [{"bill_code": "R1", "complaint": {"status": "success"}}]
            )
        self.assertEqual(1, connection.rollback_count)


if __name__ == "__main__":
    unittest.main()
