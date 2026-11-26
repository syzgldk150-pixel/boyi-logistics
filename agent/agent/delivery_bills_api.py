"""Signed Console API for 财务 → 派件账单（邵阳大祥S站，2026-11-24）.

The request middleware already checked the live identity against
``shared.identity_routes.agent_permission`` (``/internal/v1/admin/finance`` → 财务 view for
GET, operate for POST); changing the 派送费 rule or the settings also needs a super
administrator, re-checked here.

A query composes the bill once: the 每日到货表 rows of 邵阳大祥S站 (``arrivals_reader``,
injected because this package may not import ``tools``), the Ronghui 结算明细 of the 大祥S站
account read live through the ordinary business service (``delivery-bill-settlements``,
which also records the account use), the 派送费 rule versions and what people saved
(``shared.finance`` repository). The result is kept in this process for 30 minutes as a
snapshot: confirming, saving 增值费 and revoking name the snapshot and act on rows the
server composed, never on amounts sent by the browser; after each write the snapshot is
rebuilt from the same platform data and fresh database state. Since 2026-11-26 增值费 and
其他收入 come straight from the 融辉 结算明细 (the 结算类型 lists are in the settings) and
配送方式 such as 自提 carry no 派送费; nothing is entered by hand any more.
Rules and arithmetic live in ``shared.finance.delivery_bills``.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from agent.tms_runtime.direct_business import DirectBusinessError
from shared.contracts import api_failure, api_success
from shared.finance.delivery_bills import (
    MAX_BATCH,
    SETTLEMENT_ACCOUNT_ID,
    STATION_NAME,
    bill_range,
    build_bill,
    delivery_fee,
    describe_rule,
    segment_examples,
    parse_rule_form,
    settlement_entries,
    settlement_window,
)
from shared.finance.errors import FinanceError, FinanceValidationError
from shared.finance.models import FinanceActor
from shared.handbook_fee_rules import FeeRuleError
from shared.redaction import redact_text

CHINA_TZ = ZoneInfo("Asia/Shanghai")
SNAPSHOT_SECONDS = 30 * 60
MAX_SNAPSHOTS = 16
SETTLEMENT_TIMEOUT = 85


class _Snapshot:
    def __init__(self, inputs: dict[str, Any]) -> None:
        self.id = str(uuid.uuid4())
        self.created = time.monotonic()
        self.inputs = inputs
        self.bill: dict[str, Any] = {}
        self.lock = threading.Lock()

    def row(self, code: str) -> dict[str, Any]:
        for row in self.bill.get("rows", ()):
            if row["tracking_number"] == code:
                return row
        raise FinanceError("DELIVERY_BILL_NOT_IN_SNAPSHOT", "这单不在本次查询的账单里（融辉未结算或不在日期范围内）。")


class _Snapshots:
    def __init__(self) -> None:
        self._items: OrderedDict[str, _Snapshot] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, snapshot: _Snapshot) -> None:
        with self._lock:
            self._items[snapshot.id] = snapshot
            while len(self._items) > MAX_SNAPSHOTS:
                self._items.popitem(last=False)

    def get(self, snapshot_id: Any) -> _Snapshot:
        with self._lock:
            snapshot = self._items.get(str(snapshot_id or ""))
            if snapshot is None or time.monotonic() - snapshot.created > SNAPSHOT_SECONDS:
                self._items.pop(str(snapshot_id or ""), None)
                raise FinanceError("DELIVERY_SNAPSHOT_EXPIRED", "查询结果已过期（超过 30 分钟或服务重启），请重新查询。")
            return snapshot


def _failure(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content=api_failure(code, redact_text(message)))


def _finance_actor(actor: Any) -> FinanceActor:
    return FinanceActor(str(actor.actor_id), str(getattr(actor, "display_name", "") or ""))


def _today() -> dt.date:
    return dt.datetime.now(CHINA_TZ).date()


def _dict(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise FinanceValidationError("请求内容无效。")
    return body


def _code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if not code or len(code) > 64 or not code.replace("-", "").isalnum():
        raise FinanceValidationError("运单号无效。")
    return code


def create_delivery_bills_router(
    *,
    finance_provider: Callable[[], Any],
    arrivals_reader: Callable[[dt.date, dt.date, dt.date], dict[str, Any]],
    settlement_reader: Callable[[dt.date, dt.date, Any], Awaitable[dict[str, Any]]],
    admin_provider: Callable[[Request], Any],
    super_admin_provider: Callable[[Request], Any],
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/admin/finance/delivery-bills", route_class=EnvelopedRoute)
    snapshots = _Snapshots()

    def finance():
        repository = finance_provider()
        if repository is None:
            raise FinanceError("FINANCE_UNAVAILABLE", "财务服务当前不可用。", http_status=503)
        return repository

    async def run(call, *args):
        try:
            result = call(*args)
            if hasattr(result, "__await__"):
                result = await result
            return api_success(result)
        except FinanceError as exc:
            return _failure(exc.code, exc.message, exc.http_status)
        except FeeRuleError as exc:
            return _failure(exc.code, exc.message, 422)
        except DirectBusinessError as exc:
            return _failure(exc.code, f"原平台读取失败：{exc}", 502)

    def rebuild(snapshot: _Snapshot) -> dict[str, Any]:
        inputs = snapshot.inputs
        repository = finance()
        codes = [item["tracking_number"] for item in inputs["arrivals"]]
        bill = build_bill(start=inputs["start"], end=inputs["end"], arrivals=inputs["arrivals"],
                          settlements=inputs["settlements"], known_arrivals=inputs["known"],
                          missing_dates=inputs["missing_dates"], settings=inputs["settings"],
                          rules=repository.delivery_rules(), items=repository.delivery_items(codes))
        snapshot.bill = bill
        return view(snapshot)

    def view(snapshot: _Snapshot) -> dict[str, Any]:
        inputs = snapshot.inputs
        return {**snapshot.bill, "snapshot_id": snapshot.id, "station": STATION_NAME,
                "start": inputs["start"].isoformat(), "end": inputs["end"].isoformat(),
                "settlement_window": {"start": inputs["window"][0].isoformat(), "end": inputs["window"][1].isoformat()},
                "fetched_at": inputs["fetched_at"], "settings": inputs["settings"]}

    # -- 账单 ------------------------------------------------------------------------------

    @router.get("")
    async def query(request: Request, start: str = "", end: str = ""):
        actor = admin_provider(request)

        async def compose() -> dict[str, Any]:
            first, last = bill_range(start, end)
            repository = finance()
            settings = await run_in_threadpool(repository.delivery_settings)
            window = settlement_window(first, last, settings["lookahead_days"], _today())
            platform = await settlement_reader(window[0], window[1], actor)
            arrivals = await run_in_threadpool(arrivals_reader, first, last, max(window[1], last))
            snapshot = _Snapshot({
                "start": first, "end": last, "window": window, "settings": settings,
                "settlements": settlement_entries(platform.get("rows") or []),
                "arrivals": arrivals["arrivals"], "known": arrivals["known"], "missing_dates": arrivals["missing_dates"],
                "fetched_at": str(platform.get("fetched_at") or ""),
            })
            result = await run_in_threadpool(rebuild, snapshot)
            snapshots.add(snapshot)
            return result

        return await run(compose)

    @router.post("/confirm")
    async def confirm(request: Request):
        actor = admin_provider(request)
        body = _dict(await request.json())

        def work() -> dict[str, Any]:
            if set(body) - {"request_id", "snapshot_id", "items"}:
                raise FinanceValidationError("确认内容无效。")
            items = body.get("items")
            if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH:
                raise FinanceValidationError(f"一次最多确认 {MAX_BATCH} 单。")
            snapshot = snapshots.get(body.get("snapshot_id"))
            with snapshot.lock:
                rows, stale = [], []
                for item in items:
                    if not isinstance(item, dict) or set(item) - {"tracking_number", "fingerprint"}:
                        raise FinanceValidationError("确认内容无效。")
                    code = _code(item.get("tracking_number"))
                    row = snapshot.row(code)
                    if row["fingerprint"] != item.get("fingerprint"):
                        stale.append({"tracking_number": code, "ok": False, "error_code": "DELIVERY_BILL_CHANGED",
                                      "message": "这单的内容已变化，请看最新数据后再确认。"})
                        continue
                    rows.append(row)
                result = {"results": [], "succeeded": 0, "failed": 0}
                if rows:
                    result = finance().confirm_delivery_bills(rows, request_id=body.get("request_id"),
                                                              actor=_finance_actor(actor))
                result["results"] = [*result["results"], *stale]
                result["failed"] += len(stale)
                return {**result, "bill": rebuild(snapshot)}

        return await run(lambda: run_in_threadpool(work))

    @router.post("/revoke")
    async def revoke(request: Request):
        actor = admin_provider(request)
        body = _dict(await request.json())

        def work() -> dict[str, Any]:
            if set(body) - {"request_id", "snapshot_id", "tracking_number", "expected_revision"}:
                raise FinanceValidationError("撤销内容无效。")
            snapshot = snapshots.get(body.get("snapshot_id"))
            code = _code(body.get("tracking_number"))
            with snapshot.lock:
                snapshot.row(code)
                result = finance().revoke_delivery_bill(code, {"expected_revision": body.get("expected_revision")},
                                                        request_id=body.get("request_id"), actor=_finance_actor(actor))
                return {**result, "bill": rebuild(snapshot)}

        return await run(lambda: run_in_threadpool(work))

    # -- 派送费规则与设置 -------------------------------------------------------------------

    def rules_view() -> dict[str, Any]:
        rules = finance().delivery_rules()
        for rule in rules:
            rule["description"] = describe_rule(rule)
            try:
                rule["examples"] = segment_examples(rule)
            except FinanceValidationError:
                rule["examples"] = None  # a stored rule the engine cannot evaluate shows no examples
        return {"rules": rules}

    @router.get("/rules")
    async def list_rules(request: Request):
        admin_provider(request)
        return await run(lambda: run_in_threadpool(rules_view))

    @router.post("/rules/trial")
    async def trial(request: Request):
        """试算：按表单内容算每段示例金额，给了结算重量再算这一单的派送费；不保存。"""
        admin_provider(request)
        body = _dict(await request.json())

        def work() -> dict[str, Any]:
            form = body.get("rule")
            weight = body.get("settlement_weight")
            if set(body) - {"rule", "settlement_weight"} or not isinstance(form, dict):
                raise FinanceValidationError("试算内容无效。")
            parsed = parse_rule_form({"effective_from": _today().isoformat(), **form})
            result: dict[str, Any] = {"examples": segment_examples(parsed)}
            if weight is None or not str(weight).strip():
                return result
            try:
                number = Decimal(str(weight).strip())
            except Exception:
                raise FinanceValidationError("结算重量不是有效数字。") from None
            if not number.is_finite() or number < 0 or number > 100000:
                raise FinanceValidationError("结算重量必须在 0 到 100000 之间。")
            amount, steps = delivery_fee({"content": parsed["content"]}, number)
            return {**result, "amount": format(amount, "f"), "steps": steps}

        return await run(lambda: run_in_threadpool(work))

    @router.post("/rules")
    async def create_rule(request: Request):
        actor = super_admin_provider(request)
        body = _dict(await request.json())

        def work() -> dict[str, Any]:
            form = {key: value for key, value in body.items() if key != "request_id"}
            result = finance().create_delivery_rule(form, request_id=body.get("request_id"), actor=_finance_actor(actor))
            return {**result, **rules_view()}

        return await run(lambda: run_in_threadpool(work))

    @router.post("/rules/{rule_id}/retire")
    async def retire_rule(rule_id: int, request: Request):
        actor = super_admin_provider(request)
        body = _dict(await request.json())

        def work() -> dict[str, Any]:
            payload = {key: value for key, value in body.items() if key != "request_id"}
            result = finance().retire_delivery_rule(rule_id, payload, request_id=body.get("request_id"),
                                                    actor=_finance_actor(actor))
            return {**result, **rules_view()}

        return await run(lambda: run_in_threadpool(work))

    @router.get("/settings")
    async def get_settings(request: Request):
        admin_provider(request)
        return await run(lambda: run_in_threadpool(lambda: finance().delivery_settings()))

    @router.post("/settings")
    async def save_settings(request: Request):
        actor = super_admin_provider(request)
        body = _dict(await request.json())

        def work() -> dict[str, Any]:
            payload = {key: value for key, value in body.items() if key != "request_id"}
            return finance().save_delivery_settings(payload, request_id=body.get("request_id"),
                                                    actor=_finance_actor(actor))

        return await run(lambda: run_in_threadpool(work))

    return router


def settlement_reader_for(business_service: Any) -> Callable[[dt.date, dt.date, Any], Awaitable[dict[str, Any]]]:
    """融辉结算明细经普通业务接口读取（登记账号使用、按本人身份）；失败原样说明原因，不给旧数据。"""

    async def read(start: dt.date, end: dt.date, actor: Any) -> dict[str, Any]:
        try:
            result = await business_service.invoke(
                "delivery-bill-settlements", {"account_id": SETTLEMENT_ACCOUNT_ID, "start": start.isoformat(),
                                              "end": end.isoformat()},
                principal={"actor_id": actor.actor_id, "roles": list(actor.roles)}, request_id=str(uuid.uuid4()),
                timeout_sec=SETTLEMENT_TIMEOUT)
        except DirectBusinessError as exc:
            raise FinanceError(exc.code, f"融辉结算明细读取失败：{exc}", http_status=502) from exc
        data = result.get("data") if result.get("ok") else None
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            error = result.get("error")
            message = (error.get("message") if isinstance(error, dict) else error) or "融辉结算明细读取失败，请稍后重试。"
            raise FinanceError("DELIVERY_SETTLEMENT_UNAVAILABLE", f"融辉结算明细读取失败：{message}", http_status=502)
        return data

    return read
