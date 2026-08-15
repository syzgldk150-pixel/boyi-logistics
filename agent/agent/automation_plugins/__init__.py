"""Signed reusable automation actions and instance-bound execution."""

from agent.automation_plugins.catalog import (
    CompositeToolRegistry,
    PluginCatalog,
    PluginCatalogEntry,
    project_contract_fragment,
)
from agent.automation_plugins.broker import (
    LocalBrokerCapabilityIssuer,
    LocalCoreAutomationBroker,
)
from agent.automation_plugins.configuration import AutomationProjectConfigurationService
from agent.automation_plugins.core_adapter import (
    AccountManagerSessionResolver,
    CoreBrokerInvocationContext,
    RegisteredCoreAutomationBrokerAdapter,
)
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.automation_plugins.first_party import (
    FilesystemFirstPartyPackageMaterializer,
    FirstPartyReleasePreflight,
    SignedFirstPartyPackageProvider,
    SourceFirstPartyPackageProvider,
    bootstrap_first_party_plugins,
    preflight_signed_first_party_release,
)
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.manifest import AutomationPluginManifest
from agent.automation_plugins.package import Ed25519TrustStore
from agent.automation_plugins.release_config import (
    ProductionPluginReleaseConfig,
    load_production_plugin_release_config,
)

__all__ = [
    "AutomationPluginManifest",
    "AutomationPluginService",
    "AutomationProjectConfigurationService",
    "AccountManagerSessionResolver",
    "CompositeToolRegistry",
    "CoreBrokerInvocationContext",
    "Ed25519TrustStore",
    "FilesystemFirstPartyPackageMaterializer",
    "FirstPartyReleasePreflight",
    "LocalBrokerCapabilityIssuer",
    "LocalCoreAutomationBroker",
    "PluginCatalog",
    "PluginCatalogEntry",
    "PluginExecutionRouter",
    "ProductionPluginReleaseConfig",
    "RegisteredCoreAutomationBrokerAdapter",
    "SignedFirstPartyPackageProvider",
    "SourceFirstPartyPackageProvider",
    "bootstrap_first_party_plugins",
    "compile_instance_arguments",
    "load_production_plugin_release_config",
    "project_contract_fragment",
    "preflight_signed_first_party_release",
]
