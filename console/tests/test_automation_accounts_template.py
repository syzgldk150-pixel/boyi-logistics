import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


class AutomationAccountsTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )
        cls.template = cls.env.get_template("automation_accounts.html")

    def _render(self) -> str:
        return self.template.render(
            app_title="Test Console",
            message="",
            message_kind="info",
            account_rows=[
                {
                    "account_id": "ronghui_ops_01",
                    "name": "融辉运营账号 01",
                    "system": "ronghui",
                    "system_label": "TMS融辉",
                    "is_default": True,
                    "is_active": True,
                    "session_capable": True,
                    "login_kind": "image",
                    "has_saved_credentials": True,
                    "has_manual_credentials": True,
                    "has_env_credentials": False,
                    "credential_source": "saved",
                    "credentials_label": "已保存账号密码",
                    "credentials_tone": "success",
                    "status_note": "",
                    "credentials": {
                        "username": "739010002",
                        "phone": "17600001528",
                        "has_saved_credentials": True,
                        "has_manual_credentials": True,
                        "has_env_credentials": False,
                        "credential_source": "saved",
                    },
                    "status": {
                        "status": "authenticated",
                        "label": "已登录",
                        "status_tone": "success",
                        "challenge_type": "sms",
                        "last_validation_at": "2026-05-25 18:29:58",
                    },
                    "status_label": "已登录",
                    "status_tone": "success",
                },
                {
                    "account_id": "price_default",
                    "name": "大祥报价账号",
                    "system": "ronghui",
                    "system_label": "TMS融辉",
                    "is_default": True,
                    "is_active": True,
                    "session_capable": True,
                    "login_kind": "image",
                    "has_saved_credentials": False,
                    "has_manual_credentials": False,
                    "has_env_credentials": False,
                    "credential_source": "",
                    "credentials_label": "未保存账号密码",
                    "credentials_tone": "warning",
                    "status_note": "当前只检测到浏览器登录态，未保存账号密码；登录态失效后需重新登录。",
                    "credentials": {
                        "username": "",
                        "phone": "",
                        "has_saved_credentials": False,
                        "has_manual_credentials": False,
                        "has_env_credentials": False,
                        "credential_source": "",
                    },
                    "status": {
                        "status": "authenticated",
                        "label": "已登录",
                        "status_tone": "success",
                        "challenge_type": "sms",
                        "last_validation_at": "2026-05-31 16:03:49",
                    },
                    "status_label": "登录态有效",
                    "status_tone": "warning",
                }
            ],
            account_groups=[],
            accounts=[],
            account_filter="",
            account_filter_label="",
            account_total_count=2,
            account_system_counts={"ronghui": 2},
            account_tab_systems=["ronghui"],
            account_system_labels={"ronghui": "TMS融辉"},
            account_system_order=["ronghui"],
            account_warning="",
        )

    def test_status_refresh_preserves_password_input_being_edited(self):
        html = self._render()

        self.assertIn("function shouldResetPasswordSavedState(input, saved)", html)
        self.assertIn('input.dataset.passwordDirty === "true"', html)
        self.assertIn("document.activeElement === input", html)
        self.assertIn("setPasswordSavedState(passwordInput, !!credentials.has_saved_credentials)", html)

    def test_account_management_hides_purpose_and_default_account_fields(self):
        html = self._render()

        self.assertIn("大祥报价账号", html)
        self.assertNotIn('name="account_purpose"', html)
        self.assertNotIn("普通TMS账号", html)
        self.assertNotIn("大祥报价</span>", html)
        self.assertNotIn("默认账号", html)
        self.assertIn('<option value="ronghui">TMS融辉</option>', html)
        system_select = html[html.index('name="system"') :]
        system_select = system_select[: system_select.index("</select>")]
        self.assertNotIn('value="price"', system_select)

    def test_authenticated_without_credentials_is_marked_as_session_only(self):
        html = self._render()

        self.assertIn("登录态有效", html)
        self.assertIn("未保存账号密码", html)
        self.assertIn("当前只检测到浏览器登录态", html)


if __name__ == "__main__":
    unittest.main()
