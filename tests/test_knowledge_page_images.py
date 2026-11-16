"""Table pages of a knowledge answer are sent as pictures (业务手册 收尾第 4b 步).

A table's text layer comes out scrambled, so the page itself is drawn and sent
to Feishu, WeChat (official iLink image upload) and the AI assistant page; when
a picture cannot be made or sent, the person gets the page text and the reason.
All documents and tokens here are invented.
"""
import asyncio
import base64
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent.knowledge_answers import PageImage, looks_like_table, quote_knowledge

ROOT = Path(__file__).resolve().parents[1]
TABLE_PAGE = "\n".join(["送货上下楼收费标准", "楼层", "无电梯", "有电梯", "2-3层", "0.05元/公斤", "4-6层",
                        "0.10元/公斤", "7层", "0.15元/公斤", "单件超过120公斤", "需协商"])
BODY = f"[PDF 第 50 页]\n5.1 本章规定送货上下楼服务的收费方式，适用于全网派送业务。\n\n[PDF 第 51 页]\n{TABLE_PAGE}"
ITEM = {"source_id": "node-pdf", "document_id": "boxcnPdfToken0001", "title": "测试网络管理手册",
        "url": "https://example.feishu.cn/wiki/pdf", "format": "pdf", "body": BODY}
RESULTS = [{"ok": True, "items": [ITEM]}]
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_table_pages_are_recognised_and_prose_is_not():
    assert looks_like_table(TABLE_PAGE)
    assert not looks_like_table("5.1 本章规定送货上下楼服务的收费方式，适用于全网派送业务。\n" * 8)


def test_a_table_quote_sends_the_page_picture_instead_of_scrambled_text():
    quote = quote_knowledge({"source_id": "node-pdf", "quote": "4-6层\n0.10元/公斤"}, RESULTS)
    assert quote.table_pages == (51,)
    assert quote.render() == ("以下是《测试网络管理手册》第 51 页的原文（表格页，原文见下方截图）：\n\n"
                              "全文：https://example.feishu.cn/wiki/pdf")
    (image,) = quote.images()
    assert (image.document_id, image.page, image.caption) == ("boxcnPdfToken0001", 51, "《测试网络管理手册》第 51 页")
    assert "4-6层" in image.fallback and "可能错位" in image.fallback
    prose = quote_knowledge({"source_id": "node-pdf", "quote": "适用于全网派送业务。"}, RESULTS)
    assert prose.table_pages == () and prose.images() == ()
    hinted = quote_knowledge({"source_id": "node-pdf", "quote": "适用于全网派送业务。", "table": True}, RESULTS)
    assert hinted.table_pages == (50,)


def test_pdf_page_is_drawn_in_the_disposable_child(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=300, height=400)
    pdf = tmp_path / "manual.pdf"
    with pdf.open("wb") as handle:
        writer.write(handle)
    script = ROOT / "agent" / "tools" / "feishu_knowledge_pdf.py"
    output = subprocess.run([sys.executable, "-I", str(script), "--render", "2", str(pdf)], capture_output=True,
                            text=True, timeout=60, check=True).stdout
    data = json.loads(output)["data"]
    assert data["page"] == 2 and base64.b64decode(data["png_base64"]).startswith(b"\x89PNG")
    missing = json.loads(subprocess.run([sys.executable, "-I", str(script), "--render", "9", str(pdf)],
                                        capture_output=True, text=True, timeout=60).stdout)
    assert missing == {"code": "KNOWLEDGE_PDF_EXTRACTION_FAILED"}


def test_knowledge_source_returns_png_bytes_only():
    from tools.feishu_knowledge import FeishuKnowledgeSource, KnowledgeError

    class Cli:
        def __init__(self, data):
            self.data, self.calls = data, []

        def render_pdf_page(self, document_id, page, *, deadline):
            self.calls.append((document_id, page))
            return {"data": self.data}

    good = Cli({"page": 3, "png_base64": base64.b64encode(PNG).decode()})
    assert FeishuKnowledgeSource(cli=good).page_image("boxcnPdfToken0001", 3) == PNG
    assert good.calls == [("boxcnPdfToken0001", 3)]
    for data in ({"page": 4, "png_base64": base64.b64encode(PNG).decode()}, {"page": 3, "png_base64": "bm90LXBuZw=="}):
        with pytest.raises(KnowledgeError):
            FeishuKnowledgeSource(cli=Cli(data)).page_image("boxcnPdfToken0001", 3)


# -- channels -----------------------------------------------------------------------------------

IMAGE = PageImage("boxcnPdfToken0001", 51, "《测试网络管理手册》第 51 页", "这一页的文字如下：……")


def _run(coroutine):
    return asyncio.run(coroutine)


def test_feishu_and_wechat_pictures_and_plain_failure_messages(monkeypatch):
    from feishu import reply_images

    texts, feishu, wechat = [], [], []

    async def send_text(receive_id, text):
        texts.append((receive_id, text))

    async def wechat_sender(receive_id, image):
        wechat.append((receive_id, image))

    monkeypatch.setattr(reply_images, "_send_feishu_image_sync", lambda receive_id, image: feishu.append((receive_id, image)))
    reply_images.bind_page_renderer(lambda document_id, page: PNG)
    reply_images.bind_channel_image_sender("wechat:", wechat_sender)
    try:
        assert _run(reply_images.send_reply_images("oc_chat", [IMAGE], send_text=send_text)) == 1
        assert _run(reply_images.send_reply_images("wechat:dev-1", [IMAGE], send_text=send_text)) == 1
        assert feishu == [("oc_chat", PNG)] and wechat == [("wechat:dev-1", PNG)] and texts == []

        def broken(document_id, page):
            raise RuntimeError("飞书 PDF 下载失败")
        reply_images.bind_page_renderer(broken)
        assert _run(reply_images.send_reply_images("oc_chat", [IMAGE], send_text=send_text)) == 0
        assert texts == [("oc_chat", "《测试网络管理手册》第 51 页的截图没有发出：飞书 PDF 下载失败。\n这一页的文字如下：……")]
    finally:
        reply_images.bind_page_renderer(None)
        reply_images.bind_channel_image_sender("wechat:", None)


def test_wechat_image_follows_the_official_upload_protocol():
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    from agent.wechat_bot.ilink import ILinkClient

    seen = {}

    def handler(request):
        if request.url.path.endswith("/ilink/bot/getuploadurl"):
            seen["ticket"] = json.loads(request.content)
            return httpx.Response(200, json={"upload_full_url": "https://novac2c.cdn.weixin.qq.com/c2c/upload?x=1"})
        if request.url.host == "novac2c.cdn.weixin.qq.com":
            seen["cipher"] = request.content
            return httpx.Response(200, headers={"x-encrypted-param": "download-param"})
        if request.url.path.endswith("/ilink/bot/sendmessage"):
            seen["message"] = json.loads(request.content)["msg"]
            return httpx.Response(200, json={"ret": 0})
        raise AssertionError(request.url)

    client = ILinkClient(transport=httpx.MockTransport(handler))
    _run(client.send_image("https://ilinkai.weixin.qq.com", "bot-token", to_user_id="wx-owner", image=PNG,
                           context_token="ctx-1"))
    ticket, message = seen["ticket"], seen["message"]
    assert (ticket["media_type"], ticket["to_user_id"], ticket["rawsize"]) == (1, "wx-owner", len(PNG))
    assert ticket["rawfilemd5"] == hashlib.md5(PNG).hexdigest() and ticket["no_need_thumb"] is True
    assert ticket["filesize"] == len(seen["cipher"]) == (len(PNG) // 16 + 1) * 16
    key = bytes.fromhex(ticket["aeskey"])
    assert unpad(AES.new(key, AES.MODE_ECB).decrypt(seen["cipher"]), 16) == PNG
    item = message["item_list"][0]
    assert item["type"] == 2 and message["context_token"] == "ctx-1" and message["to_user_id"] == "wx-owner"
    media = item["image_item"]["media"]
    assert media["encrypt_query_param"] == "download-param" and media["encrypt_type"] == 1
    assert base64.b64decode(media["aes_key"]).decode() == ticket["aeskey"]  # base64 of the hex text
    assert item["image_item"]["mid_size"] == len(seen["cipher"])


def test_wechat_upload_url_must_be_wechat_https_or_built_from_the_param():
    from agent.wechat_bot.ilink import ILinkError, cdn_upload_url

    assert cdn_upload_url({"upload_param": "p/1"}, "abc") == \
        "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=p%2F1&filekey=abc"
    for bad in ({"upload_full_url": "http://novac2c.cdn.weixin.qq.com/x"}, {"upload_full_url": "https://evil.example/x"},
                {}):
        with pytest.raises(ILinkError):
            cdn_upload_url(bad, "abc")


def test_ai_assistant_gets_data_urls_or_the_reason():
    from agent.harness_api import page_images

    ok = page_images([IMAGE], lambda document_id, page: PNG)
    assert ok == [{"caption": IMAGE.caption, "data_url": "data:image/png;base64," + base64.b64encode(PNG).decode()}]
    failed = page_images([IMAGE], None)
    assert failed[0]["error"] == "PDF 截图尚未配置" and failed[0]["fallback"] == IMAGE.fallback


def test_attachments_ride_with_the_reply_to_the_channels():
    from uuid import uuid4

    from agent.channel_chat import reply_to_feishu
    from agent.harness.sidecar import SidecarResult
    from agent.harness.sessions import InMemoryHarnessSessionRepository
    from agent.harness_application import HarnessConversationService
    from agent.orchestration.models import Actor, ActorType

    actor = Actor(ActorType.FEISHU_USER, "ou-clerk", ("admin", "super_admin"), authenticated_by="feishu_admin_binding")

    class Sidecar:
        def run(self, **_):
            return SidecarResult("以下是……（表格页，原文见下方截图）：", 2, attachments=(IMAGE,))

    service = HarnessConversationService(repository=InMemoryHarnessSessionRepository(),
                                         sidecar_factory=lambda actor, request_id: Sidecar(), timeout_seconds=5)
    result = reply_to_feishu(service, actor=actor, chat_id="oc_chat", event_id=str(uuid4()), message="收费标准表")
    assert result["attachments"] == (IMAGE,) and result["replayed"] is False
