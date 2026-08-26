"""Closed core primitives for signed first-party automation actions.

The replaceable package owns pagination loops, normalization, commit order and
result evidence.  This module exposes only exact page/resource/projection
operations.  It deliberately has no dependency on ``tools`` or ``feishu``;
the service composition root injects those infrastructure ports.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.core_adapter import (
    CoreBrokerHandler,
    CoreBrokerInvocationContext,
)
from agent.automation_plugins.broker import VERIFIED_WRITE_NOOP_FIELD
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.first_party_handler_common import (
    _CUSTOMER_IDENTITY_DOMAIN,
    _CUSTOMER_PUBLIC_FIELDS,
    _MAX_RECORDS,
    _SENSITIVE_KEY_MARKERS,
    _OpaqueCodec,
    _account_descriptor,
    _business_date,
    _clock_site,
    _committed_result_count,
    _contains_broker_owned_material,
    _customer_public_item,
    _declared_page_result,
    _encode_daily_sign_result,
    _error,
    _nonnegative_int,
    _one_account,
    _one_role_account,
    _optional_finite_number,
    _page_size,
    _page_state,
    _require_context,
    _scrub_business_value,
    _source_directions,
    _strict_arguments,
    _strict_record_list,
    _text,
    customer_problem_identity,
)
from agent.automation_plugins.manifest import canonical_json_bytes


AccountDescriptorPort = Callable[[str], Mapping[str, Any]]
CustomerActionPort = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ClockActionPort = Callable[[Mapping[str, Any]], Mapping[str, Any]]
DailySignSyncPort = Callable[
    [Mapping[str, Any], Mapping[str, str]],
    Mapping[str, Any],
]
PageReaderPort = Callable[[Mapping[str, Any], str, int, int], Mapping[str, Any]]
ProjectionReplacePort = Callable[[list[dict[str, Any]], str], Mapping[str, Any]]
ResourceReplacePort = Callable[[str, list[Any], str | None], Mapping[str, Any]]
RecordReadPort = Callable[[Mapping[str, Any], str], Mapping[str, Any] | None]
SnapshotReadPort = Callable[[str], Sequence[Mapping[str, Any]]]
IdentityReadPort = Callable[[str], Sequence[str]]
SnapshotCleanupPort = Callable[[int], Mapping[str, Any]]
ResourceRecordReplacePort = Callable[
    [str, str, list[dict[str, Any]], str], Mapping[str, Any]
]
ResourceArchivePort = Callable[[str, list[dict[str, Any]], str], Mapping[str, Any]]
YundaDispatchPagePort = Callable[
    [Mapping[str, Any], str, str, int, int], Mapping[str, Any]
]
YundaSendPagePort = Callable[
    [Mapping[str, Any], str, int, int], Mapping[str, Any]
]
YundaRecordReadPort = Callable[
    [Mapping[str, Any], str], Mapping[str, Any]
]
YundaRendererReadPort = Callable[
    [Mapping[str, Any], str, str], Mapping[str, Any]
]
YundaResourceCommitPort = Callable[
    [str, list[dict[str, Any]], str, bool], Mapping[str, Any]
]
YundaProjectionCommitPort = Callable[
    [list[dict[str, Any]], str], Mapping[str, Any]
]
DeliveryViewListPort = Callable[[str], Sequence[Mapping[str, Any]]]
DeliveryRecordPagePort = Callable[[str, str, int, int], Mapping[str, Any]]
DeliveryStatusReadPort = Callable[
    [Mapping[str, Any], list[str]], Sequence[Mapping[str, Any]]
]
DeliveryRecordWritePort = Callable[
    [str, list[dict[str, str]]], Mapping[str, Any]
]
DeliveryProjectionPort = Callable[
    [list[str], str], Mapping[str, Any]
]
ScanNextSubmitPort = Callable[
    [Mapping[str, Any], list[dict[str, str]]], Mapping[str, Any]
]
ScanNextVerifyPort = Callable[
    [Mapping[str, Any], list[dict[str, str]], str, str], Mapping[str, Any]
]


@dataclass(frozen=True)
class FirstPartyCoreHandlerPorts:
    """Infrastructure callbacks available to the closed handler set.

    Optional callbacks keep unavailable business actions fail-closed: the
    corresponding operation/action pair is not registered at all.
    """

    describe_account: AccountDescriptorPort
    customer_action: CustomerActionPort | None = None
    clock_action: ClockActionPort | None = None
    daily_sign_sync: DailySignSyncPort | None = None
    arrive_list_read_page: PageReaderPort | None = None
    site_send_read_page: PageReaderPort | None = None
    replace_waybill_snapshot: ProjectionReplacePort | None = None
    replace_arrival_forecast_snapshot: ProjectionReplacePort | None = None
    replace_arrive_sheet_resource: ResourceReplacePort | None = None
    replace_sheet_resource: ResourceReplacePort | None = None
    replace_bitable_resource: ResourceReplacePort | None = None
    scan_read_page: PageReaderPort | None = None
    waybill_detail_read: RecordReadPort | None = None
    child_count_read: RecordReadPort | None = None
    replace_scan_snapshot: ProjectionReplacePort | None = None
    scan_next_submit: ScanNextSubmitPort | None = None
    scan_next_verify: ScanNextVerifyPort | None = None
    read_scan_snapshot: SnapshotReadPort | None = None
    cleanup_scan_snapshot: SnapshotCleanupPort | None = None
    read_completed_arrivals_before: IdentityReadPort | None = None
    read_pending_waybills: SnapshotReadPort | None = None
    replace_arrival_snapshot: ProjectionReplacePort | None = None
    refresh_split_pending_snapshot: ProjectionReplacePort | None = None
    replace_arrival_stats_sheet: ResourceRecordReplacePort | None = None
    archive_arrival_stats_sheet: ResourceArchivePort | None = None
    yunda_dispatch_read_page: YundaDispatchPagePort | None = None
    yunda_send_read_page: YundaSendPagePort | None = None
    yunda_special_line_read_page: YundaSendPagePort | None = None
    yunda_tracking_detail_read: YundaRecordReadPort | None = None
    yunda_original_data_read: YundaRecordReadPort | None = None
    yunda_renderer_detail_read: YundaRendererReadPort | None = None
    append_yunda_dispatch_bitable: YundaResourceCommitPort | None = None
    replace_yunda_send_bitable: YundaResourceCommitPort | None = None
    replace_yunda_send_sheet: YundaResourceCommitPort | None = None
    replace_yunda_waybill_projection: YundaProjectionCommitPort | None = None
    delivery_list_views: DeliveryViewListPort | None = None
    delivery_list_records: DeliveryRecordPagePort | None = None
    delivery_status_read: DeliveryStatusReadPort | None = None
    delivery_write_records: DeliveryRecordWritePort | None = None
    delivery_projection_update: DeliveryProjectionPort | None = None


_CUSTOMER_TOOL = "sync_customer_service_problems"
_CLOCK_TOOL = "clock_in_dual"
_DAILY_SIGN_TOOL = "sync_daily_should_sign"
_ARRIVE_TOOL = "sync_arrive_list"
_ARRIVAL_STATS_TOOL = "sync_arrival_stats"
_SCAN_TOOL = "sync_scan_codes"
_SITE_SEND_TOOL = "sync_site_send_list"
_SITE_SEND_BITABLE_ROLE = "site_send_bitable"
_SITE_SEND_SHEET_ROLE = "site_send_sheet"
_YUNDA_DISPATCH_TOOL = "sync_yunda_dispatch_forecast"
_YUNDA_SEND_TOOL = "sync_yunda_send_waybills"
_DELIVERY_TOOL = "sync_delivery_status"
_DELIVERY_RESOURCE_ROLE = "delivery_status_bitable"
_CUSTOMER_DIRECTIONS = frozenset({"received", "published", "both"})
_DAILY_SIGN_ARGUMENT_FIELDS = frozenset(
    {
        "enrich_addresses",
        "days",
        "source_start",
        "source_end",
        "problem_page_size",
        "problem_max_pages",
        "problem_retry_attempts",
        "problem_timeout_sec",
        "sign_page_size",
        "sign_max_pages",
        "sign_chunk_days",
        "sign_retry_attempts",
        "sign_timeout_sec",
        "waybill_timeout_sec",
        "browser_batch_size",
        "sign_site_code",
        "exact_sign_conflict_limit",
        "exact_historical_sign_limit",
    }
)
_ARRIVE_FIELDS = (
    "tracking_number",
    "goods_name",
    "package_type",
    "delivery_method",
    "quantity",
    "receipt_number",
    "actual_weight",
    "volume",
    "remarks",
    "destination_station",
    "recipient_name",
    "recipient_phone",
    "recipient_address",
    "settlement_weight",
    "volumetric_weight",
    "shipping_fee",
    "payment_type",
    "pay_on_arrival",
)
_SITE_FIELDS = (
    "tracking_number",
    "send_site",
    "package_type",
    "destination",
    "pieces",
    "weight",
)
_SCAN_SOURCE_FIELDS = (
    "bill_code",
    "destination",
    "scan_type",
    "scan_time",
    "scan_site",
)
_SCAN_SNAPSHOT_FIELDS = (
    "raw_code",
    "destination",
    "code_type",
    "main_tracking",
)
_SCAN_NEXT_FIELDS = ("bill_code", "station_name")
_SCAN_READ_FIELDS = (
    "raw_code",
    "destination",
    "code_type",
)
_ARRIVAL_STATS_FIELDS = (*_ARRIVE_FIELDS, "arrived_quantity")
_ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS = frozenset({"receipt_number", "remarks"})
_ARRIVAL_SNAPSHOT_FIELDS = (
    "tracking_number",
    "destination_station",
    "expected_quantity",
    "arrived_quantity",
    "goods_name",
    "package_type",
    "delivery_method",
    "recipient_address",
)
_PENDING_FIELDS = (
    "tracking_number",
    "destination_station",
    "expected_quantity",
    "arrived_quantity",
    "pending_quantity",
    "first_arrival_at",
    "last_arrival_at",
    "arrival_status",
)


def _arrival_stats_v1_records(
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
        if not isinstance(raw, Mapping):
            raise _error(f"{label} record schema is invalid", "BROKER_ARGUMENT_INVALID")
        keys = set(raw)
        missing = required - keys
        if keys - required or not missing.issubset(_ARRIVAL_STATS_V1_OPTIONAL_EMPTY_FIELDS):
            raise _error(f"{label} record schema is invalid", "BROKER_ARGUMENT_INVALID")
        output.append({**dict(raw), **{field: "" for field in missing}})
    return output
_YUNDA_DISPATCH_FIELDS = (
    "主单号",
    "开单件数",
    "扫描件数",
    "重量/kg",
    "体积/m3",
    "包装类型",
    "清场时间",
    "规划时效",
    "开单目的地址",
    "预计到达时间",
    "应派时间",
)
_YUNDA_SEND_FIELDS = (
    "5.14编号",
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
    "现付",
    "月结",
    "提付",
    "中转运费",
    "回单号",
    "备注",
    "结算重量",
    "体积",
    "支付类型",
    "体积重",
    "到付款",
    "日期",
)
_YUNDA_DISPATCH_RESOURCE_ROLE = "dispatch_forecast_bitable"
_YUNDA_SEND_BITABLE_ROLE = "send_waybills_bitable"
_YUNDA_SEND_SHEET_ROLE = "send_waybills_sheet"

# This closed declaration is checked against the signed release contract. A
# newly registered write must be named here and call ``_mark_write_started``
# at the actual mutating port boundary.
MARKED_WRITE_ACTION_KEYS = frozenset(
    {
        ("browser.invoke", "ronghui.clock.submit"),
        ("ledger.invoke", "daily_sign.authoritative_sync"),
        ("projection.invoke", "scan.snapshot.replace"),
        ("projection.invoke", "scan.snapshot.cleanup"),
        ("projection.invoke", "waybill.snapshot.replace"),
        ("projection.invoke", "arrival.forecast_snapshot.replace"),
        ("projection.invoke", "arrival.snapshot.replace"),
        ("projection.invoke", "split_pending.snapshot.refresh"),
        ("network.request", "feishu.sheet.replace"),
        ("network.request", "feishu.sheet.add"),
        ("browser.invoke", "ronghui.scan_next.submit"),
        ("network.request", "feishu.bitable.write_records"),
        ("network.request", "feishu.bitable.replace_snapshot"),
        ("network.request", "feishu.bitable.append_yunda_dispatch_forecast"),
        ("network.request", "feishu.bitable.replace_yunda_send_waybills_date"),
        ("network.request", "feishu.sheet.replace_yunda_send_waybills"),
        ("projection.invoke", "waybill.yunda.replace_date"),
        ("projection.invoke", "waybill.delivery_status.update"),
    }
)


class _FirstPartyCoreHandlers:
    def __init__(
        self,
        ports: FirstPartyCoreHandlerPorts,
        *,
        secret: bytes,
    ) -> None:
        self._ports = ports
        self._codec = _OpaqueCodec(secret)

    @staticmethod
    def _mark_write_started(context: CoreBrokerInvocationContext) -> None:
        """Persist the one-shot attempt receipt at the write boundary."""

        if context.mark_write_started is not None:
            context.mark_write_started()

    def _clock_context(
        self,
        context: CoreBrokerInvocationContext,
    ) -> tuple[str, dict[str, Any]]:
        _require_context(context, tool_name=_CLOCK_TOOL, role="account_id")
        if self._ports.clock_action is None:
            raise _error(
                "clock primitive is unavailable",
                "BROKER_ACTION_UNAVAILABLE",
            )
        account_id = _one_account(context)
        descriptor = _account_descriptor(
            self._ports,
            account_id,
            systems={"ronghui"},
        )
        return account_id, descriptor

    def clock_precheck(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        account_id, descriptor = self._clock_context(context)
        values = _strict_arguments(arguments, {"site", "clock_types"})
        site = _clock_site(values.get("site"))
        raw_types = values.get("clock_types")
        if not isinstance(raw_types, list) or len(raw_types) != 2:
            raise _error("clock types are invalid", "BROKER_ARGUMENT_INVALID")
        clock_types = [_text(item, "clock_type", maximum=32) for item in raw_types]
        if len(set(clock_types)) != 2:
            raise _error("clock types must be distinct", "BROKER_ARGUMENT_INVALID")
        assert self._ports.clock_action is not None
        raw = self._ports.clock_action(
            {
                "action": "precheck",
                "account_id": account_id,
                "session_profile": str(descriptor.get("session_profile") or ""),
                "site": site,
                "clock_types": clock_types,
            }
        )
        if not isinstance(raw, Mapping) or raw.get("ready") is not True:
            raise _error("clock precheck failed", "BROKER_SOURCE_INVALID")
        proof = {
            "ready": True,
            "site_sha256": hashlib.sha256(canonical_json_bytes(site)).hexdigest(),
            "clock_types": clock_types,
        }
        return {
            "ready": True,
            "evidence_ref": self._codec.evidence(
                context,
                "clock-precheck",
                proof,
            ),
        }

    def clock_submit(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        account_id, descriptor = self._clock_context(context)
        values = _strict_arguments(arguments, {"clock_type", "site"})
        clock_type = _text(values.get("clock_type"), "clock_type", maximum=32)
        site = _clock_site(values.get("site"))
        assert self._ports.clock_action is not None
        self._mark_write_started(context)
        raw = self._ports.clock_action(
            {
                "action": "submit",
                "account_id": account_id,
                "session_profile": str(descriptor.get("session_profile") or ""),
                "site": site,
                "clock_type": clock_type,
            }
        )
        if not isinstance(raw, Mapping) or raw.get("accepted") is not True:
            raise _error("clock submit was not accepted", "BROKER_WRITE_FAILED")
        submitted_at = _text(raw.get("submitted_at"), "submitted_at", maximum=32)
        try:
            datetime.strptime(submitted_at, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise _error(
                "clock submit timestamp is invalid",
                "BROKER_SOURCE_INVALID",
            ) from exc
        operation_payload = {
            "v": 1,
            "clock_type": clock_type,
            "site": site,
            "submitted_at": submitted_at,
        }
        operation_id = self._codec.encode(
            context,
            "clock-operation",
            operation_payload,
        )
        proof = {
            "accepted": True,
            "operation_sha256": hashlib.sha256(
                operation_id.encode("ascii")
            ).hexdigest(),
        }
        return {
            "accepted": True,
            "operation_id": operation_id,
            "evidence_ref": self._codec.evidence(
                context,
                "clock-submit",
                proof,
            ),
        }

    def clock_verify(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        account_id, descriptor = self._clock_context(context)
        values = _strict_arguments(
            arguments,
            {"clock_type", "operation_id", "site"},
        )
        clock_type = _text(values.get("clock_type"), "clock_type", maximum=32)
        site = _clock_site(values.get("site"))
        operation = self._codec.decode(
            context,
            "clock-operation",
            values.get("operation_id"),
        )
        if (
            operation.get("v") != 1
            or operation.get("clock_type") != clock_type
            or operation.get("site") != site
        ):
            raise _error(
                "clock operation does not match the verification request",
                "BROKER_CURSOR_INVALID",
            )
        submitted_at = _text(
            operation.get("submitted_at"),
            "submitted_at",
            maximum=32,
        )
        assert self._ports.clock_action is not None
        raw = self._ports.clock_action(
            {
                "action": "verify",
                "account_id": account_id,
                "session_profile": str(descriptor.get("session_profile") or ""),
                "site": site,
                "clock_type": clock_type,
                "submitted_at": submitted_at,
            }
        )
        if not isinstance(raw, Mapping) or raw.get("confirmed") is not True:
            raise _error(
                "clock write was not confirmed by a fresh read",
                "WRITE_OUTCOME_UNKNOWN",
            )
        if _text(raw.get("clock_type"), "clock_type", maximum=32) != clock_type:
            raise _error(
                "clock verification returned another operation type",
                "BROKER_SOURCE_INVALID",
            )
        observed_at = _text(raw.get("observed_at"), "observed_at", maximum=32)
        record_id = _text(raw.get("record_id"), "record_id", maximum=256)
        proof = {
            "confirmed": True,
            "clock_type": clock_type,
            "observed_at": observed_at,
            "record_sha256": hashlib.sha256(record_id.encode("utf-8")).hexdigest(),
        }
        return {
            "confirmed": True,
            "clock_type": clock_type,
            "observed_at": observed_at,
            "evidence_ref": self._codec.evidence(
                context,
                "clock-verify",
                proof,
            ),
        }

    def daily_sign_authoritative_sync(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_DAILY_SIGN_TOOL, role="account_id")
        if self._ports.daily_sign_sync is None:
            raise _error(
                "daily-sign authoritative primitive is unavailable",
                "BROKER_ACTION_UNAVAILABLE",
            )
        values = _strict_arguments(arguments, set(_DAILY_SIGN_ARGUMENT_FIELDS))
        if _contains_broker_owned_material(values):
            raise _error(
                "daily-sign arguments contain broker-owned material",
                "BROKER_ARGUMENT_INVALID",
            )
        r13_account_id = _one_role_account(context, "r13_account_id")
        account_id = _one_role_account(context, "account_id")
        _account_descriptor(self._ports, r13_account_id, systems={"r13"})
        _account_descriptor(self._ports, account_id, systems={"ronghui"})
        daily_resources = {
            role: str(context.resource_bindings.get(role) or "").strip()
            for role in ("daily_sign_bitable", "daily_sign_sheet")
        }
        if any(not resource_id for resource_id in daily_resources.values()):
            raise _error(
                "daily-sign managed resources are not bound",
                "BROKER_RESOURCE_UNAVAILABLE",
            )
        self._mark_write_started(context)
        authoritative = self._ports.daily_sign_sync(
            {
                **values,
                "r13_account_id": r13_account_id,
                "account_id": account_id,
            },
            daily_resources,
        )
        encoded = _encode_daily_sign_result(authoritative)
        data = encoded["data"]
        meta = encoded["meta"]
        proof = {
            "status": encoded["status"],
            "account_bindings_sha256": hashlib.sha256(
                canonical_json_bytes(
                    {
                        "r13_account_id": r13_account_id,
                        "account_id": account_id,
                    }
                )
            ).hexdigest(),
            "resource_bindings_sha256": hashlib.sha256(
                canonical_json_bytes(daily_resources)
            ).hexdigest(),
            "source_run_id": (
                meta.get("source_run_id")
                or data.get("source_run_id")
            ),
            "record_count": meta.get("record_count"),
            "pagination_complete": meta.get("pagination_complete"),
            "postconditions": meta.get("postconditions"),
        }
        return {
            "result": encoded,
            "evidence_ref": self._codec.evidence(
                context,
                "daily-sign-authoritative-sync",
                proof,
            ),
        }

    def customer_list_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_CUSTOMER_TOOL, role="customer_service_source")
        if self._ports.customer_action is None or not context.account_ids:
            raise _error("customer source primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"direction", "cursor", "page_size"})
        direction = str(values.get("direction") or "").strip().lower()
        if direction not in _CUSTOMER_DIRECTIONS:
            raise _error("customer direction is invalid", "BROKER_ARGUMENT_INVALID")
        size = _page_size(values.get("page_size"))
        sources: list[tuple[str, dict[str, Any], str, str]] = []
        for account_id in context.account_ids:
            descriptor = _account_descriptor(
                self._ports,
                account_id,
                systems={"ronghui", "yunda"},
            )
            platform = str(descriptor["system"]).strip().lower()
            for source_direction in _source_directions(platform, direction):
                sources.append((account_id, descriptor, platform, source_direction))
        state = _page_state(
            self._codec,
            context,
            "customer-list",
            values.get("cursor"),
            initial={"v": 1, "source": 0, "page": 1, "count": 0, "total": None},
        )
        try:
            source_index = int(state["source"])
            page_number = int(state["page"])
            cumulative = int(state["count"])
            expected_total = state.get("total")
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("customer cursor state is invalid", "BROKER_CURSOR_INVALID") from exc
        if (
            not 0 <= source_index < len(sources)
            or not 1 <= page_number <= 10_000
            or cumulative < 0
            or (expected_total is not None and (isinstance(expected_total, bool) or not isinstance(expected_total, int)))
        ):
            raise _error("customer cursor state is invalid", "BROKER_CURSOR_INVALID")
        account_id, descriptor, platform, source_direction = sources[source_index]
        raw = self._ports.customer_action(
            {
                "platform": platform,
                "account_id": account_id,
                "account_label": account_id,
                "session_profile": str(descriptor.get("session_profile") or ""),
                "action": "query",
                "direction": source_direction,
                "filters": {
                    "direction": source_direction,
                    "page": page_number,
                    "rows": size,
                },
            }
        )
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            code = str(raw.get("error_code") or "BROKER_SOURCE_FAILED") if isinstance(raw, Mapping) else "BROKER_SOURCE_INVALID"
            raise _error("customer source query failed", code[:64])
        rows = raw.get("rows")
        stats = raw.get("stats")
        if not isinstance(rows, list) or not isinstance(stats, Mapping) or any(not isinstance(row, Mapping) for row in rows):
            raise _error("customer source response is invalid", "BROKER_SOURCE_INVALID")
        returned = stats.get("returned")
        if isinstance(returned, bool) or not isinstance(returned, int) or returned != len(rows):
            raise _error("customer source returned count is invalid", "BROKER_SOURCE_INVALID")
        total_authoritative = stats.get("total_authoritative") is True
        declared_total = stats.get("total")
        page_items, current_total, next_count, source_complete = _declared_page_result(
            {
                "items": rows,
                "returned": returned,
                "total": declared_total,
                "total_authoritative": total_authoritative,
            },
            expected_total=expected_total,
            cumulative=cumulative,
            page_size=size,
        )
        public_items = [
            _customer_public_item(
                self._codec,
                context,
                dict(row),
                account_id=account_id,
                platform=platform,
                source_direction=source_direction,
            )
            for row in page_items
        ]
        final = source_complete and source_index + 1 == len(sources)
        next_cursor: str | None = None
        if not final:
            next_state = (
                {"v": 1, "source": source_index + 1, "page": 1, "count": 0, "total": None}
                if source_complete
                else {
                    "v": 1,
                    "source": source_index,
                    "page": page_number + 1,
                    "count": next_count,
                    "total": current_total,
                }
            )
            next_cursor = self._codec.encode(context, "customer-list", next_state)
        evidence_payload = {
            "source": source_index,
            "page": page_number,
            "returned": returned,
            "pagination_complete": final,
        }
        return {
            "items": public_items,
            "next_cursor": next_cursor,
            "pagination_complete": final,
            "evidence_ref": self._codec.evidence(context, "customer-list-page", evidence_payload),
        }

    def customer_detail(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_CUSTOMER_TOOL, role="customer_service_source")
        if self._ports.customer_action is None or not context.account_ids:
            raise _error("customer detail primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(
            arguments,
            {"dedupe_key", "platform", "source_direction", "external_id", "waybill_no"},
        )
        dedupe_key = _text(values.get("dedupe_key"), "dedupe_key", maximum=128)
        platform = _text(values.get("platform"), "platform", maximum=32).lower()
        source_direction = _text(values.get("source_direction"), "source_direction", maximum=32).lower()
        external_id = _text(values.get("external_id"), "external_id", maximum=256)
        matches: list[tuple[str, dict[str, Any]]] = []
        for account_id in context.account_ids:
            descriptor = _account_descriptor(
                self._ports,
                account_id,
                systems={"ronghui", "yunda"},
            )
            if str(descriptor["system"]).strip().lower() != platform:
                continue
            expected = self._codec.identity(
                context,
                account_id=account_id,
                platform=platform,
                external_id=external_id,
            )
            if hmac.compare_digest(dedupe_key, expected):
                matches.append((account_id, descriptor))
        if len(matches) != 1:
            raise _error("customer detail identity is not bound uniquely", "BROKER_SOURCE_IDENTITY_MISMATCH")
        account_id, descriptor = matches[0]
        item = {
            "platform": platform,
            "source_direction": source_direction,
            "external_id": external_id,
        }
        waybill_no = str(values.get("waybill_no") or "").strip()
        if waybill_no:
            if len(waybill_no) > 256:
                raise _error("waybill_no is invalid", "BROKER_ARGUMENT_INVALID")
            item["waybill_no"] = waybill_no
        raw = self._ports.customer_action(
            {
                "platform": platform,
                "account_id": account_id,
                "account_label": account_id,
                "session_profile": str(descriptor.get("session_profile") or ""),
                "action": "detail",
                "direction": source_direction,
                "item": item,
            }
        )
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            code = str(raw.get("error_code") or "BROKER_SOURCE_FAILED") if isinstance(raw, Mapping) else "BROKER_SOURCE_INVALID"
            raise _error("customer detail query failed", code[:64])
        business = _scrub_business_value(raw)
        if not isinstance(business, dict):
            raise _error("customer detail response is invalid", "BROKER_SOURCE_INVALID")
        business.update(
            {
                "dedupe_key": dedupe_key,
                "platform": platform,
                "source_direction": source_direction,
                "external_id": external_id,
            }
        )
        business["evidence_ref"] = self._codec.evidence(
            context,
            "customer-detail",
            {
                "dedupe_key": dedupe_key,
                "source_returned": True,
                "detail_digest": hashlib.sha256(canonical_json_bytes(business)).hexdigest(),
            },
        )
        return business

    def _read_single_account_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
        *,
        tool_name: str,
        purpose: str,
        reader: PageReaderPort | None,
        target_date_required: bool,
        authoritative_total_required: bool = False,
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=tool_name, role="account_id")
        if reader is None:
            raise _error("source page primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        allowed = {"cursor", "page_size"} | ({"target_date"} if target_date_required else set())
        values = _strict_arguments(arguments, allowed)
        size = _page_size(values.get("page_size"))
        target_date = (
            _business_date(values.get("target_date"))
            if target_date_required
            else date.today().isoformat()
        )
        state = _page_state(
            self._codec,
            context,
            purpose,
            values.get("cursor"),
            initial={"v": 1, "page": 0, "count": 0, "total": None, "date": target_date},
        )
        try:
            page_index = int(state["page"])
            cumulative = int(state["count"])
            expected_total = state.get("total")
            bound_date = _business_date(state.get("date"))
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("source cursor state is invalid", "BROKER_CURSOR_INVALID") from exc
        if bound_date != target_date or not 0 <= page_index <= 10_000 or cumulative < 0:
            raise _error("source cursor state is invalid", "BROKER_CURSOR_INVALID")
        if expected_total is not None and (
            isinstance(expected_total, bool) or not isinstance(expected_total, int) or expected_total < 0
        ):
            raise _error("source cursor total is invalid", "BROKER_CURSOR_INVALID")
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"ronghui"})
        raw = reader(descriptor, bound_date, page_index, size)
        if not isinstance(raw, Mapping):
            raise _error("source page adapter returned an invalid response", "BROKER_SOURCE_INVALID")
        if authoritative_total_required and raw.get("total_authoritative") is not True:
            raise _error(
                "scan source must declare an authoritative total",
                "BROKER_SOURCE_TOTAL_REQUIRED",
            )
        items, total, next_count, complete = _declared_page_result(
            raw,
            expected_total=expected_total,
            cumulative=cumulative,
            page_size=size,
        )
        if any(not isinstance(item, (Mapping, list, tuple)) for item in items):
            raise _error("source page contains an invalid row", "BROKER_SOURCE_INVALID")
        next_cursor = None
        if not complete:
            next_cursor = self._codec.encode(
                context,
                purpose,
                {
                    "v": 1,
                    "page": page_index + 1,
                    "count": next_count,
                    "total": total,
                    "date": bound_date,
                },
            )
        safe_items = _scrub_business_value(items)
        evidence_payload = {
            "page": page_index,
            "returned": raw.get("returned"),
            "pagination_complete": complete,
            "target_date": bound_date,
        }
        return {
            "items": safe_items,
            "next_cursor": next_cursor,
            "pagination_complete": complete,
            "evidence_ref": self._codec.evidence(context, f"{purpose}-page", evidence_payload),
        }

    def arrive_list_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if context.tool_name not in {_ARRIVE_TOOL, _ARRIVAL_STATS_TOOL}:
            raise _error(
                "arrive-list page is not valid for this signed tool",
                "BROKER_CONTEXT_INVALID",
            )
        return self._read_single_account_page(
            context,
            arguments,
            tool_name=context.tool_name,
            purpose=(
                "arrival-stats-arrive-list"
                if context.tool_name == _ARRIVAL_STATS_TOOL
                else "arrive-list"
            ),
            reader=self._ports.arrive_list_read_page,
            target_date_required=True,
        )

    def scan_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if context.tool_name not in {_ARRIVAL_STATS_TOOL, _SCAN_TOOL}:
            raise _error(
                "scan page is not valid for this signed tool",
                "BROKER_CONTEXT_INVALID",
            )
        result = self._read_single_account_page(
            context,
            arguments,
            tool_name=context.tool_name,
            purpose=(
                "scan-sync-source"
                if context.tool_name == _SCAN_TOOL
                else "arrival-stats-scan"
            ),
            reader=self._ports.scan_read_page,
            target_date_required=True,
            authoritative_total_required=context.tool_name == _SCAN_TOOL,
        )
        result["items"] = _strict_record_list(
            result["items"],
            fields=_SCAN_SOURCE_FIELDS,
            label="scan source",
        )
        return result

    def site_send_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        values = _strict_arguments(arguments, {"cursor", "page_size", "target_date"})
        target_date = _business_date(values.get("target_date"))
        result = self._read_single_account_page(
            context,
            {
                "cursor": values.get("cursor"),
                "page_size": values.get("page_size"),
                "target_date": target_date,
            },
            tool_name=_SITE_SEND_TOOL,
            purpose="site-send",
            reader=self._ports.site_send_read_page,
            target_date_required=True,
        )
        result["items"] = _strict_record_list(
            result["items"],
            fields=_SITE_FIELDS,
            label="site-send source",
        )
        result["target_date"] = target_date
        return result

    def delivery_list_views(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_DELIVERY_TOOL,
            role=_DELIVERY_RESOURCE_ROLE,
        )
        values = _strict_arguments(arguments, set())
        if values:
            raise _error("delivery view arguments are invalid", "BROKER_ARGUMENT_INVALID")
        resource_id = str(context.resource_id or "").strip()
        if not resource_id or self._ports.delivery_list_views is None:
            raise _error(
                "delivery Bitable view primitive is unavailable",
                "BROKER_ACTION_UNAVAILABLE",
            )
        raw = self._ports.delivery_list_views(resource_id)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) > 1000:
            raise _error("delivery Bitable view list is invalid", "BROKER_SOURCE_INVALID")
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in raw:
            if not isinstance(value, Mapping) or set(value) != {"view_id", "view_name"}:
                raise _error("delivery Bitable view schema is invalid", "BROKER_SOURCE_INVALID")
            view_id = _text(value.get("view_id"), "view_id", maximum=128)
            view_name = _text(value.get("view_name"), "view_name", maximum=128)
            if view_id in seen:
                raise _error("delivery Bitable view identity is duplicated", "BROKER_SOURCE_INVALID")
            seen.add(view_id)
            items.append({"view_id": view_id, "view_name": view_name})
        proof = {
            "resource_id": resource_id,
            "view_count": len(items),
        }
        return {
            "items": items,
            "evidence_ref": self._codec.evidence(context, "delivery-view-list", proof),
        }

    def delivery_list_records(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_DELIVERY_TOOL,
            role=_DELIVERY_RESOURCE_ROLE,
        )
        resource_id = str(context.resource_id or "").strip()
        reader = self._ports.delivery_list_records
        if not resource_id or reader is None:
            raise _error(
                "delivery Bitable record primitive is unavailable",
                "BROKER_ACTION_UNAVAILABLE",
            )
        values = _strict_arguments(arguments, {"view_id", "cursor", "page_size"})
        view_id = _text(values.get("view_id"), "view_id", maximum=128)
        size = _page_size(values.get("page_size"))
        state = _page_state(
            self._codec,
            context,
            "delivery-records",
            values.get("cursor"),
            initial={"v": 1, "page": 0, "count": 0, "total": None, "view_id": view_id},
        )
        try:
            page_index = int(state["page"])
            cumulative = int(state["count"])
            expected_total = state.get("total")
            bound_view_id = _text(state.get("view_id"), "view_id", maximum=128)
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("delivery record cursor is invalid", "BROKER_CURSOR_INVALID") from exc
        if (
            bound_view_id != view_id
            or not 0 <= page_index <= 10_000
            or cumulative < 0
            or (
                expected_total is not None
                and (
                    isinstance(expected_total, bool)
                    or not isinstance(expected_total, int)
                    or expected_total < 0
                )
            )
        ):
            raise _error("delivery record cursor is invalid", "BROKER_CURSOR_INVALID")
        raw = reader(resource_id, bound_view_id, page_index, size)
        if not isinstance(raw, Mapping):
            raise _error("delivery Bitable page is invalid", "BROKER_SOURCE_INVALID")
        page_items, total, next_count, complete = _declared_page_result(
            raw,
            expected_total=expected_total,
            cumulative=cumulative,
            page_size=size,
        )
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in page_items:
            if not isinstance(value, Mapping) or set(value) != {
                "record_id",
                "waybill_no",
                "status",
            }:
                raise _error("delivery Bitable record schema is invalid", "BROKER_SOURCE_INVALID")
            record_id = _text(value.get("record_id"), "record_id", maximum=128)
            waybill_no = str(value.get("waybill_no") or "").strip()
            status = str(value.get("status") or "").strip()
            if len(waybill_no) > 128 or len(status) > 128:
                raise _error("delivery Bitable record is invalid", "BROKER_SOURCE_INVALID")
            if record_id in seen:
                raise _error("delivery Bitable page has duplicate records", "BROKER_SOURCE_INVALID")
            seen.add(record_id)
            items.append(
                {
                    "record_id": record_id,
                    "waybill_no": waybill_no,
                    "status": status,
                }
            )
        next_cursor = None
        if not complete:
            next_cursor = self._codec.encode(
                context,
                "delivery-records",
                {
                    "v": 1,
                    "page": page_index + 1,
                    "count": next_count,
                    "total": total,
                    "view_id": bound_view_id,
                },
            )
        proof = {
            "resource_id": resource_id,
            "view_id": bound_view_id,
            "page": page_index,
            "returned": len(items),
            "pagination_complete": complete,
        }
        return {
            "items": items,
            "next_cursor": next_cursor,
            "pagination_complete": complete,
            "evidence_ref": self._codec.evidence(context, "delivery-record-page", proof),
        }

    def delivery_status_read(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_DELIVERY_TOOL, role="account_id")
        reader = self._ports.delivery_status_read
        if reader is None:
            raise _error("delivery source primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"bill_codes"})
        raw_codes = values.get("bill_codes")
        if not isinstance(raw_codes, list) or not 1 <= len(raw_codes) <= 200:
            raise _error("delivery source bill codes are invalid", "BROKER_ARGUMENT_INVALID")
        bill_codes = [
            _text(value, "bill_code", maximum=128)
            for value in raw_codes
        ]
        if len(bill_codes) != len(set(bill_codes)):
            raise _error("delivery source bill codes are duplicated", "BROKER_ARGUMENT_INVALID")
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"ronghui"})
        raw = reader(descriptor, bill_codes)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) > len(bill_codes):
            raise _error("delivery source response is invalid", "BROKER_SOURCE_INVALID")
        requested = set(bill_codes)
        items: list[dict[str, str]] = []
        seen: dict[str, str] = {}
        for value in raw:
            if not isinstance(value, Mapping) or set(value) != {"bill_code", "status"}:
                raise _error("delivery source row schema is invalid", "BROKER_SOURCE_INVALID")
            code = _text(value.get("bill_code"), "bill_code", maximum=128)
            status = _text(value.get("status"), "status", maximum=128)
            if code not in requested:
                raise _error("delivery source changed a bill identity", "BROKER_SOURCE_INVALID")
            if code in seen and seen[code] != status:
                raise _error("delivery source returned conflicting statuses", "BROKER_SOURCE_CHANGED")
            if code not in seen:
                seen[code] = status
                items.append({"bill_code": code, "status": status})
        proof = {
            "requested": len(bill_codes),
            "returned": len(items),
        }
        return {
            "items": items,
            "evidence_ref": self._codec.evidence(context, "delivery-status-read", proof),
        }

    def delivery_write_records(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_DELIVERY_TOOL,
            role=_DELIVERY_RESOURCE_ROLE,
        )
        resource_id = str(context.resource_id or "").strip()
        writer = self._ports.delivery_write_records
        if not resource_id or writer is None:
            raise _error(
                "delivery Bitable write primitive is unavailable",
                "BROKER_ACTION_UNAVAILABLE",
            )
        values = _strict_arguments(arguments, {"records"})
        raw_records = values.get("records")
        if not isinstance(raw_records, list) or not 1 <= len(raw_records) <= _MAX_RECORDS:
            raise _error("delivery Bitable writes are invalid", "BROKER_ARGUMENT_INVALID")
        records: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in raw_records:
            if not isinstance(value, Mapping) or set(value) != {"record_id", "status"}:
                raise _error("delivery Bitable write schema is invalid", "BROKER_ARGUMENT_INVALID")
            record_id = _text(value.get("record_id"), "record_id", maximum=128)
            status = _text(value.get("status"), "status", maximum=128)
            if record_id in seen:
                raise _error("delivery Bitable writes contain duplicates", "BROKER_ARGUMENT_INVALID")
            seen.add(record_id)
            records.append({"record_id": record_id, "status": status})
        self._mark_write_started(context)
        raw = writer(resource_id, records)
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            raise _error("delivery Bitable write failed", "BROKER_RESOURCE_WRITE_FAILED")
        written = raw.get("record_count", raw.get("written"))
        if isinstance(written, bool) or not isinstance(written, int) or written != len(records):
            raise _error("delivery Bitable write count changed", "BROKER_RESOURCE_WRITE_MISMATCH")
        proof = {
            "resource_id": resource_id,
            "record_count": len(records),
            "committed": True,
        }
        return {
            "committed": True,
            "written": written,
            "evidence_ref": self._codec.evidence(context, "delivery-record-write", proof),
        }

    def delivery_projection_update(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_DELIVERY_TOOL, role="account_id")
        writer = self._ports.delivery_projection_update
        if writer is None:
            raise _error(
                "delivery projection primitive is unavailable",
                "BROKER_ACTION_UNAVAILABLE",
            )
        values = _strict_arguments(arguments, {"bill_codes", "status"})
        raw_codes = values.get("bill_codes")
        if not isinstance(raw_codes, list) or not 1 <= len(raw_codes) <= _MAX_RECORDS:
            raise _error("delivery projection bill codes are invalid", "BROKER_ARGUMENT_INVALID")
        bill_codes = [_text(value, "bill_code", maximum=128) for value in raw_codes]
        if len(bill_codes) != len(set(bill_codes)):
            raise _error("delivery projection bill codes are duplicated", "BROKER_ARGUMENT_INVALID")
        status = _text(values.get("status"), "status", maximum=32)
        if status != "signed":
            raise _error("delivery projection status is not signed", "BROKER_ARGUMENT_INVALID")
        _account_descriptor(self._ports, _one_account(context), systems={"ronghui"})
        self._mark_write_started(context)
        raw = writer(bill_codes, status)
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            raise _error("delivery projection update failed", "BROKER_PROJECTION_FAILED")
        updated = raw.get("updated")
        if isinstance(updated, bool) or not isinstance(updated, int) or not 0 <= updated <= len(bill_codes):
            raise _error("delivery projection update count is invalid", "BROKER_PROJECTION_MISMATCH")
        proof = {
            "requested": len(bill_codes),
            "updated": updated,
            "committed": True,
        }
        return {
            "committed": True,
            "updated": updated,
            "evidence_ref": self._codec.evidence(context, "delivery-projection-update", proof),
        }

    def yunda_dispatch_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_YUNDA_DISPATCH_TOOL, role="account_id")
        reader = self._ports.yunda_dispatch_read_page
        if reader is None:
            raise _error("Yunda dispatch source primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(
            arguments,
            {"target_date", "dest_brch", "cursor", "page_size"},
        )
        target_date = _business_date(values.get("target_date"))
        destination_branch = _text(values.get("dest_brch"), "dest_brch", maximum=64)
        size = _page_size(values.get("page_size"))
        state = _page_state(
            self._codec,
            context,
            "yunda-dispatch",
            values.get("cursor"),
            initial={
                "v": 1,
                "page": 0,
                "count": 0,
                "total": None,
                "date": target_date,
                "dest_brch": destination_branch,
            },
        )
        try:
            page_index = int(state["page"])
            cumulative = int(state["count"])
            expected_total = state.get("total")
            bound_date = _business_date(state.get("date"))
            bound_branch = _text(state.get("dest_brch"), "dest_brch", maximum=64)
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("Yunda dispatch cursor is invalid", "BROKER_CURSOR_INVALID") from exc
        if (
            bound_date != target_date
            or bound_branch != destination_branch
            or not 0 <= page_index <= 10_000
            or cumulative < 0
            or (
                expected_total is not None
                and (
                    isinstance(expected_total, bool)
                    or not isinstance(expected_total, int)
                    or expected_total < 0
                )
            )
        ):
            raise _error("Yunda dispatch cursor is invalid", "BROKER_CURSOR_INVALID")
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"yunda"})
        raw = reader(
            descriptor,
            bound_date,
            bound_branch,
            page_index,
            size,
        )
        if not isinstance(raw, Mapping):
            raise _error("Yunda dispatch source response is invalid", "BROKER_SOURCE_INVALID")
        items, total, next_count, complete = _declared_page_result(
            raw,
            expected_total=expected_total,
            cumulative=cumulative,
            page_size=size,
        )
        if any(not isinstance(item, Mapping) for item in items):
            raise _error("Yunda dispatch source row is invalid", "BROKER_SOURCE_INVALID")
        next_cursor = None
        if not complete:
            next_cursor = self._codec.encode(
                context,
                "yunda-dispatch",
                {
                    "v": 1,
                    "page": page_index + 1,
                    "count": next_count,
                    "total": total,
                    "date": bound_date,
                    "dest_brch": bound_branch,
                },
            )
        proof = {
            "page": page_index,
            "returned": len(items),
            "pagination_complete": complete,
            "target_date": bound_date,
            "dest_brch": bound_branch,
        }
        return {
            "items": _scrub_business_value(items),
            "next_cursor": next_cursor,
            "pagination_complete": complete,
            "evidence_ref": self._codec.evidence(
                context,
                "yunda-dispatch-page",
                proof,
            ),
        }

    def _yunda_send_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
        *,
        source: str,
        reader: YundaSendPagePort | None,
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_YUNDA_SEND_TOOL, role="account_id")
        if reader is None:
            raise _error("Yunda send source primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"target_date", "cursor", "page_size"})
        target_date = _business_date(values.get("target_date"))
        size = _page_size(values.get("page_size"))
        purpose = f"yunda-send-{source}"
        state = _page_state(
            self._codec,
            context,
            purpose,
            values.get("cursor"),
            initial={"v": 1, "page": 1, "count": 0, "total": None, "date": target_date},
        )
        try:
            page_number = int(state["page"])
            cumulative = int(state["count"])
            expected_total = state.get("total")
            bound_date = _business_date(state.get("date"))
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("Yunda send cursor is invalid", "BROKER_CURSOR_INVALID") from exc
        if (
            bound_date != target_date
            or not 1 <= page_number <= 10_000
            or cumulative < 0
            or (
                expected_total is not None
                and (
                    isinstance(expected_total, bool)
                    or not isinstance(expected_total, int)
                    or expected_total < 0
                )
            )
        ):
            raise _error("Yunda send cursor is invalid", "BROKER_CURSOR_INVALID")
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"yunda"})
        raw = reader(descriptor, bound_date, page_number, size)
        if not isinstance(raw, Mapping):
            raise _error("Yunda send source response is invalid", "BROKER_SOURCE_INVALID")
        items, total, next_count, complete = _declared_page_result(
            raw,
            expected_total=expected_total,
            cumulative=cumulative,
            page_size=size,
        )
        if any(not isinstance(item, Mapping) for item in items):
            raise _error("Yunda send source row is invalid", "BROKER_SOURCE_INVALID")
        next_cursor = None
        if not complete:
            next_cursor = self._codec.encode(
                context,
                purpose,
                {
                    "v": 1,
                    "page": page_number + 1,
                    "count": next_count,
                    "total": total,
                    "date": bound_date,
                },
            )
        proof = {
            "source": source,
            "page": page_number,
            "returned": len(items),
            "pagination_complete": complete,
            "target_date": bound_date,
        }
        return {
            "items": _scrub_business_value(items),
            "next_cursor": next_cursor,
            "pagination_complete": complete,
            "evidence_ref": self._codec.evidence(
                context,
                f"{purpose}-page",
                proof,
            ),
        }

    def yunda_send_waybill_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._yunda_send_page(
            context,
            arguments,
            source="send-waybill",
            reader=self._ports.yunda_send_read_page,
        )

    def yunda_special_line_page(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._yunda_send_page(
            context,
            arguments,
            source="special-line",
            reader=self._ports.yunda_special_line_read_page,
        )

    def _yunda_record_read(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
        *,
        label: str,
        reader: YundaRecordReadPort | None,
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_YUNDA_SEND_TOOL, role="account_id")
        if reader is None:
            raise _error("Yunda detail primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"bill_code"})
        bill_code = _text(values.get("bill_code"), "bill_code", maximum=128)
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"yunda"})
        raw = reader(descriptor, bill_code)
        if not isinstance(raw, Mapping):
            raise _error("Yunda detail response is invalid", "BROKER_SOURCE_INVALID")
        safe = _scrub_business_value(raw)
        if not isinstance(safe, dict):
            raise _error("Yunda detail response is invalid", "BROKER_SOURCE_INVALID")
        proof = {
            "bill_code_sha256": hashlib.sha256(bill_code.encode("utf-8")).hexdigest(),
            "detail_sha256": hashlib.sha256(canonical_json_bytes(safe)).hexdigest(),
        }
        return {
            "record": safe,
            "evidence_ref": self._codec.evidence(context, label, proof),
        }

    def yunda_tracking_detail(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._yunda_record_read(
            context,
            arguments,
            label="yunda-tracking-detail",
            reader=self._ports.yunda_tracking_detail_read,
        )

    def yunda_original_data(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._yunda_record_read(
            context,
            arguments,
            label="yunda-original-data",
            reader=self._ports.yunda_original_data_read,
        )

    def yunda_renderer_detail(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_YUNDA_SEND_TOOL, role="account_id")
        reader = self._ports.yunda_renderer_detail_read
        if reader is None:
            raise _error("Yunda renderer primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"bill_code", "created_dot_code"})
        bill_code = _text(values.get("bill_code"), "bill_code", maximum=128)
        created_dot_code = str(values.get("created_dot_code") or "").strip()
        if len(created_dot_code) > 128:
            raise _error("created_dot_code is invalid", "BROKER_ARGUMENT_INVALID")
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"yunda"})
        raw = reader(descriptor, bill_code, created_dot_code)
        if not isinstance(raw, Mapping):
            raise _error("Yunda renderer response is invalid", "BROKER_SOURCE_INVALID")
        safe = _scrub_business_value(raw)
        if not isinstance(safe, dict):
            raise _error("Yunda renderer response is invalid", "BROKER_SOURCE_INVALID")
        proof = {
            "bill_code_sha256": hashlib.sha256(bill_code.encode("utf-8")).hexdigest(),
            "detail_sha256": hashlib.sha256(canonical_json_bytes(safe)).hexdigest(),
        }
        return {
            "record": safe,
            "evidence_ref": self._codec.evidence(
                context,
                "yunda-renderer-detail",
                proof,
            ),
        }

    @staticmethod
    def _yunda_records(
        arguments: Mapping[str, Any],
        *,
        fields: Sequence[str],
        identity_field: str,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        values = _strict_arguments(
            arguments,
            {"records", "target_date", "ensure_fields"},
        )
        target_date = _business_date(values.get("target_date"))
        ensure_fields = values.get("ensure_fields", True)
        if not isinstance(ensure_fields, bool):
            raise _error("ensure_fields is invalid", "BROKER_ARGUMENT_INVALID")
        records = _strict_record_list(
            values.get("records"),
            fields=fields,
            label="Yunda sink",
        )
        identities: set[str] = set()
        for record in records:
            identity = _text(record.get(identity_field), identity_field, maximum=128)
            if identity in identities:
                raise _error("Yunda sink contains duplicate identities", "BROKER_ARGUMENT_INVALID")
            identities.add(identity)
            if identity_field == "5.14编号" and record.get("日期") != target_date:
                raise _error("Yunda send record date drifted", "BROKER_ARGUMENT_INVALID")
        return records, target_date, ensure_fields

    def _yunda_resource_commit(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
        *,
        tool_name: str,
        role: str,
        fields: Sequence[str],
        identity_field: str,
        port: YundaResourceCommitPort | None,
        label: str,
        allow_verified_empty_noop: bool = False,
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=tool_name, role=role)
        resource_id = str(context.resource_id or "").strip()
        if not resource_id or port is None:
            raise _error("Yunda resource primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        records, target_date, ensure_fields = self._yunda_records(
            arguments,
            fields=fields,
            identity_field=identity_field,
        )
        empty_noop = allow_verified_empty_noop and not records
        if not empty_noop:
            self._mark_write_started(context)
        try:
            raw = port(resource_id, records, target_date, ensure_fields)
        except PluginExecutionError as exc:
            if exc.code in {
                "BROKER_RESOURCE_UNAVAILABLE",
                "BROKER_RESOURCE_MISMATCH",
                "BROKER_RESOURCE_INVALID",
                "WRITE_OUTCOME_UNKNOWN",
            }:
                raise
            raise _error(
                "Yunda resource write was not confirmed by an exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        except Exception as exc:
            raise _error(
                "Yunda resource write was not confirmed by an exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
            or isinstance(raw.get("readback_count"), bool)
            or not isinstance(raw.get("readback_count"), int)
            or raw.get("readback_count") != len(records)
            or isinstance(raw.get("record_count"), bool)
            or not isinstance(raw.get("record_count"), int)
            or raw.get("record_count") != len(records)
        ):
            raise _error(
                "Yunda resource write was not confirmed by an exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        raw_readback_sha256 = raw.get("readback_sha256")
        readback_sha256 = (
            raw_readback_sha256.strip().lower()
            if isinstance(raw_readback_sha256, str)
            else ""
        )
        if len(readback_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in readback_sha256
        ):
            raise _error(
                "Yunda resource readback evidence is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        if empty_noop and (
            raw.get("no_op") is not True
            or raw.get("written") != 0
            or raw.get("readback_count") != 0
        ):
            raise _error(
                "Yunda empty append was not proven to be a no-op",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "target_date": target_date,
            "record_count": len(records),
            "committed": True,
            "verified": True,
            "readback_count": len(records),
            "readback_sha256": readback_sha256,
        }
        for key in ("written", "deleted", "created_fields", "skipped"):
            value = raw.get(key)
            if isinstance(value, (bool, int)):
                proof[key] = value
        result = {
            **proof,
            "evidence_ref": self._codec.evidence(context, label, proof),
        }
        if empty_noop:
            result[VERIFIED_WRITE_NOOP_FIELD] = True
        return result

    def append_yunda_dispatch_bitable(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._yunda_resource_commit(
            context,
            arguments,
            tool_name=_YUNDA_DISPATCH_TOOL,
            role=_YUNDA_DISPATCH_RESOURCE_ROLE,
            fields=_YUNDA_DISPATCH_FIELDS,
            identity_field="主单号",
            port=self._ports.append_yunda_dispatch_bitable,
            label="yunda-dispatch-bitable-append",
            allow_verified_empty_noop=True,
        )

    def replace_yunda_send_bitable(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._yunda_resource_commit(
            context,
            arguments,
            tool_name=_YUNDA_SEND_TOOL,
            role=_YUNDA_SEND_BITABLE_ROLE,
            fields=_YUNDA_SEND_FIELDS,
            identity_field="5.14编号",
            port=self._ports.replace_yunda_send_bitable,
            label="yunda-send-bitable-replace-date",
        )

    def replace_yunda_send_sheet(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._yunda_resource_commit(
            context,
            arguments,
            tool_name=_YUNDA_SEND_TOOL,
            role=_YUNDA_SEND_SHEET_ROLE,
            fields=_YUNDA_SEND_FIELDS,
            identity_field="5.14编号",
            port=self._ports.replace_yunda_send_sheet,
            label="yunda-send-sheet-replace",
        )

    def replace_yunda_waybill_projection(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_YUNDA_SEND_TOOL, role="account_id")
        _one_account(context)
        port = self._ports.replace_yunda_waybill_projection
        if port is None:
            raise _error("Yunda projection primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        records, target_date, _ensure_fields = self._yunda_records(
            arguments,
            fields=_YUNDA_SEND_FIELDS,
            identity_field="5.14编号",
        )
        self._mark_write_started(context)
        try:
            raw = port(records, target_date)
        except PluginExecutionError as exc:
            if exc.code == "WRITE_OUTCOME_UNKNOWN":
                raise
            raise _error(
                "Yunda projection commit was not confirmed by an exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        except Exception as exc:
            raise _error(
                "Yunda projection commit was not confirmed by an exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
            or isinstance(raw.get("readback_count"), bool)
            or not isinstance(raw.get("readback_count"), int)
            or raw.get("readback_count") != len(records)
            or isinstance(raw.get("record_count"), bool)
            or not isinstance(raw.get("record_count"), int)
            or raw.get("record_count") != len(records)
        ):
            raise _error(
                "Yunda projection commit was not confirmed by an exact readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        raw_readback_sha256 = raw.get("readback_sha256")
        readback_sha256 = (
            raw_readback_sha256.strip().lower()
            if isinstance(raw_readback_sha256, str)
            else ""
        )
        if len(readback_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in readback_sha256
        ):
            raise _error(
                "Yunda projection readback evidence is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "target_date": target_date,
            "record_count": len(records),
            "committed": True,
            "verified": True,
            "readback_count": len(records),
            "readback_sha256": readback_sha256,
        }
        for key in ("upserted", "updates", "creates", "deleted_stale"):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                proof[key] = value
        return {
            **proof,
            "evidence_ref": self._codec.evidence(
                context,
                "yunda-waybill-projection-replace-date",
                proof,
            ),
        }

    @staticmethod
    def _records(
        arguments: Mapping[str, Any],
        *,
        fields: Sequence[str] = _ARRIVE_FIELDS,
    ) -> tuple[list[dict[str, Any]], str]:
        values = _strict_arguments(arguments, {"records", "target_date"})
        target_date = _business_date(values.get("target_date"))
        records = _strict_record_list(
            values.get("records"),
            fields=fields,
            label="projection",
        )
        identities: set[str] = set()
        for row in records:
            identity = _text(row.get("tracking_number"), "tracking_number", maximum=128)
            if identity in identities:
                raise _error("projection records contain duplicate identities", "BROKER_ARGUMENT_INVALID")
            identities.add(identity)
        return records, target_date

    def _projection_replace(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
        *,
        port: ProjectionReplacePort | None,
        label: str,
        tool_names: set[str],
        fields: Sequence[str] = _ARRIVE_FIELDS,
    ) -> Mapping[str, Any]:
        if context.tool_name not in tool_names or context.role != "account_id":
            raise _error("projection primitive is not valid for this tool", "BROKER_CONTEXT_INVALID")
        _one_account(context)
        if port is None:
            raise _error("projection primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        records, target_date = self._records(arguments, fields=fields)
        self._mark_write_started(context)
        raw = port(records, target_date)
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
        ):
            raise _error(
                "projection commit has no exact fresh readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        observed = raw.get(
            "record_count",
            raw.get("rows", raw.get("replaced", raw.get("upserted"))),
        )
        if isinstance(observed, bool) or not isinstance(observed, int) or observed != len(records):
            raise _error(
                "arrival projection fresh readback count is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {"target_date": target_date, "record_count": len(records), "committed": True}
        return {
            **proof,
            "evidence_ref": self._codec.evidence(context, label, proof),
        }

    def replace_waybill_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        normalized_arguments = arguments
        if context.tool_name == _ARRIVAL_STATS_TOOL:
            values = _strict_arguments(arguments, {"records", "target_date"})
            normalized_arguments = {
                "records": _arrival_stats_v1_records(
                    values.get("records"),
                    fields=_ARRIVE_FIELDS,
                    label="projection",
                ),
                "target_date": values.get("target_date"),
            }
        return self._projection_replace(
            context,
            normalized_arguments,
            port=self._ports.replace_waybill_snapshot,
            label="waybill-snapshot-replace",
            tool_names={_ARRIVE_TOOL, _ARRIVAL_STATS_TOOL},
        )

    def replace_arrival_forecast_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._projection_replace(
            context,
            arguments,
            port=self._ports.replace_arrival_forecast_snapshot,
            label="arrival-forecast-snapshot-replace",
            tool_names={_ARRIVE_TOOL},
        )

    def waybill_detail(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_ARRIVAL_STATS_TOOL, role="account_id")
        if self._ports.waybill_detail_read is None:
            raise _error("waybill detail primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"tracking_number"})
        tracking = _text(values.get("tracking_number"), "tracking_number", maximum=128)
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"ronghui"})
        raw = self._ports.waybill_detail_read(descriptor, tracking)
        if raw is None:
            proof = {"tracking_number": tracking, "found": False}
            return {
                **proof,
                "evidence_ref": self._codec.evidence(context, "waybill-detail-read", proof),
            }
        if not isinstance(raw, Mapping) or set(raw) != set(_ARRIVE_FIELDS):
            raise _error("waybill detail adapter returned schema drift", "BROKER_SOURCE_INVALID")
        record = dict(raw)
        if str(record.get("tracking_number") or "").strip() != tracking:
            raise _error("waybill detail changed source identity", "BROKER_SOURCE_IDENTITY_MISMATCH")
        proof = {"tracking_number": tracking, "found": True}
        return {
            **proof,
            "record": _scrub_business_value(record),
            "evidence_ref": self._codec.evidence(context, "waybill-detail-read", proof),
        }

    def child_count(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_ARRIVAL_STATS_TOOL, role="account_id")
        if self._ports.child_count_read is None:
            raise _error("child-count primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"tracking_number"})
        tracking = _text(values.get("tracking_number"), "tracking_number", maximum=128)
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"ronghui"})
        raw = self._ports.child_count_read(descriptor, tracking)
        if not isinstance(raw, Mapping) or set(raw) != {"tracking_number", "count"}:
            raise _error("child-count adapter returned schema drift", "BROKER_SOURCE_INVALID")
        if str(raw.get("tracking_number") or "").strip() != tracking:
            raise _error("child-count changed source identity", "BROKER_SOURCE_IDENTITY_MISMATCH")
        count = _nonnegative_int(raw.get("count"), "child count", maximum=100_000)
        proof = {"tracking_number": tracking, "count": count}
        return {
            **proof,
            "evidence_ref": self._codec.evidence(context, "child-count-read", proof),
        }

    def replace_scan_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if context.tool_name not in {_ARRIVAL_STATS_TOOL, _SCAN_TOOL} or context.role != "account_id":
            raise _error(
                "scan snapshot primitive is not valid for this tool",
                "BROKER_CONTEXT_INVALID",
            )
        _one_account(context)
        if self._ports.replace_scan_snapshot is None:
            raise _error("scan snapshot primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"records", "target_date"})
        target_date = _business_date(values.get("target_date"))
        records = _strict_record_list(
            values.get("records"),
            fields=_SCAN_SNAPSHOT_FIELDS,
            label="scan snapshot",
        )
        seen: set[str] = set()
        for row in records:
            raw_code = _text(row.get("raw_code"), "raw_code", maximum=128)
            main = _text(row.get("main_tracking"), "main_tracking", maximum=128)
            code_type = str(row.get("code_type") or "")
            if raw_code in seen or code_type not in {"main", "child"}:
                raise _error("scan snapshot identity is invalid", "BROKER_ARGUMENT_INVALID")
            if (code_type == "main") != (raw_code == main):
                raise _error("scan snapshot classification is invalid", "BROKER_ARGUMENT_INVALID")
            seen.add(raw_code)
        expected_rows = sorted(records, key=lambda row: str(row["raw_code"]))
        expected_sha256 = hashlib.sha256(
            canonical_json_bytes(expected_rows)
        ).hexdigest()
        self._mark_write_started(context)
        try:
            raw = self._ports.replace_scan_snapshot(records, target_date)
        except PluginExecutionError as exc:
            if exc.code == "WRITE_OUTCOME_UNKNOWN":
                raise
            raise _error(
                "scan snapshot terminal state is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        except Exception as exc:
            raise _error(
                "scan snapshot terminal state is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
            or raw.get("record_count") != len(records)
            or raw.get("readback_count") != len(records)
            or raw.get("identities_sha256") != expected_sha256
        ):
            raise _error(
                "scan snapshot terminal state is unknown",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "target_date": target_date,
            "record_count": len(records),
            "committed": True,
            "verified": True,
            "identities_sha256": expected_sha256,
        }
        return {
            **proof,
            "evidence_ref": self._codec.evidence(context, "scan-snapshot-replace", proof),
        }

    def submit_scan_next(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_SCAN_TOOL, role="account_id")
        port = self._ports.scan_next_submit
        if port is None:
            raise _error("scan-next primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"items"})
        items = _strict_record_list(
            values.get("items"),
            fields=_SCAN_NEXT_FIELDS,
            label="scan-next batch",
        )
        if not 1 <= len(items) <= 200:
            raise _error("scan-next batch size is invalid", "BROKER_ARGUMENT_INVALID")
        requested: dict[str, str] = {}
        normalized: list[dict[str, str]] = []
        for row in items:
            bill_code = _text(row.get("bill_code"), "bill_code", maximum=128)
            station_name = _text(row.get("station_name"), "station_name", maximum=256)
            if bill_code in requested or not station_name:
                raise _error("scan-next batch identity is invalid", "BROKER_ARGUMENT_INVALID")
            requested[bill_code] = station_name
            normalized.append({"bill_code": bill_code, "station_name": station_name})
        account_id = _one_account(context)
        descriptor = _account_descriptor(self._ports, account_id, systems={"ronghui"})
        self._mark_write_started(context)
        try:
            raw = port(descriptor, normalized)
        except PluginExecutionError as exc:
            if exc.code == "WRITE_OUTCOME_UNKNOWN":
                raise
            raise _error(
                "scan-next submission did not produce a verifiable outcome",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        except Exception as exc:
            raise _error(
                "scan-next submission did not produce a verifiable outcome",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if not isinstance(raw, Mapping) or raw.get("ok") is not True or raw.get("stage") != "done":
            raise _error(
                "scan-next submission did not produce a verifiable outcome",
                "WRITE_OUTCOME_UNKNOWN",
            )
        write_started_at = _text(
            raw.get("write_started_at"),
            "write_started_at",
            maximum=64,
        )
        write_finished_at = _text(
            raw.get("write_finished_at"),
            "write_finished_at",
            maximum=64,
        )
        try:
            started_at = datetime.fromisoformat(write_started_at)
            finished_at = datetime.fromisoformat(write_finished_at)
        except ValueError as exc:
            raise _error(
                "scan-next submission timestamps are invalid",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if (
            started_at.tzinfo is None
            or started_at.utcoffset() is None
            or finished_at.tzinfo is None
            or finished_at.utcoffset() is None
            or finished_at < started_at
        ):
            raise _error(
                "scan-next submission timestamps are invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        detail = raw.get("detail")
        if not isinstance(detail, Mapping):
            raise _error(
                "scan-next submission proof is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        returned_items = detail.get("items")
        stations = detail.get("stations")
        skipped_raw = detail.get("skipped_signed_codes")
        total_scanned = detail.get("total_scanned")
        if (
            returned_items != normalized
            or not isinstance(stations, list)
            or not isinstance(skipped_raw, list)
            or isinstance(total_scanned, bool)
            or not isinstance(total_scanned, int)
        ):
            raise _error(
                "scan-next submission proof is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        scanned_codes: list[str] = []
        for station_raw in stations:
            if not isinstance(station_raw, Mapping):
                raise _error(
                    "scan-next station proof is invalid",
                    "WRITE_OUTCOME_UNKNOWN",
                )
            station_name = _text(
                station_raw.get("station_name"),
                "station_name",
                maximum=256,
            )
            bill_codes = station_raw.get("bill_codes")
            count = station_raw.get("count")
            if (
                not isinstance(bill_codes, list)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count != len(bill_codes)
            ):
                raise _error(
                    "scan-next station proof is invalid",
                    "WRITE_OUTCOME_UNKNOWN",
                )
            for value in bill_codes:
                bill_code = _text(value, "bill_code", maximum=128)
                if requested.get(bill_code) != station_name:
                    raise _error(
                        "scan-next station proof changed an identity",
                        "WRITE_OUTCOME_UNKNOWN",
                    )
                scanned_codes.append(bill_code)
        skipped_codes = [
            _text(value, "skipped_signed_code", maximum=128)
            for value in skipped_raw
        ]
        if (
            "" in skipped_codes
            or len(scanned_codes) != len(set(scanned_codes))
            or len(skipped_codes) != len(set(skipped_codes))
            or set(scanned_codes) & set(skipped_codes)
            or set(scanned_codes) | set(skipped_codes) != set(requested)
            or total_scanned != len(scanned_codes)
        ):
            raise _error(
                "scan-next submission identities are inconsistent",
                "WRITE_OUTCOME_UNKNOWN",
            )
        items_sha256 = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
        scanned_set = set(scanned_codes)
        scanned_items = [
            dict(item) for item in normalized if item["bill_code"] in scanned_set
        ]
        readback_identities = sorted(
            scanned_items,
            key=lambda item: (item["bill_code"], item["station_name"]),
        )
        readback_sha256 = hashlib.sha256(
            canonical_json_bytes(readback_identities)
        ).hexdigest()
        proof = {
            "v": 2,
            "items_sha256": items_sha256,
            "submitted": len(normalized),
            "scanned": len(scanned_codes),
            "skipped_sha256": hashlib.sha256(
                canonical_json_bytes(skipped_codes)
            ).hexdigest(),
            "postcondition": "uploaded_and_table_cleared",
            "scanned_items": scanned_items,
            "readback_sha256": readback_sha256,
            "write_started_at": write_started_at,
            "write_finished_at": write_finished_at,
        }
        operation_id = self._codec.encode(context, "scan-next-operation", proof)
        public = {
            "operation_id": operation_id,
            "items_sha256": items_sha256,
            "submitted": len(normalized),
            "scanned": len(scanned_codes),
            "skipped_signed_codes": skipped_codes,
            "postcondition": "uploaded_and_table_cleared",
        }
        return {
            **public,
            "evidence_ref": self._codec.evidence(context, "scan-next-submit", public),
        }

    def verify_scan_next(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_SCAN_TOOL, role="account_id")
        verify_port = self._ports.scan_next_verify
        if verify_port is None:
            raise _error(
                "scan-next authoritative verifier is unavailable",
                "WRITE_OUTCOME_UNKNOWN",
            )
        values = _strict_arguments(
            arguments,
            {
                "operation_id",
                "items_sha256",
                "submitted",
                "scanned",
                "skipped_signed_codes",
            },
        )
        proof = self._codec.decode(
            context,
            "scan-next-operation",
            values.get("operation_id"),
        )
        items_sha256 = _text(
            values.get("items_sha256"),
            "items_sha256",
            maximum=64,
        )
        submitted = values.get("submitted")
        scanned = values.get("scanned")
        skipped_raw = values.get("skipped_signed_codes")
        if (
            len(items_sha256) != 64
            or isinstance(submitted, bool)
            or not isinstance(submitted, int)
            or isinstance(scanned, bool)
            or not isinstance(scanned, int)
            or not isinstance(skipped_raw, list)
        ):
            raise _error("scan-next verification is invalid", "BROKER_ARGUMENT_INVALID")
        skipped_codes = [
            _text(value, "skipped_signed_code", maximum=128)
            for value in skipped_raw
        ]
        skipped_sha256 = hashlib.sha256(
            canonical_json_bytes(skipped_codes)
        ).hexdigest()
        if (
            proof.get("v") != 2
            or proof.get("items_sha256") != items_sha256
            or proof.get("submitted") != submitted
            or proof.get("scanned") != scanned
            or proof.get("skipped_sha256") != skipped_sha256
            or proof.get("postcondition") != "uploaded_and_table_cleared"
            or scanned + len(skipped_codes) != submitted
        ):
            raise _error(
                "scan-next verification proof changed",
                "BROKER_SOURCE_IDENTITY_MISMATCH",
            )
        scanned_items = _strict_record_list(
            proof.get("scanned_items"),
            fields=_SCAN_NEXT_FIELDS,
            label="scan-next readback identity",
        )
        if len(scanned_items) != scanned:
            raise _error(
                "scan-next verification identities changed",
                "BROKER_SOURCE_IDENTITY_MISMATCH",
            )
        seen_codes: set[str] = set()
        normalized_scanned_items: list[dict[str, str]] = []
        for row in scanned_items:
            bill_code = _text(row.get("bill_code"), "bill_code", maximum=128)
            station_name = _text(
                row.get("station_name"),
                "station_name",
                maximum=256,
            )
            if bill_code in seen_codes:
                raise _error(
                    "scan-next verification identities changed",
                    "BROKER_SOURCE_IDENTITY_MISMATCH",
                )
            seen_codes.add(bill_code)
            normalized_scanned_items.append(
                {"bill_code": bill_code, "station_name": station_name}
            )
        expected_readback_sha256 = hashlib.sha256(
            canonical_json_bytes(
                sorted(
                    normalized_scanned_items,
                    key=lambda item: (item["bill_code"], item["station_name"]),
                )
            )
        ).hexdigest()
        if proof.get("readback_sha256") != expected_readback_sha256:
            raise _error(
                "scan-next verification identities changed",
                "BROKER_SOURCE_IDENTITY_MISMATCH",
            )
        write_started_at = _text(
            proof.get("write_started_at"),
            "write_started_at",
            maximum=64,
        )
        write_finished_at = _text(
            proof.get("write_finished_at"),
            "write_finished_at",
            maximum=64,
        )
        account_id = _one_account(context)
        descriptor = _account_descriptor(
            self._ports,
            account_id,
            systems={"ronghui"},
        )
        try:
            readback = verify_port(
                descriptor,
                normalized_scanned_items,
                write_started_at,
                write_finished_at,
            )
        except PluginExecutionError as exc:
            if exc.code == "WRITE_OUTCOME_UNKNOWN":
                raise
            raise _error(
                "scan-next write was not confirmed by a fresh server read",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        except Exception as exc:
            raise _error(
                "scan-next write was not confirmed by a fresh server read",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        if (
            not isinstance(readback, Mapping)
            or readback.get("ok") is not True
            or readback.get("verified") is not True
            or isinstance(readback.get("record_count"), bool)
            or not isinstance(readback.get("record_count"), int)
            or readback.get("record_count") != scanned
            or readback.get("identities_sha256") != expected_readback_sha256
        ):
            raise _error(
                "scan-next write was not confirmed by a fresh server read",
                "WRITE_OUTCOME_UNKNOWN",
            )
        public = {
            "verified": True,
            "items_sha256": items_sha256,
            "submitted": submitted,
            "scanned": scanned,
            "skipped_signed_codes": skipped_codes,
            "postcondition": "server_ledger_verified",
            "readback_count": scanned,
        }
        return {
            **public,
            "evidence_ref": self._codec.evidence(context, "scan-next-verify", public),
        }

    def read_scan_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_ARRIVAL_STATS_TOOL, role="account_id")
        _one_account(context)
        if self._ports.read_scan_snapshot is None:
            raise _error("scan snapshot read primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"target_date"})
        target_date = _business_date(values.get("target_date"))
        raw = self._ports.read_scan_snapshot(target_date)
        records = _strict_record_list(raw, fields=_SCAN_READ_FIELDS, label="scan snapshot read")
        proof = {"target_date": target_date, "record_count": len(records)}
        return {
            "items": _scrub_business_value(records),
            "pagination_complete": True,
            "evidence_ref": self._codec.evidence(context, "scan-snapshot-read", proof),
        }

    def cleanup_scan_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_ARRIVAL_STATS_TOOL, role="account_id")
        _one_account(context)
        if self._ports.cleanup_scan_snapshot is None:
            raise _error("scan cleanup primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"retention_days"})
        retention = _nonnegative_int(values.get("retention_days"), "retention_days", maximum=3650)
        self._mark_write_started(context)
        raw = self._ports.cleanup_scan_snapshot(retention)
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
        ):
            raise _error(
                "scan cleanup has no exact fresh readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        deleted = _nonnegative_int(raw.get("deleted", 0), "deleted rows", maximum=_MAX_RECORDS)
        proof = {
            "retention_days": retention,
            "deleted": deleted,
            "committed": True,
            "skipped": raw.get("skipped") is True,
        }
        return {
            **proof,
            "evidence_ref": self._codec.evidence(context, "scan-snapshot-cleanup", proof),
        }

    def read_completed_arrivals(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_ARRIVAL_STATS_TOOL, role="account_id")
        _one_account(context)
        if self._ports.read_completed_arrivals_before is None:
            raise _error("arrival history primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"target_date"})
        target_date = _business_date(values.get("target_date"))
        raw = self._ports.read_completed_arrivals_before(target_date)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) > _MAX_RECORDS:
            raise _error("arrival history response is invalid", "BROKER_SOURCE_INVALID")
        tracking_numbers = sorted(
            {_text(item, "completed tracking number", maximum=128) for item in raw}
        )
        proof = {"target_date": target_date, "record_count": len(tracking_numbers)}
        return {
            "tracking_numbers": tracking_numbers,
            "pagination_complete": True,
            "evidence_ref": self._codec.evidence(context, "arrival-history-read", proof),
        }

    def read_pending_waybills(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(context, tool_name=_ARRIVAL_STATS_TOOL, role="account_id")
        _one_account(context)
        if self._ports.read_pending_waybills is None:
            raise _error("pending-waybill primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"target_date"})
        target_date = _business_date(values.get("target_date"))
        raw = self._ports.read_pending_waybills(target_date)
        records = _strict_record_list(raw, fields=_PENDING_FIELDS, label="pending waybill")
        proof = {"target_date": target_date, "record_count": len(records)}
        return {
            "items": _scrub_business_value(records),
            "pagination_complete": True,
            "evidence_ref": self._codec.evidence(context, "pending-waybill-read", proof),
        }

    def replace_arrival_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._projection_replace(
            context,
            arguments,
            port=self._ports.replace_arrival_snapshot,
            label="arrival-stat-snapshot-replace",
            tool_names={_ARRIVAL_STATS_TOOL},
            fields=_ARRIVAL_SNAPSHOT_FIELDS,
        )

    def refresh_split_pending_snapshot(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        values = _strict_arguments(arguments, {"records", "target_date"})
        normalized_arguments = {
            "records": _arrival_stats_v1_records(
                values.get("records"),
                fields=_ARRIVAL_STATS_FIELDS,
                label="projection",
            ),
            "target_date": values.get("target_date"),
        }
        return self._projection_replace(
            context,
            normalized_arguments,
            port=self._ports.refresh_split_pending_snapshot,
            label="split-pending-snapshot-refresh",
            tool_names={_ARRIVAL_STATS_TOOL},
            fields=_ARRIVAL_STATS_FIELDS,
        )

    @staticmethod
    def _sheet_rows(tool_name: str, values: object) -> tuple[list[list[Any]], int]:
        columns = len(_ARRIVE_FIELDS) if tool_name == _ARRIVE_TOOL else len(_SITE_FIELDS)
        if not isinstance(values, list) or len(values) > _MAX_RECORDS:
            raise _error("sheet values are invalid", "BROKER_ARGUMENT_INVALID")
        rows: list[list[Any]] = []
        for raw in values:
            if not isinstance(raw, list) or len(raw) != columns:
                raise _error("sheet row schema is invalid", "BROKER_ARGUMENT_INVALID")
            rows.append(list(raw))
        return rows, columns

    def replace_sheet(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if context.tool_name == _ARRIVAL_STATS_TOOL:
            if self._ports.replace_arrival_stats_sheet is None:
                raise _error("arrival statistics sheet primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
            values = _strict_arguments(arguments, {"resource_slot", "records", "target_date"})
            slot = _text(values.get("resource_slot"), "resource_slot", maximum=64)
            slots = {
                "arrival_stats_primary": ("arrival_stats_primary_sheet", "stats"),
                "arrival_stats_secondary": ("arrival_stats_secondary_sheet", "stats"),
                "arrival_stats_pending": ("arrival_stats_pending_sheet", "pending"),
                "arrival_stats_split_pending": (
                    "arrival_stats_split_pending_sheet",
                    "split_pending",
                ),
            }
            if slot not in slots:
                raise _error("sheet resource slot is not signed for this tool", "BROKER_RESOURCE_DENIED")
            expected_role, layout = slots[slot]
            if context.role != expected_role or not context.resource_id:
                raise _error(
                    "the exact arrival statistics sheet resource is not bound",
                    "BROKER_CONTEXT_INVALID",
                )
            target_date = _business_date(values.get("target_date"))
            fields = _PENDING_FIELDS if slot == "arrival_stats_pending" else _ARRIVAL_STATS_FIELDS
            records = (
                _strict_record_list(
                    values.get("records"),
                    fields=fields,
                    label="arrival statistics sheet",
                )
                if fields == _PENDING_FIELDS
                else _arrival_stats_v1_records(
                    values.get("records"),
                    fields=fields,
                    label="arrival statistics sheet",
                )
            )
            self._mark_write_started(context)
            raw = self._ports.replace_arrival_stats_sheet(
                context.resource_id,
                layout,
                records,
                target_date,
            )
            if (
                not isinstance(raw, Mapping)
                or raw.get("ok") is not True
                or raw.get("verified") is not True
            ):
                raise _error(
                    "sheet snapshot commit has no exact fresh readback",
                    "WRITE_OUTCOME_UNKNOWN",
                )
            observed = raw.get("record_count", raw.get("rows"))
            if isinstance(observed, bool) or not isinstance(observed, int) or observed != len(records):
                raise _error(
                    "sheet snapshot fresh readback count is invalid",
                    "WRITE_OUTCOME_UNKNOWN",
                )
            proof = {
                "resource_slot": slot,
                "record_count": len(records),
                "committed": True,
                "verified": True,
            }
            return {
                **proof,
                "evidence_ref": self._codec.evidence(
                    context,
                    "arrival-statistics-sheet-replace",
                    proof,
                ),
            }
        if context.tool_name == _SITE_SEND_TOOL:
            _require_context(
                context,
                tool_name=_SITE_SEND_TOOL,
                role=_SITE_SEND_SHEET_ROLE,
            )
            if not context.resource_id:
                raise _error("site-send sheet resource is not bound", "BROKER_CONTEXT_INVALID")
            if self._ports.site_send_read_page is None:
                raise _error(
                    "site-send source contract has not been verified",
                    "BROKER_SOURCE_EVIDENCE_PENDING",
                )
            if self._ports.replace_sheet_resource is None:
                raise _error("sheet resource primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
            values = _strict_arguments(arguments, {"values"})
            rows, _ = self._sheet_rows(_SITE_SEND_TOOL, values.get("values"))
            self._mark_write_started(context)
            raw = self._ports.replace_sheet_resource(context.resource_id, rows, None)
            if not isinstance(raw, Mapping) or raw.get("ok") is not True:
                raise _error("sheet snapshot commit failed", "BROKER_RESOURCE_WRITE_FAILED")
            observed = raw.get("record_count", raw.get("rows"))
            if isinstance(observed, bool) or not isinstance(observed, int) or observed != len(rows):
                raise _error("sheet snapshot count is invalid", "BROKER_RESOURCE_WRITE_MISMATCH")
            proof = {
                "record_count": len(rows),
                "committed": True,
            }
            return {
                **proof,
                "evidence_ref": self._codec.evidence(
                    context,
                    "site-send-sheet-replace",
                    proof,
                ),
            }
        if self._ports.replace_arrive_sheet_resource is None:
            raise _error("sheet resource primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        slots = {
            "arrive_primary_sheet": "arrive_primary_sheet",
            "arrive_secondary_sheet": "arrive_secondary_sheet",
        }
        if context.tool_name != _ARRIVE_TOOL:
            raise _error("sheet primitive is not valid for this tool", "BROKER_CONTEXT_INVALID")
        values = _strict_arguments(arguments, {"resource_slot", "values", "target_date"})
        slot = _text(values.get("resource_slot"), "resource_slot", maximum=64)
        if slot not in slots:
            raise _error("sheet resource slot is not signed for this tool", "BROKER_RESOURCE_DENIED")
        if context.role != slots[slot] or not context.resource_id:
            raise _error(
                "the exact arrive sheet resource is not bound",
                "BROKER_CONTEXT_INVALID",
            )
        rows, _ = self._sheet_rows(context.tool_name, values.get("values"))
        target_date = _business_date(values.get("target_date"))
        self._mark_write_started(context)
        raw = self._ports.replace_arrive_sheet_resource(
            context.resource_id,
            rows,
            target_date,
        )
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
        ):
            raise _error(
                "sheet snapshot commit has no exact fresh readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        observed = raw.get("record_count", raw.get("rows"))
        if isinstance(observed, bool) or not isinstance(observed, int) or observed != len(rows):
            raise _error(
                "sheet snapshot fresh readback count is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {"resource_slot": slot, "record_count": len(rows), "committed": True}
        return {
            **proof,
            "evidence_ref": self._codec.evidence(context, "sheet-snapshot-replace", proof),
        }

    def archive_arrival_stats(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_ARRIVAL_STATS_TOOL,
            role="arrival_stats_archive_sheet",
        )
        if not context.resource_id:
            raise _error(
                "the exact arrival archive resource is not bound",
                "BROKER_CONTEXT_INVALID",
            )
        if self._ports.archive_arrival_stats_sheet is None:
            raise _error("arrival archive primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"records", "target_date"})
        target_date = _business_date(values.get("target_date"))
        records = _arrival_stats_v1_records(
            values.get("records"),
            fields=_ARRIVAL_STATS_FIELDS,
            label="arrival archive",
        )
        self._mark_write_started(context)
        raw = self._ports.archive_arrival_stats_sheet(
            context.resource_id,
            records,
            target_date,
        )
        if (
            not isinstance(raw, Mapping)
            or raw.get("ok") is not True
            or raw.get("verified") is not True
        ):
            raise _error(
                "arrival archive commit has no exact fresh readback",
                "WRITE_OUTCOME_UNKNOWN",
            )
        observed = raw.get("record_count", raw.get("rows"))
        if isinstance(observed, bool) or not isinstance(observed, int) or observed != len(records):
            raise _error(
                "arrival archive fresh readback count is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            )
        proof = {
            "target_date": target_date,
            "record_count": len(records),
            "committed": True,
            "verified": True,
        }
        return {
            **proof,
            "evidence_ref": self._codec.evidence(context, "arrival-statistics-archive", proof),
        }

    def replace_bitable(
        self,
        context: CoreBrokerInvocationContext,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_context(
            context,
            tool_name=_SITE_SEND_TOOL,
            role=_SITE_SEND_BITABLE_ROLE,
        )
        if not context.resource_id:
            raise _error("site-send Bitable resource is not bound", "BROKER_CONTEXT_INVALID")
        if self._ports.site_send_read_page is None:
            raise _error(
                "site-send source contract has not been verified",
                "BROKER_SOURCE_EVIDENCE_PENDING",
            )
        if self._ports.replace_bitable_resource is None:
            raise _error("Bitable resource primitive is unavailable", "BROKER_ACTION_UNAVAILABLE")
        values = _strict_arguments(arguments, {"records"})
        records = values.get("records")
        if not isinstance(records, list) or len(records) > _MAX_RECORDS:
            raise _error("Bitable records are invalid", "BROKER_ARGUMENT_INVALID")
        normalized: list[dict[str, Any]] = []
        identities: set[str] = set()
        for raw in records:
            if not isinstance(raw, Mapping) or set(raw) != {"fields"} or not isinstance(raw.get("fields"), Mapping):
                raise _error("Bitable record schema is invalid", "BROKER_ARGUMENT_INVALID")
            fields = dict(raw["fields"])
            if set(fields) != set(_SITE_FIELDS):
                raise _error("Bitable field roles are invalid", "BROKER_ARGUMENT_INVALID")
            identity = _text(fields.get("tracking_number"), "tracking_number", maximum=128)
            fields["tracking_number"] = identity
            for field in ("send_site", "package_type", "destination"):
                value = fields.get(field)
                if not isinstance(value, str) or len(value) > 512:
                    raise _error("Bitable text field is invalid", "BROKER_ARGUMENT_INVALID")
                fields[field] = value.strip()
            fields["pieces"] = _optional_finite_number(fields.get("pieces"), "pieces")
            fields["weight"] = _optional_finite_number(fields.get("weight"), "weight")
            if identity in identities:
                raise _error("Bitable records contain duplicate identities", "BROKER_ARGUMENT_INVALID")
            identities.add(identity)
            normalized.append({"fields": fields})
        self._mark_write_started(context)
        raw = self._ports.replace_bitable_resource(
            context.resource_id,
            normalized,
            None,
        )
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            raise _error("Bitable snapshot commit failed", "BROKER_RESOURCE_WRITE_FAILED")
        observed = raw.get("record_count", raw.get("written"))
        if isinstance(observed, bool) or not isinstance(observed, int) or observed != len(normalized):
            raise _error("Bitable snapshot count is invalid", "BROKER_RESOURCE_WRITE_MISMATCH")
        proof = {
            "record_count": len(normalized),
            "committed": True,
        }
        return {
            **proof,
            "evidence_ref": self._codec.evidence(context, "bitable-snapshot-replace", proof),
        }

    def handler_map(self) -> dict[tuple[str, str], CoreBrokerHandler]:
        handlers: dict[tuple[str, str], CoreBrokerHandler] = {}
        if self._ports.clock_action is not None:
            handlers.update(
                {
                    ("browser.invoke", "ronghui.clock.precheck"): self.clock_precheck,
                    ("browser.invoke", "ronghui.clock.submit"): self.clock_submit,
                    ("browser.invoke", "ronghui.clock.verify"): self.clock_verify,
                }
            )
        if self._ports.daily_sign_sync is not None:
            handlers[("ledger.invoke", "daily_sign.authoritative_sync")] = (
                self.daily_sign_authoritative_sync
            )
        if self._ports.customer_action is not None:
            handlers.update(
                {
                    ("browser.invoke", "customer_problem.list_page"): self.customer_list_page,
                    ("browser.invoke", "customer_problem.detail"): self.customer_detail,
                }
            )
        if self._ports.arrive_list_read_page is not None:
            handlers[("browser.invoke", "ronghui.arrive_list.read_page")] = self.arrive_list_page
        if self._ports.scan_read_page is not None:
            handlers[("browser.invoke", "ronghui.scan.read_page")] = self.scan_page
        if self._ports.waybill_detail_read is not None:
            handlers[("browser.invoke", "ronghui.waybill_detail.read")] = self.waybill_detail
        if self._ports.child_count_read is not None:
            handlers[("browser.invoke", "ronghui.child_count.read")] = self.child_count
        if self._ports.site_send_read_page is not None:
            handlers[("browser.invoke", "ronghui.site_send.read_page")] = self.site_send_page
        if self._ports.delivery_status_read is not None:
            handlers[("browser.invoke", "ronghui.delivery_status.read")] = (
                self.delivery_status_read
            )
        if self._ports.yunda_dispatch_read_page is not None:
            handlers[("browser.invoke", "yunda.dispatch_forecast.read_page")] = (
                self.yunda_dispatch_page
            )
        if self._ports.yunda_send_read_page is not None:
            handlers[("browser.invoke", "yunda.send_waybill.list_page")] = (
                self.yunda_send_waybill_page
            )
        if self._ports.yunda_special_line_read_page is not None:
            handlers[("browser.invoke", "yunda.special_line.list_page")] = (
                self.yunda_special_line_page
            )
        if self._ports.yunda_tracking_detail_read is not None:
            handlers[("browser.invoke", "yunda.waybill.tracking_detail")] = (
                self.yunda_tracking_detail
            )
        if self._ports.yunda_original_data_read is not None:
            handlers[("browser.invoke", "yunda.waybill.original_data")] = (
                self.yunda_original_data
            )
        if self._ports.yunda_renderer_detail_read is not None:
            handlers[("browser.invoke", "yunda.send_waybill.renderer_detail")] = (
                self.yunda_renderer_detail
            )
        if self._ports.replace_waybill_snapshot is not None:
            handlers[("projection.invoke", "waybill.snapshot.replace")] = self.replace_waybill_snapshot
        if self._ports.replace_arrival_forecast_snapshot is not None:
            handlers[("projection.invoke", "arrival.forecast_snapshot.replace")] = (
                self.replace_arrival_forecast_snapshot
            )
        if self._ports.replace_scan_snapshot is not None:
            handlers[("projection.invoke", "scan.snapshot.replace")] = self.replace_scan_snapshot
        if self._ports.read_scan_snapshot is not None:
            handlers[("projection.invoke", "scan.snapshot.read")] = self.read_scan_snapshot
        if self._ports.cleanup_scan_snapshot is not None:
            handlers[("projection.invoke", "scan.snapshot.cleanup")] = self.cleanup_scan_snapshot
        if self._ports.read_completed_arrivals_before is not None:
            handlers[("projection.invoke", "arrival.snapshot.completed_before")] = (
                self.read_completed_arrivals
            )
        if self._ports.read_pending_waybills is not None:
            handlers[("projection.invoke", "waybill.pending.read")] = self.read_pending_waybills
        if self._ports.replace_arrival_snapshot is not None:
            handlers[("projection.invoke", "arrival.snapshot.replace")] = (
                self.replace_arrival_snapshot
            )
        if self._ports.refresh_split_pending_snapshot is not None:
            handlers[("projection.invoke", "split_pending.snapshot.refresh")] = (
                self.refresh_split_pending_snapshot
            )
        if (
            self._ports.replace_arrive_sheet_resource is not None
            or
            self._ports.replace_sheet_resource is not None
            or self._ports.replace_arrival_stats_sheet is not None
        ):
            handlers[("network.request", "feishu.sheet.replace")] = self.replace_sheet
        if self._ports.archive_arrival_stats_sheet is not None:
            handlers[("network.request", "feishu.sheet.add")] = self.archive_arrival_stats
        if (
            self._ports.scan_next_submit is not None
            and self._ports.scan_next_verify is not None
        ):
            handlers[("browser.invoke", "ronghui.scan_next.submit")] = self.submit_scan_next
            handlers[("browser.invoke", "ronghui.scan_next.verify")] = self.verify_scan_next
        if self._ports.delivery_list_views is not None:
            handlers[("network.request", "feishu.bitable.list_views")] = (
                self.delivery_list_views
            )
        if self._ports.delivery_list_records is not None:
            handlers[("network.request", "feishu.bitable.list_records")] = (
                self.delivery_list_records
            )
        if self._ports.delivery_write_records is not None:
            handlers[("network.request", "feishu.bitable.write_records")] = (
                self.delivery_write_records
            )
        if self._ports.replace_bitable_resource is not None:
            handlers[("network.request", "feishu.bitable.replace_snapshot")] = self.replace_bitable
        if self._ports.append_yunda_dispatch_bitable is not None:
            handlers[("network.request", "feishu.bitable.append_yunda_dispatch_forecast")] = (
                self.append_yunda_dispatch_bitable
            )
        if self._ports.replace_yunda_send_bitable is not None:
            handlers[("network.request", "feishu.bitable.replace_yunda_send_waybills_date")] = (
                self.replace_yunda_send_bitable
            )
        if self._ports.replace_yunda_send_sheet is not None:
            handlers[("network.request", "feishu.sheet.replace_yunda_send_waybills")] = (
                self.replace_yunda_send_sheet
            )
        if self._ports.replace_yunda_waybill_projection is not None:
            handlers[("projection.invoke", "waybill.yunda.replace_date")] = (
                self.replace_yunda_waybill_projection
            )
        if self._ports.delivery_projection_update is not None:
            handlers[("projection.invoke", "waybill.delivery_status.update")] = (
                self.delivery_projection_update
            )
        return handlers


def build_first_party_core_handler_map(
    ports: FirstPartyCoreHandlerPorts,
    *,
    cursor_secret: bytes | None = None,
) -> dict[tuple[str, str], CoreBrokerHandler]:
    """Build exact handler pairs; absent ports remain unregistered."""

    return _FirstPartyCoreHandlers(
        ports,
        secret=cursor_secret or secrets.token_bytes(32),
    ).handler_map()


__all__ = [
    "FirstPartyCoreHandlerPorts",
    "MARKED_WRITE_ACTION_KEYS",
    "build_first_party_core_handler_map",
    "customer_problem_identity",
]
