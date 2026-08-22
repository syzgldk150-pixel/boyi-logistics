from __future__ import annotations

from copy import deepcopy

import pytest

from agent.automation_plugins.models import (
    GenerationBoundResult,
    GenerationVerificationContext,
)
from agent.orchestration.models import OperationType, PlanStep, RiskLevel, RunStatus, sha256_json
from agent.orchestration.result_verifier import ResultVerifier


def _step(*, condition: str = "executor_reported_success") -> PlanStep:
    return PlanStep(
        step_key="write",
        tool_name="write_tool",
        tool_version="1.0.0",
        operation_type=OperationType.EXTERNAL_WRITE,
        arguments={"entity_id": "entity-1"},
        account_id="account-1",
        depends_on=(),
        idempotency_key="write-1",
        expected_evidence=(),
        postconditions=({"name": condition},),
        risk_level=RiskLevel.HIGH,
        requires_approval=True,
    )


def _capability(*, condition: str = "executor_reported_success"):
    return {
        "evidence": [{"required": True, "pagination_complete": False}],
        "postconditions": [{"name": condition}],
        "output_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["SUCCESS", "FAILED"]},
                "data": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "entity_id": {"type": "string"},
                        "state": {"type": "string"},
                    },
                    "required": ["ok", "entity_id", "state"],
                    "additionalProperties": False,
                },
                "meta": {"type": "object", "additionalProperties": True},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "error": {
                    "oneOf": [
                        {"type": "object", "additionalProperties": True},
                        {"type": "null"},
                    ]
                },
            },
            "required": ["status", "data", "meta", "warnings", "error"],
        },
    }


def _verified_result():
    data = {"ok": True, "entity_id": "entity-1", "state": "updated"}
    digest = sha256_json(data)
    evidence_ref = f"tool-result:write_tool:{digest}"
    observed_at = "2026-08-13T00:00:00Z"
    return {
        "status": "SUCCESS",
        "data": data,
        "meta": {
            "source_system": "external",
            "account_id": "account-1",
            "observed_at": observed_at,
            "record_count": 1,
            "pagination_complete": True,
            "evidence_refs": [evidence_ref],
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": "executor_reported_success",
                    "verified": True,
                    "observed_at": observed_at,
                    "evidence_ref": evidence_ref,
                    "details": {"result_sha256": digest},
                }
            },
        },
        "warnings": [],
        "error": None,
    }


def test_matching_condition_observation_evidence_and_result_hash_is_accepted():
    outcome = ResultVerifier().verify(_step(), _verified_result(), _capability())

    assert outcome.accepted is True
    assert outcome.run_status is RunStatus.COMPLETED


def test_write_finalization_persistence_failure_remains_unknown_and_recoverable():
    class _FailingGenerationLeases:
        def finalize_generation_write(self, **_kwargs):
            raise RuntimeError("generation repository unavailable")

    value = _verified_result()
    value["meta"].pop("account_id")
    raw = GenerationBoundResult(
        value,
        verification=GenerationVerificationContext(
            automation_id="write-instance",
            generation=1,
            lease_id="c0f9af26-8a75-469c-89a7-94fbce2453ad",
            account_ids=("account-1",),
            account_bindings_sha256="a" * 64,
            requires_write_verification=True,
        ),
    )

    outcome = ResultVerifier(_FailingGenerationLeases()).verify(
        _step(), raw, _capability()
    )

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.BLOCKED_DATA
    assert outcome.code == "WRITE_OUTCOME_UNKNOWN"
    assert "RuntimeError" in outcome.message
    assert "generation repository unavailable" in outcome.message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("condition", "some_other_condition"),
        ("observed_at", "2026-08-13T00:00:01Z"),
        ("evidence_ref", "evidence:other"),
    ],
)
def test_mismatched_proof_metadata_is_blocked_data(field, value):
    result = _verified_result()
    result["meta"]["postcondition_evidence"]["0"][field] = value

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.BLOCKED_DATA
    assert outcome.code == "POSTCONDITION_UNVERIFIED"


def test_executor_proof_for_a_different_result_hash_is_blocked_data():
    result = _verified_result()
    result["data"]["state"] = "different"

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.BLOCKED_DATA
    assert outcome.code == "POSTCONDITION_UNVERIFIED"


def test_failed_postcondition_result_is_classified_as_blocked_data():
    result = deepcopy(_verified_result())
    result["status"] = "FAILED"
    result["error"] = {
        "code": "POSTCONDITION_UNVERIFIED",
        "message": "source read-back was inconclusive",
        "retryable": False,
    }

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.BLOCKED_DATA
    assert outcome.code == "POSTCONDITION_UNVERIFIED"


def test_naive_result_observation_time_is_rejected():
    result = _verified_result()
    result["meta"]["observed_at"] = "2026-08-13T00:00:00"
    result["meta"]["postcondition_evidence"]["0"]["observed_at"] = "2026-08-13T00:00:00"

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.code == "INVALID_OBSERVED_AT"


def test_missing_required_output_field_is_blocked_data():
    result = _verified_result()
    del result["data"]["entity_id"]

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.BLOCKED_DATA
    assert outcome.code == "OUTPUT_SCHEMA_MISMATCH"


def test_missing_registered_output_schema_is_terminal_configuration_failure():
    capability = _capability()
    del capability["output_schema"]

    outcome = ResultVerifier().verify(_step(), _verified_result(), capability)

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.FAILED_TERMINAL
    assert outcome.code == "INVALID_TOOL_OUTPUT_SCHEMA"


def test_governed_pending_login_failure_blocks_for_session_recovery():
    result = {
        "status": "FAILED",
        "data": {},
        "meta": {
            "blocked_status": "BLOCKED_LOGIN",
            "source_system": "yunda",
            "account_id": "account-1",
        },
        "warnings": [],
        "error": {
            "code": "AUTH_PENDING_CODE",
            "message": "verification code is required",
            "retryable": False,
        },
    }

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.BLOCKED_LOGIN
    assert outcome.code == "AUTH_PENDING_CODE"


@pytest.mark.parametrize("code", ["INVALID_PAGINATION_TOTAL", "MISSING_EXTERNAL_ID"])
def test_governed_source_failure_honors_only_known_blocked_data_status(code):
    result = {
        "status": "FAILED",
        "data": {},
        "meta": {"blocked_status": "BLOCKED_DATA"},
        "warnings": [],
        "error": {"code": code, "message": "source evidence is incomplete", "retryable": False},
    }

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.BLOCKED_DATA
    assert outcome.code == code


def test_unknown_caller_blocked_status_cannot_override_terminal_failure():
    result = {
        "status": "FAILED",
        "data": {},
        "meta": {"blocked_status": "COMPLETED"},
        "warnings": [],
        "error": {"code": "UNCLASSIFIED_FAILURE", "message": "failed", "retryable": False},
    }

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.FAILED_TERMINAL


def test_cancelled_tool_result_is_a_cancelled_run_not_a_terminal_failure():
    result = {
        "status": "FAILED",
        "data": {},
        "meta": {},
        "warnings": [],
        "error": {
            "code": "CANCELLED",
            "message": "Tool execution was cancelled",
            "retryable": False,
        },
    }

    outcome = ResultVerifier().verify(_step(), result, _capability())

    assert outcome.accepted is False
    assert outcome.run_status is RunStatus.CANCELLED
    assert outcome.code == "CANCELLED"
