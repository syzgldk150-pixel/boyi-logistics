from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.orchestration.automation_project_api import (
    create_automation_project_router,
)
from agent.orchestration.models import Actor, ActorType


class _Receipt:
    def to_dict(self) -> dict[str, str]:
        return {"run_id": "run-1", "command_id": "command-1"}


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_policies(self) -> dict[str, Any]:
        return {"items": []}

    def update_policy(self, automation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((automation_id, kwargs))
        return {"automation_id": automation_id, "configured_mode": kwargs["mode"]}

    def pending_approvals(self, automation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((automation_id, kwargs))
        return {"automation_id": automation_id, "pending_count": 0}

    def decide_pending_approvals(
        self,
        automation_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((automation_id, kwargs))
        return {
            "automation_id": automation_id,
            "pending_count": 0,
            "decision": kwargs["decision"],
            "decided_count": 1,
            "run_receipts": [
                {
                    "automation_id": automation_id,
                    "work_item_id": "work-1",
                    "run_id": "run-1",
                    "status": "WAITING_APPROVAL",
                }
            ],
        }

    def invoke_console(self, automation_id: str, **kwargs: Any) -> _Receipt:
        self.calls.append((automation_id, kwargs))
        return _Receipt()


def _client() -> tuple[TestClient, _Service, Actor]:
    service = _Service()
    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    app = FastAPI()
    app.include_router(
        create_automation_project_router(
            service_provider=lambda: service,  # type: ignore[arg-type]
            actor_provider=lambda _request: actor,
        )
    )
    return TestClient(app), service, actor


def test_project_policy_route_derives_actor_and_accepts_only_cas_fields() -> None:
    client, service, actor = _client()

    response = client.post(
        "/internal/v1/automation-projects/project-a/approval-policy",
        json={
            "mode": "PROJECT_FULL_AUTO",
            "request_id": "request-1",
            "expected_policy_version": 3,
            "expected_project_configuration_version": 5,
        },
    )

    assert response.status_code == 200
    automation_id, payload = service.calls[-1]
    assert automation_id == "project-a"
    assert payload["actor"] is actor
    assert payload["expected_policy_version"] == 3
    assert payload["comment"] == ""

    rejected = client.post(
        "/internal/v1/automation-projects/project-a/approval-policy",
        json={
            "mode": "REQUIRE_EACH_RUN",
            "request_id": "request-2",
            "expected_policy_version": 4,
            "expected_project_configuration_version": 5,
            "actor": {"actor_id": "forged"},
        },
    )
    assert rejected.status_code == 422
    assert len(service.calls) == 1


def test_grouped_approval_route_never_accepts_approval_ids_or_plan_hashes() -> None:
    client, service, actor = _client()
    body = {
        "expected_pending_set_hash": "a" * 64,
        "request_id": "request-3",
        "comment": "approve visible set",
    }

    approved = client.post(
        "/internal/v1/automation-projects/project-a/pending-approvals/approve",
        json=body,
    )

    assert approved.status_code == 200
    automation_id, payload = service.calls[-1]
    assert automation_id == "project-a"
    assert payload["decision"] == "APPROVED"
    assert payload["actor"] is actor
    receipt = approved.json()["data"]["run_receipts"][0]
    assert receipt == {
        "automation_id": "project-a",
        "work_item_id": "work-1",
        "run_id": "run-1",
        "status": "WAITING_APPROVAL",
    }
    assert "approval_id" not in receipt
    assert "plan_hash" not in receipt

    rejected = client.post(
        "/internal/v1/automation-projects/project-a/pending-approvals/approve",
        json={**body, "approval_ids": ["forged"], "plan_hash": "b" * 64},
    )
    assert rejected.status_code == 422
    assert len(service.calls) == 1


def test_console_invoke_submits_only_server_resolved_project_identity() -> None:
    client, service, actor = _client()

    response = client.post(
        "/internal/v1/automation-projects/project-a/invoke",
        json={"request_id": "request-4"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"run_id": "run-1", "command_id": "command-1"}
    automation_id, payload = service.calls[-1]
    assert automation_id == "project-a"
    assert payload == {"request_id": "request-4", "actor": actor}
