"""FastAPI routes for the embedded TMS runtime and session management."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent.tms_runtime.dispatch import TARGETS, TaskRequest, execute_target
from agent.api_contracts import EnvelopedRoute
from agent.tms_runtime.errors import TMSAuthStateError, auth_error_payload
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.monitoring import (
    build_daily_sign_monitoring_snapshot,
    build_monitoring_detail_link,
    build_monitoring_snapshot,
)


router = APIRouter(route_class=EnvelopedRoute)

ACCOUNT_LIST_CACHE_TTL_SEC = 60
_ACCOUNT_LIST_CACHE: dict[str, Any] = {}
_ACCOUNT_LIST_CACHE_LOCK = threading.Lock()
_ACCOUNT_LIST_REFRESHING = False


class SubmitCodeRequest(BaseModel):
    code: str = ""


class CredentialsRequest(BaseModel):
    username: str = ""
    password: str = ""
    phone: str = ""


class AccountCreateRequest(BaseModel):
    account_id: str = ""
    system: str = ""
    name: str = ""
    account_purpose: str = ""


class AccountNameRequest(BaseModel):
    name: str = ""


class AccountActiveRequest(BaseModel):
    is_active: bool = True


class AccountAutoLoginRequest(BaseModel):
    enabled: bool = True


class MonitoringDetailLinkRequest(BaseModel):
    system: str = ""
    category_id: str = ""
    title: str = ""
    resource_id: str = ""
    type_code: str = ""
    target_title: str = ""


def _success_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def _account_manager():
    return get_account_manager()


def _account_list_payload(*, force: bool = False) -> dict[str, Any]:
    return {"accounts": _account_manager().list_accounts(include_status=True, force=force)}


def _cache_age_sec(now: float, cached_at: float | None) -> int:
    if not cached_at:
        return 0
    return max(int(now - cached_at), 0)


def _account_list_cache_meta(
    *,
    cached: bool,
    stale: bool,
    refreshing: bool,
    cache_age_sec: int,
) -> dict[str, Any]:
    return {
        "cached": cached,
        "stale": stale,
        "refreshing": refreshing,
        "cache_age_sec": cache_age_sec,
    }


def _store_account_list_cache(payload: dict[str, Any]) -> None:
    with _ACCOUNT_LIST_CACHE_LOCK:
        _ACCOUNT_LIST_CACHE.clear()
        _ACCOUNT_LIST_CACHE.update(
            {
                "payload": copy.deepcopy(payload),
                "cached_at": time.time(),
            }
        )


def _invalidate_account_list_cache() -> None:
    with _ACCOUNT_LIST_CACHE_LOCK:
        _ACCOUNT_LIST_CACHE.clear()


def update_account_list_cache_status(status_payload: dict[str, Any]) -> bool:
    """Update one cached account row with a freshly checked status payload."""
    account_id = str(status_payload.get("account_id") or "").strip()
    if not account_id:
        return False
    with _ACCOUNT_LIST_CACHE_LOCK:
        payload = _ACCOUNT_LIST_CACHE.get("payload")
        if not isinstance(payload, dict):
            return False
        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            return False
        for account in accounts:
            if not isinstance(account, dict):
                continue
            if str(account.get("account_id") or "").strip() != account_id:
                continue
            account["status"] = copy.deepcopy(dict(status_payload))
            return True
    return False


def _refresh_account_list_cache(*, force: bool = True) -> None:
    global _ACCOUNT_LIST_REFRESHING
    try:
        _store_account_list_cache(_account_list_payload(force=force))
    finally:
        with _ACCOUNT_LIST_CACHE_LOCK:
            _ACCOUNT_LIST_REFRESHING = False


def _schedule_account_list_refresh(*, force: bool = True) -> bool:
    global _ACCOUNT_LIST_REFRESHING
    with _ACCOUNT_LIST_CACHE_LOCK:
        if _ACCOUNT_LIST_REFRESHING:
            return False
        _ACCOUNT_LIST_REFRESHING = True
    thread = threading.Thread(
        target=_refresh_account_list_cache,
        kwargs={"force": force},
        name="account-list-refresh",
        daemon=True,
    )
    thread.start()
    return True


def _cached_account_list_response(*, force: bool = False, prefer_cached: bool = False) -> dict[str, Any]:
    now = time.time()
    with _ACCOUNT_LIST_CACHE_LOCK:
        cached_payload = copy.deepcopy(_ACCOUNT_LIST_CACHE.get("payload"))
        cached_at = _ACCOUNT_LIST_CACHE.get("cached_at")
        refreshing = bool(_ACCOUNT_LIST_REFRESHING)

    if prefer_cached and cached_payload:
        age = _cache_age_sec(now, cached_at if isinstance(cached_at, (int, float)) else None)
        stale = bool(force or age >= ACCOUNT_LIST_CACHE_TTL_SEC)
        if stale and not refreshing:
            refreshing = _schedule_account_list_refresh(force=force)
        return _success_response(
            {
                **cached_payload,
                **_account_list_cache_meta(
                    cached=True,
                    stale=stale,
                    refreshing=refreshing,
                    cache_age_sec=age,
                ),
            }
        )

    payload = _account_list_payload(force=force)
    _store_account_list_cache(payload)
    return _success_response(
        {
            **payload,
            **_account_list_cache_meta(
                cached=False,
                stale=False,
                refreshing=False,
                cache_age_sec=0,
            ),
        }
    )


def _account_error_response(exc: TMSAuthStateError, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=auth_error_payload(exc))


@router.get("/admin/accounts")
def automation_accounts(force: bool = False, prefer_cached: bool = False):
    return _cached_account_list_response(force=force, prefer_cached=prefer_cached)


@router.post("/admin/accounts")
def automation_account_create(req: AccountCreateRequest):
    try:
        account = _account_manager().create_account(
            account_id=req.account_id,
            system=req.system,
            name=req.name,
            account_purpose=req.account_purpose,
        )
        _invalidate_account_list_cache()
        return _success_response({"account": account})
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.get("/admin/accounts/{account_id}/status")
def automation_account_status(account_id: str, force: bool = False):
    try:
        status = _account_manager().check_status_with_auto_login(account_id, force=force)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.post("/admin/accounts/{account_id}/name")
def automation_account_update_name(account_id: str, req: AccountNameRequest):
    try:
        account = _account_manager().update_name(account_id, req.name)
        _invalidate_account_list_cache()
        return _success_response({"account": account})
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.post("/admin/accounts/{account_id}/credentials")
def automation_account_save_credentials(account_id: str, req: CredentialsRequest):
    try:
        credentials = _account_manager().save_credentials(
            account_id,
            username=req.username,
            password=req.password,
            phone=req.phone,
        )
        _invalidate_account_list_cache()
        return _success_response({"credentials": credentials})
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.post("/admin/accounts/{account_id}/credentials/clear")
def automation_account_clear_credentials(account_id: str):
    try:
        credentials = _account_manager().clear_credentials(account_id)
        _invalidate_account_list_cache()
        return _success_response({"credentials": credentials})
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.post("/admin/accounts/{account_id}/login")
def automation_account_login(account_id: str):
    try:
        status = _account_manager().login(account_id)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/accounts/{account_id}/submit-code")
def automation_account_submit_code(account_id: str, req: SubmitCodeRequest):
    try:
        status = _account_manager().submit_code(account_id, req.code)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/accounts/{account_id}/clear-session")
def automation_account_clear_session(account_id: str):
    try:
        status = _account_manager().clear_session(account_id)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.post("/admin/accounts/{account_id}/default")
def automation_account_set_default(account_id: str):
    try:
        account = _account_manager().set_default(account_id)
        _invalidate_account_list_cache()
        return _success_response({"account": account})
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.post("/admin/accounts/{account_id}/active")
def automation_account_set_active(account_id: str, req: AccountActiveRequest):
    try:
        account = _account_manager().set_active(account_id, req.is_active)
        _invalidate_account_list_cache()
        return _success_response({"account": account})
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.post("/admin/accounts/{account_id}/auto-login")
def automation_account_set_auto_login(account_id: str, req: AccountAutoLoginRequest):
    try:
        account = _account_manager().set_auto_login(account_id, req.enabled)
        _invalidate_account_list_cache()
        return _success_response({"account": account})
    except TMSAuthStateError as exc:
        return _account_error_response(exc)


@router.get("/admin/tms/session/status")
def tms_session_status(force: bool = False):
    return _success_response(_account_manager().describe_status("ronghui_default", validate=True, force=force))


@router.post("/admin/tms/session/send-code")
def tms_session_send_code():
    try:
        status = _account_manager().login("ronghui_default")
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.get("/admin/tms/session/credentials")
def tms_session_credentials():
    return _success_response(_account_manager().public_credentials("ronghui_default"))


@router.post("/admin/tms/session/credentials")
def tms_session_save_credentials(req: CredentialsRequest):
    try:
        credentials = _account_manager().save_credentials(
            "ronghui_default",
            username=req.username,
            password=req.password,
            phone=req.phone,
        )
        _invalidate_account_list_cache()
        return _success_response(credentials)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/tms/session/credentials/clear")
def tms_session_clear_credentials():
    credentials = _account_manager().clear_credentials("ronghui_default")
    _invalidate_account_list_cache()
    return _success_response(credentials)


@router.post("/admin/tms/session/submit-code")
def tms_session_submit_code(req: SubmitCodeRequest):
    try:
        status = _account_manager().submit_code("ronghui_default", req.code)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/tms/session/clear")
def tms_session_clear():
    status = _account_manager().clear_session("ronghui_default")
    update_account_list_cache_status(status)
    return _success_response(status)


@router.get("/admin/tms/price-session/status")
def tms_price_session_status(force: bool = False):
    return _success_response(_account_manager().describe_status("price_default", validate=True, force=force))


@router.post("/admin/tms/price-session/send-code")
def tms_price_session_send_code():
    try:
        status = _account_manager().login("price_default")
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.get("/admin/tms/price-session/credentials")
def tms_price_session_credentials():
    return _success_response(_account_manager().public_credentials("price_default"))


@router.post("/admin/tms/price-session/credentials")
def tms_price_session_save_credentials(req: CredentialsRequest):
    try:
        credentials = _account_manager().save_credentials(
            "price_default",
            username=req.username,
            password=req.password,
            phone=req.phone,
        )
        _invalidate_account_list_cache()
        return _success_response(credentials)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/tms/price-session/credentials/clear")
def tms_price_session_clear_credentials():
    credentials = _account_manager().clear_credentials("price_default")
    _invalidate_account_list_cache()
    return _success_response(credentials)


@router.post("/admin/tms/price-session/submit-code")
def tms_price_session_submit_code(req: SubmitCodeRequest):
    try:
        status = _account_manager().submit_code("price_default", req.code)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/tms/price-session/clear")
def tms_price_session_clear():
    status = _account_manager().clear_session("price_default")
    update_account_list_cache_status(status)
    return _success_response(status)


@router.get("/admin/tms/yunda-session/status")
def tms_yunda_session_status(force: bool = False):
    return _success_response(_account_manager().describe_status("yunda_default", validate=True, force=force))


@router.post("/admin/tms/yunda-session/send-code")
def tms_yunda_session_send_code():
    try:
        status = _account_manager().login("yunda_default")
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.get("/admin/tms/yunda-session/credentials")
def tms_yunda_session_credentials():
    return _success_response(_account_manager().public_credentials("yunda_default"))


@router.post("/admin/tms/yunda-session/credentials")
def tms_yunda_session_save_credentials(req: CredentialsRequest):
    try:
        credentials = _account_manager().save_credentials(
            "yunda_default",
            username=req.username,
            password=req.password,
            phone=req.phone,
        )
        _invalidate_account_list_cache()
        return _success_response(credentials)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/tms/yunda-session/credentials/clear")
def tms_yunda_session_clear_credentials():
    credentials = _account_manager().clear_credentials("yunda_default")
    _invalidate_account_list_cache()
    return _success_response(credentials)


@router.post("/admin/tms/yunda-session/submit-code")
def tms_yunda_session_submit_code(req: SubmitCodeRequest):
    try:
        status = _account_manager().submit_code("yunda_default", req.code)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/tms/yunda-session/clear")
def tms_yunda_session_clear():
    status = _account_manager().clear_session("yunda_default")
    update_account_list_cache_status(status)
    return _success_response(status)


@router.get("/admin/monitoring/snapshot")
def monitoring_snapshot(systems: str = "yunda,ronghui", force: bool = False, prefer_cached: bool = False):
    selected = [item.strip() for item in str(systems or "").split(",") if item.strip()]
    return build_monitoring_snapshot(systems=selected, force=force, prefer_cached=prefer_cached)


@router.get("/admin/monitoring/daily-sign")
def monitoring_daily_sign(force: bool = False, target_date: str = "", prefer_cached: bool = False):
    return build_daily_sign_monitoring_snapshot(force=force, target_date=target_date, prefer_cached=prefer_cached)


@router.post("/admin/monitoring/detail-link")
def monitoring_detail_link(req: MonitoringDetailLinkRequest):
    if hasattr(req, "model_dump"):
        payload = req.model_dump()
    else:
        payload = req.dict()
    return build_monitoring_detail_link(payload)


def _normalize_task_request(raw_payload: Any) -> TaskRequest:
    if isinstance(raw_payload, dict) and (
        "params" in raw_payload
        or "timeout_sec" in raw_payload
    ):
        return TaskRequest(**raw_payload)
    if isinstance(raw_payload, dict):
        return TaskRequest(params=raw_payload)
    return TaskRequest(params={})


def _build_handler(endpoint_name: str):
    async def _handler(request: Request):
        try:
            raw_payload = await request.json()
        except Exception:
            raw_payload = {}
        req = _normalize_task_request(raw_payload)
        status_code, payload = await execute_target(endpoint_name, req)
        if status_code != 200:
            return JSONResponse(status_code=status_code, content=payload)
        return payload

    return _handler


for _endpoint_name in TARGETS:
    router.add_api_route(
        f"/tms/{_endpoint_name}",
        _build_handler(_endpoint_name),
        methods=["POST"],
        name=f"tms_{_endpoint_name}",
    )
