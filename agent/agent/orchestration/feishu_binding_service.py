"""Feishu identity binding and sender resolution for the current ingress."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.feishu_command_contract import match_feishu_approval_binding
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from shared.feishu_binding_repository import BindingChallengeExpired, BindingConflict


_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


class FeishuBindingService:
    """Bind Feishu senders to Console identities and resolve verified senders."""

    def __init__(self, repository: Any, *, identity_access=None) -> None:
        self._repository = repository
        self.identity_access = identity_access

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode("ascii")).hexdigest()

    def create_binding_challenge(self, admin_user_id: int, *, role_id=None, account_id=None, label="") -> dict[str, Any]:
        if not isinstance(label, str) or len(label.strip()) > 80:
            raise OrchestrationError("INVALID_INPUT", "飞书账号备注需为不超过 80 个字符的文本")
        if role_id is not None and (not isinstance(role_id, str) or not role_id.strip()):
            raise OrchestrationError("INVALID_INPUT", "身份编号格式无效")
        if account_id is not None and (type(account_id) is not int or account_id <= 0):
            raise OrchestrationError("INVALID_INPUT", "后台账号编号格式无效")
        if role_id and account_id:
            raise OrchestrationError("INVALID_INPUT", "请选择身份或后台账号，不能同时选择")
        if not role_id and not account_id:
            account_id = admin_user_id  # The existing self-binding endpoint's explicit contract.
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        challenge_id = str(uuid.uuid4())
        with self._repository.unit_of_work() as uow:
            admin = uow.feishu_bindings.get_admin_user(admin_user_id, for_update=True)
            if (
                admin is None
                or admin.get("is_active") not in {True, 1}
                or str(admin.get("control_plane_role") or "") != "super_admin"
            ):
                raise OrchestrationError(
                    "FEISHU_BINDING_FORBIDDEN",
                    "Only an active Console super administrator can create a binding code",
                )
            try:
                uow.feishu_bindings.create_challenge({
                    "challenge_id": challenge_id,
                    "admin_user_id": int(admin_user_id),
                    "code_sha256": self._digest(code),
                    "expires_at": expires_at,
                    "access_role_id": role_id,
                    "identity_account_id": account_id,
                    "identity_label": str(label).strip(),
                })
            except ValueError as exc:
                raise OrchestrationError("INVALID_INPUT", "所选身份或后台账号不存在或已停用") from exc
            uow.commit()
        return {
            "challenge_id": challenge_id,
            "binding_code": code,
            "expires_at": expires_at.isoformat(),
            "command": f"绑定审批 {code}",
        }

    def binding_status(self, admin_user_id: int) -> dict[str, Any]:
        with self._repository.unit_of_work() as uow:
            admin = uow.feishu_bindings.get_admin_user(admin_user_id)
            binding = (
                uow.feishu_bindings.get_binding_for_admin(admin_user_id)
                if admin is not None
                else None
            )
        is_bound = bool(binding and binding.get("active") in {True, 1})
        public_binding = None
        if binding is not None:
            public_binding = {
                "notifications_enabled": bool(binding.get("notifications_enabled")),
                "active": bool(binding.get("active")),
                "bound_at": binding.get("bound_at"),
                "revoked_at": binding.get("revoked_at"),
                "updated_at": binding.get("updated_at"),
            }
        return {"bound": is_bound, "binding": public_binding}

    def revoke_binding(self, admin_user_id: int) -> None:
        with self._repository.unit_of_work() as uow:
            uow.feishu_bindings.revoke(admin_user_id)
            uow.commit()

    def resolve_actor(self, open_id: str) -> Actor:
        if self.identity_access is not None:
            access = self.identity_access.feishu(open_id)
            return Actor(ActorType.FEISHU_USER, str(open_id),
                         roles=("admin", "super_admin") if access.active and access.super_admin else ("admin",) if access.active else (),
                         display_name=access.role_name if access.active else "",
                         authenticated_by="feishu_admin_binding" if access.active else "feishu_verified_event")
        with self._repository.unit_of_work() as uow:
            binding = uow.feishu_bindings.resolve_binding(open_id)
        if (
            binding
            and binding.get("active") in {True, 1}
            and binding.get("is_active") in {True, 1}
            and str(binding.get("control_plane_role") or "") == "super_admin"
        ):
            return Actor(
                ActorType.FEISHU_USER,
                str(open_id),
                roles=("admin", "super_admin"),
                display_name=str(binding.get("display_name") or binding.get("username") or ""),
                authenticated_by="feishu_admin_binding",
            )
        return Actor(
            ActorType.FEISHU_USER,
            str(open_id),
            roles=(),
            authenticated_by="feishu_verified_event",
        )

    def handle_binding_text(self, open_id: str, chat_id: str, text: str) -> str | None:
        """Current Feishu ingress only binds identity; it never decides old Runs."""
        normalized = str(text or "").strip()
        match = match_feishu_approval_binding(normalized)
        if match:
            return self._bind(open_id, chat_id, match.group(1).upper())
        return None

    def _bind(self, open_id: str, chat_id: str, code: str) -> str:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            with self._repository.unit_of_work() as uow:
                failure = uow.feishu_bindings.failure_state(open_id, for_update=True)
                if failure and failure.get("is_locked"):
                    uow.commit()
                    return "绑定尝试过多，请稍后再试。"
                challenge = uow.feishu_bindings.get_challenge_by_digest(
                    self._digest(code),
                    for_update=True,
                )
                valid = bool(
                    challenge
                    and challenge.get("used_at") is None
                    and isinstance(challenge.get("expires_at"), datetime)
                    and challenge["expires_at"] > now
                    and challenge.get("is_active") in {True, 1}
                    and str(challenge.get("control_plane_role") or "") == "super_admin"
                )
                if not valid:
                    uow.feishu_bindings.record_failure(open_id)
                    uow.commit()
                    return "绑定码无效、已过期或已使用。"
                uow.feishu_bindings.consume_challenge(
                    challenge_id=str(challenge["challenge_id"]),
                    admin_user_id=int(challenge["admin_user_id"]),
                    binding_id=str(uuid.uuid4()),
                    open_id=open_id,
                    chat_id=chat_id,
                )
                uow.commit()
        except BindingConflict:
            return "该飞书账号已有绑定，请先在后台系统管理中查看或解绑。"
        except BindingChallengeExpired:
            return "绑定码无效、已过期或已使用。"
        except ValueError:
            return "所选身份或账号已失效，请在后台重新生成绑定码。"
        return "飞书身份绑定成功；身份权限变更或解绑会立即生效。"
