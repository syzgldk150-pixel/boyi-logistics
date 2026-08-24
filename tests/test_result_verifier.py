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


def _verified_result(*, proof_identity: str = "write_tool"):
    data = {"ok": True, "entity_id": "entity-1", "state": "updated"}
    digest = sha256_json(data)
    evidence_ref = f"tool-result:{proof_identity}:{digest}"
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


def test_installed_tool_alias_accepts_generation_bound_plugin_proof_identity():
    capability = _capability()
    capability["_plugin_runtime"] = {"plugin_id": "signed_plugin"}

    outcome = ResultVerifier().verify(
        _step(),
        _verified_result(proof_identity="signed_plugin"),
        capability,
    )

    assert outcome.accepted is True
    assert outcome.run_status is RunStatus.COMPLETED


def _scan_step(*, dry_run: bool) -> PlanStep:
    return PlanStep(
        step_key="scan",
        tool_name="automation.scan_codes.run",
        tool_version="1.0.23",
        operation_type=OperationType.READ if dry_run else OperationType.EXTERNAL_WRITE,
        arguments=(
            {"dry_run": True}
            if dry_run
            else {"dry_run": False, "_scan_preview_binding": {"context_sha256": "c" * 64}}
        ),
        account_id="account-1",
        depends_on=(),
        idempotency_key="scan-1",
        expected_evidence=(),
        postconditions=(
            {"name": "authoritative_scan_preview_returned"}
            if dry_run
            else {"name": "scan_formal_execution_verified"}
        ,),
        risk_level=RiskLevel.LOW if dry_run else RiskLevel.HIGH,
        requires_approval=not dry_run,
    )


def _scan_capability():
    capability = _capability(condition="scan_formal_execution_verified")
    capability["output_schema"]["properties"]["data"] = {
        "type": "object",
        "additionalProperties": True,
    }
    capability["_plugin_runtime"] = {
        "automation_id": "scan_codes",
        "plugin_id": "sync_scan_codes",
        "trust_source": "ed25519_first_party",
    }
    return capability


def _scan_result(*, dry_run: bool, batch_count: int = 1):
    observed_at = "2026-08-24T00:00:00Z"
    source_refs = ["evidence:source"]
    if dry_run:
        preview = {
            "observed_at": observed_at,
            "pagination_complete": True,
            "source_page_count": 1,
            "normalized_record_count": 1,
            "source_snapshot_sha256": "a" * 64,
            "source_evidence_refs": source_refs,
            "selection_count": 1,
            "selection_sha256": "b" * 64,
            "batch_count": 1,
            "batch_plan_sha256": "c" * 64,
        }
        details = {
            "phase": "preview",
            "pagination_complete": True,
            "source_page_count": 1,
            "normalized_record_count": 1,
            "source_snapshot_sha256": "a" * 64,
            "source_evidence_refs": source_refs,
            "selection_count": 1,
            "selection_sha256": "b" * 64,
            "batch_count": 1,
            "batch_plan_sha256": "c" * 64,
            "write_attempted": False,
        }
        condition = "authoritative_scan_preview_returned"
        primary = source_refs[-1]
        evidence_refs = source_refs
        data = {
            "phase": "preview",
            "dry_run": True,
            "preview_evidence": preview,
            "evidence": {"observed_at": observed_at},
        }
    else:
        projection = "evidence:projection"
        submit_refs = [f"evidence:submit:{index}" for index in range(batch_count)]
        verification_refs = [f"evidence:verify:{index}" for index in range(batch_count)]
        scheduled = batch_count
        details = {
            "phase": "formal",
            "preview_revalidation_matched": True,
            "preview_context_sha256": "c" * 64,
            "projection_evidence_ref": projection,
            "projection_record_count": 1,
            "projection_snapshot_sha256": "a" * 64,
            "batch_count": batch_count,
            "scheduled_items": scheduled,
            "scanned": scheduled,
            "skipped_signed_count": 0,
            "submit_evidence_refs": submit_refs,
            "verification_evidence_refs": verification_refs,
            "external_write_attempted": bool(batch_count),
        }
        condition = "scan_formal_execution_verified"
        primary = verification_refs[-1] if verification_refs else projection
        batch_refs = [
            ref
            for pair in zip(submit_refs, verification_refs, strict=True)
            for ref in pair
        ]
        evidence_refs = source_refs + [projection] + batch_refs
        data = {
            "phase": "formal",
            "dry_run": False,
            "batches": batch_count,
            "normalized": 1,
            "scheduled_items": scheduled,
            "scanned": scheduled,
            "skipped_signed_count": 0,
            "preview_revalidation": {
                "verified": True,
                "context_sha256": "c" * 64,
                "source_snapshot_sha256": "a" * 64,
            },
            "evidence": {"observed_at": observed_at},
        }
    return {
        "status": "SUCCESS",
        "data": data,
        "meta": {
            "source_system": "ronghui",
            "observed_at": observed_at,
            "record_count": 1,
            "pagination_complete": True,
            "evidence_refs": evidence_refs,
            "postconditions": {"0": True},
            "postcondition_evidence": {
                "0": {
                    "condition": condition,
                    "verified": True,
                    "observed_at": observed_at,
                    "evidence_ref": primary,
                    "details": details,
                }
            },
        },
        "warnings": [],
        "error": None,
    }


def _generation_bound_scan(result, *, requires_write: bool, started_writes: int):
    return GenerationBoundResult(
        result,
        verification=GenerationVerificationContext(
            automation_id="scan_codes",
            generation=1,
            lease_id="c0f9af26-8a75-469c-89a7-94fbce2453ad",
            account_ids=("account-1",),
            account_bindings_sha256="d" * 64,
            requires_write_verification=requires_write,
            started_mutating_call_count=started_writes,
        ),
    )


class _FinalizingGenerationLeases:
    def __init__(self):
        self.outcomes = []

    def finalize_generation_write(self, **values):
        self.outcomes.append(values["outcome"])


def test_scan_preview_requires_plugin_claim_and_core_zero_write_proof():
    result = _scan_result(dry_run=True)
    accepted = ResultVerifier().verify(
        _scan_step(dry_run=True),
        _generation_bound_scan(result, requires_write=False, started_writes=0),
        _scan_capability(),
    )
    assert accepted.accepted is True

    result["meta"]["postcondition_evidence"]["0"]["details"]["write_attempted"] = True
    rejected = ResultVerifier().verify(
        _scan_step(dry_run=True),
        _generation_bound_scan(result, requires_write=False, started_writes=0),
        _scan_capability(),
    )
    assert rejected.code == "POSTCONDITION_UNVERIFIED"


@pytest.mark.parametrize("batch_count", [0, 2])
def test_scan_formal_requires_complete_batch_proof_and_count_conservation(batch_count):
    leases = _FinalizingGenerationLeases()
    result = _scan_result(dry_run=False, batch_count=batch_count)
    outcome = ResultVerifier(leases).verify(
        _scan_step(dry_run=False),
        _generation_bound_scan(
            result,
            requires_write=True,
            started_writes=1 + batch_count,
        ),
        _scan_capability(),
    )
    assert outcome.accepted is True
    assert leases.outcomes


def test_scan_formal_missing_verification_or_tampered_conservation_is_rejected():
    result = _scan_result(dry_run=False, batch_count=2)
    result["meta"]["postcondition_evidence"]["0"]["details"][
        "verification_evidence_refs"
    ].pop()
    missing = ResultVerifier(_FinalizingGenerationLeases()).verify(
        _scan_step(dry_run=False),
        _generation_bound_scan(result, requires_write=True, started_writes=3),
        _scan_capability(),
    )
    assert missing.code == "POSTCONDITION_UNVERIFIED"

    result = _scan_result(dry_run=False, batch_count=2)
    result["data"]["scanned"] = 1
    tampered = ResultVerifier(_FinalizingGenerationLeases()).verify(
        _scan_step(dry_run=False),
        _generation_bound_scan(result, requires_write=True, started_writes=3),
        _scan_capability(),
    )
    assert tampered.code == "POSTCONDITION_UNVERIFIED"


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
