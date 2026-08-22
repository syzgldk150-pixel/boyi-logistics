"""Production ports with fresh readback for delivery/site plugin writes."""

from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, NoReturn, Sequence

from agent.automation_plugins.delivery_site_handlers import (
    DELIVERY_SITE_WRITE_ACTION_KEYS,
    DeliverySiteHandlerPorts,
    WriteStartMarker,
    build_delivery_site_handler_map,
)
from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.tms_runtime.account_manager import (
    AutomationAccountManager,
    get_account_manager,
)
from agent.tms_runtime.errors import TMSAuthStateError


ResourceLoader = Callable[[str], Mapping[str, Any] | None]
FeishuOperation = Callable[[str, dict[str, Any]], Mapping[str, Any]]
SiteBitableSync = Callable[
    [str, list[dict[str, Any]], dict[str, Any], WriteStartMarker],
    Mapping[str, Any],
]
SiteSheetSync = Callable[
    [str, list[list[Any]], dict[str, Any], WriteStartMarker],
    Mapping[str, Any],
]
ProjectionRead = Callable[[tuple[str, ...]], Sequence[Mapping[str, Any]]]
ProjectionWrite = Callable[[list[str], str, WriteStartMarker], Mapping[str, Any]]


_MAX_RECORDS = 20_000
_BITABLE_PAGE_SIZE = 500
_MAX_BITABLE_PAGES = 50
_SITE_EXTERNAL_FIELDS = {
    "tracking_number": "运单编号",
    "send_site": "发货网点",
    "package_type": "包装类型",
    "destination": "目的网点",
    "pieces": "件数",
    "weight": "重量",
}
_SITE_TEXT_FIELDS = frozenset({"tracking_number", "send_site", "package_type", "destination"})
_SITE_NUMBER_FIELDS = frozenset({"pieces", "weight"})
_DELIVERY_FIELDS = ("运单编号", "签收状态")
_SITE_SHEET_COLUMNS = (
    "tracking_number",
    "send_site",
    "package_type",
    "pieces",
    "weight",
    "destination",
)


def _error(message: str, code: str) -> PluginExecutionError:
    return PluginExecutionError(message, code=code)


def _write_unknown(message: str, *, cause: Exception | None = None) -> NoReturn:
    error = _error(message, "WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def _write_started(marker: WriteStartMarker) -> bool:
    """Return the delegated marker state when the closed handler tracks it."""

    return (
        getattr(marker, "observable", False) is True
        and getattr(marker, "started", False) is True
    )


def _nested_write_failure(
    marker: WriteStartMarker,
    message: str,
    *,
    cause: Exception | None = None,
) -> NoReturn:
    """Keep a pre-write nested failure out of unknown-write recovery."""

    if getattr(marker, "observable", False) is True and not _write_started(marker):
        error = _error(message, "FAILED_BEFORE_WRITE")
    else:
        error = _error(message, "WRITE_OUTCOME_UNKNOWN")
    if cause is None:
        raise error
    raise error from cause


def _default_resource_loader(resource_id: str) -> Mapping[str, Any] | None:
    from agent.workflow_resource_store import get_workflow_resource

    return get_workflow_resource(resource_id)


def _default_feishu_operation(
    action: str,
    params: dict[str, Any],
) -> Mapping[str, Any]:
    from tools.feishu_cli_tool import feishu_operation

    return feishu_operation(action, params)


def _default_site_bitable_sync(
    resource_id: str,
    records: list[dict[str, Any]],
    params: dict[str, Any],
    write_started: WriteStartMarker,
) -> Mapping[str, Any]:
    from tools.phase7_sync_common import sync_bitable_snapshot

    return sync_bitable_snapshot(
        resource_id,
        records,
        params,
        mark_write_started=write_started,
    )


def _default_site_sheet_sync(
    resource_id: str,
    rows: list[list[Any]],
    params: dict[str, Any],
    write_started: WriteStartMarker,
) -> Mapping[str, Any]:
    from tools.phase7_sync_common import sync_sheet_snapshot

    return sync_sheet_snapshot(
        resource_id,
        rows,
        params,
        mark_write_started=write_started,
    )


def _default_projection_read(
    bill_codes: tuple[str, ...],
) -> Sequence[Mapping[str, Any]]:
    from tools.phase7_mysql_store import list_console_waybills_by_numbers

    return list_console_waybills_by_numbers(list(bill_codes))


def _default_projection_write(
    bill_codes: list[str],
    status: str,
    write_started: WriteStartMarker,
) -> Mapping[str, Any]:
    from tools.phase7_mysql_store import update_console_waybill_statuses

    return update_console_waybill_statuses(
        bill_codes,
        status,
        mark_write_started=write_started,
    )


def _describe_authenticated_account(
    manager: AutomationAccountManager,
    account_id: str,
) -> Mapping[str, Any]:
    try:
        descriptor = manager.require_authenticated_binding(account_id)
    except TMSAuthStateError as exc:
        raise _error(
            "the exact delivery account is no longer authenticated",
            "BLOCKED_LOGIN",
        ) from exc
    if not isinstance(descriptor, Mapping):
        raise _error("the delivery account descriptor is invalid", "BROKER_ACCOUNT_INVALID")
    if str(descriptor.get("account_id") or "").strip() != account_id:
        raise _error("the delivery account binding changed", "BROKER_ACCOUNT_MISMATCH")
    if str(descriptor.get("system") or "").strip().lower() != "ronghui":
        raise _error(
            "the delivery account is not a Ronghui account",
            "BROKER_ACCOUNT_SYSTEM_MISMATCH",
        )
    if not str(descriptor.get("session_profile") or "").strip():
        raise _error("the delivery account is not authenticated", "BLOCKED_LOGIN")
    return descriptor


def _exact_resource(
    loader: ResourceLoader,
    resource_id: str,
    *,
    kind: str,
    required_fields: tuple[str, ...],
) -> dict[str, Any]:
    try:
        raw = loader(resource_id)
    except PluginExecutionError:
        raise
    except Exception as exc:
        raise _error(
            "the exact managed resource is unavailable",
            "BROKER_RESOURCE_UNAVAILABLE",
        ) from exc
    if not isinstance(raw, Mapping):
        raise _error(
            "the exact managed resource no longer exists",
            "BROKER_RESOURCE_UNAVAILABLE",
        )
    resource = dict(raw)
    metadata = resource.get("_meta")
    if (
        resource.get("resource_kind") != kind
        or not isinstance(metadata, Mapping)
        or str(metadata.get("resource_key") or "").strip() != resource_id
    ):
        raise _error(
            "the exact managed resource changed kind or identity",
            "BROKER_RESOURCE_MISMATCH",
        )
    if any(not str(resource.get(field) or "").strip() for field in required_fields):
        raise _error(
            "the exact managed resource configuration is incomplete",
            "BROKER_RESOURCE_INVALID",
        )
    return resource


def _record_items(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool | None]:
    if payload.get("error") or payload.get("errors"):
        raise _error("Bitable fresh read failed", "BROKER_RESOURCE_UNAVAILABLE")
    data = payload.get("data")
    nested = data if isinstance(data, Mapping) else {}
    candidates = (nested.get("items"), payload.get("items"), payload.get("records"))
    items: Any = None
    for candidate in candidates:
        if isinstance(candidate, list):
            items = candidate
            break
    if items is None or any(not isinstance(item, Mapping) for item in items):
        raise _error("Bitable fresh read schema is invalid", "BROKER_SOURCE_INVALID")
    has_more = nested.get("has_more", payload.get("has_more"))
    if has_more is not None and not isinstance(has_more, bool):
        raise _error("Bitable fresh read pagination is invalid", "BROKER_SOURCE_INVALID")
    return [dict(item) for item in items], has_more


def _read_all_bitable(
    invoke: FeishuOperation,
    *,
    base_token: str,
    table_id: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for page_index in range(_MAX_BITABLE_PAGES):
        try:
            result = invoke(
                "list_records",
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "limit": _BITABLE_PAGE_SIZE,
                    "offset": page_index * _BITABLE_PAGE_SIZE,
                    "as": "bot",
                    "dry_run": False,
                },
            )
        except PluginExecutionError:
            raise
        except Exception as exc:
            raise _error(
                "Bitable fresh read failed",
                "BROKER_RESOURCE_UNAVAILABLE",
            ) from exc
        if not isinstance(result, Mapping):
            raise _error("Bitable fresh read schema is invalid", "BROKER_SOURCE_INVALID")
        items, has_more = _record_items(result)
        for item in items:
            record_id = str(item.get("record_id") or "").strip()
            if not record_id or record_id in seen_ids:
                raise _error(
                    "Bitable fresh read identity is missing or duplicated",
                    "BROKER_SOURCE_INVALID",
                )
            seen_ids.add(record_id)
            output.append(item)
            if len(output) > _MAX_RECORDS:
                raise _error(
                    "Bitable fresh read exceeded its closed record limit",
                    "BROKER_SOURCE_INVALID",
                )
        if has_more is False or (has_more is None and len(items) < _BITABLE_PAGE_SIZE):
            return output
        if not items:
            raise _error(
                "Bitable fresh read pagination did not advance",
                "BROKER_SOURCE_INVALID",
            )
    raise _error(
        "Bitable fresh read exceeded its closed page limit",
        "BROKER_SOURCE_INVALID",
    )


def _field_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float, Decimal)):
        return str(value).strip()
    if isinstance(value, list):
        return "".join(_field_text(item) for item in value).strip()
    if isinstance(value, Mapping):
        for key in ("text", "value", "name", "link"):
            if key in value:
                candidate = _field_text(value.get(key))
                if candidate:
                    return candidate
        return "".join(_field_text(item) for item in value.values()).strip()
    return str(value).strip()


def _number_text(value: object) -> str | None:
    text = _field_text(value).replace(",", "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise _error("snapshot numeric field is invalid", "BROKER_SOURCE_INVALID") from exc
    if not number.is_finite():
        raise _error("snapshot numeric field is invalid", "BROKER_SOURCE_INVALID")
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _site_bitable_snapshot(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_identity: dict[str, dict[str, Any]] = {}
    required = set(_SITE_EXTERNAL_FIELDS.values())
    for raw in records:
        fields = raw.get("fields")
        if not isinstance(fields, Mapping) or not required <= set(fields):
            raise _error(
                "site-send Bitable fresh row is missing a reviewed field",
                "BROKER_SOURCE_INVALID",
            )
        canonical: dict[str, Any] = {}
        for role, external in _SITE_EXTERNAL_FIELDS.items():
            if role in _SITE_TEXT_FIELDS:
                canonical[role] = _field_text(fields.get(external))
            elif role in _SITE_NUMBER_FIELDS:
                canonical[role] = _number_text(fields.get(external))
        identity = str(canonical["tracking_number"])
        if not identity or identity in by_identity:
            raise _error(
                "site-send Bitable identity is missing or duplicated",
                "BROKER_SOURCE_INVALID",
            )
        by_identity[identity] = canonical
    return [by_identity[identity] for identity in sorted(by_identity)]


def _delivery_bitable_snapshot(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for raw in records:
        record_id = str(raw.get("record_id") or "").strip()
        fields = raw.get("fields")
        if not record_id or not isinstance(fields, Mapping) or any(field not in fields for field in _DELIVERY_FIELDS):
            raise _error(
                "delivery Bitable fresh row is missing its identity fields",
                "BROKER_SOURCE_INVALID",
            )
        waybill_no = "".join(_field_text(fields.get("运单编号")).removeprefix("=").strip(" '\"").split())
        status = "".join(_field_text(fields.get("签收状态")).split())
        if not waybill_no:
            raise _error(
                "delivery Bitable fresh row has no waybill identity",
                "BROKER_SOURCE_INVALID",
            )
        output.append(
            {
                "record_id": record_id,
                "waybill_no": waybill_no,
                "status": status,
            }
        )
    return sorted(output, key=lambda item: item["record_id"])


def _sheet_values(payload: Mapping[str, Any]) -> list[list[Any]]:
    if payload.get("error") or payload.get("errors"):
        raise _error("Sheet fresh read failed", "BROKER_RESOURCE_UNAVAILABLE")
    data = payload.get("data")
    nested = data if isinstance(data, Mapping) else {}
    value_range = nested.get("valueRange")
    value_mapping = value_range if isinstance(value_range, Mapping) else {}
    nested_data = nested.get("data")
    nested_mapping = nested_data if isinstance(nested_data, Mapping) else {}
    candidates = (
        value_mapping.get("values"),
        nested_mapping.get("values"),
        nested.get("values"),
        payload.get("values"),
    )
    values: Any = None
    for candidate in candidates:
        if isinstance(candidate, list):
            values = candidate
            break
    if values is None or any(not isinstance(row, list) for row in values):
        raise _error("Sheet fresh read schema is invalid", "BROKER_SOURCE_INVALID")
    return [list(row) for row in values]


def _site_sheet_snapshot(rows: Sequence[Sequence[Any]]) -> list[list[Any]]:
    if len(rows) > _MAX_RECORDS:
        raise _error(
            "site-send Sheet fresh read exceeded its closed row limit",
            "BROKER_SOURCE_INVALID",
        )
    normalized: list[list[Any]] = []
    for raw in rows:
        if len(raw) > len(_SITE_SHEET_COLUMNS):
            raise _error("site-send Sheet row has extra fields", "BROKER_SOURCE_INVALID")
        row = list(raw) + [""] * (len(_SITE_SHEET_COLUMNS) - len(raw))
        canonical = [
            _field_text(row[0]),
            _field_text(row[1]),
            _field_text(row[2]),
            _number_text(row[3]),
            _number_text(row[4]),
            _field_text(row[5]),
        ]
        if not any(value not in (None, "") for value in canonical):
            normalized.append(canonical)
            continue
        if not canonical[0]:
            raise _error(
                "site-send Sheet row has no identity",
                "BROKER_SOURCE_INVALID",
            )
        normalized.append(canonical)
    while normalized and not any(value not in (None, "") for value in normalized[-1]):
        normalized.pop()
    identities = [str(row[0]) for row in normalized]
    if len(identities) != len(set(identities)):
        raise _error(
            "site-send Sheet identities are duplicated",
            "BROKER_SOURCE_INVALID",
        )
    return normalized


def _read_sheet(
    invoke: FeishuOperation,
    *,
    spreadsheet_token: str,
    value_range: str,
) -> list[list[Any]]:
    try:
        result = invoke(
            "read_sheet",
            {
                "spreadsheet_token": spreadsheet_token,
                "range": value_range,
                "as": "bot",
                "dry_run": False,
            },
        )
    except PluginExecutionError:
        raise
    except Exception as exc:
        raise _error("Sheet fresh read failed", "BROKER_RESOURCE_UNAVAILABLE") from exc
    if not isinstance(result, Mapping):
        raise _error("Sheet fresh read schema is invalid", "BROKER_SOURCE_INVALID")
    return _sheet_values(result)


def _snapshot_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _verified_result(
    *,
    before: object,
    after: object,
    count: int,
) -> dict[str, Any]:
    return {
        "ok": True,
        "verified": True,
        "record_count": count,
        "before_sha256": _snapshot_digest(before),
        "after_sha256": _snapshot_digest(after),
        "before_observation_id": secrets.token_hex(16),
        "after_observation_id": secrets.token_hex(16),
        "write_response_received": True,
        "acknowledged_count": count,
    }


def _response_count(
    payload: object,
    *,
    field: str,
    expected: int,
) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("ok") is True
        and not payload.get("error")
        and not payload.get("errors")
        and not isinstance(payload.get(field), bool)
        and isinstance(payload.get(field), int)
        and payload.get(field) == expected
    )


def _projection_snapshot(
    rows: Sequence[Mapping[str, Any]],
    requested: tuple[str, ...],
) -> list[dict[str, str]]:
    from tools.phase7_mysql_store import CONSOLE_WAYBILL_FIELDS

    required = set(CONSOLE_WAYBILL_FIELDS)
    requested_set = set(requested)
    by_identity: dict[str, dict[str, str]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or not required <= set(raw):
            raise _error(
                "delivery projection fresh row is incomplete",
                "BROKER_SOURCE_INVALID",
            )
        identity = str(raw.get("waybill_no") or "").strip()
        if identity not in requested_set or identity in by_identity:
            raise _error(
                "delivery projection fresh identities are zero, multiple, or extra",
                "BROKER_SOURCE_INVALID",
            )
        by_identity[identity] = {field: str(raw.get(field) or "").strip() for field in CONSOLE_WAYBILL_FIELDS}
    if set(by_identity) != requested_set:
        raise _error(
            "delivery projection fresh identities are zero, multiple, or extra",
            "BROKER_SOURCE_INVALID",
        )
    return [by_identity[identity] for identity in sorted(by_identity)]


def build_production_delivery_site_ports(
    *,
    account_manager: AutomationAccountManager | None = None,
    resource_loader: ResourceLoader | None = None,
    feishu_operation: FeishuOperation | None = None,
    site_bitable_sync: SiteBitableSync | None = None,
    site_sheet_sync: SiteSheetSync | None = None,
    projection_read: ProjectionRead | None = None,
    projection_write: ProjectionWrite | None = None,
) -> DeliverySiteHandlerPorts:
    manager = account_manager or get_account_manager()
    load_resource = resource_loader or _default_resource_loader
    invoke_feishu = feishu_operation or _default_feishu_operation
    sync_bitable = site_bitable_sync or _default_site_bitable_sync
    sync_sheet = site_sheet_sync or _default_site_sheet_sync
    read_projection = projection_read or _default_projection_read
    write_projection = projection_write or _default_projection_write

    def mark_write_started(marker: WriteStartMarker) -> None:
        if marker is not None:
            marker()

    def replace_site_bitable(
        resource_id: str,
        records: list[dict[str, Any]],
        target_date: str,
        write_started: WriteStartMarker,
    ) -> Mapping[str, Any]:
        resource = _exact_resource(
            load_resource,
            resource_id,
            kind="feishu_bitable",
            required_fields=("base_token", "table_id"),
        )
        base_token = str(resource["base_token"])
        table_id = str(resource["table_id"])
        before_raw = _read_all_bitable(
            invoke_feishu,
            base_token=base_token,
            table_id=table_id,
        )
        before = _site_bitable_snapshot(before_raw)
        translated = [
            {"fields": {external: raw["fields"][role] for role, external in _SITE_EXTERNAL_FIELDS.items()}}
            for raw in records
        ]
        expected = _site_bitable_snapshot(translated)
        write_error: Exception | None = None
        response: object = None
        try:
            response = sync_bitable(
                resource_id,
                translated,
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "target_date": target_date,
                    "as": "bot",
                    "dry_run": False,
                },
                write_started,
            )
        except Exception as exc:
            write_error = exc
        if write_error is not None and not _write_started(write_started):
            _nested_write_failure(
                write_started,
                "site-send Bitable write failed before mutation",
                cause=write_error,
            )
        if isinstance(response, Mapping) and (response.get("error") or response.get("errors")) and not _write_started(write_started):
            _nested_write_failure(
                write_started,
                "site-send Bitable write failed before mutation",
            )
        try:
            after = _site_bitable_snapshot(
                _read_all_bitable(
                    invoke_feishu,
                    base_token=base_token,
                    table_id=table_id,
                )
            )
        except Exception as exc:
            _write_unknown("site-send Bitable post-write read failed", cause=exc)
        if write_error is not None:
            _write_unknown("site-send Bitable write response was lost", cause=write_error)
        if not _response_count(response, field="written", expected=len(records)):
            _write_unknown("site-send Bitable write response was incomplete")
        if after != expected:
            _write_unknown("site-send Bitable post-write snapshot changed")
        return _verified_result(before=before, after=after, count=len(records))

    def replace_site_sheet(
        resource_id: str,
        rows: list[list[Any]],
        target_date: str,
        write_started: WriteStartMarker,
    ) -> Mapping[str, Any]:
        resource = _exact_resource(
            load_resource,
            resource_id,
            kind="feishu_sheet",
            required_fields=("spreadsheet_token", "range"),
        )
        spreadsheet_token = str(resource["spreadsheet_token"])
        value_range = str(resource["range"])
        clear_range = str(resource.get("clear_range") or value_range).strip()
        from tools.phase7_sync_common import parse_a1_range

        try:
            value_shape = parse_a1_range(value_range)
            clear_shape = parse_a1_range(clear_range)
        except ValueError as exc:
            raise _error(
                "the site-send Sheet range is invalid",
                "BROKER_RESOURCE_INVALID",
            ) from exc
        if (
            value_shape["sheet"] != clear_shape["sheet"]
            or value_shape["start_col"] != clear_shape["start_col"]
            or value_shape["start_row"] != clear_shape["start_row"]
            or value_shape["col_count"] != len(_SITE_SHEET_COLUMNS)
            or clear_shape["col_count"] != len(_SITE_SHEET_COLUMNS)
        ):
            raise _error(
                "the site-send Sheet ranges do not describe one exact snapshot",
                "BROKER_RESOURCE_INVALID",
            )
        before = _site_sheet_snapshot(
            _read_sheet(
                invoke_feishu,
                spreadsheet_token=spreadsheet_token,
                value_range=clear_range,
            )
        )
        expected = _site_sheet_snapshot(rows)
        write_error: Exception | None = None
        response: object = None
        try:
            response = sync_sheet(
                resource_id,
                rows,
                {
                    "spreadsheet_token": spreadsheet_token,
                    "range": value_range,
                    "clear_range": clear_range,
                    "target_date": target_date,
                    "as": "bot",
                    "dry_run": False,
                },
                write_started,
            )
        except Exception as exc:
            write_error = exc
        if write_error is not None and not _write_started(write_started):
            _nested_write_failure(
                write_started,
                "site-send Sheet write failed before mutation",
                cause=write_error,
            )
        if isinstance(response, Mapping) and (response.get("error") or response.get("errors")) and not _write_started(write_started):
            _nested_write_failure(
                write_started,
                "site-send Sheet write failed before mutation",
            )
        try:
            after = _site_sheet_snapshot(
                _read_sheet(
                    invoke_feishu,
                    spreadsheet_token=spreadsheet_token,
                    value_range=clear_range,
                )
            )
        except Exception as exc:
            _write_unknown("site-send Sheet post-write read failed", cause=exc)
        if write_error is not None:
            _write_unknown("site-send Sheet write response was lost", cause=write_error)
        if not _response_count(response, field="rows", expected=len(rows)):
            _write_unknown("site-send Sheet write response was incomplete")
        if after != expected:
            _write_unknown("site-send Sheet post-write snapshot changed")
        return _verified_result(before=before, after=after, count=len(rows))

    def write_delivery_bitable(
        resource_id: str,
        records: list[dict[str, str]],
        write_started: WriteStartMarker,
    ) -> Mapping[str, Any]:
        resource = _exact_resource(
            load_resource,
            resource_id,
            kind="feishu_bitable",
            required_fields=("base_token", "table_id"),
        )
        base_token = str(resource["base_token"])
        table_id = str(resource["table_id"])
        before = _delivery_bitable_snapshot(
            _read_all_bitable(
                invoke_feishu,
                base_token=base_token,
                table_id=table_id,
            )
        )
        by_id = {row["record_id"]: dict(row) for row in before}
        if any(record["record_id"] not in by_id for record in records):
            raise _error(
                "delivery Bitable target identity is missing before write",
                "BROKER_SOURCE_INVALID",
            )
        expected = [dict(row) for row in before]
        expected_by_id = {row["record_id"]: row for row in expected}
        for record in records:
            expected_by_id[record["record_id"]]["status"] = "".join(record["status"].split())
        expected.sort(key=lambda item: item["record_id"])
        payload = [
            {
                "record_id": record["record_id"],
                "fields": {"签收状态": record["status"]},
            }
            for record in records
        ]
        write_error: Exception | None = None
        response: object = None
        try:
            mark_write_started(write_started)
            response = invoke_feishu(
                "write_records",
                {
                    "base_token": base_token,
                    "table_id": table_id,
                    "records": payload,
                    "as": "bot",
                    "dry_run": False,
                },
            )
        except Exception as exc:
            write_error = exc
        if write_error is not None and not _write_started(write_started):
            _nested_write_failure(
                write_started,
                "delivery Bitable write failed before mutation",
                cause=write_error,
            )
        try:
            after = _delivery_bitable_snapshot(
                _read_all_bitable(
                    invoke_feishu,
                    base_token=base_token,
                    table_id=table_id,
                )
            )
        except Exception as exc:
            _write_unknown("delivery Bitable post-write read failed", cause=exc)
        if write_error is not None:
            _write_unknown("delivery Bitable write response was lost", cause=write_error)
        if not _response_count(response, field="written", expected=len(records)):
            _write_unknown("delivery Bitable write response was incomplete")
        if after != expected:
            _write_unknown("delivery Bitable post-write snapshot changed")
        return _verified_result(before=before, after=after, count=len(records))

    def update_delivery_projection(
        bill_codes: tuple[str, ...],
        status: str,
        write_started: WriteStartMarker,
    ) -> Mapping[str, Any]:
        before = _projection_snapshot(read_projection(bill_codes), bill_codes)
        expected = [dict(row) for row in before]
        for row in expected:
            row["status"] = status
        write_error: Exception | None = None
        response: object = None
        try:
            response = write_projection(list(bill_codes), status, write_started)
        except Exception as exc:
            write_error = exc
        if write_error is not None and not _write_started(write_started):
            _nested_write_failure(
                write_started,
                "delivery projection update failed before mutation",
                cause=write_error,
            )
        if isinstance(response, Mapping) and (response.get("error") or response.get("errors")) and not _write_started(write_started):
            _nested_write_failure(
                write_started,
                "delivery projection update failed before mutation",
            )
        try:
            after = _projection_snapshot(read_projection(bill_codes), bill_codes)
        except Exception as exc:
            _write_unknown("delivery projection post-write read failed", cause=exc)
        if write_error is not None:
            _write_unknown("delivery projection write response was lost", cause=write_error)
        if not _response_count(response, field="updated", expected=len(bill_codes)):
            _write_unknown("delivery projection write response was incomplete")
        if after != expected:
            _write_unknown("delivery projection post-write snapshot changed")
        return _verified_result(before=before, after=after, count=len(bill_codes))

    return DeliverySiteHandlerPorts(
        describe_account=lambda account_id: _describe_authenticated_account(
            manager,
            account_id,
        ),
        site_bitable_replace=replace_site_bitable,
        site_sheet_replace=replace_site_sheet,
        delivery_bitable_write=write_delivery_bitable,
        delivery_projection_update=update_delivery_projection,
    )


def build_production_delivery_site_handler_map(
    *,
    cursor_secret: bytes,
    account_manager: AutomationAccountManager | None = None,
) -> dict[tuple[str, str], Any]:
    handlers = build_delivery_site_handler_map(
        build_production_delivery_site_ports(account_manager=account_manager),
        cursor_secret=cursor_secret,
    )
    if set(handlers) != set(DELIVERY_SITE_WRITE_ACTION_KEYS):
        raise ValueError("production delivery/site action set changed")
    return handlers


__all__ = [
    "build_production_delivery_site_handler_map",
    "build_production_delivery_site_ports",
]
