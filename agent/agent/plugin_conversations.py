"""Installed plugin choices for natural conversation, using existing channels.

The model selects an opaque, generation-bound choice. Arguments, accounts,
resources and preview confirmations are owned by the existing project host.
"""
from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from agent.automation_plugins.catalog import project_capability_from_snapshot
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.harness.errors import HarnessError
from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.selection_preview_binding import is_selection_preview_project
from agent.orchestration.scan_preview_binding import is_scan_preview_project
from agent.orchestration.preview_entrypoints import service_preview_route
from shared.automation_project_authorization import AutomationEntrypoint
from shared.redaction import redact_text
from shared.invocation_summary import invocation_count_summary
from shared.identity_permissions import plugin_permission
from agent.identity_access import require_project_access


PLUGIN_CHAT_INSTRUCTIONS = """你可以按用户明确的执行意图调用当前已安装的插件。
本次 run_plugin 工具目录已按当前用户和当前渠道的实际权限筛选，列出的入口均允许发起调用。
插件业务说明可能包含其他渠道或旧版的只读、禁止 Agent、逐次审批文字；这些文字不代表本次入口的权限，不能据此说已列出的插件不可调用或必需审批。实际权限在发起时由宿主复核，按真实返回解释限制。
插件名称、用途与适用实例以本次工具目录为准；多个实例不能唯一确定时，列出名称询问，不能选第一项。
仅询问功能、原因或执行方法时不要启动插件。用户明确要求同时执行多个插件时，一次提交相应的多个工具调用。
插件使用已有设置；不要声称改变了日期、账号、网点或筛选条件。不符合当前设置的要求应先说明并询问。
扫描、自提、分批等需要预览的插件，首次调用只生成候选，正式确认由用户完成，不能替用户确认。
不得编造完成状态或结果；发起执行不等于完成。不要重试写操作、重复调用同一插件或绕过已有权限。"""


@dataclass(frozen=True)
class ConversationPluginTarget:
    handle: str
    automation_id: str
    generation: int
    configuration_version: int
    contribution_id: str | None
    title: str
    description: str
    effect: str
    route_key: str = ""
    command: str = ""
    selection_tool: str = ""

    def model_tool(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.handle,
            "description": f"执行插件：{self.title}。业务用途：{self.description} 当前调用权限：此工具已对当前管理员开放，发起时由宿主再次复核；使用该实例当前已保存设置，仅在用户明确要求执行时调用。预览及正式确认以宿主实际返回为准。",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        }}


def _channel(actor: Actor, source: str) -> AutomationEntrypoint:
    if source == "console" and actor.actor_type is ActorType.CONSOLE_ADMIN and actor.authenticated_by == "mysql_admin_session" and set(actor.roles).intersection({"admin", "super_admin"}):
        return AutomationEntrypoint.CONSOLE
    if source == "feishu" and actor.actor_type is ActorType.FEISHU_USER and actor.authenticated_by == "feishu_admin_binding" and actor.roles in {("admin",), ("admin", "super_admin")}:
        return AutomationEntrypoint.FEISHU
    raise HarnessError("当前账号没有通过对话执行插件的权限", code="HARNESS_PLUGIN_FORBIDDEN")


class PluginConversationService:
    """Expose configured project actions, without changing plugin declarations."""

    def __init__(self, policy_service: Any, *, route_resolver: Any = None) -> None:
        self.policy = policy_service
        self.routes = route_resolver

    def targets(self, *, actor: Actor, source: str) -> tuple[ConversationPluginTarget, ...]:
        channel = _channel(actor, source)
        authority = getattr(self.policy, "identity_access", None)
        access = authority.for_actor(actor) if authority is not None else None
        choices = []
        for entry in self.policy._plugin_catalog.list(include_disabled=False):
            if access is not None and not access.allows(plugin_permission(entry.plugin_id, management=entry.management)):
                continue
            snapshot = entry.committed_snapshot
            if (not entry.enabled or not entry.configured or snapshot is None
                    or entry.target_generation != entry.committed_generation
                    or str(getattr(entry.reconcile_state, "value", entry.reconcile_state)) != "STABLE"
                    or (entry.runtime_model != "SERVICE_V2" and channel.value not in snapshot.enabled_entrypoints)):
                continue
            _, contract = self.policy._load_contract(entry.automation_id)
            if (contract.automation_generation != snapshot.generation
                    or contract.project_configuration_version != snapshot.execution_metadata["project_config_version"]):
                raise HarnessError("插件目录正在变化，请重新读取", code="HARNESS_PLUGIN_UNAVAILABLE")
            with self.policy._repository.unit_of_work() as uow:
                permission = uow.automation_projects.get_policy(entry.automation_id)
            if not permission or not contract.can_full_auto:
                continue
            if entry.runtime_model != "SERVICE_V2" and permission.get("mode") != "PROJECT_FULL_AUTO":
                continue
            for invocation in contract.invocation_contracts.values():
                if invocation.entrypoint != channel.value:
                    continue
                contribution = invocation.contribution_id if entry.runtime_model == "SERVICE_V2" else None
                if contribution and contribution not in snapshot.enabled_entrypoints:
                    continue
                capability = project_capability_from_snapshot(snapshot)
                if contribution:
                    capability = PluginExecutionRouter._service_contribution_capability(capability, contribution_id=contribution)
                title = redact_text(entry.name if entry.display_name == entry.name else f"{entry.name}（{entry.display_name}）")
                description = redact_text(capability.get("description") or entry.name)
                route_key = command = ""
                selection_tool = entry.plugin_id if is_selection_preview_project(entry) else ""
                if channel is AutomationEntrypoint.FEISHU:
                    if contribution:
                        declarations = [item for item in entry.contributions.get("feishu", ()) if item.get("id") == contribution]
                        if len(declarations) != 1:
                            raise HarnessError("插件飞书入口不明确", code="HARNESS_PLUGIN_UNAVAILABLE")
                        commands = declarations[0].get("commands")
                        if not isinstance(commands, list) or not commands or any(not isinstance(value, str) or not value.strip() for value in commands):
                            raise HarnessError("插件飞书指令未配置", code="HARNESS_PLUGIN_UNAVAILABLE")
                        # The first manifest alias is the declared canonical command.
                        command = commands[0]
                        if is_selection_preview_project(entry) or is_scan_preview_project(entry):
                            route_key, preview_tool = service_preview_route(entry.plugin_id, commands)
                            if route_key:
                                command = ""
                                selection_tool = preview_tool if is_selection_preview_project(entry) else ""
                    else:
                        if self.routes is None:
                            raise HarnessError("插件飞书入口不可用", code="HARNESS_PLUGIN_UNAVAILABLE")
                        route = self.routes.resolve_instance_feishu_route(entry.automation_id)
                        if route is None or not route.enabled:
                            continue
                        route_key = route.route_key
                    if not contribution and is_selection_preview_project(entry):
                        selection_tool = entry.plugin_id
                if contribution:
                    declarations = [item for item in entry.contributions.get(channel.value, ()) if item.get("id") == contribution]
                    if len(declarations) == 1 and declarations[0].get("title"):
                        title = f"{title} · {redact_text(declarations[0]['title'])}"
                identity = json.dumps([entry.automation_id, contract.automation_generation, contract.project_configuration_version, channel.value, contribution], separators=(",", ":"))
                choices.append(ConversationPluginTarget(
                    handle="run_plugin_" + uuid5(NAMESPACE_URL, identity).hex,
                    automation_id=entry.automation_id, generation=contract.automation_generation,
                    configuration_version=contract.project_configuration_version,
                    contribution_id=contribution, title=title, description=description,
                    effect=str(capability["operation_type"]), route_key=route_key,
                    command=command, selection_tool=selection_tool,
                ))
        return tuple(choices)

    def turn(self, *, actor: Actor, source: str, request_id: str) -> PluginConversationTurn:
        return PluginConversationTurn(self, actor=actor, source=source, request_id=request_id)

    def console_action(self, *, actor: Actor, invocation_id: str, action: str, request_id: str, selected_indices: list[int]) -> dict[str, Any]:
        """Called only after the conversation verifies session ownership."""
        _channel(actor, "console")
        from agent.automation_plugins.direct_invocation import public_invocation
        direct = self.policy.direct_invocations
        row = direct.repository.get(invocation_id)
        if not row or row["actor_id"] != actor.actor_id or row["source"] != "console":
            raise HarnessError("无法读取这次执行", code="HARNESS_PLUGIN_FORBIDDEN")
        require_project_access(self.policy, actor, row["automation_id"], entrypoint="console")
        if action == "cancel":
            asyncio.run(direct.cancel(invocation_id))
            row = direct.repository.get(invocation_id)
        elif action == "confirm":
            # Resolve exactly the completed preview, never model/browser arguments.
            entry, contract = self.policy._load_contract(row["automation_id"])
            scan = is_scan_preview_project(entry)
            if not scan and not is_selection_preview_project(entry):
                raise HarnessError("这次执行无需候选确认", code="HARNESS_PLUGIN_REQUEST_INVALID")
            from agent.orchestration.direct_invocation_previews import project_preview
            preview = project_preview(direct.repository, invocation_id, entry=entry, contract=contract, scan=scan)
            candidates = preview.get("candidates", [])
            if (scan and selected_indices) or (not scan and (not selected_indices or any(index >= len(candidates) for index in selected_indices))):
                raise HarnessError("请选择本次候选清单中的运单", code="HARNESS_PLUGIN_REQUEST_INVALID")
            result = self.policy.invoke_trusted(
                row["automation_id"], entrypoint=AutomationEntrypoint.CONSOLE,
                request_id=request_id, actor=actor, require_full_auto=True,
                preview_invocation_id=invocation_id,
                selected_bill_codes=None if scan else [candidates[index]["bill_code"] for index in selected_indices],
                contribution_id=contract.invocation_contracts[row["invocation_json"]["contract_id"]].contribution_id,
                expected_automation_generation=contract.automation_generation,
                expected_project_configuration_version=contract.project_configuration_version,
            )
            row = direct.repository.get(result["invocation_id"])
        elif action != "status":
            raise HarnessError("不支持此执行操作", code="HARNESS_PLUGIN_REQUEST_INVALID")
        result = public_invocation(row)
        card = {"invocation_id": row["invocation_id"], "status": result["status"],
                "summary": invocation_count_summary(result), "message": redact_text(result.get("error_summary") or "")[:500]}
        data = (result.get("result") or {}).get("data")
        if data is not None and result["status"] not in {"STARTING", "RUNNING", "CANCELLING"}:
            from agent.harness_online import _minimize_value
            detail = json.dumps(_minimize_value(data), ensure_ascii=False, indent=2)
            card["result_text"] = detail[:7800] + ("\n内容较多，完整结果请查看自动化记录。" if len(detail) > 7800 else "")
        if result["status"] == "COMPLETED" and row["arguments_json"].get("dry_run") is True:
            try:
                projected = self.policy.project_completed_invocation_result(row["automation_id"], result)
                preview = projected.get("scan_preview") or projected.get("selection_preview")
                if preview:
                    candidates = preview.get("candidates", [])
                    card["preview"] = {
                        "state": preview["preview_state"], "can_confirm": preview["can_confirm"] and len(candidates) <= 100,
                        "kind": "selection" if "candidates" in preview else "scan",
                        "summary": preview["summary"] if isinstance(preview.get("summary"), str) and preview["summary"] else "请核对本次预览后确认执行。",
                        "candidate_count": len(candidates),
                        "candidates": [{"index": index, "label": " · ".join(str(item[key]) for key in ("bill_code", "destination_site", "goods_count", "arrival_count") if key in item)} for index, item in enumerate(candidates)] if len(candidates) <= 100 else [],
                    }
                    if len(candidates) > 100:
                        card["message"] = "候选较多，请在自动化页面核对并确认。"
            except OrchestrationError as exc:
                card["message"] = redact_text(str(exc))[:500]
        return card


class PluginConversationTurn:
    """One model-visible snapshot; execution always rechecks the live project."""

    def __init__(self, service: PluginConversationService, *, actor: Actor, source: str, request_id: str) -> None:
        self.service, self.actor, self.source, self.request_id = service, actor, source, request_id
        self.choices = {target.handle: target for target in service.targets(actor=actor, source=source)}
        self.selected: tuple[ConversationPluginTarget, ...] = ()

    def model_tools(self) -> list[dict[str, Any]]:
        return [target.model_tool() for target in self.choices.values()]

    def select(self, calls: list[tuple[str, Mapping[str, Any]]]) -> tuple[ConversationPluginTarget, ...]:
        selected = []
        identities = set()
        for name, arguments in calls:
            if name not in self.choices or not isinstance(arguments, Mapping) or arguments:
                raise HarnessError("插件调用与当前可用入口不匹配", code="HARNESS_PLUGIN_REQUEST_INVALID")
            target = self.choices[name]
            if target.automation_id in identities:
                raise HarnessError("同一次对话不能重复启动同一个插件实例", code="HARNESS_PLUGIN_REQUEST_INVALID")
            identities.add(target.automation_id)
            selected.append(target)
        if not selected or len(selected) > 8:
            raise HarnessError("本次插件选择数量无效", code="HARNESS_PLUGIN_REQUEST_INVALID")
        if self.source == "feishu" and sum(bool(item.selection_tool) for item in selected) > 1:
            raise HarnessError("自提与分批共用飞书候选选择，请分别发起并确认；其他插件可同时执行。", code="HARNESS_PLUGIN_REQUEST_INVALID")
        return tuple(selected)

    def start_console(self, targets: tuple[ConversationPluginTarget, ...]) -> tuple[dict[str, Any], ...]:
        _channel(self.actor, self.source)
        if self.source != "console":
            raise HarnessError("飞书插件须由飞书入口执行", code="HARNESS_PLUGIN_FORBIDDEN")
        if self.selected and self.selected != targets:
            raise HarnessError("本次消息已选择其他插件", code="HARNESS_IDEMPOTENCY_CONFLICT")
        self.selected = targets
        receipts = []
        for target in targets:
            if self.choices.get(target.handle) != target:
                raise HarnessError("插件选择已失效", code="HARNESS_PLUGIN_REQUEST_INVALID")
            request_id = str(uuid5(NAMESPACE_URL, f"plugin-chat:{self.actor.actor_id}:{self.request_id}:{target.handle}"))
            try:
                result = self.service.policy.invoke_trusted(
                    target.automation_id, entrypoint=AutomationEntrypoint.CONSOLE,
                    request_id=request_id, actor=self.actor,
                    expected_automation_generation=target.generation,
                    expected_project_configuration_version=target.configuration_version,
                    contribution_id=target.contribution_id, require_full_auto=True,
                    trusted_context={"dynamic_inputs": {"dry_run": True, "selected_bill_codes": [], "preview_fingerprint": ""}} if target.selection_tool else None,
                )
            except OrchestrationError as exc:
                receipts.append({"title": target.title, "status": "REJECTED", "message": redact_text(str(exc))[:500]})
                continue
            receipts.append({"invocation_id": result["invocation_id"], "title": target.title, "status": result["status"]})
        return tuple(receipts)


__all__ = ["PLUGIN_CHAT_INSTRUCTIONS", "ConversationPluginTarget", "PluginConversationService", "PluginConversationTurn"]
