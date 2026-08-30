"""Strictly distinguish process completion from business success."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
import uuid

from agent.automation_plugins.models import (
    GenerationBoundResult,
    GenerationVerificationContext,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.migration import PluginMigrationRuntimeCoordinator
from agent.automation_plugins.code_owned_fields import (
    SCAN_FORMAL_POSTCONDITION,
    SCAN_PHASE_PREVIEW,
    SCAN_PREVIEW_POSTCONDITION,
    resolve_scan_capability_phase,
)
from agent.automation_plugins.ports import RuntimeGenerationLeasePort
from agent.orchestration.models import OrchestrationError, PlanStep, RunStatus, ToolResult, sha256_json
from agent.tool_registry import validate_schema_instance
from shared.redaction import redact_text


@dataclass(frozen=True)
class VerificationOutcome:
    accepted: bool
    run_status: RunStatus
    code: str
    message: str
    result: ToolResult | None = None
    generation_verification: GenerationVerificationContext | None = None


class ResultVerifier:
    _SERVICE_V2_RUNTIME_MODEL = "SERVICE_V2"
    _SERVICE_V2_DIRECT_WRITE_EFFECTS = frozenset({"external_write", "destructive"})
    _SERVICE_V2_WRITE_POSTCONDITION = "plugin_result_contract_valid"
    _SERVICE_V2_REQUIRED_EVIDENCE_FIELDS = frozenset(
        {"service", "operation", "outcome"}
    )
    _CLOCK_SITE_FIELDS = ("sitecode", "sitefbcode", "sitename", "sitefbname")
    REQUIRED_META_FIELDS = frozenset(
        {"source_system", "account_id", "observed_at", "record_count", "pagination_complete", "evidence_refs"}
    )

    def __init__(
        self,
        generation_leases: RuntimeGenerationLeasePort | None = None,
        migration_runtime: PluginMigrationRuntimeCoordinator | None = None,
    ) -> None:
        self._generation_leases = generation_leases
        self._migration_runtime = migration_runtime

    def verify(
        self,
        step: PlanStep,
        raw_result: Mapping[str, Any],
        capability: Mapping[str, Any],
    ) -> VerificationOutcome:
        verification = getattr(raw_result, "generation_verification", None)
        schema_result: Mapping[str, Any] = raw_result
        verification_error: VerificationOutcome | None = None
        if isinstance(verification, GenerationVerificationContext):
            raw_meta = raw_result.get("meta")
            if not isinstance(raw_meta, Mapping) or "account_id" in raw_meta:
                verification_error = self._failure(
                    "PLUGIN_ACCOUNT_PROOF_FORGED",
                    "Plugin payload cannot provide core account binding proof",
                )
            elif step.account_id and step.account_id not in verification.account_ids:
                verification_error = self._failure(
                    "RESULT_ACCOUNT_MISMATCH",
                    "Approved account is not bound to the committed plugin generation",
                )
            else:
                # The subprocess never receives account identifiers, and its
                # verified result must not turn the core-only side channel into
                # a new disclosure path.  A stable binding-set proof works for
                # both singleton and collection roles; the actual IDs remain
                # available only on ``GenerationVerificationContext`` to
                # trusted core projections and reconcilers.
                trusted_account_id = f"binding-set:{verification.account_bindings_sha256}"
                enriched = dict(raw_result)
                enriched["meta"] = {**dict(raw_meta), "account_id": trusted_account_id}
                raw_result = GenerationBoundResult(enriched, verification=verification)
        outcome = verification_error or self._verify(
            step,
            raw_result,
            capability,
            schema_result=schema_result,
            trusted_account_proof=(
                f"binding-set:{verification.account_bindings_sha256}"
                if isinstance(verification, GenerationVerificationContext)
                else None
            ),
            generation_verification=verification,
        )
        outcome = self._finalize_generation_write(step, raw_result, outcome, verification)
        if outcome.accepted and isinstance(verification, GenerationVerificationContext):
            return replace(outcome, generation_verification=verification)
        return outcome

    def _verify(
        self,
        step: PlanStep,
        raw_result: Mapping[str, Any],
        capability: Mapping[str, Any],
        *,
        schema_result: Mapping[str, Any] | None = None,
        trusted_account_proof: str | None = None,
        generation_verification: GenerationVerificationContext | None = None,
    ) -> VerificationOutcome:
        if not isinstance(raw_result, Mapping):
            return self._failure("INVALID_RESULT_CONTRACT", "Tool result must be a JSON object")
        account_scope = capability.get("account_scope") or {}
        account_scope_mode = (
            str(account_scope.get("mode") or "none")
            if isinstance(account_scope, Mapping) and "mode" in account_scope
            else "single" if isinstance(account_scope, Mapping) and account_scope.get("required") else "optional"
        )
        if account_scope_mode == "none" and step.account_id:
            return self._failure(
                "ACCOUNT_SCOPE_MISMATCH",
                "This tool does not accept an account binding",
            )
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
        expected_account_proof = trusted_account_proof or step.account_id
        if expected_account_proof and str(normalized.meta.get("account_id") or "") != expected_account_proof:
            return self._failure("RESULT_ACCOUNT_MISMATCH", "Tool result account does not match the approved plan")
        if not self._valid_observed_at(normalized.meta.get("observed_at")):
            return self._failure("INVALID_OBSERVED_AT", "Tool result observed_at must be an ISO timestamp")
        if not isinstance(normalized.meta.get("record_count"), int) or normalized.meta.get("record_count") < 0:
            return self._failure("INVALID_RECORD_COUNT", "Tool result record_count must be a non-negative integer")
        if not isinstance(normalized.meta.get("evidence_refs"), list) or any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in normalized.meta.get("evidence_refs") or []
        ):
            return self._failure("INVALID_EVIDENCE_REFS", "Tool result evidence_refs must be an array")
        if len(normalized.meta.get("evidence_refs") or []) != len(
            set(normalized.meta.get("evidence_refs") or [])
        ):
            return self._failure(
                "INVALID_EVIDENCE_REFS",
                "Tool result evidence_refs must not contain duplicates",
            )

        output_schema = capability.get("output_schema")
        if not isinstance(output_schema, Mapping):
            return self._failure(
                "INVALID_TOOL_OUTPUT_SCHEMA",
                "The registered tool output schema is missing or invalid",
            )
        try:
            validate_schema_instance(
                f"{step.tool_name} output",
                dict(schema_result) if schema_result is not None else normalized.to_dict(),
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

        service_v2_evidence_error = self._service_v2_evidence_error(
            step=step,
            capability=capability,
            normalized=normalized,
            evidence_requirements=evidence_requirements,
        )
        if service_v2_evidence_error is not None:
            return service_v2_evidence_error

        try:
            scan_phase = resolve_scan_capability_phase(capability, step.arguments)
        except ValueError:
            return self._failure(
                "SCAN_EXECUTION_PHASE_INVALID",
                "Scan execution phase is incomplete or ambiguous",
            )
        postconditions = (
            [{"name": SCAN_PREVIEW_POSTCONDITION}]
            if scan_phase == SCAN_PHASE_PREVIEW
            else capability.get("postconditions") or []
        )
        postcondition_requirements = [postconditions] if isinstance(postconditions, Mapping) else postconditions
        if not isinstance(postcondition_requirements, list):
            return self._failure("INVALID_TOOL_POSTCONDITIONS", "Tool postconditions contract is invalid")
        service_v2_postcondition_error = self._service_v2_postcondition_error(
            step=step,
            capability=capability,
            normalized=normalized,
            postcondition_requirements=postcondition_requirements,
        )
        if service_v2_postcondition_error is not None:
            return service_v2_postcondition_error
        if (
            step.operation_type.value not in {"read", "compute"}
            or scan_phase == SCAN_PHASE_PREVIEW
        ) and postcondition_requirements:
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
                    plugin_runtime = capability.get("_plugin_runtime")
                    proof_identity = (
                        str(plugin_runtime.get("plugin_id") or "").strip()
                        if isinstance(plugin_runtime, Mapping)
                        else ""
                    ) or step.tool_name
                    expected_ref = f"tool-result:{proof_identity}:{digest}"
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
                elif name == SCAN_PREVIEW_POSTCONDITION:
                    message = self._scan_preview_proof_error(
                        normalized=normalized,
                        proof=proof,
                        generation_verification=generation_verification,
                    )
                    if message:
                        return VerificationOutcome(
                            accepted=False,
                            run_status=RunStatus.BLOCKED_DATA,
                            code="POSTCONDITION_UNVERIFIED",
                            message=message,
                            result=normalized,
                        )
                elif name == SCAN_FORMAL_POSTCONDITION:
                    message = self._scan_formal_proof_error(
                        normalized=normalized,
                        proof=proof,
                        generation_verification=generation_verification,
                    )
                    if message:
                        return VerificationOutcome(
                            accepted=False,
                            run_status=RunStatus.BLOCKED_DATA,
                            code="POSTCONDITION_UNVERIFIED",
                            message=message,
                            result=normalized,
                        )

        return VerificationOutcome(
            accepted=True,
            run_status=RunStatus.COMPLETED,
            code="VERIFIED",
            message="Tool result and evidence were verified",
            result=normalized,
        )

    @classmethod
    def _is_service_v2(cls, capability: Mapping[str, Any]) -> bool:
        runtime = capability.get("_plugin_runtime")
        return (
            isinstance(runtime, Mapping)
            and str(runtime.get("runtime_model") or "").strip().upper()
            == cls._SERVICE_V2_RUNTIME_MODEL
        )

    @classmethod
    def _is_service_v2_direct_write(
        cls,
        step: PlanStep,
        capability: Mapping[str, Any],
    ) -> bool:
        if not cls._is_service_v2(capability):
            return False
        effect = str(capability.get("effect") or "").strip().lower()
        operation_type = str(
            capability.get("operation_type") or step.operation_type.value
        ).strip().lower()
        return (
            effect in cls._SERVICE_V2_DIRECT_WRITE_EFFECTS
            or operation_type in cls._SERVICE_V2_DIRECT_WRITE_EFFECTS
        )

    @staticmethod
    def _non_empty_contract_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    @classmethod
    def _service_v2_expected_target(
        cls,
        capability: Mapping[str, Any],
    ) -> tuple[str | None, str | None, VerificationOutcome | None]:
        service = cls._non_empty_contract_text(capability.get("service"))
        operation = cls._non_empty_contract_text(capability.get("operation"))
        if service is None or operation is None:
            return None, None, cls._failure(
                "INVALID_TOOL_EVIDENCE",
                "Service v2 write contract must declare one service and operation",
            )

        runtime = capability.get("_plugin_runtime")
        compiled_invocations = (
            runtime.get("compiled_invocations")
            if isinstance(runtime, Mapping)
            else None
        )
        if isinstance(compiled_invocations, Mapping):
            for compiled in compiled_invocations.values():
                if not isinstance(compiled, Mapping):
                    return None, None, cls._failure(
                        "INVALID_TOOL_EVIDENCE",
                        "Service v2 invocation target is invalid",
                    )
                target = compiled.get("target")
                if not isinstance(target, Mapping):
                    return None, None, cls._failure(
                        "INVALID_TOOL_EVIDENCE",
                        "Service v2 invocation target is missing",
                    )
                target_service = cls._non_empty_contract_text(target.get("service"))
                target_operation = cls._non_empty_contract_text(target.get("operation"))
                target_identity = {
                    "service",
                    "operation",
                    "contribution_id",
                    "contribution_kind",
                }
                if (
                    set(target) != target_identity
                    or target_service is None
                    or target_operation is None
                    or any(
                        cls._non_empty_contract_text(target.get(field)) is None
                        for field in ("contribution_id", "contribution_kind")
                    )
                ):
                    return None, None, cls._failure(
                        "INVALID_TOOL_EVIDENCE",
                        "Service v2 invocation target is invalid",
                    )
                if target_service == service and target_operation == operation:
                    return service, operation, None
            # The compiled table is core-owned when present.  A capability
            # contract that is not represented there is stale and cannot be
            # used to accept a plugin-produced business result.
            if compiled_invocations:
                return None, None, cls._failure(
                    "INVALID_TOOL_EVIDENCE",
                    "Service v2 result target does not match its committed invocation contract",
                )
        return service, operation, None

    @classmethod
    def _service_v2_evidence_error(
        cls,
        *,
        step: PlanStep,
        capability: Mapping[str, Any],
        normalized: ToolResult,
        evidence_requirements: list[Any],
    ) -> VerificationOutcome | None:
        if not cls._is_service_v2_direct_write(step, capability):
            return None

        required_contracts = [
            item
            for item in evidence_requirements
            if isinstance(item, Mapping) and item.get("required") is True
        ]
        if len(required_contracts) != 1:
            return cls._failure(
                "INVALID_TOOL_EVIDENCE",
                "Service v2 write contract must contain exactly one required evidence declaration",
            )
        required_fields = required_contracts[0].get("required_fields")
        if (
            not isinstance(required_fields, list)
            or any(
                not isinstance(field, str) or not field.strip()
                for field in required_fields
            )
            or len(required_fields) != len(set(required_fields))
            or set(required_fields) != cls._SERVICE_V2_REQUIRED_EVIDENCE_FIELDS
        ):
            return cls._failure(
                "INVALID_TOOL_EVIDENCE",
                "Service v2 write evidence must require service, operation and outcome",
            )

        expected_service, expected_operation, contract_error = cls._service_v2_expected_target(
            capability
        )
        if contract_error is not None:
            return contract_error
        evidence = normalized.data.get("evidence")
        if not isinstance(evidence, Mapping):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write result lacks independent Host evidence",
            )
        observed_at = normalized.meta.get("observed_at")
        if (
            not cls._valid_observed_at(observed_at)
            or not isinstance(normalized.meta.get("source_system"), str)
            or not normalized.meta.get("source_system", "").strip()
            or evidence.get("service") != expected_service
            or evidence.get("operation") != expected_operation
            or evidence.get("outcome") != "WRITE_VERIFIED"
            or not isinstance(evidence.get("service"), str)
            or not isinstance(evidence.get("operation"), str)
            or not isinstance(evidence.get("outcome"), str)
            or normalized.meta.get("write_outcome") != "WRITE_VERIFIED"
        ):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write evidence identity or outcome is inconsistent",
            )
        if "observed_at" in evidence and (
            not cls._valid_observed_at(evidence.get("observed_at"))
            or evidence.get("observed_at") != observed_at
        ):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write evidence observation time is inconsistent",
            )
        identity_error = cls._service_v2_identity_error(normalized.data)
        if identity_error is not None:
            return cls._blocked_service_v2_result(normalized, identity_error)
        clock_error = cls._service_v2_clock_result_error(step, normalized)
        if clock_error is not None:
            return cls._blocked_service_v2_result(normalized, clock_error)
        return None

    @classmethod
    def _service_v2_identity_error(cls, value: Any) -> str | None:
        """Service payloads cannot disclose or invent core account identity."""

        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                if (
                    key in {"account_id", "account_ids"}
                    or key.endswith(("_account_id", "_account_ids"))
                ):
                    return "Service v2 result contains a broker-owned account identity"
                nested_error = cls._service_v2_identity_error(child)
                if nested_error is not None:
                    return nested_error
        elif isinstance(value, list):
            for child in value:
                nested_error = cls._service_v2_identity_error(child)
                if nested_error is not None:
                    return nested_error
        return None

    @classmethod
    def _service_v2_clock_result_error(
        cls,
        step: PlanStep,
        normalized: ToolResult,
    ) -> str | None:
        evidence = normalized.data.get("evidence")
        results = normalized.data.get("results")
        if not isinstance(evidence, Mapping) or (
            "both_third_party_clock_ins_confirmed" not in evidence
        ):
            return None
        if evidence.get("both_third_party_clock_ins_confirmed") is not True:
            return "Dual clock result did not prove both independent confirmations"
        if normalized.meta.get("source_system") != "ronghui":
            return "Dual clock result source system is not Ronghui"
        if not isinstance(results, list) or len(results) != 2:
            return "Dual clock result must contain exactly two verification results"
        if normalized.meta.get("record_count") != len(results):
            return "Dual clock result count is inconsistent with its result summary"

        expected_types = [
            str(step.arguments.get("first_type") or "").strip(),
            str(step.arguments.get("second_type") or "").strip(),
        ]
        if not all(expected_types) or expected_types[0] == expected_types[1]:
            return "Dual clock request does not declare two distinct clock types"
        expected_site = {
            field: str(step.arguments.get(field) or "").strip()
            for field in cls._CLOCK_SITE_FIELDS
        }
        if not all(expected_site.values()):
            return "Dual clock request does not declare a complete site identity"

        refs = normalized.meta.get("evidence_refs")
        if not isinstance(refs, list) or len(refs) != 5:
            return "Dual clock evidence must contain precheck, submit and verify receipts"
        result_refs: list[str] = []
        operation_ids: list[str] = []
        observed_at = normalized.meta.get("observed_at")
        try:
            result_observed = datetime.fromisoformat(
                str(observed_at).replace("Z", "+00:00")
            )
        except ValueError:
            return "Dual clock result observation time is invalid"
        for index, result in enumerate(results):
            if not isinstance(result, Mapping):
                return "Dual clock verification result is invalid"
            if (
                result.get("clock_type") != expected_types[index]
                or result.get("confirmed") is not True
                or not isinstance(result.get("operation_id"), str)
                or not result.get("operation_id").strip()
                or not isinstance(result.get("outcome_category"), str)
                or not result.get("outcome_category").strip()
                or not isinstance(result.get("evidence_ref"), str)
                or not result.get("evidence_ref").strip()
                or result.get("evidence_ref") not in refs
                or result.get("site") != expected_site
                or not cls._valid_observed_at(result.get("observed_at"))
            ):
                return "Dual clock verification result is incomplete or cross-site"
            try:
                verification_observed = datetime.fromisoformat(
                    str(result["observed_at"]).replace("Z", "+00:00")
                )
            except ValueError:
                return "Dual clock verification result observation time is invalid"
            if verification_observed > result_observed:
                return "Dual clock verification result is newer than its result receipt"
            result_refs.append(str(result["evidence_ref"]))
            operation_ids.append(str(result["operation_id"]))
        if len(set(result_refs)) != len(result_refs) or len(set(operation_ids)) != len(operation_ids):
            return "Dual clock verification receipts or operation identifiers are duplicated"
        return None

    @classmethod
    def _service_v2_postcondition_error(
        cls,
        *,
        step: PlanStep,
        capability: Mapping[str, Any],
        normalized: ToolResult,
        postcondition_requirements: list[Any],
    ) -> VerificationOutcome | None:
        if not cls._is_service_v2_direct_write(step, capability):
            return None
        if len(postcondition_requirements) != 1 or not isinstance(
            postcondition_requirements[0], Mapping
        ):
            return cls._failure(
                "INVALID_TOOL_POSTCONDITIONS",
                "Service v2 write contract must declare one postcondition",
            )
        if postcondition_requirements[0].get("name") != cls._SERVICE_V2_WRITE_POSTCONDITION:
            return cls._failure(
                "INVALID_TOOL_POSTCONDITIONS",
                "Service v2 write contract has an unsupported postcondition",
            )
        reported = normalized.meta.get("postconditions")
        proofs = normalized.meta.get("postcondition_evidence")
        if (
            not isinstance(reported, Mapping)
            or set(reported) != {"0"}
            or reported.get("0") is not True
            or not isinstance(proofs, Mapping)
            or set(proofs) != {"0"}
        ):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write postcondition proof is missing or ambiguous",
            )
        proof = proofs["0"]
        refs = normalized.meta.get("evidence_refs")
        required_proof_fields = {"condition", "verified", "observed_at", "evidence_ref", "details"}
        if (
            not isinstance(proof, Mapping)
            or not required_proof_fields <= set(proof)
            or proof.get("condition") != cls._SERVICE_V2_WRITE_POSTCONDITION
            or proof.get("verified") is not True
            or not cls._valid_observed_at(proof.get("observed_at"))
            or proof.get("observed_at") != normalized.meta.get("observed_at")
            or not isinstance(refs, list)
            or not refs
            or proof.get("evidence_ref") != refs[-1]
        ):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write postcondition proof does not match Host evidence",
            )
        details = proof.get("details")
        if not isinstance(details, Mapping):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write postcondition summary is missing",
            )
        detail_refs = details.get("evidence_refs")
        summary = details.get("result_summary")
        if detail_refs != refs or not isinstance(summary, Mapping):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write postcondition summary does not close Host evidence refs",
            )
        expected_service = str(capability.get("service") or "").strip()
        expected_operation = str(capability.get("operation") or "").strip()
        if not cls._service_v2_summary_matches(
            summary,
            normalized,
            service=expected_service,
            operation=expected_operation,
        ):
            return cls._blocked_service_v2_result(
                normalized,
                "Service v2 write postcondition summary does not match the result",
            )
        return None

    @classmethod
    def _service_v2_summary_matches(
        cls,
        summary: Mapping[str, Any],
        normalized: ToolResult,
        *,
        service: str,
        operation: str,
    ) -> bool:
        # A full result summary is the strongest portable form: the core
        # compares it byte-for-byte as a JSON structure.  A compact summary is
        # accepted only when it includes a core-recomputable result digest;
        # otherwise a plugin could assert success while omitting changed rows.
        if dict(summary) == dict(normalized.data):
            return True
        if (
            summary.get("service") != service
            or summary.get("operation") != operation
            or summary.get("outcome") != "WRITE_VERIFIED"
            or summary.get("record_count") != normalized.meta.get("record_count")
            or summary.get("evidence_refs") != normalized.meta.get("evidence_refs")
        ):
            return False
        digest = summary.get("result_sha256")
        return isinstance(digest, str) and digest == sha256_json(normalized.data)

    @staticmethod
    def _blocked_service_v2_result(
        normalized: ToolResult,
        message: str,
    ) -> VerificationOutcome:
        return VerificationOutcome(
            accepted=False,
            run_status=RunStatus.BLOCKED_DATA,
            code="POSTCONDITION_UNVERIFIED",
            message=message,
            result=normalized,
        )

    @staticmethod
    def _scan_preview_proof_error(
        *,
        normalized: ToolResult,
        proof: Mapping[str, Any],
        generation_verification: GenerationVerificationContext | None,
    ) -> str | None:
        data = normalized.data
        preview = data.get("preview_evidence")
        details = proof.get("details")
        evidence_refs = normalized.meta.get("evidence_refs")
        expected_detail_fields = {
            "phase",
            "pagination_complete",
            "source_page_count",
            "normalized_record_count",
            "source_snapshot_sha256",
            "source_evidence_refs",
            "selection_count",
            "selection_sha256",
            "batch_count",
            "batch_plan_sha256",
            "write_attempted",
        }
        if (
            data.get("phase") != "preview"
            or data.get("dry_run") is not True
            or not isinstance(preview, Mapping)
            or not isinstance(details, Mapping)
            or set(details) != expected_detail_fields
            or details.get("phase") != "preview"
            or details.get("pagination_complete") is not True
            or details.get("write_attempted") is not False
            or generation_verification is None
            or generation_verification.started_mutating_call_count != 0
            or not isinstance(evidence_refs, list)
        ):
            return "Scan preview lacks authoritative zero-write evidence"
        source_refs = preview.get("source_evidence_refs")
        if (
            not isinstance(source_refs, list)
            or not source_refs
            or source_refs != details.get("source_evidence_refs")
            or any(ref not in evidence_refs for ref in source_refs)
            or proof.get("evidence_ref") != source_refs[-1]
        ):
            return "Scan preview source evidence is incomplete or mismatched"
        field_pairs = {
            "source_page_count": "source_page_count",
            "normalized_record_count": "normalized_record_count",
            "source_snapshot_sha256": "source_snapshot_sha256",
            "selection_count": "selection_count",
            "selection_sha256": "selection_sha256",
            "batch_count": "batch_count",
            "batch_plan_sha256": "batch_plan_sha256",
        }
        if any(details.get(detail) != preview.get(source) for detail, source in field_pairs.items()):
            return "Scan preview counts or digests do not match its authoritative result"
        observed_at = normalized.meta.get("observed_at")
        evidence = data.get("evidence")
        if (
            preview.get("observed_at") != observed_at
            or not isinstance(evidence, Mapping)
            or evidence.get("observed_at") != observed_at
            or preview.get("pagination_complete") is not True
        ):
            return "Scan preview observation time or pagination proof is invalid"
        return None

    @staticmethod
    def _scan_formal_proof_error(
        *,
        normalized: ToolResult,
        proof: Mapping[str, Any],
        generation_verification: GenerationVerificationContext | None,
    ) -> str | None:
        data = normalized.data
        details = proof.get("details")
        evidence_refs = normalized.meta.get("evidence_refs")
        expected_detail_fields = {
            "phase",
            "preview_revalidation_matched",
            "preview_context_sha256",
            "projection_evidence_ref",
            "projection_record_count",
            "projection_snapshot_sha256",
            "batch_count",
            "scheduled_items",
            "scanned",
            "skipped_signed_count",
            "submit_evidence_refs",
            "verification_evidence_refs",
            "external_write_attempted",
        }
        if (
            data.get("phase") != "formal"
            or data.get("dry_run") is not False
            or not isinstance(details, Mapping)
            or set(details) != expected_detail_fields
            or details.get("phase") != "formal"
            or details.get("preview_revalidation_matched") is not True
            or not isinstance(evidence_refs, list)
            or generation_verification is None
            or generation_verification.requires_write_verification is not True
        ):
            return "Formal scan proof envelope is incomplete"
        preview = data.get("preview_revalidation")
        projection_ref = details.get("projection_evidence_ref")
        submit_refs = details.get("submit_evidence_refs")
        verification_refs = details.get("verification_evidence_refs")
        numeric = {
            "batch_count": details.get("batch_count"),
            "scheduled_items": details.get("scheduled_items"),
            "scanned": details.get("scanned"),
            "skipped_signed_count": details.get("skipped_signed_count"),
        }
        if (
            not isinstance(preview, Mapping)
            or preview.get("verified") is not True
            or details.get("preview_context_sha256") != preview.get("context_sha256")
            or not isinstance(projection_ref, str)
            or not projection_ref
            or projection_ref not in evidence_refs
            or details.get("projection_record_count") != data.get("normalized")
            or details.get("projection_snapshot_sha256")
            != preview.get("source_snapshot_sha256")
            or not isinstance(submit_refs, list)
            or not isinstance(verification_refs, list)
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in numeric.values())
            or numeric["batch_count"] != data.get("batches")
            or numeric["scheduled_items"] != data.get("scheduled_items")
            or numeric["scanned"] != data.get("scanned")
            or numeric["skipped_signed_count"] != data.get("skipped_signed_count")
            or numeric["scheduled_items"]
            != numeric["scanned"] + numeric["skipped_signed_count"]
        ):
            return "Formal scan counts, projection, or preview revalidation do not match"
        batch_count = numeric["batch_count"]
        if (
            len(submit_refs) != batch_count
            or len(verification_refs) != batch_count
            or len(set(submit_refs + verification_refs)) != 2 * batch_count
            or any(ref not in evidence_refs for ref in submit_refs + verification_refs)
            or generation_verification.started_mutating_call_count != 1 + batch_count
        ):
            return "Formal scan batch receipts do not match the core write boundary"
        projection_index = evidence_refs.index(projection_ref)
        expected_batch_tail = [
            ref
            for pair in zip(submit_refs, verification_refs, strict=True)
            for ref in pair
        ]
        if (
            len(evidence_refs) != len(set(evidence_refs))
            or projection_index < 1
            or evidence_refs[projection_index + 1 :] != expected_batch_tail
        ):
            return "Formal scan submit and verification evidence order is invalid"
        if batch_count:
            if (
                details.get("external_write_attempted") is not True
                or proof.get("evidence_ref") != verification_refs[-1]
            ):
                return "Formal scan does not end at the last server-ledger verification"
        elif (
            details.get("external_write_attempted") is not False
            or submit_refs
            or verification_refs
            or proof.get("evidence_ref") != projection_ref
        ):
            return "Zero-candidate scan proof contains a false external write"
        evidence = data.get("evidence")
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("observed_at") != normalized.meta.get("observed_at")
        ):
            return "Formal scan observation time is inconsistent"
        return None

    def _finalize_generation_write(
        self,
        step: PlanStep,
        raw_result: Mapping[str, Any],
        outcome: VerificationOutcome,
        verification: GenerationVerificationContext | None,
    ) -> VerificationOutcome:
        if verification is None:
            return outcome
        if not isinstance(verification, GenerationVerificationContext):
            return self._failure(
                "GENERATION_LEASE_INVALID",
                "Plugin generation verification metadata is incomplete",
            )
        if verification.orchestration_run_id is not None and (
            not verification.orchestration_run_id
            or len(verification.orchestration_run_id) > 191
        ):
            return self._failure(
                "GENERATION_LEASE_INVALID",
                "Plugin generation verification metadata is incomplete",
            )
        try:
            uuid.UUID(verification.lease_id)
        except (ValueError, TypeError, AttributeError):
            return self._failure(
                "GENERATION_LEASE_INVALID",
                "Plugin generation verification metadata is incomplete",
            )
        if not verification.automation_id or verification.generation <= 0:
            return self._failure(
                "GENERATION_LEASE_INVALID",
                "Plugin generation verification metadata is incomplete",
            )
        if (
            len(verification.account_bindings_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in verification.account_bindings_sha256
            )
            or any(not account_id for account_id in verification.account_ids)
            or len(verification.account_ids) != len(set(verification.account_ids))
        ):
            return self._failure(
                "GENERATION_LEASE_INVALID",
                "Plugin generation verification metadata is incomplete",
            )
        is_write = step.operation_type.value not in {"read", "compute"}
        if is_write != verification.requires_write_verification:
            return self._failure(
                "GENERATION_LEASE_INVALID",
                "Plugin generation lease kind does not match the planned operation",
            )
        if not is_write:
            if verification.started_mutating_call_count != 0:
                return self._failure(
                    "GENERATION_READ_WRITE_BOUNDARY_INVALID",
                    "Plugin read execution has no authoritative zero-write proof",
                )
            return outcome
        if self._generation_leases is None:
            return self._failure(
                "GENERATION_VERIFIER_UNAVAILABLE",
                "Plugin write lease cannot be finalized without its generation repository",
            )
        final_outcome = (
            RuntimeLeaseOutcome.WRITE_VERIFIED
            if outcome.accepted
            else RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
        )
        evidence_value = outcome.result.to_dict() if outcome.result is not None else dict(raw_result)
        try:
            self._generation_leases.finalize_generation_write(
                automation_id=verification.automation_id,
                generation=verification.generation,
                lease_id=verification.lease_id,
                outcome=final_outcome,
                evidence_sha256=sha256_json(evidence_value),
            )
        except Exception as exc:
            # The adapter already crossed the signed write boundary and the
            # lease remains VERIFYING. A persistence outage here is therefore
            # an unknown external outcome, never a terminal/retryable result.
            return VerificationOutcome(
                False,
                RunStatus.BLOCKED_DATA,
                "WRITE_OUTCOME_UNKNOWN",
                "Plugin generation write finalization could not be persisted "
                f"({type(exc).__name__}: {redact_text(exc)[:300]})",
            )
        if (
            self._migration_runtime is not None
            and verification.orchestration_run_id is not None
        ):
            try:
                claim = self._migration_runtime.find_claim_for_execution(
                    verification.automation_id,
                    verification.orchestration_run_id,
                )
                self._migration_runtime.settle_after_write_verification(
                    claim,
                    final_outcome.value,
                    now=datetime.now(timezone.utc),
                )
            except Exception as exc:
                return VerificationOutcome(
                    False,
                    RunStatus.BLOCKED_DATA,
                    "PLUGIN_MIGRATION_SETTLEMENT_UNAVAILABLE",
                    "Plugin migration write settlement could not be persisted "
                    f"({type(exc).__name__}: {redact_text(exc)[:300]})",
                )
        return outcome

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
        if code == "WRITE_OUTCOME_UNKNOWN":
            # A durable started-write boundary is never replayable.  The
            # original plugin diagnostic is deliberately subordinate to this
            # governing status.
            status = RunStatus.BLOCKED_DATA
        elif code in {"CANCELLED", "CANCELED"}:
            status = RunStatus.CANCELLED
        elif governed_blocked_status == RunStatus.BLOCKED_LOGIN.value or code in {
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
