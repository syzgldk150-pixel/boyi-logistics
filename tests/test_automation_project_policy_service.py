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


class AutomationProjectPolicyServiceTests(AutomationProjectPolicyServiceTestBase):
    def test_console_invoke_builds_only_server_owned_project_identity(self):
        receipt = self.service.invoke_console(
            AUTOMATION_ID,
            request_id="request-console",
            actor=_admin(),
        )

        self.assertEqual("run-invoke", receipt.run_id)
        command = self.gateway.command
        self.assertIsNotNone(command)
        self.assertEqual("automation.project.invoke", command.command_type)
        self.assertEqual({"mode": "saved"}, command.parameters["arguments"])
        self.assertEqual(1, command.automation_invocation.automation_generation)
        self.assertEqual(CONTRACT_HASH, command.automation_invocation.contract_hash)

    def test_service_v2_console_invoke_requires_exact_active_contribution(self):
        self._set_service_v2_console_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)

        receipt = service.invoke_console(
            AUTOMATION_ID,
            request_id="request-service-v2-console",
            actor=_admin(),
            contribution_id="run_now",
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual(
            [
                {
                    "automation_id": AUTOMATION_ID,
                    "generation": 1,
                    "contribution_kind": "console",
                    "contribution_id": "run_now",
                }
            ],
            registry.calls,
        )

    def test_service_v2_selection_preview_is_host_owned_and_read_phase_bound(self):
        self._set_service_v2_selection_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)

        receipt = service.invoke_selection_preview(
            AUTOMATION_ID,
            request_id="selection-preview",
            actor=_admin(),
        )

        self.assertEqual("run-invoke", receipt.run_id)
        command = self.gateway.command
        self.assertEqual("execute_console", command.automation_invocation.contract_id)
        self.assertEqual(
            {
                "mode": "saved",
                "dry_run": True,
                "selected_bill_codes": [],
                "preview_fingerprint": "",
            },
            command.parameters["arguments"],
        )
        self.assertEqual(
            "PREVIEW",
            command.parameters["execution_context"]["selection_phase"],
        )

        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_console(
                AUTOMATION_ID,
                request_id="selection-direct",
                actor=_admin(),
                contribution_id="execute_console",
            )
        self.assertEqual("SELECTION_INPUT_INVALID", raised.exception.code)

    def test_service_v2_selection_confirmation_replays_inside_and_after_ttl(self):
        self._set_service_v2_selection_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)
        service._load_contract = (  # type: ignore[method-assign]
            lambda _automation_id: self.fail(
                "exact selection replay must precede current contract resolution"
            )
        )
        now = datetime.now(timezone.utc)
        cases = (
            (
                "11111111-1111-4111-8111-111111111111",
                "selection-replay-active",
                now - timedelta(minutes=5),
            ),
            (
                "22222222-2222-4222-8222-222222222222",
                "selection-replay-expired",
                now - timedelta(minutes=20),
            ),
        )

        for preview_run_id, request_id, observed_at in cases:
            with self.subTest(request_id=request_id):
                key, row = self._persisted_selection_confirmation(
                    preview_run_id=preview_run_id,
                    request_id=request_id,
                    observed_at=observed_at,
                )
                self.repository.state.commands_by_idempotency[("console", key)] = row

                receipt = service.confirm_selection_preview(
                    AUTOMATION_ID,
                    preview_run_id=preview_run_id,
                    selected_bill_codes=["R0001"],
                    request_id=request_id,
                    actor=_admin(),
                )

                self.assertEqual(row["command_id"], receipt.command_id)
                self.assertEqual(
                    row["parameters_json"],
                    self.gateway.command.parameters,
                )

        with self.assertRaises(OrchestrationError) as raised:
            service.confirm_selection_preview(
                AUTOMATION_ID,
                preview_run_id=cases[-1][0],
                selected_bill_codes=["R0002"],
                request_id=cases[-1][1],
                actor=_admin(),
            )
        self.assertEqual("REQUEST_ID_REUSED", raised.exception.code)
        self.assertEqual([], registry.calls)

    def test_service_v2_selection_guard_replays_race_before_live_checks(self):
        self._set_service_v2_selection_contract()
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)
        preview_run_id = "44444444-4444-4444-8444-444444444444"
        request_id = "selection-concurrent-loser"
        key, row = self._persisted_selection_confirmation(
            preview_run_id=preview_run_id,
            request_id=request_id,
            observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        self.repository.state.commands_by_idempotency[("console", key)] = row
        preview_context = row["parameters_json"]["execution_context"][
            "selection_preview"
        ]
        formal_arguments = row["parameters_json"]["arguments"]
        resolution = SimpleNamespace(
            context=preview_context,
            formal_arguments={
                "dry_run": formal_arguments["dry_run"],
                "selected_bill_codes": formal_arguments["selected_bill_codes"],
                "preview_fingerprint": formal_arguments["preview_fingerprint"],
            },
        )
        original_get = _Commands.get_by_idempotency
        lookups: list[bool] = []

        def race_lookup(
            commands: _Commands,
            source: str,
            idempotency_key: str,
            *,
            for_update: bool = False,
        ):
            lookups.append(for_update)
            if len(lookups) == 1:
                return None
            return original_get(
                commands,
                source,
                idempotency_key,
                for_update=for_update,
            )

        with (
            patch.object(_Commands, "get_by_idempotency", race_lookup),
            patch(
                "agent.orchestration.automation_project_policy_service.resolve_selection_preview",
                return_value=resolution,
            ) as resolve,
            patch.object(
                service,
                "_lock_and_compile_contract",
                side_effect=AssertionError(
                    "exact concurrent replay must precede live contract locking"
                ),
            ) as lock_contract,
        ):
            receipt = service.confirm_selection_preview(
                AUTOMATION_ID,
                preview_run_id=preview_run_id,
                selected_bill_codes=["R0001"],
                request_id=request_id,
                actor=_admin(),
            )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual([False, True, False], lookups)
        self.assertEqual(1, resolve.call_count)
        self.assertFalse(resolve.call_args.kwargs["for_update"])
        lock_contract.assert_not_called()
        self.assertEqual(1, len(registry.calls))

    def test_service_v2_selection_confirmation_pins_context_and_consumes_once(self):
        self._set_service_v2_selection_contract()
        service = self._service_with_contribution_registry(_ContributionRegistry())
        preview_run_id = "33333333-3333-4333-8333-333333333333"
        preview_context = {
            "observed_at": "2026-08-31T01:02:03Z",
            "context_sha256": "e" * 64,
        }
        resolution = SimpleNamespace(
            context=preview_context,
            formal_arguments={
                "dry_run": False,
                "selected_bill_codes": ["R0001"],
                "preview_fingerprint": "f" * 64,
            },
        )
        consumed = OrchestrationError(
            "SELECTION_PREVIEW_ALREADY_CONSUMED",
            "synthetic preview consumption conflict",
        )

        with (
            patch(
                "agent.orchestration.automation_project_policy_service.resolve_selection_preview",
                return_value=resolution,
            ) as resolve,
            patch(
                "agent.orchestration.automation_project_policy_service.ensure_selection_preview_active"
            ) as ensure_active,
            patch(
                "agent.orchestration.automation_project_policy_service.consume_selection_preview"
            ) as consume,
        ):
            receipt = service.confirm_selection_preview(
                AUTOMATION_ID,
                preview_run_id=preview_run_id,
                selected_bill_codes=["R0001"],
                request_id="selection-first-confirmation",
                actor=_admin(),
            )

            self.assertEqual("run-invoke", receipt.run_id)
            self.assertEqual(2, resolve.call_count)
            self.assertFalse(resolve.call_args_list[0].kwargs["for_update"])
            self.assertTrue(resolve.call_args_list[1].kwargs["for_update"])
            ensure_active.assert_called_once()
            consume.assert_called_once()
            self.assertEqual(
                preview_context,
                self.gateway.command.parameters["execution_context"][
                    "selection_preview"
                ],
            )
            self.assertEqual(
                preview_context["observed_at"],
                self.gateway.command.parameters["execution_context"]["occurred_at"],
            )

            consume.side_effect = consumed
            with self.assertRaises(OrchestrationError) as raised:
                service.confirm_selection_preview(
                    AUTOMATION_ID,
                    preview_run_id=preview_run_id,
                    selected_bill_codes=["R0001"],
                    request_id="selection-different-request",
                    actor=_admin(),
                )
            self.assertEqual(
                "SELECTION_PREVIEW_ALREADY_CONSUMED",
                raised.exception.code,
            )

    def test_service_v2_selection_project_allows_non_selection_sibling(self):
        self._set_service_v2_selection_contract()
        self.entry.contributions["console"].append(
            {
                "id": "inspect_console",
                "title": "Inspect status",
                "service": "plugin.selection@1",
                "operation": "inspect",
                "default_enabled": False,
            }
        )
        self.contract = replace(
            self.contract,
            invocation_contracts={
                **self.contract.invocation_contracts,
                "inspect_console": InvocationArgumentContract(
                    contract_id="inspect_console",
                    entrypoint="console",
                    expected_arguments={"mode": "saved"},
                    dynamic_argument_resolvers={},
                    contribution_id="inspect_console",
                ),
            },
        )
        service = self._service_with_contribution_registry(_ContributionRegistry())

        receipt = service.invoke_console(
            AUTOMATION_ID,
            request_id="selection-sibling-invoke",
            actor=_admin(),
            contribution_id="inspect_console",
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertNotIn(
            "selection_phase",
            self.gateway.command.parameters["execution_context"],
        )
        invocation = AutomationProjectInvocation(
            automation_id=AUTOMATION_ID,
            automation_generation=1,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="inspect_console",
            contract_hash=CONTRACT_HASH,
            policy_version=1,
            project_configuration_version=1,
            request_id="selection-sibling-policy",
        )
        evaluation = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )
        self.assertTrue(evaluation.allowed)
        self.assertEqual("PROJECT_FULL_AUTO", evaluation.code)

    def test_service_v2_selection_formal_requires_persisted_preview_identity(self):
        self._set_service_v2_selection_contract()
        service = self._service_with_contribution_registry(_ContributionRegistry())

        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.CONSOLE,
                request_id="selection-formal-without-preview",
                actor=_admin(),
                trusted_context={
                    "dynamic_inputs": {
                        "dry_run": False,
                        "selected_bill_codes": ["R0001"],
                        "preview_fingerprint": "f" * 64,
                    }
                },
                contribution_id="execute_console",
            )
        self.assertEqual("SELECTION_PREVIEW_REQUIRED", raised.exception.code)

    def test_service_v2_selection_policy_matches_contract_without_host_fields(self):
        self._set_service_v2_selection_contract()
        invocation = AutomationProjectInvocation(
            automation_id=AUTOMATION_ID,
            automation_generation=1,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="execute_console",
            contract_hash=CONTRACT_HASH,
            policy_version=1,
            project_configuration_version=1,
            request_id="selection-policy",
        )
        preview_plan = replace(
            _plan(invocation),
            steps=(
                replace(
                    _plan(invocation).steps[0],
                    operation_type=OperationType.READ,
                    risk_level=RiskLevel.LOW,
                    arguments={
                        "mode": "saved",
                        "dry_run": True,
                        "selected_bill_codes": [],
                        "preview_fingerprint": "",
                    },
                ),
            ),
        )

        preview = self.service.evaluate_invocation(
            preview_plan,
            _admin(),
            "console",
            {"selection_phase": "PREVIEW"},
            invocation,
        )

        self.assertTrue(preview.allowed)
        self.assertFalse(preview.requires_approval)
        self.assertEqual("SELECTION_PREVIEW_ALLOWED", preview.code)

    def test_service_v2_missing_or_stale_console_projection_fails_closed(self):
        self._set_service_v2_console_contract()

        for error_code in ("RUNTIME_PROJECTION_STALE", "CAPABILITY_UNAVAILABLE"):
            with self.subTest(error_code=error_code):
                registry = _ContributionRegistry(error_code=error_code)
                service = self._service_with_contribution_registry(registry)

                with self.assertRaises(OrchestrationError) as raised:
                    service.invoke_console(
                        AUTOMATION_ID,
                        request_id=f"request-{error_code.lower()}",
                        actor=_admin(),
                        contribution_id="run_now",
                    )

                self.assertEqual(
                    "PROJECT_RUNTIME_PROJECTION_STALE",
                    raised.exception.code,
                )
                self.assertIsNone(self.gateway.command)

    def test_action_v1_ignores_registry_but_service_v2_requires_it(self):
        registry = _ContributionRegistry(error_code="RUNTIME_PROJECTION_STALE")
        service = self._service_with_contribution_registry(registry)
        self.entry.runtime_model = "ACTION_V1"

        action_receipt = service.invoke_console(
            AUTOMATION_ID,
            request_id="request-action-v1-with-registry",
            actor=_admin(),
        )

        self.assertEqual("run-invoke", action_receipt.run_id)
        self.assertEqual([], registry.calls)

        self._set_service_v2_console_contract()
        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_console(
                AUTOMATION_ID,
                request_id="request-service-v2-without-registry",
                actor=_admin(),
                contribution_id="run_now",
            )

        self.assertEqual("PROJECT_RUNTIME_PROJECTION_STALE", raised.exception.code)


    def test_action_v1_event_entrypoint_is_always_disabled(self):
        self.entry.runtime_model = "ACTION_V1"
        self.contract = _contract_for(AutomationEntrypoint.EVENTS)
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)

        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.EVENTS,
                request_id="event-one",
                actor=self._verified_event_actor(),
                trusted_context={"unexpected": "still-disabled-first"},
                expected_automation_generation=1,
            )

        self.assertEqual("PROJECT_ENTRYPOINT_DISABLED", raised.exception.code)
        self.assertEqual([], registry.calls)
        self.assertIsNone(self.gateway.command)

    def test_scan_preview_formal_invoke_stays_disabled_under_current_governance(self):
        self._set_scan_project()
        self.entry.installed_version = "1.0.22"

        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_console(
                AUTOMATION_ID,
                request_id="request-scan-formal-disabled",
                actor=_admin(),
                preview_run_id="11111111-1111-4111-8111-111111111111",
            )

        self.assertEqual(
            "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED",
            raised.exception.code,
        )
        self.assertIsNone(self.gateway.command)

    def test_exact_scan_project_injects_read_only_preview_server_side(self):
        self._set_scan_project()

        receipt = self.service.invoke_console(
            AUTOMATION_ID,
            request_id="request-scan-preview",
            actor=_admin(),
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual(
            {"mode": "saved", "dry_run": True},
            self.gateway.command.parameters["arguments"],
        )
        self.assertNotIn(
            "scan_preview",
            self.gateway.command.parameters["execution_context"],
        )

    def test_selection_preview_injects_server_owned_read_only_arguments(self):
        self._set_selection_project()

        receipt = self.service.invoke_selection_preview(
            "split_pending_problem_upload",
            request_id="request-selection-preview",
            actor=_admin(),
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual(
            {
                "dry_run": True,
                "selected_bill_codes": [],
                "preview_fingerprint": "",
            },
            self.gateway.command.parameters["arguments"],
        )

    def test_selection_workflow_rejects_incomplete_server_inputs(self):
        self._set_selection_project()

        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                "split_pending_problem_upload",
                entrypoint=AutomationEntrypoint.CONSOLE,
                request_id="request-selection-incomplete",
                actor=_admin(),
                trusted_context={"dynamic_inputs": {"dry_run": True}},
            )

        self.assertEqual("SELECTION_INPUT_INVALID", raised.exception.code)
        self.assertIsNone(self.gateway.command)

    def test_trusted_wait_returns_only_bounded_scan_preview_projection(self):
        self._set_scan_project()
        projection = {
            "contract_version": 1,
            "preview_run_id": "run-invoke",
            "selection_count": 2,
            "can_confirm": True,
        }
        self.service.get_scan_preview_projection = (  # type: ignore[method-assign]
            lambda _automation_id, **_kwargs: dict(projection)
        )

        result = asyncio.run(
            self.service.invoke_trusted_and_wait(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.CONSOLE,
                request_id="request-scan-preview-wait",
                actor=_admin(),
            )
        )

        self.assertTrue(result["success"])
        self.assertEqual(projection, result["scan_preview"])

    def test_release_hold_blocks_project_writes_and_typed_invoke(self):
        service = AutomationProjectPolicyService(
            self.repository,
            core_catalog=SimpleNamespace(),
            plugin_catalog=_Catalog(self.entry),
            command_gateway=self.gateway,
            release_hold_provider=lambda: True,
        )
        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_console(
                AUTOMATION_ID,
                request_id="request-held",
                actor=_admin(),
            )
        self.assertEqual("RELEASE_HELD", raised.exception.code)
        self.assertIsNone(self.gateway.command)

    def test_full_auto_policy_save_does_not_lock_credentials_or_stage_runtime(self):
        self.service.get_policy_projection = (  # type: ignore[method-assign]
            lambda _automation_id: {
                "configured_mode": self.repository.state.policy["mode"]
            }
        )
        result = self.service.update_policy(
            AUTOMATION_ID,
            mode="PROJECT_FULL_AUTO",
            request_id="policy-full-auto-one",
            comment="reviewed",
            expected_policy_version=1,
            expected_project_configuration_version=1,
            actor=_admin(),
        )

        self.assertEqual("PROJECT_FULL_AUTO", result["configured_mode"])
        self.assertEqual([("commit",)], self.repository.account_lock_events)

    def test_policy_change_invalidates_and_wakes_sleeping_approval_runs(self):
        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"
        self.repository.state.pending = [
            {
                "approval_id": "approval-one",
                "run_id": "run-one",
                "run_status": "WAITING_APPROVAL",
            }
        ]
        self.service.get_policy_projection = (  # type: ignore[method-assign]
            lambda _automation_id: {
                "configured_mode": self.repository.state.policy["mode"]
            }
        )

        result = self.service.update_policy(
            AUTOMATION_ID,
            mode="PROJECT_FULL_AUTO",
            request_id="policy-wake-waiting-run",
            comment="resume without stale approval",
            expected_policy_version=1,
            expected_project_configuration_version=1,
            actor=_admin(),
        )

        self.assertEqual("PROJECT_FULL_AUTO", result["configured_mode"])
        self.assertEqual([], self.repository.state.pending)
        self.assertEqual(["run-one"], self.repository.runnable_run_ids)
        self.assertEqual(["run-one"], self.woken_run_ids)

    def test_project_takeover_event_request_fits_legacy_char36_and_is_idempotent(self):
        class _ScheduledPolicies:
            def __init__(self):
                self.events = []

            def ensure_default(self, task_id):
                return {"task_id": task_id, "mode": "REQUIRE_EACH_RUN", "version": 1}

            def get_event_by_request(self, task_id, request_id):
                return next(
                    (
                        event
                        for event in self.events
                        if event["task_id"] == task_id
                        and event["request_id"] == request_id
                    ),
                    None,
                )

            def append_event(self, row):
                self.events.append(dict(row))
                return dict(row)

        task_id = "scheduled_task_identifier_0001"
        scheduled = _ScheduledPolicies()
        uow = SimpleNamespace(scheduled_policies=scheduled)
        takeover = AutomationProjectPolicyService._retire_legacy_schedule_policies
        kwargs = {
            "uow": uow,
            "automation_id": AUTOMATION_ID,
            "rows": [{"id": task_id}],
            "actor": _admin(),
            "request_id": "project-policy-request-with-a-longer-id",
            "correlation_id": "correlation-id",
            "occurred_at": datetime.now(timezone.utc),
        }

        takeover(**kwargs)
        takeover(**kwargs)

        self.assertEqual(1, len(scheduled.events))
        self.assertLessEqual(len(scheduled.events[0]["request_id"]), 36)

    def test_full_auto_policy_save_is_independent_of_credential_change(self):
        self.repository.block_account_locks = True
        self.service.update_policy(
            AUTOMATION_ID,
            mode="PROJECT_FULL_AUTO",
            request_id="policy-full-auto-blocked",
            comment="reviewed",
            expected_policy_version=1,
            expected_project_configuration_version=1,
            actor=_admin(),
        )
        self.assertEqual("PROJECT_FULL_AUTO", self.repository.state.policy["mode"])

    def test_policy_save_preserves_target_generation_while_runtime_reconciles(self):
        self.repository.state.project.update(
            {
                "target_generation": 2,
                "committed_generation": 1,
                "reconcile_state": "PREPARING",
            }
        )
        self.repository.state.config["config_version"] = 2
        self.repository.state.policy.update(
            {
                "project_generation": 2,
                "project_configuration_version": 2,
            }
        )

        self.service.update_policy(
            AUTOMATION_ID,
            mode="PROJECT_FULL_AUTO",
            request_id="policy-during-reconcile",
            comment="keep intent",
            expected_policy_version=1,
            expected_project_configuration_version=2,
            actor=_admin(),
        )

        self.assertEqual(2, self.repository.state.policy["project_generation"])

    def test_policy_projection_classifies_all_runtime_transition_states(self):
        self.service._compile_entry = (  # type: ignore[method-assign]
            lambda _entry, _rows: self.contract
        )
        policy = {
            **self.repository.state.policy,
            "mode": "PROJECT_FULL_AUTO",
        }
        transition_states = (
            "PREPARING",
            "WAITING_COEFFECTS",
            "READY_TO_COMMIT",
            "DRAINING",
            "DISPOSING",
        )

        for reconcile_state in transition_states:
            with self.subTest(reconcile_state=reconcile_state):
                entry = SimpleNamespace(
                    automation_id=AUTOMATION_ID,
                    enabled=True,
                    configured=True,
                    target_generation=2,
                    committed_generation=1,
                    reconcile_state=reconcile_state,
                    current_enabled_entrypoints=("console",),
                    project_config_version=2,
                )

                projection = self.service._describe_entry(entry, policy)

                self.assertEqual("PROJECT_FULL_AUTO", projection["configured_mode"])
                self.assertEqual("PROJECT_FULL_AUTO", projection["effective_mode"])
                self.assertEqual("RECONCILING", projection["effective_status"])
                self.assertEqual("RECONCILING", projection["runtime_status"])
                self.assertFalse(projection["runnable"])
                self.assertEqual(
                    f"RECONCILE_{reconcile_state}",
                    projection["runtime_reason"],
                )

    def test_policy_projection_marks_failed_runtime_unavailable_without_downgrade(self):
        self.service._compile_entry = (  # type: ignore[method-assign]
            lambda _entry, _rows: self.contract
        )
        policy = {
            **self.repository.state.policy,
            "mode": "PROJECT_FULL_AUTO",
        }

        for reconcile_state in ("BLOCKED_UNKNOWN_WRITE", "ERROR"):
            with self.subTest(reconcile_state=reconcile_state):
                entry = SimpleNamespace(
                    automation_id=AUTOMATION_ID,
                    enabled=True,
                    configured=True,
                    target_generation=2,
                    committed_generation=1,
                    reconcile_state=reconcile_state,
                    current_enabled_entrypoints=("console",),
                    project_config_version=2,
                )

                projection = self.service._describe_entry(entry, policy)

                self.assertEqual("PROJECT_FULL_AUTO", projection["configured_mode"])
                self.assertEqual("PROJECT_FULL_AUTO", projection["effective_mode"])
                self.assertEqual("UNAVAILABLE", projection["effective_status"])
                self.assertEqual("UNAVAILABLE", projection["runtime_status"])
                self.assertFalse(projection["runnable"])
                self.assertEqual(
                    f"RECONCILE_{reconcile_state}",
                    projection["runtime_reason"],
                )

    def test_policy_projection_uses_stable_reason_priority_for_closed_projects(self):
        self.service._compile_entry = (  # type: ignore[method-assign]
            lambda _entry, _rows: self.contract
        )
        policy = {
            **self.repository.state.policy,
            "mode": "PROJECT_FULL_AUTO",
        }
        cases = (
            (False, False, (), "PROJECT_DISABLED"),
            (True, False, (), "PROJECT_CONFIGURATION_INCOMPLETE"),
            (True, True, (), "ENTRYPOINTS_DISABLED"),
        )
        for enabled, configured, entrypoints, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                entry = SimpleNamespace(
                    automation_id=AUTOMATION_ID,
                    enabled=enabled,
                    configured=configured,
                    target_generation=1,
                    committed_generation=1,
                    reconcile_state="STABLE",
                    current_enabled_entrypoints=entrypoints,
                    project_config_version=1,
                )
                projection = self.service._describe_entry(entry, policy)
                self.assertFalse(projection["runnable"])
                self.assertEqual(expected_reason, projection["runtime_reason"])

    def test_policy_projection_keeps_contract_error_ahead_of_reconcile_reason(self):
        def raise_contract(_entry, _rows):
            raise RuntimeError("invalid committed project contract")

        self.service._compile_entry = raise_contract  # type: ignore[method-assign]
        entry = SimpleNamespace(
            automation_id=AUTOMATION_ID,
            enabled=True,
            configured=True,
            target_generation=2,
            committed_generation=1,
            reconcile_state="PREPARING",
            current_enabled_entrypoints=("console",),
            project_config_version=2,
        )
        projection = self.service._describe_entry(
            entry,
            {**self.repository.state.policy, "mode": "PROJECT_FULL_AUTO"},
        )
        self.assertEqual("PROJECT_CONTRACT_UNAVAILABLE", projection["runtime_reason"])

    def test_startup_defaults_bootstrapped_policy_to_durable_full_auto(self):
        result = self.service.ensure_default_full_auto_policies()

        self.assertEqual({"changed": 1}, result)
        self.assertEqual("PROJECT_FULL_AUTO", self.repository.state.policy["mode"])
        self.assertIsNone(self.repository.state.policy["contract_hash"])
        self.assertEqual(
            "AUTOMATION_DEFAULT_FULL_AUTO",
            self.repository.state.policy_events[0]["reason"],
        )

        # A later administrator choice is authoritative.  The one-time audit
        # marker must stop every subsequent startup from changing it back.
        self.repository.state.policy.update(
            {"mode": "REQUIRE_EACH_RUN", "version": 3}
        )
        replay = self.service.ensure_default_full_auto_policies()
        self.assertEqual({"changed": 0}, replay)
        self.assertEqual("REQUIRE_EACH_RUN", self.repository.state.policy["mode"])
        self.assertEqual(1, len(self.repository.state.policy_events))

    def test_startup_default_never_overwrites_explicit_super_admin_choice(self):
        self.repository.state.policy_events.append(
            {
                "automation_id": AUTOMATION_ID,
                "request_id": "administrator-choice",
                "reason": "SUPER_ADMIN_PROJECT_POLICY_CHANGED",
                "to_mode": "REQUIRE_EACH_RUN",
            }
        )

        result = self.service.ensure_default_full_auto_policies()

        self.assertEqual({"changed": 0}, result)
        self.assertEqual("REQUIRE_EACH_RUN", self.repository.state.policy["mode"])
        self.assertEqual(1, len(self.repository.state.policy_events))

    def test_default_full_auto_mode_is_approval_free_and_toggle_requires_approval(self):
        invocation = _invocation()
        self.repository.state.policy["mode"] = "PROJECT_FULL_AUTO"

        automatic = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(automatic.allowed)
        self.assertFalse(automatic.requires_approval)

        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"
        approval_required = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(approval_required.allowed)
        self.assertTrue(approval_required.requires_approval)

    def test_service_v2_ignores_historical_require_each_run_policy(self):
        self.entry.runtime_model = "SERVICE_V2"
        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"
        invocation = _invocation()

        decision = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)
        self.assertEqual("PROJECT_FULL_AUTO", decision.code)

    def test_service_v2_projection_hides_historical_policy_mode(self):
        self.service._compile_entry = (  # type: ignore[method-assign]
            lambda _entry, _rows: self.contract
        )
        entry = SimpleNamespace(
            automation_id=AUTOMATION_ID,
            runtime_model="SERVICE_V2",
            enabled=True,
            configured=True,
            target_generation=1,
            committed_generation=1,
            reconcile_state="STABLE",
            current_enabled_entrypoints=("console",),
            project_config_version=1,
        )

        for historical_mode in ("REQUIRE_EACH_RUN", "LEGACY_SCHEDULE_ONLY"):
            with self.subTest(historical_mode=historical_mode):
                projection = self.service._describe_entry(
                    entry,
                    {**self.repository.state.policy, "mode": historical_mode},
                )

                self.assertEqual(
                    "PROJECT_FULL_AUTO", projection["configured_mode"]
                )
                self.assertEqual("PROJECT_FULL_AUTO", projection["effective_mode"])

    def test_action_v1_require_each_run_still_requires_approval(self):
        self.entry.runtime_model = "ACTION_V1"
        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"
        invocation = _invocation()

        decision = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.requires_approval)
        self.assertEqual("PROJECT_APPROVAL_REQUIRED", decision.code)

    def test_service_v2_full_auto_still_honors_contract_restriction(self):
        self.entry.runtime_model = "SERVICE_V2"
        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"
        self.contract = replace(
            self.contract,
            can_full_auto=False,
            restriction_code="PROJECT_CONTRACT_NOT_RUNNABLE",
        )
        invocation = _invocation()

        decision = self.service.evaluate_invocation(
            _plan(invocation),
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertFalse(decision.allowed)
        self.assertFalse(decision.requires_approval)
        self.assertEqual("PROJECT_CONTRACT_NOT_RUNNABLE", decision.code)

    def test_scan_preview_never_requires_formal_project_approval(self):
        plan, invocation = self._scan_policy_subject(dry_run=True)
        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"

        for source in ("console", "feishu", "webhook"):
            with self.subTest(source=source):
                decision = self.service.evaluate_invocation(
                    plan,
                    _admin(),
                    source,
                    {},
                    replace(
                        invocation,
                        entrypoint=AutomationEntrypoint(source),
                        contract_id=source,
                    ),
                )

                self.assertTrue(decision.allowed)
                self.assertFalse(decision.requires_approval)
                self.assertEqual("SCAN_PREVIEW_ALLOWED", decision.code)

    def test_selection_preview_matches_saved_contract_without_approval(self):
        plan, invocation = self._selection_policy_subject(dry_run=True)
        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"

        decision = self.service.evaluate_invocation(
            plan,
            _admin(),
            "console",
            {"dynamic_inputs": dict(plan.steps[0].arguments)},
            invocation,
        )

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)
        self.assertEqual("SELECTION_PREVIEW_ALLOWED", decision.code)

    def test_selection_formal_matches_saved_contract_and_honors_policy(self):
        plan, invocation = self._selection_policy_subject(dry_run=False)
        self.repository.state.policy["mode"] = "PROJECT_FULL_AUTO"

        decision = self.service.evaluate_invocation(
            plan,
            _admin(),
            "console",
            {"dynamic_inputs": dict(plan.steps[0].arguments)},
            invocation,
        )

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.requires_approval)

    def test_scan_formal_require_each_run_requires_approval_for_all_entrypoints(self):
        plan, invocation = self._scan_policy_subject(dry_run=False)
        self.repository.state.policy["mode"] = "REQUIRE_EACH_RUN"
        execution_context = {
            "scan_preview": plan.steps[0].arguments["_scan_preview_binding"]
        }

        for source in ("console", "feishu", "webhook"):
            with self.subTest(source=source):
                decision = self.service.evaluate_invocation(
                    plan,
                    _admin(),
                    source,
                    execution_context,
                    replace(
                        invocation,
                        entrypoint=AutomationEntrypoint(source),
                        contract_id=source,
                    ),
                )
                self.assertTrue(decision.allowed)
                self.assertTrue(decision.requires_approval)
                self.assertEqual("PROJECT_APPROVAL_REQUIRED", decision.code)

    def test_scan_formal_honors_current_full_auto_mode_for_all_entrypoints(self):
        plan, invocation = self._scan_policy_subject(dry_run=False)
        execution_context = {
            "scan_preview": plan.steps[0].arguments["_scan_preview_binding"]
        }
        self.repository.state.policy.update(
            {
                "mode": "PROJECT_FULL_AUTO",
                "approved_by_actor_id": "system:migration",
                "approved_by_actor_role": "system",
            }
        )

        for source in ("console", "feishu", "webhook"):
            with self.subTest(source=source):
                current = self.service.evaluate_invocation(
                    plan,
                    _admin(),
                    source,
                    execution_context,
                    replace(
                        invocation,
                        entrypoint=AutomationEntrypoint(source),
                        contract_id=source,
                    ),
                )
                self.assertTrue(current.allowed)
                self.assertFalse(current.requires_approval)
                self.assertEqual("PROJECT_FULL_AUTO", current.code)

    def test_policy_version_drift_rechecks_current_durable_mode(self):
        invocation = _invocation()
        plan = _plan(invocation)

        self.repository.state.policy.update(
            {"mode": "PROJECT_FULL_AUTO", "version": 2}
        )
        automatic = self.service.evaluate_invocation(
            plan,
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(automatic.allowed)
        self.assertFalse(automatic.requires_approval)

        self.repository.state.policy.update(
            {"mode": "REQUIRE_EACH_RUN", "version": 3}
        )
        approval_required = self.service.evaluate_invocation(
            plan,
            _admin(),
            "console",
            {},
            invocation,
        )

        self.assertTrue(approval_required.allowed)
        self.assertTrue(approval_required.requires_approval)

    def test_scheduler_invocation_binds_exact_row_generation_and_context(self):
        self.contract = _contract_for(AutomationEntrypoint.SCHEDULER)
        actor = Actor(
            ActorType.SCHEDULER,
            "schedule-one",
            roles=("system",),
            authenticated_by="apscheduler",
        )

        receipt = self.service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.SCHEDULER,
            request_id="scheduler:schedule-one:2026-08-15T07:00:00+08:00",
            actor=actor,
            trusted_context={
                "task_id": "schedule-one",
                "scheduled_for": "2026-08-15T07:00:00+08:00",
                "cron_expression": "0 7 * * *",
                "configuration_version": 1,
            },
            idempotency_key="scheduler:schedule-one:2026-08-15T07:00:00+08:00",
            expected_automation_generation=1,
            expected_project_configuration_version=1,
        )

        self.assertEqual("run-invoke", receipt.run_id)
        command = self.gateway.command
        self.assertEqual("scheduler", command.source)
        self.assertEqual(
            "scheduler:schedule-one",
            command.automation_invocation.contract_id,
        )
        self.assertEqual(
            "schedule-one",
            command.parameters["execution_context"]["task_id"],
        )

    def test_service_v2_scheduler_invoke_requires_exact_active_contribution(self):
        self.entry.runtime_model = "SERVICE_V2"
        scheduler_contract = InvocationArgumentContract(
            contract_id="scheduler:schedule-one",
            entrypoint="scheduler",
            expected_arguments={"mode": "saved"},
            dynamic_argument_resolvers={},
            contribution_id="daily_run",
        )
        self.contract = replace(
            self.contract,
            invocation_contracts={scheduler_contract.contract_id: scheduler_contract},
            allowed_entrypoints=frozenset({"scheduler"}),
        )
        registry = _ContributionRegistry()
        service = self._service_with_contribution_registry(registry)
        actor = Actor(
            ActorType.SCHEDULER,
            "schedule-one",
            roles=("system",),
            authenticated_by="apscheduler",
        )

        receipt = service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint=AutomationEntrypoint.SCHEDULER,
            request_id="scheduler:schedule-one:service-v2",
            actor=actor,
            trusted_context={"task_id": "schedule-one"},
            expected_automation_generation=1,
            expected_project_configuration_version=1,
            contribution_id="daily_run",
        )

        self.assertEqual("run-invoke", receipt.run_id)
        self.assertEqual(
            [
                {
                    "automation_id": AUTOMATION_ID,
                    "generation": 1,
                    "contribution_kind": "scheduler",
                    "contribution_id": "daily_run",
                }
            ],
            registry.calls,
        )

    def test_trusted_wait_preserves_terminal_error_for_scheduler_status(self):
        self.contract = _contract_for(AutomationEntrypoint.SCHEDULER)
        self.gateway.run_result = {
            "run_id": "run-invoke",
            "command_id": "command-failed",
            "work_item_id": "work-invoke",
            "status": "FAILED_TERMINAL",
            "correlation_id": "correlation-failed",
            "error_code": "PROJECT_INVOCATION_STALE",
            "error_summary": "Committed automation contract no longer matches",
        }
        actor = Actor(
            ActorType.SCHEDULER,
            "schedule-one",
            roles=("system",),
            authenticated_by="apscheduler",
        )

        result = asyncio.run(
            self.service.invoke_trusted_and_wait(
                AUTOMATION_ID,
                entrypoint=AutomationEntrypoint.SCHEDULER,
                request_id="scheduler:schedule-one:failed",
                actor=actor,
                trusted_context={
                    "task_id": "schedule-one",
                    "scheduled_for": "2026-08-15T07:00:00+08:00",
                    "cron_expression": "0 7 * * *",
                    "configuration_version": 1,
                },
                expected_automation_generation=1,
                expected_project_configuration_version=1,
            )
        )

        self.assertFalse(result["success"])
        self.assertEqual("PROJECT_INVOCATION_STALE", result["error_code"])
        self.assertEqual(
            "Committed automation contract no longer matches",
            result["error_summary"],
        )

    def test_scheduler_invocation_rejects_missing_or_different_task_contract(self):
        self.contract = _contract_for(AutomationEntrypoint.SCHEDULER)
        actor = Actor(
            ActorType.SCHEDULER,
            "schedule-one",
            roles=("system",),
            authenticated_by="apscheduler",
        )
        for context, code in (
            ({}, "PROJECT_SCHEDULE_ID_REQUIRED"),
            ({"task_id": "schedule-two"}, "PROJECT_ENTRYPOINT_DISABLED"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(OrchestrationError) as raised:
                    self.service.invoke_trusted(
                        AUTOMATION_ID,
                        entrypoint=AutomationEntrypoint.SCHEDULER,
                        request_id=f"scheduler-{code.lower()}",
                        actor=actor,
                        trusted_context=context,
                        expected_automation_generation=1,
                    )
                self.assertEqual(code, raised.exception.code)
        self.assertIsNone(self.gateway.command)

    def test_webhook_dynamic_values_come_only_from_verified_route_context(self):
        self.contract = _contract_for(
            AutomationEntrypoint.WEBHOOK,
            dynamic_resolvers={"BILL_CODE": "verified_webhook_field"},
        )
        self.service._dynamic_resolver = (  # type: ignore[attr-defined]
            lambda resolver_id, field, context: (
                context["dynamic_inputs"][field]
                if resolver_id == "verified_webhook_field"
                else None
            )
        )
        actor = Actor(
            ActorType.WEBHOOK,
            "route-one",
            authenticated_by="signed_webhook_route",
        )

        self.service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint="webhook",
            request_id="event-one",
            actor=actor,
            trusted_context={
                "route_id": "route-one",
                "route_revision": 3,
                "source_event_id": "event-one",
                "webhook_path": "scan-sync",
                "webhook_method": "POST",
                "dynamic_inputs": {"BILL_CODE": "10001"},
            },
            expected_automation_generation=1,
        )

        command = self.gateway.command
        self.assertEqual(
            {"mode": "saved", "BILL_CODE": "10001"},
            command.parameters["arguments"],
        )
        self.assertNotIn("account_id", command.parameters["arguments"])

    def test_trusted_invocation_rejects_untrusted_actor_and_context_override(self):
        self.contract = _contract_for(AutomationEntrypoint.WEBHOOK)
        actor = Actor(ActorType.WEBHOOK, "route-one")
        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint="webhook",
                request_id="event-one",
                actor=actor,
                expected_automation_generation=1,
            )
        self.assertEqual("TRUSTED_ENTRYPOINT_REQUIRED", raised.exception.code)

        actor = replace(actor, authenticated_by="signed_webhook_route")
        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint="webhook",
                request_id="event-one",
                actor=actor,
                trusted_context={"arguments": {"account_id": "override"}},
                expected_automation_generation=1,
            )
        self.assertEqual("TRUSTED_CONTEXT_INVALID", raised.exception.code)

    def test_grouped_approval_is_atomic_when_one_member_changes(self):
        invocation = _invocation()
        self.repository.state.pending = [
            _pending("approval-one", invocation),
            _pending("approval-two", invocation),
        ]
        pending = self.service.pending_approvals(AUTOMATION_ID, actor=_admin())
        self.repository.state.fail_decision_at = 2

        with self.assertRaises(OrchestrationError) as raised:
            self.service.decide_pending_approvals(
                AUTOMATION_ID,
                decision="APPROVED",
                expected_pending_set_hash=pending["pending_set_hash"],
                request_id="batch-one",
                comment="approve both",
                actor=_admin(),
            )

        self.assertEqual("PENDING_SET_CHANGED", raised.exception.code)
        self.assertEqual(2, len(self.repository.state.pending))
        self.assertEqual([], self.repository.state.decisions)
        self.assertEqual({}, self.repository.state.batches)

    def test_grouped_approval_returns_one_safe_receipt_per_visible_run(self):
        invocation = _invocation()
        self.repository.state.pending = [
            _pending("approval-one", invocation),
            _pending("approval-two", invocation),
        ]
        pending = self.service.pending_approvals(AUTOMATION_ID, actor=_admin())

        result = self.service.decide_pending_approvals(
            AUTOMATION_ID,
            decision="APPROVED",
            expected_pending_set_hash=pending["pending_set_hash"],
            request_id="batch-safe-receipts",
            comment="approve visible set",
            actor=_admin(),
        )

        self.assertEqual(2, result["decided_count"])
        self.assertEqual(
            {"run-approval-one", "run-approval-two"},
            {receipt["run_id"] for receipt in result["run_receipts"]},
        )
        self.assertTrue(
            all(
                set(receipt)
                == {"automation_id", "work_item_id", "run_id", "status"}
                for receipt in result["run_receipts"]
            )
        )

    def test_grouped_approval_survives_policy_version_drift_when_plan_is_current(self):
        invocation = _invocation()
        self.repository.state.pending = [_pending("approval-one", invocation)]
        # Policy intent may be saved again while the immutable plugin/config
        # contract remains current.  Version drift alone must not strand the
        # approval in the matters center.
        self.repository.state.policy.update(
            {"mode": "REQUIRE_EACH_RUN", "version": 2}
        )
        pending = self.service.pending_approvals(AUTOMATION_ID, actor=_admin())

        result = self.service.decide_pending_approvals(
            AUTOMATION_ID,
            decision="APPROVED",
            expected_pending_set_hash=pending["pending_set_hash"],
            request_id="batch-policy-version-drift",
            comment="approve current plan",
            actor=_admin(),
        )

        self.assertEqual(1, result["decided_count"])
        self.assertEqual(["run-approval-one"], self.repository.runnable_run_ids)
        self.assertEqual(["run-approval-one"], self.woken_run_ids)

    def test_grouped_approval_replay_is_exact_and_does_not_repeat_decisions(self):
        invocation = _invocation()
        self.repository.state.pending = [_pending("approval-one", invocation)]
        pending = self.service.pending_approvals(AUTOMATION_ID, actor=_admin())
        first = self.service.decide_pending_approvals(
            AUTOMATION_ID,
            decision="APPROVED",
            expected_pending_set_hash=pending["pending_set_hash"],
            request_id="batch-one",
            comment="approve",
            actor=_admin(),
        )
        second = self.service.decide_pending_approvals(
            AUTOMATION_ID,
            decision="APPROVED",
            expected_pending_set_hash=pending["pending_set_hash"],
            request_id="batch-one",
            comment="approve",
            actor=_admin(),
        )

        self.assertEqual(first, second)
        self.assertEqual(0, first["pending_count"])
        self.assertEqual("APPROVED", first["decision"])
        self.assertEqual(1, first["decided_count"])
        self.assertEqual(
            [
                {
                    "automation_id": AUTOMATION_ID,
                    "work_item_id": "work-approval-one",
                    "run_id": "run-approval-one",
                    "status": "WAITING_APPROVAL",
                }
            ],
            first["run_receipts"],
        )
        self.assertNotIn("approval_id", first["run_receipts"][0])
        self.assertNotIn("plan_hash", first["run_receipts"][0])
        self.assertEqual(1, len(self.repository.state.decisions))
        self.assertEqual(["run-approval-one"], self.repository.runnable_run_ids)
        self.assertEqual(["run-approval-one"], self.woken_run_ids)
        with self.assertRaises(OrchestrationError) as raised:
            self.service.decide_pending_approvals(
                AUTOMATION_ID,
                decision="APPROVED",
                expected_pending_set_hash=pending["pending_set_hash"],
                request_id="batch-one",
                comment="different",
                actor=_admin(),
            )
        self.assertEqual("REQUEST_ID_REUSED", raised.exception.code)


if __name__ == "__main__":
    import unittest

    unittest.main()
