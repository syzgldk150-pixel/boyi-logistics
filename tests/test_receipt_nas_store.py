"""The NAS archive against a real local WebDAV-like HTTP server."""
import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from shared.receipt_nas_store import NasStoreError, ReceiptNasStore

AUTH = "Basic " + base64.b64encode(b"rw:secret").decode()


class _Dav(BaseHTTPRequestHandler):
    files: dict = {}
    folders: set = set()
    log: list = []

    def log_message(self, *_args):
        pass

    def _go(self):
        path = unquote(self.path)
        self.log.append((self.command, path))
        if self.headers.get("Authorization") != AUTH:
            return self._reply(401)
        if self.command == "MKCOL":
            if path in self.folders:
                return self._reply(405)
            self.folders.add(path)
            return self._reply(201)
        if self.command == "PUT":
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if path in self.files and self.headers.get("If-None-Match") == "*":
                return self._reply(412)
            if path.rsplit("/", 1)[0] not in self.folders:
                return self._reply(409)
            self.files[path] = body
            return self._reply(201)
        if path == "/dav/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path not in self.files:
            return self._reply(404)
        return self._reply(200, b"" if self.command == "HEAD" else self.files[path], len(self.files[path]))

    def _reply(self, status, body=b"", length=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        if body:
            self.wfile.write(body)

    do_GET = do_HEAD = do_PUT = do_MKCOL = _go


@pytest.fixture
def dav():
    _Dav.files, _Dav.folders, _Dav.log = {}, {"/dav"}, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Dav)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/dav"
    server.shutdown()
    server.server_close()


PATH = "ronghui/2026-11/R00024388425/sign_aaaaaaaaaaaaaaaa.jpg"


def test_write_once_then_read_and_stream(dav):
    store = ReceiptNasStore(dav, "rw", "secret")
    assert store.exists(PATH) is False
    assert store.put_if_absent(PATH, b"image-1") is True
    assert store.put_if_absent(PATH, b"image-2") is False  # never overwritten
    assert store.read(PATH) == b"image-1" == b"".join(store.stream(PATH))
    assert {"/dav/ronghui", "/dav/ronghui/2026-11", "/dav/ronghui/2026-11/R00024388425"} <= _Dav.folders


def test_concurrent_writer_and_existing_folders_are_tolerated(dav):
    store = ReceiptNasStore(dav, "rw", "secret")
    store.put_if_absent(PATH, b"a")
    other = "ronghui/2026-11/R00024388425/receipt_bbbbbbbbbbbbbbbb.jpg"
    assert store.put_if_absent(other, b"b") is True  # folders already exist (405) is fine


@pytest.mark.parametrize("bad", ["../secret.jpg", "a/../../b.jpg", "/etc/passwd", "a//b.jpg", "a/b c.jpg", "a/%2e%2e/b.jpg", ""])
def test_paths_outside_the_folder_are_never_requested(dav, bad):
    store = ReceiptNasStore(dav, "rw", "secret")
    for call in (store.exists, store.read, lambda path: store.put_if_absent(path, b"x")):
        with pytest.raises(NasStoreError) as error:
            call(bad)
        assert error.value.code == "NAS_PATH_INVALID"
    assert _Dav.log == []


def test_errors_are_classified_without_leaking_credentials(dav):
    with pytest.raises(NasStoreError) as denied:
        ReceiptNasStore(dav, "rw", "wrong").exists(PATH)
    assert denied.value.code == "NAS_AUTH_FAILED" and "wrong" not in str(denied.value)
    with pytest.raises(NasStoreError) as missing:
        ReceiptNasStore(dav, "rw", "secret").read(PATH)
    assert missing.value.code == "NAS_NOT_FOUND"
    with pytest.raises(NasStoreError) as offline:
        ReceiptNasStore("http://127.0.0.1:1/dav", "rw", "secret", timeout=2).exists(PATH)
    assert offline.value.code == "NAS_UNAVAILABLE" and "127.0.0.1" not in str(offline.value)
    with pytest.raises(NasStoreError):
        ReceiptNasStore(dav, "rw", "secret").read("redirect")  # a redirect is not followed with credentials
    for bad_url in ("ftp://nas/dav", "", "http:///dav", "http://nas/dav?x=1"):
        with pytest.raises(NasStoreError):
            ReceiptNasStore(bad_url, "rw", "secret")


def test_empty_or_oversized_content_is_refused(dav):
    store = ReceiptNasStore(dav, "rw", "secret")
    with pytest.raises(NasStoreError):
        store.put_if_absent(PATH, b"")
