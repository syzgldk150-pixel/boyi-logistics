"""FastAPI routes for the embedded TMS runtime and session management."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from agent.api_contracts import EnvelopedRoute
from agent.execution_boundary import EXECUTION_CAPABILITY_HEADER, authorize_tms_target
from agent.orchestration.models import Actor, ActorType
from agent.tms_runtime.account_contracts import PRICE_ACCOUNT_ID
from agent.tms_runtime.dispatch import TARGETS, TaskRequest, execute_target
from agent.tms_runtime.errors import TMSAuthStateError, auth_error_payload
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.monitoring import (
    build_daily_sign_monitoring_snapshot,
    build_monitoring_detail_link,
    build_monitoring_snapshot,
)
from shared.manual_entry_contracts import (
    RONGHUI_MANUAL_PROXY_ALLOWED_PREFIXES,
    RONGHUI_MANUAL_PROXY_SAVE_PATH,
    YUNDA_MANUAL_PROXY_ALLOWED_PREFIXES,
    YUNDA_MANUAL_PROXY_SAVE_PATH,
    canonical_manual_proxy_path,
)
router = APIRouter(route_class=EnvelopedRoute)

ACCOUNT_LIST_CACHE_TTL_SEC = 60
_ACCOUNT_LIST_CACHE: dict[str, Any] = {}
_ACCOUNT_LIST_CACHE_LOCK = threading.Lock()
_ACCOUNT_LIST_REFRESHING = False
_agent_command_runtime: Any | None = None


# Third-party active HTML/JS must not execute under the Console origin.  These
# targets remain registered only so old calls fail explicitly instead of
# falling through to another compatibility route.
DIRECT_MANUAL_TARGETS = frozenset()
DISABLED_ACTIVE_ORIGINAL_PAGE_TARGETS = frozenset(
    {"yunda_waybill_entry"}
)


COMPAT_TOOL_BY_TARGET: dict[str, str] = {
    "receipts_audit": "receipts_audit",
    "receipts_sync": "receipts_sync",
    "clock_in_dual": "clock_in_dual",
    "self_pickup_problem_upload": "self_pickup_problem_upload",
    "split_pending_problem_upload": "split_pending_problem_upload",
}

CUSTOMER_SERVICE_TOOL_BY_ACTION = {
    "query": "customer_service_problem_query",
    "detail": "customer_service_problem_detail",
    "fetch_attachment": "customer_service_problem_fetch_attachment",
    "mark_read": "customer_service_problem_mark_read",
    "reply": "customer_service_problem_reply",
    "publish": "customer_service_problem_publish",
    "upload_attachment": "customer_service_problem_upload_attachment",
}


COMPAT_READ_TARGETS = frozenset(
    {
        "delivery_status",
        "get_price",
        "query_waybill_detail",
        "ronghui_tms_tracking",
        "tracking_query",
        "waybill_tracking",
        "yunda_price",
        "yunda_waybill_tracking",
    }
)


def _compatibility_action_is_read(endpoint_name: str, params: dict[str, Any]) -> bool:
    if endpoint_name in COMPAT_READ_TARGETS:
        return True
    if endpoint_name == "customer_service_problem":
        return str(params.get("action") or "query").strip().lower() in {
            "query",
            "detail",
            "fetch_attachment",
        }
    return False


def bind_agent_command_runtime(agent_runtime: Any | None) -> None:
    """Inject the AgentCore/Gateway facade from the composition root."""

    global _agent_command_runtime
    _agent_command_runtime = agent_runtime


def authorize_direct_manual_target(
    endpoint_name: str,
    params: dict[str, Any],
    *,
    console_principal_verified: bool = False,
) -> bool:
    """Allow only the reviewed isolated-origin original-page proxy contract."""

    if not console_principal_verified or endpoint_name not in {
        "ronghui_waybill_proxy",
        "yunda_waybill_proxy",
    }:
        return False
    provider = "ronghui" if endpoint_name == "ronghui_waybill_proxy" else "yunda"
    if str(params.get("proxy_prefix") or "") != f"/original/{provider}":
        return False
    method = str(params.get("method") or "GET").strip().upper()
    raw_remote_path = str(params.get("path") or "").strip()
    remote_path = canonical_manual_proxy_path(raw_remote_path) if raw_remote_path else ""
    if raw_remote_path and not remote_path:
        return False
    if method == "GET":
        if provider == "yunda":
            return remote_path.startswith(YUNDA_MANUAL_PROXY_ALLOWED_PREFIXES)
        return not remote_path or remote_path.startswith(RONGHUI_MANUAL_PROXY_ALLOWED_PREFIXES)
    if method != "POST":
        return False
    if provider == "yunda":
        return remote_path == YUNDA_MANUAL_PROXY_SAVE_PATH
    return remote_path == RONGHUI_MANUAL_PROXY_SAVE_PATH


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
            for key in (
                "auto_login_enabled",
                "auto_login_failure_count",
                "auto_login_failure_limit",
                "auto_login_blocked",
                "is_active",
            ):
                if key in status_payload:
                    account[key] = copy.deepcopy(status_payload[key])
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
    return _success_response(_account_manager().describe_status(PRICE_ACCOUNT_ID, validate=True, force=force))


@router.post("/admin/tms/price-session/send-code")
def tms_price_session_send_code():
    try:
        status = _account_manager().login(PRICE_ACCOUNT_ID)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.get("/admin/tms/price-session/credentials")
def tms_price_session_credentials():
    return _success_response(_account_manager().public_credentials(PRICE_ACCOUNT_ID))


@router.post("/admin/tms/price-session/credentials")
def tms_price_session_save_credentials(req: CredentialsRequest):
    try:
        credentials = _account_manager().save_credentials(
            PRICE_ACCOUNT_ID,
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
    credentials = _account_manager().clear_credentials(PRICE_ACCOUNT_ID)
    _invalidate_account_list_cache()
    return _success_response(credentials)


@router.post("/admin/tms/price-session/submit-code")
def tms_price_session_submit_code(req: SubmitCodeRequest):
    try:
        status = _account_manager().submit_code(PRICE_ACCOUNT_ID, req.code)
        update_account_list_cache_status(status)
        return _success_response(status)
    except TMSAuthStateError as exc:
        return JSONResponse(status_code=200, content=auth_error_payload(exc))


@router.post("/admin/tms/price-session/clear")
def tms_price_session_clear():
    status = _account_manager().clear_session(PRICE_ACCOUNT_ID)
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


def _compat_idempotency_key(request: Request) -> str:
    return str(
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Idempotency-Key")
        or ""
    ).strip()


def _read_idempotency_key(endpoint_name: str, params: dict[str, Any]) -> str:
    del endpoint_name, params
    # Compatibility reads are safe to repeat and must observe fresh source
    # state.  Callers may still provide Idempotency-Key to dedupe their own
    # retry; without one, each read receives a new Run instead of reusing a
    # stale historical result forever.
    return f"legacy-tms-read:{uuid.uuid4()}"


def _without_empty_values(params: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in params.items()
        if value is not None and (not isinstance(value, str) or value.strip())
    }


def _coerce_compatibility_integer(params: dict[str, Any], key: str) -> None:
    value = params.get(key)
    if isinstance(value, str) and value.strip().isdigit():
        params[key] = int(value.strip())


def _normalize_receipts_sync_params(params: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "platform",
        "direction",
        "date_from",
        "date_to",
        "q",
        "receipt_status",
        "date_type",
        "code_type",
        "page_size",
        "max_pages",
        "timeout_sec",
        "source_workers",
    )
    normalized = _without_empty_values({key: params.get(key) for key in allowed})
    normalized["platform"] = str(normalized.get("platform") or "all").strip().lower()
    direction = str(normalized.get("direction") or "both").strip().lower()
    normalized["direction"] = "both" if direction == "all" else direction
    for key in ("page_size", "max_pages", "timeout_sec", "source_workers"):
        _coerce_compatibility_integer(normalized, key)
    return normalized


def _normalize_receipts_audit_params(params: dict[str, Any]) -> dict[str, Any]:
    # raw_payload can contain credentials and mutable third-party fields.  A
    # governed audit must locate the current external record again by one of
    # the explicit identifiers and fail if that lookup is not unique.
    allowed = (
        "receipt_id",
        "platform",
        "direction",
        "result",
        "reason",
        "waybill_no",
        "receipt_no",
        "return_waybill_no",
    )
    return _without_empty_values({key: params.get(key) for key in allowed})


def _normalize_customer_service_params(
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Flatten the deprecated wide payload into one precise tool contract."""

    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    filters = params.get("filters") if isinstance(params.get("filters"), dict) else {}
    normalized: dict[str, Any] = {
        "platform": params.get("platform"),
        "account_id": params.get("account_id"),
        "account_label": params.get("account_label"),
    }
    if action == "query":
        for key in (
            "direction",
            "q",
            "waybill_no",
            "date_from",
            "date_to",
            "page",
            "rows",
            "page_size",
        ):
            normalized[key] = filters.get(key, params.get(key))
        for key in ("page", "rows", "page_size"):
            _coerce_compatibility_integer(normalized, key)
    elif action == "detail":
        for key in ("external_id", "source_direction", "waybill_no", "status"):
            normalized[key] = item.get(key, params.get(key))
    elif action == "mark_read":
        normalized["external_id"] = item.get("external_id", params.get("external_id"))
    elif action == "reply":
        for key in ("external_id", "source_direction", "waybill_no", "status"):
            normalized[key] = item.get(key, params.get(key))
        for key in ("reply_text", "prob_status", "old_prob_status"):
            normalized[key] = payload.get(key, params.get(key))
    elif action == "fetch_attachment":
        normalized["source_url"] = payload.get("source_url", params.get("source_url"))
    elif action == "publish":
        normalized["payload"] = payload
    elif action == "upload_attachment":
        normalized["file_path"] = payload.get("file_path", params.get("file_path"))
    return _without_empty_values(normalized)


def _normalize_clock_in_dual_params(params: dict[str, Any]) -> dict[str, Any]:
    nested = params.get("params") if isinstance(params.get("params"), dict) else {}

    def selected(*names: str) -> Any:
        for source in (params, nested):
            for name in names:
                value = source.get(name)
                if value not in (None, ""):
                    return value
        return None

    normalized = _without_empty_values(
        {
            "account_id": selected("account_id", "accountId"),
            "sitecode": selected("sitecode"),
            "sitefbcode": selected("sitefbcode"),
            "sitename": selected("sitename", "site_name"),
            "sitefbname": selected("sitefbname", "site_fb_name"),
            "first_type": selected("first_type"),
            "second_type": selected("second_type"),
            "delay_seconds": selected("delay_seconds"),
        }
    )
    delay = normalized.get("delay_seconds")
    if isinstance(delay, str):
        try:
            normalized["delay_seconds"] = float(delay.strip())
        except ValueError:
            pass
    return normalized


def _normalize_compatibility_params(
    endpoint_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    if endpoint_name == "receipts_sync":
        return _normalize_receipts_sync_params(params)
    if endpoint_name == "receipts_audit":
        return _normalize_receipts_audit_params(params)
    if endpoint_name == "customer_service_problem":
        action = str(params.get("action") or "query").strip().lower()
        return _normalize_customer_service_params(action, params)
    if endpoint_name == "clock_in_dual":
        return _normalize_clock_in_dual_params(params)
    return dict(params)


async def _submit_compat_command(
    endpoint_name: str,
    req: TaskRequest,
    request: Request,
) -> JSONResponse | dict[str, Any]:
    runtime = _agent_command_runtime
    if runtime is None:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error_code": "CONTROL_PLANE_UNAVAILABLE",
                "error": "Agent control plane is not initialized",
            },
        )

    trusted_actor = Actor(
        ActorType.LEGACY_API,
        "tms-compatibility-api",
        roles=(),
        authenticated_by="internal_api_token",
    )
    command_source = "legacy_api"
    request_path = str(getattr(getattr(request, "url", None), "path", ""))
    principal = getattr(getattr(request, "state", None), "console_principal", None)
    if request_path.startswith("/internal/v1/") and req.source == "console":
        if not isinstance(principal, dict):
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error_code": "TRUSTED_CONSOLE_ACTOR_REQUIRED",
                    "error": "a valid Console administrator session is required",
                },
            )
        trusted_actor = Actor(
            ActorType.CONSOLE_ADMIN,
            str(principal["actor_id"]),
            roles=tuple(str(role) for role in principal["roles"]),
            display_name=str(principal.get("display_name") or "")[:200],
            authenticated_by="mysql_admin_session",
        )
        command_source = "console"

    supplied_idempotency_key = str(req.idempotency_key or "").strip()
    if endpoint_name in COMPAT_READ_TARGETS:
        tool_name = "tms_query"
        params = {"endpoint": f"/{endpoint_name}", "params": dict(req.params)}
        idempotency_key = supplied_idempotency_key or _compat_idempotency_key(request) or _read_idempotency_key(
            endpoint_name,
            req.params,
        )
    else:
        if endpoint_name == "customer_service_problem":
            action = str(req.params.get("action") or "query").strip().lower()
            tool_name = CUSTOMER_SERVICE_TOOL_BY_ACTION.get(action, "")
        else:
            tool_name = COMPAT_TOOL_BY_TARGET.get(endpoint_name, "")
        params = _normalize_compatibility_params(endpoint_name, req.params)
        idempotency_key = supplied_idempotency_key or _compat_idempotency_key(request)
        if not idempotency_key and _compatibility_action_is_read(endpoint_name, req.params):
            idempotency_key = _read_idempotency_key(endpoint_name, req.params)
        if not idempotency_key:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error_code": "IDEMPOTENCY_KEY_REQUIRED",
                    "error": "write compatibility commands require Idempotency-Key",
                },
            )

    if not tool_name:
        return JSONResponse(
            status_code=410,
            content={
                "ok": False,
                "error_code": "DIRECT_TMS_ENTRY_DISABLED",
                "error": "submit this operation through /internal/v1/commands",
            },
        )

    result = await runtime.execute_tool(
        tool_name,
        params,
        actor=trusted_actor,
        source=command_source,
        idempotency_key=idempotency_key,
        execution_context={
            "compatibility_endpoint": f"/tms/{endpoint_name}",
            "deprecated": True,
        },
    )
    if not isinstance(result, dict):
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error_code": "INVALID_CONTROL_PLANE_RESPONSE",
                "error": "Agent facade returned a non-object response",
            },
        )
    return {
        "ok": bool(result.get("success")),
        "data": result.get("data") if result.get("success") else {},
        "error": result.get("error") if not result.get("success") else None,
        "error_code": result.get("error_code") if not result.get("success") else None,
        "command_id": result.get("command_id"),
        "work_item_id": result.get("work_item_id"),
        "run_id": result.get("run_id"),
        "correlation_id": result.get("correlation_id"),
        "status": result.get("status"),
        "approval": result.get("approval"),
        "next_poll_after_ms": result.get("next_poll_after_ms", 0),
        "deprecated": True,
    }


def _build_handler(endpoint_name: str):
    async def _handler(request: Request):
        try:
            raw_payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error_code": "INVALID_JSON_BODY",
                    "error": "request body must be valid JSON",
                },
            )
        if not isinstance(raw_payload, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error_code": "INVALID_REQUEST_BODY",
                    "error": "request body must be a JSON object",
                },
            )
        try:
            req = _normalize_task_request(raw_payload)
        except ValidationError:
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "INVALID_TASK_REQUEST",
                    "error": "request does not match the task contract",
                },
            )
        if endpoint_name in DISABLED_ACTIVE_ORIGINAL_PAGE_TARGETS:
            return JSONResponse(
                status_code=410,
                content={
                    "ok": False,
                    "error_code": "ACTIVE_ORIGINAL_PAGE_DISABLED",
                    "error": "third-party active original pages are disabled under the Console origin",
                },
            )
        execution_capability = str(
            request.headers.get(EXECUTION_CAPABILITY_HEADER) or ""
        ).strip()
        if authorize_tms_target(
            execution_capability,
            endpoint_name,
            request_params=req.params,
        ):
            status_code, payload = await execute_target(endpoint_name, req)
            if status_code != 200:
                return JSONResponse(status_code=status_code, content=payload)
            return payload
        if authorize_direct_manual_target(
            endpoint_name,
            req.params,
            console_principal_verified=isinstance(
                getattr(getattr(request, "state", None), "console_principal", None),
                dict,
            ),
        ):
            status_code, payload = await execute_target(endpoint_name, req)
            if status_code != 200:
                return JSONResponse(status_code=status_code, content=payload)
            return payload
        return await _submit_compat_command(endpoint_name, req, request)

    return _handler


for _endpoint_name in TARGETS:
    router.add_api_route(
        f"/tms/{_endpoint_name}",
        _build_handler(_endpoint_name),
        methods=["POST"],
        name=f"tms_{_endpoint_name}",
    )
