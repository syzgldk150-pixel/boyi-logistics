"""Knowledge answers are the document's own words (业务手册 收尾第 4 步).

The model only points at one passage; code checks it is one continuous piece of
a body read in this turn, sends that piece verbatim with one line naming where it
comes from and the link, and never sends a model paraphrase. Bodies are invented.
"""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.harness.catalog import HarnessToolCatalog
from agent.harness.models import HarnessMessage
from agent.harness_application import TrustedHarnessInvocationAdapter, build_fixed_harness_tools
from agent.harness_online import OnlineHarnessSidecar
from agent.knowledge_answers import NO_QUOTE_REPLY, QuoteError, quote_knowledge
from agent.llm_client import LLMClient
from agent.orchestration.models import Actor, ActorType

ACTOR = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin", "super_admin"), authenticated_by="feishu_admin_binding")
PDF_BODY = ("[PDF 第 51 页]\n第五章 送货上下楼服务收费标准\n5.1 无电梯送货上楼，按楼层与结算重量收取。\n\n"
            "[PDF 第 52 页]\n5.2 破损件须在签收时当场拍照登记，\n超过 24 小时不再受理。\n5.3 其他情况协商处理。")
DOC_BODY = "# 回单管理\n## 回单寄回\n回单须在签收后 7 日内寄回始发网点。\n## 回单审核\n审核不通过须写明原因。"
PDF = {"source_id": "node-pdf", "title": "测试网络管理手册", "url": "https://example.feishu.cn/wiki/pdf",
       "format": "pdf", "body": PDF_BODY, "space": "测试空间", "selected_pages": [51, 52]}
DOC = {"source_id": "node-doc", "title": "回单规定", "url": "https://example.feishu.cn/wiki/doc", "body": DOC_BODY,
       "space": "测试空间"}
RESULTS = [{"ok": True, "status": "found", "items": [PDF, DOC]}]


def test_pdf_passage_is_returned_verbatim_with_its_page_and_link():
    quote = quote_knowledge({"source_id": "node-pdf", "quote": "破损件须在签收时当场拍照登记，超过 24 小时不再受理。"}, RESULTS)
    assert quote.text == "5.2 破损件须在签收时当场拍照登记，\n超过 24 小时不再受理。".split("5.2 ")[1]
    assert quote.location == "第 52 页" and quote.pages == (52,)
    assert quote.render() == ("以下是《测试网络管理手册》第 52 页的原文：\n\n破损件须在签收时当场拍照登记，\n超过 24 小时不再受理。"
                              "\n\n全文：https://example.feishu.cn/wiki/pdf")


def test_passage_across_a_page_break_names_both_pages_and_drops_the_marker():
    quote = quote_knowledge({"source_id": "node-pdf", "quote": "按楼层与结算重量收取。5.2 破损件须在签收时"}, RESULTS)
    assert quote.location == "第 51–52 页" and "[PDF" not in quote.text


def test_online_document_names_the_nearest_heading():
    quote = quote_knowledge({"source_id": "node-doc", "quote": "回单须在签收后 7 日内寄回始发网点。"}, RESULTS)
    assert quote.render().startswith("以下是《回单规定》“回单寄回”部分的原文：")


@pytest.mark.parametrize("arguments,message", [
    ({"source_id": "node-pdf", "quote": "破损件须在签收后当场拍照登记"}, "一字不差"),       # one word changed
    ({"source_id": "node-pdf", "quote": "破损件须在签收时当场拍照登记，其他情况协商处理。"}, "一字不差"),  # stitched
    ({"source_id": "node-x", "quote": "破损件须在签收时当场拍照登记"}, "实际返回"),
    ({"source_id": "node-pdf", "quote": "5.2"}, "太短"),
    ({"source_id": "node-pdf"}, "source_id"),
])
def test_anything_but_original_text_is_refused(arguments, message):
    with pytest.raises(QuoteError, match=message):
        quote_knowledge(arguments, RESULTS)


def test_failed_reads_cannot_be_quoted():
    with pytest.raises(QuoteError, match="实际返回"):
        quote_knowledge({"source_id": "node-pdf", "quote": "破损件须在签收时当场拍照登记"},
                        [{"ok": False, "items": [PDF]}])


# -- through the conversation loop -----------------------------------------------------------

def _call(name, arguments, call_id="c1"):
    return {"tool_calls": [{"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


SEARCH = _call("query_knowledge", {"query": "破损", "limit": 5})


def _sidecar(replies):
    model = LLMClient()
    model.public_status = lambda: {"configured": True}
    seen = []

    async def chat(messages, tools=None, **_):
        seen.append([dict(item) for item in messages])
        assert any(tool["function"]["name"] == "quote_knowledge" for tool in tools)
        return replies[len(seen) - 1]

    model.chat = chat
    adapter = TrustedHarnessInvocationAdapter(policy_service=SimpleNamespace(), actor=ACTOR, base_request_id=str(uuid4()),
                                              fixed_handlers={"knowledge.search": lambda args: {**RESULTS[0], "source": "test"}})
    sidecar = OnlineHarnessSidecar(catalog=HarnessToolCatalog(invocation_port=adapter, fixed_tools=build_fixed_harness_tools()),
                                   llm=model)
    return sidecar, seen


def _run(sidecar, text="破损件怎么处理"):
    return sidecar.run(messages=(HarnessMessage("user", text, str(uuid4())),), timeout_seconds=5)


def test_quote_tool_answer_is_the_original_passage():
    sidecar, _ = _sidecar([SEARCH, _call("quote_knowledge", {"source_id": "node-pdf",
                                                             "quote": "破损件须在签收时当场拍照登记，超过 24 小时不再受理。"}, "c2")])
    result = _run(sidecar)
    assert result.content.startswith("以下是《测试网络管理手册》第 52 页的原文：")
    assert [quote.pages for quote in sidecar.quotes] == [(52,)]
    assert any(item.action_id == "knowledge.quote" and item.decision == "EXECUTE" for item in result.decisions)


def test_paraphrase_is_never_sent_the_model_is_asked_to_quote_first():
    paraphrase = {"content": "破损件要当天拍照，一天内报上来就行。"}
    sidecar, seen = _sidecar([SEARCH, paraphrase, _call("quote_knowledge", {
        "source_id": "node-pdf", "quote": "破损件须在签收时当场拍照登记，超过 24 小时不再受理。"}, "c2")])
    result = _run(sidecar)
    assert "一天内报上来就行" not in result.content and result.content.startswith("以下是")
    assert "quote_knowledge" in seen[2][-1]["content"]  # the nudge


def test_persistent_paraphrase_or_bad_quotes_end_without_any_model_wording():
    paraphrase = {"content": "破损件要当天拍照，一天内报上来就行。"}
    sidecar, _ = _sidecar([SEARCH, paraphrase, paraphrase])
    result = _run(sidecar)
    assert result.content.startswith(NO_QUOTE_REPLY) and "一天内报上来就行" not in result.content
    assert "测试网络管理手册" in result.content  # the documents are still listed
    bad = _call("quote_knowledge", {"source_id": "node-pdf", "quote": "破损件一天内报上来"}, "c2")
    sidecar, _ = _sidecar([SEARCH, bad, bad])
    result = _run(sidecar)
    assert result.content.startswith(NO_QUOTE_REPLY)


# -- 业务手册 → 试问 ---------------------------------------------------------------------------

def test_trial_turn_offers_only_read_actions_and_keeps_its_own_state():
    from agent.agent_actions.business_actions import BusinessActions

    actions = BusinessActions(invoke=lambda *a: {"ok": False}, cs_accounts=lambda: [])
    real, trial = actions.turn(ACTOR), actions.turn(ACTOR, trial=True)
    names = {tool["function"]["name"] for tool in trial.model_tools()}
    assert "biz_reply_problem" not in names and {"biz_query_problems", "biz_fee_quote"} <= names
    assert not trial.handles("biz_reply_problem") and real.handles("biz_reply_problem")
    assert trial._key != real._key


def test_try_api_returns_the_reply_quotes_and_steps():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent.handbook_try_api import create_handbook_try_router

    sidecar, _ = _sidecar([SEARCH, _call("quote_knowledge", {"source_id": "node-doc",
                                                             "quote": "回单须在签收后 7 日内寄回始发网点。"}, "c2")])
    seen = []

    class Runtime:
        def trial_sidecar(self, actor, request_id):
            seen.append(actor)
            return sidecar

    app = FastAPI()
    app.include_router(create_handbook_try_router(runtime_provider=Runtime, admin_provider=lambda request: ACTOR))
    data = TestClient(app).post("/internal/v1/admin/handbook-try", json={"question": "回单多久寄回"}).json()["data"]
    assert data["reply"].startswith("以下是《回单规定》“回单寄回”部分的原文：")
    assert data["quotes"] == [{"title": "回单规定", "location": "“回单寄回”部分", "pages": [], "url": DOC["url"]}]
    assert {"action": "引用原文", "decision": "执行"} in data["decisions"] and seen == [ACTOR]
    assert data["steps"][2] == {"title": "检索知识库", "state": "done", "result": "检索知识库并引用原文 1 段"}


def test_try_needs_only_viewing_the_handbook():
    from shared.identity_routes import agent_permission, console_permission

    assert console_permission("POST", "/handbook/try") == "handbook:view"
    assert agent_permission("POST", "/internal/v1/admin/handbook-try") == "handbook:view"
