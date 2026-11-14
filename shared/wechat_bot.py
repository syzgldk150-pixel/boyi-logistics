"""Rules for WeChat personal-account bots (official iLink Bot API).

Each bound device is one WeChat account that scanned a QR code generated in
the Console. Its messages enter the same chat pipeline as Feishu under the
actor id ``wechat:<device_id>``, so every query, plugin call and AI reply is
authorised by the same live identity lookup. A device may additionally narrow
the identity's permissions to a chosen subset; it can never widen them.

This module has no I/O and reads no environment.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Iterable, Mapping

from shared.identity_permissions import AGENT, AGENT_CAPABILITIES, IdentityAccess

WECHAT_ACTOR_PREFIX = "wechat:"
_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# iLink message protocol values (official plugin api/types.ts).
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
# The official plugin splits outgoing text at 4000 characters.
TEXT_CHUNK_LIMIT = 4000

CONNECTION_STATES = {
    "connecting": "连接中",
    "online": "在线",
    "session_expired": "会话失效，需重新扫码",
    "error": "连接异常，自动重试中",
    "stopped": "已停止",
}

LABEL_MAX_LENGTH = 80


def wechat_actor_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not _DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("微信设备编号格式无效")
    return WECHAT_ACTOR_PREFIX + device_id


def parse_wechat_actor_id(actor_id: Any) -> str | None:
    """Return the device id for a WeChat actor id, otherwise ``None``."""
    if not isinstance(actor_id, str) or not actor_id.startswith(WECHAT_ACTOR_PREFIX):
        return None
    device_id = actor_id[len(WECHAT_ACTOR_PREFIX):]
    return device_id if _DEVICE_ID_RE.fullmatch(device_id) else None


def is_wechat_actor_id(actor_id: Any) -> bool:
    return isinstance(actor_id, str) and actor_id.startswith(WECHAT_ACTOR_PREFIX)


def validate_device_permissions(values: Any) -> tuple[str, ...] | None:
    """``None`` keeps the person's agent permissions; a list narrows them."""
    if values is None:
        return None
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or v not in AGENT_CAPABILITIES for v in values):
        raise ValueError("包含未知的机器人权限")
    result = tuple(sorted(set(values)))
    if "finance.manage" in result and "finance.read" not in result:
        raise ValueError("财务采集需要同时允许财务数据")
    return result


def validate_label(label: Any) -> str:
    if not isinstance(label, str) or len(label.strip()) > LABEL_MAX_LENGTH:
        raise ValueError("微信设备备注需为不超过 80 个字符的文本")
    return label.strip()


def restrict_access(access: IdentityAccess, allowed: Iterable[str] | None) -> IdentityAccess:
    """Intersect the person's live agent permissions with the device's limits."""
    access = access.for_channel(AGENT)
    if not access.active or allowed is None:
        return access
    base = ({key: access.agent_level(key) for key in AGENT_CAPABILITIES} if access.super_admin
            else dict(access.agent))
    keep = set(allowed)
    return replace(access, super_admin=False, role_name=f"{access.role_name}（设备限制）", console={},
                   agent={key: level for key, level in base.items() if key in keep})


def inbound_text(message: Mapping[str, Any]) -> str:
    """Text of one user message: typed text plus WeChat's voice transcription."""
    parts: list[str] = []
    for item in message.get("item_list") or ():
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text")
        elif kind == ITEM_VOICE:
            text = (item.get("voice_item") or {}).get("text")
        else:
            continue
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def has_unsupported_media(message: Mapping[str, Any]) -> bool:
    return any(
        isinstance(item, Mapping) and item.get("type") in {ITEM_IMAGE, ITEM_FILE, ITEM_VIDEO}
        for item in message.get("item_list") or ()
    )


def keep_line_breaks(text: str) -> str:
    """Keep the plain-text layout in WeChat, which renders replies as Markdown.

    Since ClawBot 2.4 the WeChat client renders bot text as Markdown, where a
    single line break inside a paragraph collapses into a space. Replies are
    shared plain text (one field per line), so each such break becomes a
    Markdown hard break (two trailing spaces); blank lines stay paragraphs.
    """
    lines = str(text or "").split("\n")
    out = []
    for index, line in enumerate(lines):
        line = line.rstrip()
        following = lines[index + 1] if index + 1 < len(lines) else ""
        out.append(line + "  " if line and following.strip() else line)
    return "\n".join(out)


def split_text(text: str, limit: int = TEXT_CHUNK_LIMIT) -> list[str]:
    """Split on line breaks where possible; never drop text."""
    text = str(text or "")
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [chunk.rstrip("\n") for chunk in chunks if chunk.strip()]
