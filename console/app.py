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


class LocalDocFlowApp(
    AuthServiceMixin,
    MonitoringFinanceServiceMixin,
    CustomerServiceMixin,
    WaybillsReceiptsServiceMixin,
    TmsProxyServiceMixin,
    AutomationServiceMixin,
    DocumentServiceMixin,
):
    routes = ConsoleRouteDispatcher()

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
        self.recovered_documents = self.service.recover_pending_documents()
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
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if not self._ensure_authorized(handler):
            return
        if self.routes.handle_write(self, handler, path, parsed.path, query, method.upper()):
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
        if self.routes.handle_public_get(self, handler, path, parsed.path, query):
            return
        if not self._ensure_authorized(handler):
            return
        if self.routes.handle_get(self, handler, path, parsed.path, query):
            return
        self._send_text(handler, HTTPStatus.NOT_FOUND, "Not found")

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        _CURRENT_ADMIN_USER.set(None)
        parsed = urlparse(handler.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if self.routes.handle_public_post(self, handler, path, parsed.path, query):
            return
        if not self._ensure_authorized(handler):
            return
        if self.routes.handle_post(self, handler, path, parsed.path, query):
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
