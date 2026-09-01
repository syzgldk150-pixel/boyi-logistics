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
            "range": "live-sheet-id!A1:S5000",
        },
        source="reviewed-metadata-sync",
    )
