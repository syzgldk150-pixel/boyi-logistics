"""Service for audited, per-task scheduled approval configuration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
import uuid

from agent.orchestration.models import Actor, ActorType, OrchestrationError, new_id
from agent.orchestration.policy_engine import ScheduledAllowlistEntry
from agent.orchestration.ports import ToolCatalogPort
from shared.redaction import redact_text
from shared.orchestration_repository import ConcurrentUpdateError, OrchestrationRepository
from shared.scheduled_task_approval import (
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


class ScheduledTaskApprovalService:
    def __init__(
        self,
        repository: OrchestrationRepository,
        catalog: ToolCatalogPort,
        *,
        enabled_finance_platforms: Sequence[str] = (),
        active_account_ids_provider: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._enabled_finance_platforms = tuple(enabled_finance_platforms)
        self._active_account_ids_provider = active_account_ids_provider

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
        with self._repository.unit_of_work() as uow:
            locked_task = uow.scheduled_policies.lock_task(task_id)
            if locked_task is None:
                raise OrchestrationError("SCHEDULE_TASK_NOT_FOUND", "Scheduled task disappeared")
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


def _datetime_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


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
    return {}


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
