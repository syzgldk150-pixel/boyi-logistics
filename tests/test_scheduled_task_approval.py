from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.orchestration.models import Actor, ActorType, OrchestrationError
from agent.orchestration.scheduled_task_approval_service import (
    BOOTSTRAP_COMPLETION_REQUEST_ID,
    BOOTSTRAP_COMPLETION_TASK_ID,
    ScheduledTaskApprovalService,
)
from agent.tool_registry import ToolRegistry
from shared.scheduled_task_approval import (
    ScheduledTaskApprovalContractError,
    build_scheduled_task_contract,
    exemption_eligibility,
)
from shared.scheduled_task_contracts import ScheduledTaskContractError


def _task(**overrides):
    row = {
        "id": "clockin_daxiang_1830",
        "name": "display-only",
        "tool_name": "clock_in_dual",
        "tool_params": {
            "account_id": "ronghui_default",
            "sitecode": "7390004",
            "sitefbcode": "73901",
            "sitename": "邵阳大祥站",
            "sitefbname": "邵阳操作场",
            "first_type": "交件到港",
            "second_type": "接件离港",
            "delay_seconds": 2,
        },
        "cron_expression": "30 18 * * *",
        "enabled": True,
        "configuration_version": 1,
    }
    row.update(overrides)
    return row


class _BootstrapPolicyStore:
    def __init__(self, repository):
        self._repository = repository

    def get_event_by_request(self, task_id, request_id):
        event = self._repository.events.get((task_id, request_id))
        return deepcopy(event) if event is not None else None

    def append_event(self, row):
        key = (str(row["task_id"]), str(row["request_id"]))
        self._repository.events.setdefault(key, deepcopy(dict(row)))
        return deepcopy(self._repository.events[key])


class _BootstrapUow:
    def __init__(self, repository):
        self.scheduled_policies = _BootstrapPolicyStore(repository)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def commit(self):
        return None


class _BootstrapRepo:
    def __init__(self, rows):
        self.rows = list(rows)
        self.events = {}

    def list_scheduled_task_policy_rows(self):
        return deepcopy(self.rows)

    def unit_of_work(self):
        return _BootstrapUow(self)


def test_contract_is_privacy_safe_stable_and_display_name_independent():
    capability = ToolRegistry().get_capability("clock_in_dual")
    first = build_scheduled_task_contract(_task(), capability)
    renamed = build_scheduled_task_contract(_task(name="new display"), capability)

    assert first.contract_hash == renamed.contract_hash
    assert "tool_params" not in first.snapshot
    assert "account_id" not in first.snapshot
    assert first.snapshot["arguments_hash"]
    assert first.snapshot["enabled"] is True


@pytest.mark.parametrize(
    ("change", "expected_equal"),
    (
        ({"configuration_version": 2}, False),
        ({"cron_expression": "31 18 * * *"}, False),
        ({"enabled": False}, False),
    ),
)
def test_material_task_changes_invalidate_contract(change, expected_equal):
    capability = ToolRegistry().get_capability("clock_in_dual")
    original = build_scheduled_task_contract(_task(), capability)
    if change.get("enabled") is False:
        with pytest.raises(ScheduledTaskApprovalContractError, match="TASK_DISABLED"):
            build_scheduled_task_contract(_task(**change), capability)
    else:
        changed = build_scheduled_task_contract(_task(**change), capability)
        assert (original.contract_hash == changed.contract_hash) is expected_equal


@pytest.mark.parametrize(
    "cron_expression",
    ("0 9 * * 1", "*/5 9 * * *", "0 24 * * *", "60 9 * * *", "@startup"),
)
def test_non_exact_daily_cron_cannot_receive_exemption(cron_expression):
    capability = ToolRegistry().get_capability("clock_in_dual")
    with pytest.raises(ScheduledTaskApprovalContractError, match="CRON_NOT_EXACT_DAILY_TIME"):
        build_scheduled_task_contract(_task(cron_expression=cron_expression), capability)


def test_only_reviewed_startup_profile_can_build_special_cron_contract():
    service = ScheduledTaskApprovalService(
        SimpleNamespace(),
        ToolRegistry(),
        enabled_finance_platforms=("ronghui",),
    )
    startup = {
        "id": "finance_startup_catchup",
        "name": "startup catch-up",
        "tool_name": "sync_finance_bills",
        "tool_params": {
            "mode": "sync",
            "platform": "ronghui",
            "rescan_days": 7,
            "_startup_catchup": True,
        },
        "cron_expression": "@startup",
        "enabled": True,
        "configuration_version": 1,
    }

    _capability, contract, rules = service._contract_for_task(
        startup,
        require_reviewed=True,
    )

    assert contract is not None
    assert contract.snapshot["task_id"] == "finance_startup_catchup"
    assert contract.snapshot["cron_expression"] == "@startup"
    assert rules == {}

    with pytest.raises(ScheduledTaskContractError) as error:
        service._contract_for_task(
            {**startup, "id": "finance_bills_0010"},
            require_reviewed=True,
        )
    assert "CRON_NOT_EXACT_DAILY_TIME" in str(error.value)


def test_policy_list_marks_unsupported_cron_as_fail_closed():
    class _Repo:
        @staticmethod
        def list_scheduled_task_policy_rows():
            return [
                _task(
                    cron_expression="0 9 * * 1",
                    mode="EXACT_SCHEDULE_EXEMPT",
                    contract_hash="previous-contract",
                    tool_contract_hash="previous-tool",
                    policy_version=2,
                )
            ]

    item = ScheduledTaskApprovalService(_Repo(), ToolRegistry()).list_policies()["items"][0]

    assert item["configured_mode"] == "EXACT_SCHEDULE_EXEMPT"
    assert item["effective_mode"] == "REQUIRE_EACH_RUN"
    assert item["effective_status"] == "UNSUPPORTED"
    assert item["can_exempt"] is False
    assert item["invalid_reason"] == "TASK_CONTRACT_INVALID"
    assert item["configuration_version"] == 1


def test_governed_tool_change_invalidates_but_description_does_not():
    capability = ToolRegistry().get_capability("clock_in_dual")
    original = build_scheduled_task_contract(_task(), capability)
    display_change = deepcopy(capability)
    display_change["description"] = "wording only"
    governed_change = deepcopy(capability)
    governed_change["timeout"] += 1

    assert build_scheduled_task_contract(_task(), display_change).contract_hash == original.contract_hash
    assert build_scheduled_task_contract(_task(), governed_change).contract_hash != original.contract_hash


def test_only_explicit_schedule_allowlist_tools_are_eligible():
    catalog = ToolRegistry()
    assert exemption_eligibility(catalog.get_capability("clock_in_dual")) == (True, None)
    eligible, reason = exemption_eligibility(catalog.get_capability("customer_service_problem_reply"))
    assert eligible is False
    assert reason == "TOOL_REQUIRES_PER_RUN_APPROVAL"


def test_policy_write_requires_signed_console_super_admin_before_repository_access():
    class _Repo:
        def unit_of_work(self):
            raise AssertionError("repository must not be reached")

    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())
    common = {
        "task_ids": ["task-1"],
        "mode": "REQUIRE_EACH_RUN",
        "comment": "",
        "request_id": "00000000-0000-4000-8000-000000000001",
        "expected_versions": {"task-1": 1},
        "expected_configuration_versions": {"task-1": 1},
    }
    actors = (
        Actor(ActorType.CONSOLE_ADMIN, "1", roles=("admin",), authenticated_by="mysql_admin_session"),
        Actor(ActorType.CONSOLE_ADMIN, "1", roles=("super_admin",), authenticated_by="basic_auth"),
        Actor(ActorType.FEISHU_USER, "u", roles=("super_admin",), authenticated_by="mysql_admin_session"),
    )
    for actor in actors:
        with pytest.raises(OrchestrationError) as error:
            service.set_policies(actor=actor, **common)
        assert error.value.code == "ACTION_FORBIDDEN"


def test_request_id_must_be_uuid_before_repository_access():
    class _Repo:
        def unit_of_work(self):
            raise AssertionError("repository must not be reached")

    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())
    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    with pytest.raises(OrchestrationError) as error:
        service.set_policies(
            task_ids=["task-1"],
            mode="REQUIRE_EACH_RUN",
            comment="",
            request_id="not-a-uuid",
            expected_versions={"task-1": 1},
            expected_configuration_versions={"task-1": 1},
            actor=actor,
        )
    assert error.value.code == "INVALID_REQUEST_ID"


def test_policy_write_requires_exact_configuration_version_coverage_before_repository_access():
    class _Repo:
        def unit_of_work(self):
            raise AssertionError("repository must not be reached")

    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())

    with pytest.raises(OrchestrationError) as error:
        service.set_policies(
            task_ids=["task-1"],
            mode="REQUIRE_EACH_RUN",
            comment="",
            request_id="00000000-0000-4000-8000-000000000001",
            expected_versions={"task-1": 1},
            expected_configuration_versions={},
            actor=actor,
        )

    assert error.value.code == "TASK_CONFIGURATION_VERSION_REQUIRED"


@pytest.mark.parametrize("mode", ("REQUIRE_EACH_RUN", "EXACT_SCHEDULE_EXEMPT"))
def test_policy_write_rejects_stale_task_configuration_after_lock(monkeypatch, mode):
    task_id = "clockin_daxiang_1830"

    class _Policies:
        @staticmethod
        def lock_task(selected_task_id):
            assert selected_task_id == task_id
            return _task(configuration_version=2)

        @staticmethod
        def ensure_default(selected_task_id):
            assert selected_task_id == task_id
            return {"version": 1, "mode": "REQUIRE_EACH_RUN"}

        @staticmethod
        def list_events_by_request(*_args, **_kwargs):
            pytest.fail("idempotent replay must not bypass configuration-version validation")

    class _Uow:
        scheduled_policies = _Policies()

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    class _Repo:
        @staticmethod
        def unit_of_work():
            return _Uow()

    actor = Actor(
        ActorType.CONSOLE_ADMIN,
        "1",
        roles=("super_admin",),
        authenticated_by="mysql_admin_session",
    )
    service = ScheduledTaskApprovalService(_Repo(), ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("stale task configuration must not be authorized"),
    )

    with pytest.raises(OrchestrationError) as error:
        service.set_policies(
            task_ids=[task_id],
            mode=mode,
            comment="",
            request_id="00000000-0000-4000-8000-000000000001",
            expected_versions={task_id: 1},
            expected_configuration_versions={task_id: 1},
            actor=actor,
        )

    assert error.value.code == "TASK_CONFIGURATION_VERSION_CONFLICT"
    assert error.value.details == {"task_id": task_id}


def test_task_configuration_conflict_maps_to_http_409():
    from main import orchestration_error_handler

    response = asyncio.run(
        orchestration_error_handler(
            SimpleNamespace(
                url=SimpleNamespace(
                    path="/internal/v1/scheduled-task-approval-policies"
                )
            ),
            OrchestrationError(
                "TASK_CONFIGURATION_VERSION_CONFLICT",
                "refresh and review",
            ),
        )
    )

    assert response.status_code == 409
    assert b"TASK_CONFIGURATION_VERSION_CONFLICT" in response.body


@pytest.mark.parametrize("initially_present", (False, True))
def test_bootstrap_marker_seals_absent_or_disabled_reviewed_task(monkeypatch, initially_present):
    initial_rows = [_task(enabled=False)] if initially_present else []
    repository = _BootstrapRepo(initial_rows)
    service = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("sealed tasks must not be evaluated"),
    )

    assert service.bootstrap_reviewed_policies() == {
        "reviewed_candidates": 0,
        "created": 0,
        "already_present": 0,
        "explicitly_configured": 0,
        "rejected": 0,
        "completed": 1,
    }
    assert (
        BOOTSTRAP_COMPLETION_TASK_ID,
        BOOTSTRAP_COMPLETION_REQUEST_ID,
    ) in repository.events

    repository.rows = [
        _task(
            enabled=True,
            mode="REQUIRE_EACH_RUN",
            policy_version=1,
            approved_by_actor_id=None,
        )
    ]
    restarted = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        restarted,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("completed bootstrap must never re-authorize"),
    )

    assert restarted.bootstrap_reviewed_policies()["completed"] == 1
    assert repository.rows[0]["mode"] == "REQUIRE_EACH_RUN"


def test_bootstrap_respects_explicit_require_each_run_policy(monkeypatch):
    repository = _BootstrapRepo(
        [
            _task(
                mode="REQUIRE_EACH_RUN",
                policy_version=2,
                approved_by_actor_id="admin-1",
            )
        ]
    )
    service = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("explicit policies must not be bootstrapped"),
    )

    assert service.bootstrap_reviewed_policies() == {
        "reviewed_candidates": 1,
        "created": 0,
        "already_present": 0,
        "explicitly_configured": 1,
        "rejected": 0,
        "completed": 1,
    }


def test_bootstrap_does_not_regrant_or_rehash_existing_exact_policy(monkeypatch):
    row = _task(
        cron_expression="0 9 * * 1",
        mode="EXACT_SCHEDULE_EXEMPT",
        contract_hash="stale-contract",
        tool_contract_hash="stale-tool",
        policy_version=2,
        approved_by_actor_id="admin-1",
    )
    repository = _BootstrapRepo([row])
    service = ScheduledTaskApprovalService(repository, ToolRegistry())
    monkeypatch.setattr(
        service,
        "_contract_for_task",
        lambda *_args, **_kwargs: pytest.fail("existing exact policy must not be re-authorized"),
    )

    result = service.bootstrap_reviewed_policies()

    assert result["already_present"] == 1
    assert result["completed"] == 1
    assert repository.rows[0]["contract_hash"] == "stale-contract"
    assert repository.rows[0]["tool_contract_hash"] == "stale-tool"


def test_bootstrap_invalid_candidate_has_no_marker_and_retries_after_repair(monkeypatch):
    repository = _BootstrapRepo([_task(cron_expression="0 9 * * 1")])
    service = ScheduledTaskApprovalService(repository, ToolRegistry())

    first = service.bootstrap_reviewed_policies()

    assert first["rejected"] == 1
    assert first["completed"] == 0
    assert repository.events == {}

    repository.rows[0]["cron_expression"] = "30 18 * * *"
    bootstrapped = []

    def record_bootstrap(row, **_kwargs):
        bootstrapped.append(str(row["id"]))
        repository.rows[0]["mode"] = "EXACT_SCHEDULE_EXEMPT"

    monkeypatch.setattr(service, "_bootstrap_one", record_bootstrap)
    second = service.bootstrap_reviewed_policies()

    assert second["created"] == 1
    assert second["rejected"] == 0
    assert second["completed"] == 1
    assert bootstrapped == ["clockin_daxiang_1830"]
    assert (
        BOOTSTRAP_COMPLETION_TASK_ID,
        BOOTSTRAP_COMPLETION_REQUEST_ID,
    ) in repository.events
