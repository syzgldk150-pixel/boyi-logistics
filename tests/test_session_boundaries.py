from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent.tms_runtime.session_adapters import RonghuiSessionAdapter, YundaSessionAdapter
from agent.tms_runtime.session_models import safe_profile_name
from agent.tms_runtime.session_state import SessionStateStore
from agent.tms_runtime.session_validators import looks_like_ronghui_login


class _Broker:
    pass


class _Response:
    def __init__(self, *, location: str = "", body: str = ""):
        self.headers = {"Location": location}
        self.text = body


class SessionBoundaryTests(unittest.TestCase):
    def test_provider_adapters_delegate_to_the_selected_flow(self):
        broker = _Broker()
        with (
            patch.object(RonghuiSessionAdapter, "send_ronghui_code", return_value={"provider": "ronghui"}),
            patch.object(
                RonghuiSessionAdapter,
                "submit_ronghui_code",
                side_effect=lambda code: {"provider": "ronghui", "code": code},
            ),
            patch.object(YundaSessionAdapter, "send_yunda_code", return_value={"provider": "yunda"}),
            patch.object(
                YundaSessionAdapter,
                "submit_yunda_code",
                side_effect=lambda code: {"provider": "yunda", "code": code},
            ),
        ):
            self.assertEqual("ronghui", RonghuiSessionAdapter(broker).send_code()["provider"])
            self.assertEqual("123456", RonghuiSessionAdapter(broker).submit_code("123456")["code"])
            self.assertEqual("yunda", YundaSessionAdapter(broker).send_code()["provider"])
            self.assertEqual("654321", YundaSessionAdapter(broker).submit_code("654321")["code"])

        self.assertEqual("agent.tms_runtime.session_ronghui_adapter", RonghuiSessionAdapter.__module__)
        self.assertEqual("agent.tms_runtime.session_yunda_adapter", YundaSessionAdapter.__module__)

    def test_state_store_isolated_from_login_flow(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            store = SessionStateStore(Path(temp_dir))
            store.write_dict(path, {"status": "authenticated"})
            self.assertEqual({"status": "authenticated"}, store.read_dict(path))
            store.remove(path)
            self.assertIsNone(store.read_dict(path))

    def test_login_validator_and_profile_normalization_are_pure(self):
        self.assertTrue(looks_like_ronghui_login(_Response(location="/system/login")))
        self.assertTrue(looks_like_ronghui_login(_Response(body='<input name="validateCode">')))
        self.assertFalse(looks_like_ronghui_login(_Response(body="dashboard")))
        self.assertEqual("price_default", safe_profile_name(" Price/default "))


if __name__ == "__main__":
    unittest.main()
