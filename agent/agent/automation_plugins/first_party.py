"""Deterministic first-party action packages and legacy instance bootstrap.

Package identity is ``plugin_id + version``. The 18 current automation cards
are separate instances and may share one action package (notably clock-in and
finance). No account identifier, credential or cron expression is embedded in
the package. Existing values are preserved only by the migration authority.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from agent.automation_plugins.errors import (
    AutomationPluginError,
    PluginConflictError,
    PluginPackageError,
)
from agent.automation_plugins.execution import FilesystemPluginIntegrityVerifier
from agent.automation_plugins.manifest import AutomationPluginManifest, canonical_json_bytes
from agent.automation_plugins.models import (
    BootstrapResult,
    FirstPartyInstanceSeed,
    PluginTrustSource,
    PluginVersionRecord,
)
from agent.automation_plugins.package import (
    PackageSignatureVerifier,
    SIGNATURE_NAME,
    VerifiedPluginPackage,
    _zip_bytes,
    extract_verified_package,
    verify_signed_plugin_zip,
)
from agent.automation_plugins.ports import (
    AutomationPluginRepositoryPort,
    FirstPartyPackageMaterializerPort,
    FirstPartyPackageProvider,
    FirstPartyPackageRecoveryMaterializerPort,
    PluginEnvironmentBuilderPort,
    PluginStoragePort,
)
from agent.automation_plugins.release_scope import (
    RUNNABLE_SERVER_FIRST_PARTY_PLUGIN_IDS,
)
from agent.automation_plugins.sdk import PLUGIN_SDK_SOURCE
from agent.automation_plugins.storage import (
    VERIFIED_ARCHIVE_RELATIVE,
    validate_regular_plugin_file,
)
from shared.automation_project_manifest import FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES
from shared.scheduled_task_contracts import APPROVED_SCHEDULED_TASK_PROFILES


FIRST_PARTY_ROOT = Path(__file__).resolve().parents[2] / "first_party_automation_plugins"
DIGEST_LOCK_PATH = FIRST_PARTY_ROOT / "digests.json"
FIRST_PARTY_RUNTIME_PATH = FIRST_PARTY_ROOT / "_runtime" / "main.py"
FIRST_PARTY_RESULT_PATH = FIRST_PARTY_ROOT / "_runtime" / "result.py"
# Payload and Broker-effect changes advance the signed executable contract.
FIRST_PARTY_PACKAGE_VERSION = "1.0.8"
_RELEASE_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_ACCOUNT_SYSTEM_PREFIXES = {
    "ronghui_": "ronghui",
    "price_": "ronghui",
    "yunda_": "yunda",
    "r7_": "r7",
    "r13_": "r13",
}
_GOVERNANCE_ANCHOR_FIELDS = (
    "name",
    "version",
    "operation_type",
    "risk_level",
    "approval",
    "permissions",
    "idempotency",
    "retry",
    "evidence",
    "postconditions",
    "project_full_auto_allowed",
)
_CODE_OWNED_ACCOUNT_ROLES: Mapping[str, tuple[Mapping[str, Any], ...]] = {
    # The finance action fans out through the core finance source resolver.
    # These are logical roles, never account IDs; migration binds the reviewed
    # accounts and a new instance starts with all three roles unbound.
    "sync_finance_bills": (
        {"role": "finance_quote_source", "argument_field": "account_id", "collection": False},
        {"role": "finance_daxiang_s_source", "argument_field": "account_id", "collection": False},
        {"role": "finance_self_pickup_source", "argument_field": "account_id", "collection": False},
    ),
    "sync_customer_service_problems": (
        {"role": "customer_service_source", "argument_field": "account_ids", "collection": True},
    ),
}
_CODE_OWNED_ENTRYPOINT_DYNAMIC_FIELDS: Mapping[
    str,
    Mapping[str, tuple[str, ...]],
] = {
    "sync_delivery_status": {
        "webhook": ("BILL_CODE", "RECORD_ID"),
    },
    "sync_arrival_stats": {
        "webhook": ("target_date", "dry_run"),
    },
    "sync_daily_send_orders": {
        "feishu": ("target_date", "start_date", "end_date"),
    },
    "r7_departure_checkin": {
        "feishu": ("plate_numbers",),
    },
    "self_pickup_problem_upload": {
        "feishu": (
            "dry_run",
            "selected_bill_codes",
            "preview_fingerprint",
        ),
    },
    "split_pending_problem_upload": {
        "feishu": (
            "dry_run",
            "selected_bill_codes",
            "preview_fingerprint",
        ),
    },
    "sync_yunda_dispatch_forecast": {
        "feishu": ("target_date",),
    },
    "sync_yunda_send_waybills": {
        "feishu": ("target_date", "start_date", "end_date"),
    },
}
_CODE_OWNED_ENTRYPOINT_FIXED_RESOLVERS: Mapping[
    str,
    Mapping[str, Mapping[str, str]],
] = {
    "sync_site_send_list": {
        "console": {"target_date": "current_business_day"},
        "scheduler": {"target_date": "current_business_day"},
    },
}
_CONFIG_HIDDEN_DYNAMIC_PLUGINS = frozenset(
    {
        "sync_site_send_list",
        "self_pickup_problem_upload",
        "split_pending_problem_upload",
    }
)
_OPTIONAL_CODE_OWNED_ENTRYPOINT_DYNAMIC_FIELDS = frozenset(
    {
        ("sync_yunda_dispatch_forecast", "feishu", "target_date"),
        ("sync_daily_send_orders", "feishu", "target_date"),
        ("sync_daily_send_orders", "feishu", "start_date"),
        ("sync_daily_send_orders", "feishu", "end_date"),
        ("sync_yunda_send_waybills", "feishu", "target_date"),
        ("sync_yunda_send_waybills", "feishu", "start_date"),
        ("sync_yunda_send_waybills", "feishu", "end_date"),
    }
)
_WEBHOOK_ROUTE_PLUGINS = frozenset(
    {"sync_delivery_status", "sync_scan_codes", "sync_arrival_stats"}
)
_FEISHU_ROUTE_PLUGINS = frozenset(
    {
        "r7_departure_checkin",
        "r7_arrival_checkin",
        "self_pickup_problem_upload",
        "split_pending_problem_upload",
        "sync_arrive_list",
        "sync_scan_codes",
        "sync_arrival_stats",
        "sync_daily_send_orders",
        "sync_yunda_dispatch_forecast",
        "sync_yunda_send_waybills",
    }
)
_ACTION_RESOURCE_ROLES: Mapping[str, tuple[Mapping[str, Any], ...]] = {
    "sync_arrive_list": (
        {
            "role": "arrive_primary_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "arrive_secondary_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ),
    "self_pickup_problem_upload": (
        {
            "role": "self_pickup_source_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ),
    "split_pending_problem_upload": (
        {
            "role": "split_pending_source_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "split_pending_target_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ),
    "sync_daily_send_orders": (
        {
            "role": "send_order_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
    ),
    "sync_daily_should_sign": (
        {
            "role": "daily_sign_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
        {
            "role": "daily_sign_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ),
    "sync_delivery_status": (
        {
            "role": "delivery_status_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
    ),
    "sync_arrival_stats": (
        {
            "role": "arrival_stats_primary_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "arrival_stats_secondary_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "arrival_stats_pending_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": False,
        },
        {
            "role": "arrival_stats_archive_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
        {
            "role": "arrival_stats_split_pending_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ),
    "sync_site_send_list": (
        {
            "role": "site_send_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
        {
            "role": "site_send_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ),
    "sync_yunda_dispatch_forecast": (
        {
            "role": "dispatch_forecast_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
    ),
    "sync_yunda_send_waybills": (
        {
            "role": "send_waybills_bitable",
            "allowed_kinds": ["feishu_bitable"],
            "required": True,
        },
        {
            "role": "send_waybills_sheet",
            "allowed_kinds": ["feishu_sheet"],
            "required": True,
        },
    ),
}
_RESOURCE_BACKED_ARGUMENT_FIELDS: Mapping[str, frozenset[str]] = {
    "sync_daily_send_orders": frozenset(
        {"request_body", "base_token", "table_id"}
    ),
    "sync_delivery_status": frozenset(
        {"base_token", "table_id", "view_id", "view_name"}
    ),
    "sync_site_send_list": frozenset(
        {
            "request_body",
            "base_token",
            "table_id",
            "spreadsheet_token",
            "range",
        }
    ),
}
_CODE_OWNED_REMOVED_ARGUMENT_FIELDS: Mapping[str, frozenset[str]] = {
    # The legacy whole-tool webhook has no authoritative readback.  It is not
    # part of the signed package; system ingress routes remain independent.
    "sync_scan_codes": frozenset({"trigger_flow"}),
    "sync_arrival_stats": frozenset({"trigger_flow"}),
}
_CODE_OWNED_RESOURCE_ROLES: Mapping[str, tuple[Mapping[str, Any], ...]] = {
    plugin_id: (
        tuple(
            role
            for role in (
                {
                    "role": "webhook_route",
                    "allowed_kinds": ["webhook_route"],
                    "required": False,
                }
                if plugin_id in _WEBHOOK_ROUTE_PLUGINS
                else None,
                {
                    "role": "feishu_route",
                    "allowed_kinds": ["feishu_route"],
                    "required": False,
                }
                if plugin_id in _FEISHU_ROUTE_PLUGINS
                else None,
            )
            if role is not None
        )
        + _ACTION_RESOURCE_ROLES.get(plugin_id, ())
    )
    for plugin_id in sorted(
        _WEBHOOK_ROUTE_PLUGINS
        | _FEISHU_ROUTE_PLUGINS
        | set(_ACTION_RESOURCE_ROLES)
    )
}
_FIRST_PARTY_INSTANCES = FIRST_PARTY_MIGRATION_INSTANCE_TEMPLATES


@dataclass(frozen=True)
class FirstPartyBrokerAction:
    """One low-level capability used by package-owned orchestration.

    An action is deliberately narrower than an automation tool.  The core
    broker may expose these exact pairs, but it may never register a
    ``run``/``execute`` action for an entire first-party automation.
    """

    operation: str
    action: str
    roles: tuple[str, ...]
    effect: str


# This is a closed, source-owned classification. Every signed first-party
# Broker action is listed as either read or write. New actions must be
# classified explicitly before a manifest may be built or signed.
_FIRST_PARTY_WRITE_BROKER_ACTIONS = frozenset(
    {
        "ronghui.clock.submit",
        "r7.arrival.submit",
        "r7.departure.submit",
        "r7.checkin_log.append_evidence",
        "ronghui.problem.create",
        "split_pending.snapshot.replace",
        "feishu.sheet.replace_rows",
        "ronghui.complaint.create",
        "split_pending.result.upsert",
        "daily_sign.problem_event.upsert",
        "scan.snapshot.replace",
        "scan.snapshot.cleanup",
        "waybill.snapshot.replace",
        "arrival.snapshot.replace",
        "split_pending.snapshot.refresh",
        "feishu.sheet.replace",
        "feishu.sheet.add",
        "arrival.forecast_snapshot.replace",
        "sync_daily_send_orders.lock.acquire",
        "sync_daily_send_orders.lock.release",
        "feishu.bitable.delete_records",
        "feishu.bitable.write_records",
        "waybill.ronghui.replace_date",
        "daily_sign.authoritative_sync",
        "waybill.delivery_status.update",
        "finance.batch.acquire",
        "finance.source_snapshot.write",
        "finance.projection.commit",
        "ronghui.scan_next.submit",
        "feishu.bitable.replace_snapshot",
        "feishu.bitable.append_yunda_dispatch_forecast",
        "feishu.bitable.replace_yunda_send_waybills_date",
        "feishu.sheet.replace_yunda_send_waybills",
        "waybill.yunda.replace_date",
    }
)


_FIRST_PARTY_READ_BROKER_ACTIONS = frozenset(
    {
        "customer_problem.detail",
        "customer_problem.list_page",
        "r7.arrival.query_page",
        "r7.arrival.verify",
        "r7.departure.query_page",
        "r7.departure.verify",
        "ronghui.arrive_list.read_page",
        "ronghui.clock.precheck",
        "ronghui.clock.verify",
        "ronghui.complaint.query",
        "ronghui.complaint.verify",
        "ronghui.delivery_status.read",
        "ronghui.finance.capture_page",
        "ronghui.finance.verify_source_totals",
        "ronghui.problem.query",
        "ronghui.problem.verify",
        "ronghui.scan_next.verify",
        "ronghui.scan.read_page",
        "ronghui.send_order.read_page",
        "ronghui.site_send.read_page",
        "ronghui.waybill_detail.read",
        "yunda.dispatch_forecast.read_page",
        "yunda.send_waybill.list_page",
        "yunda.send_waybill.renderer_detail",
        "yunda.special_line.list_page",
        "yunda.waybill.original_data",
        "yunda.waybill.tracking_detail",
        "feishu.bitable.list_records",
        "feishu.bitable.list_views",
        "feishu.sheet.read_rows",
        "arrival.snapshot.completed_before",
        "r7.checkin_log.read_daily_success",
        "scan.snapshot.read",
        "split_pending.snapshot.read",
        "waybill.pending.read",
    }
)


def _broker_action(operation: str, action: str, *roles: str) -> FirstPartyBrokerAction:
    if action in _FIRST_PARTY_WRITE_BROKER_ACTIONS:
        effect = "write"
    elif action in _FIRST_PARTY_READ_BROKER_ACTIONS:
        effect = "read"
    else:
        raise PluginPackageError(
            f"first-party Broker action has no explicit effect classification: {operation}/{action}"
        )
    return FirstPartyBrokerAction(
        operation=operation,
        action=action,
        roles=tuple(roles),
        effect=effect,
    )


# These contracts are part of the signed action package.  They describe only
# closed browser/resource/projection primitives; pagination, transformation,
# ordering, evidence construction and retry decisions live in the payload.
_FIRST_PARTY_BROKER_ACTIONS: Mapping[str, tuple[FirstPartyBrokerAction, ...]] = {
    "clock_in_dual": (
        _broker_action("browser.invoke", "ronghui.clock.precheck", "account_id"),
        _broker_action("browser.invoke", "ronghui.clock.submit", "account_id"),
        _broker_action("browser.invoke", "ronghui.clock.verify", "account_id"),
    ),
    "r7_arrival_checkin": (
        _broker_action("projection.invoke", "r7.checkin_log.read_daily_success", "account_id"),
        _broker_action("browser.invoke", "r7.arrival.query_page", "account_id"),
        _broker_action("browser.invoke", "r7.arrival.submit", "account_id"),
        _broker_action("browser.invoke", "r7.arrival.verify", "account_id"),
        _broker_action("projection.invoke", "r7.checkin_log.append_evidence", "account_id"),
    ),
    "r7_departure_checkin": (
        _broker_action("projection.invoke", "r7.checkin_log.read_daily_success", "account_id"),
        _broker_action("browser.invoke", "r7.departure.query_page", "account_id"),
        _broker_action("browser.invoke", "r7.departure.submit", "account_id"),
        _broker_action("browser.invoke", "r7.departure.verify", "account_id"),
        _broker_action("projection.invoke", "r7.checkin_log.append_evidence", "account_id"),
    ),
    "self_pickup_problem_upload": (
        _broker_action(
            "network.request",
            "feishu.sheet.read_rows",
            "self_pickup_source_sheet",
        ),
        _broker_action(
            "browser.invoke",
            "ronghui.problem.query",
            "account_id",
            "daxiang_s_account_id",
        ),
        _broker_action(
            "browser.invoke",
            "ronghui.problem.create",
            "account_id",
            "daxiang_s_account_id",
        ),
        _broker_action(
            "browser.invoke",
            "ronghui.problem.verify",
            "account_id",
            "daxiang_s_account_id",
        ),
    ),
    "split_pending_problem_upload": (
        _broker_action(
            "network.request",
            "feishu.sheet.read_rows",
            "split_pending_source_sheet",
        ),
        _broker_action(
            "projection.invoke",
            "split_pending.snapshot.read",
            "split_pending_target_sheet",
        ),
        _broker_action(
            "projection.invoke",
            "split_pending.snapshot.replace",
            "split_pending_target_sheet",
        ),
        _broker_action(
            "network.request",
            "feishu.sheet.replace_rows",
            "split_pending_target_sheet",
        ),
        _broker_action("browser.invoke", "ronghui.complaint.query", "account_id"),
        _broker_action("browser.invoke", "ronghui.complaint.create", "account_id"),
        _broker_action("browser.invoke", "ronghui.complaint.verify", "account_id"),
        _broker_action("browser.invoke", "ronghui.problem.query", "account_id"),
        _broker_action("browser.invoke", "ronghui.problem.create", "account_id"),
        _broker_action("browser.invoke", "ronghui.problem.verify", "account_id"),
        _broker_action(
            "projection.invoke",
            "split_pending.result.upsert",
            "split_pending_target_sheet",
        ),
        _broker_action("ledger.invoke", "daily_sign.problem_event.upsert", "account_id"),
    ),
    "sync_arrival_stats": (
        _broker_action("browser.invoke", "ronghui.arrive_list.read_page", "account_id"),
        _broker_action("browser.invoke", "ronghui.scan.read_page", "account_id"),
        _broker_action("browser.invoke", "ronghui.waybill_detail.read", "account_id"),
        _broker_action("projection.invoke", "scan.snapshot.replace", "account_id"),
        _broker_action("projection.invoke", "scan.snapshot.read", "account_id"),
        _broker_action("projection.invoke", "scan.snapshot.cleanup", "account_id"),
        _broker_action("projection.invoke", "arrival.snapshot.completed_before", "account_id"),
        _broker_action("projection.invoke", "waybill.snapshot.replace", "account_id"),
        _broker_action("projection.invoke", "waybill.pending.read", "account_id"),
        _broker_action("projection.invoke", "arrival.snapshot.replace", "account_id"),
        _broker_action("projection.invoke", "split_pending.snapshot.refresh", "account_id"),
        _broker_action(
            "network.request",
            "feishu.sheet.replace",
            "arrival_stats_primary_sheet",
            "arrival_stats_secondary_sheet",
            "arrival_stats_pending_sheet",
            "arrival_stats_split_pending_sheet",
        ),
        _broker_action(
            "network.request",
            "feishu.sheet.add",
            "arrival_stats_archive_sheet",
        ),
    ),
    "sync_arrive_list": (
        _broker_action("browser.invoke", "ronghui.arrive_list.read_page", "account_id"),
        _broker_action("projection.invoke", "waybill.snapshot.replace", "account_id"),
        _broker_action("projection.invoke", "arrival.forecast_snapshot.replace", "account_id"),
        _broker_action(
            "network.request",
            "feishu.sheet.replace",
            "arrive_primary_sheet",
            "arrive_secondary_sheet",
        ),
    ),
    "sync_customer_service_problems": (
        _broker_action(
            "browser.invoke",
            "customer_problem.list_page",
            "customer_service_source",
        ),
        _broker_action(
            "browser.invoke",
            "customer_problem.detail",
            "customer_service_source",
        ),
    ),
    "sync_daily_send_orders": (
        _broker_action("ledger.invoke", "sync_daily_send_orders.lock.acquire", "account_id"),
        _broker_action("ledger.invoke", "sync_daily_send_orders.lock.release", "account_id"),
        _broker_action("browser.invoke", "ronghui.send_order.read_page", "account_id"),
        _broker_action(
            "network.request",
            "feishu.bitable.list_records",
            "send_order_bitable",
        ),
        _broker_action(
            "network.request",
            "feishu.bitable.delete_records",
            "send_order_bitable",
        ),
        _broker_action(
            "network.request",
            "feishu.bitable.write_records",
            "send_order_bitable",
        ),
        _broker_action("projection.invoke", "waybill.ronghui.replace_date", "account_id"),
    ),
    "sync_daily_should_sign": (
        _broker_action(
            "ledger.invoke",
            "daily_sign.authoritative_sync",
            "r13_account_id",
            "account_id",
            "daily_sign_bitable",
            "daily_sign_sheet",
        ),
    ),
    "sync_delivery_status": (
        _broker_action(
            "network.request",
            "feishu.bitable.list_views",
            "delivery_status_bitable",
        ),
        _broker_action(
            "network.request",
            "feishu.bitable.list_records",
            "delivery_status_bitable",
        ),
        _broker_action("browser.invoke", "ronghui.delivery_status.read", "account_id"),
        _broker_action(
            "network.request",
            "feishu.bitable.write_records",
            "delivery_status_bitable",
        ),
        _broker_action("projection.invoke", "waybill.delivery_status.update", "account_id"),
    ),
    "sync_finance_bills": (
        _broker_action(
            "browser.invoke",
            "ronghui.finance.capture_page",
            "finance_quote_source",
            "finance_daxiang_s_source",
            "finance_self_pickup_source",
        ),
        _broker_action(
            "browser.invoke",
            "ronghui.finance.verify_source_totals",
            "finance_quote_source",
            "finance_daxiang_s_source",
            "finance_self_pickup_source",
        ),
        _broker_action(
            "ledger.invoke",
            "finance.batch.acquire",
            "finance_quote_source",
            "finance_daxiang_s_source",
            "finance_self_pickup_source",
        ),
        _broker_action(
            "ledger.invoke",
            "finance.source_snapshot.write",
            "finance_quote_source",
            "finance_daxiang_s_source",
            "finance_self_pickup_source",
        ),
        _broker_action(
            "ledger.invoke",
            "finance.projection.commit",
            "finance_quote_source",
            "finance_daxiang_s_source",
            "finance_self_pickup_source",
        ),
    ),
    "sync_scan_codes": (
        _broker_action("browser.invoke", "ronghui.scan.read_page", "account_id"),
        _broker_action("projection.invoke", "scan.snapshot.replace", "account_id"),
        _broker_action("browser.invoke", "ronghui.scan_next.submit", "account_id"),
        _broker_action("browser.invoke", "ronghui.scan_next.verify", "account_id"),
    ),
    "sync_site_send_list": (
        _broker_action("browser.invoke", "ronghui.site_send.read_page", "account_id"),
        _broker_action(
            "network.request",
            "feishu.bitable.replace_snapshot",
            "site_send_bitable",
        ),
        _broker_action(
            "network.request",
            "feishu.sheet.replace",
            "site_send_sheet",
        ),
    ),
    "sync_yunda_dispatch_forecast": (
        _broker_action("browser.invoke", "yunda.dispatch_forecast.read_page", "account_id"),
        _broker_action(
            "network.request",
            "feishu.bitable.append_yunda_dispatch_forecast",
            "dispatch_forecast_bitable",
        ),
    ),
    "sync_yunda_send_waybills": (
        _broker_action("browser.invoke", "yunda.send_waybill.list_page", "account_id"),
        _broker_action("browser.invoke", "yunda.special_line.list_page", "account_id"),
        _broker_action("browser.invoke", "yunda.waybill.tracking_detail", "account_id"),
        _broker_action("browser.invoke", "yunda.waybill.original_data", "account_id"),
        _broker_action("browser.invoke", "yunda.send_waybill.renderer_detail", "account_id"),
        _broker_action(
            "network.request",
            "feishu.bitable.replace_yunda_send_waybills_date",
            "send_waybills_bitable",
        ),
        _broker_action(
            "network.request",
            "feishu.sheet.replace_yunda_send_waybills",
            "send_waybills_sheet",
        ),
        _broker_action("projection.invoke", "waybill.yunda.replace_date", "account_id"),
    ),
}

# Only instances of these reviewed plugin packages are eligible for
# receipt-based unknown-write recovery. The keys retain the seeded first-party
# instance index for auditability; authorization deliberately uses the package
# identities in the values so repeated installed instances are covered.
RECOVERABLE_WRITE_PROJECT_PLUGINS: Mapping[str, str] = {
    "arrive_list": "sync_arrive_list",
    "arrival_stats": "sync_arrival_stats",
    "daily_sign": "sync_daily_should_sign",
    "delivery_status": "sync_delivery_status",
    "finance_startup_catchup": "sync_finance_bills",
}


def recoverable_write_broker_actions() -> frozenset[tuple[str, str, str]]:
    """Return the exact signed write pairs eligible for receipt locators.

    This is derived from the signed manifest source rather than a hand-kept
    count.  The Broker has a deliberately matching close-set and tests keep
    the two contracts equal whenever package permissions change.
    """

    return frozenset(
        (automation_id, action.operation, action.action)
        for automation_id, plugin_id in RECOVERABLE_WRITE_PROJECT_PLUGINS.items()
        for action in _FIRST_PARTY_BROKER_ACTIONS[plugin_id]
        if action.effect == "write"
    )

_SUBPROCESS_BROKER_ONLY_FIELDS = frozenset(
    {
        "request_body",
        "arrive_list_request_body",
        "scan_next_request_body",
        "flow_payload",
        "base_token",
        "spreadsheet_token",
        "table_id",
        "sheet_id",
        "view_id",
        "view_name",
        "range",
        "clear_range",
        "source_sheet_title",
        "session_profile",
        "screenshot_dir",
        "screenshot_map",
        "screenshot_path",
    }
)

_FIRST_PARTY_BROKER_CALL_LIMITS: Mapping[str, int] = {
    "clock_in_dual": 12,
    "r7_arrival_checkin": 32,
    "r7_departure_checkin": 32,
    "self_pickup_problem_upload": 768,
    "split_pending_problem_upload": 768,
    "sync_arrival_stats": 1000,
    "sync_arrive_list": 512,
    "sync_customer_service_problems": 1000,
    "sync_daily_send_orders": 1000,
    "sync_daily_should_sign": 1,
    "sync_delivery_status": 768,
    "sync_finance_bills": 768,
    "sync_scan_codes": 1000,
    "sync_site_send_list": 512,
    "sync_yunda_dispatch_forecast": 768,
    "sync_yunda_send_waybills": 1000,
}


def _is_broker_only_field(name: object) -> bool:
    normalized = str(name or "").strip().lower()
    return (
        normalized in _SUBPROCESS_BROKER_ONLY_FIELDS
        or normalized in {"account_id", "account_ids"}
        or normalized.endswith(("_account_id", "_account_ids"))
        or any(marker in normalized for marker in ("password", "cookie", "credential", "secret", "token"))
    )


def _subprocess_safe_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Remove values that must be resolved by the core broker.

    This is recursive because a caller must not smuggle an account identifier
    through a nested recheck item or an untyped request body.
    """

    result = copy.deepcopy(dict(schema))
    properties = result.get("properties")
    if isinstance(properties, dict):
        removed = {str(name) for name in properties if _is_broker_only_field(name)}
        result["properties"] = {
            str(name): _subprocess_safe_schema(value)
            if isinstance(value, Mapping)
            else copy.deepcopy(value)
            for name, value in properties.items()
            if str(name) not in removed
        }
        required = result.get("required")
        if isinstance(required, list):
            result["required"] = [str(name) for name in required if str(name) not in removed]
    items = result.get("items")
    if isinstance(items, Mapping):
        result["items"] = _subprocess_safe_schema(items)
    for branch_name in ("oneOf", "anyOf", "allOf"):
        branches = result.get(branch_name)
        if isinstance(branches, list):
            result[branch_name] = [
                _subprocess_safe_schema(branch) if isinstance(branch, Mapping) else copy.deepcopy(branch)
                for branch in branches
            ]
    return result


def expected_first_party_automation_ids() -> frozenset[str]:
    """The explicit UI/project and governed-schedule union (currently 18)."""

    return frozenset(_FIRST_PARTY_INSTANCES) | frozenset(APPROVED_SCHEDULED_TASK_PROFILES)


def expected_first_party_plugin_ids() -> frozenset[str]:
    _validate_shared_project_union()
    return frozenset(item.tool_name for item in _FIRST_PARTY_INSTANCES.values())


def release_first_party_plugin_ids() -> frozenset[str]:
    """Return the reviewed server actions admitted to this production release."""

    known = expected_first_party_plugin_ids()
    selected = frozenset(RUNNABLE_SERVER_FIRST_PARTY_PLUGIN_IDS)
    unknown = selected - known
    if unknown:
        raise PluginPackageError(
            "first-party release scope contains unknown actions: "
            + ", ".join(sorted(unknown))
        )
    return selected


def deferred_first_party_plugin_ids() -> frozenset[str]:
    """Return governed legacy tools that must not execute in this release."""

    return expected_first_party_plugin_ids() - release_first_party_plugin_ids()


def release_first_party_automation_ids() -> frozenset[str]:
    """Return only legacy instances backed by an admitted server action."""

    selected = release_first_party_plugin_ids()
    return frozenset(
        automation_id
        for automation_id, definition in _FIRST_PARTY_INSTANCES.items()
        if definition.tool_name in selected
    )


def deferred_first_party_automation_ids() -> frozenset[str]:
    """Persisted legacy identities hidden from execution in this release."""

    return expected_first_party_automation_ids() - release_first_party_automation_ids()


def deferred_first_party_automation_plugins() -> dict[str, str]:
    """Return reserved deferred legacy identities with their exact action."""

    deferred = deferred_first_party_automation_ids()
    return {
        automation_id: definition.tool_name
        for automation_id, definition in _FIRST_PARTY_INSTANCES.items()
        if automation_id in deferred
    }


def _validate_shared_project_union() -> None:
    expected = expected_first_party_automation_ids()
    actual = set(_FIRST_PARTY_INSTANCES)
    if actual != expected:
        raise PluginPackageError(
            "shared first-party definitions do not match the static/scheduled union; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    for automation_id, definition in _FIRST_PARTY_INSTANCES.items():
        if definition.automation_id != automation_id:
            raise PluginPackageError(f"first-party instance identity mismatch: {automation_id}")
        profile = APPROVED_SCHEDULED_TASK_PROFILES.get(automation_id)
        if profile is None:
            continue
        if (
            profile.tool_name != definition.tool_name
            or dict(profile.approved_arguments) != dict(definition.legacy_arguments)
            or frozenset(profile.approved_task_ids) != frozenset(definition.scheduled_task_ids)
        ):
            raise PluginPackageError(f"shared first-party profile mismatch: {automation_id}")


def _system_for_migration_account(account_id: object) -> str:
    value = str(account_id or "").strip().lower()
    for prefix, system in _ACCOUNT_SYSTEM_PREFIXES.items():
        if value.startswith(prefix):
            return system
    raise PluginPackageError("first-party account role has no explicit supported system")


def _account_roles_for_plugin(
    plugin_id: str,
    definitions: Sequence[Any],
    tool_contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    schema = tool_contract.get("input_schema")
    properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    roles: dict[str, dict[str, Any]] = {}
    for raw_role in _CODE_OWNED_ACCOUNT_ROLES.get(plugin_id, ()):
        role = str(raw_role["role"])
        roles[role] = {
            "systems": {"ronghui", "yunda"}
            if plugin_id == "sync_customer_service_problems"
            else {"ronghui"},
            "argument_field": str(raw_role["argument_field"]),
            "collection": raw_role["collection"] is True,
        }
    for definition in definitions:
        for field, account_id in definition.legacy_arguments.items():
            if field in {"account_id", "account_ids"} or field.endswith(("_account_id", "_account_ids")):
                role = roles.setdefault(
                    field,
                    {"systems": set(), "argument_field": field, "collection": False},
                )
                role["systems"].add(_system_for_migration_account(account_id))
    # A schema account role without a reviewed first-party binding is unsafe;
    # new instances remain unbound but the action still needs an allowed system.
    schema_roles = {
        str(field) for field in properties
        if str(field) in {"account_id", "account_ids"}
        or str(field).endswith(("_account_id", "_account_ids"))
    }
    if plugin_id not in _CODE_OWNED_ACCOUNT_ROLES:
        for role in sorted(schema_roles - set(roles)):
            declared_platforms = {
                str(definition.legacy_arguments.get("platform") or "").strip().lower()
                for definition in definitions
            }
            declared_platforms.discard("")
            if role == "account_id" and declared_platforms and declared_platforms <= {
                "ronghui",
                "yunda",
                "r7",
                "r13",
            }:
                roles[role] = {
                    "systems": declared_platforms,
                    "argument_field": role,
                    "collection": role.endswith("_ids"),
                }
    if plugin_id not in _CODE_OWNED_ACCOUNT_ROLES and schema_roles - set(roles):
        raise PluginPackageError(
            "first-party account role lacks an explicit migration system: "
            + ", ".join(sorted(schema_roles - set(roles)))
        )
    return [
        {
            "role": role,
            "allowed_systems": sorted(spec["systems"]),
            # Account selection never falls back to a tool/default account.
            # Every new instance must bind each declared role explicitly.
            "required": True,
            # The action process receives only a short-lived broker capability;
            # exact account IDs and sessions never cross the process boundary.
            "argument_field": None,
            "collection": spec["collection"] is True,
        }
        for role, spec in roles.items()
    ]


def _instance_visible_schema(
    tool_contract: Mapping[str, Any],
    *,
    dynamic_fields: set[str],
    resource_backed_fields: frozenset[str],
    hide_dynamic: bool,
) -> dict[str, Any]:
    schema = copy.deepcopy(dict(tool_contract["input_schema"]))
    properties = schema.get("properties", {})
    hidden = {
        str(name)
        for name in properties
        if str(name) == "account_id"
        or str(name) == "account_ids"
        or str(name).endswith(("_account_id", "_account_ids"))
        or str(name) in resource_backed_fields
        or (hide_dynamic and str(name) in dynamic_fields)
    }
    schema["properties"] = {
        name: value for name, value in properties.items() if name not in hidden
    }
    schema["required"] = [name for name in schema.get("required", []) if name not in hidden]
    return _subprocess_safe_schema(schema)


def first_party_instance_seeds() -> tuple[FirstPartyInstanceSeed, ...]:
    _validate_shared_project_union()
    return tuple(
        FirstPartyInstanceSeed(
            automation_id=automation_id,
            plugin_id=definition.tool_name,
            version=FIRST_PARTY_PACKAGE_VERSION,
            display_name=automation_id,
            allowed_entrypoints=tuple(sorted(definition.allowed_entrypoints)),
        )
        for automation_id, definition in sorted(_FIRST_PARTY_INSTANCES.items())
    )


def release_first_party_instance_seeds() -> tuple[FirstPartyInstanceSeed, ...]:
    selected = release_first_party_automation_ids()
    return tuple(
        seed for seed in first_party_instance_seeds() if seed.automation_id in selected
    )


def resolve_first_party_manifests(
    core_catalog: Any,
    *,
    _plugin_ids: frozenset[str] | None = None,
) -> dict[str, AutomationPluginManifest]:
    """Resolve immutable subprocess action packages for an explicit source set.

    The private selector lets the production release avoid even inspecting
    fail-closed action contracts.  Development callers omit it and continue to
    validate the complete first-party source inventory.
    """

    _validate_shared_project_union()
    known_plugin_ids = expected_first_party_plugin_ids()
    selected_plugin_ids = (
        known_plugin_ids if _plugin_ids is None else frozenset(_plugin_ids)
    )
    unknown_plugin_ids = selected_plugin_ids - known_plugin_ids
    if unknown_plugin_ids:
        raise PluginPackageError(
            "first-party manifest selector contains unknown actions: "
            + ", ".join(sorted(unknown_plugin_ids))
        )
    grouped: dict[str, list[Any]] = {}
    for definition in _FIRST_PARTY_INSTANCES.values():
        grouped.setdefault(definition.tool_name, []).append(definition)
    manifests: dict[str, AutomationPluginManifest] = {}
    for plugin_id, definitions in sorted(grouped.items()):
        if plugin_id not in selected_plugin_ids:
            continue
        capability = core_catalog.get_capability(plugin_id)
        if capability is None:
            raise PluginPackageError(f"first-party core tool is absent: {plugin_id}")
        capability = copy.deepcopy(dict(capability))
        capability["project_full_auto_allowed"] = capability.get("project_full_auto_allowed") is True
        removed_fields = _CODE_OWNED_REMOVED_ARGUMENT_FIELDS.get(
            plugin_id,
            frozenset(),
        )
        input_schema = capability.get("input_schema")
        if removed_fields and isinstance(input_schema, dict):
            properties = input_schema.get("properties")
            if isinstance(properties, dict):
                input_schema["properties"] = {
                    field: schema
                    for field, schema in properties.items()
                    if field not in removed_fields
                }
            required = input_schema.get("required")
            if isinstance(required, list):
                input_schema["required"] = [
                    field for field in required if field not in removed_fields
                ]
        for definition in definitions:
            profile = APPROVED_SCHEDULED_TASK_PROFILES.get(definition.automation_id)
            if profile is not None and str(capability.get("version") or "") != profile.tool_version:
                raise PluginPackageError(f"first-party tool/profile version mismatch: {definition.automation_id}")
        entrypoints = sorted(
            set().union(*(set(definition.allowed_entrypoints) for definition in definitions))
        )
        scheduler_dynamic_resolvers: dict[str, str] = {}
        for definition in definitions:
            for field, resolver_name in definition.legacy_dynamic_argument_rules.items():
                previous = scheduler_dynamic_resolvers.get(str(field))
                if previous is not None and previous != str(resolver_name):
                    raise PluginPackageError(f"conflicting first-party resolver for {plugin_id}.{field}")
                scheduler_dynamic_resolvers[str(field)] = str(resolver_name)
        entrypoint_dynamic_resolvers: dict[str, dict[str, str]] = {}
        declared_dynamic = _CODE_OWNED_ENTRYPOINT_DYNAMIC_FIELDS.get(plugin_id, {})
        fixed_resolvers = _CODE_OWNED_ENTRYPOINT_FIXED_RESOLVERS.get(plugin_id, {})
        for entrypoint in entrypoints:
            resolvers = (
                dict(scheduler_dynamic_resolvers)
                if entrypoint == "scheduler"
                else {}
            )
            for field in declared_dynamic.get(entrypoint, ()):
                if field in resolvers:
                    raise PluginPackageError(
                        f"conflicting first-party resolver for {plugin_id}.{field}"
                    )
                resolver_field = re.sub(r"[^a-z0-9_.-]", "_", field.lower())
                optional_prefix = (
                    "optional_"
                    if (plugin_id, entrypoint, field)
                    in _OPTIONAL_CODE_OWNED_ENTRYPOINT_DYNAMIC_FIELDS
                    else ""
                )
                resolvers[field] = (
                    f"verified_{optional_prefix}{entrypoint}_{resolver_field}"
                )
            for field, resolver_name in fixed_resolvers.get(entrypoint, {}).items():
                previous = resolvers.get(str(field))
                if previous is not None and previous != str(resolver_name):
                    raise PluginPackageError(
                        f"conflicting first-party resolver for {plugin_id}.{field}"
                    )
                resolvers[str(field)] = str(resolver_name)
            entrypoint_dynamic_resolvers[entrypoint] = resolvers
        all_dynamic_fields = {
            field
            for resolvers in entrypoint_dynamic_resolvers.values()
            for field in resolvers
        }
        config_schema = _instance_visible_schema(
            capability,
            dynamic_fields=all_dynamic_fields,
            resource_backed_fields=_RESOURCE_BACKED_ARGUMENT_FIELDS.get(
                plugin_id,
                frozenset(),
            ),
            hide_dynamic=plugin_id in _CONFIG_HIDDEN_DYNAMIC_PLUGINS,
        )
        invocation_schema = _instance_visible_schema(
            capability,
            dynamic_fields=all_dynamic_fields,
            resource_backed_fields=_RESOURCE_BACKED_ARGUMENT_FIELDS.get(
                plugin_id,
                frozenset(),
            ),
            hide_dynamic=False,
        )
        signed_tool_contract = copy.deepcopy(capability)
        signed_tool_contract["executor"] = "payload/main.py"
        signed_tool_contract["input_schema"] = copy.deepcopy(invocation_schema)
        entrypoint_input_schemas: dict[str, dict[str, Any]] = {}
        config_field_names = set(config_schema.get("properties", {}))
        for entrypoint in entrypoints:
            visible_fields = config_field_names | set(
                entrypoint_dynamic_resolvers[entrypoint]
            )
            entrypoint_schema = copy.deepcopy(invocation_schema)
            entrypoint_schema["properties"] = {
                field: schema
                for field, schema in entrypoint_schema.get("properties", {}).items()
                if field in visible_fields
            }
            entrypoint_schema["required"] = [
                field
                for field in entrypoint_schema.get("required", [])
                if field in visible_fields
            ]
            entrypoint_input_schemas[entrypoint] = entrypoint_schema
        governance_anchor = {
            field: copy.deepcopy(signed_tool_contract[field])
            for field in _GOVERNANCE_ANCHOR_FIELDS
        }
        broker_actions = _FIRST_PARTY_BROKER_ACTIONS.get(plugin_id)
        if not broker_actions:
            raise PluginPackageError(f"first-party action has no closed broker contract: {plugin_id}")
        declared_broker_roles = {
            str(item["role"])
            for item in _account_roles_for_plugin(
                plugin_id,
                definitions,
                capability,
            )
        } | {
            str(item["role"])
            for item in _ACTION_RESOURCE_ROLES.get(plugin_id, ())
        }
        referenced_roles = {role for action in broker_actions for role in action.roles}
        if referenced_roles != declared_broker_roles:
            raise PluginPackageError(
                f"first-party broker/account role mismatch for {plugin_id}: "
                f"declared={sorted(declared_broker_roles)}, "
                f"referenced={sorted(referenced_roles)}"
            )
        schedule_kinds: list[str] = []
        if "scheduler" in entrypoints:
            has_startup = any(
                (
                    APPROVED_SCHEDULED_TASK_PROFILES.get(definition.automation_id) is not None
                    and APPROVED_SCHEDULED_TASK_PROFILES[definition.automation_id].cron_expression
                    == "@startup"
                )
                for definition in definitions
            )
            has_daily = any(
                not (
                    APPROVED_SCHEDULED_TASK_PROFILES.get(definition.automation_id) is not None
                    and APPROVED_SCHEDULED_TASK_PROFILES[definition.automation_id].cron_expression
                    == "@startup"
                )
                for definition in definitions
            )
            if has_daily:
                schedule_kinds.append("daily_times")
            if has_startup:
                schedule_kinds.append("startup")
        manifest = AutomationPluginManifest.from_mapping(
            {
                "schema_version": 1,
                "plugin_id": plugin_id,
                "name": plugin_id,
                "version": FIRST_PARTY_PACKAGE_VERSION,
                "description": str(capability.get("description") or plugin_id),
                "execution_platform": "server",
                "runtime": {
                    "kind": "python_subprocess",
                    "entrypoint": "payload/main.py",
                },
                "config_schema": copy.deepcopy(config_schema),
                "account_roles": _account_roles_for_plugin(plugin_id, definitions, capability),
                "resource_roles": [
                    copy.deepcopy(dict(item))
                    for item in _CODE_OWNED_RESOURCE_ROLES.get(plugin_id, ())
                ],
                "scheduling": {
                    "supported": "scheduler" in entrypoints,
                    "allowed_kinds": schedule_kinds,
                    "max_daily_times": (
                        96
                        if plugin_id == "sync_customer_service_problems"
                        else 32
                    )
                    if "scheduler" in entrypoints
                    else 0,
                },
                "allowed_entrypoints": entrypoints,
                "invocation_contracts": {
                    entrypoint: {
                        "input_schema": copy.deepcopy(
                            entrypoint_input_schemas[entrypoint]
                        ),
                        "argument_template": {
                            field: {"source": "project_config", "key": field}
                            for field in sorted(config_schema.get("properties", {}))
                            if field
                            not in entrypoint_dynamic_resolvers[entrypoint]
                        },
                        "dynamic_resolvers": copy.deepcopy(
                            entrypoint_dynamic_resolvers[entrypoint]
                        ),
                    }
                    for entrypoint in entrypoints
                },
                "governance_anchor": governance_anchor,
                "tool_contract": signed_tool_contract,
                "worker_requirement": {
                    "required": False,
                    "interactive_session": False,
                    "supported_os": ["linux"],
                    "queue_deadline_seconds": 86400,
                },
                "project_full_auto_allowed": capability["project_full_auto_allowed"],
                "runtime_permissions": {
                    "network": any(item.operation == "network.request" for item in broker_actions),
                    "browser": any(item.operation == "browser.invoke" for item in broker_actions),
                    "office": False,
                    "file_roles": [],
                    "broker_operations": [
                        {
                            "operation": item.operation,
                            "action": item.action,
                            "roles": list(item.roles),
                            "effect": item.effect,
                        }
                        for item in broker_actions
                    ],
                    "max_broker_calls": _FIRST_PARTY_BROKER_CALL_LIMITS[plugin_id],
                },
            }
        )
        manifests[plugin_id] = manifest
    if set(manifests) != selected_plugin_ids:
        raise PluginPackageError("resolved first-party plugin package set is incomplete")
    return manifests


def resolve_release_first_party_manifests(
    core_catalog: Any,
) -> dict[str, AutomationPluginManifest]:
    """Resolve only actions admitted by the executable production allowlist."""

    selected = release_first_party_plugin_ids()
    scoped = resolve_first_party_manifests(core_catalog, _plugin_ids=selected)
    if set(scoped) != selected:
        raise PluginPackageError("first-party release manifest set is incomplete")
    if any(
        manifest.execution_platform != "server"
        or manifest.worker_requirement.get("required") is not False
        for manifest in scoped.values()
    ):
        raise PluginPackageError("first-party release scope is not server-only")
    return scoped


def release_first_party_broker_action_keys(
    core_catalog: Any,
) -> frozenset[tuple[str, str]]:
    """Return the only first-party Broker pairs exposed by this release."""

    return frozenset(
        (str(item["operation"]), str(item["action"]))
        for manifest in resolve_release_first_party_manifests(core_catalog).values()
        for item in manifest.runtime_permissions["broker_operations"]
    )


def first_party_payload_files(manifest: AutomationPluginManifest) -> dict[str, bytes]:
    """Return the action-owned source files embedded in one signed ZIP."""

    if manifest.plugin_id not in expected_first_party_plugin_ids():
        raise PluginPackageError("unknown first-party action package")
    action_path = FIRST_PARTY_ROOT / manifest.plugin_id / "payload" / "action.py"
    required = (FIRST_PARTY_RUNTIME_PATH, FIRST_PARTY_RESULT_PATH, action_path)
    if any(not path.is_file() for path in required):
        missing = [str(path.relative_to(FIRST_PARTY_ROOT)) for path in required if not path.is_file()]
        raise PluginPackageError(
            "first-party action source is incomplete: " + ", ".join(missing)
        )
    return {
        "payload/main.py": FIRST_PARTY_RUNTIME_PATH.read_bytes(),
        "payload/action.py": action_path.read_bytes(),
        "payload/boyi_plugin_result.py": FIRST_PARTY_RESULT_PATH.read_bytes(),
        "payload/boyi_plugin_sdk.py": PLUGIN_SDK_SOURCE.encode("utf-8"),
    }


def _builtin_release_files(manifest: AutomationPluginManifest) -> dict[str, bytes]:
    manifest_bytes = canonical_json_bytes(manifest.to_signed_mapping())
    attestation = canonical_json_bytes(
        {
            "schema_version": 1,
            "trust_source": PluginTrustSource.BUILTIN_RELEASE.value,
            "plugin_id": manifest.plugin_id,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
    )
    return {
        "builtin_release.json": attestation,
        "manifest.json": manifest_bytes,
        **first_party_payload_files(manifest),
    }


def build_builtin_release_package(manifest: AutomationPluginManifest) -> bytes:
    """Build a deterministic, digest-locked ``builtin_release`` action ZIP."""

    return _zip_bytes(_builtin_release_files(manifest))


def first_party_digest_snapshot(core_catalog: Any) -> dict[str, Any]:
    plugins: dict[str, dict[str, str]] = {}
    for plugin_id, manifest in resolve_first_party_manifests(core_catalog).items():
        package = build_builtin_release_package(manifest)
        plugins[plugin_id] = {
            "manifest_sha256": manifest.manifest_sha256,
            "package_sha256": hashlib.sha256(package).hexdigest(),
        }
    return {"schema_version": 1, "plugins": plugins}


def release_first_party_digest_snapshot(core_catalog: Any) -> dict[str, Any]:
    """Digest only actions admitted to the executable production release."""

    plugins: dict[str, dict[str, str]] = {}
    for plugin_id, manifest in resolve_release_first_party_manifests(
        core_catalog
    ).items():
        package = build_builtin_release_package(manifest)
        plugins[plugin_id] = {
            "manifest_sha256": manifest.manifest_sha256,
            "package_sha256": hashlib.sha256(package).hexdigest(),
        }
    return {"schema_version": 1, "plugins": plugins}


class SourceFirstPartyPackageProvider:
    """Development-only source descriptor provider.

    Production bootstrap rejects ``BUILTIN_RELEASE`` unless the caller opts
    into the explicit development flag.  Production uses
    ``SignedFirstPartyPackageProvider`` below.
    """

    def __init__(self, digest_lock_path: Path | str = DIGEST_LOCK_PATH) -> None:
        self._digest_lock_path = Path(digest_lock_path)

    def _load_digest_lock(self) -> dict[str, Any]:
        if not self._digest_lock_path.is_file():
            raise PluginPackageError("first-party digest lock does not exist")
        raw = self._digest_lock_path.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginPackageError("first-party digest lock is invalid") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "plugins"}:
            raise PluginPackageError("first-party digest lock schema is invalid")
        if value["schema_version"] != 1 or not isinstance(value["plugins"], dict):
            raise PluginPackageError("first-party digest lock schema version is invalid")
        if raw not in {canonical_json_bytes(value), canonical_json_bytes(value) + b"\n"}:
            raise PluginPackageError("first-party digest lock must use canonical JSON")
        return value

    def load_versions(
        self,
        *,
        core_catalog: object,
        current_release_sha: str,
        expected_release_sha: str,
    ) -> Sequence[PluginVersionRecord]:
        current = str(current_release_sha or "").strip().lower()
        expected = str(expected_release_sha or "").strip().lower()
        if not _RELEASE_SHA_RE.fullmatch(current) or current != expected:
            raise PluginPackageError("current release SHA does not match verified release identity")
        manifests = resolve_release_first_party_manifests(core_catalog)
        lock = self._load_digest_lock()["plugins"]
        if set(lock) != expected_first_party_plugin_ids():
            raise PluginPackageError("first-party digest lock plugin set is stale")
        records: list[PluginVersionRecord] = []
        for plugin_id, manifest in sorted(manifests.items()):
            package_digest = hashlib.sha256(build_builtin_release_package(manifest)).hexdigest()
            expected_digests = lock[plugin_id]
            if not isinstance(expected_digests, dict) or set(expected_digests) != {
                "manifest_sha256",
                "package_sha256",
            }:
                raise PluginPackageError(f"first-party digest entry is invalid: {plugin_id}")
            if expected_digests["manifest_sha256"] != manifest.manifest_sha256:
                raise PluginPackageError(f"first-party manifest digest mismatch: {plugin_id}")
            if expected_digests["package_sha256"] != package_digest:
                raise PluginPackageError(f"first-party package digest mismatch: {plugin_id}")
            records.append(
                PluginVersionRecord(
                    plugin_id=plugin_id,
                    version=manifest.version,
                    package_sha256=package_digest,
                    manifest_sha256=manifest.manifest_sha256,
                    manifest=manifest.to_signed_mapping(),
                    trust_source=PluginTrustSource.BUILTIN_RELEASE,
                    install_root=None,
                    release_sha=current,
                )
            )
        return records


def verify_release_first_party_source_review(
    core_catalog: Any,
    *,
    digest_lock_path: Path | str = DIGEST_LOCK_PATH,
) -> dict[str, Any]:
    """Require exact reviewed manifest and payload bytes for RUNNABLE actions."""

    lock = SourceFirstPartyPackageProvider(digest_lock_path)._load_digest_lock()
    snapshot = release_first_party_digest_snapshot(core_catalog)
    locked_plugins = lock["plugins"]
    for plugin_id, current in snapshot["plugins"].items():
        locked = locked_plugins.get(plugin_id)
        if not isinstance(locked, dict) or locked != current:
            raise PluginPackageError(
                f"first-party release source is not digest-reviewed: {plugin_id}"
            )
    return snapshot


class SignedFirstPartyPackageProvider:
    """Verify and materialize the complete offline-signed first-party release."""

    _INDEX_NAME = "release-index.json"

    def __init__(
        self,
        *,
        artifact_root: Path | str,
        signature_verifier: PackageSignatureVerifier,
        storage: PluginStoragePort | None = None,
        environments: PluginEnvironmentBuilderPort | None = None,
        digest_lock_path: Path | str = DIGEST_LOCK_PATH,
    ) -> None:
        self._artifact_root = Path(artifact_root)
        self._signature_verifier = signature_verifier
        self._storage = storage
        self._environments = environments
        self._digest_lock_path = Path(digest_lock_path)
        self._verified: dict[tuple[str, str], VerifiedPluginPackage] = {}

    @staticmethod
    def _same_immutable_identity(
        persisted: PluginVersionRecord,
        descriptor: PluginVersionRecord,
    ) -> bool:
        return (
            persisted.plugin_id == descriptor.plugin_id
            and persisted.version == descriptor.version
            and persisted.package_sha256 == descriptor.package_sha256
            and persisted.manifest_sha256 == descriptor.manifest_sha256
            and persisted.trust_source == descriptor.trust_source
            and canonical_json_bytes(persisted.manifest)
            == canonical_json_bytes(descriptor.manifest)
        )

    @staticmethod
    def _normalized_install_metadata(
        version: PluginVersionRecord,
    ) -> dict[str, Any]:
        root = str(version.install_root or "")
        if not root:
            raise PluginPackageError(
                "persisted first-party package has no immutable install root"
            )
        metadata = copy.deepcopy(dict(version.install_metadata))
        recorded_root = metadata.get("install_root")
        if recorded_root is not None and str(recorded_root) != root:
            raise PluginPackageError(
                "persisted first-party install metadata changed its root"
            )
        metadata["install_root"] = root
        return metadata

    @staticmethod
    def _validate_python_relative(
        root: Path,
        relative: object,
    ) -> None:
        expected = (
            "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"
        )
        if str(relative or "") != expected:
            raise PluginPackageError(
                "persisted first-party Python interpreter path is invalid"
            )
        pure = PurePosixPath(expected)
        target = root.joinpath(*pure.parts)
        if target.is_symlink():
            raise PluginPackageError(
                "persisted first-party Python interpreter is unsafe"
            )
        try:
            resolved = target.resolve()
            resolved.relative_to(root)
            validate_regular_plugin_file(target)
        except Exception as exc:
            raise PluginPackageError(
                "persisted first-party Python interpreter is missing or unsafe"
            ) from exc

    def _validate_persisted_materialization(
        self,
        *,
        persisted: PluginVersionRecord,
        descriptor: PluginVersionRecord,
    ) -> bool:
        """Return False only when the exact safe target is genuinely absent."""

        if (
            descriptor.trust_source != PluginTrustSource.ED25519_FIRST_PARTY
            or not self._same_immutable_identity(persisted, descriptor)
        ):
            raise PluginPackageError(
                "persisted first-party package differs from the verified release"
            )
        verified = self._verified.get((descriptor.plugin_id, descriptor.version))
        if (
            verified is None
            or verified.package_sha256 != descriptor.package_sha256
            or verified.manifest_sha256 != descriptor.manifest_sha256
            or verified.manifest.to_signed_mapping() != descriptor.manifest
        ):
            raise PluginPackageError(
                "persisted first-party package was not verified in this release"
            )
        expected_root, exists = self._storage.inspect_expected_version_root(
            plugin_id=descriptor.plugin_id,
            version=descriptor.version,
            manifest_sha256=descriptor.manifest_sha256,
        )
        metadata = self._normalized_install_metadata(persisted)
        if (
            Path(str(persisted.install_root)) != expected_root
            or metadata.get("install_root") != str(expected_root)
        ):
            raise PluginPackageError(
                "persisted first-party install root is not the deterministic target"
            )
        expected_files = [
            {"path": item.path, "sha256": item.sha256, "size": item.size}
            for item in verified.files
        ]
        if (
            set(metadata)
            != {
                "signing_key_id",
                "python_relative",
                "archive_relative",
                "archive_sha256",
                "package_files",
                "install_root",
            }
            or metadata.get("signing_key_id") != verified.signing_key_id
            or metadata.get("archive_relative") != VERIFIED_ARCHIVE_RELATIVE
            or metadata.get("archive_sha256") != verified.package_sha256
            or metadata.get("package_files") != expected_files
        ):
            raise PluginPackageError(
                "persisted first-party install metadata differs from signed bytes"
            )
        if not exists:
            return False
        try:
            FilesystemPluginIntegrityVerifier().verify_install_root(
                {
                    "install_root": str(expected_root),
                    "install_metadata": metadata,
                }
            )
            if {item.name for item in expected_root.iterdir()} != {
                "package",
                "venv",
                VERIFIED_ARCHIVE_RELATIVE,
            }:
                raise PluginPackageError(
                    "persisted first-party immutable root has unexpected entries"
                )
            package_root = expected_root / "package"
            actual_package_files = sorted(
                path.relative_to(package_root).as_posix()
                for path in package_root.rglob("*")
                if path.is_file()
            )
            expected_package_files = sorted(
                [*(item.path for item in verified.files), SIGNATURE_NAME]
            )
            if actual_package_files != expected_package_files:
                raise PluginPackageError(
                    "persisted first-party package tree differs from signed files"
                )
            with zipfile.ZipFile(
                io.BytesIO(verified.archive_bytes),
                mode="r",
            ) as archive:
                expected_signature = archive.read(SIGNATURE_NAME)
            signature_path = package_root / SIGNATURE_NAME
            validate_regular_plugin_file(signature_path)
            if signature_path.read_bytes() != expected_signature:
                raise PluginPackageError(
                    "persisted first-party extracted signature is corrupt"
                )
            self._storage.read_verified_archive(
                expected_root,
                VERIFIED_ARCHIVE_RELATIVE,
                expected_sha256=verified.package_sha256,
            )
            self._validate_python_relative(
                expected_root,
                metadata.get("python_relative"),
            )
        except PluginPackageError:
            raise
        except Exception as exc:
            raise PluginPackageError(
                "persisted first-party immutable root failed integrity verification"
            ) from exc
        return True

    def _load_index(self) -> dict[str, Any]:
        root = self._artifact_root
        if root.is_symlink() or not root.is_dir():
            raise PluginPackageError("signed first-party artifact directory is missing or unsafe")
        path = root / self._INDEX_NAME
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise PluginPackageError("signed first-party release index is missing or unsafe")
        raw = path.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PluginPackageError("signed first-party release index is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "release_sha", "plugins"}
            or value.get("schema_version") != 1
            or not isinstance(value.get("plugins"), dict)
            or raw not in {canonical_json_bytes(value), canonical_json_bytes(value) + b"\n"}
        ):
            raise PluginPackageError("signed first-party release index schema is invalid")
        return value

    def load_versions(
        self,
        *,
        core_catalog: object,
        current_release_sha: str,
        expected_release_sha: str,
    ) -> Sequence[PluginVersionRecord]:
        current = str(current_release_sha or "").strip().lower()
        expected = str(expected_release_sha or "").strip().lower()
        if not _RELEASE_SHA_RE.fullmatch(current) or current != expected:
            raise PluginPackageError("current release SHA does not match verified release identity")
        manifests = resolve_release_first_party_manifests(core_catalog)
        verify_release_first_party_source_review(
            core_catalog,
            digest_lock_path=self._digest_lock_path,
        )
        source_lock = SourceFirstPartyPackageProvider(
            self._digest_lock_path
        )._load_digest_lock()["plugins"]
        missing_source_reviews = set(manifests) - set(source_lock)
        if missing_source_reviews:
            raise PluginPackageError(
                "first-party source digest lock is missing release actions: "
                + ", ".join(sorted(missing_source_reviews))
            )
        index = self._load_index()
        if str(index["release_sha"] or "").strip().lower() != current:
            raise PluginPackageError("signed first-party index belongs to another release")
        indexed = index["plugins"]
        if set(indexed) != set(manifests):
            raise PluginPackageError("signed first-party artifact set is incomplete")
        expected_files = {
            self._INDEX_NAME,
            *(f"{plugin_id}-{manifest.version}.zip" for plugin_id, manifest in manifests.items()),
        }
        actual_files = set()
        for item in self._artifact_root.iterdir():
            if item.is_symlink() or not item.is_file():
                raise PluginPackageError("signed first-party artifact directory contains unsafe entries")
            actual_files.add(item.name)
        if actual_files != expected_files:
            raise PluginPackageError("signed first-party artifact directory has missing or extra files")
        verified_by_key: dict[tuple[str, str], VerifiedPluginPackage] = {}
        records: list[PluginVersionRecord] = []
        for plugin_id, source_manifest in sorted(manifests.items()):
            row = indexed[plugin_id]
            source_row = source_lock[plugin_id]
            if not isinstance(row, dict) or set(row) != {
                "version",
                "manifest_sha256",
                "package_sha256",
            }:
                raise PluginPackageError(f"signed first-party index entry is invalid: {plugin_id}")
            if not isinstance(source_row, dict):
                raise PluginPackageError(f"first-party source digest entry is invalid: {plugin_id}")
            if (
                row["version"] != source_manifest.version
                or row["manifest_sha256"] != source_manifest.manifest_sha256
                or source_row.get("manifest_sha256") != source_manifest.manifest_sha256
            ):
                raise PluginPackageError(f"signed first-party manifest is not source-reviewed: {plugin_id}")
            archive_path = self._artifact_root / f"{plugin_id}-{source_manifest.version}.zip"
            verified = verify_signed_plugin_zip(
                archive_path,
                verifier=self._signature_verifier,
                expected_package_sha256=str(row["package_sha256"]),
            )
            if (
                verified.manifest.plugin_id != plugin_id
                or verified.manifest.version != source_manifest.version
                or verified.manifest_sha256 != source_manifest.manifest_sha256
                or verified.manifest.to_signed_mapping()
                != source_manifest.to_signed_mapping()
            ):
                raise PluginPackageError(f"signed first-party package contract drifted: {plugin_id}")
            verified_by_key[(plugin_id, source_manifest.version)] = verified
            records.append(
                PluginVersionRecord(
                    plugin_id=plugin_id,
                    version=source_manifest.version,
                    package_sha256=verified.package_sha256,
                    manifest_sha256=verified.manifest_sha256,
                    manifest=verified.manifest.to_signed_mapping(),
                    trust_source=PluginTrustSource.ED25519_FIRST_PARTY,
                    install_root=None,
                    release_sha=current,
                    install_metadata={"signing_key_id": verified.signing_key_id},
                )
            )
        # Publish verified bytes only after the complete set passes.
        self._verified = verified_by_key
        return records

    def materialize(self, version: PluginVersionRecord) -> PluginVersionRecord:
        if self._storage is None or self._environments is None:
            raise PluginPackageError("signed first-party materialization storage is not configured")
        key = (version.plugin_id, version.version)
        verified = self._verified.get(key)
        if verified is None or version.trust_source != PluginTrustSource.ED25519_FIRST_PARTY:
            raise PluginPackageError("signed first-party version was not verified as part of this release")
        if (
            verified.package_sha256 != version.package_sha256
            or verified.manifest_sha256 != version.manifest_sha256
        ):
            raise PluginPackageError("signed first-party version changed before materialization")
        staging = self._storage.create_staging_root(version.plugin_id, version.version)
        committed: Path | None = None
        try:
            package_root = staging / "package"
            extract_verified_package(verified, package_root)
            archive_relative = self._storage.persist_verified_archive(
                staging,
                verified.archive_bytes,
                expected_sha256=verified.package_sha256,
            )
            python_path = self._environments.build(staging, verified.manifest)
            python_relative = str(python_path.relative_to(staging)).replace("\\", "/")
            committed = self._storage.commit_staging_root(
                staging,
                plugin_id=version.plugin_id,
                version=version.version,
                manifest_sha256=version.manifest_sha256,
            )
            return PluginVersionRecord(
                plugin_id=version.plugin_id,
                version=version.version,
                package_sha256=version.package_sha256,
                manifest_sha256=version.manifest_sha256,
                manifest=verified.manifest.to_signed_mapping(),
                trust_source=version.trust_source,
                install_root=str(committed),
                state=version.state,
                installed_at=version.installed_at,
                release_sha=version.release_sha,
                install_metadata={
                    "signing_key_id": verified.signing_key_id,
                    "python_relative": python_relative,
                    "archive_relative": archive_relative,
                    "archive_sha256": verified.package_sha256,
                    "package_files": [
                        {"path": item.path, "sha256": item.sha256, "size": item.size}
                        for item in verified.files
                    ],
                },
            )
        except Exception:
            if committed is not None:
                self._storage.remove_version_root(committed)
            else:
                self._storage.discard_staging_root(staging)
            raise

    def recover_missing(
        self,
        *,
        persisted: PluginVersionRecord,
        descriptor: PluginVersionRecord,
    ) -> PluginVersionRecord | None:
        """Rebuild only one verified, deterministic root that is exactly absent."""

        if self._storage is None or self._environments is None:
            raise PluginPackageError(
                "signed first-party recovery storage is not configured"
            )
        if self._validate_persisted_materialization(
            persisted=persisted,
            descriptor=descriptor,
        ):
            return None
        try:
            rebuilt = self.materialize(descriptor)
        except PluginConflictError as conflict:
            # Another bootstrap may have atomically committed the same target.
            # It is usable only after the complete persisted-byte validation;
            # recovery never overwrites or adopts an unverified directory.
            if self._validate_persisted_materialization(
                persisted=persisted,
                descriptor=descriptor,
            ):
                return None
            raise PluginPackageError(
                "first-party recovery target appeared without valid bytes"
            ) from conflict
        try:
            if (
                not self._same_immutable_identity(rebuilt, persisted)
                or self._normalized_install_metadata(rebuilt)
                != self._normalized_install_metadata(persisted)
                or not self._validate_persisted_materialization(
                    persisted=persisted,
                    descriptor=descriptor,
                )
            ):
                raise PluginPackageError(
                    "rebuilt first-party materialization did not verify"
                )
        except Exception:
            self.discard(rebuilt)
            raise
        return rebuilt

    def discard(self, version: PluginVersionRecord) -> None:
        if self._storage is None:
            raise PluginPackageError("signed first-party materialization storage is not configured")
        if version.install_root:
            self._storage.remove_version_root(Path(version.install_root))


@dataclass(frozen=True)
class FirstPartyReleasePreflight:
    release_sha: str
    package_count: int
    instance_count: int
    contracts_sha256: str


def preflight_signed_first_party_release(
    *,
    artifact_root: Path | str,
    signature_verifier: PackageSignatureVerifier,
    core_catalog: object,
    release_sha: str,
    digest_lock_path: Path | str = DIGEST_LOCK_PATH,
) -> FirstPartyReleasePreflight:
    """Read-only validation used before any migration or service mutation."""

    provider = SignedFirstPartyPackageProvider(
        artifact_root=artifact_root,
        signature_verifier=signature_verifier,
        digest_lock_path=digest_lock_path,
    )
    records = tuple(
        provider.load_versions(
            core_catalog=core_catalog,
            current_release_sha=release_sha,
            expected_release_sha=release_sha,
        )
    )
    if (
        {record.plugin_id for record in records} != release_first_party_plugin_ids()
        or any(record.trust_source != PluginTrustSource.ED25519_FIRST_PARTY for record in records)
    ):
        raise PluginPackageError("signed first-party preflight returned an invalid package set")
    contract_rows = [
        {
            "plugin_id": record.plugin_id,
            "version": record.version,
            "package_sha256": record.package_sha256,
            "manifest_sha256": record.manifest_sha256,
            "trust_source": record.trust_source.value,
        }
        for record in sorted(records, key=lambda item: item.plugin_id)
    ]
    return FirstPartyReleasePreflight(
        release_sha=str(release_sha).lower(),
        package_count=len(records),
        instance_count=len(release_first_party_instance_seeds()),
        contracts_sha256=hashlib.sha256(canonical_json_bytes(contract_rows)).hexdigest(),
    )


class FilesystemFirstPartyPackageMaterializer:
    """Materialize verified release-source bytes without a production private key.

    This is deliberately separate from the Ed25519 upload path: trust was
    established by the release SHA plus the committed digest lock.  The
    materialized result is still immutable and receives its own isolated venv.
    """

    def __init__(
        self,
        *,
        storage: PluginStoragePort,
        environments: PluginEnvironmentBuilderPort,
    ) -> None:
        self._storage = storage
        self._environments = environments

    def materialize(self, version: PluginVersionRecord) -> PluginVersionRecord:
        if version.trust_source != PluginTrustSource.BUILTIN_RELEASE:
            raise PluginPackageError("first-party materializer accepts builtin_release only")
        manifest = AutomationPluginManifest.from_mapping(version.manifest)
        package = build_builtin_release_package(manifest)
        if hashlib.sha256(package).hexdigest() != version.package_sha256:
            raise PluginPackageError("built-in package bytes changed before materialization")
        files = _builtin_release_files(manifest)
        if (
            hashlib.sha256(canonical_json_bytes(manifest.to_signed_mapping())).hexdigest()
            != version.manifest_sha256
        ):
            raise PluginPackageError("built-in manifest changed before materialization")
        staging = self._storage.create_staging_root(manifest.plugin_id, manifest.version)
        committed: Path | None = None
        try:
            package_root = staging / "package"
            package_root.mkdir(parents=True, exist_ok=False)
            for name, content in sorted(files.items()):
                relative = Path(*name.split("/"))
                output = package_root / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
                try:
                    output.chmod(0o444)
                except OSError:
                    pass
            archive_relative = self._storage.persist_verified_archive(
                staging,
                package,
                expected_sha256=version.package_sha256,
            )
            python_path = self._environments.build(staging, manifest)
            python_relative = str(python_path.relative_to(staging)).replace("\\", "/")
            committed = self._storage.commit_staging_root(
                staging,
                plugin_id=manifest.plugin_id,
                version=manifest.version,
                manifest_sha256=manifest.manifest_sha256,
            )
            return PluginVersionRecord(
                plugin_id=version.plugin_id,
                version=version.version,
                package_sha256=version.package_sha256,
                manifest_sha256=version.manifest_sha256,
                manifest=manifest.to_signed_mapping(),
                trust_source=version.trust_source,
                install_root=str(committed),
                state=version.state,
                installed_at=version.installed_at,
                release_sha=version.release_sha,
                install_metadata={
                    "python_relative": python_relative,
                    "archive_relative": archive_relative,
                    "archive_sha256": version.package_sha256,
                    "package_files": [
                        {
                            "path": name,
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "size": len(content),
                        }
                        for name, content in sorted(files.items())
                    ],
                },
            )
        except Exception:
            if committed is not None:
                self._storage.remove_version_root(committed)
            else:
                self._storage.discard_staging_root(staging)
            raise

    def discard(self, version: PluginVersionRecord) -> None:
        if version.install_root:
            self._storage.remove_version_root(Path(version.install_root))


def bootstrap_first_party_plugins(
    repository: AutomationPluginRepositoryPort,
    *,
    core_catalog: object,
    current_release_sha: str,
    expected_release_sha: str,
    package_provider: FirstPartyPackageProvider | None = None,
    package_materializer: FirstPartyPackageMaterializerPort | None = None,
    allow_development_builtin: bool = False,
) -> BootstrapResult:
    """Install missing instances and stage older signed first-party versions.

    Administrator bindings, schedules, enabled entrypoints (including an empty
    set), and durable approval intent are preserved across the staged upgrade.
    """

    try:
        if package_provider is None:
            raise PluginPackageError("signed first-party package provider is required")
        provider = package_provider
        if package_materializer is None and isinstance(provider, FirstPartyPackageMaterializerPort):
            package_materializer = provider
        descriptors = tuple(
            provider.load_versions(
                core_catalog=core_catalog,
                current_release_sha=current_release_sha,
                expected_release_sha=expected_release_sha,
            )
        )
        if {item.plugin_id for item in descriptors} != release_first_party_plugin_ids():
            raise PluginPackageError("first-party provider returned an incomplete package set")
        if (
            not allow_development_builtin
            and any(item.trust_source == PluginTrustSource.BUILTIN_RELEASE for item in descriptors)
        ):
            raise PluginPackageError("builtin_release is not trusted for production bootstrap")
        if any(
            item.trust_source
            not in {PluginTrustSource.ED25519_FIRST_PARTY, PluginTrustSource.BUILTIN_RELEASE}
            for item in descriptors
        ):
            raise PluginPackageError("first-party provider returned an unsupported trust source")
        versions: list[PluginVersionRecord] = []
        newly_materialized: list[PluginVersionRecord] = []

        def discard_unpersisted_materializations() -> list[tuple[str, str, str]]:
            cleanup_errors: list[tuple[str, str, str]] = []
            for version in reversed(newly_materialized):
                try:
                    installed = repository.get_package_version(
                        version.plugin_id,
                        version.version,
                    )
                    if installed is None and version.install_root:
                        assert package_materializer is not None
                        package_materializer.discard(version)
                except Exception as exc:
                    cleanup_errors.append(
                        (version.plugin_id, version.version, type(exc).__name__)
                    )
            return cleanup_errors

        try:
            for descriptor in descriptors:
                existing = repository.get_package_version(
                    descriptor.plugin_id,
                    descriptor.version,
                )
                if existing is not None:
                    if (
                        existing.plugin_id != descriptor.plugin_id
                        or existing.version != descriptor.version
                        or existing.package_sha256 != descriptor.package_sha256
                        or existing.manifest_sha256 != descriptor.manifest_sha256
                        or existing.trust_source != descriptor.trust_source
                        or canonical_json_bytes(existing.manifest)
                        != canonical_json_bytes(descriptor.manifest)
                        or existing.install_root is None
                    ):
                        raise PluginPackageError(
                            "existing first-party package is stale or not materialized: "
                            f"{descriptor.plugin_id}"
                        )
                    if descriptor.trust_source == PluginTrustSource.ED25519_FIRST_PARTY:
                        if not isinstance(
                            package_materializer,
                            FirstPartyPackageRecoveryMaterializerPort,
                        ):
                            raise PluginPackageError(
                                "signed first-party recovery materializer is required: "
                                f"{descriptor.plugin_id}"
                            )
                        rebuilt = package_materializer.recover_missing(
                            persisted=existing,
                            descriptor=descriptor,
                        )
                        if rebuilt is not None:
                            newly_materialized.append(rebuilt)
                    # ``existing`` is deliberately retained: a rebuilt record
                    # is only filesystem proof and must never replace immutable
                    # DB bytes during the idempotent registration below.
                    versions.append(existing)
                    continue
                if descriptor.install_root is None:
                    if package_materializer is None:
                        raise PluginPackageError(
                            "first-party package materializer is required: "
                            f"{descriptor.plugin_id}"
                        )
                    descriptor = package_materializer.materialize(descriptor)
                    newly_materialized.append(descriptor)
                if descriptor.install_root is None:
                    raise PluginPackageError(
                        f"first-party package was not materialized: {descriptor.plugin_id}"
                    )
                versions.append(descriptor)
            seeds = release_first_party_instance_seeds()
            persisted = repository.bootstrap_missing(
                tuple(versions),
                seeds,
                release_sha=current_release_sha.lower(),
            )
        except Exception as bootstrap_error:
            cleanup_errors = discard_unpersisted_materializations()
            if cleanup_errors:
                if isinstance(bootstrap_error, AutomationPluginError):
                    original_error = f"{bootstrap_error.code}: {bootstrap_error}"
                else:
                    original_error = type(bootstrap_error).__name__.upper()
                cleanup_summary = ", ".join(
                    f"{plugin_id}@{version}:{error_type}"
                    for plugin_id, version, error_type in cleanup_errors
                )
                raise PluginPackageError(
                    f"first-party bootstrap failed ({original_error}); "
                    f"materialization cleanup failed: {cleanup_summary}"
                ) from bootstrap_error
            raise
        expected_instances = release_first_party_automation_ids()
        if set(persisted.created) & set(persisted.existing):
            raise PluginPackageError("repository returned overlapping bootstrap states")
        if set(persisted.created) | set(persisted.existing) != expected_instances:
            raise PluginPackageError("repository returned an incomplete instance bootstrap result")
        return BootstrapResult(
            created=tuple(sorted(persisted.created)),
            existing=tuple(sorted(persisted.existing)),
            rejected={},
        )
    except AutomationPluginError as exc:
        return BootstrapResult(created=(), existing=(), rejected={"*": f"{exc.code}: {exc}"})
    except Exception as exc:
        return BootstrapResult(
            created=(),
            existing=(),
            rejected={"*": f"BOOTSTRAP_FAILED: {type(exc).__name__}"},
        )
