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
from typing import Any, Callable, Mapping

from agent.automation_plugins.errors import PluginExecutionError
from agent.automation_plugins.manifest import canonical_json_bytes
from agent.automation_plugins.models import (
    GenerationBoundResult,
    GenerationVerificationContext,
    RuntimeGenerationLease,
    RuntimeLeaseOutcome,
)
from agent.automation_plugins.ports import (
    ExecutionCapabilityIssuerPort,
    PluginIntegrityVerifierPort,
    PluginSandboxLauncherPort,
    RuntimeGenerationLeasePort,
)
from agent.automation_plugins.sandbox import FailClosedPluginSandbox
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
_PREWRITE_SESSION_FAILURE_CODES = frozenset({"AUTH_REQUIRED", "AUTH_PENDING_CODE"})


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
        release_hold_provider: Callable[[], bool] | None = None,
        allow_development_builtin: bool = False,
    ) -> None:
        self._core = core_executor
        self._issuer = capability_issuer
        self._integrity = integrity_verifier or FilesystemPluginIntegrityVerifier()
        self._sandbox = sandbox_launcher or FailClosedPluginSandbox()
        self._allow_development_builtin = bool(allow_development_builtin)
        self._generation_leases = generation_leases
        # A production caller must explicitly prove that release activation
        # completed.  Missing or failing hold state is intentionally held.
        self._release_hold_provider = release_hold_provider or (lambda: True)
        self._running: dict[str, dict[str, Any]] = {}

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
        environment = {
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",
            "PYTHON_DOTENV_DISABLED": "1",
            "BOYI_PLUGIN_EXECUTION_CAPABILITY": capability,
            "BOYI_AUTOMATION_ID": automation_id,
            "BOYI_PLUGIN_ID": plugin_id,
            "BOYI_PLUGIN_VERSION": plugin_version,
            "BOYI_PLUGIN_BROKER_ENDPOINT": broker_endpoint,
            "BOYI_PLUGIN_BROKER_CALL_TIMEOUT": str(broker_call_timeout_seconds),
        }
        for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TZ"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment

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
        return result

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
    def _lease_outcome(
        capability: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        process_launched: bool,
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
                return RuntimeLeaseOutcome.VERIFYING
            return RuntimeLeaseOutcome.SUCCEEDED
        if error_code in _PREWRITE_SESSION_FAILURE_CODES:
            return RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
        if capability.get("operation_type") in _WRITE_TYPES and process_launched:
            return RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
        return RuntimeLeaseOutcome.FAILED_BEFORE_WRITE

    async def execute(
        self,
        capability: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        trusted_scheduler_context: Mapping[str, object] | None = None,
        trusted_invocation_context: Mapping[str, object] | None = None,
    ) -> Mapping[str, Any]:
        initial_metadata = capability.get("_plugin_runtime")
        if not isinstance(initial_metadata, Mapping):
            return await self._core.execute(
                dict(capability),
                dict(params),
                trusted_scheduler_context=trusted_scheduler_context,
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
            release_held = self._release_hold_provider()
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
        run_binding = self._run_binding(
            trusted_invocation_context,
            automation_id=automation_id,
            generation=raw_generation,
        )
        timeout = max(1, min(int(capability.get("timeout") or 60), 3600))
        lease = self._generation_leases.acquire_committed_generation(
            automation_id,
            expected_generation=raw_generation,
            expected_manifest_sha256=str(initial_metadata.get("manifest_sha256") or ""),
            lease_id=str(uuid.uuid4()),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=timeout + 60),
        )
        resolved = self._lease_capability(lease, automation_id=automation_id)
        outcome = RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
        execution_state = {"process_launched": False}
        try:
            result = await self._execute_plugin(
                resolved,
                params,
                trusted_scheduler_context=trusted_scheduler_context,
                execution_state=execution_state,
                invocation_id=lease.lease_id,
                run_binding=run_binding,
            )
            outcome = self._lease_outcome(
                resolved,
                result,
                process_launched=execution_state["process_launched"],
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
                    ),
                )
            return result
        except Exception:
            # Any subprocess write that passed launch reports its uncertainty
            # as a normal result. Exceptions here are pre-write/control-plane
            # failures and therefore safe to record as FAILED_BEFORE_WRITE.
            outcome = (
                RuntimeLeaseOutcome.WRITE_OUTCOME_UNKNOWN
                if resolved.get("operation_type") in _WRITE_TYPES
                and execution_state["process_launched"]
                else RuntimeLeaseOutcome.FAILED_BEFORE_WRITE
            )
            raise
        finally:
            self._generation_leases.release_generation(lease, outcome=outcome)

    async def _execute_plugin(
        self,
        capability: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        trusted_scheduler_context: Mapping[str, object] | None = None,
        execution_state: dict[str, bool] | None = None,
        invocation_id: str,
        run_binding: Mapping[str, str],
    ) -> Mapping[str, Any]:
        metadata = capability.get("_plugin_runtime")
        if not isinstance(metadata, Mapping):
            raise PluginExecutionError("resolved generation is not a plugin capability")
        runtime = metadata.get("runtime")
        if not isinstance(runtime, Mapping):
            raise PluginExecutionError("plugin runtime metadata is missing")
        runtime_kind = runtime.get("kind")
        trust_source = str(metadata.get("trust_source") or "")
        if runtime_kind != "python_subprocess":
            raise PluginExecutionError(
                "plugin actions must execute from their signed subprocess payload",
                code="PLUGIN_RUNTIME_FORBIDDEN",
            )
        if trust_source not in {"ed25519_upload", "ed25519_first_party"}:
            raise PluginExecutionError(
                "plugin subprocess is not backed by an Ed25519 package",
                code="PLUGIN_TRUST_SOURCE_INVALID",
            )
        self._reject_sensitive_arguments(params)
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
        token = self._issuer.issue(
            automation_id=automation_id,
            plugin_version=version,
            tool_name=str(metadata.get("core_tool_name") or capability.get("name") or ""),
            ttl_seconds=timeout + 30,
            runtime_permissions=runtime_permissions,
            account_roles=account_roles,
            resource_roles=resource_roles,
            account_bindings=copy.deepcopy(dict(account_bindings)),
            resource_bindings={str(key): str(value) for key, value in resource_bindings.items()},
        )
        action_name = str(capability.get("name") or "")
        payload = canonical_json_bytes(
            {
                "schema_version": 1,
                "automation_id": automation_id,
                "plugin_id": plugin_id,
                "plugin_version": version,
                "arguments": dict(params),
            }
        )
        proc: asyncio.subprocess.Process | None = None
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            launched = await self._sandbox.launch(
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
            )
            if not isinstance(launched, asyncio.subprocess.Process):
                raise PluginExecutionError("sandbox launcher returned an invalid process")
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
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
            stdout_task = asyncio.create_task(self._read_limited(proc.stdout, MAX_PLUGIN_OUTPUT_BYTES))
            stderr_task = asyncio.create_task(self._read_limited(proc.stderr, MAX_PLUGIN_STDERR_BYTES))
            try:
                stdout, stderr, _ = await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, proc.wait()),
                    timeout=timeout,
                )
            except (asyncio.TimeoutError, PluginExecutionError):
                await self._terminate(proc)
                for task in (stdout_task, stderr_task):
                    if not task.done():
                        task.cancel()
                unknown_write = capability.get("operation_type") in _WRITE_TYPES
                return {
                    "success": False,
                    "error": "plugin execution timed out or exceeded its output limit",
                    "error_code": "WRITE_OUTCOME_UNKNOWN" if unknown_write else "PLUGIN_EXECUTION_LIMIT",
                    "retryable": False,
                }
            if proc.returncode != 0:
                unknown_write = capability.get("operation_type") in _WRITE_TYPES
                return {
                    "success": False,
                    "error": redact_text(stderr.decode("utf-8", errors="replace"))[-500:],
                    "error_code": "WRITE_OUTCOME_UNKNOWN" if unknown_write else "PLUGIN_PROCESS_FAILED",
                    "retryable": False,
                }
            try:
                result = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                unknown_write = capability.get("operation_type") in _WRITE_TYPES
                return {
                    "success": False,
                    "error": "plugin output is not one UTF-8 JSON value",
                    "error_code": "WRITE_OUTCOME_UNKNOWN" if unknown_write else "PLUGIN_OUTPUT_INVALID",
                    "retryable": False,
                }
            if not isinstance(result, dict):
                unknown_write = capability.get("operation_type") in _WRITE_TYPES
                return {
                    "success": False,
                    "error": "plugin output must be a JSON object",
                    "error_code": "WRITE_OUTCOME_UNKNOWN" if unknown_write else "PLUGIN_OUTPUT_INVALID",
                    "retryable": False,
                }
            output_schema = capability.get("output_schema")
            if not isinstance(output_schema, Mapping):
                unknown_write = capability.get("operation_type") in _WRITE_TYPES
                return {
                    "success": False,
                    "error": "signed plugin output schema is missing",
                    "error_code": "WRITE_OUTCOME_UNKNOWN" if unknown_write else "PLUGIN_OUTPUT_INVALID",
                    "retryable": False,
                }
            try:
                validate_schema_instance(
                    f"{action_name} output",
                    result,
                    output_schema,
                )
            except (KeyError, TypeError, ValueError):
                unknown_write = capability.get("operation_type") in _WRITE_TYPES
                return {
                    "success": False,
                    "error": "plugin output does not match its signed schema",
                    "error_code": "WRITE_OUTCOME_UNKNOWN" if unknown_write else "PLUGIN_OUTPUT_INVALID",
                    "retryable": False,
                }
            required_result_fields = {"status", "data", "meta", "warnings", "error"}
            if not required_result_fields <= set(result):
                unknown_write = capability.get("operation_type") in _WRITE_TYPES
                return {
                    "success": False,
                    "error": "plugin output does not use the unified result contract",
                    "error_code": "WRITE_OUTCOME_UNKNOWN" if unknown_write else "PLUGIN_OUTPUT_INVALID",
                    "retryable": False,
                }
            if str(result.get("status") or "").upper() != "SUCCESS":
                return result
            return result
        finally:
            self._running.pop(invocation_id, None)
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
        if len(matches) != 1:
            return {
                "ok": False,
                "code": "NOT_RUNNING" if not matches else "AMBIGUOUS_PLUGIN_INVOCATION",
                "message": "trusted plugin Run binding did not resolve exactly one process",
            }
        invocation_id, _ = matches[0]
        return await self.cancel_tool(tool_name, started_at=f"invocation:{invocation_id}")
