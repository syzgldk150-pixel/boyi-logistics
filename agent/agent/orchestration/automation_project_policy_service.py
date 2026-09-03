"""Project-scoped authorization, grouped approvals, and typed invocation.

The service never accepts task identities, plan hashes, contract hashes, or
actor fields from a browser.  Those values are resolved from the immutable
committed plugin generation and locked orchestration rows.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from agent.automation_plugins.catalog import (
    PluginCatalog,
    PluginCatalogEntry,
    project_contract_fragment,
)
from agent.automation_plugins.code_owned_fields import (
    SCAN_PHASE_FORMAL,
    SCAN_PHASE_PREVIEW,
    SELECTION_PHASE_FORMAL,
    SELECTION_PHASE_PREVIEW,
    resolve_scan_execution_phase,
    resolve_selection_execution_phase,
)
from agent.automation_plugins.service_v2_contract import (
    SELECTION_ARGUMENT_FIELDS,
    resolve_service_v2_selection_phase,
)
from agent.orchestration.automation_project_service_v2 import (
    normalize_contribution_id,
    require_active_service_v2_dispatch,
    require_service_v2_policy_mode,
    resolve_invocation_contract_id,
    validate_service_v2_event_context,
    validate_service_v2_compiled_target,
)
from agent.orchestration.automation_project_policy_plan import (
    plan_from_mapping as _plan_from_mapping,
)
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    EntityRef,
    OperationType,
    OrchestrationError,
    Plan,
    RiskLevel,
    new_id,
)
from agent.orchestration.policy_engine import ProjectPolicyEvaluation
from agent.orchestration.automation_project_policy_support import (
    _automation_id,
    _bootstrap_automation_ids,
    _bootstrap_project_is_stable,
    _comment,
    _datetime_text,
    _entrypoint,
    _idempotency_key,
    _pending_set_hash,
    _policy_summary,
    _positive_int,
    _project_denied,
    _request_id,
    _selection_contribution_id,
    _selection_preview_expectation,
    _trusted_context,
    _validate_bootstrap_schedule_set,
)
from agent.orchestration.scan_preview_binding import (
    SCAN_PREVIEW_CONTEXT_KEY,
    ScanPreviewExpectation,
    consume_scan_preview,
    ensure_scan_preview_active,
    is_scan_preview_project,
    normalize_preview_run_id,
    require_scan_formal_governance,
    resolve_scan_preview,
    restore_scan_preview_replay,
    scan_preview_public_projection,
)
from agent.orchestration.selection_preview_binding import (
    SELECTION_PREVIEW_CONTEXT_KEY,
    consume_selection_preview,
    ensure_selection_preview_active,
    is_selection_preview_project,
    resolve_selection_preview,
    selection_confirmation_arguments,
    selection_preview_contribution,
    selection_preview_public_projection,
    restore_selection_preview_replay,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectContractError,
    AutomationProjectInvocation,
    AutomationProjectPolicyMode,
    CompiledAutomationProjectContract,
    OMIT_DYNAMIC_ARGUMENT,
    canonical_sha256,
    compile_automation_project_contract,
)
from shared.automation_project_manifest import AutomationProjectInstanceDefinition
from shared.automation_project_policy_repository import (
    AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ID as _PROJECT_BOOTSTRAP_ACTOR_ID,
    AUTOMATION_PROJECT_BOOTSTRAP_ACTOR_ROLE as _PROJECT_BOOTSTRAP_ACTOR_ROLE,
    AUTOMATION_PROJECT_BOOTSTRAP_COMPLETED_BY as _PROJECT_BOOTSTRAP_COMPLETED_BY,
    AUTOMATION_PROJECT_BOOTSTRAP_EVENT_COMMENT as _PROJECT_BOOTSTRAP_EVENT_COMMENT,
    AUTOMATION_PROJECT_BOOTSTRAP_POLICY_COMMENT as _PROJECT_BOOTSTRAP_POLICY_COMMENT,
    AUTOMATION_PROJECT_BOOTSTRAP_REASON as _PROJECT_BOOTSTRAP_REASON,
    AutomationProjectBootstrapContractError,
    SUPER_ADMIN_PROJECT_POLICY_REASON,
    automation_project_bootstrap_project_set_sha256 as _bootstrap_project_set_sha256,
    automation_project_bootstrap_release_sha as _bootstrap_release_sha,
    automation_project_bootstrap_source_snapshot_sha256,
    automation_project_policy_bootstrap_request_id,
    derive_automation_project_bootstrap_source_snapshot,
    validate_automation_project_configuration_evidence,
    validate_automation_project_bootstrap_policy_event,
    validate_existing_automation_project_bootstrap as _validate_existing_bootstrap,
    validate_initial_automation_project_bootstrap_policy,
    validate_unconfigured_automation_project_policy,
)
from shared.orchestration_repository import ConcurrentUpdateError, InvalidStateError
from shared.orchestration_repository_support import IdempotencyConflict
from shared.redaction import redact_text


logger = logging.getLogger(__name__)


_USER_POLICY_MODES = frozenset(
    {
        AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value,
        AutomationProjectPolicyMode.REQUIRE_EACH_RUN.value,
    }
)
_SCHEDULED_POLICY_EVENT_REQUEST_ID_MAX_LENGTH = 36
_SOURCE_LABELS = {
    "console": "后台",
    "harness": "Harness",
    "scheduler": "定时",
    "feishu": "飞书",
    "webhook": "Webhook",
    "events": "Event",
    "module_slots": "模块扩展",
}
_RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "EXTREME": 3}
_DEFAULT_FULL_AUTO_ACTOR_ID = "system:migration:automation-full-auto-v1"
_DEFAULT_FULL_AUTO_REASON = "AUTOMATION_DEFAULT_FULL_AUTO"
_PROJECT_FULL_AUTO = AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value


class AutomationProjectPolicyService:
    """One authorization authority for every trusted project entrypoint."""

    def __init__(
        self,
        repository: Any,
        core_catalog: Any,
        plugin_catalog: PluginCatalog,
        *,
        command_gateway: CommandGateway | None = None,
        wake_runner: Callable[[str], None] | None = None,
        dynamic_resolver: (Callable[[str, str, Mapping[str, Any]], Any] | None) = None,
        release_hold_provider: Callable[[], bool] | None = None,
        contribution_registry: Any | None = None,
    ) -> None:
        self._repository = repository
        self._core_catalog = core_catalog
        self._plugin_catalog = plugin_catalog
        self._command_gateway = command_gateway
        self._wake_runner = wake_runner
        self._dynamic_resolver = dynamic_resolver
        self._release_hold_provider = release_hold_provider
        self._contribution_registry = contribution_registry

    @property
    def command_gateway(self) -> CommandGateway:
        if self._command_gateway is None:
            raise OrchestrationError("PROJECT_INVOKE_UNAVAILABLE", "Automation project command gateway is unavailable")
        return self._command_gateway

    def list_policies(self) -> dict[str, Any]:
        with self._repository.unit_of_work() as uow:
            policies = {str(row.get("automation_id") or ""): row for row in uow.automation_projects.list_policies()}
        items = [
            self._describe_entry(entry, policies.get(entry.automation_id))
            for entry in self._plugin_catalog.list()
        ]
        return {"items": sorted(items, key=lambda item: item["automation_id"])}

    def get_policy_projection(self, automation_id: str) -> dict[str, Any]:
        safe_id = _automation_id(automation_id)
        entry = self._plugin_catalog.get(safe_id)
        if entry is None:
            raise OrchestrationError(
                "AUTOMATION_PROJECT_NOT_FOUND",
                "Automation project is not installed",
            )
        with self._repository.unit_of_work() as uow:
            policy = uow.automation_projects.get_policy(safe_id)
        return self._describe_entry(entry, policy)

    def bootstrap_legacy_project_policies(
        self,
        *,
        expected_automation_ids: Sequence[str],
        release_sha: str,
    ) -> dict[str, Any]:
        """Transfer only proven legacy schedule grants into project policy.

        The first-party configuration bootstrap has already retired the old
        per-task EXACT rows before generations commit.  Authorization is
        therefore derived only from the immutable original grant plus the
        exact migration-owned retirement event associated with the committed
        project configuration.  The whole 018 marker is one transaction.
        """
        self._require_release_held()
        automation_ids = _bootstrap_automation_ids(expected_automation_ids)
        try:
            safe_release_sha = _bootstrap_release_sha(release_sha)
        except AutomationProjectBootstrapContractError as exc:
            raise OrchestrationError(
                exc.code,
                "Automation project bootstrap requires a full release digest",
            ) from exc
        entries: dict[str, PluginCatalogEntry] = {}
        try:
            for automation_id in automation_ids:
                entry = self._plugin_catalog.require(automation_id)
                if entry.automation_id != automation_id:
                    raise ValueError("catalog identity mismatch")
                entries[automation_id] = entry
        except Exception as exc:
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_SCOPE_INVALID",
                "Automation project bootstrap release scope is unavailable",
            ) from exc
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        bootstrap_actor = Actor(
            actor_type=ActorType.SYSTEM,
            actor_id=_PROJECT_BOOTSTRAP_ACTOR_ID,
            roles=("system",),
            display_name="Automation project bootstrap 018",
            authenticated_by="release_hold",
        )
        created_items: list[dict[str, Any]] = []
        retired_exact_count = 0
        try:
            with self._repository.unit_of_work() as uow:
                marker = uow.automation_projects.get_bootstrap_marker_018(
                    for_update=True
                )
                existing_items = uow.automation_projects.list_bootstrap_items_018(
                    for_update=True
                )
                if marker is not None:
                    result = _validate_existing_bootstrap(
                        marker,
                        existing_items,
                        expected_automation_ids=automation_ids,
                    )
                    uow.commit()
                    return result
                if existing_items:
                    raise OrchestrationError(
                        "PROJECT_POLICY_BOOTSTRAP_PARTIAL",
                        "Automation project bootstrap is partially persisted",
                    )
                for automation_id in automation_ids:
                    entry = entries[automation_id]
                    project = uow.automation_plugins.get_project(
                        automation_id,
                        for_update=True,
                    )
                    if not _bootstrap_project_is_stable(project):
                        raise OrchestrationError(
                            "PROJECT_POLICY_BOOTSTRAP_CONTRACT_INVALID",
                            "Automation project generation is not a stable migration project",
                        )
                    contract, config = self._lock_and_compile_contract(
                        uow,
                        entry,
                        require_enabled=True,
                        lock_rows=True,
                    )
                    rows = uow.automation_projects.list_configuration_rows(
                        automation_id,
                        for_update=True,
                    )
                    legacy_rows = (
                        uow.automation_projects.list_automation_identity_backup_rows_018(
                            tuple(str(row.get("id") or "") for row in rows),
                            for_update=True,
                        )
                    )
                    _validate_bootstrap_schedule_set(entry, contract, rows)
                    policy = uow.automation_projects.get_policy(
                        automation_id,
                        for_update=True,
                    )
                    validate_unconfigured_automation_project_policy(
                        policy,
                        automation_generation=contract.automation_generation,
                        project_configuration_version=(
                            contract.project_configuration_version
                        ),
                    )
                    policy_events = uow.automation_projects.list_policy_events(
                        automation_id,
                        for_update=True,
                    )
                    configuration_evidence = (
                        uow.automation_projects.list_configuration_event_evidence(
                            automation_id,
                            project_configuration_version=(
                                contract.project_configuration_version
                            ),
                            for_update=True,
                        )
                    )
                    configuration_evidence_binding = (
                        validate_automation_project_configuration_evidence(
                        automation_id=automation_id,
                        release_sha=safe_release_sha,
                        config=config,
                        automation_generation=contract.automation_generation,
                        project_configuration_version=(
                            contract.project_configuration_version
                        ),
                        scheduled_task_count=len(rows),
                        policy_events=policy_events,
                        evidence_rows=configuration_evidence,
                    )
                    )
                    configuration_request_id = configuration_evidence_binding[
                        "request_id"
                    ]
                    scheduled_events = (
                        uow.automation_projects.list_scheduled_policy_events(
                            automation_id,
                            for_update=True,
                        )
                    )
                    (
                        source_snapshot,
                        legacy_authorized,
                        project_retired_count,
                    ) = derive_automation_project_bootstrap_source_snapshot(
                            automation_id=automation_id,
                            automation_generation=contract.automation_generation,
                            project_configuration_version=(
                                contract.project_configuration_version
                            ),
                            contract_hash=contract.contract_hash,
                            configuration_request_id=configuration_request_id,
                            configuration_event_metadata_sha256=(
                                configuration_evidence_binding["metadata_sha256"]
                            ),
                            rows=rows,
                            legacy_rows=legacy_rows,
                            scheduled_events=scheduled_events,
                    )
                    retired_exact_count += project_retired_count
                    initial_mode = (
                        AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value
                        if rows and legacy_authorized
                        else AutomationProjectPolicyMode.REQUIRE_EACH_RUN.value
                    )
                    source_set_sha256 = (
                        automation_project_bootstrap_source_snapshot_sha256(
                            source_snapshot
                        )
                    )
                    project_request_id = (
                        automation_project_policy_bootstrap_request_id(
                            automation_id
                        )
                    )
                    if uow.automation_projects.get_event_by_request(
                        automation_id,
                        project_request_id,
                        for_update=True,
                    ) is not None:
                        raise OrchestrationError(
                            "PROJECT_POLICY_BOOTSTRAP_PARTIAL",
                            "Automation project bootstrap event exists without its marker",
                        )
                    correlation_id = project_request_id
                    if initial_mode == AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value:
                        updated = uow.automation_projects.update_policy(
                            automation_id,
                            expected_version=int(policy["version"]),
                            mode=initial_mode,
                            contract_hash=contract.contract_hash,
                            contract_snapshot=contract.snapshot,
                            tool_contract_hash=contract.tool_contract_hash,
                            plugin_contract_hash=contract.plugin_contract_hash,
                            project_generation=contract.automation_generation,
                            project_configuration_version=(
                                contract.project_configuration_version
                            ),
                            actor_id=_PROJECT_BOOTSTRAP_ACTOR_ID,
                            actor_role=_PROJECT_BOOTSTRAP_ACTOR_ROLE,
                            actor_display_name=bootstrap_actor.display_name,
                            comment=_PROJECT_BOOTSTRAP_POLICY_COMMENT,
                        )
                        event_contract_hash: str | None = contract.contract_hash
                        event_contract_snapshot: Mapping[str, Any] | None = (
                            contract.snapshot
                        )
                        event_tool_hash: str | None = contract.tool_contract_hash
                        event_plugin_hash: str | None = contract.plugin_contract_hash
                    else:
                        updated = policy
                        event_contract_hash = None
                        event_contract_snapshot = None
                        event_tool_hash = None
                        event_plugin_hash = None
                    bootstrap_event = uow.automation_projects.append_event(
                        {
                            "automation_id": automation_id,
                            "request_id": project_request_id,
                            "from_mode": policy.get("mode"),
                            "to_mode": initial_mode,
                            "contract_hash": event_contract_hash,
                            "contract_snapshot_json": event_contract_snapshot,
                            "tool_contract_hash": event_tool_hash,
                            "plugin_contract_hash": event_plugin_hash,
                            "project_generation": contract.automation_generation,
                            "project_configuration_version": (
                                contract.project_configuration_version
                            ),
                            "actor_id": _PROJECT_BOOTSTRAP_ACTOR_ID,
                            "actor_role": _PROJECT_BOOTSTRAP_ACTOR_ROLE,
                            "actor_display_name": bootstrap_actor.display_name,
                            "reason": _PROJECT_BOOTSTRAP_REASON,
                            "comment": _PROJECT_BOOTSTRAP_EVENT_COMMENT,
                            "correlation_id": correlation_id,
                            "occurred_at": now,
                        }
                    )
                    item = uow.automation_projects.create_bootstrap_item_018(
                        automation_id=automation_id,
                        initial_mode=initial_mode,
                        source_set_sha256=source_set_sha256,
                        source_snapshot=source_snapshot,
                        policy_version=int(updated["version"]),
                    )
                    validate_automation_project_bootstrap_policy_event(
                        bootstrap_event,
                        item=item,
                    )
                    validate_initial_automation_project_bootstrap_policy(
                        updated,
                        item=item,
                        bootstrap_event=bootstrap_event,
                    )
                    created_items.append(dict(item))
                project_set_sha256 = _bootstrap_project_set_sha256(
                    safe_release_sha,
                    created_items,
                )
                uow.automation_projects.create_bootstrap_marker_018(
                    release_sha=safe_release_sha,
                    project_set_sha256=project_set_sha256,
                    completed_by=_PROJECT_BOOTSTRAP_COMPLETED_BY,
                )
                uow.commit()
        except AutomationProjectBootstrapContractError as exc:
            raise OrchestrationError(
                exc.code,
                "Automation project bootstrap evidence is inconsistent",
            ) from exc
        except ConcurrentUpdateError as exc:
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_CONCURRENT_UPDATE",
                "Automation project bootstrap changed concurrently",
            ) from exc
        legacy_count = sum(
            1
            for item in created_items
            if item.get("initial_mode")
            == AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value
        )
        return {
            "status": "created",
            "project_count": len(created_items),
            "legacy_schedule_only": legacy_count,
            "require_each_run": len(created_items) - legacy_count,
            "retired_scheduled_exact": retired_exact_count,
            "project_set_sha256": _bootstrap_project_set_sha256(
                safe_release_sha,
                created_items,
            ),
        }

    def ensure_default_full_auto_policies(self) -> dict[str, int]:
        """Make legacy/default policies durable full-auto without touching runtime.

        Migration 020 handles existing databases.  This startup pass is only
        for policies freshly created by the release-held 018 evidence
        bootstrap after SQL migrations have completed.  Its audit marker also
        makes a later explicit administrator choice authoritative.
        """
        changed = 0
        with self._repository.unit_of_work() as uow:
            policy_ids = tuple(
                str(row.get("automation_id") or "")
                for row in uow.automation_projects.list_policies()
                if str(row.get("mode") or "")
                in {
                    AutomationProjectPolicyMode.REQUIRE_EACH_RUN.value,
                    AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value,
                }
            )
            for automation_id in policy_ids:
                policy = uow.automation_projects.get_policy(
                    automation_id,
                    for_update=True,
                )
                if policy is None or str(policy.get("mode") or "") not in {
                    AutomationProjectPolicyMode.REQUIRE_EACH_RUN.value,
                    AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value,
                }:
                    continue
                policy_events = uow.automation_projects.list_policy_events(
                    automation_id,
                    for_update=True,
                )
                if any(
                    str(event.get("reason") or "")
                    == SUPER_ADMIN_PROJECT_POLICY_REASON
                    for event in policy_events
                ):
                    # A real administrator choice always wins, including when
                    # a process restarted after the 018 bootstrap but before
                    # this one-time default marker was written.
                    continue
                request_id = f"default-full-auto:{automation_id}"
                if uow.automation_projects.get_event_by_request(
                    automation_id,
                    request_id,
                    for_update=True,
                ) is not None:
                    # The one-time default was already applied.  If an
                    # administrator later selected REQUIRE_EACH_RUN, startup
                    # must preserve that explicit choice instead of treating
                    # it as a partial migration or changing it back.
                    continue
                previous_mode = str(policy["mode"])
                updated = uow.automation_projects.update_policy(
                    automation_id,
                    expected_version=int(policy["version"]),
                    mode=AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value,
                    contract_hash=None,
                    contract_snapshot=None,
                    tool_contract_hash=None,
                    plugin_contract_hash=None,
                    project_generation=int(policy["project_generation"]),
                    project_configuration_version=int(
                        policy["project_configuration_version"]
                    ),
                    actor_id=_DEFAULT_FULL_AUTO_ACTOR_ID,
                    actor_role="system",
                    actor_display_name="Automation full-auto migration",
                    comment="Defaulted automation project to durable full auto",
                )
                correlation_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"boyi:automation-default-full-auto:{automation_id}",
                    )
                )
                uow.automation_projects.append_event(
                    {
                        "automation_id": automation_id,
                        "request_id": request_id,
                        "from_mode": previous_mode,
                        "to_mode": AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value,
                        "contract_hash": None,
                        "contract_snapshot_json": None,
                        "tool_contract_hash": None,
                        "plugin_contract_hash": None,
                        "project_generation": updated["project_generation"],
                        "project_configuration_version": updated[
                            "project_configuration_version"
                        ],
                        "actor_id": _DEFAULT_FULL_AUTO_ACTOR_ID,
                        "actor_role": "system",
                        "actor_display_name": "Automation full-auto migration",
                        "reason": _DEFAULT_FULL_AUTO_REASON,
                        "comment": "Defaulted automation project to durable full auto",
                        "correlation_id": correlation_id,
                    }
                )
                changed += 1
            uow.commit()
        return {"changed": changed}

    def update_policy(
        self,
        automation_id: str,
        *,
        mode: str,
        request_id: str,
        comment: str,
        expected_policy_version: int,
        expected_project_configuration_version: int,
        actor: Actor,
    ) -> dict[str, Any]:
        self._require_release_active()
        self._require_super_admin(actor)
        safe_id = _automation_id(automation_id)
        safe_mode = str(mode or "").strip().upper()
        if safe_mode not in _USER_POLICY_MODES:
            raise OrchestrationError(
                "INVALID_APPROVAL_POLICY_MODE",
                "Project policy must be full auto or require each run",
            )
        catalog_entry = self._plugin_catalog.require(safe_id)
        require_service_v2_policy_mode(catalog_entry, safe_mode)
        safe_request_id = _request_id(request_id)
        safe_comment = _comment(comment)
        expected_policy = _positive_int(
            expected_policy_version,
            "expected_policy_version",
        )
        expected_configuration = _positive_int(
            expected_project_configuration_version,
            "expected_project_configuration_version",
        )
        wake_run_ids: tuple[str, ...] = ()
        correlation_id = new_id()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            with self._repository.unit_of_work() as uow:
                project = uow.automation_plugins.get_project(safe_id, for_update=True)
                if project is None:
                    raise OrchestrationError(
                        "AUTOMATION_PROJECT_NOT_FOUND",
                        "Automation project is not installed",
                    )
                config = uow.automation_plugins.get_project_config(
                    safe_id,
                    for_update=True,
                )
                if config is None:
                    raise OrchestrationError(
                        "PROJECT_CONFIGURATION_MISSING",
                        "Automation project configuration is not initialized",
                    )
                policy = uow.automation_projects.get_policy(
                    safe_id,
                    for_update=True,
                )
                if policy is None:
                    raise OrchestrationError(
                        "PROJECT_POLICY_NOT_INITIALIZED",
                        "Automation project policy is not initialized",
                    )
                prior = uow.automation_projects.get_event_by_request(
                    safe_id,
                    safe_request_id,
                    for_update=True,
                )
                if prior is not None:
                    self._validate_policy_replay(
                        prior,
                        current=policy,
                        mode=safe_mode,
                        comment=safe_comment,
                        actor=actor,
                    )
                    uow.commit()
                    return self.get_policy_projection(safe_id)
                if int(config.get("config_version") or 0) != expected_configuration:
                    raise OrchestrationError(
                        "PROJECT_CONFIGURATION_CHANGED",
                        "Automation project configuration changed; refresh and retry",
                    )
                if int(policy.get("version") or 0) != expected_policy:
                    raise OrchestrationError(
                        "PROJECT_POLICY_CHANGED",
                        "Automation project policy changed; refresh and retry",
                    )
                # Approval policy is durable administrator intent.  Changing it
                # must not create or reconcile a runtime generation: runtime
                # lineage is owned exclusively by project/plugin configuration.
                project_generation = int(
                    policy.get("project_generation")
                    or project.get("target_generation")
                    or project.get("committed_generation")
                    or 1
                )
                contract_hash = None
                contract_snapshot = None
                tool_contract_hash = None
                plugin_contract_hash = None
                updated = uow.automation_projects.update_policy(
                    safe_id,
                    expected_version=expected_policy,
                    mode=safe_mode,
                    contract_hash=contract_hash,
                    contract_snapshot=contract_snapshot,
                    tool_contract_hash=tool_contract_hash,
                    plugin_contract_hash=plugin_contract_hash,
                    project_generation=project_generation,
                    project_configuration_version=expected_configuration,
                    actor_id=actor.actor_id,
                    actor_role="super_admin",
                    actor_display_name=actor.display_name or None,
                    comment=safe_comment,
                )
                uow.automation_projects.append_event(
                    {
                        "automation_id": safe_id,
                        "request_id": safe_request_id,
                        "from_mode": policy.get("mode"),
                        "to_mode": safe_mode,
                        "contract_hash": contract_hash,
                        "contract_snapshot_json": contract_snapshot,
                        "tool_contract_hash": tool_contract_hash,
                        "plugin_contract_hash": plugin_contract_hash,
                        "project_generation": project_generation,
                        "project_configuration_version": expected_configuration,
                        "actor_id": actor.actor_id,
                        "actor_role": "super_admin",
                        "actor_display_name": actor.display_name or None,
                        "reason": SUPER_ADMIN_PROJECT_POLICY_REASON,
                        "comment": safe_comment,
                        "correlation_id": correlation_id,
                        "occurred_at": now,
                    }
                )
                self._retire_legacy_schedule_policies(
                    uow,
                    automation_id=safe_id,
                    rows=uow.automation_projects.list_configuration_rows(
                        safe_id,
                        for_update=True,
                    ),
                    actor=actor,
                    request_id=safe_request_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                )
                self._append_policy_domain_event(
                    uow,
                    automation_id=safe_id,
                    mode=safe_mode,
                    version=int(updated["version"]),
                    actor=actor,
                    request_id=safe_request_id,
                    correlation_id=correlation_id,
                    occurred_at=now,
                )
                wake_run_ids = (
                    uow.automation_projects.invalidate_pending_approvals_and_wake_runs(
                        safe_id,
                        event_repository=uow.events,
                    )
                )
                uow.commit()
        except ConcurrentUpdateError as exc:
            raise OrchestrationError(
                "PROJECT_POLICY_CHANGED",
                "Automation project policy changed; refresh and retry",
            ) from exc
        except IdempotencyConflict as exc:
            raise OrchestrationError(
                "REQUEST_ID_REUSED",
                "Request id was already used for a different project policy change",
            ) from exc
        for run_id in wake_run_ids:
            if self._wake_runner is not None:
                self._wake_runner(run_id)
        return self.get_policy_projection(safe_id)

    def pending_approvals(self, automation_id: str, *, actor: Actor) -> dict[str, Any]:
        safe_id = _automation_id(automation_id)
        self._require_console_admin(actor)
        with self._repository.unit_of_work() as uow:
            if uow.automation_plugins.get_project(safe_id) is None:
                raise OrchestrationError(
                    "AUTOMATION_PROJECT_NOT_FOUND",
                    "Automation project is not installed",
                )
            rows = uow.automation_projects.list_pending_approvals(safe_id)
            projection = self._pending_projection(safe_id, rows, actor=actor)
        return projection

    def decide_pending_approvals(
        self,
        automation_id: str,
        *,
        decision: str,
        expected_pending_set_hash: str,
        request_id: str,
        comment: str,
        actor: Actor,
    ) -> dict[str, Any]:
        self._require_release_active()
        self._require_super_admin(actor)
        safe_id = _automation_id(automation_id)
        normalized_decision = str(decision or "").strip().upper()
        if normalized_decision not in {"APPROVED", "REJECTED"}:
            raise OrchestrationError(
                "INVALID_APPROVAL_DECISION",
                "Project approval decision must be approved or rejected",
            )
        safe_expected_hash = str(expected_pending_set_hash or "").strip().lower()
        if len(safe_expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in safe_expected_hash
        ):
            raise OrchestrationError(
                "EXPECTED_PENDING_SET_HASH_REQUIRED",
                "A valid pending set hash is required",
            )
        safe_request_id = _request_id(request_id)
        safe_comment = _comment(comment)
        decided_run_ids: list[str] = []
        try:
            with self._repository.unit_of_work() as uow:
                project = uow.automation_plugins.get_project(safe_id, for_update=True)
                if project is None:
                    raise OrchestrationError(
                        "AUTOMATION_PROJECT_NOT_FOUND",
                        "Automation project is not installed",
                    )
                replay = uow.automation_projects.get_batch_by_request(
                    safe_id,
                    safe_request_id,
                    for_update=True,
                )
                if replay is not None:
                    result = self._validate_batch_replay(
                        replay,
                        decision=normalized_decision,
                        expected_hash=safe_expected_hash,
                        actor=actor,
                        comment=safe_comment,
                    )
                    uow.commit()
                    return result
                # Lock every waiting Run before locking approval rows, matching
                # individual decision and execution-consumption lock order.
                uow.automation_projects.lock_waiting_approval_runs(safe_id)
                uow.automation_projects.expire_pending_approvals(safe_id)
                rows = uow.automation_projects.list_pending_approvals(
                    safe_id,
                    for_update=True,
                )
                current_hash = _pending_set_hash(rows)
                if current_hash != safe_expected_hash:
                    latest = self._pending_projection(safe_id, rows, actor=actor)
                    raise OrchestrationError(
                        "PENDING_SET_CHANGED",
                        "Pending approval set changed; refresh and retry",
                        details={"pending": latest},
                    )
                self._validate_pending_rows(
                    uow,
                    project=project,
                    automation_id=safe_id,
                    rows=rows,
                    approving=normalized_decision == "APPROVED",
                )
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                for row in rows:
                    decided = uow.approvals.record_decision(
                        {
                            "decision_id": new_id(),
                            "approval_id": row["approval_id"],
                            "actor_type": actor.actor_type.value,
                            "actor_id": actor.actor_id,
                            "actor_roles": list(actor.roles),
                            "decision": normalized_decision,
                            "reason": safe_comment,
                            "decided_at": now,
                        },
                        expected_plan_hash=str(row["plan_hash"]),
                    )
                    if decided.get("_decision_error"):
                        raise InvalidStateError(
                            "a pending approval expired during the grouped decision"
                        )
                    uow.runs.make_waiting_approval_runnable(str(row["run_id"]))
                    self._append_approval_domain_event(
                        uow,
                        row=row,
                        actor=actor,
                        decision=normalized_decision,
                        comment=safe_comment,
                        occurred_at=now,
                    )
                    decided_run_ids.append(str(row["run_id"]))
                remaining = uow.automation_projects.list_pending_approvals(
                    safe_id,
                    for_update=True,
                )
                result = {
                    **self._pending_projection(safe_id, remaining, actor=actor),
                    "decision": normalized_decision,
                    "decided_count": len(rows),
                    "run_receipts": [
                        {
                            "automation_id": safe_id,
                            "work_item_id": str(row["work_item_id"]),
                            "run_id": str(row["run_id"]),
                            # The Run is still locked in WAITING_APPROVAL while the
                            # decision commits; runner wake happens after this UOW.
                            # This is the last status the receipt can attest.
                            "status": str(
                                row.get("run_status") or "WAITING_APPROVAL"
                            ).upper(),
                        }
                        for row in rows
                    ],
                }
                uow.automation_projects.create_batch(
                    {
                        "batch_id": new_id(),
                        "automation_id": safe_id,
                        "request_id": safe_request_id,
                        "decision": normalized_decision,
                        "expected_pending_set_hash": safe_expected_hash,
                        "decided_pending_set_hash": current_hash,
                        "decided_count": len(rows),
                        "actor_id": actor.actor_id,
                        "actor_role": "super_admin",
                        "comment": safe_comment,
                        "result_json": result,
                    }
                )
                uow.commit()
        except InvalidStateError as exc:
            latest = self.pending_approvals(safe_id, actor=actor)
            raise OrchestrationError(
                "PENDING_SET_CHANGED",
                "A pending approval changed during the grouped decision",
                details={"pending": latest},
            ) from exc
        except IdempotencyConflict as exc:
            raise OrchestrationError(
                "REQUEST_ID_REUSED",
                "Request id was already used for a different grouped decision",
            ) from exc
        for run_id in sorted(set(decided_run_ids)):
            if self._wake_runner is not None:
                self._wake_runner(run_id)
        return result

    def get_scan_preview_projection(
        self,
        automation_id: str,
        *,
        preview_run_id: str,
    ) -> dict[str, Any]:
        """Project one verified preview without exposing persisted item evidence."""
        safe_id = _automation_id(automation_id)
        entry, contract = self._load_contract(safe_id)
        if not is_scan_preview_project(entry):
            raise OrchestrationError(
                "SCAN_PREVIEW_PROJECT_INVALID",
                "A scan preview is only available for the signed scan project",
                details={"status": "BLOCKED_DATA"},
            )
        expectation = ScanPreviewExpectation(
            project_instance_id=safe_id,
            generation=contract.automation_generation,
            contract_digest=contract.contract_hash,
            configuration_version=contract.project_configuration_version,
        )
        with self._repository.unit_of_work() as uow:
            return scan_preview_public_projection(
                uow,
                preview_run_id=preview_run_id,
                expectation=expectation,
                now=datetime.now(timezone.utc),
            )

    def get_selection_preview_projection(
        self,
        automation_id: str,
        *,
        preview_run_id: str,
    ) -> dict[str, Any]:
        safe_id = _automation_id(automation_id)
        entry, contract = self._load_contract(safe_id)
        if not is_selection_preview_project(entry):
            raise OrchestrationError(
                "SELECTION_PREVIEW_PROJECT_INVALID",
                "该自动化不支持后台候选选择。",
                details={"status": "BLOCKED_DATA"},
            )
        expectation = _selection_preview_expectation(
            entry,
            contract,
            entrypoint=AutomationEntrypoint.CONSOLE.value,
            contribution_id=_selection_contribution_id(
                entry,
                AutomationEntrypoint.CONSOLE,
            ),
        )
        with self._repository.unit_of_work() as uow:
            return selection_preview_public_projection(
                uow,
                preview_run_id=preview_run_id,
                expectation=expectation,
                now=datetime.now(timezone.utc),
            )

    def invoke_selection_preview(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor: Actor,
    ) -> Any:
        safe_id = _automation_id(automation_id)
        entry, _contract = self._load_contract(safe_id)
        if not is_selection_preview_project(entry):
            raise OrchestrationError(
                "SELECTION_PREVIEW_PROJECT_INVALID",
                "该自动化不支持后台候选选择。",
            )
        contribution_id = _selection_contribution_id(
            entry,
            AutomationEntrypoint.CONSOLE,
        )
        return self.invoke_trusted(
            safe_id,
            entrypoint=AutomationEntrypoint.CONSOLE,
            request_id=request_id,
            actor=actor,
            trusted_context={
                "dynamic_inputs": {
                    "dry_run": True,
                    "selected_bill_codes": [],
                    "preview_fingerprint": "",
                }
            },
            contribution_id=contribution_id,
        )

    def confirm_selection_preview(
        self,
        automation_id: str,
        *,
        preview_run_id: str,
        selected_bill_codes: Sequence[str],
        request_id: str,
        actor: Actor,
    ) -> Any:
        safe_id = _automation_id(automation_id)
        entry = self._load_catalog_entry(safe_id)
        if not is_selection_preview_project(entry):
            raise OrchestrationError(
                "SELECTION_PREVIEW_PROJECT_INVALID",
                "该自动化不支持后台候选选择。",
            )
        safe_preview_run_id = normalize_preview_run_id(preview_run_id)
        idempotency_key = (
            f"automation:{safe_id}:console:{actor.actor_id}:"
            f"selection:{safe_preview_run_id}:{request_id}"
        )
        if str(getattr(entry, "runtime_model", "ACTION_V1") or "ACTION_V1") == "SERVICE_V2":
            contribution_id = _selection_contribution_id(
                entry,
                AutomationEntrypoint.CONSOLE,
            )
            return self.invoke_trusted(
                safe_id,
                entrypoint=AutomationEntrypoint.CONSOLE,
                request_id=request_id,
                actor=actor,
                trusted_context={},
                selection_preview_run_id=safe_preview_run_id,
                selected_bill_codes=selected_bill_codes,
                contribution_id=contribution_id,
                idempotency_key=idempotency_key,
            )
        entry, contract = self._load_contract(safe_id)
        expectation = _selection_preview_expectation(entry, contract)
        with self._repository.unit_of_work() as uow:
            arguments = selection_confirmation_arguments(
                uow,
                preview_run_id=safe_preview_run_id,
                expectation=expectation,
                selected_bill_codes=selected_bill_codes,
                now=datetime.now(timezone.utc),
            )
        contribution_id = _selection_contribution_id(
            entry,
            AutomationEntrypoint.CONSOLE,
        )
        return self.invoke_trusted(
            safe_id,
            entrypoint=AutomationEntrypoint.CONSOLE,
            request_id=request_id,
            actor=actor,
            trusted_context={"dynamic_inputs": arguments},
            contribution_id=contribution_id,
            idempotency_key=idempotency_key,
        )

    async def confirm_selection_preview_and_wait(
        self,
        automation_id: str,
        *,
        preview_run_id: str,
        selected_bill_codes: Sequence[str],
        entrypoint: AutomationEntrypoint | str,
        request_id: str,
        actor: Actor,
        trusted_context: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_automation_generation: int | None = None,
        expected_project_configuration_version: int | None = None,
        timeout_seconds: float = 1800.0,
    ) -> dict[str, Any]:
        """Confirm a persisted selection without accepting its fingerprint.

        Trusted transport adapters provide only the preview run identity and
        the user's selected bill codes.  The immutable fingerprint and formal
        action arguments are restored from the verified persisted preview.
        """
        safe_id = _automation_id(automation_id)
        entry = self._load_catalog_entry(safe_id)
        if not is_selection_preview_project(entry):
            raise OrchestrationError(
                "SELECTION_PREVIEW_PROJECT_INVALID",
                "该自动化不支持后台候选选择。",
            )
        context = dict(trusted_context or {})
        if "dynamic_inputs" in context:
            raise OrchestrationError(
                "SELECTION_INPUT_INVALID",
                "Selection confirmation inputs must be restored by the server",
                details={"status": "BLOCKED_DATA"},
            )
        safe_preview_run_id = normalize_preview_run_id(preview_run_id)
        safe_entrypoint = _entrypoint(entrypoint)
        if str(getattr(entry, "runtime_model", "ACTION_V1") or "ACTION_V1") == "SERVICE_V2":
            contribution_id = _selection_contribution_id(entry, safe_entrypoint)
            return await self.invoke_trusted_and_wait(
                safe_id,
                entrypoint=safe_entrypoint,
                request_id=request_id,
                actor=actor,
                trusted_context=context,
                selection_preview_run_id=safe_preview_run_id,
                selected_bill_codes=selected_bill_codes,
                contribution_id=contribution_id,
                idempotency_key=idempotency_key,
                expected_automation_generation=expected_automation_generation,
                expected_project_configuration_version=(
                    expected_project_configuration_version
                ),
                timeout_seconds=timeout_seconds,
            )
        entry, contract = self._load_contract(safe_id)
        expectation = _selection_preview_expectation(entry, contract)
        with self._repository.unit_of_work() as uow:
            arguments = selection_confirmation_arguments(
                uow,
                preview_run_id=safe_preview_run_id,
                expectation=expectation,
                selected_bill_codes=selected_bill_codes,
                now=datetime.now(timezone.utc),
            )
        context["dynamic_inputs"] = arguments
        contribution_id = _selection_contribution_id(entry, safe_entrypoint)
        return await self.invoke_trusted_and_wait(
            safe_id,
            entrypoint=safe_entrypoint,
            request_id=request_id,
            actor=actor,
            trusted_context=context,
            contribution_id=contribution_id,
            idempotency_key=idempotency_key,
            expected_automation_generation=expected_automation_generation,
            expected_project_configuration_version=(
                expected_project_configuration_version
            ),
            timeout_seconds=timeout_seconds,
        )

    def invoke_console(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor: Actor,
        preview_run_id: str | None = None,
        contribution_id: str | None = None,
    ) -> Any:
        return self.invoke_trusted(
            automation_id,
            entrypoint=AutomationEntrypoint.CONSOLE,
            request_id=request_id,
            actor=actor,
            preview_run_id=preview_run_id,
            contribution_id=contribution_id,
        )

    def invoke_harness(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor: Actor,
        expected_automation_generation: int,
        contribution_id: str,
    ) -> Any:
        """Submit one read-only Service V2 contribution from Harness.

        Harness may select only an opaque catalog entry.  It cannot supply
        project arguments or trusted transport context; both remain compiled
        and server-owned at the exact active generation.
        """
        return self.invoke_trusted(
            automation_id,
            entrypoint=AutomationEntrypoint.HARNESS,
            request_id=request_id,
            actor=actor,
            expected_automation_generation=expected_automation_generation,
            contribution_id=contribution_id,
        )

    def invoke_trusted(
        self,
        automation_id: str,
        *,
        entrypoint: AutomationEntrypoint | str,
        request_id: str,
        actor: Actor,
        trusted_context: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_automation_generation: int | None = None,
        expected_project_configuration_version: int | None = None,
        preview_run_id: str | None = None,
        selection_preview_run_id: str | None = None,
        selected_bill_codes: Sequence[str] | None = None,
        contribution_id: str | None = None,
    ) -> Any:
        """Submit one server-resolved invocation for a trusted entry adapter.

        The adapter supplies only transport facts that it has already verified.
        It cannot supply action arguments or any project/contract identity.  The
        latter are compiled from the immutable committed generation and locked
        again in the same Unit of Work that accepts the Command.
        """
        self._require_release_active()
        if self._command_gateway is None:
            raise OrchestrationError(
                "PROJECT_INVOKE_UNAVAILABLE",
                "Automation project command gateway is unavailable",
            )
        source = _entrypoint(entrypoint)
        self._require_trusted_entrypoint_actor(source, actor)
        safe_id = _automation_id(automation_id)
        safe_request_id = _request_id(request_id)
        safe_contribution_id = normalize_contribution_id(contribution_id)
        contract: CompiledAutomationProjectContract | None = None
        if selection_preview_run_id is not None:
            entry = self._load_catalog_entry(safe_id)
        else:
            entry, contract = self._load_contract(safe_id)
        is_service_v2 = getattr(entry, "runtime_model", "ACTION_V1") == "SERVICE_V2"
        if source is AutomationEntrypoint.EVENTS and not is_service_v2:
            raise OrchestrationError(
                "PROJECT_ENTRYPOINT_DISABLED",
                "Action V1 projects cannot invoke the managed Event entrypoint",
            )
        if (
            source
            in {
                AutomationEntrypoint.HARNESS,
                AutomationEntrypoint.MODULE_SLOTS,
            }
            and not is_service_v2
        ):
            raise OrchestrationError(
                "PROJECT_ENTRYPOINT_DISABLED",
                "Managed entrypoint accepts only Service V2 contributions",
            )
        command_idempotency_key = _idempotency_key(
            idempotency_key
            or (
                f"automation:{safe_id}:{source.value}:"
                + (f"{safe_contribution_id or 'default'}:" if is_service_v2 else "")
                + f"{actor.actor_id}:{safe_request_id}"
            )
        )
        context = _trusted_context(source, trusted_context)
        expected_event_name = (
            validate_service_v2_event_context(context, request_id=safe_request_id)
            if is_service_v2 and source is AutomationEntrypoint.EVENTS
            else None
        )
        if is_service_v2 and source is AutomationEntrypoint.WEBHOOK and "dynamic_inputs" in context:
            raise OrchestrationError(
                "TRUSTED_CONTEXT_INVALID",
                "Managed Webhook arguments must come from the signed project contract",
            )
        expected_generation = (
            _positive_int(
                expected_automation_generation,
                "expected_automation_generation",
            )
            if expected_automation_generation is not None
            else None
        )
        expected_configuration = (
            _positive_int(
                expected_project_configuration_version,
                "expected_project_configuration_version",
            )
            if expected_project_configuration_version is not None
            else None
        )
        if source is not AutomationEntrypoint.CONSOLE and expected_generation is None:
            raise OrchestrationError(
                "PROJECT_GENERATION_REQUIRED",
                "Trusted non-Console entrypoints must bind a committed generation",
            )
        scan_preview_project = is_scan_preview_project(entry)
        selection_preview_project = is_selection_preview_project(entry)
        safe_selection_preview_run_id = (
            normalize_preview_run_id(selection_preview_run_id)
            if selection_preview_run_id is not None
            else None
        )
        service_v2_selection_contribution: Mapping[str, Any] | None = None
        expected_selection_contribution_id: str | None = None
        if selection_preview_project and is_service_v2:
            service_v2_selection_contribution = selection_preview_contribution(
                entry,
                source.value,
            )
            expected_selection_contribution_id = str(
                (service_v2_selection_contribution or {}).get("id") or ""
            ).strip()
        is_service_v2_selection_invocation = bool(
            is_service_v2
            and expected_selection_contribution_id
            and safe_contribution_id == expected_selection_contribution_id
        )
        if safe_selection_preview_run_id is not None and (
            not is_service_v2_selection_invocation
        ):
            raise OrchestrationError(
                "SELECTION_PREVIEW_PROJECT_INVALID",
                "Selection preview consumption is unavailable for this project",
                details={"status": "BLOCKED_DATA"},
            )
        if (
            is_service_v2_selection_invocation
            and safe_selection_preview_run_id is None
            and "dynamic_inputs" not in context
        ):
            raise OrchestrationError(
                "SELECTION_INPUT_INVALID",
                "Service v2 selection inputs must be resolved by the Host",
                details={"status": "BLOCKED_DATA"},
            )
        if safe_selection_preview_run_id is not None:
            if selected_bill_codes is None or "dynamic_inputs" in context:
                raise OrchestrationError(
                    "SELECTION_INPUT_INVALID",
                    "Selection confirmation accepts only Host-resolved bill codes",
                    details={"status": "BLOCKED_DATA"},
                )
            with self._repository.unit_of_work() as uow:
                replay = restore_selection_preview_replay(
                    uow,
                    source=source.value,
                    idempotency_key=command_idempotency_key,
                    actor=actor,
                    trusted_context=context,
                    project_instance_id=safe_id,
                    request_id=safe_request_id,
                    preview_run_id=safe_selection_preview_run_id,
                    selected_bill_codes=selected_bill_codes,
                    expected_entrypoint=source.value,
                    expected_contribution_id=expected_selection_contribution_id,
                    expected_generation=expected_generation,
                    expected_configuration_version=expected_configuration,
                )
            if replay is not None:
                return self._command_gateway.submit(replay)
        elif selected_bill_codes is not None:
            raise OrchestrationError(
                "SELECTION_INPUT_INVALID",
                "Bill-code selection requires one persisted preview",
                details={"status": "BLOCKED_DATA"},
            )
        if contract is None:
            entry, contract = self._load_contract(safe_id)
        is_service_v2 = getattr(entry, "runtime_model", "ACTION_V1") == "SERVICE_V2"
        scan_preview_project = is_scan_preview_project(entry)
        selection_preview_project = is_selection_preview_project(entry)
        current_selection = (
            selection_preview_contribution(entry, source.value)
            if selection_preview_project and is_service_v2
            else None
        )
        current_selection_contribution_id = str(
            (current_selection or {}).get("id") or ""
        ).strip()
        is_service_v2_selection_invocation = bool(
            is_service_v2
            and current_selection_contribution_id
            and safe_contribution_id == current_selection_contribution_id
        )
        selection_invocation = bool(
            selection_preview_project
            and (not is_service_v2 or is_service_v2_selection_invocation)
        )
        if safe_selection_preview_run_id is not None:
            if (
                not selection_invocation
                or current_selection_contribution_id
                != expected_selection_contribution_id
            ):
                raise OrchestrationError(
                    "PROJECT_INVOCATION_STALE",
                    "Selection contribution changed before invocation",
                    details={"status": "BLOCKED_DATA"},
                )
        selection_expectation = (
            _selection_preview_expectation(
                entry,
                contract,
                entrypoint=(
                    source.value if safe_selection_preview_run_id is not None else None
                ),
                contribution_id=(
                    current_selection_contribution_id
                    if safe_selection_preview_run_id is not None
                    else None
                ),
            )
            if selection_invocation and safe_selection_preview_run_id is not None
            else None
        )
        safe_preview_run_id = None
        if preview_run_id is not None:
            safe_preview_run_id = normalize_preview_run_id(preview_run_id)
            with self._repository.unit_of_work() as uow:
                replay = restore_scan_preview_replay(
                    uow,
                    source=source.value,
                    idempotency_key=command_idempotency_key,
                    actor=actor,
                    trusted_context=context,
                    project_instance_id=safe_id,
                    request_id=safe_request_id,
                    preview_run_id=safe_preview_run_id,
                    expected_generation=expected_generation,
                    expected_configuration_version=expected_configuration,
                )
            if replay is not None:
                return self._command_gateway.submit(replay)
            require_scan_formal_governance(entry)
        if expected_generation is not None and contract.automation_generation != expected_generation:
            raise OrchestrationError(
                "PROJECT_INVOCATION_STALE",
                "Automation project generation changed before invocation",
            )
        if expected_configuration is not None and contract.project_configuration_version != expected_configuration:
            raise OrchestrationError(
                "PROJECT_INVOCATION_STALE",
                "Automation project configuration changed before invocation",
            )
        invocation_contract_id = resolve_invocation_contract_id(
            contract,
            source=source,
            contribution_id=safe_contribution_id,
            context=context,
        )
        invocation_contract = contract.invocation_contracts.get(invocation_contract_id)
        if invocation_contract is None or invocation_contract.entrypoint != source.value:
            raise OrchestrationError(
                "PROJECT_ENTRYPOINT_DISABLED",
                "Requested entrypoint is not enabled for this automation project",
            )
        managed_projection_source = source in {
            AutomationEntrypoint.CONSOLE,
            AutomationEntrypoint.HARNESS,
            AutomationEntrypoint.SCHEDULER,
            AutomationEntrypoint.FEISHU,
            AutomationEntrypoint.WEBHOOK,
            AutomationEntrypoint.EVENTS,
            AutomationEntrypoint.MODULE_SLOTS,
        }
        if is_service_v2 and managed_projection_source:
            require_active_service_v2_dispatch(
                self._contribution_registry,
                source=source,
                entry=entry,
                automation_id=safe_id,
                generation=contract.automation_generation,
                invocation_contract=invocation_contract,
                context=context,
                expected_event_name=expected_event_name,
            )
        occurred_at = datetime.now(timezone.utc)
        execution_context = {
            "project_request_id": safe_request_id,
            "entrypoint": source.value,
            "occurred_at": occurred_at.isoformat(),
            **context,
        }
        if is_service_v2:
            execution_context["contribution_id"] = invocation_contract.contribution_id
        selection_dynamic_inputs: Mapping[str, Any] | None = None
        selection_context: Mapping[str, Any] | None = None
        service_v2_selection_phase: str | None = None
        if selection_invocation and "dynamic_inputs" in context:
            raw_selection_inputs = context["dynamic_inputs"]
            if not isinstance(raw_selection_inputs, Mapping) or set(raw_selection_inputs) != {
                "dry_run",
                "selected_bill_codes",
                "preview_fingerprint",
            }:
                raise OrchestrationError(
                    "SELECTION_INPUT_INVALID",
                    "Selection workflow inputs must come from the server preview flow",
                    details={"status": "BLOCKED_DATA"},
                )
            selection_dynamic_inputs = raw_selection_inputs
            if is_service_v2:
                try:
                    service_v2_selection_phase = resolve_service_v2_selection_phase(
                        raw_selection_inputs
                    )
                except ValueError as exc:
                    raise OrchestrationError(
                        "SELECTION_EXECUTION_PHASE_INVALID",
                        "Service v2 selection phase is incomplete or ambiguous",
                        details={"status": "BLOCKED_DATA"},
                    ) from exc
                execution_context["selection_phase"] = service_v2_selection_phase
                if (
                    service_v2_selection_phase == SELECTION_PHASE_FORMAL
                    and safe_selection_preview_run_id is None
                ) or (
                    service_v2_selection_phase == SELECTION_PHASE_PREVIEW
                    and safe_selection_preview_run_id is not None
                ):
                    raise OrchestrationError(
                        "SELECTION_PREVIEW_REQUIRED",
                        "Formal selection execution requires one persisted preview",
                        details={"status": "BLOCKED_DATA"},
                    )
        arguments = dict(invocation_contract.expected_arguments)
        for field_name, resolver_id in sorted(
            invocation_contract.dynamic_argument_resolvers.items()
        ):
            if self._dynamic_resolver is None:
                raise OrchestrationError(
                    "PROJECT_DYNAMIC_INPUT_UNAVAILABLE",
                    "Project invocation requires a server-owned dynamic resolver",
                )
            try:
                resolved_value = self._dynamic_resolver(
                    resolver_id,
                    field_name,
                    execution_context,
                )
                if resolved_value is OMIT_DYNAMIC_ARGUMENT:
                    continue
                arguments[field_name] = resolved_value
            except Exception as exc:
                raise OrchestrationError(
                    "PROJECT_DYNAMIC_INPUT_UNAVAILABLE",
                    "Dynamic project invocation input could not be resolved",
                ) from exc
        if scan_preview_project and safe_preview_run_id is None:
            arguments["dry_run"] = True
        if safe_selection_preview_run_id is not None:
            if selection_expectation is None or selected_bill_codes is None:
                raise OrchestrationError(
                    "SELECTION_PREVIEW_PROJECT_INVALID",
                    "Selection preview confirmation is unavailable",
                    details={"status": "BLOCKED_DATA"},
                )
            with self._repository.unit_of_work() as uow:
                selection = resolve_selection_preview(
                    uow,
                    preview_run_id=safe_selection_preview_run_id,
                    expectation=selection_expectation,
                    selected_bill_codes=selected_bill_codes,
                    now=occurred_at,
                    for_update=False,
                )
            selection_dynamic_inputs = dict(selection.formal_arguments)
            selection_context = dict(selection.context)
            execution_context["selection_phase"] = SELECTION_PHASE_FORMAL
            execution_context[SELECTION_PREVIEW_CONTEXT_KEY] = selection_context
            execution_context["occurred_at"] = selection_context["observed_at"]
        if selection_dynamic_inputs is not None:
            arguments.update(dict(selection_dynamic_inputs))
        preview_expectation = ScanPreviewExpectation(
            project_instance_id=safe_id,
            generation=contract.automation_generation,
            contract_digest=contract.contract_hash,
            configuration_version=contract.project_configuration_version,
        )
        preview_context: Mapping[str, Any] | None = None
        if safe_preview_run_id is not None:
            with self._repository.unit_of_work() as uow:
                preview = resolve_scan_preview(
                    uow,
                    preview_run_id=safe_preview_run_id,
                    expectation=preview_expectation,
                    formal_arguments=arguments,
                    now=occurred_at,
                    for_update=False,
                )
            arguments = dict(preview.formal_arguments)
            preview_context = dict(preview.context)
            execution_context[SCAN_PREVIEW_CONTEXT_KEY] = preview_context
            # A stable preview observation time makes concurrent retries carry
            # byte-identical immutable Command parameters.
            execution_context["occurred_at"] = preview_context["observed_at"]
        with self._repository.unit_of_work() as uow:
            policy = uow.automation_projects.get_policy(safe_id)
        if policy is None:
            raise OrchestrationError(
                "PROJECT_POLICY_NOT_INITIALIZED",
                "Automation project policy is not initialized",
            )
        policy_version = _positive_int(int(policy.get("version") or 0), "policy_version")
        invocation = AutomationProjectInvocation(
            automation_id=safe_id,
            automation_generation=contract.automation_generation,
            entrypoint=source,
            contract_id=invocation_contract.contract_id,
            contract_hash=contract.contract_hash,
            policy_version=policy_version,
            project_configuration_version=contract.project_configuration_version,
            request_id=safe_request_id,
        )
        command = Command(
            command_type="automation.project.invoke",
            source=source.value,
            actor=actor,
            parameters={
                "tool_name": contract.tool_name,
                "arguments": arguments,
                "execution_context": execution_context,
            },
            idempotency_key=command_idempotency_key,
            entity_refs=(
                EntityRef(
                    entity_type="automation_project",
                    entity_id=safe_id,
                    source_system="agent",
                ),
            ),
            automation_invocation=invocation,
        )

        def guard(uow: Any) -> None:
            selection_confirmation = bool(
                safe_selection_preview_run_id is not None
                and selection_expectation is not None
                and selection_context is not None
                and selected_bill_codes is not None
            )

            def accepted_command_exists(*, for_update: bool) -> bool:
                existing_command = uow.commands.get_by_idempotency(
                    command.source,
                    command.idempotency_key,
                    for_update=for_update,
                )
                if existing_command is None:
                    return False
                if selection_confirmation:
                    restore_selection_preview_replay(
                        uow,
                        source=source.value,
                        idempotency_key=command_idempotency_key,
                        actor=actor,
                        trusted_context=context,
                        project_instance_id=safe_id,
                        request_id=safe_request_id,
                        preview_run_id=safe_selection_preview_run_id,
                        selected_bill_codes=selected_bill_codes,
                        expected_entrypoint=source.value,
                        expected_contribution_id=(
                            expected_selection_contribution_id
                        ),
                        expected_generation=expected_generation,
                        expected_configuration_version=expected_configuration,
                    )
                return True

            if accepted_command_exists(for_update=selection_confirmation):
                return
            locked_contract, _config = self._lock_and_compile_contract(
                uow,
                entry,
                expected=contract,
                require_enabled=True,
            )
            current_policy = uow.automation_projects.get_policy(
                safe_id,
                for_update=True,
            )
            if (
                current_policy is None
                or int(current_policy.get("version") or 0) != policy_version
                or locked_contract.contract_hash != invocation.contract_hash
                or locked_contract.automation_generation != invocation.automation_generation
            ):
                raise OrchestrationError(
                    "PROJECT_INVOCATION_STALE",
                    "Automation project changed before command acceptance",
                )
            if is_service_v2 and source in {
                AutomationEntrypoint.FEISHU,
                AutomationEntrypoint.WEBHOOK,
                AutomationEntrypoint.EVENTS,
                AutomationEntrypoint.MODULE_SLOTS,
            }:
                require_active_service_v2_dispatch(
                    self._contribution_registry,
                    source=source,
                    entry=entry,
                    automation_id=safe_id,
                    generation=locked_contract.automation_generation,
                    invocation_contract=invocation_contract,
                    context=context,
                    expected_event_name=expected_event_name,
                )
            # Project/config rows above serialize all accepted entrypoints for
            # this automation. Recheck the idempotency key after taking that
            # lock so a concurrent retry reuses its original Command, while a
            # distinct Console/Scheduler/Feishu request is rejected before a
            # second Run can be created.
            if accepted_command_exists(for_update=True):
                return
            active_run = uow.runs.get_active_for_automation(
                safe_id,
                for_update=True,
            )
            if active_run is not None:
                raise OrchestrationError(
                    "AUTOMATION_ALREADY_RUNNING",
                    "该脚本已在运行",
                    details={
                        "active_run_id": str(active_run.get("run_id") or ""),
                        "active_status": str(active_run.get("status") or ""),
                    },
                )
            if (
                safe_selection_preview_run_id is not None
                and selection_expectation is not None
                and selection_context is not None
                and selected_bill_codes is not None
            ):
                locked_selection = resolve_selection_preview(
                    uow,
                    preview_run_id=safe_selection_preview_run_id,
                    expectation=selection_expectation,
                    selected_bill_codes=selected_bill_codes,
                    now=occurred_at,
                    for_update=True,
                )
                ensure_selection_preview_active(
                    locked_selection.context,
                    now=datetime.now(timezone.utc),
                )
                accepted_selection_arguments = {
                    field_name: arguments.get(field_name)
                    for field_name in SELECTION_ARGUMENT_FIELDS
                }
                if (
                    dict(locked_selection.formal_arguments)
                    != accepted_selection_arguments
                    or locked_selection.context.get("context_sha256")
                    != selection_context.get("context_sha256")
                ):
                    raise OrchestrationError(
                        "SELECTION_PREVIEW_STALE",
                        "Selection preview changed before command acceptance",
                        details={"status": "BLOCKED_DATA"},
                    )
                consume_selection_preview(
                    uow,
                    expectation=selection_expectation,
                    context=locked_selection.context,
                    command=command,
                    occurred_at=occurred_at,
                )
            if preview_context is not None and safe_preview_run_id is not None:
                locked_preview = resolve_scan_preview(
                    uow,
                    preview_run_id=safe_preview_run_id,
                    expectation=preview_expectation,
                    formal_arguments=arguments,
                    now=occurred_at,
                    for_update=True,
                )
                existing_command = uow.commands.get_by_idempotency(
                    command.source,
                    command.idempotency_key,
                    for_update=True,
                )
                if existing_command is None:
                    ensure_scan_preview_active(
                        locked_preview.context,
                        now=datetime.now(timezone.utc),
                    )
                    if locked_preview.context.get("context_sha256") != preview_context.get("context_sha256"):
                        raise OrchestrationError(
                            "SCAN_PREVIEW_STALE",
                            "The scan preview changed before command acceptance",
                            details={"status": "BLOCKED_DATA"},
                        )
                    consume_scan_preview(
                        uow,
                        context=locked_preview.context,
                        command=command,
                        occurred_at=occurred_at,
                    )

        return self._command_gateway.submit(command, uow_guard=guard)

    async def invoke_trusted_and_wait(
        self,
        automation_id: str,
        *,
        entrypoint: AutomationEntrypoint | str,
        request_id: str,
        actor: Actor,
        trusted_context: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_automation_generation: int | None = None,
        expected_project_configuration_version: int | None = None,
        preview_run_id: str | None = None,
        selection_preview_run_id: str | None = None,
        selected_bill_codes: Sequence[str] | None = None,
        contribution_id: str | None = None,
        timeout_seconds: float = 1800.0,
    ) -> dict[str, Any]:
        receipt = self.invoke_trusted(
            automation_id,
            entrypoint=entrypoint,
            request_id=request_id,
            actor=actor,
            trusted_context=trusted_context,
            idempotency_key=idempotency_key,
            expected_automation_generation=expected_automation_generation,
            expected_project_configuration_version=(expected_project_configuration_version),
            preview_run_id=preview_run_id,
            selection_preview_run_id=selection_preview_run_id,
            selected_bill_codes=selected_bill_codes,
            contribution_id=contribution_id,
        )
        if self._command_gateway is None:  # defensive; invoke_trusted checked it
            raise OrchestrationError(
                "PROJECT_INVOKE_UNAVAILABLE",
                "Automation project command gateway is unavailable",
            )
        run = await self._command_gateway.wait_for_run(
            receipt.run_id,
            timeout_seconds=timeout_seconds,
        )
        status = str(run.get("status") or "")
        result = {
            "success": status == "COMPLETED",
            "status": status,
            "command_id": str(run.get("command_id") or receipt.command_id),
            "work_item_id": str(run.get("work_item_id") or receipt.work_item_id),
            "run_id": str(run.get("run_id") or receipt.run_id),
            "correlation_id": str(run.get("correlation_id") or ""),
            "error_code": str(run.get("error_code") or "").strip() or None,
            "error_summary": (
                redact_text(run.get("error_summary"))[:500] or None
            ),
        }
        if status == "COMPLETED" and preview_run_id is None:
            entry, _contract = self._load_contract(automation_id)
            if is_scan_preview_project(entry):
                result["scan_preview"] = self.get_scan_preview_projection(
                    automation_id,
                    preview_run_id=str(run.get("run_id") or receipt.run_id),
                )
        return result

    def evaluate_invocation(
        self,
        plan: Plan,
        actor: Actor,
        source: str,
        execution_context: Mapping[str, Any],
        invocation: AutomationProjectInvocation,
        project_transaction: Any | None = None,
    ) -> ProjectPolicyEvaluation:
        del actor
        if source != invocation.entrypoint.value:
            return _project_denied(
                "PROJECT_ENTRYPOINT_MISMATCH",
                "Automation invocation source is not its signed entrypoint",
            )
        try:
            entry = self._plugin_catalog.require(invocation.automation_id)
            if project_transaction is None:
                with self._repository.unit_of_work() as uow:
                    contract, _config = self._lock_and_compile_contract(
                        uow,
                        entry,
                        require_enabled=True,
                        lock_rows=False,
                    )
                    policy = uow.automation_projects.get_policy(
                        invocation.automation_id,
                    )
                    policy_events = (
                        uow.automation_projects.list_policy_events(
                            invocation.automation_id,
                        )
                        if is_scan_preview_project(entry)
                        else []
                    )
            else:
                contract, _config = self._lock_and_compile_contract(
                    project_transaction,
                    entry,
                    require_enabled=True,
                    lock_rows=True,
                )
                policy = project_transaction.automation_projects.get_policy(
                    invocation.automation_id,
                    for_update=True,
                )
                policy_events = (
                    project_transaction.automation_projects.list_policy_events(
                        invocation.automation_id,
                        for_update=True,
                    )
                    if is_scan_preview_project(entry)
                    else []
                )
        except (AutomationProjectContractError, KeyError, ValueError):
            return _project_denied(
                "PROJECT_INVOCATION_STALE",
                "Automation project contract is no longer current",
            )
        if policy is None:
            return _project_denied(
                "PROJECT_POLICY_NOT_INITIALIZED",
                "Automation project policy is not initialized",
            )
        # The invocation binds the accepted command to one exact plugin and
        # project-configuration contract.  Policy is different: it is durable
        # administrator intent and must be re-read when a persisted Run is
        # resumed.  Rejecting an older policy_version here would make a
        # REQUIRE_EACH_RUN -> PROJECT_FULL_AUTO change wake a waiting Run only
        # to fail it as stale before the current policy can take effect.
        # Contract/generation/configuration matching below remains strict.
        scan_phase: str | None = None
        selection_phase: str | None = None
        is_service_v2_entry = (
            getattr(entry, "runtime_model", "ACTION_V1") == "SERVICE_V2"
        )
        selection_invocation = False
        if is_selection_preview_project(entry):
            if is_service_v2_entry:
                declaration = selection_preview_contribution(entry, source)
                selection_invocation = bool(
                    declaration is not None
                    and declaration.get("id") == invocation.contract_id
                )
            else:
                selection_invocation = True
        if is_scan_preview_project(entry):
            if len(plan.steps) != 1:
                return _project_denied(
                    "SCAN_EXECUTION_PHASE_INVALID",
                    "Scan execution requires one exact governed step",
                )
            try:
                scan_phase = resolve_scan_execution_phase(
                    automation_id=str(getattr(entry, "automation_id", "") or ""),
                    plugin_id=str(getattr(entry, "plugin_id", "") or ""),
                    trust_source=str(getattr(entry, "trust_source", "") or ""),
                    arguments=plan.steps[0].arguments,
                )
            except ValueError:
                return _project_denied(
                    "SCAN_EXECUTION_PHASE_INVALID",
                    "Scan execution phase is incomplete or ambiguous",
                )
        if selection_invocation:
            if len(plan.steps) != 1:
                return _project_denied(
                    "SELECTION_EXECUTION_PHASE_INVALID",
                    "Selection execution requires one exact governed step",
                )
            try:
                if is_service_v2_entry:
                    selection_phase = resolve_service_v2_selection_phase(
                        plan.steps[0].arguments
                    )
                    if execution_context.get("selection_phase") != selection_phase:
                        raise ValueError("selection phase context drifted")
                else:
                    selection_phase = resolve_selection_execution_phase(
                        automation_id=str(getattr(entry, "automation_id", "") or ""),
                        plugin_id=str(getattr(entry, "plugin_id", "") or ""),
                        trust_source=str(getattr(entry, "trust_source", "") or ""),
                        arguments=plan.steps[0].arguments,
                    )
            except ValueError:
                return _project_denied(
                    "SELECTION_EXECUTION_PHASE_INVALID",
                    "Selection execution phase is incomplete or ambiguous",
                )
        contract_plan = plan
        if scan_phase == SCAN_PHASE_PREVIEW:
            preview_step = plan.steps[0]
            if (
                preview_step.operation_type is not OperationType.READ
                or preview_step.risk_level is not RiskLevel.LOW
            ):
                return _project_denied(
                    "SCAN_EXECUTION_PHASE_INVALID",
                    "Scan preview plan does not use its effective read-only governance",
                )
            try:
                signed_operation = OperationType(contract.operation_type)
            except ValueError:
                return _project_denied(
                    "PROJECT_INVOCATION_STALE",
                    "Automation project contract is no longer current",
                )
            contract_plan = replace(
                plan,
                steps=(replace(preview_step, operation_type=signed_operation),),
            )
        if selection_phase in {SELECTION_PHASE_PREVIEW, SELECTION_PHASE_FORMAL}:
            selection_step = plan.steps[0]
            if selection_phase == SELECTION_PHASE_PREVIEW and (
                selection_step.operation_type is not OperationType.READ
                or selection_step.risk_level is not RiskLevel.LOW
            ):
                return _project_denied(
                    "SELECTION_EXECUTION_PHASE_INVALID",
                    "Selection preview plan does not use read-only governance",
                )
            contract_step = selection_step
            if is_service_v2_entry:
                contract_step = replace(
                    contract_step,
                    arguments={
                        field_name: value
                        for field_name, value in contract_step.arguments.items()
                        if field_name not in SELECTION_ARGUMENT_FIELDS
                    },
                )
            if selection_phase == SELECTION_PHASE_PREVIEW:
                try:
                    signed_operation = OperationType(contract.operation_type)
                except ValueError:
                    return _project_denied(
                        "PROJECT_INVOCATION_STALE",
                        "Automation project contract is no longer current",
                    )
                contract_step = replace(
                    contract_step,
                    operation_type=signed_operation,
                )
            contract_plan = replace(plan, steps=(contract_step,))
        if not contract.matches_plan(
            contract_plan,
            invocation,
            source=source,
            execution_context=execution_context,
            dynamic_resolver=self._dynamic_resolver,
        ):
            if selection_phase is not None:
                diagnostic_step = contract_plan.steps[0]
                diagnostic_contract = contract.invocation_contracts.get(
                    invocation.contract_id
                )
                logger.error(
                    "Selection contract mismatch automation_id=%s "
                    "tool_name_match=%s tool_version_match=%s operation_match=%s "
                    "plan_binding_match=%s expected_argument_keys=%s "
                    "actual_argument_keys=%s dynamic_argument_keys=%s",
                    invocation.automation_id,
                    diagnostic_step.tool_name == contract.tool_name,
                    diagnostic_step.tool_version == contract.tool_version,
                    diagnostic_step.operation_type.value == contract.operation_type,
                    (
                        contract_plan.automation_id == contract.automation_id
                        and contract_plan.automation_generation
                        == contract.automation_generation
                        and contract_plan.automation_contract_hash
                        == contract.contract_hash
                    ),
                    sorted(
                        (diagnostic_contract.expected_arguments or {}).keys()
                        if diagnostic_contract is not None
                        else []
                    ),
                    sorted(diagnostic_step.arguments.keys()),
                    sorted(
                        (diagnostic_contract.dynamic_argument_resolvers or {}).keys()
                        if diagnostic_contract is not None
                        else []
                    ),
                )
            return _project_denied(
                "PROJECT_INVOCATION_STALE",
                "Automation plan does not match the committed project contract",
            )
        if is_scan_preview_project(entry):
            if scan_phase == SCAN_PHASE_PREVIEW:
                return ProjectPolicyEvaluation(
                    allowed=True,
                    requires_approval=False,
                    code="SCAN_PREVIEW_ALLOWED",
                    reason="The governed scan preview is read-only",
                )
        if selection_phase == SELECTION_PHASE_PREVIEW:
            return ProjectPolicyEvaluation(
                allowed=True,
                requires_approval=False,
                code="SELECTION_PREVIEW_ALLOWED",
                reason="The governed selection preview is read-only",
            )
        mode = _PROJECT_FULL_AUTO if getattr(entry, "runtime_model", "ACTION_V1") == "SERVICE_V2" else str(policy.get("mode") or "")
        if mode == AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value:
            legacy_active = self._legacy_schedule_active(policy, contract)
            return ProjectPolicyEvaluation(
                allowed=True,
                requires_approval=(
                    False if source == "scheduler" and legacy_active else True
                ),
                code=(
                    "LEGACY_SCHEDULE_ONLY"
                    if legacy_active
                    else "PROJECT_APPROVAL_REQUIRED"
                ),
                reason=(
                    "Current compiled legacy schedule contract is automatic"
                    if source == "scheduler" and legacy_active
                    else "Legacy project contract is stale or not a scheduler invocation"
                ),
            )
        if mode == AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value:
            if not contract.can_full_auto:
                return _project_denied(
                    contract.restriction_code or "PROJECT_CONTRACT_NOT_RUNNABLE",
                    "The current signed project contract is not runnable",
                )
            return ProjectPolicyEvaluation(
                allowed=True,
                requires_approval=False,
                code="PROJECT_FULL_AUTO",
                reason="Current signed project contract is fully automatic",
            )
        return ProjectPolicyEvaluation(
            allowed=True,
            requires_approval=True,
            code="PROJECT_APPROVAL_REQUIRED",
            reason="Current project policy requires a separate approval",
        )

    def _load_contract(
        self,
        automation_id: str,
    ) -> tuple[PluginCatalogEntry, CompiledAutomationProjectContract]:
        try:
            entry = self._plugin_catalog.require(automation_id)
            with self._repository.unit_of_work() as uow:
                rows = uow.automation_projects.list_configuration_rows(automation_id)
            return entry, self._compile_entry(entry, rows)
        except AutomationProjectContractError as exc:
            raise OrchestrationError(
                exc.code,
                "Automation project contract is unavailable",
            ) from exc
        except Exception as exc:
            if isinstance(exc, OrchestrationError):
                raise
            raise OrchestrationError(
                "PROJECT_CONTRACT_UNAVAILABLE",
                "Automation project contract is unavailable",
            ) from exc

    def _load_catalog_entry(self, automation_id: str) -> PluginCatalogEntry:
        try:
            return self._plugin_catalog.require(automation_id)
        except Exception as exc:
            if isinstance(exc, OrchestrationError):
                raise
            raise OrchestrationError(
                "PROJECT_CONTRACT_UNAVAILABLE",
                "Automation project contract is unavailable",
            ) from exc

    def _compile_entry(
        self,
        entry: PluginCatalogEntry,
        scheduled_rows: Sequence[Mapping[str, Any]],
    ) -> CompiledAutomationProjectContract:
        snapshot = entry.committed_snapshot
        metadata = snapshot.execution_metadata if snapshot is not None else None
        if not isinstance(metadata, Mapping):
            raise AutomationProjectContractError("PLUGIN_RUNTIME_NOT_COMMITTED")
        compiled_invocations = metadata.get("compiled_invocations")
        governance = metadata.get("governance_anchor")
        if not isinstance(compiled_invocations, Mapping) or not isinstance(
            governance,
            Mapping,
        ):
            raise AutomationProjectContractError("PLUGIN_RUNTIME_SNAPSHOT_INVALID")
        argument_templates: dict[str, Mapping[str, Any]] = {}
        dynamic_resolvers: dict[str, Mapping[str, str]] = {}
        for raw_entrypoint, raw_contract in compiled_invocations.items():
            expected_fields = {"arguments", "dynamic_resolvers"}
            if entry.runtime_model == "SERVICE_V2":
                expected_fields.update({"target", "governance"})
            if not isinstance(raw_contract, Mapping) or set(raw_contract) != expected_fields:
                raise AutomationProjectContractError(
                    "PLUGIN_RUNTIME_SNAPSHOT_INVALID"
                )
            arguments = raw_contract.get("arguments")
            resolvers = raw_contract.get("dynamic_resolvers")
            if not isinstance(arguments, Mapping) or not isinstance(resolvers, Mapping):
                raise AutomationProjectContractError(
                    "PLUGIN_RUNTIME_SNAPSHOT_INVALID"
                )
            entrypoint = str(raw_entrypoint)
            validate_service_v2_compiled_target(entry, entrypoint, raw_contract)
            argument_templates[entrypoint] = dict(arguments)
            dynamic_resolvers[entrypoint] = {
                str(key): str(value) for key, value in resolvers.items()
            }
        definition = AutomationProjectInstanceDefinition(
            automation_id=entry.automation_id,
            plugin_id=entry.plugin_id,
            tool_name=str(governance.get("name") or ""),
            argument_templates=argument_templates,
            dynamic_argument_resolvers=dynamic_resolvers,
            account_bindings=dict(metadata.get("account_bindings") or {}),
            allowed_entrypoints=frozenset(str(key) for key in compiled_invocations),
            project_config=dict(metadata.get("project_config") or {}),
            resource_bindings=dict(metadata.get("resource_bindings") or {}),
        )
        fragment = project_contract_fragment(entry)
        return compile_automation_project_contract(
            definition,
            catalog=self._core_catalog,
            scheduled_rows=scheduled_rows,
            plugin_contract_provider=lambda requested: (
                fragment if requested == entry.automation_id else None
            ),
        )

    def _lock_and_compile_contract(
        self,
        uow: Any,
        entry: PluginCatalogEntry,
        *,
        expected: CompiledAutomationProjectContract | None = None,
        require_enabled: bool,
        lock_rows: bool = True,
    ) -> tuple[CompiledAutomationProjectContract, Mapping[str, Any]]:
        automation_id = entry.automation_id
        project = uow.automation_plugins.get_project(
            automation_id,
            for_update=lock_rows,
        )
        if project is None:
            raise OrchestrationError(
                "AUTOMATION_PROJECT_NOT_FOUND",
                "Automation project is not installed",
            )
        if require_enabled and (
            project.get("enabled") not in {True, 1}
            or str(project.get("state") or "") != "ENABLED"
        ):
            raise OrchestrationError(
                "PROJECT_DISABLED",
                "Automation project is disabled",
            )
        if (
            str(project.get("reconcile_state") or "") != "STABLE"
            or project.get("committed_generation") != project.get("target_generation")
        ):
            raise OrchestrationError(
                "PROJECT_RUNTIME_RECONCILING",
                "Automation project runtime is synchronizing; old configuration cannot run",
            )
        committed = project.get("committed_generation")
        if type(committed) is not int or committed <= 0:
            raise OrchestrationError(
                "PLUGIN_RUNTIME_NOT_COMMITTED",
                "Automation project has no committed runtime generation",
            )
        generation = uow.automation_plugins.get_generation_row(
            automation_id,
            committed,
            for_update=lock_rows,
        )
        if generation is None or str(generation.get("state") or "") != "COMMITTED":
            raise OrchestrationError(
                "PLUGIN_RUNTIME_NOT_COMMITTED",
                "Automation project committed runtime is unavailable",
            )
        config = uow.automation_plugins.get_project_config(
            automation_id,
            for_update=lock_rows,
        )
        if config is None:
            raise OrchestrationError(
                "PROJECT_CONFIGURATION_MISSING",
                "Automation project configuration is not initialized",
            )
        rows = uow.automation_projects.list_configuration_rows(
            automation_id,
            for_update=lock_rows,
        )
        contract = self._compile_entry(entry, rows)
        if (
            contract.automation_generation != committed
            or str(generation.get("manifest_sha256") or "")
            != contract.manifest_sha256
        ):
            raise OrchestrationError(
                "PROJECT_INVOCATION_STALE",
                "Automation project generation changed",
            )
        if expected is not None and (
            expected.contract_hash != contract.contract_hash
            or expected.automation_generation != contract.automation_generation
        ):
            raise OrchestrationError(
                "PROJECT_CONFIGURATION_CHANGED",
                "Automation project changed during the request",
            )
        return contract, config

    def _describe_entry(
        self,
        entry: PluginCatalogEntry,
        policy: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        configured = (
            _PROJECT_FULL_AUTO if getattr(entry, "runtime_model", "ACTION_V1") == "SERVICE_V2"
            else str((policy or {}).get("mode") or _PROJECT_FULL_AUTO)
        )
        browser_configured = (
            configured
            if configured in _USER_POLICY_MODES
            else AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value
        )
        effective = browser_configured
        status = "ACTIVE"
        contract: CompiledAutomationProjectContract | None = None
        contract_error: str | None = None
        try:
            with self._repository.unit_of_work() as uow:
                rows = uow.automation_projects.list_configuration_rows(
                    entry.automation_id
                )
            contract = self._compile_entry(entry, rows)
        except AutomationProjectContractError as exc:
            contract_error = exc.code
        except Exception:
            contract_error = "PROJECT_CONTRACT_UNAVAILABLE"
        stable = (
            getattr(entry, "committed_generation", None) is not None
            and getattr(entry, "target_generation", None)
            == getattr(entry, "committed_generation", None)
            and str(
                getattr(
                    getattr(entry, "reconcile_state", ""),
                    "value",
                    getattr(entry, "reconcile_state", ""),
                )
            )
            == "STABLE"
        )
        # This flag means the durable administrator mode is selectable.  The
        # separate runnable/runtime fields carry current execution health.
        can_full_auto = True
        reconcile_state = str(
            getattr(
                getattr(entry, "reconcile_state", ""),
                "value",
                getattr(entry, "reconcile_state", ""),
            )
        )
        if stable and contract is not None:
            runtime_status = "READY"
        elif reconcile_state in {
            "PREPARING",
            "WAITING_COEFFECTS",
            "READY_TO_COMMIT",
            "DRAINING",
            "DISPOSING",
        }:
            runtime_status = "RECONCILING"
        else:
            runtime_status = "UNAVAILABLE"
        runnable = bool(
            getattr(entry, "enabled", False)
            and getattr(entry, "configured", False)
            and runtime_status == "READY"
            and contract is not None
            and getattr(entry, "current_enabled_entrypoints", ())
        )
        current_entrypoints = getattr(entry, "current_enabled_entrypoints", ())
        # Keep the reason deterministic and actionable.  A disabled or
        # incomplete project must not be mislabeled as a runtime failure, and
        # contract errors must remain visible even while a generation is
        # reconciling.  Console-only entrypoint gating is handled by Console's
        # entrypoint-specific execution gate.
        if not getattr(entry, "enabled", False):
            runtime_reason = "PROJECT_DISABLED"
        elif not getattr(entry, "configured", False):
            runtime_reason = "PROJECT_CONFIGURATION_INCOMPLETE"
        elif not current_entrypoints:
            runtime_reason = "ENTRYPOINTS_DISABLED"
        elif contract_error is not None:
            runtime_reason = contract_error
        elif reconcile_state and reconcile_state != "STABLE":
            runtime_reason = f"RECONCILE_{reconcile_state}"
        elif runtime_status != "READY":
            runtime_reason = "PROJECT_RUNTIME_UNAVAILABLE"
        else:
            runtime_reason = None
        if configured == AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value:
            if (
                contract is not None
                and self._legacy_schedule_active(policy or {}, contract)
                and stable
            ):
                effective = AutomationProjectPolicyMode.LEGACY_SCHEDULE_ONLY.value
                status = "LEGACY_SCHEDULE_ONLY"
            else:
                status = "RECONCILING" if runtime_status == "RECONCILING" else "UNAVAILABLE"
        elif configured == AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value:
            effective = AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value
            if runtime_status == "RECONCILING":
                status = "RECONCILING"
            elif runtime_status != "READY" or contract is None:
                status = "UNAVAILABLE"
            elif not contract.can_full_auto:
                status = "UNSUPPORTED"
        elif contract_error is not None:
            status = "UNSUPPORTED"
        elif not can_full_auto:
            status = "UNSUPPORTED"
        summary = _policy_summary(
            configured=configured,
            effective=effective,
            status=status,
            reason=(
                contract_error
                or (contract.restriction_code if contract is not None else None)
            ),
        )
        return {
            "automation_id": entry.automation_id,
            "configured_mode": browser_configured,
            "effective_mode": effective,
            "effective_status": status,
            "can_full_auto": can_full_auto,
            "runnable": runnable,
            "runtime_status": runtime_status,
            "runtime_reason": runtime_reason,
            "summary": summary,
            "updated_by": str(
                (policy or {}).get("approved_by_actor_display_name")
                or (policy or {}).get("approved_by_actor_id")
                or ""
            )[:100],
            "updated_at": _datetime_text((policy or {}).get("updated_at")),
            "policy_version": _positive_int(
                int((policy or {}).get("version") or 0),
                "policy_version",
            ),
            "project_configuration_version": _positive_int(
                int(
                    getattr(entry, "project_config_version", 0)
                    or (policy or {}).get("project_configuration_version")
                    or 0
                ),
                "project_configuration_version",
            ),
        }

    @staticmethod
    def _full_auto_active(
        policy: Mapping[str, Any],
        contract: CompiledAutomationProjectContract,
    ) -> bool:
        # PROJECT_FULL_AUTO is durable policy intent; the current invocation is
        # still matched against the exact committed contract before evaluation.
        return contract.can_full_auto

    @staticmethod
    def _legacy_schedule_active(
        policy: Mapping[str, Any],
        contract: CompiledAutomationProjectContract,
    ) -> bool:
        return bool(
            str(policy.get("contract_hash") or "") == contract.contract_hash
            and str(policy.get("tool_contract_hash") or "")
            == contract.tool_contract_hash
            and str(policy.get("plugin_contract_hash") or "")
            == str(contract.plugin_contract_hash or "")
            and int(policy.get("project_generation") or 0)
            == contract.automation_generation
            and int(policy.get("project_configuration_version") or 0)
            == contract.project_configuration_version
        )

    def _pending_projection(
        self,
        automation_id: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        actor: Actor,
    ) -> dict[str, Any]:
        pending_count = len(rows)
        if pending_count:
            highest = max(
                (str(row.get("risk_level") or "LOW").upper() for row in rows),
                key=lambda value: _RISK_ORDER.get(value, -1),
            )
            if highest == "EXTREME":
                highest = "CRITICAL"
            sources = Counter(str(row.get("source") or "unknown") for row in rows)
            source_summary = "，".join(
                f"{_SOURCE_LABELS.get(source, source)} {count}"
                for source, count in sorted(sources.items())
            )
            pending_hash = _pending_set_hash(rows)
        else:
            highest = ""
            source_summary = ""
            pending_hash = ""
        can_decide = self._is_super_admin(actor)
        return {
            "automation_id": automation_id,
            "pending_count": pending_count,
            "highest_risk": highest,
            "source_summary": source_summary,
            "pending_set_hash": pending_hash,
            "can_approve": can_decide,
            "can_reject": can_decide,
        }

    def _validate_pending_rows(
        self,
        uow: Any,
        *,
        project: Mapping[str, Any],
        automation_id: str,
        rows: Sequence[Mapping[str, Any]],
        approving: bool,
    ) -> None:
        if approving and (
            project.get("enabled") not in {True, 1}
            or str(project.get("state") or "") != "ENABLED"
        ):
            raise OrchestrationError(
                "PROJECT_DISABLED",
                "Disabled automation projects cannot be approved",
            )
        if not approving:
            return
        policy = uow.automation_projects.get_policy(
            automation_id,
            for_update=True,
        )
        if policy is None:
            raise OrchestrationError(
                "PROJECT_POLICY_NOT_INITIALIZED",
                "Automation project policy is not initialized",
            )
        try:
            entry = self._plugin_catalog.require(automation_id)
            contract, _config = self._lock_and_compile_contract(
                uow,
                entry,
                require_enabled=True,
                lock_rows=True,
            )
        except (AutomationProjectContractError, KeyError, ValueError) as exc:
            raise OrchestrationError(
                "PROJECT_PENDING_SET_INVALID",
                "Automation project contract changed before approval",
            ) from exc
        for row in rows:
            if str(row.get("required_role") or "") != "super_admin":
                raise OrchestrationError(
                    "PROJECT_PENDING_SET_INVALID",
                    "Project pending approval has an unexpected role",
                )
            raw_invocation = row.get("automation_invocation_json")
            try:
                invocation = AutomationProjectInvocation.from_mapping(raw_invocation)
            except (AutomationProjectContractError, TypeError) as exc:
                raise OrchestrationError(
                    "PROJECT_PENDING_SET_INVALID",
                    "Project pending approval has invalid invocation identity",
                ) from exc
            raw_plan = row.get("plan_json")
            raw_parameters = row.get("parameters_json")
            try:
                plan = _plan_from_mapping(raw_plan)
            except OrchestrationError as exc:
                raise OrchestrationError(
                    "PROJECT_PENDING_SET_INVALID",
                    "Project pending approval has an invalid plan",
                ) from exc
            execution_context = (
                raw_parameters.get("execution_context", {})
                if isinstance(raw_parameters, Mapping)
                else None
            )
            if (
                invocation.automation_id != automation_id
                or plan.automation_id != automation_id
                or plan.automation_generation != invocation.automation_generation
                or plan.automation_contract_hash != invocation.contract_hash
                or plan.plan_hash != str(row.get("plan_hash") or "")
                or plan.plan_hash != str(row.get("current_plan_hash") or "")
                or not isinstance(execution_context, Mapping)
                or not contract.matches_plan(
                    plan,
                    invocation,
                    source=str(row.get("source") or ""),
                    execution_context=execution_context,
                    dynamic_resolver=self._dynamic_resolver,
                )
            ):
                raise OrchestrationError(
                    "PROJECT_PENDING_SET_INVALID",
                    "Project pending approval is stale",
                )

    @staticmethod
    def _validate_policy_replay(
        event: Mapping[str, Any],
        *,
        current: Mapping[str, Any],
        mode: str,
        comment: str,
        actor: Actor,
    ) -> None:
        if (
            str(event.get("to_mode") or "") != mode
            or str(event.get("actor_id") or "") != actor.actor_id
            or str(event.get("actor_role") or "") != "super_admin"
            or str(event.get("comment") or "") != comment
        ):
            raise IdempotencyConflict(
                "project policy request was reused with different input"
            )
        if (
            str(current.get("mode") or "") != mode
            or str(current.get("contract_hash") or "")
            != str(event.get("contract_hash") or "")
        ):
            raise OrchestrationError(
                "PROJECT_POLICY_CHANGED",
                "The original idempotent policy result was superseded",
            )

    @staticmethod
    def _validate_batch_replay(
        batch: Mapping[str, Any],
        *,
        decision: str,
        expected_hash: str,
        actor: Actor,
        comment: str,
    ) -> dict[str, Any]:
        if (
            str(batch.get("decision") or "") != decision
            or str(batch.get("expected_pending_set_hash") or "") != expected_hash
            or str(batch.get("actor_id") or "") != actor.actor_id
            or str(batch.get("actor_role") or "") != "super_admin"
            or str(batch.get("comment") or "") != comment
        ):
            raise IdempotencyConflict(
                "project approval batch request was reused with different input"
            )
        result = batch.get("result_json")
        if not isinstance(result, Mapping):
            raise OrchestrationError(
                "INVALID_PROJECT_APPROVAL_BATCH",
                "Persisted project approval batch is invalid",
            )
        return dict(result)

    @staticmethod
    def _retire_legacy_schedule_policies(
        uow: Any,
        *,
        automation_id: str,
        rows: Sequence[Mapping[str, Any]],
        actor: Actor,
        request_id: str,
        correlation_id: str,
        occurred_at: datetime,
    ) -> None:
        for row in rows:
            task_id = str(row.get("id") or "").strip()
            if not task_id:
                raise OrchestrationError(
                    "PROJECT_SCHEDULE_INVALID",
                    "Automation project contains an invalid schedule identity",
                )
            policy = uow.scheduled_policies.ensure_default(task_id)
            from_mode = str(policy.get("mode") or "REQUIRE_EACH_RUN")
            if from_mode == "EXACT_SCHEDULE_EXEMPT":
                uow.scheduled_policies.update_policy(
                    task_id,
                    expected_version=int(policy["version"]),
                    mode="REQUIRE_EACH_RUN",
                    contract_hash=None,
                    contract_snapshot=None,
                    tool_contract_hash=None,
                    actor_id=actor.actor_id,
                    actor_role="super_admin",
                    actor_display_name=actor.display_name or None,
                    comment="Project-level policy takeover",
                )
            event_request_id = f"{request_id}:takeover:{task_id}"
            if len(event_request_id) > _SCHEDULED_POLICY_EVENT_REQUEST_ID_MAX_LENGTH:
                event_request_id = canonical_sha256(event_request_id)[
                    :_SCHEDULED_POLICY_EVENT_REQUEST_ID_MAX_LENGTH
                ]
            if uow.scheduled_policies.get_event_by_request(
                task_id,
                event_request_id,
            ) is None:
                uow.scheduled_policies.append_event(
                    {
                        "task_id": task_id,
                        "request_id": event_request_id,
                        "from_mode": from_mode,
                        "to_mode": "REQUIRE_EACH_RUN",
                        "contract_hash": None,
                        "contract_snapshot_json": None,
                        "tool_contract_hash": None,
                        "actor_id": actor.actor_id,
                        "actor_role": "super_admin",
                        "actor_display_name": actor.display_name or None,
                        "reason": "PROJECT_POLICY_TAKEOVER",
                        "comment": f"Project policy now owns {automation_id}",
                        "occurred_at": occurred_at,
                        "correlation_id": correlation_id,
                    }
                )

    @staticmethod
    def _append_policy_domain_event(
        uow: Any,
        *,
        automation_id: str,
        mode: str,
        version: int,
        actor: Actor,
        request_id: str,
        correlation_id: str,
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
                    "actor_id": actor.actor_id,
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

    @staticmethod
    def _append_approval_domain_event(
        uow: Any,
        *,
        row: Mapping[str, Any],
        actor: Actor,
        decision: str,
        comment: str,
        occurred_at: datetime,
    ) -> None:
        uow.events.append_with_outbox(
            {
                "event_id": new_id(),
                "event_type": "agent.approval.decided",
                "schema_version": 1,
                "source_system": "agent",
                "source_event_id": None,
                "entity_type": "approval_request",
                "entity_id": row["approval_id"],
                "work_item_id": row["work_item_id"],
                "run_id": row["run_id"],
                "occurred_at": occurred_at,
                "observed_at": occurred_at,
                "correlation_id": row["correlation_id"],
                "causation_id": row.get("causation_id"),
                "payload": {
                    "decision": decision,
                    "plan_hash": row["plan_hash"],
                    "actor_type": actor.actor_type.value,
                    "actor_id": actor.actor_id,
                    "actor_roles": list(actor.roles),
                    "comment": comment,
                    "batch": True,
                },
            },
            (
                {
                    "consumer_name": "orchestration.audit",
                    "topic": "agent.approval.decided",
                    "partition_key": str(row["work_item_id"]),
                    "max_attempts": 10,
                },
                {
                    "consumer_name": "feishu.approval",
                    "topic": "agent.approval.decided",
                    "partition_key": str(row["approval_id"]),
                    "max_attempts": 20,
                },
            ),
        )

    @staticmethod
    def _is_super_admin(actor: Actor) -> bool:
        return (
            actor.actor_type is ActorType.CONSOLE_ADMIN
            and "super_admin" in actor.roles
            and actor.authenticated_by == "mysql_admin_session"
        )

    def _require_release_active(self) -> None:
        if (
            self._release_hold_provider is not None
            and self._release_hold_provider() is True
        ):
            raise OrchestrationError(
                "RELEASE_HELD",
                "Automation project changes are disabled during release activation",
            )

    def _require_release_held(self) -> None:
        if self._release_hold_provider is None:
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_HOLD_REQUIRED",
                "Automation project bootstrap requires a release hold",
            )
        try:
            release_held = self._release_hold_provider()
        except Exception as exc:
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_HOLD_UNAVAILABLE",
                "Automation project bootstrap could not verify the release hold",
            ) from exc
        if release_held is not True:
            raise OrchestrationError(
                "PROJECT_POLICY_BOOTSTRAP_HOLD_REQUIRED",
                "Automation project bootstrap requires a release hold",
            )

    @classmethod
    def _require_super_admin(cls, actor: Actor) -> None:
        if not cls._is_super_admin(actor):
            raise OrchestrationError(
                "SUPER_ADMIN_REQUIRED",
                "A signed Console super administrator is required",
            )

    @staticmethod
    def _require_console_admin(actor: Actor) -> None:
        if (
            actor.actor_type is not ActorType.CONSOLE_ADMIN
            or not {"admin", "super_admin"}.intersection(actor.roles)
            or actor.authenticated_by != "mysql_admin_session"
        ):
            raise OrchestrationError(
                "ACTION_FORBIDDEN",
                "A signed Console administrator is required",
            )

    @classmethod
    def _require_trusted_entrypoint_actor(
        cls,
        entrypoint: AutomationEntrypoint,
        actor: Actor,
    ) -> None:
        if entrypoint in {
            AutomationEntrypoint.CONSOLE,
            AutomationEntrypoint.HARNESS,
            AutomationEntrypoint.MODULE_SLOTS,
        }:
            cls._require_console_admin(actor)
            return
        if entrypoint is AutomationEntrypoint.FEISHU:
            if actor.actor_type is ActorType.FEISHU_USER and (
                (actor.authenticated_by == "feishu_verified_event" and actor.roles == ())
                or (actor.authenticated_by == "feishu_admin_binding" and actor.roles == ("admin", "super_admin"))
            ):
                return
            raise OrchestrationError(
                "TRUSTED_ENTRYPOINT_REQUIRED",
                "Automation invocation did not originate from its trusted adapter",
            )
        expected = {
            AutomationEntrypoint.SCHEDULER: (
                ActorType.SCHEDULER,
                "apscheduler",
                ("system",),
            ),
            AutomationEntrypoint.WEBHOOK: (
                ActorType.WEBHOOK,
                "signed_webhook_route",
                (),
            ),
            AutomationEntrypoint.EVENTS: (
                ActorType.EVENT,
                "managed_event_dispatcher",
                (),
            ),
        }[entrypoint]
        actor_type, authenticated_by, roles = expected
        if (
            actor.actor_type is not actor_type
            or actor.authenticated_by != authenticated_by
            or actor.roles != roles
        ):
            raise OrchestrationError(
                "TRUSTED_ENTRYPOINT_REQUIRED",
                "Automation invocation did not originate from its trusted adapter",
            )
