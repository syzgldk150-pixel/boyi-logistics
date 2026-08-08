import json
import os
import time
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse

import ddddocr
import requests


class TMSAuth:
    """
    使用 requests.Session + OCR 登录，无需浏览器/驱动，适合服务器环境。
    可通过 config.json 覆盖 login_api、captcha_endpoint 等。
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = os.environ.get(
                "CONFIG_PATH",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
            )

        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        headers = self.config.get("test_headers") or {}
        self.base_origin = (headers.get("Origin") or "https://tms.ronghuiwl.com").rstrip("/")
        self.login_page = self._build_url(self.config.get("login_page") or f"{self.base_origin}/system/login")
        self.login_api = self.config.get("login_api") or "/system/login"
        # 默认验证码接口与前端一致：/validateCode/code?timestamp
        self.captcha_endpoint = self.config.get("captcha_endpoint") or "/validateCode/code"
        self.verify_url = self._build_url(self.config.get("verify_url") or f"{self.base_origin}/widget/home")

        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.session = requests.Session()

        sanitized_headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
        if sanitized_headers:
            self.session.headers.update(sanitized_headers)
        self.session.headers.setdefault("Referer", self.login_page)

    @staticmethod
    def _js_escape(text: str) -> str:
        safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./"
        out = []
        for ch in text:
            if ch in safe:
                out.append(ch)
                continue
            code = ord(ch)
            if code < 256:
                out.append(f"%{code:02X}")
            else:
                out.append(f"%u{code:04X}")
        return "".join(out)

    def _set_user_info_cookie(self, user_info: Dict[str, str]) -> None:
        if not isinstance(user_info, dict) or not user_info:
            return
        domain = urlparse(self.base_origin).hostname or None
        cookie_value = self._js_escape(json.dumps(user_info, ensure_ascii=False, separators=(",", ":")))
        self.session.cookies.set("userInfo", cookie_value, domain=domain, path="/")

    def _build_url(self, path_or_url: str) -> str:
        if not path_or_url:
            return self.base_origin
        if path_or_url.lower().startswith("http"):
            return path_or_url
        return urljoin(self.base_origin + "/", path_or_url.lstrip("/"))

    def _fetch_captcha_bytes(self) -> bytes:
        captcha_url = self._build_url(self.captcha_endpoint)
        resp = self.session.get(
            captcha_url,
            params={"_t": int(time.time() * 1000)},
            timeout=10,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": self.login_page,
            },
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            snippet = resp.text[:200] if hasattr(resp, "text") else ""
            raise RuntimeError(f"验证码接口返回的不是图片，Content-Type={content_type}, 内容片段={snippet}")
        return resp.content

    def _solve_captcha(self) -> Optional[str]:
        image_bytes = self._fetch_captcha_bytes()
        try:
            code = self.ocr.classification(image_bytes)
            return code.strip()
        except Exception as exc:
            raise RuntimeError(f"无法识别验证码图片: {exc}")

    def _is_login_success(self, resp: requests.Response) -> bool:
        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "")
            if location and "/system/login" not in location:
                return True

        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                if payload.get("success") is True:
                    return True
                if payload.get("code") in (0, "0"):
                    return True
                status_value = str(payload.get("status")).lower()
                if status_value in {"ok", "success", "200"}:
                    return True

        if resp.status_code == 200:
            body = resp.text or ""
            if "validateCode" not in body and "system/login" not in body:
                return True

        return False

    def _verify_authenticated(self) -> bool:
        try:
            resp = self.session.get(self.verify_url, allow_redirects=False, timeout=10)
        except Exception:
            return True

        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "")
            if location and "/system/login" in location:
                return False

        if resp.status_code == 200:
            body = resp.text or ""
            if "username" in body and "password" in body and "/system/login" in body:
                return False

        return resp.status_code < 500

    def _attempt_login_once(self, user_info: Dict[str, str]) -> bool:
        captcha_code = self._solve_captcha()
        if not captcha_code:
            return False

        payload = {
            "username": user_info["operator_uid"],
            "password": user_info["operator_password"],
            "validateCode": captcha_code,
        }
        login_url = self._build_url(self.login_api)
        resp = self.session.post(login_url, data=payload, allow_redirects=False, timeout=12)
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                login_result = resp.json()
            except ValueError:
                login_result = None
            if isinstance(login_result, dict) and isinstance(login_result.get("result"), dict):
                self._set_user_info_cookie(login_result["result"])
        return self._is_login_success(resp)

    def login_and_get_session(self, max_attempts: int = 6):
        user_info = self.config.get("test_user_data") or {}
        if "operator_uid" not in user_info or "operator_password" not in user_info:
            raise RuntimeError("config.json 缺少 operator_uid / operator_password")

        try:
            self.session.get(self.login_page, timeout=10)
        except Exception as exc:
            print(f"[Login] 预加载登录页失败: {exc}")

        for attempt in range(1, max_attempts + 1):
            try:
                if self._attempt_login_once(user_info) and self._verify_authenticated():
                    print(f"[Login] 第 {attempt} 次尝试成功，已获取 Session（requests）")
                    return self.session
                print(f"[Login] 第 {attempt} 次尝试失败，重试中...")
            except Exception as exc:
                print(f"[Login] 第 {attempt} 次尝试异常: {exc}")
            time.sleep(1.0)

        return None
