from __future__ import annotations

from copy import deepcopy

import pytest

from agent.automation_plugins.first_party_handlers import customer_problem_identity
from agent.automation_plugins.models import GenerationVerificationContext
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    OperationType,
    OrchestrationError,
    PlanStep,
    ToolResult,
    sha256_json,
)
from agent.orchestration.pilot_projection import PilotProjectionService


class FakePilotSources:
    def __init__(self) -> None:
        self.sync_run = {
            "run_id": "source-run-1",
            "status": "success",
            "degraded": False,
            "r13_complete": True,
            "problems_complete": True,
            "signs_complete": True,
        }
        self.ledger: list[dict] = []
        self.arrivals: list[dict] = []
        self.problems: list[dict] = []
        self.signs: list[dict] = []

    def get_daily_sign_sync_run(self, run_id: str, *, for_update: bool = False):
        del for_update
        return deepcopy(self.sync_run) if run_id == self.sync_run["run_id"] else None

    def list_daily_sign_ledger(self, *, for_update: bool = False):
        del for_update
        return deepcopy(self.ledger)

    def list_active_arrival_evidence(self):
        return deepcopy(self.arrivals)

    def list_valid_problem_evidence(self):
        return deepcopy(self.problems)

    def list_main_sign_evidence(self):
        return deepcopy(self.signs)


class FakeWorkItems:
    def __init__(self, items: list[dict] | None = None) -> None:
        self.items = {str(item["dedupe_key"]): deepcopy(item) for item in (items or [])}
        self.entities: list[dict] = []

    def list_by_type(self, item_type: str, *, for_update: bool = False):
        del for_update
        return [deepcopy(item) for item in self.items.values() if item["type"] == item_type]

    def create_or_get(self, row: dict):
        key = str(row["dedupe_key"])
        existing = self.items.get(key)
        if existing is not None:
            return {**deepcopy(existing), "_created": False}
        item = {
            **deepcopy(row),
            "version": 1,
            "current_reason_code": row.get("current_reason_code"),
            "current_reason_summary": row.get("current_reason_summary"),
        }
        self.items[key] = item
        return {**deepcopy(item), "_created": True}

    def transition(
        self,
        work_item_id: str,
        *,
        expected_version: int,
        expected_statuses,
        status: str,
        reason_code=None,
        reason_summary=None,
        resolution=None,
        closed_at=None,
    ):
        item = self._by_id(work_item_id)
        assert item["version"] == expected_version
        assert item["status"] in expected_statuses
        item.update(
            status=status,
            current_reason_code=reason_code,
            current_reason_summary=reason_summary,
            resolution_json=resolution,
            closed_at=closed_at,
            version=item["version"] + 1,
        )
        return deepcopy(item)

    def refresh_projection(
        self,
        work_item_id: str,
        *,
        expected_version: int,
        title: str,
        priority: str,
        source: str,
        sla_deadline,
        reason_code,
        reason_summary,
    ):
        item = self._by_id(work_item_id)
        assert item["version"] == expected_version
        item.update(
            title=title,
            priority=priority,
            source=source,
            sla_deadline=sla_deadline,
            current_reason_code=reason_code,
            current_reason_summary=reason_summary,
            version=item["version"] + 1,
        )
        return deepcopy(item)

    def add_entity(self, row: dict) -> None:
        self.entities.append(deepcopy(row))

    def _by_id(self, work_item_id: str) -> dict:
        return next(item for item in self.items.values() if item["work_item_id"] == work_item_id)


class FakeEvidence:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, row: dict):
        self.rows.append(deepcopy(row))
        return deepcopy(row)


class FakeEvents:
    def __init__(self) -> None:
        self.rows: list[tuple[dict, tuple]] = []

    def append_with_outbox(self, event: dict, outbox):
        value = (deepcopy(event), tuple(deepcopy(tuple(outbox))))
        self.rows.append(value)
        return {"event": event, "outbox": list(outbox)}


class FakeUow:
    def __init__(self, items: list[dict] | None = None) -> None:
        self.pilot_sources = FakePilotSources()
        self.work_items = FakeWorkItems(items)
        self.evidence = FakeEvidence()
        self.events = FakeEvents()


def _command() -> Command:
    return Command(
        command_id="command-1",
        correlation_id="correlation-1",
        command_type="tool.execute",
        source="scheduler",
        actor=Actor(actor_type=ActorType.SCHEDULER, actor_id="scheduler"),
        parameters={"tool_name": "pilot"},
        idempotency_key="scheduler:pilot:2026-08-13T00:00:00+08:00",
    )


def _step(tool_name: str) -> PlanStep:
    return PlanStep(
        step_key="step-1",
        tool_name=tool_name,
        tool_version="1.0.0",
        operation_type=OperationType.READ,
        arguments={},
        account_id=None,
        depends_on=(),
        idempotency_key="step-key",
        expected_evidence=(),
        postconditions=(),
    )


def _run() -> dict:
    return {
        "run_id": "run-1",
        "work_item_id": "gateway-item",
        "correlation_id": "correlation-1",
    }


def _step_row() -> dict:
    return {"step_id": "step-row-1"}


def _daily_result() -> ToolResult:
    return ToolResult(
        status="SUCCESS",
        data={"source_run_id": "source-run-1", "legacy_candidate_keys": []},
        meta={
            "observed_at": "2026-08-13T01:00:00Z",
            "pagination_complete": True,
            "account_id": "all_configured",
        },
    )


def _customer_result(detail_rechecks: list[dict]) -> ToolResult:
    return ToolResult(
        status="SUCCESS",
        data={
            "open_items": [],
            "resolved_items": [],
            "account_proofs": [
                {
                    "platform": "yunda",
                    "account_id": "account-1",
                    "direction": direction,
                    "total": 0,
                    "unique_records": 0,
                    "pages": [{"page": 1, "returned": 0}],
                    "pagination_complete": True,
                }
                for direction in ("received", "published")
            ],
            "detail_rechecks": detail_rechecks,
            "legacy_candidate_keys": [],
            "legacy_source_complete": True,
            "legacy_source_errors": [],
        },
        meta={
            "observed_at": "2026-08-13T01:00:00Z",
            "pagination_complete": True,
            "account_id": "all_configured",
        },
    )


def _existing_item(dedupe_key: str, item_type: str, *, status: str = "OPEN") -> dict:
    return {
        "work_item_id": f"item-{dedupe_key}",
        "command_id": "old-command",
        "type": item_type,
        "title": dedupe_key,
        "status": status,
        "priority": "NORMAL",
        "source": "test",
        "dedupe_key": dedupe_key,
        "sla_deadline": None,
        "version": 1,
    }


def _plugin_customer_verification() -> GenerationVerificationContext:
    return GenerationVerificationContext(
        automation_id="customer-project",
        generation=2,
        lease_id="00000000-0000-4000-8000-000000000001",
        account_ids=("account-1",),
        account_bindings_sha256="a" * 64,
        requires_write_verification=False,
    )


def _plugin_customer_result(*, external_id: str = "opaque-1") -> ToolResult:
    opaque_key = customer_problem_identity(
        account_id="account-1",
        platform="yunda",
        external_id=external_id,
    )
    return ToolResult(
        status="SUCCESS",
        data={
            "records": [
                {
                    "dedupe_key": opaque_key,
                    "platform": "yunda",
                    "source_direction": "query",
                    "external_id": external_id,
                    "waybill_no": "430000000009",
                    "status": "待处理",
                    "reply_text": "",
                    "resolved": False,
                    "resolution_reason": "",
                }
            ],
            "rechecks": [],
            "evidence": {
                "configured_accounts_queried": True,
                "pagination_complete": True,
                "page_count": 1,
                "record_count": 1,
            },
        },
        meta={
            "observed_at": "2026-08-13T01:00:00Z",
            "pagination_complete": True,
            "account_id": "binding-set:" + "a" * 64,
        },
    )


def _plugin_customer_context_error_result(
    dedupe_key: str,
    *,
    context_error: str = "SUBJECT_ENTITY_MISSING",
    status: str = "BLOCKED_DATA",
    source_returned: bool = False,
) -> ToolResult:
    result = _plugin_customer_result()
    return ToolResult(
        status=result.status,
        data={
            **result.data,
            "records": [],
            "rechecks": [
                {
                    "dedupe_key": dedupe_key,
                    "context_error": context_error,
                    "status": status,
                    "resolution_reason": (
                        "explicit_terminal_status" if status == "RESOLVED" else ""
                    ),
                    "error_code": context_error,
                    "source_returned": source_returned,
                    "evidence": {},
                }
            ],
            "evidence": {
                **result.data["evidence"],
                "record_count": 0,
            },
        },
        meta=result.meta,
    )


def test_plugin_customer_projection_resolves_opaque_identity_from_trusted_side_channel() -> None:
    result = _plugin_customer_result()
    opaque_key = str(result.data["records"][0]["dedupe_key"])
    uow = FakeUow()

    outcome = PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=result,
        generation_verification=_plugin_customer_verification(),
    )

    assert opaque_key in uow.work_items.items
    assert uow.work_items.items[opaque_key]["status"] == "OPEN"
    customer_evidence = next(
        row for row in uow.evidence.rows if row["source_record_type"] == "customer_problem"
    )
    assert customer_evidence["account_id"] == "account-1"
    assert "account_id" not in result.data["records"][0]
    assert outcome is not None
    assert outcome.candidate_keys == (opaque_key,)


def test_plugin_customer_projection_reuses_legacy_item_without_exposing_account() -> None:
    external_id = "legacy-1"
    legacy_key = f"problem:yunda:account-1:{external_id}"
    uow = FakeUow(
        [_existing_item(legacy_key, "CUSTOMER_SERVICE_PROBLEM")]
    )
    result = _plugin_customer_result(external_id=external_id)

    PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=result,
        generation_verification=_plugin_customer_verification(),
    )

    assert list(uow.work_items.items) == [legacy_key]
    assert "account_id" not in result.data["records"][0]


def test_plugin_customer_projection_rejects_binding_set_mismatch_before_mutation() -> None:
    result = _plugin_customer_result()
    result = ToolResult(
        status=result.status,
        data=result.data,
        meta={**result.meta, "account_id": "binding-set:" + "b" * 64},
    )
    uow = FakeUow()

    with pytest.raises(OrchestrationError) as exc_info:
        PilotProjectionService().project_successful_step(
            uow=uow,
            run=_run(),
            step_row=_step_row(),
            step=_step("sync_customer_service_problems"),
            command=_command(),
            result=result,
            generation_verification=_plugin_customer_verification(),
        )

    assert exc_info.value.code == "CUSTOMER_ACCOUNT_SCOPE_INCOMPLETE"
    assert uow.work_items.items == {}


def test_plugin_customer_context_error_retains_exact_persisted_item_as_blocked() -> None:
    key = "problem:v1:" + ("c" * 64)
    uow = FakeUow([_existing_item(key, "CUSTOMER_SERVICE_PROBLEM")])

    outcome = PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=_plugin_customer_context_error_result(key),
        generation_verification=_plugin_customer_verification(),
    )

    item = uow.work_items.items[key]
    assert item["status"] == "BLOCKED_DATA"
    assert item["current_reason_code"] == "SUBJECT_ENTITY_MISSING"
    assert item.get("resolution_json") is None
    assert outcome is not None
    assert f"detail_recheck:{key}" in outcome.incomplete_sources


def test_plugin_customer_context_error_retains_exact_legacy_persisted_key() -> None:
    key = "problem:yunda:account-1:legacy-context-error"
    uow = FakeUow([_existing_item(key, "CUSTOMER_SERVICE_PROBLEM")])

    PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=_plugin_customer_context_error_result(key),
        generation_verification=_plugin_customer_verification(),
    )

    assert list(uow.work_items.items) == [key]
    assert uow.work_items.items[key]["status"] == "BLOCKED_DATA"
    assert uow.work_items.items[key]["current_reason_code"] == "SUBJECT_ENTITY_MISSING"


def test_plugin_customer_context_error_never_binds_a_colliding_normalized_alias() -> None:
    origin_key = "problem:yunda:account-1:persisted-external"
    colliding_key = customer_problem_identity(
        account_id="account-1",
        platform="yunda",
        external_id="other-open-item",
    )
    uow = FakeUow(
        [
            _existing_item(origin_key, "CUSTOMER_SERVICE_PROBLEM"),
            _existing_item(colliding_key, "CUSTOMER_SERVICE_PROBLEM"),
        ]
    )

    PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=_plugin_customer_context_error_result(
            origin_key,
            context_error="SUBJECT_ENTITY_IDENTITY_MISMATCH",
        ),
        generation_verification=_plugin_customer_verification(),
    )

    assert uow.work_items.items[origin_key]["status"] == "BLOCKED_DATA"
    assert (
        uow.work_items.items[origin_key]["current_reason_code"]
        == "SUBJECT_ENTITY_IDENTITY_MISMATCH"
    )
    assert uow.work_items.items[colliding_key]["status"] == "BLOCKED_DATA"
    assert (
        uow.work_items.items[colliding_key]["current_reason_code"]
        == "PROBLEM_DISAPPEARED_NEEDS_DETAIL"
    )


def test_plugin_customer_context_error_rejects_unpersisted_identity() -> None:
    persisted_key = "problem:v1:" + ("d" * 64)
    forged_key = "problem:v1:" + ("e" * 64)
    uow = FakeUow([_existing_item(persisted_key, "CUSTOMER_SERVICE_PROBLEM")])

    with pytest.raises(OrchestrationError) as exc_info:
        PilotProjectionService().project_successful_step(
            uow=uow,
            run=_run(),
            step_row=_step_row(),
            step=_step("sync_customer_service_problems"),
            command=_command(),
            result=_plugin_customer_context_error_result(forged_key),
            generation_verification=_plugin_customer_verification(),
        )

    assert exc_info.value.code == "UNEXPECTED_PROBLEM_DETAIL_RECHECK"
    assert uow.work_items.items[persisted_key]["status"] == "OPEN"
    assert uow.evidence.rows == []


def test_plugin_customer_context_error_can_never_claim_resolution() -> None:
    key = "problem:v1:" + ("f" * 64)
    uow = FakeUow([_existing_item(key, "CUSTOMER_SERVICE_PROBLEM")])

    with pytest.raises(OrchestrationError) as exc_info:
        PilotProjectionService().project_successful_step(
            uow=uow,
            run=_run(),
            step_row=_step_row(),
            step=_step("sync_customer_service_problems"),
            command=_command(),
            result=_plugin_customer_context_error_result(
                key,
                status="RESOLVED",
                source_returned=True,
            ),
            generation_verification=_plugin_customer_verification(),
        )

    assert exc_info.value.code == "PROBLEM_RECHECK_CONTEXT_INVALID"
    assert uow.work_items.items[key]["status"] == "OPEN"


def test_daily_sign_requires_main_waybill_sign_evidence() -> None:
    uow = FakeUow()
    uow.pilot_sources.ledger = [
        {
            "tracking_number": "R001",
            "system_sign_due_at": "2026-08-13 23:59:59",
            "tms_signed": True,
        }
    ]

    with pytest.raises(OrchestrationError) as exc_info:
        PilotProjectionService().project_successful_step(
            uow=uow,
            run=_run(),
            step_row=_step_row(),
            step=_step("sync_daily_should_sign"),
            command=_command(),
            result=_daily_result(),
        )

    assert exc_info.value.code == "UNPROVEN_DAILY_SIGN_CLOSURE"


def test_daily_sign_missing_sla_is_blocked_and_excluded_from_home_projection() -> None:
    uow = FakeUow()
    uow.pilot_sources.ledger = [
        {
            "tracking_number": "R002",
            "system_sign_due_at": None,
            "r13_plan_sign_at": None,
            "tms_signed": False,
            "calculation_trace": {"reason": "no_actual_arrival"},
        }
    ]

    outcome = PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_daily_should_sign"),
        command=_command(),
        result=_daily_result(),
    )

    item = uow.work_items.items["daily_sign:R002"]
    assert item["status"] == "BLOCKED_DATA"
    assert item["current_reason_code"] == "SIGN_SLA_MISSING"
    assert outcome is not None
    assert outcome.candidate_keys == ()
    assert any(row[0]["event_type"] == "projection.shadow_compared" for row in uow.events.rows)


def test_daily_sign_closes_only_with_real_main_sign_event() -> None:
    existing = _existing_item("daily_sign:R003", "DAILY_SIGN")
    uow = FakeUow([existing])
    uow.pilot_sources.ledger = [{"tracking_number": "R003", "tms_signed": True}]
    uow.pilot_sources.signs = [
        {
            "source": "tms_scan",
            "external_id": "sign-1",
            "tracking_number": "R003",
            "scan_code": "R003",
            "scan_type": "签收",
            "scanned_at": "2026-08-13 08:30:00",
            "scan_site": "site",
        }
    ]

    PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_daily_should_sign"),
        command=_command(),
        result=_daily_result(),
    )

    item = uow.work_items.items["daily_sign:R003"]
    assert item["status"] == "RESOLVED"
    assert item["current_reason_code"] == "TMS_MAIN_WAYBILL_SIGNED"


def test_customer_problem_projection_closes_explicit_and_blocks_disappeared() -> None:
    disappeared = _existing_item(
        "problem:ronghui:account-1:old-id",
        "CUSTOMER_SERVICE_PROBLEM",
    )
    uow = FakeUow([disappeared])
    result = ToolResult(
        status="SUCCESS",
        data={
            "open_items": [
                {
                    "platform": "ronghui",
                    "account_id": "account-1",
                    "external_id": "open-id",
                    "dedupe_key": "problem:ronghui:account-1:open-id",
                    "waybill_no": "R100",
                    "source_direction": "received",
                    "status": "未回复",
                    "reply_text": "",
                    "resolved": False,
                    "resolution_reason": "",
                }
            ],
            "resolved_items": [
                {
                    "platform": "ronghui",
                    "account_id": "account-1",
                    "external_id": "resolved-id",
                    "dedupe_key": "problem:ronghui:account-1:resolved-id",
                    "waybill_no": "R101",
                    "source_direction": "received",
                    "status": "已回复",
                    "reply_text": "已处理",
                    "resolved": True,
                    "resolution_reason": "explicit_reply",
                }
            ],
            "account_proofs": [
                {
                    "platform": "ronghui",
                    "account_id": "account-1",
                    "direction": "received",
                    "total": 2,
                    "unique_records": 2,
                    "pages": [{"page": 1, "returned": 2}],
                    "pagination_complete": True,
                },
                {
                    "platform": "ronghui",
                    "account_id": "account-1",
                    "direction": "published",
                    "total": 0,
                    "unique_records": 0,
                    "pages": [{"page": 1, "returned": 0}],
                    "pagination_complete": True,
                },
            ],
            "detail_rechecks": [],
            "legacy_candidate_keys": ["problem:ronghui:account-1:open-id"],
            "legacy_source_complete": True,
            "legacy_source_errors": [],
        },
        meta={
            "observed_at": "2026-08-13T01:00:00Z",
            "pagination_complete": True,
            "account_id": "all_configured",
        },
    )

    outcome = PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=result,
    )

    assert uow.work_items.items["problem:ronghui:account-1:open-id"]["status"] == "OPEN"
    assert uow.work_items.items["problem:ronghui:account-1:resolved-id"]["status"] == "RESOLVED"
    old = uow.work_items.items["problem:ronghui:account-1:old-id"]
    assert old["status"] == "BLOCKED_DATA"
    assert old["current_reason_code"] == "PROBLEM_DISAPPEARED_NEEDS_DETAIL"
    assert outcome is not None
    assert outcome.candidate_keys == ("problem:ronghui:account-1:open-id",)
    assert outcome.source_complete is False
    assert "detail_recheck:problem:ronghui:account-1:old-id" in outcome.incomplete_sources
    assert any(entity["entity_type"] == "waybill" for entity in uow.work_items.entities)


def test_disappeared_problem_resolves_only_from_exact_detail_evidence() -> None:
    key = "problem:yunda:account-1:external-1"
    uow = FakeUow([_existing_item(key, "CUSTOMER_SERVICE_PROBLEM", status="BLOCKED_DATA")])
    result = _customer_result(
        [
            {
                "dedupe_key": key,
                "platform": "yunda",
                "account_id": "account-1",
                "external_id": "external-1",
                "source_direction": "received",
                "status": "RESOLVED",
                "resolution_reason": "explicit_terminal_status",
                "error_code": "",
                "source_returned": True,
                "evidence": {
                    "detail_mapping_count": 1,
                    "reply_present": False,
                    "status_values": ["已完成"],
                },
            }
        ]
    )

    PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=result,
    )

    item = uow.work_items.items[key]
    assert item["status"] == "RESOLVED"
    assert item["current_reason_code"] == "PROBLEM_DETAIL_EXPLICITLY_RESOLVED"
    assert any(
        row["source_record_type"] == "customer_problem_detail_recheck"
        and row["completeness_status"] == "COMPLETE"
        for row in uow.evidence.rows
    )


def test_disappeared_problem_detail_login_failure_blocks_exact_account() -> None:
    key = "problem:yunda:account-1:external-2"
    uow = FakeUow([_existing_item(key, "CUSTOMER_SERVICE_PROBLEM", status="BLOCKED_LOGIN")])
    result = _customer_result(
        [
            {
                "dedupe_key": key,
                "platform": "yunda",
                "account_id": "account-1",
                "external_id": "external-2",
                "source_direction": "received",
                "status": "BLOCKED_LOGIN",
                "resolution_reason": "",
                "error_code": "AUTH_REQUIRED",
                "source_returned": False,
                "evidence": {},
            }
        ]
    )

    outcome = PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=result,
    )

    item = uow.work_items.items[key]
    assert item["status"] == "BLOCKED_LOGIN"
    assert item["current_reason_code"] == "AUTH_REQUIRED"
    assert any(
        row["source_record_type"] == "customer_problem_detail_recheck"
        and row["completeness_status"] == "INCOMPLETE"
        for row in uow.evidence.rows
    )
    assert outcome is not None
    assert outcome.source_complete is False
    assert f"detail_recheck:{key}" in outcome.incomplete_sources
    shadow = next(row for row in uow.evidence.rows if row["source_record_type"] == "shadow_projection")
    assert shadow["completeness_status"] == "INCOMPLETE"


def test_disappeared_problem_unknown_detail_never_closes() -> None:
    key = "problem:yunda:account-1:external-3"
    uow = FakeUow([_existing_item(key, "CUSTOMER_SERVICE_PROBLEM")])
    result = _customer_result(
        [
            {
                "dedupe_key": key,
                "platform": "yunda",
                "account_id": "account-1",
                "external_id": "external-3",
                "source_direction": "received",
                "status": "BLOCKED_DATA",
                "resolution_reason": "",
                "error_code": "DETAIL_TERMINAL_STATE_UNPROVEN",
                "source_returned": True,
                "evidence": {
                    "detail_mapping_count": 1,
                    "reply_present": False,
                    "status_values": ["处理中"],
                },
            }
        ]
    )

    PilotProjectionService().project_successful_step(
        uow=uow,
        run=_run(),
        step_row=_step_row(),
        step=_step("sync_customer_service_problems"),
        command=_command(),
        result=result,
    )

    item = uow.work_items.items[key]
    assert item["status"] == "BLOCKED_DATA"
    assert item["current_reason_code"] == "DETAIL_TERMINAL_STATE_UNPROVEN"


def test_daily_sign_incomplete_sync_still_persists_incomplete_shadow_round() -> None:
    uow = FakeUow()
    uow.pilot_sources.sync_run.update(
        {
            "status": "failed",
            "r13_complete": False,
            "problems_complete": True,
            "signs_complete": False,
        }
    )
    result = ToolResult(
        status="FAILED",
        data={"source_run_id": "source-run-1"},
        meta={
            "observed_at": "2026-08-13T01:00:00Z",
            "pagination_complete": False,
            "account_id": "multi_account",
        },
        error={
            "code": "DAILY_SIGN_SOURCE_INCOMPLETE",
            "message": "source incomplete",
            "retryable": False,
        },
    )

    outcome = PilotProjectionService().record_incomplete_attempt(
        uow=uow,
        run=_run(),
        step_row={**_step_row(), "attempt_count": 1},
        step=_step("sync_daily_should_sign"),
        command=_command(),
        failure_code="DAILY_SIGN_SOURCE_INCOMPLETE",
        result=result,
    )

    assert outcome is not None
    assert outcome.source_complete is False
    assert outcome.candidate_hash == sha256_json([])
    assert outcome.legacy_hash == sha256_json([])
    assert outcome.candidate_keys == ()
    assert "failure:DAILY_SIGN_SOURCE_INCOMPLETE" in outcome.incomplete_sources
    assert "daily_sign_sync:r13_complete" in outcome.incomplete_sources
    assert "daily_sign_sync:signs_complete" in outcome.incomplete_sources
    assert "daily_sign_sync:status:failed" in outcome.incomplete_sources
    assert "daily_sign_candidates:unavailable" in outcome.incomplete_sources
    shadow = next(
        row for row in uow.evidence.rows if row["source_record_type"] == "shadow_projection"
    )
    assert shadow["completeness_status"] == "INCOMPLETE"
    assert shadow["record_count"] == 0
    assert shadow["summary_json"]["step_attempt_no"] == 1
    event = next(
        row[0] for row in uow.events.rows if row[0]["event_type"] == "projection.shadow_compared"
    )
    assert event["payload"]["source_complete"] is False


def test_customer_pagination_failure_preserves_computable_shadow_diff() -> None:
    uow = FakeUow()
    candidate_key = "problem:yunda:account-1:open-1"
    legacy_only_key = "problem:yunda:account-1:legacy-only"
    result = ToolResult(
        status="SUCCESS",
        data={
            "open_items": [
                {
                    "platform": "yunda",
                    "account_id": "account-1",
                    "external_id": "open-1",
                    "dedupe_key": candidate_key,
                }
            ],
            "resolved_items": [],
            "account_proofs": [
                {
                    "platform": "yunda",
                    "account_id": "account-1",
                    "direction": "received",
                    "total": 1,
                    "unique_records": 1,
                    "pages": [{"page": 1, "returned": 1}],
                    "pagination_complete": True,
                },
                {
                    "platform": "yunda",
                    "account_id": "account-1",
                    "direction": "published",
                    "total": 1,
                    "unique_records": 0,
                    "pages": [{"page": 1, "returned": 0}],
                    "pagination_complete": False,
                },
            ],
            "legacy_candidate_keys": [legacy_only_key],
            "legacy_source_complete": True,
            "legacy_source_errors": [],
        },
        meta={
            "observed_at": "2026-08-13T01:00:00Z",
            "pagination_complete": False,
            "account_id": "all_configured",
        },
    )

    outcome = PilotProjectionService().record_incomplete_attempt(
        uow=uow,
        run=_run(),
        step_row={**_step_row(), "attempt_count": 2},
        step=_step("sync_customer_service_problems"),
        command=_command(),
        failure_code="PAGINATION_INCOMPLETE",
        result=result,
    )

    assert outcome is not None
    assert outcome.source_complete is False
    assert outcome.candidate_keys == (candidate_key,)
    assert outcome.candidate_hash == sha256_json([candidate_key])
    assert outcome.legacy_hash == sha256_json([legacy_only_key])
    assert outcome.missing_keys == (legacy_only_key,)
    assert outcome.extra_keys == (candidate_key,)
    assert "failure:PAGINATION_INCOMPLETE" in outcome.incomplete_sources
    assert "customer_pagination:incomplete" in outcome.incomplete_sources
    assert (
        "customer_pagination:yunda:account-1:published"
        in outcome.incomplete_sources
    )
    shadow = next(
        row for row in uow.evidence.rows if row["source_record_type"] == "shadow_projection"
    )
    assert shadow["completeness_status"] == "INCOMPLETE"
    assert shadow["record_count"] == 1
    assert shadow["summary_json"]["step_attempt_no"] == 2
