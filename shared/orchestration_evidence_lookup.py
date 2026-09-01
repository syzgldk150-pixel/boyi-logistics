"""Exact read-only evidence lookup shared by orchestration projections."""

from __future__ import annotations

from typing import Any

from shared.orchestration_repository_support import _decode_row, _required_text, _row_dict


class EvidenceLookupMixin:
    def get(self, evidence_id: str) -> dict[str, Any] | None:
        """Read one exact evidence row without scanning or fuzzy matching."""

        with self.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM evidence_records WHERE evidence_id=%s",
                (_required_text(evidence_id, "evidence_id"),),
            )
            return _decode_row(_row_dict(cursor, cursor.fetchone()), ("summary_json",))


__all__ = ["EvidenceLookupMixin"]
