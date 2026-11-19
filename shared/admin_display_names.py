"""Back-office display names for console administrator actor ids (shown in operation logs)."""
from __future__ import annotations

from typing import Any, Iterable


def admin_ids(actor_ids: Iterable[Any] | None) -> list[int]:
    """Console administrator ids among ``actor_ids`` (Feishu/WeChat/system actors are skipped)."""
    return sorted({int(item) for item in actor_ids or () if str(item).isdigit()})[:50]


def fetch_admin_display_names(cursor: Any, actor_ids: Iterable[Any] | None) -> dict[str, str]:
    """``{actor_id: display name}`` read through an open cursor; unknown ids are left out."""
    ids = admin_ids(actor_ids)
    if not ids:
        return {}
    cursor.execute("SELECT id, display_name, username FROM admin_users WHERE id IN (" + ",".join(["%s"] * len(ids)) + ")",
                   tuple(ids))
    names = [column[0] for column in cursor.description or ()]
    rows = [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]
    return {str(row["id"]): str(row["display_name"] or row["username"]) for row in rows}


__all__ = ["admin_ids", "fetch_admin_display_names"]
