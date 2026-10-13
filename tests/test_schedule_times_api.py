"""The schedule route syncs one project's jobs after a times-only save."""
import pytest
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.automation_plugins.management_api import create_automation_plugin_management_router
from tests.automation_plugin_management_api_support import _console_actor
from shared.orchestration_repository_support import _json_hash


class _ScheduleService:
    def __init__(self, *, times_only: bool) -> None:
        self.times_only = times_only
        self.calls: list[dict[str, Any]] = []

    def save_console_schedule(self, automation_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"automation_id": automation_id, **kwargs})
        result = {
            "automation_id": automation_id,
            "project_configuration_version": 4,
            "schedule": kwargs["schedule"],
            "schedule_sha256": _json_hash(kwargs["schedule"]),
            "enabled_entrypoints": ["daily_run"],
            "runtime_model": "SERVICE_V2",
            "entrypoint_kinds": {"daily_run": "scheduler"},
            "generation_ready": True,
        }
        if self.times_only:
            result["schedule_times_only"] = True
        return result


def _client(service, refreshes, syncs):
    app = FastAPI()
    app.include_router(create_automation_plugin_management_router(
        service_provider=lambda: service,
        actor_provider=lambda _request: _console_actor(),
        scheduler_refresh_provider=lambda: refreshes.append(True) or {"initialized": True, "invalid_tasks": []},
        scheduler_project_sync_provider=(
            lambda automation_id: syncs.append(automation_id) or {"initialized": True, "invalid_tasks": []}
        ),
    ))
    return TestClient(app)


def _payload(**extra):
    return {
        "schedule": {"kind": "daily_times", "times": ["08:00", "09:30"], "enabled": True},
        "request_id": str(uuid4()),
        "expected_project_configuration_version": 4,
        **extra,
    }


def test_times_only_save_syncs_only_its_project_jobs():
    refreshes, syncs = [], []
    service = _ScheduleService(times_only=True)
    expected = {"kind": "daily_times", "times": ["08:00"], "enabled": True}
    response = _client(service, refreshes, syncs).put(
        "/internal/v1/automation/instances/automation-1/schedule", json=_payload(expected_schedule_sha256=_json_hash(expected)),
    )
    assert response.status_code == 200
    assert response.json()["data"]["schedule_runtime_state"] == "ACTIVE"
    assert syncs == ["automation-1"] and refreshes == []
    assert service.calls[0]["expected_schedule_sha256"] == _json_hash(expected)


def test_generation_switch_keeps_the_full_scheduler_refresh():
    refreshes, syncs = [], []
    service = _ScheduleService(times_only=False)
    response = _client(service, refreshes, syncs).put(
        "/internal/v1/automation/instances/automation-1/schedule", json=_payload(),
    )
    assert response.status_code == 200
    assert refreshes == [True] and syncs == []
    assert service.calls[0]["expected_schedule_sha256"] is None


def test_failed_project_sync_reports_refresh_failed():
    service = _ScheduleService(times_only=True)
    app = FastAPI()

    def broken(_automation_id):
        raise RuntimeError("sync failed")

    app.include_router(create_automation_plugin_management_router(
        service_provider=lambda: service, actor_provider=lambda _request: _console_actor(),
        scheduler_refresh_provider=lambda: {"initialized": True}, scheduler_project_sync_provider=broken,
    ))
    response = TestClient(app).put(
        "/internal/v1/automation/instances/automation-1/schedule",
        json=_payload(expected_schedule_sha256="a" * 64),
    )
    assert response.status_code == 200
    assert response.json()["data"]["schedule_runtime_state"] == "REFRESH_FAILED"


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 64, "x" * 64, 42, {}, ""])
def test_schedule_route_rejects_invalid_expected_digest(digest):
    service = _ScheduleService(times_only=True)
    response = _client(service, [], []).put(
        "/internal/v1/automation/instances/automation-1/schedule",
        json=_payload(expected_schedule_sha256=digest),
    )
    assert response.status_code == 422
    assert service.calls == []


def test_schedule_route_rejects_retired_schedule_dto_cas_field():
    service = _ScheduleService(times_only=True)
    response = _client(service, [], []).put(
        "/internal/v1/automation/instances/automation-1/schedule",
        json=_payload(expected_schedule={"kind": "daily_times", "times": ["08:00"], "enabled": True}),
    )
    assert response.status_code == 422
    assert service.calls == []
