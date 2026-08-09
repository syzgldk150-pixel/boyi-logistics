"""Console composition root and HTTP lifecycle."""

from console.app_support import *  # noqa: F403
from console.services.auth import AuthServiceMixin
from console.services.monitoring_finance import MonitoringFinanceServiceMixin
from console.services.customer_service import CustomerServiceMixin
from console.services.waybills_receipts import WaybillsReceiptsServiceMixin
from console.services.tms_proxy import TmsProxyServiceMixin
from console.services.automation import AutomationServiceMixin
from console.services.documents import DocumentServiceMixin
from console.navigation import (
    CONSOLE_NAVIGATION,
    MOBILE_NAVIGATION_CANDIDATES,
    mobile_bottom_nav_for_user,
)


class LocalDocFlowApp(AuthServiceMixin, MonitoringFinanceServiceMixin, CustomerServiceMixin, WaybillsReceiptsServiceMixin, TmsProxyServiceMixin, AutomationServiceMixin, DocumentServiceMixin):
    def __init__(self) -> None:
        self.settings = load_settings()
        self.template_store = TemplateStore(self.settings)
        self.repository = DocumentRepository(self.settings)
        self.repository.initialize()
        self._session_secret = self.settings.session_secret or secrets.token_hex(32)
        if not self.settings.session_secret:
            print(
                "DOCFLOW_SESSION_SECRET is not set; using a temporary session secret for this process."
            )
        self._ensure_seed_admin_user()
        self.service = DocumentService(
            self.settings,
            self.repository,
            self.template_store,
            build_qwen_provider(self.settings),
        )
        self.task_queue = DocumentTaskQueue(self.settings.ocr_worker_count, self.service.process_document)
        self.service.attach_task_queue(self.task_queue)
        self.task_queue.start()
        self.recovered_documents = []  # self.service.recover_pending_documents()  # TEMP: skip DB
        self.template_env = Environment(
            loader=FileSystemLoader(MODULE_DIR / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.template_env.filters["tojson_pretty"] = lambda value: json.dumps(
            value, ensure_ascii=False, indent=2
        )
        self.template_env.globals["ui_label"] = ui_label
        self.template_env.globals["current_admin_user"] = current_admin_user
        self.template_env.globals["console_navigation"] = CONSOLE_NAVIGATION
        self.template_env.globals["mobile_navigation_candidates"] = MOBILE_NAVIGATION_CANDIDATES
        self.template_env.globals["mobile_navigation_for_user"] = mobile_bottom_nav_for_user
        self.project_modules = self._build_project_modules()
        self.finance_service = FinanceService(self.repository, agent_request=self._agent_request)
        self.finance_service.initialize_schema()
        self.automation_virtual_task_state: dict[str, dict[str, Any]] = {}
        self.routes = ConsoleRouteDispatcher()

    def _ensure_seed_admin_user(self) -> None:
        if self.repository.count_admin_users() > 0:
            return
        username = self.settings.admin_seed_username
        password = self.settings.admin_seed_password
        if not username or not password:
            print(
                "No admin user exists. Set DOCFLOW_ADMIN_USERNAME and DOCFLOW_ADMIN_PASSWORD "
                "before starting the console to create the first admin account."
            )
            return
        self.repository.create_admin_user(
            username=username,
            display_name="系统管理员",
            password_hash=hash_admin_password(password),
            is_active=True,
        )
        print("Created the first admin account from DOCFLOW_ADMIN_USERNAME.")

    def run(self) -> None:
        handler = self._build_handler()
        server = ThreadingHTTPServer((self.settings.host, self.settings.port), handler)
        print(
            f"Logistics Agent local console: http://{self.settings.host}:{self.settings.port} "
            f"(Qwen workers={self.settings.ocr_worker_count})"
        )
        if self.recovered_documents:
            print(f"Recovered queued documents: {self.recovered_documents}")
        server.serve_forever()

    def _build_handler(self):
        app = self

        class RequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                app.handle_get(self)

            def do_POST(self) -> None:
                app.handle_post(self)

            def do_PUT(self) -> None:
                app.handle_proxy_write(self, "PUT")

            def do_PATCH(self) -> None:
                app.handle_proxy_write(self, "PATCH")

            def do_DELETE(self) -> None:
                app.handle_proxy_write(self, "DELETE")

            def log_message(self, fmt: str, *args) -> None:
                return

        return RequestHandler

    def handle_proxy_write(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        _CURRENT_ADMIN_USER.set(None)
        parsed = urlparse(handler.path)
        query = parse_qs(parsed.query)
        if not self._ensure_authorized(handler):
            return

        if parsed.path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_ronghui_receipt_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        if parsed.path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_yunda_receipt_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        if parsed.path.startswith(RONGHUI_LIVE_PROXY_PREFIX):
            self._handle_ronghui_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        if parsed.path.startswith(YUNDA_LIVE_PROXY_PREFIX):
            self._handle_yunda_live_proxy(handler, parsed.path, method=method.upper(), query=query)
            return
        self._send_json(
            handler,
            HTTPStatus.NOT_FOUND,
            {"ok": False, "message": "代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
        )

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        _CURRENT_ADMIN_USER.set(None)
        parsed = urlparse(handler.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        query = parse_qs(parsed.query)

        if path.startswith("/static/"):
            relpath = path[len("/static/") :]
            self._serve_static_file(handler, relpath)
            return
        if path == "/login":
            self._render_login(handler, query)
            return
        if not self._ensure_authorized(handler):
            return
        if self.routes.handle_get(self, handler, path, parsed.path, query):
            return

        if path in {"/", "/portal"}:
            self._render_portal(handler, query)
            return
        if path == "/monitoring/summary":
            self._handle_monitoring_summary(handler, query)
            return
        if path == "/monitoring/daily-sign":
            self._handle_monitoring_daily_sign(handler, query)
            return
        if path == "/monitoring/stream":
            self._handle_monitoring_stream(handler, query)
            return
        if path == "/monitoring/detail-link":
            self._handle_monitoring_detail_link(handler, query)
            return
        if path in {"/ocr", "/workspaces/ocr"}:
            self._render_ocr_workspace(handler, query)
            return
        if path == "/ocr/boyi/frame":
            frame_query = dict(query)
            frame_query["boyi_frame"] = ["1"]
            self._render_ocr_workspace(handler, frame_query)
            return
        if path == "/receipts":
            self._render_receipts(handler, query)
            return
        if path == "/modules/customer-service":
            self._render_customer_service(handler, query)
            return
        if path == "/modules/finance":
            self._render_finance(handler, query)
            return
        if path == "/finance/summary":
            self._handle_finance_get(handler, "summary", query)
            return
        if path == "/finance/trend":
            self._handle_finance_get(handler, "trend", query)
            return
        if path == "/finance/entries":
            self._handle_finance_get(handler, "entries", query)
            return
        if path == "/finance/fee-mappings":
            self._handle_finance_get(handler, "fee_mappings", query)
            return
        if path == "/finance/sync-batches":
            self._handle_finance_get(handler, "sync_batches", query)
            return
        if path == "/customer-service/problems/attachments/preview":
            self._handle_customer_service_attachment_preview(handler, query)
            return
        if path == "/customer-service/problem-settings":
            self._handle_customer_service_problem_settings_get(handler)
            return
        if path == "/receipts/data":
            self._handle_receipts_data(handler, query)
            return
        if path == "/receipts/download-images":
            self._handle_receipts_image_archive(handler, query)
            return
        if path.startswith("/receipts/attachments/"):
            self._handle_receipt_attachment(handler, path, query)
            return
        if parsed.path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_ronghui_receipt_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if parsed.path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_yunda_receipt_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if path.startswith("/receipts/"):
            self._handle_receipt_detail(handler, path)
            return
        if parsed.path.startswith(RONGHUI_LIVE_PROXY_PREFIX):
            self._handle_ronghui_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if parsed.path.startswith(YUNDA_LIVE_PROXY_PREFIX):
            self._handle_yunda_live_proxy(handler, parsed.path, method="GET", query=query)
            return
        if path == "/dispatch":
            self._render_dispatch(handler, query)
            return
        if path == "/tracking":
            self._render_tracking(handler, query)
            return
        if path == "/waybills":
            self._render_waybills(handler, query)
            return
        if path == "/line-haul-contacts":
            self._render_line_haul_contacts(handler, query)
            return
        if path.startswith("/waybills/") and path.endswith("/print"):
            waybill_id = self._parse_document_id(path)
            if waybill_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Waybill not found.")
                return
            self._render_waybill_print(handler, waybill_id, query)
            return
        if path in {"/automations", "/workspaces/automations"}:
            self._render_automations(handler, query)
            return
        if path.startswith("/automation-accounts/") and path.endswith("/status"):
            self._handle_automation_account_status_get(handler, path, query)
            return
        if path == "/automation-accounts/statuses":
            self._handle_automation_accounts_statuses_get(handler, query)
            return
        if path == "/automation-accounts":
            self._render_automation_accounts(handler, query)
            return
        if path == "/settings/accounts":
            self._render_admin_accounts(handler, query)
            return
        if path == "/automations/session-context":
            self._handle_automation_session_context(handler, query)
            return
        session_route = self._automation_session_route(path)
        if session_route and session_route[1] == "/status":
            self._handle_tms_session_status(handler, profile=session_route[0], query=query)
            return
        if path == "/automations/tms-session/status":
            self._handle_tms_session_status(handler, query=query)
            return
        if path == "/templates/new":
            self._render_template_editor(handler, None, query)
            return
        if path.startswith("/templates/") and path.endswith("/edit"):
            template_name = unquote(path[len("/templates/") : -len("/edit")].strip("/"))
            if not template_name:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Template not found.")
                return
            self._render_template_editor(handler, template_name, query)
            return
        if path.startswith("/modules/"):
            slug = path[len("/modules/") :].strip("/")
            if not slug:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Module not found.")
                return
            self._render_module(handler, slug, query)
            return
        if path == "/automations/tasks/output":
            self._handle_automation_task_output(handler, query)
            return
        if path.startswith("/documents/") and path.endswith("/export.json"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._export_document_json(handler, document_id)
            return
        if path.startswith("/documents/"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._render_document(handler, document_id, query)
            return
        if path.startswith("/runtime/"):
            relpath = path[len("/runtime/") :]
            self._serve_runtime_file(handler, relpath)
            return
        self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        _CURRENT_ADMIN_USER.set(None)
        parsed = urlparse(handler.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/login":
            self._handle_login(handler)
            return
        if not self._ensure_authorized(handler):
            return
        query = parse_qs(parsed.query)
        if self.routes.handle_post(self, handler, path, parsed.path, query):
            return
        if path == "/logout":
            self._handle_logout(handler)
            return
        if path == "/settings/profile/avatar":
            self._handle_admin_avatar_upload(handler)
            return
        if path == "/settings/accounts/create":
            self._handle_admin_account_create(handler)
            return
        if path.startswith("/settings/accounts/") and path.endswith("/toggle"):
            self._handle_admin_account_toggle(handler, path)
            return
        if path.startswith("/settings/accounts/") and path.endswith("/reset-password"):
            self._handle_admin_account_reset_password(handler, path)
            return
        if self._handle_automation_account_post(handler, path):
            return

        if path == "/customer-service/problem-settings":
            self._handle_customer_service_problem_settings_post(handler)
            return
        if path == "/customer-service/problems/query":
            self._handle_customer_service_problem_query(handler)
            return
        if path == "/customer-service/problems/detail":
            self._handle_customer_service_problem_agent_action(handler, "detail")
            return
        if path == "/customer-service/problems/mark-read":
            self._handle_customer_service_problem_agent_action(handler, "mark_read")
            return
        if path == "/customer-service/problems/reply":
            self._handle_customer_service_problem_agent_action(handler, "reply")
            return
        if path == "/customer-service/problems/publish":
            self._handle_customer_service_problem_agent_action(handler, "publish")
            return
        if path == "/customer-service/problems/attachments/upload":
            self._handle_customer_service_attachment_upload(handler)
            return

        if path == "/finance/sync":
            self._handle_finance_post(handler, "sync")
            return
        if path == "/finance/backfill":
            self._handle_finance_post(handler, "backfill")
            return
        if re.fullmatch(r"/finance/fee-mappings/\d+", path):
            self._handle_finance_post(handler, "save_mapping", path=path)
            return
        if re.fullmatch(r"/finance/sync-batches/\d+/retry", path):
            self._handle_finance_post(handler, "retry_batch", path=path)
            return

        if path == "/tracking/query":
            self._handle_tracking_query(handler)
            return
        if path == "/receipts/sync":
            self._handle_receipts_sync(handler)
            return
        if path.startswith("/receipts/") and path.endswith("/audit"):
            self._handle_receipt_audit(handler, path)
            return
        if parsed.path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_ronghui_receipt_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if parsed.path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX):
            self._handle_yunda_receipt_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if parsed.path.startswith(RONGHUI_LIVE_PROXY_PREFIX):
            self._handle_ronghui_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if parsed.path.startswith(YUNDA_LIVE_PROXY_PREFIX):
            self._handle_yunda_live_proxy(handler, parsed.path, method="POST", query=parse_qs(parsed.query))
            return
        if path.startswith("/ocr/yunda/"):
            self._handle_yunda_entry(handler, path)
            return
        if path in {"/upload", "/ocr/upload"}:
            self._handle_upload(handler)
            return
        if path == "/waybills/quote-options":
            self._handle_quote_options(handler)
            return
        if path == "/waybills/manual":
            self._handle_manual_waybill(handler)
            return
        if path.startswith("/waybills/") and path.endswith("/status"):
            waybill_id = self._parse_document_id(path)
            if waybill_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")
                return
            self._handle_waybill_status_update(handler, waybill_id)
            return
        if path == "/line-haul-contacts/create":
            self._handle_line_haul_contact_create(handler)
            return
        if path == "/line-haul-contacts/import-paste":
            self._handle_line_haul_contact_import_paste(handler)
            return
        if path.startswith("/line-haul-contacts/") and path.endswith("/update"):
            self._handle_line_haul_contact_update(handler, path)
            return
        if path == "/templates/select":
            self._handle_template_select(handler)
            return
        if path == "/templates/save":
            self._handle_template_save(handler)
            return
        if path == "/automations/resources/save":
            self._handle_automation_resource_save(handler)
            return
        if path == "/automations/tasks/save":
            self._handle_automation_task_save(handler)
            return
        if path == "/automations/tasks/run-now":
            self._handle_automation_task_run_now(handler)
            return
        if path == "/automations/tasks/cancel":
            self._handle_automation_task_cancel(handler)
            return
        session_route = self._automation_session_route(path)
        if session_route and self._handle_automation_session_post(handler, session_route[0], session_route[1]):
            return
        if path == "/automations/tms-session/send-code":
            self._handle_tms_session_action(
                handler,
                endpoint="/internal/v1/admin/tms/session/send-code",
                payload={},
                success_message="TMS融辉登录已提交；如出现图片验证码，请按图输入后提交。",
                timeout=90,
            )
            return
        if path == "/automations/tms-session/save-credentials":
            values = self._parse_urlencoded_form(handler)
            self._handle_tms_session_action(
                handler,
                endpoint="/internal/v1/admin/tms/session/credentials",
                payload={
                    "username": str(values.get("username", "") or "").strip(),
                    "password": str(values.get("password", "") or ""),
                    "phone": str(values.get("phone", "") or "").strip(),
                },
                success_message="TMS 默认登录配置已保存。",
                timeout=20,
            )
            return
        if path == "/automations/tms-session/clear-credentials":
            self._handle_tms_session_action(
                handler,
                endpoint="/internal/v1/admin/tms/session/credentials/clear",
                payload={},
                success_message="TMS 默认登录配置已清空。",
                timeout=20,
            )
            return
        if path == "/automations/tms-session/submit-code":
            values = self._parse_urlencoded_form(handler)
            sms_code = str(values.get("code", "") or "").strip()
            if not sms_code:
                self._respond_tms_action(
                    handler,
                    ok=False,
                    message="验证码不能为空。",
                    kind="warning",
                    http_status=HTTPStatus.BAD_REQUEST,
                )
                return
            self._handle_tms_session_action(
                handler,
                endpoint="/internal/v1/admin/tms/session/submit-code",
                payload={"code": sms_code},
                success_message="TMS 登录成功，共享登录态已更新。",
                timeout=45,
            )
            return
        if path == "/automations/tms-session/clear":
            self._handle_tms_session_action(
                handler,
                endpoint="/internal/v1/admin/tms/session/clear",
                payload={},
                success_message="TMS 登录态已清除。",
                timeout=20,
            )
            return
        if path == "/automations/admin/import-phase7-resources":
            self._handle_automation_admin_action(
                handler,
                endpoint="/internal/v1/admin/import-phase7-resources",
                success_message="Phase 7 资源已重新导入。",
            )
            return
        if path == "/automations/admin/seed-phase7-tasks":
            self._handle_automation_admin_action(
                handler,
                endpoint="/internal/v1/admin/seed-phase7-tasks",
                success_message="Phase 7 默认任务模板已写入并重载调度。",
            )
            return
        if path == "/automations/admin/reload":
            self._handle_automation_admin_action(
                handler,
                endpoint="/internal/v1/admin/reload",
                success_message="Agent 运行时配置已重载。",
            )
            return
        if path.startswith("/documents/") and path.endswith("/review"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._handle_review(handler, document_id)
            return
        if path.startswith("/documents/") and path.endswith("/reprocess"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._handle_reprocess(handler, document_id)
            return
        if path.startswith("/documents/") and path.endswith("/delete"):
            document_id = self._parse_document_id(path)
            if document_id is None:
                self._send_text(handler, HTTPStatus.NOT_FOUND, "Document not found.")
                return
            self._handle_delete(handler, document_id)
            return
        self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")

    def _render_portal(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        template = self.template_env.get_template("portal.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            settings=self.settings,
        )
        self._send_html(handler, body)


if __name__ == "__main__":
    load_console_environment()
    LocalDocFlowApp().run()
