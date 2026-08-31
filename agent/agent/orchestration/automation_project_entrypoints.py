"""Trusted Scheduler, Feishu, and Webhook adapters for automation projects.

Transport handlers verify their native signature/session first, resolve one
exact committed instance route, and pass only closed transport facts here.
Neither a caller nor an LLM can submit action arguments or project identity.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.scan_preview_binding import normalize_preview_run_id
from agent.orchestration.selection_preview_binding import (
    SELECTION_PREVIEW_PROJECTS,
)
from agent.automation_plugins.catalog import (
    PluginCatalog,
    project_capability_from_snapshot,
)
from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    RuntimeCoeffectKind,
    RuntimeGenerationState,
)
from agent.automation_plugins.ports import RuntimeGenerationRepositoryPort
from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from shared.automation_project_authorization import (
    OMIT_DYNAMIC_ARGUMENT,
    AutomationEntrypoint,
)


_ACCOUNT_FIELDS = frozenset({"account_id", "account_ids"})
_RESOURCE_FIELDS = frozenset({"resource_id", "resource_ids", "resource_binding", "resource_bindings"})
_SCAN_FEISHU_ROUTE_KEY = "builtin.scan_codes"
_SCAN_WEBHOOK_ROUTE_KEY = "webhook/phase7/scan"
_SCAN_AUTOMATION_ID = "scan_codes"


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class AutomationProjectEntrypointRoute:
    route_id: str
    route_key: str
    entrypoint: AutomationEntrypoint
    automation_id: str
    automation_generation: int
    project_configuration_version: int
    route_revision: int
    action_fields: frozenset[str]
    dynamic_fields: frozenset[str]
    project_config: Mapping[str, Any] = dataclass_field(default_factory=dict)
    account_bindings: Mapping[str, str | tuple[str, ...]] = dataclass_field(
        default_factory=dict
    )
    enabled: bool = True

    def __post_init__(self) -> None:
        for field_name in ("route_id", "route_key", "automation_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value or len(value) > 191:
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Persisted automation project route is invalid",
                )
            object.__setattr__(self, field_name, value)
        for field_name in (
            "automation_generation",
            "project_configuration_version",
            "route_revision",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Persisted automation project route version is invalid",
                )
        if not self.dynamic_fields <= self.action_fields:
            raise OrchestrationError(
                "PROJECT_ROUTE_INVALID",
                "Persisted automation route dynamic fields exceed its signed schema",
            )
        if not isinstance(self.project_config, Mapping) or not isinstance(
            self.account_bindings,
            Mapping,
        ):
            raise OrchestrationError(
                "PROJECT_ROUTE_INVALID",
                "Persisted automation project bindings are invalid",
            )
        normalized_bindings: dict[str, str | tuple[str, ...]] = {}
        for raw_role, raw_binding in self.account_bindings.items():
            role = str(raw_role or "").strip()
            if not role:
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Persisted automation account role is invalid",
                )
            if isinstance(raw_binding, str):
                binding: str | tuple[str, ...] = raw_binding.strip()
                if not binding:
                    raise OrchestrationError(
                        "PROJECT_ROUTE_INVALID",
                        "Persisted automation account binding is invalid",
                    )
            elif isinstance(raw_binding, (list, tuple)):
                values = tuple(str(item or "").strip() for item in raw_binding)
                if not values or not all(values) or len(values) != len(set(values)):
                    raise OrchestrationError(
                        "PROJECT_ROUTE_INVALID",
                        "Persisted automation account collection is invalid",
                    )
                binding = values
            else:
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Persisted automation account binding is invalid",
                )
            normalized_bindings[role] = binding
        object.__setattr__(self, "project_config", dict(self.project_config))
        object.__setattr__(self, "account_bindings", normalized_bindings)


class AutomationProjectRouteResolverPort(Protocol):
    def resolve_committed_route(
        self,
        *,
        entrypoint: AutomationEntrypoint,
        route_key: str,
    ) -> AutomationProjectEntrypointRoute | None:
        """Return one exact committed route; never guess by tool or plugin id."""


class CommittedAutomationProjectRouteResolver:
    """Resolve a transport key only through an immutable committed generation.

    Route ownership is a core-managed resource binding.  The current resource
    revision must equal the RESOURCE coeffect recorded for the committed
    generation; a path edit, resource replacement, or stale generation fails
    closed instead of silently retargeting the Webhook/Feishu event.
    """

    def __init__(
        self,
        *,
        catalog: PluginCatalog,
        runtime_repository: RuntimeGenerationRepositoryPort,
        binding_resolver: ProductionProjectBindingResolver,
        resource_provider: Any,
    ) -> None:
        if not callable(resource_provider):
            raise TypeError("resource_provider must be callable")
        self._catalog = catalog
        self._runtime = runtime_repository
        self._bindings = binding_resolver
        self._resource_provider = resource_provider

    def resolve_committed_route(
        self,
        *,
        entrypoint: AutomationEntrypoint,
        route_key: str,
    ) -> AutomationProjectEntrypointRoute | None:
        safe_key = _normalize_route_key(entrypoint, route_key)
        role_name = f"{entrypoint.value}_route"
        expected_kind = role_name
        matches: list[AutomationProjectEntrypointRoute] = []
        for entry in self._catalog.list(include_disabled=False):
            snapshot = entry.committed_snapshot
            if (
                snapshot is None
                or entry.committed_generation != snapshot.generation
                or entry.target_generation != entry.committed_generation
                or str(getattr(entry.reconcile_state, "value", entry.reconcile_state))
                != "STABLE"
            ):
                continue
            entrypoint_enabled = entrypoint.value in snapshot.enabled_entrypoints
            metadata = snapshot.execution_metadata
            project_config = metadata.get("project_config")
            account_bindings = metadata.get("account_bindings")
            resource_bindings = metadata.get("resource_bindings")
            descriptor = metadata.get("runtime_descriptor")
            compiled_invocations = metadata.get("compiled_invocations")
            if (
                not isinstance(project_config, Mapping)
                or not isinstance(account_bindings, Mapping)
                or not isinstance(resource_bindings, Mapping)
                or not isinstance(descriptor, Mapping)
                or not isinstance(compiled_invocations, Mapping)
            ):
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Committed automation route metadata is invalid",
                )
            resource_id = resource_bindings.get(role_name)
            if resource_id is None:
                continue
            resource_roles = descriptor.get("resource_roles")
            if not isinstance(resource_roles, list):
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Committed automation route contract is invalid",
                )
            roles = [
                item
                for item in resource_roles
                if isinstance(item, Mapping)
                and str(item.get("role") or "") == role_name
            ]
            if len(roles) != 1:
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Committed automation route role is invalid",
                )
            try:
                resource_descriptor = self._bindings.describe_resource_binding(
                    automation_id=entry.automation_id,
                    role=roles[0],
                    resource_id=str(resource_id),
                )
                resource = self._resource_provider(str(resource_id))
            except Exception as exc:
                raise OrchestrationError(
                    "PROJECT_ROUTE_STALE",
                    "Committed automation route resource is unavailable",
                ) from exc
            if (
                not isinstance(resource, Mapping)
                or str(resource.get("resource_kind") or "") != expected_kind
            ):
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Committed automation route resource has the wrong kind",
                )
            candidate_key_field = (
                "path"
                if entrypoint is AutomationEntrypoint.WEBHOOK
                else "route_key"
            )
            candidate_key = _normalize_route_key(
                entrypoint,
                resource.get(candidate_key_field),
            )
            if candidate_key != safe_key:
                continue
            if not entrypoint_enabled:
                matches.append(
                    AutomationProjectEntrypointRoute(
                        route_id=str(resource_id),
                        route_key=candidate_key,
                        entrypoint=entrypoint,
                        automation_id=entry.automation_id,
                        automation_generation=snapshot.generation,
                        project_configuration_version=int(
                            metadata.get("project_config_version") or 0
                        ),
                        route_revision=int(
                            resource_descriptor.get("configuration_version") or 0
                        ),
                        action_fields=frozenset(),
                        dynamic_fields=frozenset(),
                        project_config=project_config,
                        account_bindings=account_bindings,
                        enabled=False,
                    )
                )
                continue
            generation = self._runtime.get_generation(
                entry.automation_id,
                snapshot.generation,
            )
            if generation is None or generation.state is not RuntimeGenerationState.COMMITTED:
                raise OrchestrationError(
                    "PROJECT_ROUTE_STALE",
                    "Committed automation route generation is unavailable",
                )
            observations = [
                item
                for item in generation.coeffects
                if item.kind is RuntimeCoeffectKind.RESOURCE
                and item.key == role_name
            ]
            if (
                len(observations) != 1
                or observations[0].ready is not True
                or observations[0].revision
                != _canonical_digest(resource_descriptor)
            ):
                raise OrchestrationError(
                    "PROJECT_ROUTE_STALE",
                    "Committed automation route revision changed",
                )
            project_capability_from_snapshot(snapshot)
            action_contract = metadata.get("action_contract")
            invocation = compiled_invocations.get(entrypoint.value)
            if not isinstance(action_contract, Mapping) or not isinstance(
                invocation,
                Mapping,
            ):
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Committed automation invocation contract is invalid",
                )
            schema = action_contract.get("input_schema")
            properties = schema.get("properties") if isinstance(schema, Mapping) else None
            dynamic_resolvers = invocation.get("dynamic_resolvers")
            if not isinstance(properties, Mapping) or not isinstance(
                dynamic_resolvers,
                Mapping,
            ):
                raise OrchestrationError(
                    "PROJECT_ROUTE_INVALID",
                    "Committed automation invocation schema is invalid",
                )
            matches.append(
                AutomationProjectEntrypointRoute(
                    route_id=str(resource_id),
                    route_key=candidate_key,
                    entrypoint=entrypoint,
                    automation_id=entry.automation_id,
                    automation_generation=snapshot.generation,
                    project_configuration_version=int(
                        metadata.get("project_config_version") or 0
                    ),
                    route_revision=int(
                        resource_descriptor.get("configuration_version") or 0
                    ),
                    action_fields=frozenset(str(field) for field in properties),
                    dynamic_fields=frozenset(
                        str(field) for field in dynamic_resolvers
                    ),
                    project_config=dict(project_config),
                    account_bindings=dict(account_bindings),
                )
            )
        if len(matches) > 1:
            raise OrchestrationError(
                "PROJECT_ROUTE_AMBIGUOUS",
                "Verified transport route is bound to multiple automation instances",
            )
        return matches[0] if matches else None


class ServiceV2FeishuDispatcher:
    """Resolve one active Service V2 command through the managed registry.

    This adapter is intentionally separate from the fixed Action V1 route
    resolver.  The verified Feishu transport supplies only its stable event
    identities and command text; project and contribution identity come only
    from the active managed-contribution projection.
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

        return await self._policy.invoke_trusted_and_wait(
            getattr(target, "automation_id", None),
            entrypoint=AutomationEntrypoint.FEISHU,
            request_id=safe_event_id,
            actor=actor,
            trusted_context={
                "event_id": safe_event_id,
                "chat_id": safe_chat_id,
            },
            idempotency_key=f"feishu:{safe_event_id}",
            expected_automation_generation=getattr(target, "generation", None),
            contribution_id=getattr(target, "contribution_id", None),
        )


class ServiceV2WebhookDispatcher:
    """Dispatch one exact managed Webhook route without transport payload input."""

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
        except (AttributeError, TypeError) as exc:
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
            },
            idempotency_key=f"webhook:v2:{event_digest}",
            expected_automation_generation=generation,
            contribution_id=contribution_id,
        )
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
        return {"success": result["success"], "status": result["status"]}


class AutomationProjectEntrypoints:
    """Narrow entry adapters around the project policy authority."""

    def __init__(
        self,
        policy_service: AutomationProjectPolicyService,
        *,
        route_resolver: AutomationProjectRouteResolverPort,
        feishu_actor_resolver: Any | None = None,
    ) -> None:
        self._policy = policy_service
        self._routes = route_resolver
        self._feishu_actor_resolver = feishu_actor_resolver

    async def invoke_feishu(
        self,
        *,
        route_key: str,
        event_id: str,
        sender_id: str,
        chat_id: str,
        envelope: Mapping[str, Any] | None = None,
        preview_run_id: str | None = None,
    ) -> dict[str, Any]:
        route = self._require_route(AutomationEntrypoint.FEISHU, route_key)
        safe_event_id = _stable_identifier(event_id, "event_id")
        safe_sender_id = _stable_identifier(sender_id, "sender_id")
        safe_chat_id = _stable_identifier(chat_id, "chat_id")
        dynamic_inputs = _extract_dynamic_inputs(route, envelope or {})
        selection_route = route.automation_id in SELECTION_PREVIEW_PROJECTS
        safe_preview_run_id = None
        if preview_run_id is not None:
            if selection_route:
                safe_preview_run_id = normalize_preview_run_id(preview_run_id)
                if set(dynamic_inputs) != {"selected_bill_codes"}:
                    raise OrchestrationError(
                        "SELECTION_INPUT_INVALID",
                        "Selection confirmation accepts only selected bill codes",
                        details={"status": "BLOCKED_DATA"},
                    )
            elif (
                route.route_key != _SCAN_FEISHU_ROUTE_KEY
                or route.automation_id != _SCAN_AUTOMATION_ID
            ):
                raise OrchestrationError(
                    "SCAN_PREVIEW_ID_INVALID",
                    "A scan preview cannot be used by this Feishu route",
                )
            else:
                safe_preview_run_id = normalize_preview_run_id(preview_run_id)
        elif selection_route and dynamic_inputs:
            raise OrchestrationError(
                "SELECTION_INPUT_INVALID",
                "Selection preview inputs are supplied only by the server",
                details={"status": "BLOCKED_DATA"},
            )
        actor = (
            self._feishu_actor_resolver(safe_sender_id)
            if self._feishu_actor_resolver is not None
            else Actor(
                ActorType.FEISHU_USER,
                safe_sender_id,
                authenticated_by="feishu_verified_event",
            )
        )
        trusted_context = {
            "route_id": route.route_id,
            "route_revision": route.route_revision,
            "event_id": safe_event_id,
            "chat_id": safe_chat_id,
        }
        if selection_route and safe_preview_run_id is not None:
            return await self._policy.confirm_selection_preview_and_wait(
                route.automation_id,
                preview_run_id=safe_preview_run_id,
                selected_bill_codes=dynamic_inputs["selected_bill_codes"],
                entrypoint=AutomationEntrypoint.FEISHU,
                request_id=safe_event_id,
                actor=actor,
                trusted_context=trusted_context,
                idempotency_key=f"feishu:{safe_event_id}",
                expected_automation_generation=route.automation_generation,
                expected_project_configuration_version=(
                    route.project_configuration_version
                ),
            )
        if selection_route:
            dynamic_inputs = {
                "dry_run": True,
                "selected_bill_codes": [],
                "preview_fingerprint": "",
            }
        trusted_context["dynamic_inputs"] = dynamic_inputs
        result = await self._policy.invoke_trusted_and_wait(
            route.automation_id,
            entrypoint=AutomationEntrypoint.FEISHU,
            request_id=safe_event_id,
            actor=actor,
            trusted_context=trusted_context,
            idempotency_key=f"feishu:{safe_event_id}",
            expected_automation_generation=route.automation_generation,
            expected_project_configuration_version=(
                route.project_configuration_version
            ),
            preview_run_id=(safe_preview_run_id if not selection_route else None),
        )
        if selection_route and str(result.get("status") or "").upper() == "COMPLETED":
            result["selection_preview"] = self._policy.get_selection_preview_projection(
                route.automation_id,
                preview_run_id=str(result.get("run_id") or ""),
            )
        return result

    def describe_feishu_route(
        self,
        route_key: str,
    ) -> AutomationProjectEntrypointRoute:
        """Return the exact committed Feishu route for a server-owned flow.

        The projection is intentionally limited to non-secret project config
        and Business Account identifiers.  Callers cannot resolve a route by
        tool/plugin id and therefore cannot pick the first repeated instance.
        """

        return self._require_route(AutomationEntrypoint.FEISHU, route_key)

    def require_feishu_account_bindings(
        self,
        route_key: str,
        *roles: str,
    ) -> dict[str, str | tuple[str, ...]]:
        route = self.describe_feishu_route(route_key)
        requested = tuple(str(role or "").strip() for role in roles)
        if not requested or not all(requested) or len(requested) != len(set(requested)):
            raise OrchestrationError(
                "PROJECT_ACCOUNT_BINDING_INVALID",
                "Automation account role request is invalid",
            )
        result: dict[str, str | tuple[str, ...]] = {}
        for role in requested:
            binding = route.account_bindings.get(role)
            if binding is None:
                raise OrchestrationError(
                    "PROJECT_ACCOUNT_BINDING_MISSING",
                    "Automation project account binding is unavailable",
                )
            result[role] = binding
        return result

    async def invoke_webhook(
        self,
        *,
        route_key: str,
        source_event_id: str,
        webhook_path: str,
        envelope: Mapping[str, Any] | None = None,
        preview_run_id: Any | None = None,
    ) -> dict[str, Any]:
        route = self._require_route(AutomationEntrypoint.WEBHOOK, route_key)
        safe_event_id = _stable_identifier(source_event_id, "source_event_id")
        safe_path = str(webhook_path or "").strip("/")
        if not safe_path or len(safe_path) > 512:
            raise OrchestrationError(
                "PROJECT_ROUTE_INVALID",
                "Verified Webhook path is invalid",
            )
        dynamic_inputs = _extract_dynamic_inputs(route, envelope or {})
        safe_preview_run_id = None
        if preview_run_id is not None:
            if (
                route.route_key != _SCAN_WEBHOOK_ROUTE_KEY
                or route.automation_id != _SCAN_AUTOMATION_ID
            ):
                raise OrchestrationError(
                    "SCAN_PREVIEW_ID_INVALID",
                    "A scan preview cannot be used by this Webhook route",
                )
            if not isinstance(preview_run_id, str):
                raise OrchestrationError(
                    "SCAN_PREVIEW_ID_INVALID",
                    "Scan preview run id must be a canonical UUID string",
                )
            safe_preview_run_id = normalize_preview_run_id(preview_run_id)
            if preview_run_id != safe_preview_run_id:
                raise OrchestrationError(
                    "SCAN_PREVIEW_ID_INVALID",
                    "Scan preview run id must be canonical",
                )
        return await self._policy.invoke_trusted_and_wait(
            route.automation_id,
            entrypoint=AutomationEntrypoint.WEBHOOK,
            request_id=safe_event_id,
            actor=Actor(
                ActorType.WEBHOOK,
                route.route_id,
                authenticated_by="signed_webhook_route",
            ),
            trusted_context={
                "route_id": route.route_id,
                "route_revision": route.route_revision,
                "source_event_id": safe_event_id,
                "webhook_path": safe_path,
                "dynamic_inputs": dynamic_inputs,
            },
            idempotency_key=f"webhook:{route.route_id}:{safe_event_id}",
            expected_automation_generation=route.automation_generation,
            expected_project_configuration_version=(
                route.project_configuration_version
            ),
            preview_run_id=safe_preview_run_id,
        )

    def _require_route(
        self,
        entrypoint: AutomationEntrypoint,
        route_key: str,
    ) -> AutomationProjectEntrypointRoute:
        safe_key = _normalize_route_key(entrypoint, route_key)
        route = self._routes.resolve_committed_route(
            entrypoint=entrypoint,
            route_key=safe_key,
        )
        if route is None:
            raise OrchestrationError(
                "PROJECT_ROUTE_NOT_FOUND",
                "Automation project route is unavailable",
            )
        if route.entrypoint is not entrypoint or route.route_key != safe_key:
            raise OrchestrationError(
                "PROJECT_ROUTE_INVALID",
                "Automation project route identity does not match",
            )
        if route.enabled is not True:
            raise OrchestrationError(
                "PROJECT_ENTRYPOINT_DISABLED",
                "Automation project route is disabled",
            )
        return route


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
    route: AutomationProjectEntrypointRoute,
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


def _normalize_route_key(
    entrypoint: AutomationEntrypoint,
    value: Any,
) -> str:
    route_key = str(value or "").strip()
    if entrypoint is AutomationEntrypoint.WEBHOOK:
        route_key = route_key.strip("/")
    if not route_key or len(route_key) > 191:
        raise OrchestrationError(
            "PROJECT_ROUTE_NOT_FOUND",
            "Automation project route is unavailable",
        )
    return route_key
