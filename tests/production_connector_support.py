"""Isolated Host bridge for real package algorithms and production Connectors."""
import asyncio
import importlib.util
from dataclasses import replace
from pathlib import Path

from agent.automation_plugins.connector_registry import (
    ConnectorBindingKind, ConnectorBindingRef, ConnectorResourceBindingRef, ConnectorHostInternalBindingRef,
)


class ConnectorTestHost:
    def __init__(self, registry, context, plugin_id):
        self.registry = registry
        self.context = replace(context, connector_binding_resolver=self.resolve_binding)
        self.calls = []
        path = Path(__file__).resolve().parents[1] / "agent/service_v2_plugins" / plugin_id / "payload/plugin.py"
        spec = importlib.util.spec_from_file_location(f"isolated_{plugin_id}_adapter",path)
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)

    def resolve_binding(self, requirement):
        return self.binding(requirement.service)

    def binding(self, service):
        descriptor = self.registry.resolve(service)
        if descriptor.binding_kind is ConnectorBindingKind.ACCOUNT:
            role = descriptor.account_role
            return ConnectorBindingRef(service,role,self.context.account_bindings[role][0],"ronghui",self.context)
        if descriptor.binding_kind is ConnectorBindingKind.RESOURCE:
            role = descriptor.resource_role
            return ConnectorResourceBindingRef(service,role,self.context.resource_bindings[role],"feishu_sheet",self.context)
        return ConnectorHostInternalBindingRef(service,self.context)

    def invoke(self, suffix, operation, arguments):
        service = f"connector.boyi.{suffix}@1"
        self.calls.append((suffix,operation))
        return asyncio.run(self.registry.invoke(resolved=self.registry.require_operation(service,operation),
            binding=self.binding(service),arguments=arguments))

    def broker(self, operation, **kwargs):
        class Receipt(dict):
            pass
        def host(_operation, *, action, role, arguments):
            assert _operation == "service.invoke" and role == "__system__"
            service = arguments["service"]
            result = Receipt(self.invoke(service.removeprefix("connector.boyi.").removesuffix("@1"),action,arguments["arguments"]))
            result.host_evidence_ref = f"isolated-host-receipt-{len(self.calls)}"
            return result
        return self.plugin.service_invoke_adapter(host,operation,**kwargs)
