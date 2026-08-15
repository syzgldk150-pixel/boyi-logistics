"""Production bindings for the signed daily send-order broker primitives."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from agent.automation_plugins.daily_send_handlers import (
    DailySendHandlerPorts,
    build_daily_send_handler_map,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.tms_runtime.account_manager import AutomationAccountManager, get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError


ResourceLoader = Callable[[str], Mapping[str, Any] | None]
FeishuOperation = Callable[[str, dict[str, Any]], Mapping[str, Any]]
SourcePageReader = Callable[
    [Mapping[str, Any], str, int, int], Mapping[str, Any]
]
ProjectionSync = Callable[[list[dict[str, Any]], str], Mapping[str, Any]]
ProjectionRead = Callable[[str], Sequence[Mapping[str, Any]]]
ProjectionLookup = Callable[[str], Mapping[str, Any] | None]


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
_BITABLE_NUMERIC_FIELDS = frozenset(
    {"件数", "实际重量", "录单金额", "体积重量", "体积", "结算重量", "到付款"}
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
_MAX_PAGES = 50
_PAGE_SIZE = 200


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _required_profile(descriptor: Mapping[str, Any]) -> str:
    profile = str(descriptor.get("session_profile") or "").strip()
    if not profile:
        raise _error("the exact daily-send account has no session profile", "BLOCKED_LOGIN")
    return profile


def _authenticated_account(
    manager: AutomationAccountManager,
    account_id: str,
) -> Mapping[str, Any]:
    try:
        descriptor = manager.require_authenticated_binding(account_id)
    except TMSAuthStateError as exc:
        raise _error(
            "the exact daily-send account is no longer authenticated",
            "BLOCKED_LOGIN",
        ) from exc
    if str(descriptor.get("account_id") or "").strip() != account_id:
        raise _error("the daily-send account binding changed", "BROKER_ACCOUNT_INVALID")
    if str(descriptor.get("system") or "").strip().lower() != "ronghui":
        raise _error(
            "the daily-send account is not a Ronghui account",
            "BROKER_ACCOUNT_SYSTEM_MISMATCH",
        )
    return descriptor


def _default_source_page(
    descriptor: Mapping[str, Any],
    target_date: str,
    page_index: int,
    page_size: int,
) -> Mapping[str, Any]:
    from agent.tms_runtime.scripts import Send_order

    auth = Send_order.TMSAuth(profile=_required_profile(descriptor))
    session = auth.login_and_get_session()
    if session is None:
        raise _error("Ronghui login did not return a session", "BLOCKED_LOGIN")
    try:
        payload = Send_order.fetch_send_orders(
            session,
            Send_order._build_date_range(date.fromisoformat(target_date)),
            page_index=page_index,
            page_size=page_size,
            referer=Send_order.DEFAULT_REFERER,
        )
    except TMSAuthStateError as exc:
        raise _error("the daily-send session expired", "BLOCKED_LOGIN") from exc
    except Exception as exc:
        raise _error("the Ronghui send-order page is unavailable", "BROKER_SOURCE_FAILED") from exc
    if not isinstance(payload, Mapping):
        raise _error("the Ronghui send-order page is invalid", "BROKER_SOURCE_INVALID")
    rows = payload.get("data")
    total = payload.get("total")
    if not isinstance(rows, list) or isinstance(total, bool):
        raise _error("the Ronghui send-order page is invalid", "BROKER_SOURCE_INVALID")
    try:
        total_value = int(total)
    except (TypeError, ValueError) as exc:
        raise _error("the Ronghui send-order total is invalid", "BROKER_SOURCE_INVALID") from exc
    if str(total_value) != str(total).strip() or total_value < 0:
        raise _error("the Ronghui send-order total is invalid", "BROKER_SOURCE_INVALID")
    return {"items": rows, "total": total_value}


def _default_resource_loader(resource_id: str) -> Mapping[str, Any] | None:
    from agent.workflow_resource_store import get_workflow_resource

    return get_workflow_resource(resource_id)


def _default_feishu_operation(action: str, params: dict[str, Any]) -> Mapping[str, Any]:
    from tools.feishu_cli_tool import feishu_operation

    return feishu_operation(action, params)


def _default_projection_sync(
    records: list[dict[str, Any]],
    target_date: str,
) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import sync_console_waybills

    return sync_console_waybills(
        records,
        source="ronghui",
        target_date=target_date,
        replace_date=True,
    )


def _default_projection_read(target_date: str) -> Sequence[Mapping[str, Any]]:
    from tools.phase7_mysql_store import list_console_waybills_by_source_date

    return list_console_waybills_by_source_date(
        source="ronghui",
        target_date=target_date,
    )


def _default_projection_lookup(waybill_no: str) -> Mapping[str, Any] | None:
    from tools.phase7_mysql_store import get_console_waybill_by_number

    return get_console_waybill_by_number(waybill_no)


def _bitable_coordinates(
    resource_loader: ResourceLoader,
    resource_id: str,
) -> tuple[str, str]:
    resource = resource_loader(resource_id)
    if not isinstance(resource, Mapping):
        raise _error("the exact daily-send Bitable resource is unavailable", "BROKER_RESOURCE_UNAVAILABLE")
    if str(resource.get("resource_kind") or "").strip().lower() != "feishu_bitable":
        raise _error("the daily-send resource kind changed", "BROKER_RESOURCE_KIND_MISMATCH")
    base_token = str(resource.get("base_token") or "").strip()
    table_id = str(resource.get("table_id") or "").strip()
    if not base_token or not table_id:
        raise _error("the daily-send Bitable resource is incomplete", "BROKER_RESOURCE_UNAVAILABLE")
    return base_token, table_id


def _record_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = (
        payload.get("data", {}).get("items") if isinstance(payload.get("data"), Mapping) else None,
        payload.get("items"),
        payload.get("records"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            if any(not isinstance(item, Mapping) for item in candidate):
                raise _error(
                    "daily-send Bitable record page is invalid",
                    "BROKER_SOURCE_INVALID",
                )
            return [dict(item) for item in candidate]
    raise _error(
        "daily-send Bitable record page is incomplete",
        "BROKER_SOURCE_INVALID",
    )


def _list_page(
    *,
    resource_loader: ResourceLoader,
    feishu_operation: FeishuOperation,
    resource_id: str,
    offset: int,
    page_size: int,
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    base_token, table_id = _bitable_coordinates(resource_loader, resource_id)
    try:
        result = feishu_operation(
            "list_records",
            {
                "base_token": base_token,
                "table_id": table_id,
                "limit": page_size,
                "offset": offset,
                "as": "bot",
            },
        )
    except Exception as exc:
        raise _error("daily-send Bitable fresh read failed", "BROKER_SOURCE_FAILED") from exc
    if not isinstance(result, Mapping) or result.get("error") or result.get("errors"):
        raise _error("daily-send Bitable fresh read failed", "BROKER_SOURCE_FAILED")
    items = _record_items(result)
    output: list[dict[str, Any]] = []
    for item in items:
        record_id = str(item.get("record_id") or "").strip()
        raw_fields = item.get("fields")
        if not record_id or not isinstance(raw_fields, Mapping):
            raise _error("daily-send Bitable record is invalid", "BROKER_SOURCE_INVALID")
        output.append(
            {
                "record_id": record_id,
                "fields": {field: raw_fields.get(field) for field in fields},
            }
        )
    return output


def _list_all(
    *,
    resource_loader: ResourceLoader,
    feishu_operation: FeishuOperation,
    resource_id: str,
    fields: tuple[str, ...] = _BITABLE_FIELDS,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_index in range(_MAX_PAGES):
        page = _list_page(
            resource_loader=resource_loader,
            feishu_operation=feishu_operation,
            resource_id=resource_id,
            offset=page_index * _PAGE_SIZE,
            page_size=_PAGE_SIZE,
            fields=fields,
        )
        new_records = []
        for record in page:
            record_id = str(record["record_id"])
            if record_id in seen:
                raise _error("daily-send Bitable pagination repeated a record", "BROKER_SOURCE_INVALID")
            seen.add(record_id)
            new_records.append(record)
        records.extend(new_records)
        if len(page) < _PAGE_SIZE:
            return records
    raise _error("daily-send Bitable pagination exceeded its signed limit", "BROKER_SOURCE_INVALID")


def _canonical_field(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return tuple(_canonical_field(item) for item in value)
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "link"):
            if key in value:
                candidate = _canonical_field(value.get(key))
                if candidate not in ("", (), None):
                    return candidate
        return tuple((str(key), _canonical_field(item)) for key, item in sorted(value.items()))
    return str(value).strip()


def _date_value(value: object) -> str:
    from agent.automation_plugins.daily_send_handlers import _date_text

    return _date_text(value)


def _canonical_bitable_record(fields: Mapping[str, Any]) -> dict[str, Any]:
    if set(fields) != set(_BITABLE_FIELDS):
        raise _error("daily-send Bitable fields are incomplete", "BROKER_SOURCE_INVALID")
    output = {field: _canonical_field(fields.get(field)) for field in _BITABLE_FIELDS}
    output["发件日期"] = _date_value(fields.get("发件日期"))
    output["运单编号"] = "".join(str(output["运单编号"]).removeprefix("=").strip(" '\"").split())
    for field in _BITABLE_NUMERIC_FIELDS:
        raw = fields.get(field)
        if raw in (None, ""):
            output[field] = None
            continue
        try:
            number = Decimal(str(raw).replace(",", "").strip())
        except (InvalidOperation, ValueError) as exc:
            raise _error("daily-send Bitable numeric field is invalid", "BROKER_SOURCE_INVALID") from exc
        if not number.is_finite():
            raise _error("daily-send Bitable numeric field is invalid", "BROKER_SOURCE_INVALID")
        output[field] = format(number.normalize(), "f")
    if not output["发件日期"] or not output["运单编号"]:
        raise _error("daily-send Bitable identity is invalid", "BROKER_SOURCE_INVALID")
    return output


def _verify_bitable_records(
    desired: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> bool:
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for record in desired:
        fields = record.get("fields")
        if not isinstance(fields, Mapping):
            return False
        canonical = _canonical_bitable_record(fields)
        key = (str(canonical["发件日期"]), str(canonical["运单编号"]))
        if key in expected:
            return False
        expected[key] = canonical
    actual: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in observed:
        fields = record.get("fields")
        if not isinstance(fields, Mapping):
            return False
        canonical = _canonical_bitable_record(fields)
        key = (str(canonical["发件日期"]), str(canonical["运单编号"]))
        if key in expected:
            actual.setdefault(key, []).append(canonical)
    return all(actual.get(key) == [value] for key, value in expected.items())


def _normalize_projection(record: Mapping[str, Any]) -> dict[str, str] | None:
    payload = {field: str(record.get(field, "") or "").strip() for field in _PROJECTION_FIELDS}
    return payload if payload["waybill_no"] else None


def _verify_projection(
    desired: Sequence[Mapping[str, Any]],
    observed: Sequence[Mapping[str, Any]],
) -> bool:
    expected: dict[str, dict[str, str]] = {}
    for raw in desired:
        record = _normalize_projection(raw)
        if record is None or record["waybill_no"] in expected:
            return False
        expected[record["waybill_no"]] = record
    actual: dict[str, list[dict[str, str]]] = {}
    for raw in observed:
        record = _normalize_projection(raw)
        if record is None:
            return False
        if record["status"] == "cancelled" and record["waybill_no"] not in expected:
            continue
        actual.setdefault(record["waybill_no"], []).append(record)
    for identity, wanted in expected.items():
        rows = actual.get(identity)
        if rows is None or len(rows) != 1:
            return False
        observed_row = rows[0]
        for field in _PROJECTION_FIELDS:
            if field == "status" and observed_row[field] == "cancelled":
                continue
            if observed_row[field] != wanted[field]:
                return False
    return not (set(actual) - set(expected))


def build_production_daily_send_ports(
    *,
    account_manager: AutomationAccountManager | None = None,
    resource_loader: ResourceLoader | None = None,
    feishu_operation: FeishuOperation | None = None,
    source_page: SourcePageReader | None = None,
    projection_sync: ProjectionSync | None = None,
    projection_read: ProjectionRead | None = None,
    projection_lookup: ProjectionLookup | None = None,
) -> DailySendHandlerPorts:
    manager = account_manager or get_account_manager()
    load_resource = resource_loader or _default_resource_loader
    invoke_feishu = feishu_operation or _default_feishu_operation
    read_source = source_page or _default_source_page
    sync_projection = projection_sync or _default_projection_sync
    read_projection = projection_read or _default_projection_read
    lookup_projection = projection_lookup or _default_projection_lookup

    def list_records(
        resource_id: str,
        offset: int,
        page_size: int,
        fields: tuple[str, ...],
    ) -> Sequence[Mapping[str, Any]]:
        return _list_page(
            resource_loader=load_resource,
            feishu_operation=invoke_feishu,
            resource_id=resource_id,
            offset=offset,
            page_size=page_size,
            fields=fields,
        )

    def delete_records(resource_id: str, record_ids: tuple[str, ...]) -> Mapping[str, Any]:
        base_token, table_id = _bitable_coordinates(load_resource, resource_id)
        before = _list_all(
            resource_loader=load_resource,
            feishu_operation=invoke_feishu,
            resource_id=resource_id,
            fields=("运单编号", "发件日期"),
        )
        before_ids = {str(item.get("record_id") or "") for item in before}
        if not set(record_ids) <= before_ids:
            raise _error(
                "daily-send Bitable delete identity is missing",
                "BROKER_SOURCE_INVALID",
            )
        preserved_ids = before_ids - set(record_ids)
        try:
            invoke_feishu(
                "delete_records",
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "record_ids": list(record_ids),
                    "as": "bot",
                },
            )
        except Exception:
            pass
        try:
            observed = _list_all(
                resource_loader=load_resource,
                feishu_operation=invoke_feishu,
                resource_id=resource_id,
                fields=("运单编号", "发件日期"),
            )
        except Exception as exc:
            raise _error(
                "daily-send Bitable delete readback is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        remaining_ids = {str(item.get("record_id") or "") for item in observed}
        verified = (
            not (set(record_ids) & remaining_ids)
            and preserved_ids <= remaining_ids
        )
        return {
            "ok": verified,
            "verified": verified,
            "deleted": len(record_ids) if verified else 0,
        }

    def write_records(resource_id: str, records: list[dict[str, Any]]) -> Mapping[str, Any]:
        base_token, table_id = _bitable_coordinates(load_resource, resource_id)
        before = _list_all(
            resource_loader=load_resource,
            feishu_operation=invoke_feishu,
            resource_id=resource_id,
        )
        before_ids = {str(item.get("record_id") or "") for item in before}
        try:
            invoke_feishu(
                "write_records",
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "records": records,
                    "as": "bot",
                },
            )
        except Exception:
            pass
        try:
            observed = _list_all(
                resource_loader=load_resource,
                feishu_operation=invoke_feishu,
                resource_id=resource_id,
            )
        except Exception as exc:
            raise _error(
                "daily-send Bitable write readback is invalid",
                "WRITE_OUTCOME_UNKNOWN",
            ) from exc
        after_ids = {str(item.get("record_id") or "") for item in observed}
        verified = (
            before_ids <= after_ids
            and len(after_ids - before_ids) == len(records)
            and _verify_bitable_records(records, observed)
        )
        return {
            "ok": verified,
            "verified": verified,
            "written": len(records) if verified else 0,
        }

    def replace_projection(
        records: list[dict[str, Any]],
        target_date: str,
    ) -> Mapping[str, Any]:
        existed = {
            str(record["waybill_no"]): lookup_projection(str(record["waybill_no"])) is not None
            for record in records
        }
        before = list(read_projection(target_date))
        try:
            sync_projection(records, target_date)
        except Exception:
            pass
        after = list(read_projection(target_date))
        verified = _verify_projection(records, after)
        expected_ids = {str(record["waybill_no"]) for record in records}
        before_active = {
            str(record.get("waybill_no") or "")
            for record in before
            if str(record.get("status") or "") != "cancelled"
        }
        after_active = {
            str(record.get("waybill_no") or "")
            for record in after
            if str(record.get("status") or "") != "cancelled"
        }
        updates = sum(1 for identity in expected_ids if existed.get(identity))
        creates = len(expected_ids) - updates
        deleted_stale = len(before_active - after_active)
        return {
            "ok": verified,
            "verified": verified,
            "upserted": len(expected_ids),
            "updates": updates,
            "creates": creates,
            "deleted_stale": deleted_stale,
        }

    return DailySendHandlerPorts(
        describe_account=lambda account_id: _authenticated_account(manager, account_id),
        source_page=read_source,
        bitable_list=list_records,
        bitable_delete=delete_records,
        bitable_write=write_records,
        projection_replace=replace_projection,
    )


def build_production_daily_send_handler_map(
    *,
    cursor_secret: bytes,
    account_manager: AutomationAccountManager | None = None,
) -> dict[tuple[str, str], Any]:
    return build_daily_send_handler_map(
        build_production_daily_send_ports(account_manager=account_manager),
        cursor_secret=cursor_secret,
    )


__all__ = [
    "build_production_daily_send_handler_map",
    "build_production_daily_send_ports",
]
