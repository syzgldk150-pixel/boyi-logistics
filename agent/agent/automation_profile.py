"""Runtime automation profile switch for supplier-specific workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


VALID_PROFILES = {"ronghui", "yunda"}
DEFAULT_PROFILE = "ronghui"
PROFILE_LABELS = {
    "ronghui": "融辉自动化",
    "yunda": "韵达自动化",
}

STATE_PATH = Path(__file__).resolve().parent / "tms_runtime" / "state" / "automation_profile.json"


def normalize_profile(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "": DEFAULT_PROFILE,
        "default": "ronghui",
        "tms": "ronghui",
        "rh": "ronghui",
        "ronghui": "ronghui",
        "融辉": "ronghui",
        "韵达": "yunda",
        "yunda": "yunda",
        "yd": "yunda",
    }
    normalized = aliases.get(text, text)
    if normalized not in VALID_PROFILES:
        raise ValueError(f"不支持的自动化 Profile: {value}")
    return normalized


def profile_label(profile: str) -> str:
    return PROFILE_LABELS.get(normalize_profile(profile), str(profile))


def get_current_profile() -> str:
    if not STATE_PATH.exists():
        return DEFAULT_PROFILE
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_PROFILE
    if not isinstance(payload, dict):
        return DEFAULT_PROFILE
    try:
        return normalize_profile(payload.get("profile"))
    except ValueError:
        return DEFAULT_PROFILE


def set_current_profile(profile: Any) -> dict[str, str]:
    normalized = normalize_profile(profile)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profile": normalized,
        "label": profile_label(normalized),
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def describe_current_profile() -> dict[str, str]:
    profile = get_current_profile()
    return {
        "profile": profile,
        "label": profile_label(profile),
    }
