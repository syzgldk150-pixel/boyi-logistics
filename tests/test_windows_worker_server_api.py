from __future__ import annotations

import base64
import copy
import hashlib
import urllib.parse
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
from Crypto.PublicKey import ECC
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.package import (
    Ed25519PackageSigner,
    Ed25519TrustStore,
)
from agent.windows_worker.protocol import (
    MemoryReplayGuard,
    sign_worker_envelope,
    verify_worker_envelope,
)
from agent.windows_worker.server_api import (
    FilesystemWorkerPackageArchiveReader,
    TLS_CLIENT_CERTIFICATE_HEADER,
    TLS_CLIENT_VERIFY_HEADER,
    WORKER_DEVICE_HEADER,
    WORKER_MESSAGES_PATH,
    WORKER_PACKAGE_PREFIX,
    WORKER_POLL_PATH,
    WindowsWorkerServerTransport,
    build_worker_transport_router,
    is_worker_transport_path,
    load_worker_server_signer,
)


NOW = datetime(2026, 8, 15, 1, 2, 3, tzinfo=timezone.utc)
DEVICE_ID = "office_pc_one"
DEVICE_KEY_ID = "device-key-one"
SERVER_KEY_ID = "server-key-one"
PLUGIN_ID = "sync_arrive_list"
PLUGIN_VERSION = "1.0.0"
PACKAGE = b"signed-plugin-archive"
PACKAGE_SHA256 = hashlib.sha256(PACKAGE).hexdigest()
DISPATCH_AUTHORIZATION_ID = "f6d9dc71-b197-4800-bad3-4efe484406df"


def _signer(key_id: str) -> tuple[Ed25519PackageSigner, Ed25519TrustStore, bytes]:
    private_key = ECC.generate(curve="Ed25519")
    public_key = private_key.public_key().export_key(format="raw")
    return (
        Ed25519PackageSigner(key_id=key_id, private_key=private_key),
        Ed25519TrustStore({key_id: public_key}),
        public_key,
    )


def _escaped_certificate(
    der: bytes = b"\x30test-worker-certificate-der",
) -> tuple[str, str]:
    # Nginx proves that this is an X.509 certificate.  The Agent intentionally
    # derives only the exact DER fingerprint from the overwritten escaped PEM.
    encoded = base64.b64encode(der).decode("ascii")
    pem = f"-----BEGIN CERTIFICATE-----\n{encoded}\n-----END CERTIFICATE-----\n"
    return urllib.parse.quote(pem, safe=""), hashlib.sha256(der).hexdigest()


class _Repository:
    def __init__(self, *, public_key: bytes, certificate_sha256: str) -> None:
        identity = {
            "device_key_id": DEVICE_KEY_ID,
            "ed25519_public_key_base64": base64.b64encode(public_key).decode("ascii"),
            "tls_client_certificate_sha256": certificate_sha256,
        }
        self.device = {
            "device_id": DEVICE_ID,
            "service_state": "ONLINE",
            "identity_json": identity,
            "identity_sha256": hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
            "paired_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
            "last_inbound_message_id": None,
            "last_inbound_envelope_sha256": None,
        }
        self.dispatch: Mapping[str, Any] | None = None
        self.heartbeat_calls = 0
        self.status_messages: dict[str, str] = {}
        self.package_authorized = True
        self.package_calls: list[dict[str, str]] = []

    def get_worker_device(self, device_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None:
        assert for_update is True
        return copy.deepcopy(self.device) if device_id == DEVICE_ID else None

    def claim_dispatch_envelopes(
        self,
        *,
        device_id: str,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        release_hold: bool,
        release_sha: str,
        envelope_factory: Any,
    ) -> list[Mapping[str, Any]]:
        assert (device_id, worker_id, limit) == (
            DEVICE_ID,
            f"worker-transport:{DEVICE_ID}",
            1,
        )
        assert lease_seconds == 60
        assert release_hold is False
        assert release_sha == "abcdef1"
        if self.dispatch is None:
            envelope = envelope_factory(
                device_id=DEVICE_ID,
                sequence=1,
                message_id="e380856b-bf71-4a3a-9f35-c6cb55f747c6",
                body={
                    "job": {"job_id": "62497802-499c-4e62-86e2-ea7a92d9d741"},
                    "dispatch": {
                        "release_hold": False,
                        "authorization_id": DISPATCH_AUTHORIZATION_ID,
                        "release_sha": "abcdef1",
                    },
                },
            )
            self.dispatch = {"dispatch_envelope_json": envelope}
        return [copy.deepcopy(self.dispatch)]

    def heartbeat_device(
        self,
        envelope: Mapping[str, Any],
        *,
        principal_device_id: str,
        paired_public_key_fingerprint: str,
        signature_verified: bool,
    ) -> Mapping[str, Any]:
        assert principal_device_id == DEVICE_ID
        assert paired_public_key_fingerprint == self.device["paired_public_key_fingerprint"]
        assert signature_verified is True
        self.heartbeat_calls += 1
        self.device["last_inbound_message_id"] = envelope["message_id"]
        self.device["last_inbound_envelope_sha256"] = hashlib.sha256(
            canonical_json_bytes(dict(envelope))
        ).hexdigest()
        return copy.deepcopy(self.device)

    def record_worker_job_status(
        self,
        envelope: Mapping[str, Any],
        *,
        principal_device_id: str,
        paired_public_key_fingerprint: str,
        signature_verified: bool,
    ) -> Mapping[str, Any]:
        assert principal_device_id == DEVICE_ID
        assert paired_public_key_fingerprint == self.device["paired_public_key_fingerprint"]
        assert signature_verified is True
        message_id = str(envelope["message_id"])
        digest = hashlib.sha256(canonical_json_bytes(dict(envelope))).hexdigest()
        prior = self.status_messages.get(message_id)
        if prior is not None and prior != digest:
            raise AssertionError("test repository received a changed durable message")
        self.status_messages[message_id] = digest
        return {
            "message_id": message_id,
            "job_id": envelope["body"]["job_id"],
            "status": envelope["body"]["status"],
            "duplicate": prior is not None,
            "job": {},
        }

    def authorize_worker_package_download(self, **arguments: str) -> Mapping[str, Any] | None:
        self.package_calls.append(dict(arguments))
        if not self.package_authorized:
            return None
        metadata = {
            "install_root": "/srv/automation-plugins/sync_arrive_list/1.0.0",
            "archive_relative": "package-archive.zip",
            "archive_sha256": PACKAGE_SHA256,
        }
        return {
            "job_id": "62497802-499c-4e62-86e2-ea7a92d9d741",
            "assigned_device_id": DEVICE_ID,
            "plugin_id": PLUGIN_ID,
            "version": PLUGIN_VERSION,
            "package_sha256": PACKAGE_SHA256,
            "dispatch_authorization_id": DISPATCH_AUTHORIZATION_ID,
            "install_root_metadata_json": metadata,
            "install_root_metadata_sha256": hashlib.sha256(
                canonical_json_bytes(metadata)
            ).hexdigest(),
            "trust_source": "ed25519_upload",
        }


class _UnitOfWork(AbstractContextManager["_UnitOfWork"]):
    def __init__(self, repository: _Repository) -> None:
        self.automation_plugins = repository
        self.committed = False

    def __enter__(self) -> _UnitOfWork:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


class _Orchestration:
    def __init__(self, repository: _Repository) -> None:
        self.repository = repository
        self.units: list[_UnitOfWork] = []

    def unit_of_work(self) -> _UnitOfWork:
        unit = _UnitOfWork(self.repository)
        self.units.append(unit)
        return unit


class _ArchiveReader:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []
        self.payload = PACKAGE

    def read_authorized_archive(self, authorization: Mapping[str, Any]) -> bytes:
        self.calls.append(copy.deepcopy(dict(authorization)))
        return self.payload


def _fixture(
    *,
    held: bool = False,
    client_host: str = "127.0.0.1",
) -> tuple[
    TestClient,
    _Repository,
    _Orchestration,
    Ed25519PackageSigner,
    Ed25519TrustStore,
    dict[str, str],
    _ArchiveReader,
]:
    device_signer, _, public_key = _signer(DEVICE_KEY_ID)
    server_signer, server_trust, _ = _signer(SERVER_KEY_ID)
    escaped_certificate, certificate_sha256 = _escaped_certificate()
    repository = _Repository(
        public_key=public_key,
        certificate_sha256=certificate_sha256,
    )
    orchestration = _Orchestration(repository)
    archive_reader = _ArchiveReader()
    service = WindowsWorkerServerTransport(
        orchestration_repository=orchestration,
        release_hold_provider=lambda: held,
        release_sha="abcdef1",
        server_signer=server_signer,
        package_reader=archive_reader,
        now_provider=lambda: NOW,
    )
    app = FastAPI()
    app.include_router(build_worker_transport_router(service))
    client = TestClient(app, client=(client_host, 50000))
    headers = {
        WORKER_DEVICE_HEADER: DEVICE_ID,
        TLS_CLIENT_VERIFY_HEADER: "SUCCESS",
        TLS_CLIENT_CERTIFICATE_HEADER: escaped_certificate,
    }
    return (
        client,
        repository,
        orchestration,
        device_signer,
        server_trust,
        headers,
        archive_reader,
    )


def _signed_inbound(
    signer: Ed25519PackageSigner,
    *,
    sequence: int,
    kind: str,
    body: Mapping[str, Any],
    message_id: str | None = None,
) -> Mapping[str, Any]:
    return sign_worker_envelope(
        signer=signer,
        device_id=DEVICE_ID,
        sequence=sequence,
        kind=kind,
        body=body,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        message_id=message_id,
    )


def test_worker_transport_path_bypass_is_exact_and_closed() -> None:
    package_path = (
        f"{WORKER_PACKAGE_PREFIX}{PLUGIN_ID}/{PLUGIN_VERSION}/"
        f"{PACKAGE_SHA256}/{DISPATCH_AUTHORIZATION_ID}"
    )
    assert is_worker_transport_path(WORKER_POLL_PATH)
    assert is_worker_transport_path(WORKER_MESSAGES_PATH)
    assert is_worker_transport_path(package_path)
    assert not is_worker_transport_path("/internal/v1/automation/worker")
    assert not is_worker_transport_path(f"{WORKER_POLL_PATH}/extra")
    assert not is_worker_transport_path(f"{WORKER_PACKAGE_PREFIX}{PLUGIN_ID}/{PLUGIN_VERSION}")
    assert not is_worker_transport_path(f"{package_path}/extra")
    assert not is_worker_transport_path(
        f"{WORKER_PACKAGE_PREFIX}{PLUGIN_ID}/{PLUGIN_VERSION}/{'0' * 64}/not-a-uuid"
    )


def test_router_provider_fails_closed_until_composition_is_ready() -> None:
    app = FastAPI()
    app.include_router(build_worker_transport_router(service_provider=lambda: None))

    response = TestClient(app, client=("127.0.0.1", 50000)).get(
        f"{WORKER_POLL_PATH}?device_id={DEVICE_ID}"
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WORKER_TRANSPORT_UNAVAILABLE"


def test_poll_uses_mtls_principal_and_redelivers_exact_signed_command() -> None:
    client, _, orchestration, _, server_trust, headers, _ = _fixture()
    route = f"{WORKER_POLL_PATH}?device_id={DEVICE_ID}"
    first = client.get(route, headers=headers)
    second = client.get(route, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    assert all(unit.committed for unit in orchestration.units)

    envelope = first.json()
    unsigned = verify_worker_envelope(
        envelope,
        verifier=server_trust,
        replay_guard=MemoryReplayGuard(),
        expected_device_id=DEVICE_ID,
        expected_key_id=SERVER_KEY_ID,
        now=NOW,
    )
    assert unsigned["kind"] == "COMMAND"


def test_spoofed_device_or_unverified_certificate_never_becomes_a_principal() -> None:
    client, _, orchestration, _, _, headers, _ = _fixture()
    spoofed = client.get(
        f"{WORKER_POLL_PATH}?device_id=other_device",
        headers=headers,
    )
    assert spoofed.status_code == 400
    assert spoofed.json()["error"]["code"] == "WORKER_DEVICE_SELECTION_INVALID"
    assert orchestration.units == []

    unverified_headers = {**headers, TLS_CLIENT_VERIFY_HEADER: "FAILED"}
    unverified = client.get(
        f"{WORKER_POLL_PATH}?device_id={DEVICE_ID}",
        headers=unverified_headers,
    )
    assert unverified.status_code == 401
    assert unverified.json()["error"]["code"] == "WORKER_MTLS_VERIFICATION_REQUIRED"

    changed_certificate, _ = _escaped_certificate(
        b"\x30different-worker-certificate-der"
    )
    mismatch = client.get(
        f"{WORKER_POLL_PATH}?device_id={DEVICE_ID}",
        headers={**headers, TLS_CLIENT_CERTIFICATE_HEADER: changed_certificate},
    )
    assert mismatch.status_code == 401

    remote_client, *_ = _fixture(client_host="198.51.100.8")
    remote = remote_client.get(
        f"{WORKER_POLL_PATH}?device_id={DEVICE_ID}",
        headers=headers,
    )
    assert remote.status_code == 401
    assert remote.json()["error"]["code"] == "WORKER_TRANSPORT_PROXY_REQUIRED"


def test_signed_heartbeat_is_durably_acked_and_exact_duplicate_is_explicit() -> None:
    client, repository, orchestration, device_signer, _, headers, _ = _fixture()
    envelope = _signed_inbound(
        device_signer,
        sequence=1,
        kind="HEARTBEAT",
        body={
            "service_state": "ONLINE",
            "session_state": "AVAILABLE",
            "release_hold": False,
            "active_jobs": 0,
            "worker_version": "1.0.0",
        },
    )
    accepted = client.post(WORKER_MESSAGES_PATH, headers=headers, json=envelope)
    duplicate = client.post(WORKER_MESSAGES_PATH, headers=headers, json=envelope)
    assert accepted.status_code == 202
    assert accepted.json()["duplicate"] is False
    assert duplicate.status_code == 409
    assert duplicate.headers["X-Worker-Message-Status"] == "already-accepted"
    assert duplicate.json()["duplicate"] is True
    assert repository.heartbeat_calls == 2
    assert all(unit.committed for unit in orchestration.units)


def test_invalid_ed25519_signature_is_rejected_before_durable_message_write() -> None:
    client, repository, _, device_signer, _, headers, _ = _fixture()
    envelope = dict(
        _signed_inbound(
            device_signer,
            sequence=1,
            kind="HEARTBEAT",
            body={
                "service_state": "ONLINE",
                "session_state": "AVAILABLE",
                "release_hold": False,
                "active_jobs": 0,
                "worker_version": "1.0.0",
            },
        )
    )
    envelope["signature"] = base64.b64encode(b"\0" * 64).decode("ascii")
    response = client.post(WORKER_MESSAGES_PATH, headers=headers, json=envelope)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WORKER_PROTOCOL_INVALID"
    assert repository.heartbeat_calls == 0


def test_job_status_duplicate_uses_repository_durable_ack_semantics() -> None:
    client, repository, _, device_signer, _, headers, _ = _fixture()
    message_id = str(uuid.uuid4())
    envelope = _signed_inbound(
        device_signer,
        sequence=2,
        kind="JOB_STATUS",
        message_id=message_id,
        body={
            "job_id": "62497802-499c-4e62-86e2-ea7a92d9d741",
            "dispatch_message_id": "e380856b-bf71-4a3a-9f35-c6cb55f747c6",
            "dispatch_authorization_id": DISPATCH_AUTHORIZATION_ID,
            "status": "SUCCEEDED",
            "process_confirmed": True,
            "result": {"installed": True},
            "error_code": None,
        },
    )
    assert client.post(WORKER_MESSAGES_PATH, headers=headers, json=envelope).status_code == 202
    duplicate = client.post(WORKER_MESSAGES_PATH, headers=headers, json=envelope)
    assert duplicate.status_code == 409
    assert duplicate.headers["X-Worker-Message-Status"] == "already-accepted"
    assert repository.status_messages == {
        message_id: hashlib.sha256(canonical_json_bytes(dict(envelope))).hexdigest()
    }


def test_package_requires_exact_live_dispatch_and_rechecks_archive_digest() -> None:
    client, repository, _, _, _, headers, archive_reader = _fixture()
    route = (
        f"{WORKER_PACKAGE_PREFIX}{PLUGIN_ID}/{PLUGIN_VERSION}/"
        f"{PACKAGE_SHA256}/{DISPATCH_AUTHORIZATION_ID}"
    )
    response = client.get(route, headers=headers)
    assert response.status_code == 200
    assert response.content == PACKAGE
    assert response.headers["Cache-Control"] == "no-store"
    assert repository.package_calls == [
        {
            "device_id": DEVICE_ID,
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "package_sha256": PACKAGE_SHA256,
            "dispatch_authorization_id": DISPATCH_AUTHORIZATION_ID,
        }
    ]
    assert len(archive_reader.calls) == 1

    archive_reader.payload = b"tampered-package"
    tampered = client.get(route, headers=headers)
    assert tampered.status_code == 503
    assert tampered.json()["error"]["code"] == "WORKER_TRANSPORT_UNAVAILABLE"

    repository.package_authorized = False
    denied = client.get(route, headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "WORKER_PACKAGE_NOT_AUTHORIZED"


def test_release_hold_blocks_both_command_claim_and_package_download() -> None:
    client, repository, _, _, _, headers, _ = _fixture(held=True)
    poll = client.get(
        f"{WORKER_POLL_PATH}?device_id={DEVICE_ID}",
        headers=headers,
    )
    assert poll.status_code == 503
    assert poll.json()["error"]["code"] == "WORKER_RELEASE_HOLD"
    package = client.get(
        (
            f"{WORKER_PACKAGE_PREFIX}{PLUGIN_ID}/{PLUGIN_VERSION}/"
            f"{PACKAGE_SHA256}/{DISPATCH_AUTHORIZATION_ID}"
        ),
        headers=headers,
    )
    assert package.status_code == 503
    assert package.json()["error"]["code"] == "WORKER_RELEASE_HOLD"
    assert repository.dispatch is None
    assert repository.package_calls == []


def test_package_route_fails_closed_without_repository_authorization_contract() -> None:
    client, repository, _, _, _, headers, _ = _fixture()
    repository.authorize_worker_package_download = None  # type: ignore[assignment]
    route = (
        f"{WORKER_PACKAGE_PREFIX}{PLUGIN_ID}/{PLUGIN_VERSION}/"
        f"{PACKAGE_SHA256}/{DISPATCH_AUTHORIZATION_ID}"
    )
    response = client.get(route, headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WORKER_TRANSPORT_UNAVAILABLE"


def test_server_signer_requires_explicit_protected_ed25519_file(tmp_path: Path) -> None:
    with pytest.raises((FileNotFoundError, ValueError)):
        load_worker_server_signer(
            private_key_path=tmp_path / "missing.pem",
            key_id=SERVER_KEY_ID,
        )

    private_key = ECC.generate(curve="Ed25519")
    key_path = tmp_path / "worker-server.pem"
    key_path.write_text(private_key.export_key(format="PEM"), encoding="ascii")
    key_path.chmod(0o600)
    signer = load_worker_server_signer(
        private_key_path=key_path,
        key_id=SERVER_KEY_ID,
    )
    message = b"worker-command"
    trust = Ed25519TrustStore(
        {SERVER_KEY_ID: private_key.public_key().export_key(format="raw")}
    )
    trust.verify(
        key_id=SERVER_KEY_ID,
        message=message,
        signature=signer.sign(message),
    )


def test_filesystem_package_reader_enforces_storage_containment_and_digest(
    tmp_path: Path,
) -> None:
    from agent.automation_plugins.storage import FilesystemPluginStorage

    storage = FilesystemPluginStorage(tmp_path / "plugins")
    install_root = storage.root / PLUGIN_ID / f"{PLUGIN_VERSION}-{'a' * 12}"
    install_root.mkdir(parents=True)
    archive = install_root / "package-archive.zip"
    archive.write_bytes(PACKAGE)
    authorization = {
        "package_sha256": PACKAGE_SHA256,
        "trust_source": "ed25519_upload",
        "install_root_metadata_json": {
            "install_root": str(install_root),
            "archive_relative": "package-archive.zip",
            "archive_sha256": PACKAGE_SHA256,
        },
    }
    reader = FilesystemWorkerPackageArchiveReader(storage)
    assert reader.read_authorized_archive(authorization) == PACKAGE

    with pytest.raises(Exception, match="metadata is inconsistent"):
        reader.read_authorized_archive(
            {**authorization, "trust_source": "builtin_development"}
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "package-archive.zip").write_bytes(PACKAGE)
    with pytest.raises(Exception, match="outside plugin storage"):
        reader.read_authorized_archive(
            {
                **authorization,
                "install_root_metadata_json": {
                    **authorization["install_root_metadata_json"],
                    "install_root": str(outside),
                },
            }
        )
