import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import LocalDocFlowApp


class AutomationTMSSessionTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )
        cls.template = cls.env.get_template("automation.html")

    def _render(
        self,
        status: str,
        label: str,
        error_text: str = "",
        *,
        username: str = "",
        password: str = "",
        phone: str = "",
        has_saved_credentials: bool | None = None,
        automation_db_warning: str = "",
        challenge_type: str = "sms",
        challenge_label: str = "短信验证码",
        captcha_image: str = "",
    ) -> str:
        if has_saved_credentials is None:
            has_saved_credentials = bool(username and password and phone)
        return self.template.render(
            app_title="Test Console",
            message="",
            message_kind="info",
            settings={},
            scheduled_tasks=[],
            scheduled_task_count=0,
            enabled_task_count=0,
            automation_db_warning=automation_db_warning,
            tms_session_status={
                "status": status,
                "label": label,
                "status_tone": "success" if status == "authenticated" else "warning" if status == "pending_code" else "neutral" if status == "logged_out" else "error",
                "last_validation_at": "2026-04-22 12:00:00",
                "last_error_summary": error_text,
                "authenticated_at": "2026-04-22 11:59:00" if status == "authenticated" else "",
                "pending_since": "2026-04-22 11:58:00" if status == "pending_code" else "",
                "expires_at": "2026-04-23 11:59:00" if status == "authenticated" else "",
                "has_saved_credentials": has_saved_credentials,
                "challenge_type": challenge_type,
                "challenge_label": challenge_label,
                "captcha_image": captcha_image,
            },
            tms_session_credentials={
                "username": username,
                "password": password,
                "phone": phone,
                "updated_at": "2026-04-22 12:00:00" if has_saved_credentials else "",
                "has_saved_credentials": has_saved_credentials,
            },
        )

    def test_template_renders_tms_session_forms(self):
        html = self._render("logged_out", "未登录")
        self.assertIn("/automations/tms-session/send-code", html)
        self.assertIn("/automations/tms-session/submit-code", html)
        self.assertIn("/automations/tms-session/clear", html)
        self.assertIn("/automations/tms-session/save-credentials", html)
        self.assertIn("/automations/tms-session/clear-credentials", html)
        self.assertIn('data-status-url="/automations/tms-session/status"', html)

    def test_template_renders_authenticated_state(self):
        html = self._render("authenticated", "已登录", username="saved-user", password="saved-pass", phone="13800000000")
        self.assertIn("已登录", html)
        self.assertIn("验证码共享登录态", html)
        self.assertIn('value="saved-user"', html)
        self.assertIn('value="saved-pass"', html)
        self.assertIn('value="13800000000"', html)
        self.assertIn("默认登录配置", html)

    def test_template_renders_pending_code_state(self):
        html = self._render("pending_code", "待输入验证码", "等待输入短信验证码")
        self.assertIn("待输入验证码", html)
        self.assertIn("等待输入短信验证码", html)

    def test_template_renders_pending_image_captcha(self):
        html = self._render(
            "pending_code",
            "待输入验证码",
            "请输入图片验证码",
            challenge_type="image",
            challenge_label="图片验证码",
            captcha_image="data:image/png;base64,abc",
        )

        self.assertIn("请输入 TMS融辉 图片验证码", html)
        self.assertIn('src="data:image/png;base64,abc"', html)

    def test_status_normalization_preserves_captcha_image(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)

        state = app._normalize_tms_session_status_payload(
            {
                "ok": True,
                "status": "pending_code",
                "challenge_type": "image",
                "challenge_label": "图片验证码",
                "captcha_image": "data:image/png;base64,abc",
                "captcha_image_mime": "image/png",
                "captcha_captured_at": "2026-05-20 12:00:00",
            }
        )

        self.assertEqual(state["challenge_type"], "image")
        self.assertEqual(state["captcha_image"], "data:image/png;base64,abc")
        self.assertEqual(state["captcha_image_mime"], "image/png")

    def test_template_renders_expired_state(self):
        html = self._render("expired", "已过期", "登录态已失效，请重新发送验证码。")
        self.assertIn("已过期", html)
        self.assertIn("登录态已失效，请重新发送验证码。", html)

    def test_template_disables_send_code_without_saved_credentials(self):
        html = self._render("logged_out", "未登录", has_saved_credentials=False)
        self.assertIn("发送验证码", html)
        self.assertIn("disabled", html)


    def test_template_renders_degraded_database_warning(self):
        html = self._render(
            "logged_out",
            "未登录",
            automation_db_warning="db degraded",
        )
        self.assertIn("auto-feedback--warning", html)
        self.assertIn("db degraded", html)


    def test_template_renders_auto_login_fallback_message(self):
        html = self._render(
            "pending_code",
            "å¾…è¾“å…¥éªŒè¯ç ",
            "è‡ªåŠ¨è¯†åˆ«å¤±è´¥ 4 æ¬¡ï¼Œè¯·äººå·¥è¾“å…¥æˆ–åˆ·æ–°éªŒè¯ç åŽé‡è¯•ã€‚",
            challenge_type="image",
            challenge_label="å›¾ç‰‡éªŒè¯ç ",
            captcha_image="data:image/png;base64,abc",
        )

        self.assertIn("è‡ªåŠ¨è¯†åˆ«å¤±è´¥ 4 æ¬¡", html)

    def test_template_disables_send_code_without_saved_credentials(self):
        html = self._render("logged_out", "æœªç™»å½•", has_saved_credentials=False, challenge_type="")
        self.assertIn("ç™»å½•", html)
        self.assertIn("disabled", html)

    def test_template_renders_multi_role_account_binding_controls(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_base_url="http://agent.local")
        app.automation_virtual_task_state = {}
        task = app._build_virtual_automation_task("self_pickup_problem_upload")
        app._enrich_automation_tasks_with_accounts(
            [task],
            [
                {
                    "account_id": "ronghui_self_pickup_problem",
                    "system": "ronghui",
                    "name": "自提部账号",
                    "is_active": True,
                    "status_label": "已登录",
                },
                {
                    "account_id": "ronghui_daxiang_s",
                    "system": "ronghui",
                    "name": "大祥S站账号",
                    "is_active": True,
                    "status_label": "未登录",
                },
            ],
        )

        html = self.template.render(
            app_title="Test Console",
            message="",
            message_kind="info",
            settings={},
            scheduled_tasks=[task],
            scheduled_task_count=1,
            enabled_task_count=0,
            automation_provider_labels={"ronghui": "TMS融辉", "yunda": "韵达"},
            automation_provider_counts={"ronghui": 1, "yunda": 0},
            automation_provider_enabled_counts={"ronghui": 0, "yunda": 0},
            automation_db_warning="",
            automation_account_warning="",
            tms_session_status={"status": "logged_out", "label": "未登录", "status_tone": "neutral"},
            tms_session_credentials={"has_saved_credentials": False},
        )

        self.assertIn("自提部账号", html)
        self.assertIn("大祥S站账号", html)
        self.assertIn('data-json-path="account_id"', html)
        self.assertIn('data-json-path="daxiang_s_account_id"', html)
        self.assertIn('name="account_role__account_id"', html)
        self.assertIn('name="account_role__daxiang_s_account_id"', html)


if __name__ == "__main__":
    unittest.main()
