"""Exercise the Host-owned daily-sign port through actual local HTTP auth."""
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from agent.execution_boundary import current_execution_capability, execution_capability_scope, authorize_tms_target, EXECUTION_CAPABILITY_HEADER
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from plugin_core_adapters.daily_sign_ports import build_daily_sign_port_handlers
from tools import tms_tool


def test_v2_daily_sign_uses_host_read_scope_without_exposing_or_retaining_it(monkeypatch):
    expected = [{"billNumberMain": "R00021000001", "planSignTime": "2026-09-12 23:59:59"}]
    seen = []
    tokens = []
    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            token = self.headers.get(EXECUTION_CAPABILITY_HEADER, '')
            allowed = authorize_tms_target(token, self.path, request_params=request['params'])
            if allowed:
                seen.append(request['params'])
                tokens.append(token)
            self.send_response(200 if allowed else 410)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': allowed, 'data': expected if allowed else None}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(tms_tool, 'HTTP_SERVICE_URL', f'http://127.0.0.1:{server.server_port}/tms')
    accounts = SimpleNamespace(
        require_active_binding_descriptor=lambda account: {"system": "r13"},
        resolve_role_account_params=lambda params, **_kwargs: params,
    )
    handlers = build_daily_sign_port_handlers(account_manager=accounts)
    context = CoreBrokerInvocationContext(automation_id="isolated-daily-sign", plugin_version="2.0.1",
        tool_name="sync_daily_should_sign", operation="daily_sign.port", action="daily_sign_r13.read_r13",
        role="daily_sign_r13", account_ids=("isolated-r13",))
    try:
        with execution_capability_scope("sync_daily_should_sign_v2", ttl_seconds=30):
            outer = current_execution_capability()
            assert not authorize_tms_target(outer, "get_qianshou")
            result = handlers[("daily_sign.port", context.action)](context, {"values": {"days": 1}})
            assert result["value"]["ok"] is True, result["value"]
            assert result["value"]["data"] == expected
            assert current_execution_capability() == outer
            assert not authorize_tms_target(outer, "get_qianshou")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert seen == [{"days": 1, "r13_account_id": "isolated-r13"}]
    assert current_execution_capability() == ""
    assert tokens and all(not authorize_tms_target(token, 'get_qianshou') for token in tokens)


def test_daily_sign_port_rejects_an_unreviewed_tool_before_http():
    handlers = build_daily_sign_port_handlers(account_manager=object())
    context = CoreBrokerInvocationContext(automation_id="isolated", plugin_version="2.0.1",
        tool_name="unreviewed", operation="daily_sign.port", action="daily_sign_r13.read_r13",
        role="daily_sign_r13", account_ids=("isolated-r13",))
    with pytest.raises(PluginExecutionError, match="identity changed"):
        handlers[("daily_sign.port", context.action)](context, {"values": {"days": 1}})
