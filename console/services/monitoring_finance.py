"""Console application services grouped by business responsibility."""

from console.app_support import *  # noqa: F403


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

    def _render_finance(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        user = getattr(handler, "current_admin_user", None) or current_admin_user() or {}
        is_super_admin = self._is_super_admin_user(user)
        can_manage = is_super_admin or "finance:operate" in (user.get("access_permissions") or ())
        template = self.template_env.get_template("finance.html")
        self._send_html(handler, template.render(
            app_title=self.settings.app_title, can_manage=can_manage, is_super_admin=is_super_admin,
        ))

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
