from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.automation_plugins import first_party as first_party_module

from agent.automation_plugins.first_party import (
    preflight_signed_first_party_release,
    release_first_party_automation_ids,
    release_first_party_plugin_ids,
    verify_release_first_party_source_review,
)
from agent.automation_plugins.package import Ed25519TrustStore
from agent.tool_registry import ToolRegistry
from scripts import build_first_party_plugin_release as builder_module
from scripts.build_first_party_plugin_release import main


def _test_key(tmp_path: Path):
    from Crypto.PublicKey import ECC

    private_key = ECC.generate(curve="Ed25519")
    key_path = tmp_path / "test-signing-key.pem"
    key_path.write_text(private_key.export_key(format="PEM"), encoding="ascii")
    if os.name != "nt":
        key_path.chmod(0o600)
    return private_key, key_path


def _test_trust_root(tmp_path: Path, private_key, *, key_id: str = "test-key") -> Path:
    trust_root = tmp_path / "trust"
    trust_root.mkdir()
    (trust_root / f"{key_id}.pub").write_bytes(
        private_key.public_key().export_key(format="raw")
    )
    return trust_root


def test_builds_complete_atomic_first_party_release(tmp_path: Path) -> None:
    key, key_path = _test_key(tmp_path)
    output = tmp_path / "release"
    release_sha = "a" * 40

    assert main(
        [
            "--private-key",
            str(key_path),
            "--key-id",
            "test-key",
            "--release-sha",
            release_sha,
            "--output-root",
            str(output),
        ]
    ) == 0

    entries = sorted(path.name for path in output.iterdir())
    assert len(entries) == len(release_first_party_plugin_ids()) + 1
    assert entries.count("release-index.json") == 1
    assert sum(name.endswith(".zip") for name in entries) == len(
        release_first_party_plugin_ids()
    )
    index = json.loads((output / "release-index.json").read_text(encoding="utf-8"))
    assert index["release_sha"] == release_sha
    assert set(index["plugins"]) == release_first_party_plugin_ids()

    public_key = key.public_key().export_key(format="raw")
    result = preflight_signed_first_party_release(
        artifact_root=output,
        signature_verifier=Ed25519TrustStore({"test-key": public_key}),
        core_catalog=ToolRegistry(),
        release_sha=release_sha,
    )
    assert result.package_count == len(release_first_party_plugin_ids())
    assert result.instance_count == len(release_first_party_automation_ids())


def test_refuses_to_merge_into_existing_output(tmp_path: Path) -> None:
    _, key_path = _test_key(tmp_path)
    output = tmp_path / "release"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        main(
            [
                "--private-key",
                str(key_path),
                "--key-id",
                "test-key",
                "--release-sha",
                "b" * 40,
                "--output-root",
                str(output),
            ]
        )
    assert marker.read_text(encoding="utf-8") == "keep"


def test_reuses_verified_immutable_packages_for_a_new_release_sha(tmp_path: Path) -> None:
    key, key_path = _test_key(tmp_path)
    trust_root = _test_trust_root(tmp_path, key)
    source = tmp_path / "source-release"
    output = tmp_path / "rebound-release"
    old_sha = "c" * 40
    new_sha = "d" * 40

    assert main(
        [
            "--private-key",
            str(key_path),
            "--key-id",
            "test-key",
            "--release-sha",
            old_sha,
            "--output-root",
            str(source),
        ]
    ) == 0
    source_index_path = source / "release-index.json"
    source_index_path.write_bytes(source_index_path.read_bytes() + b"\n")
    assert main(
        [
            "--reuse-artifact-root",
            str(source),
            "--trust-root",
            str(trust_root),
            "--release-sha",
            new_sha,
            "--output-root",
            str(output),
        ]
    ) == 0

    index = json.loads((output / "release-index.json").read_text(encoding="utf-8"))
    assert index["release_sha"] == new_sha
    assert index["plugins"] == json.loads(
        (source / "release-index.json").read_text(encoding="utf-8")
    )["plugins"]
    for package in source.glob("*.zip"):
        assert (output / package.name).read_bytes() == package.read_bytes()


def test_reuse_rejects_payload_that_no_longer_matches_reviewed_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    key, key_path = _test_key(tmp_path)
    trust_root = _test_trust_root(tmp_path, key)
    source = tmp_path / "source-release"
    output = tmp_path / "rebound-release"
    assert main(
        [
            "--private-key",
            str(key_path),
            "--key-id",
            "test-key",
            "--release-sha",
            "e" * 40,
            "--output-root",
            str(source),
        ]
    ) == 0
    original = builder_module.first_party_payload_files

    def drifted_payload(manifest):
        files = original(manifest)
        if manifest.plugin_id == "clock_in_dual":
            files["payload/action.py"] += b"\n# changed after signing\n"
        return files

    monkeypatch.setattr(builder_module, "first_party_payload_files", drifted_payload)
    with pytest.raises(ValueError, match="does not match the current reviewed source"):
        main(
            [
                "--reuse-artifact-root",
                str(source),
                "--trust-root",
                str(trust_root),
                "--release-sha",
                "f" * 40,
                "--output-root",
                str(output),
            ]
        )
    assert not output.exists()


def test_release_review_rejects_payload_drift_with_unchanged_manifest(
    monkeypatch,
) -> None:
    original = first_party_module.first_party_payload_files

    def drifted_payload(manifest):
        files = original(manifest)
        if manifest.plugin_id == "sync_arrive_list":
            files["payload/action.py"] += b"\n# unreviewed drift\n"
        return files

    monkeypatch.setattr(
        first_party_module,
        "first_party_payload_files",
        drifted_payload,
    )

    with pytest.raises(Exception, match="source is not digest-reviewed"):
        verify_release_first_party_source_review(ToolRegistry())
