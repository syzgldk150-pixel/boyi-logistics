"""Agent API for 财务 → 派件账单 (agent/agent/delivery_bills_api.py) with fake providers. 数据虚构。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.delivery_bills_api import create_delivery_bills_router
from shared.finance.delivery_bills import parse_rule_form
from shared.finance.errors import FinanceError

BASE = "/internal/v1/admin/finance/delivery-bills"


class FakeFinance:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.confirmed_rows: list[dict] = []
        segments = [{"from": "0", "first_weight": "30", "first_price": "10", "extra_price": "0.5"}]
        content = parse_rule_form({"effective_from": "2026-11-01", "segments": segments, "rounding": "exact"})["content"]
        self.rules = [{"id": 5, "effective_from": "2026-11-01", "status": "active", "content": content,
                       "segments": segments, "rounding": "exact",
                       "revision": 1, "note": None, "created_by": "超管", "created_at": "2026-11-24 10:00:00",
                       "confirmed_count": 0}]

    def delivery_settings(self):
        return {"lookahead_days": 10, "include_types": [], "exclude_types": [],
                "value_added_types": ["付上楼费收入", "付卸货费"], "no_fee_methods": ["自提"], "revision": 1}

    def delivery_rules(self):
        return [dict(rule) for rule in self.rules]

    def delivery_items(self, codes):
        return {code: dict(self.items[code]) for code in codes if code in self.items}

    def confirm_delivery_bills(self, rows, *, request_id, actor):
        self.confirmed_rows.extend(rows)
        for row in rows:
            self.items[row["tracking_number"]] = {"status": "confirmed", "delivery_fee": row["delivery_fee"],
                                                  "delivery_rule_id": row["delivery_rule_id"], "delivery_steps": [],
                                                  "payable_total": row["payable_total"], "ronghui_amount": row["ronghui_amount"],
                                                  "value_added_total": "0", "revision": 1, "lines": []}
        return {"results": [{"tracking_number": row["tracking_number"], "ok": True} for row in rows],
                "succeeded": len(rows), "failed": 0}

    def create_delivery_rule(self, form, *, request_id, actor):
        raise AssertionError("super administrator check must run first")


def settlement_rows():
    def row(code, kind, money, weight="45.5"):
        return {"BILL_CODE": code, "BALANCE_TYPE": kind, "BALANCE_CUR_MONEY": money, "BALANCE_DATE": "2026-11-05 08:00:00",
                "SETTLEMENT_WEIGHT": weight, "PIECE_NUMBER": "2", "BL_DEL": "否"}
    return [row("R001", "付派送费", "40.5"), row("R001", "付回单服务费", "3"), row("R001", "付上楼费收入", "12"),
            row("R002", "付派送费", "18", "20")]


@pytest.fixture
def setup():
    finance = FakeFinance()
    state = {"settlement_error": None, "reads": []}

    async def settlement_reader(start, end, actor):
        state["reads"].append((start.isoformat(), end.isoformat()))
        if state["settlement_error"]:
            raise FinanceError("DELIVERY_SETTLEMENT_UNAVAILABLE", state["settlement_error"], http_status=502)
        return {"rows": settlement_rows(), "fetched_at": "2026-11-24 12:00:00"}

    def arrivals_reader(start, end, through):
        return {"arrivals": [{"tracking_number": "R001", "arrival_date": "2026-11-04", "goods_name": "家具", "delivery_method": "派送"},
                             {"tracking_number": "R002", "arrival_date": "2026-11-04", "goods_name": "百货", "delivery_method": "自提"},
                             {"tracking_number": "R003", "arrival_date": "2026-11-05", "goods_name": "床垫", "delivery_method": "派送"}],
                "known": ["R001", "R002", "R003"], "missing_dates": []}

    def super_admin(request):
        raise PermissionError("super administrator required")

    app = FastAPI()
    app.include_router(create_delivery_bills_router(
        finance_provider=lambda: finance, arrivals_reader=arrivals_reader, settlement_reader=settlement_reader,
        admin_provider=lambda request: SimpleNamespace(actor_id="7", display_name="会计", roles=("admin",)),
        super_admin_provider=super_admin))
    return TestClient(app, raise_server_exceptions=False), finance, state


def query(client):
    response = client.get(f"{BASE}?start=2026-11-01&end=2026-11-30")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_query_composes_settled_rows_and_confirms_server_rows_only(setup):
    client, finance, state = setup
    bill = query(client)
    assert state["reads"][0][0] == "2026-11-01"  # 结算日期从到货表起始日开始，往后多查 N 天（不超过今天）
    assert [row["tracking_number"] for row in bill["rows"]] == ["R001", "R002"]
    assert bill["summary"]["unsettled_count"] == 1 and bill["rows"][0]["ronghui_amount"] == "55.5000"
    row = bill["rows"][0]
    # 增值费取融辉结算的上楼费；回单服务费是其他收入；自提单 R002 不算派送费、照样能确认。
    assert row["value_added_total"] == "12.0000" and row["other_income"] == "3.0000"
    assert row["payable_total"] == "29.7500"
    pickup = bill["rows"][1]
    assert pickup["no_delivery_fee"] and pickup["delivery_fee"] is None and pickup["can_confirm"]
    assert pickup["payable_total"] == "0.0000"
    stale = client.post(f"{BASE}/confirm", json={"request_id": "x", "snapshot_id": bill["snapshot_id"],
                                                  "items": [{"tracking_number": "R001", "fingerprint": "tampered"}]}).json()["data"]
    assert stale["failed"] == 1 and stale["results"][0]["error_code"] == "DELIVERY_BILL_CHANGED" and not finance.confirmed_rows
    done = client.post(f"{BASE}/confirm", json={"request_id": "x", "snapshot_id": bill["snapshot_id"],
                                                "items": [{"tracking_number": "r001", "fingerprint": row["fingerprint"]}]}).json()["data"]
    assert done["succeeded"] == 1 and finance.confirmed_rows[0]["ronghui_amount"] == "55.5000"
    assert finance.confirmed_rows[0]["delivery_rule_id"] == 5
    assert done["bill"]["rows"][0]["status"] == "confirmed" and done["bill"]["snapshot_id"] == bill["snapshot_id"]
    missing = client.post(f"{BASE}/confirm", json={"request_id": "x", "snapshot_id": bill["snapshot_id"],
                                                   "items": [{"tracking_number": "R003", "fingerprint": "f"}]})
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "DELIVERY_BILL_NOT_IN_SNAPSHOT"


def test_manual_value_added_endpoints_are_gone(setup):
    client, _finance, _state = setup
    bill = query(client)
    for path in ("fees/prepare", "fees/compute", "fees/save"):
        response = client.post(f"{BASE}/{path}", json={"snapshot_id": bill["snapshot_id"], "tracking_number": "R001"})
        assert response.status_code in (404, 405)


def test_super_admin_only_rules_and_failures_are_explained(setup):
    client, _finance, state = setup
    assert client.post(f"{BASE}/rules", json={"request_id": "x", "effective_from": "2026-12-01"}).status_code == 500
    segments = [{"from": "0", "first_weight": "1", "first_price": "5", "extra_price": "1"},
                {"from": "50", "first_weight": "50", "first_price": "30", "extra_price": "0.5"}]
    trial = client.post(f"{BASE}/rules/trial", json={"rule": {"segments": segments, "rounding": "ceil_kg"},
                                                      "settlement_weight": "120.2"}).json()["data"]
    assert trial["amount"] == "65.50"
    assert trial["examples"][1] == [{"weight": "50", "amount": "30.00"}]
    # Without a weight the trial only returns each segment's example amounts (the editor's “算出来” column).
    examples_only = client.post(f"{BASE}/rules/trial", json={"rule": {"segments": segments, "rounding": "exact"}}).json()["data"]
    assert examples_only == {"examples": [[{"weight": "0", "amount": "5.00"}, {"weight": "49", "amount": "53.00"}],
                                          [{"weight": "50", "amount": "30.00"}]]}
    rules = client.get(f"{BASE}/rules").json()["data"]["rules"]
    assert rules[0]["description"] == "0 公斤以上：起步价 10 元（含 30 公斤），超出每公斤 0.5 元；超出部分按实际重量；正好在分界按下一段算"
    assert rules[0]["examples"] == [[{"weight": "0", "amount": "10.00"}]]
    expired = client.post(f"{BASE}/revoke", json={"request_id": "x", "snapshot_id": "nope", "tracking_number": "R001",
                                                  "expected_revision": 1})
    assert expired.status_code == 409 and expired.json()["error"]["code"] == "DELIVERY_SNAPSHOT_EXPIRED"
    too_long = client.get(f"{BASE}?start=2026-10-01&end=2026-11-30")
    assert too_long.status_code == 400
    state["settlement_error"] = "融辉账号登录已失效"
    failed = client.get(f"{BASE}?start=2026-11-01&end=2026-11-30")
    assert failed.status_code == 502 and "登录已失效" in failed.json()["error"]["message"]
