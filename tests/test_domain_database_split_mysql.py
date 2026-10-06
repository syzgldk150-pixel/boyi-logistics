"""Real MySQL coverage for physical relocation, routing and reversible cutover."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pymysql
import pytest

from console.database import DocumentRepository
from shared.finance.repository import FinanceRepository
from shared.runtime_repositories import WAYBILL_FIELDS, WaybillRepository
from shared.waybill_source_coverage import WaybillSourceRepository, WaybillSourceScope

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")
ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "agent/scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database(tmp_path):
    if os.environ.get("AGENT_DB_HOST") != "127.0.0.1" or os.environ.get("AGENT_DB_PORT") != "33330":
        pytest.fail("database split tests require the isolated loopback MySQL on port 33330")
    name = "test_split_" + uuid4().hex[:12]
    options = dict(host="127.0.0.1", port=33330, user=os.environ["AGENT_DB_USER"],
                   password=os.environ.get("AGENT_DB_PASS", ""), charset="utf8mb4",
                   cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    conn = pymysql.connect(**options)
    migration = load("migration_055_domain_databases")
    runner = load("run_migrations")
    with conn.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
        conn.select_db(name)
        cursor.execute(runner.SCHEMA_MIGRATIONS_SQL)
        for version, path in runner.discover_migrations():
            if int(version) <= 13 or version in {"039", "040", "045", "051", "052", "054"}:
                for statement in runner.split_sql_statements(path.read_text(encoding="utf-8")):
                    cursor.execute(statement)
        for table, identity, number, source, fee, status in (
            ('waybills',1,'RH-1','ronghui','12.30','cancelled'),
            ('waybills',2,'YD-1','yunda','45.67','in_transit'),
            ('waybills',3,'OCR-1','ocr','0.00','in_transit'),
            ('boyi_waybills',1,'BY00001','manual','20.00','in_transit'),
        ):
            row = {field: '' for field in WAYBILL_FIELDS}
            row.update(id=identity,waybill_no=number,source=source,freight_fee=fee,status=status,
                       open_date='2026-09-08',created_at='2026-09-08 12:00:00',updated_at='2026-09-08 12:00:00')
            if source == 'manual':
                row.update(receipt_required=1,sender_address='测试发件地址')
            cursor.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('%s' for _ in row)})", list(row.values()))
        cursor.execute("INSERT INTO receipt_records(id,platform,direction,waybill_no,receipt_no,raw_payload_json,synced_at,created_at,updated_at) VALUES (1,'yunda','send','YD-1','RC-Y','{}',NOW(),NOW(),NOW()),(2,'ronghui','send','RH-1','RC-R','{}',NOW(),NOW(),NOW())")
        cursor.execute("INSERT INTO receipt_attachments(record_id,source_url,created_at,updated_at) VALUES (1,'fixture://photo',NOW(),NOW())")
    options["database"] = name

    @contextmanager
    def connect():
        c = pymysql.connect(**{**options, "autocommit": False})
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def apply():
        with conn.cursor() as cursor:
            migration.apply(cursor, ROOT / "agent/migrations/055_domain_database_split.sql", runner.split_sql_statements, tmp_path)

    finance_repo = FinanceRepository(connect)
    batch = finance_repo.create_batch(trigger_type="fixture", start_date="2026-09-08", end_date="2026-09-08")
    run = finance_repo.start_run(batch_id=batch, platform="ronghui", account_id="fixture",
                                 login_account="fixture", session_profile="fixture", target_date="2026-09-08")
    with conn.cursor() as cursor:
        for direction, income, expense, before, after in (
            ("income", "12.3456", "0.0000", "100.0000", "112.3456"),
            ("expense", "0.0000", "7.8901", "112.3456", "104.4555"),
        ):
            cursor.execute("INSERT INTO finance_fee_items(platform,raw_primary_fee_name,direction,first_seen_month,last_seen_month,created_at,updated_at) VALUES('ronghui','fixture',%s,'2026-09-01','2026-09-01',NOW(6),NOW(6))", (direction,))
            fee = cursor.lastrowid
            cursor.execute("INSERT INTO finance_transactions(run_id,fee_item_id,platform,account_id,login_account,source_record_key,business_date,raw_primary_fee_name,direction,income,expense,before_balance,after_balance,source_payload_json,created_at) VALUES(%s,%s,'ronghui','fixture','fixture',%s,'2026-09-08','fixture',%s,%s,%s,%s,%s,'{}',NOW(6))", (run, fee, direction, direction, income, expense, before, after))

    try:
        yield conn, connect, migration, apply
    finally:
        # All names were allocated by this fixture; child schemas go first.
        with conn.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            for suffix in ("_finance", "_waybill", ""):
                cursor.execute(f"DROP DATABASE IF EXISTS `{name + suffix}`")
        conn.close()


def repository(connect):
    repo = DocumentRepository.__new__(DocumentRepository)
    repo.placeholder = "%s"
    repo.connect = connect
    return repo


def test_physical_split_preserves_rows_links_and_finance_foreign_keys(database):
    conn, connect, migration, apply = database
    apply()
    apply()  # An acknowledged migration is idempotent.
    with conn.cursor() as cur:
        runtime, waybill, finance = migration.names(cur)
        assert migration.table_type(cur, runtime, "finance_transactions") == "VIEW"
        assert migration.table_type(cur, finance, "finance_transactions") == "BASE TABLE"
        assert migration.table_type(cur, waybill, "yunda_receipts") == "BASE TABLE"
        cur.execute("SELECT COUNT(*) AS n FROM waybills")
        assert cur.fetchone()["n"] == 3
        cur.execute("SELECT r.platform FROM receipt_attachments a JOIN receipt_records r ON r.id=a.record_id")
        assert cur.fetchone()["platform"] == "yunda"
        cur.execute("SELECT REFERENCED_TABLE_SCHEMA FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND TABLE_NAME='finance_transactions' AND REFERENCED_TABLE_NAME='finance_fee_items'", (finance,))
        assert cur.fetchone()["REFERENCED_TABLE_SCHEMA"] == finance
        cur.execute("SELECT REFERENCED_TABLE_SCHEMA FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND TABLE_NAME='finance_source_run_bindings' AND REFERENCED_TABLE_NAME='module_data_sources'", (finance,))
        assert cur.fetchone()["REFERENCED_TABLE_SCHEMA"] == runtime
        cur.execute("SELECT SUM(income) AS income,SUM(expense) AS expense,SUM(income-expense) AS net FROM finance_transactions")
        totals = cur.fetchone()
        assert str(totals["income"]) == "12.3456"
        assert str(totals["expense"]) == "7.8901"
        assert str(totals["net"]) == "4.4555"
    finance_repo = FinanceRepository(connect)
    finance_repo.initialize_schema()
    batch_id = finance_repo.create_batch(trigger_type="manual", start_date="2026-09-08", end_date="2026-09-08")
    assert batch_id > 0
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM `{finance}`.finance_sync_batches WHERE id=%s", (batch_id,))
        assert cur.fetchone()["n"] == 1


def test_platform_writes_same_id_and_receipt_global_identity(database):
    conn, connect, migration, apply = database
    apply()
    repo = repository(connect)
    assert repo.get_waybill(1, source="manual")["waybill_no"] == "BY00001"
    assert repo.get_waybill(1, source="ronghui")["waybill_no"] == "RH-1"
    with pytest.raises(ValueError):
        repo.get_waybill(1)
    assert repo.update_waybill_status(1, "signed", source="manual")
    assert repo.get_waybill(1, source="ronghui")["status"] == "cancelled"
    assert repo.get_waybill(3, source="ocr")["waybill_no"] == "OCR-1"
    assert repo.search_waybills({"date_from":"2026-09-08", "date_to":"2026-09-08"})["pagination"]["total"] == 4
    yunda = repo.upsert_receipt_record({"platform":"yunda", "direction":"send", "waybill_no":"NEW-Y", "receipt_no":"RC-NEW-Y"})
    ronghui = repo.upsert_receipt_record({"platform":"ronghui", "direction":"send", "waybill_no":"NEW-R", "receipt_no":"RC-NEW-R"})
    assert yunda["id"] != ronghui["id"]
    assert yunda["id"] > 2 and ronghui["id"] > 2
    again = repo.upsert_receipt_record({"platform":"yunda", "direction":"send", "waybill_no":"NEW-Y", "receipt_no":"RC-NEW-Y"})
    assert again["id"] == yunda["id"]
    repo.update_receipt_audit_status(ronghui["id"], "审核通过")
    assert repo.get_receipt_record(yunda["id"])["audit_status"] != "审核通过"
    assert repo.get_receipt_record(ronghui["id"])["audit_status"] == "审核通过"
    with conn.cursor() as cur, pytest.raises(RuntimeError, match="mismatch"):
        migration.restore(cur)  # A write after activation prevents stale restore.


def test_source_publication_and_legacy_sync_use_platform_tables(database):
    conn, connect, migration, apply = database
    apply()
    source_repo = WaybillSourceRepository(connect)
    scope = WaybillSourceScope("yunda", "yunda:native-waybill", "site/send", "account")
    record = {"waybill_no":"YD-NEW", "source_record_id":"YD-NEW", "open_date":"2026-09-08", "freight_fee":"88.50"}
    source_repo.publish([record], scope=scope, business_date=date(2026,9,8), captured_at=datetime(2026,9,9,tzinfo=timezone.utc), complete=True, expected_total=1)
    assert source_repo.read_scope(scope, date(2026,9,8))[0]["waybill_no"] == "YD-NEW"
    legacy = WaybillRepository(connect)
    legacy.sync_records([{"waybill_no":"RH-NEW", "open_date":"2026-09-08"}], source="ronghui")
    assert legacy.get_by_number("RH-NEW", source="ronghui")
    legacy.update_statuses(["RH-NEW", "YD-NEW"], "signed")
    assert legacy.get_by_number("RH-NEW", source="ronghui")["status"] == "signed"
    assert legacy.get_by_number("YD-NEW", source="yunda")["status"] == "signed"


def test_restore_and_reapply_before_activation(database):
    conn, connect, migration, apply = database
    apply()
    with conn.cursor() as cur:
        migration.restore(cur)
        runtime, _, _ = migration.names(cur)
        assert migration.table_type(cur, runtime, "waybills") == "BASE TABLE"
        assert migration.table_type(cur, runtime, "finance_transactions") == "BASE TABLE"
        cur.execute("SELECT COUNT(*) AS n FROM waybills")
        assert cur.fetchone()["n"] == 3
    apply()
