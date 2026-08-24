"""Validate a plan against the live capability catalog and authoritative context."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.automation_plugins.code_owned_fields import (
    SCAN_PHASE_PREVIEW,
    resolve_scan_capability_phase,
)
from agent.orchestration.impact_preview import validate_write_impact
from agent.orchestration.models import (
    ContextSnapshot,
    OperationType,
    OrchestrationError,
    Plan,
    RiskLevel,
    topological_steps,
)
from agent.orchestration.ports import ToolCatalogPort


class PlanValidator:
    def __init__(self, catalog: ToolCatalogPort) -> None:
        self._catalog = catalog

    def validate(self, plan: Plan, context: ContextSnapshot, *, llm_selected: bool = False) -> Plan:
        if plan.context_fingerprint != context.fingerprint:
            raise OrchestrationError("CONTEXT_CHANGED", "Plan context fingerprint does not match current context")
        if plan.tool_catalog_hash != self._catalog.catalog_hash:
            raise OrchestrationError("TOOL_CATALOG_CHANGED", "Tool catalog changed after the plan was created")
        topological_steps(plan.steps)

        for step in plan.steps:
            capability = self._catalog.get_capability(step.tool_name)
            if capability is None:
                raise OrchestrationError("UNKNOWN_TOOL", f"Unknown tool: {step.tool_name}")
            if str(capability.get("version") or "") != step.tool_version:
                raise OrchestrationError("TOOL_VERSION_CHANGED", f"Tool version changed: {step.tool_name}")
            try:
                scan_phase = resolve_scan_capability_phase(
                    capability,
                    step.arguments,
                )
            except ValueError as exc:
                raise OrchestrationError(
                    "SCAN_EXECUTION_PHASE_INVALID",
                    "Scan execution phase is incomplete or ambiguous",
                ) from exc
            expected_operation = (
                OperationType.READ.value
                if scan_phase == SCAN_PHASE_PREVIEW
                else str(capability.get("operation_type") or "")
            )
            expected_risk = (
                RiskLevel.LOW.value
                if scan_phase == SCAN_PHASE_PREVIEW
                else str(capability.get("risk_level") or "")
            )
            if expected_operation != step.operation_type.value:
                raise OrchestrationError("TOOL_OPERATION_CHANGED", f"Tool operation changed: {step.tool_name}")
            if expected_risk != step.risk_level.value:
                raise OrchestrationError("TOOL_RISK_CHANGED", f"Tool risk changed: {step.tool_name}")
            if llm_selected and (
                not bool(capability.get("llm_exposed"))
                or step.operation_type not in {OperationType.READ, OperationType.COMPUTE}
            ):
                raise OrchestrationError("LLM_WRITE_FORBIDDEN", "LLM plans may contain only exposed read/compute tools")
            project_bound = self._is_broker_bound_project(step.tool_name, capability)
            if not project_bound:
                try:
                    self._catalog.validate_arguments(step.tool_name, step.arguments)
                except (KeyError, TypeError, ValueError) as exc:
                    raise OrchestrationError(
                        "INVALID_TOOL_ARGUMENTS",
                        f"Tool arguments do not satisfy the governed input_schema: {exc}",
                        details={"status": "NEEDS_CLARIFICATION"},
                    ) from exc
                self._validate_account_scope(step.account_id, capability, context)
            self._validate_integrity(capability, context)
            if not step.idempotency_key:
                raise OrchestrationError("STEP_IDEMPOTENCY_REQUIRED", f"Step has no idempotency key: {step.step_key}")
            validate_write_impact(operation_type=step.operation_type, impact=plan.impact)
        return plan

    @staticmethod
    def _is_broker_bound_project(tool_name: str, capability: Mapping[str, Any]) -> bool:
        """Identify project actions whose account/resource inputs stay broker-only.

        Their exact invocation arguments are validated against the committed,
        signed project contract by the project policy service.  The core tool
        schema still describes the legacy whole-tool API and requires account
        identifiers that are intentionally absent from plugin JSON.
        """

        return (
            str(tool_name).startswith("automation.")
            and str(tool_name).endswith(".run")
            and isinstance(capability.get("_plugin_runtime"), Mapping)
        )

    @staticmethod
    def _validate_account_scope(
        account_id: str | None,
        capability: Mapping[str, Any],
        context: ContextSnapshot,
    ) -> None:
        scope = capability.get("account_scope") or {}
        if isinstance(scope, str):
            mode = scope
        elif isinstance(scope, Mapping):
            if "mode" in scope:
                mode = str(scope.get("mode") or "none")
            elif bool(scope.get("required")):
                mode = "single"
            else:
                mode = "optional"
        else:
            raise OrchestrationError("INVALID_ACCOUNT_SCOPE", "Tool account_scope must be an object or string")

        if mode in {"none", "optional"}:
            if account_id and account_id not in context.account_ids:
                raise OrchestrationError(
                    "UNKNOWN_ACCOUNT",
                    f"Account is not in authoritative context: {account_id}",
                    details={"status": "NEEDS_CLARIFICATION"},
                )
            return
        if mode == "single":
            if not account_id:
                if len(context.account_ids) == 0:
                    raise OrchestrationError("ACCOUNT_REQUIRED", "Tool requires one explicit account")
                if len(context.account_ids) > 1:
                    raise OrchestrationError("ACCOUNT_AMBIGUOUS", "Multiple accounts matched; choose one account explicitly")
                return
            if account_id not in context.account_ids:
                raise OrchestrationError(
                    "UNKNOWN_ACCOUNT",
                    f"Account is not in authoritative context: {account_id}",
                    details={"status": "NEEDS_CLARIFICATION"},
                )
            return
        if mode == "all_configured":
            if not context.account_ids:
                raise OrchestrationError("NO_CONFIGURED_ACCOUNTS", "No configured account is available")
            if account_id:
                raise OrchestrationError("ACCOUNT_SCOPE_MISMATCH", "This tool must cover all configured accounts")
            return
        if mode == "single_or_all_configured":
            if not context.account_ids:
                raise OrchestrationError("NO_CONFIGURED_ACCOUNTS", "No configured account is available")
            if account_id and account_id not in context.account_ids:
                raise OrchestrationError(
                    "UNKNOWN_ACCOUNT",
                    f"Account is not in authoritative context: {account_id}",
                )
            return
        raise OrchestrationError("INVALID_ACCOUNT_SCOPE", f"Unknown account scope mode: {mode}")

    @staticmethod
    def _validate_integrity(capability: Mapping[str, Any], context: ContextSnapshot) -> None:
        evidence = capability.get("evidence") or []
        if isinstance(evidence, Mapping) and "required_fields" in evidence:
            required_fields = evidence.get("required_fields") or []
            if not isinstance(required_fields, list):
                raise OrchestrationError("INVALID_TOOL_EVIDENCE", "Tool evidence required_fields is invalid")
            if "pagination_complete" in required_fields and context.source_integrity.get("pagination_complete") is False:
                raise OrchestrationError("SOURCE_INCOMPLETE", "Source pagination is incomplete")
            return
        requirements = [evidence] if isinstance(evidence, Mapping) else evidence
        if not isinstance(requirements, list):
            raise OrchestrationError("INVALID_TOOL_EVIDENCE", "Tool evidence contract is invalid")
        requires_fresh_source = any(bool(item.get("requires_source_integrity")) for item in requirements if isinstance(item, Mapping))
        if not requires_fresh_source:
            return
        if not context.source_integrity:
            raise OrchestrationError("SOURCE_INTEGRITY_MISSING", "Required source integrity context is missing")
        incomplete = sorted(
            name
            for name, value in context.source_integrity.items()
            if value is False or (isinstance(value, Mapping) and value.get("complete") is False)
        )
        if incomplete:
            raise OrchestrationError("SOURCE_INCOMPLETE", f"Source integrity is incomplete: {', '.join(incomplete)}")
