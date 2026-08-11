"""
R7 SSO 登录复用模块（Playwright）。

供 `scripts/auto_checkin_r7.py` 以及其他 R7 页面自动化脚本复用：
  - 统一维护登录地址、默认账号密码、验证码识别与登录选择器
  - 对外提供 `build_auth()` / `ensure_logged_in()` / `login()`
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent.tms_runtime.scripts.browser_manager import BrowserLoginSelectors, TMSBrowserAuth
from agent.tms_runtime.scripts.r7_login_manager import R7SSOAuth


LOGIN_URL = (
    "https://sso.ronghuiwl.com/sso/#/login?"
    "redirect=https://r7.ronghuiwl.com/gateway/sso/auth/login&"
    "back=https://r7.ronghuiwl.com/"
)
HOME_URL = "https://r7.ronghuiwl.com/"

# 默认账号密码从环境变量读取，避免写死在代码里。
DEFAULT_USERNAME = (
    os.getenv("R7_CAOZUOCHANG_USER")
    or os.getenv("R7_USERNAME")
    or os.getenv("R7_USER")
    or ""
)
DEFAULT_PASSWORD = os.getenv("R7_CAOZUOCHANG_PASS") or os.getenv("R7_PASSWORD") or ""


DEFAULT_SELECTORS = BrowserLoginSelectors(
    # 账号输入框：用户提供的 xpath 在部分情况下可能过于严格，这里做更稳健的匹配。
    username='(//form//div[contains(@class, "is-error")]//input | //form//input[not(@type="password") and not(contains(@placeholder, "验证码"))])[1]',
    password='//form//input[@type="password"]',
    captcha_input='//form//input[@placeholder="验证码"]',
    captcha_image='//form//canvas[contains(@class, "cursor-pointer")]',
    submit='//form//button[.//span[normalize-space(.)="登录"]]',
)


def _playwright_cookie_from_requests(cookie: Any) -> dict[str, Any] | None:
    name = str(getattr(cookie, "name", "") or "").strip()
    value = str(getattr(cookie, "value", "") or "")
    if not name:
        return None

    item: dict[str, Any] = {
        "name": name,
        "value": value,
        "path": getattr(cookie, "path", None) or "/",
        "secure": bool(getattr(cookie, "secure", False)),
    }

    domain = str(getattr(cookie, "domain", "") or "").strip()
    if domain:
        item["domain"] = domain
    else:
        item["url"] = HOME_URL

    expires = getattr(cookie, "expires", None)
    if expires not in (None, ""):
        try:
            item["expires"] = int(expires)
        except (TypeError, ValueError):
            pass

    rest = getattr(cookie, "_rest", None) or {}
    if isinstance(rest, dict):
        http_only = rest.get("HttpOnly") or rest.get("httponly")
        if http_only is not None:
            item["httpOnly"] = True
        same_site = rest.get("SameSite") or rest.get("samesite")
        if same_site:
            normalized = str(same_site).strip().capitalize()
            if normalized in {"Strict", "Lax", "None"}:
                item["sameSite"] = normalized

    return item


def _inject_http_sso_state(page: Any, *, token: str, session: Any) -> None:
    init_script = f"""(() => {{
        try {{
            const token = {json.dumps(token)};
            window.localStorage.setItem("accessToken", token);
            window.sessionStorage.setItem("accessToken", token);
        }} catch (e) {{}}
    }})();"""
    try:
        page.add_init_script(init_script)
    except Exception:
        try:
            page.context.add_init_script(init_script)
        except Exception:
            pass

    cookies = []
    for cookie in getattr(session, "cookies", []) or []:
        converted = _playwright_cookie_from_requests(cookie)
        if converted:
            cookies.append(converted)

    if cookies:
        page.context.add_cookies(cookies)

    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    page.evaluate(
        """(token) => {
            window.localStorage.setItem("accessToken", token);
            window.sessionStorage.setItem("accessToken", token);
        }""",
        token,
    )

    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass


def _login_with_http_sso(page: Any, auth: TMSBrowserAuth, *, username: str, password: str) -> None:
    sso_auth = R7SSOAuth(state_path=getattr(auth, "sso_state_path", None))
    session = sso_auth.login_and_get_session(
        username=username,
        password=password,
        max_attempts=max(1, int(auth.max_attempts)),
        attach_bearer=False,
        exchange=True,
        verify=False,
    )
    token = str(sso_auth.last_token or "").strip()
    if not token:
        raise RuntimeError("R7 HTTP SSO returned empty token.")

    _inject_http_sso_state(page, token=token, session=session)
    if not auth._is_logged_in(page):
        raise RuntimeError(f"R7 HTTP SSO state was injected but browser is still on login page. url={page.url}")


def build_auth(*, max_attempts: int = 3, account_id: str = "r7_default") -> TMSBrowserAuth:
    """
    创建 R7 SSO 的浏览器登录器。
    """

    auth = TMSBrowserAuth(
        base_url="https://sso.ronghuiwl.com",
        login_path="/sso/#/login?redirect=https://r7.ronghuiwl.com/gateway/sso/auth/login&back=https://r7.ronghuiwl.com/",
        home_path="/",
        selectors=DEFAULT_SELECTORS,
        max_attempts=min(max(1, int(max_attempts)), 3),
        home_url=HOME_URL,
        login_url=LOGIN_URL,
        use_shared_session=False,
    )
    from agent.tms_runtime.sso_session_persistence import default_sso_state_path

    auth.sso_state_path = default_sso_state_path(account_id or "r7_default")
    return auth


def ensure_logged_in(page: Any, auth: TMSBrowserAuth, *, username: str, password: str) -> None:
    """
    使用传入的 auth 执行登录（成功返回，否则抛异常）。
    """

    http_error: BaseException | None = None
    try:
        _login_with_http_sso(page, auth, username=username, password=password)
        return
    except BaseException as exc:
        http_error = exc

    try:
        auth.login(page, username=username, password=password)
        return
    except BaseException as browser_exc:
        raise RuntimeError(
            "R7 login failed. "
            f"HTTP SSO failed: {type(http_error).__name__}: {http_error}; "
            f"browser fallback failed: {type(browser_exc).__name__}: {browser_exc}"
        ) from browser_exc


def login(
    page: Any,
    *,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    max_attempts: int = 3,
) -> TMSBrowserAuth:
    """
    快捷方法：创建 auth 并登录，返回 auth。
    """

    auth = build_auth(max_attempts=max_attempts)
    ensure_logged_in(page, auth, username=username, password=password)
    return auth
