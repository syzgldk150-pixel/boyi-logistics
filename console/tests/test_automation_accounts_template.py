import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


CONSOLE_DIR = Path(__file__).resolve().parents[1]


class AutomationAccountsTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = CONSOLE_DIR / "templates"
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
                    "auto_login_enabled": True,
                    "auto_login_blocked": False,
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
                    "auto_login_enabled": False,
                    "auto_login_blocked": False,
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
                },
                {
                    "account_id": "ronghui_paused",
                    "name": "融辉已暂停账号",
                    "system": "ronghui",
                    "system_label": "TMS融辉",
                    "is_default": False,
                    "is_active": True,
                    "auto_login_enabled": True,
                    "auto_login_blocked": True,
                    "session_capable": True,
                    "login_kind": "image",
                    "has_saved_credentials": True,
                    "has_manual_credentials": True,
                    "has_env_credentials": False,
                    "credential_source": "saved",
                    "credentials_label": "已保存账号密码",
                    "credentials_tone": "success",
                    "status_note": "连续失败后已暂停。",
                    "credentials": {
                        "username": "paused-test-user",
                        "phone": "",
                        "has_saved_credentials": True,
                        "has_manual_credentials": True,
                        "has_env_credentials": False,
                        "credential_source": "saved",
                    },
                    "status": {
                        "status": "pending_code",
                        "label": "自动登录已暂停",
                        "status_tone": "warning",
                        "challenge_type": "image",
                        "captcha_image": "data:image/png;base64,stale-captcha",
                    },
                    "status_label": "自动登录已暂停",
                    "status_tone": "warning",
                },
            ],
            account_groups=[],
            accounts=[],
            account_filter="",
            account_filter_label="",
            account_total_count=3,
            account_system_counts={"ronghui": 3},
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

    def test_account_note_is_editable_and_updates_without_login_status_check(self):
        html = self._render()

        self.assertIn('data-account-name>融辉运营账号 01</span>', html)
        self.assertIn('action="/automation-accounts/ronghui_ops_01/name"', html)
        self.assertIn('data-account-name-form', html)
        self.assertIn('data-account-name-input', html)
        self.assertIn('maxlength="80"', html)
        self.assertIn('value="融辉运营账号 01"', html)
        self.assertIn("显示在所属系统下方，用于区分账号用途。", html)
        self.assertIn('row.querySelector("[data-account-name]")', html)
        self.assertIn("if (!payload.state && !isNameForm) await refreshRowStatus(row);", html)

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

    def test_account_page_exposes_clear_auto_login_and_disable_semantics(self):
        html = self._render()
        template = (CONSOLE_DIR / "templates" / "automation_accounts.html").read_text(encoding="utf-8")

        self.assertIn("<th>自动登录</th>", html)
        self.assertIn('role="switch"', html)
        self.assertIn('aria-checked="true"', html)
        self.assertIn("data-account-auto-login-switch", html)
        self.assertIn("定时校验与掉线恢复", html)
        self.assertIn("保存凭据后可立即登录", html)
        self.assertIn("仅关闭监控，仍可手动登录", template)
        self.assertIn("立即执行一次登录；自动登录开关只控制定时校验与掉线恢复", html)
        self.assertIn("重新登录", html)
        self.assertIn('name="auto_login_enabled"', html)
        self.assertIn("清除会话并关闭自动登录", html)
        self.assertIn("清空账号密码并关闭自动登录", html)
        self.assertIn("停止任务使用与登录监控", html)
        self.assertNotIn("环境变量凭据", html)
        self.assertNotIn("不会删除部署环境变量", html)

    def test_paused_auto_login_does_not_render_stale_captcha_or_code_entry(self):
        html = self._render()
        template = (CONSOLE_DIR / "templates" / "automation_accounts.html").read_text(encoding="utf-8")

        paused_row = html[html.index('data-account-id="ronghui_paused"') :]
        paused_row = paused_row[: paused_row.index("</tr>")]
        self.assertIn('data-account-code-form hidden', paused_row)
        self.assertNotIn("stale-captcha", paused_row)
        self.assertIn("has_active_challenge = status_value == 'pending_code' and not account.auto_login_blocked", template)
        self.assertIn('const hasActiveChallenge = pending && row.dataset.accountAutoLoginBlocked !== "true";', template)

    def test_disabled_badge_is_hidden_for_active_accounts_and_action_is_reversible(self):
        html = self._render()
        template = (CONSOLE_DIR / "templates" / "automation_accounts.html").read_text(encoding="utf-8")
        stylesheet = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertEqual(3, html.count("data-account-disabled-badge hidden>已停用</span>"))
        self.assertIn(".automation-account-mini-badge[hidden] { display: none !important; }", stylesheet)
        self.assertIn("'停用账号' if account.is_active else '重新启用账号'", template)
        self.assertIn('active ? "停用账号" : "重新启用账号"', template)
        self.assertIn("恢复任务使用与登录监控", template)


if __name__ == "__main__":
    unittest.main()
