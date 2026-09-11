"""Python-only facts about the exact service resolved by the Host."""
from __future__ import annotations

from collections.abc import Mapping


class ServiceInvocationResult(dict):
    """Public business data with a non-serializing Host provenance side channel."""

    def __init__(self, data, *, service: str, operation: str, effect: str):
        super().__init__(data)
        self.service_target = {"service": service, "operation": operation, "effect": effect}


def customer_observed_result(observation, *, action: str):
    """Read only the exact customer primitive, never a similarly named service."""
    if observation.get("operation") == "service.invoke":
        if observation.get("service_target") != {
            "service": "connector.boyi.customer_sources@1", "operation": action, "effect": "read"
        } or observation.get("action") != action:
            return None
    elif observation.get("action") != "customer_problem." + action:
        return None
    result = observation.get("result")
    return result if isinstance(result, Mapping) else None
