"""Host-owned invocation surface for waybill-entry Service v2 extensions."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.automation_project_service_v2 import (
    resolve_active_service_v2_module_slot,
)
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.models import Actor, ActorType, OrchestrationError, RunStatus
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    canonical_sha256,
)
from shared.waybill_entry_extensions import (
    WAYBILL_ENTRY_ACTIONS_SLOT,
    WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD,
    WAYBILL_ENTRY_VALIDATORS_SLOT,
    normalize_waybill_entry_draft,
    normalize_waybill_entry_extension_handle,
    normalize_waybill_entry_slot,
    normalize_waybill_entry_validator_result,
)


_RECEIPT_FIELDS = frozenset(
    {
        "command_id",
        "work_item_id",
        "run_id",
        "status",
        "reused",
        "next_poll_after_ms",
    }
)
_RESULT_SUMMARY_FIELDS = frozenset({"status", "data", "meta", "warnings", "error"})


class ServiceV2WaybillEntryExtensionHost:
    """Render and invoke only the two fixed waybill-entry module slots."""

    def __init__(
        self,
        *,
        policy_service: AutomationProjectPolicyService,
        contribution_registry: Any,
        command_gateway: CommandGateway,
        validator_timeout_seconds: float = 30.0,
    ) -> None:
        self._policy = policy_service
        self._registry = contribution_registry
        self._gateway = command_gateway
        self._validator_timeout_seconds = max(0.1, float(validator_timeout_seconds))

    def list_module_slots(self, *, actor: Actor) -> dict[str, Any]:
        self._require_console_admin(actor)
        return {"module_slots": list(self._module_slot_snapshot())}

    async def invoke_active_validators(
        self,
        *,
        request_id: str,
        waybill: Mapping[str, Any],
        actor: Actor,
    ) -> dict[str, Any]:
        """Run one exact active validator set and reject projection drift."""

        self._require_console_admin(actor)
        try:
            safe_request_id = self._request_uuid(request_id)
            safe_waybill = normalize_waybill_entry_draft(waybill)
        except ValueError as exc:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_REQUEST_INVALID",
                "Waybill-entry validator-set request is invalid",
            ) from exc
        before = self._module_slot_snapshot(expected_slot=WAYBILL_ENTRY_VALIDATORS_SLOT)
        results = await asyncio.gather(
            *(
                self.invoke(
                    slot=WAYBILL_ENTRY_VALIDATORS_SLOT,
                    handle=item["handle"],
                    request_id=safe_request_id,
                    waybill=safe_waybill,
                    actor=actor,
                )
                for item in before
            )
        )
        after = self._module_slot_snapshot(expected_slot=WAYBILL_ENTRY_VALIDATORS_SLOT)
        if after != before:
            raise OrchestrationError(
                "PROJECT_RUNTIME_PROJECTION_STALE",
                "Active waybill-entry validators changed during validation",
            )
        issues: list[object] = []
        valid = True
        for result in results:
            if (
                not isinstance(result, Mapping)
                or set(result) != {"kind", "validation"}
                or result.get("kind") != "validator"
            ):
                raise OrchestrationError(
                    "WAYBILL_EXTENSION_RESULT_INVALID",
                    "Waybill-entry validator-set result is invalid",
                )
            try:
                validation = normalize_waybill_entry_validator_result(result.get("validation"))
            except ValueError as exc:
                raise OrchestrationError(
                    "WAYBILL_EXTENSION_RESULT_INVALID",
                    "Waybill-entry validator-set result is invalid",
                ) from exc
            valid = valid and bool(validation["valid"])
            issues.extend(validation["issues"])
        try:
            validation = normalize_waybill_entry_validator_result({"valid": valid, "issues": issues})
        except ValueError as exc:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_RESULT_INVALID",
                "Waybill-entry validator-set result is invalid",
            ) from exc
        return {"kind": "validator_set", "validation": validation}

    def _module_slot_snapshot(
        self,
        *,
        expected_slot: str | None = None,
    ) -> tuple[dict[str, str], ...]:
        snapshot = getattr(self._registry, "active_module_slot_snapshot", None)
        if not callable(snapshot):
            raise OrchestrationError(
                "WAYBILL_EXTENSION_UNAVAILABLE",
                "Waybill-entry extension projection is unavailable",
            )
        try:
            rows = snapshot(slot=expected_slot) if expected_slot is not None else snapshot()
        except Exception as exc:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_UNAVAILABLE",
                "Waybill-entry extension projection is unavailable",
            ) from exc
        if not isinstance(rows, tuple):
            raise OrchestrationError(
                "WAYBILL_EXTENSION_UNAVAILABLE",
                "Waybill-entry extension projection is invalid",
            )
        result: list[dict[str, str]] = []
        identities: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {
                "slot",
                "handle",
                "title",
            }:
                raise OrchestrationError(
                    "WAYBILL_EXTENSION_UNAVAILABLE",
                    "Waybill-entry extension projection is invalid",
                )
            try:
                slot = normalize_waybill_entry_slot(row.get("slot"))
                handle = normalize_waybill_entry_extension_handle(row.get("handle"))
            except ValueError as exc:
                raise OrchestrationError(
                    "WAYBILL_EXTENSION_UNAVAILABLE",
                    "Waybill-entry extension projection is invalid",
                ) from exc
            title = row.get("title")
            if (
                type(title) is not str
                or not title
                or title != title.strip()
                or len(title) > 120
                or (expected_slot is not None and slot != expected_slot)
                or (slot, handle) in identities
            ):
                raise OrchestrationError(
                    "WAYBILL_EXTENSION_UNAVAILABLE",
                    "Waybill-entry extension projection is invalid",
                )
            identities.add((slot, handle))
            result.append({"slot": slot, "handle": handle, "title": title})
        result.sort(key=lambda item: (item["slot"], item["title"], item["handle"]))
        return tuple(result)

    async def invoke(
        self,
        *,
        slot: str,
        handle: str,
        request_id: str,
        waybill: Mapping[str, Any],
        actor: Actor,
    ) -> dict[str, Any]:
        self._require_console_admin(actor)
        try:
            safe_slot = normalize_waybill_entry_slot(slot)
            safe_handle = normalize_waybill_entry_extension_handle(handle)
            safe_waybill = normalize_waybill_entry_draft(waybill)
            safe_request_id = self._request_uuid(request_id)
        except ValueError as exc:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_REQUEST_INVALID",
                "Waybill-entry extension request is invalid",
            ) from exc
        target = resolve_active_service_v2_module_slot(
            self._registry,
            slot=safe_slot,
            handle=safe_handle,
        )
        automation_id = str(self._target_value(target, "automation_id") or "")
        generation = self._target_value(target, "generation")
        contribution_id = str(self._target_value(target, "contribution_id") or "")
        idempotency_key = "waybill-module-slot:" + canonical_sha256(
            {
                "actor_id": actor.actor_id,
                "slot": safe_slot,
                "handle": safe_handle,
                "request_id": safe_request_id,
            }
        )
        receipt = self._policy.invoke_trusted(
            automation_id,
            entrypoint=AutomationEntrypoint.MODULE_SLOTS,
            request_id=safe_request_id,
            actor=actor,
            trusted_context={
                "module_slot": {"slot": safe_slot, "handle": safe_handle},
                "dynamic_inputs": {WAYBILL_ENTRY_DYNAMIC_ARGUMENT_FIELD: safe_waybill},
            },
            idempotency_key=idempotency_key,
            expected_automation_generation=generation,
            contribution_id=contribution_id,
        )
        if safe_slot == WAYBILL_ENTRY_ACTIONS_SLOT:
            return {"kind": "action", "receipt": self._safe_receipt(receipt)}
        if safe_slot != WAYBILL_ENTRY_VALIDATORS_SLOT:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_REQUEST_INVALID",
                "Waybill-entry extension slot is invalid",
            )
        run_id = str(getattr(receipt, "run_id", "") or "")
        try:
            run = await self._gateway.wait_for_run(
                run_id,
                timeout_seconds=self._validator_timeout_seconds,
            )
        except OrchestrationError as exc:
            if exc.code == "RUN_WAIT_TIMEOUT":
                raise OrchestrationError(
                    "WAYBILL_EXTENSION_TIMEOUT",
                    "Waybill-entry validator did not complete in time",
                ) from exc
            raise
        validation = self._validation_from_run(run)
        return {"kind": "validator", "validation": validation}

    @staticmethod
    def _target_value(target: Any, field: str) -> Any:
        return target.get(field) if isinstance(target, Mapping) else getattr(target, field, None)

    @staticmethod
    def _request_uuid(value: object) -> str:
        if type(value) is not str:
            raise ValueError("request id is invalid")
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("request id is invalid")
        return value

    @staticmethod
    def _require_console_admin(actor: Actor) -> None:
        if (
            actor.actor_type is not ActorType.CONSOLE_ADMIN
            or not {"admin", "super_admin"}.intersection(actor.roles)
            or actor.authenticated_by != "mysql_admin_session"
        ):
            raise OrchestrationError(
                "ACTION_FORBIDDEN",
                "A signed Console administrator is required",
            )

    @staticmethod
    def _safe_receipt(receipt: Any) -> dict[str, Any]:
        raw = receipt.to_dict() if callable(getattr(receipt, "to_dict", None)) else receipt
        if not isinstance(raw, Mapping) or set(raw) != _RECEIPT_FIELDS:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_RESULT_INVALID",
                "Waybill-entry action receipt is invalid",
            )
        identifiers = tuple(
            raw.get(field)
            for field in (
                "command_id",
                "work_item_id",
                "run_id",
            )
        )
        status = raw.get("status")
        next_poll = raw.get("next_poll_after_ms")
        if (
            any(type(value) is not str or not value for value in identifiers)
            or status not in {item.value for item in RunStatus}
            or type(raw.get("reused")) is not bool
            or type(next_poll) is not int
            or next_poll < 0
        ):
            raise OrchestrationError(
                "WAYBILL_EXTENSION_RESULT_INVALID",
                "Waybill-entry action receipt is invalid",
            )
        return {field: raw[field] for field in _RECEIPT_FIELDS}

    @staticmethod
    def _validation_from_run(run: Any) -> dict[str, object]:
        if not isinstance(run, Mapping) or run.get("status") != "COMPLETED":
            raise OrchestrationError(
                "WAYBILL_EXTENSION_EXECUTION_FAILED",
                "Waybill-entry validator did not complete successfully",
            )
        steps = run.get("steps")
        if not isinstance(steps, list) or len(steps) != 1:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_RESULT_INVALID",
                "Waybill-entry validator must produce exactly one result",
            )
        step = steps[0]
        summary = step.get("result_summary_json") if isinstance(step, Mapping) else None
        if (
            not isinstance(step, Mapping)
            or step.get("status") != "COMPLETED"
            or not isinstance(summary, Mapping)
            or set(summary) != _RESULT_SUMMARY_FIELDS
            or summary.get("status") != "SUCCESS"
            or summary.get("error") is not None
        ):
            raise OrchestrationError(
                "WAYBILL_EXTENSION_RESULT_INVALID",
                "Waybill-entry validator result is invalid",
            )
        try:
            return normalize_waybill_entry_validator_result(summary.get("data"))
        except ValueError as exc:
            raise OrchestrationError(
                "WAYBILL_EXTENSION_RESULT_INVALID",
                "Waybill-entry validator result is invalid",
            ) from exc


__all__ = ["ServiceV2WaybillEntryExtensionHost"]
