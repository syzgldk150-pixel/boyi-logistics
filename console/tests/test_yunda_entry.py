import base64
import io
import json
import re
import sys
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


CONSOLE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = CONSOLE_DIR.parent
AGENT_DIR = WORKSPACE_DIR / "agent"
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))

from app import LocalDocFlowApp  # noqa: E402
from shared.yunda_console_waybill import build_console_waybill_from_yunda_data  # noqa: E402


class _Handler:
    def __init__(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)


class _LiveHandler:
    def __init__(self, body=b"", headers=None):
        self.headers = {
            "Content-Length": str(len(body)),
            **(headers or {}),
        }
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


class _Repository:
    def __init__(self):
        self.upserts = []
        self.snapshots = []
        self.waybills_by_no = {}

    def upsert_provider_waybill(self, payload, *, source, writer_id=""):
        record = {"id": len(self.upserts) + 101, **payload, "source": source}
        self.upserts.append({"payload": payload, "source": source, "writer_id": writer_id})
        self.waybills_by_no[payload["waybill_no"]] = record
        return record

    def create_waybill_provider_snapshot(
        self,
        *,
        provider,
        remote_waybill_no="",
        snapshot_kind,
        payload,
        waybill_id=None,
    ):
        self.snapshots.append(
            {
                "provider": provider,
                "remote_waybill_no": remote_waybill_no,
                "snapshot_kind": snapshot_kind,
                "payload": payload,
                "waybill_id": waybill_id,
            }
        )
        return len(self.snapshots)

    def get_waybill_by_no(self, waybill_no, *, source=None):
        return self.waybills_by_no.get(waybill_no)


class YundaEntryTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(
            loader=FileSystemLoader(CONSOLE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def test_document_template_renders_yunda_initial_entry_tab(self):
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
            yunda_mode=True,
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
            ui_label=lambda value: value,
        )

        self.assertIn('data-entry-tabs-root', html)
        self.assertIn('class="ocr-page no-scroll entry-tabs-page"', html)
        self.assertIn("/static/js/yunda_entry_mode.js", html)
        self.assertIn('data-entry-initial-provider="yunda"', html)
        self.assertIn('data-entry-id="entry-1"', html)
        self.assertIn('data-entry-provider="yunda"', html)
        self.assertIn('data-yunda-root', html)
        self.assertNotIn('data-yunda-side-root', html)
        self.assertIn('韵达录单', html)
        self.assertIn('韵达 1', html)
        self.assertIn('data-yunda-live-frame', html)
        self.assertIn('min-width: 1280px', html)
        self.assertIn('body.entry-tabs-page .sidebar { display: flex !important; }', html)
        self.assertIn('/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&amp;p=nil', html)
        self.assertIn('data-entry-add-provider="boyi"', html)
        self.assertIn('data-entry-add-provider="ronghui"', html)
        self.assertIn('data-entry-src-ronghui="/ocr/ronghui/live"', html)
        self.assertNotIn('data-mode-panel="yunda"', html)
        self.assertNotIn(' src="/ocr/ronghui/live"', html)

    def test_document_template_renders_multi_entry_tab_shell_for_yunda(self):
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
            yunda_mode=True,
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
            ui_label=lambda value: value,
        )

        self.assertIn('data-entry-tabs-root', html)
        self.assertIn('data-entry-tab-list', html)
        self.assertIn('data-entry-frame-stack', html)
        self.assertIn('data-entry-max-tabs="6"', html)
        self.assertIn('data-entry-single="true"', html)
        self.assertIn('data-entry-add-provider="boyi"', html)
        self.assertIn('data-entry-add-provider="yunda"', html)
        self.assertIn('data-entry-add-provider="ronghui"', html)
        self.assertIn('data-entry-ocr-link href="/ocr?mode=ocr"', html)
        self.assertIn('data-entry-initial-provider="yunda"', html)
        self.assertIn('/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&amp;p=nil', html)
        self.assertNotIn('data-mode-panel="yunda"', html)

    def test_document_template_renders_ronghui_initial_entry_tab(self):
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
            ronghui_mode=True,
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
            ui_label=lambda value: value,
        )

        self.assertIn('data-entry-tabs-root', html)
        self.assertIn('class="ocr-page no-scroll entry-tabs-page"', html)
        self.assertIn('data-entry-initial-provider="ronghui"', html)
        self.assertIn('data-entry-id="entry-1"', html)
        self.assertIn('data-entry-provider="ronghui"', html)
        self.assertIn('data-ronghui-root', html)
        self.assertIn('data-ronghui-live-frame', html)
        self.assertIn('src="/ocr/ronghui/live"', html)
        self.assertIn('body.entry-tabs-page .sidebar { display: flex !important; }', html)
        self.assertIn('data-entry-add-provider="boyi"', html)
        self.assertIn('data-entry-add-provider="yunda"', html)
        self.assertIn(
            'data-entry-src-yunda="/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&amp;p=nil"',
            html,
        )
        self.assertNotIn('data-mode-panel="ronghui"', html)
        self.assertNotIn(' src="/ocr/yunda/live', html)

    def test_document_template_mode_switch_supports_multi_open_tabs(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")

        self.assertIn("const ENTRY_MAX_TABS = 6", template)
        self.assertIn("function createEntryTab", template)
        self.assertIn("function activateEntryTab", template)
        self.assertIn("function closeEntryTab", template)
        self.assertIn('entryTabsRoot.dataset.entrySingle = tabCount === 1 ? "true" : "false"', template)
        self.assertIn("function addEntryTab", template)
        self.assertIn("entryTabsRoot.dataset.entryInitialProvider", template)
        self.assertIn("provider === \"boyi\" ? \"/ocr/boyi/frame\"", template)
        self.assertIn("provider === \"ronghui\" ? \"/ocr/ronghui/live\"", template)
        self.assertIn("provider === \"yunda\" ? \"/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html?page=tab&p=nil\"", template)

    def test_document_template_mode_switch_supports_ronghui_mode(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")

        self.assertIn('["manual", "ocr", "yunda", "ronghui"].includes(mode)', template)
        self.assertIn('params.get("mode") === "ronghui" ? "ronghui"', template)
        self.assertIn("const loadModeLiveFrame", template)
        self.assertIn("loadModeLiveFrame(nextMode)", template)
        self.assertIn("scheduleStoredPrefillToFrame(nextMode)", template)
        self.assertIn('const PREFILL_STORAGE_KEY = "shipnow.manualQuote.prefill"', template)
        self.assertIn("const prefillReadyModes = new Set()", template)
        self.assertIn('const requiresPrefillReady = (mode) => mode === "ronghui" || mode === "yunda"', template)
        self.assertIn("prefillReadyModes.add(data.provider)", template)
        self.assertIn("resetPrefillReady(mode)", template)
        self.assertIn("if (requiresPrefillReady(mode) && !prefillReadyModes.has(mode)) return false;", template)
        self.assertIn('key: "destination_site"', template)
        self.assertIn('"目的地"', template)

    def test_live_frontend_binds_all_ronghui_and_yunda_instances(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn('document.querySelectorAll("[data-ronghui-root]")', script)
        self.assertIn('ronghuiRoot.querySelector("[data-ronghui-status-chip]")', script)
        self.assertIn('ronghuiRoot.querySelector("[data-ronghui-live-frame]")', script)
        self.assertIn('document.querySelectorAll("[data-yunda-root]")', script)
        self.assertIn('yundaRoot.querySelector("[data-yunda-status-chip]")', script)
        self.assertIn("initAllRonghuiLiveInstances", script)
        self.assertIn("initAllYundaLiveInstances", script)

    def test_document_template_mode_switch_order_uses_business_labels(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        switches = re.findall(
            r'<div class="mode-switch" aria-label="Entry Mode">(.*?)</div>',
            template,
            flags=re.S,
        )

        self.assertEqual(4, len(switches))
        for switch in switches:
            labels = re.findall(r'<a [^>]*data-mode-link="([^"]+)"[^>]*>(.*?)</a>', switch, flags=re.S)
            compact_labels = [(mode, re.sub(r"<[^>]+>", "", label).strip()) for mode, label in labels]
            self.assertEqual(
                [
                    ("manual", "\u535a\u76ca"),
                    ("yunda", "\u97f5\u8fbe"),
                    ("ronghui", "\u878d\u8f89"),
                    ("ocr", "OCR"),
                ],
                compact_labels,
            )

    def test_ronghui_live_frontend_marks_original_page_and_auth_errors(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn("function initRonghuiLiveInstance", script)
        self.assertIn("function initAllRonghuiLiveInstances", script)
        self.assertIn('document.querySelectorAll("[data-ronghui-root]")', script)
        self.assertIn('ronghuiRoot.querySelector("[data-ronghui-status-chip]")', script)
        self.assertIn('ronghuiRoot.querySelector("[data-ronghui-live-frame]")', script)
        self.assertIn('ronghuiRoot.querySelector("[data-ronghui-live-fallback]")', script)
        self.assertIn("原页模式", script)
        self.assertIn("AUTH_REQUIRED", script)
        self.assertIn("ronghuiSessionUrl", script)

    def test_original_page_proxies_include_manual_prefill_listener(self):
        yunda_proxy = (AGENT_DIR / "agent" / "tms_runtime" / "scripts" / "yunda_waybill_proxy.py").read_text(
            encoding="utf-8"
        )
        ronghui_proxy = (AGENT_DIR / "agent" / "tms_runtime" / "scripts" / "ronghui_waybill_proxy.py").read_text(
            encoding="utf-8"
        )

        for script in (yunda_proxy, ronghui_proxy):
            self.assertIn("SHIPNOW_PREFILL", script)
            self.assertIn("SHIPNOW_PREFILL_RESULT", script)
            self.assertIn("codexManualPrefill", script)
            self.assertIn("setMiniValue", script)
            self.assertNotIn("triggerSave", script)
            self.assertNotIn("submitWaybill", script)

        self.assertIn("codex-yunda-prefill-script", yunda_proxy)
        self.assertIn("codex-yunda-local-print-script", yunda_proxy)
        self.assertIn("shipnow_autoprint_url", yunda_proxy)
        self.assertIn("XMLHttpRequest.prototype.open", yunda_proxy)
        self.assertIn("codex-ronghui-prefill-script", ronghui_proxy)
        self.assertIn("function sectionElements", ronghui_proxy)
        self.assertIn("findFieldNearLabel(name, spec.section)", ronghui_proxy)
        self.assertIn("function labelTextMatches", ronghui_proxy)
        self.assertIn("parentElement.nextElementSibling", ronghui_proxy)
        self.assertIn("SHIPNOW_PREFILL_READY", ronghui_proxy)
        self.assertIn("startPrefill", ronghui_proxy)
        self.assertIn("function isRonghuiPrefillReady", ronghui_proxy)
        self.assertIn('document.readyState !== "complete"', ronghui_proxy)
        self.assertIn("function dismissStartupNotice", ronghui_proxy)
        self.assertIn("function waitForRonghuiPrefillReady", ronghui_proxy)
        self.assertIn("notifyPrefillReadyWhenReady", ronghui_proxy)
        self.assertIn("attempt < 80", ronghui_proxy)
        self.assertIn("activePrefillKey", ronghui_proxy)
        self.assertIn("same payload", ronghui_proxy)

    def test_yunda_prefill_waits_for_original_page_ready(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")
        yunda_proxy = (AGENT_DIR / "agent" / "tms_runtime" / "scripts" / "yunda_waybill_proxy.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('const requiresPrefillReady = (mode) => mode === "ronghui" || mode === "yunda"', template)
        self.assertIn("SHIPNOW_PREFILL_READY", yunda_proxy)
        self.assertIn("provider: \"yunda\"", yunda_proxy)
        self.assertIn("function isYundaPrefillReady", yunda_proxy)
        self.assertIn("function waitForYundaPrefillReady", yunda_proxy)
        self.assertIn("notifyPrefillReadyWhenReady", yunda_proxy)
        self.assertIn("attempt < 80", yunda_proxy)
        self.assertIn("activePrefillKey", yunda_proxy)

    def test_ronghui_prefill_label_matching_rejects_whole_section_containers(self):
        ronghui_proxy = (AGENT_DIR / "agent" / "tms_runtime" / "scripts" / "ronghui_waybill_proxy.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("if (text === wanted) return true;", ronghui_proxy)
        self.assertIn('if (text.indexOf("查询") !== -1 && wanted.indexOf("查询") === -1) return false;', ronghui_proxy)
        self.assertIn("if (wanted.length < 3) return false;", ronghui_proxy)
        self.assertIn("text.length <= wanted.length + 4", ronghui_proxy)
        self.assertIn("labelTextMatches(text, wanted)", ronghui_proxy)
        self.assertNotIn("if (!wanted || text.indexOf(wanted) === -1) continue;", ronghui_proxy)

    def test_ronghui_prefill_payload_covers_manual_quote_fields_from_real_case(self):
        template = (CONSOLE_DIR / "templates" / "document.html").read_text(encoding="utf-8")

        expected_manual_fields = [
            "destination_site",
            "receiver_name",
            "receiver_phone",
            "receiver_address",
            "goods_name_lines",
            "package_type_lines",
            "quantity_lines",
            "weight_kg",
            "volume_m3",
            "freight_fee",
            "delivery_method",
            "payment_method",
        ]
        for field_name in expected_manual_fields:
            with self.subTest(field_name=field_name):
                self.assertIn(f'{field_name}: manualFieldValue("{field_name}")', template)

        expected_ronghui_keys = [
            "destination_site",
            "receiver_name",
            "receiver_phone",
            "receiver_address",
            "goods_name",
            "package_type",
            "quantity",
            "weight",
            "volume",
            "freight_fee",
            "delivery_method",
            "payment_method",
        ]
        for field_key in expected_ronghui_keys:
            with self.subTest(field_key=field_key):
                self.assertIn(f'key: "{field_key}"', template)

        self.assertIn('"到站"', template)
        self.assertIn('"目的地"', template)
        self.assertLess(template.index('key: "destination_site"'), template.index('key: "receiver_address"'))

    def test_yunda_frontend_uses_fixed_business_layout(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn("YUNDA_UI_SCHEMA", script)
        self.assertIn("寄方", script)
        self.assertIn("货物信息", script)
        self.assertIn("打开草稿箱", script)
        self.assertNotIn("state.sections.map", script)
        self.assertNotIn("function renderField", script)
        self.assertNotIn("DraftListInput", script)
        self.assertNotIn('data-yunda-field="sale_phone"', script)

    def test_yunda_frontend_formats_remote_electronic_stock(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn("function formatElectronicStock", script)
        self.assertIn("formatElectronicStock(state.panels?.electronicStock)", script)
        self.assertIn('"remain_num_elec"', script)
        self.assertNotIn("state.panels?.electronicStock ||", script)

    def test_yunda_frontend_accepts_real_logistics_number_key(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn("patchLogisticsFromResult", script)
        self.assertIn("data?.result?.logistics", script)
        self.assertIn("patch.LogisticsId = waybillNo", script)

    def test_yunda_frontend_wires_feedback_buttons_to_remote_actions(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn('feedbackAddress: "/ocr/yunda/feedback/address"', script)
        self.assertIn('feedbackCost: "/ocr/yunda/feedback/cost"', script)
        self.assertIn('feedbackCostUpload: "/ocr/yunda/feedback/cost/upload"', script)
        self.assertIn('returnUpload: "/ocr/yunda/return-upload"', script)
        self.assertIn('downloadTemplate: "/ocr/yunda/download-template"', script)
        self.assertIn('data-yunda-action="feedback-address"', script)
        self.assertIn('data-yunda-action="feedback-cost"', script)
        self.assertIn('data-yunda-upload="feedback-cost-1"', script)
        self.assertIn('data-yunda-upload="return-upload"', script)
        self.assertIn('data-yunda-action="download-template"', script)
        self.assertNotIn("GIS错误反馈</button> disabled", script)

    def test_yunda_frontend_renders_remote_child_waybills(self):
        script = (CONSOLE_DIR / "static" / "js" / "yunda_entry_mode.js").read_text(encoding="utf-8")

        self.assertIn("children: []", script)
        self.assertIn("Array.isArray(state.panels.children)", script)
        self.assertIn("暂无子单号", script)


class YundaEntryBackendTests(unittest.TestCase):
    def _app(self, repository=None):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(agent_base_url="http://agent.test", agent_timeout_seconds=30)
        app.repository = repository or _Repository()

        def capture_json(self, handler, status, payload):
            self.sent_status = status
            self.sent_payload = payload

        app._send_json = types.MethodType(capture_json, app)
        return app

    def test_call_yunda_entry_runtime_translates_auth_required(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {
                "method": method,
                "endpoint": endpoint,
                "payload": payload,
                "timeout": timeout,
            }
            return {
                "ok": True,
                "data": {
                    "error_code": "AUTH_REQUIRED",
                    "error": "login required",
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        status, payload = app._call_yunda_entry_runtime("bootstrap", form={"BuyerName": "张三"})

        self.assertEqual(HTTPStatus.OK, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("AUTH_REQUIRED", payload["auth_state"]["code"])
        self.assertEqual("/tms/yunda_waybill_entry", app.last_call["endpoint"])
        self.assertEqual("bootstrap", app.last_call["payload"]["params"]["action"])

    def test_persist_yunda_save_creates_waybill_and_two_snapshots(self):
        repository = _Repository()
        app = self._app(repository)

        payload = {
            "ok": True,
            "message": "saved",
            "auth_state": {"code": "AUTHENTICATED"},
            "field_errors": {},
            "data": {
                "waybill_no": "YD001",
                "normalized_form": {
                    "LogisticsId": "YD001",
                    "OpenDate": "2026/05/17",
                    "BuyerDestinationDotName": "长沙岳麓",
                    "BuyerAddress": "湖南省长沙市岳麓区测试路1号",
                    "BuyerName": "张三",
                    "BuyerMobile": "13800000000",
                    "SenderName": "李四",
                    "SenderMobile": "13900000000",
                    "ItemName": "配件",
                    "PackingType1": "纸箱",
                    "ItemTotalNumber": "1",
                    "DispatchMode": "送货",
                    "Freight": "20.00",
                    "PaymentType": "寄付",
                },
            },
        }

        result = app._persist_yunda_runtime_result(
            action="save",
            request_body={"form": payload["data"]["normalized_form"], "client_meta": {"action": "save"}},
            payload=payload,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(1, len(repository.upserts))
        self.assertEqual(2, len(repository.snapshots))
        self.assertEqual(["save_request", "save_response"], [item["snapshot_kind"] for item in repository.snapshots])
        self.assertEqual("YD001", result["data"]["local_waybill"]["waybill_no"])
        self.assertTrue(result["data"]["print_url"].endswith("/waybills/101/print?preview=1"))

    def test_persist_yunda_draft_save_writes_snapshot_only(self):
        repository = _Repository()
        app = self._app(repository)

        payload = {
            "ok": True,
            "message": "draft saved",
            "auth_state": {"code": "AUTHENTICATED"},
            "field_errors": {},
            "data": {
                "waybill_no": "YD002",
                "normalized_form": {"LogisticsId": "YD002", "BuyerName": "李雷"},
            },
        }

        result = app._persist_yunda_runtime_result(
            action="drafts/save",
            request_body={"form": payload["data"]["normalized_form"], "client_meta": {"action": "drafts/save"}},
            payload=payload,
        )

        self.assertTrue(result["ok"])
        self.assertEqual([], repository.upserts)
        self.assertEqual(1, len(repository.snapshots))
        self.assertEqual("draft_save", repository.snapshots[0]["snapshot_kind"])

    def test_handle_yunda_entry_reads_client_meta_and_returns_json(self):
        app = self._app()

        def call_runtime(self, action, *, form=None, context=None, timeout_sec=180):
            self.runtime_call = {
                "action": action,
                "form": form,
                "context": context,
                "timeout_sec": timeout_sec,
            }
            return HTTPStatus.OK, {
                "ok": True,
                "message": "ok",
                "data": {"normalized_form": dict(form or {}), "waybill_no": "YD003"},
                "field_errors": {},
                "auth_state": {"code": "AUTHENTICATED"},
            }

        def persist(self, *, action, request_body, payload):
            self.persist_call = {"action": action, "request_body": request_body, "payload": payload}
            return payload

        app._call_yunda_entry_runtime = types.MethodType(call_runtime, app)
        app._persist_yunda_runtime_result = types.MethodType(persist, app)

        app._handle_yunda_entry(
            _Handler(
                {
                    "form": {"LogisticsId": "YD003"},
                    "context": {"page_url": "https://example.test/page"},
                    "client_meta": {"selectedDraftId": "D-1"},
                }
            ),
            "/ocr/yunda/save",
        )

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual("save", app.runtime_call["action"])
        self.assertEqual("D-1", app.runtime_call["context"]["client_meta"]["selectedDraftId"])
        self.assertEqual("YD003", app.sent_payload["data"]["waybill_no"])

    def test_handle_yunda_live_proxy_returns_agent_raw_response(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {
                "method": method,
                "endpoint": endpoint,
                "payload": payload,
                "timeout": timeout,
            }
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "text/html; charset=utf-8"},
                        "body_base64": base64.b64encode("<html>Yunda</html>".encode("utf-8")).decode("ascii"),
                        "remote_path": "/ky_inms/public/index.php/business/waybill/entry/indexNew.html",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(headers={"Accept": "text/html"})

        app._handle_yunda_live_proxy(
            handler,
            "/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/indexNew.html",
            method="GET",
            query={"page": ["tab"], "p": ["nil"]},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("text/html; charset=utf-8", handler.header_value("Content-Type"))
        self.assertEqual("<html>Yunda</html>", handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual("/tms/yunda_waybill_proxy", app.last_call["endpoint"])
        self.assertEqual("GET", app.last_call["payload"]["params"]["method"])
        self.assertEqual("/ky_inms/public/index.php/business/waybill/entry/indexNew.html", app.last_call["payload"]["params"]["path"])
        self.assertEqual("page=tab&p=nil", app.last_call["payload"]["params"]["query"])

    def test_handle_yunda_live_proxy_adds_local_print_url_to_successful_save_response(self):
        repository = _Repository()
        app = self._app(repository)
        remote_body = {"info": "1", "LogisticsId": "YD777", "message": "saved"}

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "application/json; charset=utf-8"},
                        "body_base64": base64.b64encode(json.dumps(remote_body, ensure_ascii=False).encode("utf-8")).decode("ascii"),
                        "remote_path": "/ky_inms/public/index.php/business/waybill/entry/save.html",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        body = (
            "LogisticsId=YD777&OpenDate=2026%2F05%2F26&BuyerName=%E5%BC%A0%E4%B8%89"
            "&BuyerMobile=13800000000&BuyerAddress=%E6%B5%8B%E8%AF%95%E5%9C%B0%E5%9D%80"
            "&SenderName=%E6%9D%8E%E5%9B%9B&SenderMobile=13900000000&ItemName=%E9%85%8D%E4%BB%B6"
            "&PackingType1=%E7%BA%B8%E7%AE%B1&ItemTotalNumber=1&DispatchMode=%E9%80%81%E8%B4%A7"
            "&Freight=20.00&PaymentType=%E5%AF%84%E4%BB%98"
        ).encode("utf-8")
        handler = _LiveHandler(
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )

        app._handle_yunda_live_proxy(
            handler,
            "/ocr/yunda/live/ky_inms/public/index.php/business/waybill/entry/save.html",
            method="POST",
            query={},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        response_body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual("1", response_body["info"])
        self.assertEqual("YD777", response_body["LogisticsId"])
        self.assertEqual("/waybills/101/print?preview=1", response_body["shipnow_print_url"])
        self.assertEqual("/waybills/101/print?autoprint=1", response_body["shipnow_autoprint_url"])
        self.assertEqual(101, response_body["shipnow_local_waybill_id"])
        self.assertEqual(1, len(repository.upserts))
        self.assertEqual("YD777", repository.upserts[0]["payload"]["waybill_no"])
        self.assertEqual(["save_request", "save_response"], [item["snapshot_kind"] for item in repository.snapshots])
        self.assertIn("body_base64", app.last_call["payload"]["params"])

    def test_handle_ronghui_live_proxy_returns_agent_raw_response(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {
                "method": method,
                "endpoint": endpoint,
                "payload": payload,
                "timeout": timeout,
            }
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "text/html; charset=utf-8"},
                        "body_base64": base64.b64encode("<html>Ronghui</html>".encode("utf-8")).decode("ascii"),
                        "remote_path": "/widget/home",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(headers={"Accept": "text/html"})

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live",
            method="GET",
            query={},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("text/html; charset=utf-8", handler.header_value("Content-Type"))
        self.assertEqual("<html>Ronghui</html>", handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual("/tms/ronghui_waybill_proxy", app.last_call["endpoint"])
        self.assertEqual("GET", app.last_call["payload"]["params"]["method"])
        self.assertEqual("", app.last_call["payload"]["params"]["path"])
        self.assertEqual("", app.last_call["payload"]["params"]["query"])
        self.assertEqual("/ocr/ronghui/live", app.last_call["payload"]["params"]["proxy_prefix"])

    def test_handle_ronghui_live_proxy_forwards_redirect_headers(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {
                "method": method,
                "endpoint": endpoint,
                "payload": payload,
                "timeout": timeout,
            }
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 302,
                        "headers": {
                            "Content-Type": "text/plain; charset=utf-8",
                            "Location": "/ocr/ronghui/live/widget/home?page=next",
                            "Refresh": "0; url=/ocr/ronghui/live/module/index?mv=index",
                        },
                        "body_base64": base64.b64encode(b"").decode("ascii"),
                        "remote_path": "/dataOperation/saveTables",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(headers={"Accept": "text/html"})

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live/dataOperation/saveTables",
            method="POST",
            query={},
        )

        self.assertEqual(HTTPStatus.FOUND, handler.status)
        self.assertEqual("/ocr/ronghui/live/widget/home?page=next", handler.header_value("Location"))
        self.assertEqual("0; url=/ocr/ronghui/live/module/index?mv=index", handler.header_value("Refresh"))

    def test_handle_ronghui_live_proxy_preserves_static_cache_headers(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {
                            "Content-Type": "text/css; charset=utf-8",
                            "Cache-Control": "public, max-age=86400",
                        },
                        "body_base64": base64.b64encode(b".mini{}").decode("ascii"),
                        "remote_path": "/static/miniui2/themes/default/miniui.css",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(headers={"Accept": "text/css"})

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live/static/miniui2/themes/default/miniui.css",
            method="GET",
            query={},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("public, max-age=86400", handler.header_value("Cache-Control"))
        self.assertEqual(1, sum(1 for name, _ in handler.sent_headers if name.lower() == "cache-control"))

    def test_handle_ronghui_live_proxy_auth_required_returns_readable_iframe_body(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": False,
                    "error_code": "AUTH_REQUIRED",
                    "error": "当前未登录或登录态已过期。",
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(headers={"Accept": "text/html"})

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live",
            method="GET",
            query={},
        )

        body = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("text/html; charset=utf-8", handler.header_value("Content-Type"))
        self.assertIn("AUTH_REQUIRED", body)
        self.assertIn("当前未登录或登录态已过期", body)
        self.assertEqual("/tms/ronghui_waybill_proxy", app.last_call["endpoint"])

    def test_handle_ronghui_live_proxy_outer_auth_required_returns_readable_iframe_body(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": False,
                "status": 401,
                "error_code": "AUTH_REQUIRED",
                "error": "Ronghui login is required.",
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(headers={"Accept": "text/html"})

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live",
            method="GET",
            query={},
        )

        body = handler.wfile.getvalue().decode("utf-8")
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("text/html; charset=utf-8", handler.header_value("Content-Type"))
        self.assertIn("AUTH_REQUIRED", body)
        self.assertIn("Ronghui login is required", body)
        self.assertEqual("/tms/ronghui_waybill_proxy", app.last_call["endpoint"])

    def test_request_handler_routes_write_methods_to_live_proxy_dispatcher(self):
        app = self._app()
        routed = []

        def handle_proxy_write(self, handler, method):
            routed.append((handler, method))

        app.handle_proxy_write = types.MethodType(handle_proxy_write, app)
        handler_cls = app._build_handler()
        handler = handler_cls.__new__(handler_cls)

        for method_name in ("do_PUT", "do_PATCH", "do_DELETE"):
            with self.subTest(method=method_name):
                getattr(handler, method_name)()

        self.assertEqual(
            [("PUT"), ("PATCH"), ("DELETE")],
            [method for _, method in routed],
        )

    def test_handle_ronghui_live_proxy_allows_entry_auxiliary_paths_seen_in_live_page(self):
        app = self._app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {
                "method": method,
                "endpoint": endpoint,
                "payload": payload,
                "timeout": timeout,
            }
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "application/json; charset=utf-8"},
                        "body_base64": base64.b64encode(b'{"success":true}').decode("ascii"),
                        "remote_path": "/commonOption/queryDispInfoByAddress",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(headers={"Accept": "application/json"})

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live/commonOption/queryDispInfoByAddress",
            method="GET",
            query={"address": ["shaoyang"]},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("/tms/ronghui_waybill_proxy", app.last_call["endpoint"])
        self.assertEqual("/commonOption/queryDispInfoByAddress", app.last_call["payload"]["params"]["path"])
        self.assertEqual("address=shaoyang", app.last_call["payload"]["params"]["query"])

    def test_handle_ronghui_live_proxy_preserves_multipart_upload_body(self):
        repository = _Repository()
        app = self._app(repository)
        boundary = "----WebKitFormBoundaryCodex"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="demo.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
            "PNGDATA\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        content_type = f"multipart/form-data; boundary={boundary}"

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "application/json; charset=utf-8"},
                        "body_base64": base64.b64encode(b'{"success":true,"url":"group1/demo.png"}').decode("ascii"),
                        "remote_path": "/file/upload",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _LiveHandler(body=body, headers={"Content-Type": content_type})

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live/file/upload",
            method="POST",
            query={},
        )

        params = app.last_call["payload"]["params"]
        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual("/file/upload", params["path"])
        self.assertEqual(content_type, params["content_type"])
        self.assertEqual(body, base64.b64decode(params["body_base64"]))
        self.assertEqual([], repository.snapshots)

    def test_handle_ronghui_live_proxy_snapshots_successful_save_without_changing_response(self):
        repository = _Repository()
        app = self._app(repository)
        remote_body = {"success": True, "message": "保存成功"}

        def agent_request(self, method, endpoint, *, payload=None, timeout=None):
            self.last_call = {"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout}
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "status_code": 200,
                        "headers": {"Content-Type": "application/json; charset=utf-8"},
                        "body_base64": base64.b64encode(json.dumps(remote_body, ensure_ascii=False).encode("utf-8")).decode("ascii"),
                        "remote_path": "/dataOperation/saveTables",
                    },
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        body = "operationKey=TAB_BILL_CHECK_ADD&BILL_CODE=R0001&ACCEPT_MAN=%E5%BC%A0%E4%B8%89".encode("utf-8")
        handler = _LiveHandler(
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )

        app._handle_ronghui_live_proxy(
            handler,
            "/ocr/ronghui/live/dataOperation/saveTables",
            method="POST",
            query={},
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual(remote_body, json.loads(handler.wfile.getvalue().decode("utf-8")))
        self.assertEqual([], repository.upserts)
        self.assertEqual(["save_request", "save_response"], [item["snapshot_kind"] for item in repository.snapshots])
        self.assertEqual("ronghui", repository.snapshots[0]["provider"])
        self.assertIn("body_base64", app.last_call["payload"]["params"])


class SharedYundaMapperTests(unittest.TestCase):
    def test_shared_mapper_keeps_live_and_sync_shapes_consistent(self):
        live_row = {
            "LogisticsId": "YD009",
            "BuyerDestinationDotName": "长沙岳麓",
            "OpenDate": "2026/05/17",
            "BuyerAddress": "湖南省长沙市岳麓区测试路1号",
            "BuyerName": "张三",
            "BuyerMobile": "13800000000",
            "SenderName": "李四",
            "SenderMobile": "13900000000",
            "ItemName": "配件",
            "PackingType1": "纸箱",
            "ItemTotalNumber": "1",
            "DispatchMode": "送货",
            "Freight": "20",
            "PaymentType": "寄付",
            "Remarks": "备注A",
        }
        sync_row = {
            "tracking_number": "YD009",
            "destination_site": "长沙岳麓",
            "open_date": "2026-05-17",
            "receiver_address": "湖南省长沙市岳麓区测试路1号",
            "receiver_name": "张三",
            "receiver_phone": "13800000000",
            "sender_name": "李四",
            "sender_phone": "13900000000",
            "goods_name": "配件",
            "package_type": "纸箱",
            "quantity": "1",
            "delivery_method": "送货",
            "payment_method": "寄付",
            "shipping_fee": "20.00",
            "remark": "备注A",
        }

        live_payload = build_console_waybill_from_yunda_data(live_row)
        sync_payload = build_console_waybill_from_yunda_data(sync_row)

        self.assertIsNotNone(live_payload)
        self.assertIsNotNone(sync_payload)
        self.assertEqual(live_payload["waybill_no"], sync_payload["waybill_no"])
        self.assertEqual(live_payload["destination_site"], sync_payload["destination_site"])
        self.assertEqual(live_payload["receiver_name"], sync_payload["receiver_name"])
        self.assertEqual(live_payload["goods_name_lines"], sync_payload["goods_name_lines"])
        self.assertEqual(live_payload["payment_method"], sync_payload["payment_method"])
        self.assertEqual("送货", live_payload["delivery_method"])

    def test_shared_mapper_maps_known_numeric_dispatch_mode(self):
        payload = build_console_waybill_from_yunda_data(
            {
                "LogisticsId": "YD010",
                "DispatchMode": "180",
                "ItemName": "配件",
            }
        )

        self.assertIsNotNone(payload)
        self.assertEqual("派送", payload["delivery_method"])

    def test_shared_mapper_ignores_unknown_numeric_dispatch_mode(self):
        payload = build_console_waybill_from_yunda_data(
            {
                "LogisticsId": "YD012",
                "DispatchMode": "999",
                "ItemName": "配件",
            }
        )

        self.assertIsNotNone(payload)
        self.assertEqual("", payload["delivery_method"])

    def test_shared_mapper_uses_text_delivery_method_after_numeric_dispatch_mode(self):
        payload = build_console_waybill_from_yunda_data(
            {
                "LogisticsId": "YD011",
                "DispatchMode": "180",
                "delivery_method": "派送",
                "ItemName": "配件",
            }
        )

        self.assertIsNotNone(payload)
        self.assertEqual("派送", payload["delivery_method"])


if __name__ == "__main__":
    unittest.main()
