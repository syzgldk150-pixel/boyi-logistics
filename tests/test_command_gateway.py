from __future__ import annotations

import unittest

from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.models import Actor, ActorType, Command


class CommandGatewayIdentityTests(unittest.TestCase):
    @staticmethod
    def _command(idempotency_key: str) -> Command:
        return Command(
            command_type="tool.execute",
            source="webhook",
            actor=Actor(ActorType.WEBHOOK, "phase7"),
            parameters={"tool_name": "query_waybill", "arguments": {}},
            idempotency_key=idempotency_key,
        )

    def test_gateway_work_item_identity_hashes_the_complete_idempotency_key(self):
        shared_prefix = "event-" + ("a" * 180)
        first = CommandGateway._classify_work_item(self._command(shared_prefix + "-one"))
        second = CommandGateway._classify_work_item(self._command(shared_prefix + "-two"))

        self.assertNotEqual(first[2], second[2])
        self.assertTrue(first[2].startswith("command:"))
        self.assertLessEqual(len(first[2]), 191)

    def test_gateway_work_item_identity_is_stable_for_a_replayed_command(self):
        first = CommandGateway._classify_work_item(self._command("event-1"))
        replay = CommandGateway._classify_work_item(self._command("event-1"))

        self.assertEqual(first, replay)


if __name__ == "__main__":
    unittest.main()
