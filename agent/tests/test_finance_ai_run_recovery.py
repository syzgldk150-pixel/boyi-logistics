from __future__ import annotations

import datetime as dt
import unittest

from shared.finance import FinanceRepository


class _Cursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._rows = []
        self.description = None

    def execute(self, sql, params):
        normalized = " ".join(str(sql).split())
        self.rowcount = 0
        self._rows = []
        if normalized.startswith("SELECT rc.id AS review_case_id"):
            cutoff = params[0]
            for case_id, case in self.state["cases"].items():
                run = self.state["runs"].get(case["current_ai_run_id"])
                if (
                    case["status"] == "open"
                    and case["ai_status"] == "running"
                    and run is not None
                    and run["status"] == "running"
                    and run["started_at"] < cutoff
                ):
                    self._rows.append(
                        {"review_case_id": case_id, "ai_run_id": run["id"]}
                    )
            return
        if "error_code = 'FINANCE_AI_RUN_INTERRUPTED'" in normalized:
            finished_at, ai_run_id, review_case_id = params
            run = self.state["runs"].get(ai_run_id)
            if (
                run is not None
                and run["review_case_id"] == review_case_id
                and run["status"] == "running"
            ):
                run.update(
                    status="failed",
                    error_code="FINANCE_AI_RUN_INTERRUPTED",
                    finished_at=finished_at,
                )
                self.rowcount = 1
            return
        if "SET ai_status = 'failed'" in normalized:
            _updated_at, review_case_id, ai_run_id = params
            case = self.state["cases"].get(review_case_id)
            if (
                case is not None
                and case["status"] == "open"
                and case["ai_status"] == "running"
                and case["current_ai_run_id"] == ai_run_id
            ):
                case["ai_status"] = "failed"
                self.rowcount = 1
            return
        if normalized.startswith("UPDATE finance_review_ai_runs SET status = %s"):
            status, suggestion, error_code, error_message, finished_at, ai_run_id, review_case_id = params
            run = self.state["runs"].get(ai_run_id)
            if (
                run is not None
                and run["review_case_id"] == review_case_id
                and run["status"] == "running"
            ):
                run.update(
                    status=status,
                    suggestion=suggestion,
                    error_code=error_code,
                    error_message=error_message,
                    finished_at=finished_at,
                )
                self.rowcount = 1
            return
        if normalized.startswith("UPDATE finance_review_cases SET ai_status = %s"):
            status, _updated_at, review_case_id, ai_run_id = params
            case = self.state["cases"].get(review_case_id)
            if (
                case is not None
                and case["ai_status"] == "running"
                and case["current_ai_run_id"] == ai_run_id
            ):
                case["ai_status"] = status
                self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


class _Connection:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _Cursor(self.state)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class FinanceAiRunRecoveryTests(unittest.TestCase):
    def test_stale_current_run_is_recoverable_and_late_result_cannot_overwrite(self):
        state = {
            "cases": {
                11: {
                    "status": "open",
                    "ai_status": "running",
                    "current_ai_run_id": 21,
                }
            },
            "runs": {
                21: {
                    "id": 21,
                    "review_case_id": 11,
                    "status": "running",
                    "started_at": dt.datetime(2026, 8, 29, 10, 0),
                }
            },
        }
        repository = FinanceRepository(lambda: _Connection(state))

        recovered = repository.recover_interrupted_review_ai_runs(
            stale_before=dt.datetime(2026, 8, 29, 10, 30)
        )

        self.assertEqual(1, recovered)
        self.assertEqual("failed", state["cases"][11]["ai_status"])
        self.assertEqual("FINANCE_AI_RUN_INTERRUPTED", state["runs"][21]["error_code"])

        state["runs"][22] = {
            "id": 22,
            "review_case_id": 11,
            "status": "running",
            "started_at": dt.datetime(2026, 8, 29, 11, 0),
        }
        state["cases"][11].update(ai_status="running", current_ai_run_id=22)

        late_applied = repository.finish_review_ai_run(
            review_case_id=11,
            ai_run_id=21,
            suggestion={"fee_level": "waybill"},
        )
        current_applied = repository.finish_review_ai_run(
            review_case_id=11,
            ai_run_id=22,
            suggestion={"fee_level": "operating"},
        )

        self.assertFalse(late_applied)
        self.assertTrue(current_applied)
        self.assertEqual("failed", state["runs"][21]["status"])
        self.assertEqual("success", state["runs"][22]["status"])
        self.assertEqual("success", state["cases"][11]["ai_status"])
        self.assertEqual(22, state["cases"][11]["current_ai_run_id"])


if __name__ == "__main__":
    unittest.main()
