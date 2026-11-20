"""Signed Console API for 业务手册 → 计算规则 (block-built fee rules) and → 报价 (internal quotes).

The request middleware already checked the live identity against
``shared.identity_routes.agent_permission``: the 业务手册 module grants reading
and editing (create a fee, save, tick checks, submit, withdraw, copy, trial);
approve and retire need a super administrator, re-checked here. Quoting only
needs the module to be visible: it reads approved rules and keeps a quote record.
Blocks and calculation live in ``shared.fee_engine``, versions and approval in
``shared.handbook_fee_rules``, storage in ``shared.fee_rule_repository``.
"""
from __future__ import annotations

import base64
import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from shared.contracts import api_failure, api_success
from shared.fee_engine import boundary_rows, calculate, quote, shared_params
from shared.handbook_fee_rules import FeeRuleError, quote_catalog, rule_from_record
from shared.redaction import redact_text

_STATUS = {"FEE_RULE_NOT_FOUND": 404, "FEE_RULE_CONFLICT": 409, "FEE_RULE_STATE": 409, "FEE_RULE_NOT_APPROVABLE": 409,
           "FEE_RULE_NOT_SUBMITTABLE": 409, "FEE_RULE_NAME_TAKEN": 409, "FEE_RULE_CHECKS_STALE": 409,
           "FEE_RULE_SOURCE_PAGE_UNAVAILABLE": 503, "FEE_DRAFT_BUSY": 409, "FEE_DRAFT_NOT_FOUND": 404,
           "FEE_DRAFT_NOT_READY": 409, "FEE_DRAFT_UNAVAILABLE": 503, "FEE_DRAFT_READ_FAILED": 503,
           "FEE_RULE_DATE_TAKEN": 409, "FEE_RULES_UNAVAILABLE": 503, "WAYBILL_LOOKUP_UNAVAILABLE": 503}
_TRACKING = re.compile(r"^[A-Za-z0-9-]{6,40}$")


class RowVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_version: int


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fee_name: str


class SaveRequest(RowVersion):
    fee_name: str | None = None
    version_label: str
    effective_from: str | None = None
    content: dict[str, Any]
    checks: list[dict[str, Any]]


class ChecksRequest(RowVersion):
    checks: list[dict[str, Any]]


class EffectiveDatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_from: str
    versions: list[dict[str, Any]] = Field(min_length=1, max_length=50)


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any] = Field(default_factory=dict)
    boundary: bool = False


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    billing_date: str
    fee_kinds: list[str] = Field(min_length=1, max_length=30)
    values: dict[str, Any] = Field(default_factory=dict)
    tracking_number: str | None = None


class WaybillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracking_number: str


def _failure(exc: FeeRuleError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS.get(exc.code, 422), content=api_failure(exc.code, redact_text(exc.message)))


def _date(value: str | None) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise FeeRuleError("FEE_QUOTE_DATE_INVALID", "开单日期格式无效。") from exc


def _trial_detail(values: dict[str, Any], result: dict[str, Any], boundary: bool) -> dict[str, Any]:
    """What the operation log keeps of a trial: the values typed (short) and the outcome."""
    shown = {key: value for key, value in values.items() if not isinstance(value, (list, dict))}
    pieces = sum(len(value) for value in values.values() if isinstance(value, list))
    return {"values": dict(list(shown.items())[:8]), "piece_rows": pieces, "outcome": result["outcome"],
            "amount": result["amount"], "boundary": boundary}


def create_fee_rules_router(*, repository_provider: Callable[[], Any], admin_provider: Callable[[Request], Any],
                            super_admin_provider: Callable[[Request], Any],
                            source_page_provider: Callable[[str, int], bytes] | None = None) -> APIRouter:
    """``source_page_provider(document title, page)`` gives a manual page as PNG for 核对 (composed in main)."""
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

    @router.post("")
    async def create_fee(req: CreateRequest, request: Request):
        actor = admin_provider(request)
        return await run(lambda: {"rule_id": repository().create_fee(fee_name=req.fee_name, actor_id=actor.actor_id)})

    @router.get("/source-page")
    async def source_page(request: Request, document: str = "", page: int = 0):
        """One manual page as a picture, next to the 收费表 lines that cite it."""
        admin_provider(request)

        def render():
            title = document.strip()
            if not title or len(title) > 200 or not 1 <= page <= 2000:
                raise FeeRuleError("FEE_RULE_SOURCE_PAGE_INVALID", "出处缺少文档名称或页码。")
            if source_page_provider is None:
                raise FeeRuleError("FEE_RULE_SOURCE_PAGE_UNAVAILABLE", "手册原页图片当前不可用。")
            try:
                image = source_page_provider(title, page)
            except FeeRuleError:
                raise
            except Exception as exc:  # the provider's own Chinese reason, redacted below
                raise FeeRuleError("FEE_RULE_SOURCE_PAGE_UNAVAILABLE", f"手册原页图片没有取到：{exc}") from exc
            return {"document": title, "page": page, "png_base64": base64.b64encode(image).decode("ascii")}

        return await run(render)

    @router.post("/effective-dates")
    async def effective_dates(req: EffectiveDatesRequest, request: Request):
        actor = admin_provider(request)
        return await run(lambda: {"updated": repository().set_effective_dates(
            req.versions, effective_from=req.effective_from, actor_id=actor.actor_id)})

    @router.get("/{rule_id}")
    async def get_version(rule_id: str, request: Request):
        admin_provider(request)

        def read():
            record = repository().get(rule_id)
            try:
                rule = rule_from_record(record)
                record["params"] = [param.as_dict() for param in rule.content.params]
                record["content_problems"] = []
            except FeeRuleError as exc:
                record["params"] = []
                record["content_problems"] = exc.message.split("；")
            return record

        return await run(read)

    @router.post("/{rule_id}/copy")
    async def copy(rule_id: str, request: Request):
        actor = admin_provider(request)
        return await run(lambda: {"rule_id": repository().copy_to_draft(rule_id, actor_id=actor.actor_id)})

    @router.post("/{rule_id}/save")
    async def save(rule_id: str, req: SaveRequest, request: Request):
        actor = admin_provider(request)
        return await run(lambda: repository().save_draft(
            rule_id, version_label=req.version_label, effective_from=req.effective_from, content=req.content,
            checks=req.checks, fee_name=req.fee_name, actor_id=actor.actor_id,
            expected_row_version=req.row_version) or {"saved": True})

    @router.post("/{rule_id}/checks")
    async def checks(rule_id: str, req: ChecksRequest, request: Request):
        actor = admin_provider(request)
        return await run(lambda: {"saved": True, "row_version": repository().set_checks(
            rule_id, checks=req.checks, actor_id=actor.actor_id, expected_row_version=req.row_version)})

    @router.post("/{rule_id}/trial")
    async def trial(rule_id: str, req: TrialRequest, request: Request):
        actor = admin_provider(request)

        def calculate_saved():
            rule = rule_from_record(repository().get(rule_id))
            result = calculate(rule.content, req.values).as_dict()
            result.update(rule_version=rule.version, rule_status=rule.status, reportable=rule.reportable)
            data = {"result": result}
            if req.boundary:
                data["boundary"] = boundary_rows(rule.content, req.values)
            repository().record_trial(rule_id, actor_id=actor.actor_id, detail=_trial_detail(req.values, result, req.boundary))
            data["trial_count"] = repository().get(rule_id)["trial_count"]
            return data

        return await run(calculate_saved)

    @router.post("/{rule_id}/{action}")
    async def change(rule_id: str, action: str, req: RowVersion, request: Request):
        if action not in {"submit", "withdraw", "approve", "retire"}:
            return JSONResponse(status_code=404, content=api_failure("NOT_FOUND", "不支持的操作"))
        actor = (super_admin_provider if action in {"approve", "retire"} else admin_provider)(request)
        return await run(lambda: getattr(repository(), action)(
            rule_id, actor_id=actor.actor_id, expected_row_version=req.row_version) or {"action": action})

    return router


def create_fee_quotes_router(*, repository_provider: Callable[[], Any], admin_provider: Callable[[Request], Any],
                             waybill_reader: Callable[[str, Any], Awaitable[dict[str, Any]]] | None = None) -> APIRouter:
    """业务手册 → 报价: approved rules only, chosen by billing date; every quote is kept."""
    router = APIRouter(prefix="/internal/v1/admin/fee-quotes", route_class=EnvelopedRoute)

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

    @router.get("/catalog")
    async def catalog(request: Request, billing_date: str | None = None):
        admin_provider(request)

        def read():
            when = _date(billing_date) if billing_date else date.today()
            rules, unavailable = quote_catalog(repository().approved(), business_date=when)
            fees = [{"fee_kind": item.fee_kind, "name": item.name, "version": item.version, "group": item.rule.group,
                     "params": [param.key for param in item.rule.params], "reminders": list(item.rule.reminders)}
                    for item in rules]
            try:
                params = [param.as_dict() for param in shared_params(rules)]
                conflict = ""
            except FeeRuleError as exc:
                params, conflict = [], exc.message
            return {"billing_date": when.isoformat(), "fees": fees, "params": params, "unavailable": unavailable,
                    "conflict": conflict}

        return await run(read)

    @router.post("")
    async def make_quote(req: QuoteRequest, request: Request):
        actor = admin_provider(request)

        def compute():
            when = _date(req.billing_date)
            rules, unavailable = quote_catalog(repository().approved(), business_date=when)
            by_kind = {item.fee_kind: item for item in rules}
            chosen = [by_kind[kind] for kind in dict.fromkeys(req.fee_kinds) if kind in by_kind]
            skipped = [item for item in unavailable if item["fee_kind"] in req.fee_kinds]
            unknown = [kind for kind in req.fee_kinds if kind not in by_kind and kind not in {i["fee_kind"] for i in skipped}]
            if unknown:
                raise FeeRuleError("FEE_QUOTE_UNKNOWN_FEE", "选择的费用不存在或已作废，请刷新后重选。")
            if not chosen:
                raise FeeRuleError("FEE_QUOTE_NOTHING", "所选费用在这个开单日期都没有已生效的规则："
                                   + "；".join(f"{item['name']}：{item['reason']}" for item in skipped))
            result = quote(chosen, req.values).as_dict()
            result["unavailable"] = skipped
            result["billing_date"] = when.isoformat()
            tracking = req.tracking_number if req.tracking_number and _TRACKING.fullmatch(req.tracking_number) else None
            result["quote_id"] = repository().record_quote(
                actor_id=actor.actor_id, channel="console", billing_date=when, fee_kinds=[item.fee_kind for item in chosen],
                values=req.values, result=result, tracking_number=tracking)
            return result

        return await run(compute)

    @router.get("/recent")
    async def recent(request: Request):
        admin_provider(request)
        return await run(lambda: {"quotes": repository().recent_quotes(limit=20)})

    @router.post("/waybill")
    async def waybill(req: WaybillRequest, request: Request):
        actor = admin_provider(request)
        tracking = req.tracking_number.strip().upper()
        if not _TRACKING.fullmatch(tracking):
            return _failure(FeeRuleError("FEE_QUOTE_TRACKING_INVALID", "运单号格式不对。"))
        if waybill_reader is None:
            return _failure(FeeRuleError("WAYBILL_LOOKUP_UNAVAILABLE", "运单预填当前不可用，请手动填写。"))
        try:
            return api_success(await waybill_reader(tracking, actor))
        except FeeRuleError as exc:
            return _failure(exc)

    return router


__all__ = ["create_fee_quotes_router", "create_fee_rules_router"]
