"""Migration-owned schema contract for the shared finance ledger.

Finance DDL lives in ``agent/migrations/004_finance_runtime_tables.sql``.
Runtime code may only validate that the deployment has applied it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


FINANCE_REQUIRED_TABLES = frozenset(
    {
        "finance_sync_batches",
        "finance_sync_runs",
        "finance_fee_items",
        "finance_transactions",
        "finance_summary_snapshots",
        "finance_fee_mappings",
        "finance_mapping_audit_logs",
        "finance_fee_subjects",
        "finance_review_cases",
        "finance_review_ai_runs",
        "finance_waybill_facts",
        "finance_anomalies",
        "finance_knowledge_exports",
    }
)

# Compatibility export: runtime DDL is intentionally empty after migration.
MYSQL_DDL: tuple[str, ...] = ()


def mysql_schema_statements() -> tuple[str, ...]:
    """Return no DDL; deployment SQL migrations own schema changes."""

    return MYSQL_DDL


def _table_name(row: Any) -> str:
    if isinstance(row, Mapping):
        value = row.get("TABLE_NAME")
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) == 1:
        value = row[0]
    else:
        raise TypeError("finance schema query returned an unsupported row shape")
    table_name = str(value or "").strip()
    if not table_name:
        raise RuntimeError("finance schema query returned an empty table name")
    return table_name


def validate_finance_schema(cursor: Any) -> None:
    cursor.execute(
        """
        SELECT TABLE_NAME FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        """
    )
    tables = {_table_name(row) for row in cursor.fetchall() or []}
    missing = sorted(FINANCE_REQUIRED_TABLES - tables)
    if missing:
        raise RuntimeError(
            "finance schema is not migrated; run deployment migrations first: " + ", ".join(missing)
        )
