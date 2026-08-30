"""Single credential-free environment builder for plugin subprocesses."""

from __future__ import annotations

import os
from collections.abc import Mapping


_SAFE_INHERITED_ENVIRONMENT = (
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "TZ",
)


def minimal_plugin_environment(
    *,
    capability: str,
    automation_id: str,
    plugin_id: str,
    plugin_version: str,
    broker_endpoint: str,
    broker_call_timeout_seconds: int,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the closed environment shared by production and offline tests."""

    source = os.environ if inherited is None else inherited
    environment = {
        "PATH": os.defpath,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "",
        "PYTHON_DOTENV_DISABLED": "1",
        "BOYI_PLUGIN_EXECUTION_CAPABILITY": capability,
        "BOYI_AUTOMATION_ID": automation_id,
        "BOYI_PLUGIN_ID": plugin_id,
        "BOYI_PLUGIN_VERSION": plugin_version,
        "BOYI_PLUGIN_BROKER_ENDPOINT": broker_endpoint,
        "BOYI_PLUGIN_BROKER_CALL_TIMEOUT": str(broker_call_timeout_seconds),
    }
    for name in _SAFE_INHERITED_ENVIRONMENT:
        value = source.get(name)
        if value:
            environment[name] = str(value)
    return environment


__all__ = ["minimal_plugin_environment"]
