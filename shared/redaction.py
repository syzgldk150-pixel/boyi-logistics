"""Pure recursive redaction helpers for logs, audits, and diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


REDACTED = "[REDACTED]"
MAX_REDACTION_DEPTH = 20

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|authentication|cookie|credential|password|passwd|passphrase|secret|"
    r"token|capability|api[_-]?key|access[_-]?key|private[_-]?key|sso|storage[_-]?state|"
    r"session[_-]?(?:state|id)|"
    r"request[_-]?body|raw[_-]?request)",
    re.IGNORECASE,
)
_AUTH_SCHEME = re.compile(
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_HEADER_VALUE = re.compile(
    r"\b(Authorization|Proxy-Authorization)\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_COOKIE_HEADER = re.compile(
    r"\b(Cookie|Set-Cookie)\s*([:=])\s*[^\r\n]+",
    re.IGNORECASE,
)
_QUOTED_ASSIGNMENT = re.compile(
    r"(?P<prefix>[\"']?(?:password|passwd|passphrase|secret|token|(?:execution[_-]?)?capability|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|authorization|authentication(?:[_-]?key)?|cookie)"
    r"[\"']?\s*[:=]\s*)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?P<prefix>[\"']?(?:password|passwd|passphrase|secret|token|(?:execution[_-]?)?capability|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|authorization|authentication(?:[_-]?key)?|cookie)"
    r"[\"']?\s*[:=]\s*)(?P<value>[^\s,;}\"']+)",
    re.IGNORECASE,
)
_QUERY_VALUE = re.compile(
    r"(?P<prefix>[?&](?:access_token|token|capability|execution_capability|api_key|access_key|authenticationKey|key|secret|password)=)"
    r"[^&#\s]+",
    re.IGNORECASE,
)
_KNOWN_SECRET = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16})\b"
)


def is_sensitive_key(key: Any) -> bool:
    """Return whether a field name represents secret or raw request material."""

    return bool(_SENSITIVE_KEY.search(str(key or "")))


def redact_text(value: Any) -> str:
    """Redact common credential forms without logging the original value."""

    text = str(value or "")
    text = _AUTH_SCHEME.sub(REDACTED, text)
    text = _HEADER_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    text = _COOKIE_HEADER.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    text = _QUOTED_ASSIGNMENT.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    text = _QUERY_VALUE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)
    return _KNOWN_SECRET.sub(REDACTED, text)


def redact_sensitive(
    value: Any,
    *,
    key: Any = None,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Recursively redact sensitive fields while preserving non-secret structure."""

    if is_sensitive_key(key):
        return REDACTED
    if _depth >= MAX_REDACTION_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return "[REDACTED BYTES]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, BaseException):
        return redact_text(value)

    seen = _seen if _seen is not None else set()
    object_id = id(value)
    if object_id in seen:
        return "[CYCLE]"

    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            return {
                str(item_key): redact_sensitive(
                    item_value,
                    key=item_key,
                    _depth=_depth + 1,
                    _seen=seen,
                )
                for item_key, item_value in value.items()
            }
        finally:
            seen.discard(object_id)

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(object_id)
        try:
            return [
                redact_sensitive(item, _depth=_depth + 1, _seen=seen)
                for item in value
            ]
        finally:
            seen.discard(object_id)

    return redact_text(value)
