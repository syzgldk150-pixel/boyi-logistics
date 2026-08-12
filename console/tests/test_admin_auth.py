import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import (  # noqa: E402
    LocalDocFlowApp,
    current_admin_user,
    hash_admin_password,
    verify_admin_password,
)


class _Handler:
    def __init__(self, path="/", headers=None, form=None):
        body = urlencode(form or {}).encode("utf-8")
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        self.headers.update(headers or {})
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        for header_name, header_value in self.sent_headers:
            if header_name.lower() == name.lower():
                return header_value
        return ""


class _MultipartHandler(_Handler):
    def __init__(self, path="/settings/profile/avatar", headers=None, body=b"", content_type="multipart/form-data; boundary=test-boundary"):
        self.path = path
        self.headers = Message()
        self.headers["Content-Length"] = str(len(body))
        self.headers["Content-Type"] = content_type
        self.headers["Accept"] = "application/json"
        self.headers["X-Requested-With"] = "XMLHttpRequest"
        self.headers["Host"] = "localhost:8765"
        self.headers["Referer"] = "http://localhost:8765/"
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []


class _SessionRepo:
    def __init__(self, session=None):
        self.session = session
        self.touched = []
        self.deleted = []

    def get_admin_session(self, session_id):
        return self.session if session_id == "sid" else None

    def touch_admin_session(self, session_id):
        self.touched.append(session_id)

    def delete_admin_session(self, session_id):
        self.deleted.append(session_id)


class _LoginRepo:
    def __init__(self):
        self.user = {
            "id": 7,
            "username": "admin",
            "display_name": "Admin",
            "is_active": 1,
            "password_hash": hash_admin_password("strong-password"),
        }
        self.created_sessions = []
        self.recorded_logins = []

    def get_admin_user_by_username(self, username):
        return self.user if username == "admin" else None

    def count_admin_users(self):
        return 1

    def delete_expired_admin_sessions(self, now):
        self.deleted_before = now

    def create_admin_session(self, **kwargs):
        self.created_sessions.append(kwargs)

    def record_admin_login(self, user_id):
        self.recorded_logins.append(user_id)


class _AccountRepo:
    def __init__(self):
        self.created = []
        self.active_updates = []

    def get_admin_user_by_username(self, username):
        return None

    def create_admin_user(self, **kwargs):
        self.created.append(kwargs)
        return 9

    def get_admin_user(self, user_id):
        return {"id": user_id, "username": "admin", "is_active": 1}

    def set_admin_user_active(self, user_id, is_active):
        self.active_updates.append((user_id, is_active))


class _AvatarRepo:
    def __init__(self):
        self.avatar_updates = []

    def update_admin_user_avatar(self, user_id, avatar_path):
        self.avatar_updates.append((user_id, avatar_path))


class AdminAuthTests(unittest.TestCase):
    def _build_app(self, repository):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(
            app_title="Test Console",
            basic_auth_user="",
            basic_auth_password="",
            session_cookie_secure=False,
            session_ttl_hours=12,
            session_secret="unit-test-secret",
        )
        app.repository = repository
        app._session_secret = "unit-test-secret"
        return app

    def test_password_hash_verification(self):
        password_hash = hash_admin_password("secret-password")

        self.assertTrue(verify_admin_password("secret-password", password_hash))
        self.assertFalse(verify_admin_password("wrong-password", password_hash))
        self.assertNotIn("secret-password", password_hash)

    def test_unauthenticated_page_redirects_to_login(self):
        app = self._build_app(_SessionRepo())
        handler = _Handler(path="/")

        app.handle_get(handler)

        self.assertEqual(HTTPStatus.SEE_OTHER, handler.status)
        self.assertEqual("/login?next=%2F", handler.header("Location"))

    def test_login_route_is_public(self):
        app = self._build_app(_SessionRepo())
        called = {}
        app._render_login = lambda handler, query: called.update({"query": query})
        handler = _Handler(path="/login?next=/ocr")

        app.handle_get(handler)

        self.assertEqual({"next": ["/ocr"]}, called["query"])

    def test_valid_session_cookie_authorizes_request(self):
        session = {
            "session_id": "sid",
            "user_id": 7,
            "username": "admin",
            "display_name": "Admin",
            "is_active": 1,
            "role": "super_admin",
            "expires_at": datetime.now() + timedelta(hours=1),
        }
        repo = _SessionRepo(session=session)
        app = self._build_app(repo)
        cookie = app._encode_session_cookie("sid")
        handler = _Handler(headers={"Cookie": f"docflow_admin_session={cookie}"})

        self.assertTrue(app._ensure_authorized(handler))
        self.assertEqual("admin", current_admin_user()["username"])
        self.assertEqual(["sid"], repo.touched)

    def test_login_success_creates_session_cookie(self):
        repo = _LoginRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/login",
            form={"username": "admin", "password": "strong-password", "next": "/ocr"},
        )

        app._handle_login(handler)

        self.assertEqual(HTTPStatus.SEE_OTHER, handler.status)
        self.assertEqual("/ocr", handler.header("Location"))
        self.assertIn("docflow_admin_session=", handler.header("Set-Cookie"))
        self.assertEqual(7, repo.created_sessions[0]["user_id"])
        self.assertEqual([7], repo.recorded_logins)

    def test_create_admin_account_hashes_password(self):
        repo = _AccountRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/settings/accounts/create",
            form={
                "username": "ops-admin",
                "display_name": "Ops Admin",
                "password": "another-strong-password",
            },
        )
        app._set_current_admin_user(
            handler,
            {"id": 1, "username": "owner", "role": "super_admin", "is_legacy_basic_auth": False},
        )

        app._handle_admin_account_create(handler)

        self.assertEqual(HTTPStatus.SEE_OTHER, handler.status)
        self.assertEqual("ops-admin", repo.created[0]["username"])
        self.assertTrue(verify_admin_password("another-strong-password", repo.created[0]["password_hash"]))
        self.assertNotIn("another-strong-password", repo.created[0]["password_hash"])

    def test_current_account_cannot_be_disabled(self):
        repo = _AccountRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/settings/accounts/7/toggle",
            form={"target_active": "0"},
        )
        app._set_current_admin_user(
            handler,
            {"id": 7, "username": "admin", "role": "super_admin", "is_legacy_basic_auth": False},
        )

        app._handle_admin_account_toggle(handler, "/settings/accounts/7/toggle")

        self.assertEqual([], repo.active_updates)
        self.assertEqual(HTTPStatus.SEE_OTHER, handler.status)
        self.assertIn("message=", handler.header("Location"))

    def test_normal_admin_cannot_create_administrator_accounts(self):
        repo = _AccountRepo()
        app = self._build_app(repo)
        handler = _Handler(
            path="/settings/accounts/create",
            form={"username": "ops-admin", "display_name": "Ops", "password": "strong-password"},
        )
        app._set_current_admin_user(
            handler,
            {"id": 7, "username": "admin", "role": "admin", "is_legacy_basic_auth": False},
        )

        app._handle_admin_account_create(handler)

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual([], repo.created)

    def test_avatar_upload_updates_current_admin_avatar(self):
        repo = _AvatarRepo()
        app = self._build_app(repo)
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"avatar"
        body = (
            b"--test-boundary\r\n"
            b'Content-Disposition: form-data; name="avatar"; filename="avatar.png"\r\n'
            b"Content-Type: image/png\r\n\r\n"
            + png_bytes
            + b"\r\n--test-boundary--\r\n"
        )
        handler = _MultipartHandler(body=body)

        with tempfile.TemporaryDirectory() as temp_dir:
            app.settings.runtime_dir = Path(temp_dir)
            app._set_current_admin_user(
                handler,
                {
                    "id": 7,
                    "username": "admin",
                    "display_name": "Admin",
                    "avatar_path": "",
                    "avatar_url": "",
                    "is_legacy_basic_auth": False,
                },
            )

            app._handle_admin_avatar_upload(handler)

            self.assertEqual(HTTPStatus.OK, handler.status)
            self.assertEqual(1, len(repo.avatar_updates))
            self.assertEqual(7, repo.avatar_updates[0][0])
            saved_relpath = repo.avatar_updates[0][1]
            self.assertTrue(saved_relpath.startswith("avatars/admin_7_"))
            self.assertTrue((Path(temp_dir) / saved_relpath).exists())
            self.assertEqual(saved_relpath, current_admin_user()["avatar_path"])


if __name__ == "__main__":
    unittest.main()
