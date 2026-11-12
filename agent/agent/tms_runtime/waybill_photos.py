"""Sign-for and receipt photos of one platform waybill, read live and read only.

Sources, verified against the original pages on 2026-11-12:

- Ronghui sign photo: the tracking page's "查看图片" calls
  ``FIND_PIC_SCAN_BY_BILL_CODE`` with ``PIC_TYPE=1``; ``SAVE_POS`` is a
  ``/unauth/download/`` path on the TMS host (downloadable without login).
- Yunda sign photo: the INMS tracking result ``monitoring.signedPic`` lists
  images on ``kyscan.yunda56.com`` (downloadable without login).
- Receipt photos: the receipt pages read by ``receipts_sync`` for exactly this
  waybill (Ronghui process records by ``BILL_CODE``, Yunda by ``LogisticsId``).

Only image addresses are returned; the Console fetches and caches the images.
A receipt read failure is reported next to the sign photos, never hidden.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from agent.tms_runtime.account_manager import resolve_account_params
from agent.tms_runtime.direct_business import DirectBusinessError
from agent.tms_runtime.direct_execution import call_blocking
from agent.tms_runtime.scripts import receipts_sync
from agent.tms_runtime.scripts import yunda_waybill_tracking

RONGHUI_SIGN_PIC_CALL_ID = "FIND_PIC_SCAN_BY_BILL_CODE"
RONGHUI_SIGN_PIC_TYPE = "1"
YUNDA_SIGN_PHOTO_HOST = "kyscan.yunda56.com"
WAYBILL_NO_PATTERN = re.compile(r"[A-Za-z0-9-]{4,40}")
MAX_PHOTOS = 12


def _validated(params: dict[str, Any]) -> tuple[str, str]:
    if set(params) != {"platform", "waybill_no"} or not all(isinstance(params[key], str) for key in params):
        raise DirectBusinessError("INVALID_WAYBILL_PHOTO_QUERY", "签收图片只接受平台和运单号。")
    platform, waybill_no = params["platform"].strip(), params["waybill_no"].strip()
    if platform not in {"ronghui", "yunda"} or not WAYBILL_NO_PATTERN.fullmatch(waybill_no):
        raise DirectBusinessError("INVALID_WAYBILL_PHOTO_QUERY", "签收图片的平台或运单号无效。")
    return platform, waybill_no


def _unique(urls: list[str]) -> list[str]:
    return list(dict.fromkeys(url for url in urls if url))[:MAX_PHOTOS]


def _ronghui_headers() -> dict[str, str]:
    return {
        "Accept": "text/plain, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{receipts_sync.RONGHUI_ORIGIN}/widget/home",
    }


def _ronghui_sign_url(save_pos: Any) -> str:
    text = str(save_pos or "").strip()
    if text.startswith("/unauth/download/"):
        return f"{receipts_sync.RONGHUI_ORIGIN}{text}"
    parsed = urlparse(text)
    if parsed.scheme == "https" and parsed.hostname == urlparse(receipts_sync.RONGHUI_ORIGIN).hostname:
        return text
    return ""


def _ronghui_photos(waybill_no: str, *, session_profile: str, timeout_sec: int) -> dict[str, Any]:
    session = receipts_sync.get_session_broker(session_profile).build_requests_session(validate=False)
    headers = _ronghui_headers()
    rows = receipts_sync._fetch_ronghui_find_all(
        session, RONGHUI_SIGN_PIC_CALL_ID,
        data={"BILL_CODE": waybill_no, "PIC_TYPE": RONGHUI_SIGN_PIC_TYPE},
        headers=headers, timeout_sec=timeout_sec)
    sign = [_ronghui_sign_url(row.get("SAVE_POS")) for row in rows
            if str(row.get("BILL_CODE") or "").strip() == waybill_no]
    result: dict[str, Any] = {"sign_photos": _unique(sign), "receipt_photos": []}
    try:
        attachments = receipts_sync._fetch_ronghui_record_attachments(
            session, {"BILL_CODE": waybill_no}, headers=headers, timeout_sec=timeout_sec)
        result["receipt_photos"] = _unique([str(item.get("source_url") or "") for item in attachments])
    except Exception as exc:  # the sign photos are still shown with the reason
        result["receipt_error"] = f"融辉回单读取失败（{type(exc).__name__}）"
    return result


def _yunda_sign_urls(payload: Any, waybill_no: str) -> list[str]:
    node = yunda_waybill_tracking._find_waybill_node(payload, waybill_no) or {}
    monitoring = node.get("monitoring") if isinstance(node, dict) else None
    pictures = monitoring.get("signedPic") if isinstance(monitoring, dict) else None
    urls = []
    for item in pictures if isinstance(pictures, list) else []:
        parsed = urlparse(str(item or "").strip())
        if parsed.scheme == "https" and parsed.hostname == YUNDA_SIGN_PHOTO_HOST and parsed.path.startswith("/ws/"):
            urls.append(parsed.geturl())
    return urls


def _yunda_photos(waybill_no: str, *, account: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    session_profile = str(account.get("session_profile") or "")
    session = yunda_waybill_tracking.get_session_broker(session_profile).build_requests_session_unchecked()
    payload = yunda_waybill_tracking._request_payload(
        session, yunda_waybill_tracking._normalize_query_url({}), waybill_no, {}, timeout_sec=timeout_sec)
    result: dict[str, Any] = {"sign_photos": _unique(_yunda_sign_urls(payload, waybill_no)), "receipt_photos": []}
    try:
        records, _stats, warnings = receipts_sync._fetch_yunda(
            {**account, "waybill_no": waybill_no, "timeout_sec": timeout_sec}, direction="send")
        if warnings:
            raise RuntimeError("receipt page")
        result["receipt_photos"] = _unique([
            str(item.get("source_url") or "")
            for record in records if record.get("waybill_no") == waybill_no
            for item in record.get("attachments") or []])
    except Exception as exc:
        result["receipt_error"] = f"韵达回单读取失败（{type(exc).__name__}）"
    return result


async def query_waybill_photos_async(params: dict[str, Any], timeout_sec: int, *, read_lifecycle) -> dict[str, Any]:
    platform, waybill_no = _validated(params)
    account = await call_blocking(
        lambda: resolve_account_params({}, default_system=platform,
                                       default_purpose="price" if platform == "ronghui" else ""),
        timeout_sec=timeout_sec)
    account_id, session_profile = account.get("account_id"), str(account.get("session_profile") or "")
    if not account_id or not session_profile:
        raise DirectBusinessError("AUTH_REQUIRED", "签收图片未解析到有效平台账号。")
    read_timeout = min(int(timeout_sec), 30)

    def read() -> dict[str, Any]:
        if platform == "ronghui":
            photos = _ronghui_photos(waybill_no, session_profile=session_profile, timeout_sec=read_timeout)
        else:
            photos = _yunda_photos(waybill_no, account=account, timeout_sec=read_timeout)
        return {"platform": platform, "waybill_no": waybill_no, **photos}

    return await read_lifecycle.call_read(
        operation=f"waybill-photos.{platform}", account_ids=(account_id,),
        handler=lambda: call_blocking(read, timeout_sec=timeout_sec))
