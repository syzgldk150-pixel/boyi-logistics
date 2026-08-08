"""Pure Agent HTTP authentication policy."""

from __future__ import annotations

import secrets
from dataclasses import dataclass


INTERNAL_API_TOKEN_HEADER = "X-Agent-Internal-Token"
PUBLIC_EXACT_PATHS = frozenset({"/health", "/feishu/webhook/event"})
PUBLIC_PATH_PREFIXES = ("/webhook/",)


@dataclass(frozen=True)
class AuthenticationFailure:
    status_code: int
    message: str


def is_public_path(path: str) -> bool:
    normalized = "/" + str(path or "").lstrip("/")
    if normalized in PUBLIC_EXACT_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def authenticate_internal_request(
    *,
    path: str,
    expected_token: str,
    provided_token: str,
) -> AuthenticationFailure | None:
    """Return a failure for protected requests, otherwise ``None``."""

    if is_public_path(path):
        return None
    expected = str(expected_token or "").strip()
    if not expected:
        return AuthenticationFailure(503, "Agent internal API token is not configured")
    provided = str(provided_token or "").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        return AuthenticationFailure(401, "Invalid or missing Agent internal API token")
    return None
