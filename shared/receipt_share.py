"""Rules shared by the Console, the Agent refresh job and the public share service.

One place defines: the three image kinds, link tokens, access codes, the
customer sender-matching rules and the file-naming template. No environment,
database or network access happens here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import date, datetime
from typing import Any, Mapping, Sequence

# Fixed display order; a share lists the kinds it includes in this order.
IMAGE_KINDS: tuple[str, ...] = ("sender_label", "sign", "receipt")
IMAGE_KIND_LABELS: dict[str, str] = {"sender_label": "寄件单", "sign": "签收图", "receipt": "回单图"}
SHARE_SCOPES: tuple[str, ...] = ("all", "approved", "by_date")
SHARE_PLATFORMS: tuple[str, ...] = ("ronghui", "yunda", "boyi")
PLATFORM_LABELS: dict[str, str] = {"ronghui": "融辉", "yunda": "韵达", "boyi": "博益"}

# Sender fields a customer rule may test, and the comparisons allowed.
MATCH_FIELDS: dict[str, str] = {"sender_name": "发货人", "sender_phone": "发货电话", "sender_address": "发货地址"}
MATCH_OPS: tuple[str, ...] = ("eq", "contains")
MAX_MATCH_CONDITIONS = 20

DEFAULT_NAMING_TEMPLATE = "{客户简称}-{运单号}-{单据类型}.jpg"
NAMING_FIELDS: tuple[str, ...] = ("客户简称", "运单号", "单据类型", "开单日期", "序号")
_TEMPLATE_FIELD = re.compile(r"\{([^{}]*)\}")
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_ACCESS_CODE_RE = re.compile(r"[0-9A-Za-z]{4}")


class ReceiptShareError(ValueError):
    """A share setting is invalid; the message is safe to show to an administrator."""


def share_token(secret: str, share_id: int, token_version: int) -> str:
    """Derive the link code. Only its SHA-256 is stored; the Console re-derives it to copy."""

    if not secret or len(secret) < 32:
        raise ReceiptShareError("共享链接密钥未配置。")
    digest = hmac.new(secret.encode("utf-8"), f"receipt-share:{int(share_id)}:{int(token_version)}".encode("ascii"),
                      hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode("ascii").rstrip("=")  # 128 bits, 22 characters


def token_sha256(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def is_token_shape(value: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(str(value or "")))


def normalize_access_code(value: Any) -> str:
    code = str(value or "").strip()
    if code and not _ACCESS_CODE_RE.fullmatch(code):
        raise ReceiptShareError("访问码为 4 位数字或字母。")
    return code


def access_code_sha256(secret: str, share_id: int, code: str) -> str:
    """Keyed digest so a leaked table does not reveal the short code."""

    return hmac.new(secret.encode("utf-8"), f"receipt-share-code:{int(share_id)}:{code}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _access_code_key(secret: str, share_id: int, purpose: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), f"receipt-share-code-{purpose}:{int(share_id)}".encode("ascii"),
                    hashlib.sha256).digest()


def seal_access_code(secret: str, share_id: int, code: str) -> str:
    """Encrypt-then-MAC a short code for one share (HMAC-SHA256 keystream; standard library only)."""

    import os

    data = normalize_access_code(code).encode("ascii")
    if not data:
        raise ReceiptShareError("访问码为空。")
    nonce = os.urandom(16)
    stream = hmac.new(_access_code_key(secret, share_id, "enc"), nonce, hashlib.sha256).digest()
    cipher = bytes(left ^ right for left, right in zip(data, stream))
    tag = hmac.new(_access_code_key(secret, share_id, "mac"), nonce + cipher, hashlib.sha256).digest()[:16]
    return "v1." + base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii").rstrip("=")


def open_access_code(secret: str, share_id: int, sealed: str | None) -> str:
    """The code sealed for this share, or "" when absent, tampered with or sealed for another share."""

    text = str(sealed or "")
    if not text.startswith("v1."):
        return ""
    try:
        raw = base64.urlsafe_b64decode(text[3:] + "=" * (-len(text[3:]) % 4))
    except (ValueError, TypeError):
        return ""
    if len(raw) < 16 + 1 + 16:
        return ""
    nonce, cipher, tag = raw[:16], raw[16:-16], raw[-16:]
    expected = hmac.new(_access_code_key(secret, share_id, "mac"), nonce + cipher, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected) or len(cipher) > 32:
        return ""
    stream = hmac.new(_access_code_key(secret, share_id, "enc"), nonce, hashlib.sha256).digest()
    try:
        return normalize_access_code(bytes(left ^ right for left, right in zip(cipher, stream)).decode("ascii"))
    except (UnicodeDecodeError, ReceiptShareError):
        return ""


MAX_EXPIRY_DAYS = 3650


def normalize_expiry_days(value: Any) -> int:
    """Whole days a link stays valid; 0 means no expiry."""

    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_EXPIRY_DAYS:
        raise ReceiptShareError(f"有效期请填 1–{MAX_EXPIRY_DAYS} 天，或选择长期有效。")
    return value


def normalize_image_kinds(values: Any) -> list[str]:
    chosen = {str(item) for item in values} if isinstance(values, (list, tuple, set, frozenset)) else set()
    if not chosen or not chosen <= set(IMAGE_KINDS):
        raise ReceiptShareError("图片类型至少选一项：寄件单、签收图、回单图。")
    return [kind for kind in IMAGE_KINDS if kind in chosen]


def image_kinds_text(kinds: Sequence[str]) -> str:
    return "·".join(IMAGE_KIND_LABELS[kind] for kind in IMAGE_KINDS if kind in kinds)


def normalize_match_rules(raw: Any) -> dict[str, Any]:
    """Validate ``{"mode": "any"|"all", "conditions": [{field, op, value}, …]}``."""

    if not isinstance(raw, Mapping) or set(raw) != {"mode", "conditions"}:
        raise ReceiptShareError("客户匹配规则格式无效。")
    mode = str(raw.get("mode") or "")
    conditions = raw.get("conditions")
    if mode not in {"any", "all"} or not isinstance(conditions, list) or not conditions:
        raise ReceiptShareError("请至少添加一条发货人匹配规则。")
    if len(conditions) > MAX_MATCH_CONDITIONS:
        raise ReceiptShareError(f"匹配规则最多 {MAX_MATCH_CONDITIONS} 条。")
    result = []
    for item in conditions:
        if not isinstance(item, Mapping) or set(item) != {"field", "op", "value"}:
            raise ReceiptShareError("客户匹配规则格式无效。")
        field, op, value = str(item["field"]), str(item["op"]), str(item["value"] or "").strip()
        if field not in MATCH_FIELDS or op not in MATCH_OPS:
            raise ReceiptShareError("匹配规则只能按发货人名称、电话或地址，等于或包含。")
        if len(value) < 2 or len(value) > 120:
            raise ReceiptShareError("匹配内容为 2–120 个字。")  # one character would match almost everyone
        result.append({"field": field, "op": op, "value": value})
    return {"mode": mode, "conditions": result}


def match_rules_sql(rules: Mapping[str, Any], *, alias: str = "w") -> tuple[str, list[str]]:
    """SQL predicate over a waybill row for validated rules; values are parameters."""

    rules = normalize_match_rules(rules)
    if not re.fullmatch(r"[a-z]\w{0,15}", alias):
        raise ValueError("invalid alias")
    parts, params = [], []
    for item in rules["conditions"]:
        column = f"{alias}.{item['field']}"  # field is from the MATCH_FIELDS allow-list
        if item["op"] == "eq":
            parts.append(f"{column} = %s")
            params.append(item["value"])
        else:
            parts.append(f"{column} LIKE %s ESCAPE '!'")
            escaped = item["value"].replace("!", "!!").replace("%", "!%").replace("_", "!_")
            params.append(f"%{escaped}%")
    joiner = " OR " if rules["mode"] == "any" else " AND "
    return "(" + joiner.join(parts) + ")", params


def match_rules_match(rules: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """The same decision as ``match_rules_sql`` for one row already in memory."""

    rules = normalize_match_rules(rules)
    hits = []
    for item in rules["conditions"]:
        actual = str(row.get(item["field"]) or "")
        hits.append(actual == item["value"] if item["op"] == "eq" else item["value"] in actual)
    return any(hits) if rules["mode"] == "any" else all(hits)


def normalize_naming_template(value: Any) -> str:
    template = str(value or "").strip()
    if not template or len(template) > 160:
        raise ReceiptShareError("命名规则不能为空，且不超过 160 个字。")
    fields = _TEMPLATE_FIELD.findall(template)
    if any(field not in NAMING_FIELDS for field in fields) or "{" in _TEMPLATE_FIELD.sub("", template) \
            or "}" in _TEMPLATE_FIELD.sub("", template):
        raise ReceiptShareError("命名规则只能使用这些字段：" + "、".join(NAMING_FIELDS) + "。")
    if "运单号" not in fields or "单据类型" not in fields:
        raise ReceiptShareError("命名规则必须包含 {运单号} 和 {单据类型}，否则同一客户的文件会重名。")
    if _UNSAFE_FILENAME.search(_TEMPLATE_FIELD.sub("", template)):
        raise ReceiptShareError('命名规则不能包含 \\ / : * ? " < > | 这些字符。')
    if not re.search(r"\.(jpg|jpeg|png)$", template, re.IGNORECASE):
        raise ReceiptShareError("命名规则要以 .jpg 或 .png 结尾。")
    return template


def render_file_name(template: str, *, short_name: str, waybill_no: str, kind: str,
                     doc_date: date | datetime | str | None, seq: int, of: int = 1) -> str:
    """The one renderer of a shared file name: dialog example, list and downloads all use it.

    Several pictures of the same kind for one waybill get ``(n)`` before the
    extension unless the template already carries ``{序号}``.
    """

    template = normalize_naming_template(template)
    if isinstance(doc_date, (date, datetime)):
        date_text = doc_date.strftime("%Y%m%d")
    else:
        date_text = re.sub(r"\D", "", str(doc_date or ""))[:8]
    values = {"客户简称": short_name, "运单号": waybill_no, "单据类型": IMAGE_KIND_LABELS[kind],
              "开单日期": date_text, "序号": str(int(seq))}
    name = _TEMPLATE_FIELD.sub(lambda match: _UNSAFE_FILENAME.sub("_", str(values[match.group(1)])), template)
    if int(of) > 1 and "{序号}" not in template:
        stem, dot, extension = name.rpartition(".")
        name = f"{stem}({int(seq)}){dot}{extension}"
    return name


def nas_image_path(platform: str, waybill_no: str, kind: str, content_sha256: str, extension: str,
                   doc_month: str) -> str:
    """Archive path relative to the share folder: by platform, month and waybill, never by customer."""

    if platform not in SHARE_PLATFORMS or kind not in IMAGE_KINDS:
        raise ValueError("invalid image identity")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", waybill_no) or not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
        raise ValueError("invalid image identity")
    if not re.fullmatch(r"\d{4}-\d{2}", doc_month) or extension not in {"jpg", "png"}:
        raise ValueError("invalid image identity")
    return f"{platform}/{doc_month}/{waybill_no}/{kind}_{content_sha256[:16]}.{extension}"
