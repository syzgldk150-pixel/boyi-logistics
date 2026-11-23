"""业务手册 → 计算规则 → 核对: manual pages cited by 收费表 lines, found by exact title (2026-11-20)."""
import pytest

from agent.fee_source_pages import FeeSourcePages
from shared.handbook_fee_rules import FeeRuleError

DOCUMENTS = [{"title": "融辉物流网络管理手册 V6.1.pdf", "obj_token": "doc-1"},
             {"title": "重名手册", "obj_token": "doc-2"}, {"title": "重名手册.pdf", "obj_token": "doc-3"}]


def test_pages_come_from_the_one_document_with_that_title_and_are_kept_for_a_while():
    listed, rendered, now = [], [], [0.0]

    def documents():
        listed.append(1)
        return DOCUMENTS

    def render(document_id, page):
        rendered.append((document_id, page))
        return b"png-" + document_id.encode() + bytes([page])

    pages = FeeSourcePages(list_documents=documents, render_page=render, clock=lambda: now[0])
    assert pages.page("融辉物流网络管理手册 V6.1", 50) == b"png-doc-1" + bytes([50])  # spaces and .pdf do not matter
    assert pages.page("融辉物流网络管理手册V6.1", 50) == b"png-doc-1" + bytes([50])
    assert rendered == [("doc-1", 50)] and listed == [1]
    with pytest.raises(FeeRuleError, match="多个名为"):
        pages.page("重名手册", 1)
    with pytest.raises(FeeRuleError, match="没有找到文档"):
        pages.page("别的手册", 1)
    now[0] = 601.0
    pages.page("融辉物流网络管理手册 V6.1", 51)
    assert listed == [1, 1]  # the document list is read again after ten minutes


def test_a_source_name_finds_the_knowledge_file_despite_marks_dashes_and_the_version_v():
    """2026-11-23 on ECS: sources say “融辉物流网络管理手册 V6.1”, the file is “《融辉物流网络管理手册》-6.1.pdf”."""
    files = [{"title": "《融辉物流网络管理手册》-6.1.pdf", "obj_token": "doc-1"}, {"title": "网点操作手册 (2).pdf", "obj_token": "doc-2"}]
    pages = FeeSourcePages(list_documents=lambda: files, render_page=lambda document_id, page: document_id.encode())
    assert pages.page("融辉物流网络管理手册 V6.1", 60) == b"doc-1"
    assert pages.page("《融辉物流网络管理手册》-6.1", 61) == b"doc-1"  # the name AI drafts write
    assert pages.page("网点操作手册（2）", 1) == b"doc-2"
    with pytest.raises(FeeRuleError, match="没有找到文档"):
        pages.page("融辉物流网络管理手册 V6.0", 1)  # a different version is still a different document
