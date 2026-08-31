from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.orchestration.models import Actor, ActorType, Command, OrchestrationError
from agent.orchestration.selection_preview_binding import (
    SELECTION_PREVIEW_CONTEXT_KEY,
    SelectionPreviewExpectation,
    consume_selection_preview,
    ensure_selection_preview_active,
    is_selection_preview_project,
    resolve_selection_preview,
    restore_selection_preview_replay,
    selection_confirmation_arguments,
    selection_preview_public_projection,
    validate_selection_preview_context,
)
from shared.automation_project_authorization import (
    AutomationEntrypoint,
    AutomationProjectInvocation,
    canonical_sha256,
)
from shared.orchestration_repository_support import IdempotencyConflict


PROJECT_ID = "self_pickup_problem_upload"
RUN_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def get(self, identity, *, for_update=False):
        del for_update
        value = self.rows.get(identity)
        return copy.deepcopy(value) if value is not None else None

    def get_by_idempotency(self, source, idempotency_key, *, for_update=False):
        del for_update
        for value in self.rows.values():
            if (
                value.get("source") == source
                and value.get("idempotency_key") == idempotency_key
            ):
                return copy.deepcopy(value)
        return None


class _Steps:
    def __init__(self, rows):
        self.rows = rows

    def list_for_run(self, run_id):
        return copy.deepcopy(self.rows.get(run_id, []))


class _Uow:
    def __init__(self, fixture):
        self.runs = _Rows(fixture["runs"])
        self.commands = _Rows(fixture["commands"])
        self.steps = _Steps(fixture["steps"])
        self.events = _Events()


class _Events:
    def __init__(self):
        self.rows = []

    def append_with_outbox(self, event, outbox):
        del outbox
        if any(
            row["event_type"] == event["event_type"]
            and row["source_event_id"] == event["source_event_id"]
            for row in self.rows
        ):
            raise IdempotencyConflict("selection preview already consumed")
        self.rows.append(copy.deepcopy(event))


def _expectation() -> SelectionPreviewExpectation:
    return SelectionPreviewExpectation(
        project_instance_id=PROJECT_ID,
        plugin_id=PROJECT_ID,
        generation=4,
        contract_digest="c" * 64,
        configuration_version=7,
    )


def _fixture(*, observed_at=NOW - timedelta(minutes=2)):
    candidates = [
        {
            "arrival_count": 2,
            "bill_code": "R0001",
            "delivery_method": "自提",
            "destination_site": "邵阳大祥S站",
            "goods_count": 2,
            "row_number": 12,
            "source_id": "source-one",
            "source_name": "每日到货表",
        },
        {
            "arrival_count": 1,
            "bill_code": "R0002",
            "delivery_method": "自提",
            "destination_site": "邵阳自提部",
            "goods_count": 1,
            "row_number": 18,
            "source_id": "source-one",
            "source_name": "每日到货表",
        },
    ]
    result = {
        "status": "SUCCESS",
        "data": {
            "dry_run": True,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "preview_fingerprint": "f" * 64,
            "duplicate_source_rows": 0,
        },
        "meta": {"observed_at": observed_at.isoformat()},
        "warnings": [],
        "error": None,
    }
    command_id = "preview-command"
    return {
        "runs": {
            RUN_ID: {
                "run_id": RUN_ID,
                "command_id": command_id,
                "status": "COMPLETED",
            }
        },
        "commands": {
            command_id: {
                "command_type": "automation.project.invoke",
                "parameters_json": {
                    "tool_name": f"automation.{PROJECT_ID}.run",
                    "arguments": {
                        "dry_run": True,
                        "selected_bill_codes": [],
                        "preview_fingerprint": "",
                    },
                },
                "automation_invocation_json": {
                    "automation_id": PROJECT_ID,
                    "automation_generation": 4,
                    "contract_hash": "c" * 64,
                    "project_configuration_version": 7,
                },
            }
        },
        "steps": {
            RUN_ID: [
                {
                    "step_id": "preview-step",
                    "status": "COMPLETED",
                    "postcondition_status": "VERIFIED",
                    "tool_name": f"automation.{PROJECT_ID}.run",
                    "result_summary_json": result,
                    "result_sha256": canonical_sha256(result),
                }
            ]
        },
    }


def test_public_projection_exposes_candidates_but_not_server_fingerprint():
    projection = selection_preview_public_projection(
        _Uow(_fixture()),
        preview_run_id=RUN_ID,
        expectation=_expectation(),
        now=NOW,
    )

    assert projection["candidate_count"] == 2
    assert [item["bill_code"] for item in projection["candidates"]] == [
        "R0001",
        "R0002",
    ]
    assert projection["can_confirm"] is True
    assert "preview_fingerprint" not in projection


def test_confirmation_uses_persisted_fingerprint_and_exact_selected_subset():
    arguments = selection_confirmation_arguments(
        _Uow(_fixture()),
        preview_run_id=RUN_ID,
        expectation=_expectation(),
        selected_bill_codes=["R0002"],
        now=NOW,
    )

    assert arguments == {
        "dry_run": False,
        "selected_bill_codes": ["R0002"],
        "preview_fingerprint": "f" * 64,
    }


def test_confirmation_blocks_expired_or_unavailable_selection():
    with pytest.raises(OrchestrationError, match="超过十五分钟") as expired:
        selection_confirmation_arguments(
            _Uow(_fixture(observed_at=NOW - timedelta(minutes=16))),
            preview_run_id=RUN_ID,
            expectation=_expectation(),
            selected_bill_codes=["R0001"],
            now=NOW,
        )
    assert expired.value.code == "SELECTION_PREVIEW_EXPIRED"

    with pytest.raises(OrchestrationError, match="不在当前候选") as unavailable:
        selection_confirmation_arguments(
            _Uow(_fixture()),
            preview_run_id=RUN_ID,
            expectation=_expectation(),
            selected_bill_codes=["R9999"],
            now=NOW,
        )
    assert unavailable.value.code == "SELECTION_CHANGED"


def test_tampered_persisted_result_is_rejected():
    fixture = _fixture()
    fixture["steps"][RUN_ID][0]["result_summary_json"]["data"]["candidates"][0][
        "bill_code"
    ] = "R-TAMPERED"

    with pytest.raises(OrchestrationError, match="校验失败") as error:
        selection_preview_public_projection(
            _Uow(fixture),
            preview_run_id=RUN_ID,
            expectation=_expectation(),
            now=NOW,
        )
    assert error.value.code == "SELECTION_PREVIEW_INVALID"


def _service_v2_expectation(
    *,
    entrypoint: str = "console",
    contribution_id: str = "execute_console",
) -> SelectionPreviewExpectation:
    return SelectionPreviewExpectation(
        project_instance_id="self_pickup_problem_upload_v2",
        plugin_id="self_pickup_problem_upload_v2",
        generation=4,
        contract_digest="c" * 64,
        configuration_version=7,
        runtime_model="SERVICE_V2",
        entrypoint=entrypoint,
        contribution_id=contribution_id,
        title="预览自提问题件候选",
    )


def test_service_v2_feishu_only_selection_is_still_host_governed() -> None:
    entry = SimpleNamespace(
        automation_id="selection-v2",
        plugin_id="selection_v2",
        runtime_model="SERVICE_V2",
        contributions={
            "console": (),
            "feishu": (
                {
                    "id": "execute_feishu",
                    "service": "plugin.selection_v2.runner@1",
                    "operation": "execute",
                    "selection_preview_operation": "preview",
                },
            ),
        },
    )

    assert is_selection_preview_project(entry) is True


def _service_v2_fixture():
    fixture = _fixture()
    command = fixture["commands"]["preview-command"]
    command["parameters_json"]["tool_name"] = (
        "automation.self_pickup_problem_upload_v2.run"
    )
    command["parameters_json"]["execution_context"] = {
        "selection_phase": "PREVIEW"
    }
    command["source"] = "console"
    command["automation_invocation_json"]["automation_id"] = (
        "self_pickup_problem_upload_v2"
    )
    command["automation_invocation_json"]["contract_id"] = "execute_console"
    command["automation_invocation_json"]["entrypoint"] = "console"
    fixture["steps"][RUN_ID][0]["tool_name"] = (
        "automation.self_pickup_problem_upload_v2.run"
    )
    return fixture


def test_service_v2_projection_binds_signed_contribution_and_host_phase():
    fixture = _service_v2_fixture()
    projection = selection_preview_public_projection(
        _Uow(fixture),
        preview_run_id=RUN_ID,
        expectation=_service_v2_expectation(),
        now=NOW,
    )

    assert projection["automation_id"] == "self_pickup_problem_upload_v2"
    assert projection["title"] == "预览自提问题件候选"
    assert projection["summary"] == {"duplicate_source_rows": 0}

    fixture["commands"]["preview-command"]["automation_invocation_json"][
        "contract_id"
    ] = "unsigned_contribution"
    with pytest.raises(OrchestrationError) as raised:
        selection_preview_public_projection(
            _Uow(fixture),
            preview_run_id=RUN_ID,
            expectation=_service_v2_expectation(),
            now=NOW,
        )
    assert raised.value.code == "SELECTION_PREVIEW_STALE"


@pytest.mark.parametrize(
    "drift",
    ("command_source", "invocation_entrypoint", "contribution_id"),
)
def test_service_v2_projection_rejects_cross_entrypoint_preview(drift: str) -> None:
    fixture = _service_v2_fixture()
    command = fixture["commands"]["preview-command"]
    if drift == "command_source":
        command["source"] = "feishu"
    elif drift == "invocation_entrypoint":
        command["automation_invocation_json"]["entrypoint"] = "feishu"
    else:
        command["automation_invocation_json"]["contract_id"] = "execute_feishu"

    with pytest.raises(OrchestrationError) as raised:
        selection_preview_public_projection(
            _Uow(fixture),
            preview_run_id=RUN_ID,
            expectation=_service_v2_expectation(),
            now=NOW,
        )
    assert raised.value.code == "SELECTION_PREVIEW_STALE"


def test_service_v2_feishu_expectation_accepts_only_feishu_preview() -> None:
    fixture = _service_v2_fixture()
    command = fixture["commands"]["preview-command"]
    command["source"] = "feishu"
    command["automation_invocation_json"]["entrypoint"] = "feishu"
    command["automation_invocation_json"]["contract_id"] = "execute_feishu"

    projection = selection_preview_public_projection(
        _Uow(fixture),
        preview_run_id=RUN_ID,
        expectation=_service_v2_expectation(
            entrypoint="feishu",
            contribution_id="execute_feishu",
        ),
        now=NOW,
    )

    assert projection["preview_run_id"] == RUN_ID


def test_service_v2_resolution_builds_valid_compact_context() -> None:
    resolution = resolve_selection_preview(
        _Uow(_service_v2_fixture()),
        preview_run_id=RUN_ID,
        expectation=_service_v2_expectation(),
        selected_bill_codes=["R0002"],
        now=NOW,
        for_update=False,
    )

    assert resolution.formal_arguments == {
        "dry_run": False,
        "selected_bill_codes": ["R0002"],
        "preview_fingerprint": "f" * 64,
    }
    context = validate_selection_preview_context(resolution.context)
    assert context["entrypoint"] == "console"
    assert context["contribution_id"] == "execute_console"
    assert context["preview_run_id"] == RUN_ID
    assert context["preview_step_id"] == "preview-step"
    assert context["candidate_count"] == 2
    assert context["selection_count"] == 1
    assert context["formal_arguments_sha256"] == canonical_sha256(
        resolution.formal_arguments
    )
    ensure_selection_preview_active(context, now=NOW)

    with pytest.raises(OrchestrationError) as expired:
        ensure_selection_preview_active(context, now=NOW + timedelta(minutes=14))
    assert expired.value.code == "SELECTION_PREVIEW_EXPIRED"

    tampered = dict(context)
    tampered["candidate_count"] = 1
    with pytest.raises(OrchestrationError) as invalid:
        validate_selection_preview_context(tampered)
    assert invalid.value.code == "SELECTION_PREVIEW_CONTEXT_INVALID"


def test_service_v2_selection_preview_is_consumed_once_across_requests():
    uow = _Uow(_service_v2_fixture())
    expectation = _service_v2_expectation()
    resolution = resolve_selection_preview(
        uow,
        preview_run_id=RUN_ID,
        expectation=expectation,
        selected_bill_codes=["R0001"],
        now=NOW,
        for_update=True,
    )
    first = SimpleNamespace(
        source="console",
        idempotency_key="selection:first",
        correlation_id="correlation-first",
        command_id="command-first",
    )
    consume_selection_preview(
        uow,
        expectation=expectation,
        context=resolution.context,
        command=first,
        occurred_at=NOW,
    )

    assert len(uow.events.rows) == 1
    assert uow.events.rows[0]["payload"]["selection_count"] == 1
    with pytest.raises(OrchestrationError) as raised:
        consume_selection_preview(
            uow,
            expectation=expectation,
            context=resolution.context,
            command=SimpleNamespace(
                source="console",
                idempotency_key="selection:second",
                correlation_id="correlation-second",
                command_id="command-second",
            ),
            occurred_at=NOW,
        )
    assert raised.value.code == "SELECTION_PREVIEW_ALREADY_CONSUMED"


def test_action_v1_preview_consumption_remains_unchanged() -> None:
    uow = _Uow(_fixture())

    consume_selection_preview(
        uow,
        expectation=_expectation(),
        context=None,
        command=SimpleNamespace(
            source="console",
            idempotency_key="legacy-selection",
        ),
        occurred_at=NOW,
    )

    assert uow.events.rows == []


def _accepted_replay_fixture():
    fixture = _service_v2_fixture()
    expectation = _service_v2_expectation()
    resolution = resolve_selection_preview(
        _Uow(fixture),
        preview_run_id=RUN_ID,
        expectation=expectation,
        selected_bill_codes=["R0001"],
        now=NOW,
        for_update=False,
    )
    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "admin-1",
        ("admin", "super_admin"),
    )
    request_id = "selection-request-1"
    invocation = AutomationProjectInvocation(
        automation_id="self_pickup_problem_upload_v2",
        automation_generation=4,
        entrypoint=AutomationEntrypoint.CONSOLE,
        contract_id="execute_console",
        contract_hash="c" * 64,
        policy_version=3,
        project_configuration_version=7,
        request_id=request_id,
    )
    trusted_context = {"transport_marker": "trusted-console"}
    parameters = {
        "tool_name": "automation.self_pickup_problem_upload_v2.run",
        "arguments": dict(resolution.formal_arguments),
        "execution_context": {
            "project_request_id": request_id,
            "entrypoint": "console",
            "occurred_at": resolution.context["observed_at"],
            "contribution_id": "execute_console",
            "selection_phase": "FORMAL",
            "dynamic_inputs": dict(resolution.formal_arguments),
            SELECTION_PREVIEW_CONTEXT_KEY: dict(resolution.context),
            **trusted_context,
        },
    }
    command = Command(
        command_type="automation.project.invoke",
        source="console",
        actor=actor,
        parameters=parameters,
        idempotency_key="automation:selection:request-1",
        automation_invocation=invocation,
        command_id="accepted-command",
        correlation_id="accepted-correlation",
        requested_at=NOW,
    )
    fixture["commands"][command.command_id] = {
        "command_id": command.command_id,
        "command_type": command.command_type,
        "source": command.source,
        "actor_type": actor.actor_type.value,
        "actor_id": actor.actor_id,
        "actor_roles_json": list(actor.roles),
        "parameters_json": dict(parameters),
        "idempotency_key": command.idempotency_key,
        "entity_refs_json": [],
        "automation_invocation_json": invocation.to_dict(),
        "correlation_id": command.correlation_id,
        "requested_at": command.requested_at,
    }
    return _Uow(fixture), actor, command, trusted_context


def _restore_kwargs(actor, command, trusted_context):
    return {
        "source": "console",
        "idempotency_key": command.idempotency_key,
        "actor": actor,
        "trusted_context": trusted_context,
        "project_instance_id": "self_pickup_problem_upload_v2",
        "request_id": "selection-request-1",
        "preview_run_id": RUN_ID,
        "selected_bill_codes": ["R0001"],
        "expected_entrypoint": "console",
        "expected_contribution_id": "execute_console",
        "expected_generation": 4,
        "expected_configuration_version": 7,
    }


def test_service_v2_replay_restores_exact_command_after_preview_expiry() -> None:
    uow, actor, command, trusted_context = _accepted_replay_fixture()
    preview_context = command.parameters["execution_context"][
        SELECTION_PREVIEW_CONTEXT_KEY
    ]
    with pytest.raises(OrchestrationError) as expired:
        ensure_selection_preview_active(
            preview_context,
            now=NOW + timedelta(minutes=14),
        )
    assert expired.value.code == "SELECTION_PREVIEW_EXPIRED"

    replay = restore_selection_preview_replay(
        uow,
        **_restore_kwargs(actor, command, trusted_context),
    )

    assert replay is not None
    assert replay.command_id == command.command_id
    assert replay.correlation_id == command.correlation_id
    assert replay.requested_at == command.requested_at
    assert replay.parameters == command.parameters
    assert replay.automation_invocation == command.automation_invocation


@pytest.mark.parametrize(
    "drift",
    (
        "actor",
        "roles",
        "transport",
        "project",
        "request",
        "entrypoint",
        "contribution",
        "preview",
        "selection",
    ),
)
def test_service_v2_replay_rejects_identity_or_selection_drift(drift: str) -> None:
    uow, actor, command, trusted_context = _accepted_replay_fixture()
    kwargs = _restore_kwargs(actor, command, trusted_context)
    if drift == "actor":
        kwargs["actor"] = Actor(
            ActorType.CONSOLE_ADMIN,
            "admin-2",
            actor.roles,
        )
    elif drift == "roles":
        kwargs["actor"] = Actor(
            ActorType.CONSOLE_ADMIN,
            actor.actor_id,
            ("super_admin",),
        )
    elif drift == "transport":
        kwargs["trusted_context"] = {"transport_marker": "changed"}
    elif drift == "project":
        kwargs["project_instance_id"] = "another-selection-project"
    elif drift == "request":
        kwargs["request_id"] = "selection-request-2"
    elif drift == "entrypoint":
        kwargs["expected_entrypoint"] = "feishu"
    elif drift == "contribution":
        kwargs["expected_contribution_id"] = "execute_feishu"
    elif drift == "preview":
        kwargs["preview_run_id"] = "22222222-2222-4222-8222-222222222222"
    else:
        kwargs["selected_bill_codes"] = ["R0002"]

    with pytest.raises(OrchestrationError) as raised:
        restore_selection_preview_replay(uow, **kwargs)
    assert raised.value.code == "REQUEST_ID_REUSED"


@pytest.mark.parametrize(
    ("field", "value"),
    (("expected_generation", 5), ("expected_configuration_version", 8)),
)
def test_service_v2_replay_keeps_explicit_contract_drift_stale(
    field: str,
    value: int,
) -> None:
    uow, actor, command, trusted_context = _accepted_replay_fixture()
    kwargs = _restore_kwargs(actor, command, trusted_context)
    kwargs[field] = value

    with pytest.raises(OrchestrationError) as raised:
        restore_selection_preview_replay(uow, **kwargs)
    assert raised.value.code == "PROJECT_INVOCATION_STALE"
