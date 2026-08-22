"""Windows background Worker command loop and Tray boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
import uuid
from typing import Any, Mapping

from agent.automation_plugins.errors import WorkerProtocolError
from agent.automation_plugins.package import PackageSignatureVerifier
from agent.windows_worker.models import (
    InteractiveSessionState,
    WorkerJob,
    WorkerJobStatus,
    WorkerJobType,
    assert_broker_only_worker_value,
    worker_job_from_mapping,
)
from agent.windows_worker.ports import (
    FailClosedReleaseHoldProvider,
    LocalWorkerRuntimePort,
    ReleaseHoldProvider,
    TrayRunnerPort,
)
from agent.windows_worker.protocol import ReplayGuardPort, verify_worker_envelope
from agent.windows_worker.routes import build_worker_package_path


@dataclass(frozen=True)
class WorkerExecutionResult:
    job_id: str
    status: WorkerJobStatus
    process_confirmed: bool
    result: Mapping[str, Any]
    error_code: str | None = None
    dispatch_message_id: str | None = None
    dispatch_authorization_id: str | None = None


class WindowsWorkerBackgroundService:
    """Execute one signed exact-device job, never retrying protected writes."""

    def __init__(
        self,
        *,
        device_id: str,
        runtime: LocalWorkerRuntimePort,
        tray: TrayRunnerPort,
        release_hold_provider: ReleaseHoldProvider | None = None,
    ) -> None:
        self._device_id = str(device_id)
        self._runtime = runtime
        self._tray = tray
        self._release_hold = release_hold_provider or FailClosedReleaseHoldProvider()

    def execute_signed_command(
        self,
        envelope: Mapping[str, Any],
        *,
        verifier: PackageSignatureVerifier,
        replay_guard: ReplayGuardPort,
        expected_server_key_id: str,
        now: datetime,
    ) -> WorkerExecutionResult:
        unsigned = verify_worker_envelope(
            envelope,
            verifier=verifier,
            replay_guard=replay_guard,
            expected_device_id=self._device_id,
            expected_key_id=expected_server_key_id,
            now=now,
        )
        if unsigned["kind"] != "COMMAND" or set(unsigned["body"]) != {"job", "dispatch"}:
            raise WorkerProtocolError("signed Worker command body is invalid")
        raw_job = unsigned["body"]["job"]
        dispatch = unsigned["body"]["dispatch"]
        if not isinstance(raw_job, Mapping) or not isinstance(dispatch, Mapping):
            raise WorkerProtocolError("signed Worker command job is invalid")
        if set(dispatch) != {"release_hold", "authorization_id", "release_sha"}:
            raise WorkerProtocolError("signed Worker dispatch authorization is invalid")
        try:
            uuid.UUID(str(dispatch["authorization_id"]))
        except ValueError as exc:
            raise WorkerProtocolError("signed Worker dispatch authorization_id is invalid") from exc
        if dispatch["release_hold"] is not False or not re.fullmatch(
            r"[0-9a-f]{7,64}",
            str(dispatch["release_sha"]),
        ):
            raise WorkerProtocolError("signed Worker dispatch is held or has an invalid release")
        job = worker_job_from_mapping(raw_job)
        if job.job_type in {WorkerJobType.INSTALL, WorkerJobType.UPGRADE}:
            job = self._localized_package_job(
                job,
                dispatch_authorization_id=str(dispatch["authorization_id"]),
            )
        result = self.execute(
            job,
            now=now,
            signed_dispatch_authorized=True,
        )
        return replace(
            result,
            dispatch_message_id=str(unsigned["message_id"]),
            dispatch_authorization_id=str(dispatch["authorization_id"]),
        )

    @staticmethod
    def _localized_package_job(
        job: WorkerJob,
        *,
        dispatch_authorization_id: str,
    ) -> WorkerJob:
        payload = dict(job.payload)
        package = payload.get("package")
        if (
            set(payload) != {"package"}
            or not isinstance(package, Mapping)
            or set(package) != {"plugin_id", "version", "package_sha256"}
        ):
            raise WorkerProtocolError("signed Worker package identity is not closed")
        plugin_id = str(package.get("plugin_id") or "")
        version = str(package.get("version") or "")
        package_sha256 = str(package.get("package_sha256") or "")
        if plugin_id != job.plugin_id or version != job.plugin_version:
            raise WorkerProtocolError("signed Worker package identity differs from its job")
        try:
            package_path = build_worker_package_path(
                plugin_id=plugin_id,
                version=version,
                package_sha256=package_sha256,
                dispatch_authorization_id=dispatch_authorization_id,
            )
        except ValueError as exc:
            raise WorkerProtocolError("signed Worker package identity is invalid") from exc
        return replace(
            job,
            payload={
                "package_url": package_path,
                "package_sha256": package_sha256,
            },
        )

    def execute(
        self,
        job: WorkerJob,
        *,
        now: datetime | None = None,
        signed_dispatch_authorized: bool = False,
    ) -> WorkerExecutionResult:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise WorkerProtocolError("Worker execution time must be timezone-aware")
        if job.target_device_id != self._device_id or job.max_attempts != 1:
            raise WorkerProtocolError("Worker job targets another device or allows retry")
        if self._release_hold.is_held() and not signed_dispatch_authorized:
            return WorkerExecutionResult(
                job_id=job.job_id,
                status=WorkerJobStatus.BLOCKED_DATA,
                process_confirmed=True,
                result={},
                error_code="RELEASE_HELD",
            )
        if current.astimezone(timezone.utc) >= job.deadline_at.astimezone(timezone.utc):
            return WorkerExecutionResult(
                job_id=job.job_id,
                status=WorkerJobStatus.FAILED,
                process_confirmed=True,
                result={},
                error_code="WORKER_DEADLINE_EXCEEDED",
            )
        if not self._runtime.begin_once(job):
            prior = self._runtime.prior_result(job.job_id)
            if not isinstance(prior, Mapping):
                raise WorkerProtocolError("Worker job idempotency record is incomplete")
            return WorkerExecutionResult(
                job_id=job.job_id,
                status=WorkerJobStatus(str(prior["status"])),
                process_confirmed=True,
                result=dict(prior.get("result") or {}),
                error_code=str(prior.get("error_code") or "") or None,
            )
        process_dispatched = False
        try:
            if job.job_type in {WorkerJobType.INSTALL, WorkerJobType.UPGRADE}:
                result = self._runtime.install_or_upgrade(job)
            elif job.job_type == WorkerJobType.INVOKE:
                if (
                    job.requires_interactive_session
                    and self._tray.session_state() != InteractiveSessionState.AVAILABLE.value
                ):
                    response = WorkerExecutionResult(
                        job_id=job.job_id,
                        status=WorkerJobStatus.BLOCKED_DATA,
                        process_confirmed=True,
                        result={},
                        error_code="INTERACTIVE_SESSION_UNAVAILABLE",
                    )
                    self._runtime.save_result(
                        job.job_id,
                        {
                            "status": response.status.value,
                            "result": {},
                            "error_code": response.error_code,
                        },
                    )
                    return response
                process_dispatched = True
                result = self._tray.run_instance_action(job)
            elif job.job_type in {WorkerJobType.UNINSTALL, WorkerJobType.CLEANUP}:
                if self._runtime.has_cleanup_blocker(
                    job.automation_id,
                    excluding_job_id=job.job_id,
                ):
                    raise WorkerProtocolError(
                        "active job or unknown write outcome blocks Worker cleanup"
                    )
                # The background runtime owns package/venv/config/log cleanup;
                # Tray cleanup is invoked only for declared interactive data.
                if job.requires_interactive_session:
                    self._tray.cleanup_instance(job)
                result = self._runtime.cleanup_instance(job)
            else:  # pragma: no cover - enum guard
                raise WorkerProtocolError("Worker job type is unsupported")
            assert_broker_only_worker_value(result)
            response = WorkerExecutionResult(
                job_id=job.job_id,
                status=WorkerJobStatus.SUCCEEDED,
                process_confirmed=True,
                result=dict(result),
            )
        except Exception as exc:
            unknown = (
                job.is_write
                and job.job_type == WorkerJobType.INVOKE
                and process_dispatched
            )
            response = WorkerExecutionResult(
                job_id=job.job_id,
                status=WorkerJobStatus.OUTCOME_UNKNOWN if unknown else WorkerJobStatus.FAILED,
                process_confirmed=not unknown,
                result={},
                error_code="WRITE_OUTCOME_UNKNOWN" if unknown else getattr(
                    exc, "code", type(exc).__name__.upper()
                ),
            )
        self._runtime.save_result(
            job.job_id,
            {
                "status": response.status.value,
                "result": dict(response.result),
                "error_code": response.error_code,
            },
        )
        return response
