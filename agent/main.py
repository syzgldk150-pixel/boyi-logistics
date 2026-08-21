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
from datetime import datetime, time as datetime_time, timedelta, timezone
from contextlib import asynccontextmanager, suppress
from logging.handlers import TimedRotatingFileHandler
from typing import Any
from zoneinfo import ZoneInfo

import psutil
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

from shared.redaction import redact_text
from shared.contracts import api_failure, api_success
from shared.finance.sources import (
    enabled_finance_account_ids,
    enabled_finance_platforms,
    enabled_finance_source_specs,
)
from shared.runtime_events import (
    publish_finance_alert,
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
from agent.automation_plugins.production import (
    ProductionAutomationPluginRuntime,
    build_production_automation_plugin_runtime,
    production_cursor_secret,
)
from agent.automation_plugins.first_party import (
    release_first_party_automation_ids,
    release_first_party_broker_action_keys,
    release_first_party_plugin_ids,
)
from agent.automation_plugins.management_api import (
    create_automation_plugin_management_router,
)
from agent.orchestration.approval_service import ApprovalService
from agent.orchestration.automation_project_api import (
    create_automation_project_router,
)
from agent.orchestration.automation_project_entrypoints import (
    AutomationProjectEntrypoints,
    CommittedAutomationProjectRouteResolver,
    TrustedDynamicArgumentResolver,
)
from agent.orchestration.automation_project_policy_service import (
    AutomationProjectPolicyService,
)
from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.context_builder import ContextBuilder
from agent.orchestration.control_plane_service import ControlPlaneService
from agent.orchestration.feishu_approval_service import FeishuApprovalService
from agent.orchestration.execution_adapter import RegisteredToolExecutionAdapter
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    EntityRef,
    OrchestrationError,
    RunStatus,
    new_id,
)
from agent.orchestration.outbox_dispatcher import OutboxDispatcher
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.policy_engine import PolicyEngine
from agent.orchestration.scheduled_task_approval_service import ScheduledTaskApprovalService
from agent.orchestration.result_verifier import ResultVerifier
from agent.orchestration.workflow_runner import WorkflowRunner
from agent.automation_plugins.release_scope import WINDOWS_WORKER_RELEASE_ENABLED
if WINDOWS_WORKER_RELEASE_ENABLED:
    from agent.windows_worker.server_api import (
        FilesystemWorkerPackageArchiveReader,
        WindowsWorkerServerTransport,
        build_worker_transport_router,
        is_worker_transport_path,
        load_worker_server_signer,
    )
from agent.llm_settings import (
    LLMCompatibilityService,
    LLMSettingsError,
    LLMSettingsRepository,
)
from agent.http_security import INTERNAL_API_TOKEN_HEADER, authenticate_internal_request
from agent.execution_boundary import EXECUTION_CAPABILITY_HEADER, authorize_tms_target
from agent.phase7_resource_import import import_phase7_resources
from agent.runtime_config import load_agent_environment
from agent.scheduler import (
    begin_scheduler_release_activation,
    consume_scheduler_release_hold,
    init_scheduler,
    pause_scheduler_for_release,
    reload_scheduler,
    scheduler_release_hold_requested,
    scheduler_runtime_status,
    seed_phase7_schedule_tasks,
)
from agent.tms_runtime import router as tms_router
from agent.api_contracts import validation_failure
from agent.tms_runtime.account_manager import get_account_manager
from agent.tms_runtime.monitoring import configure_feishu_operation
from agent.tms_runtime.routes import update_account_list_cache_status
from agent.tms_runtime.routes import bind_agent_command_runtime
from agent.tms_runtime.session_broker import get_session_broker
from agent.workflow_resource_store import get_workflow_resource, list_workflow_resources
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
    bind_feishu_approval_runtime,
    queue_bot_menu_payload,
    queue_im_message_payload,
)
from feishu.notify import (
    send_finance_anomaly_alert,
    send_text_sync,
    send_tms_session_disconnected_alert,
)
from tools.feishu_cli_tool import feishu_operation
from tools.price_tool import run_price_tool
from tools.track_waybill_tool import run_track_waybill
from plugin_core_adapters import build_production_first_party_core_handler_map
from shared.orchestration_repository import (
    OrchestrationPersistenceError,
    OrchestrationRepository,
)


register_tms_session_alert(send_tms_session_disconnected_alert)
register_finance_alert(send_finance_anomaly_alert)
configure_feishu_operation(feishu_operation)


agent_core: AgentCore | None = None
orchestration_repository: OrchestrationRepository | None = None
workflow_runner: WorkflowRunner | None = None
outbox_dispatcher: OutboxDispatcher | None = None
control_plane_service: ControlPlaneService | None = None
scheduled_task_approval_service: ScheduledTaskApprovalService | None = None
automation_project_policy_service: AutomationProjectPolicyService | None = None
scheduled_task_approval_bootstrap: dict[str, int] = {}
automation_plugin_runtime: ProductionAutomationPluginRuntime | None = None
automation_worker_transport_service: WindowsWorkerServerTransport | None = None
automation_project_entrypoints: AutomationProjectEntrypoints | None = None
feishu_approval_service: FeishuApprovalService | None = None
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
FINANCE_FAILURE_RUN_STATUSES = frozenset(
    {
        RunStatus.BLOCKED_LOGIN.value,
        RunStatus.BLOCKED_DATA.value,
        RunStatus.FAILED_TERMINAL.value,
    }
)


def _runtime() -> AgentCore:
    if agent_core is None:
        raise RuntimeError("Agent runtime is not initialized")
    return agent_core


def _control_plane() -> ControlPlaneService:
    if control_plane_service is None:
        raise RuntimeError("Agent control plane is not initialized")
    return control_plane_service


def _orchestration_repo() -> OrchestrationRepository:
    if orchestration_repository is None:
        raise RuntimeError("Agent orchestration repository is not initialized")
    return orchestration_repository


def _scheduled_task_approvals() -> ScheduledTaskApprovalService:
    if scheduled_task_approval_service is None:
        raise RuntimeError("Scheduled task approval service is not initialized")
    return scheduled_task_approval_service


def _automation_project_policies() -> AutomationProjectPolicyService:
    if automation_project_policy_service is None:
        raise RuntimeError("Automation project policy service is not initialized")
    return automation_project_policy_service


def _automation_project_entrypoint_service() -> AutomationProjectEntrypoints:
    if automation_project_entrypoints is None:
        raise RuntimeError("Automation project entrypoints are not initialized")
    return automation_project_entrypoints


def _feishu_approvals() -> FeishuApprovalService:
    if feishu_approval_service is None:
        raise RuntimeError("Feishu approval service is not initialized")
    return feishu_approval_service


def _automation_plugins() -> ProductionAutomationPluginRuntime:
    if automation_plugin_runtime is None:
        raise RuntimeError("Automation plugin runtime is not initialized")
    return automation_plugin_runtime


def _automation_worker_transport() -> WindowsWorkerServerTransport:
    if automation_worker_transport_service is None:
        raise RuntimeError("Windows Worker transport is not initialized")
    return automation_worker_transport_service


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


def _automation_worker_dispatch_health(*, release_hold: bool) -> dict[str, Any]:
    if not WINDOWS_WORKER_RELEASE_ENABLED:
        return {
            "enabled": False,
            "state": "disabled",
            "release_hold": False,
            "active_jobs": 0,
        }
    repository = orchestration_repository
    if repository is None:
        return {"state": "stopped", "release_hold": True, "active_jobs": 0}
    try:
        with repository.unit_of_work() as uow:
            payload = uow.automation_plugins.worker_dispatch_health(
                release_hold=release_hold,
            )
    except Exception as exc:
        logger.error(
            "Automation worker dispatch health failed error=%s",
            redact_text(exc),
        )
        return {
            "state": "error",
            "release_hold": True,
            "active_jobs": 0,
            "error_code": getattr(exc, "code", type(exc).__name__.upper())[:64],
        }
    return {
        "state": "held" if release_hold else "running",
        "release_hold": bool(payload.get("release_hold")),
        "active_jobs": int(payload.get("active_jobs") or 0),
    }


def _orchestration_connection():
    """Return a transaction-scoped MySQL connection; runtime never runs DDL."""

    import pymysql

    return pymysql.connect(
        host=os.getenv("AGENT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("AGENT_DB_PORT", "3306")),
        user=os.getenv("AGENT_DB_USER", "agent"),
        password=os.getenv("AGENT_DB_PASS", ""),
        database=os.getenv("AGENT_DB_NAME", "agent_db"),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _resolve_command_accounts(command: Command) -> list[dict]:
    """Resolve public account identities without exposing credential material."""

    rows = get_account_manager().list_accounts(include_status=False, validate=False)
    active = [row for row in rows if bool(row.get("is_active", True))]
    arguments = command.parameters.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    requested: list[str] = []
    explicit = command.parameters.get("account_id") or arguments.get("account_id")
    if explicit:
        requested.append(str(explicit))
    account_ids = arguments.get("account_ids")
    if isinstance(account_ids, list):
        requested.extend(str(value) for value in account_ids if str(value or "").strip())
    tool_name = str(command.parameters.get("tool_name") or "")
    if tool_name == "sync_customer_service_problems" and not requested:
        active = [row for row in active if str(row.get("system") or "") in {"ronghui", "yunda"}]
    elif tool_name == "sync_finance_bills":
        production_sources = {
            (spec.platform, spec.account_id)
            for spec in enabled_finance_source_specs()
        }
        platform = str(arguments.get("platform") or "").strip().lower()
        if platform and platform not in enabled_finance_platforms():
            return []
        active = [
            row
            for row in active
            if (str(row.get("system") or "").strip().lower(), str(row.get("account_id") or ""))
            in production_sources
            and (not platform or str(row.get("system") or "").strip().lower() == platform)
        ]
        if requested:
            requested_set = set(requested)
            active = [row for row in active if str(row.get("account_id") or "") in requested_set]
    elif requested:
        requested_set = set(requested)
        active = [row for row in active if str(row.get("account_id") or "") in requested_set]
    return [
        {
            "account_id": str(row.get("account_id") or ""),
            "system": str(row.get("system") or ""),
            "account_purpose": str(row.get("account_purpose") or ""),
            "session_profile": str(row.get("session_profile") or ""),
            "is_active": bool(row.get("is_active", True)),
        }
        for row in active
    ]


def _active_account_ids() -> tuple[str, ...]:
    rows = get_account_manager().list_accounts(include_status=False, validate=False)
    return tuple(
        str(row.get("account_id") or "").strip()
        for row in rows
        if bool(row.get("is_active", True)) and str(row.get("account_id") or "").strip()
    )


def _resolve_command_entities(command: Command) -> list[dict]:
    return [reference.to_dict() for reference in command.entity_refs]


def _resolve_source_integrity(command: Command) -> dict:
    # Source integrity must come from an authoritative server-side resolver.
    # Caller-provided execution context is deliberately excluded from plan
    # fingerprints and approval decisions.
    del command
    return {}


def _resolve_command_resources(command: Command) -> dict:
    service = control_plane_service
    return service.resolve_command_context(command) if service is not None else {}


def _noop_outbox_handler(delivery, _uow):
    return {"event_id": delivery.get("event_id"), "acknowledged": True}


def _run_plan_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    plan = run.get("plan_json")
    raw_steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(raw_steps, list):
        return []
    return [dict(step) for step in raw_steps if isinstance(step, dict)]


def _project_finance_failure_event(
    delivery: dict[str, Any],
    uow: Any,
    *,
    run: dict[str, Any],
    steps: list[dict[str, Any]],
    failure_status: str,
) -> dict[str, Any]:
    finance_steps = [
        step
        for step in steps
        if str(step.get("tool_name") or "").strip() == "sync_finance_bills"
    ]
    if not finance_steps:
        return {"event_id": delivery.get("event_id"), "projected": False}

    source_payload = delivery.get("payload_json")
    if not isinstance(source_payload, dict):
        raise RuntimeError("finance failure source event has an invalid payload")
    run_id = str(delivery.get("run_id") or "").strip()
    causation_id = str(delivery.get("event_id") or "").strip()
    if not run_id or not causation_id:
        raise RuntimeError("finance failure source event is missing identity")
    startup_catchup = any(
        isinstance(step.get("arguments"), dict)
        and step["arguments"].get("_startup_catchup") is True
        for step in finance_steps
    )
    error_code = redact_text(
        source_payload.get("error_code") or "FINANCE_SYNC_FAILED"
    ).strip()[:64]
    error_summary = redact_text(
        source_payload.get("error_summary") or "Finance synchronization did not complete"
    )[:500]
    failure_event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"boyi:finance.sync.failed:{causation_id}")
    )
    receipt = uow.events.append_with_outbox(
        {
            "event_id": failure_event_id,
            "event_type": "finance.sync.failed",
            "schema_version": 1,
            "source_system": "agent",
            "source_event_id": causation_id,
            "entity_type": "agent_run",
            "entity_id": run_id,
            "work_item_id": run.get("work_item_id"),
            "run_id": run_id,
            "step_id": None,
            "occurred_at": delivery.get("occurred_at")
            or datetime.now(timezone.utc).replace(tzinfo=None),
            "observed_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "correlation_id": str(
                delivery.get("correlation_id") or run.get("correlation_id") or ""
            ),
            "causation_id": causation_id,
            "payload": {
                "run_id": run_id,
                "status": failure_status,
                "tool_name": "sync_finance_bills",
                "error_code": error_code or "FINANCE_SYNC_FAILED",
                "error_summary": error_summary,
                "startup_catchup": startup_catchup,
            },
        },
        (
            {
                "consumer_name": "finance.failure_alert",
                "topic": "finance.sync.failed",
                "partition_key": run_id,
                "max_attempts": 10,
            },
        ),
    )
    return {
        "event_id": delivery.get("event_id"),
        "projected": True,
        "finance_failure_event_id": receipt["event"]["event_id"],
    }


def _project_run_completed_event(delivery, uow):
    """Project governed Run completion and finance failure lifecycle events."""

    payload = delivery.get("payload_json")
    if (
        str(delivery.get("event_type") or "") != "agent.run.status_changed"
        or not isinstance(payload, dict)
    ):
        return {"event_id": delivery.get("event_id"), "projected": False}

    target_status = str(payload.get("to") or "").strip()
    if target_status not in {
        RunStatus.COMPLETED.value,
        *FINANCE_FAILURE_RUN_STATUSES,
    }:
        return {"event_id": delivery.get("event_id"), "projected": False}

    run_id = str(delivery.get("run_id") or "").strip()
    run = uow.runs.get(run_id, for_update=False) if run_id else None
    if not isinstance(run, dict):
        raise RuntimeError("run lifecycle event does not match a durable Run")
    steps = _run_plan_steps(run)
    if target_status in FINANCE_FAILURE_RUN_STATUSES:
        return _project_finance_failure_event(
            delivery,
            uow,
            run=run,
            steps=steps,
            failure_status=target_status,
        )

    if str(run.get("status") or "") != RunStatus.COMPLETED.value:
        raise RuntimeError("completed run event does not match durable Run state")
    tool_names = [
        str(step.get("tool_name") or "").strip()
        for step in steps
        if str(step.get("tool_name") or "").strip()
    ]
    causation_id = str(delivery.get("event_id") or "").strip()
    completed_event_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"boyi:agent.run.completed:{causation_id}")
    )
    receipt = uow.events.append_with_outbox(
        {
            "event_id": completed_event_id,
            "event_type": "agent.run.completed",
            "schema_version": 1,
            "source_system": "agent",
            "source_event_id": causation_id,
            "entity_type": "agent_run",
            "entity_id": run_id,
            "work_item_id": run.get("work_item_id"),
            "run_id": run_id,
            "step_id": None,
            "occurred_at": delivery.get("occurred_at") or datetime.now(timezone.utc).replace(tzinfo=None),
            "observed_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "correlation_id": str(delivery.get("correlation_id") or run.get("correlation_id") or ""),
            "causation_id": causation_id,
            "payload": {
                "run_id": run_id,
                "status": RunStatus.COMPLETED.value,
                "tool_names": tool_names,
            },
        },
        (
            {
                "consumer_name": "finance.brain",
                "topic": "agent.run.completed",
                "partition_key": run_id,
                "max_attempts": 5,
            },
        ),
    )
    return {
        "event_id": delivery.get("event_id"),
        "projected": True,
        "completed_event_id": receipt["event"]["event_id"],
    }


def _finance_sync_failure_handler(delivery, _uow):
    """Deliver one redacted finance failure alert through the durable outbox."""

    payload = delivery.get("payload_json")
    if (
        str(delivery.get("event_type") or "") != "finance.sync.failed"
        or not isinstance(payload, dict)
    ):
        return {"event_id": delivery.get("event_id"), "processed": False}
    if payload.get("startup_catchup") is True:
        return {
            "event_id": delivery.get("event_id"),
            "processed": True,
            "suppressed": True,
            "reason": "startup_catchup",
        }

    status = str(payload.get("status") or "").strip()
    if status not in FINANCE_FAILURE_RUN_STATUSES:
        raise RuntimeError("finance failure event has an unsupported Run status")
    run_id = str(payload.get("run_id") or delivery.get("run_id") or "").strip()
    error_code = redact_text(
        payload.get("error_code") or "FINANCE_SYNC_FAILED"
    ).strip()[:64]
    error_summary = redact_text(
        payload.get("error_summary") or "Finance synchronization did not complete"
    )[:500]
    details = f"Run {run_id}; status={status}; {error_summary}"
    sent = publish_finance_alert(
        {
            "anomaly_type": error_code or "FINANCE_SYNC_FAILED",
            "title": "\u8d22\u52a1\u540c\u6b65\u5931\u8d25\u6216\u963b\u585e",
            "details": details[:500],
            "admin_url": "/modules/finance#sync",
        }
    )
    if not sent:
        raise RuntimeError("finance failure alert delivery was not acknowledged")
    return {
        "event_id": delivery.get("event_id"),
        "processed": True,
        "suppressed": False,
        "sent": True,
    }


def _finance_brain_completed_handler(runtime: AgentCore, loop, delivery, _uow):
    """Consume finance Run completion on the main loop without execution bypasses."""

    payload = delivery.get("payload_json")
    tool_names = payload.get("tool_names") if isinstance(payload, dict) else None
    if not isinstance(tool_names, list) or "sync_finance_bills" not in tool_names:
        return {"event_id": delivery.get("event_id"), "processed": False}
    brain = runtime.finance_brain
    if brain is None:
        raise RuntimeError("finance brain is unavailable")
    future = asyncio.run_coroutine_threadsafe(brain.process_after_sync(), loop)
    try:
        result = future.result(timeout=1800)
    except Exception:
        future.cancel()
        raise
    return {
        "event_id": delivery.get("event_id"),
        "processed": True,
        "result": result,
    }


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


def _check_tms_account_session(account_manager, account: dict) -> dict:
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
    global AGENT_INTERNAL_API_TOKEN, CONSOLE_IDENTITY_VERIFIER, agent_core
    global orchestration_repository, workflow_runner, outbox_dispatcher, control_plane_service
    global scheduled_task_approval_service, scheduled_task_approval_bootstrap
    global automation_plugin_runtime, automation_project_policy_service
    global automation_worker_transport_service
    global automation_project_entrypoints
    global feishu_approval_service
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
            "get_price": run_price_tool,
        }
    )
    runtime = _runtime()
    loop = asyncio.get_running_loop()
    logger.info("Agent service starting instance_id=%s pid=%s", INSTANCE_ID, os.getpid())

    await runtime.init()
    repository = OrchestrationRepository(_orchestration_connection)
    mysql_version = await asyncio.to_thread(repository.validate_mysql8)
    await asyncio.to_thread(
        repository.validate_schema,
        include_windows_worker=WINDOWS_WORKER_RELEASE_ENABLED,
    )
    logger.info("Orchestration persistence ready mysql=%s", mysql_version)

    tool_executor = ToolExecutor()
    core_catalog = runtime.registry
    account_manager = get_account_manager()
    cursor_secret = production_cursor_secret(os.environ)
    plugin_handlers = build_production_first_party_core_handler_map(
        account_manager=account_manager,
        cursor_secret=cursor_secret,
        allowed_action_keys=release_first_party_broker_action_keys(core_catalog),
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
    )
    await plugin_runtime.start()
    automation_plugin_runtime = plugin_runtime
    if WINDOWS_WORKER_RELEASE_ENABLED:
        worker_server_signer = load_worker_server_signer(
            private_key_path=str(
                os.getenv("BOYI_AUTOMATION_WORKER_SERVER_SIGNING_KEY_PATH", "") or ""
            ).strip(),
            key_id=str(
                os.getenv("BOYI_AUTOMATION_WORKER_SERVER_SIGNING_KEY_ID", "") or ""
            ).strip(),
        )
        automation_worker_transport_service = WindowsWorkerServerTransport(
            orchestration_repository=repository,
            release_hold_provider=scheduler_release_hold_requested,
            release_sha=_release_sha(),
            server_signer=worker_server_signer,
            package_reader=FilesystemWorkerPackageArchiveReader(plugin_runtime.storage),
        )
    await asyncio.to_thread(plugin_runtime.reconcile)
    initial_plugin_health = plugin_runtime.health()
    catalog = plugin_runtime.composite_catalog
    agent_core.configure_tool_catalog(catalog)
    execution_port = RegisteredToolExecutionAdapter(
        catalog=catalog,
        executor=plugin_runtime.execution_router,
        direct_runners={
            "track_waybill": run_track_waybill,
            "get_price": run_price_tool,
        },
    )
    context_builder = ContextBuilder(
        account_resolver=_resolve_command_accounts,
        resource_resolver=_resolve_command_resources,
        entity_resolver=_resolve_command_entities,
        integrity_resolver=_resolve_source_integrity,
    )
    planner = DeterministicPlanner(catalog)
    validator = PlanValidator(catalog)
    schedule_policy_service = ScheduledTaskApprovalService(
        repository,
        catalog,
        enabled_finance_platforms=enabled_finance_platforms(),
        active_account_ids_provider=_active_account_ids,
        implicit_account_ids_by_tool={
            "sync_finance_bills": enabled_finance_account_ids(),
        },
        bootstrap_allowed_tool_names=release_first_party_plugin_ids(),
    )
    account_manager.set_credentials_change_guard(
        schedule_policy_service.begin_credentials_change
    )
    scheduled_task_approval_service = schedule_policy_service
    scheduled_task_approval_bootstrap = await asyncio.to_thread(
        schedule_policy_service.bootstrap_reviewed_policies
    )
    logger.info(
        "Scheduled approval bootstrap reviewed=%d created=%d existing=%d configured=%d rejected=%d completed=%d",
        scheduled_task_approval_bootstrap.get("reviewed_candidates", 0),
        scheduled_task_approval_bootstrap.get("created", 0),
        scheduled_task_approval_bootstrap.get("already_present", 0),
        scheduled_task_approval_bootstrap.get("explicitly_configured", 0),
        scheduled_task_approval_bootstrap.get("rejected", 0),
        scheduled_task_approval_bootstrap.get("completed", 0),
    )
    runner_holder: dict[str, WorkflowRunner] = {}
    gateway = CommandGateway(
        repository,
        wake_runner=lambda run_id: runner_holder["runner"].wake(run_id),
    )
    project_policy_service = AutomationProjectPolicyService(
        repository,
        core_catalog,
        plugin_runtime.catalog,
        command_gateway=gateway,
        wake_runner=lambda run_id: runner_holder["runner"].wake(run_id),
        dynamic_resolver=TrustedDynamicArgumentResolver(),
        release_hold_provider=scheduler_release_hold_requested,
    )
    default_full_auto = {"changed": 0}
    if scheduler_release_hold_requested():
        bootstrap_automation_ids = release_first_party_automation_ids()
        if len(bootstrap_automation_ids) != 16:
            raise RuntimeError(
                "automation project bootstrap release scope must contain exactly 16 instances"
            )
        project_policy_bootstrap = await asyncio.to_thread(
            project_policy_service.bootstrap_legacy_project_policies,
            expected_automation_ids=tuple(sorted(bootstrap_automation_ids)),
            release_sha=_release_sha(),
        )
        logger.info(
            "Automation project policy bootstrap status=%s projects=%d legacy=%d require_each=%d retired_exact=%d",
            project_policy_bootstrap.get("status", "unknown"),
            project_policy_bootstrap.get("project_count", 0),
            project_policy_bootstrap.get("legacy_schedule_only", 0),
            project_policy_bootstrap.get("require_each_run", 0),
            project_policy_bootstrap.get("retired_scheduled_exact", 0),
        )
        # This conversion is intentionally replayable under the release hold:
        # its per-project audit marker and super-admin event fence make it safe
        # after a crash between the 018 bootstrap commit and this step.
        default_full_auto = await asyncio.to_thread(
            project_policy_service.ensure_default_full_auto_policies
        )
    logger.info(
        "Automation project durable full-auto defaults changed=%d",
        default_full_auto.get("changed", 0),
    )
    automation_project_policy_service = project_policy_service
    policy = PolicyEngine(
        catalog,
        scheduler_allowlist_provider=schedule_policy_service.allowlist_entries,
        project_policy_provider=project_policy_service.evaluate_invocation,
    )
    approval_service = ApprovalService(
        repository,
        policy,
        wake_runner=lambda run_id: runner_holder["runner"].wake(run_id),
    )
    feishu_approval_service = FeishuApprovalService(
        repository,
        approval_service,
        send_text=send_text_sync,
    )
    automation_project_entrypoints = AutomationProjectEntrypoints(
        project_policy_service,
        route_resolver=CommittedAutomationProjectRouteResolver(
            catalog=plugin_runtime.catalog,
            runtime_repository=plugin_runtime.runtime_repository,
            binding_resolver=plugin_runtime.binding_resolver,
            resource_provider=get_workflow_resource,
        ),
        feishu_actor_resolver=feishu_approval_service.resolve_actor,
    )
    bind_automation_project_entrypoints(automation_project_entrypoints)
    bind_feishu_approval_runtime(feishu_approval_service)
    runner = WorkflowRunner(
        repository=repository,
        catalog=catalog,
        execution_port=execution_port,
        context_builder=context_builder,
        planner=planner,
        validator=validator,
        policy=policy,
        approval_service=approval_service,
        verifier=ResultVerifier(plugin_runtime.runtime_repository),
        worker_id=f"{INSTANCE_ID}:runs",
        protected_step_start_guard=schedule_policy_service.begin_protected_step_start,
    )
    runner_holder["runner"] = runner
    dispatcher = OutboxDispatcher(
        repository,
        worker_id=f"{INSTANCE_ID}:outbox",
        # FinanceBrain may make several bounded model calls.  Keep its durable
        # lease longer than the handler timeout so a second Agent instance does
        # not reclaim and duplicate the same post-sync analysis.
        lease_seconds=3600,
        handlers={
            "orchestration.run_worker": _noop_outbox_handler,
            "orchestration.audit": _project_run_completed_event,
            "feishu.approval": feishu_approval_service.handle_outbox,
            "feishu.approval.expiry": feishu_approval_service.handle_outbox,
            "finance.failure_alert": _finance_sync_failure_handler,
            "finance.brain": lambda delivery, uow: _finance_brain_completed_handler(
                runtime,
                loop,
                delivery,
                uow,
            ),
        },
    )
    service = ControlPlaneService(
        repository,
        approval_service,
        wake_runner=runner.wake,
        cancel_active=runner.cancel_active,
        wake_outbox=dispatcher.wake,
    )
    orchestration_repository = repository
    workflow_runner = runner
    outbox_dispatcher = dispatcher
    control_plane_service = service
    runtime.configure_orchestration(
        command_gateway=gateway,
        repository=repository,
        workflow_runner=runner,
        execution_runtime=plugin_runtime.execution_router,
        control_plane_service=service,
    )
    release_hold = (
        scheduler_release_hold_requested()
        or initial_plugin_health.get("ok") is not True
    )
    await runner.start(held_for_release=release_hold)
    await dispatcher.start()
    bind_agent_runtime(runtime, loop)
    bind_agent_command_runtime(runtime)

    def on_session_restored(payload: dict) -> bool:
        account_id = str((payload or {}).get("account_id") or "").strip()
        if not account_id:
            return False
        loop.call_soon_threadsafe(
            asyncio.create_task,
            service.publish_session_restored(account_id),
        )
        return True

    register_account_session_restored(on_session_restored)

    scheduler = init_scheduler(
        runtime,
        automation_project_invoker=project_policy_service,
        include_startup_catchup=not release_hold,
    )
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

    logger.info("Agent service started instance_id=%s", INSTANCE_ID)
    yield

    logger.info("Agent service shutting down instance_id=%s", INSTANCE_ID)
    if tms_session_alert_task is not None:
        tms_session_alert_stop.set()
        tms_session_alert_task.cancel()
        with suppress(asyncio.CancelledError):
            await tms_session_alert_task
    scheduler.shutdown(wait=False)
    register_account_session_restored(None)
    bind_agent_command_runtime(None)
    bind_automation_project_entrypoints(None)
    bind_feishu_approval_runtime(None)
    await stop_feishu_ws()
    await runner.stop()
    await dispatcher.stop()
    await plugin_runtime.stop()
    await runtime.close()
    bind_automation_project_entrypoints(None)
    control_plane_service = None
    scheduled_task_approval_service = None
    scheduled_task_approval_bootstrap = {}
    automation_plugin_runtime = None
    automation_worker_transport_service = None
    automation_project_entrypoints = None
    feishu_approval_service = None
    automation_project_policy_service = None
    outbox_dispatcher = None
    workflow_runner = None
    orchestration_repository = None
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
    )
)
app.include_router(
    create_automation_plugin_management_router(
        service_provider=lambda: _automation_plugins().management,
        actor_provider=lambda request: _require_console_admin_request(request),
        include_worker_routes=WINDOWS_WORKER_RELEASE_ENABLED,
    )
)
if WINDOWS_WORKER_RELEASE_ENABLED:
    app.include_router(
        build_worker_transport_router(service_provider=_automation_worker_transport)
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
        "ACCOUNT_ACTIVE_RUN_CHECK_FAILED": 503,
        "ACCOUNT_EXECUTION_GUARD_UNAVAILABLE": 503,
        "IDEMPOTENCY_CONFLICT": 409,
        "SCHEDULE_TASK_NOT_FOUND": 404,
        "ILLEGAL_RUN_TRANSITION": 409,
        "PROJECT_ROUTE_NOT_FOUND": 404,
        "PROJECT_ROUTE_AMBIGUOUS": 409,
        "PROJECT_ROUTE_STALE": 409,
        "PROJECT_RUNTIME_RECONCILING": 409,
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
    if WINDOWS_WORKER_RELEASE_ENABLED and is_worker_transport_path(request.url.path):
        # Worker routes authenticate a loopback TLS proxy, the paired mTLS
        # certificate and a signed device envelope.  They never accept the
        # Console/internal bearer token as a substitute for device identity.
        return await call_next(request)
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
    return await call_next(request)


def _admin_request_requires_console_principal(path: str) -> bool:
    """Keep service authentication separate from administrator authority."""

    normalized = "/" + str(path or "").lstrip("/")
    return (
        normalized == "/internal/v1/scheduled-task-approval-policies"
        or normalized == "/internal/v1/automation-project-policies"
        or normalized.startswith("/internal/v1/automation-projects/")
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
    route_key = request.url.path.strip("/")
    return await _automation_project_entrypoint_service().invoke_webhook(
        route_key=route_key,
        source_event_id=source_event_id,
        webhook_path=route_key,
        envelope=envelope,
    )


@app.post("/webhook/{path:path}")
async def webhook_handler(path: str, request: Request):
    await _verify_webhook_token(request)
    envelope = await _webhook_envelope(request)
    source_event_id = _stable_webhook_event_id(envelope)
    route_key = request.url.path.strip("/")
    logger.info("Verified automation project Webhook route hit: /%s", path)
    return await _automation_project_entrypoint_service().invoke_webhook(
        route_key=route_key,
        source_event_id=source_event_id,
        webhook_path=route_key,
        envelope=envelope,
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
                "outbox": _orchestration_repo().outbox_health(),
                "scheduler": scheduler_runtime_status(),
                "workflow_runner": (
                    workflow_runner.runtime_status()
                    if workflow_runner is not None
                    else {
                        "state": "stopped",
                        "release_hold": False,
                        "active_runs": 0,
                    }
                ),
                "automation_plugins": _automation_plugin_health(),
                "automation_workers": _automation_worker_dispatch_health(
                    release_hold=scheduler_release_hold_requested()
                ),
                "scheduled_task_approval_bootstrap": dict(scheduled_task_approval_bootstrap),
                "tms_session": get_session_broker().describe_status(validate=False),
            },
            "last_tool_run": runtime.last_tool_info(),
            "heavy_task_lock": runtime.heavy_lock_held(),
        }
    )


class ActorPayload(BaseModel):
    actor_type: str
    actor_id: str
    roles: list[str] = Field(default_factory=list)
    display_name: str = ""
    authenticated_by: str = ""


class EntityRefPayload(BaseModel):
    entity_type: str
    entity_id: str
    source_system: str = ""
    relation_type: str = "subject"
    metadata: dict = Field(default_factory=dict)


class CommandRequest(BaseModel):
    command_type: str
    parameters: dict = Field(default_factory=dict)
    idempotency_key: str
    entity_refs: list[EntityRefPayload] = Field(default_factory=list)
    correlation_id: str | None = None
    actor: ActorPayload | None = None
    actor_roles: list[str] = Field(default_factory=list)
    source: str


class TrustedActionRequest(BaseModel):
    actor: ActorPayload | None = None
    actor_roles: list[str] = Field(default_factory=list)
    source: str


class CancelRunRequest(TrustedActionRequest):
    comment: str = ""


class RetryRunRequest(TrustedActionRequest):
    reason: str = ""


class ClarificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = ""
    account_id: str | None = None
    argument_updates: dict[str, Any] | None = None


class ClarifyRunRequest(TrustedActionRequest):
    clarification: str | ClarificationPayload


class AssignWorkItemRequest(TrustedActionRequest):
    owner_id: str
    expected_version: int | None = None


class ApprovalDecisionRequest(TrustedActionRequest):
    approval_id: str | None = None
    plan_hash: str
    comment: str = ""


class ScheduledTaskApprovalPolicyRequest(TrustedActionRequest):
    model_config = ConfigDict(extra="forbid")

    task_ids: list[str]
    mode: str
    comment: str = ""
    request_id: str
    expected_versions: dict[str, int]
    expected_configuration_versions: dict[str, int]


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


def _command_from_request(req: CommandRequest, request: Request) -> Command:
    actor, source = _http_request_actor(request, requested_source=req.source)
    parameters = dict(req.parameters)
    if req.command_type == "tool.execute" and "arguments" not in parameters:
        raise OrchestrationError("INVALID_TOOL_ARGUMENTS", "tool.execute parameters require arguments")
    return Command(
        command_type=req.command_type,
        source=source,
        actor=actor,
        parameters=parameters,
        idempotency_key=req.idempotency_key,
        entity_refs=tuple(
            EntityRef(**reference.model_dump()) for reference in req.entity_refs
        ),
        correlation_id=req.correlation_id or new_id(),
    )


def _action_actor(req: TrustedActionRequest, request: Request) -> Actor:
    actor, _source = _http_request_actor(request, requested_source=req.source)
    return actor


def _require_console_admin_action(req: TrustedActionRequest, request: Request) -> Actor:
    actor = _action_actor(req, request)
    if (
        req.source != "console"
        or actor.actor_type is not ActorType.CONSOLE_ADMIN
        or not {"admin", "super_admin"}.intersection(actor.roles)
    ):
        raise OrchestrationError(
            "ACTION_FORBIDDEN",
            "This control-plane action requires an authenticated Console administrator",
        )
    return actor


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
async def get_feishu_approval_binding(request: Request):
    actor = _require_console_super_admin_request(request)
    return api_success(await asyncio.to_thread(_feishu_approvals().binding_status, int(actor.actor_id)))


@app.post("/internal/v1/admin/feishu-approval-binding/challenge")
async def create_feishu_approval_binding_challenge(request: Request):
    actor = _require_console_super_admin_request(request)
    return api_success(
        await asyncio.to_thread(
            _feishu_approvals().create_binding_challenge,
            int(actor.actor_id),
        )
    )


@app.delete("/internal/v1/admin/feishu-approval-binding")
async def revoke_feishu_approval_binding(request: Request):
    actor = _require_console_super_admin_request(request)
    await asyncio.to_thread(_feishu_approvals().revoke_binding, int(actor.actor_id))
    return api_success({"revoked": True})


@app.post("/internal/v1/commands", status_code=202)
async def submit_command(req: CommandRequest, request: Request):
    receipt = _runtime().submit_command(_command_from_request(req, request))
    return JSONResponse(
        status_code=202,
        content=api_success(receipt.to_dict()),
        headers={"Location": f"/internal/v1/runs/{receipt.run_id}"},
    )


@app.get("/internal/v1/runs/{run_id}")
async def get_control_plane_run(run_id: str, request: Request):
    _require_console_admin_request(request)
    return api_success(_control_plane().get_run(run_id))


@app.post("/internal/v1/runs/{run_id}/cancel")
async def cancel_control_plane_run(run_id: str, req: CancelRunRequest, request: Request):
    return api_success(
        await _control_plane().cancel_run(
            run_id,
            actor=_require_console_admin_action(req, request),
            comment=req.comment,
        )
    )


@app.post("/internal/v1/runs/{run_id}/retry")
async def retry_control_plane_run(run_id: str, req: RetryRunRequest, request: Request):
    return api_success(
        _control_plane().retry_run(
            run_id,
            actor=_require_console_admin_action(req, request),
            reason=req.reason,
        )
    )


@app.post("/internal/v1/runs/{run_id}/clarify")
async def clarify_control_plane_run(run_id: str, req: ClarifyRunRequest, request: Request):
    clarification = req.clarification
    if isinstance(clarification, ClarificationPayload):
        clarification = clarification.model_dump(exclude_none=True)
    return api_success(
        _control_plane().clarify_run(
            run_id,
            actor=_require_console_admin_action(req, request),
            clarification=clarification,
        )
    )


@app.get("/internal/v1/work-items")
async def list_control_plane_work_items(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    item_type: str | None = Query(default=None, alias="type"),
    source: str | None = None,
    owner: str | None = None,
    sla: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    _require_console_admin_request(request)
    normalized_page = max(1, int(page))
    normalized_page_size = max(1, min(int(page_size), 500))
    offset = (normalized_page - 1) * normalized_page_size
    sla_from, sla_before, sla_missing = _work_item_sla_filter(sla)
    data = _control_plane().list_work_items(
        status=status,
        item_type=item_type,
        priority=priority,
        source=source,
        query=q,
        owner_id=owner,
        sla_from=sla_from,
        sla_before=sla_before,
        sla_missing=sla_missing,
        limit=normalized_page_size,
        offset=offset,
    )
    data.update(
        {
            "page": normalized_page,
            "page_size": normalized_page_size,
            "has_previous": normalized_page > 1,
        }
    )
    return api_success(data)


def _work_item_sla_filter(
    value: str | None,
) -> tuple[datetime | None, datetime | None, bool | None]:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None, None, None
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    today_start = datetime.combine(now.date(), datetime_time.min)
    tomorrow_start = today_start + timedelta(days=1)
    if normalized == "overdue":
        return None, now, False
    if normalized == "today":
        return today_start, tomorrow_start, False
    if normalized == "upcoming":
        return tomorrow_start, None, False
    if normalized == "missing":
        return None, None, True
    raise OrchestrationError("INVALID_SLA_FILTER", "Unsupported SLA filter")


@app.get("/internal/v1/work-items/{work_item_id}")
async def get_control_plane_work_item(work_item_id: str, request: Request):
    _require_console_admin_request(request)
    return api_success(_control_plane().get_work_item(work_item_id))


@app.get("/internal/v1/work-items/{work_item_id}/timeline")
async def get_control_plane_timeline(
    work_item_id: str,
    request: Request,
    limit: int = 500,
):
    _require_console_admin_request(request)
    return api_success(_control_plane().get_timeline(work_item_id, limit=limit))


@app.get("/internal/v1/work-items/{work_item_id}/evidence")
async def get_control_plane_evidence(
    work_item_id: str,
    request: Request,
    run_id: str | None = None,
    limit: int = 200,
):
    _require_console_admin_request(request)
    return api_success(_control_plane().list_evidence(work_item_id, run_id=run_id, limit=limit))


@app.post("/internal/v1/work-items/{work_item_id}/assign")
async def assign_control_plane_work_item(
    work_item_id: str,
    req: AssignWorkItemRequest,
    request: Request,
):
    actor = _require_console_admin_action(req, request)
    current = _orchestration_repo().get_work_item(work_item_id)
    if current is None:
        raise OrchestrationError("WORK_ITEM_NOT_FOUND", "Work item was not found")
    expected_version = req.expected_version if req.expected_version is not None else int(current["version"])
    return api_success(
        _control_plane().assign_work_item(
            work_item_id,
            expected_version=expected_version,
            owner_type=actor.actor_type.value,
            owner_id=req.owner_id,
        )
    )


@app.post("/internal/v1/approvals/{approval_id}/approve")
async def approve_control_plane_plan(
    approval_id: str,
    req: ApprovalDecisionRequest,
    request: Request,
):
    if req.approval_id and req.approval_id != approval_id:
        raise OrchestrationError("APPROVAL_ID_MISMATCH", "Path and body approval IDs do not match")
    return api_success(
        _control_plane().approve(
            approval_id,
            plan_hash=req.plan_hash,
            actor=_require_console_admin_action(req, request),
            source="console",
            comment=req.comment,
        )
    )


@app.post("/internal/v1/approvals/{approval_id}/reject")
async def reject_control_plane_plan(
    approval_id: str,
    req: ApprovalDecisionRequest,
    request: Request,
):
    if req.approval_id and req.approval_id != approval_id:
        raise OrchestrationError("APPROVAL_ID_MISMATCH", "Path and body approval IDs do not match")
    return api_success(
        _control_plane().reject(
            approval_id,
            plan_hash=req.plan_hash,
            actor=_require_console_admin_action(req, request),
            source="console",
            comment=req.comment,
        )
    )


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
            "error": "tool-name cancellation is disabled; cancel the durable Run by run_id",
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


class FinanceAnalyzeRequest(BaseModel):
    limit: int = 20


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
            "tool-name cancellation is disabled; use POST /internal/v1/runs/{run_id}/cancel",
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
    runner = workflow_runner
    if runner is None:
        raise HTTPException(status_code=409, detail="Workflow runner is unavailable")
    async with RELEASE_ACTIVATION_LOCK:
        try:
            release_marker_present = scheduler_release_hold_requested()
            plugin_runtime = _automation_plugins()
            await asyncio.to_thread(plugin_runtime.reconcile)
            plugin_status = plugin_runtime.health()
            if plugin_status.get("ok") is not True:
                raise RuntimeError("Automation plugin service integrity check failed")
            scheduler_status = begin_scheduler_release_activation(_release_sha())
            runner_status = runner.resume_after_release()
            if WINDOWS_WORKER_RELEASE_ENABLED:
                worker_status = _automation_worker_dispatch_health(release_hold=False)
                worker_ready = (
                    worker_status.get("state") == "running"
                    and worker_status.get("release_hold") is False
                    and int(worker_status.get("active_jobs") or 0) == 0
                )
            else:
                worker_status = {
                    "enabled": False,
                    "state": "disabled",
                    "release_hold": False,
                    "active_jobs": 0,
                }
                worker_ready = True
            if (
                scheduler_status.get("state") != "running"
                or bool(scheduler_status.get("release_hold"))
                != release_marker_present
                or runner_status.get("state") != "running"
                or runner_status.get("release_hold") is not False
                or not worker_ready
                or plugin_status.get("ok") is not True
            ):
                raise RuntimeError("Release runtimes did not enter the running state")
            # Marker consumption is the final mutation. Explicitly unavailable
            # projects remain blocked by their own runtime status; they do not
            # prevent healthy projects or the scheduler from being activated.
            # Windows Worker is explicitly out of scope and therefore has no
            # route, signer or dispatcher gate.
            scheduler_status = consume_scheduler_release_hold(_release_sha())
        except RuntimeError as exc:
            if scheduler_release_hold_requested():
                try:
                    runner.hold_for_release()
                except RuntimeError:
                    logger.exception("Workflow runner could not be re-held after activation failure")
                try:
                    pause_scheduler_for_release()
                except RuntimeError:
                    logger.exception("Scheduler could not be re-held after activation failure")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = {
        **scheduler_status,
        "workflow_runner": runner.runtime_status(),
        "automation_plugins": plugin_status,
        "automation_workers": worker_status,
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


@app.post("/internal/v1/admin/finance/reviews/analyze")
async def internal_analyze_finance_reviews(req: FinanceAnalyzeRequest):
    brain = _runtime().finance_brain
    if brain is None:
        raise HTTPException(status_code=503, detail="finance brain is not initialized")
    return api_success(await brain.analyze_pending(limit=max(1, min(req.limit, 100))))


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


@app.get("/internal/v1/scheduled-task-approval-policies")
async def internal_scheduled_task_approval_policies(request: Request):
    _require_console_admin_request(request)
    return api_success(_scheduled_task_approvals().list_policies())


@app.post("/internal/v1/scheduled-task-approval-policies")
async def update_scheduled_task_approval_policies(
    req: ScheduledTaskApprovalPolicyRequest,
    request: Request,
):
    actor = _require_console_admin_action(req, request)
    return api_success(
        _scheduled_task_approvals().set_policies(
            task_ids=req.task_ids,
            mode=req.mode,
            comment=req.comment,
            request_id=req.request_id,
            expected_versions=req.expected_versions,
            expected_configuration_versions=req.expected_configuration_versions,
            actor=actor,
        )
    )


@app.post("/admin/seed-phase7-tasks", deprecated=True)
async def seed_phase7_tasks():
    runtime = _runtime()
    seeded = list(seed_phase7_schedule_tasks(runtime))
    logger.info("Seeded Phase 7 schedule templates: %s", ", ".join(seeded))
    scheduler = reload_scheduler(runtime)
    return {"status": "ok", "seeded": seeded, "scheduler": scheduler}


@app.post("/internal/v1/admin/seed-phase7-tasks")
async def internal_seed_phase7_tasks():
    return api_success(await seed_phase7_tasks())


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
