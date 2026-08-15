"""Service for audited, per-task scheduled approval configuration."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from agent.orchestration.models import (
    Actor,
    ActorType,
    OperationType,
    OrchestrationError,
    PlanStep,
    new_id,
)
from agent.orchestration.policy_engine import ScheduledAllowlistEntry
from agent.orchestration.ports import ToolCatalogPort
from shared.redaction import redact_text
from shared.orchestration_repository import (
    AccountExecutionLockUnavailable,
    ConcurrentUpdateError,
    OrchestrationRepository,
)
from shared.scheduled_task_approval import (
    ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
    ACCOUNT_CREDENTIAL_CHANGE_COMMENT,
    ACCOUNT_CREDENTIAL_CHANGE_REASON,
    ScheduledTaskApprovalContractError,
    ScheduledTaskApprovalMode,
    ScheduledTaskPolicyStatus,
    build_scheduled_task_contract,
    exemption_eligibility,
)
from shared.scheduled_task_contracts import (
    APPROVED_SCHEDULED_TASK_PROFILES,
    ScheduledTaskContractError,
    validate_persisted_scheduled_task,
)


MIGRATION_ACTOR_ID = "system:migration:control-plane-v1"
MIGRATION_ACTOR_ROLE = "migration_authority"
MIGRATION_COMMENT = "preserve previously authorized production automation"
BOOTSTRAP_COMPLETION_TASK_ID = "__control_plane_v1_bootstrap_complete__"
BOOTSTRAP_COMPLETION_REQUEST_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "boyi:control-plane-v1:bootstrap-complete")
)
BOOTSTRAP_COMPLETION_COMMENT = "control-plane v1 reviewed schedule evaluation completed"
TERMINAL_RUN_STATUSES = frozenset(
    {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}
)
PROTECTED_CREDENTIAL_OPERATIONS = frozenset(
    {
        OperationType.INTERNAL_PROJECTION_WRITE.value,
        OperationType.EXTERNAL_WRITE.value,
        OperationType.FINANCIAL_WRITE.value,
        OperationType.DESTRUCTIVE.value,
    }
)
PROJECT_CREDENTIAL_CHANGE_REASON = "ACCOUNT_CREDENTIAL_CHANGED"
PROJECT_CREDENTIAL_CHANGE_COMMENT = (
    "Project full-auto authorization revoked before bound credentials changed"
)


class ScheduledTaskApprovalService:
    def __init__(
        self,
        repository: OrchestrationRepository,
        catalog: ToolCatalogPort,
        *,
        enabled_finance_platforms: Sequence[str] = (),
        active_account_ids_provider: Callable[[], Sequence[str]] | None = None,
        implicit_account_ids_by_tool: Mapping[str, Sequence[str]] | None = None,
        bootstrap_allowed_tool_names: Sequence[str] | None = None,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._enabled_finance_platforms = tuple(enabled_finance_platforms)
        self._active_account_ids_provider = active_account_ids_provider
        self._implicit_account_ids_by_tool = _normalize_implicit_account_ids(
            implicit_account_ids_by_tool
        )
        self._bootstrap_allowed_tool_names = (
            None
            if bootstrap_allowed_tool_names is None
            else frozenset(
                str(item or "").strip()
                for item in bootstrap_allowed_tool_names
                if str(item or "").strip()
            )
        )
        if self._bootstrap_allowed_tool_names == frozenset():
            raise ValueError("bootstrap allowed tool names cannot be empty")
        self._credential_policy_lock = threading.RLock()
        self._credential_changes_in_progress: dict[str, int] = {}

    def list_policies(self) -> dict[str, Any]:
        rows = self._repository.list_scheduled_task_policy_rows()
        active_account_ids = self._load_active_account_ids()
        return {
            "items": [
                self._describe(row, active_account_ids=active_account_ids)
                for row in rows
            ]
        }

    def allowlist_entries(self) -> tuple[ScheduledAllowlistEntry, ...]:
        """Return only effective exact policies; any error fails closed."""

        entries: list[ScheduledAllowlistEntry] = []
        active_account_ids = self._load_active_account_ids()
        for row in self._repository.list_scheduled_task_policy_rows():
            if not _enabled(row.get("enabled")):
                continue
            described, contract, dynamic_rules = self._evaluate(
                row,
                active_account_ids=active_account_ids,
            )
            if described["effective_mode"] != ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value:
                continue
            if contract is None:
                continue
            entries.append(
                ScheduledAllowlistEntry.from_arguments(
                    task_id=str(row["id"]),
                    tool_name=str(row["tool_name"]),
                    tool_version=str(contract.snapshot["tool_version"]),
                    arguments=dict(row.get("tool_params") or {}),
                    dynamic_argument_rules=dynamic_rules,
                    cron_expression=str(row["cron_expression"]),
                    contract_hash=contract.contract_hash,
                    configuration_version=int(row["configuration_version"]),
                )
            )
        return tuple(entries)

    def begin_credentials_change(self, account_id: str) -> Callable[[], None]:
        """Reject active writes, revoke exemptions, and hold an account lease.

        The MySQL named lock is connection-scoped rather than transactional: all
        database checks and revocation transactions finish before the caller
        changes a broker/file credential, while protected step starts remain
        serialized until the returned idempotent callback releases the lock.
        """

        safe_account_id = _validate_account_id(account_id)
        try:
            execution_lease = self._repository.acquire_account_execution_locks(
                (safe_account_id,),
                timeout_seconds=0,
            )
        except AccountExecutionLockUnavailable as exc:
            raise OrchestrationError(
                "ACCOUNT_EXECUTION_IN_PROGRESS",
                "Credentials cannot change while account-bound execution is starting",
            ) from exc
        except Exception as exc:
            raise OrchestrationError(
                "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
                "Credentials cannot change because the account execution guard is unavailable",
            ) from exc
        self._mark_credentials_change_started(safe_account_id)
        try:
            self._assert_no_active_protected_runs(safe_account_id)
            self._revoke_exact_policies_for_account(safe_account_id)
        except BaseException:
            self._mark_credentials_change_finished(safe_account_id)
            execution_lease.release()
            raise

        finish_lock = threading.Lock()
        finished = False

        def finish() -> None:
            nonlocal finished
            with finish_lock:
                if finished:
                    return
                execution_lease.release()
                self._mark_credentials_change_finished(safe_account_id)
                finished = True

        return finish

    def begin_protected_step_start(self, step: PlanStep) -> Callable[[], None]:
        """Serialize one account-bound write until its RUNNING state commits."""

        if step.operation_type in {OperationType.READ, OperationType.COMPUTE}:
            return _noop_finish
        account_ids = self._account_ids_for_tool_execution(
            tool_name=step.tool_name,
            arguments=step.arguments,
            explicit_account_id=step.account_id,
        )
        if not account_ids:
            return _noop_finish
        try:
            lease = self._repository.acquire_account_execution_locks(
                account_ids,
                timeout_seconds=0,
            )
        except AccountExecutionLockUnavailable as exc:
            raise OrchestrationError(
                "ACCOUNT_CREDENTIAL_CHANGE_IN_PROGRESS",
                "Account credentials are changing; protected execution has not started",
            ) from exc
        except Exception as exc:
            raise OrchestrationError(
                "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
                "Protected execution cannot start because the account guard is unavailable",
                details={"status": "BLOCKED_DATA"},
            ) from exc
        return lease.release

    def _assert_no_active_protected_runs(self, account_id: str) -> None:
        try:
            rows = self._repository.list_nonterminal_runs_with_commands()
        except Exception as exc:
            raise OrchestrationError(
                "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                "Credentials cannot change because active Runs could not be checked",
            ) from exc
        active_run_ids: list[str] = []
        try:
            for row in rows:
                if str(row.get("status") or "").strip().upper() in TERMINAL_RUN_STATUSES:
                    continue
                if self._run_references_protected_account(row, account_id):
                    active_run_ids.append(str(row.get("run_id") or "").strip())
        except OrchestrationError:
            raise
        except Exception as exc:
            raise OrchestrationError(
                "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                "Credentials cannot change because an active Run could not be classified",
            ) from exc
        if active_run_ids:
            raise OrchestrationError(
                "ACCOUNT_CREDENTIAL_ACTIVE_RUN",
                "Credentials cannot change while a protected account-bound Run is non-terminal",
                details={"run_ids": sorted(active_run_ids)},
            )

    def _run_references_protected_account(
        self,
        row: Mapping[str, Any],
        account_id: str,
    ) -> bool:
        plan = row.get("plan_json")
        if plan is not None and not isinstance(plan, Mapping):
            raise OrchestrationError(
                "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                "An active Run has an invalid persisted plan",
            )
        raw_steps = plan.get("steps") if isinstance(plan, Mapping) else None
        if raw_steps is not None and not isinstance(raw_steps, list):
            raise OrchestrationError(
                "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                "An active Run has invalid persisted plan steps",
            )
        for raw_step in raw_steps or ():
            if not isinstance(raw_step, Mapping):
                raise OrchestrationError(
                    "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                    "An active Run has an invalid persisted step",
                )
            if self._execution_descriptor_is_protected_for_account(
                raw_step,
                account_id=account_id,
                arguments=raw_step.get("arguments"),
            ):
                return True

        parameters = row.get("command_parameters_json")
        if not isinstance(parameters, Mapping):
            raise OrchestrationError(
                "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                "An active Run has invalid command parameters",
            )
        return self._execution_descriptor_is_protected_for_account(
            parameters,
            account_id=account_id,
            arguments=parameters.get("arguments"),
        )

    def _execution_descriptor_is_protected_for_account(
        self,
        descriptor: Mapping[str, Any],
        *,
        account_id: str,
        arguments: Any,
    ) -> bool:
        tool_name = str(descriptor.get("tool_name") or "").strip()
        descriptor_arguments = arguments if isinstance(arguments, Mapping) else {}
        explicit_account_id = descriptor.get("account_id")
        account_ids = self._account_ids_for_tool_execution(
            tool_name=tool_name,
            arguments=descriptor_arguments,
            explicit_account_id=explicit_account_id,
        )
        if account_id not in account_ids:
            return False
        raw_operation = str(descriptor.get("operation_type") or "").strip()
        if not raw_operation:
            capability = self._catalog.get_capability(tool_name)
            if not isinstance(capability, Mapping):
                raise OrchestrationError(
                    "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                    "An account-bound active Run references an unknown tool contract",
                )
            raw_operation = str(capability.get("operation_type") or "").strip()
        if raw_operation not in {item.value for item in OperationType}:
            raise OrchestrationError(
                "ACCOUNT_ACTIVE_RUN_CHECK_FAILED",
                "An account-bound active Run has an invalid operation type",
            )
        return raw_operation in PROTECTED_CREDENTIAL_OPERATIONS

    def revoke_exact_policies_for_account(self, account_id: str) -> dict[str, Any]:
        """Atomically revoke every exact exemption that references ``account_id``.

        Credential persistence calls this synchronously before touching the
        credential store. Any repository failure propagates so the caller can
        fail closed without changing the external principal behind an approved
        account slot.
        """

        safe_account_id = _validate_account_id(account_id)
        self._mark_credentials_change_started(safe_account_id)
        try:
            return self._revoke_exact_policies_for_account(safe_account_id)
        finally:
            self._mark_credentials_change_finished(safe_account_id)

    def _revoke_exact_policies_for_account(
        self,
        safe_account_id: str,
    ) -> dict[str, Any]:
        request_id = new_id()
        correlation_id = new_id()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        revoked_task_ids: list[str] = []
        try:
            with self._repository.unit_of_work() as uow:
                rows = uow.scheduled_policies.list_with_tasks(for_update=True)
                affected = [
                    row
                    for row in rows
                    if str(row.get("mode") or "")
                    == ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value
                    and safe_account_id in self._account_ids_for_task(row)
                ]
                for row in affected:
                    task_id = str(row.get("id") or "").strip()
                    updated = uow.scheduled_policies.update_policy(
                        task_id,
                        expected_version=int(row.get("policy_version") or 0),
                        mode=ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value,
                        contract_hash=None,
                        contract_snapshot=None,
                        tool_contract_hash=None,
                        actor_id=ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
                        actor_role="system",
                        actor_display_name="Account credential safety guard",
                        comment=ACCOUNT_CREDENTIAL_CHANGE_COMMENT,
                    )
                    uow.scheduled_policies.append_event(
                        {
                            "task_id": task_id,
                            "request_id": request_id,
                            "from_mode": ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value,
                            "to_mode": ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value,
                            "contract_hash": None,
                            "contract_snapshot_json": None,
                            "tool_contract_hash": None,
                            "actor_id": ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
                            "actor_role": "system",
                            "actor_display_name": "Account credential safety guard",
                            "reason": ACCOUNT_CREDENTIAL_CHANGE_REASON,
                            "comment": ACCOUNT_CREDENTIAL_CHANGE_COMMENT,
                            "occurred_at": now,
                            "correlation_id": correlation_id,
                        }
                    )
                    self._append_domain_event(
                        uow,
                        task_id=task_id,
                        request_id=request_id,
                        correlation_id=correlation_id,
                        mode=ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value,
                        version=int(updated["version"]),
                        actor_id=ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
                        occurred_at=now,
                    )
                    revoked_task_ids.append(task_id)
                project_rows = uow.automation_projects.list_account_binding_policy_rows(
                    for_update=True
                )
                affected_projects = [
                    row
                    for row in project_rows
                    if str(row.get("mode") or "") == "PROJECT_FULL_AUTO"
                    and safe_account_id
                    in _collect_project_account_binding_ids(
                        row.get("account_bindings_json")
                    )
                ]
                for row in affected_projects:
                    automation_id = str(row.get("automation_id") or "").strip()
                    event_request_id = f"{request_id}:{automation_id}"
                    updated = uow.automation_projects.update_policy(
                        automation_id,
                        expected_version=int(row.get("version") or 0),
                        mode="REQUIRE_EACH_RUN",
                        contract_hash=None,
                        contract_snapshot=None,
                        tool_contract_hash=None,
                        plugin_contract_hash=None,
                        project_generation=int(
                            row.get("project_generation") or 0
                        ),
                        project_configuration_version=int(
                            row.get("project_configuration_version") or 0
                        ),
                        actor_id=ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
                        actor_role="system",
                        actor_display_name="Account credential safety guard",
                        comment=PROJECT_CREDENTIAL_CHANGE_COMMENT,
                    )
                    uow.automation_projects.append_event(
                        {
                            "automation_id": automation_id,
                            "request_id": event_request_id,
                            "from_mode": "PROJECT_FULL_AUTO",
                            "to_mode": "REQUIRE_EACH_RUN",
                            "contract_hash": None,
                            "contract_snapshot_json": None,
                            "tool_contract_hash": None,
                            "plugin_contract_hash": None,
                            "project_generation": int(
                                row.get("project_generation") or 0
                            ),
                            "project_configuration_version": int(
                                row.get("project_configuration_version") or 0
                            ),
                            "actor_id": ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
                            "actor_role": "system",
                            "actor_display_name": "Account credential safety guard",
                            "reason": PROJECT_CREDENTIAL_CHANGE_REASON,
                            "comment": PROJECT_CREDENTIAL_CHANGE_COMMENT,
                            "occurred_at": now,
                            "correlation_id": correlation_id,
                        }
                    )
                    self._append_project_domain_event(
                        uow,
                        automation_id=automation_id,
                        request_id=event_request_id,
                        correlation_id=correlation_id,
                        mode="REQUIRE_EACH_RUN",
                        version=int(updated["version"]),
                        actor_id=ACCOUNT_CREDENTIAL_CHANGE_ACTOR_ID,
                        occurred_at=now,
                    )
                uow.commit()
        except ConcurrentUpdateError as exc:
            raise OrchestrationError(
                "ACCOUNT_POLICY_REVOCATION_CONFLICT",
                "Scheduled approval policy changed while credentials were being prepared",
            ) from exc
        return {
            "account_id": safe_account_id,
            "revoked_count": len(revoked_task_ids),
            "task_ids": sorted(revoked_task_ids),
        }

    def _mark_credentials_change_started(self, account_id: str) -> None:
        with self._credential_policy_lock:
            self._credential_changes_in_progress[account_id] = (
                self._credential_changes_in_progress.get(account_id, 0) + 1
            )

    def _mark_credentials_change_finished(self, account_id: str) -> None:
        with self._credential_policy_lock:
            remaining = self._credential_changes_in_progress.get(account_id, 0) - 1
            if remaining > 0:
                self._credential_changes_in_progress[account_id] = remaining
            else:
                self._credential_changes_in_progress.pop(account_id, None)

    def _account_ids_for_task(self, task: Mapping[str, Any]) -> set[str]:
        return self._account_ids_for_tool_execution(
            tool_name=str(task.get("tool_name") or "").strip(),
            arguments=task.get("tool_params") or {},
        )

    def _account_ids_for_tool_execution(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        explicit_account_id: Any = None,
    ) -> set[str]:
        account_ids = _collect_account_ids(arguments)
        safe_explicit = str(explicit_account_id or "").strip()
        if safe_explicit:
            account_ids.add(safe_explicit)
        account_ids.update(
            self._implicit_account_ids_by_tool.get(str(tool_name or "").strip(), ())
        )
        account_ids.update(self._project_account_ids_for_tool(tool_name))
        return account_ids

    def _project_account_ids_for_tool(self, tool_name: str) -> set[str]:
        """Resolve plugin accounts from the immutable committed generation.

        Plugin payload arguments deliberately contain no business-account IDs.
        The core capability is therefore the only safe source for the account
        execution lock and the credential-change active-Run scan.
        """

        safe_tool_name = str(tool_name or "").strip()
        prefix = "automation."
        suffix = ".run"
        if not safe_tool_name.startswith(prefix) or not safe_tool_name.endswith(suffix):
            return set()
        automation_id = safe_tool_name[len(prefix) : -len(suffix)].strip()
        if not automation_id:
            raise OrchestrationError(
                "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
                "A protected project execution has an invalid tool identity",
            )
        try:
            capability = self._catalog.get_capability(safe_tool_name)
        except Exception as exc:
            raise OrchestrationError(
                "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
                "Project account bindings could not be loaded from the committed generation",
            ) from exc
        runtime = (
            capability.get("_plugin_runtime")
            if isinstance(capability, Mapping)
            else None
        )
        if (
            not isinstance(runtime, Mapping)
            or str(runtime.get("automation_id") or "").strip() != automation_id
        ):
            raise OrchestrationError(
                "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
                "Project account bindings do not match the committed generation",
            )
        try:
            return _collect_project_account_binding_ids(
                runtime.get("account_bindings")
            )
        except OrchestrationError as exc:
            raise OrchestrationError(
                "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE",
                "Project account bindings are invalid in the committed generation",
            ) from exc

    def _reject_exact_grant_during_credentials_change(
        self,
        tasks: Sequence[Mapping[str, Any]],
    ) -> None:
        changing = set(self._credential_changes_in_progress)
        blocked_task_ids = sorted(
            str(task.get("id") or "").strip()
            for task in tasks
            if self._account_ids_for_task(task) & changing
        )
        if blocked_task_ids:
            raise OrchestrationError(
                "ACCOUNT_CREDENTIAL_CHANGE_IN_PROGRESS",
                "Exact schedule exemption cannot be granted while referenced account credentials are changing",
                details={"task_ids": blocked_task_ids},
            )

    def set_policies(
        self,
        *,
        task_ids: Sequence[str],
        mode: str,
        comment: str,
        request_id: str,
        expected_versions: Mapping[str, int],
        expected_configuration_versions: Mapping[str, int],
        actor: Actor,
    ) -> dict[str, Any]:
        self._require_super_admin(actor)
        try:
            target_mode = ScheduledTaskApprovalMode(str(mode))
        except ValueError as exc:
            raise OrchestrationError("INVALID_SCHEDULE_POLICY_MODE", "Unknown scheduled approval mode") from exc
        clean_ids = tuple(dict.fromkeys(str(value or "").strip() for value in task_ids))
        if not clean_ids or any(not value for value in clean_ids):
            raise OrchestrationError("INVALID_SCHEDULE_TASK_IDS", "At least one task_id is required")
        if len(clean_ids) > 200:
            raise OrchestrationError("TOO_MANY_SCHEDULE_TASKS", "At most 200 tasks may be changed together")
        if set(expected_versions) != set(clean_ids):
            raise OrchestrationError(
                "POLICY_VERSION_REQUIRED",
                "expected_versions must contain exactly every selected task",
            )
        if set(expected_configuration_versions) != set(clean_ids):
            raise OrchestrationError(
                "TASK_CONFIGURATION_VERSION_REQUIRED",
                "expected_configuration_versions must contain exactly every selected task",
            )
        safe_request_id = str(request_id or "").strip()
        try:
            safe_request_id = str(uuid.UUID(safe_request_id))
        except ValueError as exc:
            raise OrchestrationError("INVALID_REQUEST_ID", "request_id must be a UUID") from exc
        correlation_id = safe_request_id
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        safe_comment = redact_text(comment).strip()[:1000]
        results: list[dict[str, Any]] = []
        active_account_ids = self._load_active_account_ids()
        exact_grant = target_mode is ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT
        if exact_grant:
            self._credential_policy_lock.acquire()
        try:
            with self._repository.unit_of_work() as uow:
                prepared: list[tuple[dict[str, Any], dict[str, Any], Any, dict[str, str]]] = []
                locked: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for task_id in sorted(clean_ids):
                    task = uow.scheduled_policies.lock_task(task_id)
                    if task is None:
                        raise OrchestrationError("SCHEDULE_TASK_NOT_FOUND", "A selected scheduled task was not found")
                    policy = uow.scheduled_policies.ensure_default(task_id)
                    locked.append((task, policy))

                for task, _policy in locked:
                    task_id = str(task["id"])
                    expected_configuration_version = expected_configuration_versions[task_id]
                    if (
                        isinstance(expected_configuration_version, bool)
                        or not isinstance(expected_configuration_version, int)
                        or expected_configuration_version < 1
                    ):
                        raise OrchestrationError(
                            "TASK_CONFIGURATION_VERSION_REQUIRED",
                            "Task configuration versions must be positive integers",
                        )
                    if int(task.get("configuration_version") or 0) != expected_configuration_version:
                        raise OrchestrationError(
                            "TASK_CONFIGURATION_VERSION_CONFLICT",
                            "Scheduled task configuration changed; refresh and review it before changing approval policy",
                            details={"task_id": task_id},
                        )

                if exact_grant:
                    self._reject_exact_grant_during_credentials_change(
                        tuple(task for task, _policy in locked)
                    )

                existing_events = uow.scheduled_policies.list_events_by_request(
                    safe_request_id,
                    for_update=True,
                )
                if existing_events:
                    existing_ids = {str(item.get("task_id") or "") for item in existing_events}
                    if existing_ids != set(clean_ids) or any(
                        str(item.get("to_mode") or "") != target_mode.value
                        for item in existing_events
                    ):
                        raise OrchestrationError(
                            "IDEMPOTENCY_CONFLICT",
                            "request_id was reused for a different policy change",
                        )
                    items = [
                        self._describe(
                            {**task, **policy, "policy_version": policy["version"]},
                            active_account_ids=active_account_ids,
                        )
                        for task, policy in locked
                    ]
                    uow.commit()
                    return {"items": items, "updated_count": 0}

                for task, policy in locked:
                    task_id = str(task["id"])
                    expected = expected_versions[task_id]
                    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
                        raise OrchestrationError("POLICY_VERSION_REQUIRED", "Policy versions must be positive integers")
                    if int(policy.get("version") or 0) != expected:
                        raise OrchestrationError("POLICY_VERSION_CONFLICT", "A scheduled policy changed concurrently")
                    capability, contract, rules = self._contract_for_task(
                        task,
                        active_account_ids=active_account_ids,
                    )
                    if target_mode is ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT and contract is None:
                        eligible, reason = exemption_eligibility(capability)
                        raise OrchestrationError(
                            "SCHEDULE_EXEMPT_NOT_ALLOWED",
                            "This scheduled task cannot be exempted",
                            details={"task_id": task_id, "reason": reason or "INVALID_TASK_CONTRACT"},
                        )
                    prepared.append((task, policy, contract, rules))

                for task, policy, contract, _rules in prepared:
                    exact = target_mode is ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT
                    updated = uow.scheduled_policies.update_policy(
                        str(task["id"]),
                        expected_version=int(policy["version"]),
                        mode=target_mode.value,
                        contract_hash=contract.contract_hash if exact and contract is not None else None,
                        contract_snapshot=contract.snapshot if exact and contract is not None else None,
                        tool_contract_hash=contract.tool_contract_hash if exact and contract is not None else None,
                        actor_id=actor.actor_id,
                        actor_role="super_admin",
                        actor_display_name=actor.display_name,
                        comment=safe_comment,
                    )
                    event = uow.scheduled_policies.append_event(
                        {
                            "task_id": task["id"],
                            "request_id": safe_request_id,
                            "from_mode": policy.get("mode"),
                            "to_mode": target_mode.value,
                            "contract_hash": contract.contract_hash if exact and contract is not None else None,
                            "contract_snapshot_json": contract.snapshot if exact and contract is not None else None,
                            "tool_contract_hash": contract.tool_contract_hash if exact and contract is not None else None,
                            "actor_id": actor.actor_id,
                            "actor_role": "super_admin",
                            "actor_display_name": actor.display_name,
                            "reason": "console_policy_change",
                            "comment": safe_comment,
                            "occurred_at": now,
                            "correlation_id": correlation_id,
                        }
                    )
                    self._append_domain_event(
                        uow,
                        task_id=str(task["id"]),
                        request_id=safe_request_id,
                        correlation_id=correlation_id,
                        mode=target_mode.value,
                        version=int(updated["version"]),
                        actor_id=actor.actor_id,
                        occurred_at=now,
                    )
                    results.append(
                        self._describe(
                            {**task, **updated, "policy_version": updated["version"]},
                            active_account_ids=active_account_ids,
                        )
                    )
                uow.commit()
        except ConcurrentUpdateError as exc:
            raise OrchestrationError("POLICY_VERSION_CONFLICT", "A scheduled policy changed concurrently") from exc
        finally:
            if exact_grant:
                self._credential_policy_lock.release()
        return {"items": results, "updated_count": len(results)}

    def bootstrap_reviewed_policies(self) -> dict[str, int]:
        """One-time preservation of the previously reviewed production set."""

        counts = {
            "reviewed_candidates": 0,
            "created": 0,
            "already_present": 0,
            "explicitly_configured": 0,
            "rejected": 0,
            "completed": 0,
        }
        if self._bootstrap_completion_marker_exists():
            counts["completed"] = 1
            return counts
        rows = self._repository.list_scheduled_task_policy_rows()
        active_account_ids = self._load_active_account_ids()
        reviewed_task_ids = {
            task_id
            for profile in APPROVED_SCHEDULED_TASK_PROFILES.values()
            if self._bootstrap_allowed_tool_names is None
            or profile.tool_name in self._bootstrap_allowed_tool_names
            for task_id in profile.approved_task_ids
        }
        for row in rows:
            if str(row.get("id") or "") not in reviewed_task_ids or not _enabled(row.get("enabled")):
                continue
            counts["reviewed_candidates"] += 1
            policy_mode = str(row.get("mode") or ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value)
            if policy_mode == ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value:
                counts["already_present"] += 1
                continue
            untouched_default = (
                policy_mode == ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value
                and int(row.get("policy_version") or 1) == 1
                and not row.get("approved_by_actor_id")
            )
            if not untouched_default:
                counts["explicitly_configured"] += 1
                continue
            try:
                capability, contract, _rules = self._contract_for_task(
                    row,
                    require_reviewed=True,
                    active_account_ids=active_account_ids,
                )
                del capability
                if contract is None:
                    counts["rejected"] += 1
                    continue
            except (ScheduledTaskContractError, ScheduledTaskApprovalContractError, ValueError, TypeError):
                counts["rejected"] += 1
                continue
            try:
                self._bootstrap_one(row, active_account_ids=active_account_ids)
            except (ConcurrentUpdateError, OrchestrationError):
                counts["rejected"] += 1
            else:
                counts["created"] += 1
        if counts["rejected"] == 0:
            self._write_bootstrap_completion_marker()
            counts["completed"] = 1
        return counts

    def _bootstrap_completion_marker_exists(self) -> bool:
        with self._repository.unit_of_work() as uow:
            return bool(
                uow.scheduled_policies.get_event_by_request(
                    BOOTSTRAP_COMPLETION_TASK_ID,
                    BOOTSTRAP_COMPLETION_REQUEST_ID,
                )
            )

    def _write_bootstrap_completion_marker(self) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._repository.unit_of_work() as uow:
            if uow.scheduled_policies.get_event_by_request(
                BOOTSTRAP_COMPLETION_TASK_ID,
                BOOTSTRAP_COMPLETION_REQUEST_ID,
            ):
                uow.commit()
                return
            uow.scheduled_policies.append_event(
                {
                    "task_id": BOOTSTRAP_COMPLETION_TASK_ID,
                    "request_id": BOOTSTRAP_COMPLETION_REQUEST_ID,
                    "from_mode": None,
                    "to_mode": ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value,
                    "contract_hash": None,
                    "contract_snapshot_json": None,
                    "tool_contract_hash": None,
                    "actor_id": MIGRATION_ACTOR_ID,
                    "actor_role": MIGRATION_ACTOR_ROLE,
                    "actor_display_name": "Control Plane v1 migration",
                    "reason": "control_plane_v1_bootstrap_complete",
                    "comment": BOOTSTRAP_COMPLETION_COMMENT,
                    "occurred_at": now,
                    "correlation_id": BOOTSTRAP_COMPLETION_REQUEST_ID,
                }
            )
            uow.commit()

    def _bootstrap_one(
        self,
        task: Mapping[str, Any],
        *,
        active_account_ids: set[str] | None = None,
    ) -> None:
        task_id = str(task["id"])
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"boyi:control-plane-v1:{task_id}"))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        correlation_id = new_id()
        with self._credential_policy_lock:
            with self._repository.unit_of_work() as uow:
                locked_task = uow.scheduled_policies.lock_task(task_id)
                if locked_task is None:
                    raise OrchestrationError("SCHEDULE_TASK_NOT_FOUND", "Scheduled task disappeared")
                self._reject_exact_grant_during_credentials_change((locked_task,))
                _capability, locked_contract, _rules = self._contract_for_task(
                    locked_task,
                    require_reviewed=True,
                    active_account_ids=active_account_ids,
                )
                if locked_contract is None:
                    raise OrchestrationError("SCHEDULE_EXEMPT_NOT_ALLOWED", "Reviewed task contract changed")
                policy = uow.scheduled_policies.ensure_default(task_id)
                if uow.scheduled_policies.get_event_by_request(task_id, request_id):
                    uow.commit()
                    return
                if str(policy.get("mode") or "") != ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value:
                    raise OrchestrationError("POLICY_VERSION_CONFLICT", "Scheduled policy is already configured")
                updated = uow.scheduled_policies.update_policy(
                    task_id,
                    expected_version=int(policy["version"]),
                    mode=ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value,
                    contract_hash=locked_contract.contract_hash,
                    contract_snapshot=locked_contract.snapshot,
                    tool_contract_hash=locked_contract.tool_contract_hash,
                    actor_id=MIGRATION_ACTOR_ID,
                    actor_role=MIGRATION_ACTOR_ROLE,
                    actor_display_name="Control Plane v1 migration",
                    comment=MIGRATION_COMMENT,
                )
                uow.scheduled_policies.append_event(
                    {
                        "task_id": task_id,
                        "request_id": request_id,
                        "from_mode": policy.get("mode"),
                        "to_mode": ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value,
                        "contract_hash": locked_contract.contract_hash,
                        "contract_snapshot_json": locked_contract.snapshot,
                        "tool_contract_hash": locked_contract.tool_contract_hash,
                        "actor_id": MIGRATION_ACTOR_ID,
                        "actor_role": MIGRATION_ACTOR_ROLE,
                        "actor_display_name": "Control Plane v1 migration",
                        "reason": "control_plane_v1_bootstrap",
                        "comment": MIGRATION_COMMENT,
                        "occurred_at": now,
                        "correlation_id": correlation_id,
                    }
                )
                self._append_domain_event(
                    uow,
                    task_id=task_id,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    mode=ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value,
                    version=int(updated["version"]),
                    actor_id=MIGRATION_ACTOR_ID,
                    occurred_at=now,
                )
                uow.commit()

    def _contract_for_task(
        self,
        task: Mapping[str, Any],
        *,
        require_reviewed: bool = False,
        active_account_ids: set[str] | None = None,
    ) -> tuple[Mapping[str, Any] | None, Any | None, dict[str, str]]:
        tool_name = str(task.get("tool_name") or "").strip()
        capability = self._catalog.get_capability(tool_name)
        rules: dict[str, str] = {}
        try:
            reviewed = validate_persisted_scheduled_task(
                task,
                capability=capability,
                validate_arguments=self._catalog.validate_arguments,
                enabled_finance_platforms=self._enabled_finance_platforms,
            )
            rules = dict(reviewed.dynamic_argument_rules)
        except ScheduledTaskContractError:
            if require_reviewed:
                raise
            rules = _runtime_dynamic_rules(tool_name)
        if not isinstance(capability, Mapping):
            return capability, None, rules
        try:
            self._catalog.validate_arguments(tool_name, task.get("tool_params"))
            self._validate_accounts(
                task.get("tool_params") or {},
                capability,
                active_account_ids=active_account_ids,
            )
            profile = _profile_for_task_id(str(task.get("id") or ""))
            special_cron = profile.cron_expression if profile is not None else None
            contract = build_scheduled_task_contract(
                task,
                capability,
                dynamic_argument_rules=rules,
                allowed_special_cron=special_cron,
            )
        except (KeyError, TypeError, ValueError, ScheduledTaskApprovalContractError):
            return capability, None, rules
        return capability, contract, rules

    def _validate_accounts(
        self,
        arguments: Mapping[str, Any],
        capability: Mapping[str, Any],
        *,
        active_account_ids: set[str] | None = None,
    ) -> None:
        if self._active_account_ids_provider is None:
            return
        active_ids = active_account_ids
        if active_ids is None:
            active_ids = self._load_active_account_ids()
        if active_ids is None:
            raise ScheduledTaskApprovalContractError("ACCOUNT_DIRECTORY_UNAVAILABLE")
        requested = _collect_account_ids(arguments)
        if requested - active_ids:
            raise ScheduledTaskApprovalContractError("ACCOUNT_NOT_ACTIVE")
        account_scope = capability.get("account_scope") or {}
        if isinstance(account_scope, Mapping):
            mode = str(account_scope.get("mode") or "")
            required = bool(account_scope.get("required")) or mode == "single"
        else:
            mode = str(account_scope or "")
            required = mode == "single"
        if required and not requested:
            raise ScheduledTaskApprovalContractError("ACCOUNT_ID_REQUIRED")
        if mode in {"all_configured", "single_or_all_configured"} and not active_ids:
            raise ScheduledTaskApprovalContractError("NO_CONFIGURED_ACCOUNTS")

    def _evaluate(
        self,
        row: Mapping[str, Any],
        *,
        active_account_ids: set[str] | None = None,
    ) -> tuple[dict[str, Any], Any | None, dict[str, str]]:
        capability, contract, rules = self._contract_for_task(
            row,
            active_account_ids=active_account_ids,
        )
        configured = str(row.get("mode") or ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value)
        if configured not in {mode.value for mode in ScheduledTaskApprovalMode}:
            configured = ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value
        tool_eligible, eligibility_reason = exemption_eligibility(capability)
        eligible = tool_eligible and contract is not None
        if tool_eligible and contract is None:
            eligibility_reason = "TASK_CONTRACT_INVALID"
        effective = ScheduledTaskApprovalMode.REQUIRE_EACH_RUN.value
        status = ScheduledTaskPolicyStatus.ACTIVE.value
        stale_reason: str | None = None
        if not eligible:
            status = ScheduledTaskPolicyStatus.UNSUPPORTED.value
            stale_reason = eligibility_reason
        elif configured == ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value:
            if contract is None:
                status = ScheduledTaskPolicyStatus.STALE.value
                stale_reason = "TASK_CONTRACT_INVALID"
            elif str(row.get("contract_hash") or "") != contract.contract_hash:
                status = ScheduledTaskPolicyStatus.STALE.value
                stale_reason = "TASK_OR_TOOL_CONFIGURATION_CHANGED"
            elif str(row.get("tool_contract_hash") or "") != contract.tool_contract_hash:
                status = ScheduledTaskPolicyStatus.STALE.value
                stale_reason = "TOOL_CONTRACT_CHANGED"
            else:
                effective = ScheduledTaskApprovalMode.EXACT_SCHEDULE_EXEMPT.value
        described = {
            "task_id": str(row.get("id") or ""),
            "configured_mode": configured,
            "mode": configured,
            "effective_mode": effective,
            "effective_status": status,
            "can_exempt": eligible,
            "eligible": eligible,
            "version": int(row.get("policy_version") or row.get("version") or 1),
            "configuration_version": int(row.get("configuration_version") or 0),
            "policy_hash_short": str(row.get("contract_hash") or "")[:12] or None,
            "approved_by": str(row.get("approved_by_actor_display_name") or row.get("approved_by_actor_id") or "") or None,
            "approved_at": _datetime_text(row.get("approved_at")),
            "invalid_reason": stale_reason,
            "stale_reason": stale_reason,
        }
        return described, contract, rules

    def _describe(
        self,
        row: Mapping[str, Any],
        *,
        active_account_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        return self._evaluate(row, active_account_ids=active_account_ids)[0]

    def _load_active_account_ids(self) -> set[str] | None:
        if self._active_account_ids_provider is None:
            return None
        try:
            active_values = self._active_account_ids_provider()
            return {
                str(value or "").strip()
                for value in active_values
                if str(value or "").strip()
            }
        except Exception as exc:
            raise ScheduledTaskApprovalContractError("ACCOUNT_DIRECTORY_UNAVAILABLE") from exc

    @staticmethod
    def _require_super_admin(actor: Actor) -> None:
        if (
            actor.actor_type is not ActorType.CONSOLE_ADMIN
            or "super_admin" not in actor.roles
            or actor.authenticated_by != "mysql_admin_session"
        ):
            raise OrchestrationError(
                "ACTION_FORBIDDEN",
                "A signed Console super administrator is required",
            )

    @staticmethod
    def _append_domain_event(
        uow: Any,
        *,
        task_id: str,
        request_id: str,
        correlation_id: str,
        mode: str,
        version: int,
        actor_id: str,
        occurred_at: datetime,
    ) -> None:
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": "scheduled_task.approval_policy_changed",
                "schema_version": 1,
                "source_system": "agent",
                "source_event_id": f"{task_id}:{request_id}",
                "entity_type": "scheduled_task",
                "entity_id": task_id,
                "occurred_at": occurred_at,
                "observed_at": occurred_at,
                "correlation_id": correlation_id,
                "payload": {
                    "task_id": task_id,
                    "mode": mode,
                    "policy_version": version,
                    "actor_id": actor_id,
                },
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": "scheduled_task.approval_policy_changed",
                    "partition_key": task_id,
                    "max_attempts": 10,
                },
            ),
        )

    @staticmethod
    def _append_project_domain_event(
        uow: Any,
        *,
        automation_id: str,
        request_id: str,
        correlation_id: str,
        mode: str,
        version: int,
        actor_id: str,
        occurred_at: datetime,
    ) -> None:
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": "automation_project.approval_policy_changed",
                "schema_version": 1,
                "source_system": "agent",
                "source_event_id": f"{automation_id}:{request_id}",
                "entity_type": "automation_project",
                "entity_id": automation_id,
                "occurred_at": occurred_at,
                "observed_at": occurred_at,
                "correlation_id": correlation_id,
                "payload": {
                    "automation_id": automation_id,
                    "mode": mode,
                    "policy_version": version,
                    "actor_id": actor_id,
                    "reason": PROJECT_CREDENTIAL_CHANGE_REASON,
                },
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": "automation_project.approval_policy_changed",
                    "partition_key": automation_id,
                    "max_attempts": 10,
                },
            ),
        )


def _datetime_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _noop_finish() -> None:
    return None


def _enabled(value: Any) -> bool:
    return value is True or type(value) is int and value == 1


def _profile_for_task_id(task_id: str):
    for profile in APPROVED_SCHEDULED_TASK_PROFILES.values():
        if task_id in profile.approved_task_ids:
            return profile
    return None


def _runtime_dynamic_rules(tool_name: str) -> dict[str, str]:
    if tool_name == "sync_finance_bills":
        return {"target_date": "scheduled_previous_day"}
    if tool_name == "sync_site_send_list":
        return {"target_date": "current_business_day"}
    return {}


def _validate_account_id(account_id: str) -> str:
    safe_account_id = str(account_id or "").strip()
    if not safe_account_id or len(safe_account_id) > 191:
        raise OrchestrationError(
            "INVALID_ACCOUNT_ID",
            "A valid account_id is required before changing credentials",
        )
    return safe_account_id


def _normalize_implicit_account_ids(
    values: Mapping[str, Sequence[str]] | None,
) -> dict[str, frozenset[str]]:
    normalized: dict[str, frozenset[str]] = {}
    for raw_tool_name, raw_account_ids in (values or {}).items():
        tool_name = str(raw_tool_name or "").strip()
        if not tool_name:
            raise ValueError("implicit account mapping contains an empty tool name")
        if isinstance(raw_account_ids, str):
            candidates: Sequence[str] = (raw_account_ids,)
        else:
            candidates = raw_account_ids
        account_ids = frozenset(
            str(value or "").strip()
            for value in candidates
            if str(value or "").strip()
        )
        if not account_ids:
            raise ValueError(
                f"implicit account mapping for {tool_name!r} must not be empty"
            )
        normalized[tool_name] = account_ids
    return normalized


def _collect_account_ids(value: Any, *, key: str = "") -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            name = str(nested_key)
            if name in {"account_id", "accountId"} or name.endswith("_account_id"):
                if isinstance(nested_value, str) and nested_value.strip():
                    result.add(nested_value.strip())
            elif name == "account_ids" and isinstance(nested_value, list):
                result.update(str(item).strip() for item in nested_value if str(item or "").strip())
            result.update(_collect_account_ids(nested_value, key=name))
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_account_ids(item, key=key))
    return result


def _collect_project_account_binding_ids(value: Any) -> set[str]:
    """Collect every account-pool ID from the closed role binding structure."""

    if not isinstance(value, Mapping):
        raise OrchestrationError(
            "ACCOUNT_POLICY_REVOCATION_FAILED",
            "A project has invalid persisted account bindings",
        )
    result: set[str] = set()
    for role, binding in value.items():
        if not str(role or "").strip():
            raise OrchestrationError(
                "ACCOUNT_POLICY_REVOCATION_FAILED",
                "A project account binding has an empty role",
            )
        if isinstance(binding, str):
            account_id = binding.strip()
            if not account_id:
                raise OrchestrationError(
                    "ACCOUNT_POLICY_REVOCATION_FAILED",
                    "A project account binding is empty",
                )
            result.add(account_id)
            continue
        if isinstance(binding, list):
            if not binding or any(
                not isinstance(item, str) or not item.strip() for item in binding
            ):
                raise OrchestrationError(
                    "ACCOUNT_POLICY_REVOCATION_FAILED",
                    "A project collection account binding is invalid",
                )
            account_ids = [item.strip() for item in binding]
            if len(account_ids) != len(set(account_ids)):
                raise OrchestrationError(
                    "ACCOUNT_POLICY_REVOCATION_FAILED",
                    "A project collection account binding contains duplicates",
                )
            result.update(account_ids)
            continue
        raise OrchestrationError(
            "ACCOUNT_POLICY_REVOCATION_FAILED",
            "A project account binding has an unsupported value",
        )
    return result
