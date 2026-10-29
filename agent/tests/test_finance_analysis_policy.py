from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class FinanceAnalysisPolicyTests(unittest.TestCase):
    def test_completed_finance_collection_never_implicitly_starts_analysis(self):
        from main import _finance_brain_completed_handler

        submit = Mock(side_effect=AssertionError("automatic analysis is prohibited"))
        delivery = {"event_id": "old-completion", "event_type": "agent.run.completed",
                    "payload_json": {"status": "COMPLETED", "tool_names": ["finance_statement"]}}
        first = _finance_brain_completed_handler(submit, delivery, object())
        second = _finance_brain_completed_handler(submit, delivery, object())
        self.assertFalse(first["processed"])
        self.assertEqual(first, second)
        submit.assert_not_called()

    def test_manual_finance_analysis_returns_this_invocation_result(self):
        from main import FinanceAnalyzeRequest, internal_analyze_finance_reviews

        request_id = "00000000-0000-4000-8000-000000000020"
        business_result = {"completed_count": 7, "failed_count": 0}
        brain = SimpleNamespace(analyze_pending=AsyncMock(return_value=business_result))
        observed = {}

        async def call_business(**kwargs):
            observed.update(kwargs)
            result = await kwargs["handler"]()
            return {"invocation_id": request_id, "status": "COMPLETED", "result": result}

        policies = SimpleNamespace(direct_invocations=SimpleNamespace(call_business=call_business))
        request = SimpleNamespace(state=SimpleNamespace(console_principal={
            "actor_type": "console_admin", "actor_id": "42", "roles": ["admin"],
            "authenticated_by": "console_identity",
        }))
        with (patch("main._runtime", return_value=SimpleNamespace(finance_brain=brain)),
              patch("main._automation_project_policies", return_value=policies)):
            response = asyncio.run(internal_analyze_finance_reviews(
                FinanceAnalyzeRequest(request_id=request_id, limit=7), request))
        self.assertEqual("COMPLETED", response["data"]["status"])
        self.assertEqual(business_result, response["data"]["result"])
        self.assertEqual(request_id, observed["request_id"])
        self.assertEqual("42", observed["actor_id"])
        self.assertEqual("console", observed["source"])
        self.assertEqual({"limit": 7}, observed["arguments"])
        brain.analyze_pending.assert_awaited_once_with(limit=7)

    def test_non_finance_completion_does_not_invoke_finance_brain(self):
        from main import _finance_brain_completed_handler

        result = _finance_brain_completed_handler(
            Mock(side_effect=AssertionError("must not submit")),
            {
                "event_id": "00000000-0000-0000-0000-000000000005",
                "event_type": "agent.run.completed",
                "payload_json": {"tool_names": ["sync_daily_should_sign"]},
            },
            object(),
        )

        self.assertFalse(result["processed"])


if __name__ == "__main__":
    unittest.main()
