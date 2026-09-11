"""Lossless JSON for validated plugin definitions and runtime records.

Callers validate schemas and reject credentials before persistence. Audit and
error output continue to use shared.redaction, never this serializer.
"""
from datetime import datetime
import hashlib
import json
from typing import Any


def _datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return str(value)
    raise TypeError("plugin record contains a non-JSON value")


def plugin_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=_datetime)


def plugin_json_digest(value: Any) -> str:
    return hashlib.sha256(plugin_json(value).encode("utf-8")).hexdigest()
