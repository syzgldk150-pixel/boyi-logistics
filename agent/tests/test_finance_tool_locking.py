from __future__ import annotations

import contextlib
import unittest

from tools.finance_sync_service import FinanceSyncError
from tools.sync_finance_bills_tool import _database_lock, run_sync_finance_bills


class _Cursor:
    def __init__(self, acquired):
        self.acquired = acquired
        self.calls = []
        self.closed = False

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return (self.acquired,)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self, acquired):
        self.cursor_value = _Cursor(acquired)
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


class FinanceToolLockingTests(unittest.TestCase):
    def test_mysql_advisory_lock_is_held_and_released(self):
        connection = _Connection(1)
        with _database_lock(lambda: connection):
            self.assertIn("GET_LOCK", connection.cursor_value.calls[0][0])
            self.assertEqual(("shipnow.finance.sync",), connection.cursor_value.calls[0][1])
        self.assertIn("RELEASE_LOCK", connection.cursor_value.calls[-1][0])
        self.assertTrue(connection.cursor_value.closed)
        self.assertTrue(connection.closed)

    def test_busy_mysql_lock_fails_explicitly(self):
        connection = _Connection(0)
        with self.assertRaises(FinanceSyncError) as caught:
            with _database_lock(lambda: connection):
                pass
        self.assertEqual("FINANCE_SYNC_ALREADY_RUNNING", caught.exception.code)

    def test_unknown_exception_is_not_returned_to_api(self):
        @contextlib.contextmanager
        def unsafe_failure():
            raise RuntimeError("https://example.invalid/sso?token=fixture-secret")
            yield

        result = run_sync_finance_bills({}, lock_context=unsafe_failure)
        self.assertEqual("FINANCE_SYNC_INTERNAL", result["error_code"])
        self.assertEqual("lock_setup", result["diagnostic_stage"])
        self.assertEqual("RuntimeError", result["diagnostic_type"])
        self.assertIn("unsafe_failure", result["diagnostic_trace"])
        self.assertNotIn("token", result["error"].lower())
        self.assertNotIn("fixture-secret", result["error"])
        self.assertNotIn("fixture-secret", result["diagnostic_trace"])


if __name__ == "__main__":
    unittest.main()
