"""Exact Business Account, managed-resource and named-Worker binding checks."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Sequence
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginConflictError
from agent.automation_plugins.models import DeviceBinding


_BINDING_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProductionProjectBindingResolver:
    """Resolve only explicitly supplied identifiers from core-owned pools.

    Account lookup never uses an account's ``is_default`` flag or list order.
    Resource lookup requires an explicit ``resource_kind`` in the managed
    resource itself.  Worker lookup accepts an offline or locked named device
    (jobs may queue), but never a disabled device or a different platform.
    """

    def __init__(
        self,
        *,
        account_manager: Any,
        resource_provider: Callable[[str], Mapping[str, Any] | None],
        worker_repository: Any,
    ) -> None:
        if not callable(getattr(account_manager, "list_accounts", None)):
            raise TypeError("account_manager must expose list_accounts()")
        if not callable(resource_provider):
            raise TypeError("resource_provider must be callable")
        if not callable(getattr(worker_repository, "get_worker_device", None)):
            raise TypeError("worker_repository must expose get_worker_device()")
        self._account_manager = account_manager
        self._resource_provider = resource_provider
        self._workers = worker_repository

    @staticmethod
    def _identifier(value: object, field: str) -> str:
        identifier = str(value or "").strip()
        if not _BINDING_ID_RE.fullmatch(identifier):
            raise PluginConflictError(
                f"{field} is invalid",
                code="PLUGIN_BINDING_INVALID",
            )
        return identifier

    @staticmethod
    def _allowed_values(
        role: Mapping[str, object],
        field: str,
    ) -> frozenset[str]:
        values = role.get(field)
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) or not item.strip() for item in values)
        ):
            raise PluginConflictError(
                "signed binding role is invalid",
                code="PLUGIN_BINDING_CONTRACT_INVALID",
            )
        return frozenset(item.strip() for item in values)

    def describe_account_binding(
        self,
        *,
        automation_id: str,
        role: Mapping[str, object],
        account_id: str,
    ) -> dict[str, Any]:
        del automation_id
        exact_id = self._identifier(account_id, "account_id")
        allowed_systems = self._allowed_values(role, "allowed_systems")
        matches = [
            item
            for item in self._account_manager.list_accounts(
                include_status=False,
                validate=False,
            )
            if isinstance(item, Mapping)
            and str(item.get("account_id") or "").strip() == exact_id
        ]
        if len(matches) != 1:
            raise PluginConflictError(
                "the explicitly bound Business Account does not exist uniquely",
                code="PLUGIN_ACCOUNT_BINDING_NOT_FOUND",
            )
        account = matches[0]
        system = str(account.get("system") or "").strip()
        if account.get("is_active") is not True:
            raise PluginConflictError(
                "the explicitly bound Business Account is disabled",
                code="PLUGIN_ACCOUNT_BINDING_DISABLED",
            )
        if system not in allowed_systems:
            raise PluginConflictError(
                "the Business Account system is not allowed by the signed role",
                code="PLUGIN_ACCOUNT_BINDING_SYSTEM_MISMATCH",
            )
        return {
            "account_id": exact_id,
            "system": system,
            "active": True,
            "updated_at": str(account.get("updated_at") or ""),
        }

    def validate_account_binding(
        self,
        *,
        automation_id: str,
        role: Mapping[str, object],
        account_id: str,
    ) -> None:
        self.describe_account_binding(
            automation_id=automation_id,
            role=role,
            account_id=account_id,
        )

    def describe_resource_binding(
        self,
        *,
        automation_id: str,
        role: Mapping[str, object],
        resource_id: str,
    ) -> dict[str, Any]:
        del automation_id
        exact_id = self._identifier(resource_id, "resource_id")
        allowed_kinds = self._allowed_values(role, "allowed_kinds")
        try:
            record = self._resource_provider(exact_id)
        except Exception as exc:
            raise PluginConflictError(
                "the managed resource pool is unavailable",
                code="PLUGIN_RESOURCE_POOL_UNAVAILABLE",
            ) from exc
        if not isinstance(record, Mapping):
            raise PluginConflictError(
                "the explicitly bound managed resource does not exist",
                code="PLUGIN_RESOURCE_BINDING_NOT_FOUND",
            )
        resource_kind = str(record.get("resource_kind") or "").strip()
        if not resource_kind:
            raise PluginConflictError(
                "the managed resource has no explicit resource_kind",
                code="PLUGIN_RESOURCE_KIND_REQUIRED",
            )
        if resource_kind not in allowed_kinds:
            raise PluginConflictError(
                "the managed resource kind is not allowed by the signed role",
                code="PLUGIN_RESOURCE_KIND_MISMATCH",
            )
        metadata = record.get("_meta")
        safe_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        configuration_version = safe_metadata.get("configuration_version")
        config_sha256 = str(safe_metadata.get("config_sha256") or "").lower()
        if (
            type(configuration_version) is not int
            or configuration_version <= 0
            or not _SHA256_RE.fullmatch(config_sha256)
        ):
            raise PluginConflictError(
                "the managed resource revision is incomplete",
                code="PLUGIN_RESOURCE_REVISION_INVALID",
            )
        return {
            "resource_id": exact_id,
            "resource_kind": resource_kind,
            "source": str(safe_metadata.get("source") or ""),
            "configuration_version": configuration_version,
            "config_sha256": config_sha256,
            "updated_at": str(safe_metadata.get("updated_at") or ""),
        }

    def validate_resource_binding(
        self,
        *,
        automation_id: str,
        role: Mapping[str, object],
        resource_id: str,
    ) -> None:
        self.describe_resource_binding(
            automation_id=automation_id,
            role=role,
            resource_id=resource_id,
        )

    def require_active(
        self,
        *,
        resource_id: str,
        allowed_kinds: Sequence[str],
    ) -> dict[str, str]:
        """Resolve one exact, complete managed-resource revision for the broker.

        Workflow resources have no independent status field.  In this model
        ``active`` means that the exact identifier still exists and its kind,
        source and immutable revision metadata are complete.  The caller has
        already selected the signed instance resource role; this method never
        searches by name, kind, default flag or list position.
        """

        kinds = [str(item or "").strip() for item in allowed_kinds]
        if not kinds or any(not item for item in kinds):
            raise PluginConflictError(
                "signed resource kinds are invalid",
                code="PLUGIN_BINDING_CONTRACT_INVALID",
            )
        descriptor = self.describe_resource_binding(
            automation_id="runtime-broker",
            role={"allowed_kinds": kinds},
            resource_id=resource_id,
        )
        source = str(descriptor.get("source") or "").strip()
        if not source:
            raise PluginConflictError(
                "the managed resource revision has no authoritative source",
                code="PLUGIN_RESOURCE_REVISION_INVALID",
            )
        return {
            "resource_id": str(descriptor["resource_id"]),
            "resource_kind": str(descriptor["resource_kind"]),
            "source": source,
            "configuration_version": str(descriptor["configuration_version"]),
            "config_sha256": str(descriptor["config_sha256"]),
            "updated_at": str(descriptor.get("updated_at") or ""),
        }

    def describe_device_binding(
        self,
        *,
        automation_id: str,
        device_id: str,
        worker_requirement: Mapping[str, object],
    ) -> dict[str, Any]:
        del automation_id
        exact_id = self._identifier(device_id, "device_id")
        row = self._workers.get_worker_device(exact_id)
        if not isinstance(row, Mapping):
            raise PluginConflictError(
                "the explicitly named Worker device is not paired",
                code="PLUGIN_WORKER_BINDING_NOT_FOUND",
            )
        supported_os = worker_requirement.get("supported_os")
        if not isinstance(supported_os, list) or not supported_os:
            raise PluginConflictError(
                "signed Worker requirements are invalid",
                code="PLUGIN_WORKER_CONTRACT_INVALID",
            )
        platform = str(row.get("platform") or "").strip().lower()
        service_state = str(row.get("service_state") or "").strip().upper()
        if platform not in {str(item).strip().lower() for item in supported_os}:
            raise PluginConflictError(
                "the named Worker platform does not match the signed requirement",
                code="PLUGIN_WORKER_PLATFORM_MISMATCH",
            )
        if service_state == "DISABLED":
            raise PluginConflictError(
                "the named Worker device is disabled",
                code="PLUGIN_WORKER_BINDING_DISABLED",
            )
        capabilities = row.get("capabilities_json")
        if not isinstance(capabilities, Mapping):
            raise PluginConflictError(
                "the named Worker capability record is invalid",
                code="PLUGIN_WORKER_CAPABILITY_INVALID",
            )
        if (
            worker_requirement.get("interactive_session") is True
            and capabilities.get("interactive") is not True
        ):
            raise PluginConflictError(
                "the named Worker does not support interactive Tray execution",
                code="PLUGIN_WORKER_CAPABILITY_MISMATCH",
            )
        fingerprint = str(row.get("paired_public_key_fingerprint") or "").lower()
        capabilities_sha256 = str(row.get("capabilities_sha256") or "").lower()
        record_version = row.get("record_version")
        if (
            not _SHA256_RE.fullmatch(fingerprint)
            or not _SHA256_RE.fullmatch(capabilities_sha256)
            or isinstance(record_version, bool)
            or not isinstance(record_version, int)
            or record_version < 1
        ):
            raise PluginConflictError(
                "the named Worker immutable identity is incomplete",
                code="PLUGIN_WORKER_IDENTITY_INVALID",
            )
        return {
            "device_id": exact_id,
            "device_name": str(row.get("display_name") or exact_id)[:255],
            "platform": platform,
            "service_state": service_state,
            "interactive_session_state": str(
                row.get("interactive_session_state") or ""
            ).upper(),
            "paired_public_key_fingerprint": fingerprint,
            "capabilities_sha256": capabilities_sha256,
            "record_version": record_version,
        }

    def resolve_device_binding(
        self,
        *,
        automation_id: str,
        device_id: str,
        worker_requirement: Mapping[str, object],
    ) -> DeviceBinding:
        descriptor = self.describe_device_binding(
            automation_id=automation_id,
            device_id=device_id,
            worker_requirement=worker_requirement,
        )
        return DeviceBinding(
            device_id=str(descriptor["device_id"]),
            device_name=str(descriptor["device_name"]),
        )

    def detached_worker_row(self, device_id: str) -> dict[str, Any] | None:
        """Testing/support hook that never exposes the row through HTTP."""

        row = self._workers.get_worker_device(device_id)
        return copy.deepcopy(dict(row)) if isinstance(row, Mapping) else None


__all__ = ["ProductionProjectBindingResolver"]
