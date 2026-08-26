"""Console application services grouped by business responsibility."""

import uuid
from typing import Any, Mapping

from console.app_support import *  # noqa: F403
from console.services.automation_projects import *  # noqa: F403


SCAN_PREVIEW_PROJECT_ID = "scan_codes"
SCAN_PREVIEW_PUBLIC_FIELDS = frozenset(
    {
        "contract_version",
        "preview_run_id",
        "target_date",
        "observed_at",
        "expires_at",
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
        "can_confirm",
    }
)
SCAN_PREVIEW_ERROR_MESSAGES = {
    "SCAN_PREVIEW_ID_INVALID": "扫描预览标识无效，请重新生成预览。",
    "SCAN_PREVIEW_NOT_FOUND": "扫描预览不存在，请重新生成预览。",
    "SCAN_PREVIEW_INCOMPLETE": "扫描预览尚未完整生成，请重新生成预览。",
    "SCAN_PREVIEW_INVALID": "扫描预览证据无效，请重新生成预览。",
    "SCAN_PREVIEW_EXPIRED": "扫描预览已超过十五分钟，请重新生成预览。",
    "SCAN_PREVIEW_STALE": "扫描数据已变化，请重新生成预览后再确认。",
    "PROJECT_INVOCATION_STALE": "项目配置已变化，请重新生成预览后再确认。",
    "SCAN_PREVIEW_ALREADY_CONSUMED": "该预览已提交过正式请求，请查询原 Run，不要重复执行。",
    "REQUEST_ID_REUSED": "本次请求标识已被使用，请重新点击确认。",
    "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED": "正式扫描尚未开放，本次没有写入第三方系统。",
    "SCAN_PREVIEW_CONTEXT_REQUIRED": "服务端扫描合同缺少预览上下文，正式执行已阻断。",
    "SCAN_PREVIEW_CONTEXT_INVALID": "服务端扫描合同与预览不一致，正式执行已阻断。",
}


def normalize_scan_preview_projection(
    raw: Any,
    *,
    expected_run_id: str,
) -> dict[str, Any] | None:
    """Accept only the frozen public scan preview contract."""

    if not isinstance(raw, Mapping) or set(raw) != SCAN_PREVIEW_PUBLIC_FIELDS:
        return None
    preview_run_id = str(raw.get("preview_run_id") or "").strip()
    try:
        normalized_preview_run_id = str(uuid.UUID(preview_run_id))
    except (ValueError, AttributeError):
        return None
    if normalized_preview_run_id != preview_run_id or preview_run_id != expected_run_id:
        return None
    if raw.get("contract_version") != 1 or not isinstance(raw.get("can_confirm"), bool):
        return None
    target_date = str(raw.get("target_date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
        return None
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return None
    timestamps: dict[str, str] = {}
    for field in ("observed_at", "expires_at"):
        value = str(raw.get(field) or "").strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if not value or len(value) > 64 or parsed.tzinfo is None:
            return None
        timestamps[field] = value
    counts: dict[str, int] = {}
    for field in (
        "source_page_count",
        "normalized_record_count",
        "selection_count",
        "batch_count",
    ):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        counts[field] = value
    return {
        "contract_version": 1,
        "preview_run_id": preview_run_id,
        "target_date": target_date,
        **timestamps,
        **counts,
        "can_confirm": raw["can_confirm"],
    }


def scan_preview_error_message(error_code: Any, fallback: Any = "") -> str:
    code = str(error_code or "").strip()
    if code in SCAN_PREVIEW_ERROR_MESSAGES:
        return SCAN_PREVIEW_ERROR_MESSAGES[code]
    return normalize_feedback_text(fallback or "扫描预览当前不可用，请重新生成。")


def group_scheduled_rows_by_automation_id(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group only by the persisted project identity; never infer it from task IDs."""

    groups: list[dict[str, Any]] = []
    linked_groups: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        automation_id = str(row.get("automation_id") or "").strip()
        if AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id):
            group = linked_groups.get(automation_id)
            if group is None:
                group = {
                    "storage_key": automation_id,
                    "task_id": automation_id,
                    "missing_automation_id": False,
                    "rows": [],
                }
                linked_groups[automation_id] = group
                groups.append(group)
            group["rows"].append(row)
            continue

        task_id = str(row.get("id") or "").strip()
        if not task_id:
            task_id = f"unlinked_scheduled_task_{index + 1}"
        groups.append(
            {
                "storage_key": f"__unlinked_scheduled_task__:{index}",
                "task_id": task_id,
                "missing_automation_id": True,
                "rows": [row],
            }
        )
    return groups


def _automation_plugin_block_warning(plugin: Mapping[str, Any]) -> str:
    """Keep configuration closure distinct from an immutable runtime transition."""

    if plugin.get("configured") is not True:
        return AUTOMATION_RUNTIME_REASON_LABELS[
            "PROJECT_CONFIGURATION_INCOMPLETE"
        ]
    enabled_entrypoints = plugin.get("enabled_entrypoints")
    if isinstance(enabled_entrypoints, list) and not enabled_entrypoints:
        return AUTOMATION_RUNTIME_REASON_LABELS["ENTRYPOINTS_DISABLED"]
    missing = [
        str(item).strip()
        for item in plugin.get("missing_requirements") or []
        if str(item).strip()
    ]
    if missing:
        if any(
            "合同" in item
            or "Schema" in item
            or "投影" in item
            or "运行描述符" in item
            for item in missing
        ):
            return AUTOMATION_RUNTIME_CONTRACT_ERROR_LABEL
        return "；".join(dict.fromkeys(missing))
    state = str(plugin.get("state") or "UNKNOWN").upper()
    if state not in AUTOMATION_PLUGIN_STABLE_STATES:
        reconcile_state = str(plugin.get("reconcile_state") or "UNKNOWN").upper()
        if reconcile_state == "BLOCKED_UNKNOWN_WRITE":
            return (
                "上次运行的保存结果无法确认。为防止重复写入，任务已暂停。"
                "请先核对业务表格，再检查结果并恢复。"
            )
        if reconcile_state == "ERROR":
            return "运行环境准备失败，任务已暂停。请联系管理员检查后再恢复。"
        return "运行环境正在更新，任务暂时不可运行。已有设置和自动执行状态会保留。"
    return AUTOMATION_RUNTIME_REASON_LABELS["PROJECT_RUNTIME_UNAVAILABLE"]


class AutomationServiceMixin(AutomationProjectsServiceMixin):
    def _build_virtual_automation_task(
        self,
        task_id: str,
        *,
        override: dict[str, Any] | None = None,
        feedback: dict[str, Any] | None = None,
        open_task_id: str | None = None,
        workflow_resources: dict[str, dict[str, Any]] | None = None,
        resource_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        override = override or {}
        workflow = automation_workflow_definition(task_id)
        runtime_state = self.automation_virtual_task_state.get(task_id, {})
        baseline = build_virtual_task_defaults(task_id)
        workflow_resources = workflow_resources or {}
        resource_overrides = resource_overrides or {}
        webhook_info = resolve_automation_webhook_info(
            task_id,
            workflow_resources=workflow_resources,
            agent_base_url=self.settings.agent_base_url,
        )
        resource_bindings = build_automation_resource_bindings(
            task_id,
            workflow_resources,
            resource_overrides=resource_overrides,
        )
        missing_required_resources = [
            item["resource_key"]
            for item in resource_bindings
            if item.get("missing") and item.get("required")
        ]
        missing_required_resource_labels = [
            item.get("display_name") or item["resource_key"]
            for item in resource_bindings
            if item.get("missing") and item.get("required")
        ]
        tool_params = runtime_state.get("tool_params")
        if not isinstance(tool_params, dict):
            tool_params = dict(baseline.get("tool_params") or {})
        raw_json = override.get("tool_params_json")
        if raw_json is None:
            raw_json = runtime_state.get("tool_params_json")
        if raw_json is None:
            raw_json = json.dumps(tool_params, ensure_ascii=False, indent=2)

        name_value = str(
            override.get("name")
            or runtime_state.get("name")
            or workflow.get("display_name")
            or baseline.get("name")
            or task_id
        )
        tool_name_value = str(
            override.get("tool_name")
            or runtime_state.get("tool_name")
            or workflow.get("tool_name")
            or baseline.get("tool_name")
            or ""
        )
        provider_value = automation_task_provider(task_id, workflow, tool_name_value)
        last_run_value = str(runtime_state.get("last_run") or "")
        last_status = str(runtime_state.get("last_status") or "")
        last_duration_ms = runtime_state.get("last_duration_ms")
        last_message_value = str(runtime_state.get("last_message") or "")
        task_mode_value = str(workflow.get("task_mode") or baseline.get("task_mode") or "manual")
        is_schedulable = task_mode_value == "scheduled"
        control_plane_only = automation_task_control_plane_only(task_id)
        trigger_label = str(workflow.get("trigger_label") or "手动执行")
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

        cron_values: list[str] = []
        if override.get("cron_expression"):
            cron_values = [
                item.strip()
                for item in str(override.get("cron_expression", "") or "").splitlines()
                if item.strip()
            ]
        if override_schedule_times:
            cron_values = [build_daily_cron_expression(item) or "" for item in override_schedule_times]
        if not cron_values and is_schedulable:
            default_schedule_times = workflow.get("default_schedule_times")
            if isinstance(default_schedule_times, list):
                cron_values = [
                    build_daily_cron_expression(str(item or "").strip()) or ""
                    for item in default_schedule_times
                    if str(item or "").strip()
                ]

        schedule_info = summarize_task_group_schedule(cron_values) if is_schedulable else {
            "supported": False,
            "time_values": [],
            "summary": str(workflow.get("schedule_summary") or trigger_label),
            "warning": "",
            "raw_value": "",
        }
        schedule_supported = bool(schedule_info.get("supported")) or (is_schedulable and not schedule_info.get("raw_value"))
        schedule_summary = str(schedule_info.get("summary") or workflow.get("schedule_summary") or trigger_label)
        schedule_warning = str(schedule_info.get("warning") or "")
        if is_schedulable and not schedule_info.get("raw_value"):
            schedule_warning = "当前未设置定时；可先手动执行，也可保存后启用定时。"

        return {
            "id": task_id,
            "note": str(workflow.get("note") or automation_task_note(task_id)),
            "provider": provider_value,
            "provider_label": automation_provider_label(provider_value),
            "system_badges": list(workflow.get("system_badges") or []),
            "task_id": task_id,
            "task_mode": task_mode_value,
            "display_task_id": task_id,
            "group_size": 1,
            "task_ids": [],
            "task_cron_expressions": {},
            "name_value": name_value,
            "tool_name_value": tool_name_value,
            "cron_expression_value": str(schedule_info.get("raw_value") or ""),
            "enabled_value": bool(override.get("enabled")) if is_schedulable else False,
            "tool_params_json": raw_json,
            "tool_param_fields": flatten_automation_fields(tool_params),
            "schedule_supported": schedule_supported if is_schedulable else False,
            "schedule_editable": is_schedulable and not control_plane_only,
            "schedule_time_values": list(schedule_info.get("time_values") or []) if is_schedulable else [],
            "schedule_summary": schedule_summary,
            "schedule_icon": "clock" if is_schedulable else "zap",
            "schedule_warning": schedule_warning if is_schedulable else f"此流程不写入 scheduled_tasks，默认通过{trigger_label}触发。",
            "trigger_label": trigger_label,
            "is_schedulable": is_schedulable,
            "can_save": not control_plane_only,
            "can_run_now": not control_plane_only,
            "control_plane_only": control_plane_only,
            "control_plane_notice": (
                CONTROL_PLANE_ONLY_AUTOMATION_MESSAGE if control_plane_only else ""
            ),
            "has_webhook": bool(webhook_info.get("path")),
            "webhook_path": str(webhook_info.get("path") or ""),
            "webhook_full_url": str(webhook_info.get("full_url") or ""),
            "webhook_masked_url": str(webhook_info.get("masked_url") or ""),
            "webhook_token_enabled": bool(webhook_info.get("token_enabled")),
            "webhook_header_name": str(webhook_info.get("header_name") or ""),
            "webhook_body_json": json.dumps(webhook_info.get("body_example") or {}, ensure_ascii=False, indent=2),
            "resource_bindings": resource_bindings,
            "missing_required_resources": missing_required_resources,
            "missing_required_resource_labels": missing_required_resource_labels,
            "resource_blocked": bool(missing_required_resources),
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
                    str(workflow.get("note") or ""),
                    tool_name_value,
                    task_id,
                    trigger_label,
                    automation_provider_label(provider_value),
                )
                if item
            ),
            "sort_order": int(workflow.get("order") or 999),
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

    def _finalize_automation_task_run(
        self,
        payload: dict[str, Any],
        run_result: dict[str, Any],
        *,
        started_at: float,
    ) -> dict[str, Any]:
        tool_response = run_result.get("data") if isinstance(run_result.get("data"), dict) else {}
        run_ok = bool(run_result.get("ok") and tool_response.get("success"))
        run_cancelled = bool(tool_response.get("canceled"))
        duration_s = tool_response.get("duration_s")
        try:
            duration_ms = (
                int(round(float(duration_s) * 1000))
                if duration_s is not None
                else int(round((time.perf_counter() - started_at) * 1000))
            )
        except (TypeError, ValueError):
            duration_ms = int(round((time.perf_counter() - started_at) * 1000))
        run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        error_message_full = normalize_feedback_text(
            tool_response.get("error") or run_result.get("error") or ""
        )
        error_message_value = shorten_error_message(error_message_full)
        feedback_meta = automation_runtime_feedback_meta(
            ok=run_ok,
            cancelled=run_cancelled,
            success_message=payload["name"],
            error_message=error_message_value,
        )

        if payload.get("task_mode") == "scheduled" and not payload.get(
            "project_plugin_instance"
        ):
            self.repository.update_scheduled_task_runtime(
                base_task_id=payload["task_id"],
                last_run=run_time,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if run_ok else error_message_full,
            )
        else:
            self._record_virtual_task_runtime(
                payload["task_id"],
                payload=payload,
                last_run=run_time,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if run_ok else error_message_full,
            )

        return {
            "ok": run_ok,
            "cancelled": run_cancelled,
            "task_id": payload["task_id"],
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "status_label": feedback_meta["status_label"],
            "activity_label": "最近运行",
            "activity_value": run_time,
            "duration_label": format_duration_label(duration_ms),
            "error": error_message_full,
            "payload": tool_response.get("data") if run_ok else run_result,
        }

    def _start_automation_task_run(
        self,
        payload: dict[str, Any],
        *,
        trusted_context: dict[str, Any],
        browser_request_uuid: str,
        preview_run_id: str | None = None,
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
        if preview_run_id is not None:
            safe_preview_run_id = self._normalize_browser_request_uuid(preview_run_id)
            if automation_id != SCAN_PREVIEW_PROJECT_ID or not safe_preview_run_id:
                return {
                    "ok": False,
                    "status": HTTPStatus.BAD_REQUEST,
                    "error": SCAN_PREVIEW_ERROR_MESSAGES[
                        "SCAN_PREVIEW_ID_INVALID"
                    ],
                    "error_code": "SCAN_PREVIEW_ID_INVALID",
                }
            invoke_payload["preview_run_id"] = safe_preview_run_id
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
        run_id = str(receipt.get("run_id") or "").strip() if isinstance(receipt, dict) else ""
        if not run_id:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_GATEWAY,
                "error": "Agent 未返回可追踪的 run_id。",
                "error_code": "INVALID_AGENT_RUN_CONTRACT",
            }

        started_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if payload.get("task_mode") == "scheduled" and not payload.get(
            "project_plugin_instance"
        ):
            self.repository.update_scheduled_task_runtime(
                base_task_id=payload["task_id"],
                last_run=started_stamp,
                last_status="running",
                last_duration_ms=None,
                last_message="",
            )
        else:
            self._record_virtual_task_runtime(
                payload["task_id"],
                payload=payload,
                last_run=started_stamp,
                last_status="running",
                last_duration_ms=None,
                last_message="",
            )
        state = dict(self.automation_virtual_task_state.get(payload["task_id"], {}))
        state.update(
            {
                "run_id": run_id,
                "task_mode": (
                    "plugin"
                    if payload.get("project_plugin_instance")
                    else payload.get("task_mode")
                ),
                "last_run": started_stamp,
                "last_status": "running",
            }
        )
        self.automation_virtual_task_state[payload["task_id"]] = state
        return run_result

    def _render_automations(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict,
        *,
        resource_overrides: dict[str, dict[str, Any]] | None = None,
        task_overrides: dict[str, dict[str, Any]] | None = None,
        task_feedbacks: dict[str, dict[str, Any]] | None = None,
        open_task_id: str | None = None,
    ) -> None:
        resource_overrides = resource_overrides or {}
        task_overrides = task_overrides or {}
        task_feedbacks = task_feedbacks or {}

        automation_db_warning = ""
        try:
            scheduled_rows = self.repository.list_scheduled_tasks()
            workflow_resource_rows = self.repository.list_workflow_resources()
        except Exception as exc:
            LOGGER.warning("Failed to load automations from repository: %s", exc)
            scheduled_rows = []
            workflow_resource_rows = []
            automation_db_warning = normalize_feedback_text(
                f"自动化任务数据库当前不可达，任务列表已临时降级为空。详情：{exc}"
            )

        (
            automation_plugin_packages,
            automation_plugin_instances,
            automation_workers,
            unsupported_automation_ids,
            hidden_automation_ids,
            automation_plugin_warning,
            can_manage_plugins,
        ) = self._load_automation_plugin_catalog(handler)
        plugin_instances_by_id = {
            str(item["automation_id"]): item for item in automation_plugin_instances
        }
        scheduled_row_groups = group_scheduled_rows_by_automation_id(scheduled_rows)
        workflow_resources = {
            str(item.get("resource_key", "") or ""): item
            for item in workflow_resource_rows
        }

        tasks_by_id: dict[str, dict[str, Any]] = {}
        for scheduled_group in scheduled_row_groups:
            base_task_id = str(scheduled_group["task_id"])
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
            workflow = automation_workflow_definition(base_task_id)
            control_plane_only = automation_task_control_plane_only(base_task_id)
            override = task_overrides.get(base_task_id, {})
            tool_params = primary_row.get("tool_params", {})
            default_tool_params = workflow.get("default_tool_params")
            if isinstance(default_tool_params, dict) and isinstance(tool_params, dict):
                merged_tool_params = json.loads(json.dumps(default_tool_params, ensure_ascii=False))
                merged_tool_params.update(tool_params)
                tool_params = merged_tool_params
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
                or workflow.get("display_name")
                or normalize_task_display_name(str(primary_row.get("name", "") or ""))
            )
            tool_name_value = str(override.get("tool_name") or primary_row.get("tool_name", "") or "")
            if control_plane_only:
                tool_name_value = str(workflow.get("tool_name") or tool_name_value)
            provider_value = automation_task_provider(base_task_id, workflow, tool_name_value)
            resource_bindings = build_automation_resource_bindings(
                base_task_id,
                workflow_resources,
                resource_overrides=resource_overrides,
            )
            missing_required_resources = [
                item["resource_key"]
                for item in resource_bindings
                if item.get("missing") and item.get("required")
            ]
            missing_required_resource_labels = [
                item.get("display_name") or item["resource_key"]
                for item in resource_bindings
                if item.get("missing") and item.get("required")
            ]
            tasks_by_id[str(scheduled_group["storage_key"])] = {
                **primary_row,
                "note": str(workflow.get("note") or automation_task_note(base_task_id)),
                "provider": provider_value,
                "provider_label": automation_provider_label(provider_value),
                "system_badges": list(workflow.get("system_badges") or []),
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
                "schedule_editable": not control_plane_only,
                "schedule_time_values": schedule_info["time_values"],
                "schedule_summary": schedule_info["summary"],
                "schedule_icon": "clock",
                "schedule_warning": schedule_info["warning"],
                "trigger_label": str(workflow.get("trigger_label") or "定时任务"),
                "is_schedulable": True,
                "can_save": not control_plane_only,
                "can_run_now": not control_plane_only,
                "control_plane_only": control_plane_only,
                "control_plane_notice": (
                    CONTROL_PLANE_ONLY_AUTOMATION_MESSAGE
                    if control_plane_only
                    else ""
                ),
                "has_webhook": False,
                "webhook_path": "",
                "webhook_full_url": "",
                "webhook_masked_url": "",
                "webhook_token_enabled": False,
                "webhook_header_name": webhook_token_header_name(),
                "webhook_body_json": "{}",
                "resource_bindings": resource_bindings,
                "missing_required_resources": missing_required_resources,
                "missing_required_resource_labels": missing_required_resource_labels,
                "resource_blocked": bool(missing_required_resources),
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
                        str(workflow.get("note") or automation_task_note(base_task_id)),
                        tool_name_value,
                        base_task_id,
                        automation_provider_label(provider_value),
                    )
                    if item
                ),
                "sort_order": int(workflow.get("order") or 999),
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
                workflow_resources=workflow_resources,
                resource_overrides=resource_overrides,
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
            if plugin is None:
                task["can_save"] = False
                task["can_run_now"] = False
                task["schedule_editable"] = False
                task["plugin_blocked"] = True
                task["plugin_warning"] = (
                    "迁移/插件缺失：定时任务未关联 automation_id，已作为独立阻断项显示，禁止按任务名推断归属。"
                    if task.get("automation_link_missing")
                    else "迁移/插件缺失：该任务不在 Agent 已安装实例目录中，运行与配置均已阻断。"
                )
                continue

            instance_name = str(plugin.get("instance_name") or "").strip()
            workflow_name = str(
                automation_workflow_definition(automation_id).get("display_name") or ""
            ).strip()
            task["name_value"] = (
                workflow_name
                if not instance_name or instance_name == automation_id
                else instance_name
            ) or str(task.get("name_value") or automation_id)
            task["display_task_id"] = automation_id
            task["plugin_blocked"] = bool(plugin.get("blocked"))
            task["control_plane_only"] = False
            task["control_plane_notice"] = ""
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
            stable_state = str(plugin.get("state") or "") in AUTOMATION_PLUGIN_STABLE_STATES
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
                            "旧定时不符合签名动作包的调度合同，禁止猜测迁移",
                        ]
                    )
                )

            if plugin.get("execution_platform") == "windows":
                bound_device_id = str((plugin.get("device") or {}).get("device_id") or "")
                bound_worker = next(
                    (
                        worker
                        for worker in automation_workers
                        if str(worker.get("device_id") or "") == bound_device_id
                    ),
                    None,
                )
                if bound_device_id and (
                    bound_worker is None or not bool(bound_worker.get("binding_usable"))
                ):
                    task["plugin_blocked"] = True
                    plugin["blocked"] = True
                    plugin["missing_requirements"] = list(
                        dict.fromkeys(
                            [
                                *list(plugin.get("missing_requirements") or []),
                                "已绑定 Windows Worker 不在线或会话不可用",
                            ]
                        )
                    )
            base_runnable = bool(
                plugin.get("enabled") and plugin.get("configured") and not plugin.get("blocked")
            )
            console_enabled = "console" in set(plugin.get("enabled_entrypoints") or [])
            task["can_run_now"] = bool(base_runnable and console_enabled)
            task["run_disabled_reason"] = (
                "后台入口已关闭" if base_runnable and not console_enabled else "当前不可执行"
            )
            task["plugin_worker_options"] = (
                automation_workers if plugin.get("execution_platform") == "windows" else []
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
                    str(plugin.get("execution_platform") or ""),
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
        (
            automation_approval_policy_warning,
            can_manage_approval_policies,
        ) = self._load_automation_project_policies(handler, tasks)
        automation_accounts, automation_account_warning = self._fetch_automation_accounts(
            force=False,
            prefer_cached=True,
        )
        self._enrich_automation_tasks_with_accounts(tasks, automation_accounts)
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
            automation_workers=automation_workers,
            unsupported_automation_ids=unsupported_automation_ids,
            automation_plugin_warning=automation_plugin_warning,
            can_manage_plugins=can_manage_plugins,
        )
        self._send_html(handler, body)

    def _handle_automation_resource_save(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        ajax_request = self._is_ajax_request(handler)
        resources_json = str(values.get("resources_json", "") or "").strip()
        resource_key = str(values.get("resource_key", "") or "").strip()
        config_json = str(values.get("config_json", "") or "").strip()
        open_task_id = str(values.get("task_id", "") or "").strip()

        if resources_json:
            try:
                resource_payload = json.loads(resources_json)
            except json.JSONDecodeError as exc:
                message = f"批量资源 JSON 解析失败：{exc.msg}"
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._redirect_with_message(handler, "/automations", message, "warning")
                return

            if not isinstance(resource_payload, dict):
                message = "批量资源必须是 JSON 对象。"
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._redirect_with_message(handler, "/automations", message, "warning")
                return

            parsed_resources: dict[str, dict[str, Any]] = {}
            resource_overrides: dict[str, dict[str, Any]] = {}
            errors: list[str] = []
            for raw_key, raw_json in resource_payload.items():
                key = str(raw_key or "").strip()
                config_text = str(raw_json or "").strip()
                if not key or not config_text:
                    continue
                resource_overrides[key] = {"config_json": config_text}
                try:
                    parsed_config = json.loads(config_text)
                except json.JSONDecodeError as exc:
                    errors.append(f"{key}: JSON 解析失败：{exc.msg}")
                    continue
                if not isinstance(parsed_config, dict):
                    errors.append(f"{key}: 必须是 JSON 对象")
                    continue
                parsed_resources[key] = parsed_config

            if errors:
                message = "；".join(errors)
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._render_automations(
                    handler,
                    {"message": [message], "kind": ["warning"]},
                    resource_overrides=resource_overrides,
                    open_task_id=open_task_id or None,
                )
                return

            if not parsed_resources:
                message = "没有需要保存的资源。"
                if ajax_request:
                    self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": message})
                    return
                self._redirect_with_message(handler, "/automations", message, "warning")
                return

            for key, config in parsed_resources.items():
                self.repository.upsert_workflow_resource(key, config, source="backend_console")

            message = f"已保存 {len(parsed_resources)} 个资源。"
            if ajax_request:
                self._send_json(
                    handler,
                    HTTPStatus.OK,
                    {"ok": True, "message": message, "saved": sorted(parsed_resources)},
                )
                return
            self._redirect_with_message(handler, "/automations", message, "success")
            return

        if not resource_key:
            self._redirect_with_message(handler, "/automations", "缺少资源标识。", "warning")
            return

        try:
            config = json.loads(config_json or "{}")
        except json.JSONDecodeError as exc:
            self._render_automations(
                handler,
                {"message": [f"资源 {resource_key} 的 JSON 解析失败：{exc.msg}"], "kind": ["warning"]},
                resource_overrides={resource_key: {"config_json": config_json}},
                open_task_id=open_task_id or None,
            )
            return

        if not isinstance(config, dict):
            self._render_automations(
                handler,
                {"message": [f"资源 {resource_key} 必须是 JSON 对象。"], "kind": ["warning"]},
                resource_overrides={resource_key: {"config_json": config_json}},
                open_task_id=open_task_id or None,
            )
            return

        self.repository.upsert_workflow_resource(resource_key, config, source="backend_console")
        self._redirect_with_message(handler, "/automations", f"资源已保存：{resource_key}", "success")

    def _handle_automation_task_save(self, handler: BaseHTTPRequestHandler) -> None:
        ajax_request = self._is_ajax_request(handler)
        payload, override, error_message = self._collect_automation_task_submission(handler)
        if not payload:
            task_id = next(iter(override.keys()), "")
            if ajax_request:
                self._send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "message": error_message})
                return
            self._render_automations(
                handler,
                {"message": [error_message], "kind": ["warning"]},
                task_overrides=override,
                open_task_id=task_id,
            )
            return

        if payload.get("project_plugin_instance"):
            message = "插件项目设置只能通过当前卡片的“保存项目设置”提交到 Agent。"
            if ajax_request:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "task_id": payload.get("task_id", ""), "message": message},
                )
                return
            self._redirect_with_message(handler, "/automations", message, "warning")
            return

        reload_result = self._persist_automation_task(payload)
        save_message = str(payload.get("save_message") or "").strip()
        success_message = f"已保存：{payload['name']}"
        if save_message:
            success_message = f"{success_message}；{save_message}"

        if ajax_request:
            ok = reload_result.get("ok", True) if payload.get("task_mode") == "scheduled" else True
            self._send_json(handler, HTTPStatus.OK, {
                "ok": ok,
                "task_id": payload.get("task_id", ""),
                "message": success_message,
            })
            return

        if payload.get("task_mode") != "scheduled":
            self._redirect_with_message(handler, "/automations", success_message.replace("已保存", "任务配置已保存", 1), "success")
            return
        if not reload_result.get("ok"):
            self._redirect_with_message(
                handler,
                "/automations",
                f"任务已保存。Agent 当前未连接，稍后重载后生效：{reload_result.get('error', 'unknown error')}",
                "warning",
            )
            return

        self._redirect_with_message(handler, "/automations", success_message.replace("已保存", "任务已保存", 1), "success")

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
        payload, override, error_message = self._collect_automation_task_submission(
            handler,
            allow_missing_schedule=True,
        )
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

        missing_required_resources: list[str] = []
        if not payload.get("project_plugin_instance"):
            try:
                workflow_resource_rows = self.repository.list_workflow_resources()
            except Exception:
                workflow_resource_rows = []
            workflow_resources = {
                str(item.get("resource_key", "") or ""): item
                for item in workflow_resource_rows
            }
            resource_bindings = build_automation_resource_bindings(
                payload["task_id"], workflow_resources
            )
            missing_required_resources = [
                item.get("display_name") or item["resource_key"]
                for item in resource_bindings
                if item.get("missing") and item.get("required")
            ]
        if missing_required_resources:
            message = (
                "缺少运行资源："
                + "、".join(missing_required_resources)
                + "。请先在当前任务设置中补齐资源配置。"
            )
            if ajax_request:
                self._send_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "task_id": payload["task_id"],
                        "title": "执行未开始",
                        "message": message,
                    },
                )
                return
            self._render_automations(
                handler,
                {"message": [message], "kind": ["warning"]},
                task_overrides=override,
                open_task_id=payload["task_id"],
            )
            return

        started_at = time.perf_counter()
        started_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reload_result = (
            {"ok": True, "data": {"mode": "agent_project_configuration"}}
            if payload.get("project_plugin_instance")
            else self._persist_automation_task(payload)
        )
        if not reload_result.get("ok"):
            failure_message = normalize_feedback_text(reload_result.get("error", "unknown error"))
            duration_ms = int(round((time.perf_counter() - started_at) * 1000))
            if payload.get("task_mode") == "scheduled":
                self.repository.update_scheduled_task_runtime(
                    base_task_id=payload["task_id"],
                    last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    last_status="error",
                    last_duration_ms=duration_ms,
                    last_message=failure_message,
                )
            else:
                self._record_virtual_task_runtime(
                    payload["task_id"],
                    payload=payload,
                    last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    last_status="error",
                    last_duration_ms=duration_ms,
                    last_message=failure_message,
                )
            response_payload = {
                "ok": False,
                "task_id": payload["task_id"],
                "title": "立即执行未开始",
                "message": failure_message,
                "status_label": "执行失败",
                "activity_label": "最近运行",
                "activity_value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "duration_label": format_duration_label(duration_ms),
                "error": failure_message,
            }
            if ajax_request:
                self._send_json(handler, HTTPStatus.OK, response_payload)
                return
            self._render_automations(
                handler,
                {"message": [f"任务已保存，但 Agent 当前不可达，无法立即执行：{failure_message}"], "kind": ["warning"]},
                task_overrides=override,
                task_feedbacks={
                    payload["task_id"]: {
                        "kind": "error",
                        "title": "立即执行未开始",
                        "message": failure_message,
                    }
                },
                open_task_id=payload["task_id"],
            )
            return

        run_result = self._start_automation_task_run(
            payload,
            trusted_context=trusted_context,
            browser_request_uuid=browser_request_uuid,
        )
        if not run_result.get("ok"):
            failure_message = normalize_feedback_text(
                run_result.get("error") or "Agent 命令提交失败"
            )
            response_payload = {
                "ok": False,
                "pending": False,
                "task_id": payload["task_id"],
                "title": "立即执行未开始",
                "message": failure_message,
                "error": failure_message,
                "error_code": str(run_result.get("error_code") or "COMMAND_SUBMIT_FAILED"),
            }
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
                {"message": [failure_message], "kind": ["warning"]},
                task_overrides=override,
                open_task_id=payload["task_id"],
            )
            return

        receipt = dict(run_result.get("data") or {})
        response_payload = {
            "ok": True,
            "pending": True,
            "task_id": payload["task_id"],
            "title": "命令已受理",
            "message": "命令已提交到控制平面，后续会按 Run 状态自动更新；如需审批，可在当前项目卡片原位处理。",
            "status_label": "等待状态同步",
            "activity_label": "提交时间",
            "activity_value": started_stamp,
            "duration_label": format_duration_label(0),
            "error": "",
            "command_id": receipt.get("command_id"),
            "work_item_id": receipt.get("work_item_id"),
            "run_id": receipt.get("run_id"),
            "next_poll_after_ms": receipt.get("next_poll_after_ms", 1000),
        }
        if ajax_request:
            self._send_json(handler, HTTPStatus.ACCEPTED, response_payload)
            return
        self._render_automations(
            handler,
            {"message": [f"任务已开始执行：{payload['name']}"], "kind": ["success"]},
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
        preview_run_id = self._normalize_browser_request_uuid(
            values.get("preview_run_id")
        )
        if (
            not request_id
            or set(values) != {"task_id", "preview_run_id"}
            or task_id != SCAN_PREVIEW_PROJECT_ID
            or not preview_run_id
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
                "task_id": SCAN_PREVIEW_PROJECT_ID,
                "task_mode": "plugin",
                "project_plugin_instance": True,
                "name": "扫描",
                "tool_name": f"automation.{SCAN_PREVIEW_PROJECT_ID}.run",
                "tool_params": {},
                "tool_params_json": "{}",
            },
            trusted_context=trusted_context,
            browser_request_uuid=request_id,
            preview_run_id=preview_run_id,
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
        run_id = str(receipt.get("run_id") or "").strip() if isinstance(receipt, dict) else ""
        if not run_id:
            self._send_json(
                handler,
                HTTPStatus.BAD_GATEWAY,
                {
                    "ok": False,
                    "error_code": "INVALID_AGENT_RUN_CONTRACT",
                    "message": "Agent 未返回可追踪的正式扫描 Run。",
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
                "command_id": receipt.get("command_id"),
                "work_item_id": receipt.get("work_item_id"),
                "run_id": run_id,
                "next_poll_after_ms": receipt.get("next_poll_after_ms", 1000),
            },
        )

    def _handle_automation_task_cancel(self, handler: BaseHTTPRequestHandler) -> None:
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id", "") or "").strip()
        run_id = str(values.get("run_id", "") or "").strip()

        if not task_id or not run_id:
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
        result = self._agent_request(
            "POST",
            f"/internal/v1/runs/{quote(run_id, safe='')}/cancel",
            payload={
                "comment": "Console 自动化页面取消",
                "actor": trusted_context["actor"],
                "actor_roles": list(trusted_context.get("actor_roles") or []),
                "source": "console",
            },
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

        run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
        self._send_json(
            handler,
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "task_id": task_id,
                "run_id": run_id,
                "title": "取消中",
                "message": "已发送取消请求，正在安全停止当前 Run。",
                "activity_value": str(run.get("started_at") or run.get("created_at") or ""),
                "pending": True,
                "cancel_requested": True,
            },
        )

    def _handle_automation_task_output(self, handler: BaseHTTPRequestHandler, query: dict) -> None:
        """Return durable Run state; legacy tool output remains read-only compatibility."""
        trusted_context = self._control_plane_read_context(handler)
        if trusted_context is None:
            return
        tool_name = str(query.get("tool_name", [""])[0]).strip()
        task_id = str(query.get("task_id", [""])[0]).strip()
        scan_phase = str(query.get("scan_phase", [""])[0]).strip().lower()
        started_at = str(query.get("started_at", [""])[0]).strip()
        run_id = str(query.get("run_id", [""])[0]).strip()
        try:
            offset = int(query.get("offset", ["0"])[0])
        except (ValueError, IndexError):
            offset = 0
        if run_id:
            result = self._agent_request(
                "GET",
                f"/internal/v1/runs/{quote(run_id, safe='')}",
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
                        "error": normalize_feedback_text(result.get("error") or "Run 状态查询失败"),
                    },
                )
                return
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            run = data.get("run") if isinstance(data.get("run"), dict) else {}
            status = str(run.get("status") or "").upper()
            terminal_statuses = {"COMPLETED", "PARTIAL", "FAILED_TERMINAL", "CANCELLED"}
            running_statuses = {"RUNNING", "VERIFYING"}
            attention_titles = {
                "BLOCKED_DATA": "数据阻塞",
                "BLOCKED_LOGIN": "登录已失效",
                "NEEDS_CLARIFICATION": "需要补充信息",
                "FAILED_RETRYABLE": "执行暂时失败",
            }
            is_terminal = status in terminal_statuses
            awaiting_approval = status == "WAITING_APPROVAL"
            attention_title = attention_titles.get(status, "")
            attention = bool(attention_title)
            attention_message = normalize_feedback_text(run.get("error_summary") or "")
            state_line = f"Run {run_id} · {status or 'UNKNOWN'}"
            payload: dict[str, Any] = {
                "lines": [state_line] if offset <= 0 else [],
                "running": status in running_statuses,
                "pending": not is_terminal,
                "awaiting_approval": awaiting_approval,
                "cancel_requested": bool(run.get("cancel_requested_at")),
                "started_at": str(run.get("started_at") or run.get("created_at") or started_at),
                "offset": 1,
                "total": 1,
                "run_id": run_id,
                "status": status,
                "attention": attention,
                "attention_title": attention_title,
                "attention_message": attention_message,
                "next_poll_after_ms": (
                    0 if attention else data.get("next_poll_after_ms", 1000)
                ),
            }
            if is_terminal:
                cancelled = status == "CANCELLED"
                ok = status == "COMPLETED"
                error_message = normalize_feedback_text(run.get("error_summary") or "")
                payload["runtime"] = {
                    "ok": ok,
                    "cancelled": cancelled,
                    "title": "已完成" if ok else "已取消" if cancelled else "执行未完成",
                    "message": error_message,
                    "last_run": str(run.get("finished_at") or run.get("updated_at") or ""),
                    "duration_label": "",
                    "error": error_message,
                    "payload": {"run_id": run_id, "status": status},
                }
                last_status = "success" if ok else "cancelled" if cancelled else "error"
                last_run = str(run.get("finished_at") or run.get("updated_at") or "")
                if task_id:
                    local_state = self.automation_virtual_task_state.get(task_id, {})
                    if local_state.get("task_mode") == "scheduled":
                        self.repository.update_scheduled_task_runtime(
                            base_task_id=task_id,
                            last_run=last_run,
                            last_status=last_status,
                            last_duration_ms=None,
                            last_message=error_message,
                        )
                    else:
                        self._record_virtual_task_runtime(
                            task_id,
                            last_run=last_run,
                            last_status=last_status,
                            last_duration_ms=None,
                            last_message=error_message,
                        )
                if (
                    ok
                    and task_id == SCAN_PREVIEW_PROJECT_ID
                    and scan_phase == "preview"
                ):
                    preview_result = self._agent_request(
                        "GET",
                        (
                            f"/internal/v1/automation-projects/{SCAN_PREVIEW_PROJECT_ID}"
                            f"/scan-previews/{quote(run_id, safe='')}"
                        ),
                        timeout=5,
                        console_principal=trusted_context["_console_principal"],
                    )
                    if preview_result.get("ok"):
                        projection = normalize_scan_preview_projection(
                            preview_result.get("data"),
                            expected_run_id=run_id,
                        )
                        if projection is None:
                            payload["scan_preview_error"] = {
                                "error_code": "INVALID_SCAN_PREVIEW_CONTRACT",
                                "message": (
                                    "Agent 返回的扫描预览合同无效，确认执行已阻断。"
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
            self._send_json(handler, HTTPStatus.OK, payload)
            return
        if not tool_name:
            self._send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing tool_name"})
            return
        query_string = f"/internal/v1/tool-output/{tool_name}?offset={offset}"
        if started_at:
            query_string += f"&started_at={quote(started_at, safe='')}"
        result = self._agent_request(
            "GET",
            query_string,
            timeout=5,
            console_principal=trusted_context["_console_principal"],
        )
        if result.get("ok"):
            payload = dict(result["data"])
            if task_id:
                runtime = self._runtime_from_local_task_state(task_id, since=started_at or None)
                if runtime and payload.get("running"):
                    payload["running"] = False
                if not runtime and not payload.get("running"):
                    runtime = self._sync_task_runtime_from_output_payload(task_id, payload)
                    if not runtime:
                        runtime = self._sync_task_runtime_from_latest_tool_log(
                            task_id,
                            tool_name,
                            since=started_at or None,
                            console_principal=trusted_context["_console_principal"],
                        )
                if runtime:
                    payload["runtime"] = runtime
            self._send_json(handler, HTTPStatus.OK, payload)
        else:
            payload = {"lines": [], "running": False, "offset": 0, "total": 0}
            if task_id:
                runtime = self._runtime_from_local_task_state(task_id, since=started_at or None)
                if not runtime:
                    runtime = self._sync_task_runtime_from_latest_tool_log(
                        task_id,
                        tool_name,
                        since=started_at or None,
                        console_principal=trusted_context["_console_principal"],
                    )
                if runtime:
                    payload["runtime"] = runtime
            self._send_json(handler, HTTPStatus.OK, payload)

    def _merge_submitted_account_roles(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_params: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(tool_params or {})
        workflow = automation_workflow_definition(task_id)
        for role in self._automation_task_account_roles(task_id, workflow, tool_name):
            field = str(role.get("field") or "account_id").strip() or "account_id"
            form_key = f"account_role__{field}"
            if form_key not in values:
                continue
            raw_value = values.get(form_key)
            if isinstance(raw_value, list):
                raw_value = raw_value[-1] if raw_value else ""
            selected_account_id = str(raw_value or "").strip()
            if selected_account_id:
                merged[field] = selected_account_id
            else:
                merged.pop(field, None)
        return merged

    def _collect_automation_task_submission(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        allow_missing_schedule: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], str]:
        values = self._parse_urlencoded_form(handler)
        task_id = str(values.get("task_id", "") or "").strip()
        name = str(values.get("name", "") or "").strip()
        tool_name = str(values.get("tool_name", "") or "").strip()
        task_mode = str(values.get("task_mode", "") or "scheduled").strip().lower() or "scheduled"
        cron_expression = str(values.get("cron_expression", "") or values.get("cron_expression_raw", "") or "").strip()
        tool_params_json = str(values.get("tool_params_json", "") or "").strip()
        enabled = str(values.get("enabled", "") or "").strip().lower() in {"1", "on", "true", "yes"}
        schedule_times_json = str(values.get("schedule_times_json", "") or "").strip()
        project_plugin_instance = str(
            values.get("project_plugin_instance", "") or ""
        ).strip().lower() in {"1", "on", "true", "yes"}

        if project_plugin_instance:
            if not self._automation_project_id(task_id):
                return None, {}, "自动化项目标识无效。"
            tool_name = f"automation.{task_id}.run"
            tool_params_json = "{}"

        override = {
            task_id: {
                "name": name,
                "tool_name": tool_name,
                "task_mode": task_mode,
                "cron_expression": cron_expression,
                "tool_params_json": tool_params_json,
                "schedule_times_json": schedule_times_json,
                "enabled": enabled,
            }
        }

        if not task_id or not name or not tool_name:
            return None, override, "任务 ID、任务名称和工具名称不能为空。"

        if automation_task_control_plane_only(task_id) and not project_plugin_instance:
            return None, override, CONTROL_PLANE_ONLY_AUTOMATION_MESSAGE

        try:
            tool_params = json.loads(tool_params_json or "{}")
        except json.JSONDecodeError as exc:
            return None, override, f"任务 {task_id} 的参数 JSON 解析失败：{exc.msg}"

        if not isinstance(tool_params, dict):
            return None, override, f"任务 {task_id} 的参数必须是 JSON 对象。"

        if project_plugin_instance:
            tool_params = {}
        else:
            tool_params = self._merge_submitted_account_roles(
                task_id=task_id,
                tool_name=tool_name,
                tool_params=tool_params,
                values=values,
            )
        tool_params_json = json.dumps(tool_params, ensure_ascii=False, indent=2)
        override[task_id]["tool_params_json"] = tool_params_json

        if task_mode != "scheduled":
            return (
                {
                    "task_id": task_id,
                    "name": name,
                    "tool_name": tool_name,
                    "task_mode": task_mode,
                    "cron_expression": "",
                    "cron_expressions": [],
                    "tool_params_json": tool_params_json,
                    "tool_params": tool_params,
                    "schedule_times": [],
                    "schedule_times_json": "[]",
                    "enabled": False,
                    "project_plugin_instance": project_plugin_instance,
                },
                override,
                "",
            )

        schedule_times: list[str] = []
        if schedule_times_json:
            try:
                parsed_schedule_times = json.loads(schedule_times_json)
            except json.JSONDecodeError as exc:
                return None, override, f"任务 {task_id} 的执行时间 JSON 解析失败：{exc.msg}"
            if not isinstance(parsed_schedule_times, list):
                return None, override, f"任务 {task_id} 的执行时间必须是数组。"
            schedule_times = normalize_schedule_times(
                [str(item or "").strip() for item in parsed_schedule_times]
            )

        cron_expressions: list[str] = []
        if schedule_times:
            cron_expressions = [build_daily_cron_expression(item) or "" for item in schedule_times]
        elif cron_expression:
            cron_expressions = [
                item.strip()
                for item in str(cron_expression).splitlines()
                if item.strip()
            ]

        cron_expressions = [item for item in cron_expressions if item]
        if not cron_expressions:
            if project_plugin_instance:
                return (
                    {
                        "task_id": task_id,
                        "name": name,
                        "tool_name": tool_name,
                        "task_mode": "scheduled",
                        "cron_expression": "",
                        "cron_expressions": [],
                        "tool_params_json": "{}",
                        "tool_params": {},
                        "schedule_times": [],
                        "schedule_times_json": "[]",
                        "enabled": False,
                        "project_plugin_instance": True,
                    },
                    override,
                    "",
                )
            if not allow_missing_schedule:
                return None, override, "请至少设置一个执行时间"
            return (
                {
                    "task_id": task_id,
                    "name": name,
                    "tool_name": tool_name,
                    "task_mode": "manual",
                    "cron_expression": "",
                    "cron_expressions": [],
                    "tool_params_json": tool_params_json,
                    "tool_params": tool_params,
                    "schedule_times": [],
                    "schedule_times_json": "[]",
                    "enabled": False,
                    "project_plugin_instance": False,
                },
                override,
                "",
            )

        return (
            {
                "task_id": task_id,
                "name": name,
                "tool_name": tool_name,
                "task_mode": task_mode,
                "cron_expression": cron_expressions[0],
                "cron_expressions": cron_expressions,
                "tool_params_json": tool_params_json,
                "tool_params": tool_params,
                "schedule_times": schedule_times,
                "schedule_times_json": json.dumps(schedule_times, ensure_ascii=False),
                "enabled": enabled,
                "project_plugin_instance": project_plugin_instance,
            },
            override,
            "",
        )

    def _persist_automation_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        if automation_task_control_plane_only(str(payload.get("task_id") or "")):
            return {
                "ok": False,
                "error": CONTROL_PLANE_ONLY_AUTOMATION_MESSAGE,
            }
        if str(payload.get("task_mode") or "scheduled") != "scheduled":
            self._record_virtual_task_runtime(payload["task_id"], payload=payload)
            return {"ok": True, "status": None, "data": {"mode": "virtual"}}

        task_group = []
        schedule_times = list(payload.get("schedule_times") or [])
        cron_expressions = list(payload.get("cron_expressions") or [])
        if not cron_expressions:
            task_group.append(
                {
                    "task_id": payload["task_id"],
                    "name": payload["name"],
                    "tool_name": payload["tool_name"],
                    "tool_params": payload["tool_params"],
                    "cron_expression": "",
                    "enabled": False,
                }
            )
        for index, cron_expression in enumerate(cron_expressions):
            schedule_time = schedule_times[index] if index < len(schedule_times) else ""
            task_group.append(
                {
                    "task_id": build_task_schedule_id(
                        payload["task_id"],
                        cron_expression=cron_expression,
                        schedule_time=schedule_time,
                        position=index,
                    ),
                    "name": payload["name"],
                    "tool_name": payload["tool_name"],
                    "tool_params": payload["tool_params"],
                    "cron_expression": cron_expression,
                    "enabled": payload["enabled"],
                }
            )
        self.repository.replace_scheduled_task_group(
            base_task_id=payload["task_id"],
            tasks=task_group,
        )
        return self._agent_request("POST", "/internal/v1/admin/reload", payload={})

    def _handle_automation_admin_action(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        endpoint: str,
        success_message: str,
    ) -> None:
        result = self._agent_request("POST", endpoint, payload={})
        if result.get("ok"):
            self._redirect_with_message(handler, "/automations", success_message, "success")
            return
        self._redirect_with_message(
            handler,
            "/automations",
            f"Agent call failed: {result.get('error', 'unknown error')}",
            "warning",
        )

    def _automation_session_config(self, profile: Any) -> dict[str, str]:
        normalized = normalize_automation_session_profile(profile)
        return dict(AUTOMATION_SESSION_PROFILES[normalized])

    def _automation_session_route(self, path: str) -> tuple[str, str] | None:
        normalized_path = str(path or "").rstrip("/") or "/"
        for profile, config in AUTOMATION_SESSION_PROFILES.items():
            prefix = str(config["console_prefix"]).rstrip("/")
            if normalized_path == prefix or normalized_path.startswith(prefix + "/"):
                suffix = normalized_path[len(prefix) :] or "/"
                return profile, suffix
        return None

    def _automation_session_agent_endpoint(self, profile: Any, suffix: str) -> str:
        config = self._automation_session_config(profile)
        return f"{config['agent_prefix'].rstrip('/')}/{str(suffix or '').strip('/')}"

    def _automation_session_actions(self, profile: Any) -> dict[str, str]:
        config = self._automation_session_config(profile)
        prefix = config["console_prefix"].rstrip("/")
        return {
            "status": f"{prefix}/status",
            "send_code": f"{prefix}/send-code",
            "save_credentials": f"{prefix}/save-credentials",
            "clear_credentials": f"{prefix}/clear-credentials",
            "submit_code": f"{prefix}/submit-code",
            "clear": f"{prefix}/clear",
        }

    def _automation_session_context(self, profile: Any, *, force: bool = True) -> dict[str, Any]:
        normalized = normalize_automation_session_profile(profile)
        config = self._automation_session_config(normalized)
        state = self._fetch_tms_session_status(normalized, force=force)
        credentials = self._fetch_tms_session_credentials(normalized)
        state.setdefault("profile", normalized)
        state.update(
            {
                "profile_label": config["label"],
                "dot_label": config["dot_label"],
                "login_kind": config["login_kind"],
            }
        )
        return {
            "ok": True,
            "profile": normalized,
            "profile_label": config["label"],
            "dot_label": config["dot_label"],
            "login_kind": config["login_kind"],
            "actions": self._automation_session_actions(normalized),
            "state": state,
            "credentials": credentials,
        }

    def _handle_automation_session_context(
        self,
        handler: BaseHTTPRequestHandler,
        query: dict,
    ) -> None:
        profile = query.get("profile", ["ronghui"])[0]
        force = self._query_bool(query, "force", True)
        self._send_json(handler, HTTPStatus.OK, self._automation_session_context(profile, force=force))

    def _handle_automation_session_post(
        self,
        handler: BaseHTTPRequestHandler,
        profile: str,
        suffix: str,
    ) -> bool:
        normalized = normalize_automation_session_profile(profile)
        config = self._automation_session_config(normalized)
        label = config["label"]
        image_login = config.get("login_kind") == "image"
        if suffix == "/send-code":
            if image_login:
                self._handle_tms_session_action(
                    handler,
                    profile=normalized,
                    endpoint=self._automation_session_agent_endpoint(normalized, "send-code"),
                    payload={},
                    success_message=f"{label}正在自动识别图片验证码并登录；如自动识别失败，将保留人工输入入口。",
                    timeout=90,
                )
                return True
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "send-code"),
                payload={},
                success_message=(
                    f"{label}登录已提交；如出现图片验证码，请按图输入后提交。"
                    if image_login
                    else f"{label} 验证码已发送，请在顶部模块输入短信验证码。"
                ),
                timeout=90,
            )
            return True
        if suffix == "/save-credentials":
            values = self._parse_urlencoded_form(handler)
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "credentials"),
                payload={
                    "username": str(values.get("username", "") or "").strip(),
                    "password": str(values.get("password", "") or ""),
                    "phone": str(values.get("phone", "") or "").strip(),
                },
                success_message=f"{label} 登录配置已保存。",
                timeout=20,
            )
            return True
        if suffix == "/clear-credentials":
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "credentials/clear"),
                payload={},
                success_message=f"{label} 登录配置已清空。",
                timeout=20,
            )
            return True
        if suffix == "/submit-code":
            values = self._parse_urlencoded_form(handler)
            sms_code = str(values.get("code", "") or "").strip()
            if not sms_code:
                self._respond_tms_action(
                    handler,
                    profile=normalized,
                    ok=False,
                    message=f"{label} {'图片验证码' if image_login else '验证码'}不能为空。",
                    kind="warning",
                    http_status=HTTPStatus.BAD_REQUEST,
                )
                return True
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "submit-code"),
                payload={"code": sms_code},
                success_message=f"{label} 登录成功，登录态已更新。",
                timeout=45,
            )
            return True
        if suffix == "/clear":
            self._handle_tms_session_action(
                handler,
                profile=normalized,
                endpoint=self._automation_session_agent_endpoint(normalized, "clear"),
                payload={},
                success_message=f"{label} 已退出登录，自动登录与断线提醒已关闭。",
                timeout=20,
            )
            return True
        return False

    def _default_tms_session_status(
        self,
        *,
        status: str = "error",
        label: str = "异常",
        last_error_summary: str = "",
        agent_ok: bool = False,
    ) -> dict[str, Any]:
        tone_map = {
            "authenticated": "success",
            "pending_code": "warning",
            "logged_out": "neutral",
            "expired": "error",
            "error": "error",
        }
        safe_status = status if status in tone_map else "error"
        return {
            "status": safe_status,
            "label": label,
            "status_tone": tone_map[safe_status],
            "authenticated": safe_status == "authenticated",
            "pending_code": safe_status == "pending_code",
            "last_validation_at": "",
            "last_error_summary": last_error_summary,
            "authenticated_at": "",
            "pending_since": "",
            "expires_at": "",
            "has_saved_credentials": False,
            "has_manual_credentials": False,
            "has_env_credentials": False,
            "credential_source": "",
            "challenge_type": "",
            "challenge_label": "",
            "captcha_image": "",
            "captcha_image_mime": "",
            "captcha_captured_at": "",
            "agent_ok": agent_ok,
        }

    def _normalize_tms_session_status_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return self._default_tms_session_status(last_error_summary="TMS 登录态接口返回了无效数据。")

        if payload.get("ok") is False:
            return self._default_tms_session_status(
                last_error_summary=normalize_feedback_text(
                    payload.get("message") or payload.get("error") or "TMS 登录态接口调用失败。"
                ),
            )

        status = str(payload.get("status") or "").strip() or "error"
        label = str(payload.get("label") or "").strip()
        default_labels = {
            "authenticated": "已登录",
            "pending_code": "待输入验证码",
            "logged_out": "未登录",
            "expired": "已过期",
            "error": "异常",
        }
        normalized = self._default_tms_session_status(
            status=status,
            label=label or default_labels.get(status, "异常"),
            last_error_summary=str(payload.get("last_error_summary") or "").strip(),
            agent_ok=True,
        )
        normalized.update(
            {
                "last_validation_at": str(payload.get("last_validation_at") or "").strip(),
                "authenticated_at": str(payload.get("authenticated_at") or "").strip(),
                "pending_since": str(payload.get("pending_since") or "").strip(),
                "expires_at": str(payload.get("expires_at") or "").strip(),
                "has_saved_credentials": bool(payload.get("has_saved_credentials")),
                "has_manual_credentials": bool(payload.get("has_manual_credentials")),
                "has_env_credentials": bool(payload.get("has_env_credentials")),
                "credential_source": str(payload.get("credential_source") or "").strip(),
                "challenge_type": str(payload.get("challenge_type") or "").strip(),
                "challenge_label": str(payload.get("challenge_label") or "").strip(),
                "captcha_image": str(payload.get("captcha_image") or "").strip(),
                "captcha_image_mime": str(payload.get("captcha_image_mime") or "").strip(),
                "captcha_captured_at": str(payload.get("captcha_captured_at") or "").strip(),
                "profile": str(payload.get("profile") or "").strip(),
                "account_id": str(payload.get("account_id") or "").strip(),
                "account_name": str(payload.get("account_name") or "").strip(),
                "system": str(payload.get("system") or "").strip(),
                "system_label": str(payload.get("system_label") or "").strip(),
                "login_kind": str(payload.get("login_kind") or "").strip(),
            }
        )
        return normalized

    def _fetch_tms_session_status(self, profile: Any = "ronghui", *, force: bool = True) -> dict[str, Any]:
        normalized = normalize_automation_session_profile(profile)
        label = self._automation_session_config(normalized)["label"]
        endpoint = self._automation_session_agent_endpoint(normalized, "status")
        if force:
            endpoint = f"{endpoint}?force=1"
        result = self._agent_request("GET", endpoint, timeout=35 if force else 12)
        if not result.get("ok"):
            return self._default_tms_session_status(
                last_error_summary=normalize_feedback_text(
                    result.get("error") or f"Agent 当前不可达，无法获取{label}登录态。"
                ),
            )
        return self._normalize_tms_session_status_payload(result.get("data"))

    def _default_tms_session_credentials(self) -> dict[str, Any]:
        return {
            "username": "",
            "password": "",
            "phone": "",
            "updated_at": "",
            "has_saved_credentials": False,
            "has_manual_credentials": False,
            "has_env_credentials": False,
            "credential_source": "",
        }

    def _normalize_tms_session_credentials_payload(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return self._default_tms_session_credentials()
        if payload.get("ok") is False:
            return self._default_tms_session_credentials()
        return {
            "username": str(payload.get("username") or ""),
            "password": "",
            "phone": str(payload.get("phone") or ""),
            "updated_at": str(payload.get("updated_at") or ""),
            "has_saved_credentials": bool(payload.get("has_saved_credentials")),
            "has_manual_credentials": bool(payload.get("has_manual_credentials")),
            "has_env_credentials": bool(payload.get("has_env_credentials")),
            "credential_source": str(payload.get("credential_source") or "").strip(),
        }

    def _fetch_tms_session_credentials(self, profile: Any = "ronghui") -> dict[str, Any]:
        normalized = normalize_automation_session_profile(profile)
        result = self._agent_request("GET", self._automation_session_agent_endpoint(normalized, "credentials"), timeout=8)
        if not result.get("ok"):
            return self._default_tms_session_credentials()
        return self._normalize_tms_session_credentials_payload(result.get("data"))

    def _handle_tms_session_status(
        self,
        handler: BaseHTTPRequestHandler,
        profile: Any = "ronghui",
        query: dict | None = None,
    ) -> None:
        force = self._query_bool(query, "force", True)
        self._send_json(handler, HTTPStatus.OK, self._fetch_tms_session_status(profile, force=force))

    def _wants_json_response(self, handler: BaseHTTPRequestHandler) -> bool:
        accept = str(handler.headers.get("Accept") or "").lower()
        requested_with = str(handler.headers.get("X-Requested-With") or "").lower()
        return "application/json" in accept or requested_with == "fetch"

    def _respond_tms_action(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        profile: Any = "ronghui",
        ok: bool,
        message: str,
        kind: str,
        http_status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        normalized = normalize_automation_session_profile(profile)
        config = self._automation_session_config(normalized)
        if self._wants_json_response(handler):
            state = self._fetch_tms_session_status(normalized)
            state.update(
                {
                    "profile": normalized,
                    "profile_label": config["label"],
                    "dot_label": config["dot_label"],
                    "login_kind": config["login_kind"],
                }
            )
            self._send_json(
                handler,
                http_status,
                {
                    "ok": ok,
                    "message": message,
                    "kind": kind,
                    "profile": normalized,
                    "profile_label": config["label"],
                    "dot_label": config["dot_label"],
                    "login_kind": config["login_kind"],
                    "actions": self._automation_session_actions(normalized),
                    "state": state,
                    "credentials": self._fetch_tms_session_credentials(normalized),
                },
            )
            return
        self._redirect_with_message(handler, "/automations", message, kind)

    def _handle_tms_session_action(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        profile: Any = "ronghui",
        endpoint: str,
        payload: dict[str, Any],
        success_message: str,
        timeout: int,
    ) -> None:
        normalized = normalize_automation_session_profile(profile)
        label = self._automation_session_config(normalized)["label"]
        result = self._agent_request("POST", endpoint, payload=payload, timeout=timeout)
        if not result.get("ok"):
            self._respond_tms_action(
                handler,
                profile=normalized,
                ok=False,
                message=f"{label}登录态操作失败：{normalize_feedback_text(result.get('error') or 'unknown error')}",
                kind="warning",
                http_status=HTTPStatus.BAD_GATEWAY,
            )
            return

        response_payload = result.get("data")
        if not isinstance(response_payload, dict):
            self._respond_tms_action(
                handler,
                profile=normalized,
                ok=False,
                message=f"{label}登录态接口返回了无效数据。",
                kind="warning",
                http_status=HTTPStatus.BAD_GATEWAY,
            )
            return

        if response_payload.get("ok") is False:
            error_text = normalize_feedback_text(
                response_payload.get("message") or response_payload.get("error") or "TMS 登录态操作失败。"
            )
            error_code = str(response_payload.get("error_code") or "").strip().upper()
            kind = "warning" if error_code in {"AUTH_REQUIRED", "AUTH_PENDING_CODE"} else "error"
            self._respond_tms_action(
                handler,
                profile=normalized,
                ok=False,
                message=error_text,
                kind=kind,
                http_status=HTTPStatus.OK,
            )
            return

        self._respond_tms_action(
            handler,
            profile=normalized,
            ok=True,
            message=success_message,
            kind="success",
            http_status=HTTPStatus.OK,
        )

    def _latest_tool_log(
        self,
        tool_name: str,
        *,
        since: str | None = None,
        console_principal: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        endpoint = f"/internal/v1/tool-logs?limit=5&tool_name={quote(tool_name, safe='')}"
        if console_principal is None:
            return None
        result = self._agent_request(
            "GET",
            endpoint,
            timeout=5,
            console_principal=console_principal,
        )
        if not result.get("ok"):
            return None
        data = result.get("data")
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            created_at = str(row.get("created_at") or "")
            if since and created_at and created_at < since:
                continue
            return row
        return None

    def _sync_task_runtime_from_latest_tool_log(
        self,
        task_id: str,
        tool_name: str,
        *,
        since: str | None = None,
        console_principal: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = self._latest_tool_log(
            tool_name,
            since=since,
            console_principal=console_principal,
        )
        if not row:
            return None

        result = row.get("result")
        if not isinstance(result, dict):
            result = {}
        cancelled = bool(result.get("canceled"))
        ok = bool(row.get("success")) and not cancelled
        last_run = str(row.get("created_at") or "")
        duration_ms = row.get("duration_ms")
        error_message_full = normalize_feedback_text(result.get("error") or "")
        error_message_value = shorten_error_message(error_message_full)
        payload = result.get("data") if ok else result
        feedback_meta = automation_runtime_feedback_meta(
            ok=ok,
            cancelled=cancelled,
            success_message=str(automation_workflow_definition(task_id).get("display_name") or task_id),
            error_message=error_message_value,
        )

        if self.repository.list_scheduled_task_group(task_id):
            self.repository.update_scheduled_task_runtime(
                base_task_id=task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )
        else:
            self._record_virtual_task_runtime(
                task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )

        return {
            "ok": ok,
            "cancelled": cancelled,
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "last_run": last_run,
            "duration_label": format_duration_label(duration_ms),
            "error": error_message_full,
            "payload": payload,
        }

    def _runtime_from_local_task_state(self, task_id: str, *, since: str | None = None) -> dict[str, Any] | None:
        rows: list[dict[str, Any]] = []
        try:
            rows = self.repository.list_scheduled_task_group(task_id)
        except Exception:
            rows = []

        if rows:
            state = max(rows, key=lambda item: str(item.get("last_run") or ""))
        else:
            state = dict(getattr(self, "automation_virtual_task_state", {}).get(task_id) or {})

        last_run = str(state.get("last_run") or "")
        if not last_run or (since and last_run < since):
            return None

        last_status = str(state.get("last_status") or "")
        if last_status in {"", "running"}:
            return None

        cancelled = last_status == "cancelled"
        ok = last_status == "success" and not cancelled
        error_message_full = normalize_feedback_text(state.get("last_message") or "")
        feedback_meta = automation_runtime_feedback_meta(
            ok=ok,
            cancelled=cancelled,
            success_message=str(automation_workflow_definition(task_id).get("display_name") or task_id),
            error_message=shorten_error_message(error_message_full),
        )
        return {
            "ok": ok,
            "cancelled": cancelled,
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "last_run": last_run,
            "duration_label": format_duration_label(state.get("last_duration_ms")),
            "error": error_message_full,
            "payload": None,
        }

    def _sync_task_runtime_from_output_payload(
        self,
        task_id: str,
        output_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        lines = output_payload.get("lines")
        if not isinstance(lines, list) or not lines:
            return None

        parsed_result: dict[str, Any] | None = None
        for row in reversed(lines):
            text = str(row or "").strip()
            if not text or not text.startswith("{"):
                continue
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed_result = candidate
                break

        if not parsed_result:
            return None

        cancelled = bool(parsed_result.get("canceled"))
        ok = bool(parsed_result.get("ok")) and not parsed_result.get("error") and not cancelled
        last_run = str(output_payload.get("started_at") or "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        duration_ms = None
        for key in ("duration_s", "cost_sec"):
            raw_value = parsed_result.get(key)
            try:
                if raw_value not in (None, ""):
                    duration_ms = int(round(float(raw_value) * 1000))
                    break
            except (TypeError, ValueError):
                continue
        error_message_full = normalize_feedback_text(parsed_result.get("error") or "")
        error_message_value = shorten_error_message(error_message_full)
        payload = parsed_result if ok else {"data": parsed_result}
        feedback_meta = automation_runtime_feedback_meta(
            ok=ok,
            cancelled=cancelled,
            success_message=str(automation_workflow_definition(task_id).get("display_name") or task_id),
            error_message=error_message_value,
        )

        if self.repository.list_scheduled_task_group(task_id):
            self.repository.update_scheduled_task_runtime(
                base_task_id=task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )
        else:
            self._record_virtual_task_runtime(
                task_id,
                last_run=last_run,
                last_status=feedback_meta["status"],
                last_duration_ms=duration_ms,
                last_message="" if ok else error_message_full,
            )

        return {
            "ok": ok,
            "cancelled": cancelled,
            "title": feedback_meta["title"],
            "message": feedback_meta["message"],
            "last_run": last_run,
            "duration_label": format_duration_label(duration_ms),
            "error": error_message_full,
            "payload": payload,
        }

    def _is_ajax_request(self, handler: BaseHTTPRequestHandler) -> bool:
        requested_with = str(handler.headers.get("X-Requested-With", "") or "").strip().lower()
        accept_header = str(handler.headers.get("Accept", "") or "").strip().lower()
        return requested_with in {"xmlhttprequest", "fetch"} or "application/json" in accept_header
