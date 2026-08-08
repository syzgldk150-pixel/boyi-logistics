"""回复格式化：将 Agent 结果格式化为飞书消息"""

from __future__ import annotations

import re


_FENCE_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.*?)\*\*|__(.*?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)|(?<!_)_(?!_)(.*?)(?<!_)_(?!_)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_QUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
_LIST_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)


def format_reply(text: str) -> str:
    """格式化回复文本（后续可扩展为卡片消息）"""
    if not text:
        return "处理完成"

    text = _strip_markdown(text)
    # 限制长度，飞书文本消息最大 4096 字节
    if len(text.encode("utf-8")) > 4000:
        text = text[:1500] + "\n\n...(内容过长已截断)"
    return text


def _strip_markdown(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = _FENCE_RE.sub(lambda match: match.group(1).strip(), normalized)
    normalized = _HEADING_RE.sub("", normalized)
    normalized = _BOLD_RE.sub(lambda match: match.group(1) or match.group(2) or "", normalized)
    normalized = _ITALIC_RE.sub(lambda match: match.group(1) or match.group(2) or "", normalized)
    normalized = _INLINE_CODE_RE.sub(lambda match: match.group(1), normalized)
    normalized = _QUOTE_RE.sub("", normalized)
    normalized = _LIST_RE.sub("- ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()
