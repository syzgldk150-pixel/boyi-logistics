"""Statement plugin → Host capture primitives → ledger, on a migrated loopback MySQL.

The plugin's real ``action.run`` talks to a fake Ronghui finance page through a
Host double that records observations exactly like the Broker; the Host
primitives are the production handlers over the real repository.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import types
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from plugin_core_adapters import finance_platform_capture as capture_module
from plugin_core_adapters.finance_platform_capture import _FinancePlatformCaptureHandlers
from shared.data_sources import DataSourceRepository
from shared.finance import FinanceRepository
from tests.test_finance_periods_mysql import database  # noqa: F401  (module-scoped migrated database)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8",
)
ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_ID = "statement-plugin-test"
SITES = {
    "finance_quote_source": ("stmt-quote", "7100004", "合成大站", 20),
    "finance_daxiang_s_source": ("stmt-daxiang-s", "7100017", "合成小站", 3),
    "finance_self_pickup_source": ("stmt-self-pickup", "7100001", "合成自提部", 1),
}
ACTIONS = {"write_platform_capture": capture_module.WRITE_ACTION,
           "publish_capture_day": capture_module.PUBLISH_DAY_ACTION,
           "finalize_capture": capture_module.FINALIZE_ACTION}


def load_action():
    """Execute the package source without leaving bytecode in the package tree."""

    path = ROOT / "agent/service_v2_plugins/sync_ronghui_finance_statement_v2/payload/action.py"
    module = types.ModuleType("statement_plugin_action")
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def ronghui_rows(site_code, site_name, per_day):
    rows, balance, order = [], Decimal("5000.00"), 0
    for day in range(1, 32):
        if per_day == 1 and day % 5 == 0:
            continue  # a day without any settlement
        for index in range(per_day):
            order += 1
            item, amount = (("合成派送费", Decimal("-7.25")), ("合成到付款", Decimal("21.00")),
                            ("合成包仓费", Decimal("-100.00")))[index % 3]
            if index == 0 and day == 3:
                item, amount = "合成品质罚款", Decimal("0")
            after = balance + amount
            settled = dt.datetime(2026, 3, day, 8, 0) + dt.timedelta(minutes=index)
            rows.append({
                "GUID": f"{site_code}-{order}", "BALANCE_DATE": settled.strftime("%Y-%m-%d %H:%M:%S"),
                "FINANCE_DATE": (settled - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                "BALANCE_TYPE": item, "BALANCE_CUR_MONEY": float(amount),
                "BALANCE_PRE_CONFIRM_MONEY": float(balance), "BALANCE_BACK_CONFIRM_MONEY": float(after),
                "BILL_CODE": f"R{order:011d}" if item != "合成包仓费" else f"S2026{order:015d}",
                "BALANCE_ORDER": 600000000 + order, "SITE_NAME_CODE": site_code, "SITE_NAME": site_name,
                "SOURCE_TYPE": "系统", "REMARK": "", "CUSTOMER_NAME": "合成客户",
            })
            balance = after
    return rows


class FakeRonghui:
    """Answers the two declared finance calls like the real page does."""

    def __init__(self):
        self.rows = {site: ronghui_rows(site, name, per_day) for _role, (_acct, site, name, per_day) in SITES.items()}

    def answer(self, request, body):
        field = "BALANCE_DATE" if "BALANCE_DATE" in body else "FINANCE_DATE"
        period = json.loads(body[field])
        start = dt.datetime.strptime(period["start"], "%Y/%m/%d %H:%M:%S")
        end = dt.datetime.strptime(period["end"], "%Y/%m/%d %H:%M:%S")
        rows = [row for row in self.rows[body["SITE_NAME_CODE"]]
                if start <= dt.datetime.strptime(row[field], "%Y-%m-%d %H:%M:%S") <= end]
        if request.startswith("statement_"):
            totals = {}
            for row in rows:
                totals[row["BALANCE_TYPE"]] = totals.get(row["BALANCE_TYPE"], Decimal(0)) + Decimal(str(row["BALANCE_CUR_MONEY"]))
            rows = [{"BALANCE_TYPE": name, "BALANCE_CUR_MONEY": float(amount), "SITE_NAME_CODE": body["SITE_NAME_CODE"],
                     "SITE_NAME": rows[0]["SITE_NAME"], "CENTER_NAME": "合成中心"} for name, amount in totals.items()]
        index, size = int(body["pageIndex"]), int(body["pageSize"])
        return {"total": len(rows), "data": rows[index * size:(index + 1) * size], "condition": {"x": [1, {"y": None}]}}


class Result(dict):
    def __init__(self, data, reference):
        super().__init__(data)
        self.host_evidence_ref = reference


class FakeHost:
    """Broker double: records http.request results exactly as observations."""

    def __init__(self, handlers, ronghui, *, tamper=None):
        self.handlers, self.ronghui, self.tamper = handlers, ronghui, tamper
        self.observations: list[dict] = []
        self.calls = 0

    def _context(self, role, action):
        accounts = {item: (acct,) for item, (acct, *_rest) in SITES.items()}
        return CoreBrokerInvocationContext(
            automation_id=AUTOMATION_ID, plugin_version="1.0.0", tool_name="finance_statement",
            operation="ledger.invoke", action=action, role=role, account_ids=accounts[role],
            account_bindings=accounts, generation=1, write_attempt_identity={"invocation_id": "inv-test"},
            host_call_observations=lambda: tuple(copy.deepcopy(self.observations)),
        )

    def __call__(self, operation, *, action, role, arguments):
        self.calls += 1
        reference = f"host-ref-{self.calls}"
        if operation == "http.request":
            result = {"status_code": 200, "response": self.ronghui.answer(arguments["request"], arguments["body"])}
            self.observations.append({
                "operation": operation, "action": action, "role": role, "write_started": False,
                "arguments_sha256": hashlib.sha256(canonical_json_bytes(arguments)).hexdigest(),
                "evidence_ref": reference, "result": json.loads(canonical_json_bytes(result)),
            })
            return Result(result, reference)
        if operation == "account.context":
            _acct, site, name, _per_day = SITES[role]
            return Result({"loginSiteCode": site, "loginSiteName": name, "loginEmpCode": "", "loginEmpName": ""}, reference)
        assert operation == "service.invoke"
        connector_role = arguments["service"].removeprefix("connector.boyi.").removesuffix("@1")
        values = arguments["arguments"]
        if self.tamper and arguments["operation"] == "write_platform_capture":
            values = self.tamper(copy.deepcopy(values))
        handler = self.handlers.platform_capture_handler_map()[("ledger.invoke", ACTIONS[arguments["operation"]])]
        try:
            result = handler(self._context(connector_role, ACTIONS[arguments["operation"]]), values)
        except PluginExecutionError as exc:
            raise RuntimeError(exc.code) from exc  # the Broker returns only the error code
        return Result({key: value for key, value in result.items() if key != "evidence_ref"}, reference)


class Manager:
    def require_active_binding_descriptor(self, account_id):
        return {"account_id": account_id, "system": "ronghui", "session_profile": f"profile-{account_id}"}

    def public_credentials(self, account_id):
        return {"username": f"login-{account_id}"}


@pytest.fixture
def host(database, monkeypatch):  # noqa: F811
    repository = FinanceRepository(lambda: database.pymysql.connect(
        host=database.host, port=database.port, user=database.user, password=database.password,
        database=database.database, charset="utf8mb4", autocommit=True,
        cursorclass=database.pymysql.cursors.DictCursor))
    sites = {acct: {"loginSiteCode": site, "loginSiteName": name} for acct, site, name, _ in SITES.values()}
    monkeypatch.setattr(capture_module, "account_context_read",
                        lambda context, _arguments, _manifest: sites[context.account_ids[0]])
    monkeypatch.setattr(DataSourceRepository, "producer_snapshot",
                        lambda self, *_args, **_kwargs: {"plugin_id": "sync_ronghui_finance_statement_v2",
                                                         "plugin_version": "1.0.0", "package_sha256": "a" * 64,
                                                         "manifest_sha256": "b" * 64})
    handlers = _FinancePlatformCaptureHandlers(
        account_manager=Manager(), repository_factory=lambda: repository, cursor_secret=b"s" * 32,
    )
    return repository, handlers


def test_monthly_capture_is_verified_kept_raw_and_published_to_the_ledger(host, database):  # noqa: F811
    repository, handlers = host
    before = repository.get_period_view("2026-03", today=dt.date(2026, 10, 23))
    assert not {check["code"]: check for check in before["checks"]}["platform_capture"]["passed"]
    assert before["platform"]["sources"] == []
    action = load_action()
    fake = FakeHost(handlers, FakeRonghui())
    result = action.run(fake, {"month": "2026-03"}, today=dt.date(2026, 10, 23))
    assert result["status"] == "SUCCESS", result["error"]
    sources = {item["site_name"]: item for item in result["data"]["sources"]}
    assert sources["合成大站"]["detail_rows"] == 620 and sources["合成大站"]["published_days"] == 31
    assert sources["合成自提部"]["detail_rows"] == 25

    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT site_code, detail_row_count, statement_net, detail_net, billing_statement_net, "
                       "published_at, publish_batch_id FROM finance_platform_captures ORDER BY id")
        captures = cursor.fetchall()
        assert len(captures) == 3 and all(row["published_at"] and row["statement_net"] == row["detail_net"]
                                          for row in captures)
        cursor.execute("SELECT JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.CUSTOMER_NAME')) AS customer, COUNT(*) AS n "
                       "FROM finance_platform_detail_rows GROUP BY customer")
        assert cursor.fetchall() == [{"customer": "合成客户", "n": 620 + 93 + 25}]
        cursor.execute("SELECT COUNT(*) AS n FROM finance_platform_detail_rows WHERE amount = 0")
        zero_rows = cursor.fetchone()["n"]
        cursor.execute("SELECT COUNT(*) AS n FROM finance_transactions")
        assert cursor.fetchone()["n"] == 620 + 93 + 25 - zero_rows
        cursor.execute("SELECT status, COUNT(*) AS n FROM finance_sync_runs GROUP BY status ORDER BY status")
        # The self-pickup site has no settlement on six days and only a zero row on the 3rd.
        assert {row["status"]: row["n"] for row in cursor.fetchall()} == {"no_data": 7, "success": 86}
        cursor.execute("SELECT DISTINCT status FROM finance_sync_batches")
        assert [row["status"] for row in cursor.fetchall()] == ["success"]

    view = repository.get_period_view("2026-03", today=dt.date(2026, 10, 23))
    checks = {check["code"]: check["passed"] for check in view["checks"]}
    assert checks["coverage"] and checks["rescan_window"] and checks["balance_chain"], view["checks"]
    assert checks["platform_capture"], view["checks"]
    statement_total = sum(Decimal(str(row["statement_net"])) for row in captures)
    assert Decimal(view["report"]["net_change"]) == statement_total
    assert Decimal(view["platform"]["settlement_net"]) == statement_total

    platform = {item["item_name"]: item for item in view["platform"]["items"]}
    assert platform["合成派送费"]["suggestion"]["level"] == "waybill"
    assert platform["合成包仓费"]["suggestion"]["level"] == "operating"
    assert platform["合成包仓费"]["number_shapes"] == [{"label": "S+19位数字", "rows": 217, "waybill": False}]
    assert platform["合成品质罚款"]["zero_only"] and platform["合成品质罚款"]["settlement_amount"] == "0.00"
    assert {source["site_name"] for source in view["platform"]["sources"]} == {"合成大站", "合成小站", "合成自提部"}
    ledger = {item["primary_fee_name"]: item for item in view["items"] if item["direction"] == "expense"}
    assert ledger["合成派送费"]["platform_capture"]["settlement_amount"] == ledger["合成派送费"]["net_change"]
    assert "合成品质罚款" not in ledger

    big = next(source for source in view["platform"]["sources"] if source["site_name"] == "合成大站")
    detail = repository.platform_month_details("2026-03", capture_key=big["capture_key"],
                                               item_name="合成派送费", basis="settlement", limit=5)
    assert detail["total"] == 216 and detail["complete"] and len(detail["rows"]) == 5
    first = detail["rows"][0]
    assert first["customer_name"] == "合成客户" and first["is_waybill_number"] and first["number_shape"] == "R+11位数字"
    assert first["raw"]["BALANCE_TYPE"] == "合成派送费"
    billing = repository.platform_month_details("2026-03", capture_key=big["capture_key"],
                                                item_name="合成派送费", basis="billing")
    assert billing["complete"] and billing["detail_amount"] == billing["statement_amount"]
    with pytest.raises(LookupError):
        repository.platform_month_details("2026-04", capture_key=big["capture_key"],
                                          item_name="合成派送费", basis="settlement")


def test_pages_that_differ_from_host_observations_are_rejected(host):
    _repository, handlers = host
    action = load_action()

    def tamper(values):
        values["detail_pages"][0]["response"]["data"][0]["BALANCE_CUR_MONEY"] = 1.0
        return values

    result = action.run(FakeHost(handlers, FakeRonghui(), tamper=tamper), {"month": "2026-03"},
                        today=dt.date(2026, 10, 23))
    assert result["status"] == "FAILED" and result["error"]["code"] == "BROKER_SOURCE_MISMATCH"


def test_unfinished_month_and_republish_are_refused(host):
    _repository, handlers = host
    action = load_action()
    assert action.run(FakeHost(handlers, FakeRonghui()), {"month": "2026-10"},
                      today=dt.date(2026, 10, 23))["error"]["code"] == "MONTH_NOT_FINISHED"
    fake = FakeHost(handlers, FakeRonghui())
    role = "finance_self_pickup_source"
    site = SITES[role][1]
    period = json.dumps({"start": "2026/03/01 00:00:00", "end": "2026/03/31 23:59:59"}, ensure_ascii=False)
    client = action._Client(fake)
    written = client.connector(role, "write_platform_capture", {
        "schema_version": 1, "period_start": "2026-03-01", "period_end": "2026-03-31",
        "field_mapping": action.FIELD_MAPPING, "request_profile": action.REQUEST_PROFILE,
        "statement_pages": client.pages("statement_self_pickup", role, "BALANCE_DATE", period, site),
        "billing_statement_pages": [],
        "detail_pages": client.pages("detail_self_pickup", role, "BALANCE_DATE", period, site),
    })
    day = {"schema_version": 1, "capture_key": written["capture_key"], "business_date": "2026-03-02"}
    client.connector(role, "publish_capture_day", day)
    with pytest.raises(RuntimeError, match="BROKER_STATE_CONFLICT"):
        client.connector(role, "publish_capture_day", day)
    with pytest.raises(RuntimeError):
        client.connector(role, "finalize_capture", {"schema_version": 1, "capture_key": written["capture_key"]})
