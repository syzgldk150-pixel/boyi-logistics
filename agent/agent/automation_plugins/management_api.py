"""Closed internal HTTP API for signed plugin and project management."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from email import policy
from email.parser import BytesParser
from pathlib import PurePath
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from agent.api_contracts import EnvelopedRoute
from agent.automation_plugins.errors import (
    AutomationPluginError,
    PluginConflictError,
    PluginNotFoundError,
    PluginPackageError,
    PluginSignatureError,
    PluginUninstallBlocked,
)
from agent.automation_plugins.management import (
    AutomationPluginManagementService,
    MigrationPreparationPersistedError,
)
from agent.orchestration.models import Actor
from shared.contracts import api_failure, api_success
from shared.orchestration_repository_support import OrchestrationPersistenceError
from shared.redaction import redact_text


_MAX_HTTP_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_MULTIPART_OVERHEAD_BYTES = 512 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BOUNDARY_RE = re.compile(r"^[0-9A-Za-z'()+_,./:=?-]{1,70}$")


class PluginStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    request_id: str = Field(min_length=1, max_length=64)
    expected_record_version: int = Field(ge=1)


class PluginUninstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=64)
    expected_record_version: int = Field(ge=1)
    current_version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=64)
    confirm: Literal[True]


class PluginDataPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    confirm: Literal[True]


class PluginMigrationCreateRequest(BaseModel):
    """Closed DTO; the server derives all project/configuration snapshots."""

    model_config = ConfigDict(extra="forbid", strict=True)

    migration_pair_id: str = Field(min_length=36, max_length=36)
    source_automation_id: str = Field(min_length=1, max_length=128)
    target_automation_id: str = Field(min_length=1, max_length=128)
    business_key_fields: list[str] = Field(min_length=1, max_length=8)
    business_key_namespace: str | None = Field(default=None, min_length=1, max_length=96)
    request_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)


class PluginMigrationOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_record_version: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    confirm: Literal[True]


class PluginScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["none", "daily_times", "startup"]
    times: list[str] = Field(max_length=96)
    enabled: bool


class PluginConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    config: dict[str, Any]
    account_bindings: dict[str, Any]
    resource_bindings: dict[str, Any]
    enabled_entrypoints: list[str] = Field(max_length=64)
    device_id: str | None = Field(default=None, max_length=128)
    schedule: PluginScheduleRequest
    request_id: str = Field(min_length=1, max_length=64)
    expected_project_configuration_version: int = Field(ge=1)


class ArrivalStatsRecoveryReadbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    arrival_stat_runs: int = Field(ge=0)
    arrival_stat_items: int = Field(ge=0)
    feishu_rows_created: int = Field(ge=0)


class ArrivalStatsRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    generation: int = Field(ge=1)
    lease_id: str = Field(min_length=1, max_length=64)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    readback: ArrivalStatsRecoveryReadbackRequest
    request_id: str = Field(min_length=1, max_length=64)


class UnknownWriteRecoveryRequest(BaseModel):
    """No actor-supplied readback/evidence can influence recovery."""

    model_config = ConfigDict(extra="forbid", strict=True)

    generation: int = Field(ge=1)
    lease_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)


class CurrentUnknownWriteRecoveryRequest(BaseModel):
    """The server resolves generation and lease identity from current state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=64)


class WorkerIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    device_key_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    ed25519_public_key_base64: str = Field(min_length=44, max_length=44)
    tls_client_certificate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkerCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    interactive: bool


class WorkerPairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    device_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    platform: Literal["windows"]
    agent_version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=64)
    identity_json: WorkerIdentityRequest
    capabilities_json: WorkerCapabilitiesRequest
    request_id: str = Field(min_length=1, max_length=64)


def _plugin_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, PluginNotFoundError):
        status_code = 404
    elif isinstance(exc, PluginConflictError) and exc.code == "PLUGIN_MANAGEMENT_FORBIDDEN":
        status_code = 403
    elif isinstance(exc, (PluginSignatureError, PluginPackageError, ValueError)):
        status_code = 422
    elif isinstance(exc, (PluginConflictError, PluginUninstallBlocked)):
        status_code = 409
    elif isinstance(exc, OrchestrationPersistenceError):
        status_code = 409
    else:  # pragma: no cover - caller deliberately re-raises unknown failures
        raise exc
    code = getattr(exc, "code", "PLUGIN_REQUEST_INVALID")
    return JSONResponse(
        status_code=status_code,
        content=api_failure(str(code), redact_text(exc)[:500]),
    )


async def _service_response(call: Callable[[], Any]) -> dict[str, Any] | JSONResponse:
    try:
        return api_success(await run_in_threadpool(call))
    except (
        AutomationPluginError,
        OrchestrationPersistenceError,
        ValueError,
    ) as exc:
        return _plugin_error_response(exc)


def _entrypoint_kind_mapping(value: Any) -> dict[str, str]:
    """Extract only the closed contribution-kind map used by runtime status."""

    if not isinstance(value, Mapping):
        return {}
    kinds: dict[str, str] = {}
    for raw_id, raw_kind in value.items():
        contribution_id = str(raw_id or "").strip()
        if not contribution_id:
            continue
        if isinstance(raw_kind, Mapping):
            raw_kind = raw_kind.get("contribution_kind")
        contribution_kind = str(raw_kind or "").strip().lower()
        if contribution_kind in {"console", "scheduler", "webhook", "feishu", "events"}:
            kinds[contribution_id] = contribution_kind
    return kinds


def _catalog_entrypoint_metadata(
    service: Any,
    *,
    actor: Actor,
    automation_id: str,
) -> tuple[str, dict[str, str]]:
    """Read the exact instance contribution map when save output omits it."""

    provider = getattr(service, "catalog_projection", None)
    if not callable(provider):
        return "", {}
    try:
        projection = provider(actor=actor)
    except Exception:  # noqa: BLE001 - runtime status must fail closed
        return "", {}
    if not isinstance(projection, Mapping):
        return "", {}
    instances = projection.get("instances")
    if not isinstance(instances, list):
        return "", {}
    for item in instances:
        if not isinstance(item, Mapping) or str(item.get("automation_id") or "") != automation_id:
            continue
        runtime_model = str(item.get("runtime_model") or "").strip().upper()
        kinds = _entrypoint_kind_mapping(item.get("entrypoint_kinds"))
        if not kinds:
            kinds = _entrypoint_kind_mapping(item.get("invocation_contracts"))
        return runtime_model, kinds
    return "", {}


def _scheduler_contribution_enabled(
    data: Mapping[str, Any],
    *,
    service: Any,
    actor: Actor,
    automation_id: str,
) -> bool:
    """Check the contribution kind, never the v2 contribution ID spelling."""

    enabled = data.get("enabled_entrypoints")
    if not isinstance(enabled, list):
        return False
    enabled_ids = {str(item or "").strip() for item in enabled if str(item or "").strip()}
    if not enabled_ids:
        return False

    runtime_model = str(data.get("runtime_model") or "").strip().upper()
    kinds = _entrypoint_kind_mapping(data.get("entrypoint_kinds"))
    if not kinds:
        kinds = _entrypoint_kind_mapping(data.get("invocation_contracts"))
    if not kinds:
        catalog_runtime_model, catalog_kinds = _catalog_entrypoint_metadata(
            service,
            actor=actor,
            automation_id=automation_id,
        )
        runtime_model = runtime_model or catalog_runtime_model
        kinds = catalog_kinds

    # Unknown runtime models are never schedulable, even if a future response
    # happens to contain a scheduler-looking contribution map.
    if runtime_model not in {"", "ACTION_V1", "SERVICE_V2"}:
        return False
    if runtime_model == "ACTION_V1":
        return "scheduler" in enabled_ids
    # The contribution map is authoritative for v2.  A v2 package without its
    # map is not schedulable.
    if kinds:
        return any(kinds.get(contribution_id) == "scheduler" for contribution_id in enabled_ids)
    if runtime_model == "SERVICE_V2":
        return False
    if runtime_model == "":
        return "scheduler" in enabled_ids
    return False


def _refresh_after_committed_operation(
    response: dict[str, Any] | JSONResponse,
    *,
    scheduler_refresh_provider: Callable[[], Mapping[str, Any]] | None,
    committed_field: str,
    refresh_failure_message: str,
) -> dict[str, Any] | JSONResponse:
    """Refresh the scheduler after a durable control-plane mutation.

    A failed refresh must not turn an already-committed mutation into an
    apparent operation failure.  The response instead records the committed
    mutation and tells the caller to retry refresh/status, not the mutation.
    """

    if isinstance(response, JSONResponse) or response.get("ok") is not True:
        return response
    data = response.get("data")
    if not isinstance(data, dict):
        return response
    refresh_completed = False
    if scheduler_refresh_provider is not None:
        try:
            refreshed = scheduler_refresh_provider()
            refresh_completed = bool(
                isinstance(refreshed, Mapping) and refreshed.get("initialized") is True
            )
        except Exception:  # noqa: BLE001 - committed mutation, report refresh separately
            refresh_completed = False
    data.update(
        {
            committed_field: True,
            "scheduler_refresh_completed": refresh_completed,
            "schedule_runtime_state": (
                "REFRESHED" if refresh_completed else "REFRESH_FAILED"
            ),
        }
    )
    if not refresh_completed:
        response["message"] = refresh_failure_message
    return response


def _refresh_after_committed_migration(
    response: dict[str, Any] | JSONResponse,
    *,
    scheduler_refresh_provider: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, Any] | JSONResponse:
    """Keep migration response wording while using the common refresh helper."""

    return _refresh_after_committed_operation(
        response,
        scheduler_refresh_provider=scheduler_refresh_provider,
        committed_field="migration_operation_committed",
        refresh_failure_message=(
            "迁移操作已提交，但调度器刷新失败；请刷新状态或重试调度刷新，"
            "不要重复提交迁移切换。"
        ),
    )


def _multipart_boundary(content_type: str) -> str:
    try:
        header = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii")
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise PluginPackageError(
            "plugin upload Content-Type is invalid",
            code="PLUGIN_MULTIPART_INVALID",
        ) from exc
    if header.get_content_type().lower() != "multipart/form-data":
        raise PluginPackageError(
            "plugin upload must use multipart/form-data",
            code="PLUGIN_MULTIPART_INVALID",
        )
    boundary = str(header.get_boundary() or "")
    if not _BOUNDARY_RE.fullmatch(boundary):
        raise PluginPackageError(
            "plugin upload multipart boundary is invalid",
            code="PLUGIN_MULTIPART_INVALID",
        )
    return boundary


async def _parse_plugin_upload(
    request: Request,
    *,
    expected_text_fields: frozenset[str],
) -> tuple[bytes, dict[str, str]]:
    content_type = str(request.headers.get("content-type") or "")
    _multipart_boundary(content_type)
    raw_length = str(request.headers.get("content-length") or "").strip()
    if raw_length:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise PluginPackageError(
                "plugin upload Content-Length is invalid",
                code="PLUGIN_MULTIPART_INVALID",
            ) from exc
        if not 0 < declared_length <= _MAX_HTTP_ARCHIVE_BYTES + _MAX_MULTIPART_OVERHEAD_BYTES:
            raise PluginPackageError(
                "plugin upload exceeds the HTTP package limit",
                code="PLUGIN_PACKAGE_TOO_LARGE",
            )
    body = await request.body()
    if not body or len(body) > _MAX_HTTP_ARCHIVE_BYTES + _MAX_MULTIPART_OVERHEAD_BYTES:
        raise PluginPackageError(
            "plugin upload body is empty or too large",
            code="PLUGIN_PACKAGE_TOO_LARGE",
        )
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise PluginPackageError(
            "plugin multipart body is invalid",
            code="PLUGIN_MULTIPART_INVALID",
        ) from exc
    if not message.is_multipart():
        raise PluginPackageError(
            "plugin multipart body has no parts",
            code="PLUGIN_MULTIPART_INVALID",
        )
    parts: list[tuple[str, str | None, bytes]] = []
    for part in message.iter_parts():
        if part.is_multipart() or part.get_content_disposition() != "form-data":
            raise PluginPackageError(
                "plugin multipart contains an unsupported part",
                code="PLUGIN_MULTIPART_INVALID",
            )
        if str(part.get("Content-Transfer-Encoding") or "").lower() not in {
            "",
            "7bit",
            "8bit",
            "binary",
        }:
            raise PluginPackageError(
                "plugin multipart transfer encoding is unsupported",
                code="PLUGIN_MULTIPART_INVALID",
            )
        name = str(part.get_param("name", header="content-disposition") or "")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if not name or not isinstance(payload, bytes):
            raise PluginPackageError(
                "plugin multipart part is invalid",
                code="PLUGIN_MULTIPART_INVALID",
            )
        parts.append((name, filename, payload))
    expected_names = {"package", *expected_text_fields}
    counts = Counter(name for name, _filename, _payload in parts)
    if set(counts) != expected_names or any(count != 1 for count in counts.values()):
        raise PluginPackageError(
            "plugin upload fields are incomplete, duplicated or unsupported",
            code="PLUGIN_MULTIPART_FIELDS_INVALID",
        )
    package_name, package_filename, package_bytes = next(
        item for item in parts if item[0] == "package"
    )
    del package_name
    if (
        not package_filename
        or PurePath(package_filename).suffix.lower() != ".zip"
        or not package_bytes
        or len(package_bytes) > _MAX_HTTP_ARCHIVE_BYTES
    ):
        raise PluginPackageError(
            "plugin package must be one non-empty ZIP within the upload limit",
            code="PLUGIN_ZIP_REQUIRED",
        )
    fields: dict[str, str] = {}
    for name, filename, payload in parts:
        if name == "package":
            continue
        if filename is not None or len(payload) > 1024 or b"\x00" in payload:
            raise PluginPackageError(
                "plugin upload text field is invalid",
                code="PLUGIN_MULTIPART_FIELDS_INVALID",
            )
        try:
            fields[name] = payload.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise PluginPackageError(
                "plugin upload text fields must be UTF-8",
                code="PLUGIN_MULTIPART_FIELDS_INVALID",
            ) from exc
    digest = fields.get("package_sha256", "").lower()
    if not _SHA256_RE.fullmatch(digest):
        raise PluginPackageError(
            "plugin transport digest is invalid",
            code="PLUGIN_PACKAGE_DIGEST_INVALID",
        )
    fields["package_sha256"] = digest
    return package_bytes, fields


def create_automation_plugin_management_router(
    *,
    service_provider: Callable[[], AutomationPluginManagementService],
    actor_provider: Callable[[Request], Actor],
    include_worker_routes: bool = True,
    scheduler_refresh_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> APIRouter:
    """Build the plugin API without importing the Agent composition root."""

    router = APIRouter(route_class=EnvelopedRoute)

    @router.get("/internal/v1/automation/plugins/catalog", response_model=None)
    async def plugin_catalog(request: Request) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().catalog_projection(actor=actor)
        )

    if include_worker_routes:

        @router.get("/internal/v1/automation/workers", response_model=None)
        async def plugin_workers(request: Request) -> dict[str, Any] | JSONResponse:
            actor = actor_provider(request)
            return await _service_response(
                lambda: service_provider().worker_projection(actor=actor)
            )

        @router.post("/internal/v1/automation/workers/pair", response_model=None)
        async def pair_plugin_worker(
            payload: WorkerPairRequest,
            request: Request,
        ) -> dict[str, Any] | JSONResponse:
            actor = actor_provider(request)
            return await _service_response(
                lambda: service_provider().pair_worker(
                    device_id=payload.device_id,
                    display_name=payload.display_name,
                    platform=payload.platform,
                    agent_version=payload.agent_version,
                    identity=payload.identity_json.model_dump(),
                    capabilities=payload.capabilities_json.model_dump(),
                    request_id=payload.request_id,
                    actor=actor,
                )
            )

    @router.post("/internal/v1/automation/plugins/install", response_model=None)
    async def install_plugin(request: Request) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        try:
            package, fields = await _parse_plugin_upload(
                request,
                expected_text_fields=frozenset(
                    {"instance_name", "request_id", "package_sha256"}
                ),
            )
        except PluginPackageError as exc:
            return _plugin_error_response(exc)
        response = await _service_response(
            lambda: service_provider().install(
                package,
                instance_name=fields["instance_name"],
                request_id=fields["request_id"],
                transport_package_sha256=fields["package_sha256"],
                actor=actor,
            )
        )
        return _refresh_after_committed_operation(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
            committed_field="plugin_operation_committed",
            refresh_failure_message=(
                "插件安装操作已提交，但调度器刷新失败；请刷新状态或重试调度刷新，"
                "不要重复提交安装操作。"
            ),
        )

    @router.post(
        "/internal/v1/automation/instances/{automation_id}/upgrade",
        response_model=None,
    )
    async def upgrade_plugin(
        automation_id: str,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        try:
            package, fields = await _parse_plugin_upload(
                request,
                expected_text_fields=frozenset(
                    {"request_id", "expected_record_version", "package_sha256"}
                ),
            )
            expected_record_version = int(fields["expected_record_version"])
            if expected_record_version < 1:
                raise ValueError("expected_record_version must be positive")
        except (PluginPackageError, ValueError) as exc:
            return _plugin_error_response(exc)
        response = await _service_response(
            lambda: service_provider().upgrade(
                automation_id,
                package,
                request_id=fields["request_id"],
                expected_record_version=expected_record_version,
                transport_package_sha256=fields["package_sha256"],
                actor=actor,
            )
        )
        return _refresh_after_committed_operation(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
            committed_field="plugin_operation_committed",
            refresh_failure_message=(
                "插件升级操作已提交，但调度器刷新失败；请刷新状态或重试调度刷新，"
                "不要重复提交升级操作。"
            ),
        )

    @router.post(
        "/internal/v1/automation/instances/{automation_id}/state",
        response_model=None,
    )
    async def set_plugin_state(
        automation_id: str,
        payload: PluginStateRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        response = await _service_response(
            lambda: service_provider().set_enabled(
                automation_id,
                enabled=payload.enabled,
                request_id=payload.request_id,
                expected_record_version=payload.expected_record_version,
                actor=actor,
            )
        )
        return _refresh_after_committed_operation(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
            committed_field="plugin_operation_committed",
            refresh_failure_message=(
                "插件启停操作已提交，但调度器刷新失败；请刷新状态或重试调度刷新，"
                "不要重复提交启停操作。"
            ),
        )

    @router.post(
        "/internal/v1/automation/instances/{automation_id}/uninstall",
        response_model=None,
    )
    async def uninstall_plugin(
        automation_id: str,
        payload: PluginUninstallRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        response = await _service_response(
            lambda: service_provider().uninstall(
                automation_id,
                request_id=payload.request_id,
                expected_record_version=payload.expected_record_version,
                current_version=payload.current_version,
                actor=actor,
            )
        )
        return _refresh_after_committed_operation(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
            committed_field="plugin_operation_committed",
            refresh_failure_message=(
                "插件卸载操作已提交，但调度器刷新失败；请刷新状态或重试调度刷新，"
                "不要重复提交卸载操作。"
            ),
        )

    @router.post(
        "/internal/v1/automation/plugin-data/{automation_id}/permanent-clear",
        response_model=None,
    )
    async def permanently_clear_plugin_data(
        automation_id: str,
        payload: PluginDataPurgeRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().permanently_clear_data(
                automation_id,
                request_id=payload.request_id,
                reason=payload.reason,
                actor=actor,
            )
        )

    @router.get(
        "/internal/v1/automation/migrations/{migration_pair_id}",
        response_model=None,
    )
    async def get_plugin_migration_pair(
        migration_pair_id: str,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().migration_pair(migration_pair_id, actor=actor)
        )

    @router.post("/internal/v1/automation/migrations", response_model=None)
    async def create_plugin_migration_pair(
        payload: PluginMigrationCreateRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        create = lambda: service_provider().create_migration_pair(
            migration_pair_id=payload.migration_pair_id,
            source_automation_id=payload.source_automation_id,
            target_automation_id=payload.target_automation_id,
            business_key_fields=tuple(payload.business_key_fields),
            business_key_namespace=payload.business_key_namespace,
            request_id=payload.request_id,
            reason=payload.reason,
            actor=actor,
        )
        try:
            response = api_success(await run_in_threadpool(create))
        except MigrationPreparationPersistedError as exc:
            # The target task was already disabled in the PREPARING
            # transaction.  Reload even though clone/finalize failed so an
            # in-memory scheduler cannot retain an old target job.  This is a
            # committed, replayable outcome, not an instruction to create a
            # different pair/request.
            response = api_success(
                {
                    "migration_pair_id": exc.migration_pair_id,
                    "state": "PREPARING",
                    "migration_preparation_committed": True,
                    "retry_with_same_request_id": True,
                    "preparation_phase": exc.phase,
                }
            )
            refreshed = _refresh_after_committed_migration(
                response,
                scheduler_refresh_provider=scheduler_refresh_provider,
            )
            assert isinstance(refreshed, dict)
            refreshed["message"] = (
                "迁移准备态已持久化，目标自动入口已禁用；请使用相同 request_id 重试，"
                "不要创建新的迁移对。"
            )
            return JSONResponse(status_code=202, content=refreshed)
        except (
            AutomationPluginError,
            OrchestrationPersistenceError,
            ValueError,
        ) as exc:
            response = _plugin_error_response(exc)
        return _refresh_after_committed_migration(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
        )

    @router.post(
        "/internal/v1/automation/migrations/{migration_pair_id}/ready",
        response_model=None,
    )
    async def mark_plugin_migration_ready(
        migration_pair_id: str,
        payload: PluginMigrationOperationRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        response = await _service_response(
            lambda: service_provider().mark_migration_ready(
                migration_pair_id,
                expected_record_version=payload.expected_record_version,
                request_id=payload.request_id,
                reason=payload.reason,
                actor=actor,
            )
        )
        return _refresh_after_committed_migration(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
        )

    @router.post(
        "/internal/v1/automation/migrations/{migration_pair_id}/cutover",
        response_model=None,
    )
    async def cutover_plugin_migration_pair(
        migration_pair_id: str,
        payload: PluginMigrationOperationRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        response = await _service_response(
            lambda: service_provider().cutover_migration_pair(
                migration_pair_id,
                expected_record_version=payload.expected_record_version,
                request_id=payload.request_id,
                reason=payload.reason,
                actor=actor,
            )
        )
        return _refresh_after_committed_migration(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
        )

    @router.post(
        "/internal/v1/automation/migrations/{migration_pair_id}/rollback",
        response_model=None,
    )
    async def rollback_plugin_migration_pair(
        migration_pair_id: str,
        payload: PluginMigrationOperationRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        response = await _service_response(
            lambda: service_provider().rollback_migration_pair(
                migration_pair_id,
                expected_record_version=payload.expected_record_version,
                request_id=payload.request_id,
                reason=payload.reason,
                actor=actor,
            )
        )
        return _refresh_after_committed_migration(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
        )

    @router.post(
        "/internal/v1/automation/migrations/{migration_pair_id}/complete",
        response_model=None,
    )
    async def complete_plugin_migration_pair(
        migration_pair_id: str,
        payload: PluginMigrationOperationRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        response = await _service_response(
            lambda: service_provider().complete_migration_pair(
                migration_pair_id,
                expected_record_version=payload.expected_record_version,
                request_id=payload.request_id,
                reason=payload.reason,
                actor=actor,
            )
        )
        return _refresh_after_committed_migration(
            response,
            scheduler_refresh_provider=scheduler_refresh_provider,
        )

    @router.put(
        "/internal/v1/automation/instances/{automation_id}/configuration",
        response_model=None,
    )
    async def save_plugin_configuration(
        automation_id: str,
        payload: PluginConfigurationRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        service = service_provider()
        response = await _service_response(
            lambda: service.save_configuration(
                automation_id,
                config=payload.config,
                account_bindings=payload.account_bindings,
                resource_bindings=payload.resource_bindings,
                enabled_entrypoints=tuple(payload.enabled_entrypoints),
                schedule=payload.schedule.model_dump(),
                device_id=payload.device_id,
                request_id=payload.request_id,
                expected_project_configuration_version=(
                    payload.expected_project_configuration_version
                ),
                actor=actor,
            )
        )
        if isinstance(response, JSONResponse):
            return response
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("plugin configuration response is invalid")
        schedule = data.get("schedule")
        entrypoints = data.get("enabled_entrypoints")
        schedule_enabled = bool(
            isinstance(schedule, Mapping) and schedule.get("enabled") is True
        )
        scheduler_entrypoint_enabled = _scheduler_contribution_enabled(
            data,
            service=service,
            actor=actor,
            automation_id=automation_id,
        )
        refresh_completed = False
        if data.get("generation_ready") is not True:
            runtime_state = "BLOCKED_GENERATION"
        else:
            try:
                refreshed = (
                    scheduler_refresh_provider()
                    if scheduler_refresh_provider is not None
                    else {"initialized": False}
                )
                refresh_completed = bool(
                    isinstance(refreshed, Mapping)
                    and refreshed.get("initialized") is True
                )
            except Exception:  # noqa: BLE001 - config is durable; report refresh separately
                refresh_completed = False
            if not refresh_completed:
                runtime_state = "REFRESH_FAILED"
            elif not schedule_enabled:
                runtime_state = "DISABLED"
            elif not scheduler_entrypoint_enabled:
                runtime_state = "ENTRYPOINT_DISABLED"
            else:
                runtime_state = "ACTIVE"
        data.update(
            {
                "schedule_runtime_state": runtime_state,
                "schedule_runtime_enabled": bool(
                    runtime_state == "ACTIVE"
                ),
                "scheduler_refresh_completed": refresh_completed,
            }
        )
        return response

    @router.post(
        "/internal/v1/automation/instances/{automation_id}/generation/recover-not-applied",
        response_model=None,
    )
    async def recover_arrival_stats_not_applied(
        automation_id: str,
        payload: ArrivalStatsRecoveryRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().recover_arrival_stats_not_applied(
                automation_id,
                generation=payload.generation,
                lease_id=payload.lease_id,
                evidence_sha256=payload.evidence_sha256,
                readback=payload.readback.model_dump(),
                request_id=payload.request_id,
                actor=actor,
            )
        )

    @router.post(
        "/internal/v1/automation/instances/{automation_id}/generation/recover-unknown-write",
        response_model=None,
    )
    async def recover_unknown_write(
        automation_id: str,
        payload: UnknownWriteRecoveryRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().recover_unknown_write(
                automation_id,
                generation=payload.generation,
                lease_id=payload.lease_id,
                request_id=payload.request_id,
                actor=actor,
            )
        )

    @router.post(
        "/internal/v1/automation/instances/{automation_id}/generation/recover-current-unknown-write",
        response_model=None,
    )
    async def recover_current_unknown_write(
        automation_id: str,
        payload: CurrentUnknownWriteRecoveryRequest,
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().recover_current_unknown_write(
                automation_id,
                request_id=payload.request_id,
                actor=actor,
            )
        )

    return router


__all__ = [
    "PluginConfigurationRequest",
    "PluginDataPurgeRequest",
    "ArrivalStatsRecoveryReadbackRequest",
    "ArrivalStatsRecoveryRequest",
    "CurrentUnknownWriteRecoveryRequest",
    "UnknownWriteRecoveryRequest",
    "PluginScheduleRequest",
    "PluginStateRequest",
    "PluginUninstallRequest",
    "WorkerCapabilitiesRequest",
    "WorkerIdentityRequest",
    "WorkerPairRequest",
    "create_automation_plugin_management_router",
]
