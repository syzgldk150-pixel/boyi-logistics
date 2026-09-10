"""Isolated MySQL checks for source collision, partial refresh and publication."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
import pymysql

from shared.runtime_repositories import WaybillRepository
from shared.waybill_source_coverage import WaybillSourceRepository, WaybillSourceScope

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")
ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 8)
SCOPE = WaybillSourceScope("ronghui", "fixture-site/send", "fixture-site/all", "account-a")
CAPTURED = datetime(2026, 9, 9, tzinfo=timezone.utc)


@pytest.fixture
def database():
    requested = os.environ.get("AGENT_DB_NAME", "")
    if not re.fullmatch(r"[A-Za-z0-9_]+_test", requested):
        pytest.fail("waybill coverage requires an explicitly test-scoped database environment")
    name = "waybill_coverage_" + uuid4().hex + "_test"
    kwargs = dict(host=os.environ["AGENT_DB_HOST"], port=int(os.environ["AGENT_DB_PORT"]),
                  user=os.environ["AGENT_DB_USER"], password=os.environ.get("AGENT_DB_PASS", ""),
                  cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4", autocommit=False)
    with pymysql.connect(**kwargs) as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    kwargs["database"] = name
    with pymysql.connect(**kwargs) as connection, connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS waybill_source_coverage")
        cursor.execute("DROP TABLE IF EXISTS waybills")
        initial = (ROOT / "agent/migrations/001_shared_runtime_tables.sql").read_text()
        ddl = initial[initial.index("CREATE TABLE IF NOT EXISTS waybills ("):].split(";", 1)[0]
        cursor.execute(ddl)
        migration = (ROOT / "agent/migrations/045_waybill_source_coverage.sql").read_text()
        for sql in migration.split(";"):
            if sql.strip():
                cursor.execute(sql)
        connection.commit()

    @contextmanager
    def connect():
        connection = pymysql.connect(**kwargs)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    try:
        yield connect
    finally:
        server_options = {key: value for key, value in kwargs.items() if key != "database"}
        with pymysql.connect(**server_options) as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{name}`")


def record(number: str, *, piece: str = "1"):
    return {"waybill_no": number, "source_record_id": number, "open_date": DAY.isoformat(), "quantity_lines": piece}


def rows(connect):
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM waybills ORDER BY id")
        return cursor.fetchall()


def test_partial_refresh_preserves_other_rows_and_never_claims_complete_day(database):
    repository = WaybillSourceRepository(database)
    repository.publish([record("one"), record("two")], scope=SCOPE, business_date=DAY,
                       captured_at=CAPTURED, complete=True, expected_total=2)
    before = repository.coverage(SCOPE, DAY)
    result = repository.publish([record("one", piece="5")], scope=SCOPE, business_date=DAY,
                       captured_at=CAPTURED, complete=False)
    actual = rows(database)
    assert len(actual) == 2 and actual[0]["quantity_lines"] == "5"
    assert result["deleted_stale"] == 0
    after = repository.coverage(SCOPE, DAY)
    assert after["complete_through"] is None and not after["final_day"]
    assert before["complete_through"] is not None
    assert after["record_count"] == before["record_count"]


def test_account_rebinding_reuses_entity_but_different_sources_never_merge(database):
    repository = WaybillSourceRepository(database)
    for scope in [SCOPE, replace(SCOPE, account_id="account-b"), replace(SCOPE, source_scope="other-site/send")]:
        repository.publish([record("same-number")], scope=scope, business_date=DAY,
                           captured_at=CAPTURED, complete=False)
    actual = rows(database)
    assert len(actual) == 2
    assert actual[0]["source_account_id"] == "account-b"
    assert actual[1]["source_scope"] == "other-site/send"


def test_unreviewed_scope_cannot_guess_overlapping_legacy_identity(database):
    WaybillRepository(database).sync_records([record("same-number")], source="ronghui")
    repository = WaybillSourceRepository(database)
    before = rows(database)
    with pytest.raises(ValueError, match="WAYBILL_LEGACY_SOURCE_UNVERIFIED"):
        repository.publish([record("same-number")], scope=SCOPE, business_date=DAY,
                           captured_at=CAPTURED, complete=True, expected_total=1)
    assert rows(database) == before
    assert repository.coverage(SCOPE, DAY) is None
    # Absence from a scoped result does not prove the legacy row's identity.
    repository.publish([], scope=SCOPE, business_date=DAY,
                       captured_at=CAPTURED, complete=True, expected_total=0)
    actual = rows(database)
    assert len(actual) == 1 and actual[0]["source_scope"] is None


@pytest.mark.parametrize("provider", ["ronghui", "yunda"])
@pytest.mark.parametrize("status", ["in_transit", "cancelled"])
@pytest.mark.parametrize("complete", [True, False])
def test_fresh_native_identity_reconciles_legacy_without_duplicate_totals(database, provider, status, complete):
    from console.database import DocumentRepository

    legacy = WaybillRepository(database)
    original = {**record("native-one"), "status": status, "freight_fee": "12.50", "transfer_fee": "12.50",
                "receiver_name": "original recipient"}
    legacy.sync_records([original], source=provider)
    before = rows(database)[0]
    scope = replace(SCOPE, source=provider, source_scope=f"{provider}:native-waybill")
    repository = WaybillSourceRepository(database)
    console = DocumentRepository.__new__(DocumentRepository)
    console.connect = database
    console.placeholder = "%s"
    fresh = {**original, "status": "signed", "freight_fee": "18.25", "transfer_fee": "18.25", "quantity_lines": "5",
             "receiver_name": "corrected recipient"}
    for _ in range(2):
        result = repository.publish([fresh], scope=scope, business_date=DAY,
            captured_at=CAPTURED, complete=complete, expected_total=1 if complete else None)
        assert result["updates"] == 1 and result["creates"] == 0
        current = legacy.get_by_number("native-one", source=provider)
        assert current["id"] == before["id"] and current["created_at"] == before["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        assert current["status"] == ("cancelled" if status == "cancelled" else "signed")
        assert current["receiver_name"] == "corrected recipient" and current["quantity_lines"] == "5"
        assert current["source_scope"] == scope.source_scope and current["source_account_id"] == scope.account_id
        assert current["source_permission_scope"] == (scope.permission_scope if complete else None)
        cached = repository.read_cached(source=provider, date_from=DAY, date_to=DAY)
        assert cached["total"] == len(cached["rows"]) == 1
        page = console.search_waybills({"source": provider, "date_from": DAY.isoformat(), "date_to": DAY.isoformat()})
        assert page["pagination"]["total"] == page["summary"]["total"] == len(page["rows"]) == 1
        assert page["summary"]["fee_total"] == "18.25"


@pytest.mark.parametrize("conflict", ["duplicate_legacy", "scoped_collision", "partial_identity", "manual_origin"])
def test_legacy_conflict_fails_before_publishing_any_rows(database, conflict):
    legacy = WaybillRepository(database)
    legacy.sync_records([record("a-safe"), record("z-conflict")], source="ronghui")
    with database() as connection, connection.cursor() as cursor:
        if conflict in {"duplicate_legacy", "scoped_collision"}:
            cursor.execute("SELECT * FROM waybills WHERE waybill_no=%s", ("z-conflict",))
            values = cursor.fetchone()
            values.pop("id")
            if conflict == "scoped_collision":
                values.update(source_scope="other/verified", source_record_id="z-conflict")
            cursor.execute(f"INSERT INTO waybills ({','.join(values)}) VALUES ({','.join('%s' for _ in values)})",
                           list(values.values()))
        elif conflict == "partial_identity":
            cursor.execute("UPDATE waybills SET source_account_id=%s WHERE waybill_no=%s", ("unknown-binding", "z-conflict"))
        else:
            cursor.execute("UPDATE waybills SET writer_id=%s WHERE waybill_no=%s", ("manual-writer", "z-conflict"))
    before = rows(database)
    scope = replace(SCOPE, source_scope="ronghui:native-waybill")
    repository = WaybillSourceRepository(database)
    with pytest.raises(ValueError, match="WAYBILL_LEGACY_"):
        repository.publish([record("a-safe", piece="9"), record("z-conflict")], scope=scope,
            business_date=DAY, captured_at=CAPTURED, complete=True, expected_total=2)
    assert rows(database) == before and repository.coverage(scope, DAY) is None


def test_native_reconciliation_leaves_other_platforms_and_scoped_entities_untouched(database):
    legacy = WaybillRepository(database)
    legacy.sync_records([record("same-number")], source="ronghui")
    legacy.sync_records([record("same-number", piece="8")], source="yunda")
    repository = WaybillSourceRepository(database)
    repository.publish([record("other-number", piece="4")], scope=SCOPE, business_date=DAY,
        captured_at=CAPTURED, complete=True, expected_total=1)
    before = [row for row in rows(database) if row["source"] == "yunda" or row["source_scope"] == SCOPE.source_scope]
    native = replace(SCOPE, source_scope="ronghui:native-waybill")
    repository.publish([record("same-number", piece="6")], scope=native, business_date=DAY,
        captured_at=CAPTURED, complete=True, expected_total=1)
    after = rows(database)
    assert len(after) == 3
    assert [row for row in after if row["source"] == "yunda" or row["source_scope"] == SCOPE.source_scope] == before
    assert legacy.get_by_number("same-number", source="ronghui")["quantity_lines"] == "6"


@pytest.mark.parametrize("commit_ack_lost", [False, True])
def test_verified_nightly_publication_reports_legacy_adoption_as_update(database, monkeypatch, commit_ack_lost):
    from plugin_core_adapters.waybill_query import publish_verified_waybills

    legacy = WaybillRepository(database)
    legacy.sync_records([{**record("native-one"), "status": "cancelled"}], source="ronghui")
    original_id = rows(database)[0]["id"]
    scope = replace(SCOPE, source_scope="ronghui:native-waybill")
    if commit_ack_lost:
        publish = WaybillSourceRepository.publish
        def lose_ack(self, *args, **kwargs):
            publish(self, *args, **kwargs)
            raise ConnectionError("isolated commit acknowledgement lost")
        monkeypatch.setattr(WaybillSourceRepository, "publish", lose_ack)
    result = publish_verified_waybills([record("native-one", piece="3")], source="ronghui",
        target_date=DAY, account_id=scope.account_id, scope_resolver=lambda *_args: scope,
        connection_factory=database)
    assert result["verified"] is True and result["updates"] == 1 and result["creates"] == 0
    assert result["deleted_stale"] == 0 and result["readback_count"] == 1
    current = rows(database)
    assert len(current) == 1 and current[0]["id"] == original_id
    assert current[0]["status"] == "cancelled" and current[0]["quantity_lines"] == "3"


def test_wrong_total_and_older_snapshot_leave_prior_publication_unchanged(database):
    repository = WaybillSourceRepository(database)
    repository.publish([record("one")], scope=SCOPE, business_date=DAY,
                       captured_at=CAPTURED, complete=True, expected_total=1)
    before = rows(database)
    with pytest.raises(ValueError, match="authoritative total"):
        repository.publish([], scope=SCOPE, business_date=DAY,
                           captured_at=CAPTURED, complete=True, expected_total=1)
    with pytest.raises(ValueError, match="older source snapshot"):
        repository.publish([], scope=SCOPE, business_date=DAY,
                           captured_at=datetime(2026, 9, 8, tzinfo=timezone.utc), complete=True, expected_total=0)
    assert rows(database) == before


def test_2355_snapshot_remains_open_until_next_day_collection(database):
    repository = WaybillSourceRepository(database)
    result = repository.publish([record("one")], scope=SCOPE, business_date=DAY,
        captured_at=datetime(2026, 9, 8, 15, 55, tzinfo=timezone.utc), complete=True, expected_total=1)
    assert result["final_day"] is False
    repository.publish([record("one"), record("late-order")], scope=SCOPE, business_date=DAY,
                       captured_at=CAPTURED, complete=True, expected_total=2)
    assert repository.coverage(SCOPE, DAY)["final_day"] == 1
    assert len(rows(database)) == 2


def test_legacy_sync_does_not_overwrite_same_number_from_other_platform(database):
    repository = WaybillRepository(database)
    repository.sync_records([record("same-number")], source="ronghui")
    repository.sync_records([record("same-number", piece="5")], source="yunda")
    assert len(rows(database)) == 2
    with pytest.raises(ValueError, match="ambiguous"):
        repository.get_by_number("same-number")


def test_narrower_permission_snapshot_cannot_delete_other_permission_rows(database):
    repository = WaybillSourceRepository(database)
    repository.publish([record("one"), record("two")], scope=SCOPE, business_date=DAY,
                       captured_at=CAPTURED, complete=True, expected_total=2)
    narrower = replace(SCOPE, permission_scope="fixture-site/limited")
    repository.publish([record("one")], scope=narrower, business_date=DAY,
                       captured_at=CAPTURED, complete=True, expected_total=1)
    assert len(rows(database)) == 2


def test_real_provider_paging_normalization_database_and_cached_query_chain(database, monkeypatch):
    from agent.send_waybills_business import build_direct_waybill_query_service
    from plugin_core_adapters.waybill_query import build_reviewed_provider_source
    from plugin_core_adapters.waybill_query_scope import observe_query_scope
    from agent.tms_runtime.scripts import Send_order
    from types import SimpleNamespace
    source_rows = [{"BILL_CODE": f"R-fixture-{index:03d}", "REGISTER_DATE": "2026/09/08 12:00:00",
                    "PIECE_NUMBER": 1, "GUEST_FREIGHT": "12.50"} for index in range(101)]
    requests = []
    fail_second_page = False
    class Session:
        def get(self, url, **kwargs):
            assert url == "https://tms.ronghuiwl.com/module/index?mv=index"
            return SimpleNamespace(status_code=200, text='<input id="loginSiteCode" value="fixture-site">')
        def post(self, url, *, params, data, **kwargs):
            assert url == Send_order.DATA_QUERY_URL and params == {"id": "FIND_BILL_SEND"}
            page, size = int(data["pageIndex"]), int(data["pageSize"])
            requests.append(page)
            if fail_second_page and page == 1:
                raise TimeoutError("fixture upstream page unavailable")
            payload = {"total": len(source_rows), "data": source_rows[page*size:(page+1)*size]}
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    class Auth:
        def __init__(self, *, profile):
            assert profile == "fixture-profile"
        def login_and_get_session(self):
            return Session()
    monkeypatch.setattr(Send_order, "TMSAuth", Auth)
    observe = lambda: observe_query_scope("ronghui", SCOPE.account_id, Session())
    binding = build_reviewed_provider_source(scope=observe(),
        descriptor={"account_id": SCOPE.account_id, "system": "ronghui", "session_profile": "fixture-profile"},
        verify_scope=observe)
    service = build_direct_waybill_query_service(connection_factory=database, source_resolver=lambda _: binding)
    first = service.query({"source": "ronghui", "date_from": DAY.isoformat()})
    assert first["complete"] is True and first["data"]["total"] == 101 and requests == [0, 1]
    second = service.query({"source": "ronghui", "date_from": DAY.isoformat()})
    assert second["publications"][0]["from_database"] is True and requests == [0, 1]
    before = rows(database)
    fail_second_page = True
    partial = service.query({"source": "ronghui", "date_from": DAY.isoformat(), "force_refresh": True})
    assert partial["complete"] is False and partial["data"]["total"] == 101
    assert rows(database) == before


def test_actual_nightly_projection_uses_verified_scope_and_rebinds_account(database, monkeypatch):
    from plugin_core_adapters import waybill_query
    from plugin_core_adapters.daily_send import build_production_daily_send_ports
    from plugin_core_adapters.first_party import _replace_yunda_waybill_projection
    monkeypatch.setattr(waybill_query, "waybill_connection", database)
    monkeypatch.setattr(waybill_query, "resolve_source_scope", lambda source, account_id:
        WaybillSourceScope(source, "fixture/site", "fixture/all", account_id))
    ports = build_production_daily_send_ports(account_manager=object())
    first = ports.projection_replace([record("night-one"), record("night-two")], DAY.isoformat(), {"account_id": "a"})
    assert first["verified"] and first["creates"] == 2
    second = ports.projection_replace([record("night-one", piece="3")], DAY.isoformat(), {"account_id": "b"})
    assert second["updates"] == 1 and second["creates"] == 0 and second["deleted_stale"] == 1
    assert len(rows(database)) == 1 and rows(database)[0]["source_account_id"] == "b"
    yunda = _replace_yunda_waybill_projection(
        [{"5.14编号": "night-one", "日期": DAY.isoformat(), "件数": "7"}], DAY.isoformat(), {"account_id": "c"})
    assert yunda["verified"] and yunda["creates"] == 1
    assert len(rows(database)) == 2
    def unavailable(*_args):
        raise ValueError("WAYBILL_SOURCE_SCOPE_UNVERIFIED")
    monkeypatch.setattr(waybill_query, "resolve_source_scope", unavailable)
    partial = waybill_query.publish_collected_waybills([record("unsafe")], source="ronghui",
        target_date=DAY, account_id="a", complete=True)
    assert partial["partial"] and partial["upserted"] == 0 and len(rows(database)) == 2


def test_ronghui_exact_native_detail_upserts_only_returned_identity(database, monkeypatch):
    from plugin_core_adapters.waybill_query import build_reviewed_provider_source
    from agent.send_waybills_business import build_direct_waybill_query_service
    from agent.tms_runtime.scripts import Send_order, query_waybill_detail
    from types import SimpleNamespace
    calls = []
    raw = {"BILL_CODE": "exact-one", "REGISTER_DATE": DAY.isoformat(), "PIECE_NUMBER": 4}
    class Session:
        def post(self, url, *, data, **kwargs):
            assert url == query_waybill_detail.DETAIL_URL
            assert data == {"billCode": "exact-one", "isView": "true"}
            calls.append(url)
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {"result": {"data": [dict(raw)]}})
    monkeypatch.setattr(Send_order, "TMSAuth", lambda **kwargs:
        SimpleNamespace(login_and_get_session=lambda: Session()))
    repository = WaybillSourceRepository(database)
    repository.publish([record("untouched")], scope=SCOPE, business_date=DAY,
        captured_at=CAPTURED, complete=True, expected_total=1)
    binding = build_reviewed_provider_source(scope=SCOPE,
        descriptor={"account_id": SCOPE.account_id, "system": "ronghui", "session_profile": "fixture"},
        verify_scope=lambda: SCOPE)
    service = build_direct_waybill_query_service(connection_factory=database, source_resolver=lambda _: binding)
    result = service.query({"source": "ronghui", "waybill_no": "exact-one"})
    assert result["ok"] and result["data"]["total"] == 1
    assert len(rows(database)) == 2 and len(calls) == 1
    assert not repository.coverage(SCOPE, DAY)["final_day"]
    raw.pop("REGISTER_DATE")
    failed = service.query({"source": "ronghui", "waybill_no": "exact-one", "force_refresh": True})
    assert failed["errors"][0]["code"] == "WAYBILL_EXACT_QUERY_DATE_UNAVAILABLE"
    assert len(rows(database)) == 2
