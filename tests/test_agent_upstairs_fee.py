"""C line step C3: the robot computes the upstairs fee for one Ronghui waybill.

Billing date, settlement weight and piece count come from the live waybill;
floor and elevator only from the person's words (asked when missing); only rule
versions approved in 业务手册 → 计算规则 give an amount. The waybill numbers and
remarks here are invented.
"""
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from agent.agent_actions import fee_actions
from agent.agent_actions.business_actions import BusinessActions
from agent.orchestration.models import Actor, ActorType
from shared.handbook_fee_rules import rule_from_record

ROOT = Path(__file__).resolve().parents[1]
DRAFT = json.loads((ROOT / "tests" / "fixtures" / "fee_rules" / "upstairs_v61_draft.json").read_text(encoding="utf-8"))
APPROVED = {**DRAFT, "rule_id": "r1", "status": "APPROVED", "version_label": "V6.1", "effective_from": "2026-10-01"}
ACTOR = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin", "super_admin"), authenticated_by="feishu_admin_binding")
CODE = "R00099990001"


def row(**changes):
    base = {"requested_bill_code": CODE, "tracking_number": CODE, "billing_date": "2026-11-12 16:27:26",
            "settlement_weight": 195, "actual_weight": 195, "quantity": 1, "upstairs_service": 0,
            "remark": "客户要求拆托上楼，费用客户自行承担"}
    base.update(changes)
    return base


class Platform:
    def __init__(self, rows=None, fail=None):
        self.calls, self.rows, self.fail = [], rows if rows is not None else [row()], fail

    def __call__(self, operation, params, principal, request_id):
        self.calls.append((operation, dict(params)))
        assert operation == "query_waybill_detail"
        if self.fail:
            return {"ok": False, "error_code": "AUTH_REQUIRED", "error": self.fail}
        return {"ok": True, "data": {"ok": True, "data": deepcopy(self.rows)}}


def _turn(platform, rules=(APPROVED,)):
    actions = BusinessActions(invoke=platform, cs_accounts=lambda: [], fee_rules=lambda kind: list(rules),
                              today=lambda: date(2026, 11, 16))
    return actions.turn(ACTOR)


def ask(turn, text, tracking=None):
    arguments = {"tracking_number": tracking} if tracking else {}
    return turn.execute("biz_upstairs_fee", arguments, message=text)


@pytest.mark.parametrize("text,floor", [("5楼", 5), ("送到5层", 5), ("五楼", 5), ("十二楼", 12), ("二十层", 20),
                                        ("R00099990001", None), ("3楼还是5楼", None)])
def test_floor_is_read_from_the_persons_words(text, floor):
    assert fee_actions.parse_floor(text) == floor


@pytest.mark.parametrize("text,elevator", [("无电梯", False), ("没有电梯", False), ("步梯", False), ("有电梯", True),
                                           ("电梯房", True), ("5楼", None), ("有电梯吗没电梯", None)])
def test_elevator_is_read_from_the_persons_words(text, elevator):
    assert fee_actions.parse_elevator(text) is elevator


def test_full_question_gives_the_amount_with_its_basis_and_the_remark_reminder():
    platform = Platform()
    text, decision = ask(_turn(platform), f"{CODE} 送5楼 无电梯 上楼费多少", CODE)
    assert decision.decision == "EXECUTE" and decision.action_id == "fee.upstairs"
    assert platform.calls == [("query_waybill_detail", {"bill_codes": [CODE], "decrypt_masked": False})]
    # 195 kg, one piece of 195 kg (over the 120 kg limit) -> negotiate, no fixed amount
    assert "上楼费：需协商" in text and "单件超过 120 公斤" in text
    assert "开单日期 2026-11-12" in text and "结算重量 195 公斤" in text and "件数 1" in text
    assert "规则：V6.1（执行日期 2026-10-01）" in text
    assert "单上没有勾选上楼服务，但备注写了" in text


def test_missing_floor_and_elevator_are_asked_then_the_same_waybill_is_used():
    platform = Platform(rows=[row(actual_weight=80, settlement_weight=80)])
    turn = _turn(platform)
    text, decision = ask(turn, f"算一下 {CODE} 的上楼费", CODE)
    assert decision.decision == "CLARIFY" and "送到几楼、有没有电梯" in text
    text, decision = ask(turn, "5楼")
    assert decision.decision == "CLARIFY" and "有没有电梯" in text and "送到几楼" not in text
    text, decision = ask(turn, "没电梯")
    assert decision.decision == "EXECUTE"
    # base 10 + 0.12×80 = 19.6, floor 5: 0.10×80 = 8, one piece 50–100 kg: 10 → 37.60
    assert "上楼费：37.60 元" in text
    text, _ = ask(turn, "那3楼呢")
    assert "上楼费：33.60 元" in text  # 19.6 + 0.05×80 + 10
    assert len(platform.calls) == 1  # the waybill is read once for the thread


def test_no_approved_rule_for_the_billing_date_gives_no_amount():
    text, decision = ask(_turn(Platform(), rules=()), f"{CODE} 5楼 有电梯", CODE)
    assert decision.decision == "EXECUTE"
    assert "没有已审核生效的上楼费规则，不能给出金额" in text and "元" not in text.split("\n")[-1]
    later = {**APPROVED, "effective_from": "2026-12-01"}
    text, _ = ask(_turn(Platform(), rules=(later,)), f"{CODE} 5楼 有电梯", CODE)
    assert "不能给出金额" in text  # billing date 2026-11-12 is before that version


def test_multi_piece_surcharge_is_only_decided_when_certain():
    rule = rule_from_record(APPROVED)
    facts = fee_actions.waybill_facts(CODE, {"data": [row(quantity=17, actual_weight=195, settlement_weight=195)]})
    counts, over, note = fee_actions.piece_counts(rule, facts)
    assert (counts, over) == ((0, 0), 0) and "平均每件约 11.5 公斤，按没有单件超过 50 公斤计算" in note
    heavy = fee_actions.waybill_facts(CODE, {"data": [row(quantity=2, actual_weight=150, settlement_weight=150)]})
    assert "以下金额不含单件加收" in fee_actions.piece_counts(rule, heavy)[2]
    light = fee_actions.waybill_facts(CODE, {"data": [row(quantity=3, actual_weight=40, settlement_weight=40)]})
    assert fee_actions.piece_counts(rule, light) == ((0, 0), 0, "")
    single = fee_actions.waybill_facts(CODE, {"data": [row(quantity=1, actual_weight=Decimal("100"))]})
    assert fee_actions.piece_counts(rule, single) == ((1, 0), 0, "")  # 100 kg is in the (50, 100] tier


def test_ticked_upstairs_service_has_no_remark_reminder():
    facts = fee_actions.waybill_facts(CODE, {"data": [row(upstairs_service=1)]})
    assert not any("没有勾选" in item for item in fee_actions.reminders(facts))


@pytest.mark.parametrize("changes,message", [
    ({"billing_date": ""}, "没有读到开单日期"),
    ({"settlement_weight": None}, "没有读到结算重量"),
    ({"quantity": 0}, "没有读到件数"),
    ({"tracking_number": "R00099990002", "requested_bill_code": "R00099990002"}, "没有查到运单"),
])
def test_missing_waybill_facts_stop_the_calculation(changes, message):
    text, decision = ask(_turn(Platform(rows=[row(**changes)])), f"{CODE} 5楼 无电梯", CODE)
    assert decision.decision == "REJECT" and message in text


def test_platform_failure_and_other_platforms_are_reported_plainly():
    text, decision = ask(_turn(Platform(fail="融辉登录已失效")), f"{CODE} 5楼 无电梯", CODE)
    assert decision.decision == "REJECT" and "融辉登录已失效" in text
    yunda = "4312345678901"
    text, decision = ask(_turn(Platform()), f"{yunda} 5楼 无电梯", yunda)
    assert decision.decision in {"REJECT", "CLARIFY"} and "元" not in text


def test_without_a_waybill_the_person_is_asked_for_one():
    text, decision = ask(_turn(Platform()), "上楼费怎么算")
    assert decision.decision == "CLARIFY" and "运单号" in text
