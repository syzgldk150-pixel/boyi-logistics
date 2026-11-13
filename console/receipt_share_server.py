"""Public, read-only customer share service (spec ⑩c) — a separate process.

It serves only ``/r/{link code}`` paths: the page, its data, pictures,
single downloads and a zip. It has its own settings file, a read-only NAS
account and its own database account; it holds no Console session, no Agent
address and no platform login, and imports nothing from the Console.

Every request re-reads the share by the digest of the link code, so disabling,
resetting or expiring a link takes effect at once. A link that is unknown,
disabled, reset or expired gets the same page with no data. Pictures are
addressed by ids that are valid only inside the share that lists them.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import zipfile
from contextlib import closing
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, quote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from shared import receipt_share as rules
from shared import receipt_share_repository as repository
from shared.receipt_nas_store import NasStoreError, ReceiptNasStore

LOGGER = logging.getLogger("receipt_share")
PAGE_SIZE = 24
ZIP_MAX_IMAGES = 500
THUMB_SIDE = 560
THUMB_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
TABS = {"receipt": ("sign", "receipt"), "label": ("sender_label",)}
_PATH = re.compile(r"/r/([A-Za-z0-9_-]{22})(?:/(data|zip|code|img/(\d{1,18})|file/(\d{1,18})))?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# requests allowed per window, per client address
RATE_LIMITS = {"page": (60, 60.0), "image": (600, 60.0), "zip": (5, 600.0), "code": (10, 600.0)}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow, noarchive",
    "X-Frame-Options": "DENY", "Cache-Control": "private, no-store", "Cross-Origin-Resource-Policy": "same-origin",
}


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock, self._lock, self._hits = clock, threading.Lock(), {}

    def allow(self, bucket: str, key: str) -> bool:
        limit, window = RATE_LIMITS[bucket]
        now = self._clock()
        with self._lock:
            if len(self._hits) > 20000:
                self._hits.clear()
            hits = [stamp for stamp in self._hits.get((bucket, key), []) if now - stamp < window]
            allowed = len(hits) < limit
            if allowed:
                hits.append(now)
            self._hits[(bucket, key)] = hits
            return allowed


class ThumbCache:
    """Bounded on-disk thumbnails keyed by content digest; originals stay on the NAS."""

    def __init__(self, directory: Path, max_bytes: int = THUMB_CACHE_MAX_BYTES) -> None:
        self._directory, self._max_bytes, self._lock = directory, max_bytes, threading.Lock()

    def _path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid digest")
        return self._directory / digest[:2] / f"{digest}.jpg"

    def get(self, digest: str) -> bytes | None:
        path = self._path(digest)
        try:
            data = path.read_bytes()
            os.utime(path, None)
            return data
        except OSError:
            return None

    def put(self, digest: str, data: bytes) -> None:
        path = self._path(digest)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
            self._trim()

    def _trim(self) -> None:
        files = [(item.stat().st_mtime, item.stat().st_size, item) for item in self._directory.glob("*/*.jpg")]
        total = sum(size for _mtime, size, _item in files)
        for _mtime, size, item in sorted(files):
            if total <= self._max_bytes:
                break
            item.unlink(missing_ok=True)
            total -= size


def make_thumbnail(content: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        image = image.convert("RGB")
        image.thumbnail((THUMB_SIDE, THUMB_SIDE))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=78)
        return output.getvalue()


class ShareService:
    def __init__(self, *, connect: Callable[[], Any], nas: ReceiptNasStore, secret: str, thumbs: ThumbCache,
                 templates: Environment, now: Callable[[], datetime] = datetime.now,
                 limiter: RateLimiter | None = None) -> None:
        if len(secret) < 32:
            raise ValueError("share secret is not configured")
        self.connect, self.nas, self.secret, self.thumbs = connect, nas, secret, thumbs
        self.templates, self.now, self.limiter = templates, now, limiter or RateLimiter()

    # -- share state -------------------------------------------------------
    def load(self, token: str) -> tuple[dict[str, Any] | None, str]:
        """``(share, state)`` with state ``ok`` / ``gone``; nothing distinguishes why a link is gone."""

        with closing(self.connect()) as connection:
            share = repository.public_share(connection, rules.token_sha256(token))
            connection.commit()
        if (share is None or share["status"] != "active" or not share["customer_active"]
                or (share["expires_at"] is not None and share["expires_at"] <= self.now())
                # the digest was found, but only the current code version is valid
                or not hmac.compare_digest(rules.share_token(self.secret, share["id"], share["token_version"]), token)):
            return None, "gone"
        return share, "ok"

    def code_cookie(self, share: Mapping[str, Any]) -> str:
        message = f"receipt-share-cookie:{share['id']}:{share['token_version']}:{share['access_code_sha256']}"
        return hmac.new(self.secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    def code_matches(self, share: Mapping[str, Any], code: str) -> bool:
        try:
            code = rules.normalize_access_code(code)
        except rules.ReceiptShareError:
            return False
        return bool(code) and hmac.compare_digest(
            rules.access_code_sha256(self.secret, share["id"], code), str(share["access_code_sha256"]))

    # -- data --------------------------------------------------------------
    def file_name(self, share: Mapping[str, Any], item: Mapping[str, Any]) -> str:
        return rules.render_file_name(
            share["naming_template"], short_name=share["customer_short_name"], waybill_no=item["waybill_no"],
            kind=item["kind"], doc_date=item["doc_date"], seq=item["seq"], of=item["same_kind"])

    def listing(self, share: Mapping[str, Any], query: Mapping[str, list[str]], *, limit: int = PAGE_SIZE,
                all_pages: bool = False) -> dict[str, Any]:
        def text(name: str) -> str:
            return str((query.get(name) or [""])[0] or "").strip()

        tab = text("tab") if text("tab") in TABS else "receipt"
        allowed = [kind for kind in TABS[tab] if kind in share["image_types"]]
        kind = text("kind")
        kinds = [kind] if kind in allowed else allowed
        keyword = text("q")[:40] if re.fullmatch(r"[A-Za-z0-9-]{0,40}", text("q")) else ""
        date_from = text("from") if _DATE.fullmatch(text("from")) else ""
        date_to = text("to") if _DATE.fullmatch(text("to")) else ""
        try:
            page = max(1, min(int(text("page") or "1"), 100000))
        except ValueError:
            page = 1
        with closing(self.connect()) as connection:
            total, rows = repository.public_items(
                connection, share["id"], kinds=kinds, keyword=keyword, date_from=date_from, date_to=date_to,
                limit=limit, offset=0 if all_pages else (page - 1) * limit)
            connection.commit()
        return {
            "tab": tab, "kind": kind if kind in allowed else "", "page": page, "page_size": limit, "total": total,
            "pages": max(1, -(-total // limit)),
            "items": [{"id": row["image_id"], "name": self.file_name(share, row), "waybill_no": row["waybill_no"],
                       "kind": rules.IMAGE_KIND_LABELS[row["kind"]],
                       "date": row["doc_date"].isoformat() if row["doc_date"] else ""} for row in rows],
            "_rows": rows,
        }

    def image(self, share: Mapping[str, Any], image_id: int) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = repository.public_image(connection, share["id"], image_id)
            connection.commit()
        return row

    def record(self, share: Mapping[str, Any], action: str, client_ip: str, user_agent: str) -> None:
        try:
            with closing(self.connect()) as connection:
                repository.record_access(connection, share["id"], action=action, client_ip=client_ip,
                                         user_agent=user_agent, now=self.now())
                connection.commit()
        except Exception:  # noqa: BLE001 - statistics must never block a customer
            LOGGER.warning("share access not recorded")


def build_handler(service: ShareService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "share"
        sys_version = ""

        def log_message(self, *_args: Any) -> None:  # never log link codes
            return

        # -- plumbing ------------------------------------------------------
        def _client(self) -> str:
            # Only the local reverse proxy connects; it sets the real client address.
            return str(self.headers.get("X-Real-IP") or self.client_address[0])[:64]

        def _send(self, status: int, body: bytes, content_type: str, extra: Mapping[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _page(self, status: int, **context: Any) -> None:
            nonce = secrets.token_urlsafe(18)
            body = service.templates.get_template("share_public.html").render(nonce=nonce, **context).encode("utf-8")
            self._send(status, body, "text/html; charset=utf-8", {
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self'; "
                    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")})

        def _not_found(self) -> None:
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")

        def _limited(self, bucket: str) -> bool:
            if service.limiter.allow(bucket, self._client()):
                return False
            self._send(HTTPStatus.TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试。".encode("utf-8"),
                       "text/plain; charset=utf-8", {"Retry-After": "60"})
            return True

        def _has_code(self, share: Mapping[str, Any], token: str) -> bool:
            if not share["access_code_sha256"]:
                return True
            wanted = service.code_cookie(share)
            for part in str(self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == f"rs_{token[:8]}" and hmac.compare_digest(value, wanted):
                    return True
            return False

        # -- routes --------------------------------------------------------
        def do_HEAD(self) -> None:  # noqa: N802
            self._not_found()

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            match = _PATH.fullmatch(parts.path)
            if match is None:
                return self._not_found()
            token, action = match.group(1), match.group(2) or ""
            bucket = "image" if action.startswith(("img/", "file/")) else ("zip" if action == "zip" else "page")
            if self._limited(bucket):
                return None
            share, state = service.load(token)
            query = parse_qs(parts.query)
            if share is None:
                if action:
                    return self._not_found()
                return self._page(HTTPStatus.NOT_FOUND, state=state, token=token)
            if not self._has_code(share, token):
                if action:
                    return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "请先输入访问码。"})
                return self._page(HTTPStatus.OK, state="code", token=token, customer="")
            user_agent = str(self.headers.get("User-Agent") or "")
            if not action:
                service.record(share, "view", self._client(), user_agent)
                return self._page(HTTPStatus.OK, state="ok", token=token, **self._context(share, query))
            if action == "data":
                listing = service.listing(share, query)
                listing.pop("_rows")
                return self._json(HTTPStatus.OK, {"ok": True, "data": listing})
            if action == "zip":
                return self._zip(share, query, user_agent)
            return self._picture(share, int(match.group(3) or match.group(4)), download=action.startswith("file/"),
                                 thumb=query.get("thumb") == ["1"])

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            match = _PATH.fullmatch(parts.path)
            if match is None or match.group(2) != "code":
                return self._not_found()
            token = match.group(1)
            if self._limited("code"):
                return None
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            raw = self.rfile.read(length) if 0 <= length <= 256 else b""
            share, _state = service.load(token)
            code = (parse_qs(raw.decode("utf-8", "replace")).get("code") or [""])[0]
            if share is None or not share["access_code_sha256"] or not service.code_matches(share, code):
                if share is None:
                    return self._page(HTTPStatus.NOT_FOUND, state="gone", token=token)
                return self._page(HTTPStatus.FORBIDDEN, state="code", token=token, customer="", wrong=True)
            cookie = (f"rs_{token[:8]}={service.code_cookie(share)}; Path=/r/{token}; HttpOnly; Secure; "
                      "SameSite=Strict; Max-Age=43200")
            self.send_response(HTTPStatus.SEE_OTHER)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Set-Cookie", cookie)
            self.send_header("Location", f"/r/{token}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        def _context(self, share: Mapping[str, Any], query: Mapping[str, list[str]]) -> dict[str, Any]:
            listing = service.listing(share, query)
            listing.pop("_rows")
            tabs = [{"key": key, "label": "回单" if key == "receipt" else "寄件主单",
                     "kinds": [{"value": kind, "label": rules.IMAGE_KIND_LABELS[kind]} for kind in kinds
                               if kind in share["image_types"]]}
                    for key, kinds in TABS.items() if any(kind in share["image_types"] for kind in kinds)]
            if listing["tab"] not in {tab["key"] for tab in tabs} and tabs:
                listing = service.listing(share, {**query, "tab": [tabs[0]["key"]]})
                listing.pop("_rows")
            naming = re.sub(r"\.(jpg|jpeg|png)$", "", share["naming_template"], flags=re.IGNORECASE)
            return {
                "customer": share["customer_name"], "tabs": tabs, "listing": listing,
                "naming": naming.replace("{", "").replace("}", ""),
                "updated": share["last_refreshed_at"].strftime("%Y-%m-%d %H:%M") if share["last_refreshed_at"] else "",
                "expires": f"有效期至 {share['expires_at']:%Y-%m-%d}" if share["expires_at"] else "长期有效",
            }

        def _picture(self, share: Mapping[str, Any], image_id: int, *, download: bool, thumb: bool) -> None:
            row = service.image(share, image_id)
            if row is None:
                return self._not_found()
            try:
                if thumb and not download:
                    content = service.thumbs.get(row["content_sha256"])
                    if content is None:
                        content = make_thumbnail(service.nas.read(row["nas_path"]))
                        service.thumbs.put(row["content_sha256"], content)
                    return self._send(HTTPStatus.OK, content, "image/jpeg", {"Cache-Control": "private, max-age=86400"})
                content = service.nas.read(row["nas_path"])
            except NasStoreError:
                return self._send(HTTPStatus.SERVICE_UNAVAILABLE, "图片暂时无法读取，请稍后再试。".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            except Exception:  # noqa: BLE001 - an undecodable archive file
                return self._not_found()
            mime_type = row["mime_type"] if row["mime_type"] in {"image/jpeg", "image/png"} else "application/octet-stream"
            extra = {"Content-Security-Policy": "sandbox; default-src 'none'"}
            if download:
                name = service.file_name(share, row)
                extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name, safe='')}"
                service.record(share, "download", self._client(), str(self.headers.get("User-Agent") or ""))
            return self._send(HTTPStatus.OK, content, mime_type, extra)

        def _zip(self, share: Mapping[str, Any], query: Mapping[str, list[str]], user_agent: str) -> None:
            listing = service.listing(share, query, limit=ZIP_MAX_IMAGES + 1, all_pages=True)
            rows = listing["_rows"]
            if not rows:
                return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "当前筛选下没有图片。"})
            if len(rows) > ZIP_MAX_IMAGES:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {
                    "ok": False, "message": f"一次最多打包 {ZIP_MAX_IMAGES} 张，请用日期或单号缩小范围。"})
            with closing(service.connect()) as connection:
                paths = {row["image_id"]: repository.public_image(connection, share["id"], row["image_id"]) for row in rows}
                connection.commit()
            service.record(share, "zip", self._client(), user_agent)
            self.send_response(HTTPStatus.OK)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            archive_name = f"{share['customer_short_name']}-{service.now():%Y%m%d}.zip"
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(archive_name, safe='')}")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            used: set[str] = set()
            try:
                with zipfile.ZipFile(_Unseekable(self.wfile), "w", zipfile.ZIP_STORED) as archive:
                    for row in rows:
                        detail = paths.get(row["image_id"])
                        if detail is None:
                            continue
                        name = service.file_name(share, row)
                        if name in used:  # a template without the fields that keep names apart
                            stem, dot, extension = name.rpartition(".")
                            name = f"{stem}-{row['image_id']}{dot}{extension}"
                        used.add(name)
                        try:
                            archive.writestr(name, service.nas.read(detail["nas_path"]))
                        except NasStoreError:
                            archive.writestr(name + ".读取失败.txt", "这张图片暂时无法读取，请稍后单独下载。")
            except (BrokenPipeError, ConnectionResetError):
                return None
            return None

    return Handler


class _Unseekable:
    """Lets ``zipfile`` stream to a socket (it then writes data descriptors instead of seeking)."""

    def __init__(self, raw: Any) -> None:
        self._raw, self._written = raw, 0

    def write(self, data: bytes) -> int:
        self._raw.write(data)
        self._written += len(data)
        return len(data)

    def tell(self) -> int:
        return self._written

    def flush(self) -> None:
        self._raw.flush()


def load_public_settings(path: str) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.lstrip().startswith("#"):
            settings[key.strip()] = value
    return settings


def main() -> None:
    """Entry point of ``receipt-share.service``; all configuration comes from its own settings file."""

    import pymysql

    from shared.mysql_connection import mysql_tls_options

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    settings = load_public_settings(os.environ.get("BOYI_RECEIPT_SHARE_PUBLIC_ENV_FILE", "~/.boyi-receipt-share/public.env"))

    def connect() -> Any:
        return pymysql.connect(
            host=settings["SHARE_DB_HOST"], port=int(settings.get("SHARE_DB_PORT") or 3306),
            user=settings["SHARE_DB_USER"], password=settings["SHARE_DB_PASS"],
            database=settings.get("SHARE_DB_NAME") or "agent_db", charset="utf8mb4", autocommit=False,
            cursorclass=pymysql.cursors.DictCursor, connect_timeout=10, read_timeout=30, write_timeout=30,
            **mysql_tls_options(settings.get("SHARE_DB_SSL_CA") or None))

    folder = settings.get("RECEIPT_NAS_FOLDER") or "/团队文件-物流共享资料/回单归档"
    nas = ReceiptNasStore(
        settings["RECEIPT_NAS_URL"].rstrip("/") + "/".join(quote(part, safe="") for part in folder.split("/")),
        settings["RECEIPT_NAS_RO_USER"], settings["RECEIPT_NAS_RO_PASS"])
    here = Path(__file__).resolve().parent
    service = ShareService(
        connect=connect, nas=nas, secret=settings["RECEIPT_SHARE_TOKEN_SECRET"],
        thumbs=ThumbCache(Path(settings.get("SHARE_THUMB_DIR") or here / "runtime" / "share_thumbs")),
        templates=Environment(loader=FileSystemLoader(here / "templates"), autoescape=select_autoescape(["html"])))
    server = ThreadingHTTPServer(("127.0.0.1", int(settings.get("SHARE_PORT") or 8766)), build_handler(service))
    server.daemon_threads = True
    LOGGER.info("receipt share service listening on 127.0.0.1:%s", server.server_address[1])
    server.serve_forever()


if __name__ == "__main__":
    main()
