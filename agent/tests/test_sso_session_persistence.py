import json
import tempfile
import unittest
from pathlib import Path

import requests

from agent.tms_runtime.sso_session_persistence import SSOSessionPersistenceMixin


class FakeSSOClient(SSOSessionPersistenceMixin):
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.session = requests.Session()
        self.last_token = None


class SSOSessionPersistenceTests(unittest.TestCase):
    def test_authenticated_state_can_be_restored_and_invalidated_without_leaking_token(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "account" / "sso_session.json"
            writer = FakeSSOClient(state_path)
            writer.last_token = "synthetic-token"
            writer.session.cookies.set("synthetic-cookie", "synthetic-value")
            writer._save_sso_state(status="authenticated")

            reader = FakeSSOClient(state_path)
            restored = reader.restore_persisted_session(
                validate=True,
                validator=lambda: True,
                attach_bearer=True,
            )

            self.assertTrue(restored)
            self.assertEqual("Bearer synthetic-token", reader.session.headers["Authorization"])
            self.assertEqual("synthetic-value", reader.session.cookies.get("synthetic-cookie"))

            invalidated = reader.restore_persisted_session(
                validate=True,
                validator=lambda: False,
                attach_bearer=True,
            )
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertFalse(invalidated)
            self.assertEqual("expired", persisted["status"])
            self.assertEqual("", persisted["token"])
            self.assertNotIn("Authorization", reader.session.headers)

    def test_clear_removes_runtime_state_and_in_memory_authentication(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "account" / "sso_session.json"
            client = FakeSSOClient(state_path)
            client.last_token = "synthetic-token"
            client.session.headers["Authorization"] = "Bearer synthetic-token"
            client.session.cookies.set("synthetic-cookie", "synthetic-value")
            client._save_sso_state(status="authenticated")

            client.clear_persisted_session()

            self.assertFalse(state_path.exists())
            self.assertIsNone(client.last_token)
            self.assertNotIn("Authorization", client.session.headers)
            self.assertEqual([], list(client.session.cookies))


if __name__ == "__main__":
    unittest.main()
