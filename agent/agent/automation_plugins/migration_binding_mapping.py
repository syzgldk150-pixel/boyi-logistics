"""Code-owned one-to-one binding maps for reviewed Action-v1 migrations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from agent.automation_plugins.errors import PluginConflictError


@dataclass(frozen=True)
class MigrationBindingMapping:
    """Exact source-role to target-role maps for one reviewed plugin pair."""

    account_roles: Mapping[str, str]
    resource_roles: Mapping[str, str]
    consumed_dry_run_values: tuple[bool, ...] = ()
    consumed_empty_config_fields: tuple[str, ...] = ()

    def copy_config(self, source_config: Mapping[str, Any]) -> dict[str, Any]:
        """Consume only reviewed equivalent config values, retaining all others.

        V2 preview/run operations own this flag. Never turn a saved preview
        default into a real write, or silently discard unknown business keys.
        Source and target snapshots remain in the migration audit.
        """
        result = deepcopy(dict(source_config))
        if "dry_run" in result and self.consumed_dry_run_values:
            value = result["dry_run"]
            if type(value) is not bool or value not in self.consumed_dry_run_values:
                raise PluginConflictError(
                    "saved dry_run mode is not compatible with target entrypoints",
                    code="PLUGIN_MIGRATION_CONFIG_MODE_UNSUPPORTED",
                )
            del result["dry_run"]
        for field in self.consumed_empty_config_fields:
            if result.get(field) == "":
                del result[field]
        return result


def _mapping(
    *,
    account_roles: Mapping[str, str],
    resource_roles: Mapping[str, str],
    consumed_dry_run_values: tuple[bool, ...] = (),
    consumed_empty_config_fields: tuple[str, ...] = (),
) -> MigrationBindingMapping:
    return MigrationBindingMapping(
        account_roles=MappingProxyType(dict(account_roles)),
        resource_roles=MappingProxyType(dict(resource_roles)),
        consumed_dry_run_values=consumed_dry_run_values,
        consumed_empty_config_fields=consumed_empty_config_fields,
    )


_REVIEWED_BINDING_MAPPINGS: Mapping[
    tuple[str, str, str],
    MigrationBindingMapping,
] = MappingProxyType(
    {
        ("clockin_daxiang", "clock_in_dual", "clockin_daxiang_v2"): _mapping(account_roles={"account_id":"operator"}, resource_roles={}),
        ("clockin_daxiang_s", "clock_in_dual", "clockin_daxiang_s_v2"): _mapping(account_roles={"account_id":"operator"}, resource_roles={}),
        ("arrive_list", "sync_arrive_list", "sync_arrive_list_v2"): _mapping(
            consumed_dry_run_values=(False,),
            account_roles={"account_id":"arrive_list_ronghui"}, resource_roles={"arrive_primary_sheet":"arrive_primary_sheet", "arrive_secondary_sheet":"arrive_secondary_sheet"}),
        ("site_send", "sync_site_send_list", "sync_site_send_list_v2"): _mapping(
            account_roles={"account_id":"site_send_ronghui"}, resource_roles={"site_send_bitable":"site_send_bitable", "site_send_sheet":"site_send_sheet"}),
        ("send_order", "sync_daily_send_orders", "sync_daily_send_orders_v2"): _mapping(
            consumed_dry_run_values=(False,),
            account_roles={"account_id":"daily_send_source"}, resource_roles={"send_order_bitable":"send_order_bitable"}),
        ("delivery_status", "sync_delivery_status", "sync_delivery_status_v2"): _mapping(
            consumed_dry_run_values=(False,),
            account_roles={"account_id":"delivery_source"}, resource_roles={"delivery_status_bitable":"delivery_status_bitable"}),
        ("daily_sign", "sync_daily_should_sign", "sync_daily_should_sign_v2"): _mapping(
            account_roles={"r13_account_id":"daily_sign_r13", "account_id":"daily_sign_tms"},
            resource_roles={"daily_sign_bitable":"daily_sign_bitable", "daily_sign_sheet":"daily_sign_sheet"}),
        ("customer_problems_shadow", "sync_customer_service_problems", "sync_customer_service_problems_v2"): _mapping(
            account_roles={"customer_service_source":"customer_service_source"}, resource_roles={}),
        ("yunda_dispatch_forecast", "sync_yunda_dispatch_forecast", "sync_yunda_dispatch_forecast_v2"): _mapping(
            consumed_dry_run_values=(False,),
            account_roles={"account_id":"yunda_dispatch_source"}, resource_roles={"dispatch_forecast_bitable":"dispatch_forecast_bitable"}),
        ("yunda_send_waybills", "sync_yunda_send_waybills", "sync_yunda_send_waybills_v2"): _mapping(
            consumed_dry_run_values=(False,),
            account_roles={"account_id":"yunda_send_source"}, resource_roles={"send_waybills_bitable":"send_waybills_bitable", "send_waybills_sheet":"send_waybills_sheet"}),
        **{(instance, "sync_finance_bills", "sync_finance_bills_v2"): _mapping(
            account_roles={role:role for role in ("finance_quote_source", "finance_daxiang_s_source", "finance_self_pickup_source")}, resource_roles={})
            for instance in ("finance_bills", "finance_startup_catchup")},
        (
            "arrival_stats",
            "sync_arrival_stats",
            "sync_arrival_stats_v2",
        ): _mapping(
            account_roles={"account_id": "arrival_stats_tms"},
            resource_roles={
                "arrival_stats_primary_sheet": "arrival_stats_primary_sheet",
                "arrival_stats_secondary_sheet": "arrival_stats_secondary_sheet",
                "arrival_stats_pending_sheet": "arrival_stats_pending_sheet",
                "arrival_stats_archive_sheet": "arrival_stats_archive_sheet",
                "arrival_stats_split_pending_sheet": (
                    "arrival_stats_split_pending_sheet"
                ),
            },
        ),
        (
            "self_pickup_problem_upload",
            "self_pickup_problem_upload",
            "self_pickup_problem_upload_v2",
        ): _mapping(
            account_roles={
                "account_id": "self_pickup_primary",
                "daxiang_s_account_id": "self_pickup_daxiang_s",
            },
            resource_roles={
                "self_pickup_source_sheet": "self_pickup_source_sheet",
            },
        ),
        (
            "split_pending_problem_upload",
            "split_pending_problem_upload",
            "split_pending_problem_upload_v2",
        ): _mapping(
            account_roles={"account_id": "split_pending_ronghui"},
            resource_roles={
                "split_pending_source_sheet": "split_pending_source_sheet",
                "split_pending_target_sheet": "split_pending_target_sheet",
            },
        ),
        (
            "scan_codes",
            "sync_scan_codes",
            "sync_scan_codes_v2",
        ): _mapping(
            # Both old Console defaults become the same explicit preview;
            # formal scanning still requires the newly generated confirmation.
            consumed_dry_run_values=(False, True),
            # Both payloads resolve an omitted date on each invocation. Do not
            # freeze today's date into settings or discard invalid date values.
            consumed_empty_config_fields=("target_date",),
            account_roles={"account_id": "scan_ronghui"},
            resource_roles={},
        ),
    }
)


def reviewed_migration_binding_mapping(
    *,
    source_automation_id: str,
    source_plugin_id: str,
    target_plugin_id: str,
) -> MigrationBindingMapping | None:
    """Return an immutable reviewed map; unknown pairs never infer by shape."""

    return _REVIEWED_BINDING_MAPPINGS.get(
        (
            str(source_automation_id or "").strip(),
            str(source_plugin_id or "").strip(),
            str(target_plugin_id or "").strip(),
        )
    )


__all__ = [
    "MigrationBindingMapping",
    "reviewed_migration_binding_mapping",
]
