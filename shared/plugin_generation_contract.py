"""Closed metadata envelopes shared by persistence and runtime projections."""
from collections.abc import Mapping

EXECUTION_METADATA_FIELDS = frozenset({
    "project_config_version", "project_config", "account_bindings", "resource_bindings",
    "device_binding", "schedule", "compiled_invocations", "runtime_descriptor",
    "action_contract", "governance_anchor",
    "runtime_model", "plugin_api", "service_contracts", "contributions", "storage_contract",
})


def validate_execution_envelope(value, *, runtime_model, plugin_api):
    if runtime_model != "SERVICE_V2":
        raise ValueError("generation runtime model is unsupported")
    if not isinstance(value, Mapping) or set(value) != EXECUTION_METADATA_FIELDS:
        raise ValueError("generation execution metadata fields are not closed")
    if value["runtime_model"] != runtime_model or value["plugin_api"] != plugin_api:
        raise ValueError("generation runtime model or API differs from its envelope")
    if any(not isinstance(value[key], Mapping) for key in ("service_contracts", "contributions", "storage_contract")):
        raise ValueError("generation service contracts must be objects")
    services = value["service_contracts"]
    if set(services) != {"provides", "requires"} or any(not isinstance(services[key], (list, tuple)) for key in services):
        raise ValueError("generation provided and required contracts are invalid")
