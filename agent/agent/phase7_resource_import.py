"""Import built-in and file-based Phase 7 workflow resources into MySQL."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent.workflow_resource_store import upsert_workflow_resource


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_RESOURCE_PATH = PROJECT_ROOT / "deploy" / "phase7_resources.json"

BUILTIN_RESOURCES: dict[str, dict] = {
    "phase7.delivery_status_bitable": {
        "resource_kind": "feishu_bitable",
        "base_token": "Fcm8b2H7wayK1UsYLjlcFmWhnMh",
        "table_id": "tblX96gGAuBfJrtW",
        "view_name": "未签收明细",
        "view_id": "veweDmbdIS",
    },
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
    "phase7.delivery_status_webhook": {
        "resource_kind": "webhook_route",
        "path": "webhook/sign-status",
    },
    "phase7.scan_webhook": {
        "resource_kind": "webhook_route",
        "path": "webhook/phase7/scan",
    },
    "phase7.stats_webhook": {
        "resource_kind": "webhook_route",
        "path": "webhook/phase7/stats",
    },
    "automation.feishu_route.r7_departure_checkin": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.r7_departure_checkin",
    },
    "automation.feishu_route.r7_arrival_checkin": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.r7_arrival_checkin",
    },
    "automation.feishu_route.arrive_list": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.arrive_list",
    },
    "automation.feishu_route.send_order": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.send_order",
    },
    "automation.feishu_route.yunda_dispatch_forecast": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.yunda_dispatch_forecast",
    },
    "automation.feishu_route.yunda_send_waybills": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.yunda_send_waybills",
    },
    "automation.feishu_route.scan_codes": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.scan_codes",
    },
    "automation.feishu_route.arrival_stats": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.arrival_stats",
    },
    "automation.feishu_route.self_pickup_problem_upload": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.self_pickup_problem_upload",
    },
    "automation.feishu_route.split_pending_problem_upload": {
        "resource_kind": "feishu_route",
        "route_key": "builtin.split_pending_problem_upload",
    },
    "phase7.split_pending_source_sheet": {
        "display_name": "每日到货表",
        "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
        "sheet_id": "8fc516",
        "range": "8fc516!A1:S5000",
    },
    "phase7.split_pending_target_sheet": {
        "display_name": "分批及有发未到表",
        "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
        "sheet_id": "bNhh7u",
        "range": "bNhh7u!A1:S1",
        "clear_range": "bNhh7u!A2:S5000",
    },
}

# Problem-action packages bind resource identities, never document locators.
# Reuse the already reviewed daily-arrival document locator for its distinct
# self-pickup worksheet and normalize both split worksheets to the managed
# ``feishu_sheet`` kind expected by the signed manifests.
for _problem_sheet_key in (
    "phase7.split_pending_source_sheet",
    "phase7.split_pending_target_sheet",
):
    BUILTIN_RESOURCES[_problem_sheet_key]["resource_kind"] = "feishu_sheet"
BUILTIN_RESOURCES["phase7.self_pickup_source_sheet"] = {
    "resource_kind": "feishu_sheet",
    "spreadsheet_token": BUILTIN_RESOURCES["phase7.split_pending_source_sheet"][
        "spreadsheet_token"
    ],
    "sheet_id": "UeBd3I",
    "range": "UeBd3I!A1:S5000",
}


def _load_local_resource_file() -> dict[str, dict]:
    configured_path = str(os.getenv("PHASE7_RESOURCE_IMPORT_PATH") or "").strip()
    target = Path(configured_path) if configured_path else LOCAL_RESOURCE_PATH
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Phase 7 resource file must be a JSON object: {target}")
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def import_phase7_resources() -> list[str]:
    imported: list[str] = []
    resource_map = dict(BUILTIN_RESOURCES)
    resource_map.update(_load_local_resource_file())
    for resource_key, config in resource_map.items():
        upsert_workflow_resource(resource_key, config, source="local-config-import")
        imported.append(resource_key)
    return imported
