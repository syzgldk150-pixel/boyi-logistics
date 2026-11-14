"""Bot tokens and long-poll cursors, kept out of MySQL in 0600 files."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "state" / "wechat_bot"


class WeChatTokenStore:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self._root = Path(root)

    def _dir(self, device_id: str) -> Path:
        if not _DEVICE_ID_RE.fullmatch(str(device_id)):
            raise ValueError("invalid device id")
        return self._root / device_id

    def _write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, device_id: str, *, token: str, base_url: str, ilink_bot_id: str, owner_user_id: str) -> None:
        if not token or not ilink_bot_id or not owner_user_id:
            raise ValueError("incomplete bot credentials")
        self._write(self._dir(device_id) / "bot.json", {
            "token": token, "base_url": base_url, "ilink_bot_id": ilink_bot_id, "owner_user_id": owner_user_id,
        })

    def load(self, device_id: str) -> dict[str, Any] | None:
        data = self._read(self._dir(device_id) / "bot.json")
        if not data or not all(isinstance(data.get(key), str) and data[key]
                               for key in ("token", "base_url", "ilink_bot_id", "owner_user_id")):
            return None
        return data

    def load_cursor(self, device_id: str) -> str:
        data = self._read(self._dir(device_id) / "cursor.json") or {}
        value = data.get("get_updates_buf")
        return value if isinstance(value, str) else ""

    def save_cursor(self, device_id: str, buf: str) -> None:
        self._write(self._dir(device_id) / "cursor.json", {"get_updates_buf": buf})

    def delete(self, device_id: str) -> None:
        directory = self._dir(device_id)
        for name in ("bot.json", "cursor.json"):
            try:
                (directory / name).unlink()
            except FileNotFoundError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass

    def device_ids(self, *, older_than_seconds: float = 0.0) -> list[str]:
        if not self._root.is_dir():
            return []
        cutoff = time.time() - older_than_seconds
        return sorted(path.name for path in self._root.iterdir()
                      if path.is_dir() and _DEVICE_ID_RE.fullmatch(path.name) and path.stat().st_mtime <= cutoff)

    def tokens(self, device_ids: list[str]) -> list[str]:
        result = []
        for device_id in device_ids:
            data = self.load(device_id)
            if data:
                result.append(data["token"])
        return result
