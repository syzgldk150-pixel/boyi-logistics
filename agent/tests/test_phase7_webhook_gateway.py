from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from agent.orchestration.models import OrchestrationError


class _EntrypointsFacade:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke_webhook(self, **request):
        call = dict(request)
        call["envelope"] = {
            "body": dict(request["envelope"]["body"]),
            "query": dict(request["envelope"]["query"]),
        }
        self.calls.append(call)
        if call["route_key"] not in {
            "webhook/sign-status",
            "webhook/phase7/scan",
            "webhook/phase7/stats",
        }:
            raise OrchestrationError(
                "PROJECT_ROUTE_NOT_FOUND",
                "Automation project route is unavailable",
            )
        supplied = set(call["envelope"]["body"]) | set(call["envelope"]["query"])
        if "account_id" in supplied or "account_ids" in supplied:
            raise OrchestrationError(
                "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN",
                "Transport callers cannot override project account bindings",
            )
        return {
            "success": False,
            "status": "WAITING_APPROVAL",
            "run_id": f"run-{len(self.calls)}",
        }


def test_delivery_scan_and_arrival_webhooks_use_only_typed_project_routes() -> None:
    entrypoints = _EntrypointsFacade()
    client = TestClient(main.app)

    with (
        patch.object(main, "automation_project_entrypoints", entrypoints),
        patch.object(main, "_webhook_token", return_value="test-webhook-token"),
    ):
        headers = {main.WEBHOOK_TOKEN_HEADER: "test-webhook-token"}
        delivery = client.post(
            "/webhook/sign-status?source_event_id=delivery-1&BILL_CODE=R001",
            headers=headers,
            json={
                "RECORD_ID": "rec-1",
                "event_id": "transport-only",
            },
        )
        scan = client.post(
            "/webhook/phase7/scan?event_id=scan-1",
            headers=headers,
            json={"trigger_flow": False, "delivery_attempt": "transport-only"},
        )
        arrival = client.post(
            "/webhook/phase7/stats?id=arrival-1",
            headers=headers,
            json={
                "target_date": "2026-08-13",
                "dry_run": True,
                "trace": "transport-only",
            },
        )

    assert [response.status_code for response in (delivery, scan, arrival)] == [200, 200, 200]
    assert [call["route_key"] for call in entrypoints.calls] == [
        "webhook/sign-status",
        "webhook/phase7/scan",
        "webhook/phase7/stats",
    ]
    assert [call["source_event_id"] for call in entrypoints.calls] == [
        "delivery-1",
        "scan-1",
        "arrival-1",
    ]
    assert entrypoints.calls[0]["envelope"]["body"]["RECORD_ID"] == "rec-1"
    assert entrypoints.calls[1]["envelope"]["body"]["trigger_flow"] is False
    assert entrypoints.calls[2]["envelope"]["body"]["target_date"] == "2026-08-13"
    assert all(call["webhook_path"] == call["route_key"] for call in entrypoints.calls)


def test_arrival_webhook_does_not_inject_a_default_account() -> None:
    entrypoints = _EntrypointsFacade()
    client = TestClient(main.app)

    with (
        patch.object(main, "automation_project_entrypoints", entrypoints),
        patch.object(main, "_webhook_token", return_value="test-webhook-token"),
    ):
        response = client.post(
            "/webhook/phase7/stats?id=arrival-no-account",
            headers={main.WEBHOOK_TOKEN_HEADER: "test-webhook-token"},
            json={"target_date": "2026-08-13", "dry_run": True},
        )

    assert response.status_code == 200
    assert "account_id" not in entrypoints.calls[0]["envelope"]["body"]
    assert "account_id" not in entrypoints.calls[0]["envelope"]["query"]


def test_arrival_webhook_rejects_account_override() -> None:
    entrypoints = _EntrypointsFacade()
    client = TestClient(main.app)

    with (
        patch.object(main, "automation_project_entrypoints", entrypoints),
        patch.object(main, "_webhook_token", return_value="test-webhook-token"),
    ):
        response = client.post(
            "/webhook/phase7/stats?id=arrival-account-override",
            headers={main.WEBHOOK_TOKEN_HEADER: "test-webhook-token"},
            json={
                "account_id": "caller-controlled-account",
                "target_date": "2026-08-13",
                "dry_run": True,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN"
    assert "cannot override" in response.json()["error"]["message"]
    assert len(entrypoints.calls) == 1


def test_unbound_webhook_route_fails_closed_instead_of_returning_placeholder() -> None:
    entrypoints = _EntrypointsFacade()
    client = TestClient(main.app)

    with (
        patch.object(main, "automation_project_entrypoints", entrypoints),
        patch.object(main, "_webhook_token", return_value="test-webhook-token"),
    ):
        response = client.post(
            "/webhook/not-installed?id=unknown-route-1",
            headers={main.WEBHOOK_TOKEN_HEADER: "test-webhook-token"},
            json={},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_ROUTE_NOT_FOUND"
