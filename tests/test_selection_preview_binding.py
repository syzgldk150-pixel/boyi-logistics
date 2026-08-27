from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from agent.orchestration.models import OrchestrationError
from agent.orchestration.selection_preview_binding import (
    SelectionPreviewExpectation,
    selection_confirmation_arguments,
    selection_preview_public_projection,
)
from shared.automation_project_authorization import canonical_sha256


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
                    "status": "COMPLETED",
                    "postcondition_status": "VERIFIED",
                    "tool_name": f"automation.{PROJECT_ID}.run",
                    "result_summary_json": result,
                    "result_sha256": canonical_sha256(result),
                }
            ]
        },
    }


def test_public_projection_exposes_candidates_but_not_server_fingerprint():
    projection = selection_preview_public_projection(
        _Uow(_fixture()),
        preview_run_id=RUN_ID,
        expectation=_expectation(),
        now=NOW,
    )

    assert projection["candidate_count"] == 2
    assert [item["bill_code"] for item in projection["candidates"]] == [
        "R0001",
        "R0002",
    ]
    assert projection["can_confirm"] is True
    assert "preview_fingerprint" not in projection


def test_confirmation_uses_persisted_fingerprint_and_exact_selected_subset():
    arguments = selection_confirmation_arguments(
        _Uow(_fixture()),
        preview_run_id=RUN_ID,
        expectation=_expectation(),
        selected_bill_codes=["R0002"],
        now=NOW,
    )

    assert arguments == {
        "dry_run": False,
        "selected_bill_codes": ["R0002"],
        "preview_fingerprint": "f" * 64,
    }


def test_confirmation_blocks_expired_or_unavailable_selection():
    with pytest.raises(OrchestrationError, match="超过十五分钟") as expired:
        selection_confirmation_arguments(
            _Uow(_fixture(observed_at=NOW - timedelta(minutes=16))),
            preview_run_id=RUN_ID,
            expectation=_expectation(),
            selected_bill_codes=["R0001"],
            now=NOW,
        )
    assert expired.value.code == "SELECTION_PREVIEW_EXPIRED"

    with pytest.raises(OrchestrationError, match="不在当前候选") as unavailable:
        selection_confirmation_arguments(
            _Uow(_fixture()),
            preview_run_id=RUN_ID,
            expectation=_expectation(),
            selected_bill_codes=["R9999"],
            now=NOW,
        )
    assert unavailable.value.code == "SELECTION_CHANGED"


def test_tampered_persisted_result_is_rejected():
    fixture = _fixture()
    fixture["steps"][RUN_ID][0]["result_summary_json"]["data"]["candidates"][0][
        "bill_code"
    ] = "R-TAMPERED"

    with pytest.raises(OrchestrationError, match="校验失败") as error:
        selection_preview_public_projection(
            _Uow(fixture),
            preview_run_id=RUN_ID,
            expectation=_expectation(),
            now=NOW,
        )
    assert error.value.code == "SELECTION_PREVIEW_INVALID"
