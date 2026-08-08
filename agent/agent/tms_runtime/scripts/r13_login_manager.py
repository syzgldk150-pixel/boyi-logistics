import json
import logging
import os
import time
from typing import Optional, Tuple
from urllib.parse import quote, urljoin

import requests


DEFAULT_SSO_ORIGIN = "https://sso.ronghuiwl.com"
DEFAULT_R13_ORIGIN = "https://r13.ronghuiwl.com"
DEFAULT_LOGIN_API = "/gateway/sso/auth/login"
DEFAULT_EXCHANGE_API = "/gateway/sso/auth/login"


class R13SSOAuth:
    """
    Headless SSO login helper for r13.ronghuiwl.com using requests.Session.
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = os.environ.get(
                "CONFIG_PATH",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
            )

        self.config = {}
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as handle:
                self.config = json.load(handle)

        self.sso_origin = (self.config.get("r13_sso_origin") or DEFAULT_SSO_ORIGIN).rstrip("/")
        self.r13_origin = (self.config.get("r13_origin") or DEFAULT_R13_ORIGIN).rstrip("/")
        self.login_api = self.config.get("r13_login_api") or DEFAULT_LOGIN_API
        self.exchange_api = self.config.get("r13_exchange_api") or DEFAULT_EXCHANGE_API
        self.welcome_url = self.config.get("r13_welcome_url") or f"{self.r13_origin}/welcome"
        self.sso_login_url = self.config.get("r13_sso_login_url") or self._build_sso_login_url()

        self.session = requests.Session()
        disable_proxy = self.config.get("r13_disable_proxy")
        if disable_proxy is None:
            env_disable = os.environ.get("R13_DISABLE_PROXY")
            if env_disable is not None:
                disable_proxy = env_disable.strip().lower() in {"1", "true", "yes", "y", "on"}
        if disable_proxy:
            self.session.trust_env = False
        self._apply_headers()
        self.last_token: Optional[str] = None

    def _apply_headers(self) -> None:
        headers = self.config.get("r13_headers") or {}
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
        redirect = f"{self.r13_origin}/gateway/sso/auth/login"
        back = self.r13_origin
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

    def _resolve_credentials(
        self,
        username: Optional[str],
        password: Optional[str],
        account_key: Optional[str] = None,
    ) -> Tuple[str, str]:
        user_data = self.config.get("r13_user_data") or self.config.get("test_user_data") or {}

        if account_key:
            alias_map = {
                "r13": ("r13_uid", "r13_password"),
                "daxiang": ("daxiang_uid", "daxiang_password"),
                "daxiang_s": ("daxiang_s_user", "daxiang_s_password"),
                "operator": ("operator_uid", "operator_password"),
                "chexian": ("chexian_uid", "chexian_password"),
            }
            user_key, pass_key = alias_map.get(str(account_key).strip().lower(), ("", ""))
            user_value = user_data.get(user_key)
            pass_value = user_data.get(pass_key)
            if user_value and pass_value:
                return str(user_value), str(pass_value)
            raise RuntimeError(f"Missing r13 credentials for account_key={account_key}.")

        if username and password:
            return username, password

        env_user = os.environ.get("R13_USERNAME") or os.environ.get("R13_USER")
        env_pass = os.environ.get("R13_PASSWORD")
        if env_user and env_pass:
            return env_user, env_pass

        candidates = [
            ("r13_uid", "r13_password"),
            ("daxiang_uid", "daxiang_password"),
            ("daxiang_s_user", "daxiang_s_password"),
            ("operator_uid", "operator_password"),
            ("chexian_uid", "chexian_password"),
        ]
        for user_key, pass_key in candidates:
            user_value = user_data.get(user_key)
            pass_value = user_data.get(pass_key)
            if user_value and pass_value:
                return str(user_value), str(pass_value)

        raise RuntimeError("Missing r13 credentials. Provide username/password or set R13_USERNAME/R13_PASSWORD.")

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
        extra_payload = self.config.get("r13_login_payload_extra") or {}
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

        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected login payload type.")

        token = (payload.get("data") or {}).get("tokenValue")
        if payload.get("code") in (200, 0) and token:
            return str(token)

        message = payload.get("message") or payload.get("msg") or payload
        raise RuntimeError(f"SSO login failed: {message}")

    def _exchange_token(self, token: str) -> None:
        exchange_url = self._build_url(self.r13_origin, self.exchange_api)
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
            return True

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
        account_key: Optional[str] = None,
        max_attempts: int = 3,
        attach_bearer: bool = True,
        exchange: bool = True,
        verify: Optional[bool] = None,
    ) -> requests.Session:
        logger = logging.getLogger(__name__)
        username, password = self._resolve_credentials(username, password, account_key)
        if verify is None:
            verify = exchange

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
                        logger.info("R13 SSO login succeeded on attempt %s.", attempt)
                        return self.session
                    logger.info("R13 SSO login not verified on attempt %s, retrying.", attempt)
                else:
                    logger.info("R13 SSO token acquired on attempt %s.", attempt)
                    return self.session
            except Exception as exc:
                last_error = exc
                logger.warning("R13 SSO login failed on attempt %s: %s", attempt, exc)
            time.sleep(1.0)

        suffix = f" Last error: {last_error}" if last_error else ""
        raise RuntimeError(f"R13 SSO login failed after multiple attempts.{suffix}")
