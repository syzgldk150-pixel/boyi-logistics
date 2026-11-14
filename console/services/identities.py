"""System management forms for shared identities, Feishu bindings and WeChat devices."""
from __future__ import annotations

import re
from http import HTTPStatus

from console.app_support import current_admin_user
from shared.identity_permissions import AGENT_CAPABILITIES, CONSOLE_MODULES

WECHAT_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def device_permissions(values: dict) -> list[str] | None:
    """``None`` keeps the identity's permissions; otherwise only the ticked ones."""
    if values.get("limit_permissions") not in {"1", True}:
        return None
    raw = values.get("permissions")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return [key.removeprefix("permission_") for key, value in values.items()
            if key.startswith("permission_") and value == "1"]


def role_levels(values: dict, prefix: str, catalog) -> dict[str, str]:
    """``console_<key>``/``agent_<key>`` selects; empty means not granted."""
    return {key: str(values.get(prefix + key)) for key in catalog if values.get(prefix + key)}


def binding_target(value) -> dict:
    """The person a Feishu binding or WeChat device belongs to."""
    person_id = str(value or "").strip()
    if not WECHAT_UUID_RE.fullmatch(person_id):
        raise ValueError("请选择人员")
    return {"person_id": person_id}


class IdentitiesServiceMixin:
    def _identity_form_allowed(self, handler) -> bool:
        return self._require_super_admin_account_write(handler) and self._require_same_origin_write(handler)

    def _render_identity_accounts(self, handler, query, *, binding_challenge=None):
        if not self._is_super_admin_user(current_admin_user()):
            self._send_text(handler, HTTPStatus.FORBIDDEN, "只有超级管理员可以管理账号与身份。")
            return
        identities = self.repository.identities
        persons = identities.list_persons()
        self._send_html(handler, self.template_env.get_template("admin_accounts.html").render(
            app_title=self.settings.app_title, users=self.repository.list_admin_users(),
            is_super_admin=True, identity_roles=identities.list_roles(),
            feishu_bindings=identities.list_bindings(), console_modules=CONSOLE_MODULES, agent_capabilities=AGENT_CAPABILITIES, persons=persons,
            wechat_devices=self.repository.wechat_devices.list_devices(),
            binding_challenge=binding_challenge,
            message=query.get("message", [""])[0], message_kind=query.get("kind", ["info"])[0],
        ))

    def _handle_identity_save(self, handler):
        if not self._identity_form_allowed(handler):
            return
        values = self._parse_urlencoded_form(handler)
        try:
            self.repository.identities.save_role(
                role_id=values.get("role_id") or None, name=values.get("name", ""),
                console=role_levels(values, "console_", CONSOLE_MODULES),
                agent=role_levels(values, "agent_", AGENT_CAPABILITIES),
                active=values.get("active") == "1",
            )
        except ValueError as exc:
            self._redirect_with_message(handler, "/settings/accounts", str(exc), "warning")
            return
        self._redirect_with_message(handler, "/settings/accounts", "身份已保存，该身份下人员的后台、飞书和微信的新请求立即使用新权限。", "success")

    def _handle_person_save(self, handler):
        if not self._identity_form_allowed(handler):
            return
        values = self._parse_urlencoded_form(handler)
        person_id = str(values.get("person_id") or "").strip() or None
        if person_id and not WECHAT_UUID_RE.fullmatch(person_id):
            self._redirect_with_message(handler, "/settings/accounts", "人员编号无效", "warning")
            return
        try:
            self.repository.identities.save_person(
                person_id=person_id, name=values.get("name", ""),
                role_id=str(values.get("access_role_id") or "").strip() or None,
                active=None if "active" not in values else values.get("active") == "1",
            )
        except ValueError as exc:
            self._redirect_with_message(handler, "/settings/accounts", str(exc), "warning")
            return
        self._redirect_with_message(handler, "/settings/accounts",
                                    "人员已保存，他的后台账号、飞书与微信的新请求立即使用新身份。" if person_id else "人员已新增。", "success")

    def _handle_identity_binding_update(self, handler, binding_id, *, revoke):
        if not self._identity_form_allowed(handler):
            return
        values = self._parse_urlencoded_form(handler)
        try:
            self.repository.identities.update_binding(
                binding_id, revoke=revoke, label=values.get("label", ""),
                **({} if revoke else binding_target(values.get("target", ""))),
            )
        except ValueError as exc:
            self._redirect_with_message(handler, "/settings/accounts", str(exc), "warning")
            return
        self._redirect_with_message(handler, "/settings/accounts", "飞书账号已解绑，可以重新绑定。" if revoke else "飞书账号已更新。", "success")

    # -- WeChat devices (official iLink Bot API) ---------------------------
    def _wechat_json(self, handler, result) -> None:
        if result.get("ok") and isinstance(result.get("data"), dict):
            self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": result["data"], "error": None})
            return
        message = str(result.get("error") or "微信设备操作失败")
        self._send_json(handler, HTTPStatus.BAD_REQUEST, {
            "ok": False, "data": None,
            "error": {"code": str(result.get("error_code") or "WECHAT_BOT_FAILED"), "message": message}})

    def _handle_wechat_login_create(self, handler):
        if not self._identity_form_allowed(handler):
            return
        body = self._parse_json_body(handler)
        try:
            payload = {**binding_target(str(body.get("target") or "")),
                       "label": str(body.get("label") or ""), "permissions": device_permissions(body)}
        except ValueError as exc:
            self._wechat_json(handler, {"ok": False, "error": str(exc), "error_code": "INVALID_INPUT"})
            return
        self._wechat_json(handler, self._agent_request("POST", "/internal/v1/admin/wechat-bot/logins",
                                                       payload=payload, timeout=30))

    def _handle_wechat_login_status(self, handler, login_id):
        if not self._is_super_admin_user(current_admin_user()) or not WECHAT_UUID_RE.fullmatch(login_id):
            self._send_text(handler, HTTPStatus.FORBIDDEN, "只有超级管理员可以管理微信设备。")
            return
        self._wechat_json(handler, self._agent_request("GET", f"/internal/v1/admin/wechat-bot/logins/{login_id}", timeout=12))

    def _handle_wechat_login_action(self, handler, login_id, action):
        if not self._identity_form_allowed(handler):
            return
        if not WECHAT_UUID_RE.fullmatch(login_id):
            self._wechat_json(handler, {"ok": False, "error": "扫码绑定编号无效"})
            return
        if action == "verify-code":
            body = self._parse_json_body(handler)
            result = self._agent_request("POST", f"/internal/v1/admin/wechat-bot/logins/{login_id}/verify-code",
                                         payload={"code": str(body.get("code") or "")}, timeout=12)
        else:
            result = self._agent_request("DELETE", f"/internal/v1/admin/wechat-bot/logins/{login_id}", timeout=12)
        self._wechat_json(handler, result)

    def _handle_wechat_device_update(self, handler, device_id, *, revoke):
        if not self._identity_form_allowed(handler):
            return
        if not WECHAT_UUID_RE.fullmatch(device_id):
            self._redirect_with_message(handler, "/settings/accounts", "微信设备编号无效", "warning")
            return
        values = self._parse_urlencoded_form(handler)
        if revoke:
            result = self._agent_request("DELETE", f"/internal/v1/admin/wechat-bot/devices/{device_id}", timeout=20)
            if not result.get("ok"):
                self._redirect_with_message(handler, "/settings/accounts", str(result.get("error") or "微信设备解绑失败"), "warning")
                return
            self._redirect_with_message(handler, "/settings/accounts", "微信设备已解绑，该微信的新消息立即停止处理。", "success")
            return
        try:
            self.repository.wechat_devices.update_device(
                device_id, label=values.get("label", ""), permissions=device_permissions(values),
                **binding_target(values.get("target", "")))
        except ValueError as exc:
            self._redirect_with_message(handler, "/settings/accounts", str(exc), "warning")
            return
        self._redirect_with_message(handler, "/settings/accounts", "微信设备的人员与功能限制已更新，下一条消息立即生效。", "success")
