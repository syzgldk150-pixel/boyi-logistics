"""Upgrade populated pre-identity accounts without losing their Feishu binding."""
import os
from unittest.mock import patch
from uuid import uuid4

import pytest

from shared.identity_repository import IdentityRepository
from tests.test_manual_unknown_write_mysql import database as database
from tests.test_feishu_bindings_mysql import pytestmark


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
        assert before[0]["identity_account_id"] == accounts["super_admin"]
        # The identity can now be changed through the real repository, without
        # leaving the old creator's super-admin permissions attached to it.
        role_id = identities.save_role(name="查询",permissions=["business.query"],active=True)
        identities.update_binding(binding_id,role_id=role_id,label="原飞书账号")
        assert identities.feishu("ou-before-upgrade").allows("business.query")
        assert not identities.feishu("ou-before-upgrade").super_admin
    finally:
        with database._server_connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{name}`")
