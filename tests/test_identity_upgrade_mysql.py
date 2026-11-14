"""Upgrade populated pre-identity accounts without losing their Feishu binding."""
import json
import os
from unittest.mock import patch
from uuid import uuid4

import pytest

from shared.identity_repository import IdentityRepository
from tests.identity_support import role_sets
from tests.mysql_migrated_database import database as database
from tests.test_feishu_bindings_mysql import pytestmark
from tests.mysql_database_cleanup import drop_test_database


def test_existing_accounts_and_bindings_keep_their_actual_owner_after_upgrade(database):
    name = f"test_identity_upgrade_{uuid4().hex}"
    with database._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        database._apply_through(name, "017")
        database._seed_required_project_resources(name)
        database._apply_through(name, "046")
        def connect():
            return database.pymysql.connect(host=database.host, port=database.port, user=database.user,
                password=database.password, database=name, charset="utf8mb4",
                cursorclass=database.pymysql.cursors.DictCursor, autocommit=False)
        with connect() as connection, connection.cursor() as cursor:
            accounts = {}
            for role in ("admin", "super_admin"):
                cursor.execute("INSERT INTO admin_users (username,password_hash,display_name,is_active,role,control_plane_role,created_at,updated_at) VALUES (%s,'synthetic-only',%s,1,%s,%s,NOW(6),NOW(6))",
                    (role,role,role,role))
                accounts[role] = cursor.lastrowid
            binding_id = str(uuid4())
            cursor.execute("INSERT INTO feishu_admin_bindings (binding_id,admin_user_id,open_id,last_chat_id,notifications_enabled,active) VALUES (%s,%s,'ou-before-upgrade','isolated-chat',TRUE,TRUE)",
                (binding_id, accounts["super_admin"]))
            connection.commit()
        with patch.dict(os.environ, database._environment(name), clear=False):
            database.runner.run(check_only=False)
            database.runner.run(check_only=True)
        identities = IdentityRepository(connect)
        assert identities.account(accounts["super_admin"]).super_admin
        assert identities.feishu("ou-before-upgrade").super_admin
        ordinary = identities.account(accounts["admin"])
        assert ordinary.active and ordinary.allows("business.query") and not ordinary.super_admin
        before = identities.list_bindings()
        assert len(before) == 1 and before[0]["binding_id"] == binding_id
        assert before[0]["account_id"] == accounts["super_admin"]
        # The binding can now move to another person through the real repository,
        # without keeping the old creator's super-admin permissions attached to it.
        role_id = identities.save_role(name="查询",**role_sets("business.query"),active=True)
        identities.update_binding(binding_id,person_id=identities.save_person(name="原飞书账号",role_id=role_id),label="原飞书账号")
        assert identities.feishu("ou-before-upgrade").allows("business.query")
        assert not identities.feishu("ou-before-upgrade").super_admin
    finally:
        with database._server_connection() as connection, connection.cursor() as cursor:
            drop_test_database(cursor, name)


def test_bindings_and_devices_become_people_after_upgrade(database):
    """081 data: role-only and account-backed Feishu bindings, codes and WeChat devices."""
    name = f"test_person_upgrade_{uuid4().hex}"
    with database._server_connection() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    try:
        database._apply_through(name, "017")
        database._seed_required_project_resources(name)
        # Later migrations bind the entity databases, so the real runner takes it to 081.
        all_migrations = database.runner.discover_migrations()
        with patch.dict(os.environ, database._environment(name), clear=False), patch.object(
                database.runner, "discover_migrations", lambda: [m for m in all_migrations if m[0] <= "081"]):
            database.runner.run(check_only=False)

        def connect():
            return database.pymysql.connect(host=database.host, port=database.port, user=database.user,
                password=database.password, database=name, charset="utf8mb4",
                cursorclass=database.pymysql.cursors.DictCursor, autocommit=False)
        role_id = str(uuid4())
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO access_roles (role_id,name,permissions_json) VALUES (%s,'司机',JSON_ARRAY('business.query'))", (role_id,))
            everything = str(uuid4())
            cursor.execute("INSERT INTO access_roles (role_id,name,permissions_json) VALUES (%s,'全部',JSON_ARRAY("
                           "'business.query','waybill.write','customer_service','receipt.share','dispatch','line_haul',"
                           "'warehouse.monitor','plugins.execute','finance.read','finance.manage','accounts.manage','ai.chat'))", (everything,))
            reader = str(uuid4())
            cursor.execute("INSERT INTO access_roles (role_id,name,permissions_json) VALUES (%s,'只看财务',JSON_ARRAY('finance.read'))", (reader,))
            accounts = {}
            for role, display, access in (("super_admin", "老板", None), ("admin", "", role_id), ("admin", "停用", role_id)):
                cursor.execute("INSERT INTO admin_users (username,password_hash,display_name,is_active,role,control_plane_role,created_at,updated_at,access_role_id) "
                               "VALUES (%s,'synthetic-only',%s,%s,%s,%s,NOW(6),NOW(6),%s)",
                               (f"user-{len(accounts)}", display, 0 if display == "停用" else 1, role, role, access))
                accounts[display or "clerk"] = cursor.lastrowid
            boss, clerk, disabled = accounts["老板"], accounts["clerk"], accounts["停用"]
            for open_id, label, access, account, active in (
                    ("ou-role-only", "仓库小张", role_id, None, True), ("ou-no-label-9876", "", role_id, None, True),
                    ("ou-boss", "", None, boss, True), ("ou-disabled", "", None, disabled, True),
                    ("ou-revoked", "旧绑定", role_id, None, False)):
                cursor.execute("INSERT INTO feishu_admin_bindings (binding_id,admin_user_id,open_id,last_chat_id,active,access_role_id,identity_account_id,identity_label) "
                               "VALUES (%s,%s,%s,'chat',%s,%s,%s,%s)", (str(uuid4()), boss, open_id, active, access, account, label))
            codes = {}
            for key, access, account in (("role", role_id, None), ("account", None, clerk)):
                codes[key] = str(uuid4())
                cursor.execute("INSERT INTO feishu_admin_binding_challenges (challenge_id,admin_user_id,code_sha256,expires_at,access_role_id,identity_account_id) "
                               "VALUES (%s,%s,%s,UTC_TIMESTAMP(6)+INTERVAL 10 MINUTE,%s,%s)", (codes[key], boss, uuid4().hex * 2, access, account))
            devices = {}
            for key, access, account, limits in (("role", role_id, None, None), ("clerk", None, clerk, '["business.query","dispatch","ai.chat"]')):
                devices[key] = str(uuid4())
                cursor.execute("INSERT INTO wechat_bot_devices (device_id,ilink_bot_id,owner_user_id,identity_label,access_role_id,identity_account_id,created_by_admin_id,allowed_permissions_json) "
                               "VALUES (%s,%s,'owner@im.wechat',%s,%s,%s,%s,%s)", (devices[key], f"bot-{key}", "司机老王" if access else "", access, account, boss, limits))
            connection.commit()
        with patch.dict(os.environ, database._environment(name), clear=False):
            database.runner.run(check_only=False)
            database.runner.run(check_only=True)
        identities = IdentityRepository(connect)
        # Accounts keep their access; each became exactly one person.
        assert identities.account(boss).super_admin
        assert identities.account(clerk).allows("business.query") and not identities.account(clerk).super_admin
        assert not identities.account(disabled).active
        persons = {row["person_id"]: row for row in identities.list_persons()}
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,person_id FROM admin_users")
            person_of = {row["id"]: row["person_id"] for row in cursor.fetchall()}
            cursor.execute("SELECT challenge_id,person_id,used_at FROM feishu_admin_binding_challenges")
            challenges = {row["challenge_id"]: row for row in cursor.fetchall()}
        assert len(set(person_of.values())) == 3
        assert persons[person_of[boss]]["name"] == "老板" and persons[person_of[clerk]]["name"] == "user-1"
        assert persons[person_of[clerk]]["access_role_id"] == role_id
        # Bindings that inherited an account joined that account's person.
        assert identities.feishu("ou-boss").super_admin and identities.feishu("ou-boss").person_id == person_of[boss]
        assert not identities.feishu("ou-disabled").active
        # Role-only bindings became chat-only people named after their label.
        role_only = identities.feishu("ou-role-only")
        assert role_only.allows("business.query") and persons[role_only.person_id]["name"] == "仓库小张"
        assert persons[role_only.person_id]["account_id"] is None
        assert persons[identities.feishu("ou-no-label-9876").person_id]["name"] == "飞书用户 9876"
        revoked = next(row for row in identities.list_bindings() if row["identity_label"] == "旧绑定")
        assert not revoked["active"] and not persons[revoked["person_id"]]["is_active"]
        # A pending account code still targets that person; a role-only code is retired.
        assert challenges[codes["account"]]["person_id"] == person_of[clerk] and challenges[codes["account"]]["used_at"] is None
        assert challenges[codes["role"]]["person_id"] is None and challenges[codes["role"]]["used_at"] is not None
        # WeChat devices follow the same mapping.
        assert identities.wechat_device(devices["clerk"]).person_id == person_of[clerk]
        device_person = identities.wechat_device(devices["role"]).person_id
        assert persons[device_person]["name"] == "司机老王" and identities.wechat_device(devices["role"]).allows("business.query")
        # 083: each old list became a Console set and an agent set with the same reach.
        roles = {row["name"]: row for row in identities.list_roles()}
        assert roles["司机"]["console"] == {"business.query": "view"} and roles["司机"]["agent"] == {"business.query": "direct"}
        assert roles["只看财务"]["console"] == {"finance": "view"} and roles["只看财务"]["agent"] == {"finance.read": "private"}
        assert roles["全部"]["console"] == {
            "accounts.manage": "operate", "ai_assistant": "view", "business.query": "view", "customer_service": "operate",
            "dispatch": "view", "finance": "operate", "line_haul": "operate", "plugins.execute": "operate",
            "receipt.share": "operate", "warehouse.monitor": "view", "waybill.write": "operate"}
        assert roles["全部"]["agent"] == {
            "accounts.manage": "direct", "ai.chat": "direct", "business.query": "direct", "customer_service": "confirm",
            "finance.manage": "confirm", "finance.read": "private", "plugins.execute": "confirm", "warehouse.monitor": "direct"}
        # Device limits keep only agent capabilities.
        with connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT allowed_permissions_json FROM wechat_bot_devices WHERE device_id=%s", (devices["clerk"],))
            assert sorted(json.loads(cursor.fetchone()["allowed_permissions_json"])) == ["ai.chat", "business.query"]
        assert identities.wechat_device(devices["clerk"]).allows("business.query")
    finally:
        with database._server_connection() as connection, connection.cursor() as cursor:
            drop_test_database(cursor, name)
