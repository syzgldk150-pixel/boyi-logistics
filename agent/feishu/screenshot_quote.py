"""Quote from a screenshot in Feishu and WeChat (收尾 截图报价): the channel side.

A picture from a person with a valid identity allowed to run plugins is read by
the active model (``agent.screenshot_quote``); the recognised address is shown,
and the usual ``报价 地址，重量[，体积]`` text goes through the normal message
path, so the quote's permissions, plugin call and reply are unchanged. A
missing weight is asked and the next short reply with a weight finishes the
quote; a reply in other words is understood by the model and turned into the
same standard text. The service is bound by the composition root; this module never imports
the WeChat implementation (the WeChat channel hands over decrypted bytes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_SERVICE: Any | None = None
NOT_ENABLED_REPLY = "截图报价尚未启用，请直接发“报价 地址，重量”。"
NO_PERMISSION_REPLY = "当前身份不能使用截图报价（需要“执行插件”权限），请联系超级管理员。"


def bind_screenshot_quote(service: Any | None) -> None:
    global _SERVICE
    _SERVICE = service


def _key(chat_id: str, sender_id: str) -> str:
    return f"{chat_id}:{sender_id}"


async def _allowed(sender_id: str) -> bool:
    from feishu import message_handler

    access = getattr(message_handler._FEISHU_BINDING_RUNTIME, "identity_access", None)
    if access is None:
        return False
    identity = await asyncio.to_thread(access.feishu, str(sender_id or ""))
    return bool(identity.active and identity.allows("plugins.execute"))


def _download_feishu_image_sync(message_id: str, image_key: str) -> bytes:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    client = lark.Client.builder().app_id(os.getenv("FEISHU_APP_ID", "")).app_secret(
        os.getenv("FEISHU_APP_SECRET", "")).build()
    response = client.im.v1.message_resource.get(
        GetMessageResourceRequest.builder().message_id(message_id).file_key(image_key).type("image").build())
    if not response.success() or response.file is None:
        raise RuntimeError(f"飞书图片下载失败（code={getattr(response, 'code', '')}）")
    return response.file.read()


async def _read_and_quote(*, chat_id: str, sender_id: str, image: bytes, run_quote) -> None:
    from feishu import message_handler

    outcome = await _SERVICE.read(_key(chat_id, sender_id), image, actor_id=str(sender_id))
    await message_handler._reply_text(chat_id, outcome.reply, reply_type="screenshot_quote")
    if outcome.quote_text:
        await run_quote(outcome.quote_text)


async def handle_feishu_image(*, chat_id: str, sender_id: str, message_id: str, raw_content: Any) -> None:
    from feishu import message_handler

    if _SERVICE is None:
        await message_handler._reply_text(chat_id, NOT_ENABLED_REPLY)
        return
    if not await _allowed(sender_id):
        await message_handler._reply_text(chat_id, NO_PERMISSION_REPLY)
        return
    try:
        content = raw_content if isinstance(raw_content, dict) else json.loads(str(raw_content or "{}"))
        image_key = str(content.get("image_key") or "")
        if not message_id or not image_key:
            raise RuntimeError("消息里没有图片")
        image = await asyncio.to_thread(_download_feishu_image_sync, message_id, image_key)
    except Exception as exc:  # noqa: BLE001 - told to the person
        logger.warning("screenshot quote image not downloaded: %s", type(exc).__name__)
        await message_handler._reply_text(chat_id, f"图片没有下载成功：{str(exc)[:60]}。可以直接发“报价 地址，重量”。")
        return
    await _read_and_quote(chat_id=chat_id, sender_id=sender_id, image=image,
                          run_quote=lambda text: message_handler._process_and_reply(text, sender_id, chat_id))


async def handle_channel_image(*, actor_id: str, image: bytes, event_id: str) -> None:
    """Another one-to-one channel (WeChat) hands over a decrypted picture from its verified sender."""
    from feishu import message_handler

    if _SERVICE is None:
        await message_handler._reply_text(actor_id, NOT_ENABLED_REPLY)
        return
    if not await _allowed(actor_id):
        await message_handler._reply_text(actor_id, NO_PERMISSION_REPLY)
        return
    await _read_and_quote(chat_id=actor_id, sender_id=actor_id, image=image,
                          run_quote=lambda text: message_handler.handle_channel_text(
                              actor_id=actor_id, text=text, event_id=f"{event_id}:quote"))


async def follow_up_text(text: str, *, sender_id: str, chat_id: str) -> str | None:
    """The text to process next for a reply to a waiting screenshot address; ``None`` when answered here.

    A weight (in any wording the model understands) becomes the full quote text, shown first with what
    was understood; a corrected address asks for the weight again; a cancel ends it; anything else is
    returned unchanged as an ordinary message.
    """
    from feishu import message_handler

    if _SERVICE is None:
        return text
    outcome = await _SERVICE.follow_up(_key(chat_id, sender_id), text, actor_id=str(sender_id))
    if outcome is None:
        return text
    if outcome.reply:
        await message_handler._reply_text(chat_id, outcome.reply, reply_type="screenshot_quote")
    return outcome.quote_text


__all__ = ["bind_screenshot_quote", "follow_up_text", "handle_channel_image", "handle_feishu_image"]
