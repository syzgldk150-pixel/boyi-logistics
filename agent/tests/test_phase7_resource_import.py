from __future__ import annotations

from unittest.mock import call, patch

from agent import phase7_resource_import


YUNDA_MANAGED_RESOURCES = {
    "phase7.yunda_dispatch_forecast_bitable": {
        "resource_kind": "feishu_bitable",
        "base_token": "Et8sboZiSahfhYsa0i3c6hkwnXg",
        "table_id": "tblT43ay2KjeXdC0",
    },
    "phase7.yunda_send_waybills_bitable": {
        "resource_kind": "feishu_bitable",
        "base_token": "Fcm8b2H7wayK1UsYLjlcFmWhnMh",
        "table_id": "tblNHfIVVeaTBB7Y",
    },
    "phase7.yunda_send_waybills_sheet": {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "GILYss6KhhBBuRt9FPWcXbben7c",
        "sheet_id": "Sheet1",
        "sheet_range": "Sheet1!A2:A2",
        "clear_range": "Sheet1!A2:Y5000",
    },
}

PROBLEM_MANAGED_RESOURCE_KEYS = {
    "phase7.self_pickup_source_sheet",
    "phase7.split_pending_source_sheet",
    "phase7.split_pending_target_sheet",
}


def test_yunda_managed_resources_are_explicit_reviewed_rows() -> None:
    assert {
        key: phase7_resource_import.BUILTIN_RESOURCES[key]
        for key in YUNDA_MANAGED_RESOURCES
    } == YUNDA_MANAGED_RESOURCES


def test_phase7_import_persists_every_yunda_resource_without_key_inference() -> None:
    with (
        patch.object(phase7_resource_import, "_load_local_resource_file", return_value={}),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        imported = phase7_resource_import.import_phase7_resources()

    assert set(YUNDA_MANAGED_RESOURCES) <= set(imported)
    for resource_key, config in YUNDA_MANAGED_RESOURCES.items():
        assert call(resource_key, config, source="local-config-import") in upsert.call_args_list


def test_problem_sheet_resources_are_explicit_managed_rows() -> None:
    resources = {
        key: phase7_resource_import.BUILTIN_RESOURCES[key]
        for key in PROBLEM_MANAGED_RESOURCE_KEYS
    }
    assert set(resources) == PROBLEM_MANAGED_RESOURCE_KEYS
    assert {config["resource_kind"] for config in resources.values()} == {
        "feishu_sheet"
    }
    assert len({config["spreadsheet_token"] for config in resources.values()}) == 1
    assert len({config["sheet_id"] for config in resources.values()}) == 3
    for config in resources.values():
        assert config["range"].startswith(f"{config['sheet_id']}!A1:S")
    assert resources["phase7.split_pending_target_sheet"]["clear_range"].startswith(
        f"{resources['phase7.split_pending_target_sheet']['sheet_id']}!A2:S"
    )
    assert (
        resources["phase7.split_pending_source_sheet"]["sheet_title"]
        == "每日到货表"
    )
    assert resources["phase7.split_pending_source_sheet"]["sheet_header_constraints"] == {
        "A": ["运单编号", "单号"],
        "E": ["件数"],
        "S": ["累计到货件数", "已到货件数", "到货件数"],
    }
    assert resources["phase7.self_pickup_source_sheet"]["sheet_title"] == "每日到货表"
    assert (
        resources["phase7.self_pickup_source_sheet"]["sheet_header_constraints"]
        == resources["phase7.split_pending_source_sheet"]["sheet_header_constraints"]
    )
    assert (
        resources["phase7.self_pickup_source_sheet"]["spreadsheet_token"],
        resources["phase7.self_pickup_source_sheet"]["sheet_id"],
        resources["phase7.self_pickup_source_sheet"]["range"],
        resources["phase7.self_pickup_source_sheet"]["formula_source_sheet_id"],
        resources["phase7.self_pickup_source_sheet"]["formula_source_range"],
    ) == (
        resources["phase7.split_pending_source_sheet"]["spreadsheet_token"],
        "UeBd3I",
        "UeBd3I!A1:S5000",
        "8fc516",
        "8fc516!A1:S197",
    )
    assert (
        resources["phase7.self_pickup_source_sheet"]["sheet_id"]
        != resources["phase7.split_pending_source_sheet"]["sheet_id"]
    )


def test_phase7_import_persists_problem_resources_without_key_inference() -> None:
    with (
        patch.object(phase7_resource_import, "_load_local_resource_file", return_value={}),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        imported = phase7_resource_import.import_phase7_resources()

    assert PROBLEM_MANAGED_RESOURCE_KEYS <= set(imported)
    for resource_key in PROBLEM_MANAGED_RESOURCE_KEYS:
        assert call(
            resource_key,
            phase7_resource_import.BUILTIN_RESOURCES[resource_key],
            source="local-config-import",
        ) in upsert.call_args_list


def test_missing_fixed_route_repair_creates_only_absent_reviewed_routes() -> None:
    missing_key = "automation.feishu_route.self_pickup_problem_upload"

    def load_resource(key: str):
        if key == missing_key:
            return None
        return {"resource_kind": "existing"}

    with (
        patch.object(
            phase7_resource_import,
            "get_workflow_resource",
            side_effect=load_resource,
        ),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        repaired = phase7_resource_import.repair_missing_fixed_automation_routes()

    assert repaired == [missing_key]
    upsert.assert_called_once_with(
        missing_key,
        phase7_resource_import.BUILTIN_RESOURCES[missing_key],
        source="reviewed-route-repair",
    )


def test_missing_fixed_route_repair_never_rewrites_existing_route() -> None:
    with (
        patch.object(
            phase7_resource_import,
            "get_workflow_resource",
            return_value={"resource_kind": "feishu_route", "route_key": "live"},
        ),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        repaired = phase7_resource_import.repair_missing_fixed_automation_routes()

    assert repaired == []
    upsert.assert_not_called()


def test_reviewed_metadata_sync_preserves_live_sheet_locator() -> None:
    current = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "live-document-token",
        "sheet_id": "live-sheet-id",
        "range": "live-sheet-id!A1:S5000",
        "_meta": {"configuration_version": 8},
    }
    with (
        patch.object(
            phase7_resource_import,
            "get_workflow_resource",
            side_effect=lambda key: (
                current
                if key == "phase7.split_pending_source_sheet"
                else None
                if key in {
                    "phase7.stats_archive_sheet",
                    "phase7.self_pickup_source_sheet",
                }
                else phase7_resource_import.BUILTIN_RESOURCES.get(key)
            ),
        ),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        updated = phase7_resource_import.sync_reviewed_phase7_resource_metadata()

    assert updated == ["phase7.split_pending_source_sheet"]
    upsert.assert_called_once_with(
        "phase7.split_pending_source_sheet",
        {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "live-document-token",
            "sheet_id": "live-sheet-id",
            "sheet_title": "每日到货表",
            "business_purpose": "分批及有发未到问题件来源",
            "sheet_header_constraints": {
                "A": ["运单编号", "单号"],
                "E": ["件数"],
                "S": ["累计到货件数", "已到货件数", "到货件数"],
            },
            "range": "live-sheet-id!A1:S5000",
        },
        source="reviewed-metadata-sync",
    )


def test_reviewed_metadata_sync_repoints_self_pickup_to_daxiang_arrival_source() -> None:
    stale_self_pickup_source = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
        "sheet_id": "8fc516",
        "sheet_title": "每日到货表",
        "business_purpose": "自提到货问题件来源",
        "sheet_header_constraints": {
            "A": ["运单编号", "单号"],
            "E": ["件数"],
            "S": ["累计到货件数", "已到货件数", "到货件数"],
        },
        "range": "8fc516!A1:S5000",
        "_meta": {"configuration_version": 4},
    }

    def load_resource(key: str):
        if key == "phase7.self_pickup_source_sheet":
            return stale_self_pickup_source
        return None

    with (
        patch.object(
            phase7_resource_import,
            "get_workflow_resource",
            side_effect=load_resource,
        ),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        updated = phase7_resource_import.sync_reviewed_phase7_resource_metadata()

    assert updated == ["phase7.self_pickup_source_sheet"]
    upsert.assert_called_once_with(
        "phase7.self_pickup_source_sheet",
        {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
            "sheet_id": "UeBd3I",
            "sheet_title": "每日到货表",
            "formula_source_sheet_id": "8fc516",
            "formula_source_range": "8fc516!A1:S197",
            "business_purpose": "自提到货问题件来源",
            "sheet_header_constraints": {
                "A": ["运单编号", "单号"],
                "E": ["件数"],
                "S": ["累计到货件数", "已到货件数", "到货件数"],
            },
            "range": "UeBd3I!A1:S5000",
        },
        source="reviewed-self-pickup-source-sync",
    )


def test_reviewed_metadata_sync_marks_archive_as_document_scoped() -> None:
    current = {
        "resource_kind": "feishu_sheet",
        "spreadsheet_token": "live-archive-document-token",
        "default_write_range": "A1:S199",
        "_meta": {"configuration_version": 3},
    }
    with (
        patch.object(
            phase7_resource_import,
            "get_workflow_resource",
            side_effect=lambda key: (
                current if key == "phase7.stats_archive_sheet" else None
            ),
        ),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        updated = phase7_resource_import.sync_reviewed_phase7_resource_metadata()

    assert updated == ["phase7.stats_archive_sheet"]
    upsert.assert_called_once_with(
        "phase7.stats_archive_sheet",
        {
            "resource_kind": "feishu_sheet",
            "spreadsheet_token": "live-archive-document-token",
            "default_write_range": "A1:S199",
            "resource_scope": "spreadsheet",
            "business_purpose": "到货统计归档",
        },
        source="reviewed-metadata-sync",
    )


def test_reviewed_metadata_sync_adds_distinct_bitable_business_purposes() -> None:
    send_order = {
        "resource_kind": "feishu_bitable",
        "base_token": "live-base-token",
        "table_id": "live-table-id",
        "_meta": {"configuration_version": 5},
    }

    def load_resource(key: str):
        if key == "phase7.send_order_bitable":
            return send_order
        return None

    with (
        patch.object(
            phase7_resource_import,
            "get_workflow_resource",
            side_effect=load_resource,
        ),
        patch.object(phase7_resource_import, "upsert_workflow_resource") as upsert,
    ):
        updated = phase7_resource_import.sync_reviewed_phase7_resource_metadata()

    assert updated == ["phase7.send_order_bitable"]
    upsert.assert_called_once_with(
        "phase7.send_order_bitable",
        {
            "resource_kind": "feishu_bitable",
            "base_token": "live-base-token",
            "table_id": "live-table-id",
            "business_purpose": "当日寄件数据",
        },
        source="reviewed-metadata-sync",
    )
