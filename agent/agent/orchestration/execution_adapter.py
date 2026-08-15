"""Injected adapter from governed plan steps to existing tool runtimes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from agent.orchestration.models import OperationType, OrchestrationError, PlanStep, sha256_json
from agent.automation_plugins.models import GenerationBoundResult
from agent.execution_boundary import execution_capability_scope
from agent.tool_executor import build_trusted_scheduler_context
from shared.redaction import redact_sensitive, redact_text


class RegisteredToolExecutionAdapter:
    """Run subprocess or in-process tools without making business-success claims."""

    def __init__(
        self,
        *,
        catalog: Any,
        executor: Any,
        direct_runners: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        reconcilers: Mapping[str, Callable[[dict[str, Any]], Mapping[str, Any]]] | None = None,
    ) -> None:
        self._catalog = catalog
        self._executor = executor
        self._direct_runners = dict(direct_runners or {})
        self._reconcilers = dict(reconcilers or {})
        self._step_to_tool: dict[tuple[str, str], tuple[str, str]] = {}

    async def execute_step(
        self,
        step: PlanStep,
        *,
        run_id: str,
        step_id: str,
        execution_context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        capability = self._catalog.get_capability(step.tool_name)
        if capability is None:
            raise OrchestrationError("UNKNOWN_TOOL", f"Unknown tool: {step.tool_name}")
        if str(capability.get("version") or "") != step.tool_version:
            raise OrchestrationError("TOOL_VERSION_CHANGED", f"Tool version changed: {step.tool_name}")
        self._step_to_tool[(run_id, step_id)] = (step.tool_name, "")
        started = time.monotonic()
        direct_runner = self._direct_runners.get(step.tool_name)
        try:
            if direct_runner is not None:
                timeout = max(1, int(capability.get("timeout") or 60))
                with execution_capability_scope(step.tool_name, ttl_seconds=timeout + 30):
                    raw = await asyncio.to_thread(direct_runner, dict(step.arguments))
                process_result: Mapping[str, Any] = {
                    "success": not (isinstance(raw, Mapping) and (raw.get("error") or raw.get("ok") is False)),
                    "data": raw,
                    "duration_s": round(time.monotonic() - started, 3),
                }
            else:
                trusted_context = build_trusted_scheduler_context(
                    step.tool_name,
                    execution_context,
                )
                is_plugin = isinstance(capability.get("_plugin_runtime"), Mapping)
                if is_plugin:
                    project_invocation = execution_context.get(
                        "_automation_project_invocation"
                    )
                    if not isinstance(project_invocation, Mapping):
                        raise OrchestrationError(
                            "PROJECT_INVOCATION_REQUIRED",
                            "Automation plugin execution requires a trusted project invocation",
                        )
                    process_result = await self._executor.execute(
                        capability,
                        dict(step.arguments),
                        trusted_scheduler_context=trusted_context,
                        trusted_invocation_context={
                            "run_id": run_id,
                            "step_id": step_id,
                            "_automation_project_invocation": dict(project_invocation),
                        },
                    )
                elif trusted_context is None:
                    process_result = await self._executor.execute(capability, dict(step.arguments))
                else:
                    process_result = await self._executor.execute(
                        capability,
                        dict(step.arguments),
                        trusted_scheduler_context=trusted_context,
                    )
                info = self._executor.running_tool_info(step.tool_name)
                self._step_to_tool[(run_id, step_id)] = (step.tool_name, str(info.get("started_at") or ""))
        except Exception as exc:
            return {
                "status": "FAILED",
                "data": {},
                "meta": self._base_meta(step, capability, execution_context),
                "warnings": [],
                "error": {
                    "code": type(exc).__name__.upper(),
                    "message": redact_text(exc)[:500],
                    "retryable": False,
                },
            }
        finally:
            self._step_to_tool.pop((run_id, step_id), None)
        normalized = self._normalize_process_result(
            step,
            capability,
            execution_context,
            process_result,
        )
        verification = getattr(process_result, "generation_verification", None)
        if verification is not None:
            return GenerationBoundResult(normalized, verification=verification)
        return normalized

    async def cancel_step(self, *, run_id: str, step_id: str) -> Mapping[str, Any]:
        identity = self._step_to_tool.get((run_id, step_id))
        if identity is None:
            return {"ok": False, "code": "NOT_RUNNING", "message": "The step is not running"}
        tool_name, started_at = identity
        cancel_bound = getattr(self._executor, "cancel_bound_run", None)
        if callable(cancel_bound):
            bound_result = await cancel_bound(
                tool_name=tool_name,
                run_id=run_id,
                step_id=step_id,
            )
            if bound_result.get("code") != "NOT_RUNNING":
                return bound_result
        return await self._executor.cancel_tool(tool_name, started_at=started_at)

    async def reconcile_step(
        self,
        step: PlanStep,
        *,
        run_id: str,
        step_id: str,
        persisted_step: Mapping[str, Any],
        execution_context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Read the target system after an interrupted write; never infer success."""

        del run_id, step_id, persisted_step
        capability = self._catalog.get_capability(step.tool_name)
        if capability is None or str(capability.get("version") or "") != step.tool_version:
            return {"resolution": "UNKNOWN", "code": "TOOL_VERSION_CHANGED"}
        reconciler = self._reconcilers.get(step.tool_name)
        if reconciler is None:
            return {"resolution": "UNSUPPORTED", "code": "RECONCILER_NOT_CONFIGURED"}
        timeout = max(1, int(capability.get("timeout") or 60))
        try:
            with execution_capability_scope(step.tool_name, ttl_seconds=timeout + 30):
                raw = await asyncio.wait_for(
                    asyncio.to_thread(reconciler, dict(step.arguments)),
                    timeout=timeout,
                )
        except Exception as exc:
            return {
                "resolution": "UNKNOWN",
                "code": type(exc).__name__.upper(),
                "message": redact_text(exc)[:500],
            }
        if not isinstance(raw, Mapping):
            return {"resolution": "UNKNOWN", "code": "INVALID_RECONCILIATION_RESULT"}
        resolution = str(raw.get("resolution") or "UNKNOWN").strip().upper()
        if resolution not in {"APPLIED", "NOT_APPLIED", "UNKNOWN"}:
            resolution = "UNKNOWN"
        result = raw.get("result")
        normalized_result: Mapping[str, Any] | None = None
        if isinstance(result, Mapping):
            normalized_result = (
                redact_sensitive(dict(result))
                if _is_unified_result(result)
                else self._normalize_process_result(step, capability, execution_context, result)
            )
        return redact_sensitive(
            {
                "resolution": resolution,
                "code": str(raw.get("code") or ""),
                "message": str(raw.get("message") or ""),
                "result": normalized_result,
            }
        )

    def _normalize_process_result(
        self,
        step: PlanStep,
        capability: Mapping[str, Any],
        execution_context: Mapping[str, Any],
        process_result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not isinstance(process_result, Mapping):
            return self._failed_contract(step, capability, execution_context, "Tool process returned a non-object result")
        if _is_unified_result(process_result):
            return redact_sensitive(dict(process_result))
        payload = process_result.get("data")
        if not isinstance(payload, Mapping):
            payload = {}
        if process_result.get("canceled") is True or process_result.get("cancelled") is True:
            return {
                "status": "FAILED",
                "data": redact_sensitive(dict(payload)),
                "meta": self._base_meta(step, capability, execution_context),
                "warnings": [],
                "error": {
                    "code": "CANCELLED",
                    "message": "Tool execution was cancelled",
                    "retryable": False,
                },
            }
        if _is_unified_result(payload):
            return redact_sensitive(dict(payload))
        nested_ok = payload.get("ok")
        nested_success = payload.get("success")
        nested_status = str(payload.get("status") or "").lower()
        process_success = process_result.get("success") is True
        if (
            not process_success
            or nested_ok is False
            or nested_success is False
            or nested_status in {"failed", "error", "auth_required"}
            or payload.get("error")
        ):
            code = str(
                process_result.get("error_code")
                or payload.get("error_code")
                or ("LOGIN_REQUIRED" if nested_status == "auth_required" else "TOOL_EXECUTION_FAILED")
            ).upper()
            message = str(process_result.get("error") or payload.get("error") or payload.get("message") or "Tool execution failed")
            nested_error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
            retryable = bool(
                process_result.get("retryable")
                or nested_error.get("retryable")
            )
            return {
                "status": "FAILED",
                "data": redact_sensitive(dict(payload)),
                "meta": self._base_meta(step, capability, execution_context),
                "warnings": [],
                "error": {
                    "code": code,
                    "message": redact_text(message)[:500],
                    "retryable": retryable,
                },
            }

        meta = self._base_meta(step, capability, execution_context)
        actual_meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
        meta.update(redact_sensitive(dict(actual_meta)))
        record_count = _actual_record_count(payload)
        meta["record_count"] = record_count if record_count is not None else (1 if payload else 0)
        pagination_complete = _actual_pagination_complete(payload, capability)
        meta["pagination_complete"] = (
            pagination_complete
            if pagination_complete is not None
            else not _requires_complete_pagination(capability)
        )
        evidence_refs = payload.get("evidence_refs")
        if isinstance(evidence_refs, list):
            meta["evidence_refs"] = redact_sensitive(evidence_refs)
        else:
            meta["evidence_refs"] = [_result_evidence_ref(step.tool_name, payload)]
        postconditions = payload.get("postconditions")
        if isinstance(postconditions, Mapping):
            meta["postconditions"] = dict(postconditions)
        postcondition_evidence = payload.get("postcondition_evidence")
        if isinstance(postcondition_evidence, Mapping):
            meta["postcondition_evidence"] = redact_sensitive(dict(postcondition_evidence))

        requirements = _postcondition_requirements(capability)
        if (
            step.operation_type not in {OperationType.READ, OperationType.COMPUTE}
            and len(requirements) == 1
            and requirements[0].get("name") == "executor_reported_success"
            and (nested_ok is True or nested_success is True)
        ):
            result_digest = _result_evidence_digest(payload)
            evidence_ref = f"tool-result:{step.tool_name}:{result_digest}"
            if evidence_ref not in meta["evidence_refs"]:
                meta["evidence_refs"].append(evidence_ref)
            meta["postconditions"] = {"0": True}
            meta["postcondition_evidence"] = {
                "0": {
                    "condition": "executor_reported_success",
                    "verified": True,
                    "observed_at": meta["observed_at"],
                    "evidence_ref": evidence_ref,
                    "details": {"result_sha256": result_digest},
                }
            }
        return {
            "status": "SUCCESS",
            "data": redact_sensitive(dict(payload)),
            "meta": meta,
            "warnings": list(payload.get("warnings") or []) if isinstance(payload.get("warnings"), list) else [],
            "error": None,
        }

    @staticmethod
    def _base_meta(
        step: PlanStep,
        capability: Mapping[str, Any],
        execution_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        evidence = capability.get("evidence") or []
        requirements = [evidence] if isinstance(evidence, Mapping) else evidence
        source_systems = {
            str(item.get("source_system") or "").strip()
            for item in requirements
            if isinstance(item, Mapping) and str(item.get("source_system") or "").strip()
        }
        source_system = next(iter(source_systems)) if len(source_systems) == 1 else ""
        return {
            "source_system": source_system,
            "account_id": step.account_id or "",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "execution_source": str(execution_context.get("source") or ""),
        }

    def _failed_contract(
        self,
        step: PlanStep,
        capability: Mapping[str, Any],
        execution_context: Mapping[str, Any],
        message: str,
    ) -> Mapping[str, Any]:
        return {
            "status": "FAILED",
            "data": {},
            "meta": self._base_meta(step, capability, execution_context),
            "warnings": [],
            "error": {"code": "INVALID_RESULT_CONTRACT", "message": message, "retryable": False},
        }


def _is_unified_result(value: Mapping[str, Any]) -> bool:
    return all(key in value for key in ("status", "data", "meta", "warnings", "error"))


def _actual_record_count(payload: Mapping[str, Any]) -> int | None:
    meta = payload.get("meta")
    if isinstance(meta, Mapping) and isinstance(meta.get("record_count"), int):
        return int(meta["record_count"])
    for key in ("rows", "records", "items"):
        if isinstance(payload.get(key), list):
            return len(payload[key])
    for key in ("fetched", "record_count", "count"):
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _actual_pagination_complete(
    payload: Mapping[str, Any],
    capability: Mapping[str, Any],
) -> bool | None:
    meta = payload.get("meta")
    if isinstance(meta, Mapping) and isinstance(meta.get("pagination_complete"), bool):
        return bool(meta["pagination_complete"])
    if isinstance(payload.get("pagination_complete"), bool):
        return bool(payload["pagination_complete"])
    evidence = capability.get("evidence") or []
    requirements = [evidence] if isinstance(evidence, Mapping) else evidence
    if isinstance(requirements, list) and requirements and all(
        isinstance(item, Mapping) and item.get("pagination_complete") is False for item in requirements
    ):
        return True
    return None


def _requires_complete_pagination(capability: Mapping[str, Any]) -> bool:
    evidence = capability.get("evidence") or []
    requirements = [evidence] if isinstance(evidence, Mapping) else evidence
    return bool(
        isinstance(requirements, list)
        and any(
            isinstance(item, Mapping)
            and (
                item.get("pagination_complete") is True
                or "pagination_complete" in (item.get("required_fields") or [])
            )
            for item in requirements
        )
    )


def _postcondition_requirements(capability: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = capability.get("postconditions") or []
    requirements = [value] if isinstance(value, Mapping) else value
    return [item for item in requirements if isinstance(item, Mapping)] if isinstance(requirements, list) else []


def _result_evidence_ref(tool_name: str, payload: Mapping[str, Any]) -> str:
    return f"tool-result:{tool_name}:{_result_evidence_digest(payload)}"


def _result_evidence_digest(payload: Mapping[str, Any]) -> str:
    return sha256_json(redact_sensitive(dict(payload)))
