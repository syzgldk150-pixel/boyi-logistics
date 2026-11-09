"""Customer-service AI phase 1: advice validation, the Agent service and the case repository (fake model, no database)."""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.ai_tasks import AITaskGateway
from agent.customer_service_ai import CustomerServiceAIService, EvidenceProvider
from shared.customer_service_ai import (
    ADVICE_CASE_THRESHOLD,
    CustomerServiceAIError,
    CustomerServiceAIRepository,
    mask_address,
    mask_phones,
    problem_key,
    problem_needs_reply,
    rank_similar_cases,
    validate_suggestion,
)
from shared.identity_routes import agent_permission

EVIDENCE = "单号 YD4312891203；轨迹 2026-11-08 21:05:00 到达大祥站"
KW = {"scenario_ids": ["urge", "undelivered", "damage"], "evidence_ids": ["E1", "E2"], "knowledge_ids": ["K1"],
      "evidence_text": EVIDENCE}


def _answer(**overrides):
    base = {"scenario": "undelivered", "confidence": 0.86, "evidence_refs": ["E2"], "knowledge_refs": [],
            "reply": "您好，该件 11-08 21:05 已到达大祥站，正在安排派送，请保持电话畅通。", "reason": "到站未派送"}
    return {**base, **overrides}


def test_validation_accepts_a_reply_built_from_the_evidence():
    result = validate_suggestion(_answer(), **KW)
    assert result["confidence"] == "0.86" and result["scenario"] == "undelivered"
    assert validate_suggestion(_answer(reply=None, evidence_refs=[]), **KW)["reply"] is None


@pytest.mark.parametrize("overrides", [
    {"scenario": "made_up"},
    {"confidence": 1.2},
    {"confidence": "high"},
    {"evidence_refs": ["E9"]},
    {"knowledge_refs": ["K2"]},
    {"reason": ""},
    {"reply": "您好，保证今天送到。"},
    {"reply": "您好，该件 11-09 08:30 已在派送。"},
    {"reply": "您好，单号 YD9999999999 已到站。"},
    {"reply": "您好，正在安排派送。", "evidence_refs": []},
    {"reply": "x" * 301},
])
def test_validation_refuses_open_or_unfounded_answers(overrides):
    with pytest.raises(ValueError):
        validate_suggestion(_answer(**overrides), **KW)
    with pytest.raises(ValueError):
        validate_suggestion(["not", "an", "object"], **KW)


def test_masking_and_problem_keys():
    assert mask_phones("电话 13912348826") == "电话 139****8826"
    assert mask_address("大祥区宝庆路12号3栋") == "大祥区宝庆路#号#栋"
    assert problem_key(" s1 ", "e1", "received") == ("s1", "e1", "received")
    with pytest.raises(CustomerServiceAIError):
        problem_key("", "e1", "received")


def test_customer_service_permission_covers_the_ai_routes():
    assert agent_permission("POST", "/internal/v1/admin/customer-service/ai/suggest") == "customer_service"
    assert agent_permission("POST", "/internal/v1/admin/customer-service/ai/cases") == "customer_service"


SCENARIOS = [
    {"scenario_id": "urge", "name": "催件 / 查件", "hint": "h", "tier": "L1", "locked": False, "case_count": 5},
    {"scenario_id": "undelivered", "name": "未派送 / 滞留", "hint": "h", "tier": "L0", "locked": False, "case_count": 1},
    {"scenario_id": "damage", "name": "破损", "hint": "h", "tier": "L1", "locked": True, "case_count": 9},
]
PROBLEM = {"source_id": "s1", "external_id": "e1", "source_direction": "received", "platform": "yunda",
           "waybill_no": "YD4312891203", "status": "待处理", "resolved": False, "problem_type": "物流停滞",
           "problem_text": "两天没更新，电话 13912348826", "reply_text": "", "updated_at": ""}


class FakeRepo:
    def __init__(self, candidates=()):
        self.saved, self.cases, self.suggestions = [], [], {}
        self.candidates = list(candidates)

    def similar_case_candidates(self, problem):
        return [dict(item) for item in self.candidates]

    def scenario_stats(self):
        return [{"scenario_id": "urge", "name": "催件", "tier": "L1", "locked": False, "case_count": 5, "recent_cases": 4,
                 "adopted": 3, "edited": 1, "unused": 0, "adoption_rate": 0.75},
                {"scenario_id": "damage", "name": "破损", "tier": "L0", "locked": True, "case_count": 0, "recent_cases": 0,
                 "adopted": 0, "edited": 0, "unused": 0, "adoption_rate": None}]

    def load_problem(self, key):
        if key != ("s1", "e1", "received"):
            raise CustomerServiceAIError("PROBLEM_NOT_FOUND", "x")
        return dict(PROBLEM)

    def scenarios(self):
        return [dict(row) for row in SCENARIOS]

    def save_suggestion(self, row):
        self.saved.append(row)
        self.suggestions[row["suggestion_id"]] = {**row, "evidence_json": json.dumps(row["evidence"])}

    def get_suggestion(self, suggestion_id):
        return self.suggestions.get(suggestion_id)

    def record_case(self, case):
        self.cases.append(case)
        return {"case_id": len(self.cases), "replayed": False}

    def latest_suggestions(self, keys):
        return {}


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def public_status(self):
        return {"configured": True, "provider": "deepseek", "model": "fake", "config_version_id": 1}

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return {"content": json.dumps(self.answer, ensure_ascii=False)}


class FakeAudit:
    def __init__(self):
        self.outcomes = []

    def record_run(self, run):
        pass

    def record_outcome(self, run_id, *, outcome, actor_id):
        self.outcomes.append((run_id, outcome, actor_id))
        return True


async def _tracking(problem):
    return [{"source": "物流轨迹", "text": "2026-11-08 21:05:00 到达大祥站，电话 13800001111"}]


async def _broken(problem):
    raise RuntimeError("Authorization: Bearer secret")


async def _slow(problem):
    await asyncio.sleep(5)
    return []


def _service(answer, repo=None, audit=None):
    repo = repo or FakeRepo()
    audit = audit or FakeAudit()
    service = CustomerServiceAIService(
        repo, AITaskGateway(FakeLLM(answer), audit),
        evidence_providers=(EvidenceProvider("物流轨迹", _tracking), EvidenceProvider("运单库", _broken),
                            EvidenceProvider("仓库扫描", _slow, timeout_seconds=0.05)),
        audit=audit,
    )
    return service, repo, audit


def test_new_scenario_keeps_the_draft_on_record_but_withholds_it():
    service, repo, _ = _service(_answer())
    result = asyncio.run(service.suggest("s1", "e1", "received", actor_id="admin-1"))
    assert result["scenario"]["tier"] == "L0" and result["reply"] is None and result["draft_withheld"] is True
    assert repo.saved[0]["reply_text"] and repo.saved[0]["scenario_id"] == "undelivered"
    assert result["unavailable"] == ["运单库：暂时无法读取", "仓库扫描：查询超时"]
    assert "13800001111" not in json.dumps(result["evidence"], ensure_ascii=False)
    assert [item["id"] for item in result["evidence"]] == ["E1", "E2"] and result["evidence"][1]["cited"] is True


def test_mature_scenario_shows_the_draft_and_the_model_sees_masked_evidence():
    service, _, _ = _service(_answer(scenario="urge"))
    result = asyncio.run(service.suggest("s1", "e1", "received"))
    assert result["scenario"]["tier"] == "L1" and result["reply"].startswith("您好")
    sent = service._gateway._llm.calls[0][1]["content"]
    assert "13912348826" not in sent and "139****8826" in sent


def test_model_failure_is_mapped_without_saving_advice():
    service, repo, _ = _service({"scenario": "nope"})
    with pytest.raises(CustomerServiceAIError) as refused:
        asyncio.run(service.suggest("s1", "e1", "received"))
    assert refused.value.code == "CS_AI_INVALID" and repo.saved == []


def test_cases_record_how_the_advice_was_used():
    service, repo, audit = _service(_answer(scenario="urge"))
    advice = asyncio.run(service.suggest("s1", "e1", "received"))
    common = {"source_id": "s1", "external_id": "e1", "source_direction": "received", "prob_status": "已处理",
              "suggestion_id": advice["suggestion_id"], "actor_id": "admin-1"}
    adopted = asyncio.run(service.record_case(request_id="r1", reply_text=advice["reply"], **common))
    edited = asyncio.run(service.record_case(request_id="r2", reply_text="您好，已安排派送。", **common))
    manual = asyncio.run(service.record_case(request_id="r3", reply_text="人工回复", **{**common, "suggestion_id": None}))
    assert (adopted["suggestion_use"], edited["suggestion_use"], manual["suggestion_use"]) == ("adopted", "edited", "none")
    assert repo.cases[0]["scenario_id"] == "urge" and repo.cases[2]["scenario_id"] is None
    assert repo.cases[0]["evidence"][0]["id"] == "E1"
    assert [outcome for _, outcome, _ in audit.outcomes] == ["ADOPTED", "EDITED"]
    with pytest.raises(CustomerServiceAIError):
        asyncio.run(service.record_case(request_id="r4", reply_text=" ", **common))


def test_case_for_new_scenario_counts_without_marking_advice_used():
    service, repo, audit = _service(_answer())
    advice = asyncio.run(service.suggest("s1", "e1", "received"))
    result = asyncio.run(service.record_case(request_id="r1", source_id="s1", external_id="e1", source_direction="received",
                                             reply_text="人工回复", prob_status="已处理",
                                             suggestion_id=advice["suggestion_id"], actor_id="a"))
    assert result["suggestion_use"] == "none" and repo.cases[0]["scenario_id"] == "undelivered" and audit.outcomes == []


def test_case_with_a_foreign_suggestion_is_refused():
    service, repo, _ = _service(_answer(scenario="urge"))
    advice = asyncio.run(service.suggest("s1", "e1", "received"))
    repo.suggestions[advice["suggestion_id"]]["external_id"] = "other"
    with pytest.raises(CustomerServiceAIError) as refused:
        asyncio.run(service.record_case(request_id="r1", source_id="s1", external_id="e1", source_direction="received",
                                        reply_text="x", prob_status="", suggestion_id=advice["suggestion_id"], actor_id="a"))
    assert refused.value.code == "SUGGESTION_MISMATCH"


class _Cursor:
    def __init__(self, fetch_rows=()):
        self.executed = []
        self.fetch_rows = list(fetch_rows)
        self.description = [("case_id",), ("source_id",), ("external_id",), ("source_direction",)]
        self.lastrowid = 41

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchall(self):
        rows, self.fetch_rows = self.fetch_rows, []
        return rows

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


CASE = {"request_id": "r1", "source_id": "s1", "external_id": "e1", "source_direction": "received",
        "platform": "yunda", "waybill_no": "YD1", "problem_type": "催件", "problem_text": "t", "scenario_id": "urge",
        "suggestion_id": None, "suggestion_use": "none", "reply_text": "回复", "prob_status": "已处理",
        "evidence": None, "actor_id": "a"}


def test_repository_counts_a_new_case_and_promotes_the_scenario():
    cursor = _Cursor()
    result = CustomerServiceAIRepository(lambda: _Connection(cursor)).record_case(CASE)
    assert result == {"case_id": 41, "replayed": False}
    update_sql, update_params = cursor.executed[-1]
    assert update_sql.startswith("UPDATE cs_scenarios SET case_count=case_count+1")
    assert update_params == ("L0", ADVICE_CASE_THRESHOLD, "L1", "urge")


def test_repository_replays_the_same_request_and_refuses_reuse():
    cursor = _Cursor(fetch_rows=[(7, "s1", "e1", "received")])
    assert CustomerServiceAIRepository(lambda: _Connection(cursor)).record_case(CASE) == {"case_id": 7, "replayed": True}
    assert not any(sql.startswith("INSERT") for sql, _ in cursor.executed)
    cursor = _Cursor(fetch_rows=[(7, "s1", "e2", "received")])
    with pytest.raises(CustomerServiceAIError) as reused:
        CustomerServiceAIRepository(lambda: _Connection(cursor)).record_case(CASE)
    assert reused.value.code == "REQUEST_ID_REUSED"


# ---- Phase 2: similar cases, learning stats, pending rule ----

CANDIDATES = [
    {"case_id": 1, "source_id": "s9", "external_id": "x1", "source_direction": "received", "problem_type": "物流停滞",
     "problem_text": "两天没更新了，件是不是丢了", "reply_text": "您好，已联系分拨核实，今天内答复。", "suggestion_use": "edited",
     "scenario_name": "未派送 / 滞留", "created_at": "2026-11-08 10:00:00"},
    {"case_id": 2, "source_id": "s9", "external_id": "x2", "source_direction": "received", "problem_type": "改址",
     "problem_text": "客户要求改地址到另一个小区", "reply_text": "您好，已登记改址。", "suggestion_use": "none",
     "scenario_name": "改址", "created_at": "2026-11-07 10:00:00"},
    {"case_id": 3, "source_id": "s1", "external_id": "e1", "source_direction": "received", "problem_type": "物流停滞",
     "problem_text": "两天没更新", "reply_text": "同一件的旧回复", "suggestion_use": "none", "created_at": "2026-11-06"},
]


def test_similar_cases_rank_by_text_and_type_and_skip_the_same_problem():
    ranked = rank_similar_cases(PROBLEM, CANDIDATES)
    assert [case["case_id"] for case in ranked] == [1]
    assert ranked[0]["similarity"] >= 0.3


def test_validation_accepts_only_given_case_refs():
    assert validate_suggestion(_answer(case_refs=["C1"]), **{**KW, "case_ids": ["C1"]})["case_refs"] == ["C1"]
    with pytest.raises(ValueError):
        validate_suggestion(_answer(case_refs=["C2"]), **{**KW, "case_ids": ["C1"]})


def test_advice_shows_the_similar_cases_the_model_used_without_their_facts():
    answer = _answer(scenario="urge", case_refs=["C1"])
    service, _, _ = _service(answer, repo=FakeRepo(CANDIDATES))
    result = asyncio.run(service.suggest("s1", "e1", "received"))
    assert [(case["id"], case["cited"]) for case in result["cases"]] == [("C1", True)]
    sent = json.loads(service._gateway._llm.calls[0][1]["content"])
    assert sent["cases"][0]["reply"].startswith("您好，已联系分拨")
    assert "case_id" not in sent["cases"][0] and "别的件" in service._gateway._llm.calls[0][0]["content"]


def test_learning_stats_total_the_adoption_rate():
    service, _, _ = _service(_answer())
    stats = asyncio.run(service.stats())
    assert stats["totals"] == {"cases": 4, "adopted": 3, "edited": 1, "adoption_rate": 0.75}


@pytest.mark.parametrize(("row", "expected"), [
    ({"status": "待处理", "reply_text": "", "resolved": False}, True),
    ({"status": "未回复", "reply_text": "暂无回复", "resolved": False}, True),
    ({"status": "待处理", "reply_text": "已安排派送", "resolved": False}, False),
    ({"status": "已处理", "reply_text": "", "resolved": False}, False),
    ({"status": "待处理", "reply_text": "", "resolved": True}, False),
    ({"status": "", "reply_text": "", "resolved": False}, False),
])
def test_pending_rule_matches_the_page(row, expected):
    assert problem_needs_reply(row) is expected


class _StatsCursor(_Cursor):
    def __init__(self, rows):
        super().__init__(fetch_rows=rows)
        self.description = [(name,) for name in ("scenario_id", "name", "tier", "locked", "case_count", "adopted",
                                                 "edited", "unused", "recent_cases")]


def test_repository_stats_compute_rates_only_when_advice_was_shown():
    cursor = _StatsCursor([("urge", "催件", "L1", 0, 7, 3, 1, 2, 6), ("damage", "破损", "L0", 1, 0, 0, 0, 0, 0)])
    stats = CustomerServiceAIRepository(lambda: _Connection(cursor)).scenario_stats()
    assert stats[0]["adoption_rate"] == 0.75 and stats[1]["adoption_rate"] is None and stats[1]["locked"] is True
    assert "INTERVAL %s DAY" in cursor.executed[0][0] and cursor.executed[0][1] == (30,)


def test_repository_candidate_query_is_bounded_to_the_platform():
    cursor = _Cursor()
    cursor.description = [("case_id",)]
    CustomerServiceAIRepository(lambda: _Connection(cursor)).similar_case_candidates(PROBLEM)
    sql, params = cursor.executed[0]
    assert "c.platform=%s" in sql and params[0] == "yunda" and params[-1] == 60
