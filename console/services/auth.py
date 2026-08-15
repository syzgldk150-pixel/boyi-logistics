"""Console application services grouped by business responsibility."""

from collections.abc import Mapping

from console.app_support import *  # noqa: F403
from console.navigation import (
    MobileNavigationValidationError,
    serialize_mobile_bottom_nav,
    validate_mobile_bottom_nav,
)


class AuthServiceMixin:
    def _require_same_origin_write(self, handler: BaseHTTPRequestHandler) -> bool:
        """Require browser writes to originate from this Console host."""

        host = str(handler.headers.get("Host") or "").strip().lower()
        source = str(
            handler.headers.get("Origin") or handler.headers.get("Referer") or ""
        ).strip()
        parsed = urlparse(source)
        if (
            host
            and parsed.scheme.lower() in {"http", "https"}
            and parsed.netloc.lower() == host
        ):
            return True

        message = "请求来源校验失败，请从当前 Console 页面重试。"
        self._send_json(
            handler,
            HTTPStatus.FORBIDDEN,
            {
                "ok": False,
                "data": None,
                "error": {
                    "code": "CSRF_ORIGIN_REJECTED",
                    "message": message,
                },
                # Keep the established Console error fields for older callers.
                "error_code": "CSRF_ORIGIN_REJECTED",
                "message": message,
            },
        )
        return False

    @staticmethod
    def _is_super_admin_user(user: Mapping[str, Any] | None) -> bool:
        value = user or {}
        return (
            not bool(value.get("is_legacy_basic_auth"))
            and str(value.get("role") or "") == "super_admin"
            and int(value.get("id") or 0) > 0
        )

    def _require_super_admin_account_write(self, handler: BaseHTTPRequestHandler) -> bool:
        if self._is_super_admin_user(current_admin_user()):
            return True
        self._send_text(
            handler,
            HTTPStatus.FORBIDDEN,
            "Super administrator permission is required.",
        )
        return False

    def _ensure_authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        user = self._authenticated_user_from_request(handler)
        if user:
            return True

        if self._is_ajax_request(handler):
            self._send_json(
                handler,
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "message": "请先登录后台。", "login_url": "/login"},
            )
            return False

        next_url = quote(handler.path or "/", safe="")
        self._redirect(handler, f"/login?next={next_url}")
        return False

    def _authenticated_user_from_request(self, handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
        legacy_user = self._legacy_basic_auth_user(handler)
        if legacy_user:
            self._set_current_admin_user(handler, legacy_user)
            return legacy_user

        session_id = self._session_id_from_cookie(handler)
        if not session_id:
            return None
        session = self.repository.get_admin_session(session_id)
        if not session:
            return None
        if not bool(session.get("is_active")):
            self.repository.delete_admin_session(session_id)
            return None

        expires_at = self._coerce_datetime(session.get("expires_at"))
        if expires_at <= datetime.now():
            self.repository.delete_admin_session(session_id)
            return None

        self.repository.touch_admin_session(session_id)
        user = {
            "id": int(session.get("user_id") or 0),
            "username": str(session.get("username") or ""),
            "display_name": str(session.get("display_name") or ""),
            "avatar_path": str(session.get("avatar_path") or ""),
            "avatar_url": self._admin_avatar_url(str(session.get("avatar_path") or "")),
            "ui_preferences_json": str(session.get("ui_preferences_json") or "{}"),
            "control_plane_role": str(session.get("control_plane_role") or "admin"),
            "role": str(session.get("role") or "admin"),
            "is_legacy_basic_auth": False,
        }
        self._set_current_admin_user(handler, user)
        return user

    def _legacy_basic_auth_user(self, handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
        username = getattr(self.settings, "basic_auth_user", "")
        password = getattr(self.settings, "basic_auth_password", "")
        if not username or not password:
            return None

        auth_header = handler.headers.get("Authorization", "")
        expected_token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        if not hmac.compare_digest(auth_header, f"Basic {expected_token}"):
            return None
        return {
            "id": 0,
            "username": username,
            "display_name": username,
            "avatar_path": "",
            "avatar_url": "",
            "ui_preferences_json": "{}",
            "role": "legacy_admin",
            "control_plane_role": "legacy_admin",
            "is_legacy_basic_auth": True,
        }

    def _set_current_admin_user(self, handler: BaseHTTPRequestHandler, user: dict[str, Any]) -> None:
        setattr(handler, "current_admin_user", user)
        _CURRENT_ADMIN_USER.set(user)

    def _session_id_from_cookie(self, handler: BaseHTTPRequestHandler) -> str:
        raw_cookie = str(handler.headers.get("Cookie") or "")
        if not raw_cookie:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return ""
        morsel = cookie.get(ADMIN_SESSION_COOKIE)
        if morsel is None:
            return ""
        return self._decode_session_cookie(morsel.value)

    def _decode_session_cookie(self, cookie_value: str) -> str:
        raw = str(cookie_value or "")
        session_id, separator, signature = raw.partition(".")
        if not session_id or not separator or not signature:
            return ""
        expected = self._sign_session_id(session_id)
        if not hmac.compare_digest(signature, expected):
            return ""
        return session_id

    def _encode_session_cookie(self, session_id: str) -> str:
        return f"{session_id}.{self._sign_session_id(session_id)}"

    def _sign_session_id(self, session_id: str) -> str:
        secret = getattr(self, "_session_secret", "") or getattr(self.settings, "session_secret", "")
        return hmac.new(secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def _build_session_cookie_header(self, cookie_value: str, *, max_age: int) -> str:
        secure = "; Secure" if getattr(self.settings, "session_cookie_secure", False) else ""
        return (
            f"{ADMIN_SESSION_COOKIE}={cookie_value}; Path=/; HttpOnly; "
            f"SameSite=Lax; Max-Age={max_age}{secure}"
        )

    def _clear_session_cookie_header(self) -> str:
        secure = "; Secure" if getattr(self.settings, "session_cookie_secure", False) else ""
        return (
            f"{ADMIN_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age=0{secure}"
        )

    def _coerce_datetime(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return datetime.min
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return datetime.min

    def _clean_next_url(self, raw_url: str) -> str:
        candidate = str(raw_url or "").strip() or "/"
        parsed = urlparse(candidate)
        if parsed.scheme or parsed.netloc or not candidate.startswith("/") or candidate.startswith("//"):
            return "/"
        return candidate

    def _render_login(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        user = self._authenticated_user_from_request(handler)
        next_url = self._clean_next_url(query.get("next", ["/"])[0])
        if user:
            self._redirect(handler, next_url)
            return

        template = self.template_env.get_template("login.html")
        body = template.render(
            app_title=self.settings.app_title,
            next_url=next_url,
            username_value=query.get("username", [""])[0],
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            has_admin_users=self.repository.count_admin_users() > 0,
        )
        self._send_html(handler, body)

    def _handle_login(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        username = str(values.get("username", "") or "").strip()
        password = str(values.get("password", "") or "")
        next_url = self._clean_next_url(values.get("next", "/"))
        user = self.repository.get_admin_user_by_username(username)
        if not user or not bool(user.get("is_active")) or not verify_admin_password(password, str(user.get("password_hash") or "")):
            template = self.template_env.get_template("login.html")
            body = template.render(
                app_title=self.settings.app_title,
                next_url=next_url,
                username_value=username,
                message="账号或密码不正确。",
                message_kind="warning",
                has_admin_users=self.repository.count_admin_users() > 0,
            )
            self._send_html(handler, body, status=HTTPStatus.UNAUTHORIZED)
            return

        now = datetime.now()
        ttl_hours = getattr(self.settings, "session_ttl_hours", 12)
        expires_at = now + timedelta(hours=ttl_hours)
        self.repository.delete_expired_admin_sessions(now)
        session_id = secrets.token_urlsafe(32)
        self.repository.create_admin_session(
            session_id=session_id,
            user_id=int(user["id"]),
            expires_at=expires_at,
        )
        self.repository.record_admin_login(int(user["id"]))
        cookie_value = self._encode_session_cookie(session_id)
        cookie_header = self._build_session_cookie_header(
            cookie_value,
            max_age=int(ttl_hours) * 3600,
        )
        self._redirect(handler, next_url, headers=[("Set-Cookie", cookie_header)])

    def _handle_logout(self, handler: BaseHTTPRequestHandler) -> None:
        session_id = self._session_id_from_cookie(handler)
        if session_id:
            self.repository.delete_admin_session(session_id)
        self._redirect(
            handler,
            "/login?message=%E5%B7%B2%E9%80%80%E5%87%BA%E5%90%8E%E5%8F%B0%E3%80%82&kind=success",
            headers=[("Set-Cookie", self._clear_session_cookie_header())],
        )

    def _handle_mobile_navigation_save(self, handler: BaseHTTPRequestHandler) -> None:
        user = current_admin_user()
        if not user or bool(user.get("is_legacy_basic_auth")) or int(user.get("id") or 0) <= 0:
            self._send_json(
                handler,
                HTTPStatus.FORBIDDEN,
                {
                    "ok": False,
                    "data": None,
                    "error": {
                        "code": "MOBILE_NAVIGATION_SYNC_UNAVAILABLE",
                        "message": "应急 Basic Auth 没有管理员账号标识，无法同步移动底栏偏好。",
                    },
                },
            )
            return

        body = self._parse_json_body(handler)
        try:
            routes = validate_mobile_bottom_nav(body.get("routes"))
        except MobileNavigationValidationError as exc:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "data": None,
                    "error": {"code": "INVALID_MOBILE_NAVIGATION", "message": str(exc)},
                },
            )
            return

        user_id = int(user["id"])
        stored_user = self.repository.get_admin_user(user_id)
        if not stored_user:
            self._send_json(
                handler,
                HTTPStatus.UNAUTHORIZED,
                {
                    "ok": False,
                    "data": None,
                    "error": {"code": "ADMIN_USER_NOT_FOUND", "message": "当前管理员账号不可用。"},
                },
            )
            return

        preferences_json = serialize_mobile_bottom_nav(stored_user.get("ui_preferences_json"), routes)
        if not self.repository.update_admin_ui_preferences(user_id, preferences_json):
            self._send_json(
                handler,
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "data": None,
                    "error": {"code": "MOBILE_NAVIGATION_SAVE_FAILED", "message": "移动底栏偏好未保存。"},
                },
            )
            return

        updated_user = dict(user)
        updated_user["ui_preferences_json"] = preferences_json
        self._set_current_admin_user(handler, updated_user)
        self._send_json(
            handler,
            HTTPStatus.OK,
            {"ok": True, "data": {"routes": list(routes)}, "error": None},
        )

    def _render_admin_accounts(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        template = self.template_env.get_template("admin_accounts.html")
        body = template.render(
            app_title=self.settings.app_title,
            users=self.repository.list_admin_users(),
            is_super_admin=str((current_admin_user() or {}).get("role") or "") == "super_admin",
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _render_automation_accounts(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        accounts, account_warning = self._fetch_automation_accounts(force=False, prefer_cached=True)
        account_groups = self._automation_account_groups(accounts)
        account_system_counts = {group["system"]: group["count"] for group in account_groups}
        valid_systems = set(AUTOMATION_ACCOUNT_SYSTEM_ORDER) | set(account_system_counts)
        requested_system = str(query.get("system", [""])[0] or "").strip().lower()
        account_filter = requested_system if requested_system in valid_systems else ""
        account_rows = [account for group in account_groups for account in group["accounts"]]
        account_tab_systems = [
            system
            for system in AUTOMATION_ACCOUNT_SYSTEM_ORDER
            if account_system_counts.get(system, 0) > 0
        ]
        account_tab_systems.extend(
            sorted(
                system
                for system, count in account_system_counts.items()
                if count > 0 and system not in AUTOMATION_ACCOUNT_SYSTEM_ORDER
            )
        )
        template = self.template_env.get_template("automation_accounts.html")
        body = template.render(
            app_title=self.settings.app_title,
            accounts=accounts,
            account_groups=account_groups,
            account_rows=account_rows,
            account_filter=account_filter,
            account_filter_label=(
                f"{AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(account_filter, account_filter)} "
                if account_filter
                else ""
            ),
            account_total_count=len(accounts),
            account_system_counts=account_system_counts,
            account_tab_systems=account_tab_systems,
            account_system_labels=AUTOMATION_ACCOUNT_SYSTEM_LABELS,
            account_system_order=AUTOMATION_ACCOUNT_SYSTEM_ORDER,
            account_warning=account_warning,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _query_bool(self, query: dict | None, name: str, default: bool = False) -> bool:
        raw = str((query or {}).get(name, ["1" if default else ""])[0] or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def _handle_automation_account_status_get(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        query: dict | None = None,
    ) -> None:
        prefix = "/automation-accounts/"
        suffix = "/status"
        account_id = unquote(path[len(prefix) : -len(suffix)].strip("/"))
        if not account_id:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": "账号不存在。", "kind": "warning"},
            )
            return
        status_result = self._fetch_automation_account_status_state(
            account_id,
            force=self._query_bool(query, "force", True),
        )
        if not status_result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "message": status_result.get("message") or "账号状态获取失败。",
                    "kind": "warning",
                },
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "state": status_result.get("state") or {}})

    def _handle_automation_accounts_statuses_get(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict | None = None,
    ) -> None:
        accounts, warning = self._fetch_automation_accounts(
            force=self._query_bool(query, "force", False),
            prefer_cached=self._query_bool(query, "prefer_cached", True),
        )
        if warning:
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {"ok": False, "message": warning, "accounts": []})
            return
        meta = getattr(self, "_automation_accounts_cache_meta", {})
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "accounts": accounts, **meta})

    def _fetch_automation_accounts(
        self,
        *,
        force: bool = True,
        prefer_cached: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        query_params = {}
        if force:
            query_params["force"] = "1"
        if prefer_cached:
            query_params["prefer_cached"] = "1"
        endpoint = "/internal/v1/admin/accounts"
        if query_params:
            endpoint = f"{endpoint}?{urlencode(query_params)}"
        self._automation_accounts_cache_meta = {}
        result = self._agent_request("GET", endpoint, timeout=12 if prefer_cached else 45 if force else 12)
        if not result.get("ok"):
            return [], normalize_feedback_text(result.get("error") or "Agent 当前不可达，无法获取业务账号状态。")
        payload = result.get("data")
        if not isinstance(payload, dict):
            return [], "Agent 账号接口返回了无效数据。"
        if payload.get("ok") is False:
            return [], normalize_feedback_text(payload.get("message") or payload.get("error") or "Agent 账号接口调用失败。")
        raw_accounts = payload.get("accounts")
        if not isinstance(raw_accounts, list):
            return [], "Agent 账号接口缺少 accounts 列表。"
        self._automation_accounts_cache_meta = {
            key: payload[key]
            for key in ("cached", "stale", "refreshing", "cache_age_sec")
            if key in payload
        }

        accounts: list[dict[str, Any]] = []
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            account = dict(item)
            system = str(account.get("system") or "").strip().lower()
            if system == "price":
                system = "ronghui"
                account["account_purpose"] = account.get("account_purpose") or "price"
            account["system"] = system
            account["system_label"] = str(
                account.get("system_label")
                or AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(system, system or "-")
            )
            if system == "ronghui":
                account["system_label"] = AUTOMATION_ACCOUNT_SYSTEM_LABELS["ronghui"]
            account["name"] = str(account.get("name") or account.get("account_id") or "").strip()
            status = dict(account.get("status") if isinstance(account.get("status"), dict) else {})
            credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
            safe_credentials = dict(credentials)
            safe_credentials["password"] = ""
            has_manual_credentials = bool(safe_credentials.get("has_manual_credentials"))
            has_env_credentials = False
            credential_source = "saved" if has_manual_credentials else ""
            has_saved_credentials = has_manual_credentials
            safe_credentials.update(
                {
                    "has_saved_credentials": has_saved_credentials,
                    "has_manual_credentials": has_manual_credentials,
                    "has_env_credentials": False,
                    "credential_source": credential_source,
                }
            )
            if has_manual_credentials:
                credentials_label = "已保存账号密码"
                credentials_tone = "success"
            else:
                credentials_label = "未保存账号密码"
                credentials_tone = "warning"
            raw_status_value = str(status.get("status") or "").strip()
            status_label = str(status.get("label") or "")
            status_tone = str(status.get("status_tone") or "")
            status_note = ""
            auto_login_enabled = bool(account.get("auto_login_enabled", False))
            auto_login_blocked = bool(account.get("auto_login_blocked", False))
            if not bool(account.get("is_active", True)):
                status_label = "已停用"
                status_tone = "neutral"
                status_note = "不参与任务执行与登录监控；停用操作不会退出当前会话。"
                status["last_error_summary"] = ""
            elif bool(account.get("session_capable")) and raw_status_value == "authenticated" and not has_saved_credentials:
                status_label = "登录态有效"
                status_tone = "warning"
                status_note = "当前只检测到浏览器登录态，未保存账号密码；登录态失效后需重新登录。"
            elif not auto_login_enabled:
                if raw_status_value == "logged_out":
                    status_label = "已退出"
                elif raw_status_value == "authenticated":
                    status_label = "已登录（未监控）"
                else:
                    status_label = "自动登录已关闭"
                status_tone = "warning" if raw_status_value == "authenticated" else "neutral"
                status_note = "不做定时登录校验、自动登录或飞书断线提醒；仍可手动登录。"
                status["last_error_summary"] = ""
            elif auto_login_blocked:
                failure_limit = int(account.get("auto_login_failure_limit") or 3)
                status_label = "自动登录已暂停"
                status_tone = "warning"
                status_note = f"连续失败达到 {failure_limit} 次，为防止账号锁定已停止重试；请手动登录。"
            status["label"] = status_label
            status["status_tone"] = status_tone
            status["status_note"] = status_note
            status["has_saved_credentials"] = has_saved_credentials
            status["has_manual_credentials"] = has_manual_credentials
            status["has_env_credentials"] = has_env_credentials
            status["credential_source"] = credential_source
            account["status"] = status
            account["credentials"] = safe_credentials
            account["status_label"] = status_label
            account["status_tone"] = status_tone
            account["status_note"] = status_note
            account["credential_source"] = credential_source
            account["has_saved_credentials"] = has_saved_credentials
            account["has_manual_credentials"] = has_manual_credentials
            account["has_env_credentials"] = has_env_credentials
            account["credentials_label"] = credentials_label
            account["credentials_tone"] = credentials_tone
            account["auto_login_enabled"] = auto_login_enabled
            account["auto_login_blocked"] = auto_login_blocked
            accounts.append(account)
        return accounts, ""

    def _fetch_automation_account_status_state(self, account_id: str, *, force: bool = True) -> dict[str, Any]:
        quoted_id = quote(str(account_id or "").strip(), safe="")
        if not quoted_id:
            return {"ok": False, "message": "账号不存在。"}
        suffix = "?force=1" if force else ""
        result = self._agent_request("GET", f"/internal/v1/admin/accounts/{quoted_id}/status{suffix}", timeout=35)
        if not result.get("ok"):
            return {
                "ok": False,
                "message": f"Agent 调用失败：{normalize_feedback_text(result.get('error') or 'unknown error')}",
            }
        payload = result.get("data")
        if not isinstance(payload, dict):
            return {"ok": False, "message": "Agent 账号状态接口返回了无效数据。"}
        if payload.get("ok") is False:
            return {
                "ok": False,
                "message": normalize_feedback_text(
                    payload.get("message") or payload.get("error") or "账号状态获取失败。"
                ),
            }
        state = dict(payload)
        state.pop("ok", None)
        return {"ok": True, "state": state}

    def _automation_account_groups(self, accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows_by_system: dict[str, list[dict[str, Any]]] = {
            system: [] for system in AUTOMATION_ACCOUNT_SYSTEM_ORDER
        }
        for account in accounts:
            system = str(account.get("system") or "").strip().lower()
            rows_by_system.setdefault(system, []).append(account)

        groups: list[dict[str, Any]] = []
        for system in [*AUTOMATION_ACCOUNT_SYSTEM_ORDER, *sorted(set(rows_by_system) - set(AUTOMATION_ACCOUNT_SYSTEM_ORDER))]:
            rows = sorted(
                rows_by_system.get(system, []),
                key=lambda item: (
                    not bool(item.get("is_default")),
                    not bool(item.get("is_active", True)),
                    str(item.get("name") or item.get("account_id") or ""),
                ),
            )
            groups.append(
                {
                    "system": system,
                    "label": AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(system, system or "-"),
                    "accounts": rows,
                    "count": len(rows),
                }
            )
        return groups

    def _automation_account_options_by_system(self, accounts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        options: dict[str, list[dict[str, Any]]] = {}
        for account in accounts:
            account = dict(account)
            status = account.get("status") if isinstance(account.get("status"), dict) else {}
            session_ready = (
                not bool(account.get("session_capable"))
                or str(status.get("status") or "").strip() == "authenticated"
            )
            account["binding_usable"] = bool(account.get("is_active", True)) and session_ready
            system = str(account.get("system") or "").strip().lower()
            options.setdefault(system, []).append(account)
        for system, rows in options.items():
            rows.sort(
                key=lambda item: (
                    not bool(item.get("binding_usable")),
                    not bool(item.get("is_default")),
                    str(item.get("name") or item.get("account_id") or ""),
                )
            )
        return options

    def _automation_task_account_roles(
        self,
        task_id: str,
        workflow: dict[str, Any] | None = None,
        tool_name: str = "",
        provider: str = "",
    ) -> list[dict[str, Any]]:
        workflow = workflow or automation_workflow_definition(task_id)
        raw_roles = workflow.get("account_roles")
        roles_declared = isinstance(raw_roles, list)
        roles = raw_roles if roles_declared else []
        if not roles and not roles_declared:
            normalized = normalize_task_group_id(task_id)
            tool_name_value = str(tool_name or workflow.get("tool_name") or "").strip()
            provider_value = str(provider or "").strip().lower()
            if tool_name_value.startswith("r7_") or normalized.startswith("r7_"):
                roles = [{"label": "运行账号", "field": "account_id", "system": "r7", "default_account_id": "r7_default"}]
            elif tool_name_value == "sync_daily_should_sign" or normalized.startswith("daily_sign"):
                roles = [
                    {"label": "R13应签查询账号", "field": "r13_account_id", "system": "r13", "default_account_id": "r13_default"},
                    {"label": "TMS邵阳大祥站账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_daxiang_s"},
                ]
            elif provider_value == "yunda" or tool_name_value.startswith("sync_yunda_"):
                roles = [{"label": "运行账号", "field": "account_id", "system": "yunda", "default_account_id": "yunda_default"}]
            elif (
                "price" in tool_name_value.lower()
                or normalized.startswith("price")
                or tool_name_value == "ronghui_waybill_proxy"
                or normalized == "ronghui_waybill_proxy"
            ):
                roles = [{"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "price_default"}]
            else:
                roles = [{"label": "运行账号", "field": "account_id", "system": "ronghui", "default_account_id": "ronghui_default"}]

        normalized_roles: list[dict[str, Any]] = []
        for role in roles:
            if not isinstance(role, dict):
                continue
            allowed_systems = role.get("allowed_systems")
            if isinstance(allowed_systems, list):
                systems = [
                    str(item or "").strip().lower()
                    for item in allowed_systems
                    if str(item or "").strip().lower() in AUTOMATION_ACCOUNT_SYSTEM_LABELS
                ]
            else:
                system = str(role.get("system") or "").strip().lower()
                systems = [system] if system in AUTOMATION_ACCOUNT_SYSTEM_LABELS else []
            systems = list(dict.fromkeys(systems))
            if not systems:
                continue
            system = systems[0]
            field = str(role.get("field") or role.get("role") or "account_id").strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field):
                continue
            normalized_roles.append(
                {
                    "label": str(role.get("label") or "运行账号").strip() or "运行账号",
                    "field": field,
                    "system": system,
                    "systems": systems,
                    "system_label": " / ".join(
                        AUTOMATION_ACCOUNT_SYSTEM_LABELS.get(item, item) for item in systems
                    ),
                    "default_account_id": str(
                        role.get("default_account_id")
                        or AUTOMATION_DEFAULT_ACCOUNT_IDS.get(system, "")
                    ).strip(),
                    "required": bool(role.get("required", True)),
                    "binding_cardinality": (
                        "many"
                        if str(role.get("binding_cardinality") or "one") == "many"
                        else "one"
                    ),
                }
            )
        return normalized_roles

    def _legacy_task_account_system(
        self,
        task_id: str,
        workflow: dict[str, Any] | None = None,
        tool_name: str = "",
        provider: str = "",
    ) -> str:
        roles = self._automation_task_account_roles(task_id, workflow, tool_name, provider)
        return str((roles[0] if roles else {}).get("system") or "ronghui")

    def _legacy_task_account_purpose(
        self,
        task_id: str,
        workflow: dict[str, Any] | None = None,
        tool_name: str = "",
    ) -> str:
        workflow = workflow or automation_workflow_definition(task_id)
        normalized = normalize_task_group_id(task_id)
        tool_name_value = str(tool_name or workflow.get("tool_name") or "").strip()
        if "price" in tool_name_value.lower() or normalized.startswith("price"):
            return "price"
        if tool_name_value == "self_pickup_problem_upload" or normalized == "self_pickup_problem_upload":
            return "self_pickup_problem"
        return "general"

    def _enrich_automation_tasks_with_accounts(
        self,
        tasks: list[dict[str, Any]],
        accounts: list[dict[str, Any]],
    ) -> None:
        options_by_system = self._automation_account_options_by_system(accounts)
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            workflow = dict(automation_workflow_definition(task_id))
            plugin = task.get("plugin")
            if isinstance(plugin, dict):
                workflow["account_roles"] = list(plugin.get("account_roles") or [])
            plugin_account_bindings = (
                plugin.get("account_bindings")
                if isinstance(plugin, dict)
                and isinstance(plugin.get("account_bindings"), dict)
                else {}
            )
            try:
                payload = json.loads(str(task.get("tool_params_json") or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            role_bindings: list[dict[str, Any]] = []
            for role in self._automation_task_account_roles(
                task_id,
                workflow,
                str(task.get("tool_name_value") or ""),
                str(task.get("provider") or ""),
            ):
                system = str(role.get("system") or "").strip().lower()
                systems = list(role.get("systems") or [system])
                options = [
                    account
                    for allowed_system in systems
                    for account in options_by_system.get(str(allowed_system), [])
                ]
                option_ids = {str(item.get("account_id") or "") for item in options}
                field = str(role.get("field") or "account_id")
                many = role.get("binding_cardinality") == "many"
                if isinstance(plugin, dict):
                    # Installed projects are configured only from the core-owned
                    # project binding.  Legacy cron parameters remain readable
                    # migration evidence, never an execution/config authority.
                    raw_configured = plugin_account_bindings.get(field)
                    configured_account_ids = (
                        [str(item or "").strip() for item in raw_configured]
                        if isinstance(raw_configured, list)
                        else [str(raw_configured or "").strip()]
                    )
                else:
                    configured_account_id = str(payload.get(field) or "").strip()
                    if not configured_account_id and field == "account_id":
                        configured_account_id = str(payload.get("accountId") or "").strip()
                    configured_account_ids = [configured_account_id]
                configured_account_ids = [item for item in configured_account_ids if item]
                if not many and len(configured_account_ids) > 1:
                    configured_account_ids = configured_account_ids[:1]
                selected_account_ids = [
                    item for item in configured_account_ids if item in option_ids
                ]
                selected_accounts = [
                    item
                    for item in options
                    if str(item.get("account_id") or "") in selected_account_ids
                ]
                configured_invalid = len(selected_account_ids) != len(configured_account_ids)
                unavailable = any(
                    not bool(account.get("binding_usable")) for account in selected_accounts
                )
                blocked = configured_invalid or unavailable or (
                    bool(role.get("required", True)) and not selected_accounts
                )
                if configured_invalid:
                    blocked_reason = "已保存账号不存在或不属于此角色"
                elif not selected_accounts:
                    blocked_reason = "未选择账号"
                elif any(not bool(account.get("is_active", True)) for account in selected_accounts):
                    blocked_reason = "已保存账号已停用"
                elif any(
                    bool(account.get("session_capable"))
                    and not bool(account.get("binding_usable"))
                    for account in selected_accounts
                ):
                    blocked_reason = "已保存账号登录态无效"
                else:
                    blocked_reason = ""
                role_bindings.append(
                    {
                        **role,
                        "options": options,
                        "selected_account_ids": selected_account_ids,
                        "selected_account_id": selected_account_ids[0]
                        if selected_account_ids
                        else "",
                        "blocked": blocked,
                        "blocked_reason": blocked_reason if blocked else "",
                    }
                )

            first_role = role_bindings[0] if role_bindings else {}
            task["account_role_bindings"] = role_bindings
            task["account_system"] = str(first_role.get("system") or "")
            task["account_system_label"] = str(first_role.get("system_label") or "")
            task["account_options"] = list(first_role.get("options") or [])
            task["selected_account_id"] = str(first_role.get("selected_account_id") or "")
            account_block_reasons = [
                str(role.get("blocked_reason") or "")
                for role in role_bindings
                if role.get("blocked") and str(role.get("blocked_reason") or "")
            ]
            task["account_blocked"] = bool(account_block_reasons)
            task["account_block_reasons"] = account_block_reasons
            if account_block_reasons:
                task["can_run_now"] = False
                task["plugin_blocked"] = True
                existing_warning = str(task.get("plugin_warning") or "").strip()
                account_warning = "；".join(dict.fromkeys(account_block_reasons))
                task["plugin_warning"] = "；".join(
                    item for item in (existing_warning, account_warning) if item
                )

    def _handle_automation_account_post(self, handler: BaseHTTPRequestHandler, path: str) -> bool:
        if path == "/automation-accounts/create":
            values = self._parse_urlencoded_form(handler)
            account_id = str(values.get("account_id", "") or "").strip()
            system = str(values.get("system", "") or "").strip()
            name = str(values.get("name", "") or "").strip()
            return self._proxy_automation_account_action(
                handler,
                "POST",
                "/internal/v1/admin/accounts",
                payload={
                    "account_id": account_id,
                    "system": system,
                    "name": name,
                },
                success_message=f"业务账号已创建：{name or account_id}",
                timeout=12,
                account_id=account_id,
            )

        prefix = "/automation-accounts/"
        if not path.startswith(prefix):
            return False
        parts = [part for part in path[len(prefix) :].strip("/").split("/") if part]
        if len(parts) != 2:
            return False
        account_id = unquote(parts[0])
        action = parts[1]
        quoted_id = quote(account_id, safe="")
        values = self._parse_urlencoded_form(handler)

        if action == "name":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/name",
                payload={"name": str(values.get("name", "") or "").strip()},
                success_message="账号备注已保存。",
                timeout=12,
                account_id=account_id,
                refresh_status=False,
            )
        if action == "credentials":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/credentials",
                payload={
                    "username": str(values.get("username", "") or "").strip(),
                    "password": str(values.get("password", "") or ""),
                    "phone": str(values.get("phone", "") or "").strip(),
                },
                success_message="账号凭据已保存。",
                timeout=20,
                account_id=account_id,
            )
        if action == "login":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/login",
                payload={},
                success_message="已立即执行一次登录；自动登录开关只控制后续定时校验与掉线恢复。",
                timeout=90,
                account_id=account_id,
            )
        if action == "submit-code":
            code = str(values.get("code", "") or "").strip()
            if not code:
                if self._is_ajax_request(handler):
                    self._send_json(
                        handler,
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "message": "验证码不能为空。", "kind": "warning"},
                    )
                    return True
                self._redirect_with_message(handler, "/automation-accounts", "验证码不能为空。", "warning")
                return True
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/submit-code",
                payload={"code": code},
                success_message="验证码已提交。",
                timeout=45,
                account_id=account_id,
            )
        if action == "clear-session":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/clear-session",
                payload={},
                success_message="已退出登录，自动登录与断线提醒已关闭。",
                timeout=20,
                account_id=account_id,
            )
        if action == "clear-credentials":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/credentials/clear",
                payload={},
                success_message="后台保存的账号凭据已清空，自动登录已关闭。",
                timeout=20,
                account_id=account_id,
            )
        if action == "default":
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/default",
                payload={},
                success_message="默认账号已更新。",
                timeout=12,
                account_id=account_id,
            )
        if action == "active":
            target_active = str(values.get("target_active", "") or "").strip() == "1"
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/active",
                payload={"is_active": target_active},
                success_message="账号状态已更新。",
                timeout=12,
                account_id=account_id,
            )
        if action == "auto-login":
            enabled = str(values.get("auto_login_enabled", "") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            return self._proxy_automation_account_action(
                handler,
                "POST",
                f"/internal/v1/admin/accounts/{quoted_id}/auto-login",
                payload={"enabled": enabled},
                success_message="自动登录与断线提醒已开启。" if enabled else "自动登录与断线提醒已关闭。",
                timeout=12,
                account_id=account_id,
            )
        return False

    def _proxy_automation_account_action(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, Any],
        success_message: str,
        timeout: int,
        account_id: str = "",
        refresh_status: bool = True,
    ) -> bool:
        result = self._agent_request(method, endpoint, payload=payload, timeout=timeout)
        message = success_message
        kind = "success"
        response_payload: dict[str, Any] | None = None
        if not result.get("ok"):
            message = f"Agent 调用失败：{normalize_feedback_text(result.get('error') or 'unknown error')}"
            kind = "warning"
        else:
            raw_payload = result.get("data")
            if not isinstance(raw_payload, dict):
                message = "Agent 账号接口返回了无效数据。"
                kind = "warning"
            else:
                response_payload = raw_payload
            if isinstance(response_payload, dict) and response_payload.get("ok") is False:
                message = normalize_feedback_text(
                    response_payload.get("message")
                    or response_payload.get("error")
                    or "账号操作失败。"
                )
                kind = "warning"
        if self._is_ajax_request(handler):
            response: dict[str, Any] = {"ok": kind == "success", "message": message, "kind": kind}
            if isinstance(response_payload, dict) and response_payload.get("ok") is not False:
                direct_state = self._automation_account_state_from_payload(response_payload)
                if direct_state is not None:
                    response["state"] = direct_state
                elif account_id and refresh_status:
                    status_result = self._fetch_automation_account_status_state(account_id)
                    if status_result.get("ok"):
                        response["state"] = status_result.get("state") or {}
                    elif kind == "success":
                        response["ok"] = False
                        response["kind"] = "warning"
                        response["message"] = status_result.get("message") or "账号状态获取失败。"
                credentials = response_payload.get("credentials")
                if isinstance(credentials, dict):
                    public_credentials = dict(credentials)
                    public_credentials["password"] = ""
                    response["credentials"] = public_credentials
                account = response_payload.get("account")
                if isinstance(account, dict):
                    response["account"] = dict(account)
            self._send_json(handler, HTTPStatus.OK, response)
            return True
        self._redirect_with_message(handler, "/automation-accounts", message, kind)
        return True

    def _automation_account_state_from_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        nested = payload.get("state")
        if isinstance(nested, dict):
            state = dict(nested)
            state.pop("ok", None)
            return state
        if isinstance(payload.get("status"), str):
            state = dict(payload)
            state.pop("ok", None)
            return state
        return None

    def _handle_admin_account_create(self, handler: BaseHTTPRequestHandler) -> None:
        if not self._require_super_admin_account_write(handler):
            return
        values = self._parse_urlencoded_form(handler)
        username = str(values.get("username", "") or "").strip()
        display_name = str(values.get("display_name", "") or "").strip()
        password = str(values.get("password", "") or "")
        if not ADMIN_USERNAME_RE.fullmatch(username):
            self._redirect_with_message(handler, "/settings/accounts", "账号需为 3-64 位字母、数字、点、下划线、@ 或短横线。", "warning")
            return
        if len(password) < 8:
            self._redirect_with_message(handler, "/settings/accounts", "密码至少需要 8 位。", "warning")
            return
        if self.repository.get_admin_user_by_username(username):
            self._redirect_with_message(handler, "/settings/accounts", "账号已存在。", "warning")
            return
        self.repository.create_admin_user(
            username=username,
            display_name=display_name or username,
            password_hash=hash_admin_password(password),
            is_active=True,
        )
        self._redirect_with_message(handler, "/settings/accounts", f"账号已创建：{username}", "success")

    def _handle_admin_account_toggle(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        if not self._require_super_admin_account_write(handler):
            return
        user_id = self._parse_admin_user_id(path, "toggle")
        if user_id is None:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Admin account not found.")
            return
        values = self._parse_urlencoded_form(handler)
        target_active = str(values.get("target_active", "") or "") == "1"
        current_user = current_admin_user() or {}
        if int(current_user.get("id") or 0) == user_id and not target_active:
            self._redirect_with_message(handler, "/settings/accounts", "不能停用当前登录账号。", "warning")
            return
        if not self.repository.get_admin_user(user_id):
            self._redirect_with_message(handler, "/settings/accounts", "账号不存在。", "warning")
            return
        self.repository.set_admin_user_active(user_id, target_active)
        message = "账号已启用。" if target_active else "账号已停用。"
        self._redirect_with_message(handler, "/settings/accounts", message, "success")

    def _handle_admin_account_reset_password(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        if not self._require_super_admin_account_write(handler):
            return
        user_id = self._parse_admin_user_id(path, "reset-password")
        if user_id is None:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Admin account not found.")
            return
        values = self._parse_urlencoded_form(handler)
        password = str(values.get("password", "") or "")
        if len(password) < 8:
            self._redirect_with_message(handler, "/settings/accounts", "新密码至少需要 8 位。", "warning")
            return
        if not self.repository.get_admin_user(user_id):
            self._redirect_with_message(handler, "/settings/accounts", "账号不存在。", "warning")
            return
        self.repository.update_admin_user_password(user_id, hash_admin_password(password))
        self._redirect_with_message(handler, "/settings/accounts", "密码已重置，原有会话已失效。", "success")

    def _handle_admin_account_role(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        current_user = current_admin_user() or {}
        if bool(current_user.get("is_legacy_basic_auth")) or str(current_user.get("role") or "") != "super_admin":
            self._send_text(handler, HTTPStatus.FORBIDDEN, "Super administrator permission is required.")
            return
        user_id = self._parse_admin_user_id(path, "role")
        if user_id is None or not self.repository.get_admin_user(user_id):
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Admin account not found.")
            return
        values = self._parse_urlencoded_form(handler)
        role = str(values.get("role") or "").strip()
        if role not in {"admin", "super_admin"}:
            self._redirect_with_message(handler, "/settings/accounts", "管理员角色无效。", "warning")
            return
        try:
            self.repository.set_admin_user_role(user_id, role)
        except ValueError as exc:
            self._redirect_with_message(handler, "/settings/accounts", str(exc), "warning")
            return
        self._redirect_with_message(handler, "/settings/accounts", "管理员角色已更新，原会话已失效。", "success")

    def _handle_admin_avatar_upload(self, handler: BaseHTTPRequestHandler) -> None:
        user = current_admin_user() or {}
        user_id = int(user.get("id") or 0)
        return_to = self._request_return_to(handler, "/")
        if not user_id or bool(user.get("is_legacy_basic_auth")):
            self._send_avatar_upload_error(handler, HTTPStatus.FORBIDDEN, "当前登录方式不支持上传头像。", return_to)
            return

        try:
            content_length = int(handler.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length > AVATAR_MAX_BYTES + 512 * 1024:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像图片不能超过 2MB。", return_to)
            return

        form = self._parse_multipart_form(handler)
        item = form["avatar"] if "avatar" in form else None
        if isinstance(item, list):
            item = item[0] if item else None
        if item is None or not getattr(item, "filename", ""):
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "请选择要上传的头像图片。", return_to)
            return

        suffix = Path(str(item.filename or "")).suffix.lower()
        if suffix not in AVATAR_ALLOWED_EXTENSIONS:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像仅支持 JPG、PNG、WebP 或 GIF 图片。", return_to)
            return

        payload = item.file.read(AVATAR_MAX_BYTES + 1)
        if not payload:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像图片为空。", return_to)
            return
        if len(payload) > AVATAR_MAX_BYTES:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像图片不能超过 2MB。", return_to)
            return

        detected_suffix = self._detect_avatar_extension(payload)
        if detected_suffix is None:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像文件格式无法识别。", return_to)
            return

        avatar_dir = self.settings.runtime_dir / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        filename = f"admin_{user_id}_{secrets.token_hex(12)}{detected_suffix}"
        target = (avatar_dir / filename).resolve()
        try:
            target.relative_to(avatar_dir.resolve())
        except ValueError:
            self._send_avatar_upload_error(handler, HTTPStatus.BAD_REQUEST, "头像保存路径无效。", return_to)
            return

        target.write_bytes(payload)
        relpath = str(target.relative_to(self.settings.runtime_dir)).replace("\\", "/")
        previous_avatar_path = str(user.get("avatar_path") or "")
        self.repository.update_admin_user_avatar(user_id, relpath)
        self._delete_admin_avatar_file(previous_avatar_path, keep_relpath=relpath)

        avatar_url = self._admin_avatar_url(relpath)
        updated_user = dict(user)
        updated_user["avatar_path"] = relpath
        updated_user["avatar_url"] = avatar_url
        self._set_current_admin_user(handler, updated_user)

        if self._is_ajax_request(handler):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "avatar_url": avatar_url, "message": "头像已更新。"})
            return
        self._redirect_with_message(handler, return_to, "头像已更新。", "success")

    def _send_avatar_upload_error(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        message: str,
        return_to: str,
    ) -> None:
        if self._is_ajax_request(handler):
            self._send_json(handler, status, {"ok": False, "message": message})
            return
        self._redirect_with_message(handler, return_to, message, "warning")

    def _detect_avatar_extension(self, payload: bytes) -> str | None:
        if payload.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if payload.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
            return ".webp"
        return None

    def _admin_avatar_url(self, avatar_path: str) -> str:
        normalized = str(avatar_path or "").strip().replace("\\", "/")
        if not normalized or not normalized.startswith("avatars/"):
            return ""
        return self._runtime_url(normalized)

    def _delete_admin_avatar_file(self, relpath: str, *, keep_relpath: str = "") -> None:
        normalized = str(relpath or "").strip().replace("\\", "/")
        if not normalized or normalized == keep_relpath or not normalized.startswith("avatars/"):
            return
        avatar_root = (self.settings.runtime_dir / "avatars").resolve()
        target = (self.settings.runtime_dir / Path(normalized)).resolve()
        try:
            target.relative_to(avatar_root)
        except ValueError:
            return
        if target.exists() and target.is_file():
            target.unlink()

    def _request_return_to(self, handler: BaseHTTPRequestHandler, fallback: str = "/") -> str:
        referer = str(handler.headers.get("Referer") or "").strip()
        if referer.startswith("/"):
            return self._safe_return_to(referer, fallback)
        parsed = urlparse(referer)
        host = str(handler.headers.get("Host") or "").strip()
        if parsed.netloc and parsed.netloc == host:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            return self._safe_return_to(path, fallback)
        return fallback

    def _parse_admin_user_id(self, path: str, suffix: str) -> int | None:
        prefix = "/settings/accounts/"
        suffix_value = f"/{suffix}"
        if not path.startswith(prefix) or not path.endswith(suffix_value):
            return None
        raw = path[len(prefix) : -len(suffix_value)].strip("/")
        try:
            return int(raw)
        except ValueError:
            return None
