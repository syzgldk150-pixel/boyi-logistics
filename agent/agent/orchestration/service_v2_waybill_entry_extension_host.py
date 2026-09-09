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
from agent.orchestration.models import Actor, ActorType, OrchestrationError
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


_RESULT_SUMMARY_FIELDS = frozenset({"status", "data", "meta", "warnings", "error"})


class ServiceV2WaybillEntryExtensionHost:
    """Render and invoke only the two fixed waybill-entry module slots."""

    def __init__(
        self,
        *,
        policy_service: AutomationProjectPolicyService,
        contribution_registry: Any,
        validator_timeout_seconds: float = 30.0,
    ) -> None:
        self._policy = policy_service
        self._registry = contribution_registry
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
        invocation_id = str(receipt.get("invocation_id") or "") if isinstance(receipt, Mapping) else ""
        if not invocation_id:
            raise OrchestrationError("WAYBILL_EXTENSION_RESULT_INVALID", "Waybill-entry invocation identity is missing")
        result = await self._policy.direct_invocations.wait(
            invocation_id, timeout_seconds=self._validator_timeout_seconds)
        if result.get("running"):
            await self._policy.direct_invocations.cancel(invocation_id)
            raise OrchestrationError("WAYBILL_EXTENSION_TIMEOUT", "Waybill-entry extension did not complete in time")
        summary = self._summary_from_invocation(result)
        if safe_slot == WAYBILL_ENTRY_ACTIONS_SLOT:
            return {"kind": "action", "result": dict(summary["data"])}
        try:
            validation = normalize_waybill_entry_validator_result(summary.get("data"))
        except ValueError as exc:
            raise OrchestrationError("WAYBILL_EXTENSION_RESULT_INVALID", "Waybill-entry validator result is invalid") from exc
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
    def _summary_from_invocation(invocation: Any) -> Mapping[str, Any]:
        if not isinstance(invocation, Mapping) or invocation.get("status") != "COMPLETED":
            raise OrchestrationError("WAYBILL_EXTENSION_EXECUTION_FAILED", "Waybill-entry extension did not complete successfully")
        summary = invocation.get("result")
        if (not isinstance(summary, Mapping) or set(summary) != _RESULT_SUMMARY_FIELDS
                or summary.get("status") != "SUCCESS" or summary.get("error") is not None
                or not isinstance(summary.get("data"), Mapping)):
            raise OrchestrationError("WAYBILL_EXTENSION_RESULT_INVALID", "Waybill-entry extension result is invalid")
        return summary


__all__ = ["ServiceV2WaybillEntryExtensionHost"]
