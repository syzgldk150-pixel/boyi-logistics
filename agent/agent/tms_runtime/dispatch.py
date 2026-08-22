"""Compatibility dispatcher for legacy http_service endpoints."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from agent.tms_runtime.errors import TMSAuthStateError, auth_error_payload
from agent.tms_runtime.account_manager import resolve_account_params, resolve_role_account_params


logger = logging.getLogger("agent")

MODULE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = MODULE_DIR / "scripts"
SCRIPTS_PACKAGE = "agent.tms_runtime.scripts"


class TaskRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_sec: int = Field(default=600, ge=1, le=24 * 60 * 60)
    actor: dict[str, Any] | None = None
    actor_roles: list[str] = Field(default_factory=list)
    source: str = "legacy_api"
    idempotency_key: str | None = None


@dataclass(frozen=True)
class Target:
    module: str
    func: str
    max_concurrency: int = 3


SCAN_NEXT_CONCURRENCY = max(1, int(os.getenv("SCAN_NEXT_CONCURRENCY", "3")))

TARGETS: dict[str, Target] = {
    "fetch_dispatch": Target(module=f"{SCRIPTS_PACKAGE}.fetch_dispatch", func="run_once"),
    "fetch_pre_arrive_list": Target(module=f"{SCRIPTS_PACKAGE}.fetch_pre_arrive_list", func="run_once"),
    "send_order": Target(module=f"{SCRIPTS_PACKAGE}.Send_order", func="run_once"),
    "delivery_status": Target(module=f"{SCRIPTS_PACKAGE}.Delivery_status", func="run_once"),
    "clock_in_dual": Target(module=f"{SCRIPTS_PACKAGE}.clock_in_dual", func="run_once"),
    "auto_checkin_r7": Target(module=f"{SCRIPTS_PACKAGE}.auto_checkin_r7", func="run_once"),
    "ronghui_tms_tracking": Target(module="agent.tms_runtime.scripts.ronghui_tms_tracking", func="run_once"),
    "yunda_waybill_tracking": Target(module=f"{SCRIPTS_PACKAGE}.yunda_waybill_tracking", func="run_once"),
    "tracking_query": Target(module=f"{SCRIPTS_PACKAGE}.tracking_query", func="run_once"),
    "get_scan": Target(module=f"{SCRIPTS_PACKAGE}.get_scan", func="run_once"),
    "get_sign_records": Target(module=f"{SCRIPTS_PACKAGE}.get_sign_records", func="run_once"),
    "get_qianshou": Target(module=f"{SCRIPTS_PACKAGE}.get_qianshou", func="run_once"),
    "get_sign_records": Target(
        module="agent.tms_runtime.scripts.get_sign_records",
        func="run_once",
    ),
    "get_price": Target(module="agent.tms_runtime.scripts.get_price", func="run_once"),
    "get_wangdiansendlist": Target(module=f"{SCRIPTS_PACKAGE}.get_wangdiansendlist", func="run_once"),
    "child_count": Target(module=f"{SCRIPTS_PACKAGE}.child_count", func="run_once"),
    "yunda_waybill_entry": Target(module="agent.tms_runtime.scripts.yunda_waybill_entry", func="run_once"),
    "yunda_waybill_proxy": Target(module="agent.tms_runtime.scripts.yunda_waybill_proxy", func="run_once"),
    "receipts_sync": Target(module="agent.tms_runtime.scripts.receipts_sync", func="run_once"),
    "receipts_audit": Target(module="agent.tms_runtime.scripts.receipts_audit", func="run_once", max_concurrency=1),
    "customer_service_problem": Target(
        module="agent.tms_runtime.scripts.customer_service_problem",
        func="run_once",
        max_concurrency=6,
    ),
    "ronghui_waybill_proxy": Target(
        module="agent.tms_runtime.scripts.ronghui_waybill_proxy",
        func="run_once",
        max_concurrency=12,
    ),
    "yunda_price": Target(module="agent.tms_runtime.scripts.yunda_price", func="run_once"),
    "waybill_tracking": Target(module=f"{SCRIPTS_PACKAGE}.waybill_tracking", func="run_once"),
    "query_waybill_detail": Target(module=f"{SCRIPTS_PACKAGE}.query_waybill_detail", func="run_once"),
    "self_pickup_problem_upload": Target(module=f"{SCRIPTS_PACKAGE}.self_pickup_problem_upload", func="run_once", max_concurrency=1),
    "split_pending_problem_upload": Target(module="agent.tms_runtime.scripts.split_pending_problem_upload", func="run_once", max_concurrency=1),
    "scan_next": Target(module=f"{SCRIPTS_PACKAGE}.scan_next", func="run_once", max_concurrency=SCAN_NEXT_CONCURRENCY),
    "yunda_dispatch_forecast": Target(module=f"{SCRIPTS_PACKAGE}.yunda_dispatch_forecast", func="run_once"),
    "yunda_send_waybills": Target(module=f"{SCRIPTS_PACKAGE}.yunda_send_waybills", func="run_once"),
}

TARGET_ACCOUNT_SYSTEMS: dict[str, str] = {
    "fetch_dispatch": "ronghui",
    "fetch_pre_arrive_list": "ronghui",
    "send_order": "ronghui",
    "delivery_status": "ronghui",
    "clock_in_dual": "ronghui",
    "get_scan": "ronghui",
    "get_sign_records": "ronghui",
    "get_qianshou": "r13",
    "get_sign_records": "ronghui",
    "get_wangdiansendlist": "ronghui",
    "child_count": "ronghui",
    "ronghui_tms_tracking": "ronghui",
    "waybill_tracking": "ronghui",
    "query_waybill_detail": "ronghui",
    "self_pickup_problem_upload": "ronghui",
    "split_pending_problem_upload": "ronghui",
    "scan_next": "ronghui",
    "get_price": "ronghui",
    "yunda_waybill_tracking": "yunda",
    "yunda_waybill_entry": "yunda",
    "yunda_waybill_proxy": "yunda",
    "receipts_sync": "ronghui",
    "ronghui_waybill_proxy": "ronghui",
    "yunda_price": "yunda",
    "yunda_dispatch_forecast": "yunda",
    "yunda_send_waybills": "yunda",
}

TARGET_ACCOUNT_PURPOSES: dict[str, str] = {
    "get_price": "price",
    "receipts_sync": "price",
    "ronghui_waybill_proxy": "price",
}

TARGET_DEFAULT_ACCOUNT_IDS: dict[str, str] = {
    "self_pickup_problem_upload": "ronghui_self_pickup_problem",
    "split_pending_problem_upload": "ronghui_default",
}

TARGET_ACCOUNT_ROLE_FIELDS: dict[str, list[dict[str, str]]] = {
    "self_pickup_problem_upload": [
        {
            "account_field": "daxiang_s_account_id",
            "output_session_profile_field": "daxiang_s_session_profile",
        },
    ],
}

_SEMAPHORES: dict[str, asyncio.Semaphore] = {
    name: asyncio.Semaphore(max(1, target.max_concurrency))
    for name, target in TARGETS.items()
}


def _sanitize_params_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if str(key).lower() in {
                "account",
                "aurora-token",
                "code",
                "mobile",
                "pass",
                "passwd",
                "password",
                "phone",
                "token",
                "user",
                "username",
                "validatecode",
            }:
                sanitized[str(key)] = "***"
            else:
                sanitized[str(key)] = _sanitize_params_for_log(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_params_for_log(item) for item in value]
    return value


def _load_callable(target: Target) -> Callable[[dict[str, Any]], Any]:
    mod = importlib.import_module(target.module)
    if target.module.startswith(f"{SCRIPTS_PACKAGE}.") and _script_auth_is_stale(mod):
        # A legacy bare import can leave a script module cached with a TMSAuth
        # class from outside the packaged runtime.  Do not execute that mixed
        # module; rebuild it from the canonical package modules instead.
        _discard_stale_script_modules(target.module)
        mod = importlib.import_module(target.module)
    fn = getattr(mod, target.func, None)
    if fn is None or not callable(fn):
        raise AttributeError(f"{target.module}.{target.func} not found or not callable")
    return fn


def _module_is_from_scripts(module: object) -> bool:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        return False
    try:
        Path(module_file).resolve().relative_to(SCRIPTS_DIR.resolve())
    except ValueError:
        return False
    return True


def _script_auth_is_stale(module: object) -> bool:
    """Return whether a runtime script cached an auth class from outside its package."""

    auth = getattr(module, "TMSAuth", None)
    if auth is None:
        return False
    auth_module_name = getattr(auth, "__module__", "")
    expected_module_name = f"{SCRIPTS_PACKAGE}.login_manager"
    if auth_module_name != expected_module_name:
        return True
    return not _module_is_from_scripts(sys.modules.get(expected_module_name))


def _discard_stale_script_modules(module_name: str) -> None:
    """Remove only the cached runtime script/auth modules that failed isolation."""

    login_manager_name = f"{SCRIPTS_PACKAGE}.login_manager"
    for stale_name in (module_name, login_manager_name):
        stale_module = sys.modules.pop(stale_name, None)
        child_name = stale_name.rsplit(".", 1)[-1]
        package = sys.modules.get(SCRIPTS_PACKAGE)
        if stale_module is not None and getattr(package, child_name, None) is stale_module:
            delattr(package, child_name)


def _run_sync(fn: Callable[[dict[str, Any]], Any], params: dict[str, Any]) -> Any:
    return fn(params)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _error_payload(exc: BaseException, *, override_type: str | None = None, override_error: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": override_type or type(exc).__name__,
        "error": override_error or str(exc),
    }


async def execute_target(name: str, req: TaskRequest) -> tuple[int, dict[str, Any]]:
    semaphore = _SEMAPHORES[name]
    async with semaphore:
        logger.info(
            "tms start %s timeout_sec=%s params=%s",
            name,
            req.timeout_sec,
            _sanitize_params_for_log(req.params),
        )
        started = time.time()
        try:
            target = TARGETS[name]
            input_params = dict(req.params)
            target_account_id = TARGET_DEFAULT_ACCOUNT_IDS.get(name, "")
            if (
                target_account_id
                and not input_params.get("account_id")
                and not input_params.get("accountId")
                and not input_params.get("session_profile")
            ):
                input_params["account_id"] = target_account_id
            effective_params = resolve_account_params(
                input_params,
                default_system=TARGET_ACCOUNT_SYSTEMS.get(name, ""),
                default_purpose=TARGET_ACCOUNT_PURPOSES.get(name, ""),
            )
            for role_binding in TARGET_ACCOUNT_ROLE_FIELDS.get(name, []):
                effective_params = resolve_role_account_params(
                    effective_params,
                    account_field=role_binding["account_field"],
                    output_session_profile_field=role_binding.get("output_session_profile_field", "session_profile"),
                )
            fn = _load_callable(target)
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_sync, fn, effective_params),
                timeout=req.timeout_sec,
            )
            cost = round(time.time() - started, 3)
            payload = _jsonable(result)
            task_ok = True
            if isinstance(payload, dict) and "ok" in payload:
                task_ok = bool(payload.get("ok"))
            logger.info("tms done %s cost_sec=%.3f", name, cost)
            return 200, {
                "ok": task_ok,
                "cost_sec": cost,
                "data": payload,
            }
        except TMSAuthStateError as exc:
            cost = round(time.time() - started, 3)
            logger.warning("tms auth %s code=%s cost_sec=%.3f", name, exc.code, cost)
            return 200, {
                **auth_error_payload(exc),
                "cost_sec": cost,
                "data": {},
            }
        except asyncio.TimeoutError as exc:
            logger.warning("tms timeout %s after=%ss", name, req.timeout_sec)
            return 504, _error_payload(exc, override_type="Timeout", override_error=f"Task timeout after {req.timeout_sec}s")
        except Exception as exc:
            logger.error("tms error %s %s\n%s", name, exc, traceback.format_exc())
            return 500, _error_payload(exc)
