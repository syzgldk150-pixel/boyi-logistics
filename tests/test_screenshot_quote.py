"""Quote from a screenshot (收尾 截图报价) in Feishu and WeChat.

The active model reads the delivery address through the AI task gateway; code
checks it against the text the model read, shows it, and hands the usual
``报价 地址，重量[，体积]`` text to the existing quote route. A missing weight is
asked; the next short weight reply finishes the quote. Every address, name and
phone number here is invented (never copy a real customer's screenshot into tests).
"""
import asyncio
import base64
import json

import httpx
import pytest

from shared.ai_tasks import AITaskError, AITaskResult
from shared.screenshot_quote import NO_ADDRESS_REPLY, quote_text, validate_reading

SOURCE = "湖南省示例市虚构区样例小区3栋\n2单 101房\n示例联系人13800000000 一个冰箱 300公斤 2方"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def test_reading_is_kept_only_when_address_and_numbers_are_in_the_text_read():
    reading = validate_reading({"address": "湖南省示例市虚构区样例小区3栋2单101房", "weight_kg": 300, "volume_m3": "2",
                                "source_text": SOURCE, "note": ""})
    assert reading == {"address": "湖南省示例市虚构区样例小区3栋2单101房", "weight_kg": "300", "volume_m3": "2", "note": ""}
    assert validate_reading({"address": None, "source_text": "", "weight_kg": None})["address"] is None
    stripped = validate_reading({"address": "湖南省示例市虚构区样例小区3栋2单101房13800000000", "source_text": SOURCE + "13800000000"})
    assert "13800000000" not in stripped["address"]
    for bad, message in (({"address": "湖南省长沙市岳麓区麓谷大道", "source_text": SOURCE}, "source_text"),
                         ({"address": "湖南省示例市虚构区样例小区3栋", "weight_kg": 500, "source_text": SOURCE}, "weight_kg"),
                         ({"address": "湖南省示例市虚构区样例小区3栋", "volume_m3": -1, "source_text": SOURCE}, "volume_m3"),
                         ([], "JSON")):
        with pytest.raises(ValueError, match=message):
            validate_reading(bad)


def test_quote_text_is_what_a_person_would_type():
    from agent.direct_tool_router import parse_price_request

    text = quote_text("湖南省示例市 虚构区，样例小区3栋", "300", "2")
    assert text == "报价 湖南省示例市虚构区样例小区3栋，300，2"
    assert parse_price_request(text) == {"address": "湖南省示例市虚构区样例小区3栋", "weight": 300.0, "volume": 2.0}


class Gateway:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def run(self, spec, payload, *, subject_ref, actor_id=None, images=()):
        self.calls.append((spec.task_id, subject_ref, actor_id, images))
        if self.error:
            raise self.error
        return AITaskResult(run_id="r1", result=spec.validate(self.answer), provider="deepseek", model="m",
                            config_version_id=1, attempts=1)


def _service(answer=None, error=None, clock=None):
    from agent.screenshot_quote import ScreenshotQuoteService
    gateway = Gateway(answer, error)
    return ScreenshotQuoteService(gateway, **({"clock": clock} if clock else {})), gateway


def run(coroutine):
    return asyncio.run(coroutine)


def test_with_a_weight_the_quote_runs_at_once():
    service, gateway = _service({"address": "湖南省示例市虚构区样例小区3栋2单101房", "weight_kg": 300, "source_text": SOURCE})
    outcome = run(service.read("chat:ou", JPEG, actor_id="ou"))
    assert outcome.quote_text == "报价 湖南省示例市虚构区样例小区3栋2单101房，300"
    assert "识别到的收货地址：湖南省示例市虚构区样例小区3栋2单101房" in outcome.reply and "按 0.1 立方米" in outcome.reply
    assert gateway.calls[0][0] == "quote.read_screenshot" and gateway.calls[0][3] == (JPEG,)


def test_without_a_weight_it_asks_and_the_next_short_reply_finishes_the_quote():
    now = [0.0]
    service, _ = _service({"address": "湖南省示例市虚构区样例小区3栋2单101房", "source_text": SOURCE}, clock=lambda: now[0])
    outcome = run(service.read("chat:ou", JPEG, actor_id="ou"))
    assert outcome.quote_text is None and "请告诉我重量（公斤）" in outcome.reply
    assert service.follow_up("chat:other", "300") is None  # another person's reply
    assert service.follow_up("chat:ou", "R00024785328 到哪了") is None  # not a weight
    assert service.follow_up("chat:ou", "600斤") is None  # the quote route does not take 斤; stays waiting
    assert service.follow_up("chat:ou", "300公斤 2方") == "报价 湖南省示例市虚构区样例小区3栋2单101房，300，2"
    assert service.follow_up("chat:ou", "300") is None  # used once
    run(service.read("chat:ou", JPEG, actor_id="ou"))
    now[0] = 901.0
    assert service.follow_up("chat:ou", "300") is None  # 15 minutes passed


def test_weight_replies_are_read_the_ways_people_type_them():
    """2026-11-16: “300公斤6个方” went to the chat model, which no longer knew the address."""
    address = "湖南省示例市虚构区样例小区3栋2单101房"
    cases = {
        "300公斤6个方": "300，6",
        "300kg 2方": "300，2",
        "2方，300": "300，2",
        "300 6方": "300，6",
        "重量300公斤 体积6立方米左右": "300，6",
        "大概300公斤": "300",
        "6个方 300公斤": "300，6",
        "300.5公斤，1.2m³": "300.5，1.2",
    }
    for text, numbers in cases.items():
        service, _ = _service({"address": address, "source_text": SOURCE})
        run(service.read("chat:ou", JPEG, actor_id="ou"))
        assert service.follow_up("chat:ou", text) == f"报价 {address}，{numbers}", text
    for text in ("600斤", "300公斤 2方 3方", "300公斤 400公斤", "R00024785328 到哪了", "明天300公斤送到", "300公斤6个方送海口"):
        service, _ = _service({"address": address, "source_text": SOURCE})
        run(service.read("chat:ou", JPEG, actor_id="ou"))
        assert service.follow_up("chat:ou", text) is None, text


def test_no_address_and_ai_failures_are_told_plainly():
    service, _ = _service({"address": None, "source_text": "会议安排"})
    assert run(service.read("k", JPEG, actor_id="ou")).reply == NO_ADDRESS_REPLY
    service, _ = _service(error=AITaskError("AI_UNAVAILABLE", "尚未启用 AI 模型。"))
    outcome = run(service.read("k", JPEG, actor_id="ou"))
    assert outcome.quote_text is None and "截图没有识别成功：尚未启用 AI 模型。" in outcome.reply
    assert "超过 8MB" in run(service.read("k", b"", actor_id="ou")).reply


def test_gateway_sends_the_picture_and_audits_only_its_hash():
    from agent.ai_tasks.gateway import AITaskGateway
    from shared.screenshot_quote import READ_SCREENSHOT

    seen, audits = [], []

    class LLM:
        def public_status(self):
            return {"configured": True, "provider": "deepseek", "model": "deepseek-v4-flash", "config_version_id": 3}

        async def chat(self, messages, **kwargs):
            seen.append((messages, kwargs))
            return {"content": json.dumps({"address": "湖南省示例市虚构区样例小区3栋", "source_text": SOURCE})}

    class Audit:
        def record_run(self, row):
            audits.append(row)

    result = run(AITaskGateway(LLM(), Audit()).run(READ_SCREENSHOT, {"task": "读图"}, subject_ref="s", images=(JPEG,)))
    assert result.result["address"] == "湖南省示例市虚构区样例小区3栋"
    content = seen[0][0][1]["content"]
    assert content[0] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()}}
    assert content[1]["type"] == "text" and seen[0][1]["response_format"] == {"type": "json_object"}
    assert "base64" not in json.dumps(audits[0], ensure_ascii=False) and len(audits[0]["input_sha256"]) == 64
    with pytest.raises(AITaskError, match="只支持"):
        run(AITaskGateway(LLM(), Audit()).run(READ_SCREENSHOT, {}, subject_ref="s", images=(b"not-an-image",)))


# -- channels ---------------------------------------------------------------------------------

def _wire_feishu(monkeypatch, allowed=True):
    from types import SimpleNamespace

    from feishu import message_handler, screenshot_quote

    replies, quotes = [], []

    async def reply(chat_id, text, **_):
        replies.append((chat_id, text))

    async def process(text, sender_id, chat_id):
        quotes.append((text, sender_id, chat_id))

    identity = SimpleNamespace(active=allowed, allows=lambda permission: allowed and permission == "plugins.execute")
    monkeypatch.setattr(message_handler, "_FEISHU_BINDING_RUNTIME",
                        SimpleNamespace(identity_access=SimpleNamespace(feishu=lambda sender: identity)))
    monkeypatch.setattr(message_handler, "_reply_text", reply)
    monkeypatch.setattr(message_handler, "_process_and_reply", process)
    monkeypatch.setattr(screenshot_quote, "_download_feishu_image_sync", lambda message_id, key: JPEG)
    return screenshot_quote, replies, quotes


def test_feishu_screenshot_runs_the_normal_quote_path(monkeypatch):
    module, replies, quotes = _wire_feishu(monkeypatch)
    service, _ = _service({"address": "湖南省示例市虚构区样例小区3栋2单101房", "source_text": SOURCE})
    module.bind_screenshot_quote(service)
    try:
        run(module.handle_feishu_image(chat_id="oc_1", sender_id="ou_1", message_id="om_1",
                                       raw_content=json.dumps({"image_key": "img_1"})))
        assert "请告诉我重量" in replies[-1][1] and quotes == []
        assert module.follow_up_text("300", sender_id="ou_1", chat_id="oc_1") == "报价 湖南省示例市虚构区样例小区3栋2单101房，300"
        assert module.follow_up_text("你好", sender_id="ou_1", chat_id="oc_1") == "你好"
        service._gateway.answer = {"address": "湖南省示例市虚构区样例小区3栋2单101房", "weight_kg": 300, "source_text": SOURCE}
        run(module.handle_feishu_image(chat_id="oc_1", sender_id="ou_1", message_id="om_2",
                                       raw_content={"image_key": "img_2"}))
        assert quotes == [("报价 湖南省示例市虚构区样例小区3栋2单101房，300", "ou_1", "oc_1")]
    finally:
        module.bind_screenshot_quote(None)


def test_feishu_screenshot_needs_permission_and_reports_download_failures(monkeypatch):
    module, replies, _ = _wire_feishu(monkeypatch, allowed=False)
    service, gateway = _service({"address": None, "source_text": ""})
    module.bind_screenshot_quote(service)
    try:
        run(module.handle_feishu_image(chat_id="oc_1", sender_id="ou_1", message_id="om_1", raw_content={"image_key": "k"}))
        assert replies[-1][1] == module.NO_PERMISSION_REPLY and gateway.calls == []  # no model call
        module2, replies2, _ = _wire_feishu(monkeypatch)

        def broken(message_id, key):
            raise RuntimeError("飞书图片下载失败（code=99991672）")
        monkeypatch.setattr(module2, "_download_feishu_image_sync", broken)
        run(module2.handle_feishu_image(chat_id="oc_1", sender_id="ou_1", message_id="om_1", raw_content={"image_key": "k"}))
        assert "图片没有下载成功：飞书图片下载失败" in replies2[-1][1]
    finally:
        module.bind_screenshot_quote(None)
    run(module.handle_feishu_image(chat_id="oc_1", sender_id="ou_1", message_id="om_1", raw_content={}))
    assert replies2[-1][1] == module.NOT_ENABLED_REPLY


def test_wechat_picture_keys_urls_and_decryption():
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    from agent.wechat_bot.ilink import ILinkClient, ILinkError, image_aes_key, image_download_url

    key = bytes(range(16))
    assert image_aes_key({"aeskey": key.hex()}) == key
    assert image_aes_key({"media": {"aes_key": base64.b64encode(key).decode()}}) == key
    assert image_aes_key({"media": {"aes_key": base64.b64encode(key.hex().encode()).decode()}}) == key
    assert image_aes_key({"media": {}}) is None
    with pytest.raises(ILinkError):
        image_aes_key({"aeskey": "zz"})
    assert image_download_url({"media": {"encrypt_query_param": "a/b"}}) == \
        "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=a%2Fb"
    with pytest.raises(ILinkError):
        image_download_url({"media": {"full_url": "https://evil.example/x"}})

    def handler(request):
        assert request.url.host == "novac2c.cdn.weixin.qq.com"
        return httpx.Response(200, content=AES.new(key, AES.MODE_ECB).encrypt(pad(JPEG, 16)))

    client = ILinkClient(transport=httpx.MockTransport(handler))
    image = run(client.download_image({"aeskey": key.hex(), "media": {"encrypt_query_param": "p"}}))
    assert image == JPEG


def test_wechat_channel_hands_pictures_to_the_screenshot_quote(tmp_path):
    from agent.wechat_bot.channel import WeChatChannelRuntime
    from agent.wechat_bot.tokens import WeChatTokenStore
    from shared.wechat_bot import wechat_actor_id

    device = "0f0e0d0c-0b0a-4908-8706-050403020100"
    tokens = WeChatTokenStore(tmp_path / "tokens")
    tokens.save(device, token="synthetic", base_url="https://ilinkai.weixin.qq.com", ilink_bot_id="bot@im.bot",
                owner_user_id="owner@im.wechat")
    pictures = []

    class Devices:
        def record_connection(self, *args, **kwargs):
            pass

    class Client:
        async def download_image(self, image_item):
            return JPEG

    async def deliver(actor_id, text, event_id):
        raise AssertionError("text path not expected")

    async def deliver_image(actor_id, image, event_id):
        pictures.append((actor_id, image, event_id))

    runtime = WeChatChannelRuntime(devices=Devices(), tokens=tokens, client=Client(), lease=None, deliver=deliver,
                                   deliver_image=deliver_image)
    row = {"device_id": device, "owner_user_id": "owner@im.wechat"}

    async def go():
        await runtime._accept(row, {"message_type": 1, "from_user_id": "owner@im.wechat", "message_id": "9",
                                    "item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "p"}}}]})
        await asyncio.gather(*runtime._handlers)

    run(go())
    actor = wechat_actor_id(device)
    assert pictures == [(actor, JPEG, f"{actor}:9")]
