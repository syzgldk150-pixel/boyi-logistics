"""Explicit production scope for the current server-only plugin release.

The migration matrix is reviewed by humans, but production must not parse a
Markdown file to decide what code may execute.  This allowlist is therefore a
separate executable control.  A test keeps it byte-for-byte aligned with the
matrix rows marked ``RUNNABLE``.
"""

from __future__ import annotations


RUNNABLE_SERVER_FIRST_PARTY_PLUGIN_IDS = frozenset(
    {
        "clock_in_dual",
        "self_pickup_problem_upload",
        "split_pending_problem_upload",
        "sync_arrival_stats",
        "sync_arrive_list",
        "sync_customer_service_problems",
        "sync_daily_send_orders",
        "sync_daily_should_sign",
        "sync_delivery_status",
        "sync_finance_bills",
        "sync_scan_codes",
        "sync_site_send_list",
        "sync_yunda_dispatch_forecast",
        "sync_yunda_send_waybills",
    }
)

DEFERRED_R7_PLUGIN_IDS = frozenset(
    {
        "r7_arrival_checkin",
        "r7_departure_checkin",
    }
)

# Migration 018 assigns the new non-null schedule-generation column this
# value before any plugin generation is committed.  Only the exact reviewed
# R7 migration identities may retain it while their actions remain deferred.
DEFERRED_R7_LEGACY_SCHEDULE_GENERATION = 1

# Windows Service/Tray, public Worker transport and its release prerequisites
# are intentionally outside this release.  Enabling them is a reviewed code
# change, not an environment fallback.
WINDOWS_WORKER_RELEASE_ENABLED = False


__all__ = [
    "DEFERRED_R7_LEGACY_SCHEDULE_GENERATION",
    "DEFERRED_R7_PLUGIN_IDS",
    "RUNNABLE_SERVER_FIRST_PARTY_PLUGIN_IDS",
    "WINDOWS_WORKER_RELEASE_ENABLED",
]
