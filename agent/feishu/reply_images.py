"""Send table pages of a knowledge answer as pictures (业务手册 收尾第 4b 步).

A knowledge answer quotes the document's own words; a table's text layer comes
out scrambled, so the page picture is sent instead. The conversation hands over
``knowledge_answers.PageImage`` items; this module renders each page through the
renderer bound by the composition root and sends it to Feishu, or through the
image sender another channel (WeChat) registered for its actor prefix. When a
picture cannot be made or sent, the person gets the page text and the reason —
never silence.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

PageRenderer = Callable[[str, int], bytes]
ImageSender = Callable[[str, bytes], Awaitable[None]]
TextSender = Callable[[str, str], Awaitable[Any]]

_PAGE_RENDERER: PageRenderer | None = None
_CHANNEL_IMAGE_SENDERS: dict[str, ImageSender] = {}


def bind_page_renderer(renderer: PageRenderer | None) -> None:
    global _PAGE_RENDERER
    if renderer is not None and not callable(renderer):
        raise TypeError("renderer must be callable")
    _PAGE_RENDERER = renderer


def render_page(document_id: str, page: int) -> bytes:
    """The bound renderer, for the AI assistant page (it shows the picture inline)."""
    if _PAGE_RENDERER is None:
        raise RuntimeError("PDF 截图尚未配置")
    return _PAGE_RENDERER(document_id, page)


def bind_channel_image_sender(prefix: str, sender: ImageSender | None) -> None:
    """Register (or with ``None`` remove) the picture sender of another chat channel."""
    if not isinstance(prefix, str) or not prefix.endswith(":") or prefix == "feishu:":
        raise ValueError("channel prefix must end with ':'")
    if sender is None:
        _CHANNEL_IMAGE_SENDERS.pop(prefix, None)
        return
    if not callable(sender):
        raise TypeError("sender must be callable")
    _CHANNEL_IMAGE_SENDERS[prefix] = sender


def _send_feishu_image_sync(receive_id: str, image: bytes, receive_id_type: str = "chat_id") -> None:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateImageRequest, CreateImageRequestBody, CreateMessageRequest, CreateMessageRequestBody,
    )

    client = lark.Client.builder().app_id(os.getenv("FEISHU_APP_ID", "")).app_secret(
        os.getenv("FEISHU_APP_SECRET", "")).build()
    uploaded = client.im.v1.image.create(CreateImageRequest.builder().request_body(
        CreateImageRequestBody.builder().image_type("message").image(io.BytesIO(image)).build()).build())
    image_key = getattr(getattr(uploaded, "data", None), "image_key", None) if uploaded.success() else None
    if not image_key:
        raise RuntimeError(f"飞书图片上传失败（code={getattr(uploaded, 'code', '')}）")
    sent = client.im.v1.message.create(CreateMessageRequest.builder().receive_id_type(receive_id_type).request_body(
        CreateMessageRequestBody.builder().receive_id(receive_id).msg_type("image")
        .content(json.dumps({"image_key": image_key})).build()).build())
    if not sent.success():
        raise RuntimeError(f"飞书图片发送失败（code={sent.code}）")


async def _send_image(receive_id: str, image: bytes) -> None:
    for prefix, sender in _CHANNEL_IMAGE_SENDERS.items():
        if receive_id.startswith(prefix):
            await sender(receive_id, image)
            return
    await asyncio.to_thread(_send_feishu_image_sync, receive_id, image)


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:80] if text and len(text) < 200 and "\n" not in text else type(exc).__name__


async def send_reply_images(receive_id: str, attachments: Iterable[Any], *, send_text: TextSender) -> int:
    """Send each table page; returns how many pictures went out. Failures are told to the person."""
    sent = 0
    for item in list(attachments or ())[:2]:
        page, document_id = getattr(item, "page", None), getattr(item, "document_id", None)
        caption, fallback = str(getattr(item, "caption", "") or "这一页"), str(getattr(item, "fallback", "") or "")
        if type(page) is not int or not isinstance(document_id, str):
            continue
        try:
            if _PAGE_RENDERER is None:
                raise RuntimeError("PDF 截图尚未配置")
            image = await asyncio.to_thread(_PAGE_RENDERER, document_id, page)
            await _send_image(str(receive_id), image)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - the person is told and gets the text instead
            logger.warning("knowledge page image not sent | page=%s | reason=%s", page, type(exc).__name__)
            await send_text(str(receive_id), f"{caption}的截图没有发出：{_reason(exc)}。\n{fallback}".strip())
    return sent


__all__ = ["bind_channel_image_sender", "bind_page_renderer", "render_page", "send_reply_images"]
