"""Shared AI task gateway and its audit repository (no database, fake model)."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from agent.ai_tasks import AITaskGateway
from shared.ai_tasks import AITaskError, AITaskSpec
from shared.ai_tasks.audit import AITaskAuditRepository


def _validate(answer):
    if not isinstance(answer, dict) or answer.get("label") not in {"a", "b"}:
        raise ValueError("label must be a or b")
    return {"label": answer["label"]}


SPEC = AITaskSpec(task_id="test.pick_label", prompt_version="1", system_prompt="pick a label", validate=_validate)


class FakeLLM:
    def __init__(self, *contents, configured=True, error=None):
        self.contents = list(contents)
        self.configured = configured
        self.error = error
        self.calls = []

    def public_status(self):
        return {"configured": self.configured, "provider": "deepseek", "model": "fake-model", "config_version_id": 9}

    async def chat(self, messages, **kwargs):
        self.calls.append(([dict(m) for m in messages], kwargs))
        if self.error is not None:
            raise self.error
        return {"content": self.contents.pop(0)}


class FakeAudit:
    def __init__(self, fail=False):
        self.runs = []
        self.fail = fail

    def record_run(self, run):
        if self.fail:
            raise RuntimeError("db down")
        self.runs.append(run)


def _run(gateway, payload=None, spec=SPEC):
    return asyncio.run(gateway.run(spec, payload or {"q": "x"}, subject_ref="test:1", actor_id="admin-1"))


def test_success_is_audited_and_uses_the_active_model_snapshot():
    llm, audit = FakeLLM(json.dumps({"label": "a"})), FakeAudit()
    result = _run(AITaskGateway(llm, audit))
    assert result.result == {"label": "a"} and result.model == "fake-model" and result.attempts == 1
    _, kwargs = llm.calls[0]
    assert kwargs == {"provider": "deepseek", "expected_model": "fake-model", "expected_config_version_id": 9,
                      "response_format": {"type": "json_object"}}
    (run,) = audit.runs
    assert run["status"] == "SUCCEEDED" and run["run_id"] == result.run_id and run["result"] == {"label": "a"}
    assert run["task_id"] == "test.pick_label" and run["subject_ref"] == "test:1" and run["actor_id"] == "admin-1"


def test_payload_is_redacted_before_the_model_and_only_hashed_in_the_audit():
    llm, audit = FakeLLM(json.dumps({"label": "b"})), FakeAudit()
    _run(AITaskGateway(llm, audit), {"note": "x", "api_key": "sk-live", "nested": {"password": "p"}})
    sent = llm.calls[0][0][1]["content"]
    assert "sk-live" not in sent and '"p"' not in sent and json.loads(sent)["note"] == "x"
    assert audit.runs[0]["input_sha256"] == hashlib.sha256(sent.encode("utf-8")).hexdigest()
    assert "input" not in audit.runs[0]


def test_invalid_answer_is_retried_with_the_error_then_accepted():
    llm, audit = FakeLLM("not json", json.dumps({"label": "b"})), FakeAudit()
    result = _run(AITaskGateway(llm, audit))
    assert result.attempts == 2 and result.result == {"label": "b"}
    retry_messages = llm.calls[1][0]
    assert retry_messages[-2] == {"role": "assistant", "content": "not json"}
    assert "不符合要求" in retry_messages[-1]["content"]
    assert audit.runs[0]["attempts"] == 2 and audit.runs[0]["status"] == "SUCCEEDED"


def test_answers_that_keep_failing_validation_are_refused_and_audited():
    llm, audit = FakeLLM(json.dumps({"label": "z"}), json.dumps({"label": "y"})), FakeAudit()
    with pytest.raises(AITaskError) as refused:
        _run(AITaskGateway(llm, audit))
    assert refused.value.code == "AI_INVALID" and "label must be" in refused.value.detail
    assert len(llm.calls) == 2 and audit.runs[0]["status"] == "INVALID" and audit.runs[0].get("result") is None


def test_provider_errors_are_not_retried():
    llm, audit = FakeLLM(error=RuntimeError("Authorization: Bearer abc")), FakeAudit()
    with pytest.raises(AITaskError) as failed:
        _run(AITaskGateway(llm, audit))
    assert failed.value.code == "AI_FAILED" and len(llm.calls) == 1
    assert audit.runs[0]["status"] == "FAILED" and "abc" not in audit.runs[0]["error_message"]


def test_unconfigured_model_is_unavailable_without_a_call():
    llm, audit = FakeLLM(configured=False), FakeAudit()
    with pytest.raises(AITaskError) as unavailable:
        _run(AITaskGateway(llm, audit))
    assert unavailable.value.code == "AI_UNAVAILABLE" and llm.calls == []
    assert audit.runs[0]["status"] == "UNAVAILABLE"


def test_success_is_not_returned_when_its_audit_cannot_be_stored():
    with pytest.raises(AITaskError) as unaudited:
        _run(AITaskGateway(FakeLLM(json.dumps({"label": "a"})), FakeAudit(fail=True)))
    assert unaudited.value.code == "AI_AUDIT_FAILED"


def test_failure_paths_still_raise_their_own_error_when_audit_is_down():
    with pytest.raises(AITaskError) as failed:
        _run(AITaskGateway(FakeLLM(error=RuntimeError("x")), FakeAudit(fail=True)))
    assert failed.value.code == "AI_FAILED"


def test_model_override_and_bad_specs_are_refused():
    override = AITaskSpec(task_id="test.pick_label", prompt_version="1", system_prompt="p", validate=_validate,
                          model_override="glm-4-flash")
    with pytest.raises(AITaskError) as refused:
        _run(AITaskGateway(FakeLLM()), spec=override)
    assert refused.value.code == "AI_MODEL_OVERRIDE_UNSUPPORTED"
    for kwargs in ({"task_id": "Bad Id"}, {"system_prompt": " "}, {"max_retries": 3}, {"timeout_seconds": 0}):
        base = {"task_id": "test.ok", "prompt_version": "1", "system_prompt": "p", "validate": _validate}
        with pytest.raises((ValueError, TypeError)):
            AITaskSpec(**{**base, **kwargs})


class _Cursor:
    def __init__(self, rowcount=1):
        self.executed = []
        self.rowcount = rowcount

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_audit_repository_writes_one_row_and_records_outcomes():
    cursor = _Cursor()
    connection = _Connection(cursor)
    repo = AITaskAuditRepository(lambda: connection)
    repo.record_run({"run_id": "r1", "task_id": "test.pick_label", "prompt_version": "1", "subject_ref": "test:1",
                     "status": "SUCCEEDED", "result": {"label": "a"}, "output_text": "x" * 30000, "attempts": 1})
    sql, params = cursor.executed[0]
    assert sql.startswith("INSERT INTO ai_task_runs") and params[0] == "r1"
    assert json.loads(params[10]) == {"label": "a"} and len(params[9]) == 20000
    assert connection.committed and connection.closed
    assert repo.record_outcome("r1", outcome="EDITED", actor_id="admin-1") is True
    assert "outcome IS NULL" in cursor.executed[1][0]
    with pytest.raises(ValueError):
        repo.record_run({"run_id": "r2", "task_id": "t", "prompt_version": "1", "subject_ref": "s", "status": "DONE"})
    with pytest.raises(ValueError):
        repo.record_outcome("r1", outcome="LIKED", actor_id="a")
