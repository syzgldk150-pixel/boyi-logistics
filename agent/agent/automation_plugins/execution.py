"""Route instance-bound actions to core tools or isolated plugin processes."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import signal
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.host_capability_registry import (
    CapabilityEffect,
    governance_for_effect,
)
from agent.automation_plugins.code_owned_fields import (
    SCAN_PHASE_PREVIEW,
    apply_scan_execution_boundary,
    resolve_scan_capability_phase,
)
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.migration import (
    MigrationRunClaim,
    PluginMigrationRuntimeCoordinator,
)
from agent.automation_plugins.models import (
    GenerationBoundResult,
    GenerationVerificationContext,
    PluginRuntimeModel,
    RuntimeGenerationLease,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.ports import (
    ExecutionCapabilityIssuerPort,
    PluginIntegrityVerifierPort,
    PluginSandboxLauncherPort,
    RuntimeGenerationLeasePort,
)
from agent.automation_plugins.runtime_environment import minimal_plugin_environment
from agent.automation_plugins.sandbox import FailClosedPluginSandbox, SandboxCanaryResult
from agent.automation_plugins.service_v2_contract import (
    resolve_service_v2_selection_target,
)
from agent.automation_plugins.storage import validate_plugin_tree, validate_regular_plugin_file
from agent.tool_registry import validate_schema_instance
from shared.automation_project_authorization import (
    AutomationProjectContractError,
    AutomationProjectInvocation,
)
from shared.redaction import redact_text


MAX_PLUGIN_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_PLUGIN_STDERR_BYTES = 1024 * 1024
_WRITE_TYPES = frozenset({"internal_projection_write", "external_write", "financial_write", "destructive"})
_FORBIDDEN_ARGUMENT_TOKENS = ("password", "cookie", "credential", "secret", "token")
_MAX_SERVICE_CALL_DEPTH = 8
_SERVICE_INVOKE_CONTRIBUTION_ID = "host.service.invoke"
_T = TypeVar("_T")
_SERVICE_OPERATION_TYPES = {
    CapabilityEffect.READ: "read",
    CapabilityEffect.COMPUTE: "compute",
    CapabilityEffect.INTERNAL_WRITE: "internal_projection_write",
    CapabilityEffect.EXTERNAL_WRITE: "external_write",
    CapabilityEffect.DESTRUCTIVE: "destructive",
}


class FilesystemPluginIntegrityVerifier:
    """Verify every signed package file immediately before execution."""

    def verify_install_root(self, runtime_metadata: Mapping[str, object]) -> None:
        root_value = runtime_metadata.get("install_root")
        metadata = runtime_metadata.get("install_metadata")
        if not isinstance(root_value, str) or not root_value or not isinstance(metadata, Mapping):
            raise PluginExecutionError("plugin install metadata is incomplete", code="PLUGIN_NOT_MATERIALIZED")
        raw_root = Path(root_value)
        try:
            validate_plugin_tree(raw_root)
        except Exception as exc:
            raise PluginExecutionError(
                "plugin immutable tree contains an unsafe filesystem entry",
                code="PLUGIN_INTEGRITY_FAILED",
            ) from exc
        root = raw_root.resolve()
        package_root = (root / "package").resolve()
        try:
            package_root.relative_to(root)
        except ValueError as exc:
            raise PluginExecutionError("plugin package root escaped its immutable version") from exc
        files = metadata.get("package_files")
        if not isinstance(files, list) or not files:
            raise PluginExecutionError("plugin signed file table is missing", code="PLUGIN_INTEGRITY_MISSING")
        for raw in files:
            if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256", "size"}:
                raise PluginExecutionError("plugin signed file table is invalid")
            relative = str(raw["path"] or "")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
                raise PluginExecutionError("plugin signed path is unsafe")
            target = package_root.joinpath(*pure.parts).resolve()
            try:
                target.relative_to(package_root)
            except ValueError as exc:
                raise PluginExecutionError("plugin signed path escaped package root") from exc
            try:
                validate_regular_plugin_file(target)
            except Exception as exc:
                raise PluginExecutionError(
                    "plugin signed file is missing or unsafe",
                    code="PLUGIN_INTEGRITY_FAILED",
                ) from exc
            expected_size = raw["size"]
            if isinstance(expected_size, bool) or not isinstance(expected_size, int):
                raise PluginExecutionError("plugin signed size is invalid")
            content = target.read_bytes()
            if len(content) != expected_size or hashlib.sha256(content).hexdigest() != raw["sha256"]:
                raise PluginExecutionError("plugin install root failed integrity verification")


class PluginExecutionRouter:
    """ToolExecutor-compatible router keyed by an automation instance."""

    def __init__(
        self,
        *,
        core_executor: Any,
        capability_issuer: ExecutionCapabilityIssuerPort,
        integrity_verifier: PluginIntegrityVerifierPort | None = None,
        sandbox_launcher: PluginSandboxLauncherPort | None = None,
        generation_leases: RuntimeGenerationLeasePort | None = None,
        migration_runtime: PluginMigrationRuntimeCoordinator | None = None,
        release_hold_provider: Callable[[], bool] | None = None,
        allow_development_builtin: bool = False,
    ) -> None:
        self._core = core_executor
        self._issuer = capability_issuer
        self._integrity = integrity_verifier or FilesystemPluginIntegrityVerifier()
        self._sandbox = sandbox_launcher or FailClosedPluginSandbox()
        self._allow_development_builtin = bool(allow_development_builtin)
        self._generation_leases = generation_leases
        self._migration_runtime = migration_runtime
        # A production caller must explicitly prove that release activation
        # completed.  Missing or failing hold state is intentionally held.
        self._release_hold_provider = release_hold_provider or (lambda: True)
        self._running: dict[str, dict[str, Any]] = {}

    async def startup_sandbox_canary(self) -> SandboxCanaryResult:
        """Return the cached real-sandbox proof used by production health."""

        runner = getattr(self._sandbox, "startup_canary", None)
        if not callable(runner):
            return SandboxCanaryResult(
                False,
                "PLUGIN_SANDBOX_CANARY_UNAVAILABLE",
                datetime.now(timezone.utc),
            )
        try:
            result = await runner()
        except Exception:
            return SandboxCanaryResult(
                False,
                "PLUGIN_SANDBOX_CANARY_FAILED",
                datetime.now(timezone.utc),
            )
        if not isinstance(result, SandboxCanaryResult):
            return SandboxCanaryResult(
                False,
                "PLUGIN_SANDBOX_CANARY_INVALID",
                datetime.now(timezone.utc),
            )
        return result

    @staticmethod
    def _is_live_plugin_invocation(current: Mapping[str, Any]) -> bool:
        proc = current.get("proc")
        return proc is not None and proc.returncode is None

    @staticmethod
    def _minimal_environment(
        *,
        capability: str,
        automation_id: str,
        plugin_id: str,
        plugin_version: str,
        broker_endpoint: str,
        broker_call_timeout_seconds: int,
    ) -> dict[str, str]:
        return minimal_plugin_environment(
            capability=capability,
            automation_id=automation_id,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            broker_endpoint=broker_endpoint,
            broker_call_timeout_seconds=broker_call_timeout_seconds,
        )

    @staticmethod
    def _reject_sensitive_arguments(arguments: Mapping[str, Any]) -> None:
        for key in arguments:
            lowered = str(key).lower()
            if (
                lowered == "account_id"
                or lowered == "account_ids"
                or lowered.endswith(("_account_id", "_account_ids"))
                or any(token in lowered for token in _FORBIDDEN_ARGUMENT_TOKENS)
            ):
                raise PluginExecutionError(
                    "plugin subprocess cannot receive account or credential material",
                    code="PLUGIN_ARGUMENT_FORBIDDEN",
                )

    @staticmethod
    def _run_binding(
        value: Mapping[str, object] | None,
        *,
        automation_id: str,
        generation: int,
    ) -> dict[str, str]:
        expected_fields = {
            "run_id",
            "step_id",
            "_automation_project_invocation",
        }
        if value is None or set(value) != expected_fields:
            raise PluginExecutionError(
                "trusted plugin invocation binding is invalid",
                code="PLUGIN_RUN_BINDING_INVALID",
            )
        result = {key: str(value.get(key) or "").strip() for key in ("run_id", "step_id")}
        if any(not item or len(item) > 191 for item in result.values()):
            raise PluginExecutionError(
                "trusted plugin invocation binding is invalid",
                code="PLUGIN_RUN_BINDING_INVALID",
            )
        raw_invocation = value.get("_automation_project_invocation")
        if not isinstance(raw_invocation, Mapping):
            raise PluginExecutionError(
                "trusted automation project invocation is missing",
                code="PLUGIN_PROJECT_INVOCATION_REQUIRED",
            )
        try:
            invocation = AutomationProjectInvocation.from_mapping(raw_invocation)
        except AutomationProjectContractError as exc:
            raise PluginExecutionError(
                "trusted automation project invocation is invalid",
                code="PLUGIN_PROJECT_INVOCATION_INVALID",
            ) from exc
        if (
            invocation.automation_id != automation_id
            or invocation.automation_generation != generation
        ):
            raise PluginExecutionError(
                "trusted automation project invocation generation changed",
                code="PLUGIN_PROJECT_GENERATION_CONFLICT",
            )
        result.update(
            {
                "entrypoint": invocation.entrypoint.value,
                "contract_id": invocation.contract_id,
            }
        )
        return result

    @staticmethod
    def _service_run_binding(
        capability: Mapping[str, Any],
        *,
        service: str,
        operation: str,
        effect: CapabilityEffect,
        call_chain: tuple[str, ...],
    ) -> dict[str, Any]:
        metadata = capability.get("_plugin_runtime")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("runtime_model") != PluginRuntimeModel.SERVICE_V2.value
        ):
            raise PluginExecutionError(
                "service Provider is not a committed Service v2 capability",
                code="SERVICE_PROVIDER_ROUTE_INVALID",
            )
        contracts = metadata.get("service_contracts")
        provided = contracts.get("provides") if isinstance(contracts, Mapping) else None
        matches = [
            item
            for item in provided or ()
            if isinstance(item, Mapping) and item.get("service") == service
        ]
        operations = matches[0].get("operations") if len(matches) == 1 else None
        operation_effects = {
            str(item.get("name") or ""): str(item.get("effect") or "")
            for item in operations or ()
            if isinstance(item, Mapping) and set(item) == {"name", "effect"}
        }
        if (
            not service
            or not operation
            or not isinstance(operations, (list, tuple))
            or len(operation_effects) != len(operations)
            or operation_effects.get(operation) != effect.value
        ):
            raise PluginExecutionError(
                "service Provider operation is absent from its committed contract",
                code="SERVICE_OPERATION_UNDECLARED",
            )
        if (
            not call_chain
            or call_chain[-1] != service
            or len(call_chain) > _MAX_SERVICE_CALL_DEPTH
            or len(call_chain) != len(set(call_chain))
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or len(item) > 191
                for item in call_chain
            )
        ):
            raise PluginExecutionError(
                "service invocation ancestry is invalid",
                code="SERVICE_CALL_CHAIN_INVALID",
            )
        invocation_id = str(uuid.uuid4())
        return {
            "run_id": invocation_id,
            "step_id": f"service:{invocation_id}",
            "entrypoint": "service",
            "contract_id": _SERVICE_INVOKE_CONTRIBUTION_ID,
            "service_target": {
                "service": service,
                "operation": operation,
                "contribution_id": _SERVICE_INVOKE_CONTRIBUTION_ID,
                "contribution_kind": "service",
            },
            "service_governance": governance_for_effect(effect).to_mapping(),
        }

    @staticmethod
    def _service_effect_capability(
        capability: Mapping[str, Any],
        effect: CapabilityEffect,
    ) -> dict[str, Any]:
        """Bind Provider lease governance to the exact immutable operation.

        A Service v2 package can expose read and protected operations together;
        its primary tool contract is therefore not sufficient to decide one
        internal call's lease outcome.  The Registry-selected manifest effect
        is the sole authority for this invocation.
        """

        operation_type = _SERVICE_OPERATION_TYPES.get(effect)
        if operation_type is None:
            raise PluginExecutionError(
                "service Provider operation effect is invalid",
                code="SERVICE_OPERATION_UNDECLARED",
            )
        governance = governance_for_effect(effect).to_mapping()
        resolved = copy.deepcopy(dict(capability))
        for field_name, value in governance.items():
            resolved[field_name] = copy.deepcopy(value)
        resolved["operation_type"] = operation_type
        return resolved

    @classmethod
    def _service_contribution_capability(
        cls,
        capability: Mapping[str, Any],
        *,
        contribution_id: str,
    ) -> dict[str, Any]:
        """Resolve one direct Service v2 contribution from committed material."""

        metadata = capability.get("_plugin_runtime")
        compiled_invocations = (
            metadata.get("compiled_invocations")
            if isinstance(metadata, Mapping)
            else None
        )
        compiled = (
            compiled_invocations.get(contribution_id)
            if isinstance(compiled_invocations, Mapping)
            else None
        )
        target = compiled.get("target") if isinstance(compiled, Mapping) else None
        governance = (
            compiled.get("governance") if isinstance(compiled, Mapping) else None
        )
        exact_governance = cls._validated_service_governance(governance)
        if (
            not isinstance(target, Mapping)
            or set(target)
            != {
                "service",
                "operation",
                "contribution_id",
                "contribution_kind",
            }
            or target.get("contribution_id") != contribution_id
            or any(
                not isinstance(target.get(field_name), str)
                or not str(target.get(field_name) or "").strip()
                for field_name in (
                    "service",
                    "operation",
                    "contribution_id",
                    "contribution_kind",
                )
            )
        ):
            raise PluginExecutionError(
                "service-v2 contribution target is invalid",
                code="PLUGIN_GENERATION_METADATA_INVALID",
            )
        try:
            effect = CapabilityEffect(str(exact_governance["effect"]))
        except ValueError as exc:
            raise PluginExecutionError(
                "service-v2 contribution effect is invalid",
                code="PLUGIN_GENERATION_METADATA_INVALID",
            ) from exc
        resolved = cls._service_effect_capability(capability, effect)
        resolved["service"] = str(target["service"])
        resolved["operation"] = str(target["operation"])
        return resolved

    @staticmethod
    def _validated_service_governance(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise PluginExecutionError(
                "service invocation governance is invalid",
                code="PLUGIN_GENERATION_METADATA_INVALID",
            )
        try:
            effect = CapabilityEffect(str(value.get("effect") or ""))
        except (TypeError, ValueError) as exc:
            raise PluginExecutionError(
                "service invocation governance effect is invalid",
                code="PLUGIN_GENERATION_METADATA_INVALID",
            ) from exc
        expected = governance_for_effect(effect).to_mapping()
        observed = copy.deepcopy(dict(value))
        if canonical_json_bytes(observed) != canonical_json_bytes(expected):
            raise PluginExecutionError(
                "service invocation governance drifted",
                code="PLUGIN_GENERATION_METADATA_INVALID",
            )
        return expected

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    @staticmethod
    async def _read_limited(reader: asyncio.StreamReader, limit: int) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > limit:
                raise PluginExecutionError("plugin process output exceeded the allowed limit")
            chunks.append(chunk)

    @classmethod
    async def _collect_bounded_process_output(cls, proc: asyncio.subprocess.Process) -> None:
        """Drain failed sandbox output without exposing transport internals."""

        if proc.stdout is None or proc.stderr is None:
            return
        await asyncio.gather(
            cls._read_limited(proc.stdout, MAX_PLUGIN_OUTPUT_BYTES),
            cls._read_limited(proc.stderr, MAX_PLUGIN_STDERR_BYTES),
            return_exceptions=True,
        )

    @staticmethod
    async def _cancel_and_reap_tasks(*tasks: asyncio.Task[bytes] | None) -> None:
        pending = [task for task in tasks if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if tasks:
            await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)

    @staticmethod
    async def _await_with_timeout(awaitable: Awaitable[_T], *, timeout: float) -> _T:
        """Apply a timeout without Python 3.10 ``wait_for`` cancelling races."""

        operation = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait((operation,), timeout=timeout)
        except asyncio.CancelledError:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise
        if not done:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise asyncio.TimeoutError
        return operation.result()

    async def _acquire_generation_lease(
        self,
        automation_id: str,
        *,
        expected_generation: int,
        expected_manifest_sha256: str,
        lease_id: str,
        orchestration_run_id: str,
        expires_at: datetime,
    ) -> RuntimeGenerationLease:
        """Acquire a database-backed lease without blocking the event loop."""

        repository = self._generation_leases
        if repository is None:
            raise PluginExecutionError(
                "atomic generation lease service is not configured",
                code="PLUGIN_GENERATION_LEASE_UNAVAILABLE",
            )
        task = asyncio.create_task(
            asyncio.to_thread(
                repository.acquire_committed_generation,
                automation_id,
                expected_generation=expected_generation,
                expected_manifest_sha256=expected_manifest_sha256,
                lease_id=lease_id,
                orchestration_run_id=orchestration_run_id,
                expires_at=expires_at,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread cannot be cancelled after it entered the repository. If
            # acquisition completed, release that exact lease before honoring
            # cancellation so a Run never leaves an orphaned generation lease.
            try:
                lease = await asyncio.shield(task)
            except Exception:
                pass
            else:
                try:
                    await self._release_generation_lease(
                        lease,
                        outcome=RuntimeLeaseOutcome.FAILED_BEFORE_WRITE,
                    )
                except Exception:
                    pass
            raise

    async def _release_generation_lease(
        self,
        lease: RuntimeGenerationLease,
        *,
        outcome: RuntimeLeaseOutcome,
    ) -> None:
        repository = self._generation_leases
        if repository is None:
            raise PluginExecutionError(
                "atomic generation lease service is not configured",
                code="PLUGIN_GENERATION_LEASE_UNAVAILABLE",
            )
        task = asyncio.create_task(
            asyncio.to_thread(repository.release_generation, lease, outcome=outcome)
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    async def _claim_migration_run(
        self,
        *,
        automation_id: str,
        params: Mapping[str, Any],
        run_id: str,
        lease_id: str,
        now: datetime,
        expires_at: datetime,
        target_generation: int,
        contribution_id: str,
        contribution_kind: str,
        dry_run: bool,
    ) -> MigrationRunClaim | None:
        coordinator = self._migration_runtime
        if coordinator is None:
            return None
        task = asyncio.create_task(
            asyncio.to_thread(
                coordinator.claim_for_execution,
                automation_id,
                params,
                run_id,
                lease_id,
                now,
                expires_at,
                target_generation,
                contribution_id,
                contribution_kind,
                dry_run,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                claim = await asyncio.shield(task)
            except Exception:
                pass
            else:
                if claim is not None:
                    try:
                        await asyncio.shield(
                            asyncio.to_thread(
                                coordinator.settle_before_write_result,
                                claim,
                                "CANCELLED",
                                now=datetime.now(timezone.utc),
                            )
                        )
                    except Exception:
                        pass
            raise

    async def _settle_migration_before_verification(
        self,
        claim: MigrationRunClaim | None,
        outcome: RuntimeLeaseOutcome,
    ) -> None:
        coordinator = self._migration_runtime
        if coordinator is None or claim is None:
            return
        task = asyncio.create_task(
            asyncio.to_thread(
                coordinator.settle_before_write_result,
                claim,
                outcome.value,
                now=datetime.now(timezone.utc),
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise

    def _validated_plugin_launch_paths(
        self,
        metadata: Mapping[str, object],
        runtime: Mapping[str, object],
    ) -> tuple[str, str, str, Path, PurePosixPath, PurePosixPath]:
        """Validate the immutable tree and filesystem launch paths off-loop."""

        self._integrity.verify_install_root(metadata)
        automation_id = str(metadata.get("automation_id") or "")
        plugin_id = str(metadata.get("plugin_id") or "")
        version = str(metadata.get("version") or "")
        root = Path(str(metadata.get("install_root") or "")).resolve()
        install_metadata = metadata.get("install_metadata")
        if not all((automation_id, plugin_id, version)) or not isinstance(install_metadata, Mapping):
            raise PluginExecutionError("plugin instance metadata is incomplete")
        python_relative = PurePosixPath(str(install_metadata.get("python_relative") or ""))
        entry_relative = PurePosixPath(str(runtime.get("entrypoint") or ""))
        if any(part in {"", ".", ".."} for part in (*python_relative.parts, *entry_relative.parts)):
            raise PluginExecutionError("plugin executable path is unsafe")
        python_path = root.joinpath(*python_relative.parts).resolve()
        entrypoint = (root / "package").joinpath(*entry_relative.parts).resolve()
        for target in (python_path, entrypoint):
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise PluginExecutionError("plugin executable escaped its install root") from exc
            if not target.is_file() or target.is_symlink():
                raise PluginExecutionError("plugin executable is missing or unsafe")
        return automation_id, plugin_id, version, root, python_relative, entry_relative

    @staticmethod
    def _lease_capability(
        lease: RuntimeGenerationLease,
        *,
        automation_id: str,
    ) -> dict[str, Any]:
        capability = lease.runtime_metadata
        if not isinstance(capability, Mapping):
            raise PluginExecutionError(
                "committed generation has no execution metadata",
                code="PLUGIN_GENERATION_METADATA_INVALID",
            )
        result = copy.deepcopy(dict(capability))
        metadata = result.get("_plugin_runtime")
        snapshot = lease.snapshot
        account_bindings = metadata.get("account_bindings") if isinstance(metadata, Mapping) else None
        account_bindings_sha256 = (
            hashlib.sha256(canonical_json_bytes(dict(account_bindings))).hexdigest()
            if isinstance(account_bindings, Mapping)
            else ""
        )
        if (
            not isinstance(metadata, Mapping)
            or lease.automation_id != automation_id
            or snapshot.automation_id != automation_id
            or lease.generation != snapshot.generation
            or metadata.get("automation_id") != automation_id
            or metadata.get("generation") != lease.generation
            or metadata.get("plugin_id") != snapshot.plugin_id
            or metadata.get("version") != snapshot.plugin_version
            or metadata.get("package_sha256") != snapshot.package_sha256
            or metadata.get("manifest_sha256") != snapshot.manifest_sha256
            or metadata.get("trust_source") != snapshot.trust_source.value
            or account_bindings_sha256 != snapshot.account_bindings_sha256
        ):
            raise PluginExecutionError(
                "committed generation execution metadata does not match its snapshot",
                code="PLUGIN_GENERATION_METADATA_INVALID",
            )
        return result

    @staticmethod
    def _write_failure_outcome(
        capability: Mapping[str, Any],
        *,
        process_launched: bool,
        started_mutating_call_count: int | None,
    ) -> RuntimeLeaseOutcome:
        if started_mutating_call_count is not None and started_mutating_call_count > 0:
            return RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
        if capability.get("operation_type") not in _WRITE_TYPES:
            return RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
        if started_mutating_call_count == 0:
            return RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
        return (
            RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
            if process_launched
            else RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
        )

    @staticmethod
    def _lease_outcome(
        capability: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        process_launched: bool,
        started_mutating_call_count: int | None = None,
    ) -> RuntimeLeaseOutcome:
        error_code = str(result.get("error_code") or "").upper()
        nested_error = result.get("error")
        if not error_code and isinstance(nested_error, Mapping):
            error_code = str(nested_error.get("code") or "").upper()
        if error_code == "WRITE_OUTCOME_UNKNOWN":
            return RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
        process_success = result.get("success") is True or str(result.get("status") or "").upper() == "SUCCESS"
        if process_success:
            if capability.get("operation_type") in _WRITE_TYPES:
                if started_mutating_call_count == 0:
                    return RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
                if (
                    isinstance(started_mutating_call_count, int)
                    and not isinstance(started_mutating_call_count, bool)
                    and started_mutating_call_count > 0
                ):
                    return RuntimeLeaseOutcome.VERIFYING
                return RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
            if (
                isinstance(started_mutating_call_count, int)
                and not isinstance(started_mutating_call_count, bool)
                and started_mutating_call_count > 0
            ):
                return RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
            return RuntimeLeaseOutcome.SUCCEEDED
        return PluginExecutionRouter._write_failure_outcome(
            capability,
            process_launched=process_launched,
            started_mutating_call_count=started_mutating_call_count,
        )

    def _observe_started_mutating_calls(
        self,
        capability: str,
        execution_state: dict[str, object],
    ) -> None:
        try:
            observer = getattr(self._issuer, "started_mutating_call_count", None)
            if not callable(observer):
                raise ValueError("started mutating call observation is unavailable")
            observed = observer(capability)
            if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
                raise ValueError("invalid broker call count")
            execution_state["started_mutating_call_count"] = observed
        except Exception:
            execution_state["started_mutating_call_count"] = None

    def _observe_host_call_observations(
        self,
        capability: str,
        execution_state: dict[str, object],
    ) -> None:
        try:
            observer = getattr(self._issuer, "broker_call_observations", None)
            if not callable(observer):
                raise ValueError("broker observations are unavailable")
            observed = observer(capability)
            if not isinstance(observed, (list, tuple)) or any(
                not isinstance(item, Mapping) for item in observed
            ):
                raise ValueError("broker observations are invalid")
            execution_state["host_call_observations"] = tuple(
                copy.deepcopy(dict(item)) for item in observed
            )
        except Exception:
            execution_state["host_call_observations"] = ()

    def _failure_code(
        self,
        capability: Mapping[str, Any],
        fallback: str,
        token: str,
        execution_state: dict[str, object] | None,
    ) -> str:
        if capability.get("operation_type") not in _WRITE_TYPES:
            return fallback
        if execution_state is None:
            return "WRITE_OUTCOME_UNKNOWN"
        self._observe_started_mutating_calls(token, execution_state)
        outcome = self._write_failure_outcome(
            capability,
            process_launched=bool(execution_state["process_launched"]),
            started_mutating_call_count=execution_state["started_mutating_call_count"],
        )
        return fallback if outcome is RuntimeLeaseOutcome.FAILED_BEFORE_WRITE else "WRITE_OUTCOME_UNKNOWN"

    def _govern_started_write_failure(
        self,
        capability: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        token: str,
        execution_state: dict[str, object] | None,
    ) -> Mapping[str, Any]:
        """Make a started write's governing result code non-retryable.

        Plugin failures remain useful diagnostics, but cannot override the
        durable started-write boundary.  The original code is retained only as
        a subordinate, redacted contract diagnostic.
        """

        if capability.get("operation_type") not in _WRITE_TYPES or execution_state is None:
            return result
        self._observe_started_mutating_calls(token, execution_state)
        if execution_state["started_mutating_call_count"] in (None, 0):
            return result
        original_error = result.get("error")
        original_code = str(
            result.get("error_code")
            or (original_error.get("code") if isinstance(original_error, Mapping) else "")
            or "PLUGIN_RESULT_FAILED"
        ).upper()[:64]
        safe_error = dict(original_error) if isinstance(original_error, Mapping) else {}
        safe_error["code"] = "WRITE_OUTCOME_UNKNOWN"
        safe_error["original_error_code"] = original_code
        governed = dict(result)
        governed["error_code"] = "WRITE_OUTCOME_UNKNOWN"
        governed["error"] = safe_error
        governed["retryable"] = False
        return governed

    @staticmethod
    def _has_observed_started_write(execution_state: Mapping[str, object]) -> bool:
        started_count = execution_state.get("started_mutating_call_count")
        return isinstance(started_count, int) and not isinstance(started_count, bool) and started_count > 0

    @staticmethod
    def _lease_release_unknown_result(release_error: Exception) -> Mapping[str, Any]:
        """Do not turn a post-write persistence failure into a normal tool error.

        The broker's started-write receipt is authoritative: if recording the
        corresponding generation outcome fails, no success or retryable result
        may escape.  The storage failure remains a redacted subordinate
        diagnostic for operators and recovery tooling.
        """

        return {
            "status": "FAILED",
            "data": {},
            "meta": {"blocked_status": "BLOCKED_DATA"},
            "warnings": [],
            "error": {
                "code": "WRITE_OUTCOME_UNKNOWN",
                "message": "A started plugin write could not persist its generation lease outcome",
                "retryable": False,
                "persistence_diagnostic": {
                    "code": type(release_error).__name__.upper()[:64],
                    "message": redact_text(release_error)[:500],
                },
            },
        }

    async def execute(
        self,
        capability: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        trusted_scheduler_context: Mapping[str, object] | None = None,
        trusted_invocation_context: Mapping[str, object] | None = None,
        execution_identity: Mapping[str, object] | None = None,
        _service_target: Mapping[str, str] | None = None,
        _service_call_chain: tuple[str, ...] = (),
    ) -> Mapping[str, Any]:
        initial_metadata = capability.get("_plugin_runtime")
        if not isinstance(initial_metadata, Mapping):
            return await self._core.execute(
                dict(capability),
                dict(params),
                trusted_scheduler_context=trusted_scheduler_context,
                execution_identity=execution_identity,
            )
        automation_id = str(initial_metadata.get("automation_id") or "")
        raw_generation = initial_metadata.get("generation")
        if not automation_id or isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            raise PluginExecutionError(
                "plugin capability has no committed generation",
                code="PLUGIN_GENERATION_REQUIRED",
            )
        if self._generation_leases is None:
            raise PluginExecutionError(
                "atomic generation lease service is not configured",
                code="PLUGIN_GENERATION_LEASE_UNAVAILABLE",
            )
        try:
            release_held = await asyncio.to_thread(self._release_hold_provider)
        except Exception as exc:
            raise PluginExecutionError(
                "automation plugin release hold state is unavailable",
                code="PLUGIN_RELEASE_HOLD_UNAVAILABLE",
            ) from exc
        if release_held is not False:
            raise PluginExecutionError(
                "automation plugin invocation is held for release",
                code="PLUGIN_RELEASE_HELD",
            )
        if _service_target is None:
            if _service_call_chain:
                raise PluginExecutionError(
                    "service invocation ancestry has no target",
                    code="SERVICE_CALL_CHAIN_INVALID",
                )
            run_binding = self._run_binding(
                trusted_invocation_context,
                automation_id=automation_id,
                generation=raw_generation,
            )
            if initial_metadata.get("runtime_model") == PluginRuntimeModel.SERVICE_V2.value:
                try:
                    selection_target = resolve_service_v2_selection_target(
                        initial_metadata,
                        contribution_id=run_binding["contract_id"],
                        contribution_kind=run_binding["entrypoint"],
                        arguments=params,
                    )
                except ValueError as exc:
                    raise PluginExecutionError(
                        "service-v2 selection invocation is invalid",
                        code="PLUGIN_GENERATION_METADATA_INVALID",
                    ) from exc
                if selection_target is not None:
                    target, governance, _selection_phase = selection_target
                    run_binding = {
                        **run_binding,
                        "service_target": target,
                        "service_governance": governance,
                    }
        else:
            if set(_service_target) != {"service", "operation", "effect"}:
                raise PluginExecutionError(
                    "service invocation target is invalid",
                    code="SERVICE_PROVIDER_ROUTE_INVALID",
                )
            try:
                service_effect = CapabilityEffect(
                    str(_service_target.get("effect") or "")
                )
            except (TypeError, ValueError) as exc:
                raise PluginExecutionError(
                    "service invocation effect is invalid",
                    code="SERVICE_OPERATION_UNDECLARED",
                ) from exc
            run_binding = self._service_run_binding(
                capability,
                service=str(_service_target.get("service") or ""),
                operation=str(_service_target.get("operation") or ""),
                effect=service_effect,
                call_chain=_service_call_chain,
            )
        timeout = max(1, min(int(capability.get("timeout") or 60), 3600))
        lease_id = str(uuid.uuid4())
        acquired_at = datetime.now(timezone.utc)
        expires_at = acquired_at + timedelta(seconds=timeout + 60)
        migration_claim = await self._claim_migration_run(
            automation_id=automation_id,
            params=params,
            run_id=run_binding["run_id"],
            lease_id=lease_id,
            now=acquired_at,
            expires_at=expires_at,
            target_generation=raw_generation,
            contribution_id=run_binding["contract_id"],
            contribution_kind=run_binding["entrypoint"],
            dry_run=params.get("dry_run") is True,
        )
        try:
            lease = await self._acquire_generation_lease(
                automation_id,
                expected_generation=raw_generation,
                expected_manifest_sha256=str(
                    initial_metadata.get("manifest_sha256") or ""
                ),
                lease_id=lease_id,
                orchestration_run_id=run_binding["run_id"],
                expires_at=expires_at,
            )
        except (Exception, asyncio.CancelledError):
            await self._settle_migration_before_verification(
                migration_claim,
                RuntimeLeaseOutcome.FAILED_BEFORE_WRITE,
            )
            raise
        try:
            if lease.orchestration_run_id != run_binding["run_id"]:
                raise PluginExecutionError(
                    "committed generation lease Run binding changed",
                    code="PLUGIN_GENERATION_LEASE_RUN_BINDING_CONFLICT",
                )
            resolved = await asyncio.to_thread(
                self._lease_capability,
                lease,
                automation_id=automation_id,
            )
            if _service_target is not None:
                resolved = self._service_effect_capability(resolved, service_effect)
            else:
                resolved_metadata = resolved.get("_plugin_runtime")
                if (
                    isinstance(resolved_metadata, Mapping)
                    and resolved_metadata.get("runtime_model")
                    == PluginRuntimeModel.SERVICE_V2.value
                ):
                    resolved = self._service_contribution_capability(
                        resolved,
                        contribution_id=str(run_binding["contract_id"]),
                    )
            try:
                initial_scan_phase = resolve_scan_capability_phase(capability, params)
                resolved_scan_phase = resolve_scan_capability_phase(resolved, params)
                if initial_scan_phase != resolved_scan_phase:
                    raise ValueError("scan execution phase changed with the generation lease")
                resolved = apply_scan_execution_boundary(resolved, params)
            except ValueError as exc:
                raise PluginExecutionError(
                    "scan execution boundary is invalid",
                    code="SCAN_EXECUTION_BOUNDARY_INVALID",
                ) from exc
        except (Exception, asyncio.CancelledError):
            await self._release_generation_lease(
                lease,
                outcome=RuntimeLeaseOutcome.FAILED_BEFORE_WRITE,
            )
            await self._settle_migration_before_verification(
                migration_claim,
                RuntimeLeaseOutcome.FAILED_BEFORE_WRITE,
            )
            raise
        is_scan_preview = resolved_scan_phase == SCAN_PHASE_PREVIEW
        outcome = RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
        execution_state: dict[str, object] = {
            "process_launched": False,
            "started_mutating_call_count": None,
            "host_call_observations": (),
        }
        try:
            result = await self._execute_plugin(
                resolved,
                params,
                trusted_scheduler_context=trusted_scheduler_context,
                execution_state=execution_state,
                invocation_id=lease.lease_id,
                run_binding=run_binding,
                service_call_chain=_service_call_chain,
            )
            if is_scan_preview and execution_state["started_mutating_call_count"] != 0:
                if isinstance(execution_state["started_mutating_call_count"], int):
                    outcome = RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
                raise PluginExecutionError(
                    "scan preview write-boundary evidence is unavailable or nonzero",
                    code="SCAN_PREVIEW_WRITE_BOUNDARY_INVALID",
                )
            outcome = self._lease_outcome(
                resolved,
                result,
                process_launched=bool(execution_state["process_launched"]),
                started_mutating_call_count=execution_state["started_mutating_call_count"],
            )
            if outcome in {RuntimeLeaseOutcome.VERIFYING, RuntimeLeaseOutcome.SUCCEEDED}:
                raw_account_bindings = resolved["_plugin_runtime"].get("account_bindings")
                if not isinstance(raw_account_bindings, Mapping):
                    outcome = RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
                    raise PluginExecutionError(
                        "committed generation account bindings are invalid",
                        code="PLUGIN_GENERATION_METADATA_INVALID",
                    )
                account_ids: list[str] = []
                for raw_binding in raw_account_bindings.values():
                    values = raw_binding if isinstance(raw_binding, (list, tuple)) else (raw_binding,)
                    for value in values:
                        account_id = str(value or "").strip()
                        if not account_id or account_id in account_ids:
                            if account_id in account_ids:
                                continue
                            outcome = RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
                            raise PluginExecutionError(
                                "committed generation account binding is empty",
                                code="PLUGIN_GENERATION_METADATA_INVALID",
                            )
                        account_ids.append(account_id)
                result = GenerationBoundResult(
                    result,
                    verification=GenerationVerificationContext(
                        automation_id=lease.automation_id,
                        generation=lease.generation,
                        lease_id=lease.lease_id,
                        account_ids=tuple(account_ids),
                        account_bindings_sha256=lease.snapshot.account_bindings_sha256,
                        requires_write_verification=(outcome == RuntimeLeaseOutcome.VERIFYING),
                        started_mutating_call_count=execution_state[
                            "started_mutating_call_count"
                        ],
                        orchestration_run_id=run_binding["run_id"],
                        host_call_observations=tuple(
                            execution_state["host_call_observations"]
                        ),
                    ),
                )
            return result
        except asyncio.CancelledError:
            outcome = self._write_failure_outcome(
                resolved,
                process_launched=bool(execution_state["process_launched"]),
                started_mutating_call_count=execution_state["started_mutating_call_count"],
            )
            raise
        except Exception:
            if outcome is not RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN:
                outcome = self._write_failure_outcome(
                    resolved,
                    process_launched=bool(execution_state["process_launched"]),
                    started_mutating_call_count=execution_state["started_mutating_call_count"],
                )
            raise
        finally:
            # ``_execute_plugin`` observes the broker receipt in its own
            # finalizer before control reaches this persistence boundary.
            try:
                await self._release_generation_lease(lease, outcome=outcome)
            except Exception as release_error:
                if migration_claim is not None:
                    unknown_outcome = (
                        RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
                        if self._has_observed_started_write(execution_state)
                        else RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
                    )
                    try:
                        await self._settle_migration_before_verification(
                            migration_claim,
                            unknown_outcome,
                        )
                    except Exception:
                        pass
                if self._has_observed_started_write(execution_state):
                    return self._lease_release_unknown_result(release_error)
                raise
            try:
                await self._settle_migration_before_verification(
                    migration_claim,
                    outcome,
                )
            except Exception as settlement_error:
                if self._has_observed_started_write(execution_state):
                    return self._lease_release_unknown_result(settlement_error)
                raise PluginExecutionError(
                    "migration run-key settlement is unavailable",
                    code="PLUGIN_MIGRATION_SETTLEMENT_UNAVAILABLE",
                ) from settlement_error

    @staticmethod
    def _service_result_has_evidence(
        result: Mapping[str, Any],
        *,
        service: str,
        operation: str,
        effect: CapabilityEffect,
        verification: GenerationVerificationContext,
    ) -> bool:
        write = effect in {
            CapabilityEffect.INTERNAL_WRITE,
            CapabilityEffect.EXTERNAL_WRITE,
            CapabilityEffect.DESTRUCTIVE,
        }
        data = result.get("data")
        meta = result.get("meta")
        evidence = data.get("evidence") if isinstance(data, Mapping) else None
        refs = meta.get("evidence_refs") if isinstance(meta, Mapping) else None
        observed_at = meta.get("observed_at") if isinstance(meta, Mapping) else None
        try:
            parsed_at = datetime.fromisoformat(
                str(observed_at or "").replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if (
            str(result.get("status") or "").upper() != "SUCCESS"
            or not isinstance(evidence, Mapping)
            or evidence.get("service") != service
            or evidence.get("operation") != operation
            or not str(evidence.get("outcome") or "").strip()
            or not isinstance(refs, list)
            or not refs
            or any(not isinstance(item, str) or not item.strip() for item in refs)
            or len(refs) != len(set(refs))
            or parsed_at.tzinfo is None
        ):
            return False
        if write and (
            str(evidence.get("outcome") or "").upper() != "WRITE_VERIFIED"
            or str(meta.get("write_outcome") or "").upper() != "WRITE_VERIFIED"
        ):
            return False
        if write:
            host_refs = [
                observation.get("evidence_ref")
                for observation in verification.host_call_observations
                if isinstance(observation, Mapping)
                and isinstance(observation.get("evidence_ref"), str)
                and observation.get("evidence_ref", "").strip()
            ]
            if (
                len(host_refs) != len(verification.host_call_observations)
                or refs != host_refs
            ):
                return False
        return True

    async def _finalize_service_write(
        self,
        verification: GenerationVerificationContext,
        *,
        result: Mapping[str, Any],
        outcome: RuntimeLeaseOutcome,
    ) -> None:
        repository = self._generation_leases
        if repository is None:
            raise PluginExecutionError(
                "service Provider write verifier is unavailable",
                code="WRITE_OUTCOME_UNKNOWN",
            )
        evidence_sha256 = hashlib.sha256(
            canonical_json_bytes(dict(result))
        ).hexdigest()
        try:
            await asyncio.to_thread(
                repository.finalize_generation_write,
                automation_id=verification.automation_id,
                generation=verification.generation,
                lease_id=verification.lease_id,
                outcome=outcome,
                evidence_sha256=evidence_sha256,
            )
        except Exception as exc:
            raise PluginExecutionError(
                "service Provider write finalization could not be persisted",
                code="WRITE_OUTCOME_UNKNOWN",
            ) from exc
        coordinator = self._migration_runtime
        if coordinator is None or verification.orchestration_run_id is None:
            return
        try:
            claim = await asyncio.to_thread(
                coordinator.find_claim_for_execution,
                verification.automation_id,
                verification.orchestration_run_id,
            )
            await asyncio.to_thread(
                coordinator.settle_after_write_verification,
                claim,
                outcome.value,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise PluginExecutionError(
                "service Provider migration write settlement is unavailable",
                code="PLUGIN_MIGRATION_SETTLEMENT_UNAVAILABLE",
            ) from exc

    async def execute_service_operation(
        self,
        capability: Mapping[str, Any],
        arguments: Mapping[str, Any],
        *,
        service: str,
        operation: str,
        effect: CapabilityEffect,
        call_chain: tuple[str, ...],
    ) -> Mapping[str, Any]:
        """Execute an internal Provider through the normal lease and sandbox path."""

        result = await self.execute(
            capability,
            arguments,
            _service_target={
                "service": service,
                "operation": operation,
                "effect": effect.value,
            },
            _service_call_chain=call_chain,
        )
        verification = getattr(result, "generation_verification", None)
        if not isinstance(verification, GenerationVerificationContext):
            if str(result.get("status") or "").upper() == "SUCCESS":
                raise PluginExecutionError(
                    "service Provider success lacks generation evidence",
                    code="SERVICE_PROVIDER_EVIDENCE_INVALID",
                )
            return dict(result)
        write = effect in {
            CapabilityEffect.INTERNAL_WRITE,
            CapabilityEffect.EXTERNAL_WRITE,
            CapabilityEffect.DESTRUCTIVE,
        }
        if write != (verification.requires_write_verification is True):
            raise PluginExecutionError(
                "service Provider lease effect changed during execution",
                code="SERVICE_PROVIDER_EVIDENCE_INVALID",
            )
        evidence_valid = self._service_result_has_evidence(
            result,
            service=service,
            operation=operation,
            effect=effect,
            verification=verification,
        )
        if not write:
            if verification.started_mutating_call_count != 0 or not evidence_valid:
                raise PluginExecutionError(
                    "service Provider read evidence is invalid",
                    code="SERVICE_PROVIDER_EVIDENCE_INVALID",
                )
            return dict(result)

        final_outcome = (
            RuntimeLeaseOutcome.WRITE_VERIFIED
            if evidence_valid
            else RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
        )
        await self._finalize_service_write(
            verification,
            result=result,
            outcome=final_outcome,
        )
        if not evidence_valid:
            raise PluginExecutionError(
                "service Provider write evidence is incomplete",
                code="WRITE_OUTCOME_UNKNOWN",
            )
        return dict(result)

    async def _execute_plugin(
        self,
        capability: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        trusted_scheduler_context: Mapping[str, object] | None = None,
        execution_state: dict[str, object] | None = None,
        invocation_id: str,
        run_binding: Mapping[str, Any],
        service_call_chain: tuple[str, ...] = (),
    ) -> Mapping[str, Any]:
        metadata = capability.get("_plugin_runtime")
        if not isinstance(metadata, Mapping):
            raise PluginExecutionError("resolved generation is not a plugin capability")
        runtime = metadata.get("runtime")
        if not isinstance(runtime, Mapping):
            raise PluginExecutionError("plugin runtime metadata is missing")
        runtime_kind = runtime.get("kind")
        trust_source = str(metadata.get("trust_source") or "")
        runtime_model = str(
            metadata.get("runtime_model") or PluginRuntimeModel.ACTION_V1.value
        )
        if runtime_model not in {
            PluginRuntimeModel.ACTION_V1.value,
            PluginRuntimeModel.SERVICE_V2.value,
        }:
            raise PluginExecutionError(
                "plugin runtime model is unsupported",
                code="PLUGIN_RUNTIME_MODEL_INVALID",
            )
        if runtime_kind != "python_subprocess":
            raise PluginExecutionError(
                "plugin actions must execute from their signed subprocess payload",
                code="PLUGIN_RUNTIME_FORBIDDEN",
            )
        allowed_trust_sources = (
            {"super_admin_upload", "builtin_bundle"}
            if runtime_model == PluginRuntimeModel.SERVICE_V2.value
            else {"ed25519_upload", "ed25519_first_party"}
        )
        if trust_source not in allowed_trust_sources:
            raise PluginExecutionError(
                "plugin subprocess trust source does not match its runtime model",
                code="PLUGIN_TRUST_SOURCE_INVALID",
            )
        self._reject_sensitive_arguments(params)
        (
            automation_id,
            plugin_id,
            version,
            root,
            python_relative,
            entry_relative,
        ) = await asyncio.to_thread(
            self._validated_plugin_launch_paths,
            metadata,
            runtime,
        )
        timeout = max(1, min(int(capability.get("timeout") or 60), 3600))
        account_roles = metadata.get("account_roles")
        resource_roles = metadata.get("resource_roles")
        account_bindings = metadata.get("account_bindings")
        resource_bindings = metadata.get("resource_bindings")
        runtime_permissions = metadata.get("runtime_permissions")
        if (
            not isinstance(account_roles, list)
            or not isinstance(resource_roles, list)
            or not isinstance(account_bindings, Mapping)
            or not isinstance(resource_bindings, Mapping)
            or not isinstance(runtime_permissions, Mapping)
        ):
            raise PluginExecutionError("plugin capability declaration is incomplete")
        issued_runtime_permissions = copy.deepcopy(dict(runtime_permissions))
        if runtime_model == PluginRuntimeModel.SERVICE_V2.value:
            raw_service_governance = run_binding.get("service_governance")
            if raw_service_governance is None:
                compiled_invocations = metadata.get("compiled_invocations")
                compiled = (
                    compiled_invocations.get(run_binding.get("contract_id"))
                    if isinstance(compiled_invocations, Mapping)
                    else None
                )
                raw_service_governance = (
                    compiled.get("governance")
                    if isinstance(compiled, Mapping)
                    else None
                )
            exact_service_governance = self._validated_service_governance(
                raw_service_governance
            )
            issued_runtime_permissions["_service_effect_ceiling"] = str(
                exact_service_governance["effect"]
            )
        if service_call_chain:
            issued_runtime_permissions["_service_call_chain"] = list(
                service_call_chain
            )
        token = self._issuer.issue(
            automation_id=automation_id,
            plugin_version=version,
            tool_name=str(metadata.get("core_tool_name") or capability.get("name") or ""),
            ttl_seconds=timeout + 30,
            runtime_permissions=issued_runtime_permissions,
            account_roles=account_roles,
            resource_roles=resource_roles,
            account_bindings=copy.deepcopy(dict(account_bindings)),
            resource_bindings={str(key): str(value) for key, value in resource_bindings.items()},
            write_attempt_context={
                "automation_id": automation_id,
                # This comes from the committed generation snapshot and is
                # therefore stable across repeated installations of a plugin.
                "plugin_id": plugin_id,
                "generation": int(metadata.get("generation") or 0),
                "lease_id": invocation_id,
                "orchestration_run_id": run_binding["run_id"],
                "step_id": run_binding["step_id"],
            },
        )
        action_name = str(capability.get("name") or "")
        payload_contract: dict[str, Any] = {
            "schema_version": 1,
            "automation_id": automation_id,
            "plugin_id": plugin_id,
            "plugin_version": version,
            "arguments": dict(params),
        }
        if runtime_model == PluginRuntimeModel.SERVICE_V2.value:
            service_target = run_binding.get("service_target")
            direct_contribution = False
            if service_target is not None:
                target = service_target
                governance = run_binding.get("service_governance")
            else:
                compiled_invocations = metadata.get("compiled_invocations")
                if not isinstance(compiled_invocations, Mapping):
                    raise PluginExecutionError(
                        "service-v2 invocation table is missing",
                        code="PLUGIN_GENERATION_METADATA_INVALID",
                    )
                contribution_id = run_binding["contract_id"]
                compiled = compiled_invocations.get(contribution_id)
                direct_contribution = compiled is not None
                if compiled is None and run_binding["entrypoint"] == "scheduler":
                    candidates = [
                        item
                        for item in compiled_invocations.values()
                        if isinstance(item, Mapping)
                        and isinstance(item.get("target"), Mapping)
                        and item["target"].get("contribution_kind") == "scheduler"
                    ]
                    compiled = candidates[0] if len(candidates) == 1 else None
                target = (
                    compiled.get("target") if isinstance(compiled, Mapping) else None
                )
                governance = (
                    compiled.get("governance") if isinstance(compiled, Mapping) else None
                )
            if (
                not isinstance(target, Mapping)
                or set(target)
                != {
                    "service",
                    "operation",
                    "contribution_id",
                    "contribution_kind",
                }
                or not all(str(value or "").strip() for value in target.values())
                or str(target.get("contribution_kind"))
                != run_binding["entrypoint"]
                or (
                    direct_contribution
                    and str(target.get("contribution_id")) != contribution_id
                )
            ):
                raise PluginExecutionError(
                    "service-v2 invocation target is invalid",
                    code="PLUGIN_GENERATION_METADATA_INVALID",
                )
            exact_governance = self._validated_service_governance(governance)
            payload_contract.update(
                {
                    "schema_version": 2,
                    "runtime_model": PluginRuntimeModel.SERVICE_V2.value,
                    "entrypoint": run_binding["entrypoint"],
                    "target": copy.deepcopy(dict(target)),
                    "governance": exact_governance,
                }
            )
        payload = canonical_json_bytes(payload_contract)
        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[bytes] | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        launch_task: asyncio.Task[object] | None = None
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            try:
                launch_task = asyncio.create_task(
                    self._sandbox.launch(
                        install_root=root,
                        python_relative=python_relative.as_posix(),
                        entrypoint_relative=entry_relative.as_posix(),
                        environment=self._minimal_environment(
                            capability=token,
                            automation_id=automation_id,
                            plugin_id=plugin_id,
                            plugin_version=version,
                            broker_endpoint=self._issuer.broker_endpoint,
                            broker_call_timeout_seconds=timeout,
                        ),
                        broker_socket_path=self._issuer.broker_socket_path,
                    ),
                )
                launched = await asyncio.shield(launch_task)
            except asyncio.CancelledError:
                if launch_task is not None:
                    try:
                        launched = await asyncio.shield(launch_task)
                    except Exception:
                        launched = None
                    if isinstance(launched, asyncio.subprocess.Process):
                        if execution_state is not None:
                            execution_state["process_launched"] = True
                        await self._terminate(launched)
                raise
            except Exception:
                return {
                    "success": False,
                    "error": "plugin sandbox could not start safely",
                    "error_code": "PLUGIN_SANDBOX_START_FAILED",
                    "retryable": False,
                }
            if not isinstance(launched, asyncio.subprocess.Process):
                return {
                    "success": False,
                    "error": "plugin sandbox could not start safely",
                    "error_code": "PLUGIN_SANDBOX_START_FAILED",
                    "retryable": False,
                }
            proc = launched
            if execution_state is not None:
                execution_state["process_launched"] = True
            self._running[invocation_id] = {
                "proc": proc,
                "started_at": started_at,
                "core_tool_name": "",
                "action_name": action_name,
                "automation_id": automation_id,
                "generation": metadata.get("generation"),
                **dict(run_binding),
            }
            if proc.stdin is None or proc.stdout is None or proc.stderr is None:
                await self._terminate(proc)
                await self._collect_bounded_process_output(proc)
                return {
                    "success": False,
                    "error": "plugin sandbox could not start safely",
                    "error_code": "PLUGIN_SANDBOX_START_FAILED",
                    "retryable": False,
                }
            stdout_task = asyncio.create_task(self._read_limited(proc.stdout, MAX_PLUGIN_OUTPUT_BYTES))
            stderr_task = asyncio.create_task(self._read_limited(proc.stderr, MAX_PLUGIN_STDERR_BYTES))
            deadline = asyncio.get_running_loop().time() + timeout
            try:
                async def _send_stdin() -> None:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                    proc.stdin.close()

                await self._await_with_timeout(_send_stdin(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._terminate(proc)
                await self._cancel_and_reap_tasks(stdout_task, stderr_task)
                return {
                    "success": False,
                    "error": "plugin sandbox did not accept its request before the execution deadline",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_EXECUTION_LIMIT", token, execution_state
                    ),
                    "retryable": False,
                }
            except asyncio.CancelledError:
                await self._terminate(proc)
                await self._cancel_and_reap_tasks(stdout_task, stderr_task)
                raise
            except Exception:
                await self._terminate(proc)
                await self._cancel_and_reap_tasks(stdout_task, stderr_task)
                return {
                    "success": False,
                    "error": "plugin sandbox could not accept its request",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_SANDBOX_START_FAILED", token, execution_state
                    ),
                    "retryable": False,
                }
            try:
                stdout, stderr, _ = await self._await_with_timeout(
                    asyncio.gather(stdout_task, stderr_task, proc.wait()),
                    timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                )
            except asyncio.CancelledError:
                await self._terminate(proc)
                await self._cancel_and_reap_tasks(stdout_task, stderr_task)
                raise
            except (asyncio.TimeoutError, PluginExecutionError):
                await self._terminate(proc)
                await self._cancel_and_reap_tasks(stdout_task, stderr_task)
                return {
                    "success": False,
                    "error": "plugin execution timed out or exceeded its output limit",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_EXECUTION_LIMIT", token, execution_state
                    ),
                    "retryable": False,
                }
            if proc.returncode != 0:
                return {
                    "success": False,
                    "error": redact_text(stderr.decode("utf-8", errors="replace"))[-500:],
                    "error_code": self._failure_code(
                        capability, "PLUGIN_PROCESS_FAILED", token, execution_state
                    ),
                    "retryable": False,
                }
            try:
                result = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {
                    "success": False,
                    "error": "plugin output is not one UTF-8 JSON value",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_OUTPUT_INVALID", token, execution_state
                    ),
                    "retryable": False,
                }
            if not isinstance(result, dict):
                return {
                    "success": False,
                    "error": "plugin output must be a JSON object",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_OUTPUT_INVALID", token, execution_state
                    ),
                    "retryable": False,
                }
            output_schema = capability.get("output_schema")
            if not isinstance(output_schema, Mapping):
                return {
                    "success": False,
                    "error": "signed plugin output schema is missing",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_OUTPUT_INVALID", token, execution_state
                    ),
                    "retryable": False,
                }
            try:
                validate_schema_instance(
                    f"{action_name} output",
                    result,
                    output_schema,
                )
            except (KeyError, TypeError, ValueError):
                return {
                    "success": False,
                    "error": "plugin output does not match its signed schema",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_OUTPUT_INVALID", token, execution_state
                    ),
                    "retryable": False,
                }
            required_result_fields = {"status", "data", "meta", "warnings", "error"}
            if not required_result_fields <= set(result):
                return {
                    "success": False,
                    "error": "plugin output does not use the unified result contract",
                    "error_code": self._failure_code(
                        capability, "PLUGIN_OUTPUT_INVALID", token, execution_state
                    ),
                    "retryable": False,
                }
            if str(result.get("status") or "").upper() != "SUCCESS":
                return self._govern_started_write_failure(
                    capability,
                    result,
                    token=token,
                    execution_state=execution_state,
                )
            return result
        finally:
            self._running.pop(invocation_id, None)
            if execution_state is not None:
                self._observe_started_mutating_calls(token, execution_state)
                self._observe_host_call_observations(token, execution_state)
            self._issuer.revoke(token)

    def running_tool_info(self, tool_name: str) -> dict[str, Any]:
        matches = [
            (invocation_id, current)
            for invocation_id, current in self._running.items()
            if current.get("action_name") == tool_name
            and self._is_live_plugin_invocation(current)
        ]
        if len(matches) > 1:
            return {
                "running": True,
                "ambiguous": True,
                "active_invocations": len(matches),
                "started_at": "",
                "cancel_requested": False,
            }
        if len(matches) == 1:
            invocation_id, current = matches[0]
            proc = current.get("proc")
            core_tool_name = str(current.get("core_tool_name") or "")
            if core_tool_name:
                info = dict(self._core.running_tool_info(core_tool_name))
                info.setdefault("started_at", str(current.get("started_at") or ""))
                info["invocation_id"] = invocation_id
                return info
            return {
                "running": bool(proc is not None and proc.returncode is None),
                "started_at": f"invocation:{invocation_id}",
                "invocation_id": invocation_id,
                "run_id": str(current.get("run_id") or ""),
                "step_id": str(current.get("step_id") or ""),
                "cancel_requested": False,
            }
        return self._core.running_tool_info(tool_name)

    def get_running_output(
        self,
        tool_name: str,
        offset: int = 0,
        started_at: str = "",
    ) -> dict[str, Any]:
        """Expose a ToolExecutor-compatible, payload-free plugin status view."""

        matches = [
            (invocation_id, current)
            for invocation_id, current in self._running.items()
            if current.get("action_name") == tool_name
        ]
        if started_at.startswith("invocation:"):
            requested = started_at.removeprefix("invocation:")
            matches = [item for item in matches if item[0] == requested]
        else:
            matches = [
                item
                for item in matches
                if self._is_live_plugin_invocation(item[1])
            ]
        if not matches:
            return dict(
                self._core.get_running_output(
                    tool_name,
                    offset=offset,
                    started_at=started_at,
                )
            )
        if len(matches) > 1:
            return {
                "lines": [],
                "running": True,
                "ambiguous": True,
                "active_invocations": len(matches),
                "offset": offset,
                "total": 0,
                "started_at": "",
                "cancel_requested": False,
            }
        invocation_id, current = matches[0]
        core_tool_name = str(current.get("core_tool_name") or "")
        if core_tool_name:
            return dict(
                self._core.get_running_output(
                    core_tool_name,
                    offset=offset,
                    started_at=started_at,
                )
            )
        proc = current.get("proc")
        return {
            "lines": [],
            "running": bool(proc is not None and proc.returncode is None),
            "offset": offset,
            "total": 0,
            "started_at": f"invocation:{invocation_id}",
            "invocation_id": invocation_id,
            "run_id": str(current.get("run_id") or ""),
            "step_id": str(current.get("step_id") or ""),
            "cancel_requested": False,
        }

    def is_tool_running(self, tool_name: str) -> bool:
        return bool(self.running_tool_info(tool_name).get("running"))

    def running_tools(self) -> list[str]:
        plugin_tools = {
            str(current.get("action_name") or "")
            for current in self._running.values()
            if current.get("action_name")
            and self._is_live_plugin_invocation(current)
        }
        return sorted(plugin_tools | {str(name) for name in self._core.running_tools()})

    def last_tool_info(self) -> dict[str, Any] | None:
        return self._core.last_tool_info()

    def heavy_lock_held(self) -> bool:
        core_held = bool(self._core.heavy_lock_held())
        plugin_running = any(
            self._is_live_plugin_invocation(current)
            for current in self._running.values()
        )
        return plugin_running or core_held

    async def cancel_tool(self, tool_name: str, started_at: str = "") -> Mapping[str, Any]:
        matches = [
            (invocation_id, current)
            for invocation_id, current in self._running.items()
            if current.get("action_name") == tool_name
        ]
        if started_at.startswith("invocation:"):
            requested = started_at.removeprefix("invocation:")
            matches = [item for item in matches if item[0] == requested]
        if not matches:
            return await self._core.cancel_tool(tool_name, started_at=started_at)
        if len(matches) != 1:
            return {
                "ok": False,
                "code": "AMBIGUOUS_PLUGIN_INVOCATION",
                "message": "multiple plugin invocations require an exact trusted run binding",
            }
        _, current = matches[0]
        core_tool_name = str(current.get("core_tool_name") or "")
        if core_tool_name:
            return await self._core.cancel_tool(core_tool_name, started_at=started_at)
        proc = current.get("proc")
        if proc is None or proc.returncode is not None:
            return {"ok": False, "code": "NOT_RUNNING", "message": "plugin is not running"}
        await self._terminate(proc)
        return {"ok": True, "started_at": current.get("started_at"), "message": "plugin cancelled"}

    async def cancel_bound_run(
        self,
        *,
        tool_name: str,
        run_id: str,
        step_id: str,
    ) -> Mapping[str, Any]:
        """Cancel only the invocation bound by the trusted orchestration Run."""

        matches = [
            (invocation_id, current)
            for invocation_id, current in self._running.items()
            if current.get("action_name") == tool_name
            and current.get("run_id") == run_id
            and current.get("step_id") == step_id
        ]
        if not matches:
            return await self._core.cancel_bound_run(
                tool_name=tool_name,
                run_id=run_id,
                step_id=step_id,
            )
        if len(matches) != 1:
            return {
                "ok": False,
                "code": "AMBIGUOUS_PLUGIN_INVOCATION",
                "message": "trusted plugin Run binding did not resolve exactly one process",
            }
        invocation_id, _ = matches[0]
        return await self.cancel_tool(tool_name, started_at=f"invocation:{invocation_id}")
