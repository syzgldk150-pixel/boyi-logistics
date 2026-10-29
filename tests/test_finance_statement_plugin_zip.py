"""Statement capture as the actual ZIP: bwrap sandbox, Unix broker, account HTTP and finance connectors.

The fake Ronghui page answers the plugin's declared requests through the real
``AccountHTTPTransport`` seam, so the Host connector checks the plugin's pages
against the observations the production Broker actually recorded.
"""
from __future__ import annotations

import datetime as dt
import shutil
from decimal import Decimal

import pytest

from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.developer_v2 import build_service_v2_package
from agent.automation_plugins.finance_connectors_v2 import build_finance_connectors
from plugin_core_adapters import finance_platform_capture as capture_module
from plugin_core_adapters.finance_platform_capture import build_production_finance_statement_handler_map
from shared.data_sources import DataSourceRepository
from shared.finance import FinanceRepository
from tests.service_v2_production_protocol_support import PackagedConnectorHost
from tests.test_automation_plugin_connector_runtime_v2 import _AccountResolver
from tests.test_finance_periods_mysql import database  # noqa: F401  (module-scoped migrated database)
from tests.test_finance_statement_plugin_mysql import SITES, FakeRonghui, Manager


pytestmark = [
    pytest.mark.skipif(shutil.which("bwrap") is None, reason="requires bubblewrap"),
    pytest.mark.skipif(__import__("os").getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8"),
]
ACCOUNTS = {role: account for role, (account, *_rest) in SITES.items()}
CONTEXTS = {account: {"loginSiteCode": site, "loginSiteName": name, "loginEmpCode": "9001", "loginEmpName": "合成财务"}
            for account, site, name, _per_day in SITES.values()}


class Page:
    """The fake Ronghui page behind each selected account."""

    def __init__(self, ronghui):
        self.ronghui = ronghui
        self.calls = []

    def transport(self):
        page = self

        class Transport:
            def __init__(self, *, system, account_id):
                assert system == "ronghui" and account_id in CONTEXTS
                self.account_id = account_id

            def exchange(self, declaration, body, *, mark_write_started):
                assert declaration["action"] == "read_json"
                assert body["SITE_NAME_CODE"] == CONTEXTS[self.account_id]["loginSiteCode"]
                page.calls.append((self.account_id, declaration["name"], dict(body)))
                return {"status_code": 200, "response": page.ronghui.answer(declaration["name"], body)}

            def close(self):
                pass

        return Transport


def packaged(tmp_path, monkeypatch, database, ronghui):  # noqa: F811
    monkeypatch.setattr("tests.service_v2_production_protocol_support.build_plugin_zip", build_service_v2_package)
    page = Page(ronghui)
    monkeypatch.setattr("agent.automation_plugins.account_http.AccountHTTPTransport", page.transport())
    read_context = lambda context, _arguments, _manifest: CONTEXTS[context.account_ids[0]]  # noqa: E731
    monkeypatch.setattr("agent.automation_plugins.account_context.account_context_read", read_context)
    monkeypatch.setattr(capture_module, "account_context_read", read_context)
    monkeypatch.setattr(DataSourceRepository, "producer_snapshot", lambda self, *_a, **_k: {
        "plugin_id": "sync_ronghui_finance_statement_v2", "plugin_version": "1.0.0",
        "package_sha256": "a" * 64, "manifest_sha256": "b" * 64})
    repository = FinanceRepository(lambda: database.pymysql.connect(
        host=database.host, port=database.port, user=database.user, password=database.password,
        database=database.database, charset="utf8mb4", autocommit=True,
        cursorclass=database.pymysql.cursors.DictCursor))
    reviewed = build_production_finance_statement_handler_map(
        cursor_secret=b"f" * 32, account_manager=Manager(), repository_factory=lambda: repository)
    context = CoreBrokerInvocationContext(
        automation_id="isolated-finance-statement", plugin_version="1.0.0",
        tool_name="sync_ronghui_finance_statement_v2", operation="service.invoke", action="run",
        role="__system__", account_bindings={role: (account,) for role, account in ACCOUNTS.items()},
    )
    host = PackagedConnectorHost(tmp_path, "sync_ronghui_finance_statement_v2",
                                 ConnectorRegistry(build_finance_connectors(reviewed)), context,
                                 account_resolver=_AccountResolver(system="ronghui"))
    return host, page, repository


def captures(database, month):  # noqa: F811
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT site_code, detail_row_count, statement_net, published_at FROM finance_platform_captures "
                       "WHERE period_start = %s ORDER BY id", (month,))
        return list(cursor.fetchall())


def test_real_zip_captures_three_bound_accounts_and_publishes_the_month(tmp_path, monkeypatch, database):  # noqa: F811
    host, page, repository = packaged(tmp_path, monkeypatch, database, FakeRonghui())
    result = host.execute({"month": "2026-03"}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert result["meta"]["write_outcome"] == "WRITE_VERIFIED"
    assert {source["site_name"]: source["detail_rows"] for source in result["data"]["sources"]} == {
        "合成大站": 620, "合成小站": 93, "合成自提部": 25}
    assert {account for account, _name, _body in page.calls} == set(ACCOUNTS.values())
    # Settlement statement, billing statement and two detail pages for the big site.
    assert sorted(name for account, name, _ in page.calls if account == ACCOUNTS["finance_quote_source"]) == [
        "detail_quote", "detail_quote", "statement_quote", "statement_quote"]
    stored = captures(database, dt.date(2026, 3, 1))
    assert len(stored) == 3 and all(row["published_at"] for row in stored)
    writes = [row for row in host.receipts if row["action"] == "write_platform_capture"]
    assert len(writes) == 3
    view = repository.get_period_view("2026-03", today=dt.date(2026, 10, 23))
    assert {check["code"]: check["passed"] for check in view["checks"]}["platform_capture"]
    assert Decimal(view["report"]["net_change"]) == sum(Decimal(str(row["statement_net"])) for row in stored)


def test_real_zip_month_without_settlements_publishes_no_data_days(tmp_path, monkeypatch, database):  # noqa: F811
    ronghui = FakeRonghui()
    ronghui.rows = {site: [] for site in ronghui.rows}
    host, _page, repository = packaged(tmp_path, monkeypatch, database, ronghui)
    result = host.execute({"month": "2026-04"}, operation="run")
    assert result["status"] == "SUCCESS", result
    assert all(source["detail_rows"] == 0 and source["ledger_rows"] == 0 and source["published_days"] == 30
               for source in result["data"]["sources"])
    assert len(captures(database, dt.date(2026, 4, 1))) == 3
    assert repository.get_period_view("2026-04", today=dt.date(2026, 10, 23))["report"]["transaction_count"] == 0


def test_real_zip_missing_key_field_is_refused_by_the_host(tmp_path, monkeypatch, database):  # noqa: F811
    ronghui = FakeRonghui()
    for rows in ronghui.rows.values():
        for row in rows:
            row["BALANCE_DATE"] = row["BALANCE_DATE"].replace("2026-03", "2026-05")
            row["FINANCE_DATE"] = row["FINANCE_DATE"].replace("2026-03", "2026-05")
    first = next(iter(ronghui.rows.values()))
    first[5]["GUID"] = ""
    host, _page, _repository = packaged(tmp_path, monkeypatch, database, ronghui)
    result = host.execute({"month": "2026-05"}, operation="run")
    assert result["status"] == "FAILED" and result["error"]["code"] == "CAPTURE_FIELD_MISSING", result
    assert result["meta"]["write_outcome"] == "WRITE_OUTCOME_UNKNOWN"
    assert captures(database, dt.date(2026, 5, 1)) == []


def test_real_zip_statement_mismatch_stops_before_any_write(tmp_path, monkeypatch, database):  # noqa: F811
    ronghui = FakeRonghui()
    answer = ronghui.answer

    def drifting(request, body):
        result = answer(request, body)
        if request.startswith("statement_") and "BALANCE_DATE" in body and result["data"]:
            result["data"][0]["BALANCE_CUR_MONEY"] += 1
        return result

    ronghui.answer = drifting
    for rows in ronghui.rows.values():
        for row in rows:
            row["BALANCE_DATE"] = row["BALANCE_DATE"].replace("2026-03", "2026-07")
            row["FINANCE_DATE"] = row["FINANCE_DATE"].replace("2026-03", "2026-07")
    host, _page, _repository = packaged(tmp_path, monkeypatch, database, ronghui)
    result = host.execute({"month": "2026-07"}, operation="run")
    assert result["status"] == "FAILED" and result["error"]["code"] == "STATEMENT_MISMATCH", result
    assert result["meta"]["write_outcome"] == "NOT_APPLIED" and not host.receipts
    assert captures(database, dt.date(2026, 7, 1)) == []


def test_undeclared_period_is_refused_before_reading(tmp_path, monkeypatch, database):  # noqa: F811
    host, page, _repository = packaged(tmp_path, monkeypatch, database, FakeRonghui())
    result = host.execute({"month": "2099-01"}, operation="run")
    assert result["status"] == "FAILED" and result["error"]["code"] == "MONTH_NOT_FINISHED"
    assert page.calls == [] and not host.receipts
