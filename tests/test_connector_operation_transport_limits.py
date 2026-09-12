"""Connector page contracts must survive the full Host service proxy."""
from dataclasses import replace

import pytest

from agent.automation_plugins.capability_proxy_v2 import _canonical_json
from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.errors import PluginExecutionError
from tests.test_automation_plugin_connector_runtime_v2 import _adapter, _invoke, _result_registry


@pytest.mark.parametrize("large_input", (False, True))
def test_connector_uses_declared_page_limit_in_both_directions(large_input):
    page = "A" * (1024 * 1024 + 32)
    schema = {"type": "object", "properties": {"value": {"type": "string", "maxLength": len(page)}},
              "required": ["value"], "additionalProperties": False}
    called = []

    def handler(_binding, arguments):
        called.append(arguments)
        return {"value": "accepted" if large_input else page}

    source = _result_registry(handler, output_schema=schema)
    descriptor = source.snapshot()[0]
    operation = descriptor.operations[0]
    if large_input:
        operation = replace(operation, input_schema=schema)
    registry = ConnectorRegistry((replace(descriptor, operations=(replace(operation,
        max_input_bytes=2*1024*1024, max_output_bytes=2*1024*1024),)),))
    adapter, _ = _adapter(connector_registry=registry)
    result = _invoke(adapter, arguments={"arguments": {"value": page}} if large_input else None)
    assert result == {"value": "accepted" if large_input else page}
    assert len(called) == 1

    # A Connector with the original small bound still rejects the same page.
    bounded = ConnectorRegistry((replace(descriptor, operations=(operation,)),))
    adapter, _ = _adapter(connector_registry=bounded)
    with pytest.raises(PluginExecutionError):
        _invoke(adapter, arguments={"arguments": {"value": page}} if large_input else None)


def test_managed_storage_document_limit_is_unchanged():
    with pytest.raises(PluginExecutionError, match="document is too large"):
        _canonical_json({"value": "A" * (1024 * 1024 + 1)})
