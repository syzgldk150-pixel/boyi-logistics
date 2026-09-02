"""Closed broker primitives for the signed daily send-order action."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.core_adapter import (
    CoreBrokerHandler,
    CoreBrokerInvocationContext,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes


AccountDescriptorPort = Callable[[str], Mapping[str, Any]]
SourcePagePort = Callable[[Mapping[str, Any], str, int, int], Mapping[str, Any]]
BitableListPort = Callable[[str, int, int, tuple[str, ...]], Sequence[Mapping[str, Any]]]
BitableDeletePort = Callable[[str, tuple[str, ...]], Mapping[str, Any]]
BitableWritePort = Callable[[str, list[dict[str, Any]]], Mapping[str, Any]]
ProjectionReplacePort = Callable[[list[dict[str, Any]], str], Mapping[str, Any]]


_TOOL = "sync_daily_send_orders"
_ACCOUNT_ROLE = "account_id"
_RESOURCE_ROLE = "send_order_bitable"
_MAX_RECORDS = 10_000
_MAX_PAGE_SIZE = 500
_MAX_RECORD_REFS = 10_000
_LEASE_SECONDS = 3_600.0
MARKED_WRITE_ACTION_KEYS = frozenset(
    {
        ("ledger.invoke", "sync_daily_send_orders.lock.acquire"),
        ("ledger.invoke", "sync_daily_send_orders.lock.release"),
        ("network.request", "feishu.bitable.delete_records"),
        ("network.request", "feishu.bitable.write_records"),
        ("projection.invoke", "waybill.ronghui.replace_date"),
    }
)
_WAYBILL_FIELD = "运单编号"
_DATE_FIELD = "发件日期"
_BITABLE_FIELDS = (
    "运单编号",
    "发件日期",
    "签收状态",
    "目的网点",
    "收件区/县",
    "收件地址",
    "寄件人",
    "寄件手机",
    "收货人",
    "收货电话",
    "货物名称",
    "包装类型",
    "派送方式",
    "件数",
    "实际重量",
    "录单金额",
    "回单号",
    "备注",
    "支付类型",
    "体积重量",
    "体积",
    "结算重量",
    "到付款",
)
_NUMERIC_FIELDS = frozenset(
    {
        "件数",
        "实际重量",
        "录单金额",
        "体积重量",
        "体积",
        "结算重量",
        "到付款",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "BILL_CODE",
        "INSERT_DATE",
        "BL_SIGNS_MARKING_TEXT",
        "DESTINATION",
        "ACCEPT_COUNTY",
        "ACCEPT_MAN_ADDRESS",
        "SEND_MAN",
        "SEND_MAN_PHONE",
        "ACCEPT_MAN",
        "ACCEPT_MAN_PHONE",
        "GOODS_NAME",
        "PACK_TYPE",
        "DISPATCH_MODE",
        "PIECE_NUMBER",
        "FEE_WEIGHT",
        "GUEST_FREIGHT",
        "R_BILLCODE",
        "REMARK",
        "PAYMENT_TYPE",
        "VOLUME_WEIGHT",
        "VOLUME",
        "SETTLEMENT_WEIGHT",
        "TOPAYMENT",
        "当前扫描状态",
        "最新扫描状态",
        "扫描状态",
        "最新扫描类型",
        "scan_status",
        "current_scan_status",
        "scan_type",
        "SCAN_TYPE",
    }
)
_PROJECTION_FIELDS = (
    "waybill_no",
    "destination_site",
    "open_date",
    "receiver_address",
    "receiver_name",
    "receiver_phone",
    "sender_name",
    "sender_phone",
    "goods_name_lines",
    "package_type_lines",
    "quantity_lines",
    "weight_volume",
    "delivery_method",
    "freight_fee",
    "pickup_fee",
    "delivery_fee",
    "transfer_fee",
    "payment_method",
    "insurance_amount",
    "cod_amount",
    "remark",
    "scan_status",
    "status",
)


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if value is None or isinstance(value, (bool, Mapping, list, tuple, set)):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    result = str(value).strip()
    if not result or len(result) > maximum:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    return result


def _optional_text(value: object, label: str, *, maximum: int = 2_000) -> str:
    if value in (None, ""):
        return ""
    return _text(value, label, maximum=maximum)


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if not minimum <= result <= maximum or str(result) != str(value).strip():
        raise _error(f"{label} is outside its signed limit", "BROKER_ARGUMENT_INVALID")
    return result


def _business_date(value: object) -> str:
    text = _text(value, "target_date", maximum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise _error("target_date must use YYYY-MM-DD", "BROKER_ARGUMENT_INVALID") from exc
    if parsed.isoformat() != text:
        raise _error("target_date must use YYYY-MM-DD", "BROKER_ARGUMENT_INVALID")
    return text


def _strict(arguments: Mapping[str, Any], fields: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping) or set(arguments) != fields:
        raise _error("daily-send primitive arguments are invalid", "BROKER_ARGUMENT_INVALID")
    return dict(arguments)


def _require_context(
    context: CoreBrokerInvocationContext,
    *,
    operation: str,
    action: str,
    role: str,
) -> None:
    if context.tool_name != _TOOL or context.operation != operation or context.action != action or context.role != role:
        raise _error("daily-send broker context is invalid", "BROKER_CONTEXT_INVALID")


def _one_account(context: CoreBrokerInvocationContext) -> str:
    if len(context.account_ids) != 1:
        raise _error("daily-send requires one exact account", "BROKER_CONTEXT_INVALID")
    account_id = str(context.account_ids[0]).strip()
    bound = tuple(str(value).strip() for value in context.account_bindings.get(_ACCOUNT_ROLE, ()))
    if not account_id or bound != (account_id,):
        raise _error("daily-send account binding changed", "BROKER_CONTEXT_INVALID")
    return account_id


def _account_descriptor(
    ports: "DailySendHandlerPorts",
    context: CoreBrokerInvocationContext,
) -> Mapping[str, Any]:
    account_id = _one_account(context)
    descriptor = ports.describe_account(account_id)
    if not isinstance(descriptor, Mapping):
        raise _error("daily-send account descriptor is invalid", "BROKER_ACCOUNT_INVALID")
    if str(descriptor.get("account_id") or "").strip() != account_id:
        raise _error("daily-send account descriptor changed", "BROKER_ACCOUNT_INVALID")
    if str(descriptor.get("system") or "").strip().lower() != "ronghui":
        raise _error("daily-send requires a Ronghui account", "BROKER_ACCOUNT_SYSTEM_MISMATCH")
    if not str(descriptor.get("session_profile") or "").strip():
        raise _error("daily-send account is not authenticated", "BLOCKED_LOGIN")
    return descriptor


def _resource_id(context: CoreBrokerInvocationContext) -> str:
    if not context.resource_id:
        raise _error("daily-send Bitable resource is unbound", "BROKER_RESOURCE_UNAVAILABLE")
    bound = str(context.resource_bindings.get(_RESOURCE_ROLE) or "").strip()
    if bound != context.resource_id:
        raise _error("daily-send resource binding changed", "BROKER_CONTEXT_INVALID")
    return bound


class _OpaqueCodec:
    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("daily-send broker secret must contain at least 32 bytes")
        self._secret = bytes(secret)

    @staticmethod
    def _context(context: CoreBrokerInvocationContext) -> dict[str, str]:
        return {
            "automation_id": context.automation_id,
            "plugin_version": context.plugin_version,
            "tool_name": context.tool_name,
        }

    def encode(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        payload: Mapping[str, Any],
    ) -> str:
        body = canonical_json_bytes({"context": self._context(context), "purpose": purpose, "payload": dict(payload)})
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode("ascii").rstrip("=")

    def decode(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        value: object,
    ) -> dict[str, Any]:
        token = _text(value, "opaque reference", maximum=2_048)
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:
            raise _error("opaque reference is invalid", "BROKER_CURSOR_INVALID") from exc
        if len(raw) <= 32:
            raise _error("opaque reference is invalid", "BROKER_CURSOR_INVALID")
        body, supplied = raw[:-32], raw[-32:]
        expected = hmac.new(self._secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise _error("opaque reference is invalid", "BROKER_CURSOR_INVALID")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _error("opaque reference is invalid", "BROKER_CURSOR_INVALID") from exc
        if (
            not isinstance(decoded, Mapping)
            or decoded.get("context") != self._context(context)
            or decoded.get("purpose") != purpose
            or not isinstance(decoded.get("payload"), Mapping)
        ):
            raise _error("opaque reference is invalid", "BROKER_CURSOR_INVALID")
        return dict(decoded["payload"])

    def evidence(
        self,
        context: CoreBrokerInvocationContext,
        purpose: str,
        payload: Mapping[str, Any],
    ) -> str:
        digest = hmac.new(
            self._secret,
            canonical_json_bytes({"context": self._context(context), "purpose": purpose, "payload": dict(payload)}),
            hashlib.sha256,
        ).hexdigest()
        return f"daily-send:{purpose}:v1:{digest}"


@dataclass(frozen=True)
class DailySendHandlerPorts:
    describe_account: AccountDescriptorPort
    source_page: SourcePagePort
    bitable_list: BitableListPort
    bitable_delete: BitableDeletePort
    bitable_write: BitableWritePort
    projection_replace: ProjectionReplacePort


@dataclass
class _Lease:
    owner: tuple[str, str]
    nonce_sha256: str
    expires_at: float


class _LeaseRegistry:
    def __init__(self, codec: _OpaqueCodec) -> None:
        self._codec = codec
        self._lock = threading.Lock()
        self._lease: _Lease | None = None

    def acquire(
        self,
        context: CoreBrokerInvocationContext,
    ) -> str:
        now = time.monotonic()
        owner = (context.automation_id, context.plugin_version)
        nonce = secrets.token_urlsafe(32)
        nonce_sha256 = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        with self._lock:
            if self._lease is not None and self._lease.expires_at > now:
                raise _error("daily-send synchronization is already running", "BROKER_CONCURRENCY_BLOCKED")
            self._lease = _Lease(
                owner=owner,
                nonce_sha256=nonce_sha256,
                expires_at=now + _LEASE_SECONDS,
            )
        return self._codec.encode(
            context,
            "lease",
            {"nonce": nonce, "owner": list(owner)},
        )

    def release(
        self,
        context: CoreBrokerInvocationContext,
        lease_ref: object,
    ) -> None:
        payload = self._codec.decode(context, "lease", lease_ref)
        nonce = _text(payload.get("nonce"), "lease nonce", maximum=256)
        owner = (context.automation_id, context.plugin_version)
        if payload.get("owner") != list(owner):
            raise _error("daily-send lease owner changed", "BROKER_CURSOR_INVALID")
        digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        with self._lock:
            if self._lease is None:
                raise _error("daily-send lease is not active", "BROKER_CURSOR_INVALID")
            if self._lease.owner != owner or not hmac.compare_digest(
                self._lease.nonce_sha256,
                digest,
            ):
                raise _error("daily-send lease is not active", "BROKER_CURSOR_INVALID")
            self._lease = None


def _decimal(value: object, label: str) -> str | int | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID") from exc
    if not number.is_finite():
        raise _error(f"{label} is invalid", "BROKER_ARGUMENT_INVALID")
    if number == number.to_integral_value():
        return int(number)
    return format(number, "f")


def _date_text(value: object) -> str:
    if value in (None, "") or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float, Decimal)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    text = str(value).strip()
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
    return ""


def _field_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise _error("Bitable text field is invalid", "BROKER_ARGUMENT_INVALID")
    if isinstance(value, (str, int, float, Decimal)):
        return str(value).strip()
    if isinstance(value, (list, tuple)):
        return "".join(_field_text(item) for item in value).strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "link"):
            result = _field_text(value.get(key))
            if result:
                return result
        return "".join(_field_text(item) for item in value.values()).strip()
    raise _error("Bitable text field is invalid", "BROKER_ARGUMENT_INVALID")


def _canonical_fields(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(_BITABLE_FIELDS):
        raise _error("daily-send Bitable fields are invalid", "BROKER_ARGUMENT_INVALID")
    output: dict[str, Any] = {}
    for field in _BITABLE_FIELDS:
        raw = value.get(field)
        if field == _WAYBILL_FIELD:
            identity = "".join(_field_text(raw).removeprefix("=").strip(" '\"").split())
            if not identity or len(identity) > 128:
                raise _error("daily-send waybill identity is invalid", "BROKER_ARGUMENT_INVALID")
            output[field] = identity
        elif field == _DATE_FIELD:
            date_value = _date_text(raw)
            if not date_value:
                raise _error("daily-send date field is invalid", "BROKER_ARGUMENT_INVALID")
            output[field] = date_value
        elif field in _NUMERIC_FIELDS:
            output[field] = _decimal(raw, field)
        else:
            output[field] = _field_text(raw)
    return output


def _projection_records(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > _MAX_RECORDS:
        raise _error("daily-send projection records are invalid", "BROKER_ARGUMENT_INVALID")
    records: list[dict[str, str]] = []
    identities: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != set(_PROJECTION_FIELDS):
            raise _error("daily-send projection record is invalid", "BROKER_ARGUMENT_INVALID")
        record = {field: _optional_text(raw.get(field), field) for field in _PROJECTION_FIELDS}
        identity = record["waybill_no"]
        if not identity or identity in identities:
            raise _error("daily-send projection identities are invalid", "BROKER_ARGUMENT_INVALID")
        identities.add(identity)
        records.append(record)
    return records


class _DailySendHandlers:
    def __init__(self, ports: DailySendHandlerPorts, *, secret: bytes) -> None:
        self._ports = ports
        self._codec = _OpaqueCodec(secret)
        self._leases = _LeaseRegistry(self._codec)

    @staticmethod
    def _mark_write_started(context: CoreBrokerInvocationContext) -> None:
        if context.mark_write_started is not None:
            context.mark_write_started()

    def acquire(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="ledger.invoke",
            action="sync_daily_send_orders.lock.acquire",
            role=_ACCOUNT_ROLE,
        )
        _strict(arguments, set())
        _account_descriptor(self._ports, context)
        lease_ref = self._leases.acquire(context)
        proof = {"acquired": True, "owner": context.automation_id}
        return {
            "acquired": True,
            "lease_ref": lease_ref,
            "evidence_ref": self._codec.evidence(context, "lock-acquire", proof),
        }

    def release(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="ledger.invoke",
            action="sync_daily_send_orders.lock.release",
            role=_ACCOUNT_ROLE,
        )
        values = _strict(arguments, {"lease_ref"})
        _account_descriptor(self._ports, context)
        self._leases.release(context, values.get("lease_ref"))
        proof = {"released": True, "owner": context.automation_id}
        return {
            "committed": True,
            "released": True,
            "evidence_ref": self._codec.evidence(context, "lock-release", proof),
        }

    def source_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="browser.invoke",
            action="ronghui.send_order.read_page",
            role=_ACCOUNT_ROLE,
        )
        values = _strict(arguments, {"target_date", "page_index", "page_size"})
        descriptor = _account_descriptor(self._ports, context)
        target_date = _business_date(values.get("target_date"))
        page_index = _integer(values.get("page_index"), "page_index", minimum=0, maximum=49)
        page_size = _integer(values.get("page_size"), "page_size", minimum=1, maximum=_MAX_PAGE_SIZE)
        raw = self._ports.source_page(descriptor, target_date, page_index, page_size)
        if not isinstance(raw, Mapping):
            raise _error("daily-send source page is invalid", "BROKER_SOURCE_INVALID")
        items = raw.get("items")
        total = raw.get("total")
        if (
            not isinstance(items, list)
            or len(items) > page_size
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < len(items)
            or total > _MAX_RECORDS
        ):
            raise _error("daily-send source page is invalid", "BROKER_SOURCE_INVALID")
        projected: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise _error("daily-send source row is invalid", "BROKER_SOURCE_INVALID")
            projected.append({field: item.get(field) for field in _SOURCE_FIELDS if field in item})
        proof = {
            "target_date": target_date,
            "page_index": page_index,
            "returned": len(projected),
            "total": total,
        }
        return {
            "items": projected,
            "total": total,
            "evidence_ref": self._codec.evidence(context, "source-page", proof),
        }

    def list_records(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="network.request",
            action="feishu.bitable.list_records",
            role=_RESOURCE_ROLE,
        )
        values = _strict(arguments, {"offset", "page_size", "fields"})
        resource_id = _resource_id(context)
        offset = _integer(values.get("offset"), "offset", minimum=0, maximum=_MAX_RECORDS)
        page_size = _integer(values.get("page_size"), "page_size", minimum=1, maximum=200)
        raw_fields = values.get("fields")
        if (
            not isinstance(raw_fields, list)
            or not raw_fields
            or any(not isinstance(field, str) for field in raw_fields)
            or len(set(raw_fields)) != len(raw_fields)
            or not set(raw_fields) <= set(_BITABLE_FIELDS)
        ):
            raise _error("daily-send Bitable fields are invalid", "BROKER_ARGUMENT_INVALID")
        fields = tuple(raw_fields)
        raw_items = self._ports.bitable_list(resource_id, offset, page_size, fields)
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)) or len(raw_items) > page_size:
            raise _error("daily-send Bitable page is invalid", "BROKER_SOURCE_INVALID")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("fields"), Mapping):
                raise _error("daily-send Bitable record is invalid", "BROKER_SOURCE_INVALID")
            record_id = _text(raw.get("record_id"), "record_id", maximum=512)
            if record_id in seen:
                raise _error("daily-send Bitable page repeated a record", "BROKER_SOURCE_INVALID")
            seen.add(record_id)
            record_ref = self._codec.encode(context, "record", {"record_id": record_id})
            record_fields = {field: raw["fields"].get(field) for field in fields}
            items.append({"record_ref": record_ref, "fields": record_fields})
        # The action intentionally repeats the same exact-resource read before
        # and after a write.  Bind every observation to a fresh opaque nonce so
        # two equal snapshots remain distinct evidence events instead of
        # collapsing to the same deterministic HMAC reference.
        proof = {
            "offset": offset,
            "returned": len(items),
            "fields": list(fields),
            "observation_id": secrets.token_hex(16),
        }
        return {
            "items": items,
            "evidence_ref": self._codec.evidence(context, "bitable-list", proof),
        }

    def delete_records(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="network.request",
            action="feishu.bitable.delete_records",
            role=_RESOURCE_ROLE,
        )
        values = _strict(arguments, {"record_refs"})
        resource_id = _resource_id(context)
        refs = values.get("record_refs")
        if not isinstance(refs, list) or not refs or len(refs) > _MAX_RECORD_REFS:
            raise _error("daily-send delete references are invalid", "BROKER_ARGUMENT_INVALID")
        record_ids: list[str] = []
        for ref in refs:
            payload = self._codec.decode(context, "record", ref)
            record_ids.append(_text(payload.get("record_id"), "record_id", maximum=512))
        if len(set(record_ids)) != len(record_ids):
            raise _error("daily-send delete references are duplicated", "BROKER_ARGUMENT_INVALID")
        self._mark_write_started(context)
        raw = self._ports.bitable_delete(resource_id, tuple(record_ids))
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
            or raw.get("deleted") != len(record_ids)
        ):
            raise _error(
                "daily-send Bitable delete was not confirmed by a fresh read",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "deleted": len(record_ids),
            "record_set_sha256": hashlib.sha256(canonical_json_bytes(sorted(record_ids))).hexdigest(),
        }
        return {
            "committed": True,
            "deleted": len(record_ids),
            "evidence_ref": self._codec.evidence(context, "bitable-delete", proof),
        }

    def write_records(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="network.request",
            action="feishu.bitable.write_records",
            role=_RESOURCE_ROLE,
        )
        values = _strict(arguments, {"records"})
        resource_id = _resource_id(context)
        records = values.get("records")
        if not isinstance(records, list) or not records or len(records) > _MAX_RECORDS:
            raise _error("daily-send Bitable records are invalid", "BROKER_ARGUMENT_INVALID")
        normalized: list[dict[str, Any]] = []
        identities: set[str] = set()
        for raw in records:
            if not isinstance(raw, Mapping) or set(raw) != {"fields"}:
                raise _error("daily-send Bitable record is invalid", "BROKER_ARGUMENT_INVALID")
            canonical = _canonical_fields(raw.get("fields"))
            identity = str(canonical[_WAYBILL_FIELD])
            if identity in identities:
                raise _error("daily-send Bitable identities are duplicated", "BROKER_ARGUMENT_INVALID")
            identities.add(identity)
            normalized.append({"fields": dict(raw["fields"])})
        self._mark_write_started(context)
        raw = self._ports.bitable_write(resource_id, normalized)
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
            or raw.get("written") != len(normalized)
        ):
            raise _error(
                "daily-send Bitable write was not confirmed by a fresh read",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "written": len(normalized),
            "snapshot_sha256": hashlib.sha256(canonical_json_bytes(sorted(identities))).hexdigest(),
        }
        return {
            "committed": True,
            "written": len(normalized),
            "evidence_ref": self._codec.evidence(context, "bitable-write", proof),
        }

    def replace_projection(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            operation="projection.invoke",
            action="waybill.ronghui.replace_date",
            role=_ACCOUNT_ROLE,
        )
        values = _strict(arguments, {"records", "target_date"})
        _account_descriptor(self._ports, context)
        target_date = _business_date(values.get("target_date"))
        records = _projection_records(values.get("records"))
        if any(record["open_date"] != target_date for record in records):
            raise _error("daily-send projection date changed", "BROKER_ARGUMENT_INVALID")
        self._mark_write_started(context)
        raw = self._ports.projection_replace(records, target_date)
        if not isinstance(raw, Mapping) or raw.get("ok") is not True or raw.get("verified") is not True:
            raise _error(
                "daily-send projection was not confirmed by a fresh read",
                "WRITE_OUTCOME_UNKNOWN",
            )
        counts = {}
        for key in ("upserted", "updates", "creates", "deleted_stale"):
            value = raw.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _error("daily-send projection counts are invalid", "BROKER_PROJECTION_MISMATCH")
            counts[key] = value
        if counts["upserted"] != counts["updates"] + counts["creates"]:
            raise _error("daily-send projection counts are inconsistent", "BROKER_PROJECTION_MISMATCH")
        proof = {
            "target_date": target_date,
            "record_count": len(records),
            **counts,
        }
        return {
            "committed": True,
            **counts,
            "evidence_ref": self._codec.evidence(context, "projection-replace", proof),
        }

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        return {
            ("ledger.invoke", "sync_daily_send_orders.lock.acquire"): self.acquire,
            ("ledger.invoke", "sync_daily_send_orders.lock.release"): self.release,
            ("browser.invoke", "ronghui.send_order.read_page"): self.source_page,
            ("network.request", "feishu.bitable.list_records"): self.list_records,
            ("network.request", "feishu.bitable.delete_records"): self.delete_records,
            ("network.request", "feishu.bitable.write_records"): self.write_records,
            ("projection.invoke", "waybill.ronghui.replace_date"): self.replace_projection,
        }


def build_daily_send_handler_map(
    ports: DailySendHandlerPorts,
    *,
    cursor_secret: bytes,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    return _DailySendHandlers(ports, secret=cursor_secret).handler_map()


__all__ = [
    "DailySendHandlerPorts",
    "MARKED_WRITE_ACTION_KEYS",
    "build_daily_send_handler_map",
]
