"""Read-only preflight for migration 018 required-existing resources.

The companion code-owned identity set is exported for static migration checks;
those rows have exact reviewed defaults and therefore are not queried here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, NamedTuple


class RequiredExistingResourceSpec(NamedTuple):
    resource_key: str
    expected_kind: str
    required_fields: tuple[str, ...]
    alternative_field_groups: tuple[tuple[str, ...], ...] = ()


# These are the eight installation-specific rows that migration 018 deliberately
# refuses to guess or materialize. A static migration test keeps this closed
# diagnostic contract aligned with the SQL guard.
AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS = (
    RequiredExistingResourceSpec(
        "phase7.site_send_bitable",
        "feishu_bitable",
        ("base_token", "table_id"),
    ),
    RequiredExistingResourceSpec(
        "phase7.site_send_sheet",
        "feishu_sheet",
        ("spreadsheet_token", "range"),
    ),
    RequiredExistingResourceSpec(
        "phase7.send_order_bitable",
        "feishu_bitable",
        ("base_token", "table_id"),
    ),
    RequiredExistingResourceSpec(
        "phase7.arrive_primary_sheet",
        "feishu_sheet",
        ("spreadsheet_token", "range", "clear_range"),
    ),
    RequiredExistingResourceSpec(
        "phase7.arrive_secondary_sheet",
        "feishu_sheet",
        ("spreadsheet_token", "range", "clear_range"),
    ),
    RequiredExistingResourceSpec(
        "phase7.stats_archive_sheet",
        "feishu_sheet",
        ("spreadsheet_token",),
        (("default_write_range", "source_snapshot_range"),),
    ),
    RequiredExistingResourceSpec(
        "phase7.daily_sign_bitable",
        "feishu_bitable",
        ("base_token", "table_id"),
    ),
    RequiredExistingResourceSpec(
        "phase7.daily_sign_sheet",
        "feishu_sheet",
        ("spreadsheet_token", "range"),
    ),
)

# Exact migration-owned rows materialized from the reviewed built-in resource
# catalog. The two deferred R7 route resources are intentionally absent.
AUTOMATION_PROJECT_CODE_OWNED_RESOURCE_KEYS = frozenset(
    {
        "phase7.delivery_status_bitable",
        "phase7.yunda_dispatch_forecast_bitable",
        "phase7.yunda_send_waybills_bitable",
        "phase7.yunda_send_waybills_sheet",
        "phase7.delivery_status_webhook",
        "phase7.scan_webhook",
        "phase7.stats_webhook",
        "automation.feishu_route.arrive_list",
        "automation.feishu_route.send_order",
        "automation.feishu_route.yunda_dispatch_forecast",
        "automation.feishu_route.yunda_send_waybills",
        "automation.feishu_route.scan_codes",
        "automation.feishu_route.arrival_stats",
        "automation.feishu_route.self_pickup_problem_upload",
        "automation.feishu_route.split_pending_problem_upload",
        "phase7.self_pickup_source_sheet",
        "phase7.split_pending_source_sheet",
        "phase7.split_pending_target_sheet",
    }
)

AUTOMATION_PROJECT_REQUIRED_RESOURCE_FIELD_NAMES = (
    "resource_kind",
    "base_token",
    "table_id",
    "spreadsheet_token",
    "range",
    "clear_range",
    "default_write_range",
    "source_snapshot_range",
)


def _required_resource_projection_sql() -> str:
    projections = ["resource_key"]
    for field_name in AUTOMATION_PROJECT_REQUIRED_RESOURCE_FIELD_NAMES:
        json_path = f"$.{field_name}"
        projections.extend(
            (
                f"JSON_CONTAINS_PATH(config_json, 'one', '{json_path}') "
                f"AS {field_name}_present",
                f"JSON_TYPE(JSON_EXTRACT(config_json, '{json_path}')) "
                f"AS {field_name}_type",
                "COALESCE("
                f"JSON_TYPE(JSON_EXTRACT(config_json, '{json_path}')) = "
                "'STRING' AND "
                f"TRIM(JSON_UNQUOTE(JSON_EXTRACT(config_json, '{json_path}'))) "
                f"<> '', FALSE) AS {field_name}_nonempty",
            )
        )
    projections.append(
        "COALESCE("
        "JSON_TYPE(JSON_EXTRACT(config_json, '$.resource_kind')) = 'STRING' "
        "AND BINARY JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.resource_kind')) "
        "= BINARY %s, FALSE) AS resource_kind_matches"
    )
    return (
        "SELECT\n    "
        + ",\n    ".join(projections)
        + "\nFROM workflow_resources "
        "WHERE BINARY resource_key = BINARY %s"
    )


_AUTOMATION_PROJECT_REQUIRED_RESOURCE_PROJECTION_SQL = (
    _required_resource_projection_sql()
)


def _database_flag(value: object) -> bool:
    return value is True or value == 1


def _required_string_field_problem(
    row: Mapping[str, object],
    field_name: str,
) -> str | None:
    if not _database_flag(row.get(f"{field_name}_present")):
        return "MISSING_FIELD"
    if str(row.get(f"{field_name}_type") or "").upper() != "STRING":
        return "INVALID_FIELD_TYPE"
    if not _database_flag(row.get(f"{field_name}_nonempty")):
        return "EMPTY_FIELD"
    return None


def _alternative_field_group_problem(
    row: Mapping[str, object],
    field_names: tuple[str, ...],
) -> str | None:
    problems = tuple(
        _required_string_field_problem(row, field_name)
        for field_name in field_names
    )
    if any(problem is None for problem in problems):
        return None
    if all(problem == "MISSING_FIELD" for problem in problems):
        return "MISSING_FIELD"
    if "INVALID_FIELD_TYPE" in problems:
        return "INVALID_FIELD_TYPE"
    return "EMPTY_FIELD"


def _required_resource_findings(cursor: Any) -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    for spec in AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS:
        cursor.execute(
            _AUTOMATION_PROJECT_REQUIRED_RESOURCE_PROJECTION_SQL,
            (spec.expected_kind, spec.resource_key),
        )
        row = cursor.fetchone()
        if not isinstance(row, Mapping):
            findings.append((spec.resource_key, "MISSING_ROW", "resource_key"))
            continue

        if _database_flag(row.get("resource_kind_present")):
            kind_problem = _required_string_field_problem(row, "resource_kind")
            if kind_problem is not None:
                findings.append((spec.resource_key, kind_problem, "resource_kind"))
            elif not _database_flag(row.get("resource_kind_matches")):
                findings.append(
                    (spec.resource_key, "INVALID_KIND", "resource_kind")
                )

        for field_name in spec.required_fields:
            problem = _required_string_field_problem(row, field_name)
            if problem is not None:
                findings.append((spec.resource_key, problem, field_name))

        for field_names in spec.alternative_field_groups:
            problem = _alternative_field_group_problem(row, field_names)
            if problem is not None:
                findings.append(
                    (
                        spec.resource_key,
                        problem,
                        "_or_".join(field_names),
                    )
                )
    return findings


def check_automation_project_required_resources(
    connect: Callable[[], Any],
) -> int:
    """Validate the eight pre-018 resource shapes without exposing config."""

    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            findings = _required_resource_findings(cursor)
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()

    expected_count = len(AUTOMATION_PROJECT_REQUIRED_EXISTING_RESOURCE_SPECS)
    if not findings:
        print(f"automation_project_required_resources=ok count={expected_count}")
        return 0

    print(f"automation_project_required_resources=blocked count={len(findings)}")
    for resource_key, reason, field_name in findings:
        print(
            "automation_project_required_resource="
            f"{resource_key} reason={reason} field={field_name}"
        )
    return 1
