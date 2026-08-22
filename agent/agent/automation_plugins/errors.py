"""Fail-closed errors for the automation plugin platform."""

from __future__ import annotations


class AutomationPluginError(RuntimeError):
    """Base error with a stable machine-readable code."""

    code = "AUTOMATION_PLUGIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.code


class PluginManifestError(AutomationPluginError):
    code = "PLUGIN_MANIFEST_INVALID"


class PluginPackageError(AutomationPluginError):
    code = "PLUGIN_PACKAGE_INVALID"


class PluginSignatureError(PluginPackageError):
    code = "PLUGIN_SIGNATURE_INVALID"


class PluginConflictError(AutomationPluginError):
    code = "PLUGIN_VERSION_CONFLICT"


class PluginNotFoundError(AutomationPluginError):
    code = "PLUGIN_NOT_FOUND"


class PluginUninstallBlocked(AutomationPluginError):
    code = "PLUGIN_UNINSTALL_BLOCKED"


class PluginExecutionError(AutomationPluginError):
    code = "PLUGIN_EXECUTION_FAILED"


class WorkerProtocolError(AutomationPluginError):
    code = "WORKER_PROTOCOL_INVALID"
