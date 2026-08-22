from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase

from agent.automation_plugins.binding_resolver import ProductionProjectBindingResolver
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    PluginTrustSource,
    RuntimeCoeffectKind,
    RuntimeCoeffectSnapshot,
    RuntimeGenerationRecord,
    RuntimeGenerationSnapshot,
    RuntimeGenerationState,
)
from agent.orchestration.automation_project_entrypoints import (
    AutomationProjectEntrypointRoute,
    AutomationProjectEntrypoints,
    CommittedAutomationProjectRouteResolver,
    TrustedDynamicArgumentResolver,
)
from agent.orchestration.models import ActorType, OrchestrationError
from shared.automation_project_authorization import (
    OMIT_DYNAMIC_ARGUMENT,
    AutomationEntrypoint,
)


class _PolicyService:
    def __init__(self) -> None:
        self.calls = []

    async def invoke_trusted_and_wait(self, automation_id, **kwargs):
        self.calls.append((automation_id, kwargs))
        return {"success": True, "status": "COMPLETED", "run_id": "run-one"}


class _RouteResolver:
    def __init__(self, *routes: AutomationProjectEntrypointRoute) -> None:
        self.routes = {
            (route.entrypoint, route.route_key): route for route in routes
        }

    def resolve_committed_route(self, *, entrypoint, route_key):
        return self.routes.get((entrypoint, route_key))


def _sha(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _committed_route_fixture(*, entrypoint: str = "webhook", duplicate: bool = False):
    route_role = f"{entrypoint}_route"
    resource_id = (
        "phase7.scan_webhook"
        if entrypoint == "webhook"
        else "automation.feishu_route.scan_codes"
    )
    route_config = (
        {"path": "webhook/phase7/scan"}
        if entrypoint == "webhook"
        else {"route_key": "builtin.scan_codes"}
    )
    resource = {
        "resource_kind": route_role,
        **route_config,
        "_meta": {
            "source": "migration",
            "configuration_version": 2,
            "config_sha256": "a" * 64,
            "updated_at": "2026-08-15 10:00:00",
        },
    }
    resource_role = {
        "role": route_role,
        "allowed_kinds": [route_role],
        "required": False,
    }
    descriptor = {
        "resource_id": resource_id,
        "resource_kind": route_role,
        "source": "migration",
        "configuration_version": 2,
        "config_sha256": "a" * 64,
        "updated_at": "2026-08-15 10:00:00",
    }
    metadata = {
        "project_config_version": 3,
        "project_config": {"trigger_flow": False},
        "account_bindings": {"account_id": "business-account"},
        "resource_bindings": {route_role: resource_id},
        "device_binding": None,
        "schedule": {"kind": "none", "times": [], "enabled": False},
        "compiled_invocations": {
            entrypoint: {
                "arguments": {},
                "dynamic_resolvers": {
                    "trigger_flow": f"verified_{entrypoint}_trigger_flow"
                },
            }
        },
        "runtime_descriptor": {
            "install_metadata": {
                "install_root": "/srv/plugins/scan",
                "python_relative": "venv/bin/python",
            },
            "runtime": {"kind": "python_subprocess", "entrypoint": "payload/main.py"},
            "runtime_permissions": {},
            "account_roles": [],
            "resource_roles": [resource_role],
        },
        "action_contract": {
            "input_schema": {
                "type": "object",
                "properties": {"trigger_flow": {"type": "boolean"}},
                "required": [],
                "additionalProperties": False,
            }
        },
        "governance_anchor": {},
    }
    snapshot = RuntimeGenerationSnapshot(
        automation_id="scan-instance",
        generation=4,
        plugin_id="sync_scan_codes",
        plugin_version="1.0.0",
        package_sha256="1" * 64,
        manifest_sha256="2" * 64,
        trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
        project_config_sha256=_sha(metadata["project_config"]),
        account_bindings_sha256=_sha(metadata["account_bindings"]),
        resource_bindings_sha256=_sha(metadata["resource_bindings"]),
        device_binding_sha256=_sha(None),
        schedule_sha256=_sha(metadata["schedule"]),
        core_registry_sha256=_sha({}),
        tool_contract_sha256=_sha(metadata["action_contract"]),
        invocation_contracts_sha256=_sha({}),
        compiled_invocations_sha256=_sha(metadata["compiled_invocations"]),
        runtime_descriptor_sha256=_sha(metadata["runtime_descriptor"]),
        governance_anchor_sha256=_sha(metadata["governance_anchor"]),
        policy_contract_sha256=_sha({}),
        enabled_entrypoints=(entrypoint,),
        execution_metadata=metadata,
        created_at=datetime.now(timezone.utc),
    )
    entries = [SimpleNamespace(
        automation_id="scan-instance",
        target_generation=4,
        committed_generation=4,
        reconcile_state="STABLE",
        committed_snapshot=snapshot,
    )]
    observation = RuntimeCoeffectSnapshot(
        kind=RuntimeCoeffectKind.RESOURCE,
        key=route_role,
        revision=_sha(descriptor),
        ready=True,
    )
    generation = RuntimeGenerationRecord(
        snapshot=snapshot,
        state=RuntimeGenerationState.COMMITTED,
        coeffects=(observation,),
    )
    generations = {("scan-instance", 4): generation}
    if duplicate:
        duplicate_snapshot = replace(snapshot, automation_id="scan-instance-two")
        duplicate_generation = replace(generation, snapshot=duplicate_snapshot)
        entries.append(
            SimpleNamespace(
                automation_id="scan-instance-two",
                target_generation=4,
                committed_generation=4,
                reconcile_state="STABLE",
                committed_snapshot=duplicate_snapshot,
            )
        )
        generations[("scan-instance-two", 4)] = duplicate_generation

    class _Catalog:
        @staticmethod
        def list(*, include_disabled=True):
            del include_disabled
            return list(entries)

    class _Runtime:
        @staticmethod
        def get_generation(automation_id, generation_number):
            return generations.get((automation_id, generation_number))

    class _Accounts:
        @staticmethod
        def list_accounts(**_kwargs):
            return []

    class _Workers:
        @staticmethod
        def get_worker_device(_device_id):
            return None

    provider = lambda requested_resource_id: (
        resource if requested_resource_id == resource_id else None
    )
    bindings = ProductionProjectBindingResolver(
        account_manager=_Accounts(),
        resource_provider=provider,
        worker_repository=_Workers(),
    )
    return _Catalog(), _Runtime(), bindings, provider, resource


def _route(
    entrypoint: AutomationEntrypoint,
    *,
    action_fields=frozenset({"trigger_flow", "dry_run"}),
    dynamic_fields=frozenset({"trigger_flow"}),
) -> AutomationProjectEntrypointRoute:
    return AutomationProjectEntrypointRoute(
        route_id=f"route-{entrypoint.value}",
        route_key=f"{entrypoint.value}-scan",
        entrypoint=entrypoint,
        automation_id="scan-instance",
        automation_generation=4,
        project_configuration_version=3,
        route_revision=2,
        action_fields=action_fields,
        dynamic_fields=dynamic_fields,
    )


class AutomationProjectEntrypointTests(TestCase):
    def test_scan_webhook_uses_exact_instance_and_omits_account_arguments(self):
        policy = _PolicyService()
        route = _route(AutomationEntrypoint.WEBHOOK)
        service = AutomationProjectEntrypoints(
            policy,
            route_resolver=_RouteResolver(route),
        )

        result = asyncio.run(
            service.invoke_webhook(
                route_key="webhook-scan",
                source_event_id="scan-one",
                webhook_path="phase7/scan",
                envelope={
                    "body": {
                        "trigger_flow": False,
                        "delivery_attempt": "transport-only",
                    },
                    "query": {"event_id": "scan-one"},
                },
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, len(policy.calls))
        automation_id, trusted = policy.calls[0]
        self.assertEqual("scan-instance", automation_id)
        self.assertNotIn("arguments", trusted)
        self.assertEqual(4, trusted["expected_automation_generation"])
        self.assertEqual(3, trusted["expected_project_configuration_version"])
        self.assertEqual(ActorType.WEBHOOK, trusted["actor"].actor_type)
        self.assertEqual(
            {"trigger_flow": False},
            trusted["trusted_context"]["dynamic_inputs"],
        )

    def test_webhook_rejects_account_and_saved_config_overrides(self):
        policy = _PolicyService()
        route = _route(AutomationEntrypoint.WEBHOOK)
        service = AutomationProjectEntrypoints(
            policy,
            route_resolver=_RouteResolver(route),
        )
        for envelope, code in (
            (
                {"body": {"account_id": "caller-account"}, "query": {}},
                "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN",
            ),
            (
                {"body": {"dry_run": True}, "query": {}},
                "PROJECT_ARGUMENT_OVERRIDE_FORBIDDEN",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(OrchestrationError) as raised:
                    asyncio.run(
                        service.invoke_webhook(
                            route_key="webhook-scan",
                            source_event_id="scan-two",
                            webhook_path="phase7/scan",
                            envelope=envelope,
                        )
                    )
                self.assertEqual(code, raised.exception.code)
        self.assertEqual([], policy.calls)

    def test_feishu_route_is_explicit_and_stable_event_id_is_required(self):
        policy = _PolicyService()
        route = _route(AutomationEntrypoint.FEISHU)
        service = AutomationProjectEntrypoints(
            policy,
            route_resolver=_RouteResolver(route),
        )

        asyncio.run(
            service.invoke_feishu(
                route_key="feishu-scan",
                event_id="event-one",
                sender_id="user-one",
                chat_id="chat-one",
                envelope={"body": {"trigger_flow": True}, "query": {}},
            )
        )

        _automation_id, trusted = policy.calls[0]
        self.assertEqual("feishu:event-one", trusted["idempotency_key"])
        self.assertEqual(ActorType.FEISHU_USER, trusted["actor"].actor_type)
        self.assertEqual("feishu_verified_event", trusted["actor"].authenticated_by)

        with self.assertRaises(OrchestrationError) as raised:
            asyncio.run(
                service.invoke_feishu(
                    route_key="feishu-scan",
                    event_id="",
                    sender_id="user-one",
                    chat_id="chat-one",
                )
            )
        self.assertEqual("STABLE_EVENT_ID_REQUIRED", raised.exception.code)

    def test_route_must_exist_for_the_exact_entrypoint_key(self):
        service = AutomationProjectEntrypoints(
            _PolicyService(),
            route_resolver=_RouteResolver(),
        )
        with self.assertRaises(OrchestrationError) as raised:
            asyncio.run(
                service.invoke_webhook(
                    route_key="missing",
                    source_event_id="event-one",
                    webhook_path="phase7/scan",
                )
            )
        self.assertEqual("PROJECT_ROUTE_NOT_FOUND", raised.exception.code)

    def test_code_owned_dynamic_resolver_rejects_cross_entrypoint_values(self):
        resolver = TrustedDynamicArgumentResolver()
        scheduled_for = datetime.fromisoformat("2026-08-15T00:10:00+08:00")
        self.assertEqual(
            "2026-08-14",
            resolver(
                "scheduled_previous_day",
                "target_date",
                {
                    "entrypoint": "scheduler",
                    "scheduled_for": scheduled_for.isoformat(),
                },
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "scheduler",
                    "scheduled_for": "2026-08-14T16:30:00+00:00",
                },
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "console",
                    "occurred_at": "2026-08-14T16:30:00+00:00",
                },
            ),
        )
        with self.assertRaises(ValueError):
            resolver(
                "current_business_day",
                "target_date",
                {
                    "entrypoint": "webhook",
                    "occurred_at": "2026-08-14T16:30:00+00:00",
                },
            )
        self.assertEqual(
            "R001",
            resolver(
                "verified_webhook_bill_code",
                "BILL_CODE",
                {
                    "entrypoint": "webhook",
                    "dynamic_inputs": {"BILL_CODE": "R001"},
                },
            ),
        )
        with self.assertRaises(ValueError):
            resolver(
                "verified_feishu_bill_code",
                "BILL_CODE",
                {
                    "entrypoint": "webhook",
                    "dynamic_inputs": {"BILL_CODE": "R001"},
                },
            )
        self.assertIs(
            OMIT_DYNAMIC_ARGUMENT,
            resolver(
                "verified_optional_feishu_target_date",
                "target_date",
                {"entrypoint": "feishu", "dynamic_inputs": {}},
            ),
        )
        self.assertEqual(
            "2026-08-15",
            resolver(
                "verified_optional_feishu_target_date",
                "target_date",
                {
                    "entrypoint": "feishu",
                    "dynamic_inputs": {"target_date": "2026-08-15"},
                },
            ),
        )

    def test_committed_webhook_route_binds_exact_generation_and_resource_revision(self):
        catalog, runtime, bindings, provider, resource = _committed_route_fixture()
        resolver = CommittedAutomationProjectRouteResolver(
            catalog=catalog,
            runtime_repository=runtime,
            binding_resolver=bindings,
            resource_provider=provider,
        )

        route = resolver.resolve_committed_route(
            entrypoint=AutomationEntrypoint.WEBHOOK,
            route_key="/webhook/phase7/scan/",
        )

        self.assertIsNotNone(route)
        self.assertEqual("scan-instance", route.automation_id)
        self.assertEqual(4, route.automation_generation)
        self.assertEqual(2, route.route_revision)
        self.assertEqual(frozenset({"trigger_flow"}), route.dynamic_fields)

        resource["_meta"]["configuration_version"] = 3
        resource["_meta"]["config_sha256"] = "b" * 64
        with self.assertRaises(OrchestrationError) as raised:
            resolver.resolve_committed_route(
                entrypoint=AutomationEntrypoint.WEBHOOK,
                route_key="webhook/phase7/scan",
            )
        self.assertEqual("PROJECT_ROUTE_STALE", raised.exception.code)

    def test_committed_feishu_route_exposes_only_snapshot_config_and_account_ids(self):
        catalog, runtime, bindings, provider, _resource = _committed_route_fixture(
            entrypoint="feishu"
        )
        resolver = CommittedAutomationProjectRouteResolver(
            catalog=catalog,
            runtime_repository=runtime,
            binding_resolver=bindings,
            resource_provider=provider,
        )
        service = AutomationProjectEntrypoints(
            _PolicyService(),
            route_resolver=resolver,
        )

        route = service.describe_feishu_route("builtin.scan_codes")

        self.assertEqual("scan-instance", route.automation_id)
        self.assertEqual({"trigger_flow": False}, route.project_config)
        self.assertEqual(
            {"account_id": "business-account"},
            route.account_bindings,
        )

    def test_disabled_committed_route_is_resolved_then_hard_blocked(self):
        catalog, runtime, bindings, provider, _resource = _committed_route_fixture(
            entrypoint="feishu"
        )
        entry = catalog.list()[0]
        entry.committed_snapshot = replace(
            entry.committed_snapshot,
            enabled_entrypoints=(),
        )
        resolver = CommittedAutomationProjectRouteResolver(
            catalog=catalog,
            runtime_repository=runtime,
            binding_resolver=bindings,
            resource_provider=provider,
        )
        service = AutomationProjectEntrypoints(
            _PolicyService(),
            route_resolver=resolver,
        )

        with self.assertRaises(OrchestrationError) as raised:
            service.describe_feishu_route("builtin.scan_codes")

        self.assertEqual("PROJECT_ENTRYPOINT_DISABLED", raised.exception.code)

    def test_duplicate_committed_feishu_alias_is_ambiguous_and_never_uses_first(self):
        catalog, runtime, bindings, provider, _resource = _committed_route_fixture(
            entrypoint="feishu",
            duplicate=True,
        )
        resolver = CommittedAutomationProjectRouteResolver(
            catalog=catalog,
            runtime_repository=runtime,
            binding_resolver=bindings,
            resource_provider=provider,
        )

        with self.assertRaises(OrchestrationError) as raised:
            resolver.resolve_committed_route(
                entrypoint=AutomationEntrypoint.FEISHU,
                route_key="builtin.scan_codes",
            )

        self.assertEqual("PROJECT_ROUTE_AMBIGUOUS", raised.exception.code)


if __name__ == "__main__":
    import unittest

    unittest.main()
