from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, Optional

from agent.tms_runtime import captcha_ocr
from agent.tms_runtime.session_broker import get_session_broker


def resolve_chromium_executable() -> str:
    for env_name in ("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "CHROMIUM_EXECUTABLE", "CHROME_BIN"):
        value = str(os.getenv(env_name) or "").strip()
        if value and os.path.exists(value):
            return value

    for candidate in (
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
    ):
        if os.path.exists(candidate):
            return candidate

    for command in ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome"):
        resolved = shutil.which(command)
        if resolved:
            return resolved

    return ""


def chromium_launch_kwargs(*, headless: bool = True, slow_mo_ms: int = 0, channel: Optional[str] = None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headless": headless,
        "slow_mo": slow_mo_ms,
        "args": ["--no-sandbox"],
    }
    if channel:
        options["channel"] = channel
        return options

    executable = resolve_chromium_executable()
    if executable:
        options["executable_path"] = executable
    return options


@dataclass(frozen=True)
class BrowserLoginSelectors:
    username: str
    password: str
    captcha_image: str
    captcha_input: str
    submit: str


DEFAULT_SELECTORS = BrowserLoginSelectors(
    # Provided by user as XPath (Playwright locator supports xpath=...).
    username="//form//input[@name=\"username\"]",
    password="//form//input[@type=\"password\"]",
    captcha_image="//form//img[@id=\"yzm_img\"]",
    captcha_input="//form//input[@name=\"validateCode\"]",
    submit="//form//a[normalize-space(.)=\"登录\"]",
)


class TMSBrowserAuth:
    def __init__(
        self,
        *,
        base_url: str = "https://tms.ronghuiwl.com",
        login_path: str = "/system/login",
        home_path: str = "/",
        selectors: BrowserLoginSelectors = DEFAULT_SELECTORS,
        max_attempts: int = 6,
        login_url: Optional[str] = None,
        home_url: Optional[str] = None,
        use_shared_session: bool = True,
        profile: str = "default",
    ):
        self.base_url = base_url.rstrip("/")
        self.login_url = (login_url or f"{self.base_url}{login_path}").strip()
        self.home_url = (home_url or f"{self.base_url}{home_path}").strip()
        self.selectors = selectors
        self.max_attempts = max_attempts
        self.use_shared_session = bool(use_shared_session)
        self.profile = str(profile or "default").strip() or "default"

    @staticmethod
    def _sel(selector: str) -> str:
        selector = (selector or "").strip()
        if selector.startswith("//") or selector.startswith("(//"):
            return f"xpath={selector}"
        return selector

    def _wait_visible(self, page: Any, selector: str, *, timeout_ms: int = 15_000):
        return page.wait_for_selector(self._sel(selector), state="visible", timeout=timeout_ms)

    def _login_state_summary(self, page: Any, *, captcha_text: str = "") -> str:
        parts = []
        try:
            parts.append(f"url={page.url}")
        except Exception:
            pass

        for label, selector in (
            ("username_visible", self.selectors.username),
            ("password_visible", self.selectors.password),
            ("captcha_visible", self.selectors.captcha_input),
        ):
            try:
                parts.append(f"{label}={bool(page.is_visible(self._sel(selector)))}")
            except Exception:
                pass

        try:
            submit = self._loc(page, self.selectors.submit)
            if submit.count() > 0:
                parts.append(f"submit_enabled={bool(submit.first.is_enabled())}")
        except Exception:
            pass

        try:
            captcha = self._loc(page, self.selectors.captcha_input)
            if captcha.count() > 0 and captcha.first.is_visible():
                parts.append(f"captcha_ocr_empty={not bool(str(captcha_text or '').strip())}")
        except Exception:
            pass

        try:
            body_text = self._loc(page, "body").first.inner_text(timeout=1_000)
            body_text = " ".join(str(body_text or "").split())
            if body_text:
                parts.append(f"body_text={body_text[:160]}")
        except Exception:
            pass

        return "; ".join(parts)

    def _is_logged_in(self, page: Any) -> bool:
        url = page.url or ""
        if "/system/login" in url or "#/login" in url:
            return False
        try:
            body = (page.content() or "").strip()
        except Exception:
            body = ""

        # If the server returns a JSON error payload, treat it as not logged in / not usable.
        if body.startswith("{") and ("\"success\"" in body or "\"message\"" in body):
            return False

        # DOM-based: if we can still see the login form markers, treat as not logged in.
        try:
            if page.is_visible(self._sel(self.selectors.captcha_input)):
                return False
        except Exception:
            pass
        try:
            if page.is_visible(self._sel(self.selectors.username)) and page.is_visible(self._sel(self.selectors.password)):
                return False
        except Exception:
            pass

        return True

    def login(self, page: Any, *, username: str, password: str) -> None:
        if self.use_shared_session:
            _ = (username, password)
            get_session_broker(self.profile).ensure_authenticated(validate=True)
            page.goto(self.home_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            if not self._is_logged_in(page):
                raise RuntimeError("Shared TMS browser session is unavailable or expired.")
            return

        username = str(username or "").strip()
        password = str(password or "").strip()
        if not username or not password:
            raise RuntimeError("Browser login requires username and password.")

        last_error: BaseException | None = None
        last_summary = ""
        for _attempt in range(1, max(1, int(self.max_attempts)) + 1):
            captcha_text = ""
            try:
                page.goto(self.login_url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                if self._is_logged_in(page):
                    return

                self._wait_visible(page, self.selectors.username).fill(username)
                self._wait_visible(page, self.selectors.password).fill(password)
                captcha_text = self._read_captcha_if_visible(page)
                if captcha_text:
                    self._wait_visible(page, self.selectors.captcha_input).fill(captcha_text)
                self._wait_visible(page, self.selectors.submit).click()
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                if self._is_logged_in(page):
                    page.goto(self.home_url, wait_until="domcontentloaded", timeout=60_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    if self._is_logged_in(page):
                        return
                last_summary = self._login_state_summary(page, captcha_text=captcha_text)
            except BaseException as exc:
                last_error = exc
                try:
                    last_summary = self._login_state_summary(page, captcha_text=captcha_text)
                except Exception:
                    last_summary = ""

        if last_error is not None:
            suffix = f"; {last_summary}" if last_summary else ""
            raise RuntimeError(f"Browser login failed: {type(last_error).__name__}: {last_error}{suffix}") from last_error
        if last_summary:
            raise RuntimeError(f"Browser login failed: {last_summary}")
        raise RuntimeError("Browser login failed.")

    def _read_captcha_if_visible(self, page: Any) -> str:
        try:
            captcha_input = self._loc(page, self.selectors.captcha_input)
            if captcha_input.count() <= 0 or not captcha_input.first.is_visible():
                return ""
        except Exception:
            return ""

        try:
            captcha_image = self._loc(page, self.selectors.captcha_image)
            if captcha_image.count() <= 0 or not captcha_image.first.is_visible():
                return ""
            image_bytes = captcha_image.first.screenshot()
            return captcha_ocr.classify_captcha_image(image_bytes, max_length=4)
        except Exception:
            return ""

    @staticmethod
    def _loc(page: Any, selector: str):
        selector = (selector or "").strip()
        if selector.startswith("//") or selector.startswith("(//"):
            return page.locator(f"xpath={selector}")
        return page.locator(selector)


def launch_browser(
    *,
    headless: bool = True,
    slow_mo_ms: int = 0,
    channel: Optional[str] = None,
    use_tms_storage_state: bool = True,
    profile: str = "default",
):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as first_error:  # pragma: no cover
        # Some repos keep a local `playwright/` folder (e.g. Docker assets) which can shadow the PyPI package.
        # Retry the import with CWD removed from sys.path.
        try:
            import importlib
            import sys

            original_sys_path = list(sys.path)
            sys.path = [p for p in original_sys_path if p not in ("", ".", os.getcwd())]
            sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
        except Exception as second_error:
            raise RuntimeError(
                "Playwright 未安装或被本地 `playwright/` 目录遮蔽：请执行 `python -m pip install playwright`，并安装浏览器 `python -m playwright install chromium`。"
            ) from second_error
        finally:
            try:
                sys.path = original_sys_path
            except Exception:
                pass

    p = sync_playwright().start()
    browser = p.chromium.launch(**chromium_launch_kwargs(headless=headless, slow_mo_ms=slow_mo_ms, channel=channel))
    context_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
    if use_tms_storage_state:
        context_kwargs["storage_state"] = get_session_broker(profile).get_storage_state_path(validate=True)
    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    return p, browser, context, page
