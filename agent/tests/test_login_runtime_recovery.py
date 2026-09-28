"""Runtime failures recover without exhausting the account's authentication budget."""

from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import requests
from playwright.sync_api import TimeoutError as BrowserTimeoutError

from agent.tms_runtime import account_manager as accounts
from agent.tms_runtime import session_login_worker
from agent.tms_runtime.errors import TMSAuthStateError, login_runtime_error
from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth
from agent.tms_runtime.scripts.r13_login_manager import R13SSOAuth
from agent.tms_runtime.session_state import SessionStateStore


class LoginRuntimeErrorTests(unittest.TestCase):
    def test_runtime_error_causes_survive_auth_wrappers_without_exposing_details(self):
        failures = (
            (BrowserTimeoutError("fixture-sensitive-browser-input"), "LOGIN_PAGE_UNAVAILABLE"),
            (requests.ReadTimeout("fixture-sensitive-session-url"), "LOGIN_NETWORK_UNAVAILABLE"),
            (subprocess.TimeoutExpired("fixture-worker-command", 120), "LOGIN_TIMEOUT"),
        )
        for cause, code in failures:
            with self.subTest(code=code):
                wrapped = TMSAuthStateError("AUTH_REQUIRED", "fixture-provider-wrapper")
                wrapped.__cause__ = cause
                actual = login_runtime_error(wrapped)
                self.assertEqual(code, actual.code)
                self.assertNotIn("fixture-", str(actual))

    def test_upstream_unavailability_is_distinct_from_an_authentication_rejection(self):
        for status in (400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                actual = login_runtime_error(requests.HTTPError(response=response))
                self.assertEqual(status >= 500 or status == 429, actual is not None)
        self.assertIsNone(login_runtime_error(TMSAuthStateError("AUTH_REQUIRED", "账号或密码错误")))
        self.assertIsNone(login_runtime_error(TMSAuthStateError("BLOCKED_LOGIN", "busy")))

    def test_worker_does_not_commit_partial_state_after_browser_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            failure = TMSAuthStateError("AUTH_REQUIRED", "fixture-wrapper")
            failure.__cause__ = BrowserTimeoutError("fixture-sensitive-input")
            with patch.object(session_login_worker, "build_session_broker") as factory:
                factory.return_value.send_code.side_effect = failure
                session_login_worker.run_worker(profile="fixture", state_dir=state, request={"action": "send"})
            result = SessionStateStore.read_dict(state / "operation_result.json")
            self.assertEqual("LOGIN_PAGE_UNAVAILABLE", result["error_code"])
            self.assertFalse(result["commit_staged_state"])
            self.assertNotIn("fixture-sensitive", result["error"])

    def test_worker_unexpected_exception_is_a_runtime_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(session_login_worker, "build_session_broker") as factory:
                factory.return_value.send_code.side_effect = RuntimeError("fixture-sensitive-input")
                session_login_worker.run_worker(profile="fixture", state_dir=state, request={"action": "send"})
            result = SessionStateStore.read_dict(state / "operation_result.json")
            self.assertEqual("LOGIN_WORKER_UNAVAILABLE", result["error_code"])
            self.assertFalse(result["commit_staged_state"])
            self.assertNotIn("fixture-sensitive", result["error"])

    def test_sso_transport_cause_reaches_the_account_manager(self):
        for auth_type in (R7SSOAuth, R13SSOAuth):
            with self.subTest(provider=auth_type.__name__), tempfile.TemporaryDirectory() as directory:
                auth = auth_type(config_path="", state_path=Path(directory) / "sso_session.json")
                with (
                    patch.object(auth, "_request_token", side_effect=requests.ReadTimeout("fixture-timeout")),
                    patch("time.sleep"),
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        auth.login_and_get_session(
                            username="fixture-user", password="fixture-password", max_attempts=1, allow_cached=False
                        )
                self.assertEqual("LOGIN_NETWORK_UNAVAILABLE", login_runtime_error(raised.exception).code)

    def test_sso_verification_outage_is_not_a_rejected_login(self):
        for auth_type in (R7SSOAuth, R13SSOAuth):
            with self.subTest(provider=auth_type.__name__), tempfile.TemporaryDirectory() as directory:
                auth = auth_type(config_path="", state_path=Path(directory) / "sso_session.json")
                with patch.object(auth.session, "get", side_effect=requests.ReadTimeout("fixture-timeout")):
                    with self.assertRaises(requests.ReadTimeout):
                        auth._verify_authenticated()
                for code in (429, 503):
                    response = requests.Response()
                    response.status_code = code
                    with patch.object(auth.session, "get", return_value=response):
                        with self.assertRaises(requests.HTTPError):
                            auth._verify_authenticated()


class AccountLoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        state = Path(directory) / "state"
        for name, value in (
            ("STATE_DIR", state),
            ("ACCOUNTS_PATH", state / "automation_accounts.json"),
            ("LOCAL_ACCOUNT_DIR", state / "automation_account_credentials"),
        ):
            self.stack.enter_context(patch.object(accounts, name, value))
        self.stack.enter_context(patch.object(requests.Session, "request", side_effect=AssertionError("no network")))
        self.manager = accounts.AutomationAccountManager()

    def test_browser_runtime_failures_retry_later_without_authentication_failure_count(self):
        with patch.object(accounts, "get_session_broker") as factory:
            broker = factory.return_value
            broker.get_manual_credentials.return_value = {"has_manual_credentials": True}
            broker.describe_status.return_value = {"status": "expired", "authenticated": False}
            self.manager.set_auto_login("ronghui_default", True)
            # A previous real rejection is preserved, not erased by runtime errors.
            self.manager._set_auto_login_state("ronghui_default", failure_count=1)
            for code in ("LOGIN_TIMEOUT", "LOGIN_WORKER_UNAVAILABLE", "LOGIN_PAGE_UNAVAILABLE", "AUTH_UNAVAILABLE"):
                broker.send_code.side_effect = TMSAuthStateError(code, "fixture-details")
                status = self.manager.check_status_with_auto_login("ronghui_default")
                self.assertEqual(1, status["auto_login_failure_count"])
                self.assertFalse(status["auto_login_blocked"])
                self.assertTrue(status["auto_login_retryable"])
                self.assertEqual(code, status["last_error_code"])
                self.assertNotIn("fixture-details", status["last_error_summary"])
            self.assertEqual(4, broker.send_code.call_count)
            broker.send_code.side_effect = None
            broker.send_code.return_value = {"status": "authenticated", "authenticated": True}
            restored = self.manager.check_status_with_auto_login("ronghui_default")
            self.assertTrue(restored["authenticated"])
            self.assertEqual(0, restored["auto_login_failure_count"])
            self.assertFalse(restored["auto_login_blocked"])
            self.assertNotIn("auto_login_retryable", restored)
            self.assertEqual(5, broker.send_code.call_count)

    def test_sso_runtime_failures_do_not_pause_and_manual_success_clears_a_real_pause(self):
        for account_id in ("r7_default", "r13_default"):
            with self.subTest(account=account_id):
                self.manager.save_credentials(account_id, username="fixture-user", password="fixture-password")
                self.manager.set_auto_login(account_id, True)
                with patch.object(self.manager, "_sso_auth") as factory:
                    auth = factory.return_value
                    auth.persisted_status.return_value = {"status": "expired", "authenticated": False}
                    auth.login_and_get_session.side_effect = requests.ReadTimeout("fixture-network-error")
                    for _ in range(4):
                        status = self.manager.check_status_with_auto_login(account_id)
                        self.assertTrue(status["auto_login_retryable"])
                        self.assertEqual(0, status["auto_login_failure_count"])
                        self.assertFalse(status["auto_login_blocked"])
                    self.manager._set_auto_login_state(account_id, failure_count=3, blocked=True)
                    auth.login_and_get_session.side_effect = None
                    auth.persisted_status.return_value = {"status": "authenticated", "authenticated": True}
                    restored = self.manager.login(account_id)
                    self.assertTrue(restored["authenticated"])
                    self.assertEqual(0, restored["auto_login_failure_count"])
                    self.assertFalse(restored["auto_login_blocked"])


if __name__ == "__main__":
    unittest.main()
