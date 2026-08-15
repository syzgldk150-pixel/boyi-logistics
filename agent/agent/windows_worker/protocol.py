"""Ed25519-signed, replay-resistant Worker envelopes."""

from __future__ import annotations

import base64
import binascii
import copy
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

from agent.automation_plugins.errors import WorkerProtocolError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.package import PackageSignatureVerifier, PackageSigner


_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
_PAIRED_KINDS = frozenset({"COMMAND", "HEARTBEAT", "JOB_STATUS"})
_PAIR_KINDS = frozenset({"PAIR_REQUEST", "PAIR_RESPONSE"})
_KINDS = _PAIRED_KINDS | _PAIR_KINDS
_UNSIGNED_FIELDS = frozenset(
    {"schema_version", "message_id", "device_id", "sequence", "issued_at", "expires_at", "kind", "body"}
)
_SIGNED_FIELDS = _UNSIGNED_FIELDS | {"key_id", "signature"}


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise WorkerProtocolError("Worker envelope timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WorkerProtocolError(f"{field} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise WorkerProtocolError(f"{field} is invalid") from exc
    if _format_time(parsed) != value:
        raise WorkerProtocolError(f"{field} must use whole-second canonical UTC")
    return parsed


@runtime_checkable
class ReplayGuardPort(Protocol):
    def accept(self, *, device_id: str, sequence: int, message_id: str) -> bool: ...


@runtime_checkable
class ReplaySequenceRepositoryPort(Protocol):
    def advance_sequence(self, *, device_id: str, sequence: int, message_id: str) -> bool:
        """Atomically reject duplicate IDs and sequence rollback."""


class RepositoryReplayGuard:
    """Production adapter whose replay state survives Agent restarts."""

    def __init__(self, repository: ReplaySequenceRepositoryPort) -> None:
        self._repository = repository

    def accept(self, *, device_id: str, sequence: int, message_id: str) -> bool:
        return self._repository.advance_sequence(
            device_id=device_id,
            sequence=sequence,
            message_id=message_id,
        )


class MemoryReplayGuard:
    """Test/single-process guard; production persists sequence per device."""

    def __init__(self) -> None:
        self._sequences: dict[str, int] = {}
        self._messages: set[str] = set()

    def accept(self, *, device_id: str, sequence: int, message_id: str) -> bool:
        if message_id in self._messages or sequence <= self._sequences.get(device_id, -1):
            return False
        self._messages.add(message_id)
        self._sequences[device_id] = sequence
        return True


def sign_worker_envelope(
    *,
    signer: PackageSigner,
    device_id: str,
    sequence: int,
    kind: str,
    body: Mapping[str, Any],
    issued_at: datetime,
    expires_at: datetime,
    message_id: str | None = None,
) -> dict[str, Any]:
    if issued_at.tzinfo is None or expires_at.tzinfo is None:
        raise WorkerProtocolError("Worker envelope timestamps must be timezone-aware")
    if not _DEVICE_ID_RE.fullmatch(device_id):
        raise WorkerProtocolError("device_id is invalid")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise WorkerProtocolError("sequence must be a non-negative integer")
    if kind not in _KINDS or not isinstance(body, Mapping):
        raise WorkerProtocolError("Worker envelope kind/body is invalid")
    message_id = message_id or str(uuid.uuid4())
    try:
        uuid.UUID(message_id)
    except ValueError as exc:
        raise WorkerProtocolError("message_id must be UUID") from exc
    if expires_at <= issued_at or (expires_at - issued_at).total_seconds() > 300:
        raise WorkerProtocolError("Worker envelope lifetime must be from 1 to 300 seconds")
    unsigned = {
        "schema_version": 1,
        "message_id": message_id,
        "device_id": device_id,
        "sequence": sequence,
        "issued_at": _format_time(issued_at),
        "expires_at": _format_time(expires_at),
        "kind": kind,
        "body": copy.deepcopy(dict(body)),
    }
    signature = signer.sign(canonical_json_bytes(unsigned))
    return {
        **unsigned,
        "key_id": signer.key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_worker_envelope(
    envelope: Mapping[str, Any],
    *,
    verifier: PackageSignatureVerifier,
    replay_guard: ReplayGuardPort,
    expected_device_id: str,
    expected_key_id: str,
    now: datetime,
) -> dict[str, Any]:
    raw = copy.deepcopy(dict(envelope))
    if set(raw) != _SIGNED_FIELDS or raw.get("schema_version") != 1:
        raise WorkerProtocolError("Worker envelope schema is invalid")
    if raw.get("device_id") != expected_device_id or not _DEVICE_ID_RE.fullmatch(expected_device_id):
        raise WorkerProtocolError("Worker envelope targets a different device")
    if raw.get("kind") not in _PAIRED_KINDS or not isinstance(raw.get("body"), dict):
        raise WorkerProtocolError("Worker envelope kind/body is invalid")
    sequence = raw.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise WorkerProtocolError("Worker envelope sequence is invalid")
    message_id = raw.get("message_id")
    try:
        uuid.UUID(str(message_id))
    except ValueError as exc:
        raise WorkerProtocolError("Worker envelope message_id is invalid") from exc
    issued_at = _parse_time(raw.get("issued_at"), "issued_at")
    expires_at = _parse_time(raw.get("expires_at"), "expires_at")
    if now.tzinfo is None:
        raise WorkerProtocolError("verification time must be timezone-aware")
    current = now.astimezone(timezone.utc)
    if expires_at <= issued_at or (expires_at - issued_at).total_seconds() > 300:
        raise WorkerProtocolError("Worker envelope lifetime is invalid")
    if current < issued_at or current >= expires_at:
        raise WorkerProtocolError("Worker envelope is not currently valid")
    key_id = raw.get("key_id")
    if not isinstance(key_id, str) or not key_id or key_id != expected_key_id:
        raise WorkerProtocolError("Worker envelope key_id is invalid")
    try:
        signature = base64.b64decode(raw.get("signature"), validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise WorkerProtocolError("Worker envelope signature encoding is invalid") from exc
    if len(signature) != 64:
        raise WorkerProtocolError("Worker envelope signature length is invalid")
    unsigned = {key: raw[key] for key in _UNSIGNED_FIELDS}
    verifier.verify(key_id=key_id, message=canonical_json_bytes(unsigned), signature=signature)
    if not replay_guard.accept(
        device_id=expected_device_id,
        sequence=sequence,
        message_id=str(message_id),
    ):
        raise WorkerProtocolError("Worker envelope replay or sequence rollback detected")
    return unsigned
