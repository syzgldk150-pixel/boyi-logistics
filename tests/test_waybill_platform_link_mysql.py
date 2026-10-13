"""Real MySQL platform-link transactions and migration 059, confined to loopback fixtures."""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from console.database import ManualWaybillEditError
from tests.test_domain_database_split_mysql import database as split_database, load  # noqa: F401
from tests.test_manual_waybill_edit_mysql import logs, seed
from tests.test_waybill_source_coverage_mysql import database, manual_repository  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def require_loopback():
    if os.environ.get("AGENT_DB_HOST") != "127.0.0.1" or os.environ.get("AGENT_DB_PORT") != "33330":
        pytest.fail("platform links require the isolated loopback MySQL on port 33330")


@pytest.fixture
def repository(manual_repository):
    repo = manual_repository
    with repo.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS name")
        name = cursor.fetchone()["name"]
        # The shared fixture already applied 059 to its local boyi_waybills.
        sql = (ROOT / "agent/migrations/057_boyi_waybill_edit_logs.sql").read_text(encoding="utf-8")
        cursor.execute(sql.replace("{{waybill_db}}", f"`{name}`"))
    return repo


def link(repo, row, platform="yunda", number="4321098765", **kwargs):
    return repo.link_manual_waybill(
        row["id"], platform, number,
        expected_platform=kwargs.pop("expected_platform", ""), expected_waybill_no=kwargs.pop("expected_waybill_no", ""),
        request_id=kwargs.pop("request_id", str(uuid4())), request_sha256=kwargs.pop("request_sha256", "a" * 64),
        actor_id="7", actor_name="测试管理员",
    )


def test_link_and_history_are_committed_together_and_listed(repository):
    row = seed(repository)
    result = link(repository, row)
    assert result == {"outcome": "SAVED", "changes": [
        {"field": "linked_waybill", "label": "绑定平台运单", "before": "", "after": "韵达 4321098765"},
    ]}
    stored = repository.get_waybill(row["id"], source="manual")
    assert (stored["linked_platform"], stored["linked_waybill_no"]) == ("yunda", "4321098765")
    assert stored["freight_fee"] == row["freight_fee"] and stored["remark"] == row["remark"]
    assert json.loads(logs(repository)[0]["changes_json"]) == result["changes"]

    listed = repository.search_waybills({"q": row["waybill_no"], "source": "all"})["rows"]
    assert [item["linked_text"] for item in listed] == ["韵达 4321098765"]
    assert listed[0]["tracking_url"] == "/tracking?tracking_number=4321098765&provider=yunda"


def test_change_and_clear_use_the_link_the_operator_saw(repository):
    row = seed(repository)
    link(repository, row)
    with pytest.raises(ManualWaybillEditError) as error:
        link(repository, row, "best", "BEST0001")
    assert error.value.code == "STALE"
    changed = link(repository, row, "best", "BEST0001", expected_platform="yunda", expected_waybill_no="4321098765")
    assert changed["changes"][0]["before"] == "韵达 4321098765" and changed["changes"][0]["after"] == "百世 BEST0001"
    cleared = link(repository, row, "", "", expected_platform="best", expected_waybill_no="BEST0001")
    assert cleared["changes"][0]["after"] == ""
    stored = repository.get_waybill(row["id"], source="manual")
    assert stored["linked_platform"] is None and stored["linked_waybill_no"] is None
    assert len(logs(repository)) == 3
    assert link(repository, row, "", "")["outcome"] == "UNCHANGED"
    assert len(logs(repository)) == 3


def test_replay_and_cancelled_waybill(repository):
    row = seed(repository)
    request_id = str(uuid4())
    first = link(repository, row, request_id=request_id)
    assert link(repository, row, request_id=request_id) == {"outcome": "REPLAYED", "changes": first["changes"]}
    with pytest.raises(ManualWaybillEditError) as error:
        link(repository, row, request_id=request_id, request_sha256="b" * 64)
    assert error.value.code == "REQUEST_REUSED"

    cancelled = seed(repository)
    with repository.connect() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE boyi_waybills SET status='cancelled' WHERE id=%s", (cancelled["id"],))
    with pytest.raises(ManualWaybillEditError) as error:
        link(repository, cancelled)
    assert error.value.code == "CANCELLED"
    assert len(logs(repository)) == 1


def test_migration_059_adds_columns_to_entity_table_and_runtime_view(split_database):
    connection, _connect, migration, apply = split_database
    apply()
    runner = load("run_migrations")
    with connection.cursor() as cursor:
        migration.apply_later_migration(
            cursor, (ROOT / "agent/migrations/059_boyi_waybill_platform_link.sql").read_text(encoding="utf-8"),
            runner.split_sql_statements)
        runtime, waybill, _ = migration.names(cursor)
        for schema in (waybill, runtime):
            assert {"linked_platform", "linked_waybill_no"} <= set(migration.columns(cursor, schema, "boyi_waybills"))
        assert "linked_platform" not in migration.columns(cursor, runtime, "waybills")
