"""Low-level unknown-write lease-set helpers for repository transactions."""

from __future__ import annotations

from typing import Any

from shared import automation_plugin_repository as _repository

_rows = _repository._rows


def lock_remaining_unknown_generation_leases(
    cursor: Any,
    automation_id: str,
    generation: int,
) -> list[dict[str, Any]]:
    """Lock the generation's remaining unknown leases after a target settles."""

    cursor.execute(
        """
        SELECT lease_id FROM automation_project_generation_leases
        WHERE automation_id=%s AND generation=%s
          AND outcome='WRITE_OUTCOME_UNKNOWN'
        ORDER BY lease_id FOR UPDATE
        """,
        (automation_id, generation),
    )
    return _rows(cursor)
