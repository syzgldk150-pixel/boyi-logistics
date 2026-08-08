"""Shared OCR helpers for short image captchas used by TMS logins."""

from __future__ import annotations

import re
import threading
from typing import Any


_OCR_LOCK = threading.Lock()
_OCR_ENGINE: Any | None = None
_OCR_INIT_ERROR: BaseException | None = None
_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


class CaptchaOCRUnavailableError(RuntimeError):
    """Raised when the OCR dependency cannot be initialized."""


class CaptchaOCRFailedError(RuntimeError):
    """Raised when OCR execution fails for a specific image."""


def normalize_captcha_text(value: str, *, max_length: int = 4) -> str:
    cleaned = _ALNUM_RE.sub("", str(value or "")).strip()
    if max_length > 0:
        return cleaned[:max_length]
    return cleaned


def _load_engine() -> Any:
    global _OCR_ENGINE, _OCR_INIT_ERROR

    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    if _OCR_INIT_ERROR is not None:
        raise CaptchaOCRUnavailableError("ddddocr is unavailable.") from _OCR_INIT_ERROR

    with _OCR_LOCK:
        if _OCR_ENGINE is not None:
            return _OCR_ENGINE
        if _OCR_INIT_ERROR is not None:
            raise CaptchaOCRUnavailableError("ddddocr is unavailable.") from _OCR_INIT_ERROR
        try:
            import ddddocr

            _OCR_ENGINE = ddddocr.DdddOcr(show_ad=False)
        except BaseException as exc:  # pragma: no cover - dependency/import failures are environment-specific
            _OCR_INIT_ERROR = exc
            raise CaptchaOCRUnavailableError("ddddocr is unavailable.") from exc
    return _OCR_ENGINE


def classify_captcha_image(image_bytes: bytes, *, max_length: int = 4) -> str:
    if not image_bytes:
        return ""
    engine = _load_engine()
    try:
        raw_text = engine.classification(image_bytes)
    except Exception as exc:
        raise CaptchaOCRFailedError(f"Captcha OCR failed: {exc}") from exc
    return normalize_captcha_text(str(raw_text or ""), max_length=max_length)
