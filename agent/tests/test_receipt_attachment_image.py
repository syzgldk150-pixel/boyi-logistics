"""Yunda receipt images are read only through the logged-in Agent session."""
import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.tms_runtime import receipts_query
from agent.tms_runtime.direct_business import DirectBusinessError

IMAGE_URL = "https://kyinms.yunda56.com/upload/return/demo.jpg"


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload, self.status_code, self.closed = payload, status_code, False

    def iter_content(self, _size):
        yield self.payload

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _broker(session):
    return lambda _profile: SimpleNamespace(build_requests_session=lambda validate: session)


def test_fetch_returns_image_bytes_without_following_redirects():
    session = _Session(_Response(b"\xff\xd8receipt"))
    with patch.object(receipts_query.receipts_sync, "get_session_broker", side_effect=_broker(session)):
        result = receipts_query.fetch_yunda_receipt_image({"source_url": IMAGE_URL}, session_profile="yunda")

    assert result == {"mime_type": "image/jpeg", "size": 9,
                      "content_base64": base64.b64encode(b"\xff\xd8receipt").decode("ascii")}
    assert session.calls[0][0] == IMAGE_URL
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.response.closed


@pytest.mark.parametrize("params", [
    {"source_url": "https://example.test/upload/demo.jpg"},
    {"source_url": "https://kyinms.yunda56.com.evil.test/demo.jpg"},
    {"source_url": "https://user:pw@kyinms.yunda56.com/demo.jpg"},
    {"source_url": "https://kyinms.yunda56.com/export/report.xlsx"},
    {"source_url": "file:///etc/passwd"},
    {"source_url": IMAGE_URL, "session_profile": "other"},
])
def test_fetch_rejects_anything_but_a_yunda_image_path(params):
    with patch.object(receipts_query.receipts_sync, "get_session_broker") as broker:
        with pytest.raises(DirectBusinessError) as caught:
            receipts_query.fetch_yunda_receipt_image(params, session_profile="yunda")
    assert caught.value.code == "INVALID_RECEIPT_IMAGE"
    broker.assert_not_called()


@pytest.mark.parametrize(("response", "code"), [
    (_Response(b"<html>login</html>"), "RECEIPT_IMAGE_NOT_IMAGE"),
    (_Response(b"", status_code=302), "RECEIPT_IMAGE_UNAVAILABLE"),
])
def test_fetch_fails_explicitly_when_no_image_is_returned(response, code):
    with patch.object(receipts_query.receipts_sync, "get_session_broker", side_effect=_broker(_Session(response))):
        with pytest.raises(DirectBusinessError) as caught:
            receipts_query.fetch_yunda_receipt_image({"source_url": IMAGE_URL}, session_profile="yunda")
    assert caught.value.code == code


def test_async_read_uses_the_resolved_yunda_account():
    session = _Session(_Response(b"\x89PNGreceipt"))

    async def exercise():
        lifecycle = DirectPluginInvocationService(object(), SimpleNamespace(), None, release_hold_provider=lambda: False)
        with patch.object(receipts_query, "resolve_account_params",
                          return_value={"account_id": "yunda-account", "session_profile": "yunda"}) as resolve, \
             patch.object(receipts_query.receipts_sync, "get_session_broker", side_effect=_broker(session)):
            result = await receipts_query.fetch_receipt_image_async({"source_url": IMAGE_URL}, 30,
                                                                    read_lifecycle=lifecycle)
        assert resolve.call_args.kwargs["default_system"] == "yunda"
        assert result["mime_type"] == "image/png"
        assert lifecycle.active_read_count() == 0

    asyncio.run(exercise())


def test_async_read_requires_a_yunda_account():
    async def exercise():
        with patch.object(receipts_query, "resolve_account_params", return_value={}):
            with pytest.raises(DirectBusinessError) as caught:
                await receipts_query.fetch_receipt_image_async({"source_url": IMAGE_URL}, 30, read_lifecycle=None)
        assert caught.value.code == "AUTH_REQUIRED"

    asyncio.run(exercise())
