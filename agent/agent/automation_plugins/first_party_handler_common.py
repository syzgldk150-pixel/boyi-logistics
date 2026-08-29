"""Shared closed validators and opaque evidence helpers for first-party handlers.

The public handler facade stays in :mod:`first_party_handlers`.  This module
contains only deterministic contract validation and broker-owned codec logic,
so the action-specific handler class can remain reviewable without duplicating
security-sensitive validation rules.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import zlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence

from agent.automation_plugins.core_adapter import CoreBrokerInvocationContext
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes


AccountDescriptorPort = Callable[[str], Mapping[str, Any]]


class _AccountDescriptorPorts(Protocol):
    describe_account: AccountDescriptorPort


_CUSTOMER_PUBLIC_FIELDS = (
    "platform",
    "source_direction",
    "external_id",
    "waybill_no",
    "status",
    "problem_type",
    "problem_text",
    "reply_text",
    "created_at",
    "registered_at",
    "registration_saved_at",
    "registered_site",
    "updated_at",
    "resolved",
    "resolution_reason",
)
_SENSITIVE_KEY_MARKERS = (
    "password",
    "cookie",
    "credential",
    "secret",
    "token",
    "session",
    "authorization",
)
_MAX_RECORDS = 20_000
_CUSTOMER_IDENTITY_DOMAIN = "boyi.customer-problem.identity.v1"


def customer_problem_identity(
    *,
    account_id: str,
    platform: str,
    external_id: str,
) -> str:
    """Return a stable pseudonymous identity without exposing ``account_id``.

    The digest deliberately omits plugin version, generation, automation
    instance and process-local broker secrets.  A persisted work item must
    survive hot replacement and Agent restart.  The control plane can resolve
    the pseudonym by recomputing it only for the account IDs in the trusted
    generation side channel; plugin JSON never receives those IDs.
    """

    material = canonical_json_bytes(
        {
            "domain": _CUSTOMER_IDENTITY_DOMAIN,
            "account_id": _text(account_id, "account_id", maximum=128),
            "platform": _text(platform, "platform", maximum=32).lower(),
            "external_id": _text(external_id, "external_id", maximum=256),
        }
    )
    return f"problem:v1:{hashlib.sha256(material).hexdigest()}"


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _strict_arguments(arguments: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    values = dict(arguments)
    unknown = set(values) - allowed
    if unknown:
        raise _error("broker primitive received undeclared arguments", "BROKER_ARGUMENT_INVALID")
    return values


def _text(value: object, label: str, *, maximum: int = 1024) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return result


def _clock_site(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "sitecode",
        "sitefbcode",
        "sitename",
        "sitefbname",
    }:
        raise _error("clock site is invalid", "BROKER_ARGUMENT_INVALID")
    return {
        "sitecode": _text(value.get("sitecode"), "sitecode", maximum=64),
        "sitefbcode": _text(value.get("sitefbcode"), "sitefbcode", maximum=64),
        "sitename": _text(value.get("sitename"), "sitename", maximum=100),
        "sitefbname": _text(value.get("sitefbname"), "sitefbname", maximum=100),
    }


def _page_size(value: object) -> int:
    if isinstance(value, bool):
        raise _error("page_size is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _error("page_size is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if not 1 <= result <= 200:
        raise _error("page_size is outside the closed limit", "BROKER_ARGUMENT_INVALID")
    return result


def _business_date(value: object) -> str:
    text = _text(value, "target_date", maximum=10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise _error("target_date is invalid", "BROKER_ARGUMENT_INVALID") from exc


def _optional_finite_number(value: object, label: str) -> int | float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if not number.is_finite():
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _one_account(context: CoreBrokerInvocationContext) -> str:
    if len(context.account_ids) != 1:
        raise _error("broker action requires exactly one bound account", "BROKER_ROLE_INVALID")
    return context.account_ids[0]


def _one_role_account(context: CoreBrokerInvocationContext, role: str) -> str:
    values = context.account_bindings.get(role)
    if not isinstance(values, tuple) or len(values) != 1 or not values[0]:
        raise _error(
            "broker action requires one exact account for every signed role",
            "BROKER_ROLE_INVALID",
        )
    return values[0]


def _require_context(
    context: CoreBrokerInvocationContext,
    *,
    tool_name: str,
    role: str,
) -> None:
    if context.tool_name != tool_name or context.role != role:
        raise _error("broker action is not valid for this signed tool role", "BROKER_CONTEXT_INVALID")


def _account_descriptor(
    ports: _AccountDescriptorPorts,
    account_id: str,
    *,
    systems: set[str],
) -> dict[str, Any]:
    raw = ports.describe_account(account_id)
    if not isinstance(raw, Mapping):
        raise _error("account adapter returned an invalid descriptor", "BROKER_ACCOUNT_INVALID")
    descriptor = dict(raw)
    if str(descriptor.get("account_id") or "") != account_id:
        raise _error("account adapter changed the exact binding", "BROKER_ACCOUNT_MISMATCH")
    if str(descriptor.get("system") or "").strip().lower() not in systems:
        raise _error("account system does not match the primitive", "BROKER_ACCOUNT_SYSTEM_MISMATCH")
    return descriptor


def _daily_sign_account_bindings(
    ports: _AccountDescriptorPorts,
    context: CoreBrokerInvocationContext,
) -> dict[str, str]:
    r13_account_id = _one_role_account(context, "r13_account_id")
    account_id = _one_role_account(context, "account_id")
    _account_descriptor(ports, r13_account_id, systems={"r13"})
    _account_descriptor(ports, account_id, systems={"ronghui"})
    return {
        "r13_account_id": r13_account_id,
        "account_id": account_id,
    }


class _OpaqueCodec:
    _COMPRESSED_PREFIX = b"Z1\0"

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("first-party broker cursor secret must contain at least 32 bytes")
        self._secret = secret

    @staticmethod
    def _context_material(context: CoreBrokerInvocationContext, purpose: str) -> bytes:
        return canonical_json_bytes(
            {
                "automation_id": context.automation_id,
                "plugin_version": context.plugin_version,
                "tool_name": context.tool_name,
                "role": context.role,
                "purpose": purpose,
                "account_ids": list(context.account_ids),
                "resource_id": context.resource_id,
            }
        )

    def encode(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        payload: Mapping[str, Any],
    ) -> str:
        body = canonical_json_bytes(dict(payload))
        compressed = self._COMPRESSED_PREFIX + zlib.compress(body, level=9)
        if len(compressed) < len(body):
            body = compressed
        signature = hmac.new(
            self._secret,
            self._context_material(context, purpose) + b"\0" + body,
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode("ascii")

    def decode(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        token: object,
    ) -> dict[str, Any]:
        value = _text(token, "cursor", maximum=2048)
        try:
            padding = "=" * (-len(value) % 4)
            decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
            if len(decoded) <= 32:
                raise ValueError("short token")
            body, supplied = decoded[:-32], decoded[-32:]
            expected = hmac.new(
                self._secret,
                self._context_material(context, purpose) + b"\0" + body,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("bad signature")
            if body.startswith(self._COMPRESSED_PREFIX):
                body = zlib.decompress(body[len(self._COMPRESSED_PREFIX) :])
            payload = json.loads(body.decode("utf-8"))
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            zlib.error,
        ) as exc:
            raise _error("broker cursor is invalid", "BROKER_CURSOR_INVALID") from exc
        if not isinstance(payload, dict):
            raise _error("broker cursor is invalid", "BROKER_CURSOR_INVALID")
        return payload

    def identity(
        self,
        context: CoreBrokerInvocationContext,
        *,
        account_id: str,
        platform: str,
        external_id: str,
    ) -> str:
        del context
        return customer_problem_identity(
            account_id=account_id,
            platform=platform,
            external_id=external_id,
        )

    def evidence(
        self,
        context: CoreBrokerInvocationContext,
        label: str,
        payload: Mapping[str, Any],
    ) -> str:
        digest = hmac.new(
            self._secret,
            self._context_material(context, label) + b"\0" + canonical_json_bytes(dict(payload)),
            hashlib.sha256,
        ).hexdigest()
        return f"broker-evidence:{label}:{digest}"


def _source_directions(platform: str, requested: str) -> tuple[str, ...]:
    requested_values = ("received", "published") if requested == "both" else (requested,)
    values: list[str] = []
    for item in requested_values:
        if platform == "ronghui":
            values.append("received" if item == "received" else "registered")
        elif platform == "yunda":
            values.append("query" if item == "received" else "published")
        else:
            raise _error("customer account system is unsupported", "BROKER_ACCOUNT_SYSTEM_MISMATCH")
    return tuple(values)


def _page_state(
    codec: _OpaqueCodec,
    context: CoreBrokerInvocationContext,
    purpose: str,
    cursor: object,
    *,
    initial: Mapping[str, Any],
) -> dict[str, Any]:
    if cursor in (None, ""):
        return dict(initial)
    state = codec.decode(context, purpose, cursor)
    if state.get("v") != 1:
        raise _error("broker cursor version is invalid", "BROKER_CURSOR_INVALID")
    return state


def _declared_page_result(
    raw: Mapping[str, Any],
    *,
    expected_total: int | None,
    cumulative: int,
    page_size: int,
) -> tuple[list[Any], int | None, int, bool]:
    items = raw.get("items")
    returned = raw.get("returned")
    if not isinstance(items, list) or isinstance(returned, bool) or not isinstance(returned, int):
        raise _error("source page response is invalid", "BROKER_SOURCE_INVALID")
    if returned < 0 or returned > page_size or returned != len(items):
        raise _error("source page count is invalid", "BROKER_SOURCE_INVALID")
    authoritative = raw.get("total_authoritative") is True
    raw_total = raw.get("total")
    total: int | None = None
    if authoritative:
        if isinstance(raw_total, bool) or not isinstance(raw_total, int) or raw_total < 0:
            raise _error("source total is invalid", "BROKER_SOURCE_INVALID")
        total = raw_total
        if expected_total is not None and expected_total != total:
            raise _error("source total changed during pagination", "BROKER_SOURCE_CHANGED")
    elif expected_total is not None:
        raise _error("source lost its authoritative total", "BROKER_SOURCE_CHANGED")
    next_cumulative = cumulative + returned
    if total is not None:
        if next_cumulative > total:
            raise _error("source returned more rows than its declared total", "BROKER_SOURCE_INVALID")
        if returned == 0 and next_cumulative < total:
            raise _error("source pagination ended before its declared total", "BROKER_PAGINATION_INCOMPLETE")
        complete = next_cumulative == total
    else:
        complete = returned < page_size
    return items, total, next_cumulative, complete


def _customer_public_item(
    codec: _OpaqueCodec,
    context: CoreBrokerInvocationContext,
    row: Mapping[str, Any],
    *,
    account_id: str,
    platform: str,
    source_direction: str,
) -> dict[str, Any]:
    external_id = _text(row.get("external_id"), "customer external_id", maximum=256)
    observed_platform = str(row.get("platform") or "").strip().lower()
    observed_direction = str(row.get("source_direction") or "").strip().lower()
    if observed_platform != platform or observed_direction != source_direction:
        raise _error("customer source changed platform or direction", "BROKER_SOURCE_IDENTITY_MISMATCH")
    result = {
        field: row.get(field)
        for field in _CUSTOMER_PUBLIC_FIELDS
        if field in row and field not in {"platform", "source_direction", "external_id"}
    }
    result.update(
        {
            "platform": platform,
            "source_direction": source_direction,
            "external_id": external_id,
            "dedupe_key": codec.identity(
                context,
                account_id=account_id,
                platform=platform,
                external_id=external_id,
            ),
        }
    )
    return result


def _scrub_business_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_")
            if normalized in {"account_id", "account_ids", "account_label"} or normalized.endswith(
                ("_account_id", "_account_ids")
            ):
                continue
            if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
                continue
            output[key] = _scrub_business_value(child)
        return output
    if isinstance(value, list):
        return [_scrub_business_value(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_business_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _contains_broker_owned_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            normalized = str(raw_key).strip().lower().replace("-", "_")
            if normalized in {"account_id", "account_ids", "account_label"} or normalized.endswith(
                ("_account_id", "_account_ids")
            ):
                return True
            if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
                return True
            if _contains_broker_owned_material(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_broker_owned_material(item) for item in value)
    return False


def _encode_daily_sign_result(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "status",
        "data",
        "meta",
        "warnings",
        "error",
    }:
        raise _error(
            "daily-sign authoritative result schema is invalid",
            "BROKER_SOURCE_INVALID",
        )
    status = raw.get("status")
    data = raw.get("data")
    meta = raw.get("meta")
    warnings = raw.get("warnings")
    error = raw.get("error")
    if (
        status not in {"SUCCESS", "FAILED"}
        or not isinstance(data, Mapping)
        or not isinstance(meta, Mapping)
        or not isinstance(warnings, list)
        or any(not isinstance(item, str) for item in warnings)
        or (error is not None and not isinstance(error, Mapping))
        or (status == "SUCCESS" and error is not None)
    ):
        raise _error(
            "daily-sign authoritative result schema is invalid",
            "BROKER_SOURCE_INVALID",
        )
    encoded_meta = dict(meta)
    if encoded_meta.pop("account_id", None) != "multi_account":
        raise _error(
            "daily-sign authoritative account scope is invalid",
            "BROKER_SOURCE_INVALID",
        )
    encoded_meta["account_scope"] = "multi_account"
    encoded = {
        "status": status,
        "data": dict(data),
        "meta": encoded_meta,
        "warnings": list(warnings),
        "error": dict(error) if isinstance(error, Mapping) else None,
    }
    if _contains_broker_owned_material(encoded):
        raise _error(
            "daily-sign authoritative result exposed broker-owned material",
            "BROKER_SOURCE_INVALID",
        )
    if status == "SUCCESS":
        evidence_refs = encoded_meta.get("evidence_refs")
        postconditions = encoded_meta.get("postconditions")
        postcondition_evidence = encoded_meta.get("postcondition_evidence")
        terminal_proof = (
            postcondition_evidence.get("0")
            if isinstance(postcondition_evidence, Mapping)
            else None
        )
        terminal_details = (
            terminal_proof.get("details")
            if isinstance(terminal_proof, Mapping)
            else None
        )
        if (
            encoded_meta.get("pagination_complete") is not True
            or not isinstance(evidence_refs, list)
            or not evidence_refs
            or any(not isinstance(item, str) or not item for item in evidence_refs)
            or not isinstance(postconditions, Mapping)
            or postconditions.get("0") is not True
        ):
            raise _error(
                "daily-sign authoritative success evidence is incomplete",
                "BROKER_SOURCE_INVALID",
            )
        if (
            not isinstance(terminal_proof, Mapping)
            or terminal_proof.get("verified") is not True
            or terminal_proof.get("condition")
            != "authoritative_snapshot_committed"
            or not isinstance(terminal_details, Mapping)
        ):
            raise _error(
                "daily-sign authoritative terminal readback is incomplete",
                "WRITE_OUTCOME_UNKNOWN",
            )
    return encoded


def _strict_record_list(
    value: object,
    *,
    fields: Sequence[str],
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > _MAX_RECORDS:
        raise _error(f"{label} records are invalid", "BROKER_ARGUMENT_INVALID")
    required = set(fields)
    output: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise _error(f"{label} record schema is invalid", "BROKER_ARGUMENT_INVALID")
        output.append(dict(raw))
    return output


def _nonnegative_int(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if result < 0 or result > maximum:
        raise _error(f"{label} is outside the closed limit", "BROKER_ARGUMENT_INVALID")
    return result


def _committed_result_count(raw: Mapping[str, Any], expected: int) -> None:
    observed = raw.get(
        "record_count",
        raw.get("rows", raw.get("replaced", raw.get("upserted"))),
    )
    if isinstance(observed, bool) or not isinstance(observed, int) or observed != expected:
        raise _error("projection commit count is invalid", "BROKER_PROJECTION_MISMATCH")
