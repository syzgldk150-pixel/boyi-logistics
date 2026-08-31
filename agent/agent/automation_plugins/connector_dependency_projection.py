"""Safe dependency material shared by Connector-aware runtime coeffects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from agent.automation_plugins.connector_compatibility import (
    ConnectorRequirementContract,
    connector_requirement_for_service,
    connector_requirement_from_mapping,
    evaluate_connector_requirement,
)
from agent.automation_plugins.connector_registry import ConnectorRegistry
from agent.automation_plugins.service_registry import ServiceRegistry


def project_service_dependencies(
    required_services: Iterable[str],
    *,
    connector_requirements: Iterable[
        ConnectorRequirementContract | Mapping[str, Any]
    ] = (),
    connector_registry: ConnectorRegistry,
    service_registry: ServiceRegistry,
) -> tuple[tuple[str, bool, Mapping[str, Any]], ...]:
    """Project ready/missing service facts without exposing adapter internals."""

    connector_projections = {
        str(item["service"]): item for item in connector_registry.safe_projection()
    }
    exact_connector_requirements = tuple(
        item
        if isinstance(item, ConnectorRequirementContract)
        else connector_requirement_from_mapping(item)
        for item in connector_requirements
    )
    projected: list[tuple[str, bool, Mapping[str, Any]]] = []
    for service in required_services:
        connector = connector_projections.get(service)
        if service.startswith("connector."):
            compatibility = evaluate_connector_requirement(
                connector_registry,
                connector_requirement_for_service(
                    exact_connector_requirements,
                    service,
                ),
                service=service,
            )
            ready = compatibility.ready
            projected.append(
                (
                    service,
                    ready,
                    {
                        "service": service,
                        "dependency_status": (
                            "READY"
                            if ready
                            else compatibility.reason_code
                            or "CONNECTOR_REQUIREMENT_INCOMPATIBLE"
                        ),
                        "dependency_reason": compatibility.reason,
                        "connector": connector,
                        "connector_contract_sha256": (
                            connector_registry.contract_sha256(service)
                            if connector is not None
                            else None
                        ),
                        "provider": None,
                    },
                )
            )
            continue

        provider = service_registry.provider_for(service)
        claimed = provider or service_registry.claimed_provider_for(service)
        ready = provider is not None
        status = "READY" if ready else "PROVIDER_BLOCKED" if claimed else "MISSING_PROVIDER"
        projected.append(
            (
                service,
                ready,
                {
                    "service": service,
                    "dependency_status": status,
                    "provider": (
                        {
                            "plugin_id": claimed.plugin_id,
                            "plugin_version": claimed.plugin_version,
                            "package_sha256": claimed.package_sha256,
                            "manifest_sha256": claimed.manifest_sha256,
                            "generation": claimed.generation,
                            "runtime_mode": claimed.runtime_mode,
                        }
                        if claimed is not None
                        else None
                    ),
                },
            )
        )
    return tuple(projected)


__all__ = ["project_service_dependencies"]
