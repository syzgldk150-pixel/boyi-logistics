"""Agent layer P2: the business handbook — compile rules, approval, runtime effect.

Compiled content may only refer to existing actions and plugins; only an
approved version of an active note reaches conversations; editing pauses it;
unconditional rules are enforced by code, conditional ones only instruct.
A compiled version is submitted for review once its ambiguities are answered
and its examples were trial-run; development requirements never block.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from agent.agent_actions.gateway import ActionGateway
from agent.agent_actions.spec import ActionSpec
from agent.agent_actions.trial import HandbookTrial
from agent.handbook import HandbookService
from shared.agent_handbook import (
    HandbookCatalog, HandbookError, HandbookSnapshot, ambiguity_items, approval_blockers, build_snapshot,
    describe_compiled, normalize_answers, submit_blockers,
    validate_compiled,
)
from shared.ai_tasks import AITaskError, AITaskResult

CATALOG = HandbookCatalog(actions={"waybill.lookup": "查询运单信息", "shipment.query": "查询发货吨位"},
                          plugins={"plugin:quote": "三平台报价", "plugin:list": "到货清单"})
COMPILED = {
    "restatement": "说“岳麓店”指长沙岳麓区网点；“问个价”要三平台报价；到货清单先问再做。",
    "aliases": [{"term": "岳麓店", "kind": "text", "target": "长沙岳麓区测试网点"},
                {"term": "比价", "kind": "plugin", "target": "plugin:quote"}],
    "examples": [{"utterance": "问个价 测试市 50 公斤", "action": "plugin:quote"}],
    "rules": [{"type": "ask_first", "action": "plugin:list", "condition": "", "message": "到货清单同步前先确认"},
              {"type": "forbid", "action": "*", "condition": "下午三点前", "message": "三点前不要同步"}],
    "ambiguities": ["“下午”是否包含节假日未写明"],
    "unsupported": [],
}


# -- content rules ---------------------------------------------------------------------

def test_valid_content_is_normalized_and_described():
    compiled = validate_compiled(json.loads(json.dumps(COMPILED)), CATALOG)
    assert compiled["aliases"][0] == {"term": "岳麓店", "kind": "text", "target": "长沙岳麓区测试网点"}
    lines = describe_compiled(compiled, CATALOG)
    assert any("系统无法核对" in line for line in lines) and any("插件已核对" in line for line in lines)
    assert any("由系统强制" in line for line in lines) and any("由 AI 判断" in line for line in lines)


@pytest.mark.parametrize("patch,message", [
    ({"aliases": [{"term": "x", "kind": "plugin", "target": "plugin:missing"}]}, "插件不存在"),
    ({"examples": [{"utterance": "删库", "action": "db.drop"}]}, "不存在的动作"),
    ({"rules": [{"type": "forbid", "action": "*", "condition": "", "message": "全禁"}]}, "必须写明条件"),
    ({"rules": [{"type": "ask_first", "action": "waybill.lookup", "condition": "", "message": "先问"}]}, "只能用于插件"),
    ({"aliases": [{"term": "a", "kind": "text", "target": "b"}, {"term": "a", "kind": "text", "target": "c"}]}, "重复"),
    ({"extra": 1}, "未定义的字段"),
])
def test_content_referring_to_unknown_things_is_rejected(patch, message):
    with pytest.raises(ValueError, match=message):
        validate_compiled({**COMPILED, **patch}, CATALOG)


def test_submit_and_approval_need_answers_and_a_trial_but_not_unsupported():
    compiled = validate_compiled(COMPILED, CATALOG)
    version = {"version": 2, "status": "COMPILED", "compiled": compiled, "validation": {"ok": True}, "acknowledged": []}
    trial = {"rows": [{"pass": True}]}
    assert submit_blockers(version, latest_version=2) == ["还差 1 项歧义确认", "请先试跑说法样例"]
    ready = {**version, "acknowledged": [{"index": 0, "answer": "包含", "by": "9", "at": ""}], "trial": trial}
    assert submit_blockers(ready, latest_version=2) == []
    assert "只能提交最新版本" in submit_blockers(ready, latest_version=3)
    unsupported = {**ready, "compiled": {**compiled, "unsupported": ["自动给客户打电话"]}}
    assert submit_blockers(unsupported, latest_version=2) == []  # 开发需求不阻塞 (spec 52)
    assert approval_blockers(ready, latest_version=2) == ["这个版本还没有提交审核"]
    assert approval_blockers({**ready, "status": "PENDING"}, latest_version=2) == []
    legacy = {**ready, "acknowledged": [0], "compiled": {**compiled, "ambiguities": ["旧的字符串歧义"]}}
    assert submit_blockers(legacy, latest_version=2) == []  # versions compiled before options existed


def test_ambiguities_carry_candidate_answers_and_answers_are_checked():
    compiled = validate_compiled({**COMPILED, "ambiguities": [
        {"question": "“下午”是否包含节假日？", "options": ["包含", "不包含", "包含"]}, "旧格式歧义"]}, CATALOG)
    assert compiled["ambiguities"] == [{"question": "“下午”是否包含节假日？", "options": ["包含", "不包含"]},
                                       {"question": "旧格式歧义", "options": []}]
    items = ambiguity_items(compiled)
    assert normalize_answers([{"index": 0, "answer": "不包含"}, {"index": 1, "answer": ""}], items) == [(0, "不包含"), (1, "")]
    with pytest.raises(HandbookError, match="选择或填写"):
        normalize_answers([{"index": 0, "answer": " "}], items)
    with pytest.raises(HandbookError, match="编号"):
        normalize_answers([{"index": 5, "answer": "x"}], items)


# -- runtime effect --------------------------------------------------------------------

SNAPSHOT = build_snapshot([("e1", 1, validate_compiled(COMPILED, CATALOG))])


def test_snapshot_expands_only_terms_the_person_used_and_instructs_for_visible_actions():
    assert SNAPSHOT.expand_message("查一下物流") == "查一下物流"
    expanded = SNAPSHOT.expand_message("岳麓店今天发了多少", titles={})
    assert expanded.endswith("（业务手册：岳麓店=长沙岳麓区测试网点）")
    section = SNAPSHOT.prompt_section({"plugin:quote": "三平台报价"})
    assert "“问个价 测试市 50 公斤”应选择「三平台报价」" in section
    assert "三点前" in section and "不能扩大权限" in section
    assert "到货清单同步前先确认" not in section  # unconditional rules are enforced by code, not prompted


def test_later_entries_never_override_an_earlier_term():
    other = {**COMPILED, "aliases": [{"term": "岳麓店", "kind": "text", "target": "别的网点"}]}
    merged = build_snapshot([("e1", 1, COMPILED), ("e2", 1, other)])
    assert [item["target"] for item in merged.aliases if item["term"] == "岳麓店"] == ["长沙岳麓区测试网点"]


def test_gateway_enforces_unconditional_rules():
    gateway = ActionGateway()
    listing = ActionSpec(action_id="plugin:list", title="到货清单", effect="internal_projection_write", capability="plugins.execute")
    decision = gateway.decide(listing, actor=None, arguments={}, message="同步一下到货清单", handbook=SNAPSHOT)
    assert decision.decision == "CONFIRM" and decision.reason == "到货清单同步前先确认"
    forbid = build_snapshot([("e", 1, {**COMPILED, "rules": [
        {"type": "forbid", "action": "plugin:list", "condition": "", "message": "到货清单只在后台手动同步"}]})])
    decision = gateway.decide(listing, actor=None, arguments={}, message="同步一下到货清单", handbook=forbid)
    assert decision.decision == "REJECT" and decision.reason == "到货清单只在后台手动同步"


def test_alias_lets_grounding_accept_the_approved_meaning():
    quote = ActionSpec(action_id="plugin:quote", title="三平台报价", effect="read", capability="plugins.execute",
                       input_schema={"type": "object", "required": ["address", "weight"], "properties": {
                           "address": {"type": "string"}, "weight": {"type": "number"}}})
    message = "报个价 岳麓店 50 公斤"
    plain = ActionGateway().decide(quote, actor=None, arguments={"address": "长沙岳麓区测试网点", "weight": 50}, message=message)
    assert plain.decision == "CLARIFY"
    expanded = SNAPSHOT.expand_message(message)
    assert ActionGateway().decide(quote, actor=None, arguments={"address": "长沙岳麓区测试网点", "weight": 50},
                                  message=expanded).decision == "EXECUTE"


# -- service flow with in-memory fakes --------------------------------------------------

class MemoryRepository:
    def __init__(self):
        self.entries, self.versions, self.events = {}, {}, []

    def create_entry(self, *, title, body, actor_id):
        entry_id = f"e{len(self.entries) + 1}"
        self.entries[entry_id] = {"entry_id": entry_id, "title": title, "status": "DRAFT", "latest_version": 1,
                                  "active_version": None, "row_version": 1, "created_by": actor_id}
        self.versions[(entry_id, 1)] = {"version": 1, "body": body, "status": "DRAFT", "compiled": None,
                                        "validation": None, "trial": None, "acknowledged": [], "edited_by": actor_id}
        return entry_id

    def get_entry(self, entry_id):
        entry = dict(self.entries[entry_id])
        entry["versions"] = [dict(v) for (eid, _), v in sorted(self.versions.items(), reverse=True) if eid == entry_id]
        entry["events"] = [dict(item) for item in self.events if item["entry_id"] == entry_id]
        return entry

    def list_entries(self, *, include_retired=False):
        return [dict(e) for e in self.entries.values() if include_retired or e["status"] != "RETIRED"]

    def display_names(self, actor_ids):
        return {str(item): f"同事{item}" for item in actor_ids if str(item).isdigit()}

    def maintainer_open_ids(self, actor_ids):
        return [f"ou_{item}" for item in sorted(set(actor_ids))]

    def _event(self, entry_id, version, action, actor_id):
        self.events.append({"entry_id": entry_id, "version": version, "action": action, "actor_id": actor_id})

    def save_draft(self, entry_id, *, title, body, actor_id, expected_row_version):
        entry = self.entries[entry_id]
        latest = self.versions[(entry_id, entry["latest_version"])]
        if latest["status"] == "PENDING":
            raise HandbookError("HANDBOOK_CONFLICT", "这个版本正在审核")
        version = entry["latest_version"] if latest["status"] in {"DRAFT", "COMPILED"} else entry["latest_version"] + 1
        self.versions[(entry_id, version)] = {"version": version, "body": body, "status": "DRAFT", "compiled": None,
                                              "validation": None, "trial": None, "acknowledged": [], "edited_by": actor_id}
        entry.update(latest_version=version, title=title, row_version=entry["row_version"] + 1,
                     status="PAUSED" if entry["status"] == "ACTIVE" else entry["status"])
        return version

    def record_compile(self, entry_id, version, *, compiled, validation, run_id, actor_id, seconds=None):
        self.versions[(entry_id, version)].update(compiled=compiled, validation=validation,
                                                  status="COMPILED" if validation["ok"] else "DRAFT")
        self._event(entry_id, version, "COMPILE", actor_id)

    def record_trial(self, entry_id, version, trial, *, actor_id="system"):
        self.versions[(entry_id, version)]["trial"] = trial

    def acknowledge(self, entry_id, version, answers, *, actor_id="system", at=""):
        self.versions[(entry_id, version)]["acknowledged"] = [
            {"index": index, "answer": answer, "by": actor_id, "at": at} for index, answer in answers]

    def submit(self, entry_id, version, *, actor_id, blockers):
        problems = blockers(self.versions[(entry_id, version)], self.entries[entry_id]["latest_version"])
        if problems:
            raise HandbookError("HANDBOOK_NOT_SUBMITTABLE", "；".join(problems))
        self.versions[(entry_id, version)]["status"] = "PENDING"
        self._event(entry_id, version, "SUBMIT", actor_id)

    def send_back(self, entry_id, version, *, actor_id, reason="", action="WITHDRAW"):
        self.versions[(entry_id, version)]["status"] = "COMPILED"
        self._event(entry_id, version, action, actor_id)

    def discard(self, entry_id, version, *, actor_id):
        self.versions[(entry_id, version)]["status"] = "VOID"
        entry = self.entries[entry_id]
        entry["status"] = entry["status"] if entry["active_version"] is not None else "RETIRED"
        return entry["status"]

    def approve(self, entry_id, version, *, actor_id, blockers):
        entry = self.entries[entry_id]
        problems = blockers(self.versions[(entry_id, version)], entry["latest_version"])
        if problems:
            raise HandbookError("HANDBOOK_NOT_APPROVABLE", "；".join(problems))
        self.versions[(entry_id, version)]["status"] = "ACTIVE"
        entry.update(status="ACTIVE", active_version=version)

    def active_compiled(self):
        return [(eid, e["active_version"], self.versions[(eid, e["active_version"])]["compiled"])
                for eid, e in self.entries.items() if e["status"] == "ACTIVE"]


class FakeGateway:
    def __init__(self, answer):
        self.answer, self.payloads = answer, []

    async def run(self, spec, payload, *, subject_ref, actor_id=None):
        self.payloads.append(payload)
        try:
            result = spec.validate(self.answer)
        except ValueError as exc:
            raise AITaskError("AI_INVALID", "AI 返回的结果不符合要求，未保存。", run_id="r-bad", detail=str(exc)) from exc
        return AITaskResult(run_id="r-1", result=result, provider="p", model="m", config_version_id=1, attempts=1)


async def _passing_trial(examples, candidate, actor):
    rows = [{"utterance": item["utterance"], "expected": item["action"], "after": item["action"], "pass": True}
            for item in examples]
    return {"cases": len(rows), "passed": len(rows), "improved": 0, "rows": rows}


def _service(answer, trial=_passing_trial, notices=None):
    repo = MemoryRepository()
    notify = (lambda open_ids, text: notices.append((open_ids, text))) if notices is not None else None
    return HandbookService(repo, FakeGateway(answer), catalog_provider=lambda actor: CATALOG, trial_runner=trial,
                           notify_maintainers=notify), repo


def _ready(service, entry_id):
    """Compile, answer the ambiguity and trial-run: what the editor does before submitting."""
    actor = SimpleNamespace(actor_id="9")
    asyncio.run(service.compile(entry_id, actor=actor))
    service.acknowledge(entry_id, 1, answers=[{"index": 0, "answer": "不包含节假日"}], actor_id="9")
    asyncio.run(service.trial(entry_id, actor=actor))


def test_compile_submit_approve_edit_pause_cycle():
    notices = []
    service, repo = _service(COMPILED, notices=notices)
    entry_id = service.create(title="网点简称", body="大家说岳麓店就是长沙岳麓区测试网点", actor_id="9")
    result = asyncio.run(service.compile(entry_id, actor=SimpleNamespace(actor_id="9")))
    assert result["validation"]["ok"] and service.snapshot().empty and "seconds" in result
    assert service._gateway.payloads[0]["plugins"][0] == {"id": "plugin:list", "title": "到货清单"}
    with pytest.raises(HandbookError, match="歧义"):
        service.submit(entry_id, 1, actor_id="9")
    service.acknowledge(entry_id, 1, answers=[{"index": 0, "answer": "不包含节假日"}], actor_id="9")
    with pytest.raises(HandbookError, match="试跑"):
        service.submit(entry_id, 1, actor_id="9")
    asyncio.run(service.trial(entry_id, actor=SimpleNamespace(actor_id="9")))
    with pytest.raises(HandbookError, match="提交审核"):
        service.approve(entry_id, 1, actor_id="1")  # super admins approve submitted versions only
    service.submit(entry_id, 1, actor_id="9")
    detail = service.get_entry(entry_id, actor=None)
    assert detail["versions"][0]["status"] == "PENDING" and detail["versions"][0]["blockers"] == []
    assert detail["versions"][0]["acknowledged"][0]["answer"] == "不包含节假日" and detail["people"]["9"] == "同事9"
    service.approve(entry_id, 1, actor_id="1")
    assert notices and notices[-1][0] == ["ou_9"] and "审核通过并生效" in notices[-1][1]
    assert service.snapshot().expand_message("岳麓店发了多少").endswith("岳麓店=长沙岳麓区测试网点）")
    service.save(entry_id, title="网点简称", body="改了说明", actor_id="9", row_version=repo.entries[entry_id]["row_version"])
    assert repo.entries[entry_id]["status"] == "PAUSED" and service.snapshot().empty  # editing pauses at once


def test_reject_sends_back_and_discard_without_active_version_retires():
    notices = []
    service, repo = _service(COMPILED, notices=notices)
    entry_id = service.create(title="t", body="b", actor_id="9")
    _ready(service, entry_id)
    service.submit(entry_id, 1, actor_id="9")
    with pytest.raises(HandbookError, match="正在审核"):
        service.save(entry_id, title="t", body="b2", actor_id="9", row_version=repo.entries[entry_id]["row_version"])
    service.reject(entry_id, 1, actor_id="1", reason="例子不够")
    assert repo.versions[(entry_id, 1)]["status"] == "COMPILED" and "例子不够" in notices[-1][1]
    assert service.discard(entry_id, 1, actor_id="1") == "RETIRED"
    assert service.list_entries()["entries"] == [] and len(service.list_entries(include_retired=True)["entries"]) == 1


def test_invalid_compile_is_recorded_and_cannot_be_submitted():
    service, repo = _service({**COMPILED, "examples": [{"utterance": "x", "action": "db.drop"}]})
    entry_id = service.create(title="t", body="b", actor_id="9")
    result = asyncio.run(service.compile(entry_id, actor=SimpleNamespace(actor_id="9")))
    assert result["validation"]["ok"] is False and "不存在的动作" in result["validation"]["errors"][0]
    with pytest.raises(HandbookError):
        service.submit(entry_id, 1, actor_id="9")


def test_trial_compares_before_and_after_without_running_anything():
    picks = []

    class Model:
        async def chat(self, messages, tools):
            with_handbook = "问个价 测试市 50 公斤”应选择" in messages[0]["content"]
            picks.append(with_handbook)
            name = "run_plugin_quote" if with_handbook else "ask_user"
            return {"tool_calls": [{"function": {"name": name, "arguments": "{}"}}]}

    tools = lambda actor: ([], {"run_plugin_quote": "plugin:quote"}, {"plugin:quote": "三平台报价"}, "")
    trial = HandbookTrial(Model(), tools, active=lambda: HandbookSnapshot())
    service, _ = _service(COMPILED, trial=trial)
    entry_id = service.create(title="t", body="b", actor_id="9")
    asyncio.run(service.compile(entry_id, actor=SimpleNamespace(actor_id="9")))
    result = asyncio.run(service.trial(entry_id, actor=SimpleNamespace(actor_id="9")))
    assert result == {"cases": 1, "passed": 1, "improved": 1, "rows": [{
        "utterance": "问个价 测试市 50 公斤", "expected": "plugin:quote", "expected_title": "三平台报价",
        "before": "ask_user", "before_title": "ask_user", "after": "plugin:quote", "after_title": "三平台报价",
        "pass": True}]}
    assert picks == [False, True]


def test_snapshot_read_failure_keeps_the_last_good_content():
    service, repo = _service(COMPILED)
    entry_id = service.create(title="t", body="b", actor_id="9")
    _ready(service, entry_id)
    service.submit(entry_id, 1, actor_id="9")
    service.approve(entry_id, 1, actor_id="9")
    good = service.snapshot()
    service._invalidate()
    repo.active_compiled = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert service.snapshot() is good


# -- real migration 085 ------------------------------------------------------------------
from tests.mysql_migrated_database import database  # noqa: E402,F401


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
def test_migrations_085_089_repository_lifecycle(database):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    import pymysql

    from shared.agent_handbook_repository import AgentHandbookRepository

    def connect():
        return pymysql.connect(host=database.host, port=database.port, user=database.user, password=database.password,
                               database=database.database, charset="utf8mb4", autocommit=False)

    repo = AgentHandbookRepository(connect)
    entry_id = repo.create_entry(title="网点简称", body="说明", actor_id="9")
    compiled = validate_compiled(COMPILED, CATALOG)
    repo.record_compile(entry_id, 1, compiled=compiled, validation={"ok": True, "errors": []}, run_id=None, actor_id="9",
                        seconds=12.3)
    repo.acknowledge(entry_id, 1, [(0, "不包含")], actor_id="9", at="2026-11-19T00:00:00+00:00")
    repo.record_trial(entry_id, 1, {"cases": 1, "passed": 1, "improved": 0, "rows": [{"pass": True}]}, actor_id="9")
    repo.submit(entry_id, 1, actor_id="9", blockers=lambda v, latest: submit_blockers(v, latest_version=latest))
    repo.approve(entry_id, 1, actor_id="9", blockers=lambda v, latest: approval_blockers(v, latest_version=latest))
    assert repo.active_compiled() == [(entry_id, 1, compiled)]
    row_version = repo.get_entry(entry_id)["row_version"]
    with pytest.raises(HandbookError, match="别人修改"):
        repo.save_draft(entry_id, title="网点简称", body="新说明", actor_id="9", expected_row_version=row_version - 1)
    assert repo.save_draft(entry_id, title="网点简称", body="新说明", actor_id="9", expected_row_version=row_version) == 2
    entry = repo.get_entry(entry_id)
    assert entry["status"] == "PAUSED" and repo.active_compiled() == []
    assert [v["status"] for v in entry["versions"]] == ["DRAFT", "ACTIVE"]
    with pytest.raises(HandbookError, match="重新编译"):
        repo.resume(entry_id, expected_row_version=entry["row_version"])
    assert repo.discard(entry_id, 2, actor_id="1") == "PAUSED"  # the approved V1 stays and can be resumed
    repo.resume(entry_id, expected_row_version=repo.get_entry(entry_id)["row_version"], actor_id="1")
    entry = repo.get_entry(entry_id)
    assert entry["status"] == "ACTIVE" and [v["status"] for v in entry["versions"]] == ["VOID", "ACTIVE"]
    actions = [item["action"] for item in reversed(entry["events"])]
    assert actions == ["CREATE", "COMPILE", "ACKNOWLEDGE", "TRIAL", "SUBMIT", "APPROVE", "SAVE", "DISCARD", "RESUME"]
    assert entry["events"][-2]["detail"] == {"ok": True, "seconds": 12.3}
    listed = next(item for item in repo.list_entries() if item["entry_id"] == entry_id)
    assert listed["latest_status"] == "VOID" and listed["summary"] == "新说明"
    repo.set_status(entry_id, "RETIRED", expected_row_version=entry["row_version"], actor_id="1")
    assert all(item["entry_id"] != entry_id for item in repo.list_entries())
    assert any(item["entry_id"] == entry_id for item in repo.list_entries(include_retired=True))


def test_sidecar_prompts_with_handbook_and_blocks_forbidden_fixed_query():
    from uuid import uuid4

    from agent.harness.catalog import HarnessToolCatalog
    from agent.harness.models import HarnessMessage
    from agent.harness_application import TrustedHarnessInvocationAdapter, build_fixed_harness_tools
    from agent.harness_online import OnlineHarnessSidecar
    from agent.llm_client import LLMClient
    from agent.orchestration.models import Actor, ActorType

    actor = Actor(ActorType.CONSOLE_ADMIN, "17", ("admin",), authenticated_by="mysql_admin_session")
    handbook = build_snapshot([("e", 1, {**COMPILED, "examples": [{"utterance": "这票到哪了", "action": "waybill.lookup"}],
                                         "rules": [{"type": "forbid", "action": "waybill.lookup", "condition": "",
                                                    "message": "运单信息请在后台查询"}]})])
    prompts, replies = [], iter([
        {"tool_calls": [{"id": "c", "function": {"name": "query_waybill", "arguments": '{"waybill_number":"R90000000001"}'}}]},
        {"content": "运单信息请在后台查询。"}])
    model = LLMClient()
    model.public_status = lambda: {"configured": True}
    async def chat(messages, **_):
        prompts.append(messages)
        return next(replies)
    model.chat = chat
    handler = lambda arguments: pytest.fail("forbidden query must not run")
    adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(), actor=actor, base_request_id=str(uuid4()),
                                              fixed_handlers={"waybill.lookup": handler})
    catalog = HarnessToolCatalog(invocation_port=adapter, fixed_tools=build_fixed_harness_tools())
    sidecar = OnlineHarnessSidecar(catalog=catalog, llm=model, handbook=handbook)
    result = sidecar.run(messages=(HarnessMessage("user", "R90000000001 这票到哪了", str(uuid4())),), timeout_seconds=5)
    assert "“这票到哪了”应选择「查询运单信息」" in prompts[0][0]["content"]
    assert "HANDBOOK_FORBIDDEN" in prompts[1][-1]["content"]
    assert result.decisions[0].decision == "REJECT"


def test_api_lets_module_editors_edit_but_only_super_admins_take_effect():
    """The 业务手册 module grants editing; approve, resume and retire need a super administrator."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent.handbook_api import create_handbook_router

    calls = []

    class Service:
        def save(self, entry_id, **kwargs):
            calls.append("save")
            return 2

        def approve(self, entry_id, version, *, actor_id):
            calls.append("approve")

        def submit(self, entry_id, version, *, actor_id):
            calls.append("submit")

        def withdraw(self, entry_id, version, *, actor_id):
            calls.append("withdraw")

        def acknowledge(self, entry_id, version, *, answers, indices, actor_id):
            calls.append(("acknowledge", answers))
            return [0]

        def pause(self, entry_id, *, row_version, actor_id):
            calls.append("pause")

        def resume(self, entry_id, *, row_version, actor_id):
            calls.append("resume")

        def retire(self, entry_id, *, row_version, actor_id):
            calls.append("retire")

    def admin(request):
        return SimpleNamespace(actor_id="7")

    def super_admin(request):
        raise PermissionError("super administrator required")

    app = FastAPI()
    app.include_router(create_handbook_router(service_provider=Service, admin_provider=admin,
                                              super_admin_provider=super_admin))
    client = TestClient(app, raise_server_exceptions=False)
    entry = "11111111-2222-4333-8444-555555555555"
    base = f"/internal/v1/admin/handbook/{entry}"
    assert client.post(f"{base}/save", json={"title": "t", "body": "b", "row_version": 1}).status_code == 200
    assert client.post(f"{base}/pause", json={"row_version": 1}).status_code == 200
    assert client.post(f"{base}/acknowledge", json={"version": 2, "answers": [{"index": 0, "answer": "是"}]}).status_code == 200
    assert client.post(f"{base}/acknowledge", json={"version": 2}).status_code == 422
    assert client.post(f"{base}/submit", json={"version": 2}).status_code == 200
    assert client.post(f"{base}/withdraw", json={"version": 2}).status_code == 200
    for action, payload in (("approve", {"version": 2}), ("resume", {"row_version": 1}), ("retire", {"row_version": 1}),
                            ("reject", {"version": 2}), ("discard", {"version": 2})):
        assert client.post(f"{base}/{action}", json=payload).status_code == 500
    assert calls == ["save", "pause", ("acknowledge", [{"index": 0, "answer": "是"}]), "submit", "withdraw"]


def test_try_steps_are_derived_from_what_the_turn_did():
    from agent.handbook_try_api import trial_steps
    from shared.agent_action_audit import ActionDecision

    quoted = trial_steps([ActionDecision("knowledge.search", "read", "EXECUTE", "查询知识库"),
                          ActionDecision("knowledge.quote", "read", "EXECUTE")], quotes=1, reply="原文……")
    assert [step["state"] for step in quoted] == ["done", "done", "done", "skipped", "skipped", "done"]
    assert "引用原文 1 段" in quoted[2]["result"] and quoted[5]["result"] == "未执行插件 / 未写入 / 未发送"
    asked = trial_steps([ActionDecision("fee.upstairs", "read", "CLARIFY", "计算上楼费")], quotes=0, reply="几楼？")
    assert asked[1]["result"].startswith("已执行反问") and asked[3]["state"] == "done" and asked[4]["state"] == "blocked"
