import base64
import io
import json
import sys
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


CONSOLE_DIR = Path(__file__).resolve().parents[1]
BROWSER_REQUEST_UUID = "123e4567-e89b-42d3-a456-426614174001"
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from app import LocalDocFlowApp  # noqa: E402


class _Handler:
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None):
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


class _CustomerServiceRepo:
    def __init__(self):
        self.saved = None

    def get_workflow_resource(self, resource_key):
        self.last_get_key = resource_key
        return {
            "resource_key": resource_key,
            "config": {
                "ronghui_account_ids": ["ronghui-a"],
                "yunda_account_ids": ["yunda-a"],
                "poll_interval_sec": 45,
                "sound_enabled": True,
                "password": "must-not-return",
            },
        }

    def upsert_workflow_resource(self, resource_key, config, source="backend_console"):
        self.saved = {"resource_key": resource_key, "config": dict(config), "source": source}

    def count_by_status(self):
        return {}

    def list_documents(self, limit=5):
        return []


class CustomerServiceModuleTests(unittest.TestCase):
    def _build_app(self):
        app = LocalDocFlowApp.__new__(LocalDocFlowApp)
        app.settings = SimpleNamespace(app_title="ShipNow", agent_base_url="http://agent.test", agent_timeout_seconds=30)
        app.repository = _CustomerServiceRepo()
        app._control_plane_write_context = lambda handler: {
            "actor": {
                "actor_type": "console_admin",
                "actor_id": "9",
                "roles": ["super_admin"],
                "display_name": "tester",
                "authenticated_by": "mysql_admin_session",
            },
            "actor_roles": ["super_admin"],
            "source": "console",
        }
        app._control_plane_read_context = app._control_plane_write_context
        app.template_env = Environment(
            loader=FileSystemLoader(CONSOLE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        app.project_modules = app._build_project_modules()
        app.sent_status = None
        app.sent_payload = None
        app.sent_html = ""

        accounts = [
            {
                "account_id": "ronghui-a",
                "name": "融辉 A",
                "system": "ronghui",
                "credentials": {"username": "739010002", "password": "secret", "has_saved_credentials": True},
                "status": {"status": "authenticated", "label": "登录态有效"},
            },
            {
                "account_id": "yunda-a",
                "name": "韵达 A",
                "system": "yunda",
                "credentials": {"username": "56739382003", "password": "secret", "has_saved_credentials": True},
                "status": {"status": "authenticated", "label": "登录态有效"},
            },
            {
                "account_id": "r7-a",
                "name": "R7",
                "system": "r7",
                "credentials": {"password": "secret"},
            },
        ]

        def fetch_accounts(self, *, force=True, prefer_cached=False):
            return [dict(item) for item in accounts], ""

        def send_json(self, handler, status, payload):
            self.sent_status = status
            self.sent_payload = payload

        def send_html(self, handler, body, status=HTTPStatus.OK):
            self.sent_status = status
            self.sent_html = body

        app._fetch_automation_accounts = types.MethodType(fetch_accounts, app)
        app._send_json = types.MethodType(send_json, app)
        app._send_html = types.MethodType(send_html, app)
        return app

    def test_customer_service_route_renders_problem_workbench(self):
        app = self._build_app()

        app._render_customer_service(_Handler(), {})

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertIn('data-customer-service-workbench', app.sent_html)
        self.assertIn('/customer-service/problems/query', app.sent_html)
        self.assertIn('问题件工作台', app.sent_html)
        self.assertNotIn('module-detail', app.sent_html)

    def test_customer_service_route_keeps_settings_collapsed_by_default(self):
        app = self._build_app()

        app._render_customer_service(_Handler(), {})

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertIn('data-cs-settings-panel hidden', app.sent_html)
        self.assertIn('data-cs-settings-toggle', app.sent_html)
        self.assertIn('data-cs-account-summary', app.sent_html)

    def test_customer_service_uses_single_date_range_picker(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")

        self.assertIn("data-cs-date-range", template)
        self.assertIn("data-cs-date-range-label", template)
        self.assertIn("data-cs-calendar-grid", template)
        self.assertIn("data-cs-calendar-prev", template)
        self.assertIn("data-cs-calendar-next", template)
        self.assertIn("data-cs-quick-range", template)
        self.assertNotIn('type="date"', template)

    def test_customer_service_date_range_is_visible_in_top_filter_bar(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")

        date_range_index = template.index("data-cs-date-range")
        form_end_index = template.index("</form>")
        settings_panel_index = template.index("data-cs-settings-panel")

        self.assertLess(date_range_index, form_end_index)
        self.assertLess(date_range_index, settings_panel_index)

    def test_customer_service_direction_filter_has_two_business_options(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn('<option value="published_to_me">发布给我的</option>', template)
        self.assertIn('<option value="my_published">我发布的</option>', template)
        self.assertNotIn("收到/待处理", template)
        self.assertNotIn("我登记的", template)
        self.assertNotIn("韵达查询", template)
        self.assertNotIn("韵达发布", template)
        self.assertIn("normalizeDirectionFilter", script)
        self.assertIn('return direction || "published_to_me"', script)

    def test_customer_service_settings_are_autosaved_without_save_button(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertNotIn("data-cs-save-settings", template)
        self.assertNotIn("保存设置", template)
        self.assertIn("autoSaveSettings", script)
        self.assertIn("data-cs-settings-autosave", script)

    def test_customer_service_removes_sound_reminder_controls_and_audio_logic(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertNotIn("data-cs-sound", template)
        self.assertNotIn('name="sound_enabled"', template)
        self.assertNotIn("声音提醒", template)
        self.assertNotIn("data-cs-sound", script)
        self.assertNotIn("sound_enabled", script)
        self.assertNotIn("function beep", script)
        self.assertNotIn("AudioContext", script)

    def test_customer_service_query_click_uses_document_level_delegation(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn('document.addEventListener("click"', script)
        self.assertIn('target.closest("[data-cs-query]")', script)
        self.assertNotIn('document.querySelectorAll("[data-cs-query]', script)

    def test_customer_service_initializes_each_workbench_tab(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn('document.querySelectorAll("[data-customer-service-workbench]")', script)
        self.assertIn("function initCustomerServiceRoot(root)", script)
        self.assertIn("function isRootActive()", script)
        self.assertIn("if (!isRootActive()) return;", script)

    def test_customer_service_problem_rows_open_processing_modal(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn("data-cs-problem-modal", template)
        self.assertIn("data-cs-problem-content", template)
        self.assertIn("data-cs-problem-attachments", template)
        self.assertIn('event.type === "dblclick"', script)
        self.assertIn('target.closest("[data-cs-row]")', script)
        self.assertNotIn("data-cs-drawer", template)

    def test_customer_service_problem_modal_does_not_render_top_summary_row(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertNotIn("data-cs-problem-summary", template)
        self.assertNotIn("problemSummary", script)
        self.assertNotIn(".customer-service-problem-summary", css)

    def test_customer_service_problem_modal_does_not_render_raw_fields(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertNotIn("原始字段", script)
        self.assertNotIn("JSON.stringify(raw)", script)
        self.assertIn("renderProblemModal", script)
        self.assertIn("collectProblemAttachments", script)

    def test_customer_service_problem_list_uses_login_account_and_no_direction_column(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn("<th>登录账号</th>", template)
        self.assertIn("<th>问题类型</th>", template)
        self.assertNotIn("<th>方向</th>", template)
        self.assertIn("displayAccount(row)", script)
        self.assertIn("rowProblemType(row)", script)
        self.assertNotIn('<td>${escapeHtml(row.source_direction || "")}</td>', script)

    def test_customer_service_highlights_only_unreplied_actionable_rows(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn("function problemNeedsAttention", script)
        self.assertIn("problemNeedsAttention(row)", script)
        self.assertIn('needsAttention ? "is-attention" : ""', script)
        self.assertIn("problemNeedsAttention(row) && !seen.has(problemKey(row))", script)
        self.assertIn(".customer-service-table tbody tr.is-attention td", css)
        self.assertNotIn(".customer-service-table tbody tr.is-new td", css)

    def test_customer_service_problem_modal_prioritizes_readable_business_sections(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn("关键信息", template)
        self.assertIn("customer-service-problem-message-main", script)
        self.assertIn("customer-service-problem-reply-body", script)
        self.assertIn(".customer-service-problem-message-main", css)
        self.assertIn(".customer-service-problem-meta--compact", css)
        self.assertNotIn('{ label: "方向"', script)

    def test_customer_service_problem_modal_uses_grouped_compact_processing_layout(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn("customer-service-problem-section--reading", template)
        self.assertIn("customer-service-problem-attachment-block", template)
        self.assertIn("<h4>附件</h4>", template)
        self.assertIn('rows="3"', template)
        self.assertIn("renderProblemMetaGroups", script)
        self.assertIn('title: "站点"', script)
        self.assertIn('title: "人员与货物"', script)
        self.assertIn('title: "时间"', script)
        self.assertIn(".customer-service-problem-meta-group", css)
        self.assertIn(".customer-service-problem-reply-fields", css)

    def test_customer_service_reply_form_uses_explicit_default_status(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertNotIn("保持原状态", template)
        self.assertIn('<option value="已处理" selected>已处理</option>', template)
        self.assertIn('const DEFAULT_REPLY_STATUS = "已处理";', script)
        self.assertIn("replyStatusNode.value = DEFAULT_REPLY_STATUS", script)
        self.assertNotIn('prob_status: status || row.status || "处理中"', script)

    def test_customer_service_reply_submit_is_observable_and_refreshes(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn("data-cs-reply-submit", template)
        self.assertIn("state.replying", script)
        self.assertIn("回复提交中", script)
        self.assertIn("await runQuery()", script)
        self.assertIn('target.closest("[data-cs-reply-submit]")', script)

    def test_customer_service_reply_success_rerenders_open_modal_before_refresh(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        submit_start = script.index("async function submitReply")
        submit_end = script.index("function updatePublishAccounts", submit_start)
        submit_block = script[submit_start:submit_end]

        self.assertIn("state.selectedDetails", script)
        self.assertIn("renderProblemModal(row, state.selectedDetails || [])", submit_block)
        self.assertLess(submit_block.index("row.reply_text = replyText"), submit_block.index("renderProblemModal(row, state.selectedDetails || [])"))
        self.assertLess(submit_block.index("renderProblemModal(row, state.selectedDetails || [])"), submit_block.index("await runQuery()"))

    def test_customer_service_problem_modal_does_not_render_mark_read_button(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertNotIn("data-cs-mark-read", template)
        self.assertNotIn("标记已读", template)
        self.assertNotIn("async function markRead", script)
        self.assertNotIn('target.closest("[data-cs-mark-read]")', script)

    def test_customer_service_problem_modal_shows_notified_site_and_attachment_images(self):
        template = (CONSOLE_DIR / "templates" / "customer_service.html").read_text(encoding="utf-8")
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn("data-cs-image-viewer", template)
        self.assertIn("data-cs-image-viewer-img", template)
        self.assertIn("发布网点", script)
        self.assertIn("通知网点", script)
        self.assertIn("通知网点编码", script)
        self.assertIn("目的网点", script)
        self.assertIn("normalizeAttachmentHref", script)
        self.assertIn("customer-service-attachment-thumb", script)
        self.assertIn("attachmentPreviewHref", script)
        self.assertIn("/customer-service/problems/attachments/preview", script)
        self.assertIn("data-cs-image-preview", script)
        self.assertIn(".customer-service-attachment-gallery", css)
        self.assertIn(".customer-service-image-viewer", css)

    def test_customer_service_yunda_site_fields_match_origin_columns(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn("publishSite", script)
        self.assertIn("publishSiteCode", script)
        self.assertIn("destinationSite", script)
        self.assertIn('"site_id"', script)
        self.assertIn('"site_id_bm"', script)
        self.assertIn('"recv_site_id"', script)
        self.assertIn('"recv_site_nm_arr"', script)
        self.assertIn('"recv_comp"', script)
        self.assertIn('{ label: "发布网点", value: publishSite }', script)
        self.assertIn('{ label: "发布网点编码", value: publishSiteCode }', script)
        self.assertIn('{ label: "通知网点", value: notifiedSite }', script)
        self.assertIn('{ label: "通知网点编码", value: notifiedSiteCode }', script)
        self.assertIn('{ label: "目的网点", value: destinationSite }', script)
        self.assertIn('{ label: "寄件站点", value: senderSite }', script)
        self.assertNotIn('{ label: "发布/登记编码", value: registerSiteCode }', script)

    def test_customer_service_problem_attachments_use_detail_paths_not_list_icons(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn("attachment_path", script)
        self.assertIn("old_name", script)
        self.assertIn("isYundaAttachmentIconOnly", script)
        self.assertIn("item.href &&", script)
        self.assertNotIn('candidates.push({ href: "", label: "原页附件" });', script)

    def test_customer_service_yunda_download_attachment_uses_public_index_root(self):
        script = (CONSOLE_DIR / "static" / "customer_service.js").read_text(encoding="utf-8")

        self.assertIn("YUNDA_ATTACHMENT_PUBLIC_ROOT", script)
        self.assertIn('text.startsWith("/base/")', script)
        self.assertIn('${YUNDA_ATTACHMENT_PUBLIC_ROOT}/${text.replace(/^\\/+/, "")}', script)

    def test_customer_service_problem_content_is_top_aligned(self):
        css = (CONSOLE_DIR / "static" / "style.css").read_text(encoding="utf-8")

        self.assertIn(".customer-service-problem-message-main {", css)
        self.assertIn("justify-content: flex-start;", css)
        self.assertIn(".customer-service-problem-content { display: grid; gap: 12px; align-items: start; }", css)

    def test_customer_service_attachment_preview_streams_agent_image_bytes(self):
        app = self._build_app()

        def fake_call(
            payload,
            *,
            trusted_context,
            browser_request_uuid,
            timeout_sec=120,
        ):
            self.assertEqual("fetch_attachment", payload["action"])
            self.assertEqual("yunda-a", payload["account_id"])
            self.assertEqual(BROWSER_REQUEST_UUID, browser_request_uuid)
            self.assertEqual("9", trusted_context["actor"]["actor_id"])
            self.assertEqual("https://kyproblem.yunda56.com/ky_problem/public/static/problem/image/a.png", payload["payload"]["source_url"])
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "content_type": "image/png",
                        "filename": "a.png",
                        "body_base64": "iVBORw0KGgpwYXlsb2Fk",
                    },
                    "cost_sec": 0.1,
                },
            }

        app._call_customer_service_problem_agent = fake_call
        handler = _Handler()

        app._handle_customer_service_attachment_preview(
            handler,
            {
                "platform": ["yunda"],
                "account_id": ["yunda-a"],
                "src": ["https://kyproblem.yunda56.com/ky_problem/public/static/problem/image/a.png"],
                "request_uuid": [BROWSER_REQUEST_UUID],
            },
        )

        self.assertEqual(HTTPStatus.OK, handler.status)
        self.assertEqual(b"\x89PNG\r\n\x1a\npayload", handler.wfile.getvalue())
        self.assertIn(("Content-Type", "image/png"), handler.sent_headers)
        self.assertIn(("X-Content-Type-Options", "nosniff"), handler.sent_headers)
        self.assertTrue(any(name == "Content-Security-Policy" for name, _value in handler.sent_headers))
        self.assertNotIn("set-cookie", {name.lower() for name, _value in handler.sent_headers})

    def test_customer_service_attachment_preview_rejects_svg_even_when_agent_claims_png(self):
        app = self._build_app()

        def fake_call(payload, *, trusted_context, browser_request_uuid, timeout_sec=120):
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "data": {
                        "ok": True,
                        "content_type": "image/png",
                        "filename": "payload.svg",
                        "body_base64": base64.b64encode(
                            b"<svg xmlns='http://www.w3.org/2000/svg'><script>bad()</script></svg>"
                        ).decode("ascii"),
                    },
                },
            }

        app._call_customer_service_problem_agent = fake_call
        handler = _Handler()
        app._handle_customer_service_attachment_preview(
            handler,
            {
                "platform": ["yunda"],
                "account_id": ["yunda-a"],
                "src": ["https://kyproblem.yunda56.com/image/payload.svg"],
                "request_uuid": [BROWSER_REQUEST_UUID],
            },
        )

        self.assertEqual(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, app.sent_status)
        self.assertFalse(app.sent_payload["ok"])

    def test_customer_service_raster_magic_ignores_claimed_mime(self):
        app = self._build_app()
        self.assertEqual("image/jpeg", app._customer_service_raster_mime_type(b"\xff\xd8jpeg"))
        self.assertEqual("image/png", app._customer_service_raster_mime_type(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual("image/gif", app._customer_service_raster_mime_type(b"GIF87a"))
        self.assertEqual("image/webp", app._customer_service_raster_mime_type(b"RIFFxxxxWEBP"))
        self.assertEqual("", app._customer_service_raster_mime_type(b"<?xml version='1.0'?><svg/>"))

    def test_problem_settings_get_filters_accounts_and_never_returns_credentials(self):
        app = self._build_app()

        app._handle_customer_service_problem_settings_get(_Handler())

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        payload_text = json.dumps(app.sent_payload, ensure_ascii=False)
        self.assertTrue(app.sent_payload["ok"])
        self.assertEqual(["ronghui-a"], app.sent_payload["settings"]["ronghui_account_ids"])
        self.assertEqual(["yunda-a"], app.sent_payload["settings"]["yunda_account_ids"])
        self.assertEqual({"ronghui", "yunda"}, {item["system"] for item in app.sent_payload["accounts"]})
        self.assertEqual(
            {"739010002", "56739382003"},
            {item["login_account"] for item in app.sent_payload["accounts"]},
        )
        self.assertNotIn("secret", payload_text)
        self.assertNotIn("password", payload_text.lower())
        self.assertNotIn("must-not-return", payload_text)

    def test_problem_settings_post_persists_only_ids_and_poll_flags(self):
        app = self._build_app()
        handler = _Handler(
            json.dumps(
                {
                    "ronghui_account_ids": ["ronghui-a", "r7-a"],
                    "yunda_account_ids": ["yunda-a"],
                    "poll_interval_sec": 5,
                    "sound_enabled": True,
                    "cookie": "must-not-save",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            {
                "Content-Type": "application/json",
                "X-Browser-Request-UUID": BROWSER_REQUEST_UUID,
            },
        )

        app._handle_customer_service_problem_settings_post(handler)

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        saved = app.repository.saved["config"]
        self.assertEqual(["ronghui-a"], saved["ronghui_account_ids"])
        self.assertEqual(["yunda-a"], saved["yunda_account_ids"])
        self.assertEqual(15, saved["poll_interval_sec"])
        self.assertNotIn("sound_enabled", saved)
        self.assertNotIn("sound_enabled", app.sent_payload["settings"])
        self.assertNotIn("cookie", saved)

    def test_problem_query_aggregates_selected_accounts_with_source_fields(self):
        app = self._build_app()
        calls = []

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            params = payload["params"]
            calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": True,
                    "rows": [
                        {
                            "platform": params["platform"],
                            "account_id": params["account_id"],
                            "account_label": params["account_label"],
                            "account_login": params["account_login"],
                            "source_direction": params["filters"]["direction"],
                            "external_id": f'{params["platform"]}-1',
                            "waybill_no": "2606000040",
                            "raw": {"REGISTER_SITE": "邵阳操作场", "SEND_SITE": "邵阳操作场"}
                            if params["account_login"] == "739010002"
                            else {"REGISTER_SITE": "长沙操作场", "SEND_SITE": "株洲操作场"},
                        }
                    ],
                    "stats": {"total": 1},
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            json.dumps(
                {
                    "platforms": ["ronghui", "yunda"],
                    "account_ids": ["ronghui-a", "yunda-a"],
                    "filters": {"direction": "received"},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            {
                "Content-Type": "application/json",
                "X-Browser-Request-UUID": BROWSER_REQUEST_UUID,
            },
        )

        app._handle_customer_service_problem_query(handler)

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertTrue(app.sent_payload["ok"])
        self.assertEqual(2, len(app.sent_payload["rows"]))
        self.assertEqual({"ronghui-a", "yunda-a"}, {item["account_id"] for item in app.sent_payload["rows"]})
        self.assertEqual({"739010002", "56739382003"}, {item["account_login"] for item in app.sent_payload["rows"]})
        self.assertEqual("/internal/v1/tms/customer_service_problem", calls[0]["endpoint"])
        self.assertEqual("query", calls[0]["payload"]["params"]["action"])
        self.assertEqual("console", calls[0]["payload"]["source"])
        self.assertEqual("console_admin", calls[0]["payload"]["actor"]["actor_type"])
        self.assertTrue(calls[0]["payload"]["idempotency_key"].startswith("console:9:tool.execute:"))
        self.assertIn("account_login", calls[0]["payload"]["params"])
        self.assertNotIn("password", json.dumps(calls, ensure_ascii=False).lower())

    def test_problem_query_filters_739010002_to_shaoyang_operation_site(self):
        app = self._build_app()

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            params = payload["params"]
            if params["account_login"] == "739010002":
                rows = [
                    {
                        "platform": "ronghui",
                        "account_id": params["account_id"],
                        "account_login": params["account_login"],
                        "external_id": "keep",
                        "waybill_no": "R-keep",
                        "raw": {"REGISTER_SITE": "邵阳操作场", "SEND_SITE": "邵阳操作场"},
                    },
                    {
                        "platform": "ronghui",
                        "account_id": params["account_id"],
                        "account_login": params["account_login"],
                        "external_id": "wrong-notified",
                        "waybill_no": "R-notified",
                        "raw": {"REGISTER_SITE": "邵阳操作场", "SEND_SITE": "长沙操作场"},
                    },
                    {
                        "platform": "ronghui",
                        "account_id": params["account_id"],
                        "account_login": params["account_login"],
                        "external_id": "wrong-publish",
                        "waybill_no": "R-publish",
                        "raw": {"REGISTER_SITE": "长沙操作场", "SEND_SITE": "邵阳操作场"},
                    },
                    {
                        "platform": "ronghui",
                        "account_id": params["account_id"],
                        "account_login": params["account_login"],
                        "external_id": "missing-site",
                        "waybill_no": "R-missing",
                        "raw": {"REGISTER_SITE": "邵阳操作场"},
                    },
                ]
            else:
                rows = [
                    {
                        "platform": "yunda",
                        "account_id": params["account_id"],
                        "account_login": params["account_login"],
                        "external_id": "other-account",
                        "waybill_no": "Y-keep",
                        "raw": {"site_id": "长沙操作场", "recv_site_id": "株洲操作场"},
                    }
                ]
            return {"ok": True, "status": 200, "data": {"ok": True, "rows": rows}}

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            json.dumps(
                {
                    "platforms": ["ronghui", "yunda"],
                    "account_ids": ["ronghui-a", "yunda-a"],
                    "filters": {"direction": "published_to_me"},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            {
                "Content-Type": "application/json",
                "X-Browser-Request-UUID": BROWSER_REQUEST_UUID,
            },
        )

        app._handle_customer_service_problem_query(handler)

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertEqual({"keep", "other-account"}, {row["external_id"] for row in app.sent_payload["rows"]})
        self.assertNotIn("wrong-notified", {row["external_id"] for row in app.sent_payload["rows"]})
        self.assertNotIn("wrong-publish", {row["external_id"] for row in app.sent_payload["rows"]})
        self.assertNotIn("missing-site", {row["external_id"] for row in app.sent_payload["rows"]})

    def test_problem_query_preserves_per_account_error_diagnostics(self):
        app = self._build_app()
        failures = [
            {
                "account_id": "ronghui-a",
                "message": "页面结构变化",
                "error_code": "AMBIGUOUS_GRID_URL",
            },
            {
                "account_id": "yunda-a",
                "message": "需要重新登录",
                "error_code": "AUTH_REQUIRED",
            },
        ]

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            params = payload["params"]
            match = next(item for item in failures if item["account_id"] == params["account_id"])
            return {
                "ok": True,
                "status": 200,
                "data": {
                    "ok": False,
                    "message": match["message"],
                    "error_code": match["error_code"],
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            json.dumps(
                {
                    "platforms": ["ronghui", "yunda"],
                    "account_ids": ["ronghui-a", "yunda-a"],
                    "filters": {"direction": "received"},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            {
                "Content-Type": "application/json",
                "X-Browser-Request-UUID": BROWSER_REQUEST_UUID,
            },
        )

        app._handle_customer_service_problem_query(handler)

        self.assertEqual(HTTPStatus.OK, app.sent_status)
        self.assertFalse(app.sent_payload["ok"])
        self.assertEqual([], app.sent_payload["rows"])
        self.assertEqual(2, app.sent_payload["stats"]["error_count"])
        self.assertEqual(
            {"AMBIGUOUS_GRID_URL", "AUTH_REQUIRED"},
            {item["error_code"] for item in app.sent_payload["errors"]},
        )
        self.assertEqual({"ronghui-a", "yunda-a"}, {item["account_id"] for item in app.sent_payload["errors"]})

    def test_problem_reply_submits_precise_durable_command(self):
        app = self._build_app()
        calls = []

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append({"method": method, "endpoint": endpoint, "payload": payload, "timeout": timeout})
            return {
                "ok": True,
                "status": HTTPStatus.ACCEPTED,
                "data": {
                    "command_id": "command-reply-1",
                    "work_item_id": "work-item-reply-1",
                    "run_id": "run-reply-1",
                    "status": "RECEIVED",
                    "reused": False,
                    "next_poll_after_ms": 1000,
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            json.dumps(
                {
                    "platform": "ronghui",
                    "account_id": "ronghui-a",
                    "item": {
                        "external_id": "problem-1",
                        "source_direction": "published_to_me",
                        "waybill_no": "2606000040",
                        "status": "待处理",
                        "raw": {"token": "must-not-cross-command-boundary"},
                    },
                    "payload": {
                        "reply_text": "已处理",
                        "prob_status": "已处理",
                        "old_prob_status": "待处理",
                        "REVERSION": "legacy duplicate",
                        "arbitrary_write": {"status": "closed"},
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            {
                "Content-Type": "application/json",
                "X-Browser-Request-UUID": BROWSER_REQUEST_UUID,
            },
        )

        app._handle_customer_service_problem_agent_action(handler, "reply")

        response = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(HTTPStatus.ACCEPTED, handler.status)
        self.assertTrue(response["pending"])
        self.assertEqual("run-reply-1", response["run_id"])
        self.assertEqual("/internal/v1/commands", calls[0]["endpoint"])
        command = calls[0]["payload"]
        self.assertEqual("tool.execute", command["command_type"])
        self.assertEqual(
            "customer_service_problem_reply",
            command["parameters"]["tool_name"],
        )
        self.assertEqual(
            f"console:9:tool.execute:{BROWSER_REQUEST_UUID}",
            command["idempotency_key"],
        )
        arguments = command["parameters"]["arguments"]
        self.assertEqual("problem-1", arguments["external_id"])
        self.assertEqual("2606000040", arguments["waybill_no"])
        self.assertEqual("已处理", arguments["reply_text"])
        self.assertNotIn("raw", arguments)
        self.assertNotIn("REVERSION", arguments)
        self.assertNotIn("arbitrary_write", arguments)
        self.assertEqual("console", command["source"])
        self.assertEqual(["super_admin"], command["actor_roles"])

    def test_problem_write_requires_stable_browser_request_uuid(self):
        app = self._build_app()

        def fail_agent(*args, **kwargs):
            raise AssertionError("missing UUID must fail before Agent submission")

        app._agent_request = fail_agent
        handler = _Handler(
            json.dumps(
                {
                    "platform": "ronghui",
                    "account_id": "ronghui-a",
                    "item": {"external_id": "problem-1"},
                }
            ).encode("utf-8"),
            {"Content-Type": "application/json"},
        )

        app._handle_customer_service_problem_agent_action(handler, "mark_read")

        response = app.sent_payload
        self.assertEqual(HTTPStatus.BAD_REQUEST, app.sent_status)
        self.assertFalse(response["ok"])
        self.assertEqual("BROWSER_REQUEST_UUID_REQUIRED", response["error_code"])

    def test_problem_publish_uses_precise_tool_and_closed_payload(self):
        app = self._build_app()
        calls = []

        def agent_request(self, method, endpoint, *, payload=None, timeout=None, console_principal=None):
            calls.append(payload)
            return {
                "ok": True,
                "status": HTTPStatus.ACCEPTED,
                "data": {
                    "command_id": "command-publish-1",
                    "work_item_id": "work-item-publish-1",
                    "run_id": "run-publish-1",
                    "status": "RECEIVED",
                    "reused": False,
                    "next_poll_after_ms": 1000,
                },
            }

        app._agent_request = types.MethodType(agent_request, app)
        handler = _Handler(
            json.dumps(
                {
                    "platform": "yunda",
                    "account_id": "yunda-a",
                    "payload": {
                        "ship_no": "Y0001",
                        "classes_type": "破损",
                        "prob_text": "外包装破损",
                        "site_id": ["site-1"],
                        "unknown": "must-not-cross-command-boundary",
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            {
                "Content-Type": "application/json",
                "X-Browser-Request-UUID": BROWSER_REQUEST_UUID,
            },
        )

        app._handle_customer_service_problem_agent_action(handler, "publish")

        command = calls[0]
        self.assertEqual(
            "customer_service_problem_publish",
            command["parameters"]["tool_name"],
        )
        publish_payload = command["parameters"]["arguments"]["payload"]
        self.assertEqual(["site-1"], publish_payload["site_id"])
        self.assertNotIn("unknown", publish_payload)


if __name__ == "__main__":
    unittest.main()
