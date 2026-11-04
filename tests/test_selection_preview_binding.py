from __future__ import annotations

from types import SimpleNamespace

from agent.orchestration.selection_preview_binding import is_selection_preview_project


def test_service_v2_feishu_only_selection_is_still_host_governed() -> None:
    entry = SimpleNamespace(
        automation_id="selection-v2",
        plugin_id="selection_v2",
        runtime_model="SERVICE_V2",
        contributions={
            "console": (),
            "feishu": (
                {
                    "id": "execute_feishu",
                    "service": "plugin.selection_v2.runner@1",
                    "operation": "execute",
                    "selection_preview_operation": "preview",
                },
            ),
        },
    )

    assert is_selection_preview_project(entry) is True
