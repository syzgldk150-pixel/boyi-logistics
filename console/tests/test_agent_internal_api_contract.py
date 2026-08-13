"""Contract tests for the single Console-to-Agent request boundary."""

from __future__ import annotations

import json
import os
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shared.service_identity import ConsoleIdentityVerifier


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import LocalDocFlowApp  # noqa: E402
from console.app_support import _CURRENT_ADMIN_USER  # noqa: E402


class _Response:
    def __init__(self, payload: dict, *, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

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
        with patch("console.services.agent_api.urlopen", return_value=_Response(envelope)):
            result = self._app()._agent_request("GET", "/internal/v1/tools")

        self.assertEqual({"ok": True, "status": 200, "data": {"rows": [1]}}, result)

    def test_malformed_versioned_response_fails_explicitly(self):
        with patch("console.services.agent_api.urlopen", return_value=_Response({"rows": []})):
            result = self._app()._agent_request("GET", "/internal/v1/tools")

        self.assertFalse(result["ok"])
        self.assertEqual("invalid_internal_contract", result["error_code"])

    def test_accepted_status_is_preserved_for_asynchronous_commands(self):
        envelope = {"ok": True, "data": {"run_id": "run-1"}, "error": None}
        with patch(
            "console.services.agent_api.urlopen",
            return_value=_Response(envelope, status=202),
        ):
            result = self._app()._agent_request("POST", "/internal/v1/commands", payload={})

        self.assertEqual(202, result["status"])
        self.assertEqual("run-1", result["data"]["run_id"])

    def test_signed_console_principal_is_bound_to_request_and_not_sent_as_marker(self):
        envelope = {"ok": True, "data": {"run_id": "run-1"}, "error": None}
        captured = {}

        def open_request(request, **_kwargs):
            captured["request"] = request
            return _Response(envelope, status=202)

        principal = {
            "actor_type": "console_admin",
            "actor_id": "42",
            "roles": ["super_admin"],
            "display_name": "Admin",
            "authenticated_by": "mysql_admin_session",
        }
        payload = {
            "source": "console",
            "actor": {"actor_id": "forged-body"},
        }
        with patch.dict(os.environ, {"CONSOLE_AGENT_SIGNING_SECRET": "separate-signing-secret"}), patch(
            "console.services.agent_api.urlopen",
            side_effect=open_request,
        ):
            result = self._app()._agent_request(
                "POST",
                "/internal/v1/commands?mode=exact",
                payload=payload,
                console_principal=principal,
            )

        self.assertTrue(result["ok"])
        request = captured["request"]
        raw_body = request.data
        self.assertNotIn(b"_console_principal", raw_body)
        verifier = ConsoleIdentityVerifier("separate-signing-secret")
        verified = verifier.verify(
            headers=dict(request.header_items()),
            method="POST",
            request_target="/internal/v1/commands?mode=exact",
            body=raw_body,
        )
        self.assertEqual("42", verified["actor_id"])
        self.assertEqual(["super_admin"], verified["roles"])

    def test_console_principal_fails_closed_without_dedicated_signing_secret(self):
        principal = {
            "actor_type": "console_admin",
            "actor_id": "42",
            "roles": ["admin"],
            "authenticated_by": "mysql_admin_session",
        }
        with patch.dict(os.environ, {}, clear=True):
            result = self._app()._agent_request(
                "POST",
                "/internal/v1/commands",
                payload={},
                console_principal=principal,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(503, result["status"])
        self.assertEqual("CONSOLE_SIGNING_SECRET_NOT_CONFIGURED", result["error_code"])

    def test_console_principal_marker_in_request_body_is_rejected(self):
        with patch("console.services.agent_api.urlopen") as open_request:
            result = self._app()._agent_request(
                "POST",
                "/internal/v1/commands",
                payload={"_console_principal": {"actor_id": "attacker"}},
            )

        self.assertFalse(result["ok"])
        self.assertEqual("INVALID_CALLER_CONTEXT", result["error_code"])
        open_request.assert_not_called()

    def test_admin_endpoint_automatically_signs_real_mysql_session(self):
        envelope = {"ok": True, "data": {"accounts": []}, "error": None}
        captured = {}

        def open_request(request, **_kwargs):
            captured["request"] = request
            return _Response(envelope)

        session_user = {
            "id": 42,
            "username": "operator",
            "display_name": "Operator",
            "control_plane_role": "admin",
            "is_legacy_basic_auth": False,
        }
        token = _CURRENT_ADMIN_USER.set(session_user)
        try:
            with patch.dict(
                os.environ,
                {"CONSOLE_AGENT_SIGNING_SECRET": "separate-signing-secret"},
            ), patch(
                "console.services.agent_api.urlopen",
                side_effect=open_request,
            ):
                result = self._app()._agent_request(
                    "GET",
                    "/internal/v1/admin/accounts?force=1",
                )
        finally:
            _CURRENT_ADMIN_USER.reset(token)

        self.assertTrue(result["ok"])
        request = captured["request"]
        verified = ConsoleIdentityVerifier("separate-signing-secret").verify(
            headers=dict(request.header_items()),
            method="GET",
            request_target="/internal/v1/admin/accounts?force=1",
            body=b"",
        )
        self.assertEqual("42", verified["actor_id"])
        self.assertEqual(["admin"], verified["roles"])

    def test_admin_endpoint_rejects_basic_or_missing_mysql_session(self):
        for session_user in (
            None,
            {
                "id": 0,
                "username": "break-glass",
                "is_legacy_basic_auth": True,
            },
        ):
            token = _CURRENT_ADMIN_USER.set(session_user)
            try:
                result = self._app()._agent_request(
                    "POST",
                    "/internal/v1/admin/reload",
                    payload={},
                )
            finally:
                _CURRENT_ADMIN_USER.reset(token)

            self.assertFalse(result["ok"])
            self.assertEqual(HTTPStatus.FORBIDDEN, result["status"])
            self.assertEqual("MYSQL_ADMIN_SESSION_REQUIRED", result["error_code"])

    def test_non_internal_or_noncanonical_agent_endpoint_is_rejected_before_network(self):
        invalid_endpoints = (
            "/health",
            "https://attacker.test/internal/v1/tools",
            "//attacker.test/internal/v1/tools",
            "/internal/v1/../admin/reload",
            "/internal/v1/%2e%2e/admin/reload",
            "/internal/v1/%252e%252e/admin/reload",
            "/internal/v1/tools#fragment",
            "/internal/v1/tools\\escape",
        )
        with patch("console.services.agent_api.urlopen") as open_request:
            for endpoint in invalid_endpoints:
                with self.subTest(endpoint=endpoint):
                    result = self._app()._agent_request("GET", endpoint)
                    self.assertFalse(result["ok"])
                    self.assertEqual("INVALID_AGENT_ENDPOINT", result["error_code"])
        open_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
