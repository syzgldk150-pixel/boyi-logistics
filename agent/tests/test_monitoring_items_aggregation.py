"""Pure monitoring aggregation checks; no external account or HTTP calls."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import Mock, patch

import pytest

from agent.tms_runtime import monitoring, monitoring_ronghui, monitoring_yunda


@pytest.fixture(autouse=True)
def clear_item_cache():
    monitoring._ITEM_CACHE.clear()
    monitoring._ITEM_LOCKS.clear()
    yield
    monitoring._ITEM_CACHE.clear()
    monitoring._ITEM_LOCKS.clear()


def item_page(**updates):
    return {
        "status": "ok", "items": [], "total": 0, "page": 1, "page_size": 20,
        "has_more": False, "message": "", **updates,
    }


@pytest.mark.parametrize("status,code", [("error", "MONITORING_SOURCE_FAILED"), ("auth_required", "AUTH_REQUIRED")])
def test_adapter_failure_is_not_converted_to_success_or_cached(status, code):
    broker = Mock()
    with (patch.object(monitoring, "get_session_broker", return_value=broker),
          patch.object(monitoring_yunda, "collect_items", return_value=item_page(status=status, total=None))):
        result = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications")
    assert result["ok"] is False
    assert result["error_code"] == code
    assert result["total"] is None
    assert monitoring._ITEM_CACHE == {}
    assert monitoring._ITEM_LOCKS == {}
    broker.build_requests_session.return_value.close.assert_called_once()


def test_unknown_quantity_is_not_zeroed_in_page_or_combined_total():
    with (patch.object(monitoring, "get_session_broker", return_value=Mock()),
          patch.object(monitoring_yunda, "collect_items", return_value=item_page(total=None))):
        page = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications")
    assert page["total"] is None
    totals = monitoring._build_totals([
        {"system": "yunda", "total_count": None, "exception_count": None},
        {"system": "ronghui", "total_count": 4, "exception_count": 2},
    ])
    assert totals == {"total_pending": None, "yunda_pending": None,
                      "ronghui_pending": 4, "exception_pending": None}


def test_overlapping_categories_are_not_added_to_supplier_total():
    categories = [
        {"category_id": "yunda:parent", "count": 8, "include_in_total": False},
        {"category_id": "yunda:child", "parent_category_id": "yunda:parent", "count": 3, "include_in_total": False},
        {"category_id": "yunda:work_orders", "count": 2, "include_in_total": False},
    ]
    with (patch.object(monitoring, "get_session_broker", return_value=Mock()),
          patch.object(monitoring_yunda, "collect_categories", return_value=categories)):
        result = monitoring._collect_yunda_snapshot(force=False, updated_at="synthetic-time")
    assert result["total_count"] is None
    assert [item["count"] for item in result["categories"]] == [8, 3, 2]


@pytest.mark.parametrize("system,category_id", [
    ("yunda", "yunda:unknown"), ("ronghui", "ronghui:unknown"),
    ("yunda", "ronghui:announcements"), ("other", "other:notifications"),
])
def test_unknown_category_is_rejected_before_session_or_adapter(system, category_id):
    with (patch.object(monitoring, "get_session_broker") as broker,
          patch.object(monitoring_yunda, "collect_items") as yunda,
          patch.object(monitoring_ronghui, "collect_items") as ronghui):
        result = monitoring.build_monitoring_items(system=system, category_id=category_id)
    assert result["ok"] is False
    assert result["total"] is None
    broker.assert_not_called()
    yunda.assert_not_called()
    ronghui.assert_not_called()


def test_detail_link_uses_server_category_target_and_ignores_browser_url():
    trusted = "https://ky-client.yunda56.com/#/server-owned-category"
    snapshot = {"systems": [{"categories": [{"category_id": "yunda:notifications", "title": "通知", "open_url": trusted}]}]}
    with patch.object(monitoring, "build_monitoring_snapshot", return_value=snapshot):
        link = monitoring.build_monitoring_detail_link({
            "system": "yunda", "category_id": "yunda:notifications",
            "url": "https://untrusted.test/", "open_url": "https://untrusted.test/",
            "resource_id": "other", "target_title": "Other", "account_id": "other",
        })
        rejected = monitoring.build_monitoring_detail_link({
            "system": "yunda", "category_id": "yunda:unknown", "url": trusted,
        })
    assert link["ok"] is True
    assert link["open_url"] == trusted
    assert link["mode"] == "external"
    assert rejected["ok"] is False
    assert "open_url" not in rejected


def test_forced_refresh_never_runs_the_capability_health_matrix():
    broker = Mock()
    with (patch.object(monitoring, "get_session_broker", return_value=broker),
          patch.object(monitoring_yunda, "collect_categories", return_value=[]),
          patch.object(monitoring_yunda, "collect_items", return_value=item_page())):
        monitoring._collect_system_snapshot("yunda", force=True, updated_at="synthetic-time")
        monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications", force=True)
    broker.validate_health_matrix.assert_not_called()
    assert all(call.kwargs == {"validate": False} for call in broker.build_requests_session.call_args_list)


def test_cache_is_scoped_to_server_profile_category_and_page():
    foreign_key = ("yunda", "other-profile", "yunda:notifications", 1)
    monitoring._ITEM_CACHE[foreign_key] = (time.time(), item_page(total=999))
    broker = Mock()
    with (patch.object(monitoring, "get_session_broker", return_value=broker) as resolve,
          patch.object(monitoring_yunda, "collect_items", side_effect=[item_page(total=1), item_page(total=2, page=2)]) as collect,
          patch.object(monitoring_ronghui, "collect_items", return_value=item_page(total=3))):
        first = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications")
        cached = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications")
        second = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications", page=2)
        ronghui = monitoring.build_monitoring_items(system="ronghui", category_id="ronghui:announcements")
    assert first["total"] == cached["total"] == 1
    assert cached["cached"] is True
    assert second["total"] == 2
    assert ronghui["total"] == 3
    assert collect.call_count == 2
    assert [call.args[0] for call in resolve.call_args_list] == ["yunda", "yunda", "default"]
    assert set(monitoring._ITEM_CACHE) == {
        foreign_key, ("yunda", "yunda", "yunda:notifications", 1),
        ("yunda", "yunda", "yunda:notifications", 2), ("ronghui", "default", "ronghui:announcements", 1),
    }


def test_forced_refresh_reports_failure_instead_of_reusing_previous_success():
    with (patch.object(monitoring, "get_session_broker", return_value=Mock()),
          patch.object(monitoring_yunda, "collect_items", side_effect=[
              item_page(total=2), item_page(status="error", total=None),
          ]) as collect):
        original = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications")
        failure = monitoring.build_monitoring_items(system="yunda", category_id="yunda:notifications", force=True)
    assert failure["ok"] is False
    assert failure["total"] is None
    assert original["total"] == 2
    assert collect.call_count == 2
    assert monitoring._ITEM_CACHE == {}


def test_concurrent_reads_of_same_page_share_one_collection():
    collection_started, release_collection, waiter_blocked = Event(), Event(), Event()

    class ObservedLock:
        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            if self.lock.locked():
                waiter_blocked.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    def collect_page(*_args):
        collection_started.set()
        assert release_collection.wait(timeout=5)
        return item_page(total=7)

    key = ("yunda", "yunda", "yunda:notifications", 1)
    monitoring._ITEM_LOCKS[key] = (ObservedLock(), 0)
    with (patch.object(monitoring, "get_session_broker", return_value=Mock()),
          patch.object(monitoring_yunda, "collect_items", side_effect=collect_page) as collect,
          ThreadPoolExecutor(max_workers=2) as executor):
        first = executor.submit(monitoring.build_monitoring_items, system="yunda",
                                category_id="yunda:notifications", force=True)
        try:
            assert collection_started.wait(timeout=5)
            second = executor.submit(monitoring.build_monitoring_items, system="yunda",
                                     category_id="yunda:notifications", force=True)
            assert waiter_blocked.wait(timeout=5)
        finally:
            release_collection.set()
        first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    assert first_result["ok"] is second_result["ok"] is True
    assert first_result["total"] == second_result["total"] == 7
    assert second_result["cached"] is True
    collect.assert_called_once()
    assert monitoring._ITEM_LOCKS == {}
