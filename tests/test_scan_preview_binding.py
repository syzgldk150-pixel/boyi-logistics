from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.orchestration.models import (
    OrchestrationError,
)
from agent.orchestration.scan_preview_binding import require_scan_formal_governance


PROJECT_ID = "scan_codes"
RUN_ID = "11111111-1111-4111-8111-111111111111"
CONTRACT_DIGEST = "c" * 64
NOW = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)
FORMAL_ARGUMENTS = {
    "target_date": "2026-08-24",
    "batch_size": 1,
    "max_batches": 2,
}


def test_formal_path_requires_the_v2_scan_plugin_preview_contribution():
    preview = {"id": "preview_scan", "selection_preview_operation": "preview"}
    entry = SimpleNamespace(
        automation_id="87911cef-3773-4273-b644-030108c43678",
        plugin_id="sync_scan_codes_v2",
        runtime_model="SERVICE_V2",
        trust_source="super_admin_upload",
        contributions={"console": [preview]},
    )
    require_scan_formal_governance(entry)

    for drift in (
        {"contributions": {"console": [{"id": "run_now"}]}},
        {"contributions": {"console": [preview, dict(preview, id="second")]}},
        {"plugin_id": "sync_scan_codes"},
        {"runtime_model": "ACTION_V1"},
        {"trust_source": "ed25519_first_party"},
    ):
        with pytest.raises(OrchestrationError) as disabled:
            require_scan_formal_governance(SimpleNamespace(**{**vars(entry), **drift}))
        assert disabled.value.code == "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED"
