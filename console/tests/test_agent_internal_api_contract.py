"""Contract tests for the single Console-to-Agent request boundary."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import LocalDocFlowApp  # noqa: E402


class _Response:
    status = 200

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self._body


class AgentInternalApiContractTests(unittest.TestCase):
    def _app(self) -> LocalDocFlowApp:
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(
            agent_base_url="http://agent.test",
            agent_timeout_seconds=10,
            agent_internal_api_token="test-token",
        )
        return app

    def test_success_envelope_is_unwrapped_once(self):
        envelope = {"ok": True, "data": {"rows": [1]}, "error": None}
        with patch("console.services.automation.urlopen", return_value=_Response(envelope)) as mocked_open:
            result = self._app()._agent_request("GET", "/internal/v1/tools")

        self.assertEqual({"ok": True, "status": 200, "data": {"rows": [1]}}, result)
        self.assertEqual(10, mocked_open.call_args.kwargs["timeout"])

    def test_explicit_timeout_is_forwarded_and_zero_uses_default(self):
        envelope = {"ok": True, "data": {}, "error": None}
        with patch("console.services.automation.urlopen", return_value=_Response(envelope)) as mocked_open:
            self._app()._agent_request("GET", "/internal/v1/tools", timeout=7)
            self.assertEqual(7, mocked_open.call_args.kwargs["timeout"])
            self._app()._agent_request("GET", "/internal/v1/tools", timeout=0)
            self.assertEqual(10, mocked_open.call_args.kwargs["timeout"])

    def test_malformed_versioned_response_fails_explicitly(self):
        with patch("console.services.automation.urlopen", return_value=_Response({"rows": []})):
            result = self._app()._agent_request("GET", "/internal/v1/tools")

        self.assertFalse(result["ok"])
        self.assertEqual("invalid_internal_contract", result["error_code"])


if __name__ == "__main__":
    unittest.main()
