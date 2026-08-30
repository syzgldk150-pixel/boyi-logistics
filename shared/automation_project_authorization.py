"""Pure contracts for project-scoped automation authorization.

The persisted policy is authority only when it still matches a freshly
compiled project/tool/configuration contract.  Snapshots intentionally contain
hashes and stable identifiers, never raw arguments or plugin package bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from shared.automation_project_manifest import (
    AutomationProjectInstanceDefinition,
    TRUSTED_AUTOMATION_ENTRYPOINTS,
)
import hashlib
import json
import re


_CORE_GOVERNANCE_FIELDS = (
    "name",
    "version",
    "operation_type",
    "risk_level",
    "approval",
    "permissions",
    "idempotency",
    "retry",
    "evidence",
    "postconditions",
    "project_full_auto_allowed",
)


class AutomationProjectPolicyMode(str, Enum):
    PROJECT_FULL_AUTO = "PROJECT_FULL_AUTO"
    REQUIRE_EACH_RUN = "REQUIRE_EACH_RUN"
    LEGACY_SCHEDULE_ONLY = "LEGACY_SCHEDULE_ONLY"


class AutomationEntrypoint(str, Enum):
    SCHEDULER = "scheduler"
    CONSOLE = "console"
    FEISHU = "feishu"
    WEBHOOK = "webhook"


class AutomationProjectContractError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code)


class _OmitDynamicArgument:
    __slots__ = ()


# A code-owned resolver may omit an optional occurrence field.  The singleton
# never enters JSON, plan state, or a transport envelope; both command creation
# and policy recheck interpret the exact same identity.
OMIT_DYNAMIC_ARGUMENT = _OmitDynamicArgument()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, datetime):
            if item.tzinfo is None:
                raise TypeError("naive datetime is not canonical")
            return item.isoformat()
        if isinstance(item, Mapping):
            return {
                str(key): normalize(nested)
                for key, nested in sorted(item.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(item, (list, tuple)):
            return [normalize(nested) for nested in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise TypeError(f"unsupported canonical value: {type(item).__name__}")

    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True)
class AutomationProjectInvocation:
    automation_id: str
    automation_generation: int
    entrypoint: AutomationEntrypoint
    contract_id: str
    contract_hash: str
    policy_version: int
    project_configuration_version: int
    request_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise AutomationProjectContractError("UNSUPPORTED_PROJECT_INVOCATION_SCHEMA")
        if not str(self.automation_id or "").strip():
            raise AutomationProjectContractError("AUTOMATION_ID_REQUIRED")
        if type(self.automation_generation) is not int or self.automation_generation <= 0:
            raise AutomationProjectContractError("INVALID_AUTOMATION_GENERATION")
        if not str(self.contract_id or "").strip():
            raise AutomationProjectContractError("PROJECT_INVOCATION_CONTRACT_REQUIRED")
        if len(str(self.contract_hash or "")) != 64:
            raise AutomationProjectContractError("PROJECT_CONTRACT_HASH_REQUIRED")
        if type(self.policy_version) is not int or self.policy_version <= 0:
            raise AutomationProjectContractError("INVALID_PROJECT_POLICY_VERSION")
        if type(self.project_configuration_version) is not int or self.project_configuration_version <= 0:
            raise AutomationProjectContractError("INVALID_PROJECT_CONFIGURATION_VERSION")
        if not str(self.request_id or "").strip():
            raise AutomationProjectContractError("PROJECT_INVOCATION_REQUEST_ID_REQUIRED")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "automation_id": self.automation_id,
            "automation_generation": self.automation_generation,
            "entrypoint": self.entrypoint.value,
            "contract_id": self.contract_id,
            "contract_hash": self.contract_hash,
            "policy_version": self.policy_version,
            "project_configuration_version": self.project_configuration_version,
            "request_id": self.request_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AutomationProjectInvocation":
        expected_fields = {
            "schema_version",
            "automation_id",
            "automation_generation",
            "entrypoint",
            "contract_id",
            "contract_hash",
            "policy_version",
            "project_configuration_version",
            "request_id",
        }
        if not isinstance(value, Mapping) or set(value) != expected_fields:
            raise AutomationProjectContractError("INVALID_PROJECT_INVOCATION")
        for field_name in (
            "schema_version",
            "automation_generation",
            "policy_version",
            "project_configuration_version",
        ):
            if type(value.get(field_name)) is not int:
                raise AutomationProjectContractError("INVALID_PROJECT_INVOCATION")
        try:
            return cls(
                schema_version=value["schema_version"],
                automation_id=str(value.get("automation_id") or ""),
                automation_generation=value["automation_generation"],
                entrypoint=AutomationEntrypoint(str(value.get("entrypoint") or "")),
                contract_id=str(value.get("contract_id") or ""),
                contract_hash=str(value.get("contract_hash") or ""),
                policy_version=value["policy_version"],
                project_configuration_version=value["project_configuration_version"],
                request_id=str(value.get("request_id") or ""),
            )
        except (TypeError, ValueError) as exc:
            raise AutomationProjectContractError("INVALID_PROJECT_INVOCATION") from exc


@dataclass(frozen=True)
class InvocationArgumentContract:
    contract_id: str
    entrypoint: str
    expected_arguments: Mapping[str, Any]
    dynamic_argument_resolvers: Mapping[str, str]
    input_schema: Mapping[str, Any] | None = None
    contribution_id: str | None = None


@dataclass(frozen=True)
class CompiledAutomationProjectContract:
    automation_id: str
    automation_generation: int
    manifest_sha256: str
    tool_name: str
    tool_version: str
    operation_type: str
    risk_level: str
    invocation_contracts: Mapping[str, InvocationArgumentContract]
    account_bindings: Mapping[str, str | tuple[str, ...]]
    allowed_entrypoints: frozenset[str]
    contract_hash: str
    tool_contract_hash: str
    plugin_contract_hash: str | None
    project_configuration_version: int
    snapshot: Mapping[str, Any]
    can_full_auto: bool
    code_owned_plan_fields: frozenset[str] = frozenset()
    restriction_code: str | None = None

    def matches_plan(
        self,
        plan: Any,
        invocation: AutomationProjectInvocation,
        *,
        source: str,
        execution_context: Mapping[str, Any] | None = None,
        dynamic_resolver: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
    ) -> bool:
        if invocation.automation_id != self.automation_id:
            return False
        if invocation.automation_generation != self.automation_generation:
            return False
        if invocation.contract_hash != self.contract_hash:
            return False
        if invocation.project_configuration_version != self.project_configuration_version:
            return False
        if (
            getattr(plan, "automation_id", None) != self.automation_id
            or getattr(plan, "automation_generation", None)
            != self.automation_generation
            or getattr(plan, "automation_contract_hash", None) != self.contract_hash
        ):
            return False
        if invocation.entrypoint.value != str(source or "").strip():
            return False
        if invocation.entrypoint.value not in self.allowed_entrypoints:
            return False
        invocation_contract = self.invocation_contracts.get(invocation.contract_id)
        if (
            invocation_contract is None
            or invocation_contract.entrypoint != invocation.entrypoint.value
        ):
            return False
        steps = tuple(getattr(plan, "steps", ()) or ())
        if len(steps) != 1:
            return False
        step = steps[0]
        if (
            str(getattr(step, "tool_name", "")) != self.tool_name
            or str(getattr(step, "tool_version", "")) != self.tool_version
            or str(getattr(getattr(step, "operation_type", ""), "value", getattr(step, "operation_type", "")))
            != self.operation_type
        ):
            return False
        return _arguments_match(
            invocation_contract.expected_arguments,
            getattr(step, "arguments", {}),
            invocation_contract.dynamic_argument_resolvers,
            execution_context or {},
            code_owned_plan_fields=self.code_owned_plan_fields,
            input_schema=invocation_contract.input_schema,
            dynamic_resolver=dynamic_resolver,
        )


PluginContractProvider = Callable[[str], Mapping[str, Any] | None]


def compile_automation_project_contract(
    definition: AutomationProjectInstanceDefinition,
    *,
    catalog: Any,
    scheduled_rows: Sequence[Mapping[str, Any]] = (),
    plugin_contract_provider: PluginContractProvider | None = None,
) -> CompiledAutomationProjectContract:
    automation_id = str(definition.automation_id or "").strip()
    if not automation_id:
        raise AutomationProjectContractError("AUTOMATION_ID_REQUIRED")
    if plugin_contract_provider is None:
        raise AutomationProjectContractError("PLUGIN_NOT_INSTALLED")
    provided = plugin_contract_provider(automation_id)
    if provided is None:
        raise AutomationProjectContractError("PLUGIN_NOT_INSTALLED")
    if not isinstance(provided, Mapping):
        raise AutomationProjectContractError("PLUGIN_CONTRACT_FRAGMENT_INVALID")
    _assert_privacy_safe_fragment(provided)
    plugin_fragment = dict(provided)
    runtime_model = str(plugin_fragment.get("runtime_model") or "ACTION_V1")
    if runtime_model == "SERVICE_V2":
        capability = plugin_fragment.get("tool_contract")
    elif runtime_model == "ACTION_V1":
        capability = catalog.get_capability(definition.tool_name)
    else:
        raise AutomationProjectContractError("PLUGIN_RUNTIME_MODEL_INVALID")
    if not isinstance(capability, Mapping):
        raise AutomationProjectContractError("PROJECT_TOOL_UNAVAILABLE")
    tool_version = str(capability.get("version") or "").strip()
    operation_type = str(capability.get("operation_type") or "").strip()
    risk_level = str(capability.get("risk_level") or "").strip()
    if not tool_version or not operation_type or not risk_level:
        raise AutomationProjectContractError("PROJECT_TOOL_CONTRACT_INVALID")
    tool_contract_payload = {
        key: capability.get(key) for key in _CORE_GOVERNANCE_FIELDS
    }
    tool_contract_hash = canonical_sha256(tool_contract_payload)
    _validate_plugin_fragment(
        plugin_fragment,
        automation_id=automation_id,
        definition=definition,
        core_tool_contract=tool_contract_payload,
    )
    code_owned_plan_fields = frozenset(
        str(item) for item in plugin_fragment.get("code_owned_plan_fields", [])
    )
    if plugin_fragment.get("enabled") is not True:
        raise AutomationProjectContractError("PLUGIN_DISABLED")
    plugin_contract_hash = canonical_sha256(plugin_fragment)
    automation_generation = plugin_fragment.get("committed_generation")
    if type(automation_generation) is not int or automation_generation <= 0:
        raise AutomationProjectContractError("PLUGIN_RUNTIME_NOT_COMMITTED")

    plugin_entrypoints = plugin_fragment.get("allowed_entrypoints")
    if not isinstance(plugin_entrypoints, list):
        raise AutomationProjectContractError("PLUGIN_ENTRYPOINTS_INVALID")
    effective_entrypoints = frozenset(str(item) for item in plugin_entrypoints)
    entrypoint_kinds = plugin_fragment.get("entrypoint_kinds")
    if runtime_model == "ACTION_V1" and entrypoint_kinds is None:
        entrypoint_kinds = {
            entrypoint: entrypoint for entrypoint in effective_entrypoints
        }
    if not isinstance(entrypoint_kinds, Mapping) or set(entrypoint_kinds) != set(
        effective_entrypoints
    ):
        raise AutomationProjectContractError("PLUGIN_ENTRYPOINTS_INVALID")
    normalized_entrypoint_kinds = {
        str(entrypoint): str(kind)
        for entrypoint, kind in entrypoint_kinds.items()
    }
    if (
        not effective_entrypoints
        or set(normalized_entrypoint_kinds.values())
        - (TRUSTED_AUTOMATION_ENTRYPOINTS | {"events"})
    ):
        raise AutomationProjectContractError("PLUGIN_ENTRYPOINTS_INVALID")
    if runtime_model == "ACTION_V1" and any(
        entrypoint != kind
        for entrypoint, kind in normalized_entrypoint_kinds.items()
    ):
        raise AutomationProjectContractError("PLUGIN_ENTRYPOINTS_INVALID")
    configured_entrypoints = plugin_fragment.get("enabled_entrypoints")
    if configured_entrypoints is not None:
        if not isinstance(configured_entrypoints, list):
            raise AutomationProjectContractError("PROJECT_ENTRYPOINTS_INVALID")
        configured = frozenset(str(item) for item in configured_entrypoints)
        if configured - effective_entrypoints:
            raise AutomationProjectContractError("PROJECT_ENTRYPOINTS_INVALID")
        effective_entrypoints = configured

    if frozenset(definition.allowed_entrypoints) != effective_entrypoints:
        raise AutomationProjectContractError("PROJECT_ENTRYPOINTS_STALE")
    if set(definition.argument_templates) - effective_entrypoints:
        raise AutomationProjectContractError("PROJECT_ENTRYPOINT_TEMPLATE_INVALID")
    if set(definition.dynamic_argument_resolvers) - effective_entrypoints:
        raise AutomationProjectContractError("PROJECT_DYNAMIC_RESOLVER_INVALID")

    normalized_accounts = _normalize_identifier_bindings(
        definition.account_bindings,
        error_code="PROJECT_ACCOUNT_BINDINGS_INVALID",
    )
    normalized_resources = _normalize_identifier_bindings(
        definition.resource_bindings,
        error_code="PROJECT_RESOURCE_BINDINGS_INVALID",
    )
    if canonical_sha256(definition.project_config) != plugin_fragment[
        "project_config_sha256"
    ]:
        raise AutomationProjectContractError("PROJECT_CONFIG_STALE")
    if canonical_sha256(normalized_accounts) != plugin_fragment[
        "account_bindings_sha256"
    ]:
        raise AutomationProjectContractError("PROJECT_ACCOUNT_BINDINGS_STALE")
    if canonical_sha256(normalized_resources) != plugin_fragment[
        "resource_bindings_sha256"
    ]:
        raise AutomationProjectContractError("PROJECT_RESOURCE_BINDINGS_STALE")

    materialized_by_entrypoint = _materialize_signed_invocation_arguments(
        plugin_fragment,
        definition=definition,
        account_bindings=normalized_accounts,
    )
    for entrypoint in effective_entrypoints:
        materialized = materialized_by_entrypoint.get(entrypoint)
        declared = definition.argument_templates.get(entrypoint)
        if not isinstance(materialized, Mapping) or not isinstance(declared, Mapping):
            raise AutomationProjectContractError("PROJECT_ENTRYPOINT_TEMPLATE_MISSING")
        if not _strict_json_equal(dict(materialized), dict(declared)):
            raise AutomationProjectContractError("PROJECT_ENTRYPOINT_TEMPLATE_STALE")
        signed_resolvers = plugin_fragment["invocation_contracts"][entrypoint][
            "dynamic_resolvers"
        ]
        declared_resolvers = definition.dynamic_argument_resolvers.get(entrypoint, {})
        if not _strict_json_equal(dict(signed_resolvers), dict(declared_resolvers)):
            raise AutomationProjectContractError("PROJECT_DYNAMIC_RESOLVER_STALE")

    invocation_contracts: dict[str, InvocationArgumentContract] = {}
    schedule_snapshots: list[dict[str, Any]] = []
    scheduler_contributions = tuple(
        entrypoint
        for entrypoint in sorted(effective_entrypoints)
        if normalized_entrypoint_kinds[entrypoint]
        == AutomationEntrypoint.SCHEDULER.value
    )
    if scheduled_rows and len(scheduler_contributions) != 1:
        raise AutomationProjectContractError(
            "PROJECT_SCHEDULE_CONTRIBUTION_AMBIGUOUS"
        )
    scheduler_contribution = (
        scheduler_contributions[0] if len(scheduler_contributions) == 1 else None
    )
    scheduler_template = (
        materialized_by_entrypoint.get(scheduler_contribution)
        if scheduler_contribution is not None
        else None
    )
    scheduler_resolvers = (
        dict(definition.dynamic_argument_resolvers.get(scheduler_contribution, {}))
        if scheduler_contribution is not None
        else {}
    )
    for row in sorted(scheduled_rows, key=lambda item: str(item.get("id") or "")):
        if str(row.get("automation_id") or "") != automation_id:
            raise AutomationProjectContractError("PROJECT_SCHEDULE_IDENTITY_MISMATCH")
        if str(row.get("tool_name") or "") != str(plugin_fragment["action_id"]):
            raise AutomationProjectContractError("PROJECT_SCHEDULE_TOOL_MISMATCH")
        if row.get("automation_generation") != automation_generation:
            raise AutomationProjectContractError("PROJECT_SCHEDULE_GENERATION_MISMATCH")
        task_id = str(row.get("id") or "").strip()
        if not task_id:
            raise AutomationProjectContractError("PROJECT_SCHEDULE_ID_REQUIRED")
        row_arguments = row.get("tool_params")
        if not isinstance(row_arguments, Mapping):
            raise AutomationProjectContractError("PROJECT_SCHEDULE_ARGUMENTS_INVALID")
        if scheduler_contribution is None:
            raise AutomationProjectContractError(
                "PROJECT_SCHEDULE_CONTRIBUTION_MISSING"
            )
        _validate_signed_action_arguments(
            plugin_fragment["invocation_contracts"][scheduler_contribution][
                "input_schema"
            ],
            row_arguments,
            error_code="PROJECT_SCHEDULE_ARGUMENTS_INVALID",
            dynamic_fields=set(scheduler_resolvers),
        )
        if not isinstance(scheduler_template, Mapping) or not _arguments_match(
            scheduler_template,
            row_arguments,
            scheduler_resolvers,
            {},
            validate_dynamic=False,
        ):
            raise AutomationProjectContractError("PROJECT_SCHEDULE_ARGUMENTS_STALE")
        configuration_version = row.get("configuration_version")
        if type(configuration_version) is not int or configuration_version <= 0:
            raise AutomationProjectContractError("PROJECT_SCHEDULE_VERSION_INVALID")
        contract_id = f"scheduler:{task_id}"
        invocation_contracts[contract_id] = InvocationArgumentContract(
            contract_id=contract_id,
            entrypoint=AutomationEntrypoint.SCHEDULER.value,
            expected_arguments=dict(row_arguments),
            dynamic_argument_resolvers=scheduler_resolvers,
            input_schema=plugin_fragment["invocation_contracts"][
                scheduler_contribution
            ]["input_schema"],
            contribution_id=(
                scheduler_contribution
                if runtime_model == "SERVICE_V2"
                else None
            ),
        )
        schedule_snapshots.append(
            {
                "task_id": task_id,
                "contract_id": contract_id,
                "configuration_version": configuration_version,
                "enabled": bool(row.get("enabled")),
                "cron_expression_hash": canonical_sha256(
                    str(row.get("cron_expression") or "")
                ),
                "arguments_hash": canonical_sha256(row_arguments),
                "dynamic_resolvers_hash": canonical_sha256(scheduler_resolvers),
            }
        )

    for entrypoint in sorted(
        effective_entrypoints - set(scheduler_contributions)
    ):
        arguments = materialized_by_entrypoint.get(entrypoint)
        if not isinstance(arguments, Mapping):
            raise AutomationProjectContractError("PROJECT_ENTRYPOINT_TEMPLATE_MISSING")
        _validate_signed_action_arguments(
            plugin_fragment["invocation_contracts"][entrypoint]["input_schema"],
            arguments,
            error_code="PROJECT_ARGUMENT_TEMPLATE_INVALID",
            dynamic_fields=set(
                definition.dynamic_argument_resolvers.get(entrypoint, {})
            ),
        )
        resolvers = dict(definition.dynamic_argument_resolvers.get(entrypoint, {}))
        contract_id = entrypoint
        invocation_contracts[contract_id] = InvocationArgumentContract(
            contract_id=contract_id,
            entrypoint=normalized_entrypoint_kinds[entrypoint],
            expected_arguments=dict(arguments),
            dynamic_argument_resolvers=resolvers,
            input_schema=plugin_fragment["invocation_contracts"][entrypoint][
                "input_schema"
            ],
            contribution_id=entrypoint if runtime_model == "SERVICE_V2" else None,
        )

    invocation_snapshots: list[dict[str, Any]] = []
    for item in sorted(
        invocation_contracts.values(), key=lambda value: value.contract_id
    ):
        invocation_snapshot = {
            "contract_id": item.contract_id,
            "entrypoint": item.entrypoint,
            "arguments_hash": canonical_sha256(item.expected_arguments),
            "dynamic_resolvers_hash": canonical_sha256(item.dynamic_argument_resolvers),
        }
        if runtime_model == "SERVICE_V2":
            invocation_snapshot["contribution_id"] = item.contribution_id
        invocation_snapshots.append(invocation_snapshot)
    snapshot = {
        "schema_version": 1,
        "automation_id": automation_id,
        "automation_generation": automation_generation,
        "manifest_sha256": plugin_fragment["manifest_sha256"],
        "tool_name": plugin_fragment["action_id"],
        "governance_anchor_name": definition.tool_name,
        "tool_version": str(plugin_fragment["tool_contract"]["version"]),
        "operation_type": operation_type,
        "risk_level": risk_level,
        "allowed_entrypoints": sorted(effective_entrypoints),
        "invocation_contracts": invocation_snapshots,
        "account_bindings_sha256": plugin_fragment["account_bindings_sha256"],
        "resource_bindings_sha256": plugin_fragment["resource_bindings_sha256"],
        "device_binding_sha256": plugin_fragment["device_binding_sha256"],
        "project_config_sha256": plugin_fragment["project_config_sha256"],
        "tool_contract_hash": tool_contract_hash,
        "plugin_contract_hash": plugin_contract_hash,
        "code_owned_plan_fields": sorted(code_owned_plan_fields),
        "scheduled_configurations": schedule_snapshots,
    }
    contract_hash = canonical_sha256(snapshot)
    # The browser-visible CAS token is the exact persisted aggregate version.
    # Contract drift remains bound separately by the full server-side digest.
    project_configuration_version = plugin_fragment["project_config_version"]
    restriction = _full_auto_restriction(tool_contract_payload, plugin_fragment)
    return CompiledAutomationProjectContract(
        automation_id=automation_id,
        automation_generation=automation_generation,
        manifest_sha256=plugin_fragment["manifest_sha256"],
        tool_name=plugin_fragment["action_id"],
        tool_version=str(plugin_fragment["tool_contract"]["version"]),
        operation_type=operation_type,
        risk_level=risk_level,
        invocation_contracts=invocation_contracts,
        account_bindings=normalized_accounts,
        allowed_entrypoints=effective_entrypoints,
        contract_hash=contract_hash,
        tool_contract_hash=tool_contract_hash,
        plugin_contract_hash=plugin_contract_hash,
        project_configuration_version=project_configuration_version,
        snapshot=snapshot,
        can_full_auto=restriction is None,
        code_owned_plan_fields=code_owned_plan_fields,
        restriction_code=restriction,
    )


def _full_auto_restriction(
    governance_anchor: Mapping[str, Any],
    plugin_fragment: Mapping[str, Any],
) -> str | None:
    operation_type = str(governance_anchor.get("operation_type") or "")
    approval = governance_anchor.get("approval")
    mode = str(approval.get("mode") or "") if isinstance(approval, Mapping) else ""
    if mode == "disabled":
        return "TOOL_DISABLED"
    runtime_model = str(plugin_fragment.get("runtime_model") or "ACTION_V1")
    full_auto_trust = (
        plugin_fragment.get("trust_source")
        in {"super_admin_upload", "builtin_bundle"}
        if runtime_model == "SERVICE_V2"
        else plugin_fragment.get("trust_source")
        in {"ed25519_upload", "ed25519_first_party"}
    )
    if not full_auto_trust:
        return "PLUGIN_TRUST_NOT_FULL_AUTO"
    plugin_tool = plugin_fragment.get("tool_contract")
    if not isinstance(plugin_tool, Mapping):
        return "PLUGIN_TOOL_CONTRACT_INVALID"
    if operation_type in {
        "internal_projection_write",
        "external_write",
        "financial_write",
        "destructive",
    }:
        schema = plugin_tool.get("input_schema")
        if not isinstance(schema, Mapping) or schema.get("additionalProperties") is not False:
            return "WRITE_INPUT_NOT_CLOSED"
        evidence = governance_anchor.get("evidence")
        postconditions = governance_anchor.get("postconditions")
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("required") is not True
            or not _named_values(evidence.get("required_fields"))
            or not _named_postconditions(postconditions)
        ):
            return "WRITE_VERIFICATION_NOT_CLOSED"
        idempotency = governance_anchor.get("idempotency")
        if not isinstance(idempotency, Mapping):
            return "WRITE_IDEMPOTENCY_CONTRACT_INVALID"
        idempotency_mode = str(idempotency.get("mode") or "")
        retry = governance_anchor.get("retry")
        if idempotency_mode == "none":
            if (
                not isinstance(retry, Mapping)
                or retry.get("safe") is not False
                or retry.get("max_attempts") != 1
            ):
                return "NON_IDEMPOTENT_WRITE_RETRY_UNSAFE"
        elif idempotency_mode == "key":
            if not _named_values(idempotency.get("key_fields")):
                return "IDEMPOTENCY_KEY_REQUIRED"
        elif idempotency_mode == "parameters":
            # The core registry treats the complete closed input object as the
            # idempotency identity. This mode therefore has no separate key
            # fields, but remains a valid signed write contract.
            if idempotency.get("key_fields") != []:
                return "WRITE_IDEMPOTENCY_CONTRACT_INVALID"
        else:
            return "WRITE_IDEMPOTENCY_CONTRACT_INVALID"
    return None


def _normalize_identifier_bindings(
    bindings: Mapping[str, Any],
    *,
    error_code: str,
) -> dict[str, str | tuple[str, ...]]:
    if not isinstance(bindings, Mapping):
        raise AutomationProjectContractError(error_code)
    normalized: dict[str, str | tuple[str, ...]] = {}
    for raw_role, raw_value in sorted(bindings.items(), key=lambda item: str(item[0])):
        role = str(raw_role or "").strip()
        if not role or len(role) > 128:
            raise AutomationProjectContractError(error_code)
        if isinstance(raw_value, str):
            value = raw_value.strip()
            if not value or len(value) > 128:
                raise AutomationProjectContractError(error_code)
            normalized[role] = value
            continue
        if not isinstance(raw_value, (list, tuple)) or not raw_value:
            raise AutomationProjectContractError(error_code)
        values = tuple(str(item or "").strip() for item in raw_value)
        if (
            any(not item or len(item) > 128 for item in values)
            or len(set(values)) != len(values)
        ):
            raise AutomationProjectContractError(error_code)
        normalized[role] = values
    return normalized


def _materialize_signed_invocation_arguments(
    fragment: Mapping[str, Any],
    *,
    definition: AutomationProjectInstanceDefinition,
    account_bindings: Mapping[str, str | tuple[str, ...]],
) -> dict[str, dict[str, Any]]:
    """Resolve signed templates using only core-owned project state.

    Account role declarations are signed by the package. Multiple roles may
    intentionally collect into one array argument (finance/customer fan-out),
    while a scalar role may only target one scalar argument. Project config
    cannot overwrite signed constants, dynamic fields, or account arguments.
    """

    config = definition.project_config
    if not isinstance(config, Mapping):
        raise AutomationProjectContractError("PROJECT_CONFIG_INVALID")
    config_schema = fragment.get("config_schema")
    if not isinstance(config_schema, Mapping):
        raise AutomationProjectContractError("PLUGIN_CONFIG_SCHEMA_INVALID")
    config_properties = config_schema.get("properties")
    if not isinstance(config_properties, Mapping):
        raise AutomationProjectContractError("PLUGIN_CONFIG_SCHEMA_INVALID")
    if set(config) - set(config_properties):
        raise AutomationProjectContractError("PROJECT_CONFIG_FIELD_NOT_SIGNED")

    raw_roles = fragment.get("account_roles")
    if not isinstance(raw_roles, list):
        raise AutomationProjectContractError("PLUGIN_ACCOUNT_ROLES_INVALID")
    runtime_kind = str(fragment.get("runtime_kind") or "")
    if runtime_kind != "python_subprocess":
        raise AutomationProjectContractError("PLUGIN_RUNTIME_KIND_INVALID")
    roles: dict[str, Mapping[str, Any]] = {}
    for raw_role in raw_roles:
        if not isinstance(raw_role, Mapping):
            raise AutomationProjectContractError("PLUGIN_ACCOUNT_ROLES_INVALID")
        role = str(raw_role.get("role") or "").strip()
        raw_field_name = raw_role.get("argument_field")
        field_name = (
            None
            if raw_field_name is None
            else str(raw_field_name or "").strip()
        )
        collection = raw_role.get("collection")
        required = raw_role.get("required")
        if (
            not role
            or type(collection) is not bool
            or type(required) is not bool
            or role in roles
            or field_name is not None
        ):
            raise AutomationProjectContractError("PLUGIN_ACCOUNT_ROLES_INVALID")
        roles[role] = raw_role
    if set(account_bindings) - set(roles):
        raise AutomationProjectContractError("PROJECT_ACCOUNT_BINDINGS_INVALID")
    missing_roles = {
        role
        for role, declaration in roles.items()
        if declaration.get("required") is True and role not in account_bindings
    }
    if missing_roles:
        raise AutomationProjectContractError("PROJECT_ACCOUNT_BINDINGS_MISSING")

    scalar_arguments: dict[str, str] = {}
    collected_arguments: dict[str, list[str]] = {}
    for role, declaration in roles.items():
        if role not in account_bindings:
            continue
        binding = account_bindings[role]
        raw_field_name = declaration.get("argument_field")
        # Subprocess plugins receive a short-lived broker capability only.
        # Their account binding is still required and hash-bound, but account
        # identifiers are never injected into plugin action arguments.
        if raw_field_name is None:
            continue
        field_name = str(raw_field_name)
        if declaration["collection"] is True:
            values = list(binding) if isinstance(binding, tuple) else [binding]
            target = collected_arguments.setdefault(field_name, [])
            for value in values:
                if value in target:
                    raise AutomationProjectContractError(
                        "PROJECT_ACCOUNT_BINDINGS_DUPLICATE"
                    )
                target.append(value)
            continue
        if isinstance(binding, tuple) or field_name in scalar_arguments:
            raise AutomationProjectContractError("PROJECT_ACCOUNT_BINDINGS_INVALID")
        scalar_arguments[field_name] = binding
    if set(scalar_arguments) & set(collected_arguments):
        raise AutomationProjectContractError("PLUGIN_ACCOUNT_ROLES_INVALID")

    raw_contracts = fragment.get("invocation_contracts")
    if not isinstance(raw_contracts, Mapping):
        raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
    materialized: dict[str, dict[str, Any]] = {}
    for raw_entrypoint, raw_contract in raw_contracts.items():
        entrypoint = str(raw_entrypoint)
        if not isinstance(raw_contract, Mapping):
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
        raw_template = raw_contract.get("argument_template")
        dynamic_resolvers = raw_contract.get("dynamic_resolvers")
        if not isinstance(raw_template, Mapping) or not isinstance(
            dynamic_resolvers, Mapping
        ):
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
        arguments: dict[str, Any] = {}
        for raw_field, raw_binding in raw_template.items():
            field_name = str(raw_field)
            if not isinstance(raw_binding, Mapping):
                raise AutomationProjectContractError(
                    "PLUGIN_INVOCATION_CONTRACT_INVALID"
                )
            source = raw_binding.get("source")
            if source == "project_config":
                if set(raw_binding) != {"source", "key"}:
                    raise AutomationProjectContractError(
                        "PLUGIN_INVOCATION_CONTRACT_INVALID"
                    )
                config_key = str(raw_binding.get("key") or "")
                if config_key != field_name or config_key not in config_properties:
                    raise AutomationProjectContractError(
                        "PLUGIN_INVOCATION_CONTRACT_INVALID"
                    )
                if config_key in config:
                    arguments[field_name] = config[config_key]
            elif source == "literal":
                if set(raw_binding) != {"source", "value"}:
                    raise AutomationProjectContractError(
                        "PLUGIN_INVOCATION_CONTRACT_INVALID"
                    )
                arguments[field_name] = raw_binding.get("value")
            else:
                raise AutomationProjectContractError(
                    "PLUGIN_INVOCATION_CONTRACT_INVALID"
                )
        if set(arguments) & (set(scalar_arguments) | set(collected_arguments)):
            raise AutomationProjectContractError("PLUGIN_ACCOUNT_TEMPLATE_INVALID")
        arguments.update(scalar_arguments)
        arguments.update(
            {field_name: list(values) for field_name, values in collected_arguments.items()}
        )
        if set(arguments) & set(dynamic_resolvers):
            raise AutomationProjectContractError("PLUGIN_DYNAMIC_TEMPLATE_INVALID")
        materialized[entrypoint] = arguments
    return materialized


def _validate_signed_action_arguments(
    schema: Any,
    arguments: Any,
    *,
    error_code: str,
    dynamic_fields: set[str] | None = None,
) -> None:
    """Validate the signed action payload without consulting a core executor.

    The plugin action schema is a separate contract from the core governance
    anchor.  This deliberately implements the closed JSON-Schema subset
    accepted by automation manifests and fails closed on unsupported schema
    constructs instead of falling back to a legacy core tool schema.
    """

    try:
        effective_schema = dict(schema) if isinstance(schema, Mapping) else schema
        if isinstance(effective_schema, dict) and dynamic_fields:
            required = effective_schema.get("required", [])
            if isinstance(required, list):
                effective_schema["required"] = [
                    field for field in required if field not in dynamic_fields
                ]
        _validate_signed_schema_value(effective_schema, arguments)
    except (TypeError, ValueError) as exc:
        raise AutomationProjectContractError(error_code) from exc


def _validate_signed_schema_value(schema: Any, value: Any) -> None:
    if not isinstance(schema, Mapping):
        raise TypeError("schema must be an object")
    supported = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "description",
        "title",
        "default",
    }
    if set(schema) - supported:
        raise ValueError("unsupported signed action schema construct")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not any(
            _strict_json_equal(value, candidate) for candidate in enum
        ):
            raise ValueError("value is outside enum")
    if "const" in schema and not _strict_json_equal(value, schema["const"]):
        raise ValueError("value differs from const")
    expected_type = schema.get("type")
    if "pattern" in schema and expected_type != "string":
        raise ValueError("pattern is only valid for strings")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            raise TypeError("value must be an object")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise TypeError("object schema is invalid")
        if any(not isinstance(item, str) for item in required) or not set(
            required
        ) <= set(properties):
            raise ValueError("object required fields are invalid")
        if set(required) - set(value):
            raise ValueError("required action field is absent")
        unknown = set(value) - set(properties)
        if unknown and schema.get("additionalProperties") is not True:
            raise ValueError("action payload contains an unsigned field")
        for field_name in set(value) & set(properties):
            _validate_signed_schema_value(properties[field_name], value[field_name])
        return
    if expected_type == "array":
        if not isinstance(value, list):
            raise TypeError("value must be an array")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if type(minimum) is int and len(value) < minimum:
            raise ValueError("array is too short")
        if type(maximum) is int and len(value) > maximum:
            raise ValueError("array is too long")
        if schema.get("uniqueItems") is True:
            hashes = [canonical_sha256(item) for item in value]
            if len(hashes) != len(set(hashes)):
                raise ValueError("array items are not unique")
        item_schema = schema.get("items")
        if not isinstance(item_schema, Mapping):
            raise TypeError("array item schema is required")
        for item in value:
            _validate_signed_schema_value(item_schema, item)
        return
    if expected_type == "string":
        if not isinstance(value, str):
            raise TypeError("value must be a string")
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if type(minimum) is int and len(value) < minimum:
            raise ValueError("string is too short")
        if type(maximum) is int and len(value) > maximum:
            raise ValueError("string is too long")
        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise TypeError("string pattern must be text")
            try:
                matched = re.search(pattern, value)
            except re.error as exc:
                raise ValueError("string pattern is invalid") from exc
            if matched is None:
                raise ValueError("string does not match pattern")
        return
    if expected_type == "integer":
        if type(value) is not int:
            raise TypeError("value must be an integer")
    elif expected_type == "number":
        if type(value) not in {int, float, Decimal}:
            raise TypeError("value must be numeric")
    elif expected_type == "boolean":
        if type(value) is not bool:
            raise TypeError("value must be boolean")
        return
    elif expected_type == "null":
        if value is not None:
            raise TypeError("value must be null")
        return
    else:
        raise ValueError("signed action schema type is unsupported")
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    numeric = Decimal(str(value))
    if minimum is not None and numeric < Decimal(str(minimum)):
        raise ValueError("numeric value is too small")
    if maximum is not None and numeric > Decimal(str(maximum)):
        raise ValueError("numeric value is too large")


def _named_values(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _named_postconditions(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if isinstance(item, str) and item.strip():
            continue
        if isinstance(item, Mapping) and isinstance(item.get("name"), str) and item["name"].strip():
            continue
        return False
    return True


def _validate_plugin_fragment(
    fragment: Mapping[str, Any],
    *,
    automation_id: str,
    definition: AutomationProjectInstanceDefinition,
    core_tool_contract: Mapping[str, Any],
) -> None:
    if str(fragment.get("automation_id") or "") != automation_id:
        raise AutomationProjectContractError("PLUGIN_INSTANCE_IDENTITY_MISMATCH")
    if str(fragment.get("plugin_id") or "") != str(definition.plugin_id or ""):
        raise AutomationProjectContractError("PLUGIN_IDENTITY_MISMATCH")
    if not str(fragment.get("plugin_id") or "").strip():
        raise AutomationProjectContractError("PLUGIN_ID_REQUIRED")
    version = fragment.get("plugin_version", fragment.get("version"))
    if not isinstance(version, str) or not version.strip():
        raise AutomationProjectContractError("PLUGIN_VERSION_REQUIRED")
    runtime_model = str(fragment.get("runtime_model") or "ACTION_V1")
    allowed_trust_sources = (
        {"super_admin_upload", "builtin_bundle"}
        if runtime_model == "SERVICE_V2"
        else {"ed25519_upload", "ed25519_first_party", "builtin_release"}
    )
    if runtime_model not in {"ACTION_V1", "SERVICE_V2"} or fragment.get(
        "trust_source"
    ) not in allowed_trust_sources:
        raise AutomationProjectContractError("PLUGIN_TRUST_SOURCE_INVALID")
    for field_name in ("package_sha256", "manifest_sha256"):
        digest = fragment.get(field_name)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise AutomationProjectContractError("PLUGIN_DIGEST_INVALID")
    plugin_tool = fragment.get("tool_contract")
    if not isinstance(plugin_tool, Mapping):
        raise AutomationProjectContractError("PLUGIN_TOOL_CONTRACT_INVALID")
    governance_anchor = fragment.get("governance_anchor")
    if not isinstance(governance_anchor, Mapping):
        raise AutomationProjectContractError("PLUGIN_GOVERNANCE_ANCHOR_INVALID")
    if set(governance_anchor) != set(_CORE_GOVERNANCE_FIELDS):
        raise AutomationProjectContractError("PLUGIN_GOVERNANCE_ANCHOR_INVALID")
    if str(governance_anchor.get("name") or "") != definition.tool_name:
        raise AutomationProjectContractError("PLUGIN_TOOL_IDENTITY_MISMATCH")
    if canonical_sha256(governance_anchor) != canonical_sha256(core_tool_contract):
        raise AutomationProjectContractError("PLUGIN_TOOL_CONTRACT_MISMATCH")
    if fragment.get("governance_anchor_sha256") != canonical_sha256(
        governance_anchor
    ):
        raise AutomationProjectContractError("PLUGIN_GOVERNANCE_ANCHOR_INVALID")
    signed_action_governance = {
        key: plugin_tool.get(key) for key in _CORE_GOVERNANCE_FIELDS
    }
    if canonical_sha256(signed_action_governance) != canonical_sha256(
        governance_anchor
    ):
        raise AutomationProjectContractError("PLUGIN_TOOL_CONTRACT_MISMATCH")
    if fragment.get("project_full_auto_allowed") is not plugin_tool.get(
        "project_full_auto_allowed"
    ):
        raise AutomationProjectContractError("PLUGIN_FULL_AUTO_CEILING_MISMATCH")
    if fragment.get("runtime_kind") != "python_subprocess":
        raise AutomationProjectContractError("PLUGIN_RUNTIME_KIND_INVALID")
    action_id = str(fragment.get("action_id") or "")
    if action_id != f"automation.{automation_id}.run":
        raise AutomationProjectContractError("PLUGIN_ACTION_IDENTITY_MISMATCH")

    raw_code_owned_plan_fields = fragment.get("code_owned_plan_fields", [])
    if (
        not isinstance(raw_code_owned_plan_fields, list)
        or any(
            not isinstance(item, str) or not item.strip()
            for item in raw_code_owned_plan_fields
        )
        or raw_code_owned_plan_fields
        != sorted(set(raw_code_owned_plan_fields))
    ):
        raise AutomationProjectContractError(
            "PLUGIN_CODE_OWNED_PLAN_FIELDS_INVALID"
        )
    exact_code_owned_identity = (
        str(fragment.get("trust_source") or ""),
        automation_id,
        str(fragment.get("plugin_id") or ""),
    )
    expected_code_owned_plan_fields = {
        (
            "ed25519_first_party",
            "customer_problems_shadow",
            "sync_customer_service_problems",
        ): ["recheck_items"],
        (
            "ed25519_first_party",
            "scan_codes",
            "sync_scan_codes",
        ): ["_scan_preview_binding", "dry_run"],
    }.get(exact_code_owned_identity, [])
    if raw_code_owned_plan_fields != expected_code_owned_plan_fields:
        raise AutomationProjectContractError(
            "PLUGIN_CODE_OWNED_PLAN_FIELDS_INVALID"
        )

    plugin_entrypoints = fragment.get("allowed_entrypoints")
    entrypoint_kinds = fragment.get("entrypoint_kinds")
    if runtime_model == "ACTION_V1" and entrypoint_kinds is None and isinstance(
        plugin_entrypoints, list
    ):
        entrypoint_kinds = {
            str(entrypoint): str(entrypoint) for entrypoint in plugin_entrypoints
        }
    invocation_contracts = fragment.get("invocation_contracts")
    config_schema = fragment.get("config_schema")
    account_roles = fragment.get("account_roles")
    if not isinstance(plugin_entrypoints, list) or not isinstance(
        invocation_contracts, Mapping
    ) or not isinstance(config_schema, Mapping) or not isinstance(account_roles, list):
        raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
    if (
        set(invocation_contracts) != set(plugin_entrypoints)
        or not isinstance(entrypoint_kinds, Mapping)
        or set(entrypoint_kinds) != set(plugin_entrypoints)
        or any(
            str(kind) not in TRUSTED_AUTOMATION_ENTRYPOINTS | {"events"}
            for kind in entrypoint_kinds.values()
        )
    ):
        raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
    for entrypoint, raw_contract in invocation_contracts.items():
        if not str(entrypoint).strip() or not isinstance(raw_contract, Mapping):
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
        expected_contract_fields = {
            "input_schema",
            "argument_template",
            "dynamic_resolvers",
        }
        if runtime_model == "SERVICE_V2":
            expected_contract_fields |= {
                "service",
                "operation",
                "contribution_kind",
            }
        if set(raw_contract) != expected_contract_fields:
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
        if (
            str(raw_contract.get("contribution_kind") or entrypoint)
            != str(entrypoint_kinds.get(entrypoint) or "")
        ):
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
        if not isinstance(raw_contract.get("input_schema"), Mapping):
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
        if not isinstance(raw_contract.get("argument_template"), Mapping):
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")
        if not isinstance(raw_contract.get("dynamic_resolvers"), Mapping):
            raise AutomationProjectContractError("PLUGIN_INVOCATION_CONTRACT_INVALID")

    config_version = fragment.get("project_config_version")
    if type(config_version) is not int or config_version <= 0:
        raise AutomationProjectContractError("PROJECT_CONFIG_VERSION_INVALID")
    for field_name in (
        "project_config_sha256",
        "account_bindings_sha256",
        "resource_bindings_sha256",
        "device_binding_sha256",
    ):
        digest = fragment.get(field_name)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise AutomationProjectContractError("PROJECT_BINDING_DIGEST_INVALID")


def _arguments_match(
    expected: Mapping[str, Any],
    actual: Any,
    dynamic_rules: Mapping[str, str],
    execution_context: Mapping[str, Any],
    *,
    validate_dynamic: bool = True,
    code_owned_plan_fields: frozenset[str] = frozenset(),
    input_schema: Mapping[str, Any] | None = None,
    dynamic_resolver: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
) -> bool:
    if not isinstance(actual, Mapping):
        return False
    left = dict(expected)
    right = dict(actual)
    for field_name, rule in dynamic_rules.items():
        expected_value = left.pop(field_name, None)
        actual_present = field_name in right
        actual_value = right.pop(field_name, None)
        if rule == "scheduled_previous_day":
            # The stored schedule template omits the occurrence-specific value.
            if not validate_dynamic:
                if actual_present and actual_value not in (None, ""):
                    return False
                continue
            scheduled_for = _parse_scheduled_for(execution_context.get("scheduled_for"))
            if scheduled_for is None or not actual_present:
                return False
            if actual_value != (scheduled_for.date() - timedelta(days=1)).isoformat():
                return False
            continue
        if not validate_dynamic:
            if actual_present and not _strict_json_equal(expected_value, actual_value):
                return False
            continue
        if dynamic_resolver is None:
            return False
        try:
            resolved_value = dynamic_resolver(str(rule), field_name, execution_context)
        except Exception:
            return False
        if resolved_value is OMIT_DYNAMIC_ARGUMENT:
            if actual_present:
                return False
            continue
        if not actual_present:
            return False
        if not _strict_json_equal(resolved_value, actual_value):
            return False
    if not _code_owned_plan_arguments_match(
        expected=left,
        actual=right,
        fields=code_owned_plan_fields,
        input_schema=input_schema,
        execution_context=execution_context,
    ):
        return False
    return _strict_json_equal(left, right)


def _code_owned_plan_arguments_match(
    *,
    expected: Mapping[str, Any],
    actual: dict[str, Any],
    fields: frozenset[str],
    input_schema: Mapping[str, Any] | None,
    execution_context: Mapping[str, Any],
) -> bool:
    if not fields:
        return True
    if not isinstance(input_schema, Mapping):
        return False
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return False
    if fields == frozenset({"dry_run", "_scan_preview_binding"}):
        if any(field in expected for field in fields) or "dry_run" not in actual:
            return False
        dry_run = actual.pop("dry_run")
        dry_run_schema = properties.get("dry_run")
        if not isinstance(dry_run_schema, Mapping):
            return False
        try:
            _validate_signed_schema_value(dry_run_schema, dry_run)
        except (TypeError, ValueError):
            return False
        binding_present = "_scan_preview_binding" in actual
        binding = actual.pop("_scan_preview_binding", None)
        raw_context = execution_context.get("scan_preview")
        if dry_run:
            return not binding_present and raw_context is None
        signed_schema = properties.get("_scan_preview_binding")
        if not binding_present or not isinstance(signed_schema, Mapping):
            return False
        try:
            _validate_signed_schema_value(signed_schema, binding)
        except (TypeError, ValueError):
            return False
        return isinstance(raw_context, Mapping) and _strict_json_equal(
            raw_context,
            binding,
        )
    if fields != frozenset({"recheck_items"}):
        return False
    if "recheck_items" in expected or "recheck_items" not in actual:
        return False
    signed_schema = properties.get("recheck_items")
    if not isinstance(signed_schema, Mapping):
        return False
    recheck_items = actual.pop("recheck_items")
    try:
        _validate_signed_schema_value(signed_schema, recheck_items)
    except (TypeError, ValueError):
        return False
    if not isinstance(recheck_items, list):
        return False
    dedupe_keys: list[str] = []
    for item in recheck_items:
        if not isinstance(item, Mapping) or _contains_argument_field(
            item,
            "account_id",
        ):
            return False
        dedupe_key = item.get("dedupe_key")
        if (
            not isinstance(dedupe_key, str)
            or not dedupe_key.strip()
            or dedupe_key != dedupe_key.strip()
        ):
            return False
        dedupe_keys.append(dedupe_key)
    return (
        len(dedupe_keys) == len(set(dedupe_keys))
        and dedupe_keys == sorted(dedupe_keys)
    )


def _contains_argument_field(value: Any, field_name: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).strip().lower() == field_name
            or _contains_argument_field(nested, field_name)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_argument_field(item, field_name) for item in value)
    return False


def _parse_scheduled_for(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(ZoneInfo("Asia/Shanghai"))


def _strict_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _strict_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


_SENSITIVE_FRAGMENT_MARKERS = (
    "password",
    "secret",
    "token",
    "cookie",
    "credential",
    "authorization",
    "api_key",
    "apikey",
)


def _assert_privacy_safe_fragment(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE_FRAGMENT_MARKERS):
                raise AutomationProjectContractError("PLUGIN_CONTRACT_FRAGMENT_SENSITIVE")
            _assert_privacy_safe_fragment(nested)
    elif isinstance(value, list):
        for item in value:
            _assert_privacy_safe_fragment(item)
    else:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise AutomationProjectContractError("PLUGIN_CONTRACT_FRAGMENT_INVALID") from exc
