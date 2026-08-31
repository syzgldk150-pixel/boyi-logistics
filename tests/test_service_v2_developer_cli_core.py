from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.automation_plugins import developer_v2
from agent.automation_plugins.developer_v2 import (
    ServiceV2DeveloperError,
    build_service_v2_package,
    init_service_v2_source,
    inspect_service_v2_artifact,
    load_local_json_object,
    load_verified_local_artifact,
    validate_service_v2_artifact,
)
from agent.automation_plugins import package_v2
from agent.automation_plugins.errors import PluginManifestError, PluginPackageError
from agent.automation_plugins.service_v2_contract import ServiceV2ProjectContract
from scripts import service_v2_plugin


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = (
    PROJECT_ROOT
    / "agent"
    / "extension_sdk"
    / "schemas"
    / "manifest-v2.schema.json"
)


def _init(tmp_path: Path, *, plugin_id: str = "sample_compute") -> Path:
    return init_service_v2_source(
        tmp_path / plugin_id,
        plugin_id=plugin_id,
        name="Sample compute",
        version="1.2.3",
    )


def test_init_build_and_direct_zip_load_are_deterministic_and_sdk_owned(
    tmp_path: Path,
) -> None:
    source = _init(tmp_path)
    assert sorted(item.relative_to(source).as_posix() for item in source.rglob("*")) == [
        "manifest.json",
        "payload",
        "payload/main.py",
    ]

    from_source = load_verified_local_artifact(source)
    first = build_service_v2_package(source, tmp_path / "first.zip")
    second = build_service_v2_package(source, tmp_path / "second.zip")

    assert first == tmp_path / "first.zip"
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    assert load_verified_local_artifact(first).package_sha256 == from_source.package_sha256
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "manifest.json",
            "payload/boyi_plugin_sdk.py",
            "payload/main.py",
        ]
        assert archive.read("payload/boyi_plugin_sdk.py") == (
            PROJECT_ROOT
            / "agent"
            / "service_v2_plugins"
            / "_shared"
            / "boyi_plugin_sdk.py"
        ).read_bytes()


def test_init_template_runs_as_a_closed_compute_result(tmp_path: Path) -> None:
    source = _init(tmp_path)
    verified = load_verified_local_artifact(source)
    contract = ServiceV2ProjectContract.from_manifest(verified.manifest)
    invocation = contract.invocation_contracts["run"]
    request = {
        "schema_version": 2,
        "runtime_model": "SERVICE_V2",
        "automation_id": "offline-example",
        "plugin_id": "sample_compute",
        "plugin_version": "1.2.3",
        "entrypoint": "console",
        "target": {
            "service": invocation["service"],
            "operation": invocation["operation"],
            "contribution_id": "run",
            "contribution_kind": "console",
        },
        "governance": dict(invocation["governance"]),
        "arguments": {},
    }
    completed = subprocess.run(
        [sys.executable, str(source / "payload" / "main.py")],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=True,
        env={
            "BOYI_AUTOMATION_ID": "offline-example",
            "BOYI_PLUGIN_ID": "sample_compute",
            "BOYI_PLUGIN_VERSION": "1.2.3",
        },
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "SUCCESS"
    assert result["error"] is None
    assert result["warnings"] == []
    assert result["data"] == {"message": "Service v2 example is ready."}
    assert result["meta"]["source_system"] == "service_v2_example"
    assert result["meta"]["record_count"] == 1
    assert result["meta"]["pagination_complete"] is True
    assert result["meta"]["evidence_refs"] == []
    assert "account_id" not in result["meta"]
    assert datetime.fromisoformat(result["meta"]["observed_at"].replace("Z", "+00:00")).tzinfo

    drifted_request = json.loads(json.dumps(request))
    drifted_request["governance"]["unexpected"] = True
    drifted = subprocess.run(
        [sys.executable, str(source / "payload" / "main.py")],
        input=json.dumps(drifted_request),
        text=True,
        capture_output=True,
        check=True,
        env={
            "BOYI_AUTOMATION_ID": "offline-example",
            "BOYI_PLUGIN_ID": "sample_compute",
            "BOYI_PLUGIN_VERSION": "1.2.3",
        },
    )
    assert json.loads(drifted.stdout)["error"]["code"] == "INVALID_REQUEST"


def test_init_validates_identity_before_creating_destination(tmp_path: Path) -> None:
    destination = tmp_path / "invalid"
    with pytest.raises(PluginManifestError, match="plugin_id"):
        init_service_v2_source(destination, plugin_id="Invalid Plugin")
    assert not destination.exists()


def test_init_failure_removes_only_known_created_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "cleanup"

    def fail_after_write(*args: object, **kwargs: object) -> object:
        raise RuntimeError("offline validation failure")

    monkeypatch.setattr(developer_v2, "_scan_source_from_anchor", fail_after_write)
    with pytest.raises(RuntimeError, match="offline validation failure"):
        init_service_v2_source(destination, plugin_id="cleanup_example")
    assert not destination.exists()


def test_init_cleanup_failure_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "cleanup_blocked"
    original_unlink = developer_v2.os.unlink

    def fail_after_write(*args: object, **kwargs: object) -> object:
        raise RuntimeError("offline validation failure")

    def guarded_unlink(path: str, *args: object, **kwargs: object) -> None:
        if path == "main.py":
            raise PermissionError("blocked for test")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(developer_v2, "_scan_source_from_anchor", fail_after_write)
    monkeypatch.setattr(developer_v2.os, "unlink", guarded_unlink)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        init_service_v2_source(destination, plugin_id="cleanup_blocked")
    assert captured.value.code == "INIT_CLEANUP_FAILED"


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "credentials.json",
        "private_key.pem",
        "client-cert.p12",
        "session-state.json",
    ],
)
def test_sensitive_names_are_classified_without_filesystem_reads(name: str) -> None:
    assert developer_v2._is_sensitive_entry_name(name) is True


def test_sensitive_source_candidate_is_rejected_before_any_content_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    (source / "payload" / "credentials.json").touch()

    def content_read_forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("sensitive candidate content must not be read")

    monkeypatch.setattr(developer_v2, "_read_scanned_regular_file", content_read_forbidden)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(source)
    assert captured.value.code == "SENSITIVE_SOURCE_NAME"


@pytest.mark.parametrize(
    "member_name",
    [
        "payload/nested/.env.local",
        "payload\\nested\\credentials.json",
        "payload/nested/session-token.txt",
    ],
)
def test_zip_sensitive_path_is_rejected_from_central_directory_before_member_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member_name: str,
) -> None:
    candidate = tmp_path / "candidate.zip"
    with zipfile.ZipFile(candidate, "w") as archive:
        # Empty data proves the test does not create or inspect sensitive content.
        archive.writestr(member_name, b"")

    def member_read_forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("ZIP member decompression must not occur before rejection")

    monkeypatch.setattr(package_v2, "_read_member", member_read_forbidden)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(candidate)
    assert captured.value.code == "SENSITIVE_SOURCE_NAME"


def test_source_cannot_supply_host_owned_sdk_before_member_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    (source / "payload" / "boyi_plugin_sdk.py").touch()

    def content_read_forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("source-owned SDK content must not be read")

    monkeypatch.setattr(developer_v2, "_read_scanned_regular_file", content_read_forbidden)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(source)
    assert captured.value.code == "SDK_SOURCE_FORBIDDEN"


def test_source_rejects_symlinks_and_unexpected_root_entries(tmp_path: Path) -> None:
    source = _init(tmp_path)
    link = source / "payload" / "linked.py"
    try:
        link.symlink_to(source / "payload" / "main.py")
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(source)
    assert captured.value.code == "UNSAFE_SOURCE_OBJECT"
    link.unlink()
    (source / "README.md").write_text("offline example\n", encoding="utf-8")
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(source)
    assert captured.value.code == "SOURCE_LAYOUT_INVALID"


def test_source_rejects_ancestor_symlink_before_inspecting_members(tmp_path: Path) -> None:
    real_parent = tmp_path / "real_parent"
    real_parent.mkdir()
    source = _init(real_parent)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(alias / source.name)
    assert captured.value.code in {
        "LOCAL_ARTIFACT_UNAVAILABLE",
        "LOCAL_ARTIFACT_NOT_FOUND",
    }


def test_source_parent_swap_is_detected_before_payload_member_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_parent = tmp_path / "source_parent"
    source_parent.mkdir()
    source = _init(source_parent)
    moved_parent = tmp_path / "source_parent_moved"
    replacement = tmp_path / "replacement_parent"
    replacement.mkdir()
    original_assert = developer_v2._assert_directory_path_matches
    swapped = False

    def swap_before_path_check(
        path: Path,
        expected: object,
        **kwargs: object,
    ) -> None:
        nonlocal swapped
        if path == source and not swapped:
            swapped = True
            source_parent.rename(moved_parent)
            source_parent.symlink_to(replacement, target_is_directory=True)
        original_assert(path, expected, **kwargs)

    def member_read_forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("source member read must not occur after path swap")

    monkeypatch.setattr(
        developer_v2,
        "_assert_directory_path_matches",
        swap_before_path_check,
    )
    monkeypatch.setattr(developer_v2, "_read_scanned_regular_file", member_read_forbidden)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(source)
    assert captured.value.code == "LOCAL_ARTIFACT_CHANGED"


def test_source_leaf_swap_after_type_check_is_rejected_before_member_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    replacement = _init(tmp_path, plugin_id="replacement_compute")
    moved_source = tmp_path / "original_compute"
    original_open = developer_v2.os.open
    swapped = False

    def swap_before_source_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            path == source.name
            and dir_fd is not None
            and flags & os.O_DIRECTORY
            and not swapped
        ):
            swapped = True
            source.rename(moved_source)
            replacement.rename(source)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def member_read_forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("replacement source members must not be read")

    monkeypatch.setattr(developer_v2.os, "open", swap_before_source_open)
    monkeypatch.setattr(developer_v2, "_read_scanned_regular_file", member_read_forbidden)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_verified_local_artifact(source)
    assert captured.value.code == "LOCAL_ARTIFACT_CHANGED"
    assert moved_source.is_dir()
    assert source.is_dir()


def test_source_aggregate_size_is_rejected_before_member_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    extra = source / "payload" / "extra.bin"
    extra.write_bytes(b"bounded")
    source_size = sum(
        item.stat().st_size
        for item in (source / "manifest.json", source / "payload" / "main.py", extra)
    )

    def member_read_forbidden(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("oversize source members must not be read")

    monkeypatch.setattr(
        developer_v2,
        "MAX_TOTAL_UNCOMPRESSED_BYTES",
        source_size - 1,
    )
    monkeypatch.setattr(developer_v2, "_read_scanned_regular_file", member_read_forbidden)
    with pytest.raises(PluginPackageError, match="package size limit"):
        load_verified_local_artifact(source)


def test_local_json_loader_is_strict_bounded_and_name_safe(tmp_path: Path) -> None:
    valid = tmp_path / "scenarios.json"
    valid.write_text('{"scenario":"compute"}', encoding="utf-8")
    assert load_local_json_object(valid) == {"scenario": "compute"}

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"scenario":1,"scenario":2}', encoding="utf-8")
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_local_json_object(duplicate)
    assert captured.value.code == "LOCAL_JSON_INVALID"

    non_finite = tmp_path / "non_finite.json"
    non_finite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ServiceV2DeveloperError):
        load_local_json_object(non_finite)

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(ServiceV2DeveloperError):
        load_local_json_object(array)

    with pytest.raises(PluginPackageError, match="size limit"):
        load_local_json_object(valid, maximum_bytes=1)


def test_sensitive_json_candidate_is_rejected_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "credentials.json"
    candidate.touch()

    def open_forbidden(*args: object, **kwargs: object) -> int:
        raise AssertionError("sensitive candidate must not be opened")

    monkeypatch.setattr(developer_v2.os, "open", open_forbidden)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        load_local_json_object(candidate)
    assert captured.value.code == "SENSITIVE_SOURCE_NAME"


def test_regular_file_reader_rejects_same_inode_metadata_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "scenario.json"
    candidate.write_text('{"kind":"compute"}', encoding="utf-8")
    scanned = developer_v2._scan_regular_file(candidate, candidate.name)
    expected = scanned.stat_result
    calls = 0

    def drifting_fstat(descriptor: int) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            st_mode=expected.st_mode,
            st_dev=expected.st_dev,
            st_ino=expected.st_ino,
            st_size=expected.st_size,
            st_mtime_ns=expected.st_mtime_ns + int(calls > 1),
            st_ctime_ns=expected.st_ctime_ns,
        )

    monkeypatch.setattr(developer_v2.os, "fstat", drifting_fstat)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        developer_v2._read_scanned_regular_file(scanned, maximum=1024)
    assert captured.value.code == "LOCAL_ARTIFACT_CHANGED"


def test_package_refuses_overwrite_and_cleans_sibling_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        build_service_v2_package(source, existing)
    assert existing.read_bytes() == b"preserve"

    output = tmp_path / "failed.zip"

    def fail_link(*args: object, **kwargs: object) -> None:
        raise OSError("offline link failure")

    monkeypatch.setattr(developer_v2.os, "link", fail_link)
    with pytest.raises(OSError, match="offline link failure"):
        build_service_v2_package(source, output)
    assert not output.exists()
    assert list(tmp_path.glob(".failed.zip.*.tmp")) == []


def test_package_parent_swap_cannot_redirect_or_publish_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    parent = tmp_path / "publish"
    parent.mkdir()
    moved_parent = tmp_path / "publish_moved"
    attacker_parent = tmp_path / "attacker"
    attacker_parent.mkdir()
    original_link = developer_v2.os.link

    def swap_before_link(*args: object, **kwargs: object) -> None:
        parent.rename(moved_parent)
        parent.symlink_to(attacker_parent, target_is_directory=True)
        original_link(*args, **kwargs)

    monkeypatch.setattr(developer_v2.os, "link", swap_before_link)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        build_service_v2_package(source, parent / "artifact.zip")
    assert captured.value.code == "OUTPUT_PARENT_CHANGED"
    assert not (attacker_parent / "artifact.zip").exists()
    assert not (moved_parent / "artifact.zip").exists()


def test_package_leaf_replacement_is_detected_and_never_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    output = tmp_path / "artifact.zip"
    original_link = developer_v2.os.link

    def replace_after_link(*args: object, **kwargs: object) -> None:
        original_link(*args, **kwargs)
        output.unlink()
        output.write_bytes(b"foreign replacement")

    monkeypatch.setattr(developer_v2.os, "link", replace_after_link)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        build_service_v2_package(source, output)
    assert captured.value.code == "OUTPUT_ARTIFACT_CHANGED"
    assert output.read_bytes() == b"foreign replacement"
    assert list(tmp_path.glob(".artifact.zip.*.tmp")) == []


def test_package_leaf_replacement_after_temporary_unlink_cannot_be_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _init(tmp_path)
    output = tmp_path / "artifact.zip"
    original_metadata = developer_v2._output_entry_metadata
    output_metadata_calls = 0

    def replace_before_post_unlink_snapshot(
        parent_descriptor: int,
        output_name: str,
        *,
        missing_code: str,
    ) -> os.stat_result:
        nonlocal output_metadata_calls
        if output_name == output.name:
            output_metadata_calls += 1
            if output_metadata_calls == 2:
                output.unlink()
                output.write_bytes(b"late foreign replacement")
        return original_metadata(
            parent_descriptor,
            output_name,
            missing_code=missing_code,
        )

    monkeypatch.setattr(
        developer_v2,
        "_output_entry_metadata",
        replace_before_post_unlink_snapshot,
    )
    with pytest.raises(ServiceV2DeveloperError) as captured:
        build_service_v2_package(source, output)
    assert captured.value.code == "OUTPUT_ARTIFACT_CHANGED"
    assert output.read_bytes() == b"late foreign replacement"
    assert list(tmp_path.glob(".artifact.zip.*.tmp")) == []


def test_init_parent_swap_does_not_create_at_replacement_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "init_parent"
    parent.mkdir()
    moved_parent = tmp_path / "init_parent_moved"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    original_mkdir = developer_v2.os.mkdir
    swapped = False

    def swap_before_target_create(path: str, *args: object, **kwargs: object) -> None:
        nonlocal swapped
        if path == "created" and not swapped:
            swapped = True
            parent.rename(moved_parent)
            parent.symlink_to(replacement, target_is_directory=True)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(developer_v2.os, "mkdir", swap_before_target_create)
    with pytest.raises(ServiceV2DeveloperError) as captured:
        init_service_v2_source(parent / "created", plugin_id="created")
    assert captured.value.code == "OUTPUT_PARENT_CHANGED"
    assert not (replacement / "created").exists()
    assert not (moved_parent / "created").exists()


def test_validate_and_inspect_expose_only_safe_derived_material(tmp_path: Path) -> None:
    source = _init(tmp_path)
    validation = validate_service_v2_artifact(source)
    inspection = inspect_service_v2_artifact(source)
    encoded = json.dumps(inspection, ensure_ascii=False, sort_keys=True)

    assert validation["valid"] is True
    assert validation["identity"] == inspection["identity"]
    assert inspection["identity"]["plugin_id"] == "sample_compute"
    assert inspection["contract"]["tool"]["effect"] == "compute"
    assert inspection["wizard"]["permissions"] == []
    assert [item["path"] for item in inspection["members"]] == [
        "manifest.json",
        "payload/boyi_plugin_sdk.py",
        "payload/main.py",
    ]
    assert str(source.resolve()) not in encoded
    assert "Service v2 example is ready." not in encoded
    assert "BOYI_PLUGIN_ID" not in encoded
    assert set(inspection) == {"identity", "members", "contract", "wizard"}


def test_existing_invalid_zip_uses_authoritative_package_verifier(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not-a-zip")
    with pytest.raises(PluginPackageError, match="valid ZIP"):
        load_verified_local_artifact(invalid)


def test_manifest_schema_matches_template_envelope_and_authoritative_contract(
    tmp_path: Path,
) -> None:
    source = _init(tmp_path)
    schema = json.loads(MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(manifest)
    assert schema["properties"]["schema_version"]["const"] == manifest["schema_version"]
    assert schema["properties"]["runtime_model"]["const"] == manifest["runtime_model"]
    assert re.fullmatch(schema["$defs"]["plugin_id"]["pattern"], manifest["plugin_id"])
    assert re.fullmatch(schema["$defs"]["semver"]["pattern"], manifest["version"])
    assert schema["properties"]["runtime"]["properties"]["python"]["const"] == "3.10"
    assert manifest["requires"] == []
    required_service = schema["$defs"]["required_service"]
    assert len(required_service["oneOf"]) == 2
    assert required_service["oneOf"][0]["required"] == ["service"]
    assert required_service["oneOf"][1]["required"] == ["service", "account_role"]
    assert schema["$defs"]["connector_service"]["pattern"].startswith("^connector\\.")
    verified = load_verified_local_artifact(source)
    projected = ServiceV2ProjectContract.from_manifest(verified.manifest)
    assert projected.allowed_entrypoints == ("run",)
    assert projected.tool_contract["effect"] == "compute"


def test_core_cli_returns_stable_json_and_nonzero_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "cli_source"
    assert service_v2_plugin.main(
        ["init", str(source), "--plugin-id", "cli_compute", "--name", "CLI compute"]
    ) == 0
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["ok"] is True
    assert initialized["data"]["identity"]["plugin_id"] == "cli_compute"

    assert service_v2_plugin.main(["validate", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["valid"] is True

    package = tmp_path / "cli.zip"
    assert service_v2_plugin.main(["package", str(source), str(package)]) == 0
    packaged = json.loads(capsys.readouterr().out)
    assert packaged["data"]["identity"]["plugin_id"] == "cli_compute"

    assert service_v2_plugin.main(["inspect", str(package)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["data"]["wizard"]["plugin_id"] == "cli_compute"

    assert service_v2_plugin.main(["package", str(source), str(package)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    failure = json.loads(captured.err)
    assert failure == {
        "error": {
            "code": "LOCAL_TARGET_EXISTS",
            "message": "local target already exists",
        },
        "ok": False,
    }

    assert service_v2_plugin.main([]) == 2
    usage = json.loads(capsys.readouterr().err)
    assert usage["error"]["code"] == "CLI_USAGE_ERROR"


def test_windows_streams_reconfigure_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Stream:
        def __init__(self) -> None:
            self.encodings: list[str] = []

        def reconfigure(self, *, encoding: str) -> None:
            self.encodings.append(encoding)

    stdout = _Stream()
    stderr = _Stream()
    monkeypatch.setattr(service_v2_plugin.sys, "platform", "win32")
    monkeypatch.setattr(service_v2_plugin.sys, "stdout", stdout)
    monkeypatch.setattr(service_v2_plugin.sys, "stderr", stderr)
    service_v2_plugin._configure_windows_streams()
    assert stdout.encodings == ["utf-8"]
    assert stderr.encodings == ["utf-8"]
