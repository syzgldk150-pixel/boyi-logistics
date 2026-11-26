"""派件账单存储（迁移 093–095 + shared.finance.delivery_bill_repository）on the isolated loopback MySQL. 数据虚构。"""
from __future__ import annotations

import datetime as dt
import json
import os
from uuid import uuid4

import pytest

from shared.finance import FinanceActor, FinanceError, FinanceRepository, FinanceValidationError
from shared.finance.delivery_bills import build_bill, settlement_entries
from tests.mysql_migrated_database import database  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="isolated MySQL required")

ACTOR = FinanceActor("7", "测试会计")
D = dt.date


@pytest.fixture(autouse=True)
def require_loopback():
    if os.environ.get("AGENT_DB_HOST") != "127.0.0.1" or os.environ.get("AGENT_DB_PORT") != "33330":
        pytest.fail("delivery bill tests require the isolated loopback MySQL on port 33330")


def test_migrations_seed_value_added_types(database):  # noqa: F811 - runs first, before ``repo`` resets settings
    with database._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT value_added_types_json AS v, no_fee_methods_json AS m FROM finance_delivery_bill_settings")
        row = cursor.fetchone()
    loaded = {key: json.loads(value) if isinstance(value, (str, bytes)) else value for key, value in row.items()}
    assert loaded == {"v": ["付上楼费收入", "付卸货费", "付清点费收入"], "m": ["自提"]}


@pytest.fixture
def repo(database):  # noqa: F811
    with database._connection() as connection, connection.cursor() as cursor:
        for statement in ("DELETE FROM finance_delivery_bill_fee_lines", "DELETE FROM finance_delivery_bill_items",
                          "DELETE FROM finance_delivery_fee_rules", "DELETE FROM finance_events",
                          "UPDATE finance_delivery_bill_settings SET lookahead_days=10, include_types_json=JSON_ARRAY(),"
                          " exclude_types_json=JSON_ARRAY(), value_added_types_json=JSON_ARRAY('付上楼费收入', '付卸货费'),"
                          " no_fee_methods_json=JSON_ARRAY('自提'), revision=1 WHERE id=1"):
            cursor.execute(statement)
        connection.commit()
    return FinanceRepository(lambda: database._connection())


def rid() -> str:
    return str(uuid4())


def add_rule(repo, effective="2026-11-01", start="10"):
    return repo.create_delivery_rule({"effective_from": effective, "rounding": "exact", "segments": [
        {"from": "0", "first_weight": "30", "first_price": start, "extra_price": "0.5"},
        {"from": "100", "first_weight": "100", "first_price": "60", "extra_price": "0.4"}]}, request_id=rid(), actor=ACTOR)


def bill(repo, method="派送", unloading="12"):
    def line(kind, money, weight="45.5"):
        return {"BILL_CODE": "R001", "BALANCE_TYPE": kind, "BALANCE_CUR_MONEY": money, "BALANCE_DATE": "2026-11-05",
                "SETTLEMENT_WEIGHT": weight, "PIECE_NUMBER": "2", "BL_DEL": "否"}
    settlements = settlement_entries([line("付派送费", "40.5"), line("派到付款", "-300"), line("付卸货费", unloading),
                                      line("付代收货款手续费", "2")])
    arrivals = [{"tracking_number": "R001", "arrival_date": "2026-11-04", "goods_name": "家具", "delivery_method": method}]
    return build_bill(start=D(2026, 11, 1), end=D(2026, 11, 30), arrivals=arrivals, settlements=settlements,
                      known_arrivals=[], missing_dates=[], settings=repo.delivery_settings(),
                      rules=repo.delivery_rules(), items=repo.delivery_items(["R001"]))


def test_settings_seed_and_revision(repo):
    settings = repo.delivery_settings()
    assert settings["lookahead_days"] == 10 and settings["include_types"] == [] and settings["revision"] == 1
    assert settings["value_added_types"] == ["付上楼费收入", "付卸货费"] and settings["no_fee_methods"] == ["自提"]
    saved = repo.save_delivery_settings({"lookahead_days": 7, "include_types": ["收工单罚款"], "exclude_types": [],
                                         "value_added_types": ["付上楼费收入"], "no_fee_methods": ["自提", "预约自提"],
                                         "expected_revision": 1}, request_id=rid(), actor=ACTOR)
    stored = repo.delivery_settings()
    assert saved["revision"] == 2 and stored["include_types"] == ["收工单罚款"]
    assert stored["value_added_types"] == ["付上楼费收入"] and stored["no_fee_methods"] == ["自提", "预约自提"]
    with pytest.raises(FinanceError) as stale:
        repo.save_delivery_settings({"lookahead_days": 7, "include_types": [], "exclude_types": [],
                                     "expected_revision": 1}, request_id=rid(), actor=ACTOR)
    assert stale.value.code == "DELIVERY_SETTINGS_CHANGED"


def test_rules_unique_active_date_and_retire(repo):
    first = add_rule(repo)
    with pytest.raises(FinanceError) as taken:
        add_rule(repo)
    assert taken.value.code == "DELIVERY_RULE_DATE_TAKEN"
    rules = repo.delivery_rules()
    assert rules[0]["segments"][1] == {"from": "100", "first_weight": "100", "first_price": "60", "extra_price": "0.4"}
    assert rules[0]["content"]["formula"]["type"] == "tiers" and rules[0]["confirmed_count"] == 0
    repo.retire_delivery_rule(first["id"], {"expected_revision": 1}, request_id=rid(), actor=ACTOR)
    assert repo.delivery_rules()[0]["status"] == "retired"
    add_rule(repo)  # the date is free again once the old version is retired
    with pytest.raises(FinanceValidationError):
        repo.create_delivery_rule({"effective_from": "2026-11-02", "rounding": "exact", "segments": [
            {"from": "0", "first_weight": "30", "first_price": "x", "extra_price": "1"}]}, request_id=rid(), actor=ACTOR)


def test_confirm_revoke_round_trip_keeps_settled_value_added(repo):
    add_rule(repo)
    row = bill(repo)["rows"][0]
    assert row["ronghui_amount"] == "54.5000" and row["item_revision"] is None
    assert row["value_added_total"] == "12.0000" and row["other_income"] == "2.0000"
    assert row["payable_total"] == "29.7500"
    request_id = rid()
    confirmed = repo.confirm_delivery_bill(row, request_id=request_id, actor=ACTOR)
    assert confirmed["revision"] == 1 and confirmed["payable_total"] == "29.7500"
    assert repo.confirm_delivery_bill(row, request_id=request_id, actor=ACTOR) == confirmed  # replay, no second write
    with pytest.raises(FinanceError) as twice:
        repo.confirm_delivery_bill({**row, "item_revision": 1}, request_id=rid(), actor=ACTOR)
    assert twice.value.code == "DELIVERY_BILL_CONFIRMED"
    item = repo.delivery_items(["R001"])["R001"]
    assert item["status"] == "confirmed" and item["delivery_fee"] == "17.7500" and item["ronghui_amount"] == "54.5000"
    assert item["value_added_total"] == "12.0000"
    assert repo.delivery_rules()[0]["confirmed_count"] == 1
    with pytest.raises(FinanceError) as in_use:
        repo.retire_delivery_rule(item["delivery_rule_id"], {"expected_revision": 1}, request_id=rid(), actor=ACTOR)
    assert in_use.value.code == "DELIVERY_RULE_IN_USE"
    # 确认后融辉又改了卸货费：确认的增值费不变，并提示融辉金额与确认时不同。
    confirmed_row = bill(repo, unloading="20")["rows"][0]
    assert confirmed_row["status"] == "confirmed" and not confirmed_row["can_confirm"]
    assert confirmed_row["value_added_total"] == "12.0000" and "与确认时" in confirmed_row["problems"][0]
    repo.revoke_delivery_bill("R001", {"expected_revision": 1}, request_id=rid(), actor=ACTOR)
    reopened = repo.delivery_items(["R001"])["R001"]
    assert reopened["status"] == "open" and reopened["delivery_fee"] is None and reopened["value_added_total"] == "0.0000"


def test_self_pickup_confirms_without_delivery_fee(repo):
    row = bill(repo, method="自提")["rows"][0]  # 没有派送费规则也能确认
    assert row["no_delivery_fee"] and row["delivery_fee"] is None and row["can_confirm"]
    assert row["payable_total"] == "12.0000"
    repo.confirm_delivery_bill(row, request_id=rid(), actor=ACTOR)
    item = repo.delivery_items(["R001"])["R001"]
    assert item["status"] == "confirmed" and item["delivery_fee"] is None and item["delivery_rule_id"] is None
    assert item["payable_total"] == "12.0000"
    again = bill(repo, method="自提")["rows"][0]
    assert again["status"] == "confirmed" and again["payable_total"] == "12.0000" and again["delivery_fee"] is None
    # 仓储仍按行口径核对：标成“不算派送费”却带着派送费的行被拒绝。
    repo.revoke_delivery_bill("R001", {"expected_revision": 1}, request_id=rid(), actor=ACTOR)
    add_rule(repo)
    mixed = {**bill(repo)["rows"][0], "no_delivery_fee": True}
    with pytest.raises(FinanceError) as changed:
        repo.confirm_delivery_bill(mixed, request_id=rid(), actor=ACTOR)
    assert changed.value.code == "DELIVERY_BILL_CHANGED"


def test_confirm_refuses_when_rule_changed_after_query(repo):
    add_rule(repo)
    row = bill(repo)["rows"][0]
    add_rule(repo, effective="2026-11-03", start="20")
    with pytest.raises(FinanceError) as changed:
        repo.confirm_delivery_bill(row, request_id=rid(), actor=ACTOR)
    assert changed.value.code == "DELIVERY_RULE_CHANGED"
    fresh = bill(repo)["rows"][0]
    assert fresh["delivery_fee"] == "27.7500"
    result = repo.confirm_delivery_bills([fresh, {**fresh, "tracking_number": "R404", "can_confirm": False,
                                                  "problems": ["不在账单里"]}], request_id=rid(), actor=ACTOR)
    assert result["succeeded"] == 1 and result["failed"] == 1
