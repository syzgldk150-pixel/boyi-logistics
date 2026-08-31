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


def _admin() -> Actor:
    return Actor(
        actor_type=ActorType.CONSOLE_ADMIN,
        actor_id="admin-one",
        roles=("admin", "super_admin"),
        display_name="Administrator",
        authenticated_by="mysql_admin_session",
    )


def _contract() -> CompiledAutomationProjectContract:
    return CompiledAutomationProjectContract(
        automation_id=AUTOMATION_ID,
        automation_generation=1,
        manifest_sha256=MANIFEST_HASH,
        tool_name=f"automation.{AUTOMATION_ID}.run",
        tool_version="1.0.0",
        operation_type=OperationType.EXTERNAL_WRITE.value,
        risk_level=RiskLevel.HIGH.value,
        invocation_contracts={
            "console": InvocationArgumentContract(
                contract_id="console",
                entrypoint="console",
                expected_arguments={"mode": "saved"},
                dynamic_argument_resolvers={},
            )
        },
        account_bindings={"primary": "account-one"},
        allowed_entrypoints=frozenset({"console"}),
        contract_hash=CONTRACT_HASH,
        tool_contract_hash=TOOL_HASH,
        plugin_contract_hash=PLUGIN_HASH,
        project_configuration_version=1,
        snapshot={"automation_id": AUTOMATION_ID},
        can_full_auto=True,
    )


def _contract_for(
    entrypoint: AutomationEntrypoint,
    *,
    dynamic_resolvers: dict[str, str] | None = None,
) -> CompiledAutomationProjectContract:
    source = entrypoint.value
    contract_id = "scheduler:schedule-one" if entrypoint is AutomationEntrypoint.SCHEDULER else source
    return replace(
        _contract(),
        invocation_contracts={
            contract_id: InvocationArgumentContract(
                contract_id=contract_id,
                entrypoint=source,
                expected_arguments={"mode": "saved"},
                dynamic_argument_resolvers=dynamic_resolvers or {},
            )
        },
        allowed_entrypoints=frozenset({source}),
    )


def _invocation(*, request_id: str = "invoke-one") -> AutomationProjectInvocation:
    return AutomationProjectInvocation(
        automation_id=AUTOMATION_ID,
        automation_generation=1,
        entrypoint=AutomationEntrypoint.CONSOLE,
        contract_id="console",
        contract_hash=CONTRACT_HASH,
        policy_version=1,
        project_configuration_version=1,
        request_id=request_id,
    )


def _plan(invocation: AutomationProjectInvocation) -> Plan:
    return Plan(
        command_type="automation.project.invoke",
        context_fingerprint="context-one",
        tool_catalog_hash="catalog-one",
        steps=(
            PlanStep(
                step_key="execute",
                tool_name=f"automation.{AUTOMATION_ID}.run",
                tool_version="1.0.0",
                operation_type=OperationType.EXTERNAL_WRITE,
                arguments={"mode": "saved"},
                account_id=None,
                depends_on=(),
                idempotency_key="step-one",
                expected_evidence=(),
                postconditions=(),
                risk_level=RiskLevel.HIGH,
                requires_approval=True,
            ),
        ),
        automation_id=AUTOMATION_ID,
        automation_generation=invocation.automation_generation,
        automation_contract_hash=invocation.contract_hash,
    )


def _pending(approval_id: str, invocation: AutomationProjectInvocation) -> dict:
    plan = _plan(invocation)
    return {
        "approval_id": approval_id,
        "work_item_id": f"work-{approval_id}",
        "run_id": f"run-{approval_id}",
        "approval_round": 1,
        "plan_hash": plan.plan_hash,
        "current_plan_hash": plan.plan_hash,
        "risk_level": "HIGH",
        "required_role": "super_admin",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc),
        "source": "console",
        "run_status": "WAITING_APPROVAL",
        "plan_json": plan.to_dict(),
        "parameters_json": {
            "tool_name": f"automation.{AUTOMATION_ID}.run",
            "arguments": {"mode": "saved"},
            "execution_context": {},
        },
        "automation_invocation_json": invocation.to_dict(),
        "correlation_id": f"correlation-{approval_id}",
        "causation_id": None,
    }


class _State:
    def __init__(self) -> None:
        self.project = {
            "automation_id": AUTOMATION_ID,
            "enabled": True,
            "state": "ENABLED",
            "target_generation": 1,
            "committed_generation": 1,
            "reconcile_state": "STABLE",
        }
        self.config = {"automation_id": AUTOMATION_ID, "config_version": 1}
        self.policy = {
            "automation_id": AUTOMATION_ID,
            "mode": "REQUIRE_EACH_RUN",
            "version": 1,
            "project_generation": 1,
            "project_configuration_version": 1,
            "approved_by_actor_id": None,
            "approved_by_actor_role": None,
        }
        self.pending: list[dict] = []
        self.decisions: list[dict] = []
        self.batches: dict[tuple[str, str], dict] = {}
        self.policy_events: list[dict] = []
        self.domain_events: list[dict] = []
        self.commands_by_idempotency: dict[tuple[str, str], dict] = {}
        self.fail_decision_at: int | None = None


class _AutomationProjects:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    @property
    def _state(self) -> _State:
        return self._repository.state

    def list_policies(self):
        return [dict(self._state.policy)]

    def get_policy(self, automation_id, **_kwargs):
        del automation_id
        return dict(self._state.policy)

    def get_event_by_request(self, automation_id, request_id, **_kwargs):
        return next(
            (
                dict(row)
                for row in self._state.policy_events
                if row["automation_id"] == automation_id
                and row["request_id"] == request_id
            ),
            None,
        )

    def list_policy_events(self, automation_id, **_kwargs):
        return [
            copy.deepcopy(row)
            for row in self._state.policy_events
            if row.get("automation_id") == automation_id
        ]

    def update_policy(self, automation_id, *, expected_version, **values):
        if automation_id != AUTOMATION_ID or self._state.policy["version"] != expected_version:
            raise AssertionError("policy CAS mismatch")
        self._state.policy.update(values)
        self._state.policy["version"] += 1
        self._state.policy["updated_at"] = datetime.now(timezone.utc)
        self._state.policy["approved_by_actor_id"] = values.get("actor_id")
        return dict(self._state.policy)

    def append_event(self, row):
        self._state.policy_events.append(dict(row))
        return dict(row)

    def list_configuration_rows(self, _automation_id, **_kwargs):
        return []

    def expire_pending_approvals(self, _automation_id):
        return 0

    def invalidate_pending_approvals_and_wake_runs(
        self,
        automation_id,
        *,
        event_repository=None,
    ):
        del event_repository
        if automation_id != AUTOMATION_ID:
            return ()
        run_ids = tuple(
            str(row["run_id"])
            for row in self._state.pending
            if row.get("run_status", "WAITING_APPROVAL")
            == "WAITING_APPROVAL"
        )
        self._state.pending = []
        self._repository.runnable_run_ids.extend(run_ids)
        return run_ids

    def lock_waiting_approval_runs(self, automation_id):
        if automation_id != AUTOMATION_ID:
            return ()
        return tuple(
            str(row["run_id"])
            for row in self._state.pending
            if row.get("run_status", "WAITING_APPROVAL")
            == "WAITING_APPROVAL"
        )

    def list_pending_approvals(self, automation_id, **_kwargs):
        if automation_id != AUTOMATION_ID:
            return []
        return [copy.deepcopy(row) for row in self._state.pending]

    def get_batch_by_request(self, automation_id, request_id, **_kwargs):
        row = self._state.batches.get((automation_id, request_id))
        return copy.deepcopy(row) if row is not None else None

    def create_batch(self, row):
        key = (row["automation_id"], row["request_id"])
        self._state.batches[key] = copy.deepcopy(dict(row))
        return copy.deepcopy(dict(row))


class _AutomationPlugins:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def get_project(self, automation_id, **_kwargs):
        return (
            dict(self._repository.state.project)
            if automation_id == AUTOMATION_ID
            else None
        )

    def get_project_config(self, automation_id, **_kwargs):
        return (
            dict(self._repository.state.config)
            if automation_id == AUTOMATION_ID
            else None
        )


class _Approvals:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def record_decision(self, row, *, expected_plan_hash):
        state = self._repository.state
        if state.fail_decision_at is not None and len(state.decisions) + 1 == state.fail_decision_at:
            raise InvalidStateError("approval changed")
        target = next(
            item
            for item in state.pending
            if item["approval_id"] == row["approval_id"]
        )
        if target["plan_hash"] != expected_plan_hash:
            return {"_decision_error": "PLAN_CHANGED"}
        state.pending.remove(target)
        state.decisions.append(dict(row))
        return dict(row)


class _Events:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def append_with_outbox(self, event, _outbox):
        self._repository.state.domain_events.append(copy.deepcopy(dict(event)))


class _Runs:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def make_waiting_approval_runnable(self, run_id):
        self._repository.runnable_run_ids.append(str(run_id))
        return {"run_id": str(run_id), "status": "WAITING_APPROVAL"}


class _Commands:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def get_by_idempotency(self, source, idempotency_key, *, for_update=False):
        del for_update
        row = self._repository.state.commands_by_idempotency.get(
            (source, idempotency_key)
        )
        return copy.deepcopy(row) if row is not None else None


class _Uow:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository
        self.automation_projects = _AutomationProjects(repository)
        self.automation_plugins = _AutomationPlugins(repository)
        self.approvals = _Approvals(repository)
        self.events = _Events(repository)
        self.runs = _Runs(repository)
        self.commands = _Commands(repository)
        self.scheduled_policies = SimpleNamespace()
        self._snapshot: _State | None = None

    def __enter__(self):
        self._snapshot = copy.deepcopy(self._repository.state)
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is not None and self._snapshot is not None:
            self._repository.state = self._snapshot
        return False

    def commit(self):
        self._repository.account_lock_events.append(("commit",))
        return None


class _Repository:
    def __init__(self) -> None:
        self.state = _State()
        self.account_lock_events: list[tuple] = []
        self.block_account_locks = False
        self.runnable_run_ids: list[str] = []

    def unit_of_work(self):
        return _Uow(self)

    def acquire_account_execution_locks(self, account_ids, *, timeout_seconds=0):
        if timeout_seconds != 0:
            raise AssertionError("project policy account lock must not wait")
        normalized = tuple(sorted(account_ids))
        self.account_lock_events.append(("acquire", normalized))
        if self.block_account_locks:
            raise AccountExecutionLockUnavailable("synthetic credential change")
        repository = self

        class _Lease:
            def release(self):
                repository.account_lock_events.append(("release", normalized))

        return _Lease()


class _Catalog:
    def __init__(self, entry) -> None:
        self._entry = entry

    def require(self, automation_id):
        if automation_id != self._entry.automation_id:
            raise KeyError(automation_id)
        return self._entry

    def get(self, automation_id):
        return self._entry if automation_id == self._entry.automation_id else None

    def list(self):
        return (self._entry,)


class _Gateway:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository
        self.command = None
        self.run_result = None

    def submit(self, command, *, uow_guard=None):
        with self.repository.unit_of_work() as uow:
            if uow_guard is not None:
                uow_guard(uow)
            uow.commit()
        self.command = command
        return CommandReceipt(
            command_id=command.command_id,
            work_item_id="work-invoke",
            run_id="run-invoke",
            status=RunStatus.RECEIVED,
            reused=False,
        )

    async def wait_for_run(self, run_id, *, timeout_seconds):
        self.waited = (run_id, timeout_seconds)
        if self.run_result is not None:
            return dict(self.run_result)
        return {
            "run_id": run_id,
            "command_id": self.command.command_id,
            "work_item_id": "work-invoke",
            "status": "COMPLETED",
            "correlation_id": self.command.correlation_id,
        }


class _ContributionRegistry:
    def __init__(
        self,
        *,
        error_code: str | None = None,
        event_name: str = "shipment.created",
        durable: bool = False,
    ) -> None:
        self.error_code = error_code
        self.event_name = event_name
        self.durable = durable
        self.calls: list[dict[str, object]] = []

    def resolve_active(
        self,
        *,
        automation_id: str,
        generation: int,
        contribution_kind: str,
        contribution_id: str,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "automation_id": automation_id,
                "generation": generation,
                "contribution_kind": contribution_kind,
                "contribution_id": contribution_id,
            }
        )
        if self.error_code is not None:
            raise PluginConflictError(
                "synthetic runtime projection rejection",
                code=self.error_code,
            )
        return SimpleNamespace(
            automation_id=automation_id,
            generation=generation,
            contribution_kind=contribution_kind,
            contribution_id=contribution_id,
            phase="COMMITTED",
            backend_status="READY",
            declaration={"event": self.event_name, "durable": self.durable},
        )


class _ModuleSlotRegistry:
    def __init__(self, *, fail_after_first: bool = False) -> None:
        self.fail_after_first = fail_after_first
        self.calls: list[dict[str, str]] = []
        self.declaration = {
            "id": "validate_waybill",
            "slot": "waybill_entry.validators",
            "title": "Validate waybill",
            "service": "plugin.waybill.validator@1",
            "operation": "validate",
            "default_enabled": True,
        }

    def resolve_active_module_slot(self, *, slot: str, handle: str) -> SimpleNamespace:
        self.calls.append({"slot": slot, "handle": handle})
        if self.fail_after_first and len(self.calls) > 1:
            raise PluginConflictError("synthetic generation switch", code="RUNTIME_PROJECTION_STALE")
        return SimpleNamespace(
            automation_id=AUTOMATION_ID,
            generation=1,
            contribution_id="validate_waybill",
            contribution_kind="module_slots",
            slot=slot,
            handle=handle,
            declaration_sha256=hashlib.sha256(canonical_json_bytes(self.declaration)).hexdigest(),
            service="plugin.waybill.validator@1",
            operation="validate",
            declaration=dict(self.declaration),
        )


class AutomationProjectPolicyServiceTests(TestCase):
    def setUp(self) -> None:
        self.repository = _Repository()
        self.contract = _contract()
        self.entry = SimpleNamespace(automation_id=AUTOMATION_ID)
        self.gateway = _Gateway(self.repository)
        self.woken_run_ids: list[str] = []
        self.service = AutomationProjectPolicyService(
            self.repository,
            core_catalog=SimpleNamespace(),
            plugin_catalog=_Catalog(self.entry),
            command_gateway=self.gateway,
            wake_runner=self.woken_run_ids.append,
        )
        self.service._load_contract = lambda _automation_id: (  # type: ignore[method-assign]
            self.entry,
            self.contract,
        )
        self.service._lock_and_compile_contract = (  # type: ignore[method-assign]
            lambda _uow, _entry, **_kwargs: (
                self.contract,
                dict(self.repository.state.config),
            )
        )

    def _service_with_contribution_registry(
        self,
        registry: _ContributionRegistry,
    ) -> AutomationProjectPolicyService:
        service = AutomationProjectPolicyService(
            self.repository,
            core_catalog=SimpleNamespace(),
            plugin_catalog=_Catalog(self.entry),
            command_gateway=self.gateway,
            wake_runner=self.woken_run_ids.append,
            contribution_registry=registry,
        )
        service._load_contract = lambda _automation_id: (  # type: ignore[method-assign]
            self.entry,
            self.contract,
        )
        service._lock_and_compile_contract = (  # type: ignore[method-assign]
            lambda _uow, _entry, **_kwargs: (
                self.contract,
                dict(self.repository.state.config),
            )
        )
        return service

    def _set_service_v2_console_contract(self) -> None:
        self.entry.runtime_model = "SERVICE_V2"
        self.contract = replace(
            self.contract,
            invocation_contracts={
                "run_now": InvocationArgumentContract(
                    contract_id="run_now",
                    entrypoint="console",
                    expected_arguments={"mode": "saved"},
                    dynamic_argument_resolvers={},
                    contribution_id="run_now",
                )
            },
            allowed_entrypoints=frozenset({"console"}),
        )

    def _set_service_v2_selection_contract(self) -> None:
        self.entry.runtime_model = "SERVICE_V2"
        self.entry.plugin_id = "selection_v2"
        self.entry.trust_source = "ed25519_first_party"
        self.entry.display_name = "Selection v2"
        self.entry.contributions = {
            "console": [
                {
                    "id": "execute_console",
                    "title": "Preview candidates",
                    "service": "plugin.selection@1",
                    "operation": "execute",
                    "selection_preview_operation": "preview",
                    "default_enabled": False,
                }
            ],
            "feishu": [],
        }
        self.contract = replace(
            self.contract,
            invocation_contracts={
                "execute_console": InvocationArgumentContract(
                    contract_id="execute_console",
                    entrypoint="console",
                    expected_arguments={"mode": "saved"},
                    dynamic_argument_resolvers={},
                    contribution_id="execute_console",
                )
            },
            allowed_entrypoints=frozenset({"console"}),
        )

    def _persisted_selection_confirmation(
        self,
        *,
        preview_run_id: str,
        request_id: str,
        observed_at: datetime,
    ) -> tuple[str, dict]:
        actor = _admin()
        idempotency_key = (
            f"automation:{AUTOMATION_ID}:console:{actor.actor_id}:"
            f"selection:{preview_run_id}:{request_id}"
        )
        selected = ["R0001"]
        formal_arguments = {
            "mode": "saved",
            "dry_run": False,
            "selected_bill_codes": selected,
            "preview_fingerprint": "f" * 64,
        }
        observed_text = observed_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        expires_text = (
            observed_at.astimezone(timezone.utc) + timedelta(minutes=15)
        ).isoformat().replace("+00:00", "Z")
        selection_context = {
            "contract_version": 1,
            "plugin_id": "selection_v2",
            "preview_run_id": preview_run_id,
            "preview_step_id": "preview-step",
            "preview_result_sha256": "b" * 64,
            "project_instance_id": AUTOMATION_ID,
            "generation": 1,
            "contract_digest": CONTRACT_HASH,
            "configuration_version": 1,
            "entrypoint": "console",
            "contribution_id": "execute_console",
            "preview_fingerprint": "f" * 64,
            "candidate_count": 1,
            "candidates_sha256": "c" * 64,
            "selection_count": 1,
            "selection_sha256": canonical_sha256(selected),
            "formal_arguments_sha256": canonical_sha256(formal_arguments),
            "observed_at": observed_text,
            "expires_at": expires_text,
        }
        selection_context["context_sha256"] = canonical_sha256(selection_context)
        invocation = AutomationProjectInvocation(
            automation_id=AUTOMATION_ID,
            automation_generation=1,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="execute_console",
            contract_hash=CONTRACT_HASH,
            policy_version=1,
            project_configuration_version=1,
            request_id=request_id,
        )
        return idempotency_key, {
            "command_id": f"command-{request_id}",
            "command_type": "automation.project.invoke",
            "source": "console",
            "actor_type": actor.actor_type.value,
            "actor_id": actor.actor_id,
            "actor_roles_json": list(actor.roles),
            "entity_refs_json": [
                {
                    "entity_type": "automation_project",
                    "entity_id": AUTOMATION_ID,
                    "source_system": "agent",
                    "relation_type": "subject",
                    "metadata": {},
                }
            ],
            "parameters_json": {
                "tool_name": f"automation.{AUTOMATION_ID}.run",
                "arguments": formal_arguments,
                "execution_context": {
                    "project_request_id": request_id,
                    "entrypoint": "console",
                    "occurred_at": observed_text,
                    "contribution_id": "execute_console",
                    "selection_phase": "FORMAL",
                    "selection_preview": selection_context,
                },
            },
            "automation_invocation_json": invocation.to_dict(),
            "idempotency_key": idempotency_key,
            "correlation_id": f"correlation-{request_id}",
            "requested_at": observed_at,
        }

    def _set_service_v2_feishu_contract(self) -> None:
        self.entry.runtime_model = "SERVICE_V2"
        self.contract = replace(
            self.contract,
            invocation_contracts={
                "lookup_command": InvocationArgumentContract(
                    contract_id="lookup_command",
                    entrypoint="feishu",
                    expected_arguments={"mode": "saved"},
                    dynamic_argument_resolvers={},
                    contribution_id="lookup_command",
                )
            },
            allowed_entrypoints=frozenset({"feishu"}),
        )

    def _set_service_v2_webhook_contract(self) -> None:
        self.entry.runtime_model = "SERVICE_V2"
        self.contract = replace(
            self.contract,
            invocation_contracts={
                "receive_status": InvocationArgumentContract(
                    contract_id="receive_status",
                    entrypoint="webhook",
                    expected_arguments={"mode": "saved"},
                    dynamic_argument_resolvers={},
                    contribution_id="receive_status",
                )
            },
            allowed_entrypoints=frozenset({"webhook"}),
        )

    def _set_service_v2_event_contract(self) -> None:
        self.entry.runtime_model = "SERVICE_V2"
        self.contract = replace(
            self.contract,
            invocation_contracts={
                "handle_created": InvocationArgumentContract(
                    contract_id="handle_created",
                    entrypoint="events",
                    expected_arguments={"mode": "saved"},
                    dynamic_argument_resolvers={},
                    contribution_id="handle_created",
                )
            },
            allowed_entrypoints=frozenset({"events"}),
        )

    def _set_service_v2_module_slot_contract(self) -> None:
        self.entry.runtime_model = "SERVICE_V2"
        self.entry.invocation_contracts = {
            "validate_waybill": {
                "contribution_kind": "module_slots",
                "service": "plugin.waybill.validator@1",
                "operation": "validate",
                "dynamic_resolvers": {"waybill": WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID},
            }
        }
        self.contract = replace(
            self.contract,
            invocation_contracts={
                "validate_waybill": InvocationArgumentContract(
                    contract_id="validate_waybill",
                    entrypoint="module_slots",
                    expected_arguments={"mode": "saved"},
                    dynamic_argument_resolvers={"waybill": WAYBILL_ENTRY_DYNAMIC_RESOLVER_ID},
                    contribution_id="validate_waybill",
                )
            },
            allowed_entrypoints=frozenset({"module_slots"}),
        )

    @staticmethod
    def _verified_feishu_actor() -> Actor:
        return Actor(
            ActorType.FEISHU_USER,
            "sender-one",
            authenticated_by="feishu_verified_event",
        )

    @staticmethod
    def _verified_webhook_actor() -> Actor:
        return Actor(
            ActorType.WEBHOOK,
            "webhook:route-owner-digest",
            authenticated_by="signed_webhook_route",
        )

    @staticmethod
    def _verified_event_actor() -> Actor:
        return Actor(
            ActorType.EVENT,
            "event:owner-digest",
            authenticated_by="managed_event_dispatcher",
        )

    def _set_scan_project(self) -> None:
        self.entry.automation_id = "scan_codes"
        self.entry.plugin_id = "sync_scan_codes"
        self.entry.trust_source = "ed25519_first_party"
        self.entry.project_full_auto_allowed = True
        self.entry.governance_anchor = {
            "operation_type": "internal_projection_write",
            "risk_level": "medium",
            "approval": {"required_role": "admin"},
            "permissions": {"required_roles": ["admin"]},
            "project_full_auto_allowed": True,
        }

    def _set_selection_project(
        self,
        automation_id: str = "split_pending_problem_upload",
    ) -> None:
        self.entry.automation_id = automation_id
        self.entry.plugin_id = automation_id
        self.entry.trust_source = "ed25519_first_party"
        self.service._dynamic_resolver = (  # type: ignore[method-assign]
            lambda _resolver_id, field, context: context["dynamic_inputs"][field]
        )
        self.contract = replace(
            _contract(),
            automation_id=automation_id,
            tool_name=f"automation.{automation_id}.run",
            invocation_contracts={
                "console": InvocationArgumentContract(
                    contract_id="console",
                    entrypoint="console",
                    expected_arguments={},
                    dynamic_argument_resolvers={
                        "dry_run": "verified_console_dry_run",
                        "selected_bill_codes": "verified_console_selected_bill_codes",
                        "preview_fingerprint": "verified_console_preview_fingerprint",
                    },
                )
            },
        )

    def _scan_policy_subject(self, *, dry_run: bool):
        self._set_scan_project()
        console_contract = _contract().invocation_contracts["console"]
        scan_invocation_contracts = {
            source: replace(
                console_contract,
                contract_id=source,
                entrypoint=source,
            )
            for source in ("console", "feishu", "webhook")
        }
        self.contract = replace(
            _contract(),
            automation_id="scan_codes",
            tool_name="automation.scan_codes.run",
            operation_type=OperationType.INTERNAL_PROJECTION_WRITE.value,
            risk_level=RiskLevel.MEDIUM.value,
            code_owned_plan_fields=frozenset(
                {"dry_run", "_scan_preview_binding"}
            ),
            invocation_contracts={
                source: replace(
                    contract,
                    input_schema={
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string"},
                            "dry_run": {"type": "boolean"},
                            "_scan_preview_binding": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    },
                )
                for source, contract in scan_invocation_contracts.items()
            },
            allowed_entrypoints=frozenset({"console", "feishu", "webhook"}),
        )
        invocation = AutomationProjectInvocation(
            automation_id="scan_codes",
            automation_generation=1,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="console",
            contract_hash=CONTRACT_HASH,
            policy_version=1,
            project_configuration_version=1,
            request_id="scan-policy",
        )
        arguments = {"mode": "saved", "dry_run": dry_run}
        if not dry_run:
            arguments["_scan_preview_binding"] = {"context_sha256": "a" * 64}
        plan = Plan(
            command_type="automation.project.invoke",
            context_fingerprint="context-one",
            tool_catalog_hash="catalog-one",
            steps=(
                PlanStep(
                    step_key="execute",
                    tool_name="automation.scan_codes.run",
                    tool_version="1.0.0",
                    operation_type=(
                        OperationType.READ
                        if dry_run
                        else OperationType.INTERNAL_PROJECTION_WRITE
                    ),
                    arguments=arguments,
                    account_id=None,
                    depends_on=(),
                    idempotency_key="scan-step",
                    expected_evidence=(),
                    postconditions=(),
                    risk_level=RiskLevel.LOW if dry_run else RiskLevel.MEDIUM,
                    requires_approval=not dry_run,
                ),
            ),
            automation_id="scan_codes",
            automation_generation=1,
            automation_contract_hash=CONTRACT_HASH,
        )
        return plan, invocation

    def _selection_policy_subject(self, *, dry_run: bool):
        self._set_selection_project()
        invocation = AutomationProjectInvocation(
            automation_id="split_pending_problem_upload",
            automation_generation=1,
            entrypoint=AutomationEntrypoint.CONSOLE,
            contract_id="console",
            contract_hash=CONTRACT_HASH,
            policy_version=1,
            project_configuration_version=1,
            request_id="selection-policy",
        )
        arguments = {
            "dry_run": dry_run,
            "selected_bill_codes": [] if dry_run else ["R1"],
            "preview_fingerprint": "" if dry_run else "a" * 64,
        }
        plan = Plan(
            command_type="automation.project.invoke",
            context_fingerprint="context-one",
            tool_catalog_hash="catalog-one",
            steps=(
                PlanStep(
                    step_key="execute",
                    tool_name="automation.split_pending_problem_upload.run",
                    tool_version="1.0.0",
                    operation_type=(
                        OperationType.READ if dry_run else OperationType.EXTERNAL_WRITE
                    ),
                    arguments=arguments,
                    account_id=None,
                    depends_on=(),
                    idempotency_key="selection-step",
                    expected_evidence=(),
                    postconditions=(),
                    risk_level=RiskLevel.LOW if dry_run else RiskLevel.HIGH,
                    requires_approval=not dry_run,
                ),
            ),
            automation_id="split_pending_problem_upload",
            automation_generation=1,
            automation_contract_hash=CONTRACT_HASH,
        )
        return plan, invocation

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
