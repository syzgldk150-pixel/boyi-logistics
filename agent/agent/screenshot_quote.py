"""Quote from a screenshot (收尾 截图报价): read the picture, then hand the usual quote text on.

The active model reads the delivery address (and any written weight / volume)
through the shared AI task gateway (``shared.screenshot_quote.READ_SCREENSHOT``).
With a weight, the caller runs the normal ``报价 地址，重量[，体积]`` text; without
one, the address waits here for 15 minutes for the same person's reply. A reading
with doubts (a weight range, an unclear piece count) is shown and waits for
"按这个报" (or a corrected weight) instead of quoting at once. A plain
weight ("300公斤6个方") is read by code; any other wording is understood by the
active model (``quote.read_follow_up``: give the weight, correct the address,
cancel, or something else), whose numbers and address must appear in the reply. This
module has no Feishu or tool imports; channels call it through
``feishu/screenshot_quote.py``.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from agent.agent_actions.pending import PendingConfirmations
from shared.ai_tasks import AITaskError
from shared.screenshot_quote import (
    CANCELLED_REPLY, MAX_IMAGE_BYTES, NO_ADDRESS_REPLY, PENDING_TTL_SECONDS, READ_SCREENSHOT, PendingQuote,
    ask_weight_reply, confirm_reply, follow_up_task, is_go_ahead, quote_text, quoting_reply,
)

# A follow-up that gives the weight, optionally with a volume, in the ways people type it:
# "300", "300公斤", "300kg 2方", "2方，300", "300公斤6个方", "重量300公斤 体积6立方米左右".
_NUMBER = r"\d+(?:\.\d+)?"
_VOLUME_TOKEN = re.compile(rf"(?P<value>{_NUMBER})\s*(?:个\s*)?(?:立方米|立方|方|m3|m³)", re.IGNORECASE)
_WEIGHT_TOKEN = re.compile(rf"(?P<value>{_NUMBER})\s*(?:公斤|千克|kg)", re.IGNORECASE)
_BARE_NUMBER = re.compile(rf"^(?P<value>{_NUMBER})$")
# What may surround the numbers without changing their meaning; anything else is not a weight reply.
_FILLER = re.compile(r"[\s，,、；;。.:：]|重量|体积|大概|大约|约|左右|一共|总共|共|是|货|毛重")


def _weight_and_volume(text: str) -> tuple[str, str | None] | None:
    """``(weight_kg, volume_m3)`` when ``text`` only gives a weight and maybe one volume; otherwise ``None``."""
    raw = str(text or "").strip()
    if not raw or len(raw) > 40:
        return None
    volumes = list(_VOLUME_TOKEN.finditer(raw))
    if len(volumes) > 1:
        return None
    rest = raw
    volume = None
    if volumes:
        volume = volumes[0].group("value")
        rest = rest[:volumes[0].start()] + " " + rest[volumes[0].end():]
    weights = list(_WEIGHT_TOKEN.finditer(rest))
    if len(weights) > 1:
        return None
    if weights:
        weight = weights[0].group("value")
        rest = rest[:weights[0].start()] + " " + rest[weights[0].end():]
        return (weight, volume) if not _FILLER.sub("", rest) else None
    # Without a unit the one remaining number is the weight ("300", "300 6方").
    bare = _BARE_NUMBER.match(_FILLER.sub("", rest))
    return (bare.group("value"), volume) if bare else None


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
        basis, doubts = tuple(reading.get("basis") or ()), tuple(reading.get("doubts") or ())
        if not address:
            self._pending.take(key)
            return ScreenshotOutcome(NO_ADDRESS_REPLY)
        if weight is None:
            self._pending.remember(key, PendingQuote(address, volume, basis=basis))
            return ScreenshotOutcome(ask_weight_reply(address, volume, basis, doubts))
        if doubts:  # unsure: show what was read and let the person say "按这个报" or correct it
            self._pending.remember(key, PendingQuote(address, volume, weight, basis))
            return ScreenshotOutcome(confirm_reply(address, weight, volume, basis, doubts))
        self._pending.take(key)
        return ScreenshotOutcome(quoting_reply(address, weight, volume, basis), quote_text(address, weight, volume))

    async def follow_up(self, key: str, text: str, *, actor_id: str) -> ScreenshotOutcome | None:
        """What a reply means for a waiting address; ``None`` sends it on as an ordinary message.

        ``quote_text`` set: run that quote (``reply`` may say what was understood). Only ``reply``:
        answered here, nothing else runs. The address stays waiting for anything unrelated.
        """
        pending = self._pending.peek(key)
        raw = str(text or "").strip()
        if pending is None or not raw:
            return None
        if pending.weight is not None and is_go_ahead(raw):
            composed = _composed(pending.address, pending.weight, pending.volume)
            if composed is not None:
                self._pending.take(key)
                return ScreenshotOutcome(quoting_reply(pending.address, pending.weight, pending.volume, pending.basis),
                                         composed)
        quick = _weight_and_volume(raw)
        if quick is not None:
            composed = _composed(pending.address, quick[0], quick[1] or pending.volume)
            if composed is not None:
                self._pending.take(key)
                return ScreenshotOutcome("", composed)
        if _belongs_elsewhere(raw):
            if _is_cancel(raw):  # "取消" also clears the waiting address, and still reaches its own route
                self._pending.take(key)
            return None
        try:
            reading = (await self._gateway.run(
                follow_up_task(raw),
                {"waiting_address": pending.address, "waiting_volume_m3": pending.volume, "reply": raw},
                subject_ref=f"screenshot-follow-up:{key}", actor_id=actor_id)).result
        except AITaskError:
            return None  # not understood: an ordinary message, the address keeps waiting
        if self._pending.peek(key) != pending:
            return None  # answered or expired meanwhile
        intent = reading["intent"]
        if intent == "cancel":
            self._pending.take(key)
            return ScreenshotOutcome(CANCELLED_REPLY)
        address = reading["address"] or pending.address
        volume = reading["volume_m3"] or pending.volume
        if intent == "address":
            self._pending.remember(key, replace(pending, address=address, volume=volume))
            if pending.weight is not None:
                return ScreenshotOutcome(confirm_reply(address, pending.weight, volume, pending.basis, ("收货地址已按你说的改了",)))
            return ScreenshotOutcome(ask_weight_reply(address, volume, pending.basis))
        if intent != "quote":
            return None
        composed = _composed(address, reading["weight_kg"], volume)
        if composed is None:
            return None
        self._pending.take(key)
        return ScreenshotOutcome(quoting_reply(address, reading["weight_kg"], volume), composed)


def _composed(address: str, weight: str, volume: str | None) -> str | None:
    """The quote text, when the quote route's own parser accepts it."""
    from agent.direct_tool_router import parse_price_request

    text = quote_text(address, weight.strip(), volume.strip() if volume else None)
    return text if parse_price_request(text) is not None else None


def _is_cancel(text: str) -> bool:
    from agent.direct_tool_router import is_cancel_text

    return is_cancel_text(text)


def _belongs_elsewhere(text: str) -> bool:
    """Replies another route owns exactly; they never go to the model or change the waiting quote."""
    from agent.direct_tool_router import (
        direct_tool_request_from_text, is_confirm_text, is_reserved_feishu_command_text, parse_price_request,
    )

    if len(text) > 200 or is_confirm_text(text) or _is_cancel(text) or parse_price_request(text) is not None:
        return True
    request = direct_tool_request_from_text(text)
    # A reply the quote route only half-recognises ("地址改成…，一共1.5吨" → "请提供完整收货地址和重量")
    # is exactly what the model should read; any other recognised command keeps its own route.
    local = request.get("local_result") if isinstance(request, dict) else None
    if (isinstance(request, dict) and request.get("mode") == "service_v2_quote"
            and isinstance(local, dict) and local.get("success") is False):
        return False
    return request is not None or is_reserved_feishu_command_text(text)


__all__ = ["ScreenshotOutcome", "ScreenshotQuoteService"]
