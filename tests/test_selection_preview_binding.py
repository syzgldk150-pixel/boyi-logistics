from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


from agent.orchestration.selection_preview_binding import SelectionPreviewExpectation, is_selection_preview_project
from shared.automation_project_authorization import (
    canonical_sha256,
)
from shared.orchestration_repository_support import IdempotencyConflict


PROJECT_ID = "self_pickup_problem_upload"
RUN_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def get(self, identity, *, for_update=False):
        del for_update
        value = self.rows.get(identity)
        return copy.deepcopy(value) if value is not None else None

    def get_by_idempotency(self, source, idempotency_key, *, for_update=False):
        del for_update
        for value in self.rows.values():
            if (
                value.get("source") == source
                and value.get("idempotency_key") == idempotency_key
            ):
                return copy.deepcopy(value)
        return None


class _Steps:
    def __init__(self, rows):
        self.rows = rows

    def list_for_run(self, run_id):
        return copy.deepcopy(self.rows.get(run_id, []))


class _Uow:
    def __init__(self, fixture):
        self.runs = _Rows(fixture["runs"])
        self.commands = _Rows(fixture["commands"])
        self.steps = _Steps(fixture["steps"])
        self.events = _Events()


class _Events:
    def __init__(self):
        self.rows = []

    def append_with_outbox(self, event, outbox):
        del outbox
        if any(
            row["event_type"] == event["event_type"]
            and row["source_event_id"] == event["source_event_id"]
            for row in self.rows
        ):
            raise IdempotencyConflict("selection preview already consumed")
        self.rows.append(copy.deepcopy(event))


def _expectation() -> SelectionPreviewExpectation:
    return SelectionPreviewExpectation(
        project_instance_id=PROJECT_ID,
        plugin_id=PROJECT_ID,
        generation=4,
        contract_digest="c" * 64,
        configuration_version=7,
    )


def _fixture(*, observed_at=NOW - timedelta(minutes=2)):
    candidates = [
        {
            "arrival_count": 2,
            "bill_code": "R0001",
            "delivery_method": "自提",
            "destination_site": "邵阳大祥S站",
            "goods_count": 2,
            "row_number": 12,
            "source_id": "source-one",
            "source_name": "每日到货表",
        },
        {
            "arrival_count": 1,
            "bill_code": "R0002",
            "delivery_method": "自提",
            "destination_site": "邵阳自提部",
            "goods_count": 1,
            "row_number": 18,
            "source_id": "source-one",
            "source_name": "每日到货表",
        },
    ]
    result = {
        "status": "SUCCESS",
        "data": {
            "dry_run": True,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "preview_fingerprint": "f" * 64,
            "duplicate_source_rows": 0,
        },
        "meta": {"observed_at": observed_at.isoformat()},
        "warnings": [],
        "error": None,
    }
    command_id = "preview-command"
    return {
        "runs": {
            RUN_ID: {
                "run_id": RUN_ID,
                "command_id": command_id,
                "status": "COMPLETED",
            }
        },
        "commands": {
            command_id: {
                "command_type": "automation.project.invoke",
                "parameters_json": {
                    "tool_name": f"automation.{PROJECT_ID}.run",
                    "arguments": {
                        "dry_run": True,
                        "selected_bill_codes": [],
                        "preview_fingerprint": "",
                    },
                },
                "automation_invocation_json": {
                    "automation_id": PROJECT_ID,
                    "automation_generation": 4,
                    "contract_hash": "c" * 64,
                    "project_configuration_version": 7,
                },
            }
        },
        "steps": {
            RUN_ID: [
                {
                    "step_id": "preview-step",
                    "status": "COMPLETED",
                    "postcondition_status": "VERIFIED",
                    "tool_name": f"automation.{PROJECT_ID}.run",
                    "result_summary_json": result,
                    "result_sha256": canonical_sha256(result),
                }
            ]
        },
    }


def _service_v2_expectation(
    *,
    entrypoint: str = "console",
    contribution_id: str = "execute_console",
) -> SelectionPreviewExpectation:
    return SelectionPreviewExpectation(
        project_instance_id="self_pickup_problem_upload_v2",
        plugin_id="self_pickup_problem_upload_v2",
        generation=4,
        contract_digest="c" * 64,
        configuration_version=7,
        runtime_model="SERVICE_V2",
        entrypoint=entrypoint,
        contribution_id=contribution_id,
        title="预览自提问题件候选",
    )


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


def _service_v2_fixture():
    fixture = _fixture()
    command = fixture["commands"]["preview-command"]
    command["parameters_json"]["tool_name"] = (
        "automation.self_pickup_problem_upload_v2.run"
    )
    command["parameters_json"]["execution_context"] = {
        "selection_phase": "PREVIEW"
    }
    command["source"] = "console"
    command["automation_invocation_json"]["automation_id"] = (
        "self_pickup_problem_upload_v2"
    )
    command["automation_invocation_json"]["contract_id"] = "execute_console"
    command["automation_invocation_json"]["entrypoint"] = "console"
    fixture["steps"][RUN_ID][0]["tool_name"] = (
        "automation.self_pickup_problem_upload_v2.run"
    )
    return fixture


def _restore_kwargs(actor, command, trusted_context):
    return {
        "source": "console",
        "idempotency_key": command.idempotency_key,
        "actor": actor,
        "trusted_context": trusted_context,
        "project_instance_id": "self_pickup_problem_upload_v2",
        "request_id": "selection-request-1",
        "preview_run_id": RUN_ID,
        "selected_bill_codes": ["R0001"],
        "expected_entrypoint": "console",
        "expected_contribution_id": "execute_console",
        "expected_generation": 4,
        "expected_configuration_version": 7,
    }

