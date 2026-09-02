"""Console-side ZIP plugin upload and lifecycle management helpers."""

import uuid

from console.app_support import *  # noqa: F403
from shared.service_identity import (
    ConsoleIdentityError,
    build_console_identity_headers,
)


_SERVICE_V2_INSTALL_INTENT_FIELDS = frozenset(
    {
        "instance_name",
        "permissions_confirmed",
    }
)
_SERVICE_V2_INSPECTION_FIELDS = frozenset(
    {
        "plugin_id",
        "name",
        "version",
        "host_api",
        "permissions",
        "account_roles",
        "resource_roles",
        "config_schema",
        "contributions",
        "scheduling",
        "settings_ui",
    }
)
_SERVICE_V2_INTENT_MAX_BYTES = 16 * 1024
_SERVICE_V2_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SERVICE_V2_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SERVICE_V2_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SERVICE_V2_BINDING_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_SERVICE_V2_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SERVICE_V2_FORBIDDEN_BROWSER_AUTHORITY = frozenset(
    {
        "automation_id",
        "device_id",
        "manifest",
        "manifest_sha256",
        "package_digest",
        "package_sha256",
    }
)


class AutomationPluginManagementServiceMixin:
    def _automation_plugin_settings_bridge_token(
        self,
        automation_id: str,
        actor_id: str,
    ) -> str:
        payload = {
            "automation_id": automation_id,
            "actor_id": actor_id,
            "expires_at": int(time.time()) + 30 * 60,
            "nonce": secrets.token_urlsafe(18),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = hmac.new(
            str(self._session_secret).encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{encoded}.{signature}"

    def _automation_plugin_settings_bridge_token_valid(
        self,
        token: Any,
        *,
        automation_id: str,
        actor_id: str,
    ) -> bool:
        encoded, separator, signature = str(token or "").partition(".")
        if not separator or len(encoded) > 2048 or not re.fullmatch(r"[0-9a-f]{64}", signature):
            return False
        expected = hmac.new(
            str(self._session_secret).encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(payload, dict)
            and set(payload) == {"automation_id", "actor_id", "expires_at", "nonce"}
            and payload.get("automation_id") == automation_id
            and payload.get("actor_id") == actor_id
            and isinstance(payload.get("expires_at"), int)
            and int(time.time()) <= payload["expires_at"] <= int(time.time()) + 30 * 60
            and isinstance(payload.get("nonce"), str)
            and 12 <= len(payload["nonce"]) <= 128
        )

    def _render_automation_plugin_settings(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
        query: dict[str, list[str]],
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        trusted_context = self._control_plane_read_context(handler)
        if not automation_id or trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以打开插件设置。",
            )
            return
        result = self._agent_request(
            "GET",
            f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/settings-context",
            timeout=15,
            console_principal=trusted_context["_console_principal"],
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else None
        settings_ui = data.get("settings_ui") if isinstance(data, dict) else None
        if not result.get("ok") or not isinstance(data, dict):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_SETTINGS_UNAVAILABLE",
                fallback_message="插件设置暂时无法打开。",
            )
            return
        if settings_ui != {"entry": "settings/index.html", "bridge_api": "1.0.0"}:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "PLUGIN_SETTINGS_UI_UNAVAILABLE",
                "这个插件不需要单独设置。",
            )
            return
        actor_id = str(trusted_context["actor"].get("actor_id") or "")
        bridge_session = self._automation_plugin_settings_bridge_token(
            automation_id,
            actor_id,
        )
        template = self.template_env.get_template("automation_plugin_settings.html")
        body = template.render(
            app_title=self.settings.app_title,
            message=query.get("message", [""])[0],
            message_kind=query.get("kind", ["info"])[0],
            automation_id=automation_id,
            instance_name=normalize_feedback_text(data.get("instance_name") or "插件设置"),
            plugin_name=normalize_feedback_text(data.get("plugin_name") or "插件"),
            plugin_version=str(data.get("plugin_version") or ""),
            enabled=data.get("enabled") is True,
            configured=data.get("configured") is True,
            bridge_session=bridge_session,
        )
        self._send_html(handler, body)

    def _handle_automation_plugin_settings_asset(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
        asset_path: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        trusted_context = self._control_plane_read_context(handler)
        if not automation_id or trusted_context is None:
            return
        raw_path = unquote(str(asset_path or ""))
        if (
            not raw_path
            or len(raw_path) > 512
            or "\\" in raw_path
            or any(part in {"", ".", ".."} for part in raw_path.split("/"))
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_SETTINGS_ASSET_INVALID",
                "插件设置资源地址无效。",
            )
            return
        endpoint = (
            f"/internal/v1/automation/instances/{quote(automation_id, safe='')}"
            f"/settings-assets/{quote(raw_path, safe='/')}"
        )
        result = self._agent_binary_request(
            endpoint,
            timeout=15,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok") or not isinstance(result.get("data"), bytes):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "PLUGIN_SETTINGS_ASSET_UNAVAILABLE",
                "插件设置资源暂时无法读取。",
            )
            return
        content_type = str(result.get("content_type") or "application/octet-stream").lower()
        allowed_types = {
            "text/html",
            "text/css",
            "text/javascript",
            "application/javascript",
            "application/json",
            "image/svg+xml",
            "image/png",
            "image/jpeg",
            "image/webp",
            "font/woff2",
        }
        if content_type not in allowed_types:
            self._control_plane_error(
                handler,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "PLUGIN_SETTINGS_ASSET_TYPE_UNSUPPORTED",
                "插件设置资源类型不受支持。",
            )
            return
        self._send_bytes(
            handler,
            HTTPStatus.OK,
            result["data"],
            content_type,
            cache_control="private, max-age=300",
            extra_headers={
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; font-src 'self'; connect-src 'none'; "
                    "frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _handle_automation_plugin_settings_bridge(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        trusted_context = self._control_plane_write_context(handler)
        if not automation_id or trusted_context is None:
            return
        if "super_admin" not in list(trusted_context.get("actor_roles") or []):
            self._control_plane_error(
                handler,
                HTTPStatus.FORBIDDEN,
                "SUPER_ADMIN_REQUIRED",
                "只有超级管理员可以保存插件设置。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        operation = str(values.get("operation") or "").strip().lower()
        actor_id = str(trusted_context["actor"].get("actor_id") or "")
        if (
            set(values) != {"bridge_session", "operation", "payload"}
            or operation not in {"context", "save"}
            or not self._automation_plugin_settings_bridge_token_valid(
                values.get("bridge_session"),
                automation_id=automation_id,
                actor_id=actor_id,
            )
            or not isinstance(values.get("payload"), dict)
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_SETTINGS_BRIDGE_INVALID",
                "插件设置会话已失效，请返回自动化页面后重新打开。",
            )
            return
        if operation == "save":
            payload = values["payload"]
            expected_fields = {
                "config",
                "account_bindings",
                "resource_bindings",
                "request_id",
                "expected_project_configuration_version",
            }
            request_id = self._normalize_browser_request_uuid(payload.get("request_id"))
            expected_version = payload.get("expected_project_configuration_version")
            if (
                set(payload) != expected_fields
                or not request_id
                or isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 0
                or not all(
                    isinstance(payload.get(field), dict)
                    and self._service_v2_json_tree_is_safe(payload[field])
                    for field in ("config", "account_bindings", "resource_bindings")
                )
            ):
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "PLUGIN_SETTINGS_VALUES_INVALID",
                    "插件设置内容无效，请检查后重试。",
                )
                return
            result = self._agent_request(
                "PUT",
                f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/plugin-settings",
                payload={**payload, "request_id": request_id},
                timeout=25,
                console_principal=trusted_context["_console_principal"],
            )
            if not result.get("ok"):
                self._automation_project_agent_error(
                    handler,
                    result,
                    automation_id=automation_id,
                    fallback_code="PLUGIN_SETTINGS_SAVE_FAILED",
                    fallback_message="插件设置保存失败。",
                )
                return
            self._clear_automation_plugin_catalog_cache()
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "data": result.get("data") if isinstance(result.get("data"), dict) else {},
                    "message": "插件设置已保存。",
                },
            )
            return

        if values["payload"]:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_SETTINGS_CONTEXT_FIELDS_INVALID",
                "读取设置上下文时不能提交业务参数。",
            )
            return
        settings_result = self._agent_request(
            "GET",
            f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/settings-context",
            timeout=15,
            console_principal=trusted_context["_console_principal"],
        )
        catalog_result = self._agent_request(
            "GET",
            "/internal/v1/automation/plugins/catalog",
            timeout=15,
            console_principal=trusted_context["_console_principal"],
        )
        settings_data = settings_result.get("data")
        if not settings_result.get("ok") or not isinstance(settings_data, dict):
            self._automation_project_agent_error(
                handler,
                settings_result,
                automation_id=automation_id,
                fallback_code="PLUGIN_SETTINGS_CONTEXT_UNAVAILABLE",
                fallback_message="插件设置上下文暂时无法读取。",
            )
            return
        raw_accounts, account_warning = self._fetch_automation_accounts(
            force=False,
            prefer_cached=True,
            console_principal=trusted_context["_console_principal"],
        )
        safe_accounts = []
        for account in raw_accounts if not account_warning else []:
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("account_id") or "").strip()
            if not _SERVICE_V2_BINDING_ID_RE.fullmatch(account_id):
                continue
            status = account.get("status") if isinstance(account.get("status"), dict) else {}
            safe_accounts.append(
                {
                    "account_ref": account_id,
                    "name": normalize_feedback_text(account.get("name") or "业务账号")[:160],
                    "system": str(account.get("system") or "").strip().lower(),
                    "system_label": normalize_feedback_text(account.get("system_label") or "业务系统")[:80],
                    "available": bool(account.get("is_active", True))
                    and (
                        account.get("session_capable") is not True
                        or str(status.get("status") or "").lower() == "authenticated"
                    ),
                    "status_label": normalize_feedback_text(
                        account.get("status_label") or status.get("label") or "状态未知"
                    )[:80],
                }
            )
        safe_accounts.sort(key=lambda item: (item["system_label"], item["name"], item["account_ref"]))

        from console.services.automation_projects import _normalize_plugin_resources

        catalog_data = catalog_result.get("data") if isinstance(catalog_result.get("data"), dict) else {}
        resources, resources_valid = _normalize_plugin_resources(catalog_data.get("resources"))
        if not catalog_result.get("ok") or not resources_valid:
            resources = []
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {
                    "settings": settings_data,
                    "accounts": safe_accounts,
                    "account_catalog_available": not bool(account_warning),
                    "resources": resources,
                    "resource_catalog_available": bool(
                        catalog_result.get("ok")
                        and resources_valid
                        and catalog_data.get("resource_pool_available") is True
                    ),
                    "bridge_api": "1.0.0",
                },
                "message": "插件设置已连接。",
            },
        )

    @staticmethod
    def _service_v2_json_tree_is_safe(value: Any, *, depth: int = 0) -> bool:
        """Bound JSON crossing the Agent-to-browser wizard projection."""

        if depth > 16:
            return False
        if value is None or isinstance(value, bool):
            return True
        if isinstance(value, str):
            return len(value) <= 4096
        if isinstance(value, int):
            return not isinstance(value, bool)
        if isinstance(value, float):
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError):
                return False
            return True
        if isinstance(value, list):
            return len(value) <= 256 and all(
                AutomationPluginManagementServiceMixin._service_v2_json_tree_is_safe(
                    item,
                    depth=depth + 1,
                )
                for item in value
            )
        if isinstance(value, dict):
            return len(value) <= 256 and all(
                isinstance(key, str)
                and 0 < len(key) <= 128
                and key not in _SERVICE_V2_FORBIDDEN_BROWSER_AUTHORITY
                and AutomationPluginManagementServiceMixin._service_v2_json_tree_is_safe(
                    item,
                    depth=depth + 1,
                )
                for key, item in value.items()
            )
        return False

    @classmethod
    def _normalize_service_v2_inspection(
        cls,
        value: Any,
    ) -> dict[str, Any] | None:
        """Copy only the closed, display-safe inspection contract."""

        if not isinstance(value, dict) or set(value) != _SERVICE_V2_INSPECTION_FIELDS:
            return None
        plugin_id = str(value.get("plugin_id") or "").strip()
        name = normalize_feedback_text(redact_text(value.get("name") or "")).strip()
        version = str(value.get("version") or "").strip()
        host_api = value.get("host_api")
        if (
            not _SERVICE_V2_PLUGIN_ID_RE.fullmatch(plugin_id)
            or not name
            or len(name) > 160
            or not _SERVICE_V2_VERSION_RE.fullmatch(version)
            or not isinstance(host_api, dict)
            or set(host_api) != {"minimum", "maximum_exclusive"}
            or not all(
                _SERVICE_V2_VERSION_RE.fullmatch(str(host_api.get(key) or ""))
                for key in ("minimum", "maximum_exclusive")
            )
        ):
            return None

        account_roles: list[dict[str, Any]] = []
        raw_account_roles = value.get("account_roles")
        if not isinstance(raw_account_roles, list) or len(raw_account_roles) > 64:
            return None
        for raw in raw_account_roles:
            if not isinstance(raw, dict) or set(raw) != {
                "role",
                "allowed_systems",
                "required",
            }:
                return None
            role = str(raw.get("role") or "").strip()
            systems = raw.get("allowed_systems")
            if (
                not _SERVICE_V2_IDENTIFIER_RE.fullmatch(role)
                or not isinstance(systems, list)
                or not systems
                or len(systems) > 32
                or not all(
                    isinstance(item, str)
                    and _SERVICE_V2_IDENTIFIER_RE.fullmatch(item)
                    for item in systems
                )
                or len(systems) != len(set(systems))
                or not isinstance(raw.get("required"), bool)
            ):
                return None
            account_roles.append(
                {
                    "role": role,
                    "allowed_systems": list(systems),
                    "required": raw["required"],
                }
            )

        resource_roles: list[dict[str, Any]] = []
        raw_resource_roles = value.get("resource_roles")
        if not isinstance(raw_resource_roles, list) or len(raw_resource_roles) > 64:
            return None
        for raw in raw_resource_roles:
            if not isinstance(raw, dict) or set(raw) != {
                "role",
                "allowed_kinds",
                "required",
            }:
                return None
            role = str(raw.get("role") or "").strip()
            kinds = raw.get("allowed_kinds")
            if (
                not _SERVICE_V2_IDENTIFIER_RE.fullmatch(role)
                or not isinstance(kinds, list)
                or not kinds
                or len(kinds) > 32
                or not all(
                    isinstance(item, str)
                    and _SERVICE_V2_IDENTIFIER_RE.fullmatch(item)
                    for item in kinds
                )
                or len(kinds) != len(set(kinds))
                or not isinstance(raw.get("required"), bool)
            ):
                return None
            resource_roles.append(
                {
                    "role": role,
                    "allowed_kinds": list(kinds),
                    "required": raw["required"],
                }
            )

        permissions: list[dict[str, Any]] = []
        raw_permissions = value.get("permissions")
        if not isinstance(raw_permissions, list) or len(raw_permissions) > 128:
            return None
        account_role_names = {item["role"] for item in account_roles}
        resource_role_names = {item["role"] for item in resource_roles}
        for raw in raw_permissions:
            if not isinstance(raw, dict) or set(raw) != {
                "name",
                "operations",
                "account_role",
                "resource_role",
            }:
                return None
            capability = str(raw.get("name") or "").strip()
            operations = raw.get("operations")
            account_role = raw.get("account_role")
            resource_role = raw.get("resource_role")
            if (
                not _SERVICE_V2_IDENTIFIER_RE.fullmatch(capability)
                or not isinstance(operations, list)
                or not operations
                or len(operations) > 64
                or not all(
                    isinstance(item, str)
                    and _SERVICE_V2_IDENTIFIER_RE.fullmatch(item)
                    for item in operations
                )
                or len(operations) != len(set(operations))
                or (account_role is not None and account_role not in account_role_names)
                or (resource_role is not None and resource_role not in resource_role_names)
            ):
                return None
            permissions.append(
                {
                    "name": capability,
                    "operations": list(operations),
                    "account_role": account_role,
                    "resource_role": resource_role,
                }
            )

        contributions: list[dict[str, Any]] = []
        raw_contributions = value.get("contributions")
        allowed_kinds = {
            "console",
            "scheduler",
            "webhook",
            "feishu",
            "events",
            "module_slots",
            "harness",
        }
        if not isinstance(raw_contributions, list) or len(raw_contributions) > 128:
            return None
        seen_contributions: set[str] = set()
        for raw in raw_contributions:
            if not isinstance(raw, dict):
                return None
            contribution_id = str(raw.get("id") or "").strip()
            kind = str(raw.get("kind") or "").strip()
            title = normalize_feedback_text(redact_text(raw.get("title") or "")).strip()
            expected_fields = (
                {"id", "kind", "title", "description", "effect"}
                if kind == "harness"
                else {"id", "kind", "title", "default_enabled"}
            )
            if set(raw) != expected_fields or (
                contribution_id in seen_contributions
                or not _SERVICE_V2_IDENTIFIER_RE.fullmatch(contribution_id)
                or kind not in allowed_kinds
                or not title
                or len(title) > 160
                or (kind != "harness" and not isinstance(raw.get("default_enabled"), bool))
            ):
                return None
            seen_contributions.add(contribution_id)
            normalized_contribution = {
                    "id": contribution_id,
                    "kind": kind,
                    "title": title,
            }
            if kind == "harness":
                normalized_contribution.update(
                    {
                        "description": normalize_feedback_text(redact_text(raw.get("description") or "")),
                        "effect": str(raw.get("effect") or ""),
                    }
                )
            else:
                normalized_contribution["default_enabled"] = raw["default_enabled"]
            contributions.append(normalized_contribution)

        scheduling = value.get("scheduling")
        if (
            not isinstance(scheduling, dict)
            or set(scheduling) != {"supported", "default_schedule"}
            or not isinstance(scheduling.get("supported"), bool)
        ):
            return None
        default_schedule = scheduling.get("default_schedule")
        if not cls._normalize_service_v2_schedule(default_schedule):
            return None
        config_schema = value.get("config_schema")
        if (
            not isinstance(config_schema, dict)
            or not cls._service_v2_json_tree_is_safe(config_schema)
        ):
            return None
        try:
            schema_bytes = json.dumps(
                config_schema,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        if len(schema_bytes) > 128 * 1024:
            return None
        settings_ui = value.get("settings_ui")
        if settings_ui is not None and settings_ui != {
            "entry": "settings/index.html",
            "bridge_api": "1.0.0",
        }:
            return None
        return {
            "plugin_id": plugin_id,
            "name": name,
            "version": version,
            "host_api": {
                "minimum": str(host_api["minimum"]),
                "maximum_exclusive": str(host_api["maximum_exclusive"]),
            },
            "permissions": permissions,
            "account_roles": account_roles,
            "resource_roles": resource_roles,
            "config_schema": config_schema,
            "contributions": contributions,
            "scheduling": {
                "supported": scheduling["supported"],
                "default_schedule": dict(default_schedule),
            },
            "settings_ui": dict(settings_ui) if settings_ui is not None else None,
        }

    @staticmethod
    def _normalize_service_v2_schedule(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or set(value) != {"kind", "times", "enabled"}:
            return None
        kind = value.get("kind")
        times = value.get("times")
        enabled = value.get("enabled")
        if (
            kind not in {"none", "daily_times"}
            or not isinstance(times, list)
            or len(times) > 24
            or not all(isinstance(item, str) and _SERVICE_V2_TIME_RE.fullmatch(item) for item in times)
            or len(times) != len(set(times))
            or not isinstance(enabled, bool)
            or (kind == "none" and (times or enabled))
            or (kind == "daily_times" and (not times or not enabled))
        ):
            return None
        return {"kind": kind, "times": list(times), "enabled": enabled}

    @classmethod
    def _normalize_service_v2_install_intent(cls, raw: Any) -> str | None:
        """Canonicalize the browser intent without inventing business defaults."""

        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _SERVICE_V2_INTENT_MAX_BYTES:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or set(value) != _SERVICE_V2_INSTALL_INTENT_FIELDS
            or value.get("permissions_confirmed") is not True
        ):
            return None
        instance_name = normalize_feedback_text(str(value.get("instance_name") or "")).strip()
        if (
            not instance_name
            or len(instance_name) > 120
        ):
            return None
        normalized = {
            "instance_name": instance_name,
            "permissions_confirmed": True,
        }
        try:
            canonical = json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return None
        return canonical if len(canonical.encode("utf-8")) <= _SERVICE_V2_INTENT_MAX_BYTES else None

    def _agent_plugin_multipart_request(
        self,
        endpoint: str,
        *,
        package_path: Path,
        fields: dict[str, str],
        console_principal: dict[str, Any],
        timeout: int = 90,
    ) -> dict[str, Any]:
        """Forward one bounded ZIP as signed multipart without trusting browser metadata."""

        try:
            endpoint = self._validate_internal_agent_endpoint(endpoint)
        except ValueError as exc:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "INVALID_AGENT_ENDPOINT",
                "error": str(exc),
            }
        token = str(getattr(self.settings, "agent_internal_api_token", "") or "").strip()
        if not token:
            return {
                "ok": False,
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error_code": "AGENT_INTERNAL_TOKEN_NOT_CONFIGURED",
                "error": "智能服务内部接口未配置。",
            }
        try:
            package_size = package_path.stat().st_size
            if package_size <= 0 or package_size > self._automation_plugin_max_package_bytes:
                raise ValueError("plugin package size is outside the accepted boundary")
            package_bytes = package_path.read_bytes()
        except (OSError, ValueError) as exc:
            LOGGER.warning("Rejected staged plugin package: %s", type(exc).__name__)
            return {
                "ok": False,
                "status": HTTPStatus.BAD_REQUEST,
                "error_code": "INVALID_PLUGIN_PACKAGE_SIZE",
                "error": "插件包大小无效或暂时无法读取。",
            }
        signed_fields = dict(fields)
        signed_fields["package_sha256"] = hashlib.sha256(package_bytes).hexdigest()
        boundary = f"----ConsoleAutomationPlugin{secrets.token_hex(18)}"
        parts: list[bytes] = []
        for name, value in signed_fields.items():
            safe_name = str(name).replace('"', "")
            safe_value = str(value).replace("\r", " ").replace("\n", " ")
            parts.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    (
                        f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
                    ).encode("ascii"),
                    safe_value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        parts.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                b'Content-Disposition: form-data; name="package"; filename="automation-plugin.zip"\r\n',
                b"Content-Type: application/zip\r\n\r\n",
                package_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        body = b"".join(parts)
        url = f"{self.settings.agent_base_url.rstrip('/')}{endpoint}"
        headers = {
            "X-Agent-Internal-Token": token,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        signing_secret = str(os.getenv("CONSOLE_AGENT_SIGNING_SECRET", "") or "").strip()
        if not signing_secret:
            return {
                "ok": False,
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error_code": "CONSOLE_SIGNING_SECRET_NOT_CONFIGURED",
                "error": "后台到智能服务的签名未配置。",
            }
        try:
            headers.update(
                build_console_identity_headers(
                    secret=signing_secret,
                    method="POST",
                    request_target=endpoint,
                    body=body,
                    principal=console_principal,
                    nonce=secrets.token_urlsafe(24),
                )
            )
        except ConsoleIdentityError as exc:
            return {
                "ok": False,
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error_code": exc.code,
                "error": str(exc),
            }
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict) or not {"ok", "data", "error"}.issubset(payload):
                    raise ValueError("智能服务返回了无法识别的数据")
                if payload.get("ok") is not True:
                    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                    return {
                        "ok": False,
                        "status": response.status,
                        "error_code": str(error.get("code") or "PLUGIN_PACKAGE_REJECTED"),
                        "error": redact_text(error.get("message") or "插件包被拒绝。"),
                        "data": payload.get("data"),
                    }
                return {"ok": True, "status": response.status, "data": payload.get("data")}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
            return {
                "ok": False,
                "status": exc.code,
                "error_code": str(error.get("code") or "PLUGIN_PACKAGE_REQUEST_FAILED"),
                "error": redact_text(error.get("message") or "插件包上传失败。"),
                "data": payload.get("data") if isinstance(payload, dict) else None,
            }
        except (URLError, ValueError) as exc:
            return {
                "ok": False,
                "status": HTTPStatus.BAD_GATEWAY,
                "error_code": "PLUGIN_PACKAGE_REQUEST_FAILED",
                "error": redact_text(str(exc)),
            }

    def _handle_automation_plugin_package_upload(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        automation_id: str = "",
        inspect_only: bool = False,
    ) -> None:
        requested_automation_id = str(automation_id or "").strip()
        automation_id = self._automation_project_id(requested_automation_id)
        if inspect_only and requested_automation_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_INSPECTION_SCOPE_INVALID",
                "安装前检查不能指定已有实例。",
            )
            return
        if requested_automation_id and not automation_id:
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
                "只有超级管理员可以安装或升级自动化。",
            )
            return
        try:
            content_length = int(handler.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            content_length = -1
        if content_length <= 0 or content_length > self._automation_plugin_max_package_bytes + 512 * 1024:
            self._control_plane_error(
                handler,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "PLUGIN_PACKAGE_TOO_LARGE",
                "扩展压缩包不能超过 32 兆字节。",
            )
            return
        form = self._parse_multipart_form(handler)
        if inspect_only:
            allowed_form_fields = {"package", "request_id"}
        elif automation_id:
            allowed_form_fields = {"package", "request_id", "expected_record_version"}
        elif "intent" in form:
            allowed_form_fields = {"package", "request_id", "intent"}
        else:
            # Keep the established Action-v1 upload contract unchanged.
            allowed_form_fields = {"package", "request_id", "instance_name"}
        unexpected_form_fields = set(form.keys()) - allowed_form_fields
        duplicate_form_fields = [
            field for field in form.keys() if isinstance(form[field], list)
        ]
        if unexpected_form_fields or duplicate_form_fields:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "UNSUPPORTED_PLUGIN_PACKAGE_FIELDS",
                "插件包请求包含不支持的字段。",
            )
            return
        package_item = form["package"] if "package" in form else None
        filename = str(getattr(package_item, "filename", "") or "")
        if package_item is None or not filename or Path(filename).suffix.lower() != ".zip":
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_ZIP_REQUIRED",
                "请选择一个扩展压缩包。",
            )
            return
        request_id = self._normalize_browser_request_uuid(
            form.getvalue("request_id") or handler.headers.get("X-Browser-Request-UUID")
        )
        if not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "BROWSER_REQUEST_UUID_REQUIRED",
                "缺少有效且稳定的请求标识，插件包未提交。",
            )
            return
        package_bytes = package_item.file.read(self._automation_plugin_max_package_bytes + 1)
        if (
            not package_bytes
            or len(package_bytes) > self._automation_plugin_max_package_bytes
            or not zipfile.is_zipfile(io.BytesIO(package_bytes))
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_ZIP",
                "扩展压缩包为空、超过 32 兆字节或格式无效。",
            )
            return

        fields = {"request_id": request_id}
        if inspect_only:
            endpoint = "/internal/v1/automation/plugins/inspect-upload"
        elif automation_id:
            expected_record_version_raw = str(form.getvalue("expected_record_version") or "").strip()
            try:
                expected_record_version = int(expected_record_version_raw)
            except ValueError:
                expected_record_version = 0
            if expected_record_version < 1:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "EXPECTED_RECORD_VERSION_REQUIRED",
                    "实例版本快照已缺失，请刷新后重试。",
                )
                return
            fields["expected_record_version"] = str(expected_record_version)
            endpoint = (
                f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/upgrade"
            )
        elif "intent" in form:
            canonical_intent = self._normalize_service_v2_install_intent(
                form.getvalue("intent")
            )
            if canonical_intent is None:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "PLUGIN_INSTALL_INTENT_INVALID",
                    "安装信息不完整或已被修改，请重新检查插件包。",
                )
                return
            fields["intent"] = canonical_intent
            endpoint = "/internal/v1/automation/plugins/install-v2"
        else:
            instance_name = normalize_feedback_text(str(form.getvalue("instance_name") or "")).strip()
            if not instance_name or len(instance_name) > 120:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "INSTANCE_NAME_REQUIRED",
                    "请填写 1 至 120 个字符的实例名称。",
                )
                return
            fields["instance_name"] = instance_name
            endpoint = "/internal/v1/automation/plugins/install"

        upload_root = (self.settings.runtime_dir / "automation_plugin_uploads").resolve()
        upload_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(upload_root, 0o700)
        except OSError:
            pass
        target = (upload_root / f"{request_id}-{secrets.token_hex(8)}.zip").resolve()
        try:
            target.relative_to(upload_root)
        except ValueError:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "INVALID_PLUGIN_UPLOAD_PATH",
                "插件上传路径无效。",
            )
            return
        try:
            target.write_bytes(package_bytes)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            result = self._agent_plugin_multipart_request(
                endpoint,
                package_path=target,
                fields=fields,
                console_principal=trusted_context["_console_principal"],
            )
        except OSError as exc:
            LOGGER.warning("Failed to stage automation plugin package: %s", type(exc).__name__)
            self._control_plane_error(
                handler,
                HTTPStatus.SERVICE_UNAVAILABLE,
                "PLUGIN_UPLOAD_STAGING_FAILED",
                "插件包暂存失败，请稍后重试。",
            )
            return
        finally:
            target.unlink(missing_ok=True)
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_PACKAGE_REQUEST_FAILED",
                fallback_message="自动化插件包处理失败。",
            )
            return
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if inspect_only:
            inspection = self._normalize_service_v2_inspection(data)
            if inspection is None:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_GATEWAY,
                    "INVALID_SERVICE_V2_INSPECTION_CONTRACT",
                    "智能服务返回了无效的扩展检查结果。",
                )
                return
            self._send_json(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "data": {
                        **inspection,
                        "account_options": [],
                        "resource_options": [],
                        "account_pool_available": True,
                        "resource_pool_available": True,
                        "warnings": [],
                    },
                    "message": "插件包已检查，请确认实例名称和权限。",
                },
            )
            return
        created_id = self._automation_project_id(data.get("automation_id"))
        if automation_id:
            if created_id and created_id != automation_id:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_GATEWAY,
                    "INVALID_PLUGIN_INSTANCE_RESPONSE",
                    "智能服务返回了不匹配的扩展实例。",
                )
                return
            created_id = automation_id
        elif not created_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "INVALID_PLUGIN_INSTANCE_RESPONSE",
                "智能服务未返回有效的扩展实例。",
            )
            return
        self._clear_automation_plugin_catalog_cache()
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {"automation_id": created_id},
                "message": (
                    "自动化已升级。"
                    if automation_id
                    else "自动化已安装并保持停用，请完成插件设置后再启用。"
                ),
            },
        )
    def _handle_automation_plugin_instance_action(
        self,
        handler: BaseHTTPRequestHandler,
        automation_id: str,
        action: str,
    ) -> None:
        automation_id = self._automation_project_id(automation_id)
        if not automation_id or action not in {"enable", "disable", "uninstall"}:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "AUTOMATION_PLUGIN_ACTION_NOT_FOUND",
                "插件实例操作不存在。",
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
                "只有超级管理员可以管理插件实例。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        expected_record_version = values.get("expected_record_version")
        if (
            not request_id
            or isinstance(expected_record_version, bool)
            or not isinstance(expected_record_version, int)
            or expected_record_version < 1
        ):
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_ACTION_VERSION_REQUIRED",
                "缺少请求标识或实例版本快照，请刷新后重试。",
            )
            return
        if action in {"enable", "disable"}:
            if set(values) - {"request_id", "expected_record_version"}:
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "UNSUPPORTED_PLUGIN_ACTION_FIELDS",
                    "实例状态请求包含不支持的字段。",
                )
                return
            endpoint = f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/state"
            payload = {
                "enabled": action == "enable",
                "request_id": request_id,
                "expected_record_version": expected_record_version,
            }
        else:
            current_version = str(values.get("current_version") or "").strip()
            if (
                set(values)
                - {"request_id", "expected_record_version", "current_version", "confirm"}
                or not self._automation_plugin_version_re.fullmatch(current_version)
                or values.get("confirm") is not True
            ):
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "PLUGIN_UNINSTALL_CONFIRMATION_REQUIRED",
                    "卸载必须确认当前实例版本与不可撤销范围。",
                )
                return
            endpoint = f"/internal/v1/automation/instances/{quote(automation_id, safe='')}/uninstall"
            payload = {
                "request_id": request_id,
                "expected_record_version": expected_record_version,
                "current_version": current_version,
                "confirm": True,
            }
        result = self._agent_request(
            "POST",
            endpoint,
            payload=payload,
            timeout=45 if action == "uninstall" else 20,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_INSTANCE_ACTION_FAILED",
                fallback_message="插件实例操作失败。",
            )
            return
        self._clear_automation_plugin_catalog_cache()
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {"automation_id": automation_id},
                "message": {
                    "enable": "自动化实例已启用。",
                    "disable": "自动化实例已停用。",
                    "uninstall": "自动化实例已卸载。",
                }[action],
            },
        )
    def _handle_automation_plugin_migration_action(
        self,
        handler: BaseHTTPRequestHandler,
        migration_pair_id: str,
        action: str,
    ) -> None:
        if action not in {"create", "ready", "cutover", "rollback", "complete"}:
            self._control_plane_error(
                handler,
                HTTPStatus.NOT_FOUND,
                "PLUGIN_MIGRATION_ACTION_NOT_FOUND",
                "迁移操作不存在。",
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
                "只有超级管理员可以管理插件迁移。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        if not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_MIGRATION_REQUEST_INVALID",
                "缺少安全请求标识，请刷新后重试。",
            )
            return

        if action == "create":
            source_id = self._automation_project_id(values.get("source_automation_id"))
            target_id = self._automation_project_id(values.get("target_automation_id"))
            raw_fields = values.get("business_key_fields")
            namespace = values.get("business_key_namespace")
            fields = (
                [str(field or "").strip() for field in raw_fields]
                if isinstance(raw_fields, list)
                else []
            )
            namespace = str(namespace or "").strip() or None
            if (
                set(values)
                != {
                    "source_automation_id",
                    "target_automation_id",
                    "business_key_fields",
                    "business_key_namespace",
                    "request_id",
                }
                or not source_id
                or not target_id
                or source_id == target_id
                or not 1 <= len(fields) <= 8
                or len(fields) != len(set(fields))
                or any(not self._valid_migration_business_key_field(field) for field in fields)
                or (namespace is not None and len(namespace) > 96)
            ):
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "PLUGIN_MIGRATION_CREATE_INVALID",
                    "请选择一个旧项目、一个 v2 项目，并明确防重复业务字段。",
                )
                return
            endpoint = "/internal/v1/automation/migrations"
            payload = {
                "migration_pair_id": str(uuid.uuid4()),
                "source_automation_id": source_id,
                "target_automation_id": target_id,
                "business_key_fields": fields,
                "business_key_namespace": namespace,
                "request_id": request_id,
                "reason": "超级管理员在自动化页面创建并行迁移验证",
            }
            automation_id = target_id
        else:
            pair_id = str(migration_pair_id or "").strip()
            expected_record_version = values.get("expected_record_version")
            if (
                not self._automation_plugin_migration_pair_id_re.fullmatch(pair_id)
                or set(values) != {"request_id", "expected_record_version", "confirm"}
                or isinstance(expected_record_version, bool)
                or not isinstance(expected_record_version, int)
                or expected_record_version < 1
                or values.get("confirm") is not True
            ):
                self._control_plane_error(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    "PLUGIN_MIGRATION_OPERATION_INVALID",
                    "迁移状态快照已失效，请刷新后重试。",
                )
                return
            endpoint = (
                f"/internal/v1/automation/migrations/{quote(pair_id, safe='')}/{action}"
            )
            payload = {
                "expected_record_version": expected_record_version,
                "request_id": request_id,
                "reason": {
                    "ready": "超级管理员确认 v2 真跑证据并标记迁移就绪",
                    "cutover": "超级管理员接管自动执行入口",
                    "rollback": "超级管理员将后续入口回滚到旧项目",
                    "complete": "超级管理员确认完成迁移",
                }[action],
                "confirm": True,
            }
            automation_id = ""

        result = self._agent_request(
            "POST",
            endpoint,
            payload=payload,
            timeout=30,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_MIGRATION_ACTION_FAILED",
                fallback_message="插件迁移操作失败。",
            )
            return
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": result.get("data") if isinstance(result.get("data"), dict) else {},
                "message": {
                    "create": "迁移对已创建，v2 项目只开放手动验证入口。",
                    "ready": "真实执行证据已通过，迁移可以接管自动执行。",
                    "cutover": "自动执行入口已原子切换到 v2 项目。",
                    "rollback": "后续自动执行入口已恢复到旧项目。",
                    "complete": "迁移已完成，旧项目现在可以单独卸载。",
                }[action],
            },
        )

    def _handle_automation_plugin_unknown_write_recovery(
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
                "只有超级管理员可以恢复未知写入项目。",
            )
            return
        values = self._read_control_plane_json(handler)
        if values is None:
            return
        request_id = self._normalize_browser_request_uuid(values.get("request_id"))
        if set(values) != {"request_id"} or not request_id:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_REQUEST,
                "PLUGIN_RECOVERY_REQUEST_INVALID",
                "未知写入恢复请求无效。",
            )
            return
        result = self._agent_request(
            "POST",
            (
                f"/internal/v1/automation/instances/{quote(automation_id, safe='')}"
                "/generation/recover-current-unknown-write"
            ),
            payload={"request_id": request_id},
            timeout=30,
            console_principal=trusted_context["_console_principal"],
        )
        if not result.get("ok"):
            self._automation_project_agent_error(
                handler,
                result,
                automation_id=automation_id,
                fallback_code="PLUGIN_UNKNOWN_WRITE_RECOVERY_FAILED",
                fallback_message="未知写入恢复失败。",
            )
            return
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        recovery_status = str(data.get("recovery_status") or "").strip().upper()
        if recovery_status == "UNKNOWN":
            self._control_plane_error(
                handler,
                HTTPStatus.CONFLICT,
                "PLUGIN_RECOVERY_EVIDENCE_UNRESOLVED",
                (
                    "系统仍无法确认上次是否已经保存。任务会继续暂停，也没有重复执行。"
                    "请先到对应业务表格核对实际结果。"
                ),
            )
            return
        if recovery_status not in {"APPLIED", "NOT_APPLIED"}:
            self._control_plane_error(
                handler,
                HTTPStatus.BAD_GATEWAY,
                "PLUGIN_RECOVERY_RESPONSE_INVALID",
                "智能服务返回了无法识别的恢复结果。",
            )
            return
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {
                    "automation_id": automation_id,
                    "recovery_status": recovery_status,
                    "transitioned": bool(data.get("transitioned")),
                },
                "message": (
                    "已确认上次保存成功，任务已恢复。"
                    if recovery_status == "APPLIED"
                    else "已确认上次没有保存，任务已进入安全重试状态。"
                ),
            },
        )
