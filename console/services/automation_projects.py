"""Console plugin catalog transport, policy and lifecycle services."""

import copy
import threading
import time
from concurrent.futures import Future
from collections.abc import Mapping
from console.app_support import *  # noqa: F403
from console.services.automation_plugin_management import AutomationPluginManagementServiceMixin
from console.services.module_data_sources import ModuleDataSourcesServiceMixin
from console.services.automation_project_contributions import (
    AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE as AUTOMATION_PLUGIN_V2_ENTRYPOINT_ID_RE,
)
from console.services.automation_catalog_projection import (
    AUTOMATION_PROJECT_POLICY_ENDPOINT as AUTOMATION_PROJECT_POLICY_ENDPOINT,
    AUTOMATION_PROJECT_POLICY_MODES as AUTOMATION_PROJECT_POLICY_MODES,
    AUTOMATION_PROJECT_EFFECTIVE_MODES as AUTOMATION_PROJECT_EFFECTIVE_MODES,
    AUTOMATION_PROJECT_POLICY_STATUSES as AUTOMATION_PROJECT_POLICY_STATUSES,
    AUTOMATION_PROJECT_RUNTIME_STATUSES as AUTOMATION_PROJECT_RUNTIME_STATUSES,
    AUTOMATION_RUNTIME_REASON_LABELS as AUTOMATION_RUNTIME_REASON_LABELS,
    AUTOMATION_RUNTIME_CONTRACT_ERROR_LABEL as AUTOMATION_RUNTIME_CONTRACT_ERROR_LABEL,
    AUTOMATION_RUNTIME_RECONCILING_LABEL as AUTOMATION_RUNTIME_RECONCILING_LABEL,
    AUTOMATION_PROJECT_ID_RE as AUTOMATION_PROJECT_ID_RE,
    AUTOMATION_PENDING_SET_HASH_RE as AUTOMATION_PENDING_SET_HASH_RE,
    AUTOMATION_RUN_RECEIPT_ID_RE as AUTOMATION_RUN_RECEIPT_ID_RE,
    AUTOMATION_RUN_RECEIPT_STATUSES as AUTOMATION_RUN_RECEIPT_STATUSES,
    AUTOMATION_APPROVAL_BATCH_MAX_RUNS as AUTOMATION_APPROVAL_BATCH_MAX_RUNS,
    AUTOMATION_PROJECT_COMMENT_MAX_CHARS as AUTOMATION_PROJECT_COMMENT_MAX_CHARS,
    AUTOMATION_PENDING_RISK_LABELS as AUTOMATION_PENDING_RISK_LABELS,
    AUTOMATION_PLUGIN_CATALOG_ENDPOINT as AUTOMATION_PLUGIN_CATALOG_ENDPOINT,
    AUTOMATION_WORKERS_ENDPOINT as AUTOMATION_WORKERS_ENDPOINT,
    AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES as AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES,
    AUTOMATION_PLUGIN_STATE_LABELS as AUTOMATION_PLUGIN_STATE_LABELS,
    AUTOMATION_PLUGIN_STABLE_STATES as AUTOMATION_PLUGIN_STABLE_STATES,
    AUTOMATION_PLUGIN_RECONCILE_STATES as AUTOMATION_PLUGIN_RECONCILE_STATES,
    AUTOMATION_PLUGIN_RECONCILE_DISPLAY_STATES as AUTOMATION_PLUGIN_RECONCILE_DISPLAY_STATES,
    AUTOMATION_PLUGIN_ID_RE as AUTOMATION_PLUGIN_ID_RE,
    AUTOMATION_PLUGIN_VERSION_RE as AUTOMATION_PLUGIN_VERSION_RE,
    AUTOMATION_PLUGIN_API_RE as AUTOMATION_PLUGIN_API_RE,
    AUTOMATION_PLUGIN_SERVICE_RE as AUTOMATION_PLUGIN_SERVICE_RE,
    AUTOMATION_PLUGIN_MIGRATION_PAIR_ID_RE as AUTOMATION_PLUGIN_MIGRATION_PAIR_ID_RE,
    AUTOMATION_PLUGIN_RUNTIME_MODEL_LABELS as AUTOMATION_PLUGIN_RUNTIME_MODEL_LABELS,
    AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL as AUTOMATION_PLUGIN_UNSUPPORTED_RUNTIME_MODEL,
    AUTOMATION_PLUGIN_DEPENDENCY_STATE_LABELS as AUTOMATION_PLUGIN_DEPENDENCY_STATE_LABELS,
    AUTOMATION_PLUGIN_DEPENDENCY_BLOCKING_STATES as AUTOMATION_PLUGIN_DEPENDENCY_BLOCKING_STATES,
    AUTOMATION_PLUGIN_MIGRATION_STATE_LABELS as AUTOMATION_PLUGIN_MIGRATION_STATE_LABELS,
    AUTOMATION_PLUGIN_MIGRATION_TEST_STATE_LABELS as AUTOMATION_PLUGIN_MIGRATION_TEST_STATE_LABELS,
    AUTOMATION_PLUGIN_BLOCK_REASON_COPY as AUTOMATION_PLUGIN_BLOCK_REASON_COPY,
    AUTOMATION_WORKER_ID_RE as AUTOMATION_WORKER_ID_RE,
    AUTOMATION_PLUGIN_BINDING_ID_RE as AUTOMATION_PLUGIN_BINDING_ID_RE,
    AUTOMATION_PLUGIN_CONFIG_KEY_RE as AUTOMATION_PLUGIN_CONFIG_KEY_RE,
    AUTOMATION_PLUGIN_CODE_OWNED_CONFIG_KEY_RE as AUTOMATION_PLUGIN_CODE_OWNED_CONFIG_KEY_RE,
    AUTOMATION_PLUGIN_MIGRATION_RESERVED_BUSINESS_KEY_FIELDS as AUTOMATION_PLUGIN_MIGRATION_RESERVED_BUSINESS_KEY_FIELDS,
    AUTOMATION_PLUGIN_ENTRYPOINTS as AUTOMATION_PLUGIN_ENTRYPOINTS,
    AUTOMATION_PLUGIN_V2_ENTRYPOINT_KINDS as AUTOMATION_PLUGIN_V2_ENTRYPOINT_KINDS,
    AUTOMATION_PLUGIN_CONTRIBUTION_PROJECTION_STATES as AUTOMATION_PLUGIN_CONTRIBUTION_PROJECTION_STATES,
    AUTOMATION_PLUGIN_ACTIVE_CONTRIBUTION_FIELDS as AUTOMATION_PLUGIN_ACTIVE_CONTRIBUTION_FIELDS,
    AUTOMATION_PLUGIN_CONFIG_MAX_FIELDS as AUTOMATION_PLUGIN_CONFIG_MAX_FIELDS,
    AUTOMATION_PLUGIN_CONFIG_MAX_BYTES as AUTOMATION_PLUGIN_CONFIG_MAX_BYTES,
    AUTOMATION_PLUGIN_SCHEDULE_MAX_DAILY_TIMES as AUTOMATION_PLUGIN_SCHEDULE_MAX_DAILY_TIMES,
    AUTOMATION_PLUGIN_SCHEDULE_RUNTIME_STATES as AUTOMATION_PLUGIN_SCHEDULE_RUNTIME_STATES,
    AUTOMATION_PLUGIN_CONFIG_COPY as AUTOMATION_PLUGIN_CONFIG_COPY,
    AUTOMATION_PLUGIN_COMMON_CONFIG_KEYS as AUTOMATION_PLUGIN_COMMON_CONFIG_KEYS,
    _valid_migration_business_key_field as _valid_migration_business_key_field,
    AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY as AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY,
    AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY_BY_PLUGIN as AUTOMATION_PLUGIN_ACCOUNT_ROLE_COPY_BY_PLUGIN,
    AUTOMATION_PLUGIN_RESOURCE_ROLE_COPY as AUTOMATION_PLUGIN_RESOURCE_ROLE_COPY,
    AUTOMATION_RESOURCE_DISPLAY_NAMES as AUTOMATION_RESOURCE_DISPLAY_NAMES,
    _plain_role_copy as _plain_role_copy,
    _resource_display_name as _resource_display_name,
    _configuration_summary as _configuration_summary,
    _normalize_browser_plugin_config_value as _normalize_browser_plugin_config_value,
    _normalize_browser_plugin_bindings as _normalize_browser_plugin_bindings,
    normalize_automation_project_policy_items as normalize_automation_project_policy_items,
    build_automation_project_policy_view as build_automation_project_policy_view,
    apply_automation_project_execution_gate as apply_automation_project_execution_gate,
    normalize_automation_pending_approvals as normalize_automation_pending_approvals,
    normalize_automation_approval_batch_result as normalize_automation_approval_batch_result,
    _normalize_plugin_account_roles as _normalize_plugin_account_roles,
    _normalize_plugin_resource_roles as _normalize_plugin_resource_roles,
    _normalize_plugin_resources as _normalize_plugin_resources,
    _plugin_config_value as _plugin_config_value,
    _normalize_plugin_config_schema as _normalize_plugin_config_schema,
    _normalize_plugin_code_owned_config_fields as _normalize_plugin_code_owned_config_fields,
    _normalize_plugin_entrypoints as _normalize_plugin_entrypoints,
    _normalize_plugin_entrypoint_kinds as _normalize_plugin_entrypoint_kinds,
    _normalize_plugin_scheduling as _normalize_plugin_scheduling,
    _normalize_plugin_schedule as _normalize_plugin_schedule,
    _normalize_plugin_binding_map as _normalize_plugin_binding_map,
    _normalize_plugin_runtime_model as _normalize_plugin_runtime_model,
    _normalize_plugin_api as _normalize_plugin_api,
    _normalize_plugin_semver as _normalize_plugin_semver,
    _normalize_plugin_versions as _normalize_plugin_versions,
    _normalize_plugin_provided_services as _normalize_plugin_provided_services,
    _normalize_plugin_dependency_state as _normalize_plugin_dependency_state,
    _normalize_plugin_blocking_reasons as _normalize_plugin_blocking_reasons,
    _normalize_plugin_migration as _normalize_plugin_migration,
    AUTOMATION_PLUGIN_MISSING_REQUIREMENT_LABELS as AUTOMATION_PLUGIN_MISSING_REQUIREMENT_LABELS,
    normalize_automation_plugin_catalog as normalize_automation_plugin_catalog,
    normalize_automation_workers as normalize_automation_workers,
    normalize_hidden_automation_ids as normalize_hidden_automation_ids,
    automation_plugin_block_warning as automation_plugin_block_warning,
)


class AutomationProjectsServiceMixin(AutomationPluginManagementServiceMixin, ModuleDataSourcesServiceMixin):
    _automation_plugin_max_package_bytes = AUTOMATION_PLUGIN_MAX_PACKAGE_BYTES
    _automation_plugin_version_re = AUTOMATION_PLUGIN_VERSION_RE
    _automation_plugin_migration_pair_id_re = AUTOMATION_PLUGIN_MIGRATION_PAIR_ID_RE
    _valid_migration_business_key_field = staticmethod(
        _valid_migration_business_key_field
    )

    def _clear_automation_plugin_catalog_cache(self) -> None:
        lock = getattr(self, "_automation_catalog_cache_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._automation_catalog_cache_lock = lock
        with lock:
            self._automation_catalog_cache = {}
            self._automation_catalog_cache_epoch = getattr(self, "_automation_catalog_cache_epoch", 0) + 1

    def _load_automation_plugin_catalog(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        refresh_resources: bool = False,
        prefer_stale: bool = False,
        module: str | None = None,
        summary: bool = False,
    ):
        if refresh_resources:
            self._clear_automation_plugin_catalog_cache()
            return self._load_automation_plugin_catalog_uncached(
                handler,
                refresh_resources=True, module=module, summary=summary,
            )
        user = getattr(handler, "current_admin_user", None)
        principal = self._mysql_console_principal(user)
        # This read projection is identical for authenticated administrators of
        # the same roles. Session authentication still runs on every request;
        # execution and mutations always return to the authoritative Agent.
        cache_key = (
            principal is not None,
            tuple(sorted(str(item) for item in (principal or {}).get("roles") or [])),
            module, summary,
        )
        lock = getattr(self, "_automation_catalog_cache_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._automation_catalog_cache_lock = lock
        current = time.monotonic()
        with lock:
            cache = getattr(self, "_automation_catalog_cache", {})
            cached = cache.get(cache_key)
            if cached is not None and (
                cached[0] > current
                or (prefer_stale and cached[0] + 300.0 > current)
            ):
                return copy.deepcopy(cached[1])
            epoch = getattr(self, "_automation_catalog_cache_epoch", 0)
            inflight = getattr(self, "_automation_catalog_inflight", {})
            flight_key = (epoch, cache_key)
            future = inflight.get(flight_key)
            owner = future is None
            if owner:
                future = Future()
                inflight[flight_key] = future
                self._automation_catalog_inflight = inflight
        if not owner:
            return copy.deepcopy(future.result(timeout=20))
        try:
            result = self._load_automation_plugin_catalog_uncached(handler, module=module, summary=summary)
            with lock:
                if epoch != getattr(self, "_automation_catalog_cache_epoch", 0):
                    # A known mutation invalidates the caller's response too,
                    # not just the stored cache. Never render a removed instance
                    # from the old in-flight request after an uninstall.
                    result = ([], [], [], [], frozenset(), "插件目录已变化，请刷新后继续。", result[6])
                elif not result[5]:
                    cache = getattr(self, "_automation_catalog_cache", {})
                    cache[cache_key] = (time.monotonic() + 60.0, copy.deepcopy(result))
                    self._automation_catalog_cache = cache
            future.set_result(copy.deepcopy(result))
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with lock:
                self._automation_catalog_inflight.pop(flight_key, None)

    def _load_automation_plugin_catalog_uncached(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        refresh_resources: bool = False,
        module: str | None = None,
        summary: bool = False,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[str],
        frozenset[str],
        str,
        bool,
    ]:
        user = getattr(handler, "current_admin_user", None)
        principal = self._mysql_console_principal(user)
        can_manage = bool(
            principal and "super_admin" in list(principal.get("roles") or [])
        )
        if principal is None:
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                "插件目录只对真实的数据库管理员会话开放。",
                False,
            )

        catalog_endpoint = AUTOMATION_PLUGIN_CATALOG_ENDPOINT
        catalog_query = {}
        if module is not None:
            catalog_query["module"] = module
        if summary:
            catalog_query["summary"] = "1"
        if refresh_resources:
            catalog_query["refresh_resources"] = "1"
        if catalog_query:
            catalog_endpoint += "?" + urlencode(catalog_query)
        catalog_result = self._agent_request(
            "GET",
            catalog_endpoint,
            timeout=15,
            console_principal=principal,
        )
        if not catalog_result.get("ok"):
            code = str(catalog_result.get("error_code") or "PLUGIN_CATALOG_UNAVAILABLE")
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                f"插件目录当前不可用（{code}），所有项目已阻断运行。",
                can_manage,
            )
        packages, instances, unsupported = normalize_automation_plugin_catalog(
            catalog_result.get("data")
        )
        data = catalog_result.get("data")
        if isinstance(data, dict) and "project_policies" in data:
            policy_data = data["project_policies"]
            raw_policies = policy_data.get("items") if isinstance(policy_data, dict) else None
            safe_policies = normalize_automation_project_policy_items(raw_policies)
            policy_ids = [item["automation_id"] for item in safe_policies]
            policy_error = str(policy_data.get("error_code") or "") if isinstance(policy_data, dict) else "PROJECT_POLICY_RESPONSE_INVALID"
            if not isinstance(raw_policies, list) or len(policy_ids) != len(set(policy_ids)):
                policy_error = "PROJECT_POLICY_RESPONSE_INVALID"
                safe_policies = []
            by_id = {item["automation_id"]: item for item in safe_policies}
            for instance in instances:
                instance["catalog_policy"] = by_id.get(instance["automation_id"])
                instance["catalog_policy_error"] = policy_error
        hidden_automation_ids = normalize_hidden_automation_ids(data)
        raw_instances = data.get("instances") if isinstance(data, dict) else None
        if not isinstance(raw_instances, list):
            raw_instances = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw_instances, list):
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                "插件目录返回无效，所有项目已阻断运行。",
                can_manage,
            )
        raw_instance_ids = [
            str(item.get("automation_id") or "").strip()
            if isinstance(item, dict)
            else ""
            for item in raw_instances
        ]
        normalized_instance_ids = [
            str(item.get("automation_id") or "").strip()
            for item in instances
        ]
        expected_instance_ids = [identity for identity in raw_instance_ids if identity not in unsupported]
        if any(not AUTOMATION_PROJECT_ID_RE.fullmatch(identity) for identity in raw_instance_ids) or expected_instance_ids != normalized_instance_ids:
            return (
                [],
                [],
                [],
                [],
                frozenset(),
                "插件目录实例身份无法核验，暂不能展示新的运行状态。",
                can_manage,
            )

        requires_workers = any(
            str(item.get("execution_platform") or "").strip().lower() == "windows"
            or bool(item.get("worker_required"))
            for item in [*packages, *instances]
        )
        if not requires_workers:
            return (
                packages,
                instances,
                [],
                unsupported,
                hidden_automation_ids,
                "",
                can_manage,
            )

        workers_result = self._agent_request(
            "GET",
            AUTOMATION_WORKERS_ENDPOINT,
            timeout=12,
            console_principal=principal,
        )
        workers = (
            normalize_automation_workers(workers_result.get("data"))
            if workers_result.get("ok")
            else []
        )
        warning = ""
        if not workers_result.get("ok") and any(
            item.get("execution_platform") == "windows" for item in instances
        ):
            code = str(workers_result.get("error_code") or "WORKERS_UNAVAILABLE")
            warning = f"工作节点列表当前不可用（{code}），相关项目已阻断。"
        return (
            packages,
            instances,
            workers,
            unsupported,
            hidden_automation_ids,
            warning,
            can_manage,
        )

    def _load_automation_project_policies(
        self,
        handler: BaseHTTPRequestHandler,
        tasks: list[dict[str, Any]],
        *,
        timeout_seconds: float = 12,
    ) -> tuple[str, bool]:
        user = getattr(handler, "current_admin_user", None)
        principal = self._mysql_console_principal(user)
        can_manage = bool(
            principal and "super_admin" in list(principal.get("roles") or [])
        )
        governed_tasks = [task for task in tasks if task.get("plugin")]
        unlinked_tasks = [
            task for task in tasks if task.get("automation_link_missing")
        ]
        for task in unlinked_tasks:
            # A row without persisted project identity is not an automation
            # project.  Do not derive a policy or pending endpoint from its task ID.
            task["approval_policy"] = None
        blocked_tasks = [
            task
            for task in tasks
            if not task.get("plugin") and not task.get("automation_link_missing")
        ]
        for task in blocked_tasks:
            automation_id = str(task.get("task_id") or "")
            policy = build_automation_project_policy_view(
                automation_id,
                None,
                load_error="插件缺失，项目权限与运行均已阻断。",
            )
            task["approval_policy"] = policy
            apply_automation_project_execution_gate(task, policy)
        if principal is None:
            warning = "项目权限只对真实的数据库管理员会话开放。"
            for task in governed_tasks:
                automation_id = str(task.get("task_id") or "")
                policy = build_automation_project_policy_view(
                    automation_id,
                    None,
                    load_error=warning,
                )
                task["approval_policy"] = policy
                apply_automation_project_execution_gate(task, policy)
            return warning, False

        if not governed_tasks:
            return "", can_manage
        if all("catalog_policy" in task["plugin"] for task in governed_tasks):
            errors = {task["plugin"].get("catalog_policy_error") for task in governed_tasks} - {"", None}
            result = {"ok": not errors, "error_code": "PROJECT_POLICY_SERVICE_UNAVAILABLE" if errors else "",
                "data": {"items": [task["plugin"]["catalog_policy"] for task in governed_tasks]}}
        else:
            # Compatibility with the existing full catalog/detail endpoint.
            result = self._agent_request(
                "GET",
                AUTOMATION_PROJECT_POLICY_ENDPOINT + "?" + urlencode({"automation_ids": ",".join(str(task["task_id"]) for task in governed_tasks)}),
                timeout=max(0.5, float(timeout_seconds)),
                console_principal=principal,
            )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_items = data.get("items") if isinstance(data, dict) else None
        safe_items = normalize_automation_project_policy_items(raw_items)
        if not result.get("ok") or not isinstance(raw_items, list):
            error_code = str(
                result.get("error_code") or "PROJECT_POLICY_SERVICE_UNAVAILABLE"
            ).strip()
            warning = f"项目权限当前不可用（{error_code}），任务配置仍可查看。"
            items_by_automation_id: dict[str, dict[str, Any]] = {}
        else:
            warning = ""
            items_by_automation_id = {
                str(item["automation_id"]): item for item in safe_items
            }

        for task in governed_tasks:
            automation_id = str(task.get("task_id") or "")
            policy = build_automation_project_policy_view(
                automation_id,
                items_by_automation_id.get(automation_id),
                load_error=warning,
            )
            task["approval_policy"] = policy
            apply_automation_project_execution_gate(task, policy)
        return warning, can_manage

    @staticmethod
    def _automation_project_id(value: Any) -> str:
        automation_id = str(value or "").strip()
        return automation_id if AUTOMATION_PROJECT_ID_RE.fullmatch(automation_id) else ""

    @staticmethod
    def _automation_project_policy_from_result(
        result: dict[str, Any],
        automation_id: str,
    ) -> dict[str, Any] | None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_policy = data.get("policy") if isinstance(data.get("policy"), dict) else data
        safe = normalize_automation_project_policy_items([raw_policy])
        if len(safe) != 1 or safe[0]["automation_id"] != automation_id:
            return None
        return safe[0]

    @staticmethod
    def _automation_pending_from_result(
        result: dict[str, Any],
        automation_id: str,
    ) -> dict[str, Any] | None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        raw_pending = data.get("pending") if isinstance(data.get("pending"), dict) else data
        return normalize_automation_pending_approvals(
            raw_pending,
            expected_automation_id=automation_id,
        )

    def _automation_project_agent_error(
        self,
        handler: BaseHTTPRequestHandler,
        result: dict[str, Any],
        *,
        automation_id: str,
        fallback_code: str,
        fallback_message: str,
        include_pending: bool = False,
    ) -> None:
        try:
            status = HTTPStatus(int(result.get("status")))
        except (TypeError, ValueError):
            status = HTTPStatus.BAD_GATEWAY
        if status not in {
            HTTPStatus.BAD_REQUEST,
            HTTPStatus.FORBIDDEN,
            HTTPStatus.NOT_FOUND,
            HTTPStatus.CONFLICT,
            HTTPStatus.UNPROCESSABLE_ENTITY,
            HTTPStatus.TOO_MANY_REQUESTS,
            HTTPStatus.SERVICE_UNAVAILABLE,
        }:
            status = HTTPStatus.BAD_GATEWAY
        safe_data: dict[str, Any] | None = None
        if include_pending:
            pending = self._automation_pending_from_result(result, automation_id)
            if pending is not None:
                safe_data = {"pending": pending}
        self._control_plane_error(
            handler,
            status,
            str(result.get("error_code") or fallback_code)[:128],
            normalize_feedback_text(str(result.get("error") or fallback_message))[:1000],
            data=safe_data,
        )

    def _handle_automation_project_approval_policy(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PROJECT_NOT_FOUND",
                "自动化项目不存在。",
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以修改项目运行权限。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        allowed_fields = {
            "mode",
            "request_id",
            "comment",
            "expected_policy_version",
            "expected_project_configuration_version",
        }
        if set(values) - allowed_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "UNSUPPORTED_POLICY_FIELDS",
                "项目权限请求包含不支持的字段，请刷新后重试。",
            )
            return
        mode = str(values.get("mode") or "").strip().upper()
        if mode not in AUTOMATION_PROJECT_POLICY_MODES:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_APPROVAL_POLICY_MODE",
                "项目权限只能是完全自动或每次运行审批。",
            )
            return
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        comment = normalize_feedback_text(str(values.get("comment") or "")).strip()
        expected_policy_version = values.get("expected_policy_version")
        expected_project_configuration_version = values.get(
            "expected_project_configuration_version"
        )
        if not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "BROWSER_REQUEST_UUID_REQUIRED",
                "缺少有效且稳定的请求标识，项目权限未保存。",
            )
            return
        if len(comment) > AUTOMATION_PROJECT_COMMENT_MAX_CHARS:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "COMMENT_TOO_LONG",
                "理由不能超过 500 个字符。",
            )
            return
        if any(
            isinstance(version, bool) or not isinstance(version, int) or version < 1
            for version in (
                expected_policy_version,
                expected_project_configuration_version,
            )
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PROJECT_POLICY_VERSION_REQUIRED",
                "项目权限或配置版本已缺失，请刷新页面后重试。",
            )
            return
        result = self._agent_request(
            "POST",
            f"/internal/v1/automation-projects/{quote(automation_id, safe='')}/approval-policy",
            payload={
                "mode": mode,
                "request_id": request_id,
                "comment": comment,
                "expected_policy_version": expected_policy_version,
                "expected_project_configuration_version": expected_project_configuration_version,
            },
            timeout=20,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PROJECT_POLICY_UPDATE_FAILED",
                fallback_message="项目权限保存失败。",
            )
            return
        self._clear_automation_plugin_catalog_cache()
        policy = self._automation_project_policy_from_result(result, automation_id)
        if policy is None:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PROJECT_POLICY_RESPONSE",
                "智能服务未返回完整的项目权限结果。",
            )
            return
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {"policy": build_automation_project_policy_view(automation_id, policy)},
                "message": "项目权限已保存。",
            },
        )

    def _handle_automation_project_pending_approvals_get(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PROJECT_NOT_FOUND",
                "自动化项目不存在。",
            )
            return
        trusted_context = self._control_plane_read_context(handler)
        if trusted_context is None:
            return
        result = self._agent_request(
            "GET",
            f"/internal/v1/automation-projects/{quote(automation_id, safe='')}/pending-approvals",
            timeout=12,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PENDING_APPROVALS_UNAVAILABLE",
                fallback_message="待审批集合暂时不可用。",
            )
            return
        pending = self._automation_pending_from_result(result, automation_id)
        if pending is None:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PENDING_APPROVALS_RESPONSE",
                "智能服务未返回有效的待审批集合。",
            )
            return
        self._send_json(handler, HTTPStatus.OK, {"ok": True, "data": {"pending": pending}})

    def _handle_automation_project_pending_approvals_action(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
        action: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id or action not in {"approve", "reject"}:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_APPROVAL_ACTION_NOT_FOUND",
                "批量审批操作不存在。",
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以批量处理自动化审批。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        allowed_fields = {"expected_pending_set_hash", "request_id", "comment"}
        if set(values) - allowed_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "UNSUPPORTED_PENDING_APPROVAL_FIELDS",
                "批量审批请求不能包含审批标识或计划摘要。",
            )
            return
        expected_hash = str(values.get("expected_pending_set_hash") or "").strip()
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        comment = normalize_feedback_text(str(values.get("comment") or "")).strip()
        if not AUTOMATION_PENDING_SET_HASH_RE.fullmatch(expected_hash):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "EXPECTED_PENDING_SET_HASH_REQUIRED",
                "待审批集合已缺失，请原位刷新后重试。",
            )
            return
        if not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "BROWSER_REQUEST_UUID_REQUIRED",
                "缺少有效且稳定的请求标识，批量审批未提交。",
            )
            return
        if len(comment) > AUTOMATION_PROJECT_COMMENT_MAX_CHARS:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "COMMENT_TOO_LONG",
                "说明不能超过 500 个字符。",
            )
            return
        result = self._agent_request(
            "POST",
            (
                f"/internal/v1/automation-projects/{quote(automation_id, safe='')}"
                f"/pending-approvals/{action}"
            ),
            payload={
                "expected_pending_set_hash": expected_hash,
                "request_id": request_id,
                "comment": comment,
            },
            timeout=20,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PENDING_APPROVAL_ACTION_FAILED",
                fallback_message="批量审批操作失败。",
                include_pending=True,
            )
            return
        pending = self._automation_pending_from_result(result, automation_id)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        batch = normalize_automation_approval_batch_result(
            data,
            expected_automation_id=automation_id,
            expected_decision="APPROVED" if action == "approve" else "REJECTED",
        )
        if pending is None or batch is None:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PENDING_APPROVAL_ACTION_RESPONSE",
                "智能服务未返回完整的批量审批结果。",
            )
            return
        safe_data: dict[str, Any] = {
            "pending": pending,
            "decided_count": batch["decided_count"],
        }
        if action == "approve":
            safe_data["run_receipts"] = batch["run_receipts"]
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": safe_data,
                "message": "已批量通过。" if action == "approve" else "已批量驳回。",
            },
        )

    def _handle_automation_plugin_configuration_save(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PLUGIN_INSTANCE_NOT_FOUND",
                "插件实例不存在。",
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以保存自动化项目设置。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        allowed_fields = {
            "config",
            "account_bindings",
            "resource_bindings",
            "enabled_entrypoints",
            "device_id",
            "schedule",
            "request_id",
            "expected_project_configuration_version",
        }
        if set(values) != allowed_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_CONFIGURATION_FIELDS",
                "项目设置字段不完整或包含不支持的字段。",
            )
            return

        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        expected_version = values.get("expected_project_configuration_version")
        config_valid, config = _normalize_browser_plugin_config_value(values.get("config"))
        accounts_valid, account_bindings = _normalize_browser_plugin_bindings(
            values.get("account_bindings")
        )
        resources_valid, resource_bindings = _normalize_browser_plugin_bindings(
            values.get("resource_bindings")
        )
        enabled_entrypoints, entrypoints_valid = _normalize_plugin_entrypoints(
            values.get("enabled_entrypoints"),
            # The Agent validates the submitted IDs against the project's signed
            # v1 manifest or installed v2 contribution contract.
            runtime_model="SERVICE_V2",
        )
        raw_device_id = values.get("device_id")
        device_id = str(raw_device_id or "").strip()
        raw_schedule = values.get("schedule")
        schedule: dict[str, Any] = {}
        schedule_valid = False
        if isinstance(raw_schedule, dict) and set(raw_schedule) == {"kind", "times", "enabled"}:
            schedule_kind = str(raw_schedule.get("kind") or "").strip().lower()
            raw_times = raw_schedule.get("times")
            schedule_enabled = raw_schedule.get("enabled")
            if isinstance(raw_times, list) and isinstance(schedule_enabled, bool):
                schedule_times = [str(item or "").strip() for item in raw_times]
                times_valid = (
                    len(schedule_times) <= AUTOMATION_PLUGIN_SCHEDULE_MAX_DAILY_TIMES
                    and len(schedule_times) == len(set(schedule_times))
                    and all(
                        re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item)
                        for item in schedule_times
                    )
                )
                schedule_valid = bool(
                    schedule_kind in {"none", "daily_times", "startup"}
                    and times_valid
                    and (
                        (schedule_kind == "none" and not schedule_times and not schedule_enabled)
                        or (schedule_kind == "startup" and not schedule_times)
                        or (schedule_kind == "daily_times" and bool(schedule_times))
                    )
                )
                if schedule_valid:
                    schedule = {
                        "kind": schedule_kind,
                        "times": sorted(schedule_times),
                        "enabled": schedule_enabled,
                    }
        try:
            config_size = len(
                json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        except (TypeError, ValueError):
            config_size = AUTOMATION_PLUGIN_CONFIG_MAX_BYTES + 1
        if (
            not request_id
            or isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
            or not config_valid
            or config_size > AUTOMATION_PLUGIN_CONFIG_MAX_BYTES
            or not accounts_valid
            or not resources_valid
            or not entrypoints_valid
            or not schedule_valid
            or (raw_device_id is not None and not device_id)
            or (device_id and not AUTOMATION_WORKER_ID_RE.fullmatch(device_id))
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_CONFIGURATION",
                "项目设置、绑定、运行入口或配置版本无效。",
            )
            return

        payload = {
            "config": config,
            "account_bindings": account_bindings,
            "resource_bindings": resource_bindings,
            "enabled_entrypoints": enabled_entrypoints,
            "device_id": device_id or None,
            "schedule": schedule,
            "request_id": request_id,
            "expected_project_configuration_version": expected_version,
        }
        result = self._agent_request(
            "PUT",
            f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/configuration",
            payload=payload,
            timeout=25,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_CONFIGURATION_SAVE_FAILED",
                fallback_message="自动化项目设置保存失败。",
            )
            return

        raw_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        new_version = raw_data.get("project_configuration_version")
        if (
            isinstance(new_version, bool)
            or not isinstance(new_version, int)
            or new_version < 1
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PLUGIN_CONFIGURATION_RESPONSE",
                "智能服务未返回新的项目配置版本，请刷新页面核对后重试。",
            )
            return
        response_data: dict[str, Any] = {
            "automation_id": automation_id,
            "project_configuration_version": new_version,
        }
        if isinstance(raw_data.get("configured"), bool):
            response_data["configured"] = raw_data["configured"]
        runtime_state = str(
            raw_data.get("schedule_runtime_state") or "REFRESH_FAILED"
        ).strip().upper()
        if runtime_state not in AUTOMATION_PLUGIN_SCHEDULE_RUNTIME_STATES:
            runtime_state = "REFRESH_FAILED"
        refresh_completed = raw_data.get("scheduler_refresh_completed") is True
        if (
            runtime_state in {"ACTIVE", "DISABLED", "ENTRYPOINT_DISABLED"}
            and not refresh_completed
        ):
            runtime_state = "REFRESH_FAILED"
        response_data["schedule_runtime_state"] = runtime_state
        response_data["schedule_runtime_enabled"] = bool(
            raw_data.get("schedule_runtime_enabled") is True
            and runtime_state == "ACTIVE"
        )
        response_data["scheduler_refresh_completed"] = refresh_completed
        messages = {
            "ACTIVE": "项目设置已保存，运行中定时已按新配置刷新。",
            "DISABLED": "项目设置已保存，运行中定时已关闭。",
            "ENTRYPOINT_DISABLED": (
                "项目设置已保存；定时时间已保留，但系统定时入口关闭，当前不会运行。"
            ),
            "BLOCKED_GENERATION": (
                "项目设置已保存，但新运行代际尚未就绪；请使用同一请求重试，"
                "系统不会沿用旧权限执行。"
            ),
            "REFRESH_FAILED": (
                "项目设置已保存，但运行中调度器刷新失败；旧任务集保持不变，"
                "请使用同一请求重试。"
            ),
        }
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": response_data,
                "message": messages[runtime_state],
            },
        )

    def _handle_automation_plugin_schedule_save(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        """Save only Console-owned scheduling state for one plugin instance."""

        automation_id = self._automation_project_id(automation_id)
        if not automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PLUGIN_INSTANCE_NOT_FOUND",
                "插件实例不存在。",
            )
            return
        trusted_context = self._control_plane_write_context(handler)
        if trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以保存定时计划。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        if set(values) != {
            "schedule",
            "request_id",
            "expected_project_configuration_version",
        }:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_SCHEDULE_FIELDS",
                "定时计划包含不支持的字段。",
            )
            return
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        expected_version = values.get("expected_project_configuration_version")
        raw_schedule = values.get("schedule")
        schedule: dict[str, Any] = {}
        if isinstance(raw_schedule, dict) and set(raw_schedule) == {"kind", "times", "enabled"}:
            kind = str(raw_schedule.get("kind") or "").strip().lower()
            times_raw = raw_schedule.get("times")
            enabled = raw_schedule.get("enabled")
            if isinstance(times_raw, list) and isinstance(enabled, bool):
                times = [str(item or "").strip() for item in times_raw]
                if (
                    kind in {"none", "daily_times", "startup"}
                    and len(times) <= AUTOMATION_PLUGIN_SCHEDULE_MAX_DAILY_TIMES
                    and len(times) == len(set(times))
                    and all(
                        re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item)
                        for item in times
                    )
                    and (
                        (kind == "none" and not times and not enabled)
                        or (kind == "startup" and not times)
                        or (kind == "daily_times" and bool(times))
                    )
                ):
                    schedule = {"kind": kind, "times": sorted(times), "enabled": enabled}
        if (
            not request_id
            or isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
            or not schedule
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_SCHEDULE",
                "定时计划或配置版本无效，请刷新后重试。",
            )
            return
        result = self._agent_request(
            "PUT",
            f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/schedule",
            payload={
                "schedule": schedule,
                "request_id": request_id,
                "expected_project_configuration_version": expected_version,
            },
            timeout=25,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_SCHEDULE_SAVE_FAILED",
                fallback_message="定时计划保存失败。",
            )
            return
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        new_version = data.get("project_configuration_version")
        runtime_state = str(data.get("schedule_runtime_state") or "REFRESH_FAILED").upper()
        if (
            isinstance(new_version, bool)
            or not isinstance(new_version, int)
            or new_version < 1
            or runtime_state not in AUTOMATION_PLUGIN_SCHEDULE_RUNTIME_STATES
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PLUGIN_SCHEDULE_RESPONSE",
                "定时计划可能已保存，请刷新页面核对状态。",
            )
            return
        self._clear_automation_plugin_catalog_cache()
        messages = {
            "ACTIVE": "定时计划已保存并生效。",
            "DISABLED": "定时计划已关闭。",
            "ENTRYPOINT_DISABLED": "定时计划已保存，但插件没有开放系统定时入口。",
            "BLOCKED_GENERATION": "定时计划已保存，运行环境就绪后才会执行。",
            "REFRESH_FAILED": "定时计划已保存，但调度状态尚未同步，请稍后刷新。",
        }
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {
                    "automation_id": automation_id,
                    "project_configuration_version": new_version,
                    "schedule_runtime_state": runtime_state,
                    "schedule_runtime_enabled": data.get("schedule_runtime_enabled") is True,
                    "scheduler_refresh_completed": data.get("scheduler_refresh_completed") is True,
                },
                "message": messages[runtime_state],
            },
        )
