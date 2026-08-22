"""Closed write handlers for delivery status and site-send snapshots.

The signed payload owns business selection and commit order.  These handlers
validate the exact broker side-channel bindings and accept success only when a
production port returns two distinct fresh observations proving the requested
write.  Raw account/resource identifiers never enter plugin JSON.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.automation_plugins.core_adapter import (
    CoreBrokerHandler,
    CoreBrokerInvocationContext,
)
from agent.automation_plugins.first_party_handler_common import (
    _OpaqueCodec,
    _account_descriptor,
    _business_date,
    _error,
    _optional_finite_number,
    _text,
)
from agent.automation_plugins.manifest import canonical_json_bytes


AccountDescriptorPort = Callable[[str], Mapping[str, Any]]
WriteStartMarker = Callable[[], None] | None
SiteBitableReplacePort = Callable[[str, list[dict[str, Any]], str, WriteStartMarker], Mapping[str, Any]]
SiteSheetReplacePort = Callable[[str, list[list[Any]], str, WriteStartMarker], Mapping[str, Any]]
DeliveryBitableWritePort = Callable[[str, list[dict[str, str]], WriteStartMarker], Mapping[str, Any]]
DeliveryProjectionUpdatePort = Callable[[tuple[str, ...], str, WriteStartMarker], Mapping[str, Any]]


SITE_WRITE_ACTION_KEYS = frozenset(
    {
        ("network.request", "feishu.bitable.replace_snapshot"),
        ("network.request", "feishu.sheet.replace"),
    }
)
DELIVERY_WRITE_ACTION_KEYS = frozenset(
    {
        ("network.request", "feishu.bitable.write_records"),
        ("projection.invoke", "waybill.delivery_status.update"),
    }
)
DELIVERY_SITE_WRITE_ACTION_KEYS = SITE_WRITE_ACTION_KEYS | DELIVERY_WRITE_ACTION_KEYS
MARKED_WRITE_ACTION_KEYS = DELIVERY_SITE_WRITE_ACTION_KEYS

_SITE_TOOL = "sync_site_send_list"
_SITE_BITABLE_ROLE = "site_send_bitable"
_SITE_SHEET_ROLE = "site_send_sheet"
_DELIVERY_TOOL = "sync_delivery_status"
_DELIVERY_BITABLE_ROLE = "delivery_status_bitable"
_ACCOUNT_ROLE = "account_id"
_MAX_RECORDS = 20_000
_SITE_FIELDS = (
    "tracking_number",
    "send_site",
    "package_type",
    "destination",
    "pieces",
    "weight",
)
_SITE_SHEET_COLUMNS = (
    "tracking_number",
    "send_site",
    "package_type",
    "pieces",
    "weight",
    "destination",
)
_VERIFIED_RESULT_FIELDS = {
    "ok",
    "verified",
    "record_count",
    "before_sha256",
    "after_sha256",
    "before_observation_id",
    "after_observation_id",
    "write_response_received",
}


@dataclass(frozen=True)
class DeliverySiteHandlerPorts:
    describe_account: AccountDescriptorPort
    site_bitable_replace: SiteBitableReplacePort
    site_sheet_replace: SiteSheetReplacePort
    delivery_bitable_write: DeliveryBitableWritePort
    delivery_projection_update: DeliveryProjectionUpdatePort


def _require_context(
    context: CoreBrokerInvocationContext,
    *,
    tool_name: str,
    operation: str,
    action: str,
    role: str,
) -> None:
    if (
        context.tool_name != tool_name
        or context.operation != operation
        or context.action != action
        or context.role != role
    ):
        raise _error(
            "delivery/site write broker context is invalid",
            "BROKER_CONTEXT_INVALID",
        )


def _resource_id(context: CoreBrokerInvocationContext, role: str) -> str:
    resource_id = str(context.resource_id or "").strip()
    bound = str(context.resource_bindings.get(role) or "").strip()
    if not resource_id or bound != resource_id:
        raise _error(
            "delivery/site resource binding changed",
            "BROKER_CONTEXT_INVALID",
        )
    return resource_id


def _account_id(context: CoreBrokerInvocationContext) -> str:
    if len(context.account_ids) != 1:
        raise _error(
            "delivery projection requires one exact account",
            "BROKER_CONTEXT_INVALID",
        )
    account_id = str(context.account_ids[0] or "").strip()
    bound = context.account_bindings.get(_ACCOUNT_ROLE)
    if not account_id or bound != (account_id,):
        raise _error(
            "delivery account binding changed",
            "BROKER_CONTEXT_INVALID",
        )
    return account_id


def _site_records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > _MAX_RECORDS:
        raise _error("site-send Bitable records are invalid", "BROKER_ARGUMENT_INVALID")
    output: list[dict[str, Any]] = []
    identities: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"fields"} or not isinstance(raw.get("fields"), Mapping):
            raise _error(
                "site-send Bitable record schema is invalid",
                "BROKER_ARGUMENT_INVALID",
            )
        fields = dict(raw["fields"])
        if set(fields) != set(_SITE_FIELDS):
            raise _error(
                "site-send Bitable field set is invalid",
                "BROKER_ARGUMENT_INVALID",
            )
        tracking_number = _text(
            fields.get("tracking_number"),
            "tracking_number",
            maximum=128,
        )
        if tracking_number in identities:
            raise _error(
                "site-send Bitable identities are duplicated",
                "BROKER_ARGUMENT_INVALID",
            )
        identities.add(tracking_number)
        normalized: dict[str, Any] = {"tracking_number": tracking_number}
        for field in ("send_site", "package_type", "destination"):
            raw_text = fields.get(field)
            if not isinstance(raw_text, str) or len(raw_text) > 512:
                raise _error(
                    "site-send Bitable text field is invalid",
                    "BROKER_ARGUMENT_INVALID",
                )
            normalized[field] = raw_text.strip()
        normalized["pieces"] = _optional_finite_number(fields.get("pieces"), "pieces")
        normalized["weight"] = _optional_finite_number(fields.get("weight"), "weight")
        output.append({"fields": normalized})
    return output


def _site_sheet_rows(value: object) -> list[list[Any]]:
    if not isinstance(value, list) or len(value) > _MAX_RECORDS:
        raise _error("site-send Sheet rows are invalid", "BROKER_ARGUMENT_INVALID")
    output: list[list[Any]] = []
    identities: set[str] = set()
    for raw in value:
        if not isinstance(raw, list) or len(raw) != len(_SITE_SHEET_COLUMNS):
            raise _error(
                "site-send Sheet row schema is invalid",
                "BROKER_ARGUMENT_INVALID",
            )
        tracking_number = _text(raw[0], "tracking_number", maximum=128)
        if tracking_number in identities:
            raise _error(
                "site-send Sheet identities are duplicated",
                "BROKER_ARGUMENT_INVALID",
            )
        identities.add(tracking_number)
        row: list[Any] = [tracking_number]
        for index, field in ((1, "send_site"), (2, "package_type")):
            value_text = raw[index]
            if not isinstance(value_text, str) or len(value_text) > 512:
                raise _error(
                    f"site-send Sheet {field} is invalid",
                    "BROKER_ARGUMENT_INVALID",
                )
            row.append(value_text.strip())
        pieces = _optional_finite_number(raw[3], "pieces")
        weight = _optional_finite_number(raw[4], "weight")
        row.append("" if pieces is None else pieces)
        row.append("" if weight is None else weight)
        destination = raw[5]
        if not isinstance(destination, str) or len(destination) > 512:
            raise _error(
                "site-send Sheet destination is invalid",
                "BROKER_ARGUMENT_INVALID",
            )
        row.append(destination.strip())
        output.append(row)
    return output


def _delivery_records(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_RECORDS:
        raise _error(
            "delivery Bitable records are invalid",
            "BROKER_ARGUMENT_INVALID",
        )
    output: list[dict[str, str]] = []
    identities: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"record_id", "status"}:
            raise _error(
                "delivery Bitable record schema is invalid",
                "BROKER_ARGUMENT_INVALID",
            )
        record_id = _text(raw.get("record_id"), "record_id", maximum=128)
        status = _text(raw.get("status"), "status", maximum=128)
        if record_id in identities:
            raise _error(
                "delivery Bitable record identities are duplicated",
                "BROKER_ARGUMENT_INVALID",
            )
        identities.add(record_id)
        output.append({"record_id": record_id, "status": status})
    return output


def _delivery_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_RECORDS:
        raise _error(
            "delivery projection identities are invalid",
            "BROKER_ARGUMENT_INVALID",
        )
    output = tuple(_text(item, "bill_code", maximum=128) for item in value)
    if len(output) != len(set(output)):
        raise _error(
            "delivery projection identities are duplicated",
            "BROKER_ARGUMENT_INVALID",
        )
    return output


def _verified_result(
    raw: object,
    *,
    expected_count: int,
    context: CoreBrokerInvocationContext,
    codec: _OpaqueCodec,
    purpose: str,
    proof: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _VERIFIED_RESULT_FIELDS:
        raise _error(
            "write result did not contain a closed fresh-read proof",
            "WRITE_OUTCOME_UNKNOWN",
        )
    result = dict(raw)
    count = result.get("record_count")
    before_digest = str(result.get("before_sha256") or "")
    after_digest = str(result.get("after_sha256") or "")
    before_observation = str(result.get("before_observation_id") or "")
    after_observation = str(result.get("after_observation_id") or "")
    if (
        result.get("ok") is not True
        or result.get("verified") is not True
        or result.get("write_response_received") is not True
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != expected_count
        or len(before_digest) != 64
        or len(after_digest) != 64
        or any(character not in "0123456789abcdef" for character in before_digest)
        or any(character not in "0123456789abcdef" for character in after_digest)
        or not before_observation
        or not after_observation
        or before_observation == after_observation
    ):
        raise _error(
            "write was not confirmed by two exact fresh snapshots",
            "WRITE_OUTCOME_UNKNOWN",
        )
    evidence = {
        **dict(proof),
        "record_count": expected_count,
        "before_sha256": before_digest,
        "after_sha256": after_digest,
        "observation_pair_sha256": hashlib.sha256(
            canonical_json_bytes([before_observation, after_observation])
        ).hexdigest(),
    }
    return {
        "committed": True,
        "record_count": expected_count,
        "evidence_ref": codec.evidence(context, purpose, evidence),
    }


class _DeliverySiteHandlers:
    def __init__(self, ports: DeliverySiteHandlerPorts, *, secret: bytes) -> None:
        self._ports = ports
        self._codec = _OpaqueCodec(secret)

    def replace_site_bitable(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_SITE_TOOL,
            operation="network.request",
            action="feishu.bitable.replace_snapshot",
            role=_SITE_BITABLE_ROLE,
        )
        if not isinstance(arguments, Mapping) or set(arguments) != {
            "records",
            "target_date",
        }:
            raise _error("site-send Bitable arguments are invalid", "BROKER_ARGUMENT_INVALID")
        target_date = _business_date(arguments.get("target_date"))
        records = _site_records(arguments.get("records"))
        result = self._ports.site_bitable_replace(
            _resource_id(context, _SITE_BITABLE_ROLE),
            records,
            target_date,
            context.mark_write_started,
        )
        return _verified_result(
            result,
            expected_count=len(records),
            context=context,
            codec=self._codec,
            purpose="site-send-bitable-replace",
            proof={"target_date": target_date},
        )

    def replace_site_sheet(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_SITE_TOOL,
            operation="network.request",
            action="feishu.sheet.replace",
            role=_SITE_SHEET_ROLE,
        )
        if not isinstance(arguments, Mapping) or set(arguments) != {
            "values",
            "target_date",
        }:
            raise _error("site-send Sheet arguments are invalid", "BROKER_ARGUMENT_INVALID")
        target_date = _business_date(arguments.get("target_date"))
        rows = _site_sheet_rows(arguments.get("values"))
        result = self._ports.site_sheet_replace(
            _resource_id(context, _SITE_SHEET_ROLE),
            rows,
            target_date,
            context.mark_write_started,
        )
        return _verified_result(
            result,
            expected_count=len(rows),
            context=context,
            codec=self._codec,
            purpose="site-send-sheet-replace",
            proof={"target_date": target_date},
        )

    def write_delivery_bitable(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_DELIVERY_TOOL,
            operation="network.request",
            action="feishu.bitable.write_records",
            role=_DELIVERY_BITABLE_ROLE,
        )
        if not isinstance(arguments, Mapping) or set(arguments) != {"records"}:
            raise _error("delivery Bitable arguments are invalid", "BROKER_ARGUMENT_INVALID")
        records = _delivery_records(arguments.get("records"))
        verified = _verified_result(
            self._ports.delivery_bitable_write(
                _resource_id(context, _DELIVERY_BITABLE_ROLE),
                records,
                context.mark_write_started,
            ),
            expected_count=len(records),
            context=context,
            codec=self._codec,
            purpose="delivery-bitable-update",
            proof={
                "record_set_sha256": hashlib.sha256(
                    canonical_json_bytes(sorted(row["record_id"] for row in records))
                ).hexdigest()
            },
        )
        return {
            **verified,
            "written": verified["record_count"],
        }

    def update_delivery_projection(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_DELIVERY_TOOL,
            operation="projection.invoke",
            action="waybill.delivery_status.update",
            role=_ACCOUNT_ROLE,
        )
        if not isinstance(arguments, Mapping) or set(arguments) != {
            "bill_codes",
            "status",
        }:
            raise _error("delivery projection arguments are invalid", "BROKER_ARGUMENT_INVALID")
        status = _text(arguments.get("status"), "status", maximum=32)
        if status != "signed":
            raise _error("delivery projection status is invalid", "BROKER_ARGUMENT_INVALID")
        bill_codes = _delivery_codes(arguments.get("bill_codes"))
        account_id = _account_id(context)
        _account_descriptor(self._ports, account_id, systems={"ronghui"})
        verified = _verified_result(
            self._ports.delivery_projection_update(
                bill_codes,
                status,
                context.mark_write_started,
            ),
            expected_count=len(bill_codes),
            context=context,
            codec=self._codec,
            purpose="delivery-projection-update",
            proof={
                "status": status,
                "identity_set_sha256": hashlib.sha256(canonical_json_bytes(sorted(bill_codes))).hexdigest(),
            },
        )
        return {
            **verified,
            "updated": verified["record_count"],
        }

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        return {
            ("network.request", "feishu.bitable.replace_snapshot"): (self.replace_site_bitable),
            ("network.request", "feishu.sheet.replace"): self.replace_site_sheet,
            ("network.request", "feishu.bitable.write_records"): (self.write_delivery_bitable),
            ("projection.invoke", "waybill.delivery_status.update"): (self.update_delivery_projection),
        }


def build_delivery_site_handler_map(
    ports: DeliverySiteHandlerPorts,
    *,
    cursor_secret: bytes,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    handlers = _DeliverySiteHandlers(ports, secret=cursor_secret).handler_map()
    if set(handlers) != set(DELIVERY_SITE_WRITE_ACTION_KEYS):
        raise ValueError("delivery/site handler action set changed")
    if SITE_WRITE_ACTION_KEYS & DELIVERY_WRITE_ACTION_KEYS:
        raise ValueError("delivery/site handler action sets overlap")
    return handlers


__all__ = [
    "DELIVERY_SITE_WRITE_ACTION_KEYS",
    "DELIVERY_WRITE_ACTION_KEYS",
    "MARKED_WRITE_ACTION_KEYS",
    "SITE_WRITE_ACTION_KEYS",
    "DeliverySiteHandlerPorts",
    "WriteStartMarker",
    "build_delivery_site_handler_map",
]
