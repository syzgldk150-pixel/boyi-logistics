"""Monthly inventory, close, reopen and drift on a migrated loopback MySQL.

Ledger rows are written through the production batch/run/commit/finalize API,
so publication, derivatives and the drift hook run exactly as in a sync.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from uuid import uuid4

import pytest

from shared.finance import FinancePeriodError, FinanceRepository
from shared.finance.models import TransactionRecord
from shared.finance.validation import CaptureEvidence, validate_finance_capture
from tests import test_mysql_orchestration_integration as mysql_helpers
from tests.mysql_database_cleanup import drop_test_database


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8",
)
TODAY = dt.date(2026, 10, 23)
SOURCE = {"account": "period-test-account", "site": "PERIOD-SITE", "name": "期间测试网点", "producer": "period-test-producer"}


@pytest.fixture(scope="module")
def database():
    import pymysql

    if os.environ.get("AGENT_DB_HOST") != "127.0.0.1":
        pytest.fail("period close tests require the isolated loopback MySQL")
    case = type("FinancePeriodDatabase", (mysql_helpers.MySqlOrchestrationIntegrationTests,), {})
    case.pymysql = pymysql
    case.host = os.environ["AGENT_DB_HOST"]
    case.port = int(os.environ["AGENT_DB_PORT"])
    case.user = os.environ["AGENT_DB_USER"]
    case.password = os.environ["AGENT_DB_PASS"]
    case.database = f"test_finance_periods_{uuid4().hex}"
    case.runner = mysql_helpers._load_migration_runner()
    with case._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{case.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        case._run_migrations(case.database)
        yield case
    finally:
        with case._server_connection() as connection, connection.cursor() as cursor:
            drop_test_database(cursor, case.database)


@pytest.fixture(scope="module")
def repository(database):
    return FinanceRepository(lambda: database.pymysql.connect(
        host=database.host, port=database.port, user=database.user, password=database.password,
        database=database.database, charset="utf8mb4", autocommit=True,
        cursorclass=database.pymysql.cursors.DictCursor,
    ))


def month_days(year: int, month: int) -> list[dt.date]:
    day, days = dt.date(year, month, 1), []
    while day.month == month:
        days.append(day)
        day += dt.timedelta(days=1)
    return days


def seed(repository, days, items, *, opening=Decimal("1000.0000"), skip=(), waybill=True, tag="a"):
    """Publish one batch; a skipped day keeps moving the balance like a lost day."""

    batch_id = repository.create_batch(trigger_type="manual", start_date=days[0], end_date=days[-1],
                                       rescan_days=1, requested_by="period-test")
    balance = opening
    run_ids = {}
    for day in days:
        rows = []
        for ordinal, (name, direction, amount) in enumerate(items):
            income = amount if direction == "income" else Decimal("0.0000")
            expense = amount if direction == "expense" else Decimal("0.0000")
            after = balance + income - expense
            rows.append(TransactionRecord(
                platform="ronghui", account_id=SOURCE["account"], login_account=SOURCE["account"],
                source_record_key=f"{tag}-{day.isoformat()}-{ordinal}", business_date=day,
                primary_fee_name=name, direction=direction, income=income, expense=expense,
                before_balance=balance, after_balance=after,
                transaction_at=dt.datetime.combine(day, dt.time(9, ordinal)),
                waybill_no=f"WB{day:%m%d}{ordinal}" if waybill else "", source_reference=str(ordinal + 1),
                source_payload={"BALANCE_ORDER": str(ordinal + 1)},
            ))
            balance = after
        if day in skip:
            continue
        # Each day is cut from a verified period statement, as the statement capture commits it.
        validation = validate_finance_capture(CaptureEvidence(
            remote_total=len(rows), page_row_counts=[len(rows)], transactions=rows, summaries=(),
            intended_write_count=len(rows), period_statement_ref=f"period-test-{tag}"))
        run_id = repository.start_run(batch_id=batch_id, platform="ronghui", account_id=SOURCE["account"],
                                      login_account=SOURCE["account"], session_profile="period-test",
                                      target_date=day, source_site_code=SOURCE["site"],
                                      source_site_name=SOURCE["name"], producer_instance_id=SOURCE["producer"],
                                      producer_generation=1)
        repository.commit_run_snapshot(run_id=run_id, transactions=rows, summaries=(), validation=validation)
        run_ids[day] = run_id
    repository.finalize_batch(batch_id)
    if days[0].day == 1 and len(days) == len(month_days(days[0].year, days[0].month)):
        attest_month_capture(repository, days, run_ids)
    return balance


def attest_month_capture(repository, days, run_ids):
    """Mark the month's published runs as one verified full-month capture.

    Capture verification itself is covered by the statement plugin tests;
    here the close rules only need the ledger to be capture-backed.
    """

    with repository._connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO finance_platform_captures (capture_ref, platform, source_role, account_id, site_code, "
            "site_name, period_start, period_end, statement_item_count, statement_net, detail_row_count, "
            "detail_income, detail_expense, detail_net, field_mapping_json, request_profile_json, content_sha256, "
            "automation_id, plugin_version, generation, published_at, created_at) VALUES "
            "(%s, 'ronghui', 'finance_quote_source', %s, %s, %s, %s, %s, 0, 0, 0, 0, 0, 0, '{}', "
            "'{\"waybill_number_shapes\": []}', %s, %s, '1.0.0', 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
            (str(uuid4()), SOURCE["account"], SOURCE["site"], SOURCE["name"], days[0], days[-1], "0" * 64,
             SOURCE["producer"]),
        )
        capture_id = cursor.lastrowid
        cursor.executemany(
            "INSERT INTO finance_platform_capture_days (capture_id, business_date, run_id, outcome, record_count, "
            "published_at) VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6))",
            [(capture_id, day, run_id, "success", 0) for day, run_id in run_ids.items()],
        )


def bind(repository, view, *, month: str, level="operating"):
    for item in view["items"]:
        repository.save_fee_mapping(
            fee_item_id=item["fee_item_id"], fee_level=level, canonical_subject_name=item["primary_fee_name"],
            requires_waybill=level == "waybill", effective_start_month=month,
            include_in_cost=item["direction"] == "expense", changed_by="period-tester", reason="期间测试绑定",
        )


def check(view, code):
    return next(item for item in view["checks"] if item["code"] == code)


def request_id():
    return str(uuid4())


def test_migration_062_projects_period_tables_and_anomaly_acknowledgement(database):
    runtime, finance = database.database, database.database + "_finance"
    with database._server_connection() as connection, connection.cursor() as cursor:
        for table in ("finance_periods", "finance_period_item_reviews", "finance_period_closes",
                      "finance_period_close_lines", "finance_period_events"):
            cursor.execute("SELECT TABLE_SCHEMA, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_NAME = %s "
                           "AND TABLE_SCHEMA IN (%s, %s)", (table, runtime, finance))
            assert {(row["TABLE_SCHEMA"], row["TABLE_TYPE"]) for row in cursor.fetchall()} == {
                (finance, "BASE TABLE"), (runtime, "VIEW")}
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
                       "AND TABLE_NAME = 'finance_anomalies' AND COLUMN_NAME LIKE 'acknowledg%%'", (runtime,))
        assert {row["COLUMN_NAME"] for row in cursor.fetchall()} == {
            "acknowledged_by", "acknowledged_at", "acknowledge_reason", "acknowledged_count", "acknowledged_amount"}


def test_inventory_close_reopen_and_reclose(repository):
    items = (("期间派送费", "expense", Decimal("12.5000")), ("期间寄到付款", "income", Decimal("30.0000")))
    days = month_days(2026, 3)
    seed(repository, days, items, tag="close")
    view = repository.get_period_view("2026-03", today=TODAY)
    assert view["status"] == "open" and view["data_source"] == "live"
    assert all(check(view, code)["passed"] for code in ("month_finished", "coverage", "rescan_window", "balance_chain"))
    assert not check(view, "items_bound")["passed"] and not check(view, "reviews_and_anomalies")["passed"]
    assert {item["review_status"] for item in view["items"]} == {"unbound"}
    assert view["report"]["transaction_count"] == 62
    assert view["report"]["total_income"] == "930.00" and view["report"]["total_expense"] == "387.50"

    bind(repository, view, month="2026-03")
    view = repository.get_period_view("2026-03", today=TODAY)
    assert check(view, "items_bound")["passed"] and check(view, "reviews_and_anomalies")["passed"]
    assert not view["can_close"] and {item["review_status"] for item in view["items"]} == {"pending"}
    first = view["items"][0]
    with pytest.raises(FinancePeriodError) as stale:
        repository.confirm_period_items("2026-03", [{"fee_item_id": first["fee_item_id"],
                                                     "mapping_id": first["mapping"]["mapping_id"] + 999}],
                                        actor="period-tester", request_id=request_id())
    assert stale.value.code == "MAPPING_CHANGED"
    with pytest.raises(FinancePeriodError) as early:
        repository.close_period("2026-03", actor="period-admin", request_id=request_id(), today=TODAY)
    assert early.value.code == "PERIOD_CHECKS_FAILED"
    confirm_id = request_id()
    payload = [{"fee_item_id": item["fee_item_id"], "mapping_id": item["mapping"]["mapping_id"]} for item in view["items"]]
    confirmed = repository.confirm_period_items("2026-03", payload, actor="period-tester", request_id=confirm_id)
    assert repository.confirm_period_items("2026-03", payload, actor="period-tester", request_id=confirm_id) == confirmed

    view = repository.get_period_view("2026-03", today=TODAY)
    assert view["can_close"], view["checks"]
    close_id = request_id()
    closed = repository.close_period("2026-03", actor="period-admin", request_id=close_id, today=TODAY)
    assert closed["version"] == 1 and closed["transaction_count"] == 62
    assert closed["total_income"] == "930.00" and closed["total_expense"] == "387.50" and closed["net_change"] == "542.50"
    assert repository.close_period("2026-03", actor="period-admin", request_id=close_id, today=TODAY) == closed
    with pytest.raises(FinancePeriodError) as again:
        repository.close_period("2026-03", actor="period-admin", request_id=request_id(), today=TODAY)
    assert again.value.code == "PERIOD_CLOSED"

    view = repository.get_period_view("2026-03", today=TODAY)
    assert view["status"] == "closed" and view["data_source"] == "snapshot" and view["current_version"] == 1
    assert view["report"]["net_change"] == "542.50" and all(item["closed"] for item in view["items"])
    with pytest.raises(FinancePeriodError) as locked:
        repository.confirm_period_items("2026-03", payload, actor="period-tester", request_id=request_id())
    assert locked.value.code == "PERIOD_CLOSED"
    with pytest.raises(FinancePeriodError) as mapping_locked:
        repository.save_fee_mapping(fee_item_id=first["fee_item_id"], fee_level="operating",
                                    canonical_subject_name="改动后的科目",
                                    effective_start_month="2026-03", include_in_cost=False,
                                    changed_by="period-tester", reason="试图改动已封账月份")
    assert mapping_locked.value.code == "PERIOD_CLOSED"
    assert repository.get_period_view("2026-03", today=TODAY)["items"][0]["mapping"]["mapping_id"] == first["mapping"]["mapping_id"]
    # A version starting after the closed month leaves March untouched.
    repository.save_fee_mapping(fee_item_id=first["fee_item_id"], fee_level="operating",
                                canonical_subject_name="四月起新科目", effective_start_month="2026-04",
                                include_in_cost=first["direction"] == "expense",
                                changed_by="period-tester", reason="四月起改科目")

    with pytest.raises(FinancePeriodError) as no_reason:
        repository.reopen_period("2026-03", actor="period-admin", request_id=request_id(), reason=" ")
    assert no_reason.value.code == "INVALID_REQUEST"
    reopened = repository.reopen_period("2026-03", actor="period-admin", request_id=request_id(), reason="补录核对")
    assert reopened["status"] == "reopened" and reopened["reopen_reason"] == "补录核对"
    view = repository.get_period_view("2026-03", today=TODAY)
    assert view["status"] == "reopened" and view["data_source"] == "live"
    assert view["comparison"]["base_version"] == 1 and view["comparison"]["differences"] == []
    with pytest.raises(FinancePeriodError) as not_closed:
        repository.reopen_period("2026-03", actor="period-admin", request_id=request_id(), reason="再次反结账")
    assert not_closed.value.code == "PERIOD_NOT_CLOSED"
    second = repository.close_period("2026-03", actor="period-admin", request_id=request_id(), today=TODAY)
    assert second["version"] == 2 and second["content_sha256"] == closed["content_sha256"]
    view = repository.get_period_view("2026-03", today=TODAY)
    assert [item["version"] for item in view["closes"]] == [2, 1]
    assert view["comparison"]["base_version"] == 1 and view["comparison"]["delta"] == "0.00"


def test_rescan_after_close_records_drift_without_moving_the_snapshot(repository):
    items = (("漂移派送费", "expense", Decimal("8.0000")),)
    days = month_days(2026, 1)
    balance = seed(repository, days, items, tag="drift")
    view = repository.get_period_view("2026-01", today=TODAY)
    bind(repository, view, month="2026-01")
    view = repository.get_period_view("2026-01", today=TODAY)
    repository.confirm_period_items("2026-01", [{"fee_item_id": item["fee_item_id"], "mapping_id": item["mapping"]["mapping_id"]}
                                                 for item in view["items"]], actor="period-tester", request_id=request_id())
    repository.close_period("2026-01", actor="period-admin", request_id=request_id(), today=TODAY)
    assert balance == Decimal("752.0000")

    # A manual backfill of 2026-01-15 now reports a different amount.
    seed(repository, [dt.date(2026, 1, 15)], (("漂移派送费", "expense", Decimal("9.0000")),),
         opening=Decimal("888.0000"), tag="drift-backfill")
    view = repository.get_period_view("2026-01", today=TODAY)
    assert view["status"] == "closed" and view["report"]["total_expense"] == "248.00"
    assert view["drift"] and view["drift"][0]["business_date"] == "2026-01-15"
    assert view["drift"][0]["delta"] == "-1.00"
    unnotified = [row for row in repository.list_unnotified_anomalies(limit=200) if row["anomaly_type"] == "CLOSED_PERIOD_DRIFT"]
    assert len(unnotified) == 1 and json.loads(unnotified[0]["details_json"])["period_month"] == "2026-01"

    # Restoring the original data resolves the drift without reopening.
    seed(repository, [dt.date(2026, 1, 15)], items, opening=Decimal("888.0000"), tag="drift-restore")
    assert repository.get_period_view("2026-01", today=TODAY)["drift"] == []


def test_checks_report_gaps_early_snapshots_balance_breaks_and_unfinished_month(repository, database):
    items = (("缺口派送费", "expense", Decimal("5.0000")),)
    days = month_days(2026, 5)
    seed(repository, days, items, skip={dt.date(2026, 5, 10)}, tag="gap")
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE finance_sync_runs SET started_at = '2026-05-21 00:10:00' "
                       "WHERE account_id = %s AND target_date = '2026-05-20'", (SOURCE["account"],))
        cursor.execute("UPDATE finance_sync_runs SET validation_status = 'warning', validation_report_json = %s "
                       "WHERE account_id = %s AND target_date = '2026-05-21'",
                       (json.dumps({"warnings": [{"code": "AMOUNT_EXTREME"}]}), SOURCE["account"]))
        cursor.execute("UPDATE finance_sync_runs SET validation_status = 'warning', validation_report_json = %s "
                       "WHERE account_id = %s AND target_date = '2026-05-22'",
                       (json.dumps({"warnings": [{"code": "NEW_FEE_ITEM"}, {"code": "HISTORICAL_REVISION"}]}), SOURCE["account"]))
        connection.commit()
    view = repository.get_period_view("2026-05", today=TODAY)
    coverage = check(view, "coverage")
    assert not coverage["passed"]
    reasons = {issue["reason"]: issue["dates"] for issue in coverage["issues"]}
    assert reasons["缺少同步数据"] == ["2026-05-10"]
    assert reasons["校验未通过或有需处理的校验提示"] == ["2026-05-21"]
    rescan = check(view, "rescan_window")
    assert not rescan["passed"] and rescan["issues"][0]["dates"] == ["2026-05-20"]
    assert view["earliest_close_date"] == "2026-05-27"
    balance = check(view, "balance_chain")
    assert not balance["passed"]
    assert [(issue["from_date"], issue["to_date"], issue["difference"]) for issue in balance["issues"]] == [
        ("2026-05-09", "2026-05-11", "-5.00")]
    assert not repository.get_period_view("2026-05", today=dt.date(2026, 5, 31))["checks"][0]["passed"]


def test_missing_waybill_acknowledgement_survives_identical_rescans_only(repository):
    items = (("缺单号派送费", "expense", Decimal("6.0000")),)
    day = dt.date(2026, 7, 3)
    seed(repository, month_days(2026, 7), items, waybill=False, tag="ack")
    view = repository.get_period_view("2026-07", today=TODAY)
    bind(repository, view, month="2026-07", level="waybill")
    view = repository.get_period_view("2026-07", today=TODAY)
    blocking = check(view, "reviews_and_anomalies")
    anomalies = [issue for issue in blocking["issues"] if issue["kind"] == "missing_waybill"]
    assert not blocking["passed"] and len(anomalies) == 31
    target = next(issue for issue in anomalies if issue["business_date"] == day.isoformat())
    with pytest.raises(FinancePeriodError) as short:
        repository.acknowledge_anomaly(target["anomaly_id"], actor="period-tester", request_id=request_id(), reason="x")
    assert short.value.code == "INVALID_REQUEST"
    ack_id = request_id()
    result = repository.acknowledge_anomaly(target["anomaly_id"], actor="period-tester", request_id=ack_id,
                                            reason="平台未提供运单号")
    assert result["status"] == "acknowledged"
    assert repository.acknowledge_anomaly(target["anomaly_id"], actor="period-tester", request_id=ack_id,
                                          reason="平台未提供运单号") == result

    def open_ids():
        current = check(repository.get_period_view("2026-07", today=TODAY), "reviews_and_anomalies")
        return {issue["anomaly_id"] for issue in current["issues"] if issue["kind"] == "missing_waybill"}

    assert target["anomaly_id"] not in open_ids()
    seed(repository, [day], items, opening=Decimal("982.0000"), waybill=False, tag="ack-same")
    assert target["anomaly_id"] not in open_ids()
    seed(repository, [day], (("缺单号派送费", "expense", Decimal("7.0000")),), opening=Decimal("982.0000"),
         waybill=False, tag="ack-changed")
    assert target["anomaly_id"] in open_ids()


def test_close_requires_the_month_ledger_from_one_full_month_capture(repository):
    items = (("抓取校验派送费", "expense", Decimal("3.0000")),)
    seed(repository, month_days(2026, 2), items, tag="capture-backed")
    assert check(repository.get_period_view("2026-02", today=TODAY), "platform_capture")["passed"]
    # A later plain daily sync of one day replaces that day's visible run.
    seed(repository, [dt.date(2026, 2, 9)], items, opening=Decimal("976.0000"), tag="capture-daily")
    result = check(repository.get_period_view("2026-02", today=TODAY), "platform_capture")
    assert not result["passed"]
    assert result["issues"][0]["reason"] == "这些日期的账本不是来自整月统计抓取"
    assert result["issues"][0]["dates"] == ["2026-02-09"]
