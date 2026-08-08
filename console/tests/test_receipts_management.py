import base64
import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, select_autoescape


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

sys.modules.setdefault(
    "config",
    types.SimpleNamespace(
        MODULE_DIR=CONSOLE_DIR,
        PROJECT_ROOT=CONSOLE_DIR,
        Settings=object,
        load_settings=lambda: SimpleNamespace(),
    ),
)

from app import LocalDocFlowApp  # noqa: E402
from database import DocumentRepository  # noqa: E402


class _Handler:
    def __init__(self, body=b"", headers=None):
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        return None

    def header_value(self, name):
        for header_name, value in self.sent_headers:
            if header_name.lower() == name.lower():
                return value
        return ""


class _BinaryResponse:
    def __init__(self, payload, *, content_type="image/jpeg", status=200):
        self.payload = payload
        self.offset = 0
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _ReceiptRepo:
    def __init__(self):
        self.search_calls = []
        self.detail_calls = []
        self.waybill_calls = []
        self.attachment_calls = []
        self.archive_attachment_calls = []
        self.audit_logs = []
        self.audit_status_updates = []
        self.upserted_records = []
        self.upserted_attachments = []
        self.cache_updates = []
        self.receipt_detail = None
        self.waybills_by_no = {}

    def search_receipts(self, filters, *, page, page_size):
        self.search_calls.append({"filters": dict(filters), "page": page, "page_size": page_size})
        return {
            "rows": [
                {
                    "id": 3,
                    "platform": "yunda",
                    "platform_label": "韵达",
                    "direction": "send",
                    "direction_label": "寄件",
                    "waybill_no": "979903652",
                    "receipt_no": "HD001",
                    "return_waybill_no": "",
                    "receipt_status": "已返回",
                    "audit_status": "待审核",
                    "photo_status": "已上传",
                    "photo_count": 2,
                    "signed_confirmed": "否",
                    "updated_at": "2026-06-03 10:00:00",
                    "thumbnail_url": "/receipts/attachments/7",
                }
            ],
            "pagination": {
                "page": 1,
                "page_size": 50,
                "total": 1,
                "total_pages": 1,
                "has_prev": False,
                "has_next": False,
            },
        }

    def get_receipt_detail(self, receipt_id):
        self.detail_calls.append(receipt_id)
        if self.receipt_detail is not None:
            return self.receipt_detail
        return {
            "record": {"id": receipt_id, "platform": "yunda", "direction": "send", "waybill_no": "979903652"},
            "attachments": [
                {
                    "id": 7,
                    "attachment_type": "signed_receipt",
                    "display_name": "已签电子回单",
                    "file_url": "/receipts/attachments/7",
                }
            ],
            "audit_logs": [],
        }

    def get_waybill_by_no(self, waybill_no, *, source=None):
        self.waybill_calls.append({"waybill_no": waybill_no, "source": source})
        return self.waybills_by_no.get(str(waybill_no))

    def get_receipt_record(self, receipt_id):
        if receipt_id != 3:
            return None
        return {
            "id": 3,
            "platform": "yunda",
            "platform_label": "韵达",
            "direction": "send",
            "direction_label": "寄件",
            "waybill_no": "979903652",
            "receipt_no": "HD001",
            "return_waybill_no": "",
            "audit_status": "待审核",
            "raw_payload": {"token": "must-not-be-logged", "收货人": "王五"},
        }

    def update_receipt_audit_status(self, receipt_id, audit_status):
        self.audit_status_updates.append({"receipt_id": receipt_id, "audit_status": audit_status})
        record = self.get_receipt_record(receipt_id)
        if not record:
            return None
        return {**record, "audit_status": audit_status}

    def get_receipt_attachment(self, attachment_id):
        self.attachment_calls.append(attachment_id)
        return {
            "id": attachment_id,
            "mime_type": "image/png",
            "local_path": "receipts/yunda/979903652/demo.png",
            "source_url": "https://example.test/demo.png",
        }

    def list_receipt_image_attachments_for_filters(self, filters):
        self.archive_attachment_calls.append(dict(filters))
        return [
            {
                "id": 7,
                "record_id": 3,
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "980606124",
                "display_name": "receipt.jpg",
                "mime_type": "image/jpeg",
                "local_path": "receipts/yunda/3/receipt.jpg",
                "source_url": "https://example.test/receipt.jpg",
                "receipt_raw_payload": {"备注信息": "原单备注 2231-2606000006 其他文字"},
            },
            {
                "id": 8,
                "record_id": 4,
                "platform": "ronghui",
                "direction": "send",
                "waybill_no": "2003441427",
                "display_name": "receipt.png",
                "mime_type": "image/png",
                "local_path": "receipts/ronghui/4/receipt.png",
                "source_url": "https://example.test/receipt.png",
                "receipt_raw_payload": {"备注信息": "没有匹配备注"},
            },
            {
                "id": 9,
                "record_id": 5,
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "980777777",
                "display_name": "receipt.webp",
                "mime_type": "image/webp",
                "local_path": "receipts/yunda/5/receipt.webp",
                "source_url": "https://example.test/receipt.webp",
                "receipt_raw_payload": {"remark": {"lines": ["other text 2444-2606000007"]}},
            },
        ]

    def record_receipt_audit_log(self, **kwargs):
        self.audit_logs.append(dict(kwargs))

    def upsert_receipt_record(self, payload):
        self.upserted_records.append(dict(payload))
        return {"id": 88, **payload}

    def upsert_receipt_attachment(self, payload):
        self.upserted_attachments.append(dict(payload))
        return {"id": len(self.upserted_attachments), **payload}

    def update_receipt_attachment_cache(self, attachment_id, *, local_path, file_hash, mime_type, file_size):
        self.cache_updates.append(
            {
                "attachment_id": attachment_id,
                "local_path": local_path,
                "file_hash": file_hash,
                "mime_type": mime_type,
                "file_size": file_size,
            }
        )


def _build_app(repository):
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.settings = SimpleNamespace(
        app_title="Test Console",
        agent_base_url="http://agent.test",
        agent_timeout_seconds=30,
        runtime_dir=Path(tempfile.mkdtemp()),
    )
    app.repository = repository
    app.template_env = Environment(
        loader=FileSystemLoader(CONSOLE_DIR / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return app


class ReceiptRepositoryTests(unittest.TestCase):
    def test_receipt_search_where_filters_allowed_fields(self):
        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"

        where_sql, params = repo._build_receipt_search_where(
            {
                "platform": "yunda",
                "direction": "send",
                "q": "979903652",
                "audit_status": "待审核",
                "photo_status": "has_photo",
                "date_from": "2026-06-01",
                "date_to": "2026-06-03",
            }
        )

        self.assertIn("platform = %s", where_sql)
        self.assertIn("direction = %s", where_sql)
        self.assertIn("waybill_no LIKE", where_sql)
        self.assertIn("receipt_no LIKE", where_sql)
        self.assertIn("audit_status = %s", where_sql)
        self.assertIn("audit_status LIKE %s", where_sql)
        self.assertIn("photo_count > 0", where_sql)
        self.assertIn("updated_at >=", where_sql)
        self.assertIn("updated_at <=", where_sql)
        self.assertIn("NOT (platform = 'yunda' AND direction = 'receive')", where_sql)
        self.assertEqual(
            [
                "yunda",
                "send",
                "%979903652%",
                "%979903652%",
                "%979903652%",
                "待审核",
                "%待%",
                "%审核%",
                "2026-06-01 00:00:00",
                "2026-06-03 23:59:59",
            ],
            params,
        )

    def test_receipt_pending_audit_filter_includes_ronghui_directional_statuses(self):
        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"

        where_sql, params = repo._build_receipt_search_where({"audit_status": "待审核"})

        self.assertIn("audit_status = %s", where_sql)
        self.assertIn("audit_status LIKE %s", where_sql)
        self.assertEqual(["待审核", "%待%", "%审核%"], params)

    def test_receipt_upsert_payload_uses_platform_direction_waybill_receipt_key(self):
        calls = []

        class _Cursor:
            lastrowid = 12

            def execute(self, sql, params=None):
                calls.append((sql, params))

            def fetchone(self):
                return {"id": 12}

        class _Context:
            def __enter__(self):
                return SimpleNamespace(cursor=lambda: _Cursor())

            def __exit__(self, exc_type, exc, tb):
                return False

        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"
        repo.connect = lambda: _Context()
        repo.get_receipt_record = lambda receipt_id: {"id": receipt_id}

        result = repo.upsert_receipt_record(
            {
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "979903652",
                "receipt_no": "HD001",
                "receipt_status": "已返回",
                "audit_status": "待审核",
                "raw_payload": {"a": 1},
            }
        )

        self.assertEqual({"id": 12}, result)
        self.assertTrue(any("receipt_records" in sql and "ON DUPLICATE KEY UPDATE" in sql for sql, _ in calls))

    def test_receipt_upsert_does_not_downgrade_completed_audit_to_pending_sync_status(self):
        calls = []

        class _Cursor:
            lastrowid = 12

            def execute(self, sql, params=None):
                calls.append((sql, params or []))

            def fetchone(self):
                return {"id": 12}

        class _Context:
            def __enter__(self):
                return SimpleNamespace(cursor=lambda: _Cursor())

            def __exit__(self, exc_type, exc, tb):
                return False

        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"
        repo.connect = lambda: _Context()
        repo.get_receipt_record = lambda receipt_id: {"id": receipt_id, "audit_status": "\u5ba1\u6838\u901a\u8fc7"}

        result = repo.upsert_receipt_record(
            {
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "982806097",
                "receipt_no": "",
                "receipt_status": "\u59cb\u53d1\u5206\u62e8\u5df2\u53d1",
                "audit_status": "\u5f85\u5ba1\u6838",
                "raw_payload": {"waybill_no": "982806097"},
            }
        )

        self.assertEqual({"id": 12, "audit_status": "\u5ba1\u6838\u901a\u8fc7"}, result)
        upsert_sql, upsert_params = calls[0]
        self.assertIn("audit_status = CASE", upsert_sql)
        self.assertIn("THEN audit_status ELSE VALUES(audit_status) END", upsert_sql)
        self.assertIn("\u5ba1\u6838\u901a\u8fc7", upsert_params)
        self.assertIn("\u5ba1\u6838\u4e0d\u901a\u8fc7", upsert_params)
        self.assertIn("\u5f85\u5ba1\u6838", upsert_params)
        self.assertIn("\u672a\u5ba1\u6838", upsert_params)

    def test_receipt_upsert_restores_completed_audit_from_success_log_after_stale_sync(self):
        updates = []

        class _Cursor:
            lastrowid = 12

            def execute(self, sql, params=None):
                return None

            def fetchone(self):
                return {"id": 12}

        class _Context:
            def __enter__(self):
                return SimpleNamespace(cursor=lambda: _Cursor())

            def __exit__(self, exc_type, exc, tb):
                return False

        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"
        repo.connect = lambda: _Context()
        repo.get_receipt_record = lambda receipt_id: {"id": receipt_id, "audit_status": "\u5f85\u5ba1\u6838"}
        repo.list_receipt_audit_logs = lambda receipt_id, limit=100: [
            {
                "action": "audit_original_page",
                "result_status": "success",
                "request_summary": {"result": "passed"},
            }
        ]

        def update_status(receipt_id, audit_status):
            updates.append({"receipt_id": receipt_id, "audit_status": audit_status})
            return {"id": receipt_id, "audit_status": audit_status}

        repo.update_receipt_audit_status = update_status

        result = repo.upsert_receipt_record(
            {
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "982806097",
                "receipt_no": "",
                "receipt_status": "\u59cb\u53d1\u5206\u62e8\u5df2\u53d1",
                "audit_status": "\u5f85\u5ba1\u6838",
                "raw_payload": {"waybill_no": "982806097"},
            }
        )

        self.assertEqual({"id": 12, "audit_status": "\u5ba1\u6838\u901a\u8fc7"}, result)
        self.assertEqual([{"receipt_id": 12, "audit_status": "\u5ba1\u6838\u901a\u8fc7"}], updates)

    def test_receipt_attachment_upsert_matches_existing_source_url_before_hash(self):
        calls = []

        class _Cursor:
            lastrowid = 0

            def execute(self, sql, params=None):
                calls.append((sql, params))

            def fetchone(self):
                return {"id": 21}

        class _Context:
            def __enter__(self):
                return SimpleNamespace(cursor=lambda: _Cursor())

            def __exit__(self, exc_type, exc, tb):
                return False

        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"
        repo.connect = lambda: _Context()
        repo.get_receipt_attachment = lambda attachment_id: {"id": attachment_id}

        result = repo.upsert_receipt_attachment(
            {
                "record_id": 88,
                "attachment_type": "FIND_TAB_PIC_SCAN_ALL",
                "display_name": "demo.jpg",
                "source_url": "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/demo.jpg",
                "file_hash": "source-url-hash",
            }
        )

        self.assertEqual({"id": 21}, result)
        first_sql, first_params = calls[0]
        self.assertIn("source_url =", first_sql)
        self.assertEqual([88, "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/demo.jpg"], first_params)
        self.assertFalse(any("INSERT INTO receipt_attachments" in sql for sql, _ in calls))

    def test_receipt_attachment_cache_preserves_existing_source_hash(self):
        calls = []

        class _Cursor:
            def execute(self, sql, params=None):
                calls.append((sql, params))

        class _Context:
            def __enter__(self):
                return SimpleNamespace(cursor=lambda: _Cursor())

            def __exit__(self, exc_type, exc, tb):
                return False

        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"
        repo.connect = lambda: _Context()

        repo.update_receipt_attachment_cache(
            21,
            local_path="receipts/ronghui/88/21_demo.jpg",
            file_hash="content-digest",
            mime_type="image/jpeg",
            file_size=12345,
        )

        self.assertIn("CASE WHEN file_hash IS NULL OR file_hash = ''", calls[0][0])

    def test_receipt_attachment_list_dedupes_same_source_url(self):
        class _Cursor:
            def execute(self, sql, params=None):
                return None

            def fetchall(self):
                return [
                    {
                        "id": 21,
                        "record_id": 88,
                        "platform": "ronghui",
                        "direction": "send",
                        "waybill_no": "2003441429",
                        "source_url": "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/demo.jpg",
                        "local_path": "",
                        "file_hash": "source-hash",
                        "file_size": 0,
                    },
                    {
                        "id": 22,
                        "record_id": 88,
                        "platform": "ronghui",
                        "direction": "send",
                        "waybill_no": "2003441429",
                        "source_url": "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/demo.jpg",
                        "local_path": "receipts/ronghui/88/22_demo.jpg",
                        "file_hash": "content-digest",
                        "file_size": 12345,
                    },
                ]

        class _Context:
            def __enter__(self):
                return SimpleNamespace(cursor=lambda: _Cursor())

            def __exit__(self, exc_type, exc, tb):
                return False

        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"
        repo.connect = lambda: _Context()

        rows = repo.list_receipt_attachments(88)

        self.assertEqual(1, len(rows))
        self.assertEqual(22, rows[0]["id"])

    def test_receipt_image_archive_filters_qualify_record_columns_in_join(self):
        calls = []

        class _Cursor:
            def execute(self, sql, params=None):
                calls.append((sql, params))

            def fetchall(self):
                return []

        class _Context:
            def __enter__(self):
                return SimpleNamespace(cursor=lambda: _Cursor())

            def __exit__(self, exc_type, exc, tb):
                return False

        repo = DocumentRepository.__new__(DocumentRepository)
        repo.placeholder = "%s"
        repo.connect = lambda: _Context()

        repo.list_receipt_image_attachments_for_filters(
            {
                "platform": "yunda",
                "direction": "send",
                "q": "980606124",
                "audit_status": "审核通过",
                "photo_status": "has_photo",
                "date_from": "2026-06-01",
                "date_to": "2026-06-05",
            }
        )

        sql = calls[0][0]
        self.assertIn("r.platform = %s", sql)
        self.assertIn("r.direction = %s", sql)
        self.assertIn("r.waybill_no LIKE", sql)
        self.assertIn("r.audit_status = %s", sql)
        self.assertIn("r.photo_count > 0", sql)
        self.assertIn("r.updated_at >=", sql)
        self.assertIn("r.updated_at <=", sql)
        self.assertIn("NOT (r.platform = 'yunda' AND r.direction = 'receive')", sql)


class ReceiptRouteTests(unittest.TestCase):
    def test_render_receipts_starts_empty_until_query_submit(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        handler = _Handler()

        app._render_receipts(handler, {})

        html = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual([], repo.search_calls)
        self.assertIn("共 0 条", html)
        self.assertIn("暂无回单数据", html)
        self.assertIn('name="queried" value=""', html)

    def test_render_receipts_passes_filters_to_repository(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        handler = _Handler()
        today = datetime.now().strftime("%Y-%m-%d")

        app._render_receipts(
            handler,
            {
                "platform": ["yunda"],
                "direction": ["send"],
                "q": ["979903652"],
                "audit_status": ["待审核"],
                "photo_status": ["has_photo"],
                "queried": ["1"],
            },
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual(
            {
                "platform": "yunda",
                "direction": "send",
                "q": "979903652",
                "receipt_status": "all",
                "audit_status": "待审核",
                "photo_status": "has_photo",
                "date_from": today,
                "date_to": today,
            },
            repo.search_calls[0]["filters"],
        )
        html = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("data-receipts-page", html)
        self.assertIn("979903652", html)
        self.assertIn("data-receipt-quick-range=\"3\"", html)
        self.assertIn("data-receipt-thumb-preview", html)
        self.assertIn("data-receipt-audit-modal", html)
        self.assertIn("data-receipt-spinner", html)
        self.assertIn('name="queried" value="1"', html)
        self.assertIn(f'value="{today}" data-receipt-date-from', html)
        self.assertIn(f'value="{today}" data-receipt-date-to', html)
        self.assertIn("/ky_inms/public/index.php/business/waybill/delivery/index.html", html)

    def test_render_receipts_forces_send_direction_even_with_old_receive_query(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        handler = _Handler()

        app._render_receipts(
            handler,
            {"direction": ["receive"], "platform": ["ronghui"], "queried": ["1"]},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("send", repo.search_calls[0]["filters"]["direction"])
        html = handler.wfile.getvalue().decode("utf-8")
        self.assertIn('name="direction" value="send"', html)
        self.assertNotIn('name="direction">', html)

    def test_receipts_data_starts_empty_until_query_submit(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipts_data(handler, {"platform": ["ronghui"], "page_size": ["25"]})

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["data"]["rows"])
        self.assertEqual(0, payload["data"]["pagination"]["total"])
        self.assertEqual([], repo.search_calls)

    def test_receipts_data_returns_json_rows(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipts_data(
            handler,
            {"platform": ["ronghui"], "direction": ["receive"], "page_size": ["25"], "queried": ["1"]},
        )

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual("979903652", payload["data"]["rows"][0]["waybill_no"])
        self.assertEqual("ronghui", repo.search_calls[0]["filters"]["platform"])
        self.assertEqual("send", repo.search_calls[0]["filters"]["direction"])
        self.assertEqual(25, repo.search_calls[0]["page_size"])

    def test_receipt_detail_returns_record_attachments_and_audit_logs(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipt_detail(handler, "/receipts/3")

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual(3, payload["data"]["record"]["id"])
        self.assertEqual(7, payload["data"]["attachments"][0]["id"])

    def test_receipt_detail_enriches_missing_summary_from_local_waybill(self):
        repo = _ReceiptRepo()
        repo.receipt_detail = {
            "record": {
                "id": 3,
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "981296115",
                "raw_payload": {"运单号": "981296115"},
                "detail_summary": {
                    "recipient_name": "",
                    "recipient_address": "",
                    "goods_name": "",
                    "package_type": "",
                    "piece_count": "",
                    "actual_weight": "",
                    "volume": "",
                    "waybill_no": "981296115",
                },
            },
            "attachments": [],
            "audit_logs": [],
        }
        repo.waybills_by_no["981296115"] = {
            "waybill_no": "981296115",
            "receiver_name": "山东客户",
            "receiver_address": "山东省潍坊市奎文区",
            "goods_name_lines": "轴承",
            "package_type_lines": "纸箱",
            "quantity_lines": "3",
            "weight_volume": "实际重量 28.50 / 体积 0.32",
        }
        app = _build_app(repo)
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipt_detail(handler, "/receipts/3")

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        record = payload["data"]["record"]
        self.assertEqual("山东客户", record["detail_summary"]["recipient_name"])
        self.assertEqual("山东省潍坊市奎文区", record["detail_summary"]["recipient_address"])
        self.assertEqual("轴承", record["detail_summary"]["goods_name"])
        self.assertEqual("纸箱", record["detail_summary"]["package_type"])
        self.assertEqual("3", record["detail_summary"]["piece_count"])
        self.assertEqual("28.50", record["detail_summary"]["actual_weight"])
        self.assertEqual("0.32", record["detail_summary"]["volume"])
        self.assertEqual("raw_payload,local_waybills", record["detail_summary_source"])
        self.assertEqual([], record["detail_summary_missing"])
        self.assertEqual([{"waybill_no": "981296115", "source": "yunda"}], repo.waybill_calls)

    def test_receipt_detail_does_not_guess_weight_volume_without_explicit_labels(self):
        repo = _ReceiptRepo()
        repo.receipt_detail = {
            "record": {
                "id": 3,
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "981296115",
                "raw_payload": {},
                "detail_summary": {},
            },
            "attachments": [],
            "audit_logs": [],
        }
        repo.waybills_by_no["981296115"] = {
            "waybill_no": "981296115",
            "receiver_name": "山东客户",
            "weight_volume": "28.50 / 0.32",
        }
        app = _build_app(repo)
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipt_detail(handler, "/receipts/3")

        record = json.loads(handler.wfile.getvalue().decode("utf-8"))["data"]["record"]
        self.assertEqual("", record["detail_summary"]["actual_weight"])
        self.assertEqual("", record["detail_summary"]["volume"])
        self.assertIn("actual_weight", record["detail_summary_missing"])
        self.assertIn("volume", record["detail_summary_missing"])

    def test_receipt_detail_enriches_ronghui_from_tms_detail_when_local_missing(self):
        repo = _ReceiptRepo()
        repo.receipt_detail = {
            "record": {
                "id": 4,
                "platform": "ronghui",
                "direction": "send",
                "waybill_no": "2003441429",
                "raw_payload": {},
                "detail_summary": {},
            },
            "attachments": [],
            "audit_logs": [],
        }
        app = _build_app(repo)
        agent_calls = []

        def fake_agent(method, endpoint, *, payload=None, timeout=None):
            agent_calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "data": {
                        "records": [
                            {
                                "bill_code": "2003441429",
                                "recipient_name": "融辉收货人",
                                "recipient_address": "湖南省邵阳市",
                                "goods_name": "配件",
                                "package_type": "木箱",
                                "quantity": "1",
                                "actual_weight": "106",
                                "volume": "0.58",
                                "tracking_number": "2003441429",
                            }
                        ]
                    },
                },
            }

        app._agent_request = fake_agent
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipt_detail(handler, "/receipts/4")

        record = json.loads(handler.wfile.getvalue().decode("utf-8"))["data"]["record"]
        self.assertEqual("融辉收货人", record["detail_summary"]["recipient_name"])
        self.assertEqual("106", record["detail_summary"]["actual_weight"])
        self.assertEqual("tms_detail", record["detail_summary_source"])
        self.assertEqual("/internal/v1/tms/query_waybill_detail", agent_calls[0]["endpoint"])
        self.assertEqual(["2003441429"], agent_calls[0]["payload"]["params"]["bill_codes"])

    def test_receipt_detail_uses_feishu_exact_search_only_after_local_yunda_missing(self):
        repo = _ReceiptRepo()
        repo.receipt_detail = {
            "record": {
                "id": 5,
                "platform": "yunda",
                "direction": "send",
                "waybill_no": "981296115",
                "raw_payload": {},
                "detail_summary": {},
            },
            "attachments": [],
            "audit_logs": [],
        }
        app = _build_app(repo)
        agent_calls = []

        def fake_agent(method, endpoint, *, payload=None, timeout=None):
            agent_calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "items": [
                        {
                            "record_id": "rec-1",
                            "fields": {
                                "运单编号": "981296115",
                                "收货人": "飞书收货人",
                                "收件地址": "上海市青浦区",
                                "货物名称": "电机",
                                "包装类型": "托盘",
                                "件数": "2",
                                "实际重量": "88",
                                "体积": "0.45",
                            },
                        }
                    ],
                },
            }

        app._agent_request = fake_agent
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipt_detail(handler, "/receipts/5")

        record = json.loads(handler.wfile.getvalue().decode("utf-8"))["data"]["record"]
        self.assertEqual("飞书收货人", record["detail_summary"]["recipient_name"])
        self.assertEqual("feishu_bitable", record["detail_summary_source"])
        self.assertEqual("/internal/v1/tools/run", agent_calls[0]["endpoint"])
        self.assertEqual("feishu_operation", agent_calls[0]["payload"]["tool_name"])
        self.assertEqual("search_records", agent_calls[0]["payload"]["params"]["action"])
        self.assertEqual(
            {
                "conjunction": "and",
                "conditions": [{"field_name": "运单编号", "operator": "is", "value": ["981296115"]}],
            },
            agent_calls[0]["payload"]["params"]["params"]["filter"],
        )
        self.assertEqual(1, agent_calls[0]["payload"]["params"]["params"]["page_size"])

    def test_receipt_detail_keeps_images_available_when_external_enrichment_fails(self):
        repo = _ReceiptRepo()
        repo.receipt_detail = {
            "record": {
                "id": 4,
                "platform": "ronghui",
                "direction": "send",
                "waybill_no": "2003441429",
                "raw_payload": {},
                "detail_summary": {},
            },
            "attachments": [{"id": 9, "file_url": "/receipts/attachments/9"}],
            "audit_logs": [],
        }
        app = _build_app(repo)
        app._agent_request = lambda *args, **kwargs: {"ok": False, "error": "agent unavailable"}
        handler = _Handler(headers={"Accept": "application/json"})

        app._handle_receipt_detail(handler, "/receipts/4")

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual(9, payload["data"]["attachments"][0]["id"])
        self.assertIn("recipient_name", payload["data"]["record"]["detail_summary_missing"])
        self.assertIn("agent unavailable", payload["data"]["record"]["detail_summary_error"])

    def test_receipt_audit_route_updates_status_and_logs_sanitized_summary(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        app._agent_request = lambda *args, **kwargs: {
            "ok": True,
            "data": {
                "ok": True,
                "data": {
                    "ok": True,
                    "platform": "yunda",
                    "result_status": "passed",
                    "audit_status": "审核通过",
                    "message": "审核完成",
                },
            },
        }
        handler = _Handler(
            json.dumps({"result": "passed", "reason": ""}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )

        app._handle_receipt_audit(handler, "/receipts/3/audit")

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual([{"receipt_id": 3, "audit_status": "审核通过"}], repo.audit_status_updates)
        self.assertEqual("success", repo.audit_logs[-1]["result_status"])
        self.assertNotIn("raw_payload", repo.audit_logs[-1]["request_summary"])
        self.assertNotIn("token", json.dumps(repo.audit_logs[-1], ensure_ascii=False))

    def test_receipt_audit_route_does_not_update_when_agent_requires_capture(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        app._agent_request = lambda *args, **kwargs: {
            "ok": True,
            "data": {
                "ok": False,
                "error_code": "AUDIT_CAPTURE_REQUIRED",
                "message": "回单审核真实接口尚未抓取，未执行第三方审核请求。",
            },
        }
        handler = _Handler(
            json.dumps({"result": "failed", "reason": "照片不清晰"}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )

        app._handle_receipt_audit(handler, "/receipts/3/audit")

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertFalse(payload["ok"])
        self.assertEqual("AUDIT_CAPTURE_REQUIRED", payload["error_code"])
        self.assertEqual([], repo.audit_status_updates)
        self.assertEqual("failed", repo.audit_logs[-1]["result_status"])

    def test_receipt_audit_route_records_original_page_execution_without_agent_call(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)

        def fail_agent(*args, **kwargs):
            raise AssertionError("original page execution should not call agent audit adapter")

        app._agent_request = fail_agent
        handler = _Handler(
            json.dumps({"result": "passed", "reason": "", "execution": "original_page"}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )

        app._handle_receipt_audit(handler, "/receipts/3/audit")

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual([{"receipt_id": 3, "audit_status": "审核通过"}], repo.audit_status_updates)
        self.assertEqual("success", repo.audit_logs[-1]["result_status"])
        self.assertEqual("ORIGINAL_PAGE_EXECUTED", repo.audit_logs[-1]["response_status"])
        self.assertNotIn("raw_payload", repo.audit_logs[-1]["request_summary"])

    def test_receipt_attachment_serves_cached_file(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        file_path = app.settings.runtime_dir / "receipts" / "yunda" / "979903652" / "demo.png"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        handler = _Handler()

        app._handle_receipt_attachment(handler, "/receipts/attachments/7")

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("image/png", handler.header_value("Content-Type"))
        self.assertEqual("", handler.header_value("Content-Disposition"))
        self.assertEqual(b"\x89PNG\r\n\x1a\n", handler.wfile.getvalue())

    def test_receipt_attachment_download_sets_attachment_header(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        file_path = app.settings.runtime_dir / "receipts" / "yunda" / "979903652" / "demo.png"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        handler = _Handler()

        app._handle_receipt_attachment(handler, "/receipts/attachments/7", {"download": ["1"]})

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("image/png", handler.header_value("Content-Type"))
        self.assertIn("attachment;", handler.header_value("Content-Disposition"))
        self.assertIn("receipt-7.png", handler.header_value("Content-Disposition"))
        self.assertEqual(b"\x89PNG\r\n\x1a\n", handler.wfile.getvalue())

    def test_receipt_attachment_recaches_when_cached_file_is_not_image(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        file_path = app.settings.runtime_dir / "receipts" / "yunda" / "979903652" / "demo.png"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"<html>bad cache</html>")
        handler = _Handler()

        def fetch_source(attachment, source_url):
            return b"\xff\xd8fresh-image", "image/jpeg"

        app._fetch_receipt_attachment_source = fetch_source
        app._handle_receipt_attachment(handler, "/receipts/attachments/7")

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("image/jpeg", handler.header_value("Content-Type"))
        self.assertEqual(b"\xff\xd8fresh-image", handler.wfile.getvalue())
        self.assertEqual(7, repo.cache_updates[0]["attachment_id"])
        self.assertTrue(repo.cache_updates[0]["local_path"].endswith(".png"))

    def test_receipt_image_archive_downloads_current_query_images_with_remark_names(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        yunda_file = app.settings.runtime_dir / "receipts" / "yunda" / "3" / "receipt.jpg"
        ronghui_file = app.settings.runtime_dir / "receipts" / "ronghui" / "4" / "receipt.png"
        nested_remark_file = app.settings.runtime_dir / "receipts" / "yunda" / "5" / "receipt.webp"
        yunda_file.parent.mkdir(parents=True)
        ronghui_file.parent.mkdir(parents=True)
        nested_remark_file.parent.mkdir(parents=True)
        yunda_file.write_bytes(b"\xff\xd8yunda")
        ronghui_file.write_bytes(b"\x89PNGronghui")
        nested_remark_file.write_bytes(b"RIFFxxxxWEBPreceipt")
        handler = _Handler()

        app._handle_receipts_image_archive(
            handler,
            {"platform": ["yunda"], "date_from": ["2026-06-01"], "date_to": ["2026-06-05"], "queried": ["1"]},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("application/zip", handler.header_value("Content-Type"))
        self.assertIn("receipt-images-2026-06-01-2026-06-05.zip", handler.header_value("Content-Disposition"))
        self.assertEqual("yunda", repo.archive_attachment_calls[0]["platform"])
        with zipfile.ZipFile(io.BytesIO(handler.wfile.getvalue())) as archive:
            names = sorted(archive.namelist())
            self.assertEqual(["2003441427.png", "980606124-231-2606000006.jpg", "980777777-444-2606000007.webp"], names)
            self.assertEqual(b"\xff\xd8yunda", archive.read("980606124-231-2606000006.jpg"))

    def test_receipt_attachment_fetches_whitelisted_ronghui_obs_image(self):
        app = _build_app(_ReceiptRepo())
        response = _BinaryResponse(b"\xff\xd8receipt-image", content_type="image/jpeg")

        with patch("console.services.waybills_receipts.urlopen", return_value=response) as mocked_urlopen:
            result = app._fetch_receipt_attachment_source(
                {"platform": "ronghui"},
                "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg",
            )

        self.assertEqual((b"\xff\xd8receipt-image", "image/jpeg"), result)
        self.assertIn("rhk13.obs.cn-east-3.myhuaweicloud.com", mocked_urlopen.call_args.args[0].full_url)

    def test_receipt_attachment_normalizes_bare_ronghui_obs_image_url(self):
        app = _build_app(_ReceiptRepo())
        response = _BinaryResponse(b"\xff\xd8receipt-image", content_type="image/jpeg")

        with patch("console.services.waybills_receipts.urlopen", return_value=response) as mocked_urlopen:
            result = app._fetch_receipt_attachment_source(
                {"platform": "ronghui"},
                "rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg",
            )

        self.assertEqual((b"\xff\xd8receipt-image", "image/jpeg"), result)
        self.assertEqual(
            "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg",
            mocked_urlopen.call_args.args[0].full_url,
        )

    def test_receipt_attachment_normalizes_ronghui_origin_prefixed_obs_image_url(self):
        app = _build_app(_ReceiptRepo())
        response = _BinaryResponse(b"\xff\xd8receipt-image", content_type="image/jpeg")

        with patch("console.services.waybills_receipts.urlopen", return_value=response) as mocked_urlopen:
            result = app._fetch_receipt_attachment_source(
                {"platform": "ronghui"},
                "https://tms.ronghuiwl.com/rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg",
            )

        self.assertEqual((b"\xff\xd8receipt-image", "image/jpeg"), result)
        self.assertEqual(
            "https://rhk13.obs.cn-east-3.myhuaweicloud.com/k13/20260604/demo.jpg",
            mocked_urlopen.call_args.args[0].full_url,
        )

    def test_receipt_attachment_rejects_proxy_payload_that_is_not_image(self):
        app = _build_app(_ReceiptRepo())

        def agent_request(method, endpoint, *, payload=None, timeout=None):
            encoded = base64.b64encode(b"<html>not an image</html>").decode("ascii")
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "status_code": 200,
                    "headers": {"Content-Type": "text/html"},
                    "body_base64": encoded,
                },
            }

        app._agent_request = agent_request
        result = app._fetch_receipt_attachment_source(
            {"platform": "ronghui"},
            "https://tms.ronghuiwl.com/unauth/download/demo.jpg",
        )

        self.assertIsNone(result)

    def test_receipt_attachment_rejects_non_whitelisted_direct_image_host(self):
        app = _build_app(_ReceiptRepo())

        with patch("console.services.waybills_receipts.urlopen") as mocked_urlopen:
            result = app._fetch_receipt_attachment_source(
                {"platform": "ronghui"},
                "https://example.test/k13/demo.jpg",
            )

        self.assertIsNone(result)
        mocked_urlopen.assert_not_called()

    def test_receipts_query_rejects_incomplete_date_range_without_agent_call(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        calls = []

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            return {"ok": True, "data": {"ok": True, "data": {"records": []}}}

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            body=b"platform=all&direction=all&date_from=2026-06-01&date_to=",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Accept": "application/json"},
        )

        app._handle_receipts_sync(handler)

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.BAD_REQUEST, handler.status)
        self.assertFalse(payload["ok"])
        self.assertEqual([], calls)

    def test_receipts_query_uses_bounded_agent_timeout_and_query_limits(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "data": {
                        "records": [
                            {
                                "platform": "ronghui",
                                "direction": "send",
                                "waybill_no": "R0001",
                                "receipt_no": "HD0001",
                                "attachments": [{"source_url": "https://example.test/a.jpg", "file_hash": "h1"}],
                            }
                        ],
                        "stats": {"fetched": 1},
                        "warnings": [],
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            body=b"platform=all&direction=all&date_from=2026-06-01&date_to=2026-06-03",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Accept": "application/json"},
        )

        app._handle_receipts_sync(handler)

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual("/internal/v1/tms/receipts_sync", app.last_call["endpoint"])
        self.assertEqual("send", app.last_call["payload"]["params"]["direction"])
        self.assertLess(app.last_call["payload"]["timeout_sec"], 900)
        self.assertLess(app.last_call["timeout"], 900)
        self.assertEqual("5", app.last_call["payload"]["params"]["max_pages"])
        self.assertEqual("12", app.last_call["payload"]["params"]["timeout_sec"])
        self.assertEqual(1, len(repo.upserted_records))
        self.assertEqual(1, len(repo.upserted_attachments))

    def test_receipts_query_defaults_empty_date_range_to_today(self):
        repo = _ReceiptRepo()
        app = _build_app(repo)
        today = datetime.now().strftime("%Y-%m-%d")

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {"ok": True, "data": {"ok": True, "data": {"records": [], "stats": {}, "warnings": []}}}

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            body=b"platform=all&direction=all&date_from=&date_to=",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Accept": "application/json"},
        )

        app._handle_receipts_sync(handler)

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertTrue(payload["ok"])
        self.assertEqual(today, app.last_call["payload"]["params"]["date_from"])
        self.assertEqual(today, app.last_call["payload"]["params"]["date_to"])

    def test_receipt_yunda_live_proxy_uses_receipt_prefix_and_existing_agent_endpoint(self):
        app = _build_app(_ReceiptRepo())

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "text/html; charset=utf-8"},
                        "body_base64": base64.b64encode(b"<html>receipt</html>").decode("ascii"),
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(headers={"Accept": "text/html"})

        app._handle_yunda_receipt_live_proxy(
            handler,
            "/receipts/yunda/live/ky_inms/public/index.php/business/waybill/mailing/index.html",
            method="GET",
            query={"page": ["tab"]},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("/internal/v1/tms/yunda_waybill_proxy", app.last_call["endpoint"])
        self.assertEqual("/receipts/yunda/live", app.last_call["payload"]["params"]["proxy_prefix"])
        self.assertEqual("/ky_inms/public/index.php/business/waybill/mailing/index.html", app.last_call["payload"]["params"]["path"])

    def test_receipt_ronghui_live_proxy_uses_receipt_prefix_and_existing_agent_endpoint(self):
        app = _build_app(_ReceiptRepo())

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "text/html; charset=utf-8"},
                        "body_base64": base64.b64encode(b"<html>ronghui receipt</html>").decode("ascii"),
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(headers={"Accept": "text/html"})

        app._handle_ronghui_receipt_live_proxy(
            handler,
            "/receipts/ronghui/live",
            method="GET",
            query={"receipt_entry": ["send"]},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("/internal/v1/tms/ronghui_waybill_proxy", app.last_call["endpoint"])
        self.assertEqual("/receipts/ronghui/live", app.last_call["payload"]["params"]["proxy_prefix"])
        self.assertEqual("", app.last_call["payload"]["params"]["path"])
        self.assertEqual("", app.last_call["payload"]["params"]["query"])
        self.assertEqual("寄方回单跟踪", app.last_call["payload"]["params"]["entry_menu_text"])


class ReceiptTemplateTests(unittest.TestCase):
    def test_receipts_template_contains_filter_table_preview_and_audit_modal(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn("data-receipts-page", template)
        self.assertIn("data-receipt-filter-form", template)
        self.assertIn("data-receipt-table", template)
        self.assertIn("data-receipt-thumb-preview", template)
        self.assertIn("data-receipt-thumb-image", template)
        self.assertIn("data-receipt-preview-download", template)
        self.assertIn("data-receipt-download-images", template)
        self.assertIn("data-receipt-review-modal", template)
        self.assertIn("data-receipt-original-open", template)
        self.assertIn("data-receipt-audit-modal", template)
        self.assertIn("data-receipt-audit-frame", template)
        self.assertIn("data-receipt-query-marker", template)
        self.assertIn("data-receipt-date-range", template)
        self.assertIn("data-receipt-date-range-label", template)
        self.assertIn('name="direction" value="send"', template)
        self.assertNotIn('id="receipt-direction"', template)
        self.assertNotIn('<select id="receipt-direction"', template)
        self.assertNotIn("<th>方向</th>", template)
        self.assertNotIn("receipt-col-direction", template)
        self.assertNotIn("directionClassFor", template)
        self.assertNotIn("data-receipt-detail-drawer", template)
        self.assertIn("/receipts/data", template)
        self.assertIn("/receipts/download-images", template)
        self.assertIn("minmax(260px, 1.45fr)", template)
        self.assertIn("max-height: calc(100vh - 260px)", template)
        self.assertNotIn("@media (max-width: 1760px)", template)

    def test_receipts_modal_escapes_revealed_main_transform_and_stays_viewport_bounded(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn("document.body.appendChild(preview)", template)
        self.assertIn("document.body.appendChild(reviewModal)", template)
        self.assertIn("document.body.appendChild(auditModal)", template)
        self.assertIn("calc(100dvh - 48px)", template)
        self.assertNotIn("96vw", template)
        self.assertNotIn("94vw", template)

    def test_receipt_preview_image_fits_preview_window(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            ".receipt-preview-body {",
            "overflow: hidden;",
            ".receipt-preview-stage {",
            "display: flex;",
            "width: 100%;",
            "height: 100%;",
            ".receipt-preview img { display: block; width: 100%; height: 100%; object-fit: contain;",
            "data-receipt-preview-error",
            ".receipt-preview-stage.is-broken",
            "markBrokenThumbnail",
        ):
            self.assertIn(expected, template)
        self.assertNotIn(".receipt-preview-stage {\n    display: grid;\n    place-items: center;\n    min-width: 0;\n    min-height: 0;\n    height: 100%;\n    overflow: auto;", template)

    def test_receipts_date_popover_can_escape_filter_panel(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn(".receipt-filter-panel { position: relative; z-index: 20; overflow: visible; }", template)
        self.assertIn(".receipt-table-panel { position: relative; z-index: 1; overflow: hidden; }", template)
        self.assertNotIn(".receipt-filter-panel { overflow: hidden; }", template)

    def test_receipts_date_range_picker_uses_single_calendar_grid(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn("data-receipt-calendar-grid", template)
        self.assertIn("data-receipt-calendar-prev", template)
        self.assertIn("data-receipt-calendar-next", template)
        self.assertIn("data-receipt-calendar-day", template)
        self.assertIn("handleCalendarDayClick", template)
        self.assertIn("const renderCalendar", template)
        self.assertNotIn('type="date"', template)
        self.assertNotIn("data-receipt-date-from-picker", template)
        self.assertNotIn("data-receipt-date-to-picker", template)

    def test_receipts_query_updates_table_without_full_page_reload(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn("const renderReceiptRows", template)
        self.assertIn("const refreshReceiptTable", template)
        self.assertIn("history.replaceState", template)
        self.assertIn("data-receipt-pagination-actions", template)
        self.assertIn("data-receipt-count-muted", template)
        self.assertNotIn("form.submit()", template)
        self.assertNotIn("data-receipt-active-filters", template)
        self.assertNotIn("receipt-active-filters", template)

    def test_receipts_updated_at_column_shows_full_timestamp(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            ".receipt-table { width: 100%; min-width: 0; table-layout: fixed; }",
            ".receipt-col-updated { width: 16%; }",
            ".receipt-table .receipt-updated-cell",
            "text-overflow: clip;",
            "font-size: .78rem;",
            "<col class=\"receipt-col-updated\">",
            "class=\"receipt-updated-cell\" title=\"{{ row.updated_at }}\"",
            "class=\"receipt-updated-cell\" title=\"${escapeHtml(row.updated_at || \"\")}\"",
        ):
            self.assertIn(expected, template)

    def test_receipts_table_width_does_not_push_filter_panel_offscreen(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            ".receipts-page { display: grid; gap: 16px; width: 100%; min-width: 0; max-width: none; }",
            ".receipt-table-panel { position: relative; z-index: 1; overflow: hidden; }",
            ".receipt-table-wrap { width: 100%; max-width: 100%; min-width: 0; overflow-y: auto; overflow-x: hidden;",
            ".receipt-table { width: 100%; min-width: 0; table-layout: fixed; }",
            ".receipt-col-photo { width: 6%; }",
            ".receipt-col-action { width: 8%; }",
        ):
            self.assertIn(expected, template)

    def test_receipt_sync_status_can_be_dismissed(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "data-receipt-sync-status-text",
            "data-receipt-sync-status-close",
            "const syncStatusText",
            "const syncStatusClose",
            "const setSyncStatus",
            "const hideSyncStatus",
            "syncStatusClose?.addEventListener(\"click\", hideSyncStatus)",
        ):
            self.assertIn(expected, template)
        self.assertNotIn("syncStatus.textContent =", template)

    def test_receipts_successful_query_hides_sync_status_instead_of_green_banner(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        self.assertIn("setSubmitBusy(true)", template)
        self.assertIn("data-receipt-spinner", template)
        self.assertIn("hideSyncStatus();", template)
        self.assertNotIn(".receipt-sync-status.is-success", template)
        self.assertNotIn('"is-success"', template)

    def test_receipts_ronghui_pending_audit_status_uses_warning_chip(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "const isPendingAuditStatus",
            "text.includes(\"待\") && text.includes(\"审核\")",
            "isPendingAuditStatus(value)",
            "'待' in (row.audit_status or '') and '审核' in (row.audit_status or '')",
        ):
            self.assertIn(expected, template)

    def test_receipt_audit_original_page_auto_queries_clicked_number(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "auditQueryPayloadFor",
            "startAuditAutoQuery(record, meta)",
            "{{ ronghui_live_url }}?receipt_entry=send",
            "collectAuditDocuments",
            "applyYundaAuditQuery",
            "applyRonghuiAuditQuery",
            "按运单号查询",
            "按回单号查询",
            "按回单快递单号查询",
            "正在按单号查询",
            "已${result.label || \"按单号\"}查询",
        ):
            self.assertIn(expected, template)
        self.assertNotIn('{{ ronghui_live_url }}/widget/home', template)

    def test_yunda_audit_auto_query_confirms_choice_before_clicking(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "const isChoiceCheckedByText",
            "const isYundaAuditPageReady",
            "const findAuditButton",
            "const yundaQueryLabelFor",
            "const hasVisibleLoadingMask",
            "yunda_page_loading",
            "yunda_page_stabilizing",
            "doc.readyState === \"complete\"",
            "bodyText.includes(\"查询条件\")",
            "bodyText.includes(\"数据列表\")",
            "nextCount >= 4",
            "window.setTimeout(tick, 1000)",
            "const choiceTextContext",
            "const isChoiceInputNode",
            "if (isChoiceInputNode(next)) break;",
            "if (isChoiceInputNode(nextElement)) break;",
            "const layuiChoiceWidgetFor",
            ".layui-form-checkbox,.layui-form-radio",
            "const renderLayuiChoiceState",
            "const isChoiceControlChecked",
            "layui-form-checked",
            "const choiceClickTarget",
            "const clickChoiceControl",
            "const forceClickChoiceControl",
            "const markAuditQueryPrepared",
            "const isAuditQueryPrepared",
            "const wasChoiceReady",
            "const wasInputReady",
            "const wasPrepared",
            "const prepareYundaChoiceForQuery",
            "reason: \"choice_rearming\"",
            "forceClick: mode.active && !wasPrepared",
            "reason: \"choice_confirm_pending\"",
            "reason: \"choice_not_ready\"",
            "if (choiceState.reason === \"choice_confirm_pending\" || !wasChoiceReady || !wasInputReady || !wasPrepared)",
        ):
            self.assertIn(expected, template)

        self.assertLess(template.index("reason: \"choice_confirm_pending\""), template.index("clickAuditButton(doc, \"查询\")"))
        self.assertIn("return controls.find((control) => choiceTextContext(control, labelText).includes(labelText)) || null;", template)
        self.assertIn("if (layuiWidget) return layuiWidget;", template)
        self.assertIn("renderLayuiChoiceState(control);", template)
        self.assertIn("return Boolean(control) && isChoiceControlChecked(control, labelText, checked);", template)
        self.assertIn("let next = control.nextSibling;", template)
        self.assertNotIn("choiceTextContext(control).includes(labelText)", template)
        self.assertNotIn("controls.find((control) => controlTextContext(control).includes(labelText))", template)
        self.assertIn("if (control.checked) {\n        forceClickChoiceControl(control, labelText);\n        return true;\n      }", template)
        self.assertIn("return forceClickChoiceControl(control, labelText);", template)
        self.assertNotIn("control.checked = true;\n        dispatchAuditInputEvents(control);", template)
        self.assertNotIn("if (control.checked) clickChoiceControl(control);\n      if (!control.checked) clickChoiceControl(control);", template)

    def test_ronghui_audit_auto_query_waits_for_page_stability_before_clicking(self):
        template = (CONSOLE_DIR / "templates" / "receipts.html").read_text(encoding="utf-8")

        for expected in (
            "const isRonghuiAuditPageReady",
            "ronghui_page_loading",
            "ronghui_page_stabilizing",
            "const ronghuiAuditReadyKeyFor",
            "const wasPrepared = isAuditQueryPrepared(doc, payload)",
            "reason: \"ronghui_choice_confirm_pending\"",
            "payload.platform === \"ronghui\"",
            "window.setTimeout(tick, 1000)",
        ):
            self.assertIn(expected, template)

        ronghui_body = template[template.index("const applyRonghuiAuditQuery"):]
        self.assertLess(ronghui_body.index("reason: \"ronghui_choice_confirm_pending\""), ronghui_body.index("clickAuditButton(doc, \"查询\")"))


if __name__ == "__main__":
    unittest.main()
