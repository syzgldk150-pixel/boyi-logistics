"""Deployment status and rollback helpers for migration 030."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


MIGRATION_VERSION = "030"
DELIVERY_TABLE = "feishu_approval_deliveries"
LEASE_COLUMNS = (
    "notification_lease_token",
    "notification_lease_expires_at",
)


def report_feishu_notification_lease_status(
    *,
    connect: Callable[[], Any],
    require_mysql8: Callable[[Any], str],
    migration_table_exists: Callable[[Any], bool],
) -> int:
    """Report whether migration 030 predates this release without exposing rows."""

    connection = connect()
    try:
        with connection.cursor() as cursor:
            require_mysql8(cursor)
            applied = False
            if migration_table_exists(cursor):
                cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s",
                    (MIGRATION_VERSION,),
                )
                applied = cursor.fetchone() is not None
            print(
                "feishu_notification_lease_status="
                + ("applied" if applied else "pending")
            )
    finally:
        connection.close()
    return 0


def restore_feishu_notification_leases(
    *,
    connect: Callable[[], Any],
    require_mysql8: Callable[[Any], str],
    table_exists: Callable[[Any, str], bool],
    column_exists: Callable[[Any, str, str], bool],
) -> int:
    """Clear migration-030 sender leases before restarting pre-030 code."""

    connection = connect()
    try:
        connection.begin()
        restored = 0
        with connection.cursor() as cursor:
            require_mysql8(cursor)
            columns_exist = table_exists(cursor, DELIVERY_TABLE) and all(
                column_exists(cursor, DELIVERY_TABLE, column_name)
                for column_name in LEASE_COLUMNS
            )
            if columns_exist:
                cursor.execute(
                    """
                    UPDATE feishu_approval_deliveries
                    SET notification_lease_token=NULL,
                        notification_lease_expires_at=NULL,
                        updated_at=NOW(6)
                    WHERE notification_lease_token IS NOT NULL
                       OR notification_lease_expires_at IS NOT NULL
                    """
                )
                restored = int(getattr(cursor, "rowcount", 0) or 0)
        connection.commit()
        print(f"feishu_notification_leases_restored={restored}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return 0
