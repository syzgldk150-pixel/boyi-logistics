"""Pure contracts for per-scheduled-task approval policy snapshots.

The snapshot deliberately stores hashes of task arguments and governed tool
behaviour instead of the raw values.  This keeps policy/audit tables free of
credentials and request payloads while still making any material change
invalidate an exact-schedule exemption.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


POLICY_SCHEMA_VERSION = 1
_DAILY_CRON_RE = re.compile(r"^(?P<minute>\d{1,2}) (?P<hour>\d{1,2}) \* \* \*$")


class ScheduledTaskApprovalMode(str, Enum):
    REQUIRE_EACH_RUN = "REQUIRE_EACH_RUN"
    EXACT_SCHEDULE_EXEMPT = "EXACT_SCHEDULE_EXEMPT"


class ScheduledTaskPolicyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    UNSUPPORTED = "UNSUPPORTED"


class ScheduledTaskApprovalContractError(ValueError):
    """A task/tool pair cannot receive an exact-schedule exemption."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Only behaviour- and governance-bearing fields participate.  In particular,
# display descriptions are excluded so wording-only edits do not revoke an
# otherwise identical authorization.
TOOL_CONTRACT_FIELDS = (
    "version",
    "operation_type",
    "risk_level",
    "approval",
    "permissions",
    "account_scope",
    "idempotency",
    "retry",
    "evidence",
    "postconditions",
    "input_schema",
    "output_schema",
    "executor",
    "timeout",
    "cancel",
)


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible policy material deterministically."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def tool_contract_snapshot(capability: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(capability, Mapping):
        raise ScheduledTaskApprovalContractError("UNKNOWN_TOOL")
    return {
        field: capability[field]
        for field in TOOL_CONTRACT_FIELDS
        if field in capability
    }


def tool_contract_hash(capability: Mapping[str, Any]) -> str:
    return sha256_json(tool_contract_snapshot(capability))


def exemption_eligibility(capability: Mapping[str, Any] | None) -> tuple[bool, str | None]:
    """Return whether the tool explicitly permits exact scheduled execution."""

    if not isinstance(capability, Mapping):
        return False, "UNKNOWN_TOOL"
    approval = capability.get("approval")
    if not isinstance(approval, Mapping) or approval.get("mode") != "schedule_allowlist":
        return False, "TOOL_REQUIRES_PER_RUN_APPROVAL"
    operation_type = str(capability.get("operation_type") or "")
    risk_level = str(capability.get("risk_level") or "")
    if operation_type == "destructive" or risk_level == "extreme" or approval.get("mode") == "disabled":
        return False, "OPERATION_DISABLED"
    return True, None


@dataclass(frozen=True)
class ScheduledTaskContract:
    snapshot: Mapping[str, Any]
    contract_hash: str
    tool_contract_hash: str
    arguments_hash: str
    dynamic_rules_hash: str


def build_scheduled_task_contract(
    task: Mapping[str, Any],
    capability: Mapping[str, Any],
    *,
    dynamic_argument_rules: Mapping[str, str] | None = None,
    allowed_special_cron: str | None = None,
) -> ScheduledTaskContract:
    """Build the privacy-safe exact execution contract for one task row."""

    eligible, reason = exemption_eligibility(capability)
    if not eligible:
        raise ScheduledTaskApprovalContractError(reason or "EXEMPTION_NOT_ALLOWED")
    if not isinstance(task, Mapping):
        raise ScheduledTaskApprovalContractError("INVALID_TASK")
    task_id = str(task.get("id") or "").strip()
    tool_name = str(task.get("tool_name") or "").strip()
    cron_expression = str(task.get("cron_expression") or "").strip()
    arguments = task.get("tool_params")
    configuration_version = task.get("configuration_version")
    enabled = task.get("enabled")
    if not task_id or not tool_name or not cron_expression:
        raise ScheduledTaskApprovalContractError("INVALID_TASK")
    if allowed_special_cron is not None:
        if allowed_special_cron != "@startup" or cron_expression != allowed_special_cron:
            raise ScheduledTaskApprovalContractError("SPECIAL_CRON_NOT_APPROVED")
    else:
        cron_match = _DAILY_CRON_RE.fullmatch(cron_expression)
        if (
            cron_match is None
            or not 0 <= int(cron_match.group("minute")) <= 59
            or not 0 <= int(cron_match.group("hour")) <= 23
        ):
            raise ScheduledTaskApprovalContractError("CRON_NOT_EXACT_DAILY_TIME")
    if not isinstance(arguments, Mapping):
        raise ScheduledTaskApprovalContractError("INVALID_TOOL_ARGUMENTS")
    if isinstance(configuration_version, bool) or not isinstance(configuration_version, int):
        raise ScheduledTaskApprovalContractError("INVALID_CONFIGURATION_VERSION")
    if configuration_version < 1:
        raise ScheduledTaskApprovalContractError("INVALID_CONFIGURATION_VERSION")
    if not (enabled is True or type(enabled) is int and enabled == 1):
        raise ScheduledTaskApprovalContractError("TASK_DISABLED")
    if tool_name != str(capability.get("name") or tool_name):
        # Test doubles and legacy catalogs may omit ``name``.  A present name,
        # however, must identify the exact task tool.
        raise ScheduledTaskApprovalContractError("TOOL_NAME_MISMATCH")

    rules = dict(dynamic_argument_rules or {})
    if any(not str(key).strip() or not str(value).strip() for key, value in rules.items()):
        raise ScheduledTaskApprovalContractError("INVALID_DYNAMIC_ARGUMENT_RULES")
    governed_tool_hash = tool_contract_hash(capability)
    arguments_digest = sha256_json(dict(arguments))
    rules_digest = sha256_json(rules)
    postconditions_digest = sha256_json(capability.get("postconditions") or [])
    snapshot = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "task_id": task_id,
        "tool_name": tool_name,
        "tool_version": str(capability.get("version") or ""),
        "operation_type": str(capability.get("operation_type") or ""),
        "risk_level": str(capability.get("risk_level") or ""),
        "approval_mode": "schedule_allowlist",
        "cron_expression": cron_expression,
        "enabled": True,
        "configuration_version": configuration_version,
        "arguments_hash": arguments_digest,
        "dynamic_rules_hash": rules_digest,
        "postconditions_hash": postconditions_digest,
        "tool_contract_hash": governed_tool_hash,
    }
    return ScheduledTaskContract(
        snapshot=snapshot,
        contract_hash=sha256_json(snapshot),
        tool_contract_hash=governed_tool_hash,
        arguments_hash=arguments_digest,
        dynamic_rules_hash=rules_digest,
    )
