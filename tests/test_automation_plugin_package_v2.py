from __future__ import annotations

import copy
import hashlib
import json
import stat
import warnings
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from agent.automation_plugins.errors import PluginManifestError, PluginPackageError
from agent.automation_plugins.manifest_v2 import canonical_json_bytes
from agent.automation_plugins.package_v2 import (
    VerifiedPluginPackageV2,
    extract_verified_plugin_package_v2,
    verify_unsigned_plugin_zip_v2,
)


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "runtime_model": "service_v2",
        "plugin_id": "sample_plugin",
        "name": "Sample",
        "version": "1.0.0",
        "description": "Sample service plugin",
        "host_api": {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"},
        "runtime": {
            "kind": "python_subprocess",
            "python": "3.10",
            "mode": "on_demand",
            "entrypoint": "payload/main.py",
            "requirements_lock": None,
            "wheelhouse": [],
        },
        "provides": [
            {
                "service": "plugin.sample_plugin.runner@1",
                "operations": ["run"],
            }
        ],
        "requires": [],
        "capabilities": [],
        "account_roles": [],
        "resource_roles": [],
        "contributes": {
            "console": [
                {
                    "id": "run",
                    "title": "Run",
                    "service": "plugin.sample_plugin.runner@1",
                    "operation": "run",
                    "default_enabled": True,
                }
            ],
            "scheduler": [],
            "webhook": [],
            "feishu": [],
            "events": [],
        },
        "config_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        },
        "storage": {"kv": False, "collections": []},
    }


def _zip(
    entries: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=compression) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, content in entries:
                archive.writestr(name, content, compress_type=compression)
    return stream.getvalue()


def _package(
    manifest: dict[str, object] | None = None,
    *,
    extra: list[tuple[str | zipfile.ZipInfo, bytes]] | None = None,
    main: bytes = b"def run():\n    return {'ok': True}\n",
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    entries: list[tuple[str | zipfile.ZipInfo, bytes]] = [
        (
            "manifest.json",
            json.dumps(manifest or _manifest(), ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
        ),
        ("payload/main.py", main),
    ]
    entries.extend(extra or [])
    return _zip(entries, compression=compression)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_unsigned_v2_zip_returns_closed_record_and_extracts_regular_files(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    runtime = manifest["runtime"]
    assert isinstance(runtime, dict)
    runtime.update(
        {
            "requirements_lock": "payload/requirements.lock",
            "wheelhouse": ["payload/wheelhouse/example-1.0.0-py3-none-any.whl"],
        }
    )
    package_bytes = _package(
        manifest,
        extra=[
            ("payload/requirements.lock", b"example==1.0.0\n"),
            ("payload/wheelhouse/example-1.0.0-py3-none-any.whl", b"PK\x00\xff"),
        ],
    )

    verified = verify_unsigned_plugin_zip_v2(
        package_bytes,
        transport_sha256=_digest(package_bytes),
    )

    assert isinstance(verified, VerifiedPluginPackageV2)
    assert verified.package_bytes == package_bytes
    assert verified.archive_bytes == package_bytes
    assert verified.package_sha256 == _digest(package_bytes)
    assert verified.manifest_mapping == manifest
    assert verified.manifest_sha256 == _digest(canonical_json_bytes(manifest))
    assert {item.path for item in verified.files} == {
        "manifest.json",
        "payload/main.py",
        "payload/requirements.lock",
        "payload/wheelhouse/example-1.0.0-py3-none-any.whl",
    }
    persistence = verified.to_persistence_mapping()
    assert persistence["runtime_model"] == "service_v2"
    assert persistence["plugin_id"] == "sample_plugin"
    assert persistence["manifest_json"] == manifest
    for field in (
        "files_sha256",
        "runtime_sha256",
        "config_schema_sha256",
        "service_contracts_sha256",
        "contributions_sha256",
        "capabilities_sha256",
        "storage_sha256",
    ):
        assert len(str(persistence[field])) == 64

    destination = extract_verified_plugin_package_v2(
        verified,
        tmp_path / "package",
    )
    assert (destination / "manifest.json").is_file()
    assert (destination / "payload" / "main.py").is_file()
    assert not (destination / "signature.json").exists()
    assert not (destination / "payload" / "main.py").is_symlink()


def test_default_scheduler_outside_host_daily_contract_is_rejected_before_install() -> None:
    manifest = _manifest()
    contributes = manifest["contributes"]
    assert isinstance(contributes, dict)
    contributes["scheduler"] = [
        {
            "id": "weekday_run",
            "title": "Weekday run",
            "service": "plugin.sample_plugin.runner@1",
            "operation": "run",
            "default_enabled": True,
            "schedule": {
                "kind": "cron",
                "expression": "5 18 * * 1-5",
                "timezone": "Asia/Shanghai",
            },
        }
    ]
    package_bytes = _package(manifest)

    with pytest.raises(PluginManifestError, match="default scheduler"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256=_digest(package_bytes),
        )


def test_unsigned_v2_zip_requires_exact_transport_digest() -> None:
    package_bytes = _package()

    with pytest.raises(PluginPackageError, match="transport digest"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256="0" * 64,
        )
    with pytest.raises(PluginPackageError, match="64 hexadecimal"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256="not-a-digest",
        )


@pytest.mark.parametrize("unsafe_name", ["../payload/main.py", "/payload/main.py", "C:/main.py"])
def test_unsigned_v2_zip_rejects_path_escape(unsafe_name: str) -> None:
    package_bytes = _zip(
        [
            ("manifest.json", canonical_json_bytes(_manifest())),
            (unsafe_name, b"pass\n"),
        ]
    )

    with pytest.raises(PluginPackageError, match="unsafe|traversal|unsupported"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256=_digest(package_bytes),
        )


def test_unsigned_v2_zip_rejects_symlink_and_duplicate_members() -> None:
    link = zipfile.ZipInfo("payload/main.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    symlink_package = _zip(
        [
            ("manifest.json", canonical_json_bytes(_manifest())),
            (link, b"target.py"),
        ]
    )
    with pytest.raises(PluginPackageError, match="symbolic links"):
        verify_unsigned_plugin_zip_v2(
            symlink_package,
            transport_sha256=_digest(symlink_package),
        )

    duplicate_package = _zip(
        [
            ("manifest.json", canonical_json_bytes(_manifest())),
            ("payload/main.py", b"pass\n"),
            ("payload/MAIN.py", b"pass\n"),
        ]
    )
    with pytest.raises(PluginPackageError, match="duplicate|case-colliding"):
        verify_unsigned_plugin_zip_v2(
            duplicate_package,
            transport_sha256=_digest(duplicate_package),
        )


def test_unsigned_v2_zip_rejects_invalid_utf8_and_compression_bomb() -> None:
    invalid_utf8 = _package(main=b"\xff\xfe")
    with pytest.raises(PluginPackageError, match="UTF-8"):
        verify_unsigned_plugin_zip_v2(
            invalid_utf8,
            transport_sha256=_digest(invalid_utf8),
        )

    compressed_bomb = _package(main=b"a" * (1024 * 1024))
    with pytest.raises(PluginPackageError, match="compression ratio"):
        verify_unsigned_plugin_zip_v2(
            compressed_bomb,
            transport_sha256=_digest(compressed_bomb),
        )


def test_unsigned_v2_zip_rejects_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.automation_plugins import package_v2

    package_bytes = _package()
    monkeypatch.setattr(package_v2, "MAX_FILE_BYTES", 8)

    with pytest.raises(PluginPackageError, match="per-file size limit"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256=_digest(package_bytes),
        )


@pytest.mark.parametrize(
    "path",
    ["payload/ui/panel.js", "payload/post_install.py", "payload/setup.py"],
)
def test_unsigned_v2_zip_rejects_custom_frontend_and_install_hooks(path: str) -> None:
    package_bytes = _package(extra=[(path, b"pass\n")])

    with pytest.raises(PluginPackageError, match="frontend|post-install|build scripts"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256=_digest(package_bytes),
        )


def test_unsigned_v2_zip_rejects_missing_declared_runtime_files() -> None:
    manifest = _manifest()
    runtime = manifest["runtime"]
    assert isinstance(runtime, dict)
    runtime.update(
        {
            "requirements_lock": "payload/requirements.lock",
            "wheelhouse": ["payload/wheelhouse/missing-1.0.0-py3-none-any.whl"],
        }
    )
    without_lock = _package(manifest)
    with pytest.raises(PluginPackageError, match="requirements lock"):
        verify_unsigned_plugin_zip_v2(
            without_lock,
            transport_sha256=_digest(without_lock),
        )

    without_wheel = _package(
        manifest,
        extra=[("payload/requirements.lock", b"missing==1.0.0\n")],
    )
    with pytest.raises(PluginPackageError, match="wheelhouse"):
        verify_unsigned_plugin_zip_v2(
            without_wheel,
            transport_sha256=_digest(without_wheel),
        )


def test_unsigned_v2_zip_rejects_invalid_manifest_without_v1_fallback() -> None:
    invalid = copy.deepcopy(_manifest())
    invalid["runtime_model"] = "action_v1"
    package_bytes = _package(invalid)

    with pytest.raises(PluginManifestError, match="runtime_model must be service_v2"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256=_digest(package_bytes),
        )


def test_unsigned_v2_zip_rejects_manifest_post_install_hook_before_runtime() -> None:
    invalid = _manifest()
    invalid["post_install"] = {"entrypoint": "payload/main.py"}
    package_bytes = _package(invalid)

    with pytest.raises(PluginPackageError, match="post-install hooks"):
        verify_unsigned_plugin_zip_v2(
            package_bytes,
            transport_sha256=_digest(package_bytes),
        )
