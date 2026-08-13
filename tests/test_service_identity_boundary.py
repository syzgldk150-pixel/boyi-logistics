from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.tool_executor import (
    _redact_execution_capability,
    build_tool_subprocess_environment,
)
from agent.execution_boundary import execution_capability_scope
from shared.service_identity import (
    ConsoleIdentityError,
    ConsoleIdentityVerifier,
    build_console_identity_headers,
)


PRINCIPAL = {
    "actor_type": "console_admin",
    "actor_id": "7",
    "roles": ["admin"],
    "display_name": "Operator",
    "authenticated_by": "mysql_admin_session",
}


def _headers(*, timestamp: int = 1_800_000_000, nonce: str = "nonce_abcdefghijklmnopqrstuvwxyz"):
    return build_console_identity_headers(
        secret="dedicated-console-secret",
        method="POST",
        request_target="/internal/v1/commands?x=1",
        body=b'{"value":1}',
        principal=PRINCIPAL,
        timestamp=timestamp,
        nonce=nonce,
    )


def test_console_signature_binds_method_target_body_and_rejects_replay() -> None:
    verifier = ConsoleIdentityVerifier("dedicated-console-secret")
    headers = _headers()
    principal = verifier.verify(
        headers=headers,
        method="POST",
        request_target="/internal/v1/commands?x=1",
        body=b'{"value":1}',
        now=1_800_000_005,
    )
    assert principal == PRINCIPAL

    with pytest.raises(ConsoleIdentityError, match="already used") as replay:
        verifier.verify(
            headers=headers,
            method="POST",
            request_target="/internal/v1/commands?x=1",
            body=b'{"value":1}',
            now=1_800_000_006,
        )
    assert replay.value.code == "CONSOLE_SIGNATURE_REPLAYED"

    for method, target, body in (
        ("GET", "/internal/v1/commands?x=1", b'{"value":1}'),
        ("POST", "/internal/v1/commands?x=2", b'{"value":1}'),
        ("POST", "/internal/v1/commands?x=1", b'{"value":2}'),
    ):
        with pytest.raises(ConsoleIdentityError) as invalid:
            ConsoleIdentityVerifier("dedicated-console-secret").verify(
                headers=headers,
                method=method,
                request_target=target,
                body=body,
                now=1_800_000_005,
            )
        assert invalid.value.code == "INVALID_CONSOLE_SIGNATURE"


def test_console_signature_expires() -> None:
    with pytest.raises(ConsoleIdentityError) as expired:
        ConsoleIdentityVerifier("dedicated-console-secret").verify(
            headers=_headers(),
            method="POST",
            request_target="/internal/v1/commands?x=1",
            body=b'{"value":1}',
            now=1_800_000_031,
        )
    assert expired.value.code == "CONSOLE_SIGNATURE_EXPIRED"


def test_tool_subprocess_environment_strips_management_secrets_only() -> None:
    source = {
        "AGENT_INTERNAL_API_TOKEN": "management-token",
        "CONSOLE_AGENT_SIGNING_SECRET": "signing-secret",
        "DOCFLOW_SESSION_SECRET": "cookie-secret",
        "DOCFLOW_AGENT_WEBHOOK_TOKEN": "webhook-secret",
        "AGENT_WEBHOOK_TOKEN": "webhook-secret-2",
        "FEISHU_EVENT_VERIFICATION_TOKEN": "verification-secret",
        "FEISHU_VERIFICATION_TOKEN": "verification-secret-2",
        "DOCFLOW_BASIC_AUTH_PASS": "basic-password",
        "DOCFLOW_BASIC_AUTH_USER": "basic-user",
        "DOCFLOW_ADMIN_PASSWORD": "bootstrap-password",
        "DOCFLOW_ADMIN_USERNAME": "bootstrap-user",
        "FEISHU_APP_ID": "business-app-id",
        "AGENT_DB_HOST": "database-host",
        "PYTHONPATH": "existing-path",
    }
    with patch.dict("os.environ", source, clear=True):
        environment = build_tool_subprocess_environment("execution-only")

    for name in (
        "AGENT_INTERNAL_API_TOKEN",
        "CONSOLE_AGENT_SIGNING_SECRET",
        "DOCFLOW_SESSION_SECRET",
        "DOCFLOW_AGENT_WEBHOOK_TOKEN",
        "AGENT_WEBHOOK_TOKEN",
        "FEISHU_EVENT_VERIFICATION_TOKEN",
        "FEISHU_VERIFICATION_TOKEN",
        "DOCFLOW_BASIC_AUTH_PASS",
        "DOCFLOW_BASIC_AUTH_USER",
        "DOCFLOW_ADMIN_PASSWORD",
        "DOCFLOW_ADMIN_USERNAME",
    ):
        assert name not in environment
    assert environment["AGENT_EXECUTION_CAPABILITY"] == "execution-only"
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert environment["FEISHU_APP_ID"] == "business-app-id"
    assert environment["AGENT_DB_HOST"] == "database-host"


def test_execution_capability_is_redacted_with_or_without_a_field_name() -> None:
    capability = "ephemeral-capability-value"
    assert capability not in _redact_execution_capability(
        f"AGENT_EXECUTION_CAPABILITY={capability} bare={capability}",
        capability,
    )


def test_unsigned_http_actor_cannot_claim_console_admin_role() -> None:
    from main import _http_request_actor

    request = SimpleNamespace(state=SimpleNamespace(console_principal=None))
    actor, source = _http_request_actor(request, requested_source="legacy_api")
    assert source == "legacy_api"
    assert actor.actor_id == "internal-api"
    assert actor.roles == ()
    assert actor.authenticated_by == "internal_api_token"

    with pytest.raises(Exception, match="signed Console"):
        _http_request_actor(request, requested_source="console")


def test_signed_http_principal_overrides_forged_body_actor() -> None:
    from main import CommandRequest, _command_from_request

    request = SimpleNamespace(state=SimpleNamespace(console_principal=PRINCIPAL))
    command = _command_from_request(
        CommandRequest(
            command_type="tool.execute",
            parameters={"tool_name": "track_waybill", "arguments": {}},
            idempotency_key="console:7:tool.execute:request-1",
            source="console",
            actor={
                "actor_type": "console_admin",
                "actor_id": "attacker",
                "roles": ["super_admin"],
                "authenticated_by": "forged",
            },
            actor_roles=["super_admin"],
        ),
        request,
    )

    assert command.actor.actor_id == "7"
    assert command.actor.roles == ("admin",)
    assert command.actor.authenticated_by == "mysql_admin_session"


def test_control_plane_reads_require_signed_console_admin() -> None:
    from main import _require_console_admin_request

    unsigned = SimpleNamespace(state=SimpleNamespace(console_principal=None))
    with pytest.raises(Exception, match="signed Console"):
        _require_console_admin_request(unsigned)

    signed = SimpleNamespace(state=SimpleNamespace(console_principal=PRINCIPAL))
    actor = _require_console_admin_request(signed)
    assert actor.actor_id == "7"
    assert actor.roles == ("admin",)


def test_agent_admin_prefix_requires_console_principal() -> None:
    from main import _admin_request_requires_console_principal

    for path in (
        "/admin/accounts",
        "/admin/reload",
        "/internal/v1/admin/accounts/a/status",
        "/internal/v1/admin/tms/session/credentials",
        "/internal/v1/admin/monitoring/snapshot",
        "/internal/v1/admin/import-phase7-resources",
    ):
        assert _admin_request_requires_console_principal(path)
    for path in (
        "/internal/v1/commands",
        "/internal/v1/tools",
        "/tms/get_price",
        "/administrator",
    ):
        assert not _admin_request_requires_console_principal(path)


def _run_agent_auth_middleware(
    path: str,
    *,
    signed_principal: dict | None = None,
    authorization: str = "",
) -> tuple[int, dict, list]:
    from fastapi import Request
    from fastapi.responses import JSONResponse
    import main

    headers = {"X-Agent-Internal-Token": "service-token"}
    if authorization:
        headers["Authorization"] = authorization
    if signed_principal is not None:
        headers.update(
            build_console_identity_headers(
                secret="dedicated-console-secret",
                method="GET",
                request_target=path,
                body=b"",
                principal=signed_principal,
                nonce="middleware_nonce_abcdefghijkl",
            )
        )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [
            (name.lower().encode("ascii"), value.encode("utf-8"))
            for name, value in headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("agent.test", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    reached: list = []

    async def call_next(request):
        reached.append(getattr(request.state, "console_principal", None))
        return JSONResponse({"passed": True})

    request = Request(scope, receive)
    with patch.object(main, "AGENT_INTERNAL_API_TOKEN", "service-token"), patch.object(
        main,
        "CONSOLE_IDENTITY_VERIFIER",
        ConsoleIdentityVerifier("dedicated-console-secret"),
    ):
        response = asyncio.run(main.require_internal_api_token(request, call_next))
    return response.status_code, json.loads(response.body), reached


def test_shared_service_token_and_basic_auth_cannot_reach_agent_admin_routes() -> None:
    for path in (
        "/internal/v1/admin/accounts",
        "/internal/v1/admin/tms/session/credentials",
        "/admin/accounts",
        "/admin/reload",
    ):
        status, payload, reached = _run_agent_auth_middleware(
            path,
            authorization="Basic Zm9vOmJhcg==",
        )
        assert status == 403
        assert payload["error"]["code"] == "ACTION_FORBIDDEN"
        assert reached == []


@pytest.mark.parametrize("role", ["admin", "super_admin"])
def test_signed_mysql_console_admin_can_reach_agent_admin_routes(role: str) -> None:
    principal = {**PRINCIPAL, "roles": [role]}
    for path in (
        "/internal/v1/admin/accounts",
        "/admin/accounts",
    ):
        status, payload, reached = _run_agent_auth_middleware(
            path,
            signed_principal=principal,
        )
        assert status == 200
        assert payload == {"passed": True}
        assert reached == [principal]


def test_execution_capability_bypasses_only_exact_legacy_tms_target() -> None:
    from main import _execution_capability_authorizes_request

    class _Request:
        def __init__(self, path: str, payload: dict):
            self.method = "POST"
            self.url = SimpleNamespace(path=path)
            self._payload = payload

        async def json(self):
            return self._payload

    with execution_capability_scope("get_price", ttl_seconds=30) as capability:
        assert asyncio.run(
            _execution_capability_authorizes_request(
                _Request("/tms/get_price", {"params": {"address": "x"}}),
                capability,
            )
        )
        assert not asyncio.run(
            _execution_capability_authorizes_request(
                _Request("/tms/clock_in_dual", {"params": {}}),
                capability,
            )
        )
        assert not asyncio.run(
            _execution_capability_authorizes_request(
                _Request("/internal/v1/tms/get_price", {"params": {"address": "x"}}),
                capability,
            )
        )
