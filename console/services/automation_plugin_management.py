"""Console-side ZIP plugin upload and lifecycle management helpers."""

import uuid

from console.app_support import *  # noqa: F403
from shared.service_identity import (
    ConsoleIdentityError,
    build_console_identity_headers,
)


class AutomationPluginManagementServiceMixin:
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
                "error": "Agent 内部接口未配置。",
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
                "error": "Console-to-Agent 签名未配置。",
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
                    raise ValueError("Agent returned an invalid internal API contract")
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
    ) -> None:
        requested_automation_id = str(automation_id or "").strip()
        automation_id = self._automation_project_id(requested_automation_id)
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
                "插件 ZIP 不能超过 32MB。",
            )
            return
        form = self._parse_multipart_form(handler)
        allowed_form_fields = (
            {"package", "request_id", "expected_record_version"}
            if automation_id
            else {"package", "request_id", "instance_name"}
        )
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
                "请选择一个 ZIP 插件包。",
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
                "插件包为空、超过 32MB 或不是有效 ZIP。",
            )
            return

        fields = {"request_id": request_id}
        if automation_id:
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
        created_id = self._automation_project_id(data.get("automation_id"))
        self._send_json(
            handler,
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {"automation_id": created_id} if created_id else {},
                "message": (
                    "自动化已升级。"
                    if automation_id
                    else "自动化已安装，系统将按依赖、配置和账号状态准备项目。"
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
                "Agent 返回了无法识别的恢复结果。",
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
