"""Console application services grouped by business responsibility."""

from typing import Any, Mapping
import re

from console.app_support import *  # noqa: F403
from console.services.automation_projects import *  # noqa: F403
from console.services.automation_projects import (
    automation_plugin_block_warning as _automation_plugin_block_warning,
)

from console.services.automation_preview_support import (
    SCAN_PREVIEW_ERROR_MESSAGES,
    SCAN_PREVIEW_PUBLIC_FIELDS,
    SELECTION_PREVIEW_ERROR_MESSAGES,
    SELECTION_PREVIEW_PUBLIC_FIELDS,
    current_plugin_schedule_rows,
    group_scheduled_rows_by_automation_id,
    normalize_scan_preview_projection,
    normalize_selection_preview_projection,
    preview_project_id_valid,
    scan_preview_error_message,
    selection_preview_error_message,
)
from shared.plugin_invocation_repository import ACTIVE_INVOCATION_STATUSES, TERMINAL_INVOCATION_STATUSES
from console.services.automation_invocation_output import invocation_output_lines, invocation_start_feedback
from console.services.automation_invocation_history import AutomationInvocationHistoryMixin

INVOCATION_STATUSES = ACTIVE_INVOCATION_STATUSES | TERMINAL_INVOCATION_STATUSES


def _automation_existing_run_receipt(details: Any) -> dict[str, str] | None:
    if not isinstance(details, Mapping):
        return None
    invocation_id = details.get("active_invocation_id")
    status = details.get("active_status")
    kind = details.get("blocking_kind")
    if (
        not isinstance(invocation_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", invocation_id) is None
        or not isinstance(status, str)
        or status not in INVOCATION_STATUSES
        or not isinstance(kind, str)
        or kind != "ACTIVE"
    ):
        return None
    return {"invocation_id": invocation_id, "status": status, "blocking_kind": kind}


def _automation_blocking_feedback(details: Any) -> tuple[str, str]:
    return ("本次未启动", "当前所需资源仍在使用，本次已结束，请稍后重新触发。")


class AutomationServiceMixin(AutomationInvocationHistoryMixin, AutomationProjectsServiceMixin):
    def _build_virtual_automation_task(
        self,
        task_id: str,
        *,
        override: dict[str, Any] | None = None,
        feedback: dict[str, Any] | None = None,
        open_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Project one installed instance that has no persisted schedule row yet."""
        override = override or {}
        runtime_state = self.automation_virtual_task_state.get(task_id, {})
        tool_params = runtime_state.get("tool_params")
        if not isinstance(tool_params, dict):
            tool_params = {}
        raw_json = override.get("tool_params_json")
        if raw_json is None:
            raw_json = runtime_state.get("tool_params_json")
        if raw_json is None:
            raw_json = json.dumps(tool_params, ensure_ascii=False, indent=2)

        name_value = str(
            override.get("name")
            or runtime_state.get("name")
            or normalize_task_display_name(task_id)
            or task_id
        )
        tool_name_value = str(override.get("tool_name") or runtime_state.get("tool_name") or "")
        provider_value = "ronghui"
        last_run_value = str(runtime_state.get("last_run") or "")
        last_status = str(runtime_state.get("last_status") or "")
        last_duration_ms = runtime_state.get("last_duration_ms")
        last_message_value = str(runtime_state.get("last_message") or "")
        trigger_label = "手动执行"
        return {
            "id": task_id,
            "note": "",
            "provider": provider_value,
            "provider_label": automation_provider_label(provider_value),
            "system_badges": [],
            "task_id": task_id,
            "task_mode": "manual",
            "display_task_id": task_id,
            "group_size": 1,
            "task_ids": [],
            "task_cron_expressions": {},
            "name_value": name_value,
            "tool_name_value": tool_name_value,
            "cron_expression_value": "",
            "enabled_value": False,
            "tool_params_json": raw_json,
            "tool_param_fields": flatten_automation_fields(tool_params),
            "schedule_supported": False,
            "schedule_editable": False,
            "schedule_time_values": [],
            "schedule_summary": trigger_label,
            "schedule_icon": "zap",
            "schedule_warning": f"此流程不写入 scheduled_tasks，默认通过{trigger_label}触发。",
            "trigger_label": trigger_label,
            "is_schedulable": False,
            "can_save": True,
            "can_run_now": True,
            "has_webhook": False,
            "webhook_path": "",
            "webhook_full_url": "",
            "webhook_masked_url": "",
            "webhook_token_enabled": False,
            "webhook_header_name": "",
            "webhook_body_json": "{}",
            "resource_bindings": [],
            "missing_required_resources": [],
            "missing_required_resource_labels": [],
            "resource_blocked": False,
            "last_status_value": last_status,
            "last_run_value": last_run_value,
            "last_activity_value": last_run_value,
            "last_duration_label": format_duration_label(last_duration_ms),
            "last_message_value": last_message_value,
            "last_error_summary": shorten_error_message(last_message_value) if last_status == "error" else "",
            "search_text": " ".join(
                item.lower()
                for item in (
                    name_value,
                    tool_name_value,
                    task_id,
                    trigger_label,
                    automation_provider_label(provider_value),
                )
                if item
            ),
            "sort_order": 999,
            "is_open": bool(open_task_id == task_id or feedback),
            "feedback": feedback,
        }

    def _record_virtual_task_runtime(
        self,
        task_id: str,
        *,
        payload: dict[str, Any] | None = None,
        last_run: str | None = None,
        last_status: str | None = None,
        last_duration_ms: int | None = None,
        last_message: str | None = None,
    ) -> None:
        state = dict(self.automation_virtual_task_state.get(task_id, {}))
        if payload:
            state["name"] = payload.get("name")
            state["tool_name"] = payload.get("tool_name")
            state["tool_params"] = dict(payload.get("tool_params") or {})
            state["tool_params_json"] = payload.get("tool_params_json") or json.dumps(
                payload.get("tool_params") or {},
                ensure_ascii=False,
                indent=2,
            )
        if last_run is not None:
            state["last_run"] = last_run
        if last_status is not None:
            state["last_status"] = last_status
        if last_duration_ms is not None:
            state["last_duration_ms"] = last_duration_ms
        if last_message is not None:
            state["last_message"] = last_message
        self.automation_virtual_task_state[task_id] = state

    def _start_automation_task_run(
        self,
        payload: dict[str, Any],
        *,
        trusted_context: dict[str, Any],
        browser_request_uuid: str,
        preview_invocation_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke one saved automation project; Agent resolves its trusted configuration."""

        automation_id = self._automation_project_id(payload.get("task_id"))
        request_id = self._normalize_browser_request_uuid(browser_request_uuid)
        if not automation_id or not request_id:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error": "自动化项目或浏览器请求标识无效。",
                "error_code": "INVALID_AUTOMATION_PROJECT_INVOKE",
            }
        invoke_payload = {"request_id": request_id}
        contribution_id = str(payload.get("contribution_id") or "").strip().lower()
        if contribution_id:
            if not AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE.fullmatch(contribution_id):
                return {
                    "ok": False,
                    "status": HTTPStatus.BAD_REQUEST,
                    "error": "自动化手动入口无效。",
                    "error_code": "INVALID_AUTOMATION_CONTRIBUTION",
                }
            invoke_payload["contribution_id"] = contribution_id
        if preview_invocation_id is not None:
            safe_preview_invocation_id = self._normalize_browser_request_uuid(preview_invocation_id)
            if not preview_project_id_valid(automation_id) or not safe_preview_invocation_id:
                return {
                    "ok": False,
                    "status": HTTPStatus.BAD_REQUEST,
                    "error": SCAN_PREVIEW_ERROR_MESSAGES[
                        "SCAN_PREVIEW_ID_INVALID"
                    ],
                    "error_code": "SCAN_PREVIEW_ID_INVALID",
                }
            invoke_payload["preview_invocation_id"] = safe_preview_invocation_id
        run_result = self._agent_request(
            "POST",
            f"/internal/v1/automation-projects/{quote(automation_id, safe='')}/invoke",
            payload=invoke_payload,
            timeout=self.settings.agent_timeout_seconds,
            console_principal=trusted_context["_console_principal"],
        )
        if not run_result.get("ok"):
            return run_result

        receipt = run_result.get("data")
        invocation_id = str(receipt.get("invocation_id") or "").strip() if isinstance(receipt, dict) else ""
        if not invocation_id:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_GATEWAY,
                "error": "智能服务未返回可追踪的执行标识。",
                "error_code": "INVALID_AGENT_RUN_CONTRACT",
            }

        started_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        receipt_status = str(receipt.get("status") or "")
        local_status = ("running" if receipt_status in ACTIVE_INVOCATION_STATUSES else
                        "success" if receipt_status == "COMPLETED" else
                        "cancelled" if receipt_status == "CANCELLED" else "error")
        self._record_virtual_task_runtime(
            payload["task_id"],
            payload=payload,
            last_run=started_stamp,
            last_status=local_status,
            last_duration_ms=None,
            last_message="",
        )
        state = dict(self.automation_virtual_task_state.get(payload["task_id"], {}))
        state.update(
            {
                "invocation_id": invocation_id,
                "task_mode": "plugin",
                "last_run": started_stamp,
                "last_status": local_status,
            }
        )
        self.automation_virtual_task_state[payload["task_id"]] = state
        return run_result

    def _render_automations(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict,
        *,
        task_overrides: dict[str, dict[str, Any]] | None = None,
        task_feedbacks: dict[str, dict[str, Any]] | None = None,
        open_task_id: str | None = None,
        module: str = "automation",
    ) -> None:
        task_overrides = task_overrides or {}
        task_feedbacks = task_feedbacks or {}
        request_headers = getattr(handler, "headers", {})
        partial_navigation = (
            str(request_headers.get("X-Requested-With") or "")
            == "ConsolePartialNavigation"
        )

        automation_db_warning = ""
        try:
            scheduled_rows = self.repository.list_scheduled_tasks()
        except Exception as exc:
            LOGGER.warning("Failed to load automations from repository: %s", exc)
            scheduled_rows = []
            automation_db_warning = normalize_feedback_text(
                f"自动化任务数据库当前不可达，任务列表已临时降级为空。详情：{exc}"
            )

        # Keep-alive connections reuse the handler; never carry an old age.
        handler.automation_catalog_snapshot_age_seconds = None
        if query.get("refresh_resources") == ["1"]:
            plugin_catalog = self._load_automation_plugin_catalog(
                handler, module=module, summary=True,
                refresh_resources=True,
            )
        else:
            # Sidebar clicks and full page loads both show the last safe
            # snapshot at once and refresh it in the background.
            plugin_catalog = self._load_automation_plugin_catalog(
                handler, module=module, summary=True,
                prefer_stale=True,
            )
        snapshot_age = getattr(handler, "automation_catalog_snapshot_age_seconds", None)
        automation_catalog_snapshot_notice = (
            f"列表为约 {max(1, round(snapshot_age / 60))} 分钟前的状态，正在后台更新"
            if isinstance(snapshot_age, (int, float)) and snapshot_age >= 300
            else ""
        )
        (
            automation_plugin_packages,
            automation_plugin_instances,
            unsupported_automation_ids,
            hidden_automation_ids,
            automation_plugin_warning,
            can_manage_plugins,
        ) = plugin_catalog
        plugin_instances_by_id = {
            str(item["automation_id"]): item for item in automation_plugin_instances
        }
        # A failed catalog is not evidence that installed plugins disappeared.
        # Without its module/retirement ownership, old SQL schedules must not
        # reappear as missing-plugin cards. The catalog warning remains visible.
        scheduled_row_groups = group_scheduled_rows_by_automation_id(
            [] if automation_plugin_warning and not automation_plugin_instances
            else current_plugin_schedule_rows(scheduled_rows)
        )
        tasks_by_id: dict[str, dict[str, Any]] = {}
        for scheduled_group in scheduled_row_groups:
            base_task_id = str(scheduled_group["task_id"])
            if module != "automation" and base_task_id not in plugin_instances_by_id:
                continue
            if (
                base_task_id in hidden_automation_ids
                and not bool(scheduled_group["missing_automation_id"])
                and base_task_id not in plugin_instances_by_id
            ):
                # Historical rows stay in Agent for audit, but an explicitly
                # deferred project must not render an unusable Console card.
                continue
            automation_link_missing = bool(
                scheduled_group["missing_automation_id"]
            )
            rows = list(scheduled_group["rows"])
            rows = sorted(
                rows,
                key=lambda item: (
                    str(item.get("cron_expression", "") or ""),
                    str(item.get("id", "") or ""),
                ),
            )
            primary_row = next((item for item in rows if str(item.get("id", "") or "") == base_task_id), rows[0])
            override = task_overrides.get(base_task_id, {})
            tool_params = primary_row.get("tool_params", {})
            raw_json = override.get("tool_params_json")
            if raw_json is None:
                raw_json = json.dumps(tool_params, ensure_ascii=False, indent=2)

            override_schedule_times_raw = str(override.get("schedule_times_json", "") or "").strip()
            override_schedule_times: list[str] = []
            if override_schedule_times_raw:
                try:
                    parsed_schedule_times = json.loads(override_schedule_times_raw)
                except json.JSONDecodeError:
                    parsed_schedule_times = []
                if isinstance(parsed_schedule_times, list):
                    override_schedule_times = normalize_schedule_times(
                        [str(item or "").strip() for item in parsed_schedule_times]
                    )

            cron_values = [str(item.get("cron_expression", "") or "").strip() for item in rows]
            if override.get("cron_expression"):
                cron_values = [str(override.get("cron_expression", "") or "").strip()]
            if override_schedule_times:
                cron_values = [build_daily_cron_expression(item) or "" for item in override_schedule_times]

            schedule_info = summarize_task_group_schedule(cron_values)
            last_run_value = max((str(item.get("last_run") or "") for item in rows), default="")
            created_at_value = max((str(item.get("created_at") or "") for item in rows), default="")
            last_activity = last_run_value or created_at_value
            last_status = next(
                (
                    str(item.get("last_status", "") or "")
                    for item in rows
                    if str(item.get("last_run") or "") == last_run_value and last_run_value
                ),
                str(primary_row.get("last_status", "") or ""),
            )
            last_duration_ms = next(
                (
                    item.get("last_duration_ms")
                    for item in rows
                    if str(item.get("last_run") or "") == last_run_value and last_run_value
                ),
                primary_row.get("last_duration_ms"),
            )
            last_message_value = next(
                (
                    str(item.get("last_message", "") or "")
                    for item in rows
                    if str(item.get("last_run") or "") == last_run_value and last_run_value
                ),
                str(primary_row.get("last_message", "") or ""),
            )
            enabled_value = override.get("enabled")
            if enabled_value is None:
                enabled_value = any(bool(item.get("enabled")) for item in rows)

            name_value = str(
                override.get("name")
                or normalize_task_display_name(str(primary_row.get("name", "") or ""))
            )
            tool_name_value = str(override.get("tool_name") or primary_row.get("tool_name", "") or "")
            provider_value = "ronghui"
            tasks_by_id[str(scheduled_group["storage_key"])] = {
                **primary_row,
                "note": "",
                "provider": provider_value,
                "provider_label": automation_provider_label(provider_value),
                "system_badges": [],
                "task_id": base_task_id,
                "automation_link_missing": automation_link_missing,
                "task_mode": "scheduled",
                "display_task_id": str(primary_row.get("id", "") or base_task_id) if len(rows) == 1 else base_task_id,
                "group_size": len(rows),
                "task_ids": [str(item.get("id") or "").strip() for item in rows],
                "task_cron_expressions": {
                    str(item.get("id") or "").strip(): str(
                        item.get("cron_expression") or ""
                    ).strip()
                    for item in rows
                    if str(item.get("id") or "").strip()
                },
                "name_value": name_value,
                "tool_name_value": tool_name_value,
                "cron_expression_value": schedule_info["raw_value"],
                "enabled_value": bool(enabled_value),
                "tool_params_json": raw_json,
                "tool_param_fields": flatten_automation_fields(tool_params),
                "schedule_supported": bool(schedule_info["supported"] or not schedule_info["raw_value"]),
                "schedule_editable": True,
                "schedule_time_values": schedule_info["time_values"],
                "schedule_summary": schedule_info["summary"],
                "schedule_icon": "clock",
                "schedule_warning": schedule_info["warning"],
                "trigger_label": "定时任务",
                "is_schedulable": True,
                "can_save": True,
                "can_run_now": True,
                "has_webhook": False,
                "webhook_path": "",
                "webhook_full_url": "",
                "webhook_masked_url": "",
                "webhook_token_enabled": False,
                "webhook_header_name": webhook_token_header_name(),
                "webhook_body_json": "{}",
                "resource_bindings": [],
                "missing_required_resources": [],
                "missing_required_resource_labels": [],
                "resource_blocked": False,
                "last_status_value": last_status,
                "last_run_value": last_run_value,
                "last_activity_value": last_activity,
                "last_duration_label": format_duration_label(last_duration_ms),
                "last_message_value": last_message_value,
                "last_error_summary": shorten_error_message(last_message_value) if last_status == "error" else "",
                "search_text": " ".join(
                    item.lower()
                    for item in (
                        name_value,
                        tool_name_value,
                        base_task_id,
                        automation_provider_label(provider_value),
                    )
                    if item
                ),
                "sort_order": 999,
                "is_open": bool(
                    open_task_id == base_task_id
                    or task_feedbacks.get(base_task_id)
                ),
                "feedback": task_feedbacks.get(base_task_id),
            }

        for plugin in automation_plugin_instances:
            automation_id = str(plugin["automation_id"])
            if automation_id in tasks_by_id:
                continue
            plugin_override = dict(task_overrides.get(automation_id) or {})
            plugin_override.setdefault("name", str(plugin.get("instance_name") or automation_id))
            plugin_override.setdefault("tool_name", f"automation.{automation_id}.run")
            plugin_override.setdefault("enabled", False)
            task = self._build_virtual_automation_task(
                automation_id,
                override=plugin_override,
                feedback=task_feedbacks.get(automation_id),
                open_task_id=open_task_id,
            )
            can_schedule = bool(plugin.get("can_schedule"))
            task.update(
                {
                    "task_mode": "scheduled" if can_schedule else "manual",
                    "is_schedulable": can_schedule,
                    "schedule_supported": can_schedule,
                    "schedule_editable": can_schedule,
                    "trigger_label": "定时任务 / 手工执行" if can_schedule else "手工执行",
                }
            )
            tasks_by_id[automation_id] = task

        for task in tasks_by_id.values():
            automation_id = str(task.get("task_id") or "")
            plugin = (
                None
                if task.get("automation_link_missing")
                else plugin_instances_by_id.get(automation_id)
            )
            task["plugin"] = plugin
            task["plugin_missing"] = plugin is None
            task["plugin_warning"] = ""
            if plugin and plugin.get("runtime_model") == "SERVICE_V2":
                declared_systems = {
                    system for role in plugin.get("account_roles", [])
                    for system in role.get("allowed_systems", [])
                }
                if len(declared_systems) == 1:
                    provider = next(iter(declared_systems))
                    if provider in AUTOMATION_PROVIDER_LABELS:
                        task["provider"] = provider
            if plugin is None:
                task["can_save"] = False
                task["can_run_now"] = False
                task["schedule_editable"] = False
                task["plugin_blocked"] = True
                task["plugin_warning"] = (
                    "扩展信息不完整：定时任务未关联项目标识，当前已停止运行。"
                    if task.get("automation_link_missing")
                    else "扩展信息缺失：该任务当前不能运行或设置。"
                )
                continue

            instance_name = str(plugin.get("instance_name") or "").strip()
            task["name_value"] = (
                instance_name if instance_name and instance_name != automation_id else ""
            ) or str(task.get("name_value") or automation_id)
            task["display_task_id"] = automation_id
            task["plugin_blocked"] = bool(plugin.get("blocked"))
            task["has_webhook"] = False
            task["webhook_path"] = ""
            task["webhook_full_url"] = ""
            task["webhook_masked_url"] = ""
            task["resource_bindings"] = []
            task["missing_required_resources"] = []
            task["missing_required_resource_labels"] = []
            task["resource_blocked"] = False
            task["is_schedulable"] = bool(plugin.get("can_schedule"))
            task["schedule_supported"] = bool(task.get("is_schedulable"))
            stable_state = bool(plugin.get("lifecycle_actions_allowed"))
            task["can_save"] = stable_state
            task["schedule_editable"] = bool(plugin.get("can_schedule")) and stable_state

            cron_values = [
                str(value or "").strip()
                for value in (task.get("task_cron_expressions") or {}).values()
                if str(value or "").strip()
            ]
            legacy_schedule_times = list(task.get("schedule_time_values") or [])
            signed_schedule = dict(plugin.get("schedule") or {})
            signed_schedule_kind = str(signed_schedule.get("kind") or "none")
            allowed_schedule_kinds = set(
                (plugin.get("scheduling") or {}).get("allowed_kinds") or []
            )
            task["plugin_schedule_source"] = "agent"
            if signed_schedule_kind != "none":
                plugin_schedule_kind = signed_schedule_kind
                plugin_schedule_supported = signed_schedule_kind in allowed_schedule_kinds
                task["schedule_time_values"] = list(signed_schedule.get("times") or [])
                task["enabled_value"] = bool(signed_schedule.get("enabled"))
            elif not cron_values:
                plugin_schedule_kind = "none"
                plugin_schedule_supported = True
                task["schedule_time_values"] = []
                task["enabled_value"] = False
            elif all(value == "@startup" for value in cron_values):
                plugin_schedule_kind = "startup"
                plugin_schedule_supported = "startup" in allowed_schedule_kinds
                task["schedule_time_values"] = []
                task["plugin_schedule_source"] = "legacy_migration"
            elif legacy_schedule_times:
                plugin_schedule_kind = "daily_times"
                plugin_schedule_supported = "daily_times" in allowed_schedule_kinds
                task["schedule_time_values"] = legacy_schedule_times
                task["plugin_schedule_source"] = "legacy_migration"
            else:
                plugin_schedule_kind = "unsupported"
                plugin_schedule_supported = False
            task["plugin_schedule_kind"] = plugin_schedule_kind
            task["plugin_schedule_supported"] = plugin_schedule_supported
            # Only Agent-owned schedules carry a digest for the times-only CAS.
            task["plugin_schedule_sha256"] = (
                plugin.get("schedule_sha256")
                if task["plugin_schedule_source"] == "agent"
                else None
            )
            task["plugin_schedule_max_daily_times"] = int(
                (plugin.get("scheduling") or {}).get("max_daily_times") or 0
            )
            if not plugin_schedule_supported:
                task["plugin_blocked"] = True
                plugin["blocked"] = True
                plugin["missing_requirements"] = list(
                    dict.fromkeys(
                        [
                            *list(plugin.get("missing_requirements") or []),
                            "定时设置与当前扩展不匹配，请联系系统管理员处理",
                        ]
                    )
                )

            base_runnable = bool(
                plugin.get("enabled") and plugin.get("configured") and not plugin.get("blocked")
            )
            console_enabled = "console" in set(
                plugin.get("enabled_entrypoint_kinds") or []
            )
            task["can_run_now"] = bool(base_runnable and console_enabled)
            task["run_disabled_reason"] = (
                str(task.get("console_disabled_reason") or "后台入口已关闭")
                if base_runnable and not console_enabled
                else "当前不可执行"
            )
            task["plugin_warning"] = _automation_plugin_block_warning(plugin)
            if not task.get("plugin_blocked"):
                task["plugin_warning"] = ""
            task["search_text"] = " ".join(
                item
                for item in (
                    str(task.get("search_text") or ""),
                    str(plugin.get("plugin_id") or ""),
                    str(plugin.get("instance_name") or ""),
                    str(plugin.get("version") or ""),
                )
                if item
            ).lower()

        tasks = sorted(
            tasks_by_id.values(),
            key=lambda item: (
                int(item.get("sort_order") or 999),
                str(item.get("name_value") or ""),
            ),
        )
        plugin_filter = ""
        raw_plugin_filter = query.get("plugin")
        if isinstance(raw_plugin_filter, list) and len(raw_plugin_filter) == 1:
            candidate = str(raw_plugin_filter[0] or "").strip()
            if candidate and len(candidate) <= 160:
                plugin_filter = candidate
                tasks = [
                    task
                    for task in tasks
                    if task.get("plugin")
                    and (
                        str(task["plugin"].get("plugin_id") or "") == candidate
                        or str(task.get("task_id") or "") == candidate
                    )
                ]
        policy_options = {"timeout_seconds": 3} if partial_navigation else {}
        (
            automation_approval_policy_warning,
            can_manage_approval_policies,
        ) = self._load_automation_project_policies(handler, tasks, **policy_options)
        # Account candidate lists and external resource checks belong to settings.
        # Runtime readiness in the signed catalog still gates every action.
        automation_account_warning = ""
        automation_provider_counts = {
            provider: sum(1 for row in tasks if str(row.get("provider") or "ronghui") == provider)
            for provider in AUTOMATION_PROVIDER_LABELS
        }
        automation_provider_enabled_counts = {
            provider: sum(
                1
                for row in tasks
                if str(row.get("provider") or "ronghui") == provider and row.get("enabled_value")
            )
            for provider in AUTOMATION_PROVIDER_LABELS
        }

        template = self.template_env.get_template("automation.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            settings=self.settings,
            scheduled_tasks=tasks,
            management_module=module,
            source_producers=[{"automation_id": item["automation_id"], "name": item["instance_name"]} for item in automation_plugin_instances],
            management_title={"automation": "自动化", "finance": "财务数据源", "customer_service": "客服数据源"}[module],
            management_url={"automation": "/automations", "finance": "/modules/finance/data-sources", "customer_service": "/modules/customer-service/data-sources"}[module],
            scheduled_task_count=len(tasks),
            enabled_task_count=sum(1 for row in tasks if row.get("enabled_value")),
            automation_provider_labels=AUTOMATION_PROVIDER_LABELS,
            automation_provider_counts=automation_provider_counts,
            automation_provider_enabled_counts=automation_provider_enabled_counts,
            automation_db_warning=automation_db_warning,
            automation_account_warning=automation_account_warning,
            automation_approval_policy_warning=automation_approval_policy_warning,
            can_manage_approval_policies=can_manage_approval_policies,
            automation_plugin_packages=automation_plugin_packages,
            automation_plugin_instances=automation_plugin_instances,
            unsupported_automation_ids=unsupported_automation_ids,
            automation_plugin_warning=automation_plugin_warning,
            automation_catalog_snapshot_notice=automation_catalog_snapshot_notice,
            can_manage_plugins=can_manage_plugins,
            automation_plugin_filter=plugin_filter,
        )
        self._send_html(handler, body)

    def _handle_automation_task_run_now(self, handler: BaseHTTPRequestHandler) -> None:
        ajax_request = self._is_ajax_request(handler)
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        browser_request_uuid = str(
            handler.headers.get("X-Browser-Request-UUID") or ""
        ).strip()
        if not browser_request_uuid:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error": "缺少稳定的浏览器请求标识，无法安全提交命令。",
                },
            )
            return
        payload, override, error_message = self._collect_automation_task_submission(handler)
        if not payload:
            task_id = next(iter(override.keys()), "")
            if ajax_request:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "task_id": task_id,
                        "title": "执行未开始",
                        "message": error_message,
                    },
                )
                return
            self._render_automations(
                handler,
                {"message": [error_message], "kind": ["warning"]},
                task_overrides=override,
                open_task_id=task_id,
            )
            return

        started_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_result = self._start_automation_task_run(
            payload,
            trusted_context=trusted_context,
            browser_request_uuid=browser_request_uuid,
        )
        if not run_result.get("ok"):
            error_code = str(
                run_result.get("error_code") or "COMMAND_SUBMIT_FAILED"
            )
            already_running = error_code == "AUTOMATION_ALREADY_RUNNING"
            failure_message = normalize_feedback_text(
                run_result.get("error") or "智能服务命令提交失败"
            )
            blocking_title, blocking_message = _automation_blocking_feedback(
                run_result.get("data")
            )
            response_payload = {
                "ok": False,
                "pending": False,
                "task_id": payload["task_id"],
                "title": blocking_title if already_running else "立即执行未开始",
                "message": (
                    blocking_message
                    if already_running
                    else failure_message
                ),
                "error": failure_message,
                "error_code": error_code,
            }
            if already_running:
                existing_run = _automation_existing_run_receipt(run_result.get("data"))
                if existing_run is not None:
                    # This identifies the prior Run. The rejected request is
                    # still a 409 and is never presented as newly accepted.
                    response_payload["existing_run"] = existing_run
            if ajax_request:
                try:
                    upstream_status = HTTPStatus(int(run_result.get("status")))
                except (TypeError, ValueError):
                    upstream_status = HTTPStatus.BAD_GATEWAY
                if upstream_status not in {
                    HTTPStatus.CONFLICT,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                }:
                    upstream_status = HTTPStatus.BAD_GATEWAY
                self._send_json(handler, upstream_status, response_payload)
                return
            self._render_automations(
                handler,
                {
                    "message": [
                        blocking_message if already_running else failure_message
                    ],
                    "kind": ["warning"],
                },
                task_overrides=override,
                task_feedbacks={
                    payload["task_id"]: {
                        "kind": "warning",
                        "title": response_payload["title"],
                        "message": response_payload["message"],
                    }
                },
                open_task_id=payload["task_id"],
            )
            return

        receipt = dict(run_result.get("data") or {})
        response_payload = {
            "ok": True,
            "pending": True,
            "task_id": payload["task_id"],
            "title": "执行已发起",
            "message": "脚本直接执行，完成后显示本次结果。",
            "status_label": "等待状态同步",
            "activity_label": "提交时间",
            "activity_value": started_stamp,
            "duration_label": format_duration_label(0),
            "error": "",
            "invocation_id": receipt.get("invocation_id"),
            "next_poll_after_ms": receipt.get("next_poll_after_ms", 1000),
            **invocation_start_feedback(receipt),
        }
        if ajax_request:
            self._send_json(handler, HTTPStatus.ACCEPTED, response_payload)
            return
        self._render_automations(
            handler,
            {"message": [response_payload["message"]], "kind": ["success" if response_payload["ok"] else "warning"]},
            task_overrides=override,
            task_feedbacks={
                payload["task_id"]: {
                    "kind": "info",
                    "title": response_payload["title"],
                    "message": response_payload["message"],
                }
            },
            open_task_id=payload["task_id"],
        )

    def _handle_scan_preview_confirmation(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> None:
        """Submit one explicit formal scan request from a frozen public preview."""

        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        request_id = self._normalize_browser_request_uuid(
            handler.headers.get("X-Browser-Request-UUID")
        )
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id") or "").strip()
        preview_invocation_id = self._normalize_browser_request_uuid(
            values.get("preview_invocation_id")
        )
        if (
            not request_id
            or set(values) != {"task_id", "preview_invocation_id"}
            or not preview_project_id_valid(task_id)
            or not preview_invocation_id
        ):
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error_code": "SCAN_PREVIEW_ID_INVALID",
                    "message": SCAN_PREVIEW_ERROR_MESSAGES[
                        "SCAN_PREVIEW_ID_INVALID"
                    ],
                },
            )
            return

        result = self._start_automation_task_run(
            {
                "task_id": task_id,
                "task_mode": "plugin",
                "project_plugin_instance": True,
                "name": "扫描",
                "tool_name": f"automation.{task_id}.run",
                "tool_params": {},
                "tool_params_json": "{}",
            },
            trusted_context=trusted_context,
            browser_request_uuid=request_id,
            preview_invocation_id=preview_invocation_id,
        )
        if not result.get("ok"):
            error_code = str(result.get("error_code") or "").strip()
            try:
                upstream_status = HTTPStatus(int(result.get("status")))
            except (TypeError, ValueError):
                upstream_status = HTTPStatus.BAD_GATEWAY
            if upstream_status not in {
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.GONE,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
            }:
                upstream_status = HTTPStatus.BAD_GATEWAY
            self._send_json(
                handler,
                upstream_status,
                {
                    "ok": False,
                    "error_code": error_code or "SCAN_PREVIEW_CONFIRMATION_FAILED",
                    "message": scan_preview_error_message(
                        error_code,
                        result.get("error"),
                    ),
                },
            )
            return

        receipt = result.get("data")
        invocation_id = str(receipt.get("invocation_id") or "").strip() if isinstance(receipt, dict) else ""
        if not invocation_id:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error_code": "INVALID_AGENT_RUN_CONTRACT",
                    "message": "智能服务未返回可追踪的正式扫描记录。",
                },
            )
            return
        self._send_json(
            handler,
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "pending": True,
                "title": "正式扫描请求已受理",
                "message": "正式请求已绑定本次预览，后续状态会在当前卡片更新。",
                "invocation_id": invocation_id,
                "next_poll_after_ms": receipt.get("next_poll_after_ms", 1000),
                **invocation_start_feedback(receipt),
            },
        )

    def _handle_selection_preview_start(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> None:
        """Read one live candidate list without writing to the business system."""

        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        request_id = self._normalize_browser_request_uuid(
            handler.headers.get("X-Browser-Request-UUID")
        )
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id") or "").strip()
        if (
            not request_id
            or set(values) != {"task_id"}
            or not preview_project_id_valid(task_id)
        ):
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error_code": "SELECTION_PREVIEW_PROJECT_INVALID",
                    "message": "候选读取请求无效，请刷新页面后重试。",
                },
            )
            return
        result = self._agent_request(
            "POST",
            (
                f"/internal/v1/automation-projects/{quote(task_id, safe='')}"
                "/selection-previews"
            ),
            payload={"request_id": request_id},
            timeout=self.settings.agent_timeout_seconds,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            error_code = str(result.get("error_code") or "").strip()
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error_code": error_code or "SELECTION_PREVIEW_UNAVAILABLE",
                    "message": selection_preview_error_message(
                        error_code, result.get("error")
                    ),
                },
            )
            return
        receipt = result.get("data")
        invocation_id = (
            str(receipt.get("invocation_id") or "").strip()
            if isinstance(receipt, Mapping)
            else ""
        )
        if not self._normalize_browser_request_uuid(invocation_id):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error_code": "INVALID_AGENT_RUN_CONTRACT",
                    "message": "智能服务未返回可追踪的候选读取记录。",
                },
            )
            return
        self._send_json(
            handler,
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "pending": True,
                "title": "正在读取候选运单",
                "message": "正在读取飞书来源表，完成后可直接勾选要处理的运单。",
                "invocation_id": invocation_id,
                "next_poll_after_ms": receipt.get("next_poll_after_ms", 1000),
                **invocation_start_feedback(receipt),
            },
        )

    def _handle_selection_preview_confirmation(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> None:
        """Submit selected bills from one server-verified candidate list."""

        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        request_id = self._normalize_browser_request_uuid(
            handler.headers.get("X-Browser-Request-UUID")
        )
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id") or "").strip()
        preview_invocation_id = self._normalize_browser_request_uuid(
            values.get("preview_invocation_id")
        )
        selected_raw = str(values.get("selected_bill_codes_json") or "")
        try:
            selected = json.loads(selected_raw)
        except (TypeError, ValueError):
            selected = None
        if (
            not request_id
            or set(values)
            != {"task_id", "preview_invocation_id", "selected_bill_codes_json"}
            or not preview_project_id_valid(task_id)
            or not preview_invocation_id
            or not isinstance(selected, list)
            or not selected
            or len(selected) > 10_000
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > 64
                for item in selected
            )
            or len(selected) != len(set(selected))
        ):
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error_code": "SELECTION_INVALID",
                    "message": SELECTION_PREVIEW_ERROR_MESSAGES["SELECTION_INVALID"],
                },
            )
            return
        result = self._agent_request(
            "POST",
            (
                f"/internal/v1/automation-projects/{quote(task_id, safe='')}"
                f"/selection-previews/{quote(preview_invocation_id, safe='')}/confirm"
            ),
            payload={
                "request_id": request_id,
                "selected_bill_codes": [item.strip() for item in selected],
            },
            timeout=self.settings.agent_timeout_seconds,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            error_code = str(result.get("error_code") or "").strip()
            try:
                upstream_status = HTTPStatus(int(result.get("status")))
            except (TypeError, ValueError):
                upstream_status = HTTPStatus.BAD_GATEWAY
            if upstream_status not in {
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.CONFLICT,
                HTTPStatus.GONE,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.SERVICE_UNAVAILABLE,
            }:
                upstream_status = HTTPStatus.BAD_GATEWAY
            self._send_json(
                handler,
                upstream_status,
                {
                    "ok": False,
                    "error_code": error_code or "SELECTION_CONFIRMATION_FAILED",
                    "message": selection_preview_error_message(
                        error_code, result.get("error")
                    ),
                },
            )
            return
        receipt = result.get("data")
        invocation_id = (
            str(receipt.get("invocation_id") or "").strip()
            if isinstance(receipt, Mapping)
            else ""
        )
        if not self._normalize_browser_request_uuid(invocation_id):
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error_code": "INVALID_AGENT_RUN_CONTRACT",
                    "message": "智能服务未返回可追踪的正式处理记录。",
                },
            )
            return
        self._send_json(
            handler,
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "pending": True,
                "title": "所选运单已提交",
                "message": f"已提交 {len(selected)} 票运单，执行结果会在当前卡片更新。",
                "invocation_id": invocation_id,
                "next_poll_after_ms": receipt.get("next_poll_after_ms", 1000),
                **invocation_start_feedback(receipt),
            },
        )

    def _handle_automation_task_cancel(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id", "") or "").strip()
        invocation_id = str(values.get("invocation_id", "") or "").strip()

        if not task_id or not invocation_id:
            self._send_json(
                handler,
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "task_id": task_id,
                    "message": "缺少任务标识，无法取消执行。",
                },
            )
            return

        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        current = self._agent_request(
            "GET", f"/internal/v1/automation-invocations/{quote(invocation_id, safe='')}",
            timeout=5, console_principal=trusted_context.get("_console_principal"),
        )
        current_row = current.get("data") if isinstance(current.get("data"), dict) else {}
        if (not current.get("ok") or current_row.get("invocation_id") != invocation_id
                or not self._automation_project_id(task_id) or current_row.get("automation_id") != task_id):
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {
                "ok": False, "pending": False, "error_code": "INVALID_INVOCATION_RESULT",
                "message": "无法确认这次执行属于当前卡片，取消请求未发送。",
            })
            return
        result = self._agent_request(
            "POST",
            f"/internal/v1/automation-invocations/{quote(invocation_id, safe='')}/cancel",
            payload={},
            timeout=10,
            console_principal=trusted_context.get("_console_principal"),
        )
        if not result.get("ok"):
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": False,
                    "task_id": task_id,
                    "message": normalize_feedback_text(result.get("error") or "取消请求失败"),
                },
            )
            return

        payload = result.get("data")
        if not isinstance(payload, dict):
            payload = {}

        run = payload
        status = str(run.get("status") or "").strip().upper()
        if (run.get("invocation_id") != invocation_id or run.get("automation_id") != task_id
                or status not in INVOCATION_STATUSES):
            self._send_json(handler, HTTPStatus.BAD_GATEWAY, {
                "ok": False, "pending": False, "error_code": "INVALID_INVOCATION_RESULT",
                "message": "取消结果不完整或不匹配，请查看本次执行记录。",
            })
            return
        cancelled = status == "CANCELLED"
        pending = status in {"STARTING", "RUNNING", "CANCELLING"}
        unknown = status == "WRITE_OUTCOME_UNKNOWN"
        self._send_json(
            handler,
            HTTPStatus.ACCEPTED if pending else HTTPStatus.OK,
            {
                "ok": True,
                "task_id": task_id,
                "invocation_id": invocation_id,
                "title": "取消中" if pending else ("已取消" if cancelled else "本次执行已结束"),
                "message": (
                    "已发送取消请求，正在停止本次执行。" if pending else
                    "本次执行已停止，停止前的写入结果尚未确认，请核对实际结果。" if unknown else
                    "本次执行已取消。" if cancelled else "本次执行已经结束。"
                ),
                "activity_value": str(run.get("started_at") or run.get("created_at") or ""),
                "pending": pending,
                "cancel_requested": pending,
                "cancelled": cancelled,
                "status": status,
            },
        )

    def _handle_automation_task_output(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        """Return the result of this exact invocation; never reuse historical output."""
        trusted_context = self._control_plane_read_context(handler)
        if trusted_context is None:
            return
        tool_name = str(query.get("tool_name", [""])[0]).strip()
        task_id = str(query.get("task_id", [""])[0]).strip()
        started_at = str(query.get("started_at", [""])[0]).strip()
        invocation_id = str(query.get("invocation_id", [""])[0]).strip()
        if invocation_id:
            result = self._agent_request(
                "GET",
                f"/internal/v1/automation-invocations/{quote(invocation_id, safe='')}",
                timeout=5,
                console_principal=trusted_context["_console_principal"],
            )
            if not result.get("ok"):
                self._send_json(
                    handler,
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "lines": [],
                        "running": False,
                        "offset": 0,
                        "total": 0,
                        "error": "任务状态暂时无法读取，请稍后刷新。",
                    },
                )
                return
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            run = data
            status = str(run.get("status") or "").upper()
            invocation_phase = str(run.get("invocation_phase") or "run")
            scan_phase = invocation_phase if run.get("plugin_id") == "sync_scan_codes_v2" else "run"
            selection_phase = invocation_phase if run.get("plugin_id") in {
                "self_pickup_problem_upload_v2", "split_pending_problem_upload_v2"
            } else "run"
            execution_phase = str(run.get("execution_phase") or "").strip().lower()
            stage_code = str(run.get("stage_code") or "").strip().upper()
            stage_description = str(run.get("stage_description") or "").strip()
            terminal_statuses = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED", "WRITE_OUTCOME_UNKNOWN"}
            active_statuses = {"STARTING", "RUNNING", "CANCELLING"}
            if (run.get("invocation_id") != invocation_id or not self._automation_project_id(task_id)
                    or run.get("automation_id") != task_id or status not in terminal_statuses | active_statuses):
                self._send_json(handler, HTTPStatus.BAD_GATEWAY, {
                    "lines": [], "running": False, "pending": False, "offset": 0, "total": 0,
                    "error": "本次执行记录不完整或不匹配，请刷新后查看。",
                    "error_code": "INVALID_INVOCATION_RESULT",
                })
                return
            attention_titles = {
                "BLOCKED_DATA": "执行前检查未通过",
                "BLOCKED_LOGIN": "登录已失效",
                "NEEDS_CLARIFICATION": "需要补充信息",
                "FAILED_RETRYABLE": "执行暂时失败",
            }
            public_status_labels = {
                "STARTING": "等待执行",
                "CANCELLING": "正在取消",
                "FAILED": "执行失败",
                "WRITE_OUTCOME_UNKNOWN": "写入结果未确认",
                "RECEIVED": "已收到",
                "PLANNED": "准备执行",
                "WAITING_APPROVAL": "等待审批",
                "APPROVED": "已通过审批",
                "RUNNING": "执行中",
                "VERIFYING": "正在核验",
                "COMPLETED": "已完成",
                "PARTIAL": "部分完成",
                "NEEDS_CLARIFICATION": "需要补充信息",
                "BLOCKED_LOGIN": "登录已失效",
                "BLOCKED_DATA": "执行前检查未通过",
                "FAILED_RETRYABLE": "执行暂时失败",
                "FAILED_TERMINAL": "执行未完成",
                "CANCELLED": "已取消",
            }
            is_terminal = status in terminal_statuses
            awaiting_approval = False
            error_code = str(
                run.get("public_problem_code") or run.get("error_code") or ""
            ).strip().upper()
            problem_titles = {
                "ACCOUNT_LOGIN_REQUIRED": "登录已失效",
                "AUTH_REQUIRED": "登录已失效",
                "IMPACT_PREVIEW_REQUIRED": "执行前检查未通过",
                "RESOURCE_BUSY": "当前资源被占用，本次未执行",
                "RESOURCE_PERMISSION_DENIED": "飞书权限不足",
                "RESOURCE_UNAVAILABLE": "数据位置不可用",
                "RUNTIME_GENERATION_UNSTABLE": "运行环境同步中",
                "SOURCE_SCHEMA_CHANGED": "数据源结构已变化",
                "SOURCE_SHEET_NOT_FOUND": "未找到目标工作表",
                "SOURCE_UNAVAILABLE": "数据源暂时不可达",
                "WRITE_OUTCOME_UNKNOWN": "写入结果待确认",
            }
            attention_title = problem_titles.get(error_code) or attention_titles.get(status, "")
            attention = bool(attention_title)
            attention_message = automation_run_feedback_message(
                error_code=error_code,
                status=status,
            )
            waiting_for_resource = run.get("waiting_for_resource") is True
            if waiting_for_resource:
                stage_description = "正在等待空闲资源，释放后会继续执行，无需重复提交。"
            state_label = stage_description or public_status_labels.get(status, "正在同步")
            lines = invocation_output_lines(run, state_label=state_label)
            is_running = status in active_statuses
            payload: dict[str, Any] = {
                "lines": lines,
                "output_mode": "snapshot",
                "running": is_running,
                "queued": False,
                "pending": status in active_statuses,
                "waiting_for_resource": waiting_for_resource,
                "awaiting_approval": awaiting_approval,
                "cancel_requested": status == "CANCELLING",
                "started_at": str(
                    run.get("stage_started_at")
                    or run.get("started_at")
                    or run.get("created_at")
                    or started_at
                ),
                "offset": len(lines),
                "total": len(lines),
                "invocation_id": invocation_id,
                "automation_id": task_id,
                "invocation_phase": invocation_phase,
                "status": status,
                "collector_navigation": run.get("collector_navigation"),
                "write_receipts": run.get("write_receipts"),
                "execution_phase": execution_phase,
                "stage_code": stage_code,
                "stage_description": stage_description,
                "public_problem_code": str(run.get("public_problem_code") or ""),
                "attention": attention,
                "attention_title": attention_title,
                "attention_message": attention_message,
                "next_poll_after_ms": (
                    0 if attention or is_terminal else data.get("next_poll_after_ms", 1000)
                ),
            }
            if is_terminal:
                cancelled = status == "CANCELLED"
                ok = status == "COMPLETED"
                error_message = (
                    ""
                    if ok
                    else automation_run_feedback_message(
                        error_code=error_code,
                        status=status,
                    )
                )
                payload["runtime"] = {
                    "ok": ok,
                    "cancelled": cancelled,
                    "title": "已完成" if ok else "已取消" if cancelled else "执行未完成",
                    "message": error_message,
                    "last_run": str(run.get("finished_at") or run.get("updated_at") or ""),
                    "duration_label": "",
                    "error": "",
                    "payload": run.get("result") or {},
                }
                if (
                    ok
                    and preview_project_id_valid(task_id)
                    and scan_phase == "preview"
                ):
                    preview_result = self._agent_request(
                        "GET",
                        (
                            f"/internal/v1/automation-projects/{quote(task_id, safe='')}"
                            f"/scan-previews/{quote(invocation_id, safe='')}"
                        ),
                        timeout=5,
                        console_principal=trusted_context["_console_principal"],
                    )
                    if preview_result.get("ok"):
                        projection = normalize_scan_preview_projection(
                            preview_result.get("data"),
                            expected_invocation_id=invocation_id,
                            expected_automation_id=task_id,
                        )
                        if projection is None:
                            payload["scan_preview_error"] = {
                                "error_code": "INVALID_SCAN_PREVIEW_CONTRACT",
                                "message": (
                                    "智能服务返回的扫描预览数据无效，确认执行已阻断。"
                                ),
                            }
                        else:
                            payload["scan_preview"] = projection
                            payload["runtime"]["title"] = "扫描预览已生成"
                            payload["runtime"]["message"] = (
                                "请核对日期、来源记录、待扫描数量和批次数。"
                            )
                    else:
                        error_code = str(
                            preview_result.get("error_code") or ""
                        ).strip()
                        payload["scan_preview_error"] = {
                            "error_code": error_code or "SCAN_PREVIEW_UNAVAILABLE",
                            "message": scan_preview_error_message(
                                error_code,
                                preview_result.get("error"),
                            ),
                        }
                if (
                    ok
                    and preview_project_id_valid(task_id)
                    and selection_phase == "preview"
                ):
                    preview_result = self._agent_request(
                        "GET",
                        (
                            f"/internal/v1/automation-projects/{quote(task_id, safe='')}"
                            f"/selection-previews/{quote(invocation_id, safe='')}"
                        ),
                        timeout=5,
                        console_principal=trusted_context["_console_principal"],
                    )
                    if preview_result.get("ok"):
                        projection = normalize_selection_preview_projection(
                            preview_result.get("data"),
                            expected_automation_id=task_id,
                            expected_invocation_id=invocation_id,
                        )
                        if projection is None:
                            payload["selection_preview_error"] = {
                                "error_code": "INVALID_SELECTION_PREVIEW_CONTRACT",
                                "message": "候选清单格式无效，确认处理已阻断。",
                            }
                        else:
                            payload["selection_preview"] = projection
                            payload["runtime"]["title"] = "候选运单已读取"
                            payload["runtime"]["message"] = (
                                "请勾选本次要处理的运单，再确认提交。"
                            )
                    else:
                        error_code = str(
                            preview_result.get("error_code") or ""
                        ).strip()
                        payload["selection_preview_error"] = {
                            "error_code": error_code or "SELECTION_PREVIEW_UNAVAILABLE",
                            "message": selection_preview_error_message(
                                error_code, preview_result.get("error")
                            ),
                        }
                elif (
                    not ok
                    and preview_project_id_valid(task_id)
                    and selection_phase == "preview"
                ):
                    preview_error_code = str(
                        run.get("public_problem_code") or run.get("error_code") or ""
                    ).strip()
                    preview_message = selection_preview_error_message(
                        preview_error_code,
                        automation_run_feedback_message(
                            error_code=preview_error_code,
                            status=status,
                        ),
                    )
                    payload["selection_preview_error"] = {
                        "error_code": preview_error_code or "SELECTION_PREVIEW_UNAVAILABLE",
                        "message": preview_message,
                    }
                    payload["runtime"]["title"] = "候选读取失败"
                    payload["runtime"]["message"] = preview_message
            if (
                attention
                and preview_project_id_valid(task_id)
                and selection_phase == "preview"
                and "selection_preview_error" not in payload
            ):
                preview_error_code = str(
                    run.get("public_problem_code") or run.get("error_code") or ""
                ).strip()
                preview_message = selection_preview_error_message(
                    preview_error_code,
                    automation_run_feedback_message(
                        error_code=preview_error_code,
                        status=status,
                    ),
                )
                payload["selection_preview_error"] = {
                    "error_code": preview_error_code or "SELECTION_PREVIEW_UNAVAILABLE",
                    "message": preview_message,
                }
                payload["runtime"] = {
                    "ok": False,
                    "cancelled": False,
                    "title": "候选读取失败",
                    "message": preview_message,
                    "last_run": str(run.get("updated_at") or ""),
                    "duration_label": "",
                    "error": "",
                    "payload": run.get("result") or {},
                }
                payload["pending"] = False
            self._send_json(handler, HTTPStatus.OK, payload)
            return
        self._send_json(handler, HTTPStatus.BAD_REQUEST, {
            "error": "缺少本次执行标识，请从插件卡片发起新执行。",
            "running": False, "pending": False, "lines": [], "offset": 0, "total": 0,
        })

    def _collect_automation_task_submission(
        self,
        handler: BaseHTTPRequestHandler,
    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str]:
        """Accept only an installed project; Agent owns its saved configuration."""
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id", "") or "").strip()
        name = str(values.get("name", "") or "").strip()
        project_plugin_instance = str(
            values.get("project_plugin_instance", "") or ""
        ).strip().lower() in {"1", "on", "true", "yes"}
        raw_contribution_id = values.get("contribution_id", "")
        if isinstance(raw_contribution_id, list):
            return None, {}, "一次只能选择一个自动化手动入口。"
        contribution_id = str(raw_contribution_id or "").strip().lower()
        if contribution_id and not AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE.fullmatch(
            contribution_id
        ):
            return None, {}, "自动化手动入口无效。"
        override = {task_id: {"name": name}}
        if not project_plugin_instance or not self._automation_project_id(task_id):
            return None, override, "自动化项目标识无效。"
        if not name:
            return None, override, "自动化项目名称不能为空。"
        return (
            {
                "task_id": task_id,
                "name": name,
                "tool_name": f"automation.{task_id}.run",
                "tool_params": {},
                "tool_params_json": "{}",
                "project_plugin_instance": True,
                "contribution_id": contribution_id,
            },
            override,
            "",
        )

    def _is_ajax_request(self, handler: BaseHTTPRequestHandler) -> bool:
        requested_with = str(handler.headers.get("X-Requested-With", "") or "").strip().lower()
        accept_header = str(handler.headers.get("Accept", "") or "").strip().lower()
        return requested_with in {"xmlhttprequest", "fetch"} or "application/json" in accept_header
