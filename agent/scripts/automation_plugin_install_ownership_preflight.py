"""Read-only proof of deterministic roots owned by pre-release plugin rows."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any


_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_AUTOMATION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_METADATA_FIELDS = frozenset(
    {
        "signing_key_id",
        "python_relative",
        "archive_relative",
        "archive_sha256",
        "package_files",
        "install_root",
    }
)


class PluginInstallOwnershipPreflightError(RuntimeError):
    """A value-free reason the ownership proof cannot be emitted."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code)


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise PluginInstallOwnershipPreflightError("INVALID_DATABASE_STATE") from exc
    if not isinstance(value, Mapping):
        raise PluginInstallOwnershipPreflightError("INVALID_DATABASE_STATE")
    return dict(value)


def _digest(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginInstallOwnershipPreflightError("INVALID_DATABASE_STATE") from exc
    return hashlib.sha256(encoded).hexdigest()


def _install_root(value: object) -> PurePosixPath:
    raw = value if isinstance(value, str) else ""
    root = PurePosixPath(raw)
    if (
        not raw
        or not root.is_absolute()
        or root == PurePosixPath("/")
        or str(root) != raw
        or ".." in root.parts
    ):
        raise PluginInstallOwnershipPreflightError("INVALID_INSTALL_ROOT")
    return root


def _identity(row: object, root: PurePosixPath) -> tuple[str, str, str, str]:
    if not isinstance(row, Mapping):
        raise PluginInstallOwnershipPreflightError("INVALID_DATABASE_STATE")
    plugin_id = row.get("plugin_id")
    automation_id = row.get("automation_id")
    version = row.get("version")
    package_sha256 = row.get("package_sha256")
    manifest_sha256 = row.get("manifest_sha256")
    metadata_sha256 = row.get("install_root_metadata_sha256")
    if (
        not isinstance(automation_id, str)
        or not _AUTOMATION_ID_RE.fullmatch(automation_id)
        or not isinstance(plugin_id, str)
        or not _PLUGIN_ID_RE.fullmatch(plugin_id)
        or not isinstance(version, str)
        or not _VERSION_RE.fullmatch(version)
        or not isinstance(package_sha256, str)
        or not _SHA256_RE.fullmatch(package_sha256)
        or not isinstance(manifest_sha256, str)
        or not _SHA256_RE.fullmatch(manifest_sha256)
        or not isinstance(metadata_sha256, str)
        or not _SHA256_RE.fullmatch(metadata_sha256)
        or row.get("trust_source") != "ed25519_first_party"
        or row.get("state") not in {"INSTALLED", "ACTIVE"}
    ):
        raise PluginInstallOwnershipPreflightError("INVALID_DATABASE_STATE")

    manifest = _mapping(row.get("manifest_json"))
    metadata = _mapping(row.get("install_root_metadata_json"))
    expected_root = root / plugin_id / f"{version}-{manifest_sha256[:12]}"
    if (
        manifest.get("plugin_id") != plugin_id
        or manifest.get("version") != version
        or _digest(manifest) != manifest_sha256
        or set(metadata) != _METADATA_FIELDS
        or _digest(metadata) != metadata_sha256
        or metadata.get("install_root") != str(expected_root)
        or metadata.get("archive_relative") != "package-archive.zip"
        or metadata.get("archive_sha256") != package_sha256
        or metadata.get("python_relative") != "venv/bin/python"
        or not isinstance(metadata.get("signing_key_id"), str)
        or not metadata["signing_key_id"]
        or not isinstance(metadata.get("package_files"), list)
    ):
        raise PluginInstallOwnershipPreflightError("INVALID_DATABASE_STATE")
    return plugin_id, version, package_sha256, manifest_sha256


def check_automation_plugin_install_ownership(
    connect: Callable[[], Any],
    install_root: object,
) -> int:
    """Emit only safe immutable identities; never expose persisted metadata."""

    connection = None
    try:
        root = _install_root(install_root)
        connection = connect()
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'automation_plugin_versions'
                """
            )
            if cursor.fetchone() is None:
                rows: list[object] = []
            else:
                cursor.execute(
                    """
                    SELECT 1
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'automation_projects'
                    """
                )
                if cursor.fetchone() is None:
                    raise PluginInstallOwnershipPreflightError(
                        "INVALID_DATABASE_STATE"
                    )
                cursor.execute(
                    """
                    SELECT project.automation_id,
                           version.plugin_id, version.version,
                           version.package_sha256, version.manifest_sha256,
                           version.manifest_json, version.trust_source,
                           version.install_root_metadata_json,
                           version.install_root_metadata_sha256, version.state
                    FROM automation_projects AS project
                    INNER JOIN automation_plugin_versions AS version
                      ON BINARY version.plugin_id = BINARY project.plugin_id
                     AND BINARY version.version = BINARY project.plugin_version
                    WHERE BINARY version.trust_source = BINARY %s
                    ORDER BY BINARY project.automation_id
                    """,
                    ("ed25519_first_party",),
                )
                rows = list(cursor.fetchall())
        identities = sorted({_identity(row, root) for row in rows})
    except PluginInstallOwnershipPreflightError as exc:
        print(
            "automation_plugin_install_ownership=blocked "
            f"reason={exc.code} count=1"
        )
        return 1
    except Exception:
        print(
            "automation_plugin_install_ownership=blocked "
            "reason=PREFLIGHT_RUNTIME_ERROR count=1"
        )
        return 1
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()

    print(f"automation_plugin_install_ownership=ok count={len(identities)}")
    for plugin_id, version, package_sha256, manifest_sha256 in identities:
        print(
            "automation_plugin_install_owner "
            f"plugin_id={plugin_id} version={version} "
            f"package_sha256={package_sha256} "
            f"manifest_sha256={manifest_sha256}"
        )
    return 0
