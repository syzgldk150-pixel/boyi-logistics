"""Closed value objects for a user-owned Harness conversation."""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from agent.harness.errors import HarnessError


_MAX_MESSAGE_CHARS = 4_000
_MAX_MESSAGES_PER_SESSION = 64
_ALLOWED_ROLES = frozenset({"user", "assistant", "tool"})


def canonical_uuid(value: object, *, field_name: str) -> str:
    """Return one canonical UUID string or reject non-canonical variants."""

    if not isinstance(value, str):
        raise HarnessError(f"{field_name} must be a canonical UUID", code="HARNESS_ID_INVALID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise HarnessError(f"{field_name} must be a canonical UUID", code="HARNESS_ID_INVALID") from exc
    if str(parsed) != value:
        raise HarnessError(f"{field_name} must be a canonical UUID", code="HARNESS_ID_INVALID")
    return value


def exact_principal(value: object) -> str:
    """Validate a signed-principal identifier without normalizing identity."""

    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise HarnessError("signed principal is invalid", code="HARNESS_PRINCIPAL_INVALID")
    return value


def strict_json(value: object, *, field_name: str) -> Any:
    """Round-trip a finite JSON value and return an independent copy."""

    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise HarnessError(f"{field_name} must be strict JSON", code="HARNESS_PROTOCOL_INVALID") from exc


@dataclass(frozen=True)
class HarnessMessage:
    """One bounded, JSON-only conversation message."""

    role: str
    content: str
    message_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.role not in _ALLOWED_ROLES:
            raise HarnessError("message role is invalid", code="HARNESS_PROTOCOL_INVALID")
        if not isinstance(self.content, str) or len(self.content) > _MAX_MESSAGE_CHARS:
            raise HarnessError("message content is invalid", code="HARNESS_MESSAGE_INVALID")
        canonical_uuid(self.message_id, field_name="message_id")
        if self.created_at.tzinfo is None:
            raise HarnessError("message timestamp must be timezone-aware", code="HARNESS_MESSAGE_INVALID")


@dataclass(frozen=True)
class ToolCall:
    """The deliberately small model-to-host tool call wire shape."""

    tool_id: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_id, str) or not self.tool_id or len(self.tool_id) > 160:
            raise HarnessError("tool identifier is invalid", code="HARNESS_TOOL_INVALID")
        if not isinstance(self.arguments, Mapping):
            raise HarnessError("tool arguments must be a JSON object", code="HARNESS_PROTOCOL_INVALID")
        object.__setattr__(self, "arguments", strict_json(dict(self.arguments), field_name="tool arguments"))


@dataclass(frozen=True)
class HarnessSession:
    """In-memory only session state; it never claims durable persistence."""

    session_id: str
    principal_id: str
    messages: tuple[HarnessMessage, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        canonical_uuid(self.session_id, field_name="session_id")
        exact_principal(self.principal_id)
        if len(self.messages) > _MAX_MESSAGES_PER_SESSION:
            raise HarnessError("session message limit exceeded", code="HARNESS_LIMIT_EXCEEDED")
        if not all(isinstance(item, HarnessMessage) for item in self.messages):
            raise HarnessError("session messages are invalid", code="HARNESS_PROTOCOL_INVALID")
        if self.created_at.tzinfo is None:
            raise HarnessError("session timestamp must be timezone-aware", code="HARNESS_PROTOCOL_INVALID")

    @property
    def persistence_status(self) -> str:
        """Expose the explicit process-memory persistence guarantee."""

        return "MEMORY_ONLY"

    def append(self, message: HarnessMessage) -> "HarnessSession":
        if len(self.messages) >= _MAX_MESSAGES_PER_SESSION:
            raise HarnessError("session message limit exceeded", code="HARNESS_LIMIT_EXCEEDED")
        return HarnessSession(
            session_id=self.session_id,
            principal_id=self.principal_id,
            messages=(*self.messages, copy.deepcopy(message)),
            created_at=self.created_at,
        )


__all__ = [
    "HarnessMessage",
    "HarnessSession",
    "ToolCall",
    "canonical_uuid",
    "exact_principal",
    "strict_json",
]
