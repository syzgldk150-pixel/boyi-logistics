"""Synchronize Ronghui and Yunda finance ledgers from their original pages."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)
for package_root in (WORKSPACE_ROOT, PROJECT_ROOT):
    if package_root not in sys.path:
        sys.path.insert(0, package_root)

from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.scripts.finance_capture_common import FinanceCaptureError
from agent.tms_runtime.scripts.finance_live_capture import build_live_finance_adapter
from shared.finance import FinanceRepository
from tools.finance_sync_service import FinanceSyncError, FinanceSyncService


LOCK_PATH = os.path.join(PROJECT_ROOT, "logs", ".finance_sync.lock")


def _default_connection_factory() -> Any:
    from agent.workflow_resource_store import _connect

    return _connect()


@contextmanager
def _local_process_lock() -> Iterator[None]:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            import fcntl
        except ImportError:
            yield
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise FinanceSyncError(
                "FINANCE_SYNC_ALREADY_RUNNING",
                "财务账单同步已有实例正在执行",
            ) from exc
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(descriptor)


@contextmanager
def _database_lock(connection_factory: Any) -> Iterator[None]:
    connection = connection_factory()
    cursor = connection.cursor()
    acquired = False
    try:
        cursor.execute("SELECT GET_LOCK(%s, 0)", ("shipnow.finance.sync",))
        row = cursor.fetchone()
        if isinstance(row, Mapping):
            value = next(iter(row.values()), None)
        elif isinstance(row, (list, tuple)):
            value = row[0] if row else None
        else:
            value = row
        acquired = int(value or 0) == 1
        if not acquired:
            raise FinanceSyncError(
                "FINANCE_SYNC_ALREADY_RUNNING",
                "财务账单同步已有实例正在执行",
            )
        yield
    finally:
        if acquired:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", ("shipnow.finance.sync",))
            except Exception:
                pass
        try:
            cursor.close()
        finally:
            connection.close()


@contextmanager
def _single_instance_lock(connection_factory: Any) -> Iterator[None]:
    with _local_process_lock(), _database_lock(connection_factory):
        yield


def run_sync_finance_bills(
    params: Mapping[str, Any] | None = None,
    *,
    repository: Any | None = None,
    account_manager: Any | None = None,
    adapter_factory: Any | None = None,
    shared_api: Any | None = None,
    now: Any | None = None,
    connection_factory: Any | None = None,
    lock_context: Any | None = None,
) -> dict[str, Any]:
    request = dict(params or {})
    try:
        effective_connection_factory = connection_factory or _default_connection_factory
        lock_manager = (
            lock_context()
            if callable(lock_context)
            else lock_context
            if lock_context is not None
            else _single_instance_lock(effective_connection_factory)
        )
        with lock_manager:
            service = FinanceSyncService(
                repository=repository or FinanceRepository(effective_connection_factory),
                account_manager=account_manager or get_account_manager(),
                adapter_factory=adapter_factory or build_live_finance_adapter,
                shared_api=shared_api,
                now=now,
            )
            result = service.run(request)
            result.setdefault("success", bool(result.get("ok")))
            return result
    except (FinanceSyncError, FinanceCaptureError) as exc:
        return {
            "success": False,
            "ok": False,
            "error_code": str(getattr(exc, "code", "FINANCE_SYNC_FAILED")),
            "error": str(exc),
        }
    except Exception:
        return {
            "success": False,
            "ok": False,
            "error_code": "FINANCE_SYNC_FAILED",
            "error": "财务账单同步失败；请查看脱敏后的服务日志",
        }


def main() -> None:
    raw = sys.stdin.read()
    params = json.loads(raw) if raw.strip() else {}
    print(json.dumps(run_sync_finance_bills(params), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
