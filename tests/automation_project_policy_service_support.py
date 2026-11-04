from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from uuid import uuid4
from unittest.mock import patch

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    OperationType,
    RiskLevel,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    CompiledAutomationProjectContract,
    InvocationArgumentContract,
    canonical_sha256,
)
from shared.account_execution_locks import AccountExecutionLockUnavailable
from shared.orchestration_repository_support import InvalidStateError
from shared.waybill_entry_extensions import (
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
    "_State",
    "_AutomationProjects",
    "_AutomationPlugins",
    "_Uow",
    "_Repository",
    "_Catalog",
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
        self.policy_events: list[dict] = []
        self.acceptance_lock_trace: list[str] = []
        self.fail_invocation_create_after_guard = False


class _AutomationProjects:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    @property
    def _state(self) -> _State:
        return self._repository.state

    def list_policies(self, *, automation_ids=None):
        return [dict(self._state.policy)] if automation_ids is None or self._state.policy["automation_id"] in automation_ids else []

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

class _AutomationPlugins:
    def require_migration_direct_entrypoint(self, automation_id, *, source, super_admin):
        # This unit fixture contains no migration pairs; migration ownership
        # and transition races are exercised against MySQL separately.
        return None

    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository

    def get_generation_row(self, automation_id, generation, **_kwargs):
        return copy.deepcopy(self._repository.generation_rows.get((automation_id, generation)))

    def get_project(self, automation_id, **kwargs):
        if kwargs.get("for_update"):
            self._repository.state.acceptance_lock_trace.append("project:lock")
        project = dict(self._repository.state.project)
        project["automation_id"] = automation_id
        return project

    def get_project_config(self, automation_id, **_kwargs):
        return (
            dict(self._repository.state.config)
            if automation_id == AUTOMATION_ID
            else None
        )


class _Uow:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository
        self.automation_projects = _AutomationProjects(repository)
        self.automation_plugins = _AutomationPlugins(repository)
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
        self.generation_rows: dict[tuple[str, int], dict] = {}

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


class _DirectCalls:
    """Policy unit port: capture real invocation arguments, never build a Run."""
    def __init__(self, policy_repository):
        self.policy_repository = policy_repository
        self.repository = self
        self.call = None
        self.calls = []
        self.rows = {}
        self.result = None

    def by_request(self, key):
        return next((row for row in self.rows.values() if row["request_key_sha256"] == key), None)

    def get(self, identity):
        return self.rows.get(identity)

    def start(self, **fields):
        from agent.automation_plugins.direct_invocation import public_invocation
        invocation = fields["invocation"]
        key = canonical_sha256([fields["source"], fields["actor_id"], "automation." + invocation.automation_id + ".run", fields["request_key"]])
        replay = self.by_request(key)
        if replay is not None:
            return public_invocation(replay)
        with self.policy_repository.unit_of_work() as uow:
            fields["admission_guard"](uow)
            if self.policy_repository.state.fail_invocation_create_after_guard:
                raise InvalidStateError("synthetic direct invocation create failure")
            uow.commit()
        preview_id = fields.get("preview_invocation_id")
        if preview_id is not None and self.rows[preview_id].get("preview_consumed_by"):
            from shared.orchestration_repository_support import IdempotencyConflict
            raise IdempotencyConflict("preview was already consumed")
        self.call = SimpleNamespace(**fields)
        self.calls.append(self.call)
        identity = str(uuid4())
        if preview_id is not None:
            self.rows[preview_id]["preview_consumed_by"] = identity
        row = {"invocation_id": identity, "request_id": invocation.request_id,
            "request_key_sha256": canonical_sha256([fields["source"], fields["actor_id"], "automation." + invocation.automation_id + ".run", fields["request_key"]]),
            "automation_id": invocation.automation_id, "generation": invocation.automation_generation,
            "arguments_json": dict(fields["arguments"]), "invocation_json": invocation.to_dict(),
            "actor_id": fields["actor_id"], "preview_invocation_id": fields.get("preview_invocation_id"),
            "status": "STARTING", "result_json": None}
        self.rows[identity] = row
        metadata = fields["capability"]["_plugin_runtime"]
        self.policy_repository.generation_rows.setdefault((invocation.automation_id, invocation.automation_generation), {
            "runtime_model": metadata["runtime_model"],
            "snapshot_json": {"execution_metadata": {"contributions": copy.deepcopy(metadata.get("contributions", {}))}},
        })
        return public_invocation(row)

    async def wait(self, identity, *, timeout_seconds=None):
        from agent.automation_plugins.direct_invocation import public_invocation
        self.waited = identity, timeout_seconds
        return self.result or public_invocation({**self.rows[identity], "status": "COMPLETED",
            "result_json": {"status": "SUCCESS", "data": {}, "error": None}})


class AutomationProjectPolicyServiceTestBase(TestCase):
    def setUp(self) -> None:
        self.repository = _Repository()
        self.contract = _contract()
        self.entry = SimpleNamespace(automation_id=AUTOMATION_ID, runtime_model="SERVICE_V2", plugin_id="policy_fixture", committed_snapshot={})
        self.direct = _DirectCalls(self.repository)
        def capability(_snapshot):
            return {"name": self.contract.tool_name, "operation_type": self.contract.operation_type,
                "input_schema": {"type": "object", "additionalProperties": True},
                "permissions": {"required_roles": ["admin"]}, "approval": {"mode": "required"},
                "_plugin_runtime": {"automation_id": self.entry.automation_id,
                    "plugin_id": self.entry.plugin_id, "runtime_model": self.entry.runtime_model,
                    "contributions": getattr(self.entry, "contributions", {
                        item.entrypoint: [{"id": key}] for key, item in self.contract.invocation_contracts.items()
                    })}}
        self.addCleanup(patch.stopall)
        patch("agent.orchestration.direct_project_invocation.project_capability_from_snapshot", side_effect=capability).start()
        patch("agent.orchestration.direct_project_invocation.PluginExecutionRouter._service_contribution_capability", side_effect=lambda capability, **_: capability).start()
        # Every project is Service V2; its exact committed contribution is active.
        self.contribution_registry = _ContributionRegistry()
        self.service = AutomationProjectPolicyService(
            self.repository,
            plugin_catalog=_Catalog(self.entry),
            direct_invocations=self.direct,
            contribution_registry=self.contribution_registry,
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
            plugin_catalog=_Catalog(self.entry),
            direct_invocations=self.direct,
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
        self.entry.trust_source = "super_admin_upload"
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

    def _set_selection_project(
        self,
        automation_id: str = "split_pending_problem_upload",
    ) -> None:
        self.entry.automation_id = automation_id
        self.entry.plugin_id = automation_id
        self.entry.trust_source = "super_admin_upload"
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
