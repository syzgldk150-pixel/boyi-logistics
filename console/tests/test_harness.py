"""Focused contract tests for the fixed authenticated Console Harness slice."""

from __future__ import annotations

import io
import json
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from console.navigation import CONSOLE_MENU_REGISTRATIONS, CONSOLE_NAVIGATION
from console.permission_registry import CONSOLE_PERMISSION_BY_MENU_ID, has_console_permission
from console.routes import ConsoleRouteDispatcher
from console.routes import harness as harness_routes
from console.services.auth import AuthServiceMixin
from console.services.harness import HarnessServiceMixin
from shared.business_modules import BUSINESS_MODULE_CATALOG, CORE_MODULE_CODES


REQUEST_UUID = "123e4567-e89b-42d3-a456-426614174000"
SESSION_UUID = "123e4567-e89b-42d3-a456-426614174001"
MESSAGE_UUID = "123e4567-e89b-42d3-a456-426614174002"
PRINCIPAL = {
    "actor_type": "console_admin",
    "actor_id": "17",
    "roles": ["admin"],
    "display_name": "运营管理员",
    "authenticated_by": "mysql_admin_session",
}


class _Handler:
    def __init__(
        self,
        payload: object,
        *,
        origin: str = "https://console.test",
        user: dict[str, object] | None = None,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {
            "Host": "console.test",
            "Origin": origin,
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self.rfile = io.BytesIO(body)
        self.current_admin_user = user or {
            "id": 17,
            "username": "ops-admin",
            "display_name": "运营管理员",
            "control_plane_role": "admin",
            "is_legacy_basic_auth": False,
        }


class _HarnessApp(HarnessServiceMixin, AuthServiceMixin):
    def __init__(self, result: dict, *, principal: dict | None = PRINCIPAL) -> None:
        self.settings = SimpleNamespace(agent_timeout_seconds=11, app_title="Test Console")
        self.result = result
        self.principal = principal
        self.agent_calls: list[dict] = []
        self.sent: list[tuple[int, dict]] = []
        self.html = ""

    def _mysql_console_principal(self, _user):
        return self.principal

    def _agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
        self.agent_calls.append(
            {
                "method": method,
                "endpoint": endpoint,
                "payload": payload,
                "timeout": timeout,
                "console_principal": console_principal,
            }
        )
        return self.result

    def _send_json(self, _handler, status, payload):
        self.sent.append((int(status), payload))

    def _send_html(self, _handler, body, status=HTTPStatus.OK):
        self.html = body
        self.sent.append((int(status), {"html": body}))


def _session_result() -> dict:
    return {
        "ok": True,
        "status": 200,
        "data": {
            "session_id": SESSION_UUID,
            "request_uuid": REQUEST_UUID,
            "persistence_status": "MEMORY_ONLY_NON_PRODUCTION",
            "status": "READY",
            "availability": "OFFLINE_RESTRICTED",
            "blocked_reason": None,
            "read_only": True,
            "tools": [
                {
                    "tool_id": "knowledge.search",
                    "title": "知识检索",
                    "description": "读取受限知识投影。",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            ],
        },
    }


def _message_result() -> dict:
    return {
        "ok": True,
        "status": 200,
        "data": {
            "session_id": SESSION_UUID,
            "request_uuid": REQUEST_UUID,
            "message_id": MESSAGE_UUID,
            "persistence_status": "MEMORY_ONLY_NON_PRODUCTION",
            "status": "COMPLETED",
            "process": [{"title": "受限处理", "summary": "已完成只读检索。"}],
            "evidence": [{"title": "投影记录", "summary": "来自离线测试投影。"}],
            "result": "当前没有真实业务数据可供声明。",
            "tool_summaries": [{"title": "知识检索", "summary": "只读工具已调用。"}],
            "assistant_message": "这是受限结果。",
            "read_only": True,
            "tool_calls": 1,
        },
    }


def test_harness_is_the_fifteenth_fixed_core_module_and_navigation_entry() -> None:
    menu_ids = tuple(item.menu_id for item in CONSOLE_MENU_REGISTRATIONS)
    assert len(BUSINESS_MODULE_CATALOG) == 15
    assert len(CONSOLE_MENU_REGISTRATIONS) == 15
    assert menu_ids.index("harness") == menu_ids.index("automations") + 1
    harness_item = next(item for item in CONSOLE_NAVIGATION if item["route"] == "/harness")
    assert harness_item["label"] == "Harness 助手"
    assert harness_item["icon"] == "message-square"
    assert "harness" in CORE_MODULE_CODES
    harness_module = next(item for item in BUSINESS_MODULE_CATALOG if item.module_code == "harness")
    assert harness_module.disable_allowed is False
    assert len([item for item in BUSINESS_MODULE_CATALOG if not item.disable_allowed]) == 7


def test_harness_permission_is_registered_for_existing_admin_roles() -> None:
    permission = CONSOLE_PERMISSION_BY_MENU_ID["harness"]
    assert permission.permission_id == "console.menu.harness.view"
    assert has_console_permission("admin", permission.permission_id)
    assert has_console_permission("super_admin", permission.permission_id)
    assert not has_console_permission("legacy_admin", permission.permission_id)


def test_route_dispatcher_owns_only_fixed_harness_paths() -> None:
    app = _HarnessApp(_session_result())
    app.template_env = SimpleNamespace(
        get_template=lambda _name: SimpleNamespace(render=lambda **_values: "<html>harness</html>")
    )
    dispatcher = ConsoleRouteDispatcher()
    handler = _Handler({"request_uuid": REQUEST_UUID})

    assert dispatcher.handle_get(app, handler, "/harness", "/harness", {}) is True
    assert dispatcher.handle_post(
        app,
        handler,
        "/harness/sessions",
        "/harness/sessions",
        {},
    ) is True
    assert harness_routes.handle_get(app, handler, "/harness/plugin", "/harness/plugin", {}) is False
    assert harness_routes.handle_post(app, handler, "/harness/plugin", "/harness/plugin", {}) is False


def test_session_create_forwards_exact_body_and_signed_mysql_principal() -> None:
    app = _HarnessApp(_session_result())
    app._handle_harness_session_create(_Handler({"request_uuid": REQUEST_UUID}))

    assert app.sent[-1][0] == HTTPStatus.OK
    assert app.sent[-1][1]["ok"] is True
    assert app.sent[-1][1]["data"]["availability"] == "OFFLINE_RESTRICTED"
    assert app.agent_calls == [
        {
            "method": "POST",
            "endpoint": "/internal/v1/harness/sessions",
            "payload": {"request_uuid": REQUEST_UUID},
            "timeout": 11,
            "console_principal": PRINCIPAL,
        }
    ]


def test_message_post_forwards_only_closed_browser_fields() -> None:
    app = _HarnessApp(_message_result())
    app._handle_harness_message_post(
        _Handler(
            {
                "request_uuid": REQUEST_UUID,
                "session_id": SESSION_UUID,
                "message": "  查看受限摘要  ",
            }
        )
    )

    call = app.agent_calls[0]
    assert call["endpoint"] == "/internal/v1/harness/messages"
    assert call["payload"] == {
        "request_uuid": REQUEST_UUID,
        "session_id": SESSION_UUID,
        "message": "  查看受限摘要  ",
    }
    assert call["console_principal"] == PRINCIPAL
    assert app.sent[-1][1]["data"]["result"] == "当前没有真实业务数据可供声明。"


def test_basic_auth_fails_closed_before_body_or_agent_access() -> None:
    app = _HarnessApp(_session_result(), principal=None)
    handler = _Handler(
        {"request_uuid": REQUEST_UUID},
        user={"id": 0, "username": "emergency", "is_legacy_basic_auth": True},
    )

    app._handle_harness_session_create(handler)

    assert app.sent[-1][0] == HTTPStatus.FORBIDDEN
    assert app.sent[-1][1]["error"]["code"] == "MYSQL_ADMIN_SESSION_REQUIRED"
    assert app.agent_calls == []


def test_cross_origin_and_unknown_fields_fail_closed_before_agent_access() -> None:
    app = _HarnessApp(_session_result())
    app._handle_harness_session_create(
        _Handler({"request_uuid": REQUEST_UUID}, origin="https://attacker.test")
    )
    assert app.sent[-1][1]["error"]["code"] == "CSRF_ORIGIN_REJECTED"
    assert app.agent_calls == []

    app = _HarnessApp(_session_result())
    app._handle_harness_session_create(
        _Handler({"request_uuid": REQUEST_UUID, "tool": "forged"})
    )
    assert app.sent[-1][0] == HTTPStatus.BAD_REQUEST
    assert app.sent[-1][1]["error"]["code"] == "INVALID_HARNESS_REQUEST"
    assert app.agent_calls == []


def test_invalid_uuid_and_message_bounds_are_rejected() -> None:
    app = _HarnessApp(_session_result())
    app._handle_harness_session_create(_Handler({"request_uuid": REQUEST_UUID.upper()}))
    assert app.sent[-1][1]["error"]["code"] == "REQUEST_UUID_INVALID"
    assert app.agent_calls == []

    app = _HarnessApp(_message_result())
    app._handle_harness_message_post(
        _Handler(
            {
                "request_uuid": REQUEST_UUID,
                "session_id": SESSION_UUID,
                "message": "x" * 4001,
            }
        )
    )
    assert app.sent[-1][1]["error"]["code"] == "MESSAGE_INVALID"
    assert app.agent_calls == []


def test_agent_errors_and_unsafe_success_data_never_fallback_to_success() -> None:
    app = _HarnessApp(
        {
            "ok": False,
            "status": 503,
            "error_code": "PRODUCTION_GATED",
            "error": "生产能力尚未开放",
        }
    )
    app._handle_harness_session_create(_Handler({"request_uuid": REQUEST_UUID}))
    assert app.sent[-1][0] == HTTPStatus.SERVICE_UNAVAILABLE
    assert app.sent[-1][1]["ok"] is False
    assert app.sent[-1][1]["error"]["code"] == "PRODUCTION_GATED"

    for forbidden_key in ("service", "Service", "service "):
        app = _HarnessApp(
            {
                "ok": True,
                "status": 200,
                "data": {"session_id": SESSION_UUID, forbidden_key: "forged"},
            }
        )
        app._handle_harness_session_create(_Handler({"request_uuid": REQUEST_UUID}))
        assert app.sent[-1][0] == HTTPStatus.BAD_GATEWAY
        assert app.sent[-1][1]["error"]["code"] == "INVALID_HARNESS_RESPONSE"


def test_template_and_script_keep_host_rendered_accessible_safe_surface() -> None:
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "harness.html").read_text(encoding="utf-8")
    script = (root / "static" / "harness.js").read_text(encoding="utf-8")
    styles = (root / "static" / "style.css").read_text(encoding="utf-8")

    assert '{% extends "base.html" %}' in template
    for marker in (
        "data-harness-form",
        "data-harness-thread",
        "data-harness-welcome",
        "data-harness-process",
        "data-harness-evidence",
        "data-harness-result",
        "data-harness-tool-summaries",
        'maxlength="4000"',
        'aria-live="polite"',
    ):
        assert marker in template
    assert "https://" not in template
    assert "http://" not in template
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert '"/harness/sessions"' in script
    assert '"/harness/messages"' in script
    assert "appendMessage(\"user\"" in script
    assert "appendMessage(\"assistant\"" in script
    assert "event.isComposing" in script
    assert "安全运行环境暂不可用" in script
    assert "输入只读查询" in template
    assert "harness-layout" not in template
    assert ".harness-page" in styles
    assert ".harness-thread" in styles
    assert ".harness-message--user" in styles
    assert ":focus" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
