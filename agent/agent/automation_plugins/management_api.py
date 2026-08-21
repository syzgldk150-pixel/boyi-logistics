"""Closed internal HTTP API for signed plugin and project management."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
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
from agent.automation_plugins.management import AutomationPluginManagementService
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
    enabled_entrypoints: list[str] = Field(min_length=1, max_length=4)
    device_id: str | None = Field(default=None, max_length=128)
    schedule: PluginScheduleRequest
    request_id: str = Field(min_length=1, max_length=64)
    expected_project_configuration_version: int = Field(ge=1)


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
            code="SIGNED_ZIP_REQUIRED",
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
        return await _service_response(
            lambda: service_provider().install(
                package,
                instance_name=fields["instance_name"],
                request_id=fields["request_id"],
                transport_package_sha256=fields["package_sha256"],
                actor=actor,
            )
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
        return await _service_response(
            lambda: service_provider().upgrade(
                automation_id,
                package,
                request_id=fields["request_id"],
                expected_record_version=expected_record_version,
                transport_package_sha256=fields["package_sha256"],
                actor=actor,
            )
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
        return await _service_response(
            lambda: service_provider().set_enabled(
                automation_id,
                enabled=payload.enabled,
                request_id=payload.request_id,
                expected_record_version=payload.expected_record_version,
                actor=actor,
            )
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
        return await _service_response(
            lambda: service_provider().uninstall(
                automation_id,
                request_id=payload.request_id,
                expected_record_version=payload.expected_record_version,
                current_version=payload.current_version,
                actor=actor,
            )
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
        return await _service_response(
            lambda: service_provider().save_configuration(
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

    @router.get(
        "/internal/v1/automation/instances/delivery_status/generation/diagnostic",
        response_model=None,
    )
    async def delivery_status_generation_diagnostic(
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().delivery_status_generation_diagnostic(actor=actor)
        )

    @router.get(
        "/internal/v1/automation/instances/arrival_stats/generation/diagnostic",
        response_model=None,
    )
    async def arrival_stats_generation_diagnostic(
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        actor = actor_provider(request)
        return await _service_response(
            lambda: service_provider().arrival_stats_generation_diagnostic(actor=actor)
        )

    return router


__all__ = [
    "PluginConfigurationRequest",
    "PluginScheduleRequest",
    "PluginStateRequest",
    "PluginUninstallRequest",
    "WorkerCapabilitiesRequest",
    "WorkerIdentityRequest",
    "WorkerPairRequest",
    "create_automation_plugin_management_router",
]
