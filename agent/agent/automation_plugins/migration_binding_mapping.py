"""Code-owned one-to-one binding maps for reviewed Action-v1 migrations."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class MigrationBindingMapping:
    """Exact source-role to target-role maps for one reviewed plugin pair."""

    account_roles: Mapping[str, str]
    resource_roles: Mapping[str, str]


def _mapping(
    *,
    account_roles: Mapping[str, str],
    resource_roles: Mapping[str, str],
) -> MigrationBindingMapping:
    return MigrationBindingMapping(
        account_roles=MappingProxyType(dict(account_roles)),
        resource_roles=MappingProxyType(dict(resource_roles)),
    )


_REVIEWED_BINDING_MAPPINGS: Mapping[
    tuple[str, str, str],
    MigrationBindingMapping,
] = MappingProxyType(
    {
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
                "account_id": "account_id",
                "daxiang_s_account_id": "daxiang_s_account_id",
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
            account_roles={"account_id": "account_id"},
            resource_roles={
                "split_pending_source_sheet": "split_pending_source_sheet",
                "split_pending_target_sheet": "split_pending_target_sheet",
            },
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
