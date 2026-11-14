"""Application-root wiring of the WeChat personal-account bot channel.

The ``agent.wechat_bot`` package has no dependency on Feishu; this module joins
it to the shared chat pipeline in ``feishu.message_handler``: every verified
device message enters ``handle_channel_text`` with the actor id
``wechat:<device_id>`` and every reply to that id goes back through iLink.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from agent.wechat_bot.channel import WeChatChannelRuntime, WeChatConsumerLease
from agent.wechat_bot.ilink import ILinkClient
from agent.wechat_bot.service import WeChatBotService
from agent.wechat_bot.tokens import WeChatTokenStore
from feishu.message_handler import bind_channel_reply_sender, handle_channel_text
from shared.identity_repository import IdentityRepository
from shared.wechat_bot import WECHAT_ACTOR_PREFIX
from shared.wechat_bot_repository import WeChatDeviceRepository

logger = logging.getLogger("agent.wechat_bot")


class WeChatBotRuntime:
    def __init__(self, *, connect: Callable[[], Any], lease_connect: Callable[[], Any],
                 identities: IdentityRepository, tokens: WeChatTokenStore | None = None):
        self.client = ILinkClient()
        self.devices = WeChatDeviceRepository(connect)
        self.tokens = tokens or WeChatTokenStore()
        self.channel = WeChatChannelRuntime(
            devices=self.devices, tokens=self.tokens, client=self.client,
            lease=WeChatConsumerLease(lease_connect), deliver=self._deliver)
        self.service = WeChatBotService(
            devices=self.devices, identities=identities, client=self.client, tokens=self.tokens,
            on_bound=self.channel.device_bound, on_revoked=self.channel.device_revoked)

    @staticmethod
    async def _deliver(actor_id: str, text: str, event_id: str) -> None:
        await handle_channel_text(actor_id=actor_id, text=text, event_id=event_id)

    def start(self) -> None:
        bind_channel_reply_sender(WECHAT_ACTOR_PREFIX, self.channel.send)
        self.channel.start()
        logger.info("WeChat bot channel started")

    async def stop(self) -> None:
        await self.service.close()
        await self.channel.stop()
        bind_channel_reply_sender(WECHAT_ACTOR_PREFIX, None)
        await self.client.close()
        logger.info("WeChat bot channel stopped")
