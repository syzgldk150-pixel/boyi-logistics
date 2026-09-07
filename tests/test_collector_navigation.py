"""Missing evidence must not invent collector or source destinations."""
from shared.collector_navigation import collector_run_navigation, finance_failure_ownership
from tests.test_module_data_sources_mysql import database  # noqa: F401


def test_unknown_run_does_not_use_current_sources(database):
    fixture, name = database
    with fixture._connection(name) as connection:
        result = collector_run_navigation(connection, "missing-or-purged-run")
    assert result == {"status": "unverified", "message": "运行所属来源尚未核验。", "sources": []}


def test_legacy_notification_ownership_is_exact_not_a_display_name():
    steps, navigation = finance_failure_ownership(None, "unknown", [
        {"tool_name": "sync_finance_bills_fake", "name": "sync_finance_bills"}])
    assert not steps and navigation["status"] == "unverified"
    assert "module_url" not in navigation
    actual = {"tool_name": "sync_finance_bills", "arguments": {"_startup_catchup": True}}
    steps, _ = finance_failure_ownership(None, "unknown", [actual])
    assert steps == [actual]


def test_verified_finance_wrapper_preserves_host_startup_suppression(monkeypatch):
    monkeypatch.setattr("shared.collector_navigation.collector_run_navigation",
        lambda connection, run_id: {"status": "known", "module": "finance", "sources": []})
    actual = {"tool_name": "plugin__actual_instance", "arguments": {"_startup_catchup": True}}
    steps, _ = finance_failure_ownership(object(), "actual-run", [actual])
    assert steps == [actual]
