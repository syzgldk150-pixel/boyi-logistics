"""Focused tests extracted from the former TMS runtime aggregate."""

from _tms_runtime_test_support import *  # noqa: F403


class AutomationAccountManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        state_dir = Path(self.tempdir.name) / "state"
        self.patches = [
            patch.object(account_manager_module, "STATE_DIR", state_dir),
            patch.object(account_manager_module, "ACCOUNTS_PATH", state_dir / "automation_accounts.json"),
            patch.object(account_manager_module, "LOCAL_ACCOUNT_DIR", state_dir / "automation_account_credentials"),
        ]
        for item in self.patches:
            item.start()
        self.manager = account_manager_module.AutomationAccountManager()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    def test_local_credentials_preserve_password_and_show_success_status(self):
        result = self.manager.save_credentials(
            "r7_default",
            username="73901001",
            password="r7-secret-pass",
        )
        self.assertTrue(result["has_saved_credentials"])
        self.assertEqual(result["password"], "")

        masked_result = self.manager.save_credentials(
            "r7_default",
            username="73901001",
            password=SAVED_PASSWORD_MASK,
        )
        private = self.manager.private_credentials("r7_default")
        status = self.manager.describe_status("r7_default", validate=False)

        self.assertTrue(masked_result["has_saved_credentials"])
        self.assertEqual(masked_result["password"], "")
        self.assertEqual(private["password"], "r7-secret-pass")
        self.assertEqual(status["status_tone"], "success")
        self.assertEqual(status["label"], "凭据已配置")

    def test_default_ronghui_account_status_maps_to_default_profile(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker:
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "profile": "default",
                    "status": "authenticated",
                    "label": "已登录",
                    "status_tone": "success",
                    "authenticated": True,
                    "pending_code": False,
                    "last_validation_at": "2026-05-22 11:46:00",
                    "last_error_summary": "",
                    "authenticated_at": "2026-05-22 11:45:00",
                    "pending_since": "",
                    "expires_at": "",
                    "has_saved_credentials": True,
                }

        def fake_get_session_broker(profile):
            calls.append(("profile", profile))
            return FakeBroker()

        with patch.object(account_manager_module, "get_session_broker", side_effect=fake_get_session_broker):
            status = self.manager.describe_status("ronghui_default", validate=True, force=True)

        self.assertEqual(calls[0], ("profile", "default"))
        self.assertEqual(calls[1], ("describe", True, True))
        self.assertEqual(status["profile"], "default")
        self.assertEqual(status["account_id"], "ronghui_default")
        self.assertEqual(status["system"], "ronghui")

    def test_force_status_check_auto_logs_in_expired_session(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker:
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "profile": "default",
                    "status": "expired" if validate else "authenticated",
                    "label": "已过期" if validate else "已登录",
                    "status_tone": "error" if validate else "success",
                    "authenticated": False if validate else True,
                    "pending_code": False,
                    "last_validation_at": "2026-06-01 13:53:51" if validate else "2026-06-01 13:52:51",
                    "last_error_summary": "session expired" if validate else "",
                    "authenticated_at": "2026-06-01 12:00:00",
                    "pending_since": "",
                    "expires_at": "",
                    "has_saved_credentials": True,
                }

            def send_code(self):
                calls.append(("send_code",))
                return {
                    "profile": "default",
                    "status": "authenticated",
                    "label": "已登录",
                    "status_tone": "success",
                    "authenticated": True,
                    "pending_code": False,
                    "last_validation_at": "2026-06-01 13:53:52",
                    "last_error_summary": "",
                    "authenticated_at": "2026-06-01 13:53:52",
                    "pending_since": "",
                    "expires_at": "",
                    "has_saved_credentials": True,
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            status = self.manager.check_status_with_auto_login("ronghui_default", force=True)

        self.assertEqual(status["status"], "authenticated")
        self.assertEqual(status["account_id"], "ronghui_default")
        self.assertEqual(status["system"], "ronghui")
        self.assertEqual(
            calls,
            [
                ("describe", True, True),
                ("send_code",),
            ],
        )

    def test_force_status_check_keeps_pending_code_without_resending(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker:
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "profile": "yunda",
                    "status": "pending_code",
                    "label": "待输入验证码",
                    "status_tone": "warning",
                    "authenticated": False,
                    "pending_code": True,
                    "last_validation_at": "",
                    "last_error_summary": "短信验证码已发送",
                    "authenticated_at": "",
                    "pending_since": "2026-06-01 13:53:51",
                    "expires_at": "",
                    "challenge_type": "sms",
                    "has_saved_credentials": True,
                }

            def send_code(self):
                calls.append(("send_code",))
                raise AssertionError("pending_code accounts must not resend code")

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            status = self.manager.check_status_with_auto_login("yunda_default", force=True)

        self.assertEqual(status["status"], "pending_code")
        self.assertEqual(status["account_id"], "yunda_default")
        self.assertEqual(calls, [("describe", True, True)])

    def test_default_accounts_include_daxiang_s_independent_profile(self):
        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }

        self.assertIn("ronghui_daxiang_s", accounts)
        self.assertEqual("TMS大祥S站账号", accounts["ronghui_daxiang_s"]["name"])
        self.assertEqual("daxiang_s", accounts["ronghui_daxiang_s"]["account_purpose"])
        self.assertEqual("大祥S站", accounts["ronghui_daxiang_s"]["account_purpose_label"])
        self.assertEqual("daxiang_s", accounts["ronghui_daxiang_s"]["session_profile"])
        self.assertTrue(accounts["ronghui_daxiang_s"]["is_default"])

    def test_legacy_price_account_migrates_to_ronghui_price_purpose(self):
        account_manager_module.ACCOUNTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        account_manager_module.ACCOUNTS_PATH.write_text(
            json.dumps(
                [
                    {
                        "account_id": "price_default",
                        "system": "price",
                        "name": "大祥报价账号",
                        "is_active": True,
                        "is_default": True,
                        "session_profile": "price",
                        "created_at": "2026-05-01 08:00:00",
                        "updated_at": "2026-05-01 08:00:00",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }

        self.assertEqual("ronghui", accounts["price_default"]["system"])
        self.assertEqual("TMS融辉", accounts["price_default"]["system_label"])
        self.assertEqual("price", accounts["price_default"]["account_purpose"])
        self.assertEqual("大祥报价", accounts["price_default"]["account_purpose_label"])
        self.assertEqual("price", accounts["price_default"]["session_profile"])
        self.assertTrue(accounts["price_default"]["is_default"])
        self.assertNotIn("price", {item["system"] for item in accounts.values()})

    def test_defaults_are_scoped_by_system_and_purpose(self):
        self.manager.create_account(
            account_id="price_backup",
            system="ronghui",
            account_purpose="price",
            name="报价备用账号",
        )

        self.manager.set_default("price_backup")
        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }

        self.assertTrue(accounts["ronghui_default"]["is_default"])
        self.assertTrue(accounts["price_backup"]["is_default"])
        self.assertFalse(accounts["price_default"]["is_default"])
        self.assertEqual("price", accounts["price_backup"]["account_purpose"])
        self.assertEqual("price_price_backup", accounts["price_backup"]["session_profile"])

    def test_resolve_execution_params_uses_price_default_for_ronghui_price_purpose(self):
        params = self.manager.resolve_execution_params(
            {},
            default_system="ronghui",
            default_purpose="price",
        )

        self.assertEqual("price_default", params["account_id"])
        self.assertEqual("price", params["session_profile"])

    def test_resolve_role_account_params_injects_r13_credentials(self):
        self.manager.save_credentials(
            "r13_default",
            username="r13-user",
            password="r13-pass",
        )

        params = self.manager.resolve_role_account_params(
            {"r13_account_id": "r13_default", "days": 1},
            account_field="r13_account_id",
            output_account_field="",
            output_session_profile_field="",
        )

        self.assertEqual("r13-user", params["username"])
        self.assertEqual("r13-pass", params["password"])
        self.assertEqual("r13_default", params["r13_account_id"])
        self.assertEqual(1, params["days"])

    def test_account_login_response_includes_context_and_hides_password(self):
        class FakeBroker:
            def send_code(self):
                return {
                    "status": "authenticated",
                    "authenticated": True,
                    "pending_code": False,
                    "profile": "default",
                    "password": "secret-value",
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            result = self.manager.login("ronghui_default")

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(result["account_id"], "ronghui_default")
        self.assertEqual(result["account_name"], "TMS融辉默认账号")
        self.assertEqual(result["system"], "ronghui")
        self.assertEqual(result["system_label"], "TMS融辉")
        self.assertEqual(result["account_purpose"], "general")
        self.assertEqual(result["session_profile"], "default")
        self.assertTrue(result["session_capable"])
        self.assertNotIn("password", result)

    def test_account_submit_code_response_includes_context_and_hides_password(self):
        class FakeBroker:
            def submit_code(self, code):
                return {
                    "status": "authenticated",
                    "authenticated": True,
                    "submitted_length": len(code),
                    "password": "secret-value",
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            result = self.manager.submit_code("ronghui_default", "123456")

        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(result["submitted_length"], 6)
        self.assertEqual(result["account_id"], "ronghui_default")
        self.assertEqual(result["session_profile"], "default")
        self.assertNotIn("password", result)
