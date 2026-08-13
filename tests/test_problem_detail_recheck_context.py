from __future__ import annotations

from agent.orchestration.control_plane_service import _customer_problem_open_refs
from agent.orchestration.models import Actor, ActorType, Command, ContextSnapshot
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


def test_open_problem_context_uses_exact_persisted_source_identity() -> None:
    assert _customer_problem_open_refs(_Uow()) == [
        {
            "dedupe_key": "problem:yunda:account-1:external-1",
            "platform": "yunda",
            "account_id": "account-1",
            "external_id": "external-1",
            "source_direction": "received",
            "waybill_no": "430000000001",
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
    assert plan.steps[0].operation_type.value == "read"
