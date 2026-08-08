"""Shared local validation for waybill tracking numbers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

INVALID_TRACKING_NUMBER_CODE = "INVALID_TRACKING_NUMBER"
RONGHUI_R_LENGTH_ERROR = "单号格式错误：R 开头融辉单号应为 R+11位主单或 R+15位子单，请检查是否多输/少输数字。"


@dataclass(frozen=True)
class TrackingNumberValidation:
    tracking_number: str
    provider: str = ""
    error: str = ""
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def params(self) -> dict[str, str]:
        params = {"tracking_number": self.tracking_number}
        if self.provider:
            params["provider"] = self.provider
        return params

    def error_result(self) -> dict[str, str]:
        result = {"error": self.error or "单号格式错误"}
        if self.error_code:
            result["error_code"] = self.error_code
        return result


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_tracking_number(value: Any) -> str:
    text = _clean_str(value)
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", "", text).upper()


def _provider_from_hint(value: Any) -> str:
    hint = _clean_str(value).lower()
    if "韵达" in hint or "yunda" in hint:
        return "yunda"
    if "融辉" in hint or "ronghui" in hint or hint == "r7":
        return "ronghui"
    if "专线" in hint or "zhuanxian" in hint:
        return "zhuanxian"
    return ""


def infer_tracking_provider(tracking_number: Any) -> str:
    code = normalize_tracking_number(tracking_number)
    if not code:
        return ""
    if code.startswith("RC") or code.startswith("R") or code.startswith("200"):
        return "ronghui"
    if code.startswith("000"):
        return "zhuanxian"
    if re.fullmatch(r"\d+", code):
        return "yunda"
    return ""


def _invalid(tracking_number: str, provider: str, error: str) -> TrackingNumberValidation:
    return TrackingNumberValidation(
        tracking_number=tracking_number,
        provider=provider,
        error=error,
        error_code=INVALID_TRACKING_NUMBER_CODE,
    )


def _validate_ronghui(code: str) -> TrackingNumberValidation:
    if code.startswith("RC"):
        suffix = code[2:]
        if suffix and re.fullmatch(r"[A-Z0-9]+", suffix):
            return TrackingNumberValidation(code, "ronghui")
        return _invalid(code, "ronghui", "单号格式错误：RC 开头融辉单号应为 RC 后跟字母或数字。")

    if code.startswith("R"):
        suffix = code[1:]
        if not suffix.isdigit():
            return _invalid(code, "ronghui", "单号格式错误：R 开头融辉单号应为 R 后跟数字。")
        if len(suffix) not in {11, 15}:
            return _invalid(code, "ronghui", RONGHUI_R_LENGTH_ERROR)
        return TrackingNumberValidation(code, "ronghui")

    if code.startswith("200"):
        if code.isdigit() and len(code) >= 4:
            return TrackingNumberValidation(code, "ronghui")
        return _invalid(code, "ronghui", "单号格式错误：200 开头融辉单号应为纯数字。")

    return _invalid(code, "ronghui", "单号格式错误：融辉单号应以 R、RC 或 200 开头。")


def _validate_yunda(code: str) -> TrackingNumberValidation:
    if not code.isdigit():
        return _invalid(code, "yunda", "单号格式错误：韵达单号应为纯数字。")
    if code.startswith(("000", "200")):
        return _invalid(code, "yunda", "单号格式错误：该数字前缀不属于韵达单号。")
    if len(code) < 9:
        return _invalid(code, "yunda", "单号格式错误：韵达单号应为至少9位纯数字。")
    return TrackingNumberValidation(code, "yunda")


def _validate_zhuanxian(code: str) -> TrackingNumberValidation:
    if code.startswith("000") and code.isdigit() and len(code) >= 4:
        return TrackingNumberValidation(code, "zhuanxian")
    return _invalid(code, "zhuanxian", "单号格式错误：专线单号应为 000 开头的纯数字。")


def validate_tracking_number(tracking_number: Any, *, provider_hint: Any = "") -> TrackingNumberValidation:
    code = normalize_tracking_number(tracking_number)
    if not code:
        return _invalid("", _provider_from_hint(provider_hint), "缺少单号")

    hint_provider = _provider_from_hint(provider_hint)
    provider = hint_provider or infer_tracking_provider(code)
    if not re.fullmatch(r"[A-Z0-9]+", code):
        return _invalid(code, provider, "单号格式错误：只支持字母和数字。")

    if provider == "ronghui":
        return _validate_ronghui(code)
    if provider == "yunda":
        return _validate_yunda(code)
    if provider == "zhuanxian":
        return _validate_zhuanxian(code)
    return _invalid(code, "", "单号格式错误：无法识别单号类型，请输入 R/RC/200 开头的融辉单号、纯数字韵达单号或 000 开头的专线单号。")
