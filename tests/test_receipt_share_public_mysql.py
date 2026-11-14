"""Real MySQL + real HTTP: the public share service shows one customer's pictures and nothing else."""
from __future__ import annotations

import http.client
import io
import json
import sys
import threading
import zipfile
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "console"))
import receipt_share_server as server  # noqa: E402
from shared import receipt_share as rules  # noqa: E402
from shared.receipt_nas_store import NasStoreError  # noqa: E402
from tests.mysql_isolated_fixtures import repository, pytestmark  # noqa: E402,F401
from tests.test_receipt_share_refresh_mysql import _insert  # noqa: E402

SECRET = "p" * 48
NOW = datetime(2026, 11, 14, 10, 0, 0)


def _jpeg(color):
    output = io.BytesIO()
    Image.new("RGB", (900, 700), color).save(output, format="JPEG")
    return output.getvalue()


class _Nas:
    def __init__(self):
        self.files, self.down, self.reads = {}, False, []

    def read(self, path):
        self.reads.append(path)
        if self.down:
            raise NasStoreError("NAS_UNAVAILABLE", "暂时连不上 NAS。")
        return self.files[path]


@pytest.fixture
def world(repository, tmp_path):
    connection = repository._connection_factory()
    with connection.cursor() as cursor:
        for table in ("receipt_share_access_logs", "receipt_share_items", "receipt_share_images", "receipt_shares",
                      "receipt_share_customers"):
            cursor.execute(f"DELETE FROM {table}")
    nas = _Nas()

    def share(name, short, *, code="", kinds=("sender_label", "sign", "receipt"), **columns):
        customer = _insert(connection, "receipt_share_customers", {
            "code": "K" + short, "name": name, "short_name": short,
            "match_json": json.dumps({"mode": "any", "conditions": [{"field": "sender_name", "op": "eq", "value": short}]})})
        share_id = _insert(connection, "receipt_shares", {
            "customer_id": customer, "request_id": f"00000000-0000-4000-8000-{customer:012d}", "token_sha256": f"{customer:064d}",
            "image_types_json": json.dumps(list(kinds)), "naming_template": rules.DEFAULT_NAMING_TEMPLATE,
            "last_refreshed_at": NOW, **columns})
        token = rules.share_token(SECRET, share_id, 1)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE receipt_shares SET token_sha256 = %s, access_code_sha256 = %s WHERE id = %s",
                           (rules.token_sha256(token), rules.access_code_sha256(SECRET, share_id, code) if code else None, share_id))
        return share_id, token

    def image(share_id, waybill, kind, seq, day, color):
        content = _jpeg(color)
        path = f"ronghui/2026-11/{waybill}/{kind}_{len(nas.files):016d}.jpg"
        nas.files[path] = content
        import hashlib
        image_id = _insert(connection, "receipt_share_images", {
            "platform": "ronghui", "waybill_no": waybill, "kind": kind, "source_key": hashlib.sha256(path.encode()).hexdigest(),
            "seq": seq, "content_sha256": hashlib.sha256(content).hexdigest(), "nas_path": path, "mime_type": "image/jpeg",
            "file_size": len(content), "doc_date": day})
        _insert(connection, "receipt_share_items", {"share_id": share_id, "image_id": image_id, "platform": "ronghui",
                                                    "waybill_no": waybill, "kind": kind, "seq": seq, "doc_date": day})
        return image_id

    mine, token = share("勇胜贸易有限公司", "勇胜")
    other, other_token = share("别家物流有限公司", "别家")
    ids = {
        "sign": image(mine, "R001", "sign", 1, "2026-11-10", (10, 10, 10)),
        "receipt1": image(mine, "R001", "receipt", 1, "2026-11-10", (20, 20, 20)),
        "receipt2": image(mine, "R001", "receipt", 2, "2026-11-10", (30, 30, 30)),
        "label": image(mine, "R002", "sender_label", 1, "2026-11-12", (40, 40, 40)),
        "other": image(other, "R900", "sign", 1, "2026-11-11", (50, 50, 50)),
    }
    connection.commit()
    clock = {"now": NOW}
    service = server.ShareService(
        connect=repository._connection_factory, nas=nas, secret=SECRET, thumbs=server.ThumbCache(tmp_path / "thumbs"),
        templates=Environment(loader=FileSystemLoader(ROOT / "console" / "templates"), autoescape=select_autoescape(["html"])),
        now=lambda: clock["now"])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.build_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path, *, method="GET", body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=20)
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        payload = response.read()
        client.close()
        return response.status, dict(response.getheaders()), payload

    yield type("World", (), {"get": staticmethod(get), "token": token, "other_token": other_token, "ids": ids, "mine": mine,
                             "nas": nas, "connection": connection, "clock": clock, "share": staticmethod(share),
                             "image": staticmethod(image), "service": service, "port": httpd.server_address[1]})
    httpd.shutdown()
    httpd.server_close()
    connection.close()


def _data(world, query=""):
    status, _headers, body = world.get(f"/r/{world.token}/data{query}")
    assert status == 200
    return json.loads(body)["data"]


def test_page_lists_only_this_customer_in_two_separate_tabs(world):
    status, headers, body = world.get(f"/r/{world.token}")
    page = body.decode("utf-8")
    assert status == 200 and "勇胜贸易有限公司" in page and "别家" not in page and "R900" not in page
    assert ">回单</button>" in page and ">寄件主单</button>" in page
    assert headers["X-Robots-Tag"].startswith("noindex") and headers["Referrer-Policy"] == "no-referrer"
    assert "script-src 'nonce-" in headers["Content-Security-Policy"] and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "nas_path" not in page and "ronghui/2026-11" not in page and SECRET not in page
    receipts = _data(world)
    assert receipts["total"] == 3 and {item["kind"] for item in receipts["items"]} == {"签收图", "回单图"}
    # File names follow the link's template; two receipt pictures of one waybill do not collide.
    assert sorted(item["name"] for item in receipts["items"]) == [
        "勇胜-R001-回单图(1).jpg", "勇胜-R001-回单图(2).jpg", "勇胜-R001-签收图.jpg"]
    labels = _data(world, "?tab=label")
    assert [(item["name"], item["kind"]) for item in labels["items"]] == [("勇胜-R002-寄件单.jpg", "寄件单")]
    assert _data(world, "?kind=sign")["total"] == 1 and _data(world, "?q=R002")["total"] == 0
    assert _data(world, "?tab=label&from=2026-11-13")["total"] == 0 and _data(world, "?kind=sender_label")["total"] == 3
    world.connection.commit()
    with world.connection.cursor() as cursor:
        cursor.execute("SELECT view_count FROM receipt_shares WHERE id = %s", (world.mine,))
        assert cursor.fetchone()["view_count"] == 1


def test_pictures_are_reachable_only_through_the_share_that_lists_them(world):
    status, headers, body = world.get(f"/r/{world.token}/img/{world.ids['sign']}")
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and body.startswith(b"\xff\xd8")
    assert headers["Content-Security-Policy"] == "sandbox; default-src 'none'"
    # Another customer's picture id, through my link, does not exist.
    for action in ("img", "file"):
        assert world.get(f"/r/{world.token}/{action}/{world.ids['other']}")[0] == 404
    assert world.get(f"/r/{world.other_token}/img/{world.ids['other']}")[0] == 200
    status, headers, body = world.get(f"/r/{world.token}/file/{world.ids['receipt2']}")
    assert status == 200 and headers["Content-Disposition"] == "attachment; filename*=UTF-8''" + quote("勇胜-R001-回单图(2).jpg", safe="")
    thumb = world.get(f"/r/{world.token}/img/{world.ids['sign']}?thumb=1")[2]
    assert Image.open(io.BytesIO(thumb)).size[0] <= server.THUMB_SIDE and len(thumb) < len(body)
    world.nas.reads.clear()
    world.get(f"/r/{world.token}/img/{world.ids['sign']}?thumb=1")
    assert world.nas.reads == []  # the thumbnail is served from the server cache
    world.nas.down = True
    assert world.get(f"/r/{world.token}/img/{world.ids['receipt1']}")[0] == 503
    assert world.get(f"/r/{world.token}")[0] == 200  # the page still opens while the NAS is offline


@pytest.mark.parametrize("path", [
    "/", "/r", "/r/", "/r/short", "/admin", "/receipts", "/r/{token}/../../etc/passwd", "/r/{token}/img/abc",
    "/r/{token}/img/1/extra", "/r/{token}/unknown", "/static/style.css", "/r/{token}%2f..%2f", "/r/{token}/img/-1",
])
def test_nothing_but_share_paths_exists(world, path):
    assert world.get(path.replace("{token}", world.token))[0] == 404
    assert world.get(path.replace("{token}", world.token), method="POST", body=b"code=1234")[0] == 404


def test_gone_links_all_look_the_same_and_show_no_data(world):
    unknown = rules.share_token(SECRET, 999999, 1)
    disabled_id, disabled = world.share("停用客户有限公司", "停用", status="disabled")
    expired_id, expired = world.share("过期客户有限公司", "过期", expires_at=NOW - timedelta(seconds=1))
    reset_id, old = world.share("重置客户有限公司", "重置")
    new = rules.share_token(SECRET, reset_id, 2)
    with world.connection.cursor() as cursor:
        cursor.execute("UPDATE receipt_shares SET token_version = 2, token_sha256 = %s WHERE id = %s", (rules.token_sha256(new), reset_id))
    world.image(disabled_id, "R700", "sign", 1, "2026-11-10", (1, 1, 1))
    world.connection.commit()
    pages = []
    for token in (unknown, disabled, expired, old):
        status, _headers, body = world.get(f"/r/{token}")
        assert status == 404
        pages.append(body.decode("utf-8").replace(token, "T"))
        assert world.get(f"/r/{token}/data")[0] == 404 and world.get(f"/r/{token}/zip")[0] == 404
    import re
    stripped = {re.sub(r'nonce="[^"]+"', "", page) for page in pages}
    assert len(stripped) == 1 and "链接已失效" in pages[0] and "客户" not in pages[0].split("链接已失效")[0][-200:]
    assert world.get(f"/r/{new}")[0] == 200
    # Expiry is checked on every request, not only when the page was opened.
    world.clock["now"] = NOW + timedelta(days=400)
    with world.connection.cursor() as cursor:
        cursor.execute("UPDATE receipt_shares SET expires_at = %s WHERE id = %s", (NOW + timedelta(days=30), world.mine))
    world.connection.commit()
    assert world.get(f"/r/{world.token}/img/{world.ids['sign']}")[0] == 404


def test_access_code_gates_page_data_and_pictures(world):
    share_id, token = world.share("加密客户有限公司", "加密", code="4821")
    image_id = world.image(share_id, "R800", "sign", 1, "2026-11-10", (9, 9, 9))
    world.connection.commit()
    status, _headers, body = world.get(f"/r/{token}")
    assert status == 200 and "请输入访问码" in body.decode("utf-8") and "加密客户" not in body.decode("utf-8")
    assert world.get(f"/r/{token}/data")[0] == 403 and world.get(f"/r/{token}/img/{image_id}")[0] == 403
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert world.get(f"/r/{token}/code", method="POST", body=b"code=0000", headers=form)[0] == 403
    status, headers, _body = world.get(f"/r/{token}/code", method="POST", body=b"code=4821", headers=form)
    cookie = headers["Set-Cookie"]
    assert status == 303 and all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=Strict", f"Path=/r/{token}"))
    session = {"Cookie": cookie.split(";")[0]}
    assert "加密客户有限公司" in world.get(f"/r/{token}", headers=session)[2].decode("utf-8")
    assert world.get(f"/r/{token}/img/{image_id}", headers=session)[0] == 200
    # The cookie of one link opens no other link.
    name = cookie.split("=")[0]
    assert world.get(f"/r/{world.token}/data", headers={"Cookie": f"{name}=forged"})[0] == 200  # no code on that link
    other_id, other = world.share("另一加密客户", "另一", code="1111")
    world.connection.commit()
    assert world.get(f"/r/{other}/data", headers={"Cookie": f"rs_{other[:8]}={cookie.split('=')[1].split(';')[0]}"})[0] == 403


def test_zip_uses_the_naming_rule_and_respects_filters(world):
    status, headers, body = world.get(f"/r/{world.token}/zip?tab=receipt")
    assert status == 200 and headers["Content-Type"] == "application/zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(body)).namelist())
    assert names == ["勇胜-R001-回单图(1).jpg", "勇胜-R001-回单图(2).jpg", "勇胜-R001-签收图.jpg"]
    assert zipfile.ZipFile(io.BytesIO(world.get(f"/r/{world.token}/zip?tab=label")[2])).namelist() == ["勇胜-R002-寄件单.jpg"]
    assert world.get(f"/r/{world.token}/zip?q=NOPE")[0] == 404
    world.connection.commit()
    with world.connection.cursor() as cursor:
        cursor.execute("SELECT action FROM receipt_share_access_logs WHERE share_id = %s ORDER BY id", (world.mine,))
        assert [row["action"] for row in cursor.fetchall()] == ["zip", "zip"]


def test_rate_limits_and_wrong_code_attempts_are_bounded(world):
    world.service.limiter = server.RateLimiter()
    statuses = [world.get(f"/r/{world.token}/zip?q=NOPE", headers={"X-Real-IP": "203.0.113.9"})[0] for _ in range(7)]
    assert statuses[:5] == [404] * 5 and statuses[5:] == [429, 429]
    assert world.get(f"/r/{world.token}/zip?q=NOPE", headers={"X-Real-IP": "203.0.113.10"})[0] == 404  # per client
    share_id, token = world.share("限流客户", "限流", code="4821")
    world.connection.commit()
    form = {"Content-Type": "application/x-www-form-urlencoded", "X-Real-IP": "203.0.113.11"}
    tries = [world.get(f"/r/{token}/code", method="POST", body=f"code={n:04d}".encode(), headers=form)[0] for n in range(12)]
    assert tries[:10] == [403] * 10 and tries[10:] == [429, 429]


def test_customer_page_works_in_a_real_browser_under_its_security_policy(world):
    playwright = pytest.importorskip("playwright.sync_api")
    problems = []

    def until(page, expression):
        # The page's policy forbids eval, so poll from outside instead of wait_for_function.
        import time
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if page.evaluate(expression):
                return
            time.sleep(0.1)
        raise AssertionError(f"condition not reached: {expression}")

    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(8000)
        page.on("pageerror", lambda error: problems.append(str(error)))
        page.on("console", lambda message: problems.append(message.text) if message.type == "error" else None)
        page.goto(f"http://127.0.0.1:{world.port}/r/{world.token}")
        until(page, "document.querySelectorAll('.card img').length === 3 && "
                               "[...document.querySelectorAll('.card img')].every(i => i.complete && i.naturalWidth > 0)")
        assert page.locator("h1").inner_text() == "勇胜贸易有限公司"
        assert page.locator("[data-total]").inner_text() == "共 3 张图片"
        assert page.locator(".grid").evaluate("node => getComputedStyle(node).gridTemplateColumns.split(' ').length") == 4
        assert page.locator("[data-kind] option").all_inner_texts() == ["全部类型", "签收图", "回单图"]
        page.locator("[data-kind]").select_option("receipt")
        until(page, "document.querySelectorAll('.card').length === 2")
        page.locator('[data-tab="label"]').click()
        until(page, "document.querySelectorAll('.card').length === 1")
        assert page.locator(".card .name").inner_text() == "勇胜-R002-寄件单.jpg"
        assert page.locator("[data-kind]").is_hidden()  # one kind in this tab: no type filter
        assert page.locator("[data-zip]").get_attribute("href") == f"/r/{world.token}/zip?tab=label"
        page.locator(".card .thumb").click()
        until(page, "document.querySelector('[data-viewer]').open && "
                               "document.querySelector('[data-viewer-image]').naturalWidth === 900")
        page.locator("[data-viewer-close]").click()
        page.set_viewport_size({"width": 390, "height": 800})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        # A link disabled while the page is open stops answering on the next action.
        with world.connection.cursor() as cursor:
            cursor.execute("UPDATE receipt_shares SET status = 'disabled' WHERE id = %s", (world.mine,))
        world.connection.commit()
        page.locator('[data-tab="receipt"]').click()
        until(page, "document.body.textContent.includes('链接已失效')")
        browser.close()
    assert [item for item in problems if "404" not in item] == []


def test_backend_failures_answer_plainly_and_other_methods_are_refused(world, monkeypatch):
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        assert world.get(f"/r/{world.token}", method=method, body=b"x")[0] == 404

    def broken():
        raise OSError("database unreachable")

    monkeypatch.setattr(world.service, "connect", broken)
    status, headers, body = world.get(f"/r/{world.token}")
    assert status == 503 and "暂时不可用" in body.decode("utf-8") and "Traceback" not in body.decode("utf-8")
    assert headers["X-Content-Type-Options"] == "nosniff"
    big = world.get(f"/r/{world.token}/code", method="POST", body=b"code=" + b"1" * 400,
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert big[0] in {403, 404, 503}
