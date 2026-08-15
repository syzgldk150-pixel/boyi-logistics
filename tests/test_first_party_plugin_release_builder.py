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
from scripts.build_first_party_plugin_release import main


def _test_key(tmp_path: Path):
    from Crypto.PublicKey import ECC

    private_key = ECC.generate(curve="Ed25519")
    key_path = tmp_path / "test-signing-key.pem"
    key_path.write_text(private_key.export_key(format="PEM"), encoding="ascii")
    if os.name != "nt":
        key_path.chmod(0o600)
    return private_key, key_path


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
