"""mTLS- and Ed25519-authenticated server transport for Windows Workers.

The reverse proxy is the TLS endpoint.  It must overwrite the two ``X-SSL``
headers below and proxy this route only over loopback.  A caller-controlled
device header merely selects a paired row; trust is established by matching
the verified client certificate and the signed envelope to that row.

This module is deliberately independent from ``main``.  The composition root
must inject the orchestration repository, release-hold authority, release SHA,
server signer and verified package archive reader explicitly.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, Request
from fastapi.responses import Response

from agent.automation_plugins.errors import (
    AutomationPluginError,
    PluginPackageError,
    PluginSignatureError,
    WorkerProtocolError,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.package import (
    MAX_ARCHIVE_BYTES,
    Ed25519PackageSigner,
    Ed25519TrustStore,
    PackageSigner,
)
from agent.windows_worker.protocol import (
    ReplayGuardPort,
    sign_worker_envelope,
    verify_worker_envelope,
)
from agent.windows_worker.routes import (
    WORKER_MESSAGES_PATH,
    WORKER_PACKAGE_PREFIX,
    WORKER_POLL_PATH,
    build_worker_package_path,
    is_worker_transport_path,
)
from shared.automation_plugin_repository import AutomationPluginReleaseHold
from shared.contracts import api_failure
from shared.orchestration_repository_support import (
    ConcurrentUpdateError,
    IdempotencyConflict,
    OrchestrationPersistenceError,
)


WORKER_DEVICE_HEADER = "X-Worker-Device-ID"
TLS_CLIENT_VERIFY_HEADER = "X-SSL-Client-Verify"
TLS_CLIENT_CERTIFICATE_HEADER = "X-SSL-Client-Cert"

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PEM_BODY_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MAX_INBOUND_ENVELOPE_BYTES = 1024 * 1024
_MAX_ESCAPED_CERTIFICATE_BYTES = 128 * 1024
_WORKER_PACKAGE_TRUST_SOURCES = frozenset(
    {"ed25519_first_party", "ed25519_upload"}
)
_PACKAGE_AUTHORIZATION_REQUIRED_FIELDS = frozenset(
    {
        "job_id",
        "assigned_device_id",
        "plugin_id",
        "version",
        "package_sha256",
        "dispatch_authorization_id",
        "install_root_metadata_json",
        "install_root_metadata_sha256",
        "trust_source",
    }
)


@dataclass(frozen=True)
class WorkerTransportPrincipal:
    """Paired identity proven independently by mTLS and Ed25519."""

    device_id: str
    device_key_id: str
    public_key_fingerprint: str
    tls_client_certificate_sha256: str
    verifier: Ed25519TrustStore
    device_row: Mapping[str, Any]


@runtime_checkable
class WorkerPackageArchiveReaderPort(Protocol):
    def read_authorized_archive(self, authorization: Mapping[str, Any]) -> bytes:
        """Read immutable ZIP bytes bound by a durable exact-device job."""


class FilesystemWorkerPackageArchiveReader:
    """Adapter over ``FilesystemPluginStorage.read_verified_archive``."""

    def __init__(self, storage: Any) -> None:
        if not callable(getattr(storage, "read_verified_archive", None)):
            raise TypeError("storage must expose read_verified_archive")
        self._storage = storage

    def read_authorized_archive(self, authorization: Mapping[str, Any]) -> bytes:
        metadata = authorization.get("install_root_metadata_json")
        if not isinstance(metadata, Mapping):
            raise PluginPackageError("authorized package install metadata is invalid")
        install_root = str(metadata.get("install_root") or "").strip()
        archive_relative = str(metadata.get("archive_relative") or "").strip()
        archive_sha256 = str(metadata.get("archive_sha256") or "").strip().lower()
        expected_sha256 = str(authorization.get("package_sha256") or "").strip().lower()
        trust_source = str(authorization.get("trust_source") or "").strip()
        if (
            not install_root
            or not archive_relative
            or archive_sha256 != expected_sha256
            or not _SHA256_RE.fullmatch(expected_sha256)
            or trust_source not in _WORKER_PACKAGE_TRUST_SOURCES
        ):
            raise PluginPackageError("authorized package archive metadata is inconsistent")
        return self._storage.read_verified_archive(
            Path(install_root),
            archive_relative,
            expected_sha256=expected_sha256,
        )


class WorkerTransportHttpError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.headers = dict(headers or {})


class WorkerTransportRepositoryContractError(RuntimeError):
    """The frozen repository does not expose a required closed operation."""


class _NonConsumingReplayGuard(ReplayGuardPort):
    """Let the repository decide durable duplicate/sequence semantics.

    Consuming a process-memory replay slot before the MySQL transaction would
    reject a legitimate retry whose previous HTTP response was lost.
    """

    def accept(self, *, device_id: str, sequence: int, message_id: str) -> bool:
        del device_id, sequence, message_id
        return True


_NON_CONSUMING_REPLAY_GUARD = _NonConsumingReplayGuard()


def _canonical_json_response(
    value: Mapping[str, Any],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    return Response(
        content=canonical_json_bytes(dict(value)),
        status_code=status_code,
        media_type="application/json",
        headers=dict(headers or {}),
    )


def _failure_response(error: WorkerTransportHttpError) -> Response:
    return _canonical_json_response(
        api_failure(error.code, error.message),
        status_code=error.status_code,
        headers=error.headers,
    )


def _raw_header_values(request: Request, name: str) -> list[bytes]:
    expected = name.lower().encode("ascii")
    return [value for key, value in request.scope.get("headers", []) if key.lower() == expected]


def _single_header(request: Request, name: str) -> str:
    values = _raw_header_values(request, name)
    if len(values) != 1:
        raise WorkerTransportHttpError(
            401,
            "WORKER_TRANSPORT_PRINCIPAL_INVALID",
            "Worker transport principal headers are missing or ambiguous",
        )
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise WorkerTransportHttpError(
            401,
            "WORKER_TRANSPORT_PRINCIPAL_INVALID",
            "Worker transport principal header encoding is invalid",
        ) from exc
    if not value or value != value.strip():
        raise WorkerTransportHttpError(
            401,
            "WORKER_TRANSPORT_PRINCIPAL_INVALID",
            "Worker transport principal header is invalid",
        )
    return value


def _require_loopback_proxy(request: Request) -> None:
    host = str(getattr(request.client, "host", "") or "")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise WorkerTransportHttpError(
            401,
            "WORKER_TRANSPORT_PROXY_REQUIRED",
            "Worker transport is accepted only from the loopback TLS proxy",
        ) from exc
    loopback = address.is_loopback or bool(
        getattr(address, "ipv4_mapped", None)
        and address.ipv4_mapped.is_loopback
    )
    if not loopback:
        raise WorkerTransportHttpError(
            401,
            "WORKER_TRANSPORT_PROXY_REQUIRED",
            "Worker transport is accepted only from the loopback TLS proxy",
        )


def _selected_device_id(request: Request, route_device_id: str | None = None) -> str:
    header_device_id = _single_header(request, WORKER_DEVICE_HEADER)
    selected = str(route_device_id if route_device_id is not None else header_device_id)
    if (
        not _DEVICE_ID_RE.fullmatch(selected)
        or header_device_id != selected
    ):
        raise WorkerTransportHttpError(
            400,
            "WORKER_DEVICE_SELECTION_INVALID",
            "Worker device selection is invalid",
        )
    return selected


def _certificate_der_from_escaped_header(value: str) -> bytes:
    if (
        not value
        or len(value.encode("ascii")) > _MAX_ESCAPED_CERTIFICATE_BYTES
        or _INVALID_PERCENT_ESCAPE_RE.search(value)
    ):
        raise WorkerTransportHttpError(
            401,
            "WORKER_TLS_CERTIFICATE_INVALID",
            "Worker TLS client certificate header is invalid",
        )
    try:
        decoded = urllib.parse.unquote_to_bytes(value).decode("ascii")
    except (UnicodeDecodeError, ValueError) as exc:
        raise WorkerTransportHttpError(
            401,
            "WORKER_TLS_CERTIFICATE_INVALID",
            "Worker TLS client certificate encoding is invalid",
        ) from exc
    normalized = decoded.replace("\r\n", "\n")
    if "\r" in normalized:
        raise WorkerTransportHttpError(
            401,
            "WORKER_TLS_CERTIFICATE_INVALID",
            "Worker TLS client certificate line endings are invalid",
        )
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    lines = normalized.split("\n")
    if (
        len(lines) < 3
        or lines[0] != "-----BEGIN CERTIFICATE-----"
        or lines[-1] != "-----END CERTIFICATE-----"
        or any(not line or len(line) > 64 for line in lines[1:-1])
    ):
        raise WorkerTransportHttpError(
            401,
            "WORKER_TLS_CERTIFICATE_INVALID",
            "Worker TLS client certificate PEM is invalid",
        )
    encoded = "".join(lines[1:-1])
    if not _PEM_BODY_RE.fullmatch(encoded):
        raise WorkerTransportHttpError(
            401,
            "WORKER_TLS_CERTIFICATE_INVALID",
            "Worker TLS client certificate base64 is invalid",
        )
    try:
        der = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkerTransportHttpError(
            401,
            "WORKER_TLS_CERTIFICATE_INVALID",
            "Worker TLS client certificate base64 is invalid",
        ) from exc
    if (
        not 16 <= len(der) <= 64 * 1024
        or der[0] != 0x30
        or base64.b64encode(der).decode("ascii") != encoded
    ):
        raise WorkerTransportHttpError(
            401,
            "WORKER_TLS_CERTIFICATE_INVALID",
            "Worker TLS client certificate DER is invalid",
        )
    return der


def _closed_worker_identity(value: Any) -> tuple[str, bytes, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "device_key_id",
        "ed25519_public_key_base64",
        "tls_client_certificate_sha256",
    }:
        raise WorkerTransportHttpError(
            401,
            "WORKER_DEVICE_IDENTITY_INVALID",
            "Paired Worker identity is invalid",
        )
    key_id = str(value.get("device_key_id") or "")
    public_text = str(value.get("ed25519_public_key_base64") or "")
    certificate_sha256 = str(value.get("tls_client_certificate_sha256") or "")
    if not _KEY_ID_RE.fullmatch(key_id) or not _SHA256_RE.fullmatch(certificate_sha256):
        raise WorkerTransportHttpError(
            401,
            "WORKER_DEVICE_IDENTITY_INVALID",
            "Paired Worker identity fields are invalid",
        )
    try:
        public_key = base64.b64decode(public_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkerTransportHttpError(
            401,
            "WORKER_DEVICE_IDENTITY_INVALID",
            "Paired Worker public key is invalid",
        ) from exc
    if (
        len(public_key) != 32
        or base64.b64encode(public_key).decode("ascii") != public_text
    ):
        raise WorkerTransportHttpError(
            401,
            "WORKER_DEVICE_IDENTITY_INVALID",
            "Paired Worker public key is invalid",
        )
    return key_id, public_key, certificate_sha256


def _identity_sha256(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(identity))).hexdigest()


def _authenticate_principal(
    request: Request,
    *,
    selected_device_id: str,
    repository: Any,
) -> WorkerTransportPrincipal:
    _require_loopback_proxy(request)
    verify_value = _single_header(request, TLS_CLIENT_VERIFY_HEADER)
    if not secrets.compare_digest(verify_value, "SUCCESS"):
        raise WorkerTransportHttpError(
            401,
            "WORKER_MTLS_VERIFICATION_REQUIRED",
            "Worker TLS client certificate was not verified",
        )
    certificate_header = _single_header(request, TLS_CLIENT_CERTIFICATE_HEADER)
    certificate_der = _certificate_der_from_escaped_header(certificate_header)
    certificate_sha256 = hashlib.sha256(certificate_der).hexdigest()

    getter = getattr(repository, "get_worker_device", None)
    if not callable(getter):
        raise WorkerTransportRepositoryContractError(
            "Worker repository does not expose get_worker_device"
        )
    row = getter(selected_device_id, for_update=True)
    if (
        not isinstance(row, Mapping)
        or str(row.get("device_id") or "") != selected_device_id
        or str(row.get("service_state") or "").upper() == "DISABLED"
    ):
        raise WorkerTransportHttpError(
            401,
            "WORKER_DEVICE_NOT_PAIRED",
            "Worker device is not paired for transport",
        )
    identity = row.get("identity_json")
    key_id, public_key, paired_certificate_sha256 = _closed_worker_identity(identity)
    public_key_fingerprint = hashlib.sha256(public_key).hexdigest()
    stored_public_key_fingerprint = str(
        row.get("paired_public_key_fingerprint") or ""
    ).strip().lower()
    stored_identity_sha256 = str(row.get("identity_sha256") or "").strip().lower()
    if (
        not _SHA256_RE.fullmatch(stored_public_key_fingerprint)
        or not secrets.compare_digest(
            public_key_fingerprint,
            stored_public_key_fingerprint,
        )
        or not _SHA256_RE.fullmatch(stored_identity_sha256)
        or not secrets.compare_digest(
            _identity_sha256(dict(identity)),
            stored_identity_sha256,
        )
        or not secrets.compare_digest(
            certificate_sha256,
            paired_certificate_sha256,
        )
    ):
        raise WorkerTransportHttpError(
            401,
            "WORKER_DEVICE_IDENTITY_MISMATCH",
            "Worker transport identity does not match its pairing",
        )
    return WorkerTransportPrincipal(
        device_id=selected_device_id,
        device_key_id=key_id,
        public_key_fingerprint=public_key_fingerprint,
        tls_client_certificate_sha256=certificate_sha256,
        verifier=Ed25519TrustStore({key_id: public_key}),
        device_row=dict(row),
    )


async def _read_inbound_envelope(request: Request) -> dict[str, Any]:
    content_type = str(request.headers.get("content-type") or "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json" or request.headers.get("content-encoding"):
        raise WorkerTransportHttpError(
            415,
            "WORKER_MESSAGE_MEDIA_TYPE_INVALID",
            "Worker messages require unencoded application/json",
        )
    content_lengths = _raw_header_values(request, "Content-Length")
    if len(content_lengths) > 1:
        raise WorkerTransportHttpError(
            400,
            "WORKER_MESSAGE_LENGTH_INVALID",
            "Worker message Content-Length is ambiguous",
        )
    if content_lengths:
        try:
            declared = int(content_lengths[0].decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkerTransportHttpError(
                400,
                "WORKER_MESSAGE_LENGTH_INVALID",
                "Worker message Content-Length is invalid",
            ) from exc
        if declared < 0 or declared > _MAX_INBOUND_ENVELOPE_BYTES:
            raise WorkerTransportHttpError(
                413,
                "WORKER_MESSAGE_TOO_LARGE",
                "Worker message exceeds one MiB",
            )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_INBOUND_ENVELOPE_BYTES:
            raise WorkerTransportHttpError(
                413,
                "WORKER_MESSAGE_TOO_LARGE",
                "Worker message exceeds one MiB",
            )
        chunks.append(bytes(chunk))
    raw = b"".join(chunks)
    if not raw:
        raise WorkerTransportHttpError(
            400,
            "WORKER_MESSAGE_INVALID",
            "Worker message body is empty",
        )
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise WorkerTransportHttpError(
            400,
            "WORKER_MESSAGE_INVALID",
            "Worker message is invalid JSON",
        ) from exc
    if not isinstance(value, dict):
        raise WorkerTransportHttpError(
            400,
            "WORKER_MESSAGE_INVALID",
            "Worker message must be a JSON object",
        )
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _verified_inbound_envelope(
    envelope: Mapping[str, Any],
    *,
    principal: WorkerTransportPrincipal,
    now: datetime,
) -> dict[str, Any]:
    try:
        unsigned = verify_worker_envelope(
            envelope,
            verifier=principal.verifier,
            replay_guard=_NON_CONSUMING_REPLAY_GUARD,
            expected_device_id=principal.device_id,
            expected_key_id=principal.device_key_id,
            now=now,
        )
    except PluginSignatureError as exc:
        raise WorkerProtocolError("Worker envelope signature is invalid") from exc
    except AutomationPluginError:
        raise
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("Worker envelope verification failed") from exc
    if unsigned.get("kind") not in {"HEARTBEAT", "JOB_STATUS"}:
        raise WorkerProtocolError("Worker inbound message kind is not accepted")
    return dict(unsigned)


def _utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WorkerTransportRepositoryContractError(
            "Worker server clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


class WindowsWorkerServerTransport:
    """Application service behind the three exact Worker routes."""

    def __init__(
        self,
        *,
        orchestration_repository: Any,
        release_hold_provider: Callable[[], bool] | Any,
        release_sha: str,
        server_signer: PackageSigner,
        package_reader: WorkerPackageArchiveReaderPort | None = None,
        now_provider: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if not callable(getattr(orchestration_repository, "unit_of_work", None)):
            raise TypeError("orchestration_repository must expose unit_of_work")
        signer_key_id = str(getattr(server_signer, "key_id", "") or "")
        if not _KEY_ID_RE.fullmatch(signer_key_id) or not callable(
            getattr(server_signer, "sign", None)
        ):
            raise ValueError("an explicit Ed25519 Worker server signer is required")
        if not _RELEASE_SHA_RE.fullmatch(str(release_sha or "")):
            raise ValueError("Worker server release_sha is invalid")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Worker server lease_seconds must be from 1 to 300")
        if package_reader is not None and not isinstance(
            package_reader,
            WorkerPackageArchiveReaderPort,
        ):
            raise TypeError("package_reader does not implement the closed archive port")
        self._orchestration = orchestration_repository
        self._release_hold_provider = release_hold_provider
        self._release_sha = str(release_sha)
        self._signer = server_signer
        self._package_reader = package_reader
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._lease_seconds = lease_seconds

    def _release_held(self) -> bool:
        try:
            if callable(self._release_hold_provider):
                return bool(self._release_hold_provider())
            checker = getattr(self._release_hold_provider, "is_held", None)
            if callable(checker):
                return bool(checker())
        except Exception:
            return True
        return True

    def poll(self, request: Request, *, device_id: str) -> Response:
        selected_device_id = _selected_device_id(request, device_id)
        with self._orchestration.unit_of_work() as uow:
            repository = uow.automation_plugins
            principal = _authenticate_principal(
                request,
                selected_device_id=selected_device_id,
                repository=repository,
            )
            held = self._release_held()
            if held:
                raise WorkerTransportHttpError(
                    503,
                    "WORKER_RELEASE_HOLD",
                    "Worker command dispatch is held for release",
                )
            issued_at = _utc_now(self._now())

            def envelope_factory(
                *,
                device_id: str,
                sequence: int,
                message_id: str,
                body: Mapping[str, Any],
            ) -> Mapping[str, Any]:
                return sign_worker_envelope(
                    signer=self._signer,
                    device_id=device_id,
                    sequence=sequence,
                    kind="COMMAND",
                    body=body,
                    issued_at=issued_at,
                    expires_at=issued_at + timedelta(seconds=self._lease_seconds),
                    message_id=message_id,
                )

            claimer = getattr(repository, "claim_dispatch_envelopes", None)
            if not callable(claimer):
                raise WorkerTransportRepositoryContractError(
                    "Worker repository does not expose durable dispatch claims"
                )
            rows = claimer(
                device_id=principal.device_id,
                worker_id=f"worker-transport:{principal.device_id}",
                limit=1,
                lease_seconds=self._lease_seconds,
                release_hold=held,
                release_sha=self._release_sha,
                envelope_factory=envelope_factory,
            )
            uow.commit()
        if not rows:
            return Response(status_code=204)
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise WorkerTransportRepositoryContractError(
                "Worker repository returned an invalid dispatch set"
            )
        envelope = rows[0].get("dispatch_envelope_json")
        if not isinstance(envelope, Mapping):
            raise WorkerTransportRepositoryContractError(
                "Worker dispatch row omitted its durable signed envelope"
            )
        return _canonical_json_response(dict(envelope))

    async def receive_message(self, request: Request) -> Response:
        selected_device_id = _selected_device_id(request)
        envelope = await _read_inbound_envelope(request)
        now = _utc_now(self._now())
        with self._orchestration.unit_of_work() as uow:
            repository = uow.automation_plugins
            principal = _authenticate_principal(
                request,
                selected_device_id=selected_device_id,
                repository=repository,
            )
            verified = _verified_inbound_envelope(
                envelope,
                principal=principal,
                now=now,
            )
            message_id = str(verified["message_id"])
            envelope_sha256 = hashlib.sha256(
                canonical_json_bytes(dict(envelope))
            ).hexdigest()
            prior_duplicate = (
                str(principal.device_row.get("last_inbound_message_id") or "")
                == message_id
                and str(
                    principal.device_row.get("last_inbound_envelope_sha256") or ""
                )
                == envelope_sha256
            )
            kind = str(verified["kind"])
            if kind == "HEARTBEAT":
                recorder = getattr(repository, "heartbeat_device", None)
                if not callable(recorder):
                    raise WorkerTransportRepositoryContractError(
                        "Worker repository does not expose durable heartbeats"
                    )
                recorder(
                    envelope,
                    principal_device_id=principal.device_id,
                    paired_public_key_fingerprint=principal.public_key_fingerprint,
                    signature_verified=True,
                )
                duplicate = prior_duplicate
                processed_status = "ACCEPTED"
            else:
                recorder = getattr(repository, "record_worker_job_status", None)
                if not callable(recorder):
                    raise WorkerTransportRepositoryContractError(
                        "Worker repository does not expose durable job status ACKs"
                    )
                result = recorder(
                    envelope,
                    principal_device_id=principal.device_id,
                    paired_public_key_fingerprint=principal.public_key_fingerprint,
                    signature_verified=True,
                )
                if not isinstance(result, Mapping):
                    raise WorkerTransportRepositoryContractError(
                        "Worker repository returned an invalid job-status ACK"
                    )
                duplicate = result.get("duplicate") is True
                processed_status = str(result.get("status") or "")
            uow.commit()
        acknowledgement = {
            "schema_version": 1,
            "message_id": message_id,
            "kind": kind,
            "accepted": True,
            "duplicate": duplicate,
            "processed_status": processed_status,
        }
        if duplicate:
            return _canonical_json_response(
                acknowledgement,
                status_code=409,
                headers={"X-Worker-Message-Status": "already-accepted"},
            )
        return _canonical_json_response(acknowledgement, status_code=202)

    def package(
        self,
        request: Request,
        *,
        plugin_id: str,
        version: str,
        package_sha256: str,
        dispatch_authorization_id: str,
    ) -> Response:
        try:
            build_worker_package_path(
                plugin_id=plugin_id,
                version=version,
                package_sha256=package_sha256,
                dispatch_authorization_id=dispatch_authorization_id,
            )
        except ValueError:
            raise WorkerTransportHttpError(
                404,
                "WORKER_PACKAGE_NOT_FOUND",
                "Worker package is unavailable",
            ) from None
        selected_device_id = _selected_device_id(request)
        with self._orchestration.unit_of_work() as uow:
            repository = uow.automation_plugins
            principal = _authenticate_principal(
                request,
                selected_device_id=selected_device_id,
                repository=repository,
            )
            if self._release_held():
                raise WorkerTransportHttpError(
                    503,
                    "WORKER_RELEASE_HOLD",
                    "Worker package download is held for release",
                )
            authorize = getattr(
                repository,
                "authorize_worker_package_download",
                None,
            )
            if not callable(authorize):
                raise WorkerTransportRepositoryContractError(
                    "Worker repository lacks exact-device durable package authorization"
                )
            authorization = authorize(
                device_id=principal.device_id,
                plugin_id=plugin_id,
                plugin_version=version,
                package_sha256=package_sha256,
                dispatch_authorization_id=dispatch_authorization_id,
            )
        if authorization is None:
            raise WorkerTransportHttpError(
                403,
                "WORKER_PACKAGE_NOT_AUTHORIZED",
                "Worker device has no durable authorization for this package",
            )
        if (
            not isinstance(authorization, Mapping)
            or not _PACKAGE_AUTHORIZATION_REQUIRED_FIELDS.issubset(authorization)
            or str(authorization.get("assigned_device_id") or "")
            != principal.device_id
            or str(authorization.get("plugin_id") or "") != plugin_id
            or str(authorization.get("version") or "") != version
            or str(authorization.get("package_sha256") or "") != package_sha256
            or str(authorization.get("dispatch_authorization_id") or "")
            != dispatch_authorization_id
        ):
            raise WorkerTransportRepositoryContractError(
                "Worker package authorization contract is invalid"
            )
        if self._package_reader is None:
            raise WorkerTransportRepositoryContractError(
                "Worker package archive reader is not configured"
            )
        payload = self._package_reader.read_authorized_archive(authorization)
        expected_sha256 = package_sha256
        if (
            not isinstance(payload, bytes)
            or not payload
            or len(payload) > MAX_ARCHIVE_BYTES
            or hashlib.sha256(payload).hexdigest() != expected_sha256
        ):
            raise WorkerTransportRepositoryContractError(
                "Worker package archive failed immutable digest verification"
            )
        return Response(
            content=payload,
            status_code=200,
            media_type="application/zip",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="{plugin_id}-{version}.zip"'
                ),
                "ETag": f'"{expected_sha256}"',
                "X-Content-Type-Options": "nosniff",
            },
        )


async def _invoke_route(call: Callable[[], Any]) -> Response:
    try:
        result = call()
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, Response):
            raise WorkerTransportRepositoryContractError(
                "Worker transport route did not return an HTTP response"
            )
        return result
    except WorkerTransportHttpError as exc:
        return _failure_response(exc)
    except AutomationPluginReleaseHold:
        return _failure_response(
            WorkerTransportHttpError(
                503,
                "WORKER_RELEASE_HOLD",
                "Worker command dispatch is held for release",
            )
        )
    except IdempotencyConflict:
        return _failure_response(
            WorkerTransportHttpError(
                409,
                "WORKER_MESSAGE_CONFLICT",
                "Worker message conflicts with durable transport state",
            )
        )
    except ConcurrentUpdateError:
        return _failure_response(
            WorkerTransportHttpError(
                409,
                "WORKER_TRANSPORT_CONCURRENT_UPDATE",
                "Worker transport state changed concurrently",
            )
        )
    except PluginPackageError:
        return _failure_response(
            WorkerTransportHttpError(
                503,
                "WORKER_TRANSPORT_UNAVAILABLE",
                "Worker transport is unavailable",
            )
        )
    except (WorkerProtocolError, AutomationPluginError, TypeError, ValueError):
        return _failure_response(
            WorkerTransportHttpError(
                400,
                "WORKER_PROTOCOL_INVALID",
                "Worker transport message or command is invalid",
            )
        )
    except (
        WorkerTransportRepositoryContractError,
        OrchestrationPersistenceError,
    ):
        return _failure_response(
            WorkerTransportHttpError(
                503,
                "WORKER_TRANSPORT_UNAVAILABLE",
                "Worker transport is unavailable",
            )
        )


def build_worker_transport_router(
    service: WindowsWorkerServerTransport | None = None,
    *,
    service_provider: Callable[[], WindowsWorkerServerTransport] | None = None,
) -> APIRouter:
    if (service is None) == (service_provider is None):
        raise TypeError("provide exactly one Worker transport service or provider")
    if service is not None and not isinstance(service, WindowsWorkerServerTransport):
        raise TypeError("service must be WindowsWorkerServerTransport")
    if service_provider is not None and not callable(service_provider):
        raise TypeError("service_provider must be callable")

    def current_service() -> WindowsWorkerServerTransport:
        candidate = service if service is not None else service_provider()
        if not isinstance(candidate, WindowsWorkerServerTransport):
            raise WorkerTransportRepositoryContractError(
                "Worker transport service is unavailable"
            )
        return candidate

    router = APIRouter()

    @router.get(WORKER_POLL_PATH, include_in_schema=False)
    async def poll_worker_commands(request: Request, device_id: str) -> Response:
        return await _invoke_route(
            lambda: current_service().poll(request, device_id=device_id)
        )

    @router.post(WORKER_MESSAGES_PATH, include_in_schema=False)
    async def receive_worker_message(request: Request) -> Response:
        return await _invoke_route(lambda: current_service().receive_message(request))

    @router.get(
        (
            f"{WORKER_PACKAGE_PREFIX}{{plugin_id}}/{{version}}/"
            "{package_sha256}/{dispatch_authorization_id}"
        ),
        include_in_schema=False,
    )
    async def download_worker_package(
        request: Request,
        plugin_id: str,
        version: str,
        package_sha256: str,
        dispatch_authorization_id: str,
    ) -> Response:
        return await _invoke_route(
            lambda: current_service().package(
                request,
                plugin_id=plugin_id,
                version=version,
                package_sha256=package_sha256,
                dispatch_authorization_id=dispatch_authorization_id,
            )
        )

    return router


def load_worker_server_signer(
    *,
    private_key_path: Path | str,
    key_id: str,
) -> Ed25519PackageSigner:
    """Load only an explicitly injected, protected Ed25519 key file.

    This helper never discovers environment variables and never emits key
    bytes.  The composition root must supply both the environment-derived
    absolute path and key ID; missing or unsafe material fails closed.
    """

    raw_path = str(private_key_path or "").strip()
    if not raw_path or not _KEY_ID_RE.fullmatch(str(key_id or "")):
        raise ValueError("Worker server signing key path and key_id are required")
    requested = Path(raw_path)
    if not requested.is_absolute() or requested.is_symlink():
        raise ValueError("Worker server signing key must be an absolute non-symlink file")
    target = requested.resolve(strict=True)
    before = target.stat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 64 * 1024:
        raise ValueError("Worker server signing key file is invalid")
    if os.name != "nt" and stat.S_IMODE(before.st_mode) & 0o077:
        raise ValueError("Worker server signing key file permissions are too broad")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0) or 0)
    descriptor = os.open(target, flags)
    material = bytearray()
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise ValueError("Worker server signing key changed during inspection")
        while len(material) <= 64 * 1024:
            chunk = os.read(descriptor, min(8192, 64 * 1024 + 1 - len(material)))
            if not chunk:
                break
            material.extend(chunk)
        if not material or len(material) > 64 * 1024:
            raise ValueError("Worker server signing key file is invalid")
        try:
            from Crypto.PublicKey import ECC
        except ImportError as exc:  # pragma: no cover - locked production dependency
            raise RuntimeError("pycryptodome with Ed25519 support is required") from exc
        try:
            private_key = ECC.import_key(bytes(material))
        except (ValueError, TypeError, IndexError) as exc:
            raise ValueError("Worker server signing key is not valid Ed25519") from exc
        if (
            not private_key.has_private()
            or str(getattr(private_key, "curve", "")) != "Ed25519"
        ):
            raise ValueError("Worker server signing key is not valid Ed25519")
        return Ed25519PackageSigner(key_id=str(key_id), private_key=private_key)
    finally:
        os.close(descriptor)
        for index in range(len(material)):
            material[index] = 0


__all__ = [
    "FilesystemWorkerPackageArchiveReader",
    "TLS_CLIENT_CERTIFICATE_HEADER",
    "TLS_CLIENT_VERIFY_HEADER",
    "WORKER_DEVICE_HEADER",
    "WORKER_MESSAGES_PATH",
    "WORKER_PACKAGE_PREFIX",
    "WORKER_POLL_PATH",
    "WindowsWorkerServerTransport",
    "WorkerPackageArchiveReaderPort",
    "WorkerTransportPrincipal",
    "build_worker_transport_router",
    "is_worker_transport_path",
    "load_worker_server_signer",
]
