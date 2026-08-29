"""Boundaries for staged, per-profile session login operations."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from agent.tms_runtime import session_persistence as persistence_module
from agent.tms_runtime import session_login_worker
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_broker import SessionBroker
from agent.tms_runtime.session_state import SessionStateStore


def _write_staged_success(stage_dir: Path) -> None:
    SessionStateStore.write_dict(
        stage_dir / "storage_state.json",
        {"cookies": [], "origins": []},
    )
    SessionStateStore.write_dict(stage_dir / "cookies.json", {"cookies": []})
    SessionStateStore.write_dict(
        stage_dir / "session_meta.json",
        {
            "status": "authenticated",
            "authenticated_at": "2026-08-29 10:00:00",
            "last_validation_at": "",
            "last_error_summary": "",
            "pending_since": "",
            "expires_at": "",
        },
    )
    SessionStateStore.write_dict(
        stage_dir / "operation_result.json",
        {
            "ok": True,
            "result": {"status": "authenticated"},
            "commit_staged_state": True,
        },
    )


class _SuccessfulProcess:
    _next_pid = 20_000

    def __init__(self, command: list[str], *, before_return=None) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.command = list(command)
        self.stage_dir = Path(command[-1]) if command else Path()
        self.before_return = before_return
        self.returncode: int | None = None
        self.input_payload = ""

    def communicate(self, *, input: str, timeout: float):
        self.input_payload = input
        _write_staged_success(self.stage_dir)
        if self.before_return is not None:
            self.before_return()
        self.returncode = 0
        return ("", "")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class SessionProcessIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _broker(self, profile: str, *, timeout: float = 120.0) -> SessionBroker:
        return SessionBroker(
            profile_name=profile,
            username_envs=(),
            password_envs=(),
            phone_envs=(),
            state_dir_override=self.root / profile,
            browser_action_timeout_sec=timeout,
        )

    def test_browser_timeout_uses_env_only_when_constructor_does_not_override(self):
        with patch.dict(os.environ, {"TMS_BROWSER_ACTION_TIMEOUT_SECONDS": "17.5"}):
            configured = SessionBroker(
                profile_name="env_timeout",
                username_envs=(),
                password_envs=(),
                phone_envs=(),
                state_dir_override=self.root / "env-timeout",
            )
            explicit = SessionBroker(
                profile_name="explicit_timeout",
                username_envs=(),
                password_envs=(),
                phone_envs=(),
                state_dir_override=self.root / "explicit-timeout",
                browser_action_timeout_sec=8,
            )

        self.assertEqual(17.5, configured._browser_action_timeout_sec)
        self.assertEqual(8.0, explicit._browser_action_timeout_sec)

    def test_browser_timeout_rejects_invalid_or_non_positive_env(self):
        for value in ("not-a-number", "", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"TMS_BROWSER_ACTION_TIMEOUT_SECONDS": value}):
                    with self.assertRaises(ValueError):
                        SessionBroker(
                            profile_name="bad_env_timeout",
                            username_envs=(),
                            password_envs=(),
                            phone_envs=(),
                            state_dir_override=self.root / f"bad-env-{value or 'blank'}",
                        )

    def test_login_worker_returns_typed_staging_envelope(self):
        stage_dir = self.root / "worker-envelope"
        stage_dir.mkdir()

        class Broker:
            @staticmethod
            def send_code():
                raise TMSAuthStateError("AUTH_PENDING_CODE", "fixture challenge")

        with patch.object(session_login_worker, "build_session_broker", return_value=Broker()):
            exit_code = session_login_worker.run_worker(
                profile="worker_profile",
                state_dir=stage_dir,
                request={"action": "send", "code": ""},
            )

        envelope = SessionStateStore.read_dict(stage_dir / "operation_result.json")
        self.assertEqual(0, exit_code)
        self.assertEqual("AUTH_PENDING_CODE", envelope["error_code"])
        self.assertTrue(envelope["commit_staged_state"])

    def test_staged_login_commits_meta_last_and_never_puts_code_in_argv(self):
        broker = self._broker("account_one")
        processes: list[_SuccessfulProcess] = []
        replacements: list[tuple[Path, Path]] = []
        real_replace = os.replace

        def popen(command, **kwargs):
            self.assertTrue(kwargs.get("start_new_session"))
            process = _SuccessfulProcess(command)
            processes.append(process)
            return process

        def replace(source, target):
            replacements.append((Path(source), Path(target)))
            return real_replace(source, target)

        with (
            patch.object(persistence_module.subprocess, "Popen", side_effect=popen),
            patch.object(persistence_module.os, "replace", side_effect=replace),
        ):
            result = broker.submit_code("654321")

        self.assertEqual("authenticated", result["status"])
        self.assertEqual(1, len(processes))
        self.assertNotIn("654321", " ".join(processes[0].command))
        self.assertEqual("654321", json.loads(processes[0].input_payload)["code"])
        committed_names = [
            target.name
            for source, target in replacements
            if target.parent == broker._state_dir and source.parent != broker._state_dir
        ]
        self.assertEqual("session_meta.json", committed_names[-1])
        self.assertFalse((broker._state_dir / ".login_ops").exists())

    def test_timeout_terminates_worker_and_releases_profile_lock(self):
        broker = self._broker("timeout_account", timeout=0.01)

        class TimeoutProcess:
            pid = 30_001
            returncode = None

            def communicate(self, *, input, timeout):
                raise subprocess.TimeoutExpired("session_login_worker", timeout)

            def poll(self):
                return self.returncode

        process = TimeoutProcess()
        terminated: list[tuple[object, int | None]] = []

        def terminate(target, *, process_group_id=None):
            terminated.append((target, process_group_id))
            target.returncode = -signal.SIGTERM

        with (
            patch.object(persistence_module.subprocess, "Popen", return_value=process),
            patch.object(persistence_module.os, "getpgid", return_value=30_777),
            patch.object(broker, "_terminate_process_tree", side_effect=terminate),
        ):
            with self.assertRaises(TMSAuthStateError) as ctx:
                broker.send_code()

        self.assertEqual("LOGIN_TIMEOUT", ctx.exception.code)
        self.assertEqual([(process, 30_777)], terminated)
        self.assertTrue(broker._login_operation_lock.acquire(blocking=False))
        broker._login_operation_lock.release()
        self.assertFalse((broker._state_dir / ".login_ops").exists())

    def test_same_profile_fails_immediately_while_different_profile_runs(self):
        first = self._broker("account_a")
        second = self._broker("account_b")
        started = threading.Event()
        release = threading.Event()
        first_result: dict[str, object] = {}

        class BlockingProcess(_SuccessfulProcess):
            def communicate(self, *, input: str, timeout: float):
                self.input_payload = input
                started.set()
                if not release.wait(timeout=2):
                    raise AssertionError("test did not release staged worker")
                _write_staged_success(self.stage_dir)
                self.returncode = 0
                return ("", "")

        def popen(command, **_kwargs):
            profile = command[command.index("--profile") + 1]
            if profile == "account_a":
                return BlockingProcess(command)
            return _SuccessfulProcess(command)

        def run_first() -> None:
            try:
                first_result["value"] = first.send_code()
            except BaseException as exc:  # pragma: no cover - asserted below
                first_result["error"] = exc

        with patch.object(persistence_module.subprocess, "Popen", side_effect=popen):
            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(started.wait(timeout=1))

            started_at = time.monotonic()
            health = first.health_snapshot()
            self.assertLess(time.monotonic() - started_at, 0.2)
            self.assertEqual("logged_out", health["status"])

            with self.assertRaises(TMSAuthStateError) as ctx:
                first.send_code()
            self.assertEqual("BLOCKED_LOGIN", ctx.exception.code)

            other_result = second.send_code()
            self.assertEqual("authenticated", other_result["status"])

            async def ticker() -> bool:
                await asyncio.sleep(0)
                return True

            self.assertTrue(asyncio.run(ticker()))
            release.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", first_result)
        self.assertEqual("authenticated", first_result["value"]["status"])

    def test_session_change_discards_stale_worker_result(self):
        broker = self._broker("stale_account")
        process = _SuccessfulProcess([], before_return=broker.clear)

        def popen(command, **_kwargs):
            process.command = list(command)
            process.stage_dir = Path(command[-1])
            return process

        with patch.object(persistence_module.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(TMSAuthStateError) as ctx:
                broker.send_code()

        self.assertEqual("BLOCKED_LOGIN", ctx.exception.code)
        self.assertFalse(broker._storage_state_path.exists())
        self.assertEqual("logged_out", broker.health_snapshot()["status"])

    def test_credential_change_bumps_epoch_and_discards_worker_result(self):
        broker = self._broker("credential_change")
        before_epoch = broker._state_epoch

        def update_credentials() -> None:
            broker.save_credentials(username="fixture-user", password="fixture-pass", phone="")

        process = _SuccessfulProcess([], before_return=update_credentials)

        def popen(command, **_kwargs):
            process.command = list(command)
            process.stage_dir = Path(command[-1])
            return process

        with patch.object(persistence_module.subprocess, "Popen", side_effect=popen):
            with self.assertRaises(TMSAuthStateError) as ctx:
                broker.send_code()

        self.assertEqual("BLOCKED_LOGIN", ctx.exception.code)
        self.assertGreater(broker._state_epoch, before_epoch + 1)
        self.assertFalse(broker._storage_state_path.exists())

    @unittest.skipUnless(os.name == "posix", "ECS process-group cleanup is POSIX-specific")
    def test_process_tree_cleanup_escalates_from_term_to_kill(self):
        class Process:
            pid = 40_001

            def __init__(self):
                self.wait_calls = 0

            def poll(self):
                return None

            def wait(self, timeout):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("session_login_worker", timeout)
                return -signal.SIGKILL

        process = Process()
        with patch.object(persistence_module.os, "killpg") as killpg:
            SessionBroker._terminate_process_tree(
                process,
                process_group_id=40_777,
            )

        self.assertEqual(
            [
                ((40_777, signal.SIGTERM), {}),
                ((40_777, signal.SIGKILL), {}),
            ],
            killpg.call_args_list,
        )

    @unittest.skipUnless(os.name == "posix", "ECS process-group cleanup is POSIX-specific")
    def test_process_tree_cleanup_kills_original_group_after_leader_exits(self):
        class Process:
            pid = 41_001

            @staticmethod
            def poll():
                return 0

            @staticmethod
            def wait(timeout):
                return 0

        process = Process()
        with patch.object(persistence_module.os, "killpg") as killpg:
            SessionBroker._terminate_process_tree(
                process,
                process_group_id=41_777,
            )

        self.assertEqual(
            [
                ((41_777, signal.SIGTERM), {}),
                ((41_777, signal.SIGKILL), {}),
            ],
            killpg.call_args_list,
        )

    def test_response_hooks_only_reject_explicit_login_signals(self):
        yunda = SessionBroker(
            profile_name="yunda_test",
            username_envs=(),
            password_envs=(),
            phone_envs=(),
            login_mode="yunda_password",
            state_dir_override=self.root / "yunda-hook",
        )
        session = yunda._install_session_auth_hook(requests.Session())
        hook = session.hooks["response"][0]
        normal_html = SimpleNamespace(
            url="https://kyinms.yunda56.com/order/create",
            headers={"content-type": "text/html"},
            text="<html><main>normal business page</main></html>",
        )
        self.assertIs(normal_html, hook(normal_html))

        normal_password_form = SimpleNamespace(
            url="https://kyinms.yunda56.com/order/create",
            headers={"content-type": "text/html"},
            text='<html><form id="change_password"><input type="password"></form></html>',
        )
        self.assertIs(normal_password_form, hook(normal_password_form))

        login_html = SimpleNamespace(
            url="https://kyinms.yunda56.com/order/create",
            headers={"content-type": "text/html"},
            text='<html><form id="login_form"><input type="password"></form></html>',
        )
        with self.assertRaises(TMSAuthStateError) as yunda_ctx:
            hook(login_html)
        self.assertEqual("AUTH_REQUIRED", yunda_ctx.exception.code)

        relative_login_redirect = SimpleNamespace(
            url="https://kyinms.yunda56.com/order/create",
            headers={"Location": "/login", "content-type": "text/html"},
            text="",
        )
        with self.assertRaises(TMSAuthStateError) as relative_ctx:
            hook(relative_login_redirect)
        self.assertEqual("AUTH_REQUIRED", relative_ctx.exception.code)

        ronghui = self._broker("ronghui_hook")
        ronghui_session = ronghui._install_session_auth_hook(requests.Session())
        ronghui_hook = ronghui_session.hooks["response"][0]
        redirect = SimpleNamespace(
            url="https://tms.ronghuiwl.com/data/query",
            headers={"Location": "/system/login"},
            text="",
        )
        with self.assertRaises(TMSAuthStateError) as ronghui_ctx:
            ronghui_hook(redirect)
        self.assertEqual("AUTH_REQUIRED", ronghui_ctx.exception.code)

    def test_unknown_capability_fails_without_network(self):
        broker = self._broker("unknown_capability")
        with (
            patch.object(
                broker,
                "_session_from_saved_state_locked",
                side_effect=AssertionError("must fail before session construction"),
            ),
            self.assertRaises(TMSAuthStateError) as ctx,
        ):
            broker.open_capability_session("ronghui_everything")

        self.assertEqual("SESSION_CAPABILITY_UNKNOWN", ctx.exception.code)

    def test_ronghui_health_matrix_probes_each_read_capability_and_never_writes(self):
        broker = self._broker("ronghui_matrix")
        broker._storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}),
            encoding="utf-8",
        )

        menu_urls = {
            "快件跟踪": "/widget/home?mv=tracking",
            "登记问题件查询": "/widget/home?mv=problem",
            "网点到离港记录": "/widget/home?mv=clock",
            "结算明细查询": "/widget/home?mv=finance",
        }
        page_markers = {
            "tracking": "FIND_SACN_TRACK_BY_CODE",
            "problem": "FIND_PROBLEM_REGISTER_LIST",
            "clock": "FIND_REACH_OR_LEAVE_PORT_DETNEW",
            "finance": (
                "FIND_BALANCE_QRY_WST_WITH_SITE "
                "FIND_BALANCE_QRY_TJ_WST FIND_BALANCE_QRY_TJ_DETAIL"
            ),
        }

        class Session:
            def __init__(self):
                self.calls: list[tuple[str, dict[str, object]]] = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url.endswith("/menuTreeExtend/loadMenu"):
                    return SimpleNamespace(
                        status_code=200,
                        url=url,
                        headers={"content-type": "application/json"},
                        text="",
                        json=lambda: {
                            "success": True,
                            "result": {
                                "data": [
                                    {"text": label, "url": target}
                                    for label, target in menu_urls.items()
                                ]
                            },
                        },
                    )
                marker = next(
                    (value for key, value in page_markers.items() if f"mv={key}" in url),
                    "authenticated home",
                )
                return SimpleNamespace(
                    status_code=200,
                    url=url,
                    headers={"content-type": "text/html"},
                    text=f"<html><main>{marker}</main></html>",
                )

        session = Session()
        with patch.object(
            broker,
            "_session_from_saved_state_locked",
            return_value=session,
        ):
            result = broker.validate_health_matrix()

        self.assertEqual("degraded", result["status"])
        self.assertEqual(6, len(session.calls))
        self.assertTrue(all(kwargs.get("timeout") == 15 for _url, kwargs in session.calls))
        self.assertFalse(hasattr(session, "post"))
        self.assertEqual(
            {
                "ronghui_home",
                "ronghui_scan",
                "ronghui_problem",
                "ronghui_clock",
                "ronghui_finance",
                "ronghui_write",
            },
            set(result["capabilities"]),
        )
        for capability in (
            "ronghui_home",
            "ronghui_scan",
            "ronghui_problem",
            "ronghui_clock",
        ):
            self.assertEqual("ok", result["capabilities"][capability]["status"])
        self.assertEqual("UNKNOWN", result["capabilities"]["ronghui_write"]["status"])

    def test_ronghui_health_matrix_does_not_copy_one_capability_failure(self):
        broker = self._broker("ronghui_matrix_isolated_failure")
        broker._storage_state_path.write_text(
            json.dumps({"cookies": [], "origins": []}),
            encoding="utf-8",
        )

        class Session:
            @staticmethod
            def get(url, **_kwargs):
                if url.endswith("/menuTreeExtend/loadMenu"):
                    return SimpleNamespace(
                        status_code=200,
                        url=url,
                        headers={"content-type": "application/json"},
                        text="",
                        json=lambda: {
                            "success": True,
                            "result": {
                                "data": [
                                    {"text": "快件跟踪", "url": "/widget/home?mv=tracking"},
                                    {"text": "登记问题件查询", "url": "/widget/home?mv=problem"},
                                    {"text": "网点到离港记录", "url": "/widget/home?mv=clock"},
                                ]
                            },
                        },
                    )
                status_code = 503 if "mv=tracking" in url else 200
                marker = (
                    "FIND_PROBLEM_REGISTER_LIST"
                    if "mv=problem" in url
                    else "FIND_REACH_OR_LEAVE_PORT_DETNEW"
                )
                return SimpleNamespace(
                    status_code=status_code,
                    url=url,
                    headers={"content-type": "text/html"},
                    text=f"<html>{marker}</html>",
                )

        with patch.object(
            broker,
            "_session_from_saved_state_locked",
            return_value=Session(),
        ):
            result = broker.validate_health_matrix()

        self.assertEqual(
            "CAPABILITY_UNAVAILABLE",
            result["capabilities"]["ronghui_scan"]["status"],
        )
        self.assertEqual("ok", result["capabilities"]["ronghui_home"]["status"])
        self.assertEqual("ok", result["capabilities"]["ronghui_problem"]["status"])
        self.assertEqual("ok", result["capabilities"]["ronghui_clock"]["status"])
        self.assertEqual("UNKNOWN", result["capabilities"]["ronghui_write"]["status"])

    def test_ronghui_write_capability_has_no_synthetic_online_probe(self):
        broker = self._broker("ronghui_write_unknown")
        with (
            patch.object(
                broker,
                "resolve_login_config",
                side_effect=AssertionError("write capability must not resolve a target"),
            ),
            self.assertRaises(TMSAuthStateError) as ctx,
        ):
            broker._validate_capability_once(requests.Session(), "ronghui_write")

        self.assertEqual("CAPABILITY_UNKNOWN", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
