"""Canonical daily-sign material shared by persistence and packaged rules."""
import hashlib
import json
from typing import Any, Iterable


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))


def snapshot_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    material = sorted(_json(row) for row in rows)
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()
