"""The single allow-list of login-free platform images, including redirects."""
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request

import pytest

from shared import receipt_image_sources as sources


@pytest.mark.parametrize("url,allowed", [
    ("https://tms.ronghuiwl.com/unauth/download/a.jpg", True),
    ("https://rhk13.obs.cn-east-3.myhuaweicloud.com/x/b.png", True),
    ("https://kyscan.yunda56.com/ws/c.jpeg", True),
    ("https://tms.ronghuiwl.com/widget/home.jpg", False),
    ("http://tms.ronghuiwl.com/unauth/download/a.jpg", False),
    ("https://tms.ronghuiwl.com:8443/unauth/download/a.jpg", False),
    ("https://user:pw@kyscan.yunda56.com/ws/c.jpg", False),
    ("https://kyscan.yunda56.com.evil.test/ws/c.jpg", False),
    ("https://kyscan.yunda56.com/ws/c.php", False),
    ("https://127.0.0.1/ws/c.jpg", False),
    ("https://kyscan.yunda56.com:bad/ws/c.jpg", False),
    ("", False),
])
def test_allow_list(url, allowed):
    assert sources._allowed(url)[0] is allowed


def test_redirects_are_followed_only_to_allow_listed_addresses():
    handler = sources._AllowListedRedirects()
    request = Request("https://tms.ronghuiwl.com/unauth/download/a.jpg")
    headers = {}
    assert handler.redirect_request(request, io.BytesIO(), 302, "Found", headers,
                                    "http://127.0.0.1:9000/internal/v1/health") is None
    assert handler.redirect_request(request, io.BytesIO(), 302, "Found", headers,
                                    "https://evil.test/a.jpg") is None
    followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", headers,
                                        "https://rhk13.obs.cn-east-3.myhuaweicloud.com/a.jpg")
    assert followed.full_url == "https://rhk13.obs.cn-east-3.myhuaweicloud.com/a.jpg"


def test_non_image_answers_and_unlisted_addresses_are_refused():
    class _Response(io.BytesIO):
        status = 200
        headers = {"Content-Type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = []

    def opener(request, *, timeout, context):
        calls.append(request.full_url)
        return _Response(b"<html>login</html>")

    assert sources.fetch_public_receipt_image("https://kyscan.yunda56.com/ws/c.jpg", opener=opener) is None
    assert sources.fetch_public_receipt_image("https://127.0.0.1/ws/c.jpg", opener=opener) is None
    assert calls == ["https://kyscan.yunda56.com/ws/c.jpg"]
