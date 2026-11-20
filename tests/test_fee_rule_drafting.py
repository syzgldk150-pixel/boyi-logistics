"""计算规则 → 从手册起草 (方案 B, 2026-11-20): what the AI returns is checked against the pages it read."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.ai_tasks import AITaskError
from shared.fee_rule_drafting import (
    MAX_PAGES, chapters, check_draft, drafting_payload, drafting_spec, formula_numbers, page_numbers, page_range,
)
from shared.handbook_fee_rules import FeeRuleError

ROOT = Path(__file__).resolve().parents[1]
BATCH = json.loads((ROOT / "tests" / "fixtures" / "fee_rules" / "v61_first_batch.json").read_text(encoding="utf-8"))
# Synthetic manual page: the numbers the count-check fixture uses are written on it.
PAGES = [{"page": 59, "text": "第十章 开箱清点服务\n开箱清点20件以内免费，超过部分按0.1元/件收取，最低5元，最高100元。"},
         {"page": 60, "text": "件数为开箱或拆托后的内物件数，以签收底单客户签字件数为准。"}]


def _fee(name="开箱清点", **changes):
    content = copy.deepcopy(BATCH["count_check"]["content"])
    for source in _walk_sources(content):
        source.pop("document", None)
        source["location"] = "第五篇第十章 第 59 页"
    return {"name": name, "content": {**content, **changes}, "questions": []}


def _walk_sources(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source" and isinstance(item, dict):
                yield item
            else:
                yield from _walk_sources(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_sources(item)


def test_a_draft_built_only_from_the_pages_passes_and_gets_the_document_name():
    result = check_draft({"fees": [_fee()], "skipped": ["第 60 页只是说明"]}, document="示例手册 V1", pages=PAGES,
                         existing=["开箱清点"])
    fee = result["fees"][0]
    assert fee["existing"] is True and result["skipped"] == ["第 60 页只是说明"]
    assert {source["document"] for source in _walk_sources(fee["content"])} == {"示例手册 V1"}


@pytest.mark.parametrize("change, message", [
    (lambda fee: fee["content"]["formula"]["tiers"][1]["then"].__setitem__("max", "120"), "原文里没有的数字：120"),
    (lambda fee: fee["content"]["formula"]["source"].__setitem__("location", "第 70 页"), "不在本次读取的第 59–60 页内"),
    (lambda fee: fee["content"]["formula"]["source"].__setitem__("location", "附表"), "没写页码"),
    (lambda fee: fee["content"]["formula"]["source"].__setitem__("quote", "50件以内免费"), "原文里找不到"),
    (lambda fee: fee["content"]["formula"].pop("source"), "没有出处"),
    (lambda fee: fee["content"].__setitem__("formula", {"type": "magic"}), "不是有效的积木"),
    (lambda fee: fee.__setitem__("questions", [{"text": "怎么收？", "options": ["只有一个"]}]), "2 到 6 个"),
])
def test_made_up_numbers_wrong_pages_and_bad_rules_go_back_to_the_model(change, message):
    fee = _fee()
    change(fee)
    with pytest.raises(ValueError, match=message):
        check_draft({"fees": [fee]}, document="示例手册 V1", pages=PAGES)


def test_answer_shape_names_and_questions():
    with pytest.raises(ValueError, match="fees"):
        check_draft({"rules": []}, document="d", pages=PAGES)
    with pytest.raises(ValueError, match="重名"):
        check_draft({"fees": [_fee(), _fee()]}, document="d", pages=PAGES)
    fee = _fee()
    fee["questions"] = [{"text": "混装时怎么收？", "options": ["需人工判断（现在的做法）", "其他（写明做法）"]}]
    assert check_draft({"fees": [fee]}, document="d", pages=PAGES)["fees"][0]["questions"] == [
        {"kind": "question", "text": "混装时怎么收？", "options": ["需人工判断（现在的做法）", "其他（写明做法）"], "answer": None, "note": ""}]


def test_numbers_on_the_page_include_chinese_ones_and_the_formula_lists_its_own():
    assert {f"{item:f}" for item in page_numbers("三件起10元，每多一件加5元，二十公斤以上0.15元")} >= {"3", "10", "5", "20", "0.15", "1"}
    found = {f"{number:f}" for _, number in formula_numbers(BATCH["count_check"]["content"])}
    assert found == {"0", "20", "0.1", "5", "100"}  # parameter input limits (max 100000) are not formula numbers


def test_chapters_and_page_ranges():
    outline = [{"title": "第五篇 收费", "page": 46, "level": 0}, {"title": "第三章 卸货", "page": 46, "level": 1},
               {"title": "第五章 上楼", "page": 50, "level": 1}, {"title": "第六篇 附则", "page": 64, "level": 0}]
    assert [(item["title"], item["start"], item["end"]) for item in chapters(outline, 90)] == [
        ("第五篇 收费", 46, 64), ("第三章 卸货", 46, 50), ("第五章 上楼", 50, 64), ("第六篇 附则", 64, 90)]
    assert page_range(50, 52, 90) == (50, 52)
    with pytest.raises(FeeRuleError, match=f"最多起草 {MAX_PAGES} 页"):
        page_range(46, 63, 90)
    with pytest.raises(FeeRuleError, match="共 90 页"):
        page_range(80, 95, 90)


def test_the_task_is_bound_to_the_pages_it_read():
    spec = drafting_spec(document="示例手册 V1", pages=PAGES, existing=[])
    assert spec.task_id == "handbook.fee_draft" and spec.max_retries == 1
    assert spec.validate({"fees": [_fee()]})["fees"][0]["name"] == "开箱清点"
    payload = drafting_payload(document="示例手册 V1", pages=PAGES, existing=["上楼费"], chapter="第十章")
    assert payload["page_range"] == "第 59–60 页" and payload["existing_fees"] == ["上楼费"]


# -- the drafting job (agent/agent/fee_drafting.py) --------------------------------------------

class _Repository:
    def __init__(self):
        self.added = []

    def list_versions(self, include_retired=False):
        return [{"fee_name": "开箱清点", "status": "DRAFT"}]

    def add_drafted(self, **kwargs):
        self.added.append(kwargs)
        return {"rule_id": f"r-{len(self.added)}", "fee_name": kwargs["fee_name"], "new_fee": False}


class _Gateway:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def run(self, spec, payload, *, subject_ref, actor_id, images):
        self.calls.append((spec.task_id, payload["page_range"], len(images), subject_ref))
        if self.error:
            raise self.error
        return SimpleNamespace(run_id="run-1", result=spec.validate(self.answer))


def _service(gateway, repository=None):
    from agent.fee_drafting import FeeDraftService

    document = {"node_token": "n1", "title": "示例手册 V1.pdf", "obj_token": "o1", "page_count": 90,
                "pages": [{"page": number, "text": ""} for number in range(1, 59)] + PAGES
                + [{"page": number, "text": ""} for number in range(61, 91)],
                "outline": [{"title": "第十章 开箱清点", "page": 59, "level": 0}]}
    reads = []

    def read(node):
        reads.append(node)
        return document

    service = FeeDraftService(
        documents=lambda: [{"node_token": "n1", "title": "示例手册 V1.pdf", "obj_type": "file", "space": "示例空间"},
                           {"node_token": "n2", "title": "说明.docx", "obj_type": "docx"}],
        read_pdf=read, page_images=lambda obj, pages: [b"\x89PNG\r\n\x1a\n"] * len(pages), gateway=gateway,
        repository=repository or _Repository())
    return service, reads


def test_a_job_reads_once_drafts_and_keeps_chosen_fees_as_drafts():
    gateway = _Gateway({"fees": [_fee()], "skipped": []})
    repository = _Repository()
    service, reads = _service(gateway, repository)
    assert [item["node_token"] for item in service.documents()] == ["n1"]
    assert service.outline("n1")["chapters"] == [{"title": "第十章 开箱清点", "level": 0, "start": 59, "end": 90}]
    with pytest.raises(FeeRuleError, match="知识来源里没有"):
        service.outline("n2")

    async def scenario():
        job = service.start(node_token="n1", start=59, end=60, chapter="第十章 开箱清点", actor_id="7")
        with pytest.raises(FeeRuleError, match="已经有一个起草"):
            service.start(node_token="n1", start=59, end=60, chapter="", actor_id="7")
        for _ in range(300):
            await asyncio.sleep(0.01)
            if service.job(job["job_id"], actor_id="7")["status"] != "RUNNING":
                break
        return job["job_id"]

    job_id = asyncio.run(scenario())
    view = service.job(job_id, actor_id="7")
    assert view["status"] == "DONE" and view["document_title"] == "示例手册 V1"
    fee = view["result"]["fees"][0]
    assert fee["existing"] is True and fee["lines"][1]["text"].endswith("（最低 5 元，最高 100 元）")
    assert reads == ["n1"]  # the outline read is reused by the job
    assert gateway.calls == [("handbook.fee_draft", "第 59–60 页", 2, "fee-draft:n1:59-60")]
    with pytest.raises(FeeRuleError, match="没有找到这次起草"):
        service.job(job_id, actor_id="8")  # only the person who started it sees it
    saved = service.save(job_id, indices=[0], actor_id="7")
    assert saved["created"][0]["rule_id"] == "r-1"
    assert repository.added[0]["version_label"] == "AI草稿" and repository.added[0]["detail"]["pages"] == "59-60"
    assert service.save(job_id, indices=[0], actor_id="7")["created"][0]["already"] is True  # not saved twice
    assert len(repository.added) == 1
    with pytest.raises(FeeRuleError, match="请选择"):
        service.save(job_id, indices=[3], actor_id="7")


def test_a_failed_job_says_why():
    gateway = _Gateway(error=AITaskError("AI_INVALID", "AI 返回的结果不符合要求，未保存。", detail="第 1 项“x”用了原文里没有的数字：120"))
    service, _ = _service(gateway)

    async def scenario():
        return await _start_and_wait(service, 59, 60, actor="7")

    view = asyncio.run(scenario())
    assert view["status"] == "FAILED" and "原文里没有的数字：120" in view["error"]
    too_long = asyncio.run(_start_and_wait(service, 46, 63))
    assert too_long["status"] == "FAILED" and "最多起草" in too_long["error"]


async def _start_and_wait(service, start, end, actor="9"):
    job = service.start(node_token="n1", start=start, end=end, chapter="", actor_id=actor)
    for _ in range(300):
        await asyncio.sleep(0.01)
        view = service.job(job["job_id"], actor_id=actor)
        if view["status"] != "RUNNING":
            return view
    return view


def test_drafts_api_routes_and_permissions():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent.fee_drafts_api import create_fee_drafts_router
    from shared.identity_routes import agent_permission

    gateway = _Gateway({"fees": [_fee()], "skipped": []})
    service, _ = _service(gateway)
    app = FastAPI()
    app.include_router(create_fee_drafts_router(service_provider=lambda: service,
                                                admin_provider=lambda request: SimpleNamespace(actor_id="7")))
    client = TestClient(app)
    base = "/internal/v1/admin/fee-drafts"
    assert client.get(f"{base}/documents").json()["data"]["documents"][0]["title"] == "示例手册 V1.pdf"
    assert client.get(f"{base}/outline", params={"node_token": "n1"}).json()["data"]["page_count"] == 90
    started = client.post(f"{base}/jobs", json={"node_token": "n1", "start": 59, "end": 60, "chapter": "第十章"})
    assert started.status_code == 200 and started.json()["data"]["job_id"]
    assert client.get(f"{base}/jobs/nope").status_code == 404
    assert agent_permission("GET", f"{base}/documents") == "handbook:view"
    assert agent_permission("POST", f"{base}/jobs") == "handbook:operate"


def test_pages_are_rendered_in_one_batch_under_the_output_cap(tmp_path):
    from pypdf import PdfWriter

    from tools.feishu_knowledge_pdf import MAX_BATCH_IMAGE_BYTES, render_pages

    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=595, height=842)
    path = tmp_path / "manual.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    rendered = render_pages(str(path), [2, 3, 4, 5])["pages"]
    assert [item["page"] for item in rendered] == [2, 3, 4, 5]
    assert sum(len(item["png_base64"]) for item in rendered) <= MAX_BATCH_IMAGE_BYTES
    for pages in ([1, 2, 3, 4, 5], [1, 1], [6]):
        with pytest.raises(ValueError):
            render_pages(str(path), pages)
