"""Project-scoped authorization and typed invocation.

The service never accepts task identities, plan hashes, contract hashes, or
actor fields from a browser.  Those values are resolved from the immutable
committed plugin generation and locked orchestration rows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Awaitable

from shared.plugin_invocation_repository import ACTIVE_INVOCATION_STATUSES

from agent.automation_plugins.catalog import (
    PluginCatalog,
    PluginCatalogEntry,
    project_contract_fragment,
)
from agent.automation_plugins.catalog_read_scope import catalog_read_transaction, catalog_row_cache
from agent.orchestration.automation_project_service_v2 import (
    validate_service_v2_compiled_target,
)
from agent.orchestration.models import (
    Actor,
    ActorType,
    OrchestrationError,
)
from agent.orchestration.automation_project_policy_support import (
    policy_list_automation_ids, scoped_policy_projection, _automation_id,
    _datetime_text,
    _policy_summary,
    _positive_int,
    _selection_contribution_id,
)
from agent.orchestration.scan_preview_binding import (
    is_scan_preview_project,
)
from agent.orchestration.selection_preview_binding import (
    is_selection_preview_project,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectContractError,
    AutomationProjectPolicyMode,
    CompiledAutomationProjectContract,
    compile_automation_project_contract,
)
from shared.automation_project_manifest import AutomationProjectInstanceDefinition
from shared.automation_project_policy_repository import (
    SUPER_ADMIN_PROJECT_POLICY_REASON,
)


logger = logging.getLogger(__name__)


_DEFAULT_FULL_AUTO_ACTOR_ID = "system:migration:automation-full-auto-v1"
_DEFAULT_FULL_AUTO_REASON = "AUTOMATION_DEFAULT_FULL_AUTO"
_PROJECT_FULL_AUTO = AutomationProjectPolicyMode.PROJECT_FULL_AUTO.value


class AutomationProjectPolicyService:
    """One authorization authority for every trusted project entrypoint."""

    def __init__(
        self,
        repository: Any,
        plugin_catalog: PluginCatalog,
        *,
        dynamic_resolver: (Callable[[str, str, Mapping[str, Any]], Any] | None) = None,
        release_hold_provider: Callable[[], bool] | None = None,
        contribution_registry: Any | None = None,
        direct_invocations: Any | None = None,
    ) -> None:
        self._repository = repository
        self._plugin_catalog = plugin_catalog
        self._dynamic_resolver = dynamic_resolver
        self._release_hold_provider = release_hold_provider
        self._contribution_registry = contribution_registry
        self.direct_invocations = direct_invocations

    def list_policies(self, *, automation_ids: Sequence[str] | None = None) -> dict[str, Any]:
        selected = policy_list_automation_ids(automation_ids) if automation_ids is not None else None
        return scoped_policy_projection(self._repository, self._plugin_catalog, selected, self._describe_entry)

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

    def get_scan_preview_projection(self, automation_id: str, *, preview_invocation_id: str) -> dict[str, Any]:
        from agent.orchestration.direct_invocation_previews import project_preview
        entry, contract = self._load_contract(_automation_id(automation_id))
        if not is_scan_preview_project(entry):
            raise OrchestrationError("SCAN_PREVIEW_PROJECT_INVALID", "该插件不支持扫描预览。")
        return project_preview(self.direct_invocations.repository, preview_invocation_id, entry=entry, contract=contract, scan=True)

    def get_selection_preview_projection(self, automation_id: str, *, preview_invocation_id: str) -> dict[str, Any]:
        from agent.orchestration.direct_invocation_previews import project_preview
        entry, contract = self._load_contract(_automation_id(automation_id))
        if not is_selection_preview_project(entry) or is_scan_preview_project(entry):
            raise OrchestrationError("SELECTION_PREVIEW_PROJECT_INVALID", "该插件不支持候选选择。")
        return project_preview(self.direct_invocations.repository, preview_invocation_id, entry=entry, contract=contract, scan=False)

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

    def confirm_selection_preview(self, automation_id: str, *, preview_invocation_id: str, selected_bill_codes: Sequence[str], request_id: str, actor: Actor) -> Any:
        entry = self._load_catalog_entry(automation_id)
        return self.invoke_trusted(automation_id, entrypoint=AutomationEntrypoint.CONSOLE,
            request_id=request_id, actor=actor, preview_invocation_id=preview_invocation_id,
            selected_bill_codes=selected_bill_codes,
            contribution_id=_selection_contribution_id(entry, AutomationEntrypoint.CONSOLE))

    def invoke_console(
        self,
        automation_id: str,
        *,
        request_id: str,
        actor: Actor,
        preview_invocation_id: str | None = None,
        contribution_id: str | None = None,
        runtime_inputs: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.invoke_trusted(
            automation_id,
            entrypoint=AutomationEntrypoint.CONSOLE,
            request_id=request_id,
            actor=actor,
            preview_invocation_id=preview_invocation_id,
            contribution_id=contribution_id,
            trusted_context={"dynamic_inputs": runtime_inputs} if runtime_inputs is not None else None,
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

    def invoke_trusted(self, automation_id: str, **kwargs) -> Any:
        from agent.orchestration.direct_project_invocation import invoke_direct
        return invoke_direct(self, automation_id, **kwargs)

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
        preview_invocation_id: str | None = None,
        selected_bill_codes: Sequence[str] | None = None,
        contribution_id: str | None = None,
        timeout_seconds: float = 1800.0,
        on_accepted: Callable[[Any], Awaitable[None]] | None = None,
        require_full_auto: bool = False,
    ) -> dict[str, Any]:
        # Admission is synchronous cross-region database work; keep it off the
        # event loop exactly like the HTTP invoke route already does.
        receipt = await asyncio.to_thread(
            self.invoke_trusted,
            automation_id, entrypoint=entrypoint, request_id=request_id, actor=actor,
            trusted_context=trusted_context, idempotency_key=idempotency_key,
            expected_automation_generation=expected_automation_generation,
            expected_project_configuration_version=expected_project_configuration_version,
            preview_invocation_id=preview_invocation_id,
            selected_bill_codes=selected_bill_codes, contribution_id=contribution_id,
            require_full_auto=require_full_auto,
        )
        if on_accepted is not None and receipt.get("status") in ACTIVE_INVOCATION_STATUSES:
            await on_accepted(receipt)
        result = await self.direct_invocations.wait(receipt["invocation_id"], timeout_seconds=timeout_seconds)
        return self.project_completed_invocation_result(automation_id, result, is_formal=preview_invocation_id is not None)

    def project_completed_invocation_result(self, automation_id: str, result: dict[str, Any], *, is_formal: bool = False) -> dict[str, Any]:
        """Reuse the public preview projection when a later result read completes."""
        if result["status"] == "COMPLETED" and not is_formal and (result.get("output") or {}).get("dry_run") is True:
            entry = self._load_catalog_entry(automation_id)
            if is_scan_preview_project(entry):
                result["scan_preview"] = self.get_scan_preview_projection(automation_id, preview_invocation_id=result["invocation_id"])
            if is_selection_preview_project(entry):
                result["selection_preview"] = self.get_selection_preview_projection(automation_id, preview_invocation_id=result["invocation_id"])
        return result

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
        locked_project: Mapping[str, Any] | None = None,
    ) -> tuple[CompiledAutomationProjectContract, Mapping[str, Any]]:
        automation_id = entry.automation_id
        project = locked_project or uow.automation_plugins.get_project(
            automation_id, for_update=lock_rows
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
        # Plugin projects are always fully automatic; the policy row keeps
        # its version and audit trail only.
        configured = _PROJECT_FULL_AUTO
        browser_configured = configured
        effective = browser_configured
        status = "ACTIVE"
        contract: CompiledAutomationProjectContract | None = None
        contract_error: str | None = None
        try:
            display_rows = (catalog_row_cache(self._repository) or {}).get(entry.automation_id)
            if display_rows is not None:
                rows = display_rows.schedules.get(entry.automation_id, [])
            else:
                with catalog_read_transaction(self._repository) as uow:
                    rows = uow.automation_projects.list_configuration_rows(entry.automation_id)
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
        if runtime_status == "RECONCILING":
            status = "RECONCILING"
        elif runtime_status != "READY" or contract is None:
            status = "UNAVAILABLE"
        elif not contract.can_full_auto:
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
        if entrypoint is AutomationEntrypoint.HARNESS and actor.actor_type is ActorType.FEISHU_USER:
            if actor.authenticated_by == "feishu_admin_binding" and "admin" in actor.roles:
                return
            raise OrchestrationError("TRUSTED_ENTRYPOINT_REQUIRED", "AI 对话需要已绑定的飞书身份")
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
                or (actor.authenticated_by == "feishu_admin_binding" and actor.roles in {("admin",), ("admin", "super_admin")})
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
