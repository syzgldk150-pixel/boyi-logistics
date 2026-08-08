"""Live session validation and authenticated-session construction."""

from agent.tms_runtime.session_support import *  # noqa: F403


class SessionValidationMixin:
    def _validate_storage_state_with_browser_locked(self) -> tuple[str, str]:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise self._dependency_error("Playwright Python dependency is missing; cannot validate TMS session.") from exc
        except Exception as exc:
            raise self._dependency_error(f"Playwright dependency failed to load: {exc}") from exc

        playwright = sync_playwright().start()
        browser = None
        context = None
        try:
            config = self.resolve_login_config()
            browser = playwright.chromium.launch(**_chromium_launch_kwargs())
            context = browser.new_context(
                viewport={"width": 1440, "height": 960},
                storage_state=str(self._storage_state_path),
            )
            page = context.new_page()
            page.goto(config.home_url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                page.wait_for_timeout(1_000)

            current_url = str(page.url or "").strip()
            on_login_page = any(keyword in current_url for keyword in self._login_url_keywords)
            try:
                if self._login_page_marker:
                    on_login_page = on_login_page or page.locator(self._login_page_marker).count() > 0
            except Exception:
                pass
            if on_login_page:
                return "expired", "登录态已失效，请重新发送验证码。"
            return "authenticated", ""
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

    def _should_validate_ronghui_scan_api_locked(self) -> bool:
        if self._is_yunda_mode():
            return False
        profile = str(self.profile_name or "").strip().lower()
        return profile != "price" and not profile.startswith("price_")

    def _should_validate_ronghui_menu_api_locked(self) -> bool:
        return self._should_validate_ronghui_scan_api_locked()

    def _validate_ronghui_scan_api_session_locked(
        self,
        session: requests.Session,
        config: LoginConfig,
    ) -> tuple[str, str]:
        today = dt.date.today()
        date_range = {
            "start": dt.datetime.combine(today, dt.time(0, 0, 0)).strftime("%Y/%m/%d %H:%M:%S"),
            "end": dt.datetime.combine(today, dt.time(23, 59, 59)).strftime("%Y/%m/%d %H:%M:%S"),
        }
        date_range_json = json.dumps(date_range, ensure_ascii=False)
        payload = {
            "searchOrderType": "BILL_CODE",
            "searchOrderInput": "",
            "SCAN_TYPE": RONGHUI_SCAN_VALIDATION_SCAN_TYPE,
            "searchDateType": "SCAN_DATE",
            "SEARCH_DATE_RANGE": date_range_json,
            "SCAN_DATE": date_range_json,
            "SCAN_SITE_CODE": RONGHUI_SCAN_VALIDATION_SITE_CODE,
            "LOGIN_SITE_CODE": RONGHUI_SCAN_VALIDATION_SITE_CODE,
            "pageIndex": "0",
            "pageSize": "1",
            "REMARK": "",
            "BL_SUB_RECEIPT": "",
            "sortField": "",
            "sortOrder": "",
            "totalColumns": "[]",
        }
        response = session.post(
            _join_origin_path(config.base_origin, RONGHUI_SCAN_VALIDATION_PATH),
            params={"id": RONGHUI_SCAN_VALIDATION_CALL_ID},
            data=payload,
            headers={
                "Accept": "text/plain, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": config.base_origin,
                "Referer": _join_origin_path(config.base_origin, "/widget/home"),
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
            timeout=15,
        )
        if response.status_code in {301, 302, 303, 307, 308} or looks_like_ronghui_login(response):
            return "expired", "Ronghui scan API validation reached login page; please login again."
        if response.status_code != 200:
            return "expired", f"Ronghui scan API validation failed: HTTP {response.status_code}."
        try:
            payload_json = response.json()
        except Exception as exc:
            return "expired", f"Ronghui scan API validation returned non-JSON response: {exc}."
        if not isinstance(payload_json, dict):
            return "expired", "Ronghui scan API validation returned an unexpected payload."
        blob = json.dumps(payload_json, ensure_ascii=False)
        blob_lower = blob.lower()
        if any(marker in blob_lower for marker in ("auth_required", "system/login", "validatecode")):
            return "expired", "Ronghui scan API validation response indicates login is required."
        return "authenticated", ""

    def _validate_ronghui_menu_api_session_locked(
        self,
        session: requests.Session,
        config: LoginConfig,
    ) -> tuple[str, str]:
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
        if response.status_code in {301, 302, 303, 307, 308} or looks_like_ronghui_login(response):
            return "expired", "Ronghui menu validation reached login page; please login again."
        if response.status_code != 200:
            return "expired", f"Ronghui menu validation failed: HTTP {response.status_code}."
        try:
            payload_json = response.json()
        except Exception as exc:
            return "expired", f"Ronghui menu validation returned non-JSON response: {exc}."
        if not isinstance(payload_json, dict):
            return "expired", "Ronghui menu validation returned an unexpected payload."
        if payload_json.get("success") is False:
            message = str(payload_json.get("message") or "").strip()
            return "expired", f"Ronghui menu validation failed: {message or 'menu API returned success=false'}."
        result = payload_json.get("result")
        data = result.get("data") if isinstance(result, dict) else result
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as exc:
                return "expired", f"Ronghui menu validation returned undecodable menu data: {exc}."
        if not isinstance(data, list):
            return "expired", "Ronghui menu validation did not return a menu tree."
        return "authenticated", ""

    def _validate_yunda_report_session_locked(self) -> tuple[str, str]:
        config = self.resolve_login_config()
        session = self._session_from_saved_state_locked()
        try:
            self._ensure_yunda_report_session_in_requests_locked(session, config)
            self._ensure_yunda_inms_session_in_requests_locked(session, config)
            self._ensure_yunda_message_session_in_requests_locked(session, config)
            self._ensure_yunda_problem_session_in_requests_locked(session, config)
        except TMSAuthStateError as exc:
            return "expired", str(exc)
        fallback_domain = urlparse(config.base_origin).hostname or "yunda56.com"
        cookies = self._storage_cookies_from_requests_session(session, fallback_domain)
        self._write_storage_cookies_locked(cookies)
        return "authenticated", ""

    def _validate_locked(self, *, force: bool = False) -> dict[str, Any]:
        meta = self._load_meta()
        if self._pending is not None or (
            str(meta.get("status") or "") == "pending_code" and self._pending_storage_state_path.exists()
        ):
            return self._save_meta(
                {
                    **meta,
                    "status": "pending_code",
                    "pending_since": meta.get("pending_since") or _format_ts(_now_ts()),
                    "label": _status_label("pending_code"),
                }
            )
        status = str(meta.get("status") or "logged_out")
        if (
            status in {"logged_out", "error"}
            and self._storage_state_path.exists()
            and str(meta.get("authenticated_at") or "").strip()
        ):
            meta = self._save_meta(
                {
                    **meta,
                    "status": "authenticated",
                    "last_error_summary": "",
                }
            )
            status = "authenticated"
        if status not in {"authenticated", "expired"} or not self._storage_state_path.exists():
            if status == "pending_code":
                if self._pending_storage_state_path.exists():
                    return self._save_meta(meta)
                return self._save_meta(
                    {
                        **meta,
                        "status": "logged_out",
                        "pending_since": "",
                        "label": _status_label("logged_out"),
                    }
                )
            return self._save_meta(meta)

        last_validation_text = str(meta.get("last_validation_at") or "").strip()
        if not force and last_validation_text:
            try:
                last_validation = time.mktime(time.strptime(last_validation_text, "%Y-%m-%d %H:%M:%S"))
            except Exception:
                last_validation = 0
            if _now_ts() - last_validation <= VALIDATION_TTL_SEC:
                return self._save_meta(meta)

        if self._is_yunda_mode():
            next_status, error_text = self._validate_yunda_report_session_locked()
        else:
            try:
                config = self.resolve_login_config()
                session = self._session_from_saved_state_locked()
                response = session.get(
                    config.home_url,
                    allow_redirects=False,
                    timeout=15,
                )
                body = response.text if response.status_code == 200 else ""
                location = str(response.headers.get("Location") or "")
                redirected_to_login = any(keyword in location for keyword in self._login_url_keywords)
                invalid = redirected_to_login or (
                    response.status_code == 200
                    and bool(self._login_body_markers)
                    and all(marker in body for marker in self._login_body_markers)
                )
                next_status = "expired" if invalid else "authenticated"
                error_text = "登录态已失效，请重新发送验证码。" if invalid else ""
                if next_status == "authenticated" and self._should_validate_ronghui_scan_api_locked():
                    try:
                        next_status, error_text = self._validate_ronghui_scan_api_session_locked(session, config)
                    except Exception as exc:
                        next_status = "error"
                        error_text = f"Ronghui scan API validation failed: {exc}"
                if next_status == "authenticated" and self._should_validate_ronghui_menu_api_locked():
                    try:
                        next_status, error_text = self._validate_ronghui_menu_api_session_locked(session, config)
                    except Exception as exc:
                        next_status = "error"
                        error_text = f"Ronghui menu validation failed: {exc}"
                if next_status == "authenticated":
                    fallback_domain = urlparse(config.base_origin).hostname or "tms.ronghuiwl.com"
                    cookies = self._storage_cookies_from_requests_session(session, fallback_domain)
                    self._write_storage_cookies_locked(cookies)
            except Exception as exc:
                requests_error = exc
                try:
                    next_status, error_text = self._validate_storage_state_with_browser_locked()
                except Exception as browser_exc:
                    next_status = "error"
                    error_text = f"登录态校验失败: {requests_error}; browser fallback failed: {browser_exc}"

        return self._save_meta(
            {
                **meta,
                "status": next_status,
                "last_validation_at": _format_ts(_now_ts()),
                "last_error_summary": error_text,
                "label": _status_label(next_status),
            }
        )
