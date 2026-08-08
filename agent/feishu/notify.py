"""Small Feishu notification helpers for proactive service alerts."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("feishu")

_STATE_DIR = Path(__file__).resolve().parent / "state"
_LAST_CHAT_PATH = _STATE_DIR / "last_chat.json"


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _ensure_state_dir() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def remember_chat_id(chat_id: str) -> None:
    """Remember the latest Feishu chat that talked to the bot."""
    normalized = str(chat_id or "").strip()
    if not normalized:
        return
    try:
        _ensure_state_dir()
        _LAST_CHAT_PATH.write_text(
            json.dumps(
                {
                    "chat_id": normalized,
                    "updated_at": _now_text(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("Failed to remember Feishu chat id", exc_info=True)


def _env_first(*names: str) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _load_last_chat_id() -> str:
    try:
        if not _LAST_CHAT_PATH.exists():
            return ""
        payload = json.loads(_LAST_CHAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("chat_id") or "").strip()


def resolve_notify_target() -> tuple[str, str] | tuple[None, None]:
    """Resolve where proactive Feishu alerts should be sent."""
    chat_id = _env_first(
        "FEISHU_TMS_ALERT_CHAT_ID",
        "FEISHU_NOTIFY_CHAT_ID",
        "FEISHU_ALERT_CHAT_ID",
        "FEISHU_DEFAULT_CHAT_ID",
    )
    if chat_id:
        return "chat_id", chat_id

    open_id = _env_first(
        "FEISHU_TMS_ALERT_OPEN_ID",
        "FEISHU_NOTIFY_OPEN_ID",
        "FEISHU_ALERT_OPEN_ID",
    )
    if open_id:
        return "open_id", open_id

    user_id = _env_first(
        "FEISHU_TMS_ALERT_USER_ID",
        "FEISHU_NOTIFY_USER_ID",
        "FEISHU_ALERT_USER_ID",
    )
    if user_id:
        return "user_id", user_id

    last_chat_id = _load_last_chat_id()
    if last_chat_id:
        return "chat_id", last_chat_id

    return None, None


def send_text_sync(receive_id: str, text: str, receive_id_type: str = "chat_id") -> bool:
    """Send a plain text Feishu message with the bot app credentials."""
    receive_id = str(receive_id or "").strip()
    text = str(text or "").strip()
    if not receive_id or not text:
        return False

    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.warning("Feishu app credentials are missing; proactive alert skipped.")
        return False

    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )

        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )

        resp = client.im.v1.message.create(req)
        if not resp.success():
            logger.error("Feishu proactive message failed: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True
    except Exception:
        logger.warning("Feishu proactive message failed", exc_info=True)
        return False


def _status_text(status: str) -> str:
    return {
        "expired": "\u5df2\u8fc7\u671f",
        "logged_out": "\u672a\u767b\u5f55",
        "pending_code": "\u5f85\u8f93\u5165\u9a8c\u8bc1\u7801",
        "error": "\u5f02\u5e38",
    }.get(status, status or "\u672a\u77e5")


def build_tms_session_disconnected_message(
    status: str,
    reason: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    normalized_status = str(status or "").strip()
    context = context or {}
    challenge_type = str(context.get("challenge_type") or context.get("challengeType") or "").strip().lower()
    system_label = str(context.get("system_label") or context.get("system") or "TMS").strip()
    if normalized_status == "pending_code":
        if challenge_type == "image":
            title = f"\u3010Agent \u81ea\u52a8\u5316\u3011{system_label} \u56fe\u5f62\u9a8c\u8bc1\u7801\u81ea\u52a8\u8bc6\u522b\u672a\u901a\u8fc7\uff0c\u7b49\u5f85\u4eba\u5de5\u8f93\u5165\u9a8c\u8bc1\u7801\u3002"
        elif challenge_type == "sms":
            title = f"\u3010Agent \u81ea\u52a8\u5316\u3011{system_label} \u624b\u673a\u9a8c\u8bc1\u7801\u5df2\u89e6\u53d1\uff0c\u7b49\u5f85\u5728\u98de\u4e66\u8f93\u5165\u9a8c\u8bc1\u7801\u3002"
        else:
            title = f"\u3010Agent \u81ea\u52a8\u5316\u3011{system_label} \u767b\u5f55\u6b63\u5728\u7b49\u5f85\u9a8c\u8bc1\u7801\u3002"
        lines = [
            title,
            f"\u72b6\u6001\uff1a{_status_text(normalized_status)}",
        ]
    else:
        lines = [
            f"\u3010Agent \u81ea\u52a8\u5316\u3011{system_label} \u767b\u5f55\u6001\u5df2\u65ad\u5f00\uff0c\u81ea\u52a8\u767b\u5f55\u672a\u5b8c\u6210\uff0c\u9700\u8981\u5904\u7406\u3002",
            f"\u72b6\u6001\uff1a{_status_text(normalized_status)}",
        ]
    account_name = str(context.get("account_name") or "").strip()
    account_id = str(context.get("account_id") or "").strip()
    profile = str(context.get("profile") or "").strip()
    last_validation_at = str(context.get("last_validation_at") or "").strip()
    if system_label:
        lines.append(f"\u7cfb\u7edf\uff1a{system_label}")
    if account_name or account_id:
        account_text = account_name or account_id
        if account_id and account_id != account_text:
            account_text = f"{account_text} ({account_id})"
        lines.append(f"\u8d26\u53f7\uff1a{account_text}")
    if profile:
        lines.append(f"profile\uff1a{profile}")
    if last_validation_at:
        lines.append(f"\u6821\u9a8c\u65f6\u95f4\uff1a{last_validation_at}")
    normalized_reason = str(reason or "").strip()
    if normalized_reason:
        lines.append(f"\u539f\u56e0\uff1a{normalized_reason[:300]}")
    if normalized_status == "pending_code":
        if challenge_type == "image":
            lines.append("\u8bf7\u6253\u5f00\u540e\u53f0\u8d26\u53f7\u7ba1\u7406\u9875\u67e5\u770b\u56fe\u5f62\u9a8c\u8bc1\u7801\uff0c\u6216\u5728\u98de\u4e66\u53d1\u9001\u201c\u767b\u5f55\u201d\u9009\u62e9\u8d26\u53f7\u540e\u63d0\u4ea4\u3002")
        else:
            lines.append("\u8bf7\u5728\u98de\u4e66\u76f4\u63a5\u56de\u590d\u624b\u673a\u9a8c\u8bc1\u7801\uff1b\u5982\u6709\u591a\u4e2a\u8d26\u53f7\u5f85\u9a8c\u8bc1\uff0c\u5148\u53d1\u9001\u201c\u767b\u5f55\u201d\u9009\u62e9\u8d26\u53f7\u3002")
    else:
        lines.append("\u8bf7\u5728\u98de\u4e66\u53d1\u9001\u201c\u767b\u5f55\u201d\u9009\u62e9\u8d26\u53f7\uff1b\u82e5\u8fdb\u5165\u9a8c\u8bc1\u7801\u767b\u5f55\uff0c\u53ef\u76f4\u63a5\u5728\u98de\u4e66\u63d0\u4ea4\uff0c\u6216\u5230\u540e\u53f0\u8d26\u53f7\u7ba1\u7406\u9875\u5904\u7406\u3002")
    return "\n".join(lines)


def send_tms_session_disconnected_alert(status_payload: dict[str, Any]) -> bool:
    if str(os.getenv("FEISHU_TMS_SESSION_ALERT_DISABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False

    receive_id_type, receive_id = resolve_notify_target()
    if not receive_id_type or not receive_id:
        logger.warning("No Feishu notify target configured for TMS session alert.")
        return False

    status = str(status_payload.get("status") or "").strip()
    reason = str(status_payload.get("last_error_summary") or "").strip()
    return send_text_sync(
        receive_id,
        build_tms_session_disconnected_message(status, reason, status_payload),
        receive_id_type=receive_id_type,
    )
