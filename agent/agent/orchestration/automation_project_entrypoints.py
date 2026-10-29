"""Trusted Scheduler, Feishu, Webhook, and Event adapters for projects.

Transport handlers verify their native signature/session first, resolve one
exact committed instance route, and pass closed transport facts and explicitly
declared occurrence inputs. Callers cannot replace project identity or settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo

from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.scan_preview_binding import normalize_preview_invocation_id
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from shared.automation_project_authorization import (
    OMIT_DYNAMIC_ARGUMENT,
    AutomationEntrypoint,
)


_ACCOUNT_FIELDS = frozenset({"account_id", "account_ids"})
_RESOURCE_FIELDS = frozenset({"resource_id", "resource_ids", "resource_binding", "resource_bindings"})


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _managed_target_identity(target: Any) -> tuple[str, int, str]:
    try:
        automation_id = (
            target.get("automation_id")
            if isinstance(target, Mapping)
            else getattr(target, "automation_id")
        )
        generation = (
            target.get("generation")
            if isinstance(target, Mapping)
            else getattr(target, "generation")
        )
        contribution_id = (
            target.get("contribution_id")
            if isinstance(target, Mapping)
            else getattr(target, "contribution_id")
        )
    except Exception as exc:
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation project runtime projection identity is invalid",
        ) from exc
    if (
        not _is_exact_managed_identifier(automation_id)
        or type(generation) is not int
        or generation <= 0
        or not _is_exact_managed_identifier(contribution_id)
    ):
        raise OrchestrationError(
            "PROJECT_RUNTIME_PROJECTION_STALE",
            "Automation project runtime projection identity is invalid",
        )
    return automation_id, generation, contribution_id


def _safe_managed_dispatch_result(result: Any) -> dict[str, Any]:
    if (
        not isinstance(result, Mapping)
        or type(result.get("success")) is not bool
        or not isinstance(result.get("status"), str)
        or not result["status"]
    ):
        raise OrchestrationError(
            "PROJECT_INVOKE_UNAVAILABLE",
            "Automation project result is unavailable",
        )
    identity = normalize_preview_invocation_id(result.get("invocation_id"))
    return {
        "invocation_id": identity,
        **{key: result.get(key) for key in ("success", "status", "result", "output", "error", "error_code", "running", "status_url")},
        "error_summary": result.get("error_summary") if not result["success"] else None,
    }


class ServiceV2FeishuDispatcher:
    """Resolve one active Service V2 command through the managed registry.

    This adapter is intentionally separate from the host-parsed fixed command
    routes.  The verified Feishu transport supplies only its stable event
    identities, command text and declared runtime inputs; project and contribution
    identity come only from the active managed-contribution projection.
    """

    def __init__(
        self,
        *,
        policy_service: AutomationProjectPolicyService,
        contribution_registry: Any,
        resolve_actor: Any,
    ) -> None:
        resolve_command = getattr(
            contribution_registry,
            "resolve_active_feishu_command",
            None,
        )
        if not callable(resolve_command):
            raise TypeError(
                "contribution_registry must resolve active Feishu commands"
            )
        if not callable(resolve_actor):
            raise TypeError("resolve_actor must be callable")
        self._policy = policy_service
        self._resolve_command = resolve_command
        self._resolve_actor = resolve_actor

    async def dispatch(
        self,
        *,
        command_text: str,
        event_id: str,
        sender_id: str,
        chat_id: str,
        on_accepted: Callable[[Any], Awaitable[None]] | None = None,
        conversation_target: Any = None,
        preview_invocation_id: str | None = None,
        selected_bill_codes: list[str] | None = None,
        runtime_inputs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Dispatch an exact active command, or return ``None`` if unknown."""

        try:
            target = self._resolve_command(command_text)
        except PluginConflictError as exc:
            if exc.code == "CAPABILITY_UNAVAILABLE":
                return None
            raise OrchestrationError(
                "PROJECT_RUNTIME_PROJECTION_STALE",
                "Automation project runtime projection is unavailable",
            ) from exc
        if target is None:
            return None

        if conversation_target is not None and (target.automation_id != conversation_target.automation_id or target.generation != conversation_target.generation or target.contribution_id != conversation_target.contribution_id):
            raise OrchestrationError("PROJECT_INVOCATION_STALE", "对话中选择的插件已变化，请重新触发")

        dynamic_context: dict[str, Any] = {}
        if runtime_inputs is not None:
            if not isinstance(runtime_inputs, Mapping):
                raise OrchestrationError("PROJECT_DYNAMIC_INPUT_INVALID", "本次调用参数必须为对象")
            entry, contract = self._policy._load_contract(target.automation_id)
            invocation = contract.invocation_contracts.get(target.contribution_id)
            if invocation is None or contract.automation_generation != target.generation:
                raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "插件入口已变化，请重新触发")
            if set(runtime_inputs) - set(invocation.dynamic_argument_resolvers):
                raise OrchestrationError("PROJECT_ARGUMENT_OVERRIDE_FORBIDDEN", "本次参数未在插件入口声明")
            declarations = [item for item in entry.contributions.get("feishu", ())
                            if item.get("id") == target.contribution_id]
            if len(declarations) != 1 or "input_schema" not in declarations[0]:
                raise OrchestrationError("PROJECT_DYNAMIC_INPUT_INVALID", "插件入口不接受本次输入")
            from agent.tool_registry import validate_schema_instance
            try:
                validate_schema_instance("feishu.runtime_inputs", dict(runtime_inputs), declarations[0]["input_schema"])
            except (ValueError, TypeError, KeyError) as exc:
                raise OrchestrationError("PROJECT_DYNAMIC_INPUT_INVALID", "本次调用参数缺失或格式无效") from exc
            dynamic_context["dynamic_inputs"] = dict(runtime_inputs)

        safe_event_id = _stable_identifier(event_id, "event_id")
        safe_sender_id = _stable_identifier(sender_id, "sender_id")
        safe_chat_id = _stable_identifier(chat_id, "chat_id")
        try:
            actor = self._resolve_actor(safe_sender_id)
        except OrchestrationError:
            raise
        except Exception as exc:
            raise OrchestrationError(
                "TRUSTED_ENTRYPOINT_REQUIRED",
                "Feishu actor identity could not be verified",
            ) from exc
        if not isinstance(actor, Actor) or actor.actor_id != safe_sender_id:
            raise OrchestrationError(
                "TRUSTED_ENTRYPOINT_REQUIRED",
                "Feishu actor identity does not match the verified sender",
            )

        invoke_kwargs: dict[str, Any] = {
            "entrypoint": AutomationEntrypoint.FEISHU,
            "request_id": safe_event_id,
            "actor": actor,
            "trusted_context": {
                "event_id": safe_event_id,
                "chat_id": safe_chat_id,
                **dynamic_context,
            },
            "idempotency_key": f"feishu:{safe_event_id}",
            "expected_automation_generation": getattr(target, "generation", None),
            "contribution_id": getattr(target, "contribution_id", None),
        }
        if on_accepted is not None:
            invoke_kwargs["on_accepted"] = on_accepted
        if preview_invocation_id is not None:
            invoke_kwargs["preview_invocation_id"] = normalize_preview_invocation_id(preview_invocation_id)
        if selected_bill_codes is not None:
            invoke_kwargs["selected_bill_codes"] = selected_bill_codes
        if conversation_target is not None:
            invoke_kwargs.update(require_full_auto=True, expected_project_configuration_version=conversation_target.configuration_version)
        return await self._policy.invoke_trusted_and_wait(
            getattr(target, "automation_id", None),
            **invoke_kwargs,
        )


class ServiceV2WebhookDispatcher:
    """Dispatch an exact managed route with only its declared occurrence fields."""

    def __init__(
        self,
        *,
        policy_service: AutomationProjectPolicyService,
        contribution_registry: Any,
    ) -> None:
        resolve_route = getattr(
            contribution_registry,
            "resolve_active_webhook_route",
            None,
        )
        if not callable(resolve_route):
            raise TypeError(
                "contribution_registry must resolve active Webhook routes"
            )
        self._policy = policy_service
        self._resolve_route = resolve_route

    async def dispatch(
        self,
        *,
        method: str,
        route: str,
        source_event_id: str,
        envelope: Mapping[str, Any] | None = None,
        preview_invocation_id: str | None = None,
        expected_target: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Dispatch an exact active route, or return ``None`` if unowned."""

        try:
            target = self._resolve_route(method=method, route=route)
        except Exception as exc:
            raise OrchestrationError(
                "PROJECT_RUNTIME_PROJECTION_STALE",
                "Automation project runtime projection is unavailable",
            ) from exc
        if target is None:
            return None

        automation_id, generation, contribution_id = _managed_target_identity(target)
        if expected_target is not None and (
                generation != expected_target["generation"] or automation_id != expected_target["automation_id"]
                or contribution_id != expected_target["contribution_id"]):
            raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "回调插件入口刚刚发生变化，请重试")
        dynamic_context: dict[str, Any] = {}
        preview_kwargs: dict[str, Any] = {}
        if envelope is not None or preview_invocation_id is not None:
            from types import SimpleNamespace
            entry, contract = self._policy._load_contract(automation_id)
            invocation = contract.invocation_contracts.get(contribution_id)
            if invocation is None or contract.automation_generation != generation:
                raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "回调插件的设置已变化")
            dynamic = _extract_dynamic_inputs(SimpleNamespace(
                action_fields=frozenset(entry.config_schema["properties"]),
                dynamic_fields=frozenset(invocation.dynamic_argument_resolvers),
            ), envelope or {})
            if dynamic:
                dynamic_context["dynamic_inputs"] = dynamic
            if preview_invocation_id is not None:
                from agent.orchestration.scan_preview_binding import is_scan_preview_project
                if not is_scan_preview_project(entry):
                    raise OrchestrationError("SCAN_PREVIEW_ID_INVALID", "此回调不接受扫描预览")
                normalized = normalize_preview_invocation_id(preview_invocation_id)
                if normalized != preview_invocation_id:
                    raise OrchestrationError("SCAN_PREVIEW_ID_INVALID", "扫描预览标识格式无效")
                preview_kwargs["preview_invocation_id"] = normalized

        safe_event_id = _exact_stable_identifier(
            source_event_id,
            "source_event_id",
        )
        owner = {
            "automation_id": automation_id,
            "contribution_id": contribution_id,
            "method": method,
            "route": route,
        }
        owner_digest = _canonical_digest(owner)
        event_digest = _canonical_digest(
            {"owner": owner, "source_event_id": safe_event_id}
        )
        actor = Actor(
            ActorType.WEBHOOK,
            f"webhook:{owner_digest}",
            authenticated_by="signed_webhook_route",
        )
        result = await self._policy.invoke_trusted_and_wait(
            automation_id,
            entrypoint=AutomationEntrypoint.WEBHOOK,
            request_id=safe_event_id,
            actor=actor,
            trusted_context={
                "route_id": owner_digest,
                "route_revision": generation,
                "source_event_id": safe_event_id,
                "webhook_path": f"webhook/{route}",
                "webhook_method": method,
                **dynamic_context,
            },
            idempotency_key=f"webhook:v2:{event_digest}",
            expected_automation_generation=generation,
            contribution_id=contribution_id,
            **preview_kwargs,
        )
        return _safe_managed_dispatch_result(result)


class ServiceV2EventDispatcher:
    """Dispatch one exact non-durable managed Event without a live event bus."""

    def __init__(
        self,
        *,
        policy_service: AutomationProjectPolicyService,
        contribution_registry: Any,
    ) -> None:
        resolve_event = getattr(contribution_registry, "resolve_active_event", None)
        if not callable(resolve_event):
            raise TypeError("contribution_registry must resolve active Events")
        self._policy = policy_service
        self._resolve_event = resolve_event

    async def dispatch(
        self,
        *,
        event_name: str,
        source_event_id: str,
    ) -> dict[str, Any] | None:
        """Dispatch an exact active Event, or return ``None`` if unowned."""

        try:
            target = self._resolve_event(event_name=event_name)
        except Exception as exc:
            raise OrchestrationError(
                "PROJECT_RUNTIME_PROJECTION_STALE",
                "Automation project runtime projection is unavailable",
            ) from exc
        if target is None:
            return None

        automation_id, generation, contribution_id = _managed_target_identity(target)
        safe_event_name = _exact_stable_identifier(event_name, "event_name")
        safe_source_event_id = _exact_stable_identifier(
            source_event_id,
            "source_event_id",
        )
        owner = {
            "automation_id": automation_id,
            "contribution_id": contribution_id,
            "event_name": safe_event_name,
        }
        owner_digest = _canonical_digest(owner)
        event_digest = _canonical_digest(
            {"owner": owner, "source_event_id": safe_source_event_id}
        )
        result = await self._policy.invoke_trusted_and_wait(
            automation_id,
            entrypoint=AutomationEntrypoint.EVENTS,
            request_id=safe_source_event_id,
            actor=Actor(
                ActorType.EVENT,
                f"event:{owner_digest}",
                authenticated_by="managed_event_dispatcher",
            ),
            trusted_context={
                "event_name": safe_event_name,
                "source_event_id": safe_source_event_id,
            },
            idempotency_key=f"event:v2:{event_digest}",
            expected_automation_generation=generation,
            contribution_id=contribution_id,
        )
        return _safe_managed_dispatch_result(result)


class AutomationProjectEntrypoints:
    """Narrow entry adapters around the project policy authority."""

    def __init__(
        self,
        policy_service: AutomationProjectPolicyService,
        *,
        feishu_actor_resolver: Any | None = None,
        fixed_entrypoints: Any | None = None,
        service_v2_webhook_dispatcher: ServiceV2WebhookDispatcher | None = None,
    ) -> None:
        self._policy = policy_service
        self._feishu_actor_resolver = feishu_actor_resolver
        self._fixed_entrypoints = fixed_entrypoints
        self._service_v2_webhooks = service_v2_webhook_dispatcher

    async def wait_feishu_invocation(self, *, invocation_id: str, automation_id: str,
                                    event_id: str, sender_id: str, chat_id: str,
                                    timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Read only the call accepted for this verified event and sender."""
        event = _stable_identifier(event_id, "event_id")
        _stable_identifier(chat_id, "chat_id")
        sender = _stable_identifier(sender_id, "sender_id")
        try:
            valid_id = str(UUID(invocation_id)) == invocation_id
        except (ValueError, TypeError, AttributeError):
            valid_id = False
        if not valid_id or not _is_exact_managed_identifier(automation_id):
            raise OrchestrationError("INVOCATION_IDENTITY_INVALID", "执行记录标识无效")
        if self._feishu_actor_resolver is None:
            raise OrchestrationError("ACTOR_NOT_AUTHORIZED", "飞书身份未绑定")
        actor = await asyncio.to_thread(self._feishu_actor_resolver, sender)
        if not isinstance(actor, Actor) or actor.actor_id != sender:
            raise OrchestrationError("ACTOR_NOT_AUTHORIZED", "飞书身份与消息发送者不匹配")
        self._policy._require_trusted_entrypoint_actor(AutomationEntrypoint.FEISHU, actor)
        row = await asyncio.to_thread(self._policy.direct_invocations.repository.get, invocation_id)
        if row is None:
            raise OrchestrationError("INVOCATION_NOT_FOUND", "本次执行记录不可读取")
        if (row.get("source") != "feishu" or row.get("actor_id") != actor.actor_id
                or row.get("request_id") != event or row.get("automation_id") != automation_id):
            raise OrchestrationError("ACTOR_NOT_AUTHORIZED", "只能读取本次消息发起的执行")
        result = await self._policy.direct_invocations.wait(invocation_id, timeout_seconds=timeout_seconds)
        from agent.identity_access import require_project_access
        await asyncio.to_thread(require_project_access, self._policy, actor, automation_id, entrypoint="feishu")
        if result.get("invocation_id") != invocation_id or result.get("automation_id") != automation_id:
            raise OrchestrationError("INVOCATION_IDENTITY_INVALID", "执行结果与本次调用不匹配")
        return await asyncio.to_thread(
            self._policy.project_completed_invocation_result, automation_id, result,
            is_formal=bool(row.get("preview_invocation_id")),
        )

    async def cancel_feishu_invocation(self, *, invocation_id: str, event_id: str, sender_id: str, chat_id: str) -> dict[str, Any]:
        _stable_identifier(event_id, "event_id")
        _stable_identifier(chat_id, "chat_id")
        sender = _stable_identifier(sender_id, "sender_id")
        if self._feishu_actor_resolver is None:
            raise OrchestrationError("ACTOR_NOT_AUTHORIZED", "飞书身份未绑定")
        actor = self._feishu_actor_resolver(sender)
        self._policy._require_trusted_entrypoint_actor(AutomationEntrypoint.FEISHU, actor)
        row = self._policy.direct_invocations.repository.get(invocation_id)
        if row is None or row.get("source") != "feishu" or row.get("actor_id") != actor.actor_id:
            raise OrchestrationError("ACTOR_NOT_AUTHORIZED", "只能取消自己发起的执行")
        from agent.identity_access import require_project_access
        await asyncio.to_thread(require_project_access, self._policy, actor, row["automation_id"], entrypoint="feishu")
        return await self._policy.direct_invocations.cancel(invocation_id)

    async def invoke_webhook(
        self,
        *,
        route_key: str,
        source_event_id: str,
        webhook_path: str,
        envelope: Mapping[str, Any] | None = None,
        preview_invocation_id: Any | None = None,
    ) -> dict[str, Any]:
        """Dispatch an existing public callback path to its fixed plugin's enabled instance."""

        safe_path = str(webhook_path or "").strip("/")
        if self._fixed_entrypoints is None or self._service_v2_webhooks is None:
            raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "当前回调插件入口尚未就绪")
        try:
            target = self._fixed_entrypoints.webhook_target(route_key)
        except PluginConflictError as exc:
            raise OrchestrationError("PROJECT_RUNTIME_PROJECTION_STALE", "当前回调插件入口不可用") from exc
        if target is None or safe_path != str(route_key or "").strip("/"):
            raise OrchestrationError("PROJECT_ROUTE_NOT_FOUND", "Automation project route is unavailable")
        if target.get("automation_id") is None:
            raise OrchestrationError("PROJECT_ENTRYPOINT_DISABLED", "当前回调插件入口未启用")
        result = await self._service_v2_webhooks.dispatch(
            method="POST", route=target["route"], source_event_id=source_event_id,
            envelope=envelope or {}, preview_invocation_id=preview_invocation_id, expected_target=target,
        )
        if result is None:
            raise OrchestrationError("PROJECT_ENTRYPOINT_DISABLED", "当前回调插件入口未启用")
        return result


class TrustedDynamicArgumentResolver:
    """Resolve only explicitly signed, code-owned occurrence fields."""

    def __call__(
        self,
        resolver_id: str,
        field: str,
        context: Mapping[str, Any],
    ) -> Any:
        if resolver_id == "scheduled_previous_day" and field == "target_date":
            if context.get("entrypoint") != AutomationEntrypoint.SCHEDULER.value:
                raise ValueError("scheduled resolver used outside scheduler")
            scheduled_for = datetime.fromisoformat(str(context.get("scheduled_for") or ""))
            if scheduled_for.tzinfo is None:
                raise ValueError("scheduled occurrence has no timezone")
            return (scheduled_for.date() - timedelta(days=1)).isoformat()
        if resolver_id == "current_business_day" and field == "target_date":
            entrypoint = str(context.get("entrypoint") or "")
            if entrypoint == AutomationEntrypoint.SCHEDULER.value:
                timestamp = context.get("scheduled_for")
            elif entrypoint == AutomationEntrypoint.CONSOLE.value:
                timestamp = context.get("occurred_at")
            else:
                raise ValueError("business-day resolver used outside scheduler/console")
            instant = datetime.fromisoformat(str(timestamp or "").replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError("business-day occurrence has no timezone")
            return instant.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        resolver_field = re.sub(r"[^a-z0-9_.-]", "_", field.lower())
        expected_resolver = f"verified_{context.get('entrypoint')}_{resolver_field}"
        expected_optional_resolver = (
            f"verified_optional_{context.get('entrypoint')}_{resolver_field}"
        )
        dynamic_inputs = context.get("dynamic_inputs")
        if resolver_id not in {expected_resolver, expected_optional_resolver} or not isinstance(
            dynamic_inputs,
            Mapping,
        ):
            raise ValueError("dynamic resolver is not code-owned for this entrypoint")
        if field not in dynamic_inputs:
            if resolver_id == expected_optional_resolver:
                return OMIT_DYNAMIC_ARGUMENT
            raise ValueError("verified dynamic field is missing")
        return dynamic_inputs[field]


def _extract_dynamic_inputs(
    route: Any,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    if set(envelope) - {"body", "query"}:
        raise OrchestrationError(
            "PROJECT_TRANSPORT_INVALID",
            "Verified transport envelope contains unsupported sections",
        )
    body = envelope.get("body", {})
    query = envelope.get("query", {})
    if not isinstance(body, Mapping) or not isinstance(query, Mapping):
        raise OrchestrationError(
            "PROJECT_TRANSPORT_INVALID",
            "Verified transport envelope must contain JSON objects",
        )
    supplied_fields = set(body) | set(query)
    account_fields = {
        field
        for field in supplied_fields
        if str(field) in _ACCOUNT_FIELDS
        or str(field).endswith(("_account_id", "_account_ids"))
    }
    if account_fields:
        raise OrchestrationError(
            "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN",
            "Transport callers cannot override project account bindings",
        )
    resource_fields = {
        field
        for field in supplied_fields
        if str(field) in _RESOURCE_FIELDS
        or str(field).endswith(
            (
                "_resource_id",
                "_resource_ids",
                "_resource_binding",
                "_resource_bindings",
            )
        )
    }
    if resource_fields:
        raise OrchestrationError(
            "PROJECT_RESOURCE_OVERRIDE_FORBIDDEN",
            "Transport callers cannot override project resource bindings",
        )
    static_overrides = (
        supplied_fields & route.action_fields
    ) - route.dynamic_fields
    if static_overrides:
        raise OrchestrationError(
            "PROJECT_ARGUMENT_OVERRIDE_FORBIDDEN",
            "Transport callers cannot override saved project configuration",
        )
    result: dict[str, Any] = {}
    for field in sorted(route.dynamic_fields):
        body_has = field in body
        query_has = field in query
        if body_has and query_has and body[field] != query[field]:
            raise OrchestrationError(
                "PROJECT_DYNAMIC_INPUT_CONFLICT",
                "Verified transport contains conflicting dynamic input",
            )
        if body_has:
            result[field] = body[field]
        elif query_has:
            result[field] = query[field]
    return result


def _stable_identifier(value: Any, field_name: str) -> str:
    identifier = str(value or "").strip()
    if not identifier or len(identifier) > 191:
        raise OrchestrationError(
            "STABLE_EVENT_ID_REQUIRED",
            f"A stable verified {field_name} is required",
        )
    return identifier


def _is_exact_managed_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 191
    )


def _exact_stable_identifier(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 191
    ):
        raise OrchestrationError(
            "STABLE_EVENT_ID_REQUIRED",
            f"A stable verified {field_name} is required",
        )
    return value
