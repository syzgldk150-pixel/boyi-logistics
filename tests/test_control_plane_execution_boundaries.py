from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.execution_boundary import (
    authorize_tms_target,
    issue_execution_capability,
    revoke_execution_capability,
)
from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from agent.tms_runtime import routes
from agent.tms_runtime.account_manager import AutomationAccountManager
from feishu import message_handler


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agent"


class _Catalog:
    def __init__(self, operation_type: str) -> None:
        self.operation_type = operation_type

    def get_capability(self, _tool_name: str) -> dict[str, str]:
        return {"operation_type": self.operation_type}


class _Facade:
    def __init__(self, operation_type: str = "external_write") -> None:
        self.registry = _Catalog(operation_type)
        self.calls: list[tuple[str, dict, dict]] = []

    async def execute_tool(self, tool_name: str, params: dict, **kwargs):
        self.calls.append((tool_name, params, kwargs))
        return {"success": True, "data": {}}


def test_feishu_write_requires_stable_event_id() -> None:
    facade = _Facade("external_write")

    result = asyncio.run(
        message_handler._submit_tool_command(facade, "receipts_audit", {"receipt_id": "one"})
    )

    assert result["success"] is False
    assert result["error_code"] == "FEISHU_EVENT_ID_REQUIRED"
    assert facade.calls == []


def test_feishu_event_context_supplies_trusted_actor_and_idempotency_key() -> None:
    facade = _Facade("external_write")
    context_token = message_handler._COMMAND_CONTEXT.set(
        message_handler.FeishuCommandContext(
            event_id="evt-1",
            actor_id="user-1",
            chat_id="chat-1",
        )
    )
    try:
        result = asyncio.run(
            message_handler._submit_tool_command(facade, "receipts_audit", {"receipt_id": "one"})
        )
    finally:
        message_handler._COMMAND_CONTEXT.reset(context_token)

    assert result["success"] is True
    _, _, kwargs = facade.calls[0]
    assert kwargs["source"] == "feishu"
    assert kwargs["idempotency_key"] == "feishu:evt-1"
    assert kwargs["actor"].actor_id == "user-1"
    assert kwargs["actor"].roles == ()
    assert kwargs["execution_context"]["feishu_chat_id"] == "chat-1"


def test_execution_capability_is_owner_and_target_scoped() -> None:
    token = issue_execution_capability("receipts_audit", ttl_seconds=30)
    try:
        assert authorize_tms_target(token, "receipts_audit") is True
        assert authorize_tms_target(token, "customer_service_problem") is False
        assert authorize_tms_target("wrong", "receipts_audit") is False
    finally:
        revoke_execution_capability(token)
    assert authorize_tms_target(token, "receipts_audit") is False

    broad_token = issue_execution_capability("customer_service_problem", ttl_seconds=30)
    try:
        assert authorize_tms_target(broad_token, "customer_service_problem") is False
    finally:
        revoke_execution_capability(broad_token)

    read_token = issue_execution_capability("tms_query", ttl_seconds=30)
    try:
        assert authorize_tms_target(read_token, "tracking_query") is True
        assert authorize_tms_target(read_token, "receipts_sync") is False
    finally:
        revoke_execution_capability(read_token)

    preview_token = issue_execution_capability("preview_self_pickup_problems", ttl_seconds=30)
    try:
        assert authorize_tms_target(preview_token, "self_pickup_problem_upload") is True
        assert authorize_tms_target(preview_token, "split_pending_problem_upload") is False
    finally:
        revoke_execution_capability(preview_token)

    split_preview_token = issue_execution_capability(
        "preview_split_pending_problems",
        ttl_seconds=30,
    )
    try:
        assert authorize_tms_target(split_preview_token, "split_pending_problem_upload") is True
        assert authorize_tms_target(split_preview_token, "self_pickup_problem_upload") is False
    finally:
        revoke_execution_capability(split_preview_token)


def test_customer_service_execution_capability_is_bound_to_one_action() -> None:
    query_token = issue_execution_capability(
        "customer_service_problem_query",
        ttl_seconds=30,
    )
    try:
        assert (
            authorize_tms_target(
                query_token,
                "customer_service_problem",
                request_params={"action": "query"},
            )
            is True
        )
        assert (
            authorize_tms_target(
                query_token,
                "customer_service_problem",
                request_params={"action": "reply"},
            )
            is False
        )
        assert authorize_tms_target(query_token, "customer_service_problem") is False
    finally:
        revoke_execution_capability(query_token)

    daily_sign_token = issue_execution_capability(
        "sync_daily_should_sign",
        ttl_seconds=30,
    )
    try:
        assert authorize_tms_target(
            daily_sign_token,
            "customer_service_problem",
            request_params={"action": "query"},
        )
        for forbidden_action in ("reply", "publish", "mark_read", "upload_attachment"):
            assert not authorize_tms_target(
                daily_sign_token,
                "customer_service_problem",
                request_params={"action": forbidden_action},
            )
    finally:
        revoke_execution_capability(daily_sign_token)


def test_customer_service_compatibility_route_maps_actions_precisely() -> None:
    assert routes.CUSTOMER_SERVICE_TOOL_BY_ACTION == {
        "query": "customer_service_problem_query",
        "detail": "customer_service_problem_detail",
        "fetch_attachment": "customer_service_problem_fetch_attachment",
        "mark_read": "customer_service_problem_mark_read",
        "reply": "customer_service_problem_reply",
        "publish": "customer_service_problem_publish",
        "upload_attachment": "customer_service_problem_upload_attachment",
    }


def test_receipts_compatibility_params_drop_wide_and_sensitive_fields() -> None:
    sync_params = routes._normalize_compatibility_params(
        "receipts_sync",
        {
            "platform": "",
            "direction": "all",
            "audit_status": "待审核",
            "extra_filters": {"unexpected": "write-shaped input"},
        },
    )
    assert sync_params == {"platform": "all", "direction": "both"}

    audit_params = routes._normalize_compatibility_params(
        "receipts_audit",
        {
            "receipt_id": 7,
            "platform": "ronghui",
            "result": "passed",
            "waybill_no": "2606000040",
            "raw_payload": {"GUID": "must-not-cross-command-boundary", "token": "secret"},
        },
    )
    assert audit_params == {
        "receipt_id": 7,
        "platform": "ronghui",
        "result": "passed",
        "waybill_no": "2606000040",
    }


def test_receipts_sync_is_write_and_compatibility_reads_remain_fresh() -> None:
    assert routes._compatibility_action_is_read("receipts_sync", {}) is False
    first_key = routes._read_idempotency_key("tracking_query", {"number": "one"})
    second_key = routes._read_idempotency_key("tracking_query", {"number": "one"})
    assert first_key.startswith("legacy-tms-read:")
    assert first_key != second_key


def test_customer_service_compatibility_params_flatten_one_action_only() -> None:
    normalized = routes._normalize_compatibility_params(
        "customer_service_problem",
        {
            "platform": "ronghui",
            "account_id": "account-1",
            "action": "reply",
            "item": {
                "external_id": "problem-1",
                "waybill_no": "2606000040",
                "raw": {"unexpected": "third-party record"},
            },
            "payload": {
                "reply_text": "已处理",
                "update_fields": {"arbitrary": "write"},
            },
        },
    )
    assert normalized == {
        "platform": "ronghui",
        "account_id": "account-1",
        "external_id": "problem-1",
        "waybill_no": "2606000040",
        "reply_text": "已处理",
    }


def test_only_manual_original_page_targets_bypass_command_gateway() -> None:
    assert routes.DIRECT_MANUAL_TARGETS == set()
    assert routes.DISABLED_ACTIVE_ORIGINAL_PAGE_TARGETS == {"yunda_waybill_entry"}
    assert "receipts_audit" not in routes.DIRECT_MANUAL_TARGETS
    assert "customer_service_problem" not in routes.DIRECT_MANUAL_TARGETS
    assert "clock_in_dual" not in routes.DIRECT_MANUAL_TARGETS


def test_manual_proxy_bypass_is_bound_to_surface_and_http_method() -> None:
    assert routes.authorize_direct_manual_target(
        "ronghui_waybill_proxy",
        {
            "method": "GET",
            "path": "/module/index",
            "proxy_prefix": "/original/ronghui",
        },
        console_principal_verified=True,
    )
    assert routes.authorize_direct_manual_target(
        "yunda_waybill_proxy",
        {
            "method": "POST",
            "path": "/ky_inms/public/index.php/business/waybill/entry/save.html",
            "proxy_prefix": "/original/yunda",
        },
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "ronghui_waybill_proxy",
        {
            "method": "POST",
            "path": "/dataOperation/saveTables",
            "proxy_prefix": "/ocr/ronghui/live",
        },
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "yunda_waybill_proxy",
        {"method": "GET", "proxy_prefix": "/receipts/yunda/live"},
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "yunda_waybill_proxy",
        {"method": "POST", "proxy_prefix": "/receipts/yunda/live"},
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "ronghui_waybill_proxy",
        {"method": "POST", "proxy_prefix": "/unexpected"},
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "yunda_waybill_entry",
        {"action": "save"},
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "yunda_waybill_entry",
        {"action": "arbitrary-write"},
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "ronghui_waybill_proxy",
        {
            "method": "DELETE",
            "path": "/dataOperation/saveTables",
            "proxy_prefix": "/ocr/ronghui/live",
        },
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "ronghui_waybill_proxy",
        {
            "method": "GET",
            "path": "/dataOperation/arbitrary",
            "proxy_prefix": "/ocr/ronghui/live",
        },
        console_principal_verified=True,
    )
    assert not routes.authorize_direct_manual_target(
        "yunda_waybill_entry",
        {"action": "save"},
    )


def test_manual_proxy_rejects_dot_segments_and_encoded_separators() -> None:
    for path in (
        "/dataQuery/../dataOperation/saveTables",
        "/dataQuery/%2e%2e/dataOperation/saveTables",
        "/dataQuery/%252e%252e/dataOperation/saveTables",
        "/dataQuery/%2fdataOperation/saveTables",
        "/dataQuery/..\\dataOperation/saveTables",
    ):
        assert not routes.authorize_direct_manual_target(
            "ronghui_waybill_proxy",
            {
                "method": "GET",
                "path": path,
                "proxy_prefix": "/ocr/ronghui/live",
            },
            console_principal_verified=True,
        )


def test_phase7_templates_do_not_hide_writes_behind_tms_query() -> None:
    offenders = []
    for template in PHASE7_SCHEDULED_TASK_TEMPLATES:
        params = template.get("tool_params") if isinstance(template.get("tool_params"), dict) else {}
        endpoint = str(params.get("endpoint") or "")
        if template.get("tool_name") == "tms_query" and endpoint == "/clock_in_dual":
            offenders.append(str(template.get("id") or ""))
    assert offenders == []

    clock_templates = {
        str(template.get("id") or ""): template
        for template in PHASE7_SCHEDULED_TASK_TEMPLATES
        if str(template.get("id") or "").startswith("clockin_")
    }
    assert clock_templates == {}


def test_clock_in_compatibility_params_are_closed_and_flat() -> None:
    normalized = routes._normalize_compatibility_params(
        "clock_in_dual",
        {
            "timeout_sec": 600,
            "params": {
                "mode": "api",
                "sitecode": "7390017",
                "sitefbcode": "73901",
                "site_name": "邵阳大祥S站",
                "site_fb_name": "邵阳操作场",
                "first_type": "交件到港",
                "second_type": "接件离港",
                "delay_seconds": "2",
                "unexpected": "must-not-cross-command-boundary",
            },
        },
    )
    assert normalized == {
        "sitecode": "7390017",
        "sitefbcode": "73901",
        "sitename": "邵阳大祥S站",
        "sitefbname": "邵阳操作场",
        "first_type": "交件到港",
        "second_type": "接件离港",
        "delay_seconds": 2.0,
    }


def test_account_login_transition_publishes_only_safe_account_identity() -> None:
    row = {
        "account_id": "ronghui-default",
        "session_profile": "default",
        "system": "ronghui",
        "password": "must-not-publish",
    }
    with (
        patch("agent.tms_runtime.account_manager.publish_account_session_restored") as restored,
        patch("agent.tms_runtime.account_manager.publish_account_session_degraded") as degraded,
    ):
        AutomationAccountManager._publish_account_session_transition(
            row,
            {"status": "expired"},
            {"status": "authenticated", "token": "must-not-publish"},
        )
        payload = restored.call_args.args[0]
        assert payload["account_id"] == "ronghui-default"
        assert payload["previous_status"] == "expired"
        assert payload["status"] == "authenticated"
        assert "password" not in payload
        assert "token" not in payload
        degraded.assert_not_called()


def test_account_login_failure_publishes_degraded_event() -> None:
    with patch("agent.tms_runtime.account_manager.publish_account_session_degraded") as degraded:
        AutomationAccountManager._publish_account_session_transition(
            {"account_id": "account-1", "session_profile": "one", "system": "yunda"},
            {"status": "expired"},
            {"status": "error"},
        )
    assert degraded.call_args.args[0]["account_id"] == "account-1"


def _production_python_files() -> list[Path]:
    return [
        path
        for path in AGENT_ROOT.rglob("*.py")
        if "tests" not in path.parts
        and "__pycache__" not in path.parts
        and ".task_tmp" not in path.parts
    ]


def test_only_workflow_runner_invokes_tool_execution_port() -> None:
    offenders: list[str] = []
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "execute_step":
                relative = path.relative_to(REPO_ROOT).as_posix()
                if relative != "agent/agent/orchestration/workflow_runner.py":
                    offenders.append(f"{relative}:{node.lineno}")
    assert offenders == []


def test_entry_modules_do_not_import_tool_executor_or_business_scripts() -> None:
    entry_modules = [
        AGENT_ROOT / "feishu" / "bot.py",
        AGENT_ROOT / "feishu" / "message_handler.py",
        AGENT_ROOT / "agent" / "tms_runtime" / "routes.py",
        AGENT_ROOT / "agent" / "task_templates.py",
    ]
    banned_modules = {
        "agent.tool_executor",
        "agent.tms_runtime.scripts",
        "tools",
    }
    offenders: list[str] = []
    for path in entry_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                if module in banned_modules or module.startswith("agent.tms_runtime.scripts."):
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}:{module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if module in banned_modules or module.startswith("agent.tms_runtime.scripts."):
                        offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}:{module}")
    assert offenders == []


def test_compatibility_routes_execute_runtime_only_after_capability_check() -> None:
    path = AGENT_ROOT / "agent" / "tms_runtime" / "routes.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_handler"
    )
    direct_calls = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "execute_target"
    ]
    assert len(direct_calls) == 2

    source = ast.get_source_segment(path.read_text(encoding="utf-8"), handler) or ""
    assert "authorize_tms_target(" in source
    assert "request_params=req.params" in source
    assert "authorize_direct_manual_target(" in source
    assert "console_principal_verified=" in source
    assert "PLUGIN_ENTRY_REQUIRED" in source
    assert "_submit_compat_command" not in source


def test_routes_bind_direct_service_without_legacy_command_facade() -> None:
    service = SimpleNamespace(invoke=inspect.Signature)
    routes.bind_direct_business_service(service)
    try:
        assert routes._direct_business_service is service
        assert not hasattr(routes, "_agent_command_runtime")
    finally:
        routes.bind_direct_business_service(None)


def test_compatibility_route_rejects_malformed_json_instead_of_using_empty_params() -> None:
    class _MalformedRequest:
        headers: dict[str, str] = {}

        async def json(self):
            raise ValueError("malformed JSON")

    response = asyncio.run(routes._build_handler("receipts_sync")(_MalformedRequest()))

    assert response.status_code == 400
    assert b"INVALID_JSON_BODY" in response.body


def test_compatibility_route_rejects_non_object_json_body() -> None:
    class _ListRequest:
        headers: dict[str, str] = {}

        async def json(self):
            return []

    response = asyncio.run(routes._build_handler("receipts_sync")(_ListRequest()))

    assert response.status_code == 400
    assert b"INVALID_REQUEST_BODY" in response.body


def _direct_request(body, principal=None):
    class Request:
        headers = {}
        state = SimpleNamespace(console_principal=principal)
        async def json(self):
            return body
    return Request()


def test_external_write_requires_stable_uuid_before_platform_execution():
    from agent.tool_registry import ToolRegistry
    from agent.tms_runtime.direct_business import DirectBusinessService
    from unittest.mock import AsyncMock
    service = DirectBusinessService(registry=ToolRegistry(), target_executor=AsyncMock())
    routes.bind_direct_business_service(service)
    try:
        response = asyncio.run(routes._build_handler("receipts_audit")(_direct_request({"params": {
            "platform": "ronghui", "direction": "send", "result": "passed", "waybill_no": "R1"}},
            {"actor_id": "admin", "roles": ["super_admin"]})))
        assert response.status_code == 422 and b"REQUEST_ID_REQUIRED" in response.body
        service.target_executor.assert_not_awaited()
    finally:
        routes.bind_direct_business_service(None)


def test_receipts_archive_legacy_route_cannot_create_backlog():
    response = asyncio.run(routes._build_handler("receipts_sync")(_direct_request({"params": {
        "platform": "all", "direction": "both"}, "idempotency_key": "stable-old-id"})))
    assert response.status_code == 410 and b"PLUGIN_ENTRY_REQUIRED" in response.body


def test_direct_write_normalization_keeps_closed_input_contract():
    normalized = routes._normalize_receipts_audit_params({"platform": "ronghui", "direction": "send",
        "result": "passed", "waybill_no": "R1", "raw_payload": {"GUID": "forged"}})
    assert normalized["waybill_no"] == "R1" and "raw_payload" not in normalized


def test_internal_console_read_uses_verified_request_principal():
    from unittest.mock import AsyncMock
    service = SimpleNamespace(invoke=AsyncMock(return_value={"ok": True, "data": {"rows": []}, "error": None}))
    routes.bind_direct_business_service(service)
    try:
        principal = {"actor_id": "9", "roles": ["admin"]}
        response = asyncio.run(routes._build_handler("customer_service_problem")(_direct_request({"params": {
            "action": "query", "platform": "ronghui", "account_id": "account-1", "filters": {"direction": "received"}},
            "actor": {"actor_id": "forged"}, "source": "console"}, principal)))
        assert response["ok"] is True
        assert service.invoke.call_args.kwargs["principal"] == principal
        assert service.invoke.call_args.args[0] == "customer-service-query"
    finally:
        routes.bind_direct_business_service(None)


def test_public_compatibility_route_does_not_trust_forged_console_actor():
    from agent.tool_registry import ToolRegistry
    from agent.tms_runtime.direct_business import DirectBusinessService
    from unittest.mock import AsyncMock
    service = DirectBusinessService(registry=ToolRegistry(), target_executor=AsyncMock())
    routes.bind_direct_business_service(service)
    try:
        response = asyncio.run(routes._build_handler("customer_service_problem")(_direct_request({"params": {
            "action": "query", "platform": "ronghui", "account_id": "account-1", "filters": {"direction": "received"}},
            "actor": {"actor_id": "forged", "roles": ["super_admin"]}, "source": "console"})))
        assert response.status_code == 403
        service.target_executor.assert_not_awaited()
    finally:
        routes.bind_direct_business_service(None)
