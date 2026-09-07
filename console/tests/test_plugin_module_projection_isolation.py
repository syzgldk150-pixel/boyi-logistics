"""Malformed module facts and ambiguous identities do not hide independent instances."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from console.services.automation_catalog_projection import normalize_automation_plugin_catalog
from console.app import LocalDocFlowApp
from console.tests.test_automation_plugins import _catalog_payload, _plugin_instance, _plugin_package


@pytest.mark.parametrize("management", [None, {}, {"purpose": "collector", "module": [], "dataset": "", "version": "1"}])
def test_invalid_instance_management_is_reported_without_breaking_healthy_instance(management):
    catalog = _catalog_payload()
    catalog["instances"][0]["management"] = management
    _, instances, unsupported = normalize_automation_plugin_catalog(catalog)
    assert [row["automation_id"] for row in instances] == ["finance_action_south"]
    assert unsupported == ["finance_action_east"]


@pytest.mark.parametrize("duplicate", ["plugin", "instance"])
def test_duplicate_identity_rejects_all_candidates_and_preserves_independent_package(duplicate):
    catalog = _catalog_payload()
    if duplicate == "plugin":
        catalog["plugins"].append(deepcopy(catalog["plugins"][0]))
        rejected = {"finance_action_east", "finance_action_south"}
    else:
        catalog["instances"].append(deepcopy(catalog["instances"][0]))
        rejected = {"finance_action_east"}
    package = _plugin_package()
    package["plugin_id"] = "independent_action"
    instance = _plugin_instance("independent_instance", "Independent")
    instance["plugin_id"] = "independent_action"
    catalog["plugins"].append(package)
    catalog["instances"].append(instance)
    _, instances, unsupported = normalize_automation_plugin_catalog(catalog)
    assert "independent_instance" in {row["automation_id"] for row in instances}
    assert not rejected.intersection(row["automation_id"] for row in instances)
    assert set(unsupported) == rejected


def test_real_catalog_transport_keeps_healthy_rows_when_bad_identity_is_traceable():
    catalog = _catalog_payload()
    catalog["instances"][0]["management"] = {"module": "unknown"}
    service = LocalDocFlowApp.__new__(LocalDocFlowApp)
    service._mysql_console_principal = lambda _user: {"actor_id": "synthetic-admin", "roles": ["super_admin"]}
    service._agent_request = lambda *_args, **_options: {"ok": True, "data": catalog}
    result = service._load_automation_plugin_catalog_uncached(SimpleNamespace(current_admin_user={}), module="automation", summary=True)
    assert [row["automation_id"] for row in result[1]] == ["finance_action_south"]
    assert result[3] == ["finance_action_east"]


@pytest.mark.parametrize("policy_mode", ["valid", "missing", "duplicate", "error"])
def test_summary_policy_is_used_without_a_second_request_and_rejects_ambiguity(policy_mode):
    from console.tests.test_automation_approval_policy import POLICY_ITEM

    catalog = _catalog_payload()
    policies = [{**POLICY_ITEM, "automation_id": item["automation_id"]} for item in catalog["instances"]]
    if policy_mode == "missing":
        policies.pop(0)
    if policy_mode == "duplicate":
        policies.append(deepcopy(policies[0]))
    catalog["project_policies"] = {"items": policies}
    if policy_mode == "error":
        catalog["project_policies"]["error_code"] = "PROJECT_POLICY_SERVICE_UNAVAILABLE"
    service = LocalDocFlowApp.__new__(LocalDocFlowApp)
    service._mysql_console_principal = lambda _user: {"actor_id": "synthetic-admin", "roles": ["super_admin"]}
    service._agent_request = lambda *_args, **_options: {"ok": True, "data": catalog}
    handler = SimpleNamespace(current_admin_user={})
    result = service._load_automation_plugin_catalog_uncached(handler, module="automation", summary=True)
    service._agent_request = lambda *_args, **_options: pytest.fail("summary already carries authoritative policy facts")
    tasks = [{"task_id": item["automation_id"], "plugin": item, "can_run_now": True} for item in result[1]]
    service._load_automation_project_policies(handler, tasks)
    assert tasks[0]["can_run_now"] is (policy_mode == "valid")
    if policy_mode in {"missing", "duplicate"}:
        assert tasks[1]["can_run_now"] is True


def test_shared_display_cache_still_separates_roles_and_rechecks_session():
    service = LocalDocFlowApp.__new__(LocalDocFlowApp)
    service._mysql_console_principal = lambda user: user
    requests = []
    def fetch(_handler, **options):
        requests.append(options)
        return ([], [], [], [], frozenset(), "", True)
    service._load_automation_plugin_catalog_uncached = fetch
    def load(actor, roles):
        return service._load_automation_plugin_catalog(SimpleNamespace(current_admin_user={"actor_id": actor, "roles": roles}), module="finance", summary=True)
    load("admin-a", ["super_admin"])
    load("admin-b", ["super_admin"])
    assert len(requests) == 1
    load("admin-a", ["admin"])
    assert len(requests) == 2
    service._clear_automation_plugin_catalog_cache()
    load("admin-b", ["super_admin"])
    assert len(requests) == 3
