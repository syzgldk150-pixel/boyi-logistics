from __future__ import annotations

import os
from pathlib import Path

import pytest
from Crypto.PublicKey import ECC

from agent.automation_plugins.package import Ed25519TrustStore
from agent.windows_worker.dpapi import (
    load_ed25519_device_signer,
    protect_machine_secret,
    read_protected_secret,
    unprotect_machine_secret,
    write_protected_secret,
)


def test_dpapi_round_trip_is_machine_bound_and_never_plaintext(tmp_path: Path) -> None:
    entropy = b"boyi-worker:test-device"
    secret = b"test-only-secret-material"
    if os.name != "nt":
        with pytest.raises(RuntimeError, match="requires Windows"):
            protect_machine_secret(secret, entropy=entropy)
        return
    protected = protect_machine_secret(secret, entropy=entropy)
    assert protected != secret and secret not in protected
    assert unprotect_machine_secret(protected, entropy=entropy) == secret
    target = write_protected_secret(tmp_path / "device-secret.dpapi", secret, entropy=entropy)
    assert secret not in target.read_bytes()
    assert read_protected_secret(target, entropy=entropy) == secret


def test_dpapi_ed25519_signer_loads_private_material_in_memory_only(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("real DPAPI signer test runs on windows-latest")
    key = ECC.generate(curve="Ed25519")
    private_pem = key.export_key(format="PEM").encode("utf-8")
    entropy = b"boyi-worker:test-device-key"
    target = write_protected_secret(tmp_path / "device-key.dpapi", private_pem, entropy=entropy)
    signer = load_ed25519_device_signer(
        target,
        entropy=entropy,
        key_id="device-test",
    )
    message = b"signed worker envelope"
    signature = signer.sign(message)
    trust = Ed25519TrustStore(
        {"device-test": key.public_key().export_key(format="raw")}
    )
    trust.verify(key_id="device-test", message=message, signature=signature)
