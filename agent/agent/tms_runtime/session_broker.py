"""Stable facade for TMS session profiles."""

from agent.tms_runtime.session_support import *  # noqa: F403
from agent.tms_runtime.session_adapters import (
    RonghuiSessionAdapter,
    SessionProviderAdapter,
    YundaSessionAdapter,
)
from agent.tms_runtime.session_persistence import SessionPersistenceMixin
from agent.tms_runtime.session_validation_service import SessionValidationMixin


class SessionBroker(SessionPersistenceMixin, SessionValidationMixin):
    def __init__(
        self,
        *,
        profile_name: str = "default",
        username_envs: tuple[str, ...] = DEFAULT_USERNAME_ENVS,
        password_envs: tuple[str, ...] = DEFAULT_PASSWORD_ENVS,
        phone_envs: tuple[str, ...] = DEFAULT_PHONE_ENVS,
        base_origin_envs: tuple[str, ...] = ("TMS_BASE_ORIGIN",),
        base_origin_default: str = BASE_ORIGIN,
        login_path_envs: tuple[str, ...] = (),
        login_path_default: str = LOGIN_PATH,
        home_path_envs: tuple[str, ...] = (),
        home_path_default: str = HOME_PATH,
        login_url_keywords: tuple[str, ...] = ("/system/login",),
        login_body_markers: tuple[str, ...] = ("用户名", "验证码", "system/login"),
        login_page_marker: str = LOGIN_PAGE_MARKER,
        login_mode: str = "image",
        require_phone: bool = False,
    ) -> None:
        module_dir = Path(__file__).resolve().parent
        self.profile_name = _safe_profile_name(profile_name)
        self._username_envs = tuple(username_envs)
        self._password_envs = tuple(password_envs)
        self._phone_envs = tuple(phone_envs)
        self._base_origin_envs = tuple(base_origin_envs)
        self._base_origin_default = str(base_origin_default or BASE_ORIGIN).strip() or BASE_ORIGIN
        self._login_path_envs = tuple(login_path_envs)
        self._login_path_default = str(login_path_default or LOGIN_PATH).strip() or LOGIN_PATH
        self._home_path_envs = tuple(home_path_envs)
        self._home_path_default = str(home_path_default or HOME_PATH).strip() or HOME_PATH
        self._login_url_keywords = tuple(keyword for keyword in login_url_keywords if keyword)
        self._login_body_markers = tuple(marker for marker in login_body_markers if marker)
        self._login_page_marker = str(login_page_marker or "").strip()
        self._login_mode = str(login_mode or "image").strip().lower() or "image"
        self._require_phone = bool(require_phone)
        self._state_dir = module_dir / "state"
        if self.profile_name != "default":
            self._state_dir = self._state_dir / self.profile_name
        self._state_store = SessionStateStore(self._state_dir)
        self._meta_path = self._state_dir / "session_meta.json"
        self._storage_state_path = self._state_dir / "storage_state.json"
        self._cookies_path = self._state_dir / "cookies.json"
        self._pending_storage_state_path = self._state_dir / "pending_storage_state.json"
        self._pending_login_state_path = self._state_dir / "pending_login_state.json"
        self._login_profile_path = self._state_dir / "login_profile.json"
        self._lock = threading.RLock()
        # Console-entered passwords are deliberately process-local.
        self._volatile_credentials: dict[str, str] = {}
        self._pending: PendingBrowser | None = None
        self._provider_adapter: SessionProviderAdapter = (
            YundaSessionAdapter(self) if self._login_mode.startswith("yunda") else RonghuiSessionAdapter(self)
        )

    def _read_existing_meta_for_transition(self) -> dict[str, Any]:
        return self._state_store.read_dict(self._meta_path) or {}

    def _dependency_error(self, message: str) -> TMSAuthStateError:
        return TMSAuthStateError(
            "AUTH_UNAVAILABLE",
            (
                f"{message} "
                "请在项目 agent 目录执行 "
                "`.venv/bin/python -m pip install -r requirements.txt` 和 "
                "`.venv/bin/python -m playwright install chromium` 后重试。"
            ),
        )

    def send_code(self) -> dict[str, Any]:
        """Start the provider-specific login flow through the stable façade."""
        return self._provider_adapter.send_code()

    def submit_code(self, code: str) -> dict[str, Any]:
        """Complete the provider-specific login flow through the stable façade."""
        return self._provider_adapter.submit_code(code)

    def _session_from_saved_state_locked(self) -> requests.Session:
        storage_state = self._load_storage_state()
        cookies = storage_state.get("cookies", [])
        session = requests.Session()
        session.mount("https://", _RonghuiTLSAdapter())
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": self.resolve_login_config().home_url,
                "Origin": self.resolve_login_config().base_origin,
            }
        )
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            if not name:
                continue
            session.cookies.set(
                name,
                str(cookie.get("value") or ""),
                domain=cookie.get("domain"),
                path=cookie.get("path") or "/",
            )
        return session

    def describe_status(self, *, validate: bool = True, force: bool = False) -> dict[str, Any]:
        with self._lock:
            meta = self._validate_locked(force=force) if validate else self._save_meta(self._load_meta())
            credentials = self._credentials_status_locked()
            return {
                "profile": self.profile_name,
                "status": meta["status"],
                "label": meta["label"],
                "status_tone": _status_tone(meta["status"]),
                "authenticated": meta["status"] == "authenticated",
                "pending_code": meta["status"] == "pending_code",
                "last_validation_at": meta.get("last_validation_at", ""),
                "last_error_summary": meta.get("last_error_summary", ""),
                "authenticated_at": meta.get("authenticated_at", ""),
                "pending_since": meta.get("pending_since", ""),
                "expires_at": meta.get("expires_at", ""),
                "has_saved_credentials": credentials["has_saved_credentials"],
                "has_manual_credentials": credentials["has_manual_credentials"],
                "has_env_credentials": credentials["has_env_credentials"],
                "credential_source": credentials["credential_source"],
                "challenge_type": meta.get("challenge_type", "") if meta["status"] == "pending_code" else "",
                "challenge_label": meta.get("challenge_label", "") if meta["status"] == "pending_code" else "",
                "captcha_image": meta.get("captcha_image", "") if meta["status"] == "pending_code" else "",
                "captcha_image_mime": meta.get("captcha_image_mime", "") if meta["status"] == "pending_code" else "",
                "captcha_captured_at": meta.get("captcha_captured_at", "") if meta["status"] == "pending_code" else "",
            }

    def ensure_authenticated(self, *, validate: bool = True) -> dict[str, Any]:
        status = self.describe_status(validate=validate)
        if status["status"] == "authenticated":
            return status
        if status["status"] == "pending_code":
            challenge_label = str(status.get("challenge_label") or "验证码").strip()
            challenge_type = str(status.get("challenge_type") or "").strip().lower()
            if self._is_yunda_mode():
                raise TMSAuthStateError("AUTH_PENDING_CODE", YUNDA_SMS_PENDING_MESSAGE)
            if challenge_type == "image":
                raise TMSAuthStateError("AUTH_PENDING_CODE", f"融辉{challenge_label}已生成，等待人工提交验证码。")
            raise TMSAuthStateError("AUTH_PENDING_CODE", "短信验证码已发送，等待人工提交验证码。")
        raise TMSAuthStateError("AUTH_REQUIRED", status.get("last_error_summary") or "当前未登录或登录态已过期。")

    def build_requests_session(self, *, validate: bool = True) -> requests.Session:
        with self._lock:
            self.ensure_authenticated(validate=validate)
            return self._session_from_saved_state_locked()

    def build_requests_session_unchecked(self) -> requests.Session:
        with self._lock:
            if not self._storage_state_path.exists():
                raise TMSAuthStateError("AUTH_REQUIRED", "Shared storage state does not exist; please log in again.")
            return self._session_from_saved_state_locked()

    def get_storage_state_path(self, *, validate: bool = True) -> str:
        with self._lock:
            self.ensure_authenticated(validate=validate)
            if not self._storage_state_path.exists():
                raise TMSAuthStateError("AUTH_REQUIRED", "共享 storage state 不存在，请重新登录。")
            return str(self._storage_state_path)

    def __getattr__(self, name):
        adapter = self.__dict__.get("_provider_adapter")
        if adapter is not None and hasattr(type(adapter), name):
            return getattr(adapter, name)
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")


_SESSION_BROKERS: dict[str, SessionBroker] = {}


def get_session_broker(profile_name: str = "default") -> SessionBroker:
    normalized = _safe_profile_name(profile_name)
    if normalized == "ronghui":
        normalized = "default"
    if normalized not in _SESSION_BROKERS:
        if normalized == "price" or normalized.startswith("price_"):
            _SESSION_BROKERS[normalized] = SessionBroker(
                profile_name=normalized,
                username_envs=PRICE_USERNAME_ENVS,
                password_envs=PRICE_PASSWORD_ENVS,
                phone_envs=PRICE_PHONE_ENVS,
            )
        elif normalized == "yunda" or normalized.startswith("yunda_"):
            _SESSION_BROKERS[normalized] = SessionBroker(
                profile_name=normalized,
                username_envs=YUNDA_USERNAME_ENVS,
                password_envs=YUNDA_PASSWORD_ENVS,
                phone_envs=YUNDA_PHONE_ENVS,
                base_origin_envs=YUNDA_BASE_ORIGIN_ENVS,
                base_origin_default=YUNDA_BASE_ORIGIN,
                login_path_envs=YUNDA_LOGIN_PATH_ENVS,
                login_path_default=YUNDA_LOGIN_PATH,
                home_path_envs=YUNDA_HOME_PATH_ENVS,
                home_path_default=YUNDA_HOME_PATH,
                login_url_keywords=("ky-sso.yunda56.com/login", "/login"),
                login_body_markers=("用户登录", "请输入用户名"),
                login_page_marker="",
                login_mode="yunda_password",
                require_phone=False,
            )
        else:
            _SESSION_BROKERS[normalized] = SessionBroker(profile_name=normalized)
    return _SESSION_BROKERS[normalized]
