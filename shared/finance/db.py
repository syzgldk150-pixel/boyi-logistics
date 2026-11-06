"""DB-API helpers shared by the ledger mixins (no configuration, no DDL)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

DUPLICATE_KEY = 1062


def row_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [str(item[0]) for item in (getattr(cursor, "description", None) or ())]
    return dict(zip(names, row))


def fetchone(cursor: Any) -> dict[str, Any] | None:
    return row_dict(cursor, cursor.fetchone())


def fetchall(cursor: Any) -> list[dict[str, Any]]:
    return [item for row in (cursor.fetchall() or []) if (item := row_dict(cursor, row)) is not None]


def is_duplicate_key(error: BaseException) -> bool:
    args = getattr(error, "args", ())
    return bool(args) and args[0] == DUPLICATE_KEY


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def request_sha256(action: str, target: str, payload: Any) -> str:
    return hashlib.sha256(json_text({"action": action, "target": target, "payload": payload}).encode("utf-8")).hexdigest()
