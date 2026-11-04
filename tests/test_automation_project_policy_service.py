from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    OrchestrationError,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    InvocationArgumentContract,
)
from shared.orchestration_repository_support import InvalidStateError


AUTOMATION_ID = "instance-one"
CONTRACT_HASH = "a" * 64
TOOL_HASH = "b" * 64
PLUGIN_HASH = "c" * 64
MANIFEST_HASH = "d" * 64

from tests.automation_project_policy_service_support import (
    _admin,
    _contract_for,
    _Catalog,
    _ContributionRegistry,
    AutomationProjectPolicyServiceTestBase,
)


class AutomationProjectPolicyServiceTests(AutomationProjectPolicyServiceTestBase):
    def _completed_selection_preview(self, service, request_id="selection-read"):
        receipt = service.invoke_selection_preview(AUTOMATION_ID, request_id=request_id, actor=_admin())
        row = self.direct.rows[receipt["invocation_id"]]
        row.update(status="COMPLETED", result_json={"status": "SUCCESS", "error": None,
            "meta": {"observed_at": datetime.now(timezone.utc).isoformat()},
            "data": {"dry_run": True, "candidate_count": 1, "selection_limit": 250,
                "candidates": [{"bill_code": "R0001"}], "preview_fingerprint": "f" * 64}})
        return receipt["invocation_id"]

    def test_list_scope_filters_before_contract_projection_and_unknown_ids_are_empty(self):
        def no_full_catalog():
            raise AssertionError("scoped list must not construct unrelated catalog entries")

        self.service._plugin_catalog.list = no_full_catalog
        result = self.service.list_policies(automation_ids=[AUTOMATION_ID])
        self.assertEqual([AUTOMATION_ID], [row["automation_id"] for row in result["items"]])
        self.assertFalse(result["items"][0]["runnable"])
        self.assertEqual({"items": [], "missing_automation_ids": ["not-installed"]}, self.service.list_policies(automation_ids=["not-installed"]))
        with self.assertRaises(OrchestrationError):
            self.service.list_policies(automation_ids=[AUTOMATION_ID, AUTOMATION_ID])

    def test_console_invoke_builds_only_server_owned_project_identity(self):
        receipt = self.service.invoke_console(AUTOMATION_ID, request_id="request-console", actor=_admin())
        self.assertIn("invocation_id", receipt)
        self.assertNotIn("run_id", receipt)
        self.assertEqual({"mode": "saved"}, self.direct.call.arguments)
        self.assertEqual(1, self.direct.call.invocation.automation_generation)
        self.assertEqual(CONTRACT_HASH, self.direct.call.invocation.contract_hash)

    def test_failed_invocation_creation_rolls_back_policy_state(self):
        self.repository.state.fail_invocation_create_after_guard = True
        before = copy.deepcopy(self.repository.state.__dict__)
        with self.assertRaises(InvalidStateError):
            self.service.invoke_console(AUTOMATION_ID, request_id="direct-create", actor=_admin())
        self.assertEqual({}, self.direct.rows)
        self.assertEqual(before, self.repository.state.__dict__)

    def test_second_distinct_request_cannot_cancel_a_just_accepted_invocation(self):
        first = self.service.invoke_console(AUTOMATION_ID, request_id="first", actor=_admin())
        saved = copy.deepcopy(self.direct.rows[first["invocation_id"]])
        second = self.service.invoke_console(AUTOMATION_ID, request_id="second", actor=_admin())
        self.assertNotEqual(first["invocation_id"], second["invocation_id"])
        self.assertEqual(saved, self.direct.rows[first["invocation_id"]])

    def test_acceptance_locks_project_and_avoids_absent_idempotency_gap_locks(self):
        with patch.object(self.service, "_lock_and_compile_contract", wraps=self.service._lock_and_compile_contract) as guard:
            receipt = self.service.invoke_console(AUTOMATION_ID, request_id="guard", actor=_admin())
        guard.assert_called_once()
        self.assertTrue(guard.call_args.kwargs["require_enabled"])
        self.assertIn(receipt["invocation_id"], self.direct.rows)
        self.assertEqual([], self.repository.state.acceptance_lock_trace)

    def test_exact_request_replay_is_not_blocked_by_its_active_run(self):
        first = self.service.invoke_console(AUTOMATION_ID, request_id="same-request", actor=_admin())
        self.contract = replace(self.contract, contract_hash="e" * 64, project_configuration_version=2)
        call = self.direct.call
        replay = self.service.invoke_console(AUTOMATION_ID, request_id="same-request", actor=_admin())
        self.assertEqual(first["invocation_id"], replay["invocation_id"])
        self.assertIs(call, self.direct.call)
        self.assertEqual(1, len(self.direct.rows))

    def test_same_idempotency_key_with_different_request_still_conflicts(self):
        key = "automation-replay-collision"
        self.service.invoke_trusted(
            AUTOMATION_ID,
            entrypoint="console",
            request_id="request-original",
            actor=_admin(),
            idempotency_key=key,
        )

        with self.assertRaises(OrchestrationError) as raised:
            self.service.invoke_trusted(
                AUTOMATION_ID,
                entrypoint="console",
                request_id="request-different",
                actor=_admin(),
                idempotency_key=key,
            )

        self.assertEqual("REQUEST_ID_REUSED", raised.exception.code)

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

        self.assertIn("invocation_id", receipt)
        self.assertEqual(
            [
                {
                    "automation_id": AUTOMATION_ID,
                    "generation": 1,
                    "contribution_kind": "console",
                    "contribution_id": "run_now",
                }
            ] * 2,
            registry.calls,
        )

    def test_service_v2_selection_preview_is_host_owned_and_read_phase_bound(self):
        self._set_service_v2_selection_contract()
        service = self._service_with_contribution_registry(_ContributionRegistry())
        receipt = service.invoke_selection_preview(AUTOMATION_ID, request_id="selection-preview", actor=_admin())
        self.assertIn("invocation_id", receipt)
        self.assertEqual("execute_console", self.direct.call.invocation.contract_id)
        self.assertEqual({"mode": "saved", "dry_run": True, "selected_bill_codes": [], "preview_fingerprint": ""}, self.direct.call.arguments)
        # A direct manual trigger also starts a read-only preview; it never
        # invents a formal selection or writes without a completed preview.
        service.invoke_console(AUTOMATION_ID, request_id="selection-direct", actor=_admin(), contribution_id="execute_console")
        self.assertTrue(self.direct.call.arguments["dry_run"])

    def test_service_v2_selection_confirmation_replays_inside_and_after_ttl(self):
        self._set_service_v2_selection_contract()
        service = self._service_with_contribution_registry(_ContributionRegistry())
        for age in (5, 20):
            with self.subTest(age=age):
                preview_id = self._completed_selection_preview(service, "preview-" + str(age))
                request_id = "confirmation-" + str(age)
                receipt = service.confirm_selection_preview(AUTOMATION_ID, preview_invocation_id=preview_id, selected_bill_codes=["R0001"], request_id=request_id, actor=_admin())
                self.direct.rows[preview_id]["result_json"]["meta"]["observed_at"] = (datetime.now(timezone.utc)-timedelta(minutes=age)).isoformat()
                with patch.object(service, "_load_contract", side_effect=AssertionError("exact replay does not recompile current generation")):
                    replay = service.confirm_selection_preview(AUTOMATION_ID, preview_invocation_id=preview_id, selected_bill_codes=["R0001"], request_id=request_id, actor=_admin())
                self.assertEqual(receipt["invocation_id"], replay["invocation_id"])
                with self.assertRaises(OrchestrationError) as conflict:
                    service.confirm_selection_preview(AUTOMATION_ID, preview_invocation_id=preview_id, selected_bill_codes=["R0002"], request_id=request_id, actor=_admin())
                self.assertEqual("IDEMPOTENCY_CONFLICT", conflict.exception.code)

    def test_service_v2_selection_guard_replays_race_before_live_checks(self):
        self._set_service_v2_selection_contract()
        service = self._service_with_contribution_registry(_ContributionRegistry())
        preview_id = self._completed_selection_preview(service)
        first = service.confirm_selection_preview(AUTOMATION_ID, preview_invocation_id=preview_id, selected_bill_codes=["R0001"], request_id="confirmation", actor=_admin())
        original = self.direct.by_request
        lookups = []
        def racing_lookup(key):
            lookups.append(key)
            return None if len(lookups) == 1 else original(key)
        with patch.object(self.direct, "by_request", side_effect=racing_lookup), patch.object(service, "_lock_and_compile_contract", side_effect=AssertionError("accepted race cannot dispatch again")):
            replay = service.confirm_selection_preview(AUTOMATION_ID, preview_invocation_id=preview_id, selected_bill_codes=["R0001"], request_id="confirmation", actor=_admin())
        self.assertEqual(first["invocation_id"], replay["invocation_id"])
        self.assertEqual(2, len(lookups))

    def test_service_v2_selection_confirmation_pins_context_and_consumes_once(self):
        self._set_service_v2_selection_contract()
        service = self._service_with_contribution_registry(_ContributionRegistry())
        preview_id = self._completed_selection_preview(service)
        receipt = service.confirm_selection_preview(AUTOMATION_ID, preview_invocation_id=preview_id, selected_bill_codes=["R0001"], request_id="first-confirmation", actor=_admin())
        self.assertEqual(preview_id, self.direct.call.preview_invocation_id)
        self.assertEqual({"mode": "saved", "dry_run": False, "selected_bill_codes": ["R0001"], "preview_fingerprint": "f" * 64}, self.direct.call.arguments)
        self.assertEqual(receipt["invocation_id"], self.direct.rows[preview_id]["preview_consumed_by"])
        with self.assertRaises(OrchestrationError) as consumed:
            service.confirm_selection_preview(AUTOMATION_ID, preview_invocation_id=preview_id, selected_bill_codes=["R0001"], request_id="second-confirmation", actor=_admin())
        self.assertEqual("SELECTION_PREVIEW_ALREADY_CONSUMED", consumed.exception.code)

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
                trusted_context={"event_name": "shipment.created", "source_event_id": "event-one"},
                expected_automation_generation=1,
            )

        self.assertEqual("PROJECT_ENTRYPOINT_DISABLED", raised.exception.code)
        self.assertEqual([], registry.calls)

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

    def test_trusted_wait_notifies_only_after_invocation_acceptance(self):
        events = []
        original_wait = self.direct.wait
        async def on_accepted(receipt):
            events.append(("accepted", receipt["invocation_id"]))
        async def wait(identity, *, timeout_seconds):
            self.assertEqual([("accepted", identity)], events)
            events.append(("wait", identity))
            return await original_wait(identity, timeout_seconds=timeout_seconds)
        self.direct.wait = wait
        result = asyncio.run(self.service.invoke_trusted_and_wait(AUTOMATION_ID, entrypoint="console", request_id="callback", actor=_admin(), on_accepted=on_accepted))
        self.assertEqual([("accepted", result["invocation_id"]), ("wait", result["invocation_id"])], events)
        self.assertTrue(result["success"])

    def test_trusted_wait_admits_off_the_event_loop_thread(self):
        # Scheduler, Feishu and Webhook admission performs synchronous
        # cross-region database work; it must not block every HTTP request.
        receipt = {"invocation_id": "threaded-invocation", "status": "COMPLETED", "error_code": None, "output": {}}
        admission_threads = []
        def invoke_trusted(*_args, **_kwargs):
            admission_threads.append(threading.get_ident())
            return receipt
        async def wait(identity, *, timeout_seconds):
            return receipt
        async def run():
            loop_thread = threading.get_ident()
            result = await self.service.invoke_trusted_and_wait(
                AUTOMATION_ID, entrypoint="feishu", request_id="threaded-admission", actor=_admin())
            return loop_thread, result
        with patch.object(self.service, "invoke_trusted", side_effect=invoke_trusted), patch.object(self.direct, "wait", side_effect=wait):
            loop_thread, result = asyncio.run(run())
        self.assertEqual(receipt, result)
        self.assertEqual(1, len(admission_threads))
        self.assertNotEqual(loop_thread, admission_threads[0])

    def test_trusted_wait_does_not_announce_failed_admission_or_completed_replay(self):
        for status, error in (("FAILED", "PLUGIN_RELEASE_HELD"), ("FAILED", "EXECUTION_RESOURCE_BUSY"), ("COMPLETED", None), ("CANCELLED", "CANCELLED"), ("WRITE_OUTCOME_UNKNOWN", "WRITE_OUTCOME_UNKNOWN")):
            with self.subTest(status=status, error=error):
                events = []
                receipt = {"invocation_id": "terminal-invocation", "status": status,
                           "error_code": error, "output": {}}
                async def on_accepted(value):
                    events.append(value)
                async def wait(identity, *, timeout_seconds):
                    self.assertEqual(receipt["invocation_id"], identity)
                    return receipt
                with patch.object(self.service, "invoke_trusted", return_value=receipt), patch.object(self.direct, "wait", side_effect=wait):
                    result = asyncio.run(self.service.invoke_trusted_and_wait(AUTOMATION_ID, entrypoint="feishu", request_id="terminal-callback", actor=_admin(), on_accepted=on_accepted))
                self.assertEqual(receipt, result)
                self.assertEqual([], events)

    def test_release_hold_blocks_project_writes_and_typed_invoke(self):
        service = AutomationProjectPolicyService(
            self.repository,
            plugin_catalog=_Catalog(self.entry),
            release_hold_provider=lambda: True,
        )
        with self.assertRaises(OrchestrationError) as raised:
            service.invoke_console(
                AUTOMATION_ID,
                request_id="request-held",
                actor=_admin(),
            )
        self.assertEqual("RELEASE_HELD", raised.exception.code)

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

    def test_scheduler_invocation_binds_exact_row_generation_and_context(self):
        self.repository.state.policy["mode"] = "PROJECT_FULL_AUTO"
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

        self.assertIn("invocation_id", receipt)
        call = self.direct.call
        self.assertEqual("scheduler", call.source)
        self.assertEqual(
            "scheduler:schedule-one",
            call.invocation.contract_id,
        )
        self.assertEqual(
            "schedule-one",
            call.actor_id,
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

        self.assertIn("invocation_id", receipt)
        self.assertEqual(
            [
                {
                    "automation_id": AUTOMATION_ID,
                    "generation": 1,
                    "contribution_kind": "scheduler",
                    "contribution_id": "daily_run",
                }
            ] * 2,
            registry.calls,
        )

    def test_trusted_wait_preserves_terminal_error_for_scheduler_status(self):
        self.repository.state.policy["mode"] = "PROJECT_FULL_AUTO"
        self.contract = _contract_for(AutomationEntrypoint.SCHEDULER)
        self.direct.result = {
            "run_id": "run-invoke",
            "command_id": "command-failed",
            "work_item_id": "work-invoke",
            "status": "FAILED",
            "success": False,
            "invocation_id": "11111111-1111-4111-8111-111111111111",
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

    def test_webhook_dynamic_values_come_only_from_verified_route_context(self):
        self.repository.state.policy["mode"] = "PROJECT_FULL_AUTO"
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

        call = self.direct.call
        self.assertEqual(
            {"mode": "saved", "BILL_CODE": "10001"},
            call.arguments,
        )
        self.assertNotIn("account_id", call.arguments)

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

if __name__ == "__main__":
    import unittest

    unittest.main()
