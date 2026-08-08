from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent.tms_runtime.session_adapters import RonghuiSessionAdapter, YundaSessionAdapter
from agent.tms_runtime.session_models import safe_profile_name
from agent.tms_runtime.session_state import SessionStateStore
from agent.tms_runtime.session_validators import looks_like_ronghui_login


class _Broker:
    def _send_code_ronghui(self):
        return {"provider": "ronghui", "action": "send"}

    def _submit_code_ronghui(self, code):
        return {"provider": "ronghui", "action": "submit", "code": code}

    def _send_code_yunda(self):
        return {"provider": "yunda", "action": "send"}

    def _submit_code_yunda(self, code):
        return {"provider": "yunda", "action": "submit", "code": code}


class _Response:
    def __init__(self, *, location: str = "", body: str = ""):
        self.headers = {"Location": location}
        self.text = body


class SessionBoundaryTests(unittest.TestCase):
    def test_provider_adapters_delegate_to_the_selected_flow(self):
        broker = _Broker()
        self.assertEqual("ronghui", RonghuiSessionAdapter(broker).send_code()["provider"])
        self.assertEqual("123456", RonghuiSessionAdapter(broker).submit_code("123456")["code"])
        self.assertEqual("yunda", YundaSessionAdapter(broker).send_code()["provider"])
        self.assertEqual("654321", YundaSessionAdapter(broker).submit_code("654321")["code"])

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
