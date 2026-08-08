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
        "base_token": "Fcm8b2H7wayK1UsYLjlcFmWhnMh",
        "table_id": "tblX96gGAuBfJrtW",
        "view_name": "未签收明细",
        "view_id": "veweDmbdIS",
    },
    "phase7.delivery_status_webhook": {"path": "webhook/sign-status"},
    "phase7.scan_webhook": {"path": "webhook/phase7/scan"},
    "phase7.stats_webhook": {"path": "webhook/phase7/stats"},
    "phase7.split_pending_source_sheet": {
        "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
        "sheet_id": "8fc516",
        "range": "8fc516!A1:S5000",
    },
    "phase7.split_pending_target_sheet": {
        "spreadsheet_token": "F0NVsI5dlhaWugtw14YcmdrQnvh",
        "sheet_id": "bNhh7u",
        "range": "bNhh7u!A1:S1",
        "clear_range": "bNhh7u!A2:S5000",
    },
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
