from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from agent.tool_registry import ToolRegistry


class _GatewayFacade:
    def __init__(self) -> None:
        self.registry = ToolRegistry()
        self.calls: list[tuple[str, dict, dict]] = []

    async def execute_tool(self, tool_name: str, arguments: dict, **trusted_context):
        self.calls.append((tool_name, dict(arguments), dict(trusted_context)))
        return {
            "success": False,
            "status": "WAITING_APPROVAL",
            "run_id": f"run-{tool_name}",
        }


def test_delivery_scan_and_arrival_webhooks_submit_schema_mapped_gateway_commands() -> None:
    runtime = _GatewayFacade()
    resources = {
        "phase7.scan_webhook": {"path": "webhook/phase7/scan"},
        "phase7.stats_webhook": {"path": "webhook/phase7/stats"},
    }
    client = TestClient(main.app)

    with (
        patch.object(main, "agent_core", runtime),
        patch.object(main, "_webhook_token", return_value="test-webhook-token"),
        patch.object(main, "get_workflow_resource", side_effect=lambda key: resources.get(key)),
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
    assert [(name, arguments) for name, arguments, _context in runtime.calls] == [
        (
            "sync_delivery_status",
            {
                "account_id": "ronghui_default",
                "BILL_CODE": "R001",
                "RECORD_ID": "rec-1",
            },
        ),
        ("sync_scan_codes", {"trigger_flow": False}),
        (
            "sync_arrival_stats",
            {
                "account_id": "ronghui_default",
                "target_date": "2026-08-13",
                "dry_run": True,
            },
        ),
    ]
    expected = [
        ("webhook:sign-status:delivery-1", "delivery-1"),
        ("webhook:phase7/scan:scan-1", "scan-1"),
        ("webhook:phase7/stats:arrival-1", "arrival-1"),
    ]
    for (_name, arguments, context), (idempotency_key, event_id) in zip(
        runtime.calls,
        expected,
        strict=True,
    ):
        assert context["source"] == "webhook"
        assert context["idempotency_key"] == idempotency_key
        assert context["execution_context"]["source_event_id"] == event_id
        assert not {"source_event_id", "event_id", "id"}.intersection(arguments)


def test_arrival_webhook_binds_the_code_approved_account_when_legacy_payload_omits_it() -> None:
    runtime = _GatewayFacade()
    resources = {
        "phase7.stats_webhook": {"path": "webhook/phase7/stats"},
    }
    client = TestClient(main.app)

    with (
        patch.object(main, "agent_core", runtime),
        patch.object(main, "_webhook_token", return_value="test-webhook-token"),
        patch.object(main, "get_workflow_resource", side_effect=lambda key: resources.get(key)),
    ):
        response = client.post(
            "/webhook/phase7/stats?id=arrival-no-account",
            headers={main.WEBHOOK_TOKEN_HEADER: "test-webhook-token"},
            json={"target_date": "2026-08-13", "dry_run": True},
        )

    assert response.status_code == 200
    assert runtime.calls[0][0] == "sync_arrival_stats"
    assert runtime.calls[0][1]["account_id"] == "ronghui_default"


def test_arrival_webhook_rejects_account_override() -> None:
    runtime = _GatewayFacade()
    resources = {
        "phase7.stats_webhook": {"path": "webhook/phase7/stats"},
    }
    client = TestClient(main.app)

    with (
        patch.object(main, "agent_core", runtime),
        patch.object(main, "_webhook_token", return_value="test-webhook-token"),
        patch.object(main, "get_workflow_resource", side_effect=lambda key: resources.get(key)),
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
    assert "cannot override" in response.json()["detail"]
    assert runtime.calls == []
