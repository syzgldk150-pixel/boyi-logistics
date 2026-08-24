from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.orchestration.command_gateway import CommandGateway
from agent.orchestration.models import Actor, ActorType, Command, ContextSnapshot, OrchestrationError
from agent.orchestration.planner import DeterministicPlanner
from agent.orchestration.scan_preview_binding import (
    SCAN_PREVIEW_CONTEXT_KEY,
    ScanPreviewExpectation,
    consume_scan_preview,
    ensure_scan_preview_active,
    require_scan_formal_governance,
    resolve_scan_preview,
    restore_scan_preview_replay,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    canonical_sha256,
)
from shared.orchestration_repository_support import IdempotencyConflict


PROJECT_ID = "scan_project"
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


def _command(context, *, idempotency_key="formal-command-1") -> Command:
    invocation = AutomationProjectInvocation(
        automation_id=PROJECT_ID,
        automation_generation=7,
        entrypoint=AutomationEntrypoint.CONSOLE,
        contract_id="console",
        contract_hash=CONTRACT_DIGEST,
        policy_version=2,
        project_configuration_version=3,
        request_id="formal-request",
    )
    return Command(
        command_type="automation.project.invoke",
        source="console",
        actor=Actor(ActorType.CONSOLE_ADMIN, "admin-1", ("super_admin",)),
        parameters={
            "tool_name": f"automation.{PROJECT_ID}.run",
            "arguments": {**FORMAL_ARGUMENTS, "dry_run": False},
            "execution_context": {SCAN_PREVIEW_CONTEXT_KEY: dict(context)},
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
    original = _command(context, idempotency_key="formal-replay")

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
            trusted_context={},
            project_instance_id=PROJECT_ID,
            request_id="formal-request",
            preview_run_id=RUN_ID,
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
                    "trust_source": "first_party",
                },
            }

    plan = DeterministicPlanner(_Catalog()).plan(command, ContextSnapshot(values={}))

    assert plan.impact["entities"][0]["entity_id"] == RUN_ID
    assert plan.impact["entities"][0]["metadata"]["selection_count"] == 2
    assert plan.impact["source_version"]["preview_result_sha256"] == context[
        "preview_result_sha256"
    ]
    assert len(plan.impact["preview_fingerprint"]) == 64


def test_formal_path_remains_disabled_until_signed_external_governance_is_ready():
    entry = SimpleNamespace(
        plugin_id="sync_scan_codes",
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
        "approval": {"required_role": "super_admin"},
        "permissions": {"required_roles": ["super_admin"]},
        "project_full_auto_allowed": True,
    }
    require_scan_formal_governance(entry)


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
