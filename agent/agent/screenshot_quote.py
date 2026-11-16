"""Quote from a screenshot (收尾 截图报价): read the picture, then hand the usual quote text on.

The active model reads the delivery address (and any written weight / volume)
through the shared AI task gateway (``shared.screenshot_quote.READ_SCREENSHOT``).
With a weight, the caller runs the normal ``报价 地址，重量[，体积]`` text; without
one, the address waits here for 15 minutes for the same person's weight. This
module has no Feishu or tool imports; channels call it through
``feishu/screenshot_quote.py``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from agent.agent_actions.pending import PendingConfirmations
from shared.ai_tasks import AITaskError
from shared.screenshot_quote import (
    MAX_IMAGE_BYTES, NO_ADDRESS_REPLY, PENDING_TTL_SECONDS, READ_SCREENSHOT, PendingQuote, ask_weight_reply,
    quote_text, quoting_reply,
)

# A follow-up that only gives the weight, optionally with a volume ("300", "300公斤", "300kg 2方", "2方，300").
_WEIGHT = r"(?P<weight>\d+(?:\.\d+)?\s*(?:公斤|千克|kg)?)"
_VOLUME = r"(?P<volume>\d+(?:\.\d+)?\s*(?:方|立方|m3|m³))"
_FOLLOW_UP = re.compile(rf"^\s*(?:重量)?\s*{_WEIGHT}(?:\s*[，,、；;\s]\s*(?:体积)?\s*{_VOLUME})?\s*$"
                        rf"|^\s*(?:体积)?\s*{_VOLUME.replace('volume', 'volume2')}\s*[，,、；;\s]\s*(?:重量)?\s*"
                        rf"{_WEIGHT.replace('weight', 'weight2')}\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ScreenshotOutcome:
    """What to tell the person, and the quote text to run now (``None`` when a weight is still needed)."""
    reply: str
    quote_text: str | None = None


class ScreenshotQuoteService:
    def __init__(self, gateway: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._gateway = gateway
        self._pending: PendingConfirmations[PendingQuote] = PendingConfirmations(PENDING_TTL_SECONDS, clock=clock)

    async def read(self, key: str, image: bytes, *, actor_id: str) -> ScreenshotOutcome:
        if not isinstance(image, bytes) or not 0 < len(image) <= MAX_IMAGE_BYTES:
            return ScreenshotOutcome("图片为空或太大（超过 8MB），请换一张截图。")
        try:
            reading = (await self._gateway.run(READ_SCREENSHOT, {"task": "从截图中找出收货地址、重量和体积"},
                                               subject_ref=f"screenshot:{key}", actor_id=actor_id, images=(image,))).result
        except AITaskError as exc:
            return ScreenshotOutcome(f"截图没有识别成功：{exc.message}可以直接发“报价 地址，重量”。")
        address, weight, volume = reading["address"], reading["weight_kg"], reading["volume_m3"]
        if not address:
            self._pending.take(key)
            return ScreenshotOutcome(NO_ADDRESS_REPLY)
        if weight is None:
            self._pending.remember(key, PendingQuote(address, volume))
            return ScreenshotOutcome(ask_weight_reply(address, volume))
        self._pending.take(key)
        return ScreenshotOutcome(quoting_reply(address, weight, volume), quote_text(address, weight, volume))

    def follow_up(self, key: str, text: str) -> str | None:
        """The quote text when ``text`` only gives the weight for a waiting address; otherwise ``None``."""
        from agent.direct_tool_router import parse_price_request

        pending = self._pending.peek(key)
        match = _FOLLOW_UP.match(str(text or "")) if pending is not None else None
        if not match:
            return None
        weight = match.group("weight") or match.group("weight2")
        volume = match.group("volume") or match.group("volume2") or pending.volume
        composed = quote_text(pending.address, weight.strip(), volume.strip() if volume else None)
        if parse_price_request(composed) is None:  # the quote route's own parser decides what it accepts
            return None
        self._pending.take(key)
        return composed


__all__ = ["ScreenshotOutcome", "ScreenshotQuoteService"]
