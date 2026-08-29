"""Focused tests extracted from the former TMS runtime aggregate."""

import json
import threading
import time

from _tms_runtime_test_support import *  # noqa: F403
from agent.tms_runtime.errors import TMSAuthStateError


class ManualCredentialsBroker:
    def get_manual_credentials(self):
        return {
            "username": "saved-user",
            "password": "",
            "phone": "",
            "updated_at": "2026-06-01 12:00:00",
            "has_saved_credentials": True,
            "has_manual_credentials": True,
            "has_env_credentials": False,
            "credential_source": "saved",
        }


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

    def test_active_binding_descriptor_is_local_and_contains_routing_fields(self):
        with patch.object(
            account_manager_module,
            "get_session_broker",
            side_effect=AssertionError("local binding lookup must not construct a broker"),
        ):
            descriptor = self.manager.require_active_binding_descriptor(
                "ronghui_default"
            )

        self.assertEqual(
            descriptor,
            {
                "account_id": "ronghui_default",
                "system": "ronghui",
                "account_purpose": "general",
                "session_profile": "default",
            },
        )

    def test_active_binding_descriptor_rejects_disabled_account(self):
        self.manager.set_active("ronghui_default", False)

        with self.assertRaises(TMSAuthStateError) as error:
            self.manager.require_active_binding_descriptor("ronghui_default")

        self.assertEqual(error.exception.code, "ACCOUNT_DISABLED")

    def test_r13_default_binding_descriptor_owns_daxiang_s_site(self):
        descriptor = self.manager.require_active_binding_descriptor("r13_default")

        self.assertEqual("r13", descriptor["system"])
        self.assertEqual("7390017", descriptor["site_code"])

    def test_legacy_r13_default_row_is_enriched_from_account_contract(self):
        account_manager_module._write_json(
            account_manager_module.ACCOUNTS_PATH,
            [
                {
                    "account_id": "r13_default",
                    "system": "r13",
                    "name": "R13默认账号",
                    "account_purpose": "general",
                    "is_default": True,
                    "is_active": True,
                }
            ],
        )

        descriptor = self.manager.require_active_binding_descriptor("r13_default")
        stored = account_manager_module._read_json(
            account_manager_module.ACCOUNTS_PATH,
            [],
        )

        self.assertEqual("7390017", descriptor["site_code"])
        stored_r13 = next(
            row for row in stored if row["account_id"] == "r13_default"
        )
        self.assertEqual("7390017", stored_r13["site_code"])

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
        self.assertEqual(status["status"], "logged_out")
        self.assertEqual(status["label"], "已退出")
        self.assertTrue(status["session_capable"])

    def test_credentials_change_guard_covers_local_save_and_clear(self):
        credential_path = account_manager_module._local_credential_path("r7_default")
        guard_calls = []
        active_changes = set()

        def begin_change(account_id):
            self.assertNotIn(account_id, active_changes)
            active_changes.add(account_id)
            guard_calls.append(("begin", account_id, credential_path.exists()))

            def finish_change():
                guard_calls.append(("finish", account_id, credential_path.exists()))
                active_changes.remove(account_id)

            return finish_change

        self.manager.set_credentials_change_guard(begin_change)

        self.manager.save_credentials(
            "r7_default",
            username="r7-user",
            password="r7-password",
        )
        self.manager.clear_credentials("r7_default")

        self.assertEqual(
            guard_calls,
            [
                ("begin", "r7_default", False),
                ("finish", "r7_default", True),
                ("begin", "r7_default", True),
                ("finish", "r7_default", False),
            ],
        )
        self.assertEqual(active_changes, set())
        self.assertFalse(credential_path.exists())

    def test_credentials_change_guard_cleanup_runs_when_file_write_fails(self):
        active_changes = set()

        def begin_change(account_id):
            active_changes.add(account_id)

            def finish_change():
                active_changes.remove(account_id)

            return finish_change

        self.manager.set_credentials_change_guard(begin_change)
        with patch.object(
            account_manager_module,
            "_write_json",
            side_effect=OSError("synthetic file write failure"),
        ):
            with self.assertRaises(OSError):
                self.manager.save_credentials(
                    "r7_default",
                    username="replacement-user",
                    password="replacement-password",
                )

        self.assertEqual(active_changes, set())

    def test_credentials_change_guard_cleanup_runs_when_broker_save_or_clear_fails(self):
        class FailingBroker:
            @staticmethod
            def save_credentials(**_credentials):
                raise RuntimeError("synthetic broker save failure")

            @staticmethod
            def clear_saved_credentials():
                raise RuntimeError("synthetic broker clear failure")

        active_changes = set()
        guard_calls = []

        def begin_change(account_id):
            active_changes.add(account_id)
            guard_calls.append(("begin", account_id))

            def finish_change():
                guard_calls.append(("finish", account_id))
                active_changes.remove(account_id)

            return finish_change

        self.manager.set_credentials_change_guard(begin_change)
        with patch.object(
            account_manager_module,
            "get_session_broker",
            return_value=FailingBroker(),
        ):
            with self.assertRaises(RuntimeError):
                self.manager.save_credentials(
                    "ronghui_default",
                    username="replacement-user",
                    password="replacement-password",
                )
            self.assertEqual(active_changes, set())
            with self.assertRaises(RuntimeError):
                self.manager.clear_credentials("ronghui_default")

        self.assertEqual(active_changes, set())
        self.assertEqual(
            guard_calls,
            [
                ("begin", "ronghui_default"),
                ("finish", "ronghui_default"),
                ("begin", "ronghui_default"),
                ("finish", "ronghui_default"),
            ],
        )

    def test_credentials_change_guard_failure_preserves_existing_credentials(self):
        self.manager.save_credentials(
            "r7_default",
            username="original-user",
            password="original-password",
        )

        def fail_closed(_account_id):
            raise RuntimeError("synthetic policy store failure")

        self.manager.set_credentials_change_guard(fail_closed)
        with self.assertRaises(TMSAuthStateError) as save_error:
            self.manager.save_credentials(
                "r7_default",
                username="replacement-user",
                password="replacement-password",
            )
        with self.assertRaises(TMSAuthStateError) as clear_error:
            self.manager.clear_credentials("r7_default")

        private = self.manager.private_credentials("r7_default")
        self.assertEqual(
            save_error.exception.code,
            "CREDENTIAL_POLICY_REVOCATION_FAILED",
        )
        self.assertEqual(
            clear_error.exception.code,
            "CREDENTIAL_POLICY_REVOCATION_FAILED",
        )
        self.assertEqual(private["username"], "original-user")
        self.assertEqual(private["password"], "original-password")

    def test_invalid_credentials_never_start_policy_change_guard(self):
        guard_calls = []

        def begin_change(account_id):
            guard_calls.append(account_id)
            return lambda: None

        self.manager.set_credentials_change_guard(begin_change)

        with self.assertRaises(TMSAuthStateError) as error:
            self.manager.save_credentials(
                "r7_default",
                username="",
                password="",
            )

        self.assertEqual(error.exception.code, "AUTH_REQUIRED")
        self.assertEqual(guard_calls, [])

    def test_credentials_change_guard_failure_never_calls_tms_broker_writes(self):
        class RecordingBroker:
            def __init__(self):
                self.save_calls = []
                self.clear_calls = 0

            def save_credentials(self, **credentials):
                self.save_calls.append(dict(credentials))

            def clear_saved_credentials(self):
                self.clear_calls += 1

        broker = RecordingBroker()

        def fail_closed(_account_id):
            raise RuntimeError("synthetic policy store failure")

        self.manager.set_credentials_change_guard(fail_closed)
        with patch.object(
            account_manager_module,
            "get_session_broker",
            return_value=broker,
        ):
            with self.assertRaises(TMSAuthStateError) as save_error:
                self.manager.save_credentials(
                    "ronghui_default",
                    username="replacement-user",
                    password="replacement-password",
                )
            with self.assertRaises(TMSAuthStateError) as clear_error:
                self.manager.clear_credentials("ronghui_default")

        self.assertEqual(
            save_error.exception.code,
            "CREDENTIAL_POLICY_REVOCATION_FAILED",
        )
        self.assertEqual(
            clear_error.exception.code,
            "CREDENTIAL_POLICY_REVOCATION_FAILED",
        )
        self.assertEqual(broker.save_calls, [])
        self.assertEqual(broker.clear_calls, 0)

    def test_r7_and_r13_share_unified_login_auto_login_and_logout_controls(self):
        class FakeSSOAuth:
            def __init__(self):
                self.authenticated = False
                self.login_calls: list[dict[str, Any]] = []
                self.clear_calls = 0

            def _verify_authenticated(self):
                return self.authenticated

            def login_and_get_session(self, **kwargs):
                self.login_calls.append(dict(kwargs))
                self.authenticated = True
                return object()

            def clear_persisted_session(self):
                self.clear_calls += 1
                self.authenticated = False

            def persisted_status(self, *, validate, validator, attach_bearer=True):
                if validate and self.authenticated:
                    self.authenticated = bool(validator())
                return {
                    "status": "authenticated" if self.authenticated else "logged_out",
                    "label": "已登录" if self.authenticated else "已退出",
                    "status_tone": "success" if self.authenticated else "neutral",
                    "authenticated": self.authenticated,
                    "pending_code": False,
                    "last_validation_at": "2026-08-11 18:30:00" if validate else "",
                    "last_error_summary": "",
                    "authenticated_at": "2026-08-11 18:29:00" if self.authenticated else "",
                    "pending_since": "",
                    "expires_at": "",
                    "challenge_type": "",
                    "challenge_label": "",
                }

        for account_id in ("r7_default", "r13_default"):
            with self.subTest(account_id=account_id):
                auth = FakeSSOAuth()
                self.manager.save_credentials(
                    account_id,
                    username=f"{account_id}-user",
                    password=f"{account_id}-password",
                )
                with patch.object(self.manager, "_sso_auth", return_value=auth):
                    logged_in = self.manager.login(account_id)
                    auto_login = self.manager.set_auto_login(account_id, True)
                    checked = self.manager.describe_status(account_id, validate=True, force=True)
                    credentials = self.manager.clear_credentials(account_id)
                    still_logged_in = self.manager.describe_status(account_id, validate=False)
                    logged_out = self.manager.clear_session(account_id)

                self.assertEqual("authenticated", logged_in["status"])
                self.assertTrue(logged_in["session_capable"])
                self.assertTrue(auto_login["auto_login_enabled"])
                self.assertEqual("authenticated", checked["status"])
                self.assertFalse(credentials["has_saved_credentials"])
                self.assertEqual("authenticated", still_logged_in["status"])
                self.assertFalse(still_logged_in["auto_login_enabled"])
                self.assertEqual("logged_out", logged_out["status"])
                self.assertFalse(logged_out["auto_login_enabled"])
                self.assertEqual(1, auth.clear_calls)
                self.assertEqual(1, len(auth.login_calls))
                self.assertEqual(1, auth.login_calls[0]["max_attempts"])
                self.assertTrue(auth.login_calls[0]["exchange"])
                self.assertTrue(auth.login_calls[0]["verify"])

    def test_sso_manual_login_failure_stops_after_one_attempt_and_returns_account_error(self):
        class FailingSSOAuth:
            def __init__(self):
                self.calls = 0

            def login_and_get_session(self, **kwargs):
                self.calls += 1
                raise RuntimeError("synthetic upstream failure")

        auth = FailingSSOAuth()
        self.manager.save_credentials(
            "r7_default",
            username="r7-user",
            password="r7-password",
        )

        with patch.object(self.manager, "_sso_auth", return_value=auth):
            with self.assertRaises(TMSAuthStateError) as raised:
                self.manager.login("r7_default")

        self.assertEqual("LOGIN_FAILED", raised.exception.code)
        self.assertEqual(1, auth.calls)
        self.assertNotIn("synthetic upstream failure", str(raised.exception))

    def test_default_ronghui_account_status_maps_to_default_profile(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker(ManualCredentialsBroker):
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
        self.assertEqual(calls[-1], ("describe", False, True))
        self.assertEqual(status["profile"], "default")
        self.assertEqual(status["account_id"], "ronghui_default")
        self.assertEqual(status["system"], "ronghui")

    def test_force_status_check_auto_logs_in_expired_session(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker(ManualCredentialsBroker):
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
            self.manager.set_auto_login("ronghui_default", True)
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

    def test_same_account_auto_login_competition_fails_without_waiting(self):
        started = threading.Event()
        release = threading.Event()
        first_result: dict[str, Any] = {}

        class BlockingBroker(ManualCredentialsBroker):
            def describe_status(self, *, validate=True, force=False):
                if validate:
                    started.set()
                    if not release.wait(timeout=2):
                        raise AssertionError("test did not release account status check")
                return {
                    "status": "authenticated",
                    "label": "已登录",
                    "status_tone": "success",
                    "authenticated": True,
                    "pending_code": False,
                    "last_error_summary": "",
                }

        broker = BlockingBroker()

        def run_first() -> None:
            try:
                first_result["status"] = self.manager.check_status_with_auto_login(
                    "ronghui_default",
                    force=True,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                first_result["error"] = exc

        with patch.object(account_manager_module, "get_session_broker", return_value=broker):
            self.manager.set_auto_login("ronghui_default", True)
            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(started.wait(timeout=1))

            started_at = time.monotonic()
            with self.assertRaises(TMSAuthStateError) as raised:
                self.manager.check_status_with_auto_login("ronghui_default", force=True)
            self.assertLess(time.monotonic() - started_at, 0.2)
            self.assertEqual("BLOCKED_LOGIN", raised.exception.code)

            release.set()
            thread.join(timeout=2)

            follow_up = self.manager.check_status_with_auto_login(
                "ronghui_default",
                force=True,
            )

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", first_result)
        self.assertEqual("authenticated", first_result["status"]["status"])
        self.assertEqual("authenticated", follow_up["status"])

    def test_auto_login_lock_releases_when_status_check_raises(self):
        with patch.object(
            self.manager,
            "_check_status_with_auto_login_locked",
            side_effect=RuntimeError("fixture failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                self.manager.check_status_with_auto_login("ronghui_default", force=True)

        lock = self.manager._auto_login_lock("ronghui_default")
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_auto_login_state_commits_are_atomic_across_manager_instances(self):
        other_manager = account_manager_module.AutomationAccountManager()
        real_write_json = account_manager_module._write_json
        registry_path = account_manager_module.ACCOUNTS_PATH
        first_write_entered = threading.Event()
        release_first_write = threading.Event()
        second_lock_attempted = threading.Event()
        second_finished = threading.Event()
        errors: list[BaseException] = []

        class InstrumentedRLock:
            def __init__(self):
                self._lock = threading.RLock()

            def __enter__(self):
                if threading.current_thread().name == "second-account-update":
                    second_lock_attempted.set()
                return self._lock.__enter__()

            def __exit__(self, *args):
                return self._lock.__exit__(*args)

        def delayed_write(path, payload):
            if path == registry_path and threading.current_thread().name == "first-account-update":
                first_write_entered.set()
                if not release_first_write.wait(timeout=2):
                    raise AssertionError("test did not release first registry write")
            real_write_json(path, payload)

        def update_first() -> None:
            try:
                self.manager._set_auto_login_state(
                    "ronghui_default",
                    failure_count=1,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def update_second() -> None:
            try:
                other_manager._set_auto_login_state(
                    "yunda_default",
                    enabled=True,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                second_finished.set()

        with (
            patch.object(account_manager_module, "_ACCOUNTS_STATE_LOCK", InstrumentedRLock()),
            patch.object(account_manager_module, "_write_json", side_effect=delayed_write),
        ):
            first = threading.Thread(target=update_first, name="first-account-update")
            second = threading.Thread(target=update_second, name="second-account-update")
            first.start()
            self.assertTrue(first_write_entered.wait(timeout=1))
            second.start()
            self.assertTrue(second_lock_attempted.wait(timeout=1))
            self.assertFalse(second_finished.is_set())
            release_first_write.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        rows = json.loads(registry_path.read_text(encoding="utf-8"))
        by_id = {row["account_id"]: row for row in rows}
        self.assertEqual(by_id["ronghui_default"]["auto_login_failure_count"], 1)
        self.assertTrue(by_id["yunda_default"]["auto_login_enabled"])
        self.assertEqual(list(registry_path.parent.glob(f".{registry_path.name}.*.tmp")), [])

    def test_management_change_and_auto_state_commit_keep_both_updates(self):
        self.manager.list_accounts(include_status=False)
        other_manager = account_manager_module.AutomationAccountManager()
        real_write_json = account_manager_module._write_json
        registry_path = account_manager_module.ACCOUNTS_PATH
        management_write_entered = threading.Event()
        release_management_write = threading.Event()
        auto_lock_attempted = threading.Event()
        auto_finished = threading.Event()
        errors: list[BaseException] = []

        class TrackingRLock:
            def __init__(self):
                self._lock = threading.RLock()
                self._depth = threading.local()

            def __enter__(self):
                if (
                    threading.current_thread().name == "auto-state-update"
                    and not getattr(self._depth, "value", 0)
                ):
                    auto_lock_attempted.set()
                self._lock.acquire()
                self._depth.value = getattr(self._depth, "value", 0) + 1
                return self

            def __exit__(self, *_args):
                self._depth.value -= 1
                self._lock.release()

            def held_by_current_thread(self) -> bool:
                return bool(getattr(self._depth, "value", 0))

        registry_lock = TrackingRLock()

        def delayed_write(path, payload):
            if path == registry_path:
                if not registry_lock.held_by_current_thread():
                    raise AssertionError("registry save escaped the shared RLock")
                if threading.current_thread().name == "management-update":
                    management_write_entered.set()
                    if not release_management_write.wait(timeout=2):
                        raise AssertionError("test did not release management registry write")
            real_write_json(path, payload)

        def update_management_field() -> None:
            try:
                self.manager.update_name("ronghui_default", "updated account note")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def update_auto_state() -> None:
            try:
                other_manager._set_auto_login_state(
                    "yunda_default",
                    failure_count=1,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                auto_finished.set()

        with (
            patch.object(account_manager_module, "_ACCOUNTS_STATE_LOCK", registry_lock),
            patch.object(account_manager_module, "_write_json", side_effect=delayed_write),
        ):
            management = threading.Thread(
                target=update_management_field,
                name="management-update",
            )
            auto = threading.Thread(target=update_auto_state, name="auto-state-update")
            management.start()
            self.assertTrue(management_write_entered.wait(timeout=1))
            auto.start()
            self.assertTrue(auto_lock_attempted.wait(timeout=1))
            self.assertFalse(auto_finished.is_set())
            release_management_write.set()
            management.join(timeout=2)
            auto.join(timeout=2)

        self.assertFalse(management.is_alive())
        self.assertFalse(auto.is_alive())
        self.assertEqual(errors, [])
        rows = json.loads(registry_path.read_text(encoding="utf-8"))
        by_id = {row["account_id"]: row for row in rows}
        self.assertEqual(by_id["ronghui_default"]["name"], "updated account note")
        self.assertEqual(by_id["yunda_default"]["auto_login_failure_count"], 1)

    def test_different_accounts_validate_in_parallel_before_state_commit(self):
        validation_barrier = threading.Barrier(2)
        results: dict[str, dict[str, Any]] = {}
        errors: list[BaseException] = []

        class ConcurrentBroker(ManualCredentialsBroker):
            def describe_status(self, *, validate=True, force=False):
                if validate:
                    validation_barrier.wait(timeout=1)
                return {
                    "status": "expired",
                    "label": "已过期",
                    "status_tone": "error",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "session expired",
                }

            def send_code(self):
                return {
                    "status": "error",
                    "label": "自动登录失败",
                    "status_tone": "error",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "synthetic failure",
                }

        def check(account_id: str) -> None:
            try:
                results[account_id] = self.manager.check_status_with_auto_login(
                    account_id,
                    force=True,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch.object(
            account_manager_module,
            "get_session_broker",
            return_value=ConcurrentBroker(),
        ):
            self.manager.set_auto_login("ronghui_default", True)
            self.manager.set_auto_login("yunda_default", True)
            threads = [
                threading.Thread(target=check, args=(account_id,))
                for account_id in ("ronghui_default", "yunda_default")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {"ronghui_default", "yunda_default"})
        self.assertTrue(all(result["status"] == "error" for result in results.values()))
        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }
        self.assertEqual(accounts["ronghui_default"]["auto_login_failure_count"], 1)
        self.assertEqual(accounts["yunda_default"]["auto_login_failure_count"], 1)

    def test_auto_login_disabled_skips_validation_and_login(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker(ManualCredentialsBroker):
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "status": "logged_out",
                    "label": "未登录",
                    "status_tone": "neutral",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "",
                }

            def send_code(self):
                raise AssertionError("disabled auto-login must not attempt login")

        self.manager.set_auto_login("ronghui_default", False)
        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            status = self.manager.check_status_with_auto_login("ronghui_default", force=True)

        self.assertEqual([("describe", False, False)], calls)
        self.assertFalse(status["auto_login_enabled"])
        self.assertEqual("已退出", status["label"])
        self.assertTrue(status["monitoring_paused"])

    def test_clear_session_disables_auto_login_without_clearing_credentials(self):
        calls: list[str] = []

        class FakeBroker(ManualCredentialsBroker):
            def clear(self):
                calls.append("clear")
                return {
                    "status": "logged_out",
                    "label": "已退出",
                    "status_tone": "neutral",
                    "authenticated": False,
                    "pending_code": False,
                    "has_saved_credentials": True,
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            status = self.manager.clear_session("ronghui_default")

        account = next(
            item
            for item in self.manager.list_accounts(include_status=False)
            if item["account_id"] == "ronghui_default"
        )
        self.assertEqual(["clear"], calls)
        self.assertFalse(account["auto_login_enabled"])
        self.assertFalse(status["auto_login_enabled"])
        self.assertTrue(status["has_saved_credentials"])

    def test_auto_login_pauses_after_three_failed_cycles(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker(ManualCredentialsBroker):
            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "status": "expired",
                    "label": "已过期",
                    "status_tone": "error",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "session expired",
                }

            def send_code(self):
                calls.append(("send_code",))
                return {
                    "status": "error",
                    "label": "自动登录失败",
                    "status_tone": "error",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "账号或密码错误",
                }

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            self.manager.set_auto_login("ronghui_default", True)
            results = [
                self.manager.check_status_with_auto_login("ronghui_default", force=True)
                for _ in range(3)
            ]
            paused_status = self.manager.check_status_with_auto_login("ronghui_default", force=True)

        self.assertEqual(3, calls.count(("send_code",)))
        self.assertEqual(3, results[-1]["auto_login_failure_count"])
        self.assertTrue(results[-1]["auto_login_blocked"])
        self.assertEqual("自动登录已暂停", results[-1]["label"])
        self.assertEqual("自动登录已暂停", paused_status["label"])
        self.assertEqual(("describe", False, False), calls[-1])

    def test_login_page_unavailable_does_not_increment_failure_count(self):
        calls: list[str] = []

        class FakeBroker(ManualCredentialsBroker):
            def describe_status(self, *, validate=True, force=False):
                return {
                    "status": "expired",
                    "label": "已过期",
                    "status_tone": "error",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "session expired",
                }

            def send_code(self):
                calls.append("send_code")
                raise account_manager_module.TMSAuthStateError(
                    "LOGIN_PAGE_UNAVAILABLE",
                    "融辉登录页暂时没有加载完成，系统稍后会自动重试。",
                )

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            self.manager.set_auto_login("ronghui_default", True)
            results = [
                self.manager.check_status_with_auto_login("ronghui_default", force=True)
                for _ in range(3)
            ]

        self.assertEqual(["send_code", "send_code", "send_code"], calls)
        self.assertTrue(all(result["auto_login_retryable"] for result in results))
        self.assertTrue(all(result["auto_login_failure_count"] == 0 for result in results))
        self.assertTrue(all(not result["auto_login_blocked"] for result in results))

    def test_nested_blocked_login_does_not_increment_auto_login_failure_count(self):
        class FakeBroker(ManualCredentialsBroker):
            def describe_status(self, *, validate=True, force=False):
                return {
                    "status": "expired",
                    "label": "已过期",
                    "status_tone": "error",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "session expired",
                }

            def send_code(self):
                raise TMSAuthStateError(
                    "BLOCKED_LOGIN",
                    "该账号已有登录操作正在执行；本次请求未排队。",
                )

        with patch.object(account_manager_module, "get_session_broker", return_value=FakeBroker()):
            self.manager.set_auto_login("ronghui_default", True)
            with self.assertRaises(TMSAuthStateError) as raised:
                self.manager.check_status_with_auto_login("ronghui_default", force=True)

        account = next(
            item
            for item in self.manager.list_accounts(include_status=False)
            if item["account_id"] == "ronghui_default"
        )
        self.assertEqual("BLOCKED_LOGIN", raised.exception.code)
        self.assertEqual(0, account["auto_login_failure_count"])
        self.assertFalse(account["auto_login_blocked"])

    def test_force_status_check_keeps_pending_code_without_resending(self):
        calls: list[tuple[str, Any]] = []

        class FakeBroker(ManualCredentialsBroker):
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
            self.manager.set_auto_login("yunda_default", True)
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
        self.assertEqual("price_default", accounts["price_default"]["session_profile"])
        self.assertTrue(accounts["price_default"]["is_default"])
        self.assertNotIn("price", {item["system"] for item in accounts.values()})

    def test_legacy_price_runtime_directory_moves_to_price_default_without_duplication(self):
        legacy_dir = account_manager_module.STATE_DIR / "price"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "migration-marker.txt").write_text("synthetic", encoding="utf-8")

        account_manager_module.AutomationAccountManager()

        migrated_dir = account_manager_module.STATE_DIR / "price_default"
        self.assertFalse(legacy_dir.exists())
        self.assertEqual("synthetic", (migrated_dir / "migration-marker.txt").read_text(encoding="utf-8"))

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
        self.assertEqual("price_default", params["session_profile"])

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
        class FakeBroker(ManualCredentialsBroker):
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

    def test_price_account_login_starts_immediately_even_when_auto_login_is_off(self):
        calls = []

        class FakeBroker(ManualCredentialsBroker):
            def send_code(self):
                calls.append("send_code")
                return {
                    "status": "authenticated",
                    "authenticated": True,
                    "pending_code": False,
                    "profile": "price_default",
                }

        def fake_get_session_broker(profile):
            calls.append(profile)
            return FakeBroker()

        with patch.object(account_manager_module, "get_session_broker", side_effect=fake_get_session_broker):
            result = self.manager.login("price_default")

        self.assertFalse(result["auto_login_enabled"])
        self.assertEqual(result["session_profile"], "price_default")
        self.assertEqual(calls.count("send_code"), 1)
        self.assertTrue(all(item == "price_default" for item in calls if item != "send_code"))

    def test_account_submit_code_response_includes_context_and_hides_password(self):
        class FakeBroker(ManualCredentialsBroker):
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

    def test_accounts_default_to_auto_login_disabled(self):
        accounts = {
            item["account_id"]: item
            for item in self.manager.list_accounts(include_status=False)
        }
        created = self.manager.create_account(
            account_id="ronghui_ops_02",
            system="ronghui",
            name="融辉运营账号 02",
        )

        self.assertFalse(accounts["ronghui_default"]["auto_login_enabled"])
        self.assertFalse(accounts["price_default"]["auto_login_enabled"])
        self.assertFalse(created["auto_login_enabled"])

    def test_update_name_persists_account_note_without_changing_runtime_settings(self):
        before = next(
            item
            for item in self.manager.list_accounts(include_status=False)
            if item["account_id"] == "ronghui_default"
        )

        updated = self.manager.update_name("ronghui_default", "  融辉自提专用账号  ")
        reloaded = account_manager_module.AutomationAccountManager()
        persisted = next(
            item
            for item in reloaded.list_accounts(include_status=False)
            if item["account_id"] == "ronghui_default"
        )

        self.assertEqual("融辉自提专用账号", updated["name"])
        self.assertEqual("融辉自提专用账号", persisted["name"])
        self.assertEqual(before["system"], persisted["system"])
        self.assertEqual(before["session_profile"], persisted["session_profile"])
        self.assertEqual(before["is_active"], persisted["is_active"])
        self.assertEqual(before["auto_login_enabled"], persisted["auto_login_enabled"])

    def test_update_name_rejects_blank_or_overlong_note(self):
        for value in ("   ", "备注" * 41):
            with self.subTest(value_length=len(value)):
                with self.assertRaises(TMSAuthStateError) as raised:
                    self.manager.update_name("ronghui_default", value)
                self.assertEqual("INVALID_ACCOUNT_NAME", raised.exception.code)

    def test_environment_only_credentials_are_not_account_credentials(self):
        class EnvOnlyBroker:
            def get_manual_credentials(self):
                return {
                    "username": "",
                    "password": "",
                    "phone": "",
                    "has_saved_credentials": False,
                    "has_manual_credentials": False,
                    "has_env_credentials": False,
                    "credential_source": "",
                }

            def get_saved_credentials(self):
                raise AssertionError("account management must not read environment credentials")

        with patch.object(account_manager_module, "get_session_broker", return_value=EnvOnlyBroker()):
            credentials = self.manager.public_credentials("price_default")
            with self.assertRaises(TMSAuthStateError) as raised:
                self.manager.set_auto_login("price_default", True)

        self.assertFalse(credentials["has_saved_credentials"])
        self.assertFalse(credentials["has_env_credentials"])
        self.assertEqual("", credentials["credential_source"])
        self.assertEqual("AUTH_REQUIRED", raised.exception.code)

    def test_missing_saved_credentials_disable_legacy_auto_login_before_validation(self):
        calls: list[tuple[str, Any]] = []

        class NoCredentialsBroker:
            def get_manual_credentials(self):
                return {
                    "username": "",
                    "password": "",
                    "phone": "",
                    "has_saved_credentials": False,
                    "has_manual_credentials": False,
                    "has_env_credentials": False,
                    "credential_source": "",
                }

            def describe_status(self, *, validate=True, force=False):
                calls.append(("describe", validate, force))
                return {
                    "status": "logged_out",
                    "label": "未登录",
                    "status_tone": "neutral",
                    "authenticated": False,
                    "pending_code": False,
                    "last_error_summary": "",
                }

            def send_code(self):
                raise AssertionError("missing credentials must never open the login page")

        self.manager._set_auto_login_state("ronghui_default", enabled=True)
        with patch.object(account_manager_module, "get_session_broker", return_value=NoCredentialsBroker()):
            status = self.manager.check_status_with_auto_login("ronghui_default", force=True)

        self.assertEqual([("describe", False, False)], calls)
        self.assertFalse(status["auto_login_enabled"])
        self.assertTrue(status["monitoring_paused"])

    def test_clear_credentials_also_disables_auto_login(self):
        class StatefulBroker(ManualCredentialsBroker):
            has_credentials = True

            def get_manual_credentials(self):
                payload = super().get_manual_credentials()
                if self.has_credentials:
                    return payload
                return {
                    "username": "",
                    "password": "",
                    "phone": "",
                    "has_saved_credentials": False,
                    "has_manual_credentials": False,
                    "has_env_credentials": False,
                    "credential_source": "",
                }

            def clear_saved_credentials(self):
                self.has_credentials = False
                return self.get_manual_credentials()

        broker = StatefulBroker()
        with patch.object(account_manager_module, "get_session_broker", return_value=broker):
            self.manager.set_auto_login("ronghui_default", True)
            credentials = self.manager.clear_credentials("ronghui_default")

        account = next(
            item
            for item in self.manager.list_accounts(include_status=False)
            if item["account_id"] == "ronghui_default"
        )
        self.assertFalse(credentials["has_saved_credentials"])
        self.assertFalse(account["auto_login_enabled"])
