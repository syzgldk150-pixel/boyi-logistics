from __future__ import annotations

from unittest.mock import patch

from agent import workflow_resource_store


class _Repository:
    @staticmethod
    def list_records(*, include_config: bool):
        assert include_config is True
        return [
            {
                "resource_key": "phase7.input_sheet",
                "source": "phase7_resource_import",
                "configuration_version": 3,
                "config_sha256": "a" * 64,
                "config": {
                    "resource_kind": "feishu_sheet",
                    "display_name": "输入表格",
                    "spreadsheet_token": "must-not-cross-boundary",
                    "read_range": "must-not-cross-boundary",
                },
            },
            {
                "resource_key": "phase7.invalid",
                "source": "phase7_resource_import",
                "configuration_version": 1,
                "config_sha256": "b" * 64,
                "config": {
                    "resource_kind": "",
                    "token": "must-not-cross-boundary",
                },
            },
        ]


def test_managed_resource_projection_is_closed_and_credential_free() -> None:
    with patch.object(workflow_resource_store, "_repository", return_value=_Repository()):
        resources = workflow_resource_store.list_workflow_resource_descriptors()

    assert resources == [
        {
            "resource_id": "phase7.input_sheet",
            "name": "输入表格",
            "kind": "feishu_sheet",
            "status": "available",
        }
    ]
    serialized = repr(resources)
    assert "spreadsheet_token" not in serialized
    assert "read_range" not in serialized
    assert "must-not-cross-boundary" not in serialized
    assert "config_sha256" not in serialized
    assert "configuration_version" not in serialized
