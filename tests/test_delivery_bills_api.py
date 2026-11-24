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
UPSTAIRS = {"schema": 1, "group": "上楼卸货",
            "params": [{"key": "floors", "label": "楼层", "type": "number", "integer": True, "min": "1"},
                       {"key": "settlement_weight", "label": "结算重量", "type": "number", "unit": "kg",
                        "prefill": "settlement_weight"}],
            "formula": {"type": "sum", "items": [{"type": "fixed", "amount": "10"},
                                                 {"type": "rate", "rate": "0.1", "of": "settlement_weight"}]}}


class FakeFinance:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.confirmed_rows: list[dict] = []
        self.saved: list[tuple] = []
        content = parse_rule_form({"effective_from": "2026-11-01", "start_price": "10", "base_weight": "30",
                                   "extra_price": "0.5", "rounding": "exact"})["content"]
        self.rules = [{"id": 5, "effective_from": "2026-11-01", "status": "active", "content": content,
                       "start_price": "10.0000", "base_weight": "30.0000", "extra_price": "0.5000", "rounding": "exact",
                       "revision": 1, "note": None, "created_by": "超管", "created_at": "2026-11-24 10:00:00",
                       "confirmed_count": 0}]

    def delivery_settings(self):
        return {"lookahead_days": 10, "include_types": [], "exclude_types": [], "revision": 1}

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

    def save_delivery_fees(self, code, record, *, request_id, actor):
        if self.items.get(code, {}).get("status") == "confirmed":
            raise FinanceError("DELIVERY_BILL_CONFIRMED", "这单已确认。")
        self.saved.append((code, record, actor))
        self.items[code] = {"status": "open", "value_added_total": record["total"], "revision": 1,
                            "lines": record["lines"], "billing_date": record["billing_date"].isoformat()}
        return {"tracking_number": code, "revision": 1, "value_added_total": record["total"]}

    def create_delivery_rule(self, form, *, request_id, actor):
        raise AssertionError("super administrator check must run first")


class FakeHandbook:
    def approved(self):
        return [{"rule_id": "r-up", "fee_kind": "upstairs", "fee_name": "上楼费", "version_label": "V6.1",
                 "status": "APPROVED", "effective_from": "2026-01-01", "content": UPSTAIRS}]


def settlement_rows():
    def row(code, kind, money, weight="45.5"):
        return {"BILL_CODE": code, "BALANCE_TYPE": kind, "BALANCE_CUR_MONEY": money, "BALANCE_DATE": "2026-11-05 08:00:00",
                "SETTLEMENT_WEIGHT": weight, "PIECE_NUMBER": "2", "BL_DEL": "否"}
    return [row("R001", "付派送费", "40.5"), row("R001", "付回单服务费", "3"), row("R002", "付派送费", "18", "20")]


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

    async def waybill_reader(tracking, actor):
        return {"tracking_number": tracking, "billing_date": "2026-11-03", "settlement_weight": "45.5", "pieces": 2,
                "actual_weight": "44"}

    def super_admin(request):
        raise PermissionError("super administrator required")

    app = FastAPI()
    app.include_router(create_delivery_bills_router(
        finance_provider=lambda: finance, fee_rules_provider=FakeHandbook, arrivals_reader=arrivals_reader,
        settlement_reader=settlement_reader, waybill_reader=waybill_reader,
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
    assert bill["summary"]["unsettled_count"] == 1 and bill["rows"][0]["ronghui_amount"] == "43.5000"
    row = bill["rows"][0]
    stale = client.post(f"{BASE}/confirm", json={"request_id": "x", "snapshot_id": bill["snapshot_id"],
                                                  "items": [{"tracking_number": "R001", "fingerprint": "tampered"}]}).json()["data"]
    assert stale["failed"] == 1 and stale["results"][0]["error_code"] == "DELIVERY_BILL_CHANGED" and not finance.confirmed_rows
    done = client.post(f"{BASE}/confirm", json={"request_id": "x", "snapshot_id": bill["snapshot_id"],
                                                "items": [{"tracking_number": "r001", "fingerprint": row["fingerprint"]}]}).json()["data"]
    assert done["succeeded"] == 1 and finance.confirmed_rows[0]["ronghui_amount"] == "43.5000"
    assert finance.confirmed_rows[0]["delivery_rule_id"] == 5
    assert done["bill"]["rows"][0]["status"] == "confirmed" and done["bill"]["snapshot_id"] == bill["snapshot_id"]
    missing = client.post(f"{BASE}/confirm", json={"request_id": "x", "snapshot_id": bill["snapshot_id"],
                                                   "items": [{"tracking_number": "R003", "fingerprint": "f"}]})
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "DELIVERY_BILL_NOT_IN_SNAPSHOT"


def test_fees_need_prepare_then_compute_and_save(setup):
    client, finance, _state = setup
    bill = query(client)
    body = {"snapshot_id": bill["snapshot_id"], "tracking_number": "R001", "fee_kinds": ["upstairs"], "values": {}}
    early = client.post(f"{BASE}/fees/compute", json=body)
    assert early.status_code == 409 and early.json()["error"]["code"] == "DELIVERY_FEE_NOT_PREPARED"
    prep = client.post(f"{BASE}/fees/prepare", json={"snapshot_id": bill["snapshot_id"], "tracking_number": "R001"}).json()["data"]
    assert prep["billing_date"] == "2026-11-03" and [fee["name"] for fee in prep["fees"]] == ["上楼费"]
    assert prep["prefill"] == {"settlement_weight": "45.5"}
    preview = client.post(f"{BASE}/fees/compute", json={**body, "values": prep["prefill"]}).json()["data"]
    assert not preview["complete"] and "楼层" in preview["missing"][0]
    incomplete = client.post(f"{BASE}/fees/save", json={**body, "request_id": "x", "values": prep["prefill"]})
    assert incomplete.status_code == 400 and incomplete.json()["error"]["code"] == "DELIVERY_FEE_INCOMPLETE"
    saved = client.post(f"{BASE}/fees/save", json={**body, "request_id": "x", "values": {**prep["prefill"], "floors": 3}}).json()["data"]
    assert saved["value_added_total"] == "14.5500"
    code, record, actor = finance.saved[0]
    assert code == "R001" and str(record["billing_date"]) == "2026-11-03" and actor.label == "会计"
    assert record["lines"][0]["rule_id"] == "r-up"
    row = saved["bill"]["rows"][0]
    assert row["value_added_total"] == "14.5500" and row["payable_total"] == "32.3000"


def test_super_admin_only_rules_and_failures_are_explained(setup):
    client, _finance, state = setup
    assert client.post(f"{BASE}/rules", json={"request_id": "x", "effective_from": "2026-12-01"}).status_code == 500
    trial = client.post(f"{BASE}/rules/trial", json={"rule": {"start_price": "10", "base_weight": "30", "extra_price": "0.5",
                                                               "rounding": "ceil_kg"}, "settlement_weight": "30.2"}).json()["data"]
    assert trial["amount"] == "10.50"
    rules = client.get(f"{BASE}/rules").json()["data"]["rules"]
    assert "每票 10 元（含 30 公斤）" in rules[0]["description"]
    expired = client.post(f"{BASE}/revoke", json={"request_id": "x", "snapshot_id": "nope", "tracking_number": "R001",
                                                  "expected_revision": 1})
    assert expired.status_code == 409 and expired.json()["error"]["code"] == "DELIVERY_SNAPSHOT_EXPIRED"
    too_long = client.get(f"{BASE}?start=2026-10-01&end=2026-11-30")
    assert too_long.status_code == 400
    state["settlement_error"] = "融辉账号登录已失效"
    failed = client.get(f"{BASE}?start=2026-11-01&end=2026-11-30")
    assert failed.status_code == 502 and "登录已失效" in failed.json()["error"]["message"]
