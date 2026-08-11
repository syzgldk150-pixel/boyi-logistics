"""Fail-closed tests for R7/R13 SSO session verification."""

import unittest
from unittest.mock import Mock

import requests

from agent.tms_runtime.scripts.r13_login_manager import R13SSOAuth
from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth


class SSOAuthenticationVerificationTests(unittest.TestCase):
    def _auth_instances(self):
        return (R7SSOAuth(config_path=""), R13SSOAuth(config_path=""))

    def test_network_failure_is_not_treated_as_authenticated(self):
        for auth in self._auth_instances():
            with self.subTest(auth=type(auth).__name__):
                auth.session.get = Mock(side_effect=requests.ConnectionError("network unavailable"))
                with self.assertRaises(RuntimeError):
                    auth._verify_authenticated()

    def test_only_successful_non_login_page_is_authenticated(self):
        for auth in self._auth_instances():
            with self.subTest(auth=type(auth).__name__):
                authenticated = Mock(status_code=200, text="Welcome", headers={})
                auth.session.get = Mock(return_value=authenticated)
                self.assertTrue(auth._verify_authenticated())

                unauthorized = Mock(status_code=401, text="Unauthorized", headers={})
                auth.session.get = Mock(return_value=unauthorized)
                self.assertFalse(auth._verify_authenticated())

                redirected = Mock(
                    status_code=302,
                    text="",
                    headers={"Location": "https://sso.ronghuiwl.com/login"},
                )
                auth.session.get = Mock(return_value=redirected)
                self.assertFalse(auth._verify_authenticated())


if __name__ == "__main__":
    unittest.main()
