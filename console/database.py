import json
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterator

import pymysql

from config import Settings
from shared.redaction import redact_sensitive, redact_text
from shared.runtime_repositories import ScheduledTaskRepository, WorkflowResourceRepository
from shared.runtime_repositories import WaybillRepository

import re

_TASK_SLOT_ID_RE = re.compile(r"^(?P<base>.+?)__slot_(?P<slot>\d+)$")
_TASK_TIME_SUFFIX_RE = re.compile(r"^(?P<base>.+?)_(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)$")
_MONEY_CLEAN_RE = re.compile(r"[\s,￥¥元]")

WAYBILL_STATUS_LABELS = {
    "pending": "待发货",
    "in_transit": "运输中",
    "signed": "已签收",
    "cancelled": "已取消",
}
WAYBILL_STATUS_TONES = {
    "pending": "warning",
    "in_transit": "info",
    "signed": "success",
    "cancelled": "muted",
}
_COARSE_WAYBILL_STATUS_TEXT = {
    "pending",
    "in_transit",
    "signed",
    "cancelled",
    "待发货",
    "运输中",
    "运输途中",
    "未签收",
    "已签收",
    "已取消",
    "已作废",
}
_SCAN_STATUS_SHORT_LABELS = {
    "收件扫描": "收件",
    "发件扫描": "发件",
    "到件扫描": "到件",
    "到达扫描": "到达",
    "派件扫描": "派件",
    "签收扫描": "签收",
    "问题扫描": "问题",
}
WAYBILL_SOURCE_LABELS = {
    "manual": "手工",
    "ocr": "OCR",
    "ronghui": "融辉寄件",
    "yunda": "韵达寄件",
}
RECEIPT_PLATFORM_LABELS = {
    "yunda": "韵达",
    "ronghui": "融辉",
}
RECEIPT_DIRECTION_LABELS = {
    "send": "寄件",
    "receive": "派件",
}
_RECEIPT_COMPLETED_AUDIT_STATUSES = (
    "\u5ba1\u6838\u901a\u8fc7",
    "\u5ba1\u6838\u4e0d\u901a\u8fc7",
)
_RECEIPT_PENDING_AUDIT_STATUSES = (
    "\u5f85\u5ba1\u6838",
    "\u672a\u5ba1\u6838",
)
_RECEIPT_PENDING_AUDIT_LIKE_PAIRS = (
    ("%\u5f85%", "%\u5ba1\u6838%"),
    ("%\u672a%", "%\u5ba1\u6838%"),
)
RECEIPT_DETAIL_FIELD_CANDIDATES = {
    "recipient_name": (
        "收货人",
        "收件人",
        "收件人姓名",
        "RECEIVER_NAME",
        "RECEIVE_MAN",
        "CONSIGNEE",
        "CONSIGNEE_NAME",
        "ReceiverName",
        "receiverName",
        "receiveName",
    ),
    "recipient_address": (
        "收件地址",
        "收货地址",
        "收方地址",
        "RECEIVER_ADDRESS",
        "RECEIVER_ADDR",
        "CONSIGNEE_ADDRESS",
        "DESTINATION_ADDRESS",
        "ReceiverAddress",
        "receiverAddress",
        "receiveAddress",
    ),
    "goods_name": (
        "货物名称",
        "货物品名",
        "品名",
        "GOODS_NAME",
        "CARGO_NAME",
        "PRODUCT_NAME",
        "GoodsName",
        "goodsName",
        "CargoName",
    ),
    "package_type": (
        "包装类型",
        "包装",
        "PACKAGE_TYPE",
        "PACK_TYPE",
        "PACKING",
        "PackageType",
        "packageType",
    ),
    "piece_count": (
        "件数",
        "数量",
        "PIECE_COUNT",
        "PIECE",
        "QTY",
        "Goods_Num",
        "goodsNum",
        "Package_Num",
    ),
    "actual_weight": (
        "实际重量",
        "重量",
        "ACTUAL_WEIGHT",
        "REAL_WEIGHT",
        "WEIGHT",
        "ActualWeight",
        "actualWeight",
        "Weight",
    ),
    "volume": (
        "体积",
        "VOLUME",
        "VOL",
        "CUBE",
        "Volume",
        "volume",
    ),
    "waybill_no": (
        "运单号",
        "运单编号",
        "BILL_CODE",
        "BILLCODE",
        "BILL_NO",
        "Logistics_Id",
        "LogisticsId",
        "logisticsId",
        "waybill_no",
    ),
}


def normalize_waybill_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in WAYBILL_STATUS_LABELS:
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
    return legacy_map.get(str(value or "").strip(), "in_transit")


def normalize_waybill_scan_status(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    if not text or text in _COARSE_WAYBILL_STATUS_TEXT:
        return ""
    return text


def short_waybill_scan_status(value: Any) -> str:
    text = normalize_waybill_scan_status(value)
    if not text:
        return ""
    if text in _SCAN_STATUS_SHORT_LABELS:
        return _SCAN_STATUS_SHORT_LABELS[text]
    if text.endswith("扫描") and len(text) > 2:
        return text[:-2]
    return text


def _normalize_scheduled_task_group_id(task_id: str) -> str:
    raw = str(task_id or "").strip()
    match = _TASK_SLOT_ID_RE.fullmatch(raw)
    normalized = match.group("base") if match else raw
    time_match = _TASK_TIME_SUFFIX_RE.fullmatch(normalized)
    return time_match.group("base") if time_match else normalized


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _receipt_date_bound(value: Any, *, end_of_day: bool) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text} {'23:59:59' if end_of_day else '00:00:00'}"
    return text


def _waybill_date_bound(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    patterns = (
        r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})",
        r"(?P<year>\d{4})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, compact)
        if not match:
            continue
        try:
            parsed = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            return text
        return parsed.strftime("%Y-%m-%d")
    return text


def format_manual_waybill_no(sequence_value: int) -> str:
    return f"{int(sequence_value):08d}"


def _to_money_decimal(value: Any) -> tuple[Decimal, bool]:
    text = str(value or "").strip()
    if not text:
        return Decimal("0.00"), False
    cleaned = _MONEY_CLEAN_RE.sub("", text)
    if not cleaned:
        return Decimal("0.00"), False
    try:
        amount = Decimal(str(cleaned)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0.00"), True
    return amount, False


def _format_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _loads_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


LINE_HAUL_CONTACT_FIELDS = (
    "company_name",
    "service_area",
    "address",
    "contact_name",
    "phone_numbers",
    "remark",
    "source_text",
)


class DocumentRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.placeholder = "%s"
        self._mysql = pymysql
        self._scheduled_tasks = ScheduledTaskRepository(self.connect)
        self._workflow_resources = WorkflowResourceRepository(self.connect)
        self._waybills = WaybillRepository(self.connect)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        connection = self._mysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password,
            database=self.settings.mysql_database,
            connect_timeout=self.settings.mysql_connect_timeout_seconds,
            read_timeout=self.settings.mysql_connect_timeout_seconds,
            write_timeout=self.settings.mysql_connect_timeout_seconds,
            charset="utf8mb4",
            cursorclass=self._mysql.cursors.DictCursor,
            autocommit=False,
        )
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Validate the deployment-managed schema without mutating it at runtime."""
        required_tables = {
            "documents",
            "training_samples",
            "model_versions",
            "accuracy_log",
            "writers",
            "waybill_sequences",
            "waybill_provider_snapshots",
            "receipt_records",
            "receipt_attachments",
            "receipt_audit_logs",
            "admin_users",
            "admin_sessions",
            "line_haul_contacts",
        }
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT TABLE_NAME FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                """
            )
            actual_tables = {str(row.get("TABLE_NAME") or "") for row in cursor.fetchall() or []}
        missing = sorted(required_tables - actual_tables)
        if missing:
            raise RuntimeError(
                "Console schema is not migrated; run deployment migrations first: " + ", ".join(missing)
            )
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'admin_users'
                """
            )
            admin_user_columns = {str(row.get("COLUMN_NAME") or "") for row in cursor.fetchall() or []}
        if "ui_preferences_json" not in admin_user_columns:
            raise RuntimeError(
                "Console schema is not migrated; run deployment migrations first: "
                "admin_users.ui_preferences_json"
            )
        self._waybills.ensure_schema()

    def count_admin_users(self) -> int:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT COUNT(*) AS total FROM admin_users")
            row = cursor.fetchone() or {}
        return int(row.get("total") or 0)

    def list_admin_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, username, display_name, avatar_path, ui_preferences_json, is_active, last_login_at, created_at, updated_at
                FROM admin_users
                ORDER BY id ASC
                """
            )
            return list(cursor.fetchall() or [])

    def get_admin_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, username, display_name, avatar_path, ui_preferences_json, password_hash, is_active, last_login_at, created_at, updated_at
                FROM admin_users
                WHERE id = %s
                """,
                (int(user_id),),
            )
            return cursor.fetchone()

    def get_admin_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, username, display_name, avatar_path, ui_preferences_json, password_hash, is_active, last_login_at, created_at, updated_at
                FROM admin_users
                WHERE username = %s
                """,
                (str(username or "").strip(),),
            )
            return cursor.fetchone()

    def create_admin_user(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
        is_active: bool = True,
    ) -> int:
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO admin_users (
                    username, display_name, password_hash, is_active, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    str(username or "").strip(),
                    str(display_name or "").strip(),
                    str(password_hash or ""),
                    1 if is_active else 0,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def set_admin_user_active(self, user_id: int, is_active: bool) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE admin_users
                SET is_active = %s, updated_at = %s
                WHERE id = %s
                """,
                (1 if is_active else 0, _now_iso(), int(user_id)),
            )
            if not is_active:
                cursor.execute("DELETE FROM admin_sessions WHERE user_id = %s", (int(user_id),))

    def update_admin_user_password(self, user_id: int, password_hash: str) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE admin_users
                SET password_hash = %s, updated_at = %s
                WHERE id = %s
                """,
                (str(password_hash or ""), _now_iso(), int(user_id)),
            )
            cursor.execute("DELETE FROM admin_sessions WHERE user_id = %s", (int(user_id),))

    def update_admin_user_avatar(self, user_id: int, avatar_path: str) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE admin_users
                SET avatar_path = %s, updated_at = %s
                WHERE id = %s
                """,
                (str(avatar_path or "").strip(), _now_iso(), int(user_id)),
            )

    def update_admin_ui_preferences(self, user_id: int, ui_preferences_json: str) -> bool:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE admin_users
                SET ui_preferences_json = %s, updated_at = %s
                WHERE id = %s
                """,
                (str(ui_preferences_json or "{}"), _now_iso(), int(user_id)),
            )
            # The caller has already verified that this administrator exists.
            # MySQL reports 0 affected rows when an unchanged preference is
            # submitted in the same timestamp second, which is still a valid
            # idempotent save rather than a missing-user conflict.
            return cursor.rowcount >= 0

    def record_admin_login(self, user_id: int) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE admin_users
                SET last_login_at = %s, updated_at = %s
                WHERE id = %s
                """,
                (_now_iso(), _now_iso(), int(user_id)),
            )

    def create_admin_session(self, *, session_id: str, user_id: int, expires_at: datetime) -> None:
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO admin_sessions (
                    session_id, user_id, expires_at, created_at, last_seen_at
                ) VALUES (
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    str(session_id or ""),
                    int(user_id),
                    expires_at.isoformat(timespec="seconds"),
                    now,
                    now,
                ),
            )

    def get_admin_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT
                    s.session_id,
                    s.user_id,
                    s.expires_at,
                    s.created_at,
                    s.last_seen_at,
                    u.username,
                    u.display_name,
                    u.avatar_path,
                    u.ui_preferences_json,
                    u.is_active
                FROM admin_sessions s
                JOIN admin_users u ON u.id = s.user_id
                WHERE s.session_id = %s
                """,
                (str(session_id or ""),),
            )
            return cursor.fetchone()

    def touch_admin_session(self, session_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE admin_sessions
                SET last_seen_at = %s
                WHERE session_id = %s
                """,
                (_now_iso(), str(session_id or "")),
            )

    def delete_admin_session(self, session_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute("DELETE FROM admin_sessions WHERE session_id = %s", (str(session_id or ""),))

    def delete_expired_admin_sessions(self, now: datetime) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= %s",
                (now.isoformat(timespec="seconds"),),
            )

    def create_document(
        self,
        *,
        doc_token: str,
        original_name: str,
        source_relpath: str,
        template_name: str,
        status: str,
        original_path: str,
        processed_path: str,
        artifacts_dir: str,
        fields: dict[str, Any],
        raw_ocr: dict[str, Any],
        notes: str = "",
        error_message: str = "",
    ) -> int:
        now = _now_iso()
        sql = f"""
            INSERT INTO documents (
                doc_token, original_name, source_relpath, template_name, status,
                original_path, processed_path, artifacts_dir, fields_json,
                raw_ocr_json, notes, error_message, created_at, updated_at, reviewed_at
            ) VALUES (
                {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder},
                {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder},
                {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}
            )
        """
        params = [
            doc_token,
            original_name,
            source_relpath,
            template_name,
            status,
            original_path,
            processed_path,
            artifacts_dir,
            json.dumps(fields, ensure_ascii=False),
            json.dumps(raw_ocr, ensure_ascii=False),
            notes,
            error_message,
            now,
            now,
            None,
        ]
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            return int(cursor.lastrowid)

    def update_document(self, document_id: int, **updates: Any) -> None:
        if not updates:
            return
        serialized: dict[str, Any] = {}
        for key, value in updates.items():
            if key in {"fields", "raw_ocr"}:
                target_key = "fields_json" if key == "fields" else "raw_ocr_json"
                serialized[target_key] = json.dumps(value, ensure_ascii=False)
            else:
                serialized[key] = value
        serialized["updated_at"] = _now_iso()

        assignments = ", ".join(
            f"{column} = {self.placeholder}" for column in serialized
        )
        sql = f"UPDATE documents SET {assignments} WHERE id = {self.placeholder}"
        params = list(serialized.values()) + [document_id]
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params)

    def list_documents(self, limit: int = 200) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM documents ORDER BY created_at DESC LIMIT {self.placeholder}"
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [limit])
            rows = cursor.fetchall()
        return [self._row_to_document(row) for row in rows]

    def get_document(self, document_id: int) -> dict[str, Any] | None:
        sql = f"SELECT * FROM documents WHERE id = {self.placeholder}"
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [document_id])
            row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_document(row)

    def list_documents_by_status(self, statuses: list[str], limit: int = 500) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ", ".join(self.placeholder for _ in statuses)
        sql = (
            f"SELECT * FROM documents WHERE status IN ({placeholders}) "
            f"ORDER BY created_at ASC LIMIT {self.placeholder}"
        )
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [*statuses, limit])
            rows = cursor.fetchall()
        return [self._row_to_document(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS row_count FROM documents GROUP BY status"
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = int(row["row_count"])
        return counts

    def delete_document(self, document_id: int) -> bool:
        sql = f"DELETE FROM documents WHERE id = {self.placeholder}"
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [document_id])
            return int(cursor.rowcount or 0) > 0

    def rename_template_name(self, old_name: str, new_name: str) -> int:
        sql = (
            f"UPDATE documents SET template_name = {self.placeholder}, updated_at = {self.placeholder} "
            f"WHERE template_name = {self.placeholder}"
        )
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [new_name, now, old_name])
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # waybills 表操作
    # ------------------------------------------------------------------

    _WAYBILL_FIELD_NAMES = [
        "waybill_no", "destination_site", "open_date", "receiver_address",
        "receiver_name", "receiver_phone", "sender_name", "sender_phone",
        "goods_name_lines", "package_type_lines", "quantity_lines",
        "weight_volume", "delivery_method", "freight_fee", "pickup_fee",
        "delivery_fee", "transfer_fee", "payment_method", "insurance_amount",
        "cod_amount", "remark", "scan_status",
    ]

    def _field_value(self, fields: dict[str, Any], field_name: str) -> str:
        entry = fields.get(field_name, "")
        if isinstance(entry, dict):
            return str(entry.get("value", "") or "")
        return str(entry or "")

    def _insert_waybill(
        self,
        cursor: Any,
        *,
        fields: dict[str, Any],
        document_id: int | None,
        writer_id: str,
        source: str,
        now: str,
    ) -> int:
        columns = [
            "document_id", *self._WAYBILL_FIELD_NAMES,
            "writer_id", "source", "created_at", "updated_at",
        ]
        values = [document_id]
        for fname in self._WAYBILL_FIELD_NAMES:
            values.append(self._field_value(fields, fname))
        values.extend([writer_id, source, now, now])

        placeholders = ", ".join(self.placeholder for _ in columns)
        col_names = ", ".join(columns)
        sql = f"INSERT INTO waybills ({col_names}) VALUES ({placeholders})"
        cursor.execute(sql, values)
        return int(cursor.lastrowid)

    def _next_manual_waybill_no(self, cursor: Any, now: str) -> str:
        sequence_key = "manual_waybill"
        cursor.execute(
            (
                "INSERT IGNORE INTO waybill_sequences "
                f"(sequence_key, current_value, updated_at) VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder})"
            ),
            [sequence_key, 0, now],
        )
        cursor.execute(
            f"SELECT current_value FROM waybill_sequences WHERE sequence_key = {self.placeholder} FOR UPDATE",
            [sequence_key],
        )
        row = cursor.fetchone() or {"current_value": 0}
        next_value = int(row.get("current_value") or 0) + 1
        cursor.execute(
            (
                f"UPDATE waybill_sequences SET current_value = {self.placeholder}, updated_at = {self.placeholder} "
                f"WHERE sequence_key = {self.placeholder}"
            ),
            [next_value, now, sequence_key],
        )
        return format_manual_waybill_no(next_value)

    def peek_next_manual_waybill_no(self) -> str:
        """读取下一张手工单预览号；实际保存仍以事务内生成值为准。"""
        sequence_key = "manual_waybill"
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT current_value FROM waybill_sequences WHERE sequence_key = {self.placeholder}",
                [sequence_key],
            )
            row = cursor.fetchone() or {"current_value": 0}
        return format_manual_waybill_no(int(row.get("current_value") or 0) + 1)

    def create_waybill_from_fields(
        self, fields: dict[str, Any], document_id: int | None = None,
        writer_id: str = "", source: str = "ocr",
    ) -> int:
        """从 fields dict（OCR 确认后的结构）提取值，写入 waybills 表。"""
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            return self._insert_waybill(
                cursor,
                fields=fields,
                document_id=document_id,
                writer_id=writer_id,
                source=source,
                now=now,
            )

    def create_manual_waybill(self, fields: dict[str, Any], writer_id: str = "") -> tuple[int, str]:
        """写入手工录单 waybill，并在同一事务里生成全局 8 位流水号。"""
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            waybill_no = self._next_manual_waybill_no(cursor, now)
            fields_with_number = dict(fields)
            fields_with_number["waybill_no"] = waybill_no
            waybill_id = self._insert_waybill(
                cursor,
                fields=fields_with_number,
                document_id=None,
                writer_id=writer_id,
                source="manual",
                now=now,
            )
            return waybill_id, waybill_no

    def get_waybill(self, waybill_id: int) -> dict[str, Any] | None:
        sql = f"SELECT * FROM waybills WHERE id = {self.placeholder}"
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [waybill_id])
            row = cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        for field in ("created_at", "updated_at"):
            if data.get(field) and hasattr(data[field], "strftime"):
                data[field] = data[field].strftime("%Y-%m-%d %H:%M:%S")
        return data

    def get_waybill_by_no(self, waybill_no: str, *, source: str | None = None) -> dict[str, Any] | None:
        row = self._waybills.get_by_number(waybill_no, source=source)
        return self._row_to_waybill(row) if row else None

    def upsert_provider_waybill(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        writer_id: str = "",
    ) -> dict[str, Any] | None:
        waybill_no = str(payload.get("waybill_no", "") or "").strip()
        if not waybill_no:
            raise ValueError("waybill_no is required")
        self._waybills.sync_records([payload], source=source, writer_id=writer_id)
        return self.get_waybill_by_no(waybill_no, source=source)

    def create_waybill_provider_snapshot(
        self,
        *,
        provider: str,
        remote_waybill_no: str = "",
        snapshot_kind: str,
        payload: dict[str, Any] | list[Any] | str | None,
        waybill_id: int | None = None,
    ) -> int:
        sql = (
            "INSERT INTO waybill_provider_snapshots "
            "(waybill_id, provider, remote_waybill_no, snapshot_kind, payload_json, created_at) "
            f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder})"
        )
        serialized = payload if isinstance(payload, str) else json.dumps(payload or {}, ensure_ascii=False)
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                sql,
                [
                    waybill_id,
                    str(provider or "").strip(),
                    str(remote_waybill_no or "").strip(),
                    str(snapshot_kind or "").strip(),
                    serialized,
                    _now_iso(),
                ],
            )
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # 回单统一索引表操作
    # ------------------------------------------------------------------

    def upsert_receipt_record(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        platform = self._normalize_receipt_platform(payload.get("platform"))
        direction = self._normalize_receipt_direction(payload.get("direction"))
        waybill_no = str(payload.get("waybill_no", "") or "").strip()
        receipt_no = str(payload.get("receipt_no", "") or "").strip()
        if not platform or not direction:
            raise ValueError("platform and direction are required")
        if not waybill_no and not receipt_no:
            raise ValueError("waybill_no or receipt_no is required")

        raw_payload = payload.get("raw_payload", payload.get("raw_payload_json", {}))
        if isinstance(raw_payload, str):
            raw_payload_json = raw_payload
        else:
            raw_payload_json = json.dumps(raw_payload or {}, ensure_ascii=False)

        now = _now_iso()
        updated_at = str(payload.get("updated_at") or payload.get("synced_at") or now).strip() or now
        photo_count = self._safe_int(payload.get("photo_count"), default=0)
        photo_status = str(payload.get("photo_status") or ("已上传" if photo_count > 0 else "未上传")).strip()

        columns = [
            "platform",
            "direction",
            "waybill_no",
            "receipt_no",
            "return_waybill_no",
            "receipt_status",
            "audit_status",
            "photo_status",
            "photo_count",
            "signed_confirmed",
            "remote_updated_at",
            "raw_payload_json",
            "synced_at",
            "created_at",
            "updated_at",
        ]
        values = [
            platform,
            direction,
            waybill_no,
            receipt_no,
            str(payload.get("return_waybill_no", "") or "").strip(),
            str(payload.get("receipt_status", "") or "").strip(),
            str(payload.get("audit_status", "") or "").strip(),
            photo_status,
            photo_count,
            str(payload.get("signed_confirmed", "") or "").strip(),
            str(payload.get("remote_updated_at", "") or "").strip(),
            raw_payload_json,
            now,
            now,
            updated_at,
        ]
        placeholders = ", ".join(self.placeholder for _ in columns)
        update_params: list[Any] = []
        assignments_parts: list[str] = []
        for column in columns:
            if column in {"platform", "direction", "waybill_no", "receipt_no", "created_at"}:
                continue
            if column == "audit_status":
                completed_placeholders = ", ".join(self.placeholder for _ in _RECEIPT_COMPLETED_AUDIT_STATUSES)
                pending_placeholders = ", ".join(self.placeholder for _ in _RECEIPT_PENDING_AUDIT_STATUSES)
                pending_like_conditions: list[str] = []
                for _ in _RECEIPT_PENDING_AUDIT_LIKE_PAIRS:
                    pending_like_conditions.append(
                        f"(VALUES(audit_status) LIKE {self.placeholder} AND VALUES(audit_status) LIKE {self.placeholder})"
                    )
                pending_conditions = [
                    "VALUES(audit_status) = ''",
                    f"VALUES(audit_status) IN ({pending_placeholders})",
                    *pending_like_conditions,
                ]
                assignments_parts.append(
                    "audit_status = CASE "
                    f"WHEN audit_status IN ({completed_placeholders}) "
                    f"AND ({' OR '.join(pending_conditions)}) "
                    "THEN audit_status ELSE VALUES(audit_status) END"
                )
                update_params.extend(_RECEIPT_COMPLETED_AUDIT_STATUSES)
                update_params.extend(_RECEIPT_PENDING_AUDIT_STATUSES)
                for left, right in _RECEIPT_PENDING_AUDIT_LIKE_PAIRS:
                    update_params.extend([left, right])
                continue
            assignments_parts.append(f"{column} = VALUES({column})")
        assignments = ", ".join(assignments_parts)
        sql = (
            f"INSERT INTO receipt_records ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            "ON DUPLICATE KEY UPDATE "
            f"{assignments}, id = LAST_INSERT_ID(id)"
        )
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [*values, *update_params])
            receipt_id = int(getattr(cursor, "lastrowid", 0) or 0)
            if not receipt_id:
                cursor.execute(
                    (
                        "SELECT id FROM receipt_records "
                        f"WHERE platform = {self.placeholder} AND direction = {self.placeholder} "
                        f"AND waybill_no = {self.placeholder} AND receipt_no = {self.placeholder} "
                        "LIMIT 1"
                    ),
                    [platform, direction, waybill_no, receipt_no],
                )
                row = cursor.fetchone() or {}
                receipt_id = int(row.get("id") or 0)
        record = self.get_receipt_record(receipt_id) if receipt_id else None
        if (
            record
            and self._is_receipt_pending_audit_status(payload.get("audit_status"))
            and self._is_receipt_pending_audit_status(record.get("audit_status"))
        ):
            restored_status = self._latest_successful_receipt_audit_status(receipt_id)
            if restored_status:
                return self.update_receipt_audit_status(receipt_id, restored_status) or {
                    **record,
                    "audit_status": restored_status,
                }
        return record

    @staticmethod
    def _is_receipt_pending_audit_status(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        if text in _RECEIPT_PENDING_AUDIT_STATUSES:
            return True
        return ("\u5ba1\u6838" in text) and ("\u5f85" in text or "\u672a" in text)

    @staticmethod
    def _receipt_audit_status_from_result(result: Any) -> str:
        normalized = str(result or "").strip().lower()
        if normalized == "passed":
            return _RECEIPT_COMPLETED_AUDIT_STATUSES[0]
        if normalized == "failed":
            return _RECEIPT_COMPLETED_AUDIT_STATUSES[1]
        return ""

    def _latest_successful_receipt_audit_status(self, receipt_id: int) -> str:
        try:
            logs = self.list_receipt_audit_logs(receipt_id, limit=20)
        except Exception:
            return ""
        for log in logs:
            if str(log.get("result_status") or "").strip().lower() != "success":
                continue
            if str(log.get("action") or "").strip() not in {"audit", "audit_original_page"}:
                continue
            request_summary = log.get("request_summary") if isinstance(log.get("request_summary"), dict) else {}
            audit_status = self._receipt_audit_status_from_result(request_summary.get("result"))
            if audit_status:
                return audit_status
        return ""

    def upsert_receipt_attachment(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        record_id = self._safe_int(payload.get("record_id"), default=0)
        if record_id <= 0:
            raise ValueError("record_id is required")
        file_hash = str(payload.get("file_hash") or "").strip() or None
        source_url = str(payload.get("source_url", "") or "").strip()
        now = _now_iso()
        columns = [
            "record_id",
            "attachment_type",
            "display_name",
            "source_url",
            "local_path",
            "file_hash",
            "mime_type",
            "file_size",
            "uploaded_at",
            "created_at",
            "updated_at",
        ]
        values = [
            record_id,
            str(payload.get("attachment_type", "") or "").strip(),
            str(payload.get("display_name", "") or "").strip(),
            source_url,
            str(payload.get("local_path", "") or "").strip(),
            file_hash,
            str(payload.get("mime_type", "") or "").strip(),
            self._safe_int(payload.get("file_size"), default=0),
            str(payload.get("uploaded_at", "") or "").strip(),
            now,
            now,
        ]
        with self.connect() as connection:
            cursor = connection.cursor()
            def update_existing(attachment_id: int) -> dict[str, Any] | None:
                cursor.execute(
                    (
                        f"UPDATE receipt_attachments SET attachment_type = {self.placeholder}, "
                        f"display_name = {self.placeholder}, source_url = {self.placeholder}, "
                        f"local_path = CASE WHEN {self.placeholder} <> '' THEN {self.placeholder} ELSE local_path END, "
                        f"file_hash = CASE WHEN {self.placeholder} <> '' THEN {self.placeholder} ELSE file_hash END, "
                        f"mime_type = CASE WHEN {self.placeholder} <> '' THEN {self.placeholder} ELSE mime_type END, "
                        f"file_size = CASE WHEN {self.placeholder} > 0 THEN {self.placeholder} ELSE file_size END, "
                        f"uploaded_at = CASE WHEN {self.placeholder} <> '' THEN {self.placeholder} ELSE uploaded_at END, "
                        f"updated_at = {self.placeholder} WHERE id = {self.placeholder}"
                    ),
                    [
                        values[1],
                        values[2],
                        values[3],
                        values[4],
                        values[4],
                        file_hash or "",
                        file_hash or "",
                        values[6],
                        values[6],
                        values[7],
                        values[7],
                        values[8],
                        values[8],
                        now,
                        int(attachment_id),
                    ],
                )
                return self.get_receipt_attachment(int(attachment_id))

            if source_url:
                cursor.execute(
                    (
                        "SELECT id FROM receipt_attachments "
                        f"WHERE record_id = {self.placeholder} AND source_url = {self.placeholder} "
                        "ORDER BY id ASC LIMIT 1"
                    ),
                    [record_id, source_url],
                )
                existing_by_source = cursor.fetchone()
                if existing_by_source and existing_by_source.get("id"):
                    return update_existing(int(existing_by_source["id"]))

            if file_hash:
                cursor.execute(
                    (
                        "SELECT id FROM receipt_attachments "
                        f"WHERE record_id = {self.placeholder} AND file_hash = {self.placeholder} "
                        "LIMIT 1"
                    ),
                    [record_id, file_hash],
                )
                existing = cursor.fetchone()
                if existing and existing.get("id"):
                    return update_existing(int(existing["id"]))
            placeholders = ", ".join(self.placeholder for _ in columns)
            cursor.execute(
                f"INSERT INTO receipt_attachments ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            attachment_id = int(cursor.lastrowid)
        return self.get_receipt_attachment(attachment_id)

    def record_receipt_audit_log(
        self,
        *,
        receipt_id: int | None = None,
        platform: str = "",
        direction: str = "",
        action: str,
        result_status: str = "",
        operator: str = "",
        request_summary: dict[str, Any] | list[Any] | str | None = None,
        response_status: str = "",
        message: str = "",
    ) -> int:
        safe_request = self._sanitize_receipt_log_payload(request_summary)
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    "INSERT INTO receipt_audit_logs "
                    "(receipt_id, platform, direction, action, result_status, operator, "
                    "request_summary_json, response_status, message, created_at) "
                    f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, "
                    f"{self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, "
                    f"{self.placeholder}, {self.placeholder})"
                ),
                [
                    receipt_id,
                    self._normalize_receipt_platform(platform),
                    self._normalize_receipt_direction(direction),
                    str(action or "").strip(),
                    str(result_status or "").strip(),
                    str(operator or "").strip(),
                    safe_request,
                    str(response_status or "").strip(),
                    redact_text(message).strip(),
                    _now_iso(),
                ],
            )
            return int(cursor.lastrowid)

    def update_receipt_audit_status(self, receipt_id: int, audit_status: str) -> dict[str, Any] | None:
        safe_id = self._safe_int(receipt_id, default=0)
        status = str(audit_status or "").strip()
        if safe_id <= 0:
            raise ValueError("receipt_id is required")
        if not status:
            raise ValueError("audit_status is required")
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    "UPDATE receipt_records "
                    f"SET audit_status = {self.placeholder}, updated_at = {self.placeholder}, synced_at = {self.placeholder} "
                    f"WHERE id = {self.placeholder}"
                ),
                [status, now, now, safe_id],
            )
        return self.get_receipt_record(safe_id)

    def search_receipts(
        self,
        filters: dict[str, Any] | None = None,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        filters = filters or {}
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 50), 10), 100)
        where_sql, params = self._build_receipt_search_where(filters)
        count_sql = f"SELECT COUNT(*) AS row_count FROM receipt_records WHERE {where_sql}"
        rows_sql = (
            "SELECT r.*, "
            "(SELECT a.id FROM receipt_attachments a WHERE a.record_id = r.id "
            "ORDER BY a.id ASC LIMIT 1) AS thumbnail_attachment_id "
            f"FROM receipt_records r WHERE {where_sql} "
            "ORDER BY r.updated_at DESC, r.id DESC "
            f"LIMIT {self.placeholder} OFFSET {self.placeholder}"
        )
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(count_sql, params)
            total = int((cursor.fetchone() or {}).get("row_count") or 0)
            total_pages = max((total + page_size - 1) // page_size, 1)
            page = min(page, total_pages)
            offset = (page - 1) * page_size
            cursor.execute(rows_sql, [*params, page_size, offset])
            rows = [self._row_to_receipt_record(row) for row in cursor.fetchall()]
        return {
            "rows": rows,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "offset": (page - 1) * page_size,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
        }

    def _build_receipt_search_where(self, filters: dict[str, Any], *, table_alias: str = "") -> tuple[str, list[Any]]:
        column_prefix = f"{table_alias}." if table_alias else ""
        conditions = ["1=1", f"NOT ({column_prefix}platform = 'yunda' AND {column_prefix}direction = 'receive')"]
        params: list[Any] = []

        platform = self._normalize_receipt_platform(filters.get("platform"))
        if platform:
            conditions.append(f"{column_prefix}platform = {self.placeholder}")
            params.append(platform)

        direction = self._normalize_receipt_direction(filters.get("direction"))
        if direction:
            conditions.append(f"{column_prefix}direction = {self.placeholder}")
            params.append(direction)

        keyword = str(filters.get("q", "") or "").strip()
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                "("
                f"{column_prefix}waybill_no LIKE {self.placeholder} OR "
                f"{column_prefix}receipt_no LIKE {self.placeholder} OR "
                f"{column_prefix}return_waybill_no LIKE {self.placeholder}"
                ")"
            )
            params.extend([like, like, like])

        receipt_status = str(filters.get("receipt_status", "") or "").strip()
        if receipt_status and receipt_status.lower() != "all":
            conditions.append(f"{column_prefix}receipt_status = {self.placeholder}")
            params.append(receipt_status)

        audit_status = str(filters.get("audit_status", "") or "").strip()
        if audit_status and audit_status.lower() != "all":
            if audit_status == "待审核":
                conditions.append(
                    "("
                    f"{column_prefix}audit_status = {self.placeholder} OR "
                    f"({column_prefix}audit_status LIKE {self.placeholder} AND "
                    f"{column_prefix}audit_status LIKE {self.placeholder})"
                    ")"
                )
                params.extend([audit_status, "%待%", "%审核%"])
            else:
                conditions.append(f"{column_prefix}audit_status = {self.placeholder}")
                params.append(audit_status)

        photo_status = str(filters.get("photo_status", "") or "").strip().lower()
        if photo_status == "has_photo":
            conditions.append(f"{column_prefix}photo_count > 0")
        elif photo_status == "missing_photo":
            conditions.append(f"{column_prefix}photo_count = 0")

        date_from = str(filters.get("date_from", "") or "").strip()
        if date_from:
            conditions.append(f"{column_prefix}updated_at >= {self.placeholder}")
            params.append(_receipt_date_bound(date_from, end_of_day=False))

        date_to = str(filters.get("date_to", "") or "").strip()
        if date_to:
            conditions.append(f"{column_prefix}updated_at <= {self.placeholder}")
            params.append(_receipt_date_bound(date_to, end_of_day=True))

        return " AND ".join(conditions), params

    def get_receipt_record(self, receipt_id: int) -> dict[str, Any] | None:
        if int(receipt_id or 0) <= 0:
            return None
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    "SELECT r.*, "
                    "(SELECT a.id FROM receipt_attachments a WHERE a.record_id = r.id "
                    "ORDER BY a.id ASC LIMIT 1) AS thumbnail_attachment_id "
                    f"FROM receipt_records r WHERE r.id = {self.placeholder}"
                ),
                [int(receipt_id)],
            )
            row = cursor.fetchone()
        return self._row_to_receipt_record(row) if row else None

    def get_receipt_detail(self, receipt_id: int) -> dict[str, Any] | None:
        record = self.get_receipt_record(receipt_id)
        if not record:
            return None
        return {
            "record": record,
            "attachments": self.list_receipt_attachments(receipt_id),
            "audit_logs": self.list_receipt_audit_logs(receipt_id),
        }

    def list_receipt_attachments(self, receipt_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    "SELECT a.*, r.platform, r.direction, r.waybill_no "
                    "FROM receipt_attachments a "
                    "LEFT JOIN receipt_records r ON r.id = a.record_id "
                    f"WHERE a.record_id = {self.placeholder} "
                    "ORDER BY id ASC"
                ),
                [int(receipt_id)],
            )
            rows = cursor.fetchall()
        return [self._row_to_receipt_attachment(row) for row in self._dedupe_receipt_attachment_rows(rows)]

    def list_receipt_image_attachments_for_filters(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        where_sql, params = self._build_receipt_search_where(filters or {}, table_alias="r")
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    "SELECT a.*, r.platform, r.direction, r.waybill_no, r.receipt_no, "
                    "r.return_waybill_no, r.raw_payload_json "
                    "FROM receipt_attachments a "
                    "INNER JOIN receipt_records r ON r.id = a.record_id "
                    f"WHERE {where_sql} "
                    "ORDER BY r.updated_at DESC, r.id DESC, a.id ASC"
                ),
                params,
            )
            rows = cursor.fetchall()
        return [
            self._row_to_receipt_archive_attachment(row)
            for row in self._dedupe_receipt_attachment_rows(rows)
        ]

    def _receipt_attachment_row_score(self, row: dict[str, Any]) -> tuple[int, int, int]:
        has_local_path = 1 if str(row.get("local_path") or "").strip() else 0
        has_file_size = 1 if self._safe_int(row.get("file_size"), default=0) > 0 else 0
        attachment_id = self._safe_int(row.get("id"), default=0)
        return (has_local_path, has_file_size, attachment_id)

    def _dedupe_receipt_attachment_rows(self, rows: Any) -> list[dict[str, Any]]:
        output_order: list[tuple[str, str]] = []
        best_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows or []:
            data = dict(row or {})
            source_url = str(data.get("source_url") or "").strip()
            file_hash = str(data.get("file_hash") or "").strip()
            attachment_id = str(self._safe_int(data.get("id"), default=0))
            if source_url:
                key = ("source_url", source_url)
            elif file_hash:
                key = ("file_hash", file_hash)
            else:
                key = ("id", attachment_id)
            if key not in best_by_key:
                best_by_key[key] = data
                output_order.append(key)
                continue
            current = best_by_key[key]
            if self._receipt_attachment_row_score(data) > self._receipt_attachment_row_score(current):
                best_by_key[key] = data
        return [best_by_key[key] for key in output_order]

    def get_receipt_attachment(self, attachment_id: int) -> dict[str, Any] | None:
        if int(attachment_id or 0) <= 0:
            return None
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    "SELECT a.*, r.platform, r.direction, r.waybill_no "
                    "FROM receipt_attachments a "
                    "LEFT JOIN receipt_records r ON r.id = a.record_id "
                    f"WHERE a.id = {self.placeholder}"
                ),
                [int(attachment_id)],
            )
            row = cursor.fetchone()
        return self._row_to_receipt_attachment(row) if row else None

    def update_receipt_attachment_cache(
        self,
        attachment_id: int,
        *,
        local_path: str,
        file_hash: str,
        mime_type: str,
        file_size: int,
    ) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    f"UPDATE receipt_attachments SET local_path = {self.placeholder}, "
                    f"file_hash = CASE WHEN file_hash IS NULL OR file_hash = '' THEN {self.placeholder} ELSE file_hash END, "
                    f"mime_type = {self.placeholder}, "
                    f"file_size = {self.placeholder}, updated_at = {self.placeholder} "
                    f"WHERE id = {self.placeholder}"
                ),
                [
                    str(local_path or "").strip(),
                    str(file_hash or "").strip(),
                    str(mime_type or "").strip(),
                    int(file_size or 0),
                    _now_iso(),
                    int(attachment_id),
                ],
            )

    def list_receipt_audit_logs(self, receipt_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                (
                    "SELECT * FROM receipt_audit_logs "
                    f"WHERE receipt_id = {self.placeholder} "
                    "ORDER BY created_at DESC, id DESC "
                    f"LIMIT {self.placeholder}"
                ),
                [int(receipt_id), int(limit)],
            )
            rows = cursor.fetchall()
        return [self._row_to_receipt_audit_log(row) for row in rows]

    def search_waybills(
        self,
        filters: dict[str, Any] | None = None,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """查询已经开单的运单列表，并返回按当前筛选条件计算的汇总。"""
        filters = filters or {}
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 50), 10), 100)
        offset = (page - 1) * page_size
        where_sql, params = self._build_waybill_search_where(filters)

        count_sql = f"SELECT COUNT(*) AS row_count FROM waybills WHERE {where_sql}"
        rows_sql = (
            f"SELECT * FROM waybills WHERE {where_sql} "
            f"ORDER BY {self._waybill_order_clause(filters)} "
            f"LIMIT {self.placeholder} OFFSET {self.placeholder}"
        )
        summary_sql = (
            "SELECT source, created_at, open_date, freight_fee, transfer_fee, "
            f"payment_method, cod_amount FROM waybills WHERE {where_sql}"
        )

        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(count_sql, params)
            total_row = cursor.fetchone() or {"row_count": 0}
            total = int(total_row.get("row_count") or 0)
            total_pages = max((total + page_size - 1) // page_size, 1)
            page = min(page, total_pages)
            offset = (page - 1) * page_size

            cursor.execute(rows_sql, [*params, page_size, offset])
            rows = [self._row_to_waybill(row) for row in cursor.fetchall()]

            cursor.execute(summary_sql, params)
            summary_rows = cursor.fetchall()

        return {
            "rows": rows,
            "summary": self._build_waybill_search_summary(summary_rows, total),
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "offset": offset,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
        }

    def _build_waybill_search_where(self, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        conditions = ["waybill_no <> ''"]
        params: list[Any] = []

        keyword = str(filters.get("q", "") or "").strip()
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                "("
                "waybill_no LIKE {0} OR receiver_name LIKE {0} OR receiver_phone LIKE {0} "
                "OR sender_name LIKE {0} OR sender_phone LIKE {0}"
                ")".format(self.placeholder)
            )
            params.extend([like] * 5)

        date_from = str(filters.get("date_from", "") or "").strip()
        if date_from:
            conditions.append(f"open_date >= {self.placeholder}")
            params.append(_waybill_date_bound(date_from))

        date_to = str(filters.get("date_to", "") or "").strip()
        if date_to:
            conditions.append(f"open_date <= {self.placeholder}")
            params.append(_waybill_date_bound(date_to))

        status = normalize_waybill_status(filters.get("status", "")) if filters.get("status") else ""
        if status and str(filters.get("status", "")).strip().lower() != "all":
            conditions.append(f"status = {self.placeholder}")
            params.append(status)

        source = str(filters.get("source", "") or "").strip().lower()
        if source and source != "all":
            conditions.append(f"source = {self.placeholder}")
            params.append(source)

        payment_method = str(filters.get("payment_method", "") or "").strip()
        if payment_method:
            conditions.append(f"payment_method = {self.placeholder}")
            params.append(payment_method)

        delivery_method = str(filters.get("delivery_method", "") or "").strip()
        if delivery_method:
            conditions.append(f"delivery_method = {self.placeholder}")
            params.append(delivery_method)

        return " AND ".join(conditions), params

    def _waybill_order_clause(self, filters: dict[str, Any]) -> str:
        sort = str(filters.get("sort", "") or "open_date_desc").strip().lower()
        if sort == "open_date_asc":
            return "open_date ASC, created_at ASC, id ASC"
        return "open_date DESC, created_at DESC, id DESC"

    def update_waybill_status(self, waybill_id: int, status: str) -> bool:
        normalized = normalize_waybill_status(status)
        if normalized not in WAYBILL_STATUS_LABELS:
            raise ValueError("Invalid waybill status")
        sql = (
            f"UPDATE waybills SET status = {self.placeholder}, updated_at = {self.placeholder} "
            f"WHERE id = {self.placeholder}"
        )
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [normalized, _now_iso(), int(waybill_id)])
            return int(cursor.rowcount or 0) > 0

    def _waybill_opening_cost(self, row: dict[str, Any]) -> tuple[Decimal, bool]:
        source = str(row.get("source", "") or "").strip()
        field_name = "transfer_fee" if source == "yunda" else "freight_fee"
        return _to_money_decimal(row.get(field_name))

    def _waybill_pickup_payment(self, row: dict[str, Any]) -> tuple[Decimal, bool]:
        payment_method = str(row.get("payment_method", "") or "")
        if "提付" not in payment_method and "到付" not in payment_method:
            return Decimal("0.00"), False
        cod_value = str(row.get("cod_amount", "") or "").strip()
        if cod_value:
            return _to_money_decimal(cod_value)
        return _to_money_decimal(row.get("freight_fee"))

    def _build_waybill_search_summary(self, rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
        totals = {
            "opening_cost_total": Decimal("0.00"),
            "pickup_payment_total": Decimal("0.00"),
        }
        invalid_money_count = 0
        source_counts: dict[str, int] = {}
        latest_created_at = ""
        latest_open_date = ""

        for row in rows:
            source = str(row.get("source", "") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1

            created_at = row.get("created_at")
            if created_at and hasattr(created_at, "strftime"):
                created_at_text = created_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                created_at_text = str(created_at or "")
            if created_at_text > latest_created_at:
                latest_created_at = created_at_text

            open_date = str(row.get("open_date", "") or "")
            if open_date > latest_open_date:
                latest_open_date = open_date

            amount, invalid = self._waybill_opening_cost(row)
            totals["opening_cost_total"] += amount
            invalid_money_count += int(invalid)
            amount, invalid = self._waybill_pickup_payment(row)
            totals["pickup_payment_total"] += amount
            invalid_money_count += int(invalid)

        opening_cost_total = _format_money(totals["opening_cost_total"])
        pickup_payment_total = _format_money(totals["pickup_payment_total"])
        return {
            "total": total,
            "manual_count": source_counts.get("manual", 0),
            "ocr_count": source_counts.get("ocr", 0),
            "fee_total": opening_cost_total,
            "opening_cost_total": opening_cost_total,
            "insurance_total": "0.00",
            "cod_total": pickup_payment_total,
            "pickup_payment_total": pickup_payment_total,
            "invalid_money_count": invalid_money_count,
            "latest_created_at": latest_created_at,
            "latest_open_date": latest_open_date,
        }

    def _row_to_waybill(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        for field in ("created_at", "updated_at"):
            if data.get(field) and hasattr(data[field], "strftime"):
                data[field] = data[field].strftime("%Y-%m-%d %H:%M:%S")
        data["source_label"] = WAYBILL_SOURCE_LABELS.get(
            str(data.get("source", "") or ""),
            str(data.get("source", "") or "未知"),
        )
        status = normalize_waybill_status(data.get("status", ""))
        data["status"] = status
        data["status_label"] = WAYBILL_STATUS_LABELS[status]
        data["status_tone"] = WAYBILL_STATUS_TONES[status]
        scan_status = normalize_waybill_scan_status(data.get("scan_status", ""))
        data["scan_status"] = "" if status == "cancelled" else scan_status
        data["scan_status_short"] = "" if status == "cancelled" else short_waybill_scan_status(scan_status)
        opening_cost, _ = self._waybill_opening_cost(data)
        pickup_payment, _ = self._waybill_pickup_payment(data)
        data["opening_cost"] = _format_money(opening_cost)
        data["pickup_payment_amount"] = _format_money(pickup_payment)
        data["print_url"] = f"/waybills/{data.get('id')}/print"
        data["tracking_url"] = f"/tracking?tracking_number={data.get('waybill_no', '')}"
        return data

    # ------------------------------------------------------------------
    # writers 表操作
    # ------------------------------------------------------------------

    def list_writers(self) -> list[dict[str, Any]]:
        sql = "SELECT * FROM writers ORDER BY display_name"
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql)
            return [dict(row) for row in cursor.fetchall()]

    def upsert_writer(self, writer_id: str, display_name: str = "") -> None:
        """插入或更新 writer，幂等。"""
        if not writer_id:
            return
        now = _now_iso()
        sql = (
            "INSERT INTO writers (writer_id, display_name, created_at) "
            f"VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}) "
            "ON DUPLICATE KEY UPDATE display_name = VALUES(display_name)"
        )
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, [writer_id, display_name or writer_id, now])

    # ------------------------------------------------------------------
    # 专线分流公司表操作
    # ------------------------------------------------------------------

    def search_line_haul_contacts(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        where_sql, params = self._build_line_haul_contact_where(filters or {})
        sql = (
            "SELECT id, company_name, service_area, address, contact_name, phone_numbers, "
            "remark, source_text, is_active, sort_order, created_at, updated_at "
            f"FROM line_haul_contacts WHERE {where_sql} "
            "ORDER BY sort_order ASC, company_name ASC, service_area ASC, id ASC"
        )
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [self._row_to_line_haul_contact(row) for row in rows]

    def search_line_haul_contacts_page(
        self,
        filters: dict[str, Any] | None = None,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        filters = filters or {}
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 50), 20), 100)
        offset = (page - 1) * page_size
        where_sql, params = self._build_line_haul_contact_where(filters)

        count_sql = (
            "SELECT COUNT(*) AS row_count, "
            "COALESCE(SUM(is_active = 1), 0) AS active_count, "
            "COALESCE(SUM(is_active = 0), 0) AS inactive_count "
            f"FROM line_haul_contacts WHERE {where_sql}"
        )
        rows_sql = (
            "SELECT id, company_name, service_area, address, contact_name, phone_numbers, "
            "remark, source_text, is_active, sort_order, created_at, updated_at "
            f"FROM line_haul_contacts WHERE {where_sql} "
            "ORDER BY sort_order ASC, company_name ASC, service_area ASC, id ASC "
            f"LIMIT {self.placeholder} OFFSET {self.placeholder}"
        )

        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(count_sql, params)
            count_row = cursor.fetchone() or {}
            total = int(count_row.get("row_count") or 0)
            total_pages = max((total + page_size - 1) // page_size, 1)
            page = min(page, total_pages)
            offset = (page - 1) * page_size

            cursor.execute(rows_sql, [*params, page_size, offset])
            rows = [self._row_to_line_haul_contact(row) for row in cursor.fetchall()]

        return {
            "rows": rows,
            "summary": {
                "total": total,
                "active_count": int(count_row.get("active_count") or 0),
                "inactive_count": int(count_row.get("inactive_count") or 0),
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "offset": offset,
                "has_prev": page > 1,
                "has_next": page < total_pages,
            },
        }

    def get_line_haul_contact(self, contact_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id, company_name, service_area, address, contact_name, phone_numbers,
                       remark, source_text, is_active, sort_order, created_at, updated_at
                FROM line_haul_contacts
                WHERE id = %s
                """,
                (int(contact_id),),
            )
            row = cursor.fetchone()
        return self._row_to_line_haul_contact(row) if row else None

    def create_line_haul_contact(self, values: dict[str, Any]) -> dict[str, Any]:
        payload = self._sanitize_line_haul_contact_payload(values)
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            sort_order = self._resolve_next_line_haul_sort_order(cursor, values.get("sort_order"))
            cursor.execute(
                """
                INSERT INTO line_haul_contacts (
                    company_name, service_area, address, contact_name, phone_numbers,
                    remark, source_text, is_active, sort_order, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
                """,
                (
                    payload["company_name"],
                    payload["service_area"],
                    payload["address"],
                    payload["contact_name"],
                    payload["phone_numbers"],
                    payload["remark"],
                    payload["source_text"],
                    sort_order,
                    now,
                    now,
                ),
            )
            contact_id = int(cursor.lastrowid)
            return self._select_line_haul_contact_for_cursor(cursor, contact_id)

    def update_line_haul_contact(self, contact_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        payload = self._sanitize_line_haul_contact_payload(values)
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE line_haul_contacts
                SET company_name = %s,
                    service_area = %s,
                    address = %s,
                    contact_name = %s,
                    phone_numbers = %s,
                    remark = %s,
                    source_text = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    payload["company_name"],
                    payload["service_area"],
                    payload["address"],
                    payload["contact_name"],
                    payload["phone_numbers"],
                    payload["remark"],
                    payload["source_text"],
                    now,
                    int(contact_id),
                ),
            )
            if cursor.rowcount == 0:
                return None
            return self._select_line_haul_contact_for_cursor(cursor, int(contact_id))

    def import_line_haul_contacts(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        stats = {"inserted": 0, "skipped_duplicate": 0}
        if not rows:
            return stats
        now = _now_iso()
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT COALESCE(MAX(sort_order), 0) AS max_sort FROM line_haul_contacts")
            base_sort = int((cursor.fetchone() or {}).get("max_sort") or 0)
            for index, row in enumerate(rows, start=1):
                payload = self._sanitize_line_haul_contact_payload(row)
                cursor.execute(
                    """
                    SELECT id
                    FROM line_haul_contacts
                    WHERE company_name = %s
                      AND service_area = %s
                      AND address = %s
                      AND phone_numbers = %s
                    LIMIT 1
                    """,
                    (
                        payload["company_name"],
                        payload["service_area"],
                        payload["address"],
                        payload["phone_numbers"],
                    ),
                )
                if cursor.fetchone():
                    stats["skipped_duplicate"] += 1
                    continue
                cursor.execute(
                    """
                    INSERT INTO line_haul_contacts (
                        company_name, service_area, address, contact_name, phone_numbers,
                        remark, source_text, is_active, sort_order, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
                    """,
                    (
                        payload["company_name"],
                        payload["service_area"],
                        payload["address"],
                        payload["contact_name"],
                        payload["phone_numbers"],
                        payload["remark"],
                        payload["source_text"],
                        base_sort + index * 10,
                        now,
                        now,
                    ),
                )
                stats["inserted"] += 1
        return stats

    def _build_line_haul_contact_where(self, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        conditions = ["1 = 1"]
        params: list[Any] = []
        q = str(filters.get("q", "") or "").strip()
        if q:
            like = f"%{q}%"
            conditions.append(
                "("
                "company_name LIKE %s OR service_area LIKE %s OR address LIKE %s OR "
                "contact_name LIKE %s OR phone_numbers LIKE %s OR remark LIKE %s"
                ")"
            )
            params.extend([like, like, like, like, like, like])
        return " AND ".join(conditions), params

    def _select_line_haul_contact_for_cursor(self, cursor: Any, contact_id: int) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT id, company_name, service_area, address, contact_name, phone_numbers,
                   remark, source_text, is_active, sort_order, created_at, updated_at
            FROM line_haul_contacts
            WHERE id = %s
            """,
            (int(contact_id),),
        )
        return self._row_to_line_haul_contact(cursor.fetchone() or {})

    @staticmethod
    def _sanitize_line_haul_contact_payload(values: dict[str, Any]) -> dict[str, str]:
        payload = {field: str(values.get(field, "") or "").strip() for field in LINE_HAUL_CONTACT_FIELDS}
        if not payload["source_text"]:
            payload["source_text"] = " ".join(
                value for key, value in payload.items() if key != "source_text" and value
            )
        return payload

    @staticmethod
    def _resolve_next_line_haul_sort_order(cursor: Any, raw_value: Any) -> int:
        try:
            value = int(str(raw_value or "").strip())
            if value >= 0:
                return value
        except ValueError:
            pass
        cursor.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 AS next_sort FROM line_haul_contacts")
        return int((cursor.fetchone() or {}).get("next_sort") or 10)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(value: Any, *, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_receipt_platform(value: Any) -> str:
        platform = str(value or "").strip().lower()
        return platform if platform in RECEIPT_PLATFORM_LABELS else ""

    @staticmethod
    def _normalize_receipt_direction(value: Any) -> str:
        direction = str(value or "").strip().lower()
        alias_map = {
            "sender": "send",
            "mailing": "send",
            "send": "send",
            "receiver": "receive",
            "delivery": "receive",
            "receive": "receive",
        }
        return alias_map.get(direction, "")

    @staticmethod
    def _receipt_detail_summary(raw_payload: Any, record: dict[str, Any] | None = None) -> dict[str, str]:
        record = dict(record or {})
        summary = {field: "" for field in RECEIPT_DETAIL_FIELD_CANDIDATES}
        summary["waybill_no"] = str(record.get("waybill_no") or "").strip()
        for field, candidates in RECEIPT_DETAIL_FIELD_CANDIDATES.items():
            if field == "waybill_no" and summary[field]:
                continue
            summary[field] = DocumentRepository._first_receipt_payload_value(raw_payload, candidates)
        if not summary["waybill_no"]:
            summary["waybill_no"] = str(record.get("receipt_no") or "").strip()
        return summary

    @staticmethod
    def _first_receipt_payload_value(payload: Any, candidates: tuple[str, ...]) -> str:
        for value in DocumentRepository._iter_receipt_payload_values(payload, set(candidates), depth=0):
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _iter_receipt_payload_values(payload: Any, candidates: set[str], *, depth: int) -> Iterator[Any]:
        if depth > 4:
            return
        if isinstance(payload, dict):
            for key, value in payload.items():
                if str(key) in candidates and value not in (None, ""):
                    yield value
            for value in payload.values():
                if isinstance(value, (dict, list, tuple)):
                    yield from DocumentRepository._iter_receipt_payload_values(value, candidates, depth=depth + 1)
            return
        if isinstance(payload, (list, tuple)):
            for item in payload:
                if isinstance(item, (dict, list, tuple)):
                    yield from DocumentRepository._iter_receipt_payload_values(item, candidates, depth=depth + 1)

    @staticmethod
    def _format_datetime_field(value: Any) -> str:
        if value and hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        if value is None:
            return ""
        return str(value)

    def _row_to_receipt_record(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        platform = self._normalize_receipt_platform(data.get("platform"))
        direction = self._normalize_receipt_direction(data.get("direction"))
        data["platform"] = platform
        data["platform_label"] = RECEIPT_PLATFORM_LABELS.get(platform, platform or "未知")
        data["direction"] = direction
        data["direction_label"] = RECEIPT_DIRECTION_LABELS.get(direction, direction or "未知")
        data["photo_count"] = self._safe_int(data.get("photo_count"), default=0)
        if not str(data.get("photo_status", "") or "").strip():
            data["photo_status"] = "已上传" if data["photo_count"] > 0 else "未上传"
        thumbnail_id = self._safe_int(data.pop("thumbnail_attachment_id", 0), default=0)
        data["thumbnail_url"] = f"/receipts/attachments/{thumbnail_id}" if thumbnail_id else ""
        raw_payload = data.pop("raw_payload_json", None)
        data["raw_payload"] = _loads_json(raw_payload, {})
        data["detail_summary"] = self._receipt_detail_summary(data["raw_payload"], data)
        for field in ("synced_at", "created_at", "updated_at"):
            data[field] = self._format_datetime_field(data.get(field))
        return data

    def _row_to_receipt_attachment(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        attachment_id = self._safe_int(data.get("id"), default=0)
        data["id"] = attachment_id
        data["record_id"] = self._safe_int(data.get("record_id"), default=0)
        data["file_size"] = self._safe_int(data.get("file_size"), default=0)
        data["file_url"] = f"/receipts/attachments/{attachment_id}" if attachment_id else ""
        for field in ("created_at", "updated_at"):
            data[field] = self._format_datetime_field(data.get(field))
        return data

    def _row_to_receipt_archive_attachment(self, row: Any) -> dict[str, Any]:
        source = dict(row or {})
        data = self._row_to_receipt_attachment(source)
        data["receipt_no"] = str(source.get("receipt_no") or "").strip()
        data["return_waybill_no"] = str(source.get("return_waybill_no") or "").strip()
        data["receipt_raw_payload"] = _loads_json(source.get("raw_payload_json"), {})
        return data

    def _row_to_receipt_audit_log(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        data["request_summary"] = redact_sensitive(
            _loads_json(data.pop("request_summary_json", None), {})
        )
        data["message"] = redact_text(data.get("message"))
        data["created_at"] = self._format_datetime_field(data.get("created_at"))
        return data

    def _sanitize_receipt_log_payload(self, value: Any) -> str:
        if isinstance(value, str):
            return redact_text(value)
        return json.dumps(redact_sensitive(value or {}), ensure_ascii=False)

    def _row_to_document(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["fields"] = json.loads(data.pop("fields_json"))
        data["raw_ocr"] = json.loads(data.pop("raw_ocr_json"))
        return data

    def _row_to_line_haul_contact(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        data["is_active"] = bool(data.get("is_active"))
        for field in ("created_at", "updated_at"):
            value = data.get(field)
            if hasattr(value, "strftime"):
                data[field] = value.strftime("%Y-%m-%d %H:%M:%S")
            elif value is None:
                data[field] = ""
            else:
                data[field] = str(value)
        return data

    def list_workflow_resources(self) -> list[dict[str, Any]]:
        return self._workflow_resources.list_records()

    def get_workflow_resource(self, resource_key: str) -> dict[str, Any] | None:
        return self._workflow_resources.get_record(resource_key)

    def upsert_workflow_resource(self, resource_key: str, config: dict[str, Any], source: str = "backend_console") -> None:
        self._workflow_resources.upsert(resource_key, config, source=source)

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        return self._scheduled_tasks.list_tasks()

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        return self._scheduled_tasks.get_task(task_id)

    def list_scheduled_task_group(self, base_task_id: str) -> list[dict[str, Any]]:
        rows = self.list_scheduled_tasks()
        return [
            row for row in rows
            if _normalize_scheduled_task_group_id(str(row.get("id", "") or "")) == base_task_id
        ]

    def upsert_scheduled_task(
        self,
        *,
        task_id: str,
        name: str,
        tool_name: str,
        tool_params: dict[str, Any],
        cron_expression: str,
        enabled: bool,
    ) -> None:
        self._scheduled_tasks.upsert_task(
            {
                "id": task_id,
                "name": name,
                "tool_name": tool_name,
                "tool_params": tool_params,
                "cron_expression": cron_expression,
                "enabled": enabled,
            }
        )

    def delete_scheduled_task(self, task_id: str) -> None:
        self._scheduled_tasks.delete_task(task_id)

    def replace_scheduled_task_group(
        self,
        *,
        base_task_id: str,
        tasks: list[dict[str, Any]],
    ) -> None:
        existing_ids = {
            str(row.get("id", "") or "")
            for row in self.list_scheduled_task_group(base_task_id)
        }
        incoming_ids = {
            str(task.get("task_id", "") or "")
            for task in tasks
        }

        self._scheduled_tasks.replace_tasks(
            [
                {
                    "id": task["task_id"],
                    "name": task["name"],
                    "tool_name": task["tool_name"],
                    "tool_params": task.get("tool_params") or {},
                    "cron_expression": task["cron_expression"],
                    "enabled": bool(task.get("enabled", False)),
                }
                for task in tasks
            ],
            stale_task_ids=existing_ids - incoming_ids,
        )

    def update_scheduled_task_runtime(
        self,
        *,
        base_task_id: str,
        last_run: str | None,
        last_status: str | None,
        last_duration_ms: int | None,
        last_message: str | None = None,
    ) -> None:
        group_rows = self.list_scheduled_task_group(base_task_id)
        if not group_rows:
            return

        group_ids = [str(row.get("id", "") or "") for row in group_rows if str(row.get("id", "") or "")]
        if not group_ids:
            return

        self._scheduled_tasks.update_runtime_at(
            group_ids,
            last_run=last_run,
            last_status=last_status,
            last_duration_ms=last_duration_ms,
            last_message=last_message,
        )
