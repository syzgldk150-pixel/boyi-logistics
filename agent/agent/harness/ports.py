"""Closed read-only dependency ports for the Harness composition root."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class KnowledgeGateway(Protocol):
    def search(self, *, query: str, limit: int) -> Mapping[str, Any]: ...


@runtime_checkable
class WaybillGateway(Protocol):
    def lookup(self, *, waybill_number: str) -> Mapping[str, Any]: ...


@runtime_checkable
class TrackingGateway(Protocol):
    def lookup(self, *, tracking_number: str) -> Mapping[str, Any]: ...


@runtime_checkable
class WorkItemsGateway(Protocol):
    def list_open(self, *, limit: int) -> Mapping[str, Any]: ...


@runtime_checkable
class RunsGateway(Protocol):
    def get_summary(self, *, run_id: str) -> Mapping[str, Any]: ...


@runtime_checkable
class ArtifactGateway(Protocol):
    def inspect(self, *, artifact_id: str) -> Mapping[str, Any]: ...


@runtime_checkable
class TrustedHarnessInvocationPort(Protocol):
    """Root-owned bridge. It receives only a resolved opaque handle and JSON."""

    def invoke(self, *, handle: object, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class ManagedContributionSnapshotProvider(Protocol):
    """The narrow registry view required for dynamic Harness tools."""

    def active_snapshot(self) -> tuple[Mapping[str, Any], ...]: ...

    def resolve_active(
        self,
        automation_id: str,
        generation: int,
        contribution_kind: str,
        contribution_id: str,
    ) -> object: ...


__all__ = [
    "ArtifactGateway",
    "KnowledgeGateway",
    "ManagedContributionSnapshotProvider",
    "RunsGateway",
    "TrackingGateway",
    "TrustedHarnessInvocationPort",
    "WaybillGateway",
    "WorkItemsGateway",
]
