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
        self.follow_answer = None  # the model's answer to quote.read_follow_up
        self.payloads = []

    async def run(self, spec, payload, *, subject_ref, actor_id=None, images=()):
        self.calls.append((spec.task_id, subject_ref, actor_id, images))
        self.payloads.append(payload)
        if self.error:
            raise self.error
        answer = self.follow_answer if spec.task_id == "quote.read_follow_up" else self.answer
        try:
            result = spec.validate(answer)
        except ValueError as exc:  # the real gateway retries, then gives up with INVALID
            raise AITaskError("INVALID", "回答不合格", detail=str(exc)) from exc
        return AITaskResult(run_id="r1", result=result, provider="deepseek", model="m",
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
    assert follow(service, "chat:other", "300") is None  # another person's reply
    assert follow(service, "chat:ou", "R00024785328 到哪了") is None  # a tracking query, never sent to the model
    assert follow(service, "chat:ou", "300公斤 2方").quote_text == "报价 湖南省示例市虚构区样例小区3栋2单101房，300，2"
    assert follow(service, "chat:ou", "300") is None  # used once
    run(service.read("chat:ou", JPEG, actor_id="ou"))
    now[0] = 901.0
    assert follow(service, "chat:ou", "300") is None  # 15 minutes passed


ADDRESS = "湖南省示例市虚构区样例小区3栋2单101房"


def follow(service, key, text):
    return run(service.follow_up(key, text, actor_id="ou"))


def _waiting(volume=None):
    source = SOURCE if volume else SOURCE.replace(" 2方", "")
    service, gateway = _service({"address": ADDRESS, "volume_m3": volume, "source_text": source})
    run(service.read("chat:ou", JPEG, actor_id="ou"))
    gateway.calls.clear()
    gateway.payloads.clear()
    return service, gateway


def test_plain_weight_replies_are_read_by_code_without_the_model():
    cases = {"300公斤6个方": "300，6", "300kg 2方": "300，2", "2方，300": "300，2", "300 6方": "300，6",
             "重量300公斤 体积6立方米左右": "300，6", "大概300公斤": "300", "300.5公斤，1.2m³": "300.5，1.2"}
    for text, numbers in cases.items():
        service, gateway = _waiting()
        assert follow(service, "chat:ou", text).quote_text == f"报价 {ADDRESS}，{numbers}", text
        assert gateway.calls == [], text


def test_other_wording_is_understood_by_the_model_and_turned_into_the_standard_text():
    """2026-11-16: replies vary; the model reads them, code checks its numbers are in the reply."""
    service, gateway = _waiting()
    gateway.follow_answer = {"intent": "quote", "weight_value": 600, "weight_unit": "斤", "volume_m3": 6,
                             "address": None, "note": ""}
    outcome = follow(service, "chat:ou", "这批货600斤左右吧，体积差不多6个方")
    assert outcome.quote_text == f"报价 {ADDRESS}，300，6"
    assert "重量 300 公斤" in outcome.reply and "体积 6 立方米" in outcome.reply
    assert gateway.calls[0][0] == "quote.read_follow_up"
    assert gateway.payloads[0] == {"waiting_address": ADDRESS, "waiting_volume_m3": None,
                                   "reply": "这批货600斤左右吧，体积差不多6个方"}
    assert follow(service, "chat:ou", "300") is None  # finished

    service, gateway = _waiting(volume="2")
    gateway.follow_answer = {"intent": "quote", "weight_value": 1.5, "weight_unit": "吨", "volume_m3": None,
                             "address": "海南省示例市虚构区样例路8号", "note": ""}
    outcome = follow(service, "chat:ou", "地址改成海南省示例市虚构区样例路8号，一共1.5吨")
    assert outcome.quote_text == "报价 海南省示例市虚构区样例路8号，1500，2"  # the screenshot's volume is kept


def test_the_model_can_correct_the_address_or_cancel_and_otherwise_the_message_goes_on():
    service, gateway = _waiting()
    gateway.follow_answer = {"intent": "address", "weight_value": None, "volume_m3": None,
                             "address": "海南省示例市虚构区样例路8号", "note": ""}
    outcome = follow(service, "chat:ou", "不对，送到海南省示例市虚构区样例路8号")
    assert outcome.quote_text is None and "识别到的收货地址：海南省示例市虚构区样例路8号" in outcome.reply
    assert follow(service, "chat:ou", "300").quote_text == "报价 海南省示例市虚构区样例路8号，300"

    service, gateway = _waiting()
    gateway.follow_answer = {"intent": "cancel"}
    assert follow(service, "chat:ou", "算了这个先不报了").reply == "好的，这张截图的报价已取消。"
    assert follow(service, "chat:ou", "300") is None

    service, gateway = _waiting()
    gateway.follow_answer = {"intent": "other"}
    assert follow(service, "chat:ou", "今天几点下班") is None
    assert follow(service, "chat:ou", "300").quote_text == f"报价 {ADDRESS}，300"  # still waiting


def test_a_model_answer_not_backed_by_the_reply_is_never_quoted():
    for answer in ({"intent": "quote", "weight_value": 500, "weight_unit": "公斤"},      # invented weight
                   {"intent": "quote", "weight_value": 30, "weight_unit": "公斤"},       # only part of "300"
                   {"intent": "quote", "weight_value": 15, "weight_unit": "吨"},         # "1.5" is not "15"
                   {"intent": "quote", "weight_value": None},                             # quote without weight
                   {"intent": "address", "address": "湖南省长沙市岳麓区麓谷大道"},           # address not in reply
                   {"intent": "maybe"}):
        service, gateway = _waiting()
        gateway.follow_answer = answer
        assert follow(service, "chat:ou", "差不多300来斤，或者1.5吨也有可能") is None, answer
        assert service._pending.peek("chat:ou") is not None  # the address keeps waiting


def test_commands_and_confirmations_are_not_sent_to_the_model():
    for text in ("确认", "扫描", "统计", "报价 湖南省示例市虚构区样例路8号，300"):
        service, gateway = _waiting()
        assert follow(service, "chat:ou", text) is None, text
        assert gateway.calls == [], text
    service, gateway = _waiting()
    assert follow(service, "chat:ou", "取消") is None and gateway.calls == []  # reaches its own route
    assert service._pending.peek("chat:ou") is None  # and clears the waiting address


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
        service._gateway.follow_answer = {"intent": "other"}
        assert run(module.follow_up_text("你好", sender_id="ou_1", chat_id="oc_1")) == "你好"
        assert run(module.follow_up_text("300", sender_id="ou_1", chat_id="oc_1")) == "报价 湖南省示例市虚构区样例小区3栋2单101房，300"
        run(module.handle_feishu_image(chat_id="oc_1", sender_id="ou_1", message_id="om_1",
                                       raw_content=json.dumps({"image_key": "img_1"})))
        service._gateway.follow_answer = {"intent": "cancel"}
        assert run(module.follow_up_text("不用报了", sender_id="ou_1", chat_id="oc_1")) is None
        assert replies[-1][1] == "好的，这张截图的报价已取消。"
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
