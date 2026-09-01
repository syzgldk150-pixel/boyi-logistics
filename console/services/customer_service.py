"""Console application services grouped by business responsibility."""

import uuid

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
        return customer_problem_clean_text(value)

    @classmethod
    def _customer_service_problem_field(cls, row: dict[str, Any], keys: tuple[str, ...]) -> str:
        del cls
        return customer_problem_field(row, keys)

    @classmethod
    def _customer_service_should_include_problem_row(cls, row: dict[str, Any]) -> bool:
        del cls
        return legacy_customer_problem_included(
            row,
            account_login=customer_problem_clean_text(row.get("account_login")),
        )

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

    def _call_customer_service_problem_agent(
        self,
        payload: dict[str, Any],
        *,
        trusted_context: dict[str, Any],
        browser_request_uuid: str,
        timeout_sec: int = 120,
    ) -> dict[str, Any]:
        normalized_uuid = self._normalize_browser_request_uuid(browser_request_uuid)
        actor = trusted_context.get("actor") if isinstance(trusted_context, dict) else None
        actor_id = str(actor.get("actor_id") or "").strip() if isinstance(actor, dict) else ""
        if not normalized_uuid or not actor_id:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "TRUSTED_READ_COMMAND_REQUIRED",
                "error": "缺少真实管理员身份或稳定请求标识，查询命令未提交。",
            }
        action = str(payload.get("action") or "query").strip().lower()
        account_id = str(payload.get("account_id") or "").strip()
        child_uuid = uuid.uuid5(
            uuid.UUID(normalized_uuid),
            f"customer_service_problem_{action}:{account_id}",
        )
        return self._agent_request(
            "POST",
            "/internal/v1/tms/customer_service_problem",
            payload={
                "params": payload,
                "timeout_sec": timeout_sec,
                "actor": actor,
                "actor_roles": list(trusted_context.get("actor_roles") or []),
                "source": "console",
                "idempotency_key": f"console:{actor_id}:tool.execute:{child_uuid}",
            },
            timeout=max(timeout_sec + 15, self.settings.agent_timeout_seconds),
            console_principal=trusted_context.get("_console_principal"),
        )

    @staticmethod
    def _customer_service_command_arguments(
        account: dict[str, Any],
        action: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the closed input contract for one governed write tool."""

        item = body.get("item") if isinstance(body.get("item"), dict) else {}
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        arguments: dict[str, Any] = {
            "platform": str(account.get("system") or "").strip().lower(),
            "account_id": str(account.get("account_id") or "").strip(),
            "account_label": str(
                account.get("name") or account.get("account_id") or ""
            ).strip(),
        }
        if action == "mark_read":
            arguments["external_id"] = str(
                item.get("external_id") or body.get("external_id") or ""
            ).strip()
        elif action == "reply":
            arguments.update(
                {
                    "external_id": str(item.get("external_id") or "").strip(),
                    "source_direction": str(item.get("source_direction") or "").strip(),
                    "waybill_no": str(item.get("waybill_no") or "").strip(),
                    "status": str(item.get("status") or "").strip(),
                    "reply_text": str(payload.get("reply_text") or "").strip(),
                    "prob_status": str(payload.get("prob_status") or "").strip(),
                    "old_prob_status": str(payload.get("old_prob_status") or "").strip(),
                }
            )
        elif action == "publish":
            if arguments["platform"] == "yunda":
                site_ids = payload.get("site_id")
                arguments["payload"] = {
                    "ship_no": str(payload.get("ship_no") or "").strip(),
                    "classes_type": str(payload.get("classes_type") or "").strip(),
                    "prob_text": str(payload.get("prob_text") or "").strip(),
                    "site_id": [
                        str(value).strip()
                        for value in (site_ids if isinstance(site_ids, list) else [])
                        if str(value).strip()
                    ],
                }
            else:
                arguments["payload"] = {
                    key: str(payload.get(key) or "").strip()
                    for key in (
                        "bill_code",
                        "problem_type",
                        "owner_problem_type",
                        "notice_site_code",
                        "notice_site",
                        "problem_cause",
                    )
                }
        return arguments

    @staticmethod
    def _customer_service_entity_refs(
        arguments: dict[str, Any],
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        external_id = str(arguments.get("external_id") or "").strip()
        waybill_no = str(arguments.get("waybill_no") or "").strip()
        platform = str(arguments.get("platform") or "").strip()
        if external_id:
            refs.append(
                {
                    "entity_type": "customer_service_problem",
                    "entity_id": external_id,
                    "source_system": platform,
                    "relation_type": "subject",
                    "metadata": {},
                }
            )
        if waybill_no:
            refs.append(
                {
                    "entity_type": "waybill",
                    "entity_id": waybill_no,
                    "source_system": platform,
                    "relation_type": "related",
                    "metadata": {},
                }
            )
        return refs

    @staticmethod
    def _unwrap_customer_service_agent_result(result: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if not result.get("ok"):
            error = result.get("error")
            if isinstance(error, dict):
                return None, str(error.get("error") or error.get("message") or error)
            return None, str(error or "智能服务调用失败。")
        data = result.get("data")
        if not isinstance(data, dict):
            return None, "智能服务返回了无效数据。"
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
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        browser_request_uuid = str(
            handler.headers.get("X-Browser-Request-UUID") or ""
        )
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
            result = self._call_customer_service_problem_agent(
                payload,
                trusted_context=trusted_context,
                browser_request_uuid=browser_request_uuid,
                timeout_sec=180,
            )
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
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        body = self._parse_json_body(handler)
        _accounts, account_map, warning = self._customer_service_account_maps(force=False)
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning})
            return
        account, error = self._resolve_customer_service_action_account(body, account_map)
        if error or not account:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error})
            return
        if action != "detail":
            tool_name = {
                "mark_read": "customer_service_problem_mark_read",
                "reply": "customer_service_problem_reply",
                "publish": "customer_service_problem_publish",
            }.get(action, "")
            if not tool_name:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "error_code": "UNSUPPORTED_CUSTOMER_SERVICE_ACTION",
                        "message": "该客服写操作未开放。",
                    },
                )
                return
            arguments = self._customer_service_command_arguments(account, action, body)
            result = self._submit_console_tool_command(
                trusted_context=trusted_context,
                browser_request_uuid=str(
                    handler.headers.get("X-Browser-Request-UUID") or ""
                ),
                tool_name=tool_name,
                arguments=arguments,
                entity_refs=self._customer_service_entity_refs(arguments),
                console_entry=f"/customer-service/problems/{action.replace('_', '-')}",
            )
            self._send_console_command_receipt(
                handler,
                result,
                message="客服写入计划已提交，请在事项中心完成审批并查看执行结果。",
            )
            return
        payload = self._customer_service_agent_payload(account, action, body)
        result = self._call_customer_service_problem_agent(
            payload,
            trusted_context=trusted_context,
            browser_request_uuid=str(
                handler.headers.get("X-Browser-Request-UUID") or ""
            ),
            timeout_sec=180,
        )
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
        trusted_context = self._control_plane_read_context(handler)
        if trusted_context is None:
            return
        source_url = str((query.get("src") or [""])[0] or "").strip()
        browser_request_uuid = str(
            (query.get("request_uuid") or [""])[0] or ""
        ).strip()
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
        result = self._call_customer_service_problem_agent(
            payload,
            trusted_context=trusted_context,
            browser_request_uuid=browser_request_uuid,
            timeout_sec=90,
        )
        data, error = self._unwrap_customer_service_agent_result(result)
        if error:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": error, "agent": data or {}})
            return
        encoded = str((data or {}).get("body_base64") or "")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except Exception:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": "智能服务返回的附件图片内容无效。"})
            return
        content_type = self._customer_service_raster_mime_type(image_bytes)
        if not content_type:
            self._send_json(
                handler,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"ok": False, "message": "附件不是受支持的栅格图片格式。"},
            )
            return
        filename = sanitize_filename(str((data or {}).get("filename") or "problem-attachment").strip()) or "problem-attachment"
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            image_bytes,
            content_type,
            cache_control="no-store",
            extra_headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @staticmethod
    def _customer_service_raster_mime_type(payload: bytes) -> str:
        if payload.startswith(b"\xff\xd8"):
            return "image/jpeg"
        if payload.startswith(b"\x89PNG"):
            return "image/png"
        if payload.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if payload.startswith(b"BM"):
            return "image/bmp"
        if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "image/webp"
        return ""

    def _handle_customer_service_attachment_upload(self, handler: BaseHTTPRequestHandler) -> None:
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        browser_request_uuid = self._normalize_browser_request_uuid(
            handler.headers.get("X-Browser-Request-UUID")
        )
        if not browser_request_uuid:
            self._send_console_command_receipt(
                handler,
                {
                    "ok": False,
                    "status": HTTPStatus.BAD_REQUEST,
                    "error_code": "BROWSER_REQUEST_UUID_REQUIRED",
                    "error": "缺少有效且稳定的浏览器请求标识，命令未提交。",
                },
                message="",
            )
            return
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

        upload_root = (
            getattr(self.settings, "runtime_dir", MODULE_DIR / "runtime")
            / "control_plane_uploads"
            / "customer_service"
        ).resolve()
        upload_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(str(file_item.filename or "")).suffix.lower()
        safe_name = sanitize_filename(Path(str(file_item.filename or "attachment")).name) or f"attachment{suffix}"
        target = (upload_root / f"{browser_request_uuid}_{safe_name}").resolve()
        try:
            target.relative_to(upload_root)
        except ValueError:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件文件名无效。"})
            return
        payload_bytes = file_item.file.read()
        if not payload_bytes:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": "附件文件为空。"})
            return
        target_preexisted = target.exists()
        if target_preexisted and hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(
            payload_bytes
        ).digest():
            self._send_json(
                handler,
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "error_code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    "message": "同一浏览器请求标识对应了不同附件，命令未提交。",
                },
            )
            return
        if not target_preexisted:
            target.write_bytes(payload_bytes)
        arguments = {
            "platform": str(account.get("system") or "").strip().lower(),
            "account_id": str(account.get("account_id") or "").strip(),
            "account_label": str(
                account.get("name") or account.get("account_id") or ""
            ).strip(),
            "file_path": str(target),
        }
        result = self._submit_console_tool_command(
            trusted_context=trusted_context,
            browser_request_uuid=browser_request_uuid,
            tool_name="customer_service_problem_upload_attachment",
            arguments=arguments,
            entity_refs=[],
            console_entry="/customer-service/problems/attachments/upload",
        )
        if not result.get("ok") and not target_preexisted:
            target.unlink(missing_ok=True)
        self._send_console_command_receipt(
            handler,
            result,
            message="附件上传计划已提交，请在事项中心完成审批并查看执行结果。",
        )
