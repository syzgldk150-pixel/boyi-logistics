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


__all__ = [
    "AUTOMATION_ID",
    "CONTRACT_HASH",
    "TOOL_HASH",
    "PLUGIN_HASH",
    "MANIFEST_HASH",
    "_admin",
    "_contract",
    "_contract_for",
    "_invocation",
    "_plan",
    "_pending",
    "_State",
    "_AutomationProjects",
    "_AutomationPlugins",
    "_Approvals",
    "_Events",
    "_Runs",
    "_Commands",
    "_Uow",
    "_Repository",
    "_Catalog",
    "_Gateway",
    "_ContributionRegistry",
    "_ModuleSlotRegistry",
    "AutomationProjectPolicyServiceTestBase",
]


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


class AutomationProjectPolicyServiceTestBase(TestCase):
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
