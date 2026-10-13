"""Real MySQL edit transactions and migration 057, confined to loopback fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from uuid import uuid4
from unittest.mock import Mock

import pymysql
import pytest

from console.database import MANUAL_WAYBILL_EDIT_FIELDS, ManualWaybillEditError, manual_waybill_fingerprint
from console.routes import waybills
from console.tests.test_manual_waybill_edit import JsonHandler, build_app, edit_fields
from tests.test_domain_database_split_mysql import database as split_database, load  # noqa: F401
from tests.test_waybill_source_coverage_mysql import database, manual_repository  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def require_loopback():
    if os.environ.get("AGENT_DB_HOST") != "127.0.0.1" or os.environ.get("AGENT_DB_PORT") != "33330":
        pytest.fail("manual edits require the isolated loopback MySQL on port 33330")


@pytest.fixture
def repository(manual_repository):
    repo = manual_repository
    with repo.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS name")
        name = cursor.fetchone()["name"]
        sql = (ROOT / "agent/migrations/057_boyi_waybill_edit_logs.sql").read_text(encoding="utf-8")
        cursor.execute(sql.replace("{{waybill_db}}", f"`{name}`"))
    return repo


def seed(repo, **overrides):
    fields = {field: "" for field in MANUAL_WAYBILL_EDIT_FIELDS}
    fields.update(open_date="2026-10-13", freight_fee="20.00", payment_method="寄付", receipt_required="0",
                  receiver_name="测试收件人", sender_address="测试发件地址", weight_volume="10kg / 0.1m³")
    fields.update(overrides)
    identity, _ = repo.create_manual_waybill(fields)
    return repo.get_waybill(identity, source="manual")


def fields_from(row, **changes):
    fields = {field: "" if row[field] is None else str(row[field]) for field in MANUAL_WAYBILL_EDIT_FIELDS}
    fields.update(changes)
    return fields


def save(repo, row, fields=None, **kwargs):
    return repo.update_manual_waybill(row["id"], fields or fields_from(row, remark="修改备注"),
                                      expected_fingerprint=kwargs.pop("expected_fingerprint", manual_waybill_fingerprint(row)),
                                      request_id=kwargs.pop("request_id", str(uuid4())),
                                      request_sha256=kwargs.pop("request_sha256", "a" * 64),
                                      actor_id="7", actor_name=kwargs.pop("actor_name", "测试管理员"), **kwargs)


def logs(repo):
    with repo.connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM boyi_waybill_edit_logs ORDER BY id")
        return list(cursor.fetchall())


def test_changed_columns_and_history_are_committed_together(repository):
    row = seed(repository)
    result = save(repository, row, fields_from(row, freight_fee="25.00", receipt_required="1"))
    assert result == {"outcome": "SAVED", "changes": [
        {"field": "freight_fee", "label": "运费", "before": "20.00", "after": "25.00"},
        {"field": "receipt_required", "label": "需要回单", "before": "0", "after": "1"},
    ]}
    updated = repository.get_waybill(row["id"], source="manual")
    assert updated["freight_fee"] == "25.00" and updated["receipt_required"] == 1
    assert updated["sender_address"] == row["sender_address"]
    assert updated["waybill_no"] == row["waybill_no"] and updated["source"] == "manual"
    recorded = logs(repository)
    assert len(recorded) == 1 and recorded[0]["actor_id"] == "7"
    assert json.loads(recorded[0]["changes_json"]) == result["changes"]
    history = repository.list_manual_waybill_edits(row["id"])
    assert history[0]["actor"] == "测试管理员" and history[0]["changes"] == result["changes"]


def test_same_content_and_date_format_do_not_write_history(repository):
    row = seed(repository)
    result = save(repository, row, fields_from(row, open_date="2026/10/13"))
    assert result == {"outcome": "UNCHANGED", "changes": []}
    assert repository.get_waybill(row["id"], source="manual")["updated_at"] == row["updated_at"]
    assert logs(repository) == []


def test_stale_fingerprint_cannot_overwrite_a_newer_save(repository):
    row = seed(repository)
    save(repository, row)
    with pytest.raises(ManualWaybillEditError) as error:
        save(repository, row, fields_from(row, remark="旧页面备注"))
    assert error.value.code == "STALE"
    assert repository.get_waybill(row["id"], source="manual")["remark"] == "修改备注"
    assert len(logs(repository)) == 1


@pytest.mark.parametrize("source,status,code", [("manual", "cancelled", "CANCELLED"), ("ocr", "in_transit", "NOT_FOUND")])
def test_cancelled_or_nonmanual_stored_row_cannot_be_edited(repository, source, status, code):
    row = seed(repository)
    with repository.connect() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE boyi_waybills SET source=%s,status=%s WHERE id=%s", (source, status, row["id"]))
    with pytest.raises(ManualWaybillEditError) as error:
        save(repository, row)
    assert error.value.code == code
    assert logs(repository) == []


def test_replay_returns_original_changes_and_rejects_changed_payload(repository):
    row = seed(repository)
    request_id = str(uuid4())
    first = save(repository, row, request_id=request_id)
    replay = save(repository, row, request_id=request_id)
    assert replay == {"outcome": "REPLAYED", "changes": first["changes"]}
    with pytest.raises(ManualWaybillEditError) as error:
        save(repository, row, request_id=request_id, request_sha256="b" * 64)
    assert error.value.code == "REQUEST_REUSED"
    assert len(logs(repository)) == 1


def test_concurrent_same_request_waits_for_row_then_replays_committed_result(repository):
    row = seed(repository)
    original_connect = repository.connect
    entered = threading.Event()
    guard = threading.Lock()
    count = 0

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, sql, values):
            nonlocal count
            if sql.startswith("SELECT * FROM boyi_waybills") and "FOR UPDATE" in sql:
                with guard:
                    count += 1
                    if count == 2:
                        entered.set()
            return self.cursor.execute(sql, values)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def cursor(self):
            return Cursor(self.connection.cursor())

    @contextmanager
    def observed_connect():
        with original_connect() as connection:
            yield Connection(connection)

    repository.connect = observed_connect
    request_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        with original_connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM boyi_waybills WHERE id=%s FOR UPDATE", (row["id"],))
            first = pool.submit(save, repository, row, request_id=request_id)
            second = pool.submit(save, repository, row, request_id=request_id)
            assert entered.wait(5)
        results = [first.result(timeout=5), second.result(timeout=5)]
    repository.connect = original_connect
    assert sorted(item["outcome"] for item in results) == ["REPLAYED", "SAVED"]
    assert results[0]["changes"] == results[1]["changes"] and len(logs(repository)) == 1


def test_history_insert_failure_rolls_back_waybill_update(repository):
    row = seed(repository)
    with repository.connect() as connection, connection.cursor() as cursor:
        cursor.execute("CREATE TRIGGER fixture_reject_edit BEFORE INSERT ON boyi_waybill_edit_logs "
                       "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='fixture rejects history'")
    with pytest.raises(pymysql.MySQLError):
        save(repository, row)
    assert repository.get_waybill(row["id"], source="manual")["remark"] == row["remark"]
    assert logs(repository) == []


def test_unparsed_weight_is_not_silently_erased_by_an_edit(repository):
    row = seed(repository, weight_volume="历史重量文本")
    with pytest.raises(ManualWaybillEditError) as error:
        save(repository, row, fields_from(row, weight_volume=""))
    assert error.value.code == "WEIGHT_VOLUME_UNPARSED"
    assert logs(repository) == []
    assert repository.get_waybill(row["id"], source="manual")["weight_volume"] == "历史重量文本"
    assert save(repository, row, fields_from(row, weight_volume="10kg"))["outcome"] == "SAVED"


def test_migration_057_projects_entity_table_and_console_select_insert_permissions(split_database):
    connection, connect, migration, apply = split_database
    apply()
    runner = load("run_migrations")
    migration_path = ROOT / "agent/migrations/057_boyi_waybill_edit_logs.sql"
    username = "edit_fixture_" + uuid4().hex[:12]
    with connection.cursor() as cursor:
        migration.apply_later_migration(cursor, migration_path.read_text(encoding="utf-8"), runner.split_sql_statements)
        runtime, waybill, _ = migration.names(cursor)
        assert migration.table_type(cursor, waybill, "boyi_waybill_edit_logs") == "BASE TABLE"
        assert migration.table_type(cursor, runtime, "boyi_waybill_edit_logs") == "VIEW"
        cursor.execute(f"CREATE USER '{username}'@'127.0.0.1'")
        for schema in (runtime, waybill):
            cursor.execute(f"GRANT SELECT, INSERT ON `{schema}`.boyi_waybill_edit_logs TO '{username}'@'127.0.0.1'")
    try:
        with pymysql.connect(host="127.0.0.1", port=33330, user=username, password="", database=runtime,
                             cursorclass=pymysql.cursors.DictCursor, autocommit=True) as console:
            with console.cursor() as cursor:
                cursor.execute("INSERT INTO boyi_waybill_edit_logs(waybill_id,waybill_no,request_id,request_sha256,actor_id,changes_json) "
                               "VALUES(1,'BY00001',%s,%s,'7','[]')", (str(uuid4()), "a" * 64))
                cursor.execute("SELECT COUNT(*) AS n FROM boyi_waybill_edit_logs")
                assert cursor.fetchone()["n"] == 1
        with connect() as raw, raw.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS n FROM `{waybill}`.boyi_waybill_edit_logs")
            assert cursor.fetchone()["n"] == 1
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP USER '{username}'@'127.0.0.1'")


def test_http_replay_survives_changed_validator_and_keeps_origin_checks(repository):
    app = build_app()
    app.repository = app.service.repository = repository
    fields, error = app.service._normalized_manual_waybill_fields({
        **edit_fields(), "field_weight_volume": "10kg / 0.1m³",
    })
    assert not error
    waybill_id, _ = repository.create_manual_waybill(fields)
    row = repository.get_waybill(waybill_id, source="manual")
    body = {"request_id": str(uuid4()), "fingerprint": manual_waybill_fingerprint(row),
            "fields": edit_fields(field_remark="HTTP 修改备注")}
    path = f"/waybills/{waybill_id}/manual-update"
    first = JsonHandler(body)
    assert waybills.handle_post(app, first, path, "", {})
    assert first.status == 200 and first.result()["outcome"] == "SAVED"
    app._validate_active_waybill_entry_extensions.assert_called_once()

    # A lost successful response must remain replayable even while Agent is down.
    app._validate_active_waybill_entry_extensions = Mock(side_effect=RuntimeError("validator unavailable"))
    replay = JsonHandler(body)
    assert waybills.handle_post(app, replay, path, "", {})
    assert replay.status == 200 and replay.result()["outcome"] == "REPLAYED"
    assert replay.result()["changes"] == first.result()["changes"]

    changed = JsonHandler({**body, "fields": edit_fields(field_remark="不同的修改")})
    assert waybills.handle_post(app, changed, path, "", {})
    assert changed.status == 409 and changed.result()["error_code"] == "REQUEST_REUSED"
    forbidden = JsonHandler(body, origin="http://other.test")
    assert waybills.handle_post(app, forbidden, path, "", {})
    assert forbidden.status == 403
    legacy = JsonHandler(body, user={"id": 7, "username": "fixture", "is_legacy_basic_auth": True})
    assert waybills.handle_post(app, legacy, path, "", {})
    assert legacy.status == 403
    app._validate_active_waybill_entry_extensions.assert_not_called()
    assert len(logs(repository)) == 1
    assert repository.get_waybill(waybill_id, source="manual")["remark"] == "HTTP 修改备注"

    # A fresh edit is still subject to the current validators and rolls back.
    row = repository.get_waybill(waybill_id, source="manual")
    app._validate_active_waybill_entry_extensions = Mock(return_value=(False, "当前扩展拒绝修改"))
    fresh = JsonHandler({"request_id": str(uuid4()), "fingerprint": manual_waybill_fingerprint(row),
                         "fields": edit_fields(field_remark="新请求备注")})
    assert waybills.handle_post(app, fresh, path, "", {})
    assert fresh.status == 422 and fresh.result()["error_code"] == "WAYBILL_ENTRY_VALIDATION_FAILED"
    app._validate_active_waybill_entry_extensions.assert_called_once()
    assert len(logs(repository)) == 1
    assert repository.get_waybill(waybill_id, source="manual")["remark"] == "HTTP 修改备注"
