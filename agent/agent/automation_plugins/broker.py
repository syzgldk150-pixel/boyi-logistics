"""Single-use local broker for credential-free core adapter access."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from shared.redaction import redact_text


_OPERATIONS = frozenset(
    {
        "browser.invoke",
        "office.invoke",
        "file.read",
        "file.write",
        "network.request",
        "projection.invoke",
        "ledger.invoke",
    }
)
_SENSITIVE_KEYS = ("password", "cookie", "credential", "secret", "token", "session")


def _is_account_identifier_key(value: object) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return normalized in {"account_id", "account_ids"} or normalized.endswith(
        ("_account_id", "_account_ids")
    )


@dataclass(frozen=True)
class BrokerGrant:
    automation_id: str
    plugin_version: str
    tool_name: str
    expires_at: datetime
    runtime_permissions: Mapping[str, object]
    account_roles: tuple[Mapping[str, object], ...]
    resource_roles: tuple[Mapping[str, object], ...]
    account_bindings: Mapping[str, object]
    resource_bindings: Mapping[str, str]


@dataclass
class _BrokerGrantState:
    grant: BrokerGrant
    remaining_calls: int
    request_ids: set[str]
    consumed_calls: int = 0


@runtime_checkable
class CoreAutomationBrokerAdapterPort(Protocol):
    async def invoke(
        self,
        *,
        grant: BrokerGrant,
        operation: str,
        action: str,
        role: str,
        binding: object,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Revalidate exact sessions/resources and return redacted business data.

        Account-backed operations must fail ``BLOCKED_LOGIN`` when any selected
        account is missing, inactive or unauthenticated. Implementations may
        never substitute another account or return credential/session values.
        """


class LocalBrokerCapabilityIssuer:
    """Store token digests and a bounded, replay-protected invocation grant."""

    def __init__(self, socket_path: Path | str) -> None:
        self._socket_path = Path(socket_path).resolve()
        if self._socket_path == self._socket_path.parent:
            raise ValueError("broker socket path is unsafe")
        self._grants: dict[str, _BrokerGrantState] = {}
        self._lock = threading.Lock()

    @property
    def broker_endpoint(self) -> str:
        return f"unix://{self._socket_path}"

    @property
    def broker_socket_path(self) -> Path:
        return self._socket_path

    def issue(
        self,
        *,
        automation_id: str,
        plugin_version: str,
        tool_name: str,
        ttl_seconds: int,
        runtime_permissions: Mapping[str, object],
        account_roles: Sequence[Mapping[str, object]],
        resource_roles: Sequence[Mapping[str, object]],
        account_bindings: Mapping[str, object],
        resource_bindings: Mapping[str, str],
    ) -> str:
        ttl = max(1, min(int(ttl_seconds), 3600))
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        grant = BrokerGrant(
            automation_id=str(automation_id),
            plugin_version=str(plugin_version),
            tool_name=str(tool_name),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
            runtime_permissions=dict(runtime_permissions),
            account_roles=tuple(dict(role) for role in account_roles),
            resource_roles=tuple(dict(role) for role in resource_roles),
            account_bindings=dict(account_bindings),
            resource_bindings=dict(resource_bindings),
        )
        raw_limit = runtime_permissions.get("max_broker_calls")
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 0 <= raw_limit <= 1000:
            raise PluginExecutionError(
                "signed broker call limit is invalid",
                code="BROKER_CONTRACT_INVALID",
            )
        with self._lock:
            now = datetime.now(timezone.utc)
            self._grants = {
                key: state
                for key, state in self._grants.items()
                if state.grant.expires_at > now
            }
            self._grants[digest] = _BrokerGrantState(
                grant=grant,
                remaining_calls=raw_limit,
                request_ids=set(),
            )
        return token

    def revoke(self, capability: str) -> None:
        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        with self._lock:
            self._grants.pop(digest, None)

    def consumed_call_count(self, capability: str) -> int:
        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        with self._lock:
            state = self._grants.get(digest)
            if state is None:
                raise PluginExecutionError(
                    "core broker capability observation is unavailable",
                    code="BROKER_OBSERVATION_UNAVAILABLE",
                )
            return state.consumed_calls

    def consume(
        self,
        capability: str,
        *,
        request_id: str,
        operation: str,
        action: str,
        role: str,
    ) -> tuple[BrokerGrant, object]:
        if operation not in _OPERATIONS:
            raise PluginExecutionError("core broker operation is unsupported", code="BROKER_OPERATION_DENIED")
        try:
            normalized_request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginExecutionError(
                "core broker request_id is invalid",
                code="BROKER_REQUEST_INVALID",
            ) from exc
        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        with self._lock:
            state = self._grants.get(digest)
            if state is None or datetime.now(timezone.utc) >= state.grant.expires_at:
                self._grants.pop(digest, None)
                state = None
            elif normalized_request_id in state.request_ids:
                raise PluginExecutionError(
                    "core broker request replayed",
                    code="BROKER_REQUEST_REPLAYED",
                )
            elif state.remaining_calls <= 0:
                raise PluginExecutionError(
                    "core broker call limit was exhausted",
                    code="BROKER_CALL_LIMIT",
                )
            else:
                state.request_ids.add(normalized_request_id)
                state.remaining_calls -= 1
                state.consumed_calls += 1
        if state is None:
            raise PluginExecutionError("core broker capability is invalid or expired", code="BROKER_CAPABILITY_INVALID")
        grant = state.grant
        raw_contracts = grant.runtime_permissions.get("broker_operations")
        if not isinstance(raw_contracts, list):
            raise PluginExecutionError("signed broker contract is invalid", code="BROKER_CONTRACT_INVALID")
        matches = [
            contract
            for contract in raw_contracts
            if isinstance(contract, Mapping)
            and contract.get("operation") == operation
            and contract.get("action") == action
        ]
        if len(matches) != 1:
            raise PluginExecutionError(
                "broker operation/action is not signed for this plugin",
                code="BROKER_OPERATION_DENIED",
            )
        allowed_roles = matches[0].get("roles")
        if not isinstance(allowed_roles, list) or role not in allowed_roles:
            raise PluginExecutionError(
                "broker role is not signed for this operation",
                code="BROKER_ROLE_DENIED",
            )
        if operation.startswith("browser.") and grant.runtime_permissions.get("browser") is not True:
            raise PluginExecutionError("browser adapter was not signed for this plugin", code="BROKER_OPERATION_DENIED")
        if operation.startswith("office.") and grant.runtime_permissions.get("office") is not True:
            raise PluginExecutionError("Office adapter was not signed for this plugin", code="BROKER_OPERATION_DENIED")
        if operation.startswith("network.") and grant.runtime_permissions.get("network") is not True:
            raise PluginExecutionError("network adapter was not signed for this plugin", code="BROKER_OPERATION_DENIED")
        account_roles = {str(item.get("role") or "") for item in grant.account_roles}
        resource_roles = {str(item.get("role") or "") for item in grant.resource_roles}
        if role in account_roles and role in resource_roles:
            raise PluginExecutionError("broker role declaration is ambiguous", code="BROKER_CONTRACT_INVALID")
        if role in account_roles:
            if role not in grant.account_bindings:
                raise PluginExecutionError("account role is unbound", code="BROKER_ROLE_UNBOUND")
            return grant, grant.account_bindings[role]
        if role in resource_roles:
            if role not in grant.resource_bindings:
                raise PluginExecutionError("resource role is unbound", code="BROKER_ROLE_UNBOUND")
            return grant, grant.resource_bindings[role]
        raise PluginExecutionError("broker role is undeclared", code="BROKER_ROLE_UNBOUND")


def _assert_redacted(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if (
                any(marker in normalized for marker in _SENSITIVE_KEYS)
                or _is_account_identifier_key(key)
            ):
                raise PluginExecutionError("core broker adapter returned sensitive data")
            _assert_redacted(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_redacted(item)


class LocalCoreAutomationBroker:
    """Length-bounded JSON-over-Unix-socket broker; one request per token."""

    def __init__(
        self,
        *,
        issuer: LocalBrokerCapabilityIssuer,
        adapter: CoreAutomationBrokerAdapterPort,
    ) -> None:
        self._issuer = issuer
        self._adapter = adapter
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        path = self._issuer.broker_socket_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._reclaim_dead_sibling_sockets(path)
        if path.exists():
            if path.is_symlink() or not path.is_socket():
                raise PluginExecutionError("refusing to replace an unsafe broker endpoint")
            path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(path))
        path.chmod(0o600)

    @staticmethod
    def _reclaim_dead_sibling_sockets(current: Path) -> None:
        """Remove only dead-agent sibling Unix sockets; preserve all other entries."""

        pattern = re.compile(r"agent-([1-9][0-9]*)\.sock\Z")
        try:
            entries = tuple(current.parent.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry == current:
                continue
            matched = pattern.fullmatch(entry.name)
            if matched is None:
                continue
            try:
                if entry.is_symlink() or not entry.is_socket():
                    continue
                pid = int(matched.group(1))
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    entry.unlink()
                except PermissionError:
                    continue
                except OSError as exc:
                    if exc.errno != errno.ESRCH:
                        continue
                    entry.unlink()
            except OSError:
                continue

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        path = self._issuer.broker_socket_path
        if path.exists() and path.is_socket() and not path.is_symlink():
            path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, Any]
        try:
            payload = await reader.readline()
            if not payload or len(payload) > 1024 * 1024 or not payload.endswith(b"\n"):
                raise PluginExecutionError("core broker request is empty or too large")
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {
                "schema_version",
                "request_id",
                "capability",
                "operation",
                "action",
                "role",
                "arguments",
            }:
                raise PluginExecutionError("core broker request schema is invalid")
            if request.get("schema_version") != 1 or not isinstance(request.get("arguments"), dict):
                raise PluginExecutionError("core broker request fields are invalid")
            uuid.UUID(str(request.get("request_id") or ""))
            grant, binding = self._issuer.consume(
                str(request.get("capability") or ""),
                request_id=str(request.get("request_id") or ""),
                operation=str(request.get("operation") or ""),
                action=str(request.get("action") or ""),
                role=str(request.get("role") or ""),
            )
            result = await self._adapter.invoke(
                grant=grant,
                operation=str(request["operation"]),
                action=str(request["action"]),
                role=str(request["role"]),
                binding=binding,
                arguments=dict(request["arguments"]),
            )
            _assert_redacted(result)
            response = {"ok": True, "data": dict(result)}
        except Exception as exc:
            response = {
                "ok": False,
                "error_code": getattr(exc, "code", type(exc).__name__.upper())[:64],
                "error": redact_text(exc)[:300],
            }
        data = canonical_json_bytes(response)
        if len(data) > 10 * 1024 * 1024:
            data = canonical_json_bytes({"ok": False, "error_code": "BROKER_RESPONSE_TOO_LARGE"})
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
