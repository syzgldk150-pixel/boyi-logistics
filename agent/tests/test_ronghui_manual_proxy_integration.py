"""Real isolated Console/Agent/dispatcher/proxy chain with synthetic TMS I/O."""

import base64
import json
import os
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
from requests.exceptions import ConnectionError

from agent.tms_runtime.account_manager import AutomationAccountManager
from agent.tms_runtime.routes import router
from agent.tms_runtime.scripts import ronghui_waybill_proxy as proxy
from console.services.agent_api import AgentApiServiceMixin
from console.tests import test_yunda_entry as console_support
from shared.service_identity import ConsoleIdentityVerifier


class RonghuiManualProxyIntegrationTests(unittest.TestCase):
    def test_original_single_allocation_and_existence_check_cross_the_real_chain(self):
        """Neither the proxy, route, dispatch nor run_once is replaced."""
        secret = "fixture-only-console-signing-secret"
        verifier = ConsoleIdentityVerifier(secret)
        app = FastAPI()
        principals = []

        @app.middleware("http")
        async def signed_console_identity(request, call_next):
            self.assertEqual("fixture-internal-token", request.headers.get("X-Agent-Internal-Token"))
            request.state.console_principal = verifier.verify(
                headers=request.headers, method=request.method,
                request_target=request.url.path, body=await request.body(),
            )
            principals.append(request.state.console_principal)
            return await call_next(request)

        app.include_router(router, prefix="/internal/v1")
        client = TestClient(app)
        calls = []
        responses = [b'[{"BILL_CODE":";fixture-bill-one;"}]', b'[]',
                     b'[{"BILL_CODE":";fixture-bill-two;"}]']

        class Session:
            cookies = []

            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                if len(calls) > len(responses):
                    raise ConnectionError("Synthetic upstream response lost")
                body = responses[len(calls) - 1]
                return types.SimpleNamespace(status_code=200, content=body, text=body.decode(),
                                             headers={"Content-Type": "application/json"}, url=url)

        broker = types.SimpleNamespace(build_requests_session=lambda validate: Session())
        # Real account selection uses only this synthetic metadata store. No
        # manager constructor, credential store, persisted session or .env I/O.
        manager = AutomationAccountManager.__new__(AutomationAccountManager)
        manager._load_accounts = lambda: [{
            "account_id": "fixture-price", "name": "Fixture", "system": "ronghui",
            "account_purpose": "price", "is_active": True, "is_default": True,
            "session_profile": "ronghui_fixture_selected",
        }]

        class LocalResponse:
            def __init__(self, response):
                self.status = response.status_code
                self.body = response.content

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.body

        def local_agent_transport(request, **_kwargs):
            return LocalResponse(client.request(request.method, request.full_url,
                                                content=request.data, headers=dict(request.header_items())))

        def real_console_agent_request(console, *args, **kwargs):
            console.settings.agent_internal_api_token = "fixture-internal-token"
            return AgentApiServiceMixin._agent_request(console, *args, **kwargs)

        requests = (
            ("FIND_TMS_BILL_CODE_BY", b"vCount=1"),
            ("GET_BILL_BY_BILLCODE", b"BILL_CODE=fixture-bill-one"),
            ("FIND_TMS_BILL_CODE_BY", b"vCount=1"),
        )
        support = console_support.YundaEntryBackendTests()
        with patch.dict(os.environ, {"CONSOLE_AGENT_SIGNING_SECRET": secret}), patch(
            "agent.tms_runtime.account_manager._ACCOUNT_MANAGER", manager,
        ), patch.object(proxy, "get_session_broker", return_value=broker) as broker_lookup, patch(
            "console.services.agent_api.urlopen", side_effect=local_agent_transport,
        ):
            for index, (selector, body) in enumerate(requests):
                with self.subTest(selector=selector, index=index):
                    console, handler = support._post_original_page(
                        "ronghui", "/dataQuery/findAllByCallId", query=f"id={selector}", body=body,
                        agent_transport=real_console_agent_request,
                    )
                    self.assertEqual(200, handler.status, getattr(console, "sent_payload", None))
                    self.assertEqual(json.loads(responses[index]), json.loads(handler.wfile.getvalue()))
                    self.assertEqual(index + 1, len(calls))
                    method, remote_url, forwarded = calls[index]
                    self.assertEqual("POST", method)
                    self.assertEqual("/dataQuery/findAllByCallId", urlparse(remote_url).path)
                    self.assertEqual({"id": [selector]}, parse_qs(urlparse(remote_url).query))
                    self.assertEqual(body, forwarded["data"])
                    self.assertEqual("application/x-www-form-urlencoded; charset=UTF-8",
                                     forwarded["headers"]["Content-Type"])
                    self.assertFalse(forwarded["allow_redirects"])
                    self.assertEqual([], console.repository.upserts)
                    self.assertEqual([], console.repository.snapshots)
                    params = console.agent_calls[0]["payload"]["params"]
                    self.assertEqual(body, base64.b64decode(params["body_base64"]))
                    self.assertEqual("/original/ronghui", params["proxy_prefix"])
            # A lost allocation response is an error, never an automatic retry
            # or a fabricated number. The transport records the one attempt.
            console, handler = support._post_original_page(
                "ronghui", "/dataQuery/findAllByCallId", query="id=FIND_TMS_BILL_CODE_BY", body=b"vCount=1",
                agent_transport=real_console_agent_request,
            )
            self.assertEqual(502, console.sent_status)
            self.assertFalse(console.sent_payload["ok"])
            self.assertEqual(len(requests) + 1, len(calls))
            self.assertEqual([], console.repository.upserts)
            self.assertEqual([], console.repository.snapshots)
            self.assertEqual(len(requests) + 1, broker_lookup.call_count)
            broker_lookup.assert_called_with("ronghui_fixture_selected")
        self.assertEqual(len(requests) + 1, len(principals))
        self.assertTrue(all(item["actor_id"] == "7" and item["roles"] == ["super_admin"]
                            for item in principals))
        self.assertNotIn("FIND_TMS_BILL_CODE_BY", proxy.CACHEABLE_DATAQUERY_CALL_IDS)
        self.assertEqual("", proxy._cacheable_lookup_key(
            "GET", "/dataQuery/findAllByCallId", "id=FIND_TMS_BILL_CODE_BY&vCount=1",
            session_profile="ronghui_fixture_selected",
        ))
