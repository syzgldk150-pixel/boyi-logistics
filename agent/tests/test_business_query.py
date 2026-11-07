from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from agent.business_query import (
    AutomationOperationsQueryService,
    BusinessFinanceQueryService,
    BusinessQueryError,
    MySQLAutomationOperationsRepository,
    _china_day_bounds,
)
from agent.core import AgentCore
from agent.direct_readers import invoke_registered_reader
from agent.direct_tool_router import (
    business_finance_request_from_text,
    business_operations_request_from_text,
    format_automation_operations_reply,
    format_business_operations_summary_reply,
    format_business_finance_reply,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    OperationType,
    OrchestrationError,
    PlanStep,
    RiskLevel,
)
from agent.orchestration.result_verifier import ResultVerifier
from agent.tool_registry import ToolRegistry, validate_schema_instance


class _OperationsRepository:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result if result is not None else {
            "invocation_status_counts": {"COMPLETED": 2, "FAILED": 1, "WRITE_OUTCOME_UNKNOWN": 1, "RUNNING": 1},
            "freshness": {
                "latest_invocation_started_at": "2026-08-25 10:00:00",
                "latest_invocation_updated_at": "2026-08-25 10:01:00",
            },
        }
        self.error = error
        self.queries = []

    def get_operations_summary(self, *, start_date, end_date):
        self.queries.append((start_date, end_date))
        if self.error is not None:
            raise self.error
        return deepcopy(self.result)


class _Ledger:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result if result is not None else {
            "start_date": "2026-08-01", "end_date": "2026-08-25", "voucher_count": 3, "revenue": "300.0000",
            "cost": "120.0000", "expense": "30.0050", "other_income": "0.0000", "profit": "149.9950",
            "closed_through": "2026-07", "warnings": ["所查期间尚未全部封账，金额可能变动"],
        }
        self.error = error
        self.queries = []

    def ledger_summary(self, *, start_date, end_date):
        self.queries.append((start_date, end_date))
        if self.error is not None:
            raise self.error
        return deepcopy(self.result)


def _finance_payload(**overrides):
    payload = {
        "query_type": "ledger_summary",
        "availability": "DATA",
        "period": {"start_date": "2026-08-01", "end_date": "2026-08-25"},
        "summary": {"revenue": "300.00", "cost": "120.00", "expense": "30.01", "other_income": "0.00", "profit": "149.99"},
        "voucher_count": 3,
        "closed_through": "2026-07",
        "warnings": ["所查期间尚未全部封账，金额可能变动"],
    }
    payload.update(overrides)
    return payload


class AutomationOperationsQueryTests(unittest.TestCase):
    def test_china_business_day_uses_utc_naive_mysql_bounds(self):
        start, end = _china_day_bounds(dt.date(2026, 8, 25), dt.date(2026, 8, 25))
        self.assertEqual(start, dt.datetime(2026, 8, 24, 16, 0))
        self.assertEqual(end, dt.datetime(2026, 8, 25, 16, 0))

    def test_mysql_operations_aggregates_share_one_read_only_snapshot(self):
        class Cursor:
            description = (("status",), ("count",))

            def __init__(self, connection): self.connection = connection
            def execute(self, statement, params=()): self.connection.calls.append((statement, params))
            def fetchall(self):
                statement = self.connection.calls[-1][0]
                if "automation_plugin_invocations" in statement and "GROUP BY" in statement: return [{"status": "COMPLETED", "count": 1}]
                return [{"latest_invocation_started_at": dt.datetime(2026, 8, 25, 9), "latest_invocation_updated_at": dt.datetime(2026, 8, 25, 10)}]
            def close(self): pass

        class Connection:
            def __init__(self): self.calls = []
            def cursor(self): return Cursor(self)
            def close(self): pass

        connection = Connection()
        raw = MySQLAutomationOperationsRepository(lambda: connection).get_operations_summary(
            start_date=dt.date(2026, 8, 25), end_date=dt.date(2026, 8, 25)
        )
        self.assertEqual(connection.calls[0][0], "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        self.assertEqual(connection.calls[1][1], (dt.datetime(2026, 8, 24, 16), dt.datetime(2026, 8, 25, 16)))
        self.assertEqual(raw["invocation_status_counts"], {"COMPLETED": 1})
        self.assertEqual(len(connection.calls), 3)
        self.assertTrue(all("automation_plugin_invocations" in statement for statement, _ in connection.calls[1:]))

    def test_closed_date_range_returns_actual_status_counts_success_rate_and_freshness(self):
        repository = _OperationsRepository()
        result = AutomationOperationsQueryService(repository).run(
            {"start_date": "2026-08-01", "end_date": "2026-08-25"}
        )

        self.assertEqual(result["record_source"], "automation_plugin_invocations")
        self.assertEqual(result["invocations"]["status_counts"], repository.result["invocation_status_counts"])
        self.assertEqual(result["invocations"]["success_rate"], {"completed_invocations": 2, "terminal_invocations": 4, "value": "0.5000"})
        self.assertEqual(result["freshness"]["latest_invocation_updated_at"], "2026-08-25T10:01:00Z")
        self.assertEqual(repository.queries, [(dt.date(2026, 8, 1), dt.date(2026, 8, 25))])

    def test_operations_query_rejects_extra_fields_and_fails_explicitly_when_unavailable(self):
        with self.assertRaises(BusinessQueryError) as invalid:
            AutomationOperationsQueryService(_OperationsRepository()).run(
                {"start_date": "2026-08-01", "end_date": "2026-08-25", "sql": "SELECT 1"}
            )
        self.assertEqual(invalid.exception.code, "AUTOMATION_OPERATIONS_INVALID")
        with self.assertRaises(BusinessQueryError) as unavailable:
            AutomationOperationsQueryService(_OperationsRepository(error=RuntimeError("database unavailable"))).run(
                {"start_date": "2026-08-01", "end_date": "2026-08-25"}
            )
        self.assertEqual(unavailable.exception.code, "AUTOMATION_OPERATIONS_UNAVAILABLE")

    def test_operations_formatter_does_not_invent_terminal_success_or_freshness(self):
        reply = format_automation_operations_reply(
            {
                "success": True,
                "data": AutomationOperationsQueryService(_OperationsRepository()).run(
                    {"start_date": "2026-08-01", "end_date": "2026-08-25"}
                ),
            }
        )
        self.assertIn("终态成功率：0.5000", reply)
        self.assertIn("写入结果未确认 1", reply)
        self.assertIn("最近开始执行：2026-08-25T10:00:00Z", reply)
        self.assertNotIn("命令", reply)

    def test_operations_direct_runner_is_registered_tool_contract_compatible(self):
        catalog = ToolRegistry()
        service = AutomationOperationsQueryService(_OperationsRepository())
        result = asyncio.run(
            invoke_registered_reader(
                catalog=catalog, name="query_automation_operations", handler=service.run,
                arguments={"start_date": "2026-08-01", "end_date": "2026-08-25"},
                actor=Actor(ActorType.FEISHU_USER, "bound-admin", roles=("admin",), authenticated_by="feishu_admin_binding"),
                source="feishu", llm_selected=True,
            )
        )
        self.assertTrue(result["success"])
        validate_schema_instance(
            "query_automation_operations output",
            {"status": "SUCCESS", "data": result["data"], "meta": {}, "warnings": [], "error": None},
            catalog.get_capability("query_automation_operations")["output_schema"],
        )

    def test_formatter_rejects_old_contract_and_corrupted_current_totals(self):
        service = AutomationOperationsQueryService(_OperationsRepository())
        payload = service.run({"start_date": "2026-08-01", "end_date": "2026-08-25"})
        bad_payloads = [
            {"query_type": "automation_operations", "commands": {}, "runs": {}},
            payload | {"availability": "NO_DATA"},
            payload | {"record_source": "agent_runs"},
            payload | {"invocations": payload["invocations"] | {"total": True}},
            payload | {"invocations": payload["invocations"] | {"success_rate": {"completed_invocations": 2, "terminal_invocations": 4, "value": "1.0000"}}},
        ]
        for bad in bad_payloads:
            with self.subTest(payload=bad):
                self.assertIn("结果校验失败", format_automation_operations_reply({"success": True, "data": bad}))

    def test_unrecognized_status_or_missing_freshness_is_not_reported_as_no_data(self):
        for changed in (
            {"invocation_status_counts": {"FAILED_TERMINAL": 1}},
            {"freshness": {"latest_invocation_started_at": None, "latest_invocation_updated_at": None}},
            {"freshness": {"latest_invocation_started_at": "not-a-time", "latest_invocation_updated_at": "not-a-time"}},
        ):
            with self.subTest(changed=changed), self.assertRaises(BusinessQueryError) as invalid:
                AutomationOperationsQueryService(_OperationsRepository(_OperationsRepository().result | changed)).run(
                    {"start_date": "2026-08-01", "end_date": "2026-08-25"}
                )
            self.assertEqual(invalid.exception.code, "AUTOMATION_OPERATIONS_CONTRACT_INVALID")


class BusinessFinanceQueryTests(unittest.TestCase):
    def test_reads_the_ledger_summary_and_rounds_to_cents(self):
        ledger = _Ledger()
        result = BusinessFinanceQueryService(ledger).run({"start_date": "2026-08-01", "end_date": "2026-08-25"})
        self.assertEqual(ledger.queries, [(dt.date(2026, 8, 1), dt.date(2026, 8, 25))])
        self.assertEqual(result, _finance_payload(summary={"revenue": "300.00", "cost": "120.00", "expense": "30.01",
                                                           "other_income": "0.00", "profit": "150.00"}))

    def test_no_vouchers_is_no_data_without_amounts(self):
        ledger = _Ledger(_Ledger().result | {"voucher_count": 0})
        result = BusinessFinanceQueryService(ledger).run({"start_date": "2026-08-01", "end_date": "2026-08-25"})
        self.assertEqual((result["availability"], result["summary"]), ("NO_DATA", {}))

    def test_missing_reader_or_read_failure_fails_closed(self):
        with self.assertRaises(BusinessQueryError) as missing:
            BusinessFinanceQueryService().run({"start_date": "2026-01-01", "end_date": "2026-01-31"})
        self.assertEqual(missing.exception.code, "BUSINESS_QUERY_UNAVAILABLE")
        with self.assertRaises(BusinessQueryError) as failed:
            BusinessFinanceQueryService(_Ledger(error=RuntimeError("db down 9.99"))).run(
                {"start_date": "2026-01-01", "end_date": "2026-01-31"})
        self.assertEqual(failed.exception.code, "BUSINESS_QUERY_UNAVAILABLE")
        with self.assertRaises(BusinessQueryError) as malformed:
            BusinessFinanceQueryService(_Ledger(_Ledger().result | {"revenue": "abc"})).run(
                {"start_date": "2026-01-01", "end_date": "2026-01-31"})
        self.assertEqual(malformed.exception.code, "BUSINESS_QUERY_CONTRACT_INVALID")

    def test_query_boundary_allows_366_calendar_days_and_rejects_367(self):
        service = BusinessFinanceQueryService(_Ledger())
        service.run({"start_date": "2024-01-01", "end_date": "2024-12-31"})
        with self.assertRaises(BusinessQueryError) as caught:
            service.run({"start_date": "2024-01-01", "end_date": "2025-01-01"})
        self.assertEqual(caught.exception.code, "BUSINESS_QUERY_INVALID")

    def test_rejects_unknown_fields_and_platform(self):
        service = BusinessFinanceQueryService(_Ledger())
        for arguments in (
            {"start_date": "2026-01-01", "end_date": "2026-01-31", "sql": "SELECT 1"},
            {"start_date": "2026-01-01", "end_date": "2026-01-31", "platform": "ronghui"},
            {"start_date": "2026-01-31", "end_date": "2026-01-01"},
            "not-an-object",
        ):
            with self.subTest(arguments=arguments), self.assertRaises(BusinessQueryError) as caught:
                service.run(arguments)
            self.assertEqual(caught.exception.code, "BUSINESS_QUERY_INVALID")

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
            ("查询今天财务利润", {"start_date": "2026-08-25", "end_date": "2026-08-25"}),
            ("2026-08-01 到 2026-08-20 财务", {"start_date": "2026-08-01", "end_date": "2026-08-20"}),
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
            "查询融辉 2026-08-01 到 2026-08-20 财务",
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
        reply = format_business_finance_reply({"success": True, "data": _finance_payload()})
        self.assertIn("财务汇总（2026-08-01 至 2026-08-25，总账口径）", reply)
        self.assertIn("营业收入：¥300.00", reply)
        self.assertIn("营业成本：¥120.00", reply)
        self.assertIn("期间费用：¥30.01", reply)
        self.assertIn("利润：¥149.99", reply)
        self.assertNotIn("其他损益", reply)
        self.assertIn("已封账至：2026-07", reply)
        self.assertIn("提示：所查期间尚未全部封账，金额可能变动", reply)

    def test_finance_formatter_distinguishes_no_vouchers_without_zero_amounts(self):
        payload = _finance_payload(availability="NO_DATA", summary={}, voucher_count=0, closed_through=None, warnings=[])
        reply = format_business_finance_reply({"success": True, "data": payload})
        self.assertIn("该期间总账没有凭证", reply)
        self.assertIn("已封账至：尚未封账", reply)
        self.assertNotIn("¥", reply)

    def test_finance_formatter_never_leaks_failures_or_malformed_amounts(self):
        failures = (
            {"success": False, "error_code": "BUSINESS_QUERY_CONTRACT_INVALID", "error": "secret 9.99"},
            {"success": False, "error_code": "UNKNOWN", "error": "secret 8.88"},
            {"success": True, "data": _finance_payload(summary={"revenue": "synthetic 7.77", "cost": "1.00", "expense": "0.00",
                                                                "other_income": "0.00", "profit": "6.77"})},
            {"success": True, "data": _finance_payload(closed_through="'; DROP")},
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
        from tests.chat_runtime_support import configure_chat
        core = AgentCore(today_provider=lambda: dt.date(2026, 8, 25))
        core.llm.chat = AsyncMock(side_effect=AssertionError("fixed finance reads bypass LLM"))
        core.execute_tool = AsyncMock(return_value={"success": True, "data": _finance_payload()})
        _, calls = configure_chat(core)
        actor = Actor(ActorType.FEISHU_USER, "bound-admin", roles=("admin",), authenticated_by="feishu_admin_binding")
        result = asyncio.run(core.handle_message("查本月财务", conversation_id="finance-test", actor=actor, source="feishu", request_id="event-finance-1"))
        self.assertEqual(calls[0]["params"], {"start_date": "2026-08-01", "end_date": "2026-08-25"})
        self.assertIn("营业收入：¥300.00", result["reply"])
        core.execute_tool.assert_awaited_once()
        core.llm.chat.assert_not_awaited()

    def test_agent_finance_route_requires_bound_identity_and_finance_permission(self):
        from tests.chat_runtime_support import configure_chat
        from tests.test_identity_interfaces import MutableAuthority
        from agent.harness.errors import HarnessError
        core = AgentCore(today_provider=lambda: dt.date(2026, 8, 25), direct_tool_runners={"query_business_finance": Mock()})
        core.llm.chat = AsyncMock()
        authority = MutableAuthority("ai.chat")
        configure_chat(core, authority=authority)
        for authentication in ("feishu_event", "feishu_verified_event"):
            actor = Actor(ActorType.FEISHU_USER, "unbound-user", roles=("admin",), authenticated_by=authentication)
            with self.subTest(authentication=authentication), self.assertRaises(HarnessError):
                asyncio.run(core.handle_message("今天财务", conversation_id="finance-test", actor=actor, source="feishu", request_id="one"))
        actor = Actor(ActorType.CONSOLE_ADMIN, "ordinary", roles=("admin",), authenticated_by="mysql_admin_session")
        result = asyncio.run(core.handle_message("今天财务", actor=actor, source="console"))
        self.assertIn("没有该查询权限", result["reply"])
        core._direct_tool_runners["query_business_finance"].assert_not_called()
        core.llm.chat.assert_not_awaited()

    def test_operating_summary_is_fixed_route_with_finance_dates_and_bound_feishu_admin(self):
        from tests.chat_runtime_support import configure_chat
        request = business_operations_request_from_text("查本月经营情况", today=dt.date(2026, 8, 25))
        self.assertEqual(request, {"params": {"start_date": "2026-08-01", "end_date": "2026-08-25"}})
        self.assertIn("reply", business_operations_request_from_text("查融辉本月经营情况", today=dt.date(2026, 8, 25)))
        core = AgentCore(today_provider=lambda: dt.date(2026, 8, 25))
        core.execute_tool = AsyncMock(side_effect=[
            {"success": True, "data": _finance_payload()},
            {"success": True, "data": AutomationOperationsQueryService(_OperationsRepository()).run({"start_date": "2026-08-01", "end_date": "2026-08-25"})},
        ])
        _, calls = configure_chat(core)
        actor = Actor(ActorType.FEISHU_USER, "bound-admin", roles=("admin",), authenticated_by="feishu_admin_binding")
        result = asyncio.run(core.handle_message("查本月经营摘要", conversation_id="finance-test", actor=actor, source="feishu", request_id="event-operations-1"))
        self.assertEqual([item["tool_name"] for item in calls], ["query_business_finance", "query_automation_operations"])
        self.assertNotIn("platform", calls[0]["params"])
        self.assertEqual(calls[1]["params"], {"start_date": "2026-08-01", "end_date": "2026-08-25"})
        self.assertIn("客户收入维度：当前没有可信口径，无法提供。", result["reply"])
        self.assertIn("异常历史：当前没有可信历史数据，无法提供。", result["reply"])
        self.assertIn("利润：¥149.99", result["reply"])

    def test_operating_summary_keeps_finance_amounts_closed_when_finance_is_incomplete(self):
        reply = format_business_operations_summary_reply(
            {"success": False, "error_code": "BUSINESS_QUERY_DATA_INCOMPLETE"},
            {"success": True, "data": AutomationOperationsQueryService(_OperationsRepository()).run({"start_date": "2026-08-01", "end_date": "2026-08-25"})},
        )
        self.assertIn("暂不提供金额", reply)
        self.assertNotIn("¥", reply)

    def test_finance_capability_remains_hidden_from_llm_catalog(self):
        catalog = ToolRegistry()
        exposed_names = {
            tool["function"]["name"] for tool in catalog.get_openai_tools()
        }

        self.assertNotIn("query_business_finance", exposed_names)


if __name__ == "__main__":
    unittest.main()
