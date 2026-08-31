"""Process-local availability gates for managed Service v2 backends.

Durable contribution material describes the backend a signed generation
expects.  It must not change because one Agent process has not completed its
startup canary or bound an ingress yet.  This module therefore keeps the
ephemeral readiness signal separate from generation hashes and database
records, and combines it only at catalog/dispatch time.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Mapping


_LIVE_GATED_KINDS = frozenset({"harness", "webhook", "events"})
_DEFAULT_UNAVAILABLE_REASONS = {
    "harness": "HARNESS_SANDBOX_UNAVAILABLE",
    "webhook": "WEBHOOK_HOST_BACKEND_UNAVAILABLE",
    "events": "EVENTS_HOST_BACKEND_UNAVAILABLE",
}


@dataclass(frozen=True)
class RuntimeBackendState:
    available: bool
    reason_detail: str | None


class RuntimeContributionBackendAvailability:
    """Thread-safe, process-only readiness for live contribution backends."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._states = {
            kind: RuntimeBackendState(False, reason)
            for kind, reason in _DEFAULT_UNAVAILABLE_REASONS.items()
        }

    @staticmethod
    def _kind(value: object) -> str:
        kind = str(value or "")
        if kind not in _LIVE_GATED_KINDS:
            raise ValueError("managed runtime backend kind is invalid")
        return kind

    def mark_available(self, *contribution_kinds: str) -> None:
        if not contribution_kinds:
            raise ValueError("at least one managed runtime backend kind is required")
        kinds = tuple(self._kind(value) for value in contribution_kinds)
        with self._lock:
            for kind in kinds:
                self._states[kind] = RuntimeBackendState(True, None)

    def mark_unavailable(
        self,
        *contribution_kinds: str,
        reason_detail: str | None = None,
    ) -> None:
        if not contribution_kinds:
            raise ValueError("at least one managed runtime backend kind is required")
        kinds = tuple(self._kind(value) for value in contribution_kinds)
        if reason_detail is not None:
            reason = str(reason_detail).strip()
            if not reason or len(reason) > 128:
                raise ValueError("managed runtime backend reason is invalid")
        else:
            reason = None
        with self._lock:
            for kind in kinds:
                self._states[kind] = RuntimeBackendState(
                    False,
                    reason or _DEFAULT_UNAVAILABLE_REASONS[kind],
                )

    def is_available(self, contribution_kind: str) -> bool:
        kind = str(contribution_kind or "")
        if kind not in _LIVE_GATED_KINDS:
            return True
        with self._lock:
            return self._states[kind].available

    def state(self, contribution_kind: str) -> RuntimeBackendState:
        kind = self._kind(contribution_kind)
        with self._lock:
            return self._states[kind]

    def snapshot(self) -> Mapping[str, RuntimeBackendState]:
        with self._lock:
            return dict(self._states)

    def effective_status(
        self,
        *,
        contribution_kind: str,
        structural_status: tuple[str, str, str | None, str | None],
    ) -> tuple[str, str, str | None, str | None]:
        """Overlay process readiness without mutating structural material."""

        kind = str(contribution_kind or "")
        backend, status, reason_code, reason_detail = structural_status
        if status != "READY" or kind not in _LIVE_GATED_KINDS:
            return backend, status, reason_code, reason_detail
        state = self.state(kind)
        if state.available:
            return backend, status, reason_code, reason_detail
        return (
            backend,
            "CAPABILITY_UNAVAILABLE",
            "CAPABILITY_UNAVAILABLE",
            state.reason_detail or _DEFAULT_UNAVAILABLE_REASONS[kind],
        )


__all__ = [
    "RuntimeBackendState",
    "RuntimeContributionBackendAvailability",
]
