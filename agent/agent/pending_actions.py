"""按 chat_id 维护的待用户确认动作（带 TTL 的内存映射）。"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

DEFAULT_TTL_SEC = 600
_DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tms_runtime",
    "state",
    "pending_actions.json",
)

_lock = threading.Lock()
_pending: dict[str, tuple[float, dict[str, Any]]] = {}


def _state_file() -> str:
    return os.getenv("AGENT_PENDING_STATE_FILE") or _DEFAULT_STATE_FILE


def _load_locked() -> None:
    path = _state_file()
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return
    if not isinstance(raw, dict):
        return

    now = time.time()
    loaded: dict[str, tuple[float, dict[str, Any]]] = {}
    for chat_id, item in raw.items():
        if not isinstance(item, dict):
            continue
        try:
            expires_at = float(item.get("expires_at") or 0)
        except (TypeError, ValueError):
            continue
        action = item.get("action")
        if not isinstance(action, dict) or expires_at < now:
            continue
        loaded[str(chat_id)] = (expires_at, dict(action))
    _pending.clear()
    _pending.update(loaded)


def _persist_locked() -> None:
    path = _state_file()
    if not path:
        return
    now = time.time()
    payload = {
        chat_id: {"expires_at": expires_at, "action": action}
        for chat_id, (expires_at, action) in _pending.items()
        if expires_at >= now
    }
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp_path, path)


def set_pending(chat_id: str, action: dict[str, Any], ttl_sec: int = DEFAULT_TTL_SEC) -> None:
    if not chat_id:
        return
    expires_at = time.time() + max(1, int(ttl_sec))
    with _lock:
        _load_locked()
        _pending[chat_id] = (expires_at, dict(action))
        _persist_locked()


def get_pending(chat_id: str) -> dict[str, Any] | None:
    if not chat_id:
        return None
    now = time.time()
    with _lock:
        _load_locked()
        entry = _pending.get(chat_id)
        if entry is None:
            return None
        expires_at, action = entry
        if expires_at < now:
            _pending.pop(chat_id, None)
            _persist_locked()
            return None
        return dict(action)


def clear_pending(chat_id: str) -> dict[str, Any] | None:
    if not chat_id:
        return None
    with _lock:
        _load_locked()
        entry = _pending.pop(chat_id, None)
        _persist_locked()
    if entry is None:
        return None
    _, action = entry
    return dict(action)
