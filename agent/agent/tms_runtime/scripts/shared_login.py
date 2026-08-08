"""Helpers for scripts that now rely on the shared TMS login broker."""

from __future__ import annotations

import os

from agent.tms_runtime.session_broker import get_session_broker


def resolve_primary_credentials(username: str = "", password: str = "") -> tuple[str, str]:
    if username and password:
        return str(username).strip(), str(password).strip()

    config = get_session_broker().resolve_login_config()
    resolved_username = (
        username
        or config.username
        or os.getenv("TMS_USERNAME", "")
        or "shared-session"
    )
    resolved_password = (
        password
        or config.password
        or os.getenv("TMS_PASSWORD", "")
        or "shared-session"
    )
    return str(resolved_username).strip(), str(resolved_password).strip()


def load_named_accounts(keys: list[str]) -> list[tuple[str, str]]:
    if not keys:
        return []
    return [resolve_primary_credentials()]
