"""Production plugin storage configuration written by the release for its own SHA."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agent.automation_plugins.errors import PluginPackageError


PLUGIN_ROOT_ENV = "BOYI_AUTOMATION_PLUGIN_ROOT"
RELEASE_SHA_ENV = "BOYI_AUTOMATION_PLUGIN_RELEASE_SHA"
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class ProductionPluginReleaseConfig:
    plugin_root: Path
    release_sha: str

    @property
    def storage_root(self) -> Path:
        """Installed plugin versions and their isolated environments."""
        return self.plugin_root / "installed"

    @property
    def runtime_root(self) -> Path:
        """Broker sockets of the running Agent."""
        return self.plugin_root / "runtime"


def load_production_plugin_release_config(
    environ: Mapping[str, str],
    *,
    runtime_release_sha: str,
) -> ProductionPluginReleaseConfig:
    """Require the release-written EnvironmentFile contract with no fallback."""

    missing = [name for name in (PLUGIN_ROOT_ENV, RELEASE_SHA_ENV) if not str(environ.get(name) or "").strip()]
    if missing:
        raise PluginPackageError("plugin release configuration is missing: " + ", ".join(missing))
    release_sha = str(environ[RELEASE_SHA_ENV]).strip().lower()
    runtime = str(runtime_release_sha or "").strip().lower()
    if not _RELEASE_SHA_RE.fullmatch(release_sha) or release_sha != runtime:
        raise PluginPackageError("plugin release SHA does not match the running release")
    raw_root = Path(str(environ[PLUGIN_ROOT_ENV]))
    if not raw_root.is_absolute() or raw_root.is_symlink() or not raw_root.is_dir():
        raise PluginPackageError("plugin root does not exist or is unsafe")
    return ProductionPluginReleaseConfig(plugin_root=raw_root.resolve(), release_sha=release_sha)


__all__ = [
    "PLUGIN_ROOT_ENV",
    "ProductionPluginReleaseConfig",
    "RELEASE_SHA_ENV",
    "load_production_plugin_release_config",
]
