import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from console.routes import automation as automation_routes


class AutomationAccountBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.template_path = template_dir / "automation.html"
        cls.template = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        ).get_template("automation.html")

    def _render(self, *, automation_db_warning: str = "") -> str:
        return self.template.render(
            app_title="Test Console",
            message="",
            message_kind="info",
            scheduled_tasks=[],
            scheduled_task_count=0,
            enabled_task_count=0,
            automation_db_warning=automation_db_warning,
            automation_account_warning="",
            automation_approval_policy_warning="",
            automation_plugin_warning="",
            automation_plugin_packages=[],
            unsupported_automation_ids=[],
            can_manage_plugins=True,
            can_manage_approval_policies=True,
            # Passing legacy values proves the page no longer renders them.
            tms_session_status={
                "status": "authenticated",
                "label": "SHOULD_NOT_RENDER_SESSION_STATUS",
                "captcha_image": "data:image/png;base64,SHOULD_NOT_RENDER_IMAGE",
            },
            tms_session_credentials={
                "username": "SHOULD_NOT_RENDER_USERNAME",
                "password": "SHOULD_NOT_RENDER_PASSWORD",
                "phone": "SHOULD_NOT_RENDER_PHONE",
            },
        )

    def test_automation_page_does_not_render_login_status_or_credentials(self):
        html = self._render()
        source = self.template_path.read_text(encoding="utf-8")

        for forbidden in (
            "SHOULD_NOT_RENDER_SESSION_STATUS",
            "SHOULD_NOT_RENDER_IMAGE",
            "SHOULD_NOT_RENDER_USERNAME",
            "SHOULD_NOT_RENDER_PASSWORD",
            "SHOULD_NOT_RENDER_PHONE",
            "/automations/tms-session",
            "/automations/yunda-session",
            "data-tms-session",
            "tms_session_status",
            "tms_session_credentials",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, html)
                self.assertNotIn(forbidden, source)
        self.assertNotIn("账号设置", html)
        self.assertNotIn("账号管理", html)

    def test_database_warning_no_longer_promises_a_top_tms_login_fallback(self):
        html = self._render(automation_db_warning="数据库暂不可达")

        self.assertIn("数据库暂不可达", html)
        self.assertNotIn("顶部 TMS", html)
        self.assertNotIn("顶部TMS", html)

    def test_hidden_legacy_session_routes_are_not_dispatched(self):
        app = SimpleNamespace(_handle_automation_account_post=lambda *_args: False)
        legacy_paths = (
            "/automations/session-context",
            "/automations/tms-session/status",
            "/automations/tms-session/send-code",
            "/automations/tms-session/submit-code",
            "/automations/tms-session/clear",
            "/automations/tms-session/save-credentials",
            "/automations/tms-session/clear-credentials",
            "/automations/yunda-session/status",
            "/automations/yunda-session/login",
            "/automations/yunda-session/clear",
        )

        for path in legacy_paths:
            with self.subTest(method="GET", path=path):
                self.assertFalse(automation_routes.handle_get(app, object(), path, path, {}))
            with self.subTest(method="POST", path=path):
                self.assertFalse(automation_routes.handle_post(app, object(), path, path, {}))


if __name__ == "__main__":
    unittest.main()
