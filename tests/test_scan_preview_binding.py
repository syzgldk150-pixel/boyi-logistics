from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.models import (
    Actor,
    ActorType,
    Command,
    ContextSnapshot,
    OperationType,
    OrchestrationError,
    RiskLevel,
)
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.plan_validator import PlanValidator
from agent.orchestration.scan_preview_binding import (
    SCAN_PREVIEW_CONTEXT_KEY,
    ScanPreviewExpectation,
    consume_scan_preview,
    ensure_scan_preview_active,
    require_scan_formal_governance,
    resolve_scan_preview,
    restore_scan_preview_replay,
    scan_preview_public_projection,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    canonical_sha256,
)
from shared.orchestration_repository_support import IdempotencyConflict


PROJECT_ID = "scan_codes"
RUN_ID = "11111111-1111-4111-8111-111111111111"
CONTRACT_DIGEST = "c" * 64
NOW = datetime(2026, 8, 24, 4, 0, tzinfo=timezone.utc)
FORMAL_ARGUMENTS = {
    "target_date": "2026-08-24",
    "batch_size": 1,
    "max_batches": 2,
}


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def get(self, identity, *, for_update=False):
        del for_update
        row = self.rows.get(identity)
        return copy.deepcopy(row) if row is not None else None


class _Steps:
    def __init__(self, rows):
        self.rows = rows

    def list_for_run(self, run_id):
        return copy.deepcopy(self.rows.get(run_id, []))


class _Commands(_Rows):
    def __init__(self, rows):
        super().__init__(rows)
        self.by_idempotency = {}

    def get_by_idempotency(self, source, idempotency_key, *, for_update=False):
        del for_update
        return copy.deepcopy(self.by_idempotency.get((source, idempotency_key)))


class _Events:
    def __init__(self):
        self.rows = {}

    def append_with_outbox(self, row, outbox):
        assert tuple(outbox) == ()
        identity = (row["source_system"], row["event_type"], row["source_event_id"])
        if identity in self.rows:
            raise IdempotencyConflict("synthetic event collision")
        self.rows[identity] = copy.deepcopy(dict(row))
        return {"event": dict(row), "created": True}


class _Uow:
    def __init__(self, fixture):
        self.runs = _Rows(fixture["runs"])
        self.commands = _Commands(fixture["commands"])
        self.steps = _Steps(fixture["steps"])
        self.events = _Events()


def _expectation() -> ScanPreviewExpectation:
    return ScanPreviewExpectation(
        project_instance_id=PROJECT_ID,
        generation=7,
        contract_digest=CONTRACT_DIGEST,
        configuration_version=3,
    )


def _fixture(*, observed_at: datetime = NOW - timedelta(minutes=2)):
    items = [
        {"bill_code": "R123456789010001", "station_name": "A站"},
        {"bill_code": "R123456789010002", "station_name": "B站"},
    ]
    preview_arguments = {**FORMAL_ARGUMENTS, "dry_run": True}
    evidence = {
        "contract_version": 1,
        "target_date": "2026-08-24",
        "observed_at": observed_at.isoformat(),
        "pagination_complete": True,
        "source_page_count": 1,
        "normalized_record_count": 3,
        "source_snapshot_sha256": "a" * 64,
        "source_evidence_refs": ["evidence:source:1"],
        "selection_count": 2,
        "selection_sha256": canonical_sha256(items),
        "batch_count": 2,
        "batch_plan_sha256": canonical_sha256([[items[0]], [items[1]]]),
        "items": items,
    }
    result = {
        "status": "SUCCESS",
        "data": {"dry_run": True, "preview_evidence": evidence},
        "meta": {},
        "warnings": [],
        "error": None,
    }
    command_id = "preview-command"
    step = {
        "step_id": "preview-step",
        "run_id": RUN_ID,
        "status": "COMPLETED",
        "postcondition_status": "VERIFIED",
        "tool_name": f"automation.{PROJECT_ID}.run",
        "result_summary_json": result,
        "result_sha256": canonical_sha256(result),
    }
    return {
        "runs": {RUN_ID: {"run_id": RUN_ID, "command_id": command_id, "status": "COMPLETED"}},
        "commands": {
            command_id: {
                "command_id": command_id,
                "command_type": "automation.project.invoke",
                "parameters_json": {
                    "tool_name": f"automation.{PROJECT_ID}.run",
                    "arguments": preview_arguments,
                },
                "automation_invocation_json": {
                    "automation_id": PROJECT_ID,
                    "automation_generation": 7,
                    "contract_hash": CONTRACT_DIGEST,
                    "project_configuration_version": 3,
                },
            }
        },
        "steps": {RUN_ID: [step]},
    }


def _resolve(uow, *, now=NOW):
    return resolve_scan_preview(
        uow,
        preview_run_id=RUN_ID,
        expectation=_expectation(),
        formal_arguments=FORMAL_ARGUMENTS,
        now=now,
        for_update=False,
    )


def _command(
    context,
    *,
    idempotency_key="formal-command-1",
    entrypoint=AutomationEntrypoint.CONSOLE,
    actor=None,
    trusted_context=None,
) -> Command:
    resolved_actor = actor or Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-1",
        ("super_admin",),
    )
    invocation = AutomationProjectInvocation(
        automation_id=PROJECT_ID,
        automation_generation=7,
        entrypoint=entrypoint,
        contract_id=entrypoint.value,
        contract_hash=CONTRACT_DIGEST,
        policy_version=2,
        project_configuration_version=3,
        request_id="formal-request",
    )
    return Command(
        command_type="automation.project.invoke",
        source=entrypoint.value,
        actor=resolved_actor,
        parameters={
            "tool_name": f"automation.{PROJECT_ID}.run",
            "arguments": {**FORMAL_ARGUMENTS, "dry_run": False},
            "execution_context": {
                "project_request_id": "formal-request",
                "entrypoint": entrypoint.value,
                "occurred_at": context["observed_at"],
                **dict(trusted_context or {}),
                SCAN_PREVIEW_CONTEXT_KEY: dict(context),
            },
        },
        idempotency_key=idempotency_key,
        automation_invocation=invocation,
    )


def test_completed_preview_binds_exact_evidence_without_copying_bill_list():
    resolved = _resolve(_Uow(_fixture()))

    assert resolved.formal_arguments == {**FORMAL_ARGUMENTS, "dry_run": False}
    assert resolved.context["selection_count"] == 2
    assert resolved.context["selection_sha256"] == canonical_sha256(
        [
            {"bill_code": "R123456789010001", "station_name": "A站"},
            {"bill_code": "R123456789010002", "station_name": "B站"},
        ]
    )
    assert "items" not in resolved.context
    assert resolved.context["expires_at"] == "2026-08-24T04:13:00Z"


def test_public_preview_projection_is_bounded_and_marks_expiry() -> None:
    active = scan_preview_public_projection(
        _Uow(_fixture()),
        preview_run_id=RUN_ID,
        expectation=_expectation(),
        now=NOW,
    )

    assert active == {
        "contract_version": 1,
        "preview_run_id": RUN_ID,
        "target_date": "2026-08-24",
        "observed_at": "2026-08-24T03:58:00Z",
        "expires_at": "2026-08-24T04:13:00Z",
        "source_page_count": 1,
        "normalized_record_count": 3,
        "selection_count": 2,
        "batch_count": 2,
        "can_confirm": True,
    }
    assert not any("sha256" in field or field == "items" for field in active)

    expired = scan_preview_public_projection(
        _Uow(_fixture(observed_at=NOW - timedelta(minutes=16))),
        preview_run_id=RUN_ID,
        expectation=_expectation(),
        now=NOW,
    )
    assert expired["can_confirm"] is False


def test_preview_expiry_result_tampering_and_argument_drift_fail_closed():
    with pytest.raises(OrchestrationError) as expired:
        _resolve(_Uow(_fixture(observed_at=NOW - timedelta(minutes=15))))
    assert expired.value.code == "SCAN_PREVIEW_EXPIRED"

    tampered = _fixture()
    tampered["steps"][RUN_ID][0]["result_summary_json"]["data"]["preview_evidence"]["items"][0][
        "bill_code"
    ] = "R-TAMPERED"
    with pytest.raises(OrchestrationError) as invalid:
        _resolve(_Uow(tampered))
    assert invalid.value.code == "SCAN_PREVIEW_INVALID"

    with pytest.raises(OrchestrationError) as stale:
        resolve_scan_preview(
            _Uow(_fixture()),
            preview_run_id=RUN_ID,
            expectation=_expectation(),
            formal_arguments={**FORMAL_ARGUMENTS, "max_batches": 1},
            now=NOW,
            for_update=False,
        )
    assert stale.value.code == "SCAN_PREVIEW_STALE"


def test_expiry_is_rechecked_after_the_preview_lock_boundary():
    context = _resolve(
        _Uow(_fixture(observed_at=NOW - timedelta(minutes=14, seconds=59)))
    ).context

    with pytest.raises(OrchestrationError) as expired:
        ensure_scan_preview_active(context, now=NOW + timedelta(seconds=1))

    assert expired.value.code == "SCAN_PREVIEW_EXPIRED"


def test_consumption_is_once_only_but_same_command_retry_is_reusable():
    uow = _Uow(_fixture())
    context = _resolve(uow).context
    first = _command(context, idempotency_key="formal-1")
    consume_scan_preview(uow, context=context, command=first, occurred_at=NOW)
    assert len(uow.events.rows) == 1

    uow.commands.by_idempotency[(first.source, first.idempotency_key)] = {"command_id": first.command_id}
    consume_scan_preview(uow, context=context, command=_command(context, idempotency_key="formal-1"), occurred_at=NOW)
    assert len(uow.events.rows) == 1

    with pytest.raises(OrchestrationError) as consumed:
        consume_scan_preview(
            uow,
            context=context,
            command=_command(context, idempotency_key="formal-2"),
            occurred_at=NOW,
        )
    assert consumed.value.code == "SCAN_PREVIEW_ALREADY_CONSUMED"


def test_accepted_command_is_restored_for_same_idempotency_replay_after_expiry():
    preview_uow = _Uow(_fixture())
    context = _resolve(preview_uow).context
    trusted_context = {
        "task_id": "scheduled-scan",
        "scheduled_for": "2026-08-24T12:00:00+08:00",
        "cron_expression": "0 12 * * *",
        "configuration_version": 3,
    }
    original = _command(
        context,
        idempotency_key="formal-replay",
        entrypoint=AutomationEntrypoint.SCHEDULER,
        actor=Actor(ActorType.SCHEDULER, "scheduled-scan", ("system",)),
        trusted_context=trusted_context,
    )

    class _GatewayCommands:
        def __init__(self, repository):
            self.repository = repository

        def get_by_idempotency(self, source, idempotency_key, *, for_update=False):
            del for_update
            row = self.repository.commands.get((source, idempotency_key))
            return copy.deepcopy(row) if row is not None else None

    class _GatewayUow:
        def __init__(self, repository):
            self.repository = repository
            self.commands = _GatewayCommands(repository)
            self.events = repository.events
            self.work_items = SimpleNamespace(add_entity=lambda _row: None)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, _exc, _tb):
            return False

        def command_gateway_create(self, command, item, run, event, outbox):
            del event, outbox
            identity = (command["source"], command["idempotency_key"])
            existing = self.repository.commands.get(identity)
            if existing is not None:
                assert existing["parameters_json"] == command["parameters"]
                return {
                    **self.repository.receipt,
                    "created": {
                        "command": False,
                        "work_item": False,
                        "run": False,
                        "event": False,
                    },
                }
            persisted = dict(command)
            persisted["actor_roles_json"] = list(command["actor_roles"])
            persisted["entity_refs_json"] = list(command["entity_refs"])
            persisted["parameters_json"] = dict(command["parameters"])
            persisted["automation_invocation_json"] = dict(
                command["automation_invocation"]
            )
            self.repository.commands[identity] = persisted
            self.repository.receipt = {
                "command_id": command["command_id"],
                "work_item_id": item["work_item_id"],
                "run_id": run["run_id"],
                "event_id": "event-command",
            }
            self.repository.runs[run["run_id"]] = {
                "run_id": run["run_id"],
                "status": "RECEIVED",
            }
            return {
                **self.repository.receipt,
                "created": {
                    "command": True,
                    "work_item": True,
                    "run": True,
                    "event": True,
                },
            }

        @staticmethod
        def commit():
            return None

    class _GatewayRepository:
        def __init__(self):
            self.commands = {}
            self.events = _Events()
            self.runs = {}
            self.receipt = None

        def unit_of_work(self):
            return _GatewayUow(self)

        def get_run(self, run_id):
            return copy.deepcopy(self.runs.get(run_id))

    repository = _GatewayRepository()
    gateway = CommandGateway(repository)
    first = gateway.submit(
        original,
        uow_guard=lambda uow: consume_scan_preview(
            uow,
            context=context,
            command=original,
            occurred_at=NOW,
        ),
    )
    assert first.reused is False
    assert len(repository.events.rows) == 1

    with repository.unit_of_work() as replay_uow:
        replay = restore_scan_preview_replay(
            replay_uow,
            source=original.source,
            idempotency_key=original.idempotency_key,
            actor=original.actor,
            trusted_context=trusted_context,
            project_instance_id=PROJECT_ID,
            request_id="formal-request",
            preview_run_id=RUN_ID,
            expected_generation=7,
            expected_configuration_version=3,
        )

    assert replay is not None
    assert replay.command_id == original.command_id
    assert replay.correlation_id == original.correlation_id
    assert replay.parameters == original.parameters
    assert replay.automation_invocation == original.automation_invocation
    second = gateway.submit(replay)
    assert second.reused is True
    assert second.command_id == first.command_id
    assert second.work_item_id == first.work_item_id
    assert second.run_id == first.run_id
    assert len(repository.events.rows) == 1

    with repository.unit_of_work() as replay_uow, pytest.raises(
        OrchestrationError
    ) as omitted_transport:
        restore_scan_preview_replay(
            replay_uow,
            source=original.source,
            idempotency_key=original.idempotency_key,
            actor=original.actor,
            trusted_context={},
            project_instance_id=PROJECT_ID,
            request_id="formal-request",
            preview_run_id=RUN_ID,
            expected_generation=7,
            expected_configuration_version=3,
        )
    assert omitted_transport.value.code == "REQUEST_ID_REUSED"

    for generation, configuration in ((8, 3), (7, 4)):
        with repository.unit_of_work() as replay_uow, pytest.raises(
            OrchestrationError
        ) as stale:
            restore_scan_preview_replay(
                replay_uow,
                source=original.source,
                idempotency_key=original.idempotency_key,
                actor=original.actor,
                trusted_context=trusted_context,
                project_instance_id=PROJECT_ID,
                request_id="formal-request",
                preview_run_id=RUN_ID,
                expected_generation=generation,
                expected_configuration_version=configuration,
            )
        assert stale.value.code == "PROJECT_INVOCATION_STALE"

    with repository.unit_of_work() as replay_uow, pytest.raises(
        OrchestrationError
    ) as wrong_preview:
        restore_scan_preview_replay(
            replay_uow,
            source=original.source,
            idempotency_key=original.idempotency_key,
            actor=original.actor,
            trusted_context=trusted_context,
            project_instance_id=PROJECT_ID,
            request_id="formal-request",
            preview_run_id="22222222-2222-4222-8222-222222222222",
            expected_generation=7,
            expected_configuration_version=3,
        )
    assert wrong_preview.value.code == "REQUEST_ID_REUSED"


def test_planner_persists_compact_scan_impact_for_the_signed_project():
    context = _resolve(_Uow(_fixture())).context
    command = _command(context)

    class _Catalog:
        catalog_hash = "catalog-digest"

        @staticmethod
        def get_capability(tool_name):
            if tool_name != f"automation.{PROJECT_ID}.run":
                return None
            return {
                "version": "1.0.0",
                "operation_type": "internal_projection_write",
                "risk_level": "medium",
                "llm_exposed": False,
                "evidence": [],
                "postconditions": [],
                "_plugin_runtime": {
                    "automation_id": PROJECT_ID,
                    "plugin_id": "sync_scan_codes",
                    "trust_source": "ed25519_first_party",
                },
            }

    plan = DeterministicPlanner(_Catalog()).plan(command, ContextSnapshot(values={}))

    assert plan.impact["entities"][0]["entity_id"] == RUN_ID
    assert plan.impact["entities"][0]["metadata"]["selection_count"] == 2
    assert plan.impact["source_version"]["preview_result_sha256"] == context[
        "preview_result_sha256"
    ]
    assert len(plan.impact["preview_fingerprint"]) == 64
    assert plan.steps[0].arguments["_scan_preview_binding"] == context
    assert "items" not in plan.steps[0].arguments["_scan_preview_binding"]


def test_planner_keeps_dry_run_preview_free_of_formal_binding():
    context = _resolve(_Uow(_fixture())).context
    formal = _command(context)
    preview = Command(
        command_type=formal.command_type,
        source=formal.source,
        actor=formal.actor,
        parameters={
            "tool_name": f"automation.{PROJECT_ID}.run",
            "arguments": {**FORMAL_ARGUMENTS, "dry_run": True},
            "execution_context": {},
        },
        idempotency_key="preview-command-2",
        automation_invocation=formal.automation_invocation,
    )

    class _Catalog:
        catalog_hash = "catalog-digest"

        @staticmethod
        def get_capability(_tool_name):
            return {
                "version": "1.0.0",
                "operation_type": "internal_projection_write",
                "risk_level": "medium",
                "llm_exposed": False,
                "evidence": [],
                "postconditions": [],
                "_plugin_runtime": {
                    "automation_id": PROJECT_ID,
                    "plugin_id": "sync_scan_codes",
                    "trust_source": "ed25519_first_party",
                },
            }

    snapshot = ContextSnapshot(values={})
    catalog = _Catalog()
    plan = DeterministicPlanner(catalog).plan(preview, snapshot)

    assert plan.steps[0].arguments["dry_run"] is True
    assert "_scan_preview_binding" not in plan.steps[0].arguments
    assert plan.steps[0].operation_type is OperationType.READ
    assert plan.steps[0].risk_level is RiskLevel.LOW
    assert plan.steps[0].postconditions == (
        {"name": "authoritative_scan_preview_returned"},
    )
    assert PlanValidator(catalog).validate(plan, snapshot) is plan

    forged = replace(
        plan,
        steps=(
            replace(
                plan.steps[0],
                operation_type=OperationType.INTERNAL_PROJECTION_WRITE,
                risk_level=RiskLevel.MEDIUM,
            ),
        ),
    )
    with pytest.raises(OrchestrationError) as rejected:
        PlanValidator(catalog).validate(forged, snapshot)
    assert rejected.value.code == "TOOL_OPERATION_CHANGED"


def test_planner_rejects_caller_supplied_scan_payload_binding():
    context = _resolve(_Uow(_fixture())).context
    formal = _command(context)
    forged = Command(
        command_type=formal.command_type,
        source=formal.source,
        actor=formal.actor,
        parameters={
            **dict(formal.parameters),
            "arguments": {
                **dict(formal.parameters["arguments"]),
                "_scan_preview_binding": dict(context),
            },
        },
        idempotency_key="forged-binding",
        automation_invocation=formal.automation_invocation,
    )

    class _Catalog:
        catalog_hash = "catalog-digest"

        @staticmethod
        def get_capability(_tool_name):
            return {
                "version": "1.0.0",
                "operation_type": "internal_projection_write",
                "risk_level": "medium",
                "llm_exposed": False,
                "evidence": [],
                "postconditions": [],
                "_plugin_runtime": {
                    "automation_id": PROJECT_ID,
                    "plugin_id": "sync_scan_codes",
                    "trust_source": "ed25519_first_party",
                },
            }

    with pytest.raises(OrchestrationError) as rejected:
        DeterministicPlanner(_Catalog()).plan(forged, ContextSnapshot(values={}))

    assert rejected.value.code == "SCAN_PREVIEW_CONTEXT_INVALID"


def test_scan_phase_rejects_present_null_binding_instead_of_treating_it_as_absent():
    context = _resolve(_Uow(_fixture())).context
    formal = _command(context)
    invalid = Command(
        command_type=formal.command_type,
        source=formal.source,
        actor=formal.actor,
        parameters={
            "tool_name": f"automation.{PROJECT_ID}.run",
            "arguments": {
                **FORMAL_ARGUMENTS,
                "dry_run": True,
                "_scan_preview_binding": None,
            },
            "execution_context": {},
        },
        idempotency_key="preview-null-binding",
        automation_invocation=formal.automation_invocation,
    )

    class _Catalog:
        catalog_hash = "catalog-digest"

        @staticmethod
        def get_capability(_tool_name):
            return {
                "version": "1.0.0",
                "operation_type": "external_write",
                "risk_level": "high",
                "llm_exposed": False,
                "evidence": [],
                "postconditions": [],
                "_plugin_runtime": {
                    "automation_id": PROJECT_ID,
                    "plugin_id": "sync_scan_codes",
                    "trust_source": "ed25519_first_party",
                },
            }

    with pytest.raises(OrchestrationError) as rejected:
        DeterministicPlanner(_Catalog()).plan(
            invalid,
            ContextSnapshot(values={}),
        )
    assert rejected.value.code == "SCAN_PREVIEW_CONTEXT_INVALID"


def test_formal_path_remains_disabled_until_signed_external_governance_is_ready():
    entry = SimpleNamespace(
        automation_id="scan_codes",
        plugin_id="sync_scan_codes",
        trust_source="ed25519_first_party",
        project_full_auto_allowed=True,
        governance_anchor={
            "operation_type": "internal_projection_write",
            "risk_level": "medium",
            "approval": {"required_role": "admin"},
            "permissions": {"required_roles": ["admin"]},
            "project_full_auto_allowed": True,
        },
    )
    with pytest.raises(OrchestrationError) as disabled:
        require_scan_formal_governance(entry)
    assert disabled.value.code == "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED"

    entry.governance_anchor = {
        "operation_type": "external_write",
        "risk_level": "high",
        "approval": {"mode": "required", "required_role": "super_admin"},
        "permissions": {"required_roles": ["super_admin"]},
        "project_full_auto_allowed": True,
        "postconditions": [{"name": "scan_formal_execution_verified"}],
    }
    entry.installed_version = "1.0.23"
    entry.package_sha256 = "a" * 64
    entry.manifest_sha256 = "b" * 64
    entry.committed_generation = 7
    entry.target_generation = 7
    entry.governance_anchor_sha256 = canonical_sha256(entry.governance_anchor)
    entry.committed_snapshot = SimpleNamespace(
        generation=7,
        plugin_id="sync_scan_codes",
        plugin_version="1.0.23",
        package_sha256=entry.package_sha256,
        manifest_sha256=entry.manifest_sha256,
        governance_anchor_sha256=entry.governance_anchor_sha256,
        trust_source=SimpleNamespace(value="ed25519_first_party"),
    )
    require_scan_formal_governance(entry)

    entry.committed_snapshot.manifest_sha256 = "c" * 64
    with pytest.raises(OrchestrationError) as stale:
        require_scan_formal_governance(entry)
    assert stale.value.code == "SCAN_PREVIEW_FORMAL_EXECUTION_DISABLED"


def test_preview_context_tampering_is_rejected_before_planning():
    context = dict(_resolve(_Uow(_fixture())).context)
    context["selection_count"] = 1
    command = _command(context)

    class _Catalog:
        catalog_hash = "catalog-digest"

        @staticmethod
        def get_capability(_tool_name):
            return {
                "version": "1.0.0",
                "operation_type": "internal_projection_write",
                "risk_level": "medium",
                "llm_exposed": False,
                "evidence": [],
                "postconditions": [],
                "_plugin_runtime": {
                    "automation_id": PROJECT_ID,
                    "plugin_id": "sync_scan_codes",
                },
            }

    with pytest.raises(OrchestrationError) as invalid:
        DeterministicPlanner(_Catalog()).plan(command, ContextSnapshot(values={}))
    assert invalid.value.code == "SCAN_PREVIEW_CONTEXT_INVALID"
