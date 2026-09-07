"""Concurrent refresh and mutation races use the production Console cache."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

from console.services.automation_projects import AutomationProjectsServiceMixin


def make_service(reader):
    service = AutomationProjectsServiceMixin.__new__(AutomationProjectsServiceMixin)
    service._mysql_console_principal = lambda user: {"actor_id": user["id"], "roles": [user["role"]]}
    service._load_automation_plugin_catalog_uncached = reader
    return service


def result(name):
    return ([{"name": name}], [], [], [], frozenset(), "", True)


def test_same_actor_module_refresh_joins_and_different_actor_does_not_share():
    started, release = Event(), Event()
    calls = []
    def read(handler, **scope):
        calls.append((handler.current_admin_user["id"], scope))
        started.set()
        assert release.wait(3)
        return result(handler.current_admin_user["id"])
    service = make_service(read)
    actor = SimpleNamespace(current_admin_user={"id": "one", "role": "super_admin"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(service._load_automation_plugin_catalog, actor, module="finance", summary=True)
        assert started.wait(3)
        second = pool.submit(service._load_automation_plugin_catalog, actor, module="finance", summary=True)
        release.set()
        assert first.result() == second.result() == result("one")
    assert len(calls) == 1
    actor.current_admin_user = {"id": "two", "role": "viewer"}
    assert service._load_automation_plugin_catalog(actor, module="finance", summary=True) == result("two")
    assert len(calls) == 2


def test_inflight_response_cannot_repopulate_cache_after_uninstall_invalidation():
    started, release = Event(), Event()
    calls = []
    def read(handler, **scope):
        calls.append(scope)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
            return result("removed")
        return result("current")
    service = make_service(read)
    actor = SimpleNamespace(current_admin_user={"id": "one", "role": "super_admin"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service._load_automation_plugin_catalog, actor, module="automation", summary=True)
        assert started.wait(3)
        service._clear_automation_plugin_catalog_cache()
        assert service._load_automation_plugin_catalog(actor, module="automation", summary=True) == result("current")
        release.set()
        stale = first.result()
        assert stale[0] == stale[1] == []
        assert stale[5] == "插件目录已变化，请刷新后继续。"
    assert service._load_automation_plugin_catalog(actor, module="automation", summary=True) == result("current")
    assert len(calls) == 2


def test_first_unavailable_catalog_does_not_invent_or_cache_runnable_projects():
    calls = []
    def read(_handler, **scope):
        calls.append(scope)
        if len(calls) == 1:
            return ([], [], [], [], frozenset(), "isolated Agent unavailable", False)
        return result("verified-current")
    service = make_service(read)
    actor = SimpleNamespace(current_admin_user={"id": "one", "role": "super_admin"})
    unavailable = service._load_automation_plugin_catalog(actor, module="finance", summary=True)
    assert unavailable[0] == [] and unavailable[5] == "isolated Agent unavailable" and unavailable[6] is False
    assert service._load_automation_plugin_catalog(actor, module="finance", summary=True) == result("verified-current")
    assert len(calls) == 2


def test_cache_scope_changes_when_the_same_actor_loses_permissions():
    calls = []
    def read(handler, **_scope):
        calls.append(handler.current_admin_user["role"])
        return result(handler.current_admin_user["role"])
    service = make_service(read)
    actor = SimpleNamespace(current_admin_user={"id": "one", "role": "super_admin"})
    assert service._load_automation_plugin_catalog(actor, module="finance", summary=True) == result("super_admin")
    actor.current_admin_user["role"] = "viewer"
    assert service._load_automation_plugin_catalog(actor, module="finance", summary=True) == result("viewer")
    assert calls == ["super_admin", "viewer"]
