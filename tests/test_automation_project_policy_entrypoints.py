from __future__ import annotations

import asyncio
import copy
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    CommandReceipt,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
    RunStatus,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    CompiledAutomationProjectContract,
    InvocationArgumentContract,
    canonical_sha256,
)
from shared.account_execution_locks import AccountExecutionLockUnavailable
from shared.orchestration_repository import InvalidStateError
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_DRAFT_FIELDS,
    WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID,
)


AUTOMATION_ID = "instance-one"
CONTRACT_HASH = "a" * 64
TOOL_HASH = "b" * 64
PLUGIN_HASH = "c" * 64
MANIFEST_HASH = "d" * 64

from tests.automation_project_policy_service_support import (
    _admin,
    _contract,
    _contract_for,
    _invocation,
    _plan,
    _pending,
    _State,
    _AutomationProjects,
    _AutomationPlugins,
    _Approvals,
    _Events,
    _Runs,
    _Commands,
    _Uow,
    _Repository,
    _Catalog,
    _Gateway,
    _ContributionRegistry,
    _ModuleSlotRegistry,
    AutomationProjectPolicyServiceTestBase,
)


class AutomationProjectPolicyEntrypointTests(AutomationProjectPolicyServiceTestBase):
    def test_service_v2_harness_and_scheduler_require_runtime_projection(self):
        self.entry.runtime_model = "SERVICE_V2"
        cases = (
            (
                AutomationEntrypoint.HARNESS,
                "harness_lookup",
                "harness_lookup",
                _admin(),
                {},
            ),
            (
                AutomationEntrypoint.SCHEDULER,
                "scheduler:schedule-one",
                "daily_run",
                Actor(
                    ActorType.SCHEDULER,
                    "schedule-one",
                    roles=("system",),
                    authenticated_by="apscheduler",
                ),
                {"task_id": "schedule-one"},
            ),
        )
        for source, contract_id, contribution_id, actor, context in cases:
            with self.subTest(source=source.value):
                self.gateway.command = None
                self.contract = replace(
                    _contract(),
                    invocation_contracts={
                        contract_id: InvocationArgumentContract(
                            contract_id=contract_id,
                            entrypoint=source.value,
                            expected_arguments={"mode": "saved"},
                            dynamic_argument_resolvers={},
                            contribution_id=contribution_id,
                        )
                    },
                    allowed_entrypoints=frozenset({source.value}),
                )

                with self.assertRaises(OrchestrationError) as raised:
                    self.service.invoke_trusted(
                        AUTOMATION_ID,
                        entrypoint=source,
                        request_id=f"request-{source.value}-without-registry",
                        actor=actor,
                        trusted_context=context,
                        expected_automation_generation=1,
                        contribution_id=contribution_id,
                    )

                self.assertEqual(
                    "PROJECT_RUNTIME_PROJECTION_STALE",
                    raised.exception.code,
                )
                self.assertIsNone(self.gateway.command)

    def test_service_v2_feishu_revalidates_exact_projection_at_acceptance(self):
        self._set_service_v2_feishu_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)

        receipt = service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.FEISHU,
            request_id="event-service-v2-feishu",
            actor=self._verified_feishu_actor(),
            trusted_context={
                "event_id": "event-service-v2-feishu",
                "chat_id": "chat-one",
            },
            idempotency_key="feishu:event-service-v2-feishu",
            expected_automation_generation=1,
            contribution_id="lookup_command",
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual(
            [
                {
                    "automation_id": AUTOMATION_ID,
                    "generation": 1,
                    "contribution_kind": "feishu",
                    "contribution_id": "lookup_command",
                },
                {
                    "automation_id": AUTOMATION_ID,
                    "generation": 1,
                    "contribution_kind": "feishu",
                    "contribution_id": "lookup_command",
                },
            ],
            registry.calls,
        )

    def test_service_v2_feishu_requires_an_injected_runtime_projection(self):
        self._set_service_v2_feishu_contract()

        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.FEISHU,
                request_id="event-missing-projection",
                actor=self._verified_feishu_actor(),
                trusted_context={
                    "event_id": "event-missing-projection",
                    "chat_id": "chat-one",
                },
                expected_automation_generation=1,
                contribution_id="lookup_command",
            )

        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertIsNone(self.gateway.command)

    def test_service_v2_feishu_rejects_mismatched_projection_identity(self):
        self._set_service_v2_feishu_contract()

        for field_name, wrong_value in (
            ("generation", 2),
            ("contribution_kind", "console"),
        ):
            with self.subTest(field_name=field_name):
                registry = _ContributionRegistry()

                def mismatched_resolve_active(**kwargs):
                    registry.calls.append(dict(kwargs))
                    values = {
                        **kwargs,
                        "phase": "COMMITTED",
                        "backend_status": "READY",
                    }
                    values[field_name] = wrong_value
                    return SimpleNamespace(**values)

                registry.resolve_active = mismatched_resolve_active
                service = self._service_with_contribution_registry(registry)

                with self.assertRaises(OrchestrationError) as raised:
                    service.invoke_trusted(
                        AUTOMATION_ID,
                        entrypoint=AutomationEntrypoint.FEISHU,
                        request_id=f"event-wrong-{field_name}",
                        actor=self._verified_feishu_actor(),
                        trusted_context={
                            "event_id": f"event-wrong-{field_name}",
                            "chat_id": "chat-one",
                        },
                        expected_automation_generation=1,
                        contribution_id="lookup_command",
                    )

                self.assertEqual(
                    "PROJECT_RUNTIME_PROJECTION_STALE",
                    raised.exception.code,
                )
                self.assertIsNone(self.gateway.command)

    def test_service_v2_feishu_projection_race_fails_in_uow_guard(self):
        self._set_service_v2_feishu_contract()
        registry = _ContributionRegistry()

        def racing_resolve_active(**kwargs):
            registry.calls.append(dict(kwargs))
            if len(registry.calls) > 1:
                raise PluginConflictError(
                    "synthetic generation switch",
                    code="RUNTIME_PROJECTION_STALE",
                )
            return SimpleNamespace(
                **kwargs,
                phase="COMMITTED",
                backend_status="READY",
            )

        registry.resolve_active = racing_resolve_active
        service = self._service_with_contribution_registry(registry)

        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.FEISHU,
                request_id="event-racing-projection",
                actor=self._verified_feishu_actor(),
                trusted_context={
                    "event_id": "event-racing-projection",
                    "chat_id": "chat-one",
                },
                expected_automation_generation=1,
                contribution_id="lookup_command",
            )

        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual(2, len(registry.calls))
        self.assertIsNone(self.gateway.command)

    def test_service_v2_module_slot_rechecks_exact_handle_in_uow_guard(self):
        self._set_service_v2_module_slot_contract()
        registry = _ModuleSlotRegistry()
        service = self._service_with_contribution_registry(registry)
        service._dynamic_resolver = (  # type: ignore[method-assign]
            lambda resolver_id, field_name, context: (
                context["dynamic_inputs"][field_name] if resolver_id == WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID else None
            )
        )
        waybill = {field: "" for field in WAYBILL_ENTRY_DRAFT_FIELDS}

        receipt = service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.MODULE_SLOTS,
            request_id="11111111-1111-4111-8111-111111111111",
            actor=_admin(),
            trusted_context={
                "module_slot": {
                    "slot": "waybill_entry.validators",
                    "handle": "a" * 64,
                },
                "dynamic_inputs": {"waybill": waybill},
            },
            expected_automation_generation=1,
            contribution_id="validate_waybill",
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual(
            [
                {"slot": "waybill_entry.validators", "handle": "a" * 64},
                {"slot": "waybill_entry.validators", "handle": "a" * 64},
            ],
            registry.calls,
        )
        self.assertEqual(waybill, self.gateway.command.parameters["arguments"]["waybill"])

    def test_service_v2_module_slot_generation_switch_fails_before_acceptance(self):
        self._set_service_v2_module_slot_contract()
        registry = _ModuleSlotRegistry(fail_after_first=True)
        service = self._service_with_contribution_registry(registry)
        service._dynamic_resolver = (  # type: ignore[method-assign]
            lambda _resolver_id, field_name, context: context["dynamic_inputs"][field_name]
        )

        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.MODULE_SLOTS,
                request_id="22222222-2222-4222-8222-222222222222",
                actor=_admin(),
                trusted_context={
                    "module_slot": {
                        "slot": "waybill_entry.validators",
                        "handle": "a" * 64,
                    },
                    "dynamic_inputs": {"waybill": {field: "" for field in WAYBILL_ENTRY_DRAFT_FIELDS}},
                },
                expected_automation_generation=1,
                contribution_id="validate_waybill",
            )

        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual(2, len(registry.calls))
        self.assertIsNone(self.gateway.command)

    def test_action_v1_module_slot_is_rejected_before_dispatch(self):
        self.entry.runtime_model = "ACTION_V1"

        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.MODULE_SLOTS,
                request_id="33333333-3333-4333-8333-333333333333",
                actor=_admin(),
                trusted_context={},
                expected_automation_generation=1,
                contribution_id="forged",
            )

        self.assertEqual("PROJECT_ENTRYPOINT_DISABLED", raised.exception.code)
        self.assertIsNone(self.gateway.command)

    def test_action_v1_feishu_invocation_does_not_require_managed_projection(self):
        self.entry.runtime_model = "ACTION_V1"
        self.contract = _contract_for(AutomationEntrypoint.FEISHU)

        receipt = self.service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.FEISHU,
            request_id="event-action-v1",
            actor=self._verified_feishu_actor(),
            trusted_context={"event_id": "event-action-v1", "chat_id": "chat-one"},
            expected_automation_generation=1,
        )

        self.assertEqual("run-invoke", receipt.run_id)

    def test_service_v2_webhook_revalidates_exact_projection_at_acceptance(self):
        self._set_service_v2_webhook_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)

        receipt = service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.WEBHOOK,
            request_id="event-service-v2-webhook",
            actor=self._verified_webhook_actor(),
            trusted_context={
                "route_id": "route-owner-digest",
                "route_revision": 1,
                "source_event_id": "event-service-v2-webhook",
                "webhook_path": "webhook/status_update",
                "webhook_method": "POST",
            },
            idempotency_key="webhook:event-owner-digest",
            expected_automation_generation=1,
            contribution_id="receive_status",
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual({"mode": "saved"}, self.gateway.command.parameters["arguments"])
        expected_call = {
            "automation_id": AUTOMATION_ID,
            "generation": 1,
            "contribution_kind": "webhook",
            "contribution_id": "receive_status",
        }
        self.assertEqual([expected_call, expected_call], registry.calls)

    def test_service_v2_webhook_rejects_dynamic_argument_input(self):
        self._set_service_v2_webhook_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)

        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.WEBHOOK,
                request_id="event-dynamic-webhook",
                actor=self._verified_webhook_actor(),
                trusted_context={
                    "route_id": "route-owner-digest",
                    "route_revision": 1,
                    "source_event_id": "event-dynamic-webhook",
                    "webhook_path": "webhook/status_update",
                    "webhook_method": "POST",
                    "dynamic_inputs": {"mode": "override"},
                },
                expected_automation_generation=1,
                contribution_id="receive_status",
            )

        self.assertEqual("TRUSTED_CONTEXT_INVALID", raised.exception.code)
        self.assertEqual([], registry.calls)
        self.assertIsNone(self.gateway.command)

    def test_service_v2_webhook_requires_matching_projection_and_uow_recheck(self):
        self._set_service_v2_webhook_contract()
        context = {
            "route_id": "route-owner-digest",
            "route_revision": 1,
            "source_event_id": "event-webhook",
            "webhook_path": "webhook/status_update",
            "webhook_method": "POST",
        }

        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.WEBHOOK,
                request_id="event-no-registry",
                actor=self._verified_webhook_actor(),
                trusted_context=context,
                expected_automation_generation=1,
                contribution_id="receive_status",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)

        registry = _ContributionRegistry()

        def mismatched_resolve_active(**kwargs):
            registry.calls.append(dict(kwargs))
            return SimpleNamespace(
                **{**kwargs, "contribution_kind": "feishu"},
                phase="COMMITTED",
                backend_status="READY",
            )

        registry.resolve_active = mismatched_resolve_active
        service = self._service_with_contribution_registry(registry)
        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.WEBHOOK,
                request_id="event-mismatched-registry",
                actor=self._verified_webhook_actor(),
                trusted_context=context,
                expected_automation_generation=1,
                contribution_id="receive_status",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual(1, len(registry.calls))

        registry = _ContributionRegistry()

        def racing_resolve_active(**kwargs):
            registry.calls.append(dict(kwargs))
            if len(registry.calls) > 1:
                raise PluginConflictError(
                    "synthetic Webhook generation switch",
                    code="RUNTIME_PROJECTION_STALE",
                )
            return SimpleNamespace(
                **kwargs,
                phase="COMMITTED",
                backend_status="READY",
            )

        registry.resolve_active = racing_resolve_active
        service = self._service_with_contribution_registry(registry)
        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.WEBHOOK,
                request_id="event-racing-registry",
                actor=self._verified_webhook_actor(),
                trusted_context=context,
                expected_automation_generation=1,
                contribution_id="receive_status",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual(2, len(registry.calls))
        self.assertIsNone(self.gateway.command)

    def test_service_v2_event_revalidates_exact_projection_at_acceptance(self):
        self._set_service_v2_event_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)

        receipt = service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.EVENTS,
            request_id="event-one",
            actor=self._verified_event_actor(),
            trusted_context={
                "event_name": "shipment.created",
                "source_event_id": "event-one",
            },
            idempotency_key="event:v2:owner-event-digest",
            expected_automation_generation=1,
            contribution_id="handle_created",
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual({"mode": "saved"}, self.gateway.command.parameters["arguments"])
        expected_call = {
            "automation_id": AUTOMATION_ID,
            "generation": 1,
            "contribution_kind": "events",
            "contribution_id": "handle_created",
        }
        self.assertEqual([expected_call, expected_call], registry.calls)

    def test_service_v2_event_requires_exact_closed_context(self):
        self._set_service_v2_event_contract()
        valid = {
            "event_name": "shipment.created",
            "source_event_id": "event-one",
        }
        cases = (
            ({"event_name": "shipment.created"}, "event-one"),
            ({**valid, "extra": "blocked"}, "event-one"),
            ({**valid, "source_event_id": "event-two"}, "event-one"),
            ({**valid, "event_name": "Shipment.created"}, "event-one"),
            ({**valid, "event_name": "shipment/created"}, "event-one"),
            ({**valid, "event_name": "x" * 129}, "event-one"),
            ({**valid, "source_event_id": " event-one"}, "event-one"),
            ({**valid, "source_event_id": "x" * 192}, "event-one"),
        )
        for context, request_id in cases:
            with self.subTest(context=context):
                registry = _ContributionRegistry()
                service = self._service_with_contribution_registry(registry)
                with self.assertRaises(OrchestrationError) as raised:
                    service.invoke_trusted(
                        AUTOMATION_ID,
                        entrypoint=AutomationEntrypoint.EVENTS,
                        request_id=request_id,
                        actor=self._verified_event_actor(),
                        trusted_context=context,
                        expected_automation_generation=1,
                        contribution_id="handle_created",
                    )
                self.assertEqual("TRUSTED_CONTEXT_INVALID", raised.exception.code)
                self.assertEqual([], registry.calls)
                self.assertIsNone(self.gateway.command)

    def test_service_v2_event_requires_managed_event_actor(self):
        self._set_service_v2_event_contract()
        invalid_actors = (
            Actor(ActorType.WEBHOOK, "event:owner-digest", authenticated_by="managed_event_dispatcher"),
            Actor(ActorType.EVENT, "event:owner-digest"),
            Actor(
                ActorType.EVENT,
                "event:owner-digest",
                roles=("system",),
                authenticated_by="managed_event_dispatcher",
            ),
        )
        for actor in invalid_actors:
            with self.subTest(actor=actor):
                with self.assertRaises(OrchestrationError) as raised:
                    self.service.invoke_trusted(
                        AUTOMATION_ID,
                        entrypoint=AutomationEntrypoint.EVENTS,
                        request_id="event-one",
                        actor=actor,
                        trusted_context={
                            "event_name": "shipment.created",
                            "source_event_id": "event-one",
                        },
                        expected_automation_generation=1,
                        contribution_id="handle_created",
                    )
                self.assertEqual("TRUSTED_ENTRYPOINT_REQUIRED", raised.exception.code)

    def test_service_v2_event_requires_exact_declaration_and_uow_recheck(self):
        self._set_service_v2_event_contract()
        context = {"event_name": "shipment.created", "source_event_id": "event-one"}
        for registry in (
            _ContributionRegistry(event_name="shipment.updated"),
            _ContributionRegistry(durable=True),
        ):
            with self.subTest(registry=registry):
                service = self._service_with_contribution_registry(registry)
                with self.assertRaises(OrchestrationError) as raised:
                    service.invoke_trusted(
                        AUTOMATION_ID,
                        entrypoint=AutomationEntrypoint.EVENTS,
                        request_id="event-one",
                        actor=self._verified_event_actor(),
                        trusted_context=context,
                        expected_automation_generation=1,
                        contribution_id="handle_created",
                    )
                self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
                self.assertEqual(1, len(registry.calls))

        registry = _ContributionRegistry()

        def mismatched_resolve_active(**kwargs):
            registry.calls.append(dict(kwargs))
            return SimpleNamespace(
                **{**kwargs, "contribution_kind": "webhook"},
                phase="COMMITTED",
                backend_status="READY",
                declaration={"event": "shipment.created", "durable": False},
            )

        registry.resolve_active = mismatched_resolve_active
        service = self._service_with_contribution_registry(registry)
        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.EVENTS,
                request_id="event-one",
                actor=self._verified_event_actor(),
                trusted_context=context,
                expected_automation_generation=1,
                contribution_id="handle_created",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual(1, len(registry.calls))

        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.EVENTS,
                request_id="event-one",
                actor=self._verified_event_actor(),
                trusted_context=context,
                expected_automation_generation=1,
                contribution_id="handle_created",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)

        registry = _ContributionRegistry()

        def racing_resolve_active(**kwargs):
            registry.calls.append(dict(kwargs))
            if len(registry.calls) > 1:
                raise RuntimeError("synthetic Event generation switch")
            return SimpleNamespace(
                **kwargs,
                phase="COMMITTED",
                backend_status="READY",
                declaration={"event": "shipment.created", "durable": False},
            )

        registry.resolve_active = racing_resolve_active
        service = self._service_with_contribution_registry(registry)
        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.EVENTS,
                request_id="event-one",
                actor=self._verified_event_actor(),
                trusted_context=context,
                expected_automation_generation=1,
                contribution_id="handle_created",
            )
        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)
        self.assertEqual(2, len(registry.calls))
        self.assertIsNone(self.gateway.command)
