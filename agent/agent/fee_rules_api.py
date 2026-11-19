"""Signed Console API for 业务手册 → 计算规则 (fee rules managed in the back office).

The request middleware already checked the live identity against
``shared.identity_routes.agent_permission``: the 业务手册 module grants reading
and editing (save, tick checks, submit, withdraw, copy, trial); approve and
retire need a super administrator, re-checked here. Rules and validation live
in ``shared.handbook_fee_rules``; storage in ``shared.fee_rule_repository``.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from shared.contracts import api_failure, api_success
from shared.handbook_fee_rules import (
    FeeRuleError, UpstairsInputs, boundary_table, calculate_upstairs, result_dict, rule_from_record,
)
from shared.redaction import redact_text

_STATUS = {"FEE_RULE_NOT_FOUND": 404, "FEE_RULE_CONFLICT": 409, "FEE_RULE_STATE": 409, "FEE_RULE_NOT_APPROVABLE": 409,
           "FEE_RULE_NOT_SUBMITTABLE": 409,
           "FEE_RULE_DATE_TAKEN": 409, "FEE_RULES_UNAVAILABLE": 503}


class RowVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int


class SaveRequest(RowVersion):
    version_label: str
    effective_from: str | None = None
    content: dict[str, Any]
    checks: list[dict[str, Any]]


class ChecksRequest(RowVersion):
    checks: list[dict[str, Any]]


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settlement_weight_kg: str
    elevator: bool
    floor: int
    goods_type: str = "general"
    piece_counts: list[int] = []
    pieces_over_limit: int = 0
    total_pieces: int | None = None
    boundary: bool = False


def _failure(exc: FeeRuleError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS.get(exc.code, 422), content=api_failure(exc.code, redact_text(exc.message)))


def create_fee_rules_router(*, repository_provider: Callable[[], Any], admin_provider: Callable[[Request], Any],
                            super_admin_provider: Callable[[Request], Any]) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/admin/fee-rules", route_class=EnvelopedRoute)

    def repository():
        provided = repository_provider()
        if provided is None:
            raise FeeRuleError("FEE_RULES_UNAVAILABLE", "计算规则当前不可用。")
        return provided

    async def run(call):
        try:
            return api_success(await run_in_threadpool(call))
        except FeeRuleError as exc:
            return _failure(exc)

    @router.get("")
    async def list_versions(request: Request):
        admin_provider(request)
        return await run(lambda: {"versions": repository().list_versions(include_retired=True)})

    @router.get("/{rule_id}")
    async def get_version(rule_id: str, request: Request):
        admin_provider(request)
        return await run(lambda: repository().get(rule_id))

    @router.post("/{rule_id}/copy")
    async def copy(rule_id: str, request: Request):
        actor = admin_provider(request)
        return await run(lambda: {"rule_id": repository().copy_to_draft(rule_id, actor_id=actor.actor_id)})

    @router.post("/{rule_id}/save")
    async def save(rule_id: str, req: SaveRequest, request: Request):
        actor = admin_provider(request)
        return await run(lambda: repository().save_draft(
            rule_id, version_label=req.version_label, effective_from=req.effective_from, content=req.content,
            checks=req.checks, actor_id=actor.actor_id, expected_row_version=req.row_version) or {"saved": True})

    @router.post("/{rule_id}/checks")
    async def checks(rule_id: str, req: ChecksRequest, request: Request):
        actor = admin_provider(request)
        return await run(lambda: repository().set_checks(
            rule_id, checks=req.checks, actor_id=actor.actor_id, expected_row_version=req.row_version) or {"saved": True})

    @router.post("/{rule_id}/trial")
    async def trial(rule_id: str, req: TrialRequest, request: Request):
        actor = admin_provider(request)

        def calculate():
            rule = rule_from_record(repository().get(rule_id))
            try:
                weight = Decimal(req.settlement_weight_kg.strip())
            except (InvalidOperation, ValueError) as exc:
                raise FeeRuleError("FEE_INPUT_INVALID", "结算重量不是有效数字。") from exc
            result = calculate_upstairs(rule, UpstairsInputs(
                weight, req.elevator, req.floor, req.goods_type, tuple(req.piece_counts), req.pieces_over_limit,
                req.total_pieces))
            data = {"result": result_dict(result)}
            if req.boundary:
                data["boundary"] = boundary_table(rule, weight=weight)
            repository().record_trial(rule_id, actor_id=actor.actor_id, detail={
                "weight": str(weight), "floor": req.floor, "elevator": req.elevator, "outcome": result.outcome,
                "amount": None if result.amount is None else str(result.amount), "boundary": req.boundary})
            data["trial_count"] = repository().get(rule_id)["trial_count"]
            return data

        return await run(calculate)

    @router.post("/{rule_id}/{action}")
    async def change(rule_id: str, action: str, req: RowVersion, request: Request):
        if action not in {"submit", "withdraw", "approve", "retire"}:
            return JSONResponse(status_code=404, content=api_failure("NOT_FOUND", "不支持的操作"))
        actor = (super_admin_provider if action in {"approve", "retire"} else admin_provider)(request)
        return await run(lambda: getattr(repository(), action)(
            rule_id, actor_id=actor.actor_id, expected_row_version=req.row_version) or {"action": action})

    return router


__all__ = ["create_fee_rules_router"]
