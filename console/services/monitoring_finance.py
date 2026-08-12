"""Console application services grouped by business responsibility."""

from console.app_support import *  # noqa: F403
from shared.finance.sources import enabled_finance_source_specs


class MonitoringFinanceServiceMixin:
    def _monitoring_snapshot_from_agent(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
        query = query or {}
        systems = str(query.get("systems", ["yunda,ronghui"])[0] or "yunda,ronghui").strip() or "yunda,ronghui"
        force_value = str(query.get("force", ["0"])[0] or "0").strip().lower()
        force = "1" if force_value in {"1", "true", "yes", "on"} else "0"
        prefer_cached_value = str(query.get("prefer_cached", ["0"])[0] or "0").strip().lower()
        prefer_cached = "1" if prefer_cached_value in {"1", "true", "yes", "on"} else "0"
        params = {"systems": systems, "force": force}
        if prefer_cached == "1":
            params["prefer_cached"] = prefer_cached
        endpoint = "/internal/v1/admin/monitoring/snapshot?" + urlencode(params)
        result = self._agent_request("GET", endpoint, timeout=75)
        if result.get("ok") and isinstance(result.get("data"), dict):
            payload = result["data"]
            if "ok" not in payload:
                payload = {"ok": True, **payload}
            return payload
        error = result.get("error")
        return {
            "ok": False,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "poll_interval_sec": 60,
            "message": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
            "totals": {
                "total_pending": 0,
                "yunda_pending": 0,
                "ronghui_pending": 0,
                "exception_pending": 0,
            },
            "systems": [],
        }

    def _handle_monitoring_summary(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        self._send_json(handler, HTTPStatus.OK, self._monitoring_snapshot_from_agent(query))

    def _monitoring_daily_sign_from_agent(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
        query = query or {}
        force_value = str(query.get("force", ["0"])[0] or "0").strip().lower()
        force = "1" if force_value in {"1", "true", "yes", "on"} else "0"
        params = {"force": force}
        target_date = str(query.get("target_date", [""])[0] or "").strip()
        if target_date:
            params["target_date"] = target_date
        prefer_cached_value = str(query.get("prefer_cached", ["0"])[0] or "0").strip().lower()
        if prefer_cached_value in {"1", "true", "yes", "on"}:
            params["prefer_cached"] = "1"
        endpoint = "/internal/v1/admin/monitoring/daily-sign?" + urlencode(params)
        result = self._agent_request("GET", endpoint, timeout=75)
        if result.get("ok") and isinstance(result.get("data"), dict):
            payload = result["data"]
            if "ok" not in payload:
                payload = {"ok": True, **payload}
            return payload
        error = result.get("error")
        return {
            "ok": False,
            "status": "error",
            "target_date": target_date,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "poll_interval_sec": 60,
            "counts": {"unsigned_today": 0},
            "message": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
        }

    def _handle_monitoring_daily_sign(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        self._send_json(handler, HTTPStatus.OK, self._monitoring_daily_sign_from_agent(query))

    def _handle_monitoring_stream(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()

        once = str(query.get("once", ["0"])[0] or "0").strip().lower() in {"1", "true", "yes"}
        while True:
            payload = self._monitoring_snapshot_from_agent(query)
            payload_text = json.dumps(payload, ensure_ascii=False)
            event = f"event: snapshot\ndata: {payload_text}\n\n".encode("utf-8")
            try:
                handler.wfile.write(event)
                flush = getattr(handler.wfile, "flush", None)
                if callable(flush):
                    flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
            if once:
                break
            interval = max(int(payload.get("poll_interval_sec") or 60), 30)
            time.sleep(interval)

    def _handle_monitoring_detail_link(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        def first(name: str) -> str:
            return str(query.get(name, [""])[0] or "").strip()

        payload = {
            "system": first("system"),
            "category_id": first("category_id"),
            "title": first("title"),
            "resource_id": first("resource_id"),
            "type_code": first("type_code"),
            "target_title": first("target_title"),
        }
        result = self._agent_request("POST", "/internal/v1/admin/monitoring/detail-link", payload=payload, timeout=20)
        if result.get("ok") and isinstance(result.get("data"), dict):
            self._send_json(handler, HTTPStatus.OK, result["data"])
            return
        error = result.get("error")
        self._send_json(
            handler,
            HTTPStatus.BAD_GATEWAY,
            {
                "ok": False,
                "error_code": "AGENT_UNAVAILABLE",
                "message": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
            },
        )

    def _render_ocr_workspace(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        self._render_document(handler, None, query)

    def _get_finance_service(self) -> FinanceService:
        service = getattr(self, "finance_service", None)
        if service is None:
            service = FinanceService(self.repository, agent_request=self._agent_request)
            self.finance_service = service
        return service

    def _render_finance(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        today = datetime.now().date()
        template = self.template_env.get_template("finance.html")
        platform_options = []
        for source in enabled_finance_source_specs():
            if not any(item["value"] == source.platform for item in platform_options):
                platform_options.append(
                    {"value": source.platform, "label": source.platform_label}
                )
        body = template.render(
            app_title=self.settings.app_title,
            today=today.isoformat(),
            month_start=today.replace(day=1).isoformat(),
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            finance_platforms=platform_options,
        )
        self._send_html(handler, body)

    def _handle_finance_get(
        self,
        handler: BaseHTTPRequestHandler,
        resource: str,
        query: dict[str, list[str]],
    ) -> None:
        service = self._get_finance_service()
        operations = {
            "summary": service.get_summary,
            "trend": service.get_trend,
            "entries": service.list_entries,
            "fee_mappings": service.list_fee_mappings,
            "sync_batches": service.list_sync_batches,
            "review_cases": service.list_review_cases,
            "waybill_facts": service.list_waybill_facts,
            "knowledge": lambda _query: service.knowledge_status(),
        }
        operation = operations.get(resource)
        if operation is None:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error_code": "FINANCE_ROUTE_NOT_FOUND", "message": "财务接口不存在。"},
            )
            return
        try:
            data = operation(query)
        except FinanceError as exc:
            self._send_finance_error(handler, exc)
            return
        except Exception as exc:
            LOGGER.exception("Finance GET failed: %s", type(exc).__name__)
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error_code": "FINANCE_INTERNAL_ERROR",
                    "message": "财务数据查询失败，请查看服务日志后重试。",
                },
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

    def _handle_finance_post(
        self,
        handler: BaseHTTPRequestHandler,
        action: str,
        *,
        path: str = "",
    ) -> None:
        service = self._get_finance_service()
        body = self._parse_json_body(handler)
        try:
            if action == "sync":
                data = service.start_sync(body)
            elif action == "backfill":
                data = service.start_backfill(body)
            elif action == "analyze_reviews":
                data = service.analyze_review_cases(body)
            elif action == "save_mapping":
                match = re.fullmatch(r"/finance/fee-mappings/(\d+)", path)
                if not match:
                    raise FinanceValidationError("费用项目 ID 无效。")
                admin = current_admin_user() or {}
                changed_by = str(admin.get("username") or admin.get("display_name") or "").strip()
                if not changed_by:
                    raise FinanceValidationError("无法识别当前操作人，请重新登录后再保存。")
                data = service.save_fee_mapping(int(match.group(1)), body, changed_by=changed_by)
            elif action == "reject_review":
                match = re.fullmatch(r"/finance/review-cases/(\d+)/reject", path)
                if not match:
                    raise FinanceValidationError("review case ID is invalid")
                admin = current_admin_user() or {}
                changed_by = str(admin.get("username") or admin.get("display_name") or "").strip()
                if not changed_by:
                    raise FinanceValidationError("administrator identity is unavailable")
                data = service.reject_review_case(int(match.group(1)), body, changed_by=changed_by)
            elif action == "retry_batch":
                match = re.fullmatch(r"/finance/sync-batches/(\d+)/retry", path)
                if not match:
                    raise FinanceValidationError("同步批次 ID 无效。")
                data = service.retry_batch(int(match.group(1)))
            else:
                self._send_json(
                    handler,
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error_code": "FINANCE_ROUTE_NOT_FOUND", "message": "财务接口不存在。"},
                )
                return
        except FinanceError as exc:
            self._send_finance_error(handler, exc)
            return
        except Exception as exc:
            LOGGER.exception("Finance POST failed: %s", type(exc).__name__)
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error_code": "FINANCE_INTERNAL_ERROR",
                    "message": "财务操作未完成，请查看服务日志后重试。",
                },
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

    def _send_finance_error(self, handler: BaseHTTPRequestHandler, error: FinanceError) -> None:
        try:
            status = HTTPStatus(int(getattr(error, "http_status", HTTPStatus.INTERNAL_SERVER_ERROR)))
        except ValueError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(
            handler,
            status,
            {
                "ok": False,
                "error_code": str(getattr(error, "error_code", "FINANCE_ERROR")),
                "message": str(error),
            },
        )

    def _render_module(self, handler: BaseHTTPRequestHandler, slug: str, query: dict) -> None:
        counts = self.repository.count_by_status()
        modules = self._build_module_view_models(counts)
        module = modules.get(slug)
        if not module:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Module not found.")
            return
        module = dict(module)
        module["dependencies"] = [modules[item]["name"] for item in module["dependencies"]]
        module["consumers"] = [modules[item]["name"] for item in module["consumers"]]
        related_modules = [
            modules[item]
            for item in modules
            if item != slug and (
                item in self.project_modules[slug].dependencies or item in self.project_modules[slug].consumers
            )
        ]
        template = self.template_env.get_template("module.html")
        body = template.render(
            app_title=self.settings.app_title,
            module=module,
            related_modules=related_modules,
            counts=counts,
            recent_documents=self.repository.list_documents(limit=5) if slug == "ocr" else [],
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)
