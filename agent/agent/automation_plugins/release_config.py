"""Strict production release configuration for signed plugin artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agent.automation_plugins.errors import PluginPackageError


ARTIFACT_ROOT_ENV = "BOYI_AUTOMATION_PLUGIN_ARTIFACT_ROOT"
TRUST_ROOT_ENV = "BOYI_AUTOMATION_PLUGIN_TRUST_ROOT"
VERIFIED_RELEASE_SHA_ENV = "BOYI_AUTOMATION_PLUGIN_VERIFIED_RELEASE_SHA"
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class ProductionPluginReleaseConfig:
    artifact_root: Path
    trust_root: Path
    verified_release_sha: str


def load_production_plugin_release_config(
    environ: Mapping[str, str],
    *,
    runtime_release_sha: str,
) -> ProductionPluginReleaseConfig:
    """Require the release-written EnvironmentFile contract with no fallback."""

    missing = [
        name
        for name in (ARTIFACT_ROOT_ENV, TRUST_ROOT_ENV, VERIFIED_RELEASE_SHA_ENV)
        if not str(environ.get(name) or "").strip()
    ]
    if missing:
        raise PluginPackageError(
            "signed plugin release configuration is missing: " + ", ".join(missing)
        )
    verified = str(environ[VERIFIED_RELEASE_SHA_ENV]).strip().lower()
    runtime = str(runtime_release_sha or "").strip().lower()
    if not _RELEASE_SHA_RE.fullmatch(verified) or verified != runtime:
        raise PluginPackageError("plugin release SHA does not match the running release")
    artifact = Path(str(environ[ARTIFACT_ROOT_ENV])).resolve()
    trust = Path(str(environ[TRUST_ROOT_ENV])).resolve()
    if not artifact.is_absolute() or artifact.name.lower() != verified:
        raise PluginPackageError("plugin artifact root is not bound to the verified release SHA")
    if artifact.is_symlink() or not artifact.is_dir():
        raise PluginPackageError("plugin artifact root does not exist or is unsafe")
    if trust.is_symlink() or not trust.is_dir():
        raise PluginPackageError("plugin trust root does not exist or is unsafe")
    if artifact == trust or artifact in trust.parents or trust in artifact.parents:
        raise PluginPackageError("plugin artifact and trust roots must be independent")
    return ProductionPluginReleaseConfig(
        artifact_root=artifact,
        trust_root=trust,
        verified_release_sha=verified,
    )


__all__ = [
    "ARTIFACT_ROOT_ENV",
    "ProductionPluginReleaseConfig",
    "TRUST_ROOT_ENV",
    "VERIFIED_RELEASE_SHA_ENV",
    "load_production_plugin_release_config",
]
