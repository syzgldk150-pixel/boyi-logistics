"""System management forms for shared identities and all Feishu bindings."""
from __future__ import annotations

from http import HTTPStatus

from console.app_support import current_admin_user
from shared.identity_permissions import PERMISSIONS


def binding_target(value: str) -> dict:
    kind, separator, identity = str(value).partition(":")
    if not separator or not identity:
        raise ValueError("请选择飞书账号要继承的身份或后台账号")
    if kind == "role":
        return {"role_id": identity}
    if kind == "account" and identity.isdecimal() and int(identity) > 0:
        return {"account_id": int(identity)}
    raise ValueError("绑定对象无效")


class IdentitiesServiceMixin:
    def _identity_form_allowed(self, handler) -> bool:
        return self._require_super_admin_account_write(handler) and self._require_same_origin_write(handler)

    def _render_identity_accounts(self, handler, query, *, binding_challenge=None):
        if not self._is_super_admin_user(current_admin_user()):
            self._send_text(handler, HTTPStatus.FORBIDDEN, "只有超级管理员可以管理账号与身份。")
            return
        identities = self.repository.identities
        self._send_html(handler, self.template_env.get_template("admin_accounts.html").render(
            app_title=self.settings.app_title, users=self.repository.list_admin_users(),
            is_super_admin=True, identity_roles=identities.list_roles(),
            feishu_bindings=identities.list_bindings(), permission_options=PERMISSIONS,
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
                permissions=[key.removeprefix("permission_") for key, value in values.items()
                             if key.startswith("permission_") and value == "1"],
                active=values.get("active") == "1",
            )
        except ValueError as exc:
            self._redirect_with_message(handler, "/settings/accounts", str(exc), "warning")
            return
        self._redirect_with_message(handler, "/settings/accounts", "身份已保存，所绑定账号和飞书的新请求立即使用新权限。", "success")

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
        self._redirect_with_message(handler, "/settings/accounts", "飞书账号已解绑，可以重新绑定。" if revoke else "飞书账号身份已更新。", "success")
