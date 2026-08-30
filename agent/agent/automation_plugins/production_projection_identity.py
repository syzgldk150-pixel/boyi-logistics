"""Exact process-identity journal for production projection transitions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.service_registry import ServiceRegistry
from agent.automation_plugins.service_v2_projection import (
    ManagedContributionRegistry,
)


def project_projection_identity(
    *,
    services: ServiceRegistry,
    contributions: ManagedContributionRegistry,
    automation_id: str,
) -> str:
    """Hash the complete process-owned projection for one project."""

    service_references = []
    for registration in services.snapshot():
        service_references.extend(
            {
                "provider_automation_id": reference.provider_automation_id,
                "automation_id": reference.automation_id,
                "generation": reference.generation,
                "package_sha256": reference.package_sha256,
                "manifest_sha256": reference.manifest_sha256,
                "accepts_new_calls": reference.accepts_new_calls,
            }
            for reference in services.project_references(
                registration.automation_id,
            )
            if reference.automation_id == automation_id
        )
    service_references.sort(
        key=lambda item: (
            item["generation"],
            item["provider_automation_id"],
        )
    )
    contribution_records = [
        {
            "registration_id": record.registration_id,
            "automation_id": record.automation_id,
            "generation": record.generation,
            "plugin_id": record.plugin_id,
            "plugin_version": record.plugin_version,
            "package_sha256": record.package_sha256,
            "manifest_sha256": record.manifest_sha256,
            "contribution_id": record.contribution_id,
            "contribution_kind": record.contribution_kind,
            "service": record.service,
            "operation": record.operation,
            "declaration": dict(record.declaration),
            "route_keys": list(record.route_keys),
            "backend": record.backend,
            "backend_status": record.backend_status,
            "reason_code": record.reason_code,
            "reason_detail": record.reason_detail,
            "project_schedule": dict(record.project_schedule),
            "schedule_sha256": record.schedule_sha256,
            "phase": record.phase,
        }
        for record in contributions.snapshot()
        if record.automation_id == automation_id
    ]
    material = {
        "service_references": service_references,
        "contributions": contribution_records,
        "active_contribution_generation": contributions.active_generation(
            automation_id,
        ),
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


@dataclass(frozen=True)
class ProjectionIdentityToken:
    """One exact process-projection state at a monotonic project revision."""

    revision: int
    identity_sha256: str


@dataclass
class ProjectionIdentityJournal:
    """Compare process projection mutations without overwriting newer state."""

    _baselines: dict[tuple[str, int], ProjectionIdentityToken] = field(
        default_factory=dict,
    )
    _revisions: dict[str, int] = field(default_factory=dict)
    _successful_activations: dict[
        tuple[str, int],
        ProjectionIdentityToken,
    ] = field(default_factory=dict)

    def _current(
        self,
        automation_id: str,
        identity_sha256: str,
    ) -> ProjectionIdentityToken:
        return ProjectionIdentityToken(
            revision=self._revisions.get(automation_id, 0),
            identity_sha256=identity_sha256,
        )

    def begin(
        self,
        key: tuple[str, int],
        identity_sha256: str,
    ) -> None:
        self._baselines[key] = self._current(key[0], identity_sha256)

    def require_baseline(
        self,
        key: tuple[str, int],
        identity_sha256: str,
    ) -> None:
        self.require_exact(
            key[0],
            self._baselines.get(key),
            identity_sha256,
        )

    def expected_rollback(
        self,
        key: tuple[str, int],
        *,
        pending: bool,
    ) -> ProjectionIdentityToken | None:
        return (
            self._baselines.get(key)
            if pending
            else self._successful_activations.get(key)
        )

    def require_exact(
        self,
        automation_id: str,
        expected: ProjectionIdentityToken | None,
        identity_sha256: str,
    ) -> None:
        if expected is None or self._current(automation_id, identity_sha256) != expected:
            raise PluginConflictError(
                "runtime process projection changed after activation",
                code="RUNTIME_PROJECTION_ROLLBACK_FAILED",
            )

    def clear_baseline(self, key: tuple[str, int]) -> None:
        self._baselines.pop(key, None)

    def record(
        self,
        *,
        automation_id: str,
        generation: int | None,
        identity_sha256: str,
    ) -> None:
        revision = self._revisions.get(automation_id, 0) + 1
        self._revisions[automation_id] = revision
        for key in tuple(self._successful_activations):
            if key[0] == automation_id:
                self._successful_activations.pop(key, None)
        if generation is not None:
            self._successful_activations[
                (automation_id, generation)
            ] = ProjectionIdentityToken(revision, identity_sha256)

    def fail_closed(self, automation_id: str) -> None:
        for key in tuple(self._baselines):
            if key[0] == automation_id:
                self._baselines.pop(key, None)
        for key in tuple(self._successful_activations):
            if key[0] == automation_id:
                self._successful_activations.pop(key, None)
        self._revisions[automation_id] = self._revisions.get(automation_id, 0) + 1
