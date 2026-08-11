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


def unwrap_versioned_task_response(payload: object) -> dict:
    """Remove the shared API envelope while preserving the inner task payload."""
    if not isinstance(payload, dict):
        return {"error": "internal service returned a non-object response"}

    inner = payload.get("data")
    is_versioned_envelope = {"ok", "data", "error"}.issubset(payload)
    is_task_payload = isinstance(inner, dict) and "ok" in inner and (
        "data" in inner or "error" in inner
    )
    if is_versioned_envelope and is_task_payload:
        return inner
    return payload
