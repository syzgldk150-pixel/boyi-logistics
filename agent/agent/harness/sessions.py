"""Thread-safe, deliberately memory-only Harness session storage."""

from __future__ import annotations

import copy
import uuid
from threading import RLock

from agent.harness.errors import HarnessError
from agent.harness.models import HarnessMessage, HarnessSession, canonical_uuid, exact_principal


class InMemoryHarnessSessionRepository:
    """Bounded session repository with exact-principal and UUID idempotency gates."""

    persistence_status = "MEMORY_ONLY_NON_PRODUCTION"

    def __init__(
        self,
        *,
        max_sessions: int = 256,
        max_sessions_per_principal: int = 16,
    ) -> None:
        if (
            isinstance(max_sessions, bool)
            or isinstance(max_sessions_per_principal, bool)
            or max_sessions < 1
            or max_sessions_per_principal < 1
        ):
            raise ValueError("Harness repository limits must be positive integers")
        self._max_sessions = max_sessions
        self._max_sessions_per_principal = max_sessions_per_principal
        self._lock = RLock()
        self._sessions: dict[str, HarnessSession] = {}
        self._create_idempotency: dict[tuple[str, str], str] = {}
        self._message_idempotency: dict[tuple[str, str, str], tuple[str, HarnessMessage]] = {}

    @staticmethod
    def _clone(session: HarnessSession) -> HarnessSession:
        return copy.deepcopy(session)

    def create_or_get(self, *, principal_id: str, request_id: str) -> HarnessSession:
        principal = exact_principal(principal_id)
        request = canonical_uuid(request_id, field_name="request_id")
        key = (principal, request)
        with self._lock:
            previous = self._create_idempotency.get(key)
            if previous is not None:
                return self._clone(self._sessions[previous])
            if len(self._sessions) >= self._max_sessions:
                raise HarnessError("session capacity exceeded", code="HARNESS_LIMIT_EXCEEDED")
            principal_sessions = sum(
                item.principal_id == principal for item in self._sessions.values()
            )
            if principal_sessions >= self._max_sessions_per_principal:
                raise HarnessError("principal session capacity exceeded", code="HARNESS_LIMIT_EXCEEDED")
            session = HarnessSession(session_id=str(uuid.uuid4()), principal_id=principal)
            self._sessions[session.session_id] = session
            self._create_idempotency[key] = session.session_id
            return self._clone(session)

    def get(self, *, principal_id: str, session_id: str) -> HarnessSession:
        principal = exact_principal(principal_id)
        session_key = canonical_uuid(session_id, field_name="session_id")
        with self._lock:
            session = self._sessions.get(session_key)
            if session is None or session.principal_id != principal:
                raise HarnessError("session is unavailable", code="HARNESS_SESSION_NOT_FOUND")
            return self._clone(session)

    def append_message(
        self,
        *,
        principal_id: str,
        session_id: str,
        request_id: str,
        message: HarnessMessage,
    ) -> HarnessMessage:
        principal = exact_principal(principal_id)
        session_key = canonical_uuid(session_id, field_name="session_id")
        request = canonical_uuid(request_id, field_name="request_id")
        if not isinstance(message, HarnessMessage):
            raise HarnessError("message is invalid", code="HARNESS_MESSAGE_INVALID")
        fingerprint = f"{message.role}\0{message.content}\0{message.message_id}"
        key = (principal, session_key, request)
        with self._lock:
            session = self._sessions.get(session_key)
            if session is None or session.principal_id != principal:
                raise HarnessError("session is unavailable", code="HARNESS_SESSION_NOT_FOUND")
            existing = self._message_idempotency.get(key)
            if existing is not None:
                if existing[0] != fingerprint:
                    raise HarnessError("idempotency key was reused", code="HARNESS_IDEMPOTENCY_CONFLICT")
                return copy.deepcopy(existing[1])
            updated = session.append(message)
            self._sessions[session_key] = updated
            self._message_idempotency[key] = (fingerprint, copy.deepcopy(message))
            return copy.deepcopy(message)


__all__ = ["InMemoryHarnessSessionRepository"]
