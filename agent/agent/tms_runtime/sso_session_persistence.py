"""Shared persistence for R7/R13 SSO sessions.

The stored token and cookies are runtime authentication state. They stay under
the ignored TMS state directory and are never returned by account APIs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import requests


MAX_SSO_LOGIN_ATTEMPTS = 3


def default_sso_state_path(account_id: str) -> Path:
    safe_id = "".join(
        char if char.isalnum() or char in {"_", "-"} else "_"
        for char in str(account_id or "").strip().lower()
    ).strip("_")
    if not safe_id:
        raise ValueError("account_id is required for SSO session state")
    return Path(__file__).resolve().parent / "state" / "automation_account_credentials" / safe_id / "sso_session.json"


def _now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _cookie_payload(session: requests.Session) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for cookie in session.cookies:
        cookies.append(
            {
                "name": str(cookie.name),
                "value": str(cookie.value),
                "domain": str(cookie.domain or ""),
                "path": str(cookie.path or "/"),
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
            }
        )
    return cookies


def _restore_cookies(session: requests.Session, cookies: Any) -> None:
    if not isinstance(cookies, list):
        return
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        kwargs: dict[str, Any] = {
            "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", False)),
        }
        domain = str(item.get("domain") or "").strip()
        if domain:
            kwargs["domain"] = domain
        expires = item.get("expires")
        if isinstance(expires, (int, float)):
            kwargs["expires"] = int(expires)
        session.cookies.set(name, str(item.get("value") or ""), **kwargs)


class SSOSessionPersistenceMixin:
    """Mixin used by the R7 and R13 SSO clients."""

    session: requests.Session
    last_token: str | None
    state_path: Path

    def _load_sso_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_sso_state(self, *, status: str, error: str = "") -> dict[str, Any]:
        previous = self._load_sso_state()
        now = _now_label()
        payload = {
            "status": str(status or "logged_out"),
            "token": str(self.last_token or "") if status == "authenticated" else "",
            "cookies": _cookie_payload(self.session) if status == "authenticated" else [],
            "authenticated_at": (
                str(previous.get("authenticated_at") or now) if status == "authenticated" else ""
            ),
            "last_validation_at": now,
            "last_error_summary": str(error or "")[:500],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.state_path, 0o600)
        except OSError:
            pass
        return payload

    def clear_persisted_session(self) -> None:
        self.last_token = None
        self.session.headers.pop("Authorization", None)
        self.session.cookies.clear()
        self.state_path.unlink(missing_ok=True)

    def restore_persisted_session(
        self,
        *,
        validate: bool,
        validator: Callable[[], bool],
        attach_bearer: bool,
    ) -> bool:
        payload = self._load_sso_state()
        token = str(payload.get("token") or "").strip()
        if str(payload.get("status") or "") != "authenticated" or not token:
            return False
        self.last_token = token
        _restore_cookies(self.session, payload.get("cookies"))
        if attach_bearer:
            self.session.headers["Authorization"] = f"Bearer {token}"
        if not validate:
            return True
        if validator():
            self._save_sso_state(status="authenticated")
            return True
        self._save_sso_state(status="expired", error="SSO 登录态已失效，请重新登录。")
        self.last_token = None
        self.session.headers.pop("Authorization", None)
        return False

    def persisted_status(
        self,
        *,
        validate: bool,
        validator: Callable[[], bool],
        attach_bearer: bool = True,
    ) -> dict[str, Any]:
        payload = self._load_sso_state()
        if not payload:
            return {
                "status": "logged_out",
                "label": "已退出",
                "status_tone": "neutral",
                "authenticated": False,
                "pending_code": False,
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": "",
                "pending_since": "",
                "expires_at": "",
                "challenge_type": "",
                "challenge_label": "",
            }
        authenticated = self.restore_persisted_session(
            validate=validate,
            validator=validator,
            attach_bearer=attach_bearer,
        )
        payload = self._load_sso_state()
        status = "authenticated" if authenticated else str(payload.get("status") or "expired")
        return {
            "status": status,
            "label": "已登录" if status == "authenticated" else "已过期" if status == "expired" else "已退出",
            "status_tone": "success" if status == "authenticated" else "error" if status == "expired" else "neutral",
            "authenticated": status == "authenticated",
            "pending_code": False,
            "last_validation_at": str(payload.get("last_validation_at") or ""),
            "last_error_summary": str(payload.get("last_error_summary") or ""),
            "authenticated_at": str(payload.get("authenticated_at") or ""),
            "pending_since": "",
            "expires_at": "",
            "challenge_type": "",
            "challenge_label": "",
        }
