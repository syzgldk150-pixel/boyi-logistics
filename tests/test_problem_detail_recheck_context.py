from __future__ import annotations

import pytest

from agent.automation_plugins.first_party_handlers import customer_problem_identity
from agent.orchestration.control_plane_service import _customer_problem_open_refs
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OrchestrationError,
)
from agent.orchestration.planner import DeterministicPlanner


class _Catalog:
    catalog_hash = "catalog-hash"

    @staticmethod
    def get_capability(tool_name: str):
        if tool_name != "sync_customer_service_problems":
            return None
        return {
            "version": "1.1.0",
            "operation_type": "read",
            "risk_level": "low",
            "llm_exposed": False,
            "evidence": {"required": True},
            "postconditions": [{"name": "configured_accounts_queried"}],
        }


class _WorkItems:
    @staticmethod
    def list_by_type(item_type: str):
        assert item_type == "CUSTOMER_SERVICE_PROBLEM"
        return [
            {
                "work_item_id": "item-1",
                "dedupe_key": "problem:yunda:account-1:external-1",
                "status": "OPEN",
            }
        ]

    @staticmethod
    def list_entities(work_item_id: str):
        assert work_item_id == "item-1"
        return [
            {
                "relation_type": "subject",
                "entity_type": "customer_problem",
                "entity_id": "external-1",
                "source_system": "yunda",
                "metadata_json": {
                    "account_id": "account-1",
                    "source_direction": "received",
                },
            },
            {
                "relation_type": "related",
                "entity_type": "waybill",
                "entity_id": "430000000001",
                "source_system": "yunda",
                "metadata_json": {"account_id": "account-1"},
            },
        ]


class _Evidence:
    @staticmethod
    def list(_work_item_id: str, *, limit: int):
        assert limit == 500
        return []


class _Uow:
    work_items = _WorkItems()
    evidence = _Evidence()


class _MissingSubjectWorkItems:
    @staticmethod
    def list_by_type(item_type: str):
        assert item_type == "CUSTOMER_SERVICE_PROBLEM"
        return [
            {
                "work_item_id": "item-missing-subject",
                "dedupe_key": "problem:v1:" + ("a" * 64),
                "status": "OPEN",
            }
        ]

    @staticmethod
    def list_entities(work_item_id: str):
        assert work_item_id == "item-missing-subject"
        return []


class _MissingSubjectUow:
    work_items = _MissingSubjectWorkItems()
    evidence = _Evidence()


def test_open_problem_context_uses_exact_persisted_source_identity() -> None:
    opaque_key = customer_problem_identity(
        account_id="account-1",
        platform="yunda",
        external_id="external-1",
    )
    assert _customer_problem_open_refs(_Uow()) == [
        {
            "dedupe_key": opaque_key,
            "platform": "yunda",
            "external_id": "external-1",
            "source_direction": "received",
            "waybill_no": "430000000001",
        }
    ]


def test_open_problem_context_omits_invalid_optional_identity_values() -> None:
    opaque_key = "problem:v1:" + ("a" * 64)

    assert _customer_problem_open_refs(_MissingSubjectUow()) == [
        {
            "dedupe_key": opaque_key,
            "context_error": "SUBJECT_ENTITY_MISSING",
        }
    ]


def test_planner_overwrites_caller_rechecks_with_authoritative_context() -> None:
    trusted_refs = _customer_problem_open_refs(_Uow())
    context = ContextSnapshot(
        values={
            "resources": {"customer_problem_open_refs": trusted_refs},
            "accounts": [{"account_id": "account-1", "system": "yunda"}],
        },
        account_ids=("account-1",),
    )
    command = Command(
        command_type="tool.execute",
        source="scheduler",
        actor=Actor(ActorType.SCHEDULER, "scheduler"),
        parameters={
            "tool_name": "sync_customer_service_problems",
            "arguments": {
                "direction": "both",
                "recheck_items": [
                    {
                        "dedupe_key": "problem:yunda:forged:forged",
                        "platform": "yunda",
                        "account_id": "forged",
                        "external_id": "forged",
                        "source_direction": "received",
                    }
                ],
            },
        },
        idempotency_key="scheduler:customer-problems:2026-08-13T00:00:00+08:00",
    )

    plan = DeterministicPlanner(_Catalog()).plan(command, context)

    assert plan.steps[0].arguments["recheck_items"] == trusted_refs
    assert "account_id" not in plan.steps[0].arguments["recheck_items"][0]
    assert plan.steps[0].operation_type.value == "read"


def test_planner_rejects_missing_authoritative_recheck_context() -> None:
    command = Command(
        command_type="tool.execute",
        source="scheduler",
        actor=Actor(ActorType.SCHEDULER, "scheduler"),
        parameters={
            "tool_name": "sync_customer_service_problems",
            "arguments": {"direction": "both"},
        },
        idempotency_key="scheduler:customer-problems:missing-rechecks",
    )

    context = ContextSnapshot(
        values={"resources": {}, "accounts": []},
        account_ids=(),
    )
    with pytest.raises(
        OrchestrationError,
        match="must be an object array",
    ):
        DeterministicPlanner(_Catalog()).plan(command, context)


def test_planner_accepts_empty_authoritative_recheck_context() -> None:
    context = ContextSnapshot(
        values={
            "resources": {"customer_problem_open_refs": []},
            "accounts": [],
        },
        account_ids=(),
    )
    command = Command(
        command_type="tool.execute",
        source="scheduler",
        actor=Actor(ActorType.SCHEDULER, "scheduler"),
        parameters={
            "tool_name": "sync_customer_service_problems",
            "arguments": {"direction": "both"},
        },
        idempotency_key="scheduler:customer-problems:empty-rechecks",
    )

    plan = DeterministicPlanner(_Catalog()).plan(command, context)

    assert plan.steps[0].arguments["recheck_items"] == []
