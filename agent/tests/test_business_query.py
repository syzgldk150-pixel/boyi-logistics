from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

from agent.business_query import BusinessFinanceQueryService, BusinessQueryError
from agent.core import AgentCore
from agent.direct_tool_router import (
    business_finance_request_from_text,
    format_business_finance_reply,
)
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


def _finance_payload(**overrides):
    payload = {
        "query_type": "finance_summary",
        "availability": "DATA",
        "period": {"start_date": "2026-08-01", "end_date": "2026-08-25"},
        "summary": {
            "total_income": "10.01",
            "total_expense": "3.00",
            "net_change": "7.01",
            "pending_fee_items": 1,
        },
        "source": {
            "name": "shared_finance_ledger",
            "platform": "all_enabled",
            "validation_status": "passed",
            "data_through_date": "2026-08-25",
            "latest_success_at": "2026-08-25 00:10:00",
        },
        "record_count": 2,
        "warnings": ["存在未分类费用项目。"],
    }
    payload.update(overrides)
    return payload


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

    def test_natural_language_periods_are_resolved_by_code(self):
        today = dt.date(2026, 8, 25)
        cases = (
            ("今天财务收入", {"start_date": "2026-08-25", "end_date": "2026-08-25"}),
            ("昨天支出多少", {"start_date": "2026-08-24", "end_date": "2026-08-24"}),
            ("查本月收支", {"start_date": "2026-08-01", "end_date": "2026-08-25"}),
            ("上个月财务汇总", {"start_date": "2026-07-01", "end_date": "2026-07-31"}),
            ("近7天账本", {"start_date": "2026-08-19", "end_date": "2026-08-25"}),
            (
                "查询融辉 2026-08-01 到 2026-08-20 财务",
                {
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-20",
                    "platform": "ronghui",
                },
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    business_finance_request_from_text(text, today=today),
                    {"params": expected},
                )

    def test_natural_language_finance_ambiguity_and_writes_fail_closed(self):
        today = dt.date(2026, 8, 25)
        cases = (
            "查财务",
            "今天和昨天的收入",
            "2026-08-01 和 2026-08-20 的收入",
            "2026-08-20 到 2026-08-01 财务",
            "2026-08-01 到 2026-08-20 和 2026-07 财务",
            "今天和近1天财务",
            "今天和今天财务",
            "今年到今天财务",
            "最近到今天财务",
            "截至今天财务",
            "近367天财务",
            "同步昨天财务",
            "查询今天财务利润",
            "查韵达财务今天收入",
            "查融辉和韵达今天财务",
            "中通今天财务",
            "顺丰昨天收入",
            "查询德邦平台今天财务",
        )
        for text in cases:
            with self.subTest(text=text):
                result = business_finance_request_from_text(text, today=today)
                self.assertIsNotNone(result)
                self.assertIn("reply", result)
                self.assertNotIn("params", result)
        self.assertIsNone(business_finance_request_from_text("查运单 R123456789", today=today))

        year_boundary = business_finance_request_from_text(
            "昨天财务",
            today=dt.date(2026, 1, 1),
        )
        self.assertEqual(
            year_boundary,
            {"params": {"start_date": "2025-12-31", "end_date": "2025-12-31"}},
        )
        self.assertIn(
            "reply",
            business_finance_request_from_text(
                "近7天和近30天财务",
                today=today,
            ),
        )

    def test_finance_formatter_uses_only_validated_tool_owned_values(self):
        reply = format_business_finance_reply(
            {"success": True, "data": _finance_payload()}
        )

        self.assertIn("财务汇总（2026-08-01 至 2026-08-25）", reply)
        self.assertIn("总收入：¥10.01", reply)
        self.assertIn("总支出：¥3.00", reply)
        self.assertIn("净变动：¥7.01", reply)
        self.assertIn("净变动不能解释为利润", reply)

    def test_finance_formatter_distinguishes_verified_no_data_without_zero_amounts(self):
        payload = _finance_payload(
            availability="NO_DATA",
            summary={},
            record_count=0,
            warnings=["所选期间没有交易记录。"],
        )

        reply = format_business_finance_reply({"success": True, "data": payload})

        self.assertIn("已验证财务账本在该期间没有交易记录", reply)
        self.assertNotIn("¥", reply)
        self.assertNotIn("0.00", reply)

    def test_finance_formatter_never_leaks_failures_or_malformed_amounts(self):
        failures = (
            {"success": False, "error_code": "BUSINESS_QUERY_DATA_INCOMPLETE", "error": "secret 9.99"},
            {"success": False, "error_code": "UNKNOWN", "error": "secret 8.88"},
            {
                "success": True,
                "data": _finance_payload(
                    summary={
                        "total_income": "synthetic 7.77",
                        "total_expense": "3.00",
                        "net_change": "4.77",
                        "pending_fee_items": 0,
                    }
                ),
            },
        )
        for result in failures:
            with self.subTest(result=result):
                reply = format_business_finance_reply(result)
                self.assertIn("暂不提供金额", reply)
                self.assertNotIn("secret", reply)
                self.assertNotIn("7.77", reply)
                self.assertNotIn("8.88", reply)
                self.assertNotIn("9.99", reply)

    def test_agent_finance_route_bypasses_llm_and_uses_trusted_today(self):
        class _Memory:
            def get_or_create_conversation(self, _user_id, conversation_id):
                return conversation_id or "conv-finance"

            def get_recent_messages(self, *_args, **_kwargs):
                return []

            def search_knowledge(self, *_args, **_kwargs):
                return []

            def save_message(self, *_args, **_kwargs):
                return 1

        class _NoLLM:
            async def chat(self, *_args, **_kwargs):
                raise AssertionError("finance natural language must bypass the LLM")

        core = AgentCore(today_provider=lambda: dt.date(2026, 8, 25))
        core.memory = _Memory()
        core.llm = _NoLLM()
        core.execute_tool = AsyncMock(
            return_value={"success": True, "data": _finance_payload()}
        )
        actor = Actor(
            ActorType.FEISHU_USER,
            "bound-admin",
            roles=("admin",),
            authenticated_by="feishu_admin_binding",
        )

        result = asyncio.run(
            core.handle_message(
                "查本月财务",
                user_id="bound-admin",
                actor=actor,
                source="feishu",
                request_id="event-finance-1",
            )
        )

        self.assertEqual(
            result["executed_tools"][0]["params"],
            {"start_date": "2026-08-01", "end_date": "2026-08-25"},
        )
        self.assertIn("总收入：¥10.01", result["reply"])
        core.execute_tool.assert_awaited_once()

    def test_agent_finance_route_requires_bound_admin_role(self):
        class _Memory:
            def get_or_create_conversation(self, _user_id, conversation_id):
                return conversation_id or "conv-finance"

            def get_recent_messages(self, *_args, **_kwargs):
                return []

            def search_knowledge(self, *_args, **_kwargs):
                return []

            def save_message(self, *_args, **_kwargs):
                return 1

        core = AgentCore(today_provider=lambda: dt.date(2026, 8, 25))
        core.memory = _Memory()
        core.execute_tool = AsyncMock()
        actors = (
            (
                Actor(
                    ActorType.FEISHU_USER,
                    "unbound-user",
                    roles=(),
                    authenticated_by="feishu_event",
                ),
                "feishu",
            ),
            (
                Actor(
                    ActorType.FEISHU_USER,
                    "forged-admin-role",
                    roles=("admin",),
                    authenticated_by="feishu_verified_event",
                ),
                "feishu",
            ),
            (
                Actor(
                    ActorType.CONSOLE_ADMIN,
                    "console-admin",
                    roles=("admin",),
                    authenticated_by="mysql_admin_session",
                ),
                "console",
            ),
        )
        for actor, source in actors:
            with self.subTest(actor=actor.actor_id):
                result = asyncio.run(
                    core.handle_message(
                        "今天财务",
                        user_id=actor.actor_id,
                        actor=actor,
                        source=source,
                    )
                )
                self.assertIn("没有财务查询权限", result["reply"])
                self.assertEqual(result["executed_tools"], [])
        core.execute_tool.assert_not_awaited()

    def test_finance_capability_remains_hidden_from_llm_catalog(self):
        catalog = ToolRegistry()
        exposed_names = {
            tool["function"]["name"] for tool in catalog.get_openai_tools()
        }

        self.assertNotIn("query_business_finance", exposed_names)


if __name__ == "__main__":
    unittest.main()
