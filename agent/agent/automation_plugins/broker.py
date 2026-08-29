"""Single-use local broker for credential-free core adapter access."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from shared.redaction import redact_text


_OPERATIONS = frozenset(
    {
        "browser.invoke",
        "office.invoke",
        "file.read",
        "file.write",
        "network.request",
        "projection.invoke",
        "ledger.invoke",
    }
)
_SENSITIVE_KEYS = ("password", "cookie", "credential", "secret", "token", "session")
_BROKER_FRAME_PREFIX = b"BOYI-BROKER-V2 "
_MAX_BROKER_LEGACY_REQUEST_BYTES = 10 * 1024 * 1024
_MAX_BROKER_COMPRESSED_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_BROKER_REQUEST_BYTES = 64 * 1024 * 1024
_RECOVERABLE_WRITE_ACTIONS = frozenset(
    {
        ("arrive_list", "projection.invoke", "waybill.snapshot.replace"),
        ("arrive_list", "projection.invoke", "arrival.forecast_snapshot.replace"),
        ("arrive_list", "network.request", "feishu.sheet.replace"),
        ("arrival_stats", "projection.invoke", "scan.snapshot.replace"),
        ("arrival_stats", "projection.invoke", "scan.snapshot.cleanup"),
        ("arrival_stats", "projection.invoke", "waybill.snapshot.replace"),
        ("arrival_stats", "projection.invoke", "arrival.snapshot.replace"),
        ("arrival_stats", "projection.invoke", "split_pending.snapshot.refresh"),
        ("arrival_stats", "network.request", "feishu.sheet.replace"),
        ("arrival_stats", "network.request", "feishu.sheet.add"),
        ("daily_sign", "ledger.invoke", "daily_sign.authoritative_sync"),
        ("delivery_status", "network.request", "feishu.bitable.write_records"),
        ("delivery_status", "projection.invoke", "waybill.delivery_status.update"),
        ("finance_startup_catchup", "ledger.invoke", "finance.batch.acquire"),
        ("finance_startup_catchup", "ledger.invoke", "finance.source_snapshot.write"),
        ("finance_startup_catchup", "ledger.invoke", "finance.projection.commit"),
    }
)
# Recovery eligibility belongs to the signed package identity, never to a
# mutable/reusable automation instance id.  The public compatibility constant
# above intentionally remains project-oriented for management contracts; this
# internal close-set controls the strict pre-write extractor.
_RECOVERABLE_WRITE_PLUGIN_ACTIONS = frozenset(
    {
        ("sync_arrive_list", "projection.invoke", "waybill.snapshot.replace"),
        ("sync_arrive_list", "projection.invoke", "arrival.forecast_snapshot.replace"),
        ("sync_arrive_list", "network.request", "feishu.sheet.replace"),
        ("sync_arrival_stats", "projection.invoke", "scan.snapshot.replace"),
        ("sync_arrival_stats", "projection.invoke", "scan.snapshot.cleanup"),
        ("sync_arrival_stats", "projection.invoke", "waybill.snapshot.replace"),
        ("sync_arrival_stats", "projection.invoke", "arrival.snapshot.replace"),
        ("sync_arrival_stats", "projection.invoke", "split_pending.snapshot.refresh"),
        ("sync_arrival_stats", "network.request", "feishu.sheet.replace"),
        ("sync_arrival_stats", "network.request", "feishu.sheet.add"),
        ("sync_daily_should_sign", "ledger.invoke", "daily_sign.authoritative_sync"),
        ("sync_delivery_status", "network.request", "feishu.bitable.write_records"),
        ("sync_delivery_status", "projection.invoke", "waybill.delivery_status.update"),
        ("sync_finance_bills", "ledger.invoke", "finance.batch.acquire"),
        ("sync_finance_bills", "ledger.invoke", "finance.source_snapshot.write"),
        ("sync_finance_bills", "ledger.invoke", "finance.projection.commit"),
    }
)
_TARGET_REF_FIELDS = frozenset(
    {
        "schema", "automation_id", "operation", "action", "role_sha256",
        "binding_sha256", "request_sha256", "business_date_sha256",
        "batch_sha256", "run_sha256", "idempotency_key_sha256",
        "record_count", "content_sha256",
    }
)
_DATE_FIELDS = ("target_date", "business_date", "date")
_BATCH_FIELDS = ("batch_id", "batch_ref")
_RUN_FIELDS = ("run_ref", "run_id")
_IDEMPOTENCY_FIELDS = ("idempotency_key", "contract_sha256")
_RECORD_COLLECTION_FIELDS = (
    "records", "values", "bill_codes", "outcomes", "transactions", "items",
)
# This field is an internal, core-handler-to-broker contract. A signed write
# normally has to cross the durable write-attempt marker. The sole exception
# is a verified empty-input no-op, where crossing that boundary would itself
# create a false unknown-write recovery obligation. It is removed before the
# response reaches plugin payload code.
VERIFIED_WRITE_NOOP_FIELD = "_broker_verified_write_noop_v1"
_VERIFIED_WRITE_NOOP_CONTRACTS = {
    (
        "sync_yunda_dispatch_forecast",
        "network.request",
        "feishu.bitable.append_yunda_dispatch_forecast",
    ): ("records", frozenset({"records", "target_date", "ensure_fields"})),
    (
        "sync_site_send_list",
        "network.request",
        "feishu.bitable.replace_snapshot",
    ): ("records", frozenset({"records", "target_date"})),
    (
        "sync_site_send_list",
        "network.request",
        "feishu.sheet.replace",
    ): ("values", frozenset({"values", "target_date"})),
}


def recoverable_write_action_contracts() -> frozenset[tuple[str, str, str]]:
    """Expose the Broker close-set for signed-manifest contract tests."""

    return _RECOVERABLE_WRITE_ACTIONS


def _is_verified_write_noop(
    result: Mapping[str, Any],
    *,
    grant: BrokerGrant,
    request: Mapping[str, Any],
) -> bool:
    """Accept only the narrow closed result shape for a write no-op."""

    contract = _VERIFIED_WRITE_NOOP_CONTRACTS.get(
        (grant.tool_name, request.get("operation"), request.get("action"))
    )
    arguments = request.get("arguments")
    if contract is None or not isinstance(arguments, Mapping):
        return False
    collection_key, required_keys = contract
    original_collection = arguments.get(collection_key)
    return (
        set(arguments) == required_keys
        and type(original_collection) is list
        and not original_collection
        and result.get(VERIFIED_WRITE_NOOP_FIELD) is True
        and result.get("committed") is True
        and result.get("verified") is True
        and type(result.get("record_count")) is int
        and result.get("record_count") == 0
        and type(result.get("readback_count")) is int
        and result.get("readback_count") == 0
        and type(result.get("written")) is int
        and result.get("written") == 0
        and isinstance(result.get("readback_sha256"), str)
        and len(str(result["readback_sha256"])) == 64
        and all(character in "0123456789abcdef" for character in str(result["readback_sha256"]).lower())
        and isinstance(result.get("evidence_ref"), str)
        and bool(str(result["evidence_ref"]).strip())
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _first_text(arguments: Mapping[str, Any], fields: Sequence[str], *, label: str) -> str:
    values: list[str] = []
    for field in fields:
        if field not in arguments:
            continue
        value = arguments[field]
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            raise PluginExecutionError(
                f"write locator {label} is invalid",
                code="WRITE_ATTEMPT_LOCATOR_INVALID",
            )
        values.append(str(value).strip())
    if len(set(values)) > 1:
        raise PluginExecutionError(
            f"write locator {label} is ambiguous",
            code="WRITE_ATTEMPT_LOCATOR_INVALID",
        )
    return values[0] if values else ""


def _record_count(arguments: Mapping[str, Any]) -> int:
    counts: list[int] = []
    for field in _RECORD_COLLECTION_FIELDS:
        if field not in arguments:
            continue
        value = arguments[field]
        if not isinstance(value, list):
            raise PluginExecutionError(
                "write locator record collection is invalid",
                code="WRITE_ATTEMPT_LOCATOR_INVALID",
            )
        counts.append(len(value))
    if len(counts) > 1 and len(set(counts)) > 1:
        # A multi-collection write has one authoritative primary collection;
        # its action-specific validation below decides whether it is allowed.
        return max(counts)
    return counts[0] if counts else 0


def _generic_record_count(arguments: Mapping[str, Any]) -> int:
    """Count only plainly-shaped collections without rejecting old writes."""

    return max(
        (len(value) for field, value in arguments.items()
         if field in _RECORD_COLLECTION_FIELDS and isinstance(value, list)),
        default=0,
    )


def _optional_locator_text(arguments: Mapping[str, Any], fields: Sequence[str]) -> str:
    """Return an optional scalar only when it is unambiguous and safe to hash."""

    values = {
        str(arguments[field]).strip()
        for field in fields
        if field in arguments
        and not isinstance(arguments[field], bool)
        and isinstance(arguments[field], (str, int))
        and str(arguments[field]).strip()
    }
    return next(iter(values)) if len(values) == 1 else ""


def _require_locator_arguments(action: str, arguments: Mapping[str, Any]) -> None:
    """Validate only the closed public shape needed to locate a write."""

    list_actions = {
        "waybill.snapshot.replace", "arrival.forecast_snapshot.replace",
        "scan.snapshot.replace", "arrival.snapshot.replace",
        "split_pending.snapshot.refresh", "feishu.sheet.replace",
        "feishu.sheet.add", "feishu.bitable.write_records",
    }
    if action in list_actions:
        if not any(isinstance(arguments.get(field), list) for field in ("records", "values")):
            raise PluginExecutionError(
                "write locator requires records or values",
                code="WRITE_ATTEMPT_LOCATOR_INVALID",
            )
    elif action == "scan.snapshot.cleanup":
        value = arguments.get("retention_days")
        if type(value) is not int or value <= 0:
            raise PluginExecutionError("write locator retention is invalid", code="WRITE_ATTEMPT_LOCATOR_INVALID")
    elif action == "waybill.delivery_status.update":
        if not isinstance(arguments.get("bill_codes"), list) or not isinstance(arguments.get("status"), str):
            raise PluginExecutionError("write locator delivery update is invalid", code="WRITE_ATTEMPT_LOCATOR_INVALID")
    elif action == "finance.batch.acquire":
        if arguments.get("schema_version") != 1 or not isinstance(arguments.get("contract"), Mapping):
            raise PluginExecutionError("write locator finance batch is invalid", code="WRITE_ATTEMPT_LOCATOR_INVALID")
    elif action == "finance.source_snapshot.write":
        if type(arguments.get("batch_id")) is not int or not isinstance(arguments.get("target_date"), str):
            raise PluginExecutionError("write locator finance snapshot is invalid", code="WRITE_ATTEMPT_LOCATOR_INVALID")
    elif action == "finance.projection.commit":
        if type(arguments.get("batch_id")) is not int or not isinstance(arguments.get("outcomes"), list):
            raise PluginExecutionError("write locator finance commit is invalid", code="WRITE_ATTEMPT_LOCATOR_INVALID")
    elif action == "daily_sign.authoritative_sync":
        # The signed daily-sign payload has a closed but intentionally
        # versioned argument schema.  Its entire canonical content is hashed.
        return
    else:
        raise PluginExecutionError("write locator action is unsupported", code="WRITE_ATTEMPT_LOCATOR_INVALID")


def _extract_write_target_ref(
    *,
    automation_id: str,
    plugin_id: str | None = None,
    operation: str,
    action: str,
    role: str,
    binding: object,
    request_id: str,
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Build a payload-free locator before adapter invocation.

    Only the five explicitly recoverable signed packages use the closed,
    action-specific validation.  Every other signed write receives the same
    redacted durable receipt shape through a permissive generic locator so a
    newly protected write cannot regress existing first-party behavior.
    """

    if not isinstance(arguments, Mapping) or not isinstance(role, str) or not role.strip():
        raise PluginExecutionError("write locator input is invalid", code="WRITE_ATTEMPT_LOCATOR_INVALID")
    strict = (str(plugin_id or ""), operation, action) in _RECOVERABLE_WRITE_PLUGIN_ACTIONS
    if strict:
        _require_locator_arguments(action, arguments)
        business_date = _first_text(arguments, _DATE_FIELDS, label="business date")
        batch = _first_text(arguments, _BATCH_FIELDS, label="batch")
        run = _first_text(arguments, _RUN_FIELDS, label="run")
        idempotency = _first_text(arguments, _IDEMPOTENCY_FIELDS, label="idempotency")
        record_count = _record_count(arguments)
    else:
        business_date = _optional_locator_text(arguments, _DATE_FIELDS)
        batch = _optional_locator_text(arguments, _BATCH_FIELDS)
        run = _optional_locator_text(arguments, _RUN_FIELDS)
        idempotency = _optional_locator_text(arguments, _IDEMPOTENCY_FIELDS)
        record_count = _generic_record_count(arguments)
    content_sha256 = hashlib.sha256(canonical_json_bytes(dict(arguments))).hexdigest()
    target_ref = {
        "schema": 1,
        "automation_id": automation_id,
        "operation": operation,
        "action": action,
        "role_sha256": _sha256_text(role.strip()),
        "binding_sha256": _sha256_text(str(binding)),
        "request_sha256": _sha256_text(request_id),
        "business_date_sha256": _sha256_text(business_date) if business_date else "",
        "batch_sha256": _sha256_text(batch) if batch else "",
        "run_sha256": _sha256_text(run) if run else "",
        "idempotency_key_sha256": _sha256_text(idempotency) if idempotency else "",
        "record_count": record_count,
        "content_sha256": content_sha256,
    }
    if set(target_ref) != _TARGET_REF_FIELDS:
        raise AssertionError("write target locator schema drifted")
    return target_ref, hashlib.sha256(canonical_json_bytes(target_ref)).hexdigest()


def _is_account_identifier_key(value: object) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return normalized in {"account_id", "account_ids"} or normalized.endswith(
        ("_account_id", "_account_ids")
    )


@dataclass(frozen=True)
class BrokerGrant:
    automation_id: str
    plugin_version: str
    tool_name: str
    expires_at: datetime
    runtime_permissions: Mapping[str, object]
    account_roles: tuple[Mapping[str, object], ...]
    resource_roles: tuple[Mapping[str, object], ...]
    account_bindings: Mapping[str, object]
    resource_bindings: Mapping[str, str]
    # Direct adapter tests do not cross the Broker issuer/write boundary, so
    # they may construct a grant without durable receipt context.  Issuer
    # consume still rejects every write unless its exact context and recorder
    # are present.
    write_attempt_context: Mapping[str, object] = field(default_factory=dict)


@dataclass
class _BrokerGrantState:
    grant: BrokerGrant
    remaining_calls: int
    request_ids: set[str]
    consumed_calls: int = 0
    started_mutating_calls: int = 0
    pending_write_calls: dict[str, tuple[str, str, str, object, dict[str, Any]]] = field(
        default_factory=dict
    )


@runtime_checkable
class CoreAutomationBrokerAdapterPort(Protocol):
    async def invoke(
        self,
        *,
        grant: BrokerGrant,
        operation: str,
        action: str,
        role: str,
        binding: object,
        arguments: Mapping[str, Any],
        mark_write_started: Callable[[], None] | None = None,
    ) -> Mapping[str, Any]:
        """Revalidate exact sessions/resources and return redacted business data.

        Account-backed operations must fail ``BLOCKED_LOGIN`` when any selected
        account is missing, inactive or unauthenticated. Implementations may
        never substitute another account or return credential/session values.
        """


class LocalBrokerCapabilityIssuer:
    """Store token digests and a bounded, replay-protected invocation grant."""

    def __init__(
        self,
        socket_path: Path | str,
        *,
        write_attempt_recorder: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self._socket_path = Path(socket_path).resolve()
        if self._socket_path == self._socket_path.parent:
            raise ValueError("broker socket path is unsafe")
        self._grants: dict[str, _BrokerGrantState] = {}
        self._lock = threading.Lock()
        self._write_attempt_recorder = write_attempt_recorder

    @property
    def broker_endpoint(self) -> str:
        return f"unix://{self._socket_path}"

    @property
    def broker_socket_path(self) -> Path:
        return self._socket_path

    def issue(
        self,
        *,
        automation_id: str,
        plugin_version: str,
        tool_name: str,
        ttl_seconds: int,
        runtime_permissions: Mapping[str, object],
        account_roles: Sequence[Mapping[str, object]],
        resource_roles: Sequence[Mapping[str, object]],
        account_bindings: Mapping[str, object],
        resource_bindings: Mapping[str, str],
        write_attempt_context: Mapping[str, object] | None = None,
    ) -> str:
        ttl = max(1, min(int(ttl_seconds), 3600))
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        grant = BrokerGrant(
            automation_id=str(automation_id),
            plugin_version=str(plugin_version),
            tool_name=str(tool_name),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
            runtime_permissions=dict(runtime_permissions),
            account_roles=tuple(dict(role) for role in account_roles),
            resource_roles=tuple(dict(role) for role in resource_roles),
            account_bindings=dict(account_bindings),
            resource_bindings=dict(resource_bindings),
            write_attempt_context=dict(write_attempt_context or {}),
        )
        raw_limit = runtime_permissions.get("max_broker_calls")
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or not 0 <= raw_limit <= 1000:
            raise PluginExecutionError(
                "signed broker call limit is invalid",
                code="BROKER_CONTRACT_INVALID",
            )
        with self._lock:
            now = datetime.now(timezone.utc)
            self._grants = {
                key: state
                for key, state in self._grants.items()
                if state.grant.expires_at > now
            }
            self._grants[digest] = _BrokerGrantState(
                grant=grant,
                remaining_calls=raw_limit,
                request_ids=set(),
            )
        return token

    def revoke(self, capability: str) -> None:
        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        with self._lock:
            self._grants.pop(digest, None)

    def consumed_call_count(self, capability: str) -> int:
        """Legacy diagnostic counter; never use this to infer a write."""
        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        with self._lock:
            state = self._grants.get(digest)
            if state is None:
                raise PluginExecutionError(
                    "core broker capability observation is unavailable",
                    code="BROKER_OBSERVATION_UNAVAILABLE",
                )
            return state.consumed_calls

    def started_mutating_call_count(self, capability: str) -> int:
        """Return only signed writes that crossed the adapter invocation boundary."""

        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        with self._lock:
            state = self._grants.get(digest)
            if state is None:
                raise PluginExecutionError(
                    "core broker capability observation is unavailable",
                    code="BROKER_OBSERVATION_UNAVAILABLE",
                )
            return state.started_mutating_calls

    def consume(
        self,
        capability: str,
        *,
        request_id: str,
        operation: str,
        action: str,
        role: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> tuple[BrokerGrant, object]:
        if operation not in _OPERATIONS:
            raise PluginExecutionError("core broker operation is unsupported", code="BROKER_OPERATION_DENIED")
        try:
            normalized_request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginExecutionError(
                "core broker request_id is invalid",
                code="BROKER_REQUEST_INVALID",
            ) from exc
        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        with self._lock:
            state = self._grants.get(digest)
            if state is None or datetime.now(timezone.utc) >= state.grant.expires_at:
                self._grants.pop(digest, None)
                state = None
        if state is None:
            raise PluginExecutionError("core broker capability is invalid or expired", code="BROKER_CAPABILITY_INVALID")
        grant = state.grant
        raw_contracts = grant.runtime_permissions.get("broker_operations")
        if not isinstance(raw_contracts, list):
            raise PluginExecutionError("signed broker contract is invalid", code="BROKER_CONTRACT_INVALID")
        matches = [
            contract
            for contract in raw_contracts
            if isinstance(contract, Mapping)
            and contract.get("operation") == operation
            and contract.get("action") == action
        ]
        if len(matches) != 1:
            raise PluginExecutionError(
                "broker operation/action is not signed for this plugin",
                code="BROKER_OPERATION_DENIED",
            )
        effect = matches[0].get("effect")
        if effect not in {"read", "write"}:
            raise PluginExecutionError(
                "broker effect classification is not signed",
                code="BROKER_CONTRACT_INVALID",
            )
        allowed_roles = matches[0].get("roles")
        if not isinstance(allowed_roles, list) or role not in allowed_roles:
            raise PluginExecutionError(
                "broker role is not signed for this operation",
                code="BROKER_ROLE_DENIED",
            )
        if operation.startswith("browser.") and grant.runtime_permissions.get("browser") is not True:
            raise PluginExecutionError("browser adapter was not signed for this plugin", code="BROKER_OPERATION_DENIED")
        if operation.startswith("office.") and grant.runtime_permissions.get("office") is not True:
            raise PluginExecutionError("Office adapter was not signed for this plugin", code="BROKER_OPERATION_DENIED")
        if operation.startswith("network.") and grant.runtime_permissions.get("network") is not True:
            raise PluginExecutionError("network adapter was not signed for this plugin", code="BROKER_OPERATION_DENIED")
        account_roles = {str(item.get("role") or "") for item in grant.account_roles}
        resource_roles = {str(item.get("role") or "") for item in grant.resource_roles}
        if role in account_roles and role in resource_roles:
            raise PluginExecutionError("broker role declaration is ambiguous", code="BROKER_CONTRACT_INVALID")
        if role in account_roles:
            if role not in grant.account_bindings:
                raise PluginExecutionError("account role is unbound", code="BROKER_ROLE_UNBOUND")
            binding = grant.account_bindings[role]
        elif role in resource_roles:
            if role not in grant.resource_bindings:
                raise PluginExecutionError("resource role is unbound", code="BROKER_ROLE_UNBOUND")
            binding = grant.resource_bindings[role]
        else:
            raise PluginExecutionError("broker role is undeclared", code="BROKER_ROLE_UNBOUND")

        # Consuming a capability reserves its request id and call quota.  It is
        # deliberately *not* the started-write boundary: production adapters
        # still have to prove their handler/session/resource/exact-binding
        # checks before they may call ``mark_write_started``.
        with self._lock:
            current = self._grants.get(digest)
            if current is not state or datetime.now(timezone.utc) >= grant.expires_at:
                self._grants.pop(digest, None)
                raise PluginExecutionError("core broker capability is invalid or expired", code="BROKER_CAPABILITY_INVALID")
            if normalized_request_id in current.request_ids:
                raise PluginExecutionError(
                    "core broker request replayed",
                    code="BROKER_REQUEST_REPLAYED",
                )
            if current.remaining_calls <= 0:
                raise PluginExecutionError(
                    "core broker call limit was exhausted",
                    code="BROKER_CALL_LIMIT",
                )
            current.request_ids.add(normalized_request_id)
            current.remaining_calls -= 1
            current.consumed_calls += 1
            if effect == "write":
                current.pending_write_calls[normalized_request_id] = (
                    operation,
                    action,
                    role,
                    binding,
                    dict(arguments or {}),
                )
        return grant, binding

    def mark_write_started_hook(
        self,
        capability: str,
        *,
        request_id: str,
    ) -> Callable[[], None] | None:
        """Return the one-shot durable started-write marker for a consumed call.

        The callback is intentionally handed only to the production adapter.
        Calling it is the sole point that creates a receipt and increments the
        observable started count.
        """

        digest = hashlib.sha256(str(capability).encode("ascii", errors="ignore")).hexdigest()
        try:
            normalized_request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginExecutionError("core broker request_id is invalid", code="BROKER_REQUEST_INVALID") from exc
        with self._lock:
            state = self._grants.get(digest)
            if state is None:
                raise PluginExecutionError("core broker capability is invalid or expired", code="BROKER_CAPABILITY_INVALID")
            pending = state.pending_write_calls.get(normalized_request_id)
        if pending is None:
            return None

        phase = "ready"

        def mark_write_started() -> None:
            nonlocal phase
            with self._lock:
                if phase != "ready":
                    raise PluginExecutionError("write start marker was called more than once", code="WRITE_ATTEMPT_START_REPLAYED")
                current = self._grants.get(digest)
                if current is not state or normalized_request_id not in current.pending_write_calls:
                    raise PluginExecutionError("write start marker is no longer valid", code="WRITE_ATTEMPT_START_INVALID")
                context = current.grant.write_attempt_context
                required = {
                    "automation_id", "plugin_id", "generation", "lease_id",
                    "orchestration_run_id", "step_id",
                }
                if set(context) != required or self._write_attempt_recorder is None:
                    raise PluginExecutionError("durable write attempt evidence is unavailable", code="WRITE_ATTEMPT_RECEIPT_UNAVAILABLE")
                operation, action, role, binding, arguments = current.pending_write_calls[normalized_request_id]
                recorder = self._write_attempt_recorder
                receipt_context = dict(context)
                phase = "persisting"

            # Target hashing and durable storage may be expensive. The issuer
            # lock only reserves this one-shot transition; no other capability
            # observation or Broker call waits for persistence.
            try:
                target_ref, target_ref_sha256 = _extract_write_target_ref(
                    automation_id=str(receipt_context["automation_id"]),
                    plugin_id=str(receipt_context["plugin_id"]),
                    operation=operation,
                    action=action,
                    role=role,
                    binding=binding,
                    request_id=normalized_request_id,
                    arguments=arguments,
                )
                receipt = {
                    **{key: value for key, value in receipt_context.items() if key != "plugin_id"},
                    "request_id": normalized_request_id,
                    "operation": operation,
                    "action": action,
                    "argument_sha256": str(target_ref["content_sha256"]),
                    "target_ref_sha256": target_ref_sha256,
                    "target_ref_json": target_ref,
                }
                recorder(receipt)
            except Exception:
                with self._lock:
                    phase = "failed"
                raise

            # Persistence must succeed before this callback returns to the
            # adapter and permits its first external mutation.
            with self._lock:
                current = self._grants.get(digest)
                if current is not state or normalized_request_id not in current.pending_write_calls:
                    phase = "failed"
                    raise PluginExecutionError(
                        "write start marker is no longer valid",
                        code="WRITE_ATTEMPT_START_INVALID",
                    )
                current.pending_write_calls.pop(normalized_request_id, None)
                current.started_mutating_calls += 1
                phase = "started"

        def started() -> bool:
            with self._lock:
                return phase == "started"

        setattr(mark_write_started, "started", started)

        return mark_write_started


@dataclass(frozen=True)
class _PreparedBrokerInvocation:
    grant: BrokerGrant
    binding: object
    operation: str
    action: str
    role: str
    arguments: dict[str, Any]
    mark_write_started: Callable[[], None] | None


def _copy_broker_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return dict(arguments)


def _prepare_broker_invocation(
    issuer: LocalBrokerCapabilityIssuer,
    request: Mapping[str, Any],
) -> _PreparedBrokerInvocation:
    """Reserve the grant and prepare immutable call inputs off the event loop."""

    capability = str(request.get("capability") or "")
    request_id = str(request.get("request_id") or "")
    operation = str(request.get("operation") or "")
    action = str(request.get("action") or "")
    role = str(request.get("role") or "")
    arguments = _copy_broker_arguments(request["arguments"])
    grant, binding = issuer.consume(
        capability,
        request_id=request_id,
        operation=operation,
        action=action,
        role=role,
        arguments=arguments,
    )
    mark_write_started = issuer.mark_write_started_hook(
        capability,
        request_id=request_id,
    )
    return _PreparedBrokerInvocation(
        grant=grant,
        binding=binding,
        operation=operation,
        action=action,
        role=role,
        arguments=arguments,
        mark_write_started=mark_write_started,
    )


def _assert_redacted(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if (
                any(marker in normalized for marker in _SENSITIVE_KEYS)
                or _is_account_identifier_key(key)
            ):
                raise PluginExecutionError("core broker adapter returned sensitive data")
            _assert_redacted(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_redacted(item)


def _decompress_broker_request(compressed: bytes) -> bytes:
    decompressor = zlib.decompressobj()
    try:
        payload = decompressor.decompress(
            compressed,
            _MAX_BROKER_REQUEST_BYTES + 1,
        )
    except zlib.error as exc:
        raise PluginExecutionError(
            "core broker request compression is invalid",
            code="BROKER_REQUEST_INVALID",
        ) from exc
    if (
        len(payload) > _MAX_BROKER_REQUEST_BYTES
        or decompressor.unconsumed_tail
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise PluginExecutionError(
            "core broker request is too large or invalid",
            code="BROKER_REQUEST_TOO_LARGE",
        )
    return payload


def _decode_broker_request(payload: bytes) -> dict[str, Any]:
    request = json.loads(payload.decode("utf-8"))
    if not isinstance(request, dict) or set(request) != {
        "schema_version",
        "request_id",
        "capability",
        "operation",
        "action",
        "role",
        "arguments",
    }:
        raise PluginExecutionError("core broker request schema is invalid")
    if request.get("schema_version") != 1 or not isinstance(request.get("arguments"), dict):
        raise PluginExecutionError("core broker request fields are invalid")
    uuid.UUID(str(request.get("request_id") or ""))
    return request


def _serialize_broker_response(response: Mapping[str, Any]) -> bytes:
    data = canonical_json_bytes(response)
    if len(data) > 10 * 1024 * 1024:
        return canonical_json_bytes({"ok": False, "error_code": "BROKER_RESPONSE_TOO_LARGE"})
    return data


class LocalCoreAutomationBroker:
    """Length-bounded JSON-over-Unix-socket broker; one request per token."""

    def __init__(
        self,
        *,
        issuer: LocalBrokerCapabilityIssuer,
        adapter: CoreAutomationBrokerAdapterPort,
    ) -> None:
        self._issuer = issuer
        self._adapter = adapter
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        path = self._issuer.broker_socket_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._reclaim_dead_sibling_sockets(path)
        if path.exists():
            if path.is_symlink() or not path.is_socket():
                raise PluginExecutionError("refusing to replace an unsafe broker endpoint")
            path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle,
            path=str(path),
            limit=_MAX_BROKER_LEGACY_REQUEST_BYTES + 1,
        )
        path.chmod(0o600)

    @staticmethod
    def _reclaim_dead_sibling_sockets(current: Path) -> None:
        """Remove only dead-agent sibling Unix sockets; preserve all other entries."""

        pattern = re.compile(r"agent-([1-9][0-9]*)\.sock\Z")
        try:
            entries = tuple(current.parent.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry == current:
                continue
            matched = pattern.fullmatch(entry.name)
            if matched is None:
                continue
            try:
                if entry.is_symlink() or not entry.is_socket():
                    continue
                pid = int(matched.group(1))
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    entry.unlink()
                except PermissionError:
                    continue
                except OSError as exc:
                    if exc.errno != errno.ESRCH:
                        continue
                    entry.unlink()
            except OSError:
                continue

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        path = self._issuer.broker_socket_path
        if path.exists() and path.is_socket() and not path.is_symlink():
            path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, Any]
        try:
            payload = await self._read_request_payload(reader)
            request = await asyncio.to_thread(_decode_broker_request, payload)
            prepared = await asyncio.to_thread(
                _prepare_broker_invocation,
                self._issuer,
                request,
            )
            result = await self._adapter.invoke(
                grant=prepared.grant,
                operation=prepared.operation,
                action=prepared.action,
                role=prepared.role,
                binding=prepared.binding,
                arguments=prepared.arguments,
                mark_write_started=prepared.mark_write_started,
            )
            public_result = dict(result)
            if prepared.mark_write_started is not None:
                verified_noop = _is_verified_write_noop(
                    public_result,
                    grant=prepared.grant,
                    request=request,
                )
                if verified_noop and prepared.mark_write_started.started():
                    raise PluginExecutionError(
                        "core broker write no-op crossed its started boundary",
                        code="WRITE_ATTEMPT_NOOP_MARKED",
                    )
                if not verified_noop and not prepared.mark_write_started.started():
                    # A write adapter that returns without crossing the marker
                    # has not established a durable boundary and must never be
                    # treated as a successful invocation.
                    raise PluginExecutionError(
                        "core broker write adapter did not mark its started boundary",
                        code="WRITE_ATTEMPT_START_NOT_RECORDED",
                    )
                public_result.pop(VERIFIED_WRITE_NOOP_FIELD, None)
            await asyncio.to_thread(_assert_redacted, public_result)
            response = {"ok": True, "data": public_result}
        except Exception as exc:
            response = {
                "ok": False,
                "error_code": getattr(exc, "code", type(exc).__name__.upper())[:64],
                "error": redact_text(exc)[:300],
            }
        data = await asyncio.to_thread(_serialize_broker_response, response)
        try:
            writer.write(data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _read_request_payload(reader: asyncio.StreamReader) -> bytes:
        """Read one bounded request, accepting the legacy line protocol.

        Current signed packages use a compressed length-prefixed frame so a
        legitimate snapshot is not constrained by ``StreamReader.readline``.
        The decoded request remains strictly bounded. Older committed
        generations keep their original newline protocol until their leases
        drain.
        """

        try:
            first_line = await reader.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            raise PluginExecutionError(
                "core broker request is too large",
                code="BROKER_REQUEST_TOO_LARGE",
            ) from exc
        if not first_line or not first_line.endswith(b"\n"):
            raise PluginExecutionError(
                "core broker request is incomplete",
                code="BROKER_REQUEST_INVALID",
            )
        if not first_line.startswith(_BROKER_FRAME_PREFIX):
            if len(first_line) > _MAX_BROKER_LEGACY_REQUEST_BYTES:
                raise PluginExecutionError(
                    "core broker request is too large",
                    code="BROKER_REQUEST_TOO_LARGE",
                )
            return first_line[:-1]

        raw_length = first_line[len(_BROKER_FRAME_PREFIX) : -1]
        if not raw_length.isdigit() or len(raw_length) > 9:
            raise PluginExecutionError(
                "core broker frame length is invalid",
                code="BROKER_REQUEST_INVALID",
            )
        compressed_length = int(raw_length)
        if not 1 <= compressed_length <= _MAX_BROKER_COMPRESSED_REQUEST_BYTES:
            raise PluginExecutionError(
                "core broker request is too large",
                code="BROKER_REQUEST_TOO_LARGE",
            )
        try:
            compressed = await reader.readexactly(compressed_length)
        except asyncio.IncompleteReadError as exc:
            raise PluginExecutionError(
                "core broker request is incomplete",
                code="BROKER_REQUEST_INVALID",
            ) from exc
        return await asyncio.to_thread(_decompress_broker_request, compressed)
