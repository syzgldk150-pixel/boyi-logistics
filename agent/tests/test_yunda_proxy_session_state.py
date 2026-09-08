"""Real Requests/Broker continuity against an isolated HTTP protocol fixture."""

import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime import dispatch
from agent.tms_runtime.scripts import yunda_waybill_proxy as proxy
from agent.tms_runtime.session_broker import build_session_broker


ENTRY = "/ky_inms/public/index.php/business/waybill/entry/indexNew.html"
COST = "/ky_inms/public/index.php/getCostInfoPrompt.html"
TEMPLATE = "/ky_inms/public/index.php/business/waybill/entry/getTemplateList.html"
SLOW = "/ky_inms/public/index.php/fixture/slow.html"
SEED_ERROR = "/ky_inms/public/index.php/fixture/seed-error.html"
LOGIN = "/ky_inms/public/index.php/fixture/login.html"
WRITE = "/ky_inms/public/index.php/business/waybill/entry/save.html"


class _Upstream:
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                path = urlsplit(self.path).path
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.calls.append((self.command, path))
                if path == WRITE:
                    # The fixture accepted the write; its response is lost.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if path == SLOW:
                    owner.started.set()
                    if not owner.release.wait(5):
                        raise AssertionError("test did not release the held response")
                ready = "fixture_state=ready" in self.headers.get("Cookie", "")
                status, body = 200, b'{"status":0,"data":[]}'
                if path in {COST, TEMPLATE} and not ready:
                    status, body = (404, b"System Error") if path == COST else (200, b"")
                if path == SEED_ERROR:
                    status = 404
                if path == LOGIN:
                    status = 302
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if path in {ENTRY, SLOW, SEED_ERROR}:
                    self.send_header("Set-Cookie", "fixture_state=ready; Path=/ky_inms/public/; HttpOnly; SameSite=Lax")
                if path == LOGIN:
                    self.send_header("Location", "/login")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class _ObservedLock:
    """Signal contention while retaining the actual threading lock behavior."""

    def __init__(self, lock):
        self.lock = lock
        self.queued = threading.Event()

    def acquire(self, **kwargs):
        if self.lock.locked():
            self.queued.set()
        return self.lock.acquire(**kwargs)

    def release(self):
        self.lock.release()


class YundaProxySessionStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.upstream = _Upstream()
        self.addCleanup(self.upstream.close)
        self.brokers = {}
        original_target = proxy._target_from_params

        def loopback_target(path, query):
            validated_path, validated_query, _url = original_target(path, query)
            return validated_path, validated_query, self.upstream.origin + validated_path

        self.target_patch = patch.object(proxy, "_target_from_params", side_effect=loopback_target)
        self.target_patch.start()
        self.addCleanup(self.target_patch.stop)
        self.broker_patch = patch.object(proxy, "get_session_broker", side_effect=self.brokers.__getitem__)
        self.broker_patch.start()
        self.addCleanup(self.broker_patch.stop)

    def broker(self, profile="yunda_fixture_a"):
        state_dir = Path(self.directory.name) / profile
        broker = build_session_broker(profile, state_dir_override=state_dir)
        initial = {
            "cookies": [],
            "origins": [{"origin": self.upstream.origin, "localStorage": [{"name": "fixture", "value": "kept"}]}],
        }
        broker._state_store.write_dict(broker._storage_state_path, initial)
        broker._state_store.write_dict(broker._meta_path, {"status": "expired", "authenticated_at": "fixture-before"})
        self.brokers[profile] = broker
        return broker, initial

    def invoke(self, path, method="GET", profile="yunda_fixture_a"):
        return proxy.run_once({"session_profile": profile, "path": path, "method": method, "timeout_sec": 3})

    def test_entry_state_survives_separate_proxy_requests_and_broker_recreation(self):
        broker, initial = self.broker()
        meta_before = broker._meta_path.read_bytes()
        self.assertEqual(404, self.invoke(COST, "POST")["status_code"])
        with self.assertRaises(TMSAuthStateError):
            self.invoke(TEMPLATE, "POST")
        entry = self.invoke(ENTRY)
        self.assertNotIn("set-cookie", {name.lower() for name in entry["headers"]})
        # Recreate the facade: continuity must come from durable state, not a cache.
        self.brokers[broker.profile_name] = build_session_broker(broker.profile_name, state_dir_override=broker._state_dir)
        for path in (COST, TEMPLATE):
            response = self.invoke(path, "POST")
            self.assertEqual(200, response["status_code"])
            self.assertEqual(0, json.loads(base64.b64decode(response["body_base64"]))["status"])
        saved = json.loads(broker._storage_state_path.read_text())
        self.assertEqual(initial["origins"], saved["origins"])
        self.assertEqual(meta_before, broker._meta_path.read_bytes())
        self.assertFalse(broker._cookies_path.exists())
        self.assertEqual([("POST", COST), ("POST", TEMPLATE), ("GET", ENTRY), ("POST", COST), ("POST", TEMPLATE)], self.upstream.calls)

    def test_http_error_state_is_preserved_without_crossing_profiles(self):
        self.broker()
        self.broker("yunda_fixture_b")
        self.assertEqual(404, self.invoke(SEED_ERROR)["status_code"])
        self.assertEqual(200, self.invoke(COST, "POST")["status_code"])
        self.assertEqual(404, self.invoke(COST, "POST", "yunda_fixture_b")["status_code"])

    def test_unchanged_responses_do_not_rewrite_saved_state(self):
        broker, _initial = self.broker()
        with patch.object(broker._state_store, "write_dict", wraps=broker._state_store.write_dict) as save:
            self.invoke(ENTRY)
            self.assertEqual(1, save.call_count)
            self.invoke(COST, "POST")
            self.invoke(TEMPLATE, "POST")
            self.invoke(ENTRY)
            self.assertEqual(1, save.call_count)

    def test_same_profile_waits_for_prior_response_without_holding_state_lock(self):
        broker, _initial = self.broker()
        self.broker("yunda_fixture_b")
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(self.invoke, SLOW)
            self.assertTrue(self.upstream.started.wait(3))
            following_started = threading.Event()

            def following():
                following_started.set()
                return self.invoke(COST, "POST")

            later = pool.submit(following)
            try:
                self.assertTrue(following_started.wait(3))
                # A different profile and the existing state lock stay available.
                self.assertTrue(broker._lock.acquire(timeout=1))
                broker._lock.release()
                self.assertEqual(200, self.invoke(ENTRY, profile="yunda_fixture_b")["status_code"])
            finally:
                self.upstream.release.set()
            self.assertEqual(200, slow.result(timeout=3)["status_code"])
            self.assertEqual(200, later.result(timeout=3)["status_code"])

    def test_logout_during_request_rejects_old_result_without_restoring_state(self):
        broker, _initial = self.broker()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.invoke, SLOW)
            self.assertTrue(self.upstream.started.wait(3))
            try:
                broker.clear()
                logged_out = broker._meta_path.read_bytes()
            finally:
                self.upstream.release.set()
            with self.assertRaises(TMSAuthStateError) as raised:
                result.result(timeout=3)
        self.assertEqual("BLOCKED_LOGIN", raised.exception.code)
        self.assertFalse(broker._storage_state_path.exists())
        self.assertFalse(broker._cookies_path.exists())
        self.assertEqual(logged_out, broker._meta_path.read_bytes())

    def test_active_login_and_explicit_login_response_are_not_authenticated(self):
        broker, _initial = self.broker()
        before = broker._meta_path.read_bytes()
        broker._active_login_token = "fixture-login-in-progress"
        with self.assertRaises(TMSAuthStateError) as raised:
            self.invoke(ENTRY)
        self.assertEqual("BLOCKED_LOGIN", raised.exception.code)
        self.assertEqual([], self.upstream.calls)
        broker._active_login_token = None
        with self.assertRaises(TMSAuthStateError) as raised:
            self.invoke(LOGIN)
        self.assertEqual("AUTH_REQUIRED", raised.exception.code)
        self.assertEqual(before, broker._meta_path.read_bytes())
        self.assertEqual([("GET", LOGIN)], self.upstream.calls)

    def test_response_lost_after_write_is_not_retried_or_initialized_implicitly(self):
        self.broker()
        with self.assertRaises(requests.ConnectionError):
            self.invoke(WRITE, "POST")
        self.assertEqual([("POST", WRITE)], self.upstream.calls)

    def test_expired_or_cancelled_dispatch_never_sends_queued_write(self):
        broker, _initial = self.broker()
        lock = _ObservedLock(broker._original_page_request_lock)
        broker._original_page_request_lock = lock
        for cancel in (False, True):
            with self.subTest(cancel=cancel), ThreadPoolExecutor(max_workers=1) as pool:
                self.upstream.started.clear()
                self.upstream.release.clear()
                lock.queued.clear()
                before_calls = len(self.upstream.calls)
                first = pool.submit(self.invoke, SLOW)
                self.assertTrue(self.upstream.started.wait(3))

                async def scenario():
                    with patch.dict(dispatch._SEMAPHORES, {"yunda_waybill_proxy": asyncio.Semaphore(3)}), patch.object(
                        dispatch, "resolve_account_params", side_effect=lambda params, **_kwargs: {
                            **params, "session_profile": broker.profile_name,
                        },
                    ):
                        task = asyncio.create_task(dispatch.execute_target(
                            "yunda_waybill_proxy", dispatch.TaskRequest(timeout_sec=1, params={
                                "path": WRITE, "method": "POST", "timeout_sec": 30,
                                # A caller cannot extend the server's deadline.
                                "_original_page_deadline_monotonic": 1e30,
                                "_original_page_cancelled": None,
                            }),
                        ))
                        try:
                            self.assertTrue(await asyncio.to_thread(lock.queued.wait, 3))
                            if cancel:
                                task.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await task
                            else:
                                status, result = await task
                                self.assertEqual(504, status)
                                self.assertFalse(result["ok"])
                        finally:
                            self.upstream.release.set()

                try:
                    # asyncio.run also joins its real executor: the assertion
                    # below observes the formerly queued thread's final result.
                    asyncio.run(scenario())
                finally:
                    self.upstream.release.set()
                self.assertEqual(200, first.result(timeout=3)["status_code"])
                self.assertEqual([("GET", SLOW)], self.upstream.calls[before_calls:])

    def test_queued_request_cannot_adopt_a_new_login_epoch(self):
        broker, initial = self.broker()
        lock = _ObservedLock(broker._original_page_request_lock)
        broker._original_page_request_lock = lock
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.invoke, SLOW)
            self.assertTrue(self.upstream.started.wait(3))
            queued = pool.submit(self.invoke, WRITE, "POST")
            try:
                self.assertTrue(lock.queued.wait(3))
                broker.clear()
                # Use the real login-state commit with synthetic staged output;
                # no provider login, credentials, or business calls are needed.
                stage = Path(self.directory.name) / "synthetic-login-result"
                broker._state_store.write_dict(stage / "storage_state.json", initial)
                broker._state_store.write_dict(stage / "session_meta.json", {
                    "status": "authenticated", "authenticated_at": "fixture-new-login",
                })
                with broker._lock:
                    broker._bump_state_epoch_locked()
                    broker._commit_staged_login_state_locked(stage)
                new_storage = broker._storage_state_path.read_bytes()
                new_meta = broker._meta_path.read_bytes()
            finally:
                self.upstream.release.set()
            for result in (first, queued):
                with self.assertRaises(TMSAuthStateError) as raised:
                    result.result(timeout=3)
                self.assertEqual("BLOCKED_LOGIN", raised.exception.code)
        self.assertEqual([("GET", SLOW)], self.upstream.calls)
        self.assertEqual(new_storage, broker._storage_state_path.read_bytes())
        self.assertEqual(new_meta, broker._meta_path.read_bytes())
