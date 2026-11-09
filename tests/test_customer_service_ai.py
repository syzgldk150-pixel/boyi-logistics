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

    def templates(self, statuses=()):
        return []

    def flag_knowledge_change(self, seen):
        return []

    def drafting_eligible(self, scenario_id):
        return False

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


# ---- Phase 3: templates ----

from shared.customer_service_ai import (  # noqa: E402
    DISTILL_MIN_CASES,
    template_applied,
    validate_reply_template,
    validate_template_draft,
)

TEMPLATE_TEXT = "您好，该件 {到站时间} 已到达 {网点}，今天会安排派送，请保持电话畅通。"


def test_reply_templates_keep_facts_in_slots_and_make_no_promises():
    assert validate_reply_template(TEMPLATE_TEXT)[1] == ["到站时间", "网点"]
    for bad in ("您好，11-08 21:05 已到站。", "您好，保证今天到。", "您好，{网点", "", "x" * 301,
                "".join(f"{{槽{i}}}" for i in range(7))):
        with pytest.raises(ValueError):
            validate_reply_template(bad)


def test_template_applied_checks_the_fixed_wording_in_order():
    assert template_applied("您好，该件 11-08 21:05 已到达大祥站，今天会安排派送，请保持电话畅通。", TEMPLATE_TEXT)
    assert not template_applied("您好，大祥站今天会安排派送。", TEMPLATE_TEXT)


DRAFT = {"consistent": True, "name": "到站未派送", "conditions": ["已到达派送网点", "没有派件扫描"],
         "evidence_needed": ["物流轨迹"], "reply_template": TEMPLATE_TEXT, "action": "回复客户",
         "note": "C4 额外承诺了时间", "consistent_case_refs": ["C1", "C2", "C3"], "knowledge_refs": ["K1"]}
DRAFT_KW = {"case_ids": ["C1", "C2", "C3", "C4"], "knowledge_ids": ["K1"], "evidence_sources": ["问题件", "物流轨迹"]}


def test_template_drafts_need_three_agreeing_cases_and_known_sources():
    assert validate_template_draft(DRAFT, **DRAFT_KW)["slots"] == ["到站时间", "网点"]
    assert validate_template_draft({"consistent": False, "note": "各网点做法不同"}, **DRAFT_KW)["consistent"] is False
    for overrides in ({"consistent_case_refs": ["C1", "C2"]}, {"consistent_case_refs": ["C1", "C2", "C9"]},
                      {"evidence_needed": ["监控录像"]}, {"knowledge_refs": ["K2"]}, {"conditions": []},
                      {"reply_template": "您好，保证送到"}, {"consistent": "yes"}, {"name": ""}):
        with pytest.raises(ValueError):
            validate_template_draft({**DRAFT, **overrides}, **DRAFT_KW)
    with pytest.raises(ValueError):
        validate_template_draft({"consistent": False}, **DRAFT_KW)


ACTIVE = {"template_id": "t-1", "scenario_id": "urge", "version": 2, "status": "active", "name": "到站未派送",
          "conditions": ["已到达派送网点"], "reply_template": TEMPLATE_TEXT}


class TemplateRepo(FakeRepo):
    def __init__(self, templates=(), eligible=True, cases=()):
        super().__init__()
        self._templates = [dict(row) for row in templates]
        self.eligible = eligible
        self.flagged, self.drafts, self.reviews = [], [], []
        self._cases = list(cases)

    def templates(self, statuses=()):
        return [dict(row) for row in self._templates if not statuses or row["status"] in statuses]

    def flag_knowledge_change(self, seen):
        self.flagged.append(dict(seen))
        return []

    def drafting_eligible(self, scenario_id):
        return self.eligible

    def scenario_cases(self, scenario_id, limit=12):
        return [dict(row) for row in self._cases]

    def save_template_draft(self, draft):
        self.drafts.append(draft)

    def review_template(self, template_id, **kwargs):
        self.reviews.append((template_id, kwargs))
        return {"template_id": template_id}

    def cases_by_ids(self, ids):
        return []


def test_reply_following_an_approved_template_is_tier_l2():
    reply = "您好，该件 11-08 21:05 已到达大祥站，今天会安排派送，请保持电话畅通。"
    service, _, _ = _service(_answer(scenario="urge", reply=reply), repo=TemplateRepo([ACTIVE]))
    result = asyncio.run(service.suggest("s1", "e1", "received"))
    assert result["scenario"]["tier"] == "L2" and result["template"]["id"] == "t-1"
    sent = json.loads(service._gateway._llm.calls[0][1]["content"])
    assert sent["templates"][0]["reply_template"] == TEMPLATE_TEXT


def test_reply_ignoring_the_template_is_flagged_and_stays_advice():
    service, _, _ = _service(_answer(scenario="urge"), repo=TemplateRepo([ACTIVE]))
    result = asyncio.run(service.suggest("s1", "e1", "received"))
    assert result["scenario"]["tier"] == "L1" and result["template"] is None and result["template_not_followed"] is True


def test_templates_under_review_are_not_used():
    service, _, _ = _service(_answer(scenario="urge"), repo=TemplateRepo([{**ACTIVE, "status": "review"}]))
    result = asyncio.run(service.suggest("s1", "e1", "received"))
    assert result["template"] is None and result["template_not_followed"] is False
    assert json.loads(service._gateway._llm.calls[0][1]["content"])["templates"] == []


def test_knowledge_edit_times_are_checked_against_templates():
    async def knowledge(problem):
        return [{"source": "知识库 · 规定", "text": "到站 12 小时须说明原因", "document_id": "doc-1", "updated_at": "1731000000"}]

    repo = TemplateRepo()
    audit = FakeAudit()
    service = CustomerServiceAIService(repo, AITaskGateway(FakeLLM(_answer(scenario="urge", reply=None, evidence_refs=[])), audit),
                                       knowledge=EvidenceProvider("飞书知识库", knowledge), audit=audit)
    asyncio.run(service.suggest("s1", "e1", "received"))
    assert repo.flagged == [{"doc-1": "1731000000"}]


CASE_ROWS = [{"case_id": index, "problem_type": "催件", "problem_text": f"什么时候到 {index}",
              "reply_text": "您好，该件已到达大祥站，今天会安排派送。", "suggestion_use": "edited", "created_at": "2026-11-08",
              "knowledge": [{"source": "规定", "document_id": "doc-1", "updated_at": "1", "text": "条款"}]}
             for index in range(1, 6)]


def test_distill_stores_a_draft_from_agreeing_cases():
    answer = {**DRAFT, "consistent_case_refs": ["C1", "C2", "C3"], "evidence_needed": ["物流轨迹"], "knowledge_refs": ["K1"]}
    repo = TemplateRepo(cases=CASE_ROWS)
    service, _, _ = _service(answer, repo=repo)
    result = asyncio.run(service.distill("urge"))
    assert result["drafted"] is True
    draft = repo.drafts[0]
    assert draft["source_case_ids"] == [1, 2, 3] and draft["cases_total"] == 5 and draft["cases_consistent"] == 3
    assert draft["knowledge"] == [{"source": "规定", "url": None, "document_id": "doc-1", "updated_at": "1"}]


def test_distill_keeps_nothing_when_cases_disagree_or_scenario_is_locked():
    repo = TemplateRepo(cases=CASE_ROWS)
    service, _, _ = _service({"consistent": False, "note": "做法不同"}, repo=repo)
    assert asyncio.run(service.distill("urge"))["drafted"] is False and repo.drafts == []
    assert asyncio.run(service.distill("damage"))["reason"] == "scenario_locked_or_unknown"


def test_recording_a_case_schedules_distillation_in_the_background():
    answer = _answer(scenario="urge")
    repo = TemplateRepo(cases=CASE_ROWS)
    service, _, _ = _service(answer, repo=repo)

    async def flow():
        advice = await service.suggest("s1", "e1", "received")
        service._gateway._llm.answer = {**DRAFT, "consistent_case_refs": ["C1", "C2", "C3"], "evidence_needed": ["物流轨迹"],
                                        "knowledge_refs": []}
        await service.record_case(request_id="r1", source_id="s1", external_id="e1", source_direction="received",
                                  reply_text=advice["reply"], prob_status="已处理", suggestion_id=advice["suggestion_id"],
                                  actor_id="a")
        await asyncio.gather(*list(service._background))

    asyncio.run(flow())
    assert len(repo.drafts) == 1


def test_review_validates_edits_before_saving():
    repo = TemplateRepo()
    service, _, _ = _service(_answer(), repo=repo)
    with pytest.raises(CustomerServiceAIError):
        asyncio.run(service.review_template("t-1", action="approve", actor_id="a", reply_template="您好，保证送到"))
    with pytest.raises(CustomerServiceAIError):
        asyncio.run(service.review_template("t-1", action="approve", actor_id="a", conditions=[]))
    asyncio.run(service.review_template("t-1", action="approve", actor_id="a", reply_template=TEMPLATE_TEXT,
                                        conditions=[" 已到站 ", ""]))
    assert repo.reviews[0][1]["conditions"] == ["已到站"]


class _ScriptCursor(_Cursor):
    """Returns queued result sets, one per SELECT."""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if sql.lstrip().startswith("SELECT"):
            names, rows = self.results.pop(0)
            self.description = [(name,) for name in names]
            self.fetch_rows = rows


def _repo(results):
    cursor = _ScriptCursor(results)
    return CustomerServiceAIRepository(lambda: _Connection(cursor)), cursor


@pytest.mark.parametrize(("scenario", "templates", "expected"), [
    ([(0, DISTILL_MIN_CASES)], [], True),
    ([(0, DISTILL_MIN_CASES - 1)], [], False),
    ([(1, 50)], [], False),
    ([(0, 9)], [("draft", None)], False),
    ([(0, 9)], [("rejected", 6)], False),
    ([(0, 11)], [("rejected", 6)], True),
])
def test_drafting_eligibility(scenario, templates, expected):
    repo, _ = _repo([(("locked", "case_count"), scenario), (("status", "rejected_case_count"), templates)])
    assert repo.drafting_eligible("urge") is expected


def test_review_transitions_respect_the_current_state():
    repo, cursor = _repo([(("template_id", "scenario_id", "status", "knowledge_json"), [("t-1", "urge", "active", "[]")])])
    with pytest.raises(CustomerServiceAIError) as changed:
        repo.review_template("t-1", action="approve", actor_id="a")
    assert changed.value.code == "TEMPLATE_STATE_CHANGED"
    with pytest.raises(CustomerServiceAIError):
        repo.review_template("t-1", action="delete", actor_id="a")


def test_flag_knowledge_change_moves_active_templates_to_review():
    knowledge = json.dumps([{"title": "派送规定", "document_id": "doc-1", "updated_at": "100"}])
    repo, cursor = _repo([(("template_id", "knowledge_json"), [("t-1", knowledge), ("t-2", "[]")])])
    assert repo.flag_knowledge_change({"doc-1": "200"}) == ["t-1"]
    update_sql, params = cursor.executed[-1]
    assert update_sql.startswith("UPDATE cs_templates SET status='review'") and "派送规定" in params[1]
    assert json.loads(params[0])[0]["changed_to"] == "200"
