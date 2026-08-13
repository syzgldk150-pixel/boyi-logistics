import io
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import LocalDocFlowApp
from database import DocumentRepository, WAYBILL_SOURCE_LABELS, WAYBILL_STATUS_LABELS, WAYBILL_STATUS_TONES


class _RenderHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass


class _PostHandler(_RenderHandler):
    def __init__(self, values):
        super().__init__()
        from urllib.parse import urlencode

        body = urlencode(values).encode("utf-8")
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)


class _WaybillRepo:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "rows": [],
            "summary": {
                "total": 0,
                "manual_count": 0,
                "ocr_count": 0,
                "fee_total": "0.00",
                "opening_cost_total": "0.00",
                "insurance_total": "0.00",
                "cod_total": "0.00",
                "pickup_payment_total": "0.00",
                "invalid_money_count": 0,
                "latest_created_at": "",
                "latest_open_date": "",
            },
            "pagination": {
                "page": 1,
                "page_size": 50,
                "total": 0,
                "total_pages": 1,
                "offset": 0,
                "has_prev": False,
                "has_next": False,
            },
        }

    def search_waybills(self, filters, *, page, page_size):
        self.calls.append({"filters": dict(filters), "page": page, "page_size": page_size})
        return self.result

    def update_waybill_status(self, waybill_id, status):
        self.calls.append({"waybill_id": waybill_id, "status": status})
        return True


def _waybill_template_defaults():
    return {
        "current_url": "/waybills?q=00000001",
        "status_options": [
            {"value": "all", "label": "全部状态", "tone": "muted"},
            *[
                {"value": value, "label": label, "tone": WAYBILL_STATUS_TONES[value]}
                for value, label in WAYBILL_STATUS_LABELS.items()
            ],
        ],
        "source_options": [
            {"value": "all", "label": "全部来源"},
            *[
                {"value": value, "label": label}
                for value, label in WAYBILL_SOURCE_LABELS.items()
            ],
        ],
        "payment_options": ["", "现付", "寄付", "到付", "提付", "月结"],
        "delivery_options": ["", "送货", "自提", "派送"],
        "sort_options": [
            {"value": "open_date_desc", "label": "按开单日期倒序"},
            {"value": "open_date_asc", "label": "按开单日期正序"},
        ],
    }


def _build_waybill_app(repository):
    app = LocalDocFlowApp.__new__(LocalDocFlowApp)
    app.settings = SimpleNamespace(
        app_title="Test Console",
        agent_base_url="http://agent.test",
        agent_timeout_seconds=30,
    )
    app.repository = repository
    template_dir = Path(__file__).resolve().parents[1] / "templates"
    app.template_env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return app


class WaybillQueryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        cls.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )
        cls.template = cls.env.get_template("waybills.html")

    def test_template_renders_search_summary_and_table(self):
        html = self.template.render(
            app_title="Test Console",
            filters={
                "q": "00000001",
                "date_from": "2026/05/12",
                "date_to": "2026/05/13",
                "status": "in_transit",
                "source": "all",
                "payment_method": "",
                "delivery_method": "",
                "sort": "open_date_desc",
            },
            rows=[
                {
                    "id": 1,
                    "waybill_no": "00000001",
                    "open_date": "2026/05/12",
                    "created_at": "2026-05-12 10:00:00",
                    "updated_at": "2026-05-12 10:00:00",
                    "destination_site": "杭州余杭",
                    "receiver_name": "王小明",
                    "receiver_phone": "13812345678",
                    "receiver_address": "浙江省杭州市余杭区",
                    "sender_name": "李建国",
                    "sender_phone": "13987654321",
                    "goods_name_lines": "电子产品",
                    "package_type_lines": "纸箱",
                    "quantity_lines": "1件",
                    "weight_volume": "2.50kg",
                    "freight_fee": "20.00",
                    "pickup_fee": "",
                    "delivery_fee": "5.00",
                    "transfer_fee": "",
                    "payment_method": "寄付",
                    "delivery_method": "送货",
                    "insurance_amount": "",
                    "cod_amount": "",
                    "remark": "",
                    "source_label": "手工",
                    "status": "in_transit",
                    "status_label": "运输中",
                    "status_tone": "info",
                    "opening_cost": "20.00",
                    "pickup_payment_amount": "0.00",
                    "print_url": "/waybills/1/print",
                    "tracking_url": "/tracking?tracking_number=00000001",
                }
            ],
            summary={
                "total": 1,
                "manual_count": 1,
                "ocr_count": 0,
                "fee_total": "25.00",
                "opening_cost_total": "20.00",
                "insurance_total": "0.00",
                "cod_total": "0.00",
                "pickup_payment_total": "0.00",
                "invalid_money_count": 0,
                "latest_created_at": "2026-05-12 10:00:00",
                "latest_open_date": "2026/05/12",
            },
            pagination={
                "page": 1,
                "page_size": 50,
                "total": 1,
                "total_pages": 1,
                "offset": 0,
                "has_prev": False,
                "has_next": False,
            },
            prev_url="",
            next_url="",
            db_error="",
            message="",
            message_kind="info",
            **_waybill_template_defaults(),
        )

        self.assertIn("共 1 条", html)
        self.assertIn("00000001", html)
        self.assertIn("data-date-range-picker", html)
        self.assertIn('type="hidden" name="date_from"', html)
        self.assertIn("data-calendar-grid", html)
        self.assertIn('data-date-quick-range="1"', html)
        self.assertIn('data-date-quick-range="3"', html)
        self.assertIn('data-date-quick-range="7"', html)
        self.assertIn("今天", html)
        self.assertIn("3天", html)
        self.assertIn("7天", html)
        self.assertNotIn("commitRange(rangeStart, rangeEnd, { submit: true })", html)
        self.assertIn("commitRange(rangeStart, rangeEnd);", html)
        self.assertNotIn("waybill-summary-strip", html)
        self.assertIn("waybill-detail-dialog", html)
        self.assertIn("data-waybill-detail-template", html)
        self.assertIn("waybill-status-pill", html)
        self.assertNotIn('type="date" name="date_from"', html)
        self.assertIn("运单号 / 电话 / 收货人 / 发货人", html)
        self.assertIn("开单日期", html)
        self.assertIn("开单成本", html)
        self.assertIn("¥20.00", html)
        self.assertIn("提付金额", html)
        self.assertIn("运输中", html)
        self.assertIn("更多筛选", html)
        self.assertIn("列设置", html)
        self.assertIn("作废运单", html)
        self.assertIn("/waybills/1/print", html)
        self.assertIn("/tracking?tracking_number=00000001", html)
        self.assertNotIn("waybill-filter-tools", html)
        self.assertNotIn("15天", html)
        self.assertNotIn("1个月", html)
        self.assertNotIn("运单号 / 客户 / 网点 / 地址", html)
        self.assertNotIn("保价金额", html)
        self.assertNotIn("导出当前页", html)

    def test_detail_opens_as_centered_modal_not_right_drawer(self):
        template_text = (Path(__file__).resolve().parents[1] / "templates" / "waybills.html").read_text(encoding="utf-8")

        self.assertIn("data-waybill-detail-dialog", template_text)
        self.assertIn('role="dialog"', template_text)
        self.assertNotIn("<aside class=\"waybill-detail-panel\"", template_text)
        self.assertNotIn("data-waybill-detail-panel", template_text)

    def test_selected_state_uses_page_accent_not_hardcoded_blue(self):
        template_text = (Path(__file__).resolve().parents[1] / "templates" / "waybills.html").read_text(encoding="utf-8")

        self.assertNotIn("#3b82f6", template_text)
        self.assertNotIn("#2563eb", template_text)
        self.assertNotIn("#dbeafe", template_text)

    def test_status_pill_prefers_scan_status_abbreviation(self):
        html = self.template.render(
            app_title="Test Console",
            filters={
                "q": "00000001",
                "date_from": "",
                "date_to": "",
                "status": "all",
                "source": "all",
                "payment_method": "",
                "delivery_method": "",
                "sort": "open_date_desc",
            },
            rows=[
                {
                    "id": 1,
                    "waybill_no": "00000001",
                    "open_date": "2026/05/12",
                    "created_at": "2026-05-12 10:00:00",
                    "receiver_name": "王小明",
                    "receiver_phone": "13812345678",
                    "receiver_address": "浙江省杭州市余杭区",
                    "sender_name": "李建国",
                    "sender_phone": "13987654321",
                    "goods_name_lines": "电子产品",
                    "package_type_lines": "纸箱",
                    "quantity_lines": "1件",
                    "weight_volume": "2.50kg",
                    "delivery_fee": "5.00",
                    "payment_method": "寄付",
                    "remark": "",
                    "source_label": "融辉寄件",
                    "status": "in_transit",
                    "status_label": "运输中",
                    "status_tone": "info",
                    "scan_status": "发件扫描",
                    "scan_status_short": "发件",
                    "opening_cost": "20.00",
                    "pickup_payment_amount": "0.00",
                    "print_url": "/waybills/1/print",
                    "tracking_url": "/tracking?tracking_number=00000001",
                }
            ],
            summary={
                "total": 1,
                "manual_count": 0,
                "ocr_count": 0,
                "fee_total": "20.00",
                "opening_cost_total": "20.00",
                "insurance_total": "0.00",
                "cod_total": "0.00",
                "pickup_payment_total": "0.00",
                "invalid_money_count": 0,
                "latest_created_at": "2026-05-12 10:00:00",
                "latest_open_date": "2026/05/12",
            },
            pagination={
                "page": 1,
                "page_size": 50,
                "total": 1,
                "total_pages": 1,
                "offset": 0,
                "has_prev": False,
                "has_next": False,
            },
            prev_url="",
            next_url="",
            db_error="",
            message="",
            message_kind="info",
            **_waybill_template_defaults(),
        )

        self.assertIn(">发件</span>", html)
        self.assertIn('title="发件扫描"', html)
        self.assertNotIn(">运输中</span>", html)


class WaybillQueryAjaxTemplateTests(unittest.TestCase):
    def test_template_wires_ajax_search_panel(self):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        template = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        ).get_template("waybills.html")
        html = template.render(
            app_title="Test Console",
            filters={
                "q": "",
                "date_from": "",
                "date_to": "",
                "status": "all",
                "source": "all",
                "payment_method": "",
                "delivery_method": "",
                "sort": "open_date_desc",
            },
            rows=[],
            summary={
                "total": 0,
                "manual_count": 0,
                "ocr_count": 0,
                "fee_total": "0.00",
                "opening_cost_total": "0.00",
                "insurance_total": "0.00",
                "cod_total": "0.00",
                "pickup_payment_total": "0.00",
                "invalid_money_count": 0,
                "latest_created_at": "",
                "latest_open_date": "",
            },
            pagination={
                "page": 1,
                "page_size": 50,
                "total": 0,
                "total_pages": 1,
                "offset": 0,
                "has_prev": False,
                "has_next": False,
            },
            prev_url="",
            next_url="",
            db_error="",
            message="",
            message_kind="info",
            **_waybill_template_defaults(),
        )

        self.assertIn("data-waybill-ajax-panel", html)
        self.assertIn("data-waybill-filter-form", html)
        self.assertIn("WaybillAjaxSearch", html)
        self.assertIn("fetch(url.href", html)


class WaybillQueryRenderTests(unittest.TestCase):
    def test_empty_filter_page_does_not_query_all_waybills(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()

        app._render_waybills(handler, {})

        self.assertEqual([], repository.calls)
        self.assertEqual(200, handler.status)
        html = handler.wfile.getvalue().decode("utf-8")
        self.assertNotIn("waybill-summary-strip", html)

    def test_empty_filter_page_defaults_date_inputs_to_today_without_querying(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()

        with patch("console.services.waybills_receipts.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 6, 22, 9, 30, 0)
            app._render_waybills(handler, {})

        self.assertEqual([], repository.calls)
        html = handler.wfile.getvalue().decode("utf-8")
        self.assertIn('name="date_from" value="2026-06-22"', html)
        self.assertIn('name="date_to" value="2026-06-22"', html)

    def test_date_range_get_is_read_only_and_queries_persisted_snapshot(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()
        agent_calls = []

        def fake_agent_request(method, endpoint, *, payload=None, timeout=None):
            agent_calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            self.fail("waybill GET must not call Agent execution")

        app._agent_request = fake_agent_request

        app._render_waybills(
            handler,
            {
                "date_from": ["2026-05-12"],
                "date_to": ["2026-05-13"],
                "source": ["all"],
                "page_size": ["50"],
            },
        )

        self.assertEqual([], agent_calls)
        self.assertEqual(1, len(repository.calls))
        self.assertEqual("2026/05/12", repository.calls[0]["filters"]["date_from"])
        self.assertEqual("2026/05/13", repository.calls[0]["filters"]["date_to"])
        self.assertIn("GET 查询不会刷新外部来源", handler.wfile.getvalue().decode("utf-8"))

    def test_date_filter_renders_read_only_snapshot_notice(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()

        app._agent_request = lambda *args, **kwargs: self.fail("GET must remain read-only")

        app._render_waybills(
            handler,
            {
                "date_from": ["2026-06-22"],
                "date_to": ["2026-06-22"],
                "source": ["yunda"],
            },
        )

        html = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("GET 查询不会刷新外部来源", html)

    def test_keyword_filter_queries_waybills(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()
        app._agent_request = lambda *args, **kwargs: self.fail("agent should not be called without a date range")

        app._render_waybills(
            handler,
            {
                "q": ["2003441361"],
                "page_size": ["50"],
                "source": ["yunda"],
                "payment_method": ["到付"],
                "delivery_method": ["送货"],
            },
        )

        self.assertEqual(1, len(repository.calls))
        self.assertEqual(
            {
                "q": "2003441361",
                "date_from": "",
                "date_to": "",
                "status": "all",
                "source": "yunda",
                "payment_method": "到付",
                "delivery_method": "送货",
                "sort": "open_date_desc",
            },
            repository.calls[0]["filters"],
        )
        self.assertEqual(1, repository.calls[0]["page"])
        self.assertEqual(50, repository.calls[0]["page_size"])

    def test_sort_param_does_not_query_by_itself(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()

        app._render_waybills(
            handler,
            {
                "sort": ["open_date_asc"],
            },
        )

        self.assertEqual([], repository.calls)
        self.assertEqual(200, handler.status)

    def test_status_filter_queries_waybills(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()

        app._render_waybills(handler, {"status": ["signed"]})

        self.assertEqual(1, len(repository.calls))
        self.assertEqual("signed", repository.calls[0]["filters"]["status"])

    def test_source_filter_does_not_refresh_selected_provider_from_get(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()
        agent_calls = []

        def fake_agent_request(method, endpoint, *, payload=None, timeout=None):
            agent_calls.append(endpoint)
            self.fail("GET must remain read-only")

        app._agent_request = fake_agent_request

        app._render_waybills(
            handler,
            {
                "date_from": ["2026-05-12"],
                "date_to": ["2026-05-12"],
                "source": ["yunda"],
            },
        )

        self.assertEqual([], agent_calls)

    def test_manual_source_does_not_call_external_refresh(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()
        app._agent_request = lambda *args, **kwargs: self.fail("manual source should not refresh external providers")

        app._render_waybills(
            handler,
            {
                "date_from": ["2026-05-12"],
                "date_to": ["2026-05-12"],
                "source": ["manual"],
            },
        )

        self.assertEqual(1, len(repository.calls))

    def test_long_date_range_skips_external_refresh_and_still_queries_local(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()
        app._agent_request = lambda *args, **kwargs: self.fail("long date range should not refresh external providers")

        app._render_waybills(
            handler,
            {
                "date_from": ["2026-05-01"],
                "date_to": ["2026-06-15"],
            },
        )

        self.assertEqual(1, len(repository.calls))
        html = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("GET 查询不会刷新外部来源", html)

    def test_agent_unavailability_is_irrelevant_to_read_only_waybill_get(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _RenderHandler()

        def fake_agent_request(method, endpoint, *, payload=None, timeout=None):
            self.fail("GET must not call Agent")

        app._agent_request = fake_agent_request

        app._render_waybills(
            handler,
            {
                "date_from": ["2026-05-12"],
                "date_to": ["2026-05-12"],
                "source": ["ronghui"],
            },
        )

        self.assertEqual(1, len(repository.calls))
        html = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("GET 查询不会刷新外部来源", html)

    def test_keyword_where_only_searches_allowed_identity_fields(self):
        repository = DocumentRepository.__new__(DocumentRepository)
        repository.placeholder = "%s"

        where_sql, params = repository._build_waybill_search_where({"q": "王小明"})

        self.assertIn("waybill_no LIKE", where_sql)
        self.assertIn("receiver_name LIKE", where_sql)
        self.assertIn("receiver_phone LIKE", where_sql)
        self.assertIn("sender_name LIKE", where_sql)
        self.assertIn("sender_phone LIKE", where_sql)
        self.assertNotIn("destination_site LIKE", where_sql)
        self.assertNotIn("receiver_address LIKE", where_sql)
        self.assertNotIn("goods_name_lines LIKE", where_sql)
        self.assertEqual(["%王小明%"] * 5, params)

    def test_where_filters_status_source_payment_and_delivery(self):
        repository = DocumentRepository.__new__(DocumentRepository)
        repository.placeholder = "%s"

        where_sql, params = repository._build_waybill_search_where(
            {
                "status": "signed",
                "source": "ronghui",
                "payment_method": "到付",
                "delivery_method": "送货",
            }
        )

        self.assertIn("status = %s", where_sql)
        self.assertIn("source = %s", where_sql)
        self.assertIn("payment_method = %s", where_sql)
        self.assertIn("delivery_method = %s", where_sql)
        self.assertEqual(["signed", "ronghui", "到付", "送货"], params)

    def test_where_normalizes_waybill_date_bounds_to_iso(self):
        repository = DocumentRepository.__new__(DocumentRepository)
        repository.placeholder = "%s"

        where_sql, params = repository._build_waybill_search_where(
            {"date_from": "2026/06/22", "date_to": "2026/06/22"}
        )

        self.assertIn("open_date >= %s", where_sql)
        self.assertIn("open_date <= %s", where_sql)
        self.assertEqual(["2026-06-22", "2026-06-22"], params)

    def test_order_clause_is_allowlisted(self):
        repository = DocumentRepository.__new__(DocumentRepository)

        self.assertEqual("open_date ASC, created_at ASC, id ASC", repository._waybill_order_clause({"sort": "open_date_asc"}))
        self.assertEqual("open_date DESC, created_at DESC, id DESC", repository._waybill_order_clause({"sort": "DROP TABLE"}))

    def test_waybill_status_update_redirects_to_current_query(self):
        repository = _WaybillRepo()
        app = _build_waybill_app(repository)
        handler = _PostHandler(
            {
                "status": "cancelled",
                "return_to": "/waybills?q=R001&status=in_transit",
            }
        )

        app._handle_waybill_status_update(handler, 12)

        self.assertEqual(303, handler.status)
        self.assertIn(
            ("Location", "/waybills?q=R001&status=in_transit&message=%E8%BF%90%E5%8D%95%E5%B7%B2%E4%BD%9C%E5%BA%9F%E3%80%82&kind=success"),
            handler.sent_headers,
        )
        self.assertIn({"waybill_id": 12, "status": "cancelled"}, repository.calls)


class WaybillQuerySummaryTests(unittest.TestCase):
    def test_summary_uses_source_specific_cost_and_pickup_payment_totals(self):
        repository = DocumentRepository.__new__(DocumentRepository)

        summary = repository._build_waybill_search_summary(
            [
                {
                    "source": "ronghui",
                    "created_at": "2026-05-12 10:00:00",
                    "open_date": "2026/05/12",
                    "freight_fee": "20",
                    "transfer_fee": "500",
                    "payment_method": "到付",
                    "cod_amount": "8",
                },
                {
                    "source": "yunda",
                    "created_at": "2026-05-13 10:00:00",
                    "open_date": "2026/05/13",
                    "freight_fee": "115",
                    "transfer_fee": "81.85",
                    "payment_method": "到付",
                    "cod_amount": "",
                },
                {
                    "source": "manual",
                    "created_at": "2026-05-14 10:00:00",
                    "open_date": "2026/05/14",
                    "freight_fee": "2.335",
                    "transfer_fee": "",
                    "payment_method": "寄付",
                    "cod_amount": "invalid",
                },
                {
                    "source": "ocr",
                    "created_at": "2026-05-15 10:00:00",
                    "open_date": "2026/05/15",
                    "freight_fee": "bad",
                    "transfer_fee": "",
                    "payment_method": "",
                    "cod_amount": "",
                },
            ],
            total=4,
        )

        self.assertEqual(4, summary["total"])
        self.assertEqual(1, summary["manual_count"])
        self.assertEqual(1, summary["ocr_count"])
        self.assertEqual("104.19", summary["opening_cost_total"])
        self.assertEqual("104.19", summary["fee_total"])
        self.assertEqual("123.00", summary["pickup_payment_total"])
        self.assertEqual("123.00", summary["cod_total"])
        self.assertEqual("0.00", summary["insurance_total"])
        self.assertEqual(1, summary["invalid_money_count"])

    def test_row_to_waybill_adds_display_amounts(self):
        repository = DocumentRepository.__new__(DocumentRepository)

        row = repository._row_to_waybill(
            {
                "id": 9,
                "source": "yunda",
                "waybill_no": "978284775",
                "created_at": "2026-05-15 10:00:00",
                "updated_at": "2026-05-15 10:00:00",
                "freight_fee": "115.00",
                "transfer_fee": "81.85",
                "payment_method": "到付",
                "cod_amount": "",
                "status": "signed",
            }
        )

        self.assertEqual("韵达寄件", row["source_label"])
        self.assertEqual("已签收", row["status_label"])
        self.assertEqual("81.85", row["opening_cost"])
        self.assertEqual("115.00", row["pickup_payment_amount"])

    def test_row_to_waybill_adds_scan_status_abbreviation(self):
        repository = DocumentRepository.__new__(DocumentRepository)

        row = repository._row_to_waybill(
            {
                "id": 10,
                "source": "ronghui",
                "waybill_no": "R0001",
                "created_at": "2026-05-15 10:00:00",
                "updated_at": "2026-05-15 10:00:00",
                "freight_fee": "20.00",
                "transfer_fee": "",
                "payment_method": "寄付",
                "cod_amount": "",
                "status": "in_transit",
                "scan_status": "发件扫描",
            }
        )

        self.assertEqual("发件", row["scan_status_short"])
        self.assertEqual("发件扫描", row["scan_status"])


if __name__ == "__main__":
    unittest.main()
