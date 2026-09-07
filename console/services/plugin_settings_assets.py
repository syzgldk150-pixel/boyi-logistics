"""Assemble verified package settings assets without granting iframe cookies."""
from __future__ import annotations

from collections.abc import Callable
from html import escape
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re


class SettingsAssetError(ValueError):
    """The package cannot be rendered using the isolated settings contract."""


AssetReader = Callable[[str], tuple[bytes, str]]
_MAX_BYTES = 2 * 1024 * 1024
_JAVASCRIPT_TYPES = {"", "text/javascript", "application/javascript"}


class _SettingsHTML(HTMLParser):
    def __init__(self, entry: str, reader: AssetReader, nonce: str):
        super().__init__(convert_charrefs=False)
        self.directory = PurePosixPath(entry).parent
        self.reader = reader
        self.nonce = nonce
        self.output: list[str] = []
        self.raw_tag: str | None = None
        self.raw_data: list[str] = []
        self.raw_source: str | None = None
        self.total_bytes = 0
        self.asset_count = 0

    def _read(self, reference: str, kind: str) -> str:
        # Deliberately only same-directory files: no URLs, queries, encoded paths,
        # base changes, or additional cookie-bearing subresource requests.
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", reference):
            raise SettingsAssetError("unsupported settings asset reference")
        self.asset_count += 1
        if self.asset_count > 32:
            raise SettingsAssetError("too many settings assets")
        raw, content_type = self.reader(str(self.directory / reference))
        allowed = _JAVASCRIPT_TYPES - {""} if kind == "script" else {"text/css"}
        if not isinstance(raw, bytes) or content_type.lower() not in allowed:
            raise SettingsAssetError("settings asset content type mismatch")
        self.total_bytes += len(raw)
        if self.total_bytes > _MAX_BYTES:
            raise SettingsAssetError("settings document is too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SettingsAssetError("settings assets must use UTF-8") from exc
        if re.search(r"</" + kind, text, re.IGNORECASE):
            raise SettingsAssetError("settings asset contains a raw closing tag")
        return text

    def _inline(self, kind: str, text: str) -> None:
        self.output.append(f'<{kind} nonce="{self.nonce}">{text}</{kind}>')

    def handle_starttag(self, tag, attrs):
        if self.raw_tag is not None:
            raise SettingsAssetError("malformed settings script or style")
        attributes = dict(attrs)
        if len(attributes) != len(attrs):
            raise SettingsAssetError("duplicate settings HTML attributes")
        if tag == "base" or (tag == "meta" and "http-equiv" in attributes):
            raise SettingsAssetError("settings document redirects are unsupported")
        if tag == "link":
            if attributes.get("rel", "").lower() != "stylesheet" or set(attributes) - {"rel", "href", "type"}:
                raise SettingsAssetError("unsupported settings link")
            self._inline("style", self._read(attributes.get("href") or "", "style"))
        elif tag in {"script", "style"}:
            allowed = {"src", "type"} if tag == "script" else {"type"}
            if set(attributes) - allowed:
                raise SettingsAssetError("unsupported settings script or style attributes")
            expected_types = _JAVASCRIPT_TYPES if tag == "script" else {"", "text/css"}
            if (attributes.get("type") or "").lower() not in expected_types:
                raise SettingsAssetError("unsupported settings script or style type")
            self.raw_tag = tag
            self.raw_data = []
            self.raw_source = self._read(attributes["src"] or "", tag) if "src" in attributes else None
        else:
            self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.raw_tag:
            if tag != self.raw_tag:
                raise SettingsAssetError("malformed settings script or style")
            body = "".join(self.raw_data)
            if self.raw_source is not None and body.strip():
                raise SettingsAssetError("settings script has both source and inline body")
            self._inline(tag, self.raw_source if self.raw_source is not None else body)
            self.raw_tag = None
            self.raw_source = None
            self.raw_data = []
        else:
            self.output.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        if tag in {"script", "style"}:
            raise SettingsAssetError("self-closing settings script or style")
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        (self.raw_data if self.raw_tag else self.output).append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")

    def handle_comment(self, data):
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.output.append(f"<!{decl}>")


def assemble_settings_html(document: bytes, *, entry: str, reader: AssetReader, nonce: str) -> bytes:
    """Inline JS/CSS read with the same authenticated package authority as HTML."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", nonce):
        raise SettingsAssetError("invalid settings response nonce")
    parser = _SettingsHTML(entry, reader, escape(nonce, quote=True))
    parser.total_bytes = len(document)
    if parser.total_bytes > _MAX_BYTES:
        raise SettingsAssetError("settings document is too large")
    try:
        parser.feed(document.decode("utf-8"))
        parser.close()
    except UnicodeDecodeError as exc:
        raise SettingsAssetError("settings document must use UTF-8") from exc
    if parser.raw_tag is not None:
        raise SettingsAssetError("unclosed settings script or style")
    return "".join(parser.output).encode("utf-8")
