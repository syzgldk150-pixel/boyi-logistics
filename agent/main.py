"""FastAPI entrypoint for the logistics agent service."""

import asyncio
import logging
import os
import secrets
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from logging.handlers import TimedRotatingFileHandler

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

from shared.redaction import redact_text
from shared.contracts import api_failure, api_success
from shared.runtime_events import register_tms_session_alert


LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s %(levelname)-5s %(name)-16s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

WEBHOOK_TOKEN_HEADER = "X-Agent-Webhook-Token"


class RedactingFilter(logging.Filter):
    """Remove credentials from every message before it reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Ensure exception tracebacks receive the same redaction policy."""

    def formatException(self, exc_info) -> str:  # noqa: N802
        return redact_text(super().formatException(exc_info))


def _configure_handler(handler: logging.Handler) -> logging.Handler:
    handler.addFilter(RedactingFilter())
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


def _make_handler(filename: str) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        filename=os.path.join(LOG_DIR, filename),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    return _configure_handler(handler)


_logging_initialized = False


def setup_logging() -> None:
    global _logging_initialized
    if _logging_initialized:
        return
    _logging_initialized = True
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", LOG_LEVEL).upper())

    console = _configure_handler(logging.StreamHandler(sys.stdout))
    root.addHandler(console)

    for logger_name, filename in (
        ("agent", "agent.log"),
        ("tools", "tools.log"),
        ("feishu", "feishu.log"),
    ):
        module_logger = logging.getLogger(logger_name)
        module_logger.addHandler(_make_handler(filename))
        module_logger.addHandler(console)
        module_logger.propagate = False

    for uvicorn_logger in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(uvicorn_logger).addHandler(_make_handler("agent.log"))


logger = logging.getLogger("agent")


from agent.core import AgentCore
from agent.http_security import INTERNAL_API_TOKEN_HEADER, authenticate_internal_request
from agent.phase7_resource_import import import_phase7_resources
from agent.runtime_config import load_agent_environment
from agent.scheduler import init_scheduler, reload_scheduler
from agent.task_templates import PHASE7_SCHEDULED_TASK_TEMPLATES
from agent.tms_runtime import router as tms_router
from agent.api_contracts import validation_failure
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.monitoring import configure_feishu_operation
from agent.tms_runtime.routes import update_account_list_cache_status
from agent.tms_runtime.session_broker import get_session_broker
from agent.workflow_resource_store import get_workflow_resource, list_workflow_resources
from feishu.bot import (
    bind_agent_runtime,
    feishu_event_mode,
    start_feishu_ws,
    stop_feishu_ws,
    websocket_enabled,
    websocket_lease_active,
)
from feishu.message_handler import queue_bot_menu_payload, queue_im_message_payload
from feishu.notify import send_tms_session_disconnected_alert
from tools.feishu_cli_tool import feishu_operation
from tools.price_tool import run_price_tool
from tools.track_waybill_tool import run_track_waybill


register_tms_session_alert(send_tms_session_disconnected_alert)
configure_feishu_operation(feishu_operation)


agent_core: AgentCore | None = None
_start_time = time.time()
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
AGENT_INTERNAL_API_TOKEN = ""
TMS_SESSION_ALERT_STATUSES = {"pending_code", "expired", "logged_out", "error"}
TMS_SESSION_TRANSITION_ALERT_STATUSES = {"expired", "logged_out", "error"}
TRANSIENT_TMS_SESSION_ERROR_MARKERS = (
    "read timed out",
    "connect timeout",
    "connection timed out",
    "connection aborted",
    "connection reset",
    "connection refused",
    "failed to establish a new connection",
    "max retries exceeded",
    "name resolution",
    "network is unreachable",
    "remote disconnected",
    "temporarily unavailable",
    "temporary failure",
)


def _runtime() -> AgentCore:
    if agent_core is None:
        raise RuntimeError("Agent runtime is not initialized")
    return agent_core


def _tms_session_monitor_interval_sec() -> int:
    raw = str(os.getenv("TMS_SESSION_MONITOR_INTERVAL_SEC") or "60").strip()
    try:
        return max(15, int(raw))
    except ValueError:
        return 60


def _tms_session_alert_key(status_payload: dict) -> str:
    profile = str(status_payload.get("profile") or "default").strip()
    account_id = str(status_payload.get("account_id") or "ronghui_default").strip()
    status = str(status_payload.get("status") or "").strip()
    reason = str(status_payload.get("last_error_summary") or "").strip()
    if not reason and status == "pending_code":
        reason = str(status_payload.get("pending_since") or "").strip()
    return f"{profile}:{account_id}:{status}:{reason[:200]}"


def _tms_session_alert_state_key(status_payload: dict) -> str:
    profile = str(status_payload.get("profile") or "default").strip()
    account_id = str(status_payload.get("account_id") or "ronghui_default").strip()
    status = str(status_payload.get("status") or "").strip()
    reason = str(status_payload.get("last_error_summary") or "").strip()
    if not reason and status == "pending_code":
        reason = str(status_payload.get("pending_since") or "").strip()
    return f"{profile}:{account_id}:{status}:{reason[:200]}"


def _is_transient_tms_session_error_text(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in TRANSIENT_TMS_SESSION_ERROR_MARKERS)


def _is_transient_tms_session_status(status_payload: dict) -> bool:
    status = str(status_payload.get("status") or "").strip()
    if status != "error":
        return False
    reason = str(status_payload.get("last_error_summary") or "").strip()
    return _is_transient_tms_session_error_text(reason)


def _should_alert_tms_session_status(status_payload: dict) -> bool:
    status = str(status_payload.get("status") or "").strip()
    if status not in TMS_SESSION_ALERT_STATUSES:
        return False
    if _is_transient_tms_session_status(status_payload):
        return False
    return True


def _default_ronghui_status_payload(status_payload: dict) -> dict:
    payload = dict(status_payload)
    payload.setdefault("profile", "default")
    payload.setdefault("account_id", "ronghui_default")
    payload.setdefault("account_name", "TMS融辉默认账号")
    payload.setdefault("system", "ronghui")
    payload.setdefault("system_label", "TMS融辉")
    return payload


def _status_payload_with_account_context(account: dict, status_payload: dict) -> dict:
    payload = dict(status_payload or {})
    payload.setdefault("account_id", str(account.get("account_id") or "").strip())
    payload.setdefault(
        "account_name",
        str(account.get("account_name") or account.get("name") or payload.get("account_id") or "").strip(),
    )
    payload.setdefault("system", str(account.get("system") or "").strip())
    payload.setdefault("system_label", str(account.get("system_label") or payload.get("system") or "").strip())
    payload.setdefault("session_profile", str(account.get("session_profile") or "").strip())
    payload.setdefault("session_capable", bool(account.get("session_capable", False)))
    return payload


def _error_status_payload_for_account(account: dict, exc: Exception) -> dict:
    return _status_payload_with_account_context(
        account,
        {
            "status": "error",
            "label": "异常",
            "status_tone": "error",
            "authenticated": False,
            "pending_code": False,
            "last_validation_at": "",
            "last_error_summary": redact_text(exc)[:300],
            "authenticated_at": "",
            "pending_since": "",
            "expires_at": "",
        },
    )


def _account_alert_identity(status_payload: dict) -> str:
    account_id = str(status_payload.get("account_id") or "").strip()
    if account_id:
        return f"account:{account_id}"
    profile = str(status_payload.get("profile") or "default").strip()
    return f"profile:{profile}"


def _should_start_tms_session_alert_monitor() -> bool:
    if not websocket_enabled():
        return True
    return websocket_lease_active()


def _check_tms_account_session(account_manager, account: dict) -> dict:
    if not bool(account.get("is_active", True)) or not bool(account.get("session_capable", False)):
        return {"monitored": False, "should_alert": False, "status_payload": {}, "previous": {}}

    account_id = str(account.get("account_id") or "").strip()
    if not account_id:
        return {"monitored": False, "should_alert": False, "status_payload": {}, "previous": {}}

    previous: dict = {}
    try:
        previous = account_manager.describe_status(account_id, validate=False)
        status_payload = account_manager.check_status_with_auto_login(account_id, force=True)
        status_payload = _status_payload_with_account_context(account, status_payload)
        with suppress(Exception):
            update_account_list_cache_status(status_payload)
        status = str(status_payload.get("status") or "").strip()

        if status == "authenticated":
            return {
                "monitored": True,
                "should_alert": False,
                "status_payload": status_payload,
                "previous": previous,
            }

        return {
            "monitored": True,
            "should_alert": _should_alert_tms_session_status(status_payload),
            "status_payload": status_payload,
            "previous": previous,
        }
    except Exception as exc:
        status_payload = _error_status_payload_for_account(account, exc)
        with suppress(Exception):
            update_account_list_cache_status(status_payload)
        should_alert = _should_alert_tms_session_status(status_payload)
        if not should_alert:
            logger.info(
                "TMS session transient check error suppressed: account=%s reason=%s",
                status_payload.get("account_id") or "-",
                status_payload.get("last_error_summary") or "",
            )
        return {
            "monitored": True,
            "should_alert": should_alert,
            "status_payload": status_payload,
            "previous": previous,
        }


async def _monitor_tms_session_alerts(stop_event: asyncio.Event) -> None:
    """Poll enabled automation accounts and alert Feishu only when login needs attention."""
    last_alert_key_by_account: dict[str, str] = {}
    last_alert_state_key_by_account: dict[str, str] = {}
    interval_sec = _tms_session_monitor_interval_sec()

    while not stop_event.is_set():
        try:
            account_manager = get_account_manager()
            accounts = await asyncio.to_thread(account_manager.list_accounts, include_status=False)
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                check_result = await asyncio.to_thread(_check_tms_account_session, account_manager, account)
                if not check_result.get("monitored"):
                    continue
                status_payload = check_result.get("status_payload") or {}
                previous = check_result.get("previous") or {}
                status = str(status_payload.get("status") or "").strip()
                identity = _account_alert_identity(status_payload)

                if status == "authenticated":
                    last_alert_key_by_account.pop(identity, None)
                    last_alert_state_key_by_account.pop(identity, None)
                    continue
                if not check_result.get("should_alert"):
                    continue

                alert_key = _tms_session_alert_key(status_payload)
                alert_state_key = _tms_session_alert_state_key(status_payload)
                transition_alert_started = (
                    str(previous.get("status") or "").strip() == "authenticated"
                    and status in TMS_SESSION_TRANSITION_ALERT_STATUSES
                )
                if transition_alert_started or (
                    alert_key != last_alert_key_by_account.get(identity)
                    and alert_state_key != last_alert_state_key_by_account.get(identity)
                ):
                    sent = await asyncio.to_thread(send_tms_session_disconnected_alert, status_payload)
                    if sent:
                        logger.info(
                            "TMS session alert sent: account=%s status=%s",
                            status_payload.get("account_id") or "-",
                            status,
                        )
                        last_alert_key_by_account[identity] = alert_key
                        last_alert_state_key_by_account[identity] = alert_state_key
                    else:
                        logger.warning(
                            "TMS session alert skipped or failed: account=%s status=%s",
                            status_payload.get("account_id") or "-",
                            status,
                        )
        except Exception:
            logger.warning("TMS session alert monitor failed", exc_info=True)

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global AGENT_INTERNAL_API_TOKEN, agent_core
    load_agent_environment()
    setup_logging()
    AGENT_INTERNAL_API_TOKEN = str(os.getenv("AGENT_INTERNAL_API_TOKEN", "") or "").strip()
    if not AGENT_INTERNAL_API_TOKEN:
        raise RuntimeError("AGENT_INTERNAL_API_TOKEN is required")
    agent_core = AgentCore(
        direct_tool_runners={
            "track_waybill": run_track_waybill,
            "get_price": run_price_tool,
        }
    )
    runtime = _runtime()
    logger.info("Agent service starting instance_id=%s pid=%s", INSTANCE_ID, os.getpid())

    await runtime.init()
    bind_agent_runtime(runtime, asyncio.get_running_loop())

    scheduler = init_scheduler(runtime)
    scheduler.start()
    logger.info("APScheduler started with %d jobs", len(scheduler.get_jobs()))

    if websocket_enabled():
        await start_feishu_ws(runtime)
    else:
        logger.info("Feishu webhook mode enabled: %s", feishu_event_mode())

    tms_session_alert_stop = asyncio.Event()
    tms_session_alert_task = None
    if _should_start_tms_session_alert_monitor():
        tms_session_alert_task = asyncio.create_task(_monitor_tms_session_alerts(tms_session_alert_stop))
    else:
        logger.info("TMS session alert monitor skipped because this instance does not hold the Feishu lease.")

    logger.info("Agent service started instance_id=%s", INSTANCE_ID)
    yield

    logger.info("Agent service shutting down instance_id=%s", INSTANCE_ID)
    if tms_session_alert_task is not None:
        tms_session_alert_stop.set()
        tms_session_alert_task.cancel()
        with suppress(asyncio.CancelledError):
            await tms_session_alert_task
    scheduler.shutdown(wait=False)
    await stop_feishu_ws()
    await runtime.close()
    agent_core = None
    logger.info("Agent service stopped")


app = FastAPI(title="Logistics Agent", version="0.1.0", lifespan=lifespan)
app.include_router(tms_router, prefix="/internal/v1")


@app.exception_handler(RequestValidationError)
async def internal_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/internal/v1/"):
        return JSONResponse(status_code=422, content=validation_failure(exc))
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(HTTPException)
async def internal_http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/internal/v1/"):
        return JSONResponse(
            status_code=exc.status_code,
            content=api_failure(f"http_{exc.status_code}", redact_text(exc.detail)),
            headers=exc.headers,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def internal_unhandled_error(request: Request, exc: Exception):
    if request.url.path.startswith("/internal/v1/"):
        logger.error("Internal API request failed path=%s error=%s", request.url.path, redact_text(exc))
        return JSONResponse(
            status_code=500,
            content=api_failure("internal_server_error", "Internal server error"),
        )
    raise exc


@app.middleware("http")
async def require_internal_api_token(request: Request, call_next):
    failure = authenticate_internal_request(
        path=request.url.path,
        expected_token=AGENT_INTERNAL_API_TOKEN,
        provided_token=request.headers.get(INTERNAL_API_TOKEN_HEADER, ""),
    )
    if failure:
        return JSONResponse(
            status_code=failure.status_code,
            content={
                "ok": False,
                "data": None,
                "error": {"code": "internal_auth_failed", "message": failure.message},
            },
        )
    return await call_next(request)


def _webhook_token() -> str:
    return str(
        os.getenv("DOCFLOW_AGENT_WEBHOOK_TOKEN", "") or os.getenv("AGENT_WEBHOOK_TOKEN", "")
    ).strip()


async def _verify_webhook_token(request: Request) -> None:
    expected_token = _webhook_token()
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Webhook token not configured. Set DOCFLOW_AGENT_WEBHOOK_TOKEN or AGENT_WEBHOOK_TOKEN.",
        )

    provided_token = str(request.headers.get(WEBHOOK_TOKEN_HEADER, "") or "").strip()
    if not provided_token:
        raise HTTPException(
            status_code=401,
            detail=f"Missing webhook token header: {WEBHOOK_TOKEN_HEADER}",
        )

    if not secrets.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid webhook token")


async def _webhook_payload(request: Request) -> dict:
    payload: dict = {"query": dict(request.query_params)}
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        payload.update(body)
    return payload


def _phase7_webhook_tool(path: str) -> str | None:
    normalized_path = path.strip("/")
    mapping = {
        "phase7.delivery_status_webhook": "sync_delivery_status",
        "phase7.scan_webhook": "sync_scan_codes",
        "phase7.stats_webhook": "sync_arrival_stats",
    }
    for resource_key, tool_name in mapping.items():
        resource = get_workflow_resource(resource_key)
        resource_path = str((resource or {}).get("path") or "").strip("/")
        if resource_path and resource_path == normalized_path:
            return tool_name
    return None


def _feishu_verification_token() -> str:
    return str(
        os.getenv("FEISHU_EVENT_VERIFICATION_TOKEN", "") or os.getenv("FEISHU_VERIFICATION_TOKEN", "")
    ).strip()


def _verify_feishu_event_token(payload: dict) -> None:
    expected_token = _feishu_verification_token()
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Feishu event verification token is not configured",
        )

    header = payload.get("header") or {}
    provided_token = str(payload.get("token") or header.get("token") or "").strip()
    if not provided_token:
        raise HTTPException(status_code=401, detail="Missing Feishu verification token")

    if not secrets.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid Feishu verification token")


def _feishu_event_type(payload: dict) -> str:
    header = payload.get("header") or {}
    return str(header.get("event_type") or payload.get("type") or "").strip()


@app.post("/feishu/webhook/event")
async def feishu_event_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Feishu webhook payload must be a JSON object")

    _verify_feishu_event_token(payload)

    event_type = _feishu_event_type(payload)
    if event_type == "url_verification":
        challenge = str(payload.get("challenge", "") or "").strip()
        if not challenge:
            raise HTTPException(status_code=400, detail="Missing challenge in Feishu webhook payload")
        return {"challenge": challenge}

    if payload.get("encrypt") and not payload.get("event"):
        raise HTTPException(
            status_code=400,
            detail="Encrypted Feishu webhook payload is not supported. Disable Encrypt Key for this webhook.",
        )

    event_body = payload.get("event")
    if not isinstance(event_body, dict):
        logger.info("Ignored Feishu webhook payload without event body: type=%s", event_type or "unknown")
        return {"code": 0, "msg": "ignored"}

    if event_type == "im.message.receive_v1":
        accepted = queue_im_message_payload(event_body)
    elif event_type == "application.bot.menu_v6":
        accepted = queue_bot_menu_payload(event_body)
    else:
        logger.info("Ignored unsupported Feishu webhook event: %s", event_type or "unknown")
        return {"code": 0, "msg": "ignored"}

    return {"code": 0, "msg": "ok" if accepted else "ignored"}


@app.post("/webhook/sign-status")
async def webhook_sign_status(request: Request):
    await _verify_webhook_token(request)
    return await _runtime().execute_tool("sync_delivery_status", await _webhook_payload(request))


@app.post("/webhook/{path:path}")
async def webhook_handler(path: str, request: Request):
    await _verify_webhook_token(request)
    tool_name = _phase7_webhook_tool(path)
    if tool_name:
        logger.info("Webhook migrated route hit: /%s -> %s", path, tool_name)
        return await _runtime().execute_tool(tool_name, await _webhook_payload(request))
    logger.info("Webhook request received: /%s", path)
    return {"status": "ok", "message": "webhook endpoint placeholder"}


@app.get("/health")
async def health():
    """Minimal unauthenticated liveness response."""

    return {"ok": True, "status": "ok", "release_sha": _release_sha()}


def _release_sha() -> str:
    configured = str(os.getenv("AGENT_RELEASE_SHA", "") or "").strip()
    if configured:
        return configured
    release_file = os.path.join(PROJECT_ROOT, "runtime", "release_sha")
    try:
        with open(release_file, encoding="utf-8") as handle:
            value = handle.read(128).strip()
    except OSError:
        value = ""
    return value or "development"


@app.get("/internal/v1/health")
async def internal_health():
    runtime = _runtime()
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 / 1024

    uptime_sec = int(time.time() - _start_time)
    days, rem = divmod(uptime_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"

    return api_success(
        {
            "status": "ok",
            "release_sha": _release_sha(),
            "instance_id": INSTANCE_ID,
            "uptime": uptime_str,
            "memory_mb": round(mem_mb, 1),
            "components": {
                "feishu_mode": feishu_event_mode(),
                "feishu_ws": runtime.feishu_status(),
                "deepseek": runtime.llm_status("deepseek"),
                "glm": runtime.llm_status("glm"),
                "mysql": runtime.db_status(),
                "tms_session": get_session_broker().describe_status(validate=False),
            },
            "last_tool_run": runtime.last_tool_info(),
            "heavy_task_lock": runtime.heavy_lock_held(),
        }
    )


class ChatRequest(BaseModel):
    message: str
    user_id: str = "console"
    conversation_id: str | None = None


async def _chat(req: ChatRequest):
    return await _runtime().handle_message(
        message=req.message,
        user_id=req.user_id,
        conversation_id=req.conversation_id,
    )


@app.post("/internal/v1/chat")
async def internal_chat(req: ChatRequest):
    return api_success(await _chat(req))


class ToolRequest(BaseModel):
    tool_name: str
    params: dict = Field(default_factory=dict)


class CancelToolRequest(BaseModel):
    tool_name: str
    started_at: str = ""


class KnowledgeRequest(BaseModel):
    content: str
    category: str | None = None
    source: str | None = None


@app.get("/internal/v1/tools")
async def internal_list_tools():
    return api_success({"tools": _runtime().registry.list_tools()})


@app.post("/internal/v1/tools/run")
async def internal_run_tool(req: ToolRequest):
    result = await _runtime().execute_tool(req.tool_name, req.params)
    if isinstance(result, dict) and result.get("success") is False:
        return api_failure(
            str(result.get("error_code") or "tool_execution_failed"),
            str(result.get("error") or "Tool execution failed"),
            data=result,
        )
    return api_success(result)


@app.post("/internal/v1/tools/cancel")
async def internal_cancel_tool(req: CancelToolRequest):
    result = await _runtime().cancel_tool(req.tool_name, req.started_at)
    if isinstance(result, dict) and result.get("ok") is False:
        return api_failure(
            str(result.get("code") or "tool_cancel_failed"),
            str(result.get("message") or "Tool cancellation failed"),
            data=result,
        )
    return api_success(result)


async def _reload_runtime():
    runtime = _runtime()
    result = runtime.reload_runtime_config()
    logger.info("Runtime configuration reloaded")
    scheduler = reload_scheduler(runtime)
    return {"status": "ok", **result, "scheduler": scheduler}


@app.post("/internal/v1/admin/reload")
async def internal_reload_runtime():
    return api_success(await _reload_runtime())


async def _add_knowledge(req: KnowledgeRequest):
    record_id = _runtime().memory.add_knowledge(
        content=req.content,
        category=req.category,
        source=req.source,
    )
    return {"status": "ok", "id": record_id}


@app.post("/internal/v1/knowledge")
async def internal_add_knowledge(req: KnowledgeRequest):
    return api_success(await _add_knowledge(req))


async def _search_knowledge(q: str, limit: int = 5):
    rows = _runtime().memory.search_knowledge(q, limit=max(1, min(limit, 20)))
    return {"query": q, "results": rows}


@app.get("/internal/v1/knowledge/search")
async def internal_search_knowledge(q: str, limit: int = 5):
    return api_success(await _search_knowledge(q, limit))


async def _get_tool_output(tool_name: str, offset: int = 0, started_at: str = ""):
    """获取工具的实时 shell 输出"""
    return _runtime().executor.get_running_output(
        tool_name,
        offset=max(0, offset),
        started_at=started_at,
    )


@app.get("/internal/v1/tool-output/{tool_name}")
async def internal_get_tool_output(tool_name: str, offset: int = 0, started_at: str = ""):
    return api_success(await _get_tool_output(tool_name, offset, started_at))


async def _get_tool_logs(limit: int = 20, tool_name: str | None = None, success: bool | None = None):
    rows = _runtime().memory.get_tool_logs(
        limit=max(1, min(limit, 100)),
        tool_name=tool_name,
        success=success,
    )
    return {
        "limit": max(1, min(limit, 100)),
        "tool_name": tool_name,
        "success": success,
        "rows": rows,
    }


@app.get("/internal/v1/tool-logs")
async def internal_get_tool_logs(limit: int = 20, tool_name: str | None = None, success: bool | None = None):
    return api_success(await _get_tool_logs(limit, tool_name, success))


@app.get("/internal/v1/scheduled-tasks")
async def internal_scheduled_tasks():
    return api_success({"rows": _runtime().memory.list_scheduled_tasks()})


async def _seed_phase7_tasks():
    runtime = _runtime()
    seeded = []
    for task in PHASE7_SCHEDULED_TASK_TEMPLATES:
        runtime.memory.upsert_scheduled_task(task)
        seeded.append(task["id"])
    logger.info("Seeded Phase 7 schedule templates: %s", ", ".join(seeded))
    scheduler = reload_scheduler(runtime)
    return {"status": "ok", "seeded": seeded, "scheduler": scheduler}


@app.post("/internal/v1/admin/seed-phase7-tasks")
async def internal_seed_phase7_tasks():
    return api_success(await _seed_phase7_tasks())


@app.get("/internal/v1/workflow-resources")
async def internal_workflow_resources():
    return api_success({"rows": list_workflow_resources()})


async def _import_phase7_resource_configs():
    imported = import_phase7_resources()
    logger.info("Imported Phase 7 workflow resources into MySQL: %s", ", ".join(imported))
    return {"status": "ok", "imported": imported}


@app.post("/internal/v1/admin/import-phase7-resources")
async def internal_import_phase7_resource_configs():
    return api_success(await _import_phase7_resource_configs())


if __name__ == "__main__":
    import uvicorn

    load_agent_environment()
    uvicorn.run(
        "main:app",
        host=os.getenv("AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_PORT", "9000")),
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )
