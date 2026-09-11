"""Translate package-owned primitive names into declared Host services."""
from collections.abc import Mapping


def invoke_primitive(broker, primitives, operation, *, action, role, arguments,
                     preflight_services=()):
    declaration = primitives.get((operation, action, role))
    if declaration is None or not isinstance(arguments, Mapping):
        raise ValueError("PLUGIN_PRIMITIVE_UNDECLARED")
    service, name, _effect = declaration
    request = {"service": service, "operation": name, "arguments": dict(arguments)}
    if preflight_services:
        declared = {item[0] for item in primitives.values()}
        if any(item not in declared for item in preflight_services):
            raise ValueError("PLUGIN_PRIMITIVE_UNDECLARED")
        request["preflight_services"] = list(preflight_services)
    receipt = broker("service.invoke", action=name, role="__system__", arguments=request)
    if not isinstance(receipt, Mapping):
        raise ValueError("HOST_RESULT_INVALID")
    reference = getattr(receipt, "host_evidence_ref", None)
    if not isinstance(reference, str) or not reference or len(reference) > 512:
        raise ValueError("HOST_EVIDENCE_MISSING")
    return {**receipt, "evidence_ref": reference}
