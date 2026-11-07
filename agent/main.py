"""FastAPI entrypoint for the logistics agent service."""

from __future__ import annotations

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
from typing import Any

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
from shared.finance import FinanceRepository
from shared.redaction import redact_text
from shared.contracts import api_failure, api_success
from shared.plugin_invocation_repository import PluginInvocationRepository
from agent.automation_plugins.direct_invocation import DirectPluginInvocationService
from agent.plugin_business_results import PluginBusinessResults
from agent.tms_runtime.routes import bind_direct_business_service
from business_composition import build_business_service
from shared.runtime_events import (
    register_account_session_restored,
    register_finance_alert,
    register_tms_session_alert,
)
from shared.service_identity import (
    ConsoleIdentityError,
    ConsoleIdentityVerifier,
    validate_service_identity_secrets,
)


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
from agent.direct_tool_router import is_reserved_feishu_command_text
from agent.business_query import (
    AutomationOperationsQueryService,
    BusinessFinanceQueryService,
    MySQLAutomationOperationsRepository,
)
from agent.business_modules_api import create_business_module_router
from agent.automation_plugins.production import (
    ProductionAutomationPluginRuntime,
    build_production_automation_plugin_runtime,
    production_cursor_secret,
)
from agent.automation_plugins.management_api import create_automation_plugin_management_router
from agent.harness import HarnessError
from agent.harness_api import create_harness_router, harness_error_response
from agent.orchestration.automation_project_api import create_automation_project_router
from agent.orchestration.automation_project_entrypoints import (
    AutomationProjectEntrypoints,
    ServiceV2FeishuDispatcher,
    TrustedDynamicArgumentResolver,
)
from agent.orchestration.automation_project_policy_service import AutomationProjectPolicyService
from agent.orchestration.feishu_binding_service import FeishuBindingService
from agent.orchestration.service_v2_waybill_entry_extension_host import ServiceV2WaybillEntryExtensionHost
from agent.orchestration.models import (
    Actor,
    ActorType,
    OrchestrationError,
)
from agent.orchestration.result_verifier import ResultVerifier
from agent.llm_settings import (
    LLMCompatibilityService,
    LLMSettingsError,
    LLMSettingsRepository,
)
from agent.http_security import INTERNAL_API_TOKEN_HEADER, authenticate_internal_request
from agent.execution_boundary import EXECUTION_CAPABILITY_HEADER, authorize_tms_target
from agent.phase7_resource_import import (
    import_phase7_resources,
    repair_missing_fixed_automation_routes,
    sync_reviewed_phase7_resource_metadata,
)
from agent.runtime_config import load_agent_environment
from harness_composition import build_read_only_harness_gateway, build_chat_text_queries
from shared.identity_repository import IdentityRepository
from agent.identity_access import authorize_console_request
from agent.service_v2_process_runtime import (
    ServiceV2ProcessRuntime,
    service_v2_harness_conversations,
    service_v2_harness_status,
    service_v2_harness_tools,
)
from agent.scheduler import (
    begin_scheduler_release_activation,
    consume_scheduler_release_hold,
    emergency_withdraw_automation_project_jobs,
    init_scheduler,
    pause_scheduler_for_release,
    release_missed_scheduled_task_ids,
    reload_scheduler,
    run_scheduled_tasks_now,
    scheduler_release_hold_requested,
    scheduler_runtime_status,
    sync_project_jobs,
)
from agent.tms_runtime import router as tms_router
from agent.api_contracts import validation_failure
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.errors import TMSAuthStateError
from agent.tms_runtime.monitoring import configure_feishu_operation
from agent.tms_runtime.routes import ensure_account_list_cache
from agent.tms_runtime.routes import update_account_list_cache_status
from agent.tms_runtime.session_broker import get_session_broker
from agent.workflow_resource_store import get_saved_workflow_resource, list_workflow_resources
from agent.tool_executor import ToolExecutor
from feishu.bot import (
    bind_agent_runtime,
    feishu_event_mode,
    start_feishu_ws,
    stop_feishu_ws,
    websocket_enabled,
    websocket_lease_active,
)
from feishu.message_handler import (
    bind_automation_project_entrypoints,
    bind_feishu_binding_runtime,
    bind_service_v2_feishu_dispatcher,
    queue_bot_menu_payload,
    queue_im_message_payload,
)
from feishu.notify import (
    send_finance_anomaly_alert,
    send_tms_session_disconnected_alert,
)
from tools.feishu_cli_tool import feishu_operation
from tools.track_waybill_tool import run_track_waybill
from plugin_core_adapters import build_production_host_primitive_handler_map
from shared.orchestration_repository import (
    OrchestrationPersistenceError,
    OrchestrationRepository,
)
from shared.business_module_repository import BusinessModuleLifecycleService, BusinessModuleRepository


register_tms_session_alert(send_tms_session_disconnected_alert)
register_finance_alert(send_finance_anomaly_alert)
configure_feishu_operation(feishu_operation)


agent_core: AgentCore | None = None
automation_project_policy_service: AutomationProjectPolicyService | None = None
automation_plugin_runtime: ProductionAutomationPluginRuntime | None = None
automation_project_entrypoints: AutomationProjectEntrypoints | None = None
feishu_binding_service: FeishuBindingService | None = None
_start_time = time.time()
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
AGENT_INTERNAL_API_TOKEN = ""
CONSOLE_IDENTITY_VERIFIER: ConsoleIdentityVerifier | None = None
RELEASE_ACTIVATION_LOCK = asyncio.Lock()
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


def _automation_project_policies() -> AutomationProjectPolicyService:
    if automation_project_policy_service is None:
        raise RuntimeError("Automation project policy service is not initialized")
    return automation_project_policy_service


def _automation_project_entrypoint_service() -> AutomationProjectEntrypoints:
    if automation_project_entrypoints is None:
        raise RuntimeError("Automation project entrypoints are not initialized")
    return automation_project_entrypoints


def _feishu_bindings() -> FeishuBindingService:
    if feishu_binding_service is None:
        raise RuntimeError("Feishu binding service is not initialized")
    return feishu_binding_service


def _automation_plugins() -> ProductionAutomationPluginRuntime:
    if automation_plugin_runtime is None:
        raise RuntimeError("Automation plugin runtime is not initialized")
    return automation_plugin_runtime


def _automation_plugin_health() -> dict[str, Any]:
    runtime = automation_plugin_runtime
    if runtime is None:
        return {
            "ok": False,
            "broker": {"state": "stopped"},
            "catalog": {"ok": False},
            "generations": {"healthy": False},
            "error_code": "AUTOMATION_PLUGIN_RUNTIME_UNAVAILABLE",
        }
    try:
        if scheduler_release_hold_requested():
            return runtime.health_snapshot()
        return runtime.health()
    except Exception as exc:
        logger.error(
            "Automation plugin health failed error=%s",
            redact_text(exc),
        )
        return {
            "ok": False,
            "broker": {"state": "running" if runtime.started else "stopped"},
            "catalog": {"ok": False},
            "generations": {"healthy": False},
            "error_code": getattr(exc, "code", type(exc).__name__.upper())[:64],
        }


def _orchestration_connection():
    """Return a transaction-scoped MySQL connection; runtime never runs DDL."""

    import pymysql
    from shared.mysql_connection import mysql_tls_options

    return pymysql.connect(
        host=os.getenv("AGENT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_DB_PORT", "3306")),
        user=os.getenv("AGENT_DB_USER", "agent"),
        password=os.getenv("AGENT_DB_PASS", ""),
        database=os.getenv("AGENT_DB_NAME", "agent_db"),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        **mysql_tls_options(os.getenv("AGENT_DB_SSL_CA")),
    )


def _actor_from_payload(
    payload: dict | None,
    actor_roles: list[str] | None,
    source: str,
    *,
    fallback_id: str = "legacy-api",
) -> Actor:
    values = payload if isinstance(payload, dict) else {}
    source_value = str(source or "legacy_api").strip()
    default_types = {
        "console": ActorType.CONSOLE_ADMIN,
        "feishu": ActorType.FEISHU_USER,
        "scheduler": ActorType.SCHEDULER,
        "webhook": ActorType.WEBHOOK,
        "system": ActorType.SYSTEM,
    }
    raw_type = str(values.get("actor_type") or default_types.get(source_value, ActorType.LEGACY_API).value)
    try:
        actor_type = ActorType(raw_type)
    except ValueError as exc:
        raise OrchestrationError("INVALID_ACTOR", "Unknown actor_type") from exc
    expected_type = default_types.get(source_value)
    if expected_type is not None and actor_type is not expected_type:
        raise OrchestrationError("ACTOR_SOURCE_MISMATCH", "Actor type does not match command source")
    roles_value = values.get("roles") if isinstance(values.get("roles"), list) else actor_roles or []
    return Actor(
        actor_type=actor_type,
        actor_id=str(values.get("actor_id") or fallback_id),
        roles=tuple(str(role) for role in roles_value),
        display_name=str(values.get("display_name") or ""),
        authenticated_by=str(values.get("authenticated_by") or ""),
    )


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
    if status_payload.get("auto_login_retryable"):
        return False
    if status_payload.get("auto_login_enabled") is False:
        return False
    if bool(status_payload.get("account_disabled")):
        return False
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


def _check_tms_account_session(
    account_manager,
    account: dict,
    account_list_snapshot: list[dict] | None = None,
) -> dict:
    if (
        not bool(account.get("is_active", True))
        or not bool(account.get("session_capable", False))
        or not bool(account.get("auto_login_enabled", False))
        or bool(account.get("auto_login_blocked", False))
    ):
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
            if account_list_snapshot is not None:
                ensure_account_list_cache(account_list_snapshot)
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
        if isinstance(exc, TMSAuthStateError) and exc.code == "BLOCKED_LOGIN":
            logger.info(
                "TMS session check skipped because the account is already being checked: account=%s",
                account_id,
            )
            return {
                "monitored": True,
                "check_skipped": True,
                "should_alert": False,
                "status_payload": _status_payload_with_account_context(account, previous),
                "previous": previous,
            }
        status_payload = _error_status_payload_for_account(account, exc)
        with suppress(Exception):
            if account_list_snapshot is not None:
                ensure_account_list_cache(account_list_snapshot)
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
            accounts = await asyncio.to_thread(
                account_manager.list_accounts,
                include_status=True,
                validate=False,
                force=False,
            )
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                check_result = await asyncio.to_thread(
                    _check_tms_account_session,
                    account_manager,
                    account,
                    accounts,
                )
                if not check_result.get("monitored"):
                    continue
                if check_result.get("check_skipped"):
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
    global AGENT_INTERNAL_API_TOKEN, CONSOLE_IDENTITY_VERIFIER, agent_core
    global automation_plugin_runtime, automation_project_policy_service
    global automation_project_entrypoints
    global feishu_binding_service
    load_agent_environment()
    setup_logging()
    AGENT_INTERNAL_API_TOKEN = str(os.getenv("AGENT_INTERNAL_API_TOKEN", "") or "").strip()
    console_signing_secret = str(
        os.getenv("CONSOLE_AGENT_SIGNING_SECRET", "") or ""
    ).strip()
    validate_service_identity_secrets(
        internal_api_token=AGENT_INTERNAL_API_TOKEN,
        console_signing_secret=console_signing_secret,
    )
    CONSOLE_IDENTITY_VERIFIER = ConsoleIdentityVerifier(console_signing_secret)
    agent_core = AgentCore(
        direct_tool_runners={
            "track_waybill": run_track_waybill,
        }
    )
    runtime = _runtime()
    loop = asyncio.get_running_loop()
    logger.info("Agent service starting instance_id=%s pid=%s", INSTANCE_ID, os.getpid())

    await runtime.init()
    repository = OrchestrationRepository(_orchestration_connection)
    mysql_version = await asyncio.to_thread(repository.validate_mysql8)
    await asyncio.to_thread(repository.validate_schema)
    logger.info("Orchestration persistence ready mysql=%s", mysql_version)

    repaired_routes = await asyncio.to_thread(
        repair_missing_fixed_automation_routes
    )
    if repaired_routes:
        logger.warning(
            "Missing fixed automation routes repaired count=%d routes=%s",
            len(repaired_routes),
            ",".join(sorted(repaired_routes)),
        )

    tool_executor = ToolExecutor()
    core_catalog = runtime.registry
    account_manager = get_account_manager()
    cursor_secret = production_cursor_secret(os.environ)
    from agent.automation_plugins.reviewed_primitives import REVIEWED_HOST_PRIMITIVES
    plugin_handlers = build_production_host_primitive_handler_map(
        account_manager=account_manager,
        cursor_secret=cursor_secret,
        allowed_action_keys=REVIEWED_HOST_PRIMITIVES,
    )
    plugin_runtime = await asyncio.to_thread(
        build_production_automation_plugin_runtime,
        orchestration_repository=repository,
        core_catalog=core_catalog,
        core_executor=tool_executor,
        account_manager=account_manager,
        broker_handlers=plugin_handlers,
        runtime_release_sha=_release_sha(),
        environ=os.environ,
        release_hold_provider=scheduler_release_hold_requested,
        reserved_feishu_command=is_reserved_feishu_command_text,
    )
    await plugin_runtime.start()
    automation_plugin_runtime = plugin_runtime
    catalog = plugin_runtime.composite_catalog
    agent_core.configure_tool_catalog(catalog)
    business_finance_query = BusinessFinanceQueryService(FinanceRepository(runtime.memory.connection_factory))
    automation_operations_query = AutomationOperationsQueryService(
        MySQLAutomationOperationsRepository(runtime.memory.connection_factory)
    )
    runtime.configure_direct_readers({
        "query_business_finance": business_finance_query.run,
        "query_automation_operations": automation_operations_query.run,
    })
    plugin_business_results = PluginBusinessResults(repository)
    direct_invocations = DirectPluginInvocationService(
        PluginInvocationRepository(repository),
        plugin_runtime.execution_router,
        ResultVerifier(plugin_runtime.runtime_repository),
        release_hold_provider=scheduler_release_hold_requested,
        saved_resource_provider=get_saved_workflow_resource,
        account_validator=account_manager.require_active_binding_descriptor,
        prepare_arguments=plugin_business_results.prepare,
        publish_result=plugin_business_results.publish,
    )
    await direct_invocations.startup()
    runtime.configure_direct_readers({}, invocations=direct_invocations)
    account_manager.set_credentials_change_guard(direct_invocations.begin_credentials_change)
    business_service = build_business_service(
        catalog=core_catalog, plugin_catalog=plugin_runtime.catalog, invocations=direct_invocations)
    bind_direct_business_service(business_service)
    project_policy_service = AutomationProjectPolicyService(
        repository,
        plugin_runtime.catalog,
        dynamic_resolver=TrustedDynamicArgumentResolver(),
        release_hold_provider=scheduler_release_hold_requested,
        contribution_registry=plugin_runtime.contribution_registry,
        direct_invocations=direct_invocations,
    )
    # Construct a stopped Scheduler and bind its strict projection callbacks
    # before runtime generation recovery. A PENDING_PROJECTION generation may
    # only be acknowledged after the exact Console/Scheduler contribution set
    # has been refreshed; BLOCKED recovery must install its project tombstone
    # before any physical trigger can run.
    release_hold = scheduler_release_hold_requested()
    scheduler = init_scheduler(
        runtime,
        automation_project_invoker=project_policy_service,
        include_startup_catchup=False,
    )
    plugin_runtime.service_effect_driver.bind_scheduler_projection_refresher(
        lambda: reload_scheduler(
            runtime,
            automation_project_invoker=project_policy_service,
            strict=True,
        )
    )
    plugin_runtime.service_effect_driver.bind_scheduler_project_emergency_withdrawer(
        emergency_withdraw_automation_project_jobs,
    )
    plugin_runtime.service_effect_driver.bind_scheduler_project_syncer(
        lambda automation_id: sync_project_jobs(
            runtime,
            automation_id,
            automation_project_invoker=project_policy_service,
        )
    )
    await asyncio.to_thread(plugin_runtime.reconcile)
    await asyncio.to_thread(plugin_runtime.health)
    default_full_auto = {"changed": 0}
    if scheduler_release_hold_requested():
        default_full_auto = await asyncio.to_thread(
            project_policy_service.ensure_default_full_auto_policies
        )
    logger.info(
        "Automation project durable full-auto defaults changed=%d",
        default_full_auto.get("changed", 0),
    )
    automation_project_policy_service = project_policy_service
    project_policy_service.identity_access = IdentityRepository(_orchestration_connection)
    agent_core.identity_access = project_policy_service.identity_access
    feishu_binding_service = FeishuBindingService(
        repository,
        identity_access=project_policy_service.identity_access,
    )
    from agent.plugin_conversations import PluginConversationService
    plugin_conversations = PluginConversationService(project_policy_service)
    agent_core.configure_plugin_conversations(plugin_conversations)
    from agent.orchestration.automation_project_entrypoints import ServiceV2WebhookDispatcher
    plugin_runtime.fixed_entrypoints.bind_webhook_resolver(
        plugin_runtime.contribution_registry.resolve_active_webhook_route
    )
    automation_project_entrypoints = AutomationProjectEntrypoints(
        project_policy_service,
        feishu_actor_resolver=feishu_binding_service.resolve_actor,
        fixed_entrypoints=plugin_runtime.fixed_entrypoints,
        service_v2_webhook_dispatcher=ServiceV2WebhookDispatcher(
            policy_service=project_policy_service,
            contribution_registry=plugin_runtime.contribution_registry,
        ),
    )
    bind_automation_project_entrypoints(automation_project_entrypoints)
    bind_feishu_binding_runtime(feishu_binding_service)
    service_v2_feishu_dispatcher = ServiceV2FeishuDispatcher(
        policy_service=project_policy_service,
        contribution_registry=plugin_runtime.contribution_registry,
        resolve_actor=feishu_binding_service.resolve_actor,
    )
    bind_service_v2_feishu_dispatcher(service_v2_feishu_dispatcher)
    harness_gateway = build_read_only_harness_gateway(runtime, finance_summary=business_finance_query.run, invocations=direct_invocations)
    process_service_v2_runtime = ServiceV2ProcessRuntime(
        plugin_conversations=plugin_conversations,
        text_queries=build_chat_text_queries(runtime),
        policy_service=project_policy_service, contribution_registry=plugin_runtime.contribution_registry,
        backend_availability=plugin_runtime.contribution_backend_availability,
        llm_client=runtime.llm, harness_fixed_handlers=harness_gateway.handlers(),
        instance_name_resolver=lambda automation_id: plugin_runtime.catalog.require(
            automation_id
        ).display_name,
    )
    harness_runtime_status = await asyncio.to_thread(process_service_v2_runtime.start)
    runtime.configure_conversations(process_service_v2_runtime.conversations)
    logger.info("AI assistant runtime status=%s availability=%s", harness_runtime_status.status, harness_runtime_status.availability)
    runtime.configure_execution_runtime(plugin_runtime.execution_router)
    bind_agent_runtime(runtime, loop)

    async def handle_session_restored(account_id: str) -> None:
        # A restored login only makes future calls available; ended calls stay ended.
        await asyncio.to_thread(
            plugin_runtime.reconcile_after_dependency_change,
            account_id=account_id,
        )

    def on_session_restored(payload: dict) -> bool:
        account_id = str((payload or {}).get("account_id") or "").strip()
        if not account_id:
            return False
        loop.call_soon_threadsafe(
            asyncio.create_task,
            handle_session_restored(account_id),
        )
        return True

    register_account_session_restored(on_session_restored)

    scheduler.start(paused=release_hold)
    logger.info(
        "APScheduler started with %d jobs state=%s",
        len(scheduler.get_jobs()),
        "paused_for_release" if release_hold else "running",
    )

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

    def warm_workflow_resource_catalog() -> None:
        try:
            from agent.workflow_resource_store import list_workflow_resource_descriptors

            updated_metadata = sync_reviewed_phase7_resource_metadata()
            list_workflow_resource_descriptors()
            if updated_metadata:
                logger.info(
                    "Workflow resource metadata synchronized count=%d",
                    len(updated_metadata),
                )
        except Exception:
            logger.warning("Workflow resource catalog warm-up failed")

    resource_catalog_warm_task = asyncio.create_task(
        asyncio.to_thread(warm_workflow_resource_catalog),
        name="workflow-resource-catalog-warm-up",
    )

    logger.info("Agent service started instance_id=%s", INSTANCE_ID)
    yield

    logger.info("Agent service shutting down instance_id=%s", INSTANCE_ID)
    process_service_v2_runtime.stop()
    if tms_session_alert_task is not None:
        tms_session_alert_stop.set()
        tms_session_alert_task.cancel()
        with suppress(asyncio.CancelledError):
            await tms_session_alert_task
    if not resource_catalog_warm_task.done():
        resource_catalog_warm_task.cancel()
    with suppress(asyncio.CancelledError):
        await resource_catalog_warm_task
    scheduler.shutdown(wait=False)
    register_account_session_restored(None)
    bind_automation_project_entrypoints(None)
    bind_feishu_binding_runtime(None)
    bind_service_v2_feishu_dispatcher(None)
    await stop_feishu_ws()
    await direct_invocations.stop()
    bind_direct_business_service(None)
    await plugin_runtime.stop()
    await runtime.close()
    automation_plugin_runtime = None
    automation_project_entrypoints = None
    feishu_binding_service = None
    automation_project_policy_service = None
    agent_core = None
    CONSOLE_IDENTITY_VERIFIER = None
    account_manager.set_credentials_change_guard(None)
    logger.info("Agent service stopped")


app = FastAPI(title="Logistics Agent", version="0.1.0", lifespan=lifespan)
app.include_router(tms_router, deprecated=True)
app.include_router(tms_router, prefix="/internal/v1")
app.include_router(
    create_automation_project_router(
        service_provider=_automation_project_policies,
        actor_provider=lambda request: _require_console_admin_request(request),
        module_slot_host_provider=lambda: ServiceV2WaybillEntryExtensionHost(
            policy_service=_automation_project_policies(),
            contribution_registry=_automation_plugins().contribution_registry,
        ),
    )
)
app.include_router(
    create_business_module_router(
        service_provider=lambda: BusinessModuleLifecycleService(
            BusinessModuleRepository(_runtime().memory.connection_factory),
            release_sha=_release_sha(),
        ),
        admin_actor_provider=lambda request: _require_console_admin_request(request),
    )
)
app.include_router(
    create_automation_plugin_management_router(
        service_provider=lambda: _automation_plugins().management,
        actor_provider=lambda request: _require_console_admin_request(request),
        scheduler_refresh_provider=lambda: _automation_plugins().service_effect_driver.refresh_contribution_projection(),
        policy_service_provider=_automation_project_policies,
        scheduler_project_sync_provider=lambda automation_id: (
            _automation_plugins().service_effect_driver.refresh_project_schedule(automation_id)
        ),
    )
)
app.include_router(
    create_harness_router(
        conversation_provider=service_v2_harness_conversations,
        tools_provider=service_v2_harness_tools,
        actor_provider=lambda request: _require_console_admin_request(request),
        availability_provider=service_v2_harness_status,
    )
)


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


@app.exception_handler(OrchestrationError)
async def orchestration_error_handler(request: Request, exc: OrchestrationError):
    is_internal = request.url.path.startswith("/internal/v1/")
    is_verified_webhook = request.url.path.startswith("/webhook/")
    if not is_internal and not is_verified_webhook:
        raise exc
    status_by_code = {
        "RUN_NOT_FOUND": 404,
        "WORK_ITEM_NOT_FOUND": 404,
        "APPROVAL_NOT_FOUND": 404,
        "APPROVAL_FORBIDDEN": 403,
        "SUPER_ADMIN_REQUIRED": 403,
        "FEISHU_BINDING_FORBIDDEN": 403,
        "ACTION_FORBIDDEN": 403,
        "TOOL_PERMISSION_DENIED": 403,
        "OPERATION_DISABLED": 403,
        "APPROVAL_EXPIRED": 409,
        "APPROVAL_REJECTED": 409,
        "APPROVAL_NOT_PENDING": 409,
        "PLAN_STALE": 409,
        "POLICY_VERSION_CONFLICT": 409,
        "TASK_CONFIGURATION_VERSION_CONFLICT": 409,
        "ACCOUNT_CREDENTIAL_CHANGE_IN_PROGRESS": 409,
        "ACCOUNT_EXECUTION_IN_PROGRESS": 409,
        "ACCOUNT_CREDENTIAL_ACTIVE_RUN": 409,
        "ACCOUNT_POLICY_REVOCATION_CONFLICT": 409,
        "AUTOMATION_ALREADY_RUNNING": 409,
        "ACCOUNT_ACTIVE_RUN_CHECK_FAILED": 503,
        "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE": 503,
        "IDEMPOTENCY_CONFLICT": 409,
        "SCHEDULE_TASK_NOT_FOUND": 404,
        "ILLEGAL_RUN_TRANSITION": 409,
        "PROJECT_ROUTE_NOT_FOUND": 404,
        "PROJECT_ROUTE_AMBIGUOUS": 409,
        "PROJECT_ROUTE_STALE": 409,
        "PROJECT_RUNTIME_RECONCILING": 409,
        "PROJECT_RUNTIME_PROJECTION_STALE": 409,
        "PROJECT_ENTRYPOINT_DISABLED": 409,
        "PROJECT_ACCOUNT_OVERRIDE_FORBIDDEN": 422,
        "PROJECT_ARGUMENT_OVERRIDE_FORBIDDEN": 422,
        "PROJECT_DYNAMIC_INPUT_CONFLICT": 422,
        "STABLE_EVENT_ID_REQUIRED": 422,
    }
    status_code = status_by_code.get(exc.code, 422)
    return JSONResponse(
        status_code=status_code,
        content=api_failure(
            exc.code,
            redact_text(exc.message),
            data=exc.details or None if is_internal else None,
        ),
    )


@app.exception_handler(OrchestrationPersistenceError)
async def orchestration_state_conflict_handler(
    request: Request,
    exc: OrchestrationPersistenceError,
):
    if not request.url.path.startswith("/internal/v1/"):
        raise exc
    return JSONResponse(
        status_code=409,
        content=api_failure(
            "STATE_CONFLICT",
            redact_text(exc)[:500] or "Control-plane state changed concurrently",
        ),
    )


@app.exception_handler(HarnessError)
async def harness_error_handler(request: Request, exc: HarnessError):
    return harness_error_response(request, exc)


@app.exception_handler(LLMSettingsError)
async def llm_settings_error(request: Request, exc: LLMSettingsError):
    return JSONResponse(
        status_code=422,
        content=api_failure("llm_settings_invalid", redact_text(exc)),
    )


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
    request.state.console_principal = None
    execution_capability = str(
        request.headers.get(EXECUTION_CAPABILITY_HEADER) or ""
    ).strip()
    if execution_capability and await _execution_capability_authorizes_request(
        request,
        execution_capability,
    ):
        return await call_next(request)

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
    verifier = CONSOLE_IDENTITY_VERIFIER
    if verifier is not None:
        try:
            request.state.console_principal = verifier.verify(
                headers=request.headers,
                method=request.method,
                request_target=_request_target(request),
                body=await request.body(),
            )
        except ConsoleIdentityError as exc:
            status_code = 401
            if exc.code == "CONSOLE_SIGNING_SECRET_NOT_CONFIGURED":
                status_code = 503
            elif exc.code == "CONSOLE_SIGNATURE_REPLAYED":
                status_code = 409
            return JSONResponse(
                status_code=status_code,
                content=api_failure(exc.code, str(exc)),
            )
    if _admin_request_requires_console_principal(request.url.path):
        principal = getattr(request.state, "console_principal", None)
        roles = {
            str(role or "").strip().lower()
            for role in (
                principal.get("roles", [])
                if isinstance(principal, dict)
                else []
            )
        }
        if (
            not isinstance(principal, dict)
            or principal.get("actor_type") != "console_admin"
            or principal.get("authenticated_by") != "mysql_admin_session"
            or not {"admin", "super_admin"}.intersection(roles)
        ):
            return JSONResponse(
                status_code=403,
                content=api_failure(
                    "ACTION_FORBIDDEN",
                    "This Agent administration request requires an authenticated Console administrator",
                ),
            )
    principal = getattr(request.state, "console_principal", None)
    if principal and automation_project_policy_service is not None:
        access = getattr(automation_project_policy_service, "identity_access", None)
        if access is not None:
            try:
                request.state.authorized_invocation = await asyncio.to_thread(
                    authorize_console_request, access, automation_project_policy_service,
                    principal, request.method, request.url.path,
                )
            except OrchestrationError as exc:
                return JSONResponse(status_code=403, content=api_failure(exc.code, str(exc)))
    return await call_next(request)


def _admin_request_requires_console_principal(path: str) -> bool:
    """Keep service authentication separate from administrator authority."""

    normalized = "/" + str(path or "").lstrip("/")
    return (
        normalized == "/internal/v1/scheduled-task-approval-policies"
        or normalized == "/internal/v1/automation-project-policies"
        or normalized.startswith("/internal/v1/harness/")
        or normalized.startswith("/internal/v1/automation-projects/")
        or normalized.startswith("/internal/v1/automation-invocations/")
        or normalized.startswith("/internal/v1/business/")
        or normalized.startswith("/internal/v1/automation/")
        or normalized in {"/admin", "/internal/v1/admin"}
        or normalized.startswith(
            ("/admin/", "/internal/v1/admin/")
        )
    )


def _request_target(request: Request) -> str:
    target = request.url.path or "/"
    if request.url.query:
        target += f"?{request.url.query}"
    return target


async def _execution_capability_authorizes_request(
    request: Request,
    capability: str,
) -> bool:
    """Allow a capability to reach only its exact legacy ``/tms/<target>``."""

    path = str(request.url.path or "")
    if request.method.upper() != "POST" or not path.startswith("/tms/"):
        return False
    target_name = path.removeprefix("/tms/")
    if not target_name or "/" in target_name:
        return False
    try:
        payload = await request.json()
    except Exception:
        return False
    params = payload.get("params") if isinstance(payload, dict) else None
    if not isinstance(params, dict):
        return False
    return authorize_tms_target(
        capability,
        target_name,
        request_params=params,
    )


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


async def _webhook_envelope(request: Request) -> dict[str, dict]:
    """Read transport metadata without merging it into governed arguments."""

    body: dict = {}
    raw_body = await request.body()
    if raw_body.strip():
        try:
            parsed = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON webhook payload") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="Webhook JSON payload must be an object")
        body = dict(parsed)
    return {"body": body, "query": dict(request.query_params)}


def _extract_webhook_preview_run_id(
    envelope: dict[str, dict],
) -> tuple[dict[str, dict], Any | None]:
    """Remove the reserved scan confirmation field from governed arguments."""

    body = dict(envelope.get("body") or {})
    query = dict(envelope.get("query") or {})
    body_has = "preview_run_id" in body
    query_has = "preview_run_id" in query
    body_value = body.get("preview_run_id")
    query_value = query.get("preview_run_id")
    if body_has and query_has and body_value != query_value:
        raise HTTPException(
            status_code=422,
            detail="Conflicting webhook scan preview identifier",
        )
    body.pop("preview_run_id", None)
    query.pop("preview_run_id", None)
    preview_run_id = body_value if body_has else query_value if query_has else None
    if (body_has or query_has) and preview_run_id is None:
        preview_run_id = ""
    return {"body": body, "query": query}, preview_run_id


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

    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event_id = str(header.get("event_id") or "").strip()
    if event_type == "im.message.receive_v1":
        accepted = queue_im_message_payload(event_body, event_id=event_id)
    elif event_type == "application.bot.menu_v6":
        accepted = queue_bot_menu_payload(event_body, event_id=event_id)
    else:
        logger.info("Ignored unsupported Feishu webhook event: %s", event_type or "unknown")
        return {"code": 0, "msg": "ignored"}

    return {"code": 0, "msg": "ok" if accepted else "ignored"}


@app.post("/webhook/sign-status")
async def webhook_sign_status(request: Request):
    await _verify_webhook_token(request)
    envelope = await _webhook_envelope(request)
    source_event_id = _stable_webhook_event_id(envelope)
    envelope, preview_run_id = _extract_webhook_preview_run_id(envelope)
    route_key = request.url.path.strip("/")
    return await _automation_project_entrypoint_service().invoke_webhook(
        route_key=route_key,
        source_event_id=source_event_id,
        webhook_path=route_key,
        envelope=envelope,
        preview_invocation_id=preview_run_id,
    )


@app.post("/webhook/{path:path}")
async def webhook_handler(path: str, request: Request):
    await _verify_webhook_token(request)
    envelope = await _webhook_envelope(request)
    source_event_id = _stable_webhook_event_id(envelope)
    envelope, preview_run_id = _extract_webhook_preview_run_id(envelope)
    route_key = request.url.path.strip("/")
    logger.info("Verified automation project Webhook route hit: /%s", path)
    return await _automation_project_entrypoint_service().invoke_webhook(
        route_key=route_key,
        source_event_id=source_event_id,
        webhook_path=route_key,
        envelope=envelope,
        preview_invocation_id=preview_run_id,
    )


def _stable_webhook_event_id(envelope: dict) -> str:
    body = envelope.get("body") if isinstance(envelope.get("body"), dict) else {}
    query = envelope.get("query") if isinstance(envelope.get("query"), dict) else {}
    for field in ("source_event_id", "event_id", "id"):
        body_value = str(body.get(field) or "").strip()
        query_value = str(query.get(field) or "").strip()
        if body_value and query_value and body_value != query_value:
            raise HTTPException(
                status_code=422,
                detail=f"Conflicting webhook event identifier: {field}",
            )
        value = body_value or query_value
        if str(value or "").strip():
            return str(value).strip()
    raise HTTPException(status_code=422, detail="Webhook command requires a stable source event ID")


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


def _internal_health_payload() -> dict[str, Any]:
    runtime = _runtime()
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 / 1024

    uptime_sec = int(time.time() - _start_time)
    days, rem = divmod(uptime_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m"

    return (
        {
            "status": "ok",
            "release_sha": _release_sha(),
            "instance_id": INSTANCE_ID,
            "direct_invocations": {
                "active_count": len(automation_project_policy_service.direct_invocations.active_invocations())
                + automation_project_policy_service.direct_invocations.active_read_count()
                if automation_project_policy_service is not None else 0,
            },
            "uptime": uptime_str,
            "memory_mb": round(mem_mb, 1),
            "components": {
                "feishu_mode": feishu_event_mode(),
                "feishu_ws": runtime.feishu_status(),
                "deepseek": runtime.llm_status("deepseek"),
                "glm": runtime.llm_status("glm"),
                "mysql": runtime.db_status(),
                "scheduler": scheduler_runtime_status(),
                "automation_plugins": _automation_plugin_health(),
                "tms_session": get_session_broker().health_snapshot(),
            },
            "last_tool_run": runtime.last_tool_info(),
            "heavy_task_lock": runtime.heavy_lock_held(),
        }
    )


@app.get("/internal/v1/health")
async def internal_health():
    # Component probes may touch MySQL, plugin catalogs, and SDK state. Keep
    # every such probe off the Uvicorn event loop; public /health stays a
    # constant-time liveness response with no business locks.
    return api_success(await asyncio.to_thread(_internal_health_payload))


def _http_request_actor(
    request: Request,
    *,
    requested_source: str,
) -> tuple[Actor, str]:
    """Derive an HTTP actor from verified caller scope, never from JSON fields."""

    if requested_source in {"scheduler", "system"}:
        raise OrchestrationError(
            "TRUSTED_IN_PROCESS_SOURCE_REQUIRED",
            "Scheduler and system commands may only originate in process through in-process trusted adapters",
        )

    principal = getattr(request.state, "console_principal", None)
    if isinstance(principal, dict):
        if requested_source != "console":
            raise OrchestrationError(
                "ACTOR_SOURCE_MISMATCH",
                "Signed Console principals may only submit Console requests",
            )
        return (
            _actor_from_payload(
                principal,
                list(principal.get("roles") or []),
                "console",
            ),
            "console",
        )
    if requested_source == "console":
        raise OrchestrationError(
            "TRUSTED_CONSOLE_ACTOR_REQUIRED",
            "A signed Console administrator principal is required",
        )
    return (
        Actor(
            ActorType.LEGACY_API,
            "internal-api",
            roles=(),
            authenticated_by="internal_api_token",
        ),
        "legacy_api",
    )


def _require_console_admin_request(request: Request) -> Actor:
    """Require a verified Console principal for control-plane reads."""

    actor, _source = _http_request_actor(request, requested_source="console")
    if (
        actor.actor_type is not ActorType.CONSOLE_ADMIN
        or not {"admin", "super_admin"}.intersection(actor.roles)
    ):
        raise OrchestrationError(
            "ACTION_FORBIDDEN",
            "This control-plane request requires an authenticated Console administrator",
        )
    return actor


def _require_console_super_admin_request(request: Request) -> Actor:
    actor = _require_console_admin_request(request)
    if "super_admin" not in actor.roles:
        raise OrchestrationError(
            "SUPER_ADMIN_REQUIRED",
            "This action requires a Console super administrator",
        )
    return actor


@app.get("/internal/v1/admin/feishu-approval-binding")
async def get_feishu_binding(request: Request):
    actor = _require_console_super_admin_request(request)
    return api_success(await asyncio.to_thread(_feishu_bindings().binding_status, int(actor.actor_id)))


@app.post("/internal/v1/admin/feishu-approval-binding/challenge")
async def create_feishu_binding_challenge(request: Request):
    actor = _require_console_super_admin_request(request)
    payload = await request.json()
    if not isinstance(payload, dict) or set(payload) - {"role_id", "account_id", "label"}:
        raise OrchestrationError("INVALID_INPUT", "绑定对象格式无效")
    return api_success(
        await asyncio.to_thread(
            _feishu_bindings().create_binding_challenge,
            int(actor.actor_id),
            **payload,
        )
    )


@app.delete("/internal/v1/admin/feishu-approval-binding")
async def revoke_feishu_binding(request: Request):
    actor = _require_console_super_admin_request(request)
    await asyncio.to_thread(_feishu_bindings().revoke_binding, int(actor.actor_id))
    return api_success({"revoked": True})


class ChatRequest(BaseModel):
    message: str
    user_id: str = "console"
    conversation_id: str | None = None
    request_id: str | None = None
    actor: dict | None = None
    actor_roles: list[str] = Field(default_factory=list)
    source: str = "console"


@app.post("/chat", deprecated=True)
async def chat(req: ChatRequest, request: Request):
    actor, source = _http_request_actor(request, requested_source=req.source)
    return await _runtime().handle_message(
        message=req.message,
        user_id=req.user_id,
        conversation_id=req.conversation_id,
        actor=actor,
        source=source,
        request_id=req.request_id,
    )


@app.post("/internal/v1/chat")
async def internal_chat(req: ChatRequest, request: Request):
    return api_success(await chat(req, request))


class ToolRequest(BaseModel):
    tool_name: str
    params: dict = Field(default_factory=dict)
    idempotency_key: str | None = None
    actor: dict | None = None
    actor_roles: list[str] = Field(default_factory=list)
    source: str = "legacy_api"
    correlation_id: str | None = None


@app.post("/run-tool", deprecated=True)
async def run_tool(req: ToolRequest, request: Request):
    actor, source = _http_request_actor(request, requested_source=req.source)
    return await _runtime().execute_tool(
        req.tool_name,
        req.params,
        actor=actor,
        source=source,
        idempotency_key=req.idempotency_key,
        correlation_id=req.correlation_id,
    )


class CancelToolRequest(BaseModel):
    tool_name: str
    started_at: str = ""


@app.post("/cancel-tool", deprecated=True)
async def cancel_tool(req: CancelToolRequest):
    del req
    return JSONResponse(
        status_code=410,
        content={
            "ok": False,
            "error_code": "RUN_ID_REQUIRED",
            "error": "tool-name cancellation is disabled; cancel the plugin invocation by its invocation ID",
        },
    )


class KnowledgeRequest(BaseModel):
    content: str
    category: str | None = None
    source: str | None = None


class LLMConfigCandidateRequest(BaseModel):
    provider: str
    model_id: str
    api_key: SecretStr | None = None
    actor: str


class LLMConfigActionRequest(BaseModel):
    config_id: int
    actor: str


class LLMRollbackRequest(BaseModel):
    actor: str
    config_id: int | None = None


class LLMClearCredentialRequest(BaseModel):
    provider: str
    actor: str


@app.get("/tools", deprecated=True)
async def list_tools():
    return {"tools": _runtime().registry.list_tools()}


@app.get("/internal/v1/tools")
async def internal_list_tools():
    return api_success({"tools": _runtime().registry.list_tools()})


@app.post("/internal/v1/tools/run")
async def internal_run_tool(req: ToolRequest, request: Request):
    result = await run_tool(req, request)
    if isinstance(result, dict) and result.get("success") is False:
        return api_failure(
            str(result.get("error_code") or "tool_execution_failed"),
            str(result.get("error") or "Tool execution failed"),
            data=result,
        )
    return api_success(result)


@app.post("/internal/v1/tools/cancel")
async def internal_cancel_tool(req: CancelToolRequest):
    del req
    return JSONResponse(
        status_code=410,
        content=api_failure(
            "RUN_ID_REQUIRED",
            "tool-name cancellation is disabled; cancel the plugin invocation by its invocation ID",
        ),
    )


@app.post("/admin/reload", deprecated=True)
async def reload_runtime():
    runtime = _runtime()
    result = runtime.reload_runtime_config()
    logger.info("Runtime configuration reloaded")
    scheduler = reload_scheduler(runtime)
    return {"status": "ok", **result, "scheduler": scheduler}


@app.post("/internal/v1/admin/reload")
async def internal_reload_runtime():
    return api_success(await reload_runtime())


@app.post("/internal/v1/admin/scheduler/activate-after-release")
async def internal_activate_scheduler_after_release(request: Request):
    _require_console_admin_request(request)
    async with RELEASE_ACTIVATION_LOCK:
        try:
            release_marker_present = scheduler_release_hold_requested()
            plugin_runtime = _automation_plugins()
            await asyncio.to_thread(plugin_runtime.reconcile)
            plugin_status = await asyncio.to_thread(plugin_runtime.health)
            if plugin_status.get("ok") is not True:
                raise RuntimeError("Automation plugin service integrity check failed")
            scheduler_status = begin_scheduler_release_activation(_release_sha())
            if (
                scheduler_status.get("state") != "running"
                or bool(scheduler_status.get("release_hold"))
                != release_marker_present
                or plugin_status.get("ok") is not True
            ):
                raise RuntimeError("Release runtimes did not enter the running state")
            # Marker consumption is the final mutation. Explicitly unavailable
            # projects remain blocked by their own runtime status; they do not
            # prevent healthy projects or the scheduler from being activated.
            scheduler_status = consume_scheduler_release_hold(_release_sha())
        except RuntimeError as exc:
            if scheduler_release_hold_requested():
                try:
                    pause_scheduler_for_release()
                except RuntimeError:
                    logger.exception("Scheduler could not be re-held after activation failure")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if release_marker_present:
        # Scheduled collection skipped by this release runs once now. A failure
        # here must not undo an activation that has already been committed.
        try:
            missed = await asyncio.to_thread(
                release_missed_scheduled_task_ids, _runtime().memory.connection_factory
            )
            caught_up = run_scheduled_tasks_now(missed)
            if caught_up:
                logger.info("Release catch-up started for %d scheduled task(s)", len(caught_up))
        except Exception:
            logger.exception("Release catch-up of missed scheduled tasks failed")
    status = {
        **scheduler_status,
        "automation_plugins": plugin_status,
    }
    logger.info(
        "Release hold consumed after all in-scope server runtimes activated"
    )
    return api_success(status)


def _llm_settings_repository() -> LLMSettingsRepository:
    return LLMSettingsRepository(_runtime().memory.connection_factory)


@app.get("/internal/v1/admin/llm/config")
async def internal_llm_config_status():
    repository = _llm_settings_repository()
    payload = await asyncio.to_thread(repository.public_status)
    payload["runtime"] = _runtime().llm.public_status()
    return api_success(payload)


@app.get("/internal/v1/admin/llm/audit")
async def internal_llm_config_audit(limit: int = 200):
    rows = await asyncio.to_thread(_llm_settings_repository().audit_logs, limit=limit)
    return api_success({"items": rows})


@app.post("/internal/v1/admin/llm/candidates")
async def internal_save_llm_candidate(req: LLMConfigCandidateRequest, request: Request):
    repository = _llm_settings_repository()
    key = req.api_key.get_secret_value() if req.api_key is not None else None
    actor_id = _require_console_admin_request(request).actor_id
    config_id = await asyncio.to_thread(
        repository.save_candidate,
        provider=req.provider,
        model_id=req.model_id,
        api_key=key,
        actor=actor_id,
    )
    return api_success({"config_id": config_id})


@app.post("/internal/v1/admin/llm/models/refresh")
async def internal_refresh_llm_models(req: LLMConfigActionRequest):
    service = LLMCompatibilityService(_llm_settings_repository())
    models = await service.refresh_models(req.config_id)
    return api_success({"config_id": req.config_id, "models": models})


@app.post("/internal/v1/admin/llm/test")
async def internal_test_llm_candidate(req: LLMConfigActionRequest):
    service = LLMCompatibilityService(_llm_settings_repository())
    result = await service.test_candidate(req.config_id)
    return api_success(result)


@app.post("/internal/v1/admin/llm/activate")
async def internal_activate_llm_config(req: LLMConfigActionRequest, request: Request):
    repository = _llm_settings_repository()
    actor_id = _require_console_admin_request(request).actor_id
    await asyncio.to_thread(repository.activate, req.config_id, actor=actor_id)
    runtime_status = await _runtime().reload_llm_config()
    return api_success({"config_id": req.config_id, "runtime": runtime_status})


@app.post("/internal/v1/admin/llm/rollback")
async def internal_rollback_llm_config(req: LLMRollbackRequest, request: Request):
    repository = _llm_settings_repository()
    actor_id = _require_console_admin_request(request).actor_id
    config_id = await asyncio.to_thread(
        repository.rollback,
        actor=actor_id,
        config_id=req.config_id,
    )
    runtime_status = await _runtime().reload_llm_config()
    return api_success({"config_id": config_id, "runtime": runtime_status})


@app.post("/internal/v1/admin/llm/credentials/clear")
async def internal_clear_llm_credential(req: LLMClearCredentialRequest, request: Request):
    actor_id = _require_console_admin_request(request).actor_id
    await asyncio.to_thread(
        _llm_settings_repository().clear_credentials,
        req.provider,
        actor=actor_id,
    )
    runtime_status = await _runtime().reload_llm_config()
    return api_success(
        {"provider": req.provider, "configured": False, "runtime": runtime_status}
    )


@app.post("/knowledge", deprecated=True)
async def add_knowledge(req: KnowledgeRequest):
    record_id = _runtime().memory.add_knowledge(
        content=req.content,
        category=req.category,
        source=req.source,
    )
    return {"status": "ok", "id": record_id}


@app.post("/internal/v1/knowledge")
async def internal_add_knowledge(req: KnowledgeRequest):
    return api_success(await add_knowledge(req))


@app.get("/knowledge/search", deprecated=True)
async def search_knowledge(q: str, limit: int = 5):
    rows = _runtime().memory.search_knowledge(q, limit=max(1, min(limit, 20)))
    return {"query": q, "results": rows}


@app.get("/internal/v1/knowledge/search")
async def internal_search_knowledge(q: str, limit: int = 5):
    return api_success(await search_knowledge(q, limit))


@app.get("/tool-output/{tool_name}", deprecated=True)
async def get_tool_output(
    tool_name: str,
    request: Request,
    offset: int = 0,
    started_at: str = "",
):
    """获取工具的实时 shell 输出"""
    _require_console_admin_request(request)
    execution_runtime = _runtime()._execution_runtime
    if execution_runtime is None:
        raise HTTPException(status_code=503, detail="Execution runtime is unavailable")
    return execution_runtime.get_running_output(
        tool_name,
        offset=max(0, offset),
        started_at=started_at,
    )


@app.get("/internal/v1/tool-output/{tool_name}")
async def internal_get_tool_output(
    tool_name: str,
    request: Request,
    offset: int = 0,
    started_at: str = "",
):
    _require_console_admin_request(request)
    execution_runtime = _runtime()._execution_runtime
    if execution_runtime is None:
        raise HTTPException(status_code=503, detail="Execution runtime is unavailable")
    return api_success(
        execution_runtime.get_running_output(
            tool_name,
            offset=max(0, offset),
            started_at=started_at,
        )
    )


@app.get("/tool-logs", deprecated=True)
async def get_tool_logs(
    request: Request,
    limit: int = 20,
    tool_name: str | None = None,
    success: bool | None = None,
):
    _require_console_admin_request(request)
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
async def internal_get_tool_logs(
    request: Request,
    limit: int = 20,
    tool_name: str | None = None,
    success: bool | None = None,
):
    _require_console_admin_request(request)
    rows = _runtime().memory.get_tool_logs(
        limit=max(1, min(limit, 100)),
        tool_name=tool_name,
        success=success,
    )
    return api_success(
        {
            "limit": max(1, min(limit, 100)),
            "tool_name": tool_name,
            "success": success,
            "rows": rows,
        }
    )


@app.get("/scheduled-tasks", deprecated=True)
async def scheduled_tasks():
    return {"rows": _runtime().memory.list_scheduled_tasks()}


@app.get("/internal/v1/scheduled-tasks")
async def internal_scheduled_tasks(request: Request):
    _require_console_admin_request(request)
    return api_success({"rows": _runtime().memory.list_scheduled_tasks()})


@app.get("/workflow-resources", deprecated=True)
async def workflow_resources():
    return {"rows": list_workflow_resources()}


@app.get("/internal/v1/workflow-resources")
async def internal_workflow_resources():
    return api_success({"rows": list_workflow_resources()})


@app.post("/admin/import-phase7-resources", deprecated=True)
async def import_phase7_resource_configs():
    imported = import_phase7_resources()
    logger.info("Imported Phase 7 workflow resources into MySQL: %s", ", ".join(imported))
    return {"status": "ok", "imported": imported}


@app.post("/internal/v1/admin/import-phase7-resources")
async def internal_import_phase7_resource_configs():
    return api_success(await import_phase7_resource_configs())


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
