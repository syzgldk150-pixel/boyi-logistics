"""MySQL persistence for WeChat bot devices (migration 081).

The Agent creates a device only after iLink confirms the QR scan; the Console
edits its label, identity and function limits or revokes it. Bot tokens are
never stored here.
"""
from __future__ import annotations

import json
from typing import Any

from shared.identity_repository import IdentityRepository, annotate_person
from shared.wechat_bot import CONNECTION_STATES, validate_device_permissions, validate_label


class WeChatDeviceConflict(ValueError):
    """The scanned WeChat bot is already bound to an active device."""


def _decode_permissions(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    values = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return list(validate_device_permissions(values) or ())


class WeChatDeviceRepository:
    def __init__(self, connect, *, cursor_factory=None):
        self._identities = IdentityRepository(connect, cursor_factory=cursor_factory)

    def cursor(self):
        return self._identities.cursor()

    def create_device(self, *, device_id: str, ilink_bot_id: str, owner_user_id: str, label: str,
                      person_id: str, permissions, created_by: int) -> None:
        allowed = validate_device_permissions(permissions)
        with self.cursor() as cursor:
            IdentityRepository.check_person(cursor, person_id)
            cursor.execute("SELECT device_id FROM wechat_bot_devices WHERE active_bot_id=%s FOR UPDATE", (ilink_bot_id,))
            if cursor.fetchone():
                raise WeChatDeviceConflict("该微信已绑定为设备，请先在后台解绑后再重新扫码")
            cursor.execute(
                """INSERT INTO wechat_bot_devices (device_id, ilink_bot_id, owner_user_id, identity_label,
                       person_id, allowed_permissions_json, created_by_admin_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (device_id, ilink_bot_id, owner_user_id, validate_label(label), person_id,
                 None if allowed is None else json.dumps(list(allowed)), int(created_by)),
            )

    def list_devices(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute("""SELECT d.device_id,d.identity_label,d.active,d.bound_at,d.revoked_at,
                d.allowed_permissions_json,d.connection_state,d.last_message_at,d.owner_user_id,
                p.person_id,p.name AS person_name,p.is_active,p.access_role_id,
                u.id AS account_id,u.username,u.is_active AS account_active,u.control_plane_role,
                r.name AS role_name,r.is_active AS role_active
                FROM wechat_bot_devices d
                JOIN persons p ON p.person_id=d.person_id
                LEFT JOIN admin_users u ON u.person_id=p.person_id
                LEFT JOIN access_roles r ON r.role_id=p.access_role_id
                ORDER BY d.active DESC,d.bound_at DESC,d.device_id""")
            rows = list(cursor.fetchall())
        for row in rows:
            owner = str(row.pop("owner_user_id") or "")
            row["owner_hint"] = owner[:6] + "…" if owner else ""  # never show the full WeChat id
            row["allowed_permissions"] = _decode_permissions(row.pop("allowed_permissions_json"))
            annotate_person(row)
            row["effective"] = bool(row["active"] and row["effective"])
            row["connection_label"] = CONNECTION_STATES.get(row["connection_state"], row["connection_state"])
        return rows

    def active_devices(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute("SELECT device_id,ilink_bot_id,owner_user_id FROM wechat_bot_devices WHERE active=TRUE")
            return list(cursor.fetchall())

    def get_active(self, device_id: str) -> dict | None:
        with self.cursor() as cursor:
            cursor.execute("SELECT device_id,ilink_bot_id,owner_user_id FROM wechat_bot_devices WHERE device_id=%s AND active=TRUE",
                           (device_id,))
            return cursor.fetchone()

    def update_device(self, device_id: str, *, label: str, person_id=None, permissions=None) -> None:
        allowed = validate_device_permissions(permissions)
        label = validate_label(label)
        with self.cursor() as cursor:
            cursor.execute("SELECT device_id FROM wechat_bot_devices WHERE device_id=%s AND active=TRUE FOR UPDATE", (device_id,))
            if not cursor.fetchone():
                raise ValueError("微信设备不存在或已经解绑")
            IdentityRepository.check_person(cursor, person_id)
            cursor.execute(
                """UPDATE wechat_bot_devices SET identity_label=%s,person_id=%s,
                       allowed_permissions_json=%s,updated_at=NOW(6) WHERE device_id=%s""",
                (label, person_id,
                 None if allowed is None else json.dumps(list(allowed)), device_id),
            )

    def revoke(self, device_id: str) -> bool:
        with self.cursor() as cursor:
            cursor.execute(
                """UPDATE wechat_bot_devices SET active=FALSE,connection_state='stopped',revoked_at=NOW(6),updated_at=NOW(6)
                   WHERE device_id=%s AND active=TRUE""", (device_id,))
            return int(getattr(cursor, "rowcount", 0) or 0) == 1

    def record_connection(self, device_id: str, state: str, *, message_received: bool = False) -> None:
        if state not in CONNECTION_STATES:
            raise ValueError("unknown connection state")
        with self.cursor() as cursor:
            cursor.execute(
                f"""UPDATE wechat_bot_devices SET connection_state=%s{",last_message_at=NOW(6)" if message_received else ""}
                    WHERE device_id=%s AND active=TRUE""", (state, device_id))
