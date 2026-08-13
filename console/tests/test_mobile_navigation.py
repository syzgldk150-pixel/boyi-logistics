import io
import json
import sys
import types
import unittest
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path


CONSOLE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CONSOLE_DIR.parent
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from console.navigation import (  # noqa: E402
    CONSOLE_NAVIGATION,
    DEFAULT_MOBILE_BOTTOM_NAV,
    MOBILE_NAVIGATION_CANDIDATES,
    MobileNavigationValidationError,
    mobile_bottom_nav_for_user,
    serialize_mobile_bottom_nav,
    validate_mobile_bottom_nav,
)


class _Handler:
    def __init__(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)


class _PreferenceRepository:
    def __init__(self):
        self.users = {
            7: {"id": 7, "username": "admin-a", "ui_preferences_json": '{"theme":"system"}'},
            8: {"id": 8, "username": "admin-b", "ui_preferences_json": '{"theme":"dark"}'},
        }
        self.updates = []

    def get_admin_user(self, user_id):
        user = self.users.get(user_id)
        return dict(user) if user else None

    def update_admin_ui_preferences(self, user_id, preferences_json):
        if user_id not in self.users:
            return False
        self.users[user_id]["ui_preferences_json"] = preferences_json
        self.updates.append((user_id, preferences_json))
        return True


class _RecordingCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def execute(self, query, parameters):
        self.executed.append((query, parameters))


class _RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class MobileNavigationTests(unittest.TestCase):
    def _build_app(self, repository):
        from app import LocalDocFlowApp

        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.repository = repository
        app.sent_status = None
        app.sent_payload = None

        def send_json(instance, _handler, status, payload):
            instance.sent_status = status
            instance.sent_payload = payload

        app._send_json = types.MethodType(send_json, app)
        return app

    def _with_user(self, user):
        from console.app_support import _CURRENT_ADMIN_USER

        return _CURRENT_ADMIN_USER.set(user)

    def _reset_user(self, token):
        from console.app_support import _CURRENT_ADMIN_USER

        _CURRENT_ADMIN_USER.reset(token)

    def test_navigation_directory_is_unique_and_default_order_is_stable(self):
        routes = [item["route"] for item in CONSOLE_NAVIGATION]

        self.assertEqual(len(routes), len(set(routes)))
        self.assertIn("/work-items", routes)
        self.assertEqual(("/tracking", "/receipts", "/automations"), DEFAULT_MOBILE_BOTTOM_NAV)
        self.assertTrue(all(route != "/" for route in DEFAULT_MOBILE_BOTTOM_NAV))
        self.assertTrue(all(item["route"] != "/" for item in MOBILE_NAVIGATION_CANDIDATES))
        self.assertNotIn("/templates/new", routes)
        console_ui = (CONSOLE_DIR / "static" / "console_ui.js").read_text(encoding="utf-8")
        self.assertNotIn('pathname.startsWith("/templates")', console_ui)

    def test_preferences_are_read_safely_and_preserve_unrelated_keys_on_write(self):
        user = {"ui_preferences_json": '{"mobile_bottom_nav":["/receipts","/tracking","/ocr"]}'}

        self.assertEqual(("/receipts", "/tracking", "/ocr"), mobile_bottom_nav_for_user(user))
        self.assertEqual(DEFAULT_MOBILE_BOTTOM_NAV, mobile_bottom_nav_for_user({"ui_preferences_json": "not-json"}))
        serialized = serialize_mobile_bottom_nav('{"theme":"dark"}', ["/ocr", "/tracking", "/receipts"])
        self.assertEqual(
            {"mobile_bottom_nav": ["/ocr", "/tracking", "/receipts"], "theme": "dark"},
            json.loads(serialized),
        )

    def test_validation_rejects_count_duplicates_and_unknown_routes(self):
        self.assertEqual(
            ("/tracking", "/receipts", "/automations"),
            validate_mobile_bottom_nav(["/tracking", "/receipts", "/automations"]),
        )
        for routes in (
            ["/tracking", "/receipts"],
            ["/tracking", "/tracking", "/automations"],
            ["/tracking", "/receipts", "/missing"],
            "/tracking",
        ):
            with self.assertRaises(MobileNavigationValidationError):
                validate_mobile_bottom_nav(routes)

    def test_save_updates_only_the_authenticated_administrator(self):
        repository = _PreferenceRepository()
        app = self._build_app(repository)
        handler = _Handler({"routes": ["/ocr", "/tracking", "/receipts"]})
        token = self._with_user({"id": 7, "username": "admin-a", "is_legacy_basic_auth": False})
        try:
            app._handle_mobile_navigation_save(handler)
        finally:
            self._reset_user(token)

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual({"routes": ["/ocr", "/tracking", "/receipts"]}, app.sent_payload["data"])
        self.assertEqual([7], [user_id for user_id, _preferences in repository.updates])
        self.assertEqual('{"theme":"dark"}', repository.users[8]["ui_preferences_json"])
        self.assertEqual(
            ["/ocr", "/tracking", "/receipts"],
            json.loads(repository.users[7]["ui_preferences_json"])["mobile_bottom_nav"],
        )

    def test_save_rejects_invalid_payload_without_writing(self):
        repository = _PreferenceRepository()
        app = self._build_app(repository)
        handler = _Handler({"routes": ["/tracking", "/tracking", "/receipts"]})
        token = self._with_user({"id": 7, "username": "admin-a", "is_legacy_basic_auth": False})
        try:
            app._handle_mobile_navigation_save(handler)
        finally:
            self._reset_user(token)

        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)
        self.assertEqual("INVALID_MOBILE_NAVIGATION", app.sent_payload["error"]["code"])
        self.assertEqual([], repository.updates)

    def test_basic_auth_cannot_silently_fall_back_to_local_storage(self):
        repository = _PreferenceRepository()
        app = self._build_app(repository)
        handler = _Handler({"routes": ["/ocr", "/tracking", "/receipts"]})
        token = self._with_user({"id": 0, "username": "emergency", "is_legacy_basic_auth": True})
        try:
            app._handle_mobile_navigation_save(handler)
        finally:
            self._reset_user(token)

        self.assertEqual(HTTPStatus.FORBIDDEN, app.sent_status)
        self.assertEqual("MOBILE_NAVIGATION_SYNC_UNAVAILABLE", app.sent_payload["error"]["code"])
        self.assertEqual([], repository.updates)

    def test_repository_write_is_parameterized_and_idempotent_for_no_change(self):
        from database import DocumentRepository

        cursor = _RecordingCursor()
        repository = DocumentRepository.__new__(DocumentRepository)

        @contextmanager
        def connect():
            yield _RecordingConnection(cursor)

        repository.connect = connect
        self.assertTrue(repository.update_admin_ui_preferences(7, '{"mobile_bottom_nav":["/tracking","/receipts","/automations"]}'))

        query, parameters = cursor.executed[0]
        self.assertIn("UPDATE admin_users", query)
        self.assertIn("WHERE id = %s", query)
        self.assertEqual(7, parameters[-1])

    def test_migration_is_registered_and_idempotent_without_runtime_ddl(self):
        migration = PROJECT_ROOT / "agent" / "migrations" / "008_admin_ui_preferences.sql"
        source = migration.read_text(encoding="utf-8")
        from agent.scripts.run_migrations import discover_migrations, split_sql_statements

        self.assertTrue(migration.is_file())
        self.assertIn("information_schema.COLUMNS", source)
        self.assertIn("ADD COLUMN ui_preferences_json LONGTEXT NOT NULL", source)
        self.assertIn("PREPARE migration_statement", source)
        self.assertIn("EXECUTE migration_statement", source)
        self.assertIn(("008", migration), discover_migrations())
        self.assertEqual(5, len(split_sql_statements(source)))

    def test_logo_and_mobile_shell_do_not_retain_the_old_svg_mark(self):
        base = (CONSOLE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
        login = (CONSOLE_DIR / "templates" / "login.html").read_text(encoding="utf-8")

        self.assertTrue((CONSOLE_DIR / "static" / "assets" / "boyi-logistics-logo-7e1f2994.webp").is_file())
        self.assertIn('viewport-fit=cover', base)
        self.assertIn('data-mobile-bottom-nav', base)
        self.assertIn('data-mobile-more-sheet', base)
        self.assertIn('/static/assets/boyi-logistics-logo-7e1f2994.webp', base)
        self.assertIn('/static/assets/boyi-logistics-logo-7e1f2994.webp', login)
        self.assertNotIn("M13 2L3 14h9l-1 8 10-12h-9l1-8z", base)
        self.assertNotIn("M13 2L3 14h9l-1 8 10-12h-9l1-8z", login)

    def test_authenticated_route_delegates_to_the_mobile_navigation_handler(self):
        route_source = (CONSOLE_DIR / "routes" / "auth.py").read_text(encoding="utf-8")

        self.assertIn('if path == "/settings/profile/mobile-navigation":', route_source)
        self.assertIn("app._handle_mobile_navigation_save(handler)", route_source)


if __name__ == "__main__":
    unittest.main()
