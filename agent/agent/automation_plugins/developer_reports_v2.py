"""Deterministic, offline developer reports for verified Service v2 packages.

The functions in this module consume only the closed result of package
verification.  They do not install a package, resolve project bindings, create
grants, or consult runtime state.  Effect and governance projections are
therefore limited to the immutable Provider declaration and the code-owned
Host capability registry.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from agent.automation_plugins.errors import PluginManifestError
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    HOST_CAPABILITY_API_VERSION,
    default_host_capability_registry,
    effect_rank,
    governance_for_effect,
)
from agent.automation_plugins.package_v2 import VerifiedPluginPackageV2
from agent.automation_plugins.service_v2_contract import (
    SERVICE_INVOKE_PER_CALL_LIMIT,
    SYSTEM_CAPABILITY_ROLE,
    ServiceV2ProjectContract,
)
from shared.waybill_entry_extensions import normalize_waybill_entry_slot


PERMISSION_REPORT_SCHEMA = "service-v2-project-permissions/1"
PACKAGE_DIFF_REPORT_SCHEMA = "service-v2-package-diff/1"
NOT_EVALUATED_OFFLINE = "NOT_EVALUATED_OFFLINE"

_CONTRIBUTION_KINDS = (
    "console",
    "scheduler",
    "webhook",
    "feishu",
    "events",
    "harness",
    "module_slots",
)
_MANIFEST_SECTIONS = (
    "schema_version",
    "runtime_model",
    "plugin_id",
    "name",
    "version",
    "description",
    "host_api",
    "runtime",
    "provides",
    "requires",
    "capabilities",
    "account_roles",
    "resource_roles",
    "contributes",
    "config_schema",
    "storage",
)
def _require_verified(value: object, label: str) -> VerifiedPluginPackageV2:
    if not isinstance(value, VerifiedPluginPackageV2):
        raise TypeError(f"{label} must be a VerifiedPluginPackageV2")
    return value


def _canonical_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _stable(value: Any) -> Any:
    """Return a detached JSON value with declarative arrays canonically sorted."""

    if isinstance(value, Mapping):
        return {str(key): _stable(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [_stable(item) for item in value]
        return sorted(items, key=_canonical_text)
    return copy.deepcopy(value)


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_text(value).encode("utf-8")).hexdigest()


def _provider_operation_rows(
    verified: VerifiedPluginPackageV2,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provided in verified.manifest.provides:
        service = str(provided["service"])
        operations = provided.get("operations")
        if not isinstance(operations, tuple):
            raise PluginManifestError("verified Provider operations are invalid")
        for operation in operations:
            if not isinstance(operation, Mapping):
                raise PluginManifestError("verified Provider operation is invalid")
            name = str(operation.get("name") or "")
            try:
                effect = CapabilityEffect(str(operation.get("effect") or ""))
            except ValueError as exc:  # pragma: no cover - package verifier invariant
                raise PluginManifestError("verified Provider effect is invalid") from exc
            rows.append(
                {
                    "service": service,
                    "operation": name,
                    "effect": effect.value,
                    "governance": _stable(governance_for_effect(effect).to_mapping()),
                }
            )
    return sorted(rows, key=lambda item: (item["service"], item["operation"]))


def _broker_operation_index(
    contract: ServiceV2ProjectContract,
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    raw_operations = contract.runtime_permissions.get("broker_operations")
    if not isinstance(raw_operations, list):
        raise PluginManifestError("verified runtime permission projection is invalid")
    result: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for raw in raw_operations:
        if not isinstance(raw, Mapping):
            raise PluginManifestError("verified runtime permission operation is invalid")
        roles = raw.get("roles")
        if not isinstance(roles, list) or len(roles) != 1:
            raise PluginManifestError("verified runtime permission role is invalid")
        key = (str(raw.get("operation") or ""), str(raw.get("action") or ""), str(roles[0]))
        if key in result:
            raise PluginManifestError("verified runtime permission operation is ambiguous")
        result[key] = raw
    return result


def _role_projection(capability: Mapping[str, Any]) -> dict[str, str]:
    if capability.get("account_role") is not None:
        return {"kind": "account", "name": str(capability["account_role"])}
    if capability.get("resource_role") is not None:
        return {"kind": "resource", "name": str(capability["resource_role"])}
    return {"kind": "system", "name": SYSTEM_CAPABILITY_ROLE}


def _service_invoke_row(
    *,
    action: str,
    role: Mapping[str, str],
    broker_operation: Mapping[str, Any],
) -> dict[str, Any]:
    governance = governance_for_effect(CapabilityEffect.EXTERNAL_WRITE).to_mapping()
    if (
        broker_operation.get("dynamic_effect") is not True
        or broker_operation.get("effect") != CapabilityEffect.EXTERNAL_WRITE.value
        or _stable(broker_operation.get("governance")) != _stable(governance)
    ):
        raise PluginManifestError("service.invoke admission governance is invalid")
    return {
        "capability": "service.invoke",
        "action": action,
        "role": _stable(role),
        "effect": CapabilityEffect.EXTERNAL_WRITE.value,
        "dynamic_effect": True,
        "effect_resolution": "PROVIDER_OPERATION_AT_RUNTIME",
        "actual_effect": "RUNTIME_RESOLVED",
        "admission_ceiling": CapabilityEffect.EXTERNAL_WRITE.value,
        "governance": _stable(governance),
        "scheduler_allowed": True,
        "per_call_limit": SERVICE_INVOKE_PER_CALL_LIMIT,
        "grant": False,
    }


def _host_capability_rows(
    verified: VerifiedPluginPackageV2,
    contract: ServiceV2ProjectContract,
) -> list[dict[str, Any]]:
    registry = default_host_capability_registry()
    broker_operations = _broker_operation_index(contract)
    rows: list[dict[str, Any]] = []
    for capability in verified.manifest.capabilities:
        if not isinstance(capability, Mapping):
            raise PluginManifestError("verified Host capability declaration is invalid")
        name = str(capability.get("name") or "")
        role = _role_projection(capability)
        for raw_action in capability.get("operations") or ():
            action = str(raw_action)
            broker_key = (name, action, role["name"])
            try:
                broker_operation = broker_operations.pop(broker_key)
            except KeyError as exc:
                raise PluginManifestError("Host capability is absent from runtime permissions") from exc
            if name == "service.invoke":
                rows.append(
                    _service_invoke_row(
                        action=action,
                        role=role,
                        broker_operation=broker_operation,
                    )
                )
                continue
            try:
                descriptor = registry.resolve(
                    api_version=HOST_CAPABILITY_API_VERSION,
                    capability=name,
                    action=action,
                )
            except Exception as exc:
                if getattr(exc, "code", None) == "CAPABILITY_UNAVAILABLE":
                    raise PluginManifestError("Host capability is unavailable") from exc
                raise
            governance = descriptor.governance.to_mapping()
            if (
                broker_operation.get("dynamic_effect") is not False
                or broker_operation.get("effect") != descriptor.governance.effect.value
                or _stable(broker_operation.get("governance")) != _stable(governance)
            ):
                raise PluginManifestError("Host capability governance projection drifted")
            rows.append(
                {
                    "capability": name,
                    "action": action,
                    "role": _stable(role),
                    "effect": descriptor.governance.effect.value,
                    "dynamic_effect": False,
                    "effect_resolution": "HOST_CAPABILITY_REGISTRY",
                    "actual_effect": descriptor.governance.effect.value,
                    "admission_ceiling": descriptor.governance.effect.value,
                    "governance": _stable(governance),
                    "scheduler_allowed": descriptor.scheduler_allowed,
                    "per_call_limit": descriptor.per_call_limit,
                    "grant": False,
                }
            )
    if broker_operations:
        raise PluginManifestError("runtime permissions contain undeclared Host capabilities")
    return sorted(
        rows,
        key=lambda item: (
            item["capability"],
            item["action"],
            item["role"]["kind"],
            item["role"]["name"],
        ),
    )


def _contribution_rows(
    verified: VerifiedPluginPackageV2,
    contract: ServiceV2ProjectContract,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind in _CONTRIBUTION_KINDS:
        raw_items = verified.manifest.contributes.get(kind, ())
        if not isinstance(raw_items, tuple):
            raise PluginManifestError("verified contribution projection is invalid")
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise PluginManifestError("verified contribution is invalid")
            contribution_id = str(raw.get("id") or "")
            invocation = contract.invocation_contracts.get(contribution_id)
            if not isinstance(invocation, Mapping):
                raise PluginManifestError("contribution invocation contract is unavailable")
            if kind == "harness" and raw.get("effect") != invocation.get("effect"):
                raise PluginManifestError(
                    "harness contribution effect does not match its Provider operation"
                )
            if kind == "module_slots":
                try:
                    normalize_waybill_entry_slot(raw.get("slot"))
                except ValueError as exc:
                    raise PluginManifestError(
                        "module-slot contribution slot is invalid"
                    ) from exc
                if invocation.get("effect") not in {
                    CapabilityEffect.READ.value,
                    CapabilityEffect.COMPUTE.value,
                }:
                    raise PluginManifestError(
                        "module-slot contribution Provider effect is not read-only"
                    )
            rows.append(
                {
                    "kind": kind,
                    "id": contribution_id,
                    "service": str(invocation["service"]),
                    "operation": str(invocation["operation"]),
                    "effect": str(invocation["effect"]),
                    "governance": _stable(invocation["governance"]),
                    "default_enabled": raw.get("default_enabled") is True,
                    "declaration": _stable(raw),
                }
            )
    return sorted(rows, key=lambda item: (item["kind"], item["id"]))


def project_permission_report(verified: VerifiedPluginPackageV2) -> dict[str, Any]:
    """Describe one verified package's declarative authority without granting it."""

    package = _require_verified(verified, "verified")
    contract = ServiceV2ProjectContract.from_manifest(package.manifest)
    provided_operations = _provider_operation_rows(package)
    host_capabilities = _host_capability_rows(package, contract)
    contributions = _contribution_rows(package, contract)
    return {
        "schema": PERMISSION_REPORT_SCHEMA,
        "authority": {
            "mode": "DECLARATION_ONLY",
            "grants_created": False,
            "project_bindings_evaluated": False,
        },
        "plugin": {
            "plugin_id": package.manifest.plugin_id,
            "name": package.manifest.name,
            "version": package.manifest.version,
            "schema_version": package.manifest.schema_version,
            "runtime_model": package.manifest.runtime_model,
            "package_sha256": package.package_sha256,
            "manifest_sha256": package.manifest_sha256,
        },
        "provided_operations": provided_operations,
        "host_capabilities": host_capabilities,
        "contributions": contributions,
        "account_roles": sorted(
            (_stable(item) for item in contract.account_roles),
            key=lambda item: str(item["role"]),
        ),
        "resource_roles": sorted(
            (_stable(item) for item in contract.resource_roles),
            key=lambda item: str(item["role"]),
        ),
        "runtime_summary": {
            "host_api": _stable(package.manifest.host_api),
            "runtime": _stable(package.manifest.runtime),
            "runtime_permissions": _stable(contract.runtime_permissions),
            "scheduling": _stable(contract.scheduling),
            "allowed_entrypoints": sorted(contract.allowed_entrypoints),
            "default_entrypoints": sorted(contract.default_entrypoints),
            "artifact": {
                "files_sha256": package.files_sha256,
                "runtime_sha256": package.runtime_sha256,
                "file_count": len(package.files),
                "package_size": len(package.package_bytes),
            },
        },
    }


def _record_delta(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    identity: Callable[[Mapping[str, Any]], tuple[str, ...]],
) -> dict[str, Any]:
    before_index = {identity(item): _stable(item) for item in before}
    after_index = {identity(item): _stable(item) for item in after}
    if len(before_index) != len(before) or len(after_index) != len(after):
        raise PluginManifestError("developer report identity is ambiguous")
    added_keys = sorted(after_index.keys() - before_index.keys())
    removed_keys = sorted(before_index.keys() - after_index.keys())
    common_keys = sorted(before_index.keys() & after_index.keys())
    changed_keys = [key for key in common_keys if before_index[key] != after_index[key]]
    return {
        "added": [after_index[key] for key in added_keys],
        "removed": [before_index[key] for key in removed_keys],
        "changed": [
            {
                "identity": list(key),
                "before": before_index[key],
                "after": after_index[key],
            }
            for key in changed_keys
        ],
    }


def _file_delta(
    before: VerifiedPluginPackageV2,
    after: VerifiedPluginPackageV2,
) -> dict[str, Any]:
    before_files = {item.path: item.to_mapping() for item in before.files}
    after_files = {item.path: item.to_mapping() for item in after.files}
    changed_paths = [
        path
        for path in sorted(before_files.keys() & after_files.keys())
        if before_files[path] != after_files[path]
    ]
    return {
        "added": [after_files[path] for path in sorted(after_files.keys() - before_files.keys())],
        "removed": [before_files[path] for path in sorted(before_files.keys() - after_files.keys())],
        "changed": [
            {
                "path": path,
                "before": before_files[path],
                "after": after_files[path],
            }
            for path in changed_paths
        ],
    }


def _manifest_delta(
    before: VerifiedPluginPackageV2,
    after: VerifiedPluginPackageV2,
) -> dict[str, Any]:
    before_manifest = before.manifest_mapping
    after_manifest = after.manifest_mapping
    sections: list[dict[str, Any]] = []
    changed_sections: list[str] = []
    for name in _MANIFEST_SECTIONS:
        before_value = _stable(before_manifest[name])
        after_value = _stable(after_manifest[name])
        changed = before_value != after_value
        if changed:
            changed_sections.append(name)
        sections.append(
            {
                "name": name,
                "changed": changed,
                "before_sha256": _json_sha256(before_value),
                "after_sha256": _json_sha256(after_value),
            }
        )
    return {"changed_sections": changed_sections, "sections": sections}


def _effect_surface(report: Mapping[str, Any]) -> dict[tuple[str, ...], str]:
    surface: dict[tuple[str, ...], str] = {}
    for row in report["provided_operations"]:
        key = ("provider", str(row["service"]), str(row["operation"]))
        surface[key] = str(row["effect"])
    for row in report["host_capabilities"]:
        role = row["role"]
        key = (
            "host",
            str(row["capability"]),
            str(row["action"]),
            str(role["kind"]),
            str(role["name"]),
        )
        surface[key] = str(row["admission_ceiling"])
    for row in report["contributions"]:
        key = ("contribution", str(row["kind"]), str(row["id"]))
        surface[key] = str(row["effect"])
    return surface


def _effect_changes(
    before_report: Mapping[str, Any],
    after_report: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    before = _effect_surface(before_report)
    after = _effect_surface(after_report)
    escalations: list[dict[str, Any]] = []
    expansions: list[dict[str, Any]] = []
    for identity in sorted(after.keys() - before.keys()):
        effect = after[identity]
        expansion = {
            "identity": list(identity),
            "before_effect": None,
            "after_effect": effect,
        }
        expansions.append(expansion)
        if effect_rank(effect) >= effect_rank(CapabilityEffect.INTERNAL_WRITE):
            escalations.append({**expansion, "reason": "NEW_MUTATING_EFFECT"})
    for identity in sorted(before.keys() & after.keys()):
        before_effect = before[identity]
        after_effect = after[identity]
        if effect_rank(after_effect) > effect_rank(before_effect):
            escalations.append(
                {
                    "identity": list(identity),
                    "before_effect": before_effect,
                    "after_effect": after_effect,
                    "reason": "EFFECT_RANK_INCREASED",
                }
            )
    return escalations, expansions


def _semver_tuple(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise PluginManifestError("verified plugin version is invalid")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _classification(
    before: VerifiedPluginPackageV2,
    after: VerifiedPluginPackageV2,
) -> str:
    if before.manifest.plugin_id != after.manifest.plugin_id:
        return "INVALID_IDENTITY"
    if before.package_sha256 == after.package_sha256:
        return "NO_CHANGE"
    if before.manifest.version == after.manifest.version:
        return "IMMUTABLE_VERSION_CONFLICT"
    if _semver_tuple(after.manifest.version) < _semver_tuple(before.manifest.version):
        return "DOWNGRADE"
    return "REVIEW_REQUIRED"


def diff_verified_packages(
    before: VerifiedPluginPackageV2,
    after: VerifiedPluginPackageV2,
) -> dict[str, Any]:
    """Compare two verified packages without claiming runtime compatibility."""

    before_package = _require_verified(before, "before")
    after_package = _require_verified(after, "after")
    before_report = project_permission_report(before_package)
    after_report = project_permission_report(after_package)
    classification = _classification(before_package, after_package)
    files = _file_delta(before_package, after_package)
    manifest = _manifest_delta(before_package, after_package)

    provided_delta = _record_delta(
        before_report["provided_operations"],
        after_report["provided_operations"],
        identity=lambda item: (str(item["service"]), str(item["operation"])),
    )
    host_delta = _record_delta(
        before_report["host_capabilities"],
        after_report["host_capabilities"],
        identity=lambda item: (
            str(item["capability"]),
            str(item["action"]),
            str(item["role"]["kind"]),
            str(item["role"]["name"]),
        ),
    )
    contribution_delta = _record_delta(
        before_report["contributions"],
        after_report["contributions"],
        identity=lambda item: (str(item["kind"]), str(item["id"])),
    )
    before_runtime_permissions = _stable(
        before_report["runtime_summary"]["runtime_permissions"]
    )
    after_runtime_permissions = _stable(
        after_report["runtime_summary"]["runtime_permissions"]
    )
    before_account_roles = _stable(before_report["account_roles"])
    after_account_roles = _stable(after_report["account_roles"])
    before_resource_roles = _stable(before_report["resource_roles"])
    after_resource_roles = _stable(after_report["resource_roles"])
    permissions_changed = any(
        (
            provided_delta["added"],
            provided_delta["removed"],
            provided_delta["changed"],
            host_delta["added"],
            host_delta["removed"],
            host_delta["changed"],
            before_runtime_permissions != after_runtime_permissions,
            before_account_roles != after_account_roles,
            before_resource_roles != after_resource_roles,
        )
    )
    effect_escalations, permission_expansions = _effect_changes(
        before_report,
        after_report,
    )
    payload_changed = any(
        item["path"].startswith("payload/")
        for group in ("added", "removed", "changed")
        for item in files[group]
    )
    payload_only = payload_changed and set(manifest["changed_sections"]) <= {"version"}
    before_config = _stable(before_package.manifest_mapping["config_schema"])
    after_config = _stable(after_package.manifest_mapping["config_schema"])
    before_storage = _stable(before_package.manifest_mapping["storage"])
    after_storage = _stable(after_package.manifest_mapping["storage"])

    return {
        "schema": PACKAGE_DIFF_REPORT_SCHEMA,
        "classification": classification,
        "review_required": classification == "REVIEW_REQUIRED",
        "plugin_identity": {
            "before": before_package.manifest.plugin_id,
            "after": after_package.manifest.plugin_id,
            "matches": before_package.manifest.plugin_id == after_package.manifest.plugin_id,
        },
        "versions": {
            "before": before_package.manifest.version,
            "after": after_package.manifest.version,
        },
        "package": {
            "before_sha256": before_package.package_sha256,
            "after_sha256": after_package.package_sha256,
            "same_bytes": before_package.package_sha256 == after_package.package_sha256,
            "payload_changed": payload_changed,
            "payload_only": payload_only,
        },
        "files": files,
        "manifest": manifest,
        "permissions": {
            "changed": permissions_changed,
            "provided_operations": provided_delta,
            "host_capabilities": host_delta,
            "account_roles": {
                "changed": before_account_roles != after_account_roles,
                "before": before_account_roles,
                "after": after_account_roles,
            },
            "resource_roles": {
                "changed": before_resource_roles != after_resource_roles,
                "before": before_resource_roles,
                "after": after_resource_roles,
            },
            "runtime_permissions_changed": (
                before_runtime_permissions != after_runtime_permissions
            ),
            "effect_escalation": bool(effect_escalations),
            "effect_escalations": effect_escalations,
            "permission_expansions": permission_expansions,
        },
        "contributions": {
            "changed": any(bool(items) for items in contribution_delta.values()),
            "added": contribution_delta["added"],
            "removed": contribution_delta["removed"],
            "modified": contribution_delta["changed"],
        },
        "configuration": {
            "changed": before_config != after_config,
            "before_sha256": _json_sha256(before_config),
            "after_sha256": _json_sha256(after_config),
            "project_configuration": NOT_EVALUATED_OFFLINE,
        },
        "storage": {
            "changed": before_storage != after_storage,
            "before_sha256": _json_sha256(before_storage),
            "after_sha256": _json_sha256(after_storage),
        },
        "project_configuration": NOT_EVALUATED_OFFLINE,
        "compatibility_claim": "NONE",
    }


__all__ = [
    "NOT_EVALUATED_OFFLINE",
    "PACKAGE_DIFF_REPORT_SCHEMA",
    "PERMISSION_REPORT_SCHEMA",
    "diff_verified_packages",
    "project_permission_report",
]
