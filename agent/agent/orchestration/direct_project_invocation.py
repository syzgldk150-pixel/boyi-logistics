"""Compile one immediate call from the existing signed project authority."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.automation_plugins.catalog import project_capability_from_snapshot
from agent.automation_plugins.execution import PluginExecutionRouter
from agent.orchestration.automation_project_policy_support import _automation_id, _entrypoint, _request_id, _trusted_context
from agent.orchestration.automation_project_service_v2 import normalize_contribution_id, resolve_invocation_contract_id, require_active_service_v2_dispatch, validate_service_v2_event_context
from agent.orchestration.models import OrchestrationError
from agent.orchestration.scan_preview_binding import is_scan_preview_project, require_scan_formal_governance
from agent.orchestration.selection_preview_binding import is_selection_preview_project, selection_preview_contribution
from agent.tool_registry import validate_schema_instance
from shared.automation_project_authorization import AutomationEntrypoint, AutomationProjectInvocation, OMIT_DYNAMIC_ARGUMENT
from shared.automation_project_authorization import canonical_sha256
from agent.identity_access import require_project_access


def invoke_direct(service, automation_id: str, *, entrypoint, request_id, actor, trusted_context=None, idempotency_key=None, expected_automation_generation=None, expected_project_configuration_version=None, preview_invocation_id=None, selected_bill_codes=None, contribution_id=None, require_full_auto=False, **unsupported) -> dict[str, Any]:
    if unsupported:
        raise OrchestrationError("INVOCATION_INPUT_INVALID", "直接调用参数包含旧任务身份")
    service._require_release_active()
    source = _entrypoint(entrypoint)
    service._require_trusted_entrypoint_actor(source, actor)
    context = _trusted_context(source, trusted_context)
    if source is AutomationEntrypoint.EVENTS:
        validate_service_v2_event_context(context, request_id=request_id)
    direct = getattr(service, "direct_invocations", None)
    safe_id, request_id = _automation_id(automation_id), _request_id(request_id)
    require_project_access(service, actor, safe_id, entrypoint=source.value)
    from agent.automation_plugins.direct_invocation import public_invocation
    replay = direct.repository.by_request(canonical_sha256([source.value, actor.actor_id, f"automation.{safe_id}.run", idempotency_key or request_id])) if direct is not None else None
    if replay is not None:
        if replay["request_id"] != request_id:
            raise OrchestrationError("REQUEST_ID_REUSED", "同一幂等标识不能用于不同请求")
        if replay.get("preview_invocation_id") != preview_invocation_id or (selected_bill_codes is not None and replay["arguments_json"].get("selected_bill_codes") != list(selected_bill_codes)):
            raise OrchestrationError("IDEMPOTENCY_CONFLICT", "同一请求不能替换预览或所选运单")
        dynamic = (trusted_context or {}).get("dynamic_inputs", {})
        if any(replay["arguments_json"].get(key) != value for key, value in dynamic.items()):
            raise OrchestrationError("IDEMPOTENCY_CONFLICT", "同一请求不能更换参数")
        return public_invocation(replay)
    entry, contract = service._load_contract(safe_id)
    is_v2 = entry.runtime_model == "SERVICE_V2"
    if not is_v2 and source in {AutomationEntrypoint.HARNESS, AutomationEntrypoint.MODULE_SLOTS, AutomationEntrypoint.EVENTS}:
        raise OrchestrationError("PROJECT_ENTRYPOINT_DISABLED", "该调用入口需要已登记的服务贡献")
    if expected_automation_generation is not None and contract.automation_generation != expected_automation_generation:
        raise OrchestrationError("PROJECT_INVOCATION_STALE", "插件版本已变化，请重新操作")
    if expected_project_configuration_version is not None and contract.project_configuration_version != expected_project_configuration_version:
        raise OrchestrationError("PROJECT_INVOCATION_STALE", "插件设置已变化，请重新操作")
    contribution = resolve_invocation_contract_id(contract, source=source, contribution_id=normalize_contribution_id(contribution_id), context=context)
    target = contract.invocation_contracts.get(contribution)
    if target is None or target.entrypoint != source.value:
        raise OrchestrationError("PROJECT_ENTRYPOINT_DISABLED", "该插件未开放此调用入口")
    if is_v2 and source is AutomationEntrypoint.WEBHOOK:
        dynamic = context.get("dynamic_inputs", {})
        if set(dynamic) - set(target.dynamic_argument_resolvers):
            raise OrchestrationError("TRUSTED_CONTEXT_INVALID", "回调参数未在当前插件入口声明")
    if is_v2:
        require_active_service_v2_dispatch(service._contribution_registry, source=source, entry=entry, automation_id=safe_id, generation=contract.automation_generation, invocation_contract=target, context=context, expected_event_name=context.get("event_name"))
    elif source in {AutomationEntrypoint.HARNESS, AutomationEntrypoint.MODULE_SLOTS, AutomationEntrypoint.EVENTS}:
        raise OrchestrationError("PROJECT_ENTRYPOINT_DISABLED", "该调用入口需要已登记的服务贡献")
    execution_context = {"entrypoint": source.value, "occurred_at": datetime.now(timezone.utc).isoformat(), "project_request_id": request_id, "dynamic_inputs": {}, **context}
    if any(value.startswith("configured_finance_console_") for value in target.dynamic_argument_resolvers.values()):
        if entry.plugin_id != "sync_finance_bills" or source is not AutomationEntrypoint.CONSOLE:
            raise OrchestrationError("PROJECT_DYNAMIC_INPUT_INVALID", "财务设置解析器不属于此插件入口")
        execution_context["plugin_id"] = entry.plugin_id
        execution_context["project_config"] = dict(entry.committed_snapshot.execution_metadata["project_config"])
    arguments = dict(target.expected_arguments)
    for field, resolver_id in target.dynamic_argument_resolvers.items():
        if preview_invocation_id is not None and field in {"dry_run", "selected_bill_codes", "preview_fingerprint"}:
            # These are taken from the verified preview and explicit selection,
            # never from a browser-supplied dynamic-input envelope.
            continue
        if service._dynamic_resolver is None:
            raise OrchestrationError("PROJECT_DYNAMIC_INPUT_UNAVAILABLE", "缺少动态参数解析器")
        try:
            value = service._dynamic_resolver(resolver_id, field, execution_context)
        except (KeyError, TypeError, ValueError) as exc:
            code = "SELECTION_INPUT_INVALID" if field in {"dry_run", "selected_bill_codes", "preview_fingerprint"} else "PROJECT_DYNAMIC_INPUT_INVALID"
            raise OrchestrationError(code, "本次调用的动态参数缺失或无效") from exc
        if value is not OMIT_DYNAMIC_ARGUMENT:
            arguments[field] = value
    scan, selection = is_scan_preview_project(entry), is_selection_preview_project(entry)
    if scan:
        selection = False
    if is_v2 and selection:
        selection_declaration = selection_preview_contribution(entry, source.value)
        selection = selection_declaration is not None and selection_declaration["id"] == target.contribution_id
    if preview_invocation_id is not None:
        from agent.orchestration.direct_invocation_previews import confirm_preview
        if scan:
            require_scan_formal_governance(entry)
        arguments = confirm_preview(direct.repository, preview_invocation_id, entry=entry, contract=contract, actor_id=actor.actor_id, arguments=arguments, selected_bill_codes=selected_bill_codes, scan=scan)
    elif selected_bill_codes is not None:
        raise OrchestrationError("SELECTION_PREVIEW_REQUIRED", "请先读取本次候选清单")
    elif scan:
        arguments["dry_run"] = True
    elif selection:
        supplied = context.get("dynamic_inputs", {})
        if supplied.get("dry_run") is False or supplied.get("selected_bill_codes"):
            raise OrchestrationError("SELECTION_PREVIEW_REQUIRED", "正式执行必须绑定本次候选清单")
        arguments.update(dry_run=True, selected_bill_codes=[], preview_fingerprint="")
    capability = project_capability_from_snapshot(entry.committed_snapshot)
    if is_v2:
        capability = PluginExecutionRouter._service_contribution_capability(capability, contribution_id=target.contribution_id, arguments=arguments)
    from agent.automation_plugins.code_owned_fields import apply_scan_execution_boundary, apply_selection_execution_boundary
    capability = apply_selection_execution_boundary(apply_scan_execution_boundary(capability, arguments), arguments)
    schema_arguments = {key: value for key, value in arguments.items() if key != "_scan_preview_binding"}
    validate_schema_instance(str(capability["name"]), schema_arguments, capability["input_schema"])
    with service._repository.unit_of_work() as uow:
        policy = uow.automation_projects.get_policy(safe_id)
    if policy is None:
        raise OrchestrationError("PROJECT_POLICY_NOT_INITIALIZED", "插件权限尚未初始化")
    write = capability["operation_type"] not in {"read", "compute"}
    if (capability.get("approval") or {}).get("mode") == "disabled":
        raise OrchestrationError("OPERATION_DISABLED", "插件声明已停用此操作")
    permissions = capability.get("permissions")
    roles = [] if permissions == [] and not write else permissions.get("required_roles") if isinstance(permissions, dict) else None
    if not isinstance(roles, list) or (write and not roles) or any(not isinstance(role, str) or not role.strip() for role in roles):
        raise OrchestrationError("INVALID_PERMISSION_CONTRACT", "插件缺少有效权限声明")
    if not write and roles and source is AutomationEntrypoint.CONSOLE and not service._is_super_admin(actor) and not set(actor.roles).intersection(roles):
        raise OrchestrationError("TOOL_PERMISSION_DENIED", "当前账号没有此插件的读取权限")
    mode = policy.get("mode")
    full_auto = (is_v2 or mode == "PROJECT_FULL_AUTO") and contract.can_full_auto
    if require_full_auto and not full_auto:
        raise OrchestrationError("PROJECT_PERMISSION_REQUIRED", "该插件未允许自动执行，请先检查项目权限")
    legacy_schedule = source is AutomationEntrypoint.SCHEDULER and mode == "LEGACY_SCHEDULE_ONLY" and service._legacy_schedule_active(policy, contract)
    explicit_admin = source is AutomationEntrypoint.CONSOLE and (service._is_super_admin(actor) or bool(set(actor.roles).intersection(roles)))
    if write and not (full_auto or legacy_schedule or explicit_admin):
        raise OrchestrationError("PROJECT_PERMISSION_REQUIRED", "当前账号或插件设置未授权此执行")
    if direct is None:
        raise OrchestrationError("PROJECT_INVOKE_UNAVAILABLE", "插件直接调用服务尚未就绪")
    invocation = AutomationProjectInvocation(automation_id=safe_id, automation_generation=contract.automation_generation, entrypoint=source, contract_id=target.contract_id, contract_hash=contract.contract_hash, policy_version=int(policy["version"]), project_configuration_version=contract.project_configuration_version, request_id=request_id)
    def admission_guard(uow):
        service._require_release_active()
        # Pair -> project is the same lock order as cutover. Validation calls
        # are explicit Console administrator actions, never automatic ingress.
        from shared.orchestration_repository_support import ConcurrentUpdateError
        try:
            uow.automation_plugins.require_migration_direct_entrypoint(
                safe_id, source=source.value, super_admin="super_admin" in actor.roles)
        except ConcurrentUpdateError as exc:
            raise OrchestrationError("PLUGIN_MIGRATION_ENTRYPOINT_DISABLED",
                "插件正在验证或已切换，该入口当前不可执行") from exc
        require_project_access(service, actor, safe_id, entrypoint=source.value)
        current, _ = service._lock_and_compile_contract(uow, entry, expected=contract, require_enabled=True)
        current_policy = uow.automation_projects.get_policy(safe_id, for_update=True)
        if not current_policy or current_policy["version"] != policy["version"] or current_policy["mode"] != mode:
            raise OrchestrationError("PROJECT_INVOCATION_STALE", "执行前插件权限发生变化，请重新触发")
        if is_v2:
            require_active_service_v2_dispatch(service._contribution_registry, source=source, entry=entry, automation_id=safe_id, generation=current.automation_generation, invocation_contract=target, context=context, expected_event_name=context.get("event_name"))
    from shared.orchestration_repository_support import IdempotencyConflict
    try:
        return direct.start(invocation=invocation, capability=capability, arguments=arguments, source=source.value, actor_id=actor.actor_id, request_key=idempotency_key or request_id, preview_invocation_id=preview_invocation_id, admission_guard=admission_guard)
    except IdempotencyConflict as exc:
        code = "SCAN_PREVIEW_ALREADY_CONSUMED" if scan else "SELECTION_PREVIEW_ALREADY_CONSUMED" if selection else "IDEMPOTENCY_CONFLICT"
        raise OrchestrationError(code, "本次请求或候选清单已被使用，请查看原执行结果") from exc
