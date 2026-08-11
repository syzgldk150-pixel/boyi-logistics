import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote, urljoin

import requests

from agent.tms_runtime.sso_session_persistence import (
    MAX_SSO_LOGIN_ATTEMPTS,
    SSOSessionPersistenceMixin,
    default_sso_state_path,
)


DEFAULT_SSO_ORIGIN = "https://sso.ronghuiwl.com"
DEFAULT_R7_ORIGIN = "https://r7.ronghuiwl.com"
DEFAULT_LOGIN_API = "/gateway/sso/auth/login"
DEFAULT_EXCHANGE_API = "/gateway/sso/auth/login"


class R7SSOAuth(SSOSessionPersistenceMixin):
    """
    Headless SSO login helper for r7.ronghuiwl.com using requests.Session.

    Primary use: acquire a JWT (tokenValue) for API calls that require `aurora-token`.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        *,
        disable_proxy: Optional[bool] = None,
        state_path: Optional[str | Path] = None,
    ):
        if config_path is None:
            config_path = os.environ.get(
                "CONFIG_PATH",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
            )

        self.config = {}
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as handle:
                self.config = json.load(handle)

        self.sso_origin = (self.config.get("r7_sso_origin") or DEFAULT_SSO_ORIGIN).rstrip("/")
        self.r7_origin = (self.config.get("r7_origin") or DEFAULT_R7_ORIGIN).rstrip("/")
        self.login_api = self.config.get("r7_login_api") or DEFAULT_LOGIN_API
        self.exchange_api = self.config.get("r7_exchange_api") or DEFAULT_EXCHANGE_API
        self.welcome_url = self.config.get("r7_welcome_url") or f"{self.r7_origin}/"
        self.sso_login_url = self.config.get("r7_sso_login_url") or self._build_sso_login_url()

        self.session = requests.Session()
        if disable_proxy is None:
            disable_proxy = self.config.get("r7_disable_proxy")
            if disable_proxy is None:
                env_disable = os.environ.get("R7_DISABLE_PROXY")
                if env_disable is not None:
                    disable_proxy = env_disable.strip().lower() in {"1", "true", "yes", "y", "on"}
        if disable_proxy:
            self.session.trust_env = False

        self._apply_headers()
        self.last_token: Optional[str] = None
        self.state_path = Path(state_path) if state_path else default_sso_state_path("r7_default")

    def _apply_headers(self) -> None:
        headers = self.config.get("r7_headers") or {}
        sanitized = {k: v for k, v in headers.items() if k.lower() != "cookie"}
        if sanitized:
            self.session.headers.update(sanitized)

        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        self.session.headers.setdefault("Accept", "application/json, text/plain, */*")
        self.session.headers.setdefault("Content-Type", "application/json;charset=UTF-8")
        self.session.headers.setdefault("Origin", self.sso_origin)
        self.session.headers.setdefault("Referer", self.sso_login_url)

    def _build_sso_login_url(self) -> str:
        redirect = f"{self.r7_origin}/gateway/sso/auth/login"
        back = f"{self.r7_origin}/"
        redirect_value = quote(redirect, safe="")
        back_value = quote(back, safe="")
        return f"{self.sso_origin}/sso/#/login?redirect={redirect_value}&back={back_value}"

    @staticmethod
    def _build_url(origin: str, path_or_url: str) -> str:
        if not path_or_url:
            return origin
        if path_or_url.lower().startswith("http"):
            return path_or_url
        return urljoin(origin + "/", path_or_url.lstrip("/"))

    def _resolve_credentials(self, username: Optional[str], password: Optional[str]) -> Tuple[str, str]:
        if username and password:
            return username, password

        env_user = (
            os.environ.get("R7_CAOZUOCHANG_USER")
            or os.environ.get("R7_USERNAME")
            or os.environ.get("R7_USER")
        )
        env_pass = os.environ.get("R7_CAOZUOCHANG_PASS") or os.environ.get("R7_PASSWORD")
        if env_user and env_pass:
            return env_user, env_pass

        user_data = self.config.get("r7_user_data") or self.config.get("test_user_data") or {}
        candidates = [
            ("r7_uid", "r7_password"),
            ("operator_uid", "operator_password"),
            ("chexian_uid", "chexian_password"),
            ("daxiang_uid", "daxiang_password"),
        ]
        for user_key, pass_key in candidates:
            user_value = user_data.get(user_key)
            pass_value = user_data.get(pass_key)
            if user_value and pass_value:
                return str(user_value), str(pass_value)

        raise RuntimeError(
            "Missing r7 credentials. Provide username/password or set "
            "R7_CAOZUOCHANG_USER/R7_CAOZUOCHANG_PASS."
        )

    def _preflight(self) -> None:
        url = self._build_url(self.sso_origin, "/sso/platform-config.json")
        try:
            self.session.get(url, timeout=8)
        except Exception:
            return

    def _request_token(self, username: str, password: str) -> str:
        self._preflight()
        login_url = self._build_url(self.sso_origin, self.login_api)
        payload = {"name": username, "password": password}
        extra_payload = self.config.get("r7_login_payload_extra") or {}
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)

        headers = {
            "x-appId": "sso",
            "aurora-back": self.sso_login_url,
        }
        try:
            response = self.session.post(login_url, json=payload, headers=headers, timeout=12)
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = f" status={exc.response.status_code} body={(exc.response.text or '')[:200]}"
            raise RuntimeError(f"SSO login request failed.{detail}") from exc

        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            snippet = (response.text or "")[:200]
            raise RuntimeError(f"Unexpected login response: {content_type} {snippet}")

        parsed = response.json() if response.content else {}
        if not isinstance(parsed, dict):
            raise RuntimeError("Unexpected login payload type.")

        token = (parsed.get("data") or {}).get("tokenValue")
        if parsed.get("code") in (200, 0) and token:
            return str(token)

        message = parsed.get("message") or parsed.get("msg") or parsed
        raise RuntimeError(f"SSO login failed: {message}")

    def _exchange_token(self, token: str) -> None:
        exchange_url = self._build_url(self.r7_origin, self.exchange_api)
        response = self.session.get(
            exchange_url,
            params={"accessToken": token},
            allow_redirects=True,
            timeout=12,
        )
        response.raise_for_status()

    def _verify_authenticated(self) -> bool:
        try:
            response = self.session.get(self.welcome_url, allow_redirects=False, timeout=10)
        except Exception:
            return False

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            if "sso.ronghuiwl.com" in location:
                return False
        if response.status_code == 200:
            body = response.text or ""
            if "sso.ronghuiwl.com" in body:
                return False
        return response.status_code < 500

    def login_and_get_session(
        self,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        max_attempts: int = 3,
        attach_bearer: bool = True,
        exchange: bool = True,
        verify: Optional[bool] = None,
        allow_cached: bool = True,
        allow_fresh_login: bool = True,
    ) -> requests.Session:
        logger = logging.getLogger(__name__)
        if verify is None:
            verify = exchange
        if allow_cached and self.restore_persisted_session(
            validate=bool(verify),
            validator=self._verify_authenticated,
            attach_bearer=attach_bearer,
        ):
            return self.session
        if not allow_fresh_login:
            raise RuntimeError("R7 登录态不存在或已失效，请先在账号管理中登录。")

        username, password = self._resolve_credentials(username, password)
        max_attempts = min(max(1, int(max_attempts)), MAX_SSO_LOGIN_ATTEMPTS)

        last_error: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                token = self._request_token(username, password)
                self.last_token = token
                if attach_bearer:
                    self.session.headers["Authorization"] = f"Bearer {token}"
                if exchange:
                    self._exchange_token(token)
                if verify:
                    if self._verify_authenticated():
                        self._save_sso_state(status="authenticated")
                        logger.info("R7 SSO login succeeded on attempt %s.", attempt)
                        return self.session
                    logger.info("R7 SSO login not verified on attempt %s, retrying.", attempt)
                else:
                    self._save_sso_state(status="authenticated")
                    logger.info("R7 SSO token acquired on attempt %s.", attempt)
                    return self.session
            except Exception as exc:
                last_error = exc
                logger.warning("R7 SSO login failed on attempt %s: %s", attempt, type(exc).__name__)
            time.sleep(1.0)

        self._save_sso_state(status="expired", error="R7 SSO 登录失败")
        suffix = f" Last error type: {type(last_error).__name__}" if last_error else ""
        raise RuntimeError(f"R7 SSO login failed after multiple attempts.{suffix}")
