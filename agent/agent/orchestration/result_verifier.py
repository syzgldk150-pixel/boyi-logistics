"""Strictly distinguish process completion from business success."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.orchestration.models import OrchestrationError, PlanStep, RunStatus, ToolResult, sha256_json
from agent.tool_registry import validate_schema_instance


@dataclass(frozen=True)
class VerificationOutcome:
    accepted: bool
    run_status: RunStatus
    code: str
    message: str
    result: ToolResult | None = None


class ResultVerifier:
    REQUIRED_META_FIELDS = frozenset(
        {"source_system", "account_id", "observed_at", "record_count", "pagination_complete", "evidence_refs"}
    )

    def verify(
        self,
        step: PlanStep,
        raw_result: Mapping[str, Any],
        capability: Mapping[str, Any],
    ) -> VerificationOutcome:
        if not isinstance(raw_result, Mapping):
            return self._failure("INVALID_RESULT_CONTRACT", "Tool result must be a JSON object")
        if raw_result.get("ok") is False:
            return self._classified_failure(raw_result)
        if raw_result.get("success") is False:
            return self._classified_failure(raw_result)

        try:
            normalized = self._normalize_result(raw_result)
        except OrchestrationError as exc:
            return self._failure(exc.code, exc.message)
        if normalized.status != "SUCCESS":
            return self._classified_failure(normalized.to_dict())

        missing_meta = sorted(field for field in self.REQUIRED_META_FIELDS if field not in normalized.meta)
        if missing_meta:
            return self._failure(
                "RESULT_META_MISSING",
                f"Tool result meta is missing: {', '.join(missing_meta)}",
            )
        if step.account_id and str(normalized.meta.get("account_id") or "") != step.account_id:
            return self._failure("RESULT_ACCOUNT_MISMATCH", "Tool result account does not match the approved plan")
        if not self._valid_observed_at(normalized.meta.get("observed_at")):
            return self._failure("INVALID_OBSERVED_AT", "Tool result observed_at must be an ISO timestamp")
        if not isinstance(normalized.meta.get("record_count"), int) or normalized.meta.get("record_count") < 0:
            return self._failure("INVALID_RECORD_COUNT", "Tool result record_count must be a non-negative integer")
        if not isinstance(normalized.meta.get("evidence_refs"), list) or any(
            not isinstance(value, str) or not value.strip()
            for value in normalized.meta.get("evidence_refs") or []
        ):
            return self._failure("INVALID_EVIDENCE_REFS", "Tool result evidence_refs must be an array")

        output_schema = capability.get("output_schema")
        if not isinstance(output_schema, Mapping):
            return self._failure(
                "INVALID_TOOL_OUTPUT_SCHEMA",
                "The registered tool output schema is missing or invalid",
            )
        try:
            validate_schema_instance(
                f"{step.tool_name} output",
                normalized.to_dict(),
                output_schema,
            )
        except (KeyError, TypeError, ValueError):
            return VerificationOutcome(
                accepted=False,
                run_status=RunStatus.BLOCKED_DATA,
                code="OUTPUT_SCHEMA_MISMATCH",
                message="Tool result data does not match the registered output schema",
                result=normalized,
            )

        requirements = capability.get("evidence") or []
        evidence_requirements = [requirements] if isinstance(requirements, Mapping) else requirements
        if not isinstance(evidence_requirements, list):
            return self._failure("INVALID_TOOL_EVIDENCE", "Tool evidence contract is invalid")
        requires_complete_pagination = any(
            bool(item.get("pagination_complete"))
            or "pagination_complete" in (item.get("required_fields") or [])
            for item in evidence_requirements
            if isinstance(item, Mapping)
        )
        if requires_complete_pagination and normalized.meta.get("pagination_complete") is not True:
            return VerificationOutcome(
                accepted=False,
                run_status=RunStatus.BLOCKED_DATA,
                code="PAGINATION_INCOMPLETE",
                message="The source query did not prove complete pagination",
                result=normalized,
            )
        if any(bool(item.get("required")) for item in evidence_requirements if isinstance(item, Mapping)) and not normalized.meta.get("evidence_refs"):
            return VerificationOutcome(
                accepted=False,
                run_status=RunStatus.BLOCKED_DATA,
                code="EVIDENCE_MISSING",
                message="Required evidence references are missing",
                result=normalized,
            )

        postconditions = capability.get("postconditions") or []
        postcondition_requirements = [postconditions] if isinstance(postconditions, Mapping) else postconditions
        if not isinstance(postcondition_requirements, list):
            return self._failure("INVALID_TOOL_POSTCONDITIONS", "Tool postconditions contract is invalid")
        if step.operation_type.value not in {"read", "compute"} and postcondition_requirements:
            reported = normalized.meta.get("postconditions")
            proofs = normalized.meta.get("postcondition_evidence")
            evidence_refs = {str(value) for value in normalized.meta.get("evidence_refs") or []}
            result_observed_at = str(normalized.meta.get("observed_at") or "")
            for index, requirement in enumerate(postcondition_requirements):
                key = str(index)
                name = str(requirement.get("name") or "") if isinstance(requirement, Mapping) else ""
                proof = proofs.get(key) if isinstance(proofs, Mapping) else None
                if (
                    not isinstance(reported, Mapping)
                    or reported.get(key) is not True
                    or not isinstance(proof, Mapping)
                    or proof.get("verified") is not True
                    or str(proof.get("condition") or "") != name
                    or not self._valid_observed_at(proof.get("observed_at"))
                    or str(proof.get("observed_at") or "") != result_observed_at
                    or str(proof.get("evidence_ref") or "") not in evidence_refs
                ):
                    return VerificationOutcome(
                        accepted=False,
                        run_status=RunStatus.BLOCKED_DATA,
                        code="POSTCONDITION_UNVERIFIED",
                        message="One or more write postconditions lack matching source evidence",
                        result=normalized,
                    )
                if name == "executor_reported_success":
                    digest = sha256_json(normalized.data)
                    expected_ref = f"tool-result:{step.tool_name}:{digest}"
                    details = proof.get("details")
                    if (
                        str(proof.get("evidence_ref") or "") != expected_ref
                        or not isinstance(details, Mapping)
                        or str(details.get("result_sha256") or "") != digest
                    ):
                        return VerificationOutcome(
                            accepted=False,
                            run_status=RunStatus.BLOCKED_DATA,
                            code="POSTCONDITION_UNVERIFIED",
                            message="Executor success proof does not match the actual tool result",
                            result=normalized,
                        )

        return VerificationOutcome(
            accepted=True,
            run_status=RunStatus.COMPLETED,
            code="VERIFIED",
            message="Tool result and evidence were verified",
            result=normalized,
        )

    def _normalize_result(self, raw_result: Mapping[str, Any]) -> ToolResult:
        status = str(raw_result.get("status") or "").upper()
        if not status:
            raise OrchestrationError("INVALID_RESULT_CONTRACT", "Tool result status is required")
        data = raw_result.get("data")
        meta = raw_result.get("meta")
        warnings = raw_result.get("warnings") or []
        error = raw_result.get("error")
        if not isinstance(data, Mapping) or not isinstance(meta, Mapping):
            raise OrchestrationError("INVALID_RESULT_CONTRACT", "Tool result data and meta must be JSON objects")
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise OrchestrationError("INVALID_RESULT_CONTRACT", "Tool result warnings must be a string array")
        if error is not None and not isinstance(error, Mapping):
            raise OrchestrationError("INVALID_RESULT_CONTRACT", "Tool result error must be null or an object")
        return ToolResult(status=status, data=dict(data), meta=dict(meta), warnings=tuple(warnings), error=error)

    @staticmethod
    def _valid_observed_at(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None

    def _classified_failure(self, result: Mapping[str, Any]) -> VerificationOutcome:
        error_value = result.get("error")
        error = error_value if isinstance(error_value, Mapping) else {}
        meta_value = result.get("meta")
        meta = meta_value if isinstance(meta_value, Mapping) else {}
        code = str(result.get("error_code") or error.get("code") or "TOOL_RESULT_FAILED").upper()
        message = str(error.get("message") or error_value or result.get("message") or "Tool result reported failure")
        governed_blocked_status = str(meta.get("blocked_status") or "").upper()
        if governed_blocked_status == RunStatus.BLOCKED_LOGIN.value or code in {
            "LOGIN_REQUIRED",
            "AUTH_REQUIRED",
            "AUTH_PENDING_CODE",
            "SESSION_EXPIRED",
            "BLOCKED_LOGIN",
        }:
            status = RunStatus.BLOCKED_LOGIN
        elif governed_blocked_status == RunStatus.BLOCKED_DATA.value or code in {
            "PAGINATION_INCOMPLETE",
            "ACCOUNT_AMBIGUOUS",
            "MISSING_FIELD",
            "NO_DATA",
            "BLOCKED_DATA",
            "UNKNOWN_CANDIDATE",
            "POSTCONDITION_UNVERIFIED",
            "INCOMPLETE_SOURCE_EVIDENCE",
            "INVALID_SOURCE_EVIDENCE",
        }:
            status = RunStatus.BLOCKED_DATA
        elif bool(result.get("retryable") or error.get("retryable")):
            status = RunStatus.FAILED_RETRYABLE
        else:
            status = RunStatus.FAILED_TERMINAL
        return VerificationOutcome(False, status, code, message)

    @staticmethod
    def _failure(code: str, message: str) -> VerificationOutcome:
        return VerificationOutcome(False, RunStatus.FAILED_TERMINAL, code, message)
