"""Closed broker primitives for signed Ronghui problem actions.

The replaceable packages own candidate selection and operation order.  This
module exposes only exact resource reads, exact projections, and one-record
Ronghui write/readback primitives.  Account and resource identifiers stay in
the broker side channel and are never returned to package code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.core_adapter import (
    CoreBrokerHandler,
    CoreBrokerInvocationContext,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes


AccountDescriptorPort = Callable[[str], Mapping[str, Any]]
ProblemActionPort = Callable[
    [Mapping[str, Any], str, Mapping[str, Any]], Mapping[str, Any]
]
SheetRowsReadPort = Callable[[str, str, int], Mapping[str, Any]]
SheetRowsReplacePort = Callable[[str, list[list[Any]]], Mapping[str, Any]]
SnapshotReadPort = Callable[[int], Sequence[Mapping[str, Any]]]
SnapshotReplacePort = Callable[[list[dict[str, Any]]], Mapping[str, Any]]
ResultUpsertPort = Callable[[dict[str, str]], Mapping[str, Any]]
ProblemEventUpsertPort = Callable[
    [Mapping[str, Any], dict[str, str]], Mapping[str, Any]
]
CapabilityAuthorizationPort = Callable[[Mapping[str, Any], str], None]


_SELF_TOOL = "self_pickup_problem_upload"
_SPLIT_TOOL = "split_pending_problem_upload"
_SELF_SOURCE_ROLE = "self_pickup_source_sheet"
_SPLIT_SOURCE_ROLE = "split_pending_source_sheet"
_SPLIT_TARGET_ROLE = "split_pending_target_sheet"
_PRIMARY_ACCOUNT_ROLE = "account_id"
_DAXIANG_ACCOUNT_ROLE = "daxiang_s_account_id"
_MAX_COLUMNS = 19
_SELF_MAX_ROWS = 2_000
_SPLIT_MAX_ROWS = 5_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

_SELF_PROBLEM_TYPE = "开单为自提件"
_SELF_OWNER_TYPE = "特殊时效"
_SELF_PRIMARY_CAUSE = (
    "货已到，尽快安排提货，自提部免费仓储只有1天，尽快提走，"
    "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
    "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
    "托盘类、少量件数类货物提货时间:9:00-20:00；"
    "件数多的需要装卸工操作的货物提货时间10:00-20:00；"
)
_SELF_DAXIANG_CAUSE = (
    "货已到，尽快安排提货，网点免费仓储只有3天，尽快提走，"
    "超时产生仓储费0.03元/KG/天10元票/天；自提电话：0739-5186128 "
    "地址：双清区建设南路白马田伟业物流城内融辉物流(导航：勇胜物流)；"
    "托盘类、少量件数类货物提货时间:9:00-20:00；"
    "件数多的需要装卸工操作的货物提货时间10:00-20:00"
)
_SELF_CAUSE_BY_ROLE = {
    _PRIMARY_ACCOUNT_ROLE: _SELF_PRIMARY_CAUSE,
    _DAXIANG_ACCOUNT_ROLE: _SELF_DAXIANG_CAUSE,
}
_SPLIT_OWNER_BY_TYPE = {
    "少货/分批": "交接异常",
    "有发未到": "通知类（不顺延时效）",
}
MARKED_WRITE_ACTION_KEYS = frozenset(
    {
        ("browser.invoke", "ronghui.problem.create"),
        ("projection.invoke", "split_pending.snapshot.replace"),
        ("network.request", "feishu.sheet.replace_rows"),
        ("projection.invoke", "split_pending.result.upsert"),
        ("ledger.invoke", "daily_sign.problem_event.upsert"),
    }
)
_TARGET_HEADERS = (
    "运单编号",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "回单号",
    "实际重量",
    "体积",
    "备注",
    "目的站点",
    "收件人",
    "收件电话",
    "收件地址",
    "结算重量",
    "体积重",
    "运费",
    "支付类型",
    "到付款",
    "累计到货件数",
)
_SNAPSHOT_FIELDS = {
    "arrived_quantity",
    "bill_code",
    "destination_station",
    "expected_quantity",
    "pending_quantity",
    "problem_cause",
    "problem_owner_type",
    "problem_type",
    "source_row_no",
}
_SNAPSHOT_PUBLIC_FIELDS = (
    "tracking_number",
    "problem_type",
    "upload_status",
    "complaint_status",
)


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _strict(arguments: Mapping[str, Any], fields: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping) or set(arguments) != fields:
        raise _error("problem primitive arguments are invalid", "BROKER_ARGUMENT_INVALID")
    return dict(arguments)


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if value is None or isinstance(value, (bool, Mapping, list, tuple, set)):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    result = str(value).strip()
    if not result or len(result) > maximum:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return result


def _optional_text(value: object, label: str, *, maximum: int = 512) -> str:
    if value in (None, ""):
        return ""
    return _text(value, label, maximum=maximum)


def _waybill(value: object) -> str:
    result = _text(value, "bill_code", maximum=128)
    if any(character.isspace() for character in result):
        raise _error("bill_code contains whitespace", "BROKER_ARGUMENT_INVALID")
    return result


def _integer(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if str(result) != str(value).strip() or not minimum <= result <= maximum:
        raise _error(f"{label} is outside its signed limit", "BROKER_ARGUMENT_INVALID")
    return result


def _sha256(value: object, label: str) -> str:
    result = _text(value, label, maximum=64)
    if _SHA256_RE.fullmatch(result) is None:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return result


def _require_context(
    context: CoreBrokerInvocationContext,
    *,
    operation: str,
    action: str,
    roles: set[str],
    tools: set[str],
) -> None:
    if (
        context.tool_name not in tools
        or context.operation != operation
        or context.action != action
        or context.role not in roles
    ):
        raise _error("problem broker context is invalid", "BROKER_CONTEXT_INVALID")


def _one_account(
    context: CoreBrokerInvocationContext,
    ports: "ProblemHandlerPorts",
) -> Mapping[str, Any]:
    if len(context.account_ids) != 1:
        raise _error("problem action requires one exact account", "BROKER_CONTEXT_INVALID")
    account_id = str(context.account_ids[0]).strip()
    bound = tuple(
        str(value).strip()
        for value in context.account_bindings.get(context.role, ())
    )
    if not account_id or bound != (account_id,):
        raise _error("problem account binding changed", "BROKER_CONTEXT_INVALID")
    descriptor = ports.describe_account(account_id)
    if not isinstance(descriptor, Mapping):
        raise _error("problem account descriptor is invalid", "BROKER_ACCOUNT_INVALID")
    if str(descriptor.get("account_id") or "").strip() != account_id:
        raise _error("problem account descriptor changed", "BROKER_ACCOUNT_INVALID")
    if str(descriptor.get("system") or "").strip().lower() != "ronghui":
        raise _error(
            "problem action requires a Ronghui account",
            "BROKER_ACCOUNT_SYSTEM_MISMATCH",
        )
    if not str(descriptor.get("session_profile") or "").strip():
        raise _error("problem account is not authenticated", "BLOCKED_LOGIN")
    return descriptor


def _resource_id(context: CoreBrokerInvocationContext, role: str) -> str:
    if context.role != role or not context.resource_id:
        raise _error("problem resource is unbound", "BROKER_RESOURCE_UNAVAILABLE")
    bound = str(context.resource_bindings.get(role) or "").strip()
    if bound != context.resource_id:
        raise _error("problem resource binding changed", "BROKER_CONTEXT_INVALID")
    return bound


def _binding_scope(context: CoreBrokerInvocationContext) -> dict[str, str]:
    account_material = {
        key: list(value) for key, value in sorted(context.account_bindings.items())
    }
    resource_material = dict(sorted(context.resource_bindings.items()))
    return {
        "automation_id": context.automation_id,
        "binding_sha256": hashlib.sha256(
            canonical_json_bytes(
                {"accounts": account_material, "resources": resource_material}
            )
        ).hexdigest(),
        "plugin_version": context.plugin_version,
        "role": context.role,
        "tool_name": context.tool_name,
    }


class _OpaqueCodec:
    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("problem broker secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self._lock = threading.Lock()
        self._preconditions: dict[str, tuple[float, dict[str, str], str, dict[str, Any]]] = {}

    def encode(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        payload: Mapping[str, Any],
    ) -> str:
        nonce = secrets.token_urlsafe(32)
        scope = _binding_scope(context)
        body = canonical_json_bytes(
            {"context": scope, "nonce": nonce, "purpose": purpose}
        )
        signature = base64.urlsafe_b64encode(
            hmac.new(self._secret, body, hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        now = time.monotonic()
        with self._lock:
            self._preconditions = {
                key: entry
                for key, entry in self._preconditions.items()
                if entry[0] > now
            }
            if len(self._preconditions) >= 4_096:
                raise _error(
                    "problem precondition capacity is exhausted",
                    "BROKER_CONCURRENCY_BLOCKED",
                )
            self._preconditions[nonce] = (
                now + 3_600.0,
                scope,
                purpose,
                dict(payload),
            )
        return f"problem:v1:{nonce}:{signature}"

    def decode(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        reference: object,
    ) -> dict[str, Any]:
        raw = _text(reference, "precondition_ref", maximum=2_000)
        parts = raw.split(":")
        if len(parts) != 4 or parts[:2] != ["problem", "v1"]:
            raise _error("problem precondition is invalid", "BROKER_CURSOR_INVALID")
        nonce = parts[2]
        scope = _binding_scope(context)
        body = canonical_json_bytes(
            {"context": scope, "nonce": nonce, "purpose": purpose}
        )
        expected = base64.urlsafe_b64encode(
            hmac.new(self._secret, body, hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(parts[3], expected):
            raise _error("problem precondition is invalid", "BROKER_CURSOR_INVALID")
        now = time.monotonic()
        with self._lock:
            entry = self._preconditions.pop(nonce, None)
        if entry is None:
            raise _error("problem precondition is invalid", "BROKER_CURSOR_INVALID")
        expires_at, stored_scope, stored_purpose, payload = entry
        if expires_at <= now or stored_scope != scope or stored_purpose != purpose:
            raise _error("problem precondition is invalid", "BROKER_CURSOR_INVALID")
        return dict(payload)

    def evidence(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        payload: Mapping[str, Any],
    ) -> str:
        digest = hmac.new(
            self._secret,
            canonical_json_bytes(
                {
                    "context": _binding_scope(context),
                    "payload": dict(payload),
                    "purpose": purpose,
                }
            ),
            hashlib.sha256,
        ).hexdigest()
        return f"broker-evidence:problem:{purpose}:v1:{digest}"


@dataclass(frozen=True)
class ProblemHandlerPorts:
    describe_account: AccountDescriptorPort
    problem_action: ProblemActionPort
    sheet_rows_read: SheetRowsReadPort
    sheet_rows_replace: SheetRowsReplacePort
    snapshot_read: SnapshotReadPort
    snapshot_replace: SnapshotReplacePort
    result_upsert: ResultUpsertPort
    problem_event_upsert: ProblemEventUpsertPort
    authorize_capability: CapabilityAuthorizationPort | None = None


def _self_plan(context: CoreBrokerInvocationContext, bill_code: str) -> dict[str, Any]:
    cause = _SELF_CAUSE_BY_ROLE.get(context.role)
    if cause is None:
        raise _error("self-pickup account role is invalid", "BROKER_CONTEXT_INVALID")
    return {
        "bill_code": bill_code,
        "problem_cause_sha256": hashlib.sha256(cause.encode("utf-8")).hexdigest(),
        "problem_owner_type": _SELF_OWNER_TYPE,
        "problem_type": _SELF_PROBLEM_TYPE,
        "update_postpone_days": True,
    }


def _split_plan(arguments: Mapping[str, Any]) -> dict[str, Any]:
    values = _strict(
        arguments,
        {
            "bill_code",
            "problem_cause_sha256",
            "problem_owner_type",
            "problem_type",
        },
    )
    problem_type = _text(values.get("problem_type"), "problem_type", maximum=64)
    owner = _text(
        values.get("problem_owner_type"),
        "problem_owner_type",
        maximum=64,
    )
    if _SPLIT_OWNER_BY_TYPE.get(problem_type) != owner:
        raise _error("split problem classification is invalid", "BROKER_ARGUMENT_INVALID")
    return {
        "bill_code": _waybill(values.get("bill_code")),
        "problem_cause_sha256": _sha256(
            values.get("problem_cause_sha256"),
            "problem_cause_sha256",
        ),
        "problem_owner_type": owner,
        "problem_type": problem_type,
        "update_postpone_days": False,
    }


def _problem_plan_from_query(
    context: CoreBrokerInvocationContext,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    if context.tool_name == _SELF_TOOL:
        values = _strict(arguments, {"bill_code"})
        return _self_plan(context, _waybill(values.get("bill_code")))
    if context.tool_name == _SPLIT_TOOL:
        return _split_plan(arguments)
    raise _error("problem tool is invalid", "BROKER_CONTEXT_INVALID")


def _problem_identity(value: object, plan: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _error("problem readback proof is invalid", "BROKER_SOURCE_INVALID")
    identity = {
        "bill_code": _waybill(value.get("bill_code")),
        "external_id": _text(value.get("external_id"), "external_id", maximum=256),
        "problem_cause_sha256": _sha256(
            value.get("problem_cause_sha256"),
            "problem_cause_sha256",
        ),
        "problem_owner_type": _text(
            value.get("problem_owner_type"),
            "problem_owner_type",
            maximum=64,
        ),
        "problem_type": _text(value.get("problem_type"), "problem_type", maximum=64),
        "registered_at": _text(
            value.get("registered_at"),
            "registered_at",
            maximum=64,
        ),
        "registered_site": _optional_text(
            value.get("registered_site"),
            "registered_site",
            maximum=256,
        ),
    }
    for field in (
        "bill_code",
        "problem_cause_sha256",
        "problem_owner_type",
        "problem_type",
    ):
        if identity[field] != plan[field]:
            raise _error("problem readback identity changed", "BROKER_SOURCE_INVALID")
    return identity


def _safe_cell(value: object) -> object:
    if value is None or (
        isinstance(value, (str, int, float)) and not isinstance(value, bool)
    ):
        return value
    raise _error("sheet cell is invalid", "BROKER_SOURCE_INVALID")


def _rows(value: object, *, maximum: int) -> list[list[Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise _error("sheet rows are invalid", "BROKER_SOURCE_INVALID")
    output: list[list[Any]] = []
    for raw in value:
        if not isinstance(raw, list) or len(raw) > _MAX_COLUMNS:
            raise _error("sheet row is invalid", "BROKER_SOURCE_INVALID")
        output.append([_safe_cell(cell) for cell in raw])
    return output


def _count(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return int(number)


class _ProblemHandlers:
    def __init__(self, ports: ProblemHandlerPorts, *, secret: bytes) -> None:
        self._ports = ports
        self._codec = _OpaqueCodec(secret)

    @staticmethod
    def _mark_write_started(context: CoreBrokerInvocationContext) -> None:
        if context.mark_write_started is not None:
            context.mark_write_started()

    def _authorize_capability(
        self,
        descriptor: Mapping[str, Any],
    ) -> None:
        authorizer = self._ports.authorize_capability
        if authorizer is not None:
            authorizer(descriptor, "ronghui_problem")

    def sheet_read(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        roles = {
            _SELF_SOURCE_ROLE
            if context.tool_name == _SELF_TOOL
            else _SPLIT_SOURCE_ROLE
        }
        _require_context(
            context,
            operation="network.request",
            action="feishu.sheet.read_rows",
            roles=roles,
            tools={_SELF_TOOL, _SPLIT_TOOL},
        )
        values = _strict(arguments, {"end_column", "max_rows"})
        if values.get("end_column") != "S":
            raise _error("sheet end column is invalid", "BROKER_ARGUMENT_INVALID")
        signed_maximum = (
            _SELF_MAX_ROWS if context.tool_name == _SELF_TOOL else _SPLIT_MAX_ROWS
        )
        maximum = _integer(
            values.get("max_rows"),
            "max_rows",
            minimum=1,
            maximum=signed_maximum,
        )
        if maximum != signed_maximum:
            raise _error("sheet row bound changed", "BROKER_ARGUMENT_INVALID")
        role = next(iter(roles))
        resource_id = _resource_id(context, role)
        raw = self._ports.sheet_rows_read(resource_id, "S", maximum)
        if not isinstance(raw, Mapping) or raw.get("complete") is not True:
            raise _error("sheet read is incomplete", "BROKER_SOURCE_INVALID")
        rows = _rows(raw.get("rows"), maximum=maximum)
        proof = {
            "complete": True,
            "row_count": len(rows),
            "snapshot_sha256": hashlib.sha256(canonical_json_bytes(rows)).hexdigest(),
        }
        return {
            "complete": True,
            "rows": rows,
            "evidence_ref": self._codec.evidence(context, "sheet-read", proof),
        }

    def problem_query(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        roles = (
            {_PRIMARY_ACCOUNT_ROLE, _DAXIANG_ACCOUNT_ROLE}
            if context.tool_name == _SELF_TOOL
            else {_PRIMARY_ACCOUNT_ROLE}
        )
        _require_context(
            context,
            operation="browser.invoke",
            action="ronghui.problem.query",
            roles=roles,
            tools={_SELF_TOOL, _SPLIT_TOOL},
        )
        descriptor = _one_account(context, self._ports)
        plan = _problem_plan_from_query(context, arguments)
        raw = self._ports.problem_action(descriptor, "query", plan)
        if not isinstance(raw, Mapping) or raw.get("ready") is not True:
            raise _error("Ronghui problem preflight failed", "BROKER_SOURCE_INVALID")
        existing_raw = raw.get("existing")
        existing = (
            _problem_identity(existing_raw, plan)
            if isinstance(existing_raw, Mapping)
            else None
        )
        precondition_ref = self._codec.encode(context, "problem-create", plan)
        proof = {
            "bill_code": plan["bill_code"],
            "existing": existing is not None,
            "plan_sha256": hashlib.sha256(canonical_json_bytes(plan)).hexdigest(),
        }
        result: dict[str, Any] = {
            "bill_code": plan["bill_code"],
            "existing": existing is not None,
            "precondition_ref": precondition_ref,
            "ready": True,
            "evidence_ref": self._codec.evidence(
                context,
                "problem-query",
                proof,
            ),
        }
        if existing is not None:
            result.update(
                external_id=existing["external_id"],
                registered_at=existing["registered_at"],
            )
        return result

    def problem_create(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        roles = (
            {_PRIMARY_ACCOUNT_ROLE, _DAXIANG_ACCOUNT_ROLE}
            if context.tool_name == _SELF_TOOL
            else {_PRIMARY_ACCOUNT_ROLE}
        )
        _require_context(
            context,
            operation="browser.invoke",
            action="ronghui.problem.create",
            roles=roles,
            tools={_SELF_TOOL, _SPLIT_TOOL},
        )
        values = _strict(
            arguments,
            {
                "bill_code",
                "precondition_ref",
                "problem_cause",
                "problem_owner_type",
                "problem_type",
                "update_postpone_days",
            },
        )
        descriptor = _one_account(context, self._ports)
        plan = self._codec.decode(
            context,
            "problem-create",
            values.get("precondition_ref"),
        )
        bill_code = _waybill(values.get("bill_code"))
        problem_type = _text(values.get("problem_type"), "problem_type", maximum=64)
        owner = _text(
            values.get("problem_owner_type"),
            "problem_owner_type",
            maximum=64,
        )
        cause = _text(values.get("problem_cause"), "problem_cause", maximum=2_000)
        update_postpone = values.get("update_postpone_days")
        if not isinstance(update_postpone, bool):
            raise _error("update_postpone_days is invalid", "BROKER_ARGUMENT_INVALID")
        observed = {
            "bill_code": bill_code,
            "problem_cause_sha256": hashlib.sha256(cause.encode("utf-8")).hexdigest(),
            "problem_owner_type": owner,
            "problem_type": problem_type,
            "update_postpone_days": update_postpone,
        }
        if observed != plan:
            raise _error("problem write plan changed", "BROKER_CURSOR_INVALID")
        self._authorize_capability(descriptor)
        self._mark_write_started(context)
        raw = self._ports.problem_action(
            descriptor,
            "create",
            {**observed, "problem_cause": cause},
        )
        if (
            not isinstance(raw, Mapping)
            or raw.get("saved") is not True
            or raw.get("verified") is not True
            or not isinstance(raw.get("postpone_updated"), bool)
        ):
            raise _error(
                "Ronghui problem write lacks authoritative readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        identity = _problem_identity(raw, plan)
        proof = {
            "bill_code": bill_code,
            "external_id_sha256": hashlib.sha256(
                identity["external_id"].encode("utf-8")
            ).hexdigest(),
            "postpone_updated": raw["postpone_updated"],
        }
        return {
            "bill_code": bill_code,
            "committed": True,
            "external_id": identity["external_id"],
            "postpone_updated": raw["postpone_updated"],
            "evidence_ref": self._codec.evidence(
                context,
                "problem-create",
                proof,
            ),
        }

    def problem_verify(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        roles = (
            {_PRIMARY_ACCOUNT_ROLE, _DAXIANG_ACCOUNT_ROLE}
            if context.tool_name == _SELF_TOOL
            else {_PRIMARY_ACCOUNT_ROLE}
        )
        _require_context(
            context,
            operation="browser.invoke",
            action="ronghui.problem.verify",
            roles=roles,
            tools={_SELF_TOOL, _SPLIT_TOOL},
        )
        values = _strict(
            arguments,
            {
                "bill_code",
                "external_id",
                "problem_cause_sha256",
                "problem_owner_type",
                "problem_type",
            },
        )
        descriptor = _one_account(context, self._ports)
        plan = {
            "bill_code": _waybill(values.get("bill_code")),
            "external_id": _text(
                values.get("external_id"),
                "external_id",
                maximum=256,
            ),
            "problem_cause_sha256": _sha256(
                values.get("problem_cause_sha256"),
                "problem_cause_sha256",
            ),
            "problem_owner_type": _text(
                values.get("problem_owner_type"),
                "problem_owner_type",
                maximum=64,
            ),
            "problem_type": _text(
                values.get("problem_type"),
                "problem_type",
                maximum=64,
            ),
        }
        raw = self._ports.problem_action(descriptor, "verify", plan)
        if not isinstance(raw, Mapping) or raw.get("confirmed") is not True:
            raise _error(
                "Ronghui problem write could not be confirmed",
                "WRITE_OUTCOME_UNKNOWN",
            )
        identity = _problem_identity(raw, plan)
        if identity["external_id"] != plan["external_id"]:
            raise _error(
                "Ronghui problem identity changed",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "bill_code": plan["bill_code"],
            "external_id_sha256": hashlib.sha256(
                plan["external_id"].encode("utf-8")
            ).hexdigest(),
            "registered_at": identity["registered_at"],
        }
        return {
            "bill_code": plan["bill_code"],
            "confirmed": True,
            "external_id": plan["external_id"],
            "problem_cause_sha256": plan["problem_cause_sha256"],
            "problem_owner_type": plan["problem_owner_type"],
            "problem_type": plan["problem_type"],
            "registered_at": identity["registered_at"],
            "registered_site": identity["registered_site"],
            "evidence_ref": self._codec.evidence(
                context,
                "problem-verify",
                proof,
            ),
        }

    def snapshot_read(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="projection.invoke",
            action="split_pending.snapshot.read",
            roles={_SPLIT_TARGET_ROLE},
            tools={_SPLIT_TOOL},
        )
        _resource_id(context, _SPLIT_TARGET_ROLE)
        values = _strict(arguments, {"max_records"})
        maximum = _integer(
            values.get("max_records"),
            "max_records",
            minimum=1,
            maximum=_SPLIT_MAX_ROWS,
        )
        if maximum != _SPLIT_MAX_ROWS:
            raise _error("snapshot bound changed", "BROKER_ARGUMENT_INVALID")
        raw = self._ports.snapshot_read(maximum)
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) > maximum
        ):
            raise _error("split snapshot is invalid", "BROKER_SOURCE_INVALID")
        records: list[dict[str, str]] = []
        identities: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise _error("split snapshot record is invalid", "BROKER_SOURCE_INVALID")
            record = {
                field: _optional_text(item.get(field), field, maximum=256)
                for field in _SNAPSHOT_PUBLIC_FIELDS
            }
            identity = record["tracking_number"]
            if not identity or identity in identities:
                raise _error("split snapshot identity is invalid", "BROKER_SOURCE_INVALID")
            identities.add(identity)
            records.append(record)
        proof = {
            "complete": True,
            "record_count": len(records),
            "snapshot_sha256": hashlib.sha256(
                canonical_json_bytes(records)
            ).hexdigest(),
        }
        return {
            "complete": True,
            "records": records,
            "evidence_ref": self._codec.evidence(
                context,
                "snapshot-read",
                proof,
            ),
        }

    @staticmethod
    def _snapshot_records(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > _SPLIT_MAX_ROWS:
            raise _error("split snapshot records are invalid", "BROKER_ARGUMENT_INVALID")
        records: list[dict[str, Any]] = []
        identities: set[str] = set()
        for raw in value:
            if not isinstance(raw, Mapping) or set(raw) != _SNAPSHOT_FIELDS:
                raise _error("split snapshot record is invalid", "BROKER_ARGUMENT_INVALID")
            bill_code = _waybill(raw.get("bill_code"))
            if bill_code in identities:
                raise _error("split snapshot identity is duplicated", "BROKER_ARGUMENT_INVALID")
            expected = _count(raw.get("expected_quantity"), "expected_quantity")
            arrived = _count(raw.get("arrived_quantity"), "arrived_quantity")
            pending = _count(raw.get("pending_quantity"), "pending_quantity")
            if expected <= 0 or arrived < 0 or pending <= 0 or arrived + pending != expected:
                raise _error("split snapshot counts are invalid", "BROKER_ARGUMENT_INVALID")
            problem_type = _text(raw.get("problem_type"), "problem_type", maximum=64)
            owner = _text(
                raw.get("problem_owner_type"),
                "problem_owner_type",
                maximum=64,
            )
            cause = _text(raw.get("problem_cause"), "problem_cause", maximum=2_000)
            if _SPLIT_OWNER_BY_TYPE.get(problem_type) != owner:
                raise _error("split classification is invalid", "BROKER_ARGUMENT_INVALID")
            if problem_type == "有发未到" and cause != "有发未到":
                raise _error("split cause is invalid", "BROKER_ARGUMENT_INVALID")
            if problem_type == "少货/分批" and cause != f"应到{expected}件 实际到{arrived}件":
                raise _error("split cause is invalid", "BROKER_ARGUMENT_INVALID")
            record = {
                "arrived_quantity": arrived,
                "bill_code": bill_code,
                "destination_station": _optional_text(
                    raw.get("destination_station"),
                    "destination_station",
                    maximum=256,
                ),
                "expected_quantity": expected,
                "pending_quantity": pending,
                "problem_cause": cause,
                "problem_owner_type": owner,
                "problem_type": problem_type,
                "source_row_no": _integer(
                    raw.get("source_row_no"),
                    "source_row_no",
                    minimum=2,
                    maximum=_SPLIT_MAX_ROWS,
                ),
            }
            identities.add(bill_code)
            records.append(record)
        return records

    def snapshot_replace(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="projection.invoke",
            action="split_pending.snapshot.replace",
            roles={_SPLIT_TARGET_ROLE},
            tools={_SPLIT_TOOL},
        )
        _resource_id(context, _SPLIT_TARGET_ROLE)
        values = _strict(arguments, {"records"})
        records = self._snapshot_records(values.get("records"))
        self._mark_write_started(context)
        raw = self._ports.snapshot_replace(records)
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
            or raw.get("record_count") != len(records)
        ):
            raise _error(
                "split snapshot replacement lacks exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "record_count": len(records),
            "snapshot_sha256": hashlib.sha256(
                canonical_json_bytes(records)
            ).hexdigest(),
        }
        return {
            "committed": True,
            "record_count": len(records),
            "evidence_ref": self._codec.evidence(
                context,
                "snapshot-replace",
                proof,
            ),
        }

    def sheet_replace(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="network.request",
            action="feishu.sheet.replace_rows",
            roles={_SPLIT_TARGET_ROLE},
            tools={_SPLIT_TOOL},
        )
        resource_id = _resource_id(context, _SPLIT_TARGET_ROLE)
        values = _strict(arguments, {"rows"})
        rows = _rows(values.get("rows"), maximum=_SPLIT_MAX_ROWS + 1)
        if not rows or tuple(rows[0]) != _TARGET_HEADERS:
            raise _error("split target header changed", "BROKER_ARGUMENT_INVALID")
        if any(len(row) != _MAX_COLUMNS for row in rows):
            raise _error("split target row width changed", "BROKER_ARGUMENT_INVALID")
        self._mark_write_started(context)
        raw = self._ports.sheet_rows_replace(resource_id, rows)
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
            or raw.get("written") != len(rows)
        ):
            raise _error(
                "split target sheet lacks exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "row_count": len(rows),
            "snapshot_sha256": hashlib.sha256(canonical_json_bytes(rows)).hexdigest(),
        }
        return {
            "committed": True,
            "written": len(rows),
            "evidence_ref": self._codec.evidence(
                context,
                "sheet-replace",
                proof,
            ),
        }

    def result_upsert(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="projection.invoke",
            action="split_pending.result.upsert",
            roles={_SPLIT_TARGET_ROLE},
            tools={_SPLIT_TOOL},
        )
        _resource_id(context, _SPLIT_TARGET_ROLE)
        values = _strict(
            arguments,
            {
                "bill_code",
                "complaint_status",
                "problem_item_status",
                "problem_type",
            },
        )
        result = {
            "bill_code": _waybill(values.get("bill_code")),
            "complaint_status": _text(
                values.get("complaint_status"),
                "complaint_status",
                maximum=32,
            ),
            "problem_item_status": _text(
                values.get("problem_item_status"),
                "problem_item_status",
                maximum=32,
            ),
            "problem_type": _text(
                values.get("problem_type"),
                "problem_type",
                maximum=64,
            ),
        }
        if result["problem_item_status"] != "success":
            raise _error("problem result is not successful", "BROKER_ARGUMENT_INVALID")
        if (
            result["problem_type"] not in _SPLIT_OWNER_BY_TYPE
            or result["complaint_status"] != "not_applicable"
        ):
            raise _error("problem result classification is invalid", "BROKER_ARGUMENT_INVALID")
        self._mark_write_started(context)
        raw = self._ports.result_upsert(result)
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
        ):
            raise _error(
                "split result upsert lacks exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        return {
            "committed": True,
            "evidence_ref": self._codec.evidence(
                context,
                "result-upsert",
                result,
            ),
        }

    def problem_event_upsert(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="ledger.invoke",
            action="daily_sign.problem_event.upsert",
            roles={_PRIMARY_ACCOUNT_ROLE},
            tools={_SPLIT_TOOL},
        )
        values = _strict(
            arguments,
            {
                "bill_code",
                "external_id",
                "problem_type",
                "registered_at",
                "registered_site",
            },
        )
        descriptor = _one_account(context, self._ports)
        event = {
            "bill_code": _waybill(values.get("bill_code")),
            "external_id": _text(
                values.get("external_id"),
                "external_id",
                maximum=256,
            ),
            "problem_type": _text(
                values.get("problem_type"),
                "problem_type",
                maximum=64,
            ),
            "registered_at": _text(
                values.get("registered_at"),
                "registered_at",
                maximum=64,
            ),
            "registered_site": _optional_text(
                values.get("registered_site"),
                "registered_site",
                maximum=256,
            ),
        }
        if event["problem_type"] not in _SPLIT_OWNER_BY_TYPE:
            raise _error("problem event type is invalid", "BROKER_ARGUMENT_INVALID")
        self._mark_write_started(context)
        raw = self._ports.problem_event_upsert(descriptor, event)
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
        ):
            raise _error(
                "problem event upsert lacks exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            **event,
            "external_id": hashlib.sha256(
                event["external_id"].encode("utf-8")
            ).hexdigest(),
        }
        return {
            "committed": True,
            "evidence_ref": self._codec.evidence(
                context,
                "problem-event-upsert",
                proof,
            ),
        }

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        return {
            ("network.request", "feishu.sheet.read_rows"): self.sheet_read,
            ("network.request", "feishu.sheet.replace_rows"): self.sheet_replace,
            ("browser.invoke", "ronghui.problem.query"): self.problem_query,
            ("browser.invoke", "ronghui.problem.create"): self.problem_create,
            ("browser.invoke", "ronghui.problem.verify"): self.problem_verify,
            ("projection.invoke", "split_pending.snapshot.read"): self.snapshot_read,
            (
                "projection.invoke",
                "split_pending.snapshot.replace",
            ): self.snapshot_replace,
            ("projection.invoke", "split_pending.result.upsert"): self.result_upsert,
            (
                "ledger.invoke",
                "daily_sign.problem_event.upsert",
            ): self.problem_event_upsert,
        }


def build_problem_handler_map(
    ports: ProblemHandlerPorts,
    *,
    cursor_secret: bytes,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    return _ProblemHandlers(ports, secret=cursor_secret).handler_map()


__all__ = [
    "MARKED_WRITE_ACTION_KEYS",
    "ProblemHandlerPorts",
    "build_problem_handler_map",
]
