"""Closed internal HTTP projection for project-level automation policy.

The signed Console principal is resolved by the composition root.  Browser
payloads never carry actors, approval IDs, plan hashes, plugin manifests, or
runtime generation material.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, UUID4

from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.models import Actor
from agent.orchestration.service_v2_waybill_entry_extension_host import (
    ServiceV2WaybillEntryExtensionHost,
)
from shared.contracts import api_success


class ProjectPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    request_id: str
    comment: str = Field(default="", max_length=500)
    expected_policy_version: int = Field(ge=1)
    expected_project_configuration_version: int = Field(ge=1)


class ProjectPendingDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_pending_set_hash: str
    request_id: str
    comment: str = Field(default="", max_length=500)


class ProjectInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    preview_run_id: str | None = None
    contribution_id: str | None = Field(default=None, max_length=128)


class ProjectSelectionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str


class ProjectSelectionConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    selected_bill_codes: list[str] = Field(min_length=1, max_length=10_000)


class WaybillEntryModuleSlotInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID4
    waybill: dict[str, Any]


def create_automation_project_router(
    *,
    service_provider: Callable[[], AutomationProjectPolicyService],
    actor_provider: Callable[[Request], Actor],
    module_slot_host_provider: (Callable[[], ServiceV2WaybillEntryExtensionHost] | None) = None,
) -> APIRouter:
    """Build routes around injected providers without importing ``main``."""

    router = APIRouter()

    @router.get("/internal/v1/automation-project-policies")
    async def list_project_policies(request: Request) -> dict[str, Any]:
        actor_provider(request)
        return api_success(service_provider().list_policies())

    @router.post(
        "/internal/v1/automation-projects/{automation_id}/approval-policy"
    )
    async def update_project_policy(
        automation_id: str,
        payload: ProjectPolicyUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        return api_success(
            service_provider().update_policy(
                automation_id,
                mode=payload.mode,
                request_id=payload.request_id,
                comment=payload.comment,
                expected_policy_version=payload.expected_policy_version,
                expected_project_configuration_version=(
                    payload.expected_project_configuration_version
                ),
                actor=actor_provider(request),
            )
        )

    @router.get(
        "/internal/v1/automation-projects/{automation_id}/pending-approvals"
    )
    async def get_pending_project_approvals(
        automation_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = actor_provider(request)
        return api_success(
            service_provider().pending_approvals(automation_id, actor=actor)
        )

    async def decide_pending(
        automation_id: str,
        payload: ProjectPendingDecisionRequest,
        request: Request,
        *,
        decision: str,
    ) -> dict[str, Any]:
        return api_success(
            service_provider().decide_pending_approvals(
                automation_id,
                decision=decision,
                expected_pending_set_hash=payload.expected_pending_set_hash,
                request_id=payload.request_id,
                comment=payload.comment,
                actor=actor_provider(request),
            )
        )

    @router.post(
        "/internal/v1/automation-projects/{automation_id}/pending-approvals/approve"
    )
    async def approve_pending_project_approvals(
        automation_id: str,
        payload: ProjectPendingDecisionRequest,
        request: Request,
    ) -> dict[str, Any]:
        return await decide_pending(
            automation_id,
            payload,
            request,
            decision="APPROVED",
        )

    @router.post(
        "/internal/v1/automation-projects/{automation_id}/pending-approvals/reject"
    )
    async def reject_pending_project_approvals(
        automation_id: str,
        payload: ProjectPendingDecisionRequest,
        request: Request,
    ) -> dict[str, Any]:
        return await decide_pending(
            automation_id,
            payload,
            request,
            decision="REJECTED",
        )

    @router.get(
        "/internal/v1/automation-projects/{automation_id}/scan-previews/{preview_run_id}"
    )
    async def get_scan_preview(
        automation_id: str,
        preview_run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor_provider(request)
        return api_success(
            service_provider().get_scan_preview_projection(
                automation_id,
                preview_run_id=preview_run_id,
            )
        )

    @router.post(
        "/internal/v1/automation-projects/{automation_id}/selection-previews"
    )
    async def create_selection_preview(
        automation_id: str,
        payload: ProjectSelectionPreviewRequest,
        request: Request,
    ) -> dict[str, Any]:
        receipt = service_provider().invoke_selection_preview(
            automation_id,
            request_id=payload.request_id,
            actor=actor_provider(request),
        )
        serialized = (
            receipt.to_dict()
            if callable(getattr(receipt, "to_dict", None))
            else receipt
        )
        return api_success(serialized)

    @router.get(
        "/internal/v1/automation-projects/{automation_id}/selection-previews/{preview_run_id}"
    )
    async def get_selection_preview(
        automation_id: str,
        preview_run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        actor_provider(request)
        return api_success(
            service_provider().get_selection_preview_projection(
                automation_id,
                preview_run_id=preview_run_id,
            )
        )

    @router.post(
        "/internal/v1/automation-projects/{automation_id}/selection-previews/{preview_run_id}/confirm"
    )
    async def confirm_selection_preview(
        automation_id: str,
        preview_run_id: str,
        payload: ProjectSelectionConfirmationRequest,
        request: Request,
    ) -> dict[str, Any]:
        receipt = service_provider().confirm_selection_preview(
            automation_id,
            preview_run_id=preview_run_id,
            selected_bill_codes=payload.selected_bill_codes,
            request_id=payload.request_id,
            actor=actor_provider(request),
        )
        serialized = (
            receipt.to_dict()
            if callable(getattr(receipt, "to_dict", None))
            else receipt
        )
        return api_success(serialized)

    @router.post("/internal/v1/automation-projects/{automation_id}/invoke")
    async def invoke_project(
        automation_id: str,
        payload: ProjectInvokeRequest,
        request: Request,
    ) -> dict[str, Any]:
        invocation_arguments: dict[str, Any] = {
            "request_id": payload.request_id,
            "actor": actor_provider(request),
            "preview_run_id": payload.preview_run_id,
        }
        if payload.contribution_id is not None:
            invocation_arguments["contribution_id"] = payload.contribution_id
        receipt = service_provider().invoke_console(
            automation_id,
            **invocation_arguments,
        )
        serialized = receipt.to_dict() if callable(getattr(receipt, "to_dict", None)) else receipt
        return api_success(serialized)

    if module_slot_host_provider is not None:

        @router.get("/internal/v1/automation-projects/module-slots/waybill-entry")
        async def list_waybill_entry_module_slots(
            request: Request,
        ) -> dict[str, Any]:
            actor = actor_provider(request)
            return api_success(module_slot_host_provider().list_module_slots(actor=actor))

        @router.post("/internal/v1/automation-projects/module-slots/waybill-entry/validators/invoke-active")
        async def invoke_active_waybill_entry_validators(
            payload: WaybillEntryModuleSlotInvokeRequest,
            request: Request,
        ) -> dict[str, Any]:
            result = await module_slot_host_provider().invoke_active_validators(
                request_id=str(payload.request_id),
                waybill=payload.waybill,
                actor=actor_provider(request),
            )
            return api_success(result)

        @router.post("/internal/v1/automation-projects/module-slots/waybill-entry/{slot}/{handle}/invoke")
        async def invoke_waybill_entry_module_slot(
            slot: str,
            handle: str,
            payload: WaybillEntryModuleSlotInvokeRequest,
            request: Request,
        ) -> JSONResponse:
            result = await module_slot_host_provider().invoke(
                slot=slot,
                handle=handle,
                request_id=str(payload.request_id),
                waybill=payload.waybill,
                actor=actor_provider(request),
            )
            status_code = 202 if result.get("kind") == "action" else 200
            return JSONResponse(status_code=status_code, content=api_success(result))

    return router


__all__ = [
    "ProjectInvokeRequest",
    "ProjectPendingDecisionRequest",
    "ProjectPolicyUpdateRequest",
    "ProjectSelectionConfirmationRequest",
    "ProjectSelectionPreviewRequest",
    "WaybillEntryModuleSlotInvokeRequest",
    "create_automation_project_router",
]
