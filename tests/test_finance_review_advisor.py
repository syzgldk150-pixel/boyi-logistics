"""AI review advice: strict validation and the Agent advisor flow (no database, fake model)."""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.finance_review_advisor import FinanceReviewAdviceError, FinanceReviewAdvisor
from shared.finance.rules.review_ai import validate_review_suggestion

ACCOUNTS = {"540105": {"requires_party": False}, "112202": {"requires_party": True}}
PARTIES = {7: {"name": "融辉"}}


def test_validation_accepts_only_ledger_known_answers():
    ok = validate_review_suggestion({"counter_account": "540105", "party_action": "none", "confidence": 0.9, "reason": "短信成本"},
                                    accounts=ACCOUNTS, parties=PARTIES)
    assert ok == {"counter_account": "540105", "party_action": "none", "party_id": None, "confidence": "0.90", "reason": "短信成本"}
    settle = validate_review_suggestion({"counter_account": "112202", "party_action": "settle_by_waybill", "party_id": 7,
                                         "confidence": "0.7", "reason": "到付回款"}, accounts=ACCOUNTS, parties=PARTIES)
    assert settle["party_id"] == 7
    transfer = validate_review_suggestion({"counter_account": "x", "party_action": "internal_transfer", "confidence": 1, "reason": "划转"},
                                          accounts=ACCOUNTS, parties=PARTIES)
    assert transfer["counter_account"] is None
    for bad in (
        {"counter_account": "9999", "party_action": "none", "confidence": 0.5, "reason": "x"},
        {"counter_account": "112202", "party_action": "none", "confidence": 0.5, "reason": "x"},
        {"counter_account": "112202", "party_action": "party_fixed", "party_id": 99, "confidence": 0.5, "reason": "x"},
        {"counter_account": "540105", "party_action": "party_fixed", "party_id": 7, "confidence": 0.5, "reason": "x"},
        {"counter_account": "540105", "party_action": "none", "confidence": 1.5, "reason": "x"},
        {"counter_account": "540105", "party_action": "none", "confidence": 0.5, "reason": ""},
        {"counter_account": "540105", "party_action": "guess", "confidence": 0.5, "reason": "x"},
        ["not", "an", "object"],
    ):
        with pytest.raises(ValueError):
            validate_review_suggestion(bad, accounts=ACCOUNTS, parties=PARTIES)


class FakeRepo:
    def __init__(self):
        self.saved = None

    def review_evidence(self, review_id):
        return {"review": {"id": review_id, "item_l1": "短信费"}, "candidate_accounts": [], "candidate_parties": [],
                "_accounts": dict(ACCOUNTS), "_parties": dict(PARTIES)}

    def save_review_suggestion(self, review_id, suggestion, *, model):
        self.saved = (review_id, suggestion, model)
        return {"id": review_id, "suggestion": suggestion, "model": model}


class FakeLLM:
    def __init__(self, content, configured=True):
        self.content = content
        self.configured = configured
        self.calls = []

    def public_status(self):
        return {"configured": self.configured, "provider": "deepseek", "model": "fake-model", "config_version_id": 3}

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": self.content}


def test_advisor_stores_a_valid_answer_with_the_active_model():
    repo, llm = FakeRepo(), FakeLLM(json.dumps({"counter_account": "540105", "party_action": "none", "confidence": 0.8, "reason": "短信"}))
    result = asyncio.run(FinanceReviewAdvisor(repo, llm).suggest(5))
    assert result["model"] == "fake-model" and repo.saved[1]["counter_account"] == "540105"
    _, kwargs = llm.calls[0]
    assert kwargs["expected_model"] == "fake-model" and kwargs["response_format"] == {"type": "json_object"}
    assert set(json.loads(llm.calls[0][0][1]["content"])) == {"review", "candidate_accounts", "candidate_parties"}


@pytest.mark.parametrize(("llm", "code"), [
    (FakeLLM("{}", configured=False), "FINANCE_AI_UNAVAILABLE"),
    (FakeLLM("not json"), "FINANCE_AI_INVALID"),
    (FakeLLM(json.dumps({"counter_account": "6666", "party_action": "none", "confidence": 0.8, "reason": "瞎编"})), "FINANCE_AI_INVALID"),
])
def test_advisor_refuses_without_storing(llm, code):
    repo = FakeRepo()
    with pytest.raises(FinanceReviewAdviceError) as refused:
        asyncio.run(FinanceReviewAdvisor(repo, llm).suggest(5))
    assert refused.value.code == code and repo.saved is None
