"""BEST V5's official WeChat QR flow, with account-scoped staged state."""

from __future__ import annotations

import base64
import json
from typing import Any

import requests

from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.session_models import format_ts, now_ts
from agent.tms_runtime.session_provider_base import ProviderSessionAdapterBase
from agent.tms_runtime.session_support import _set_requests_cookie_from_storage


BEST_ORIGIN = "https://v5.800best.com"
BEST_MENU_PATH = "/ltlv5-war/web/menu/getUserMenuVos"
BEST_QR_PATH = "/ltlv5-war/web/wxLogin/getQrCode"
BEST_QR_STATUS_PATH = "/ltlv5-war/web/wxLogin/getQrStatus"
BEST_CACHE_LOGIN_PATH = "/ltlv5-war/web/user/cacheLogin"
QR_TTL_SECONDS = 180


def best_payload(response: Any) -> dict[str, Any]:
    """Keep remote authentication data and error bodies out of public errors."""
    if response.status_code in {401, 403}:
        raise TMSAuthStateError("AUTH_REQUIRED", "百世快运登录态已失效，请重新扫码。")
    if response.status_code != 200:
        raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运服务暂时不可用，请稍后重试。")
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运接口未返回有效数据。") from exc
    if not isinstance(payload, dict) or not str(payload.get("code") or "").strip():
        raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运接口缺少状态码。")
    if str(payload["code"]) == "40001":
        raise TMSAuthStateError("AUTH_REQUIRED", "百世快运登录态已失效，请重新扫码。")
    if str(payload["code"]) != "200":
        raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运拒绝了本次请求，请重新扫码。")
    return payload


def validate_best_menu(response: Any) -> None:
    payload = best_payload(response)
    vo = payload.get("vo")
    if (not isinstance(vo, dict) or not isinstance(vo.get("menuTreeNode"), dict)
            or not isinstance(vo["menuTreeNode"].get("children"), list)):
        raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运菜单校验未通过，无法确认登录态。")


class BestSessionAdapter(ProviderSessionAdapterBase):
    @staticmethod
    def _best_request(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return session.request(method, url, timeout=15, **kwargs)
        except requests.RequestException:
            # A requests exception may contain the QR ticket in its URL.
            raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运连接失败，请稍后重试。") from None

    def _best_session(self, state: dict[str, Any] | None = None) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Origin": BEST_ORIGIN, "Referer": BEST_ORIGIN + "/login"})
        for cookie in (state or {}).get("cookies", []):
            _set_requests_cookie_from_storage(session, cookie)
        return session

    def _best_state(self, session: requests.Session) -> dict[str, Any]:
        cookies = self._storage_cookies_from_requests_session(session, "v5.800best.com")
        return {"cookies": [cookie for cookie in cookies if cookie["domain"].lstrip(".") in {
            "v5.800best.com", "800best.com",
        }], "origins": []}

    def _best_logged_in_state(self, session: requests.Session, user: dict[str, Any], site: dict[str, Any]) -> dict[str, Any]:
        info = {**user, "siteId": site["id"], "siteCode": site.get("code"),
                "siteName": site.get("name"), "siteType": site.get("type"), "loginType": "wxScan",
                "siteStr1": site.get("siteStr1"), "siteTypeCode": site.get("typeCode"),
                "siteCompanyId": site.get("companyId"),
                **{key: site.get(key) for key in ("parentSiteId", "parentSiteCode", "parentSiteName",
                                                  "authorityTreePath", "companyTreePath")}}
        # The official frontend uses these display cookies to restore USER_INFO.
        for name in ("userName", "orgId", "ownerSiteId"):
            session.cookies.set(name, str(user.get(name) or ""), domain="v5.800best.com", path="/",
                                expires=int(now_ts()) + 86400, rest={})
        storage = self._best_state(session)
        storage["origins"] = [{"origin": BEST_ORIGIN, "localStorage": [
            {"name": "USER_INFO", "value": json.dumps(info, ensure_ascii=False)},
        ]}]
        return storage

    def send_code(self) -> dict[str, Any]:
        with self._best_session() as session:
            payload = best_payload(self._best_request(session, "GET", BEST_ORIGIN + BEST_QR_PATH))
            vo = payload.get("vo")
            ticket = vo.get("ticket") if isinstance(vo, dict) else None
            if not isinstance(ticket, str) or not ticket:
                raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运未返回扫码凭证。")
            image = self._best_request(session, "GET", "https://mp.weixin.qq.com/cgi-bin/showqrcode", params={"ticket": ticket})
            content_type = image.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type == "image/jpg":
                content_type = "image/jpeg"
            if image.status_code != 200 or content_type not in {"image/png", "image/jpeg"} or not image.content:
                raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运二维码图片获取失败。")
            self._state_store.write_dict(self._pending_storage_state_path, self._best_state(session))
            expires = now_ts() + QR_TTL_SECONDS
            self._state_store.write_dict(self._pending_login_state_path, {"ticket": ticket, "expires": expires})
            return self._save_meta({
                "status": "pending_code", "challenge_type": "qr", "challenge_label": "微信扫码登录",
                "captcha_image": f"data:{content_type};base64," + base64.b64encode(image.content).decode("ascii"),
                "captcha_image_mime": content_type, "captcha_captured_at": format_ts(now_ts()),
                "pending_since": format_ts(now_ts()), "expires_at": format_ts(expires),
                "last_error_summary": "请使用已绑定百世账号的微信扫码并确认。",
            })

    def _best_expired(self, message: str) -> dict[str, Any]:
        for path in (self._pending_login_state_path, self._pending_storage_state_path):
            path.unlink(missing_ok=True)
        return self._save_meta({"status": "expired", "last_error_summary": message})

    def submit_code(self, code: str) -> dict[str, Any]:
        pending = self._state_store.read_dict(self._pending_login_state_path)
        state = self._state_store.read_dict(self._pending_storage_state_path)
        if not pending or not state or not isinstance(pending.get("ticket"), str):
            raise TMSAuthStateError("AUTH_REQUIRED", "请先生成百世快运登录二维码。")
        if now_ts() >= float(pending.get("expires") or 0):
            return self._best_expired("百世快运二维码已过期，请刷新后重新扫码。")
        with self._best_session(state) as session:
            choices = pending.get("account_choices")
            if not choices:
                result = best_payload(self._best_request(session, "GET", BEST_ORIGIN + BEST_QR_STATUS_PATH,
                                                        params={"ticket": pending["ticket"]}))
                qr_status = str(result.get("udf2") or "")
                if qr_status in {"EXPIRED", "UN_GUANZHU", "REFRESH"}:
                    return self._best_expired("请关注并绑定百世快运公众号，再刷新二维码扫码。" if qr_status != "EXPIRED"
                                              else "百世快运二维码已过期，请刷新后重新扫码。")
                if qr_status == "UN_SCAN":
                    self._state_store.write_dict(self._pending_storage_state_path, self._best_state(session))
                    return self._load_meta()
                if qr_status != "SUCCESS":
                    raise TMSAuthStateError("AUTH_UNAVAILABLE", "百世快运返回了未知扫码状态，请刷新二维码重试。")
                candidates = result.get("voList")
                if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
                    raise TMSAuthStateError("AUTH_REQUIRED", "百世快运扫码账号数据不完整。")
                candidates = [item for item in candidates if item.get("isEnable") is True or item.get("isEnable") == 1]
                if not candidates or any(not str(item.get("userName") or "").strip() for item in candidates):
                    raise TMSAuthStateError("AUTH_REQUIRED", "扫码微信没有可用的百世快运账号。")
                choices = [{"value": str(item["userName"]),
                            "label": f"{item.get('ownerSiteName') or ''} / {item['userName']}"} for item in candidates]
                if len({item["value"] for item in choices}) != len(choices):
                    raise TMSAuthStateError("AUTH_REQUIRED", "百世快运账号返回重复，无法唯一确定登录账号。")
                pending["account_choices"] = choices
                self._state_store.write_dict(self._pending_login_state_path, pending)
                self._state_store.write_dict(self._pending_storage_state_path, self._best_state(session))
            names = [item["value"] for item in choices]
            if code == "check" and len(names) != 1:
                return self._save_meta({**self._load_meta(), "account_choices": choices,
                                       "last_error_summary": "此微信绑定多个账号，请选择本次保存的账号。"})
            selected = names[0] if code == "check" and len(names) == 1 else code
            if selected not in names:
                raise TMSAuthStateError("AUTH_REQUIRED", "所选账号不属于本次扫码结果。")
            result = best_payload(self._best_request(session, "POST", BEST_ORIGIN + BEST_CACHE_LOGIN_PATH,
                                                    data={"cacheUserName": selected}))
            vo = result.get("vo")
            user = vo.get("userVo") if isinstance(vo, dict) else None
            sites = vo.get("siteVos") if isinstance(vo, dict) else None
            if (not isinstance(user, dict) or user.get("userName") != selected or not isinstance(sites, list)
                    or len(sites) != 1 or not isinstance(sites[0], dict) or not sites[0].get("id")):
                raise TMSAuthStateError("AUTH_REQUIRED", "百世快运登录身份或站点不唯一，未保存登录态。")
            site = sites[0]
            validate_best_menu(self._best_request(session, "GET", BEST_ORIGIN + BEST_MENU_PATH))
            storage = self._best_logged_in_state(session, user, site)
            self._state_store.write_dict(self._storage_state_path, storage)
            # SessionBroker consumes storage_state.json; no credential file or
            # dynamic password is created by QR login.
            self._cookies_path.unlink(missing_ok=True)
            for path in (self._pending_login_state_path, self._pending_storage_state_path):
                path.unlink(missing_ok=True)
            stamp = format_ts(now_ts())
            return self._save_meta({"status": "authenticated", "authenticated_at": stamp,
                                    "last_validation_at": stamp, "last_error_summary": ""})
