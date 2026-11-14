"""Identity persistence using the caller's transaction/connection boundary."""
from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

from shared.identity_permissions import (
    AGENT,
    CONSOLE,
    IdentityAccess,
    access_from_row,
    validate_agent_permissions,
    validate_console_permissions,
)
from shared.runtime_repositories import _connection, _cursor
from shared.wechat_bot import (
    is_wechat_actor_id,
    parse_wechat_actor_id,
    restrict_access,
    validate_device_permissions,
)


# Permission row of one person; a person with a login account applies only
# while that account is active, and is a super admin through that account.
_PERSON_ACCESS = """p.person_id, (p.is_active AND COALESCE(u.is_active, 1)) AS is_active,
    COALESCE(u.control_plane_role, 'admin') AS control_plane_role, p.access_role_id,
    r.name AS role_name, r.is_active AS role_active, r.console_permissions_json, r.agent_permissions_json"""
_PERSON_JOINS = """JOIN persons p ON p.person_id={column}
    LEFT JOIN admin_users u ON u.person_id=p.person_id
    LEFT JOIN access_roles r ON r.role_id=p.access_role_id"""


class IdentityRepository:
    def __init__(self, connect, *, cursor_factory=None):
        self._connect = connect
        self._cursor_factory = cursor_factory

    @contextmanager
    def cursor(self, *, connection=None):
        if connection is not None:
            with _cursor(connection, self._cursor_factory) as cursor:
                yield cursor
            return
        with _connection(self._connect) as connection:
            connection.autocommit(False)
            try:
                with _cursor(connection, self._cursor_factory) as cursor:
                    yield cursor
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def account(self, account_id: int, *, connection=None) -> IdentityAccess:
        with self.cursor(connection=connection) as cursor:
            cursor.execute(f"""SELECT {_PERSON_ACCESS} FROM admin_users a {_PERSON_JOINS.format(column="a.person_id")}
                WHERE a.id=%s""", (account_id,))
            return access_from_row(cursor.fetchone())

    def person(self, person_id: str) -> IdentityAccess:
        with self.cursor() as cursor:
            cursor.execute(f"SELECT {_PERSON_ACCESS} FROM persons x {_PERSON_JOINS.format(column='x.person_id')} WHERE x.person_id=%s",
                           (person_id,))
            return access_from_row(cursor.fetchone())

    def feishu(self, open_id: str) -> IdentityAccess:
        """Bound chat sender: a Feishu open_id or a WeChat device actor id."""
        if is_wechat_actor_id(open_id):
            device_id = parse_wechat_actor_id(open_id)
            return self.wechat_device(device_id) if device_id else IdentityAccess(channel=AGENT)
        with self.cursor() as cursor:
            cursor.execute(f"""SELECT {_PERSON_ACCESS} FROM feishu_admin_bindings b {_PERSON_JOINS.format(column="b.person_id")}
                WHERE b.open_id=%s AND b.active=TRUE""", (open_id,))
            return access_from_row(cursor.fetchone(), channel=AGENT)

    def wechat_device(self, device_id: str) -> IdentityAccess:
        """Live identity of an active WeChat device, narrowed by its own limits."""
        with self.cursor() as cursor:
            cursor.execute(f"""SELECT d.allowed_permissions_json, {_PERSON_ACCESS}
                FROM wechat_bot_devices d {_PERSON_JOINS.format(column="d.person_id")}
                WHERE d.device_id=%s AND d.active=TRUE""", (device_id,))
            row = cursor.fetchone()
        if not row:
            return IdentityAccess(channel=AGENT)
        raw = row.get("allowed_permissions_json")
        allowed = validate_device_permissions(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)
        return restrict_access(access_from_row(row), allowed)

    def for_actor(self, actor, *, connection=None) -> IdentityAccess:
        """Live access of a person, in the channel the request came through.

        Console pages use the Console set; Feishu, WeChat and the Console AI
        assistant (an actor marked ``channel="agent"``) use the agent set.
        """
        kind = str(getattr(getattr(actor, "actor_type", None), "value", ""))
        auth = getattr(actor, "authenticated_by", "")
        channel = getattr(actor, "channel", "") or (CONSOLE if kind == "console_admin" else AGENT)
        if kind == "console_admin" and auth == "mysql_admin_session":
            try:
                account_id = int(actor.actor_id)
            except (TypeError, ValueError):
                return IdentityAccess(channel=channel)
            return self.account(account_id, connection=connection).for_channel(channel)
        if kind == "feishu_user" and auth in {"feishu_admin_binding", "feishu_verified_event"}:
            return self.feishu(actor.actor_id)  # always the agent set
        return IdentityAccess(channel=channel)

    def allows(self, actor, permission: str, *, connection=None, read: bool = False) -> bool:
        return self.for_actor(actor, connection=connection).allows(permission, read=read)

    def require(self, actor, permission: str) -> None:
        if not self.allows(actor, permission):
            raise PermissionError("当前身份没有此操作权限，请在系统管理中检查账号绑定的身份。")

    def list_roles(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute("""SELECT role_id,name,console_permissions_json,agent_permissions_json,is_active
                FROM access_roles ORDER BY created_at,role_id""")
            rows = list(cursor.fetchall())
        for row in rows:
            row["console"] = validate_console_permissions(json.loads(row.pop("console_permissions_json")))
            row["agent"] = validate_agent_permissions(json.loads(row.pop("agent_permissions_json")))
        return rows

    def save_role(self, *, name: str, console, agent, active: bool, role_id: str | None = None) -> str:
        name = name.strip()
        if not name or len(name) > 80 or name == "超级管理员":
            raise ValueError("身份名称需为 1–80 个字符，不能使用内置超级管理员名称")
        console = json.dumps(validate_console_permissions(console))
        agent = json.dumps(validate_agent_permissions(agent))
        with self.cursor() as cursor:
            cursor.execute("SELECT role_id FROM access_roles WHERE name=%s", (name,))
            conflict = cursor.fetchone()
            if conflict and conflict["role_id"] != role_id:
                raise ValueError("身份名称已存在")
            if role_id:
                cursor.execute("SELECT role_id FROM access_roles WHERE role_id=%s FOR UPDATE", (role_id,))
                if not cursor.fetchone():
                    raise ValueError("身份不存在")
                cursor.execute("""UPDATE access_roles SET name=%s,console_permissions_json=%s,agent_permissions_json=%s,
                    is_active=%s,updated_at=NOW(6) WHERE role_id=%s""", (name, console, agent, active, role_id))
            else:
                role_id = str(uuid4())
                cursor.execute("""INSERT INTO access_roles (role_id,name,console_permissions_json,agent_permissions_json,is_active)
                    VALUES (%s,%s,%s,%s,%s)""", (role_id, name, console, agent, active))
        return role_id

    @staticmethod
    def check_person(cursor, person_id) -> None:
        """Lock an enabled person as the target of a binding or device."""
        if not isinstance(person_id, str) or not person_id.strip():
            raise ValueError("请选择人员")
        cursor.execute("SELECT person_id FROM persons WHERE person_id=%s AND is_active=TRUE FOR UPDATE", (person_id.strip(),))
        if not cursor.fetchone():
            raise ValueError("所选人员不存在或已停用")

    @staticmethod
    def _check_role(cursor, role_id) -> None:
        if role_id is None:
            return
        cursor.execute("SELECT role_id FROM access_roles WHERE role_id=%s AND is_active=TRUE FOR UPDATE", (role_id,))
        if not cursor.fetchone():
            raise ValueError("所选身份不存在或已停用")

    def create_person(self, cursor, *, name: str, role_id: str | None) -> str:
        """Insert a person inside the caller's transaction."""
        name = _person_name(name)
        self._check_role(cursor, role_id)
        person_id = str(uuid4())
        cursor.execute("INSERT INTO persons (person_id,name,access_role_id) VALUES (%s,%s,%s)", (person_id, name, role_id))
        return person_id

    def save_person(self, *, name: str, role_id: str | None, person_id: str | None = None, active: bool | None = None) -> str:
        with self.cursor() as cursor:
            if not person_id:
                return self.create_person(cursor, name=name, role_id=role_id)
            cursor.execute("""SELECT p.person_id,u.id AS account_id FROM persons p
                LEFT JOIN admin_users u ON u.person_id=p.person_id WHERE p.person_id=%s FOR UPDATE""", (person_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError("人员不存在")
            if active is not None and row["account_id"] is not None:
                raise ValueError("该人员有后台账号，请在后台账号中启用或停用")
            self._check_role(cursor, role_id)
            cursor.execute("UPDATE persons SET name=%s,access_role_id=%s,is_active=COALESCE(%s,is_active),updated_at=NOW(6) WHERE person_id=%s",
                           (_person_name(name), role_id, active, person_id))
            return person_id

    def list_persons(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute("""SELECT p.person_id,p.name,p.access_role_id,p.is_active,p.created_at,
                r.name AS role_name,r.is_active AS role_active,
                u.id AS account_id,u.username,u.is_active AS account_active,u.control_plane_role,
                (SELECT COUNT(*) FROM feishu_admin_bindings b WHERE b.person_id=p.person_id AND b.active=TRUE) AS feishu_count,
                (SELECT COUNT(*) FROM wechat_bot_devices d WHERE d.person_id=p.person_id AND d.active=TRUE) AS wechat_count
                FROM persons p
                LEFT JOIN admin_users u ON u.person_id=p.person_id
                LEFT JOIN access_roles r ON r.role_id=p.access_role_id
                ORDER BY p.is_active DESC,p.created_at,p.person_id""")
            rows = list(cursor.fetchall())
        for row in rows:
            annotate_person(row)
        return rows

    def assign_account(self, account_id: int, role_id: str) -> None:
        """Set the identity of the person who owns this Console account."""
        with self.cursor() as cursor:
            self._check_role(cursor, role_id)
            cursor.execute("SELECT person_id,control_plane_role FROM admin_users WHERE id=%s FOR UPDATE", (account_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError("账号不存在")
            if row["control_plane_role"] == "super_admin":
                raise ValueError("超级管理员使用内置全部权限，无需分配身份")
            cursor.execute("UPDATE persons SET access_role_id=%s,updated_at=NOW(6) WHERE person_id=%s", (role_id, row["person_id"]))

    def list_bindings(self) -> list[dict]:
        with self.cursor() as cursor:
            cursor.execute("""SELECT b.binding_id,b.identity_label,b.active,b.bound_at,b.revoked_at,
                CONCAT(LEFT(b.open_id,6),'…',RIGHT(b.open_id,4)) AS identity_hint,
                p.person_id,p.name AS person_name,p.is_active,p.access_role_id,
                u.id AS account_id,u.username,u.is_active AS account_active,u.control_plane_role,
                r.name AS role_name,r.is_active AS role_active
                FROM feishu_admin_bindings b
                JOIN persons p ON p.person_id=b.person_id
                LEFT JOIN admin_users u ON u.person_id=p.person_id
                LEFT JOIN access_roles r ON r.role_id=p.access_role_id
                ORDER BY b.active DESC,b.bound_at DESC,b.binding_id""")
            rows = list(cursor.fetchall())
        for row in rows:
            annotate_person(row)
            row["effective"] = bool(row["active"] and row["effective"])
        return rows

    def update_binding(self, binding_id: str, *, revoke=False, person_id=None, label="") -> None:
        if len(label.strip()) > 80:
            raise ValueError("飞书账号备注不能超过 80 个字符")
        with self.cursor() as cursor:
            cursor.execute("SELECT binding_id FROM feishu_admin_bindings WHERE binding_id=%s AND active=TRUE FOR UPDATE", (binding_id,))
            if not cursor.fetchone():
                raise ValueError("绑定不存在或已经解绑")
            if revoke:
                cursor.execute("UPDATE feishu_admin_bindings SET active=FALSE,revoked_at=NOW(6),updated_at=NOW(6) WHERE binding_id=%s", (binding_id,))
            else:
                self.check_person(cursor, person_id)
                cursor.execute("UPDATE feishu_admin_bindings SET person_id=%s,identity_label=%s,updated_at=NOW(6) WHERE binding_id=%s",
                               (person_id, label.strip(), binding_id))


def _person_name(name) -> str:
    name = str(name or "").strip()
    if not name or len(name) > 80:
        raise ValueError("人员姓名需为 1–80 个字符")
    return name


def annotate_person(row: dict) -> None:
    """Display role and whether the person's permissions currently apply."""
    super_admin = row.get("control_plane_role") == "super_admin"
    row["super_admin"] = super_admin
    row["role_name"] = "超级管理员" if super_admin else row.get("role_name") or "未分配身份"
    row["effective"] = bool(row["is_active"] and (row.get("account_id") is None or row.get("account_active"))
                            and (super_admin or row.get("role_active")))
