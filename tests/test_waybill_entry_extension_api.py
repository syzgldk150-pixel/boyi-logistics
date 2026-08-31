from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.orchestration.automation_project_api import create_automation_project_router
from agent.orchestration.models import Actor, ActorType
from shared.waybill_entry_extensions import WAYBILL_ENTRY_DRAFT_FIELDS


REQUEST_ID = "11111111-1111-4111-8111-111111111111"
HANDLE = "a" * 64


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_module_slots(self, *, actor: Actor) -> dict[str, Any]:
        self.calls.append(("list", {"actor": actor}))
        return {
            "module_slots": [
                {
                    "slot": "waybill_entry.actions",
                    "handle": HANDLE,
                    "title": "Action",
                }
            ]
        }

    async def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("invoke", kwargs))
        if kwargs["slot"] == "waybill_entry.actions":
            return {
                "kind": "action",
                "receipt": {
                    "command_id": "command-1",
                    "work_item_id": "work-1",
                    "run_id": "run-1",
                    "status": "RECEIVED",
                    "reused": False,
                    "next_poll_after_ms": 1000,
                },
            }
        return {"kind": "validator", "validation": {"valid": True, "issues": []}}

    async def invoke_active_validators(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("invoke_active", kwargs))
        return {
            "kind": "validator_set",
            "validation": {"valid": True, "issues": []},
        }


def _draft() -> dict[str, str]:
    return {field: "" for field in WAYBILL_ENTRY_DRAFT_FIELDS}


def _client() -> tuple[TestClient, _Host, Actor]:
    host = _Host()
    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    app = FastAPI()
    app.include_router(
        create_automation_project_router(
            service_provider=lambda: object(),  # type: ignore[arg-type]
            actor_provider=lambda _request: actor,
            module_slot_host_provider=lambda: host,  # type: ignore[arg-type]
        )
    )
    return TestClient(app), host, actor


def test_module_slot_routes_return_frozen_flat_shapes_and_derive_actor() -> None:
    client, host, actor = _client()

    listed = client.get("/internal/v1/automation-projects/module-slots/waybill-entry")
    action = client.post(
        f"/internal/v1/automation-projects/module-slots/waybill-entry/waybill_entry.actions/{HANDLE}/invoke",
        json={"request_id": REQUEST_ID, "waybill": _draft()},
    )
    validator = client.post(
        f"/internal/v1/automation-projects/module-slots/waybill-entry/waybill_entry.validators/{HANDLE}/invoke",
        json={"request_id": REQUEST_ID, "waybill": _draft()},
    )

    assert listed.status_code == 200
    assert listed.json() == {
        "ok": True,
        "data": {
            "module_slots": [
                {
                    "slot": "waybill_entry.actions",
                    "handle": HANDLE,
                    "title": "Action",
                }
            ]
        },
        "error": None,
    }
    assert action.status_code == 202
    assert action.json()["data"] == {
        "kind": "action",
        "receipt": {
            "command_id": "command-1",
            "work_item_id": "work-1",
            "run_id": "run-1",
            "status": "RECEIVED",
            "reused": False,
            "next_poll_after_ms": 1000,
        },
    }
    assert validator.status_code == 200
    assert validator.json()["data"] == {
        "kind": "validator",
        "validation": {"valid": True, "issues": []},
    }
    assert host.calls[0] == ("list", {"actor": actor})
    assert set(host.calls[1][1]) == {"slot", "handle", "request_id", "waybill", "actor"}
    assert host.calls[1][1]["actor"] is actor


def test_module_slot_invoke_rejects_internal_identity_fields_and_bad_uuid() -> None:
    client, host, _actor = _client()
    path = f"/internal/v1/automation-projects/module-slots/waybill-entry/waybill_entry.actions/{HANDLE}/invoke"

    forged = client.post(
        path,
        json={
            "request_id": REQUEST_ID,
            "waybill": _draft(),
            "automation_id": "forged",
            "generation": 99,
            "contribution_id": "forged",
            "service": "forged",
            "operation": "forged",
            "effect": "external_write",
            "actor": {"actor_id": "forged"},
            "roles": ["super_admin"],
            "args": {"html": "<script>"},
        },
    )
    malformed = client.post(path, json={"request_id": "not-a-uuid", "waybill": _draft()})

    assert forged.status_code == 422
    assert malformed.status_code == 422
    assert host.calls == []


def test_active_validator_set_route_is_one_closed_authoritative_request() -> None:
    client, host, actor = _client()
    path = "/internal/v1/automation-projects/module-slots/waybill-entry/validators/invoke-active"

    response = client.post(path, json={"request_id": REQUEST_ID, "waybill": _draft()})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "kind": "validator_set",
            "validation": {"valid": True, "issues": []},
        },
        "error": None,
    }
    assert host.calls == [
        (
            "invoke_active",
            {
                "request_id": REQUEST_ID,
                "waybill": _draft(),
                "actor": actor,
            },
        )
    ]

    forged = client.post(
        path,
        json={
            "request_id": REQUEST_ID,
            "waybill": _draft(),
            "handles": [HANDLE],
            "automation_id": "forged",
        },
    )
    assert forged.status_code == 422
    assert len(host.calls) == 1


def test_main_composition_registers_closed_module_slot_routes() -> None:
    import main

    methods_by_path = {route.path: set(route.methods or ()) for route in main.app.routes if hasattr(route, "methods")}

    assert methods_by_path["/internal/v1/automation-projects/module-slots/waybill-entry"] == {"GET"}
    assert methods_by_path["/internal/v1/automation-projects/module-slots/waybill-entry/validators/invoke-active"] == {
        "POST"
    }
    assert methods_by_path["/internal/v1/automation-projects/module-slots/waybill-entry/{slot}/{handle}/invoke"] == {
        "POST"
    }
