from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from agent.business_query import BusinessFinanceQueryService, BusinessQueryError
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OperationType,
    OrchestrationError,
    PlanStep,
    RiskLevel,
)
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.result_verifier import ResultVerifier
from agent.tool_registry import ToolRegistry


class _FinanceRepository:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.queries = []

    def get_business_summary(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return deepcopy(self.result)


def _verified_summary(**overrides):
    result = {
        "entry_count": 2,
        "total_income": "10.01",
        "total_expense": "3.00",
        "net_change": "7.00",
        "pending_fee_items": 1,
        "latest_success_at": "2026-01-31 00:10:00",
        "data_through_date": "2026-01-31",
        "validation_status": "passed",
        "coverage_status": "complete",
        "reconciliation_status": "passed",
        "failed_sources": [],
        "accounts": [{"account_id": "must-not-leak"}],
    }
    result.update(overrides)
    return result


class BusinessFinanceQueryTests(unittest.TestCase):
    def _service(self, result=None, error=None):
        repository = _FinanceRepository(
            result if result is not None else _verified_summary(),
            error,
        )
        return BusinessFinanceQueryService(
            repository,
            enabled_platforms=("ronghui",),
        ), repository

    def test_returns_only_reviewed_aggregate_fields_and_accepts_rounded_display_values(self):
        service, repository = self._service()

        result = service.run(
            {
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
                "platform": "ronghui",
            }
        )

        self.assertEqual(result["availability"], "DATA")
        self.assertEqual(
            result["summary"],
            {
                "total_income": "10.01",
                "total_expense": "3.00",
                "net_change": "7.00",
                "pending_fee_items": 1,
            },
        )
        self.assertEqual(result["record_count"], 2)
        self.assertNotIn("accounts", result)
        self.assertIn("不得把净变动解释为利润", result["warnings"][0])
        self.assertEqual(repository.queries[0].platform.value, "ronghui")

    def test_verified_empty_ledger_is_explicit_no_data_without_zero_amounts(self):
        service, _ = self._service(
            _verified_summary(
                entry_count=0,
                total_income="0.00",
                total_expense="0.00",
                net_change="0.00",
                pending_fee_items=0,
            )
        )

        result = service.run({"start_date": "2026-01-01", "end_date": "2026-01-31"})

        self.assertEqual(result["availability"], "NO_DATA")
        self.assertEqual(result["summary"], {})
        self.assertEqual(result["record_count"], 0)

    def test_query_boundary_allows_366_calendar_days_and_rejects_367(self):
        service, repository = self._service(
            _verified_summary(data_through_date="2024-12-31")
        )
        service.run({"start_date": "2024-01-01", "end_date": "2024-12-31"})
        self.assertEqual(len(repository.queries), 1)

        with self.assertRaises(BusinessQueryError) as caught:
            service.run({"start_date": "2024-01-01", "end_date": "2025-01-01"})
        self.assertEqual(caught.exception.code, "BUSINESS_QUERY_INVALID")
        self.assertEqual(len(repository.queries), 1)

    def test_rejects_unknown_fields_non_string_and_disabled_platforms(self):
        service, repository = self._service()
        invalid_arguments = (
            {"start_date": "2026-01-01", "end_date": "2026-01-31", "sql": "SELECT 1"},
            {"start_date": "2026-01-01", "end_date": "2026-01-31", "platform": 1},
            {"start_date": "2026-01-01", "end_date": "2026-01-31", "platform": "yunda"},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(BusinessQueryError):
                service.run(arguments)
        self.assertEqual(repository.queries, [])

    def test_fails_closed_for_unverified_incomplete_and_unreconciled_results(self):
        cases = (
            (_verified_summary(validation_status="warning"), "BUSINESS_QUERY_DATA_UNVERIFIED"),
            (_verified_summary(failed_sources=[{"source": "x"}]), "BUSINESS_QUERY_DATA_UNVERIFIED"),
            (_verified_summary(coverage_status="incomplete"), "BUSINESS_QUERY_DATA_INCOMPLETE"),
            (
                _verified_summary(reconciliation_status="failed"),
                "BUSINESS_QUERY_RECONCILIATION_FAILED",
            ),
            (_verified_summary(data_through_date="2026-01-30"), "BUSINESS_QUERY_DATA_INCOMPLETE"),
        )
        for raw, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                service, _ = self._service(raw)
                with self.assertRaises(BusinessQueryError) as caught:
                    service.run({"start_date": "2026-01-01", "end_date": "2026-01-31"})
                self.assertEqual(caught.exception.code, expected_code)

    def test_repository_failure_uses_fixed_non_sensitive_error(self):
        service, _ = self._service(error=RuntimeError("synthetic-secret-value"))

        with self.assertRaises(BusinessQueryError) as caught:
            service.run({"start_date": "2026-01-01", "end_date": "2026-01-31"})

        self.assertEqual(caught.exception.code, "BUSINESS_QUERY_UNAVAILABLE")
        self.assertNotIn("synthetic-secret-value", str(caught.exception))

    @staticmethod
    def _plan_step(*, account_id=None) -> PlanStep:
        return PlanStep(
            step_key="business-finance-query",
            tool_name="query_business_finance",
            tool_version="1.0.0",
            operation_type=OperationType.READ,
            arguments={"start_date": "2026-01-01", "end_date": "2026-01-31"},
            account_id=account_id,
            depends_on=(),
            idempotency_key="business-finance-query-1",
            expected_evidence=(),
            postconditions=({"name": "authoritative_result_returned"},),
            risk_level=RiskLevel.LOW,
            requires_approval=False,
        )

    def test_control_plane_normalizes_and_verifies_direct_runner_result(self):
        service, _ = self._service()
        catalog = ToolRegistry()
        adapter = RegisteredToolExecutionAdapter(
            catalog=catalog,
            executor=object(),
            direct_runners={"query_business_finance": service.run},
        )
        step = self._plan_step()

        result = asyncio.run(
            adapter.execute_step(
                step,
                run_id="run-business-query",
                step_id="step-business-query",
                execution_context={"source": "console"},
            )
        )
        outcome = ResultVerifier().verify(
            step,
            result,
            catalog.get_capability("query_business_finance"),
        )

        self.assertEqual(result["status"], "SUCCESS")
        self.assertTrue(outcome.accepted)
        self.assertEqual(result["meta"]["record_count"], 2)
        self.assertTrue(result["meta"]["evidence_refs"])

    def test_control_plane_preserves_reviewed_business_error_code(self):
        service, _ = self._service(_verified_summary(coverage_status="incomplete"))
        catalog = ToolRegistry()
        adapter = RegisteredToolExecutionAdapter(
            catalog=catalog,
            executor=object(),
            direct_runners={"query_business_finance": service.run},
        )

        result = asyncio.run(
            adapter.execute_step(
                self._plan_step(),
                run_id="run-business-query",
                step_id="step-business-query",
                execution_context={"source": "console"},
            )
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"]["code"], "BUSINESS_QUERY_DATA_INCOMPLETE")

    def test_control_plane_rejects_any_account_binding_for_business_query(self):
        catalog = ToolRegistry()
        context = ContextSnapshot(values={}, account_ids=("rogue-account",))
        command = Command(
            command_type="tool.execute",
            source="console",
            actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1", roles=("admin",)),
            parameters={
                "tool_name": "query_business_finance",
                "arguments": {
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-31",
                },
                "account_id": "rogue-account",
            },
            idempotency_key="business-query-account-binding",
        )
        plan = DeterministicPlanner(catalog).plan(command, context)

        with self.assertRaises(OrchestrationError) as caught:
            PlanValidator(catalog).validate(plan, context)
        self.assertEqual(caught.exception.code, "ACCOUNT_SCOPE_MISMATCH")

        service, _ = self._service()
        adapter = RegisteredToolExecutionAdapter(
            catalog=catalog,
            executor=object(),
            direct_runners={"query_business_finance": service.run},
        )
        forged_step = self._plan_step(account_id="rogue-account")
        result = asyncio.run(
            adapter.execute_step(
                forged_step,
                run_id="run-business-query",
                step_id="step-business-query",
                execution_context={"source": "console"},
            )
        )
        outcome = ResultVerifier().verify(
            forged_step,
            result,
            catalog.get_capability("query_business_finance"),
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.code, "ACCOUNT_SCOPE_MISMATCH")

    def test_standalone_executor_is_fixed_fail_closed_placeholder(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "business_finance_query_tool.py"

        completed = subprocess.run(
            [sys.executable, str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(
            payload["error"]["code"],
            "BUSINESS_QUERY_COMPOSITION_REQUIRED",
        )


if __name__ == "__main__":
    unittest.main()
