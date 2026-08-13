"""Signed Console-to-Agent principal assertions.

The shared internal API token authenticates a service connection, not an end
user.  Console therefore signs the administrator principal separately and
binds it to one exact HTTP request.  This module is intentionally pure: callers
inject the secret and it never reads environment files or variables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


CONSOLE_PRINCIPAL_HEADER = "X-Console-Principal"
CONSOLE_TIMESTAMP_HEADER = "X-Console-Timestamp"
CONSOLE_NONCE_HEADER = "X-Console-Nonce"
CONSOLE_SIGNATURE_HEADER = "X-Console-Signature"
CONSOLE_SIGNATURE_VERSION = "v1"
CONSOLE_SIGNATURE_TTL_SECONDS = 30
CONSOLE_SIGNATURE_FUTURE_SKEW_SECONDS = 5

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_ALLOWED_ROLES = frozenset({"admin", "super_admin"})


class ConsoleIdentityError(ValueError):
    """A signed principal assertion was missing, invalid, expired, or replayed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_console_principal(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the closed principal contract accepted at the Agent boundary."""

    actor_type = str(value.get("actor_type") or "").strip()
    actor_id = str(value.get("actor_id") or "").strip()
    authenticated_by = str(value.get("authenticated_by") or "").strip()
    display_name = str(value.get("display_name") or "").strip()[:200]
    raw_roles = value.get("roles")
    roles = tuple(
        dict.fromkeys(
            str(role).strip().lower()
            for role in (raw_roles if isinstance(raw_roles, list | tuple) else [])
            if str(role or "").strip()
        )
    )
    if actor_type != "console_admin":
        raise ConsoleIdentityError("INVALID_CONSOLE_PRINCIPAL", "actor_type must be console_admin")
    if not actor_id or len(actor_id) > 128:
        raise ConsoleIdentityError("INVALID_CONSOLE_PRINCIPAL", "actor_id is required")
    if authenticated_by != "mysql_admin_session":
        raise ConsoleIdentityError(
            "INVALID_CONSOLE_PRINCIPAL",
            "authenticated_by must be mysql_admin_session",
        )
    if not roles or any(role not in _ALLOWED_ROLES for role in roles):
        raise ConsoleIdentityError("INVALID_CONSOLE_PRINCIPAL", "principal roles are invalid")
    return {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "roles": list(roles),
        "display_name": display_name,
        "authenticated_by": authenticated_by,
    }


def build_console_identity_headers(
    *,
    secret: str,
    method: str,
    request_target: str,
    body: bytes,
    principal: Mapping[str, Any],
    timestamp: int | None = None,
    nonce: str,
) -> dict[str, str]:
    """Sign one exact method, path/query, raw body, nonce, and principal."""

    key = str(secret or "").encode("utf-8")
    if not key:
        raise ConsoleIdentityError(
            "CONSOLE_SIGNING_SECRET_NOT_CONFIGURED",
            "Console-to-Agent signing secret is not configured",
        )
    safe_nonce = str(nonce or "").strip()
    if not _NONCE_RE.fullmatch(safe_nonce):
        raise ConsoleIdentityError("INVALID_CONSOLE_NONCE", "Console request nonce is invalid")
    issued_at = int(time.time()) if timestamp is None else int(timestamp)
    encoded_principal = _encode_principal(normalize_console_principal(principal))
    canonical = _canonical_request(
        method=method,
        request_target=request_target,
        body=body,
        timestamp=issued_at,
        nonce=safe_nonce,
        encoded_principal=encoded_principal,
    )
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return {
        CONSOLE_PRINCIPAL_HEADER: encoded_principal,
        CONSOLE_TIMESTAMP_HEADER: str(issued_at),
        CONSOLE_NONCE_HEADER: safe_nonce,
        CONSOLE_SIGNATURE_HEADER: f"{CONSOLE_SIGNATURE_VERSION}={signature}",
    }


@dataclass
class ConsoleIdentityVerifier:
    """Verify short-lived assertions and reject a nonce after its first use."""

    secret: str
    ttl_seconds: int = CONSOLE_SIGNATURE_TTL_SECONDS
    future_skew_seconds: int = CONSOLE_SIGNATURE_FUTURE_SKEW_SECONDS

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._used_nonces: dict[str, int] = {}

    def verify(
        self,
        *,
        headers: Mapping[str, str],
        method: str,
        request_target: str,
        body: bytes,
        now: int | None = None,
    ) -> dict[str, Any] | None:
        """Return a verified principal, ``None`` when no assertion was supplied."""

        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        names = (
            CONSOLE_PRINCIPAL_HEADER,
            CONSOLE_TIMESTAMP_HEADER,
            CONSOLE_NONCE_HEADER,
            CONSOLE_SIGNATURE_HEADER,
        )
        supplied = [lowered.get(name.lower(), "").strip() for name in names]
        if not any(supplied):
            return None
        if not all(supplied):
            raise ConsoleIdentityError(
                "INCOMPLETE_CONSOLE_SIGNATURE",
                "Signed Console identity headers are incomplete",
            )
        key = str(self.secret or "").encode("utf-8")
        if not key:
            raise ConsoleIdentityError(
                "CONSOLE_SIGNING_SECRET_NOT_CONFIGURED",
                "Console-to-Agent signing secret is not configured",
            )
        encoded_principal, raw_timestamp, nonce, provided_signature = supplied
        if not _NONCE_RE.fullmatch(nonce):
            raise ConsoleIdentityError("INVALID_CONSOLE_NONCE", "Console request nonce is invalid")
        try:
            issued_at = int(raw_timestamp)
        except ValueError as exc:
            raise ConsoleIdentityError(
                "INVALID_CONSOLE_TIMESTAMP",
                "Console request timestamp is invalid",
            ) from exc
        observed_at = int(time.time()) if now is None else int(now)
        if issued_at > observed_at + max(0, int(self.future_skew_seconds)):
            raise ConsoleIdentityError(
                "CONSOLE_SIGNATURE_FROM_FUTURE",
                "Console request timestamp is in the future",
            )
        if observed_at - issued_at > max(1, int(self.ttl_seconds)):
            raise ConsoleIdentityError("CONSOLE_SIGNATURE_EXPIRED", "Console request signature expired")
        canonical = _canonical_request(
            method=method,
            request_target=request_target,
            body=body,
            timestamp=issued_at,
            nonce=nonce,
            encoded_principal=encoded_principal,
        )
        expected_signature = f"{CONSOLE_SIGNATURE_VERSION}=" + hmac.new(
            key,
            canonical,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(provided_signature, expected_signature):
            raise ConsoleIdentityError(
                "INVALID_CONSOLE_SIGNATURE",
                "Console request signature is invalid",
            )
        principal = _decode_principal(encoded_principal)
        with self._lock:
            self._remove_expired_locked(observed_at)
            if nonce in self._used_nonces:
                raise ConsoleIdentityError(
                    "CONSOLE_SIGNATURE_REPLAYED",
                    "Console request signature was already used",
                )
            self._used_nonces[nonce] = issued_at
        return principal

    def _remove_expired_locked(self, now: int) -> None:
        retention = max(1, int(self.ttl_seconds)) + max(0, int(self.future_skew_seconds))
        for nonce, issued_at in list(self._used_nonces.items()):
            if now - issued_at > retention:
                self._used_nonces.pop(nonce, None)


def _canonical_request(
    *,
    method: str,
    request_target: str,
    body: bytes,
    timestamp: int,
    nonce: str,
    encoded_principal: str,
) -> bytes:
    safe_method = str(method or "").strip().upper()
    safe_target = str(request_target or "").strip()
    if not safe_method or not safe_target.startswith("/"):
        raise ConsoleIdentityError("INVALID_CONSOLE_SIGNATURE_INPUT", "request target is invalid")
    body_sha256 = hashlib.sha256(bytes(body or b"")).hexdigest()
    return "\n".join(
        (
            CONSOLE_SIGNATURE_VERSION,
            safe_method,
            safe_target,
            body_sha256,
            str(int(timestamp)),
            nonce,
            encoded_principal,
        )
    ).encode("utf-8")


def _encode_principal(principal: Mapping[str, Any]) -> str:
    raw = json.dumps(
        dict(principal),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_principal(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsoleIdentityError(
            "INVALID_CONSOLE_PRINCIPAL",
            "Signed Console principal is invalid",
        ) from exc
    if not isinstance(decoded, dict):
        raise ConsoleIdentityError(
            "INVALID_CONSOLE_PRINCIPAL",
            "Signed Console principal is invalid",
        )
    return normalize_console_principal(decoded)
