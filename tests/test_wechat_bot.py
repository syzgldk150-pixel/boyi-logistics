"""WeChat personal-account bot: rules, iLink wire format, QR binding and channel routing.

All WeChat data here is synthetic; no real iLink endpoint is contacted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid

import httpx
import pytest

from shared.identity_permissions import AGENT_CAPABILITIES, CONSOLE_MODULES, IdentityAccess
from shared.wechat_bot import (
    has_unsupported_media,
    inbound_text,
    parse_wechat_actor_id,
    restrict_access,
    split_text,
    validate_device_permissions,
    wechat_actor_id,
)

DEVICE = "0f0e0d0c-0b0a-4908-8706-050403020100"
PERSON = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


# -- shared rules -------------------------------------------------------------

def test_actor_ids_are_exact_and_round_trip():
    actor = wechat_actor_id(DEVICE)
    assert actor == f"wechat:{DEVICE}"
    assert parse_wechat_actor_id(actor) == DEVICE
    for bad in ("ou_feishu", f"wechat:{DEVICE.upper()}", "wechat:../x", "wechat:", None, 7):
        assert parse_wechat_actor_id(bad) is None
    with pytest.raises(ValueError):
        wechat_actor_id("not-a-uuid")


def test_device_limits_only_narrow_the_identity():
    role = IdentityAccess(True, False, "r1", "查询员", console={"finance": "operate"},
                          agent={"ai.chat": "direct", "business.query": "direct", "plugins.execute": "confirm"})
    assert restrict_access(role, None) == role.for_channel("agent")
    narrowed = restrict_access(role, ("business.query", "plugins.execute", "finance.read"))
    assert narrowed.permissions == ("business.query", "plugins.execute")  # finance.read is not in the identity
    assert narrowed.agent_level("plugins.execute") == "confirm"  # the level is the identity's, not the device's
    assert not restrict_access(role, ("business.query",)).allows("plugins.execute")
    assert not narrowed.allows("finance.read") and not narrowed.allows("finance.manage")  # Console rights never leak

    admin = IdentityAccess(True, True, None, "超级管理员")
    limited_admin = restrict_access(admin, ("business.query",))
    assert not limited_admin.super_admin
    assert limited_admin.allows("business.query") and not limited_admin.allows("super_admin")
    assert restrict_access(admin, None).allows("super_admin")
    assert restrict_access(admin, tuple(AGENT_CAPABILITIES)).permissions == tuple(sorted(AGENT_CAPABILITIES))

    inactive = IdentityAccess()
    assert not restrict_access(inactive, ("business.query",)).active
    assert restrict_access(role, ()).permissions == ()


def test_device_limit_validation_uses_the_shared_catalog():
    assert validate_device_permissions(None) is None
    assert validate_device_permissions([]) == ()
    with pytest.raises(ValueError):
        validate_device_permissions(["unknown"])
    with pytest.raises(ValueError):
        validate_device_permissions(["finance.manage"])
    with pytest.raises(ValueError):
        validate_device_permissions(["dispatch"])  # a Console module, not an agent capability


def test_inbound_text_uses_typed_text_and_voice_transcription():
    message = {"item_list": [
        {"type": 1, "text_item": {"text": " 查单号 "}},
        {"type": 3, "voice_item": {"text": "1234567890123"}},
        {"type": 2, "image_item": {}},
    ]}
    assert inbound_text(message) == "查单号\n1234567890123"
    assert has_unsupported_media(message)
    assert inbound_text({"item_list": [{"type": 2}]}) == ""


def test_split_text_never_drops_content():
    text = "\n".join(f"第{i}行" + "字" * 50 for i in range(300))
    chunks = split_text(text, limit=500)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert split_text("x" * 1200, limit=500) == ["x" * 500, "x" * 500, "x" * 200]
    assert split_text("") == []


# -- identity lookup ------------------------------------------------------------

class _Cursor:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def autocommit(self, _value):
        pass

    def cursor(self, *_args):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_identity_lookup_routes_wechat_actor_to_device_with_limits():
    from shared.identity_repository import IdentityRepository

    cursor = _Cursor([{
        "allowed_permissions_json": json.dumps(["business.query"]), "is_active": 1,
        "control_plane_role": "admin", "access_role_id": "r1", "role_name": "业务员", "role_active": 1,
        "console_permissions_json": json.dumps({"finance": "view"}),
        "agent_permissions_json": json.dumps({"business.query": "direct", "plugins.execute": "confirm"}),
    }])
    identities = IdentityRepository(lambda: _Connection(cursor))
    access = identities.feishu(wechat_actor_id(DEVICE))
    assert access.permissions == ("business.query",)
    assert "wechat_bot_devices" in cursor.executed[0][0]
    assert cursor.executed[0][1] == (DEVICE,)
    assert identities.feishu(f"wechat:{DEVICE}x") == IdentityAccess(channel="agent")  # malformed id never queries
    assert len(cursor.executed) == 1
    assert identities.feishu(wechat_actor_id(DEVICE)) == IdentityAccess(channel="agent")  # revoked / missing device


def test_feishu_binding_code_is_refused_for_wechat_senders():
    from agent.orchestration.feishu_binding_service import FeishuBindingService

    service = FeishuBindingService(repository=None)
    reply = service.handle_binding_text(wechat_actor_id(DEVICE), wechat_actor_id(DEVICE), "绑定审批 ABCDEFGHJK")
    assert reply and "后台扫码" in reply
    assert service.handle_binding_text(wechat_actor_id(DEVICE), wechat_actor_id(DEVICE), "你好") is None


# -- iLink wire format ----------------------------------------------------------

def _client(handler):
    from agent.wechat_bot.ilink import ILinkClient

    return ILinkClient(transport=httpx.MockTransport(handler))


def test_send_text_matches_official_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ret": 0})

    async def run():
        client = _client(handler)
        await client.send_text("https://ilinkai.weixin.qq.com", "synthetic-token", to_user_id="u@im.wechat",
                               text="你好", context_token="ctx-1")
        await client.close()

    asyncio.run(run())
    assert seen["url"] == "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage"
    headers = seen["headers"]
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert headers["Authorization"] == "Bearer synthetic-token"
    assert headers["iLink-App-Id"] == "bot"
    assert base64.b64decode(headers["X-WECHAT-UIN"]).decode().isdigit()
    msg = seen["body"]["msg"]
    assert msg["from_user_id"] == "" and msg["to_user_id"] == "u@im.wechat"
    assert msg["message_type"] == 2 and msg["message_state"] == 2
    assert msg["item_list"] == [{"type": 1, "text_item": {"text": "你好"}}]
    assert msg["context_token"] == "ctx-1"
    assert seen["body"]["base_info"]["channel_version"]


def test_send_failure_and_timeout_are_explicit():
    from agent.wechat_bot.ilink import ILinkError

    async def run(handler):
        client = _client(handler)
        try:
            await client.send_text("https://ilinkai.weixin.qq.com", "t", to_user_id="u", text="x", context_token=None)
        finally:
            await client.close()

    with pytest.raises(ILinkError, match="ret=-2"):
        asyncio.run(run(lambda request: httpx.Response(200, json={"ret": -2, "errmsg": "secret detail"})))

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ILinkError) as info:
        asyncio.run(run(timeout))
    assert info.value.code == "ILINK_SEND_TIMEOUT"


def test_get_updates_long_poll_timeout_is_an_empty_batch():
    def handler(request):
        raise httpx.ReadTimeout("idle", request=request)

    async def run():
        client = _client(handler)
        result = await client.get_updates("https://ilinkai.weixin.qq.com", "t", "buf-1", timeout=1)
        await client.close()
        return result

    assert asyncio.run(run()) == {"ret": 0, "msgs": [], "get_updates_buf": "buf-1"}


def test_base_url_from_ilink_must_stay_on_weixin_domain():
    from agent.wechat_bot.ilink import ILinkError, safe_base_url

    assert safe_base_url("") == "https://ilinkai.weixin.qq.com"
    assert safe_base_url("sh.ilinkai.weixin.qq.com") == "https://sh.ilinkai.weixin.qq.com"
    for bad in ("https://evil.example", "https://weixin.qq.com.evil.example", "http://ilinkai.weixin.qq.com.example"):
        with pytest.raises(ILinkError):
            safe_base_url(bad)


# -- QR binding -------------------------------------------------------------------

class _FakeIdentities:
    def __init__(self, *, target_valid=True):
        self.target_valid = target_valid

    def cursor(self):
        valid = self.target_valid

        class _Ctx:
            def __enter__(self_inner):
                return _TargetCursor(valid)

            def __exit__(self_inner, *exc):
                return False
        return _Ctx()


class _TargetCursor:
    def __init__(self, valid):
        self.valid = valid

    def execute(self, *_args):
        pass

    def fetchone(self):
        return {"ok": 1} if self.valid else None


class _FakeDevices:
    def __init__(self, conflict=False):
        self.created, self.revoked, self.conflict = [], [], conflict

    def active_devices(self):
        return []

    def create_device(self, **row):
        from shared.wechat_bot_repository import WeChatDeviceConflict

        if self.conflict:
            raise WeChatDeviceConflict("该微信已绑定为设备，请先在后台解绑后再重新扫码")
        self.created.append(row)

    def revoke(self, device_id):
        self.revoked.append(device_id)
        return True


class _FakeILink:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.verify_codes = []

    async def get_bot_qrcode(self, local_tokens):
        from agent.wechat_bot.ilink import QRCode

        return QRCode(qrcode="qr-synthetic", content="https://liteapp.weixin.qq.com/q/synthetic?qrcode=qr")

    async def get_qrcode_status(self, base_url, qrcode, verify_code=None):
        from agent.wechat_bot.ilink import QRStatus

        self.verify_codes.append(verify_code)
        await asyncio.sleep(0)
        status = self.statuses.pop(0) if self.statuses else "wait"
        if isinstance(status, QRStatus):
            return status
        return QRStatus(status=status)


def _confirmed():
    from agent.wechat_bot.ilink import QRStatus

    return QRStatus(status="confirmed", bot_token="synthetic-bot-token", ilink_bot_id="bot-1@im.bot",
                    ilink_user_id="owner-1@im.wechat", base_url="")


async def _until(service, login_id, until, timeout=5.0):
    # A wall-clock bound: ``no_sleep`` turns every asyncio.sleep into a bare
    # yield, so a step count can run out before the binding thread finishes.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.login_status(login_id, 1)
        if state["status"] in until:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("login did not finish")


async def _run_login(service, *, until=("confirmed", "failed"), **kwargs):
    login = await service.start_login(1, person_id=PERSON, label="测试设备", permissions=["business.query"], **kwargs)
    return login, await _until(service, login["login_id"], until)


@pytest.fixture
def no_sleep(monkeypatch):
    import agent.wechat_bot.service as service_module

    real_sleep = asyncio.sleep

    async def fast(seconds):
        await real_sleep(0)

    monkeypatch.setattr(service_module.asyncio, "sleep", fast)


def _service(tmp_path, ilink, devices, bound):
    from agent.wechat_bot.service import WeChatBotService
    from agent.wechat_bot.tokens import WeChatTokenStore

    async def on_bound(device_id):
        bound.append(device_id)

    tokens = WeChatTokenStore(tmp_path / "tokens")
    return WeChatBotService(devices=devices, identities=_FakeIdentities(), client=ilink, tokens=tokens,
                            on_bound=on_bound), tokens


def test_confirmed_scan_creates_device_and_keeps_token_out_of_mysql(tmp_path, no_sleep):
    devices, bound = _FakeDevices(), []
    service, tokens = _service(tmp_path, _FakeILink(["wait", "scaned", _confirmed()]), devices, bound)

    async def run():
        login, state = await _run_login(service)
        assert login["qr_image"].startswith("data:image/svg+xml")
        return state

    state = asyncio.run(run())
    assert state["status"] == "confirmed" and state["qr_image"] is None
    row = devices.created[0]
    assert row["owner_user_id"] == "owner-1@im.wechat" and row["ilink_bot_id"] == "bot-1@im.bot"
    assert row["permissions"] == ("business.query",) and row["person_id"] == PERSON
    assert "synthetic-bot-token" not in json.dumps(row, default=str)
    assert bound == [row["device_id"]] == [state["device_id"]]
    assert tokens.load(row["device_id"])["token"] == "synthetic-bot-token"


def test_verify_code_is_forwarded_once(tmp_path, no_sleep):
    ilink = _FakeILink(["scaned", "need_verifycode", "scaned", _confirmed()])
    devices = _FakeDevices()
    service, _tokens = _service(tmp_path, ilink, devices, [])

    async def run():
        login, state = await _run_login(service, until=("need_verifycode",))
        service.submit_verify_code(login["login_id"], 1, "4321")
        return await _wait(service, login["login_id"])

    assert asyncio.run(run())["status"] == "confirmed"
    assert ilink.verify_codes.count("4321") == 1


async def _wait(service, login_id):
    return await _until(service, login_id, ("confirmed", "failed"))


@pytest.mark.parametrize("status,message", [
    ("expired", "过期"), ("binded_redirect", "已连接过"), ("verify_code_blocked", "次数过多"), ("odd", "未知"),
])
def test_unsuccessful_scans_fail_explicitly(tmp_path, no_sleep, status, message):
    devices = _FakeDevices()
    service, _tokens = _service(tmp_path, _FakeILink([status]), devices, [])
    _login, state = asyncio.run(_run_login(service))
    assert state["status"] == "failed" and message in state["message"]
    assert devices.created == []


def test_rebinding_an_active_bot_is_refused_and_token_discarded(tmp_path, no_sleep):
    devices = _FakeDevices(conflict=True)
    service, tokens = _service(tmp_path, _FakeILink([_confirmed()]), devices, [])
    _login, state = asyncio.run(_run_login(service))
    assert state["status"] == "failed" and "已绑定" in state["message"]
    assert tokens.device_ids() == []


def test_login_input_and_ownership_are_checked(tmp_path, no_sleep):
    from agent.wechat_bot.service import WeChatBotError

    service, _tokens = _service(tmp_path, _FakeILink([]), _FakeDevices(), [])

    async def run():
        with pytest.raises(WeChatBotError):
            await service.start_login(1)  # a person is required
        with pytest.raises(WeChatBotError):
            await service.start_login(1, label="x" * 81, person_id=PERSON)
        with pytest.raises(WeChatBotError):
            await service.start_login(1, person_id=PERSON, permissions=["nope"])
        login = await service.start_login(1, person_id=PERSON)
        with pytest.raises(WeChatBotError):
            service.login_status(login["login_id"], 2)  # another administrator's login
        service.cancel_login(login["login_id"], 1)
        assert service.login_status(login["login_id"], 1)["status"] == "failed"
        await service.close()

    asyncio.run(run())


# -- channel ------------------------------------------------------------------------

class _ChannelDevices:
    def __init__(self):
        self.states = []

    def record_connection(self, device_id, state, *, message_received=False):
        self.states.append((state, message_received))

    def active_devices(self):
        return [{"device_id": DEVICE, "ilink_bot_id": "bot-1@im.bot", "owner_user_id": "owner-1@im.wechat"}]


class _SendingILink:
    def __init__(self):
        self.sent = []

    async def send_text(self, base_url, token, *, to_user_id, text, context_token):
        self.sent.append((to_user_id, text, context_token))

    async def notify(self, *args, **kwargs):
        pass


def _channel(tmp_path, delivered):
    from agent.wechat_bot.channel import WeChatChannelRuntime
    from agent.wechat_bot.tokens import WeChatTokenStore

    tokens = WeChatTokenStore(tmp_path / "tokens")
    tokens.save(DEVICE, token="synthetic", base_url="https://ilinkai.weixin.qq.com",
                ilink_bot_id="bot-1@im.bot", owner_user_id="owner-1@im.wechat")

    async def deliver(actor_id, text, event_id):
        delivered.append((actor_id, text, event_id))

    ilink = _SendingILink()
    runtime = WeChatChannelRuntime(devices=_ChannelDevices(), tokens=tokens, client=ilink, lease=None,
                                   deliver=deliver)
    return runtime, ilink


def test_channel_accepts_only_the_owner_once_with_stable_event_id(tmp_path):
    delivered = []
    runtime, ilink = _channel(tmp_path, delivered)
    row = {"device_id": DEVICE, "ilink_bot_id": "bot-1@im.bot", "owner_user_id": "owner-1@im.wechat"}

    def message(sender, message_id, text="查单号 1234567890123", message_type=1):
        return {"message_type": message_type, "from_user_id": sender, "message_id": message_id,
                "context_token": "ctx-9", "item_list": [{"type": 1, "text_item": {"text": text}}]}

    async def run():
        await runtime._accept(row, message("stranger@im.wechat", "1"))
        await runtime._accept(row, message("owner-1@im.wechat", "2", message_type=2))
        await runtime._accept(row, message("owner-1@im.wechat", "3"))
        await runtime._accept(row, message("owner-1@im.wechat", "3"))  # redelivered
        await runtime._accept(row, {"message_type": 1, "from_user_id": "owner-1@im.wechat", "message_id": "4",
                                    "item_list": [{"type": 2, "image_item": {}}]})
        await runtime._accept(row, {"message_type": 1, "from_user_id": "owner-1@im.wechat",
                                    "item_list": [{"type": 1, "text_item": {"text": "无编号"}}]})
        await asyncio.gather(*runtime._handlers)

    asyncio.run(run())
    actor = wechat_actor_id(DEVICE)
    assert delivered == [(actor, "查单号 1234567890123", f"{actor}:3")]
    assert ilink.sent == [("owner-1@im.wechat", "目前只支持文字和语音消息，请用文字描述要办理的事情。", "ctx-9")]


def test_channel_replies_echo_context_token_and_split_long_text(tmp_path):
    runtime, ilink = _channel(tmp_path, [])
    runtime._contexts[DEVICE] = ("owner-1@im.wechat", "ctx-1")
    asyncio.run(runtime.send(wechat_actor_id(DEVICE), "甲" * 4500))
    assert [len(text) for _to, text, _ctx in ilink.sent] == [4000, 500]
    assert {ctx for _to, _text, ctx in ilink.sent} == {"ctx-1"}
    asyncio.run(runtime.send("ou_feishu_user", "ignored"))
    assert len(ilink.sent) == 2


def test_stale_session_marks_device_and_pauses(tmp_path, monkeypatch):
    import agent.wechat_bot.channel as channel_module

    runtime, _ilink = _channel(tmp_path, [])
    sleeps = []

    class _PollingILink(_SendingILink):
        async def get_updates(self, base_url, token, buf, *, timeout):
            return {"ret": -14, "errcode": -14}

    runtime._client = _PollingILink()

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        runtime._stopping = True

    monkeypatch.setattr(channel_module.asyncio, "sleep", fake_sleep)
    asyncio.run(runtime._poll({"device_id": DEVICE, "ilink_bot_id": "bot-1@im.bot", "owner_user_id": "owner-1@im.wechat"}))
    assert sleeps == [channel_module.STALE_PAUSE_SECONDS]
    assert ("session_expired", False) in runtime._devices.states


def test_poll_saves_cursor_and_refuses_mismatched_token(tmp_path, monkeypatch):
    import agent.wechat_bot.channel as channel_module

    delivered = []
    runtime, _ilink = _channel(tmp_path, delivered)

    class _OneBatch(_SendingILink):
        async def get_updates(self, base_url, token, buf, *, timeout):
            runtime._stopping = True
            return {"ret": 0, "get_updates_buf": "buf-2", "msgs": [
                {"message_type": 1, "from_user_id": "owner-1@im.wechat", "message_id": "77",
                 "item_list": [{"type": 1, "text_item": {"text": "统计"}}]}]}

    runtime._client = _OneBatch()
    row = {"device_id": DEVICE, "ilink_bot_id": "bot-1@im.bot", "owner_user_id": "owner-1@im.wechat"}

    async def run():
        await runtime._poll(row)
        await asyncio.gather(*runtime._handlers)

    asyncio.run(run())
    assert runtime._tokens.load_cursor(DEVICE) == "buf-2"
    assert delivered and delivered[0][1] == "统计"

    runtime._stopping = False
    asyncio.run(runtime._poll({**row, "ilink_bot_id": "other@im.bot"}))
    assert runtime._devices.states[-1] == ("session_expired", False)


def test_token_store_files_are_private(tmp_path):
    from agent.wechat_bot.tokens import WeChatTokenStore

    store = WeChatTokenStore(tmp_path / "t")
    store.save(DEVICE, token="tok", base_url="https://ilinkai.weixin.qq.com", ilink_bot_id="b", owner_user_id="o")
    assert oct((tmp_path / "t" / DEVICE / "bot.json").stat().st_mode & 0o777) == "0o600"
    assert store.tokens([DEVICE]) == ["tok"]
    assert store.device_ids(older_than_seconds=3600) == []  # just written: inside the grace period
    store.delete(DEVICE)
    assert store.load(DEVICE) is None and store.device_ids() == []
    with pytest.raises(ValueError):
        store.load("../escape")


# -- shared chat pipeline -------------------------------------------------------------

def test_channel_text_uses_feishu_pipeline_and_replies_through_channel(monkeypatch):
    from feishu import message_handler

    processed, replies = [], []

    async def fake_process(text, sender_id, chat_id):
        context = message_handler._COMMAND_CONTEXT.get()
        processed.append((text, sender_id, chat_id, context.event_id if context else None))
        await message_handler._reply_text(chat_id, "已收到")

    async def sender(receive_id, text):
        replies.append((receive_id, text))

    monkeypatch.setattr(message_handler, "_process_and_reply", fake_process)
    actor = wechat_actor_id(DEVICE)

    async def run():
        with pytest.raises(ValueError):
            await message_handler.handle_channel_text(actor_id=actor, text="统计", event_id="e")
        message_handler.bind_channel_reply_sender("wechat:", sender)
        try:
            await message_handler.handle_channel_text(actor_id=actor, text=" 统计 ", event_id=f"{actor}:9")
        finally:
            message_handler.bind_channel_reply_sender("wechat:", None)

    asyncio.run(run())
    assert processed == [("统计", actor, actor, f"{actor}:9")]
    assert replies == [(actor, "已收到")]
    assert message_handler._COMMAND_CONTEXT.get() is None
    with pytest.raises(ValueError):
        message_handler.bind_channel_reply_sender("feishu:", sender)


# -- Console ---------------------------------------------------------------------------

def test_console_device_permission_form_parsing():
    from console.services.identities import device_permissions

    assert device_permissions({"permission_business.query": "1"}) is None
    assert device_permissions({"limit_permissions": "1", "permission_business.query": "1",
                               "permission_ai.chat": "0"}) == ["business.query"]
    assert device_permissions({"limit_permissions": "1", "permissions": ["ai.chat"]}) == ["ai.chat"]
    assert device_permissions({"limit_permissions": "1"}) == []


def test_console_routes_reach_wechat_handlers():
    from console.routes import auth

    calls = []

    class App:
        def __getattr__(self, name):
            return lambda *args, **kwargs: calls.append((name, args[1:], kwargs))

    login = str(uuid.uuid4())
    assert auth.handle_get(App(), None, f"/settings/accounts/wechat/logins/{login}", "", {})
    assert auth.handle_post(App(), None, "/settings/accounts/wechat/logins", "", {})
    assert auth.handle_post(App(), None, f"/settings/accounts/wechat/logins/{login}/verify-code", "", {})
    assert auth.handle_post(App(), None, f"/settings/accounts/wechat/logins/{login}/cancel", "", {})
    assert auth.handle_post(App(), None, f"/settings/accounts/wechat/{DEVICE}/update", "", {})
    assert auth.handle_post(App(), None, f"/settings/accounts/wechat/{DEVICE}/revoke", "", {})
    assert [name for name, *_ in calls] == [
        "_handle_wechat_login_status", "_handle_wechat_login_create", "_handle_wechat_login_action",
        "_handle_wechat_login_action", "_handle_wechat_device_update", "_handle_wechat_device_update"]
    assert calls[-1][2] == {"revoke": True} and calls[-2][2] == {"revoke": False}


def test_wechat_admin_routes_stay_super_admin_only():
    from shared.identity_routes import agent_permission, console_permission

    assert agent_permission("POST", "/internal/v1/admin/wechat-bot/logins") == "super_admin"
    assert agent_permission("DELETE", f"/internal/v1/admin/wechat-bot/devices/{DEVICE}") == "super_admin"
    assert console_permission("POST", "/settings/accounts/wechat/logins") == "super_admin"
    assert console_permission("GET", f"/settings/accounts/wechat/logins/{DEVICE}") == "super_admin"


def test_accounts_template_renders_wechat_devices():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader("console/templates"), autoescape=select_autoescape(["html"]))
    template = env.get_template("identity_management.html")
    base_device = {"device_id": DEVICE, "identity_label": "<司机>", "owner_hint": "owner-…", "access_role_id": "r1",
                   "person_id": PERSON, "person_name": "老王", "username": None, "role_name": "查询员", "active": True,
                   "effective": True, "connection_state": "online", "connection_label": "在线",
                   "bound_at": "2026-11-14 10:00", "last_message_at": None, "revoked_at": None}
    html = template.render(
        identity_roles=[{"role_id": "r1", "name": "查询员", "console": {"business.query": "view"},
                         "agent": {"business.query": "direct", "plugins.execute": "confirm"}, "is_active": True}],
        users=[], feishu_bindings=[], console_modules=CONSOLE_MODULES, agent_capabilities=AGENT_CAPABILITIES, binding_challenge=None, admin_user=None,
        persons=[{"person_id": PERSON, "name": "老王", "access_role_id": "r1", "role_name": "查询员", "role_active": True,
                  "is_active": True, "effective": True, "super_admin": False, "account_id": None, "username": None,
                  "feishu_count": 0, "wechat_count": 2}],
        wechat_devices=[{**base_device, "allowed_permissions": ["business.query"]},
                        {**base_device, "device_id": str(uuid.uuid4()), "allowed_permissions": None}],
    )
    assert "微信设备" in html and "扫码绑定微信" in html
    assert "&lt;司机&gt;" in html and "<司机>" not in html
    assert "限 1 项功能" in html and "不额外限制" in html
    assert f"/settings/accounts/wechat/{DEVICE}/revoke" in html
    edit = html.split(f'id="identity-wechat-edit-{DEVICE}"', 1)[1].split("</dialog>", 1)[0]
    assert 'name="limit_permissions" value="1" data-wechat-limit-toggle checked' in edit
    assert 'name="permission_business.query" value="1" checked' in edit
    assert f'<option value="{PERSON}" selected>老王 · 查询员</option>' in edit
    assert "2 台微信" in html and f'id="identity-person-edit-{PERSON}"' in html
    # The role editor shows both sets with their current levels.
    assert "后台 1 项 · 机器人 2 项" in html
    editor = html.split('data-identity-role-form', 1)[1].split("</form>", 1)[0]
    assert '<option value="view" selected>可见</option>' in editor.split('name="console_business.query"', 1)[1].split("</select>", 1)[0]
    assert 'value="operate"' not in editor.split('name="console_business.query"', 1)[1].split("</select>", 1)[0]
    plugins = editor.split('name="agent_plugins.execute"', 1)[1].split("</select>", 1)[0]
    assert '<option value="confirm" selected>需确认</option>' in plugins and "直接执行" in plugins
    query = editor.split('name="agent_business.query"', 1)[1].split("</select>", 1)[0]
    assert '<option value="direct" selected>允许</option>' in query and "需确认" not in query
    assert 'name="permission_dispatch"' not in html  # device limits list agent capabilities only
