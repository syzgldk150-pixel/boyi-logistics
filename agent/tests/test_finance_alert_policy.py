from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from agent.core import AgentCore


class _Registry:
    @staticmethod
    def get_tool(_tool_name):
        return {"runner": "fixture"}


class _Memory:
    @staticmethod
    def save_tool_log(**_kwargs):
        return None


def _core_with_result(result):
    core = AgentCore.__new__(AgentCore)
    core.registry = _Registry()
    core.memory = _Memory()
    core.finance_brain = None
    core._execute_tool_config = AsyncMock(return_value=dict(result))
    return core


class FinanceAlertPolicyTests(unittest.TestCase):
    def test_startup_catchup_failure_does_not_send_proactive_alert(self):
        core = _core_with_result(
            {
                "success": False,
                "error_code": "FINANCE_SYNC_INTERNAL",
                "error": "safe fixture error",
            }
        )
        with patch("agent.core.publish_finance_alert") as publish:
            asyncio.run(
                core.execute_tool(
                    "sync_finance_bills",
                    {"mode": "sync", "_startup_catchup": True},
                )
            )
        publish.assert_not_called()

    def test_scheduled_failure_alert_uses_specific_error_code(self):
        core = _core_with_result(
            {
                "success": False,
                "error_code": "AUTH_REQUIRED",
                "error": "account login required",
            }
        )
        with patch("agent.core.publish_finance_alert") as publish:
            asyncio.run(core.execute_tool("sync_finance_bills", {"mode": "sync"}))
        publish.assert_called_once()
        self.assertEqual("AUTH_REQUIRED", publish.call_args.args[0]["anomaly_type"])


if __name__ == "__main__":
    unittest.main()
