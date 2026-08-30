"""Strict reconstruction of persisted automation project plans."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.orchestration.models import (
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
)


def plan_from_mapping(raw: Any) -> Plan:
    """Rebuild a persisted plan only when its closed shape and hash still match."""

    if not isinstance(raw, Mapping):
        raise OrchestrationError("INVALID_PERSISTED_PLAN", "Persisted plan is invalid")
    expected_keys = {
        "schema_version",
        "command_type",
        "context_fingerprint",
        "tool_catalog_hash",
        "steps",
        "impact",
        "automation_id",
        "automation_generation",
        "automation_contract_hash",
        "plan_hash",
    }
    if set(raw) != expected_keys or type(raw.get("schema_version")) is not int:
        raise OrchestrationError("INVALID_PERSISTED_PLAN", "Persisted plan is invalid")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise OrchestrationError("INVALID_PERSISTED_PLAN", "Persisted plan is invalid")
    step_keys = {
        "step_key",
        "tool_name",
        "tool_version",
        "operation_type",
        "arguments",
        "account_id",
        "depends_on",
        "idempotency_key",
        "expected_evidence",
        "postconditions",
        "risk_level",
        "requires_approval",
    }
    steps: list[PlanStep] = []
    for item in raw_steps:
        if not isinstance(item, Mapping) or set(item) != step_keys:
            raise OrchestrationError(
                "INVALID_PERSISTED_PLAN",
                "Persisted plan step is invalid",
            )
        if (
            not isinstance(item.get("arguments"), Mapping)
            or not isinstance(item.get("depends_on"), list)
            or not isinstance(item.get("expected_evidence"), list)
            or not isinstance(item.get("postconditions"), list)
            or type(item.get("requires_approval")) is not bool
        ):
            raise OrchestrationError(
                "INVALID_PERSISTED_PLAN",
                "Persisted plan step is invalid",
            )
        account_id = item.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            raise OrchestrationError(
                "INVALID_PERSISTED_PLAN",
                "Persisted plan account binding is invalid",
            )
        try:
            operation_type = OperationType(item["operation_type"])
            risk_level = RiskLevel(item["risk_level"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OrchestrationError(
                "INVALID_PERSISTED_PLAN",
                "Persisted plan governance is invalid",
            ) from exc
        steps.append(
            PlanStep(
                step_key=str(item["step_key"]),
                tool_name=str(item["tool_name"]),
                tool_version=str(item["tool_version"]),
                operation_type=operation_type,
                arguments=dict(item["arguments"]),
                account_id=account_id,
                depends_on=tuple(str(value) for value in item["depends_on"]),
                idempotency_key=str(item["idempotency_key"]),
                expected_evidence=tuple(item["expected_evidence"]),
                postconditions=tuple(item["postconditions"]),
                risk_level=risk_level,
                requires_approval=item["requires_approval"],
            )
        )
    if not isinstance(raw.get("impact"), Mapping):
        raise OrchestrationError("INVALID_PERSISTED_PLAN", "Persisted plan is invalid")
    try:
        plan = Plan(
            command_type=str(raw["command_type"]),
            context_fingerprint=str(raw["context_fingerprint"]),
            tool_catalog_hash=str(raw["tool_catalog_hash"]),
            steps=tuple(steps),
            impact=dict(raw["impact"]),
            automation_id=raw["automation_id"],
            automation_generation=raw["automation_generation"],
            automation_contract_hash=raw["automation_contract_hash"],
            schema_version=raw["schema_version"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OrchestrationError(
            "INVALID_PERSISTED_PLAN",
            "Persisted plan is invalid",
        ) from exc
    if plan.plan_hash != raw.get("plan_hash"):
        raise OrchestrationError(
            "PLAN_HASH_MISMATCH",
            "Persisted plan hash does not match its content",
        )
    return plan
