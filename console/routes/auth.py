"""Console administrator authentication and account-management routes."""

from __future__ import annotations

from typing import Any


def handle_get(app: Any, handler: Any, path: str, _raw_path: str, query: dict[str, list[str]]) -> bool:
    if path == "/settings/accounts":
        app._render_admin_accounts(handler, query)
        return True
    return False


def handle_post(app: Any, handler: Any, path: str, _raw_path: str, _query: dict[str, list[str]]) -> bool:
    if path == "/logout":
        app._handle_logout(handler)
        return True
    if path == "/settings/profile/avatar":
        app._handle_admin_avatar_upload(handler)
        return True
    if path == "/settings/profile/mobile-navigation":
        app._handle_mobile_navigation_save(handler)
        return True
    if path == "/settings/accounts/create":
        app._handle_admin_account_create(handler)
        return True
    if path.startswith("/settings/accounts/") and path.endswith("/toggle"):
        app._handle_admin_account_toggle(handler, path)
        return True
    if path.startswith("/settings/accounts/") and path.endswith("/reset-password"):
        app._handle_admin_account_reset_password(handler, path)
        return True
    return False
