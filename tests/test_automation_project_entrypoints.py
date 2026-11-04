from __future__ import annotations

import hashlib
from datetime import datetime
from unittest import TestCase

from agent.automation_plugins.manifest import canonical_json_bytes
from agent.orchestration.automation_project_entrypoints import TrustedDynamicArgumentResolver
from shared.automation_project_authorization import (
    OMIT_DYNAMIC_ARGUMENT,
)


def _sha(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class AutomationProjectEntrypointTests(TestCase):
    def test_code_owned_dynamic_resolver_rejects_cross_entrypoint_values(self):
        resolver = TrustedDynamicArgumentResolver()
        scheduled_for = datetime.fromisoformat("2026-08-15T00:10:00+08:00")
        self.assertEqual(
            "2026-08-14",
            resolver(
                "scheduled_previous_day",
                "target_date",
                {
                    "entrypoint": "scheduler",
                    "scheduled_for": scheduled_for.isoformat(),
                },
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "scheduler",
                    "scheduled_for": "2026-08-14T16:30:00+00:00",
                },
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "console",
                    "occurred_at": "2026-08-14T16:30:00+00:00",
                },
            ),
        )
        with self.assertRaises(ValueError):
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "webhook",
                    "occurred_at": "2026-08-14T16:30:00+00:00",
                },
            )
        self.assertEqual(
            "R001",
            resolver(
                "verified_webhook_bill_code",
                "BILL_CODE",
                {
                    "entrypoint": "webhook",
                    "dynamic_inputs": {"BILL_CODE": "R001"},
                },
            ),
        )
        with self.assertRaises(ValueError):
            resolver(
                "verified_feishu_bill_code",
                "BILL_CODE",
                {
                    "entrypoint": "webhook",
                    "dynamic_inputs": {"BILL_CODE": "R001"},
                },
            )
        self.assertIs(
            OMIT_DYNAMIC_ARGUMENT,
            resolver(
                "verified_optional_feishu_target_date",
                "target_date",
                {"entrypoint": "feishu", "dynamic_inputs": {}},
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "verified_optional_feishu_target_date",
                "target_date",
                {
                    "entrypoint": "feishu",
                    "dynamic_inputs": {"target_date": "2026-08-15"},
                },
            ),
        )

if __name__ == "__main__":
    import unittest

    unittest.main()
