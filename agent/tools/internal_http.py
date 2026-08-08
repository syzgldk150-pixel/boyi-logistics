"""Authentication headers for calls to the local Agent HTTP service."""

from __future__ import annotations

import os


INTERNAL_API_TOKEN_HEADER = "X-Agent-Internal-Token"


class InternalApiTokenNotConfigured(RuntimeError):
    pass


def internal_api_headers() -> dict[str, str]:
    token = str(os.getenv("AGENT_INTERNAL_API_TOKEN", "") or "").strip()
    if not token:
        raise InternalApiTokenNotConfigured("AGENT_INTERNAL_API_TOKEN is not configured")
    return {INTERNAL_API_TOKEN_HEADER: token}
