"""Agent layer P2: the business handbook — compile rules, approval, runtime effect.

Compiled content may only refer to existing actions and plugins; only an
approved version of an active note reaches conversations; editing pauses it;
unconditional rules are enforced by code, conditional ones only instruct.
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
    HandbookCatalog, HandbookError, HandbookSnapshot, approval_blockers, build_snapshot, describe_compiled,
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


def test_approval_needs_compiled_latest_version_with_ambiguities_handled_and_nothing_unsupported():
    version = {"version": 2, "status": "COMPILED", "compiled": COMPILED, "validation": {"ok": True}, "acknowledged": []}
    assert approval_blockers(version, latest_version=2) == ["还有未处理的歧义"]
    assert approval_blockers({**version, "acknowledged": [0]}, latest_version=2) == []
    assert "只能审核最新版本" in approval_blockers({**version, "acknowledged": [0]}, latest_version=3)
    unsupported = {**version, "acknowledged": [0], "compiled": {**COMPILED, "unsupported": ["自动给客户打电话"]}}
    assert any("开发需求" in item for item in approval_blockers(unsupported, latest_version=2))


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
        self.entries, self.versions = {}, {}

    def create_entry(self, *, title, body, actor_id):
        entry_id = f"e{len(self.entries) + 1}"
        self.entries[entry_id] = {"entry_id": entry_id, "title": title, "status": "DRAFT", "latest_version": 1,
                                  "active_version": None, "row_version": 1}
        self.versions[(entry_id, 1)] = {"version": 1, "body": body, "status": "DRAFT", "compiled": None,
                                        "validation": None, "trial": None, "acknowledged": []}
        return entry_id

    def get_entry(self, entry_id):
        entry = dict(self.entries[entry_id])
        entry["versions"] = [dict(v) for (eid, _), v in sorted(self.versions.items(), reverse=True) if eid == entry_id]
        return entry

    def list_entries(self):
        return list(self.entries.values())

    def save_draft(self, entry_id, *, title, body, actor_id, expected_row_version):
        entry = self.entries[entry_id]
        latest = self.versions[(entry_id, entry["latest_version"])]
        version = entry["latest_version"] if latest["status"] in {"DRAFT", "COMPILED"} else entry["latest_version"] + 1
        self.versions[(entry_id, version)] = {"version": version, "body": body, "status": "DRAFT", "compiled": None,
                                              "validation": None, "trial": None, "acknowledged": []}
        entry.update(latest_version=version, title=title, row_version=entry["row_version"] + 1,
                     status="PAUSED" if entry["status"] == "ACTIVE" else entry["status"])
        return version

    def record_compile(self, entry_id, version, *, compiled, validation, run_id, actor_id):
        self.versions[(entry_id, version)].update(compiled=compiled, validation=validation,
                                                  status="COMPILED" if validation["ok"] else "DRAFT")

    def record_trial(self, entry_id, version, trial):
        self.versions[(entry_id, version)]["trial"] = trial

    def acknowledge(self, entry_id, version, indices):
        self.versions[(entry_id, version)]["acknowledged"] = indices

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


def _service(answer, trial=None):
    repo = MemoryRepository()
    return HandbookService(repo, FakeGateway(answer), catalog_provider=lambda actor: CATALOG, trial_runner=trial), repo


def test_compile_approve_edit_pause_cycle():
    service, repo = _service(COMPILED)
    entry_id = service.create(title="网点简称", body="大家说岳麓店就是长沙岳麓区测试网点", actor_id="9")
    result = asyncio.run(service.compile(entry_id, actor=SimpleNamespace(actor_id="9")))
    assert result["validation"]["ok"] and service.snapshot().empty
    assert service._gateway.payloads[0]["plugins"][0] == {"id": "plugin:list", "title": "到货清单"}
    with pytest.raises(HandbookError, match="歧义"):
        service.approve(entry_id, 1, actor_id="9")
    service.acknowledge(entry_id, 1, [0])
    service.approve(entry_id, 1, actor_id="9")
    assert service.snapshot().expand_message("岳麓店发了多少").endswith("岳麓店=长沙岳麓区测试网点）")
    service.save(entry_id, title="网点简称", body="改了说明", actor_id="9", row_version=repo.entries[entry_id]["row_version"])
    assert repo.entries[entry_id]["status"] == "PAUSED" and service.snapshot().empty  # editing pauses at once


def test_invalid_compile_is_recorded_and_cannot_be_approved():
    service, repo = _service({**COMPILED, "examples": [{"utterance": "x", "action": "db.drop"}]})
    entry_id = service.create(title="t", body="b", actor_id="9")
    result = asyncio.run(service.compile(entry_id, actor=SimpleNamespace(actor_id="9")))
    assert result["validation"]["ok"] is False and "不存在的动作" in result["validation"]["errors"][0]
    with pytest.raises(HandbookError):
        service.approve(entry_id, 1, actor_id="9")


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
    asyncio.run(service.compile(entry_id, actor=SimpleNamespace(actor_id="9")))
    service.acknowledge(entry_id, 1, [0])
    service.approve(entry_id, 1, actor_id="9")
    good = service.snapshot()
    service._invalidate()
    repo.active_compiled = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert service.snapshot() is good


# -- real migration 085 ------------------------------------------------------------------
from tests.mysql_migrated_database import database  # noqa: E402,F401


@pytest.mark.skipif(os.getenv("RUN_MYSQL_INTEGRATION") != "1", reason="requires isolated MySQL 8")
def test_migration_085_repository_lifecycle(database):  # noqa: F811
    assert os.environ["AGENT_DB_HOST"] == "127.0.0.1"
    import pymysql

    from shared.agent_handbook_repository import AgentHandbookRepository

    def connect():
        return pymysql.connect(host=database.host, port=database.port, user=database.user, password=database.password,
                               database=database.database, charset="utf8mb4", autocommit=False)

    repo = AgentHandbookRepository(connect)
    entry_id = repo.create_entry(title="网点简称", body="说明", actor_id="9")
    compiled = validate_compiled(COMPILED, CATALOG)
    repo.record_compile(entry_id, 1, compiled=compiled, validation={"ok": True, "errors": []}, run_id=None, actor_id="9")
    repo.acknowledge(entry_id, 1, [0])
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
    repo.set_status(entry_id, "RETIRED", expected_row_version=entry["row_version"])
    assert all(item["entry_id"] != entry_id for item in repo.list_entries())


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
