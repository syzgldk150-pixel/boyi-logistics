"""Missing evidence must not invent collector or source destinations."""
from shared.collector_navigation import collector_invocation_navigation
from tests.test_module_data_sources_mysql import database  # noqa: F401


def test_unknown_invocation_does_not_use_current_sources(database):
    fixture, name = database
    with fixture._connection(name) as connection:
        result = collector_invocation_navigation(connection, "00000000-0000-4000-8000-000000000000")
    assert result == {"status": "unverified", "message": "运行所属来源尚未核验。", "sources": []}
