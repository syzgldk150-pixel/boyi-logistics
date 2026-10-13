"""Monitoring reads accept only the source and server-owned category handle."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.tms_runtime.routes import router
from shared.identity_routes import agent_permission


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_items_route_passes_bounded_page_and_ignores_account_overrides(client):
    expected = {"ok": True, "items": [], "total": 0, "page": 2, "page_size": 20}
    with patch("agent.tms_runtime.routes.build_monitoring_items", return_value=expected) as collect:
        response = client.get("/admin/monitoring/items", params={
            "system": "yunda", "category_id": "yunda:work_orders", "page": "2", "force": "1",
            "account_id": "other", "session_profile": "other", "url": "https://invalid.test/",
            "page_size": "1000",
        })
    assert response.status_code == 200
    assert response.json() == expected
    collect.assert_called_once_with(system="yunda", category_id="yunda:work_orders", page=2, force=True)


def test_items_route_defaults_to_first_page(client):
    with patch("agent.tms_runtime.routes.build_monitoring_items", return_value={"ok": True}) as collect:
        response = client.get("/admin/monitoring/items?system=ronghui")
    assert response.status_code == 200
    collect.assert_called_once_with(system="ronghui", category_id="", page=1, force=False)


@pytest.mark.parametrize("params", [
    {"system": "other"}, {"system": "yunda", "page": "0"},
    {"system": "yunda", "page": "10001"}, {"system": "yunda", "page": "1.5"},
    {"system": "yunda", "page": "invalid"}, {},
])
def test_items_route_rejects_invalid_source_or_page_before_collection(client, params):
    with patch("agent.tms_runtime.routes.build_monitoring_items") as collect:
        response = client.get("/admin/monitoring/items", params=params)
    assert response.status_code == 422
    collect.assert_not_called()


def test_detail_link_does_not_accept_browser_target_metadata(client):
    with patch("agent.tms_runtime.routes.build_monitoring_detail_link", return_value={"ok": True}) as build:
        response = client.post("/admin/monitoring/detail-link", json={
            "system": "yunda", "category_id": "yunda:001", "resource_id": "attacker",
            "title": "Other", "type_code": "999", "target_title": "Other",
            "account_id": "other", "session_profile": "other", "url": "https://invalid.test/",
        })
    assert response.status_code == 200
    build.assert_called_once_with({"system": "yunda", "category_id": "yunda:001"})


@pytest.mark.parametrize("method,path", [
    ("GET", "/internal/v1/admin/monitoring/items"),
    ("POST", "/internal/v1/admin/monitoring/detail-link"),
])
def test_monitoring_reads_keep_super_admin_permission(method, path):
    assert agent_permission(method, path) == "super_admin"
