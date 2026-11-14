"""WeChat devices against an isolated MySQL with every migration applied (081 included)."""
from uuid import uuid4

import pytest

from agent.orchestration.feishu_binding_service import FeishuBindingService
from agent.orchestration.models import Actor, ActorType
from shared.identity_repository import IdentityRepository
from tests.identity_support import role_sets
from shared.wechat_bot import wechat_actor_id
from shared.wechat_bot_repository import WeChatDeviceConflict, WeChatDeviceRepository
from tests.mysql_migrated_database import database as database
from tests.test_feishu_bindings_mysql import binding_repository as binding_repository, person_of, pytestmark, seed_admin

__all__ = ["database", "binding_repository", "pytestmark"]


def _device(devices, admin_id, *, person_id, permissions=None, bot=None):
    device_id = str(uuid4())
    devices.create_device(device_id=device_id, ilink_bot_id=bot or f"bot-{uuid4().hex}@im.bot",
                          owner_user_id=f"owner-{uuid4().hex}@im.wechat", label="测试设备",
                          person_id=person_id, permissions=permissions, created_by=admin_id)
    return device_id


def test_device_identity_limits_edits_and_revocation_take_effect_live(binding_repository):
    connect = binding_repository._connection_factory
    identities = IdentityRepository(connect)
    devices = WeChatDeviceRepository(connect)
    admin_id = seed_admin(binding_repository)
    resolver = FeishuBindingService(binding_repository, identity_access=identities)
    role_id = identities.save_role(name=f"司机-{uuid4().hex}", **role_sets("business.query", "plugins.execute", "ai.chat"),
                                   active=True)

    driver = identities.save_person(name="司机老王", role_id=role_id)
    admin_person = person_of(binding_repository, admin_id)
    limited = _device(devices, admin_id, person_id=driver, permissions=["business.query", "finance.read"])
    inherited = _device(devices, admin_id, person_id=admin_person)  # super administrator, no device limit
    capped_admin = _device(devices, admin_id, person_id=admin_person, permissions=["business.query"])

    actor = resolver.resolve_actor(wechat_actor_id(limited))
    assert actor.actor_type is ActorType.FEISHU_USER and actor.roles == ("admin",)
    assert actor.authenticated_by == "feishu_admin_binding"
    assert identities.allows(actor, "business.query")
    assert not identities.allows(actor, "plugins.execute")  # narrowed by the device
    assert not identities.allows(actor, "finance.read")      # never wider than the identity
    assert resolver.resolve_actor(wechat_actor_id(inherited)).roles == ("admin", "super_admin")
    capped = resolver.resolve_actor(wechat_actor_id(capped_admin))
    assert capped.roles == ("admin",) and not identities.allows(capped, "super_admin")

    devices.update_device(limited, label="改名", person_id=driver, permissions=None)
    assert identities.allows(resolver.resolve_actor(wechat_actor_id(limited)), "plugins.execute")
    identities.save_role(role_id=role_id, name=f"司机-{uuid4().hex}", **role_sets("business.query"), active=False)
    assert resolver.resolve_actor(wechat_actor_id(limited)).roles == ()

    assert devices.revoke(inherited) and not devices.revoke(inherited)
    assert resolver.resolve_actor(wechat_actor_id(inherited)).roles == ()
    with pytest.raises(ValueError, match="已经解绑"):
        devices.update_device(inherited, label="x", person_id=admin_person)

    listed = {row["device_id"]: row for row in devices.list_devices()}
    assert listed[limited]["allowed_permissions"] is None and not listed[limited]["effective"]
    assert listed[capped_admin]["allowed_permissions"] == ["business.query"]
    assert listed[capped_admin]["role_name"] == "超级管理员" and listed[capped_admin]["effective"]
    assert listed[limited]["person_name"] == "司机老王"
    assert not listed[inherited]["active"]
    assert "owner-" in listed[limited]["owner_hint"] and "@im.wechat" not in listed[limited]["owner_hint"]
    active = {row["device_id"] for row in devices.active_devices()}
    assert {limited, capped_admin} <= active and inherited not in active


def test_one_active_device_per_bot_and_target_must_exist(binding_repository):
    connect = binding_repository._connection_factory
    devices = WeChatDeviceRepository(connect)
    admin_id = seed_admin(binding_repository)
    bot = f"bot-{uuid4().hex}@im.bot"
    person = person_of(binding_repository, admin_id)
    first = _device(devices, admin_id, person_id=person, bot=bot)
    with pytest.raises(WeChatDeviceConflict):
        _device(devices, admin_id, person_id=person, bot=bot)
    devices.revoke(first)
    _device(devices, admin_id, person_id=person, bot=bot)  # rebinding after revocation
    with pytest.raises(ValueError):
        _device(devices, admin_id, person_id=str(uuid4()))
    with pytest.raises(ValueError):
        _device(devices, admin_id, person_id=person, permissions=["unknown"])


def test_connection_state_is_recorded_only_for_active_devices(binding_repository):
    connect = binding_repository._connection_factory
    devices = WeChatDeviceRepository(connect)
    admin_id = seed_admin(binding_repository)
    device_id = _device(devices, admin_id, person_id=person_of(binding_repository, admin_id))
    devices.record_connection(device_id, "online", message_received=True)
    row = next(row for row in devices.list_devices() if row["device_id"] == device_id)
    assert row["connection_state"] == "online" and row["last_message_at"] is not None
    devices.revoke(device_id)
    devices.record_connection(device_id, "error")
    row = next(row for row in devices.list_devices() if row["device_id"] == device_id)
    assert row["connection_state"] == "stopped"
    with pytest.raises(ValueError):
        devices.record_connection(device_id, "made-up")


def test_console_actor_is_unaffected(binding_repository):
    identities = IdentityRepository(binding_repository._connection_factory)
    admin_id = seed_admin(binding_repository)
    actor = Actor(ActorType.CONSOLE_ADMIN, str(admin_id), roles=("admin", "super_admin"), authenticated_by="mysql_admin_session")
    assert identities.allows(actor, "super_admin")
