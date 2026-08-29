"""Shared Phase 7 MySQL tables and normalization helpers."""

from __future__ import annotations

import os
import platform
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable

import pymysql
from shared.runtime_repositories import WaybillRepository

_DECIMAL_2 = Decimal("0.01")
_DECIMAL_3 = Decimal("0.001")
_R_CHILD_TRACKING_RE = re.compile(r"^(?:R\d{11}|RC\d{10})\d{4}$")
_RONGHUI_NUMERIC_CHILD_TRACKING_RE = re.compile(r"^200\d{11}$")
_MASK_ONLY_RE = re.compile(r"^[*＊Xx]+$")

CONSOLE_WAYBILL_FIELDS = [
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
]

CONSOLE_WAYBILL_STATUS_VALUES = {"pending", "in_transit", "signed", "cancelled"}

WAYBILL_FIELDS = [
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
]

WAYBILL_EXPORT_HEADERS = [
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
]

PENDING_ARRIVAL_HEADERS = [
    "主单号",
    "目的站点",
    "应到件数",
    "已到件数",
    "未到件数",
    "状态",
    "首次扫描时间",
    "最近扫描时间",
]

_PENDING_STATUS_LABELS = {
    "pending": "未到货",
    "partial": "部分到货",
    "completed": "已到齐",
    "unknown": "未知",
}

_DICT_WAYBILL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "tracking_number": ("tracking_number", "bill_code", "billCode", "BILL_CODE", "trackingNumber"),
    "goods_name": ("goods_name", "goodsName", "GOODS_NAME"),
    "package_type": ("package_type", "packageType", "PACK_TYPE"),
    "delivery_method": ("delivery_method", "deliveryMethod", "DISPATCH_MODE"),
    "quantity": ("quantity", "qty", "pcs", "PIECE_NUMBER"),
    "receipt_number": ("receipt_number", "receiptNumber", "R_BILLCODE"),
    "actual_weight": ("actual_weight", "actualWeight", "BILL_WEIGHT"),
    "volume": ("volume", "VOLUME"),
    "remarks": ("remarks", "remark", "REMARK"),
    "destination_station": ("destination_station", "destinationStation", "DESTINATION"),
    "recipient_name": ("recipient_name", "recipientName", "ACCEPT_MAN"),
    "recipient_phone": ("recipient_phone", "recipientPhone", "ACCEPT_MAN_PHONE"),
    "recipient_address": ("recipient_address", "recipientAddress", "ACCEPT_MAN_ADDRESS"),
    "settlement_weight": ("settlement_weight", "settlementWeight", "SETTLEMENT_WEIGHT"),
    "volumetric_weight": ("volumetric_weight", "volumetricWeight", "VOLUME_WEIGHT"),
    "shipping_fee": ("shipping_fee", "shippingFee", "FREIGHT"),
    "payment_type": ("payment_type", "paymentType", "PAYMENT_TYPE"),
    "pay_on_arrival": ("pay_on_arrival", "payOnArrival", "TOPAYMENT"),
}


def _env_first(names: tuple[str, ...], default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return str(value)
    return default


def _env_int_first(names: tuple[str, ...], default: int) -> int:
    raw = _env_first(names, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _wsl_gateway_ip() -> str:
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    hex_ip = parts[2]
                    return ".".join(str(int(hex_ip[i : i + 2], 16)) for i in range(6, -1, -2))
    except (OSError, ValueError, IndexError):
        pass
    return "127.0.0.1"


def _running_in_wsl() -> bool:
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.release().lower()


def _resolve_mysql_host() -> str:
    host = _env_first(("AGENT_DB_HOST", "DOCFLOW_MYSQL_HOST"), "127.0.0.1")
    if host != "wsl-gateway":
        return host
    if _running_in_wsl():
        return _wsl_gateway_ip()
    return "127.0.0.1"


def _connect():
    return pymysql.connect(
        host=_resolve_mysql_host(),
        port=_env_int_first(("AGENT_DB_PORT", "DOCFLOW_MYSQL_PORT"), 3306),
        user=_env_first(("AGENT_DB_USER", "DOCFLOW_MYSQL_USER"), "agent"),
        password=_env_first(("AGENT_DB_PASS", "DOCFLOW_MYSQL_PASSWORD"), ""),
        database=_env_first(("AGENT_DB_NAME", "DOCFLOW_MYSQL_DATABASE"), "agent_db"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_masked_text(value: Any) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    return "*" in text or "＊" in text or bool(_MASK_ONLY_RE.fullmatch(text))


def _now_mysql() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_receipt_like_tracking(value: Any) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    return text.upper().startswith("H")


def is_child_like_tracking(value: Any, known_tracking_numbers: set[str] | None = None) -> bool:
    text = _clean_text(value)
    if not text or len(text) <= 4:
        return False
    if is_receipt_like_tracking(text):
        return False
    if _R_CHILD_TRACKING_RE.fullmatch(text):
        return True
    if _RONGHUI_NUMERIC_CHILD_TRACKING_RE.fullmatch(text):
        return True
    if known_tracking_numbers is None:
        return False
    return text[:-4] in known_tracking_numbers


def main_tracking_from_scan_code(value: Any, known_tracking_numbers: set[str] | None = None) -> str | None:
    text = _clean_text(value)
    if not text or is_receipt_like_tracking(text):
        return None
    if is_child_like_tracking(text, known_tracking_numbers):
        return text[:-4]
    return text


def should_include_waybill_tracking(
    value: Any,
    *,
    include_receipt_like: bool = False,
    include_child_like: bool = False,
) -> bool:
    if not include_receipt_like and is_receipt_like_tracking(value):
        return False
    if not include_child_like and is_child_like_tracking(value):
        return False
    return True


def has_waybill_detail(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict):
        return False
    detail_fields = (
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
    for field in detail_fields:
        value = record.get(field)
        if value not in (None, "", 0):
            return True
    return False


def _to_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _count_from_text(value: Any) -> int | None:
    parsed = _to_int(value)
    if parsed is not None:
        return parsed
    text = _clean_text(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group(0))


def _to_decimal(value: Any, quant: Decimal = _DECIMAL_2) -> Decimal | None:
    if value in (None, "", "null"):
        return None
    try:
        return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    except Exception:
        return None


def _decimal_to_output(value: Decimal | None) -> str:
    if value is None:
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    normalized = value.normalize()
    return format(normalized, "f")


def _decimal_to_number_cell(value: Any) -> int | float | str:
    if value in (None, "", "null"):
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return float(normalized)


def _int_cell(value: Any) -> int | str:
    parsed = _to_int(value)
    return parsed if parsed is not None else ""


def ensure_phase7_tables() -> None:
    """Validate Phase 7 tables and views installed by deployment migrations."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                """
            )
            tables = {str(row.get("TABLE_NAME") or "") for row in cur.fetchall() or []}
            required_tables = {"waybill_data", "split_pending_problem_items", "scan_codes"}
            missing_tables = sorted(required_tables - tables)
            if missing_tables:
                raise RuntimeError(
                    "Phase 7 schema is not migrated; run deployment migrations first: "
                    + ", ".join(missing_tables)
                )
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN ('split_pending_problem_items', 'scan_codes')
                """
            )
            columns = {
                (str(row.get("TABLE_NAME") or ""), str(row.get("COLUMN_NAME") or ""))
                for row in cur.fetchall() or []
            }
            required_columns = {
                ("split_pending_problem_items", "complaint_status"),
                ("split_pending_problem_items", "complaint_error_summary"),
                ("split_pending_problem_items", "complaint_processed_at"),
                ("scan_codes", "main_tracking"),
                ("scan_codes", "snapshot_date"),
            }
            missing_columns = sorted(
                f"{table}.{column}" for table, column in required_columns - columns
            )
            if missing_columns:
                raise RuntimeError(
                    "Phase 7 schema is not migrated; run deployment migrations first: "
                    + ", ".join(missing_columns)
                )
    finally:
        conn.close()


def ensure_console_waybill_table() -> None:
    """Validate the deployment-managed ``waybills`` schema.

    Runtime sync jobs must never create or mutate table definitions.  The
    versioned SQL migrations are responsible for installing this schema before
    either service starts.
    """
    WaybillRepository(_connect).ensure_schema()


def normalize_console_waybill_status(value: Any) -> str:
    status = _clean_text(value).lower()
    if status in CONSOLE_WAYBILL_STATUS_VALUES:
        return status
    legacy_map = {
        "待发货": "pending",
        "运输中": "in_transit",
        "运输途中": "in_transit",
        "未签收": "in_transit",
        "签收": "signed",
        "已签收": "signed",
        "已取消": "cancelled",
        "已作废": "cancelled",
        "作废": "cancelled",
        "取消": "cancelled",
    }
    return legacy_map.get(_clean_text(value), "in_transit")


def normalize_console_waybill_record(row: dict[str, Any]) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    payload = {field: _clean_text(row.get(field)) for field in CONSOLE_WAYBILL_FIELDS}
    if not payload["waybill_no"]:
        return None
    payload["status"] = normalize_console_waybill_status(payload.get("status"))
    return payload


def sync_console_waybills(
    records: list[dict[str, Any]],
    *,
    source: str,
    target_date: date | str | None = None,
    replace_date: bool = False,
) -> dict[str, Any]:
    """Upsert synced waybills into the console `/waybills` backing table."""
    source_text = _clean_text(source)[:32] or "sync"
    date_text = target_date.isoformat() if isinstance(target_date, date) else _clean_text(target_date)
    normalized_by_waybill: dict[str, dict[str, str]] = {}
    for record in records:
        normalized = normalize_console_waybill_record(record)
        if not normalized:
            continue
        normalized_by_waybill.setdefault(normalized["waybill_no"], normalized)

    ensure_console_waybill_table()
    return WaybillRepository(_connect).sync_records(
        list(normalized_by_waybill.values()),
        source=source_text,
        target_date=date_text,
        replace_date=replace_date,
        validate_schema=False,
    )


def list_console_waybills_by_source_date(
    *,
    source: str,
    target_date: date | str,
) -> list[dict[str, Any]]:
    """Return the exact persisted projection used for fresh write verification."""

    source_text = _clean_text(source)[:32]
    date_text = target_date.isoformat() if isinstance(target_date, date) else _clean_text(target_date)
    if not source_text or not date_text:
        raise ValueError("source and target_date are required")
    ensure_console_waybill_table()
    return WaybillRepository(_connect).list_by_source_date(
        source=source_text,
        target_date=date_text,
    )


def get_console_waybill_by_number(waybill_no: str) -> dict[str, Any] | None:
    """Read one current console waybill without changing projection state."""

    identity = _clean_text(waybill_no)
    if not identity:
        raise ValueError("waybill_no is required")
    ensure_console_waybill_table()
    return WaybillRepository(_connect).get_by_number(identity)


def list_console_waybills_by_numbers(
    waybill_numbers: list[str],
) -> list[dict[str, Any]]:
    """Read one bounded, binary-exact waybill identity set without mutation."""

    if not isinstance(waybill_numbers, list):
        raise ValueError("waybill_numbers must be a list")
    identities = [_clean_text(value) for value in waybill_numbers]
    if (
        not identities
        or len(identities) > 20_000
        or any(not identity for identity in identities)
        or len(identities) != len(set(identities))
    ):
        raise ValueError("waybill_numbers must be unique, non-empty, and bounded")
    ensure_console_waybill_table()
    return WaybillRepository(_connect).list_by_numbers(identities)


def update_console_waybill_statuses(
    waybill_numbers: list[str],
    status: str,
    *,
    mark_write_started: Callable[[], None] | None = None,
) -> dict[str, Any]:
    normalized_status = normalize_console_waybill_status(status)
    if normalized_status not in CONSOLE_WAYBILL_STATUS_VALUES:
        return {"ok": False, "error": "invalid status", "updated": 0}
    clean_numbers = []
    seen: set[str] = set()
    for value in waybill_numbers:
        number = _clean_text(value)
        if number and number not in seen:
            clean_numbers.append(number)
            seen.add(number)
    if not clean_numbers:
        return {"ok": True, "updated": 0, "status": normalized_status}

    return WaybillRepository(_connect).update_statuses(
        clean_numbers,
        normalized_status,
        mark_write_started=mark_write_started,
    )


def delete_receipt_like_console_waybills(*, source: str = "ronghui") -> dict[str, Any]:
    """Delete H/HR receipt-like rows from console waybill search table."""
    source_text = _clean_text(source)[:32] or "ronghui"
    ensure_console_waybill_table()
    return WaybillRepository(_connect).delete_receipt_like(source=source_text, validate_schema=False)


def normalize_waybill_record(row: list[Any] | tuple[Any, ...] | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(row, (list, tuple)):
        values = list(row)
        payload = {
            field: values[index] if index < len(values) else None
            for index, field in enumerate(WAYBILL_FIELDS)
        }
    elif isinstance(row, dict):
        payload: dict[str, Any] = {}
        for field, aliases in _DICT_WAYBILL_FIELD_ALIASES.items():
            payload[field] = None
            for alias in aliases:
                if alias in row:
                    payload[field] = row.get(alias)
                    break
    else:
        return None

    tracking_number = _clean_text(payload.get("tracking_number"))
    if not tracking_number:
        return None

    return {
        "tracking_number": tracking_number,
        "goods_name": _clean_text(payload.get("goods_name")),
        "package_type": _clean_text(payload.get("package_type")),
        "delivery_method": _clean_text(payload.get("delivery_method")),
        "quantity": _to_int(payload.get("quantity")),
        "receipt_number": _clean_text(payload.get("receipt_number")),
        "actual_weight": _to_decimal(payload.get("actual_weight")),
        "volume": _to_decimal(payload.get("volume"), _DECIMAL_3),
        "remarks": _clean_text(payload.get("remarks")),
        "destination_station": _clean_text(payload.get("destination_station")),
        "recipient_name": _clean_text(payload.get("recipient_name")),
        "recipient_phone": _clean_text(payload.get("recipient_phone")),
        "recipient_address": _clean_text(payload.get("recipient_address")),
        "settlement_weight": _to_decimal(payload.get("settlement_weight")),
        "volumetric_weight": _to_decimal(payload.get("volumetric_weight")),
        "shipping_fee": _to_decimal(payload.get("shipping_fee")),
        "payment_type": _clean_text(payload.get("payment_type")),
        "pay_on_arrival": _to_decimal(payload.get("pay_on_arrival")),
    }


def normalize_scan_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    codes = [
        str(row.get("扫描单号", "")).strip()
        for row in rows
        if str(row.get("扫描单号", "")).strip()
    ]
    known_tracking_numbers = set(codes)
    normalized_map: dict[str, dict[str, str]] = {}
    for row in rows:
        raw_code = str(row.get("扫描单号", "")).strip()
        if not raw_code:
            continue
        destination = _clean_text(row.get("目的地") or row.get("destination"))
        main_tracking = main_tracking_from_scan_code(raw_code, known_tracking_numbers)
        if not main_tracking:
            continue
        normalized_map[raw_code] = {
            "raw_code": raw_code,
            "destination": destination,
            "code_type": "child" if raw_code != main_tracking else "main",
            "main_tracking": main_tracking,
        }
    return list(normalized_map.values())


def child_items_from_scan_rows(rows: list[dict[str, str]], limit: int | None = None) -> list[dict[str, str]]:
    items = [
        {"bill_code": str(row["raw_code"]), "station_name": str(row["destination"])}
        for row in rows
        if row.get("code_type") == "child" and row.get("destination")
    ]
    items.sort(key=lambda item: (item["station_name"], item["bill_code"]))
    if limit is not None and limit >= 0:
        return items[:limit]
    return items


def missing_main_trackings_from_scan_rows(rows: list[dict[str, str]]) -> list[str]:
    return main_trackings_from_scan_rows(rows)


def main_trackings_from_scan_rows(scan_rows: list[dict[str, str]]) -> list[str]:
    known_tracking_numbers = {
        str(row.get("raw_code") or "").strip()
        for row in scan_rows
        if str(row.get("raw_code") or "").strip()
    }
    main_codes = {
        main_tracking
        for row in scan_rows
        if (main_tracking := main_tracking_from_scan_code(row.get("raw_code"), known_tracking_numbers))
    }
    return sorted(main_codes)


def _scan_row_tuple(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        row["raw_code"],
        row.get("destination", ""),
        row["code_type"],
        row.get("main_tracking") or "",
    )


_SCAN_CODES_UPSERT_SQL = """
    INSERT INTO scan_codes (
        raw_code, destination, code_type, main_tracking,
        snapshot_date, last_seen_at, seen_count
    ) VALUES (%s, %s, %s, %s, CURRENT_DATE, CURRENT_TIMESTAMP, 1)
    ON DUPLICATE KEY UPDATE
        destination = VALUES(destination),
        code_type = VALUES(code_type),
        main_tracking = VALUES(main_tracking),
        last_seen_at = CURRENT_TIMESTAMP,
        seen_count = seen_count + 1
"""

_SCAN_CODES_SNAPSHOT_UPSERT_SQL = """
    INSERT INTO scan_codes (
        raw_code, destination, code_type, main_tracking,
        snapshot_date, last_seen_at, seen_count
    ) VALUES (%s, %s, %s, %s, %s, %s, 1)
    ON DUPLICATE KEY UPDATE
        destination = VALUES(destination),
        code_type = VALUES(code_type),
        main_tracking = VALUES(main_tracking),
        last_seen_at = VALUES(last_seen_at),
        seen_count = seen_count + 1
"""


def _upsert_scan_rows(cur: Any, rows: list[dict[str, str]]) -> None:
    if rows:
        cur.executemany(
            _SCAN_CODES_UPSERT_SQL,
            [_scan_row_tuple(row) for row in rows],
        )


def replace_scan_codes(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Upsert scan rows so historical scans accumulate across runs.

    Name kept for backward-compatibility with `scan_sync_tool`. Behavior is
    UPSERT, not TRUNCATE+INSERT — see `arrival_stats_sync_tool` for rationale.
    """
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            _upsert_scan_rows(cur, rows)
        return {"ok": True, "upserted": len(rows), "replaced": len(rows)}
    finally:
        conn.close()


def replace_scan_codes_snapshot(
    rows: list[dict[str, str]],
    target_date: str,
) -> dict[str, Any]:
    """Atomically replace only the target day's signed scan snapshot."""

    ensure_phase7_tables()
    start_date = date.fromisoformat(target_date)
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM scan_codes
                WHERE snapshot_date = %s
                """,
                (start_date.isoformat(),),
            )
            deleted = int(cur.rowcount or 0)
            if rows:
                cur.executemany(
                    _SCAN_CODES_SNAPSHOT_UPSERT_SQL,
                    [
                        (
                            *_scan_row_tuple(row),
                            start_date.isoformat(),
                            start_date.isoformat(),
                        )
                        for row in rows
                    ],
                )
        conn.commit()
        return {
            "ok": True,
            "deleted": deleted,
            "replaced": len(rows),
            "target_date": start_date.isoformat(),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_scan_codes(rows: list[dict[str, str]]) -> dict[str, Any]:
    return replace_scan_codes(rows)


def cleanup_scan_codes(retention_days: int = 30) -> dict[str, Any]:
    if retention_days is None or int(retention_days) <= 0:
        return {"ok": True, "deleted": 0, "skipped": True}
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM scan_codes WHERE snapshot_date < CURDATE() - INTERVAL %s DAY",
                (int(retention_days),),
            )
            deleted = int(cur.rowcount or 0)
        return {"ok": True, "deleted": deleted, "retention_days": int(retention_days)}
    finally:
        conn.close()


def _list_scan_codes_query(
    *,
    where_sql: str = "",
    params: tuple[Any, ...] = (),
) -> list[dict[str, str]]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT scan_codes.raw_code, scan_codes.destination,
                       scan_codes.code_type, scan_codes.main_tracking
                FROM scan_codes
                {where_sql}
                ORDER BY scan_codes.destination, scan_codes.raw_code
                """,
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "raw_code": _clean_text(row.get("raw_code")),
            "destination": _clean_text(row.get("destination")),
            "code_type": _clean_text(row.get("code_type")),
            "main_tracking": _clean_text(row.get("main_tracking")),
        }
        for row in rows
        if row.get("raw_code")
    ]


def list_scan_codes() -> list[dict[str, str]]:
    return _list_scan_codes_query(
        where_sql="""
        INNER JOIN (
            SELECT raw_code, MAX(snapshot_date) AS snapshot_date
            FROM scan_codes
            GROUP BY raw_code
        ) latest
            ON latest.raw_code = scan_codes.raw_code
           AND latest.snapshot_date = scan_codes.snapshot_date
        """,
    )


def list_scan_codes_for_date(target_date: str) -> list[dict[str, str]]:
    start_date = date.fromisoformat(target_date)
    return _list_scan_codes_query(
        where_sql="WHERE snapshot_date = %s",
        params=(start_date.isoformat(),),
    )


def _waybill_row_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in WAYBILL_FIELDS)


def replace_waybill_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE waybill_data")
            if records:
                cur.executemany(
                    """
                    INSERT INTO waybill_data (
                        tracking_number, goods_name, package_type, delivery_method, quantity,
                        receipt_number, actual_weight, volume, remarks, destination_station,
                        recipient_name, recipient_phone, recipient_address, settlement_weight,
                        volumetric_weight, shipping_fee, payment_type, pay_on_arrival
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [_waybill_row_tuple(record) for record in records],
                )
        return {"ok": True, "replaced": len(records)}
    finally:
        conn.close()


def upsert_waybill_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if records:
                cur.executemany(
                    """
                    INSERT INTO waybill_data (
                        tracking_number, goods_name, package_type, delivery_method, quantity,
                        receipt_number, actual_weight, volume, remarks, destination_station,
                        recipient_name, recipient_phone, recipient_address, settlement_weight,
                        volumetric_weight, shipping_fee, payment_type, pay_on_arrival
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        goods_name = VALUES(goods_name),
                        package_type = VALUES(package_type),
                        delivery_method = VALUES(delivery_method),
                        quantity = VALUES(quantity),
                        receipt_number = VALUES(receipt_number),
                        actual_weight = VALUES(actual_weight),
                        volume = VALUES(volume),
                        remarks = VALUES(remarks),
                        destination_station = VALUES(destination_station),
                        recipient_name = VALUES(recipient_name),
                        recipient_phone = VALUES(recipient_phone),
                        recipient_address = VALUES(recipient_address),
                        settlement_weight = VALUES(settlement_weight),
                        volumetric_weight = VALUES(volumetric_weight),
                        shipping_fee = VALUES(shipping_fee),
                        payment_type = VALUES(payment_type),
                        pay_on_arrival = VALUES(pay_on_arrival)
                    """,
                    [_waybill_row_tuple(record) for record in records],
                )
        return {"ok": True, "upserted": len(records)}
    finally:
        conn.close()


def list_waybill_records(
    include_receipt_like: bool = False,
    include_child_like: bool | None = None,
) -> list[dict[str, Any]]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    tracking_number, goods_name, package_type, delivery_method, quantity,
                    receipt_number, actual_weight, volume, remarks, destination_station,
                    recipient_name, recipient_phone, recipient_address, settlement_weight,
                    volumetric_weight, shipping_fee, payment_type, pay_on_arrival
                FROM waybill_data
                ORDER BY destination_station, tracking_number
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if include_child_like is None:
        include_child_like = include_receipt_like
    if include_receipt_like and include_child_like:
        return rows
    return [
        row
        for row in rows
        if should_include_waybill_tracking(
            row.get("tracking_number"),
            include_receipt_like=include_receipt_like,
            include_child_like=include_child_like,
        )
    ]


def sort_waybill_records_by_destination(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda item: (
            _clean_text(item.get("destination_station")),
            _clean_text(item.get("tracking_number")),
        ),
    )


def list_waybill_tracking_numbers() -> list[str]:
    return [str(row["tracking_number"]) for row in list_waybill_records(include_receipt_like=True)]


def get_waybill_tracking_cache(tracking_number: Any) -> dict[str, Any]:
    code = _clean_text(tracking_number)
    if not code:
        return {}
    ensure_phase7_tables()
    ensure_console_waybill_table()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    wd.tracking_number,
                    wd.goods_name,
                    wd.quantity,
                    wd.recipient_name,
                    wd.recipient_phone,
                    wd.recipient_address,
                    wd.destination_station,
                    vap.expected_quantity,
                    vap.arrived_quantity,
                    vap.pending_quantity,
                    vap.first_arrival_at,
                    vap.last_arrival_at,
                    vap.arrival_status
                FROM waybill_data wd
                LEFT JOIN v_arrival_progress vap
                    ON vap.tracking_number = wd.tracking_number
                WHERE wd.tracking_number = %s
                LIMIT 1
                """,
                (code,),
            )
            row = cur.fetchone() or {}
            cache = dict(row)

            cur.execute(
                """
                SELECT
                    COUNT(DISTINCT raw_code) AS arrived_quantity,
                    MIN(last_seen_at) AS first_arrival_at,
                    MAX(last_seen_at) AS last_arrival_at
                FROM scan_codes
                WHERE code_type = 'child'
                  AND main_tracking = %s
                """,
                (code,),
            )
            scan_row = cur.fetchone() or {}
            scan_count = _to_int(scan_row.get("arrived_quantity"))
            if scan_count and scan_count > 0:
                cache.setdefault("tracking_number", code)
                existing_count = _to_int(cache.get("arrived_quantity"))
                if existing_count is None or scan_count > existing_count:
                    cache["arrived_quantity"] = scan_count
                if not cache.get("first_arrival_at") and scan_row.get("first_arrival_at"):
                    cache["first_arrival_at"] = scan_row.get("first_arrival_at")
                if not cache.get("last_arrival_at") and scan_row.get("last_arrival_at"):
                    cache["last_arrival_at"] = scan_row.get("last_arrival_at")

            cur.execute(
                """
                SELECT
                    waybill_no,
                    goods_name_lines,
                    quantity_lines,
                    receiver_name,
                    receiver_phone,
                    receiver_address,
                    destination_site
                FROM waybills
                WHERE waybill_no = %s
                  AND status <> 'cancelled'
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (code,),
            )
            console_row = cur.fetchone() or {}
            if console_row:
                cache.setdefault("tracking_number", code)
                goods_name = _clean_text(console_row.get("goods_name_lines"))
                if goods_name and cache.get("goods_name") in (None, ""):
                    cache["goods_name"] = goods_name
                quantity_text = _clean_text(console_row.get("quantity_lines"))
                if quantity_text and cache.get("quantity") in (None, ""):
                    cache["quantity"] = quantity_text
                if cache.get("expected_quantity") in (None, ""):
                    expected_quantity = _count_from_text(quantity_text)
                    if expected_quantity is not None:
                        cache["expected_quantity"] = expected_quantity
                receiver_name = _clean_text(console_row.get("receiver_name"))
                if receiver_name and (
                    cache.get("recipient_name") in (None, "") or _is_masked_text(cache.get("recipient_name"))
                ):
                    cache["recipient_name"] = receiver_name
                receiver_phone = _clean_text(console_row.get("receiver_phone"))
                if receiver_phone and (
                    cache.get("recipient_phone") in (None, "") or _is_masked_text(cache.get("recipient_phone"))
                ):
                    cache["recipient_phone"] = receiver_phone
                receiver_address = _clean_text(console_row.get("receiver_address"))
                if receiver_address and cache.get("recipient_address") in (None, ""):
                    cache["recipient_address"] = receiver_address
                destination_site = _clean_text(console_row.get("destination_site"))
                if destination_site and cache.get("destination_station") in (None, ""):
                    cache["destination_station"] = destination_site

            expected = _count_from_text(cache.get("expected_quantity") or cache.get("quantity"))
            arrived = _count_from_text(cache.get("arrived_quantity"))
            if expected is not None and arrived is not None:
                cache["pending_quantity"] = max(expected - arrived, 0)
                if "arrival_status" not in cache or cache.get("arrival_status") in (None, ""):
                    if arrived <= 0:
                        cache["arrival_status"] = "pending"
                    elif arrived >= expected:
                        cache["arrival_status"] = "completed"
                    else:
                        cache["arrival_status"] = "partial"
    finally:
        conn.close()
    meaningful = {key: value for key, value in cache.items() if key != "tracking_number" and value not in (None, "")}
    if not meaningful:
        return {}
    cache.setdefault("tracking_number", code)
    return cache


def list_missing_main_trackings(limit: int | None = None) -> list[str]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            sql = "SELECT main_tracking FROM v_missing_in_waybill ORDER BY main_tracking"
            if limit is not None and limit >= 0:
                sql += " LIMIT %s"
                cur.execute(sql, (int(limit),))
            else:
                cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        str(row["main_tracking"])
        for row in rows
        if row.get("main_tracking") and not is_receipt_like_tracking(row.get("main_tracking"))
    ]


def render_arrive_sheet_rows(records: list[dict[str, Any]]) -> list[list[Any]]:
    ordered_records = sort_waybill_records_by_destination(records)
    return [
        [
            record.get("tracking_number") or "",
            record.get("goods_name") or "",
            record.get("package_type") or "",
            record.get("delivery_method") or "",
            _int_cell(record.get("quantity")),
            record.get("receipt_number") or "",
            _decimal_to_number_cell(record.get("actual_weight")),
            _decimal_to_number_cell(record.get("volume")),
            record.get("remarks") or "",
            record.get("destination_station") or "",
            record.get("recipient_name") or "",
            record.get("recipient_phone") or "",
            record.get("recipient_address") or "",
            _decimal_to_number_cell(record.get("settlement_weight")),
            _decimal_to_number_cell(record.get("volumetric_weight")),
            _decimal_to_number_cell(record.get("shipping_fee")),
            record.get("payment_type") or "",
            _decimal_to_number_cell(record.get("pay_on_arrival")),
        ]
        for record in ordered_records
    ]


def render_stats_sheet_values(
    records: list[dict[str, Any]],
    count_map: dict[str, Any] | None = None,
    target_date: Any = None,
) -> list[list[Any]]:
    count_map = count_map or {}
    values: list[list[Any]] = [["运单编号", *WAYBILL_EXPORT_HEADERS]]
    for record in records:
        values.append(
            [
                record.get("tracking_number") or "",
                record.get("goods_name") or "",
                record.get("package_type") or "",
                record.get("delivery_method") or "",
                _int_cell(record.get("quantity")),
                record.get("receipt_number") or "",
                _decimal_to_number_cell(record.get("actual_weight")),
                _decimal_to_number_cell(record.get("volume")),
                record.get("remarks") or "",
                record.get("destination_station") or "",
                record.get("recipient_name") or "",
                record.get("recipient_phone") or "",
                record.get("recipient_address") or "",
                _decimal_to_number_cell(record.get("settlement_weight")),
                _decimal_to_number_cell(record.get("volumetric_weight")),
                _decimal_to_number_cell(record.get("shipping_fee")),
                record.get("payment_type") or "",
                _decimal_to_number_cell(record.get("pay_on_arrival")),
                _int_cell(count_map.get(record.get("tracking_number") or "")),
            ]
        )
    return values


def list_pending_waybills(include_receipt_like: bool = False) -> list[dict[str, Any]]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    tracking_number,
                    destination_station,
                    expected_quantity,
                    arrived_quantity,
                    pending_quantity,
                    first_arrival_at,
                    last_arrival_at,
                    arrival_status
                FROM v_arrival_progress
                WHERE arrival_status IN ('pending', 'partial')
                ORDER BY destination_station, tracking_number
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if include_receipt_like:
        return rows
    return [row for row in rows if not is_receipt_like_tracking(row.get("tracking_number"))]


def list_arrival_progress(statuses: list[str] | None = None) -> list[dict[str, Any]]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if statuses:
                placeholders = ",".join(["%s"] * len(statuses))
                cur.execute(
                    f"""
                    SELECT tracking_number, destination_station, expected_quantity,
                           arrived_quantity, pending_quantity, first_arrival_at,
                           last_arrival_at, arrival_status
                    FROM v_arrival_progress
                    WHERE arrival_status IN ({placeholders})
                    ORDER BY destination_station, tracking_number
                    """,
                    tuple(statuses),
                )
            else:
                cur.execute(
                    """
                    SELECT tracking_number, destination_station, expected_quantity,
                           arrived_quantity, pending_quantity, first_arrival_at,
                           last_arrival_at, arrival_status
                    FROM v_arrival_progress
                    ORDER BY destination_station, tracking_number
                    """
                )
            return cur.fetchall()
    finally:
        conn.close()


def _format_datetime_cell(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def render_pending_sheet_values(records: list[dict[str, Any]]) -> list[list[Any]]:
    values: list[list[Any]] = [list(PENDING_ARRIVAL_HEADERS)]
    for record in records:
        status = _clean_text(record.get("arrival_status")) or "unknown"
        values.append(
            [
                record.get("tracking_number") or "",
                record.get("destination_station") or "",
                record.get("expected_quantity") if record.get("expected_quantity") is not None else "",
                record.get("arrived_quantity") if record.get("arrived_quantity") is not None else "",
                record.get("pending_quantity") if record.get("pending_quantity") is not None else "",
                _PENDING_STATUS_LABELS.get(status, status),
                _format_datetime_cell(record.get("first_arrival_at")),
                _format_datetime_cell(record.get("last_arrival_at")),
            ]
        )
    return values


def replace_split_pending_problem_items(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Refresh the current snapshot while preserving completed steps for the same type."""

    ensure_phase7_tables()
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    refreshed_at = _now_mysql()
    for index, record in enumerate(records, start=1):
        tracking_number = _clean_text(record.get("tracking_number") or record.get("bill_code"))
        if not tracking_number:
            raise ValueError(f"第 {index} 条未齐问题件缺少运单号")
        if tracking_number in seen:
            raise ValueError(f"未齐问题件存在重复运单号: {tracking_number}")
        seen.add(tracking_number)
        expected = _to_int(record.get("expected_quantity"))
        arrived = _to_int(record.get("arrived_quantity"))
        pending = _to_int(record.get("pending_quantity"))
        if expected is None or arrived is None or pending is None:
            raise ValueError(f"{tracking_number} 的件数字段无效")
        if expected <= 0 or arrived < 0 or pending <= 0 or arrived >= expected or pending != expected - arrived:
            raise ValueError(f"{tracking_number} 的到货进度不符合未齐快照规则")
        problem_type = _clean_text(record.get("problem_type"))
        owner_type = _clean_text(record.get("problem_owner_type"))
        cause = _clean_text(record.get("problem_cause"))
        if not problem_type or not owner_type or not cause:
            raise ValueError(f"{tracking_number} 缺少问题件分类或原因")
        normalized.append(
            {
                "tracking_number": tracking_number,
                "source_row_no": int(record.get("source_row_no") or record.get("row_number") or 0),
                "destination_station": _clean_text(record.get("destination_station")),
                "expected_quantity": expected,
                "arrived_quantity": arrived,
                "pending_quantity": pending,
                "problem_type": problem_type,
                "problem_owner_type": owner_type,
                "problem_cause": cause,
            }
        )

    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            if normalized:
                cur.executemany(
                    """
                    INSERT INTO split_pending_problem_items (
                        tracking_number, source_row_no, destination_station,
                        expected_quantity, arrived_quantity, pending_quantity,
                        problem_type, problem_owner_type, problem_cause,
                        upload_status, error_summary, uploaded_at,
                        complaint_status, complaint_error_summary, complaint_processed_at,
                        refreshed_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'pending', NULL, NULL, %s, NULL, NULL, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        source_row_no = VALUES(source_row_no),
                        destination_station = VALUES(destination_station),
                        expected_quantity = VALUES(expected_quantity),
                        arrived_quantity = VALUES(arrived_quantity),
                        pending_quantity = VALUES(pending_quantity),
                        problem_owner_type = VALUES(problem_owner_type),
                        problem_cause = VALUES(problem_cause),
                        upload_status = CASE
                            WHEN problem_type = VALUES(problem_type) THEN upload_status
                            ELSE 'pending'
                        END,
                        error_summary = CASE
                            WHEN problem_type = VALUES(problem_type) THEN error_summary
                            ELSE NULL
                        END,
                        uploaded_at = CASE
                            WHEN problem_type = VALUES(problem_type) THEN uploaded_at
                            ELSE NULL
                        END,
                        complaint_status = 'not_applicable',
                        complaint_error_summary = NULL,
                        complaint_processed_at = NULL,
                        problem_type = VALUES(problem_type),
                        refreshed_at = VALUES(refreshed_at)
                    """,
                    [
                        (
                            item["tracking_number"],
                            item["source_row_no"],
                            item["destination_station"],
                            item["expected_quantity"],
                            item["arrived_quantity"],
                            item["pending_quantity"],
                            item["problem_type"],
                            item["problem_owner_type"],
                            item["problem_cause"],
                            "not_applicable",
                            refreshed_at,
                        )
                        for item in normalized
                    ],
                )
                placeholders = ",".join(["%s"] * len(normalized))
                cur.execute(
                    f"DELETE FROM split_pending_problem_items WHERE tracking_number NOT IN ({placeholders})",
                    tuple(item["tracking_number"] for item in normalized),
                )
            else:
                cur.execute("DELETE FROM split_pending_problem_items")
            deleted = int(cur.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "ok": True,
        "upserted": len(normalized),
        "deleted": deleted,
        "current": len(normalized),
        "refreshed_at": refreshed_at,
    }


def update_split_pending_combined_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_phase7_tables()
    conn = _connect()
    updated = 0
    try:
        conn.begin()
        with conn.cursor() as cur:
            for result in results:
                tracking_number = _clean_text(result.get("bill_code") or result.get("tracking_number"))
                if not tracking_number:
                    continue
                if result.get("complaint") is not None:
                    raise ValueError(f"{tracking_number} 不再支持 complaint 结果")
                problem_item = result.get("problem_item") if isinstance(result.get("problem_item"), dict) else None
                if problem_item is not None:
                    status = _clean_text(problem_item.get("status"))
                    if status not in {"success", "failed"}:
                        raise ValueError(f"{tracking_number} 的 problem_item.status 无效: {status or '空'}")
                    error_summary = (
                        ""
                        if status == "success"
                        else _clean_text(problem_item.get("error") or problem_item.get("message"))[:500]
                    )
                    cur.execute(
                        """
                        UPDATE split_pending_problem_items
                        SET upload_status = %s,
                            error_summary = %s,
                            uploaded_at = CASE WHEN %s = 'success' THEN CURRENT_TIMESTAMP ELSE NULL END,
                            complaint_status = 'not_applicable',
                            complaint_error_summary = NULL,
                            complaint_processed_at = NULL
                        WHERE tracking_number = %s
                        """,
                        (status, error_summary or None, status, tracking_number),
                    )
                    updated += int(cur.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "updated": updated}


def list_split_pending_problem_items() -> list[dict[str, Any]]:
    ensure_phase7_tables()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tracking_number, source_row_no, destination_station,
                       expected_quantity, arrived_quantity, pending_quantity,
                       problem_type, problem_owner_type, problem_cause,
                       upload_status, error_summary, uploaded_at,
                       complaint_status, complaint_error_summary,
                       complaint_processed_at, refreshed_at
                FROM split_pending_problem_items
                ORDER BY source_row_no, tracking_number
                """
            )
            return cur.fetchall()
    finally:
        conn.close()
