"""Agent composition boundary for the memory-only AI assistant conversation.

The low-level :mod:`agent.harness` package intentionally contains only closed
value objects and ports.  This module is the small application layer that
binds those objects to a signed Console administrator, an injected sidecar,
and the project-policy invocation bridge.  It does not load configuration,
open files, use a database, or contact a business system.

The composition root injects the active model adapter and the closed read-only
gateway.  Offline sidecars remain available to tests, but are never an implicit
production fallback.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from agent.harness.catalog import FixedHarnessTool, ManagedToolHandle, ToolDescriptor
from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage, HarnessSession, canonical_uuid, strict_json
from agent.harness.sessions import InMemoryHarnessSessionRepository
from agent.harness.sidecar import SidecarResult
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.redaction import redact_text
from shared.identity_permissions import tool_permission, plugin_permission


MEMORY_ONLY = "MEMORY_ONLY"
_SIGNED_CONSOLE_AUTHENTICATION = "mysql_admin_session"
_ADMIN_ROLES = frozenset({"admin", "super_admin"})
_FORBIDDEN_RECEIPT_KEYS = frozenset(
    {
        "automation_id",
        "service",
        "operation",
        "account_id",
        "resource_id",
        "provider_id",
        "contribution_id",
        "contract_id",
        "contract_hash",
        "path",
        "source_code",
        "file",
        "filename",
        "module",
        "plugin_id",
        "package",
        "package_path",
    }
)
_MESSAGE_NAMESPACE = uuid.UUID("6c3d6d7e-f934-4b1b-86c0-5f33c33d3b94")
_INVOCATION_NAMESPACE = uuid.UUID("76e2f268-6544-4a17-96d4-8e51f0a4a80c")


class HarnessSidecar(Protocol):
    """The bounded sidecar contract used by the conversation service."""

    def run(
        self,
        *,
        messages: Sequence[HarnessMessage],
        timeout_seconds: int,
    ) -> SidecarResult: ...


# The composition root binds both the exact signed Console principal and the
# canonical request UUID before constructing a sidecar/catalog instance.
HarnessSidecarFactory = Callable[[Actor, str], HarnessSidecar]
ReadOnlyFixedHandler = Callable[[Mapping[str, Any]], object]


def _error(message: str, code: str) -> HarnessError:
    return HarnessError(message, code=code)


def _normalize_admin_actor(actor: object) -> Actor:
    """Return the exact security fields used by a signed Console principal.

    ``Actor`` already strips surrounding whitespace from ``actor_id`` and
    removes empty role values.  The application boundary additionally
    lower-cases and sorts roles so a signed request cannot change its meaning
    through role casing or order.  Display name is intentionally not part of
    the authorization identity.
    """

    if not isinstance(actor, Actor):
        raise _error("A signed Console administrator is required", "HARNESS_PRINCIPAL_INVALID")
    if actor.actor_type is not ActorType.CONSOLE_ADMIN:
        raise _error("Harness requires a Console administrator", "HARNESS_PRINCIPAL_INVALID")
    if actor.authenticated_by != _SIGNED_CONSOLE_AUTHENTICATION:
        raise _error("Harness requires a MySQL administrator session", "HARNESS_PRINCIPAL_INVALID")
    if not isinstance(actor.roles, tuple):
        raise _error("Signed Console roles are invalid", "HARNESS_PRINCIPAL_INVALID")
    roles = tuple(sorted({str(role).strip().lower() for role in actor.roles if str(role).strip()}))
    if not _ADMIN_ROLES.intersection(roles):
        raise _error("Harness requires an administrator role", "HARNESS_PRINCIPAL_INVALID")
    if not actor.actor_id or actor.actor_id != actor.actor_id.strip():
        raise _error("Signed Console actor identity is invalid", "HARNESS_PRINCIPAL_INVALID")
    return Actor(
        actor_type=ActorType.CONSOLE_ADMIN,
        actor_id=actor.actor_id,
        roles=roles,
        display_name=actor.display_name,
        authenticated_by=_SIGNED_CONSOLE_AUTHENTICATION,
    )


def bind_signed_console_admin(actor: object) -> Actor:
    """Public fail-closed validator for the signed Console principal."""

    return _normalize_admin_actor(actor)


def _normalize_chat_actor(actor: object) -> Actor:
    """Accept authenticated channels without turning Feishu into a Console user."""
    if isinstance(actor, Actor) and actor.actor_type is ActorType.FEISHU_USER:
        if actor.authenticated_by != "feishu_admin_binding" or not _ADMIN_ROLES.intersection(actor.roles):
            raise _error("飞书账号没有绑定有效身份", "HARNESS_PRINCIPAL_INVALID")
        return actor
    return _normalize_admin_actor(actor)


def _principal_fingerprint(actor: Actor) -> tuple[str, str, tuple[str, ...], str]:
    return (
        actor.actor_type.value,
        actor.actor_id,
        tuple(actor.roles),
        actor.authenticated_by,
    )


def _message_id(kind: str, *, session_id: str, request_id: str) -> str:
    return str(uuid.uuid5(_MESSAGE_NAMESPACE, f"{kind}:{session_id}:{request_id}"))


def _message_request_id(kind: str, *, session_id: str, request_id: str) -> str:
    """Use separate repository idempotency keys for user and assistant rows."""

    return str(uuid.uuid5(_MESSAGE_NAMESPACE, f"repository:{kind}:{session_id}:{request_id}"))


def _message_mapping(message: HarnessMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "message_id": message.message_id,
        "created_at": message.created_at.isoformat(),
    }


def _session_mapping(session: HarnessSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "messages": [_message_mapping(item) for item in session.messages],
        "created_at": session.created_at.isoformat(),
        "persistence_status": session.persistence_status,
    }


@dataclass(frozen=True)
class HarnessSessionReceipt:
    """Result of an idempotent session creation request."""

    session: HarnessSession
    request_id: str
    replayed: bool

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def persistence_status(self) -> str:
        return MEMORY_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "replayed": self.replayed,
            **_session_mapping(self.session),
        }


@dataclass(frozen=True)
class HarnessMessageReceipt:
    """Result of one bounded user/assistant exchange."""

    session: HarnessSession
    user_message: HarnessMessage
    assistant_message: HarnessMessage
    request_id: str
    replayed: bool
    tool_calls: int
    plugin_invocations: tuple[Mapping[str, Any], ...] = ()
    plugin_requests: tuple[object, ...] = ()

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def persistence_status(self) -> str:
        return MEMORY_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "replayed": self.replayed,
            "tool_calls": self.tool_calls,
            "persistence_status": self.persistence_status,
            "user_message": _message_mapping(self.user_message),
            "assistant_message": _message_mapping(self.assistant_message),
            "messages": [_message_mapping(item) for item in self.session.messages],
        }


class HarnessConversationService:
    """Process-bounded conversation service over the memory-only repository."""

    persistence_status = MEMORY_ONLY

    def __init__(
        self,
        *,
        repository: InMemoryHarnessSessionRepository,
        sidecar_factory: HarnessSidecarFactory | None = None,
        timeout_seconds: int = 5,
        plugin_conversations=None,
        identity_access=None,
        text_queries=None,
    ) -> None:
        if not isinstance(repository, InMemoryHarnessSessionRepository):
            raise TypeError("HarnessConversationService requires the memory-only repository")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValueError("Harness sidecar timeout must be an integer from 1 to 30")
        self._repository = repository
        self._sidecar_factory = (
            production_gated_sidecar_factory
            if sidecar_factory is None
            else sidecar_factory
        )
        self._timeout_seconds = timeout_seconds
        self._lock = RLock()
        self._session_locks: dict[str, RLock] = {}
        self._session_principals: dict[str, tuple[str, str, tuple[str, ...], str]] = {}
        self._message_tool_calls: dict[str, int] = {}
        self._message_plugins: dict[str, tuple[Mapping[str, Any], ...]] = {}
        self._message_plugin_requests: dict[str, tuple[object, ...]] = {}
        self._pending_sidecars: dict[str, HarnessSidecar] = {}
        self._session_invocations: dict[str, set[str]] = {}
        self._plugin_conversations = plugin_conversations
        self._identity_access = identity_access
        self._text_queries = text_queries

    def _chat_actor(self, actor: Actor) -> Actor:
        actor = _normalize_chat_actor(actor)
        if self._identity_access is not None and not self._identity_access.allows(actor, "ai.chat"):
            raise _error("当前身份没有 AI 对话权限", "HARNESS_PERMISSION_DENIED")
        return actor

    def _session_lock(self, session_id: str):
        with self._lock:
            lock = self._session_locks.get(session_id)
        if lock is None:
            raise _error("Session principal binding is unavailable", "HARNESS_PRINCIPAL_MISMATCH")
        return lock

    @property
    def plugin_execution_enabled(self) -> bool:
        return self._plugin_conversations is not None

    def plugin_action(self, *, actor: Actor, session_id: str, invocation_id: str, request_id: str, action: str, selected_indices: list[int]) -> dict[str, Any]:
        bound_actor = _normalize_admin_actor(actor)
        self._chat_actor(bound_actor)
        session_id = canonical_uuid(session_id, field_name="session_id")
        invocation_id = canonical_uuid(invocation_id, field_name="invocation_id")
        request_id = canonical_uuid(request_id, field_name="request_id")
        if (action not in {"status", "confirm", "cancel"} or not isinstance(selected_indices, list)
                or len(selected_indices) > 100 or any(type(index) is not int or index < 0 for index in selected_indices)
                or len(set(selected_indices)) != len(selected_indices) or (action != "confirm" and selected_indices)):
            raise _error("插件操作参数无效", "HARNESS_PLUGIN_REQUEST_INVALID")
        with self._session_lock(session_id):
            self._chat_actor(bound_actor)
            if self._plugin_conversations is None or self._session_principals.get(session_id) != _principal_fingerprint(bound_actor):
                raise _error("会话权限不匹配", "HARNESS_PRINCIPAL_MISMATCH")
            self._repository.get(principal_id=bound_actor.actor_id, session_id=session_id)
            if invocation_id not in self._session_invocations.get(session_id, set()):
                raise _error("本次执行不属于当前会话", "HARNESS_PLUGIN_FORBIDDEN")
            try:
                result = self._plugin_conversations.console_action(actor=bound_actor, invocation_id=invocation_id,
                    action=action, request_id=request_id, selected_indices=selected_indices)
            except OrchestrationError as exc:
                raise _error(redact_text(str(exc))[:500], "HARNESS_PLUGIN_REQUEST_INVALID") from exc
            self._session_invocations[session_id].add(result["invocation_id"])
            return result

    def create_session(self, *, actor: Actor, request_id: str) -> HarnessSessionReceipt:
        bound_actor = self._chat_actor(actor)
        safe_request_id = canonical_uuid(request_id, field_name="request_id")
        with self._lock:
            session = self._repository.create_or_get(
                principal_id=bound_actor.actor_id,
                request_id=safe_request_id,
            )
            fingerprint = _principal_fingerprint(bound_actor)
            previous = self._session_principals.get(session.session_id)
            if previous is not None and previous != fingerprint:
                raise _error("Session principal binding changed", "HARNESS_PRINCIPAL_MISMATCH")
            self._session_principals[session.session_id] = fingerprint
            self._session_locks.setdefault(session.session_id, RLock())
            return HarnessSessionReceipt(
                session=session,
                request_id=safe_request_id,
                replayed=previous is not None,
            )

    def send_message(
        self,
        *,
        actor: Actor,
        session_id: str,
        request_id: str,
        message: str,
    ) -> HarnessMessageReceipt:
        bound_actor = self._chat_actor(actor)
        safe_session_id = canonical_uuid(session_id, field_name="session_id")
        safe_request_id = canonical_uuid(request_id, field_name="request_id")
        if not isinstance(message, str) or not message.strip():
            raise _error("Harness message must be text", "HARNESS_MESSAGE_INVALID")
        with self._session_lock(safe_session_id):
            self._chat_actor(bound_actor)
            fingerprint = self._session_principals.get(safe_session_id)
            if fingerprint is None:
                # Never infer an authorization binding from the repository's
                # principal_id alone after an application restart or injection.
                raise _error("Session principal binding is unavailable", "HARNESS_PRINCIPAL_MISMATCH")
            if fingerprint != _principal_fingerprint(bound_actor):
                raise _error("Session belongs to another signed principal", "HARNESS_PRINCIPAL_MISMATCH")
            session = self._repository.get(
                principal_id=bound_actor.actor_id,
                session_id=safe_session_id,
            )
            assistant_id = _message_id(
                "assistant",
                session_id=safe_session_id,
                request_id=safe_request_id,
            )
            user_id = _message_id(
                "user",
                session_id=safe_session_id,
                request_id=safe_request_id,
            )
            existing_assistant = next(
                (item for item in session.messages if item.message_id == assistant_id),
                None,
            )
            existing_user = next(
                (item for item in session.messages if item.message_id == user_id),
                None,
            )
            if existing_assistant is not None:
                if existing_user is None or existing_user.content != message or existing_user.role != "user":
                    raise _error("Message replay is inconsistent", "HARNESS_IDEMPOTENCY_CONFLICT")
                if assistant_id not in self._message_tool_calls:
                    raise _error("Message replay is incomplete", "HARNESS_IDEMPOTENCY_CONFLICT")
                return HarnessMessageReceipt(
                    session=session,
                    user_message=existing_user,
                    assistant_message=existing_assistant,
                    request_id=safe_request_id,
                    replayed=True,
                    tool_calls=self._message_tool_calls[assistant_id],
                    plugin_invocations=self._message_plugins.get(assistant_id, ()),
                    plugin_requests=self._message_plugin_requests.get(assistant_id, ()),
                )

            clearing = message.strip() == "清空会话"
            if clearing:
                old_session = self._repository.clear_messages(principal_id=bound_actor.actor_id, session_id=safe_session_id)
                for old_message in old_session.messages:
                    for cache in (self._message_tool_calls, self._message_plugins, self._message_plugin_requests, self._pending_sidecars):
                        cache.pop(old_message.message_id, None)
                # This only clears model context. Running plugins and their durable
                # execution records remain available in the automation module.
                self._session_invocations.pop(safe_session_id, None)

            user_message = HarnessMessage(
                role="user",
                content=message,
                message_id=user_id,
            )
            user_message = self._repository.append_message(
                principal_id=bound_actor.actor_id,
                session_id=safe_session_id,
                request_id=_message_request_id(
                    "user",
                    session_id=safe_session_id,
                    request_id=safe_request_id,
                ),
                message=user_message,
            )
            session_with_user = self._repository.get(
                principal_id=bound_actor.actor_id,
                session_id=safe_session_id,
            )
            # Exercise the domain append invariant before invoking the sidecar
            # so a full session cannot consume a model/tool call budget.
            session_with_user.append(
                HarnessMessage(
                    role="assistant",
                    content="",
                    message_id=assistant_id,
                )
            )
            sidecar = self._pending_sidecars.get(assistant_id)
            fixed_result = (SidecarResult("已清空会话。已发起的插件继续执行，结果可在自动化记录中查看。", 0)
                            if clearing else self._text_queries.reply(bound_actor, message, safe_request_id)
                            if self._text_queries is not None else None)
            if sidecar is None and fixed_result is None:
                sidecar = self._build_sidecar(bound_actor, safe_request_id)
                self._pending_sidecars[assistant_id] = sidecar
            try:
                result = fixed_result if fixed_result is not None else sidecar.run(
                    messages=session_with_user.messages,
                    timeout_seconds=self._timeout_seconds,
                )
            except HarnessError:
                raise
            except Exception as exc:
                raise _error("Harness sidecar failed", "HARNESS_SIDECAR_FAILED") from exc
            if not isinstance(result, SidecarResult):
                raise _error("Harness sidecar returned an invalid result", "HARNESS_PROTOCOL_INVALID")
            assistant_message = HarnessMessage(
                role="assistant",
                content=result.content,
                message_id=assistant_id,
            )
            assistant_message = self._repository.append_message(
                principal_id=bound_actor.actor_id,
                session_id=safe_session_id,
                request_id=_message_request_id(
                    "assistant",
                    session_id=safe_session_id,
                    request_id=safe_request_id,
                ),
                message=assistant_message,
            )
            self._message_tool_calls[assistant_id] = result.tool_calls
            self._message_plugins[assistant_id] = result.plugin_invocations
            self._message_plugin_requests[assistant_id] = result.plugin_requests
            self._session_invocations.setdefault(safe_session_id, set()).update(
                item["invocation_id"] for item in result.plugin_invocations if item.get("invocation_id")
            )
            self._pending_sidecars.pop(assistant_id, None)
            final_session = self._repository.get(
                principal_id=bound_actor.actor_id,
                session_id=safe_session_id,
            )
            self._chat_actor(bound_actor)
            return HarnessMessageReceipt(
                session=final_session,
                user_message=user_message,
                assistant_message=assistant_message,
                request_id=safe_request_id,
                replayed=False,
                tool_calls=result.tool_calls,
                plugin_invocations=result.plugin_invocations,
                plugin_requests=result.plugin_requests,
            )

    def _build_sidecar(self, actor: Actor, request_id: str) -> HarnessSidecar:
        try:
            sidecar = self._sidecar_factory(actor, request_id)
        except HarnessError:
            raise
        except Exception as exc:
            raise _error("Harness sidecar is unavailable", "HARNESS_SIDECAR_UNAVAILABLE") from exc
        if not callable(getattr(sidecar, "run", None)):
            raise _error("Harness sidecar is invalid", "HARNESS_PROTOCOL_INVALID")
        return sidecar


class ProductionGatedHarnessSidecar:
    """Explicit placeholder until production LLM/sandbox wiring is approved."""

    def run(self, *, messages: Sequence[HarnessMessage], timeout_seconds: int) -> SidecarResult:
        del messages, timeout_seconds
        raise _error(
            "Harness production sidecar is not enabled",
            "HARNESS_RUNTIME_PRODUCTION_GATED",
        )


class ProductionGatedHarnessSidecarFactory:
    """Factory that never silently substitutes an unrestricted runtime."""

    def __call__(self, actor: Actor | None = None, request_id: str | None = None) -> HarnessSidecar:
        del actor, request_id
        raise _error(
            "Harness production sidecar is not enabled",
            "HARNESS_RUNTIME_PRODUCTION_GATED",
        )

    def create(self) -> HarnessSidecar:
        return self()


def production_gated_sidecar_factory(
    actor: Actor | None = None,
    request_id: str | None = None,
) -> HarnessSidecar:
    return ProductionGatedHarnessSidecarFactory()(actor, request_id)


class _FixedHarnessHandle:
    """Opaque fixed-tool token; no public identity or callable is exposed."""

    __slots__ = ("_handle_id",)

    def __init__(self, handle_id: str) -> None:
        self._handle_id = handle_id

    def __repr__(self) -> str:
        return "<private-harness-handle>"


FIXED_HARNESS_TOOL_IDS = (
    "knowledge.search",
    "waybill.lookup",
    "tracking.lookup",
    "work_items.list_open",
    "finance.summary",
    "runs.get_summary",
    "artifact.inspect",
)

_FIXED_ARGUMENT_KEYS = {
    "knowledge.search": frozenset({"query", "limit"}),
    "waybill.lookup": frozenset({"waybill_number"}),
    "tracking.lookup": frozenset({"tracking_number"}),
    "finance.summary": frozenset({"start_date", "end_date"}),
    "work_items.list_open": frozenset({"limit"}),
    "runs.get_summary": frozenset({"run_id"}),
    "artifact.inspect": frozenset({"artifact_id"}),
}


def _object_schema(properties: Mapping[str, Mapping[str, Any]], required: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": copy.deepcopy(dict(properties)),
        "required": list(required),
        "additionalProperties": False,
    }


def build_fixed_harness_tools() -> tuple[FixedHarnessTool, ...]:
    """Build the host-rendered read-only tools without a default gateway."""

    string_id = {"type": "string", "maxLength": 191}
    descriptors = (
        ToolDescriptor(
            tool_id="knowledge.search",
            title="查询业务知识",
            description="查询已经整理并允许读取的业务知识。",
            input_schema=_object_schema(
                {
                    "query": {"type": "string", "maxLength": 500},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                ("query", "limit"),
            ),
        ),
        ToolDescriptor(
            tool_id="waybill.lookup",
            title="查询运单信息",
            description="按完整运单号精确查询一票运单。",
            input_schema=_object_schema({"waybill_number": string_id}, ("waybill_number",)),
        ),
        ToolDescriptor(
            tool_id="tracking.lookup",
            title="查询物流轨迹",
            description="按完整运单号查询物流轨迹。",
            input_schema=_object_schema({"tracking_number": string_id}, ("tracking_number",)),
        ),
        ToolDescriptor(
            tool_id="work_items.list_open",
            title="查看待处理事项",
            description="查看当前待处理、受阻或需要关注的事项。",
            input_schema=_object_schema(
                {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                ("limit",),
            ),
        ),
        ToolDescriptor(
            tool_id="finance.summary",
            title="查询财务汇总",
            description="读取指定日期范围的真实财务汇总，用于回答和分析；不触发采集。",
            input_schema=_object_schema(
                {"start_date": {"type": "string", "maxLength": 10},
                 "end_date": {"type": "string", "maxLength": 10}},
                ("start_date", "end_date"),
            ),
        ),
        ToolDescriptor(
            tool_id="runs.get_summary",
            title="查看任务运行结果",
            description="按任务运行编号查看安全摘要。",
            input_schema=_object_schema({"run_id": string_id}, ("run_id",)),
        ),
        ToolDescriptor(
            tool_id="artifact.inspect",
            title="查看运行证据",
            description="按证据编号查看脱敏后的运行证据摘要。",
            input_schema=_object_schema({"artifact_id": string_id}, ("artifact_id",)),
        ),
    )
    return tuple(
        FixedHarnessTool(
            descriptor=descriptor,
            opaque_handle=_FixedHarnessHandle(descriptor.tool_id),
            effect="read",
            harness_allowed=True,
            broker_effect="read",
        )
        for descriptor in descriptors
    )


class TrustedHarnessInvocationAdapter:
    """Bridge opaque Harness handles to policy or explicitly supplied fakes.

    Dynamic handles are accepted only as the private ``ManagedToolHandle``
    type.  ``allowed_dynamic_handles`` can be supplied by the composition root
    to close the set further; omitting it still rejects every other Python
    object and relies on the catalog's active-generation re-resolution.
    Fixed handlers are an explicit closed mapping from fixed handle IDs to
    read-only callables.  There is no default gateway.
    """

    def __init__(
        self,
        *,
        policy_service: Any,
        actor: Actor,
        base_request_id: str,
        fixed_handlers: Mapping[str, ReadOnlyFixedHandler] | None = None,
        allowed_dynamic_handles: Iterable[ManagedToolHandle] | None = None,
    ) -> None:
        self._policy_service = policy_service
        self._actor = _normalize_chat_actor(actor)
        self._base_request_id = canonical_uuid(base_request_id, field_name="request_id")
        self._call_index = 0
        self._lock = RLock()
        self._allowed_dynamic_handles = (
            None
            if allowed_dynamic_handles is None
            else frozenset(allowed_dynamic_handles)
        )
        self._fixed_handlers = self._normalize_fixed_handlers(fixed_handlers)

    def visible_tool_filter(self):
        authority = getattr(self._policy_service, "identity_access", None)
        if authority is None:
            return None
        access = authority.for_actor(self._actor)
        def allowed(handle):
            if type(handle) is _FixedHarnessHandle:
                return access.allows(tool_permission(handle._handle_id))
            if type(handle) is ManagedToolHandle:
                entry = self._policy_service._plugin_catalog.require(handle.automation_id)
                return access.allows(plugin_permission(entry.plugin_id, management=entry.management, entrypoint="harness"))
            return False
        return allowed

    @staticmethod
    def _normalize_fixed_handlers(
        handlers: Mapping[str, ReadOnlyFixedHandler] | None,
    ) -> dict[str, ReadOnlyFixedHandler]:
        if handlers is None:
            return {}
        if not isinstance(handlers, Mapping):
            raise _error("Fixed Harness handlers must be a mapping", "HARNESS_FIXED_HANDLER_INVALID")
        unknown = set(handlers) - set(FIXED_HARNESS_TOOL_IDS)
        if unknown or any(not isinstance(key, str) for key in handlers):
            raise _error("Fixed Harness handler mapping is not closed", "HARNESS_FIXED_HANDLER_INVALID")
        if any(not callable(handler) for handler in handlers.values()):
            raise _error("Fixed Harness handler is not callable", "HARNESS_FIXED_HANDLER_INVALID")
        return dict(handlers)

    @property
    def actor(self) -> Actor:
        return self._actor

    @property
    def base_request_id(self) -> str:
        return self._base_request_id

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_index

    def request_id_for_call(self, call_index: int) -> str:
        if type(call_index) is not int or call_index < 0:
            raise _error("Harness call index is invalid", "HARNESS_REQUEST_INVALID")
        return str(uuid.uuid5(_INVOCATION_NAMESPACE, f"{self._base_request_id}:{call_index}"))

    def invoke(self, *, handle: object, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(arguments, Mapping):
            raise _error("Harness invocation arguments must be an object", "HARNESS_ARGUMENT_INVALID")
        if type(handle) is ManagedToolHandle:
            if dict(arguments) != {}:
                raise _error("Dynamic Harness invocation arguments must be empty", "HARNESS_ARGUMENT_INVALID")
            self._validate_dynamic_handle(handle)
            with self._lock:
                call_index = self._call_index
                self._call_index += 1
            return self._invoke_dynamic(handle, self.request_id_for_call(call_index))
        if type(handle) is _FixedHarnessHandle:
            return self._invoke_fixed(handle, dict(arguments))
        raise _error("Harness handle is unavailable", "HARNESS_TOOL_NOT_FOUND")

    def _invoke_dynamic(self, handle: ManagedToolHandle, request_id: str) -> Mapping[str, Any]:
        self._validate_dynamic_handle(handle)
        try:
            result = self._policy_service.invoke_harness(
                handle.automation_id,
                request_id=request_id,
                actor=self._actor,
                expected_automation_generation=handle.generation,
                contribution_id=handle.contribution_id,
            )
            if not isinstance(result, Mapping) or not result.get("invocation_id"):
                raise _error("插件没有返回本次执行标识", "HARNESS_GATEWAY_FAILED")
            _reject_forbidden_receipt_keys({key: value for key, value in result.items()
                                           if key not in {"automation_id", "plugin_id", "operation", "result", "output"}})
            completed = self._policy_service.direct_invocations.wait_sync(result["invocation_id"])
        except HarnessError:
            raise
        except Exception as exc:
            raise _error("Trusted Harness invocation failed", "HARNESS_GATEWAY_FAILED") from exc
        return _strict_receipt({
            "status": completed.get("status"),
            "result": _public_plugin_result(completed.get("result")),
            "error": completed.get("error_summary"),
        })

    def _validate_dynamic_handle(self, handle: ManagedToolHandle) -> None:
        if (
            not isinstance(handle.automation_id, str)
            or not handle.automation_id
            or handle.automation_id != handle.automation_id.strip()
            or type(handle.generation) is not int
            or handle.generation < 1
            or not isinstance(handle.contribution_id, str)
            or not handle.contribution_id
            or handle.contribution_id != handle.contribution_id.strip()
        ):
            raise _error("Harness dynamic handle is invalid", "HARNESS_TOOL_INVALID")
        if self._allowed_dynamic_handles is not None and handle not in self._allowed_dynamic_handles:
            raise _error("Harness dynamic handle is unavailable", "HARNESS_TOOL_NOT_FOUND")

    def _invoke_fixed(
        self,
        handle: _FixedHarnessHandle,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        authority = getattr(self._policy_service, "identity_access", None)
        if authority is not None and not authority.allows(self._actor, tool_permission(handle._handle_id)):
            raise _error("当前身份没有该查询权限", "HARNESS_PERMISSION_DENIED")
        safe_arguments = _validate_fixed_arguments(handle._handle_id, arguments)
        handler = self._fixed_handlers.get(handle._handle_id)
        if handler is None:
            raise _error("Fixed Harness handler is unavailable", "HARNESS_GATEWAY_UNAVAILABLE")
        try:
            result = handler(safe_arguments)
        except HarnessError:
            raise
        except Exception as exc:
            raise _error("Fixed Harness handler failed", "HARNESS_GATEWAY_FAILED") from exc
        return _strict_receipt(result)


def _public_plugin_result(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _public_plugin_result(nested) for key, nested in value.items()
                if str(key).lower() not in _FORBIDDEN_RECEIPT_KEYS}
    if isinstance(value, (list, tuple)):
        return [_public_plugin_result(item) for item in value]
    return value


def _strict_receipt(result: object) -> dict[str, Any]:
    if hasattr(result, "to_dict") and callable(result.to_dict):
        try:
            result = result.to_dict()
        except Exception as exc:
            raise _error("Harness invocation result is invalid", "HARNESS_GATEWAY_FAILED") from exc
    elif isinstance(result, Mapping):
        result = dict(result)
    else:
        raise _error("Harness invocation result must be an object", "HARNESS_GATEWAY_FAILED")
    normalized = strict_json(result, field_name="Harness invocation result")
    if not isinstance(normalized, dict):
        raise _error("Harness invocation result must be an object", "HARNESS_GATEWAY_FAILED")
    _reject_forbidden_receipt_keys(normalized)
    return normalized


def _reject_forbidden_receipt_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower() in _FORBIDDEN_RECEIPT_KEYS:
                raise _error(
                    "Harness invocation result contains a forbidden identity",
                    "HARNESS_PROTOCOL_INVALID",
                )
            _reject_forbidden_receipt_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_receipt_keys(nested)


def _validate_fixed_arguments(
    handle_id: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Recheck the fixed contract for callers that bypass the catalog."""

    expected = _FIXED_ARGUMENT_KEYS.get(handle_id)
    if expected is None or set(arguments) != expected:
        raise _error("Fixed Harness arguments are invalid", "HARNESS_ARGUMENT_INVALID")
    safe = strict_json(dict(arguments), field_name="fixed Harness arguments")
    if not isinstance(safe, dict):
        raise _error("Fixed Harness arguments are invalid", "HARNESS_ARGUMENT_INVALID")
    if handle_id == "knowledge.search":
        if (
            not isinstance(safe.get("query"), str)
            or len(safe["query"]) > 500
            or isinstance(safe.get("limit"), bool)
            or not isinstance(safe.get("limit"), int)
            or not 1 <= safe["limit"] <= 20
        ):
            raise _error("Fixed Harness arguments are invalid", "HARNESS_ARGUMENT_INVALID")
    elif handle_id == "finance.summary":
        from datetime import date
        try:
            start = date.fromisoformat(safe["start_date"])
            end = date.fromisoformat(safe["end_date"])
        except (TypeError, ValueError) as exc:
            raise _error("日期须为 YYYY-MM-DD", "HARNESS_ARGUMENT_INVALID") from exc
        if start > end or (end - start).days >= 366:
            raise _error("日期范围无效", "HARNESS_ARGUMENT_INVALID")
    elif handle_id == "work_items.list_open":
        if (
            isinstance(safe.get("limit"), bool)
            or not isinstance(safe.get("limit"), int)
            or not 1 <= safe["limit"] <= 50
        ):
            raise _error("Fixed Harness arguments are invalid", "HARNESS_ARGUMENT_INVALID")
    else:
        field = next(iter(expected))
        if not isinstance(safe.get(field), str) or not 1 <= len(safe[field]) <= 191:
            raise _error("Fixed Harness arguments are invalid", "HARNESS_ARGUMENT_INVALID")
    return safe


__all__ = [
    "FIXED_HARNESS_TOOL_IDS",
    "HarnessConversationService",
    "HarnessMessageReceipt",
    "HarnessSessionReceipt",
    "HarnessSidecar",
    "HarnessSidecarFactory",
    "MEMORY_ONLY",
    "ProductionGatedHarnessSidecar",
    "ProductionGatedHarnessSidecarFactory",
    "TrustedHarnessInvocationAdapter",
    "bind_signed_console_admin",
    "build_fixed_harness_tools",
    "production_gated_sidecar_factory",
]
