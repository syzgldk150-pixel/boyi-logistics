from __future__ import annotations

import unittest

from shared.contracts import api_failure, api_success


class ContractTests(unittest.TestCase):
    def test_success_response_uses_the_stable_envelope(self):
        self.assertEqual(
            {"ok": True, "data": {"value": 1}, "error": None},
            api_success({"value": 1}),
        )

    def test_failure_response_uses_the_stable_envelope(self):
        self.assertEqual(
            {
                "ok": False,
                "data": None,
                "error": {"code": "invalid", "message": "Invalid request"},
            },
            api_failure("invalid", "Invalid request"),
        )


if __name__ == "__main__":
    unittest.main()
