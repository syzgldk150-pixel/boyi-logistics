import json
import time
import unittest
from urllib.parse import quote

from agent.tms_runtime.ronghui_user_context import (
    normalize_ronghui_user_info_storage_state,
    parse_ronghui_user_info_cookie,
)


def _user_info_value(*, account: str = "fixture-account") -> str:
    return json.dumps(
        {
            "loginUserName": "fixture-user",
            "loginUserAccount": account,
            "loginSiteName": "fixture-site",
            "loginSiteCode": "fixture-site-code",
        },
        ensure_ascii=False,
    )


class RonghuiUserContextTests(unittest.TestCase):
    def test_parses_javascript_unicode_escaped_cookie(self):
        raw = quote(_user_info_value().replace("fixture-site", "测试", 1), safe="")
        raw = raw.replace("%E6%B5%8B%E8%AF%95", "%u6D4B%u8BD5")

        parsed = parse_ronghui_user_info_cookie(raw)

        self.assertEqual("测试", parsed["loginSiteName"])

    def test_normalizes_existing_user_info_to_javascript_readable(self):
        storage_state = {
            "cookies": [
                {
                    "name": "userInfo",
                    "value": _user_info_value(),
                    "domain": "tms.ronghuiwl.com",
                    "path": "/",
                    "httpOnly": True,
                }
            ],
            "origins": [],
        }

        changed, status = normalize_ronghui_user_info_storage_state(
            storage_state,
            host="tms.ronghuiwl.com",
        )

        self.assertTrue(changed)
        self.assertEqual("ready", status)
        self.assertFalse(storage_state["cookies"][0]["httpOnly"])

    def test_rejects_missing_incomplete_and_conflicting_contexts(self):
        missing = {"cookies": [], "origins": []}
        incomplete = {
            "cookies": [
                {
                    "name": "userInfo",
                    "value": json.dumps({"loginUserName": "fixture-user"}),
                    "domain": "tms.ronghuiwl.com",
                }
            ]
        }
        conflicting = {
            "cookies": [
                {"name": "userInfo", "value": _user_info_value(account="fixture-a"), "domain": "tms.ronghuiwl.com"},
                {"name": "userInfo", "value": _user_info_value(account="fixture-b"), "domain": ".ronghuiwl.com"},
            ]
        }

        self.assertEqual(
            "missing",
            normalize_ronghui_user_info_storage_state(missing, host="tms.ronghuiwl.com")[1],
        )
        self.assertEqual(
            "incomplete",
            normalize_ronghui_user_info_storage_state(incomplete, host="tms.ronghuiwl.com")[1],
        )
        self.assertEqual(
            "conflicting",
            normalize_ronghui_user_info_storage_state(conflicting, host="tms.ronghuiwl.com")[1],
        )

    def test_rejects_user_info_not_visible_to_business_pages_or_expired(self):
        scoped = {
            "cookies": [
                {
                    "name": "userInfo",
                    "value": _user_info_value(),
                    "domain": "tms.ronghuiwl.com",
                    "path": "/system",
                }
            ]
        }
        expired = {
            "cookies": [
                {
                    "name": "userInfo",
                    "value": _user_info_value(),
                    "domain": "tms.ronghuiwl.com",
                    "path": "/",
                    "expires": time.time() - 60,
                }
            ]
        }

        self.assertEqual(
            "incomplete",
            normalize_ronghui_user_info_storage_state(scoped, host="tms.ronghuiwl.com")[1],
        )
        self.assertEqual(
            "incomplete",
            normalize_ronghui_user_info_storage_state(expired, host="tms.ronghuiwl.com")[1],
        )


if __name__ == "__main__":
    unittest.main()
