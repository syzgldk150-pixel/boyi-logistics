"""Bounded live validation and capability-scoped session construction."""

from agent.tms_runtime.session_support import *  # noqa: F403


_RONGHUI_CAPABILITIES = frozenset(
    {
        "ronghui_home",
        "ronghui_scan",
        "ronghui_problem",
        "ronghui_clock",
        "ronghui_finance",
        "ronghui_write",
    }
)
_YUNDA_CAPABILITIES = frozenset(
    {
        "yunda_inms",
        "yunda_report",
        "yunda_message",
        "yunda_problem",
    }
)
_KNOWN_CAPABILITIES = _RONGHUI_CAPABILITIES | _YUNDA_CAPABILITIES

_RONGHUI_MENU_PROBES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ronghui_scan": (
        "快件跟踪",
        ("FIND_SACN_TRACK_BY_CODE", "快件跟踪"),
    ),
    "ronghui_problem": (
        "登记问题件查询",
        ("FIND_PROBLEM_REGISTER_LIST", "登记问题件查询"),
    ),
    "ronghui_clock": (
        "网点到离港记录",
        ("FIND_REACH_OR_LEAVE_PORT_DETNEW", "REACH_OR_LEAVE_PORT_TYPE", "网点到离港记录"),
    ),
    "ronghui_finance": (
        "结算明细查询",
        (
            "FIND_BALANCE_QRY_WST_WITH_SITE",
            "FIND_BALANCE_QRY_TJ_WST",
            "FIND_BALANCE_QRY_TJ_DETAIL",
        ),
    ),
}


class SessionValidationMixin:
    def _install_session_auth_hook(self, session: requests.Session) -> requests.Session:
        if getattr(session, "_boyi_auth_hook_installed", False):
            return session

        def raise_on_login(response: Any, *_args: Any, **_kwargs: Any) -> Any:
            if self._is_yunda_mode():
                headers = getattr(response, "headers", {}) or {}
                location = str(headers.get("Location") or headers.get("location") or "").lower()
                current_url = str(getattr(response, "url", "") or "").lower()

                def is_explicit_login_url(value: str) -> bool:
                    parsed = urlparse(value)
                    host = str(parsed.hostname or "").lower()
                    path = str(parsed.path or "").lower()
                    if host == "ky-sso.yunda56.com":
                        return path.startswith(("/login", "/public/sms/"))
                    if not host:
                        return path.startswith(("/login", "/public/sms/"))
                    return host.endswith(".yunda56.com") and path.startswith("/login")

                is_login = is_explicit_login_url(current_url) or is_explicit_login_url(location)
                content_type = str(headers.get("content-type") or "").lower()
                if not is_login and "text/html" in content_type:
                    try:
                        body = str(getattr(response, "text", "") or "").lower()
                    except Exception:
                        body = ""
                    is_login = any(
                        marker in body
                        for marker in (
                            "ky-sso.yunda56.com/login",
                            'id="login_form"',
                            "id='login_form'",
                            'name="login_form"',
                            "name='login_form'",
                        )
                    )
                if is_login:
                    raise TMSAuthStateError("AUTH_REQUIRED", "韵达登录态已失效，请重新登录。")
            elif looks_like_ronghui_login(response):
                raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。")
            return response

        session.hooks.setdefault("response", []).append(raise_on_login)
        setattr(session, "_boyi_auth_hook_installed", True)
        return session

    def _validate_ronghui_home_once(
        self,
        session: requests.Session,
        config: LoginConfig,
    ) -> None:
        try:
            response = session.get(config.home_url, allow_redirects=False, timeout=15)
        except requests.RequestException as exc:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                "融辉首页只读探针请求失败。",
            ) from exc
        if looks_like_ronghui_login(response):
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。")
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"融辉目标页面返回 HTTP {getattr(response, 'status_code', '')}。",
            )

    @staticmethod
    def _walk_ronghui_menu_nodes(nodes: list[Any]):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            yield node
            children = node.get("children") or []
            if isinstance(children, list):
                yield from SessionValidationMixin._walk_ronghui_menu_nodes(children)

    def _load_ronghui_menu_once(
        self,
        session: requests.Session,
        config: LoginConfig,
    ) -> list[Any]:
        try:
            response = session.get(
                _join_origin_path(config.base_origin, RONGHUI_MENU_VALIDATION_PATH),
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": config.home_url,
                    "X-Requested-With": "XMLHttpRequest",
                },
                allow_redirects=False,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                "融辉菜单只读探针请求失败。",
            ) from exc
        if looks_like_ronghui_login(response):
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。")
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"融辉菜单只读探针返回 HTTP {getattr(response, 'status_code', '')}。",
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                "融辉菜单只读探针返回非 JSON。",
            ) from exc
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                "融辉菜单只读探针返回失败状态。",
            )
        result = payload.get("result")
        data = result.get("data") if isinstance(result, dict) else result
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as exc:
                raise TMSAuthStateError(
                    "CAPABILITY_UNAVAILABLE",
                    "融辉菜单只读探针数据无法解析。",
                ) from exc
        if not isinstance(data, list):
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                "融辉菜单只读探针未返回菜单树。",
            )
        return data

    def _validate_ronghui_menu_capability_once(
        self,
        session: requests.Session,
        config: LoginConfig,
        capability: str,
        *,
        menu_nodes: list[Any] | None = None,
    ) -> None:
        menu_text, markers = _RONGHUI_MENU_PROBES[capability]
        nodes = menu_nodes if menu_nodes is not None else self._load_ronghui_menu_once(session, config)
        candidates: set[str] = set()
        for node in self._walk_ronghui_menu_nodes(nodes):
            label = str(node.get("text") or node.get("name") or "").strip()
            raw_url = str(node.get("url") or "").strip()
            if label == menu_text and raw_url and "/widget/home" in raw_url:
                candidates.add(_join_origin_path(config.base_origin, raw_url))
        if len(candidates) != 1:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"融辉{menu_text}只读入口未唯一解析。",
            )
        try:
            response = session.get(
                next(iter(candidates)),
                allow_redirects=False,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"融辉{menu_text}只读探针请求失败。",
            ) from exc
        if looks_like_ronghui_login(response):
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录态已失效，请重新登录。")
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"融辉{menu_text}只读探针返回 HTTP {getattr(response, 'status_code', '')}。",
            )
        body = str(getattr(response, "text", "") or "")
        if not any(marker in body for marker in markers):
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"融辉{menu_text}只读探针缺少页面标记。",
            )

    def _validate_yunda_inms_once(
        self,
        session: requests.Session,
        config: LoginConfig,
    ) -> None:
        response = session.get(
            YUNDA_INMS_INDEX_URL,
            headers={"Referer": config.home_url},
            allow_redirects=True,
            timeout=30,
        )
        body = getattr(response, "text", "") or ""
        error = self._yunda_inms_auth_error(response, body)
        if error:
            raise TMSAuthStateError("AUTH_REQUIRED", error)
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"韵达运单子系统返回 HTTP {getattr(response, 'status_code', '')}。",
            )

    def _validate_yunda_report_once(
        self,
        session: requests.Session,
        _config: LoginConfig,
    ) -> None:
        target_date = dt.datetime.now().date()
        response = session.get(
            yunda_report.search_url(),
            params=yunda_report.build_search_params({}, target_date=target_date, limit=1, offset=0),
            headers=self._yunda_report_headers(yunda_report.page_url()),
            allow_redirects=False,
            timeout=30,
        )
        body = getattr(response, "text", "") or ""
        error = self._yunda_report_auth_error(response, body)
        if error:
            raise TMSAuthStateError("AUTH_REQUIRED", error)
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"韵达报表接口返回 HTTP {getattr(response, 'status_code', '')}。",
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise TMSAuthStateError("CAPABILITY_UNAVAILABLE", "韵达报表接口返回非 JSON。") from exc
        if not isinstance(payload, dict) or "rows" not in payload or "total" not in payload:
            raise TMSAuthStateError("CAPABILITY_UNAVAILABLE", "韵达报表接口缺少 rows/total。")

    def _validate_yunda_message_once(
        self,
        session: requests.Session,
        _config: LoginConfig,
    ) -> None:
        response = session.get(
            YUNDA_USER_INFO_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": YUNDA_CLIENT_HOME_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
            timeout=15,
        )
        body = getattr(response, "text", "") or ""
        error = self._yunda_message_auth_error(response, body)
        if error:
            raise TMSAuthStateError("AUTH_REQUIRED", error)
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"韵达用户接口返回 HTTP {getattr(response, 'status_code', '')}。",
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise TMSAuthStateError("CAPABILITY_UNAVAILABLE", "韵达用户接口返回非 JSON。") from exc
        if not self._extract_yunda_message_user_id(payload):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达用户接口未返回登录身份。")

    def _validate_yunda_problem_once(
        self,
        session: requests.Session,
        _config: LoginConfig,
    ) -> None:
        response = session.get(
            YUNDA_PROBLEM_QUERY_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": YUNDA_CLIENT_HOME_URL,
            },
            allow_redirects=True,
            timeout=30,
        )
        body = getattr(response, "text", "") or ""
        error = self._yunda_problem_auth_error(response, body)
        if error:
            raise TMSAuthStateError("AUTH_REQUIRED", error)
        if int(getattr(response, "status_code", 0) or 0) != 200:
            raise TMSAuthStateError(
                "CAPABILITY_UNAVAILABLE",
                f"韵达问题件子系统返回 HTTP {getattr(response, 'status_code', '')}。",
            )

    def _validate_capability_once(
        self,
        session: requests.Session,
        capability: str,
    ) -> None:
        if capability == "ronghui_write":
            raise TMSAuthStateError(
                "CAPABILITY_UNKNOWN",
                "融辉写能力没有安全的只读探针，未执行写入测试。",
            )
        config = self.resolve_login_config()
        if capability == "ronghui_home":
            self._validate_ronghui_home_once(session, config)
            return
        if capability in _RONGHUI_MENU_PROBES:
            self._validate_ronghui_menu_capability_once(session, config, capability)
            return
        validators = {
            "yunda_inms": self._validate_yunda_inms_once,
            "yunda_report": self._validate_yunda_report_once,
            "yunda_message": self._validate_yunda_message_once,
            "yunda_problem": self._validate_yunda_problem_once,
        }
        validator = validators.get(capability)
        if validator is None:
            raise TMSAuthStateError(
                "SESSION_CAPABILITY_UNKNOWN",
                f"未知会话 capability: {capability}",
            )
        validator(session, config)

    def open_capability_session(
        self,
        capability: str,
        *,
        validate: bool = True,
    ) -> requests.Session:
        normalized = str(capability or "").strip().lower()
        if normalized not in _KNOWN_CAPABILITIES:
            raise TMSAuthStateError(
                "SESSION_CAPABILITY_UNKNOWN",
                f"未知会话 capability: {normalized or '<empty>'}",
            )
        if self._is_yunda_mode() != (normalized in _YUNDA_CAPABILITIES):
            raise TMSAuthStateError(
                "SESSION_CAPABILITY_MISMATCH",
                "会话账号与目标 capability 不匹配。",
            )
        with self._lock:
            if self._active_login_token is not None:
                raise TMSAuthStateError("BLOCKED_LOGIN", "该账号正在登录，本次动作未排队。")
            if not self._storage_state_path.exists():
                raise TMSAuthStateError("AUTH_REQUIRED", "共享登录态不存在，请重新登录。")
            epoch = self._state_epoch
            session = self._session_from_saved_state_locked()
        if not validate:
            return session

        try:
            self._validate_capability_once(session, normalized)
        except TMSAuthStateError as exc:
            if exc.code == "AUTH_REQUIRED":
                with self._lock:
                    if self._state_epoch == epoch:
                        meta = self._load_meta()
                        self._save_meta(
                            {
                                **meta,
                                "status": "expired",
                                "last_validation_at": _format_ts(_now_ts()),
                                "last_error_summary": str(exc),
                            }
                        )
            raise

        with self._lock:
            if self._state_epoch != epoch or self._active_login_token is not None:
                raise TMSAuthStateError(
                    "BLOCKED_LOGIN",
                    "会话验证期间账号状态已变化，本次结果已丢弃。",
                )
            meta = self._load_meta()
            self._save_meta(
                {
                    **meta,
                    "status": "authenticated",
                    "last_validation_at": _format_ts(_now_ts()),
                    "last_error_summary": "",
                    "authenticated_at": meta.get("authenticated_at") or _format_ts(_now_ts()),
                }
            )
        return session

    def _validate(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            meta = self._load_meta()
            if self._active_login_token is not None:
                return self._save_meta(meta)
            if str(meta.get("status") or "") == "pending_code" and self._pending_storage_state_path.exists():
                return self._save_meta(meta)
            status = str(meta.get("status") or "logged_out")
            if (
                status in {"logged_out", "error"}
                and self._storage_state_path.exists()
                and str(meta.get("authenticated_at") or "").strip()
            ):
                status = "authenticated"
                meta = {**meta, "status": status, "last_error_summary": ""}
            if status not in {"authenticated", "expired"} or not self._storage_state_path.exists():
                return self._save_meta(meta)
            if not self._is_yunda_mode():
                context_status, context_error, _changed = self._normalize_ronghui_user_context_state_locked()
                if context_status != "ready":
                    return self._save_meta(
                        {
                            **meta,
                            "status": "expired",
                            "last_validation_at": _format_ts(_now_ts()),
                            "last_error_summary": context_error,
                        }
                    )
            last_validation_text = str(meta.get("last_validation_at") or "").strip()
            if not force and last_validation_text:
                try:
                    last_validation = time.mktime(time.strptime(last_validation_text, "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    last_validation = 0
                if _now_ts() - last_validation <= VALIDATION_TTL_SEC:
                    return self._save_meta(meta)
            epoch = self._state_epoch
            try:
                session = self._session_from_saved_state_locked()
            except Exception as exc:
                return self._save_meta(
                    {
                        **meta,
                        "status": "error",
                        "last_validation_at": _format_ts(_now_ts()),
                        "last_error_summary": f"登录态校验失败: {exc}",
                    }
                )
            capability = "yunda_message" if self._is_yunda_mode() else "ronghui_home"

        try:
            self._validate_capability_once(session, capability)
            next_status = "authenticated"
            error_text = ""
        except TMSAuthStateError as exc:
            next_status = "expired" if exc.code == "AUTH_REQUIRED" else "error"
            error_text = str(exc)
        except Exception as exc:
            next_status = "error"
            error_text = f"登录态校验失败: {exc}"

        with self._lock:
            if self._state_epoch != epoch or self._active_login_token is not None:
                return self._save_meta(self._load_meta())
            current = self._load_meta()
            return self._save_meta(
                {
                    **current,
                    "status": next_status,
                    "last_validation_at": _format_ts(_now_ts()),
                    "last_error_summary": error_text,
                }
            )

    def validate_health_matrix(self) -> dict[str, Any]:
        capabilities = sorted(_YUNDA_CAPABILITIES if self._is_yunda_mode() else _RONGHUI_CAPABILITIES)
        with self._lock:
            if not self._storage_state_path.exists():
                return {
                    "profile": self.profile_name,
                    "status": "unavailable",
                    "capabilities": {
                        name: {"status": "AUTH_REQUIRED", "error": "共享登录态不存在。"}
                        for name in capabilities
                    },
                }
            session = self._session_from_saved_state_locked()

        if not self._is_yunda_mode():
            config = self.resolve_login_config()
            matrix: dict[str, dict[str, str]] = {}
            try:
                menu_nodes = self._load_ronghui_menu_once(session, config)
                menu_error: TMSAuthStateError | None = None
            except TMSAuthStateError as exc:
                menu_nodes = []
                menu_error = exc
            except Exception:
                menu_nodes = []
                menu_error = TMSAuthStateError(
                    "CAPABILITY_UNAVAILABLE",
                    "融辉菜单只读探针执行失败。",
                )
            try:
                self._validate_ronghui_home_once(session, config)
                matrix["ronghui_home"] = {"status": "ok", "error": ""}
            except TMSAuthStateError as exc:
                matrix["ronghui_home"] = {"status": exc.code, "error": str(exc)}
            except Exception:
                matrix["ronghui_home"] = {
                    "status": "CAPABILITY_UNAVAILABLE",
                    "error": "融辉首页只读探针执行失败。",
                }
            for capability in sorted(_RONGHUI_MENU_PROBES):
                if menu_error is not None:
                    matrix[capability] = {
                        "status": menu_error.code,
                        "error": str(menu_error),
                    }
                    continue
                try:
                    self._validate_ronghui_menu_capability_once(
                        session,
                        config,
                        capability,
                        menu_nodes=menu_nodes,
                    )
                    matrix[capability] = {"status": "ok", "error": ""}
                except TMSAuthStateError as exc:
                    matrix[capability] = {"status": exc.code, "error": str(exc)}
                except Exception:
                    matrix[capability] = {
                        "status": "CAPABILITY_UNAVAILABLE",
                        "error": "融辉能力只读探针执行失败。",
                    }
            matrix["ronghui_write"] = {
                "status": "UNKNOWN",
                "error": "没有安全的只读探针，未执行写入测试。",
            }
            overall = "ok" if all(item["status"] == "ok" for item in matrix.values()) else "degraded"
            return {"profile": self.profile_name, "status": overall, "capabilities": matrix}

        matrix: dict[str, dict[str, str]] = {}
        for capability in capabilities:
            try:
                self._validate_capability_once(session, capability)
                matrix[capability] = {"status": "ok", "error": ""}
            except TMSAuthStateError as exc:
                matrix[capability] = {"status": exc.code, "error": str(exc)}
            except Exception as exc:
                matrix[capability] = {"status": "CAPABILITY_UNAVAILABLE", "error": str(exc)}
        overall = "ok" if all(item["status"] == "ok" for item in matrix.values()) else "degraded"
        return {"profile": self.profile_name, "status": overall, "capabilities": matrix}
