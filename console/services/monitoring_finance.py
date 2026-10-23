"""Console application services grouped by business responsibility."""

from console.app_support import *  # noqa: F403
from shared.finance.sources import enabled_finance_source_specs
from console.services.business_calls import call_business


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
                "total_pending": None,
                "yunda_pending": None,
                "ronghui_pending": None,
                "exception_pending": None,
            },
            "systems": [],
        }

    def _handle_monitoring_summary(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        self._send_json(handler, HTTPStatus.OK, self._monitoring_snapshot_from_agent(query))

    def _handle_monitoring_items(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        system = str(query.get("system", [""])[0] or "").strip()
        category_id = str(query.get("category_id", [""])[0] or "").strip()
        try:
            page = int(query.get("page", ["1"])[0])
        except (TypeError, ValueError):
            page = 0
        if system not in {"ronghui", "yunda"} or not 1 <= page <= 10_000:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {
                "ok": False, "error_code": "INVALID_MONITORING_QUERY",
                "message": "消息来源或页码无效。", "total": None, "items": [],
            })
            return
        force_value = str(query.get("force", ["0"])[0] or "0").strip().lower()
        params = {
            "system": system, "category_id": category_id, "page": str(page),
            "force": "1" if force_value in {"1", "true", "yes", "on"} else "0",
        }
        result = self._agent_request(
            "GET", "/internal/v1/admin/monitoring/items?" + urlencode(params), timeout=75,
        )
        if result.get("ok") and isinstance(result.get("data"), dict):
            self._send_json(handler, HTTPStatus.OK, result["data"])
            return
        error = result.get("error")
        status = result.get("status")
        self._send_json(handler, status if status in {400, 401, 403, 422} else HTTPStatus.BAD_GATEWAY, {
            "ok": False, "error_code": result.get("error_code") or "AGENT_UNAVAILABLE",
            "message": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False),
            "total": None, "items": [], "page": page, "page_size": 20,
        })

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
            service = FinanceService(self.repository)
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
        command_actions = {"sync", "backfill", "retry_batch"}
        trusted_context = None
        if action in command_actions:
            trusted_context = self._control_plane_write_context(handler)
            if trusted_context is None:
                return
            body = self._read_control_plane_json(handler)
            if body is None:
                return
        else:
            body = self._parse_json_body(handler)
        try:
            if action == "sync":
                arguments = service.build_sync_arguments(body)
            elif action == "backfill":
                arguments = service.build_backfill_arguments(body)
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
                arguments = service.build_retry_arguments(int(match.group(1)), body)
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
        if action not in command_actions:
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})
            return

        result = call_business(self, "finance-collect", arguments,
            trusted_context=trusted_context,
            request_id=str(handler.headers.get("X-Browser-Request-UUID") or ""),
            timeout_sec=600, write=True)
        success = result.get("ok") is True
        self._send_json(handler, HTTPStatus.OK if success else HTTPStatus.BAD_GATEWAY,
            {"ok": success, "data": result.get("data") or {},
             "message": "财务采集完成。" if success else str(result.get("error") or "财务采集未完成。"),
             "error_code": result.get("error_code")})

    def _send_finance_error(self, handler: BaseHTTPRequestHandler, error: FinanceError) -> None:
        try:
            status = HTTPStatus(int(getattr(error, "http_status", HTTPStatus.INTERNAL_SERVER_ERROR)))
        except ValueError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        payload = {
            "ok": False,
            "error_code": str(getattr(error, "error_code", "FINANCE_ERROR")),
            "message": str(error),
        }
        if getattr(error, "details", None) is not None:
            payload["details"] = error.details
        self._send_json(handler, status, payload)

    def _handle_finance_period_get(self, handler: BaseHTTPRequestHandler, month: str) -> None:
        user = getattr(handler, "current_admin_user", None) or {}
        try:
            data = self._get_finance_service().get_period(month)
        except FinanceError as exc:
            self._send_finance_error(handler, exc)
            return
        except Exception as exc:
            LOGGER.exception("Finance period GET failed: %s", type(exc).__name__)
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "error_code": "FINANCE_INTERNAL_ERROR",
                "message": "月度清点读取失败，请查看服务日志后重试。"})
            return
        data["viewer"] = {"can_close": str(user.get("control_plane_role") or "") == "super_admin"}
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

    def _handle_finance_period_details_get(
        self, handler: BaseHTTPRequestHandler, month: str, query: dict[str, list[str]]
    ) -> None:
        """Raw platform detail rows of one fee item, read-only."""

        try:
            data = self._get_finance_service().get_period_platform_details(month, query)
        except FinanceError as exc:
            self._send_finance_error(handler, exc)
            return
        except Exception as exc:
            LOGGER.exception("Finance period details GET failed: %s", type(exc).__name__)
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "error_code": "FINANCE_INTERNAL_ERROR",
                "message": "平台明细读取失败，请查看服务日志后重试。"})
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

    def _handle_finance_period_post(self, handler: BaseHTTPRequestHandler, action: str, target: str) -> None:
        """Confirm items, acknowledge an anomaly, close or reopen one month."""

        context = self._control_plane_write_context(handler)
        if context is None:
            return
        body = self._read_control_plane_json(handler)
        if body is None:
            return
        user = getattr(handler, "current_admin_user", None) or {}
        request_id = self._normalize_browser_request_uuid(body.get("request_id"))
        if not request_id or request_id != self._normalize_browser_request_uuid(
            handler.headers.get("X-Browser-Request-UUID")
        ):
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {
                "ok": False, "error_code": "INVALID_REQUEST", "message": "请求标识无效，请刷新页面后重试。"})
            return
        if action in {"close", "reopen"} and str(user.get("control_plane_role") or "") != "super_admin":
            self._send_json(handler, HTTPStatus.FORBIDDEN, {
                "ok": False, "error_code": "SUPER_ADMIN_REQUIRED", "message": "封账和反结账只允许超级管理员操作。"})
            return
        actor = str(user.get("username") or user.get("display_name") or "").strip()
        body = {**body, "request_id": request_id}
        service = self._get_finance_service()
        try:
            if not actor:
                raise FinanceValidationError("无法识别当前操作人，请重新登录后重试。")
            if action == "confirm":
                data = service.confirm_period_items(target, body, actor=actor)
            elif action == "close":
                data = service.close_period(target, body, actor=actor)
            elif action == "reopen":
                data = service.reopen_period(target, body, actor=actor)
            else:
                data = service.acknowledge_anomaly(int(target), body, actor=actor)
        except FinanceError as exc:
            self._send_finance_error(handler, exc)
            return
        except Exception as exc:
            LOGGER.exception("Finance period POST failed: %s", type(exc).__name__)
            self._send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {
                "ok": False, "error_code": "FINANCE_INTERNAL_ERROR",
                "message": "操作未完成，请刷新页面核对结果后重试。"})
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": data})

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
