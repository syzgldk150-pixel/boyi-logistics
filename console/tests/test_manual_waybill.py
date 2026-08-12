import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import ActionResult, DocumentService, LocalDocFlowApp, ui_label  # noqa: E402
from database import format_manual_waybill_no  # noqa: E402
from console.navigation import (  # noqa: E402
    CONSOLE_NAVIGATION,
    MOBILE_NAVIGATION_CANDIDATES,
    mobile_bottom_nav_for_user,
)


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"not a PNG file: {path}")
    return struct.unpack(">II", header[16:24])


class _TemplateStore:
    def get_active_template_spec(self):
        return {
            "template_name": "test_template",
            "fields": [
                {"name": "waybill_no", "label": "运单号", "required": True},
                {"name": "open_date", "label": "日期", "required": True},
                {"name": "destination_site", "label": "到站", "required": True},
                {"name": "receiver_name", "label": "收货人", "required": True},
                {"name": "receiver_phone", "label": "收货电话", "required": True},
                {"name": "receiver_address", "label": "收件地址", "required": True},
                {"name": "goods_name_lines", "label": "货物名称", "required": True},
                {"name": "package_type_lines", "label": "包装类型", "required": True},
                {"name": "quantity_lines", "label": "件数", "required": True},
                {"name": "delivery_method", "label": "送货方式", "required": True},
                {"name": "freight_fee", "label": "运费", "required": True},
                {"name": "payment_method", "label": "结算方式", "required": True},
                {"name": "delivery_fee", "label": "送货费", "required": False},
                {"name": "sender_name", "label": "发货人", "required": False},
                {"name": "sender_phone", "label": "发货电话", "required": False},
                {"name": "weight_volume", "label": "重/体积", "required": False},
                {"name": "pickup_fee", "label": "接货费", "required": False},
                {"name": "transfer_fee", "label": "中转费", "required": False},
                {"name": "remark", "label": "备注", "required": False},
            ],
        }


class _Repository:
    def __init__(self):
        self.next_value = 1
        self.created = []
        self.upserted_writers = []

    def create_manual_waybill(self, fields, writer_id=""):
        waybill_no = format_manual_waybill_no(self.next_value)
        self.next_value += 1
        stored = dict(fields)
        stored["waybill_no"] = waybill_no
        self.created.append({"fields": stored, "writer_id": writer_id})
        return len(self.created), waybill_no

    def upsert_writer(self, writer_id, display_name=""):
        self.upserted_writers.append((writer_id, display_name))


def _valid_form(**overrides):
    values = {
        "field_open_date": "20260512",
        "field_destination_site": "杭州余杭",
        "field_receiver_name": "王小明",
        "field_receiver_phone": "13812345678",
        "field_receiver_address": "浙江省杭州市余杭区文一西路969号",
        "field_goods_name_lines": "电子产品",
        "field_package_type_lines": "纸箱",
        "field_quantity_lines": "1件",
        "field_delivery_method": "送货",
        "field_freight_fee": "20",
        "field_delivery_fee": "5.5",
        "field_payment_method": "寄付",
    }
    values.update(overrides)
    return values


class ManualWaybillServiceTests(unittest.TestCase):
    def _service(self, repository):
        return DocumentService(
            SimpleNamespace(),
            repository,
            _TemplateStore(),
            qwen_provider=None,
        )

    def test_sequence_formatter_starts_with_eight_digits(self):
        self.assertEqual("00000001", format_manual_waybill_no(1))
        self.assertEqual("00000123", format_manual_waybill_no(123))

    def test_required_missing_does_not_create_or_occupy_number(self):
        repository = _Repository()
        service = self._service(repository)

        result = service.apply_manual_waybill(_valid_form(field_receiver_name=""))

        self.assertFalse(result.ok)
        self.assertIn("收货人", result.message)
        self.assertEqual([], repository.created)
        self.assertEqual(1, repository.next_value)

    def test_manual_waybill_saves_optional_empty_fields_and_formats_money(self):
        repository = _Repository()
        service = self._service(repository)

        result = service.apply_manual_waybill(
            _valid_form(field_insurance_amount="", field_cod_amount="")
        )

        self.assertTrue(result.ok)
        self.assertEqual("00000001", result.waybill_no)
        fields = repository.created[0]["fields"]
        self.assertEqual("00000001", fields["waybill_no"])
        self.assertEqual("2026/05/12", fields["open_date"])
        self.assertEqual("20.00", fields["freight_fee"])
        self.assertEqual("5.50", fields["delivery_fee"])
        self.assertEqual("", fields["insurance_amount"])
        self.assertEqual("", fields["cod_amount"])

    def test_manual_waybill_accepts_native_date_input_value(self):
        repository = _Repository()
        service = self._service(repository)

        result = service.apply_manual_waybill(_valid_form(field_open_date="2026-05-12"))

        self.assertTrue(result.ok)
        fields = repository.created[0]["fields"]
        self.assertEqual("2026/05/12", fields["open_date"])

    def test_manual_sequence_is_global_not_date_based(self):
        repository = _Repository()
        service = self._service(repository)

        first = service.apply_manual_waybill(_valid_form(field_open_date="20260512"))
        second = service.apply_manual_waybill(_valid_form(field_open_date="20260513"))

        self.assertEqual("00000001", first.waybill_no)
        self.assertEqual("00000002", second.waybill_no)

    def test_invalid_money_is_rejected_before_create(self):
        repository = _Repository()
        service = self._service(repository)

        result = service.apply_manual_waybill(_valid_form(field_freight_fee="二十元"))

        self.assertFalse(result.ok)
        self.assertIn("金额格式无效", result.message)
        self.assertEqual([], repository.created)


class ManualWaybillRouteTests(unittest.TestCase):
    def test_manual_save_without_autoprint_redirects_to_return_to(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        values = {"return_to": "/ocr/boyi/frame"}
        app.service = SimpleNamespace(
            apply_manual_waybill=lambda form_values: ActionResult(
                ok=True,
                message="手工单 00000001 已保存，请打印。",
                waybill_id=1,
                waybill_no="00000001",
            )
        )

        def parse_form(self, handler):
            return values

        def redirect(self, handler, target, message, kind):
            self.redirected = {"target": target, "message": message, "kind": kind}

        app._parse_urlencoded_form = parse_form.__get__(app, LocalDocFlowApp)
        app._redirect_with_message = redirect.__get__(app, LocalDocFlowApp)

        app._handle_manual_waybill(SimpleNamespace())

        self.assertEqual("/ocr/boyi/frame", app.redirected["target"])
        self.assertEqual("success", app.redirected["kind"])

    def test_manual_save_validation_error_redirects_to_return_to(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        values = {"return_to": "/ocr/boyi/frame"}
        app.service = SimpleNamespace(
            apply_manual_waybill=lambda form_values: ActionResult(
                ok=False,
                message="必填字段未填写：收货人",
            )
        )

        def parse_form(self, handler):
            return values

        def redirect(self, handler, target, message, kind):
            self.redirected = {"target": target, "message": message, "kind": kind}

        app._parse_urlencoded_form = parse_form.__get__(app, LocalDocFlowApp)
        app._redirect_with_message = redirect.__get__(app, LocalDocFlowApp)

        app._handle_manual_waybill(SimpleNamespace())

        self.assertEqual("/ocr/boyi/frame", app.redirected["target"])
        self.assertEqual("warning", app.redirected["kind"])

    def test_manual_save_rejects_protocol_relative_return_to(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        values = {"return_to": "//example.test/ocr/boyi/frame"}
        app.service = SimpleNamespace(
            apply_manual_waybill=lambda form_values: ActionResult(
                ok=True,
                message="手工单 00000001 已保存。",
                waybill_id=1,
                waybill_no="00000001",
            )
        )

        def parse_form(self, handler):
            return values

        def redirect(self, handler, target, message, kind):
            self.redirected = {"target": target, "message": message, "kind": kind}

        app._parse_urlencoded_form = parse_form.__get__(app, LocalDocFlowApp)
        app._redirect_with_message = redirect.__get__(app, LocalDocFlowApp)

        app._handle_manual_waybill(SimpleNamespace())

        self.assertEqual("/ocr", app.redirected["target"])
        self.assertEqual("success", app.redirected["kind"])


class ManualWaybillTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        template_dir = CONSOLE_DIR / "templates"
        cls.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html", "xml"]),
        )
        cls.env.globals["ui_label"] = ui_label
        cls.env.globals["current_admin_user"] = lambda: None
        cls.env.globals["console_navigation"] = CONSOLE_NAVIGATION
        cls.env.globals["mobile_navigation_candidates"] = MOBILE_NAVIGATION_CANDIDATES
        cls.env.globals["mobile_navigation_for_user"] = mobile_bottom_nav_for_user

    def test_document_template_defaults_to_manual_submit(self):
        template = self.env.get_template("document.html")
        html = template.render(
            app_title="Test Console",
            document=None,
            fields=[],
            pending_docs=[],
            counts={},
            queue_snapshot={},
            auto_refresh=False,
            ocr_mode=False,
            yunda_mode=False,
            ronghui_mode=False,
            boyi_frame_mode=True,
            message="",
            message_kind="info",
            original_url="",
            processed_url="",
            preprocess_info={},
            preprocess_quality={},
            raw_ocr={},
            available_templates=[],
            active_template_name="test_template",
            document_template_name="test_template",
            settings={},
            writers=[],
            document_writer_id="",
            manual_amap_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            manual_amap_sdk_should_load=False,
            manual_preview_waybill_no="00000001",
        )

        self.assertIn('action="/waybills/manual"', html)
        self.assertIn("运单录入", html)
        self.assertIn("提交后自动生成 00000001", html)
        self.assertIn('id="field_waybill_no"', html)
        self.assertIn('value="00000001"', html)
        self.assertIn("打印预览", html)
        self.assertIn("data-manual-preview", html)
        self.assertIn("地址解析", html)
        self.assertIn("data-address-parser-trigger", html)
        self.assertIn("data-address-parser-dialog", html)
        self.assertIn("field_address_parser_text", html)
        self.assertIn("data-address-parser-error", html)
        self.assertIn("parseReceiverAddressText", html)
        self.assertIn("applyParsedReceiverAddress", html)
        self.assertIn("showAddressParserError", html)
        self.assertIn("无法解析收货电话", html)
        self.assertIn("无法解析收件地址", html)
        self.assertIn('showAddressParserError(parsed.error || "地址解析失败，请手动填写。")', html)
        self.assertNotIn('showManualNotice(parsed.error || "地址解析失败，请手动填写。")', html)
        self.assertIn("z-index: 10000", html)
        self.assertIn("position: relative; z-index: 1;", html)
        self.assertIn("width: min(560px, calc(100vw - 40px))", html)
        self.assertIn("min-height: 96px", html)
        self.assertIn(".address-parser-actions { display: flex; justify-content: center; gap: 12px; padding: 16px 20px 18px; }", html)
        self.assertNotIn("event.target === addressParserDialog", html)
        self.assertIn("field_receiver_name", html)
        self.assertIn("field_receiver_phone", html)
        self.assertIn("field_receiver_address", html)
        self.assertIn("extractDestinationSiteFromAddress", html)
        self.assertIn("cityMatches[cityMatches.length - 1]", html)
        self.assertIn('document.getElementById("field_destination_site")', html)
        self.assertIn("parsed.destination_site", html)
        self.assertNotIn('name="action" value="preview"', html)
        self.assertIn("validateManualRequiredFields", html)
        self.assertIn("必填字段未填写", html)
        self.assertIn("previewManualWaybill", html)
        self.assertIn("manualWaybillPreviewNo", html)
        self.assertNotIn("未保存预览", html)
        self.assertIn("buildManualPreviewHtml", html)
        self.assertIn("applyWaybillLodopTemplate", html)
        self.assertIn("/static/js/waybill_label_html.js", html)
        self.assertIn("/static/js/waybill_label_lodop.js", html)
        self.assertNotIn("/static/js/waybill_label_svg.js", html)
        self.assertIn("WaybillLabelHtml.buildHtml", html)
        self.assertNotIn("WaybillLabelSvg.buildDataUri", html)
        self.assertIn("WaybillLabelLodop.applyTemplate", html)
        self.assertIn("applyWaybillLodopTemplate(lodop", html)
        self.assertNotIn("ADD_PRINT_IMAGE", html)
        self.assertNotIn("ADD_PRINT_TEXT", html)
        self.assertNotIn("ADD_PRINT_HTM", html)
        self.assertIn("SET_PREVIEW_WINDOW", html)
        self.assertIn("SET_PREVIEW_WINDOW(1, 0, 0, 760, 680", html)
        self.assertIn("未连接到 C-Lodop 打印服务", html)
        self.assertIn('const WAYBILL_LABEL_WIDTH = "74mm"', html)
        self.assertIn('const WAYBILL_LABEL_HEIGHT = "92mm"', html)
        self.assertNotIn("renderWaybillLabelDataUrl", html)
        self.assertNotIn("buildLodopWaybillLabelHtml", html)
        self.assertNotIn("ADD_PRINT_HTM", html)
        self.assertNotIn("window.print()", html)
        self.assertIn('name="print_orientation"', html)
        self.assertIn('name="print_offset_x"', html)
        self.assertIn('name="print_offset_y"', html)
        self.assertIn('name="print_font_scale"', html)
        self.assertIn('name="print_template_scale"', html)
        self.assertIn("data-printer-calibration", html)
        self.assertIn("data-client-notice", html)
        self.assertIn('name="auto_print"', html)
        self.assertIn('data-boyi-frame-root', html)
        self.assertIn('name="return_to" value="/ocr/boyi/frame"', html)
        self.assertIn('data-mode-panel="manual"', html)
        self.assertNotIn('class="entry-tabs-shell"', html)
        self.assertNotIn('data-entry-max-tabs="6"', html)
        self.assertNotIn('href="/ocr?mode=ocr"', html)
        self.assertNotIn('data-mode-panel="ocr"', html)
        self.assertIn("打印机设置", html)
        self.assertIn("C-Lodop优先", html)
        self.assertIn("CLodopfuncs.js", html)
        self.assertIn('data-printer-panel hidden', html)
        self.assertNotIn('data-printer-card', html)
        self.assertNotIn("是否弹出地图", html)
        self.assertNotIn("show_map_popup", html)
        self.assertNotIn("data-map-popup-toggle", html)
        self.assertIn("暂无地图信息", html)
        self.assertIn('id="manual-amap-container"', html)
        self.assertIn("window.__manualAmapConfig", html)
        self.assertNotIn("/static/js/amap_route_utils.js", html)
        self.assertNotIn("route-lines", html)
        self.assertNotIn("data-map-status", html)
        self.assertNotIn("data-map-address", html)
        self.assertIn('id="manual-route-origin"', html)
        self.assertIn("data-route-origin-input", html)
        self.assertIn("manual-route-planner--input-only", html)
        self.assertIn('placeholder="输入起始地址"', html)
        self.assertNotIn("基础行程预估", html)
        self.assertNotIn("data-route-result", html)
        self.assertNotIn("data-route-distance", html)
        self.assertNotIn("routeResult.hidden", html)
        self.assertNotIn("AMap.Driving", html)
        self.assertNotIn('name="manual-route-origin"', html)
        self.assertIn("repeat(3, minmax(0, 1fr))", html)
        self.assertIn(".manual-row--span-2 { grid-column: span 2; }", html)
        self.assertIn(".manual-row--span-3 { grid-column: span 3; }", html)
        self.assertIn('id="field_weight_kg"', html)
        self.assertIn('name="field_weight_kg"', html)
        self.assertIn('id="field_volume_m3"', html)
        self.assertIn('name="field_volume_m3"', html)
        self.assertIn('name="field_weight_volume"', html)
        self.assertIn("data-weight-volume-combined", html)
        self.assertIn("data-quote-panel", html)
        self.assertIn(".manual-quote-summary.is-error", html)
        self.assertIn('data-quote-action="calculate"', html)
        self.assertIn('data-quote-provider="yunda"', html)
        self.assertIn('data-quote-provider="ronghui"', html)
        self.assertIn("/waybills/quote-options", html)
        self.assertIn("shipnow.manualQuote.prefill", html)
        self.assertIn("SHIPNOW_PREFILL", html)
        self.assertIn("SHIPNOW_PREFILL_RESULT", html)
        self.assertIn("SHIPNOW_PREFILL_READY", html)
        self.assertIn("scheduleStoredPrefillToFrame", html)
        self.assertIn("prefillSendAttempts", html)
        self.assertIn("prefill_key", html)
        self.assertIn("attempts >= 36", html)
        self.assertIn("prefillFrameSrc", html)
        self.assertIn("_prefill", html)
        self.assertIn("codexManualPrefill", html)
        self.assertIn("helper.run", html)
        self.assertIn('inRonghuiSection("收件信息"', html)
        self.assertIn('inRonghuiSection("货物信息"', html)
        self.assertIn('inRonghuiSection("成本信息"', html)
        self.assertIn('inRonghuiSection("收入信息"', html)
        self.assertIn('"详细地址"', html)
        self.assertIn('"运输方式"', html)
        self.assertIn(".manual-map-card { flex: 0 0 auto;", html)

    def test_document_template_boyi_frame_is_isolated_manual_entry(self):
        template = self.env.get_template("document.html")
        html = template.render(
            app_title="Test Console",
            document=None,
            fields=[],
            pending_docs=[],
            counts={},
            queue_snapshot={},
            auto_refresh=False,
            ocr_mode=False,
            yunda_mode=False,
            ronghui_mode=False,
            boyi_frame_mode=True,
            message="",
            message_kind="info",
            original_url="",
            processed_url="",
            preprocess_info={},
            preprocess_quality={},
            raw_ocr={},
            available_templates=[],
            active_template_name="test_template",
            document_template_name="test_template",
            settings={},
            writers=[],
            document_writer_id="",
            manual_amap_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            manual_amap_sdk_should_load=False,
            manual_preview_waybill_no="00000001",
        )

        self.assertIn('body.boyi-frame-page .sidebar,', html)
        self.assertIn('body.boyi-frame-page .top-header { display: none !important; }', html)
        self.assertIn('body.boyi-frame-page .main-content { grid-column: 1;', html)
        self.assertNotIn('body.boyi-frame-page .topbar { display: none !important; }', html)
        self.assertIn('data-boyi-frame-root', html)
        self.assertIn('action="/waybills/manual"', html)
        self.assertIn('name="return_to" value="/ocr/boyi/frame"', html)
        self.assertNotIn('class="entry-tabs-shell"', html)
        self.assertNotIn('data-entry-max-tabs="6"', html)
        self.assertNotIn('data-entry-add-provider=', html)
        self.assertNotIn('src="/ocr/yunda/live', html)
        self.assertNotIn('src="/ocr/ronghui/live"', html)
        self.assertIn("height: clamp(320px, 42vh, 480px)", html)

    def test_manual_entry_header_uses_compact_controls(self):
        template = self.env.get_template("document.html")
        html = template.render(
            app_title="Test Console",
            document=None,
            fields=[],
            pending_docs=[],
            counts={},
            queue_snapshot={},
            auto_refresh=False,
            ocr_mode=True,
            yunda_mode=False,
            ronghui_mode=False,
            boyi_frame_mode=False,
            message="",
            message_kind="info",
            original_url="",
            processed_url="",
            preprocess_info={},
            preprocess_quality={},
            raw_ocr={},
            available_templates=[],
            active_template_name="test_template",
            document_template_name="test_template",
            settings={},
            writers=[],
            document_writer_id="",
            manual_amap_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            manual_amap_sdk_should_load=False,
            manual_preview_waybill_no="00000001",
        )

        self.assertIn("padding: 10px var(--form-x-pad);", html)
        self.assertIn(".manual-title { display: flex; align-items: center; gap: 8px; flex: 0 0 auto;", html)
        self.assertIn("font-size: .9rem; font-weight: 800; white-space: nowrap;", html)
        self.assertIn(".manual-chip { padding: 4px 8px;", html)
        self.assertIn("font-size: .72rem; font-weight: 800; white-space: nowrap;", html)
        self.assertIn(".manual-head-tools { display: inline-flex; align-items: center; justify-content: flex-end; gap: 8px; row-gap: 8px; flex-wrap: wrap;", html)
        self.assertIn(".mode-switch { display: inline-flex; gap: 4px; padding: 3px;", html)
        self.assertIn("min-height: 26px; padding: 0 8px;", html)
        self.assertIn("font-size: .74rem; font-weight: 800;", html)
        self.assertIn(".printer-trigger { height: 32px; min-height: 32px; padding: 0 10px; border-radius: 8px; font-size: .76rem; font-weight: 800; }", html)
        self.assertIn(".printer-trigger .feather, .mode-switch .feather { width: 15px; height: 15px; }", html)
        self.assertIn(".ocr-template-btn {", html)
        self.assertIn(".ocr-template-btn { height: 44px; min-height: 44px; }", html)

    def test_address_parser_fill_dispatches_events_for_invalid_state_cleanup(self):
        template = self.env.get_template("document.html")
        html = template.render(
            app_title="Test Console",
            document=None,
            fields=[],
            pending_docs=[],
            counts={},
            queue_snapshot={},
            auto_refresh=False,
            ocr_mode=True,
            yunda_mode=False,
            ronghui_mode=False,
            boyi_frame_mode=False,
            message="",
            message_kind="info",
            original_url="",
            processed_url="",
            preprocess_info={},
            preprocess_quality={},
            raw_ocr={},
            available_templates=[],
            active_template_name="test_template",
            document_template_name="test_template",
            settings={},
            writers=[],
            document_writer_id="",
            manual_amap_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            manual_amap_sdk_should_load=False,
            manual_preview_waybill_no="00000001",
        )

        self.assertIn("const setManualFieldValue = (field, value) => {", html)
        self.assertIn('field.dispatchEvent(new Event("input", { bubbles: true }));', html)
        self.assertIn('field.dispatchEvent(new Event("change", { bubbles: true }));', html)
        self.assertIn("setManualFieldValue(nameInput, parsed.receiver_name);", html)
        self.assertIn("setManualFieldValue(phoneInput, parsed.receiver_phone);", html)
        self.assertIn("setManualFieldValue(addressInput, parsed.receiver_address);", html)
        self.assertIn("syncWeightVolumeField", html)
        self.assertIn("setQuoteSummary(error, true)", html)
        self.assertNotIn("showManualNotice(error);", html)
        self.assertIn("selectQuoteProvider", html)
        self.assertIn("setMode(provider)", html)
        self.assertNotIn('fetch("/waybills/manual"', html)
        self.assertNotIn('name="field_route_origin"', html)
        self.assertIn("地图未配置", html)
        self.assertIn("manual-workbench", html)
        self.assertIn("data-ocr-workspace", html)
        self.assertIn('action="/ocr/upload"', html)
        self.assertIn('name="field_open_date"', html)
        self.assertIn('type="date" name="field_open_date"', html)
        self.assertIn('lang="zh-CN" data-open-date-input="true"', html)
        self.assertIn("openDateInput.showPicker", html)
        self.assertIn("toSlashDate", html)
        self.assertNotIn('<div class="manual-section-title">核心信息</div>', html)
        self.assertNotIn('<div class="manual-section-title">客户信息</div>', html)
        self.assertIn("manual-party-stack", html)
        self.assertIn("manual-grid--party", html)
        self.assertIn('manual-subsection-title">发货信息', html)
        self.assertIn('manual-subsection-title">收货信息', html)
        self.assertIn("货物明细", html)
        self.assertIn("费用结算", html)
        self.assertIn("发货人", html)
        self.assertIn("收货人", html)
        self.assertIn("货物名称", html)
        self.assertNotIn("寄方", html)
        self.assertNotIn("收方", html)
        self.assertNotIn("首发分拨", html)
        self.assertNotIn("网点联系方式", html)

    def test_document_template_ocr_mode_restores_full_workspace(self):
        template = self.env.get_template("document.html")
        html = template.render(
            app_title="Test Console",
            document=None,
            fields=[],
            pending_docs=[
                {
                    "id": 7,
                    "original_name": "sample.png",
                    "status": "review_required",
                }
            ],
            counts={},
            queue_snapshot={},
            auto_refresh=False,
            ocr_mode=True,
            message="",
            message_kind="info",
            original_url="",
            processed_url="",
            preprocess_info={},
            preprocess_quality={},
            raw_ocr={},
            available_templates=[],
            active_template_name="test_template",
            document_template_name="test_template",
            settings={},
            writers=[],
            document_writer_id="",
            manual_amap_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            manual_amap_sdk_should_load=False,
        )

        self.assertIn("OCR模式", html)
        self.assertIn('class="ghost-btn ocr-template-btn" href="/templates/new"', html)
        self.assertIn("模板配置", html)
        self.assertIn("上传单据图像", html)
        self.assertIn("待复核 OCR 单据", html)
        self.assertIn("sample.png", html)
        self.assertIn('href="/documents/7"', html)
        self.assertIn('action="/ocr/upload"', html)
        self.assertIn('name="return_to" value="/ocr?mode=ocr"', html)
        self.assertIn("data-ocr-workspace", html)
        self.assertRegex(html, r'data-mode-panel="manual"[^>]*hidden')
        self.assertIn('data-mode-panel="ocr"', html)
        self.assertIn('action="/waybills/manual"', html)
        self.assertIn('name="auto_print"', html)

    def test_template_editor_returns_to_ocr_mode(self):
        editor = (CONSOLE_DIR / "templates" / "template_editor.html").read_text(encoding="utf-8")
        documents_service = (CONSOLE_DIR / "services" / "documents.py").read_text(encoding="utf-8")

        self.assertEqual(2, editor.count('href="/ocr?mode=ocr"'))
        self.assertIn(
            'self._redirect_with_message(handler, "/ocr?mode=ocr", message, "success")',
            documents_service,
        )

    def test_dispatch_template_uses_shared_route_utils(self):
        template = self.env.get_template("dispatch.html")
        html = template.render(
            app_title="Test Console",
            dispatch_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            dispatch_sdk_should_load=False,
            message="",
            message_kind="info",
        )

        self.assertIn("/static/js/amap_route_utils.js", html)
        self.assertIn("基础行程预估", html)
        self.assertIn("各平台运费对比", html)
        self.assertIn("AMapRouteUtils", html)
        self.assertIn("routeUtils.createDrivingService", html)
        self.assertIn("routeUtils.searchDrivingRoute", html)
        self.assertIn("dispatch-est-price", html)

    def test_dispatch_template_does_not_render_icon_font_ligatures(self):
        template = self.env.get_template("dispatch.html")
        html = template.render(
            app_title="Test Console",
            dispatch_config={"amap_js_key": "YOUR_AMAP_JS_API_KEY", "amap_security_code": ""},
            dispatch_sdk_should_load=False,
            message="",
            message_kind="info",
        )

        self.assertNotIn("Material+Symbols", html)
        self.assertNotIn("material-symbols-outlined", html)
        for token in (
            "my_location",
            "swap_vert",
            "flag</span>",
            "local_shipping",
            "airport_shuttle",
            "workspace_premium",
        ):
            self.assertNotIn(token, html)

    def test_print_template_autoprint_has_no_app_shell(self):
        template = self.env.get_template("waybill_print.html")
        html = template.render(
            app_title="Test Console",
            autoprint=True,
            print_preview=False,
            message="",
            message_kind="info",
            waybill={
                "waybill_no": "00000001",
                "open_date": "2026/05/12",
                "destination_site": "杭州余杭",
                "receiver_name": "王小明",
                "receiver_phone": "13812345678",
                "receiver_address": "浙江省杭州市余杭区文一西路969号",
                "sender_name": "李建国",
                "sender_phone": "13987654321",
                "goods_name_lines": "电子产品",
                "package_type_lines": "纸箱",
                "quantity_lines": "1件",
                "weight_volume": "2.50kg",
                "freight_fee": "20.00",
                "delivery_fee": "5.00",
                "payment_method": "寄付",
                "remark": "易碎物品，请轻拿轻放",
                "insurance_amount": "",
                "cod_amount": "",
            },
        )

        self.assertIn("00000001", html)
        self.assertIn("CLodopfuncs.js", html)
        self.assertIn("打印预览", html)
        self.assertIn("previewWaybill()", html)
        self.assertIn("printWaybill()", html)
        self.assertIn("/static/js/waybill_label_html.js", html)
        self.assertIn("/static/js/waybill_label_lodop.js", html)
        self.assertNotIn("/static/js/waybill_label_svg.js", html)
        self.assertIn("WaybillLabelHtml?.renderPreview", html)
        self.assertNotIn("WaybillLabelSvg.buildDataUri", html)
        self.assertIn("WaybillLabelLodop.applyTemplate", html)
        self.assertIn("data-waybill-label-preview", html)
        self.assertIn("applyWaybillLodopTemplate(lodop", html)
        self.assertNotIn("ADD_PRINT_IMAGE", html)
        self.assertNotIn("ADD_PRINT_TEXT", html)
        self.assertNotIn("ADD_PRINT_HTM", html)
        self.assertIn("SET_PREVIEW_WINDOW", html)
        self.assertIn("SET_PREVIEW_WINDOW(1, 0, 0, 760, 680", html)
        self.assertIn("WAYBILL_PRINT_DATA", html)
        self.assertIn("PREVIEW()", html)
        self.assertNotIn("renderWaybillLabelDataUrl", html)
        self.assertNotIn("buildLodopWaybillLabelHtml", html)
        self.assertNotIn("window.print()", html)
        self.assertNotIn("150mm", html)
        self.assertNotIn("fallback", html.lower())
        self.assertNotIn("app-shell", html)

    def test_shared_svg_renderer_uses_fixed_74x92_template(self):
        js = (CONSOLE_DIR / "static" / "js" / "waybill_label_svg.js").read_text(encoding="utf-8")
        svg = (CONSOLE_DIR / "static" / "assets" / "waybill_label_template.svg").read_text(encoding="utf-8")

        self.assertIn('const WIDTH_MM = "74mm"', js)
        self.assertIn('const HEIGHT_MM = "92mm"', js)
        self.assertIn('const SVG_VIEW_BOX = "0 0 74 92"', js)
        self.assertIn('const TEMPLATE_URL = "/static/assets/waybill_label_template.svg"', js)
        self.assertIn("async function buildWaybillLabelSvg", js)
        self.assertIn("normalizeData", js)
        self.assertIn("data-field", svg)
        self.assertIn("data-static=\"ppt-template-slices\"", svg)
        self.assertIn("data-static=\"logo_mark\"", svg)
        self.assertIn("data:image/png;base64", svg)
        self.assertIn('width="74mm"', svg)
        self.assertIn('height="92mm"', svg)
        self.assertIn('viewBox="0 0 74 92"', svg)
        self.assertIn('data-field="waybillNo"', svg)
        self.assertIn('data-field="recipientName"', svg)
        self.assertIn('data-field="senderName"', svg)
        self.assertIn("@page { size: 74mm 92mm; margin: 0; }", js)
        self.assertNotIn("<image", js.lower())
        self.assertNotIn("png-elements", svg)
        self.assertNotIn("remark_fragile", svg)
        self.assertNotIn('width="73.36" height="91.36"', svg)
        self.assertNotIn('stroke="#000" stroke-width="0.42"', svg)
        self.assertNotIn("const pinIcon", js)
        self.assertNotIn("const boxIcon", js)
        self.assertNotIn("const noticeIcon", js)
        self.assertNotIn("王小明", js)
        self.assertNotIn("王小明", svg)
        self.assertNotIn("13812345678", js)
        self.assertNotIn("13812345678", svg)
        self.assertNotIn("YS202505210001", js)
        self.assertNotIn("YS202505210001", svg)

    def test_html_renderer_is_screen_preview_only(self):
        js = (CONSOLE_DIR / "static" / "js" / "waybill_label_html.js").read_text(encoding="utf-8")

        self.assertIn('const WIDTH_MM = "74mm"', js)
        self.assertIn('const HEIGHT_MM = "92mm"', js)
        self.assertIn("74mm×92mm 博益物流主单", js)
        self.assertIn('const BACKGROUND_URL = "/static/assets/waybill_label_background.png"', js)
        background_path = CONSOLE_DIR / "static" / "assets" / "waybill_label_background.png"
        self.assertTrue(background_path.exists())
        self.assertEqual((592, 736), _png_size(background_path))
        self.assertIn("ys-waybill-label", js)
        self.assertIn("ys-waybill-background", js)
        self.assertIn("buildHtml", js)
        self.assertIn("renderPreview", js)
        self.assertIn("formatPhone", js)
        self.assertIn("Arial", js)
        self.assertIn("WaybillLabelHtml", js)
        self.assertIn("waybillNo", js)
        self.assertIn("recipientName", js)
        self.assertIn("recipientPhone", js)
        self.assertIn("service_type", js)
        self.assertIn("recipientAddress", js)
        self.assertIn("senderName", js)
        self.assertIn('{ field: "senderName", x: mm(171), y: mm(197)', js)
        self.assertIn('{ field: "recipientName", x: mm(171), y: mm(289)', js)
        self.assertIn('{ field: "packageFee", x: mm(205), y: mm(508)', js)
        self.assertIn('{ field: "remark", x: mm(66), y: mm(612)', js)
        self.assertNotIn('{ field: "recipientName", x: mm(96), y: mm(270)', js)
        self.assertNotIn('{ field: "senderName", x: mm(382), y: mm(270)', js)
        self.assertIn("cargoName", js)
        self.assertIn("cargo_name", js)
        self.assertIn("package_type", js)
        self.assertIn("freight", js)
        self.assertIn("paymentMethod", js)
        self.assertIn("remark", js)
        self.assertIn("print_offset_x", js)
        self.assertIn("print_offset_y", js)
        self.assertIn("print_font_scale", js)
        self.assertIn("print_template_scale", js)
        self.assertIn("print_orientation", js)
        self.assertNotIn("SET_PRINT_PAGESIZE", js)
        self.assertNotIn("ADD_PRINT_HTM", js)
        self.assertNotIn("ADD_PRINT_IMAGE", js)
        self.assertNotIn("ADD_PRINT_TEXT", js)

    def test_lodop_renderer_uses_native_print_items(self):
        js = (CONSOLE_DIR / "static" / "js" / "waybill_label_lodop.js").read_text(encoding="utf-8")

        self.assertIn('const WIDTH_MM = "74mm"', js)
        self.assertIn('const HEIGHT_MM = "92mm"', js)
        self.assertIn("74mm×92mm 博益物流主单", js)
        self.assertIn("博益物流主单校准页", js)
        self.assertIn('const BACKGROUND_URL = "/static/assets/waybill_label_background.png"', js)
        background_path = CONSOLE_DIR / "static" / "assets" / "waybill_label_background.png"
        self.assertTrue(background_path.exists())
        self.assertEqual((592, 736), _png_size(background_path))
        self.assertIn("WaybillLabelLodop", js)
        self.assertIn("applyTemplate", js)
        self.assertIn("applyCalibration", js)
        self.assertIn("fetch(BACKGROUND_URL", js)
        self.assertIn("loadBackgroundDataUri", js)
        self.assertIn("blobToDataUri", js)
        self.assertIn("formatPhone", js)
        self.assertIn("Arial", js)
        self.assertIn("<img border='0' src='", js)
        self.assertNotIn("width:100%;height:100%", js)
        self.assertIn("FIELD_LAYOUT", js)
        self.assertIn("SET_PRINT_PAGESIZE", js)
        self.assertIn("ADD_PRINT_TEXT", js)
        self.assertIn("ADD_PRINT_LINE", js)
        self.assertIn("ADD_PRINT_IMAGE", js)
        self.assertEqual(1, js.count("ADD_PRINT_IMAGE"))
        self.assertIn("POS_BASEON_PAPER", js)
        self.assertIn("recipientName", js)
        self.assertIn("recipientPhone", js)
        self.assertIn("service_type", js)
        self.assertIn("recipientAddress", js)
        self.assertIn("senderName", js)
        self.assertIn('{ field: "senderName", x: mmValue(171), y: mmValue(197)', js)
        self.assertIn('{ field: "recipientName", x: mmValue(171), y: mmValue(289)', js)
        self.assertIn('{ field: "packageFee", x: mmValue(205), y: mmValue(508)', js)
        self.assertIn('{ field: "remark", x: mmValue(66), y: mmValue(612)', js)
        self.assertNotIn('{ field: "recipientName", x: mmValue(96), y: mmValue(270)', js)
        self.assertNotIn('{ field: "senderName", x: mmValue(382), y: mmValue(270)', js)
        self.assertIn("cargoName", js)
        self.assertIn("cargo_name", js)
        self.assertIn("package_type", js)
        self.assertIn("paymentMethod", js)
        self.assertIn("remark", js)
        self.assertIn('"WordWrap", 0', js)
        self.assertIn("print_template_scale", js)
        self.assertNotIn("ADD_PRINT_HTM", js)
        self.assertNotIn("waybill_label_template.svg", js)
        self.assertNotIn("image[data-static]", js)
        self.assertNotIn("text[data-field]", js)
        self.assertNotIn("waybill_logo_mark.png", js)
        self.assertNotIn("waybill_company_name_cn.png", js)
        self.assertNotIn("BLACK_PIXEL", js)

    def test_print_template_preview_mode_auto_opens_preview(self):
        template = self.env.get_template("waybill_print.html")
        html = template.render(
            app_title="Test Console",
            autoprint=False,
            print_preview=True,
            message="",
            message_kind="info",
            waybill={
                "waybill_no": "00000001",
                "open_date": "2026/05/12",
                "destination_site": "杭州余杭",
                "receiver_name": "王小明",
                "receiver_phone": "13812345678",
                "receiver_address": "浙江省杭州市余杭区文一西路969号",
                "sender_name": "李建国",
                "sender_phone": "13987654321",
                "goods_name_lines": "电子产品",
                "package_type_lines": "纸箱",
                "quantity_lines": "1件",
                "weight_volume": "2.50kg",
                "freight_fee": "20.00",
                "delivery_fee": "5.00",
                "payment_method": "寄付",
                "remark": "",
                "insurance_amount": "",
                "cod_amount": "",
            },
        )

        self.assertIn("window.setTimeout(() => previewWaybill(), 220);", html)
        self.assertNotIn("window.setTimeout(() => printWaybill(), 220);", html)

    def test_notice_default_uses_full_neutral_border(self):
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("border: 1px solid var(--line)", css)
        self.assertNotIn("border-left: 4px solid var(--accent)", css)
        self.assertNotIn("border-left: 4px solid var(--info)", css)


if __name__ == "__main__":
    unittest.main()
