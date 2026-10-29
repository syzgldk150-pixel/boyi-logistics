"""Service V2 automation plugins and instance-bound execution."""

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
from agent.automation_plugins.lifecycle import AutomationPluginService
from agent.automation_plugins.invocation import compile_instance_arguments
from agent.automation_plugins.release_config import (
    ProductionPluginReleaseConfig,
    load_production_plugin_release_config,
)

__all__ = [
    "AutomationPluginService",
    "AutomationProjectConfigurationService",
    "AccountManagerSessionResolver",
    "CompositeToolRegistry",
    "CoreBrokerInvocationContext",
    "LocalBrokerCapabilityIssuer",
    "LocalCoreAutomationBroker",
    "PluginCatalog",
    "PluginCatalogEntry",
    "PluginExecutionRouter",
    "ProductionPluginReleaseConfig",
    "RegisteredCoreAutomationBrokerAdapter",
    "compile_instance_arguments",
    "load_production_plugin_release_config",
    "project_contract_fragment",
]
