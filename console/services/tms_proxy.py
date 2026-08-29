"""Console application services grouped by business responsibility."""

from console.app_support import *  # noqa: F403
from shared.manual_entry_contracts import canonical_manual_proxy_path


class TmsProxyServiceMixin:
    _ORIGINAL_PAGE_TICKET_TTL_SECONDS = 30
    _ORIGINAL_PAGE_CAPABILITY_TTL_SECONDS = 30 * 60

    def _original_page_state(self) -> tuple[threading.Lock, dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        lock = getattr(self, "_original_page_state_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._original_page_state_lock = lock
            self._original_page_tickets = {}
            self._original_page_capabilities = {}
        return lock, self._original_page_tickets, self._original_page_capabilities

    @staticmethod
    def _request_host(handler: BaseHTTPRequestHandler) -> str:
        return str(handler.headers.get("Host") or "").strip().lower().split(":", 1)[0]

    def _active_admin_session_for_original_page(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        session = self.repository.get_admin_session(session_id)
        if not session or not bool(session.get("is_active")):
            return None
        if self._coerce_datetime(session.get("expires_at")) <= datetime.now():
            return None
        return session

    def _prune_original_page_state(self, now: float) -> None:
        lock, tickets, capabilities = self._original_page_state()
        with lock:
            for store in (tickets, capabilities):
                expired = [key for key, value in store.items() if float(value.get("expires_at") or 0) <= now]
                for key in expired:
                    store.pop(key, None)

    def _mint_isolated_original_page_ticket(
        self,
        handler: BaseHTTPRequestHandler,
        provider: str,
    ) -> None:
        if provider not in ORIGINAL_PAGE_PREFIXES:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Original page provider not found.")
            return
        session_id = self._session_id_from_cookie(handler)
        if not self._active_admin_session_for_original_page(session_id):
            self._send_json(
                handler,
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error_code": "MYSQL_ADMIN_SESSION_REQUIRED", "message": "请使用后台登录会话打开原页。"},
            )
            return
        now = time.time()
        self._prune_original_page_state(now)
        ticket = secrets.token_urlsafe(32)
        lock, tickets, _ = self._original_page_state()
        with lock:
            tickets[ticket] = {
                "provider": provider,
                "session_id": session_id,
                "expires_at": now + self._ORIGINAL_PAGE_TICKET_TTL_SECONDS,
            }
        prefix = ORIGINAL_PAGE_PREFIXES[provider]
        self._redirect(handler, f"{ORIGINAL_PAGE_ISOLATED_ORIGIN}{prefix}/?ticket={quote(ticket)}")

    def _original_page_capability_from_cookie(
        self,
        handler: BaseHTTPRequestHandler,
        provider: str,
    ) -> str:
        raw_cookie = str(handler.headers.get("Cookie") or "")
        if not raw_cookie:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return ""
        morsel = cookie.get(f"shipnow_original_{provider}")
        return str(morsel.value if morsel else "")

    def _exchange_original_page_ticket(
        self,
        handler: BaseHTTPRequestHandler,
        provider: str,
        ticket: str,
    ) -> None:
        now = time.time()
        self._prune_original_page_state(now)
        lock, tickets, capabilities = self._original_page_state()
        with lock:
            ticket_state = tickets.pop(ticket, None)
        if (
            not ticket_state
            or ticket_state.get("provider") != provider
            or float(ticket_state.get("expires_at") or 0) <= now
        ):
            self._send_text(handler, HTTPStatus.UNAUTHORIZED, "原页授权已失效，请从运单录入重新打开。")
            return
        session_id = str(ticket_state.get("session_id") or "")
        session = self._active_admin_session_for_original_page(session_id)
        if not session:
            self._send_text(handler, HTTPStatus.UNAUTHORIZED, "后台登录会话已失效，请重新登录。")
            return
        session_expires_at = self._coerce_datetime(session.get("expires_at")).timestamp()
        expires_at = min(session_expires_at, now + self._ORIGINAL_PAGE_CAPABILITY_TTL_SECONDS)
        capability = secrets.token_urlsafe(32)
        with lock:
            capabilities[capability] = {
                "provider": provider,
                "session_id": session_id,
                "expires_at": expires_at,
            }
        max_age = max(1, int(expires_at - now))
        prefix = ORIGINAL_PAGE_PREFIXES[provider]
        cookie_header = (
            f"shipnow_original_{provider}={capability}; Path={prefix}; HttpOnly; "
            f"Secure; SameSite=Strict; Max-Age={max_age}"
        )
        self._redirect(handler, f"{prefix}/", headers=[("Set-Cookie", cookie_header)])

    def _authorize_isolated_original_page(
        self,
        handler: BaseHTTPRequestHandler,
        provider: str,
    ) -> bool:
        now = time.time()
        self._prune_original_page_state(now)
        capability = self._original_page_capability_from_cookie(handler, provider)
        lock, _, capabilities = self._original_page_state()
        with lock:
            state = dict(capabilities.get(capability) or {})
        session = self._active_admin_session_for_original_page(str(state.get("session_id") or ""))
        if (
            not state
            or state.get("provider") != provider
            or float(state.get("expires_at") or 0) <= now
            or not session
        ):
            self._send_text(handler, HTTPStatus.UNAUTHORIZED, "原页授权已失效，请从运单录入重新打开。")
            return False
        self._set_current_admin_user(
            handler,
            {
                "id": int(session.get("user_id") or 0),
                "username": str(session.get("username") or ""),
                "display_name": str(session.get("display_name") or ""),
                "avatar_path": str(session.get("avatar_path") or ""),
                "avatar_url": self._admin_avatar_url(str(session.get("avatar_path") or "")),
                "ui_preferences_json": str(session.get("ui_preferences_json") or "{}"),
                "control_plane_role": str(session.get("control_plane_role") or "admin"),
                "role": str(session.get("role") or "admin"),
                "is_legacy_basic_auth": False,
            },
        )
        return True

    def _handle_isolated_original_page_request(
        self,
        handler: BaseHTTPRequestHandler,
        parsed: Any,
        *,
        method: str,
    ) -> bool:
        if self._request_host(handler) != ORIGINAL_PAGE_ISOLATED_HOST:
            return False
        path = str(parsed.path or "")
        provider = next(
            (
                name
                for name, prefix in ORIGINAL_PAGE_PREFIXES.items()
                if path == prefix or path.startswith(prefix + "/")
            ),
            "",
        )
        if not provider:
            self._redirect(handler, f"{ORIGINAL_PAGE_PRIMARY_ORIGIN}/")
            return True
        query = parse_qs(parsed.query)
        prefix = ORIGINAL_PAGE_PREFIXES[provider]
        ticket = str((query.get("ticket") or [""])[0]).strip()
        if ticket:
            if method.upper() != "GET" or path.rstrip("/") != prefix:
                self._send_text(handler, HTTPStatus.BAD_REQUEST, "Invalid original page ticket request.")
                return True
            self._exchange_original_page_ticket(handler, provider, ticket)
            return True
        if not self._authorize_isolated_original_page(handler, provider):
            return True
        if method.upper() != "GET":
            origin = str(handler.headers.get("Origin") or "").strip().rstrip("/")
            referer = str(handler.headers.get("Referer") or "").strip()
            origin_allowed = origin == ORIGINAL_PAGE_ISOLATED_ORIGIN if origin else None
            referer_allowed = referer.startswith(ORIGINAL_PAGE_ISOLATED_ORIGIN + prefix + "/")
            if origin_allowed is False or (origin_allowed is None and not referer_allowed):
                self._send_text(handler, HTTPStatus.FORBIDDEN, "Original page write origin rejected.")
                return True
            # The isolated origin uses /original/{provider}, while lifecycle
            # ownership is intentionally defined by the canonical Console
            # /original-pages prefix.  Check that owner after capability/origin
            # authorization and before any request can reach the Agent proxy.
            canonical_module_path = f"/original-pages/{provider}"
            if self._reject_unavailable_business_module_request(
                handler,
                canonical_module_path,
                method=method,
            ):
                return True
        if provider == "yunda":
            self._handle_yunda_live_proxy(
                handler,
                path,
                method=method,
                query=query,
                proxy_prefix=prefix,
                frame_ancestor_origin=ORIGINAL_PAGE_PRIMARY_ORIGIN,
            )
        else:
            self._handle_ronghui_live_proxy(
                handler,
                path,
                method=method,
                query=query,
                proxy_prefix=prefix,
                frame_ancestor_origin=ORIGINAL_PAGE_PRIMARY_ORIGIN,
            )
        return True

    def _active_original_page_proxy_disabled(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
    ) -> bool:
        raw_path = str(path or "")
        prefixes = (
            "/ocr/yunda",
            RONGHUI_RECEIPT_LIVE_PROXY_PREFIX,
            YUNDA_RECEIPT_LIVE_PROXY_PREFIX,
            RONGHUI_LIVE_PROXY_PREFIX,
            YUNDA_LIVE_PROXY_PREFIX,
        )
        if not any(
            raw_path == prefix or raw_path.startswith(prefix + "/")
            for prefix in prefixes
        ):
            return False
        self._send_json(
            handler,
            HTTPStatus.GONE,
            {
                "ok": False,
                "error_code": "ACTIVE_ORIGINAL_PAGE_DISABLED",
                "message": (
                    "第三方活动原页已安全停用；其脚本不能在 Console 管理员同源上下文执行。"
                ),
            },
        )
        return True

    def _parse_json_body(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        content_length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(content_length) if content_length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    def _read_request_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        content_length = int(handler.headers.get("Content-Length", 0) or 0)
        return handler.rfile.read(content_length) if content_length else b""

    def _yunda_live_remote_path(self, path: str, *, proxy_prefix: str = YUNDA_LIVE_PROXY_PREFIX) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == proxy_prefix:
            return YUNDA_LIVE_ENTRY_PATH
        if not raw_path.startswith(proxy_prefix + "/"):
            return ""
        remote_path = canonical_manual_proxy_path(
            "/" + raw_path[len(proxy_prefix) :].lstrip("/")
        )
        return remote_path if remote_path.startswith("/ky_inms/public/") else ""

    def _yunda_receipt_live_remote_path(self, path: str) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == YUNDA_RECEIPT_LIVE_PROXY_PREFIX:
            return YUNDA_RECEIPT_LIVE_ENTRY_PATH
        if not raw_path.startswith(YUNDA_RECEIPT_LIVE_PROXY_PREFIX + "/"):
            return ""
        remote_path = canonical_manual_proxy_path(
            "/" + raw_path[len(YUNDA_RECEIPT_LIVE_PROXY_PREFIX) :].lstrip("/")
        )
        return remote_path if remote_path.startswith("/ky_inms/public/") else ""

    def _ronghui_live_remote_path(self, path: str, *, proxy_prefix: str = RONGHUI_LIVE_PROXY_PREFIX) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == proxy_prefix:
            return ""
        if not raw_path.startswith(proxy_prefix + "/"):
            return ""
        remote_path = canonical_manual_proxy_path(
            "/" + raw_path[len(proxy_prefix) :].lstrip("/")
        )
        return remote_path if any(remote_path.startswith(prefix) for prefix in RONGHUI_LIVE_ALLOWED_PREFIXES) else ""

    def _ronghui_receipt_live_remote_path(self, path: str) -> str:
        raw_path = str(path or "").strip()
        if raw_path.rstrip("/") == RONGHUI_RECEIPT_LIVE_PROXY_PREFIX:
            return ""
        if not raw_path.startswith(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX + "/"):
            return ""
        remote_path = canonical_manual_proxy_path(
            "/" + raw_path[len(RONGHUI_RECEIPT_LIVE_PROXY_PREFIX) :].lstrip("/")
        )
        return remote_path if any(remote_path.startswith(prefix) for prefix in RONGHUI_LIVE_ALLOWED_PREFIXES) else ""

    def _flatten_query(self, query: dict[str, list[str]]) -> str:
        pairs: list[tuple[str, str]] = []
        for key, values in (query or {}).items():
            if isinstance(values, list):
                pairs.extend((str(key), str(value)) for value in values)
            else:
                pairs.append((str(key), str(values)))
        return urlencode(pairs, doseq=True)

    def _ronghui_receipt_entry_menu_text(self, query: dict[str, list[str]]) -> str:
        values = (query or {}).get("receipt_entry") or ["send"]
        raw_entry = values[0] if isinstance(values, list) and values else values
        entry = str(raw_entry or "send").strip().lower()
        return RONGHUI_RECEIPT_ENTRY_MENU_TEXTS.get(entry, RONGHUI_RECEIPT_ENTRY_MENU_TEXTS["send"])

    def _handler_headers(self, handler: BaseHTTPRequestHandler) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in getattr(handler, "headers", {}).items():
            headers[str(key)] = str(value)
        return headers

    def _unwrap_yunda_live_proxy_payload(self, result: dict[str, Any]) -> dict[str, Any] | None:
        outer = result.get("data")
        if not isinstance(outer, dict):
            return None
        nested = outer.get("data")
        if isinstance(nested, dict) and ("body_base64" in nested or "status_code" in nested):
            return nested
        if "body_base64" in outer or "status_code" in outer:
            return outer
        return None

    def _decode_proxy_body(self, proxy_payload: dict[str, Any]) -> bytes:
        raw_base64 = str(proxy_payload.get("body_base64") or "")
        if not raw_base64:
            return b""
        try:
            return base64.b64decode(raw_base64)
        except Exception:
            return b""

    def _header_value(self, headers: dict[str, Any], name: str) -> str:
        for key, value in (headers or {}).items():
            if str(key).lower() == name.lower():
                return str(value)
        return ""

    def _parse_urlencoded_form_body(self, body: bytes, content_type: str) -> dict[str, str]:
        charset = "utf-8"
        match = re.search(r"charset=([A-Za-z0-9._-]+)", str(content_type or ""), flags=re.IGNORECASE)
        if match:
            charset = match.group(1)
        parsed = parse_qs(body.decode(charset, errors="replace"), keep_blank_values=True)
        return {str(key): str(values[-1] if values else "") for key, values in parsed.items()}

    def _decode_proxy_json_body(self, proxy_payload: dict[str, Any]) -> dict[str, Any]:
        headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        content_type = self._header_value(headers, "Content-Type")
        raw_body = self._decode_proxy_body(proxy_payload)
        text = raw_body.decode("utf-8", errors="replace").strip()
        if "json" not in content_type.lower() and not text.startswith("{"):
            return {}
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _persist_yunda_live_save_result(
        self,
        *,
        request_body: bytes,
        request_content_type: str,
        proxy_payload: dict[str, Any],
    ) -> dict[str, Any]:
        response_json = self._decode_proxy_json_body(proxy_payload)
        if not response_json:
            return {}
        save_ok = str(response_json.get("info") or "").strip() == "1" or response_json.get("ok") is True
        if not save_ok:
            return {}
        form_fields = self._parse_urlencoded_form_body(request_body, request_content_type)
        normalized_form = {**form_fields}
        for key, value in response_json.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                normalized_form[str(key)] = "" if value is None else str(value)
        remote_waybill_no = str(response_json.get("LogisticsId") or normalized_form.get("LogisticsId") or "").strip()
        mapped = build_console_waybill_from_yunda_data(normalized_form, remote_waybill_no=remote_waybill_no)
        if not mapped:
            return {}
        waybill = self.repository.upsert_provider_waybill(mapped, source="yunda")
        waybill_id = int(waybill.get("id", 0) or 0) if waybill else None
        self.repository.create_waybill_provider_snapshot(
            provider="yunda",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_request",
            payload={
                "action": "live_proxy_save",
                "normalized_form": normalized_form,
                "content_type": request_content_type,
            },
            waybill_id=waybill_id,
        )
        self.repository.create_waybill_provider_snapshot(
            provider="yunda",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_response",
            payload={
                "action": "live_proxy_save",
                "response": response_json,
                "remote_path": proxy_payload.get("remote_path", ""),
            },
            waybill_id=waybill_id,
        )
        if not waybill_id:
            return {}
        return {
            "shipnow_local_waybill_id": waybill_id,
            "shipnow_print_url": f"/waybills/{waybill_id}/print?preview=1",
            "shipnow_autoprint_url": f"/waybills/{waybill_id}/print?autoprint=1",
        }

    def _patch_proxy_json_body(self, proxy_payload: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        if not patch:
            return proxy_payload
        response_json = self._decode_proxy_json_body(proxy_payload)
        if not response_json:
            return proxy_payload
        patched = dict(response_json)
        patched.update(patch)
        updated_payload = dict(proxy_payload)
        updated_payload["body_base64"] = base64.b64encode(
            json.dumps(patched, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        return updated_payload

    def _persist_ronghui_live_save_result(
        self,
        *,
        request_body: bytes,
        request_content_type: str,
        proxy_payload: dict[str, Any],
    ) -> None:
        response_json = self._decode_proxy_json_body(proxy_payload)
        if not response_json:
            return
        message = str(response_json.get("message") or response_json.get("msg") or "").strip()
        save_ok = response_json.get("success") is True or response_json.get("ok") is True or "成功" in message
        if not save_ok:
            return
        form_fields = self._parse_urlencoded_form_body(request_body, request_content_type)
        remote_waybill_no = str(response_json.get("BILL_CODE") or form_fields.get("BILL_CODE") or "").strip()
        self.repository.create_waybill_provider_snapshot(
            provider="ronghui",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_request",
            payload={
                "action": "live_proxy_save",
                "form_fields": form_fields,
                "content_type": request_content_type,
            },
            waybill_id=None,
        )
        self.repository.create_waybill_provider_snapshot(
            provider="ronghui",
            remote_waybill_no=remote_waybill_no,
            snapshot_kind="save_response",
            payload={
                "action": "live_proxy_save",
                "response": response_json,
                "remote_path": proxy_payload.get("remote_path", ""),
            },
            waybill_id=None,
        )

    def _send_proxy_bytes(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        payload: bytes,
        headers: dict[str, Any],
        *,
        frame_ancestor_origin: str = "",
    ) -> None:
        content_type = self._header_value(headers, "Content-Type") or "application/octet-stream"
        cache_control = self._header_value(headers, "Cache-Control") or "no-store"
        blocked_headers = {
            "content-type",
            "content-length",
            "transfer-encoding",
            "set-cookie",
            "cache-control",
        }
        if frame_ancestor_origin:
            blocked_headers.update({"x-frame-options", "content-security-policy"})
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            key_text = str(key)
            if key_text.lower() in blocked_headers:
                continue
            handler.send_header(key_text, str(value))
        if frame_ancestor_origin:
            handler.send_header("Content-Security-Policy", f"frame-ancestors {frame_ancestor_origin}")
        handler.send_header("Cache-Control", cache_control)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _handle_yunda_receipt_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        if method.upper() != "GET":
            self._send_json(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {
                    "ok": False,
                    "error_code": "RECEIPT_PROXY_WRITE_DISABLED",
                    "message": "回单原页代理仅供只读查看；审核请通过事项中心提交。",
                },
            )
            return
        trusted_context = self._control_plane_read_context(handler)
        if trusted_context is None:
            return
        remote_path = self._yunda_receipt_live_remote_path(path)
        if not remote_path:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "韵达回单原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": self._flatten_query(query),
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": YUNDA_RECEIPT_LIVE_PROXY_PREFIX,
        }
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/internal/v1/tms/yunda_waybill_proxy",
            payload={
                "params": params,
                "timeout_sec": 180,
                **self._control_plane_agent_body_context(trusted_context),
            },
            timeout=max(195, self.settings.agent_timeout_seconds),
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达回单原页代理调用失败。", "error": result.get("error")},
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达回单原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "韵达回单原页代理失败。")},
            )
            return
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
        )

    def _handle_ronghui_receipt_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
    ) -> None:
        if method.upper() != "GET":
            self._send_json(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {
                    "ok": False,
                    "error_code": "RECEIPT_PROXY_WRITE_DISABLED",
                    "message": "回单原页代理仅供只读查看；审核请通过事项中心提交。",
                },
            )
            return
        trusted_context = self._control_plane_read_context(handler)
        if trusted_context is None:
            return
        remote_path = self._ronghui_receipt_live_remote_path(path)
        is_entry_root = str(path or "").strip().rstrip("/") == RONGHUI_RECEIPT_LIVE_PROXY_PREFIX
        if not remote_path and not is_entry_root:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "融辉回单原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        remote_query = "" if is_entry_root else self._flatten_query(query)
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": remote_query,
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": RONGHUI_RECEIPT_LIVE_PROXY_PREFIX,
        }
        if is_entry_root:
            params["entry_menu_text"] = self._ronghui_receipt_entry_menu_text(query)
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/internal/v1/tms/ronghui_waybill_proxy",
            payload={
                "params": params,
                "timeout_sec": 180,
                **self._control_plane_agent_body_context(trusted_context),
            },
            timeout=max(195, self.settings.agent_timeout_seconds),
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            if str(result.get("error_code") or "") == "AUTH_REQUIRED":
                self._send_ronghui_auth_required_iframe(
                    handler,
                    str(result.get("error") or result.get("message") or "当前未登录或登录态已过期。"),
                )
                return
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉回单原页代理调用失败。", "error": result.get("error")},
            )
            return
        agent_payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        auth_payload = None
        if isinstance(agent_payload, dict) and agent_payload.get("ok") is False:
            auth_payload = agent_payload
        nested_agent_payload = agent_payload.get("data") if isinstance(agent_payload, dict) else None
        if isinstance(nested_agent_payload, dict) and nested_agent_payload.get("ok") is False:
            auth_payload = nested_agent_payload
        if isinstance(auth_payload, dict) and str(auth_payload.get("error_code") or "") == "AUTH_REQUIRED":
            self._send_ronghui_auth_required_iframe(
                handler,
                str(auth_payload.get("error") or auth_payload.get("message") or "当前未登录或登录态已过期。"),
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉回单原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "融辉回单原页代理失败。")},
            )
            return
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
        )

    def _handle_yunda_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
        proxy_prefix: str = YUNDA_LIVE_PROXY_PREFIX,
        frame_ancestor_origin: str = "",
    ) -> None:
        trusted_context = (
            self._control_plane_read_context(handler)
            if method.upper() == "GET"
            else self._control_plane_write_context(handler)
        )
        if trusted_context is None:
            return
        remote_path = self._yunda_live_remote_path(path, proxy_prefix=proxy_prefix)
        if not remote_path:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "韵达原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        if method.upper() != "GET" and not (
            method.upper() == "POST" and remote_path == YUNDA_LIVE_SAVE_PATH
        ):
            self._send_json(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {
                    "ok": False,
                    "error_code": "MANUAL_PROXY_WRITE_DISABLED",
                    "message": "Only the verified waybill save endpoint may write through this proxy.",
                },
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": self._flatten_query(query),
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": proxy_prefix,
        }
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/internal/v1/tms/yunda_waybill_proxy",
            payload={
                "params": params,
                "timeout_sec": 180,
                **self._control_plane_agent_body_context(trusted_context),
            },
            timeout=max(195, self.settings.agent_timeout_seconds),
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达原页代理调用失败。", "error": result.get("error")},
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "韵达原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "韵达原页代理失败。")},
            )
            return
        if method.upper() == "POST" and str(proxy_payload.get("remote_path") or remote_path) == YUNDA_LIVE_SAVE_PATH:
            print_patch = self._persist_yunda_live_save_result(
                request_body=request_body,
                request_content_type=request_content_type,
                proxy_payload=proxy_payload,
            )
            proxy_payload = self._patch_proxy_json_body(proxy_payload, print_patch)
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
            frame_ancestor_origin=frame_ancestor_origin,
        )

    def _handle_ronghui_live_proxy(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        *,
        method: str,
        query: dict[str, list[str]],
        proxy_prefix: str = RONGHUI_LIVE_PROXY_PREFIX,
        frame_ancestor_origin: str = "",
    ) -> None:
        trusted_context = (
            self._control_plane_read_context(handler)
            if method.upper() == "GET"
            else self._control_plane_write_context(handler)
        )
        if trusted_context is None:
            return
        remote_path = self._ronghui_live_remote_path(path, proxy_prefix=proxy_prefix)
        is_entry_root = str(path or "").strip().rstrip("/") == proxy_prefix
        if not remote_path and not is_entry_root:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "融辉原页代理路径不存在。", "error_code": "INVALID_PROXY_PATH"},
            )
            return
        if method.upper() != "GET" and not (
            method.upper() == "POST" and remote_path == RONGHUI_LIVE_SAVE_PATH
        ):
            self._send_json(
                handler,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {
                    "ok": False,
                    "error_code": "MANUAL_PROXY_WRITE_DISABLED",
                    "message": "Only the verified waybill save endpoint may write through this proxy.",
                },
            )
            return
        request_body = self._read_request_body(handler) if method.upper() != "GET" else b""
        request_content_type = str(handler.headers.get("Content-Type") or "")
        params = {
            "method": method.upper(),
            "path": remote_path,
            "query": self._flatten_query(query),
            "headers": self._handler_headers(handler),
            "content_type": request_content_type,
            "proxy_prefix": proxy_prefix,
        }
        if request_body:
            params["body_base64"] = base64.b64encode(request_body).decode("ascii")
        result = self._agent_request(
            "POST",
            "/internal/v1/tms/ronghui_waybill_proxy",
            payload={
                "params": params,
                "timeout_sec": 180,
                **self._control_plane_agent_body_context(trusted_context),
            },
            timeout=max(195, self.settings.agent_timeout_seconds),
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            if str(result.get("error_code") or "") == "AUTH_REQUIRED":
                self._send_ronghui_auth_required_iframe(
                    handler,
                    str(result.get("error") or result.get("message") or "当前未登录或登录态已过期。"),
                )
                return
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉原页代理调用失败。", "error": result.get("error")},
            )
            return
        agent_payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        auth_payload = None
        if isinstance(agent_payload, dict) and agent_payload.get("ok") is False:
            auth_payload = agent_payload
        nested_agent_payload = agent_payload.get("data") if isinstance(agent_payload, dict) else None
        if isinstance(nested_agent_payload, dict) and nested_agent_payload.get("ok") is False:
            auth_payload = nested_agent_payload
        if isinstance(auth_payload, dict) and str(auth_payload.get("error_code") or "") == "AUTH_REQUIRED":
            self._send_ronghui_auth_required_iframe(
                handler,
                str(auth_payload.get("error") or auth_payload.get("message") or "当前未登录或登录态已过期。"),
            )
            return
        proxy_payload = self._unwrap_yunda_live_proxy_payload(result)
        if not proxy_payload:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": "融辉原页代理返回格式异常。"},
            )
            return
        if proxy_payload.get("ok") is False:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "message": str(proxy_payload.get("error") or "融辉原页代理失败。")},
            )
            return
        if method.upper() == "POST" and str(proxy_payload.get("remote_path") or remote_path) == RONGHUI_LIVE_SAVE_PATH:
            self._persist_ronghui_live_save_result(
                request_body=request_body,
                request_content_type=request_content_type,
                proxy_payload=proxy_payload,
            )
        response_headers = proxy_payload.get("headers") if isinstance(proxy_payload.get("headers"), dict) else {}
        response_status = HTTPStatus(int(proxy_payload.get("status_code") or 200))
        self._send_proxy_bytes(
            handler,
            response_status,
            self._decode_proxy_body(proxy_payload),
            response_headers,
            frame_ancestor_origin=frame_ancestor_origin,
        )

    def _send_ronghui_auth_required_iframe(self, handler: BaseHTTPRequestHandler, auth_text: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
            "<pre>AUTH_REQUIRED\n"
            f"{html.escape(str(auth_text or '当前未登录或登录态已过期。'))}"
            "</pre></body></html>"
        ).encode("utf-8")
        self._send_proxy_bytes(
            handler,
            HTTPStatus.OK,
            body,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    def _call_yunda_entry_runtime(
        self,
        action: str,
        *,
        form: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        trusted_context: dict[str, Any] | None = None,
        timeout_sec: int = 180,
    ) -> tuple[HTTPStatus, dict[str, Any]]:
        result = self._agent_request(
            "POST",
            "/internal/v1/tms/yunda_waybill_entry",
            payload={
                "params": {
                    "action": action,
                    "form": form or {},
                    "context": context or {},
                },
                "timeout_sec": timeout_sec,
                **(trusted_context or {}),
            },
            timeout=max(timeout_sec + 15, self.settings.agent_timeout_seconds),
        )
        if not result.get("ok"):
            error = result.get("error")
            if isinstance(error, dict):
                message = str(error.get("error") or error.get("message") or "韵达运行时调用失败。")
            else:
                message = str(error or "韵达运行时调用失败。")
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "message": message,
                "auth_state": None,
                "data": {},
                "field_errors": {},
            }

        outer = result.get("data")
        if not isinstance(outer, dict):
            return HTTPStatus.BAD_GATEWAY, {
                "ok": False,
                "message": "韵达运行时返回格式异常。",
                "auth_state": None,
                "data": {},
                "field_errors": {},
            }

        auth_code = str(outer.get("error_code") or "").strip()
        if auth_code in {"AUTH_REQUIRED", "AUTH_PENDING_CODE"}:
            message = str(outer.get("error") or "韵达登录态不可用。")
            return HTTPStatus.OK, {
                "ok": False,
                "message": message,
                "auth_state": {"code": auth_code, "message": message},
                "data": {},
                "field_errors": {},
            }

        payload = outer.get("data")
        if not isinstance(payload, dict):
            payload = {
                "ok": bool(outer.get("ok")),
                "message": str(outer.get("error") or "韵达运行时返回格式异常。"),
                "data": {},
                "field_errors": {},
            }
        payload.setdefault("ok", bool(outer.get("ok", False)))
        payload.setdefault("message", "")
        payload.setdefault("data", {})
        payload.setdefault("field_errors", {})
        payload.setdefault("auth_state", {"code": "AUTHENTICATED"})
        return HTTPStatus.OK, payload

    def _persist_yunda_runtime_result(
        self,
        *,
        action: str,
        request_body: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = {
            "ok": bool(payload.get("ok")),
            "message": str(payload.get("message") or ""),
            "data": dict(payload.get("data") or {}),
            "field_errors": dict(payload.get("field_errors") or {}),
            "auth_state": payload.get("auth_state"),
        }
        if not response["ok"]:
            return response

        runtime_data = response["data"]
        normalized_form = runtime_data.get("normalized_form") if isinstance(runtime_data.get("normalized_form"), dict) else {}
        remote_waybill_no = str(runtime_data.get("waybill_no") or normalized_form.get("LogisticsId") or "").strip()
        snapshot_request = {
            "action": action,
            "request": request_body,
            "normalized_form": normalized_form,
        }
        snapshot_response = {
            "action": action,
            "payload": payload,
        }
        if action == "save":
            mapped = build_console_waybill_from_yunda_data(normalized_form, remote_waybill_no=remote_waybill_no)
            waybill = None
            waybill_id = None
            if mapped:
                waybill = self.repository.upsert_provider_waybill(mapped, source="yunda")
                waybill_id = int(waybill.get("id", 0) or 0) if waybill else None
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="save_request",
                payload=snapshot_request,
                waybill_id=waybill_id,
            )
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="save_response",
                payload={**snapshot_response, "mapped_record": mapped},
                waybill_id=waybill_id,
            )
            if waybill:
                runtime_data["local_waybill"] = waybill
                runtime_data["local_waybill_id"] = waybill_id
                runtime_data["print_url"] = f"/waybills/{waybill_id}/print?preview=1"
        elif action in {"drafts/save", "templates/save"}:
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="draft_save" if action == "drafts/save" else "template_save",
                payload={
                    **snapshot_request,
                    "payload": payload,
                },
                waybill_id=None,
            )
        elif action.startswith("print/"):
            waybill = self.repository.get_waybill_by_no(remote_waybill_no, source="yunda") if remote_waybill_no else None
            waybill_id = int(waybill.get("id", 0) or 0) if waybill else None
            self.repository.create_waybill_provider_snapshot(
                provider="yunda",
                remote_waybill_no=remote_waybill_no,
                snapshot_kind="print",
                payload=snapshot_response,
                waybill_id=waybill_id,
            )
            if waybill_id:
                runtime_data["preview_url"] = f"/waybills/{waybill_id}/print?preview=1"
        return response

    def _handle_yunda_entry(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        action = YUNDA_ENTRY_ACTIONS.get(path)
        if not action:
            self._send_json(
                handler,
                HTTPStatus.NOT_FOUND,
                {"ok": False, "message": "韵达动作不存在。", "data": {}, "field_errors": {}, "auth_state": None},
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        body = self._parse_json_body(handler)
        form = body.get("form") if isinstance(body.get("form"), dict) else {}
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        client_meta = body.get("client_meta") if isinstance(body.get("client_meta"), dict) else {}
        runtime_context = dict(context)
        if client_meta:
            runtime_context["client_meta"] = client_meta
        status, payload = self._call_yunda_entry_runtime(
            action,
            form=form,
            context=runtime_context,
            trusted_context=trusted_context,
        )
        if status == HTTPStatus.OK:
            payload = self._persist_yunda_runtime_result(action=action, request_body=body, payload=payload)
        self._send_json(handler, status, payload)
