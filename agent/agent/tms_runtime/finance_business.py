"""Console finance collection through an installed, signed finance plugin."""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from agent.orchestration.models import Actor, ActorType
from agent.tms_runtime.direct_business import DirectBusinessError

FINANCE_DYNAMIC_FIELDS = frozenset({"mode", "target_date", "start_date", "end_date", "batch_id", "rescan_days"})


class FinancePluginBusinessService:
    def __init__(self, policy: Any, catalog: Any):
        self.policy, self.catalog = policy, catalog

    async def __call__(self, params: dict[str, Any], principal: Mapping[str, Any],
                       request_id: str, timeout_sec: int) -> dict[str, Any]:
        if set(params) - FINANCE_DYNAMIC_FIELDS - {"platform", "account_id", "automation_id"}:
            raise DirectBusinessError("INVALID_FINANCE_INPUT", "财务采集包含未开放字段。")
        # The current signed collector deliberately fans out to all of its
        # three bound sources. A browser selector must not change that scope.
        if params.get("account_id") or params.get("platform") not in {None, "", "ronghui"}:
            raise DirectBusinessError("FINANCE_SCOPE_NOT_SUPPORTED", "该财务插件按已绑定数据源完整采集，不能改为单账号采集。")
        mode = params.get("mode")
        if mode not in {"sync", "backfill", "retry"}:
            raise DirectBusinessError("INVALID_FINANCE_INPUT", "财务采集模式无效。")
        dynamic = {key: value for key, value in params.items() if key in FINANCE_DYNAMIC_FIELDS}
        allowed = {"sync": {"mode", "target_date", "rescan_days"},
                   "backfill": {"mode", "start_date", "end_date"},
                   "retry": {"mode", "batch_id"}}[mode]
        try:
            if set(dynamic) - allowed:
                raise ValueError
            for key in {"target_date", "start_date", "end_date"} & dynamic.keys():
                if date.fromisoformat(dynamic[key]).isoformat() != dynamic[key]:
                    raise ValueError
            if mode == "backfill" and (not dynamic.get("start_date") or not dynamic.get("end_date")
                                       or dynamic["start_date"] > dynamic["end_date"]):
                raise ValueError
            if mode == "retry" and (type(dynamic.get("batch_id")) is not int or dynamic["batch_id"] < 1):
                raise ValueError
            if "rescan_days" in dynamic and (type(dynamic["rescan_days"]) is not int or not 1 <= dynamic["rescan_days"] <= 31):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise DirectBusinessError("INVALID_FINANCE_INPUT", "财务日期或批次参数无效。") from exc
        automation_id = str(params.get("automation_id") or "finance_bills")
        entry, contract = self.policy._load_contract(automation_id)
        if entry.plugin_id != "sync_finance_bills":
            raise DirectBusinessError("FINANCE_PLUGIN_REQUIRED", "请选择已安装的财务采集插件。")
        target = contract.invocation_contracts.get("console")
        if target is None or any(target.dynamic_argument_resolvers.get(field) != f"configured_finance_console_{field}" for field in dynamic):
            raise DirectBusinessError("FINANCE_PLUGIN_UPDATE_REQUIRED", "请先升级财务采集插件以启用按日期直接采集。")
        actor = Actor(ActorType.CONSOLE_ADMIN, str(principal["actor_id"]),
                      tuple(principal.get("roles") or ()), authenticated_by="mysql_admin_session")
        result = await self.policy.invoke_trusted_and_wait(automation_id, entrypoint="console",
            request_id=request_id, actor=actor, trusted_context={"dynamic_inputs": dynamic},
            timeout_seconds=timeout_sec)
        if result.get("status") == "RUNNING":
            result = await self.policy.direct_invocations.cancel(result["invocation_id"])
        return {"ok": result.get("success") is True, "data": result.get("result") or {},
                "invocation_id": result.get("invocation_id"), "status": result.get("status"),
                "error_code": result.get("error_code"), "error": result.get("error_summary")}
