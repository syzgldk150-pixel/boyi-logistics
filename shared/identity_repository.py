"""Identity persistence using the caller's transaction/connection boundary."""
from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

from shared.identity_permissions import IdentityAccess, access_from_row, validate_permissions
from shared.runtime_repositories import _connection, _cursor


class IdentityRepository:
    def __init__(self, connect, *, cursor_factory=None):
        self._connect = connect
        self._cursor_factory = cursor_factory

    @contextmanager
    def cursor(self):
        with _connection(self._connect) as connection:
            connection.autocommit(False)
            try:
                with _cursor(connection, self._cursor_factory) as cursor:
                    yield cursor
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def account(self, account_id: int) -> IdentityAccess:
        with self.cursor() as cursor:
            cursor.execute("""SELECT u.is_active, u.control_plane_role, u.access_role_id,
                r.name AS role_name, r.is_active AS role_active, r.permissions_json
                FROM admin_users u LEFT JOIN access_roles r ON r.role_id=u.access_role_id WHERE u.id=%s""", (account_id,))
            return access_from_row(cursor.fetchone())

    def feishu(self, open_id: str) -> IdentityAccess:
        with self.cursor() as cursor:
            cursor.execute("""SELECT b.identity_account_id,
                IF(b.identity_account_id IS NULL, 1, u.is_active) AS is_active,
                IF(b.identity_account_id IS NULL, 'admin', u.control_plane_role) AS control_plane_role,
                COALESCE(b.access_role_id, u.access_role_id) AS access_role_id,
                r.name AS role_name, r.is_active AS role_active, r.permissions_json
                FROM feishu_admin_bindings b
                LEFT JOIN admin_users u ON u.id=b.identity_account_id
                LEFT JOIN access_roles r ON r.role_id=COALESCE(b.access_role_id,u.access_role_id)
                WHERE b.open_id=%s AND b.active=TRUE""", (open_id,))
            return access_from_row(cursor.fetchone())

    def for_actor(self, actor) -> IdentityAccess:
        kind = str(getattr(getattr(actor, "actor_type", None), "value", ""))
        auth = getattr(actor, "authenticated_by", "")
        if kind == "console_admin" and auth == "mysql_admin_session":
            return self.account(int(actor.actor_id))
        if kind == "feishu_user" and auth in {"feishu_admin_binding", "feishu_verified_event"}:
            return self.feishu(actor.actor_id)
        return IdentityAccess()

    def allows(self, actor, permission: str) -> bool:
        return self.for_actor(actor).allows(permission)

    def require(self, actor, permission: str) -> None:
        if not self.allows(actor, permission):
            raise PermissionError("当前身份没有此操作权限，请在系统管理中检查账号绑定的身份。")

    def list_roles(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute("SELECT role_id,name,permissions_json,is_active FROM access_roles ORDER BY created_at,role_id")
            rows = list(cursor.fetchall())
        for row in rows:
            row["permissions"] = list(validate_permissions(json.loads(row.pop("permissions_json"))))
        return rows

    def save_role(self, *, name: str, permissions, active: bool, role_id: str | None = None) -> str:
        name = name.strip()
        if not name or len(name) > 80 or name == "超级管理员":
            raise ValueError("身份名称需为 1–80 个字符，不能使用内置超级管理员名称")
        permissions = validate_permissions(permissions)
        with self.cursor() as cursor:
            cursor.execute("SELECT role_id FROM access_roles WHERE name=%s", (name,))
            conflict = cursor.fetchone()
            if conflict and conflict["role_id"] != role_id:
                raise ValueError("身份名称已存在")
            if role_id:
                cursor.execute("SELECT role_id FROM access_roles WHERE role_id=%s FOR UPDATE", (role_id,))
                if not cursor.fetchone():
                    raise ValueError("身份不存在")
                cursor.execute("UPDATE access_roles SET name=%s,permissions_json=%s,is_active=%s,updated_at=NOW(6) WHERE role_id=%s",
                               (name, json.dumps(permissions), active, role_id))
            else:
                role_id = str(uuid4())
                cursor.execute("INSERT INTO access_roles (role_id,name,permissions_json,is_active) VALUES (%s,%s,%s,%s)",
                               (role_id, name, json.dumps(permissions), active))
        return role_id

    @staticmethod
    def check_target(cursor, *, role_id=None, account_id=None):
        if bool(role_id) == bool(account_id):
            raise ValueError("请选择一个身份或一个后台账号")
        if role_id:
            cursor.execute("SELECT role_id FROM access_roles WHERE role_id=%s AND is_active=TRUE FOR UPDATE", (role_id,))
        else:
            cursor.execute("SELECT id FROM admin_users WHERE id=%s AND is_active=TRUE FOR UPDATE", (int(account_id),))
        if not cursor.fetchone():
            raise ValueError("所选身份或账号不存在或已停用")

    def assign_account(self, account_id: int, role_id: str) -> None:
        with self.cursor() as cursor:
            self.check_target(cursor, role_id=role_id)
            cursor.execute("SELECT control_plane_role FROM admin_users WHERE id=%s FOR UPDATE", (account_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError("账号不存在")
            if row["control_plane_role"] == "super_admin":
                raise ValueError("超级管理员使用内置全部权限，无需分配身份")
            cursor.execute("UPDATE admin_users SET access_role_id=%s,updated_at=NOW(6) WHERE id=%s", (role_id,account_id))

    def list_bindings(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute("""SELECT b.binding_id,b.identity_label,b.active,b.bound_at,b.revoked_at,
                CONCAT(LEFT(b.open_id,6),'…',RIGHT(b.open_id,4)) AS identity_hint,
                b.access_role_id,b.identity_account_id,u.username,u.is_active AS account_active,
                u.control_plane_role, r.name AS role_name,r.is_active AS role_active
                FROM feishu_admin_bindings b
                LEFT JOIN admin_users u ON u.id=b.identity_account_id
                LEFT JOIN access_roles r ON r.role_id=COALESCE(b.access_role_id,u.access_role_id)
                ORDER BY b.active DESC,b.bound_at DESC,b.binding_id""")
            rows = list(cursor.fetchall())
        for row in rows:
            row["role_name"] = "超级管理员" if row["control_plane_role"] == "super_admin" else row["role_name"] or "未分配身份"
            row["effective"] = bool(row["active"] and (row["identity_account_id"] is None or row["account_active"])
                                    and (row["control_plane_role"] == "super_admin" or row["role_active"]))
        return rows

    def update_binding(self, binding_id: str, *, revoke=False, role_id=None, account_id=None, label="") -> None:
        if len(label.strip()) > 80:
            raise ValueError("飞书账号备注不能超过 80 个字符")
        with self.cursor() as cursor:
            cursor.execute("SELECT binding_id FROM feishu_admin_bindings WHERE binding_id=%s AND active=TRUE FOR UPDATE", (binding_id,))
            if not cursor.fetchone():
                raise ValueError("绑定不存在或已经解绑")
            if revoke:
                cursor.execute("UPDATE feishu_admin_bindings SET active=FALSE,revoked_at=NOW(6),updated_at=NOW(6) WHERE binding_id=%s", (binding_id,))
            else:
                self.check_target(cursor, role_id=role_id, account_id=account_id)
                cursor.execute("UPDATE feishu_admin_bindings SET access_role_id=%s,identity_account_id=%s,identity_label=%s,updated_at=NOW(6) WHERE binding_id=%s",
                               (role_id,account_id,label.strip(),binding_id))
