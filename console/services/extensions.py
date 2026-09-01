"""Read-only extension-center projections over the existing automation catalog."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from console.app_support import *  # noqa: F403
from shared.business_modules import BUSINESS_MODULE_CATALOG


_EXTENSION_RUNTIME_MODELS = frozenset({"ACTION_V1", "SERVICE_V2"})
_FIXED_MODULE_CODES = frozenset(item.module_code for item in BUSINESS_MODULE_CATALOG)
_EXTENSION_DISPLAY_NAMES = {
    "clock_in_dual": "融辉到港与离港打卡",
    "clockin_daxiang": "融辉到港与离港打卡",
    "self_pickup_problem_upload": "自提问题件上传",
    "split_pending_problem_upload": "分批未到问题件上传",
    "sync_arrival_stats": "到货统计",
    "arrival_stats": "到货统计",
    "sync_arrive_list": "到货清单同步",
    "arrive_list": "到货清单同步",
    "sync_customer_service_problems": "客服问题件同步",
    "customer_problems_shadow": "客服问题件同步",
    "sync_daily_send_orders": "每日寄件同步",
    "send_order": "每日寄件同步",
    "sync_daily_should_sign": "每日应签",
    "daily_sign": "每日应签",
    "sync_delivery_status": "签收状态同步",
    "delivery_status": "签收状态同步",
    "sync_finance_bills": "财务账单同步",
    "finance_bills": "财务账单同步",
    "sync_scan_codes": "扫描码同步",
    "scan_codes": "扫描码同步",
    "sync_site_send_list": "网点出港同步",
    "site_send": "网点出港同步",
    "sync_yunda_dispatch_forecast": "韵达派件预测",
    "yunda_dispatch_forecast": "韵达派件预测",
    "sync_yunda_send_waybills": "韵达寄件运单同步",
    "yunda_send_waybills": "韵达寄件运单同步",
}


class ExtensionsServiceMixin:
    """Present packages and their installed projects without another state source."""

    @staticmethod
    def _extension_display_status(
        instances: list[dict[str, Any]],
    ) -> tuple[str, str]:
        if not instances:
            return "尚未使用", "muted"
        if any(
            item["state"].upper() == "ERROR"
            or item["state"].upper().startswith("BLOCKED")
            for item in instances
        ):
            return "需要处理", "error"
        if any(not item["configured"] for item in instances):
            return "需要设置", "needs_configuration"
        if any(item["enabled"] for item in instances):
            return "使用中", "enabled"
        return "已暂停", "muted"

    @staticmethod
    def _extension_display_summary(package: Mapping[str, Any]) -> str:
        summary = str(package.get("action_summary") or "").strip()
        if not summary:
            return "此扩展暂时没有说明。"
        for separator in ("；", "。", "\n"):
            summary = summary.split(separator, 1)[0].strip()
        return f"{summary[:72]}{'…' if len(summary) > 72 else ''}"

    @staticmethod
    def _extension_display_name(
        package: Mapping[str, Any], instances: list[dict[str, Any]]
    ) -> str:
        plugin_id = str(package["plugin_id"])
        known_name = _EXTENSION_DISPLAY_NAMES.get(plugin_id)
        if known_name:
            return known_name
        name = str(package.get("name") or package["plugin_id"]).strip()
        if any("\u4e00" <= character <= "\u9fff" for character in name):
            return name
        if instances:
            instance_name = str(instances[0]["instance_name"]).strip()
            return f"{instance_name}{'等' if len(instances) > 1 else ''}"
        return name.replace("_", " ")

    @staticmethod
    def _extension_package_view(
        package: Mapping[str, Any], instances: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Return the intentionally small, browser-safe extension projection."""

        display_status, display_status_kind = (
            ExtensionsServiceMixin._extension_display_status(instances)
        )
        account_roles = package.get("account_roles")
        resource_roles = package.get("resource_roles")
        provided_services = package.get("provided_services")
        account_permissions = [
            str(item.get("label") or "账号权限不可用")
            for item in account_roles
            if isinstance(item, Mapping) and str(item.get("label") or "").strip()
        ] if isinstance(account_roles, list) else []
        resource_permissions = [
            str(item.get("label") or "资源权限不可用")
            for item in resource_roles
            if isinstance(item, Mapping) and str(item.get("label") or "").strip()
        ] if isinstance(resource_roles, list) else []
        service_permissions = [
            str(item) for item in provided_services if isinstance(item, str) and item.strip()
        ] if isinstance(provided_services, list) else []
        permissions_available = package.get("contract_supported") is True
        unavailable = ["不可用"]
        return {
            "plugin_id": str(package["plugin_id"]),
            "name": str(package.get("name") or package["plugin_id"]),
            "display_name": ExtensionsServiceMixin._extension_display_name(
                package, instances
            ),
            "version": str(package["version"]),
            "runtime_model": str(package["runtime_model"]),
            "runtime_model_label": str(package["runtime_model_label"]),
            "execution_platform": str(package["execution_platform"]),
            "platform_label": str(package["platform_label"]),
            "plugin_api": str(package.get("plugin_api") or ""),
            "entrypoints": list(package.get("entrypoints") or []),
            "action_summary": str(package.get("action_summary") or ""),
            "display_summary": ExtensionsServiceMixin._extension_display_summary(
                package
            ),
            "configuration_summary": str(package.get("configuration_summary") or ""),
            "permissions": (
                {"label": "账号", "items": account_permissions or (["无"] if permissions_available else unavailable)},
                {"label": "资源", "items": resource_permissions or (["无"] if permissions_available else unavailable)},
                {"label": "服务", "items": service_permissions or (["无"] if permissions_available else unavailable)},
            ),
            "instance_count": len(instances),
            "instance_count_label": (
                f"已用于 {len(instances)} 个项目" if instances else "还没有项目使用"
            ),
            "display_status": display_status,
            "display_status_kind": display_status_kind,
            "instances": instances,
        }

    @staticmethod
    def _extension_instance_view(instance: Mapping[str, Any]) -> dict[str, Any]:
        """Keep configuration, bindings, source paths and opaque payloads out of HTML."""

        state = str(instance.get("state") or "UNKNOWN")
        configured = instance.get("configured") is True
        enabled = instance.get("enabled") is True
        if state.upper() == "ERROR" or state.upper().startswith("BLOCKED"):
            display_status = str(instance.get("status_label") or "需要处理")
            display_status_kind = "error"
        elif not configured:
            display_status = "需要设置"
            display_status_kind = "needs_configuration"
        elif enabled:
            display_status = "使用中"
            display_status_kind = "enabled"
        else:
            display_status = "已暂停"
            display_status_kind = "muted"
        return {
            "automation_id": str(instance["automation_id"]),
            "instance_name": str(instance.get("instance_name") or instance["automation_id"]),
            "version": str(instance.get("version") or ""),
            "active_version": str(instance.get("active_version") or ""),
            "target_version": str(instance.get("target_version") or ""),
            "runtime_model": str(instance.get("runtime_model") or ""),
            "runtime_model_label": str(instance.get("runtime_model_label") or ""),
            "state": state,
            "status_label": str(instance.get("status_label") or "状态未知"),
            "display_status": display_status,
            "display_status_kind": display_status_kind,
            "configured": configured,
            "enabled": enabled,
            "record_version": int(instance["record_version"]),
            "lifecycle_actions_allowed": instance.get("lifecycle_actions_allowed") is True,
            "enable_allowed": instance.get("enable_allowed") is True,
            "disable_allowed": instance.get("disable_allowed") is True,
        }

    def _extension_catalog(
        self, handler: BaseHTTPRequestHandler
    ) -> tuple[list[dict[str, Any]], str, bool]:
        (
            packages,
            instances,
            _workers,
            _unsupported,
            _hidden,
            warning,
            can_manage,
        ) = self._load_automation_plugin_catalog(handler)
        package_ids = {
            str(package.get("plugin_id") or "")
            for package in packages
            if str(package.get("plugin_id") or "") not in _FIXED_MODULE_CODES
            and str(package.get("runtime_model") or "") in _EXTENSION_RUNTIME_MODELS
        }
        instances_by_plugin: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for instance in instances:
            plugin_id = str(instance.get("plugin_id") or "")
            if (
                plugin_id not in package_ids
                or str(instance.get("runtime_model") or "") not in _EXTENSION_RUNTIME_MODELS
            ):
                continue
            instances_by_plugin[plugin_id].append(self._extension_instance_view(instance))
        views = [
            self._extension_package_view(
                package,
                sorted(
                    instances_by_plugin.get(str(package["plugin_id"]), []),
                    key=lambda item: (item["instance_name"], item["automation_id"]),
                ),
            )
            for package in packages
            if str(package.get("plugin_id") or "") in package_ids
        ]
        return sorted(views, key=lambda item: (item["name"], item["plugin_id"])), warning, can_manage

    def _ensure_extension_view_access(self, handler: BaseHTTPRequestHandler) -> bool:
        user = getattr(handler, "current_admin_user", None) or current_admin_user()
        if self._can_see_extensions_navigation(user):
            return True
        self._send_json(
            handler,
            HTTPStatus.FORBIDDEN,
            {
                "ok": False,
                "error_code": "MYSQL_ADMIN_REQUIRED",
                "message": "扩展中心只对真实的数据库管理员会话开放。",
            },
        )
        return False

    def _render_extensions(
        self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]
    ) -> None:
        if not self._ensure_extension_view_access(handler):
            return
        packages, warning, can_manage = self._extension_catalog(handler)
        template = self.template_env.get_template("extensions.html")
        body = template.render(
            app_title=self.settings.app_title,
            packages=packages,
            extension_warning=warning,
            can_manage_extensions=can_manage,
            detail=None,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)

    def _render_extension_detail(
        self,
        handler: BaseHTTPRequestHandler,
        plugin_id: str,
        query: dict[str, list[str]],
    ) -> None:
        if not self._ensure_extension_view_access(handler):
            return
        packages, warning, can_manage = self._extension_catalog(handler)
        detail = next((item for item in packages if item["plugin_id"] == plugin_id), None)
        if detail is None:
            self._send_text(handler, HTTPStatus.NOT_FOUND, "Extension not found.")
            return
        template = self.template_env.get_template("extensions.html")
        body = template.render(
            app_title=self.settings.app_title,
            packages=(),
            extension_warning=warning,
            can_manage_extensions=can_manage,
            detail=detail,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
        )
        self._send_html(handler, body)
