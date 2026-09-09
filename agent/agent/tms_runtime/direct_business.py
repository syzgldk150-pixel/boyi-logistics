"""Closed, request-scoped business calls, independent of the task runner."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from agent.orchestration.models import OrchestrationError

from agent.tms_runtime.business_contracts import (
    _business_payload,
    build_customer_action_params,
    build_receipts_audit_params,
    validate_customer_write_response,
    validate_receipts_audit_response,
)
from agent.tms_runtime.dispatch import TaskRequest, execute_target

ReadHandler = Callable[[dict[str, Any], int], Awaitable[Mapping[str, Any]]]

DIRECT_READ_TARGETS = frozenset({
    "delivery_status", "get_price", "query_waybill_detail", "ronghui_tms_tracking",
    "tracking_query", "waybill_tracking", "yunda_price", "yunda_waybill_tracking",
})
CUSTOMER_ACTIONS = frozenset({
    "query", "detail", "fetch_attachment", "mark_read", "reply", "publish", "upload_attachment",
})
CUSTOMER_READ_ACTIONS = frozenset({"query", "detail", "fetch_attachment"})


class DirectBusinessError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def business_payload(response: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap transport envelopes without turning a platform failure into data."""
    current = dict(response)
    for _ in range(6):
        if current.get("ok") is False or current.get("success") is False or current.get("error"):
            error = current.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise DirectBusinessError(
                str(current.get("error_code") or "BUSINESS_CALL_FAILED"),
                str(message or current.get("message") or "业务接口执行失败。"),
            )
        nested = current.get("data")
        if not isinstance(nested, dict):
            return current
        if set(current) - {"ok", "data", "error", "error_code", "cost_sec", "status", "postcondition_evidence", "evidence"}:
            return current
        current = nested
    raise DirectBusinessError("INVALID_BUSINESS_RESPONSE", "业务接口返回层级异常。")


class DirectBusinessService:
    """Reuse registered adapters and the one invocation journal for explicit writes.

    Read handlers return business data directly. Writes are never scheduled or
    recovered: the injected invocation service records only this live request.
    """

    def __init__(self, *, registry: Any, invocation_service: Any = None,
                 target_executor: Callable[..., Awaitable[Any]] = execute_target):
        self.registry = registry
        self.invocation_service = invocation_service
        self.target_executor = target_executor
        self._read_handlers: dict[str, ReadHandler] = {}
        self._call_handlers: dict[str, Callable[..., Awaitable[Mapping[str, Any]]]] = {}

    def register_call(self, operation: str, handler: Callable[..., Awaitable[Mapping[str, Any]]]) -> None:
        if not operation or operation in self._call_handlers or operation in self._read_handlers:
            raise ValueError("duplicate or empty business operation")
        self._call_handlers[operation] = handler

    def register_read(self, operation: str, handler: ReadHandler) -> None:
        if not operation or operation in self._read_handlers:
            raise ValueError("duplicate or empty business operation")
        self._read_handlers[operation] = handler

    def _validate_tool(self, name: str, arguments: dict[str, Any], principal: Mapping[str, Any]) -> None:
        capability = self.registry.get_tool(name)
        if capability is None:
            raise DirectBusinessError("BUSINESS_OPERATION_NOT_AVAILABLE", "该业务操作未开放。")
        roles = set(principal.get("roles") or ())
        if "super_admin" in roles:
            roles.add("admin")
        required = set((capability.get("permissions") or {}).get("required_roles") or ())
        if not required.issubset(roles):
            raise DirectBusinessError("BUSINESS_PERMISSION_REQUIRED", "当前账号没有该业务操作权限。")
        self.registry.validate_input(name, arguments)

    async def _target(self, target: str, params: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
        status_code, response = await self.target_executor(
            target, TaskRequest(params=params, timeout_sec=timeout_sec), wait_for_stop=True,
            read_lifecycle=self.invocation_service if target in DIRECT_READ_TARGETS or
                (target == "customer_service_problem" and params.get("action") in CUSTOMER_READ_ACTIONS) else None,
        )
        if status_code != 200:
            raise DirectBusinessError(str(response.get("error_code") or "BUSINESS_CALL_FAILED"),
                                      str(response.get("error") or "业务接口不可达。"))
        return business_payload(response)

    async def _read(self, operation: str, params: dict[str, Any], handler: Callable[[], Awaitable[Any]]) -> Any:
        if self.invocation_service is None:
            raise DirectBusinessError("INVOCATION_SERVICE_UNAVAILABLE", "业务服务暂不可用。")
        try:
            return await self.invocation_service.call_read(operation=operation, handler=handler,
                account_ids=(str(params["account_id"]),) if params.get("account_id") else ())
        except OrchestrationError as exc:
            raise DirectBusinessError(exc.code, str(exc)) from exc

    async def invoke(self, operation: str, params: dict[str, Any], *,
                     principal: Mapping[str, Any], request_id: str, timeout_sec: int = 180) -> dict[str, Any]:
        if (not isinstance(principal, Mapping) or not principal.get("actor_id")
                or not set(principal.get("roles") or ()) & {"admin", "super_admin"}):
            raise DirectBusinessError("TRUSTED_CONSOLE_ACTOR_REQUIRED", "需要真实管理员会话。")
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise DirectBusinessError("REQUEST_ID_REQUIRED", "缺少有效请求标识。") from exc
        if not isinstance(params, dict) or not isinstance(timeout_sec, int) or not 1 <= timeout_sec <= 600:
            raise DirectBusinessError("INVALID_BUSINESS_INPUT", "业务请求格式无效。")
        if operation in self._call_handlers:
            return dict(await self._call_handlers[operation](params, principal, request_id, timeout_sec))
        if operation in self._read_handlers:
            data = await self._read(operation, params, lambda: self._read_handlers[operation](params, timeout_sec))
            if operation == "send-waybills-query" and data.get("data_status") == "partial":
                cached = data.get("data")
                if (data.get("ok") is not False or data.get("complete") is not False
                        or not isinstance(cached, Mapping) or not isinstance(cached.get("rows"), list)
                        or type(cached.get("total")) is not int or cached["total"] < 0
                        or not isinstance(data.get("errors"), list)):
                    raise DirectBusinessError("INVALID_BUSINESS_RESPONSE", "寄件数据不完整响应格式无效。")
                return {"ok": False, "data": dict(data), "error": {
                    "code": str(data.get("error_code") or "WAYBILL_DATA_PARTIAL"),
                    "message": str(data.get("error") or "原平台补查不完整，返回的数据仅代表本地快照。")}}
            return {"ok": True, "data": business_payload(data), "error": None}
        if operation in DIRECT_READ_TARGETS:
            self._validate_tool("tms_query", {"endpoint": f"/{operation}", "params": params}, principal)
            return {"ok": True, "data": await self._read(operation, params, lambda: self._target(operation, params, timeout_sec)), "error": None}
        if operation == "receipts-audit":
            tool_name, target = "receipts_audit", "receipts_audit"
            arguments = dict(params)
            self._validate_tool(tool_name, arguments, principal)
            target_params = build_receipts_audit_params(arguments)
            validator = lambda result: validate_receipts_audit_response(result, arguments)
        elif operation.startswith("customer-service-"):
            action = operation.removeprefix("customer-service-").replace("-", "_")
            if action not in CUSTOMER_ACTIONS:
                raise DirectBusinessError("BUSINESS_OPERATION_NOT_AVAILABLE", "该业务操作未开放。")
            arguments = dict(params)
            self._validate_tool(f"customer_service_problem_{action}", arguments, principal)
            target, target_params = "customer_service_problem", build_customer_action_params(action, arguments)
            if action in CUSTOMER_READ_ACTIONS:
                return {"ok": True, "data": await self._read(operation, arguments, lambda: self._target(target, target_params, timeout_sec)), "error": None}
            validator = lambda result: validate_customer_write_response(action, result, arguments)
        else:
            raise DirectBusinessError("BUSINESS_OPERATION_NOT_AVAILABLE", "该业务操作未开放。")
        if self.invocation_service is None:
            raise DirectBusinessError("INVOCATION_SERVICE_UNAVAILABLE", "执行记录服务暂不可用。")

        async def execute() -> Mapping[str, Any]:
            result = await self._target(target, target_params, timeout_sec)
            checked = validator(result)
            business_payload(checked)
            return _business_payload(checked)

        result = await self.invocation_service.call_business(
            operation=operation, request_id=request_id, actor_id=str(principal["actor_id"]),
            source="console", arguments=arguments, handler=execute, write=True,
            account_ids=(str(arguments["account_id"]),) if arguments.get("account_id") else (),
            resource_keys=(("business", operation, str(arguments.get("account_id") or arguments.get("platform") or ""),
                            str(arguments.get("external_id") or arguments.get("waybill_no") or request_id)),),
        )
        return {"ok": result.get("success") is True, "data": result.get("result") or {},
                "invocation_id": result.get("invocation_id"), "status": result.get("status"),
                "error_code": result.get("error_code"), "error": result.get("error_summary")}
