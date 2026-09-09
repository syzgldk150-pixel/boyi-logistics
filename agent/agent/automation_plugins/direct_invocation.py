"""Immediate, process-owned plugin calls. Stored rows are facts, never jobs."""
from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from agent.automation_plugins.code_owned_fields import apply_scan_execution_boundary, apply_selection_execution_boundary
from agent.orchestration.execution_resources import canonical_resource_write_locks, execution_keys_conflict, EXECUTION_ACTION_SCOPES
from agent.orchestration.models import OperationType, OrchestrationError
from shared.automation_project_authorization import canonical_sha256
from shared.execution_resource_journal import EXECUTION_RESOURCE_KEYS
from shared.plugin_invocation_repository import TERMINAL_INVOCATION_STATUSES
from shared.redaction import redact_text


@dataclass(frozen=True)
class InvocationVerificationRequest:
    """Verifier's business contract; deliberately has no Run or Step ID."""
    tool_name: str
    arguments: Mapping[str, Any]
    operation_type: OperationType
    account_id: str | None = None


def public_invocation(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        key: row.get(key) for key in (
            "invocation_id", "request_id", "automation_id", "plugin_id", "plugin_version",
            "generation", "operation", "status", "error_code", "error_summary",
            "started_at", "finished_at",
        )
    }
    for key in ("started_at", "finished_at"):
        if isinstance(result[key], datetime):
            value = result[key]
            result[key] = value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()
    result["success"] = row.get("status") == "COMPLETED"
    result["result"] = row.get("result_json")
    result["output"] = (row.get("result_json") or {}).get("data")
    result["error"] = ({"code": row.get("error_code"), "message": row.get("error_summary")} if row.get("error_code") else None)
    result["running"] = row.get("status") in {"STARTING", "RUNNING", "CANCELLING"}
    result["status_url"] = f"/internal/v1/automation-invocations/{row['invocation_id']}"
    return result


async def _preflight_thread(function, *args, **kwargs):
    """Drain non-cancellable setup work, then stop before starting business work."""
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await asyncio.gather(work, return_exceptions=True)
        raise


class DirectPluginInvocationService:
    def __init__(self, repository: Any, executor: Any, verifier: Any, *, max_concurrency: int = 16, release_hold_provider=None, saved_resource_provider=None, account_validator=None, prepare_arguments=None, publish_result=None) -> None:
        self.repository = repository
        self.executor = executor
        self.executor.direct_invocations = self
        self.verifier = verifier
        self.owner_id = str(uuid.uuid4())
        self.max_concurrency = max(1, int(max_concurrency))
        self._hold = release_hold_provider or (lambda: True)
        self._resources = saved_resource_provider
        self._account_validator = account_validator
        self._prepare_arguments = prepare_arguments
        self._publish_result = publish_result
        self._lock = threading.RLock()
        self._active: dict[str, dict] = {}
        self._active_reads: dict[str, dict] = {}
        self._held_operations: dict[str, tuple] = {}
        self._operation_changed = asyncio.Event()
        issuer = getattr(executor, "_issuer", None)
        if issuer is not None:
            issuer.host_operation_guard = self.host_operation
        self._changing_accounts: set[str] = set()
        self._closed = False
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        await _preflight_thread(self.repository.close_interrupted, self.owner_id)

    def get(self, invocation_id: str) -> dict:
        row = self.repository.get(invocation_id)
        if row is None:
            raise OrchestrationError("INVOCATION_NOT_FOUND", "没有找到本次执行记录")
        result = public_invocation(row)
        if row["status"] in TERMINAL_INVOCATION_STATUSES:
            from shared.collector_navigation import collector_invocation_navigation
            with self.repository._repository.unit_of_work() as uow:
                result["collector_navigation"] = collector_invocation_navigation(uow.connection, invocation_id)
        return result

    def list_recent(self, automation_id: str, *, limit: int = 30) -> list[dict]:
        return [public_invocation(row) for row in self.repository.list_recent(automation_id, limit=limit)]

    def active_invocations(self) -> list[dict]:
        with self._lock:
            return [{"invocation_id": key, "automation_id": value["automation_id"], "operation": value["operation"]} for key, value in self._active.items()]

    def active_read_count(self) -> int:
        with self._lock:
            return len(self._active_reads)

    async def call_read(self, *, operation: str, handler, account_ids: tuple[str, ...] = ()):
        """One immediate read, with drain/account ownership but no stored record."""
        token = uuid.uuid4().hex
        with self._lock:
            if self._closed or self._hold() is not False:
                raise OrchestrationError("PLUGIN_RELEASE_HELD", "系统正在更新，请稍后重新查询")
            if set(account_ids) & self._changing_accounts:
                raise OrchestrationError("ACCOUNT_EXECUTION_BUSY", "所需账号正在更新，请稍后重新查询")
            task = asyncio.create_task(handler())
            self._active_reads[token] = {"task": task, "account_ids": frozenset(account_ids), "operation": operation}
        try:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # HTTP disconnect/caller cancellation cannot stop to_thread.
                # Keep the read alive and counted until the actual port ends.
                await asyncio.gather(task, return_exceptions=True)
                raise
        finally:
            with self._lock:
                self._active_reads.pop(token, None)

    def begin_credentials_change(self, account_id: str):
        """Serialize a credential mutation with actual active account users."""
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("credential change account is required")
        with self._lock:
            if account_id in self._changing_accounts or any(account_id in item["account_ids"] for item in (*self._active.values(), *self._active_reads.values())):
                raise OrchestrationError("ACCOUNT_EXECUTION_BUSY", "该账号正在执行，请待本次结束后修改")
            self._changing_accounts.add(account_id)
        def release():
            with self._lock:
                self._changing_accounts.discard(account_id)
        return release

    def reserve_provider(self, invocation_id: str, capability: Mapping) -> tuple:
        """Add a signed Provider's actual scopes to its current call, never enqueue."""
        metadata = capability["_plugin_runtime"]
        accounts = {str(value) for raw in metadata["account_bindings"].values() for value in (raw if isinstance(raw, (tuple, list)) else (raw,))}
        write = capability.get("operation_type") not in {"read", "compute"}
        scopes = {}
        keys, bounded = canonical_resource_write_locks(capability, accounts, self._resources, action_scopes=scopes) if write else (set(), True)
        if write and not bounded:
            keys.update(("account-write", account) for account in accounts)
        instance_keys = {("plugin-instance", metadata["automation_id"])}
        with self._lock:
            current = self._active.get(invocation_id)
            if current is None:
                raise OrchestrationError("INVOCATION_NOT_ACTIVE", "该调用已结束，不能继续调用插件服务")
            if accounts & self._changing_accounts or any(execution_keys_conflict(left, right) for call_id, item in self._active.items() if call_id != invocation_id for left in item["keys"] for right in instance_keys):
                raise OrchestrationError("EXECUTION_RESOURCE_BUSY", "插件服务所需资源正在使用，本次已结束")
            current["keys"] = tuple(set(current["keys"]) | instance_keys)
            current["account_ids"] = frozenset(set(current["account_ids"]) | accounts)
        return tuple(sorted(keys)), scopes

    @asynccontextmanager
    async def host_operation(self, prepared):
        """Coordinate the actual host mutation inside this live request only."""
        grant = prepared.grant
        identity = grant.write_attempt_context.get("invocation_id")
        if not identity or prepared.mark_write_started is None:
            yield
            return
        keys = grant.execution_action_scopes.get((prepared.operation, prepared.action, prepared.role), grant.execution_resource_keys)
        if not keys:
            # A signed unbounded host write has no narrower reviewed scope.
            keys = tuple(("account-write", account) for accounts in grant.account_bindings.values() for account in (accounts if isinstance(accounts, (tuple, list)) else (accounts,))) or (("unscoped-host-write",),)
        token = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(10.0, max(0.0, (grant.expires_at - datetime.now(timezone.utc)).total_seconds()))
        while True:
            with self._lock:
                active = self._active.get(identity)
                if active is None or active.get("cancel_requested"):
                    raise OrchestrationError("CANCELLED", "本次调用已停止，未发起新的写入")
                conflict = any(execution_keys_conflict(left, right) for owner, held in self._held_operations.values() if owner != identity for left in held for right in keys)
                if not conflict:
                    self._held_operations[token] = (identity, keys)
                    break
                self._operation_changed.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise OrchestrationError("EXECUTION_RESOURCE_BUSY", "本次写入未能及时取得资源，请重新触发")
            try:
                await asyncio.wait_for(self._operation_changed.wait(), remaining)
            except asyncio.TimeoutError as exc:
                raise OrchestrationError("EXECUTION_RESOURCE_BUSY", "本次写入未能及时取得资源，请重新触发") from exc
        try:
            yield
        finally:
            with self._lock:
                self._held_operations.pop(token, None)
                self._operation_changed.set()

    def _reserve(self, *, row: dict, keys: tuple, coroutine_factory, account_ids: frozenset = frozenset(), admission_guard=None) -> dict:
        with self._lock:
            replay = self.repository.by_request(row["request_key_sha256"])
            if replay is not None:
                if replay["request_sha256"] != row["request_sha256"]:
                    raise OrchestrationError("IDEMPOTENCY_CONFLICT", "同一请求不能更换参数")
                return public_invocation(replay)
            busy = bool(account_ids & self._changing_accounts) or len(self._active) >= self.max_concurrency or any(
                execution_keys_conflict(left, right)
                for active in self._active.values() for left in active["keys"] for right in keys
            )
            rejected = self._closed or self._hold() is not False or busy
            row["status"] = "FAILED" if rejected else "STARTING"
            stored = self.repository.create(row, admission_guard=admission_guard)
            if stored["invocation_id"] != row["invocation_id"]:
                return public_invocation(stored)
            if rejected:
                reason = "EXECUTION_RESOURCE_BUSY" if busy else "PLUGIN_RELEASE_HELD"
                return public_invocation(self.repository.update(row["invocation_id"], status="FAILED", error_code=reason, error_summary="当前执行资源不可用，本次已结束，请稍后重新触发"))
            loop = self._loop
            if loop is None:
                loop = self._loop = asyncio.get_running_loop()
            self._active[row["invocation_id"]] = {"keys": keys, "account_ids": account_ids, "automation_id": row.get("automation_id"), "operation": row["operation"], "task": None}
            def launch():
                with self._lock:
                    active = self._active[row["invocation_id"]]
                    active["task"] = loop.create_task(coroutine_factory())
            try:
                if asyncio.get_running_loop() is loop:
                    launch()
                else:
                    loop.call_soon_threadsafe(launch)
            except RuntimeError:
                loop.call_soon_threadsafe(launch)
            return public_invocation(stored)

    def start(self, *, invocation, capability: Mapping, arguments: Mapping, source: str, actor_id: str, request_key: str, preview_invocation_id: str | None = None, admission_guard=None) -> dict:
        resolved = apply_selection_execution_boundary(apply_scan_execution_boundary(capability, arguments), arguments)
        metadata = resolved["_plugin_runtime"]
        row = self._row(operation=str(resolved["name"]), source=source, actor_id=actor_id, request_id=invocation.request_id, request_key=request_key, arguments=arguments)
        row.update(automation_id=invocation.automation_id, plugin_id=metadata["plugin_id"], plugin_version=metadata["version"], generation=invocation.automation_generation, invocation_json=invocation.to_dict(), preview_invocation_id=preview_invocation_id)
        row["request_sha256"] = canonical_sha256({"arguments": arguments, "invocation": row["invocation_json"], "actor": actor_id, "preview": preview_invocation_id})
        accounts = {str(value) for raw in metadata["account_bindings"].values() for value in (raw if isinstance(raw, (list, tuple)) else (raw,))}
        scopes = {}
        write = resolved.get("operation_type") not in {"read", "compute"}
        keys, bounded = canonical_resource_write_locks(resolved, accounts, self._resources, action_scopes=scopes) if write else (set(), True)
        if write and not bounded:
            keys.update(("account-write", account) for account in accounts)
        # Single instance calls cannot race browser state; independent instances
        # and HTTP readers share credentials without an account-wide read lock.
        return self._reserve(row=row, keys=(("plugin-instance", invocation.automation_id),), account_ids=frozenset(accounts), admission_guard=admission_guard, coroutine_factory=lambda: self._run_plugin(row, invocation, resolved, dict(arguments), tuple(sorted(keys)), scopes))

    def _row(self, *, operation, source, actor_id, request_id, request_key, arguments) -> dict:
        return {"invocation_id": str(uuid.uuid4()), "request_key_sha256": canonical_sha256([source, actor_id, operation, request_key]), "request_sha256": canonical_sha256(arguments), "request_id": request_id, "operation": operation, "source": source, "actor_id": actor_id, "owner_id": self.owner_id, "arguments_json": dict(arguments), "invocation_json": {}}

    async def _run_plugin(self, row, invocation, capability, arguments, keys, scopes) -> None:
        call_id = row["invocation_id"]
        resource_token = EXECUTION_RESOURCE_KEYS.set(keys)
        action_token = EXECUTION_ACTION_SCOPES.set(scopes)
        try:
            await _preflight_thread(self.repository.update, call_id, status="RUNNING")
            with self._lock:
                account_ids = tuple(self._active[call_id]["account_ids"])
            if account_ids and self._account_validator is None:
                raise OrchestrationError("ACCOUNT_VALIDATION_UNAVAILABLE", "本次执行缺少账号校验接口")
            for account_id in account_ids:
                await _preflight_thread(self._account_validator, account_id)
            if self._prepare_arguments is not None:
                arguments = await _preflight_thread(self._prepare_arguments, invocation, capability, arguments)
            raw = await self.executor.execute(capability, arguments, trusted_invocation_context={"invocation_id": call_id, "_automation_project_invocation": invocation.to_dict()})
            request = InvocationVerificationRequest(str(capability["name"]), arguments, OperationType(str(capability["operation_type"])))
            verification = asyncio.create_task(asyncio.to_thread(self.verifier.verify, request, raw, capability))
            try:
                outcome = await asyncio.shield(verification)
            except asyncio.CancelledError:
                # The plugin has ended, but verification can still finalize
                # durable receipts. Keep ownership until its actual result.
                outcome = await verification
            result = outcome.result.to_dict() if outcome.result is not None else dict(raw)
            async def finish():
                if outcome.accepted and self._publish_result is not None:
                    await asyncio.to_thread(self._publish_result, call_id, invocation.automation_id, capability, arguments, outcome)
                unknown = not outcome.accepted and await asyncio.to_thread(self.repository.has_unverified_write, call_id)
                await asyncio.to_thread(self.repository.update, call_id, status="COMPLETED" if outcome.accepted else "WRITE_OUTCOME_UNKNOWN" if unknown else "FAILED", result=result, error_code=None if outcome.accepted else outcome.code, error_summary=None if outcome.accepted else outcome.message)
            completion = asyncio.create_task(finish())
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                # The plugin already ended. Drain its actual business commit
                # and report its result; cancellation cannot undo that commit.
                await completion
        except BaseException as exc:
            unknown = await asyncio.to_thread(self.repository.has_started_write, call_id)
            cancelled = isinstance(exc, asyncio.CancelledError)
            await asyncio.to_thread(self.repository.update, call_id, status="WRITE_OUTCOME_UNKNOWN" if unknown else "CANCELLED" if cancelled else "FAILED", error_code="WRITE_OUTCOME_UNKNOWN" if unknown else "CANCELLED" if cancelled else getattr(exc, "code", type(exc).__name__.upper()), error_summary="执行已停止，已发出的写入结果尚未确认" if unknown else "本次执行已取消" if cancelled else redact_text(exc))
        finally:
            EXECUTION_ACTION_SCOPES.reset(action_token)
            EXECUTION_RESOURCE_KEYS.reset(resource_token)
            with self._lock:
                self._active.pop(call_id, None)

    async def wait(self, invocation_id: str, *, timeout_seconds: float | None = None) -> dict:
        with self._lock:
            active = self._active.get(invocation_id)
        if active:
            # The launch callback runs immediately on this loop, not a worker
            # polling persisted records. Yield once for cross-thread callers.
            if active["task"] is None:
                await asyncio.sleep(0)
            task = active["task"]
            if task is not None:
                if timeout_seconds is None:
                    await asyncio.shield(task)
                else:
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
                    except asyncio.TimeoutError:
                        return await asyncio.to_thread(self.get, invocation_id)
        return await asyncio.to_thread(self.get, invocation_id)

    def wait_sync(self, invocation_id: str, *, timeout_seconds: float | None = None) -> dict:
        """For synchronous tool adapters already executing in another thread."""
        if self._loop is None or not self._loop.is_running():
            raise OrchestrationError("INVOCATION_RUNTIME_UNAVAILABLE", "执行服务尚未启动")
        try:
            caller_loop = asyncio.get_running_loop()
        except RuntimeError:
            caller_loop = None
        if caller_loop is self._loop:
            raise RuntimeError("wait_sync cannot block the invocation event loop")
        future = asyncio.run_coroutine_threadsafe(self.wait(invocation_id, timeout_seconds=timeout_seconds), self._loop)
        return future.result()

    async def cancel(self, invocation_id: str) -> dict:
        with self._lock:
            active = self._active.get(invocation_id)
        if not active:
            return await asyncio.to_thread(self.get, invocation_id)
        with self._lock:
            first_cancel = not active.get("cancel_requested")
            active["cancel_requested"] = True
        if active["task"] is None:
            await asyncio.sleep(0)
        await asyncio.to_thread(self.repository.update, invocation_id, status="CANCELLING")
        task = active["task"]
        if task is not None and not task.done():
            if first_cancel:
                task.cancel()
            self._operation_changed.set()
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        # Cancellation before the coroutine's first instruction has no
        # finalizer; settle that never-started call explicitly.
        with self._lock:
            never_started = invocation_id in self._active and (task is None or task.cancelled())
            if never_started:
                self._active.pop(invocation_id, None)
        if never_started:
            await asyncio.to_thread(self.repository.update, invocation_id, status="CANCELLED", error_code="CANCELLED", error_summary="执行启动前已取消")
        return await asyncio.to_thread(self.get, invocation_id)

    async def stop(self) -> None:
        with self._lock:
            self._closed = True
            reads = [item["task"] for item in self._active_reads.values()]
        await asyncio.gather(*(self.cancel(call_id) for call_id in tuple(self._active)), return_exceptions=True)
        if reads:
            await asyncio.gather(*(asyncio.shield(task) for task in reads), return_exceptions=True)

    async def call_business(self, *, operation: str, request_id: str, actor_id: str, source: str, arguments: Mapping, handler, verify=None, write: bool = True, resource_keys: tuple = (), account_ids: tuple[str, ...] = ()) -> dict:
        row = self._row(operation=operation, source=source, actor_id=actor_id, request_id=request_id, request_key=request_id, arguments=arguments)
        async def run():
            started = False
            try:
                await _preflight_thread(self.repository.update, row["invocation_id"], status="RUNNING")
                started = True
                work = asyncio.create_task(handler())
                try:
                    result = await asyncio.shield(work)
                except asyncio.CancelledError:
                    # An HTTP request may already be accepted remotely. Keep
                    # this scope occupied until the host operation really ends.
                    await asyncio.gather(work, return_exceptions=True)
                    raise
                async def finish():
                    accepted = result.get("success") is not False and result.get("ok") is not False and str(result.get("status", "SUCCESS")).upper() not in {"ERROR", "FAILED"}
                    if verify is not None:
                        verified = verify(result)
                        if inspect.isawaitable(verified):
                            verified = await verified
                        accepted = accepted and (verified is True or getattr(verified, "accepted", False))
                    await asyncio.to_thread(self.repository.update, row["invocation_id"], status="COMPLETED" if accepted else "WRITE_OUTCOME_UNKNOWN" if write else "FAILED", result=result, error_code=None if accepted else "BUSINESS_VERIFICATION_FAILED")
                completion = asyncio.create_task(finish())
                try:
                    await asyncio.shield(completion)
                except asyncio.CancelledError:
                    # The operation returned: preserve its verified result and
                    # keep ownership until actual persistence has finished.
                    await completion
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                await asyncio.to_thread(self.repository.update, row["invocation_id"], status="WRITE_OUTCOME_UNKNOWN" if write and started else "CANCELLED" if cancelled else "FAILED", error_code="WRITE_OUTCOME_UNKNOWN" if write and started else "CANCELLED" if cancelled else getattr(exc, "code", type(exc).__name__.upper()), error_summary=redact_text(exc))
            finally:
                with self._lock:
                    self._active.pop(row["invocation_id"], None)
        accepted = self._reserve(row=row, keys=resource_keys, account_ids=frozenset(account_ids), coroutine_factory=run)
        return await self.wait(accepted["invocation_id"])
