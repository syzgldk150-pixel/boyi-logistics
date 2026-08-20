"""Provider login implementation operating through a broker context."""

from agent.tms_runtime.session_support import *  # noqa: F403


class ProviderSessionAdapterBase:
    def __init__(self, broker):
        object.__setattr__(self, "_broker", broker)

    def __getattribute__(self, name):
        if name not in {"_broker", "__class__", "__dict__", "__getattr__", "__setattr__"}:
            broker = object.__getattribute__(self, "_broker")
            if name in broker.__dict__:
                return broker.__dict__[name]
        return object.__getattribute__(self, name)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_broker"), name)

    def __setattr__(self, name, value):
        if name == "_broker":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_broker"), name, value)

    def _is_yunda_mode(self) -> bool:
        return self._login_mode == "yunda_password" or self.profile_name == "yunda"

    def _locator_visible(self, page: Any, selector: str, *, timeout_ms: int = 1_000) -> bool:
        try:
            locator = page.locator(selector)
            return locator.count() > 0 and locator.first.is_visible(timeout=timeout_ms)
        except AttributeError:
            return True
        except Exception:
            return False

    def _capture_ronghui_captcha_image(self, page: Any) -> tuple[str, str]:
        _payload, image, mime = self._capture_ronghui_captcha_payload(page)
        return image, mime

    def _capture_ronghui_captcha_payload(self, page: Any) -> tuple[bytes, str, str]:
        for selector in (CAPTCHA_IMAGE, 'img[alt="验证码"]', 'img[src*="validateCode"]'):
            try:
                image = page.locator(selector).first
                image.wait_for(state="visible", timeout=3_000)
                payload = image.screenshot(timeout=5_000)
                if payload:
                    return (
                        payload,
                        "data:image/png;base64," + base64.b64encode(payload).decode("ascii"),
                        "image/png",
                    )
            except Exception:
                continue
        return b"", "", ""

    @staticmethod
    def _ronghui_page_user_context_ready(page: Any) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    () => {
                      try {
                        const info = window.$Z && $Z.user && typeof $Z.user.getUserInfo === "function"
                          ? $Z.user.getUserInfo()
                          : null;
                        return Boolean(
                          info
                          && ["loginUserName", "loginUserAccount", "loginSiteName", "loginSiteCode"]
                            .every((field) => String(info[field] == null ? "" : info[field]).trim())
                        );
                      } catch (_) {
                        return false;
                      }
                    }
                    """
                )
            )
        except Exception:
            return False

    def _wait_ronghui_page_user_context(self, page: Any, *, timeout_ms: int = 15_000) -> bool:
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000
        while True:
            if self._ronghui_page_user_context_ready(page):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def _submit_ronghui_browser_login_attempt(
        self,
        page: Any,
        config: LoginConfig,
        *,
        captcha_code: str,
    ) -> tuple[bool, str, bool]:
        page.locator(USERNAME_INPUT).wait_for(state="visible", timeout=15_000)
        page.locator(USERNAME_INPUT).fill(config.username)
        page.locator(PASSWORD_INPUT).fill(config.password)
        page.locator(CODE_INPUT).fill(captcha_code)
        page.locator(LOGIN_BUTTON).click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            page.wait_for_timeout(800)

        login_error = self._read_login_error(page)
        current_url = str(getattr(page, "url", "") or "").strip()
        if "/system/login" in current_url and login_error:
            return False, login_error, False
        if self._wait_ronghui_page_user_context(page):
            return True, "", False

        current_url = str(getattr(page, "url", "") or "").strip()
        if "/system/login" in current_url:
            return False, login_error or "验证码不正确或登录未完成。", False
        return False, "登录完成但页面用户上下文不完整，请重新登录。", True

    def _auto_login_ronghui_image_captcha_browser(
        self,
        context: Any,
        page: Any,
        config: LoginConfig,
    ) -> dict[str, Any]:
        for attempt in range(1, MAX_AUTO_CAPTCHA_ATTEMPTS + 1):
            if attempt > 1:
                page.goto(config.login_url, wait_until="domcontentloaded", timeout=60_000)
            image_bytes, _captcha_image, _captcha_image_mime = self._capture_ronghui_captcha_payload(page)
            if not image_bytes:
                logger.warning(
                    "Ronghui captcha fetch returned no image for profile=%s attempt=%s/%s",
                    self.profile_name,
                    attempt,
                    MAX_AUTO_CAPTCHA_ATTEMPTS,
                )
                continue
            try:
                captcha_code = captcha_ocr.classify_captcha_image(image_bytes, max_length=4)
            except captcha_ocr.CaptchaOCRUnavailableError as exc:
                logger.warning("Ronghui captcha OCR unavailable for profile=%s: %s", self.profile_name, exc)
                break
            except captcha_ocr.CaptchaOCRFailedError as exc:
                logger.info(
                    "Ronghui captcha OCR failed for profile=%s attempt=%s/%s: %s",
                    self.profile_name,
                    attempt,
                    MAX_AUTO_CAPTCHA_ATTEMPTS,
                    exc,
                )
                continue
            if not captcha_code:
                continue

            success, login_error, incomplete_context = self._submit_ronghui_browser_login_attempt(
                page,
                config,
                captcha_code=captcha_code,
            )
            if success:
                logger.info(
                    "Ronghui captcha browser login succeeded for profile=%s attempt=%s/%s",
                    self.profile_name,
                    attempt,
                    MAX_AUTO_CAPTCHA_ATTEMPTS,
                )
                return self._persist_storage_state_locked(context, page)
            if incomplete_context:
                self._invalidate_ronghui_context_locked(login_error)
                raise TMSAuthStateError("AUTH_REQUIRED", login_error)
            logger.info(
                "Ronghui captcha browser login rejected for profile=%s attempt=%s/%s: %s",
                self.profile_name,
                attempt,
                MAX_AUTO_CAPTCHA_ATTEMPTS,
                login_error,
            )

        page.goto(config.login_url, wait_until="domcontentloaded", timeout=60_000)
        result = self._save_ronghui_image_pending_state_locked(
            context,
            page,
            config=config,
            message=self._ronghui_auto_login_fallback_message(),
        )
        result["auto_login_attempts_exhausted"] = True
        return result

    def _requests_session_from_storage_state_payload(
        self,
        storage_state: dict[str, Any],
        config: LoginConfig,
    ) -> requests.Session:
        session = requests.Session()
        session.mount("https://", _RonghuiTLSAdapter())
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": config.login_url,
                "Origin": config.base_origin,
            }
        )
        for cookie in storage_state.get("cookies", []):
            if not isinstance(cookie, dict):
                continue
            _set_requests_cookie_from_storage(session, cookie)
        return session

    def _ronghui_auto_login_fallback_message(self) -> str:
        return f"自动识别失败 {MAX_AUTO_CAPTCHA_ATTEMPTS} 次，请人工输入或刷新验证码后重试。"

    def _write_ronghui_image_pending_state_locked(
        self,
        storage_state: dict[str, Any],
        *,
        config: LoginConfig,
        captcha_image: str,
        captcha_image_mime: str,
        message: str,
        pending_since: str = "",
    ) -> dict[str, Any]:
        self._state_store.write_dict(self._pending_storage_state_path, storage_state)
        captured_at = _format_ts(_now_ts())
        self._pending_login_state_path.write_text(
            json.dumps(
                {
                    "login_url": config.login_url,
                    "login_action_url": _join_origin_path(config.base_origin, LOGIN_PATH),
                    "challenge_type": "image",
                    "challenge_label": "图片验证码",
                    "captcha_image": captcha_image,
                    "captcha_image_mime": captcha_image_mime,
                    "captcha_captured_at": captured_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return self._save_meta(
            {
                "status": "pending_code",
                "last_validation_at": "",
                "last_error_summary": message,
                "authenticated_at": "",
                "pending_since": pending_since or _format_ts(_now_ts()),
                "expires_at": "",
                "challenge_type": "image",
                "challenge_label": "图片验证码",
                "captcha_image": captcha_image,
                "captcha_image_mime": captcha_image_mime,
                "captcha_captured_at": captured_at,
            }
        )

    def _save_ronghui_image_pending_state_locked(
        self,
        context: Any,
        page: Any,
        *,
        config: LoginConfig,
        message: str = "",
        pending_since: str = "",
    ) -> dict[str, Any]:
        captcha_image, captcha_image_mime = self._capture_ronghui_captcha_image(page)
        storage_state = context.storage_state()
        if not isinstance(storage_state, dict):
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉图片验证码会话未能保存，请重新登录。")
        return self._write_ronghui_image_pending_state_locked(
            storage_state,
            config=config,
            captcha_image=captcha_image,
            captcha_image_mime=captcha_image_mime,
            message=message or "融辉登录页需要图片验证码，请输入图片验证码。",
            pending_since=pending_since,
        )

    def _pending_challenge_type_locked(self) -> str:
        challenge_type = str(self._load_meta().get("challenge_type") or "").strip().lower()
        if self._pending_login_state_path.exists():
            try:
                payload = json.loads(self._pending_login_state_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    challenge_type = str(payload.get("challenge_type") or challenge_type).strip().lower()
            except Exception:
                pass
        return challenge_type or "sms"

    def _submit_ronghui_captcha_login(
        self,
        config: LoginConfig,
        *,
        captcha_code: str,
        pending_since: str,
    ) -> dict[str, Any]:
        if not self._pending_storage_state_path.exists() or not self._pending_login_state_path.exists():
            raise TMSAuthStateError("AUTH_REQUIRED", "当前没有待提交的融辉图片验证码会话，请先点击登录。")

        pending_state = self._state_store.read_dict(self._pending_storage_state_path)
        pending_login = self._state_store.read_dict(self._pending_login_state_path)
        if pending_state is None or pending_login is None:
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉图片验证码会话已损坏，请重新登录。")
        captcha_data_url = str(pending_login.get("captcha_image") or "").strip()
        try:
            captcha_payload = base64.b64decode(captcha_data_url.split(",", 1)[1], validate=True)
        except (IndexError, ValueError, TypeError) as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", "融辉图片验证码会话已损坏，请重新登录。") from exc
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise self._dependency_error("Playwright Python 依赖未安装，无法提交融辉图片验证码。") from exc
        except Exception as exc:
            raise self._dependency_error(f"Playwright 依赖加载失败: {exc}") from exc

        playwright = sync_playwright().start()
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(**_chromium_launch_kwargs())
            context = browser.new_context(
                viewport={"width": 1440, "height": 960},
                storage_state=pending_state,
            )
            page = context.new_page()
            page.route(
                "**/validateCode/code*",
                lambda route: route.fulfill(
                    status=200,
                    content_type=str(pending_login.get("captcha_image_mime") or "image/png"),
                    body=captcha_payload,
                ),
            )
            page.goto(config.login_url, wait_until="domcontentloaded", timeout=60_000)
            page.unroute("**/validateCode/code*")
            success, login_error, incomplete_context = self._submit_ronghui_browser_login_attempt(
                page,
                config,
                captcha_code=captcha_code,
            )
            if success:
                return self._persist_storage_state_locked(context, page)
            if incomplete_context:
                self._invalidate_ronghui_context_locked(login_error)
                raise TMSAuthStateError("AUTH_REQUIRED", login_error)
            page.goto(config.login_url, wait_until="domcontentloaded", timeout=60_000)
            self._save_ronghui_image_pending_state_locked(
                context,
                page,
                config=config,
                message=login_error,
                pending_since=pending_since,
            )
            raise TMSAuthStateError("AUTH_PENDING_CODE", login_error)
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass

    def _yunda_report_headers(self, referer: str = "") -> dict[str, str]:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": referer or yunda_report.page_url(),
            "X-Requested-With": "XMLHttpRequest",
        }

    def _yunda_report_auth_error(self, response: Any, body: str) -> str:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in {301, 302, 401, 403}:
            return "韵达报表子系统登录态已失效，请重新登录韵达账号。"
        headers = getattr(response, "headers", {}) or {}
        location = str(headers.get("Location") or "")
        current_url = str(getattr(response, "url", "") or "")
        if "ky-sso.yunda56.com/login" in current_url or "ky-sso.yunda56.com/login" in location:
            return "韵达报表子系统登录态已失效，请重新登录韵达账号。"
        content_type = str(headers.get("content-type") or "").lower()
        if "text/html" in content_type and (
            "ky-sso.yunda56.com/login" in body
            or "login_form" in body
            or "用户登录" in body
            or "验证码" in body
        ):
            return "韵达报表子系统登录态已失效，请重新登录韵达账号。"
        return ""

    def _yunda_inms_auth_error(self, response: Any, body: str) -> str:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in {301, 302, 401, 403}:
            return "韵达快件跟踪子系统登录态已失效，请重新登录韵达账号。"
        headers = getattr(response, "headers", {}) or {}
        location = str(headers.get("Location") or "")
        current_url = str(getattr(response, "url", "") or "")
        if "ky-sso.yunda56.com/login" in current_url or "ky-sso.yunda56.com/login" in location:
            return "韵达快件跟踪子系统登录态已失效，请重新登录韵达账号。"
        content_type = str(headers.get("content-type") or "").lower()
        if "text/html" in content_type and (
            "ky-sso.yunda56.com/login" in body
            or "login_form" in body
        ):
            return "韵达快件跟踪子系统登录态已失效，请重新登录韵达账号。"
        return ""

    def _yunda_message_auth_error(self, response: Any, body: str) -> str:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in {301, 302, 303, 307, 308, 401, 403}:
            return "韵达消息中心登录态已失效，请重新登录韵达账号。"
        headers = getattr(response, "headers", {}) or {}
        location = str(headers.get("Location") or "")
        current_url = str(getattr(response, "url", "") or "")
        if "ky-sso.yunda56.com/login" in current_url or "ky-sso.yunda56.com/login" in location:
            return "韵达消息中心登录态已失效，请重新登录韵达账号。"
        lowered = str(body or "").lower()
        if any(
            marker in lowered
            for marker in (
                "auth_required",
                "session error",
                '"code":1001',
                "ky-sso.yunda56.com/login",
                "login_form",
                "/login",
                "validatecode",
                "<title>登录",
                "用户登录",
                "验证码",
            )
        ):
            return "韵达消息中心登录态已失效，请重新登录韵达账号。"
        return ""

    def _yunda_problem_auth_error(self, response: Any, body: str) -> str:
        status_code = int(getattr(response, "status_code", 0) or 0)
        message = "\u97f5\u8fbe\u95ee\u9898\u4ef6\u5b50\u7cfb\u7edf\u767b\u5f55\u6001\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u97f5\u8fbe\u8d26\u53f7\u3002"
        if status_code in {301, 302, 303, 307, 308, 401, 403}:
            return message
        headers = getattr(response, "headers", {}) or {}
        location = str(headers.get("Location") or headers.get("location") or "").lower()
        current_url = str(getattr(response, "url", "") or "").lower()
        if any(
            marker in current_url or marker in location
            for marker in (
                "ky-sso.yunda56.com/login",
                "ky-client.yunda56.com",
                "/login",
            )
        ):
            return message
        lowered = str(body or "").lower()
        if not lowered.strip():
            return "\u97f5\u8fbe\u95ee\u9898\u4ef6\u5b50\u7cfb\u7edf\u8fd4\u56de\u7a7a\u54cd\u5e94\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u97f5\u8fbe\u8d26\u53f7\u3002"
        if any(
            marker in lowered
            for marker in (
                "auth_required",
                "session error",
                "ky-sso.yunda56.com/login",
                "login_form",
                "type=\"password\"",
                "type='password'",
                "\u97f5\u8fbe\u5feb\u8fd0\u5ba2\u6237\u7aef",
                "\u7528\u6237\u767b\u5f55",
                "\u9a8c\u8bc1\u7801",
            )
        ):
            return message
        return ""

    def _extract_yunda_message_user_id(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        candidates: list[dict[str, Any]] = []
        for key in ("details", "data", "user", "result"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        candidates.append(payload)
        for item in candidates:
            for key in ("username", "userName", "user_id", "userId", "uid", "id", "account"):
                value = str(item.get(key) or "").strip()
                if value:
                    return value
        return ""

    def _ensure_yunda_report_session_in_browser_locked(self, context: Any, page: Any) -> dict[str, Any] | None:
        config = self.resolve_login_config()
        try:
            page.goto(yunda_report.page_url(), wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                page.wait_for_timeout(1_000)

            if self._is_yunda_sms_page(page):
                return self._save_yunda_sms_pending_state_locked(
                    context,
                    page,
                    config=config,
                    message=YUNDA_SMS_PENDING_MESSAGE,
                )

            if self._is_yunda_login_page(page):
                report_login_url = str(getattr(page, "url", "") or config.login_url).strip() or config.login_url
                try:
                    self._ensure_yunda_account_form_visible(page)
                except Exception as exc:
                    raise TMSAuthStateError("AUTH_REQUIRED", f"韵达报表子系统登录页加载失败: {exc}") from exc

                page.locator(YUNDA_USERNAME_INPUT).fill(config.username)
                page.locator(YUNDA_PASSWORD_INPUT).fill(config.password)
                if self._is_yunda_captcha_visible(page):
                    return self._save_yunda_pending_state_locked(
                        context,
                        page,
                        config=config,
                        login_url=report_login_url,
                        message="韵达报表子系统登录需要图片验证码，请输入验证码后提交。",
                    )

                page.locator(YUNDA_LOGIN_BUTTON).click()
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    page.wait_for_timeout(1_500)

                login_error = self._read_yunda_login_error(page)
                if self._is_yunda_sms_page(page):
                    sms_error = self._read_yunda_sms_error(page)
                    return self._save_yunda_sms_pending_state_locked(
                        context,
                        page,
                        config=config,
                        message=sms_error or login_error or YUNDA_SMS_PENDING_MESSAGE,
                    )
                if self._is_yunda_login_page(page):
                    if self._is_yunda_captcha_visible(page):
                        return self._save_yunda_pending_state_locked(
                            context,
                            page,
                            config=config,
                            login_url=str(getattr(page, "url", "") or report_login_url).strip() or report_login_url,
                            message=login_error or "韵达报表子系统登录需要图片验证码，请输入验证码后提交。",
                        )
                    raise TMSAuthStateError("AUTH_REQUIRED", login_error or "韵达报表子系统登录未完成，请重新登录韵达账号。")

                page.goto(yunda_report.page_url(), wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    page.wait_for_timeout(1_000)
                if self._is_yunda_sms_page(page):
                    sms_error = self._read_yunda_sms_error(page)
                    return self._save_yunda_sms_pending_state_locked(
                        context,
                        page,
                        config=config,
                        message=sms_error or YUNDA_SMS_PENDING_MESSAGE,
                    )
                if self._is_yunda_login_page(page):
                    if self._is_yunda_captcha_visible(page):
                        return self._save_yunda_pending_state_locked(
                            context,
                            page,
                            config=config,
                            login_url=str(getattr(page, "url", "") or report_login_url).strip() or report_login_url,
                            message="韵达报表子系统登录需要图片验证码，请输入验证码后提交。",
                        )
                    raise TMSAuthStateError("AUTH_REQUIRED", "韵达报表子系统登录未完成，请重新登录韵达账号。")

            context.storage_state(path=str(self._storage_state_path))
            return None
        except TMSAuthStateError:
            raise
        except Exception as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", f"韵达报表子系统初始化失败: {exc}") from exc

    def _ensure_yunda_inms_session_in_browser_locked(self, context: Any, page: Any) -> None:
        try:
            page.goto(YUNDA_TRACKING_CLIENT_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                page.wait_for_timeout(2_000)

            if self._is_yunda_sms_page(page):
                self._save_yunda_sms_pending_state_locked(
                    context,
                    page,
                    config=self.resolve_login_config(),
                    message=YUNDA_SMS_PENDING_MESSAGE,
                )
                raise TMSAuthStateError("AUTH_PENDING_CODE", YUNDA_SMS_PENDING_MESSAGE)
            if self._is_yunda_login_page(page):
                raise TMSAuthStateError("AUTH_REQUIRED", "韵达快件跟踪子系统登录未完成，请重新登录韵达账号。")

            try:
                page.wait_for_selector('iframe[src*="kyinms.yunda56.com"]', timeout=15_000)
            except Exception:
                page.wait_for_timeout(2_000)
            frame_urls = [
                str(getattr(frame, "url", "") or "")
                for frame in getattr(page, "frames", [])
            ]
            if not any("kyinms.yunda56.com" in frame_url for frame_url in frame_urls):
                raise TMSAuthStateError("AUTH_REQUIRED", "韵达快件跟踪子系统未加载，请重新登录韵达账号。")
        except TMSAuthStateError:
            raise
        except Exception as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", f"韵达快件跟踪子系统初始化失败: {exc}") from exc

    def _click_yunda_client_menu_in_browser_locked(self, page: Any, *, menu_text: str, route_url: str) -> dict[str, Any]:
        script = f"""
        () => {{
          const menuText = {json.dumps(menu_text, ensure_ascii=False)};
          const routeUrl = {json.dumps(route_url, ensure_ascii=False)};
          const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
          const nodes = Array.from(document.querySelectorAll("a, li, [role='menuitem'], [role='button'], button"));
          const exact = nodes.find((node) => clean(node.innerText || node.textContent) === menuText);
          const hrefMatch = nodes.find((node) => String(node.href || node.getAttribute("href") || "").includes("/4768/"));
          const loose = nodes.find((node) => clean(node.innerText || node.textContent).includes(menuText));
          const target = hrefMatch || exact || loose;
          if (target) {{
            target.click();
            return {{
              clicked: true,
              text: clean(target.innerText || target.textContent).slice(0, 80),
              href: String(target.href || target.getAttribute("href") || "").replace(/[?&][^=#]+=[^&#]*/g, (m) => m.split("=")[0] + "=<redacted>")
            }};
          }}
          window.location.href = routeUrl;
          return {{
            clicked: false,
            fallbackRoute: true,
            visibleText: clean(document.body && document.body.innerText).slice(0, 240)
          }};
        }}
        """
        try:
            result = page.evaluate(script)
        except Exception as exc:
            try:
                page.goto(route_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
            return {"clicked": False, "fallbackRoute": True, "error": str(exc)}
        return result if isinstance(result, dict) else {"clicked": False, "fallbackRoute": True}

    def _yunda_frame_urls(self, page: Any) -> list[str]:
        return [
            str(getattr(frame, "url", "") or "")
            for frame in getattr(page, "frames", [])
        ]

    def _ensure_yunda_problem_session_in_browser_locked(self, context: Any, page: Any) -> None:
        try:
            page.goto(YUNDA_CLIENT_SYSTEM_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                page.wait_for_timeout(2_000)

            if self._is_yunda_sms_page(page):
                self._save_yunda_sms_pending_state_locked(
                    context,
                    page,
                    config=self.resolve_login_config(),
                    message=YUNDA_SMS_PENDING_MESSAGE,
                )
                raise TMSAuthStateError("AUTH_PENDING_CODE", YUNDA_SMS_PENDING_MESSAGE)
            if self._is_yunda_login_page(page):
                raise TMSAuthStateError("AUTH_REQUIRED", "\u97f5\u8fbe\u95ee\u9898\u4ef6\u5b50\u7cfb\u7edf\u767b\u5f55\u672a\u5b8c\u6210\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u97f5\u8fbe\u8d26\u53f7\u3002")

            menu_result = self._click_yunda_client_menu_in_browser_locked(
                page,
                menu_text="问题件查询",
                route_url=YUNDA_PROBLEM_CLIENT_QUERY_URL,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                page.wait_for_timeout(2_000)

            try:
                page.wait_for_selector(YUNDA_PROBLEM_IFRAME_SELECTOR, timeout=30_000)
            except Exception:
                page.wait_for_timeout(3_000)
            frame_urls = self._yunda_frame_urls(page)
            for _ in range(6):
                if any("kyproblem.yunda56.com" in frame_url for frame_url in frame_urls):
                    return
                page.wait_for_timeout(3_000)
                frame_urls = self._yunda_frame_urls(page)
            if not any("kyproblem.yunda56.com" in frame_url for frame_url in frame_urls):
                current_url = str(getattr(page, "url", "") or "").lower()
                if "ky-sso.yunda56.com" in current_url:
                    raise TMSAuthStateError("AUTH_REQUIRED", "\u97f5\u8fbe\u95ee\u9898\u4ef6\u5b50\u7cfb\u7edf\u767b\u5f55\u672a\u5b8c\u6210\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u97f5\u8fbe\u8d26\u53f7\u3002")
                frame_summary = ", ".join(
                    sorted({urlparse(frame_url).netloc for frame_url in frame_urls if frame_url})[:5]
                )
                clicked_text = str(menu_result.get("text") or "").strip() if isinstance(menu_result, dict) else ""
                hint = f"\uff0c\u5df2\u70b9\u51fb\u83dc\u5355\uff1a{clicked_text}" if clicked_text else ""
                if frame_summary:
                    hint += f"\uff0c\u5f53\u524diframe\uff1a{frame_summary}"
                raise TMSAuthStateError("AUTH_REQUIRED", f"\u97f5\u8fbe\u95ee\u9898\u4ef6\u5b50\u7cfb\u7edf\u672a\u52a0\u8f7d{hint}\u3002\u8bf7\u901a\u8fc7\u97f5\u8fbe\u5ba2\u6237\u7aef\u95ee\u9898\u4ef6\u83dc\u5355\u91cd\u65b0\u521d\u59cb\u5316\u3002")
        except TMSAuthStateError:
            raise
        except Exception as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", f"\u97f5\u8fbe\u95ee\u9898\u4ef6\u5b50\u7cfb\u7edf\u521d\u59cb\u5316\u5931\u8d25: {exc}") from exc

    def _ensure_yunda_report_session_in_requests_locked(self, session: requests.Session, config: LoginConfig) -> None:
        page_response = session.get(
            yunda_report.page_url(),
            headers={"Referer": config.home_url},
            allow_redirects=True,
            timeout=30,
        )
        page_body = getattr(page_response, "text", "") or ""
        page_error = self._yunda_report_auth_error(page_response, page_body)
        if page_error:
            raise TMSAuthStateError("AUTH_REQUIRED", page_error)

        target_date = dt.datetime.now().date()
        for attempt in range(1, YUNDA_REPORT_VALIDATION_ATTEMPTS + 1):
            search_response = session.get(
                yunda_report.search_url(),
                params=yunda_report.build_search_params({}, target_date=target_date, limit=1, offset=0),
                headers=self._yunda_report_headers(yunda_report.page_url()),
                allow_redirects=False,
                timeout=30,
            )
            body = getattr(search_response, "text", "") or ""
            search_error = self._yunda_report_auth_error(search_response, body)
            if search_error:
                raise TMSAuthStateError("AUTH_REQUIRED", search_error)
            try:
                payload = search_response.json()
            except Exception as exc:
                if not body.strip():
                    message = "韵达报表接口返回空响应，请重新登录韵达账号。"
                else:
                    message = "韵达报表接口返回非 JSON，请重新登录韵达账号。"
                if attempt < YUNDA_REPORT_VALIDATION_ATTEMPTS:
                    logger.info(
                        "Yunda report validation returned invalid JSON; retrying profile=%s attempt=%s/%s status=%s content_type=%s",
                        self.profile_name,
                        attempt,
                        YUNDA_REPORT_VALIDATION_ATTEMPTS,
                        getattr(search_response, "status_code", "-"),
                        (getattr(search_response, "headers", {}) or {}).get("content-type", ""),
                    )
                    time.sleep(YUNDA_REPORT_VALIDATION_RETRY_DELAY_SEC)
                    continue
                raise TMSAuthStateError("AUTH_REQUIRED", message) from exc
            if isinstance(payload, (dict, list)):
                return
            if attempt < YUNDA_REPORT_VALIDATION_ATTEMPTS:
                logger.info(
                    "Yunda report validation returned unexpected payload; retrying profile=%s attempt=%s/%s payload_type=%s",
                    self.profile_name,
                    attempt,
                    YUNDA_REPORT_VALIDATION_ATTEMPTS,
                    type(payload).__name__,
                )
                time.sleep(YUNDA_REPORT_VALIDATION_RETRY_DELAY_SEC)
                continue
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达报表接口返回格式异常，请重新登录韵达账号。")

    def _ensure_yunda_inms_session_in_requests_locked(self, session: requests.Session, config: LoginConfig) -> None:
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

    def _ensure_yunda_message_session_in_requests_locked(self, session: requests.Session, config: LoginConfig) -> None:
        user_response = session.get(
            YUNDA_USER_INFO_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": YUNDA_CLIENT_HOME_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
            timeout=15,
        )
        user_body = getattr(user_response, "text", "") or ""
        user_error = self._yunda_message_auth_error(user_response, user_body)
        if user_error:
            raise TMSAuthStateError("AUTH_REQUIRED", user_error)
        try:
            user_payload = user_response.json()
        except Exception as exc:
            if not user_body.strip():
                raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息中心用户信息返回空响应，请重新登录韵达账号。") from exc
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息中心用户信息返回非 JSON，请重新登录韵达账号。") from exc
        user_id = self._extract_yunda_message_user_id(user_payload)
        if not user_id:
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息中心未获取到用户标识，请重新登录韵达账号。")

        message_response = session.post(
            YUNDA_MESSAGE_TYPES_URL,
            data={"userId": user_id, "sourceFlag": "up"},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": YUNDA_MESSAGE_ORIGIN,
                "Referer": YUNDA_HEAD_MESSAGE_REFERER,
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
            timeout=15,
        )
        message_body = getattr(message_response, "text", "") or ""
        message_error = self._yunda_message_auth_error(message_response, message_body)
        if message_error:
            raise TMSAuthStateError("AUTH_REQUIRED", message_error)
        if getattr(message_response, "status_code", 0) != 200:
            raise TMSAuthStateError(
                "AUTH_REQUIRED",
                f"韵达消息中心接口返回 HTTP {getattr(message_response, 'status_code', '')}，请重新登录韵达账号。",
            )
        try:
            payload = message_response.json()
        except Exception as exc:
            if not message_body.strip():
                raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息中心接口返回空响应，请重新登录韵达账号。") from exc
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息中心接口返回非 JSON，请重新登录韵达账号。") from exc
        if not isinstance(payload, (dict, list)):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达消息中心接口返回格式异常，请重新登录韵达账号。")
        if isinstance(payload, dict):
            error_code = str(payload.get("errorCode") or payload.get("code") or "").strip()
            message = str(payload.get("msg") or payload.get("message") or "").strip()
            if "数据库操作异常" in message:
                logger.warning(
                    "Yunda message center returned database error during auth validation; ignoring as non-auth failure: code=%s",
                    error_code,
                )
                return
            if error_code in {"80001", "1001", "401", "403"} or "用户ID为空" in message:
                raise TMSAuthStateError("AUTH_REQUIRED", f"韵达消息中心接口拒绝访问：{message or error_code}。请重新登录韵达账号。")

    def _ensure_yunda_problem_session_in_requests_locked(self, session: requests.Session, config: LoginConfig) -> None:
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
        if getattr(response, "status_code", 0) != 200:
            raise TMSAuthStateError(
                "AUTH_REQUIRED",
                f"\u97f5\u8fbe\u95ee\u9898\u4ef6\u5b50\u7cfb\u7edf\u8fd4\u56de HTTP {getattr(response, 'status_code', '')}\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u97f5\u8fbe\u8d26\u53f7\u3002",
            )

    def _prepare_yunda_login_page(self, page: Any, config: LoginConfig) -> None:
        page.goto(config.login_url, wait_until="domcontentloaded", timeout=60_000)
        self._ensure_yunda_account_form_visible(page)

    def _ensure_yunda_account_form_visible(self, page: Any) -> None:
        username = page.locator(YUNDA_USERNAME_INPUT)
        try:
            username_visible = username.is_visible(timeout=1_000)
        except Exception:
            username_visible = False
        if not username_visible:
            for selector in (".account-switch-icon span", ".account-switch-icon img"):
                try:
                    page.locator(selector).first.click(timeout=5_000)
                    page.wait_for_selector(YUNDA_USERNAME_INPUT, state="visible", timeout=3_000)
                    username_visible = True
                    break
                except Exception:
                    pass
        if not username_visible:
            try:
                page.evaluate(
                    """
                    () => {
                      const account = document.querySelector('.account-show');
                      const third = document.querySelector('.third-login-show');
                      if (account) account.style.display = 'block';
                      if (third) third.style.display = 'none';
                    }
                    """
                )
            except Exception:
                pass
        page.wait_for_selector(YUNDA_USERNAME_INPUT, state="visible", timeout=20_000)

    def _is_yunda_captcha_visible(self, page: Any) -> bool:
        try:
            return bool(page.locator(YUNDA_CAPTCHA_INPUT).is_visible(timeout=500))
        except Exception:
            try:
                return bool(
                    page.evaluate(
                        """
                        () => {
                          const box = document.querySelector('#login_captcha');
                          if (!box) return false;
                          const style = window.getComputedStyle(box);
                          return style.display !== 'none' && style.visibility !== 'hidden';
                        }
                        """
                    )
                )
            except Exception:
                return False

    def _read_yunda_login_error(self, page: Any) -> str:
        try:
            return str(
                page.evaluate(
                    """
                    () => {
                      const selectors = [
                        '#inputUsernameStatus',
                        '#inputPasswordStatus',
                        '#inputCaptchaStatus',
                        '.error-msg-show',
                        '.alert',
                        '.login-error'
                      ];
                      const messages = [];
                      for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                          const text = (el.innerText || el.textContent || '').trim();
                          if (!text) continue;
                          const style = window.getComputedStyle(el);
                          const visible = style.display !== 'none' && style.visibility !== 'hidden';
                          if (visible && !messages.includes(text)) messages.push(text);
                        }
                      }
                      return messages.join('；');
                    }
                    """
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _read_yunda_csrf(self, page: Any) -> str:
        try:
            return str(page.locator('input[name="_csrf"]').input_value(timeout=1_000) or "").strip()
        except Exception:
            return ""

    def _capture_yunda_captcha_image_payload(self, page: Any) -> tuple[bytes, str, str]:
        try:
            image = page.locator("#login_captcha img.captche").first
            image.wait_for(state="visible", timeout=3_000)
            payload = image.screenshot(timeout=5_000)
            if payload:
                return payload, "data:image/png;base64," + base64.b64encode(payload).decode("ascii"), "image/png"
        except Exception:
            pass
        return b"", "", ""

    def _capture_yunda_captcha_image(self, page: Any) -> tuple[str, str]:
        _payload, captcha_image, captcha_image_mime = self._capture_yunda_captcha_image_payload(page)
        if captcha_image:
            return captcha_image, captcha_image_mime
        return "", ""

    def _extract_yunda_csrf_from_html(self, body: str) -> str:
        match = re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']', body or "", flags=re.I)
        return match.group(1).strip() if match else ""

    def _read_yunda_html_error(self, body: str) -> str:
        messages: list[str] = []
        for element_id in ("inputUsernameStatus", "inputPasswordStatus", "inputCaptchaStatus"):
            pattern = rf'id=["\']{element_id}["\'][^>]*>(.*?)</span>'
            match = re.search(pattern, body or "", flags=re.I | re.S)
            if not match:
                continue
            text = re.sub(r"<[^>]+>", "", match.group(1))
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in messages:
                messages.append(text)
        return "；".join(messages)

    def _session_from_storage_state_payload(self, storage_state: dict[str, Any]) -> requests.Session:
        session = requests.Session()
        session.mount("https://", _RonghuiTLSAdapter())
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": self.resolve_login_config().login_url,
                "Origin": "https://ky-sso.yunda56.com",
            }
        )
        for cookie in storage_state.get("cookies", []):
            if not isinstance(cookie, dict):
                continue
            _set_requests_cookie_from_storage(session, cookie)
        return session

    def _requests_cookie_to_storage_cookie(self, cookie: Any, fallback_domain: str) -> dict[str, Any]:
        domain = str(getattr(cookie, "domain", "") or fallback_domain or "").strip()
        rest = getattr(cookie, "_rest", {}) or {}
        same_site_raw = str(rest.get("SameSite") or rest.get("samesite") or "").strip().lower()
        same_site = "None" if same_site_raw == "none" else "Strict" if same_site_raw == "strict" else "Lax"
        http_only_value = rest.get("HttpOnly", rest.get("httponly"))
        http_only_present = "HttpOnly" in rest or "httponly" in rest
        http_only = http_only_present and (http_only_value is None or bool(http_only_value))
        cookie_name = str(getattr(cookie, "name", "") or "")
        normalized_domain = domain.lower().lstrip(".")
        if cookie_name == RONGHUI_USER_INFO_COOKIE and (
            normalized_domain == "ronghuiwl.com" or normalized_domain.endswith(".ronghuiwl.com")
        ):
            http_only = False
        return {
            "name": cookie_name,
            "value": str(getattr(cookie, "value", "") or ""),
            "domain": domain,
            "path": str(getattr(cookie, "path", "") or "/"),
            "expires": float(getattr(cookie, "expires", None) or -1),
            "httpOnly": http_only,
            "secure": bool(getattr(cookie, "secure", False)),
            "sameSite": same_site,
        }

    def _storage_cookies_from_requests_session(self, session: requests.Session, fallback_domain: str) -> list[dict[str, Any]]:
        return [
            self._requests_cookie_to_storage_cookie(cookie, fallback_domain)
            for cookie in session.cookies
            if str(getattr(cookie, "name", "") or "").strip()
        ]

    def _write_storage_cookies_locked(self, cookies: list[dict[str, Any]]) -> str:
        storage_state = {"cookies": cookies, "origins": []}
        self._storage_state_path.write_text(
            json.dumps(storage_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._cookies_path.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        cookie_expiries = [
            float(cookie.get("expires"))
            for cookie in cookies
            if isinstance(cookie, dict) and cookie.get("expires") not in (None, "", -1)
        ]
        return _format_ts(min(cookie_expiries)) if cookie_expiries else ""

    def _persist_requests_session_locked(self, session: requests.Session, config: LoginConfig) -> dict[str, Any]:
        if self._is_yunda_mode():
            self._ensure_yunda_report_session_in_requests_locked(session, config)
            self._ensure_yunda_inms_session_in_requests_locked(session, config)
            self._ensure_yunda_message_session_in_requests_locked(session, config)
        fallback_domain = urlparse(config.base_origin).hostname or "yunda56.com"
        cookies = self._storage_cookies_from_requests_session(session, fallback_domain)
        expires_at = self._write_storage_cookies_locked(cookies)
        for path in (self._pending_storage_state_path, self._pending_login_state_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                logger.exception("Failed to remove pending Yunda login state file: %s", path)
        return self._save_meta(
            {
                "status": "authenticated",
                "last_validation_at": "",
                "last_error_summary": "",
                "authenticated_at": _format_ts(_now_ts()),
                "pending_since": "",
                "expires_at": expires_at,
            }
        )

    def _write_pending_requests_state_locked(
        self,
        session: requests.Session,
        config: LoginConfig,
        *,
        csrf: str,
        login_url: str = "",
        login_action_url: str = "",
        captcha_image: str,
        captcha_image_mime: str,
        message: str,
        pending_since: str,
    ) -> dict[str, Any]:
        pending_login_url = str(login_url or config.login_url).strip() or config.login_url
        pending_action_url = str(login_action_url or pending_login_url).strip() or pending_login_url
        fallback_domain = urlparse(pending_login_url).hostname or "ky-sso.yunda56.com"
        cookies = [
            self._requests_cookie_to_storage_cookie(cookie, fallback_domain)
            for cookie in session.cookies
            if str(getattr(cookie, "name", "") or "").strip()
        ]
        self._pending_storage_state_path.write_text(
            json.dumps({"cookies": cookies, "origins": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        pending_payload = {
            "csrf": csrf,
            "login_url": pending_login_url,
            "login_action_url": pending_action_url,
            "challenge_type": "image",
            "challenge_label": "图片验证码",
            "captcha_image": captcha_image,
            "captcha_image_mime": captcha_image_mime,
            "captcha_captured_at": _format_ts(_now_ts()),
        }
        self._pending_login_state_path.write_text(
            json.dumps(pending_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self._save_meta(
            {
                "status": "pending_code",
                "last_validation_at": "",
                "last_error_summary": message,
                "authenticated_at": "",
                "pending_since": pending_since or _format_ts(_now_ts()),
                "expires_at": "",
                "challenge_type": "image",
                "challenge_label": "图片验证码",
                "captcha_image": captcha_image,
                "captcha_image_mime": captcha_image_mime,
                "captcha_captured_at": pending_payload["captcha_captured_at"],
            }
        )

    def _save_yunda_pending_state_locked(
        self,
        context: Any,
        page: Any,
        *,
        config: LoginConfig,
        login_url: str = "",
        message: str,
        pending_since: str = "",
    ) -> dict[str, Any]:
        context.storage_state(path=str(self._pending_storage_state_path))
        csrf = self._read_yunda_csrf(page)
        pending_login_url = str(login_url or getattr(page, "url", "") or config.login_url).strip() or config.login_url
        pending_action_url = self._read_yunda_login_action(page, pending_login_url)
        captcha_image, captcha_image_mime = self._capture_yunda_captcha_image(page)
        pending_payload = {
            "csrf": csrf,
            "login_url": pending_login_url,
            "login_action_url": pending_action_url,
            "challenge_type": "image",
            "challenge_label": "图片验证码",
            "captcha_image": captcha_image,
            "captcha_image_mime": captcha_image_mime,
            "captcha_captured_at": _format_ts(_now_ts()),
        }
        self._pending_login_state_path.write_text(
            json.dumps(pending_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self._save_meta(
            {
                "status": "pending_code",
                "last_validation_at": "",
                "last_error_summary": message,
                "authenticated_at": "",
                "pending_since": pending_since or _format_ts(_now_ts()),
                "expires_at": "",
                "challenge_type": "image",
                "challenge_label": "图片验证码",
                "captcha_image": captcha_image,
                "captcha_image_mime": captcha_image_mime,
                "captcha_captured_at": pending_payload["captcha_captured_at"],
            }
        )

    def _save_yunda_sms_pending_state_locked(
        self,
        context: Any,
        page: Any,
        *,
        config: LoginConfig,
        message: str = "",
        pending_since: str = "",
        send_sms: bool = True,
    ) -> dict[str, Any]:
        if send_sms:
            self._send_yunda_sms_code_locked(page)

        context.storage_state(path=str(self._pending_storage_state_path))
        csrf = self._read_yunda_csrf(page)
        pending_login_url = str(getattr(page, "url", "") or config.login_url).strip() or config.login_url
        pending_payload = {
            "csrf": csrf,
            "login_url": pending_login_url,
            "login_action_url": pending_login_url,
            "challenge_type": "sms",
            "challenge_label": "短信验证码",
            "captcha_image": "",
            "captcha_image_mime": "",
            "captcha_captured_at": "",
        }
        self._pending_login_state_path.write_text(
            json.dumps(pending_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self._save_meta(
            {
                "status": "pending_code",
                "last_validation_at": "",
                "last_error_summary": message or YUNDA_SMS_PENDING_MESSAGE,
                "authenticated_at": "",
                "pending_since": pending_since or _format_ts(_now_ts()),
                "expires_at": "",
                "challenge_type": "sms",
                "challenge_label": "短信验证码",
            }
        )

    def _send_yunda_sms_code_locked(self, page: Any) -> None:
        try:
            send_button = page.locator(YUNDA_SMS_SEND_BUTTON).first
            if not send_button.is_visible(timeout=2_000):
                raise TMSAuthStateError(
                    "AUTH_REQUIRED",
                    "韵达短信发送按钮不可见，请重新登录韵达账号。",
                )
            if not send_button.is_enabled(timeout=2_000):
                raise TMSAuthStateError(
                    "AUTH_REQUIRED",
                    "韵达短信发送按钮当前不可用，请稍后重试。",
                )
        except TMSAuthStateError:
            raise
        except Exception as exc:
            logger.warning(
                "Yunda SMS send button inspection failed for profile=%s error_type=%s",
                self.profile_name,
                type(exc).__name__,
            )
            raise TMSAuthStateError(
                "AUTH_REQUIRED",
                "无法确认韵达短信发送按钮状态，请重新登录韵达账号。",
            ) from exc

        try:
            with page.expect_response(
                lambda response: (
                    str(getattr(getattr(response, "request", None), "method", "")).upper() == "POST"
                    and urlparse(str(getattr(response, "url", "") or "")).path
                    == YUNDA_SMS_SEND_PATH
                ),
                timeout=10_000,
            ) as response_info:
                send_button.click(timeout=5_000)
            response = response_info.value
            status = int(getattr(response, "status", 0) or 0)
            payload = response.json()
        except Exception as exc:
            logger.warning(
                "Yunda SMS send confirmation failed for profile=%s error_type=%s",
                self.profile_name,
                type(exc).__name__,
            )
            raise TMSAuthStateError(
                "AUTH_REQUIRED",
                "未收到韵达短信发送接口的成功确认，请稍后重试。",
            ) from exc

        if not 200 <= status < 300 or not isinstance(payload, dict) or payload.get("success") is not True:
            raise TMSAuthStateError(
                "AUTH_REQUIRED",
                "韵达未确认短信发送成功，请稍后重试。",
            )

    def _read_yunda_login_action(self, page: Any, fallback_url: str) -> str:
        fallback = str(fallback_url or "").strip() or self.resolve_login_config().login_url
        try:
            action = str(page.locator(YUNDA_LOGIN_FORM).get_attribute("action", timeout=1_000) or "").strip()
        except Exception:
            action = ""
        return urljoin(fallback, action) if action else fallback

    def _is_yunda_login_page(self, page: Any) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        if "ky-sso.yunda56.com/login" in current_url or "/login" in current_url:
            return True
        try:
            return page.locator(YUNDA_LOGIN_FORM).count() > 0
        except Exception:
            return False

    def _is_yunda_sms_page(self, page: Any) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        if YUNDA_SMS_PATH in current_url:
            return True
        try:
            return page.locator(YUNDA_SMS_CODE_INPUT).count() > 0 and page.locator(YUNDA_SMS_SEND_BUTTON).count() > 0
        except Exception:
            return False

    def _read_yunda_sms_error(self, page: Any) -> str:
        try:
            return str(
                page.evaluate(
                    """
                    () => {
                      const selectors = [
                        '.error-msg-show',
                        '.alert',
                        '.help-block',
                        '.invalid-feedback',
                        '.text-danger',
                        '.has-error',
                        '.layui-layer-content',
                        '.toast',
                        '[role="alert"]'
                      ];
                      const messages = [];
                      for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                          const text = (el.innerText || el.textContent || '').trim();
                          if (!text) continue;
                          const style = window.getComputedStyle(el);
                          if (style.display !== 'none' && style.visibility !== 'hidden') messages.push(text);
                        }
                      }
                      return [...new Set(messages)].join('；');
                    }
                    """
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _wait_for_yunda_sms_submit_result(self, page: Any, *, timeout_ms: int = 12_000) -> tuple[bool, str]:
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        last_error = ""
        while True:
            if not self._is_yunda_sms_page(page):
                return False, ""
            last_error = self._read_yunda_sms_error(page)
            if last_error:
                return True, last_error
            if time.monotonic() >= deadline:
                return True, last_error
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)

    def _auto_login_yunda_image_captcha(
        self,
        context: Any,
        page: Any,
        *,
        config: LoginConfig,
        pending_since: str = "",
    ) -> dict[str, Any]:
        last_error = ""
        for attempt in range(1, MAX_AUTO_CAPTCHA_ATTEMPTS + 1):
            image_bytes, _captcha_image, _captcha_image_mime = self._capture_yunda_captcha_image_payload(page)
            if not image_bytes:
                logger.info(
                    "Yunda captcha capture returned no image for profile=%s attempt=%s/%s",
                    self.profile_name,
                    attempt,
                    MAX_AUTO_CAPTCHA_ATTEMPTS,
                )
                continue
            try:
                captcha_code = captcha_ocr.classify_captcha_image(image_bytes, max_length=4)
            except captcha_ocr.CaptchaOCRUnavailableError as exc:
                logger.warning("Yunda captcha OCR unavailable for profile=%s: %s", self.profile_name, exc)
                break
            except captcha_ocr.CaptchaOCRFailedError as exc:
                logger.info(
                    "Yunda captcha OCR failed for profile=%s attempt=%s/%s: %s",
                    self.profile_name,
                    attempt,
                    MAX_AUTO_CAPTCHA_ATTEMPTS,
                    exc,
                )
                continue
            if not captcha_code:
                logger.info(
                    "Yunda captcha OCR returned empty text for profile=%s attempt=%s/%s",
                    self.profile_name,
                    attempt,
                    MAX_AUTO_CAPTCHA_ATTEMPTS,
                )
                continue

            try:
                page.locator(YUNDA_USERNAME_INPUT).fill(config.username)
                page.locator(YUNDA_PASSWORD_INPUT).fill(config.password)
                page.locator(YUNDA_CAPTCHA_INPUT).fill(captcha_code)
                page.locator(YUNDA_LOGIN_BUTTON).click()
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    page.wait_for_timeout(1_500)
            except Exception as exc:
                logger.warning(
                    "Yunda captcha submit failed for profile=%s attempt=%s/%s: %s",
                    self.profile_name,
                    attempt,
                    MAX_AUTO_CAPTCHA_ATTEMPTS,
                    exc,
                )
                continue

            login_error = self._read_yunda_login_error(page)
            if self._is_yunda_sms_page(page):
                sms_error = self._read_yunda_sms_error(page)
                return self._save_yunda_sms_pending_state_locked(
                    context,
                    page,
                    config=config,
                    message=sms_error or login_error or YUNDA_SMS_PENDING_MESSAGE,
                    pending_since=pending_since,
                )

            captcha_visible = self._is_yunda_captcha_visible(page)
            if self._is_yunda_login_page(page):
                if captcha_visible and ("验证码" in login_error or not login_error):
                    last_error = login_error or "韵达图片验证码不正确或登录未完成。"
                    logger.info(
                        "Yunda captcha auto login rejected for profile=%s attempt=%s/%s: %s",
                        self.profile_name,
                        attempt,
                        MAX_AUTO_CAPTCHA_ATTEMPTS,
                        last_error,
                    )
                    continue
                self._save_meta(
                    {
                        "status": "logged_out",
                        "last_validation_at": "",
                        "last_error_summary": login_error or "韵达账号密码登录未完成。",
                        "authenticated_at": "",
                        "pending_since": "",
                        "expires_at": "",
                    }
                )
                raise TMSAuthStateError("AUTH_REQUIRED", login_error or "韵达账号密码登录未完成。")

            logger.info(
                "Yunda captcha auto login succeeded for profile=%s attempt=%s/%s",
                self.profile_name,
                attempt,
                MAX_AUTO_CAPTCHA_ATTEMPTS,
            )
            self._pending_storage_state_path.unlink(missing_ok=True)
            meta = self._persist_storage_state_locked(context, page)
            self._close_pending_locked()
            return meta

        fallback_message = (
            f"自动识别失败 {MAX_AUTO_CAPTCHA_ATTEMPTS} 次：{last_error}"
            if last_error
            else self._ronghui_auto_login_fallback_message()
        )
        result = self._save_yunda_pending_state_locked(
            context,
            page,
            config=config,
            message=fallback_message,
            pending_since=pending_since,
        )
        result["auto_login_attempts_exhausted"] = True
        return result

    def _run_yunda_password_login(
        self,
        config: LoginConfig,
        *,
        captcha_code: str = "",
        storage_state: str | None = None,
        pending_since: str = "",
    ) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise self._dependency_error("Playwright Python 依赖未安装，无法登录韵达。") from exc
        except Exception as exc:
            raise self._dependency_error(f"Playwright 依赖加载失败: {exc}") from exc

        playwright = sync_playwright().start()
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(**_chromium_launch_kwargs())
            context_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 960}}
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            try:
                self._prepare_yunda_login_page(page, config)
            except Exception as exc:
                self._save_meta(
                    {
                        "status": "logged_out",
                        "last_validation_at": "",
                        "last_error_summary": f"韵达登录页加载失败: {exc}",
                        "authenticated_at": "",
                        "pending_since": "",
                        "expires_at": "",
                    }
                )
                raise TMSAuthStateError("AUTH_REQUIRED", f"韵达登录页加载失败: {exc}") from exc
            page.locator(YUNDA_USERNAME_INPUT).fill(config.username)
            page.locator(YUNDA_PASSWORD_INPUT).fill(config.password)
            if captcha_code:
                page.locator(YUNDA_CAPTCHA_INPUT).fill(captcha_code)
            elif self._is_yunda_captcha_visible(page):
                return self._auto_login_yunda_image_captcha(
                    context,
                    page,
                    config=config,
                    pending_since=pending_since,
                )

            page.locator(YUNDA_LOGIN_BUTTON).click()
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                page.wait_for_timeout(1_500)

            login_error = self._read_yunda_login_error(page)
            if self._is_yunda_sms_page(page):
                sms_error = self._read_yunda_sms_error(page)
                return self._save_yunda_sms_pending_state_locked(
                    context,
                    page,
                    config=config,
                    message=sms_error or login_error or YUNDA_SMS_PENDING_MESSAGE,
                    pending_since=pending_since,
                )
            captcha_visible = self._is_yunda_captcha_visible(page)
            if self._is_yunda_login_page(page):
                if captcha_visible and ("验证码" in login_error or not login_error):
                    return self._save_yunda_pending_state_locked(
                        context,
                        page,
                        config=config,
                        message=login_error or "韵达登录需要图片验证码，请输入验证码后提交。",
                        pending_since=pending_since,
                    )
                self._save_meta(
                    {
                        "status": "logged_out",
                        "last_validation_at": "",
                        "last_error_summary": login_error or "韵达账号密码登录未完成。",
                        "authenticated_at": "",
                        "pending_since": "",
                        "expires_at": "",
                    }
                )
                raise TMSAuthStateError("AUTH_REQUIRED", login_error or "韵达账号密码登录未完成。")

            self._pending_storage_state_path.unlink(missing_ok=True)
            meta = self._persist_storage_state_locked(context, page)
            self._close_pending_locked()
            return meta
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass

    def _submit_yunda_captcha_login(
        self,
        config: LoginConfig,
        *,
        captcha_code: str,
        pending_since: str,
    ) -> dict[str, Any]:
        if not self._pending_storage_state_path.exists() or not self._pending_login_state_path.exists():
            raise TMSAuthStateError("AUTH_REQUIRED", "当前没有待提交的韵达图片验证码会话，请先点击登录。")
        try:
            pending_payload = json.loads(self._pending_login_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", f"韵达图片验证码会话已损坏，请重新登录: {exc}") from exc
        try:
            storage_state = json.loads(self._pending_storage_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", f"韵达图片验证码 cookie 会话已损坏，请重新登录: {exc}") from exc
        if not isinstance(pending_payload, dict) or not isinstance(storage_state, dict):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达图片验证码会话无效，请重新登录。")

        session = self._session_from_storage_state_payload(storage_state)
        csrf = str(pending_payload.get("csrf") or "").strip()
        login_url = str(pending_payload.get("login_url") or config.login_url).strip() or config.login_url
        login_action_url = str(pending_payload.get("login_action_url") or login_url).strip() or login_url
        parsed_login = urlparse(login_url)
        login_origin = f"{parsed_login.scheme}://{parsed_login.netloc}" if parsed_login.scheme and parsed_login.netloc else "https://ky-sso.yunda56.com"
        payload = {
            "_csrf": csrf,
            "username": config.username,
            "password": config.password,
            "verifyCode": captcha_code,
        }
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": login_origin,
            "Referer": login_url,
        }
        try:
            response = session.post(
                login_action_url,
                data=payload,
                headers=headers,
                allow_redirects=True,
                timeout=30,
            )
        except Exception as exc:
            raise TMSAuthStateError("AUTH_PENDING_CODE", f"韵达验证码提交失败，请重试: {exc}") from exc

        body = response.text or ""
        current_url = str(response.url or "")
        still_on_login = "ky-sso.yunda56.com/login" in current_url or 'id="login_form"' in body
        if still_on_login:
            csrf = self._extract_yunda_csrf_from_html(body) or csrf
            login_error = self._read_yunda_html_error(body) or "韵达图片验证码不正确或登录未完成。"
            captcha_image = ""
            captcha_image_mime = ""
            try:
                captcha_response = session.get(
                    urljoin(login_url, "/public/captcha"),
                    headers={"Referer": login_url},
                    timeout=15,
                )
                captcha_response.raise_for_status()
                content_type = str(captcha_response.headers.get("content-type") or "image/png").split(";")[0]
                captcha_image_mime = content_type or "image/png"
                captcha_image = (
                    f"data:{captcha_image_mime};base64,"
                    + base64.b64encode(captcha_response.content).decode("ascii")
                )
            except Exception:
                pass
            self._write_pending_requests_state_locked(
                session,
                config,
                csrf=csrf,
                login_url=login_url,
                login_action_url=login_action_url,
                captcha_image=captcha_image,
                captcha_image_mime=captcha_image_mime,
                message=login_error,
                pending_since=pending_since,
            )
            raise TMSAuthStateError("AUTH_PENDING_CODE", login_error)

        return self._persist_requests_session_locked(session, config)

    def _submit_yunda_sms_login(
        self,
        config: LoginConfig,
        *,
        sms_code: str,
        pending_since: str,
    ) -> dict[str, Any]:
        if not self._pending_storage_state_path.exists() or not self._pending_login_state_path.exists():
            raise TMSAuthStateError("AUTH_REQUIRED", "当前没有待提交的韵达短信验证码会话，请先点击登录。")
        try:
            pending_payload = json.loads(self._pending_login_state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TMSAuthStateError("AUTH_REQUIRED", f"韵达短信验证码会话已损坏，请重新登录: {exc}") from exc
        if not isinstance(pending_payload, dict):
            raise TMSAuthStateError("AUTH_REQUIRED", "韵达短信验证码会话无效，请重新登录。")

        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise self._dependency_error("Playwright Python 依赖未安装，无法提交韵达短信验证码。") from exc
        except Exception as exc:
            raise self._dependency_error(f"Playwright 依赖加载失败: {exc}") from exc

        login_url = str(pending_payload.get("login_url") or config.login_url).strip() or config.login_url
        playwright = sync_playwright().start()
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(**_chromium_launch_kwargs())
            context = browser.new_context(
                viewport={"width": 1440, "height": 960},
                storage_state=str(self._pending_storage_state_path),
            )
            page = context.new_page()
            page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                page.wait_for_timeout(1_000)

            if self._is_yunda_login_page(page):
                self._save_meta(
                    {
                        "status": "logged_out",
                        "last_validation_at": "",
                        "last_error_summary": "韵达短信验证会话已失效，请重新登录。",
                        "authenticated_at": "",
                        "pending_since": "",
                        "expires_at": "",
                    }
                )
                raise TMSAuthStateError("AUTH_REQUIRED", "韵达短信验证会话已失效，请重新登录。")

            if not self._is_yunda_sms_page(page):
                meta = self._persist_storage_state_locked(context, page)
                self._close_pending_locked()
                return meta

            page.locator(YUNDA_SMS_CODE_INPUT).fill(sms_code)
            page.locator(YUNDA_SMS_CONFIRM_BUTTON).first.click(timeout=5_000)
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass

            still_on_sms_page, sms_error = self._wait_for_yunda_sms_submit_result(page)
            if still_on_sms_page:
                sms_error = sms_error or "韵达短信验证码不正确或登录未完成。"
                self._save_yunda_sms_pending_state_locked(
                    context,
                    page,
                    config=config,
                    message=sms_error,
                    pending_since=pending_since,
                    send_sms=False,
                )
                raise TMSAuthStateError("AUTH_PENDING_CODE", sms_error)

            if self._is_yunda_login_page(page):
                login_error = self._read_yunda_login_error(page) or "韵达短信验证未完成，请重新登录。"
                self._save_meta(
                    {
                        "status": "logged_out",
                        "last_validation_at": "",
                        "last_error_summary": login_error,
                        "authenticated_at": "",
                        "pending_since": "",
                        "expires_at": "",
                    }
                )
                raise TMSAuthStateError("AUTH_REQUIRED", login_error)

            meta = self._persist_storage_state_locked(context, page)
            self._close_pending_locked()
            return meta
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                playwright.stop()
            except Exception:
                pass

    def send_yunda_code(self) -> dict[str, Any]:
        with self._lock:
            self._close_pending_locked()
            config = self.resolve_login_config()
            self._ensure_login_prerequisites(config)
            return self._run_in_isolated_thread(lambda: self._run_yunda_password_login(config))

    def submit_yunda_code(self, code: str) -> dict[str, Any]:
        code_value = str(code or "").strip()
        if not code_value:
            raise TMSAuthStateError("AUTH_PENDING_CODE", "韵达验证码不能为空。")
        with self._lock:
            config = self.resolve_login_config()
            meta = self._load_meta()
            pending_since = meta.get("pending_since") or _format_ts(_now_ts())
            challenge_type = str(meta.get("challenge_type") or "").strip().lower()
            if self._pending_login_state_path.exists():
                try:
                    pending_payload = json.loads(self._pending_login_state_path.read_text(encoding="utf-8"))
                    if isinstance(pending_payload, dict):
                        challenge_type = str(pending_payload.get("challenge_type") or challenge_type).strip().lower()
                except Exception as exc:
                    if challenge_type == "sms":
                        raise TMSAuthStateError("AUTH_REQUIRED", f"韵达短信验证码会话已损坏，请重新登录: {exc}") from exc
            if challenge_type == "sms":
                return self._run_in_isolated_thread(
                    lambda: self._submit_yunda_sms_login(
                        config,
                        sms_code=code_value,
                        pending_since=pending_since,
                    )
                )
            return self._submit_yunda_captcha_login(
                config,
                captcha_code=code_value,
                pending_since=pending_since,
            )

    def send_ronghui_code(self) -> dict[str, Any]:

        def run_send(config: LoginConfig) -> dict[str, Any]:
            try:
                from playwright.sync_api import sync_playwright
            except ModuleNotFoundError as exc:
                raise self._dependency_error("Playwright Python 依赖未安装，无法发起登录。") from exc
            except Exception as exc:
                raise self._dependency_error(f"Playwright 依赖加载失败: {exc}") from exc

            playwright = sync_playwright().start()
            browser = None
            context = None
            try:
                browser = playwright.chromium.launch(**_chromium_launch_kwargs())
                context = browser.new_context(viewport={"width": 1440, "height": 960})
                page = context.new_page()
                page.goto(config.login_url, wait_until="domcontentloaded", timeout=60_000)
                page.locator(USERNAME_INPUT).wait_for(state="visible", timeout=15_000)
                page.locator(USERNAME_INPUT).fill(config.username)
                page.locator(PASSWORD_INPUT).fill(config.password)
                if self._locator_visible(page, PHONE_INPUT) and self._locator_visible(page, SEND_CODE_BUTTON):
                    if not config.phone:
                        raise TMSAuthStateError("AUTH_REQUIRED", "手机号不能为空。")
                    page.locator(PHONE_INPUT).fill(config.phone)
                    page.locator(SEND_CODE_BUTTON).click()
                    page.wait_for_timeout(1200)
                    error_text = self._read_login_error(page)
                    if error_text:
                        raise TMSAuthStateError("AUTH_REQUIRED", error_text)
                    context.storage_state(path=str(self._pending_storage_state_path))
                    return {
                        "status": "pending_code",
                        "last_validation_at": "",
                        "last_error_summary": "",
                        "authenticated_at": "",
                        "pending_since": _format_ts(_now_ts()),
                        "expires_at": "",
                        "challenge_type": "sms",
                        "challenge_label": "短信验证码",
                    }
                if self._locator_visible(page, CODE_INPUT) and self._locator_visible(page, CAPTCHA_IMAGE):
                    return self._auto_login_ronghui_image_captcha_browser(context, page, config)
                raise TMSAuthStateError("AUTH_REQUIRED", "融辉登录页未找到手机号或图片验证码输入框，可能页面结构已变化。")
            except TMSAuthStateError:
                raise
            except Exception as exc:
                raise TMSAuthStateError("AUTH_REQUIRED", f"融辉登录页操作失败: {exc}") from exc
            finally:
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass
                try:
                    playwright.stop()
                except Exception:
                    pass

        with self._lock:
            self._close_pending_locked()
            config = self.resolve_login_config()
            self._ensure_login_prerequisites(config, require_phone=False)
            result = self._run_in_isolated_thread(lambda: run_send(config))
            if isinstance(result, dict):
                if result.get("status") == "pending_code":
                    if result.get("challenge_type") == "image":
                        return result
                    return self._save_meta(result)
                return result
            return self._save_meta(
                {
                    "status": "pending_code",
                    "last_validation_at": "",
                    "last_error_summary": "",
                    "authenticated_at": "",
                    "pending_since": _format_ts(_now_ts()),
                    "expires_at": "",
                    "challenge_type": "sms",
                    "challenge_label": "短信验证码",
                }
            )

    def submit_ronghui_code(self, code: str) -> dict[str, Any]:

        def run_submit(config: LoginConfig, sms_code: str, pending_since: str) -> dict[str, Any]:
            try:
                from playwright.sync_api import sync_playwright
            except ModuleNotFoundError as exc:
                raise self._dependency_error("Playwright Python 依赖未安装，无法提交短信验证码。") from exc
            except Exception as exc:
                raise self._dependency_error(f"Playwright 依赖加载失败: {exc}") from exc

            playwright = sync_playwright().start()
            browser = None
            context = None
            try:
                browser = playwright.chromium.launch(**_chromium_launch_kwargs())
                context = browser.new_context(
                    viewport={"width": 1440, "height": 960},
                    storage_state=str(self._pending_storage_state_path),
                )
                page = context.new_page()
                page.goto(config.login_url, wait_until="domcontentloaded", timeout=60_000)
                page.locator(USERNAME_INPUT).fill(config.username)
                page.locator(PASSWORD_INPUT).fill(config.password)
                page.locator(PHONE_INPUT).fill(config.phone)
                page.locator(CODE_INPUT).fill(sms_code)
                page.locator(LOGIN_BUTTON).click()
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    page.wait_for_timeout(1_200)

                login_error = self._read_login_error(page)
                current_url = str(page.url or "").strip()
                if login_error or "/system/login" in current_url:
                    context.storage_state(path=str(self._pending_storage_state_path))
                    meta = self._save_meta(
                        {
                            "status": "pending_code",
                            "last_validation_at": "",
                            "last_error_summary": login_error or "登录未完成，仍停留在登录页",
                            "authenticated_at": "",
                            "pending_since": pending_since,
                            "expires_at": "",
                        }
                    )
                    raise TMSAuthStateError("AUTH_PENDING_CODE", meta["last_error_summary"])

                self._pending_storage_state_path.unlink(missing_ok=True)
                meta = self._persist_storage_state_locked(context, page)
                self._close_pending_locked()
                return meta
            finally:
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass
                try:
                    playwright.stop()
                except Exception:
                    pass

        sms_code = str(code or "").strip()
        if not sms_code:
            raise TMSAuthStateError("AUTH_PENDING_CODE", "验证码不能为空")
        with self._lock:
            if not self._pending_storage_state_path.exists():
                raise TMSAuthStateError("AUTH_REQUIRED", "当前没有待提交的验证码会话，请先发送验证码")
            pending_since = self._load_meta().get("pending_since") or _format_ts(_now_ts())
            config = self.resolve_login_config()
            if self._pending_challenge_type_locked() == "image":
                return self._run_in_isolated_thread(
                    lambda: self._submit_ronghui_captcha_login(
                        config,
                        captcha_code=sms_code,
                        pending_since=pending_since,
                    )
                )
            return self._run_in_isolated_thread(lambda: run_submit(config, sms_code, pending_since))
