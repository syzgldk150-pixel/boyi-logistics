"""Console application services grouped by business responsibility."""

from console.app_support import *  # noqa: F403
from console.services.control_plane import ControlPlaneServiceMixin


class AutomationServiceMixin:
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
            "name_value": name_value,
            "tool_name_value": tool_name_value,
            "cron_expression_value": str(schedule_info.get("raw_value") or ""),
            "enabled_value": bool(override.get("enabled")) if is_schedulable else False,
            "tool_params_json": raw_json,
            "tool_param_fields": flatten_automation_fields(tool_params),
            "schedule_supported": schedule_supported if is_schedulable else False,
            "schedule_editable": is_schedulable,
            "schedule_time_values": list(schedule_info.get("time_values") or []) if is_schedulable else [],
            "schedule_summary": schedule_summary,
            "schedule_icon": "clock" if is_schedulable else "zap",
            "schedule_warning": schedule_warning if is_schedulable else f"此流程不写入 scheduled_tasks，默认通过{trigger_label}触发。",
            "trigger_label": trigger_label,
            "is_schedulable": is_schedulable,
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

        if payload.get("task_mode") == "scheduled":
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
    ) -> dict[str, Any]:
        """Submit one durable command and return its Run receipt immediately."""

        run_result = self._agent_request(
            "POST",
            "/internal/v1/commands",
            payload={
                "command_type": "tool.execute",
                "parameters": {
                    "tool_name": payload["tool_name"],
                    "arguments": payload["tool_params"],
                },
                "entity_refs": [],
                "idempotency_key": (
                    f"console:{trusted_context['actor']['actor_id']}:tool.execute:"
                    f"{browser_request_uuid}"
                ),
                **ControlPlaneServiceMixin._control_plane_agent_body_context(trusted_context),
            },
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
        if payload.get("task_mode") == "scheduled":
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
                "task_mode": payload.get("task_mode"),
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
                f"自动化任务数据库当前不可达，任务列表已临时降级为空，仅保留顶部 TMS 登录态验证模块。详情：{exc}"
            )

        grouped_rows: dict[str, list[dict[str, Any]]] = {}
        for row in scheduled_rows:
            grouped_rows.setdefault(normalize_task_group_id(str(row.get("id", "") or "")), []).append(row)
        workflow_resources = {
            str(item.get("resource_key", "") or ""): item
            for item in workflow_resource_rows
        }

        tasks_by_id: dict[str, dict[str, Any]] = {}
        for base_task_id, rows in grouped_rows.items():
            rows = sorted(
                rows,
                key=lambda item: (
                    task_group_slot_index(str(item.get("id", "") or "")),
                    str(item.get("id", "") or ""),
                ),
            )
            primary_row = next((item for item in rows if str(item.get("id", "") or "") == base_task_id), rows[0])
            workflow = automation_workflow_definition(base_task_id)
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
            tasks_by_id[base_task_id] = {
                **primary_row,
                "note": str(workflow.get("note") or automation_task_note(base_task_id)),
                "provider": provider_value,
                "provider_label": automation_provider_label(provider_value),
                "system_badges": list(workflow.get("system_badges") or []),
                "task_id": base_task_id,
                "task_mode": "scheduled",
                "display_task_id": str(primary_row.get("id", "") or base_task_id) if len(rows) == 1 else base_task_id,
                "group_size": len(rows),
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
                "trigger_label": str(workflow.get("trigger_label") or "定时任务"),
                "is_schedulable": True,
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

        for workflow in AUTOMATION_WORKFLOW_CATALOG:
            workflow_task_id = str(workflow.get("task_id", "") or "")
            if not workflow_task_id or workflow_task_id in tasks_by_id:
                continue
            tasks_by_id[workflow_task_id] = self._build_virtual_automation_task(
                workflow_task_id,
                override=task_overrides.get(workflow_task_id),
                feedback=task_feedbacks.get(workflow_task_id),
                open_task_id=open_task_id,
                workflow_resources=workflow_resources,
                resource_overrides=resource_overrides,
            )

        tasks = sorted(
            tasks_by_id.values(),
            key=lambda item: (
                int(item.get("sort_order") or 999),
                str(item.get("name_value") or ""),
            ),
        )
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
            tms_session_status=self._fetch_tms_session_status(),
            tms_session_credentials=self._fetch_tms_session_credentials(),
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

        try:
            workflow_resource_rows = self.repository.list_workflow_resources()
        except Exception:
            workflow_resource_rows = []
        workflow_resources = {
            str(item.get("resource_key", "") or ""): item
            for item in workflow_resource_rows
        }
        resource_bindings = build_automation_resource_bindings(payload["task_id"], workflow_resources)
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
        reload_result = self._persist_automation_task(payload)
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
                self._send_json(handler, HTTPStatus.BAD_GATEWAY, response_payload)
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
            "title": "执行中",
            "message": "命令已受理，结果会按运行状态自动更新。",
            "status_label": "后台执行中",
            "activity_label": "开始时间",
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
            payload={"comment": "Console 自动化页面取消", **ControlPlaneServiceMixin._control_plane_agent_body_context(trusted_context)},
            timeout=10,
            console_principal=trusted_context["_console_principal"],
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
            is_terminal = status in terminal_statuses
            state_line = f"Run {run_id} · {status or 'UNKNOWN'}"
            payload: dict[str, Any] = {
                "lines": [state_line] if offset <= 0 else [],
                "running": not is_terminal,
                "cancel_requested": bool(run.get("cancel_requested_at")),
                "started_at": str(run.get("started_at") or run.get("created_at") or started_at),
                "offset": 1,
                "total": 1,
                "run_id": run_id,
                "status": status,
                "next_poll_after_ms": data.get("next_poll_after_ms", 1000),
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

        try:
            tool_params = json.loads(tool_params_json or "{}")
        except json.JSONDecodeError as exc:
            return None, override, f"任务 {task_id} 的参数 JSON 解析失败：{exc.msg}"

        if not isinstance(tool_params, dict):
            return None, override, f"任务 {task_id} 的参数必须是 JSON 对象。"

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
            },
            override,
            "",
        )

    def _persist_automation_task(self, payload: dict[str, Any]) -> dict[str, Any]:
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
