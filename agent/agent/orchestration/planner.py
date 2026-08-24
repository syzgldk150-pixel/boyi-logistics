"""Deterministic v1 planner.

The first release intentionally supports explicit, deterministic tool plans.
An LLM may select only catalog capabilities marked read/compute and exposed to
the LLM, but it cannot supply risk or approval decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from agent.automation_plugins.code_owned_fields import (
    SCAN_PHASE_PREVIEW,
    first_party_code_owned_plan_fields,
    resolve_scan_capability_phase,
)
from agent.orchestration.models import (
    Command,
    ContextSnapshot,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
    sha256_json,
)
from agent.orchestration.impact_preview import build_write_impact
from agent.orchestration.ports import ToolCatalogPort
from agent.orchestration.scan_preview_binding import build_scan_preview_impact
from agent.orchestration.scan_preview_binding import (
    SCAN_PREVIEW_PAYLOAD_BINDING_FIELD,
    scan_preview_payload_binding,
)


CUSTOMER_PROBLEM_SYNC_TOOL = "sync_customer_service_problems"


class DeterministicPlanner:
    def __init__(self, catalog: ToolCatalogPort) -> None:
        self._catalog = catalog

    def plan(self, command: Command, context: ContextSnapshot, *, llm_selected: bool = False) -> Plan:
        parameters = command.parameters
        tool_name = str(parameters.get("tool_name") or "").strip()
        if not tool_name:
            raise OrchestrationError("TOOL_NAME_REQUIRED", "A deterministic command requires tool_name")
        capability = self._catalog.get_capability(tool_name)
        if capability is None:
            raise OrchestrationError("UNKNOWN_TOOL", f"Unknown tool: {tool_name}")

        raw_arguments = parameters.get("arguments")
        if not isinstance(raw_arguments, Mapping):
            raise OrchestrationError("INVALID_TOOL_ARGUMENTS", "arguments must be a JSON object")
        clarification = _trusted_clarification_override(context)
        arguments = dict(raw_arguments)
        raw_updates = clarification.get("argument_updates")
        if isinstance(raw_updates, Mapping):
            arguments.update(dict(raw_updates))

        account_id_value = clarification.get("account_id", parameters.get("account_id"))
        account_id = str(account_id_value).strip() if account_id_value not in (None, "") else None
        if raw_updates is not None and "account_id" in raw_updates:
            updated_argument_account = str(raw_updates.get("account_id") or "").strip()
            if not account_id or updated_argument_account != account_id:
                raise OrchestrationError(
                    "CLARIFICATION_ACCOUNT_CONFLICT",
                    "argument_updates.account_id must match the explicit clarification account_id",
                    details={"status": "NEEDS_CLARIFICATION"},
                )
        if account_id and _input_schema_accepts_account_id(capability):
            arguments["account_id"] = account_id

        arguments = _normalize_financial_values(arguments)
        code_owned_fields = _project_code_owned_plan_fields(
            command=command,
            capability=capability,
        )
        if (
            tool_name == CUSTOMER_PROBLEM_SYNC_TOOL
            or "recheck_items" in code_owned_fields
        ):
            arguments["recheck_items"] = _customer_problem_recheck_context(context)
        if SCAN_PREVIEW_PAYLOAD_BINDING_FIELD in code_owned_fields:
            if SCAN_PREVIEW_PAYLOAD_BINDING_FIELD in raw_arguments:
                raise OrchestrationError(
                    "SCAN_PREVIEW_CONTEXT_INVALID",
                    "Scan preview payload binding is controlled by the server",
                )
            if arguments.get("dry_run") is not True:
                preview_binding = scan_preview_payload_binding(
                    command=command,
                    capability=capability,
                    arguments=arguments,
                )
                if preview_binding is None:
                    raise OrchestrationError(
                        "SCAN_PREVIEW_CONTEXT_REQUIRED",
                        "Formal scan execution requires a completed preview binding",
                    )
                arguments[SCAN_PREVIEW_PAYLOAD_BINDING_FIELD] = preview_binding

        try:
            scan_phase = resolve_scan_capability_phase(capability, arguments)
        except ValueError as exc:
            raise OrchestrationError(
                "SCAN_EXECUTION_PHASE_INVALID",
                "Scan execution phase is incomplete or ambiguous",
            ) from exc
        operation_type = (
            OperationType.READ
            if scan_phase == SCAN_PHASE_PREVIEW
            else _operation_type(capability)
        )
        if llm_selected:
            if not bool(capability.get("llm_exposed")):
                raise OrchestrationError("LLM_TOOL_NOT_EXPOSED", f"Tool is not exposed to the LLM: {tool_name}")
            if operation_type not in {OperationType.READ, OperationType.COMPUTE}:
                raise OrchestrationError("LLM_WRITE_FORBIDDEN", "LLM-selected plans may contain only read or compute tools")

        tool_version = str(capability.get("version") or "").strip()
        if not tool_version:
            raise OrchestrationError("TOOL_VERSION_REQUIRED", f"Tool capability has no version: {tool_name}")

        expected_evidence = _object_tuple(capability.get("evidence"), field_name="evidence")
        postconditions = _object_tuple(capability.get("postconditions"), field_name="postconditions")
        write_impact = build_scan_preview_impact(
            command=command,
            capability=capability,
            operation_type=operation_type,
            account_id=account_id,
            arguments=arguments,
        ) or build_write_impact(
            tool_name=tool_name,
            operation_type=operation_type,
            account_id=account_id,
            arguments=arguments,
        )
        impact = write_impact or _derive_impact(command, tool_name, operation_type, account_id, arguments)

        step_key = "step_1"
        step_idempotency = sha256_json(
            {
                "command_source": command.source,
                "command_idempotency_key": command.idempotency_key,
                "step_key": step_key,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "account_id": account_id,
                "arguments": arguments,
            }
        )
        step = PlanStep(
            step_key=step_key,
            tool_name=tool_name,
            tool_version=tool_version,
            operation_type=operation_type,
            arguments=arguments,
            account_id=account_id,
            depends_on=(),
            idempotency_key=step_idempotency,
            expected_evidence=expected_evidence,
            postconditions=postconditions,
            risk_level=(
                RiskLevel.LOW
                if scan_phase == SCAN_PHASE_PREVIEW
                else _risk_level(capability)
            ),
            requires_approval=False,
        )
        invocation = command.automation_invocation
        return Plan(
            command_type=command.command_type,
            context_fingerprint=context.fingerprint,
            tool_catalog_hash=self._catalog.catalog_hash,
            steps=(step,),
            impact=impact,
            automation_id=(invocation.automation_id if invocation is not None else None),
            automation_generation=(
                invocation.automation_generation if invocation is not None else None
            ),
            automation_contract_hash=(
                invocation.contract_hash if invocation is not None else None
            ),
        )


def _project_code_owned_plan_fields(
    *,
    command: Command,
    capability: Mapping[str, Any],
) -> tuple[str, ...]:
    """Resolve server-owned plan fields from the exact committed plugin identity.

    Project capabilities are exposed as ``automation.<instance>.run`` aliases,
    so matching the underlying core tool name silently skipped first-party
    fields for real Console and Scheduler commands.  The immutable runtime
    descriptor carries the signed instance/plugin/trust identity needed by the
    same closed declaration used during contract compilation.
    """

    invocation = command.automation_invocation
    runtime = capability.get("_plugin_runtime")
    if invocation is None or not isinstance(runtime, Mapping):
        return ()
    automation_id = str(runtime.get("automation_id") or "").strip()
    if automation_id != invocation.automation_id:
        return ()
    return first_party_code_owned_plan_fields(
        automation_id=automation_id,
        plugin_id=str(runtime.get("plugin_id") or "").strip(),
        trust_source=str(runtime.get("trust_source") or "").strip(),
    )


def _trusted_clarification_override(context: ContextSnapshot) -> dict[str, Any]:
    raw = context.values.get("clarification_override")
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise OrchestrationError(
            "INVALID_CLARIFICATION_CONTEXT",
            "Planner received an invalid clarification override",
        )
    allowed = {"schema_version", "account_id", "argument_updates"}
    if any(str(key) not in allowed for key in raw):
        raise OrchestrationError(
            "INVALID_CLARIFICATION_CONTEXT",
            "Planner received unsupported clarification override fields",
        )
    updates = raw.get("argument_updates")
    if updates is not None and not isinstance(updates, Mapping):
        raise OrchestrationError(
            "INVALID_CLARIFICATION_CONTEXT",
            "Clarification argument_updates must be an object",
        )
    return dict(raw)


def _input_schema_accepts_account_id(capability: Mapping[str, Any]) -> bool:
    schema = capability.get("input_schema")
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    return isinstance(properties, Mapping) and "account_id" in properties


def _operation_type(capability: Mapping[str, Any]) -> OperationType:
    try:
        return OperationType(str(capability.get("operation_type") or ""))
    except ValueError as exc:
        raise OrchestrationError("INVALID_TOOL_OPERATION", "Tool capability has an invalid operation_type") from exc


def _risk_level(capability: Mapping[str, Any]) -> RiskLevel:
    try:
        return RiskLevel(str(capability.get("risk_level") or ""))
    except ValueError as exc:
        raise OrchestrationError("INVALID_TOOL_RISK", "Tool capability has an invalid risk_level") from exc


def _object_tuple(value: Any, *, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, Mapping):
        return (dict(value),)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise OrchestrationError("INVALID_TOOL_CONTRACT", f"Tool {field_name} must be an object or object list")
    return tuple(dict(item) for item in value)


def _customer_problem_recheck_context(context: ContextSnapshot) -> list[dict[str, Any]]:
    resources = context.values.get("resources")
    if not isinstance(resources, Mapping):
        raise OrchestrationError(
            "INVALID_PROBLEM_RECHECK_CONTEXT",
            "Customer problem recheck context must be an object",
        )
    raw = resources.get("customer_problem_open_refs")
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, Mapping) for item in raw)
    ):
        raise OrchestrationError(
            "INVALID_PROBLEM_RECHECK_CONTEXT",
            "Customer problem open references must be an object array",
        )
    refs = [dict(item) for item in raw]
    dedupe_keys = [str(item.get("dedupe_key") or "").strip() for item in refs]
    if any(not value for value in dedupe_keys) or len(dedupe_keys) != len(set(dedupe_keys)):
        raise OrchestrationError(
            "INVALID_PROBLEM_RECHECK_CONTEXT",
            "Customer problem open references contain missing or duplicate identities",
        )
    return sorted(refs, key=lambda item: str(item["dedupe_key"]))


_MONEY_FIELD_MARKERS = (
    "amount",
    "money",
    "fee",
    "price",
    "cost",
    "payment",
    "receivable",
    "payable",
    "profit",
    "rate",
)
_ENTITY_FIELD_MARKERS = (
    "tracking_number",
    "waybill_no",
    "external_id",
    "record_id",
    "problem_id",
    "receipt_id",
)


def _normalize_financial_values(value: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize money-bearing fields to exact Decimal strings."""

    def normalize(item: Any, path: tuple[str, ...]) -> Any:
        field_name = path[-1].lower() if path else ""
        if isinstance(item, Mapping):
            return {str(key): normalize(child, (*path, str(key))) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child, path) for child in item]
        if any(marker in field_name for marker in _MONEY_FIELD_MARKERS) and item not in (None, ""):
            if isinstance(item, bool):
                raise OrchestrationError("INVALID_DECIMAL_AMOUNT", f"{'.'.join(path)} must be a decimal value")
            try:
                decimal_value = Decimal(str(item))
            except (InvalidOperation, ValueError) as exc:
                raise OrchestrationError(
                    "INVALID_DECIMAL_AMOUNT",
                    f"{'.'.join(path)} must be a decimal value",
                ) from exc
            if not decimal_value.is_finite():
                raise OrchestrationError("INVALID_DECIMAL_AMOUNT", f"{'.'.join(path)} must be finite")
            return format(decimal_value, "f")
        return item

    return normalize(value, ())


def _derive_impact(
    command: Command,
    tool_name: str,
    operation_type: OperationType,
    account_id: str | None,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the approval display from governed inputs, never caller assertions."""

    money: dict[str, str] = {}
    entities = [ref.to_dict() for ref in command.entity_refs]

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                walk(child, (*path, str(key)))
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, str(index)))
            return
        field_name = path[-1].lower() if path else ""
        dotted = ".".join(path)
        if any(marker in field_name for marker in _MONEY_FIELD_MARKERS) and value not in (None, ""):
            money[dotted] = str(value)
        if any(marker == field_name for marker in _ENTITY_FIELD_MARKERS) and value not in (None, ""):
            entities.append(
                {
                    "entity_type": field_name,
                    "entity_id": str(value),
                    "source_system": "",
                    "relation_type": "impact",
                    "metadata": {"argument_path": dotted},
                }
            )

    walk(arguments, ())
    return {
        "tool_name": tool_name,
        "operation_type": operation_type.value,
        "account_id": account_id,
        "entities": entities,
        "amounts": money,
    }
