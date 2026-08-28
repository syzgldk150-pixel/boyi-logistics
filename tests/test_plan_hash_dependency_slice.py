from __future__ import annotations

from dataclasses import replace

import pytest

from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OperationType,
    OrchestrationError,
    Plan,
    PlanStep,
    RiskLevel,
    sha256_json,
)
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner


def _capability(*, version: str = "1.0.0", evidence_name: str = "result") -> dict:
    return {
        "name": "governed_compute",
        "version": version,
        "description": "This presentation-only text is outside the v2 dependency slice",
        "operation_type": "compute",
        "risk_level": "low",
        "llm_exposed": False,
        "approval": {"mode": "none"},
        "permissions": {"required_roles": ["admin"]},
        "account_scope": {"mode": "single", "allow_implicit_default": False},
        "idempotency": {"mode": "parameters", "key_fields": []},
        "retry": {"safe": True, "max_attempts": 1},
        "evidence": [{"name": evidence_name}],
        "postconditions": [{"name": "computed"}],
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "executor": "agent.tools.compute",
        "timeout": 30,
        "heavy": False,
    }


class _Catalog:
    def __init__(self, capability: dict, *, unrelated_version: str = "1.0.0") -> None:
        self.capabilities = {
            "governed_compute": capability,
            "unrelated_tool": {
                **_capability(version=unrelated_version),
                "name": "unrelated_tool",
            },
        }

    @property
    def catalog_hash(self) -> str:
        return sha256_json(self.capabilities)

    def get_capability(self, tool_name: str):
        return self.capabilities.get(tool_name)

    @staticmethod
    def validate_arguments(_tool_name: str, _arguments) -> None:
        return None


def _context(
    *,
    selected_system: str = "ronghui",
    unrelated_system: str = "yunda",
) -> ContextSnapshot:
    accounts = (
        {
            "account_id": "account-1",
            "system": selected_system,
            "account_purpose": "general",
            "session_profile": "profile-1",
            "is_active": True,
            "name": "A presentation label that is not a binding",
        },
        {
            "account_id": "account-2",
            "system": unrelated_system,
            "account_purpose": "general",
            "session_profile": "profile-2",
            "is_active": True,
        },
    )
    return ContextSnapshot(
        values={
            "accounts": list(accounts),
            "resources": {"unrelated_dashboard_filter": "ignored"},
        },
        account_ids=("account-1", "account-2"),
        source_integrity={"unrelated_source": {"complete": True}},
    )


def _command(*, amount: str = "10.00") -> Command:
    return Command(
        command_id="command-1",
        command_type="tool.execute",
        source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1", ("admin",)),
        parameters={
            "tool_name": "governed_compute",
            "account_id": "account-1",
            "arguments": {"amount": amount, "target": "target-1"},
        },
        idempotency_key=f"console:admin-1:compute:{amount}",
    )


def _round_trip(plan: Plan) -> Plan:
    payload = plan.to_dict()
    steps = tuple(
        PlanStep(
            step_key=str(item["step_key"]),
            tool_name=str(item["tool_name"]),
            tool_version=str(item["tool_version"]),
            operation_type=OperationType(str(item["operation_type"])),
            arguments=dict(item["arguments"]),
            account_id=item.get("account_id"),
            depends_on=tuple(item.get("depends_on") or ()),
            idempotency_key=str(item["idempotency_key"]),
            expected_evidence=tuple(item.get("expected_evidence") or ()),
            postconditions=tuple(item.get("postconditions") or ()),
            risk_level=RiskLevel(str(item["risk_level"])),
            requires_approval=bool(item.get("requires_approval")),
        )
        for item in payload["steps"]
    )
    return Plan(
        schema_version=int(payload["schema_version"]),
        command_type=str(payload["command_type"]),
        context_fingerprint=str(payload["context_fingerprint"]),
        tool_catalog_hash=str(payload["tool_catalog_hash"]),
        steps=steps,
        impact=dict(payload["impact"]),
        automation_id=payload.get("automation_id"),
        automation_generation=payload.get("automation_generation"),
        automation_contract_hash=payload.get("automation_contract_hash"),
    )


def test_v2_ignores_unrelated_catalog_and_account_changes() -> None:
    catalog = _Catalog(_capability())
    plan = DeterministicPlanner(catalog).plan(_command(), _context())

    unrelated_catalog_change = _Catalog(_capability(), unrelated_version="9.0.0")
    unrelated_account_change = _context(unrelated_system="another-system")

    assert plan.schema_version == 2
    assert PlanValidator(unrelated_catalog_change).validate(
        plan,
        unrelated_account_change,
    ) is plan
    assert (
        DeterministicPlanner(unrelated_catalog_change)
        .plan(_command(), unrelated_account_change)
        .plan_hash
        == plan.plan_hash
    )


def test_v2_invalidates_selected_binding_arguments_and_contract_dependencies() -> None:
    catalog = _Catalog(_capability())
    context = _context()
    plan = DeterministicPlanner(catalog).plan(_command(), context)

    selected_binding_change = _context(selected_system="yunda")
    with pytest.raises(OrchestrationError) as changed_context:
        PlanValidator(catalog).validate(plan, selected_binding_change)
    assert changed_context.value.code == "CONTEXT_CHANGED"

    assert DeterministicPlanner(catalog).plan(
        _command(amount="11.00"),
        context,
    ).plan_hash != plan.plan_hash
    assert DeterministicPlanner(
        _Catalog(_capability(version="1.0.1"))
    ).plan(_command(), context).plan_hash != plan.plan_hash
    assert DeterministicPlanner(
        _Catalog(_capability(evidence_name="different-result"))
    ).plan(_command(), context).plan_hash != plan.plan_hash


def test_v2_automation_generation_and_contract_remain_top_level_dependencies() -> None:
    base = DeterministicPlanner(_Catalog(_capability())).plan(_command(), _context())
    project_plan = replace(
        base,
        context_fingerprint=sha256_json({"account_bindings": []}),
        automation_id="arrival_stats",
        automation_generation=1,
        automation_contract_hash="a" * 64,
    )

    assert replace(project_plan, automation_generation=2).plan_hash != project_plan.plan_hash
    assert replace(
        project_plan,
        automation_contract_hash="b" * 64,
    ).plan_hash != project_plan.plan_hash


def test_v1_round_trip_keeps_full_context_and_catalog_hash_semantics() -> None:
    catalog = _Catalog(_capability())
    context = _context()
    plan = DeterministicPlanner(catalog).plan(
        _command(),
        context,
        schema_version=1,
    )
    restored = _round_trip(plan)

    assert restored.schema_version == 1
    assert restored.context_fingerprint == context.fingerprint
    assert restored.tool_catalog_hash == catalog.catalog_hash
    assert restored.plan_hash == plan.plan_hash
    assert restored.to_dict() == plan.to_dict()


def test_plan_schema_rejects_boolean_version() -> None:
    plan = DeterministicPlanner(_Catalog(_capability())).plan(_command(), _context())

    with pytest.raises(OrchestrationError) as raised:
        replace(plan, schema_version=True)

    assert raised.value.code == "UNSUPPORTED_PLAN_SCHEMA"
