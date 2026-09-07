"""Supplier field translation owned by the customer collection package."""
from __future__ import annotations
import re
from typing import Any

SUPPORTED_PLATFORMS = {"ronghui", "yunda"}

class CustomerServiceProblemError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _first_text(source: dict[str, Any], *keys: str) -> str:
    if not isinstance(source, dict):
        return ""
    lowered = {str(key).lower(): value for key, value in source.items()}
    for key in keys:
        value = _clean_text(source.get(key))
        if value:
            return value
        value = _clean_text(lowered.get(str(key).lower()))
        if value:
            return value
    return ""


_UNREPLIED_STATUS_RE = re.compile(r"(未回复|未回|待回复|暂无回复|无回复)")
_EMPTY_REPLY_TEXTS = {"0", "-", "无", "暂无", "暂无回复", "无回复"}


def _has_problem_reply(row: dict[str, Any], reply_text: str) -> bool:
    count_text = _first_text(row, "reply_count", "REPLY_COUNT")
    if count_text:
        try:
            if float(count_text) > 0:
                return True
        except ValueError:
            pass
    text = _clean_text(reply_text)
    if not text or text in _EMPTY_REPLY_TEXTS or _UNREPLIED_STATUS_RE.search(text):
        return False
    return True


def _display_problem_status(status: str, row: dict[str, Any], reply_text: str) -> str:
    text = _clean_text(status)
    if _has_problem_reply(row, reply_text) and (not text or _UNREPLIED_STATUS_RE.search(text)):
        return "已回复"
    return text



def _normalize_platform(value: Any) -> str:
    return str(value or "").strip().lower()

def _safe_json(value: Any) -> Any:
    return value

def normalize_problem_rows(
    platform: str,
    rows: list[dict[str, Any]],
    *,
    account_id: str,
    account_label: str,
    source_direction: str,
) -> list[dict[str, Any]]:
    normalized_platform = _normalize_platform(platform)
    if normalized_platform not in SUPPORTED_PLATFORMS:
        raise CustomerServiceProblemError("UNSUPPORTED_PLATFORM", f"不支持的平台：{platform}")

    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if normalized_platform == "ronghui":
            external_id = _first_text(row, "GUID")
            waybill_no = _first_text(row, "BILL_CODE", "bill_code")
            status = _first_text(row, "REVERSION_STATUS", "BL_CHECKOK_STR", "BL_RETURN", "IS_REPLY")
            problem_type = _first_text(row, "TYPE")
            problem_text = _first_text(row, "PROBLEM_CAUSE")
            reply_text = _first_text(row, "REVERSION", "DEAL_RESULT")
            created_at = _first_text(row, "REGISTER_DATE", "REGISTER_SAVE_DATE")
            registered_at = _first_text(row, "REGISTER_DATE")
            registration_saved_at = _first_text(row, "REGISTER_SAVE_DATE")
            registered_site = _first_text(row, "REGISTER_SITE")
            updated_at = _first_text(row, "REVERSION_DATE")
        else:
            external_id = _first_text(row, "prob_main_id")
            waybill_no = _first_text(row, "ship_no", "LogisticsId")
            status = _first_text(row, "prob_status", "check_status", "issue_check_status")
            problem_text = _first_text(row, "prob_text")
            problem_type = _first_text(row, "prob_type", "issue_type")
            reply_text = _first_text(row, "reply_text")
            created_at = _first_text(row, "created_time")
            registered_at = created_at
            registration_saved_at = ""
            registered_site = _first_text(row, "register_site", "site_name")
            updated_at = _first_text(row, "modified_time", "reply_time")
        if not external_id:
            raise CustomerServiceProblemError(
                "MISSING_EXTERNAL_ID",
                f"{normalized_platform} 问题件第 {index + 1} 行缺少唯一键，已停止处理。",
            )
        output.append(
            {
                "platform": normalized_platform,
                "account_id": _clean_text(account_id),
                "account_label": _clean_text(account_label) or _clean_text(account_id),
                "source_direction": _clean_text(source_direction),
                "external_id": external_id,
                "waybill_no": waybill_no,
                "status": _display_problem_status(status, row, reply_text),
                "problem_type": problem_type,
                "problem_text": problem_text,
                "reply_text": reply_text,
                "created_at": created_at,
                "registered_at": registered_at,
                "registration_saved_at": registration_saved_at,
                "registered_site": registered_site,
                "updated_at": updated_at,
                "raw": _safe_json(row),
            }
        )
    return output
