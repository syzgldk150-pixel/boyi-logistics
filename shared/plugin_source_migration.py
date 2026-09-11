"""Move source ownership without changing business or operator-entered rows."""
from __future__ import annotations

from dataclasses import fields

from shared.data_sources import DataSourceRepository, SourceIdentity, row_dict


def transfer_plugin_sources(connection, *, source_id: str, target_id: str,
                            target_generation: int, request_id: str) -> tuple[str, ...]:
    """Caller holds the migration/project transaction and has drained both sides."""
    sources = DataSourceRepository(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM module_data_sources WHERE producer_instance_id=%s ORDER BY source_id FOR UPDATE", (source_id,))
        rows = [row_dict(cursor, row) for row in cursor.fetchall()]
    for row in rows:
        sources.switch_producer(row["source_id"], expected_revision=int(row["revision"]),
            expected_producer_instance_id=source_id, producer_instance_id=target_id,
            producer_generation=target_generation,
            identity=SourceIdentity(**{field.name: row[field.name] for field in fields(SourceIdentity)}),
            request_id=request_id)
    return tuple(row["source_id"] for row in rows)
