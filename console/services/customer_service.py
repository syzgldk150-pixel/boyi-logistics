"""Console application services grouped by business responsibility."""

from console.app_support import *  # noqa: F403


class CustomerServiceMixin:
    @staticmethod
    def _customer_service_login_account(account: dict[str, Any]) -> str:
        credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        status = account.get("status") if isinstance(account.get("status"), dict) else {}
        for source in (account, credentials, status):
            for key in (
                "login_account",
                "login_username",
                "username",
                "user_name",
                "account_no",
                "account",
                "user_id",
            ):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    def _customer_service_public_accounts(self, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        public_accounts: list[dict[str, Any]] = []
        for item in accounts or []:
            if not isinstance(item, dict):
                continue
            system = str(item.get("system") or "").strip().lower()
            if system == "price":
                system = "ronghui"
            if system not in CUSTOMER_SERVICE_ALLOWED_ACCOUNT_SYSTEMS:
                continue
            account_id = str(item.get("account_id") or item.get("id") or "").strip()
            if not account_id:
                continue
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            public_accounts.append(
                {
                    "account_id": account_id,
                    "name": str(item.get("name") or account_id).strip(),
                    "login_account": self._customer_service_login_account(item),
                    "system": system,
                    "system_label": str(
                        item.get("system_label")
                        or AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(system, system)
                    ),
                    "account_purpose": str(item.get("account_purpose") or "").strip(),
                    "status_label": str(item.get("status_label") or status.get("label") or "").strip(),
                    "status_tone": str(item.get("status_tone") or status.get("status_tone") or "").strip(),
                    "status_note": str(item.get("status_note") or status.get("status_note") or "").strip(),
                    "session_capable": bool(item.get("session_capable")),
                    "has_saved_credentials": bool(item.get("has_saved_credentials") or status.get("has_saved_credentials")),
                    "credentials_label": str(item.get("credentials_label") or "").strip(),
                    "credentials_tone": str(item.get("credentials_tone") or "").strip(),
                }
            )
        return public_accounts

    def _customer_service_account_maps(self, *, force: bool = False) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
        accounts, warning = self._fetch_automation_accounts(force=force, prefer_cached=not force)
        public_accounts = self._customer_service_public_accounts(accounts)
        return public_accounts, {item["account_id"]: item for item in public_accounts}, warning

    @staticmethod
    def _customer_service_list(value: Any) -> list[str]:
        values = value if isinstance(value, list) else []
        normalized: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    @staticmethod
    def _customer_service_poll_interval(value: Any) -> int:
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = int(CUSTOMER_SERVICE_DEFAULT_SETTINGS["poll_interval_sec"])
        return min(max(parsed, 15), 600)

    def _sanitize_customer_service_settings(
        self,
        raw_settings: dict[str, Any] | None,
        account_map: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        raw = raw_settings if isinstance(raw_settings, dict) else {}
        allowed_by_system = {
            "ronghui": [item_id for item_id, item in account_map.items() if item.get("system") == "ronghui"],
            "yunda": [item_id for item_id, item in account_map.items() if item.get("system") == "yunda"],
        }

        def keep_ids(key: str, system: str) -> list[str]:
            selected = self._customer_service_list(raw.get(key))
            allowed = set(allowed_by_system.get(system) or [])
            return [item for item in selected if item in allowed]

        return {
            "ronghui_account_ids": keep_ids("ronghui_account_ids", "ronghui"),
            "yunda_account_ids": keep_ids("yunda_account_ids", "yunda"),
            "poll_interval_sec": self._customer_service_poll_interval(raw.get("poll_interval_sec")),
        }

    def _customer_service_settings(self, account_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
        try:
            row = self.repository.get_workflow_resource(CUSTOMER_SERVICE_RESOURCE_KEY)
        except Exception:
            row = None
        config = row.get("config") if isinstance(row, dict) and isinstance(row.get("config"), dict) else {}
        merged = {**CUSTOMER_SERVICE_DEFAULT_SETTINGS, **config}
        return self._sanitize_customer_service_settings(merged, account_map)

    @staticmethod
    def _customer_service_clean_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple, set)):
            return "、".join(
                item for item in (type(self)._customer_service_clean_text(part) for part in value) if item
            )
        if isinstance(value, dict):
            return ""
        return str(value).strip()

    @classmethod
    def _customer_service_problem_field(cls, row: dict[str, Any], keys: tuple[str, ...]) -> str:
        sources: list[dict[str, Any]] = [row]
        raw = row.get("raw")
        if isinstance(raw, dict):
            sources.append(raw)
        for source in sources:
            for key in keys:
                value = cls._customer_service_clean_text(source.get(key))
                if value:
                    return value
        return ""

    @classmethod
    def _customer_service_should_include_problem_row(cls, row: dict[str, Any]) -> bool:
        account_login = cls._customer_service_clean_text(row.get("account_login"))
        if account_login != CUSTOMER_SERVICE_SITE_FILTER_LOGIN:
            return True
        publish_site = cls._customer_service_problem_field(row, CUSTOMER_SERVICE_PUBLISH_SITE_KEYS)
        notified_site = cls._customer_service_problem_field(row, CUSTOMER_SERVICE_NOTIFIED_SITE_KEYS)
        return publish_site == CUSTOMER_SERVICE_SITE_FILTER_SITE and notified_site == CUSTOMER_SERVICE_SITE_FILTER_SITE

    def _render_customer_service(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        accounts, account_map, warning = self._customer_service_account_maps(force=False)
        settings = self._customer_service_settings(account_map)
        template = self.template_env.get_template("customer_service.html")
        body = template.render(
            app_title=self.settings.app_title,
            accounts=accounts,
            settings=settings,
            account_warning=warning,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _handle_customer_service_problem_settings_get(self, handler: BaseHTTPRequestHandler) -> None:
        accounts, account_map, warning = self._customer_service_account_maps(force=False)
        settings = self._customer_service_settings(account_map)
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": not bool(warning),
                "message": warning,
                "settings": settings,
                "accounts": accounts,
            },
        )

    def _handle_customer_service_problem_settings_post(self, handler: BaseHTTPRequestHandler) -> None:
        body = self._parse_json_body(handler)
        accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        settings = self._sanitize_customer_service_settings(body, account_map)
        try:
            self.repository.upsert_workflow_resource(
                CUSTOMER_SERVICE_RESOURCE_KEY,
                settings,
                source="customer_service",
            )
        except Exception as exc:
            self._send_json(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "message": f"客服问题件设置保存失败：{exc}"},
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "settings": settings, "accounts": accounts})

    def _customer_service_selected_accounts(
        self,
        body: dict[str, Any],
        account_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        platforms = {
            str(item or "").strip().lower()
            for item in (body.get("platforms") if isinstance(body.get("platforms"), list) else [])
            if str(item or "").strip()
        }
        platforms = platforms or set(CUSTOMER_SERVICE_ALLOWED_ACCOUNT_SYSTEMS)
        raw_account_ids = self._customer_service_list(body.get("account_ids"))
        if not raw_account_ids:
            settings = self._customer_service_settings(account_map)
            raw_account_ids = [
                *settings.get("ronghui_account_ids", []),
                *settings.get("yunda_account_ids", []),
            ]
        selected: list[dict[str, Any]] = []
        for account_id in raw_account_ids:
            account = account_map.get(account_id)
            if not account:
                continue
            if account.get("system") not in platforms:
                continue
            selected.append(account)
        return selected

    def _customer_service_agent_payload(self, account: dict[str, Any], action: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "platform": account["system"],
            "account_id": account["account_id"],
            "account_label": account.get("name") or account["account_id"],
            "account_login": account.get("login_account") or "",
            "action": action,
            "filters": body.get("filters") if isinstance(body.get("filters"), dict) else {},
            "item": body.get("item") if isinstance(body.get("item"), dict) else {},
            "payload": body.get("payload") if isinstance(body.get("payload"), dict) else {},
        }

    def _call_customer_service_problem_agent(self, payload: dict[str, Any], *, timeout_sec: int = 120) -> dict[str, Any]:
        return self._agent_request(
            "POST",
            "/internal/v1/tms/customer_service_problem",
            payload={"params": payload, "timeout_sec": timeout_sec},
            timeout=max(timeout_sec + 15, self.settings.agent_timeout_seconds),
        )

    @staticmethod
    def _unwrap_customer_service_agent_result(result: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if not result.get("ok"):
            error = result.get("error")
            if isinstance(error, dict):
                return None, str(error.get("error") or error.get("message") or error)
            return None, str(error or "Agent 调用失败。")
        data = result.get("data")
        if not isinstance(data, dict):
            return None, "Agent 返回了无效数据。"
        if isinstance(data.get("data"), dict):
            nested = data["data"]
            if (
                "rows" in nested
                or "result" in nested
                or "details" in nested
                or "body_base64" in nested
                or "content_type" in nested
                or nested.get("ok") is False
            ):
                data = nested
        if data.get("ok") is False:
            return data, str(data.get("message") or data.get("error") or "问题件接口调用失败。")
        return data, ""

    def _handle_customer_service_problem_query(self, handler: BaseHTTPRequestHandler) -> None:
        body = self._parse_json_body(handler)
        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning, "rows": []})
            return
        selected_accounts = self._customer_service_selected_accounts(body, account_map)
        if not selected_accounts:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请先选择融辉或韵达账号。", "rows": []})
            return

        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def fetch_one(account: dict[str, Any]) -> dict[str, Any]:
            payload = self._customer_service_agent_payload(account, "query", body)
            result = self._call_customer_service_problem_agent(payload, timeout_sec=180)
            data, error = self._unwrap_customer_service_agent_result(result)
            return {"account": account, "payload": payload, "data": data, "error": error}

        with ThreadPoolExecutor(max_workers=min(max(len(selected_accounts), 1), 6)) as executor:
            futures = [executor.submit(fetch_one, account) for account in selected_accounts]
            for future in as_completed(futures):
                item = future.result()
                account = item["account"]
                data = item["data"]
                if item["error"]:
                    errors.append(
                        {
                            "platform": account["system"],
                            "account_id": account["account_id"],
                            "account_label": account.get("name") or account["account_id"],
                            "account_login": account.get("login_account") or "",
                            "message": item["error"],
                            "error_code": (data or {}).get("error_code") if isinstance(data, dict) else "",
                        }
                    )
                    continue
                for row in (data or {}).get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    if not str(row.get("external_id") or "").strip():
                        errors.append(
                            {
                                "platform": account["system"],
                                "account_id": account["account_id"],
                                "account_label": account.get("name") or account["account_id"],
                                "account_login": account.get("login_account") or "",
                                "message": "原系统返回问题件缺少 external_id，已跳过该账号结果。",
                                "error_code": "MISSING_EXTERNAL_ID",
                            }
                        )
                        rows = [existing for existing in rows if existing.get("account_id") != account["account_id"]]
                        break
                    normalized = dict(row)
                    normalized.setdefault("platform", account["system"])
                    normalized.setdefault("account_id", account["account_id"])
                    normalized.setdefault("account_label", account.get("name") or account["account_id"])
                    normalized.setdefault("account_login", account.get("login_account") or "")
                    if not self._customer_service_should_include_problem_row(normalized):
                        continue
                    rows.append(normalized)

        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": not errors,
                "rows": rows,
                "errors": errors,
                "stats": {
                    "account_count": len(selected_accounts),
                    "row_count": len(rows),
                    "error_count": len(errors),
                },
            },
        )

    def _resolve_customer_service_action_account(
        self,
        body: dict[str, Any],
        account_map: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str]:
        account_id = str(body.get("account_id") or (body.get("item") or {}).get("account_id") or "").strip()
        platform = str(body.get("platform") or (body.get("item") or {}).get("platform") or "").strip().lower()
        account = account_map.get(account_id)
        if not account:
            return None, "问题件处理必须带回原账号 account_id。"
        if platform and account.get("system") != platform:
            return None, "问题件平台与账号不一致，已停止提交。"
        return account, ""

    def _handle_customer_service_problem_agent_action(self, handler: BaseHTTPRequestHandler, action: str) -> None:
        body = self._parse_json_body(handler)
        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        account, error = self._resolve_customer_service_action_account(body, account_map)
        if error or not account:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
            return
        payload = self._customer_service_agent_payload(account, action, body)
        result = self._call_customer_service_problem_agent(payload, timeout_sec=180)
        data, error = self._unwrap_customer_service_agent_result(result)
        if error:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": error, "agent": data or {}})
            return
        self._send_json(handler, HTTPStatus.OK, data or {"ok": True})

    def _handle_customer_service_attachment_preview(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict[str, list[str]],
    ) -> None:
        source_url = str((query.get("src") or [""])[0] or "").strip()
        body = {
            "platform": str((query.get("platform") or [""])[0] or "").strip(),
            "account_id": str((query.get("account_id") or [""])[0] or "").strip(),
            "item": {},
            "payload": {"source_url": source_url},
        }
        if not source_url:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件图片地址为空。"})
            return
        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        account, error = self._resolve_customer_service_action_account(body, account_map)
        if error or not account:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
            return
        payload = self._customer_service_agent_payload(account, "fetch_attachment", body)
        result = self._call_customer_service_problem_agent(payload, timeout_sec=90)
        data, error = self._unwrap_customer_service_agent_result(result)
        if error:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": error, "agent": data or {}})
            return
        encoded = str((data or {}).get("body_base64") or "")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except Exception:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": "Agent 返回的附件图片内容无效。"})
            return
        content_type = str((data or {}).get("content_type") or "image/jpeg").split(";", 1)[0].strip() or "image/jpeg"
        filename = sanitize_filename(str((data or {}).get("filename") or "problem-attachment").strip()) or "problem-attachment"
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            image_bytes,
            content_type,
            cache_control="no-store",
            extra_headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    def _handle_customer_service_attachment_upload(self, handler: BaseHTTPRequestHandler) -> None:
        form = self._parse_multipart_form(handler)
        file_item = form["file"] if "file" in form else None
        if file_item is None or not getattr(file_item, "filename", ""):
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请选择附件文件。"})
            return
        body = {
            "platform": str(form.getvalue("platform") or "").strip(),
            "account_id": str(form.getvalue("account_id") or "").strip(),
            "item": {},
            "payload": {},
        }
        if "item" in form:
            try:
                item_value = json.loads(str(form.getvalue("item") or "{}"))
                if isinstance(item_value, dict):
                    body["item"] = item_value
            except Exception:
                body["item"] = {}

        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        account, error = self._resolve_customer_service_action_account(body, account_map)
        if error or not account:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
            return

        upload_root = (getattr(self.settings, "temp_dir", MODULE_DIR / "runtime" / "artifacts" / "temp") / "customer_service").resolve()
        upload_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(str(file_item.filename or "")).suffix.lower()
        safe_name = sanitize_filename(Path(str(file_item.filename or "attachment")).name) or f"attachment{suffix}"
        target = (upload_root / f"{secrets.token_hex(12)}_{safe_name}").resolve()
        try:
            target.relative_to(upload_root)
        except ValueError:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件文件名无效。"})
            return
        payload_bytes = file_item.file.read()
        if not payload_bytes:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件文件为空。"})
            return
        target.write_bytes(payload_bytes)
        body["payload"] = {
            "file_path": str(target),
            "file_name": safe_name,
            "delete_after_upload": True,
            "scene": str(form.getvalue("scene") or "").strip(),
        }
        payload = self._customer_service_agent_payload(account, "upload_attachment", body)
        result = self._call_customer_service_problem_agent(payload, timeout_sec=180)
        data, error = self._unwrap_customer_service_agent_result(result)
        if target.exists():
            target.unlink(missing_ok=True)
        if error:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": error, "agent": data or {}})
            return
        self._send_json(handler, HTTPStatus.OK, data or {"ok": True})
