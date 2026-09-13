"""Real Broker dispatch must coordinate resolved effects, not service ceilings."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.automation_plugins.broker import LocalBrokerCapabilityIssuer, LocalCoreAutomationBroker
from agent.automation_plugins.capability_proxy_v2 import build_service_v2_capability_handler_map
from agent.automation_plugins.connector_registry import ConnectorBindingKind, ConnectorDescriptor, ConnectorOperation, ConnectorRegistry
from agent.automation_plugins.core_adapter import RegisteredCoreAutomationBrokerAdapter
from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.automation_plugins.host_capability_registry import CapabilityEffect, governance_for_effect
from agent.automation_plugins.service_v2_contract import SYSTEM_CAPABILITY_ROLE
from tests.test_automation_plugin_connector_runtime_v2 import _manifest, _Orchestration


@pytest.mark.parametrize("effect", [CapabilityEffect.READ, CapabilityEffect.INTERNAL_WRITE, CapabilityEffect.EXTERNAL_WRITE])
def test_broker_read_bypasses_writer_but_actual_writes_wait_before_receipt(tmp_path, effect):
    async def scenario():
        service = "connector.boyi.coordination@1"
        schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}
        entered = []
        receipts = []

        def handler(_binding, arguments):
            entered.append(arguments["value"])
            return dict(arguments)

        registry = ConnectorRegistry((ConnectorDescriptor(
            service=service, title="Coordination test", account_role=None, allowed_systems=(),
            binding_kind=ConnectorBindingKind.HOST_INTERNAL,
            operations=(ConnectorOperation(name="query", effect=effect, input_schema=schema, output_schema=schema, handler=handler),),
        ),))
        manifest = _manifest(requires=[{"service": service, "binding_kind": "host_internal"}])
        manifest["account_roles"] = []
        adapter = RegisteredCoreAutomationBrokerAdapter(
            handlers=build_service_v2_capability_handler_map(_Orchestration(manifest), connector_registry=registry),
            connector_registry=registry,
        )
        issuer = LocalBrokerCapabilityIssuer(tmp_path / "b.sock", write_attempt_recorder=receipts.append)
        direct = DirectPluginInvocationService(None, SimpleNamespace(_issuer=issuer), None, release_hold_provider=lambda: False)
        guard_attempted = asyncio.Event()

        @asynccontextmanager
        async def observed_guard(prepared):
            guard_attempted.set()
            async with direct.host_operation(prepared):
                yield

        issuer.host_operation_guard = observed_guard
        invocation_id = str(uuid4())
        direct._active[invocation_id] = {"cancel_requested": False}
        # Another actual write is in progress on this physical target.
        direct._held_operations["other-write"] = ("other-invocation", (("unscoped-host-write",),))
        governance = governance_for_effect(CapabilityEffect.EXTERNAL_WRITE)
        capability = issuer.issue(
            automation_id="connector-project", plugin_version="1.0.0", tool_name="service.connector_consumer", ttl_seconds=60,
            runtime_permissions={"_service_effect_ceiling": CapabilityEffect.DESTRUCTIVE.value, "max_broker_calls": 1,
                "broker_operations": [{"operation": "service.invoke", "action": "query", "roles": [SYSTEM_CAPABILITY_ROLE],
                    "effect": governance.effect.value, "broker_effect": governance.broker_effect,
                    "governance": governance.to_mapping(), "dynamic_effect": True}]},
            account_roles=(), resource_roles=(), account_bindings={}, resource_bindings={},
            write_attempt_context={"automation_id": "connector-project", "plugin_id": "connector_consumer",
                "generation": 1, "lease_id": str(uuid4()), "invocation_id": invocation_id},
        )
        broker = LocalCoreAutomationBroker(issuer=issuer, adapter=adapter)
        await broker.start()

        async def call():
            reader, writer = await asyncio.open_unix_connection(str(issuer.broker_socket_path))
            writer.write(json.dumps({"schema_version": 1, "request_id": str(uuid4()), "capability": capability,
                "operation": "service.invoke", "action": "query", "role": SYSTEM_CAPABILITY_ROLE,
                "arguments": {"service": service, "operation": "query", "arguments": {"value": "actual-result"}}}).encode() + b"\n")
            await writer.drain()
            result = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return result

        pending = asyncio.create_task(call())
        write = effect not in {CapabilityEffect.READ, CapabilityEffect.COMPUTE}
        try:
            if write:
                # Wait for the real guard to observe the conflicting writer.
                await asyncio.wait_for(guard_attempted.wait(), timeout=2)
                assert not pending.done()
                assert not entered and not receipts
                direct._held_operations.pop("other-write")
                direct._operation_changed.set()
            result = await asyncio.wait_for(pending, timeout=2)
            assert result["ok"] is True, result
            assert result["data"] == {"value": "actual-result"}
            assert entered == ["actual-result"]
            assert len(receipts) == int(write)
            assert issuer.started_mutating_call_count(capability) == int(write)
            assert guard_attempted.is_set() is write
            assert not direct._held_operations if write else "other-write" in direct._held_operations
        finally:
            direct._held_operations.clear()
            direct._operation_changed.set()
            await asyncio.gather(pending, return_exceptions=True)
            await broker.stop()

    asyncio.run(scenario())
